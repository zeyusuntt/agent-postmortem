"""Fix round 9 of field milestone F: an end-to-end probe through the real CLI of the spellings the
round 8 reviews found (a path glued to CJK text, a variable anchor after `@`, a possessive
apostrophe before a space in a single-quoted path, a colon-form colour sequence and other control
sequences, `$CLAUDE_PROJECT_DIR`, a `/tmp` path with a Unicode space).

A planted run whose label, phase or transcript excerpt holds a private name spelled that way is read
by every command that prints text from a run, at share and at local, as text and as JSON, with and
without `--evidence`. No marker of the private name may reach stdout or stderr. The probe is the
proof the inventory rows of `.reviews/tooling/f/inventory_fix9.py` that name an output family point
at; `tests/test_fc_fix8_e2e.py` is the same probe for the round 8 names.

Every fixture is synthetic.
"""

from __future__ import annotations

import pytest

from tests import n3b_helpers as h
from tests.test_fc_fix7_e2e import _build, _commands

MARKERS = ("Zq9a", "Zq9b", "Zq9c")
ESC = "\x1b"
USERS = "/Us" + "ers"  # the privacy audit forbids the literal home prefix outside redact tests

NAMES = {
    "cjk_then_a_spaced_path": "请读取/srv/Zq9a Zq9b/Zq9c.md失败",
    "cjk_then_a_home_path": "无法读取" + USERS + "/zed/Zq9a/Zq9b.md",
    "kana_then_a_spaced_path": "ファイル/srv/Zq9a Zq9b/Zq9c を開けません",
    "curl_at_home_variable": "curl -d @$HOME/Zq9a/Zq9b/Zq9c.json http://x",
    "curl_at_other_home": "curl -d @~zed/Zq9a/Zq9b/Zq9c.json http://x",
    "possessive_then_a_space": "ENOENT: no such file or directory, open '/srv/Zq9a' Zq9b/Zq9c.md'",
    "colon_form_colour": f"{ESC}[38:2::255:0:0m/srv/Zq9a Zq9b/Zq9c.md{ESC}[0m failed",
    "colon_form_stripped": "[38:2::255:0:0m/srv/Zq9a Zq9b/Zq9c.md[0m failed",
    "erase_in_line": f"{ESC}[K/srv/Zq9a Zq9b/Zq9c.md",
    "project_dir_variable": "bash $CLAUDE_PROJECT_DIR/Zq9a Zq9b/Zq9c.sh now",
    "tmp_with_a_unicode_space": "/tmp/T0/Zq9a Zq9b/Zq9c/ok.md",
}
FIELDS = ["label", "phase", "excerpt"]

def _rows():
    return [
        pytest.param(name, field, level, id=f"{level}-{field}-{name}")
        for name in NAMES for field in FIELDS for level in ["share", "local"]
    ]


@pytest.mark.parametrize("name,field,level", _rows())
def test_fc9_no_command_prints_a_marker_of_a_private_name(name, field, level, tmp_path, capsys):
    planted = _build(tmp_path, field, NAMES[name])
    leaks = []
    for command, argv in _commands(planted).items():
        _code, out, err = h.cli(capsys, *argv, *(["--local"] if level == "local" else []))
        for marker in MARKERS:
            if marker in out or marker in err:
                leaks.append((command, marker))
    assert not leaks, (name, field, level, leaks[:6])


@pytest.mark.parametrize("field", FIELDS)
def test_fc9_the_probe_is_not_vacuous_raw_prints_the_marker_of_each_field(field, tmp_path, capsys):
    planted = _build(tmp_path, field, NAMES["cjk_then_a_spaced_path"])
    seen = []
    for command, argv in _commands(planted).items():
        _code, out, err = h.cli(capsys, *argv, "--raw")
        if "Zq9a" in out or "Zq9a" in err:
            seen.append(command)
    assert seen, field
