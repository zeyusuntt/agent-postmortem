"""`hook stop-watch`: tells a session that has gone idle (docs/DESIGN.md 4.14.5; field reports
2026-11-04, Zeyu's direction).

The per-call hook needs a tool call or a prompt. The manager in the field report was idle all
night, so a worker's permission prompt would have waited for morning there too. This is a Stop
hook with `asyncRewake: true`: Claude Code runs it in the background, the session goes on, and
its exit 2 wakes an idle session with its stderr as a system reminder. Nothing is armed by
anyone; the next Stop starts the next watcher.

It starts and leaves at once unless the session has something to watch: a worker of its own that
can still change, or a background command that can still go silent. Then every 20 s, stats only:
the same job files, delivery claims and words as the per-call hook, so the two never tell a thing
twice.

The worst failure here is money, a session woken over and over, so the rules are the opposite of
the per-call hook's "a repeat beats a lost line": a line whose claim could not be recorded wakes
nothing, a session is woken at most once per 5 minutes, any error exits 0 and never 2,
`AGENT_POSTMORTEM_WAKE=0` turns it off, and a Claude Code older than the first with `asyncRewake`
(where this would run as a blocking Stop hook) or of unknown version exits at once.

It ends when the session acts again (a user or assistant entry newer than its start: a late flush
of the final message does not count), its parent is gone, a newer watcher owns the session,
nothing is left to watch, or after 14 days, with one last wake saying so. A reboot loses it until
the next Stop; the per-call hook does not depend on it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

from . import hook_gate, state, workers

POLL_SECONDS = 20.0
# Set by the run.sh of a plugin whose hooks.json starts this watcher. An older plugin's run.sh sets
# nothing, so a newer CLI behind it knows no watcher runs (0.5.0 review)
PLUGIN_MARK = "AGENT_POSTMORTEM_WATCHER_HOOK"
WAKE_COOLDOWN_SECONDS = 300.0
DELIVERED_GRACE_SECONDS = 120.0  # the worker's own message usually wakes the manager first
# hooks.json gives it 14 days (Claude Code enforces `timeout` on an asyncRewake hook, and it must
# stay under 2^31 ms); the watcher leaves an hour before that, on its own terms
DEADLINE_SECONDS = 1209600.0 - 3600.0
OLD_TOLD_SECONDS = 72 * 3600.0
MIN_VERSION = (2, 1, 314)
_VERSION_TAIL = 1 << 16
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?")
_PREFIX = "agent-postmortem, while you were idle (not an error):"
_BUDGET = 400
_DEADLINE_KEY = "watch:deadline"


def enabled(main_path: str | Path) -> bool:
    """The watcher can run for this session: not turned off, and a Claude Code that has
    `asyncRewake`, by the version its transcript records."""
    return os.environ.get("AGENT_POSTMORTEM_WAKE") != "0" and _version_ok(str(main_path))


def _version_ok(main_path: str) -> bool:
    version = transcript_version(main_path)
    return version is not None and version >= MIN_VERSION


def transcript_version(main_path: str) -> tuple[int, ...] | None:
    """The Claude Code version the transcript's last entry that has one records, or None."""
    try:
        size = os.stat(main_path).st_size
        with Path(main_path).open("rb") as handle:
            handle.seek(max(0, size - _VERSION_TAIL))
            tail = handle.read(_VERSION_TAIL)
    except OSError:
        return None
    for raw in reversed(tail.split(b"\n")):
        if b'"version"' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        version = entry.get("version") if isinstance(entry, dict) else None
        if isinstance(version, str):  # the entry's own: a tool's input can hold one too
            found = _VERSION_RE.fullmatch(version)
            return tuple(map(int, found.groups())) if found is not None else None
    return None


def _worth_starting(payload: dict, gate: dict | None) -> bool:
    tasks = payload.get("background_tasks")
    if isinstance(tasks, list) and any(
            isinstance(task, dict) and task.get("type") == "shell"
            and task.get("status") in (None, "running") for task in tasks):
        return True
    table = gate.get("workers") if gate else None
    return isinstance(table, dict) and any(
        isinstance(row, list) and len(row) == 3 and row[2] != "-" for row in table.values())


class _Watch:
    def __init__(self, main_path: Path, clock) -> None:
        from . import hooks_cli, live, stalls, status  # heavy: only with something to watch

        self.hooks_cli, self.live, self.stalls, self.status = hooks_cli, live, stalls, status
        self.main_path = main_path
        self.clock = clock
        self.started = clock()
        self.token = secrets.token_hex(8)
        self.follower = live.SessionFollower(main_path, caller_session=None)
        self.store_path = self.follower.store_path
        status._restore(self.follower, state.read_cache(self.store_path))
        self.offset = self._size()
        self.follower._read()
        status._save(self.follower, {}, self.started)
        gate = hook_gate._read_gate(hook_gate._gate_path(str(main_path))) or {}
        self.table = gate.get("workers") if isinstance(gate.get("workers"), dict) else {}
        self.observed: list[dict] = []
        self.check_next = True
        self.recheck_at: float | None = None
        self.stall_turn = gate.get("stall_turn") if isinstance(gate.get("stall_turn"), int) else 0
        self.lost = False
        kinds = stalls.live_sessions(stalls.default_sessions_root(main_path))
        self.background_session = kinds.get(main_path.stem) == "bg"

    def _size(self) -> int:
        try:
            return os.stat(self.main_path).st_size
        except OSError:
            return 0

    def take_ownership(self) -> bool:
        """False when a watcher that started later already owns the session: this one was slow to
        get here, and taking its place would end both (0.5.0 review)."""
        try:
            with state.cache_transaction(self.store_path) as data:
                record = self.live._store_session(data, self.live.session_key(self.main_path))
                owner = record.get("watcher")
                start = owner.get("start") if isinstance(owner, dict) else None
                if isinstance(start, (int, float)) and not isinstance(start, bool) \
                        and start > self.started:
                    return False
                record["watcher"] = {"token": self.token, "start": self.started}
                record["seen"] = self.started
        except state.StateError:
            pass  # a claim needs the store too, so nothing wakes while it cannot be used
        return True

    def active_again(self) -> bool:
        """A user or assistant entry with a timestamp after this watcher's start. A final message
        flushed late carries its own earlier time, and a line still being written is read whole
        at the next poll."""
        size = self._size()
        if size <= self.offset:
            return False
        try:
            with self.main_path.open("rb") as handle:
                handle.seek(self.offset)
                appended = handle.read(size - self.offset)
        except OSError:
            return False
        end = appended.rfind(b"\n")
        if end < 0:
            return False
        self.offset += end + 1
        for raw in appended[:end].splitlines():
            if b'"timestamp"' not in raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("type") not in ("user", "assistant"):
                continue
            when = workers._instant(entry.get("timestamp"))
            if when is not None and when > self.started:
                return True
        return False

    def poll(self) -> int | None:
        """One look: 2 to wake (the line is on stderr and claimed), 0 to leave, None to go on."""
        now = self.clock()
        cache = state.read_cache(self.store_path)
        sessions = cache.get("sessions") if isinstance(cache.get("sessions"), dict) else {}
        record = sessions.get(self.live.session_key(self.main_path))
        record = record if isinstance(record, dict) else {}
        if self._another_owner(record):
            return 0
        changed, fresh = hook_gate._worker_changes(str(self.main_path), {"workers": self.table})
        if fresh is not None:
            self.table = fresh
        if changed or self.check_next or (self.recheck_at is not None and now >= self.recheck_at):
            check = self.hooks_cli._worker_check(self.follower, {}, cache, time.monotonic(),
                                                 previous=self.table, stall_turn=self.stall_turn,
                                                 stall_any_size=True)
            self.table = check.table
            self.observed = check.observed
            self.recheck_at = check.recheck_at
            self.stall_turn = check.stall_turn
            self.check_next = False
        # a job removed since it was read has nothing left to say
        self.observed = [w for w in self.observed if not workers.job_gone(
            workers.job_file(self.follower.jobs_root, w["short"]))]
        told = self.hooks_cli._told_workers(record)
        lines, quiet = workers.due(told, self.observed, self.hooks_cli._finishes(record))
        # an ordinary finish wakes at most once per worker since its last trouble; a later one
        # waits for the session's next call, so a worker on a cron cannot wake it every hour
        lines = [w for w in lines if not w.get("finishes")]
        fresh_lines = [w for w in lines if _worth_a_wake(w)]
        held = [w for w in lines if not _worth_a_wake(w)]
        reported = self.live.key_set(record.get("reported"))
        commands, watched_commands = self._commands(now, reported)
        if not fresh_lines and not commands and not held and not quiet \
                and not self._worth_watching(told, now) and not watched_commands:
            return 0
        if now - self.started >= DEADLINE_SECONDS:
            return self._last_wake(now)
        if held:
            self.check_next = True  # the grace runs on the clock, with no file changing
        last = record.get("last_wake")
        cooling = isinstance(last, (int, float)) and now - last < WAKE_COOLDOWN_SECONDS
        if quiet or ((fresh_lines or commands) and not cooling):
            return self._claim(now, commands)
        return None

    def _another_owner(self, record: dict) -> bool:
        owner = record.get("watcher")
        return isinstance(owner, dict) and owner.get("token") != self.token

    def _commands(self, now: float, reported: set[str]) -> tuple[list[tuple[dict, str]], bool]:
        """(the silent commands worth a wake, whether any untold command can still go silent). A
        command's silence wakes only a background session, since a server in an interactive one
        is silent by design; a wait on stdin wakes any."""
        wake: list[tuple[dict, str]] = []
        watching = False
        for command in self.stalls.running_commands(self.follower.main, now):
            key = self.hooks_cli._command_key(command)
            if key in reported:
                continue
            if command["silent"] and (command["waiting_on_stdin"] or self.background_session):
                wake.append((command, key))
            elif command["recheck_seconds"] is not None:
                watching = True
        return wake, watching

    def _worth_watching(self, told: dict[str, str], now: float) -> bool:
        for short, row in self.table.items():
            if not (isinstance(row, list) and len(row) == 3):
                continue
            identity = str(row[2])
            if not identity.startswith(("working/", "blocked/")) or workers.job_gone(
                    workers.job_file(self.follower.jobs_root, short)):
                continue  # settled, or its job was removed
            if identity.startswith("working/"):
                return True
            if identity.startswith("blocked/"):
                old = now - row[0] / 1e9 > OLD_TOLD_SECONDS and told.get(short) == identity
                if not old:
                    return True
        return False

    def _claim(self, now: float, commands: list[tuple[dict, str]]) -> int | None:
        decision: dict = {"code": None}
        lines: list[dict] = []
        quiet: dict[str, str] = {}
        shown: list[dict] = []
        dropped: list[str] = []

        def before(record: dict) -> None:
            decision["owner_lost"] = self._another_owner(record)
            last = record.get("last_wake")
            decision["cooling"] = isinstance(last, (int, float)) \
                and now - last < WAKE_COOLDOWN_SECONDS
            # decided again inside the lock: the per-call hook may have told it since the poll
            due, silent = workers.due(self.hooks_cli._told_workers(record), self.observed,
                                      self.hooks_cli._finishes(record))
            lines.extend(w for w in due if not w.get("finishes") and _worth_a_wake(w))
            quiet.update(silent)

        def then(won: list[str]) -> str | None:
            mine = [(c, key) for c, key in commands if key in won]
            if decision["owner_lost"] or decision["cooling"]:
                dropped.extend(won)
                return None
            parts = [_PREFIX]
            for worker in sorted(lines, key=lambda w: w["class"] not in workers.TROUBLE):
                line = workers.line(worker)
                if len("\n".join([*parts, line])) > _BUDGET:
                    break
                parts.append(line)
                shown.append(worker)
            for command, key in mine:
                line = self.hooks_cli._command_line(command)
                if len("\n".join([*parts, line])) > _BUDGET:
                    dropped.append(key)
                    continue
                parts.append(line)
            if len(parts) == 1:
                dropped.extend(won)
                return None
            text = "\n".join(parts)
            sys.stderr.write(text + "\n")  # before the commit: a kill here repeats, loses nothing
            sys.stderr.flush()
            decision["code"] = 2
            return text

        def after(record: dict) -> None:
            if decision["owner_lost"]:
                return
            workers.save_told(record, self.table, quiet, shown)
            record["seen"] = now
            if decision["code"] == 2:
                record["last_wake"] = now

        keys = [key for _command, key in commands]
        _won, text = self.live.claim_deliveries(
            self.main_path, keys, self.store_path, then=then, unclaim=lambda: dropped,
            strict=True, before=before, after=after)
        if decision.get("owner_lost"):
            return 0
        if decision["code"] == 2 and not text:
            return 0  # stderr went out but its claim failed to commit: no record, so no wake
        self._share_with_the_gate()
        return decision["code"]

    def _share_with_the_gate(self) -> None:
        """The per-call gate compares a job with what the last check saw, and this watcher is that
        check now: else the gate reads `working` as nothing new after this told `blocked`, the
        store never sees it, and the same prompt coming back is taken for the one already told."""
        path = hook_gate._gate_path(str(self.main_path))
        gate = hook_gate._read_gate(path)
        if gate is not None:
            hook_gate._write_gate(path, {**gate, "workers": self.table, "pending": True,
                                         "stall_turn": self.stall_turn})

    def _last_wake(self, now: float) -> int | None:
        """At the deadline, with work still out: one wake that says so, under the same rules as any
        other. Ending the turn starts the next watcher, which says so again at its own."""
        text = (f"{_PREFIX}\nagent-postmortem stopped watching this session's workers after 14 "
                "days. They may still be out: agent-postmortem status. End your turn to renew it.")
        decision: dict = {}
        dropped: list[str] = []

        def before(record: dict) -> None:
            decision["owner_lost"] = self._another_owner(record)
            last = record.get("last_wake")
            decision["cooling"] = isinstance(last, (int, float)) \
                and now - last < WAKE_COOLDOWN_SECONDS

        def then(won: list[str]) -> str | None:
            if decision["owner_lost"] or decision["cooling"] or not won:
                dropped.extend(won)
                return None
            sys.stderr.write(text + "\n")
            sys.stderr.flush()
            decision["told"] = True
            return text

        def after(record: dict) -> None:
            if decision.get("told"):
                record["last_wake"] = now

        _won, shown = self.live.claim_deliveries(
            self.main_path, [f"{_DEADLINE_KEY}:{self.token}"], self.store_path, then=then,
            unclaim=lambda: dropped, strict=True, before=before, after=after)
        if shown:
            return 2
        if "owner_lost" not in decision:
            return None  # the store could not be used: tried again at the next poll
        return None if decision["cooling"] and not decision["owner_lost"] else 0


def _worth_a_wake(worker: dict) -> bool:
    if worker["class"] in workers.TROUBLE:
        return True
    return worker["age"] >= DELIVERED_GRACE_SECONDS


def watch(payload: dict, *, clock=time.time, sleep=time.sleep, parent=os.getppid) -> int:
    """The hook's whole life. Returns its exit code."""
    if payload.get("hook_event_name") != "Stop" or "agent_id" in payload:
        return 0
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript.endswith(".jsonl"):
        return 0
    gate = hook_gate._read_gate(hook_gate._gate_path(transcript))
    if not _worth_starting(payload, gate) or not enabled(transcript):
        return 0
    the_parent = parent()
    if the_parent <= 1:
        return 0  # orphaned already
    session = _Watch(Path(transcript), clock)
    if not session.take_ownership():
        return 0
    while True:
        if parent() != the_parent or session.active_again():
            return 0
        code = session.poll()
        if code is not None:
            return code
        sleep(POLL_SECONDS)


def run() -> int:
    """Any error exits 0: an exit 2 wakes the session, and only a claimed line may."""
    try:
        payload = json.loads(sys.stdin.read() or "null")
        if not isinstance(payload, dict):
            return 0
        return watch(payload)
    except Exception:
        if os.environ.get("AGENT_POSTMORTEM_DEBUG"):
            import traceback

            from .redact import Redactor

            print(Redactor().scrub_leftover_paths(traceback.format_exc()), file=sys.stderr)
        return 0
