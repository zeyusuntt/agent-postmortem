"""Fix round 8 (W8-1 to W8-4, W8-8, W8-9) of field milestone F: the path-start grammar and the
lexer after the round 7 reviews (docs/DESIGN.md 5.1; .reviews/v0.3/F-dispositions.md, "Fix round 7
review" and "Codex round 7").

Every exact input of the sonnet round 7 review (F-W7-review.md findings 1 to 4) and of Codex round 7
(F-codex-r7.md findings 1 and 2) runs through the sinks the reviewers list, at share and at local.

Every fixture is synthetic. Private words are the ones a stranger must never read.
"""

from __future__ import annotations

import json

import pytest

from agent_postmortem import redact
from tests.test_fc_fix5_redact import (
    HIDDEN,
    LEVELS,
    _all_sinks,
    _assert_golden,
    _redactor,
)

PRIVATE8 = (
    "Zqa", "Zqb", "Zqc", "Zqd", "Bob", "Jim", "Client", "Secret", "Folder", "Acme", "Project",
    "Private", "notes.md", "x.txt", "index.js", "server",
)


def _grid8(rows):
    """Every row at both levels."""
    return [
        pytest.param(level, *row.values, id=f"{row.id}-{level}")
        for row in rows for level in LEVELS
    ]


def _clean8(r, text, private=PRIVATE8):
    for sink, out in _all_sinks(r, text).items():
        for word in private:
            assert word not in out, (sink, word, out)


# --- W8-1: an apostrophe inside a single-quoted path (sonnet 1) ---------------------------------------

W81 = [
    pytest.param(
        "ENOENT: no such file or directory, open '/srv/Zqa/Bob's Zqb/x.txt'",
        f"ENOENT: no such file or directory, open 'path-1{HIDDEN}'", id="node_open",
    ),
    pytest.param(
        "Error: Cannot find module '/srv/Zqa/Bob's Zqb/index.js'",
        f"Error: Cannot find module 'path-1{HIDDEN}'", id="node_module",
    ),
    pytest.param(
        "fatal: '/srv/Zqa/Bob's Zqb' is not a git repository",
        f"fatal: 'path-1{HIDDEN}' is not a git repository", id="git_fatal",
    ),
    pytest.param(
        "ENOENT: no such file or directory, open '/srv/Zqa/Bob's Jim's Zqb/x.txt'",
        f"ENOENT: no such file or directory, open 'path-1{HIDDEN}'", id="two_apostrophes",
    ),
]


@pytest.mark.parametrize("level,text,expected", _grid8(W81))
def test_fc8_w1_a_single_quoted_path_with_an_apostrophe_is_one_fragment(level, text, expected):
    r = _redactor(level)
    _assert_golden(r, text, expected)
    _clean8(r, text)


W81_CLEAN = [
    pytest.param('{"msg":"ENOENT: no such file or directory, open \'/srv/Bob\'s Zqb/x\'"}',
                 id="json_message"),
    pytest.param(
        "Command '['git', '-C', \"/srv/Bob's Zqb\", 'status']' returned non-zero exit status 128.",
        id="called_process_error_repr"),
    pytest.param("open('/srv/Zqa/Bob's Zqb/x')", id="python_open_call"),
    pytest.param("RuntimeError(\"open('/srv/Zqa/Bob's Zqb/x')\")", id="repr_of_an_error"),
]


@pytest.mark.parametrize("level,text", _grid8(W81_CLEAN))
def test_fc8_w1_the_other_message_shapes_of_the_review_hide_the_private_words(level, text):
    r = _redactor(level)
    _clean8(r, text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w1_an_in_word_apostrophe_never_closes_a_single_quoted_fragment(level):
    """The rule (F fix 8b, fail closed): inside a single-quoted fragment an in-word apostrophe is
    name text, never the closing quote; a fragment with no other closing quote runs to the end of
    its line (an excerpt truncated mid-path hides the rest). Controls: a quoted word pair stays
    prose, a name with a lone apostrophe keeps what follows its closing quote."""
    assert _redactor(level)("cat 'a'b 'c d'") == "cat 'a'b 'c d'"
    out = _redactor(level)("open '/srv/Zqa/x' and it's fine")
    assert out == "open 'path-1' and it's fine"
    assert _redactor(level)("it's 'the clients' files") == "it's 'the clients' files"
    # a quote on the next line is not "another quote on the line"
    out = _redactor(level)("open '/srv/Zqa/Bob's Zqb/x'\nsay 'hi' now")
    assert out == f"open 'path-1{HIDDEN}'\nsay 'hi' now", out
    # a quoted path whose closing quote was cut off (an excerpt truncated mid-path) and whose name
    # holds an in-word apostrophe hides to the end of its line: nothing after the apostrophe shows
    out = _redactor(level)("open '/srv/Zqa/Bob's Zqb")
    assert "Zqb" not in out and "Zqa" not in out, out
    assert out.startswith("open "), out
    # a quote on a LATER line neither closes the fragment nor is swallowed by it
    out = _redactor(level)("open '/srv/Zqa/Bob's Zqb\nsay 'hi' now")
    first, second = out.split("\n")
    assert "Zqb" not in first and "Zqa" not in first, out
    assert second == "say 'hi' now", out


# --- W8-2: a home variable or `~user/` anywhere a path start is searched (sonnet 2) ---------------------

W82 = [
    pytest.param('sh -c "cd $HOME/Zqa/Zqb"', id="sh_c_double_quoted"),
    pytest.param("sh -c 'cd $HOME/Zqa/Zqb && ls'", id="sh_c_single_quoted"),
    pytest.param('bash -lc "source $HOME/Zqa/Zqb/env.sh"', id="bash_lc_source"),
    pytest.param("python -c \"open('$HOME/Zqa/Zqb')\"", id="python_open"),
    pytest.param('echo "x $HOME/Zqa/Zqb/x"', id="echo_in_a_quote"),
    pytest.param('x="cat ~zed/Zqa/Zqb/x.md"', id="other_account_home_in_a_quote"),
    pytest.param('sh -c "cd ${HOME}/Zqa/Zqb"', id="braced_variable"),
    pytest.param("PATH=/usr/bin:$HOME/Zqa/Zqb", id="after_a_colon_in_an_assignment"),
]


@pytest.mark.parametrize("level,text", _grid8(W82))
def test_fc8_w2_a_home_anchor_after_a_non_prefix_character_is_a_path_start(level, text):
    r = _redactor(level)
    _clean8(r, text)
    # the plain call leaves the shell words around the quoted command, and hides the path
    assert "Zqa" not in r(text) and "Zqb" not in r(text), r(text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w2_the_prose_controls_of_the_home_anchors_stay_as_they_are(level):
    r = _redactor(level)
    for text in ("$HOMEWORK/x ok", "cost: $5/mo ok", "see $HOMES/x", "a$HOME/b", "x~y/z"):
        assert r(text) == text, text


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w2_a_home_anchor_at_a_word_start_keeps_its_old_form(level):
    r = _redactor(level)
    assert r("cd $HOME/Zqa/Zqb") == ("cd ~/.../Zqb" if level == "share" else r("cd $HOME/Zqa/Zqb"))
    assert "Zqa" not in r("cd $HOME/Zqa/Zqb")
    assert r('cd "$HOME/Zqa/Zqb"').count("Zqa") == 0


# --- W8-3: a terminal colour sequence in front of a path (sonnet 3) ------------------------------------

ESC = "\x1b"
W83 = [
    pytest.param(f"{ESC}[31m/srv/Zqa/Zqb.md{ESC}[0m failed", id="red_then_reset_and_prose"),
    pytest.param(f"{ESC}[1;34m/srv/Zqa Zqb/x.md{ESC}[0m", id="bold_blue_with_a_space"),
    pytest.param(f"ls: {ESC}[01;31m/srv/Zqa/Zqb{ESC}[m", id="ls_colour_after_a_word"),
    pytest.param(f"{ESC}[36mC:\\Zqa\\Zqb{ESC}[0m", id="windows_drive_in_colour"),
    pytest.param("[31m/srv/Zqa/Zqb.md[0m", id="esc_stripped"),
    pytest.param(f"{ESC}[32m~/Zqa/Zqb.md{ESC}[0m ok", id="home_path_in_colour"),
    pytest.param(f"{ESC}[32m$HOME/Zqa/Zqb/ok.md{ESC}[0m", id="home_variable_in_colour"),
]


@pytest.mark.parametrize("level,text", _grid8(W83))
def test_fc8_w3_a_colour_sequence_before_a_path_does_not_hide_the_start(level, text):
    r = _redactor(level)
    _clean8(r, text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w3_the_colour_sequences_themselves_are_kept(level):
    def shown(text):
        return _redactor(level)(text)

    assert shown(f"{ESC}[31m/srv/Zqa/Zqb.md{ESC}[0m") == f"{ESC}[31mpath-1{ESC}[0m"
    assert shown(f"{ESC}[1;34m/srv/Zqa/Zqb{ESC}[m") == f"{ESC}[1;34mpath-1{ESC}[m"
    # the ESC-stripped form is a sequence only where no word character precedes it: the leading
    # one is kept, a trailing one glued to the name is name text and is hidden with it
    assert shown("[31m/srv/Zqa/Zqb.md[0m") == "[31mpath-1"
    assert shown("[1;34m/srv/Zqa/Zqb [m") == "[1;34mpath-1 [m"
    assert shown(f"{ESC}[31m/srv/Zqa Zqb/x.md{ESC}[0m") == f"{ESC}[31mpath-1{HIDDEN}{ESC}[0m"
    # a colour sequence with `;` is one piece of the word, not an operator boundary
    assert shown(f"ls: {ESC}[01;31m/srv/Zqa/Zqb{ESC}[0m") == f"ls: {ESC}[01;31mpath-1{ESC}[0m"


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w3_a_bracket_sequence_that_is_not_followed_by_a_path_is_prose(level):
    r = _redactor(level)
    for text in ("array[0m/x", "x[31m/y", f"{ESC}[31mred{ESC}[0m", "[5m of rain]", "ok [0m now"):
        assert r(text) == text, text


# --- W8-4: a path after `@` (sonnet 4) -----------------------------------------------------------------

W84 = [
    pytest.param("curl -d @/srv/Zqa/Zqb.json http://x", id="curl_data"),
    pytest.param("curl -F file=@/srv/Zqa/Zqb.json http://x", id="curl_form"),
    pytest.param("curl -d @/srv/Zqa Zqb/x.json", id="curl_data_with_a_space"),
]


@pytest.mark.parametrize("level,text", _grid8(W84))
def test_fc8_w4_a_path_after_an_at_sign_is_a_path_start(level, text):
    r = _redactor(level)
    _clean8(r, text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w4_the_email_and_package_controls_behave_as_before(level):
    assert _redactor(level)("pkg @scope/pkg ok") == "pkg @scope/pkg ok"
    assert _redactor(level)("mail user@example.com ok") == "mail email:63a710 ok"
    assert _redactor(level)("mail user@host:/srv/x now") == f"mail path-1{HIDDEN}"
    # an email next to a path: the address is hashed, the path is hidden
    out = _redactor(level)("mail user@example.com /srv/Zqa/x")
    assert out == "mail email:63a710 path-1", out


# --- W8-8: Windows namespace paths, `$PWD`, a root path that starts with a space (Codex 1) ---------------

CODEX1 = [
    pytest.param("open \\\\?\\UNC\\server\\share\\Client Secret\\notes.md now",
                 f"open path-1{HIDDEN}", id="codex1_extended_unc"),
    pytest.param("open \\\\?\\C:\\Client Secret\\notes.md now", f"open path-1{HIDDEN}",
                 id="extended_drive"),
    pytest.param("open \\\\.\\C:\\Client Secret\\notes.md now", f"open path-1{HIDDEN}",
                 id="device_namespace"),
    pytest.param("open $PWD/Client Secret/deep/notes.md now", f"open path-1{HIDDEN}",
                 id="codex1_pwd"),
    pytest.param("open ${PWD}/Client Secret/deep/notes.md now", f"open path-1{HIDDEN}",
                 id="braced_pwd"),
    pytest.param("open $OLDPWD/Client Secret/deep/notes.md now", f"open path-1{HIDDEN}",
                 id="oldpwd"),
    pytest.param("open ${OLDPWD}/Client Secret/deep/notes.md now", f"open path-1{HIDDEN}",
                 id="braced_oldpwd"),
    pytest.param('cannot read "/ Client Folder/report.md" denied',
                 f'cannot read "path-1{HIDDEN}" denied', id="codex1_root_with_a_leading_space"),
    pytest.param('cannot read "/ Client Secret Folder/report.md" denied',
                 f'cannot read "path-1{HIDDEN}" denied', id="root_with_a_leading_space_two_words"),
    pytest.param('sh -c "cd $PWD/Client Secret/x"', f'sh -c "path-1{HIDDEN}"',
                 id="pwd_inside_a_quote"),
    pytest.param('cannot read "/ Client Name Inc Group Folder/report.md" denied',
                 f'cannot read "path-1{HIDDEN}" denied', id="root_with_a_leading_space_six_words"),
]


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w2_a_home_anchor_that_only_the_decoded_text_shows_is_a_start(level):
    """`\\u007e` is `~`: the raw text of this word has no anchor, only its decoded text does."""
    for text in ('echo "x \\u007e/Zqa/Zqb/y z"', 'echo "x \\u0024HOME/Zqa/Zqb/y z"'):
        r = _redactor(level)
        out = r(text)
        assert "Zqa" not in out and "Zqb" not in out, (text, out)


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w3_a_colour_sequence_that_only_the_decoded_text_shows_is_a_boundary(level):
    """`\\u001b` is ESC: the raw text of this word has no sequence, only its decoded text does."""
    text = 'echo "\\u001b[31m/srv/Zqa/Zqb y"'
    out = _redactor(level)(text)
    assert "Zqa" not in out and "Zqb" not in out, out


@pytest.mark.parametrize("level,text,expected", _grid8(CODEX1))
def test_fc8_w8_the_exact_review_inputs_are_path_starts(level, text, expected):
    r = _redactor(level)
    _assert_golden(r, text, expected)
    _clean8(r, text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w8_a_pwd_path_without_whitespace_is_an_opaque_path_never_a_repo_form(level):
    assert _redactor(level)("open $PWD/Zqa/Zqb.md now") == f"open path-1{HIDDEN}"
    assert _redactor(level)("open '$PWD/Zqa/Zqb.md' now") == "open 'path-1' now"
    assert _redactor(level)("cd $PWD/Zqa/Zqb") == "cd path-1"


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w8_the_prose_controls_of_the_new_anchors_stay_as_they_are(level):
    r = _redactor(level)
    for text in (
        "checkout / restore", "read / write", "TCP / UDP", "a / b", "$PWDX/x ok", "$PWDS/y",
        "price $PWD", "use / for the root", "the C: drive", "x ?\\y", "a \\\\b",
        "git checkout / restore / reset on these", "read / write access to us",
    ):
        assert r(text) == text, text


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w8_relative_names_stay_visible_by_the_contract_of_docs_design_5_1(level):
    """DESIGN 5.1 keeps relative names (an owner decision): `./`, `../` and `src/a.py` are not
    path starts, so command excerpts stay readable. The review's `./` row is a contract question
    put to the owner, not a change made in F."""
    r = _redactor(level)
    for text in (
        "open ./Client Secret/deep/notes.md now", "open ../Client Secret/x now",
        "open src/a.py now", "diff --git a/Zqa Zqb/x.md b/Zqa Zqb/x.md",
    ):
        assert r(text) == text, text


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w8_a_registered_root_style_value_still_wins(level):
    r = _redactor(level, "/ Client Folder/report.md")
    out = r("cannot read / Client Folder/report.md: denied")
    assert "Client" not in out and "Folder" not in out, out


# --- W8-9: a path before a JSON fragment on the same line (Codex 2) -------------------------------------

JSON_TAIL = '{"relative":"Private\\\\notes.md","status":"failed"}'
JSON_ARRAY = '["Private\\\\notes.md","b"]'
# the closing bracket at the very end of the line is a raw suffix (DESIGN 5.1), like a `)`
OBJECT_END = f"open path-1{HIDDEN}}}"
ARRAY_END = f"open path-1{HIDDEN}]"
CODEX2 = [
    pytest.param("open C:\\Clients\\Acme\\Project X " + JSON_TAIL, OBJECT_END,
                 id="codex2_drive_then_an_object"),
    pytest.param("open C:\\Clients\\Acme\\Project X " + JSON_ARRAY, ARRAY_END,
                 id="drive_then_an_array"),
    pytest.param("open \\\\host\\share\\Acme X " + JSON_TAIL, OBJECT_END,
                 id="unc_then_an_object"),
    pytest.param("open \\\\host\\share\\Acme X " + JSON_ARRAY, ARRAY_END,
                 id="unc_then_an_array"),
    pytest.param("open \\\\?\\UNC\\host\\share\\Acme X " + JSON_TAIL, OBJECT_END,
                 id="namespace_then_an_object"),
    pytest.param("open file://host/share/Acme X " + JSON_TAIL, OBJECT_END,
                 id="file_url_host_then_an_object"),
    pytest.param("open ~/Zqa/Acme X " + JSON_TAIL, OBJECT_END,
                 id="home_then_an_object"),
    pytest.param("open $HOME/Zqa/Acme X " + JSON_TAIL, OBJECT_END,
                 id="home_variable_then_an_object"),
    pytest.param("open $PWD/Zqa/Acme X " + JSON_TAIL, OBJECT_END,
                 id="pwd_then_an_object"),
]


@pytest.mark.parametrize("level,text,expected", _grid8(CODEX2))
def test_fc8_w9_a_json_fragment_after_a_path_start_stays_with_the_end_of_line_rule(
    level, text, expected,
):
    r = _redactor(level)
    assert r(text) == expected, r(text)
    _clean8(r, text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w9_a_decoded_start_in_a_closed_quote_ends_with_the_quote(level):
    """The decoded start is found (the JSON after it stays with the word scanner, not read value
    by value), and a path inside a closed quote ends with the quote, so the JSON after the quote
    is outside the span and keeps its relative name by the contract of DESIGN 5.1."""
    text = 'open "\\u002fsrv\\u002fZqa X" ' + JSON_TAIL
    out = _redactor(level)(text)
    assert out == f'open "path-1{HIDDEN}" ' + JSON_TAIL, out
    # unclosed, the quote takes the rest of the line with the JSON after it
    out = _redactor(level)('open "\\u002fsrv\\u002fZqa X ' + JSON_TAIL)
    assert "Zqa" not in out and "Private" not in out, out


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w9_a_registered_start_before_a_json_fragment_does_too(level):
    r = _redactor(level, "C:\\Clients\\Acme")
    out = r("open C:\\Clients\\Acme X " + JSON_TAIL)
    assert "Private" not in out and "Acme" not in out, out


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w9_a_document_before_a_path_or_on_another_line_is_still_read_value_by_value(level):
    r = _redactor(level)
    out = r('{"relative":"C:\\\\Zqa\\\\Zqb","status":"failed"} done')
    assert json.loads(out.rsplit("}", 1)[0] + "}")["status"] == "failed", out
    assert "Zqa" not in out and out.endswith(" done"), out
    out = _redactor(level)("open C:\\Zqa\\Zqb X\n" + JSON_TAIL)
    first, second = out.split("\n")
    assert first == f"open path-1{HIDDEN}" and "Private" in second, out


class _StartCounter:
    """The characters handed to the start grammar: a deterministic operation count."""

    def __init__(self, monkeypatch):
        self.chars = 0
        original = redact._abs_starts
        counter = self

        def counted(text):
            counter.chars += len(text)
            return original(text)

        monkeypatch.setattr(redact, "_abs_starts", counted)


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("head", ["C:\\Clients\\X ", "\\\\h\\s\\x ", "/srv/x ", "$HOME/x ", ""])
def test_fc8_w9_many_json_spans_after_a_start_cost_a_linear_scan(level, head, monkeypatch):
    r = _redactor(level)
    text = head + "[1] " * 8000
    counter = _StartCounter(monkeypatch)
    r(text)
    assert counter.chars <= 4 * len(text), (counter.chars, len(text))


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w9_many_json_spans_with_words_between_cost_a_linear_scan(level, monkeypatch):
    r = _redactor(level)
    text = "ab " + "[1] ab " * 8000
    counter = _StartCounter(monkeypatch)
    r(text)
    assert counter.chars <= 4 * len(text), (counter.chars, len(text))
