"""Fix round 8 of field milestone F: an end-to-end probe through the real CLI of the spellings the
round 7 reviews found (an apostrophe inside a single-quoted path, a home variable inside a quoted
command, a colour sequence, a path after `@`, a Windows namespace path, `$PWD`, a root path that
starts with a space, a Windows path before JSON).

A planted run whose label, phase or transcript excerpt holds a private name spelled that way is read
by every command that prints text from a run, at share and at local, as text and as JSON, with and
without `--evidence`. No marker of the private name may reach stdout or stderr. The probe is the
proof the inventory rows of `.reviews/tooling/f/inventory_fix8.py` that name an output family point
at; `tests/test_fc_fix7_e2e.py` is the same probe for the round 7 names.

Every fixture is synthetic.
"""

from __future__ import annotations

import pytest

from tests import n3b_helpers as h
from tests.test_fc_fix7_e2e import _build, _commands

MARKERS = ("Zq7a", "Zq7b", "Zq7c")
ESC = "\x1b"

NAMES = {
    "apostrophe_in_single_quotes": "ENOENT: no such file or directory, open '/srv/Zq7a/Bob's Zq7b/Zq7c.md'",
    "git_fatal": "fatal: '/srv/Zq7a/Bob's Zq7b' is not a git repository",
    "home_variable_in_a_command": 'sh -c "cd $HOME/Zq7a/Zq7b/Zq7c"',
    "other_home_in_a_quote": 'x="cat ~zed/Zq7a/Zq7b/Zq7c.md"',
    "colour_sequence": f"{ESC}[31m/srv/Zq7a/Zq7b/Zq7c.md{ESC}[0m failed",
    "colour_sequence_stripped": "[31m/srv/Zq7a Zq7b/Zq7c.md[0m failed",
    "curl_at_path": "curl -d @/srv/Zq7a Zq7b/Zq7c.json http://x",
    "extended_unc": "open \\\\?\\UNC\\server\\share\\Zq7a Zq7b\\Zq7c.md now",
    "pwd_path": "open $PWD/Zq7a Zq7b/deep/Zq7c.md now",
    "braced_pwd_path": "open ${PWD}/Zq7a Zq7b/deep/Zq7c.md now",
    "root_with_a_leading_space": 'cannot read "/ Zq7a Zq7b/Zq7c.md" denied',
    "windows_then_json": 'open C:\\Zq7a\\Zq7b X {"relative":"Zq7c\\\\notes.md","status":"failed"}',
}
FIELDS = ["label", "phase", "excerpt"]


@pytest.mark.parametrize("name", list(NAMES))
@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("level", ["share", "local"])
def test_fc8_no_command_prints_a_marker_of_a_private_name(name, field, level, tmp_path, capsys):
    planted = _build(tmp_path, field, NAMES[name])
    leaks = []
    for command, argv in _commands(planted).items():
        _code, out, err = h.cli(capsys, *argv, *(["--local"] if level == "local" else []))
        for marker in MARKERS:
            if marker in out or marker in err:
                leaks.append((command, marker))
    assert not leaks, (name, field, level, leaks[:6])


@pytest.mark.parametrize("field", FIELDS)
def test_fc8_the_probe_is_not_vacuous_raw_prints_the_marker_of_each_field(field, tmp_path, capsys):
    planted = _build(tmp_path, field, NAMES["apostrophe_in_single_quotes"])
    seen = []
    for command, argv in _commands(planted).items():
        _code, out, err = h.cli(capsys, *argv, "--raw")
        if "Zq7a" in out or "Zq7a" in err:
            seen.append(command)
    assert seen, field
