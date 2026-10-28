"""R1 (follow-up to 0aa01f3): the scan reason for a runtime kill and for a rule (c) candidate
carries the numbers of the run report's WORK AT RISK count line (docs/DESIGN.md 4.7), never
ledger.risk_paths' (which also adds facts.paths_written). Fixtures are synthetic and built
fresh under tmp_path; the wf_ ids are already in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import timedelta

from agent_postmortem import reader, report
from agent_postmortem.cli import main
from tests.n3b_helpers import CASES, FIXTURE_ROOT
from tests.test_scan_triage import (
    KILL_SENTINEL,
    _aid,
    _assistant_text,
    _assistant_tool_use,
    _freeze_now,
    _iso,
    _tool_result,
    _user,
    _wf_dir,
    _write_journal,
    _write_meta,
    _write_transcript,
)

REASON_SHAPES = (
    re.compile(r"^\d+ attempts? killed by the runtime, "),
    re.compile(r"^\d+ attempts? at risk: "),
)


def _no_cwd(event: dict) -> dict:
    event = dict(event)
    event.pop("cwd")
    return event


def _relative_edit_root(tmp_path, monkeypatch, *, silence_kill: bool):
    """A killed recent attempt whose only write is a successful relative-path Edit and whose
    transcript names no cwd: the ledger entry has no path, `facts.paths_written` has one."""
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    wf_dir = _wf_dir(root, "-home-user-scan-r1", "wf_00000000-8c0")
    a1, a2 = _aid(1), _aid(2)
    t0 = now - timedelta(hours=1)
    _write_journal(wf_dir, [
        {"type": "started", "timestamp": _iso(t0), "key": "v2:retry", "agentId": a1,
         "label": "impl:retry", "phase": "Implement"},
        {"type": "started", "timestamp": _iso(t0 + timedelta(minutes=5)), "key": "v2:retry",
         "agentId": a2, "label": "impl:retry", "phase": "Implement"},
        {"type": "result", "timestamp": _iso(t0 + timedelta(minutes=10)), "key": "v2:retry",
         "agentId": a2, "result": {"ok": True}},
    ])
    _write_meta(wf_dir, a1, "Implement retry.")
    _write_meta(wf_dir, a2, "Implement retry.")
    _write_transcript(wf_dir, a1, [
        _no_cwd(_user(a1, t0, "Implement retry.")),
        _no_cwd(_assistant_tool_use(
            a1, t0 + timedelta(seconds=5), "toolu_1", "Edit",
            {"file_path": "notes/file.md", "old_string": "a", "new_string": "b"},
        )),
        _no_cwd(_tool_result(a1, t0 + timedelta(seconds=6), "toolu_1", "Edit applied")),
        _no_cwd(_user(a1, t0 + timedelta(minutes=4), KILL_SENTINEL)),
    ])
    _write_transcript(wf_dir, a2, [
        _user(a2, t0 + timedelta(minutes=5), "Implement retry."),
        _assistant_text(a2, t0 + timedelta(minutes=6), "Done."),
    ])
    off = ["silent_retry", "mislabeled_kill"] if silence_kill else ["silent_retry"]
    config_path = tmp_path / "config.toml"
    config_path.write_text(f"[detectors]\noff = {json.dumps(off)}\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))
    return root


def _scan_rows(root, capsys):
    capsys.readouterr()
    code = main(["scan", "--all", "--json", "--root", str(root)])
    captured = capsys.readouterr()
    assert code != 2, captured.err
    return json.loads(captured.out)["rows"]


def _risk_count_line(root, run_id, capsys):
    [ref] = [r for r in reader.discover_runs(root) if r.run_id == run_id]
    capsys.readouterr()
    main(["run", str(ref.path), "--raw"])
    out = capsys.readouterr().out
    return next(line for line in out.splitlines() if line.startswith("WORK AT RISK  "))


def _scan_numbers(reason: str):
    """(attempts, paths, partial, commits, commit-id-gap flag) from a scan reason."""
    n = int(re.match(r"(\d+) attempts? ", reason).group(1))
    paths = re.search(r"(\d+)(\+?) paths? named", reason)
    commits = re.search(r"(?:(\d+)\+? commits?|(commit count unknown))$", reason)
    c = 0 if commits.group(2) else int(commits.group(1))
    gap = "+" in commits.group(0) or "unknown" in commits.group(0)
    return n, int(paths.group(1)), paths.group(2) == "+", c, gap


def _report_numbers(line: str):
    m = re.match(
        r"WORK AT RISK  (\d+) attempts? at risk: (\d+) paths? named, (\d+) commits? reported"
        r"(?: \((\d+) calls? with a commit command reported no id\))?"
        r"(; the tool cannot see all of what was written)?\.",
        line,
    )
    assert m, line
    return int(m.group(1)), int(m.group(2)), bool(m.group(5)), int(m.group(3)), bool(m.group(4))


def _assert_parity(root, capsys, shapes=None) -> int:
    checked = 0
    for row in _scan_rows(root, capsys):
        reason = row["attention_reason"]
        if not reason or not any(shape.match(reason) for shape in REASON_SHAPES):
            continue
        scan = _scan_numbers(reason)
        report = _report_numbers(_risk_count_line(root, row["run_id"], capsys))
        # the runtime-kill shape leads with the kill count, not the attempt count
        if "killed by the runtime" in reason:
            assert scan[1:3] == report[1:3], (reason, report)
        else:
            assert scan[:3] == report[:3], (reason, report)
        assert scan[3] == report[3], (reason, report)
        assert scan[4] == report[4], (reason, report)
        checked += 1
        if shapes is not None:
            shapes["kill" if "killed by the runtime" in reason else "rule_c"] += 1
    return checked


def test_rule_c_reason_for_a_relative_edit_without_cwd_matches_work_at_risk(
    tmp_path, monkeypatch, capsys,
):
    root = _relative_edit_root(tmp_path, monkeypatch, silence_kill=True)
    [row] = [r for r in _scan_rows(root, capsys) if r["attention"]]
    assert row["attention_reason"] == "2 attempts at risk: 0+ paths named, 0 commits"
    line = _risk_count_line(root, row["run_id"], capsys)
    assert line.startswith("WORK AT RISK  2 attempts at risk: 0 paths named, 0 commits reported")
    assert _assert_parity(root, capsys) == 1


def test_runtime_kill_reason_for_a_relative_edit_without_cwd_matches_work_at_risk(
    tmp_path, monkeypatch, capsys,
):
    root = _relative_edit_root(tmp_path, monkeypatch, silence_kill=False)
    reasons = [r["attention_reason"] for r in _scan_rows(root, capsys) if r["attention"]]
    assert len(reasons) == 1 and "killed by the runtime" in reasons[0], reasons
    assert "1+ paths" not in reasons[0] and "1 path " not in reasons[0]
    assert _assert_parity(root, capsys) == 1


def _pin_clock_after_latest_end(monkeypatch, root) -> bool:
    """Pin the scan clock one hour after the latest run end under `root`, so the fixtures'
    runs fall inside the 72-hour window whatever the real date is."""
    ends = [reader.load_run(ref).ended_at for ref in reader.discover_runs(root)]
    ends = [end for end in ends if end]
    if not ends:
        return False
    pinned = max(ends) + timedelta(hours=1)
    monkeypatch.setattr(report, "_now", lambda: pinned)
    return True


def test_scan_reason_numbers_equal_the_work_at_risk_line_over_every_fixture_root(
    tmp_path, monkeypatch, capsys,
):
    shapes = Counter()
    for case in CASES:
        root = FIXTURE_ROOT / case
        if _pin_clock_after_latest_end(monkeypatch, root):
            _assert_parity(root, capsys, shapes)
    # no checked-in fixture is a rule (c) candidate: add the synthetic one
    _assert_parity(_relative_edit_root(tmp_path, monkeypatch, silence_kill=True), capsys, shapes)
    assert shapes["kill"] >= 1, shapes
    assert shapes["rule_c"] >= 1, shapes
