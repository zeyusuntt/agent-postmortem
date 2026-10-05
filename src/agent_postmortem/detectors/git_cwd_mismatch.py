"""Detects a Bash command that ran outside any git repository.

DESIGN.md 4.3: minor severity, fires on every anchored, not-in-payload
'git_not_repo' MarkerHit (docs/DESIGN.md 4.13.2: the registry's own shape
restricts this id to a tool_result paired to Bash). metrics: count.
rule_id: R-GIT-CWD. Skips a read_only_output event (docs/DESIGN.md 4.3's
self-reference rule): a read-only Bash call (cat, sed, git show/diff/log/
grep/blame/cat-file, and the rest) quoting the marker from a file on disk
is not a command that actually ran outside a git repository.
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

ID = "git_cwd_mismatch"
SEVERITY = "minor"
RULE_ID = "R-GIT-CWD"
_REGISTRY_MARKER = "git_not_repo"


class GitCwdMismatch:
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
        plural = "s" if count != 1 else ""
        title = f"{count} Bash command{plural} ran outside a git repository"
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


DETECTOR: Detector = GitCwdMismatch()
