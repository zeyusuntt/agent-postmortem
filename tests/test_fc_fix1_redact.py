"""Fix round 1 of field milestone F: S1 and S-m1 (docs/DESIGN.md 5.1, FA1;
.reviews/v0.3/F-dispositions.md).

S1 (privacy blocker): at share, a token under /tmp, /private/tmp or home that encloses a
registered value got its structural form, and when that form left the registered value in
place it printed raw. The criterion now: after the structural form is computed for a token that
encloses registered values, if any of them (or the part a collapse keeps) still occurs verbatim
in the shown form, the whole token becomes its own path-N; otherwise the structural form stands.

S-m1: a quoted or backslash-escaped continuation with a space keeps its tail raw. The enclosing
token is the whole quoted span; a backslash-escaped space is a token character.

Every fixture is synthetic.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor

HOME = Path("/home/user")
PATH_N = re.compile(r"path-\d+")


def _redactor(level, *registered):
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    names = r.fresh_names(detailed=(level == "local"))
    for value in registered:
        names.path(value)
    return r


def _sinks(r, text):
    """The two text sinks: the plain Redactor call and Redactor.title."""
    return [r(text), r.title(text)]


# --- S1: the reviewer's exact rows --------------------------------------------------------------

S1_ROWS = [
    # F fix 6 (W6-2): a path hides the rest of its line, so the trailing word is `[text hidden]`
    pytest.param("acme-client/notes.md", "see /tmp/acme-client/notes.md ok",
                 "see path-2 [text hidden]", id="relative_value_inside_a_short_tmp_token"),
    pytest.param("/tmp/claude-501", "cd /tmp/claude-501/SECRETzz now", "cd path-2 [text hidden]",
                 id="tmp_dir_with_a_continuation"),
    pytest.param("/tmp/claude-501", "cd /tmp/claude-501SECRETzz now", "cd path-2 [text hidden]",
                 id="tmp_dir_with_a_glued_continuation"),
    pytest.param("/tmp/out.md", "see /tmp/out.md.bak ok", "see path-2 [text hidden]",
                 id="tmp_file_with_a_suffix"),
    pytest.param("out.md", "see /tmp/out.md", "see path-2", id="relative_value_under_tmp"),
    pytest.param("/tmp/claude-501",
                 "/private/tmp/claude-501/aa/bb/scratchpad/cards/x.md",
                 "path-2", id="collapsed_private_tmp_form_still_holds_the_value"),
    pytest.param("acme", "see /home/user/acme ok", "see path-2 [text hidden]",
                 id="single_home_segment_absolute"),
    pytest.param("acme", "see ~/acme ok", "see path-2 [text hidden]",
                 id="single_home_segment_tilde"),
    pytest.param("/home/user/proj", "at /home/user/proj.bak now", "at path-2 [text hidden]",
                 id="home_value_with_a_suffix_the_tilde_form_would_keep"),
]


@pytest.mark.parametrize("registered,text,expected", S1_ROWS)
def test_fc1_share_a_token_enclosing_a_registered_value_is_its_own_path_n(
    registered, text, expected
):
    r = _redactor("share", registered)
    for out in _sinks(r, text):
        assert out == expected, out


@pytest.mark.parametrize("registered,text,expected", S1_ROWS)
def test_fc1_share_no_registered_value_survives_verbatim_in_the_shown_form(
    registered, text, expected
):
    """The criterion itself, row by row, independent of the numbering: no output keeps the
    registered value, its last two segments, or its tilde spelling."""
    r = _redactor("share", registered)
    segments = [part for part in registered.split("/") if part]
    needles = {registered, "/".join(segments[-2:])}
    if registered.startswith(str(HOME) + "/"):
        needles.add("~" + registered[len(str(HOME)):])
    for out in _sinks(r, text):
        if PATH_N.search(out):
            continue
        for needle in needles:
            assert needle not in out, (needle, out)


@pytest.mark.parametrize(
    "registered,text,expected",
    # A home path is its real `~/.../name` display at local, so the home rows are not part
    # of this check (they are covered at share above).
    [
        row for row in S1_ROWS
        if "/home/user" not in row.values[1] and "~/" not in row.values[1]
    ],
)
def test_fc1_local_the_same_rows_show_no_raw_tail(registered, text, expected):
    """At local the P19 path function decides; no tmp row may keep the tail the reviewer's
    inputs carry (SECRETzz, .bak)."""
    r = _redactor("local", registered)
    for out in _sinks(r, text):
        assert "SECRETzz" not in out and ".bak" not in out, out


def test_fc1_local_the_three_reviewer_rows_are_path_n():
    for registered, text, expected in (
        ("acme-client/notes.md", "see /tmp/acme-client/notes.md ok", "see path-2 [text hidden]"),
        ("/tmp/claude-501", "cd /tmp/claude-501/SECRETzz now", "cd path-2 [text hidden]"),
        ("/tmp/out.md", "see /tmp/out.md.bak ok", "see path-2 [text hidden]"),
    ):
        r = _redactor("local", registered)
        for out in _sinks(r, text):
            assert out == expected, out


# --- S1: what must stay as it was ---------------------------------------------------------------


def test_fc1_a_token_under_home_whose_structural_form_hides_the_value_keeps_its_form():
    r = _redactor("share", "/home/user/proj")
    # F fix 6 (W6-2): the path ends the line (a trailing word would be hidden with it)
    for out in _sinks(r, "at /home/user/proj/deep/er/file.md"):
        assert out == "at ~/.../file.md", out


def test_fc1_a_rendered_home_display_form_is_idempotent():
    # R2-2: only an unregistered display form is idempotent; a registered basename inside it
    # makes the whole token its own path-N (the old expectation pinned the leak)
    r = _redactor("share", "/srv/other")
    for out in _sinks(r, "--- ~/.../CLAUDE.md"):
        assert out == "--- ~/.../CLAUDE.md", out
    r = _redactor("share", "CLAUDE.md")
    for out in _sinks(r, "--- ~/.../CLAUDE.md"):
        assert out == "--- path-2", out


def test_fc1_the_registered_value_alone_is_still_its_pseudonym():
    r = _redactor("share", "/tmp/out.md")
    for out in _sinks(r, "see /tmp/out.md ok"):
        # F fix 6 (W6-2): the span keeps the registered pseudonym and hides the rest of the line
        assert out == "see path-1 [text hidden]", out
    for out in _sinks(r, "see /tmp/out.md"):
        assert out == "see path-1", out


def test_fc1_a_tmp_token_that_encloses_no_registered_value_keeps_the_collapse():
    r = _redactor("share", "/srv/other")
    # F fix 6 (W6-2): the path ends the line (a trailing word would be hidden with it)
    out = r("wrote /private/tmp/claude-501/aa/bb/scratchpad/cards/x.md")
    assert out == "wrote /private/tmp/claude-501/.../x.md"


def test_fc1_a_tmp_token_whose_collapse_hides_the_registered_value_keeps_the_collapse():
    """The registered value sits in a segment the collapse drops: nothing registered survives,
    so the structural form stands."""
    r = _redactor("share", "scratchpad/cards")
    # F fix 6 (W6-2): the path ends the line (a trailing word would be hidden with it)
    out = r("wrote /private/tmp/claude-501/aa/bb/scratchpad/cards/x.md")
    assert "scratchpad" not in out and "cards" not in out
    assert out == "wrote /private/tmp/claude-501/.../x.md"


def test_fc1_a_second_token_with_the_same_text_gets_the_same_path_n():
    r = _redactor("share", "/tmp/claude-501")
    # F fix 6 (W6-2): one path per line (the rest of a line belongs to the path that starts in it)
    out = r("a /tmp/claude-501/SECRETzz\nb /tmp/claude-501/SECRETzz\nc /tmp/claude-501\nd")
    assert out == "a path-2\nb path-2\nc path-1\nd"


def test_fc1_raw_is_untouched():
    r = Redactor(context=DisclosureContext(level="raw", home=HOME))
    r.fresh_names().path("/tmp/claude-501")
    assert r("cd /tmp/claude-501/SECRETzz now") == "cd /tmp/claude-501/SECRETzz now"


# --- S-m1: quoted and backslash-escaped continuations with a space --------------------------------

S_M1_ROWS = [
    # F fix 5 (W5-1): whitespace in the decoded span carries the marker
    pytest.param('cat "/srv/data/proj/Acme Client/deep/notes.md" now',
                 'cat "path-2 [text hidden]" now', id="double_quotes"),
    pytest.param("cat '/srv/data/proj/Acme Client/deep/notes.md' now",
                 "cat 'path-2 [text hidden]' now", id="single_quotes"),
    pytest.param("cat /srv/data/proj/Acme\\ Client/deep/notes.md now",
                 "cat path-2 [text hidden]", id="escaped_space"),  # F fix 6 (W6-2)
]


@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("text,expected", S_M1_ROWS)
def test_fc1_a_continuation_with_a_space_leaves_no_raw_tail(level, text, expected):
    r = _redactor(level, "/srv/data/proj")
    for out in _sinks(r, text):
        assert "Client" not in out and "notes.md" not in out and "deep" not in out, out
        assert out == expected, out


def test_fc1_a_quoted_path_holding_the_registered_value_mid_string_is_one_token():
    r = _redactor("share", "proj")
    out = r('cat "/srv/data/proj/Acme Client/notes.md" now')
    # F fix 5 (W5-1): whitespace in the decoded span carries the marker
    assert out == 'cat "path-2 [text hidden]" now', out


def test_fc1_prose_in_quotes_that_is_not_a_path_keeps_its_words():
    r = _redactor("share", "/srv/data/proj")
    out = r('echo "building /srv/data/proj now" done')
    # F fix 4 (W3): the path is the whole quoted word and the reader is told text was hidden
    assert out == 'echo "path-2 [text hidden]" done', out


def test_fc1_a_quoted_exact_value_keeps_its_quotes():
    r = _redactor("share", "/srv/data/proj")
    assert r("a '/srv/data/proj' b") == "a 'path-1' b"
    assert r('a "/srv/data/proj" b') == 'a "path-1" b'


def test_fc1_two_quoted_paths_on_one_line_are_one_fragment_by_the_last_quote_rule():
    r = _redactor("share", "/srv/data/proj")
    out = r("cp '/srv/data/proj/a b' '/srv/other/c d'")
    # F fix 4 (W4): the second, unregistered quoted path is scrubbed too; F fix 5 (W5-1): the span
    # holds a space, so it carries the marker. F fix 9 (W9-3): a quoted path closes at the LAST
    # quote of its line (a possessive apostrophe before a space looks like a closing quote), so
    # the two quoted paths are one span
    assert out == "cp 'path-2 [text hidden]'", out


def test_fc1_an_apostrophe_in_prose_does_not_break_a_later_quoted_path():
    r = _redactor("share", "/srv/data/proj")
    out = r("don't cat '/srv/data/proj/Acme Client/notes.md' now")
    assert "Client" not in out and "notes.md" not in out, out


def test_fc1_a_trailing_backslash_space_without_a_token_character_is_not_swallowed():
    r = _redactor("share", "/srv/data/proj")
    out = r("cd /srv/data/proj\\ (x)")
    # F fix 6 (W6-2): the escaped space and `(x` are inside the path's line; `)` ends it
    assert out == "cd path-2 [text hidden])", out


def test_fc1_a_quoted_home_relative_path_with_a_space_is_one_token():
    r = _redactor("share", "acme")
    out = r('cat "~/acme dir/notes.md" now')
    # the structural form of the whole quoted token hides the registered value; only the
    # basename the 5.1 collapse keeps remains
    assert "dir" not in out and "acme" not in out, out
    # F fix 5 (W5-1): a span with a space is path-N and the marker, never the structural form
    assert out == 'cat "path-2 [text hidden]" now', out


def test_fc1_the_registered_root_never_makes_a_token_its_own_path_n():
    """The root `/` is registered but is a substring of every token; it must not trigger the
    S1 fallback for a token that encloses nothing else."""
    r = _redactor("share", "/", "/srv/other")
    # F fix 6 (W6-2): the path ends the line (a trailing word would be hidden with it)
    out = r("wrote /private/tmp/claude-501/aa/bb/srv/other/y/x.md")
    assert out == "wrote /private/tmp/claude-501/.../x.md", out


def test_fc1_a_value_after_a_quoted_span_does_not_swallow_the_text_between():
    r = _redactor("share", "/srv/data/proj")
    out = r("cp '/srv/other/a b' then /srv/data/proj/z end")
    # F fix 4 (W4): the unregistered quoted span is scrubbed too; F fix 5 (W5-1): its span holds a
    # space, so it carries the marker
    # F fix 6 (W6-2): the unquoted path hides the rest of its line
    assert out == "cp 'path-2 [text hidden]' then path-3 [text hidden]", out


def test_fc1_an_escaped_space_before_the_registered_value_extends_the_token_to_the_left():
    r = _redactor("share", "data/proj")
    out = r("cat /srv/Acme\\ Client/data/proj/x now")
    # F fix 5 (W5-1): the escaped space is whitespace in the decoded span
    assert out == "cat path-2 [text hidden]", out  # F fix 6 (W6-2)
