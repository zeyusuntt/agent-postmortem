"""N5c-2: the exit codes of rescue plan, rescue status, and watch, every overlapping pair
(docs/DESIGN.md 4.11, 4.12, 4.13.11 "N5", plan S7 and S8).

Each table is the contract's ordered one; a pair test builds a run that triggers both
conditions and checks the process status (what `cli.main` returns) and, where the command
carries one, the JSON `exit_code`: they are the same number.
"""

from __future__ import annotations

import io
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_postmortem import detectors, watch
from agent_postmortem.models import Finding
from tests.n5b_helpers import Spec, build_workflow
from tests.n5c2_helpers import (
    CHECK,
    Att,
    age_files,
    bash_step,
    build_scenario,
    file_write,
    init_repo,
    run_cli,
    shell,
    write_step,
)
from tests.test_watch import (
    _Clock,
    _journal_line,
    _meta,
    _patch_clock,
    _patch_scripted_sleep,
    _started,
    _transcript_path,
    _user_text,
    _write_lines,
    _write_transcript,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _subsets(conditions):
    return [
        pytest.param(subset, id="+".join(subset) or "none")
        for size in range(len(conditions) + 1)
        for subset in itertools.combinations(conditions, size)
    ]

# ---------------------------------------------------------------------------------------------
# rescue plan: usage 1; no run 2; lifecycle unknown, any UNKNOWN fact, or possibly running 4; else 0
# ---------------------------------------------------------------------------------------------

PLAN_CONDITIONS = ("lifecycle", "unknown_fact", "live")


def _plan_run(tmp_path, monkeypatch, suffix, *, lifecycle=False, unknown_fact=False, live=False):
    """A run that triggers exactly the named conditions. `lifecycle`: the launch's notification
    carries a status the reader does not recognize, so the lifecycle terminal is `unknown`.
    `unknown_fact`: the killed attempt also ran a shell command, so its ledger is partial, its
    repository group is refused, and its path reads UNKNOWN. `live`: the run is unfinished and
    its files are fresh. Otherwise the run is finished and its files are old."""
    repo = init_repo(tmp_path / "repo")
    steps = [write_step("t1", repo / "tracked.txt", repo)]
    if unknown_fact:
        steps.append(bash_step("t2", "make", repo))
    attempts = [Att(1, key="v2:a", steps=steps)]
    if lifecycle:
        parent = "weird"
    elif live:
        parent = "running"
        attempts.append(Att(2, key="v2:b", journal="open", start=40))
    else:
        parent = "completed"
    scenario = build_scenario(tmp_path, monkeypatch, suffix, attempts, parent=parent)
    if not live:
        age_files(tmp_path)
    return scenario.run_dir


def _plan_expected(**flags):
    return 4 if any(flags.values()) else 0


@pytest.mark.parametrize("subset", _subsets(PLAN_CONDITIONS))
def test_plan_exit_code_for_every_subset_of_its_conditions(
    subset, tmp_path, monkeypatch, capsys,
):
    flags = {"lifecycle": False, "unknown_fact": False, "live": False}
    flags.update({name: True for name in subset})
    run_dir = _plan_run(tmp_path, monkeypatch, "5c0", **flags)
    code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir), "--json"])
    assert code == _plan_expected(**flags)
    assert json.loads(out)["exit_code"] == code


def test_plan_lifecycle_unknown_alone_is_exit_4_in_text_too(tmp_path, monkeypatch, capsys):
    run_dir = _plan_run(tmp_path, monkeypatch, "5c1", lifecycle=True)
    code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir)])
    assert code == 4
    assert "UNIT" in out


def test_plan_a_finished_run_with_no_condition_is_exit_0(tmp_path, monkeypatch, capsys):
    run_dir = _plan_run(tmp_path, monkeypatch, "5c2")
    code, _out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir)])
    assert code == 0


def test_plan_usage_error_outranks_a_run_that_does_not_exist(tmp_path, capsys):
    code, _out, _err = run_cli(
        capsys, ["rescue", "plan", "wf_00000000-5cf", "--root", str(tmp_path), "--bogus-flag"],
    )
    assert code == 1


def test_plan_no_such_run_is_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "none.toml"))
    (tmp_path / "root").mkdir()
    code, _out, _err = run_cli(
        capsys, ["rescue", "plan", "wf_00000000-5cf", "--root", str(tmp_path / "root")],
    )
    assert code == 2


# ---------------------------------------------------------------------------------------------
# rescue status: usage 1; no run 2; any NONZERO_OBSERVED 3; lifecycle unknown, NOT_OBSERVED,
# WRITE_AFTER_CHECK, or any NEVER match 4; else 0 (3 wins over every 4 condition)
# ---------------------------------------------------------------------------------------------

STATUS_CONDITIONS = ("nonzero", "lifecycle", "not_observed", "write_after", "never")


def _status_run(tmp_path, monkeypatch, suffix, *, nonzero=False, lifecycle=False,
                not_observed=False, write_after=False, never=False):
    """Two declared repositories, one unit in each. Unit A's check passes unless `nonzero`; unit
    B's check is never run when `not_observed`, else passes. `write_after`: a file-tool write to
    unit A's path after the checks. `never`: a `git stash` before the checks (a NEVER match that
    is not after a check, so it adds no write-after). `lifecycle`: the notification is unknown."""
    work = tmp_path / "w"
    repo_a, repo_b = work / "a", work / "b"
    repo_a.mkdir(parents=True)
    repo_b.mkdir(parents=True)
    file_a, file_b = repo_a / "f.py", repo_b / "f.py"
    calls = []
    if never:
        calls.append(shell("git stash", repo_a))
    calls.append(shell(CHECK, repo_a, error=nonzero))
    if not not_observed:
        calls.append(shell(CHECK, repo_b))
    if write_after:
        calls.append(file_write(file_a, repo_a))
    attempts = [
        Att(1, key="v2:a", steps=[write_step("t1", file_a, repo_a)]),
        Att(2, key="v2:b", start=40, steps=[write_step("u1", file_b, repo_b)]),
    ]
    scenario = build_scenario(
        tmp_path, monkeypatch, suffix, attempts, calls=calls,
        repos=[(str(repo_a), [CHECK]), (str(repo_b), [CHECK])],
        parent="weird" if lifecycle else "completed",
    )
    return scenario.run_dir


def _status_expected(**flags):
    if flags["nonzero"]:
        return 3
    if any(flags.values()):
        return 4
    return 0


@pytest.mark.parametrize("subset", _subsets(STATUS_CONDITIONS))
def test_status_exit_code_for_every_subset_of_its_conditions(
    subset, tmp_path, monkeypatch, capsys,
):
    flags = {name: False for name in STATUS_CONDITIONS}
    flags.update({name: True for name in subset})
    run_dir = _status_run(tmp_path, monkeypatch, "5c3", **flags)
    code, out, _err = run_cli(capsys, ["rescue", "status", str(run_dir), "--json"])
    assert code == _status_expected(**flags)
    assert json.loads(out)["exit_code"] == code


def test_status_a_nonzero_check_outranks_a_never_command(tmp_path, monkeypatch, capsys):
    # F3 hardening let a NEVER match override even a failed check; 4.13.11 orders 3 first.
    run_dir = _status_run(tmp_path, monkeypatch, "5c4", nonzero=True, never=True)
    code, out, _err = run_cli(capsys, ["rescue", "status", str(run_dir)])
    assert code == 3
    assert "NEVER_COMMAND_OBSERVED" in out


def test_status_lifecycle_unknown_alone_is_exit_4(tmp_path, monkeypatch, capsys):
    run_dir = _status_run(tmp_path, monkeypatch, "5c5", lifecycle=True)
    code, _out, _err = run_cli(capsys, ["rescue", "status", str(run_dir)])
    assert code == 4


def test_status_usage_error_outranks_a_run_that_does_not_exist(tmp_path, capsys):
    code, _out, _err = run_cli(
        capsys, ["rescue", "status", "wf_00000000-5cf", "--root", str(tmp_path), "--bogus-flag"],
    )
    assert code == 1


def test_status_no_such_run_is_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "none.toml"))
    (tmp_path / "root").mkdir()
    code, _out, _err = run_cli(
        capsys, ["rescue", "status", "wf_00000000-5cf", "--root", str(tmp_path / "root")],
    )
    assert code == 2


def test_status_duplicate_declared_roots_are_a_configuration_error_exit_1(
    tmp_path, monkeypatch, capsys,
):
    work = tmp_path / "w" / "a"
    work.mkdir(parents=True)
    scenario = build_scenario(
        tmp_path, monkeypatch, "5c6",
        [Att(1, steps=[write_step("t1", work / "f.py", work)])],
        repos=[(str(work), [CHECK]), (str(work) + "/", [CHECK])],
    )
    code, out, err = run_cli(capsys, ["rescue", "status", str(scenario.run_dir), "--raw"])
    assert code == 1
    assert out == ""
    assert str(work) in err
    assert "twice" in err


def test_status_duplicate_roots_outrank_a_run_that_does_not_exist(tmp_path, monkeypatch, capsys):
    work = tmp_path / "w" / "a"
    config = tmp_path / "cfg.toml"
    config.write_text(
        f'[[repos]]\npath = "{work}"\nchecks = ["{CHECK}"]\n'
        f'[[repos]]\npath = "{work}"\nchecks = ["{CHECK}"]\n', encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config))
    (tmp_path / "root").mkdir()
    code, _out, _err = run_cli(
        capsys, ["rescue", "status", "wf_00000000-5cf", "--root", str(tmp_path / "root")],
    )
    assert code == 1


def test_status_duplicate_roots_are_named_through_the_levels_path_rule(
    tmp_path, monkeypatch, capsys,
):
    work = tmp_path / "w" / "a"
    work.mkdir(parents=True)
    scenario = build_scenario(
        tmp_path, monkeypatch, "5c7",
        [Att(1, steps=[write_step("t1", work / "f.py", work)])],
        repos=[(str(work), [CHECK]), (str(work), [CHECK])],
    )
    code, _out, err = run_cli(capsys, ["rescue", "status", str(scenario.run_dir)])
    assert code == 1
    assert str(work) not in err
    assert "path-1" in err


# ---------------------------------------------------------------------------------------------
# process status: the same numbers through a real process
# ---------------------------------------------------------------------------------------------


def _process(args: list[str], env_config: Path | None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["AGENT_POSTMORTEM_CONFIG"] = str(env_config) if env_config else "/nonexistent/none.toml"
    return subprocess.run(
        [sys.executable, "-m", "agent_postmortem.cli", *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_process_status_of_rescue_plan_matches_its_json_exit_code(tmp_path, monkeypatch):
    run_dir = _plan_run(tmp_path, monkeypatch, "5c8", lifecycle=True)
    config = tmp_path / "does-not-exist.toml"
    proc = _process(["rescue", "plan", str(run_dir), "--json"], config)
    assert proc.returncode == 4
    assert json.loads(proc.stdout)["exit_code"] == 4
    clean = _plan_run(tmp_path / "clean", monkeypatch, "5c9")
    proc = _process(["rescue", "plan", str(clean), "--json"], config)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["exit_code"] == 0


def test_process_status_of_rescue_status_matches_its_json_exit_code(tmp_path, monkeypatch):
    run_dir = _status_run(tmp_path, monkeypatch, "5ca", nonzero=True, never=True)
    config = Path(os.environ["AGENT_POSTMORTEM_CONFIG"])
    proc = _process(["rescue", "status", str(run_dir), "--json"], config)
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["exit_code"] == 3


def test_process_status_of_usage_errors(tmp_path):
    for args in (["rescue", "plan", "--bogus"], ["rescue", "status", "--bogus"], ["watch", "--bogus"]):
        assert _process(args, None).returncode == 1


# ---------------------------------------------------------------------------------------------
# watch: usage 1; no journal within 60 s 2; DONE FAILED 3; LIMIT, MALFORMED, or lifecycle
# unknown at DONE 4; DONE otherwise 0
# ---------------------------------------------------------------------------------------------


def _done(run_dir) -> tuple[int, dict]:
    out = io.StringIO()
    code = watch._emit_done(run_dir, True, out)
    return code, json.loads(out.getvalue())


def _watch_run(tmp_path, monkeypatch, suffix, parent):
    return build_workflow(
        tmp_path, monkeypatch, f"wf_00000000-{suffix}", [Spec("v2:x", 1)], parent=parent,
    )


@pytest.mark.parametrize("parent,expected_code,expected_verdict_word", [
    ("completed", 0, "CLEAN"),
    ("failed", 3, "FAILED"),
    ("weird", 4, "CLEAN"),
])
def test_watch_done_exit_code_per_lifecycle(
    parent, expected_code, expected_verdict_word, tmp_path, monkeypatch,
):
    run_dir = _watch_run(tmp_path, monkeypatch, "5cb", parent)
    code, payload = _done(run_dir)
    assert payload["event"] == "DONE"
    assert payload["verdict"].split(" ")[0] == expected_verdict_word
    assert code == expected_code


def test_watch_done_with_an_unknown_lifecycle_still_prints_its_done_line(
    tmp_path, monkeypatch,
):
    run_dir = _watch_run(tmp_path, monkeypatch, "5cb", "weird")
    out = io.StringIO()
    code = watch._emit_done(run_dir, False, out)
    assert out.getvalue().startswith("DONE") or "DONE" in out.getvalue()
    assert code == 4


def test_watch_done_failed_outranks_an_unknown_lifecycle(tmp_path, monkeypatch):
    # No detector is critical in v0.3, so a failed verdict and an unknown lifecycle cannot
    # meet on a real run; a reserved critical finding makes the pair real (3 wins over 4).
    run_dir = _watch_run(tmp_path, monkeypatch, "5cc", "weird")
    critical = Finding(
        detector="silent_retry", severity="critical", title="t", run_id="wf_00000000-5cc",
        agent_id=None, evidence=[], metrics={}, rule_id=None,
    )
    monkeypatch.setattr(detectors, "run_detectors", lambda run: [critical])
    code, payload = _done(run_dir)
    assert payload["verdict"].split(" ")[0] == "FAILED"
    assert code == 3


def test_watch_limit_is_exit_4_and_malformed_outranks_a_failed_done(
    tmp_path, monkeypatch, capsys,
):
    # LIMIT replaces DONE when the journal has a malformed line: exit 4 even for a failed run.
    run_dir = _watch_run(tmp_path, monkeypatch, "5cd", "failed")
    journal = run_dir / "journal.jsonl"
    journal.write_text(journal.read_text() + '"not an object"\n', encoding="utf-8")
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    _patch_scripted_sleep(monkeypatch, [], clock)
    code, out, _err = run_cli(capsys, ["watch", str(run_dir), "--interval", "1"])
    assert "LIMIT" in out.splitlines()[-1]
    assert code == 4


def test_watch_ordinary_limit_is_exit_4(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "wf_00000000-5ce"
    run_dir.mkdir(parents=True)
    agent = "ab00000000000001a"
    _write_lines(run_dir / "journal.jsonl", [
        _journal_line(type="launched", timestamp="2026-02-01T00:00:00.000Z"),
        _started("2026-02-01T00:00:00.000Z", "v2:k", agent, "impl:k"),
    ])
    _meta(run_dir, agent)
    transcript = _transcript_path(run_dir, agent)
    _write_transcript(transcript, [_user_text("2026-02-01T00:00:00.000Z", "Implement it.")])
    clock = _Clock()
    os.utime(transcript, (clock.now, clock.now))
    _patch_clock(monkeypatch, clock)
    _patch_scripted_sleep(monkeypatch, [lambda: clock.advance(3600 * 7)], clock)
    code, out, _err = run_cli(
        capsys, ["watch", str(run_dir), "--interval", "1", "--max-hours", "6"],
    )
    assert out.splitlines()[-1].startswith("LIMIT")
    assert code == 4


def test_watch_usage_error_is_exit_1_and_outranks_a_missing_journal(tmp_path, capsys):
    code, _out, _err = run_cli(
        capsys, ["watch", "wf_00000000-5cf", "--root", str(tmp_path), "--bogus-flag"],
    )
    assert code == 1
