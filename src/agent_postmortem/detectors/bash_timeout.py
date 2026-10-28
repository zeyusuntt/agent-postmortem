"""Detects a Bash command the runtime hard-killed after its timeout.

DESIGN.md 4.3: minor severity, fires when a Bash tool_result shows exit code
143 (BASH_TIMEOUT_EXIT_CODE, read from Event.exit_code directly -- never a
marker) or carries an anchored, not-in-payload 'bash_timeout' MarkerHit
(docs/DESIGN.md 4.13.2: the registry's own predicate already requires
line1 == 'Exit code 143' and line2 to start with 'Command timed out after').
These two triggers are independent (M1 review): an exit code alone, with no
explanatory second line (an in-memory build, a truncated result), must still
count. metrics: count. rule_id: R-NO-LONG-BASH.

Title (field milestone F, P10): "hit a hard timeout" only when every hit carries the anchored
marker; when any hit is the exit code alone, "ended with exit code 143 (a hard timeout or
another termination)". Severity and metrics do not change.
"""

from __future__ import annotations

from .. import severity as severity_rules
from ..markers import BASH_TIMEOUT_EXIT_CODE
from ..models import Event, Finding, Run
from .base import (
    KINDS_BOTH,
    Detector,
    anchored_marker_hits,
    event_for_marker_hit,
    evidence_line,
    is_bash_event,
    make_finding_id,
    single_agent_id,
    tooling_attempts,
)

ID = "bash_timeout"
SEVERITY = "minor"
RULE_ID = "R-NO-LONG-BASH"


class BashTimeout:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_BOTH

    def run(self, run: Run) -> list[Finding]:
        hits: list[Event] = []
        exit_code_only = False  # P10: some hit has no anchored timeout marker
        hit_agents: list[str] = []
        for attempt in tooling_attempts(run):
            marker_events = {
                id(event)
                for hit in anchored_marker_hits(attempt, ID)
                if (event := event_for_marker_hit(attempt, hit)) is not None
            }
            for event in attempt.events:
                if not is_bash_event(event):
                    continue
                if event.exit_code == BASH_TIMEOUT_EXIT_CODE or id(event) in marker_events:
                    hits.append(event)
                    if id(event) not in marker_events:
                        exit_code_only = True
                    hit_agents.append(attempt.agent_id)
        if not hits:
            return []
        agent_id = single_agent_id(hit_agents)

        count = len(hits)
        plural = "s" if count != 1 else ""
        if exit_code_only:
            title = (
                f"{count} Bash command{plural} ended with exit code 143 "
                "(a hard timeout or another termination)"
            )
        else:
            title = f"{count} Bash command{plural} hit a hard timeout"
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
                rule_id=RULE_ID,
                finding_id=make_finding_id(
                    run.run_id, ID, agent_id, evidence[0] if evidence else None
                ),
            )
        ]


DETECTOR: Detector = BashTimeout()
