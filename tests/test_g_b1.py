"""G-B1 (v0.3.1): the fix round for the G-B review (GB-1, GB-3, GB-4, GB-8, GB-10, GB-12 and the
identity form of corpus invariant 8). One test per accepted behavior; a row that only pins an
existing behavior (a mutation survivor of the review) carries no xfail mark.
"""

from __future__ import annotations

import json

import pytest

from agent_postmortem import cli, detectors, reader
from tests import g_corpus_support
from tests.g_helpers import Session, X, Y, async_launch, sync_launch
from tests.n4b2_helpers import result, use, workflow_run
from tests.n5b_helpers import Spec, build_workflow
from tests.n5c2_helpers import run_cli
from tests.test_g_commits import _call
from tests.test_g_here import world  # noqa: F401  (a pytest fixture, used by name)
from tests.test_g_selfreport import KEY, _claim_findings, _load

SECRET = "SECRETGB1COMMAND"
LEGEND = "COMMITS ? a completed shell call contained a commit command but reported no commit id"


# --- GB-1: the G3 title counts fields, not failures ---------------------------------------------


@pytest.mark.parametrize(
    "value,count,title",
    [
        ({KEY: 1}, 1, "completed, but its own returned result reports failures in 1 field"),
        ({KEY: 500}, 1, "completed, but its own returned result reports failures in 1 field"),
        ({KEY: 5, "lint_failed": 1}, 2,
         "completed, but its own returned result reports failures in 2 fields"),
    ],
    ids=["one field", "a large value is still one field", "two fields"],
)
def test_gb1_the_title_counts_fields(tmp_path, monkeypatch, value, count, title):
    _dir, run = _load(tmp_path, monkeypatch, value)
    [finding] = _claim_findings(run)
    assert finding.title == title
    assert finding.metrics["self_reported_failure_counts"] == count
    assert "500" not in finding.title


def test_gb12_an_upper_case_failed_suffix_is_not_counted(tmp_path, monkeypatch):
    _dir, run = _load(tmp_path, monkeypatch, {"X_FAILED": 3, "Y_Failed": 2})
    assert _claim_findings(run) == []


# --- GB-3, GB-12: the permission-rule sentence -------------------------------------------------


def _rule(tool, command):
    return f"Permission to use {tool} with command {command} has been denied."


def _denial_entries(text):
    return [use("t1", "Bash", {"command": "ls"}), result("t1", text, is_error=True)]


def _counts(tmp_path, text):
    run = workflow_run(tmp_path, _denial_entries(text))
    found = [f for f in detectors.run_detectors(run) if f.detector == "permission_denied"]
    return found[0].metrics["permission_rule"] if found else None


MULTI = _rule("Bash", f'git commit -m "first line\nsecond line {SECRET}"')


@pytest.mark.parametrize(
    "name,text",
    [
        ("a multi-line command", MULTI),
        ("a multi-line command and a detail line", MULTI + "\nmore detail"),
        ("an mcp tool name", _rule("mcp__x__y", "ls")),
        ("a tool name with a digit", _rule("Tool2", "x")),
        ("a tool name with an underscore", _rule("mcp_x", "x")),
    ],
)
def test_gb3_these_rule_sentences_count(tmp_path, name, text):
    assert _counts(tmp_path, text) == 1


def test_gb3_a_sentence_that_ends_its_first_line_counts_once(tmp_path):
    text = "Permission to use Bash with command a has been denied.\nb has been denied."
    assert _counts(tmp_path, text) == 1


@pytest.mark.parametrize(
    "name,text",
    [
        ("an empty tool name", "Permission to use  with command x has been denied."),
        ("a multi-line command whose sentence continues", "Permission to use Bash with command "
         "a\nb has been denied. Retry later."),
        ("a multi-line command that never ends the sentence", "Permission to use Bash with "
         "command a\nb"),
        ("a tool name with a dash", _rule("mcp-x", "x")),
        ("a tool name with a space", _rule("Bash x", "x")),
    ],
)
def test_gb3_gb12_these_rule_sentences_do_not_count(tmp_path, name, text):
    assert _counts(tmp_path, text) is None


@pytest.mark.parametrize(
    "text",
    [MULTI, _rule("mcp__x__y", "ls"), MULTI + "\nmore detail"],
    ids=["multi-line", "mcp name", "multi-line with detail"],
)
def test_gb3_the_independent_regex_matches(text):
    assert g_corpus_support.DENIAL_RES["permission_rule"].match(text)


@pytest.mark.parametrize(
    "text",
    ["Permission to use  with command x has been denied.",
     "Permission to use Bash with command a\nb has been denied. Retry later.",
     "Permission to use Bash with command a\nb"],
    ids=["empty name", "sentence continues", "no end"],
)
def test_gb3_the_independent_regex_rejects(text):
    assert not g_corpus_support.DENIAL_RES["permission_rule"].match(text)


@pytest.mark.parametrize("level", [[], ["--local"], ["--no-redact"]], ids=["share", "local", "raw"])
def test_gb3_a_multi_line_denial_is_counted_and_never_printed(tmp_path, capsys, level):
    run = workflow_run(tmp_path, _denial_entries(MULTI))
    saw = False
    for extra in (["--verbose", "--evidence"], ["--json"], ["--bug-report"]):
        _code, out, err = run_cli(
            capsys, ["run", str(run.path), "--root", str(tmp_path / "root"), *extra, *level],
        )
        assert SECRET not in out + err, extra
        saw = saw or "permission_denied" in out
    assert saw


# --- GB-4: the COMMITS legend ------------------------------------------------------------------


def test_gb4_the_legend_names_its_column(tmp_path, capsys):
    from tests.test_g_commits import _render, _table

    table = _table(_render(tmp_path, capsys, {X: _call("git add -A; git commit -q -m x")}))
    assert LEGEND in table
    assert not [line for line in table if line.startswith("?")]


# --- GB-8: result key names at share ------------------------------------------------------------

KEYS_WF = "wf_00000000-ea1"


def _keys_payload(tmp_path, monkeypatch, capsys, level):
    run_dir = build_workflow(
        tmp_path, monkeypatch, KEYS_WF,
        [Spec("v2:a", 1, result={"zeta_note": "x", "alpha_flag": 1}),
         Spec("v2:b", 2, result={"alpha_flag": 2, "beta_secretkey": 3})],
    )
    _code, out, _err = run_cli(
        capsys, ["run", str(run_dir), "--root", str(tmp_path / "root"), "--json", *level],
    )
    return out, [a["reported"]["keys"] for a in json.loads(out)["run"]["attempts"]]


def test_gb8_share_pseudonymizes_the_keys_in_first_seen_order(tmp_path, monkeypatch, capsys):
    out, keys = _keys_payload(tmp_path, monkeypatch, capsys, [])
    assert keys == [["key-1", "key-2"], ["key-1", "key-3"]]
    for name in ("zeta_note", "alpha_flag", "beta_secretkey"):
        assert name not in out


@pytest.mark.parametrize("level", [["--local"], ["--no-redact"]], ids=["local", "raw"])
def test_gb8_local_and_raw_keep_the_real_keys(tmp_path, monkeypatch, capsys, level):
    _out, keys = _keys_payload(tmp_path, monkeypatch, capsys, level)
    assert keys == [["alpha_flag", "zeta_note"], ["alpha_flag", "beta_secretkey"]]


def _walk_reported_keys(node):
    if isinstance(node, dict):
        reported = node.get("reported")
        if isinstance(reported, dict) and isinstance(reported.get("keys"), list):
            yield from reported["keys"]
        for value in node.values():
            yield from _walk_reported_keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_reported_keys(value)


SINKS = [
    ["run", "{dir}"],
    ["run", "{dir}", "--verbose", "--evidence"],
    ["run", "{dir}", "--json"],
    ["run", "{dir}", "--bug-report"],
    ["rescue", "plan", "{dir}"],
    ["rescue", "plan", "{dir}", "--json"],
]


@pytest.mark.parametrize(
    "level,at_share",
    [([], True), (["--local"], False), (["--no-redact"], False)],
    ids=["share", "local", "raw"],
)
def test_gb8_the_complete_outputs_name_a_key_only_where_the_level_allows(
    tmp_path, monkeypatch, capsys, level, at_share,
):
    run_dir = build_workflow(tmp_path, monkeypatch, KEYS_WF, [Spec("v2:x", 1, result={KEY: 1})])
    saw = False
    for template in SINKS:
        argv = [a.replace("{dir}", str(run_dir)) for a in template]
        _code, out, err = run_cli(capsys, [*argv, "--root", str(tmp_path / "root"), *level])
        text = out + err
        listed = 0
        if "--json" in argv and "rescue" not in argv:
            listed = sum(1 for key in _walk_reported_keys(json.loads(out)) if key == KEY)
        # the whole text is searched: nothing is stripped before the count
        assert text.count(KEY) == (0 if at_share else listed), (template, level)
        saw = saw or "claim_contradiction" in out
    assert saw


# --- GB-10: invariant 10, the predicate against the independent regex -------------------------


def _denial_run(tmp_path, texts):
    entries = []
    for number, text in enumerate(texts):
        entries += [use(f"t{number}", "Bash", {"command": "ls"}),
                    result(f"t{number}", text, is_error=True)]
    return workflow_run(tmp_path, entries)


ALL_KINDS = [
    "Permission for this action was denied by the Claude Code auto mode classifier.",
    "The user doesn't want to proceed with this tool use.",
    "Permission for this command was denied by a built-in Claude Code safety check, "
    "not by the user.",
    _rule("Bash", "ls"),
    MULTI,
    _rule("mcp__x__y", "ls"),
]


def test_gb10_a_passing_fixture_agrees_through_the_collector(tmp_path):
    run = _denial_run(tmp_path, [t for t in ALL_KINDS if t is not MULTI and "mcp__" not in t])
    tally, agree = g_corpus_support.collect_inv10(run, cli._run_findings(run))
    assert (tally.violations, agree.violations, agree.checked) == (0, 0, 1)


def test_gb10_the_multi_line_forms_agree_through_the_collector(tmp_path):
    run = _denial_run(tmp_path, ALL_KINDS)
    shared, regex = g_corpus_support.raw_denial_counts(run.attempts[0].transcript_path)
    assert shared["permission_rule"] == regex["permission_rule"] == 3
    tally, agree = g_corpus_support.collect_inv10(run, cli._run_findings(run))
    assert (tally.violations, agree.violations) == (0, 0)


def test_gb10_a_disagreeing_predicate_is_a_violation_of_the_agreement_leg(tmp_path, monkeypatch):
    from agent_postmortem import markers

    run = _denial_run(tmp_path, [ALL_KINDS[0], ALL_KINDS[1]])
    monkeypatch.setattr(markers, "denial_kind", lambda text: "safety_check")
    _tally, agree = g_corpus_support.collect_inv10(run, [])
    assert (agree.checked, agree.violations) == (1, 1)


# --- G8 invariant 8 by identity ----------------------------------------------------------------


def _same_label_session(tmp_path):
    s = Session(tmp_path)
    s.main(async_launch("t1", X, 1), sync_launch("t2", Y, 5, "completed"))
    s.child(X, meta={"description": "review"})
    s.child(Y, meta={"description": "review"})
    s.build()
    return s, reader.load_session(s.main_path)


def test_inv8_identity_pure():
    g = g_corpus_support
    assert g.inv8({"a": "completed", "b": "open"}, {"b"}).violations == 0
    assert g.inv8({"a": "completed", "b": "open"}, {"a"}).violations == 1
    assert g.inv8({"a": "completed"}, set()).violations == 0


def test_inv8_two_subagents_with_one_label_are_no_violation(tmp_path, capsys):
    s, run = _same_label_session(tmp_path)
    states = {a.agent_id: a.return_state for a in run.attempts}
    assert states[X] != "returned" and states[Y] == "returned"
    assert len({a.label for a in run.attempts}) == 1
    tally = g_corpus_support.collect_inv8(run)
    assert (tally.checked, tally.violations) == (1, 0)
    text = run_cli(capsys, ["run", str(s.main_path), "--root", str(s.root)])[1]
    # one share name for both: the limit DESIGN 4.7 records
    assert "agent-2" not in text


def test_inv8_a_listed_child_with_a_completed_outcome_is_a_violation(tmp_path, monkeypatch):
    from agent_postmortem import report

    _s, run = _same_label_session(tmp_path)
    stopped = next(a for a in run.attempts if a.agent_id == X)
    returned = next(a for a in run.attempts if a.agent_id == Y)
    monkeypatch.setattr(report, "_unreturned_subagents", lambda r, p: [returned])
    tally = g_corpus_support.collect_inv8(run)
    assert tally.violations == 1 and tally.detail["agents"] == 1
    monkeypatch.setattr(report, "_unreturned_subagents", lambda r, p: [stopped])
    assert g_corpus_support.collect_inv8(run).violations == 0


# --- GB-12: the G5 resolver lists directories only --------------------------------------------


def test_gb12_a_plain_file_named_wf_is_not_a_workflow_directory(world):  # noqa: F811
    from tests.n6_helpers import SLUG_A, uid
    from tests.test_g_here import WF_A

    workflows = world["root"] / SLUG_A / uid(1) / "subagents" / "workflows"
    (workflows / "wf_00000000-ea9").write_text("not a directory", encoding="utf-8")
    found = reader.caller_workflow_paths(world["root"], uid(1), world["cwd"])
    assert {path.name for path in found} == {WF_A}


def test_gb12_a_symlink_named_wf_to_a_file_is_not_a_workflow_directory(world):  # noqa: F811
    import os

    from tests.n6_helpers import SLUG_A, uid
    from tests.test_g_here import WF_A

    workflows = world["root"] / SLUG_A / uid(1) / "subagents" / "workflows"
    target = world["tmp"] / "plain.txt"
    target.write_text("x", encoding="utf-8")
    os.symlink(target, workflows / "wf_00000000-ea8")
    found = reader.caller_workflow_paths(world["root"], uid(1), world["cwd"])
    assert {path.name for path in found} == {WF_A}
