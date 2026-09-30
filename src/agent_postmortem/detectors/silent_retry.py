"""silent_retry: a logical agent (a journal key) was restarted more than once.

The Workflow runtime silently discards a killed attempt and starts a fresh
one under the same key; nothing in the journal calls this out as a failure.
Every attempt before the last one for a key was thrown away, so its
wall-clock time is wasted work. Fires once per key that shows more than one
attempt, as a single Finding carrying every rule the retry backs in
rule_ids: R-RETRY-SAFE always, plus R-CAP-CONCURRENCY too when two or more
logical agents in the run started their first attempt within 5 seconds of
each other (simultaneous launches are the observed trigger for paired
stalls and kills) -- never two separate Findings for the same retry.

Evidence never quotes a non-final attempt's own prompt (that text is
user-authored and, for a retried key, is often the exact same prompt
repeated verbatim on every attempt -- a privacy residual, not useful
evidence). For each non-final attempt, evidence points at the kill sentinel
line that ended it, when present (a runtime marker, docs/DESIGN.md section
3, never something the agent or user wrote); an attempt whose transcript is
missing or lacks a sentinel is simply omitted. Then, for the final attempt,
one plain pointer line names where it started, with no excerpt at all:
"<transcript>:<line>: attempt <ordinal> started". See docs/DESIGN.md
section 4.3.
"""

from __future__ import annotations

from ..markers import KILL_SENTINEL_PREFIX
from ..models import Attempt, Event, Finding, Run
from .base import (
    KINDS_WORKFLOW_ONLY,
    Detector,
    attempt_seconds,
    evidence_line,
    format_duration,
    make_finding_id,
    relative_path,
)

_BURST_WINDOW_SECONDS = 5.0


def _first_event(attempt: Attempt) -> Event | None:
    return attempt.events[0] if attempt.events else None


def _kill_sentinel_event(attempt: Attempt) -> Event | None:
    """The attempt's last user event, if it starts with the kill sentinel
    (docs/DESIGN.md section 3): the runtime's own marker, never the
    attempt's own prompt text, so it is safe to quote as evidence. None when
    the attempt has no events (a missing transcript) or its last user event
    is not the sentinel."""
    last_user: Event | None = None
    for event in attempt.events:
        if event.kind == "user":
            last_user = event
    if last_user is not None and last_user.text.startswith(KILL_SENTINEL_PREFIX):
        return last_user
    return None


def _final_attempt_pointer(run: Run, attempt: Attempt) -> str | None:
    """A plain path:line pointer at the final attempt's own start, with no
    excerpt of its prompt (docs/DESIGN.md 4.5): a completed attempt's first
    line is user-authored prompt text -- often the very same prompt every
    other attempt of this key also carries -- so quoting it here would be
    the same privacy residual this detector exists to avoid. None when the
    attempt has no events (a missing transcript)."""
    first_event = _first_event(attempt)
    if first_event is None:
        return None
    path = relative_path(run, first_event.path)
    return f"{path}:{first_event.line_no}: attempt {attempt.ordinal} started"


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
            final = attempts[-1]
            wasted_seconds = sum(attempt_seconds(attempt) for attempt in non_final)

            evidence: list[str] = []
            for attempt in non_final:
                sentinel = _kill_sentinel_event(attempt)
                if sentinel is not None:
                    evidence.append(evidence_line(run, sentinel))
            final_pointer = _final_attempt_pointer(run, final)
            if final_pointer is not None:
                evidence.append(final_pointer)

            metrics = {
                "attempts": len(attempts),
                "keys": 1,
                "wasted_seconds": wasted_seconds,
                "burst": burst,
            }
            # docs/DESIGN.md 4.3: N attempts is N-1 restarts (2 attempts is
            # one restart, not two) -- the honesty rule (section 2) means the
            # title must count the thing it names, not the length of the
            # list it was counted from.
            restarts = len(attempts) - 1
            restart_word = "time" if restarts == 1 else "times"
            title = (
                f"{agent.label} restarted {restarts} {restart_word}, "
                f"{format_duration(wasted_seconds)} wasted"
            )
            agent_id = final.agent_id
            rule_ids = ("R-RETRY-SAFE", "R-CAP-CONCURRENCY") if burst else ("R-RETRY-SAFE",)
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
                rule_id=rule_ids[0],
                rule_ids=rule_ids,
                finding_id=finding_id,
            ))

        return findings


DETECTOR: Detector = SilentRetry()
