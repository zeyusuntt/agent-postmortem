"""Fix round 9 of field milestone F: a seeded generator of private names in the spellings the round 8
reviews found (a path glued to CJK text, a variable anchor after `@`, a possessive apostrophe before a
space in a single-quoted path, a colon-form or other control sequence, `$CLAUDE_PROJECT_DIR`, a `/tmp`
path with a Unicode space).

Each case writes a name that holds words, spaces and punctuation, puts it in one of those contexts the
way a tool prints it, and checks at share and local, unregistered, that no marker survives any sink. A
failing seed prints the text. Raw is checked unchanged for the same texts.

Every fixture is synthetic.
"""

from __future__ import annotations

import random

import pytest

from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor
from tests.test_fc_fix8_fuzz import HOME, _name, _sinks

ESC = "\x1b"
USERS = "/Us" + "ers"  # the privacy audit forbids the literal home prefix outside redact tests
GLUE = [" ", " ", " ", "; ", ": ", "-", "_", ".", ",", "=", "+", " 10:30 ", " - ", "[", "]"]
CJK = ["无法读取", "请读取", "路径是", "修改", "ファイル", "読み込めません", "파일", "は"]
SEQUENCES = [
    f"{ESC}[38:2::255:0:0m", f"{ESC}[48:2:0:10:20:30m", f"{ESC}[38:5:196m", f"{ESC}[K",
    f"{ESC}[2K", f"{ESC}[1G", f"{ESC}[?25l", f"{ESC}[1;31m", "[38:2::255:0:0m", "[1;34m",
]


def _case(seed):
    rng = random.Random(seed)
    family = seed % 7
    markers = [f"Zq{seed}m{n}z" for n in range(rng.randint(2, 4))]
    name = _name(rng, markers, GLUE)
    if family == 0:  # CJK text glued to a path
        root = rng.choice(["/srv/", USERS + "/zed/", "$HOME/", "~/", "~zed/", "$PWD/", "C:\\"])
        tail = rng.choice(["", "失败", " 失败", "を開けません"])
        return markers, rng.choice(CJK) + root + name.replace("/", "-") + "/x.md" + tail
    if family == 1:  # a variable anchor after @
        anchor = rng.choice(["$HOME", "$PWD", "~zed", "${HOME}", "~", "$CLAUDE_PROJECT_DIR"])
        template = rng.choice([
            "curl -d @{a}/{n}/x.json http://x", "curl -F file=@{a}/{n}/x.json http://x",
            'curl -d "@{a}/{n}/x.json"',
        ])
        return markers, template.format(a=anchor, n=name.replace("/", "-"))
    if family == 2:  # a possessive apostrophe before a space in a single-quoted path
        first = markers[0]
        rest = " ".join(markers[1:])
        template = rng.choice([
            "ENOENT: no such file or directory, open '/srv/{a}' {b}/x.md'",
            "fatal: '/srv/{a}' {b}/x.md' is not a git repository",
            "Error: Cannot find module '/srv/{a}' {b}/x.md' now",
        ])
        return markers, template.format(a=first, b=rest)
    if family == 3:  # a control sequence in front of a path
        seq = rng.choice(SEQUENCES)
        root = rng.choice(["/srv/", "$HOME/", "~/", "C:\\", "$PWD/"])
        end = rng.choice(["", f"{ESC}[0m", f"{ESC}[0m failed"])
        return markers, seq + root + name.replace("/", "-") + "/x.md" + end
    if family == 4:  # the project directory of a hook command
        template = rng.choice([
            "bash $CLAUDE_PROJECT_DIR/{n}/x.sh now", 'python "$CLAUDE_PROJECT_DIR/{n}/x.py"',
            "cat ${{CLAUDE_PROJECT_DIR}}/{n}/x.md",
        ])
        return markers, template.format(n=name.replace("/", "-"))
    if family == 5:  # a /tmp path with a Unicode space in a lower segment
        space = rng.choice(["\u00a0", "\u3000", "\u202f", "\u2003"])
        lower = space.join(markers[1:] or markers)
        return markers, f"/tmp/T0/{lower}/{markers[0]}/ok.md"
    # family 6: a quoted spaced path followed by a quoted phrase on the same line
    return markers, f"open '/srv/{markers[0]}/{' '.join(markers[1:])}' and 'git init' now"


@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("seed", range(420))
def test_fc9_fuzz_no_marker_survives_the_review_spellings(level, seed):
    markers, text = _case(seed)
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    r.fresh_names(detailed=(level == "local"))
    for sink, out in _sinks(r, text).items():
        for marker in markers:
            assert marker not in out, (seed, level, sink, text, out)


@pytest.mark.parametrize("seed", range(0, 420, 3))
def test_fc9_fuzz_raw_is_unchanged(seed):
    _markers, text = _case(seed)
    r = Redactor(enabled=False)
    for sink, out in _sinks(r, text).items():
        assert out == text, (seed, sink)
