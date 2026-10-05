"""Fix round 5 (W5-1 to W5-6) of field milestone F: what happens after the shell-word scanner
finds a path (docs/DESIGN.md 5.1; .reviews/v0.3/F-dispositions.md, "Fix round 4 (W) review").

Every exact input of the sonnet review (F-W-review.md findings 1 to 8) and of Codex round 4
(F-codex-r4.md findings 1 to 4) runs through the sinks the reviewers list, at share and at local.

Every fixture is synthetic. Private words are the ones a stranger must never read: `Client`,
`Secret`, `Neil`, `deep`, `notes.md`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_postmortem import reader, report
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.models import Finding
from agent_postmortem.redact import Redactor

HOME = Path("/home/user")
LEVELS = ["share", "local"]
PRIVATE = ("Client", "Secret", "Neil", "deep", "notes.md")
FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "clean_run"
HIDDEN = " [text hidden]"


def _grid(rows):
    """Every row at both levels."""
    return [
        pytest.param(level, *row.values, id=f"{row.id}-{level}")
        for row in rows for level in LEVELS
    ]


def _redactor(level, *registered, repo_root=None):
    r = Redactor(context=DisclosureContext(level=level, home=HOME, repo_root=repo_root))
    names = r.fresh_names(detailed=(level == "local"))
    for value in registered:
        names.path(value)
    return r


def _all_sinks(r, text):
    """Every free-text sink the one scanner serves and the reviewers name: the plain call,
    title, a JSON string, an evidence line, a diagnostic, a finding export, exception text,
    the leftover scrub, and at local the local line scrub and the local evidence view."""
    finding = Finding(
        detector="silent_retry", severity="major", title=text, run_id="wf_00000000-a1",
        agent_id=None, evidence=[text], metrics={"command": text, "nested": [text]}, rule_id=None,
    )
    sinks = {
        "plain": r(text),
        "title": r.title(text),
        "json": json.loads(json.dumps(report.redact_value({"evidence": text}, r)))["evidence"],
        "evidence": report._shown_evidence([text], None, r)[0],
        "exception": r.exception_text(RuntimeError(text)),
        "leftover": r.scrub_leftover_paths(text),
        "diagnostic": report._diagnostic_for_export(text, r),
    }
    exported = report._export_finding(finding, r, frozenset())
    sinks["finding_title"] = exported["title"]
    sinks["finding_metrics"] = exported["metrics"]["command"]
    if r.level == "local":
        sinks["local_line"] = r.scrub_local_line(r(text))
        view = report.EvidenceView(reader.load_run(FIXTURE), r, show_excerpts=True)
        sinks["evidence_view"] = view.line(f"agent-ac100000000000001.jsonl:1: {text}")
    return sinks


def _assert_clean(r, text, private=PRIVATE):
    for sink, out in _all_sinks(r, text).items():
        for word in private:
            assert word not in out, (sink, word, out)


# Sinks whose shown text for one path is path-1 whatever the earlier sinks registered.
GOLDEN_SINKS = ("plain", "title", "json", "evidence", "diagnostic", "finding_title",
                "leftover", "exception")


def _assert_golden(r, text, expected, sinks=GOLDEN_SINKS):
    outs = _all_sinks(r, text)
    for sink in sinks:
        assert outs[sink] == expected, (sink, outs[sink])


# --- W5-1: a span that holds whitespace is path-N plus the omission marker ------------------------

W51 = [
    # sonnet 1: a /tmp path with a space at share
    pytest.param('cat "/tmp/Client Secret/deep/notes.md" now',
                 f'cat "path-1{HIDDEN}" now', id="tmp_quoted_space"),
    pytest.param("cat /tmp/Client\\ Secret/deep/notes.md now",
                 f"cat path-1{HIDDEN}", id="tmp_escaped_space"),  # F fix 6 (W6-2)
    pytest.param('cat "/tmp/Client/deep Secret/notes.md"',
                 f'cat "path-1{HIDDEN}"', id="tmp_space_in_a_deep_segment"),
    pytest.param("cat '/private/tmp/run 1/work/out.txt' ok",
                 f"cat 'path-1{HIDDEN}' ok", id="private_tmp_space"),
    pytest.param('error="/tmp/x/y.py failed with /srv/data/Client Secret/deep/notes.md"',
                 f'error="path-1{HIDDEN}"', id="tmp_then_a_foreign_path_in_one_quote"),
    pytest.param(
        "journal.jsonl: unreadable: [Errno 13] Permission denied: "
        "'/tmp/Client Secret/proj/deep/journal.jsonl'",
        f"journal.jsonl: unreadable: [Errno 13] Permission denied: 'path-1{HIDDEN}'",
        id="diagnostic_row"),
    # Codex 3 and sonnet 7: the marker follows the whitespace rule, not the old narrowing
    pytest.param('error="/srv/private/file failed with EACCES"',
                 f'error="path-1{HIDDEN}"', id="codex3_prose_after_the_path_in_a_quote"),
    pytest.param('"/srv/p/f failed"', f'"path-1{HIDDEN}"', id="anchored_closed_quote_prose"),
    pytest.param('cat "/srv/x/a b"', f'cat "path-1{HIDDEN}"', id="quoted_path_with_a_space"),
    # F fix 6 (W6-2): an unquoted path hides the rest of its line, so `now` is `[text hidden]`
    pytest.param("cat /srv/data/A\\ B/c now", f"cat path-1{HIDDEN}", id="escaped_space"),
    pytest.param("cat /srv/data/'A B'/c now", f"cat path-1{HIDDEN}", id="quoted_segment"),
    pytest.param('cat "/home/user/Client Secret/deep/notes.md" now',
                 f'cat "path-1{HIDDEN}" now', id="home_quoted_space_not_the_structural_form"),
    pytest.param("cat /home/user/Client\\ Secret/deep/notes.md now",
                 f"cat path-1{HIDDEN}", id="home_escaped_space_not_the_structural_form"),
    pytest.param("cat /srv/data/A\\\nB/c now", f"cat path-1{HIDDEN}",
                 id="continuation_joins_the_lines_and_now_is_hidden"),
    pytest.param("cat /srv/data/A\\\nB/c", "cat path-1", id="continuation_has_no_space"),
    pytest.param('cat "/srv/x/y" now', 'cat "path-1" now', id="a_quoted_path_without_space"),
]


@pytest.mark.parametrize("level,text,expected", _grid(W51))
def test_fc5_w1_whitespace_in_a_rendered_span_is_path_n_and_the_marker(level, text, expected):
    r = _redactor(level)
    _assert_golden(r, text, expected)
    _assert_clean(r, text)


W51_REPO = [
    # sonnet 2: a word that starts with an in-repo path and holds prose at local
    pytest.param('error="/repo/src/a.py failed with /srv/data/Client Secret/deep/notes.md"',
                 f'error="path-1{HIDDEN}"', id="in_repo_path_then_prose_and_a_foreign_path"),
    pytest.param('cat "/repo/src/a.py and /srv/Client/x" now',
                 f'cat "path-1{HIDDEN}" now', id="in_repo_path_then_and_a_foreign_path"),
    # F fix 6 (W6-2): the path ends the line (a trailing word would be hidden with it)
    pytest.param("cat /repo/src/a.py", "cat src/a.py", id="control_in_repo_path"),
    pytest.param("cat /repo/src/a.py now", f"cat path-1{HIDDEN}", id="in_repo_path_then_prose"),
    pytest.param('cat "/repo/src/a.py" now', 'cat "src/a.py" now', id="control_in_repo_quoted"),
]


@pytest.mark.parametrize("text,expected", W51_REPO)
def test_fc5_w1_a_repo_relative_form_never_carries_prose_or_a_foreign_path(text, expected):
    r = _redactor("local", repo_root="/repo")
    outs = _all_sinks(r, text)
    outs["evidence_view"] = outs["evidence_view"].split(": ", 1)[1]
    for sink in ("plain", "title", "json", "evidence", "diagnostic", "local_line",
                 "evidence_view", "exception"):
        assert outs[sink] == expected, (sink, outs[sink])
    _assert_clean(_redactor("local", repo_root="/repo"), text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc5_w1_an_exactly_registered_span_with_a_space_carries_the_marker(level):
    r = _redactor(level, "/srv/my work/out")
    assert r('x "/srv/my work/out" y') == f'x "path-1{HIDDEN}" y'
    # F fix 6 (W6-2): an unquoted path hides the rest of its line
    assert r("x /srv/my\\ work/out y") == f"x path-1{HIDDEN}"
    # no whitespace in the registered value: no marker
    r = _redactor(level, "/srv/work/out")
    assert r('x "/srv/work/out" y') == 'x "path-1" y'


def test_fc5_w1_a_registered_hit_at_local_with_prose_is_path_n_not_the_p19_form():
    r = _redactor("local", "/repo/src/a.py", repo_root="/repo")
    shown = r('error="/repo/src/a.py failed with EACCES"')
    assert shown == f'error="path-1{HIDDEN}"', shown
    # F fix 6 (W6-2): the registered hit keeps its display and hides the rest of the line
    assert r("see /repo/src/a.py now") == f"see src/a.py{HIDDEN}"
    assert r("see /repo/src/a.py") == "see src/a.py"


# --- W5-2: a path with a space in free text -------------------------------------------------------

W52 = [
    pytest.param("cannot open /srv/data/Client Secret/deep/notes.md now",
                 f"cannot open path-1{HIDDEN}", id="codex1_unquoted_space"),  # F fix 6 (W6-2)
    pytest.param("see `/srv/data/Client Secret/deep/notes.md` now",
                 f"see `path-1{HIDDEN}` now", id="markdown_code_span"),
    # F fix 7 (W7-1, R1): the `)` and the evidence suffix are not stops; the rest is hidden
    pytest.param("see (/srv/data/Client Secret/deep/notes.md) now",
                 f"see (path-1{HIDDEN}", id="parenthesised"),
    pytest.param("ENOENT /srv/My Dir/test_x.py:12: AssertionError",
                 f"ENOENT path-1{HIDDEN}", id="pytest_style_line_suffix"),
    pytest.param("cat /srv/data/Client Secret/deep/notes.md.",
                 f"cat path-1{HIDDEN}.", id="sentence_dot_after_a_joined_span"),
    pytest.param("cat „/srv/data/Client Secret/deep/notes.md“ now",
                 f"cat „path-1{HIDDEN}“ now", id="codex1_german_low_quotes"),
    pytest.param("cat “/srv/data/Client Secret/deep/notes.md“ now",
                 f"cat “path-1{HIDDEN}“ now", id="reversed_left_curly_quotes"),
    pytest.param("cat ”/srv/data/Client Secret/deep/notes.md” now",
                 f"cat ”path-1{HIDDEN}” now", id="right_curly_quote_as_opener"),
    pytest.param("cat «/srv/data/Client Secret/deep/notes.md» now",
                 f"cat «path-1{HIDDEN}» now", id="guillemets"),
    pytest.param("cat ‹/srv/data/Client Secret/deep/notes.md› now",
                 f"cat ‹path-1{HIDDEN}› now", id="single_guillemets"),
    pytest.param("cat 「/srv/data/Client Secret/deep/notes.md」 now",
                 f"cat 「path-1{HIDDEN}」 now", id="cjk_corner_quotes"),
    pytest.param("cat 『/srv/data/Client Secret/deep/notes.md』 now",
                 f"cat 『path-1{HIDDEN}』 now", id="cjk_white_corner_quotes"),
    pytest.param("cat `/srv/data/Client Secret/deep/notes.md`.",
                 f"cat `path-1{HIDDEN}`.", id="code_span_then_dot"),
]


@pytest.mark.parametrize("level,text,expected", _grid(W52))
def test_fc5_w2_a_path_with_a_space_in_prose_is_hidden_whole(level, text, expected):
    r = _redactor(level)
    _assert_golden(r, text, expected)
    _assert_clean(r, text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc5_w2_a_registered_value_matches_with_and_without_its_trailing_slash(level):
    r = _redactor(level, "/srv/data/Client Secret/")
    text = "cd /srv/data/Client Secret && ls"
    _assert_clean(r, text)
    assert r(text) == f"cd path-1{HIDDEN}"  # F fix 7 (W7-1, R1): `&& ls` is hidden with the line
    r = _redactor(level, "/srv/data/Client Secret/")
    assert r("cd /srv/data/Client Secret/ && ls") == f"cd path-1{HIDDEN}"


JOIN_ROWS = [
    # F fix 6 (W6-2): the forward join is replaced by the end-of-line rule; the rows keep their
    # inputs and show where the span now ends
    pytest.param("cp /srv/a/b /srv/c/d now", f"cp path-1{HIDDEN}", id="two_absolute_paths"),
    pytest.param("cat /srv/x/y foo bar baz", f"cat path-1{HIDDEN}", id="no_later_slash"),
    pytest.param("cat /srv/x/y a b c d/e now", f"cat path-1{HIDDEN}",
                 id="the_fourth_following_word_joins"),
    pytest.param("cat /srv/x/y a b c d e/f now", f"cat path-1{HIDDEN}",
                 id="the_fifth_following_word_joins_too"),
    pytest.param("cat /srv/x/y https://h/z now", f"cat path-1{HIDDEN}",
                 id="a_url_is_inside_the_span"),
    pytest.param("cat /srv/x/y\nSecret/deep/n now", "cat path-1\nSecret/deep/n now",
                 id="a_newline_stops_the_join"),
    pytest.param("cat /srv/x/y; ls src/a now", f"cat path-1{HIDDEN}",  # F fix 7 (W7-1, R1)
                 id="an_operator_stops_the_join"),
    pytest.param("cat /srv/x/y b/c d/e now", f"cat path-1{HIDDEN}",
                 id="the_span_runs_through_following_slash_words"),
    # F fix 10 (W10-3, rule b): the quote after `y` may be a possessive of a cut-off path, so the
    # fragment runs to the end of the line (was: `cat 'path-1' b/c now`)
    pytest.param("cat '/srv/x/y' b/c now", f"cat 'path-1{HIDDEN}",
                 id="a_quoted_word_is_not_joined"),
]


@pytest.mark.parametrize("level,text,expected", _grid(JOIN_ROWS))
def test_fc5_w2_the_span_runs_to_the_end_of_the_line_and_fails_closed(level, text, expected):
    assert _redactor(level)(text) == expected


# --- W5-3: backslash-CRLF inside double quotes is a continuation ---------------------------------


@pytest.mark.parametrize("level", LEVELS)
def test_fc5_w3_backslash_crlf_in_double_quotes_leaks_nothing(level):
    text = 'cat "/srv/data/Client\\\r\nSecret/deep/notes.md" now'
    r = _redactor(level)
    _assert_clean(r, text)
    assert r(text) == 'cat "path-1" now'
    assert r.title(text).replace("\r", "") == 'cat "path-1" now'


@pytest.mark.parametrize("level", LEVELS)
def test_fc5_w3_backslash_crlf_unquoted_leaks_nothing_in_a_title_or_a_name(level):
    r = _redactor(level)
    text = "cat /srv/data/Client\\\r\nSecret/deep/notes.md now"
    assert "Secret" not in r.title(text) and "notes.md" not in r.title(text)
    # F fix 6 (W6-2): the continuation joins the lines, and the path hides `now` with the marker
    assert r.title(text) == f"cat path-1{HIDDEN}"
    if level == "local":
        names = _redactor(level).names
        assert names.local_text(text) == f"cat path-1{HIDDEN}"


# --- W5-4: JSON is redacted value by value --------------------------------------------------------

COMPACT = '{"path":"/srv/data/Client/notes.md","status":"failed","count":7}'
COMPACT_OUT = '{"path":"path-1","status":"failed","count":7}'


@pytest.mark.parametrize("level", LEVELS)
def test_fc5_w4_compact_json_keeps_its_keys_and_facts(level):
    r = _redactor(level)
    outs = _all_sinks(r, COMPACT)
    for sink in ("plain", "title", "json", "evidence", "diagnostic", "finding_title",
                 "finding_metrics", "leftover"):
        assert outs[sink] == COMPACT_OUT, (sink, outs[sink])
        assert json.loads(outs[sink]) == {"path": "path-1", "status": "failed", "count": 7}
    _assert_clean(r, COMPACT)


JSON_ROWS = [
    pytest.param('{"path": "/srv/x/y.json", "n": 1}', '{"path": "path-1", "n": 1}',
                 id="spaced_json_keeps_its_bytes"),
    pytest.param('["/srv/x/y","/srv/x/z",3]', '["path-1","path-2",3]', id="array"),
    pytest.param('result: {"a":"/srv/x/y"} done', 'result: {"a":"path-1"} done',
                 id="json_substring_in_prose"),
    pytest.param('{"a":{"b":["/srv/x/y",null,true,1.5]}}',
                 '{"a":{"b":["path-1",null,true,1.5]}}', id="nested"),
    pytest.param('{"msg":"cannot open /srv/data/Client Secret/deep/notes.md now"}',
                 f'{{"msg":"cannot open path-1{HIDDEN}"}}', id="a_string_value_with_prose"),
    pytest.param('{"a":"x"} and {"b":"/srv/x/y"}', '{"a":"x"} and {"b":"path-1"}',
                 id="two_json_substrings"),
    pytest.param('{"q":"say \\"hi\\" at /srv/x/y"}', '{"q":"say \\"hi\\" at path-1"}',
                 id="escapes_in_a_string_survive"),
    pytest.param('{"inner":"{\\"p\\":\\"/srv/x/y\\"}"}', '{"inner":"{\\"p\\":\\"path-1\\"}"}',
                 id="json_inside_a_json_string"),
]


@pytest.mark.parametrize("level,text,expected", _grid(JSON_ROWS))
def test_fc5_w4_json_rows(level, text, expected):
    assert _redactor(level)(text) == expected


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text", [
    pytest.param('{"/srv/data/Client/notes.md":1,"ok":true}', id="a_path_used_as_a_key"),
    pytest.param('{"path":"/srv/data/Client/notes.md","status":', id="truncated_json"),
    pytest.param('{"path":"/srv/data/Client Secret/deep/notes.md" "x":1}', id="missing_comma"),
])
def test_fc5_w4_json_that_does_not_parse_or_keys_hold_no_private_segment(level, text):
    _assert_clean(_redactor(level), text)


@pytest.mark.parametrize("level", LEVELS)
def test_fc5_w4_text_that_is_not_json_keeps_the_free_text_path(level):
    r = _redactor(level)
    assert r("[Errno 13] Permission denied: /srv/x/y") == "[Errno 13] Permission denied: path-1"
    assert r("{not json} /srv/x/z") == "{not json} path-2"


# --- W5-5: exception_text lexes single quotes with Python escapes ---------------------------------


@pytest.mark.parametrize("level", LEVELS)
def test_fc5_w5_a_python_repr_with_an_escaped_quote_leaks_nothing(level):
    path = "/srv/O'Neil \"Client\" Secret/deep/f"
    for message in (f"PosixPath({path!r})", f"cannot open {path!r} now", f"{path!r}"):
        r = _redactor(level)
        shown = r.exception_text(ValueError(message))
        for word in PRIVATE:
            assert word not in shown, (word, shown)


def test_fc5_w5_the_python_repr_row_renders_one_path_and_keeps_the_prose():
    path = "/srv/O'Neil \"Client\" Secret/deep/f"
    shown = _redactor("share").exception_text(ValueError(f"cannot open {path!r} now"))
    assert shown == f"cannot open 'path-1{HIDDEN}' now", shown


@pytest.mark.parametrize("level", LEVELS)
def test_fc5_w5_an_oserror_filename_with_both_quote_kinds_is_swapped_for_its_display(level):
    name = "/home/user/Client Secret/O'Neil \"deep\"/x"
    exc = OSError(2, "No such file or directory", name)
    assert str(exc) == f"[Errno 2] No such file or directory: {name!r}"
    shown = _redactor(level).exception_text(exc)
    for word in PRIVATE:
        assert word not in shown, (word, shown)
    if level == "share":
        # display_path (home, structural form) is what replaces repr(filename); the scanner
        # alone would have hidden a span with a space as path-N plus the marker
        assert shown == "[Errno 2] No such file or directory: '~/.../x'", shown


# --- W5-6: a rule id is never a path --------------------------------------------------------------


@pytest.mark.parametrize("level", LEVELS)
def test_fc5_w6_a_config_warning_keeps_the_mistyped_rule_id(level):
    secret = "zz" + "secret" + "9d1"
    text = f"agent-postmortem: unknown rule id 'R-NOPE-/private/client/{secret}' in [[adopted]]"
    shown = _redactor(level).scrub_leftover_paths(text)
    assert shown == "agent-postmortem: unknown rule id 'R-NOPE-path-1' in [[adopted]]", shown
    assert secret not in shown and "client" not in shown


@pytest.mark.parametrize("text,expected", [
    # F fix 6 (W6-2): a path hides the rest of its line (the rule id before it stays)
    pytest.param("id R-RETRY-SAFE-/opt/x/y here", f"id R-RETRY-SAFE-path-1{HIDDEN}", id="real_id"),
    pytest.param("id R-NO-LONG-BASH-/opt/x/y here", f"id R-NO-LONG-BASH-path-1{HIDDEN}",
                 id="long_id"),
    pytest.param("id abc-/opt/x/y here", f"id path-1{HIDDEN}", id="other_words_stay_in_the_path"),
    pytest.param("id r-nope-/opt/x/y here", f"id path-1{HIDDEN}", id="lowercase_is_not_a_rule_id"),
])
def test_fc5_w6_only_the_rule_id_shape_is_kept_before_a_path(text, expected):
    assert _redactor("share").scrub_leftover_paths(text) == expected


# --- W5-1: the namespaces a hidden span is numbered in -----------------------------------------------


def test_fc5_w1_a_hidden_span_shares_the_leftover_namespace_with_display_path():
    r = _redactor("share")
    assert r.display_path("/etc/hosts") == "path-1"
    assert r.scrub_leftover_paths('cat "/srv/a b/c" now') == f'cat "path-2{HIDDEN}" now'
    # the plain call numbers in the run's own path namespace, after a registered value
    r = _redactor("share", "/srv/known")
    assert r('cat "/srv/a b/c" now') == f'cat "path-2{HIDDEN}" now'


@pytest.mark.parametrize("level", LEVELS)
def test_fc5_w1_the_same_hidden_span_is_the_same_path_n_twice(level):
    r = _redactor(level)
    text = 'cat "/srv/a b/c" and "/srv/d e/f" and "/srv/a b/c"'
    assert r(text) == (
        f'cat "path-1{HIDDEN}" and "path-2{HIDDEN}" and "path-1{HIDDEN}"'
    )


def test_fc5_w1_a_local_name_and_a_local_line_never_show_a_repo_relative_form_for_a_hidden_span():
    r = _redactor("local", repo_root="/repo")
    # F fix 6 (W6-2): the path hides the rest of its line
    assert r.names.local_text("see /repo/src/a\\ b.py now") == f"see path-1{HIDDEN}"
    r = _redactor("local", repo_root="/repo")
    assert r.scrub_local_line('x "/repo/src/a b.py" y') == f'x "path-1{HIDDEN}" y'
    # a span without whitespace keeps the repo-relative form
    assert r.scrub_local_line('x "/repo/src/ab.py" y') == 'x "src/ab.py" y'


def test_fc5_w1_apply_paths_renders_a_hidden_registered_span_as_an_opaque_path_n():
    r = _redactor("share", "/srv/my work/out")
    shown = r.names.apply_paths("at /srv/my work/out/sub now", render=lambda token: "R")
    assert shown == f"at path-2{HIDDEN}", shown  # F fix 6 (W6-2): the span runs to the line end
    shown = r.names.apply_paths("at /srv/my work/out/sub/x now")
    assert shown == f"at path-3{HIDDEN}", shown


# --- W5-2 (c): the registered value with a trailing slash ----------------------------------------------


@pytest.mark.parametrize("level", LEVELS)
def test_fc5_w2_a_trailing_slash_registration_is_a_value_inside_a_longer_token(level):
    r = _redactor(level, "/tmp/claude-501/")
    shown = r("ls /tmp/claude-501.bak now")
    assert "claude-501" not in shown and shown == f"ls path-2{HIDDEN}", shown  # F fix 6 (W6-2)


def test_fc5_w2_a_genuine_registration_outranks_the_slashless_spelling_of_another():
    r = _redactor("share", "/srv/work/", "/srv/work")
    # F fix 6 (W6-2): the span keeps the registered display and hides the rest of the line
    assert r("cd /srv/work now") == f"cd path-2{HIDDEN}"
    assert r("cd /srv/work/ now") == f"cd path-1{HIDDEN}"
    r = _redactor("share", "/srv/work", "/srv/work/")
    assert r("cd /srv/work now") == f"cd path-1{HIDDEN}"
    assert r("cd /srv/work/ now") == f"cd path-2{HIDDEN}"
