"""mislabeled_kill: an attempt's transcript ends with the runtime's kill
sentinel, which Claude Code logs as "[Request interrupted by user]" even
when no human pressed anything. reader.py sets Attempt.interrupt for every
such attempt and attributes its cause against the parent transcript (docs/
DESIGN.md 4.4); this detector turns each attributed interrupt into a
Finding whose severity, title, and rule_id follow that cause. The main
attempt of a session run is never evaluated here: run.attempts is child
attempts only and never includes it (docs/DESIGN.md 4.3).
"""

from __future__ import annotations

from ..models import Attempt, Event, Finding, Run
from .base import KINDS_BOTH, Detector, evidence_line, make_finding_id

# docs/DESIGN.md 4.4 clause 4: below this, an unattributed interrupt is
# merely info (an agent could plausibly have gone quiet on its own for a
# while); at or above it, silence this long is itself evidence consistent
# with a runtime kill, so the finding is major even without a parent-side
# confirmation.
_MAJOR_SILENCE_THRESHOLD_SECONDS = 300.0


def _kill_event(attempt: Attempt) -> Event | None:
    """The kill sentinel event: the last 'user'-kind event in the attempt.
    attempt.interrupt being set already guarantees one exists; this just
    re-locates it for evidence purposes (Interrupt itself only carries its
    timestamp, not the Event)."""
    last_user_index = None
    for index, event in enumerate(attempt.events):
        if event.kind == "user":
            last_user_index = index
    return attempt.events[last_user_index] if last_user_index is not None else None


def _severity_and_title(attempt: Attempt) -> tuple[str, str]:
    """Severity and title for attempt.interrupt.cause, in the four forms
    docs/DESIGN.md 4.4 specifies."""
    interrupt = attempt.interrupt
    label = attempt.label
    cause = interrupt.cause
    silence = interrupt.silence_seconds

    if cause == "parent_stop":
        return "info", f"{label} stopped by the parent session"

    if cause == "human":
        return "info", f"{label} interrupted together with the parent session"

    if cause == "runtime":
        silence_text = (
            "unknown silence" if silence is None else f"{int(round(silence))}s of silence"
        )
        title = (
            f"{label} attempt {attempt.ordinal} was killed by the runtime after "
            f"{silence_text}, logged as a user interrupt"
        )
        return "major", title

    # unattributed
    if silence is None:
        return "info", f"{label} interrupted with silence unknown, cause unattributed"
    base = f"{label} interrupted after {int(round(silence))}s of silence, cause unattributed"
    if silence >= _MAJOR_SILENCE_THRESHOLD_SECONDS:
        return "major", f"{base}, consistent with a runtime kill"
    return "info", base


def _rule_id(cause: str, severity: str) -> str | None:
    """R-MONITOR-RETRIES only when the cause is runtime, or when it is
    unattributed and major (docs/DESIGN.md 4.3): the two cases where the
    evidence itself points at the runtime, not at the parent session or a
    human."""
    if cause == "runtime":
        return "R-MONITOR-RETRIES"
    if cause == "unattributed" and severity == "major":
        return "R-MONITOR-RETRIES"
    return None


class MislabeledKill:
    id = "mislabeled_kill"
    severity_default = "major"  # nominal; the real severity varies by cause (_severity_and_title)
    kinds = KINDS_BOTH

    def run(self, run: Run) -> list[Finding]:
        findings: list[Finding] = []

        for attempt in run.attempts:
            interrupt = attempt.interrupt
            if interrupt is None:
                continue
            kill_event = _kill_event(attempt)
            if kill_event is None:
                continue

            severity, title = _severity_and_title(attempt)
            rule_id = _rule_id(interrupt.cause, severity)

            evidence = []
            if interrupt.preceding is not None:
                evidence.append(evidence_line(run, interrupt.preceding))
            evidence.append(evidence_line(run, kill_event))
            if interrupt.cause_evidence is not None:
                evidence.append(evidence_line(run, interrupt.cause_evidence))

            metrics = {
                "silence_seconds": interrupt.silence_seconds,
                "cause": interrupt.cause,
                "events": len(attempt.events),
            }

            findings.append(Finding(
                detector=self.id,
                severity=severity,
                title=title,
                run_id=run.run_id,
                agent_id=attempt.agent_id,
                evidence=evidence,
                metrics=metrics,
                rule_id=rule_id,
                finding_id=make_finding_id(
                    run.run_id, self.id, attempt.agent_id, evidence[0] if evidence else None
                ),
            ))

        return findings


DETECTOR: Detector = MislabeledKill()
