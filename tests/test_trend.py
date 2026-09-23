"""Tests for trend.py: weekly_counts and before_after. See docs/DESIGN.md
section 4.6.

Reference dates: 2026-08-25, 2026-09-01, and 2026-09-08 fall in ISO weeks
2026-W35, 2026-W36, and 2026-W37 respectively; NOW is later in 2026-W37, so
a trailing 3-week window ending at NOW covers exactly those three weeks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from agent_postmortem import state, trend
from agent_postmortem.models import Finding, Run
from agent_postmortem.redact import Redactor

WEEK1 = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)  # 2026-W35
WEEK2 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)  # 2026-W36
WEEK3 = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)  # 2026-W37
NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)  # still 2026-W37


def _run(run_id: str, started_at: datetime | None, kind: str = "workflow") -> Run:
    return Run(
        run_id=run_id,
        kind=kind,
        path=Path("/tmp") / run_id,
        project_slug="-home-user-project",
        session_id=None,
        claude_version=None,
        journal=[],
        agents=[],
        attempts=[],
        started_at=started_at,
        ended_at=started_at,
        diagnostics=[],
    )


def _finding(detector: str, rule_id: str | None = None) -> Finding:
    return Finding(
        detector=detector,
        severity="major",
        title="t",
        run_id="wf_x",
        agent_id=None,
        evidence=[],
        metrics={},
        rule_id=rule_id,
    )


# --- weekly_counts -----------------------------------------------------


def test_weekly_counts_buckets_by_iso_week_per_detector():
    runs = [
        (_run("wf_1", WEEK1), [_finding("silent_retry")]),
        (_run("wf_2", WEEK2), [_finding("silent_retry"), _finding("bash_timeout")]),
        (_run("wf_3", WEEK3), [_finding("silent_retry")]),
    ]
    counts = trend.weekly_counts(runs, weeks=3, now=NOW)

    assert counts["silent_retry"] == {"2026-W35": 1, "2026-W36": 1, "2026-W37": 1}
    assert counts["bash_timeout"] == {"2026-W35": 0, "2026-W36": 1, "2026-W37": 0}


def test_weekly_counts_counts_runs_not_findings():
    runs = [(_run("wf_1", WEEK1), [_finding("silent_retry"), _finding("silent_retry")])]
    counts = trend.weekly_counts(runs, weeks=3, now=NOW)
    assert counts["silent_retry"]["2026-W35"] == 1


def test_weekly_counts_drops_runs_outside_the_trailing_window():
    old = datetime(2026, 6, 1, tzinfo=timezone.utc)  # 2026-W23, well outside 3 weeks
    runs = [(_run("wf_old", old), [_finding("silent_retry")])]
    counts = trend.weekly_counts(runs, weeks=3, now=NOW)
    assert counts == {}


def test_weekly_counts_skips_runs_with_no_started_at():
    runs = [(_run("wf_none", None), [_finding("silent_retry")])]
    counts = trend.weekly_counts(runs, weeks=3, now=NOW)
    assert counts == {}


def test_weekly_counts_skips_a_detector_on_a_run_kind_it_does_not_apply_to():
    """docs/DESIGN.md 4.7: weekly counts are computed per detector over runs
    whose kind the detector applies to. silent_retry is workflow-only; a
    (session run, silent_retry finding) pair should never happen in
    practice (run_detectors already filters it out upstream) but must still
    be excluded defensively here, not silently counted."""
    runs = [(_run("s_1", WEEK1, kind="session"), [_finding("silent_retry")])]
    counts = trend.weekly_counts(runs, weeks=3, now=NOW)
    assert counts == {}


def test_weekly_counts_keeps_a_both_kind_detector_on_a_session_run():
    runs = [(_run("s_1", WEEK1, kind="session"), [_finding("mislabeled_kill")])]
    counts = trend.weekly_counts(runs, weeks=3, now=NOW)
    assert counts["mislabeled_kill"]["2026-W35"] == 1


# --- before_after --------------------------------------------------------


def test_before_after_splits_on_applied_at_and_counts_covered_detectors():
    applied = [
        {
            "rule_id": "R-RETRY-SAFE",
            "target": "/tmp/CLAUDE.md",
            "applied_at": WEEK2.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    ]
    runs = [
        (_run("wf_1", WEEK1), [_finding("silent_retry")]),  # before, fired
        (_run("wf_2", WEEK1), []),  # before, clean
        (_run("wf_3", WEEK3), []),  # after, clean
        (_run("wf_4", WEEK3), []),  # after, clean
    ]

    rows = trend.before_after(applied, runs)

    assert len(rows) == 1
    row = rows[0]
    assert row["rule_id"] == "R-RETRY-SAFE"
    assert row["before"] == {"runs": 2, "fired": 1, "rate": 0.5}
    assert row["after"] == {"runs": 2, "fired": 0, "rate": 0.0}


def test_before_after_excludes_runs_with_no_started_at():
    applied = [
        {
            "rule_id": "R-RETRY-SAFE",
            "target": "/tmp/CLAUDE.md",
            "applied_at": WEEK2.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    ]
    runs = [
        (_run("wf_1", WEEK1), [_finding("silent_retry")]),
        (_run("wf_none", None), [_finding("silent_retry")]),
    ]
    rows = trend.before_after(applied, runs)
    assert rows[0]["before"]["runs"] == 1
    assert rows[0]["after"]["runs"] == 0


def test_before_after_rule_with_no_backing_detectors_has_no_data():
    applied = [
        {
            "rule_id": "R-MODEL-ALLOCATION",
            "target": "/tmp/x.md",
            "applied_at": WEEK2.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    ]
    runs = [(_run("wf_1", WEEK1), [_finding("silent_retry")])]
    row = trend.before_after(applied, runs)[0]
    assert row["before"] == {"runs": 0, "fired": 0, "rate": None}
    assert row["after"] == {"runs": 0, "fired": 0, "rate": None}


def test_before_after_unknown_rule_id_has_no_data():
    applied = [{"rule_id": "R-NOT-A-RULE", "target": "/tmp/x.md", "applied_at": WEEK2.strftime("%Y-%m-%dT%H:%M:%SZ")}]
    runs = [(_run("wf_1", WEEK1), [_finding("silent_retry")])]
    row = trend.before_after(applied, runs)[0]
    assert row["before"]["rate"] is None


def test_before_after_names_the_applicable_kinds():
    """docs/DESIGN.md 4.7: the denominator printed beside every rate names
    the kind(s) the rule's backing detector(s) apply to. R-RETRY-SAFE backs
    only silent_retry, which is workflow-only."""
    applied = [{"rule_id": "R-RETRY-SAFE", "target": "/tmp/x.md", "applied_at": WEEK2.strftime("%Y-%m-%dT%H:%M:%SZ")}]
    row = trend.before_after(applied, [])[0]
    assert row["kinds"] == ["workflow"]


def test_before_after_unions_kinds_across_a_rules_backing_detectors():
    """R-NO-LONG-BASH backs bash_backgrounded and bash_timeout, both of
    which apply to both kinds."""
    applied = [{"rule_id": "R-NO-LONG-BASH", "target": "/tmp/x.md", "applied_at": WEEK2.strftime("%Y-%m-%dT%H:%M:%SZ")}]
    row = trend.before_after(applied, [])[0]
    assert row["kinds"] == ["session", "workflow"]


def test_before_after_rule_with_no_backing_detectors_names_no_kind():
    applied = [{"rule_id": "R-MODEL-ALLOCATION", "target": "/tmp/x.md", "applied_at": WEEK2.strftime("%Y-%m-%dT%H:%M:%SZ")}]
    row = trend.before_after(applied, [])[0]
    assert row["kinds"] == []


def test_before_after_denominator_excludes_a_run_of_a_kind_the_rule_does_not_apply_to():
    """R-RETRY-SAFE only ever applies to workflow runs; a session run must
    not inflate (or deflate) its before/after denominator even though it
    has its own started_at."""
    applied = [{"rule_id": "R-RETRY-SAFE", "target": "/tmp/x.md", "applied_at": WEEK2.strftime("%Y-%m-%dT%H:%M:%SZ")}]
    runs = [
        (_run("wf_1", WEEK1), [_finding("silent_retry")]),  # before, fired
        (_run("s_1", WEEK1, kind="session"), []),  # before, wrong kind: excluded
        (_run("s_2", WEEK3, kind="session"), []),  # after, wrong kind: excluded
    ]
    row = trend.before_after(applied, runs)[0]
    assert row["before"] == {"runs": 1, "fired": 1, "rate": 1.0}
    assert row["after"] == {"runs": 0, "fired": 0, "rate": None}


def test_render_trend_text_uses_before_after_wording_never_caused():
    weekly = {"silent_retry": {"2026-W35": 1}}
    rows = [
        {
            "rule_id": "R-RETRY-SAFE",
            "target": "/tmp/CLAUDE.md",
            "applied_at": "2026-09-01T00:00:00Z",
            "before": {"runs": 2, "fired": 1, "rate": 0.5},
            "after": {"runs": 2, "fired": 0, "rate": 0.0},
        }
    ]
    text = trend.render_trend_text(weekly, rows, weeks=3)
    assert "before" in text
    assert "after" in text
    assert "caused" not in text.lower()


def test_render_trend_text_names_the_kind_beside_each_rate():
    rows = [
        {
            "rule_id": "R-RETRY-SAFE",
            "target": "/tmp/CLAUDE.md",
            "applied_at": "2026-09-01T00:00:00Z",
            "kinds": ["workflow"],
            "before": {"runs": 2, "fired": 1, "rate": 0.5},
            "after": {"runs": 2, "fired": 0, "rate": 0.0},
        }
    ]
    text = trend.render_trend_text({}, rows, weeks=3)
    assert "workflow runs" in text


def test_render_trend_text_names_multiple_kinds_when_a_rule_spans_both():
    rows = [
        {
            "rule_id": "R-NO-LONG-BASH",
            "target": "/tmp/CLAUDE.md",
            "applied_at": "2026-09-01T00:00:00Z",
            "kinds": ["session", "workflow"],
            "before": {"runs": 2, "fired": 1, "rate": 0.5},
            "after": {"runs": 2, "fired": 0, "rate": 0.0},
        }
    ]
    text = trend.render_trend_text({}, rows, weeks=3)
    assert "session/workflow runs" in text


# --- target pseudonyms (docs/DESIGN.md 5.1) -------------------------------


def _target_row(target: str) -> dict:
    return {
        "rule_id": "R-RETRY-SAFE",
        "target": target,
        "applied_at": "2026-09-01T00:00:00Z",
        "kinds": ["workflow"],
        "before": {"runs": 2, "fired": 1, "rate": 0.5},
        "after": {"runs": 2, "fired": 0, "rate": 0.0},
    }


def test_render_trend_text_pseudonymizes_the_target_by_default():
    rows = [_target_row("/srv/clients/acme-secret/CLAUDE.md")]
    text = trend.render_trend_text({}, rows, weeks=3)
    assert "acme-secret" not in text
    assert "target-1 (CLAUDE.md)" in text


def test_render_trend_text_shows_the_raw_target_with_no_redact():
    rows = [_target_row("/srv/clients/acme-secret/CLAUDE.md")]
    text = trend.render_trend_text({}, rows, weeks=3, redactor=Redactor(enabled=False))
    assert "/srv/clients/acme-secret/CLAUDE.md" in text


def test_to_json_pseudonymizes_the_target_the_same_way_as_text():
    rows = [_target_row("/srv/clients/acme-secret/CLAUDE.md")]
    payload = trend.to_json({}, rows, weeks=3)
    assert payload["before_after"][0]["target"] == "target-1 (CLAUDE.md)"


def test_to_json_defaults_to_redaction_on():
    """docs/DESIGN.md 5.2: trend.to_json must not fail open either."""
    rows = [_target_row("/srv/clients/acme-secret/CLAUDE.md")]
    default_payload = trend.to_json({}, rows, weeks=3)
    raw_payload = trend.to_json({}, rows, weeks=3, redactor=Redactor(enabled=False))
    assert default_payload["before_after"][0]["target"] == "target-1 (CLAUDE.md)"
    assert raw_payload["before_after"][0]["target"] == "/srv/clients/acme-secret/CLAUDE.md"


def test_render_trend_text_target_numbering_restarts_on_every_render():
    """docs/DESIGN.md 5.1: a fresh Pseudonyms map every render, even reusing
    the same Redactor object across two calls."""
    rows = [_target_row("/srv/clients/acme-secret/CLAUDE.md")]
    redactor = Redactor(enabled=True)
    first = trend.render_trend_text({}, rows, weeks=3, redactor=redactor)
    second = trend.render_trend_text({}, rows, weeks=3, redactor=redactor)
    assert "target-1 (CLAUDE.md)" in first
    assert "target-1 (CLAUDE.md)" in second


# --- state, used by trend via the CLI's applied-rules lookup --------------


def test_state_round_trips_applied_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    fresh = state.load_state()
    assert fresh == state.default_state()

    entry = {"rule_id": "R-GIT-CWD", "target": "/tmp/x.md", "applied_at": state.now_iso()}
    state.update(lambda s: s["applied"].append(entry))

    reloaded = state.load_state()
    assert reloaded["applied"] == [entry]


def test_state_load_is_tolerant_of_a_corrupt_file_and_writes_nothing(tmp_path, monkeypatch):
    """docs/DESIGN.md 7.5: load_state() (what 'trend' calls) never writes,
    so a repeated 'trend' invocation against a corrupt state file never
    accumulates backup files -- only update()/transaction() create one,
    under the lock."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = state.state_path()
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")

    loaded = state.load_state()

    assert loaded == state.default_state()
    assert not list(path.parent.glob("state.json.corrupt-*"))
