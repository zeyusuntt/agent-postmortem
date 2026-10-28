"""Detects a StructuredOutput call whose payload failed schema validation.

DESIGN.md 4.3, 4.13.4: severity comes from the `recovered` input (major unless
the attempt or a later attempt completed with a result); fires on every
anchored, not-in-payload 'structured_output_mismatch' MarkerHit (docs/DESIGN.md
4.13.2: the
registry's own shape restricts this id to a tool_result paired to
StructuredOutput). metrics: count. A 'bytes' metric measuring the recovered
payload is not included: reader.py truncates every event's text to a
400-char excerpt before any detector sees it, so there is no way to recover
the original payload size here, only the length of the (already bounded)
marker text, which is not the same thing. rule_id: R-SCALAR-SCHEMA. Also
skips a read_only_output event (docs/DESIGN.md 4.3's self-reference rule),
though a StructuredOutput call is never itself read-only in practice.
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
    single_agent_id,
    tooling_attempts,
)

ID = "structured_output_failure"
SEVERITY = "major"
RULE_ID = "R-SCALAR-SCHEMA"
_REGISTRY_MARKER = "structured_output_mismatch"


class StructuredOutputFailure:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_BOTH

    def run(self, run: Run) -> list[Finding]:
        hits: list[Event] = []
        hit_agents: list[str | None] = []
        levels: list[str] = []
        for attempt in tooling_attempts(run):
            attempt_hits = 0
            for hit in anchored_marker_hits(attempt, _REGISTRY_MARKER):
                event = event_for_marker_hit(attempt, hit)
                if event is None or event.read_only_output:
                    continue
                hits.append(event)
                attempt_hits += 1
                hit_agents.append(attempt.agent_id)
            if attempt_hits:
                # Per attempt: did a later attempt, or the attempt itself, complete
                # with a result (4.13.4)? The finding takes the highest of them.
                levels.append(severity_rules.resolve(
                    run, ID,
                    recovered=severity_rules.recovered(run, attempt, include_self=True),
                ))
        if not hits:
            return []

        # docs/DESIGN.md 4.3, 4.7: attributed to an agent only when every hit is in it.
        agent_id = single_agent_id(hit_agents)
        count = len(hits)
        plural = "s" if count != 1 else ""
        title = f"{count} StructuredOutput call{plural} failed schema validation"
        evidence = [evidence_line(run, event) for event in hits]
        metrics = {"count": count}

        return [
            Finding(
                detector=ID,
                severity=severity_rules.worst(levels),
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
