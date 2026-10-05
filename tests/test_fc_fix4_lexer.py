"""Fix round 4 (W1, W2, W3) of field milestone F: focused unit tests of the shell-word lexer, the
NFC registered lookup and the word rewriter behind `redact.scan_paths` (docs/DESIGN.md 5.1;
.reviews/v0.3/F-W-spec.md rev 3).

Every fixture is synthetic.
"""

from __future__ import annotations

import time
import unicodedata
from pathlib import Path

import pytest

from agent_postmortem import redact
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Pseudonyms, Redactor, scan_paths

HOME = Path("/home/user")
LDQ, RDQ = "\N{LEFT DOUBLE QUOTATION MARK}", "\N{RIGHT DOUBLE QUOTATION MARK}"
LSQ, RSQ = "\N{LEFT SINGLE QUOTATION MARK}", "\N{RIGHT SINGLE QUOTATION MARK}"


def _words(text):
    """(decoded, raw) of every word, boundaries left out."""
    return [
        (seg.text, text[seg.start:seg.end]) for seg in redact._lex(text) if not seg.boundary
    ]


def _boundaries(text):
    return [text[seg.start:seg.end] for seg in redact._lex(text) if seg.boundary]


def _decoded(text):
    return [decoded for decoded, _raw in _words(text)]


# --- W1: word boundaries ----------------------------------------------------------------------


def test_fc4_lex_whitespace_splits_words_and_is_a_boundary_run():
    assert _words("cat  /srv/x\tnow") == [("cat", "cat"), ("/srv/x", "/srv/x"), ("now", "now")]
    assert _boundaries("cat  /srv/x\tnow") == ["  ", "\t"]


@pytest.mark.parametrize("op", list(";|&<>()"))
def test_fc4_lex_each_unquoted_control_operator_ends_a_word(op):
    assert _decoded(f"a{op}b") == ["a", "b"]
    assert _boundaries(f"a{op}b") == [op]


def test_fc4_lex_a_run_of_operators_is_one_boundary():
    assert _boundaries("a&&b || c;;d") == ["&&", " || ", ";;"]
    assert _decoded("(/srv/private)echo") == ["/srv/private", "echo"]


@pytest.mark.parametrize("quoted", ['"a;b"', "'a;b'", "a\\;b", f"{LDQ}a;b{RDQ}"])
def test_fc4_lex_an_operator_inside_quotes_or_escaped_is_word_text(quoted):
    assert _decoded(quoted) == ["a;b"]


def test_fc4_lex_an_unescaped_newline_ends_a_word():
    assert _decoded("a\nb") == ["a", "b"]
    assert _boundaries("a\nb") == ["\n"]


# --- W1: escapes and continuations ---------------------------------------------------------------


def test_fc4_lex_a_backslash_makes_the_next_character_literal_outside_quotes():
    assert _words("a\\ b") == [("a b", "a\\ b")]
    assert _decoded("a\\'b") == ["a'b"]
    assert _decoded('a\\"b') == ['a"b']
    assert _decoded("a\\\\b") == ["a\\b"]


def test_fc4_lex_every_decoded_character_keeps_its_raw_span():
    seg = next(s for s in redact._lex("x\\ y") if not s.boundary)
    assert seg.chars == ["x", " ", "y"]
    assert seg.rstart == [0, 1, 3] and seg.rend == [1, 3, 4]


def test_fc4_lex_a_backslash_newline_joins_the_next_line_into_the_word():
    text = "a\\\nb"
    assert _words(text) == [("ab", text)]
    seg = next(s for s in redact._lex(text) if not s.boundary)
    assert seg.rstart == [0, 3] and seg.rend == [1, 4]


def test_fc4_lex_a_backslash_crlf_is_a_continuation_too():
    assert _decoded("a\\\r\nb") == ["ab"]


def test_fc4_lex_a_continuation_inside_double_quotes_joins_the_lines():
    assert _decoded('"a\\\nb c"') == ["ab c"]


def test_fc4_lex_a_continuation_does_not_hold_inside_single_quotes():
    # a single-quoted fragment is literal and never crosses a newline: it ends there, unclosed
    assert _decoded("'a\\\nb'") == ["a\\", "b"]


def test_fc4_lex_a_trailing_backslash_is_a_literal_character():
    assert _decoded("a\\") == ["a\\"]


# --- W1: openers and quote fragments --------------------------------------------------------------


def test_fc4_lex_a_double_quoted_fragment_is_one_word_with_its_spaces():
    seg = next(s for s in redact._lex('x "a b" y') if s.text == "a b")
    assert seg.frags == [[2, 6]]
    assert _decoded('x "a b" y') == ["x", "a b", "y"]


def test_fc4_lex_a_double_quote_opens_even_in_the_middle_of_a_word():
    assert _decoded('/srv/data/Client" Secret"/deep') == ["/srv/data/Client Secret/deep"]


def test_fc4_lex_a_single_quote_opens_in_the_middle_of_a_word_unless_it_is_in_word():
    assert _decoded("/srv/data/Client' Secret'/deep") == ["/srv/data/Client Secret/deep"]
    # a word character on each side: an ordinary character, no fragment
    assert _decoded("it's O'Neil") == ["it's", "O'Neil"]
    seg = next(s for s in redact._lex("O'Neil") if not s.boundary)
    assert seg.frags == []


@pytest.mark.parametrize(
    "text",
    ["x' y", "'x", "x'", "/'y", "/srv/x/' now", "x'/a", " 'a"],
)
def test_fc4_lex_an_apostrophe_at_an_edge_or_beside_a_space_or_slash_is_an_opener(text):
    assert any(seg.frags for seg in redact._lex(text))


def test_fc4_lex_double_quote_escapes_are_only_the_five_the_shell_defines():
    assert _decoded('"a\\"b\\\\c\\$d\\`e"') == ['a"b\\c$d`e']
    # F fix 6 (W6-5): the JSON escapes (`\n` here) are decoded as well, for detection
    assert _decoded('"a\\nb"') == ["a\nb"]
    assert _decoded('"a\\qb"') == ["a\\qb"]  # a backslash before any other character is kept
    assert _decoded('"a\\xb"') == ["a\\xb"]


def test_fc4_lex_single_quotes_have_no_escapes():
    assert _decoded("'a\\b'") == ["a\\b"]
    # F fix 6 (W6-6): `\'` and `\\` are decoded in every single-quoted fragment (the union of
    # the shell and Python repr rules), so `'a\'` is an unclosed fragment holding `a'`
    assert _decoded("'a\\'") == ["a'"]


def test_fc4_lex_a_posix_apostrophe_splice_is_adjacent_fragments_of_one_word():
    text = "'O'\"'\"'Neil'"
    assert _decoded(text) == ["O'Neil"]
    seg = next(s for s in redact._lex(text) if not s.boundary)
    assert len(seg.frags) == 3 and all(close >= 0 for _open, close in seg.frags)


def test_fc4_lex_an_unclosed_quote_ends_at_the_newline_and_takes_the_line():
    assert _decoded('"a b\nc d') == ["a b", "c", "d"]
    seg = next(s for s in redact._lex('"a b\nc'))
    assert seg.frags == [[0, -1]]
    assert _decoded("cat /srv/x/y' now\nnext") == ["cat", "/srv/x/y now", "next"]


# --- W1: typographic quotes -----------------------------------------------------------------------


def test_fc4_lex_typographic_quotes_wrap_a_fragment_at_the_start_of_a_word():
    assert _decoded(f"x {LDQ}a b{RDQ} y") == ["x", "a b", "y"]
    assert _decoded(f"x {LSQ}a b{RSQ} y") == ["x", "a b", "y"]


def test_fc4_lex_typographic_quotes_open_a_fragment_anywhere_outside_another_fragment():
    assert _decoded(f"/srv/data/{LDQ}Client Secret{RDQ}/deep") == ["/srv/data/Client Secret/deep"]
    assert _decoded(f"/srv/data/{LSQ}Client Secret{RSQ}/deep") == ["/srv/data/Client Secret/deep"]
    # inside a fragment of another kind they are ordinary
    assert _decoded(f'"a {LDQ} b"') == [f"a {LDQ} b"]


def test_fc4_lex_a_right_single_quote_between_word_characters_is_an_apostrophe():
    assert _decoded(f"O{RSQ}Neil x") == [f"O{RSQ}Neil", "x"]
    assert _decoded(f"{LSQ}O{RSQ}Neil{RSQ}") == [f"O{RSQ}Neil"]


def test_fc4_lex_an_unclosed_typographic_opener_takes_the_rest_of_the_line():
    assert _decoded(f"/srv/data/{LDQ}Client Secret/deep\nnext") == [
        "/srv/data/Client Secret/deep", "next",
    ]


# --- W2: the registered lookup ---------------------------------------------------------------------


def _lookup(*values):
    lookup = redact._PathLookup()
    lookup.sync({value: f"path-{n}" for n, value in enumerate(values, 1)})
    return lookup


CAFE_NFC = unicodedata.normalize("NFC", "Caf\N{LATIN SMALL LETTER E WITH ACUTE}")
CAFE_NFD = unicodedata.normalize("NFD", CAFE_NFC)


def test_fc4_lookup_matches_either_normal_form_and_returns_the_registered_spelling():
    assert CAFE_NFC != CAFE_NFD
    nfc, nfd = _lookup(CAFE_NFC), _lookup(CAFE_NFD)
    assert nfc.exact(CAFE_NFD) == CAFE_NFC and nfd.exact(CAFE_NFC) == CAFE_NFD
    assert nfc.exact("cafe") is None  # no case folding, no accent folding
    assert _lookup("Out.md").exact("out.md") is None


def test_fc4_lookup_ignores_the_root_and_the_empty_value_and_is_incremental():
    mapping = {"/": "path-1", "": "x", "/srv/a": "path-2"}
    lookup = redact._PathLookup()
    lookup.sync(mapping)
    assert lookup.exact("/") is None and lookup.exact("/srv/a") == "/srv/a"
    mapping["/srv/b"] = "path-3"
    lookup.sync(mapping)
    assert lookup.exact("/srv/b") == "/srv/b" and lookup.exact("/srv/a") == "/srv/a"


def test_fc4_lookup_hits_gives_the_parts_a_value_spans():
    lookup = _lookup("/srv/my work/out")
    parts = ["see ", "/srv/my", " ", "work/out", "/x"]
    assert lookup.hits(parts) == [(1, 3)]
    assert lookup.hits(["/srv/my", " work", "/out"]) == [(0, 2)]
    assert lookup.hits(["nothing"]) == []


def test_fc4_lookup_tries_the_longest_value_first_and_finds_every_start():
    lookup = _lookup("/srv/a", "/srv/a/b")
    assert lookup.longest_at("/srv/a/b/c", 0) == "/srv/a/b"
    assert lookup.hits(["/srv/a x /srv/a/b"]) == [(0, 0), (0, 0)]


def test_fc4_lookup_scales_to_thousands_of_values_within_a_stated_bound():
    """The bound (docs/DESIGN.md 5.1 does not cap it): one entry per distinct value, a scan is
    O(L * k) for k values sharing a first character. 3000 absolute values and 1000 slashes."""
    names = Pseudonyms()
    for number in range(3000):
        names.path(f"/srv/data/proj{number}/src/file{number}.py")
    # F fix 6 (W6-2): one path per line (a path hides the rest of its line)
    text = "\n".join(f"/x/y{number}/z" for number in range(1000))
    started = time.monotonic()
    out = scan_paths(text, names, lambda token, hit: "<P>")
    assert out.count("<P>") == 1000
    assert time.monotonic() - started < 10


# --- W2 and W3: which words are rewritten, and how ----------------------------------------------------


def _scan(text, *registered, absolute=True):
    names = Pseudonyms()
    for value in registered:
        names.path(value)
    seen = []

    def render(token, hit):
        seen.append((token, hit))
        return "<P>"

    out = scan_paths(text, names, render, absolute=absolute)
    return out, seen


def test_fc4_a_text_with_no_slash_no_tilde_and_no_registered_value_is_returned_as_is():
    text = "plain prose with 'quotes' and (parens); nothing else"
    out, seen = _scan(text)
    assert out is text and seen == []


def test_fc4_a_word_with_no_path_keeps_its_bytes_around_a_word_with_one():
    out, _ = _scan("a  'b c' /srv/x\t( d )")
    # F fix 6 (W6-2): the path's line runs to the `)`, which is followed by the end of the line
    assert out == "a  'b c' <P> [text hidden] )"


def test_fc4_the_absolute_pass_can_be_turned_off_for_registered_values_only():
    out, seen = _scan("see /srv/other/x and /srv/reg/y", "/srv/reg", absolute=False)
    assert out == "see /srv/other/x and <P>"
    assert seen == [("/srv/reg/y", True)]


def test_fc4_the_render_callback_gets_the_path_text_and_whether_a_registered_value_was_found():
    out, seen = _scan("a /srv/reg/y b /srv/other/z", "/srv/reg")
    # F fix 6 (W6-2): the second path is inside the first path's span
    assert seen == [("/srv/reg/y b /srv/other/z", True)]
    out, seen = _scan("a /srv/reg/y\nb /srv/other/z", "/srv/reg")
    assert seen == [("/srv/reg/y", True), ("/srv/other/z", False)]


def test_fc4_a_word_exactly_a_registered_value_prints_its_registered_display_not_the_callback():
    names = Pseudonyms()
    names.path("/srv/reg")
    out = scan_paths("x /srv/reg y", names, lambda token, hit: "<WRONG>")
    assert out == "x path-1 [text hidden]"  # F fix 6 (W6-2): the span keeps the registered display
    assert scan_paths("x /srv/reg", names, lambda token, hit: "<WRONG>") == "x path-1"


@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param("NAME=/srv/x/y", "NAME=<P>", id="assignment"),
        pytest.param("_n1=/srv/x/y", "_n1=<P>", id="identifier_with_digit_and_underscore"),
        pytest.param("--out-dir=/srv/x/y", "--out-dir=<P>", id="long_option"),
        pytest.param("-C/srv/x/y", "-C<P>", id="glued_short_option"),
        pytest.param("[/srv/x/y]", "[<P>]", id="bracket"),
        pytest.param("{/srv/x/y}", "{<P>}", id="brace"),
        pytest.param("`/srv/x/y`", "`<P>`", id="backquote"),
        pytest.param("[NAME=/srv/x/y]", "[NAME=<P>]", id="bracket_then_assignment"),
    ],
)
def test_fc4_a_proven_prefix_is_kept_in_front_of_the_path(text, expected):
    assert _scan(text)[0] == expected


@pytest.mark.parametrize(
    "text",
    ["abc:/srv/x/y", "abc,/srv/x/y", "a.b=/srv/x/y", "9a=/srv/x/y", "--=/srv/x/y", "-/srv/x/y/z"],
)
def test_fc4_nothing_else_in_front_of_the_path_survives(text):
    out, seen = _scan(text)
    assert out == "<P>", out
    assert seen[0][0] == text


@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param("/srv/x/y.", "<P>.", id="dot"),
        pytest.param("/srv/x/y,", "<P>,", id="comma"),
        pytest.param("/srv/x/y:", "<P>:", id="colon"),
        pytest.param("/srv/x/y!?", "<P>!?", id="exclamation_question"),
        pytest.param("/srv/x/y:12", "<P>:12", id="line"),
        pytest.param("/srv/x/y:12:3", "<P>:12:3", id="line_column"),
        pytest.param("/srv/x/y:12:3.", "<P>:12:3.", id="line_column_dot"),
        pytest.param("/srv/x/y:12:3:4", "<P>:3:4", id="three_numbers_keep_two"),
        pytest.param(f"{LDQ}/srv/x/y{RDQ}.", f"{LDQ}<P>{RDQ}.", id="typographic_then_dot"),
        pytest.param("/srv/x/y\\ ", "<P>\\ ", id="escaped_trailing_space"),
        pytest.param("`/srv/x/y`.", "`<P>`.", id="backquote_dot"),
    ],
)
def test_fc4_the_raw_suffix_stays_outside_the_path(text, expected):
    assert _scan(text)[0] == expected


def test_fc4_a_dot_before_a_slash_is_path_text_and_a_colon_inside_the_word_is_too():
    assert _scan("/srv/x./y")[0] == "<P>"
    assert _scan("PATH=/usr/bin:/srv/x")[0] == "PATH=<P>"
    assert _scan("/srv/data/Client:Secret/deep")[0] == "<P>"


def test_fc4_a_word_that_is_one_quoted_fragment_keeps_its_delimiters():
    assert _scan('cat "/srv/x/y" now')[0] == 'cat "<P>" now'
    assert _scan("cat '/srv/x/y' now")[0] == "cat '<P>' now"
    assert _scan(f"cat {LDQ}/srv/x/y{RDQ} now")[0] == f"cat {LDQ}<P>{RDQ} now"
    assert _scan('say "--out=/srv/x/y" now')[0] == 'say "--out=<P>" now'


def test_fc4_a_closing_delimiter_after_trailing_punctuation_is_kept():
    assert _scan('x "/srv/x/y".')[0] == 'x "<P>".'
    assert _scan('{"p": "/srv/x/y"}')[0] == '{"p": "<P>"}'


def test_fc4_a_fragment_that_opens_inside_the_path_does_not_leave_a_dangling_delimiter():
    # F fix 5 (W5-1): each span holds a space, so each carries the marker
    assert _scan("/srv/data/'Client Secret'/deep")[0] == "<P> [text hidden]"
    assert _scan("/srv/data/'Client Secret'")[0] == "<P> [text hidden]"
    assert _scan("/srv/data/'Client Secret'.")[0] == "<P> [text hidden]."
    assert _scan(f"/srv/data/{LDQ}Client Secret{RDQ}/deep")[0] == "<P> [text hidden]"


def test_fc4_the_whole_word_is_the_path_text_from_the_path_start_to_its_end():
    text = "/srv/data/Client\\ Secret/'O'\"'\"'Neil'/deep/notes.md"
    out, seen = _scan(text)
    assert out == "<P> [text hidden]"  # F fix 5 (W5-1): the decoded span holds a space
    assert seen == [("/srv/data/Client Secret/O'Neil/deep/notes.md", False)]


@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param('error="write /srv/p/f failed"', 'error="<P> [text hidden]"',
                     id="prose_before_the_path"),
        pytest.param('"see /srv/p/f"', '"<P> [text hidden]"', id="quoted_prose"),
        pytest.param("cat /srv/x/y' now", "cat <P> [text hidden]", id="unclosed_quote"),
        pytest.param('cat "/srv/x/y z', 'cat "<P> [text hidden]', id="unclosed_double_quote"),
        # F fix 5 (W5-1): ids renamed from `..._has_no_marker`; whitespace in the decoded span
        # carries the marker, a continuation has none
        pytest.param('"/srv/data/A B/c"', '"<P> [text hidden]"',
                     id="quoted_path_with_a_space_carries_the_marker"),
        pytest.param("/srv/data/A\\ B/c", "<P> [text hidden]",
                     id="escaped_space_carries_the_marker"),
        pytest.param("/srv/data/A\\\nB/c", "<P>", id="continuation_has_no_marker"),
        pytest.param("/srv/data/'A B'/c", "<P> [text hidden]",
                     id="quoted_segment_carries_the_marker"),
    ],
)
def test_fc4_the_omission_marker_is_added_when_text_may_have_been_hidden(text, expected):
    assert _scan(text)[0] == expected


def test_fc4_the_marker_is_added_to_a_word_that_is_exactly_a_registered_value_with_a_space():
    # F fix 5 (W5-1): renamed from `..._is_never_added_...`; the whitespace rule has no exemption
    out, _ = _scan('x "/srv/my work/out" y', "/srv/my work/out")
    assert out == 'x "path-1 [text hidden]" y'


def test_fc4_a_registered_value_with_spaces_or_operators_is_found_across_words():
    out, seen = _scan("at /srv/my work/out/sub (x) now", "/srv/my work/out")
    # F fix 5 (W5-1): a span with a space carries the marker; one with only an operator does not
    # F fix 7 (W7-1, R1): no early stop, the span runs to the end of the line
    assert out == "at <P> [text hidden]" and seen == [("/srv/my work/out/sub (x) now", True)]
    out, _ = _scan("at /srv/a;b/c now", "/srv/a;b")
    assert out == "at <P> [text hidden]"
    out, _ = _scan("a  /srv/two  spaces/x", "/srv/two  spaces")
    assert out == "a  <P> [text hidden]"


def test_fc4_a_registered_value_is_found_in_its_raw_spelling_too():
    # the lexer drops the quote of `A"B` (it opens a fragment), so the decoded word is `AB`
    out, seen = _scan('cat /srv/data/A"B/x" y', '/srv/data/A"B')
    # F fix 6 (W6-2): the path hides the rest of its line
    assert out == "cat <P> [text hidden]" and seen == [("/srv/data/AB/x y", True)]
    out, _ = _scan("cat /srv/data/a\\b/x", "/srv/data/a\\b")
    assert out == "cat <P>"


def test_fc4_a_registered_value_overlapping_the_prefix_is_part_of_the_path():
    out, seen = _scan("see [draft]/notes now", "[draft]/notes")
    # F fix 6 (W6-2): the path hides the rest of its line
    assert out == "see path-1 [text hidden]" and seen == []
    out, _ = _scan("see NAME=x/y now", "NAME=x")
    assert out == "see <P> [text hidden]"


def test_fc4_a_registered_match_matters_in_nfc_form_inside_a_longer_word():
    out, seen = _scan(f"cat x/{CAFE_NFD}/y now", f"{CAFE_NFC}/y")
    # F fix 6 (W6-2): the path hides the rest of its line
    assert out == "cat <P> [text hidden]" and seen == [(f"x/{CAFE_NFD}/y now", True)]


def test_fc4_scheme_and_double_slash_are_normalized_before_the_renderer():
    # F fix 6 (W6-2): the path hides the rest of its line
    assert _scan("open file:///opt/x/h now")[1] == [("/opt/x/h now", False)]
    assert _scan("open //srv/x/y now")[1] == [("/srv/x/y now", False)]
    assert _scan("open file:///opt/x/h")[1] == [("/opt/x/h", False)]
    assert _scan("open //srv/x/y")[1] == [("/srv/x/y", False)]


def test_fc4_a_home_relative_word_is_a_path_and_a_lone_tilde_or_name_is_not():
    assert _scan("cd ~/proj/x now")[0] == "cd <P> [text hidden]"  # F fix 6 (W6-2)
    assert _scan("cd ~/proj/x")[0] == "cd <P>"
    assert _scan("cd ~ now")[0] == "cd ~ now"
    assert _scan("mail ~bob now")[0] == "mail ~bob now"
    assert _scan("a~/b")[0] == "a~/b"


def test_fc4_an_unregistered_run_after_a_colon_or_hyphen_needs_two_segments_or_an_extension():
    assert _scan("note:/x is prose")[0] == "note:/x is prose"
    assert _scan("read-/write is prose")[0] == "read-/write is prose"
    # F fix 6 (W6-2): the path hides the rest of its line
    assert _scan("host:/opt/x/y done")[0] == "<P> [text hidden]"
    assert _scan("file:/x.py done")[0] == "<P> [text hidden]"


# --- the Redactor and Pseudonyms entry points -------------------------------------------------------------


def _redactor(level, *, detailed=True):
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    r.fresh_names(detailed=detailed and level == "local")
    return r


def test_fc4_check_text_is_check_n_at_share_scanned_at_local_and_raw_at_raw():
    check = "pytest /srv/data/Client\\ Secret/deep -k x"
    assert _redactor("share").check_text(check) == "check-1"
    # F fix 6 (W6-2): the escaped space is whitespace in the decoded span and ` -k x` follows
    assert _redactor("local").check_text(check) == "pytest path-1 [text hidden]"
    raw = Redactor(context=DisclosureContext(level="raw", home=HOME))
    raw.fresh_names()
    assert raw.check_text(check) == check
    assert _redactor("share").check_text(None) is None


def test_fc4_alias_path_prints_another_spelling_exactly_as_the_listed_one():
    r = _redactor("share")
    r.names.path("src/a.py")
    r.names.alias_path("/repo/src/a.py", "src/a.py")
    # F fix 6 (W6-2): one path per line (the first path would hide the rest of its line)
    assert r("see /repo/src/a.py\nand src/a.py") == "see path-1\nand path-1"
    r.names.alias_path("/repo/src/a.py", "src/other.py")  # an existing spelling is never rebound
    assert r("/repo/src/a.py") == "path-1"


def test_fc4_leftover_numbering_is_shared_with_display_path_and_a_tmp_path_is_path_n():
    r = Redactor(context=DisclosureContext(level="share", home=HOME))
    r.fresh_names()
    shown = r.display_path("/etc/one")
    assert shown == "path-1"
    # F fix 6 (W6-2): one path per line (a path hides the rest of its line)
    assert r.scrub_leftover_paths(f"x {shown}\n/etc/two\n/etc/one") == "x path-1\npath-2\npath-1"
    assert r.scrub_leftover_paths("wrote /tmp/aa/bb/cc.md") == "wrote path-3"
    assert r("wrote /tmp/aa/bb/cc.md") == "wrote /tmp/aa/.../cc.md"


def test_fc4_a_local_redactor_without_the_p19_resolver_keeps_the_structural_home_form():
    r = _redactor("local", detailed=False)
    # F fix 6 (W6-2): one path per line (a path hides the rest of its line)
    assert r("at /home/user/proj/a/b.md\nand /srv/x/y") == "at ~/.../b.md\nand path-1"


def test_fc4_apply_paths_scrubs_only_registered_values():
    names = Pseudonyms()
    names.path("/srv/work/out")
    # F fix 6 (W6-2): the registered path hides the rest of its line
    assert names.apply_paths("a /srv/other/x b /srv/work/out/deep c") == (
        "a /srv/other/x b path-2 [text hidden]"
    )
    assert names.apply_paths("a /srv/work/out", render=lambda token: "<R>") == "a path-1"
    assert names.apply_paths("a /srv/work/outer", render=lambda token: f"<{token}>") == (
        "a </srv/work/outer>"
    )


def test_fc4_local_text_uses_the_scanner_for_a_name_and_collapses_a_home_relative_path():
    r = _redactor("local")
    # F fix 5 (W5-1): the escaped space is whitespace in the decoded span
    assert r.names.local_text("fix /srv/data/Client\\ Secret/deep") == "fix path-1 [text hidden]"
    assert r.names.local_text("see ~/a/b/c.md") == "see ~/.../c.md"
    assert r.names.local_text("a\x00b /") == "a b path-2"


def test_fc4_the_scanner_is_idempotent_on_its_own_output():
    for text in (
        'error="write /srv/p/f failed"', "cat /srv/data/Client\\ Secret/x now",
        f"{LDQ}/srv/a b/c{RDQ}.", "PATH=/a/b:/c/d ls", "(/srv/x) ; /srv/y:3:4",
    ):
        r = _redactor("share")
        once = r(text)
        assert r(once) == once, (text, once)


def test_fc4_an_unpaired_closing_typographic_quote_at_the_end_is_raw_suffix():
    # F fix 7 (W7-1, R1): a stray closer is name text; the rest of the line is hidden
    assert _scan(f"cat /srv/x/y{RDQ} now")[0] == "cat <P> [text hidden]"
    assert _scan(f"cat /srv/x/y{RSQ}. now")[0] == "cat <P> [text hidden]"


def test_fc4_the_longest_registered_value_decides_how_many_words_are_joined():
    # the shorter value fits in one word; the longer one holds a space and must win
    out, _ = _scan("at /srv/my work/out/x now", "/srv/my", "/srv/my work/out")
    assert out == "at <P> [text hidden]"  # F fix 6 (W6-2): the joined span runs to the line end


def test_fc4_a_registered_hit_is_path_n_at_a_local_redactor_without_the_resolver():
    r = _redactor("local", detailed=False)
    r.names.path("/home/user/proj")
    # a registered hit keeps the run's own path-N namespace; only an unregistered path gets
    # the structural home form at this level
    # F fix 6 (W6-2): one path per line (a path hides the rest of its line)
    assert r("see /home/user/proj/x/y.md\nand /home/user/other/z.md") == (
        "see path-2\nand ~/.../z.md"
    )


def test_fc4_scrub_local_line_scans_a_line_that_never_met_the_plain_call():
    row = "impl  fix /srv/data/Client\\ Secret/deep  3 attempts"
    # F fix 5 (W5-1): the escaped space is whitespace in the decoded span
    # F fix 6 (W6-2): a raw path in a row hides the rest of the row (the sinks scan each field
    # before they assemble a row, so a row only meets this net when a field was not scanned)
    assert _redactor("local").scrub_local_line(row) == "impl  fix path-1 [text hidden]"
    # share and raw lines, and a local redactor without the run sinks' detail, pass unchanged
    assert _redactor("share").scrub_local_line(row) == row
    assert _redactor("local", detailed=False).scrub_local_line(row) == row


@pytest.mark.parametrize(
    "space",
    ["\N{NO-BREAK SPACE}", "\N{IDEOGRAPHIC SPACE}", "\N{EM SPACE}", "\N{LINE SEPARATOR}"],
)
def test_fc4_a_unicode_space_is_a_name_character_not_a_word_boundary(space):
    # a shell separates words at a space, a tab and a newline only; a name may hold the others
    text = f"cat /srv/data/Client{space}Secret/deep/notes.md now"
    out, seen = _scan(text)
    assert out == "cat <P> [text hidden]", out  # F fix 6 (W6-2): the path hides the rest of its line
    assert seen == [(f"/srv/data/Client{space}Secret/deep/notes.md now", False)]


def test_fc4_a_carriage_return_is_a_word_boundary_like_a_newline():
    assert _decoded("a\r\nb\rc\x0bd\x0ce") == ["a", "b", "c", "d", "e"]
    assert _scan("cat /srv/x/y\r\nnext /srv/z/w\r\n")[0] == "cat <P>\r\nnext <P>\r\n"
