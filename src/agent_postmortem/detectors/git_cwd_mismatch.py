"""Detects a Bash command that ran outside any git repository.

DESIGN.md 4.3: minor severity, fires when a Bash tool_result contains the
not-a-git-repository marker (GIT_NOT_REPO in markers.py). metrics: count.
rule_id: R-GIT-CWD. Skips a read_only_output event (docs/DESIGN.md 4.3's
self-reference rule): a read-only Bash call (cat, sed, git show/diff/log/
grep/blame/cat-file, and the rest) quoting the marker from a file on disk
is not a command that actually ran outside a git repository.
"""

from __future__ import annotations

from ..markers import GIT_NOT_REPO
from ..models import Event, Finding, Run
from .base import (
    KINDS_BOTH,
    Detector,
    evidence_line,
    is_bash_event,
    make_finding_id,
    tooling_attempts,
)

ID = "git_cwd_mismatch"
SEVERITY = "minor"
RULE_ID = "R-GIT-CWD"


class GitCwdMismatch:
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
                if event.read_only_output:
                    continue
                if any(marker in event.markers for marker in GIT_NOT_REPO):
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


DETECTOR: Detector = GitCwdMismatch()
