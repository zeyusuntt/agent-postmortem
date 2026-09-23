"""Detects a Bash command the runtime hard-killed after its timeout.

DESIGN.md 4.3: minor severity, fires when a Bash tool_result shows exit code
143 (BASH_TIMEOUT_EXIT_CODE) or the hard-timeout marker (BASH_TIMEOUT in
markers.py: two fixed fragments of one message, both required). metrics:
count. rule_id: R-NO-LONG-BASH.
"""

from __future__ import annotations

from ..markers import BASH_TIMEOUT, BASH_TIMEOUT_EXIT_CODE
from ..models import Event, Finding, Run
from .base import (
    KINDS_BOTH,
    Detector,
    evidence_line,
    is_bash_event,
    make_finding_id,
    tooling_attempts,
)

ID = "bash_timeout"
SEVERITY = "minor"
RULE_ID = "R-NO-LONG-BASH"


def _is_timeout(event: Event) -> bool:
    if event.exit_code == BASH_TIMEOUT_EXIT_CODE:
        return True
    return all(marker in event.markers for marker in BASH_TIMEOUT)


class BashTimeout:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_BOTH

    def run(self, run: Run) -> list[Finding]:
        hits: list[Event] = []
        agent_id: str | None = None
        for attempt in tooling_attempts(run):
            for event in attempt.events:
                if not is_bash_event(event):
                    continue
                if _is_timeout(event):
                    hits.append(event)
                    if agent_id is None:
                        agent_id = attempt.agent_id
        if not hits:
            return []

        count = len(hits)
        plural = "s" if count != 1 else ""
        title = f"{count} Bash command{plural} hit a hard timeout"
        evidence = [evidence_line(run, event) for event in hits]

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


DETECTOR: Detector = BashTimeout()
