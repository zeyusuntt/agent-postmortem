"""Fix round 10 (W10-1 to W10-6) of field milestone F: the path-start grammar after the round 9
reviews (docs/DESIGN.md 5.1; .reviews/v0.3/F-dispositions.md, "Fix round 9 review";
F-W9-review.md and F-codex-r9.md).

Every exact input of the sonnet round 9 review (findings 1, 2, 4) and of Codex round 9
(findings 1 to 3) runs through the sinks the reviewers list, at share and at local.

Every fixture is synthetic. Private words are the ones a stranger must never read.
"""

from __future__ import annotations

import unicodedata

import pytest

from agent_postmortem import redact
from tests.test_fc_fix5_redact import (
    HIDDEN,
    LEVELS,
    _all_sinks,
    _redactor,
)

PRIVATE10 = (
    "Zqa", "Zqb", "Zr9a", "Zr9b", "Zr9c", "James", "Notes", "Clients", "Files", "Secret",
    "private.md", "deep", "Acme",
)


def _grid10(rows):
    """Every row at both levels."""
    return [
        pytest.param(level, *row.values, id=f"{row.id}-{level}", marks=row.marks)
        for row in rows for level in LEVELS
    ]


def _clean10(r, text, private=PRIVATE10):
    for sink, out in _all_sinks(r, text).items():
        for word in private:
            assert word not in out, (sink, word, out)


# --- W10-1: scripts written without spaces are a boundary (Codex 1) ------------------------------

W101 = [
    pytest.param("อ่าน/srv/Zr9a Zr9b/Zr9c.mdไม่ได้", id="codex1_thai_spaced_path"),
    pytest.param("អាន/srv/Zr9a Zr9b/Zr9c.mdមិនបាន", id="codex1_khmer_spaced_path"),
    pytest.param("ອ່ານ/srv/Zr9a/Zr9b.md", id="lao_then_a_path"),
    pytest.param("ཀཁ/srv/Zr9a/Zr9b.md", id="tibetan_then_a_path"),
    pytest.param("ဖတ/srv/Zr9a/Zr9b.md", id="myanmar_then_a_path"),
    pytest.param("\ua9e0/srv/Zr9a/Zr9b.md", id="myanmar_extended_b_then_a_path"),
    pytest.param("\uaa60/srv/Zr9a/Zr9b.md", id="myanmar_extended_a_then_a_path"),
    pytest.param("អាន/srv/Zr9a/Zr9b.md", id="khmer_then_a_path"),
    pytest.param("อ่าน~/Zr9a/Zr9b.md", id="thai_then_a_tilde_path"),
    pytest.param("อ่าน$HOME/Zr9a/Zr9b.md", id="thai_then_a_home_variable"),
    pytest.param("อ่านC:\\Zr9a\\Zr9b.md", id="thai_then_a_drive_path"),
    pytest.param("อ่าน\\\\server\\Zr9a\\Zr9b", id="thai_then_a_unc_path"),
    pytest.param("อ่าน$PWD/Zr9a/Zr9b.md", id="thai_then_a_working_directory_path"),
    pytest.param("อ่าน//srv/Zr9a/Zr9b.md", id="thai_then_a_double_slash_path"),
]


@pytest.mark.parametrize("level,text", _grid10(W101))
def test_fc10_w1_a_path_glued_to_an_unspaced_script_is_a_path_start(level, text):
    _clean10(_redactor(level), text)


W101_LIMIT = [
    pytest.param("читать/srv/Zr9a/Zr9c.md", id="cyrillic_limit"),
    pytest.param("اقرأ/srv/Zr9a/Zr9c.md", id="arabic_limit"),
    pytest.param("read/srv/Zr9a/Zr9c.md", id="latin_limit"),
]


@pytest.mark.parametrize("level,text", _grid10(W101_LIMIT))
def test_fc10_w1_a_script_written_with_spaces_is_a_recorded_limit(level, text):
    """Recorded limit (DESIGN 5.1): a word glued to a path in a script that has spaces between
    words is the same construction as `read/srv/x`, which the grammar reads as prose so that
    relative names and `and/or` stay visible. The text prints raw by design."""
    assert _redactor(level)(text) == text


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text", ["naïve/simple ok", "Zürich/Bern ok", "да/нет ok"])
def test_fc10_w1_controls_with_a_script_that_has_spaces_stay_prose(level, text):
    assert _redactor(level)(text) == text


# --- W10-2: after an unspaced-script character a start needs two segments or an extension ----------

W102_PROSE = [
    "测试通过/失败 共 12 项，耗时 340 秒",
    "客户端/服务端 都已重启，退出码 0",
    "成功/失败比例",
    "读/写",
    "是/否",
    "和/或",
    "和 / 或 ok",
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text", W102_PROSE[:6])
def test_fc10_w2_a_slash_between_two_cjk_words_is_prose(level, text):
    assert _redactor(level)(text) == text


@pytest.mark.parametrize("level", LEVELS)
def test_fc10_w2_a_slash_with_spaces_was_always_prose(level):
    assert _redactor(level)(W102_PROSE[-1]) == W102_PROSE[-1]


W102_HIDE = [
    pytest.param("无法读取" + "/Us" + "ers/zed/Zqa/Zqb.md", id="home_path"),
    pytest.param("请读取/srv/Zqa Zqb/x.md失败", id="spaced_path"),
    pytest.param("读取/srv/Zqa.md", id="two_segments_with_an_extension"),
    pytest.param("读取/Zqa.md", id="one_segment_with_an_extension"),
    pytest.param("อ่าน/srv/Zqa/b", id="thai_two_segments"),
    pytest.param("读取//srv/Zqa/Zqb", id="double_slash_two_segments"),
    pytest.param("请读取$HOME/Zqa", id="explicit_anchor_one_segment_home"),
    pytest.param("请读取C:\\Zqa", id="explicit_anchor_one_segment_drive"),
]


@pytest.mark.parametrize("level,text", _grid10(W102_HIDE))
def test_fc10_w2_two_segments_an_extension_or_an_anchor_still_hide(level, text):
    _clean10(_redactor(level), text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc10_w2_the_decoded_text_of_an_escaped_word_follows_the_same_rule(level):
    r = _redactor(level)
    # an escaped word is judged on its decoded text: one segment after CJK stays prose, two hide
    prose = '读"\\u002f写" ok'
    assert "Zqa" not in r("请读取\"\\u002fsrv\\u002fZqa\\u002fZqb.md\"")
    assert r(prose) == prose


# one representative character of every range of the class: with two segments the path hides, with
# one segment and no extension it is prose (the strict rule), also for a character that is not a
# word character (a Khmer symbol is a boundary already, only the strict rule shows it)
RANGE_CHARS = [
    ("thai", "\u0e01"), ("lao", "\u0e81"), ("tibetan", "\u0f40"), ("myanmar", "\u1000"),
    ("hangul_jamo", "\u1100"), ("khmer", "\u1780"), ("khmer_symbols", "\u19e0"),
    ("cjk", "\u4e00"), ("hangul_jamo_extended_a", "\ua960"), ("myanmar_extended_b", "\ua9e0"),
    ("myanmar_extended_a", "\uaa60"), ("hangul_syllables", "\uac00"),
    ("cjk_compatibility", "\uf900"), ("fullwidth", "\uff21"), ("supplementary", "\U00020000"),
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("name,char", RANGE_CHARS, ids=[n for n, _ in RANGE_CHARS])
def test_fc10_w1_every_range_of_the_class_is_a_boundary_with_the_strict_rule(level, name, char):
    _clean10(_redactor(level), char + "/srv/Zr9a/Zr9b.md")
    if name == "khmer_symbols":
        # F fix 11 (W11-2): a character of the class that is not a word character is an
        # ordinary boundary, the strict rule does not apply, one segment hides
        _clean10(_redactor(level), char + "/Zr9a ok")
    else:
        assert _redactor(level)(char + "/Zr9a ok") == char + "/Zr9a ok"


@pytest.mark.parametrize("level", LEVELS)
def test_fc10_w2_the_pinned_cost_row(level):
    """The cost (DESIGN 5.1): three parts after a CJK character are two segments, so a list
    of single characters joined by slashes is a start."""
    assert _redactor(level)("增/删/改/查 四个接口") == f"path-1{HIDDEN}"


# --- W10-3: a path-bearing single-quoted fragment (sonnet 1, Codex 2, Codex 3) ----------------------

W103 = [
    pytest.param("cannot access ‘/srv/Clients’ Files/Zqa.md’", id="sonnet1_typographic_plural"),
    pytest.param("rm: cannot remove ‘/srv/Zqa’ Zqb/x’: No such file", id="typographic_rm"),
    pytest.param("ls: ‘/srv/Zqa’ Zqb’s/x’", id="typographic_possessive_inside"),
    pytest.param("ENOENT: open '/srv/James' Notes/private.md", id="codex3_cut_off_separator"),
    pytest.param("open '/srv/James' Notes/private.md", id="fix9_limit_row_now_hidden"),
    pytest.param("open '/srv/x/James' Notes.md", id="cut_off_extension"),
    pytest.param("open '/srv/x/James' Notes\\Zqa", id="cut_off_backslash_token"),
    pytest.param("ENOENT: open ‘/srv/James’ Notes/private.md", id="typographic_cut_off_separator"),
    pytest.param("open ‘/srv/James’ Notes/private.md", id="typographic_cut_off_separator_short"),
    pytest.param("open ‘/srv/x/James’ Notes.md", id="typographic_cut_off_extension"),
]


@pytest.mark.parametrize("level,text", _grid10(W103))
def test_fc10_w3_a_possessive_in_a_path_bearing_fragment_never_closes_it(level, text):
    _clean10(_redactor(level), text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc10_w3_the_controls_keep_their_words(level):
    assert "does not appear to be a git repository" in _redactor(level)(
        "fatal: '/srv/x' does not appear to be a git repository")
    assert _redactor(level)("cp: cannot stat '/srv/x': No such file or directory").endswith(
        ": No such file or directory")
    assert _redactor(level)("echo '/' and '/srv/Zqa'") == "echo 'path-1' and 'path-2'"
    assert _redactor(level)("it's 'README' now") == "it's 'README' now"
    assert "and ‘y z’" in _redactor(level)("‘/srv/x’ and ‘y z’")
    # a quote that does not follow a word character is never read as a possessive
    assert _redactor(level)("cat '/srv/x/' b/c now") == "cat 'path-1' b/c now"
    # the token after the quote is on the same line: spaces then a newline is no token
    assert _redactor(level)("open '/srv/x/James' \nsrc/b now") == "open 'path-1' \nsrc/b now"


@pytest.mark.parametrize("level", LEVELS)
def test_fc10_w3_the_pinned_limit_row(level):
    """Recorded limit (DESIGN 5.1): the token after the cut-off possessive holds no separator
    and no extension, so the quote is read as the closing one and `Notes` shows."""
    assert _redactor(level)("open '/srv/x/James' Notes") == "open 'path-1' Notes"


W103_REGISTERED = [
    pytest.param("open 'Clients' Secret/deep/notes.md'", "deep/notes.md", id="codex2_exact_row"),
    pytest.param("open 'Clients' Secret deep/notes.md'", "deep/notes.md", id="a_further_space"),
    pytest.param("open 'Clients' Secret/deep/notes.md", "deep/notes.md", id="no_closing_quote"),
    pytest.param(
        "open 'Clients' Secret/de" + "e" + "\u0301" + "p/notes.md'",
        unicodedata.normalize("NFC", "Secret/de" + "e" + "\u0301" + "p/notes.md"),
        id="nfd_spelling_of_a_registered_value",
    ),
]


@pytest.mark.parametrize("level,text,value", _grid10(W103_REGISTERED))
def test_fc10_w3_a_registered_value_after_the_opener_makes_the_fragment_path_bearing(
    level, text, value,
):
    r = _redactor(level, value)
    _clean10(r, text, ("Clients", "Secret", "deep", "notes.md"))


@pytest.mark.parametrize("level", LEVELS)
def test_fc10_w3_without_a_registered_value_the_same_fragment_is_prose(level):
    r = _redactor(level)
    for text in ("open 'Clients' Secret/deep/notes.md'", "it's 'README' now"):
        assert r(text) == text, text


@pytest.mark.parametrize("level", LEVELS)
def test_fc10_w3_the_registered_check_reads_the_decoded_line(level):
    """The line holds a backslash: the registered value `Zqb's/deep.md` is spelled with an
    escaped quote, so only the decoded line shows it."""
    r = _redactor(level, "Zqb's/deep.md")
    _clean10(r, "open 'Clients' Zqb\\'s/deep.md'", ("Clients", "Zqb", "deep"))


class _CountingLookup:
    """Counts every probe of the registered-value index."""

    def __init__(self, monkeypatch, limit):
        self.calls = 0
        original = redact._PathLookup.longest_at

        def counted(this, text, at):
            self.calls += 1
            assert self.calls <= limit, f"more than {limit} lookup probes"  # fail fast
            return original(this, text, at)

        monkeypatch.setattr(redact._PathLookup, "longest_at", counted)


@pytest.mark.parametrize("level", LEVELS)
def test_fc10_w3_the_registered_check_is_bounded_on_a_line_of_many_fragments(level, monkeypatch):
    """10,000 single-quoted fragments and 50 registered values: the check runs once per line,
    so the number of lookup probes is a small multiple of the line length, not its square."""
    values = [f"a/b{n}.md" for n in range(50)]
    r = _redactor(level, *values)
    line = "'a' " * 10_000
    counter = _CountingLookup(monkeypatch, 6 * len(line))
    r(line)
    assert counter.calls <= 6 * len(line), counter.calls


# --- W10-4: a doubled slash inside a home or /tmp path (sonnet 2) ----------------------------------

W104 = [
    pytest.param("/home/user/Documents//Acme/plan.md", "~/.../plan.md", id="doubled_after_documents"),
    pytest.param("/home/user/Documents/Acme//plan.md", "~/.../plan.md", id="doubled_before_basename"),
    pytest.param("~/Documents//Acme/plan.md", "~/.../plan.md", id="tilde_doubled"),
    pytest.param("$HOME/Documents//Acme/plan.md", "~/.../plan.md", id="home_variable_doubled"),
    pytest.param("/tmp/build//Acme/out.js", "/tmp/build/.../out.js", id="tmp_doubled"),
    pytest.param("/tmp/a/b/c//Zqa/Zqb", "/tmp/a/.../Zqb", id="tmp_deep_doubled"),
]


@pytest.mark.parametrize("text,expected", W104)
def test_fc10_w4_a_doubled_slash_collapses_at_share(text, expected):
    r = _redactor("share")
    assert r(text) == expected, r(text)
    for sink, out in _all_sinks(r, text).items():
        for word in ("Acme", "Zqa", "Documents"):
            assert word not in out, (sink, word, out)


@pytest.mark.parametrize("text,expected", W104)
def test_fc10_w4_local_rows_keep_hiding_the_private_segments(text, expected):
    r = _redactor("local")
    out = r(text)
    assert "Acme" not in out and "Zqa" not in out, out


def test_fc10_w4_a_structural_form_with_an_extra_segment_falls_back_to_path_n(monkeypatch):
    """The shape check: with the slash collapse a no-op the structural form of the doubled
    token is not one of the contract's shapes, so the token is its own path-N."""
    monkeypatch.setattr(redact, "_collapse_slash_runs", lambda token: token)
    r = _redactor("share")
    assert r._render_path_text("/home/user/Documents//Acme/plan.md", False) == "path-1"
    assert r._render_path_text("/tmp/build//Acme/out.js", False) == "path-2"


@pytest.mark.parametrize("token,shape", [
    ("~", True), ("~/name", True), ("~/.../name", True), ("~/", True), ("/tmp", True),
    ("/tmp/first", True), ("/tmp/first/name", True), ("/tmp/first/.../name", True),
    ("/private/tmp/first/.../name", True), ("/private/tmp/", True),
    ("~/Documents//Acme/plan.md", False), ("/tmp/a/b/c", False), ("/tmp/a/.../b/c", False),
    ("~/.../a/b", False), ("/var/x", False),
])
def test_fc10_w4_the_shape_check_accepts_only_the_contract_shapes(token, shape):
    assert (redact._STRUCTURAL_SHAPE_RE.fullmatch(token) is not None) is shape


# --- W10-5: the raw-suffix search window (sonnet minor 3) ------------------------------------------


class _WindowedSuffix:
    """Wraps `_SUFFIX_RE` and records how many characters each `search` may scan."""

    def __init__(self, real):
        self.real = real
        self.scanned: list[int] = []

    def search(self, string, pos=0, *args):
        self.scanned.append(len(string) - pos)
        # fail at once rather than run a quadratic search on 100,000 characters
        assert len(string) - pos <= 512, len(string) - pos
        return self.real.search(string, pos, *args)

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.mark.parametrize("level", LEVELS)
def test_fc10_w5_a_long_run_of_suffix_characters_is_searched_in_a_bounded_window(
    level, monkeypatch,
):
    wrapper = _WindowedSuffix(redact._SUFFIX_RE)
    monkeypatch.setattr(redact, "_SUFFIX_RE", wrapper)
    r = _redactor(level)
    text = "cat /srv/Zqa/Zqb.md" + "." * 100_000 + "x"
    out = r(text)
    assert "Zqa" not in out and "Zqb" not in out, out
    assert wrapper.scanned and max(wrapper.scanned) <= 512, max(wrapper.scanned)


@pytest.mark.parametrize("level", LEVELS)
def test_fc10_w5_a_600_character_run_of_trailing_spaces_is_hidden(level):
    out = _redactor(level)("cat /srv/Zqa/Zqb.md" + " " * 600)
    assert "[text hidden]" in out, out
    assert "Zqa" not in out and "Zqb" not in out, out


@pytest.mark.parametrize("level", LEVELS)
def test_fc10_w5_a_short_suffix_is_still_kept_raw(level):
    assert _redactor(level)("cat /srv/Zqa/Zqb.md.") == "cat path-1."
    assert _redactor(level)("see /srv/Zqa/Zqb.md:12:3)") == "see path-1:12:3)"
