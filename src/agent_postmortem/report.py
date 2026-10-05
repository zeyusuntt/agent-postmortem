"""Render a Run and its Findings as text or JSON.

Verdict-first, small functions. render_bug_report drafts an upstream bug
report from the same Run and Findings render_run_text uses, in the section
order docs/DESIGN.md section 4.4 specifies: Title, Environment, What
happened, Reproduction, Evidence, Suggested fixes.
"""

from __future__ import annotations

import hashlib
import platform
import re
import shlex
import textwrap
from datetime import datetime, timedelta, timezone

from . import commands, disclosure, ledger, reader, severity
from .detectors.base import attempt_seconds, format_duration, wasted_attempts
from .models import Attempt, Finding, Run, TitleParts, raw_diagnostic
from .redact import Pseudonyms, Redactor
from .rules import RULES, rules_for

_SEVERITIES = ("critical", "major", "minor", "info")

# docs/DESIGN.md 4.7 (unit M3a): the age computed for the header's "ended ...
# ago" clause, and every "within 72 hours" question WORK AT RISK/WHAT TO DO
# ask, use this instead of datetime.now(...) directly, the same pattern
# detectors/claim_contradiction.py already uses -- a test monkeypatches
# report._now to pin the answer instead of racing the real clock.
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """value, given a UTC tzinfo if it somehow has none (defensive: every
    real reader-produced timestamp is already aware, but a hand-built test
    Run is not required to bother)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _plural(count: int, word: str) -> str:
    return word if count == 1 else f"{word}s"


_STALE_AFTER = timedelta(hours=72)


def _ended_recently(run: Run, now: datetime) -> bool:
    """docs/DESIGN.md 4.7 (unit M3a): the same 72-hour rule observe.py's own
    staleness guard uses (4.9) -- a run with no known end time is treated as
    recent, since nothing says it is old."""
    if run.ended_at is None:
        return True
    return (now - _aware(run.ended_at)) <= _STALE_AFTER


def _ended_within_72h_strict(run: Run, now: datetime) -> bool:
    """unit M3b's NEEDS ATTENTION candidate rules 1(b)/1(c): unlike
    _ended_recently's "an unknown end time counts as recent" convention
    (used everywhere else a run's own report reasons about its own age), a
    candidate rule keyed off a real elapsed age must not fire just because
    the age happens to be unknown -- both rules spell out "ended_at not
    None" explicitly."""
    if run.ended_at is None:
        return False
    return (now - _aware(run.ended_at)) <= _STALE_AFTER

# A fixed sentence for a detector whose findings carry no rule_id, because
# the failure is a runtime defect rather than something a prompt can route
# around (docs/DESIGN.md section 4.3). Used by _bug_suggested_fixes_lines.
_DETECTOR_BUG_SENTENCES = {
    "tool_json_error": (
        "This is a runtime defect, not something a prompt can route around: the "
        "tool's own input failed to parse as JSON. File it upstream with the "
        "evidence below."
    ),
    "classifier_unavailable": (
        "The safety classifier the runtime relies on for Bash was temporarily "
        "unavailable. This is a transient runtime issue, not something the "
        "agent's prompt controls."
    ),
}


def verdict_word(
    findings: list[Finding], status: str = "COMPLETE", *, terminal_failed: bool = False,
) -> str:
    """FAILED (the run's own lifecycle terminal is `failed`, or any finding is
    critical), DEGRADED (any major, not FAILED), CLEAN (otherwise), always
    suffixed with ' (INCOMPLETE)' when status is INCOMPLETE (docs/DESIGN.md
    4.6, 4.13.4). A caller that has a Run passes `verdict_of` the run instead,
    so the lifecycle terminal can never be forgotten."""
    severities = {finding.severity for finding in findings}
    if terminal_failed or "critical" in severities:
        word = "FAILED"
    elif "major" in severities:
        word = "DEGRADED"
    else:
        word = "CLEAN"
    if status == "INCOMPLETE":
        word = f"{word} (INCOMPLETE)"
    return word


def lifecycle_failed(run: Run) -> bool:
    """The run's own lifecycle terminal (4.13.3) is `failed`: the harness itself
    reported the run failed. A session has no lifecycle, so never."""
    return run.lifecycle is not None and run.lifecycle.terminal == "failed"


def verdict_of(run: Run, findings: list[Finding]) -> str:
    """The verdict word of one run over the findings its caller shows."""
    return verdict_word(findings, run.status, terminal_failed=lifecycle_failed(run))


def is_failed(run: Run, findings: list[Finding]) -> bool:
    """Whether the verdict is FAILED: the condition behind exit code 3 (4.6)."""
    return verdict_of(run, findings).split(" ", 1)[0] == "FAILED"


def _attempt_cwd(attempt: Attempt) -> str | None:
    facts = attempt.facts
    if facts is not None and isinstance(facts.cwds, list):
        for cwd in facts.cwds:
            if isinstance(cwd, str) and cwd:
                return cwd
    return None


def _register_observation_paths(observation, redactor: Redactor, run: Run | None = None) -> None:
    """docs/DESIGN.md 4.9/5.1 (N5c-1): every repository, observed path, and
    unobserved path a RunObservation carries gets its own spelling in the one
    "path" namespace (path-N at share, the P19 answer at local), the same one a
    ledger entry's `path` and `repo` use, so one path has one display in one
    render. Called before either the REPO STATE block or the JSON export is
    built."""
    if observation is None:
        return
    for group in observation.groups:
        if group.repo is not None:
            redactor.names.path(group.repo)
        for path in group.paths:
            redactor.names.path(path)
        for path in group.unobserved_paths:
            redactor.names.path(path)


def _register_names(run: Run, redactor: Redactor, *, detailed: bool = False) -> None:
    """docs/DESIGN.md 5.1: every public renderer entry point builds a fresh
    Pseudonyms map for its own render (so numbering restarts at agent-1
    every time and two reports can never be linked by a repeated pseudonym),
    then registers every child attempt's label, meta description, phase,
    and agent_type before rendering anything. Doing the full pass up front
    -- not lazily, the first time each renderer happens to touch a
    particular attempt -- is what lets Finding.title (redactor.title())
    catch a name that only ever appears embedded in a title sentence, not
    as a structured field of its own. run.main is deliberately excluded:
    its label is the fixed literal 'main', never user text, and 5.3 already
    excludes it from export.

    Also registers every attempt's own report_path claim, if it has one
    (docs/DESIGN.md 5.1, M1 review finding 1): unlike the four name fields
    above, a registered path is substituted everywhere the plain Redactor
    renders text (Redactor.__call__ itself, not only title()), so this pass
    is what lets a report_path embedded in a title or evidence line -- not
    only the structured reported/metrics fields that call
    redactor.names.path() directly at export time -- still get the same
    path-N pseudonym in a text or bug report render, which never calls
    export."""
    redactor.fresh_names(detailed=detailed)
    for attempt in run.attempts:
        redactor.names.agent(attempt.label)
        meta = attempt.meta if isinstance(attempt.meta, dict) else {}
        description = meta.get("description")
        if isinstance(description, str):
            redactor.names.description(description, label=attempt.label)
        redactor.names.phase(attempt.phase)
        redactor.names.type(attempt.agent_type)
        reported = attempt.reported
        if isinstance(reported, dict):
            claims = reported.get("claims")
            if isinstance(claims, dict):
                report_path = claims.get("report_path")
                if isinstance(report_path, str) and report_path:
                    redactor.names.path(report_path, _attempt_cwd(attempt))


def _register_facts_paths(run: Run, redactor: Redactor) -> None:
    """docs/DESIGN.md 5.1/5.3 (F4 hardening): register every attempt's own
    facts.paths_written, facts.in_flight[].path, and facts.cwds through the
    same "path" namespace report_path and observation paths already share,
    so `_export_attempt`'s plain-Redactor pass (redact_value, same treatment
    as `reported`) substitutes a path-N pseudonym instead of leaving a
    repo-relative path, or an absolute path outside home/tmp, unchanged.

    Deliberately a separate function from `_register_names` above, called
    only by `export_run` (the one place these raw values are actually
    printed, in `run --json`'s facts export): `_register_names` is also
    called directly by rescue.py, which has its own, repo-relative
    "display_key" registration for the very same underlying files
    (`_register_plan_paths`/`_register_status_paths`) -- folding this in
    there too would burn path-N ordinals on the raw absolute form before
    rescue.py ever gets to register its own, preferred relative one, so the
    same file could end up with two different pseudonyms in one rescue
    render."""
    for attempt in run.attempts:
        facts = attempt.facts
        if facts is None:
            continue
        if isinstance(facts.paths_written, list):
            for facts_path in facts.paths_written:
                if isinstance(facts_path, str) and facts_path:
                    redactor.names.path(facts_path)
        if isinstance(facts.in_flight, list):
            for entry in facts.in_flight:
                if isinstance(entry, dict):
                    in_flight_path = entry.get("path")
                    if isinstance(in_flight_path, str) and in_flight_path:
                        redactor.names.path(in_flight_path)
        if isinstance(facts.cwds, list):
            for cwd in facts.cwds:
                if isinstance(cwd, str) and cwd:
                    redactor.names.path(cwd)


def _register_ledger_paths(run: Run, redactor: Redactor) -> None:
    """docs/DESIGN.md 5.3b: every ledger entry's own `path` and `repo` goes
    through the same "path" namespace as facts and observation paths, so one
    path has one display in one render. Called after `_register_facts_paths`,
    which leaves the facts numbering unchanged."""
    for attempt in run.attempts:
        ledger = attempt.ledger
        if ledger is None:
            continue
        for entry in ledger.entries:
            for value in (entry.path, entry.repo):
                if isinstance(value, str) and value:
                    redactor.names.path(value)


def redact_value(value, redactor: Redactor):
    """Recursively apply redactor to every string in a JSON-shaped value."""
    if isinstance(value, str):
        return redactor(value)
    if isinstance(value, list):
        return [redact_value(item, redactor) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item, redactor) for key, item in value.items()}
    return value


# --- N3b: identity collection and evidence pointers (P9, P10, P16) -----------
#
# A pointer is "<file>:<line>" or "<file>:<line>: <excerpt>", the format every
# detector writes (detectors/base.py evidence_line, plus the two pointer-only
# forms). It is parsed with one strict pattern: a bare file name (letters,
# digits, dot, underscore, hyphen; no directory part), a line number, an
# optional excerpt. At share the file name must name a canonical identity
# (agent-<17 hex>.jsonl or .meta.json, <session uuid>.jsonl or .meta.json) or
# be journal.jsonl; anything else renders a fixed placeholder, never its text.

_POINTER_RE = re.compile(
    r"(?P<file>[A-Za-z0-9._-]+):(?P<line>[0-9]+)(?:: (?P<excerpt>.*))?", re.DOTALL
)
_AGENT_FILE_RE = re.compile(r"agent-(?P<id>[0-9a-f]{17})(?P<ext>\.jsonl|\.meta\.json)")
_SESSION_FILE_RE = re.compile(
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?P<ext>\.jsonl|\.meta\.json)"
)
_JOURNAL_FILE = "journal.jsonl"

EXCERPT_NOTE = (
    "note: excerpts need --local or --raw; the default level prints pointers and "
    "marker codes only"
)


def parse_pointer(text: object) -> tuple[str, int, str | None] | None:
    """(file name, line, excerpt or None) of a well-formed evidence string,
    else None."""
    if not isinstance(text, str):
        return None
    match = _POINTER_RE.fullmatch(text)
    if match is None:
        return None
    return match.group("file"), int(match.group("line")), match.group("excerpt")


def _file_identity(name: str) -> tuple[str, str, str] | None:
    """(kind, real id, extension) when `name` is a canonical transcript or
    meta file name: kind "agent" or "session"."""
    match = _AGENT_FILE_RE.fullmatch(name)
    if match is not None and disclosure.is_canonical_id("agent", match.group("id")):
        return "agent", match.group("id"), match.group("ext")
    match = _SESSION_FILE_RE.fullmatch(name)
    if match is not None and disclosure.is_canonical_id("session", match.group("id")):
        return "session", match.group("id"), match.group("ext")
    return None


def _all_attempts(run: Run) -> list[Attempt]:
    attempts = [run.main] if run.main is not None else []
    attempts.extend(run.attempts)
    return attempts


def collect_identities(run: Run, findings: list[Finding] | None = None) -> list[tuple[str, str]]:
    """P9: every (kind, real id) a text render of this run may show, in one
    deterministic order: the project directory name, then session ids (the
    run's own, then those named by transcript or evidence file names), then
    agent ids (attempts in run order, then finding and evidence ids). Only
    canonical ids are returned (disclosure.register rejects the rest; an
    attempt without a canonical agent id is named positionally at render)."""
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []

    def add(kind: str, value: object) -> None:
        if isinstance(value, str) and disclosure.is_canonical_id(kind, value):
            key = (kind, value)
            if key not in seen:
                seen.add(key)
                ordered.append(key)

    add("project", run.project_slug)
    add("session", run.session_id)
    attempts = _all_attempts(run)
    names: list[str] = []
    for attempt in attempts:
        if attempt.transcript_path is not None:
            names.append(attempt.transcript_path.name)
    for finding in findings or []:
        for evidence in finding.evidence:
            parsed = parse_pointer(evidence)
            if parsed is not None:
                names.append(parsed[0])
    for name in names:
        identity = _file_identity(name)
        if identity is not None and identity[0] == "session":
            add("session", identity[1])
    for attempt in attempts:
        add("agent", attempt.agent_id)
    for finding in findings or []:
        add("agent", finding.agent_id)
    for name in names:
        identity = _file_identity(name)
        if identity is not None and identity[0] == "agent":
            add("agent", identity[1])
    return ordered


def collect_project_identities(slugs) -> list[tuple[str, str]]:
    """P9 for a sink that renders only project names (`scan --all`): each
    distinct canonical project directory name, in the order given."""
    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for slug in slugs:
        if slug in seen or not disclosure.is_canonical_id("project", slug):
            continue
        if isinstance(slug, str):
            seen.add(slug)
            ordered.append(("project", slug))
    return ordered


def project_text(slug: str, redactor: Redactor) -> str:
    """P10/P11, the project in any text sink: at share `project:<alias>`
    (positional on fallback); at local and raw the project directory name as
    it is on disk (the string `local` prints, 4.13.5)."""
    if not slug:
        return ""
    if redactor.level == "share":
        return redactor.identity_text("project", slug)
    if redactor.level == "local":
        return redactor.names.local_text(slug)
    return slug


def identity_value(kind: str, value: str | None, redactor: Redactor) -> str | None:
    """5.3b's `agent_id` (and the like) in JSON: at share the registry alias in
    its rendered form (`a:<alias>`; positional `agent-N` on fallback and for an
    id that is not canonical, never the real value), at local the real value
    with control characters and paths neutralized, at raw the value as is."""
    if value is None:
        return None
    if redactor.level == "share":
        return redactor.identity_text(kind, value)
    if redactor.level == "local":
        return redactor.names.local_text(value)
    return value


class EvidenceView:
    """How one render prints an evidence string (P10, P16).

    share: the pointer with an aliased file name plus the marker code when
    the line is a MarkerHit's, never an excerpt; a string that is not a
    pointer with a canonical file name renders evidence-N; a pointer into an
    attempt without a canonical agent id is omitted (None).
    local, raw: today's `file:line: excerpt` only when `show_excerpts`
    (--evidence); otherwise the pointer (real file name) plus the code.
    """

    def __init__(self, run: Run, redactor: Redactor, *, show_excerpts: bool) -> None:
        self.redactor = redactor
        self.level = redactor.level
        self.show_excerpts = show_excerpts and self.level != "share"
        self._placeholders: dict[str, str] = {}
        self._codes: dict[tuple[str, int], list[str]] = {}
        self._no_identity: set[str] = set()
        for attempt in _all_attempts(run):
            if attempt.transcript_path is None:
                continue
            name = attempt.transcript_path.name
            if attempt is not run.main and not disclosure.is_canonical_id(
                "agent", attempt.agent_id
            ):
                self._no_identity.add(name)
            for hit in attempt.marker_hits:
                if not hit.anchored or hit.in_payload:
                    continue
                for line in {hit.event_line, hit.result_line}:
                    if line is not None:
                        codes = self._codes.setdefault((name, line), [])
                        if hit.marker not in codes:
                            codes.append(hit.marker)

    def _placeholder(self, text: str) -> str:
        if text not in self._placeholders:
            self._placeholders[text] = f"evidence-{len(self._placeholders) + 1}"
        return self._placeholders[text]

    def _share_file(self, name: str) -> str | None:
        if name == _JOURNAL_FILE:
            return name
        identity = _file_identity(name)
        if identity is None:
            return None
        kind, real_id, extension = identity
        alias = self.redactor.book.alias(kind, real_id)
        return disclosure.file_stem(kind, alias) + extension

    def pointer(self, name: str, line: int) -> str | None:
        """A bare `file:line` pointer with no marker code (rescue's TASK
        line): the aliased file name at share, the real one at local and raw;
        None for an attempt without a canonical agent id at share."""
        if self.level != "share":
            return f"{name}:{line}"
        if name in self._no_identity:
            return None
        shown = self._share_file(name)
        if shown is None:
            return self._placeholder(f"{name}:{line}")
        return f"{shown}:{line}"

    def ledger_line(self, line: str) -> str:
        """A ledger entry's own `<transcript file name>:<line>` (docs/DESIGN.md
        5.3b's `line` convention): at share the file half is its aliased
        spelling, an `evidence-N` placeholder when the name is no canonical
        identity or the attempt has no canonical agent id (never a real name);
        at local and raw the real file name, with no marker code."""
        parsed = parse_pointer(line)
        if self.level != "share":
            if parsed is None:
                return self.redactor.names.local_text(line) if self.level == "local" else line
            return f"{parsed[0]}:{parsed[1]}"
        if parsed is None:
            return self._placeholder(line)
        name, number, _excerpt = parsed
        if name in self._no_identity:
            return self._placeholder(line)
        shown = self._share_file(name)
        if shown is None:
            return self._placeholder(line)
        return f"{shown}:{number}"

    def _code_suffix(self, name: str, line: int) -> str:
        codes = self._codes.get((name, line))
        return f" [{','.join(sorted(codes))}]" if codes else ""

    def line(self, raw: str) -> str | None:
        parsed = parse_pointer(raw)
        if self.level == "share":
            if parsed is None:
                return self._placeholder(raw)
            name, line, _excerpt = parsed
            if name in self._no_identity:
                return None
            shown = self._share_file(name)
            if shown is None:
                return self._placeholder(raw)
            return f"{shown}:{line}{self._code_suffix(name, line)}"
        if self.show_excerpts:
            if parsed is None:
                return self.redactor(raw)
            name, line, excerpt = parsed
            pointer = f"{name}:{line}"
            return pointer if excerpt is None else f"{pointer}: {self.redactor(excerpt)}"
        if parsed is None:
            return self._placeholder(raw)
        name, line, _excerpt = parsed
        return f"{name}:{line}{self._code_suffix(name, line)}"

    def note(self) -> str | None:
        """The one share-level note a --evidence request earns."""
        return EXCERPT_NOTE if self.level == "share" else None


def _shown_evidence(
    evidence: list[str], view: "EvidenceView | None", redactor: Redactor,
) -> list[str]:
    """The rendered evidence lines of one finding or story, in first order,
    each distinct line once at share and local (N3b S13): two entries that
    differed only by an excerpt the level does not print render identically
    and show once. raw keeps v0.2's count and order (R2-3). A line the view
    omits (None) is skipped."""
    dedupe = redactor.level != "raw"
    shown: list[str] = []
    for line in evidence:
        text = view.line(line) if view is not None else redactor(line)
        if text is not None and (not dedupe or text not in shown):
            shown.append(text)
    return shown


def _format_range(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "no timestamps recorded"
    # Convert to UTC first: a timestamp that kept a non-UTC offset would
    # otherwise print its local wall-clock value with a "Z" suffix that
    # falsely claims it is UTC.
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    start_str = start.strftime("%Y-%m-%d %H:%MZ")
    end_str = (
        end.strftime("%H:%MZ") if start.date() == end.date() else end.strftime("%Y-%m-%d %H:%MZ")
    )
    return f"{start_str} to {end_str}"


def silent_retry_count(run: Run) -> int:
    """How many attempts were thrown away by a restart: for every logical
    agent, every attempt but the last one. Attempts with no key (an unkeyed
    'started' journal event) are not part of any LogicalAgent and must never
    be counted here; subtracting len(run.attempts) - len(run.agents) would
    silently attribute one to an agent that was never actually retried."""
    return sum(len(agent.attempts) - 1 for agent in run.agents if agent.attempts)


def _summary_line(run: Run) -> str:
    agent_count = len(run.agents)
    attempt_count = len(run.attempts)
    retries = silent_retry_count(run)
    wasted_seconds = sum(
        attempt_seconds(attempt)
        for agent in run.agents
        for attempt in sorted(agent.attempts, key=lambda a: a.ordinal)[:-1]
    )
    killed = sum(1 for attempt in run.attempts if attempt.outcome == "killed")
    return (
        f"{agent_count} agents became {attempt_count} attempts; "
        f"{retries} silent retries burned {format_duration(wasted_seconds)}; "
        f"{killed} kills recorded as user interrupts"
    )


def _session_summary_line(run: Run) -> str:
    """docs/DESIGN.md 4.7: a session's summary reports subagents, not
    logical agents becoming attempts -- a session's subagents are never
    retried in a way v0.1 can observe, so it says so plainly instead of
    printing a 'silent retries' count that would always read zero."""
    run_seconds = 0.0
    if run.started_at is not None and run.ended_at is not None:
        run_seconds = max((run.ended_at - run.started_at).total_seconds(), 0.0)
    subagents = len(run.attempts)
    interrupted = sum(1 for attempt in run.attempts if attempt.interrupt is not None)
    unattributed = sum(
        1 for attempt in run.attempts
        if attempt.interrupt is not None and attempt.interrupt.cause == "unattributed"
    )
    return (
        f"session ran {format_duration(run_seconds)}; {subagents} subagents, "
        f"{interrupted} interrupted ({unattributed} unattributed); retries not observable"
    )


def _summary_line_for(run: Run) -> str:
    return _session_summary_line(run) if run.kind == "session" else _summary_line(run)


def summary_line(run: Run) -> str:
    """The one-line run summary docs/DESIGN.md 4.7 renders first inside
    render_run_text/render_bug_report: 'N agents became M attempts; ...' for
    a workflow run, 'session ran T; N subagents, ...' for a session. Public
    so a caller outside this module (hooks_cli.py's notification lines,
    docs/DESIGN.md 7.2) reuses the exact same sentence instead of
    re-deriving it."""
    return _summary_line_for(run)


def _project_line(run: Run, redactor: Redactor) -> str:
    project = redactor(run.project_slug)
    version = run.claude_version or "unknown"
    time_range = _format_range(run.started_at, run.ended_at)
    return f"project {project}   claude {version}   {time_range}"


# --- unit M3a: the redesigned `run` text report -----------------------------
#
# docs/DESIGN.md 4.7 (rewritten by this unit): header, account, WORK AT RISK,
# WHAT TO DO, AGENT table, STORIES. The old v0.1 header/summary-line/
# project-line/FINDINGS/SUGGESTED RULES/NEXT ACTION shape is gone from
# render_run_text; render_bug_report keeps the v0.1 section order untouched
# (it has its own _bug_* helpers and never calls anything below).

_AGENT_END_KILLED_CAUSE = {"parent_stop": "stopped by parent", "human": "interrupted"}


def _age_string(ended_at: datetime, now: datetime, short: bool = False) -> str:
    """'<N> days|hours|minutes ago', floor-rounded, for the header's 'ended
    ... ago' clause (docs/DESIGN.md 4.7). short=True (unit M3b's scan triage
    age column) abbreviates the unit ('<N>d/h/m ago') instead of spelling it
    out; the rounding and threshold logic is exactly the same either way, so
    a triage row's age and the run report's header age can never disagree
    about which unit a given duration falls into."""
    seconds = max((now - _aware(ended_at)).total_seconds(), 0.0)
    days = int(seconds // 86400)
    if days >= 1:
        return f"{days}d ago" if short else f"{days} {_plural(days, 'day')} ago"
    hours = int(seconds // 3600)
    if hours >= 1:
        return f"{hours}h ago" if short else f"{hours} {_plural(hours, 'hour')} ago"
    minutes = int(seconds // 60)
    return f"{minutes}m ago" if short else f"{minutes} {_plural(minutes, 'minute')} ago"


def _uncertain_terminal(run: Run) -> bool:
    """docs/DESIGN.md 4.13.3 (v0.3 N2): true when nothing says this run has
    actually stopped -- a session (no lifecycle model at all, D11) or a
    workflow whose lifecycle terminal is 'none' (no generation evidence, or
    a pending one, ever reached a terminal value). Every other workflow
    terminal (completed/failed/stopped/journal_resolved/unknown) is a real,
    if sometimes unclear, word from the harness, so 'ended' stays honest."""
    if run.kind == "session":
        return True
    return run.lifecycle is not None and run.lifecycle.terminal == "none"


def _activity_recent(run: Run, now: datetime) -> bool:
    """docs/DESIGN.md 4.13.3's activity rule, for the header's own 'possibly
    running' suffix: a workflow's own Lifecycle.activity (mtime-based, set
    once at load time) for a workflow; the same 10-minute freshness rule
    applied to the session's own last recorded event for a session (no
    Lifecycle to carry a precomputed activity field, D11)."""
    if run.kind == "workflow":
        return run.lifecycle is not None and run.lifecycle.activity == "recent"
    if run.ended_at is None:
        return False
    return reader.activity_word(_aware(run.ended_at).timestamp(), now.timestamp()) == "recent"


def _run_header_line(run: Run, findings: list[Finding], show_age: bool = True) -> str:
    """'RUN <id>  <VERDICT>[ (INCOMPLETE)][ (unfinished)]  ended <ts> (<age>)'
    -- 'SESSION' instead of 'RUN' for a session run (docs/DESIGN.md 4.7 rule
    1). ended_at is never redacted (it carries no user text, same as a run
    id). (v0.3, N2, 4.13.3) When nothing certifies this run has actually
    stopped (a session, or a workflow whose lifecycle terminal is 'none'),
    the clause reads 'last activity <ts> (<age>)' instead of 'ended ... ',
    plus a trailing 'possibly running' when activity is recent -- 'ended'
    would otherwise claim a certainty nothing here actually has. The
    '(unfinished)' tag is driven by Run.finished being exactly False (never
    None: a session's own null finished never earns the tag, D11)."""
    kind_word = "SESSION" if run.kind == "session" else "RUN"
    verdict = verdict_of(run, findings)
    unfinished_tag = " (unfinished)" if run.finished is False else ""
    uncertain = _uncertain_terminal(run)
    label = "last activity" if uncertain else "ended"
    if run.ended_at is None:
        clause = f"{label}: unknown"
    else:
        ended_utc = _aware(run.ended_at).astimezone(timezone.utc)
        clause = f"{label} {ended_utc.strftime('%Y-%m-%d %H:%MZ')}"
        if show_age:
            clause += f" ({_age_string(run.ended_at, _now())})"
    if uncertain and _activity_recent(run, _now()):
        clause += "  possibly running"
    return f"{kind_word} {run.run_id}  {verdict}{unfinished_tag}  {clause}"


# docs/DESIGN.md 4.2/4.7: the phrase each attempt_failed cause renders as,
# shared by the account's failed-sentence, WHAT TO DO's relaunch guidance,
# and a story's own attempt_failed clause -- one table instead of three
# copies of the same wording.
_CAUSE_PHRASES = {
    "usage_limit_session": "session usage limit reached",
    "usage_limit_weekly": "weekly usage limit reached",
    "machine_sleep": "the machine went to sleep",
    "api_error": "a server error occurred",
    "interrupted": "interrupted before finishing",
    "unattributed": "cause not recorded in the transcript",
}


def _cause_phrase(cause: str) -> str:
    return _CAUSE_PHRASES.get(cause, cause)


def _workflow_account_lines(run: Run, findings: list[Finding]) -> list[str]:
    """docs/DESIGN.md 4.7 rule 2: 1 to 3(+) plain sentences, verdict already
    said in the header -- this is the 'what happened' account. Any sentence
    whose count is 0 is omitted outright, never printed as '0 ...'."""
    agent_count = len(run.agents)
    attempt_count = len(run.attempts)
    sentences = [
        f"{agent_count} {_plural(agent_count, 'agent')} took "
        f"{attempt_count} {_plural(attempt_count, 'attempt')}."
    ]

    runtime = 0
    parent_stop = 0
    human = 0
    unattributed = 0
    for attempt in run.attempts:
        interrupt = attempt.interrupt
        if interrupt is None:
            continue
        if interrupt.cause == "runtime":
            runtime += 1
        elif interrupt.cause == "parent_stop":
            parent_stop += 1
        elif interrupt.cause == "human":
            human += 1
        elif interrupt.cause == "unattributed":
            unattributed += 1

    if runtime:
        was_were = "was" if runtime == 1 else "were"
        sentences.append(
            f"{runtime} {_plural(runtime, 'attempt')} {was_were} killed by "
            "the runtime and restarted."
        )
    if parent_stop:
        was_were = "was" if parent_stop == 1 else "were"
        sentences.append(f"{parent_stop} {was_were} stopped by the parent session.")
    if human:
        was_were = "was" if human == 1 else "were"
        sentences.append(f"{human} {was_were} interrupted with the parent session.")
    if unattributed:
        was_were = "was" if unattributed == 1 else "were"
        sentences.append(f"{unattributed} {was_were} interrupted, cause unattributed.")

    failed_sentence = _failed_clause(findings)
    if failed_sentence:
        sentences.append(failed_sentence)

    wasted_sentence = _wasted_clause(findings)
    if wasted_sentence:
        sentences.append(wasted_sentence)

    return sentences


def _wasted_clause(findings: list[Finding]) -> str | None:
    """docs/DESIGN.md 4.7 (FA5): '<W> was spent on retries and attempts that ended
    without a result.' from `wasted_time`'s own `wasted_seconds` (workflow and session
    alike), or None when it is 0 or there is no such finding."""
    wasted_seconds = 0.0
    for finding in findings:
        if finding.detector == "wasted_time":
            wasted_seconds = finding.metrics.get("wasted_seconds", 0.0) or 0.0
    if not wasted_seconds:
        return None
    return (
        f"{format_duration(wasted_seconds)} was spent on retries and attempts that ended "
        "without a result."
    )


def _failed_clause(findings: list[Finding]) -> str | None:
    """'<F> failed (<causes, comma separated, with counts>).' (docs/
    DESIGN.md 4.7 rule 2), or None when there are no attempt_failed findings
    at all -- true of every session run today (attempt_failed is workflow-
    only), so the session account line simply never grows this clause."""
    failed = [f for f in findings if f.detector == "attempt_failed"]
    if not failed:
        return None
    causes: list[list] = []
    index_by_cause: dict[str, int] = {}
    for finding in failed:
        cause = finding.metrics.get("cause", "unattributed")
        if cause not in index_by_cause:
            index_by_cause[cause] = len(causes)
            causes.append([cause, 0])
        causes[index_by_cause[cause]][1] += 1
    cause_text = ", ".join(f"{_cause_phrase(cause)} ({count})" for cause, count in causes)
    return f"{len(failed)} failed ({cause_text})."


def _session_account_lines(run: Run, findings: list[Finding]) -> list[str]:
    """docs/DESIGN.md 4.7 rule 2: the existing session summary sentence, plus
    the failed sentence when one applies (attempt_failed is workflow-only
    today, so this never actually fires yet -- kept so the two account
    builders share the same failed-clause code path)."""
    sentences = [f"{_session_summary_line(run)}."]
    failed_sentence = _failed_clause(findings)
    if failed_sentence:
        sentences.append(failed_sentence)
    wasted_sentence = _wasted_clause(findings)
    if wasted_sentence:
        sentences.append(wasted_sentence)
    return sentences


def _account_lines(run: Run, findings: list[Finding]) -> list[str]:
    if run.kind == "session":
        return _session_account_lines(run, findings)
    return _workflow_account_lines(run, findings)


# docs/DESIGN.md 4.9/4.7: the outcomes whose facts feed WORK AT RISK, the
# AGENT table's PATHS/COMMITS columns, and a story's own facts clause --
# the same set observe.py's own _CANDIDATE_OUTCOMES already uses.
_RISK_OUTCOMES = frozenset({"killed", "failed", "superseded"})


def _risk_paths_and_commits(run: Run) -> tuple[set[str], set[str]]:
    """The union of paths_written/commits over every killed, failed, or
    superseded attempt of the run (the same population _work_at_risk_lines
    and _what_to_do_items already walk) -- shared by unit M3b's scan triage
    (the NEEDS ATTENTION candidate rules, the paths_at_risk JSON field, and
    the --observe next-command decision) so there is exactly one definition
    of "paths at risk" in this module."""
    qualifying = [a for a in run.attempts if a.outcome in _RISK_OUTCOMES]
    paths: set[str] = set()
    commits: set[str] = set()
    for attempt in qualifying:
        facts = attempt.facts
        if facts is None:
            continue
        if isinstance(facts.paths_written, list):
            paths.update(facts.paths_written)
        if isinstance(facts.commits, list):
            commits.update(facts.commits)
    return paths, commits


_RUN_REASON_WORDS = {
    "discovery_incomplete": "some run files could not be read or matched",
}

# docs/DESIGN.md 4.7 (field milestone F, P6): why an attempt could not be placed in a launch.
_UNASSIGNABLE_REASON_WORDS = {
    "before_launch": "it started before the run was launched",
    "no_usable_timestamp": "an early line has no usable timestamp",
    "launch_times_unusable": "the launch times are missing or out of order",
    "no_launch_record": "no launch of this run was found in the parent transcript",
    "launch_unusable": "a launch of this run was found but could not be used",
}
_MAX_UNASSIGNABLE_NAMED = 3
_NOT_PLACED = "could not be placed in a launch of this run"

# Ledger reasons the per-attempt clauses explain by their own counts, and the
# run-level reasons the population line reports once (docs/DESIGN.md 4.7).
_EXPLAINED_REASONS = frozenset({
    "shell_context_unverified", "possible_opaque_command", "possible_delegation",
    "possible_background_process", "possible_unknown_tool", "unassignable_attempt",
    "discovery_incomplete",
})

_MAX_HINTS = 3


def _id_list(ids: list[str], more: int) -> str:
    """`a`, `a and b`, `a, b and c`, `a, b, c and 2 more`."""
    items = list(ids)
    if more > 0:
        items.append(f"{more} more")
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _unassignable_words(run: Run, redactor: Redactor) -> str:
    """P6: `<id> could not be placed in a launch of this run (<reason>)`. Attempts with the same
    reason share one phrase (`<id>, <id> and <id> could not ...`, at most three named per reason,
    in `Run.attempts` order, then `and <k> more`); phrases of different reasons join with "; "."""
    groups: dict[str, list[Attempt]] = {}
    for attempt in run.attempts:
        if ledger.is_unassignable(run, attempt):
            reason = ledger.unassignable_reason(run, attempt)
            groups.setdefault(reason or "before_launch", []).append(attempt)
    phrases = []
    for reason, attempts in groups.items():
        named = [
            _attempt_ident(attempt, run, redactor) for attempt in attempts[:_MAX_UNASSIGNABLE_NAMED]
        ]
        words = _UNASSIGNABLE_REASON_WORDS.get(reason, _UNASSIGNABLE_REASON_WORDS["before_launch"])
        phrases.append(
            f"{_id_list(named, len(attempts) - len(named))} {_NOT_PLACED} ({words})"
        )
    return "; ".join(phrases)


def _run_reason_words(run: Run, redactor: Redactor) -> str:
    reasons = list(getattr(run, "risk_population_completeness_reasons", []) or [])
    words = []
    for reason in reasons:
        if reason == "unassignable_attempt":
            text = _unassignable_words(run, redactor)
            if text:
                words.append(text)
        elif reason in _RUN_REASON_WORDS:
            words.append(_RUN_REASON_WORDS[reason])
    if not words:
        words = [_RUN_REASON_WORDS["discovery_incomplete"]]
    return "; ".join(words)


def _attempt_ident(attempt: Attempt, run: Run, redactor: Redactor) -> str:
    """The attempt's id in WORK AT RISK: `main` for a session main thread, else
    the agent id as the level shows it (a:<alias> at share)."""
    if attempt is run.main:
        return "main"
    return identity_value("agent", attempt.agent_id, redactor) or "agent"


def _attempt_clauses(attempt: Attempt) -> list[str]:
    """docs/DESIGN.md 4.7: what one population attempt wrote and what it may have
    written, from its ledger and its calls."""
    built = attempt.ledger
    entries = built.entries if built is not None else []

    def distinct(effect: str) -> int:
        return len({entry.path for entry in entries if entry.effect == effect and entry.path})

    clauses: list[str] = []
    wrote = distinct("observed")
    if wrote:
        clauses.append(f"wrote {wrote} {_plural(wrote, 'path')} (file tools)")
    named = distinct("claimed_target")
    if named:
        clauses.append(f"named {named} {_plural(named, 'path')} in write commands")
    tried = distinct("attempted")
    if tried:
        clauses.append(f"tried {tried} {_plural(tried, 'path')} with no confirmed result")
    shell = sum(1 for call in attempt.tool_calls if call.tool_name in ledger.SHELL_TOOLS)
    if shell:
        clauses.append(
            f"ran {shell} shell {_plural(shell, 'command')} that may have written more"
        )
    unknown_tools = sum(
        1 for entry in entries if entry.effect == "possible" and entry.basis == "unknown_tool"
    )
    if unknown_tools:
        clauses.append(
            f"called {unknown_tools} unrecognized {_plural(unknown_tools, 'tool')} "
            "that may have written"
        )
    delegated = sum(1 for call in attempt.tool_calls if call.tool_name in commands.DELEGATION_TOOLS)
    if delegated:
        clauses.append(f"delegated {delegated} {_plural(delegated, 'call')} that may have written")
    reasons = built.reasons if built is not None else ["transcript_missing"]
    attempt_level = [reason for reason in reasons if reason not in _EXPLAINED_REASONS]
    if attempt_level:
        clauses.append(f"ledger incomplete ({', '.join(attempt_level)})")
    if not clauses:
        clauses.append("wrote nothing the ledger names")
    return clauses


def _unreturned_subagents(run: Run, population: list[Attempt]) -> list[Attempt]:
    """docs/DESIGN.md 4.13.1 (S11): a session's subagents outside the risk
    population that did not return a result to their parent (the guard behind a
    session's `none`)."""
    if run.kind != "session":
        return []
    inside = {id(attempt) for attempt in population}
    return [
        attempt for attempt in run.attempts
        if id(attempt) not in inside and attempt.returned_to_parent is not True
    ]


def _shell_arg(text: str, level: str) -> str:
    """A repository spelling as a command argument: quoted at raw (a real path),
    quoted at the other levels only when it holds characters a shell would split."""
    if level == "raw":
        return shlex.quote(text)
    return text if re.fullmatch(r"[\w@%+=:,./~-]+", text) else shlex.quote(text)


def _unknown_group_hints(run: Run, group, redactor: Redactor) -> list[str]:
    refs = ledger.attempt_refs(run)
    by_ref = {ref: attempt for attempt in _all_attempts(run) for ref in [refs[id(attempt)]]}
    seen: list[str] = []
    for ref in group.attempts:
        attempt = by_ref.get(ref)
        if attempt is None:
            continue
        for call in attempt.tool_calls:
            cwd = call.cwd
            if isinstance(cwd, str) and cwd.startswith("/") and cwd not in seen:
                seen.append(cwd)
    return [
        f"    hint: an attempt ran in {redactor.names.path(cwd)}" for cwd in seen[:_MAX_HINTS]
    ]


def _observation_state_lines(run: Run, grouped, observation, redactor: Redactor) -> list[str]:
    """docs/DESIGN.md 4.7 item 3 of the WORK AT RISK block."""
    if observation is not None:
        lines: list[str] = []
        checked = [group for group in observation.groups if group.checked]
        if checked:
            states = [obs.state for group in checked for obs in group.paths.values()]
            dirty = states.count("DIRTY_NOW")
            lines.append(
                f"  {dirty} {_plural(dirty, 'path')} dirty now, "
                f"{states.count('MISSING_NOW')} missing, {states.count('CLEAN_NOW')} clean"
            )
        reasons: list[str] = []
        for group in observation.groups:
            if not group.checked and not group.refused and group.reason not in reasons:
                reasons.append(group.reason or "not checked")
        for reason in reasons:
            lines.append(f"  repo state not checked ({redactor(reason)})")
        return lines
    if not any(not group.refused and group.paths for group in grouped.repos.values()):
        return []
    if _ended_recently(run, _now()):
        return ["  repo state: run with --observe"]
    return ["  historical: repo state not checked (run older than 72h)"]


def _manual_command_text(repo: str, redactor: Redactor) -> str:
    """The manual `run yourself: git ... status --short` command for one repository, in the
    spelling the level allows (docs/DESIGN.md 4.7 item 4; REPO STATE reuses it, 4.9)."""
    shown = redactor.names.path(repo)
    if redactor.level == "share":
        return f"run yourself: git status --short in {shown}; --raw prints the exact command"
    return f"run yourself: git -C {_shell_arg(shown, redactor.level)} status --short"


def _manual_command_lines(run: Run, grouped, redactor: Redactor, observation=None) -> list[str]:
    """docs/DESIGN.md 4.7 item 4: a refused group runs no git, so the block ends
    with the manual command; so does (N5 fix 1, m3) every observed group that was not
    checked or holds unobserved paths, whatever the reason, so the person is always
    told to look."""
    needs: set[str] = {repo for repo, group in grouped.repos.items() if group.refused}
    if observation is not None:
        needs.update(
            group.repo for group in observation.groups
            if group.repo is not None and (not group.checked or group.unobserved_paths)
        )
    lines: list[str] = []
    for repo in grouped.repos:
        if repo not in needs:
            continue
        lines.append("  " + _manual_command_text(repo, redactor))
    unknown = grouped.unknown
    if unknown.attempts or unknown.paths:
        lines.append("  run yourself: git status --short in the repository the agent worked in")
        if redactor.level != "share":
            lines.extend(_unknown_group_hints(run, unknown, redactor))
    return lines


def _work_at_risk_lines(run: Run, observation, redactor: Redactor) -> list[str]:
    """docs/DESIGN.md 4.7 (N5c-1; plan S4 and S11): WORK AT RISK from the write
    ledger and the repository groups."""
    population = ledger.risk_population(run)
    unreturned = _unreturned_subagents(run, population)
    risk = severity.work_risk(run)
    if risk == "none" and not unreturned:
        # P5: an empty population never claims a kill or a failure.
        if not population:
            return ["WORK AT RISK  none: no attempt was killed, failed, or left unfinished."]
        return ["WORK AT RISK  none: the attempts at risk wrote no paths."]

    def subagent_lines() -> list[str]:
        return [
            f"  {_attempt_ident(attempt, run, redactor)}  no result returned to the main session"
            for attempt in unreturned
        ]

    if not population and run.risk_population_completeness == "complete":
        k = len(unreturned)
        verb = "it" if k == 1 else "they"
        return [
            f"WORK AT RISK  unknown: {k} {_plural(k, 'subagent')} never returned a result to "
            f"the main session; the tool cannot say what {verb} wrote.",
            *subagent_lines(),
        ]
    if not population:
        return [
            "WORK AT RISK  unknown: the tool could not establish which attempts were at risk: "
            f"{_run_reason_words(run, redactor)}."
        ]

    grouped = ledger.group_by_repo(run)
    paths: set[str] = set()
    commit_ids: list[str] = []
    for attempt in population:
        if attempt.ledger is not None:
            paths.update(
                entry.path for entry in attempt.ledger.entries
                if entry.path and entry.effect in ("observed", "claimed_target", "attempted")
            )
        facts = attempt.facts
        if facts is not None and isinstance(facts.commits, list):
            commit_ids.extend(c for c in facts.commits if c not in commit_ids)
    partial = run.risk_population_completeness != "complete" or any(
        attempt.ledger is None or attempt.ledger.completeness != "complete"
        for attempt in population
    )
    n, p, c = len(population), len(paths), len(commit_ids)
    k = _commit_commands_without_id(population)
    first = (
        f"WORK AT RISK  {n} {_plural(n, 'attempt')} at risk: {p} {_plural(p, 'path')} named, "
        f"{c} {_plural(c, 'commit')} reported"
    )
    if k:
        first += f" ({k} {_plural(k, 'call')} with a commit command reported no id)"
    first += "; the tool cannot see all of what was written." if partial else "."
    lines = [first]
    for attempt in population:
        lines.append(
            f"  {_attempt_ident(attempt, run, redactor)}  {'; '.join(_attempt_clauses(attempt))}"
        )
    if run.risk_population_completeness != "complete":
        lines.append(
            "  the list of attempts at risk may be incomplete: "
            f"{_run_reason_words(run, redactor)}."
        )
    lines.extend(_observation_state_lines(run, grouped, observation, redactor))
    lines.extend(_manual_command_lines(run, grouped, redactor, observation))
    if unreturned:
        k = len(unreturned)
        ids = ", ".join(_attempt_ident(attempt, run, redactor) for attempt in unreturned)
        lines.append(
            f"  {k} {_plural(k, 'subagent')} outside this list never returned a result to "
            f"the main session: {ids}"
        )
    return lines


def _finding_ids_by_detector(findings: list[Finding], detector: str) -> list[Finding]:
    return [f for f in findings if f.detector == detector]


_ABSENT_FILE_DETECTORS = frozenset({"missing_artifact", "claim_contradiction"})


def _absent_files_item(run: Run, findings: list[Finding]) -> dict | None:
    """docs/DESIGN.md 4.13.4, 4.7 item 3: one `[look]` item for however many
    shown info-severity `missing_artifact` and `claim_contradiction` findings
    exist, only while the run's own last activity is known and within 72
    hours. A rendering hint, never a severity change; no path in its text."""
    count = sum(
        1 for finding in findings
        if finding.detector in _ABSENT_FILE_DETECTORS and finding.severity == "info"
    )
    if count == 0 or not _ended_within_72h_strict(run, _now()):
        return None
    if count == 1:
        text = "1 expected file absent now: check whether the run wrote it"
    else:
        text = f"{count} expected files absent now: check whether the run wrote them"
    return {
        "grade": "look",
        "text": text,
        "command": f"agent-postmortem run {run.run_id} --verbose",
    }


def _what_to_do_items(
    run: Run, findings: list[Finding], observation, observe_given: bool,
) -> list[dict]:
    """docs/DESIGN.md 4.7 rule 4: deterministic, at most 3 items, each
    {'grade': 'look'|'decide', 'text': str, 'command': str|None}."""
    items: list[dict] = []

    # docs/DESIGN.md 4.7 item 1 (N5c-1): only a named repository group that is
    # not refused is ever observed, so only one earns the suggestion.
    observable = any(
        not group.refused and group.paths for group in ledger.group_by_repo(run).repos.values()
    )
    if observable and not observe_given and _ended_recently(run, _now()):
        items.append({
            "grade": "look",
            "text": "see which paths are dirty",
            "command": f"agent-postmortem run {run.run_id} --observe",
        })

    failed = _finding_ids_by_detector(findings, "attempt_failed")
    wait_item = None
    safe_item = None
    for finding in failed:
        if finding.severity != "major":
            continue  # the key was already retried to completion; nothing to decide
        retry_now = finding.metrics.get("retry_now")
        reset_time = finding.metrics.get("reset_time")
        if wait_item is None and retry_now == "no" and reset_time:
            wait_item = {
                "grade": "decide",
                "text": f"wait until {reset_time} before relaunching",
                "command": None,
            }
        if safe_item is None and retry_now == "yes":
            cause = finding.metrics.get("cause", "unattributed")
            safe_item = {
                "grade": "decide",
                "text": f"relaunch is safe now: {_cause_phrase(cause)}",
                "command": None,
            }
    if len(items) < 3:
        if wait_item is not None:
            items.append(wait_item)
        elif safe_item is not None:
            items.append(safe_item)

    absent_item = _absent_files_item(run, findings)
    if absent_item is not None and len(items) < 3:
        items.append(absent_item)

    if len(items) < 3:
        # docs/DESIGN.md 4.7 rule 4c / 9 (unit M6): `run` never scans other
        # runs to learn a real r-of-R rate, so this always points at the
        # fleet-wide proposal command instead of naming this one run --
        # `rules --run <id>` still exists, just no longer what `run` prints.
        backing_rule_ids = {
            rule_id
            for finding in findings
            if finding.severity in ("major", "critical")
            for rule_id in finding.rule_ids
        }
        if backing_rule_ids:
            items.append({
                "grade": "decide",
                "text": "rules that would have prevented this",
                "command": "agent-postmortem rules propose",
            })

    # docs/DESIGN.md 4.7, 4.13.8: no generic "see the findings hidden here"
    # item. The default view hides only info findings, and the trailing
    # "N info findings not shown (--verbose)" line is already the pointer.
    return items[:3]


def _what_to_do_content(item: dict) -> str:
    grade, text, command = item["grade"], item["text"], item["command"]
    if grade == "look":
        return f"{command}   {text}"
    if command:
        return f"{text}: {command}"
    return text


def _what_to_do_lines(items: list[dict]) -> list[str]:
    lines = ["WHAT TO DO"]
    for index, item in enumerate(items, start=1):
        tag_field = f"[{item['grade']}]"
        lines.append(f"  {index}. {tag_field:<9}{_what_to_do_content(item)}")
    return lines


def _agent_end(final: Attempt | None, run: Run) -> str:
    """docs/DESIGN.md 4.7 rule 5: 'completed', 'killed', 'failed', 'stopped
    by parent', 'interrupted', 'superseded', 'no result', or 'unknown',
    derived from the final attempt's own outcome and interrupt cause.
    (v0.3, N2, 4.13.3) An attempt still open in a workflow journal (no
    'result'/'failed' event ever closed it) shows 'no result' rather than
    the stale 'completed' _compute_outcome falls back to for a transcript
    that simply has not (yet) ended in the kill sentinel."""
    if final is None:
        return "unknown"
    if (
        final.outcome == "completed"
        and run.kind == "workflow"
        and reader.attempt_unresolved_in_journal(run.journal, final.key, final.ordinal)
    ):
        return "no result"
    if final.outcome != "killed":
        return final.outcome
    cause = final.interrupt.cause if final.interrupt is not None else None
    return _AGENT_END_KILLED_CAUSE.get(cause, "killed")


_ALL_PATH_EFFECTS = frozenset({"observed", "claimed_target", "attempted"})


def _agent_paths(agent) -> tuple[int | None, bool]:
    """(count, partial) for the AGENT table's PATHS column (docs/DESIGN.md 4.7 rule 5, 4.13.8): the
    distinct ledger paths of effect observed, claimed_target, or attempted over every attempt of
    this logical agent, plus `facts.paths_written`, each normalized path once. `partial` is true
    when any attempt's ledger is partial, a missing transcript included (the count is then a
    lower bound); the count is None when no attempt has a ledger and no attempt's facts could be
    established at all."""
    known = False
    partial = False
    paths: set[str] = set()
    for attempt in agent.attempts:
        built = attempt.ledger
        if built is not None:
            # a ledger with no transcript (N8 fix 1, S1) names nothing, but it makes the count a
            # lower bound all the same; only a ledger built from a transcript establishes it
            if "transcript_missing" not in built.reasons:
                known = True
            if built.completeness == "partial":
                partial = True
        if attempt.facts is not None and isinstance(attempt.facts.paths_written, list):
            known = True
        paths.update(ledger.attempt_ledger_paths(
            attempt, _ALL_PATH_EFFECTS, include_facts_written=True,
        ))
    return (len(paths) if known else None), partial


def _agent_facts_totals(agent) -> tuple[str, int]:
    """(paths display, commit count) for the AGENT table's PATHS/COMMITS
    columns: PATHS is `_agent_paths` (`N`, `N+` for a partial ledger, `?` when
    nothing could be established, docs/DESIGN.md 4.7 rule 5); COMMITS is the
    union of `commits` over every attempt of this logical agent and has no
    such marker, it simply counts what is known."""
    count, partial = _agent_paths(agent)
    commits: set[str] = set()
    for attempt in agent.attempts:
        facts = attempt.facts
        if facts is not None and isinstance(facts.commits, list):
            commits.update(facts.commits)
    if count is None:
        return "?", len(commits)
    return f"{count}{'+' if partial else ''}", len(commits)


def _commit_commands_without_id(attempts) -> int:
    """FA6: the sum of `commit_commands_without_id` over attempts whose facts say a
    number (an unknown or missing fact adds nothing: the lower-bound mark is for a
    call the tool SAW contain a commit command and report no id)."""
    total = 0
    for attempt in attempts:
        facts = attempt.facts
        value = getattr(facts, "commit_commands_without_id", 0) if facts is not None else 0
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


def _commits_display(agent, commits_count: int) -> str:
    """The AGENT table's COMMITS cell: the count of known commit ids, `N+` when
    any attempt of the agent has a call that contains a commit command and reported no
    id (a lower bound: a commit may have happened, docs/DESIGN.md 4.7, FA6)."""
    if _commit_commands_without_id(agent.attempts) > 0:
        return f"{commits_count}+"
    return str(commits_count)


def _runtime_owned_kill(attempt) -> bool:
    """docs/DESIGN.md 4.13.8: an attempt's interrupt is a kill the runtime
    may own only when its cause is neither `parent_stop` nor `human` (that is,
    `runtime` or `unattributed`). The AGENT table's KILLED column is the only
    per-agent kill count; the account sentences split causes themselves."""
    interrupt = attempt.interrupt
    return interrupt is not None and interrupt.cause not in ("parent_stop", "human")


_MAX_LABEL = 40
_MAX_PHASE = 22
_LOWER_BOUND_LEGEND = "+ is a lower bound: incomplete evidence may hide more paths or commits."


def _truncate_shown(text: str, limit: int) -> str:
    """docs/DESIGN.md 4.7 (P3): a shown label longer than `limit` keeps its first
    `limit - 3` characters and ends `...`."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _agent_table(run: Run, redactor: Redactor) -> list[str]:
    """docs/DESIGN.md 4.7 rule 5: AGENT, [PHASE], ATTEMPTS, END, KILLED,
    PATHS, COMMITS, WASTED. PHASE is kept only when at least one logical
    agent of this run actually carries one (a session's subagents never
    do)."""
    show_phase = any(agent.phase for agent in run.agents)
    # docs/DESIGN.md 4.7 (field milestone F, P3): the shown label and phase are truncated, and
    # each column is as wide as its longest shown value plus two separator spaces (floors 10
    # and 11, today's widths, which the share labels never exceed).
    # The names layer has already replaced any path in a name (and stripped control characters),
    # so truncation can never cut a path in half; the assembled row is still scrubbed whole
    # below. Agent by agent, label then phase: the order the paths were numbered in before.
    labels: list[str] = []
    phases: list[str] = []
    for agent in run.agents:
        labels.append(_truncate_shown(redactor.names.agent(agent.label), _MAX_LABEL))
        phases.append(_truncate_shown(redactor.names.phase(agent.phase), _MAX_PHASE))
    label_width = max([10, *(len(label) + 2 for label in labels)])
    phase_width = max([11, *(len(phase) + 2 for phase in phases)])
    header = f"{'AGENT':<{label_width}}"
    if show_phase:
        header += f"{'PHASE':<{phase_width}}"
    header += f"{'ATTEMPTS':<10}{'END':<19}{'KILLED':<8}{'PATHS':<7}{'COMMITS':<9}WASTED"
    lines = [header]
    lower_bound = False

    for agent, label, phase in zip(run.agents, labels, phases):
        attempts = sorted(agent.attempts, key=lambda a: a.ordinal)
        final = attempts[-1] if attempts else None
        wasted_seconds = sum(attempt_seconds(a) for a in wasted_attempts(attempts))
        killed = sum(1 for a in attempts if _runtime_owned_kill(a))
        paths_display, commits_count = _agent_facts_totals(agent)
        commits_display = _commits_display(agent, commits_count)
        lower_bound = lower_bound or paths_display.endswith("+") or commits_display.endswith("+")

        row = f"{label:<{label_width}}"
        if show_phase:
            row += f"{phase:<{phase_width}}"
        row += (
            f"{len(attempts):<10}{_agent_end(final, run):<19}{killed:<8}"
            f"{paths_display:<7}{commits_display:<9}{format_duration(wasted_seconds)}"
        )
        lines.append(redactor.scrub_local_line(row))
    if lower_bound:  # P4
        lines.append(_LOWER_BOUND_LEGEND)
    return lines


_REFUSED_WORDS = (
    "its ledger is partial: incomplete evidence may hide more writes, so the tool does not "
    "run status for it"
)


def _repo_state_section(observation, redactor: Redactor) -> list[str]:
    """docs/DESIGN.md 4.9 (N5c-1): per group `repository <repo>  checked: ...`,
    then per observed path its state, later write yes/no, and the ordinals of the
    attempts that wrote it, and per unobserved path `not observed`. Only ever
    called when the caller actually ran --observe (observation is not None)."""
    if not observation.groups:  # P8: the scope, on one line
        return ["REPO STATE  (no repository holds a path at risk)"]
    lines = ["REPO STATE"]
    for group in observation.groups:
        repo = redactor.names.path(group.repo) if group.repo is not None else "unknown"
        if group.checked:
            lines.append(f"  repository {repo}  checked: yes")
        elif group.refused and group.repo is not None:
            # P7: say why, then the manual command (the JSON reason code stays `refused`)
            lines.append(f"  repository {repo}  checked: no ({_REFUSED_WORDS})")
            lines.append("    " + _manual_command_text(group.repo, redactor))
        else:
            reason = redactor(group.reason) if group.reason else "not checked"
            lines.append(f"  repository {repo}  checked: no ({reason})")
        for path, path_observation in group.paths.items():
            later = "yes" if path_observation.later_write_observed else "no"
            ordinals = ", ".join(str(entry["ordinal"]) for entry in path_observation.attempts)
            lines.append(
                f"    {redactor.names.path(path)}  {path_observation.state}  "
                f"later write: {later}  attempts: {ordinals}"
            )
        for path in group.unobserved_paths:
            lines.append(f"    {redactor.names.path(path)}  not observed")
    return lines


def _finding_title(finding: Finding, redactor: Redactor) -> str:
    """The title of a finding as a sink prints it (docs/DESIGN.md 4.13.4, 5.1; F fix 7, W7-2): a
    title that names a path renders from its structured parts (the path rendered once, the
    tool's own words never scanned); any other title goes through `Redactor.title`."""
    if finding.title_parts is not None:
        return redactor.title_parts(finding.title_parts)
    return redactor.title(finding.title)


def _findings_section(
    findings: list[Finding], redactor: Redactor, view: "EvidenceView | None" = None,
) -> list[str]:
    counts = {severity: 0 for severity in _SEVERITIES}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1

    lines = [
        f"FINDINGS (critical {counts['critical']}, major {counts['major']}, "
        f"minor {counts['minor']})"
    ]
    for finding in findings:
        title = _finding_title(finding, redactor)
        lines.append(f"[{finding.severity}] {finding.detector}  {title}")
        for shown in _shown_evidence(finding.evidence, view, redactor):
            lines.append(f"  {shown}")
    return lines


_TOOLING_STORY_PHRASES = {
    "bash_backgrounded": "Bash commands backgrounded",
    "bash_timeout": "Bash commands timed out",
    "tool_json_error": "tool input JSON errors",
    "structured_output_failure": "structured output failures",
    "codex_write_denied": "sandbox write denials",
    "classifier_unavailable": "classifier outages",
    "git_cwd_mismatch": "git cwd mismatches",
}

_CLAIM_CLAUSE_PHRASES = {
    "ok_false": "ok is false",
    "ready_false": "ready is false",
    "report_path_missing": "a report_path that does not exist",
}


def _claim_contradiction_phrases(metrics: dict) -> list[str]:
    """The human phrases backing a claim_contradiction finding's clauses
    (docs/DESIGN.md 4.7 rule 6), rebuilt from Finding.metrics (clause ids
    plus the claims dict) rather than re-parsing the finding's own title --
    the same information the detector used to build its title in the first
    place (detectors/claim_contradiction.py's _clauses)."""
    claims = metrics.get("claims") if isinstance(metrics.get("claims"), dict) else {}
    clause_ids = metrics.get("clauses") or []
    phrases = []
    for clause_id in clause_ids:
        if clause_id == "tests_failed":
            n = claims.get("tests_failed", 0)
            phrases.append(f"{n} failed {_plural(n, 'test')}")
        elif clause_id == "suite_failed":
            n = claims.get("suite_failed", 0)
            phrases.append(f"{n} failed suite {_plural(n, 'check')}")
        elif clause_id == "blockers":
            n = claims.get("blockers_count", 0)
            phrases.append(f"{n} {_plural(n, 'blocker')}")
        else:
            phrases.append(_CLAIM_CLAUSE_PHRASES.get(clause_id, clause_id))
    return phrases or ["a contradiction"]


def _finding_ordinal(finding: Finding, attempts_sorted: list[Attempt]) -> int:
    for attempt in attempts_sorted:
        if attempt.agent_id == finding.agent_id:
            return attempt.ordinal
    return 1


def _agent_findings_pairs(
    run: Run, findings: list[Finding],
) -> tuple[list[tuple], list[Finding]]:
    """[(logical agent, its findings)] for every agent of the run, in
    run.agents order, plus the findings with no agent_id at all (docs/
    DESIGN.md 4.7 rule 6: these form the one 'run' story). A finding whose
    agent_id names no attempt of this run (should not happen in practice) is
    folded into the run-level list too, rather than silently dropped."""
    owner_by_agent_id: dict[str, object] = {}
    for agent in run.agents:
        for attempt in agent.attempts:
            owner_by_agent_id[attempt.agent_id] = agent
    findings_by_agent: dict[int, list[Finding]] = {id(agent): [] for agent in run.agents}
    run_level: list[Finding] = []
    for finding in findings:
        owner = owner_by_agent_id.get(finding.agent_id) if finding.agent_id else None
        if owner is None:
            run_level.append(finding)
        else:
            findings_by_agent[id(owner)].append(finding)
    pairs = [(agent, findings_by_agent[id(agent)]) for agent in run.agents]
    return pairs, run_level


def _agent_story_pieces(agent, agent_findings: list[Finding]) -> list:
    """docs/DESIGN.md 4.7 rule 6: one deterministic sentence per clause type
    present, in a fixed order, joined into a paragraph, as pieces (`_sentences_pieces`).
    Not yet redacted or wrapped -- the caller does both, once, the same way for text and JSON."""
    by_detector: dict[str, list[Finding]] = {}
    for finding in agent_findings:
        by_detector.setdefault(finding.detector, []).append(finding)

    attempts_sorted = sorted(agent.attempts, key=lambda a: a.ordinal)
    non_final = attempts_sorted[:-1]
    final = attempts_sorted[-1] if attempts_sorted else None
    mislabeled_by_agent_id = {f.agent_id: f for f in by_detector.get("mislabeled_kill", [])}

    clauses: list = []

    lead = None
    silent_retry_findings = by_detector.get("silent_retry", [])
    if silent_retry_findings:
        k = silent_retry_findings[0].metrics.get("attempts", len(attempts_sorted)) - 1
        k = max(k, 0)
        silences = []
        all_runtime = bool(non_final)
        for attempt in non_final:
            mf = mislabeled_by_agent_id.get(attempt.agent_id)
            if mf is not None and mf.metrics.get("cause") == "runtime":
                silence = mf.metrics.get("silence_seconds")
                silences.append(f"{int(round(silence))}s" if silence is not None else "unknown")
            else:
                all_runtime = False
        if all_runtime and silences:
            silence_text = ", ".join(silences)
            lead = f"killed {k} {_plural(k, 'time')} by the runtime ({silence_text} of silence)"
        else:
            lead = f"restarted {k} {_plural(k, 'time')}"

    parent_stop = False
    if final is not None:
        mf = mislabeled_by_agent_id.get(final.agent_id)
        parent_stop = mf is not None and mf.metrics.get("cause") == "parent_stop"

    if lead is not None and parent_stop:
        clauses.append(f"{lead}, then stopped by the parent")
    elif lead is not None:
        clauses.append(lead)
    elif parent_stop:
        clauses.append("stopped by the parent")

    for finding in by_detector.get("attempt_failed", []):
        ordinal = _finding_ordinal(finding, attempts_sorted)
        phrase = _cause_phrase(finding.metrics.get("cause", "unattributed"))
        reset_time = finding.metrics.get("reset_time")
        text = f"attempt {ordinal} failed: {phrase}"
        if reset_time:
            text += f", resets {reset_time}"
        clauses.append(text)

    for finding in by_detector.get("claim_contradiction", []):
        ordinal = _finding_ordinal(finding, attempts_sorted)
        phrases = _claim_contradiction_phrases(finding.metrics)
        clauses.append(f"attempt {ordinal} completed but reported {'; '.join(phrases)}")

    _paths_display, commits_count = _agent_facts_totals(agent)
    path_count, paths_partial = _agent_paths(agent)
    facts_known = path_count is not None
    paths_count = path_count or 0
    was_killed = final is not None and final.outcome == "killed"
    if paths_count or commits_count:
        # a partial ledger makes the count a lower bound (docs/DESIGN.md 4.13.8)
        paths_word = (
            f"{paths_count}+ paths" if paths_partial
            else f"{paths_count} {_plural(paths_count, 'path')}"
        )
        clauses.append(
            f"Wrote {paths_word}, reported {commits_count} {_plural(commits_count, 'commit')}."
        )
    elif facts_known and was_killed and not paths_partial:
        clauses.append("Wrote no paths.")

    tooling_bits = []
    for detector_id, phrase in _TOOLING_STORY_PHRASES.items():
        for finding in by_detector.get(detector_id, []):
            count = finding.metrics.get("count", 1)
            tooling_bits.append(f"{count} {phrase}")
    if tooling_bits:
        clauses.append("; ".join(tooling_bits))

    if not clauses and agent_findings:
        clauses.append(agent_findings[0].title_parts or agent_findings[0].title)

    return _sentences_pieces(clauses)


def _sentences(clauses: list[str]) -> str:
    """Join clauses as sentences: each starts with a capital and the whole
    ends with one period. Finding titles start lowercase by convention
    (docs/DESIGN.md 4.3), so this is where a story gets its capitals."""
    parts = [clause.rstrip(".") for clause in clauses if clause]
    text = ". ".join(c[:1].upper() + c[1:] for c in parts)
    return f"{text}." if text else ""


def _sentences_pieces(clauses: list) -> list:
    """`_sentences` as pieces (F fix 7, W7-2): a clause is a string or the `TitleParts` of a
    finding title that names a path. With no `TitleParts` the result is the one string
    `_sentences` returns (or no piece for no text). With one, the template is capitalized and
    stripped of a final period like any clause, and the pieces are the strings around it (merged)
    and the `TitleParts` themselves, so `_render_pieces` can render the path once and never scan
    the tool's own words."""
    if not any(isinstance(clause, TitleParts) for clause in clauses):
        text = _sentences(clauses)
        return [text] if text else []
    parts: list = []
    for clause in clauses:
        if not clause:
            continue
        if isinstance(clause, TitleParts):
            head, placeholder, tail = clause.template.partition("{path}")
            template = head + placeholder + tail.rstrip(".")
            parts.append(TitleParts(template[:1].upper() + template[1:], clause.path))
        else:
            text = clause.rstrip(".")
            parts.append(text[:1].upper() + text[1:])
    pieces: list = []
    for index, part in enumerate(parts):
        for piece in ((". ",) if index else ()) + (part,):
            if isinstance(piece, str) and pieces and isinstance(pieces[-1], str):
                pieces[-1] += piece
            else:
                pieces.append(piece)
    if pieces and isinstance(pieces[-1], str):
        pieces[-1] += "."
    else:
        pieces.append(".")
    return pieces


def _pieces_raw(pieces: list) -> str:
    """The pieces with the real path (the raw text of a story)."""
    return "".join(piece.raw() if isinstance(piece, TitleParts) else piece for piece in pieces)


def _render_pieces(pieces: list, redactor: Redactor) -> str:
    """The pieces as a sink prints them: a string through `Redactor.title` (labels, paths and the
    rest of the scanner), a `TitleParts` through `Redactor.title_parts` (its path rendered once,
    its words never scanned)."""
    return "".join(
        redactor.title_parts(piece) if isinstance(piece, TitleParts) else redactor.title(piece)
        for piece in pieces
    )


def _story_records(run: Run, findings: list[Finding], verbose: bool) -> list[dict]:
    """docs/DESIGN.md 4.7 rule 6: one record per logical agent that earns a
    story (at least one non-info finding, or --verbose with any finding at
    all), worst severity first then by (pre-pseudonym) label, plus one
    trailing 'run' record for findings with no agent_id. Each record is
    {'agent': label_or_"run", 'severity': str, 'text': str,
    'finding_ids': [...], 'findings': [...]} -- 'findings' is
    report-internal (kept for evidence lookups) and is stripped before this
    goes anywhere public (text rendering, JSON export)."""
    pairs, run_level = _agent_findings_pairs(run, findings)

    entries = []
    for agent, agent_findings in pairs:
        if not agent_findings:
            continue
        non_info = [f for f in agent_findings if f.severity != "info"]
        if not non_info and not verbose:
            continue
        relevant = agent_findings if verbose else non_info
        rank = min(_SEVERITIES.index(f.severity) for f in relevant)
        pieces = _agent_story_pieces(agent, relevant)
        entries.append({
            "agent": agent.label,
            "severity": _SEVERITIES[rank],
            "text": _pieces_raw(pieces),
            "pieces": pieces,
            "finding_ids": [f.finding_id for f in relevant],
            "findings": relevant,
            "_rank": rank,
        })
    # Worst severity first; ties broken by wasted time (biggest loss first),
    # then by the agent's position in the run, which is the order the AGENT
    # table and the agent-N pseudonyms follow. Sorting by the raw label would
    # look random once the redactor has replaced it.
    position = {agent.label: index for index, agent in enumerate(run.agents)}

    def wasted(entry: dict) -> float:
        return sum(
            (f.metrics.get("wasted_seconds", 0) or 0)
            for f in entry["findings"] if f.detector == "silent_retry"
        )

    entries.sort(
        key=lambda entry: (entry["_rank"], -wasted(entry), position.get(entry["agent"], 1 << 30))
    )
    for entry in entries:
        del entry["_rank"]

    run_relevant = [f for f in run_level if verbose or f.severity != "info"]
    if run_relevant:
        rank = min(_SEVERITIES.index(f.severity) for f in run_relevant)
        pieces = _sentences_pieces([f.title_parts or f.title for f in run_relevant])
        entries.append({
            "agent": "run",
            "severity": _SEVERITIES[rank],
            "text": _pieces_raw(pieces),
            "pieces": pieces,
            "finding_ids": [f.finding_id for f in run_relevant],
            "findings": run_relevant,
        })

    return entries


def _wrap_story(label: str, text: str) -> list[str]:
    """Story text wrapped at 78 columns, at most 2 lines (docs/DESIGN.md 4.7
    rule 8), the first line prefixed by the (already pseudonymized/redacted)
    label, continuation lines indented to the same column."""
    indent = " " * (len(label) + 2)
    width = max(78 - len(indent), 20)
    wrapped = textwrap.wrap(text, width=width) or [text]
    wrapped = wrapped[:2]
    lines = [f"  {label}  {wrapped[0]}"]
    for extra in wrapped[1:]:
        lines.append(f"{indent}{extra}")
    return lines


def _stories_section(
    run: Run, findings: list[Finding], redactor: Redactor, verbose: bool, evidence: bool,
    view: "EvidenceView | None" = None,
) -> list[str]:
    entries = _story_records(run, findings, verbose)
    lines = [f"STORIES ({len(entries)})   details: --verbose   evidence: --evidence"]
    if evidence and view is not None and view.note() is not None:
        lines.append(f"  {view.note()}")
    for entry in entries:
        label = "run" if entry["agent"] == "run" else redactor.names.agent(entry["agent"])
        text = _render_pieces(entry["pieces"], redactor)
        lines.extend(_wrap_story(label, text))
        if evidence:
            story_lines: list[str] = []
            for finding in entry["findings"]:
                for line in finding.evidence:
                    if len(story_lines) >= 2:
                        break
                    text = view.line(line) if view is not None else redactor(line)
                    if text is not None and (redactor.level == "raw" or text not in story_lines):
                        story_lines.append(text)
                if len(story_lines) >= 2:
                    break
            lines.extend(f"    {text}" for text in story_lines)
    return lines, entries


def _silenced_detectors_line(silenced_detectors: frozenset) -> str | None:
    """docs/DESIGN.md 4.7/4.10: the last line of `run`'s and `scan`'s text
    output, printed whenever [detectors].off silences at least one detector
    -- a config-level fact, independent of whether that detector actually
    fired on this particular run or scan."""
    if not silenced_detectors:
        return None
    ids = ", ".join(sorted(silenced_detectors))
    n = len(silenced_detectors)
    return f"{n} {_plural(n, 'detector')} silenced by config: {ids}"


def render_run_text(
    run: Run,
    findings: list[Finding],
    redactor: Redactor,
    observation=None,
    verbose: bool = False,
    evidence: bool = False,
    show_age: bool = True,
    silenced_detectors: frozenset = frozenset(),
) -> str:
    """docs/DESIGN.md 4.7 (rewritten by unit M3a): header, account, WORK AT
    RISK, WHAT TO DO, AGENT table, [REPO STATE], STORIES, then either the
    hidden-info-count line or (--verbose) the full v0.1 FINDINGS list, then
    (unit M3c, 4.10) a last line naming every detector [detectors].off
    silenced, when there is at least one.
    show_age=False drops the header's "(N days ago)", which depends on the
    wall clock; `demo` uses it so README's byte-compared demo block stays
    stable from one day to the next.

    `silenced_detectors` (docs/DESIGN.md 4.6/4.7): findings from these
    detector ids are dropped before anything else in this function looks at
    `findings` -- the header's verdict, the account sentences, WORK AT RISK,
    WHAT TO DO, the AGENT table's story-adjacent totals, STORIES, and the
    --verbose FINDINGS list all see only the shown (non-silenced) findings,
    exactly like a caller that filtered before calling ever existed. The
    caller (config.partition_silenced) still has the full list for --json."""
    findings = [f for f in findings if f.detector not in silenced_detectors]

    _register_names(run, redactor, detailed=True)
    _register_observation_paths(observation, redactor, run)
    view = EvidenceView(run, redactor, show_excerpts=evidence)

    info_count = sum(1 for finding in findings if finding.severity == "info")
    hidden_count = 0 if verbose else info_count

    lines = [_run_header_line(run, findings, show_age)]
    lines.extend(redactor(sentence) for sentence in _account_lines(run, findings))
    lines.append("")
    lines.extend(redactor(line) for line in _work_at_risk_lines(run, observation, redactor))
    lines.append("")
    items = _what_to_do_items(run, findings, observation, observation is not None)
    if items:  # docs/DESIGN.md 4.7: the heading is never printed empty
        lines.extend(redactor(line) for line in _what_to_do_lines(items))
        lines.append("")
    lines.extend(_agent_table(run, redactor))
    lines.append("")
    # docs/DESIGN.md 4.9 rule 10: the REPO STATE block appears only when the
    # caller actually ran --observe; without it, text output carries no
    # trace of observation at all.
    if observation is not None:
        lines.extend(_repo_state_section(observation, redactor))
        lines.append("")
    stories_lines, _entries = _stories_section(run, findings, redactor, verbose, evidence, view)
    lines.extend(stories_lines)

    if verbose:
        lines.append("")
        lines.extend(_findings_section(findings, redactor, view))
    elif hidden_count:
        lines.append("")
        hidden_word = _plural(hidden_count, "finding")
        lines.append(f"{hidden_count} info {hidden_word} not shown (--verbose)")

    silenced_line = _silenced_detectors_line(silenced_detectors)
    if silenced_line is not None:
        lines.append(silenced_line)

    return "\n".join(lines).rstrip("\n") + "\n"


# --- unit M3b: scan triage (NEEDS ATTENTION is the default; --all keeps ---
# --- the row-per-run list above/below unchanged) ---------------------------
#
# A run needing attention is ranked, not merely listed: possibly-live runs
# first, then runs whose killed/failed attempts left the most paths behind,
# then by verdict, then by wasted time, then by recency. Every function here
# is pure (no filesystem access): the CLI stats a run's own files once and
# hands the result in as `newest_mtime`, the same "inject the clock" pattern
# _now()/_age_string already use for the wall clock.

_VERDICT_RANK = {"FAILED": 0, "DEGRADED": 1, "CLEAN": 2}


def _verdict_bare(run: Run, findings: list[Finding]) -> str:
    """verdict_of() without its trailing ' (INCOMPLETE)' suffix, for a
    ranking or a candidate rule that only cares about FAILED/DEGRADED/CLEAN
    itself, never whether the run's own inspection was complete."""
    return verdict_of(run, findings).split(" ", 1)[0]


def is_possibly_live(
    run: Run, newest_mtime: float | None, now: datetime, quiet_seconds: float,
) -> bool:
    """unit M3b / docs/DESIGN.md 7.2's UNKNOWN_QUIET_SECONDS: the best a cold
    `scan` can say about liveness without hook state or a live task list --
    a run that has not finished and was touched inside the quiet window
    might still be running. **(fixed after cross-model review, N1N2 review
    finding 3)** This also applies to a session: a session's own
    Run.finished is always None (never anything the `run.finished` check
    below rejects, since v0.3 N2, D11), and its own newest mtime is already
    computed correctly by the caller (cli._run_files_for_liveness handles
    both kinds); a workflow scan already known to have finished is the only
    thing this function still rules out by `run.finished` alone. `newest_
    mtime` is None when no file could be statted at all (already gone by the
    time this runs), which is never "recently touched"."""
    if run.finished:
        return False
    if newest_mtime is None:
        return False
    return (now.timestamp() - newest_mtime) < quiet_seconds


def scan_age_string(run: Run, now: datetime, live: bool) -> str:
    """The triage age column and the JSON `age` field: 'possibly live' when
    live, else the abbreviated '<N>d/h/m ago' built from ended_at, or
    'unknown' when ended_at itself is unknown (never None -- age is always a
    str)."""
    if live:
        return "possibly live"
    if run.ended_at is None:
        return "unknown"
    return _age_string(run.ended_at, now, short=True)


def scan_paths_at_risk(run: Run) -> int:
    """The JSON `paths_at_risk` field and the ranking key's own path count
    (docs/DESIGN.md 4.13.8): the distinct ledger paths over the run's 4.13.1
    risk population (a completed attempt in a failed generation counts)."""
    paths, _complete = ledger.risk_paths(run)
    return len(paths)


def scan_paths_at_risk_complete(run: Run) -> bool:
    """The JSON `paths_at_risk_complete` field: true only when the risk
    population and every ledger in it are complete; `paths_at_risk` is a lower
    bound otherwise."""
    _paths, complete = ledger.risk_paths(run)
    return complete


def unfinished_state(run: Run, live: bool) -> str | None:
    """docs/DESIGN.md 5.3b (v0.3, N2): 'possibly running' | 'no terminal
    record' | 'failed' | 'stopped' | 'unknown' | None for a workflow row;
    'possibly running' | None for a session row. 'abandoned' is removed:
    derived only from the lifecycle, never from per-key journal state, so a
    run whose journal happens to record a per-key 'failed' event no longer
    shows up here at all once the run's own lifecycle resolved cleanly
    (journal_resolved) -- that per-key story belongs to attempt_failed, not
    this field.

    `live` is unit M3b's own hook-free 'possibly live' heuristic (a run not
    yet Run.finished whose files were touched inside the quiet window); it
    still takes priority when true, for both kinds, since it is strictly
    fresher evidence than a workflow's own (necessarily somewhat stale,
    lifecycle is built once at load time) activity field."""
    if live:
        return "possibly running"
    if run.kind != "workflow":
        return None
    lifecycle = run.lifecycle
    if lifecycle is None:
        return None
    if lifecycle.terminal in ("completed", "journal_resolved"):
        return None
    if lifecycle.terminal in ("failed", "stopped", "unknown"):
        return lifecycle.terminal
    # lifecycle.terminal == "none"
    return "possibly running" if lifecycle.activity == "recent" else "no terminal record"


def is_attention_candidate(run: Run, findings: list[Finding], now: datetime, live: bool) -> bool:
    """unit M3b rule 1, rules (b) to (d) (docs/DESIGN.md 4.7; field milestone F, FB2): rule
    (a), "possibly live", alone no longer makes a candidate; such a run is listed under LIVE
    NOW instead, and is a candidate only through (b) to (d). `live` stays in the signature
    because the caller computes it once for the row, the ranking key, and the LIVE NOW group."""
    del live
    verdict = _verdict_bare(run, findings)
    if verdict == "FAILED":  # (d) verdict FAILED regardless of age
        return True
    if _ended_within_72h_strict(run, now):
        if verdict == "DEGRADED":  # (b) ended recently, FAILED already handled above
            return True
        paths, _commits = _risk_paths_and_commits(run)
        if paths:  # (c) ended recently, a killed/failed/superseded attempt wrote a path
            return True
    return False


def _scan_top_detector(findings: list[Finding]) -> str | None:
    """The same rule cli._top_detector applies (docs/DESIGN.md 4.7's unit
    M1d: an info-only finding, present on every run, is never the "top"
    detector), duplicated here rather than imported so this module never
    depends on cli.py."""
    counts: dict[str, int] = {}
    for finding in findings:
        if finding.severity not in ("major", "critical"):
            continue
        counts[finding.detector] = counts.get(finding.detector, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda detector_id: counts[detector_id])


def attention_reason(run: Run, findings: list[Finding]) -> str:
    """unit M3b rule 3: the one-line NEEDS ATTENTION reason, deterministic,
    first that applies."""
    failed = [f for f in findings if f.detector == "attempt_failed"]
    if failed:
        k = len(failed)
        causes: list[str] = []
        for finding in failed:
            phrase = _cause_phrase(finding.metrics.get("cause", "unattributed"))
            if phrase not in causes:
                causes.append(phrase)
        text = f"{k} {_plural(k, 'attempt')} failed: {', '.join(causes)}"
        reset_time = next(
            (f.metrics.get("reset_time") for f in failed if f.metrics.get("reset_time")), None,
        )
        if reset_time:
            text += f", resets {reset_time}"
        return text

    runtime_kills = [
        f for f in findings
        if f.detector == "mislabeled_kill" and f.metrics.get("cause") == "runtime"
    ]
    if runtime_kills:
        k = len(runtime_kills)
        _written, commits = _risk_paths_and_commits(run)
        risk_paths, paths_complete = ledger.risk_paths(run)
        n_paths = len(risk_paths)
        # docs/DESIGN.md 4.13.8: a partial ledger makes the count a lower bound.
        paths_word = (
            f"{n_paths} {_plural(n_paths, 'path')}" if paths_complete else f"{n_paths}+ paths"
        )
        return (
            f"{k} {_plural(k, 'attempt')} killed by the runtime, "
            f"{paths_word} named, "
            f"{len(commits)} {_plural(len(commits), 'commit')}"
        )

    top = _scan_top_detector(findings)
    if top is not None:
        count = sum(
            1 for f in findings if f.detector == top and f.severity in ("major", "critical")
        )
        return f"{top} x{count}"

    if run.kind == "session":
        interrupted = sum(1 for attempt in run.attempts if attempt.interrupt is not None)
        if interrupted:
            return f"{interrupted} {_plural(interrupted, 'subagent')} interrupted"

    return "clean"


def attention_next_command(run: Run, now: datetime) -> str:
    """unit M3b rule 3's second line: --observe only when a killed or
    failed attempt is known to have written a path and the run is recent
    enough that a repo might still show it -- the same "unknown end counts
    as recent" convention _what_to_do_items already uses for this exact
    decision on the `run` report."""
    paths, _commits = _risk_paths_and_commits(run)
    if paths and _ended_recently(run, now):
        return f"agent-postmortem run {run.run_id} --observe"
    return f"agent-postmortem run {run.run_id}"


def attention_sort_key(
    run: Run, findings: list[Finding], live: bool, paths_at_risk: int, now: datetime,
) -> tuple:
    """unit M3b rule 1's ranking key, ascending: possibly live first, then
    runs that ended within 72 hours (a rescue is only possible while the
    working tree still holds the work), then paths at risk descending, then
    verdict (FAILED, DEGRADED, CLEAN), then wasted seconds descending, then
    ended_at descending (unknown last)."""
    recent = _ended_within_72h_strict(run, now)
    verdict_rank = _VERDICT_RANK.get(_verdict_bare(run, findings), 3)
    wasted_seconds = 0.0
    for finding in findings:
        if finding.detector == "wasted_time":
            wasted_seconds = finding.metrics.get("wasted_seconds", 0.0) or 0.0
    ended_ts = _aware(run.ended_at).timestamp() if run.ended_at is not None else float("-inf")
    return (
        0 if live else 1, 0 if recent else 1, -paths_at_risk, verdict_rank,
        -wasted_seconds, -ended_ts,
    )


def _scan_triage_totals_line(label: str, totals: dict) -> str:
    """One LAST 7 DAYS / ALL TIME line (unit M3b rule 5): label padded to
    14 columns, then the same workflow/session numbers _scan_totals_lines
    prints, condensed onto one line with format_duration instead of a
    '%.1fh' float -- wasted_hours is recovered back to seconds rather than
    threading a parallel seconds accumulator through _cmd_scan, since
    totals['workflow'] is also the JSON payload and test_cli_smoke.py's
    test_scan_totals_are_stratified_by_kind pins its exact key set."""
    workflow = totals.get("workflow", {})
    session = totals.get("session", {})
    wasted_seconds = workflow.get("wasted_hours", 0.0) * 3600.0
    return (
        f"{label:<14}{workflow.get('runs', 0)} workflow runs, "
        f"{workflow.get('runs_with_retries', 0)} with retries, "
        f"{format_duration(wasted_seconds)} wasted; "
        f"{session.get('runs', 0)} session runs, "
        f"{session.get('interrupted', 0)} subagents interrupted"
    )


def render_scan_triage_text(
    shown_candidates: list[dict],
    total_candidates: int,
    total_runs: int,
    totals_7d: dict,
    totals_all: dict,
    redactor: Redactor,
    silenced_detectors: frozenset = frozenset(),
    live_now: list[dict] | None = None,
    total_live_now: int = 0,
) -> str:
    """unit M3b: the new default `scan` text output. `shown_candidates` is
    already sorted and already truncated to --limit by the caller;
    `total_candidates` is the true count before truncation, so the header
    can say "(3 of 172 runs)" even when only 10 are printed. Each candidate
    dict carries 'run', 'age', 'verdict', 'reason', 'command' (attention_*
    above build these; the caller assembles the dict once per row so this
    renderer stays a pure formatter, the same division of labor
    render_run_text keeps with its own _work_at_risk_lines/_what_to_do_items
    callers)."""
    redactor.names = Pseudonyms(enabled=redactor.enabled)
    # docs/DESIGN.md 4.7 (FB2): a truncated list says so in its own header.
    truncated = len(shown_candidates) < total_candidates
    note = (
        f"; showing {len(shown_candidates)}, use --limit N or --all for more" if truncated else ""
    )
    lines = [f"NEEDS ATTENTION ({total_candidates} of {total_runs} runs{note})"]
    if not shown_candidates and not total_candidates:  # P12: never a bare header
        lines.append("  nothing needs attention")
    for candidate in shown_candidates:
        run = candidate["run"]
        lines.append(
            f"  {run.run_id}  {candidate['age']}  {candidate['verdict']}  "
            f"{redactor(candidate['reason'])}"
        )
        lines.append(f"    -> {redactor(candidate['command'])}")
    if total_live_now:
        shown_live = live_now or []
        live_note = (
            f"; showing {len(shown_live)} of {total_live_now}, use --limit N or --all for more"
            if len(shown_live) < total_live_now else ""
        )
        lines.append(f"LIVE NOW ({total_live_now} of {total_runs} runs{live_note})")
        for entry in shown_live:
            lines.append(
                f"  {entry['run'].run_id}  possibly live  {entry['verdict']}  "
                f"{redactor(entry['command'])}"
            )
    lines.append(_scan_triage_totals_line("LAST 7 DAYS", totals_7d))
    lines.append(_scan_triage_totals_line("ALL TIME", totals_all))
    lines.append("  full list: agent-postmortem scan --all")
    silenced_line = _silenced_detectors_line(silenced_detectors)
    if silenced_line is not None:
        lines.append(silenced_line)
    return "\n".join(lines).rstrip("\n") + "\n"


def _scan_row_line(row: dict, redactor: Redactor) -> str:
    # docs/DESIGN.md 4.7: id, date, project, kind, agents, attempts,
    # verdict, top detector, in that order.
    project = project_text(row.get("project", ""), redactor)
    kind = row.get("kind", "workflow")
    # docs/DESIGN.md 12's D-C: the verdict is left exactly as computed (a
    # real critical finding stays visible) with "unfinished" appended
    # after it, not substituted for it, when this run has not finished --
    # so `latest` can skip it, but a run can be both genuinely broken and
    # still in progress and the row says both.
    verdict = row.get("verdict", "")
    if row.get("finished") is False:
        verdict = f"{verdict} unfinished"
    elif row.get("finished") is None and row.get("unfinished_state") == "possibly running":
        # a session has no lifecycle; recent activity marks it as maybe still going, so `latest`
        # can skip it too (release review of 0.3.0)
        verdict = f"{verdict} possibly running"
    line = (
        f"{row.get('run_id', '')}  {row.get('date', '')}  {project}  {kind}  "
        f"{row.get('agents', 0)} agents  {row.get('attempts', 0)} attempts  "
        f"{verdict}  {row.get('top_detector') or '-'}"
    )
    return line


def _scan_totals_lines(totals: dict) -> list[str]:
    """docs/DESIGN.md 4.7: totals stratified by kind -- workflow runs, runs
    with retries, attempts, wasted hours; session runs, subagents,
    interrupted -- then findings per detector (each already counted only
    over the runs of the kinds that detector applies to, since a detector
    never fires on a run outside its own kinds)."""
    workflow = totals.get("workflow", {})
    session = totals.get("session", {})
    lines = [
        f"{workflow.get('runs', 0)} workflow runs, "
        f"{workflow.get('runs_with_retries', 0)} with retries, "
        f"{workflow.get('attempts', 0)} attempts, {workflow.get('wasted_hours', 0.0):.1f}h wasted",
        f"{session.get('runs', 0)} session runs, {session.get('subagents', 0)} subagents, "
        f"{session.get('interrupted', 0)} interrupted",
    ]
    findings_per_detector = totals.get("findings_per_detector", {})
    if findings_per_detector:  # P9: a finding is not an event
        lines.append("findings per detector (one finding can cover several events):")
    for detector_id in sorted(findings_per_detector):
        lines.append(f"  {detector_id}: {findings_per_detector[detector_id]}")
    return lines


def render_scan_text(
    rows: list[dict], totals: dict, redactor: Redactor, silenced_detectors: frozenset = frozenset(),
) -> str:
    # docs/DESIGN.md 5.1: every public renderer entry point gets a fresh
    # Pseudonyms map, even one like this that never prints a per-agent
    # name (scan lists runs, not agents) -- a stale mapping left over from
    # an earlier render must never leak into this one.
    redactor.names = Pseudonyms(enabled=redactor.enabled)
    lines = [_scan_row_line(row, redactor) for row in rows]
    lines.append("")
    lines.extend(_scan_totals_lines(totals))
    silenced_line = _silenced_detectors_line(silenced_detectors)
    if silenced_line is not None:
        lines.append(silenced_line)
    return "\n".join(lines).rstrip("\n") + "\n"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _key_digest(key: str | None) -> str | None:
    """sha256(key)[:8]: an opaque, one-way stand-in for a journal key
    (docs/DESIGN.md 5.3). Unconditional on the redactor: a workflow key
    often embeds the attempt's own label verbatim (e.g. "v2:silent-alpha"),
    so the export never carries the raw key, --no-redact or not."""
    if key is None:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def _export_ledger(attempt: Attempt, redactor: Redactor, view: "EvidenceView") -> dict:
    """docs/DESIGN.md 5.3b: `{completeness, reasons, entries}` on every
    attempt. `path` and `repo` are null when the entry never named one
    (never the string "UNKNOWN"), else `path-N` at share and the P19 answer
    at local; `line` per the `line` convention. An attempt the reader never
    gave a ledger (a hand-built one) is partial for the one reason that says
    no ledger could be built."""
    ledger = attempt.ledger
    if ledger is None:
        return {"completeness": "partial", "reasons": ["transcript_missing"], "entries": []}

    def shown(value: str | None) -> str | None:
        return redactor.names.path(value) if isinstance(value, str) and value else None

    return {
        "completeness": ledger.completeness,
        "reasons": list(ledger.reasons),
        "entries": [
            {
                "effect": entry.effect,
                "basis": entry.basis,
                "completion": entry.completion,
                "path": shown(entry.path),
                "repo": shown(entry.repo),
                "line": view.ledger_line(entry.line),
            }
            for entry in ledger.entries
        ],
    }


def _export_attempt(attempt: Attempt, redactor: Redactor, view: "EvidenceView") -> dict:
    """docs/DESIGN.md 5.3's Attempt allowlist. Calling redactor.names here
    (rather than the plain redactor()) both returns the pseudonym and
    registers it, so a finding title or evidence line rendered afterward
    with the same redactor picks up the same substitution via apply()."""
    interrupt = None
    if attempt.interrupt is not None:
        interrupt = {
            "cause": attempt.interrupt.cause,
            "silence_seconds": attempt.interrupt.silence_seconds,
        }
    agent_type = redactor.names.type(attempt.agent_type) if attempt.agent_type else None
    # docs/DESIGN.md 5.3: reported is not a structured name field (it never
    # embeds a label the way a title does), so it goes through the plain
    # Redactor via redact_value -- structural redaction only, same as an
    # evidence line or a metrics value. This is what redacts a report_path
    # claim (a path) without a pseudonym ever touching it.
    reported = redact_value(attempt.reported, redactor) if attempt.reported is not None else None
    # docs/DESIGN.md 5.3's v0.2 reader changes: facts is exported, tool_calls
    # is not (a command is private, never leaves the tool). facts holds
    # paths (paths_written, in_flight[].path, cwds) that go through the same
    # plain Redactor as any other path, exactly like `reported` above -- not
    # a structured name field, so never through Pseudonyms. Built from
    # AttemptFacts' own fields directly, the same way `interrupt` above is
    # built from Interrupt's fields, never through its private complete-dict
    # method (that form is for tests only -- report.py never calls it).
    facts = None
    if attempt.facts is not None:
        facts = redact_value(
            {
                "paths_written": attempt.facts.paths_written,
                "in_flight": attempt.facts.in_flight,
                "commits": attempt.facts.commits,
                "commit_commands_without_id": attempt.facts.commit_commands_without_id,
                "cwds": attempt.facts.cwds,
                "background_unfinished": attempt.facts.background_unfinished,
                "last_tool": attempt.facts.last_tool,
                "seconds_since_last_tool_at_end": attempt.facts.seconds_since_last_tool_at_end,
            },
            redactor,
        )
    return {
        "agent_id": identity_value("agent", attempt.agent_id, redactor),
        "key": _key_digest(attempt.key),
        "label": redactor.names.agent(attempt.label),
        "phase": redactor.names.phase(attempt.phase),
        "agent_type": agent_type,
        "ordinal": attempt.ordinal,
        "outcome": attempt.outcome,
        "first_ts": _iso(attempt.first_ts),
        "last_ts": _iso(attempt.last_ts),
        "usage": attempt.usage,
        "event_count": len(attempt.events),
        "interrupt": interrupt,
        "reported": reported,
        "facts": facts,
        "ledger": _export_ledger(attempt, redactor, view),
    }


def _export_finding(
    finding: Finding, redactor: Redactor, silenced_detectors: frozenset,
    view: "EvidenceView | None" = None,
) -> dict:
    """docs/DESIGN.md 5.3: 'all fields, evidence redacted'. title goes
    through redactor.title() (structural redact() plus pseudonym
    substitution: a title routinely embeds a label verbatim, since a
    detector formats it straight into a sentence). evidence and metrics go
    through the plain redactor only -- structural redact(), never a
    pseudonym substitution: docs/DESIGN.md 5.2 keeps evidence (and a
    metrics value that can carry raw text of its own, e.g.
    bash_backgrounded's commands) a faithful quote of transcript text.
    rule_ids (unit M1d) carries no user text, so it needs no redaction, the
    same as rule_id.

    `silenced` (unit M3c, docs/DESIGN.md 4.10): true when this finding's
    detector is named in [detectors].off in the user's config file. A
    silenced finding is never dropped from --json -- only from text
    rendering and hook notification -- so this bool is what lets a JSON
    consumer tell the two apart.

    Schema 3 (N5a-1, docs/DESIGN.md 5.3b): `agent_id` is the alias, and
    `evidence` is the text renderer's evidence view (pointers with aliased
    file names at share, excerpts only at local and raw with --evidence)."""
    return {
        "detector": finding.detector,
        "severity": finding.severity,
        "title": _finding_title(finding, redactor),
        "run_id": finding.run_id,
        "agent_id": identity_value("agent", finding.agent_id, redactor),
        "evidence": _shown_evidence(finding.evidence, view, redactor),
        "metrics": redact_value(finding.metrics, redactor),
        "rule_id": finding.rule_id,
        "rule_ids": list(finding.rule_ids),
        "finding_id": finding.finding_id,
        "silenced": finding.detector in silenced_detectors,
    }


def export_observation_groups(
    observation, redactor: Redactor, *, omit_empty_unknown: bool = False,
) -> list[dict]:
    """docs/DESIGN.md 5.3b (N5c-1, plan S6): the group objects, exactly `group`
    (the index in this list), `repo`, `refused`, `checked`, `reason`, `paths`, and
    `unobserved_paths`. `repo`, every `paths` key, and every `unobserved_paths`
    element use the path namespace of the level; `repo_root`, `head`, and
    `git_version` are internal and never exported. `omit_empty_unknown` leaves the
    unknown group out when it names no path (rescue plan, S7)."""
    exported: list[dict] = []
    for group in observation.groups:
        if omit_empty_unknown and group.repo is None and not group.unobserved_paths:
            continue
        paths = {}
        for path, path_observation in group.paths.items():
            paths[redactor.names.path(path)] = {
                "state": path_observation.state,
                "reason": (
                    redactor(path_observation.reason)
                    if path_observation.reason is not None else None
                ),
                "later_write_observed": path_observation.later_write_observed,
                "attempts": [
                    {
                        **attempt,
                        "agent_id": (
                            attempt["agent_id"] if attempt.get("agent_id") == "main"
                            else identity_value("agent", attempt.get("agent_id"), redactor)
                        ),
                    }
                    for attempt in path_observation.attempts
                ],
            }
        exported.append({
            "group": len(exported),
            "repo": redactor.names.path(group.repo) if group.repo is not None else None,
            "refused": group.refused,
            "checked": group.checked,
            "reason": redactor(group.reason) if group.reason is not None else None,
            "paths": paths,
            "unobserved_paths": [redactor.names.path(path) for path in group.unobserved_paths],
        })
    return exported


def _export_observation(observation, redactor: Redactor) -> dict:
    """docs/DESIGN.md 5.3b: `observation` is `{groups: [...]}`."""
    return {"groups": export_observation_groups(observation, redactor)}


def _diagnostic_for_export(diagnostic: str, redactor: Redactor) -> str:
    """One Run.diagnostics entry for the JSON export (N3b P10b, D-h). Only a
    raw sink (the redactor is off) asks for the raw form, and it does so
    before the redactor sees the text; every other level gets the diagnostic's
    own plain string, which never carries a journal key."""
    if not redactor.enabled:
        return redactor(raw_diagnostic(diagnostic))
    return redactor(str(diagnostic))


def _export_lifecycle(run: Run) -> dict | None:
    """docs/DESIGN.md 5.3b: the four public Lifecycle fields; null for a
    session (no lifecycle model to report, D11)."""
    lifecycle = run.lifecycle
    if lifecycle is None:
        return None
    return {
        "terminal": lifecycle.terminal,
        "source": lifecycle.source,
        "activity": lifecycle.activity,
        "generations": lifecycle.generations,
    }


def export_run(
    run: Run,
    findings: list[Finding],
    redactor: Redactor,
    observation=None,
    silenced_detectors: frozenset = frozenset(),
    *,
    evidence: bool = False,
) -> dict:
    """The JSON export allowlist (docs/DESIGN.md 5.3): {run, findings}, with
    run holding exactly run_id, kind, status, claude_version, started_at,
    ended_at, counts, diagnostics, and attempts (each attempt allowlisted by
    _export_attempt). No meta, no cwd, no branch, no event list, no
    transcript text -- and this never reaches for a dataclass's own private,
    complete dict form (that form exists for tests only).

    _register_names runs first (docs/DESIGN.md 5.1): a fresh Pseudonyms map
    registered with every attempt's label/meta description/phase/agent_type
    before either attempts or findings are exported, so a finding title
    that embeds one of those verbatim (an ordinary detector does exactly
    that) is pseudonymized correctly regardless of which findings happen to
    reference which attempt.

    `silenced_detectors` (unit M3c, docs/DESIGN.md 4.10) never removes a
    finding from `findings` here -- every finding a silenced detector fired
    still exports, with `silenced: true` (5.3) -- callers that must not
    show a silenced finding (the text renderers, the verdict, stories,
    next) filter beforehand with config.partition_silenced() instead.
    """
    _register_names(run, redactor, detailed=True)
    _register_facts_paths(run, redactor)
    _register_ledger_paths(run, redactor)
    _register_observation_paths(observation, redactor, run)
    view = EvidenceView(run, redactor, show_excerpts=evidence)
    attempts = [_export_attempt(attempt, redactor, view) for attempt in run.attempts]
    exported_findings = [
        _export_finding(finding, redactor, silenced_detectors, view) for finding in findings
    ]
    interrupted = sum(1 for attempt in run.attempts if attempt.outcome == "killed")

    payload = {
        "run": {
            "run_id": run.run_id,
            "kind": run.kind,
            "status": run.status,
            "claude_version": run.claude_version,
            "started_at": _iso(run.started_at),
            "ended_at": _iso(run.ended_at),
            "counts": {
                "agents": len(run.agents),
                "attempts": len(run.attempts),
                "interrupted": interrupted,
            },
            "diagnostics": [_diagnostic_for_export(d, redactor) for d in run.diagnostics],
            "attempts": attempts,
            "lifecycle": _export_lifecycle(run),
        },
        "findings": exported_findings,
    }
    # docs/DESIGN.md 4.9 rule 10: the key is present at all only when the
    # caller actually ran --observe; without it, JSON output is unchanged.
    if observation is not None:
        payload["observation"] = _export_observation(observation, redactor)
    return payload


def _export_story(entry: dict, redactor: Redactor) -> dict:
    """docs/DESIGN.md 4.7 rule 9/5.3: {agent, severity, text, finding_ids}.
    agent goes through the same Pseudonyms.agent() the AGENT table and text
    STORIES section use (the literal 'run' label for a run-level story is
    never a user-authored name and is left alone); text through
    redactor.title() (5.1/5.2: a story sentence routinely embeds a label
    verbatim, the same reason Finding.title does)."""
    agent = "run" if entry["agent"] == "run" else redactor.names.agent(entry["agent"])
    return {
        "agent": agent,
        "severity": entry["severity"],
        "text": _render_pieces(entry["pieces"], redactor),
        "finding_ids": list(entry["finding_ids"]),
    }


def _export_next(item: dict, redactor: Redactor) -> dict:
    """docs/DESIGN.md 4.7 rule 9/5.3: {grade, text, command}. Neither field
    is a structured name field (a command names no user, only a run id, and
    'text' is one of this module's own fixed phrases), so both go through
    the plain Redactor exactly like a metrics value -- structural redact()
    only."""
    return {
        "grade": item["grade"],
        "text": redactor(item["text"]),
        "command": redactor(item["command"]) if item["command"] else None,
    }


SCHEMA_VERSION = 3


def to_json(
    run: Run,
    findings: list[Finding],
    redactor: Redactor | None = None,
    observation=None,
    silenced_detectors: frozenset = frozenset(),
    *,
    evidence: bool = False,
) -> dict:
    """docs/DESIGN.md 4.7/5.3b: {schema, disclosure, run, findings, rules,
    verdict, status, stories, next}, run and findings built by export_run (5.3's
    allowlist), rules and verdict derived the same way the text report
    derives them, status duplicated at the top level for a consumer that
    does not want to dig into run. observation (v0.2, unit M2b) is forwarded
    to export_run unchanged and is absent from the payload entirely unless
    the caller passed one.

    stories/next (unit M3a) are the same records render_run_text derives,
    exported through the allowlist of docs/DESIGN.md 4.7 rule 9/5.3 rather
    than report.py's internal, wrapped/truncated text-rendering shape:
    stories always computed with verbose=False, so an info-only agent never
    backs a JSON story either, same as the default text report.

    `silenced_detectors` (unit M3c, docs/DESIGN.md 4.10): `findings` itself
    is exported whole -- every finding, silenced or not, each getting its
    own `silenced` bool (5.3) -- but `verdict`, `rules`, `stories`, and
    `next` are all derived from the shown (non-silenced) findings only, the
    same rule the text report follows (docs/DESIGN.md 4.6/4.7): a silenced
    detector must never flip the verdict, suggest its own rule, or narrate
    its own story, even though its raw finding is still right there in
    `findings` for a caller that wants it.

    Defaults to redaction on (docs/DESIGN.md 5.2/5.3): a caller that wants
    raw output must pass an explicit Redactor(enabled=False), never get it
    by omission."""
    redactor = redactor or Redactor(enabled=True)
    payload = export_run(
        run, findings, redactor, observation, silenced_detectors, evidence=evidence,
    )
    payload["schema"] = SCHEMA_VERSION
    payload["disclosure"] = redactor.level
    shown = [finding for finding in findings if finding.detector not in silenced_detectors]
    payload["rules"] = [{"rule_id": rule.id, "count": count} for rule, count in rules_for(shown)]
    payload["verdict"] = verdict_of(run, shown)
    payload["status"] = run.status

    # export_run already ran _register_names/_register_observation_paths, so
    # every pseudonym _export_story below needs is already registered.
    story_entries = _story_records(run, shown, verbose=False)
    payload["stories"] = [_export_story(entry, redactor) for entry in story_entries]

    next_items = _what_to_do_items(run, shown, observation, observation is not None)
    payload["next"] = [_export_next(item, redactor) for item in next_items]
    return payload


def _bug_title(run: Run, findings: list[Finding], redactor: Redactor) -> str:
    verdict = verdict_of(run, findings)
    summary = redactor(_summary_line_for(run))
    return f"# {redactor(run.run_id)} ({verdict}): {summary}"


def _bug_environment_lines(run: Run) -> list[str]:
    return [
        "## Environment",
        f"- Claude Code version: {run.claude_version or 'unknown'}",
        f"- OS: {platform.platform()}",
    ]


def _bug_what_happened_lines(run: Run, findings: list[Finding], redactor: Redactor) -> list[str]:
    counts = {severity: 0 for severity in _SEVERITIES}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return [
        "## What happened",
        f"{verdict_of(run, findings)}: {redactor(_summary_line_for(run))}",
        "",
        f"- agents: {len(run.agents)}",
        f"- attempts: {len(run.attempts)}",
        f"- findings: critical {counts['critical']}, major {counts['major']}, "
        f"minor {counts['minor']}, info {counts['info']}",
    ]


def _bug_project_text(run: Run, redactor: Redactor) -> str:
    """P10/P11: the project at share is project:<alias>; at local and raw the
    project directory name as it is on disk."""
    return project_text(run.project_slug, redactor)


def _bug_reproduction_lines(run: Run, redactor: Redactor) -> list[str]:
    lines = [
        "## Reproduction",
        f"project {_bug_project_text(run, redactor)}, {len(run.agents)} logical agent(s):",
    ]
    for agent in run.agents:
        attempts = sorted(agent.attempts, key=lambda a: a.ordinal)
        models = sorted({attempt.agent_type for attempt in attempts if attempt.agent_type})
        pseudo_models = sorted({redactor.names.type(model) for model in models})
        model_str = ", ".join(pseudo_models) if pseudo_models else "unknown model"
        label = redactor.names.agent(agent.label)
        phase = redactor.names.phase(agent.phase)
        lines.append(redactor.scrub_local_line(
            f"- {label} (phase {phase}, {model_str}): {len(attempts)} attempt(s)"
        ))
    return lines


def _bug_evidence_lines(
    run: Run, findings: list[Finding], redactor: Redactor, view: "EvidenceView | None" = None,
    evidence_flag: bool = False,
) -> list[str]:
    lines = ["## Evidence", f"From run {redactor(run.run_id)}:"]
    if evidence_flag and view is not None and view.note() is not None:
        lines.append(view.note())
    if not findings:
        lines.append("(no findings)")
        return lines
    for finding in findings:
        lines.append(
            f"- [{finding.severity}] {finding.detector}: {_finding_title(finding, redactor)}"
        )
        for shown in _shown_evidence(finding.evidence, view, redactor):
            lines.append(f"  - {shown}")
    return lines


def _bug_suggested_fixes_lines(findings: list[Finding]) -> list[str]:
    # See _suggested_rules_section: counts a finding once per rule in
    # rule_ids, not just its first rule_id, so a finding backing more than
    # one rule is never silently dropped from every rule but its first.
    lines = ["## Suggested fixes"]
    rule_ids = sorted({rule_id for finding in findings for rule_id in finding.rule_ids})
    detector_ids_without_rule = sorted(
        {
            finding.detector
            for finding in findings
            if not finding.rule_ids and finding.detector in _DETECTOR_BUG_SENTENCES
        }
    )
    if not rule_ids and not detector_ids_without_rule:
        lines.append("(no suggested fix in this build)")
        return lines
    for rule_id in rule_ids:
        rule = RULES.get(rule_id)
        if rule is None:
            continue
        count = sum(1 for finding in findings if rule_id in finding.rule_ids)
        plural = "" if count == 1 else "s"
        lines.append(f"- {rule_id} (backed by {count} finding{plural}): {rule.text}")
    for detector_id in detector_ids_without_rule:
        lines.append(f"- {detector_id}: {_DETECTOR_BUG_SENTENCES[detector_id]}")
    return lines


# docs/DESIGN.md 5.2's D2, made true per level by N3b P16: the closing note
# says what the report above actually carries.
_BUG_REPORT_FOOTERS = {
    "share": (
        "Redacted: paths, ids, emails, account names, agent names. "
        "Evidence lines are pointers and marker codes only; no transcript excerpts are included."
    ),
    "local": (
        "Local level: real names, ids, and the project directory name are shown; "
        "paths are repo-relative or numbered. {evidence} Do not paste this report."
    ),
    "raw": (
        "Raw level: real names, ids, and paths are shown; nothing is redacted. "
        "{evidence} Do not paste this report."
    ),
}
_BUG_EVIDENCE_WITH = "Evidence lines include transcript excerpts (--evidence); read them first."
_BUG_EVIDENCE_WITHOUT = (
    "Evidence lines are pointers and marker codes only; add --evidence for transcript excerpts."
)


def _bug_footer(level: str, evidence: bool) -> str:
    template = _BUG_REPORT_FOOTERS[level]
    if level == "share":
        return template
    return template.format(evidence=_BUG_EVIDENCE_WITH if evidence else _BUG_EVIDENCE_WITHOUT)


def render_bug_report(
    run: Run, findings: list[Finding], redactor: Redactor, evidence: bool = False,
) -> str:
    """Markdown bug report: Title, Environment, What happened, Reproduction,
    Evidence, Suggested fixes, in that order (docs/DESIGN.md section 4.4),
    then a footer (5.2's D2) spelling out what this level prints. `evidence`
    is --evidence: at local and raw it adds transcript excerpts to the
    pointers, at share it earns the one note line (P16)."""
    _register_names(run, redactor, detailed=True)
    view = EvidenceView(run, redactor, show_excerpts=evidence)
    lines = [_bug_title(run, findings, redactor), ""]
    lines.extend(_bug_environment_lines(run))
    lines.append("")
    lines.extend(_bug_what_happened_lines(run, findings, redactor))
    lines.append("")
    lines.extend(_bug_reproduction_lines(run, redactor))
    lines.append("")
    lines.extend(_bug_evidence_lines(run, findings, redactor, view, evidence))
    lines.append("")
    lines.extend(_bug_suggested_fixes_lines(findings))
    lines.append("")
    lines.append(_bug_footer(redactor.level, evidence))
    return "\n".join(lines).rstrip("\n") + "\n"
