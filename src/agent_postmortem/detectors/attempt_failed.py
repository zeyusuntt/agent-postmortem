"""attempt_failed: names the cause of a workflow attempt the journal closed
with a `failed` event (.reviews/proposals/2026-09-v0.2-guided-rescue.md
section 4.2). The cause is read from exactly one place: the attempt's own
last transcript entry (not counting trailing attachment entries), never a
wider window and never a user-authored prompt -- a fixture whose prompt
merely mentions "API Error" must still come back unattributed
(tests/fixtures/runs/attempt_failed_causes's af-decoy key, docs/DESIGN.md 4.3's disposition N6).
docs/DESIGN.md 4.13.2 (v0.3 N1): the four runtime-cause markers and the kill sentinel are read as
anchored, not-in-payload MarkerHits on the attempt's own last event, never
Event.text.startswith directly -- the registry's own permitted shapes
already restrict these ids to the attempt's final transcript entry (the
four causes) or a user entry (the sentinel), so an anchored hit there is, by
construction, on the last event whenever the last event itself carries one.
"""

from __future__ import annotations

import re

from .. import severity as severity_rules
from ..markers import USAGE_LIMIT_SESSION, USAGE_LIMIT_WEEKLY
from ..models import Attempt, Event, Finding, Run
from .base import (
    KINDS_BOTH,
    Detector,
    anchored_marker_hits,
    evidence_line,
    final_event,
    make_finding_id,
    relative_path,
)

ID = "attempt_failed"
SEVERITY = "major"  # the highest row; the real severity comes from the `recovered` input
RULE_ID_USAGE_LIMIT = "R-USAGE-LIMIT"

_RESET_TIME_LIMIT = 40
# F3: a reset time is kept only from characters in this set (stripped of
# anything else) -- an untrusted remainder past the marker should never let
# an arbitrary character ride into the title or exported metrics.
# What a reset time may contain: digits, letters, ":", ".", ",", " ", and
# the "/", "_", "(", ")" a zone name such as "(America/Los_Angeles)" needs.
_RESET_TIME_DISALLOWED_RE = re.compile(r"[^0-9A-Za-z:., /_()-]")


def _clean_reset_time(remainder: str) -> str:
    return _RESET_TIME_DISALLOWED_RE.sub("", remainder).strip()[:_RESET_TIME_LIMIT]

# (canonical marker id, title phrase, retry_now) -- one row per
# runtime-produced marker a `failed` attempt's last transcript entry can
# carry (docs/DESIGN.md 4.3, 4.13.2). Order does not matter: the four ids
# are mutually exclusive by construction (each anchors on a distinct,
# non-overlapping prefix of the same final entry).
_CAUSE_TABLE: tuple[tuple[str, str, str], ...] = (
    ("usage_limit_session", "session usage limit reached", "no"),
    ("usage_limit_weekly", "weekly usage limit reached", "no"),
    ("machine_sleep", "the machine went to sleep", "yes"),
    ("api_error", "a server error occurred", "yes"),
)

# The two usage-limit markers alone carry a reset time after the fixed
# prefix; machine_sleep and api_error do not.
_RESET_TIME_FRAGMENTS = {
    "usage_limit_session": USAGE_LIMIT_SESSION[0],
    "usage_limit_weekly": USAGE_LIMIT_WEEKLY[0],
}


def _anchored_on_event(attempt: Attempt, marker: str, event: Event) -> bool:
    """Whether attempt carries an anchored, not-in-payload MarkerHit for
    marker whose event_line is event's own line (docs/DESIGN.md 4.13.2)."""
    return any(hit.event_line == event.line_no for hit in anchored_marker_hits(attempt, marker))


def _cause_from_last_event(attempt: Attempt, event: Event) -> tuple[str, str, str | None] | None:
    """(cause, title phrase, reset_time) from the caller's own last
    transcript entry, checked only there, never anywhere else (docs/
    DESIGN.md 4.3): the registry's final_assistant_entry shape already
    restricts usage_limit_session/weekly, machine_sleep, and api_error to
    exactly this entry, so an anchored hit for one of them is never
    misattributed to a marker merely quoted inside ordinary prose elsewhere.
    Returns None when none of the four ids anchor there."""
    for marker, phrase, _retry in _CAUSE_TABLE:
        if not _anchored_on_event(attempt, marker, event):
            continue
        reset_time = None
        fragment = _RESET_TIME_FRAGMENTS.get(marker)
        # docs/DESIGN.md 4.13.2: the anchored check this loop just passed is
        # computed on the flattened text after leading whitespace is
        # stripped; the reset-time extraction must read the same stripped
        # text, not the raw excerpt, so the two can never disagree about
        # where the fragment starts.
        stripped_text = event.text.lstrip(" \t\r\n")
        if fragment is not None and stripped_text.startswith(fragment):
            remainder = stripped_text[len(fragment):]
            reset_time = _clean_reset_time(remainder) or None
            if reset_time:
                phrase = f"{phrase}, resets {reset_time}"
        return marker, phrase, reset_time
    return None


def _retry_now_for(cause: str) -> str:
    """'no' for both usage limits, 'yes' for machine_sleep and api_error,
    'unknown' for interrupted and unattributed (docs/DESIGN.md 4.3)."""
    for table_cause, _phrase, retry_now in _CAUSE_TABLE:
        if table_cause == cause:
            return retry_now
    return "unknown"


# F4: the three tails docs/DESIGN.md 4.3 distinguishes, keyed by
# (a later attempt exists, one of them completed).
_TAIL_NOT_RETRIED = "the key was not retried"
_TAIL_RETRIED_INCOMPLETE = "retried, no later attempt completed"
_TAIL_RETRIED_COMPLETED = "a later attempt completed"


def _retry_outcome(run: Run, attempt: Attempt) -> tuple[str, str]:
    """(tail phrase, severity) for attempt's title (docs/DESIGN.md 4.3, 4.13.4).
    The severity comes from the `recovered` input; the tail reads the same input,
    so the two cannot disagree: no later attempt for the key at all ("the key was
    not retried"); a later attempt exists but none was closed with a result
    ("retried, no later attempt completed" -- a `failed, started, failed` and a
    `failed, started` (still open) sequence both land here); a later attempt was
    closed with a result ("a later attempt completed"). "Later" means a higher
    ordinal of the same key."""
    recovered = severity_rules.recovered(run, attempt, include_self=False)
    severity = severity_rules.resolve(run, ID, recovered=recovered)
    has_later = attempt.key is not None and any(
        sibling.key == attempt.key and sibling.ordinal > attempt.ordinal
        for sibling in run.attempts
    )
    if not has_later:
        return _TAIL_NOT_RETRIED, severity
    if recovered == "yes":
        return _TAIL_RETRIED_COMPLETED, severity
    return _TAIL_RETRIED_INCOMPLETE, severity


class AttemptFailed:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_BOTH

    def run(self, run: Run) -> list[Finding]:
        findings: list[Finding] = []

        if run.kind == "session":
            return self._session_findings(run)

        for attempt in run.attempts:
            if attempt.outcome != "failed":
                continue

            # docs/DESIGN.md 4.13.2: trailing attachment entries do not count as the
            # attempt's final entry (the reader's marker shape skips them the same way).
            last_event = final_event(attempt.events)
            cause = "unattributed"
            phrase: str | None = None
            reset_time: str | None = None

            if last_event is not None:
                if last_event.kind == "user" and _anchored_on_event(
                    attempt, "kill_sentinel", last_event
                ):
                    cause = "interrupted"
                    phrase = "interrupted before finishing"
                elif last_event.kind == "assistant":
                    matched = _cause_from_last_event(attempt, last_event)
                    if matched is not None:
                        cause, phrase, reset_time = matched

            retry_now = _retry_now_for(cause)
            tail, severity = _retry_outcome(run, attempt)

            if cause == "unattributed":
                title = (
                    f"{attempt.label} attempt {attempt.ordinal} failed, "
                    f"cause not recorded in the transcript; {tail}"
                )
            else:
                title = f"{attempt.label} attempt {attempt.ordinal} failed: {phrase}; {tail}"

            evidence = [evidence_line(run, last_event)] if last_event is not None else []
            rule_id = (
                RULE_ID_USAGE_LIMIT
                if cause in ("usage_limit_session", "usage_limit_weekly")
                else None
            )

            findings.append(
                Finding(
                    detector=ID,
                    severity=severity,
                    title=title,
                    run_id=run.run_id,
                    agent_id=attempt.agent_id,
                    evidence=evidence,
                    metrics={"cause": cause, "retry_now": retry_now, "reset_time": reset_time},
                    rule_id=rule_id,
                    finding_id=make_finding_id(
                        run.run_id, ID, attempt.agent_id, evidence[0] if evidence else None
                    ),
                )
            )

        return findings


    def _session_findings(self, run: Run) -> list[Finding]:
        """docs/DESIGN.md 4.3 (G1c, v0.3.1): one major finding per session subagent whose
        `return_state` is `reported_failed`; `reported_killed` adds none. The cause names what
        decided it (a synchronous launch result or a task notification), the retry advice is
        unknown, and the severity is the table's row for `recovered=unknown`."""
        findings: list[Finding] = []
        for attempt in run.attempts:
            if attempt.return_state != "reported_failed" or attempt.return_evidence is None:
                continue
            source, path, line = attempt.return_evidence
            cause = (
                "launch_result_failed" if source == "launch_result" else "task_notification_failed"
            )
            phrase = _SESSION_CAUSE_PHRASES[cause]
            event = next(
                (
                    candidate
                    for holder in (run.main, *run.attempts) if holder is not None
                    for candidate in holder.events
                    if candidate.path == path and candidate.line_no == line
                ),
                None,
            )
            pointer = (
                evidence_line(run, event) if event is not None
                else f"{relative_path(run, path)}:{line}"
            )
            severity = severity_rules.resolve(run, ID, recovered="unknown")
            title = f"{attempt.label or 'subagent'} failed: {phrase}"
            findings.append(
                Finding(
                    detector=ID,
                    severity=severity,
                    title=title,
                    run_id=run.run_id,
                    agent_id=attempt.agent_id,
                    evidence=[pointer],
                    metrics={"cause": cause, "retry_now": "unknown", "reset_time": None},
                    rule_id=None,
                    finding_id=make_finding_id(run.run_id, ID, attempt.agent_id, pointer),
                )
            )
        return findings


_SESSION_CAUSE_PHRASES = {
    "launch_result_failed": "its launch result reported failure",
    "task_notification_failed": "its task notification reported failure",
}

DETECTOR: Detector = AttemptFailed()
