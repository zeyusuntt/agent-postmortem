"""Detects a Bash command the runtime moved to the background after its timeout.

DESIGN.md 4.3: minor severity, fires on every anchored, not-in-payload
'bash_backgrounded' MarkerHit (docs/DESIGN.md 4.13.2: the registry's own
predicate already requires both BASH_BACKGROUNDED fragments, line1-anchored,
on a tool_result paired to Bash). metrics: count only (docs/DESIGN.md 5.3's
D3: the evidence lines already show the marker line the detector fired on;
a finding's metrics never carries a second copy of transcript-derived free
text alongside them). rule_id: R-NO-LONG-BASH. Never consults
Event.read_only_output (docs/DESIGN.md 4.3: a timed-out `cat` is still a
timed-out command).
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
    single_agent_id,
    tooling_attempts,
)

ID = "bash_backgrounded"
SEVERITY = "minor"
RULE_ID = "R-NO-LONG-BASH"


class BashBackgrounded:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_BOTH

    def run(self, run: Run) -> list[Finding]:
        hits: list[tuple[Attempt, Event]] = []
        for attempt in tooling_attempts(run):
            for hit in anchored_marker_hits(attempt, ID):
                event = event_for_marker_hit(attempt, hit)
                if event is not None:
                    hits.append((attempt, event))
        if not hits:
            return []

        count = len(hits)
        evidence = [evidence_line(run, event) for _, event in hits]
        plural = "s" if count != 1 else ""
        title = f"{count} Bash command{plural} timed out and moved to the background"

        agent_id = single_agent_id(attempt.agent_id for attempt, _ in hits)
        return [
            Finding(
                detector=ID,
                severity=severity_rules.resolve(run, ID),
                title=title,
                run_id=run.run_id,
                agent_id=agent_id,
                evidence=evidence,
                metrics={"count": count},
                rule_id=RULE_ID,
                finding_id=make_finding_id(
                    run.run_id, ID, agent_id, evidence[0] if evidence else None
                ),
            )
        ]


DETECTOR: Detector = BashBackgrounded()
