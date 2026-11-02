"""docs/DESIGN.md 4.14.9 items 1 and 2: scan's --since mtime pre-filter, the SINCE label, and the
stderr progress line. Fixture roots are built fresh under tmp_path (tests/live_helpers.py)."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

from live_helpers import backdate_tree, write_clean_workflow

from agent_postmortem import cli, reader
from agent_postmortem.cli import main

SLUG = "-home-user-project"


def _three_runs(root, now):
    """Old (40 days), edge (9 days: inside --since minus one day? no), recent (1 day)."""
    old = write_clean_workflow(root, SLUG, "wf_00000000-710", now - timedelta(days=40), agent=1)
    backdate_tree(old, now - timedelta(days=40))
    mid = write_clean_workflow(root, SLUG, "wf_00000000-720", now - timedelta(days=9), agent=2)
    backdate_tree(mid, now - timedelta(days=9))
    new = write_clean_workflow(root, SLUG, "wf_00000000-730", now - timedelta(days=1), agent=3)
    backdate_tree(new, now - timedelta(days=1))
    return old, mid, new


def _count_loads(monkeypatch):
    loaded: list[str] = []
    real = reader.load_run

    def counting(ref):
        loaded.append(getattr(ref, "run_id", str(ref)))
        return real(ref)

    monkeypatch.setattr(reader, "load_run", counting)
    return loaded


def test_since_skips_a_run_whose_newest_file_is_older_than_since_minus_a_day(
    tmp_path, monkeypatch, capsys,
):
    now = datetime.now(timezone.utc)
    _three_runs(tmp_path, now)
    loaded = _count_loads(monkeypatch)
    since = (now - timedelta(days=10)).date().isoformat()
    main(["scan", "--root", str(tmp_path), "--since", since, "--all", "--no-cache"])
    out = capsys.readouterr().out
    assert "wf_00000000-710" not in loaded  # 40 days old: never read
    assert "wf_00000000-720" in loaded and "wf_00000000-730" in loaded
    assert "wf_00000000-710" not in out
    assert "wf_00000000-720" in out and "wf_00000000-730" in out


def test_since_keeps_a_run_written_within_the_one_day_margin(tmp_path, monkeypatch, capsys):
    """A run whose files were last written 12 hours before --since is still read, and then the
    started-at filter decides, exactly as before the pre-filter existed."""
    now = datetime.now(timezone.utc)
    since_day = (now - timedelta(days=5)).date()
    edge_time = datetime(since_day.year, since_day.month, since_day.day, tzinfo=timezone.utc) \
        - timedelta(hours=12)
    edge = write_clean_workflow(tmp_path, SLUG, "wf_00000000-740", edge_time, agent=4)
    backdate_tree(edge, edge_time)
    loaded = _count_loads(monkeypatch)
    main(["scan", "--root", str(tmp_path), "--since", since_day.isoformat(), "--all", "--no-cache"])
    capsys.readouterr()
    assert "wf_00000000-740" in loaded


def test_since_result_set_is_unchanged_by_the_prefilter(tmp_path, monkeypatch, capsys):
    """The pre-filter only skips runs the started-at filter would drop anyway."""
    now = datetime.now(timezone.utc)
    _three_runs(tmp_path, now)
    since = (now - timedelta(days=10)).date().isoformat()
    main(["scan", "--root", str(tmp_path), "--since", since, "--json", "--no-cache"])
    with_filter = capsys.readouterr().out
    monkeypatch.setattr(cli, "_SINCE_MTIME_PREFILTER", False)
    main(["scan", "--root", str(tmp_path), "--since", since, "--json", "--no-cache"])
    without = capsys.readouterr().out
    assert with_filter == without


def test_triage_totals_line_says_since_under_since(tmp_path, capsys):
    now = datetime.now(timezone.utc)
    _three_runs(tmp_path, now)
    since = (now - timedelta(days=10)).date().isoformat()
    main(["scan", "--root", str(tmp_path), "--since", since, "--no-cache"])
    out = capsys.readouterr().out
    assert "ALL TIME" not in out
    assert any(line.startswith(f"SINCE {since}") for line in out.splitlines())


def test_triage_totals_line_keeps_all_time_without_since(tmp_path, capsys):
    now = datetime.now(timezone.utc)
    _three_runs(tmp_path, now)
    main(["scan", "--root", str(tmp_path), "--no-cache"])
    out = capsys.readouterr().out
    assert any(line.startswith("ALL TIME") for line in out.splitlines())


class _TTY(io.StringIO):
    def isatty(self) -> bool:  # noqa: D401
        return True


def test_progress_on_a_terminal_updates_one_line_and_erases_it(tmp_path, monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    _three_runs(tmp_path, now)
    fake = _TTY()
    monkeypatch.setattr(cli.sys, "stderr", fake)
    monkeypatch.setattr(cli, "_PROGRESS_MIN_INTERVAL", 0.0)
    main(["scan", "--root", str(tmp_path), "--no-cache"])
    err = fake.getvalue()
    assert "\rscanning 1/3 runs" in err and "\rscanning 3/3 runs" in err
    assert err.endswith("\r\x1b[K")  # erased: nothing left on the line
    assert "\n" not in err


def test_progress_off_a_terminal_is_one_line_only_for_a_large_scan(tmp_path, monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    _three_runs(tmp_path, now)
    monkeypatch.setattr(cli, "_PROGRESS_NOTE_MIN_RUNS", 2)
    main(["scan", "--root", str(tmp_path), "--no-cache"])
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert err.startswith("postmortem: scanning 3 runs")


def test_progress_off_a_terminal_is_silent_for_a_small_scan(tmp_path, capsys):
    now = datetime.now(timezone.utc)
    _three_runs(tmp_path, now)
    main(["scan", "--root", str(tmp_path), "--no-cache"])
    assert capsys.readouterr().err == ""
