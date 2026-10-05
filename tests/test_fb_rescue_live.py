"""Part FB3 (field milestone F, P2): `rescue plan` text on a live run
(docs/DESIGN.md 4.11 LIVE rule; .reviews/v0.3/F-plan.md FB3).

The field case: an agent running `rescue plan` inside the very session it was rescuing was
told "POSSIBLY LIVE: do not modify files in this repo", which is true of the session and
useless to it, and a live run with nothing killed yet got the same do-not-modify warning with
no file to protect. Four text branches, `rescue plan` text only (`rescue status` and all JSON
and exit codes are unchanged):

1. live, no unit: "NOTHING TO RESCUE YET: the run may still be running; no attempt has been
   killed or failed so far." with no do-not-modify sentence;
2. live, units, not the caller's session: "POSSIBLY LIVE: ... another agent may be writing
   the files listed below. Do not modify them until it has stopped; confirm with the human.";
3. live, units, a SESSION run whose transcript stem equals CLAUDE_CODE_SESSION_ID:
   "YOUR CURRENT SESSION: ...";
4. a possibly-live WORKFLOW run, including one launched by the caller's session, keeps the
   POSSIBLY LIVE warning.

Every test sets or leaves unset the variable explicitly (tests/conftest.py deletes it).
Fixtures are synthetic and written fresh under tmp_path with real wall-clock times, so a fresh
file reads as possibly live and an aged one does not.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.n5c2_helpers import age_files, run_cli
from tests.n6_helpers import SLUG_A, aid, entry, session_path, uid, write_lines
from tests.test_field_v03 import (
    FAKE_UUID,
    _aid,
    _kill,
    _no_config,
    _user,
    _wf_dir,
    _write_jsonl,
)

ENV = "CLAUDE_CODE_SESSION_ID"
KILL = "[Request interrupted by user]"
CWD = "/tmp/fb-rescue-work"

NOTHING_YET_LIVE = (
    "NOTHING TO RESCUE YET: the run may still be running; no attempt has been killed or "
    "failed so far."
)
POSSIBLY_LIVE = (
    "POSSIBLY LIVE: the run may still be running, and another agent may be writing the files "
    "listed below. Do not modify them until it has stopped; confirm with the human."
)
YOUR_SESSION = (
    "YOUR CURRENT SESSION: this run is the session you are working in. Check the files listed "
    "below yourself before relaunching the killed work."
)
OLD_LIVE = (
    "POSSIBLY LIVE: do not modify files in this repo. Confirm with the human that the run has "
    "stopped."
)
CHEERFUL = "NOTHING TO RESCUE: every attempt completed."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session_with_killed_child(root: Path, n: int, *, live: bool, kill: bool = True) -> Path:
    """A session run whose one subagent was interrupted (a unit), or, with kill False, whose
    subagent completed (no unit). Its files are fresh (possibly live) or aged two hours."""
    base = _now() - timedelta(minutes=10)
    main = session_path(root, SLUG_A, uid(n))
    write_lines(main, [
        entry("user", "work on it", base, cwd=CWD, session=uid(n)),
        entry("assistant", "launching", base + timedelta(seconds=2), cwd=CWD, session=uid(n)),
    ])
    child = root / SLUG_A / uid(n) / "subagents" / f"agent-{aid(n)}.jsonl"
    rows = [
        entry("user", "Look into it.", base + timedelta(seconds=3), cwd=CWD, session=uid(n),
              agent=aid(n)),
        entry("assistant", "on it", base + timedelta(seconds=5), cwd=CWD, session=uid(n),
              agent=aid(n)),
    ]
    if kill:
        rows.append(entry("user", KILL, base + timedelta(seconds=6), cwd=CWD, session=uid(n),
                          agent=aid(n)))
    write_lines(child, rows)
    if not live:
        age_files(root)
    return main


def _plain_live_session(root: Path, n: int, *, live: bool = True) -> Path:
    """A session with no subagent at all: a live run with no unit."""
    base = _now() - timedelta(minutes=2)
    main = session_path(root, SLUG_A, uid(n))
    write_lines(main, [
        entry("user", "hello", base, cwd=CWD, session=uid(n)),
        entry("assistant", "hi", base + timedelta(seconds=1), cwd=CWD, session=uid(n)),
    ])
    if not live:
        age_files(root)
    return main


def _workflow_with_killed_attempt(root: Path, suffix: str, *, parent: str = FAKE_UUID,
                                  live: bool = True) -> Path:
    """A workflow run launched by session `parent` (its parent session transcript exists):
    one attempt, interrupted: a unit. Fresh files read as possibly live."""
    run_dir = _wf_dir(root, f"wf_00000000-{suffix}", session=parent)
    agent_id = _aid(int(suffix, 16))
    base = _now() - timedelta(minutes=10)
    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:only", "agentId": agent_id, "label": "lone",
         "phase": "Implement"},
    ])
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Look into it.", base, agent_id),
        _kill(base + timedelta(seconds=1), agent_id),
    ])
    write_lines(session_path(root, SLUG_A, parent), [
        entry("user", "launch it", base - timedelta(seconds=30), cwd=CWD, session=parent),
        entry("assistant", "launched", base - timedelta(seconds=29), cwd=CWD, session=parent),
    ])
    if not live:
        age_files(root)
    return run_dir


def _plan(capsys, target, *flags):
    return run_cli(capsys, ["rescue", "plan", str(target), *flags])


def _first_line_after_disclosure(out: str) -> str:
    lines = out.splitlines()
    if lines and lines[0].startswith("disclosure:"):
        return lines[1]
    return lines[0]


LEVELS = [pytest.param([], id="share"), pytest.param(["--local"], id="local")]


# --- the four branches ---------------------------------------------------------------------


@pytest.mark.parametrize("flags", LEVELS)
@pytest.mark.parametrize("own", [False, True])
def test_live_no_unit_says_nothing_to_rescue_yet_and_no_do_not_modify(
    tmp_path, monkeypatch, capsys, flags, own,
):
    """Branch 1: whoever the caller is, a live run with no unit has nothing to protect yet."""
    root = tmp_path / "root"
    target = _plain_live_session(root, 1)
    _no_config(monkeypatch, tmp_path)
    if own:
        monkeypatch.setenv(ENV, uid(1))
    code, out, _err = _plan(capsys, target, *flags)
    assert code == 4
    assert _first_line_after_disclosure(out) == NOTHING_YET_LIVE
    assert "do not modify" not in out.lower() and "Do not modify" not in out
    assert "POSSIBLY LIVE" not in out and "YOUR CURRENT SESSION" not in out
    assert CHEERFUL not in out


@pytest.mark.parametrize("flags", LEVELS)
def test_live_no_unit_workflow_says_nothing_to_rescue_yet(tmp_path, monkeypatch, capsys, flags):
    root = tmp_path / "root"
    run_dir = _wf_dir(root, "wf_00000000-fb4")
    agent_id = _aid(0xFB4)
    base = _now() - timedelta(minutes=3)
    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:only", "agentId": agent_id, "label": "lone",
         "phase": "Implement"},
    ])
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [_user("Work.", base, agent_id)])
    _no_config(monkeypatch, tmp_path)
    code, out, _err = _plan(capsys, run_dir, *flags)
    assert code == 4
    assert _first_line_after_disclosure(out) == NOTHING_YET_LIVE
    assert "Do not modify" not in out


@pytest.mark.parametrize("flags", LEVELS)
@pytest.mark.parametrize("variable", ["unset", "another_session", "not_a_uuid"])
def test_live_units_not_the_callers_session_keep_the_warning(
    tmp_path, monkeypatch, capsys, flags, variable,
):
    """Branch 2: the new wording names the files and the other agent that may be writing."""
    root = tmp_path / "root"
    target = _session_with_killed_child(root, 1, live=True)
    _no_config(monkeypatch, tmp_path)
    if variable == "another_session":
        monkeypatch.setenv(ENV, uid(2))
    elif variable == "not_a_uuid":
        monkeypatch.setenv(ENV, "not-a-uuid")
    code, out, _err = _plan(capsys, target, *flags)
    assert code == 4
    assert _first_line_after_disclosure(out) == POSSIBLY_LIVE
    assert OLD_LIVE not in out and "YOUR CURRENT SESSION" not in out
    assert "UNIT" in out and "BRIEF FOR A RELAUNCH" not in out
    assert "3. [decide]" not in out and "6. [decide]" not in out


@pytest.mark.parametrize("flags", LEVELS)
def test_live_units_the_callers_own_session(tmp_path, monkeypatch, capsys, flags):
    """Branch 3: a session run whose transcript stem is CLAUDE_CODE_SESSION_ID."""
    root = tmp_path / "root"
    target = _session_with_killed_child(root, 1, live=True)
    _no_config(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV, uid(1))
    code, out, _err = _plan(capsys, target, *flags)
    assert code == 4
    assert _first_line_after_disclosure(out) == YOUR_SESSION
    assert "POSSIBLY LIVE" not in out and "Do not modify" not in out
    # the unit block is still printed, with the two read-only look steps and no brief
    assert "UNIT" in out and "1. [look]" in out and "2. [look]" in out
    assert "3. [decide]" not in out and "BRIEF FOR A RELAUNCH" not in out


def test_the_variable_is_compared_in_lowercase(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    target = _session_with_killed_child(root, 1, live=True)
    _no_config(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV, uid(1).upper())
    _code, out, _err = _plan(capsys, target)
    assert _first_line_after_disclosure(out) == YOUR_SESSION


@pytest.mark.parametrize("flags", LEVELS)
def test_a_live_workflow_launched_by_the_callers_session_keeps_the_warning(
    tmp_path, monkeypatch, capsys, flags,
):
    """Branch 4: the parent session id matches, but the workflow's workers may still write."""
    root = tmp_path / "root"
    run_dir = _workflow_with_killed_attempt(root, "fb5", parent=FAKE_UUID)
    _no_config(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV, FAKE_UUID)
    code, out, _err = _plan(capsys, run_dir, *flags)
    assert code == 4
    assert _first_line_after_disclosure(out) == POSSIBLY_LIVE
    assert "YOUR CURRENT SESSION" not in out
    assert "BRIEF FOR A RELAUNCH" not in out and "3. [decide]" not in out


@pytest.mark.parametrize("flags", LEVELS)
def test_a_run_that_is_not_live_never_gets_either_live_line(tmp_path, monkeypatch, capsys, flags):
    root = tmp_path / "root"
    target = _session_with_killed_child(root, 1, live=False)
    _no_config(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV, uid(1))
    _code, out, _err = _plan(capsys, target, *flags)
    assert "YOUR CURRENT SESSION" not in out and "POSSIBLY LIVE" not in out
    assert "UNIT" in out and "BRIEF FOR A RELAUNCH" in out


def test_a_finished_clean_run_still_gets_the_cheerful_sentence(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    target = _session_with_killed_child(root, 1, live=False, kill=False)
    _no_config(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV, uid(1))
    _code, out, _err = _plan(capsys, target)
    assert CHEERFUL in out or "NOTHING TO RESCUE YET: the run has not finished" in out
    assert NOTHING_YET_LIVE not in out


# --- the helper itself: the two guards the CLI paths cannot reach ----------------------------


def test_callers_session_helper_guards(tmp_path):
    """A workflow run is never the caller's session even if its path stem were a UUID, and
    the comparison is case-insensitive on both sides (the CLI lowercases the variable, a
    direct caller may not)."""
    import dataclasses

    from agent_postmortem import reader, rescue

    root = tmp_path / "root"
    session = reader.load_run(_session_with_killed_child(root, 1, live=False))
    assert rescue._is_callers_session(session, uid(1)) is True
    assert rescue._is_callers_session(session, uid(1).upper()) is True
    assert rescue._is_callers_session(session, uid(2)) is False
    assert rescue._is_callers_session(session, None) is False
    upper_stem = dataclasses.replace(session, path=session.path.with_name(uid(1).upper() + ".jsonl"))
    assert rescue._is_callers_session(upper_stem, uid(1)) is True
    workflow = reader.load_run(_workflow_with_killed_attempt(root, "fb7", parent=uid(5), live=False))
    assert workflow.kind == "workflow"
    disguised = dataclasses.replace(workflow, path=workflow.path.with_name(uid(5)))
    assert rescue._is_callers_session(disguised, uid(5)) is False


def test_a_possibly_live_plan_exits_4_even_where_a_finished_one_would_exit_0(monkeypatch):
    """The exit code is unchanged by FB3: live is exit 4 whatever the sentence says. Forced
    through the plan builder with the one fact that would otherwise give exit 0."""
    from agent_postmortem import config, observe, rescue
    from tests.test_rescue import _run

    run = _run([])
    monkeypatch.setattr(rescue, "empty_unit_state", lambda _run: ("ok", 0))
    observation = observe.observe_groups(run, now=_now())
    quiet = rescue.build_plan(run, observation, [], config.load_config(), possibly_live=False)
    live = rescue.build_plan(run, observation, [], config.load_config(), possibly_live=True)
    assert quiet.exit_code == 0
    assert live.exit_code == 4


# --- JSON bytes, status text and exit codes unchanged -----------------------------------


def _fixtures(tmp_path):
    root = tmp_path / "root"
    return {
        "no unit": _plain_live_session(root, 1),
        "units, session": _session_with_killed_child(root, 2, live=True),
        "units, workflow": _workflow_with_killed_attempt(root, "fb6", parent=uid(3)),
    }


def _variable_for(name: str) -> str:
    return {"no unit": uid(1), "units, session": uid(2), "units, workflow": uid(3)}[name]


@pytest.mark.parametrize("name", ["no unit", "units, session", "units, workflow"])
@pytest.mark.parametrize("flags", [[], ["--local"], ["--raw"]])
def test_plan_json_and_exit_code_do_not_depend_on_the_variable(
    tmp_path, monkeypatch, capsys, name, flags,
):
    fixtures = _fixtures(tmp_path)
    _no_config(monkeypatch, tmp_path)
    target = fixtures[name]
    without = _plan(capsys, target, "--json", *flags)
    monkeypatch.setenv(ENV, _variable_for(name))
    with_var = _plan(capsys, target, "--json", *flags)
    assert with_var == without  # exit code, stdout bytes, stderr
    payload = json.loads(without[1])
    assert payload["run"]["possibly_live"] is True
    assert payload["exit_code"] == 4 == without[0]
    # no wording of the text branches reaches the JSON
    for sentence in (NOTHING_YET_LIVE, POSSIBLY_LIVE, YOUR_SESSION, OLD_LIVE):
        assert sentence not in without[1]
    assert "YOUR CURRENT SESSION" not in without[1]


@pytest.mark.parametrize("name", ["no unit", "units, session", "units, workflow"])
@pytest.mark.parametrize("flags", [[], ["--local"], ["--json"]])
def test_rescue_status_does_not_depend_on_the_variable(
    tmp_path, monkeypatch, capsys, name, flags,
):
    fixtures = _fixtures(tmp_path)
    _no_config(monkeypatch, tmp_path)
    target = fixtures[name]
    without = run_cli(capsys, ["rescue", "status", str(target), *flags])
    monkeypatch.setenv(ENV, _variable_for(name))
    with_var = run_cli(capsys, ["rescue", "status", str(target), *flags])
    assert with_var == without
    for sentence in (NOTHING_YET_LIVE, POSSIBLY_LIVE, YOUR_SESSION):
        assert sentence not in without[1]


# --- the contract and the skill ------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


def test_design_states_the_four_branches():
    text = (ROOT / "docs" / "DESIGN.md").read_text("utf-8")
    start = text.index("LIVE rule: when the run is possibly live")
    section = text[start:text.index("No unit at all and the run is not possibly live", start)]
    for sentence in (NOTHING_YET_LIVE, POSSIBLY_LIVE, YOUR_SESSION):
        flat = " ".join(section.split())
        assert " ".join(sentence.split()) in flat.replace("`", "")
    assert "do not modify files in this repo" not in section
    assert "WORKFLOW" in section and "CLAUDE_CODE_SESSION_ID" in section
    assert "`rescue status`" in section


def test_the_rescue_skill_names_all_three_live_lines_and_what_to_do():
    text = (ROOT / "plugin" / "skills" / "rescue" / "SKILL.md").read_text("utf-8")
    flat = " ".join(text.split())
    # step 1 tells the agent what each first line means
    step1 = flat[flat.index("**1. Get the packet.**"):flat.index("**1b.")]
    assert "starts with `POSSIBLY LIVE`, stop right there" in step1
    assert "starts with `YOUR CURRENT SESSION`" in step1
    assert "do not stop" in step1
    assert "NOTHING TO RESCUE YET: the run may still be running" in step1
    assert "another agent may be writing the files" in step1
    # the pin: the sentences the tool prints open with the words the skill looks for
    from agent_postmortem import rescue

    assert hasattr(rescue, "_LIVE_OTHER") and hasattr(rescue, "_LIVE_OWN")
    assert hasattr(rescue, "_NOTHING_YET_LIVE")
    assert rescue._LIVE_OTHER == POSSIBLY_LIVE
    assert rescue._LIVE_OWN == YOUR_SESSION
    assert rescue._NOTHING_YET_LIVE == NOTHING_YET_LIVE
    for sentence in (rescue._LIVE_OTHER, rescue._LIVE_OWN):
        assert sentence.split(":")[0] in step1
