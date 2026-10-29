"""N3b Part 2, P13: handler-level errors (no run found, ambiguous run
argument, not a run, no runs under a root, a bad date, no such rule) go
through the same span renderer as parser errors.

share and local: no raw path, UUID, agent id or run argument is echoed; each
message names the kind of argument expected; the spans are numbered path-N,
value-N, uuid:<last4>, agent-N. raw: the message exactly as before.
"""

from __future__ import annotations

import shutil
import uuid

import pytest

from tests import n3b_helpers as h
from tests import n3b_planted as plant

UUID = str(uuid.UUID(int=0x7E57AB1E00000000000000000000A001))
DIRNAME = "zzMissing" + "Plant" + "7f1"
TYPED = "wf_zz" + "Plant" + "7f2"
RUN_COMMANDS = {
    "run": ["run"],
    "rescue plan": ["rescue", "plan"],
    "rescue status": ["rescue", "status"],
    "rules --run": ["rules", "--run"],
}
EXPECTED_RUN = (
    "expected a run id, a unique prefix of one, a session UUID, an agent-<id> "
    "transcript stem, a:<alias>, or the path of a run directory"
)


@pytest.fixture
def planted(tmp_path):
    return plant.build(tmp_path)


def _missing_path(tmp_path) -> str:
    return str(tmp_path / DIRNAME / UUID / "run")


def _run_cmd(name, argument, planted, *flags):
    return [*RUN_COMMANDS[name], str(argument), "--root", str(planted.root), *flags]


def _assert_clean(err: str, planted, tmp_path, *values: str):
    for value in (UUID, DIRNAME, str(tmp_path), planted.slug, planted.session, planted.agent1,
                  str(planted.root), *values):
        assert value not in err, value
    assert "Traceback" not in err


# --- no run found -----------------------------------------------------------


@pytest.mark.parametrize("level", [[], ["--local"]])
@pytest.mark.parametrize("name", sorted(RUN_COMMANDS))
def test_no_run_matches_a_path_with_a_uuid_echoes_nothing(name, level, planted, tmp_path, capsys):
    code, out, err = h.cli(capsys, *_run_cmd(name, _missing_path(tmp_path), planted, *level))
    assert code == 2
    assert out == ""
    assert "postmortem: no run matches 'path-1' under path-2" in err
    assert EXPECTED_RUN in err
    _assert_clean(err, planted, tmp_path)


@pytest.mark.parametrize("name", sorted(RUN_COMMANDS))
def test_no_run_matches_a_typed_prefix_and_a_raw_agent_id(name, planted, capsys):
    code, _out, err = h.cli(capsys, *_run_cmd(name, TYPED, planted))
    assert code == 2
    assert "no run matches 'value-1'" in err
    assert TYPED not in err
    code, _out, err = h.cli(capsys, *_run_cmd(name, planted.agent1, planted))
    assert code == 2
    assert "no run matches 'agent-1'" in err
    assert planted.agent1 not in err


@pytest.mark.parametrize("name", sorted(RUN_COMMANDS))
def test_no_run_matches_at_raw_is_todays_message(name, planted, tmp_path, capsys):
    missing = _missing_path(tmp_path)
    code, _out, err = h.cli(capsys, *_run_cmd(name, missing, planted, "--raw"))
    assert code == 2
    assert err == f"postmortem: no run matches '{missing}' under {planted.root}\n"


# --- ambiguous run argument -------------------------------------------------


@pytest.fixture
def two_runs(planted):
    """A second run whose id shares the prefix `wf_00000000-b1`."""
    source = planted.root / planted.slug / planted.session / "subagents" / "workflows"
    shutil.copytree(source / plant.WF_ID, source / "wf_00000000-b11")
    return "wf_00000000-b1"


@pytest.mark.parametrize("level", [[], ["--local"]])
@pytest.mark.parametrize("name", sorted(RUN_COMMANDS))
def test_ambiguous_prefix_lists_candidate_ids_and_echoes_no_path(
    name, level, planted, two_runs, tmp_path, capsys,
):
    code, out, err = h.cli(capsys, *_run_cmd(name, two_runs, planted, *level))
    assert code == 1
    assert out == ""
    assert "matches more than one run under path-1" in err
    assert "wf_00000000-b10, wf_00000000-b11" in err
    assert "expected a longer prefix or the path of a run directory" in err
    assert f"'{two_runs}'" not in err
    _assert_clean(err, planted, tmp_path)


def test_ambiguous_prefix_at_raw_is_todays_message(planted, two_runs, capsys):
    code, _out, err = h.cli(capsys, *_run_cmd("run", two_runs, planted, "--raw"))
    assert code == 1
    assert err == (
        f"postmortem: '{two_runs}' matches more than one run under {planted.root}: "
        "wf_00000000-b10, wf_00000000-b11\n"
    )


# --- not a run --------------------------------------------------------------


@pytest.mark.parametrize("name", ["run", "rescue plan", "rescue status", "rules --run"])
def test_a_file_that_is_not_a_run_is_named_path_n(name, planted, tmp_path, capsys):
    not_a_run = tmp_path / (DIRNAME + ".txt")
    not_a_run.write_text("hello\n", encoding="utf-8")
    code, _out, err = h.cli(capsys, *_run_cmd(name, not_a_run, planted))
    assert code == 2
    assert "'path-1' is not a run (expected a workflow run directory or a session transcript)" in err
    _assert_clean(err, planted, tmp_path)
    code, _out, err = h.cli(capsys, *_run_cmd(name, not_a_run, planted, "--raw"))
    assert err == f"postmortem: '{not_a_run}' is not a run\n"


# --- no runs under a root ---------------------------------------------------


@pytest.mark.parametrize("argv", [
    ["scan"], ["scan", "--all"], ["trend"], ["rules"], ["rules", "propose"], ["rules", "review"],
])
def test_no_runs_under_a_root_names_path_n_and_raw_is_todays(argv, planted, tmp_path, monkeypatch, capsys):
    # `rules review` reads the adopted rules first; give it one so it reaches the root check
    config = tmp_path / "adopted.toml"
    config.write_text(
        '[[adopted]]\nrule = "R-GIT-CWD"\ndate = "2026-08-15"\ntarget = "w.md"\n', encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config))
    empty = tmp_path / DIRNAME / UUID
    empty.mkdir(parents=True)
    code, _out, err = h.cli(capsys, *argv, "--root", empty)
    assert code == 2
    # docs/DESIGN.md 4.8 (0.3.4): the empty state adds fixed guidance lines (no path or id)
    assert err == (
        "postmortem: no runs found under path-1\n"
        "  agent-postmortem reads the transcripts Claude Code saves there: Workflow runs, and sessions with the subagents they launched.\n"
        "  next: agent-postmortem demo   (a sample report on a packaged synthetic run)\n"
        "  If Claude Code saves its data somewhere else, pass --root <dir> or set CLAUDE_CONFIG_DIR. It deletes transcripts older than 30 days by default.\n"
    )
    code, _out, err = h.cli(capsys, *argv, "--root", empty, "--raw")
    assert err.startswith(f"postmortem: no runs found under {empty}\n  agent-postmortem reads")


# --- bad date ---------------------------------------------------------------


@pytest.mark.parametrize("argv", [["scan"], ["rules", "propose"]])
def test_bad_date_names_the_format_and_not_the_value(argv, planted, capsys):
    bad = "2026-13-" + "Plant" + "7f3"
    code, _out, err = h.cli(capsys, *argv, "--root", planted.root, "--since", bad)
    assert code == 1
    assert err == "postmortem: --since must be YYYY-MM-DD, got 'value-1'\n"
    code, _out, err = h.cli(capsys, *argv, "--root", planted.root, "--since", bad, "--raw")
    assert err == f"postmortem: --since must be YYYY-MM-DD, got '{bad}'\n"


# --- no such rule -----------------------------------------------------------


def test_rules_show_unknown_rule_names_the_expected_kind(capsys):
    typed = "R-ZZ" + "Plant" + "7f4"
    code, _out, err = h.cli(capsys, "rules", "--show", typed)
    assert code == 1
    assert "no such rule 'value-1' (expected a rule id such as R-RETRY-SAFE" in err
    assert typed not in err
    code, _out, err = h.cli(capsys, "rules", "--show", typed, "--raw")
    assert err == f"postmortem: no such rule '{typed}'\n"


# --- local: the same spans, and no label for an error with no stdout --------


def test_local_error_prints_no_first_line_label(planted, tmp_path, capsys):
    code, out, err = h.cli(capsys, "run", _missing_path(tmp_path), "--local")
    assert code == 2
    assert out == ""
    assert "disclosure" not in err
