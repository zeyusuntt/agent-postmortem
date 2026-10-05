"""N3b D-b (mixing guard) and D-c (diagnostic) for `run` text and the bug
report: one output never mixes stable and positional aliases, and when the
final book is unavailable the one line
"agent-postmortem run: stable aliases unavailable" goes to stderr, never to
stdout.
"""

from __future__ import annotations

import os
import re
import stat

import pytest

from agent_postmortem import report, state
from agent_postmortem.cli import main
from tests import n3b_planted as plant

DIAGNOSTIC = "agent-postmortem run: stable aliases unavailable"
STABLE_AGENT_FILE = re.compile(r"agent-[0-9a-f]{12,}\.jsonl")
POSITIONAL_AGENT_FILE = re.compile(r"agent-[0-9]+\.jsonl")


@pytest.fixture
def planted(tmp_path):
    return plant.build(tmp_path)


def _run(planted, capsys, *flags):
    capsys.readouterr()
    code = main(["run", str(planted.run_dir), *flags])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _make_secret_unavailable(monkeypatch, tmp_path):
    """A secret file with mode 0644: P5 says unavailable, never repaired."""
    home = tmp_path / "unavailable-xdg"
    directory = home / "agent-postmortem"
    directory.mkdir(parents=True, mode=0o700)
    secret = directory / "secret"
    secret.write_bytes(b"s" * 32)
    os.chmod(secret, 0o644)
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    return secret


# --- D-c: unavailable at the start ----------------------------------------


@pytest.mark.parametrize("flags", [["--verbose", "--evidence"], ["--bug-report"]])
def test_unavailable_secret_gives_positional_names_and_one_stderr_line(
    flags, planted, tmp_path, monkeypatch, capsys,
):
    secret = _make_secret_unavailable(monkeypatch, tmp_path)
    before = (secret.read_bytes(), secret.stat().st_mtime_ns, stat.S_IMODE(secret.stat().st_mode))
    _code, out, err = _run(planted, capsys, *flags)
    assert err.splitlines() == [DIAGNOSTIC]
    assert "unavailable" not in out
    assert POSITIONAL_AGENT_FILE.search(out)
    assert not STABLE_AGENT_FILE.search(out)
    assert not re.search(r"project:[0-9a-f]{12}", out)
    for value in (planted.agent1, planted.agent2, planted.session, planted.slug, planted.label):
        assert value not in out
    # the unavailable secret is never repaired
    assert (secret.read_bytes(), secret.stat().st_mtime_ns,
            stat.S_IMODE(secret.stat().st_mode)) == before


def test_unavailable_bug_report_names_the_project_positionally(
    planted, tmp_path, monkeypatch, capsys,
):
    _make_secret_unavailable(monkeypatch, tmp_path)
    _code, out, _err = _run(planted, capsys, "--bug-report")
    assert "project project-1, 1 logical agent(s):" in out


def test_positional_numbering_is_stable_across_two_unavailable_renders(
    planted, tmp_path, monkeypatch, capsys,
):
    _make_secret_unavailable(monkeypatch, tmp_path)
    _code, first, _err = _run(planted, capsys, "--verbose")
    _code, second, _err = _run(planted, capsys, "--verbose")
    assert first == second


def test_available_book_prints_no_diagnostic(planted, capsys):
    _code, out, err = _run(planted, capsys, "--verbose")
    assert err == ""
    assert STABLE_AGENT_FILE.search(out)


# N5a-1: share JSON renders aliases, so it prints the diagnostic like text does;
# JSON at local and raw renders none.
@pytest.mark.parametrize(
    "flags", [["--local"], ["--raw"], ["--json", "--local"], ["--json", "--raw"]],
)
def test_levels_that_render_no_alias_print_no_diagnostic(
    flags, planted, tmp_path, monkeypatch, capsys,
):
    _make_secret_unavailable(monkeypatch, tmp_path)
    _code, out, err = _run(planted, capsys, *flags)
    assert err == ""
    assert "unavailable" not in out


# --- D-b: the mixing guard -------------------------------------------------


def _fail_slow_path(monkeypatch):
    """Registration succeeds once; every later lock (the slow path) fails."""
    real_lock = state.lock_state_dir
    calls = {"n": 0}

    def flaky_lock(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("simulated lock failure on the slow path")
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(state, "lock_state_dir", flaky_lock)
    return calls


def _drop_collected_agents(monkeypatch):
    """Make the renderer meet identities that were never collected."""
    real_collect = report.collect_identities

    def collect_without_agents(run, findings=None):
        return [item for item in real_collect(run, findings) if item[0] != "agent"]

    monkeypatch.setattr(report, "collect_identities", collect_without_agents)


@pytest.mark.parametrize("flags", [["--verbose", "--evidence"], ["--bug-report"]])
def test_a_slow_path_failure_mid_render_re_renders_with_positional_names_only(
    flags, planted, monkeypatch, capsys,
):
    _drop_collected_agents(monkeypatch)
    calls = _fail_slow_path(monkeypatch)
    _code, out, err = _run(planted, capsys, *flags)
    assert calls["n"] >= 2  # the slow path really ran and failed
    assert err.splitlines() == [DIAGNOSTIC]  # once
    assert POSITIONAL_AGENT_FILE.search(out)
    assert not STABLE_AGENT_FILE.search(out)
    assert not re.search(r"project:[0-9a-f]{12}", out)
    for value in (planted.agent1, planted.agent2, planted.session, planted.slug):
        assert value not in out


def test_a_mixed_output_never_reaches_stdout_even_for_collected_identities(
    planted, monkeypatch, capsys,
):
    """The project was collected and registered stably, yet the whole output
    is positional once the book degrades."""
    _drop_collected_agents(monkeypatch)
    _fail_slow_path(monkeypatch)
    _code, out, _err = _run(planted, capsys, "--bug-report")
    assert "project project-1, 1 logical agent(s):" in out


def test_the_guard_is_a_no_op_when_nothing_degrades(planted, monkeypatch, capsys):
    calls = {"renders": 0}
    real_render = report.render_run_text

    def counting_render(*args, **kwargs):
        calls["renders"] += 1
        return real_render(*args, **kwargs)

    monkeypatch.setattr(report, "render_run_text", counting_render)
    _run(planted, capsys, "--verbose")
    assert calls["renders"] == 1


def test_the_guard_re_renders_exactly_once(planted, monkeypatch, capsys):
    _drop_collected_agents(monkeypatch)
    _fail_slow_path(monkeypatch)
    calls = {"renders": 0}
    real_render = report.render_run_text

    def counting_render(*args, **kwargs):
        calls["renders"] += 1
        return real_render(*args, **kwargs)

    monkeypatch.setattr(report, "render_run_text", counting_render)
    _run(planted, capsys, "--verbose")
    assert calls["renders"] == 2
