"""Weekly detector counts, and the fire rate before versus after an applied
rule.

This module counts; it never claims causation. Output wording is strictly
"before" and "after". See docs/DESIGN.md section 4.6.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from .detectors import all_detectors
from .models import Finding, Run
from .redact import Pseudonyms, Redactor
from .report import SCHEMA_VERSION
from .rules import RULES

WeeklyCounts = dict[str, dict[str, int]]


def _detector_kinds() -> dict[str, frozenset[str]]:
    """detector id -> the run kinds it applies to (docs/DESIGN.md 4.3),
    looked up fresh each call so a test's own detector registrations (there
    are none in practice, but nothing here should assume otherwise) are
    never stale."""
    return {detector.id: detector.kinds for detector in all_detectors()}


def _rule_kinds(
    rule_id: str | None, kinds_by_detector: dict[str, frozenset[str]]
) -> frozenset[str]:
    """The run kinds a rule's finding-backed detectors apply to, unioned
    across every detector that backs it. Empty for an unknown rule id or one
    with no detector backing it in v0 (docs/DESIGN.md 4.7): there is nothing
    to count, so no kind applies."""
    rule = RULES.get(rule_id) if rule_id else None
    if rule is None:
        return frozenset()
    kinds: set[str] = set()
    for detector_id in rule.detectors:
        kinds |= set(kinds_by_detector.get(detector_id, ()))
    return frozenset(kinds)


def _iso_week_key(dt: datetime) -> str:
    iso = dt.date().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _trailing_iso_weeks(reference: datetime, weeks: int) -> list[str]:
    """The `weeks` ISO week keys ending at reference's own week, oldest first."""
    if weeks <= 0:
        return []
    ref_date = reference.date()
    ref_monday = ref_date - timedelta(days=ref_date.isoweekday() - 1)
    return [
        _iso_week_key(datetime.combine(ref_monday - timedelta(weeks=i), datetime.min.time()))
        for i in range(weeks - 1, -1, -1)
    ]


def weekly_counts(
    runs_with_findings: list[tuple[Run, list[Finding]]],
    weeks: int,
    *,
    now: datetime | None = None,
) -> WeeklyCounts:
    """Per detector, per ISO week in the trailing `weeks` weeks ending at
    `now` (default: the current time), the number of runs that fired that
    detector at least once. A run with no started_at is skipped: it cannot
    be placed in a week. Detectors that never fired in the window are
    simply absent from the result. A finding whose detector does not apply
    to its own run's kind (docs/DESIGN.md 4.3 and 4.7) is never counted:
    run_detectors() already keeps this from happening in practice, but a
    caller that hands in findings some other way must not silently inflate
    a count over a kind the detector never runs on.
    """
    reference = now or datetime.now(timezone.utc)
    week_keys = _trailing_iso_weeks(reference, weeks)
    window = set(week_keys)
    kinds_by_detector = _detector_kinds()

    counts: WeeklyCounts = {}
    for run, findings in runs_with_findings:
        if run.started_at is None:
            continue
        week_key = _iso_week_key(run.started_at)
        if week_key not in window:
            continue
        applicable_detectors = {
            finding.detector for finding in findings
            if run.kind in kinds_by_detector.get(finding.detector, ())
        }
        # Sorted: set iteration order changes with the hash seed, and the
        # insertion order here is the key order `trend --json` prints.
        for detector_id in sorted(applicable_detectors):
            row = counts.setdefault(detector_id, {week: 0 for week in week_keys})
            row[week_key] += 1

    return counts


def _parse_applied_at(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def before_after(
    applied: list[dict],
    runs_with_findings: list[tuple[Run, list[Finding]]],
    *,
    window_weeks: int | None = None,
) -> list[dict]:
    """For each applied-rule record, the fire rate before and after applied_at.

    "Fired" means at least one of the rule's detectors produced a finding in
    that run. Runs with no started_at, or whose kind is not one the rule's
    backing detectors apply to (docs/DESIGN.md 4.7), are excluded from both
    windows: a session run must never dilute or inflate a workflow-only
    rule's rate just because it has its own started_at. A rule with no
    detectors backing it in v0 (for example R-MODEL-ALLOCATION, which no
    detector fires yet) or an unparsable applied_at yields a 0/0, rate None
    on both sides: there is nothing to count. Each row also carries "kinds":
    the sorted run kinds the rule's detectors apply to, empty when nothing
    backs it -- the denominator's own label (docs/DESIGN.md 4.7).

    `window_weeks` (docs/DESIGN.md 9, `rules review`): when given, a run
    only counts toward "before" if it started within that many weeks before
    applied_at, and toward "after" only within that many weeks after --
    unbounded (the default, what plain `trend` uses) when None, so every
    existing caller is unaffected.
    """
    kinds_by_detector = _detector_kinds()
    window = timedelta(weeks=window_weeks) if window_weeks is not None else None
    rows: list[dict] = []
    for entry in applied:
        rule_id = entry.get("rule_id")
        applied_at = _parse_applied_at(entry.get("applied_at"))
        rule = RULES.get(rule_id)
        detector_ids = set(rule.detectors) if rule else set()
        applicable_kinds = _rule_kinds(rule_id, kinds_by_detector)

        before_runs = before_fired = after_runs = after_fired = 0
        if applied_at is not None and detector_ids:
            for run, findings in runs_with_findings:
                if run.started_at is None or run.kind not in applicable_kinds:
                    continue
                fired = any(finding.detector in detector_ids for finding in findings)
                if run.started_at < applied_at:
                    if window is not None and run.started_at < applied_at - window:
                        continue
                    before_runs += 1
                    before_fired += int(fired)
                else:
                    if window is not None and run.started_at >= applied_at + window:
                        continue
                    after_runs += 1
                    after_fired += int(fired)

        rows.append(
            {
                "rule_id": rule_id,
                "target": entry.get("target"),
                "applied_at": entry.get("applied_at"),
                "kinds": sorted(applicable_kinds),
                "before": {
                    "runs": before_runs,
                    "fired": before_fired,
                    "rate": (before_fired / before_runs) if before_runs else None,
                },
                "after": {
                    "runs": after_runs,
                    "fired": after_fired,
                    "rate": (after_fired / after_runs) if after_runs else None,
                },
            }
        )
    return rows


# --- rules review (docs/DESIGN.md 9) ----------------------------------------


def review_rows(
    adopted: list[dict], runs_with_findings: list[tuple[Run, list[Finding]]], weeks: int
) -> list[dict]:
    """before_after()'s own rows, windowed to `weeks` on each side of
    applied_at, plus each rule's own target_kind and backing detectors --
    everything `rules review`'s renderer needs, without it re-deriving any
    of before_after()'s own counting."""
    rows = before_after(adopted, runs_with_findings, window_weeks=weeks)
    for row in rows:
        rule = RULES.get(row["rule_id"])
        row["detectors"] = list(rule.detectors) if rule else []
        row["target_kind"] = rule.target_kind if rule else None
    return rows


def _rule_not_improved(before: dict, after: dict) -> bool:
    """docs/DESIGN.md 9: a rule is worth "consider removing" when both
    windows actually have data and the after-rate is not lower than the
    before-rate -- adopting it did not measurably help. Insufficient data on
    either side (0 runs, or a rate that could not be computed) is never
    flagged: there is nothing yet to conclude either way."""
    if before["runs"] == 0 or after["runs"] == 0:
        return False
    if before["rate"] is None or after["rate"] is None:
        return False
    return after["rate"] >= before["rate"]


def _adopted_date(applied_at: str | None) -> str:
    return (applied_at or "")[:10] or "unknown"


def render_review_text(rows: list[dict]) -> str:
    """docs/DESIGN.md 9: one line per adopted rule (its backing detectors'
    combined before/after counts), then a closing "consider removing" line."""
    if not rows:
        return "no adopted rule to review\n"

    lines = []
    removing = []
    for row in rows:
        before, after = row["before"], row["after"]
        label = ", ".join(row["detectors"]) if row["detectors"] else "(no detector)"
        lines.append(
            f"{row['rule_id']}  adopted {_adopted_date(row['applied_at'])}  "
            f"{label}: {before['fired']} of {before['runs']} runs before, "
            f"{after['fired']} of {after['runs']} runs after"
        )
        if _rule_not_improved(before, after):
            removing.append(row["rule_id"])

    lines.append("")
    lines.append(f"consider removing: {', '.join(removing) if removing else 'none'}")
    return "\n".join(lines) + "\n"


def _display_review_target(target: str | None, redactor: Redactor) -> str:
    """docs/DESIGN.md 9/5.1 (F13 hardening): `rules review`'s own target
    field, target-N alone by default, the raw target string reserved for
    --no-redact. Deliberately a separate function from _display_target
    above: trend's own before/after rendering keeps 'target-N (basename)'
    together by design (tests/test_trend.py pins it), but a basename is a
    real filename that can carry a client or project name, and review must
    never let it survive by default the way trend's richer, human-facing
    rendering still does."""
    target = target or ""
    if not redactor.enabled or not target:
        return target
    return redactor.names.target(target)


def review_to_json(rows: list[dict], redactor: Redactor | None = None) -> dict:
    redactor = redactor or Redactor(enabled=True)
    redactor.names = Pseudonyms(enabled=redactor.enabled)
    removing = [row["rule_id"] for row in rows if _rule_not_improved(row["before"], row["after"])]
    return {
        "schema": SCHEMA_VERSION,
        "disclosure": redactor.level,
        "reviews": [
            {
                "rule_id": row["rule_id"],
                "target_kind": row["target_kind"],
                "detectors": row["detectors"],
                "adopted_at": row["applied_at"],
                "target": _display_review_target(row["target"], redactor),
                "before": row["before"],
                "after": row["after"],
            }
            for row in rows
        ],
        "consider_removing": removing,
    }


def _format_rate(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    return f"{rate * 100:.0f}%"


def _display_target(target: str | None, redactor: Redactor) -> str:
    """docs/DESIGN.md 5.1: a rule target (the free-text 'target' an owner
    recorded for an adopted rule) renders as 'target-N (basename)', a
    per-render pseudonym plus the file's own basename -- enough to tell two
    different targets apart in one report without the full path, which
    routinely embeds a client or project name (docs/DESIGN.md 5.1's own
    example: an instruction file under a client's directory). The raw path
    prints only with --no-redact."""
    target = target or ""
    if not redactor.enabled or not target:
        return target
    if redactor.level == "local":
        # N3b P19: the one path function. trend makes no observation, so
        # there is no repository root (D-n): a path inside home prints as
        # ~/.../<basename>, any other (and any relative one, which has no
        # base to be placed against) as path-N. Never absolute.
        return redactor.names.path(target)
    pseudonym = redactor.names.target(target)
    basename = PurePosixPath(target).name or target
    return f"{pseudonym} ({basename})"


def render_trend_text(
    weekly: WeeklyCounts,
    before_after_rows: list[dict],
    weeks: int,
    redactor: Redactor | None = None,
) -> str:
    # docs/DESIGN.md 5.2/5.3: default to redaction on; raw output is an
    # explicit opt-out (Redactor(enabled=False)), never the fallback.
    redactor = redactor or Redactor(enabled=True)
    # docs/DESIGN.md 5.1: a fresh Pseudonyms map for this render (at local,
    # the one with the P19 path function attached).
    redactor.fresh_names(detailed=True)
    lines = [f"WEEKLY DETECTOR COUNTS (last {weeks} weeks)"]
    if not weekly:
        lines.append("(no findings in this window)")
    else:
        week_keys = sorted({week for row in weekly.values() for week in row})
        header = "DETECTOR".ljust(26) + "".join(week.ljust(11) for week in week_keys)
        lines.append(header)
        for detector_id in sorted(weekly):
            row = weekly[detector_id]
            counts_str = "".join(str(row.get(week, 0)).ljust(11) for week in week_keys)
            lines.append(f"{detector_id.ljust(26)}{counts_str}")

    lines.append("")
    lines.append("BEFORE / AFTER APPLIED RULES")
    if not before_after_rows:
        lines.append("(no rule adopted yet; see 'agent-postmortem rules review')")
    else:
        for row in before_after_rows:
            before = row["before"]
            after = row["after"]
            target = _display_target(row["target"], redactor)
            # docs/DESIGN.md 4.7: the denominator printed beside every rate
            # names the kind(s) the rule's backing detector(s) apply to.
            kind_label = "/".join(row.get("kinds") or []) or None
            unit = f"{kind_label} runs" if kind_label else "runs"
            lines.append(f"{row['rule_id']}  applied {row['applied_at']}  target {target}")
            before_text = (
                f"{_format_rate(before['rate'])} ({before['fired']}/{before['runs']} {unit})"
            )
            after_text = (
                f"{_format_rate(after['rate'])} ({after['fired']}/{after['runs']} {unit})"
            )
            lines.append(f"  before {before_text}  /  after {after_text}")
    return "\n".join(lines) + "\n"


def to_json(
    weekly: WeeklyCounts,
    before_after_rows: list[dict],
    weeks: int,
    redactor: Redactor | None = None,
) -> dict:
    # docs/DESIGN.md 5.2/5.3: default to redaction on, same as report.to_json;
    # docs/DESIGN.md 5.1: a fresh Pseudonyms map for this render, and 5.1's
    # 'target-N (basename)' representation matches the text renderer's.
    redactor = redactor or Redactor(enabled=True)
    # At local the same path function trend's text uses (N3b P19), so a target
    # prints as ~/.../name or path-N, never absolute.
    redactor.fresh_names(detailed=True)
    before_after_rows = [
        {**row, "target": _display_target(row["target"], redactor)} for row in before_after_rows
    ]
    # docs/DESIGN.md 5.3b: schema 3 (`weeks` stays the requested count, an int).
    # trend never filters a silenced detector
    # out of weekly_counts or before_after -- docs/DESIGN.md 4.10: "a
    # silenced detector still runs ... and still counts in trend" -- so this
    # is the one JSON command whose payload shape and population are both
    # completely unchanged by this unit apart from the new top-level key.
    return {
        "schema": SCHEMA_VERSION,
        "disclosure": redactor.level,
        "weeks": weeks,
        "weekly_counts": weekly,
        "before_after": before_after_rows,
    }
