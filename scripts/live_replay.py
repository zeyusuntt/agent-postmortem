"""Replay benchmark for live mode (docs/DESIGN.md 4.14).

Re-feeds synthetic transcripts shaped like the Phase 0 corpus episodes, line by line at their
original relative timing, to the live followers (`watch --session`'s SessionFollower and
`watch <run>`), on a virtual clock (no real waiting), and reports per failure kind: recall,
precision and the median time from the moment the evidence exists to the moment a line names it.

    PYTHONPATH=src python scripts/live_replay.py [--json] [--interval 2]

Everything it writes goes to a temporary directory, including the state directory; nothing real
is read. Standard library only, plus agent_postmortem itself.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import statistics
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

SESSION = "00000000-0000-4000-8000-000000000000"
SLUG = "-home-user-project"
T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
KILL_TOOL = "[Request interrupted by user for tool use]"
KILL = "[Request interrupted by user]"


def iso(t: float) -> str:
    return (T0 + timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def aid(n: int) -> str:
    return "a" + format(n, "016x")


def _base(t: float, agent: str | None) -> dict:
    entry = {"sessionId": SESSION, "version": "2.1.314", "cwd": "/home/user/project",
             "timestamp": iso(t)}
    if agent:
        entry["agentId"] = agent
        entry["isSidechain"] = True
    return entry


def user(t, agent, content):
    return {**_base(t, agent), "type": "user", "message": {"role": "user", "content": content}}


def text(t, agent, words):
    return {**_base(t, agent), "type": "assistant", "message": {
        "role": "assistant", "content": [{"type": "text", "text": words}], "usage": {}}}


def tool_use(t, agent, tool_id, name, tool_input):
    return {**_base(t, agent), "type": "assistant", "message": {
        "role": "assistant", "usage": {},
        "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]}}


def tool_result(t, agent, tool_id, content, **extra):
    return {**_base(t, agent), "type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": content}]}, **extra}


def notice(t, task, tool_id, status, summary):
    body = (f"<task-notification>\n<task-id>{task}</task-id>\n<tool-use-id>{tool_id}</tool-use-id>"
            f"\n<status>{status}</status>\n<summary>{summary}</summary>\n</task-notification>")
    return {**_base(t, None), "type": "user", "message": {"role": "user", "content": body},
            "origin": {"kind": "task-notification"}}


def launch(t, n):
    tool_id = f"toolu_launch{n}"
    return [
        ("main", t, tool_use(t, None, tool_id, "Agent", {"description": "d", "prompt": "p",
                                                         "run_in_background": True})),
        ("main", t + 1, tool_result(t + 1, None, tool_id, "launched",
                                    toolUseResult={"agentId": aid(n), "status": "async_launched"})),
    ]


def writes(t, n, count, base="/tmp/live-replay"):
    steps = []
    for i in range(count):
        tool_id = f"toolu_w{n}_{i}"
        steps.append((("sub", n), t + i, tool_use(t + i, aid(n), tool_id, "Write",
                                                  {"file_path": f"{base}/{n}/out{i}.md",
                                                   "content": "x"})))
        steps.append((("sub", n), t + i + 0.5, tool_result(t + i + 0.5, aid(n), tool_id, "ok")))
    return steps


@dataclass
class Scenario:
    name: str
    kind: str  # the corpus class (CORPUS.md row) it is shaped like
    native: bool  # whether 2.1.314 already surfaces it (PLATFORM.md)
    steps: list = field(default_factory=list)
    expect: list = field(default_factory=list)  # (event, agent n or None, evidence time)
    mode: str = "session"


def session_scenarios(tmp: Path) -> list[Scenario]:
    out: list[Scenario] = []
    missing = str(tmp / "missing" / "report.md")
    present = tmp / "present" / "report.md"
    present.parent.mkdir(parents=True, exist_ok=True)
    present.write_text("ok", encoding="utf-8")

    def sub(n, t, content):
        return (("sub", n), t, user(t, aid(n), content))

    # row 6 / #84346: interrupted, nothing in the session explains it, after writing 2 files
    s = Scenario("unexplained_interrupt_with_changes", "row 6 (#84346)", False)
    s.steps = launch(0, 1) + [sub(1, 2, "do the task")] + writes(5, 1, 2) + [
        (("sub", 1), 10, tool_use(10, aid(1), "toolu_b1", "Bash", {"command": "make"})),
        (("sub", 1), 640, user(640, aid(1), [{"type": "text", "text": KILL_TOOL}])),
    ]
    s.expect = [("KILL", 1, 640)]
    out.append(s)

    # the same interrupt, but this session stopped the agent: only its changes are worth a line
    s = Scenario("taskstop_with_changes", "row 6 control", True)
    s.steps = launch(0, 2) + [sub(2, 2, "do the task")] + writes(5, 2, 1) + [
        ("main", 300, tool_use(300, None, "toolu_s2", "TaskStop", {"task_id": aid(2)})),
        (("sub", 2), 301, user(301, aid(2), [{"type": "text", "text": KILL}])),
    ]
    s.expect = [("FAILED", 2, 301)]
    out.append(s)

    # the user pressed Esc: the session interrupt explains it, nothing to say
    s = Scenario("user_interrupt", "row 6 control", True)
    s.steps = launch(0, 3) + [sub(3, 2, "do the task")] + [
        ("main", 200, user(200, None, [{"type": "text", "text": KILL}])),
        (("sub", 3), 201, user(201, aid(3), [{"type": "text", "text": KILL}])),
    ]
    out.append(s)

    # native stall watchdog, after 3 writes (row 8 plus R6)
    s = Scenario("native_stall_with_changes", "row 8 + R6", True)
    s.steps = launch(0, 4) + [sub(4, 2, "do the task")] + writes(5, 4, 3) + [
        ("main", 700, notice(700, aid(4), "toolu_launch4", "failed",
                             'Agent "d" failed: Agent stalled: no progress for 600s '
                             "(stream watchdog did not recover)")),
    ]
    s.expect = [("FAILED", 4, 700)]
    out.append(s)

    # usage limit with no changes (row 9): native says it all
    s = Scenario("usage_limit_no_changes", "row 9", True)
    s.steps = launch(0, 5) + [sub(5, 2, "do the task"), (
        "main", 60, notice(60, aid(5), "toolu_launch5", "failed",
                           'Agent "d" failed: Agent terminated early due to an API error: '
                           "You've hit your session limit · resets 4:10am"))]
    out.append(s)

    # #98170 / #85066: "completed" while its own last entry is an error
    s = Scenario("completed_on_api_error", "#98170 / #85066", False)
    s.steps = launch(0, 6) + [sub(6, 2, "do the task"),
                              (("sub", 6), 30,
                               text(30, aid(6), "API Error: Server error mid-response")),
                              ("main", 31, notice(31, aid(6), "toolu_launch6", "completed",
                                                  'Agent "d" finished'))]
    s.expect = [("CLAIM", 6, 31)]
    out.append(s)

    # row 7: the prompted output file is missing at return
    s = Scenario("completed_output_missing", "row 7", False)
    s.steps = launch(0, 7) + [sub(7, 2, f"Review it and write your report to {missing}"),
                              (("sub", 7), 40, text(40, aid(7), "Done.")),
                              ("main", 41, notice(41, aid(7), "toolu_launch7", "completed",
                                                  'Agent "d" finished'))]
    s.expect = [("CLAIM", 7, 41)]
    out.append(s)

    # a clean return with its output present: nothing
    s = Scenario("clean_return", "control", True)
    s.steps = launch(0, 8) + [sub(8, 2, f"write your report to {present}"),
                              (("sub", 8), 40, text(40, aid(8), "Done.")),
                              ("main", 41, notice(41, aid(8), "toolu_launch8", "completed",
                                                  'Agent "d" finished'))]
    out.append(s)

    # row 10: completed but waiting on its own background work: native says so
    s = Scenario("waiting_on_background", "row 10", True)
    s.steps = launch(0, 9) + [sub(9, 2, "do the task"), (
        "main", 50, notice(50, aid(9), "toolu_launch9", "completed",
                           'Agent "d" is waiting on its own background work'))]
    out.append(s)

    # #92716: killed background subagent, no SubagentStop, only the killed notice, 1 write
    s = Scenario("killed_notice_with_changes", "#92716", False)
    s.steps = launch(0, 10) + [sub(10, 2, "do the task")] + writes(5, 10, 1) + [
        ("main", 90, notice(90, aid(10), "toolu_launch10", "killed",
                            'Agent "d" was stopped by Claude'))]
    s.expect = [("FAILED", 10, 90)]
    out.append(s)

    # healthy but quiet: one 700 s Bash call, then a clean return (the p99.9 healthy gap)
    s = Scenario("long_healthy_quiet", "control", True)
    s.steps = launch(0, 11) + [sub(11, 2, "run the suite"),
                               (("sub", 11), 5, tool_use(5, aid(11), "toolu_q", "Bash",
                                                         {"command": "make test"})),
                               (("sub", 11), 705, tool_result(705, aid(11), "toolu_q", "ok")),
                               (("sub", 11), 710, text(710, aid(11), "Done.")),
                               ("main", 711, notice(711, aid(11), "toolu_launch11", "completed",
                                                    'Agent "d" finished'))]
    out.append(s)

    # row 11: a background session goes blocked (waiting for input)
    s = Scenario("background_blocked", "row 11", True)
    s.steps = [("job", 0, "working"), ("job", 120, "blocked")]
    s.expect = [("STATE", None, 120)]
    out.append(s)
    return out


def _apply(step, root: Path, jobs: Path) -> None:
    role, t, entry = step
    if role == "job":
        job = jobs / "00000000"
        job.mkdir(parents=True, exist_ok=True)
        (job / "state.json").write_text(json.dumps({"state": entry, "sessionId": SESSION}),
                                        encoding="utf-8")
        return
    main = root / SLUG / f"{SESSION}.jsonl"
    sub_dir = main.parent / SESSION / "subagents"
    path = main if role == "main" else sub_dir / f"agent-{aid(role[1])}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def run_session(scenario: Scenario, tmp: Path, interval: float) -> list[tuple[float, dict]]:
    from agent_postmortem import live

    root = tmp / scenario.name / "projects"
    jobs = tmp / scenario.name / "jobs"
    main = root / SLUG / f"{SESSION}.jsonl"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(json.dumps(user(0, None, "start")) + "\n", encoding="utf-8")
    steps = sorted(scenario.steps, key=lambda s: s[1])
    end = steps[-1][1] if steps else 0
    vt = 0.0
    follower = live.SessionFollower(main, jobs_root=jobs, now=lambda: T0.timestamp() + vt,
                                    caller_session=None)
    emitted: list[tuple[float, dict]] = []
    index = 0
    while vt <= end + 6 * interval:
        while index < len(steps) and steps[index][1] <= vt:
            _apply(steps[index], root, jobs)
            index += 1
        for event in follower.poll():
            emitted.append((vt, event))
        vt += interval
    follower.close()
    return emitted


# --- workflow scenarios ---------------------------------------------------------------


def workflow_scenarios(tmp: Path) -> list[Scenario]:
    missing = str(tmp / "missing-wf" / "review.md")
    out = []
    for name, kind, native, result, expect_claim in (
        ("workflow_ok_false", "row 4", False, {"ok": False, "tests_failed": 2}, True),
        ("workflow_report_absent", "row 5", False,
         {"status": "done", "report_path": missing}, True),
        ("workflow_clean", "control", True, {"ok": True}, False),
    ):
        s = Scenario(name, kind, native, mode="workflow")
        s.steps = [("result", 30, result)]
        s.expect = [("CLAIM", 1, 30)] if expect_claim else []
        out.append(s)
    s = Scenario("workflow_killed_with_changes", "row 1 (restart is native, the files are not)",
                 False, mode="workflow")
    s.steps = [("kill", 640, None)]
    s.expect = [("KILL", 1, 640)]
    out.append(s)
    return out


def run_workflow(scenario: Scenario, tmp: Path, interval: float) -> list[tuple[float, dict]]:
    from agent_postmortem import watch

    run_dir = tmp / scenario.name / SLUG / SESSION / "subagents" / "workflows" / "wf_00000000-756"
    run_dir.mkdir(parents=True, exist_ok=True)
    agent, key = aid(1), "v2:" + "0" * 64
    (run_dir / f"agent-{agent}.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": ""}), encoding="utf-8")
    journal = run_dir / "journal.jsonl"
    transcript = run_dir / f"agent-{agent}.jsonl"
    started = {"type": "started", "timestamp": iso(0), "key": key, "agentId": agent,
               "label": "task", "phase": "P"}
    journal.write_text(json.dumps(started) + "\n", encoding="utf-8")
    lines = [user(0, None, "do the task")]
    for i in range(2):
        lines.append(tool_use(5 + i, None, f"toolu_w{i}", "Write",
                              {"file_path": f"/tmp/live-replay/wf/out{i}.md", "content": "x"}))
        lines.append(tool_result(5.5 + i, None, f"toolu_w{i}", "ok"))
    transcript.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")

    step_kind, t_step, payload = scenario.steps[0]
    clock = {"t": 0.0}
    applied = {"done": False}

    def apply_if_due():
        if applied["done"] or clock["t"] < t_step:
            return
        applied["done"] = True
        if step_kind == "result":
            with open(journal, "a", encoding="utf-8") as h:
                h.write(json.dumps({"type": "result", "timestamp": iso(t_step), "key": key,
                                    "agentId": agent, "result": payload}) + "\n")
        else:
            with open(transcript, "a", encoding="utf-8") as h:
                marker = user(t_step, None, [{"type": "text", "text": KILL_TOOL}])
                h.write(json.dumps(marker) + "\n")
            second = aid(2)
            (run_dir / f"agent-{second}.meta.json").write_text(
                json.dumps({"agentType": "general-purpose", "description": ""}), encoding="utf-8")
            (run_dir / f"agent-{second}.jsonl").write_text(
                json.dumps(user(t_step + 1, None, "do the task")) + "\n", encoding="utf-8")
            with open(journal, "a", encoding="utf-8") as h:
                restart = {**started, "agentId": second, "timestamp": iso(t_step + 1)}
                h.write(json.dumps(restart) + "\n")
                h.write(json.dumps({"type": "result", "timestamp": iso(t_step + 30), "key": key,
                                    "agentId": second, "result": {"ok": True}}) + "\n")

    base = T0.timestamp()

    def fake_now():
        return base + clock["t"]

    def fake_sleep(seconds):
        clock["t"] += seconds
        apply_if_due()

    stamped: list[tuple[float, dict]] = []

    class Stamping(io.StringIO):
        """Each printed line is stamped with the virtual time it was printed at."""

        def write(self, text):
            for line in text.splitlines():
                if line.strip():
                    stamped.append((clock["t"], json.loads(line)))
            return len(text)

    old_now, old_sleep = watch._now, watch._sleep
    watch._now, watch._sleep = fake_now, fake_sleep
    try:
        watch.watch_run(str(run_dir), tmp, interval=interval, max_hours=1.0, done_grace=0,
                        json_mode=True, out=Stamping(), err=io.StringIO())
    finally:
        watch._now, watch._sleep = old_now, old_sleep
    return stamped


def evaluate(interval: float = 2.0) -> dict:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        os.environ["XDG_STATE_HOME"] = str(tmp / "state")
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        from agent_postmortem import live

        names = live._Names()
        rows = []
        for scenario in session_scenarios(tmp) + workflow_scenarios(tmp):
            if scenario.mode == "session":
                emitted = run_session(scenario, tmp, interval)
            else:
                emitted = run_workflow(scenario, tmp, interval)
            signal = [(t, e) for t, e in emitted
                      if e["event"] in ("KILL", "FAILED", "CLAIM", "STATE")]
            matched, latencies = 0, []
            used = set()
            for kind, n, t_evidence in scenario.expect:
                alias = names(aid(n)) if n is not None else None
                for i, (t, e) in enumerate(signal):
                    if i in used or e["event"] != kind:
                        continue
                    if alias is not None and e.get("agent") != alias:
                        continue
                    used.add(i)
                    matched += 1
                    latencies.append(max(0.0, t - t_evidence))
                    break
            rows.append({
                "scenario": scenario.name, "kind": scenario.kind, "native": scenario.native,
                "expected": len(scenario.expect), "emitted": len(signal), "matched": matched,
                "latencies": latencies,
            })
    expected = sum(r["expected"] for r in rows)
    emitted = sum(r["emitted"] for r in rows)
    matched = sum(r["matched"] for r in rows)
    all_latencies = [x for r in rows for x in r["latencies"]]
    return {
        "interval_seconds": interval, "scenarios": rows,
        "recall": matched / expected if expected else 1.0,
        "precision": matched / emitted if emitted else 1.0,
        "median_time_to_detect_seconds": (
            statistics.median(all_latencies) if all_latencies else None),
        "max_time_to_detect_seconds": max(all_latencies) if all_latencies else None,
        "false_alarms": emitted - matched,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args(argv)
    with contextlib.redirect_stderr(io.StringIO()):
        result = evaluate(args.interval)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"recall {result['recall']:.2f}  precision {result['precision']:.2f}  "
          f"median time to detect {result['median_time_to_detect_seconds']}s  "
          f"max {result['max_time_to_detect_seconds']}s  (poll interval {args.interval}s)")
    print(f"{'scenario':38} {'shaped like':34} {'native':6} exp emit hit  ttd")
    for r in result["scenarios"]:
        ttd = ",".join(f"{x:g}" for x in r["latencies"]) or "-"
        print(f"{r['scenario']:38} {r['kind']:34} {'yes' if r['native'] else 'no':6} "
              f"{r['expected']:3} {r['emitted']:4} {r['matched']:3}  {ttd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
