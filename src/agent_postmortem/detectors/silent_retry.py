"""silent_retry: a logical agent (a journal key) was restarted more than once.

The Workflow runtime silently discards a killed attempt and starts a fresh
one under the same key; nothing in the journal calls this out as a failure.
Every attempt before the last one for a key was thrown away, so its
wall-clock time is wasted work. Fires once per key that shows more than one
attempt. When two or more logical agents in the run started their first
attempt within 5 seconds of each other, a second Finding lands on the same
key with rule_id R-CAP-CONCURRENCY: simultaneous launches are the observed
trigger for paired stalls and kills. See docs/DESIGN.md section 4.3.
"""

from __future__ import annotations

from ..models import Attempt, Event, Finding, Run
from .base import (
    KINDS_WORKFLOW_ONLY,
    Detector,
    attempt_seconds,
    evidence_line,
    format_duration,
    make_finding_id,
)

_BURST_WINDOW_SECONDS = 5.0


def _first_event(attempt: Attempt) -> Event | None:
    return attempt.events[0] if attempt.events else None


def _run_has_burst(run: Run) -> bool:
    """True when 2+ logical agents started their first attempt within 5s."""
    starts = []
    for agent in run.agents:
        attempts = sorted(agent.attempts, key=lambda a: a.ordinal)
        if attempts and attempts[0].first_ts is not None:
            starts.append(attempts[0].first_ts)
    starts.sort()
    return any(
        (later - earlier).total_seconds() <= _BURST_WINDOW_SECONDS
        for earlier, later in zip(starts, starts[1:])
    )


class SilentRetry:
    id = "silent_retry"
    severity_default = "major"
    kinds = KINDS_WORKFLOW_ONLY

    def run(self, run: Run) -> list[Finding]:
        findings: list[Finding] = []
        burst = _run_has_burst(run)

        for agent in run.agents:
            attempts = sorted(agent.attempts, key=lambda a: a.ordinal)
            if len(attempts) <= 1:
                continue

            non_final = attempts[:-1]
            wasted_seconds = sum(attempt_seconds(attempt) for attempt in non_final)
            evidence = [
                evidence_line(run, event)
                for attempt in attempts
                for event in (_first_event(attempt),)
                if event is not None
            ]
            metrics = {
                "attempts": len(attempts),
                "keys": 1,
                "wasted_seconds": wasted_seconds,
                "burst": burst,
            }
            title = (
                f"{agent.label} restarted {len(attempts)} times, "
                f"{format_duration(wasted_seconds)} wasted"
            )
            agent_id = attempts[-1].agent_id
            finding_id = make_finding_id(
                run.run_id, self.id, agent_id, evidence[0] if evidence else None
            )

            findings.append(Finding(
                detector=self.id,
                severity=self.severity_default,
                title=title,
                run_id=run.run_id,
                agent_id=agent_id,
                evidence=evidence,
                metrics=metrics,
                rule_id="R-RETRY-SAFE",
                finding_id=finding_id,
            ))

            if burst:
                findings.append(Finding(
                    detector=self.id,
                    severity=self.severity_default,
                    title=title,
                    run_id=run.run_id,
                    agent_id=agent_id,
                    evidence=evidence,
                    metrics=metrics,
                    rule_id="R-CAP-CONCURRENCY",
                    finding_id=finding_id,
                ))

        return findings


DETECTOR: Detector = SilentRetry()
