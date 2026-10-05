"""Fix round 6 (W6-2 to W6-8, W6-10) of field milestone F: the end-of-line rule, the paired U+201D
opener, bracket spans inside a name, JSON escapes in a double-quoted fragment, Python repr escapes
at every sink, lone surrogates in rewritten JSON, and the path forms the grammar missed
(docs/DESIGN.md 5.1; .reviews/v0.3/F-dispositions.md, "Fix round 5 review").

Every exact input of the sonnet review (F-W5-review.md) and of Codex round 5 (F-codex-r5.md) runs
through the sinks the reviewers list, at share and at local.

Every fixture is synthetic. Private words are the ones a stranger must never read.
"""

from __future__ import annotations

import json
import re

import pytest

from agent_postmortem import report
from tests.test_fc_fix5_redact import (
    GOLDEN_SINKS,
    HIDDEN,
    LEVELS,
    _all_sinks,
    _assert_golden,
    _redactor,
)

PRIVATE6 = (
    "Client", "Secret", "Neil", "deep", "notes.md", "Important", "Annual", "Holdings", "Acme",
    "copy", "bill.pdf", "Plans", "Corp", "Private", "Inc.", "Q1", "2024", "Report", "AT&T", "R&D",
    "Smith", "x.md", "Zqa",
)


def _sinks_for(text):
    """A local title is one line (control characters flatten), so a text with a newline is not a
    golden row for the two sinks that print a title."""
    return tuple(
        sink for sink in GOLDEN_SINKS
        if "\n" not in text or sink not in ("title", "finding_title")
    )


def _grid6(rows):
    """Every row at both levels."""
    return [
        pytest.param(level, *row.values, id=f"{row.id}-{level}")
        for row in rows for level in LEVELS
    ]


def _clean(r, text, private=PRIVATE6):
    for sink, out in _all_sinks(r, text).items():
        for word in private:
            assert word not in out, (sink, word, out)


# --- W6-2: a path start hides everything to the end of its line ------------------------------------

W62_CODEX = [
    pytest.param("/srv/a;b/c", "path-1", id="codex2_semicolon_inside_a_name"),
    pytest.param("cat /srv/The Important Client Annual Report 2026/deep/notes.md now",
                 f"cat path-1{HIDDEN}", id="codex2_long_name_more_than_four_words"),
    pytest.param("cat /srv/Client Secret/deep More Private Notes/notes.md now",
                 f"cat path-1{HIDDEN}", id="codex2_two_spaced_segments"),
    pytest.param("open /srv/data/Client (Acme Holdings)/deep/notes.md failed",
                 f"open path-1{HIDDEN}", id="codex2_parenthesized_directory_name"),
]

W62_SONNET = [
    pytest.param("open /srv/data/Client (copy)/deep/notes.md failed",
                 f"open path-1{HIDDEN}", id="sonnet2_copy_in_parentheses"),
    pytest.param("see /srv/data/AT&T/bill.pdf now", f"see path-1{HIDDEN}", id="sonnet2_ampersand"),
    pytest.param("see /srv/data/R&D Plans/notes.md now", f"see path-1{HIDDEN}",
                 id="sonnet2_ampersand_and_space"),
    pytest.param("see /srv/data/Client (Acme)/notes.md now", f"see path-1{HIDDEN}",
                 id="sonnet2_parenthesized_word"),
    pytest.param("see /srv/data/Client Secret (1)/x.md now", f"see path-1{HIDDEN}",
                 id="sonnet2_closing_parenthesis_then_slash"),
    pytest.param("pytest -q /srv/AT&T/x.py", "pytest -q path-1", id="sonnet2_local_check_text"),
    pytest.param("cat /srv/data/Client Secret.md now", f"cat path-1{HIDDEN}",
                 id="sonnet3_space_in_the_last_segment"),
    pytest.param("see /srv/data/Client Secret.md, now", f"see path-1{HIDDEN}",
                 id="sonnet3_space_in_the_last_segment_then_punctuation"),
    pytest.param("opened /srv/Acme Corp", f"opened path-1{HIDDEN}",
                 id="sonnet3_final_directory_with_a_space"),
    pytest.param("see /srv/data/Client Secret", f"see path-1{HIDDEN}",
                 id="sonnet3_final_directory_with_a_space_two"),
    pytest.param("see ~/Client Secret.md now", f"see path-1{HIDDEN}",
                 id="sonnet3_home_relative_space"),
    pytest.param("see /srv/data/Acme Inc. - Q1 Report 2024/x.md now", f"see path-1{HIDDEN}",
                 id="sonnet3_more_than_four_words"),
    pytest.param("see /srv/data/a b c d e f g h/Client.md now", f"see path-1{HIDDEN}",
                 id="sonnet3_eight_words"),
]


@pytest.mark.parametrize("level,text,expected", _grid6(W62_CODEX + W62_SONNET))
def test_fc6_w2_the_exact_review_inputs_are_one_path_to_the_end_of_the_line(level, text, expected):
    r = _redactor(level)
    _assert_golden(r, text, expected)
    _clean(r, text)


W62_STOPS = [
    # F fix 7 (W7-1, rule R1): the early stops are removed; the rest of the line is hidden
    pytest.param("/srv/private; echo failed", f"path-1{HIDDEN}", id="operator_semicolon_space"),
    pytest.param("/srv/private && echo failed", f"path-1{HIDDEN}", id="operator_and_space"),
    pytest.param("/srv/private || echo failed", f"path-1{HIDDEN}", id="operator_or_space"),
    pytest.param("/srv/private | wc -l", f"path-1{HIDDEN}", id="operator_pipe_space"),
    pytest.param("cat /srv/Client Secret/x.md | wc -l", f"cat path-1{HIDDEN}",
                 id="spaced_name_then_a_pipe"),
    pytest.param("cmd 2>/srv/Client/x.log; echo done", f"cmd 2>path-1{HIDDEN}",
                 id="redirect_target_then_a_semicolon"),
    pytest.param("(cat /srv/Client Secret/x)", f"(cat path-1{HIDDEN})",
                 id="closing_parenthesis_at_the_end"),
    pytest.param("echo $(cat /srv/Client/x) done", f"echo $(cat path-1{HIDDEN}",
                 id="command_substitution"),  # F fix 7 (W7-1, R1)
    pytest.param("cat /srv/Client Secret/x < /dev/null", f"cat path-1{HIDDEN}",
                 id="input_redirect_space"),  # F fix 7 (W7-1, R1): the second path is inside
    pytest.param("echo ok >/srv/Client/x", "echo ok >path-1", id="redirect_at_the_end"),
    pytest.param("/srv/Client/x.py:12: AssertionError", f"path-1{HIDDEN}",
                 id="evidence_in_the_first_word"),  # F fix 7 (W7-1, R1)
    pytest.param("/srv/Client/x.py:12:3: error: boom", f"path-1{HIDDEN}",
                 id="evidence_line_and_column"),  # F fix 7 (W7-1, R1)
    pytest.param("/srv/My Dir/test_x.py:12: AssertionError", f"path-1{HIDDEN}",
                 id="pytest_line_with_a_space"),  # F fix 7 (W7-1, R1)
    pytest.param("see /srv/Client Secret/x.py:12 failed", f"see path-1{HIDDEN}",
                 id="evidence_then_whitespace"),  # F fix 7 (W7-1, R1)
    pytest.param("see /srv/Client Secret/x.py:12:3 failed", f"see path-1{HIDDEN}",
                 id="evidence_column_then_whitespace"),  # F fix 7 (W7-1, R1)
    pytest.param("see /srv/Client Secret/x.py:12", f"see path-1{HIDDEN}:12",
                 id="evidence_at_the_end"),
    # F fix 7 (W7-1, R1): a stray closer is name text; the rest of the line is hidden
    pytest.param("cat /srv/Client Secret/x.md” now", f"cat path-1{HIDDEN}",
                 id="closing_curly_quote_ends_the_span"),
    pytest.param("cat /srv/Client Secret/x.md\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK} now",
                 f"cat path-1{HIDDEN}", id="closing_guillemet_ends_the_span"),
    pytest.param("cat /srv/Client Secret/x.md\N{RIGHT CORNER BRACKET} now",
                 f"cat path-1{HIDDEN}", id="closing_corner_bracket_ends_the_span"),
]


@pytest.mark.parametrize("level,text,expected", _grid6(W62_STOPS))
def test_fc6_w2_the_three_former_early_stops_no_longer_stop_the_span(level, text, expected):
    # F fix 7 (W7-1): renamed from `..._the_three_early_stops_keep_what_follows`; the stops are gone
    r = _redactor(level)
    _assert_golden(r, text, expected)
    _clean(r, text)


W62_INSIDE = [
    pytest.param("ls /srv/Client/x|wc -l", f"ls path-1{HIDDEN}", id="pipe_without_a_space"),
    pytest.param("/srv/Client/x;echo failed", f"path-1{HIDDEN}", id="semicolon_without_a_space"),
    pytest.param("/srv/Client/x &echo failed", f"path-1{HIDDEN}", id="ampersand_then_a_word"),
    pytest.param("/srv/Client/x.py:12abc now", f"path-1{HIDDEN}", id="not_an_evidence_suffix"),
    pytest.param("/srv/Client/x.py:12:abc now", f"path-1{HIDDEN}", id="evidence_then_a_word"),
    pytest.param("/srv/Client/x and /srv/Secret/y now", f"path-1{HIDDEN}",
                 id="a_second_path_is_inside_the_span"),
    pytest.param('/srv/Client/x "a:12 Secret" now', f"path-1{HIDDEN}",
                 id="an_evidence_look_alike_inside_a_quote"),
    pytest.param("/srv/Client/x \N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}a Secret"
                 "\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK} now",
                 f"path-1{HIDDEN}", id="a_closed_wrapper_inside_the_span"),
    pytest.param("/srv/Client/x 'a; Secret' now", f"path-1{HIDDEN}",
                 id="an_operator_inside_a_quote"),
    pytest.param('/srv/Client/x "Secret\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}" now',
                 f"path-1{HIDDEN}", id="a_closing_wrapper_inside_a_quote"),
]


@pytest.mark.parametrize("level,text,expected", _grid6(W62_INSIDE))
def test_fc6_w2_anything_else_stays_inside_the_span(level, text, expected):
    r = _redactor(level)
    _assert_golden(r, text, expected)
    _clean(r, text)


W62_LINES = [
    pytest.param("cat /srv/Client Secret/x.md now\nSecond line stays",
                 f"cat path-1{HIDDEN}\nSecond line stays", id="newline"),
    pytest.param("cat /srv/Client Secret/x.md now\r\nSecond line stays",
                 f"cat path-1{HIDDEN}\r\nSecond line stays", id="crlf"),
    pytest.param("a /srv/Client/x\nb /srv/Secret/y c\nd",
                 f"a path-1\nb path-2{HIDDEN}\nd", id="every_line_has_its_own_span"),
]


@pytest.mark.parametrize("level,text,expected", _grid6(W62_LINES))
def test_fc6_w2_a_newline_ends_the_span(level, text, expected):
    r = _redactor(level)
    outs = _all_sinks(r, text)
    for sink in _sinks_for(text):
        # the number of a second path depends on the namespace the sink uses at local
        assert re.sub(r"path-\d+", "path-N", outs[sink]) == re.sub(r"path-\d+", "path-N", expected), (
            sink, outs[sink],
        )
    _clean(r, text)


W62_CONTROLS = [
    pytest.param('cat "/srv/Client/x" and more words', 'cat "path-1" and more words',
                 id="a_closed_quote_ends_the_path"),
    pytest.param("cat /srv/Client/x. now", f"cat path-1{HIDDEN}", id="sentence_punctuation_inside"),
    pytest.param("see https://example.com/a b now", "see https://example.com/a b now",
                 id="a_url_is_not_a_path"),
    pytest.param("and/or 3/4 read-/write", "and/or 3/4 read-/write",
                 id="prose_slashes_are_not_a_start"),
]


@pytest.mark.parametrize("level,text,expected", _grid6(W62_CONTROLS))
def test_fc6_w2_controls_that_the_end_of_line_rule_leaves_alone(level, text, expected):
    assert _redactor(level)(text) == expected


@pytest.mark.parametrize("level", LEVELS)
def test_fc6_w2_a_registered_hit_followed_by_prose_hides_the_rest_of_the_line(level):
    r = _redactor(level, "out.txt")
    out = r("wrote out.txt and then the Secret was Client data")
    assert "Secret" not in out and "Client" not in out, out
    assert out.startswith("wrote path-"), out
    assert out.endswith(HIDDEN), out
    # the same registered value alone keeps the sentence around it
    assert r("wrote out.txt") == "wrote " + r("out.txt")


# --- W6-3: a partnered U+201D opens even after a word character -----------------------------------------

W63 = [
    pytest.param("cat /srv/data/Client” Secret.txt” now", f"cat path-1{HIDDEN}",
                 id="codex3_smart_quote_half_quoted_name"),
    pytest.param("cat /srv/data/Client” Secret/deep.txt”", "cat path-1" + HIDDEN,
                 id="partner_at_the_end_of_the_line"),
    pytest.param("cat /srv/x/y” now", "cat path-1" + HIDDEN,
                 id="control_a_lone_closing_quote_stays_a_suffix"),  # F fix 7 (W7-1, R1)
    pytest.param("cat /srv/x/y”\nand also “quoted” text", "cat path-1”\nand also “quoted” text",
                 id="control_a_partner_on_another_line_does_not_count"),
]


@pytest.mark.parametrize("level,text,expected", _grid6(W63))
def test_fc6_w3_a_partnered_right_double_quote_after_a_word_character_opens(level, text, expected):
    r = _redactor(level)
    _assert_golden(r, text, expected, sinks=_sinks_for(text))
    _clean(r, text)


# --- W6-4: a bracket span inside a name is part of the name ----------------------------------------------

W64_NAMES = [
    "Client[1]Secret/x", "Client[1]Secret.md", "Client[1] Secret/deep", "Acme[2024]/Q1 Report/notes.md",
    "Client[true]Secret/x", 'Client["Acme"]Secret/x', "Client{}Secret/x", "Client[]Secret",
    "Client[1,2]Secret", 'Client{"a":1}Secret', "notes[1].md",
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("name", W64_NAMES, ids=[f"n{i}" for i in range(len(W64_NAMES))])
def test_fc6_w4_a_bracket_span_inside_a_path_word_does_not_split_it(level, name):
    r = _redactor(level)
    for template in ("see /srv/data/{} now", "/srv/data/{}", "see /srv/data/{}, now"):
        _clean(r, template.format(name), PRIVATE6 + ("true", "Q1"))
    fresh = _redactor(level)(f"/srv/data/{name}")
    assert fresh == ("path-1" if " " not in name else f"path-1{HIDDEN}"), fresh


@pytest.mark.parametrize("level", LEVELS)
def test_fc6_w4_a_bracket_span_that_stands_alone_is_still_json(level):
    r = _redactor(level)
    assert r('see {"a":"/srv/Client/x","n":1} now') == 'see {"a":"path-1","n":1} now'
    assert r('[{"a":"/srv/Client/x"},"b"]') == '[{"a":"path-1"},"b"]'
    assert r('state:{"a":"/srv/Client/x"},x') == 'state:{"a":"path-1"},x'
    assert r('{"a":"/srv/Client/x"}') == '{"a":"path-1"}'


@pytest.mark.parametrize("level", LEVELS)
def test_fc6_w4_a_glued_bracket_span_is_scanned_with_its_word(level):
    r = _redactor(level)
    out = r('x{"a":"/srv/Client Secret/x"}y')
    for word in ("Client", "Secret"):
        assert word not in out, out


# --- W6-5: JSON escapes in a double-quoted fragment; the registered scan without a slash ------------------

W65_TRUNCATED = [
    '{"path":"\\u002fsrv\\u002fClient Secret\\u002fdeep\\u002fnotes.md"',
    '{"path":"\\/srv\\/Client Secret\\/deep\\/notes.md"',
    '{"path":"\\/srv\\/Client Secret\\/deep\\/notes.md" "n":1}',
    '["\\/srv\\/Client Secret\\/deep\\/notes.md",',
    '["\\u002fsrv\\u002fClient Secret\\u002fdeep\\u002fnotes.md" ',
    '{"a":{"p":"\\u002fsrv\\u002fClient Secret\\u002fdeep\\u002fnotes.md"',
    '"\\u002fsrv\\u002fClient Secret\\u002fdeep\\u002fnotes.md"',
    'x "\\/srv\\/Client Secret\\/deep\\/notes.md" y',
    '{"path":"\\u002FSRV-not-here","p2":"\\u002fsrv\\u002fClient\\u002fdeep\\u002fnotes.md"',
    '{"p":"\\u007e\\u002fClient Secret\\u002fdeep\\u002fnotes.md"',
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text", W65_TRUNCATED, ids=[f"t{i}" for i in range(len(W65_TRUNCATED))])
def test_fc6_w5_truncated_json_with_encoded_separators_is_still_a_path(level, text):
    _clean(_redactor(level), text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc6_w5_a_complete_json_text_with_no_literal_slash_is_scanned(level):
    r = _redactor(level)
    text = '{"a":"\\u002fsrv\\u002fClient\\u002fdeep\\u002fnotes.md"}'
    assert r(text) == '{"a":"path-1"}'
    _clean(r, text)
    assert r('{"a":"\\u007e\\u002fClient\\u002fdeep\\u002fnotes.md"}') in (
        '{"a":"~/.../notes.md"}', '{"a":"path-2"}', '{"a":"path-1"}',
    )


@pytest.mark.parametrize("level", LEVELS)
def test_fc6_w5_a_registered_value_with_no_slash_is_found_in_a_json_string(level):
    r = _redactor(level, "SECRETNAME")
    out = r('{"a":"SECRETNAME","b":"x SECRETNAME y"}')
    assert "SECRETNAME" not in out, out


# --- W6-6: a Python repr is one fragment at every sink -------------------------------------------------

REPR_TEXTS = [
    "ValueError: PosixPath('/srv/O\\'Neil \"Client\" Secret/deep/f')",
    "[Errno 13] Permission denied: '/srv/O\\'Neil \"Client\" Secret/deep/CLAUDE.md'",
    "FileNotFoundError: [Errno 2] No such file or directory: '/srv/Client\\\\ Secret/deep/f'",
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text", REPR_TEXTS, ids=["posixpath", "errno13", "backslash"])
def test_fc6_w6_a_python_repr_with_escapes_is_one_fragment_at_every_free_text_sink(level, text):
    r = _redactor(level)
    for sink, out in _all_sinks(r, text).items():
        for word in ("Client", "Secret", "Neil", "deep", "CLAUDE.md"):
            assert word not in out, (sink, word, out)


@pytest.mark.parametrize("level", LEVELS)
def test_fc6_w6_the_posixpath_repr_keeps_its_call_and_marks_the_hidden_text(level):
    r = _redactor(level)
    out = r(REPR_TEXTS[0])
    assert out == f"ValueError: PosixPath('path-1{HIDDEN}')", out


# --- W6-7: JSON rewriting never emits a lone surrogate ---------------------------------------------------


@pytest.mark.parametrize("level", LEVELS)
def test_fc6_w7_a_changed_json_string_with_a_lone_surrogate_still_prints(level):
    r = _redactor(level)
    text = '{"a":"\\ud800 /srv/Client/x"}'
    for out in (r(text), r.title(text), r.scrub_leftover_paths(text),
                report._diagnostic_for_export(text, r)):
        out.encode("utf-8")  # raises on a lone surrogate
        assert "Client" not in out, out
        assert out == '{"a":"\\ud800 path-1"}', out
    json.loads(r(text))


@pytest.mark.parametrize("level", LEVELS)
def test_fc6_w7_a_changed_json_string_is_ascii_escaped_and_an_unchanged_one_is_kept(level):
    r = _redactor(level)
    assert r('{"a":"café /srv/Client/x","b":"café"}') == (
        '{"a":"caf\\u00e9 path-1","b":"café"}'
    )
    assert r('{"b":"\\ud800","c":1}') == '{"b":"\\ud800","c":1}'


# --- W6-8: path forms the grammar missed -----------------------------------------------------------------

W68_PLAIN = [
    pytest.param("FILE:///srv/Client/deep/notes.md", "path-1", id="file_scheme_upper_case"),
    pytest.param("File:///srv/Client/deep/notes.md", "path-1", id="file_scheme_mixed_case"),
    pytest.param("file://localhost/srv/Client/deep/notes.md", "path-1", id="file_localhost"),
    pytest.param("FILE://LOCALHOST/srv/Client/deep/notes.md", "path-1", id="file_localhost_upper"),
    pytest.param("file:////srv/Client/deep/notes.md", "path-1", id="file_four_slashes"),
    pytest.param("see file:///srv/Client Secret/deep/notes.md now", f"see path-1{HIDDEN}",
                 id="file_scheme_space"),
    pytest.param("~bob/Client/deep/notes.md", "path-1", id="another_users_home"),
    pytest.param("see ~bob/Client Secret/deep/notes.md now", f"see path-1{HIDDEN}",
                 id="another_users_home_space"),
    pytest.param("cat ~bob.smith/Client/deep/notes.md", "cat path-1",
                 id="another_users_home_with_a_dot"),
]


@pytest.mark.parametrize("level,text,expected", _grid6(W68_PLAIN))
def test_fc6_w8_the_missing_path_forms_are_hidden(level, text, expected):
    r = _redactor(level)
    _assert_golden(r, text, expected)
    _clean(r, text)


W68_HOME = [
    pytest.param("$HOME/Client/deep/notes.md", "~/.../notes.md", id="home_variable"),
    pytest.param("${HOME}/Client/deep/notes.md", "~/.../notes.md", id="home_variable_braces"),
    pytest.param("cat $HOME/Client/deep/notes.md", "cat ~/.../notes.md",
                 id="home_variable_in_a_command"),
    pytest.param('cat "$HOME/Client/deep/notes.md"', 'cat "~/.../notes.md"',
                 id="home_variable_in_quotes"),
    pytest.param("PATH=$HOME/Client/deep/notes.md", "PATH=~/.../notes.md",
                 id="home_variable_after_an_assignment"),
    pytest.param("$HOME/Client Secret/deep/notes.md", f"path-1{HIDDEN}",
                 id="home_variable_space"),
    pytest.param("echo $HOME and ${HOME}", "echo $HOME and ${HOME}",
                 id="control_the_bare_variable_is_not_a_path"),
]


@pytest.mark.parametrize("level,text,expected", _grid6(W68_HOME))
def test_fc6_w8_a_home_variable_is_the_home_directory(level, text, expected):
    r = _redactor(level)
    _assert_golden(r, text, expected, sinks=("plain", "title", "json", "evidence", "leftover"))
    _clean(r, text, ("Client", "deep", "Secret"))


# --- W6-10: the survivors that were real test gaps ----------------------------------------------------


@pytest.mark.parametrize("level", LEVELS)
def test_fc6_w10_a_relative_registered_value_with_a_trailing_slash_matches_across_nfd_and_nfc(level):
    nfd = "Café Docs/Acme/"
    nfc = "Café Docs/Acme"
    r = _redactor(level, nfd)
    for text in (f"ls {nfc} now", f"ls {nfc}", f"ls {nfc}/ now", f"ls {nfd[:-1]} now"):
        for sink, out in _all_sinks(r, text).items():
            for word in ("Docs", "Acme", "Caf"):
                assert word not in out, (sink, word, out)
    # and the reverse spelling: an NFC registration, an NFD text
    r = _redactor(level, nfc + "/")
    out = r(f"ls {nfd[:-1]} now")
    assert "Docs" not in out and "Acme" not in out, out


# --- the rendered forms of a path on its own line (a path with prose after it is one opaque span) ----


def test_fc6_each_path_on_its_own_line_keeps_the_share_form():
    r = _redactor("share")
    text = "\n".join([
        "/home/user/proj/deep/notes.md",
        "/tmp/fix/aa/bb.md",
        "/srv/data/x",
        "/srv/data/x",
        "//srv/data/x",
        "~/proj/deep/notes.md",
    ])
    assert r(text) == "\n".join([
        "~/.../notes.md", "/tmp/fix/.../bb.md", "path-1", "path-1", "path-1", "~/.../notes.md",
    ])


def test_fc6_each_path_on_its_own_line_keeps_the_local_form():
    r = _redactor("local", repo_root="/repo")
    text = "\n".join([
        "/repo/src/m.py", "/repo", "/home/user/proj/deep/notes.md", "/opt/tool/x", "/opt/tool/x",
        "/repo/src/../../elsewhere/x.py",
    ])
    assert r(text) == "\n".join([
        "src/m.py", ".", "~/.../notes.md", "path-1", "path-1", "path-2",
    ])


@pytest.mark.parametrize("level", LEVELS)
def test_fc6_a_bare_root_and_two_spellings_of_one_path_are_one_pseudonym_on_separate_lines(level):
    r = _redactor(level)
    assert r("//srv/Zqa/x.py\n/srv/Zqa/x.py") == "path-1\npath-1"
    assert r("(cwd /)") == "(cwd path-2)"


@pytest.mark.parametrize("level", LEVELS)
def test_fc6_a_display_form_keeps_the_words_after_it_and_scanning_twice_changes_nothing(level):
    r = _redactor(level)
    for text in ("see ~/.../notes.md and more", "see ~/.../a.md; ok"):
        assert r(text) == text
        assert r(r(text)) == text
    if level == "share":  # a /tmp display form is a share form; at local a /tmp path is path-N
        text = "see /tmp/fix/.../b.md and more"
        assert r(text) == text and r(r(text)) == text
    else:
        assert r("see /tmp/fix/.../b.md and more") == "see path-1 and more"
    # an ordinary home path is not a display form
    assert "more" not in _redactor(level)("see ~/proj/notes.md and more")


@pytest.mark.parametrize("level", LEVELS)
def test_fc6_a_registered_lead_keeps_its_pseudonym_but_an_escaped_space_does_not(level):
    r = _redactor(level, "/srv/data/A", "clients/x.md")
    assert r("see clients/x.md was missing") == f"see path-2{HIDDEN}"
    assert r("see clients/x.md, then more") == f"see path-2{HIDDEN}"
    assert r("see /srv/data/A and more") == f"see path-1{HIDDEN}"
    # an escaped space belongs to a longer name: its own path, not the registered one
    out = r("see /srv/data/A\\ Client/x and more")
    assert out.startswith("see path-") and out.endswith(HIDDEN) and "path-1" not in out, out
    # a registered name that continues without a separator is a longer token: its own path
    out = r("see clients/x.md.bak and more")
    assert out.startswith("see path-") and "path-2" not in out, out


@pytest.mark.parametrize("level", LEVELS)
def test_fc6_a_bracket_span_after_a_path_on_its_line_is_hidden_with_the_rest_of_the_line(level):
    r = _redactor(level)
    for text in (
        'see /srv/Client {"a":1} Secret/deep now',
        'see /srv/Client [1] Secret/deep now',
        'GET /srv/Client {"a":"x"} {"b":2} Secret/deep',
        '{"a":1} /srv/Client {"b":2} Secret/deep',
    ):
        _clean(r, text)
    # a document before the path, or on another line, is still read value by value
    assert _redactor(level)('{"a":"/srv/x"} ok') == '{"a":"path-1"} ok'
    assert _redactor(level)('see /srv/x\n{"a":"/srv/y"}') == 'see path-1\n{"a":"path-2"}'


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("before", [
    pytest.param("see ~/Client", id="home_tilde"),
    pytest.param("see $HOME/Client", id="home_variable"),
    pytest.param("see \\/srv\\/Client", id="backslash_escaped_slashes"),
    pytest.param("see SECRETNAME", id="registered_value"),
])
def test_fc6_each_kind_of_path_start_before_a_bracket_span_keeps_the_span_with_the_line(
    level, before
):
    r = _redactor(level, "SECRETNAME")
    text = before + ' {"a":1} Zqsecret/deep now'
    out = r(text)
    assert "Zqsecret" not in out and "deep" not in out, out


@pytest.mark.parametrize("level", LEVELS)
def test_fc6_a_registered_lead_is_taken_only_from_a_text_in_nfc_form(level):
    nfc, nfd = "café/x.md", "café/x.md"
    r = _redactor(level, nfc)
    assert r(f"see {nfc} then more") == f"see path-1{HIDDEN}"
    out = _redactor(level, nfc)(f"see {nfd} then more")
    assert "x.md" not in out and "path-1" not in out and out.endswith(HIDDEN), out
