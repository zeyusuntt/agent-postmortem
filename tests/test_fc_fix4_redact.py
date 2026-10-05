"""Fix round 4 (W) of field milestone F: one free-text path scanner built on shell words
(docs/DESIGN.md 5.1; .reviews/v0.3/F-W-spec.md rev 3; the exact inputs of
.reviews/v0.3/F-codex-r3.md items 1 to 4 and of the spec reviews F-W-spec-codex*.md).

Three review rounds each found one more spelling of a path that a character-class or a
quoted-regex scanner splits, leaving a raw private tail. The unit is now a complete shell word:
once a word holds a path, the rest of the word is rendered as one path.

Every fixture is synthetic. Private words are the ones a stranger must never read: `Client`,
`Secret`, `Neil`, `deep`, `notes.md`.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from agent_postmortem import report
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor

HOME = Path("/home/user")
LEVELS = ["share", "local"]
PRIVATE = ("Client", "Secret", "Neil", "deep", "notes.md")


def _grid(rows, registered_dim=False):
    """Every row at both levels (and, when asked, with and without a registered value)."""
    cells = []
    for row in rows:
        for level in LEVELS:
            dims = [False, True] if registered_dim else [None]
            for registered in dims:
                tag = row.id if registered is None else f"{row.id}-{registered}"
                cells.append(pytest.param(
                    level, *([registered] if registered is not None else []), *row.values,
                    id=f"{tag}-{level}",
                ))
    return cells


def _redactor(level, *registered):
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    names = r.fresh_names(detailed=(level == "local"))
    for value in registered:
        names.path(value)
    return r


def _all_sinks(r, text):
    """Every free-text sink the one scanner serves: the plain call, title, a JSON string,
    an evidence line, an exception text, the leftover scrub and the local line scrub."""
    sinks = {
        "plain": r(text),
        "title": r.title(text),
        "json": json.loads(json.dumps(report.redact_value({"evidence": text}, r)))["evidence"],
        "evidence": report._shown_evidence([text], None, r)[0],
        "exception": r.exception_text(RuntimeError(text)),
        "leftover": r.scrub_leftover_paths(text),
    }
    if r.level == "local":
        sinks["local_line"] = r.scrub_local_line(r(text))
    return sinks


def _assert_clean(r, text, private):
    for sink, out in _all_sinks(r, text).items():
        for word in private:
            assert word not in out, (sink, word, out)


# --- F-codex-r3 item 1: an escaped matching delimiter does not end the quoted path ---------------

ITEM1 = [
    pytest.param("/srv/data/A", 'cat "/srv/data/A\\"B Client/deep/notes.md" now',
                 id="registered_escaped_double_quote"),
    pytest.param(None, 'cat "/srv/data/A\\"B Client/deep/notes.md" now',
                 id="unregistered_escaped_double_quote"),
    pytest.param(None, "cat /srv/data/O\\'Neil\\ Client/deep/notes.md now",
                 id="unregistered_backslash_escaped_apostrophe"),
    pytest.param("/srv/data/O", "cat /srv/data/O\\'Neil\\ Client/deep/notes.md now",
                 id="registered_backslash_escaped_apostrophe_and_space"),
    pytest.param(None, "cat '/srv/data/O'\"'\"'Neil Client/deep/notes.md' now",
                 id="unregistered_posix_apostrophe_splice"),
    pytest.param("/srv/data/O", "cat '/srv/data/O'\"'\"'Neil Client/deep/notes.md' now",
                 id="registered_posix_apostrophe_splice"),
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("registered,text", ITEM1)
def test_fc4_item1_an_escaped_delimiter_leaves_no_private_tail(level, registered, text):
    r = _redactor(level, *([registered] if registered else []))
    _assert_clean(r, text, ("Client", "deep", "notes.md"))


@pytest.mark.parametrize("level", LEVELS)
def test_fc4_item1_the_exact_goldens(level):
    r = _redactor(level, "/srv/data/A")
    for sink, out in _all_sinks(r, 'cat "/srv/data/A\\"B Client/deep/notes.md" now').items():
        if sink == "leftover":
            continue
        # F fix 5 (W5-1): the decoded span holds a space, so it carries the marker
        assert out == 'cat "path-2 [text hidden]" now', (sink, out)
    r = _redactor(level)
    assert r.scrub_leftover_paths('cat "/srv/data/A\\"B Client/deep/notes.md" now') == (
        'cat "path-1 [text hidden]" now'
    )


# --- F-codex-r3 item 2: a registered relative value at the start of a quoted path ----------------

ITEM2 = [
    pytest.param("Client", 'cat "Client Secret/deep/notes.md" now', id="double_quoted"),
    pytest.param("Client", "cat 'Client Secret/deep/notes.md' now", id="single_quoted"),
    pytest.param("Client", 'cat "Client Secret/O\'Neil/deep/notes.md" now',
                 id="embedded_opposite_quote"),
    pytest.param("Client", 'cat "Client Secret\\"x/deep/notes.md" now', id="escaped_matching_quote"),
    pytest.param("/srv/data", 'cat "/srv/data/Client Secret/deep/notes.md" now', id="absolute"),
    pytest.param("~/Client", 'cat "~/Client Secret/deep/notes.md" now', id="home_relative"),
    pytest.param("Client", 'cat " Client Secret/deep/notes.md" now', id="space_before_the_value"),
]


@pytest.mark.parametrize(
    "level,registered,text",
    _grid(ITEM2),
)
def test_fc4_item2_a_registered_value_widens_over_the_whole_quoted_word(level, registered, text):
    r = _redactor(level, registered)
    # a home-relative token keeps the basename its 5.1 collapse shows (`~/.../notes.md`)
    private = ("Secret", "deep") if "~/" in text else ("Secret", "deep", "notes.md")
    _assert_clean(r, text, private)


@pytest.mark.parametrize("level", LEVELS)
def test_fc4_item2_the_exact_goldens(level):
    r = _redactor(level, "Client")
    # F fix 5 (W5-1): the decoded span holds a space, so it carries the marker
    assert r('cat "Client Secret/deep/notes.md" now') == 'cat "path-2 [text hidden]" now'
    assert r("cat 'Client Secret/deep/notes.md' now") == "cat 'path-2 [text hidden]' now"


# --- F-codex-r3 item 3: unregistered escapes, quoted segments, typographic apostrophes -----------

ITEM3 = [
    # F fix 5 (W5-1): a decoded span that holds a space carries the marker
    # F fix 6 (W6-2): a path hides the rest of its line, so the trailing word is `[text hidden]`
    pytest.param("cat /srv/data/Client\\ Secret/deep/notes.md now", "cat path-1 [text hidden]",
                 id="escaped_space"),
    pytest.param("cat /srv/data/'Client Secret'/deep/notes.md now",
                 "cat path-1 [text hidden]", id="single_quoted_segment"),
    pytest.param('cat /srv/data/"Client Secret"/deep/notes.md now',
                 "cat path-1 [text hidden]", id="double_quoted_segment"),
    pytest.param("cat /srv/data/O\\'Neil/deep/notes.md now", "cat path-1 [text hidden]",
                 id="escaped_apostrophe"),
    pytest.param('cat "/ Client Secret/deep/notes.md" now', 'cat "path-1 [text hidden]" now',
                 id="leading_space_first_segment"),
    pytest.param("cat /srv/data/Client\\ \\ Secret/deep/notes.md now",
                 "cat path-1 [text hidden]", id="two_adjacent_escaped_spaces"),
    pytest.param("cat /srv/data/O’Neil/deep/notes.md now", "cat path-1 [text hidden]",
                 id="typographic_apostrophe"),
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text,expected", ITEM3)
def test_fc4_item3_an_unregistered_path_is_one_word(level, text, expected):
    r = _redactor(level)
    for sink, out in _all_sinks(r, text).items():
        for word in PRIVATE:
            assert word not in out, (sink, word, out)
    assert r.scrub_leftover_paths(text) == expected
    r = _redactor(level)
    assert r(text) == expected


# --- F-W-spec-codex.md items 1 to 3 ---------------------------------------------------------------

SPEC1 = [
    pytest.param("/srv/data/Client\" Secret\"/deep/notes.md", id="double_quote_mid_segment"),
    pytest.param("/srv/data/Client' Secret'/deep/notes.md", id="single_quote_mid_segment"),
    pytest.param("“/srv/data/Client Secret/deep/notes.md”", id="typographic_wrapped_path"),
    pytest.param("/srv/data/Client\\\nSecret/deep/notes.md", id="backslash_newline_unquoted"),
    pytest.param('"/srv/data/Client\\\nSecret/deep/notes.md"', id="backslash_newline_double_quoted"),
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("path", SPEC1)
def test_fc4_spec_review_1_and_2_leave_no_private_tail(level, registered, path):
    r = _redactor(level, *(["/srv/data/Client"] if registered else []))
    _assert_clean(r, f"cat {path} now", ("Secret", "deep", "notes.md"))


@pytest.mark.parametrize("level", LEVELS)
def test_fc4_a_typographic_wrapped_path_keeps_its_wrapper(level):
    r = _redactor(level)
    # F fix 5 (W5-1): the decoded span holds a space, so it carries the marker
    assert r("see “/srv/data/Client Secret/deep/notes.md” now") == (
        "see “path-1 [text hidden]” now"
    )


@pytest.mark.parametrize("level", LEVELS)
def test_fc4_a_backslash_newline_joins_the_next_line_into_the_word(level):
    r = _redactor(level)
    # F fix 6 (W6-2): the path hides the rest of its line (the continuation joined two lines)
    assert r("cat /srv/data/Client\\\nSecret/deep/notes.md now") == "cat path-1 [text hidden]"
    r = _redactor(level)
    assert r('cat "/srv/data/Client\\\nSecret/deep/notes.md" now') == 'cat "path-1" now'


# The two normal forms are built at run time, so no editor or tool that normalizes the source file
# can silently turn an NFD row into an NFC one.
CAFE_NFC = unicodedata.normalize("NFC", "Caf\N{LATIN SMALL LETTER E WITH ACUTE}")
CAFE_NFD = unicodedata.normalize("NFD", CAFE_NFC)
assert CAFE_NFC != CAFE_NFD and len(CAFE_NFD) == len(CAFE_NFC) + 1

NFC_REGISTERED = f"/srv/data/{CAFE_NFC} Bar"
NFD_REGISTERED = f"/srv/data/{CAFE_NFD} Bar"
NFC_TEXT = f"cat /srv/data/{CAFE_NFC} Bar/deep/notes.md now"
NFD_TEXT = f"cat /srv/data/{CAFE_NFD} Bar/deep/notes.md now"


NFC_ROWS = [
    pytest.param(NFC_REGISTERED, NFD_TEXT, id="nfc_registered_nfd_text"),
    pytest.param(NFD_REGISTERED, NFC_TEXT, id="nfd_registered_nfc_text"),
    pytest.param(NFC_REGISTERED, NFC_TEXT, id="nfc_registered_nfc_text"),
    pytest.param(NFD_REGISTERED, NFD_TEXT, id="nfd_registered_nfd_text"),
]


@pytest.mark.parametrize(
    "level,registered,text",
    _grid(NFC_ROWS),
)
def test_fc4_a_registered_space_bearing_value_with_a_continuation_in_both_normal_forms(
    level, registered, text
):
    r = _redactor(level, registered)
    _assert_clean(r, text, ("Bar", "deep", "notes.md"))


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize(
    "registered,text",
    [
        pytest.param(CAFE_NFC, f'cat "{CAFE_NFD} Secret/deep/notes.md" now', id="nfc_in_nfd"),
        pytest.param(CAFE_NFD, f'cat "{CAFE_NFC} Secret/deep/notes.md" now', id="nfd_in_nfc"),
    ],
)
def test_fc4_a_registered_relative_value_in_the_other_normal_form_widens(level, registered, text):
    r = _redactor(level, registered)
    _assert_clean(r, text, ("Secret", "deep", "notes.md"))


# --- spelling table over one private path ---------------------------------------------------------

SPELLINGS = [
    pytest.param("\"/srv/data/Client Secret/O'Neil/deep/notes.md\"", id="double_quoted"),
    pytest.param("'/srv/data/Client Secret/O'\"'\"'Neil/deep/notes.md'", id="posix_splice"),
    pytest.param("/srv/data/Client\\ Secret/O\\'Neil/deep/notes.md", id="backslash_escaped"),
    pytest.param("/srv/data/'Client Secret'/O\\'Neil/deep/notes.md", id="half_quoted_segment"),
    pytest.param('"/srv/data/Client \\"Secret\\"/O\'Neil/deep/notes.md"', id="escaped_quote_inside"),
    pytest.param("/srv/data/Client\\ Secret/O’Neil/deep/notes.md", id="typographic_apostrophe"),
    pytest.param("--out=\"/srv/data/Client Secret/O'Neil/deep/notes.md\"", id="option_prefix"),
    pytest.param("(/srv/data/Client\\ Secret/O\\'Neil/deep/notes.md)", id="in_parentheses"),
    pytest.param("/srv/data/Client\\ Secret/O\\'Neil/deep/notes.md.", id="followed_by_dot"),
    pytest.param("/srv/data/Client\\ Secret/O\\'Neil/deep/notes.md,", id="followed_by_comma"),
    pytest.param("/srv/data/Client\\ Secret/O\\'Neil/deep/notes.md:12", id="followed_by_line"),
    pytest.param("/srv/data/Client\\ Secret/O\\'Neil/deep/notes.md:12:3", id="followed_by_line_col"),
]


@pytest.mark.parametrize(
    "level,registered,spelling",
    _grid(SPELLINGS, registered_dim=True),
)
def test_fc4_no_private_segment_survives_any_spelling(level, registered, spelling):
    r = _redactor(level, *(["/srv/data"] if registered else []))
    _assert_clean(r, f"cat {spelling} now", PRIVATE)


# --- retained facts ------------------------------------------------------------------------------

RETAINED = [
    # F fix 6 (W6-2): an operator is a stop only when whitespace or the line end follows it
    # F fix 7 (W7-1, R1): no operator stop; the rest of the line is hidden and marked
    pytest.param("ran /srv/private; echo failed", "ran path-1 [text hidden]",
                 id="semicolon_command"),
    pytest.param("ran /srv/private;echo failed", "ran path-1 [text hidden]",
                 id="semicolon_command_without_a_space"),
    pytest.param("cat /srv/private/a.log | grep ERROR > /srv/out/b.txt",
                 "cat path-1 [text hidden]",  # F fix 7 (W7-1, R1): one span to the line end
                 id="pipeline_and_redirection"),
    pytest.param("cmd >/srv/out/x.log 2>&1", "cmd >path-1 [text hidden]",  # F fix 6 (W6-2)
                 id="redirection_glued"),
    # F fix 7 (W7-1, R1): an evidence pointer is not a stop; the rest of the line is hidden
    pytest.param("see /srv/work/out:12:3 now", "see path-1 [text hidden]", id="line_and_column"),
    pytest.param("see /srv/work/out:12 now", "see path-1 [text hidden]", id="line"),
    # F fix 6 (W6-2): a path hides the rest of its line, so the words after it are `[text hidden]`
    pytest.param("PYTHONPATH=/srv/work/out ls", "PYTHONPATH=path-1 [text hidden]",
                 id="assignment_prefix"),
    pytest.param("PATH=/usr/bin:/srv/work/out ls", "PATH=path-1 [text hidden]",
                 id="path_list_is_one_path"),
    pytest.param("(/srv/private)", "(path-1)", id="parentheses"),
    # F fix 6 (W6-2): `)` is a stop only before whitespace or the line end; `)echo` is inside
    pytest.param("/srv/private)echo", "path-1", id="paren_boundary_inside_a_name"),
    pytest.param("/srv/private) echo", "path-1 [text hidden]",  # F fix 7 (W7-1, R1)
                 id="paren_boundary_keeps_the_next_word"),
    pytest.param("[/srv/work/out]", "[path-1]", id="brackets"),
    pytest.param("wrote /srv/work/out. Next", "wrote path-1 [text hidden]", id="sentence_dot"),
    pytest.param("cc -I/srv/inc/dir -o x", "cc -Ipath-1 [text hidden]", id="glued_short_option"),
    pytest.param("cc --out=/srv/inc/dir x", "cc --out=path-1 [text hidden]",
                 id="long_option_prefix"),
]


@pytest.mark.parametrize(
    "level,text,expected",
    _grid(RETAINED),
)
def test_fc4_retained_facts_and_one_rendering(level, text, expected):
    r = _redactor(level)
    assert r.scrub_leftover_paths(text) == expected
    r = _redactor(level)
    assert r(text) == expected
    assert "private" not in r(text) and "work" not in r(text)


# --- prose and URL negative controls (unchanged bytes) ---------------------------------------------

CONTROLS = [
    "choose and/or decide",
    "read-/write access",
    "see https://x.example/y/z for more",
    "it's fine and O'Neil said so",
    "say it's fine, don't worry",
    "checkout / restore",
    "send application/json and text/plain",
    "scored 3/4 on the first run",
    "the rock 'n' roll tail",
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text", CONTROLS)
def test_fc4_prose_and_urls_keep_their_bytes(level, text):
    r = _redactor(level)
    for sink, out in _all_sinks(r, text).items():
        assert out == text, (sink, out)


def test_fc4_the_plain_share_call_scrubs_an_unregistered_path():
    r = _redactor("share", "/srv/work/out")
    out = r("nothing to hide in /srv/other/place here")
    assert out == "nothing to hide in path-2 [text hidden]"  # F fix 6 (W6-2)


def test_fc4_closing_quote_two_paths_and_the_newline_boundary():
    r = _redactor("share")
    assert r.scrub_leftover_paths("cat '/srv/x/y.md' now") == "cat 'path-1' now"
    r = _redactor("share")
    # F fix 6 (W6-2): the first path hides the rest of its line, the second path with it
    assert r.scrub_leftover_paths("cp /srv/a/b.md /srv/c/d.md") == "cp path-1 [text hidden]"
    r = _redactor("share")
    assert r.scrub_leftover_paths("cat /srv/x/y.md\nnext /srv/z/w.md") == (
        "cat path-1\nnext path-2"
    )


# --- round 2 rows -------------------------------------------------------------------------------

REGISTERED_SUFFIX = [
    pytest.param("deep/notes.md", 'cat "Client Secret/deep/notes.md" now', id="suffix"),
    pytest.param(f"{CAFE_NFC}/deep/notes.md",
                 f'cat "Client Secret/{CAFE_NFD}/deep/notes.md" now', id="nfc_suffix_in_nfd_text"),
    pytest.param(f"{CAFE_NFD}/deep/notes.md",
                 f'cat "Client Secret/{CAFE_NFC}/deep/notes.md" now', id="nfd_suffix_in_nfc_text"),
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("registered,text", REGISTERED_SUFFIX)
def test_fc4_registered_suffix_after_a_space_bearing_prefix(level, registered, text):
    r = _redactor(level, registered)
    _assert_clean(r, text, ("Client", "Secret", "deep"))


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("cat /srv/data/“Client Secret”/deep/notes.md now", id="paired_double"),
        pytest.param("cat /srv/data/‘Client Secret’/deep/notes.md now", id="paired_single"),
        pytest.param("cat /srv/data/“Client Secret/deep/notes.md now", id="unpaired_double"),
        pytest.param("cat /srv/data/‘Client Secret/deep/notes.md now", id="unpaired_single"),
    ],
)
def test_fc4_mid_segment_typographic_quotes(level, text):
    _assert_clean(_redactor(level), text, ("Secret", "deep", "notes.md"))
    _assert_clean(_redactor(level, "/srv/data"), text, ("Secret", "deep", "notes.md"))


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("cat /srv/data/Client:Secret/deep/notes.md now", id="colon_in_name"),
        pytest.param("PATH=/usr/bin:Client/deep:Secret/notes.md x", id="relative_list_element"),
    ],
)
def test_fc4_a_colon_bearing_name_leaves_no_tail(level, text):
    _assert_clean(_redactor(level), text, ("Secret", "deep", "notes.md"))


@pytest.mark.parametrize("level", LEVELS)
def test_fc4_quoted_parentheses_are_word_text(level):
    r = _redactor(level)
    assert "Client" not in r('cat "(/srv/Client Secret/x)" now')


@pytest.mark.parametrize("level", LEVELS)
def test_fc4_the_omission_marker_in_a_diagnostic_and_an_exception(level):
    text = 'error="write /srv/private/file failed with EACCES"'
    expected = 'error="path-1 [text hidden]"'
    r = _redactor(level)
    assert r(text) == expected
    r = _redactor(level)
    assert r.exception_text(RuntimeError(text)) == expected
    r = _redactor(level)
    assert report._diagnostic_for_export(text, r) == expected


@pytest.mark.parametrize("level", LEVELS)
def test_fc4_quoted_prose_around_a_registered_path_is_hidden_with_a_marker(level):
    r = _redactor(level, "/srv/data/proj")
    assert r('echo "building /srv/data/proj now" done') == 'echo "path-2 [text hidden]" done'


@pytest.mark.parametrize("level", LEVELS)
def test_fc4_a_quoted_path_with_a_space_carries_the_marker(level):
    # F fix 5 (W5-1): renamed from `..._has_no_marker`; whitespace in the decoded span carries it
    r = _redactor(level, "/srv/data/proj")
    assert r('cat "/srv/data/proj/Acme Client/deep/notes.md" now') == (
        'cat "path-2 [text hidden]" now'
    )


# --- an unclosed quote ----------------------------------------------------------------------------


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param("cat /srv/Client' Secret/deep/notes.md now",
                     "cat path-1 [text hidden]", id="opener_inside_a_segment"),
        pytest.param("cat /srv/x/'y now Secret", "cat path-1 [text hidden]",
                     id="opener_after_a_slash"),
        pytest.param('cat "/srv/Client Secret/deep/notes.md now',
                     'cat "path-1 [text hidden]', id="double_quote_never_closed"),
    ],
)
def test_fc4_an_unclosed_quote_fails_closed(level, text, expected):
    r = _redactor(level)
    out = r(text)
    for word in ("Secret", "deep", "notes.md", "now"):
        assert word not in out, out
    assert out == expected


@pytest.mark.parametrize("level", LEVELS)
def test_fc4_an_unclosed_quote_ends_at_the_newline(level):
    r = _redactor(level)
    out = r.scrub_leftover_paths("echo '/srv/x/y\nnext line' tail")
    assert out == "echo 'path-1\nnext line' tail", out


# --- registered values across word boundaries -----------------------------------------------------


@pytest.mark.parametrize("level", LEVELS)
def test_fc4_a_registered_value_with_spaces_is_found_across_words(level):
    r = _redactor(level, "/srv/my work/out")
    # F fix 5 (W5-1): the decoded span holds a space, so it carries the marker
    # F fix 6 (W6-2): the path hides the rest of its line
    assert r("at /srv/my work/out now") == "at path-1 [text hidden]"
    assert r("see /srv/my work/out/sub/file.md now") == "see path-2 [text hidden]"


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text,expected", ITEM3)
def test_fc4_item3_with_a_registered_parent_no_private_segment_survives(level, text, expected):
    """The same unregistered spellings when a registered path (a parent, or the value
    itself) is also in play: registered and unregistered share one rendering."""
    r = _redactor(level, "/srv/data")
    for sink, out in _all_sinks(r, text).items():
        for word in PRIVATE:
            assert word not in out, (sink, word, out)
    r = _redactor(level, "/srv/data/Client")
    for sink, out in _all_sinks(r, text).items():
        for word in PRIVATE:
            assert word not in out, (sink, word, out)
