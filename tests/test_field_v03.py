"""Field regression fixtures for the v0.3 evidence model (docs/DESIGN.md
4.1, 4.13, 5.3b; revised contract, commit 3446313, N0r).

Source: `.reviews/v0.3/N0r-decisions.md` D14 to D17 and D19, written against
`.reviews/v0.3/N0-codex.txt` findings 14, 15, 16 (test cases only, not the
triage-clause additions it also asks for), 17, and 19.

Each field case from the original N0 proposal is split in two, where both
levels apply (D14):

- a MODEL-LEVEL test, at the exact milestone DESIGN.md 4.1/4.13 names for
  that field (N1 MarkerHit and the parent index's generations, N2
  Run.lifecycle, N4 Attempt.ledger), reading the new field with
  `getattr(obj, "<field>", None)` so that today -- before any of N1 to N9
  exist -- the assertion fails with a plain AssertionError (the value is
  None), never an AttributeError or an ImportError. No v0.3-only class
  (MarkerHit, Ledger, LedgerEntry, Lifecycle, Generation) is imported by
  name anywhere in this file, for the same reason: importing one would turn
  every test in the file into a collection-time ImportError instead of a
  clean, per-test AssertionError.
- a PUBLIC test, through the same public surfaces the old N0 file used
  (reader.load_run, detectors.run_detectors, cli.main's text/JSON output,
  rescue via cli.main), labeled by the milestone that makes it true. A
  public test that reads a schema-3-only JSON shape (Attempt.ledger,
  Run.lifecycle, rescue plan's split paths[] rows) asserts
  `payload["schema"] == 3` first, per D14, since nothing in schema 2 (the
  shape every command keeps emitting through N4, 5.3b's activation
  paragraph) can satisfy it; a public test whose JSON or text surface
  already exists in schema 2 (Finding fields, AttemptFacts.commits, the run
  header, Run.status) is not schema-gated, since the fix lands before N5
  and is visible immediately once its own milestone does.

D17's relabeling: N1 tests assert the parent index's accepted generations
and cache invalidation directly, never through the workflow_failed
detector; N2 tests assert Run.lifecycle and workflow_failed; N4 tests
assert the ledger model; the WORK AT RISK text (and its schema-3 JSON
mirror) is a rendering assertion and stays N5 even when the ledger facts
behind it land at N4; the session brief is N5; parent fingerprinting is N1
only.

field_cat_backgrounded_still_counts (case 4) and field_quoted_notification_
ignored (case 10) already pass under v0.2 and stay plain, unmarked
regression pins (D19): they must keep passing in both the normal and
`--runxfail` runs.

Fixture conventions (CLAUDE.md, tests/test_privacy.py) are unchanged from
the original file: the one shared fake session id, cwd under
/home/user/project or /tmp/agent-postmortem-fixture/..., slug
-home-user-project, agent ids 'a' + 16 hex chars, and every wf_ id
registered in tests/synthetic-ids.txt (wf_00000000-f01 through
wf_00000000-f10 plus wf_00000000-f0e, the same set the original file
allocated; this revision adds no new wf_ id, so synthetic-ids.txt is
unchanged). Case 14's fixture moves from in-memory dataclasses to an
on-disk session transcript (main plus two subagents); it still builds that
session at test time, under the test's own tmp_path, never as a committed
tests/fixtures/ directory, so tests/fixtures/README.md needs no new entry
either.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_postmortem import cli, detectors, reader, report

FAKE_SLUG = "-home-user-project"
FAKE_UUID = "00000000-0000-4000-8000-000000000000"


# --- generic builders (mirrors tests/test_rescue.py and test_scan_triage.py) -


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aid(n: int) -> str:
    """A conforming agent id (reader._AGENT_ID_RE: 'a' + 16 hex chars),
    distinct per case so none collides with another fixture in this file."""
    return "a" + "f" + format(n, "015x")


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _user(text, ts: datetime, agent_id: str | None = None) -> dict:
    entry: dict = {"type": "user", "message": {"role": "user", "content": text}, "timestamp": _iso(ts)}
    if agent_id is not None:
        entry["agentId"] = agent_id
    return entry


def _assistant_text(text: str, ts: datetime, agent_id: str | None = None) -> dict:
    entry: dict = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}], "usage": {}},
        "timestamp": _iso(ts),
    }
    if agent_id is not None:
        entry["agentId"] = agent_id
    return entry


def _assistant_tool(tool_id: str, name: str, input_obj: dict, ts: datetime,
                     agent_id: str | None = None) -> dict:
    entry: dict = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": input_obj}],
            "usage": {},
        },
        "timestamp": _iso(ts),
    }
    if agent_id is not None:
        entry["agentId"] = agent_id
    return entry


def _tool_result(tool_id: str, content, ts: datetime, agent_id: str | None = None,
                  is_error: bool = False) -> dict:
    entry: dict = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content,
                         "is_error": is_error}],
        },
        "timestamp": _iso(ts),
    }
    if agent_id is not None:
        entry["agentId"] = agent_id
    return entry


_KILL_SENTINEL = "[Request interrupted by user]"


def _kill(ts: datetime, agent_id: str | None = None) -> dict:
    return _user(_KILL_SENTINEL, ts, agent_id)


def _wf_dir(root: Path, wf_id: str, *, slug: str = FAKE_SLUG, session: str = FAKE_UUID) -> Path:
    return root / slug / session / "subagents" / "workflows" / wf_id


def _parent_path(root: Path, *, slug: str = FAKE_SLUG, session: str = FAKE_UUID) -> Path:
    return root / slug / f"{session}.jsonl"


def _no_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "does-not-exist.toml"))


def _simple_completed_journal(run_dir: Path, agent_id: str, key: str = "v2:x") -> None:
    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": key, "agentId": agent_id, "label": "impl", "phase": "Implement"},
        {"type": "result", "key": key, "agentId": agent_id, "result": {"ok": True}},
    ])


# --- notification / parent-transcript builders (docs/DESIGN.md 4.13.3) ------


def _workflow_launch(tool_id: str, ts: datetime, resume_from: str | None = None) -> dict:
    input_obj = {"resumeFromRunId": resume_from} if resume_from else {}
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": "Workflow", "input": input_obj}],
            "usage": {},
        },
        "timestamp": _iso(ts),
    }


def _workflow_launch_result(tool_id: str, ts: datetime, run_id: str, task_id: str,
                             status: str = "async_launched") -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "launched"}],
        },
        "toolUseResult": {"runId": run_id, "taskId": task_id, "status": status},
        "timestamp": _iso(ts),
    }


def _envelope(task_id: str, tool_id: str, status: str, *, output_file: str = "/tmp/o",
              pad: int = 0) -> str:
    body = (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_id}</tool-use-id>\n"
        f"<output-file>{output_file}</output-file>\n"
        f"<status>{status}</status>\n"
        "</task-notification>"
    )
    return body + (" " * pad)


def _notification_entry(ts: datetime, envelope_text: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": envelope_text},
        "origin": {"kind": "task-notification"},
        "timestamp": _iso(ts),
    }


def _queue_notification_entry(ts: datetime, envelope_text: str) -> dict:
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "content": envelope_text,
        "timestamp": _iso(ts),
    }


# --- small helpers shared by the model-level tests below -------------------


def _lifecycle(run) -> object | None:
    return getattr(run, "lifecycle", None)


def _lifecycle_field(run, name: str):
    return getattr(_lifecycle(run), name, None)


def _ledger(attempt) -> object | None:
    return getattr(attempt, "ledger", None)


def _ledger_entries(attempt) -> list | None:
    return getattr(_ledger(attempt), "entries", None)


# =============================================================================
# 1. field_shell_writes_invisible (model N4, public N5)
# =============================================================================


def _case1_fixture(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f01"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(1)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": agent_id, "label": "impl", "phase": "Implement"},
    ])
    events = [
        _user("Write the output file.", base, agent_id),
        _assistant_tool(
            "t1", "Bash",
            {"command": "python3 -c \"open('out.txt','w').write('x')\""},
            base + timedelta(seconds=1), agent_id,
        ),
        _tool_result("t1", "", base + timedelta(seconds=2), agent_id),
        _assistant_tool("t2", "Bash", {"command": "sed -n 1p f"}, base + timedelta(seconds=3), agent_id),
        _tool_result("t2", "line one", base + timedelta(seconds=4), agent_id),
        _kill(base + timedelta(seconds=5), agent_id),
    ]
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", events)
    _no_config(monkeypatch, tmp_path)
    return run_dir, agent_id


def test_field_shell_writes_invisible_model(tmp_path, monkeypatch):
    run_dir, agent_id = _case1_fixture(tmp_path, monkeypatch)

    run = reader.load_run(run_dir)
    attempt = run.attempts[0]
    entries = _ledger_entries(attempt)
    # Only t1 (the opaque python3 write) gets an entry; t2 ('sed -n 1p f') is
    # in the closed read-only grammar (4.13.1) and gets none at all.
    assert entries is not None and len(entries) == 1
    entry = entries[0]
    assert getattr(entry, "effect", None) == "possible"
    assert getattr(entry, "basis", None) == "opaque_command"
    assert getattr(entry, "path", "unset") is None
    ledger = _ledger(attempt)
    assert getattr(ledger, "completeness", None) == "partial"
    assert "possible_opaque_command" in (getattr(ledger, "reasons", None) or [])


def test_field_shell_writes_invisible(tmp_path, monkeypatch, capsys):
    run_dir, _agent_id = _case1_fixture(tmp_path, monkeypatch)

    cli.main(["run", str(run_dir), "--json", "--no-redact"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == 3
    ledger = payload["run"]["attempts"][0]["ledger"]
    assert ledger["completeness"] == "partial"
    assert len(ledger["entries"]) == 1
    assert ledger["entries"][0]["effect"] == "possible"
    assert ledger["entries"][0]["basis"] == "opaque_command"

    cli.main(["run", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    work_at_risk_line = next(line for line in out.splitlines() if line.startswith("WORK AT RISK"))
    assert "none" not in work_at_risk_line


# =============================================================================
# 2. field_git_c_commit (model N4, public N4)
# =============================================================================


def _case2_fixture(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f02"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(2)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _simple_completed_journal(run_dir, agent_id)
    events = [
        _user("Commit the change.", base, agent_id),
        _assistant_tool(
            "t1", "Bash",
            {"command": "git -C /tmp/agent-postmortem-fixture/repo -c user.name=x commit -m msg"},
            base + timedelta(seconds=1), agent_id,
        ),
        _tool_result("t1", "[main 1a2b3c4] msg", base + timedelta(seconds=2), agent_id),
        _assistant_text("Done.", base + timedelta(seconds=3), agent_id),
    ]
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", events)
    _no_config(monkeypatch, tmp_path)
    return run_dir, agent_id


def test_field_git_c_commit_model(tmp_path, monkeypatch):
    run_dir, _agent_id = _case2_fixture(tmp_path, monkeypatch)

    run = reader.load_run(run_dir)
    attempt = run.attempts[0]
    # facts.commits already exists (v0.2); the bug is the naive 'git commit'
    # substring test missing the '-C'/'-c' global options before the
    # subcommand (docs/DESIGN.md 4.13.1's commit grammar).
    assert "1a2b3c4" in attempt.facts.commits
    entries = _ledger_entries(attempt)
    assert entries is not None and len(entries) == 1
    # repo follows the -C argument, resolved lexically (D7); it is already
    # absolute here, so call_cwd (unset on this tool_use) never matters.
    assert getattr(entries[0], "repo", None) == "/tmp/agent-postmortem-fixture/repo"


def test_field_git_c_commit(tmp_path, monkeypatch, capsys):
    run_dir, _agent_id = _case2_fixture(tmp_path, monkeypatch)

    cli.main(["run", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    row_line = next(line for line in out.splitlines() if line.startswith("impl"))
    header = next(line for line in out.splitlines() if line.startswith("AGENT"))
    # N4a: the COMMITS column is found by its header; the earlier fixed index 5
    # named the PATHS column (AGENT PHASE ATTEMPTS END KILLED PATHS COMMITS).
    commits_token = row_line.split()[header.split().index("COMMITS")]
    assert commits_token == "1"

    cli.main(["run", str(run_dir), "--json", "--no-redact"])
    payload = json.loads(capsys.readouterr().out)
    facts = payload["run"]["attempts"][0]["facts"]
    assert "1a2b3c4" in facts["commits"]


# =============================================================================
# 3. field_self_quote_backgrounded (model N1, public N1)
# =============================================================================


def _case3_fixture(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f03"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(3)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _simple_completed_journal(run_dir, agent_id)
    quoted_report = (
        "RUN wf_00000000-xxx  DEGRADED  ended 2026-09-30 10:00Z (1 day ago)\n"
        "1 agents took 2 attempts.\n"
        "Command did not complete within its 120s timeout and was moved to the background\n"
    )
    events = [
        _user("Check on the earlier run.", base, agent_id),
        _assistant_tool(
            "t1", "Bash",
            {"command": "agent-postmortem run wf_00000000-xxx --verbose"},
            base + timedelta(seconds=1), agent_id,
        ),
        _tool_result("t1", quoted_report, base + timedelta(seconds=2), agent_id),
        _assistant_text("Noted.", base + timedelta(seconds=3), agent_id),
    ]
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", events)
    _no_config(monkeypatch, tmp_path)
    return run_dir, agent_id


# N1 (implemented): a marker quoted inside a command's own payload text is
# not an anchored MarkerHit.
def test_field_self_quote_backgrounded_model(tmp_path, monkeypatch):
    run_dir, _agent_id = _case3_fixture(tmp_path, monkeypatch)

    run = reader.load_run(run_dir)
    attempt = run.attempts[0]
    marker_hits = getattr(attempt, "marker_hits", None)
    anchored_hits = [
        hit for hit in (marker_hits or [])
        if getattr(hit, "marker", None) == "bash_backgrounded" and getattr(hit, "anchored", False)
    ]
    assert marker_hits is not None and not anchored_hits


# N1 (implemented): a marker quoted inside a command's own payload text is
# not an anchored hit, so the detector must not fire on it.
def test_field_self_quote_backgrounded(tmp_path, monkeypatch):
    run_dir, _agent_id = _case3_fixture(tmp_path, monkeypatch)

    run = reader.load_run(run_dir)
    findings = detectors.run_detectors(run)
    assert not any(f.detector == "bash_backgrounded" for f in findings)


# =============================================================================
# 4. field_cat_backgrounded_still_counts (N1) -- NOT xfail: passes today,
# pins the behavior N1 must keep (D19).
# =============================================================================


def test_field_cat_backgrounded_still_counts(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f04"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(4)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _simple_completed_journal(run_dir, agent_id)
    harness_notice = (
        "Command did not complete within its 120s timeout and was moved to the "
        "background (ID: b1). Output is being written to: /tmp/x"
    )
    events = [
        _user("Show me the log.", base, agent_id),
        _assistant_tool("t1", "Bash", {"command": "cat big.log"}, base + timedelta(seconds=1), agent_id),
        _tool_result("t1", harness_notice, base + timedelta(seconds=2), agent_id),
        _assistant_text("Still running.", base + timedelta(seconds=3), agent_id),
    ]
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", events)
    _no_config(monkeypatch, tmp_path)

    run = reader.load_run(run_dir)
    findings = detectors.run_detectors(run)
    hits = [f for f in findings if f.detector == "bash_backgrounded"]
    assert len(hits) == 1


def test_field_cat_backgrounded_still_counts_public(tmp_path, monkeypatch, capsys):
    """Public level (N5, N9 fix 1): `cat big.log` whose result is the runtime's
    backgrounding notice at byte zero still counts, in the schema-3 JSON
    first and then in the text a person reads."""
    root = tmp_path / "root"
    wf_id = "wf_00000000-f04"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(4)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _simple_completed_journal(run_dir, agent_id)
    harness_notice = (
        "Command did not complete within its 120s timeout and was moved to the "
        "background (ID: b1). Output is being written to: /tmp/x"
    )
    events = [
        _user("Show me the log.", base, agent_id),
        _assistant_tool("t1", "Bash", {"command": "cat big.log"}, base + timedelta(seconds=1), agent_id),
        _tool_result("t1", harness_notice, base + timedelta(seconds=2), agent_id),
        _assistant_text("Still running.", base + timedelta(seconds=3), agent_id),
    ]
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", events)
    _no_config(monkeypatch, tmp_path)

    cli.main(["run", str(run_dir), "--json", "--no-redact"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == 3
    hits = [f for f in payload["findings"] if f["detector"] == "bash_backgrounded"]
    assert len(hits) == 1
    assert hits[0]["severity"] == "minor"
    assert hits[0]["metrics"] == {"count": 1}
    assert hits[0]["rule_ids"] == ["R-NO-LONG-BASH"]
    assert len(hits[0]["evidence"]) == 1
    assert hits[0]["evidence"][0].endswith(f"agent-{agent_id}.jsonl:3 [bash_backgrounded]")
    assert hits[0]["title"] == "1 Bash command timed out and moved to the background"

    cli.main(["run", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert "impl  1 Bash commands backgrounded." in out


# =============================================================================
# 5. field_workflow_failed_reported_clean (model N2, public N2)
# =============================================================================


def _case5_fixture(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f05"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(5)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _simple_completed_journal(run_dir, agent_id)
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do the thing.", base, agent_id),
        _assistant_text("Done.", base + timedelta(seconds=30), agent_id),
    ])

    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", base),
        _workflow_launch_result("toolu_A", base + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(base + timedelta(minutes=2), _envelope("tA", "toolu_A", "failed")),
    ])
    _no_config(monkeypatch, tmp_path)
    return run_dir, parent


def test_field_workflow_failed_reported_clean_model(tmp_path, monkeypatch):
    run_dir, _parent = _case5_fixture(tmp_path, monkeypatch)

    run = reader.load_run(run_dir)
    assert _lifecycle_field(run, "terminal") == "failed"
    assert _lifecycle_field(run, "generations") == 1


def test_field_workflow_failed_reported_clean(tmp_path, monkeypatch, capsys):
    run_dir, parent = _case5_fixture(tmp_path, monkeypatch)

    run = reader.load_run(run_dir)
    findings = detectors.run_detectors(run)
    hits = [f for f in findings if f.detector == "workflow_failed"]
    assert len(hits) == 1
    # The notification is the 3rd physical line of the parent transcript
    # (docs/DESIGN.md 4.13.3: "its evidence is the notification line").
    assert any(ev.startswith(f"{parent.name}:3") for ev in hits[0].evidence)

    cli.main(["run", str(run_dir)])
    out = capsys.readouterr().out
    header = out.splitlines()[0]
    assert "CLEAN" not in header


def test_field_workflow_failed_evidence_pins_the_deciding_snapshot_line(tmp_path, monkeypatch):
    """docs/DESIGN.md 4.13.3 (N1N2 review finding 2): load a run while
    generation A (lines 1-3) decides Run.lifecycle as failed. Appending a
    newer, completed generation B (lines 4-6) to the parent transcript after
    load_run() must not change which line workflow_failed's own evidence
    points at -- the finding's evidence is a snapshot of what decided the
    Lifecycle this detector actually fired from, not whatever the parent
    transcript says right now."""
    root = tmp_path / "root"
    wf_id = "wf_00000000-e3e"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(119)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _simple_completed_journal(run_dir, agent_id)
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do the thing.", base, agent_id),
        _assistant_text("Done.", base + timedelta(seconds=30), agent_id),
    ])

    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", base),
        _workflow_launch_result("toolu_A", base + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(base + timedelta(minutes=2), _envelope("tA", "toolu_A", "failed")),
    ])
    _no_config(monkeypatch, tmp_path)

    run = reader.load_run(run_dir)
    assert run.lifecycle.terminal == "failed"

    # A newer generation, completed, on lines 4-6 -- appended only after
    # load_run() already decided the stored lifecycle from line 3.
    with parent.open("a", encoding="utf-8") as handle:
        for entry in [
            _workflow_launch("toolu_B", base + timedelta(minutes=3)),
            _workflow_launch_result("toolu_B", base + timedelta(minutes=3, seconds=1), wf_id, "tB"),
            _notification_entry(
                base + timedelta(minutes=4), _envelope("tB", "toolu_B", "completed"),
            ),
        ]:
            handle.write(json.dumps(entry) + "\n")

    findings = detectors.run_detectors(run)
    hits = [f for f in findings if f.detector == "workflow_failed"]
    assert len(hits) == 1
    assert any(ev.startswith(f"{parent.name}:3") for ev in hits[0].evidence), hits[0].evidence
    assert not any(ev.startswith(f"{parent.name}:6") for ev in hits[0].evidence), hits[0].evidence


def test_field_lifecycle_to_dict_is_exactly_the_five_public_fields_for_a_failed_generation(
    tmp_path, monkeypatch,
):
    """N1N2 review round 2, new finding 3 (MINOR): Lifecycle carries a
    private _terminal_line (docs/DESIGN.md 4.13.3, N1N2 review finding 2) so
    workflow_failed's evidence never drifts to a newer generation; to_dict()
    must still export exactly the five public fields docs/DESIGN.md 4.1/5.3b
    document, never that private field, even when it is non-null (mutation
    M26: exporting _terminal_line from to_dict() left every existing
    lifecycle/field/report/CLI test passing)."""
    root = tmp_path / "root"
    wf_id = "wf_00000000-e45"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(925)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _simple_completed_journal(run_dir, agent_id)
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do the thing.", base, agent_id),
        _assistant_text("Done.", base + timedelta(seconds=30), agent_id),
    ])

    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", base),
        _workflow_launch_result("toolu_A", base + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(base + timedelta(minutes=1), _envelope("tA", "toolu_A", "failed")),
    ])
    _no_config(monkeypatch, tmp_path)

    run = reader.load_run(run_dir)
    assert run.lifecycle.terminal == "failed"
    assert run.lifecycle._terminal_line == 3  # the private deciding line is non-null

    payload = run.lifecycle.to_dict()
    assert set(payload.keys()) == {"terminal", "source", "activity", "generations", "diagnostics"}
    assert payload == {
        "terminal": "failed",
        "source": "notification",
        "activity": run.lifecycle.activity,
        "generations": 1,
        "diagnostics": [],
    }


# =============================================================================
# 6. field_failed_generation_completed_attempt (model N2 + N4, public N5)
# =============================================================================


def _case6_fixture(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f06"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(6)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _simple_completed_journal(run_dir, agent_id)
    events = [
        _user("Write the output file.", base, agent_id),
        _assistant_tool(
            "t1", "Bash",
            {"command": "python3 -c \"open('/tmp/agent-postmortem-fixture/out.txt','w').write('y')\""},
            base + timedelta(seconds=1), agent_id,
        ),
        _tool_result("t1", "", base + timedelta(seconds=2), agent_id),
        _assistant_text("Done.", base + timedelta(seconds=3), agent_id),
    ]
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", events)

    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", base),
        _workflow_launch_result("toolu_A", base + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(base + timedelta(minutes=2), _envelope("tA", "toolu_A", "failed")),
    ])
    _no_config(monkeypatch, tmp_path)
    return run_dir


def test_field_failed_generation_completed_attempt_lifecycle(tmp_path, monkeypatch):
    run_dir = _case6_fixture(tmp_path, monkeypatch)

    run = reader.load_run(run_dir)
    assert _lifecycle_field(run, "terminal") == "failed"


def test_field_failed_generation_completed_attempt_ledger(tmp_path, monkeypatch):
    run_dir = _case6_fixture(tmp_path, monkeypatch)

    run = reader.load_run(run_dir)
    attempt = run.attempts[0]
    assert attempt.outcome == "completed"
    entries = _ledger_entries(attempt)
    assert entries is not None and len(entries) == 1
    assert getattr(entries[0], "effect", None) == "possible"


def test_field_failed_generation_completed_attempt(tmp_path, monkeypatch, capsys):
    run_dir = _case6_fixture(tmp_path, monkeypatch)

    cli.main(["run", str(run_dir), "--json", "--no-redact"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == 3
    ledger = payload["run"]["attempts"][0]["ledger"]
    assert len(ledger["entries"]) == 1

    cli.main(["run", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    work_at_risk_line = next(line for line in out.splitlines() if line.startswith("WORK AT RISK"))
    assert "none" not in work_at_risk_line


# =============================================================================
# 7. field_resumed_run_unfinished (model N2, public N2)
# =============================================================================


def _case7_fixture(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f07"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(7)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    # The run's own journal key never resolved (today's naive "unfinished"
    # signal); the transcript itself just stops, no kill sentinel.
    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": agent_id, "label": "impl", "phase": "Implement"},
    ])
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do the thing.", base, agent_id),
        _assistant_text("Working on it.", base + timedelta(seconds=5), agent_id),
    ])

    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", base),
        _workflow_launch_result("toolu_A", base + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(base + timedelta(minutes=1), _envelope("tA", "toolu_A", "stopped")),
        _workflow_launch("toolu_B", base + timedelta(minutes=2), resume_from=wf_id),
        _workflow_launch_result("toolu_B", base + timedelta(minutes=2, seconds=1), wf_id, "tB"),
        _notification_entry(
            base + timedelta(minutes=3), _envelope("tB", "toolu_B", "completed"),
        ),
    ])
    _no_config(monkeypatch, tmp_path)
    return root, run_dir, wf_id


def test_field_resumed_run_unfinished_model(tmp_path, monkeypatch):
    _root, run_dir, _wf_id = _case7_fixture(tmp_path, monkeypatch)

    run = reader.load_run(run_dir)
    assert _lifecycle_field(run, "terminal") == "completed"
    assert _lifecycle_field(run, "generations") == 2


def test_field_resumed_run_unfinished(tmp_path, monkeypatch, capsys):
    root, run_dir, wf_id = _case7_fixture(tmp_path, monkeypatch)

    cli.main(["run", str(run_dir)])
    out = capsys.readouterr().out
    header = out.splitlines()[0]
    assert "(unfinished)" not in header

    code = cli.main(["scan", "--root", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code in (0, 3)
    row = next(r for r in payload["rows"] if r["run_id"] == wf_id)
    assert row["unfinished_state"] is None


# =============================================================================
# 8. field_resume_after_failure_possibly_running (model N2, public N2)
# =============================================================================


def _case8_fixture(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f08"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(8)
    base = datetime.now(timezone.utc) - timedelta(minutes=2)

    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": agent_id, "label": "impl", "phase": "Implement"},
    ])
    # Fresh mtimes: files written now, never backdated.
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do the thing.", base, agent_id),
        _assistant_text("Working on it.", base + timedelta(seconds=5), agent_id),
    ])

    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", base - timedelta(minutes=5)),
        _workflow_launch_result("toolu_A", base - timedelta(minutes=5, seconds=-1), wf_id, "tA"),
        _notification_entry(
            base - timedelta(minutes=3), _envelope("tA", "toolu_A", "failed"),
        ),
        _workflow_launch("toolu_B", base - timedelta(minutes=1), resume_from=wf_id),
        _workflow_launch_result("toolu_B", base - timedelta(minutes=1, seconds=-1), wf_id, "tB"),
        # No notification yet for tB: the resume is still running.
    ])
    _no_config(monkeypatch, tmp_path)
    return run_dir


def test_field_resume_after_failure_possibly_running_model(tmp_path, monkeypatch):
    run_dir = _case8_fixture(tmp_path, monkeypatch)

    run = reader.load_run(run_dir)
    assert _lifecycle_field(run, "terminal") == "none"
    assert _lifecycle_field(run, "activity") == "recent"
    assert _lifecycle_field(run, "generations") == 2


def test_field_resume_after_failure_possibly_running(tmp_path, monkeypatch, capsys):
    run_dir = _case8_fixture(tmp_path, monkeypatch)

    run = reader.load_run(run_dir)
    findings = detectors.run_detectors(run)
    assert not any(f.detector == "workflow_failed" for f in findings)

    cli.main(["run", str(run_dir)])
    out = capsys.readouterr().out
    assert "possibly running" in out


# =============================================================================
# 9. field_queue_only_notification (model N1, public N2)
# =============================================================================


def _case9_fixture(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f09"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(9)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _simple_completed_journal(run_dir, agent_id)
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do the thing.", base, agent_id),
        _assistant_text("Done.", base + timedelta(seconds=30), agent_id),
    ])

    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", base),
        _workflow_launch_result("toolu_A", base + timedelta(seconds=1), wf_id, "tA"),
        _queue_notification_entry(
            base + timedelta(minutes=2), _envelope("tA", "toolu_A", "failed"),
        ),
    ])
    _no_config(monkeypatch, tmp_path)
    return run_dir, parent


# N1 (implemented): a queue-operation-only notification must be paired into
# the parent index's own generations like a delivered one, directly, with
# no workflow_failed involved.
def test_field_queue_only_notification_model(tmp_path, monkeypatch):
    _run_dir, parent = _case9_fixture(tmp_path, monkeypatch)

    parent_index = reader.ParentIndex.for_path(parent)
    generations = getattr(parent_index, "generations", None)
    assert (
        generations is not None
        and len(generations) == 1
        and getattr(generations[0], "terminal", None) == "failed"
    )


def test_field_queue_only_notification(tmp_path, monkeypatch, capsys):
    run_dir, parent = _case9_fixture(tmp_path, monkeypatch)

    run = reader.load_run(run_dir)
    assert _lifecycle_field(run, "terminal") == "failed"
    findings = detectors.run_detectors(run)
    hits = [f for f in findings if f.detector == "workflow_failed"]
    assert len(hits) == 1
    assert any(ev.startswith(f"{parent.name}:3") for ev in hits[0].evidence)

    cli.main(["run", str(run_dir)])
    out = capsys.readouterr().out
    header = out.splitlines()[0]
    assert "CLEAN" not in header


# =============================================================================
# 10. field_quoted_notification_ignored (N1) -- NOT xfail: already passes
# today (nothing reads any notification form yet, quoted or delivered), and
# pins the behavior N1 must keep (D19): a quoted copy inside prose or a
# compaction summary must never be mistaken for a real, accepted
# notification.
# =============================================================================


def _case10_fixture(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f0a"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(10)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _simple_completed_journal(run_dir, agent_id)
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do the thing.", base, agent_id),
        _assistant_text("Done.", base + timedelta(seconds=30), agent_id),
    ])

    envelope_text = _envelope("tA", "toolu_A", "failed")
    compaction_summary = (
        "This session is being continued from a previous conversation that ran "
        f"out of context. Earlier the session saw:\n{envelope_text}\nContinuing now."
    )
    assistant_quote = f"I previously saw this notification:\n{envelope_text}"

    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", base),
        _workflow_launch_result("toolu_A", base + timedelta(seconds=1), wf_id, "tA"),
        # The envelope appears only quoted, never as its own delivered or
        # queue-operation entry.
        _user(compaction_summary, base + timedelta(minutes=1)),
        _assistant_text(assistant_quote, base + timedelta(minutes=1, seconds=5)),
    ])
    _no_config(monkeypatch, tmp_path)
    return run_dir, parent


def test_field_quoted_notification_ignored(tmp_path, monkeypatch, capsys):
    run_dir, _parent = _case10_fixture(tmp_path, monkeypatch)

    cli.main(["run", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    # N3b P15: line 1 is the raw disclosure label; the run header is line 2.
    header = out.splitlines()[1]
    assert "CLEAN" in header

    run = reader.load_run(run_dir)
    findings = detectors.run_detectors(run)
    assert not any(f.detector == "workflow_failed" for f in findings)
    assert report.verdict_word(findings, run.status) == "CLEAN"


# N1 (implemented): the model-level counterpart of the CLI-level test above.
# Review finding (.reviews/v0.3/N1-review.md #1): the CLI-level assertions
# alone are vacuous here -- workflow_failed does not exist until N2, and
# nothing in N1's own pipeline reads Generation.terminal to produce a
# CLI-visible effect, so they would hold identically whether or not the
# quoted envelope was (incorrectly) accepted. This test inspects
# ParentIndex.generations directly, the way test_field_queue_only_notification_model
# does for the delivered case, so a future loosening of _parse_envelope's
# whole-string startswith/endswith check (e.g. to a substring test) would
# actually fail a test instead of passing the full suite unnoticed.
def test_field_quoted_notification_ignored_model(tmp_path, monkeypatch):
    _run_dir, parent = _case10_fixture(tmp_path, monkeypatch)

    parent_index = reader.ParentIndex.for_path(parent)
    generations = getattr(parent_index, "generations", None)
    assert generations is not None and len(generations) == 1
    assert generations[0].terminal is None
    assert generations[0].terminal_source is None


# =============================================================================
# 11. field_conflicting_notifications (model N2, public N2)
# =============================================================================


def _case11_fixture(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f0b"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(11)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _simple_completed_journal(run_dir, agent_id)
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do the thing.", base, agent_id),
        _assistant_text("Done.", base + timedelta(seconds=30), agent_id),
    ])

    parent = _parent_path(root)
    _write_jsonl(parent, [
        _workflow_launch("toolu_A", base),
        _workflow_launch_result("toolu_A", base + timedelta(seconds=1), wf_id, "tA"),
        _notification_entry(base + timedelta(minutes=1), _envelope("tA", "toolu_A", "completed")),
        _notification_entry(base + timedelta(minutes=2), _envelope("tA", "toolu_A", "failed")),
    ])
    _no_config(monkeypatch, tmp_path)
    return run_dir


def test_field_conflicting_notifications_model(tmp_path, monkeypatch):
    run_dir = _case11_fixture(tmp_path, monkeypatch)

    run = reader.load_run(run_dir)
    assert _lifecycle_field(run, "terminal") == "unknown"
    assert run.status == "INCOMPLETE"


def test_field_conflicting_notifications(tmp_path, monkeypatch, capsys):
    run_dir = _case11_fixture(tmp_path, monkeypatch)

    cli.main(["run", str(run_dir), "--json", "--no-redact"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "INCOMPLETE"


# =============================================================================
# 12. field_parent_same_size_rewrite (N1 only, D17: accepted generations and
# cache invalidation directly, never through workflow_failed)
# =============================================================================


# N1 (implemented): the parent index is fingerprinted (size, inode, prefix
# digest), not cached by path alone, so a same-size rewrite is picked up as
# a new set of generations.
def test_field_parent_same_size_rewrite(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f0c"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(12)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _simple_completed_journal(run_dir, agent_id)
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do the thing.", base, agent_id),
        _assistant_text("Done.", base + timedelta(seconds=30), agent_id),
    ])

    parent = _parent_path(root)

    def _parent_lines(status: str, pad: int) -> list[dict]:
        return [
            _workflow_launch("toolu_A", base),
            _workflow_launch_result("toolu_A", base + timedelta(seconds=1), wf_id, "tA"),
            _notification_entry(
                base + timedelta(minutes=2), _envelope("tA", "toolu_A", status, pad=pad),
            ),
        ]

    failed_envelope_len = len(_envelope("tA", "toolu_A", "failed"))
    completed_envelope_len = len(_envelope("tA", "toolu_A", "completed"))
    pad = max(0, completed_envelope_len - failed_envelope_len)

    _write_jsonl(parent, _parent_lines("failed", pad))
    stat1 = parent.stat()
    original_mtime = stat1.st_mtime
    original_atime = stat1.st_atime
    size1 = stat1.st_size

    gens1 = getattr(reader.ParentIndex.for_path(parent), "generations", None)

    _write_jsonl(parent, _parent_lines("completed", 0))
    size2 = parent.stat().st_size
    assert size2 == size1, "fixture bug: rewrite must keep the same byte size"
    os.utime(parent, (original_atime, original_mtime))

    gens2 = getattr(reader.ParentIndex.for_path(parent), "generations", None)

    assert (
        gens1 is not None and len(gens1) == 1
        and getattr(gens1[0], "terminal", None) == "failed"
    )
    assert (
        gens2 is not None and len(gens2) == 1
        and getattr(gens2[0], "terminal", None) == "completed"
    )


# =============================================================================
# 13. field_readonly_codex_denial (public N5 only: no new model field, the
# finding shape is unchanged -- this pins the severity-table rule itself)
# =============================================================================


def test_field_readonly_codex_denial(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f0d"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(13)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _simple_completed_journal(run_dir, agent_id)
    events = [
        _user("Have codex apply the patch.", base, agent_id),
        _assistant_tool(
            "t1", "Bash",
            {"command": "node /opt/codex/codex-companion.mjs task do-the-thing"},
            base + timedelta(seconds=1), agent_id,
        ),
        _tool_result(
            "t1",
            "[codex] WRITE_DENIED: patch rejected: writing outside of the project; "
            "rejected by user approval settings",
            base + timedelta(seconds=2), agent_id,
        ),
        _assistant_text("The sandbox denied the write.", base + timedelta(seconds=3), agent_id),
    ]
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", events)
    _no_config(monkeypatch, tmp_path)

    exit_code = cli.main(["run", str(run_dir), "--json", "--no-redact"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == 3
    codex_findings = [f for f in payload["findings"] if f["detector"] == "codex_write_denied"]
    assert len(codex_findings) == 1
    assert codex_findings[0]["severity"] == "minor"
    assert codex_findings[0]["rule_ids"] == ["R-CODEX-CONTRACT", "R-ASSERT-ARTIFACT"]
    assert exit_code == 0


# =============================================================================
# 14. field_session_brief_other_agents (public N5 only: rebuilt on disk,
# through cli.main's rescue plan, per finding 15/D14)
# =============================================================================


def test_field_session_brief_other_agents(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    session_path = root / FAKE_SLUG / f"{FAKE_UUID}.jsonl"
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _write_jsonl(session_path, [
        {
            "type": "user", "message": {"role": "user", "content": "start the work"},
            "sessionId": FAKE_UUID, "timestamp": _iso(base),
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "on it"}],
                        "usage": {}},
            "sessionId": FAKE_UUID, "timestamp": _iso(base + timedelta(seconds=1)),
        },
    ])

    agent_x = _aid(14)
    agent_y = _aid(15)
    path_x = "/tmp/agent-postmortem-fixture/sentinel-x.py"
    path_y = "/tmp/agent-postmortem-fixture/sentinel-y.py"
    subagents_dir = root / FAKE_SLUG / FAKE_UUID / "subagents"

    # X: a completed write, then killed (so it still qualifies for rescue).
    _write_jsonl(subagents_dir / f"agent-{agent_x}.jsonl", [
        _user("Write x.", base, agent_x),
        _assistant_tool("tx", "Write", {"file_path": path_x}, base + timedelta(seconds=1), agent_x),
        _tool_result("tx", "", base + timedelta(seconds=2), agent_x),
        _kill(base + timedelta(seconds=3), agent_x),
    ])
    # Y: an in-flight (never-resolved) write to a DIFFERENT path, then killed.
    _write_jsonl(subagents_dir / f"agent-{agent_y}.jsonl", [
        _user("Write y.", base, agent_y),
        _assistant_tool("ty", "Write", {"file_path": path_y}, base + timedelta(seconds=1), agent_y),
        _kill(base + timedelta(seconds=2), agent_y),
    ])
    _no_config(monkeypatch, tmp_path)

    # --raw does not exist as a CLI flag until N3 (docs/DESIGN.md 4.13.5); a
    # SystemExit from argparse today is folded into an empty payload so the
    # test's own first assertion (schema 3) is what actually fails, not an
    # uncaught SystemExit.
    try:
        cli.main(["rescue", "plan", str(session_path), "--json", "--raw"])
    except SystemExit:
        payload: dict = {}
    else:
        payload = json.loads(capsys.readouterr().out)

    assert payload.get("schema") == 3
    units = payload["units"]
    # An unnamed session subagent has no label, so the unit's `agent` field cannot tell the
    # two apart (it is the label's alias); each unit is found by the transcript its task
    # pointer names, which carries the agent id.
    unit_x = next(u for u in units if u["task"]["pointer"].startswith(f"agent-{agent_x}.jsonl:"))
    unit_y = next(u for u in units if u["task"]["pointer"].startswith(f"agent-{agent_y}.jsonl:"))
    paths_x = {row["path"] for row in unit_x["paths"]}
    paths_y = {row["path"] for row in unit_y["paths"]}

    assert path_x in paths_x
    assert path_y not in paths_x
    assert path_y in paths_y
    assert path_x not in paths_y


# =============================================================================
# 15. field_live_session_wording (public N2 only: a rendering choice, no new
# model field -- Run.lifecycle is null for a session, D11)
# =============================================================================


def test_field_live_session_wording(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    session_path = root / FAKE_SLUG / f"{FAKE_UUID}.jsonl"
    now = datetime.now(timezone.utc)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(
        json.dumps({
            "type": "user", "message": {"role": "user", "content": "hi"},
            "sessionId": FAKE_UUID, "timestamp": _iso(now),
        }) + "\n",
        encoding="utf-8",
    )
    _no_config(monkeypatch, tmp_path)

    cli.main(["run", str(session_path), "--no-redact"])
    out = capsys.readouterr().out
    # N3b P15: line 1 is the raw disclosure label; the run header is line 2.
    header = out.splitlines()[1]
    assert "ended" not in header
    assert "last activity" in header


# =============================================================================
# 16. field_unknown_mcp_tool_partial (model N4, public N5)
# =============================================================================


def _case16_fixture(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f10"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(16)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": agent_id, "label": "impl", "phase": "Implement"},
    ])
    events = [
        _user("Write the file through the fs tool.", base, agent_id),
        _assistant_tool(
            "t1", "mcp__fs__write_file", {"path": "/tmp/agent-postmortem-fixture/out.txt"},
            base + timedelta(seconds=1), agent_id,
        ),
        _tool_result("t1", "ok", base + timedelta(seconds=2), agent_id),
        _kill(base + timedelta(seconds=3), agent_id),
    ]
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", events)
    _no_config(monkeypatch, tmp_path)
    return run_dir


def test_field_unknown_mcp_tool_partial_model(tmp_path, monkeypatch):
    run_dir = _case16_fixture(tmp_path, monkeypatch)

    run = reader.load_run(run_dir)
    attempt = run.attempts[0]
    entries = _ledger_entries(attempt)
    assert entries is not None and len(entries) == 1
    assert getattr(entries[0], "effect", None) == "possible"
    assert getattr(entries[0], "basis", None) == "unknown_tool"
    ledger = _ledger(attempt)
    assert getattr(ledger, "completeness", None) == "partial"
    assert "possible_unknown_tool" in (getattr(ledger, "reasons", None) or [])


def test_field_unknown_mcp_tool_partial(tmp_path, monkeypatch, capsys):
    run_dir = _case16_fixture(tmp_path, monkeypatch)

    cli.main(["run", str(run_dir), "--json", "--no-redact"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == 3
    ledger = payload["run"]["attempts"][0]["ledger"]
    assert ledger["completeness"] == "partial"
    assert len(ledger["entries"]) == 1
    assert ledger["entries"][0]["basis"] == "unknown_tool"

    cli.main(["run", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    work_at_risk_line = next(line for line in out.splitlines() if line.startswith("WORK AT RISK"))
    assert "none" not in work_at_risk_line
