"""Fix round 5 (W5-1 to W5-6) of field milestone F: focused unit tests of the lexer, the registered
lookup, the word rewriter and the JSON pass behind `redact.scan_paths` (docs/DESIGN.md 5.1;
.reviews/v0.3/F-dispositions.md, "Fix round 4 (W) review").

Every fixture is synthetic.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_postmortem import redact
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Pseudonyms, Redactor, scan_paths

HOME = Path("/home/user")
HIDDEN = " [text hidden]"
LDQ, RDQ = "\N{LEFT DOUBLE QUOTATION MARK}", "\N{RIGHT DOUBLE QUOTATION MARK}"
DLQ = "\N{DOUBLE LOW-9 QUOTATION MARK}"


def _words(text):
    return [(seg.text, text[seg.start:seg.end]) for seg in redact._lex(text) if not seg.boundary]


def _decoded(text):
    return [decoded for decoded, _raw in _words(text)]


def _scan(text, *registered, absolute=True, **options):
    names = Pseudonyms()
    for value in registered:
        names.path(value)
    seen = []

    def render(token, hit):
        seen.append((token, hit))
        return "<P>"

    out = scan_paths(text, names, render, absolute=absolute, **options)
    return out, seen


# --- W5-3: backslash CRLF inside double quotes --------------------------------------------------


def test_fc5_lex_a_backslash_crlf_in_double_quotes_is_a_continuation():
    assert _decoded('"a\\\r\nb"') == ["ab"]
    assert _decoded('"a\\\nb"') == ["ab"]
    assert _decoded("a\\\r\nb") == ["ab"]
    # a bare carriage return after a backslash is still an escaped character
    assert _decoded('"a\\\rb"') == ["a\\\rb"]


# --- W5-2 (b): paired display wrappers -----------------------------------------------------------

WRAPPERS = [
    pytest.param("`", "`", id="backtick"),
    pytest.param(DLQ, LDQ, id="german_low_then_left_curly"),
    pytest.param(DLQ, RDQ, id="german_low_then_right_curly"),
    pytest.param(LDQ, LDQ, id="left_curly_closed_by_left_curly"),
    pytest.param(RDQ, RDQ, id="right_curly_as_opener"),
    pytest.param(RDQ, LDQ, id="reversed_curly_quotes"),
    pytest.param("\N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}",
                 "\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}", id="guillemets"),
    pytest.param("\N{SINGLE LEFT-POINTING ANGLE QUOTATION MARK}",
                 "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}", id="single_guillemets"),
    pytest.param("\N{LEFT CORNER BRACKET}", "\N{RIGHT CORNER BRACKET}", id="corner_brackets"),
    pytest.param("\N{LEFT WHITE CORNER BRACKET}", "\N{RIGHT WHITE CORNER BRACKET}",
                 id="white_corner_brackets"),
]


@pytest.mark.parametrize("opener,closer", WRAPPERS)
def test_fc5_lex_a_display_wrapper_is_a_fragment_whose_body_may_hold_spaces(opener, closer):
    text = f"x {opener}a b/c{closer} y"
    assert _decoded(text) == ["x", "a b/c", "y"], text
    text = f"/srv/data/{opener}Client Secret{closer}/deep"
    assert _decoded(text) == ["/srv/data/Client Secret/deep"], text


UNPAIRED = [row for row in WRAPPERS if row.values[0] != RDQ]


@pytest.mark.parametrize("opener,closer", UNPAIRED)
def test_fc5_lex_an_unpaired_display_wrapper_takes_the_rest_of_the_line(opener, closer):
    assert _decoded(f"x {opener}a b/c d\ne f") == ["x", "a b/c d", "e", "f"]


def test_fc5_lex_a_right_double_quote_inside_a_word_is_not_an_opener():
    # the pinned F fix 4 row: a lone closing quote glued to a path is a suffix, not an opener
    # F fix 7 (W7-1, R1): a stray closer is name text; the rest of the line is hidden
    assert _scan(f"cat /srv/x/y{RDQ} now")[0] == f"cat <P>{HIDDEN}"
    assert _decoded(f"/srv/x/y{RDQ} now") == [f"/srv/x/y{RDQ}", "now"]
    # an unpaired right double quote at the start of a word is an ordinary character too
    assert _decoded(f"{RDQ}a b") == [f"{RDQ}a", "b"]


def test_fc5_lex_a_right_double_quote_after_a_word_character_opens_when_a_right_partner_follows():
    # F fix 6 (W6-3): renamed from `..._is_not_an_opener_even_with_a_partner`. The first quote
    # follows a word character and has a U+201D partner on its line, so it opens a fragment
    # (`Client” Secret.txt”`); the whole span then runs to the end of the line
    out, seen = _scan(f"cat /srv/x/y{RDQ} and /srv/z/w{RDQ} now")
    assert out == f"cat <P>{HIDDEN}", out
    assert [token for token, _hit in seen] == ["/srv/x/y and /srv/z/w now"]


def test_fc5_lex_a_right_double_quote_needs_its_partner_on_its_own_line():
    assert _decoded(f"{RDQ}a b\nc{RDQ}") == [f"{RDQ}a", "b", f"c{RDQ}"]


def test_fc5_lex_the_earlier_typographic_pairs_are_unchanged():
    assert _decoded(f"x {LDQ}a b{RDQ} y") == ["x", "a b", "y"]
    lsq, rsq = "\N{LEFT SINGLE QUOTATION MARK}", "\N{RIGHT SINGLE QUOTATION MARK}"
    assert _decoded(f"x {lsq}a b{rsq} y") == ["x", "a b", "y"]
    assert _decoded(f"O{rsq}Neil x") == [f"O{rsq}Neil", "x"]


# --- W5-1: the marker follows the whitespace rule --------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param('"/srv/data/A B/c"', f'"<P>{HIDDEN}"', id="quoted_path_with_a_space"),
        pytest.param("/srv/data/A\\ B/c", f"<P>{HIDDEN}", id="escaped_space"),
        pytest.param("/srv/data/'A B'/c", f"<P>{HIDDEN}", id="quoted_segment"),
        pytest.param('"/srv/p/f failed"', f'"<P>{HIDDEN}"', id="anchored_prose"),
        pytest.param("/srv/data/A\\\nB/c", "<P>", id="continuation_has_no_space"),
        pytest.param("/srv/data/A\\\r\nB/c", "<P>", id="crlf_continuation_has_no_space"),
        pytest.param('"/srv/x/y"', '"<P>"', id="no_whitespace_no_marker"),
        pytest.param("/srv/x/y\\ ", "<P>\\ ", id="an_escaped_trailing_space_is_the_suffix"),
        pytest.param('cat "/srv/x/y z', f'cat "<P>{HIDDEN}', id="unclosed_double_quote"),
        pytest.param("\"/srv/a\tb/c\"", f'"<P>{HIDDEN}"', id="a_tab_is_whitespace"),
    ],
)
def test_fc5_the_marker_is_added_whenever_the_decoded_span_holds_whitespace(text, expected):
    out, _seen = _scan(text)
    assert out == expected, out


def test_fc5_the_marker_follows_an_exactly_registered_value_with_whitespace():
    out, _ = _scan('x "/srv/my work/out" y', "/srv/my work/out")
    assert out == f'x "path-1{HIDDEN}" y'
    out, _ = _scan('x "/srv/work/out" y', "/srv/work/out")
    assert out == 'x "path-1" y'


def test_fc5_a_span_with_whitespace_goes_to_the_opaque_renderer():
    names = Pseudonyms()
    plain, opaque = [], []

    def render(token, hit):
        plain.append(token)
        return "<P>"

    def render_hidden(token, hit):
        opaque.append(token)
        return "<H>"

    # F fix 6 (W6-2): one path per line (an unquoted path hides the rest of its line)
    out = scan_paths(
        'a /srv/x/y\n"/srv/p/q r"\n/srv/s/t\\ u', names, render, render_hidden=render_hidden,
    )
    assert out == f"a <P>\n\"<H>{HIDDEN}\"\n<H>{HIDDEN}", out
    assert plain == ["/srv/x/y"] and opaque == ["/srv/p/q r", "/srv/s/t u"]


# --- W5-2 (a): the forward join ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected,tokens",
    [
        # F fix 6 (W6-2): the forward join is replaced by the end-of-line rule, so a span runs
        # over every word to the end of the line (no window, no `/` word needed)
        pytest.param("see /srv/a Dir/x.py now", f"see <P>{HIDDEN}", ["/srv/a Dir/x.py now"],
                     id="next_word_with_a_slash"),
        pytest.param("see /srv/a b c Dir/x.py now", f"see <P>{HIDDEN}",
                     ["/srv/a b c Dir/x.py now"], id="two_words_before_the_slash_word"),
        pytest.param("see /srv/a Dir/x y/z w now", f"see <P>{HIDDEN}",
                     ["/srv/a Dir/x y/z w now"], id="a_following_slash_word_extends_the_span"),
        # F fix 7 (W7-1, R1): the operator, closing-wrapper and evidence stops are removed
        pytest.param("see /srv/a Dir/x.py:12: now", f"see <P>{HIDDEN}",
                     ["/srv/a Dir/x.py:12: now"], id="evidence_suffix_after_the_join"),
        pytest.param("see /srv/a Dir/x.py. Next", f"see <P>{HIDDEN}", ["/srv/a Dir/x.py. Next"],
                     id="sentence_dot_after_the_join"),
        pytest.param("see /srv/a a b c d e/f", f"see <P>{HIDDEN}", ["/srv/a a b c d e/f"],
                     id="there_is_no_four_word_window"),
        pytest.param("see /srv/a a b c d/e", f"see <P>{HIDDEN}", ["/srv/a a b c d/e"],
                     id="the_fourth_word_is_inside_the_span"),
        pytest.param("see /srv/a /srv/b c/d", f"see <P>{HIDDEN}", ["/srv/a /srv/b c/d"],
                     id="a_second_absolute_path_is_inside_the_span"),
        pytest.param("see /srv/a https://h/z", f"see <P>{HIDDEN}", ["/srv/a https://h/z"],
                     id="a_url_is_inside_the_span"),
        pytest.param("see /srv/a  b/c", f"see <P>{HIDDEN}", ["/srv/a  b/c"],
                     id="two_spaces_are_still_space_separated"),
        pytest.param("see /srv/a\tb/c", f"see <P>{HIDDEN}", ["/srv/a\tb/c"], id="tab_separated"),
        pytest.param("see /srv/a\nb/c", "see <P>\nb/c", ["/srv/a"], id="newline_stops"),
        pytest.param("see /srv/a\r\nb/c", "see <P>\r\nb/c", ["/srv/a"], id="crlf_stops"),
        pytest.param("see /srv/a; b/c", f"see <P>{HIDDEN}", ["/srv/a; b/c"], id="semicolon_stops"),
        pytest.param("see /srv/a | b/c", f"see <P>{HIDDEN}", ["/srv/a | b/c"], id="pipe_stops"),
        pytest.param("see /srv/a & b/c", f"see <P>{HIDDEN}", ["/srv/a & b/c"],
                     id="ampersand_stops"),
        pytest.param("see /srv/a > b/c", f"see <P>{HIDDEN}", ["/srv/a > b/c"],
                     id="redirect_stops"),
        pytest.param("see (/srv/a) b/c", f"see (<P>{HIDDEN}", ["/srv/a) b/c"],
                     id="closing_paren_stops"),
        # F fix 10 (W10-3, rule b): a quote after a word character, then spaces and a token that
        # holds a slash, may be a possessive of a cut-off path: the fragment runs to the line end
        pytest.param("see '/srv/a' b/c", f"see '<P>{HIDDEN}", ["/srv/a' b/c"],
                     id="quoted_word_is_closed"),
        pytest.param("see /srv/a b/c/ now", f"see <P>{HIDDEN}", ["/srv/a b/c/ now"],
                     id="a_trailing_slash_word"),
        pytest.param("see /srv/a\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK} b/c",
                     f"see <P>{HIDDEN}",
                     ["/srv/a\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK} b/c"],
                     id="a_closing_wrapper_ends_the_first_word_and_the_scan"),
        pytest.param("see /srv/a b\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK} c/d",
                     f"see <P>{HIDDEN}",
                     ["/srv/a b\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK} c/d"],
                     id="a_closing_wrapper_ends_a_word_before_the_slash_word"),
        pytest.param("see /srv/a b/c\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK} d/e",
                     f"see <P>{HIDDEN}",
                     ["/srv/a b/c\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK} d/e"],
                     id="a_closing_wrapper_ends_the_extension"),
    ],
)
def test_fc5_the_forward_join_rows(text, expected, tokens):
    out, seen = _scan(text)
    assert out == expected, out
    assert [token for token, _hit in seen] == tokens


def test_fc5_the_forward_join_does_not_take_a_registered_word():
    # F fix 6 (W6-2): a second registered word is inside the first path's span (the span keeps the
    # first value's pseudonym); on two lines each is its own path
    out, _ = _scan("python /repo/a.py src/a.py now", "/repo/a.py", "src/a.py")
    assert out == f"python path-1{HIDDEN}"
    out, _ = _scan("python /repo/a.py\nsrc/a.py now", "/repo/a.py", "src/a.py")
    assert out == f"python path-1\npath-2{HIDDEN}"


def test_fc5_a_registered_word_joins_a_following_unregistered_slash_word():
    out, _ = _scan("cat /srv/x/out Dir/z now", "/srv/x/out")
    assert out == f"cat path-1{HIDDEN}"  # F fix 6 (W6-2): the span keeps the registered display


# --- W5-2 (c): a registered value matches with and without its trailing slash -----------------------


def test_fc5_lookup_a_registered_trailing_slash_value_matches_without_the_slash():
    out, seen = _scan("cd /srv/my work && ls", "/srv/my work/")
    assert out == f"cd path-1{HIDDEN}"  # F fix 7 (W7-1, R1): `&& ls` is hidden with the line


def test_fc5_lookup_a_registered_value_with_the_slash_still_matches_with_it():
    out, _ = _scan("cd /srv/work/ ok", "/srv/work/")
    assert out == f"cd path-1{HIDDEN}"  # F fix 6 (W6-2): the span keeps the registered display
    out, _ = _scan("cd /srv/work/", "/srv/work/")
    assert out == "cd path-1"


# --- W5-4: JSON ------------------------------------------------------------------------------------


def test_fc5_json_a_whole_text_and_a_substring_are_scanned_by_value():
    out, seen = _scan('{"p":"/srv/x","s":"failed","n":7}')
    assert out == '{"p":"<P>","s":"failed","n":7}' and seen == [("/srv/x", False)]
    out, _ = _scan('x {"p":"/srv/x"} y [1,"/srv/z"] z')
    assert out == 'x {"p":"<P>"} y [1,"<P>"] z'


def test_fc5_json_a_string_that_is_unchanged_keeps_its_exact_spelling():
    text = '{"a" : "caf\\u00e9 \\/ x" ,\n "b":[ 1 , 2.50 , true ] , "c" : "/srv/x"}'
    out, _ = _scan(text)
    assert out == text.replace('"/srv/x"', '"<P>"')


def test_fc5_json_a_rewritten_string_is_re_encoded_as_json():
    out, _ = _scan('{"a":"say \\"hi\\" /srv/x"}')
    assert out == '{"a":"say \\"hi\\" <P>"}'


def test_fc5_json_a_nested_json_string_is_scanned_again():
    out, _ = _scan('{"i":"{\\"p\\":\\"/srv/x\\"}"}')
    assert out == '{"i":"{\\"p\\":\\"<P>\\"}"}'


def test_fc5_json_a_registered_value_inside_a_string_is_replaced():
    out, _ = _scan('{"p":"/srv/my work/out"}', "/srv/my work/out")
    assert out == f'{{"p":"path-1{HIDDEN}"}}'


def test_fc5_json_text_that_does_not_parse_takes_the_free_text_path():
    out, _ = _scan('{"p":"/srv/x" "q":1}')
    assert "/srv/x" not in out


def test_fc5_json_the_scan_of_hostile_brackets_is_bounded():
    for hostile in (
        "[" * 200_000, "{" * 200_000, '{"a":' * 60_000, "[1," * 80_000, "[1,2 " * 80_000,
        '{"a":1 ' * 50_000, '["x" ' * 80_000,
    ):
        start = time.perf_counter()
        out, _ = _scan(hostile + " /srv/x/y")
        assert "/srv/x/y" not in out
        assert time.perf_counter() - start < 10, "the JSON pass must stay linear"


def test_fc5_json_many_bracketed_words_do_not_use_up_the_budget_before_a_document():
    out, seen = _scan("[Errno 2] " * 2000 + '{"p":"/srv/x","n":1}')
    assert out.endswith('{"p":"<P>","n":1}'), out[-60:]
    assert seen == [("/srv/x", False)]


def test_fc5_json_the_text_around_a_document_is_scanned_too():
    # F fix 6 (W6-2): a path hides the rest of its line, a bracket span on that line included;
    # on separate lines the text around a document is scanned as before
    out, seen = _scan('/srv/x/y {"a":1} /srv/z/w')
    assert out == f"<P>{HIDDEN}"
    assert [token for token, _hit in seen] == ["/srv/x/y {a:1} /srv/z/w"]  # (quotes are decoded)
    out, seen = _scan('/srv/x/y\n{"a":1}\n/srv/z/w')
    assert out == '<P>\n{"a":1}\n<P>'
    assert [token for token, _hit in seen] == ["/srv/x/y", "/srv/z/w"]
    out, _ = _scan('first [1,2] then /srv/q/r')
    assert out == "first [1,2] then <P>"


def test_fc5_json_an_error_code_in_brackets_is_not_json_and_is_untouched():
    out, _ = _scan("[Errno 13] denied: /srv/x/y")
    assert out == "[Errno 13] denied: <P>"


# --- W5-5: Python repr escapes in single-quoted fragments (exception text) -------------------------


def test_fc5_python_mode_a_single_quoted_fragment_honours_backslash_escapes():
    # F fix 6 (W6-6): there is no python_repr flag; every single-quoted fragment reads `\'`, `\\`
    text = "PosixPath('/srv/O\\'Neil \"Client\" Secret/deep/f')"
    out, seen = _scan(text)
    assert out == f"PosixPath('<P>{HIDDEN}')", out
    assert seen == [("/srv/O'Neil \"Client\" Secret/deep/f", False)]
    out, _ = _scan("'/srv/a\\\\' ok /srv/b/c")
    assert out == "'<P>' ok <P>", out


def test_fc5_python_mode_an_unclosed_fragment_ends_at_the_newline():
    out, _ = _scan("echo '/srv/x/y\nnext line' tail")
    assert out == "echo '<P>\nnext line' tail", out


def test_fc5_a_backslash_quote_in_single_quotes_is_read_as_the_union_of_shell_and_python():
    # F fix 6 (W6-6): renamed from `..._posix_mode_keeps_single_quotes_literal`: the shell rule
    # would close the fragment at the quote after the backslash; the union rule keeps it open, so
    # the path runs over the rest of the line (it can only widen a span)
    out, seen = _scan("'/srv/a\\' ok /srv/b/c")
    assert seen[0][0] == "/srv/a' ok /srv/b/c"


# --- W5-6: a rule id is never a path -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param("R-NOPE-/opt/x/y", "R-NOPE-<P>", id="rule_id_glued_to_a_path"),
        pytest.param("R-RETRY-SAFE-/opt/x/y", "R-RETRY-SAFE-<P>", id="real_rule_id"),
        pytest.param("R-X1-2-/opt/x/y", "R-X1-2-<P>", id="digits_in_the_id"),
        pytest.param("x R-NOPE-/opt/x/y.", "x R-NOPE-<P>.", id="inside_prose"),
        pytest.param("abc-/opt/x/y", "<P>", id="not_a_rule_id"),
        pytest.param("R-nope-/opt/x/y", "<P>", id="lower_case_is_not_a_rule_id"),
        pytest.param("R--/opt/x/y", "<P>", id="empty_segment_is_not_a_rule_id"),
        pytest.param("XR-NOPE-/opt/x/y", "<P>", id="must_start_the_word"),
        pytest.param("Q-NOPE-/opt/x/y", "<P>", id="only_r_starts_a_rule_id"),
    ],
)
def test_fc5_the_prefix_regex_keeps_only_a_rule_id_shape(text, expected):
    assert _scan(text)[0] == expected


# --- W5-3: title and local name scrubs scan before they flatten controls ---------------------------


def test_fc5_a_local_title_with_a_backslash_crlf_keeps_no_tail():
    r = Redactor(context=DisclosureContext(level="local", home=HOME))
    r.fresh_names(detailed=True)
    shown = r.title("cat /srv/data/Client\\\r\nSecret/deep/notes.md now")
    assert "Secret" not in shown and "notes.md" not in shown
    shown = r.names.local_text("cat /srv/data/Client\\\r\nSecret/deep/notes.md now")
    assert "Secret" not in shown and "notes.md" not in shown


def test_fc5_a_local_title_still_flattens_controls_after_the_scan():
    r = Redactor(context=DisclosureContext(level="local", home=HOME))
    r.fresh_names(detailed=True)
    shown = r.title("a\x1bb\nc")
    assert "\x1b" not in shown and "\n" not in shown
    assert r.names.local_text("a\x1bb\nc") == "a b c"
