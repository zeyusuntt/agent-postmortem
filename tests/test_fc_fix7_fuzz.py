"""Fix round 7 (W7-1, W7-3) of field milestone F: a seeded generator of private names that hold what
POSIX and Windows names hold, including every early stop the round-6 rule trusted.

A real directory or file name holds words, spaces, parentheses, `&`, `;`, `|`, `<`, `>`, a colon and
a number, a typographic closer, an apostrophe, a bracket or a quote. This generator writes such a
name the way a transcript prints it, with no quoting and no escaping at all, puts it in a sentence,
and checks at share and local, unregistered and registered, that no marker survives any sink. Unlike
`test_fc_fix6_fuzz.py` it does NOT avoid an operator followed by a space: since F fix 7 nothing in a
name ends a span early. A failing seed prints the text.

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
GLUE = [
    " ", " ", " ", "; ", " | ", " & ", ") ", " (", " < ", " > ", "&", ";", "|", ":", ": ",
    ":12 ", ":12: ", " 10:30 ", "\u2019 ", "\u201d ", "\u00bb ", "\u300d ", "\u203a ", "'", "-",
    ".", "_", "[", "]", "{", "}", "+", ",", "=", "%", "@", "(", ")", "\u2019", "\u201d", "!", "?",
    " - ", " -> ", " && ", " || ", " ;; ",
]
SENTENCES = [
    "cat {} now", "ran {}", "{} failed", "cannot open {} while the run was going",
    "see {} and /srv/other/x", "wrote {}\nnext line stays", "error: {}: permission denied",
    "pytest {}:12: AssertionError", "{}", "cd {} && make", "(cd {}) done", "open {} | wc -l",
    "[{}] is missing", "missing: {} (exit 1)",
]


def _name(rng, markers):
    parts = []
    for index, marker in enumerate(markers):
        parts.append(marker)
        if index < len(markers) - 1:
            parts.append(rng.choice(GLUE))
    return "".join(parts).strip(" ") or markers[0]


def _unix_case(seed):
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
    return markers, rng.choice(SENTENCES).format(body), rng.choice(
        [None, None, body, "/srv/data", segments[0]]
    )


def _windows_case(seed):
    rng = random.Random(10_000 + seed)
    markers = [f"Zq{seed}w{n}z" for n in range(rng.randint(2, 5))]
    name = _name(rng, markers)
    kind = rng.choice(["drive", "drive_slash", "unc"])
    if kind == "drive":
        body = "C:\\Docs\\" + name.replace("/", "-") + "\\notes.md"
    elif kind == "drive_slash":
        body = "D:/Docs/" + name + "/notes.md"
    else:
        body = "\\\\fileserver\\share\\" + name.replace("/", "-") + "\\notes.md"
    return markers, rng.choice(SENTENCES).format(body), rng.choice([None, None, body])


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


def _check(seed, level, markers, text, registered):
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    registry = r.fresh_names(detailed=(level == "local"))
    if registered is not None:
        registry.path(registered)
    for sink, out in _sinks(r, text).items():
        for marker in markers:
            assert marker not in out, (seed, level, sink, registered, text, out)


@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("seed", range(400))
def test_fc7_fuzz_no_marker_survives_a_name_with_any_early_stop_inside_it(level, seed):
    markers, text, registered = _unix_case(seed)
    _check(seed, level, markers, text, registered)


@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("seed", range(250))
def test_fc7_fuzz_no_marker_survives_a_windows_path(level, seed):
    markers, text, registered = _windows_case(seed)
    _check(seed, level, markers, text, registered)
