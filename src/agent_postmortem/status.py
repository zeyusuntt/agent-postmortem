"""`agent-postmortem status` (docs/DESIGN.md 4.14.4): one snapshot of one session.

Built from the live follower's incremental reads (live.py), whose offsets and facts are kept in
the live store (4.14.8) so a second call reads only what was appended. No full run load. Quiet
time is shown, never judged. Background-session states are passed through from the native job
files. Standard library only; read-only over everything but the live store.
"""

from __future__ import annotations

import json
import re
import stat
import time
from pathlib import Path

from . import live, stalls, state, workers
from .redact import Redactor

# Claude Code's own tool names: the only tool names status prints (a plugin or MCP tool name can
# be user-authored, 4.14.4)
BUILTIN_TOOLS = frozenset({
    "Agent", "Bash", "BashOutput", "Edit", "EnterWorktree", "ExitWorktree", "Glob", "Grep",
    "KillShell", "LS", "Monitor", "MultiEdit", "NotebookEdit", "NotebookRead", "Read",
    "ScheduleWakeup", "SendMessage", "Skill", "StructuredOutput", "SubagentHandback", "Task",
    "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate", "TodoWrite",
    "ToolSearch", "WebFetch", "WebSearch", "Workflow", "Write",
})
_BACKGROUND_RECENT_SECONDS = 86400.0
_ROW_WIDTH = 13


def _age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 120:
        return f"{int(seconds)}s"
    if seconds < 7200:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _restore(follower: live.SessionFollower, data: dict) -> None:
    transcripts = data.get("transcripts") if isinstance(data.get("transcripts"), dict) else {}
    main_entry = transcripts.get(str(follower.main_path))
    if isinstance(main_entry, dict) and isinstance(main_entry.get("fork_line"), int) \
            and main_entry.get("facts_version") == live.MAIN_FACTS_VERSION:
        follower.main_tail = live.tail_from(follower.main_path, main_entry)
        follower.main = live.MainFacts.from_dict(main_entry.get("facts"))
        follower._facts_fork_line = main_entry["fork_line"]
    sub_dir = follower.session_dir / "subagents"
    prefix = str(sub_dir) + "/"
    for path_str, entry in transcripts.items():
        if not isinstance(entry, dict) or not str(path_str).startswith(prefix):
            continue
        match = live._AGENT_FILE_RE.match(Path(path_str).name)
        if not match or entry.get("sub_facts_version") != live.SUB_FACTS_VERSION:
            continue  # another version (4.14.8): this transcript is read again from byte 0
        sub = live._Sub(match.group(1), live.tail_from(Path(path_str), entry))
        sub.facts = live.SubFacts.from_dict(entry.get("facts"))
        deferred = entry.get("deferred")
        if isinstance(deferred, (int, float)) and not isinstance(deferred, bool):
            sub.deferred = float(deferred)  # 4.14.5: a bounded read's place in its queue
        follower.subs[match.group(1)] = sub


def _save(follower: live.SessionFollower, cached: dict, now: float) -> None:
    try:
        with state.cache_transaction(follower.store_path) as data:
            transcripts = data.setdefault("transcripts", {})
            if not isinstance(transcripts, dict):
                transcripts = data["transcripts"] = {}
            transcripts[str(follower.main_path)] = {
                **live.tail_state(follower.main_tail), "facts": follower.main.to_dict(),
                "fork_line": follower._facts_fork_line, "seen": now,
                "facts_version": live.MAIN_FACTS_VERSION,
            }
            for sub in follower.subs.values():
                transcripts[str(sub.tail.path)] = {
                    **live.tail_state(sub.tail), "facts": sub.facts.to_dict(), "seen": now,
                    "sub_facts_version": live.SUB_FACTS_VERSION,
                    **({"deferred": sub.deferred} if sub.deferred is not None else {}),
                }
            checks = data.setdefault("checks", {})
            if not isinstance(checks, dict):
                checks = data["checks"] = {}
            checks.update(cached)
            for check_key in list(checks):
                # `claim:<path>` / `changes:<path>`: kept only while that transcript exists
                _kind, _sep, check_path = check_key.partition(":")
                if not check_path or not Path(check_path).exists():
                    del checks[check_key]
            for path_str in list(transcripts):
                entry = transcripts[path_str]
                seen = entry.get("seen") if isinstance(entry, dict) else None
                if not isinstance(seen, (int, float)) or now - seen > live._STORE_PRUNE_SECONDS:
                    del transcripts[path_str]
    except state.StateError:
        pass


def _signature(path: Path) -> list | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_ino, st.st_size, st.st_mtime_ns]


def _cached(checks: dict, cached: dict, key: str, path: Path, compute):
    """A per-transcript result kept by file signature: a finished subagent is parsed once."""
    signature = _signature(path)
    entry = checks.get(key)
    if isinstance(entry, dict) and entry.get("sig") == signature and "value" in entry:
        return entry["value"]
    value = compute()
    cached[key] = {"sig": signature, "value": value}
    return value


def _claim(
    follower: live.SessionFollower, sub: live._Sub, checks: dict, cached: dict,
) -> dict | None:
    if not sub.facts.may_end_on_a_cause():
        missing = live.missing_outputs(sub.facts.outputs, sub.facts.cwd)
        return {"check": "output_missing_at_return", "missing": missing} if missing else None

    def compute():
        cause = live.final_cause(sub.tail.path)
        if cause is not None:
            return {"check": "returned_on_error", "detail": cause}
        return None

    found = _cached(checks, cached, f"claim:{sub.tail.path}", sub.tail.path, compute)
    if found is not None:
        return found
    missing = live.missing_outputs(sub.facts.outputs, sub.facts.cwd)
    if missing:
        return {"check": "output_missing_at_return", "missing": missing}
    return None


def _changes(sub: live._Sub, checks: dict, cached: dict) -> tuple[int, int, bool]:
    value = _cached(checks, cached, f"changes:{sub.tail.path}", sub.tail.path,
                    lambda: list(live.changes(sub.tail.path)))
    return int(value[0]), int(value[1]), bool(value[2])


def _explained(follower: live.SessionFollower, agent_id: str, sub: live._Sub) -> str | None:
    kill_ts = sub.facts.final_ts
    for stop_ts, ids in follower.main.task_stops:
        if any(agent_id in value for value in ids):
            return "stopped by this session"
        if kill_ts is not None and stop_ts is not None and \
                0 <= kill_ts - stop_ts <= live.PARENT_STOP_WINDOW_SECONDS:
            return "stopped by this session"
    if kill_ts is not None and any(
        abs(kill_ts - ts) <= live.PARENT_SENTINEL_WINDOW_SECONDS for ts in follower.main.kill_ts
    ):
        return "interrupted together with the session"
    if sub.facts.rejected_by_user:
        return "the person rejected its tool call"
    return None


def _job_word(jobs_root: Path, session_id: str) -> str | None:
    job_dir = live.find_job(jobs_root, session_id)
    return live._read_job_state(job_dir)[0] if job_dir is not None else None


# BACKGROUND reads at most this many jobs' own sessions per call
_JOB_STALL_READS = 5


# a blocked row of another session older than this folds into one line
OLD_BLOCKED_SECONDS = 72 * 3600.0


def _background(jobs_root: Path, projects_root: Path, now: float, *,
                max_reads: int | None = _JOB_STALL_READS, want=None, mine=None) -> dict:
    """BACKGROUND (4.14.4): every job changed in the last day, and every working or blocked one,
    by native state, except that a working one idle on a command that will not end counts as
    stalled. Only a job whose own state and transcript have not changed for IDLE_SECONDS, whose
    process is a live background session and that `want(cwd)` keeps is read for that; `status`
    reads at most `max_reads` per call, starting at a place that moves every minute so every
    such job is reached, and `scan` reads them all (max_reads None). A blocked
    or failed job says why, by class and never in its own words (workers.classify), and a blocked
    one that delivered counts as `delivered`; `mine` holds the launches of the session asking, by
    short id (MainFacts.workers), checked against each job's own createdAt."""
    counts: dict[str, int] = {}
    listed: list[dict] = []
    try:
        job_dirs = sorted(p for p in jobs_root.iterdir() if p.is_dir())
    except OSError:
        job_dirs = []
    jobs = []
    for job_dir in job_dirs:
        word, session_id = live._read_job_state(job_dir)
        if word is None:
            continue
        try:
            mtime = (job_dir / "state.json").stat().st_mtime
        except OSError:
            continue
        if word not in ("working", "blocked") and now - mtime > _BACKGROUND_RECENT_SECONDS:
            continue
        jobs.append((job_dir, word, session_id, mtime))
    eligible = [(job_dir, session_id, mtime) for job_dir, word, session_id, mtime in jobs
                if word == "working" and session_id and now - mtime >= workers.MCP_TASK_WAIT_SECONDS
                and (want is None or want(_job_name_and_cwd(job_dir)[1]))]
    stalled: dict[Path, dict] = {}
    waiting: dict[Path, dict] = {}
    if eligible:
        kinds = stalls.live_sessions(projects_root.parent / "sessions")
        candidates = []
        for job_dir, session_id, mtime in eligible:
            main = _job_transcript(projects_root, session_id)
            if main is None or kinds.get(session_id) != "bg":
                continue
            job = workers.read_job(str(job_dir / "state.json")) or {}
            call = {"tool": "MCP", "seconds": round(now - mtime), "digest": "task"} \
                if workers.idle_on_mcp_task(job) else stalls.open_tool_call(main, now)
            if call is not None:
                waiting[job_dir] = call
            elif now - mtime >= stalls.IDLE_SECONDS and now - _mtime(main) >= stalls.IDLE_SECONDS:
                candidates.append((job_dir, main))
        if max_reads is not None and len(candidates) > max_reads:
            start = int(now // 60) % len(candidates)
            candidates = (candidates[start:] + candidates[:start])[:max_reads]
        for job_dir, main in candidates:
            stall = _job_stall(main, now, kinds)
            if stall is not None:
                stalled[job_dir] = stall
    for job_dir, word, session_id, mtime in jobs:
        stall = stalled.get(job_dir)
        call = waiting.get(job_dir)
        if stall is not None:
            word = "stalled"
        elif call is not None:
            word = "waiting"
        counts[word] = counts.get(word, 0) + 1
        if call is not None:
            matches = sorted(projects_root.glob(f"*/{session_id}.jsonl"))
            name, cwd = _job_name_and_cwd(job_dir)
            launch = (mine or {}).get(job_dir.name)
            listed.append({"session": live.session_public_id(matches[0]) if matches else None,
                           "state": "waiting", "age_seconds": call["seconds"],
                           "session_id": session_id, "name": name, "cwd": cwd,
                           "class": workers.TOOL_WAIT, "label": call["tool"],
                           "short": job_dir.name,
                           "mine": isinstance(launch, dict) and workers.created_during(
                               workers.read_job(str(job_dir / "state.json")) or {},
                               launch.get("ts"), launch.get("end"))})
        elif stall is not None:
            matches = sorted(projects_root.glob(f"*/{session_id}.jsonl"))
            name, cwd = _job_name_and_cwd(job_dir)
            launch = (mine or {}).get(job_dir.name)
            listed.append({"session": live.session_public_id(matches[0]) if matches else None,
                           "state": "stalled", "age_seconds": stall["idle_seconds"],
                           "session_id": session_id, "name": name, "cwd": cwd,
                           "stall": stall,
                           "mine": isinstance(launch, dict) and workers.created_during(
                               workers.read_job(str(job_dir / "state.json")) or {},
                               launch.get("ts"), launch.get("end"))})
        elif word in ("blocked", "failed") and session_id:
            matches = sorted(projects_root.glob(f"*/{session_id}.jsonl"))
            public = live.session_public_id(matches[0]) if matches else None
            name, cwd = _job_name_and_cwd(job_dir)
            data = workers.read_job(str(job_dir / "state.json")) or {}
            cls, label = workers.classify(data)
            launch = (mine or {}).get(job_dir.name)
            shown = "delivered" if cls == workers.DELIVERED_WAITING else word
            counts[word] -= 1
            counts[shown] = counts.get(shown, 0) + 1
            if not counts[word]:
                del counts[word]
            listed.append({"session": public, "state": shown, "age_seconds": round(now - mtime),
                           "session_id": session_id, "name": name, "cwd": cwd,
                           "class": cls or workers.UNKNOWN, "label": label,
                           "short": job_dir.name,
                           "mine": isinstance(launch, dict) and workers.created_during(
                               data, launch.get("ts"), launch.get("end"))})
    return {"counts": counts, "sessions": listed}


def own_workers(main_path: Path) -> dict[str, dict]:
    """The workers a session launched, from its transcript (for `scan`, which reads the caller's
    session only for this)."""
    follower = live.SessionFollower(main_path, caller_session=None)
    _restore(follower, state.read_cache(follower.store_path))
    follower._read()
    _save(follower, {}, time.time())
    return follower.main.workers


def _job_transcript(projects_root: Path, session_id: str) -> Path | None:
    matches = sorted(projects_root.glob(f"*/{session_id}.jsonl"))
    if len(matches) != 1:
        return None
    try:  # a FIFO with no writer would hold the open of whichever reader comes first, for ever
        return matches[0] if stat.S_ISREG(matches[0].stat().st_mode) else None
    except OSError:
        return None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return float("inf")


STALL_UNKNOWN = object()  # a read cut short: neither a stall nor the end of one


def _job_stall(main: Path, now: float, kinds: dict[str, str],
               deadline: float | None = None) -> object:
    """docs/DESIGN.md 4.14.4: the STALLED reading of a background
    job's own session, from its own snapshot (without a BACKGROUND list of its own), or None. With
    a `deadline` (time.monotonic), a read cut short keeps its place and returns STALL_UNKNOWN."""
    read: dict = {}
    try:
        found = snapshot(main, now=lambda: now, background=False, live_kinds=kinds,
                         read_deadline=deadline, read_state=read).get("stalled")
    except OSError:
        return None if deadline is None else STALL_UNKNOWN
    return STALL_UNKNOWN if deadline is not None and not read.get("complete") else found


def _job_name_and_cwd(job_dir: Path) -> tuple[str | None, str | None]:
    """A background session's own name and working directory from its state.json (0.4): printed
    at `--local` (the name and the directory's last component) and `--raw` only."""
    try:
        data = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    name, cwd = data.get("name"), data.get("cwd")
    name = " ".join(_CONTROL_RE.sub(" ", name).split())[:_JOB_NAME_MAX] if isinstance(name, str) \
        else None
    cwd = _CONTROL_RE.sub("", cwd).strip() if isinstance(cwd, str) else None
    return (name or None), (cwd or None)


def _shown_cwd(cwd: str | None, level: str) -> str | None:
    if cwd is None:
        return None
    if level == "raw":
        return cwd
    return Path(cwd).name or None


def _workflow(main: live.MainFacts, run_id: str) -> dict:
    """docs/DESIGN.md 4.14.4 (0.4): one workflow of the session; `ended` only once its task
    notification says how it ended, and then the next step is `run`, not `watch`."""
    ended = main.workflow_end(run_id)
    if ended is None:
        return {"run_id": run_id, "next_command": f"agent-postmortem watch {run_id}"}
    return {"run_id": run_id, "ended": ended, "next_command": f"agent-postmortem run {run_id}"}


def _commands(main: live.MainFacts, stamp: float) -> dict:
    """docs/DESIGN.md 4.14.4 (0.4): the session's background shell commands by native end; no
    end notice is "running" only as far as the transcript knows, so the oldest one's launch age
    goes beside the count, and each running one says how long its
    files have been unchanged and whether it waits on stdin."""
    counts, oldest = main.commands()
    return {"counts": counts,
            "oldest_running_seconds": round(stamp - oldest) if oldest is not None else None,
            "running": stalls.running_commands(main, stamp)}


def snapshot(main_path: Path, *, now=time.time, jobs_root: Path | None = None,
             background: bool = True, live_kinds: dict[str, str] | None = None,
             read_deadline: float | None = None, read_state: dict | None = None) -> dict:
    """docs/DESIGN.md 4.14.4: the status of one session, as a dict (`--json` prints it)."""
    stamp = now()
    follower = live.SessionFollower(main_path, jobs_root=jobs_root, now=now, caller_session=None)
    data = state.read_cache(follower.store_path)
    _restore(follower, data)
    read_complete = follower._read(deadline=read_deadline) and not follower.read_failed
    try:  # a line still being written may be the session acting again
        read_complete = read_complete and follower.main_tail.offset >= main_path.stat().st_size
    except OSError:
        read_complete = False
    if read_state is not None:
        read_state["complete"] = read_complete
    checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    cached: dict = {}
    rows: list[dict] = []
    counts = {"running": 0, "returned": 0, "interrupted": 0, "failed": 0, "waiting": 0}
    for agent_id in follower._agent_ids():
        sub = follower.subs.get(agent_id)
        facts = sub.facts if sub is not None else None
        notice = follower.notice(agent_id)
        if notice is not None and not live.returned_after(notice.get("ts"), facts):
            notice = None  # resumed since: the notice describes an earlier round
        done_ts = follower.foreground_done(agent_id)  # a nested child's in its launcher
        foreground = done_ts is not None and live.returned_after(done_ts, facts)
        row: dict = {"agent": follower.names(agent_id), "agent_id": agent_id}
        status = notice.get("status") if notice is not None else None
        if status == "failed":
            row["state"] = "failed"
            row["cause"] = notice.get("kind")
        elif status in ("killed", "stopped"):
            row["state"] = "interrupted"
            kind = notice.get("kind")
            if kind == "stopped_by_session":
                row["cause"] = "stopped by this session"
            elif kind in _FAILED_CAUSES:
                row["cause"] = _FAILED_CAUSES[kind]  # the notice names its own cause
            elif sub is not None and sub.facts.final_kind == "kill":
                row["cause"] = _explained(follower, agent_id, sub)  # R5's own reading
            else:
                row["cause"] = "ended by its task notification"
        elif (status == "completed" and notice.get("kind") != "waiting") or foreground:
            row["state"] = "returned"
        elif status == "completed":
            row["state"] = "waiting"  # native: returned, waiting on its own background work
        elif sub is not None and sub.facts.final_kind == "kill":
            row["state"] = "interrupted"
            row["cause"] = _explained(follower, agent_id, sub)
            row["at"] = sub.facts.final_ts
        else:
            row["state"] = "running"
        counts[row["state"]] += 1
        if sub is not None:
            row["edited"] = len(sub.facts.edited)
            if row["state"] == "running":
                mtime = sub.tail.mtime
                row["quiet_seconds"] = round(stamp - mtime) if mtime is not None else None
                tool = sub.facts.open_tool if sub.facts.final_kind == "tool_use" else None
                row["tool"] = tool if tool in BUILTIN_TOOLS else ("a tool" if tool else None)
            elif row["state"] in ("interrupted", "failed"):
                paths, commits, partial = _changes(sub, checks, cached)
                row.update({"paths": paths, "commits": commits, "paths_partial": partial})
            elif row["state"] == "returned":
                claim = _claim(follower, sub, checks, cached)
                if claim is not None:
                    row["claim"] = claim
        row["next_command"] = f"agent-postmortem run {follower.public_id}"
        rows.append(row)
    _save(follower, cached, stamp)
    projects_root = follower.main_path.parent.parent
    background_rows = {"counts": {}, "sessions": []}
    if background:
        background_rows = _background(follower.jobs_root, projects_root, stamp,
                                      mine=follower.main.workers)
        # a background worker owns none of the machine's other sessions
        kinds = stalls.live_sessions(stalls.default_sessions_root(follower.main_path))
        # with a launch whose id it never saw, or past the facts' bound, any row may be its own
        if kinds.get(follower.main_path.stem) == "bg" and not follower.main.unlinked_launches \
                and len(follower.main.workers) < workers.WORKERS_KEPT:
            background_rows["launched"] = len(follower.main.workers)
    return {
        "session": follower.public_id, "counts": counts,
        "subagents": rows,
        "workflows": [
            _workflow(follower.main, run_id) for run_id in sorted(follower.main.workflows)
        ],
        "commands": _commands(follower.main, stamp),
        # never from cached facts alone when this read failed or stopped short
        "stalled": None if not read_complete else stalls.session_stall(
            follower.main, session_id=follower.main_path.stem,
            sessions_root=stalls.default_sessions_root(follower.main_path), now=stamp,
            job_state=_job_word(follower.jobs_root, follower.main_path.stem), live=live_kinds,
            waiting_on_agents=bool(counts["running"] or counts["waiting"]) or any(
                "ended" not in _workflow(follower.main, run_id)
                for run_id in follower.main.workflows)),
        "background": background_rows,
        "names_unavailable": follower.names.unavailable,
    }


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_JOB_NAME_MAX = 60
# docs/DESIGN.md 4.14.4 (0.4): at the default level the one line that says how to see more
HINT = "--local prints agent ids and background session names"


def _row(word: str, rest: str) -> str:
    return f"  {word:<{_ROW_WIDTH}}{rest}"


def _paths(row: dict) -> str:
    text = live.counts_text(row.get("paths", 0), bool(row.get("paths_partial")),
                            row.get("commits", 0))
    return f"{text} changed"


_FAILED_CAUSES = {
    "stalled": "stalled (the runtime gave up)", "session_limit": "stopped by a usage limit",
    "weekly_limit": "stopped by a usage limit", "api_error": "ended on an API error",
}


def render_text(snap: dict, redactor: Redactor, now: float | None = None) -> str:
    now = time.time() if now is None else now
    counts = snap["counts"]
    total = sum(counts.values())
    fixed: set[int] = set()  # indices of lines the redactor must not see (fixed text)
    lines = [
        f"STATUS  session {snap['session']}: {_plural(total, 'subagent')} "
        f"({counts['running']} running, {counts['returned']} returned, "
        f"{counts['interrupted']} interrupted, {counts['failed']} failed"
        + (f", {counts['waiting']} waiting on their own background work"
           if counts["waiting"] else "")
        + ")"
    ]
    order = {"running": 0, "interrupted": 1, "failed": 2, "returned": 3, "waiting": 4}
    named = redactor.level in ("local", "raw")
    listed = 0
    for row in sorted(snap["subagents"], key=lambda r: (order[r["state"]], r["agent"])):
        state_word = row["state"]
        if named:
            row = {**row, "agent": row.get("agent_id") or row["agent"]}
        before = len(lines)
        if state_word == "running":
            parts = []
            quiet = row.get("quiet_seconds")
            if isinstance(quiet, (int, float)) and quiet > live.QUIET_SHOW_SECONDS:
                parts.append(f"quiet {_age(quiet)}")
            if row.get("tool"):
                parts.append(f"in {row['tool']}")
            if row.get("edited"):
                parts.append(f"edited {_plural(row['edited'], 'file')}")
            lines.append(_row("RUNNING", "  ".join([row["agent"], *parts])))
        elif state_word == "interrupted":
            cause = row.get("cause") or "nothing in this session stopped it"
            cause = redactor(cause) if redactor.level != "raw" else cause
            at = row.get("at")
            when = f"{_age(now - at)} ago; " if isinstance(at, (int, float)) else ""
            lines.append(_row("INTERRUPTED", f"{row['agent']}  {when}{cause}"))
            lines.append(_row("", f"{_paths(row)}  -> {row['next_command']}"))
        elif state_word == "failed":
            cause = _FAILED_CAUSES.get(row.get("cause"), "failed")
            lines.append(_row("FAILED", f"{row['agent']}  {cause}"))
            lines.append(_row("", f"{_paths(row)}  -> {row['next_command']}"))
        elif row.get("claim"):
            claim = row["claim"]
            if claim["check"] == "returned_on_error":
                detail = redactor(claim["detail"]) if redactor.level != "raw" else claim["detail"]
                text = f"returned, but its own last entry says: {detail}"
            elif redactor.level == "share":
                n = len(claim.get("missing", []))
                text = (f"returned, but {_plural(n, 'file')} its prompt asked for "
                        f"{'is' if n == 1 else 'are'} missing")
            else:
                shown = ", ".join(redactor.display_path(p) for p in claim.get("missing", []))
                text = f"returned, but its prompt asked for {shown}, which is missing"
            lines.append(_row("CLAIM", f"{row['agent']}  {text}"))
        listed += len(lines) > before
    completed = 0
    for workflow in sorted(snap["workflows"], key=lambda w: ("ended" in w, w["run_id"])):
        ended = workflow.get("ended")
        if ended == "completed":
            completed += 1
        elif ended is None:
            lines.append(_row("WORKFLOW", f"{workflow['run_id']}  -> {workflow['next_command']}"))
        else:
            lines.append(_row("WORKFLOW", f"{workflow['run_id']}  {ended}  "
                                          f"-> {workflow['next_command']}"))
    if completed:
        lines.append(_row("WORKFLOWS", f"{completed} completed  -> agent-postmortem status "
                                       f"{snap['session']} --json lists them"))
    commands = snap.get("commands") or {}
    command_counts = commands.get("counts") or {}
    if sum(command_counts.values()):
        # 4.14.4 (0.4): a backgrounded shell command (a `codex exec` worker, a long build) is
        # counted by its native end; nothing here reads what it did
        parts = []
        for word, n in command_counts.items():
            if not n:
                continue
            part = f"{n} {word}"
            age = commands.get("oldest_running_seconds")
            if word == "running" and isinstance(age, (int, float)):
                part += f" ({'the oldest ' if n > 1 else ''}launched {_age(age)} ago"
                part += _silence_words(commands.get("running") or [], n) + ")"
            parts.append(part)
        lines.append(_row("COMMANDS", f"{sum(command_counts.values())} in the background: "
                                      f"{', '.join(parts)}; status checks only whether they "
                                      f"still write"))
    stall = snap.get("stalled")
    if stall:
        # idle on a command that will not end; nothing native says so
        what = (f"{_plural(stall['commands'], 'background command')} "
                f"{'has' if stall['commands'] == 1 else 'have'} written nothing")
        if stall.get("silent_seconds") is not None:
            what += f" for {_age(stall['silent_seconds'])}"
        if stall.get("waiting_on_stdin"):
            what += ", waiting on stdin" if stall["commands"] == 1 else \
                f", {stall['waiting_on_stdin']} waiting on stdin"
        # only a command's own files are seen, and it may end yet
        lines.append(_row("STALLED", f"idle {_age(stall['idle_seconds'])}; {what}; "
                          + ("nothing will wake this session" if stall.get("waiting_on_stdin")
                             else "it may be hung or writing elsewhere, and only its end would "
                                  "wake this session")))
        tasks = stall.get("task_ids") or []
        stop = (f"TaskStop {', '.join(tasks)}" if named and tasks
                else "stop it (TaskStop; --local names the task)")
        if not stall.get("waiting_on_stdin"):
            stop = f"if it is stuck, {stop}"
        # fixed words and native task ids only, so the line skips the redactor, which would
        # hide `/dev/null` as a path
        fixed.add(len(lines))
        lines.append(" " * (_ROW_WIDTH + 2) + f"-> {stop}, then rerun it with </dev/null and a "
                                              f"deadline")
    background = snap["background"]
    if background["counts"]:
        total_bg = sum(background["counts"].values())
        order = {"working": 0, "stalled": 1, "waiting": 2, "blocked": 3, "delivered": 4, "done": 5,
                 "stopped": 6, "failed": 7}
        breakdown = ", ".join(f"{n} {word}" for word, n in sorted(
            background["counts"].items(), key=lambda kv: (order.get(kv[0], 9), kv[0])))
        launched = background.get("launched")
        scope = ""
        if isinstance(launched, int):  # the session is itself a background worker
            scope = (f"; this session launched {launched}, scan lists the rest" if launched
                     else "; this session launched none, scan lists them")
        lines.append(f"BACKGROUND {_plural(total_bg, 'session')} on this machine: {breakdown}"
                     f"{scope}")
        # its own workers first; another session's old blocked row is
        # one more on a line of its own, so the one that matters is not skimmed past
        rows = background["sessions"]
        if isinstance(launched, int):
            rows = [entry for entry in rows
                    if entry.get("mine") or entry.get("session") == snap["session"]]
        old = [e for e in rows if _old_blocked(e)]
        for entry in sorted((e for e in rows if not _old_blocked(e)),
                            key=lambda e: not e.get("mine")):
            session = entry.get("session_id") if redactor.level == "raw" else entry["session"]
            if not session:
                continue
            label = ""
            if named:
                cwd = _shown_cwd(entry.get("cwd"), redactor.level)
                if entry.get("name"):
                    label += f"  {entry['name']}"
                if cwd:
                    label += f"  ({cwd})" if not label else f" ({cwd})"
            if entry["state"] == "stalled":
                stall = entry.get("stall") or {}
                what = f"{_plural(stall.get('commands', 1), 'background command')} silent"
                if stall.get("silent_seconds") is not None:
                    what += f" {_age(stall['silent_seconds'])}"
                if stall.get("waiting_on_stdin"):
                    what += ", waiting on stdin"
                lines.append(_row("STALLED", f"{session}{label}  idle {_age(entry['age_seconds'])}"
                                             f"; {what}  -> agent-postmortem status "
                                             f"{entry['session']}"))
            else:
                lines.append(_row(entry["state"].upper(), f"{session}{label}  for "
                                                          f"{_age(entry['age_seconds'])}"
                                                          f"{_why(entry, named)}"))
            listed += 1
        if old:
            lines.append(_row("BLOCKED", f"{len(old)} more in other sessions, for over 3 days "
                                         f"(--json lists them)"))
    if snap.get("names_unavailable"):
        lines.append("  (stable aliases unavailable)")
    if listed and not named:
        lines.append(f"({HINT})")
    if redactor.level == "raw":
        return "\n".join(lines) + "\n"
    return "\n".join(line if index in fixed else redactor(line)
                     for index, line in enumerate(lines)) + "\n"


def _old_blocked(entry: dict) -> bool:
    return entry.get("state") == "blocked" and not entry.get("mine") \
        and (entry.get("age_seconds") or 0) > OLD_BLOCKED_SECONDS


def next_action(entry: dict) -> str:
    """The command that helps a blocked, delivered or failed background session."""
    short = entry.get("short")
    if entry.get("class") in (workers.ERROR, workers.FAILED, workers.DELIVERED_WAITING):
        return f"claude logs {short}"
    return f"claude attach {short}"


def _why(entry: dict, named: bool) -> str:
    """After a row's age: what it waits for, whose it is, and from `--local`, where a tool's
    label and the short id are shown, what helps."""
    cls = entry.get("class")
    if cls is None:
        return ""
    parts = [] if cls == workers.FAILED else [
        workers.reason_words(cls, entry.get("label") if named else None)]
    if entry.get("mine"):
        parts.append("dispatched by this session")
    text = f"  {', '.join(parts)}" if parts else ""
    return f"{text}  -> {next_action(entry)}" if named and entry.get("short") else text


def _silence_words(running: list[dict], n: int) -> str:
    """after a running count, how long the commands' files have been
    unchanged, when that is past the silence mark, and how many wait on stdin."""
    silent = [c for c in running if c.get("silent")]
    stdin = sum(1 for c in silent if c.get("waiting_on_stdin"))
    if not silent:
        return ""
    if n == 1 and len(silent) == 1:
        quiet = silent[0].get("silent_seconds")
        words = f", silent {_age(quiet)}" if quiet is not None else ", silent"
        if silent[0].get("end_only"):
            words += f", {silent[0]['end_only']} prints only at its end"
        return words + (", waiting on stdin" if stdin else "")
    return f", {len(silent)} silent" + (f", {stdin} waiting on stdin" if stdin else "")


def render_json(snap: dict, redactor: Redactor) -> str:
    """docs/DESIGN.md 4.14.4, 5.3b: schema 3 with its disclosure level, the status document."""
    shown = json.loads(json.dumps(snap))
    shown["schema"] = 3
    shown["disclosure"] = redactor.level
    shown["document"] = "status"
    shown["background"].pop("launched", None)  # text only: `mine` on each row says the same
    named = redactor.level in ("local", "raw")
    for entry in shown["background"]["sessions"]:
        if redactor.level != "raw":
            entry.pop("session_id", None)
        if not named and isinstance(entry.get("stall"), dict):
            entry["stall"].pop("task_ids", None)
        if not named:  # a tool's label and the short id are shown from --local
            entry.pop("label", None)
            entry.pop("short", None)
        elif redactor.level == "local" and isinstance(entry.get("label"), str):
            entry["label"] = redactor(entry["label"])
        if named:
            entry["cwd"] = _shown_cwd(entry.get("cwd"), redactor.level)
            if redactor.level == "local":
                # what the text line gets: a path or an id inside is hidden
                for key in ("name", "cwd"):
                    if isinstance(entry.get(key), str):
                        entry[key] = redactor(entry[key])
        else:
            entry.pop("name", None)
            entry.pop("cwd", None)
    if not named:
        # a native task id is shown from --local, like an agent id
        for command in (shown.get("commands") or {}).get("running") or []:
            command.pop("task_id", None)
        if isinstance(shown.get("stalled"), dict):
            shown["stalled"].pop("task_ids", None)
    for row in shown["subagents"]:
        if named and row.get("agent_id"):
            row["agent"] = row["agent_id"]
        else:
            row.pop("agent_id", None)
        if redactor.level != "raw" and isinstance(row.get("cause"), str):
            row["cause"] = redactor(row["cause"])
        claim = row.get("claim")
        if redactor.level != "raw" and isinstance(claim, dict) \
                and isinstance(claim.get("detail"), str):
            claim["detail"] = redactor(claim["detail"])
        if isinstance(claim, dict) and "missing" in claim:
            if redactor.level == "share":
                claim["missing"] = len(claim["missing"])
            elif redactor.level == "local":
                claim["missing"] = [redactor.display_path(p) for p in claim["missing"]]
    return json.dumps(shown, sort_keys=True) + "\n"
