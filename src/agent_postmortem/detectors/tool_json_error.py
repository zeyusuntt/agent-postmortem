"""Detects a tool call whose input failed the runtime's own JSON validation.

DESIGN.md 4.3: minor severity, fires on every anchored, not-in-payload
'tool_input_invalid' MarkerHit (docs/DESIGN.md 4.13.2: the registry's own
shape already excludes a StructuredOutput call, which gets its own
'structured_output_mismatch' id instead). This is a runtime defect an agent
cannot route around, so there is no rule_id; it goes into the bug report
instead. metrics: count, tools (the distinct tool names that hit it). Skips
a read_only_output event (docs/DESIGN.md 4.3's self-reference rule): a
Read/Grep/cat/sed/etc. call can only ever be quoting a marker string that
already exists on disk, never reporting a tool-input failure that actually
happened to this agent.
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

ID = "tool_json_error"
SEVERITY = "minor"
_REGISTRY_MARKER = "tool_input_invalid"


class ToolJsonError:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_BOTH

    def run(self, run: Run) -> list[Finding]:
        hits: list[Event] = []
        agent_id: str | None = None
        for attempt in tooling_attempts(run):
            for hit in anchored_marker_hits(attempt, _REGISTRY_MARKER):
                event = event_for_marker_hit(attempt, hit)
                if event is None or event.read_only_output:
                    continue
                hits.append(event)
                if agent_id is None:
                    agent_id = attempt.agent_id
        if not hits:
            return []

        count = len(hits)
        tools = sorted({event.tool_name for event in hits if event.tool_name})
        plural = "s" if count != 1 else ""
        title = f"{count} tool call{plural} failed input validation"
        evidence = [evidence_line(run, event) for event in hits]

        return [
            Finding(
                detector=ID,
                severity=severity_rules.resolve(run, ID),
                title=title,
                run_id=run.run_id,
                agent_id=agent_id,
                evidence=evidence,
                metrics={"count": count, "tools": tools},
                rule_id=None,
                finding_id=make_finding_id(
                    run.run_id, ID, agent_id, evidence[0] if evidence else None
                ),
            )
        ]


DETECTOR: Detector = ToolJsonError()
