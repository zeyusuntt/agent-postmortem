"""Detects a Codex companion call that could not start.

DESIGN.md 4.3, 4.13.2, 4.13.4: one finding per attempt that holds at least one anchored,
not-in-payload `codex_unavailable` MarkerHit (the main thread of a session included, its finding
carrying no agent id). The marker's only permitted shape is a Bash tool_result whose command runs
codex as a program or names codex-companion.mjs, and its predicate needs a line that starts with
the companion's own sentence, so assistant prose and the output of a non-codex command (a `cat` of
a notes file) never count.

Severity goes through severity.py: major when the attempt's outcome is `completed` (it returned a
result though its Codex call never started) and no companion call after the first failing one has
a result without the marker; minor otherwise (a later call succeeded, or the attempt was killed or
failed, which other findings carry). The title is a fixed sentence and carries no transcript text.
A read_only_output event is skipped, as for codex_write_denied.
"""

from __future__ import annotations

from .. import severity as severity_rules
from ..models import Attempt, Event, Finding, Run
from .base import (
    KINDS_BOTH,
    Detector,
    anchored_marker_hits,
    event_for_marker_hit,
    evidence_line,
    make_finding_id,
    tooling_attempts,
)

ID = "codex_unavailable"
SEVERITY = "major"
RULE_ID = "R-CODEX-CONTRACT"


def _attempt_hits(attempt: Attempt) -> list[tuple[Event, str]]:
    """(event, tool_use_id) of every counted hit, in transcript order."""
    ordered = sorted(
        anchored_marker_hits(attempt, ID), key=lambda hit: (hit.event_line, hit.tool_use_id or ""),
    )
    found: list[tuple[Event, str]] = []
    for hit in ordered:
        event = event_for_marker_hit(attempt, hit)
        if event is None or event.read_only_output or hit.tool_use_id is None:
            continue
        found.append((event, hit.tool_use_id))
    return found


def _later_success(attempt: Attempt, hits: list[tuple[Event, str]]) -> bool:
    """Whether a Bash companion call after the first failing one has a result without the
    marker."""
    failed_ids = {tool_use_id for _event, tool_use_id in hits}
    first = next((c for c in attempt.tool_calls if c.tool_use_id == hits[0][1]), None)
    if first is None:
        return False
    for call in attempt.tool_calls:
        if call.tool_name != "Bash" or call.line_no <= first.line_no:
            continue
        if not call.runs_codex_program:
            continue
        if call.result_line is not None and call.tool_use_id not in failed_ids:
            return True
    return False


class CodexUnavailable:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_BOTH

    def run(self, run: Run) -> list[Finding]:
        findings: list[Finding] = []
        for attempt in tooling_attempts(run):
            hits = _attempt_hits(attempt)
            if not hits:
                continue
            count = len(hits)
            evidence = [evidence_line(run, event) for event, _id in hits]
            agent_id = None if attempt is run.main else attempt.agent_id
            findings.append(Finding(
                detector=ID,
                severity=severity_rules.resolve(
                    run, ID,
                    outcome="completed" if attempt.outcome == "completed" else "other",
                    later_success="yes" if _later_success(attempt, hits) else "no",
                ),
                title=(
                    f"{count} Codex companion {'call' if count == 1 else 'calls'} could not "
                    "start: the Codex CLI is missing or lacks its runtime"
                ),
                run_id=run.run_id,
                agent_id=agent_id,
                evidence=evidence,
                metrics={"count": count},
                rule_id=RULE_ID,
                finding_id=make_finding_id(run.run_id, ID, agent_id, evidence[0]),
            ))
        return findings


DETECTOR: Detector = CodexUnavailable()
