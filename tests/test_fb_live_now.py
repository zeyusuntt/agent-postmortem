"""Part FB2 (field milestone F, P1): LIVE NOW is its own group in `scan`
(docs/DESIGN.md 4.7, 5.3b; .reviews/v0.3/F-plan.md FB2).

The field case: five projects reported that a clean session still being worked in showed up
under NEEDS ATTENTION just because it was "possibly live", pushing real problems down and
teaching the reader to ignore the list. The rule: rule (a) of the M3b candidate rules,
"possibly live", alone no longer makes a NEEDS ATTENTION candidate; it puts the run under a
LIVE NOW group printed after NEEDS ATTENTION, one line per run. A possibly-live run that is
DEGRADED or FAILED, or has a path at risk (rules (b) to (d)), stays under NEEDS ATTENTION
only. JSON rows keep `possibly_live`; `attention` is true only through (b) to (d) and
`attention_reason` is null whenever `attention` is false.

Fixtures are synthetic and built fresh under tmp_path; `report._now` is frozen at the real
wall clock so a fresh file's mtime reads as "possibly live" and a backdated one does not.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

from agent_postmortem import reader, report
from tests import n3b_helpers as h
from tests.n6_helpers import SLUG_A, entry, session_path, uid, write_lines
from tests.test_scan_triage import (
    KILL_SENTINEL,
    _aid,
    _assistant_text,
    _freeze_now,
    _iso,
    _user,
    _wf_dir,
    _write_journal,
    _write_meta,
    _write_transcript,
)

CWD = "/home/user/project"


def _session(root, n, end, *, live: bool, slug=SLUG_A):
    """A one-turn session ending at `end`; its file is left fresh (possibly live) or backdated
    to `end` (stale)."""
    path = session_path(root, slug, uid(n))
    write_lines(path, [
        entry("user", "hello", end - timedelta(seconds=1), cwd=CWD, session=uid(n)),
        entry("assistant", "done", end, cwd=CWD, session=uid(n)),
    ])
    if not live:
        os.utime(path, (end.timestamp(), end.timestamp()))
    return path


def _live_degraded_workflow(root, now, wf_id="wf_00000000-710"):
    """A workflow still running (no terminal record, files fresh) whose first attempt was
    killed and retried: DEGRADED by silent_retry, ended within 72 hours."""
    wf_dir = _wf_dir(root, "-home-user-live-degraded", wf_id)
    a1, a2 = _aid(201), _aid(202)
    t0 = now - timedelta(minutes=5)
    _write_journal(wf_dir, [
        {"type": "started", "timestamp": _iso(t0), "key": "v2:x", "agentId": a1,
         "label": "impl:x", "phase": "Implement"},
        {"type": "started", "timestamp": _iso(t0 + timedelta(minutes=1)), "key": "v2:x",
         "agentId": a2, "label": "impl:x", "phase": "Implement"},
    ])
    _write_meta(wf_dir, a1, "Implement x.")
    _write_meta(wf_dir, a2, "Implement x.")
    _write_transcript(wf_dir, a1, [
        _user(a1, t0, "Implement x."),
        _assistant_text(a1, t0 + timedelta(seconds=5), "Working."),
        _user(a1, t0 + timedelta(seconds=10), KILL_SENTINEL),
    ])
    _write_transcript(wf_dir, a2, [
        _user(a2, t0 + timedelta(minutes=1), "Retry: implement x."),
        _assistant_text(a2, t0 + timedelta(minutes=1, seconds=20), "Still working."),
    ])
    return wf_dir


def _scan(capsys, root, *flags):
    return h.cli(capsys, "scan", "--root", root, *flags)


def _json_rows(capsys, root, *flags):
    code, out, err = _scan(capsys, root, "--json", *flags)
    assert code in (0, 3, 4), err
    return {row["run_id"]: row for row in json.loads(out)["rows"]}


def _group(out: str, header_start: str) -> list[str]:
    """The lines of one group: its header and the indented lines under it."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(header_start))
    found = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith("  ") and not line.startswith("  full list"):
            found.append(line)
        else:
            break
    return found


def _id(path) -> str:
    return reader.load_run(path).run_id


# --- the groups ----------------------------------------------------------------------------


def test_clean_live_sessions_appear_only_under_live_now(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    older = _session(root, 1, now - timedelta(minutes=3), live=True)
    newer = _session(root, 2, now - timedelta(minutes=1), live=True)
    code, out, _err = _scan(capsys, root)
    assert code == 0
    assert _group(out, "NEEDS ATTENTION") == [
        "NEEDS ATTENTION (0 of 2 runs)", "  nothing needs attention",
        # docs/DESIGN.md 4.8 (0.3.4): childless sessions only, so the scan says so
        "  this scan found no subagent transcript and no Workflow run, which is what "
        "agent-postmortem checks; agent-postmortem demo shows a sample report",
    ]
    assert _group(out, "LIVE NOW") == [
        "LIVE NOW (2 of 2 runs)",
        f"  {_id(newer)}  possibly live  CLEAN  agent-postmortem run {_id(newer)}",
        f"  {_id(older)}  possibly live  CLEAN  agent-postmortem run {_id(older)}",
    ]
    # LIVE NOW comes after NEEDS ATTENTION and before the totals
    lines = out.splitlines()
    index = {name: next(i for i, line in enumerate(lines) if line.startswith(name))
             for name in ("NEEDS ATTENTION", "LIVE NOW", "LAST 7 DAYS", "ALL TIME")}
    assert index["NEEDS ATTENTION"] < index["LIVE NOW"] < index["LAST 7 DAYS"] < index["ALL TIME"]


def test_a_live_degraded_run_stays_under_needs_attention_only(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    wf_dir = _live_degraded_workflow(root, now)
    clean = _session(root, 1, now - timedelta(minutes=1), live=True)
    rows = _json_rows(capsys, root)
    row = rows["wf_00000000-710"]
    assert row["possibly_live"] is True
    assert row["verdict"].startswith("DEGRADED")
    assert row["attention"] is True and row["attention_reason"]
    code, out, _err = _scan(capsys, root)
    assert code == 4 or code == 0
    needs = _group(out, "NEEDS ATTENTION")
    assert needs[0] == "NEEDS ATTENTION (1 of 2 runs)"
    assert any(line.strip().startswith("wf_00000000-710") for line in needs[1:])
    live = _group(out, "LIVE NOW")
    assert live[0] == "LIVE NOW (1 of 2 runs)"
    assert [line for line in live[1:] if "wf_00000000-710" in line] == []
    assert any(_id(clean) in line for line in live[1:])
    assert wf_dir.is_dir()


def test_json_for_a_clean_live_row(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    live = _session(root, 1, now - timedelta(minutes=1), live=True)
    row = _json_rows(capsys, root)[_id(live)]
    assert row["possibly_live"] is True
    assert row["attention"] is False
    assert row["attention_reason"] is None
    assert row["age"] == "possibly live"
    assert row["verdict"] == "CLEAN"


def test_json_attention_reason_is_null_whenever_attention_is_false(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _session(root, 1, now - timedelta(minutes=1), live=True)
    _session(root, 2, now - timedelta(days=5), live=False)
    _live_degraded_workflow(root, now)
    for row in _json_rows(capsys, root).values():
        if not row["attention"]:
            assert row["attention_reason"] is None
        else:
            assert isinstance(row["attention_reason"], str) and row["attention_reason"]


def test_a_stale_clean_session_is_in_neither_group(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _session(root, 1, now - timedelta(hours=2), live=False)
    code, out, _err = _scan(capsys, root)
    assert code == 0
    assert "LIVE NOW" not in out
    assert "NEEDS ATTENTION (0 of 1 runs)" in out


def test_no_live_run_means_no_live_now_header(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _session(root, 1, now - timedelta(hours=2), live=False)
    _session(root, 2, now - timedelta(hours=3), live=False)
    out = _scan(capsys, root)[1]
    assert "LIVE NOW" not in out


# --- truncation ----------------------------------------------------------------------------


def test_live_now_is_capped_by_limit_with_its_own_showing_line(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    paths = [_session(root, n, now - timedelta(minutes=n), live=True) for n in range(1, 6)]
    code, out, _err = _scan(capsys, root, "--limit", "2")
    assert code == 0
    live = _group(out, "LIVE NOW")
    assert live[0] == "LIVE NOW (5 of 5 runs; showing 2 of 5, use --limit N or --all for more)"
    assert len(live) == 3
    # the most recent activity first
    assert _id(paths[0]) in live[1] and _id(paths[1]) in live[2]
    # without truncation the header is plain
    out_all = _scan(capsys, root, "--limit", "5")[1]
    assert _group(out_all, "LIVE NOW")[0] == "LIVE NOW (5 of 5 runs)"


def test_needs_attention_header_says_showing_when_it_truncates(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    for index in range(4):
        _live_degraded_workflow(root, now, wf_id=f"wf_00000000-fb{index}")
    out = _scan(capsys, root, "--limit", "3")[1]
    needs = _group(out, "NEEDS ATTENTION")
    assert needs[0] == "NEEDS ATTENTION (4 of 4 runs; showing 3, use --limit N or --all for more)"
    full = _scan(capsys, root, "--limit", "4")[1]
    assert _group(full, "NEEDS ATTENTION")[0] == "NEEDS ATTENTION (4 of 4 runs)"
    # a truncated list never leaks into LIVE NOW: every one of them is a candidate
    assert "LIVE NOW" not in out


# --- what does not change ------------------------------------------------------------------


@pytest.mark.parametrize("flags", [["--local"], ["--raw"], []])
def test_live_now_lines_are_the_same_at_every_level(tmp_path, monkeypatch, capsys, flags):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    live = _session(root, 1, now - timedelta(minutes=1), live=True)
    out = _scan(capsys, root, *flags)[1]
    assert _group(out, "LIVE NOW")[1] == (
        f"  {_id(live)}  possibly live  CLEAN  agent-postmortem run {_id(live)}"
    )
    assert str(root) not in out and uid(1) not in out


def test_scan_all_has_no_live_now_group(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _session(root, 1, now - timedelta(minutes=1), live=True)
    out = _scan(capsys, root, "--all")[1]
    assert "LIVE NOW" not in out and "NEEDS ATTENTION" not in out


def test_is_attention_candidate_rule_a_alone_is_not_enough(tmp_path, monkeypatch):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    path = _session(root, 1, now - timedelta(minutes=1), live=True)
    run = reader.load_run(path)
    assert report.is_attention_candidate(run, [], now, True) is False
    assert report.is_attention_candidate(run, [], now, False) is False


def test_design_states_live_now_and_the_narrowed_rule_a():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "docs" / "DESIGN.md").read_text("utf-8")
    start = text.index("`scan` (unit M3b) defaults to a triage view")
    section = text[start:text.index("Silenced detectors (v0.2, unit M3c, 4.10)", start)]
    assert "LIVE NOW" in section
    assert "rule (a)" in section
    assert "showing <k>" in section
    assert "(b) to (d)" in section
    assert "whenever `attention` is false, a clean live run included" in text
