"""Detects tool calls the runtime refused (docs/DESIGN.md 4.3, 4.13.2, 4.13.4; G7, v0.3.1).

Fires on every anchored, not-in-payload `permission_denied` MarkerHit: an `is_error` tool result
whose text starts with one of the runtime's four denial sentences (markers.denial_kind). One
finding per attempt that holds at least one, the main thread of a session included (its finding
carries no agent id and backs the run story), minor at every level.

The title counts the denials by kind, zero counts omitted. Evidence is up to 5 `file:line`
pointers and nothing else: the permission-rule sentence carries the denied command, so no excerpt
of any denial reaches a finding. `read_only_output` is not consulted: the text is the runtime's
own reply to the call, not output a command printed.
"""

from __future__ import annotations

from .. import markers
from .. import severity as severity_rules
from ..models import Attempt, Event, Finding, Run
from .base import (
    KINDS_BOTH,
    Detector,
    anchored_marker_hits,
    event_for_marker_hit,
    make_finding_id,
    relative_path,
    tooling_attempts,
)

ID = "permission_denied"
SEVERITY = "minor"
MAX_EVIDENCE = 5

# (kind, metrics key, title label), in the title's fixed order
_KINDS = (
    (markers.DENIAL_AUTO_MODE, "auto_mode", "auto mode"),
    (markers.DENIAL_USER, "user", "user"),
    (markers.DENIAL_PERMISSION_RULE, "permission_rule", "permission rule"),
    (markers.DENIAL_SAFETY_CHECK, "safety_check", "safety check"),
)


def _attempt_denials(attempt: Attempt) -> list[tuple[Event, str]]:
    """(event, kind) for every anchored denial of this attempt, in transcript order."""
    ordered = sorted(
        anchored_marker_hits(attempt, ID), key=lambda hit: (hit.event_line, hit.tool_use_id or ""),
    )
    found: list[tuple[Event, str]] = []
    for hit in ordered:
        event = event_for_marker_hit(attempt, hit)
        if event is None:
            continue
        kind = markers.denial_prefix_kind(event.text)
        if kind is None:  # cannot be classified: not claimed
            continue
        found.append((event, kind))
    return found


class PermissionDenied:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_BOTH

    def run(self, run: Run) -> list[Finding]:
        findings: list[Finding] = []
        for attempt in tooling_attempts(run):
            denials = _attempt_denials(attempt)
            if not denials:
                continue
            counts = {key: 0 for _kind, key, _label in _KINDS}
            for _event, kind in denials:
                for known, key, _label in _KINDS:
                    if kind == known:
                        counts[key] += 1
            total = len(denials)
            parts = [f"{label} {counts[key]}" for _k, key, label in _KINDS if counts[key]]
            title = (
                f"{total} tool {'call' if total == 1 else 'calls'} denied ({', '.join(parts)})"
            )
            evidence = [
                f"{relative_path(run, event.path)}:{event.line_no}"
                for event, _kind in denials[:MAX_EVIDENCE]
            ]
            agent_id = None if attempt is run.main else attempt.agent_id
            findings.append(Finding(
                detector=ID,
                severity=severity_rules.resolve(run, ID),
                title=title,
                run_id=run.run_id,
                agent_id=agent_id,
                evidence=evidence,
                metrics={"count": total, **counts},
                rule_id=None,
                finding_id=make_finding_id(run.run_id, ID, agent_id, evidence[0]),
            ))
        return findings


DETECTOR: Detector = PermissionDenied()
