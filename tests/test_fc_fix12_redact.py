"""Fix round 12 (W12-1, W12-2) of field milestone F: the strict start rule after the round 11
reviews (docs/DESIGN.md 5.1; .reviews/v0.3/F-dispositions.md, "Fix round 11 review";
F-W11-review.md and F-codex-r11.md).

W12-1: a combining mark of the unspaced class (a Thai, Lao or Myanmar vowel, tone or asat mark)
ends a word like a letter does, so a slash between two such words stays prose. W12-2: rows for
the boundary classes and suffix runs the round 11 reviewers found unpinned.

Every fixture is synthetic. Private words are the ones a stranger must never read.
"""

from __future__ import annotations

import pytest

from tests.test_fc_fix5_redact import LEVELS, _all_sinks, _redactor

PRIVATE12 = ("Zqa", "Zqb")


def _clean12(r, text):
    for sink, out in _all_sinks(r, text).items():
        for word in PRIVATE12:
            assert word not in out, (sink, word, out)


def _rows(rows):
    return [pytest.param(level, text, id=f"{rid}-{level}") for rid, text in rows for level in LEVELS]


# --- W12-1: a combining mark ends a word of the unspaced class (Codex r11 finding 1) --------------

W121_PROSE = [
    ("thai_yes_no", "ใช่/ไม่ใช่"),  # U+0E48 before the slash
    ("thai_good_bad", "ดี/ไม่ดี"),  # U+0E35
    ("lao_yes_no", "ບໍ່/ແມ່ນ"),  # U+0EC8
    ("myanmar_yes_no", "ဟုတ်/မဟုတ်"),  # U+103A
]


@pytest.mark.parametrize("level,text", _rows(W121_PROSE))
def test_fc12_w1_a_slash_between_words_that_end_in_a_mark_stays_prose(level, text):
    assert _redactor(level)(text) == text


@pytest.mark.parametrize("level", LEVELS)
def test_fc12_w1_a_spacing_mark_and_the_decoded_spelling_follow_the_same_rule(level):
    """Codex r12: U+1038 MYANMAR SIGN VISARGA is category Mc, not Mn; the decoded text of an
    escaped word reaches the same helper."""
    r = _redactor(level)
    assert r("စား/မစား") == "စား/မစား"
    decoded = '"\\u1005\\u102c\\u1038\\u002f\\u1019\\u1005\\u102c\\u1038"'
    assert r(decoded) == decoded


@pytest.mark.parametrize("level", LEVELS)
def test_fc12_w1_a_mark_outside_the_class_is_an_ordinary_boundary(level):
    """Codex r12: the mark rule holds only inside the unspaced class. An Arabic word ending in
    U+064F is no word character of the class, so the slash after it starts a path."""
    r = _redactor(level)
    _clean12(r, "تعذّرُ/Zqa")
    _clean12(r, '"\\u062a\\u0639\\u0630\\u0651\\u0631\\u064f\\u002fZqa"')


W121_PATHS = [
    ("thai_two_segments", "ดี/Zqa/x.md"),
    ("thai_extension_comma", "ใช่/Zqa.md, ok"),
    ("myanmar_following_slash", "ဟုတ်/Zqa/"),
    ("lao_extension", "ບໍ່/Zqa.md"),
]


@pytest.mark.parametrize("level,text", _rows(W121_PATHS))
def test_fc12_w1_a_path_after_a_word_that_ends_in_a_mark_still_hides(level, text):
    _clean12(_redactor(level), text)


# --- W12-2: rows the round 11 reviewers found unpinned (Codex r11 finding 2, sonnet r11 finding 4)

W122_HIDE = [
    ("period_run", "读取/Zqa.md... failed"),
    ("comma_run", "读取/Zqa.md,,, failed"),
    ("mixed_run", "读取/Zqa.md.,., failed"),
    ("decoded_comma_run", '读"\\u002fZqa.md,,," failed'),
    ("ideographic_space", "读取　/Zqa"),
    ("decoded_ideographic_space", '读"\\u3000\\u002fZqa" ok'),
    ("path_at_index_0_before_cjk_end", "/Zqa 是"),
    ("cjk_name_extension", "读取/报告Zqa.md"),
    ("digit_before_dot", "读取/Zqa1.md"),
    ("underscore_before_dot", "读取/Zqa_1.md"),
    ("long_extension", "读取/Zqa.markdown"),
    ("equals_is_no_strict_context", "--root=/Zqa"),
]


@pytest.mark.parametrize("level,text", _rows(W122_HIDE))
def test_fc12_w2_the_unpinned_rows_hide(level, text):
    _clean12(_redactor(level), text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc12_w2_the_recorded_one_segment_limits_are_pinned(level):
    """docs/DESIGN.md 5.1, recorded in F fix 12: a one-segment value after an unspaced word whose
    name goes on past a shell operator, or past a path-alphabet character after its extension,
    is read by the strict rule as prose (sonnet r11 finding 1, CONTRIVED: a root-level name with
    `(` or `&` glued to an unspaced word)."""
    r = _redactor(level)
    assert r("读取/Zqa&Zqb/x.md") == "读取/Zqa&Zqb/x.md"
    assert r("读/Zqa.md=1") == "读/Zqa.md=1"
