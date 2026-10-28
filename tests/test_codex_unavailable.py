"""The `codex_unavailable` detector and its marker (docs/DESIGN.md 4.3, 4.13.2, 4.13.4).

A Codex companion call that could not start (the CLI is missing or lacks its runtime) leaves a
"completed" agent with no review behind it. Fixture tests/fixtures/runs/codex_unavailable has four
completed attempts:

  1  one failing companion call: the finding, major
  2  assistant prose quoting the sentence: no finding
  3  `cat` of a notes file whose text contains the sentence: no finding
  4  a failing companion call, then a successful one: the finding, minor
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_postmortem import detectors, markers, reader, severity
from agent_postmortem.detectors import codex_unavailable
from tests.n5c2_helpers import run_cli

FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "codex_unavailable"
SENTENCE = "Codex CLI is not installed or is missing required runtime support"
A1, A2, A3, A4 = (f"ae10000000000000{n}" for n in (1, 2, 3, 4))
_EVIDENCE_RE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+): (?P<excerpt>.*)$")


@pytest.fixture(scope="module")
def run():
    return reader.load_run(FIXTURE)


def _attempt(run, agent_id):
    return next(a for a in run.attempts if a.agent_id == agent_id)


def _hits(run, agent_id):
    return [h for h in _attempt(run, agent_id).marker_hits if h.marker == "codex_unavailable"]


# --- the marker ------------------------------------------------------------------------


def test_marker_is_registered_with_the_one_permitted_shape():
    spec = markers.REGISTRY["codex_unavailable"]
    assert spec.producer == "codex_companion"
    assert spec.shapes == (markers.SHAPE_TOOL_RESULT_BASH_CODEX,)


@pytest.mark.parametrize("text,expected", [
    (f"Exit code 1\n{SENTENCE}. Install it.", True),
    (f"{SENTENCE}.", True),
    (f"Exit code 1\nnote\n{SENTENCE}", True),
    (f"Exit code 1\n  {SENTENCE}", False),
    (f"Exit code 1\nError: {SENTENCE}", False),
    ("Exit code 1\nsomething else failed", False),
])
def test_marker_predicate_needs_a_line_that_starts_with_the_sentence(text, expected):
    spec = markers.REGISTRY["codex_unavailable"]
    assert spec.predicate(text, markers.SHAPE_TOOL_RESULT_BASH_CODEX) is expected


def test_the_failing_companion_call_is_an_anchored_hit(run):
    (hit,) = _hits(run, A1)
    assert hit.producer == "codex_companion"
    assert hit.anchored is True and hit.in_payload is False
    assert hit.tool_use_id == "toolu_cu_1"
    assert hit.event_line == 3


def test_prose_quoting_the_sentence_is_no_hit(run):
    assert _hits(run, A2) == []


def test_a_non_codex_bash_command_printing_the_sentence_is_no_hit(run):
    assert _hits(run, A3) == []


# --- the detector ----------------------------------------------------------------------


def test_detector_is_registered_for_both_kinds():
    ids = {d.id for d in detectors.all_detectors()}
    assert "codex_unavailable" in ids
    assert codex_unavailable.DETECTOR.kinds == detectors.base.KINDS_BOTH


def test_one_finding_per_attempt_with_a_hit_and_none_for_the_negatives(run):
    findings = codex_unavailable.DETECTOR.run(run)
    assert sorted(f.agent_id for f in findings) == [A1, A4]


def test_completed_attempt_without_a_later_success_is_major(run):
    (finding,) = [f for f in codex_unavailable.DETECTOR.run(run) if f.agent_id == A1]
    assert finding.detector == "codex_unavailable"
    assert finding.severity == "major"
    assert finding.rule_id == "R-CODEX-CONTRACT"
    assert finding.metrics == {"count": 1}
    assert finding.title == (
        "1 Codex companion call could not start: the Codex CLI is missing or lacks its runtime"
    )
    (line,) = finding.evidence
    match = _EVIDENCE_RE.match(line)
    assert match is not None and match.group("file").endswith(f"agent-{A1}.jsonl")
    assert match.group("line") == "3"


def test_a_later_successful_companion_call_makes_it_minor(run):
    (finding,) = [f for f in codex_unavailable.DETECTOR.run(run) if f.agent_id == A4]
    assert finding.severity == "minor"
    assert finding.metrics == {"count": 1}
    assert finding.evidence[0].split(": ")[0].endswith(f"agent-{A4}.jsonl:3")


def test_the_title_pluralises_and_carries_no_transcript_text(run):
    for finding in codex_unavailable.DETECTOR.run(run):
        assert "Install it" not in finding.title
        assert "/tmp" not in finding.title


def test_clean_run_has_no_finding():
    clean = reader.load_run(Path(__file__).parent / "fixtures" / "runs" / "clean_run")
    assert codex_unavailable.DETECTOR.run(clean) == []


# --- severity --------------------------------------------------------------------------


@pytest.mark.parametrize("outcome,later,expected", [
    ("completed", "no", "major"),
    ("completed", "yes", "minor"),
    ("other", "no", "minor"),
    ("other", "yes", "minor"),
])
def test_severity_table(run, outcome, later, expected):
    assert severity.resolve(run, "codex_unavailable", outcome=outcome, later_success=later) == expected


def test_severity_outside_the_vocabulary_is_a_contract_error(run):
    local = reader.load_run(FIXTURE)
    answer = severity.resolve(local, "codex_unavailable", outcome="bogus", later_success="no")
    assert answer == "major"
    assert local.status == "INCOMPLETE"


# --- the text story --------------------------------------------------------------------


def test_the_story_names_the_calls_that_could_not_start(capsys):
    _code, out, _err = run_cli(
        capsys, ["run", str(FIXTURE), "--root", str(FIXTURE.parent), "--local", "--verbose"],
    )
    assert "1 Codex call could not start" in out


# --- the session-end route -------------------------------------------------------------


def test_the_session_end_route_returns_the_finding_for_a_main_thread_call(tmp_path):
    """DESIGN 4.3, 7.2: run_tooling_detectors over a session's main thread."""
    import json

    def entry(kind, uuid, message):
        return {
            "type": kind, "timestamp": "2026-01-12T08:00:01.000Z", "uuid": uuid,
            "parentUuid": None, "cwd": "/home/user/project",
            "sessionId": "00000000-0000-4000-8000-000000000000", "version": "2.1.312",
            "message": message,
        }

    command = 'node /tmp/plugins/codex/scripts/codex-companion.mjs task --wait "review"'
    rows = [
        entry("user", "u1", {"role": "user", "content": "Run the Codex review."}),
        entry("assistant", "u2", {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_m1", "name": "Bash", "input": {"command": command}}]}),
        entry("user", "u3", {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_m1", "is_error": True,
             "content": f"Exit code 1\n{SENTENCE}. Install it and retry."}]}),
    ]
    main = tmp_path / "00000000-0000-4000-8000-000000000000.jsonl"
    main.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    view = reader.load_session_view(main, [])
    findings = detectors.run_tooling_detectors(view)
    assert [f.detector for f in findings] == ["codex_unavailable"]


# --- long commands (R7): the full command decides, not ToolCall.command ----------------


def _copy_run(tmp_path, source, agent_id, old, new):
    import shutil

    dest = tmp_path / "run"
    shutil.copytree(source, dest)
    path = dest / f"agent-{agent_id}.jsonl"
    path.write_text(path.read_text().replace(old, new), encoding="utf-8")
    return reader.load_run(dest)


def test_a_later_success_with_a_command_over_2048_chars_still_makes_it_minor(tmp_path):
    long_run = _copy_run(
        tmp_path, FIXTURE, A4, "review the change", "review the change " + "x" * 2100
    )
    calls = [c for c in _attempt(long_run, A4).tool_calls if c.tool_name == "Bash"]
    assert all(c.command is None and c.command_sha256 for c in calls)
    (finding,) = [f for f in codex_unavailable.DETECTOR.run(long_run) if f.agent_id == A4]
    assert finding.severity == "minor"


def test_an_attempt_whose_only_codex_call_is_long_keeps_the_assistant_text_shape(tmp_path):
    import shutil

    agent = "ac500000000000001"
    source = Path(__file__).parent / "fixtures" / "runs" / "codex_write_denied"
    dest = tmp_path / "run"
    shutil.copytree(source, dest)
    path = dest / f"agent-{agent}.jsonl"
    text = path.read_text().replace("proxy-review.md\"}", "proxy-review.md " + "x" * 2100 + "\"}")
    text = text.replace(
        "The sandboxed reviewer could not write",
        "[codex] WRITE_DENIED: the sandboxed reviewer could not write",
    )
    path.write_text(text, encoding="utf-8")
    long_run = reader.load_run(dest)
    attempt = next(a for a in long_run.attempts if a.agent_id == agent)
    (call,) = [c for c in attempt.tool_calls if c.tool_name == "Bash"]
    assert call.command is None
    text_hits = [
        h for h in attempt.marker_hits
        if h.marker == "sandbox_write_denied" and h.tool_use_id is None
    ]
    assert len(text_hits) == 1 and text_hits[0].anchored
