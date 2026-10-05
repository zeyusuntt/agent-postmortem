"""Fix round 6 (W6-2 to W6-8) of field milestone F: focused unit tests of the end-of-line span, the
lexer and the JSON pass behind `redact.scan_paths` (docs/DESIGN.md 5.1; .reviews/v0.3/
F-dispositions.md, "Fix round 5 review").

Every fixture is synthetic.
"""

from __future__ import annotations

import time

import pytest

from agent_postmortem import redact
from tests.test_fc_fix5_lexer import HIDDEN, LDQ, RDQ, _decoded, _scan

# --- W6-2: the span runs to the end of the line, three stops end it early ---------------------------

SPANS = [
    # (text, expected output, expected rendered path texts)
    pytest.param("ls /srv/x|wc -l", f"ls <P>{HIDDEN}", ["/srv/x|wc -l"], id="pipe_without_a_space"),
    # F fix 7 (W7-1, R1): the three early stops are removed; each `stop_*` row now runs to the end
    pytest.param("a /srv/x ; b", f"a <P>{HIDDEN}", ["/srv/x ; b"], id="stop_semicolon_space"),
    pytest.param("a /srv/x ;b", f"a <P>{HIDDEN}", ["/srv/x ;b"], id="semicolon_then_a_word"),
    pytest.param("a /srv/x ;; b", f"a <P>{HIDDEN}", ["/srv/x ;; b"], id="stop_a_run_of_operators"),
    pytest.param("(/srv/x)", "(<P>)", ["/srv/x"], id="stop_closing_parenthesis_at_the_end"),
    pytest.param("x /srv/a b) c", f"x <P>{HIDDEN}", ["/srv/a b) c"], id="stop_parenthesis_space"),
    pytest.param("/srv/a&b c", f"<P>{HIDDEN}", ["/srv/a&b c"], id="ampersand_inside_a_name"),
    pytest.param("/srv/a& b", f"<P>{HIDDEN}", ["/srv/a& b"], id="stop_ampersand_space"),
    pytest.param("/srv/a>b c", f"<P>{HIDDEN}", ["/srv/a>b c"], id="redirect_glued_inside"),
    pytest.param("/srv/a < b", f"<P>{HIDDEN}", ["/srv/a < b"], id="stop_redirect_space"),
    pytest.param("/srv/a b;", f"<P>{HIDDEN};", ["/srv/a b"], id="stop_operator_at_the_end"),
    pytest.param("/srv/x y:12 z", f"<P>{HIDDEN}", ["/srv/x y:12 z"], id="stop_evidence_space"),
    pytest.param("/srv/x y:12:3: z", f"<P>{HIDDEN}", ["/srv/x y:12:3: z"],
                 id="stop_evidence_column_colon_space"),
    pytest.param("/srv/x y:12: z", f"<P>{HIDDEN}", ["/srv/x y:12: z"],
                 id="stop_evidence_colon_space"),
    pytest.param("/srv/x y:12", f"<P>{HIDDEN}:12", ["/srv/x y"], id="stop_evidence_at_the_end"),
    pytest.param("/srv/x y:12abc z", f"<P>{HIDDEN}", ["/srv/x y:12abc z"], id="not_evidence_a_word"),
    pytest.param("/srv/x y:12:abc z", f"<P>{HIDDEN}", ["/srv/x y:12:abc z"],
                 id="not_evidence_colon_then_a_word"),
    pytest.param('/srv/x "a:12 b" c', f"<P>{HIDDEN}", ['/srv/x a:12 b c'],
                 id="evidence_inside_a_quote_is_not_a_stop"),
    pytest.param("/srv/x 'a; b' c", f"<P>{HIDDEN}", ["/srv/x a; b c"],
                 id="operator_inside_a_quote_is_not_a_stop"),
    pytest.param("/srv/a b/c» d", f"<P>{HIDDEN}", ["/srv/a b/c» d"],
                 id="stop_closing_guillemet"),
    pytest.param("/srv/a «b c» d", f"<P>{HIDDEN}", ["/srv/a b c d"],
                 id="a_closed_wrapper_inside_the_span"),
    pytest.param("/srv/a b\nc d", f"<P>{HIDDEN}\nc d", ["/srv/a b"], id="newline"),
    pytest.param("/srv/a b\r\nc", f"<P>{HIDDEN}\r\nc", ["/srv/a b"], id="crlf"),
    pytest.param('"/srv/a b" c d', f'"<P>{HIDDEN}" c d', ["/srv/a b"],
                 id="a_closed_quote_ends_the_span"),
    pytest.param("cat /srv/a /srv/b/c now", f"cat <P>{HIDDEN}", ["/srv/a /srv/b/c now"],
                 id="a_second_path_is_one_span"),
    pytest.param("x /srv/a ; /srv/b c", f"x <P>{HIDDEN}", ["/srv/a ; /srv/b c"],
                 id="a_new_span_after_a_stop"),
    pytest.param("x /srv/a b ; y /srv/c d", f"x <P>{HIDDEN}", ["/srv/a b ; y /srv/c d"],
                 id="two_spans_on_one_line"),
    pytest.param("a /srv/x\tb", f"a <P>{HIDDEN}", ["/srv/x\tb"], id="a_tab_joins"),
    pytest.param("/srv/x.py:12 failed", f"<P>{HIDDEN}", ["/srv/x.py:12 failed"],
                 id="evidence_in_the_first_word"),
    pytest.param("see ~bob/x/y now", f"see <P>{HIDDEN}", ["~bob/x/y now"], id="another_home_anchor"),
    pytest.param("see $HOME/x y", f"see <P>{HIDDEN}", ["~/x y"], id="home_variable_is_home"),
    pytest.param("see ${HOME}/x y", f"see <P>{HIDDEN}", ["~/x y"], id="home_variable_braces"),
    pytest.param("see FILE:///srv/x y", f"see <P>{HIDDEN}", ["/srv/x y"], id="file_upper_case"),
    pytest.param("see file://localhost/srv/x y", f"see <P>{HIDDEN}", ["/srv/x y"],
                 id="file_localhost"),
]


@pytest.mark.parametrize("text,expected,rendered", SPANS)
def test_fc6_the_span_runs_to_the_end_of_the_line_unless_one_of_three_things_stops_it(
    text, expected, rendered
):
    # F fix 7 (W7-1): the name is kept for the pinned ids; the three stops no longer exist
    out, seen = _scan(text)
    assert out == expected, out
    assert [token for token, _hit in seen] == rendered, seen


def test_fc6_a_registered_hit_starts_a_span_to_the_end_of_the_line():
    # the span keeps the registered value's own pseudonym (one path, one pseudonym) and the marker
    out, seen = _scan("wrote out.txt then more", "out.txt", absolute=False)
    assert out == f"wrote path-1{HIDDEN}", out
    assert seen == []
    # a registered name that continues without whitespace is a longer token: its own path
    out, seen = _scan("wrote out.txt.bak then more", "out.txt", absolute=False)
    assert out == f"wrote <P>{HIDDEN}", out
    assert seen == [("out.txt.bak then more", True)]
    # the registered value alone keeps the sentence around it and its registered display
    out, seen = _scan("wrote out.txt", "out.txt", absolute=False)
    assert out == "wrote path-1" and seen == []


def test_fc6_a_long_line_and_many_stops_stay_linear():
    started = time.perf_counter()
    redact_out, _ = _scan("/srv/x " + "w " * 99_999 + "w")
    assert redact_out.endswith(HIDDEN)
    out, seen = _scan("; ".join(["/srv/x"] * 20_000))
    # F fix 7 (W7-1, R1): no early stop, so the 20 000 paths are one span to the end of the line
    assert len(seen) == 1
    assert out == f"<P>{HIDDEN}"
    out, _ = _scan(" ".join(["/srv/x"] * 20_000))
    assert out == f"<P>{HIDDEN}"
    assert time.perf_counter() - started < 5.0


def test_fc6_the_forward_join_window_is_gone():
    assert not hasattr(redact, "_JOIN_WINDOW")


# --- W6-3: a partnered U+201D opens after a word character --------------------------------------------


def test_fc6_lex_a_partnered_right_double_quote_after_a_word_character_is_a_fragment():
    assert _decoded(f"/srv/data/Client{RDQ} Secret.txt{RDQ} now") == [
        "/srv/data/Client Secret.txt", "now",
    ]
    assert _decoded(f"x Client{RDQ} Secret{RDQ} y") == ["x", "Client Secret", "y"]


def test_fc6_lex_a_lone_right_double_quote_stays_a_suffix():
    assert _decoded(f"/srv/x/y{RDQ} now") == [f"/srv/x/y{RDQ}", "now"]
    assert _decoded(f"/srv/x/y{RDQ}\nand {RDQ}z{RDQ}") == [f"/srv/x/y{RDQ}", "and", "z"]


def test_fc6_lex_a_right_double_quote_after_a_word_character_needs_a_right_partner():
    # a left partner is enough only where no word character precedes the opener
    assert _decoded(f"a{RDQ} b{LDQ} c")[0] == f"a{RDQ}"
    assert _decoded(f" {RDQ}a b{LDQ} c") == ["a b", "c"]


# --- W6-4: a bracket span is JSON only when it stands as its own token ------------------------------------


@pytest.mark.parametrize("text,spans", [
    pytest.param("see [1] now", [(4, 7)], id="after_whitespace"),
    pytest.param("[1]", [(0, 3)], id="whole_text"),
    pytest.param('a:{"k":1}', [(2, 9)], id="after_a_colon"),
    pytest.param('a,{"k":1},b', [(2, 9)], id="after_a_comma_before_a_comma"),
    pytest.param('[[1],{"k":2}]', [(0, 13)], id="nested_is_one_span"),
    pytest.param("x[1]", [], id="glued_to_a_word_before"),
    pytest.param("[1]x", [], id="glued_to_a_word_after"),
    pytest.param('a={"k":1}', [], id="after_an_equals_sign"),
    pytest.param('{"k":1}x', [], id="object_glued_after"),
    pytest.param("notes[1].md", [], id="a_file_name"),
    pytest.param("Client{}Secret", [], id="empty_object_in_a_name"),
    pytest.param('Client["Acme"]Secret', [], id="string_array_in_a_name"),
    pytest.param("Acme[2024]/Q1", [], id="array_then_a_slash"),
    pytest.param("a [1]b", [], id="a_span_glued_to_the_next_word"),
    pytest.param("see [2024]/Q1", [], id="a_slash_right_after_a_span"),
    pytest.param("see [1].md", [], id="a_dot_right_after_a_span"),
])
def test_fc6_json_spans_need_a_token_boundary(text, spans):
    assert redact._json_spans(text) == spans


def test_fc6_a_glued_bracket_span_is_found_by_the_word_scanner():
    out, seen = _scan("see /srv/data/Client[1]Secret/x now")
    assert out == f"see <P>{HIDDEN}"
    assert seen == [("/srv/data/Client[1]Secret/x now", False)]


# --- W6-5: JSON escapes in a double-quoted fragment ---------------------------------------------------------


@pytest.mark.parametrize("text,decoded", [
    pytest.param('"\\u002fsrv\\u002fx y"', "/srv/x y", id="unicode_slash"),
    pytest.param('"\\/srv\\/x"', "/srv/x", id="escaped_slash"),
    pytest.param('"a\\nb"', "a\nb", id="newline_escape"),
    pytest.param('"a\\tb\\rc\\bd\\fe"', "a\tb\rc\bd\fe", id="other_control_escapes"),
    pytest.param('"\\u00e9"', "é", id="non_ascii"),
    pytest.param('"\\ud83d\\ude00"', "\U0001f600", id="surrogate_pair"),
    pytest.param('"\\ud800x"', "\\ud800x", id="lone_surrogate_stays_text"),
    pytest.param('"\\udc00x"', "\\udc00x", id="lone_low_surrogate_stays_text"),
    pytest.param('"\\\\u002f"', "\\u002f", id="escaped_backslash_then_u"),
    pytest.param('"\\u00zz"', "\\u00zz", id="bad_unicode_escape_stays_text"),
    pytest.param('"a\\qb"', "a\\qb", id="unknown_escape_stays_literal"),
])
def test_fc6_lex_a_double_quoted_fragment_decodes_json_escapes(text, decoded):
    assert _decoded(text) == [decoded]


def test_fc6_a_text_with_a_backslash_is_scanned_without_a_literal_slash():
    out, seen = _scan('{"a":"\\u002fsrv\\u002fx"}')
    assert out == '{"a":"<P>"}'
    assert seen == [("/srv/x", False)]
    out, seen = _scan('{"a":"\\u002fsrv\\u002fx y"')  # truncated
    assert "srv" not in out and "x y" not in out, out
    assert seen


# --- W6-6: single quotes always decode the Python repr escapes -------------------------------------------


@pytest.mark.parametrize("text,decoded", [
    pytest.param("'a\\'b c'", "a'b c", id="escaped_quote"),
    pytest.param("'a\\\\'", "a\\", id="escaped_backslash_then_the_closing_quote"),
    pytest.param("'a\\b'", "a\\b", id="other_backslash_stays_literal"),
    pytest.param("'a'\\''b'", "a'b", id="shell_splice_still_one_word"),
    pytest.param("'/srv/O\\'Neil \"x\" y/f'", "/srv/O'Neil \"x\" y/f", id="repr_both_quotes"),
])
def test_fc6_lex_a_single_quoted_fragment_reads_python_escapes_at_every_sink(text, decoded):
    assert _decoded(text) == [decoded]


def test_fc6_the_python_repr_flag_is_gone():
    import inspect

    assert "python_repr" not in inspect.signature(redact.scan_paths).parameters
    assert "python_repr" not in inspect.signature(redact.Redactor.scrub_leftover_paths).parameters


# --- W6-8: path forms ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text,token", [
    pytest.param("FILE:///srv/x/y", "/srv/x/y", id="file_upper"),
    pytest.param("File:///srv/x/y", "/srv/x/y", id="file_mixed"),
    pytest.param("file://localhost/srv/x/y", "/srv/x/y", id="file_localhost"),
    pytest.param("file://LocalHost/srv/x/y", "/srv/x/y", id="file_localhost_mixed"),
    pytest.param("file:////srv/x/y", "/srv/x/y", id="file_four_slashes"),
    pytest.param("~bob/x/y", "~bob/x/y", id="another_home"),
    pytest.param("~bob.smith-2/x/y", "~bob.smith-2/x/y", id="another_home_name_characters"),
    pytest.param("$HOME/x/y", "~/x/y", id="home_variable"),
    pytest.param("${HOME}/x/y", "~/x/y", id="home_variable_braces"),
    pytest.param("HOME=$HOME/x/y", "~/x/y", id="home_variable_after_an_assignment"),
    pytest.param("--dir=${HOME}/x/y", "~/x/y", id="home_variable_after_an_option"),
])
def test_fc6_the_path_start_grammar_sees_the_missing_forms(text, token):
    out, seen = _scan(text)
    assert [t for t, _hit in seen] == [token], seen
    assert out.replace("<P>", "") in ("", "HOME=", "--dir="), out


@pytest.mark.parametrize("text", [
    "echo $HOME and ${HOME}", "~ alone", "a~b/c", "https://h/~bob/x", "$HOMEDIR/x", "$HOME", "${HOME}",
    "mail bob@example.com",
])
def test_fc6_things_that_are_not_paths_stay_as_they_are(text):
    out, seen = _scan(text)
    assert out == text and seen == [], (out, seen)
