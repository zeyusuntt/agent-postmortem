"""N3b fix A, C4: a path the tool itself interpolates (config warnings, the
state-file error) is redacted before interpolation, so a space in it leaks
no tail.
"""

from __future__ import annotations

import pytest

from agent_postmortem import state
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor
from tests import n3b_helpers as h
from tests import n3b_planted as plant

SECRET = "zz" + "secret" + "9d1"


# --- C4 ---------------------------------------------------------------------

DIR_NAMES = [
    "zzsp" + "ace dir",
    "zztab" + "\tdir",
    "zzapos" + "'trophe dir",
    "zzquote" + '"d dir',
    "zzuni" + "\u00fc\u4e2d dir",
]


@pytest.mark.parametrize("flag", [None, "--local"])
@pytest.mark.parametrize("name", DIR_NAMES)
def test_config_warning_with_awkward_directory_names_leaks_no_tail(
    name, flag, tmp_path, monkeypatch, capsys
):
    planted = plant.build(tmp_path)
    config = tmp_path / name / "agent-postmortem" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("not toml [[[", encoding="utf-8")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config))
    flags = [flag] if flag else []
    _code, _out, err = h.cli(capsys, "scan", "--root", planted.root, *flags)
    assert "agent-postmortem: path-1: could not parse config:" in err
    for fragment in ("zzsp", "zztab", "zzapos", "zzquote", "zzuni", "dir", "agent-postmortem/config"):
        assert fragment not in err, (fragment, err)


@pytest.mark.parametrize("flag", [None, "--local"])
def test_two_paths_in_one_config_warning_both_leave_no_tail(flag, tmp_path, monkeypatch, capsys):
    planted = plant.build(tmp_path)
    config = tmp_path / ("zzone " + "cfg") / "config.toml"
    config.parent.mkdir()
    other = tmp_path / ("zztwo " + "repo") / "r"
    config.write_text(
        f'[[repos]]\npath = "{other}"\nchecks = "x"\n', encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config))
    flags = [flag] if flag else []
    _code, _out, err = h.cli(capsys, "scan", "--root", planted.root, *flags)
    assert "must be a list of strings" in err
    for fragment in ("zzone", "zztwo", "cfg"):
        assert fragment not in err, (fragment, err)
    assert "path-1" in err and "path-2" in err


@pytest.mark.parametrize("flag", [None, "--local"])
def test_state_file_error_with_a_spaced_path_leaks_no_tail(flag, tmp_path, monkeypatch, capsys):
    planted = plant.build(tmp_path)
    state_file = tmp_path / ("zzstate " + "dir") / "my state.json"

    def failing(*_args, **_kwargs):
        raise OSError(13, "Permission denied", str(state_file))

    monkeypatch.setattr(state, "load_state", failing)
    flags = [flag] if flag else []
    code, _out, err = h.cli(capsys, "trend", "--root", planted.root, *flags)
    assert code == 1
    assert "Permission denied" in err
    for fragment in ("zzstate", "dir", "state.json", "my "):
        assert fragment not in err, (fragment, err)


def test_display_path_forms():
    share = Redactor(context=DisclosureContext(level="share"))
    assert share.display_path("/opt/zz a/b c") == "path-1"
    assert share.display_path("/opt/zz a/b c") == "path-1"
    assert share.display_path("/opt/other") == "path-2"
    raw = Redactor(context=DisclosureContext(level="raw"))
    assert raw.display_path("/opt/zz a/b c") == "/opt/zz a/b c"
    from pathlib import Path
    home = Path("/srv/people/tester")
    inside = Redactor(context=DisclosureContext(level="share", home=home))
    assert inside.display_path(str(home / "x y" / "my notes.md")) == "~/.../my notes.md"
    assert inside.display_path(str(home / "my notes.md")) == "~/my notes.md"


