"""Detects a StructuredOutput call whose payload failed schema validation.

DESIGN.md 4.3: major severity, fires when a StructuredOutput tool_result
contains the schema-mismatch or unparsable-JSON marker
(STRUCTURED_OUTPUT_MISMATCH in markers.py: two alternative observed forms,
either is enough). metrics: count. A 'bytes' metric measuring the recovered
payload is not included: reader.py truncates every event's text to a
400-char excerpt before any detector sees it, so there is no way to recover
the original payload size here, only the length of the (already bounded)
marker text, which is not the same thing. rule_id: R-SCALAR-SCHEMA. Also
skips a read_only_output event (docs/DESIGN.md 4.3's self-reference rule),
though a StructuredOutput call is never itself read-only in practice.
"""

from __future__ import annotations

from ..markers import STRUCTURED_OUTPUT_MISMATCH
from ..models import Event, Finding, Run
from .base import KINDS_BOTH, Detector, evidence_line, make_finding_id, tooling_attempts

ID = "structured_output_failure"
SEVERITY = "major"
RULE_ID = "R-SCALAR-SCHEMA"

class StructuredOutputFailure:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_BOTH

    def run(self, run: Run) -> list[Finding]:
        hits: list[Event] = []
        agent_id: str | None = None
        for attempt in tooling_attempts(run):
            for event in attempt.events:
                if event.kind not in ("tool_result", "attachment"):
                    continue
                if event.read_only_output:
                    continue
                if event.tool_name != "StructuredOutput":
                    continue
                if not any(marker in event.markers for marker in STRUCTURED_OUTPUT_MISMATCH):
                    continue
                hits.append(event)
                if agent_id is None:
                    agent_id = attempt.agent_id
        if not hits:
            return []

        count = len(hits)
        plural = "s" if count != 1 else ""
        title = f"{count} StructuredOutput call{plural} failed schema validation"
        evidence = [evidence_line(run, event) for event in hits]
        metrics = {"count": count}

        return [
            Finding(
                detector=ID,
                severity=SEVERITY,
                title=title,
                run_id=run.run_id,
                agent_id=agent_id,
                evidence=evidence,
                metrics=metrics,
                rule_id=RULE_ID,
                finding_id=make_finding_id(
                    run.run_id, ID, agent_id, evidence[0] if evidence else None
                ),
            )
        ]


DETECTOR: Detector = StructuredOutputFailure()
