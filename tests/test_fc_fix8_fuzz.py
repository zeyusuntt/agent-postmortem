"""Fix round 8 of field milestone F: a seeded generator of private names in the spellings the round 7
reviews found (an apostrophe inside a single-quoted path, a home or working-directory variable inside
a command, a colour sequence, a path after `@`, a Windows namespace path, a root path that starts with
a space, a Windows path before JSON).

Each case writes a name that holds words, spaces and punctuation, puts it in one of those contexts the
way a tool prints it, and checks at share and local, unregistered, that no marker survives any sink. A
failing seed prints the text. Raw is checked unchanged for the same texts.

Every fixture is synthetic.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from agent_postmortem import report
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor

HOME = Path("/home/user")
ESC = "\x1b"
GLUE = [
    " ", " ", " ", "; ", " | ", " & ", ": ", " (", ") ", "-", "_", ".", ",", "=", "+", "%", "!",
    " 10:30 ", "\u2019 ", "\u201d ", " - ", " -> ", "[", "]",
]
# a glue that holds an apostrophe between two word characters (`Bob's`). A name that ends a word
# with an apostrophe (`Clients' files`) is the recorded possessive-plural limit and is not generated.
APOSTROPHE_GLUE = ["'s ", "'s", "'"]


def _name(rng, markers, glue):
    parts = []
    for index, marker in enumerate(markers):
        parts.append(marker)
        if index < len(markers) - 1:
            parts.append(rng.choice(glue))
    return "".join(parts)


def _markers(seed, kind, count):
    return [f"Zq{seed}{kind}{n}z" for n in range(count)]


def _case(seed):
    """(markers, text) for one seed: the family is the seed modulo the number of families."""
    rng = random.Random(seed)
    family = seed % 9
    markers = _markers(seed, "m", rng.randint(2, 4))
    if family == 0:  # an apostrophe inside a single-quoted path (Node, git, GNU messages)
        name = _name(rng, markers, APOSTROPHE_GLUE + [" ", " "])
        if "'" not in name:
            name = markers[0] + "'s " + "".join(markers[1:])
        template = rng.choice([
            "ENOENT: no such file or directory, open '{}'", "Error: Cannot find module '{}'",
            "fatal: '{}' is not a git repository", "open('{}')", "cp: cannot stat '{}': denied",
        ])
        return markers, template.format("/srv/" + name + "/x.txt")
    if family == 1:  # a home variable inside a command
        name = _name(rng, markers, GLUE)
        quote = rng.choice(['"', "'"])
        if quote == "'" and "'" in name:
            quote = '"'
        template = rng.choice([
            "sh -c {q}cd $HOME/{n}/ok.md{q}", "bash -lc {q}source ${{HOME}}/{n}/ok.md{q}",
            "echo {q}x $HOME/{n}/ok.md{q}", "x={q}cat ~zed/{n}/ok.md{q}",
        ])
        return markers, template.format(q=quote, n=name)
    if family == 2:  # a colour sequence in front of a path
        name = _name(rng, markers, GLUE)
        sgr = rng.choice(["[31m", "[1;34m", "[01;31m", "[0;32m"])
        lead = rng.choice([ESC, ""])
        end = rng.choice([f"{ESC}[0m", f"{ESC}[m", ""])
        return markers, rng.choice(["{}", "ls: {}", "{} failed"]).format(
            f"{lead}{sgr}/srv/data/{name}/x.md{end}"
        )
    if family == 3:  # a path after @
        name = _name(rng, markers, GLUE)
        template = rng.choice([
            "curl -d @/srv/{n}/x.json http://x", "curl -F file=@/srv/{n}/x.json http://x",
            "curl --data-binary @/srv/{n}",
        ])
        return markers, template.format(n=name)
    if family == 4:  # a Windows namespace path
        name = _name(rng, markers, GLUE).replace("/", "-")
        prefix = rng.choice([
            "\\\\?\\UNC\\server\\share\\", "\\\\?\\C:\\Docs\\", "\\\\.\\C:\\Docs\\",
        ])
        return markers, rng.choice(["open {} now", "{}", "cannot read {}: denied"]).format(
            prefix + name + "\\x.md"
        )
    if family == 5:  # a working-directory variable
        name = _name(rng, markers, GLUE)
        anchor = rng.choice(["$PWD", "${PWD}", "$OLDPWD", "${OLDPWD}"])
        return markers, rng.choice(["open {} now", "{}", "cd {}"]).format(
            f"{anchor}/{name}/deep/x.md"
        )
    if family == 6:  # a quoted root path whose first segment starts with a space (F fix 9, W9-4)
        name = _name(rng, markers, [" ", " ", ": ", " - "])
        return markers, rng.choice(
            ['cannot read "/ {}"', 'open "/ {}" now', 'ls "/ {}" failed']
        ).format(name + "/report.md")
    if family == 7:  # a Windows path before JSON
        name = _name(rng, markers[:-1] or markers, [" ", "-", "_"])
        tail = markers[-1]
        body = rng.choice([
            f'{{"relative":"{tail}\\\\notes.md","status":"failed"}}', f'["{tail}\\\\notes.md","b"]',
            f'{{"a":{{"b":"{tail}"}},"n":1}}',
        ])
        drive = rng.choice(["C:\\Clients\\", "\\\\host\\share\\", "\\\\?\\UNC\\host\\share\\"])
        return markers, f"open {drive}{name} X {body}"
    # family 8: a path after a quote or an operator, with a colour sequence and a variable together
    name = _name(rng, markers, GLUE)
    return markers, f'sh -c "cd {ESC}[32m$HOME/{name}/ok.md{ESC}[0m && ls"'


def _sinks(r, text):
    sinks = {
        "plain": r(text),
        "title": r.title(text),
        "json": json.loads(json.dumps(report.redact_value({"e": text}, r)))["e"],
        "exception": r.exception_text(RuntimeError(text)),
        "leftover": r.scrub_leftover_paths(text),
    }
    if r.level == "local":
        sinks["local_line"] = r.scrub_local_line(r(text))
    return sinks


@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("seed", range(450))
def test_fc8_fuzz_no_marker_survives_the_review_spellings(level, seed):
    markers, text = _case(seed)
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    r.fresh_names(detailed=(level == "local"))
    for sink, out in _sinks(r, text).items():
        for marker in markers:
            assert marker not in out, (seed, level, sink, text, out)


@pytest.mark.parametrize("seed", range(0, 450, 3))
def test_fc8_fuzz_raw_is_unchanged(seed):
    _markers_, text = _case(seed)
    r = Redactor(enabled=False)
    for sink, out in _sinks(r, text).items():
        assert out == text, (seed, sink)
