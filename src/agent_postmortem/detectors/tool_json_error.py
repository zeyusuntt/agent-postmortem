"""Detects a tool call whose input failed the runtime's own JSON validation.

DESIGN.md 4.3: minor severity, fires when a tool_result contains the
tool-input validation marker (TOOL_INPUT_INVALID in markers.py). This is a
runtime defect an agent cannot route around, so there is no rule_id; it goes
into the bug report instead. metrics: count, tools (the distinct tool names
that hit it). Skips a read_only_output event (docs/DESIGN.md 4.3's
self-reference rule): a Read/Grep/cat/sed/etc. call can only ever be
quoting a marker string that already exists on disk, never reporting a
tool-input failure that actually happened to this agent.
"""

from __future__ import annotations

from ..markers import TOOL_INPUT_INVALID
from ..models import Event, Finding, Run
from .base import KINDS_BOTH, Detector, evidence_line, make_finding_id, tooling_attempts

ID = "tool_json_error"
SEVERITY = "minor"

class ToolJsonError:
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
                if any(marker in event.markers for marker in TOOL_INPUT_INVALID):
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
                severity=SEVERITY,
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
