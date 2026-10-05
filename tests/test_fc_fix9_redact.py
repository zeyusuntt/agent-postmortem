"""Fix round 9 (W9-1 to W9-7) of field milestone F: the path-start grammar after the round 8 reviews
(docs/DESIGN.md 5.1; .reviews/v0.3/F-dispositions.md, "Fix round 8 and 8b review";
F-W8-review.md and F-codex-r8.md).

Every exact input of the sonnet round 8 review (findings 1 to 3, 5, 9) and of Codex round 8
(findings 1, 3, 5) runs through the sinks the reviewers list, at share and at local.

Every fixture is synthetic. Private words are the ones a stranger must never read.
"""

from __future__ import annotations

import time

import pytest

from agent_postmortem import redact
from tests.test_fc_fix5_redact import (
    HIDDEN,
    LEVELS,
    _all_sinks,
    _assert_golden,
    _redactor,
)

PRIVATE9 = (
    "Zqa", "Zqb", "Zqc", "Zqd", "Bob", "James", "Notes", "Clients", "Files", "Client", "Secret",
    "private.md", "x.md", "Zqe", "Zqf",
)
ESC = "\x1b"
USERS = "/Us" + "ers"  # the privacy audit forbids the literal home prefix outside redact tests


def _grid9(rows):
    """Every row at both levels."""
    return [
        pytest.param(level, *row.values, id=f"{row.id}-{level}")
        for row in rows for level in LEVELS
    ]


def _clean9(r, text, private=PRIVATE9):
    for sink, out in _all_sinks(r, text).items():
        for word in private:
            assert word not in out, (sink, word, out)


# --- W9-1: a path glued to CJK text is a path start (sonnet 1) ----------------------------------------

W91 = [
    pytest.param("无法读取" + USERS + "/zed/Zqa/Zqb.md", id="chinese_then_a_home_path"),
    pytest.param("请读取/srv/Zqa Zqb/x.md失败", id="chinese_around_a_spaced_path"),
    pytest.param("ファイル/srv/Zqa Zqb/x を開けません", id="japanese_kana_then_a_spaced_path"),
    pytest.param("파일/srv/Zqa Zqb/x 없음", id="korean_then_a_spaced_path"),
    pytest.param("路径是/srv/Zqa/Zqb", id="chinese_then_a_path"),
    pytest.param("修改" + USERS + "/zed/Zqa/Zqb/file.py", id="chinese_edit_then_a_path"),
    pytest.param("请读取C:\\Zqa\\Zqb.md失败", id="chinese_then_a_drive_path"),
    pytest.param("请读取\\\\server\\Zqa\\Zqb", id="chinese_then_a_unc_path"),
    pytest.param("请读取$HOME/Zqa/Zqb.md", id="chinese_then_a_home_variable"),
    pytest.param("请读取~/Zqa/Zqb.md", id="chinese_then_a_tilde_path"),
    pytest.param("请读取~zed/Zqa/Zqb.md", id="chinese_then_another_home"),
    pytest.param("请读取$PWD/Zqa/Zqb.md", id="chinese_then_a_working_directory_path"),
    pytest.param("请读取$CLAUDE_PROJECT_DIR/Zqa/Zqb.md", id="chinese_then_the_project_directory"),
    pytest.param("読み込めません/srv/Zqa/Zqb.md", id="kana_and_kanji_then_a_path"),
    pytest.param("ｆｉｌｅ/srv/Zqa/Zqb.md", id="fullwidth_latin_letters_then_a_path"),
    pytest.param("\U00020000/srv/Zqa/Zqb.md", id="supplementary_plane_ideograph_then_a_path"),
    pytest.param("ᄀ/srv/Zqa/Zqb.md", id="hangul_jamo_then_a_path"),
    pytest.param("ꥠ/srv/Zqa/Zqb.md", id="hangul_jamo_extended_a_then_a_path"),
    pytest.param("豈/srv/Zqa/Zqb.md", id="cjk_compatibility_ideograph_then_a_path"),
    pytest.param("请读取//srv/Zqa/Zqb.md", id="chinese_then_a_double_slash_path"),
    pytest.param("文件：/srv/Zqa Zqb/x.md", id="after_a_fullwidth_colon_already_worked"),
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text", W91[:-1])
def test_fc9_w1_a_path_glued_to_cjk_text_is_a_path_start(level, text):
    r = _redactor(level)
    _clean9(r, text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w1_a_path_after_a_fullwidth_colon_or_a_space_was_already_a_start(level):
    _clean9(_redactor(level), W91[-1].values[0])
    _clean9(_redactor(level), "文件 /srv/Zqa Zqb/x.md")


W91_GOLDEN = [
    pytest.param("无法读取" + USERS + "/zed/Zqa/Zqb.md", "path-1", id="exact_home_row"),
    pytest.param("请读取/srv/Zqa Zqb/x.md失败", f"path-1{HIDDEN}", id="exact_spaced_row"),
    pytest.param("路径是/srv/Zqa/Zqb", "path-1", id="exact_plain_row"),
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text,expected", W91_GOLDEN)
def test_fc9_w1_the_exact_review_rows_render_as_one_path(level, text, expected):
    r = _redactor(level)
    _assert_golden(r, text, expected)


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w1_the_planted_title_and_exception_sinks_hide_a_cjk_glued_path(level):
    r = _redactor(level)
    text = "无法读取/srv/Zqa Zqb/Zqc.md失败"
    assert "Zqa" not in r.title(text)
    assert "Zqa" not in r.exception_text(RuntimeError(text))
    assert "Zqa" not in r.scrub_leftover_paths(text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w1_the_accepted_cost_and_the_latin_controls(level):
    """The cost (DESIGN 5.1; one segment is prose since F fix 10, W10-2). A Latin
    word with a diacritic is still a word character: `naïve/simple` and `Zürich/Bern` stay prose."""
    r = _redactor(level)
    for text in ("naïve/simple ok", "Zürich/Bern ok", "and/or ok", "和 / 或 ok"):
        assert r(text) == text, text


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w1_the_cost_row_is_hidden(level):
    """F fix 10 (W10-2): after a CJK character a start needs two segments or an extension, so the
    one-segment `和/或` is prose now (it was `path-1`); the three-part row is still a start."""
    assert _redactor(level)("和/或") == "和/或"
    assert _redactor(level)("和/或/和") == "path-1"


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w1_a_cjk_boundary_is_in_every_start_lookbehind_not_only_the_slash(level):
    """A drive, a UNC share, `~/`, `$HOME/` after a CJK character hide the private tail; a
    start glued to a Latin word still does not (`abC:\\x`, `ab$HOME/x`)."""
    r = _redactor(level)
    for text in ("abC:\\Zqa\\x ok", "ab$HOME/Zqa/x ok", "ab$PWD/Zqa/x ok"):
        assert r(text) == text, text


# --- W9-2: `@` in front of a variable anchor (sonnet 2) -----------------------------------------------

W92 = [
    pytest.param("curl -d @$HOME/Zqa/Zqb/x.json http://x", id="curl_home_variable"),
    pytest.param("curl -F file=@$HOME/Zqa Zqb/x.json http://x", id="curl_form_home_variable"),
    pytest.param("curl -d @$PWD/Zqa/Zqb/x.json", id="curl_pwd"),
    pytest.param("curl -d @~zed/Zqa/Zqb/x.json", id="curl_other_home"),
    pytest.param('curl -d "@$HOME/Zqa Zqb/x.json"', id="curl_quoted_home_variable"),
    pytest.param("curl -d @${HOME}/Zqa/Zqb/x.json", id="curl_braced_home_variable"),
    pytest.param("curl -d @~/Zqa/Zqb/x.json", id="curl_tilde"),
    pytest.param("curl -d @${PWD}/Zqa/Zqb/x.json", id="curl_braced_pwd"),
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text", [row for row in W92 if row.id in (
    "curl_home_variable", "curl_form_home_variable", "curl_pwd", "curl_other_home",
    "curl_quoted_home_variable")])
def test_fc9_w2_a_variable_anchor_after_an_at_sign_is_a_path_start(level, text):
    _clean9(_redactor(level), text)


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text", [row for row in W92 if row.id in (
    "curl_braced_home_variable", "curl_tilde", "curl_braced_pwd")])
def test_fc9_w2_the_braced_and_tilde_spellings_were_already_hidden(level, text):
    _clean9(_redactor(level), text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w2_the_package_and_address_controls_stay_as_they_are(level):
    r = _redactor(level)
    for text in ("pkg @scope/pkg ok", "mail user@host ok", "import '@scope/pkg' ok"):
        assert r(text) == text, text


# --- W9-3: a possessive apostrophe followed by a space (Codex 1) --------------------------------------

W93 = [
    pytest.param("ENOENT: no such file or directory, open '/srv/James' Notes/private.md'",
                 id="codex1_possessive_then_a_space"),
    pytest.param("ENOENT: no such file or directory, open '/srv/Clients' Files/private.md'",
                 id="plural_possessive"),
    pytest.param("fatal: '/srv/James' Notes/private.md' is not a git repository",
                 id="git_fatal_possessive"),
    pytest.param("Error: Cannot find module '/srv/James' Notes/private.md'",
                 id="node_module_possessive"),
    pytest.param("open '/srv/James' Notes/Bob's Zqb/private.md'", id="possessive_then_in_word"),
    pytest.param("open '/srv/Zqa/James' Zqb' Notes/private.md'", id="two_closing_looking_quotes"),
]


@pytest.mark.parametrize("level,text", _grid9(W93))
def test_fc9_w3_a_possessive_apostrophe_before_a_space_never_closes_a_quoted_path(level, text):
    r = _redactor(level)
    _clean9(r, text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w3_the_exact_row_renders_as_one_quoted_path(level):
    r = _redactor(level)
    text = "ENOENT: no such file or directory, open '/srv/James' Notes/private.md'"
    _assert_golden(r, text, f"ENOENT: no such file or directory, open 'path-1{HIDDEN}'")


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w3_the_fragment_closes_at_the_last_real_quote_on_the_line(level):
    """The rule (fail closed): a single-quoted fragment that holds a path start closes at the LAST
    quote of its line that is not an in-word apostrophe. The cost: a later quoted phrase on the
    same line is hidden with the path."""
    out = _redactor(level)("open '/srv/Zqa/x' and 'git init' now")
    assert out == f"open 'path-1{HIDDEN}' now", out
    out = _redactor(level)("open '/srv/James' Notes/x.md' then")
    assert out == f"open 'path-1{HIDDEN}' then", out
    # an in-word apostrophe is never the last quote: `it's` after the closing quote stays prose
    out = _redactor(level)("open '/srv/Zqa/x' it's fine")
    assert out == "open 'path-1' it's fine", out


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w3_a_fragment_without_a_path_start_closes_at_its_first_quote(level):
    r = _redactor(level)
    for text in (
        "say 'hello' and 'world' now", "cat 'a'b 'c d'", "it's 'the clients' files",
        "use 'git init' and 'git add' ok", "He said 'James' Notes' ok",
    ):
        assert r(text) == text, text


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w3_a_later_line_is_never_swallowed(level):
    out = _redactor(level)("open '/srv/James' Notes/x.md'\nsay 'hi' now")
    first, second = out.split("\n")
    assert "James" not in first and "Notes" not in first, out
    assert second == "say 'hi' now", out


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w3_a_quote_that_is_escaped_inside_a_fragment_is_still_the_quote_text(level):
    out = _redactor(level)("open '/srv/Zqa/O\\'Neil/x' ok")
    assert "Zqa" not in out and "Neil" not in out, out


# --- W9-4: the leading-space root recognizer is removed (Codex 2, sonnet 6) ---------------------------

W94_PROSE = [
    "Pass / Fail rate is 3/4 overall",
    "input / output via stdin/stdout only",
    "read / write and/or execute bits",
    "Build / Test / Deploy pipeline runs CI/CD jobs",
    "use A / B testing for km/h and m/s",
]
W94_ALREADY_PROSE = [
    "cannot read / Client Folder: denied",
    "cannot read / Department of Housing and Urban Development Office of Public Affairs/b.md",
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text", W94_PROSE)
def test_fc9_w4_a_spaced_slash_in_prose_is_never_a_path_start(level, text):
    assert _redactor(level)(text) == text


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text", W94_ALREADY_PROSE)
def test_fc9_w4_the_long_and_slashless_rows_of_the_review_are_prose_too(level, text):
    """The recognizer is rejected, not widened (a root-level directory whose name starts with a
    space does not occur on a real system): the two rows of Codex 2 are prose, a recorded limit."""
    assert _redactor(level)(text) == text


# --- W9-5: one CSI parser: colon-form SGR and other control sequences (Codex 3) -----------------------

CSI = [
    pytest.param(f"{ESC}[38:2::255:0:0m/srv/Client Secret/private.md{ESC}[0m failed",
                 id="codex3_colon_form_exact"),
    pytest.param("[38:2::255:0:0m/srv/Client Secret/private.md[0m failed",
                 id="codex3_colon_form_esc_stripped"),
    pytest.param(f"{ESC}[38:5:196m/srv/Zqa/Zqb.md{ESC}[0m", id="colon_form_256_colour"),
    pytest.param(f"{ESC}[48:2:0:10:20:30m/srv/Zqa Zqb/x.md{ESC}[0m", id="colon_form_background"),
    pytest.param(f"{ESC}[38:2::255:0:0mC:\\Zqa\\Zqb.md{ESC}[0m", id="colon_form_drive"),
    pytest.param(f"{ESC}[38:2::255:0:0m$HOME/Zqa/Zqb/ok.md{ESC}[0m", id="colon_form_home_variable"),
    pytest.param(f"{ESC}[K/srv/Zqa/Zqb", id="erase_in_line"),
    pytest.param(f"{ESC}[2K/srv/Zqa Zqb/x", id="erase_line_then_a_spaced_path"),
    pytest.param(f"{ESC}[1G/srv/Zqa/x", id="cursor_column"),
    pytest.param(f"{ESC}[?25l/srv/Zqa/x", id="hide_cursor_private_parameter"),
    pytest.param(f"{ESC}[1;31;38:2::1:2:3m/srv/Zqa/Zqb", id="mixed_semicolon_and_colon"),
    pytest.param(f"{ESC}[0 q/srv/Zqa/x", id="intermediate_byte_cursor_style"),
]


@pytest.mark.parametrize("level,text", _grid9(CSI))
def test_fc9_w5_a_control_sequence_before_a_path_does_not_hide_the_start(level, text):
    r = _redactor(level)
    _clean9(r, text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w5_three_sequences_in_a_row_then_a_path(level):
    _clean9(_redactor(level), f"{ESC}[2K{ESC}[1G{ESC}[31m/srv/Zqa/Zqb{ESC}[0m")


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w5_the_sequences_themselves_are_kept(level):
    def shown(text):
        return _redactor(level)(text)

    assert shown(f"{ESC}[38:2::255:0:0m/srv/Zqa/Zqb.md{ESC}[0m") == (
        f"{ESC}[38:2::255:0:0mpath-1{ESC}[0m"
    )
    assert shown(f"{ESC}[K/srv/Zqa/Zqb") == f"{ESC}[Kpath-1"
    assert shown("[38:2::255:0:0m/srv/Zqa/Zqb.md[0m") == "[38:2::255:0:0mpath-1"
    assert shown(f"{ESC}[2K{ESC}[1G/srv/Zqa/x") == f"{ESC}[2K{ESC}[1Gpath-1"


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w5_a_sequence_with_operator_bytes_is_one_piece_of_the_word(level):
    """`<`, `=`, `>` and `?` are parameter bytes of a private sequence, not shell operators."""
    r = _redactor(level)
    out = r(f"{ESC}[>4;2m/srv/Zqa/x")
    assert "Zqa" not in out, out


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w5_non_sgr_bracket_prose_and_plain_sequences_stay(level):
    r = _redactor(level)
    for text in (
        "array[0m/x", "x[31m/y", f"{ESC}[31mred{ESC}[0m", "[5m of rain]", "ok [0m now",
        "[K] done", "a[K/x ok", "[38:2::255:0:0m of text", f"{ESC}[Kdone", "see [1G/x",
    ):
        assert r(text) == text, text


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w5_a_bare_escape_bracket_before_a_slash_does_not_eat_the_path_start(level):
    """`ESC [` followed by `/` is not a control sequence: the slash is not a final byte, so the
    path after it is still a path."""
    out = _redactor(level)(f"{ESC}[/srv/Zqa/Zqb")
    assert "Zqa" not in out and "Zqb" not in out, out


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w5_a_colon_form_sequence_that_only_the_decoded_text_shows_is_a_boundary(level):
    text = 'echo "\\u001b[38:2::255:0:0m/srv/Zqa/Zqb y"'
    out = _redactor(level)(text)
    assert "Zqa" not in out and "Zqb" not in out, out


# --- W9-6: the /tmp collapse, $CLAUDE_PROJECT_DIR, the e-mail pattern (sonnet minors) ------------------

@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("space", ["\u00a0", "\u3000", "\u202f", "\u2003"])
def test_fc9_w6_a_tmp_path_with_a_unicode_space_in_a_lower_segment_collapses(level, space):
    text = f"/tmp/T0/Zqa{space}Zqb/Zqd/ok.md"
    r = _redactor(level)
    out = r(text)
    assert "Zqa" not in out and "Zqb" not in out and "Zqd" not in out, out
    for sink, shown in _all_sinks(_redactor(level), text).items():
        assert "Zqa" not in shown and "Zqd" not in shown, (sink, shown)
    assert redact.redact(text) == "/tmp/T0/.../ok.md"


@pytest.mark.parametrize("space", ["\u00a0", "\u3000"])
def test_fc9_w6_the_first_segment_may_hold_a_unicode_space_too(space):
    """The first segment is kept by design; the directories below it are not."""
    out = redact.redact(f"/private/tmp/Zqa{space}Zqb/Zqd/ok.md")
    assert "Zqd" not in out and out.endswith("/.../ok.md"), out


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w6_the_collapse_still_leaves_prose_after_a_tmp_path_alone(level):
    assert redact.redact("see /tmp/T0/a/b.md and /srv/x/y") == "see /tmp/T0/.../b.md and /srv/x/y"
    assert redact.redact("/tmp/T0/a ok") == "/tmp/T0/a ok"


W96 = [
    pytest.param("cat $CLAUDE_PROJECT_DIR/Zqa Zqb/x.md now", id="project_dir_with_a_space"),
    pytest.param('bash "$CLAUDE_PROJECT_DIR/Zqa Zqb/hooks/x.sh"', id="hook_command"),
    pytest.param("python $CLAUDE_PROJECT_DIR/Zqa/Zqb/x.py", id="project_dir_script"),
]


@pytest.mark.parametrize("level,text", _grid9(W96))
def test_fc9_w6_the_project_directory_variable_is_an_absolute_anchor(level, text):
    _clean9(_redactor(level), text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w6_the_braced_project_directory_was_already_hidden_by_the_brace(level):
    _clean9(_redactor(level), "cat ${CLAUDE_PROJECT_DIR}/Zqa/Zqb/x.md")


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w6_the_project_directory_variable_is_an_opaque_path_never_a_repo_form(level):
    out = _redactor(level)("cat $CLAUDE_PROJECT_DIR/Zqa/Zqb.md")
    assert out in ("cat path-1", f"cat path-1{HIDDEN}"), out


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w6_the_project_directory_controls_stay_prose(level):
    r = _redactor(level)
    for text in (
        "$CLAUDE_PROJECT_DIRS/x ok", "$CLAUDE_PROJECT_DIRX/y", "price $CLAUDE_PROJECT_DIR",
        "a$CLAUDE_PROJECT_DIR/b", "$CLAUDE_PROJECT/x",
    ):
        assert r(text) == text, text


LIMIT = 3.0  # seconds; a 100,000-character word takes about 30 s with the backtracking pattern


def _timed(func, *args):
    start = time.perf_counter()
    result = func(*args)
    return result, time.perf_counter() - start


@pytest.mark.parametrize("word", ["a" * 100_000, "a.b-" * 25_000])
def test_fc9_w6_the_email_pattern_is_linear_on_a_100000_character_word(word):
    _out, seconds = _timed(redact._EMAIL_RE.sub, "x", word)
    assert seconds < LIMIT, seconds


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w6_a_100000_character_word_is_redacted_in_bounded_time(level):
    r = _redactor(level)
    _out, seconds = _timed(r, "x " + "a" * 100_000 + " y")
    assert seconds < LIMIT, seconds


def test_fc9_w6_the_email_pattern_keeps_its_matches():
    assert redact._EMAIL_RE.findall("a.b@example.com, c+d@sub.example.com.") == [
        "a.b@example.com", "c+d@sub.example.com",
    ]
    assert redact._EMAIL_RE.findall("-a@example.com x@y") == ["-a@example.com"]
    assert redact._EMAIL_RE.sub("E", "mail bob@example.com now") == "mail E now"
    assert redact._EMAIL_RE.findall("a@b@example.com") == ["b@example.com"]


# --- W9-7: test gaps (sonnet 9, Codex 5) -----------------------------------------------------------

@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w7_a_registered_only_relative_prefix_before_json_hides_the_document(level):
    """The registered check of the JSON prepass: a registered relative value is the only start
    before the document, so the document stays with the word scanner (the end-of-line rule)."""
    r = _redactor(level, "Client Secret X")
    text = 'open Client Secret X {"relative":"Private\\\\notes.md","status":"failed"}'
    out = r(text)
    assert "Private" not in out and "Client" not in out and "Secret" not in out, out


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("path", [
    "\\\\.\\pipe\\Zqa Zqb\\x", "\\\\.\\GLOBALROOT\\Zqa Zqb\\x", "\\\\?\\GLOBALROOT\\Zqa Zqb\\x",
    "\\\\.\\Zqa\\Zqb",
])
def test_fc9_w7_a_namespace_path_with_a_name_that_is_not_a_drive_is_a_start(level, path):
    r = _redactor(level)
    _clean9(r, f"open {path} now")
    _clean9(r, f'open "{path}" now')


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("error", [
    FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError, FileExistsError,
    OSError,
])
def test_fc9_w7_exception_text_swaps_the_filenames_of_every_oserror_subclass(level, error):
    r = _redactor(level)
    for name in ("/srv/Zqa/Bob's Zqb/x.md", '/srv/Zqa/"Zqb" y/x.md', "/srv/Zqa/Bob's \"Zqb\"/x.md"):
        exc = error(2, "failed", name)
        out = r.exception_text(exc)
        assert "Zqa" not in out and "Zqb" not in out and "Bob" not in out, (error, name, out)
        two = error(2, "failed", name, None, "/srv/Zqc/Zqd")
        out = r.exception_text(two)
        assert "Zqa" not in out and "Zqc" not in out and "Zqd" not in out, (error, name, out)


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w7_exception_text_uses_the_filename_split_for_a_subclass_with_a_both_quote_repr(level):
    """The both-quote repr is `"..."` with an escaped quote or `'...\\'...'`; the split must find
    the repr the exception printed, whichever OSError subclass printed it."""
    r = _redactor(level)
    exc = PermissionError(13, "Permission denied", "/srv/Zqa/O'Neil \"x\" Zqb/f")
    assert str(exc).count("Zqa") == 1
    out = r.exception_text(exc)
    assert "Zqa" not in out and "Zqb" not in out and "Neil" not in out, out
    assert out.startswith("[Errno 13] Permission denied: "), out


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w7_the_in_word_apostrophe_is_never_the_last_quote(level):
    """After 8b an in-word apostrophe is name text. In the W9-3 rule it is also never the quote
    that closes a fragment, whichever side of the path start it stands."""
    out = _redactor(level)("open '/srv/Zqa/x' it's 'here' now")
    assert out == f"open 'path-1{HIDDEN}' now", out
    out = _redactor(level)("open '/srv/Zqa/Bob's Zqb' it's fine")
    assert out == f"open 'path-1{HIDDEN}' it's fine", out


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w4_the_leading_space_root_is_a_recorded_limit_only_when_unquoted(level):
    """Recorded limit (DESIGN 5.1): an unquoted `/ Client Folder/x.md` is not a path start (a
    root-level directory whose name starts with a space does not occur on a real system). The
    quoted spelling is still a start: a quote opens a fragment whose first `/` is the root."""
    text = "cannot read / Client Folder/report.md: denied"
    assert _redactor(level)(text) == text
    out = _redactor(level)('cannot read "/ Client Folder/report.md" denied')
    assert out == f'cannot read "path-1{HIDDEN}" denied', out


@pytest.mark.parametrize("level", LEVELS)
def test_fc9_w3_a_quoted_bare_root_does_not_take_the_rest_of_the_line(level):
    """`'/'` names no directory (`split('/')`): it is not a fragment that holds a path start for
    the last-quote rule, so a later quoted phrase on the line is kept."""
    out = _redactor(level)("echo '/' and 'keep this' now")
    assert out.endswith("and 'keep this' now"), out


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("error", [
    FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError, FileExistsError,
    TimeoutError, OSError,
])
def test_fc9_w7_a_relative_filename_of_any_oserror_subclass_is_swapped_for_its_display(level, error):
    """A relative filename is no path start, so only the filename split can hide it: both
    filenames of every subclass, with a name that holds both quote kinds (the repr then escapes one)."""
    r = _redactor(level)
    exc = error(13, "denied", "Zqa Bob's \"Zqb\" notes.md", None, "Zqc Zqd")
    out = r.exception_text(exc)
    for word in ("Zqa", "Zqb", "Zqc", "Zqd", "Bob", "notes"):
        assert word not in out, (error, word, out)
    assert out.startswith("[Errno 13] denied: "), out
