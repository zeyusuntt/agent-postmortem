"""`status` says how each of the session's workflows ended (docs/DESIGN.md 4.14.4, 0.4).

Field report 2026-10-30, "status lists every workflow with a watch hint and no verdict": a
session's 27 workflows each printed a WORKFLOW line with a watch command, including ones that
ended days ago, and the one CLAIM line was easy to miss. A workflow's end is in the main
transcript already (its task notification, under the task id its launch result names), so
status reads it without loading the run: open workflows keep their watch command, a failed,
killed or stopped one points at `run`, and completed ones fold into one count line.
"""

from __future__ import annotations

import json
from datetime import timedelta

from live_helpers import FAKE_UUID, notification, tool_result, tool_use, user, write_jsonl
from test_live_session import SLUG, T0

from agent_postmortem import live, state
from agent_postmortem.cli import main

OPEN, FAILED, DONE_A, DONE_B = (f"wf_00000000-c4{n}" for n in range(4))


def _launch(run_id: str, n: int, at) -> list[dict]:
    tool_id = f"toolu_wf{n}"
    return [
        tool_use(None, at, tool_id, "Workflow", {"script": "x"}),
        tool_result(None, at, tool_id, "launched",
                    toolUseResult={"runId": run_id, "status": "async_launched",
                                   "taskId": f"w{n:08d}"}),
    ]


def _session(tmp_path):
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    entries = [user(None, T0, "start")]
    for n, run_id in enumerate((OPEN, FAILED, DONE_A, DONE_B)):
        entries += _launch(run_id, n, T0 + timedelta(seconds=n))
    entries += [
        notification(T0 + timedelta(minutes=5), "w00000001", "toolu_wf1", "failed", "Workflow failed"),
        notification(T0 + timedelta(minutes=6), "w00000002", "toolu_wf2", "completed", "Workflow done"),
        notification(T0 + timedelta(minutes=7), "w00000003", "toolu_wf3", "completed", "Workflow done"),
    ]
    write_jsonl(main_path, entries)
    return main_path


def _workflow_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("  WORKFLOW")]


def test_open_and_failed_workflows_are_listed_first_and_completed_ones_fold(tmp_path, capsys):
    main_path = _session(tmp_path)
    assert main(["status", str(main_path)]) == 0
    lines = _workflow_lines(capsys.readouterr().out)
    assert len(lines) == 3, lines
    assert OPEN in lines[0] and f"agent-postmortem watch {OPEN}" in lines[0]
    assert FAILED in lines[1] and "failed" in lines[1] and f"agent-postmortem run {FAILED}" in lines[1]
    assert lines[2].split()[0] == "WORKFLOWS" and "2 completed" in lines[2]
    assert DONE_A not in lines[2] and DONE_B not in lines[2]


def test_json_names_each_workflows_end_and_keeps_an_open_ones_shape(tmp_path, capsys):
    main_path = _session(tmp_path)
    assert main(["status", str(main_path), "--json"]) == 0
    workflows = {w["run_id"]: w for w in json.loads(capsys.readouterr().out)["workflows"]}
    assert workflows[OPEN] == {"run_id": OPEN, "next_command": f"agent-postmortem watch {OPEN}"}
    assert workflows[FAILED]["ended"] == "failed"
    assert workflows[FAILED]["next_command"] == f"agent-postmortem run {FAILED}"
    assert workflows[DONE_A]["ended"] == workflows[DONE_B]["ended"] == "completed"


def test_facts_cached_before_workflow_ends_were_kept_are_read_again(tmp_path, capsys):
    main_path = _session(tmp_path)
    assert main(["status", str(main_path)]) == 0
    capsys.readouterr()
    path = live.SessionFollower(main_path, caller_session=None).store_path
    with state.cache_transaction(path) as data:
        entry = data["transcripts"][str(main_path)]
        entry["facts"].pop("workflow_tasks", None)
        entry.pop("facts_version", None)
    assert main(["status", str(main_path)]) == 0
    lines = _workflow_lines(capsys.readouterr().out)
    assert any("failed" in line for line in lines) and "2 completed" in lines[-1], lines


# --- batch 3 review ----------------------------------------------------------------------------


def _write(tmp_path, entries):
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [user(None, T0, "start"), *entries])
    return main_path


def _ends(capsys, main_path) -> dict:
    assert main(["status", str(main_path), "--json"]) == 0
    return {w["run_id"]: w.get("ended") for w in json.loads(capsys.readouterr().out)["workflows"]}


def test_a_resumed_workflow_is_judged_by_its_latest_launch(tmp_path, capsys):
    first = _launch(OPEN, 0, T0)
    resumed = _launch(OPEN, 7, T0 + timedelta(minutes=10))
    done = notification(T0 + timedelta(minutes=5), "w00000000", "toolu_wf0", "completed", "d")
    main_path = _write(tmp_path, [*first, done, *resumed])
    assert _ends(capsys, main_path) == {OPEN: None}  # the resumed launch is still open
    with main_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(notification(T0 + timedelta(minutes=20), "w00000007",
                                             "toolu_wf7", "failed", "f")) + "\n")
    assert _ends(capsys, main_path) == {OPEN: "failed"}


def test_a_notice_must_name_the_launchs_tool_use_id_too(tmp_path, capsys):
    stray = notification(T0 + timedelta(minutes=5), "w00000003", "toolu_unrelated", "completed",
                         "d")
    main_path = _write(tmp_path, [*_launch(DONE_B, 3, T0), stray])
    assert _ends(capsys, main_path) == {DONE_B: None}


def test_a_cancelled_workflow_reads_stopped(tmp_path, capsys):
    note = notification(T0 + timedelta(minutes=5), "w00000001", "toolu_wf1", "cancelled", "c")
    main_path = _write(tmp_path, [*_launch(FAILED, 1, T0), note])
    assert _ends(capsys, main_path) == {FAILED: "stopped"}


def test_a_synchronous_launch_result_with_an_end_is_that_end(tmp_path, capsys):
    entries = []
    for n, (run_id, word) in enumerate(((OPEN, "completed"), (FAILED, "failed"),
                                        (DONE_A, "killed"), (DONE_B, "stopped"))):
        tool_id = f"toolu_sync{n}"
        entries += [
            tool_use(None, T0, tool_id, "Workflow", {"script": "x"}),
            tool_result(None, T0, tool_id, "done",
                        toolUseResult={"runId": run_id, "status": word, "taskId": f"s{n:08d}"}),
        ]
    main_path = _write(tmp_path, entries)
    assert _ends(capsys, main_path) == {OPEN: "completed", FAILED: "failed",
                                        DONE_A: "killed", DONE_B: "stopped"}


def test_the_completed_line_points_at_the_session_status_lists(tmp_path, capsys):
    main_path = _session(tmp_path)
    assert main(["status", str(main_path)]) == 0
    out = capsys.readouterr().out
    session = out.split()[2].rstrip(":")
    line = next(line for line in out.splitlines() if line.startswith("  WORKFLOWS"))
    assert f"agent-postmortem status {session} --json" in line, line
    assert "--here" not in line


def test_a_mismatched_notice_never_replaces_the_real_end_in_either_order(tmp_path, capsys):
    # batch 4 review: notices are kept by task id; a stray one naming another tool-use id
    # overwrote, or was deduplicated against, the launch's own notice
    real = notification(T0 + timedelta(minutes=5), "w00000001", "toolu_wf1", "failed", "f")
    stray = notification(T0 + timedelta(minutes=5), "w00000001", "toolu_other", "completed", "c")
    for name, order in (("real-first", [real, stray]), ("stray-first", [stray, real])):
        main_path = _write(tmp_path / name, [*_launch(FAILED, 1, T0), *order])
        assert _ends(capsys, main_path) == {FAILED: "failed"}, name
