"""Tests for unit M3b: `scan` becomes a triage view by default.

docs/DESIGN.md 4.7/4.8 and .reviews/proposals/2026-09-v0.2-guided-rescue.md
section 6.2. Every fixture root here is built fresh under tmp_path (never
tests/fixtures/, which the tracked-file privacy sweep in test_privacy.py
audits): a workflow directory under
<root>/<slug>/<uuid>/subagents/workflows/<wf_id>/, or a session .jsonl at
<root>/<slug>/<uuid>.jsonl, using the one fake session id every fixture in
this repository shares (docs/DESIGN.md 5.4) and wf_ ids already listed in
tests/synthetic-ids.txt (reused here, not newly minted, so this file needs
no addition to that list).

report._now is monkeypatched to a value captured once per test (real wall
clock at test start, not an arbitrary date) so that a freshly written
fixture file's real mtime and the "now" scan triage compares it against
agree on what "recent" means; file mtimes are then backdated with os.utime
only where a test needs a fixture to read as old.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_postmortem import reader, report
from agent_postmortem.cli import main
from agent_postmortem.models import Attempt, AttemptFacts, Finding, Run

FAKE_UUID = "00000000-0000-4000-8000-000000000000"
CWD = "/home/user/project"
KILL_SENTINEL = "[Request interrupted by user]"


# --- fixture-building helpers ------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aid(n: int) -> str:
    """A conforming agent id (reader._AGENT_ID_RE: 'a' + 16 hex chars) -- a
    non-conforming one would force Run.status INCOMPLETE via a reader
    diagnostic and break every verdict assertion below."""
    return "a" + format(n, "016x")


def _wf_dir(root: Path, slug: str, wf_id: str) -> Path:
    return root / slug / FAKE_UUID / "subagents" / "workflows" / wf_id


def _write_journal(wf_dir: Path, entries: list[dict]) -> None:
    wf_dir.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(e) for e in entries) + "\n"
    (wf_dir / "journal.jsonl").write_text(text, encoding="utf-8")


def _write_meta(wf_dir: Path, agent_id: str, description: str) -> None:
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / f"agent-{agent_id}.meta.json").write_text(
        json.dumps({
            "agentType": "general-purpose",
            "description": description,
            "workflowPhase": "Implement",
            "spawnDepth": "1",
            "requestShape": "single",
            "requestNonInteractive": "true",
        }),
        encoding="utf-8",
    )


def _write_transcript(wf_dir: Path, agent_id: str, events: list[dict]) -> None:
    wf_dir.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(e) for e in events) + "\n"
    (wf_dir / f"agent-{agent_id}.jsonl").write_text(text, encoding="utf-8")


def _user(agent_id: str, ts: datetime, content) -> dict:
    return {
        "type": "user", "message": {"role": "user", "content": content},
        "agentId": agent_id, "sessionId": FAKE_UUID, "version": "2.1.267",
        "cwd": CWD, "timestamp": _iso(ts),
    }


def _assistant_text(agent_id: str, ts: datetime, text: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant", "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 5, "output_tokens": 5},
        },
        "agentId": agent_id, "sessionId": FAKE_UUID, "version": "2.1.267",
        "cwd": CWD, "timestamp": _iso(ts),
    }


def _assistant_tool_use(agent_id: str, ts: datetime, tool_id: str, name: str, tool_input: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
            "usage": {"input_tokens": 5, "output_tokens": 5},
        },
        "agentId": agent_id, "sessionId": FAKE_UUID, "version": "2.1.267",
        "cwd": CWD, "timestamp": _iso(ts),
    }


def _tool_result(agent_id: str, ts: datetime, tool_id: str, content: str, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}
            ],
        },
        "agentId": agent_id, "sessionId": FAKE_UUID, "version": "2.1.267",
        "cwd": CWD, "timestamp": _iso(ts),
    }


def _write_session(root: Path, slug: str, ts: datetime) -> Path:
    slug_dir = root / slug
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{FAKE_UUID}.jsonl"
    path.write_text(
        json.dumps({
            "type": "user", "message": {"role": "user", "content": "hi"},
            "sessionId": FAKE_UUID, "timestamp": _iso(ts),
        }) + "\n",
        encoding="utf-8",
    )
    return path


def _backdate(wf_dir: Path, when: datetime) -> None:
    ts = when.timestamp()
    for path in wf_dir.iterdir():
        if path.is_file():
            os.utime(path, (ts, ts))


def _fail_via_parent(root: Path, slug: str, wf_id: str, when: datetime) -> Path:
    """N5b (4.6, 4.13.4): FAILED is the run's own lifecycle terminal, no
    detector is critical any more. Write the parent transcript of the run
    `_wf_dir(root, slug, wf_id)` with a Workflow launch whose accepted
    notification says `failed`, and backdate it to `when` so the session it
    makes never reads "possibly live". The parent is itself a (CLEAN,
    zero-subagent) session run to scan."""
    parent = root / slug / f"{FAKE_UUID}.jsonl"
    parent.parent.mkdir(parents=True, exist_ok=True)
    envelope = (
        "<task-notification>\n<task-id>t1</task-id>\n"
        "<tool-use-id>toolu_f</tool-use-id>\n<status>failed</status>\n"
        "</task-notification>"
    )
    lines = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant", "usage": {},
                "content": [{"type": "tool_use", "id": "toolu_f", "name": "Workflow", "input": {}}],
            },
            "timestamp": _iso(when - timedelta(hours=2)),
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_f", "content": "launched"}],
            },
            "toolUseResult": {"runId": wf_id, "taskId": "t1", "status": "async_launched"},
            "timestamp": _iso(when - timedelta(hours=2) + timedelta(seconds=1)),
        },
        {
            "type": "user",
            "message": {"role": "user", "content": envelope},
            "origin": {"kind": "task-notification"},
            "timestamp": _iso(when),
        },
    ]
    parent.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    os.utime(parent, (when.timestamp(), when.timestamp()))
    return parent


def _freeze_now(monkeypatch) -> datetime:
    fixed = datetime.now(timezone.utc)
    monkeypatch.setattr(report, "_now", lambda: fixed)
    return fixed


# --- direct unit tests: report.py's pure scan-triage functions --------------
# (no filesystem at all -- a hand-built Run/Attempt/AttemptFacts is enough to
# pin these deterministic, pure functions precisely.)


def _facts(paths_written=(), commits=()) -> AttemptFacts:
    return AttemptFacts(
        paths_written=list(paths_written), in_flight=[], commits=list(commits),
        cwds=[], background_unfinished=0, last_tool=None,
        seconds_since_last_tool_at_end=None,
    )


def _attempt(agent_id, ordinal, outcome, facts=None) -> Attempt:
    return Attempt(
        agent_id=agent_id, key="v2:x", label="impl:x", phase="Implement",
        agent_type=None, meta={}, transcript_path=None, events=[],
        first_ts=None, last_ts=None, ordinal=ordinal, outcome=outcome, usage={},
        facts=facts,
    )


def _run(run_id, attempts, ended_at, finished=True) -> Run:
    return Run(
        run_id=run_id, kind="workflow", path=Path("/tmp/does-not-exist"),
        project_slug="-home-user-x", session_id=None, claude_version=None,
        journal=[], agents=[], attempts=attempts, started_at=None,
        ended_at=ended_at, diagnostics=[], finished=finished,
    )


def test_attention_next_command_uses_observe_only_when_paths_and_recent():
    now = datetime.now(timezone.utc)
    killed = _attempt(_aid(1), 1, "killed", _facts(paths_written=["notes.md"]))

    recent_run = _run("wf_recent", [killed], ended_at=now - timedelta(hours=1))
    assert report.attention_next_command(recent_run, now) == (
        f"agent-postmortem run {recent_run.run_id} --observe"
    )

    stale_run = _run("wf_stale", [killed], ended_at=now - timedelta(hours=100))
    assert report.attention_next_command(stale_run, now) == (
        f"agent-postmortem run {stale_run.run_id}"
    )

    no_paths_run = _run("wf_nopaths", [_attempt(_aid(2), 1, "killed", _facts())], ended_at=now)
    assert report.attention_next_command(no_paths_run, now) == (
        f"agent-postmortem run {no_paths_run.run_id}"
    )


def test_is_possibly_live_requires_unfinished_workflow_and_recent_mtime():
    now = datetime.now(timezone.utc)
    unfinished = _run("wf_a", [], ended_at=None, finished=False)
    finished = _run("wf_b", [], ended_at=None, finished=True)

    assert report.is_possibly_live(unfinished, now.timestamp() - 10, now, 600) is True
    assert report.is_possibly_live(unfinished, now.timestamp() - 900, now, 600) is False
    assert report.is_possibly_live(unfinished, None, now, 600) is False
    assert report.is_possibly_live(finished, now.timestamp() - 1, now, 600) is False


def _session_run(run_id, ended_at=None) -> Run:
    return Run(
        run_id=run_id, kind="session", path=Path("/tmp/does-not-exist"),
        project_slug="-home-user-x", session_id=None, claude_version=None,
        journal=[], agents=[], attempts=[], started_at=None,
        ended_at=ended_at, diagnostics=[], finished=None,
    )


def test_is_possibly_live_also_applies_to_a_session():
    """docs/DESIGN.md 4.13.3's Sessions paragraph: 'possibly running' is
    unit M3b's own hook-free live heuristic, the same one a workflow gets --
    never unreachable for a session. A session's Run.finished is always
    None (D11), never anything is_possibly_live's own `run.finished` check
    would reject, so only the `kind == 'workflow'` short-circuit (this
    finding) ever stood in the way."""
    now = datetime.now(timezone.utc)
    session = _session_run("s_abcdef0123456789")

    assert report.is_possibly_live(session, now.timestamp() - 10, now, 600) is True
    assert report.is_possibly_live(session, now.timestamp() - 900, now, 600) is False
    assert report.is_possibly_live(session, None, now, 600) is False


def test_scan_age_string_forms():
    now = datetime.now(timezone.utc)
    run_recent = _run("wf_a", [], ended_at=now - timedelta(minutes=8))
    run_hours = _run("wf_b", [], ended_at=now - timedelta(hours=2))
    run_days = _run("wf_c", [], ended_at=now - timedelta(days=3))
    run_unknown = _run("wf_d", [], ended_at=None)

    assert report.scan_age_string(run_recent, now, live=False) == "8m ago"
    assert report.scan_age_string(run_hours, now, live=False) == "2h ago"
    assert report.scan_age_string(run_days, now, live=False) == "3d ago"
    assert report.scan_age_string(run_unknown, now, live=False) == "unknown"
    assert report.scan_age_string(run_hours, now, live=True) == "possibly live"


# --- end-to-end fixtures: ranking, reason lines, totals, --all, JSON --------


def test_ranking_order_live_then_paths_then_verdict_then_no_paths(tmp_path, monkeypatch, capsys):
    """docs unit M3b rule 1's ranking key, proven in one pass: a possibly-live
    unfinished run ranks first; among ended runs a killed/failed attempt's
    written paths outrank verdict; among equal paths FAILED beats DEGRADED
    beats CLEAN; a DEGRADED run with no paths at all ranks last of the five
    (paths outrank verdict, so 0 paths loses to every 1-path row above it,
    whatever its own verdict)."""
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"

    # e01: possibly live -- a single 'started' key, never closed, files left
    # at their natural (fresh) mtime.
    live_dir = _wf_dir(root, "-home-user-scan-e01", "wf_00000000-710")
    t0 = now - timedelta(minutes=2)
    _write_journal(live_dir, [
        {"type": "started", "timestamp": _iso(t0), "key": "v2:e01",
         "agentId": _aid(1), "label": "impl:e01", "phase": "Implement"},
    ])
    _write_meta(live_dir, _aid(1), "Implement the e01 module.")
    _write_transcript(live_dir, _aid(1), [_user(_aid(1), t0, "Implement the e01 module.")])

    def _retried_pair(slug, wf_id, key, with_path, with_denial=False,
                      age=timedelta(hours=1, minutes=10)):
        wf_dir = _wf_dir(root, slug, wf_id)
        wf_dir.mkdir(parents=True, exist_ok=True)
        a1, a2 = _aid(101), _aid(102)
        s0 = now - age
        events1 = [_user(a1, s0, f"Implement {key}.")]
        if with_path:
            events1.append(_assistant_tool_use(
                a1, s0 + timedelta(seconds=5), "toolu_1", "Edit",
                {"file_path": f"/tmp/agent-postmortem-scantriage/{key}/file.md",
                 "old_string": "a", "new_string": "b"},
            ))
            events1.append(_tool_result(a1, s0 + timedelta(seconds=6), "toolu_1", "Edit applied"))
        else:
            events1.append(_assistant_text(a1, s0 + timedelta(seconds=5), "Working on it."))
        events1.append(_user(a1, s0 + timedelta(seconds=10), KILL_SENTINEL))
        _write_transcript(wf_dir, a1, events1)
        _write_meta(wf_dir, a1, f"Implement {key}.")

        s1 = s0 + timedelta(minutes=1)
        done_text = "Done."
        events2 = [_user(a2, s1, f"Retry: implement {key}.")]
        # N5b: `with_denial` no longer means a codex denial (that is a minor
        # finding now); it means the harness reported the run failed, which
        # is what makes a verdict FAILED (see _fail_via_parent below).
        events2.append(_assistant_text(a2, s1 + timedelta(seconds=20), done_text))
        _write_transcript(wf_dir, a2, events2)
        _write_meta(wf_dir, a2, f"Implement {key}.")

        ended = s1 + timedelta(seconds=20)
        _write_journal(wf_dir, [
            {"type": "started", "timestamp": _iso(s0), "key": key, "agentId": a1,
             "label": f"impl:{key}", "phase": "Implement"},
            {"type": "started", "timestamp": _iso(s1), "key": key, "agentId": a2,
             "label": f"impl:{key}", "phase": "Implement"},
            {"type": "result", "timestamp": _iso(ended), "key": key, "agentId": a2,
             "result": {"ok": True}},
        ])
        _backdate(wf_dir, ended)
        if with_denial:
            _fail_via_parent(root, slug, wf_id, ended)
        return wf_dir

    # e04: FAILED, 1 path (retried, and the harness reported the run failed).
    _retried_pair("-home-user-scan-e04", "wf_00000000-740", "v2:e04",
                  with_path=True, with_denial=True)
    # e02: DEGRADED, 1 path (retried, no denial).
    _retried_pair("-home-user-scan-e02", "wf_00000000-720", "v2:e02", with_path=True)
    # e03: DEGRADED, 0 paths (retried, killed attempt wrote nothing).
    _retried_pair("-home-user-scan-e03", "wf_00000000-730", "v2:e03", with_path=False)
    # e06: FAILED, 1 path, but 20 days old: recency outranks paths and
    # verdict, so it lands below every run that ended within 72 hours.
    _retried_pair("-home-user-scan-e06", "wf_00000000-760", "v2:e06",
                  with_path=True, with_denial=True, age=timedelta(days=20))

    # e05: CLEAN, 1 path -- a single occurrence (never retried) killed
    # shortly after its last real event (short silence -> cause unattributed,
    # info severity, so nothing pushes the verdict past CLEAN), with an Edit
    # completed before the kill.
    e05_dir = _wf_dir(root, "-home-user-scan-e05", "wf_00000000-750")
    a5 = _aid(105)
    u0 = now - timedelta(hours=1, minutes=5)
    _write_journal(e05_dir, [
        {"type": "started", "timestamp": _iso(u0), "key": "v2:e05", "agentId": a5,
         "label": "impl:e05", "phase": "Implement"},
    ])
    _write_meta(e05_dir, a5, "Implement e05.")
    _write_transcript(e05_dir, a5, [
        _user(a5, u0, "Implement e05."),
        _assistant_tool_use(a5, u0 + timedelta(seconds=5), "toolu_5", "Edit",
                             {"file_path": "/tmp/agent-postmortem-scantriage/e05/file.md",
                              "old_string": "a", "new_string": "b"}),
        _tool_result(a5, u0 + timedelta(seconds=6), "toolu_5", "Edit applied"),
        _assistant_text(a5, u0 + timedelta(seconds=8), "still going"),
        _user(a5, u0 + timedelta(seconds=12), KILL_SENTINEL),
    ])
    _backdate(e05_dir, u0 + timedelta(seconds=12))

    code = main(["scan", "--root", str(root)])
    captured = capsys.readouterr()
    assert code == 3, captured.err  # e04's verdict is FAILED

    run_ids_in_order = [
        line.strip().split("  ", 1)[0] for line in captured.out.splitlines()
        if line.strip().startswith("wf_")
    ]
    # FB2: e01 (possibly live, otherwise CLEAN) is no longer a NEEDS ATTENTION candidate; it
    # is the one line of the LIVE NOW group printed after the candidates.
    assert run_ids_in_order == [
        "wf_00000000-740",  # e04: FAILED, 1 path
        "wf_00000000-720",  # e02: DEGRADED, 1 path
        "wf_00000000-750",  # e05: CLEAN, 1 path
        "wf_00000000-730",  # e03: DEGRADED, 0 paths: paths outrank verdict
        "wf_00000000-760",  # e06: FAILED, 1 path, 20 days old: recency outranks both
        "wf_00000000-710",  # e01: possibly live, under LIVE NOW
    ]
    assert "LIVE NOW (1 of 8 runs)" in captured.out
    assert "possibly live" in captured.out
    assert "CLAUDE.md" not in captured.out
    assert "apply" not in captured.out


def test_clean_zero_subagent_session_is_never_a_candidate(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"

    # N1N2 review finding 3 fix: a session is now eligible for scan's
    # hook-free "possibly live" heuristic the same as a workflow, so this
    # session's own file must be backdated to actually be stale -- an hour
    # ago, matching the event timestamp above -- or it would read "possibly
    # live" from its own fresh mtime and defeat the "never a candidate" this
    # test means to pin.
    clean_session_path = _write_session(root, "-home-user-scan-clean-session", now - timedelta(hours=1))
    stale_ts = (now - timedelta(hours=1)).timestamp()
    os.utime(clean_session_path, (stale_ts, stale_ts))

    fail_dir = _wf_dir(root, "-home-user-scan-clean-wf", "wf_00000000-760")
    a1 = _aid(1)
    t0 = now - timedelta(hours=1)
    _write_journal(fail_dir, [
        {"type": "started", "timestamp": _iso(t0), "key": "v2:fail", "agentId": a1,
         "label": "impl:fail", "phase": "Implement"},
        {"type": "result", "timestamp": _iso(t0 + timedelta(seconds=10)), "key": "v2:fail",
         "agentId": a1, "result": {"ok": True}},
    ])
    _write_meta(fail_dir, a1, "Implement fail.")
    _write_transcript(fail_dir, a1, [
        _user(a1, t0, "Implement fail."),
        _assistant_text(a1, t0 + timedelta(seconds=5), "Done."),
    ])
    # N5b: FAILED comes from the harness reporting the run failed; the parent
    # transcript that says so is a second, stale, zero-subagent session.
    _fail_via_parent(root, "-home-user-scan-clean-wf", "wf_00000000-760", now - timedelta(hours=1))

    code = main(["scan", "--root", str(root)])
    captured = capsys.readouterr()
    assert code == 3, captured.err

    assert "NEEDS ATTENTION (1 of 3 runs)" in captured.out
    assert not any(line.strip().startswith("s_") for line in captured.out.splitlines())
    assert "2 session runs" in captured.out


def test_reason_line_for_usage_limit_names_cause_and_reset_time(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    wf_dir = _wf_dir(root, "-home-user-scan-usage-limit", "wf_00000000-751")
    a1 = _aid(1)
    t0 = now - timedelta(hours=1)
    _write_journal(wf_dir, [
        {"type": "started", "timestamp": _iso(t0), "key": "v2:usage", "agentId": a1,
         "label": "impl:usage", "phase": "Implement"},
        {"type": "failed", "timestamp": _iso(t0 + timedelta(minutes=5)), "key": "v2:usage",
         "agentId": a1},
    ])
    _write_meta(wf_dir, a1, "Implement usage.")
    _write_transcript(wf_dir, a1, [
        _user(a1, t0, "Implement usage."),
        _assistant_text(a1, t0 + timedelta(seconds=5), "Working on it."),
        _assistant_text(a1, t0 + timedelta(minutes=5), "You've hit your session limit · resets 2pm"),
    ])

    code = main(["scan", "--root", str(root)])
    captured = capsys.readouterr()
    assert code == 0, captured.err

    reason_line = next(
        line for line in captured.out.splitlines() if line.strip().startswith("wf_00000000-751")
    )
    assert "1 attempt failed: session usage limit reached, resets 2pm" in reason_line
    assert "CLAUDE.md" not in captured.out
    assert "apply" not in captured.out


def test_default_output_fits_25_lines_with_12_runs_and_limit_flag_caps_rows(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"

    failed_ids = [
        "wf_00000000-756", "wf_00000000-760", "wf_00000000-770", "wf_00000000-777",
        "wf_00000000-780", "wf_00000000-790", "wf_00000000-798", "wf_00000000-799",
        "wf_00000000-800", "wf_00000000-810",
    ]
    for index, wf_id in enumerate(failed_ids):
        slug = f"-home-user-scan-many-{index}"
        wf_dir = _wf_dir(root, slug, wf_id)
        agent_id = _aid(200 + index)
        t0 = now - timedelta(hours=1, minutes=index)
        _write_journal(wf_dir, [
            {"type": "started", "timestamp": _iso(t0), "key": "v2:many", "agentId": agent_id,
             "label": "impl:many", "phase": "Implement"},
            {"type": "result", "timestamp": _iso(t0 + timedelta(seconds=10)), "key": "v2:many",
             "agentId": agent_id, "result": {"ok": True}},
        ])
        _write_meta(wf_dir, agent_id, "Implement many.")
        _write_transcript(wf_dir, agent_id, [
            _user(agent_id, t0, "Implement many."),
            _assistant_text(agent_id, t0 + timedelta(seconds=5), "Done."),
        ])
        # N5b: FAILED comes from the harness reporting the run failed; each
        # parent transcript is a stale, zero-subagent session run.
        _fail_via_parent(root, slug, wf_id, t0)

    # N1N2 review finding 3 fix: backdate each session's own file so neither
    # reads "possibly live" from a fresh mtime (see the longer comment at
    # the similar fixture in test_clean_zero_subagent_session_is_never_a_
    # candidate above) -- these two must stay out of NEEDS ATTENTION for
    # this test's own "10 of 12" count to hold.
    clean_session_1 = _write_session(root, "-home-user-scan-many-clean-1", now - timedelta(hours=2))
    os.utime(clean_session_1, ((now - timedelta(hours=2)).timestamp(),) * 2)
    clean_session_2 = _write_session(root, "-home-user-scan-many-clean-2", now - timedelta(hours=3))
    os.utime(clean_session_2, ((now - timedelta(hours=3)).timestamp(),) * 2)

    code = main(["scan", "--root", str(root)])
    captured = capsys.readouterr()
    assert code == 3, captured.err
    lines = captured.out.splitlines()
    # N5b: 10 failed workflows, their 10 parent sessions, and 2 clean sessions.
    assert "NEEDS ATTENTION (10 of 22 runs)" in lines[0]
    assert len(lines) <= 25, f"default scan output was {len(lines)} lines:\n{captured.out}"

    code = main(["scan", "--root", str(root), "--limit", "3"])
    captured = capsys.readouterr()
    assert code == 3, captured.err
    lines = captured.out.splitlines()
    # FB2: a truncated list says so in its own header.
    assert lines[0] == "NEEDS ATTENTION (10 of 22 runs; showing 3, use --limit N or --all for more)"
    shown_ids = [
        line.strip().split("  ", 1)[0] for line in lines if line.strip().startswith("wf_")
    ]
    assert len(shown_ids) == 3


def test_all_flag_output_is_the_old_one_row_per_run_list(tmp_path, monkeypatch, capsys):
    """docs unit M3b rule 6: --all's text is exactly today's row-per-run
    list (render_scan_text/_scan_row_line/_scan_totals_lines, none of which
    this unit touches) -- this pins that shape so a future change to the
    triage path can never quietly bleed into --all."""
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"

    wf_dir = _wf_dir(root, "-home-user-scan-all-wf", "wf_00000000-820")
    a1 = _aid(1)
    t0 = now - timedelta(hours=1)
    _write_journal(wf_dir, [
        {"type": "started", "timestamp": _iso(t0), "key": "v2:all", "agentId": a1,
         "label": "impl:all", "phase": "Implement"},
        {"type": "result", "timestamp": _iso(t0 + timedelta(seconds=10)), "key": "v2:all",
         "agentId": a1, "result": {"ok": True}},
    ])
    _write_meta(wf_dir, a1, "Implement all.")
    _write_transcript(wf_dir, a1, [
        _user(a1, t0, "Implement all."),
        _assistant_text(a1, t0 + timedelta(seconds=5), "Done."),
    ])
    # N5b: FAILED comes from the harness reporting the run failed; the parent
    # transcript that says so is a second session run (project -wf).
    parent_path = _fail_via_parent(root, "-home-user-scan-all-wf", "wf_00000000-820", t0)
    session_path = _write_session(root, "-home-user-scan-all-session", t0 + timedelta(hours=1))

    code = main(["scan", "--root", str(root), "--all", "--no-redact"])
    captured = capsys.readouterr()
    assert code == 3, captured.err

    session_refs = {
        ref.path: ref for ref in reader.discover_runs(root, kinds=frozenset({"session"}))
    }
    session_ref = session_refs[session_path]
    parent_ref = session_refs[parent_path]

    workflow_date = t0.date().isoformat()
    session_date = (t0 + timedelta(hours=1)).date().isoformat()
    # Rows are ordered by the day, then by the exact start time. The rows are
    # built from the real clock, so they can fall on different dates; the
    # expected order follows the same rule instead of assuming
    # one date.
    # its one entry is at the frozen now and its file was just written, so the tool's own
    # liveness rule calls it possibly running, and the --all row says so (release review of 0.3.0)
    session_row = (
        f"{session_ref.run_id}  {session_date}  -home-user-scan-all-session  session  "
        f"0 agents  0 attempts  CLEAN possibly running  -\n"
    )
    workflow_row = (
        f"wf_00000000-820  {workflow_date}  -home-user-scan-all-wf  workflow  "
        f"1 agents  1 attempts  FAILED  workflow_failed\n"
    )
    # N5b: the workflow's parent transcript is a session run of its own; its
    # first event is two hours before the failure notification.
    parent_date = (t0 - timedelta(hours=2)).date().isoformat()
    parent_row = (
        f"{parent_ref.run_id}  {parent_date}  -home-user-scan-all-wf  session  "
        f"0 agents  0 attempts  CLEAN  -\n"
    )
    # release review of 0.3.0: rows are ordered by the day and then by the exact start time
    # (the parent session starts at t0 - 2h, the workflow at t0, the other session at t0 + 1h)
    ordered = sorted([
        (session_date, t0 + timedelta(hours=1), session_row), (workflow_date, t0, workflow_row),
        (parent_date, t0 - timedelta(hours=2), parent_row),
    ])
    expected = (
        "".join(row for _, _, row in ordered)
        + "\n"
        "1 workflow runs, 0 with retries, 1 attempts, 0.0h wasted\n"
        "2 session runs, 0 subagents, 0 interrupted\n"
        "findings per detector (one finding can cover several events):\n"
        "  wasted_time: 3\n"
        "  workflow_failed: 1\n"
    )
    # N3b P15: --no-redact is --raw, whose text starts with the disclosure label.
    assert captured.out == "disclosure: raw (real names, absolute paths; do not paste)\n" + expected


def test_json_rows_carry_the_new_keys_and_nothing_else(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    wf_dir = _wf_dir(root, "-home-user-scan-json", "wf_00000000-af0")
    a1 = _aid(1)
    t0 = now - timedelta(hours=1)
    _write_journal(wf_dir, [
        {"type": "started", "timestamp": _iso(t0), "key": "v2:json", "agentId": a1,
         "label": "impl:json", "phase": "Implement"},
        {"type": "result", "timestamp": _iso(t0 + timedelta(seconds=10)), "key": "v2:json",
         "agentId": a1, "result": {"ok": True}},
    ])
    _write_meta(wf_dir, a1, "Implement json.")
    _write_transcript(wf_dir, a1, [_user(a1, t0, "Implement json.")])

    code = main(["scan", "--root", str(root), "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)

    expected_keys = {
        "run_id", "kind", "date", "project", "agents", "attempts", "verdict",
        "top_detector", "finished", "attention", "attention_reason", "age",
        "possibly_live", "paths_at_risk", "paths_at_risk_complete", "unfinished_state",
    }
    for row in payload["rows"]:
        assert set(row.keys()) == expected_keys, row.keys()

    row = payload["rows"][0]
    assert row["attention"] is False
    assert row["attention_reason"] is None
    assert row["possibly_live"] is False
    assert row["paths_at_risk"] == 0
    assert row["unfinished_state"] is None
    assert row["age"].endswith("ago")


# --- unfinished_state: possibly live / failed / abandoned -------------------


def test_unfinished_state_failed_when_the_harness_reports_the_run_failed(
    tmp_path, monkeypatch, capsys,
):
    """docs/DESIGN.md 5.3b (v0.3 N2): unfinished_state's own 'failed' is now
    lifecycle-derived, never a per-key journal state -- a key whose own
    attempt ends in a journal 'failed' event no longer lands here at all
    once the run's own journal fully resolves (that per-key story belongs
    to attempt_failed); only an accepted parent-level failed terminal
    (notification/TaskStop/launch result) does."""
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    slug = "-home-user-scan-unf-failed"
    wf_id = "wf_00000000-752"
    wf_dir = _wf_dir(root, slug, wf_id)
    a1 = _aid(1)
    t0 = now - timedelta(hours=1)
    # The journal itself resolves cleanly: this failure is the harness's
    # own, not an attempt-level one.
    _write_journal(wf_dir, [
        {"type": "started", "timestamp": _iso(t0), "key": "v2:unf", "agentId": a1,
         "label": "impl:unf", "phase": "Implement"},
        {"type": "result", "timestamp": _iso(t0 + timedelta(minutes=1)), "key": "v2:unf",
         "agentId": a1, "result": {"ok": True}},
    ])
    _write_meta(wf_dir, a1, "Implement unf.")
    _write_transcript(wf_dir, a1, [_user(a1, t0, "Implement unf.")])

    parent_path = root / slug / f"{FAKE_UUID}.jsonl"
    parent_path.parent.mkdir(parents=True, exist_ok=True)
    envelope = (
        "<task-notification>\n<task-id>t1</task-id>\n"
        "<tool-use-id>toolu_1</tool-use-id>\n<status>failed</status>\n"
        "</task-notification>"
    )
    parent_lines = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "Workflow", "input": {}}],
                "usage": {},
            },
            "timestamp": _iso(t0),
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "launched"}],
            },
            "toolUseResult": {"runId": wf_id, "taskId": "t1", "status": "async_launched"},
            "timestamp": _iso(t0 + timedelta(seconds=1)),
        },
        {
            "type": "user",
            "message": {"role": "user", "content": envelope},
            "origin": {"kind": "task-notification"},
            "timestamp": _iso(t0 + timedelta(minutes=2)),
        },
    ]
    parent_path.write_text(
        "\n".join(json.dumps(line) for line in parent_lines) + "\n", encoding="utf-8",
    )

    code = main(["scan", "--root", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    # N5b (4.6): a failed lifecycle terminal makes the verdict FAILED, so the
    # scan exits 3 (it exited 0 while FAILED needed a critical finding).
    assert code == 3
    workflow_row = next(row for row in payload["rows"] if row["kind"] == "workflow")
    assert workflow_row["verdict"] == "FAILED"
    assert workflow_row["unfinished_state"] == "failed"


def test_unfinished_state_no_terminal_record_when_started_alone_and_old(tmp_path, monkeypatch, capsys):
    """docs/DESIGN.md 5.3b (v0.3 N2): 'abandoned' is removed. A workflow
    with no parent-level terminal evidence at all (zero generations) and an
    unresolved journal key is lifecycle terminal 'none'; once its files have
    gone quiet (activity 'stale'), that is 'no terminal record', never
    'abandoned'."""
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    wf_dir = _wf_dir(root, "-home-user-scan-unf-abandoned", "wf_00000000-753")
    a1 = _aid(1)
    t0 = now - timedelta(hours=2)
    # 'started' alone, no transcript at all (absent, not merely empty) --
    # DESIGN 4.2: an attempt with no transcript at all is outcome 'unknown',
    # never 'completed', so it is not-completed for unfinished_state.
    _write_journal(wf_dir, [
        {"type": "started", "timestamp": _iso(t0), "key": "v2:unf", "agentId": a1,
         "label": "impl:unf", "phase": "Implement"},
    ])
    _write_meta(wf_dir, a1, "Implement unf.")
    _backdate(wf_dir, now - timedelta(hours=2))

    code = main(["scan", "--root", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    row = payload["rows"][0]
    assert row["possibly_live"] is False
    assert row["unfinished_state"] == "no terminal record"


def test_unfinished_state_possibly_running_when_started_alone_and_fresh(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    wf_dir = _wf_dir(root, "-home-user-scan-unf-live", "wf_00000000-754")
    a1 = _aid(1)
    t0 = now - timedelta(minutes=1)
    _write_journal(wf_dir, [
        {"type": "started", "timestamp": _iso(t0), "key": "v2:unf", "agentId": a1,
         "label": "impl:unf", "phase": "Implement"},
    ])
    _write_meta(wf_dir, a1, "Implement unf.")
    # Files left at their natural fresh mtime -- no backdating.

    code = main(["scan", "--root", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    row = payload["rows"][0]
    assert row["possibly_live"] is True
    assert row["unfinished_state"] == "possibly running"


def test_unfinished_state_possibly_running_for_a_fresh_session(tmp_path, monkeypatch, capsys):
    """docs/DESIGN.md 4.13.3's Sessions paragraph / 5.3b: a session row's own
    unfinished_state reaches 'possibly running' through the same hook-free
    live heuristic a workflow gets."""
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _write_session(root, "-home-user-scan-session-fresh", now - timedelta(seconds=5))
    # File left at its natural fresh mtime -- no backdating.

    code = main(["scan", "--root", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    row = payload["rows"][0]
    assert row["kind"] == "session"
    assert row["possibly_live"] is True
    assert row["unfinished_state"] == "possibly running"


def test_unfinished_state_null_for_a_stale_session(tmp_path, monkeypatch, capsys):
    """The other half of the same rule: a session whose own file has gone
    quiet is not possibly live, and a session row's unfinished_state is
    otherwise always null (docs/DESIGN.md 5.3b: a session never derives 'no
    terminal record', this milestone claims less)."""
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    session_path = _write_session(root, "-home-user-scan-session-stale", now - timedelta(hours=2))
    stale_ts = (now - timedelta(hours=2)).timestamp()
    os.utime(session_path, (stale_ts, stale_ts))

    code = main(["scan", "--root", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    row = payload["rows"][0]
    assert row["kind"] == "session"
    assert row["possibly_live"] is False
    assert row["unfinished_state"] is None


# --- unit M3c: silencing a detector by config (docs/DESIGN.md 4.6/4.7/4.10) -


def _finding(detector: str, severity: str = "major") -> Finding:
    return Finding(
        detector=detector, severity=severity, title=f"{detector} fired",
        run_id="wf_pure", agent_id=None, evidence=[], metrics={}, rule_id=None,
        finding_id=f"{detector}-finding",
    )


def test_attention_reason_falls_back_to_the_next_top_detector_once_the_first_is_silenced():
    """Pure-function test of report.attention_reason/_scan_top_detector: with
    two major detectors tied on count, the fallback ("<detector> x<n>")
    clause names the first one (claim_contradiction sorts/inserts first);
    once that one is excluded (the same filtering config.partition_silenced
    performs), the reason names the other one instead and never the
    silenced id."""
    run = _run("wf_pure", [], ended_at=datetime.now(timezone.utc))
    findings = [_finding("claim_contradiction"), _finding("structured_output_failure")]
    assert report.attention_reason(run, findings) == "claim_contradiction x1"

    shown = [f for f in findings if f.detector != "claim_contradiction"]
    reason = report.attention_reason(run, shown)
    assert reason == "structured_output_failure x1"
    assert "claim_contradiction" not in reason


def test_verdict_and_attention_candidate_are_computed_over_shown_findings_only():
    now = datetime.now(timezone.utc)
    run = _run("wf_pure", [], ended_at=now)
    findings = [_finding("structured_output_failure")]
    assert report.verdict_word(findings, run.status) == "DEGRADED"
    assert report.is_attention_candidate(run, findings, now, live=False) is True

    shown: list[Finding] = []  # the only major detector was silenced away
    assert report.verdict_word(shown, run.status) == "CLEAN"
    assert report.is_attention_candidate(run, shown, now, live=False) is False


def test_scan_drops_a_run_from_attention_and_reports_the_silenced_count_as_its_last_line(
    tmp_path, monkeypatch, capsys,
):
    """End-to-end: a run whose only findings are two silenced major
    detectors (silent_retry + mislabeled_kill, from a silent retry) is
    DEGRADED and would otherwise be a NEEDS ATTENTION candidate; with both
    silenced by config it becomes CLEAN and is no longer a candidate at
    all -- so its id (and a fortiori any silenced detector's own id) can
    never appear in a reason line -- and the last line of scan's own output
    reports the silenced count, sorted."""
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    wf_dir = _wf_dir(root, "-home-user-scan-silence", "wf_00000000-8a0")
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
        _user(a1, t0, "Implement retry."),
        _assistant_text(a1, t0 + timedelta(seconds=5), "Starting."),
        _user(a1, t0 + timedelta(minutes=4), KILL_SENTINEL),
    ])
    _write_transcript(wf_dir, a2, [
        _user(a2, t0 + timedelta(minutes=5), "Implement retry."),
        _assistant_text(a2, t0 + timedelta(minutes=6), "Done."),
    ])

    # Sanity: without config, this run is DEGRADED and a candidate.
    code = main(["scan", "--root", str(root)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "wf_00000000-8a0" in captured.out
    assert "silenced by config" not in captured.out

    config_path = tmp_path / "config.toml"
    config_path.write_text('[detectors]\noff = ["silent_retry", "mislabeled_kill"]\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))

    code = main(["scan", "--root", str(root)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "wf_00000000-8a0" not in captured.out
    lines = captured.out.rstrip("\n").splitlines()
    assert lines[-1] == "2 detectors silenced by config: mislabeled_kill, silent_retry"


def test_scan_all_top_detector_column_excludes_a_silenced_detector(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    wf_dir = _wf_dir(root, "-home-user-scan-silence-all", "wf_00000000-8b0")
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
        _user(a1, t0, "Implement retry."),
        _assistant_text(a1, t0 + timedelta(seconds=5), "Starting."),
        _user(a1, t0 + timedelta(minutes=4), KILL_SENTINEL),
    ])
    _write_transcript(wf_dir, a2, [
        _user(a2, t0 + timedelta(minutes=5), "Implement retry."),
        _assistant_text(a2, t0 + timedelta(minutes=6), "Done."),
    ])

    config_path = tmp_path / "config.toml"
    # silent_retry is the top (major) detector here (docs/DESIGN.md 4.7's
    # M1d tie rule prefers a non-info one; mislabeled_kill is also major and
    # would become the new top once silent_retry alone is silenced).
    config_path.write_text('[detectors]\noff = ["silent_retry"]\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))

    code = main(["scan", "--root", str(root), "--all", "--no-redact"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    # N3b P15: line 1 is the raw disclosure label; the first row is line 2.
    assert "silent_retry" not in captured.out.split("\n")[1]
    assert "mislabeled_kill" in captured.out
    assert captured.out.rstrip("\n").splitlines()[-1] == "1 detector silenced by config: silent_retry"

