"""N3b fix A, C6: the /tmp collapse is anchored to a path token and never
crosses whitespace into prose.
"""

from __future__ import annotations

import pytest

from agent_postmortem.redact import redact
from tests import n3b_helpers as h
from tests import n3b_planted as plant

SECRET = "zz" + "secret" + "9d1"


# --- C6 ---------------------------------------------------------------------

SENTENCE = (
    "could not parse config: Expected '=' after a key in a key/value pair "
    "(at line 1, column 5)"
)


def test_an_interior_tmp_segment_does_not_swallow_the_following_prose():
    text = f"agent-postmortem: /srv/x/.probe/tmp/CLIENT/agent-postmortem/config.toml: {SENTENCE}"
    assert SENTENCE in redact(text, home=None)


def test_a_real_tmp_path_still_collapses_and_stops_at_whitespace():
    shown = redact("see /tmp/run1/a/b/c.txt: boom key/value pair", home=None)
    assert shown == "see /tmp/run1/.../c.txt: boom key/value pair"


@pytest.mark.parametrize("flag", [None, "--local"])
def test_config_warning_with_an_interior_tmp_dir_keeps_the_sentence(
    flag, tmp_path, monkeypatch, capsys
):
    planted = plant.build(tmp_path)
    config = tmp_path / ".probe" / "tmp" / "zzclient" / "agent-postmortem" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("not toml [[[", encoding="utf-8")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config))
    flags = [flag] if flag else []
    _code, _out, err = h.cli(capsys, "scan", "--root", planted.root, *flags)
    assert "agent-postmortem: path-1: could not parse config:" in err
    assert "(at line 1, column 5)" in err
    assert "zzclient" not in err
