"""N8b K4 (docs/DESIGN.md 4.13.8, 4.11, 4.7, 5.3b; .reviews/v0.3/N8-plan.md rev 3): the two N5
release blockers and the two rescue wording fixes.

K4.1  STEPS never prints `<none>`; a unit with no step path gets both whole-tree commands.
K4.2  the fan-out caveat prints only when two attempts of different keys overlapped in time.
K4.3  `rescue plan` and `rescue status` with no unit: one sentence rule, one exit rule.
K4.4  the PATHS surfaces: the AGENT table column, scan `paths_at_risk` (and its completeness
      flag), and the relaunch brief's `already exists`.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import reader, report, rescue
from agent_postmortem.redact import Redactor
from tests.n5b_helpers import model_attempt, model_workflow_run
from tests.n5c1_helpers import Att, Step, bash_step, build_run, write_step
from tests.n5c2_helpers import age_files, exit_of, run_cli
from tests.test_field_v03 import (
    _aid,
    _assistant_text,
    _kill,
    _no_config,
    _user,
    _wf_dir,
    _write_jsonl,
)
from tests.test_field_v03_more import _backdate
from tests.test_rescue import _build_killed_then_completed, _git

# =============================================================================================
# K4.1: STEPS for a unit with no path
# =============================================================================================


def _lone_killed_unit(tmp_path, monkeypatch, suffix: str, *, live: bool) -> Path:
    """One key, one attempt, killed, no tool call at all: no path and no checked repository."""
    root = tmp_path / "root"
    run_dir = _wf_dir(root, f"wf_00000000-{suffix}")
    agent_id = _aid(int(suffix, 16))
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:only", "agentId": agent_id, "label": "lone",
         "phase": "Implement"},
    ])
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Look into it.", base, agent_id),
        _kill(base + timedelta(seconds=1), agent_id),
    ])
    _no_config(monkeypatch, tmp_path)
    if not live:
        _backdate(run_dir, datetime.now(timezone.utc) - timedelta(days=1))
    return run_dir


def _steps_block(out: str) -> list[str]:
    lines = out.splitlines()
    start = lines.index("  STEPS")
    block = []
    for line in lines[start + 1:]:
        if not line.startswith("   "):
            break
        block.append(line)
    return block


def test_k4_1_no_path_unit_gets_both_whole_tree_commands(tmp_path, monkeypatch, capsys):
    run_dir = _lone_killed_unit(tmp_path, monkeypatch, "8e1", live=False)
    code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir)])
    assert "<none>" not in out
    block = _steps_block(out)
    assert block[0] == "   1. [look]   git status --short"
    assert block[1] == "   2. [look]   git diff"
    assert [line[3:6] for line in block[2:]] == ["3. ", "4. ", "5. ", "6. "]
    assert all("[decide]" in line for line in block[2:])
    assert code == 4


def test_k4_1_possibly_live_no_path_unit_gets_exactly_the_two_look_commands(
    tmp_path, monkeypatch, capsys,
):
    run_dir = _lone_killed_unit(tmp_path, monkeypatch, "8e2", live=True)
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir)])
    assert out.startswith("POSSIBLY LIVE")
    block = _steps_block(out)
    assert block == ["   1. [look]   git status --short", "   2. [look]   git diff"]
    assert "<none>" not in out


@pytest.mark.parametrize("live", [False, True])
def test_k4_1_json_steps_carry_both_commands_as_non_null_strings(
    tmp_path, monkeypatch, capsys, live,
):
    run_dir = _lone_killed_unit(tmp_path, monkeypatch, "8e3" if not live else "8e4", live=live)
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir), "--json"])
    steps = json.loads(out)["units"][0]["steps"]
    assert steps[0]["command"] == "git status --short"
    assert steps[1]["command"] == "git diff"
    assert [step["grade"] for step in steps[:2]] == ["look", "look"]
    assert len(steps) == (2 if live else 6)


def test_k4_1_checked_clean_tree_json_also_carries_whole_tree_commands(
    tmp_path, monkeypatch, capsys,
):
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "everything committed"], repo)
    _no_config(monkeypatch, tmp_path)
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir), "--json"])
    steps = json.loads(out)["units"][0]["steps"]
    assert steps[0]["command"] == "git status --short"
    assert steps[1]["command"] == "git diff"


def test_k4_1_a_unit_with_dirty_paths_keeps_its_pathspec(tmp_path, monkeypatch, capsys):
    """Regression pin: the pathspec form is unchanged when there is a path to name."""
    run_dir, _repo, _base = _build_killed_then_completed(tmp_path)
    _no_config(monkeypatch, tmp_path)
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir)])
    block = _steps_block(out)
    assert block[0].startswith("   1. [look]   git status --short -- ")
    assert block[1].startswith("   2. [look]   git diff -- ")


def test_k4_1_checked_clean_tree_text_is_unchanged(tmp_path, monkeypatch, capsys):
    """Regression pin: the checked, nothing-dirty text keeps its sentence (4.11)."""
    run_dir, repo, _base = _build_killed_then_completed(tmp_path)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "everything committed"], repo)
    _no_config(monkeypatch, tmp_path)
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir)])
    assert "no path is dirty now" in out
    assert "<none>" not in out


# =============================================================================================
# K4.2: the fan-out caveat
# =============================================================================================

CAVEAT = "parallel fan-out"


def _two_key_run(
    tmp_path, monkeypatch, suffix: str, *, left: tuple[int, int], right: tuple[int, int] | None,
    stamped: bool = True,
) -> Path:
    """Key `v2:left` is killed (transcript [left[0], left[1]] seconds after BASE) and key
    `v2:right` completes over [right[0], right[1]]. `stamped=False` writes no timestamps."""
    root = tmp_path / "root"
    run_dir = _wf_dir(root, f"wf_00000000-{suffix}")
    a1, a2 = _aid(int(suffix, 16)), _aid(int(suffix, 16) + 1)
    base = datetime.now(timezone.utc) - timedelta(hours=2)

    def at(seconds: int) -> datetime:
        return base + timedelta(seconds=seconds)

    def strip(entries: list[dict]) -> list[dict]:
        if stamped:
            return entries
        return [{k: v for k, v in entry.items() if k != "timestamp"} for entry in entries]

    journal = [{"type": "started", "key": "v2:left", "agentId": a1, "label": "left",
                "phase": "Implement"}]
    if right is not None:
        journal.append({"type": "started", "key": "v2:right", "agentId": a2, "label": "right",
                        "phase": "Implement"})
        journal.append({"type": "result", "key": "v2:right", "agentId": a2,
                        "result": {"ok": True}})
    _write_jsonl(run_dir / "journal.jsonl", journal)
    _write_jsonl(run_dir / f"agent-{a1}.jsonl", strip([
        _user("Left.", at(left[0]), a1),
        _assistant_text("Working.", at(left[0] + 1), a1),
        _kill(at(left[1]), a1),
    ]))
    if right is not None:
        _write_jsonl(run_dir / f"agent-{a2}.jsonl", strip([
            _user("Right.", at(right[0]), a2),
            _assistant_text("Done.", at(right[1]), a2),
        ]))
    _no_config(monkeypatch, tmp_path)
    _backdate(run_dir, datetime.now(timezone.utc) - timedelta(days=1))
    return run_dir


def _brief_text(run_dir, capsys) -> str:
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir)])
    assert "BRIEF FOR A RELAUNCH" in out
    return out


def test_k4_2_caveat_prints_when_two_keys_overlapped(tmp_path, monkeypatch, capsys):
    run_dir = _two_key_run(tmp_path, monkeypatch, "8e6", left=(0, 600), right=(1, 300))
    assert CAVEAT in _brief_text(run_dir, capsys)


def test_k4_2_no_caveat_when_the_keys_ran_one_after_the_other(tmp_path, monkeypatch, capsys):
    run_dir = _two_key_run(tmp_path, monkeypatch, "8e7", left=(0, 100), right=(500, 600))
    assert CAVEAT not in _brief_text(run_dir, capsys)


def test_k4_2_no_caveat_for_a_single_key(tmp_path, monkeypatch, capsys):
    run_dir = _two_key_run(tmp_path, monkeypatch, "8e8", left=(0, 100), right=None)
    assert CAVEAT not in _brief_text(run_dir, capsys)


def test_k4_2_unknown_timestamps_never_trigger_it(tmp_path, monkeypatch, capsys):
    run_dir = _two_key_run(
        tmp_path, monkeypatch, "8e9", left=(0, 600), right=(1, 300), stamped=False,
    )
    assert CAVEAT not in _brief_text(run_dir, capsys)


def _stamped(number: int, key: str | None, first: int | None, last: int | None):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return dataclasses.replace(
        model_attempt(number, key=key),
        first_ts=None if first is None else base + timedelta(seconds=first),
        last_ts=None if last is None else base + timedelta(seconds=last),
    )


@pytest.mark.parametrize("a,b,expected", [
    ((0, 100), (50, 150), True),
    ((0, 100), (100, 150), True),  # the closed intervals touch
    ((0, 100), (101, 150), False),
    ((50, 150), (0, 49), False),
    ((0, 1000), (10, 20), True),  # one inside the other
    ((0, None), (10, 20), False),  # unknown end
    ((None, 100), (10, 20), False),  # unknown start
])
def test_k4_2_overlap_helper_table(a, b, expected):
    run = model_workflow_run([_stamped(1, "v2:a", *a), _stamped(2, "v2:b", *b)])
    assert rescue._fan_out_overlap(run) is expected


def test_k4_2_overlap_needs_different_keys():
    """Two attempts of one key (a retry) overlapping is not a fan-out."""
    run = model_workflow_run([
        _stamped(1, "v2:a", 0, 100), dataclasses.replace(_stamped(2, "v2:a", 50, 150), ordinal=2),
    ])
    assert rescue._fan_out_overlap(run) is False


def test_k4_2_a_session_never_has_a_fan_out():
    run = model_workflow_run([_stamped(1, "v2:a", 0, 100), _stamped(2, "v2:b", 50, 150)])
    run.kind = "session"
    assert rescue._fan_out_overlap(run) is False


# =============================================================================================
# K4.3: rescue plan and rescue status with no unit
# =============================================================================================

FINISHED = "NOTHING TO RESCUE: every attempt completed."
YET = "NOTHING TO RESCUE YET: the run has not finished and no attempt was killed or failed."


def _empty_unit_run(tmp_path, monkeypatch, shape: str, suffix: str) -> Path:
    parent, journal, aged = {
        "finished": ("completed", "completed", True),
        "fresh_unfinished": ("running", "open", False),
        "stale_unfinished": ("running", "open", True),
        "unknown_terminal": ("weird", "completed", True),
    }[shape]
    run_dir = build_run(
        tmp_path, monkeypatch, f"wf_00000000-{suffix}", [Att(1, journal=journal)], parent=parent,
    )
    if aged:
        age_files(tmp_path / "root")
    return run_dir


EMPTY_SHAPES = [
    pytest.param("finished", "8f1", 0, FINISHED, id="finished all completed"),
    pytest.param("fresh_unfinished", "8f2", 4, YET, id="fresh unfinished (possibly live)"),
    pytest.param("stale_unfinished", "8f3", 4, YET, id="stale unfinished, terminal none"),
    pytest.param("unknown_terminal", "8f4", 4, YET, id="unknown terminal"),
]


@pytest.mark.parametrize("shape,suffix,code,sentence", EMPTY_SHAPES)
def test_k4_3_status_text_and_exit(tmp_path, monkeypatch, capsys, shape, suffix, code, sentence):
    run_dir = _empty_unit_run(tmp_path, monkeypatch, shape, suffix)
    got_code, out, _err = run_cli(capsys, ["rescue", "status", str(run_dir)])
    assert sentence in out
    assert (FINISHED in out) == (shape == "finished")
    assert got_code == code


@pytest.mark.parametrize("shape,suffix,code,sentence", EMPTY_SHAPES)
def test_k4_3_status_json_exit_equals_the_process_status(
    tmp_path, monkeypatch, capsys, shape, suffix, code, sentence,
):
    run_dir = _empty_unit_run(tmp_path, monkeypatch, shape, suffix)
    got_code, out, _err = run_cli(capsys, ["rescue", "status", str(run_dir), "--json"])
    payload = json.loads(out)
    assert payload["units"] == []
    assert payload["exit_code"] == got_code == code


@pytest.mark.parametrize("shape,suffix,code,sentence", EMPTY_SHAPES)
def test_k4_3_plan_text_json_and_exit(tmp_path, monkeypatch, capsys, shape, suffix, code, sentence):
    """`rescue plan` already followed the rule; the same fixtures pin that it still does, and
    that the process status, the JSON exit code, and the sentence agree with status."""
    run_dir = _empty_unit_run(tmp_path, monkeypatch, shape, suffix)
    got_code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir)])
    assert got_code == code
    if shape == "fresh_unfinished":
        # FB3: a live run with no unit has nothing to protect yet
        assert out.startswith("NOTHING TO RESCUE YET: the run may still be running")
        assert FINISHED not in out
    else:
        assert sentence in out
    json_code = exit_of(["rescue", "plan", str(run_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["nothing_to_rescue"] is True
    assert payload["exit_code"] == json_code == code


def test_k4_3_status_and_plan_use_one_predicate(monkeypatch):
    """The sentence and the exit come from the same helper for both commands."""
    # N8 fix 1, S2: a bare model attempt has no journal state and no generation, so 4.13.1 counts
    # it at risk; the risk population is stubbed empty to isolate the finished/completed clauses
    from agent_postmortem import ledger

    monkeypatch.setattr(ledger, "risk_population", lambda run: [])
    ok = model_workflow_run([model_attempt(1)], terminal="completed")
    ok.finished = True
    assert rescue.nothing_to_rescue_ok(ok) is True
    unfinished = model_workflow_run([model_attempt(1)], terminal="none")
    unfinished.finished = False
    assert rescue.nothing_to_rescue_ok(unfinished) is False
    unknown = model_workflow_run([model_attempt(1, outcome="unknown")], terminal="completed")
    unknown.finished = True
    assert rescue.nothing_to_rescue_ok(unknown) is False
    session = model_workflow_run([model_attempt(1)], terminal=None)
    session.finished = None
    assert rescue.nothing_to_rescue_ok(session) is False


# =============================================================================================
# K4.4: the PATHS surfaces
# =============================================================================================


def _ws(tmp_path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return workspace


def _multiedit(tool_id: str, path, cwd, *, result="ok", error=False) -> Step:
    return Step(
        tool_id, "MultiEdit", {"file_path": str(path), "edits": []}, result, error, str(cwd),
    )


def _failed_write(tool_id: str, path, cwd) -> Step:
    return Step(tool_id, "Write", {"file_path": str(path)}, "disk full", True, str(cwd))


def _paths_cell(run, index: int = 0) -> str:
    lines = report._agent_table(run, Redactor(enabled=False))
    header = lines[0]
    return lines[1 + index][header.index("PATHS"):header.index("COMMITS")].strip()


def _loaded(tmp_path, monkeypatch, suffix: str, attempts: list[Att], *, parent="completed"):
    run_dir = build_run(
        tmp_path, monkeypatch, f"wf_00000000-{suffix}", attempts, parent=parent,
    )
    return reader.load_run(run_dir)


def test_k4_4_agent_table_counts_a_facts_write(tmp_path, monkeypatch):
    """Regression pin: a plain Write counts one, complete ledger, no marker."""
    ws = _ws(tmp_path)
    run = _loaded(tmp_path, monkeypatch, "8f5", [
        Att(1, journal="completed", steps=[write_step("t1", ws / "a.txt", ws)]),
    ])
    assert _paths_cell(run) == "1"


def test_k4_4_agent_table_counts_a_shell_only_write(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    run = _loaded(tmp_path, monkeypatch, "8f6", [
        Att(1, journal="completed", steps=[bash_step("t1", f"echo hi > {ws}/b.txt", ws)]),
    ])
    # every shell call leaves its ledger partial (C-15), so the count is a lower bound
    assert run.attempts[0].ledger.completeness == "partial"
    assert _paths_cell(run) == "1+"


def test_k4_4_agent_table_counts_a_multiedit(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    run = _loaded(tmp_path, monkeypatch, "8f7", [
        Att(1, journal="completed", steps=[_multiedit("t1", ws / "c.txt", ws)]),
    ])
    assert _paths_cell(run) == "1"


def test_k4_4_agent_table_counts_an_attempted_write(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    run = _loaded(tmp_path, monkeypatch, "8f8", [
        Att(1, journal="failed", steps=[_failed_write("t1", ws / "d.txt", ws)]),
    ])
    assert _paths_cell(run) == "1"


def test_k4_4_agent_table_marks_a_partial_ledger_as_a_lower_bound(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    run = _loaded(tmp_path, monkeypatch, "8f9", [
        Att(1, journal="failed", steps=[
            write_step("t1", ws / "a.txt", ws),
            bash_step("t2", "make build", None),  # no cwd: the shell context is unverified
        ]),
    ])
    assert run.attempts[0].ledger.completeness == "partial"
    assert _paths_cell(run) == "1+"


def test_k4_4_a_partial_ledger_with_no_path_reads_zero_plus(tmp_path, monkeypatch):
    run = _loaded(tmp_path, monkeypatch, "8fa", [
        Att(1, journal="failed", steps=[bash_step("t1", "make build", None)]),
    ])
    assert _paths_cell(run) == "0+"


def test_k4_4_agent_table_unions_one_path_once_across_attempts_and_sources(
    tmp_path, monkeypatch,
):
    ws = _ws(tmp_path)
    run = _loaded(tmp_path, monkeypatch, "8fb", [
        Att(1, journal="failed", steps=[write_step("t1", ws / "a.txt", ws)]),
        Att(2, journal="completed", steps=[
            write_step("t1", ws / "a.txt", ws), write_step("t2", ws / "e.txt", ws),
        ]),
    ])
    assert _paths_cell(run) == "2"


def test_k4_4_agent_table_question_mark_when_no_ledger_and_no_facts():
    run = model_workflow_run([model_attempt(1)])
    assert run.attempts[0].ledger is None and run.attempts[0].facts is None
    assert _paths_cell(run) == "?"


def test_k4_4_agent_table_question_mark_for_a_missing_transcript(tmp_path, monkeypatch):
    run = _loaded(tmp_path, monkeypatch, "8fc", [Att(1, journal="completed")])
    (run.path / f"agent-{run.attempts[0].agent_id}.jsonl").unlink()
    run = reader.load_run(run.path)
    assert _paths_cell(run) == "?"


# --- scan paths_at_risk and its completeness ---------------------------------------------------


def _scan_rows(tmp_path, capsys, *extra) -> list[dict]:
    code = exit_of(["scan", "--root", str(tmp_path / "root"), "--json", *extra])
    assert code in (0, 3, 4)
    rows = json.loads(capsys.readouterr().out)["rows"]
    return [row for row in rows if row["kind"] == "workflow"]


def test_k4_4_scan_counts_a_completed_attempt_in_a_failed_generation(
    tmp_path, monkeypatch, capsys,
):
    ws = _ws(tmp_path)
    _loaded(tmp_path, monkeypatch, "8fd", [
        Att(1, journal="completed", steps=[write_step("t1", ws / "a.txt", ws)]),
    ], parent="failed")
    (row,) = _scan_rows(tmp_path, capsys)
    assert row["paths_at_risk"] == 1
    assert row["paths_at_risk_complete"] is True


def test_k4_4_scan_counts_a_shell_write_and_a_multiedit_as_a_lower_bound(tmp_path, monkeypatch, capsys):
    ws = _ws(tmp_path)
    _loaded(tmp_path, monkeypatch, "8fe", [
        Att(1, journal="failed", steps=[
            bash_step("t1", f"echo hi > {ws}/b.txt", ws), _multiedit("t2", ws / "c.txt", ws),
        ]),
    ])
    (row,) = _scan_rows(tmp_path, capsys)
    assert row["paths_at_risk"] == 2
    assert row["paths_at_risk_complete"] is False  # a shell call: the ledger is partial


def test_k4_4_scan_partial_ledger_row_is_a_lower_bound(tmp_path, monkeypatch, capsys):
    ws = _ws(tmp_path)
    _loaded(tmp_path, monkeypatch, "8e0", [
        Att(1, journal="failed", steps=[
            write_step("t1", ws / "a.txt", ws), bash_step("t2", "make build", None),
        ]),
    ])
    (row,) = _scan_rows(tmp_path, capsys)
    assert row["paths_at_risk"] == 1
    assert row["paths_at_risk_complete"] is False


def test_k4_4_scan_row_with_no_risk_population_is_complete_and_zero(
    tmp_path, monkeypatch, capsys,
):
    _loaded(tmp_path, monkeypatch, "8f0", [Att(1, journal="completed")])
    (row,) = _scan_rows(tmp_path, capsys)
    assert row["paths_at_risk"] == 0
    assert row["paths_at_risk_complete"] is True


def _runtime_kill_with_a_partial_ledger(tmp_path, monkeypatch, suffix: str):
    """Attempt 1 wrote one path and ran a shell command with no known directory, then was killed
    by the runtime and restarted; attempt 2 completed."""
    from tests.n5b_helpers import Spec, build_workflow

    return build_workflow(tmp_path, monkeypatch, f"wf_00000000-{suffix}", [
        Spec("v2:x", 1, journal="superseded", kill=True, silence=400, tools=[
            ("t1", "Write", {"file_path": str(_ws(tmp_path) / "a.txt")}, "ok", False),
            ("t2", "Bash", {"command": "make build"}, "ok", False),
        ]),
        Spec("v2:x", 2, journal="completed", start=500),
    ])


def test_k4_4_scan_text_renders_a_plus_for_a_partial_ledger(tmp_path, monkeypatch, capsys):
    _runtime_kill_with_a_partial_ledger(tmp_path, monkeypatch, "8ea")
    code, out, _err = run_cli(capsys, ["scan", "--root", str(tmp_path / "root")])
    assert code in (0, 4)
    assert "1 attempt killed by the runtime, 1+ paths named, 0 commits" in out


def test_k4_4_scan_text_has_no_plus_for_a_complete_ledger(tmp_path, monkeypatch, capsys):
    from tests.n5b_helpers import Spec, build_workflow

    build_workflow(tmp_path, monkeypatch, "wf_00000000-8eb", [
        Spec("v2:x", 1, journal="superseded", kill=True, silence=400, tools=[
            ("t1", "Write", {"file_path": str(_ws(tmp_path) / "a.txt")}, "ok", False),
        ]),
        Spec("v2:x", 2, journal="completed", start=500),
    ])
    _code, out, _err = run_cli(capsys, ["scan", "--root", str(tmp_path / "root")])
    assert "1 attempt killed by the runtime, 1 path named, 0 commits" in out
    assert "1+" not in out


def test_k4_4_scan_row_key_order_puts_the_flag_after_the_count(tmp_path, monkeypatch, capsys):
    _loaded(tmp_path, monkeypatch, "8ec", [Att(1, journal="completed")])
    (row,) = _scan_rows(tmp_path, capsys)
    keys = list(row)
    assert keys[keys.index("paths_at_risk") + 1] == "paths_at_risk_complete"


# --- the relaunch brief's `already exists` -----------------------------------------------------


def _brief_paths(tmp_path, monkeypatch, capsys, suffix: str, steps: list[Step]) -> list[str]:
    build_run(
        tmp_path, monkeypatch, f"wf_00000000-{suffix}", [Att(1, journal="failed", steps=steps)],
    )
    age_files(tmp_path / "root")
    run_dir = next((tmp_path / "root").rglob(f"wf_00000000-{suffix}"))
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir), "--json", "--raw"])
    return json.loads(out)["brief"]["already_exists"]["paths"]


def test_k4_4_brief_lists_a_confirmed_write(tmp_path, monkeypatch, capsys):
    ws = _ws(tmp_path)
    paths = _brief_paths(tmp_path, monkeypatch, capsys, "8ed", [write_step("t1", ws / "a.txt", ws)])
    assert paths == [str(ws / "a.txt")]


def test_k4_4_brief_leaves_out_an_attempted_failed_write(tmp_path, monkeypatch, capsys):
    ws = _ws(tmp_path)
    paths = _brief_paths(tmp_path, monkeypatch, capsys, "8ee", [
        write_step("t1", ws / "a.txt", ws), _failed_write("t2", ws / "d.txt", ws),
    ])
    assert paths == [str(ws / "a.txt")]


def test_k4_4_brief_leaves_out_a_claimed_shell_target(tmp_path, monkeypatch, capsys):
    ws = _ws(tmp_path)
    paths = _brief_paths(tmp_path, monkeypatch, capsys, "8ef", [
        write_step("t1", ws / "a.txt", ws), bash_step("t2", f"echo hi > {ws}/b.txt", ws),
    ])
    assert paths == [str(ws / "a.txt")]


def test_k4_4_brief_lists_a_confirmed_multiedit(tmp_path, monkeypatch, capsys):
    """A MultiEdit that returned success is an observed ledger write and a confirmed one."""
    ws = _ws(tmp_path)
    paths = _brief_paths(tmp_path, monkeypatch, capsys, "8e5", [
        write_step("t1", ws / "a.txt", ws), _multiedit("t2", ws / "c.txt", ws),
    ])
    assert sorted(paths) == sorted([str(ws / "a.txt"), str(ws / "c.txt")])


def test_k4_4_the_helper_takes_an_explicit_effect_set(tmp_path, monkeypatch):
    """One helper, each surface chooses its effect set and population."""
    from agent_postmortem import ledger

    ws = _ws(tmp_path)
    run = _loaded(tmp_path, monkeypatch, "8e4", [
        Att(1, journal="failed", steps=[
            write_step("t1", ws / "a.txt", ws),
            bash_step("t2", f"echo hi > {ws}/b.txt", ws),
            _failed_write("t3", ws / "d.txt", ws),
        ]),
    ])
    attempt = run.attempts[0]
    observed = ledger.attempt_ledger_paths(attempt, frozenset({"observed"}))
    assert [Path(p).name for p in observed] == ["a.txt"]
    everything = ledger.attempt_ledger_paths(
        attempt, frozenset({"observed", "claimed_target", "attempted"}),
    )
    assert sorted(Path(p).name for p in everything) == ["a.txt", "b.txt", "d.txt"]


# --- hand-built attempts: facts only, and the confirmed-writes effect set ------------------------


def _facts(paths: list[str]):
    from agent_postmortem.models import AttemptFacts

    return AttemptFacts(
        paths_written=paths, in_flight=[], commits=[], cwds=[], background_unfinished=0,
        last_tool=None, seconds_since_last_tool_at_end=None,
    )


def test_k4_4_a_facts_only_attempt_counts_its_facts_paths_in_the_agent_table():
    run = model_workflow_run([model_attempt(1, facts=_facts(["/tmp/ws/f.txt"]))])
    assert _paths_cell(run) == "1"


def test_k4_4_already_exists_takes_observed_ledger_paths_and_facts_paths_only():
    from agent_postmortem.models import Ledger
    from tests.n5b_helpers import entry_of

    facts_only = model_attempt(1, facts=_facts(["/tmp/ws/f.txt"]))
    mixed = model_attempt(2, key="v2:y", ledger=Ledger([
        entry_of("observed", "/tmp/ws/o.txt"), entry_of("claimed_target", "/tmp/ws/c.txt"),
        entry_of("attempted", "/tmp/ws/t.txt"), entry_of("possible", None),
    ], "complete", []))
    paths, _commits = rescue._already_exists(model_workflow_run([facts_only, mixed]))
    assert paths == ["/tmp/ws/f.txt", "/tmp/ws/o.txt"]


def test_k4_4_risk_paths_counts_a_population_attempt_without_a_ledger_as_partial():
    run = model_workflow_run([model_attempt(1, outcome="failed", facts=_facts(["/tmp/ws/f.txt"]))])
    from agent_postmortem import ledger

    paths, complete = ledger.risk_paths(run)
    assert paths == ["/tmp/ws/f.txt"]
    assert complete is False


def test_k4_4_the_story_clause_is_a_lower_bound_for_a_partial_ledger(
    tmp_path, monkeypatch, capsys,
):
    run_dir = _runtime_kill_with_a_partial_ledger(tmp_path, monkeypatch, "8e3")
    _code, out, _err = run_cli(capsys, ["run", str(run_dir)])
    story = next(line for line in out.splitlines() if "Killed 1 time by the runtime" in line)
    assert "Wrote 1+ paths" in story


def test_k4_4_the_story_clause_is_exact_for_a_complete_ledger(tmp_path, monkeypatch, capsys):
    from tests.n5b_helpers import Spec, build_workflow

    run_dir = build_workflow(tmp_path, monkeypatch, "wf_00000000-8e2", [
        Spec("v2:x", 1, journal="superseded", kill=True, silence=400, tools=[
            ("t1", "Write", {"file_path": str(_ws(tmp_path) / "a.txt")}, "ok", False),
        ]),
        Spec("v2:x", 2, journal="completed", start=500),
    ])
    _code, out, _err = run_cli(capsys, ["run", str(run_dir)])
    story = next(line for line in out.splitlines() if "Killed 1 time by the runtime" in line)
    assert "Wrote 1 path," in story
    assert "1+" not in story
