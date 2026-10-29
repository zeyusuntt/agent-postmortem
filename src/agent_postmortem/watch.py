"""`agent-postmortem watch`: a read-only follower for one workflow run
(docs/DESIGN.md 4.12; .reviews/proposals/2026-09-v0.2-guided-rescue.md
section 7).

Designed to be the command line a Claude Code Monitor runs: it prints one
START line first, then one line per event a person would act on (KILL, FAILED,
OK, QUIET) and nothing otherwise, then a terminal DONE or LIMIT line and exits.

Polling never re-reads a file it does not have to: the journal is read by
appended bytes only (`_read_new_journal_lines` keeps a byte offset and
restarts from 0 when the file is smaller than that offset -- a truncated or
replaced journal, which resets every other piece of derived state too and
prints one RESET line); a transcript is only ever stat'd for its mtime
(QUIET), or fully parsed once, the moment a KILL or FAILED needs its last
line or its silence span -- never polled incrementally. A partial trailing
journal line growing past 1 MB, or a complete line that is not a JSON
object, is malformed evidence (MALFORMED); a KILL/FAILED/OK event this poll
cannot yet resolve (the rate limit, or a transcript not on disk yet) is
retried on later polls, bounded at 12, rather than dropped.

DONE (docs/DESIGN.md 4.12, N8b K5.4) is eligible at once from a lifecycle terminal
of completed, failed, or stopped (an accepted notification, a TaskStop, or a
synchronous launch result), or after `--done-grace` once every journal key is
resolved. The lifecycle is evaluated from watch's own in-memory occurrences and
a parent index refreshed only when the parent transcript's stat signature
changes; a RESET starts a new journal epoch whose watermark hides the ended
epoch's generations.

`_now` and `_sleep` are module attributes so a test can monkeypatch both: the
suite never sleeps for a real second, and `_sleep` doubles as the point
where a scripted test appends the next bytes of a growing journal or
transcript.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from . import detectors, disclosure, ledger, reader, report
from .detectors import attempt_failed as _attempt_failed
from .detectors.base import final_event, meaningful_event_before
from .markers import KILL_SENTINEL_PREFIX

DEFAULT_INTERVAL = 5.0
DEFAULT_MAX_HOURS = 6.0
DEFAULT_QUIET_AFTER = 300.0
# A workflow that runs its phases one after another has a moment between
# two phases where every key seen so far is resolved. DONE waits this long
# with no new `started` before it is printed, so that gap is not mistaken
# for the end of the run.
DEFAULT_DONE_GRACE = 30.0

# How long `watch` waits, printing nothing, for a run directory (and its
# journal.jsonl) that does not exist yet -- the launch hook fires before the
# Workflow tool has created either (docs/DESIGN.md 7.2's D8).
_JOURNAL_WAIT_SECONDS = 60.0

# At most one line per agent per this many seconds, except DONE and LIMIT.
_RATE_LIMIT_SECONDS = 60.0

# docs/DESIGN.md 4.12 (release finding F9): a partial (no trailing newline
# yet) journal line is withheld and reread on the next poll, but a line
# that keeps growing forever with no newline (a stuck or malformed writer)
# must not be reread and rechecked without bound.
_MAX_PARTIAL_LINE_BYTES = 1_048_576

# docs/DESIGN.md 4.12 (release finding F10): a KILL/FAILED/OK event suppressed
# by the rate limit, or (KILL only) by a transcript that does not exist
# yet, is retried on the next poll rather than dropped -- but only for this
# many polls, after which it is force-emitted (KILL, with a degraded
# message) or emitted as-is (FAILED, OK) and marked handled either way, so a
# transcript that never appears can never wedge the loop forever.
_MAX_PENDING_POLLS = 12


class JournalLineTooLong(Exception):
    """docs/DESIGN.md 4.12 (release finding F9): the journal's unterminated
    tail (from the last read offset to end of file, with no newline yet)
    has grown past _MAX_PARTIAL_LINE_BYTES."""

# Every event word is padded to this width so the message text lines up the
# same way across KILL/FAILED/QUIET/DONE/LIMIT (and so KILL's own second,
# indented line lines up under it): "KILL".ljust(7) == "KILL   ", "FAILED"
# ljust(7) == "FAILED ", and so on.
_LABEL_WIDTH = 7


def _now() -> float:
    """Epoch seconds (docs/DESIGN.md 4.12): the same domain as a file's own
    mtime, so QUIET's 'now - mtime' needs no unit conversion. A test
    monkeypatches this to a controllable float instead of the real clock."""
    return time.time()


def _sleep(seconds: float) -> None:
    """A test monkeypatches this to advance a fake clock (and grow the
    journal/transcripts a poll is about to see) instead of actually
    sleeping."""
    time.sleep(seconds)


# --- run resolution -------------------------------------------------------


def find_run_dir(run_id_or_path: str, root: Path) -> Path | None:
    """A single matching workflow run directory, or None when nothing (yet)
    matches -- never raises, since the caller's job is to keep polling until
    the 60s journal-wait budget runs out (docs/DESIGN.md 4.12). Only
    workflow runs are considered: a session carries no journal.jsonl, so
    `watch` has nothing to follow for one."""
    direct = Path(run_id_or_path)
    if direct.is_dir():
        return direct
    try:
        candidates = [
            ref
            for ref in reader.discover_runs(root, kinds=frozenset({"workflow"}))
            if ref.run_id.startswith(run_id_or_path)
        ]
    except OSError:
        return None
    if len(candidates) == 1:
        return candidates[0].path
    return None


def _wait_for_journal(
    run_id_or_path: str, root: Path, start: float, interval: float, err,
) -> Path | None:
    while True:
        run_dir = find_run_dir(run_id_or_path, root)
        if run_dir is not None and (run_dir / "journal.jsonl").exists():
            return run_dir
        remaining = _JOURNAL_WAIT_SECONDS - (_now() - start)
        if remaining <= 0:
            print(
                "no journal appeared within 60 s: watch follows a Workflow run while it is "
                "running (the plugin hands you this command when one starts); "
                "agent-postmortem scan lists past runs",
                file=err,
            )
            return None
        # docs/DESIGN.md 4.12 (release finding F11): capped to whatever is
        # left of the 60s budget, so a large --interval cannot overshoot it
        # (--interval 300 must still give up within 60s, not 300s).
        _sleep(min(interval, remaining))


# --- appended-bytes journal reading ----------------------------------------


def _read_new_journal_lines(path: Path, offset: int) -> tuple[list[str], int, bool]:
    """The complete non-blank lines appended to path since `offset` bytes,
    the new offset, and whether the file was found truncated or replaced
    (release finding F8). A file smaller than `offset` has been truncated
    or replaced (a fresh run reusing the same path): read from 0 instead of
    raising or silently missing its early lines, and report `truncated`
    True so the caller can reset every piece of state it derived from the
    old content before processing the replacement. A trailing line with no
    newline yet (a write still in flight) is withheld -- the offset does not
    advance past it -- so the next call re-reads it once it is complete,
    unless it has already grown past _MAX_PARTIAL_LINE_BYTES with still no
    newline, which raises JournalLineTooLong (release finding F9) instead
    of being reread and rechecked in full, without bound, forever."""
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset, False
    truncated = size < offset
    if truncated:
        offset = 0
    if size <= offset:
        return [], offset, truncated
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
    last_newline = chunk.rfind(b"\n")
    # The bytes after the last newline (the whole chunk when there is none
    # at all) are the as-yet-unterminated next line -- checked regardless
    # of how many complete lines share this same read.
    if len(chunk) - (last_newline + 1) > _MAX_PARTIAL_LINE_BYTES:
        raise JournalLineTooLong()
    if last_newline == -1:
        return [], offset, truncated
    usable = chunk[: last_newline + 1]
    new_offset = offset + len(usable)
    text = usable.decode("utf-8", errors="replace")
    lines = [line for line in text.split("\n") if line.strip()]
    return lines, new_offset, truncated


# --- transcript helpers: full, one-shot parses, never polled ---------------


class _EventsHolder:
    """The minimal duck-typed shape detectors.base.meaningful_event_before
    needs (attempt.events) -- built directly from a one-shot
    reader._read_transcript parse, never a full reader.Attempt."""

    __slots__ = ("events",)

    def __init__(self, events: list) -> None:
        self.events = events


def _transcript_path(run_dir: Path, agent_id: str) -> Path:
    return run_dir / f"agent-{agent_id}.jsonl"


def _last_user_index(events: list) -> int | None:
    last = None
    for index, event in enumerate(events):
        if event.kind == "user":
            last = index
    return last


def _parse_kill(path: Path) -> tuple[bool, float | None, int, int, bool, int]:
    """(is_kill, silence_seconds, paths_written, commits, paths_partial, commits_without_id) for
    one attempt's transcript, from a single full parse (`paths_partial`, N8 fix 1 S-m5: the
    attempt's ledger is partial, so `paths_written` is a lower bound; `commits_without_id`,
    G4b: completed calls with a commit command that reported no id): is_kill is whether its
    last user event is the kill sentinel; silence_seconds is computed the same way the
    interrupt machinery does (reader._build_interrupt): the gap from the
    last meaningful event before the sentinel (detectors.base.
    meaningful_event_before) to the sentinel itself, None when a timestamp
    is missing or the delta would be negative."""
    diagnostics: list[str] = []
    state = ledger.TranscriptState()
    events, _version, _session_id, _total, _bad, tool_calls, _marker_hits = reader._read_transcript(
        path, diagnostics, state=state,
    )
    kill_index = _last_user_index(events)
    if kill_index is None:
        return False, None, 0, 0, False, 0
    kill_event = events[kill_index]
    if not kill_event.text.startswith(KILL_SENTINEL_PREFIX):
        return False, None, 0, 0, False, 0

    preceding = meaningful_event_before(_EventsHolder(events), kill_index)
    silence: float | None = None
    if preceding is not None and preceding.ts is not None and kill_event.ts is not None:
        delta = (kill_event.ts - preceding.ts).total_seconds()
        if delta >= 0:
            silence = delta

    facts = reader._build_attempt_facts(tool_calls, "killed", kill_event.ts, path, False)
    paths = len(facts.paths_written) if facts is not None else 0
    commits = len(facts.commits) if facts is not None else 0
    partial = ledger.build_ledger(tool_calls, path.name, state).completeness == "partial"
    without_id = report._commit_commands_without_id([SimpleNamespace(facts=facts)])
    return True, silence, paths, commits, partial, without_id


def _cause_phrase_for_failed(path: Path) -> tuple[str, str | None]:
    """(phrase, reset_time) from the same last-transcript-line rule
    detectors/attempt_failed.py uses (docs/DESIGN.md 4.3, 4.13.2): 'cause not
    recorded' when the last entry carries none of the four runtime-produced
    markers attempt_failed itself recognizes."""
    diagnostics: list[str] = []
    events, _version, _session_id, _total, _bad, _tool_calls, marker_hits = (
        reader._read_transcript(path, diagnostics)
    )
    last_event = final_event(events)
    if last_event is None:
        return "cause not recorded", None
    # _cause_from_last_event only reads attempt.marker_hits; a bare
    # namespace stands in for the real Attempt this one-off transcript was
    # never otherwise built into.
    pseudo_attempt = SimpleNamespace(marker_hits=marker_hits)
    if last_event.kind == "user" and _attempt_failed._anchored_on_event(
        pseudo_attempt, "kill_sentinel", last_event
    ):
        return "interrupted before finishing", None
    if last_event.kind == "assistant":
        matched = _attempt_failed._cause_from_last_event(pseudo_attempt, last_event)
        if matched is not None:
            _cause, phrase, reset_time = matched
            return phrase, reset_time
    return "cause not recorded", None


# --- line formatting --------------------------------------------------


def _line(word: str, rest: str) -> str:
    return f"{word:<{_LABEL_WIDTH}}{rest}"


def _rate_limited(key: str, now: float, last_print: dict[str, float]) -> bool:
    last = last_print.get(key)
    return last is not None and (now - last) < _RATE_LIMIT_SECONDS


@dataclass
class _PendingKill:
    """A detected supersede transition not yet resolved to a printed KILL
    line (release finding F10): retried on every poll until it either
    prints, is conclusively decided not to be a kill, or hits
    _MAX_PENDING_POLLS."""

    previous: object
    current: object
    polls: int = 0


@dataclass
class _PendingFailed:
    """A journal 'failed' occurrence not yet resolved to a printed FAILED
    line (release finding F10), retried the same way as _PendingKill."""

    occurrence: object
    polls: int = 0


@dataclass
class _PendingOk:
    """A journal 'result' occurrence not yet resolved to a printed OK line
    (docs/DESIGN.md 4.12, N8b K5.2), retried the same way as _PendingFailed."""

    occurrence: object
    polls: int = 0


# The lifecycle terminals that make DONE eligible at once (docs/DESIGN.md 4.12): the
# terminals a parent notification, a TaskStop, or a synchronous launch result can give.
_IMMEDIATE_TERMINALS = frozenset({"completed", "failed", "stopped"})


def _parent_signature(path: Path) -> tuple | None:
    """(inode, size, mtime_ns, ctime_ns) of the parent transcript, None when it cannot be
    statted. The parent index is refreshed only when this changes (docs/DESIGN.md 4.12)."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


# --- agent names (N3b P14, C-1, C-5, C-6) -----------------------------------

WATCH_DIAGNOSTIC = f"agent-postmortem watch: {disclosure.UNAVAILABLE_DIAGNOSTIC}"


class _AgentNames:
    """The agent named on a watch line or in an event's `agent` field is the
    event attempt's own stable alias `a:<alias>` (P14), the one `run` shows
    for the same attempt. The agents known at start register in one call; an
    agent first named by a later event registers then (one locked
    registration per new agent, the book's slow path). If a registration
    fails the book degrades: that event and every later one name agents
    positionally (`agent-N`, numbered within this watch) while the lines
    already printed stand (C-1). The diagnostic goes to stderr once, as the
    one fixed line, in text and --json mode alike (C-6); stdout stays clean.

    An attempt whose agentId is missing, null or not a canonical id (C-5) is
    named by a positional `agent-N`, never by its label."""

    def __init__(self, err) -> None:
        self._err = err
        self._diagnosed = False
        self._book: disclosure.AliasBook | None = None

    def _check(self) -> None:
        if self._book is not None and not self._book.available and not self._diagnosed:
            self._diagnosed = True
            print(WATCH_DIAGNOSTIC, file=self._err)

    def start(self, occurrences) -> None:
        known: list[tuple[str, str]] = []
        for occurrence in occurrences:
            agent_id = occurrence.agent_id
            if disclosure.is_canonical_id("agent", agent_id) and ("agent", agent_id) not in known:
                known.append(("agent", agent_id))
        self._book = disclosure.register(known) if known else disclosure.lazy_book()
        self._check()

    def started(self) -> bool:
        return self._book is not None

    def name(self, occurrence) -> str:
        assert self._book is not None
        agent_id = occurrence.agent_id
        if disclosure.is_canonical_id("agent", agent_id):
            alias = self._book.alias("agent", agent_id)
        else:
            # Not canonical: never an alias and never the label. The book
            # numbers it positionally, in step with every other positional
            # name of this watch.
            alias = self._book.alias("agent", f"no-identity:{occurrence.key}:{occurrence.ordinal}")
        self._check()
        return disclosure.rendered_form("agent", alias)


# --- the poll loop ----------------------------------------------------


def watch_run(
    run_id_or_path: str,
    root: Path,
    *,
    interval: float = DEFAULT_INTERVAL,
    max_hours: float = DEFAULT_MAX_HOURS,
    quiet_after: float = DEFAULT_QUIET_AFTER,
    done_grace: float = DEFAULT_DONE_GRACE,
    json_mode: bool = False,
    out=None,
    err=None,
) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err

    start = _now()
    run_dir = _wait_for_journal(run_id_or_path, root, start, interval, err)
    if run_dir is None:
        return 2
    journal_path = run_dir / "journal.jsonl"
    # N3b C2: a directory name is printed as a run id only when it passes the
    # reader's validator; otherwise the replacement `run` itself uses.
    run_id = reader.valid_workflow_run_id(run_dir.name)

    names = _AgentNames(err)
    offset = 0
    full_journal: list[dict] = []
    last_print: dict[str, float] = {}
    handled_kill: set[tuple[str, int]] = set()
    handled_fail: set[tuple[str, int]] = set()
    pending_kill: dict[tuple[str, int], _PendingKill] = {}
    pending_fail: dict[tuple[str, int], _PendingFailed] = {}
    handled_ok: set[tuple[str, int]] = set()
    pending_ok: dict[tuple[str, int], _PendingOk] = {}
    quiet_threshold: dict[tuple[str, int], float] = {}
    resolved_since: float | None = None
    started_seen = 0
    next_line_no = 0
    malformed = False
    start_printed = False
    # docs/DESIGN.md 4.12 (N8b K5.4): the parent index and the stat signature it was last
    # cleanly refreshed at, the newest parent generation launch line known after the previous
    # poll, and the watermark a RESET leaves behind for the epoch it ended.
    parent_path = reader._parent_transcript_path_for_workflow_dir(run_dir)
    parent_index = reader.ParentIndex(None, [], [], []) if parent_path is None else None
    parent_signature: tuple | None = None
    known_newest_launch = 0
    epoch_watermark = 0

    while True:
        try:
            new_lines, offset, truncated = _read_new_journal_lines(journal_path, offset)
        except JournalLineTooLong:
            # docs/DESIGN.md 4.12 (release finding F9): an immediate, final
            # verdict -- not gated on every key being resolved, unlike the
            # malformed-complete-line case below. A first poll that ends here prints
            # no START: the journal was never parsed, so its counts are unknown and
            # the exit is final (docs/DESIGN.md 4.12).
            if json_mode:
                payload = {"event": "MALFORMED", "reason": "line longer than 1 MB"}
                print(json.dumps(payload), file=out)
            else:
                print("MALFORMED journal line longer than 1 MB", file=out)
            return 4

        if truncated:
            # docs/DESIGN.md 4.12 (release finding F8): the journal shrank
            # (truncated or replaced by a fresh run reusing the same path)
            # -- every piece of state derived from the old content is reset
            # before the replacement content is processed at all, so no
            # event from the old journal can combine with the new one into
            # a false KILL or DONE.
            full_journal = []
            last_print = {}
            handled_kill = set()
            handled_fail = set()
            pending_kill = {}
            pending_fail = {}
            handled_ok = set()
            pending_ok = {}
            quiet_threshold = {}
            resolved_since = None
            started_seen = 0
            next_line_no = 0
            malformed = False
            # K5.4: a new journal epoch. Generations launched up to the newest line known
            # before this reset belong to the ended epoch and cannot finish the replacement.
            epoch_watermark = known_newest_launch
            if json_mode:
                print(json.dumps({"event": "RESET"}), file=out)
            else:
                print(_line("RESET", "journal was truncated or replaced; state cleared."), file=out)

        malformed_notice: int | None = None
        for raw_line in new_lines:
            next_line_no += 1
            try:
                entry = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError, RecursionError):
                entry = None
            if not isinstance(entry, dict):
                # docs/DESIGN.md 4.12 (release finding F9): a complete line
                # that is not a JSON object is malformed evidence -- printed
                # once (after START, K5.1), and DONE can never be printed
                # again for this run.
                if not malformed:
                    malformed = True
                    malformed_notice = next_line_no
                continue
            full_journal.append(entry)

        occurrences, _diagnostics = reader._run_journal_state_machine(full_journal)
        if not names.started():
            names.start(occurrences)

        # docs/DESIGN.md 4.12 (K5.1): START is the first byte of output, once, ahead of the
        # MALFORMED line this very poll parsed and ahead of every other event.
        if not start_printed:
            _emit_start(run_id, occurrences, json_mode, out)
            start_printed = True
        if malformed_notice is not None:
            if json_mode:
                payload = {"event": "MALFORMED", "line": malformed_notice, "reason": "not JSON"}
                print(json.dumps(payload), file=out)
            else:
                print(f"MALFORMED journal line {malformed_notice} is not JSON", file=out)

        now = _now()

        # KILL: a second (or later) 'started' occurrence for a key, whose
        # immediately preceding occurrence's own transcript ends in the kill
        # sentinel (docs/DESIGN.md 4.12). Newly detected transitions join
        # pending_kill rather than being resolved inline (release finding
        # F10): an event is marked handled only once it is actually
        # emitted, decided not to be a kill, or forced past the poll cap.
        previous_by_key: dict[str, object] = {}
        for occurrence in occurrences:
            previous = previous_by_key.get(occurrence.key)
            if previous is not None and occurrence.ordinal == previous.ordinal + 1:
                handle = (occurrence.key, previous.ordinal)
                if handle not in handled_kill and handle not in pending_kill:
                    pending_kill[handle] = _PendingKill(previous=previous, current=occurrence)
            previous_by_key[occurrence.key] = occurrence

        for handle in list(pending_kill.keys()):
            pending = pending_kill[handle]
            status = _try_emit_kill(
                run_dir, run_id, names, pending.previous, pending.current, now, last_print,
                json_mode, out,
            )
            if status == "retry":
                pending.polls += 1
                if pending.polls < _MAX_PENDING_POLLS:
                    continue
                _emit_kill_fallback(
                    run_id, names, pending.previous, pending.current, json_mode, out,
                )
            handled_kill.add(handle)
            del pending_kill[handle]

        # FAILED: a journal 'failed' event. Same handled-only-after-
        # emission, bounded-retry treatment as KILL above (release finding
        # F10).
        for occurrence in occurrences:
            if occurrence.closed_by != "failed":
                continue
            handle = (occurrence.key, occurrence.ordinal)
            if handle not in handled_fail and handle not in pending_fail:
                pending_fail[handle] = _PendingFailed(occurrence=occurrence)

        for handle in list(pending_fail.keys()):
            pending = pending_fail[handle]
            status = _try_emit_failed(
                run_dir, names, pending.occurrence, now, last_print, json_mode, out,
            )
            if status == "retry":
                pending.polls += 1
                if pending.polls < _MAX_PENDING_POLLS:
                    continue
                _force_emit_failed(
                    run_dir, names, pending.occurrence, now, last_print, json_mode, out,
                )
            handled_fail.add(handle)
            del pending_fail[handle]

        # OK: a journal 'result' closed an attempt. Same handled-only-after-emission,
        # bounded-retry treatment as FAILED (docs/DESIGN.md 4.12, N8b K5.2).
        for occurrence in occurrences:
            if occurrence.closed_by != "completed":
                continue
            handle = (occurrence.key, occurrence.ordinal)
            if handle not in handled_ok and handle not in pending_ok:
                pending_ok[handle] = _PendingOk(occurrence=occurrence)

        for handle in list(pending_ok.keys()):
            pending = pending_ok[handle]
            status = _try_emit_ok(names, pending.occurrence, now, last_print, json_mode, out)
            if status == "retry":
                pending.polls += 1
                if pending.polls < _MAX_PENDING_POLLS:
                    continue
                _force_emit_ok(names, pending.occurrence, now, last_print, json_mode, out)
            handled_ok.add(handle)
            del pending_ok[handle]

        # DONE (docs/DESIGN.md 4.12, N8b K5.4). The parent index is refreshed only when the
        # parent transcript's stat signature changed since the last clean refresh, and the
        # signature is not remembered while the index still owes a full recheck (a transient
        # read failure is retried on the next poll though the file did not change). The
        # lifecycle table is evaluated from this poll's own occurrences, never from a second
        # journal read.
        if parent_path is not None:
            signature = _parent_signature(parent_path)
            if (
                parent_index is None or signature != parent_signature
                or parent_index.pending_full_recheck
            ):
                parent_index = reader.ParentIndex.for_path(parent_path)
                parent_signature = None if parent_index.pending_full_recheck else signature
        journal_resolved = reader.journal_resolved_from_occurrences(occurrences)
        lifecycle = reader.lifecycle_from_journal_state(
            run_dir, run_id, journal_resolved, parent_index, after_launch_line=epoch_watermark,
        )
        known_newest_launch = max(
            (generation.launch_line for generation in parent_index.generations_for(run_id)),
            default=0,
        )

        # Eligible at once from terminal evidence the journal did not give, or after the grace
        # period once every key is resolved (the gap between two sequential phases is not the
        # end of the run). A malformed journal (release finding F9) makes DONE impossible:
        # LIMIT is printed instead, at the point DONE would have been. DONE waits for every
        # already-detected KILL, FAILED, and OK transition to be emitted or forced.
        if len(occurrences) != started_seen:
            started_seen = len(occurrences)
            resolved_since = None
        if journal_resolved:
            if resolved_since is None:
                resolved_since = now
        else:
            resolved_since = None
        eligible = lifecycle.terminal in _IMMEDIATE_TERMINALS or (
            lifecycle.terminal == "journal_resolved" and resolved_since is not None
            and now - resolved_since >= done_grace
        )
        if eligible and not (pending_kill or pending_fail or pending_ok):
            if malformed:
                return _emit_malformed_limit(json_mode, out)
            return _emit_done(run_dir, json_mode, out)

        # QUIET: the currently open attempt of every key, if any.
        last_by_key: dict[str, object] = {}
        for occurrence in occurrences:
            last_by_key[occurrence.key] = occurrence
        for key, occurrence in last_by_key.items():
            if occurrence.closed_by is not None:
                continue
            _maybe_emit_quiet(
                run_dir, names, key, occurrence, now, last_print, quiet_threshold,
                quiet_after, json_mode, out,
            )

        if now - start >= max_hours * 3600:
            _emit_limit(max_hours, json_mode, out)
            return 4

        # docs/DESIGN.md 4.12 (release finding F11): capped to whatever is
        # left of the --max-hours deadline, so a large --interval cannot
        # overshoot it either.
        remaining = max_hours * 3600 - (now - start)
        _sleep(min(interval, remaining))


def _paths_words(paths: int, partial: bool) -> str:
    return f"{paths}+ paths" if partial else f"{paths} {report._plural(paths, 'path')}"


def _commit_words(commits: int, without_id: int) -> str:
    """docs/DESIGN.md 4.12 (G4b): `reported <c> commit(s)`, `reported <c>+ commits`, or
    `commit count unknown`, by the COMMITS cell's rule (4.7)."""
    words = report._commits_words(commits, without_id)
    return words if without_id > 0 and not commits else f"reported {words}"


def _emit_kill_line(
    run_id: str, names: _AgentNames, previous, current, silence: float | None, paths: int,
    commits: int, json_mode: bool, out, *, paths_partial: bool = False,
    commits_without_id: int = 0,
) -> None:
    agent = names.name(previous)
    silence_int = None if silence is None else round(silence)
    silence_str = "unknown" if silence_int is None else str(silence_int)

    if json_mode:
        payload = {
            "event": "KILL",
            "agent": agent,
            "attempt": previous.ordinal,
            "silence_seconds": silence_int,
            "restarted_as": current.ordinal,
            "paths_written": paths,
            "commits": commits,
            "run_id": run_id,
        }
        print(json.dumps(payload), file=out)
    else:
        print(
            _line(
                "KILL",
                f"{agent} attempt {previous.ordinal} after {silence_str}s of silence; "
                f"restarted as attempt {current.ordinal}.",
            ),
            file=out,
        )
        print(
            _line(
                "",
                f"attempt {previous.ordinal} wrote {_paths_words(paths, paths_partial)}, "
                f"{_commit_words(commits, commits_without_id)}.  "
                f"-> agent-postmortem rescue plan {run_id}",
            ),
            file=out,
        )


def _try_emit_kill(
    run_dir: Path, run_id: str, names: _AgentNames, previous, current, now: float,
    last_print: dict[str, float], json_mode: bool, out,
) -> str:
    """Attempt to resolve one supersede transition to a printed KILL line
    (release finding F10). Returns 'emitted' (printed, mark handled),
    'not_a_kill' (conclusively decided -- the earlier transcript exists and
    does not end in the kill sentinel -- mark handled, never print, never
    retry again), or 'retry' (suppressed by the rate limit, or the earlier
    transcript does not exist yet -- try again next poll, up to
    _MAX_PENDING_POLLS)."""
    if not previous.agent_id:
        return "not_a_kill"
    transcript_path = _transcript_path(run_dir, previous.agent_id)
    if not transcript_path.exists():
        return "retry"
    is_kill, silence, paths, commits, partial, without_id = _parse_kill(transcript_path)
    if not is_kill:
        return "not_a_kill"
    if _rate_limited(previous.key, now, last_print):
        return "retry"

    _emit_kill_line(
        run_id, names, previous, current, silence, paths, commits, json_mode, out,
        paths_partial=partial, commits_without_id=without_id,
    )
    last_print[previous.key] = now
    return "emitted"


def _emit_kill_fallback(
    run_id: str, names: _AgentNames, previous, current, json_mode: bool, out,
) -> None:
    """docs/DESIGN.md 4.12 (release finding F10): previous.polls reached
    _MAX_PENDING_POLLS with the transition still unresolved (the earlier
    transcript never appeared, or the rate limit never cleared) -- printed
    anyway, without the transcript-derived details, so a killed attempt can
    never be silently dropped forever."""
    agent = names.name(previous)
    if json_mode:
        payload = {
            "event": "KILL",
            "agent": agent,
            "attempt": previous.ordinal,
            "silence_seconds": None,
            "restarted_as": current.ordinal,
            "paths_written": None,
            "commits": None,
            "run_id": run_id,
            "details_unavailable": True,
        }
        print(json.dumps(payload), file=out)
    else:
        print(
            _line(
                "KILL",
                f"{agent} attempt {previous.ordinal} (details unavailable); "
                f"restarted as attempt {current.ordinal}.",
            ),
            file=out,
        )


def _compute_and_emit_failed(
    run_dir: Path, names: _AgentNames, occurrence, json_mode: bool, out,
) -> None:
    phrase, reset_time = "cause not recorded", None
    if occurrence.agent_id:
        transcript_path = _transcript_path(run_dir, occurrence.agent_id)
        if transcript_path.exists():
            phrase, reset_time = _cause_phrase_for_failed(transcript_path)

    agent = names.name(occurrence)
    if json_mode:
        payload = {
            "event": "FAILED", "agent": agent, "attempt": occurrence.ordinal, "cause": phrase,
            "reset_time": reset_time,
        }
        print(json.dumps(payload), file=out)
    else:
        print(_line("FAILED", f"{agent} attempt {occurrence.ordinal}: {phrase}."), file=out)


def _try_emit_failed(
    run_dir: Path, names: _AgentNames, occurrence, now: float, last_print: dict[str, float],
    json_mode: bool, out,
) -> str:
    """docs/DESIGN.md 4.12 (release finding F10): 'emitted' or 'retry'
    (suppressed by the rate limit only -- a missing transcript already
    degrades gracefully to 'cause not recorded' rather than blocking
    emission, so it is never a reason to retry)."""
    if _rate_limited(occurrence.key, now, last_print):
        return "retry"
    _compute_and_emit_failed(run_dir, names, occurrence, json_mode, out)
    last_print[occurrence.key] = now
    return "emitted"


def _force_emit_failed(
    run_dir: Path, names: _AgentNames, occurrence, now: float, last_print: dict[str, float],
    json_mode: bool, out,
) -> None:
    """release finding F10: occurrence.polls reached _MAX_PENDING_POLLS
    while only ever suppressed by the rate limit -- printed anyway, bypassing
    the limit, so a FAILED event can never be dropped forever either."""
    _compute_and_emit_failed(run_dir, names, occurrence, json_mode, out)
    last_print[occurrence.key] = now


def _emit_start(run_id: str, occurrences: list, json_mode: bool, out) -> None:
    """docs/DESIGN.md 4.12 (N8b K5.1): once, first. `agents` is the number of distinct journal
    keys started so far and `attempts` the number of started occurrences."""
    agents = len({occurrence.key for occurrence in occurrences})
    attempts = len(occurrences)
    if json_mode:
        payload = {"event": "START", "run": run_id, "agents": agents, "attempts": attempts}
        print(json.dumps(payload), file=out)
    else:
        print(
            _line("START", f"{run_id}: {agents} agents started so far, {attempts} attempts."),
            file=out,
        )


def _emit_ok_line(names: _AgentNames, occurrence, json_mode: bool, out) -> None:
    agent = names.name(occurrence)
    if json_mode:
        payload = {"event": "OK", "agent": agent, "attempt": occurrence.ordinal}
        print(json.dumps(payload), file=out)
    else:
        print(_line("OK", f"{agent} attempt {occurrence.ordinal} returned a result."), file=out)


def _try_emit_ok(
    names: _AgentNames, occurrence, now: float, last_print: dict[str, float], json_mode: bool,
    out,
) -> str:
    """docs/DESIGN.md 4.12 (N8b K5.2): 'emitted' or 'retry' (suppressed by the per-agent rate
    limit, the only reason an OK waits)."""
    if _rate_limited(occurrence.key, now, last_print):
        return "retry"
    _emit_ok_line(names, occurrence, json_mode, out)
    last_print[occurrence.key] = now
    return "emitted"


def _force_emit_ok(
    names: _AgentNames, occurrence, now: float, last_print: dict[str, float], json_mode: bool,
    out,
) -> None:
    """The bounded retry ran out: printed through the rate limit, so an OK is never dropped."""
    _emit_ok_line(names, occurrence, json_mode, out)
    last_print[occurrence.key] = now


def _emit_malformed_limit(json_mode: bool, out) -> int:
    """docs/DESIGN.md 4.12 (release finding F9): printed at the exact point
    DONE would otherwise have been printed -- every key is resolved -- but a
    malformed journal line means the run's own final state can no longer be
    trusted, so this replaces DONE and exits 4 instead of 0/3."""
    if json_mode:
        print(json.dumps({"event": "LIMIT", "reason": "malformed"}), file=out)
    else:
        print(_line("LIMIT", "journal has malformed lines; run state unknown."), file=out)
    return 4


def _maybe_emit_quiet(
    run_dir: Path, names: _AgentNames, key: str, occurrence, now: float,
    last_print: dict[str, float], quiet_threshold: dict[tuple[str, int], float],
    quiet_after: float, json_mode: bool, out,
) -> None:
    if not occurrence.agent_id:
        return
    transcript_path = _transcript_path(run_dir, occurrence.agent_id)
    try:
        mtime = transcript_path.stat().st_mtime
    except OSError:
        return
    silence = now - mtime
    if silence < 0:
        return
    threshold_key = (occurrence.key, occurrence.ordinal)
    threshold = quiet_threshold.get(threshold_key, quiet_after)
    if silence < threshold:
        return
    if _rate_limited(key, now, last_print):
        return

    agent = names.name(occurrence)
    silence_int = round(silence)
    if json_mode:
        payload = {
            "event": "QUIET", "agent": agent, "attempt": occurrence.ordinal,
            "silence_seconds": silence_int,
        }
        print(json.dumps(payload), file=out)
    else:
        rest = f"{agent} attempt {occurrence.ordinal} has produced nothing for {silence_int}s."
        print(_line("QUIET", rest), file=out)
    last_print[key] = now
    quiet_threshold[threshold_key] = threshold * 2


def _emit_done(run_dir: Path, json_mode: bool, out) -> int:
    run = reader.load_run(run_dir)
    findings = detectors.run_detectors(run)
    verdict = report.verdict_of(run, findings)
    agents = len(run.agents)
    attempts = len(run.attempts)

    if json_mode:
        payload = {
            "event": "DONE", "agents": agents, "attempts": attempts, "verdict": verdict,
            "run_id": run.run_id, "next_command": f"agent-postmortem run {run.run_id}",
        }
        print(json.dumps(payload), file=out)
    else:
        print(
            _line(
                "DONE",
                f"{agents} agents, {attempts} attempts, {verdict}.  "
                f"-> agent-postmortem run {run.run_id}",
            ),
            file=out,
        )
    # docs/DESIGN.md 4.12, 4.13.11 (N5c-2), ordered: a FAILED verdict is 3, ahead of a run
    # whose lifecycle terminal is `unknown`, which cannot be called a clean DONE and is 4.
    if verdict.startswith("FAILED"):
        return 3
    if run.lifecycle is not None and run.lifecycle.terminal == "unknown":
        return 4
    return 0


def _format_hours(max_hours: float) -> str:
    if float(max_hours).is_integer():
        return str(int(max_hours))
    return str(max_hours)


def _emit_limit(max_hours: float, json_mode: bool, out) -> None:
    if json_mode:
        print(json.dumps({"event": "LIMIT", "hours": max_hours}), file=out)
    else:
        print(_line("LIMIT", f"{_format_hours(max_hours)}h reached; run not finished."), file=out)
