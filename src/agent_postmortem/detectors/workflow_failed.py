"""The harness itself reported this workflow run failed.

docs/DESIGN.md 4.13.3/4.13.4 (v0.3 N2): fires once per run when Run.lifecycle
.terminal == 'failed' -- an accepted terminal notification, TaskStop, or
synchronous launch result whose value is 'failed' (4.13.3), independent of
whatever the run's own journal says (a journal can report every attempt
'completed' while the harness that launched the workflow still saw it fail,
docs/DESIGN.md's field_failed_generation_completed_attempt case). major
severity (4.13.4), workflow only. Evidence is the parent transcript's own
accepted notification/TaskStop/launch-result line, `path:line`, never an
excerpt of it (the line itself is a structured handshake, not prose worth
quoting). No rule_id: section 9's rules library names none for this
detector.
"""

from __future__ import annotations

from .. import reader
from .. import severity as severity_rules
from ..models import Finding, Run
from .base import KINDS_WORKFLOW_ONLY, Detector, make_finding_id, relative_path

ID = "workflow_failed"
SEVERITY = "major"


class WorkflowFailed:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_WORKFLOW_ONLY

    def run(self, run: Run) -> list[Finding]:
        lifecycle = run.lifecycle
        if lifecycle is None or lifecycle.terminal != "failed":
            return []

        parent_path = reader.parent_transcript_path(run)
        line = reader.lifecycle_terminal_line(run)
        evidence = (
            [f"{relative_path(run, parent_path)}:{line}"]
            if parent_path is not None and line is not None
            else []
        )

        return [
            Finding(
                detector=ID,
                severity=severity_rules.resolve(run, ID, terminal=lifecycle.terminal),
                title="the harness reported this workflow failed",
                run_id=run.run_id,
                agent_id=None,
                evidence=evidence,
                metrics={},
                rule_id=None,
                finding_id=make_finding_id(
                    run.run_id, ID, None, evidence[0] if evidence else None
                ),
            )
        ]


DETECTOR: Detector = WorkflowFailed()
