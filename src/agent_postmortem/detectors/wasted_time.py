"""wasted_time: one info-level summary Finding per run of wall-clock burned.

Always fires, even on a clean run with nothing to report (wasted_seconds and
wasted_tokens are then zero), so the headline number this tool exists to
produce is always present. wasted_seconds and wasted_tokens sum only the
non-final attempts of each logical agent (the ones a retry threw away);
run_seconds and attempt_seconds cover the whole run.

A session run has no retries to observe (docs/DESIGN.md 4.7), so its
metrics are a different, smaller set: run_seconds (the same whole-run span),
child_seconds (the sum of every subagent's own wall-clock span, not just
non-final ones -- there is no "non-final" for a session), and child_count
(how many subagents ran). See docs/DESIGN.md section 4.3.
"""

from __future__ import annotations

from ..models import Attempt, Finding, Run
from .base import KINDS_BOTH, Detector, attempt_seconds, format_duration, make_finding_id


def _sum_usage(attempts: list[Attempt]) -> int:
    total = 0
    for attempt in attempts:
        for value in attempt.usage.values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += value
    return int(total)


def _non_final_attempts(run: Run) -> list[Attempt]:
    non_final: list[Attempt] = []
    for agent in run.agents:
        attempts = sorted(agent.attempts, key=lambda a: a.ordinal)
        non_final.extend(attempts[:-1])
    return non_final


def _run_seconds(run: Run) -> float:
    if run.started_at is None or run.ended_at is None:
        return 0.0
    return max((run.ended_at - run.started_at).total_seconds(), 0.0)


def _finding(run: Run, title: str, metrics: dict) -> Finding:
    return Finding(
        detector="wasted_time",
        severity="info",
        title=title,
        run_id=run.run_id,
        agent_id=None,
        evidence=[],
        metrics=metrics,
        rule_id=None,
        finding_id=make_finding_id(run.run_id, "wasted_time", None, None),
    )


def _workflow_finding(run: Run) -> Finding:
    non_final = _non_final_attempts(run)
    run_seconds = _run_seconds(run)
    total_attempt_seconds = sum(attempt_seconds(attempt) for attempt in run.attempts)
    wasted_seconds = sum(attempt_seconds(attempt) for attempt in non_final)
    wasted_tokens = _sum_usage(non_final)

    title = (
        f"run spent {format_duration(run_seconds)} wall-clock; "
        f"{format_duration(wasted_seconds)} wasted across {len(non_final)} retried attempts"
    )
    metrics = {
        "run_seconds": run_seconds,
        "attempt_seconds": total_attempt_seconds,
        "wasted_seconds": wasted_seconds,
        "wasted_tokens": wasted_tokens,
    }
    return _finding(run, title, metrics)


def _session_finding(run: Run) -> Finding:
    run_seconds = _run_seconds(run)
    child_seconds = sum(attempt_seconds(attempt) for attempt in run.attempts)
    child_count = len(run.attempts)

    title = (
        f"session ran {format_duration(run_seconds)}; {child_count} subagents "
        f"used {format_duration(child_seconds)} of agent time"
    )
    metrics = {
        "run_seconds": run_seconds,
        "child_seconds": child_seconds,
        "child_count": child_count,
    }
    return _finding(run, title, metrics)


class WastedTime:
    id = "wasted_time"
    severity_default = "info"
    kinds = KINDS_BOTH

    def run(self, run: Run) -> list[Finding]:
        if run.kind == "session":
            return [_session_finding(run)]
        return [_workflow_finding(run)]


DETECTOR: Detector = WastedTime()
