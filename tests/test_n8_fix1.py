"""N8 fix round 1 (.reviews/v0.3/N8-dispositions.md): S1, S2, S3, C1, C2, S-m1, S-m2, S-m3, S-m5.

S1    the AGENT PATHS cell is `N+` when any attempt's ledger is partial, a missing transcript included.
S2    `rescue plan` and `rescue status` never say NOTHING TO RESCUE while the 4.13.1 risk population
      is not empty or not complete.
S3    the story clause never says "Wrote no paths." for a partial ledger.
C1    scan `paths_at_risk` counts only the risk population (a safe completed attempt adds nothing).
C2    a parent append parses exactly the appended lines, through the same cached index object.
S-m1  the RESET watermark is the newest launch line of several old generations.
S-m2  `nothing_to_rescue_ok` needs EVERY attempt completed.
S-m3  one file under two spellings is one path.
S-m5  watch's KILL line says `wrote P+ paths` for a partial ledger (text only).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from agent_postmortem import ledger, reader, rescue
from tests.n5b_helpers import Spec, build_workflow, model_attempt, model_workflow_run
from tests.n5c1_helpers import Att, Step, build_run, write_step
from tests.n5c2_helpers import age_files, run_cli
from tests.test_n8_k4_rescue import _paths_cell, _scan_rows, _ws
from tests.test_n8_k5_watch import (
    JT,
    T0,
    Scn,
    lines_of,
    run_watch,
    snapshot,
)
from tests.test_watch import (
    _append_lines,
    _assistant_text,
    _assistant_tool_use,
    _build_kill_scenario,
    _Clock,
    _kill_sentinel,
    _patch_clock,
    _patch_scripted_sleep,
    _result,
    _started,
    _transcript_path,
    _user_text,
    _user_tool_result,
    _write_transcript,
)


def _loaded(tmp_path, monkeypatch, suffix: str, attempts: list[Att], *, parent="completed"):
    return reader.load_run(build_run(
        tmp_path, monkeypatch, f"wf_00000000-{suffix}", attempts, parent=parent,
    ))


# =============================================================================================
# S1: a missing transcript makes the PATHS count a lower bound
# =============================================================================================


def _failed_writer_then_a_completed_attempt_without_a_transcript(tmp_path, monkeypatch, suffix):
    ws = _ws(tmp_path)
    run = _loaded(tmp_path, monkeypatch, suffix, [
        Att(1, journal="failed", steps=[write_step("t1", ws / "a.txt", ws)]),
        Att(2, journal="completed", steps=[write_step("t1", ws / "b.txt", ws)]),
    ])
    (run.path / f"agent-{run.attempts[1].agent_id}.jsonl").unlink()
    return reader.load_run(run.path)


def test_s1_a_completed_attempt_with_its_transcript_deleted_makes_the_count_a_lower_bound(
    tmp_path, monkeypatch,
):
    run = _failed_writer_then_a_completed_attempt_without_a_transcript(tmp_path, monkeypatch, "9a1")
    assert "transcript_missing" in run.attempts[1].ledger.reasons
    assert _paths_cell(run) == "1+"


def test_s1_the_story_clause_is_a_lower_bound_too(tmp_path, monkeypatch, capsys):
    run = _failed_writer_then_a_completed_attempt_without_a_transcript(tmp_path, monkeypatch, "9a2")
    _code, out, _err = run_cli(capsys, ["run", str(run.path)])
    assert "Wrote 1+ paths" in out


def test_s1_scan_agrees_with_the_agent_column(tmp_path, monkeypatch, capsys):
    run = _failed_writer_then_a_completed_attempt_without_a_transcript(tmp_path, monkeypatch, "9a3")
    assert ledger.risk_paths(run)[1] is False
    (row,) = _scan_rows(tmp_path, capsys)
    assert row["paths_at_risk_complete"] is False


def test_s1_every_transcript_missing_still_reads_a_question_mark(tmp_path, monkeypatch):
    run = _loaded(tmp_path, monkeypatch, "9a4", [Att(1, journal="completed")])
    (run.path / f"agent-{run.attempts[0].agent_id}.jsonl").unlink()
    assert _paths_cell(reader.load_run(run.path)) == "?"


# =============================================================================================
# S2: no NOTHING TO RESCUE while the risk population is not empty or not complete
# =============================================================================================

FINISHED = "NOTHING TO RESCUE: every attempt completed."
RISK_PREFIX = "NO KILLED OR FAILED ATTEMPT: "
RISK_TAIL = " may still hold unchecked work; see WORK AT RISK in agent-postmortem run "


def _risk_run(tmp_path, monkeypatch, shape: str, suffix: str) -> Path:
    ws = _ws(tmp_path)
    if shape == "failed_terminal_completed_writer":
        run_dir = build_run(
            tmp_path, monkeypatch, f"wf_00000000-{suffix}",
            [Att(1, journal="completed", steps=[write_step("t1", ws / "a.txt", ws)])],
            parent="failed",
        )
    elif shape == "no_parent_transcript":
        run_dir = build_run(
            tmp_path, monkeypatch, f"wf_00000000-{suffix}",
            [Att(1, journal="completed"), Att(2, key="v2:y", journal="completed")], parent=None,
        )
    else:  # a truly clean run, with a parent transcript
        run_dir = build_run(
            tmp_path, monkeypatch, f"wf_00000000-{suffix}",
            [Att(1, journal="completed", steps=[write_step("t1", ws / "a.txt", ws)])],
        )
    age_files(tmp_path / "root")
    return run_dir


RISK_SHAPES = [
    pytest.param("failed_terminal_completed_writer", "9b1", 1, id="failed terminal, completed writer"),
    pytest.param("no_parent_transcript", "9b2", 2, id="no parent transcript, unassignable attempts"),
]


@pytest.mark.parametrize("command", ["plan", "status"])
@pytest.mark.parametrize("shape,suffix,count", RISK_SHAPES)
def test_s2_text_and_exit(tmp_path, monkeypatch, capsys, command, shape, suffix, count):
    run_dir = _risk_run(tmp_path, monkeypatch, shape, suffix)
    code, out, _err = run_cli(capsys, ["rescue", command, str(run_dir), "--no-redact"])
    assert FINISHED not in out
    assert f"{RISK_PREFIX}{count} attempt(s){RISK_TAIL}wf_00000000-{suffix}." in out
    assert code == 4


JSON_CASES = [
    pytest.param("plan", "failed_terminal_completed_writer", "9b1"),
    pytest.param("status", "failed_terminal_completed_writer", "9b5"),
    pytest.param("plan", "no_parent_transcript", "9b6"),
    pytest.param("status", "no_parent_transcript", "9b7"),
]


@pytest.mark.parametrize("command,shape,suffix", JSON_CASES)
def test_s2_json_and_exit(tmp_path, monkeypatch, capsys, command, shape, suffix):
    run_dir = _risk_run(tmp_path, monkeypatch, shape, suffix)
    code, out, _err = run_cli(capsys, ["rescue", command, str(run_dir), "--json"])
    payload = json.loads(out)
    assert payload["units"] == []
    assert payload["exit_code"] == code == 4
    if command == "plan":
        assert payload["nothing_to_rescue"] is False
        assert payload["brief"] is None


@pytest.mark.parametrize("command", ["plan", "status"])
def test_s2_the_sentence_prints_at_the_default_disclosure_level(
    tmp_path, monkeypatch, capsys, command,
):
    run_dir = _risk_run(tmp_path, monkeypatch, "failed_terminal_completed_writer", "9b3")
    code, out, _err = run_cli(capsys, ["rescue", command, str(run_dir)])
    assert RISK_PREFIX in out and FINISHED not in out
    assert code == 4


@pytest.mark.parametrize("command", ["plan", "status"])
def test_s2_a_truly_clean_run_with_a_parent_transcript_still_exits_zero(
    tmp_path, monkeypatch, capsys, command,
):
    run_dir = _risk_run(tmp_path, monkeypatch, "clean", "9b4")
    code, out, _err = run_cli(capsys, ["rescue", command, str(run_dir)])
    assert FINISHED in out
    assert RISK_PREFIX not in out
    assert code == 0


def test_s2_the_predicate_needs_an_empty_complete_risk_population(monkeypatch):
    attempt = model_attempt(1)
    ok = model_workflow_run([attempt], terminal="completed")
    ok.finished = True
    monkeypatch.setattr(ledger, "risk_population", lambda run: [])
    assert rescue.nothing_to_rescue_ok(ok) is True
    partial = model_workflow_run([attempt], terminal="completed", completeness="partial")
    partial.finished = True
    assert rescue.nothing_to_rescue_ok(partial) is False  # an empty but incomplete population
    monkeypatch.setattr(ledger, "risk_population", lambda run: [attempt])
    assert rescue.nothing_to_rescue_ok(ok) is False
    assert rescue.empty_unit_state(ok) == ("risk", 1)


# =============================================================================================
# S-m2: every attempt must be completed
# =============================================================================================


def test_sm2_one_completed_and_one_open_attempt_is_not_every_attempt_completed(monkeypatch):
    # the risk population is stubbed empty so only the all-completed clause decides
    monkeypatch.setattr(ledger, "risk_population", lambda run: [])
    run = model_workflow_run(
        [model_attempt(1), model_attempt(2, key="v2:y", outcome="unknown")], terminal="completed",
    )
    run.finished = True
    assert rescue.nothing_to_rescue_ok(run) is False
    assert rescue.empty_unit_state(run) == ("yet", 0)
    every = model_workflow_run(
        [model_attempt(1), model_attempt(2, key="v2:y")], terminal="completed",
    )
    every.finished = True
    assert rescue.nothing_to_rescue_ok(every) is True


def test_s2_the_plan_exit_is_4_for_a_risk_run_whatever_else_is_known(monkeypatch, tmp_path):
    """No other UNKNOWN fact exists here (a checked, empty observation), so the exit 4 can only
    come from the risk population."""
    from types import SimpleNamespace

    from agent_postmortem import config
    from tests.test_field_v03 import _no_config

    _no_config(monkeypatch, tmp_path)
    attempt = model_attempt(1)
    run = model_workflow_run([attempt], terminal="completed")
    run.finished = True
    monkeypatch.setattr(ledger, "risk_population", lambda run: [attempt])
    observation = SimpleNamespace(checked=True, groups=[], repo_root="/tmp/ws")
    plan = rescue.build_plan(run, observation, [], config.load_config())
    assert plan.has_unknown_fact is False
    assert plan.unchecked_attempts == 1
    assert plan.nothing_to_rescue is False
    assert plan.exit_code == 4


# =============================================================================================
# S3: the story clause never says "Wrote no paths." for a partial ledger
# =============================================================================================


def test_s3_a_killed_attempt_that_only_ran_bash_never_says_wrote_no_paths(
    tmp_path, monkeypatch, capsys,
):
    run_dir = build_workflow(tmp_path, monkeypatch, "wf_00000000-9c1", [
        Spec("v2:x", 1, journal="superseded", kill=True, silence=400, tools=[
            ("t1", "Bash", {"command": "make build"}, "ok", False),
        ]),
    ])
    run = reader.load_run(run_dir)
    assert run.attempts[0].outcome == "killed"
    assert run.attempts[0].ledger.completeness == "partial"
    _code, out, _err = run_cli(capsys, ["run", str(run_dir)])
    assert "Wrote no paths" not in out
    assert "Wrote 0" not in out


def test_s3_a_killed_attempt_with_a_complete_ledger_still_says_wrote_no_paths(
    tmp_path, monkeypatch, capsys,
):
    run_dir = build_workflow(tmp_path, monkeypatch, "wf_00000000-9c2", [
        Spec("v2:x", 1, journal="superseded", kill=True, silence=400, tools=[
            ("t1", "Read", {"file_path": str(_ws(tmp_path) / "a.txt")}, "ok", False),
        ]),
    ])
    run = reader.load_run(run_dir)
    assert run.attempts[0].ledger.completeness == "complete"
    _code, out, _err = run_cli(capsys, ["run", str(run_dir)])
    assert "Wrote no paths." in out


# =============================================================================================
# C1: scan counts only the risk population
# =============================================================================================


def test_c1_a_safe_completed_attempt_with_a_confirmed_path_adds_nothing(
    tmp_path, monkeypatch, capsys,
):
    ws = _ws(tmp_path)
    _loaded(tmp_path, monkeypatch, "9d1", [
        Att(1, journal="completed", steps=[write_step("t1", ws / "safe.txt", ws)]),
    ])
    (row,) = _scan_rows(tmp_path, capsys)
    assert row["paths_at_risk"] == 0
    assert row["paths_at_risk_complete"] is True


def test_c1_a_mixed_run_counts_only_the_risk_path(tmp_path, monkeypatch, capsys):
    ws = _ws(tmp_path)
    run = _loaded(tmp_path, monkeypatch, "9d2", [
        Att(1, journal="failed", steps=[write_step("t1", ws / "risk.txt", ws)]),
        Att(2, key="v2:y", journal="completed", steps=[write_step("t1", ws / "safe.txt", ws)]),
    ])
    paths, complete = ledger.risk_paths(run)
    assert [Path(p).name for p in paths] == ["risk.txt"]
    assert complete is True
    (row,) = _scan_rows(tmp_path, capsys)
    assert row["paths_at_risk"] == 1
    assert row["paths_at_risk_complete"] is True


# =============================================================================================
# C2: a parent append parses exactly the appended lines, through the same index object
# =============================================================================================


def test_c2_a_parent_append_parses_exactly_the_appended_lines_in_the_cached_index(
    tmp_path, monkeypatch, capsys,
):
    from tests.test_field_v03 import _assistant_tool
    from tests.test_n8_k5_watch import FOREVER, _Counter, _open_run

    scn = Scn(tmp_path, "9e1")
    _open_run(scn)
    scn.parent_write(scn.launch())
    counter = _Counter(monkeypatch, scn)
    seen: list[tuple[object, int]] = []

    def remember():
        index = counter.index
        seen.append((index, index.lines_parsed))

    def append_two():
        scn.parent_append([
            _assistant_tool("toolu_x", "Read", {}, T0 + timedelta(seconds=5)),
            _assistant_tool("toolu_y", "Read", {}, T0 + timedelta(seconds=6)),
        ])

    run_watch(
        monkeypatch, capsys, scn, actions=[remember, append_two, remember], extra=FOREVER,
    )
    (before_index, before_lines), (after_index, after_lines) = seen
    assert after_index is before_index  # no cache replacement, so no full reparse
    assert after_lines - before_lines == 2
    assert counter.index.rebuilds == 0


# =============================================================================================
# S-m1: the RESET watermark is the newest launch of several old generations
# =============================================================================================


def test_sm1_two_old_generations_never_let_the_older_epoch_finish_the_replacement(
    tmp_path, monkeypatch, capsys,
):
    scn = Scn(tmp_path, "9f1")
    old = scn.agent()
    _journal = [_started(JT, "v2:x", old, "impl"), _started(JT, "v2:y", scn.agent(), "other")]
    from tests.test_watch import _write_lines

    _write_lines(scn.journal, _journal)
    # two launches of the old epoch (a resume): the watermark must be the second launch's line
    scn.parent_write([
        *scn.launch("async_launched", tool="toolu_A", task="tA"),
        *scn.launch("async_launched", tool="toolu_B", task="tB"),
    ])
    fresh = scn.agent()
    snaps: list[str] = []

    def replace_journal_and_finish_the_newer_old_generation():
        snapshot(capsys, snaps)  # poll 1 sees both generations
        _write_lines(scn.journal, [_started(JT, "v2:z", fresh, "fresh")])
        scn.parent_append([scn.notify("completed", tool="toolu_B", task="tB")])

    code, out = run_watch(
        monkeypatch, capsys, scn, actions=[replace_journal_and_finish_the_newer_old_generation],
        extra=("--done-grace", "3600", "--max-hours", "0.02"), interval=5,
    )
    assert "RESET" in out
    assert "DONE" not in out  # the old epoch's terminal is not the replacement's
    assert lines_of(out)[-1].startswith("LIMIT")
    assert code == 4


# =============================================================================================
# S-m3: one file under two spellings is one path
# =============================================================================================


def _two_spellings(tmp_path, monkeypatch, suffix):
    ws = _ws(tmp_path)
    return _loaded(tmp_path, monkeypatch, suffix, [
        Att(1, journal="failed", steps=[
            write_step("t1", ws / "a.txt", ws),
            Step("t2", "Write", {"file_path": "a.txt"}, "ok", False, str(ws)),
        ]),
    ])


def test_sm3_the_agent_column_counts_one_file_under_two_spellings_once(tmp_path, monkeypatch):
    run = _two_spellings(tmp_path, monkeypatch, "901")
    assert any(not p.startswith("/") for p in run.attempts[0].facts.paths_written)
    assert _paths_cell(run) == "1"


def test_sm3_scan_counts_one_file_under_two_spellings_once(tmp_path, monkeypatch, capsys):
    _two_spellings(tmp_path, monkeypatch, "902")
    (row,) = _scan_rows(tmp_path, capsys)
    assert row["paths_at_risk"] == 1


# =============================================================================================
# S-m5: watch's KILL line is a lower bound for a partial ledger (text only)
# =============================================================================================


def _kill_scenario_with_a_shell_call(run_dir: Path):
    key, agent1, agent2 = _build_kill_scenario(run_dir)
    _write_transcript(_transcript_path(run_dir, agent1), [
        _user_text("2026-02-01T00:00:00.000Z", "Implement the watch-a module."),
        _assistant_tool_use(
            "2026-02-01T00:00:01.000Z", "toolu_1", "Edit",
            {"file_path": "/home/user/project/a.py"}, text="Editing a.",
        ),
        _user_tool_result("2026-02-01T00:00:02.000Z", "toolu_1", "ok"),
        _assistant_tool_use(
            "2026-02-01T00:00:03.000Z", "toolu_2", "Bash", {"command": "make build"},
        ),
        _user_tool_result("2026-02-01T00:00:04.000Z", "toolu_2", "ok"),
        _assistant_text("2026-02-01T00:00:05.000Z", "Built."),
        _kill_sentinel("2026-02-01T00:07:01.000Z"),
    ])
    return key, agent1, agent2


def _watch_the_kill(monkeypatch, capsys, run_dir, key, agent2, *, json_mode: bool):
    journal = run_dir / "journal.jsonl"
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_scripted_sleep(monkeypatch, [
        lambda: _append_lines(journal, [_started("2026-02-01T00:10:00.000Z", key, agent2, "impl:a")]),
        lambda: _append_lines(journal, [_result("2026-02-01T00:10:06.000Z", key, agent2)]),
    ], clock)
    from agent_postmortem.cli import main

    argv = ["watch", str(run_dir), "--interval", "1", *(["--json"] if json_mode else [])]
    main(argv)
    return capsys.readouterr().out


def test_sm5_the_kill_line_marks_a_partial_ledger_as_a_lower_bound(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "wf_00000000-911"
    key, _agent1, agent2 = _kill_scenario_with_a_shell_call(run_dir)
    out = _watch_the_kill(monkeypatch, capsys, run_dir, key, agent2, json_mode=False)
    assert "       attempt 1 wrote 1+ paths, reported 0 commits.  " in out


def test_sm5_the_json_kill_record_keeps_the_integer(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "wf_00000000-912"
    key, _agent1, agent2 = _kill_scenario_with_a_shell_call(run_dir)
    out = _watch_the_kill(monkeypatch, capsys, run_dir, key, agent2, json_mode=True)
    kill = next(r for r in map(json.loads, out.splitlines()) if r["event"] == "KILL")
    assert kill["paths_written"] == 1


def test_sm5_a_complete_ledger_keeps_the_exact_count(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "wf_00000000-913"
    key, _agent1, agent2 = _build_kill_scenario(run_dir)
    out = _watch_the_kill(monkeypatch, capsys, run_dir, key, agent2, json_mode=False)
    assert "attempt 1 wrote 2 paths, reported 0 commits." in out
    assert "2+" not in out
