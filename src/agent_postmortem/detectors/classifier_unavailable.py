"""Detects the Bash-safety classifier going unavailable in auto mode.

DESIGN.md 4.3: minor severity, fires on every anchored, not-in-payload
'classifier_unavailable' MarkerHit (docs/DESIGN.md 4.13.2: the registry's
own shape restricts this id to a tool_result paired to Bash). Not something
a prompt or hook can fix, so there is no rule_id. metrics: count. Skips a
read_only_output event (docs/DESIGN.md 4.3's self-reference rule): a
Read/Grep/sed/etc. call quoting the outage sentence from a file on disk is
not the classifier actually going unavailable during this agent's own Bash
call.
"""

from __future__ import annotations

from .. import severity as severity_rules
from ..models import Event, Finding, Run
from .base import (
    KINDS_BOTH,
    Detector,
    anchored_marker_hits,
    event_for_marker_hit,
    evidence_line,
    make_finding_id,
    tooling_attempts,
)

ID = "classifier_unavailable"
SEVERITY = "minor"


class ClassifierUnavailable:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_BOTH

    def run(self, run: Run) -> list[Finding]:
        hits: list[Event] = []
        agent_id: str | None = None
        for attempt in tooling_attempts(run):
            for hit in anchored_marker_hits(attempt, ID):
                event = event_for_marker_hit(attempt, hit)
                if event is None or event.read_only_output:
                    continue
                hits.append(event)
                if agent_id is None:
                    agent_id = attempt.agent_id
        if not hits:
            return []

        count = len(hits)
        plural = "s" if count != 1 else ""
        title = f"Bash-safety classifier was unavailable {count} time{plural}"
        evidence = [evidence_line(run, event) for event in hits]

        return [
            Finding(
                detector=ID,
                severity=severity_rules.resolve(run, ID),
                title=title,
                run_id=run.run_id,
                agent_id=agent_id,
                evidence=evidence,
                metrics={"count": count},
                rule_id=None,
                finding_id=make_finding_id(
                    run.run_id, ID, agent_id, evidence[0] if evidence else None
                ),
            )
        ]


DETECTOR: Detector = ClassifierUnavailable()
