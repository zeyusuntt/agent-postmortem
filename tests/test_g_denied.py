"""G-B, G7 (v0.3.1): the `permission_denied` detector (docs/DESIGN.md 4.3, 4.13.2, 4.13.4).

A tool result with `is_error` whose text STARTS with one of four runtime sentences. One finding
per attempt (the main thread of a session included, with no agent id), minor, a title with the
four counts, evidence of up to 5 `file:line` pointers, and no denied command or text anywhere.
"""

from __future__ import annotations

import json

import pytest

from agent_postmortem import detectors, markers, reader, severity
from tests.n4b2_helpers import result, session_run, use, workflow_run
from tests.n5b_helpers import Spec, build_workflow
from tests.n5c2_helpers import run_cli

AUTO = "Permission for this action was denied by the Claude Code auto mode classifier."
USER = "The user doesn't want to proceed with this tool use."
SAFETY = (
    "Permission for this command was denied by a built-in Claude Code safety check, "
    "not by the user."
)
SECRET = "SECRETDENIEDCOMMAND"


def rule(tool="Bash", command=f"git push origin {SECRET}"):
    return f"Permission to use {tool} with command {command} has been denied."


# (name, text, is_error, expected counts as (auto, user, rule, safety) or None for not counted)
ROWS = [
    ("auto mode", AUTO, True, (1, 0, 0, 0)),
    ("auto mode with a trailing explanation", AUTO + " Try a safer approach.", True, (1, 0, 0, 0)),
    ("user", USER, True, (0, 1, 0, 0)),
    ("user with the runtime's follow-up", USER + " The tool use was rejected.", True, (0, 1, 0, 0)),
    ("safety check", SAFETY, True, (0, 0, 0, 1)),
    ("permission rule, Bash", rule(), True, (0, 0, 1, 0)),
    ("permission rule, another tool", rule("Read", "x"), True, (0, 0, 1, 0)),
    ("permission rule, a long command", rule(command="echo " + "a" * 6000), True, (0, 0, 1, 0)),
    ("permission rule, a second line follows", rule() + "\nmore detail", True, (0, 0, 1, 0)),
    ("permission rule, a tool name holding a digit", rule("Tool2", "x"), True, (0, 0, 1, 0)),
    ("permission rule, a tool name holding an underscore", rule("mcp_x", "x"), True,
     (0, 0, 1, 0)),
    ("permission rule, an mcp tool", rule("mcp__x__y", "x"), True, (0, 0, 1, 0)),
    ("permission rule, a multi-line command", rule(command="git commit -m 'a\nb'"), True,
     (0, 0, 1, 0)),
    ("leading whitespace is stripped", "  \n" + AUTO, True, (1, 0, 0, 0)),
    # not counted
    ("non-error auto mode sentence", AUTO, False, None),
    ("non-error user sentence", USER, False, None),
    ("non-error rule sentence", rule(), False, None),
    ("sentence mid-text", "Exit code 1\n" + AUTO, True, None),
    ("sentence after other words", "Note: " + USER, True, None),
    ("wrapped in tool_use_error", "<tool_use_error>" + AUTO + "</tool_use_error>", True, None),
    ("wrapped rule", "<tool_use_error>" + rule() + "</tool_use_error>", True, None),
    ("sleep block", "Blocked: sleep 30 followed by something; use run_in_background", True, None),
    ("hook error", "PreToolUse:Bash hook error: " + AUTO, True, None),
    ("rule granted", "Permission to use Bash with command ls has been granted.", True, None),
    ("rule required", "Permission to use Bash with command ls is required.", True, None),
    ("rule unavailable", "Permission to use Bash with command ls is unavailable.", True, None),
    ("rule without the first line ending", rule() + " Retry later.", True, None),
    ("rule with a tool name holding a dash", rule("mcp-x", "x"), True, None),
    ("rule with an empty tool name", "Permission to use  with command x has been denied.",
     True, None),
    ("rule without a command", "Permission to use Bash has been denied.", True, None),
    ("auto mode granted", "Permission for this action was granted by the auto mode classifier.",
     True, None),
    ("user variant", "The user wants to proceed with this tool use.", True, None),
    ("a different sentence", "Permission denied: /etc/passwd", True, None),
    ("safety without the user clause", "Permission for this command was denied by a built-in "
     "Claude Code safety check.", True, None),
]
PARAMS = [pytest.param(*row, id=row[0]) for row in ROWS]


def denial_findings(run):
    return [f for f in detectors.run_detectors(run) if f.detector == "permission_denied"]


def _entries(text, is_error, tool="Bash"):
    return [use("t1", tool, {"command": "ls"} if tool == "Bash" else {"file_path": "/tmp/x"}),
            result("t1", text, is_error=is_error)]


def test_permission_denied_is_registered_minor_with_no_inputs():
    assert "permission_denied" in {d.id for d in detectors.all_detectors()}
    assert severity.INPUT_NAMES["permission_denied"] == ()
    assert severity.RULES["permission_denied"] == [((), "minor")]


def test_the_detector_applies_to_both_run_kinds_and_is_not_a_hook_detector():
    detector = next(d for d in detectors.all_detectors() if d.id == "permission_denied")
    assert detector.kinds == frozenset({"workflow", "session"})
    assert "permission_denied" not in detectors.TOOLING_DETECTOR_IDS


@pytest.mark.parametrize("name,text,is_error,counts", PARAMS)
def test_g7_grammar_rows(tmp_path, name, text, is_error, counts):
    run = workflow_run(tmp_path, _entries(text, is_error))
    findings = denial_findings(run)
    if counts is None:
        assert findings == []
        return
    [finding] = findings
    auto, user, rule_count, safety = counts
    assert finding.severity == "minor"
    assert finding.rule_id is None
    assert finding.metrics == {
        "count": 1, "auto_mode": auto, "user": user, "permission_rule": rule_count,
        "safety_check": safety,
    }


@pytest.mark.parametrize(
    "counts,title",
    [
        ((1, 0, 0, 0), "1 tool call denied (auto mode 1)"),
        ((0, 2, 0, 0), "2 tool calls denied (user 2)"),
        ((0, 0, 1, 0), "1 tool call denied (permission rule 1)"),
        ((0, 0, 0, 3), "3 tool calls denied (safety check 3)"),
        ((1, 2, 3, 4),
         "10 tool calls denied (auto mode 1, user 2, permission rule 3, safety check 4)"),
        ((2, 0, 0, 1), "3 tool calls denied (auto mode 2, safety check 1)"),
    ],
)
def test_g7_title_omits_zero_counts_and_keeps_the_order(tmp_path, counts, title):
    texts = [AUTO] * counts[0] + [USER] * counts[1] + [rule()] * counts[2] + [SAFETY] * counts[3]
    entries = []
    for number, text in enumerate(texts):
        tool_id = f"t{number}"
        entries += [use(tool_id, "Bash", {"command": "ls"}), result(tool_id, text, is_error=True)]
    [finding] = denial_findings(workflow_run(tmp_path, entries))
    assert finding.title == title
    assert finding.metrics["count"] == sum(counts)


def test_g7_evidence_is_capped_at_five_pointers_without_text(tmp_path):
    entries = []
    for number in range(7):
        entries += [use(f"t{number}", "Bash", {"command": "ls"}),
                    result(f"t{number}", rule(), is_error=True)]
    run = workflow_run(tmp_path, entries)
    [finding] = denial_findings(run)
    assert finding.metrics["count"] == 7
    assert len(finding.evidence) == 5
    transcript = run.attempts[0].transcript_path.name
    assert finding.evidence == [f"{transcript}:{line}" for line in (2, 4, 6, 8, 10)]
    assert all(SECRET not in line for line in finding.evidence)


@pytest.mark.parametrize("name,text,is_error,counts", [r for r in ROWS if r[3] is None and r[2]],
                         ids=[r[0] for r in ROWS if r[3] is None and r[2]])
def test_g7_a_text_that_is_not_a_denial_makes_no_anchored_hit(tmp_path, name, text, is_error,
                                                              counts):
    run = workflow_run(tmp_path, _entries(text, is_error))
    hits = [h for h in run.attempts[0].marker_hits if h.marker == "permission_denied"]
    assert [h for h in hits if h.anchored] == []


def test_g7_the_marker_hit_is_anchored_only_at_the_start(tmp_path):
    entries = [
        *_entries(AUTO, True),
        use("t2", "Bash", {"command": "ls"}),
        result("t2", "Exit code 1\n" + AUTO, is_error=True),
    ]
    run = workflow_run(tmp_path, entries)
    hits = [h for h in run.attempts[0].marker_hits if h.marker == "permission_denied"]
    assert [(h.anchored, h.in_payload) for h in hits] == [(True, False), (False, True)]
    assert markers.REGISTRY["permission_denied"].producer == "harness_tool_notice"


def test_g7_a_non_error_result_makes_no_hit_at_all(tmp_path):
    run = workflow_run(tmp_path, _entries(AUTO, False))
    assert [h for h in run.attempts[0].marker_hits if h.marker == "permission_denied"] == []


def test_g7_one_finding_per_attempt(tmp_path, monkeypatch):
    specs = [
        Spec("v2:a", 1, tools=[("t1", "Bash", {"command": "ls"}, AUTO, True)]),
        Spec("v2:b", 2, tools=[("t1", "Bash", {"command": "ls"}, USER, True),
                               ("t2", "Bash", {"command": "ls"}, USER, True)]),
        Spec("v2:c", 3, tools=[("t1", "Bash", {"command": "ls"}, "fine", False)]),
    ]
    run = reader.load_run(build_workflow(tmp_path, monkeypatch, "wf_00000000-ea2", specs))
    findings = {f.agent_id: f for f in denial_findings(run)}
    assert set(findings) == {run.attempts[0].agent_id, run.attempts[1].agent_id}
    assert findings[run.attempts[0].agent_id].metrics["count"] == 1
    assert findings[run.attempts[1].agent_id].metrics["count"] == 2


def test_g7_a_session_main_thread_finding_has_no_agent_id(tmp_path):
    main = [use("m1", "Bash", {"command": "ls"}), result("m1", SAFETY, is_error=True)]
    child = [use("c1", "Bash", {"command": "ls"}), result("c1", AUTO, is_error=True)]
    child_id = "a" + "5" * 16
    run = session_run(tmp_path, [{"type": "user", "message": {"role": "user", "content": "go"}},
                                 *main], {child_id: child})
    findings = denial_findings(run)
    assert sorted(f.agent_id or "" for f in findings) == ["", child_id]
    main_finding = next(f for f in findings if f.agent_id is None)
    assert main_finding.metrics["safety_check"] == 1
    assert main_finding.evidence[0].endswith(".jsonl:3")


@pytest.mark.parametrize("level", [[], ["--local"], ["--no-redact"]], ids=["share", "local", "raw"])
def test_g7_no_denied_command_or_text_in_any_sink(tmp_path, capsys, level):
    entries = [use("t1", "Bash", {"command": f"git push {SECRET}"}),
               result("t1", rule(), is_error=True)]
    run = workflow_run(tmp_path, entries)
    saw = False
    for extra in (["--verbose", "--evidence"], ["--json"], ["--bug-report"]):
        _code, out, err = run_cli(
            capsys, ["run", str(run.path), "--root", str(tmp_path / "root"), *extra, *level],
        )
        assert SECRET not in out + err, extra
        saw = saw or "permission_denied" in out
    assert saw


def test_g7_evidence_carries_the_marker_code_and_a_pointer_only(tmp_path, capsys):
    run = workflow_run(tmp_path, _entries(AUTO, True))
    root = str(tmp_path / "root")
    _code, out, _err = run_cli(
        capsys, ["run", str(run.path), "--root", root, "--local", "--verbose"],
    )
    line = next(line for line in out.splitlines() if line.strip().startswith("agent-"))
    assert line.strip().endswith(".jsonl:2 [permission_denied]")
    _code, out, _err = run_cli(
        capsys, ["run", str(run.path), "--root", root, "--local", "--verbose", "--evidence"],
    )
    line = next(line for line in out.splitlines() if line.strip().startswith("agent-"))
    assert line.strip().endswith(".jsonl:2")
    _code, out, _err = run_cli(capsys, ["run", str(run.path), "--root", root, "--verbose"])
    assert any(
        line.strip().endswith(".jsonl:2 [permission_denied]") for line in out.splitlines()
    )


def test_g7_the_story_names_the_denials(tmp_path, capsys):
    run = workflow_run(tmp_path, _entries(AUTO, True))
    _code, out, _err = run_cli(
        capsys, ["run", str(run.path), "--root", str(tmp_path / "root"), "--local", "--verbose"],
    )
    assert "impl  1 tool call denied." in out


def test_g7_scan_totals_count_the_detector(tmp_path, capsys):
    run = workflow_run(tmp_path, _entries(AUTO, True))
    _code, out, _err = run_cli(
        capsys, ["scan", "--all", "--json", "--root", str(tmp_path / "root")],
    )
    payload = json.loads(out)
    assert payload["totals"]["findings_per_detector"]["permission_denied"] == 1
    assert run.kind == "workflow"
