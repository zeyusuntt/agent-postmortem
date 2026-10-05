"""Fix round 11 (W11-1 to W11-3) of field milestone F: the strict start rule after the round 10
reviews (docs/DESIGN.md 5.1; .reviews/v0.3/F-dispositions.md, "Fix round 10 review";
F-W10-review.md and F-codex-r10.md).

Every exact input of Codex round 10 (finding 1) and of the sonnet round 10 review (findings 1, 2)
runs through the sinks the reviewers list, at share and at local.

Every fixture is synthetic. Private words are the ones a stranger must never read.
"""

from __future__ import annotations

import pytest

from tests.test_fc_fix5_redact import LEVELS, _all_sinks, _redactor

PRIVATE11 = ("Zqa", "Zqb", "app.py", "app", "James", "Clients", "Secret", "deep", "Acme", "Notes", "private.md")


def _clean11(r, text, private=PRIVATE11):
    for sink, out in _all_sinks(r, text).items():
        for word in private:
            assert word not in out, (sink, word, out)


def _rows(rows):
    return [pytest.param(level, text, id=f"{rid}-{level}") for rid, text in rows for level in LEVELS]


# --- W11-1: the strict test ignores the trailing `.` and `,` of the match (Codex 1) ----------------

W111 = [
    ("comma_extension", "读取/Zqa.md, failed"),
    ("period_extension", "读取/Zqa.md. failed"),
    ("comma_then_cjk", "读取/app.py, 然后重试"),
    ("decoded_comma", '读"\\u002fZqa.md," failed'),
    ("thai_comma", "อ่าน/Zqa.md, failed"),
]


@pytest.mark.parametrize("level,text", _rows(W111))
def test_fc11_w1_trailing_sentence_punctuation_does_not_hide_the_extension(level, text):
    _clean11(_redactor(level), text)


# --- W11-2: punctuation of the class is an ordinary boundary; a slash after the value is a segment -

W112_BOUNDARY = [
    ("fullwidth_colon", "错误：/Zqa"),
    ("corner_bracket", "「/Zqa」不存在"),
    ("fullwidth_colon_spaced_name", "读取失败：/Zqa Zqb/x.md"),  # U+3000 itself: test_fc_fix12
]
W112_SEGMENT = [
    ("unreadable_segment", "读取/srv/(Zqa)/x.md"),
    ("trailing_slash", "读取/Zqa/"),
]


@pytest.mark.parametrize("level,text", _rows(W112_BOUNDARY + W112_SEGMENT))
def test_fc11_w2_punctuation_is_a_boundary_and_a_following_slash_is_a_segment(level, text):
    _clean11(_redactor(level), text)


W11_PROSE = [
    "测试通过/失败 共 12 项，耗时 340 秒",
    "客户端/服务端 都已重启，退出码 0",
    "成功/失败比例",
    "读/写",
    "是/否",
    "和/或",
    "通过/失败。",
    "通过/失败, 共 3 项",
    "是/否.",
    "and/or, read-/write.",
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text", W11_PROSE)
def test_fc11_w1_w2_the_prose_controls_stay_unchanged(level, text):
    assert _redactor(level)(text) == text


@pytest.mark.parametrize("level", LEVELS)
def test_fc11_w2_the_decoded_text_of_an_escaped_word_follows_the_same_rule(level):
    """The slash after the value is read in the decoded text too: no literal slash in the raw
    text shows it."""
    _clean11(_redactor(level), '读"\\u002fZqa\\u002f" ok')
    prose = '读"\\u002f写" ok'
    assert _redactor(level)(prose) == prose


# --- W11-3: test gaps (sonnet 2, Codex M15) ---------------------------------------------------------

W113_REGISTERED = [
    ("typographic_registered_only", "open ‘Clients’ Secret/deep/notes.md’"),
    ("typographic_cut_off", "open ‘Clients’ Secret/deep/notes.md"),
    ("registered_twice", "open 'Clients' Secret/deep/notes.md' and 'Clients' Secret/deep/notes.md'"),
    ("registered_before_the_opener", "see deep/notes.md then open 'Clients' Secret/deep/notes.md'"),
    ("registered_in_an_earlier_quote", '"deep/notes.md" x open \'Clients\' Secret/deep/notes.md\''),
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("rid,text", W113_REGISTERED, ids=[r for r, _ in W113_REGISTERED])
def test_fc11_w3_a_registered_value_alone_makes_a_fragment_path_bearing(level, rid, text):
    _clean11(_redactor(level, "deep/notes.md"), text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc11_w3_a_typographic_fragment_does_not_close_at_its_first_closing_quote(level):
    text = "‘/srv/Zqa’ Zqb/x.md’ and more"
    r = _redactor(level)
    _clean11(r, text)
    assert r(text).endswith(" and more"), r(text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc11_w3_a_tab_after_the_quote_is_read_like_a_space(level):
    _clean11(_redactor(level), "open '/srv/James'\tNotes/private.md'")
    _clean11(_redactor(level), "open '/srv/James'\tNotes/private.md")  # the closing quote cut off
    _clean11(_redactor(level), "open '/srv/James' \t Notes/private.md")


@pytest.mark.parametrize("level", LEVELS)
def test_fc11_w3_a_bare_dot_token_is_no_extension_and_a_word_with_a_final_dot_is_none(level):
    r = _redactor(level)
    assert r("'/srv/x' . done") == "'path-1' . done"
    assert r("'/srv/x' end. done") == "'path-1' end. done"


@pytest.mark.parametrize("text,expected", [
    ("~/Documents///Acme/PUBLIC.md", "~/.../PUBLIC.md"),
    ("/tmp/b///Acme/x.js", "/tmp/b/.../x.js"),
])
def test_fc11_w3_a_triple_slash_collapses_at_share(text, expected):
    r = _redactor("share")
    assert r(text) == expected, r(text)
    for sink, out in _all_sinks(r, text).items():
        assert "Acme" not in out and "Documents" not in out, (sink, out)


@pytest.mark.parametrize("level", LEVELS)
def test_fc11_w4_w5_the_recorded_limits_are_pinned(level):
    """DESIGN 5.1 (F fix 11, W11-4, W11-5): a registered value makes the fragment path-bearing
    to the last quote of its line, so an earlier quoted word is hidden with it; and rule (b)
    hides the rest of the line after a path-bearing quote."""
    r = _redactor(level, "deep/notes.md")
    assert r("x 'a b' then 'Zqa Zqb/deep/notes.md' y") == "x 'path-2 [text hidden]' y"
    assert _redactor(level)("cp '/srv/a/x.md' backup/x.md && echo done") == "cp 'path-1 [text hidden]"
