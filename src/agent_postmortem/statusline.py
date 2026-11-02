"""`agent-postmortem statusline subagents` (docs/DESIGN.md 4.14.7): the subagentStatusLine command.

Claude Code calls it once per refresh tick with every visible subagent row. It writes
`{"id", "content"}` only for a row where it adds a fact (an interrupt nothing in the session
explains, what an interrupted or failed subagent changed, a return its own last entry
contradicts, quiet time above the measured p99); every other row keeps the native rendering.
A row it writes repeats the native row's own text (name or label, token count), which Claude
Code itself shows there, and nothing else from any transcript. Any error prints nothing.
Results of full parses are kept in the live store by file signature, so a finished subagent
is parsed once, not once per tick.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import disclosure, live, state, status

_SEP = " · "


def _tokens(count: object) -> str:
    if not isinstance(count, (int, float)) or count < 0:
        return ""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M tokens"
    if count >= 1000:
        return f"{count / 1000:.1f}k tokens"
    return f"{int(count)} tokens"


def _quiet(seconds: float) -> str:
    return f"quiet {status._age(seconds)}"


def _fit(prefix_parts: list[str], fact: str, columns: int) -> str:
    """The fact always survives; the native prefix is cut from its right end to fit."""
    prefix = _SEP.join(part for part in prefix_parts if part)
    full = f"{prefix}{_SEP}{fact}" if prefix else fact
    if len(full) <= columns:
        return full
    room = columns - len(fact) - len(_SEP) - 1
    if room <= 0:
        return fact[:columns]
    return f"{prefix[:room]}…{_SEP}{fact}"


def rows(payload: dict, now: float | None = None) -> list[dict]:
    if os.environ.get("AGENT_POSTMORTEM_STATUSLINE", "").strip() == "0":
        return []
    tasks = payload.get("tasks")
    transcript = payload.get("transcript_path")
    if not isinstance(tasks, list) or not isinstance(transcript, str):
        return []
    main_path = Path(transcript).expanduser()
    if not main_path.is_file():
        return []
    columns = payload.get("columns")
    columns = columns if isinstance(columns, int) and columns >= 20 else 100
    now = time.time() if now is None else now

    follower = live.SessionFollower(main_path, caller_session=None)
    data = state.read_cache(follower.store_path)
    status._restore(follower, data)
    follower.read_main()
    checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    cached: dict = {}
    out: list[dict] = []
    sub_dir = follower.session_dir / "subagents"
    for task in tasks:
        if not isinstance(task, dict):
            continue
        agent_id = task.get("id")
        if not disclosure.is_canonical_id("agent", agent_id):
            continue
        path = sub_dir / f"agent-{agent_id}.jsonl"
        if not path.is_file():
            continue
        sub = follower.subs.get(agent_id) or live._Sub(agent_id, live.Tail(path))
        entries = sub.tail.read(live._sub_want)
        if sub.tail.reset:
            sub.facts = live.SubFacts()
        sub.facts.update(entries)
        follower.subs[agent_id] = sub
        facts: list[tuple[str, str]] = []  # (full, short) forms of each fact
        word = task.get("status")
        if word == "running":
            try:
                quiet = now - path.stat().st_mtime
            except OSError:
                quiet = 0.0
            if quiet > live.QUIET_SHOW_SECONDS:
                facts.append((_quiet(quiet), _quiet(quiet)))
        elif word in ("killed", "failed", "stopped"):
            notice = follower.main.notices.get(agent_id) or {}
            named = notice.get("kind") in ("stopped_by_session", *status._FAILED_CAUSES)
            if word == "killed" and sub.facts.final_kind == "kill" and not named and \
                    status._explained(follower, agent_id, sub) is None:
                facts.append(("interrupted, nothing in this session stopped it",
                              "unexplained interrupt"))
            paths, _commits, partial = status._changes(sub, checks, cached)
            if paths:
                changed = (f"changed {paths}{'+' if partial else ''} "
                           f"path{'' if paths == 1 and not partial else 's'}")
                facts.append((changed, changed))
        elif word == "completed":
            claim = status._claim(follower, sub, checks, cached)
            if claim is not None:
                if claim["check"] == "returned_on_error":
                    # transcript text: share level, the session's ids as aliases
                    detail = live.session_redactor(main_path, [agent_id])(claim["detail"])
                    facts.append((f"returned, but its last entry says: {detail}",
                                  "returned on an error"))
                else:
                    facts.append(("returned, but a file its prompt asked for is missing",
                                  "returned, output missing"))
        if not facts:
            continue
        name = task.get("name") or task.get("label") or task.get("description") or ""
        prefix = [str(name).replace("\n", " ").strip(), _tokens(task.get("tokenCount"))]
        full = _SEP.join(f for f, _short in facts)
        short = _SEP.join(sh for _full, sh in facts)
        fact = full if len(full) + 2 <= columns else short
        out.append({"id": agent_id, "content": _fit(prefix, fact, columns)})
    # the offsets and facts too, so the next tick reads only what was appended (4.14.7)
    status._save(follower, cached, now)
    return out


def main_subagents() -> int:
    """Read the payload from stdin, print the rows; never fail, never print an error."""
    try:
        payload = json.loads(sys.stdin.read() or "null")
        if not isinstance(payload, dict):
            return 0
        for row in rows(payload):
            print(json.dumps(row, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - a statusline must never break the panel
        if os.environ.get("AGENT_POSTMORTEM_DEBUG"):
            raise
    return 0
