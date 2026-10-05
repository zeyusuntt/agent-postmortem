"""Fix round 4 (W5) of field milestone F: a seeded generator of shell spellings of one private path.

Each case builds a path whose directory names hold a marker (`Zq<digits>`) and special characters
(a space, quotes, a colon, `;`, `&`, parentheses, an accented letter in either normal form), writes it
the way a shell would accept it (every segment backslash-escaped, single-quoted, double-quoted,
a POSIX apostrophe splice, or wrapped in typographic quotes), puts it in a sentence, and checks at
share and local, unregistered and registered (the whole path, a parent, a relative suffix): no
marker survives any sink. A failing seed prints the text.

Every fixture is synthetic.
"""

from __future__ import annotations

import json
import random
import unicodedata
from pathlib import Path

import pytest

from agent_postmortem import report
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor

HOME = Path("/home/user")
SPECIALS = [" ", "'", '"', ":", ";", "&", "(", ")", "<", "|", "\N{LATIN SMALL LETTER E WITH ACUTE}",
            "\N{COMBINING ACUTE ACCENT}", "\N{RIGHT SINGLE QUOTATION MARK}", "$", "`", "*", "#"]


def _segment(rng, marker):
    chars = list(marker)
    for _ in range(rng.randint(0, 3)):
        chars.insert(rng.randint(0, len(chars)), rng.choice(SPECIALS))
    text = "".join(chars)
    # a segment is never empty, never a lone dot, never starts with a combining mark alone
    return text.strip(" ") or marker


def _encode(rng, segment):
    style = rng.choice(["escape", "single", "double", "splice", "typographic"])
    if style == "escape":
        return "".join("\\" + ch if not (ch.isalnum() or ch in "-_.") else ch for ch in segment)
    if style == "single":
        if "'" in segment:
            style = "splice"
        else:
            return f"'{segment}'"
    if style == "splice":
        return "'" + segment.replace("'", "'\"'\"'") + "'"
    if style == "double":
        return '"' + "".join("\\" + ch if ch in '"\\$`' else ch for ch in segment) + '"'
    return "\N{LEFT DOUBLE QUOTATION MARK}" + segment.replace(
        "\N{RIGHT DOUBLE QUOTATION MARK}", "x") + "\N{RIGHT DOUBLE QUOTATION MARK}"


def _case(seed):
    rng = random.Random(seed)
    markers = [f"Zq{seed}x{n}" for n in range(rng.randint(2, 4))]
    names = [_segment(rng, marker) for marker in markers]
    encoded = [_encode(rng, name) for name in names]
    separator = "/"
    body = "/srv/data" + separator + separator.join(encoded)
    if rng.random() < 0.3:
        body += "/notes.md"
    if rng.random() < 0.3:
        body += rng.choice([":12", ":12:3", ".", ",", "."])
    wrapper = rng.choice(["{}", "--out={}", "({})", "[{}]", "NAME={}", "`{}`", "-C{}"])
    text = rng.choice(["cat {} now", "ran {}", "wrote {}; echo ok", "{} failed", "see {} and {}"]).format(
        wrapper.format(body), wrapper.format(body))
    registered = rng.choice([None, "/srv/data", "/srv/data/" + names[0], names[-1], body])
    return markers, text, registered, names


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
def test_fc4_fuzz_no_marker_survives_any_shell_spelling(level, seed):
    markers, text, registered, names = _case(seed)
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    registry = r.fresh_names(detailed=(level == "local"))
    if registered is not None:
        registry.path(unicodedata.normalize("NFC", registered))
    for sink, out in _sinks(r, text).items():
        for marker in markers:
            assert marker not in out, (seed, level, sink, registered, text, out)
