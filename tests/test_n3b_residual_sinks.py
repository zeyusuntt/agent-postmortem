"""N3b Part 3 sink inventory: stderr and stdout lines that carried a path
outside home and /tmp through structural redaction only (config warnings,
trend's state-file error, apply's and `rules --show`'s hook template path).

share: such a path is path-N. local: the P19 rule (outside home and the
repository, path-N), never absolute. A path inside home keeps the structural
`~/...` form. raw prints the real path.
"""

from __future__ import annotations

import pytest

from agent_postmortem import rules, state
from tests import n3b_helpers as h
from tests import n3b_planted as plant

CFG_DIR = "zzCfg" + "Plant" + "7e8"
PKG_DIR = "zzPkg" + "Plant" + "7e9"
STATE_DIR = "zzState" + "Plant" + "7ea"


def _no_absolute(text: str, tmp_path) -> None:
    resolved = str(tmp_path.resolve())
    assert str(tmp_path) not in text and resolved not in text
    for token in text.replace("'", " ").replace('"', " ").split():
        assert not (token.startswith("/") and len(token) > 1), token


@pytest.mark.parametrize("flag", [
    None,
    "--local",
    "--raw",
])
def test_config_warning_never_prints_an_absolute_path_below_raw(flag, tmp_path, monkeypatch, capsys):
    planted = plant.build(tmp_path)
    config = tmp_path / CFG_DIR / "config.toml"
    config.parent.mkdir()
    config.write_text("not toml [[[", encoding="utf-8")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config))
    flags = [flag] if flag else []
    _code, _out, err = h.cli(capsys, "scan", "--root", planted.root, *flags)
    if flag == "--raw":
        assert f"agent-postmortem: {config}: could not parse config:" in err
        return
    assert "agent-postmortem: path-1: could not parse config:" in err
    assert CFG_DIR not in err
    _no_absolute(err, tmp_path)


@pytest.mark.parametrize("flag", [
    None,
    "--local",
    "--raw",
])
def test_trend_state_error_never_prints_an_absolute_path_below_raw(flag, tmp_path, monkeypatch, capsys):
    planted = plant.build(tmp_path)
    state_file = tmp_path / STATE_DIR / "state.json"

    def failing(*_args, **_kwargs):
        raise OSError(13, "Permission denied", str(state_file))

    monkeypatch.setattr(state, "load_state", failing)
    flags = [flag] if flag else []
    code, _out, err = h.cli(capsys, "trend", "--root", planted.root, *flags)
    assert code == 1
    assert "postmortem: could not read the state file:" in err
    if flag == "--raw":
        assert str(state_file) in err
        return
    assert "path-1" in err
    assert STATE_DIR not in err
    _no_absolute(err, tmp_path)


def _package_outside_home(tmp_path, monkeypatch):
    """The template directory as a package installed outside home."""
    hooks = tmp_path / PKG_DIR / "hooks"
    hooks.mkdir(parents=True)
    monkeypatch.setattr(rules, "_HOOKS_DIR", hooks)
    return hooks


@pytest.mark.parametrize("flag", [
    None,
    "--local",
])
def test_apply_hook_note_outside_home_is_path_n_below_raw(flag, tmp_path, monkeypatch, capsys):
    hooks = _package_outside_home(tmp_path, monkeypatch)
    target = tmp_path / "t" / "CLAUDE.md"
    target.parent.mkdir()
    target.write_text("hello\n", encoding="utf-8")
    flags = [flag] if flag else []
    _code, out, _err = h.cli(capsys, "apply", "R-MODEL-ALLOCATION", "--target", target, *flags)
    note = out.partition("Hook template:")[2]
    assert "Hook template: path-" in out
    assert "node path-" in note
    assert "model_allocation_guard.mjs" not in note and PKG_DIR not in out
    assert str(hooks) not in out
    for token in note.split():
        assert not (token.startswith("/") and len(token) > 1), token
    _code, raw, _err = h.cli(capsys, "apply", "R-MODEL-ALLOCATION", "--target", target, "--raw")
    assert f"Hook template: {hooks / 'model_allocation_guard.mjs'}" in raw


@pytest.mark.parametrize("flag", [None, "--local"])
def test_apply_hook_note_inside_home_keeps_the_structural_home_form(flag, tmp_path, monkeypatch, capsys):
    home = tmp_path / "zzhome"
    hooks = home / PKG_DIR / "hooks"
    hooks.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(rules, "_HOOKS_DIR", hooks)
    target = tmp_path / "t" / "CLAUDE.md"
    target.parent.mkdir()
    target.write_text("hello\n", encoding="utf-8")
    flags = [flag] if flag else []
    _code, out, _err = h.cli(capsys, "apply", "R-MODEL-ALLOCATION", "--target", target, *flags)
    assert "Hook template: ~/" in out
    assert str(home) not in out


@pytest.mark.parametrize("flag", [None, "--local"])
def test_rules_show_hook_template_outside_home_is_path_n_below_raw(flag, tmp_path, monkeypatch, capsys):
    hooks = _package_outside_home(tmp_path, monkeypatch)
    flags = [flag] if flag else []
    _code, out, _err = h.cli(capsys, "rules", "--show", "R-MODEL-ALLOCATION", *flags)
    assert "hook template: path-" in out
    assert str(hooks) not in out and PKG_DIR not in out
    _code, raw, _err = h.cli(capsys, "rules", "--show", "R-MODEL-ALLOCATION", "--raw")
    assert f"hook template: {hooks / 'model_allocation_guard.mjs'}" in raw


# --- hook debug tracebacks (stderr, only with AGENT_POSTMORTEM_DEBUG) --------


def test_hook_dispatcher_debug_traceback_prints_no_raw_path_or_uuid(tmp_path, monkeypatch, capsys):
    import io
    import json

    from agent_postmortem import hooks_cli
    from agent_postmortem.cli import main

    uuid = "11111111-2222-4000-8000-" + "444444444444"
    leaked = str(tmp_path / ("zzDebug" + "Plant" + "7eb") / "file.py")

    def boom(_payload):
        raise RuntimeError(f"synthetic failure for session {uuid} at {leaked}")

    monkeypatch.setattr(hooks_cli, "_cmd_check", boom)
    monkeypatch.setenv("AGENT_POSTMORTEM_DEBUG", "1")
    payload = {"hook_event_name": "Stop", "transcript_path": "x", "session_id": uuid,
               "stop_hook_active": False}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    capsys.readouterr()
    assert main(["hook", "check"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "RuntimeError" in captured.err and "synthetic failure" in captured.err
    assert uuid not in captured.err
    assert "uuid:4444" in captured.err
    assert "zzDebug" not in captured.err and str(tmp_path) not in captured.err
