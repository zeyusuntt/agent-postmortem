"""attempt_failed: names the cause of a workflow attempt the journal closed
with a `failed` event (.reviews/proposals/2026-09-v0.2-guided-rescue.md
section 4.2). The cause is read from exactly one place: the attempt's own
last transcript entry, never a wider window and never a user-authored
prompt -- a fixture whose prompt merely mentions "API Error" must still come
back unattributed (tests/fixtures/runs/attempt_failed_causes's af-decoy
key, docs/DESIGN.md 4.3's disposition N6).
"""

from __future__ import annotations

import re

from ..markers import (
    API_ERROR,
    KILL_SENTINEL_PREFIX,
    MACHINE_SLEEP,
    USAGE_LIMIT_SESSION,
    USAGE_LIMIT_WEEKLY,
)
from ..models import Attempt, Event, Finding, Run
from .base import KINDS_WORKFLOW_ONLY, Detector, evidence_line, make_finding_id

ID = "attempt_failed"
SEVERITY = "major"  # nominal; the real severity depends on whether a later attempt completed
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

# (marker fragment, cause name, title phrase, retry_now) -- one row per
# runtime-produced marker a `failed` attempt's last transcript entry can
# carry (docs/DESIGN.md 4.3). Order does not matter: the four fragments are
# mutually exclusive substrings.
_CAUSE_TABLE: tuple[tuple[str, str, str, str], ...] = (
    (USAGE_LIMIT_SESSION[0], "usage_limit_session", "session usage limit reached", "no"),
    (USAGE_LIMIT_WEEKLY[0], "usage_limit_weekly", "weekly usage limit reached", "no"),
    (MACHINE_SLEEP[0], "machine_sleep", "the machine went to sleep", "yes"),
    (API_ERROR[0], "api_error", "a server error occurred", "yes"),
)


def _cause_from_last_event(event: Event) -> tuple[str, str, str | None] | None:
    """(cause, title phrase, reset_time) from the caller's own last transcript
    entry, checked only there, never anywhere else (docs/DESIGN.md 4.3). The
    marker fragment must START the event's own text -- membership anywhere in
    it is not enough: a last assistant entry that merely quotes a marker
    inside ordinary prose ("I saw API Error: Server error mid-response in a
    log") must come back unattributed, never misattributed to that marker's
    cause. The four fragments are short, well under the 400-char excerpt
    limit, so str.startswith on event.text (the excerpt) is exactly as
    reliable as checking the full untruncated text would be. Returns None
    when none of the four markers start the text."""
    for fragment, cause, phrase, _retry in _CAUSE_TABLE:
        if not event.text.startswith(fragment):
            continue
        reset_time = None
        if cause in ("usage_limit_session", "usage_limit_weekly"):
            remainder = event.text[len(fragment):]
            reset_time = _clean_reset_time(remainder) or None
            if reset_time:
                phrase = f"{phrase}, resets {reset_time}"
        return cause, phrase, reset_time
    return None


def _retry_now_for(cause: str) -> str:
    """'no' for both usage limits, 'yes' for machine_sleep and api_error,
    'unknown' for interrupted and unattributed (docs/DESIGN.md 4.3)."""
    for _fragment, table_cause, _phrase, retry_now in _CAUSE_TABLE:
        if table_cause == cause:
            return retry_now
    return "unknown"


# F4: the three tails docs/DESIGN.md 4.3 distinguishes, keyed by
# (a later attempt exists, one of them completed).
_TAIL_NOT_RETRIED = "the key was not retried"
_TAIL_RETRIED_INCOMPLETE = "retried, no later attempt completed"
_TAIL_RETRIED_COMPLETED = "a later attempt completed"


def _retry_outcome(run: Run, attempt: Attempt) -> tuple[str, str]:
    """(tail phrase, severity) for attempt's title (docs/DESIGN.md 4.3),
    distinguishing three cases instead of two: no later attempt for the key
    at all ("the key was not retried", major); a later attempt exists but
    none of them completed ("retried, no later attempt completed", major --
    a "failed, started, failed" or "failed, started" (still open) sequence
    both land here, since both really were retried); a later attempt
    completed ("a later attempt completed", minor). "Later" means a higher
    ordinal of the same key, exactly as before."""
    if attempt.key is None:
        return _TAIL_NOT_RETRIED, "major"
    later = [
        sibling for sibling in run.attempts
        if sibling.key == attempt.key and sibling.ordinal > attempt.ordinal
    ]
    if not later:
        return _TAIL_NOT_RETRIED, "major"
    if any(sibling.outcome == "completed" for sibling in later):
        return _TAIL_RETRIED_COMPLETED, "minor"
    return _TAIL_RETRIED_INCOMPLETE, "major"


class AttemptFailed:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_WORKFLOW_ONLY

    def run(self, run: Run) -> list[Finding]:
        findings: list[Finding] = []

        for attempt in run.attempts:
            if attempt.outcome != "failed":
                continue

            last_event = attempt.events[-1] if attempt.events else None
            cause = "unattributed"
            phrase: str | None = None
            reset_time: str | None = None

            if last_event is not None:
                if last_event.kind == "user" and last_event.text.startswith(
                    KILL_SENTINEL_PREFIX
                ):
                    cause = "interrupted"
                    phrase = "interrupted before finishing"
                elif last_event.kind == "assistant":
                    matched = _cause_from_last_event(last_event)
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


DETECTOR: Detector = AttemptFailed()
