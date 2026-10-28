"""The return state of a session subagent (docs/DESIGN.md 4.1, 4.13.1 "Return evidence", G-A).

A pure decision over what the parent side recorded: the launcher transcript's launches and their
results, every inspected transcript's SendMessage requests (resume rounds), and the main
transcript's accepted task notifications. The reader gathers the facts while it streams a
transcript (`ReturnFacts`, filled by reader._LaunchLog and by the main transcript's ParentIndex)
and calls `decide`; nothing here touches the filesystem, and nothing reads a modification time.
Standard library only.

Truth table T0 to T5 (4.13.1). T0: only the measured shapes are recognized, every other presence
or type is `unknown`/`malformed`. Whatever the table does not assign is `unknown`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ABSENT = object()

AGENT_LAUNCH_TOOLS = ("Agent", "Task")
SEND_TOOL = "SendMessage"
RECORDED_TOOLS = (*AGENT_LAUNCH_TOOLS, SEND_TOOL)
_TERMINAL_WORDS = frozenset({"completed", "failed", "killed", "stopped", "cancelled"})
_STOPPED_WORDS = frozenset({"killed", "stopped", "cancelled"})
QUEUED_PREFIX = "Message queued for delivery"

RETURNED = "returned"
REPORTED_FAILED = "reported_failed"
REPORTED_KILLED = "reported_killed"
PENDING = "pending"
UNKNOWN = "unknown"


def _id_key(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True)
    except (TypeError, ValueError):
        return repr(value)


@dataclass(frozen=True)
class UseInfo:
    """One assistant `tool_use` block of a launch or resume tool."""

    name: str | None
    line: int
    ts: datetime | None
    to: object = ABSENT
    recipient: object = ABSENT


@dataclass(frozen=True)
class ResultInfo:
    """One `tool_result` block and the entry's `toolUseResult`, for the tools above."""

    line: int
    digest: str | None
    is_error: bool
    tur_kind: str  # absent | object | string | other
    agent_ids: tuple[str, ...]
    bad_id: bool  # an agentId is present and not a string
    status: object = ABSENT
    is_async: object = ABSENT
    resumed: object = ABSENT
    success: object = ABSENT
    message: object = ABSENT


@dataclass(frozen=True)
class Note:
    """An accepted main-transcript task notification (4.13.3)."""

    line: int
    task_id: str
    tool_use_id: str
    status: str
    ts: datetime | None = None


@dataclass
class ReturnFacts:
    """What one transcript recorded about launches and resume requests."""

    path: Path | None = None
    unreadable: bool = False
    uses: dict[str, list[UseInfo]] = field(default_factory=dict)
    results: dict[str, list[ResultInfo]] = field(default_factory=dict)
    use_counts: dict[str, int] = field(default_factory=dict)
    # GA-4 (4.13.1 T0): an Agent, Task or SendMessage `tool_use` whose id is not a string cannot
    # key a launch or a request. `bad_named` is the bounded fact kept for it: every string named
    # by its `to` or `recipient`, and every string agent id a result with the same non-string
    # `tool_use_id` carries. `bad_links` keys those ids (as JSON text) to let such a result in.
    bad_named: set[str] = field(default_factory=set)
    bad_links: dict[str, str] = field(default_factory=dict)

    def note_use(
        self, name: str | None, tool_id: object, line: int, ts: datetime | None, tool_input: object,
    ) -> None:
        if not isinstance(tool_id, str):
            if name in RECORDED_TOOLS:
                self.bad_links[_id_key(tool_id)] = name
                if isinstance(tool_input, dict):
                    self.bad_named.update(
                        v for v in (tool_input.get("to"), tool_input.get("recipient"))
                        if isinstance(v, str)
                    )
            return
        self.use_counts[tool_id] = self.use_counts.get(tool_id, 0) + 1
        if name not in RECORDED_TOOLS:
            return
        data = tool_input if isinstance(tool_input, dict) else {}
        self.uses.setdefault(tool_id, []).append(UseInfo(
            name, line, ts,
            data["to"] if "to" in data else ABSENT,
            data["recipient"] if "recipient" in data else ABSENT,
        ))

    def note_bad_result(self, tool_id: object, block: dict, tur: object) -> None:
        """A `tool_result` whose `tool_use_id` is not a string: when it is the id of a recorded
        malformed use, the children it names are malformed too."""
        if _id_key(tool_id) not in self.bad_links:
            return
        data = tur if isinstance(tur, dict) else {}
        self.bad_named.update(
            v for v in (block.get("agentId"), data.get("agentId"), data.get("resumedAgentId"))
            if isinstance(v, str)
        )

    def note_result(
        self, tool_id: str, block: dict, tur: object, tur_present: bool, line: int,
    ) -> None:
        if tool_id not in self.uses:
            return
        is_error = block.get("is_error") is True
        ids: list[str] = []
        bad = False
        for value in (block.get("agentId", ABSENT), tur.get("agentId", ABSENT) if isinstance(
                tur, dict) else ABSENT):
            if value is ABSENT:
                continue
            if isinstance(value, str):
                ids.append(value)
            else:
                bad = True
        kind = "absent"
        if tur_present:
            kind = "other"
            if isinstance(tur, dict):
                kind = "object"
            elif isinstance(tur, str):
                kind = "string"
        data = tur if isinstance(tur, dict) else {}
        try:
            digest = hashlib.sha256(
                json.dumps([block, tur if tur_present else None, tur_present], sort_keys=True,
                           ensure_ascii=True).encode("ascii")
            ).hexdigest()
        except (TypeError, ValueError):
            digest = None
        self.results.setdefault(tool_id, []).append(ResultInfo(
            line, digest, is_error, kind, tuple(ids), bad,
            data.get("status", ABSENT), data.get("isAsync", ABSENT),
            data.get("resumedAgentId", ABSENT), data.get("success", ABSENT),
            data.get("message", ABSENT),
        ))


@dataclass
class Decision:
    state: str
    reason: str = "none"
    resumes: int = 0
    # what decided a `reported_failed` state: ("launch_result" | "notification", line, in_launcher)
    evidence: tuple[str, int, bool] | None = None


def normalized_status(word: object) -> str:
    """completed | failed | killed (killed, stopped, cancelled) | unrecognized."""
    if word == "completed":
        return "completed"
    if word == "failed":
        return "failed"
    if isinstance(word, str) and word in _STOPPED_WORDS:
        return "killed"
    return "unrecognized"


_STATE_OF_WORD = {"completed": RETURNED, "failed": REPORTED_FAILED, "killed": REPORTED_KILLED}


def launcher_of(meta: dict, sibling_ids, own_id: str) -> tuple[str | None, str]:
    """T0/T1 over the child's meta: ("main", "none"), (parent subagent id, "none"), or
    (None, reason) with reason `malformed` or `parent_missing`."""
    parent = meta.get("parentAgentId", ABSENT)
    depth = meta.get("spawnDepth", ABSENT)
    if parent is not ABSENT and (
        not isinstance(parent, str) or not parent or parent == own_id
    ):
        return None, "malformed"
    if depth is not ABSENT and (
        not isinstance(depth, int) or isinstance(depth, bool) or depth < 1
    ):
        return None, "malformed"
    if meta.get("toolUseId", ABSENT) is not ABSENT and not isinstance(meta["toolUseId"], str):
        return None, "malformed"
    if parent is ABSENT:
        return ("main", "none") if depth is ABSENT or depth < 2 else (None, "parent_missing")
    if parent in sibling_ids:
        return parent, "none"
    return None, "parent_missing"


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _distinct(results: list[ResultInfo]) -> list[ResultInfo]:
    seen: set[str] = set()
    out = []
    for result in results:
        if result.digest is not None:
            if result.digest in seen:
                continue
            seen.add(result.digest)
        out.append(result)
    return out


def _send_uses(facts: ReturnFacts):
    for tool_id, uses in facts.uses.items():
        if uses and uses[0].name == SEND_TOOL:
            yield tool_id, uses


@dataclass
class _Effect:
    """What one SendMessage request does to one child."""

    kind: str  # resume | malformed | delivery_unknown
    tool_id: str = ""
    ts: datetime | None = None
    line: int = 0


def request_effects(
    facts: ReturnFacts, tool_id: str, uses: list[UseInfo], siblings: frozenset[str] = frozenset(),
) -> dict[str, _Effect]:
    """{child id: effect} of one SendMessage request (T0, T3). A rejected request, and a queued
    one, touch no child. `siblings` are the ids of the session's children: a `to` or
    `recipient` that is none of them is an agent name (GA-7), never a contradiction."""
    use = uses[0]
    results = _distinct(facts.results.get(tool_id, []))
    if results and all(r.is_error for r in results):
        return {}
    present = [v for v in (use.to, use.recipient) if v is not ABSENT]
    strings = {v for v in present if isinstance(v, str)}
    children = strings & siblings
    # well formed: at least one target field, every present field a string, and no two of them
    # naming different children (the runtime writes both `to` and `recipient`, with one value;
    # a value that is no child's id is an agent name and contradicts nothing)
    fields_bad = (
        (not present) or any(not isinstance(v, str) for v in present) or len(children) > 1
    )
    if not results:
        kind = "malformed" if fields_bad else "delivery_unknown"
        return {c: _Effect(kind, tool_id, use.ts, use.line) for c in strings}
    if len(results) > 1:
        kind = "malformed" if fields_bad else "delivery_unknown"
        return {c: _Effect(kind, tool_id, use.ts, use.line) for c in strings}
    result = results[0]
    resume_shape = (
        result.tur_kind == "object" and result.success is True
        and isinstance(result.resumed, str)
    )
    queued_shape = (
        result.tur_kind == "object" and result.success is True and result.resumed is ABSENT
        and isinstance(result.message, str) and result.message.startswith(QUEUED_PREFIX)
    )
    named = set(strings)
    if isinstance(result.resumed, str):
        named.add(result.resumed)
    if fields_bad or not (resume_shape or queued_shape):
        return {c: _Effect("malformed", tool_id, use.ts, use.line) for c in named}
    if queued_shape:
        return {}
    if children - {result.resumed}:
        return {c: _Effect("malformed", tool_id, use.ts, use.line) for c in named}
    return {result.resumed: _Effect("resume", tool_id, use.ts, use.line)}


def resume_count(
    child: str, issuers: list[ReturnFacts], siblings: frozenset[str] = frozenset(),
) -> int:
    count = 0
    for facts in issuers:
        for tool_id, uses in _send_uses(facts):
            effect = request_effects(facts, tool_id, uses, siblings).get(child)
            if effect is not None and effect.kind == "resume":
                count += 1
    return count


def _launch_candidates(
    child: str, launcher: ReturnFacts, notes: list[Note], meta_tool_use: object = ABSENT,
):
    agent_uses = {
        tool_id for tool_id, uses in launcher.uses.items()
        if uses and uses[0].name in AGENT_LAUNCH_TOOLS
    }
    by_result = {
        tool_id for tool_id in agent_uses
        if any(child in r.agent_ids for r in launcher.results.get(tool_id, []))
    }
    by_note = {n.tool_use_id for n in notes if n.tool_use_id in agent_uses}
    # the child's meta `toolUseId` (a string) names the launching tool_use in its launcher
    by_meta = (
        {meta_tool_use}
        if isinstance(meta_tool_use, str) and meta_tool_use in agent_uses else set()
    )
    return by_result, by_note, by_meta


def main_side_digest(
    child: str, main: ReturnFacts, notes: list[Note], meta_tool_use: object = ABSENT,
) -> str:
    """H1: a bounded digest of the main-side evidence for this child: its launch and resume
    `tool_use` ids, lines, timestamps and result classes, and its accepted notifications' ids,
    statuses and lines."""
    facts: list = []
    by_result, by_note, by_meta = _launch_candidates(child, main, notes, meta_tool_use)
    for tool_id in sorted(by_result | by_note | by_meta):
        use = main.uses[tool_id][0]
        facts.append([
            "launch", tool_id, use.line, use.ts.isoformat() if use.ts else None,
            main.use_counts.get(tool_id, 0),
            [
                [r.line, r.is_error, r.tur_kind, sorted(r.agent_ids), r.bad_id, repr(r.status),
                 repr(r.is_async)]
                for r in _distinct(main.results.get(tool_id, []))
            ],
        ])
    for tool_id, uses in _send_uses(main):
        use = uses[0]
        present = {v for v in (use.to, use.recipient) if isinstance(v, str)}
        results = _distinct(main.results.get(tool_id, []))
        resumed = any(r.resumed == child for r in results)
        if child not in present and not resumed:
            continue
        facts.append([
            "request", tool_id, use.line, use.ts.isoformat() if use.ts else None,
            main.use_counts.get(tool_id, 0), repr(use.to), repr(use.recipient),
            [
                [r.line, r.is_error, r.tur_kind, repr(r.success), repr(r.resumed), repr(r.message)]
                for r in results
            ],
        ])
    facts.append(["meta_link", meta_tool_use if isinstance(meta_tool_use, str) else None])
    facts.append(["bad", child in main.bad_named])
    facts.append(["notes", sorted((n.tool_use_id, n.status, n.line) for n in notes)])
    text = json.dumps(facts, sort_keys=True, default=repr)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def decide(
    child: str, launcher: ReturnFacts, main: ReturnFacts, notes: list[Note],
    issuers: list[ReturnFacts], siblings: frozenset[str] = frozenset(),
    meta_tool_use: object = ABSENT,
) -> Decision:
    """T0 to T5 for `child` (T1's launcher choice is `launcher_of`; its failures never reach here).

    `launcher` is the launcher transcript's facts (the main transcript's own when the child is
    launched by the main thread), `main` the main transcript's, `notes` the accepted main
    notifications whose task-id is the child, `issuers` every inspected transcript's facts that
    may hold a resume request; `siblings` the ids of the session's children (GA-7)."""
    resumes = resume_count(child, issuers, siblings)

    def unknown(reason: str) -> Decision:
        return Decision(UNKNOWN, reason, resumes)

    # T0 (GA-4): a launch or request with a non-string tool_use id names this child
    if any(child in f.bad_named for f in (launcher, main, *issuers)):
        return unknown("malformed")

    # T2: the launch
    by_result, by_note, by_meta = _launch_candidates(child, launcher, notes, meta_tool_use)
    candidates = by_result | by_note | by_meta
    if not candidates:
        return unknown("no_evidence")
    if len(candidates) > 1:
        return unknown("conflict")
    launch_id = next(iter(candidates))
    launch_use = launcher.uses[launch_id][0]
    results = _distinct(launcher.results.get(launch_id, []))
    if len(results) > 1:
        return unknown("conflict")

    sync_value: tuple[str, int] | None = None
    if results:
        result = results[0]
        if (
            result.is_error or result.tur_kind in ("string", "other") or result.bad_id
            or (result.status is not ABSENT and not isinstance(result.status, str))
        ):
            return unknown("malformed")
        ids = set(result.agent_ids)
        if ids and ids != {child}:
            return unknown("conflict")
        status = result.status
        if result.is_async is True and status in _TERMINAL_WORDS:
            return unknown("malformed")
        asynchronous = (
            status == "async_launched"
            or (result.is_async is True and status is ABSENT)
            or (not ids and launch_id in by_note)
        )
        if not asynchronous:
            if status is ABSENT or status == "completed":
                sync_value = (RETURNED, result.line)
            elif status == "failed":
                sync_value = (REPORTED_FAILED, result.line)
            elif status in _STOPPED_WORDS:
                sync_value = (REPORTED_KILLED, result.line)
            else:
                return unknown("status_unrecognized")
    elif launch_id not in by_note:
        return unknown("no_evidence")

    # T3: requests and rounds
    named: list[tuple[str, UseInfo, ReturnFacts, _Effect]] = []
    for facts in issuers:
        for tool_id, uses in _send_uses(facts):
            effect = request_effects(facts, tool_id, uses, siblings).get(child)
            if effect is not None:
                named.append((tool_id, uses[0], facts, effect))
    unique = {id(f): f for f in [launcher, *issuers]}.values()

    def carried_once(tool_id: str) -> bool:
        return sum(f.use_counts.get(tool_id, 0) for f in unique) == 1

    if not carried_once(launch_id) or not all(carried_once(item[0]) for item in named):
        return unknown("conflict")
    for _tool_id, _use, _facts, effect in named:
        if effect.kind != "resume":
            return unknown(effect.kind)
    request_rounds = [(tool_id, use, facts) for tool_id, use, facts, _e in named]
    rounds: list[tuple[str, UseInfo, ReturnFacts]] = [(launch_id, launch_use, launcher)]
    if request_rounds:
        stamps = [
            _aware(use.ts) if use.ts is not None else None
            for _t, use, _f in rounds + request_rounds
        ]
        if any(s is None for s in stamps) or len(set(stamps)) != len(stamps):
            return unknown("ambiguous")
        if any(s <= stamps[0] for s in stamps[1:]):
            return unknown("ambiguous")
        rounds = [rounds[0]] + [
            r for _s, r in sorted(
                zip(stamps[1:], request_rounds), key=lambda pair: pair[0]
            )
        ]
    round_by_id = {tool_id: (use, facts) for tool_id, use, facts in rounds}

    # T4: notifications
    for n in notes:
        if n.tool_use_id not in round_by_id:
            return unknown("conflict")
        use, facts = round_by_id[n.tool_use_id]
        if facts is main:
            if n.line <= use.line:
                return unknown("ambiguous")
        elif n.ts is None or use.ts is None or _aware(n.ts) <= _aware(use.ts):
            return unknown("ambiguous")
    latest_id = rounds[-1][0]
    in_latest = [n for n in notes if n.tool_use_id == latest_id]

    # T5
    words = {normalized_status(n.status) for n in in_latest}
    if "unrecognized" in words:
        return unknown("status_unrecognized")
    if len(words) > 1:
        return unknown("conflict")
    note_state = _STATE_OF_WORD[next(iter(words))] if words else None
    sync = sync_value if latest_id == launch_id else None
    if sync is not None and note_state is not None and sync[0] != note_state:
        return unknown("conflict")
    decided, evidence = PENDING, None
    if note_state is not None:
        decided = note_state
        evidence = ("notification", min(n.line for n in in_latest), False)
    elif sync is not None:
        decided = sync[0]
        evidence = ("launch_result", sync[1], True)
    if decided != REPORTED_FAILED:
        evidence = None
    return Decision(decided, "none", resumes, evidence)
