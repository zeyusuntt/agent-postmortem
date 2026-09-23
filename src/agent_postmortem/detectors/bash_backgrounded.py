"""Detects a Bash command the runtime moved to the background after its timeout.

DESIGN.md 4.3: minor severity, fires when a Bash tool_result contains the
backgrounded-command marker (BASH_BACKGROUNDED in markers.py: two fixed
fragments of one message, both required). metrics: count only (docs/
DESIGN.md 5.3's D3: the evidence lines already show the marker line the
detector fired on; a finding's metrics never carries a second copy of
transcript-derived free text alongside them). rule_id: R-NO-LONG-BASH.
"""

from __future__ import annotations

from ..markers import BASH_BACKGROUNDED
from ..models import Attempt, Event, Finding, Run
from .base import (
    KINDS_BOTH,
    Detector,
    evidence_line,
    is_bash_event,
    make_finding_id,
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
        hits: list[tuple[Attempt, int, Event]] = []
        for attempt in tooling_attempts(run):
            for index, event in enumerate(attempt.events):
                if not is_bash_event(event):
                    continue
                if all(marker in event.markers for marker in BASH_BACKGROUNDED):
                    hits.append((attempt, index, event))
        if not hits:
            return []

        count = len(hits)
        evidence = [evidence_line(run, event) for _, _, event in hits]
        plural = "s" if count != 1 else ""
        title = f"{count} Bash command{plural} timed out and moved to the background"

        agent_id = hits[0][0].agent_id
        return [
            Finding(
                detector=ID,
                severity=SEVERITY,
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
