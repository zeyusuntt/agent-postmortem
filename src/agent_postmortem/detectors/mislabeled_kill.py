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

from .. import severity as severity_rules
from ..models import Attempt, Event, Finding, Run
from .base import KINDS_BOTH, Detector, anchored_marker_hits, evidence_line, make_finding_id


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


def _backgrounded_precursor_line(attempt: Attempt) -> int | None:
    """docs/DESIGN.md 4.4, 4.13.8: the transcript line of an anchored
    bash_backgrounded tool_result (4.13.2) when it is the attempt's last tool
    activity: no `tool_use` and no later `tool_result` event follows it in
    the attempt (parallel calls can return out of order, so a later result of
    another call also disqualifies it). None otherwise."""
    last_tool = None
    for index, event in enumerate(attempt.events):
        if event.kind in ("tool_use", "tool_result"):
            last_tool = index
    if last_tool is None:
        return None
    last = attempt.events[last_tool]
    if last.kind != "tool_result" or last.tool_use_id is None:
        return None
    for hit in anchored_marker_hits(attempt, "bash_backgrounded"):
        if hit.event_line == last.line_no and hit.tool_use_id == last.tool_use_id:
            return hit.event_line
    return None


def _severity_and_title(run: Run, attempt: Attempt) -> tuple[str, str]:
    """Severity and title for attempt.interrupt.cause. The severity is the row
    of docs/DESIGN.md 4.3's table for (cause, silence class, position); the
    title keeps the four forms 4.4 specifies. "Consistent with a runtime kill"
    is added to an unattributed interrupt after a long silence (the one 300 s
    rule)."""
    interrupt = attempt.interrupt
    label = attempt.label
    cause = interrupt.cause
    silence = interrupt.silence_seconds
    silence_class = severity_rules.silence_class(silence)
    severity = severity_rules.resolve(
        run, "mislabeled_kill", cause=cause, silence=silence_class,
        position=severity_rules.position(run, attempt),
    )

    if cause == "parent_stop":
        return severity, f"{label} stopped by the parent session"

    if cause == "human":
        return severity, f"{label} interrupted together with the parent session"

    # The precursor clause only states what was observed just before the
    # kill; it never says the backgrounding caused it (4.13.8).
    precursor_line = _backgrounded_precursor_line(attempt)
    precursor = (
        f", after a Bash command was moved to the background (line {precursor_line})"
        if precursor_line is not None else ""
    )

    if cause == "runtime":
        silence_text = (
            "unknown silence" if silence is None else f"{int(round(silence))}s of silence"
        )
        title = (
            f"{label} attempt {attempt.ordinal} was killed by the runtime after "
            f"{silence_text}{precursor}, logged as a user interrupt"
        )
        return severity, title

    # unattributed
    if silence is None:
        return severity, f"{label} interrupted with silence unknown, cause unattributed"
    base = (
        f"{label} interrupted after {int(round(silence))}s of silence{precursor}, "
        "cause unattributed"
    )
    if silence_class == "long":
        return severity, f"{base}, consistent with a runtime kill"
    return severity, base


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
    severity_default = "major"  # the highest row; the real severity comes from the table
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

            severity, title = _severity_and_title(run, attempt)
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
