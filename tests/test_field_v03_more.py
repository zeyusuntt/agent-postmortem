"""The remaining v0.3 field-triage clauses not yet covered by
tests/test_field_v03.py (docs/DESIGN.md 4.13, 5.3b; `.reviews/v0.3/
N0r-decisions.md` D14 to D17; `.reviews/v0.3/N0-codex.txt` finding 16;
`.reviews/field-feedback-triage-2026-10-01.md`).

One test (or one small group sharing a fixture) per clause listed in
DESIGN.md 4.13.9's "New tests, one per triage clause not yet covered"
paragraph. Every builder this file needs that test_field_v03.py already
built is imported from there rather than copied (CLAUDE.md), including the
one shared fake session id/slug and the agent-id/wf-id conventions.

Model-level assertions use `getattr(obj, "field", None)` so a field that
does not exist yet (N1's MarkerHit, for example) fails with a plain
AssertionError, never an AttributeError -- the same discipline
test_field_v03.py documents at its own top. Every test is
`xfail(strict=True)` naming its own milestone unless it already passes
today, in which case it is an unmarked regression pin (D19).
"""

from __future__ import annotations

import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import cli, detectors, reader, rules, watch
from tests import rule_fixtures
from tests.test_field_v03 import (
    FAKE_UUID,
    _aid,
    _assistant_text,
    _assistant_tool,
    _kill,
    _no_config,
    _parent_path,
    _simple_completed_journal,
    _tool_result,
    _user,
    _wf_dir,
    _write_jsonl,
)
from tests.test_watch import (
    _assistant_text as _watch_assistant_text,
)
from tests.test_watch import (
    _Clock,
    _meta,
    _patch_clock,
)
from tests.test_watch import (
    _result as _watch_result,
)
from tests.test_watch import (
    _started as _watch_started,
)
from tests.test_watch import (
    _transcript_path as _watch_transcript_path,
)
from tests.test_watch import (
    _user_text as _watch_user_text,
)
from tests.test_watch import (
    _write_lines as _watch_write_lines,
)
from tests.test_watch import (
    _write_transcript as _watch_write_transcript,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _backdate(run_dir: Path, when: datetime) -> None:
    """Push every file directly inside run_dir's own mtime back to `when`
    (mirrors tests/test_scan_triage.py's _backdate): the scan-triage/rescue
    'possibly live' rule (docs/DESIGN.md 4.7 rule 1a) reads file mtime, not
    an event's own JSON timestamp, so a fixture whose events claim to be old
    still reads as live unless its files are aged too."""
    ts = when.timestamp()
    for path in run_dir.iterdir():
        if path.is_file():
            os.utime(path, (ts, ts))


# =============================================================================
# 1-2. field_cat_payload_marker_position: a cat payload where the marker
# text sits deep in ordinary output (negative: never anchored) and a
# harness notice sitting at byte zero of the same kind of result (positive:
# anchored). N1 (docs/DESIGN.md 4.13.2's MarkerHit.anchored/in_payload).
# =============================================================================


def _cat_payload_fixture(tmp_path, monkeypatch, wf_id: str, n: int, content: str):
    root = tmp_path / "root"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(n)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _simple_completed_journal(run_dir, agent_id)
    events = [
        _user("Show me the build log.", base, agent_id),
        _assistant_tool("t1", "Bash", {"command": "cat build.log"}, base + timedelta(seconds=1), agent_id),
        _tool_result("t1", content, base + timedelta(seconds=2), agent_id),
        _assistant_text("Looked at it.", base + timedelta(seconds=3), agent_id),
    ]
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", events)
    _no_config(monkeypatch, tmp_path)
    return run_dir


# N1 (implemented): a cat payload whose ordinary file content happens to
# carry both bash_backgrounded fragments well after the result's start must
# never anchor a MarkerHit there.
def test_field_cat_payload_marker_later_in_output_not_anchored(tmp_path, monkeypatch):
    content = (
        "build step 1: compiling...\n"
        "build step 2: linking...\n"
        "build step 3: running tests...\n"
        "note: an unrelated earlier job did not complete within its own budget "
        "and was later moved to the background queue by someone else\n"
    )
    run_dir = _cat_payload_fixture(tmp_path, monkeypatch, "wf_00000000-f0f", 101, content)

    run = reader.load_run(run_dir)
    attempt = run.attempts[0]
    marker_hits = getattr(attempt, "marker_hits", None)
    anchored_backgrounded = [
        hit for hit in (marker_hits or [])
        if getattr(hit, "marker", None) == "bash_backgrounded" and getattr(hit, "anchored", False)
    ]
    assert marker_hits is not None and not anchored_backgrounded


# N1 (implemented): a harness backgrounded notice sitting at the very start
# of a Bash result (byte zero) is an anchored, not-in-payload MarkerHit.
def test_field_harness_notice_at_byte_zero_is_anchored(tmp_path, monkeypatch):
    content = (
        "Command did not complete within its 120s timeout and was moved to "
        "the background (ID: b1). Output is being written to: /tmp/x"
    )
    run_dir = _cat_payload_fixture(tmp_path, monkeypatch, "wf_00000000-f11", 102, content)

    run = reader.load_run(run_dir)
    attempt = run.attempts[0]
    marker_hits = getattr(attempt, "marker_hits", None)
    anchored_backgrounded = [
        hit for hit in (marker_hits or [])
        if getattr(hit, "marker", None) == "bash_backgrounded"
        and getattr(hit, "anchored", False)
        and not getattr(hit, "in_payload", True)
    ]
    assert marker_hits is not None and len(anchored_backgrounded) == 1


# =============================================================================
# 3. field_cross_command_alias_stability: the same real agent gets the same
# 12-hex alias from `run --json` and `rescue plan --json` (docs/DESIGN.md
# 4.13.5/5.3b, D10). N3/N6.
# =============================================================================


def test_field_cross_command_alias_stability(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f12"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(103)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": agent_id, "label": "impl", "phase": "Implement"},
    ])
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do it.", base, agent_id),
        _kill(base + timedelta(seconds=1), agent_id),
    ])
    _no_config(monkeypatch, tmp_path)
    _backdate(run_dir, datetime.now(timezone.utc) - timedelta(days=1))

    def _run_json(argv):
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(argv)
        return json.loads(buf.getvalue())

    run_payload = _run_json(["run", str(run_dir), "--json"])
    run_alias = run_payload["run"]["attempts"][0]["agent_id"]

    rescue_payload = _run_json(["rescue", "plan", str(run_dir), "--json"])
    # N6 (docs/DESIGN.md 5.3b): `units[].agent` is the per-render label (a unit is a
    # logical agent and may span attempts); the attempt's alias is carried by the task
    # pointer's aliased file name, `agent-<alias hex>.jsonl:<line>` (4.13.5).
    pointer = rescue_payload["units"][0]["task"]["pointer"]
    pointer_stem = re.match(r"^agent-([0-9a-f]{12,64})\.jsonl:\d+$", pointer)
    assert pointer_stem, pointer
    rescue_alias = "a:" + pointer_stem.group(1)

    alias_re = re.compile(r"^a:[0-9a-f]{12}$")
    assert alias_re.match(run_alias), run_alias
    assert alias_re.match(rescue_alias), rescue_alias
    assert run_alias == rescue_alias


# =============================================================================
# 4. field_share_output_never_leaks_planted_real_text: a distinctive real
# label, phase title, and repo path planted in one fixture must never
# appear in run text/json, scan text/json, rescue plan text/json, rescue
# status text, or a hook check message built from the same run (docs/
# DESIGN.md 4.13.5/5.3b's share invariant; 7.2's hook-is-always-share rule).
# N3/N5.
# =============================================================================

_SENTINEL_LABEL = "Zz-Real-Agent-Nickname"
_SENTINEL_PHASE = "Zz-Real-Phase-Title"
_SENTINEL_PATH = "/home/user/project/zz-real-secret-path.py"


def _share_leak_fixture(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f13"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(104)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _write_jsonl(run_dir / "journal.jsonl", [
        {
            "type": "started", "key": "v2:x", "agentId": agent_id,
            "label": _SENTINEL_LABEL, "phase": _SENTINEL_PHASE,
        },
    ])
    events = [
        _user("Write the secret file.", base, agent_id),
        _assistant_tool(
            "t1", "Write", {"file_path": _SENTINEL_PATH, "content": "x"},
            base + timedelta(seconds=1), agent_id,
        ),
        _tool_result("t1", "", base + timedelta(seconds=2), agent_id),
        _kill(base + timedelta(seconds=3), agent_id),
    ]
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", events)

    main_transcript = _parent_path(root)
    _write_jsonl(main_transcript, [
        {
            "type": "user", "message": {"role": "user", "content": "start"},
            "sessionId": FAKE_UUID, "timestamp": _iso(base - timedelta(seconds=5)),
        },
    ])

    _no_config(monkeypatch, tmp_path)
    _backdate(run_dir, datetime.now(timezone.utc) - timedelta(days=1))
    os.utime(main_transcript, (
        (datetime.now(timezone.utc) - timedelta(days=1)).timestamp(),
        (datetime.now(timezone.utc) - timedelta(days=1)).timestamp(),
    ))
    return root, run_dir, main_transcript


def _assert_sentinels_absent(text: str) -> None:
    assert _SENTINEL_LABEL not in text
    assert _SENTINEL_PHASE not in text
    assert _SENTINEL_PATH not in text


def test_field_share_output_never_leaks_planted_real_text(tmp_path, monkeypatch, capsys):
    root, run_dir, main_transcript = _share_leak_fixture(tmp_path, monkeypatch)

    cli.main(["run", str(run_dir)])
    _assert_sentinels_absent(capsys.readouterr().out)

    cli.main(["run", str(run_dir), "--json"])
    _assert_sentinels_absent(capsys.readouterr().out)

    cli.main(["scan", "--root", str(root), "--all"])
    _assert_sentinels_absent(capsys.readouterr().out)

    cli.main(["scan", "--root", str(root), "--all", "--json"])
    _assert_sentinels_absent(capsys.readouterr().out)

    cli.main(["rescue", "plan", str(run_dir)])
    _assert_sentinels_absent(capsys.readouterr().out)

    cli.main(["rescue", "plan", str(run_dir), "--json"])
    _assert_sentinels_absent(capsys.readouterr().out)

    cli.main(["rescue", "status", str(run_dir)])
    _assert_sentinels_absent(capsys.readouterr().out)

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "hookstate"))
    hook_payload = {
        "hook_event_name": "Stop",
        "transcript_path": str(main_transcript),
        "session_id": FAKE_UUID,
        "stop_hook_active": False,
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(hook_payload)))
    cli.main(["hook", "check"])
    _assert_sentinels_absent(capsys.readouterr().out)


# =============================================================================
# 5. field_rescue_skill_uses_local_not_no_redact: the /rescue skill's own
# invocation uses --local and never --no-redact (docs/DESIGN.md 4.13.5: "The
# /rescue skill runs rescue plan --local"). N3.
# =============================================================================

_RESCUE_SKILL_PATH = (
    Path(__file__).resolve().parent.parent / "plugin" / "skills" / "rescue" / "SKILL.md"
)


def test_field_rescue_skill_uses_local_not_no_redact():
    text = _RESCUE_SKILL_PATH.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines()]
    assert "agent-postmortem rescue plan <run-id> --local --evidence" in lines
    assert "agent-postmortem rescue status <run-id> --local" in lines
    assert "--no-redact" not in text


# =============================================================================
# 6. field_operation_counts: a warm scan parses zero transcripts for every
# cached run, and detector discovery runs exactly once per process (docs/
# DESIGN.md 4.13.7). Counted by monkeypatching the parse and discovery
# functions, never by wall-clock. N7.
# =============================================================================


def _opcount_run(tmp_path, root: Path, wf_id: str, n: int) -> Path:
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(n)
    base = datetime.now(timezone.utc) - timedelta(days=5)

    _simple_completed_journal(run_dir, agent_id)
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do it.", base, agent_id),
        _assistant_text("Done.", base + timedelta(seconds=1), agent_id),
    ])
    _backdate(run_dir, base)
    return run_dir


@pytest.mark.xfail(
    strict=True,
    reason="N7 step 3 is deferred past v0.3 by the owner's decision C (N7-plan.md); a warm "
           "scan parses every transcript today",
)
def test_field_warm_scan_parses_zero_cached_transcripts(tmp_path, monkeypatch):
    root = tmp_path / "root"
    _opcount_run(tmp_path, root, "wf_00000000-f14", 105)
    _opcount_run(tmp_path, root, "wf_00000000-f15", 106)
    _no_config(monkeypatch, tmp_path)

    calls = {"n": 0}
    original = reader._read_transcript

    def _counting_read_transcript(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(reader, "_read_transcript", _counting_read_transcript)

    assert cli.main(["scan", "--root", str(root), "--all"]) in (0, 4)
    calls["n"] = 0  # cold scan done; only the second (warm) scan counts below

    assert cli.main(["scan", "--root", str(root), "--all"]) in (0, 4)
    assert calls["n"] == 0, f"warm scan parsed {calls['n']} transcripts for cached runs"


def test_field_detector_discovery_runs_once_per_process(tmp_path, monkeypatch):
    import pkgutil

    root = tmp_path / "root"
    run_dir_a = _opcount_run(tmp_path, root, "wf_00000000-f14", 107)
    run_dir_b = _opcount_run(tmp_path, root, "wf_00000000-f15", 108)
    _no_config(monkeypatch, tmp_path)

    calls = {"n": 0}
    original_iter_modules = pkgutil.iter_modules

    def _counting_iter_modules(*args, **kwargs):
        calls["n"] += 1
        return original_iter_modules(*args, **kwargs)

    monkeypatch.setattr(detectors.pkgutil, "iter_modules", _counting_iter_modules)
    # docs/DESIGN.md 4.13.11 "N7": reset the process-wide registry (a
    # documented test hook) so an earlier test's discovery does not hide this
    # one's single discovery.
    detectors._reset_registry()

    run_a = reader.load_run(run_dir_a)
    run_b = reader.load_run(run_dir_b)
    detectors.run_detectors(run_a)
    detectors.run_detectors(run_b)

    assert calls["n"] == 1, f"detector discovery ran {calls['n']} times for 2 run_detectors() calls"


# =============================================================================
# 7. field_live_workflow_attempt_shows_end_no_result: an attempt still open
# in the journal (no journal result, no kill sentinel) shows END "no
# result", not "completed" (docs/DESIGN.md 4.13.3). N2.
# =============================================================================


def test_field_live_workflow_attempt_shows_end_no_result(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f18"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(109)
    base = datetime.now(timezone.utc) - timedelta(minutes=2)

    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": agent_id, "label": "impl", "phase": "Implement"},
    ])
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do the thing.", base, agent_id),
        _assistant_text("Still working on it.", base + timedelta(seconds=5), agent_id),
    ])
    _no_config(monkeypatch, tmp_path)

    cli.main(["run", str(run_dir)])
    out = capsys.readouterr().out
    agent_row = next(line for line in out.splitlines() if line.startswith("agent-1"))
    assert "no result" in agent_row
    assert not re.search(r"\bcompleted\b", agent_row)


def test_field_live_workflow_attempt_with_kill_sentinel_shows_end_killed_not_no_result(
    tmp_path, monkeypatch, capsys,
):
    """docs/DESIGN.md 4.7 (N1N2 review finding 4, rejected with a contract
    clarification: the no-result override only ever replaces whatever
    'completed' the transcript-only outcome fallback would otherwise claim).
    A transcript that already ended in the kill sentinel is authoritative
    evidence of a kill and keeps reading 'killed', even though the journal's
    own occurrence is still open exactly like the completed-fallback case
    above -- the two cases pin both halves of 4.7's own wording at once."""
    root = tmp_path / "root"
    wf_id = "wf_00000000-e3d"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(118)
    base = datetime.now(timezone.utc) - timedelta(minutes=2)

    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": agent_id, "label": "impl", "phase": "Implement"},
    ])
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do the thing.", base, agent_id),
        _assistant_text("Working on it.", base + timedelta(seconds=5), agent_id),
        _kill(base + timedelta(seconds=10), agent_id),
    ])
    _no_config(monkeypatch, tmp_path)

    cli.main(["run", str(run_dir)])
    out = capsys.readouterr().out
    agent_row = next(line for line in out.splitlines() if line.startswith("agent-1"))
    assert "killed" in agent_row
    assert "no result" not in agent_row


# =============================================================================
# 8. field_killed_excludes_parent_stops_and_human: the AGENT table's KILLED
# column counts an interrupt only when its cause is neither parent_stop nor
# human (docs/DESIGN.md 4.13.8). N8.
# =============================================================================


def _killed_column(text: str, label: str) -> int:
    for line in text.splitlines():
        if line[:10].strip() == label:
            # AGENT[0:10] PHASE[10:21] ATTEMPTS[21:31] END[31:50] KILLED[50:58] ...
            return int(line[50:58].strip())
    raise AssertionError(f"no AGENT row for {label!r} in:\n{text}")


def test_field_killed_excludes_parent_stops_and_human(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f19"
    run_dir = _wf_dir(root, wf_id)
    base = datetime.now(timezone.utc) - timedelta(minutes=30)

    agent_p = _aid(110)
    agent_h = _aid(111)
    agent_r1 = _aid(112)
    agent_r2 = _aid(113)

    kill_ts_p = base + timedelta(seconds=2)
    kill_ts_h = base + timedelta(seconds=102)
    kill_ts_r1 = base + timedelta(seconds=250)

    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:p", "agentId": agent_p, "label": "p-agent", "phase": "Implement"},
        {"type": "started", "key": "v2:h", "agentId": agent_h, "label": "h-agent", "phase": "Implement"},
        {"type": "started", "key": "v2:r", "agentId": agent_r1, "label": "r-agent", "phase": "Implement"},
        {"type": "started", "key": "v2:r", "agentId": agent_r2, "label": "r-agent", "phase": "Implement"},
        {"type": "result", "key": "v2:r", "agentId": agent_r2, "result": {"ok": True}},
    ])
    _write_jsonl(run_dir / f"agent-{agent_p}.jsonl", [
        _user("P.", base, agent_p),
        _assistant_text("working", base + timedelta(seconds=1), agent_p),
        _kill(kill_ts_p, agent_p),
    ])
    _write_jsonl(run_dir / f"agent-{agent_h}.jsonl", [
        _user("H.", base + timedelta(seconds=100), agent_h),
        _assistant_text("working", base + timedelta(seconds=101), agent_h),
        _kill(kill_ts_h, agent_h),
    ])
    _write_jsonl(run_dir / f"agent-{agent_r1}.jsonl", [
        _user("R.", base + timedelta(seconds=200), agent_r1),
        _assistant_text("working", base + timedelta(seconds=201), agent_r1),
        _kill(kill_ts_r1, agent_r1),
    ])
    _write_jsonl(run_dir / f"agent-{agent_r2}.jsonl", [
        _user("R again.", base + timedelta(seconds=260), agent_r2),
        _assistant_text("Done.", base + timedelta(seconds=261), agent_r2),
    ])

    # Parent evidence (docs/DESIGN.md 4.4): a TaskStop naming agent_p's own
    # id by value (matches regardless of timing), and a parent kill sentinel
    # within 30s of agent_h's own kill -- both placed far (>30s) from every
    # other attempt's kill timestamp so none of them cross-attributes.
    parent = _parent_path(root)
    _write_jsonl(parent, [
        _assistant_tool(
            "stop1", "TaskStop", {"agent_id": agent_p}, kill_ts_p - timedelta(seconds=10),
        ),
        _kill(kill_ts_h - timedelta(seconds=10)),
    ])
    _no_config(monkeypatch, tmp_path)

    cli.main(["run", str(run_dir), "--no-redact"])
    out = capsys.readouterr().out
    assert _killed_column(out, "p-agent") == 0
    assert _killed_column(out, "h-agent") == 0
    assert _killed_column(out, "r-agent") == 1


# =============================================================================
# 9. field_rescue_no_none_step_and_conditional_fan_out_caveat: a rescue
# STEPS block never prints the literal '<none>' command target, and the
# fan-out caveat is printed only when two attempts of different keys
# actually overlapped in time -- never for a single-key run (docs/
# DESIGN.md 4.13.8). N8.
# =============================================================================


def test_field_rescue_no_none_step_and_conditional_fan_out_caveat(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f1a"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(114)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:only", "agentId": agent_id, "label": "lone", "phase": "Implement"},
    ])
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Look into it.", base, agent_id),
        _kill(base + timedelta(seconds=1), agent_id),
    ])
    _no_config(monkeypatch, tmp_path)
    _backdate(run_dir, datetime.now(timezone.utc) - timedelta(days=1))

    cli.main(["rescue", "plan", str(run_dir)])
    out = capsys.readouterr().out
    assert "<none>" not in out
    assert "parallel fan-out" not in out


# =============================================================================
# 10. field_no_look_when_all_hidden_info_and_no_empty_what_to_do: no [look]
# "see the findings hidden here" item when the only hidden findings are
# info, and the WHAT TO DO heading itself is never printed with nothing
# under it (docs/DESIGN.md 4.13.8). N8.
# =============================================================================


def _clean_run_fixture(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "root"
    wf_id = "wf_00000000-f1b"
    run_dir = _wf_dir(root, wf_id)
    agent_id = _aid(115)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": agent_id, "label": "impl", "phase": "Implement"},
        {"type": "result", "key": "v2:x", "agentId": agent_id, "result": {"ok": True}},
    ])
    _write_jsonl(run_dir / f"agent-{agent_id}.jsonl", [
        _user("Do it.", base, agent_id),
        _assistant_text("Done.", base + timedelta(seconds=1), agent_id),
    ])
    _no_config(monkeypatch, tmp_path)
    return run_dir


def test_field_no_look_item_when_every_hidden_finding_is_info(tmp_path, monkeypatch, capsys):
    run_dir = _clean_run_fixture(tmp_path, monkeypatch)

    cli.main(["run", str(run_dir)])
    out = capsys.readouterr().out
    assert "[look]" not in out
    assert "see the findings hidden here" not in out


def test_field_what_to_do_heading_never_printed_empty(tmp_path, monkeypatch, capsys):
    run_dir = _clean_run_fixture(tmp_path, monkeypatch)

    cli.main(["run", str(run_dir), "--verbose"])
    out = capsys.readouterr().out
    assert "WHAT TO DO" not in out


# =============================================================================
# 11. field_mislabeled_kill_precursor_wording: the runtime-cause title names
# an observed precursor (a Bash command moved to the background just
# before) without asserting that it caused the kill (docs/DESIGN.md
# 4.13.8). N8.
# =============================================================================


def test_field_mislabeled_kill_names_backgrounded_precursor(tmp_path, monkeypatch):
    root = tmp_path / "root"
    wf_id = "wf_00000000-f1c"
    run_dir = _wf_dir(root, wf_id)
    agent1 = _aid(116)
    agent2 = _aid(117)
    base = datetime.now(timezone.utc) - timedelta(minutes=20)

    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": agent1, "label": "impl", "phase": "Implement"},
        {"type": "started", "key": "v2:x", "agentId": agent2, "label": "impl", "phase": "Implement"},
        {"type": "result", "key": "v2:x", "agentId": agent2, "result": {"ok": True}},
    ])
    backgrounded_notice = (
        "Command did not complete within its 120s timeout and was moved to "
        "the background (ID: b1). Output is being written to: /tmp/x"
    )
    _write_jsonl(run_dir / f"agent-{agent1}.jsonl", [
        _user("Run the long build.", base, agent1),
        _assistant_tool("t1", "Bash", {"command": "make build"}, base + timedelta(seconds=1), agent1),
        _tool_result("t1", backgrounded_notice, base + timedelta(seconds=2), agent1),
        _kill(base + timedelta(seconds=352), agent1),  # >= 300s after the precursor
    ])
    _write_jsonl(run_dir / f"agent-{agent2}.jsonl", [
        _user("Retry.", base + timedelta(seconds=400), agent2),
        _assistant_text("Done.", base + timedelta(seconds=401), agent2),
    ])
    _no_config(monkeypatch, tmp_path)

    run = reader.load_run(run_dir)
    from agent_postmortem.detectors import mislabeled_kill

    findings = mislabeled_kill.DETECTOR.run(run)
    assert len(findings) == 1
    title = findings[0].title
    assert "moved to the background" in title
    assert "caused" not in title.lower()


# =============================================================================
# 12. field_watch_start_and_ok_lines: watch prints a START line, first, as
# soon as the journal is found, and an OK line per closed completed
# attempt, in both text and JSON (docs/DESIGN.md 4.13.8/D18). Also folds in
# the per-sink leak check for watch's own sink: its agent field is a
# per-process pseudonym, never the real planted label. N8.
# =============================================================================


def _watch_scenario(run_dir: Path) -> tuple[str, str]:
    run_dir.mkdir(parents=True)
    key, agent_id = "v2:only", "aaa0000000000001a"
    _watch_write_lines(run_dir / "journal.jsonl", [
        _watch_started("2026-02-01T00:00:00.000Z", key, agent_id, _SENTINEL_LABEL),
        _watch_result("2026-02-01T00:00:30.000Z", key, agent_id),
    ])
    _meta(run_dir, agent_id)
    _watch_write_transcript(_watch_transcript_path(run_dir, agent_id), [
        _watch_user_text("2026-02-01T00:00:00.000Z", "Only."),
        _watch_assistant_text("2026-02-01T00:00:29.000Z", "Done."),
    ])
    return key, agent_id


def test_field_watch_start_and_ok_lines_text(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "wf_00000000-f1d"
    _watch_scenario(run_dir)
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    monkeypatch.setattr(watch, "_sleep", lambda seconds: None)

    code = cli.main(["watch", str(run_dir), "--interval", "1", "--done-grace", "0"])
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]

    assert lines[0] == f"START  {run_dir.name}: 1 agents started so far, 1 attempts."
    assert any(re.match(r"^OK\s+\S+ attempt 1 returned a result\.$", line) for line in lines)
    assert _SENTINEL_LABEL not in out
    assert code == 0


def test_field_watch_start_and_ok_lines_json(monkeypatch, capsys, tmp_path):
    run_dir = tmp_path / "wf_00000000-f1e"
    _watch_scenario(run_dir)
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    monkeypatch.setattr(watch, "_sleep", lambda seconds: None)

    code = cli.main(["watch", str(run_dir), "--interval", "1", "--done-grace", "0", "--json"])
    out = capsys.readouterr().out
    events = [json.loads(line) for line in out.splitlines() if line.strip()]

    assert events, "watch --json produced no lines"
    assert events[0]["event"] == "START"
    assert events[0]["run"] == run_dir.name
    assert isinstance(events[0]["agents"], int) and isinstance(events[0]["attempts"], int)
    ok_events = [e for e in events if e.get("event") == "OK"]
    assert len(ok_events) == 1
    assert ok_events[0]["attempt"] == 1
    assert code == 0


# =============================================================================
# 13. field_scan_help_documents_exit_3: 'agent-postmortem scan --help'
# documents exit 3 meaning verdict FAILED (docs/DESIGN.md 5.3b's exit-code
# precedence table). N5.
# =============================================================================


def test_field_scan_help_documents_exit_3(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["scan", "--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "exit 3" in out.lower()
    assert "failed" in out.lower()


# =============================================================================
# 14. field_rules_propose_marks_an_adopted_rule: a rule already adopted
# (config [[adopted]]) is marked adopted instead of proposed again by
# `rules propose` (docs/DESIGN.md 4.13.8/section 9). Also folds in the
# per-sink leak check for 'rules review': the real adopted target path
# never appears, only its target-N pseudonym. N8.
# =============================================================================


def test_field_rules_propose_marks_an_adopted_rule(tmp_path, monkeypatch, capsys):
    real_target = "/home/user/project/zz-real-config-target.md"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[[adopted]]\nrule = "R-GIT-CWD"\ndate = "2026-08-10"\ntarget = "{real_target}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))

    root = tmp_path / "root"
    rule_fixtures.build_git_cwd_mismatch_run(
        root, "wf_00000000-f16", datetime(2026, 8, 10, tzinfo=timezone.utc), agent_n=1,
    )
    rule_fixtures.build_git_cwd_mismatch_run(
        root, "wf_00000000-f17", datetime(2026, 8, 10, tzinfo=timezone.utc), agent_n=2,
    )
    rule_fixtures.build_git_cwd_mismatch_run(
        root, "wf_00000000-f1f", datetime(2026, 8, 20, tzinfo=timezone.utc), agent_n=3,
    )

    code = cli.main(["rules", "propose", "--root", str(root)])
    out = capsys.readouterr().out
    assert code == 0
    assert "R-GIT-CWD" in out
    assert "adopted" in out.lower()

    code = cli.main(["rules", "review", "--root", str(root)])
    review_out = capsys.readouterr().out
    assert code == 0
    assert real_target not in review_out


# =============================================================================
# 15. field_r_no_long_bash_carries_macos_note: R-NO-LONG-BASH's own rule
# text names the macOS timeout(1) gap (docs/DESIGN.md 4.13.8). N8.
# =============================================================================


def test_field_r_no_long_bash_carries_macos_note():
    text = rules.RULES["R-NO-LONG-BASH"].text
    assert "macOS" in text
    assert "timeout(1)" in text


def test_field_rescue_fan_out_caveat_when_keys_overlapped(tmp_path, monkeypatch, capsys):
    """Regression pin (D19): today the caveat prints unconditionally, so this
    positive case already passes; it keeps N8 from satisfying the negative
    case by deleting the caveat outright."""
    root = tmp_path / "root"
    wf_id = "wf_00000000-f2a"
    run_dir = _wf_dir(root, wf_id)
    a1, a2 = _aid(116), _aid(117)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    # Two keys started together and ran at the same time (a parallel stage);
    # the first was killed by the runtime and restarted, the second completed.
    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:left", "agentId": a1, "label": "left", "phase": "Implement"},
        {"type": "started", "key": "v2:right", "agentId": a2, "label": "right", "phase": "Implement"},
        {"type": "result", "key": "v2:right", "agentId": a2, "result": {"ok": True}},
    ])
    _write_jsonl(run_dir / f"agent-{a1}.jsonl", [
        _user("Left half.", base, a1),
        _assistant_text("Working.", base + timedelta(seconds=5), a1),
        _kill(base + timedelta(seconds=600), a1),
    ])
    _write_jsonl(run_dir / f"agent-{a2}.jsonl", [
        _user("Right half.", base + timedelta(seconds=1), a2),
        _assistant_text("Done.", base + timedelta(seconds=300), a2),
    ])
    _no_config(monkeypatch, tmp_path)
    _backdate(run_dir, datetime.now(timezone.utc) - timedelta(days=1))

    cli.main(["rescue", "plan", str(run_dir)])
    out = capsys.readouterr().out
    assert "parallel fan-out" in out
