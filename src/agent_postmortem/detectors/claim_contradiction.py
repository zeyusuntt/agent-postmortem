"""claim_contradiction: a completed workflow attempt whose own self-reported
result contradicts the outcome the journal recorded (.reviews/proposals/
2026-09-v0.2-guided-rescue.md section 4.3, unit M1c).

Reads only Attempt.reported (docs/DESIGN.md section 3/4.1), which the
reader already restricted to an allowlisted set of self-reported claims --
never a raw transcript scan, never the free-text keys ("note", "summary",
...) a real result carries alongside them. The one filesystem check this
detector performs (report_path existence) follows missing_artifact's own
convention: a bare os.path.exists() on the path as reported. A relative
report_path cannot be checked against anything the reader records today
(Attempt/Event carry no cwd field in this milestone) and is reported
'unverified' rather than counted as a contradiction.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from ..models import Finding, Run
from .base import KINDS_WORKFLOW_ONLY, Detector, make_finding_id

ID = "claim_contradiction"
SEVERITY = "major"

# A report_path is checked on disk only while the file could still be
# expected to exist. Reports land in temp directories that are cleaned up
# days later: on the machine this was built on, 31 of the 50 findings this
# detector produced over old runs were report_path clauses on runs weeks
# old. For a run that ended more than this long ago the clause is not
# evidence either way and is skipped; a run with no known end time is
# treated as recent, since nothing says it is old.
REPORT_PATH_MAX_AGE = timedelta(hours=72)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _report_path_checkable(run: Run) -> bool:
    if run.ended_at is None:
        return True
    ended = run.ended_at
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    return _now() - ended <= REPORT_PATH_MAX_AGE


def _plural(n: int, word: str) -> str:
    return word if n == 1 else f"{word}s"


def _report_path_missing(claims: dict) -> bool:
    """Whether claims['report_path'] is set, absolute, and missing on disk
    at analysis time. A relative path is never checked (no attempt.cwd to
    resolve it against in this milestone) and never counts as a
    contradiction -- it is simply not evidence either way."""
    report_path = claims.get("report_path")
    if not isinstance(report_path, str) or not report_path:
        return False
    if not os.path.isabs(report_path):
        return False
    return not os.path.exists(report_path)


def _clauses(claims: dict, check_report_path: bool = True) -> list[tuple[str, str]]:
    """(clause id, human phrase) for every clause of docs/DESIGN.md 4.3 that
    holds over claims, in a fixed order."""
    clauses: list[tuple[str, str]] = []

    tests_failed = claims.get("tests_failed")
    if isinstance(tests_failed, int) and tests_failed > 0:
        clauses.append(("tests_failed", f"{tests_failed} failed {_plural(tests_failed, 'test')}"))

    suite_failed = claims.get("suite_failed")
    if isinstance(suite_failed, int) and suite_failed > 0:
        clauses.append(
            ("suite_failed", f"{suite_failed} failed suite {_plural(suite_failed, 'check')}")
        )

    if claims.get("ok") is False:
        clauses.append(("ok_false", "ok is false"))

    if claims.get("ready") is False:
        clauses.append(("ready_false", "ready is false"))

    blockers_count = claims.get("blockers_count")
    if isinstance(blockers_count, int) and blockers_count > 0:
        clauses.append(
            ("blockers", f"{blockers_count} {_plural(blockers_count, 'blocker')}")
        )

    if check_report_path and _report_path_missing(claims):
        clauses.append(("report_path_missing", "a report_path that does not exist"))

    return clauses


class ClaimContradiction:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_WORKFLOW_ONLY

    def run(self, run: Run) -> list[Finding]:
        findings: list[Finding] = []
        check_report_path = _report_path_checkable(run)

        for attempt in run.attempts:
            if attempt.outcome != "completed":
                continue
            reported = attempt.reported
            if not isinstance(reported, dict) or reported.get("type") != "dict":
                continue
            claims = reported.get("claims", {})
            clauses = _clauses(claims, check_report_path)
            if not clauses:
                continue

            phrases = "; ".join(phrase for _clause_id, phrase in clauses)
            title = (
                f"{attempt.label} attempt {attempt.ordinal} completed but its own "
                f"result reports {phrases}"
            )
            excerpt = " ".join(phrases.split())[:160]
            line = attempt.reported_line
            evidence = [f"journal.jsonl:{line}: {excerpt}"] if line is not None else []

            findings.append(
                Finding(
                    detector=ID,
                    severity=SEVERITY,
                    title=title,
                    run_id=run.run_id,
                    agent_id=attempt.agent_id,
                    evidence=evidence,
                    metrics={
                        "clauses": [clause_id for clause_id, _phrase in clauses],
                        "claims": claims,
                    },
                    rule_id=None,
                    finding_id=make_finding_id(
                        run.run_id, ID, attempt.agent_id, evidence[0] if evidence else None
                    ),
                )
            )

        return findings


DETECTOR: Detector = ClaimContradiction()
