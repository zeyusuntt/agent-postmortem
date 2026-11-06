"""The LAST 7 DAYS footer and the SINCE footer place an undated run the same way
(docs/DESIGN.md 4.7, 4.14.9 item 2 "scan footer counts more runs in one
day than in seven").

A workflow directory whose files carry no timestamp (an empty run: a launch line only) has an
unknown start, which `--since` keeps, while the 7-day window used only the run's own start or
end and so dropped it: `scan --since <today>` printed LAST 7 DAYS 2 and SINCE 3. An undated run
now falls in the 7-day window by the newest time its files were written (a run cannot start
after its last write).
"""

from __future__ import annotations

import os
import re
import time

from agent_postmortem.cli import main
from tests.test_field_v03 import FAKE_SLUG, FAKE_UUID


def _undated_workflow(root, wf_id: str, age_days: float):
    run_dir = root / FAKE_SLUG / FAKE_UUID / "subagents" / "workflows" / wf_id
    run_dir.mkdir(parents=True)
    journal = run_dir / "journal.jsonl"
    journal.write_text('{"type": "launched"}\n', encoding="utf-8")
    when = time.time() - age_days * 86400
    os.utime(journal, (when, when))
    os.utime(run_dir, (when, when))
    return run_dir


def _workflow_count(out: str, label: str) -> int:
    match = re.search(rf"^{re.escape(label)}\s+(\d+) workflow runs?", out, re.M)
    assert match, out
    return int(match.group(1))


def test_an_undated_run_written_today_is_in_the_last_seven_days_too(tmp_path, capsys):
    root = tmp_path / "projects"
    _undated_workflow(root, "wf_00000000-b37", age_days=0)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    main(["scan", "--root", str(root), "--since", today, "--no-cache"])
    out = capsys.readouterr().out
    assert _workflow_count(out, f"SINCE {today}") == 1
    assert _workflow_count(out, "LAST 7 DAYS") == 1


def test_an_undated_run_last_written_long_ago_is_outside_the_seven_days(tmp_path, capsys):
    root = tmp_path / "projects"
    _undated_workflow(root, "wf_00000000-b38", age_days=40)
    main(["scan", "--root", str(root), "--no-cache"])
    out = capsys.readouterr().out
    assert _workflow_count(out, "ALL TIME") == 1
    assert _workflow_count(out, "LAST 7 DAYS") == 0
