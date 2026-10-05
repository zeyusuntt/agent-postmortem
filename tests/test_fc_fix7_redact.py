"""Fix round 7 (W7-1, W7-3, W7-5) of field milestone F: an untrusted path hides to the end of its
fragment or line with no early stop, Windows paths are path anchors, and the JSON-span pass is
linear (docs/DESIGN.md 5.1; .reviews/v0.3/F-dispositions.md, "Fix round 6 and 6b review").

Every exact input of Codex round 6 (F-codex-r6.md) and of the sonnet review (F-W6-review.md) runs
through the sinks the reviewers list, at share and at local, unregistered and registered.

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

PRIVATE7 = (
    "Smith", "Jones", "Client", "Secret", "Archive", "Zqa", "Zqb", "Zqc", "Zqd", "Files",
    "Johnson", "Ernst", "Young", "Holdings", "Meeting", "Standup", "Notes", "old", "Acme",
    "Project", "notes.md", "x.md", "Docs", "server",
)


def _grid7(rows):
    """Every row at both levels."""
    return [
        pytest.param(level, *row.values, id=f"{row.id}-{level}")
        for row in rows for level in LEVELS
    ]


def _clean7(r, text, private=PRIVATE7):
    for sink, out in _all_sinks(r, text).items():
        for word in private:
            assert word not in out, (sink, word, out)


# --- W7-1: no early stop; the exact inputs of Codex round 6 finding 1 --------------------------------

CODEX1 = [
    pytest.param("open /srv/Smith; Jones/notes.md now", f"open path-1{HIDDEN}",
                 id="codex1_semicolon_space_in_a_name"),
    pytest.param("open /srv/Client | Secret/x now", f"open path-1{HIDDEN}",
                 id="codex1_pipe_space_in_a_name"),
    pytest.param("open /srv/Client\u201d Secret/notes.md now", f"open path-1{HIDDEN}",
                 id="codex1_closing_curly_quote_in_a_name"),
    pytest.param("open /srv/Project:12: Archive/notes.md now", f"open path-1{HIDDEN}",
                 id="codex1_line_number_shape_in_a_name"),
]

# sonnet 2 (a typographic closer after a word), 3 (a time or a number after a colon), 4 (an
# operator or a parenthesis followed by a space)
SONNET = [
    pytest.param("see /srv/data/Zqa\u2019 Files/x.md now", f"see path-1{HIDDEN}",
                 id="sonnet2_possessive_plural_absolute"),
    pytest.param("see ~/Zqa\u2019 Files/x.md now", f"see path-1{HIDDEN}",
                 id="sonnet2_possessive_plural_home"),
    pytest.param("ls /srv/data/Zqa\u2019 Files", f"ls path-1{HIDDEN}", id="sonnet2_ls"),
    pytest.param("cd /srv/data/Zqa\u2019 Files && ls", f"cd path-1{HIDDEN}", id="sonnet2_cd_and"),
    pytest.param("see /srv/data/Zqa\u2019\u2019 Files/x.md now", f"see path-1{HIDDEN}",
                 id="sonnet2_two_apostrophes"),
    pytest.param("/srv/data/Zqa\u2019, Files/x.md", f"path-1{HIDDEN}", id="sonnet2_comma_after"),
    pytest.param("/srv/data/Zqa\u201d Files", f"path-1{HIDDEN}", id="sonnet2_right_double"),
    pytest.param("/srv/data/Zqa\u00bb Files", f"path-1{HIDDEN}", id="sonnet2_guillemet"),
    pytest.param("/srv/data/Zqa\u300d Files", f"path-1{HIDDEN}", id="sonnet2_corner_bracket"),
    pytest.param("/srv/data/Zqa\u203a Files", f"path-1{HIDDEN}", id="sonnet2_single_guillemet"),
    pytest.param("/srv/data/Zqa\u300f Files", f"path-1{HIDDEN}", id="sonnet2_white_corner"),
    pytest.param("see /srv/data/Zqa 10:30 Zqb/Zqc.md", f"see path-1{HIDDEN}",
                 id="sonnet3_time_in_a_name"),
    pytest.param("see ~/Meeting 10:30 Notes/x.md now", f"see path-1{HIDDEN}",
                 id="sonnet3_time_in_a_home_name"),
    pytest.param("see /srv/data/2024-01-01 10:30:00 Standup/Zqc.md", f"see path-1{HIDDEN}",
                 id="sonnet3_date_and_time"),
    pytest.param("see /srv/data/Zqa:2024 Zqb/Zqc.md", f"see path-1{HIDDEN}",
                 id="sonnet3_colon_year"),
    pytest.param("see /srv/data/Johnson & Johnson/notes.md now", f"see path-1{HIDDEN}",
                 id="sonnet4_johnson_and_johnson"),
    pytest.param("see /srv/data/Ernst & Young/Client/notes.md now", f"see path-1{HIDDEN}",
                 id="sonnet4_ernst_and_young"),
    pytest.param("see /srv/data/Notes (old) Archive/x.md now", f"see path-1{HIDDEN}",
                 id="sonnet4_parenthesis_then_space"),
    pytest.param("see /srv/data/Client (Acme) Holdings/notes.md now", f"see path-1{HIDDEN}",
                 id="sonnet4_parenthesized_word_then_space"),
    pytest.param("see /srv/data/Smith -> Jones/x.md now", f"see path-1{HIDDEN}",
                 id="sonnet4_arrow"),
    # the sonnet 9 inputs: a closed quote first, an unquoted start later in the same word
    pytest.param('PATH="/usr/bin":/srv/Zqc Zqd/bin', f"PATH=path-1{HIDDEN}",
                 id="sonnet9_quoted_start_then_unquoted_start"),
    pytest.param('"/srv/Zqa Zqb"/Zqc Zqd/x', f"path-1{HIDDEN}",
                 id="sonnet9_closed_quote_then_a_slash"),
]


@pytest.mark.parametrize("level,text,expected", _grid7(CODEX1 + SONNET))
def test_fc7_w1_the_exact_review_inputs_hide_to_the_end_of_the_line(level, text, expected):
    r = _redactor(level)
    _assert_golden(r, text, expected)
    _clean7(r, text)


# the registered variants: the whole path is a registered value, and the line goes on after it
REGISTERED_ROWS = [
    pytest.param("/srv/Smith; Jones/notes.md", "open /srv/Smith; Jones/notes.md now",
                 id="codex1_semicolon"),
    pytest.param("/srv/Client | Secret/x", "open /srv/Client | Secret/x now", id="codex1_pipe"),
    pytest.param("/srv/Client\u201d Secret/notes.md",
                 "open /srv/Client\u201d Secret/notes.md now", id="codex1_curly_quote"),
    pytest.param("/srv/Project:12: Archive/notes.md",
                 "open /srv/Project:12: Archive/notes.md now", id="codex1_line_number_shape"),
    pytest.param("/srv/data/Johnson & Johnson/notes.md",
                 "see /srv/data/Johnson & Johnson/notes.md now", id="sonnet4_johnson"),
    pytest.param("/srv/data/Zqa\u2019 Files/x.md", "see /srv/data/Zqa\u2019 Files/x.md now",
                 id="sonnet2_possessive"),
]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("value,text", REGISTERED_ROWS)
def test_fc7_w1_a_registered_value_with_an_old_stop_inside_it_is_one_path(level, value, text):
    r = _redactor(level, value)
    word = text.split(" ", 1)[0]
    _assert_golden(r, text, f"{word} path-1{HIDDEN}")
    _clean7(r, text)
    # the value alone is its own pseudonym, with the marker only where whitespace was hidden
    assert r(value) == f"path-1{HIDDEN}"


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w1_a_registered_value_without_the_stop_character_is_unchanged(level):
    r = _redactor(level, "/srv/Smith/notes.md")
    _assert_golden(r, "open /srv/Smith/notes.md", "open path-1")


# the separator scan of the sonnet review: every printable ASCII character, four spacings
SEPARATORS = [
    c for c in map(chr, range(33, 127)) if c not in "/\\\"'`$"
]
SPACINGS = ["{c}", "{c} ", " {c}", " {c} "]


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w1_no_separator_character_ends_a_path_early(level):
    r = _redactor(level)
    leaks = []
    for char in SEPARATORS:
        for spacing in SPACINGS:
            text = "see /srv/data/Zqa" + spacing.format(c=char) + "Zqb/Zqc.md now"
            for shown in (r(text), r.title(text), r.scrub_leftover_paths(text)):
                if any(word in shown for word in ("Zqa", "Zqb", "Zqc")):
                    leaks.append((text, shown))
    assert not leaks, leaks[:5]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("quote", ["\"", "'", "`", "\u201c", "\u2018", "\u00ab", "\u300c"])
def test_fc7_w1_an_unpaired_opener_in_a_name_hides_the_rest_of_the_line(level, quote):
    r = _redactor(level)
    text = f"see /srv/data/Zqa{quote} Zqb/Zqc.md now"
    for sink, shown in _all_sinks(r, text).items():
        for word in ("Zqa", "Zqb", "Zqc"):
            assert word not in shown, (sink, word, shown)


# what ends a span is still the fragment that encloses it, and a newline
@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w1_the_closing_quote_of_an_enclosing_fragment_ends_the_span(level):
    r = _redactor(level)
    _assert_golden(r, 'cat "/srv/Zqa Zqb/x" now', f'cat "path-1{HIDDEN}" now')
    _assert_golden(r, "cat '/srv/Zqa Zqb/x' now", f"cat 'path-1{HIDDEN}' now")
    _assert_golden(r, "cat \u201c/srv/Zqa Zqb/x\u201d now", f"cat \u201cpath-1{HIDDEN}\u201d now")
    _assert_golden(r, "cat \u00ab/srv/Zqa Zqb/x\u00bb now", f"cat \u00abpath-1{HIDDEN}\u00bb now")
    _assert_golden(r, "cat `/srv/Zqa Zqb/x` now", f"cat `path-1{HIDDEN}` now")


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w1_a_newline_ends_the_span(level):
    r = _redactor(level)
    out = r("see /srv/data/Zqa; Zqb/x now\nthe next line stays")
    assert out == f"see path-1{HIDDEN}\nthe next line stays", out


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w1_a_stray_closer_after_the_path_start_is_name_text_and_a_trailing_one_is_kept(level):
    r = _redactor(level)
    assert r("see /srv/Zqa/x.md\u2019") == "see path-1\u2019"
    assert r("see /srv/Zqa/x.md.") == "see path-1."
    assert r("see /srv/Zqa/x.md:12:3") == "see path-1:12:3"
    assert r("/srv/Zqa/x.md:12:") == "path-1:12:"


# the old stops are not stops of anything: operators, wrappers and evidence suffixes after a
# whitespace-free path at the end of the line stay as they were
@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w1_text_without_a_path_is_untouched(level):
    r = _redactor(level)
    for text in ("a; b | c && d", "see https://example.com/a/b", "and/or 3/4", "(x) \u201cy\u201d"):
        assert r(text) == text


# --- W7-3: Windows paths are path anchors ----------------------------------------------------------

WINDOWS = [
    pytest.param("open C:\\Docs\\Client Secret\\notes.md now", f"open path-1{HIDDEN}",
                 id="codex2_drive_backslash_space"),
    pytest.param("open C:/Docs/Client Secret/notes.md now", f"open path-1{HIDDEN}",
                 id="drive_slash_space"),
    pytest.param("open C:\\Docs\\Client\\notes.md", "open path-1", id="drive_backslash"),
    pytest.param("open c:\\Docs\\Client\\notes.md", "open path-1", id="lower_case_drive"),
    pytest.param("open D:/Docs/Client/notes.md", "open path-1", id="drive_slash"),
    pytest.param("open \\\\server\\share\\Client Secret\\notes.md now", f"open path-1{HIDDEN}",
                 id="unc_space"),
    pytest.param("open \\\\server\\share\\Client\\notes.md", "open path-1", id="unc"),
    pytest.param('open "C:\\Docs\\Client Secret\\notes.md" now',
                 f'open "path-1{HIDDEN}" now', id="drive_double_quoted"),
    pytest.param("open 'C:\\Docs\\Client Secret\\notes.md' now",
                 f"open 'path-1{HIDDEN}' now", id="drive_single_quoted"),
    pytest.param("PATH=C:\\Docs\\Client\\bin", "PATH=path-1", id="drive_after_an_assignment"),
    pytest.param("--out=C:\\Docs\\Client\\x", "--out=path-1", id="drive_after_an_option"),
    pytest.param('"path": "C:\\\\Docs\\\\Client Secret\\\\notes.md', f'"path": "path-1{HIDDEN}',
                 id="drive_in_a_truncated_json_string"),
    pytest.param('"path": "\\\\\\\\server\\\\share\\\\Client Secret', f'"path": "path-1{HIDDEN}',
                 id="unc_in_a_truncated_json_string"),
    pytest.param("see file://server/share/Zqa Zqb/x now", f"see path-1{HIDDEN}",
                 id="file_url_with_a_host"),
    pytest.param("see file://host.example/srv/Zqa Zqb/x now", f"see path-1{HIDDEN}",
                 id="sonnet10_file_url_with_a_host_and_a_path"),
]


@pytest.mark.parametrize("level,text,expected", _grid7(WINDOWS))
def test_fc7_w3_windows_paths_are_path_anchors(level, text, expected):
    r = _redactor(level)
    _assert_golden(r, text, expected)
    _clean7(r, text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w3_a_windows_path_in_a_json_document_is_scanned_value_by_value(level):
    for value, expected in (
        ("C:\\Docs\\Client Secret\\notes.md", f"path-1{HIDDEN}"),
        ("\\\\server\\share\\Client Secret", f"path-1{HIDDEN}"),
        ("C:/Docs/Client/notes.md", "path-1"),
    ):
        r = _redactor(level)
        text = json.dumps({"path": value, "status": "failed", "count": 7})
        out = r(text)
        assert json.loads(out) == {"path": expected, "status": "failed", "count": 7}, out
        _clean7(r, text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w3_a_registered_windows_path_is_one_path(level):
    value = "C:\\Docs\\Client Secret\\notes.md"
    r = _redactor(level, value)
    _assert_golden(r, f"open {value} now", f"open path-1{HIDDEN}")
    _clean7(r, f"open {value} now")


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w3_prose_that_looks_a_little_like_a_drive_or_a_share_is_untouched(level):
    r = _redactor(level)
    for text in (
        "see https://example.com/a/b and re:/x", "a regex \\\\d+ and \\\\w", "ratio 3:/4",
        "the C: drive", "key:\\n", "ab:/cd",
    ):
        assert r(text) == text, text


# --- W7-5: the JSON-span pass is linear ---------------------------------------------------------------


class _Counter:
    """The characters handed to PathLookup.hits (the scan whose repeated restart made the pass
    quadratic): a deterministic operation count, not a wall clock."""

    def __init__(self, monkeypatch):
        self.chars = 0
        self.calls = 0
        original = redact._PathLookup.hits
        counter = self

        def counted(this, parts):
            counter.calls += 1
            counter.chars += sum(len(part) for part in parts)
            return original(this, parts)

        monkeypatch.setattr(redact._PathLookup, "hits", counted)


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w5_many_bracket_spans_after_a_registered_value_cost_a_linear_scan(level, monkeypatch):
    r = _redactor(level, "Acme")
    text = "Acme " + "[1] " * 8000
    counter = _Counter(monkeypatch)
    out = r(text)
    assert "Acme" not in out
    assert counter.chars <= 4 * len(text), (counter.chars, len(text))


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w5_many_bracket_spans_after_a_slash_path_cost_a_linear_scan(level, monkeypatch):
    r = _redactor(level, "Acme")
    text = "/srv/x " + "[1] " * 8000
    counter = _Counter(monkeypatch)
    r(text)
    assert counter.chars <= 4 * len(text), (counter.chars, len(text))


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w5_documents_before_a_path_are_still_read_value_by_value(level):
    r = _redactor(level, "Acme")
    out = r('{"a": "/srv/Zqa/x"} and [1, 2] then Acme [3]')
    assert out.startswith('{"a": "path-'), out
    assert "[1, 2]" in out and "[3]" not in out and "Zqa" not in out, out


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w5_spans_on_later_lines_after_a_path_line_are_read(level):
    r = _redactor(level, "Acme")
    out = r("Acme [1] [2]\n{\"k\": \"/srv/Zqa/x\"}\n[3, 4]")
    assert out.splitlines()[1].startswith('{"k": "path-'), out
    assert out.splitlines()[2] == "[3, 4]", out


def test_fc7_w3_a_windows_path_or_a_foreign_file_host_is_always_an_opaque_span():
    """The span of a Windows path or a `file://` URL with a host goes to the opaque renderer even
    without whitespace (never a repo-relative or structural form); a Unix path does not."""
    from agent_postmortem.redact import Pseudonyms, scan_paths

    plain, opaque = [], []

    def render(token, _hit):
        plain.append(token)
        return "<P>"

    def render_hidden(token, _hit):
        opaque.append(token)
        return "<H>"

    text = "\n".join([
        "open C:\\Docs\\x", "open \\\\h\\s\\x", "open C:/Docs/x", "open file://h/s/x", "open /srv/x",
    ])
    out = scan_paths(text, Pseudonyms(), render, render_hidden=render_hidden)
    assert out.splitlines() == ["open <H>"] * 4 + ["open <P>"], out
    assert plain == ["/srv/x"] and len(opaque) == 4


@pytest.mark.parametrize("quote", ["'", '"', "`"])
def test_fc7_w1_a_word_of_many_adjacent_fragments_is_not_quadratic(quote):
    """Every path start of a word is checked against the closed fragments of the word (a start
    inside a closed quote ends with it); with 20 000 anchors and 20 000 fragments an all-pairs
    check takes seconds. The bound is generous: the linear pass takes a few hundredths."""
    import time

    r = _redactor("share")
    text = (quote + "/a" + quote) * 20_000
    started = time.perf_counter()
    out = r(text)
    assert time.perf_counter() - started < 3.0
    assert "/a" not in out


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w5_a_backquote_pair_inside_a_name_does_not_end_the_span(level):
    """A backquote is a legal character of a name and a display wrapper; a closing backquote is
    not a stop either (docs/DESIGN.md 5.1): the span runs to the end of the line."""
    r = _redactor(level)
    for text in (
        "see /srv/Zqa `Zqb` Zqc/x now", "see /srv/Zqa`Zqb` Zqc/x now",
        "see /srv/Zqa `Zqb`Zqc/x now", "see /srv/Zqa``Zqb`` Zqc/x now",
    ):
        assert r(text).startswith("see path-") and r(text).endswith(HIDDEN), (text, r(text))
        for word in ("Zqa", "Zqb", "Zqc"):
            assert word not in r(text), (text, r(text))
