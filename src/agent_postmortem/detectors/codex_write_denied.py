"""Detects a Codex sandbox denying a write, or the workspace shell guard
blocking a command outright.

DESIGN.md 4.3, 4.13.4: minor severity, one finding per attempt, fires on every
anchored, not-in-payload 'sandbox_write_denied' or 'shell_guard_block'
MarkerHit (docs/DESIGN.md 4.13.2). The registry's own permitted shapes
already encode what D-C used to check by hand: 'sandbox_write_denied' only
ever anchors on a Bash tool_result whose own command runs codex as a program,
or an assistant entry's own whole text when the attempt also has such a Bash
call anywhere in it; 'shell_guard_block' only ever anchors on that same
Bash-codex tool_result shape. A `user` event is never a permitted shape for
either id (D-B): a review or spec pasted into a prompt that happens to quote
WRITE_DENIED is not a sandbox denying anything.

A denial alone does not show that a required write was blocked (when a
required output is really missing, missing_artifact or claim_contradiction
carries the severity), so the finding is always minor and carries both rules
in one `rule_ids`: R-CODEX-CONTRACT first, then R-ASSERT-ARTIFACT. metrics:
count, marker (the distinct marker labels matched), both per attempt.

Skips a read_only_output event (docs/DESIGN.md 4.3's self-reference rule): a
read-only Bash command (including one buried in a loop, D-B) quoting
WRITE_DENIED or the shell-guard sentence from a file on disk is not a denial
that happened to this agent.
"""

from __future__ import annotations

from .. import severity as severity_rules
from ..models import Attempt, Event, Finding, MarkerHit, Run
from .base import (
    KINDS_BOTH,
    Detector,
    anchored_marker_hits,
    event_for_marker_hit,
    evidence_line,
    make_finding_id,
    tooling_attempts,
)

ID = "codex_write_denied"
SEVERITY = "minor"
RULE_ID_DENIAL = "R-CODEX-CONTRACT"
RULE_ID_ARTIFACT = "R-ASSERT-ARTIFACT"
RULE_IDS = (RULE_ID_DENIAL, RULE_ID_ARTIFACT)
_MARKER_IDS = ("sandbox_write_denied", "shell_guard_block")


def _attempt_hits(attempt: Attempt) -> list[tuple[Event, str]]:
    """(event, marker label) pairs for this attempt, in transcript order
    (by event_line, then tool_use_id for a stable tie-break between two
    tool_result blocks sharing a line)."""
    candidates: list[tuple[int, str, MarkerHit]] = []
    for marker in _MARKER_IDS:
        for hit in anchored_marker_hits(attempt, marker):
            candidates.append((hit.event_line, hit.tool_use_id or "", hit))
    candidates.sort(key=lambda row: (row[0], row[1]))

    hits: list[tuple[Event, str]] = []
    for _line, _tie, hit in candidates:
        event = event_for_marker_hit(attempt, hit)
        if event is None or event.read_only_output:
            continue
        hits.append((event, hit.marker))
    return hits


class CodexWriteDenied:
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
            markers = sorted({marker for _, marker in hits})
            evidence = [evidence_line(run, event) for event, _ in hits]
            plural = "s" if count != 1 else ""
            findings.append(Finding(
                detector=ID,
                severity=severity_rules.resolve(run, ID),
                title=f"{count} sandboxed write{plural} denied",
                run_id=run.run_id,
                agent_id=attempt.agent_id,
                evidence=evidence,
                metrics={"count": count, "marker": markers},
                rule_id=RULE_IDS[0],
                rule_ids=RULE_IDS,
                finding_id=make_finding_id(
                    run.run_id, ID, attempt.agent_id, evidence[0] if evidence else None
                ),
            ))
        return findings


DETECTOR: Detector = CodexWriteDenied()
