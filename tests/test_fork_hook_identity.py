"""A fork whose copied block carries the fork's own `sessionId` (Claude Code 2.1.305 and later;
2.1.312 also opens it with a `history-suppression` entry, cause `fork_inherit`). Only the hook
attachments inside the block still name the session that wrote them, in their top-level
`session_id`. docs/DESIGN.md 4.2 (FA4, hook identity).

The field case (2026-10-30, "status and run disagree about the same session's subagents"): a
background session forked from its dispatcher inherited the dispatcher's Agent launch. `status`
counted that subagent as running in the fork, `run` found no subagent transcript for it and said
WORK AT RISK unknown. The subagent's transcript lives under the parent's directory, as here.

Fixtures are synthetic; session ids are built at run time and cwds are under /tmp.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_postmortem import cli, reader
from tests.test_field_v03 import (
    FAKE_SLUG,
    _aid,
    _assistant_text,
    _assistant_tool,
    _envelope,
    _no_config,
    _tool_result,
    _user,
    _write_jsonl,
)

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
CHILD = _aid(0x5A1)


def _sid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


PARENT_ID, FORK_ID, PARENT_PROCESS = _sid(0xC1A1), _sid(0xC2B2), _sid(0xC3C3)


def _at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _dt(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _hook(seconds: float, session: str, process: str | None) -> dict:
    entry = {
        "type": "attachment",
        "attachment": {"type": "hook_success", "hookName": "PostToolUse:Agent",
                       "hookEvent": "PostToolUse", "content": "", "exitCode": 0},
        "sessionId": session, "timestamp": _at(seconds), "cwd": "/tmp/fork-hook-work",
    }
    if process is not None:
        entry["session_id"] = process
    return entry


def _stamped(entry: dict, session: str) -> dict:
    return {**entry, "sessionId": session, "cwd": "/tmp/fork-hook-work"}


def _block(session: str, process: str) -> list[dict]:
    """What the parent wrote: hooks, an async Agent launch and its completed notification (as
    the queued_command attachment 2.1.312 writes beside it)."""
    launch_result = {**_tool_result("ta1", "Async agent launched successfully.", _dt(4)),
                     "toolUseResult": {"agentId": CHILD, "status": "async_launched",
                                       "isAsync": True}}
    notice = {"type": "attachment",
              "attachment": {"type": "queued_command",
                             "prompt": _envelope(CHILD, "ta1", "completed")},
              "timestamp": _at(20)}
    entries = [
        {"type": "history-suppression", "cause": "fork_inherit", "sessionId": session, "ts": _at(0)},
        _stamped(_user("Plan the work.", _dt(1)), session),
        _hook(2, session, process),
        _stamped(_assistant_tool("ta1", "Agent", {"description": "child"}, _dt(3)), session),
        _hook(3.5, session, process),
        _stamped(launch_result, session),
        _hook(4.5, session, process),
        _stamped(notice, session),
    ]
    return entries


def _write(tmp_path: Path, monkeypatch) -> tuple[Path, Path, int]:
    """P = its own history plus the child; F = P's block re-stamped with F's sessionId, then F's
    own entries, the first own hook naming F. Returns (parent, fork, last inherited line)."""
    _no_config(monkeypatch, tmp_path)
    slug_dir = tmp_path / "root" / FAKE_SLUG
    parent = slug_dir / f"{PARENT_ID}.jsonl"
    fork = slug_dir / f"{FORK_ID}.jsonl"
    _write_jsonl(parent, [entry for entry in _block(PARENT_ID, PARENT_ID)[1:]])
    _write_jsonl(slug_dir / PARENT_ID / "subagents" / f"agent-{CHILD}.jsonl", [
        _user("child work", _dt(5), CHILD),
        _assistant_text("Done.", _dt(19), CHILD),
    ])
    block = _block(FORK_ID, PARENT_PROCESS)
    own = [
        _stamped(_user("Fork: continue.", _dt(100)), FORK_ID),
        _hook(101, FORK_ID, FORK_ID),
        _stamped(_assistant_tool("own1", "Bash", {"command": "ls"}, _dt(102)), FORK_ID),
        _stamped(_tool_result("own1", "ok", _dt(103)), FORK_ID),
        _stamped(_assistant_text("Done here.", _dt(104)), FORK_ID),
    ]
    _write_jsonl(fork, block + own)
    last_foreign_hook = max(i for i, e in enumerate(block, 1) if e.get("session_id"))
    return parent, fork, last_foreign_hook


def test_the_block_up_to_the_last_hook_naming_another_session_is_inherited(tmp_path, monkeypatch):
    _parent, fork, last = _write(tmp_path, monkeypatch)
    assert reader._fork_scan(fork) == (last, True)


def test_status_and_run_agree_the_fork_has_no_subagent_of_its_own(tmp_path, monkeypatch, capsys):
    _parent, fork, _last = _write(tmp_path, monkeypatch)
    run = reader.load_run(fork)
    assert run.attempts == []
    assert [call.tool_use_id for call in run.main.tool_calls] == ["own1"]
    root = tmp_path / "root"
    cli.main(["status", str(fork), "--root", str(root), "--json"])
    status = json.loads(capsys.readouterr().out)
    assert sum(status["counts"].values()) == 0, status["counts"]
    assert status["subagents"] == []
    cli.main(["run", str(fork), "--root", str(root)])
    out = capsys.readouterr().out
    assert "0 subagents" in out
    assert "WORK AT RISK  unknown" not in out


def test_the_parent_keeps_its_subagent(tmp_path, monkeypatch, capsys):
    parent, _fork, _last = _write(tmp_path, monkeypatch)
    assert reader._fork_scan(parent) == (0, True)
    run = reader.load_run(parent)
    assert [a.agent_id for a in run.attempts] == [CHILD]


def test_a_session_whose_first_hook_names_itself_inherits_nothing(tmp_path):
    path = tmp_path / f"{FORK_ID}.jsonl"
    _write_jsonl(path, [
        _stamped(_user("Start.", _dt(0)), FORK_ID),
        _hook(1, FORK_ID, FORK_ID),
        _hook(2, FORK_ID, PARENT_PROCESS),  # a later foreign hook id is never a prefix
        _stamped(_assistant_text("ok", _dt(3)), FORK_ID),
    ])
    assert reader._fork_scan(path) == (0, True)


def test_a_fork_whose_own_hook_is_not_written_yet_inherits_nothing_for_now(tmp_path):
    path = tmp_path / f"{FORK_ID}.jsonl"
    _write_jsonl(path, _block(FORK_ID, PARENT_PROCESS))
    assert reader._fork_scan(path) == (0, False)


def test_a_short_hookless_session_stays_unsettled_until_a_hook_or_the_window_decides(tmp_path):
    # batch 1 review: a fork's copied block can still be arriving when it is first read, before
    # its first hook line; settling then would keep a follower at boundary 0 for good
    path = tmp_path / f"{FORK_ID}.jsonl"
    _write_jsonl(path, [_stamped(_user("Start.", _dt(0)), FORK_ID),
                        _stamped(_assistant_text("ok", _dt(1)), FORK_ID)])
    assert reader._fork_scan(path) == (0, False)


def _append(path: Path, entries: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _growing_fork(tmp_path: Path) -> tuple[Path, list[dict], int]:
    """A fork read while only its first copied lines (no hook line yet) are on disk."""
    _no_config_free = tmp_path / "root" / FAKE_SLUG
    fork = _no_config_free / f"{FORK_ID}.jsonl"
    block = _block(FORK_ID, PARENT_PROCESS)
    _write_jsonl(fork, block[:2])
    rest = block[2:] + [_hook(101, FORK_ID, FORK_ID),
                        _stamped(_assistant_text("Done here.", _dt(102)), FORK_ID)]
    last = max(i for i, e in enumerate(block, 1) if e.get("session_id"))
    return fork, rest, last


def test_a_follower_finds_the_fork_once_its_hook_lines_arrive(tmp_path, monkeypatch):
    from agent_postmortem import live
    _no_config(monkeypatch, tmp_path)
    fork, rest, last = _growing_fork(tmp_path)
    follower = live.SessionFollower(fork, jobs_root=tmp_path / "jobs", caller_session=None)
    assert follower._fork_boundary() == 0
    _append(fork, rest)
    assert follower._fork_boundary() == last


def test_a_parent_index_finds_the_fork_once_its_hook_lines_arrive(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    fork, rest, last = _growing_fork(tmp_path)
    reader.ParentIndex._cache.pop(fork, None)
    first = reader.ParentIndex.for_path(fork)
    assert first._skip_through == 0
    _append(fork, rest)
    second = reader.ParentIndex.for_path(fork)
    assert second._skip_through == last
    reader.ParentIndex._cache.pop(fork, None)


def test_a_hookless_session_past_the_window_is_settled(tmp_path):
    path = tmp_path / f"{FORK_ID}.jsonl"
    filler = "x" * 4000
    _write_jsonl(path, [_stamped(_assistant_text(filler, _dt(i)), FORK_ID) for i in range(300)])
    assert reader._fork_scan(path) == (0, True)


def test_an_old_style_fork_keeps_its_session_id_boundary(tmp_path):
    path = tmp_path / f"{FORK_ID}.jsonl"
    _write_jsonl(path, [_stamped(_user("Plan.", _dt(0)), PARENT_ID),
                        _hook(1, PARENT_ID, PARENT_ID),
                        _stamped(_user("Fork.", _dt(2)), FORK_ID),
                        _hook(3, FORK_ID, FORK_ID)])
    assert reader._fork_scan(path) == (2, True)


def test_a_hook_line_first_seen_past_the_first_mebibyte_decides_nothing(tmp_path):
    path = tmp_path / f"{FORK_ID}.jsonl"
    filler = "x" * 4000
    entries = [_stamped(_assistant_text(filler, _dt(i)), FORK_ID) for i in range(300)]
    entries += [_hook(400, FORK_ID, PARENT_PROCESS), _hook(401, FORK_ID, FORK_ID)]
    _write_jsonl(path, entries)
    assert reader._fork_scan(path) == (0, True)
