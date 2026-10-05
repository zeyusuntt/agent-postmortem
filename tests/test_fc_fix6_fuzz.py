"""Fix round 6 (W6-2) of field milestone F: a seeded generator of UNQUOTED private directory names.

A directory or file name of a real project holds words, spaces, parentheses, `&`, a semicolon,
brackets, an apostrophe and a dot. This generator writes such a name the way a transcript prints it,
with no quoting and no escaping at all (the shape the review rounds 4 and 5 broke), puts it in a
sentence, and checks at share and local, unregistered and registered, that no marker survives any
sink. A name never holds an operator followed by a space (`; `, `| `, `& `, `) `): by DESIGN 5.1 that
is where a span stops, so what follows it is not part of the name. A failing seed prints the text.

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
GLUE = [" ", " ", " ", "&", "-", ".", "_", "'", ";", "(", "[", "{", "+", ",", "=", "%", "@"]


def _word(rng, marker):
    shape = rng.choice(["plain", "paren", "bracket", "brace", "year", "initial"])
    if shape == "paren":
        return f"({marker})"
    if shape == "bracket":
        return f"{marker}[{rng.randint(1, 20)}]"
    if shape == "brace":
        return f"{marker}{{}}"
    if shape == "year":
        return f"{marker}.{rng.randint(1999, 2026)}"
    if shape == "initial":
        return f"{marker}&D"
    return marker


def _name(rng, markers):
    parts = []
    for index, marker in enumerate(markers):
        parts.append(_word(rng, marker))
        if index < len(markers) - 1:
            glue = rng.choice(GLUE)
            if glue in ("(", "[", "{"):
                glue = " "
            parts.append(glue)
    text = "".join(parts)
    # never an operator followed by a space (a recorded stop), never a trailing or leading space
    for operator in (";", "&", ")", "|", "<", ">"):
        text = text.replace(operator + " ", operator + "x")
    return text.strip(" ") or markers[0]


def _case(seed):
    rng = random.Random(seed)
    segments = []
    markers = []
    groups = []
    for _ in range(rng.randint(1, 4)):
        found = [f"Zq{seed}x{len(markers) + n}z" for n in range(rng.randint(1, 4))]
        markers.extend(found)
        groups.append(found)
        segments.append(_name(rng, found))
    body = rng.choice(["/srv/data/", "/srv/", "/opt/work/", "~/", "/home/user/", "/tmp/aa/"]) + "/".join(
        segments
    )
    if rng.random() < 0.3:
        body += "/notes.md"
    else:
        # the basename of a path with no hidden text is shown by design (`~/.../name`, a registered
        # display), so only the directories before it must never survive
        markers = [marker for group in groups[:-1] for marker in group]
    sentence = rng.choice([
        "cat {} now", "ran {}", "{} failed", "cannot open {} while the run was going",
        "see {} and /srv/other/x", "wrote {}\nnext line stays", "error: {}: permission denied",
        "pytest {}:12: AssertionError", "{}",
    ]).format(body)
    registered = rng.choice([None, None, body, "/srv/data", segments[0]])
    return markers, sentence, registered


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
@pytest.mark.parametrize("seed", range(300))
def test_fc6_fuzz_no_marker_survives_an_unquoted_name(level, seed):
    markers, text, registered = _case(seed)
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    registry = r.fresh_names(detailed=(level == "local"))
    if registered is not None:
        registry.path(registered)
    for sink, out in _sinks(r, text).items():
        for marker in markers:
            assert marker not in out, (seed, level, sink, registered, text, out)
