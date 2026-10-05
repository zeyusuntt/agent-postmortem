"""Fix round 2 of field milestone F: R2-1 and R2-2 (docs/DESIGN.md 5.1, FA1;
.reviews/v0.3/F-dispositions.md, Codex round 2).

R2-1: a quoted span is scanned by its opening delimiter. Its body excludes only that matching
delimiter and a newline, so a double-quoted path may hold an apostrophe and a single-quoted
path may hold a double quote, and the whole span is one token.

R2-2: there is no display-form exemption at share. A token already shaped `~/.../<name>` that
encloses a registered value is its own path-N, like any other token whose structural form keeps
a registered value.

Every fixture is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_postmortem import report
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor

HOME = Path("/home/user")


def _redactor(level, *registered):
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    names = r.fresh_names(detailed=(level == "local"))
    for value in registered:
        names.path(value)
    return r


def _all_sinks(r, text):
    """Plain text, title, JSON string value, evidence line, rescue-style evidence line, and
    exception text: every sink delegates to the one redactor."""
    return {
        "plain": r(text),
        "title": r.title(text),
        "json": json.loads(json.dumps(report.redact_value({"evidence": text}, r)))["evidence"],
        "evidence": report._shown_evidence([text], None, r)[0],
        "rescue": r(f"rescue: {text}")[len("rescue: "):],
        "exception": r.exception_text(RuntimeError(text)),
    }


# --- R2-1: the opposite quote character inside a quoted path --------------------------------

R2_1_ROWS = [
    pytest.param(
        "/srv/data/O'Neil",
        'cat "/srv/data/O\'Neil Client/deep/notes.md" now',
        'cat "path-2 [text hidden]" now',  # F fix 5 (W5-1): whitespace carries the marker
        id="apostrophe_inside_a_double_quoted_path",
    ),
    pytest.param(
        '/srv/data/A"B',
        "cat '/srv/data/A\"B Client/deep/notes.md' now",
        "cat 'path-2 [text hidden]' now",  # F fix 5 (W5-1): whitespace carries the marker
        id="double_quote_inside_a_single_quoted_path",
    ),
]


@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("registered,text,expected", R2_1_ROWS)
def test_fc2_an_opposite_quote_inside_a_quoted_path_leaves_no_raw_tail(
    level, registered, text, expected
):
    r = _redactor(level, registered)
    for sink, out in _all_sinks(r, text).items():
        assert "Client" not in out and "notes.md" not in out and "deep" not in out, (sink, out)
        assert out == expected, (sink, out)


def test_fc2_a_quoted_span_still_ends_at_its_own_delimiter():
    r = _redactor("share", "/srv/data/proj")
    out = r("cp '/srv/data/proj/a b' \"/srv/other/c d\"")
    # F fix 4 (W4): the second quoted path, scanned by its own delimiter, is scrubbed too;
    # F fix 5 (W5-1): both spans hold a space, so both carry the marker
    # F fix 10 (W10-3, rule b): the quote after `b` is followed by a space and a token with a
    # slash, so it may be a possessive of a cut-off path: the fragment runs to the end of the
    # line and the second path is inside the same span (was: two spans, path-2 and path-3)
    assert out == "cp 'path-2 [text hidden]", out


# --- R2-2: no display-form exemption at share -------------------------------------------------

R2_2_ROWS = [
    pytest.param("SECRET.md", "~/.../SECRET.md", "path-2", id="registered_basename_in_display_form"),
    pytest.param("CLAUDE.md", "--- ~/.../CLAUDE.md", "--- path-2", id="registered_claude_md_label"),
]


@pytest.mark.parametrize("registered,text,expected", R2_2_ROWS)
def test_fc2_share_a_display_form_enclosing_a_registered_value_is_its_own_path_n(
    registered, text, expected
):
    r = _redactor("share", registered)
    for sink, out in _all_sinks(r, text).items():
        assert registered not in out, (sink, out)
        assert out == expected, (sink, out)


def test_fc2_share_an_unregistered_display_form_stays_idempotent():
    r = _redactor("share", "/srv/other")
    for sink, out in _all_sinks(r, "see ~/.../notes.md ok").items():
        assert out == "see ~/.../notes.md ok", (sink, out)


def test_fc2_local_a_display_form_with_a_registered_basename_keeps_the_local_display():
    r = _redactor("local", "CLAUDE.md")
    assert r("--- ~/.../CLAUDE.md") == "--- ~/.../CLAUDE.md"
