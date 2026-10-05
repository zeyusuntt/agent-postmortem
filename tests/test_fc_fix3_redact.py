"""Fix round 3 of field milestone F: M3-1 (docs/DESIGN.md 5.1, FA1; .reviews/v0.3/F-dispositions.md,
"Main-session finding before Codex round 3").

(a) The quoted-path scrub (`_QUOTED_PATH_RE`) is delimiter-aware: the body excludes only the
matching delimiter and a newline, so a double-quoted path may hold an apostrophe and a
single-quoted path may hold a double quote, and the whole span is replaced.

(b) An apostrophe with a word character on both sides is a path-token character in the one shared
alphabet: `_ABS_PATH_RE` and the free-text scanner (F fix 4: it replaced `path_token_span`) agree.
An apostrophe at a segment edge or next to a space is not part of the token; it opens a quote.

Every fixture is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_postmortem import report
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import _ABS_PATH_RE, Pseudonyms, Redactor, scan_paths

HOME = Path("/home/user")


def _redactor(level, *registered):
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    names = r.fresh_names(detailed=(level == "local"))
    for value in registered:
        names.path(value)
    return r


def _scrubbed(level, text):
    """The unregistered path scrub: local through the detailed run sinks (scrub_local_line),
    share through scrub_leftover_paths."""
    r = _redactor(level)
    if level == "local":
        return r.scrub_local_line(r(text))
    return r.scrub_leftover_paths(text)


def _all_sinks(r, text):
    return {
        "plain": r(text),
        "title": r.title(text),
        "json": json.loads(json.dumps(report.redact_value({"evidence": text}, r)))["evidence"],
        "evidence": report._shown_evidence([text], None, r)[0],
        "exception": r.exception_text(RuntimeError(text)),
    }


# --- (a) and (b), unregistered: the path scrub --------------------------------------------------

M3_1_ROWS = [
    pytest.param(
        'cat "/srv/data/O\'Neil Client/deep/notes.md" now',
        'cat "path-1 [text hidden]" now',  # F fix 5 (W5-1): whitespace carries the marker
        id="apostrophe_inside_a_double_quoted_path",
    ),
    pytest.param(
        "cat '/srv/data/A\"B Client/deep/notes.md' now",
        "cat 'path-1 [text hidden]' now",  # F fix 5 (W5-1): whitespace carries the marker
        id="double_quote_inside_a_single_quoted_path",
    ),
    pytest.param(
        "cat /srv/data/O'Neil/notes.md now",
        "cat path-1 [text hidden]",  # F fix 6 (W6-2): a path hides the rest of its line
        id="unquoted_directory_with_an_in_word_apostrophe",
    ),
    pytest.param(
        "cat /srv/data/O'Neil's/notes.md now",
        "cat path-1 [text hidden]",  # F fix 6 (W6-2)
        id="two_in_word_apostrophes_in_one_token",
    ),
    pytest.param(
        "cat '/srv/x/y.md' now",
        "cat 'path-1' now",
        id="a_closing_quote_right_after_a_path_is_not_swallowed",
    ),
    pytest.param(
        "it's at /srv/x/y.md ok",
        "it's at path-1 [text hidden]",  # F fix 6 (W6-2): the apostrophe before the path is prose
        id="its_in_prose_next_to_a_path_is_unchanged",
    ),
    pytest.param(
        "say it's fine, don't worry",
        "say it's fine, don't worry",
        id="prose_with_apostrophes_and_no_path_is_unchanged",
    ),
    pytest.param(
        "cat /srv/x/y.md 'quoted' tail",
        "cat path-1 [text hidden]",  # F fix 6 (W6-2): the quoted word is inside the path's line
        id="an_apostrophe_next_to_a_space_is_not_part_of_the_token",
    ),
    pytest.param(
        "cat /srv/x/y' now",
        "cat path-1 [text hidden]",
        id="an_apostrophe_at_a_segment_edge_is_not_part_of_the_token",
    ),
    pytest.param(
        "cat \"/'Neil Client/notes.md\" now",
        'cat "path-1 [text hidden]" now',  # F fix 5 (W5-1): whitespace carries the marker
        id="a_quoted_path_whose_first_character_is_the_other_quote",
    ),
    pytest.param(
        "cat /srv/x/'y now",
        "cat path-1 [text hidden]",
        id="an_apostrophe_right_after_a_slash_is_not_part_of_the_token",
    ),
    pytest.param(
        "echo '/' and '/srv/x/y.md' now",
        "echo 'path-1' and 'path-2' now",
        id="an_empty_looking_quoted_root_does_not_open_a_span_over_the_next_quote",
    ),
    pytest.param(
        "cp '/srv/x/y.md' to 'keep this' now",
        "cp 'path-1 [text hidden]' now",  # F fix 9 (W9-3): closes at the LAST quote of the line
        id="a_quoted_path_closes_at_the_last_quote_of_its_line",
    ),
    pytest.param(
        "echo '/srv/x/y\nnext line' tail",
        "echo 'path-1\nnext line' tail",
        id="a_quoted_path_never_crosses_a_newline",
    ),
    pytest.param(
        "cat '/srv/x/y.md' and \"/srv/z/w v.md\" now",
        "cat 'path-1' and \"path-2 [text hidden]\" now",  # F fix 5 (W5-1): the second span has a space
        id="r2_1_two_quoted_paths_each_scanned_by_its_own_delimiter",
    ),
]


@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("text,expected", M3_1_ROWS)
def test_fc3_the_path_scrub_leaves_no_raw_tail(level, text, expected):
    out = _scrubbed(level, text)
    assert "Neil" not in out and "Client" not in out and "notes.md" not in out, out
    assert out == expected, out


# --- (a) and (b), registered: the apply_paths token scan ---------------------------------------

REGISTERED_ROWS = [
    pytest.param(
        "/srv/data/O'Neil",
        "cat /srv/data/O'Neil/notes.md now",
        "cat path-2 [text hidden]",  # F fix 6 (W6-2)
        id="registered_directory_with_an_in_word_apostrophe",
    ),
    pytest.param(
        "/srv/data/O",
        "cat /srv/data/O'Neil/notes.md now",
        "cat path-2 [text hidden]",  # F fix 6 (W6-2)
        id="registered_prefix_stops_before_an_in_word_apostrophe",
    ),
]


@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("registered,text,expected", REGISTERED_ROWS)
def test_fc3_a_registered_value_widens_over_an_in_word_apostrophe(level, registered, text, expected):
    r = _redactor(level, registered)
    for sink, out in _all_sinks(r, text).items():
        assert "Neil" not in out and "notes.md" not in out, (sink, out)
        assert out == expected, (sink, out)


# --- the one alphabet: _ABS_PATH_RE and the free-text scanner agree ---------------------------
# F fix 4: `path_token_span` is removed; the same rows now go through `scan_paths`, which finds a
# path inside a complete shell word. A row whose apostrophe is an opener (not in-word) changes on
# purpose: the unclosed opener takes the rest of its line into the word and fails closed.


def _scanned(text, *registered):
    names = Pseudonyms()
    for value in registered:
        names.path(value)
    return scan_paths(text, names, lambda token, hit: "<P>")


AGREE_TEXTS = [
    pytest.param("/srv/data/O'Neil/notes.md", "<P>", id="in_word_apostrophe"),
    # F fix 6 (W6-2): the path ends the line (a trailing word would be hidden with it)
    pytest.param("cat /srv/data/O'Neil's/notes.md", "cat <P>", id="two_in_word_apostrophes"),
    pytest.param("it's /srv/a/b", "it's <P>", id="its_before_a_path"),
]


@pytest.mark.parametrize("text,expected", AGREE_TEXTS)
def test_fc3_abs_path_re_and_the_scanner_agree(text, expected):
    match = _ABS_PATH_RE.search(text)
    assert match is not None
    assert _scanned(text) == text[:match.start()] + "<P>" + text[match.end():] == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param("/srv/a/b' now", "<P> [text hidden]", id="opener_after_the_path_fails_closed"),
        pytest.param("/srv/a/b'", "<P>", id="opener_at_the_end_goes_with_the_word"),
    ],
)
def test_fc3_a_trailing_apostrophe_is_an_opener_not_token_text(text, expected):
    assert _scanned(text) == expected


def test_fc3_the_scanner_takes_an_in_word_apostrophe_and_an_opener_closes_the_word():
    text = "cat /srv/data/O'Neil/notes.md' now"
    # the in-word apostrophe is token text; the apostrophe before the space opens a quote that
    # never closes, so the rest of the line is part of the word (it is hidden and says so)
    assert _scanned(text) == "cat <P> [text hidden]"
    assert _scanned(text, "/srv/data/O") == "cat <P> [text hidden]"
    assert _scanned("'/srv/a/b") == "'<P>"


def test_fc3_scanner_apostrophe_needs_a_word_character_on_each_side():
    # an apostrophe before a non-word character, and one after a space, open a quote: the word
    # holds the registered value and the whole word is the path
    assert _scanned("x'/a/b", "/a/b") == "<P>"
    assert _scanned(" 'a/b", "a/b") == " 'path-1"  # exactly the registered value: its pseudonym
    # an apostrophe followed by a space or by the end opens a quote that takes the line
    assert _scanned("/a/b' c", "/a/b") == "<P> [text hidden]"
    assert _scanned("/a/b'", "/a/b") == "path-1"
    # a word character on both sides is token text
    assert _scanned("/a/b'c d", "/a/b") == "<P> [text hidden]"  # F fix 6 (W6-2)
    assert _scanned("a'b/c", "b/c") == "<P>"
