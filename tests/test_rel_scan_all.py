"""Release review of 0.3.0, round 2, finding 3: `scan --all` orders rows by the day and then by
the exact start time (not by discovery path within a day), and a session row that may still be
running says `possibly running`, so the `/postmortem latest` recipe can take the last row that
is neither `unfinished` nor `possibly running` (docs/DESIGN.md 4.7).

Every fixture is synthetic."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from agent_postmortem.cli import main
from tests.test_scan_triage import (
    _freeze_now,
    _iso,
    _wf_dir,
    _write_journal,
    _write_meta,
    _write_transcript,
)


def _uuid(n: int) -> str:
    """A synthetic session id, built here so no UUID literal sits in the source (tests/privacy_rules.py
    allows only the one all-zero literal)."""
    return f"00000000-0000-4000-8000-{n:012x}"


def _session(root, slug: str, uuid: str, started: datetime, last: datetime) -> None:
    slug_dir = root / slug
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{uuid}.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "hi"},
         "sessionId": uuid, "timestamp": _iso(started)},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "ok"}]}, "sessionId": uuid, "timestamp": _iso(last)},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    os.utime(path, (last.timestamp(), last.timestamp()))


def _rows(out: str) -> list[str]:
    return [line for line in out.splitlines() if "  session  " in line or "  workflow  " in line]


def test_rows_of_one_day_are_ordered_by_their_exact_start_time(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    # discovery order is by path: "-home-user-a-late" before "-home-user-b-early"
    early = now.replace(hour=1, minute=0, second=0, microsecond=0)
    if early > now - timedelta(hours=3):
        early = now - timedelta(hours=3)
    late = early + timedelta(minutes=30)
    _session(root, "-home-user-a-late", _uuid(0xa2), late,
             late + timedelta(minutes=1))
    _session(root, "-home-user-b-early", _uuid(0xa1), early,
             early + timedelta(minutes=1))
    assert main(["scan", "--root", str(root), "--all", "--raw"]) == 0
    rows = _rows(capsys.readouterr().out)
    assert len(rows) == 2
    assert "-home-user-b-early" in rows[0] and "-home-user-a-late" in rows[1], rows


def test_a_session_row_that_may_still_be_running_says_so(tmp_path, monkeypatch, capsys):
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    _session(root, "-home-user-stale", _uuid(0xa3),
             now - timedelta(days=2), now - timedelta(days=2) + timedelta(minutes=5))
    _session(root, "-home-user-live", _uuid(0xa4),
             now - timedelta(minutes=20), now - timedelta(seconds=30))
    assert main(["scan", "--root", str(root), "--all", "--raw"]) == 0
    rows = _rows(capsys.readouterr().out)
    live = [row for row in rows if "-home-user-live" in row]
    stale = [row for row in rows if "-home-user-stale" in row]
    assert live and "possibly running" in live[0], rows
    assert stale and "possibly running" not in stale[0], rows


def _session_raw(root, slug: str, uuid: str, stamps: list[str | None], mtime: float) -> None:
    """A session whose entries carry the exact timestamp strings given (None: no timestamp)."""
    slug_dir = root / slug
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{uuid}.jsonl"
    lines = []
    for i, stamp in enumerate(stamps):
        entry = {"type": "user" if i % 2 == 0 else "assistant", "sessionId": uuid,
                 "message": {"role": "user", "content": "hi"} if i % 2 == 0 else
                 {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}
        if stamp is not None:
            entry["timestamp"] = stamp
        lines.append(entry)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_rows_are_ordered_by_the_instant_and_undated_rows_come_first(tmp_path, monkeypatch, capsys):
    """Codex release review round 3, finding 2: `-07:00` and `+00:00` start times sort by the
    instant they name, not by their text, and a run with no start time sorts before every dated
    run (oldest first: nothing says it is new), in the text and in --json alike."""
    now = _freeze_now(monkeypatch)
    root = tmp_path / "root"
    day = (now - timedelta(days=3)).date().isoformat()
    old = (now - timedelta(days=3)).timestamp()
    # 01:00-07:00 is 08:00Z, after 07:30Z, although its text sorts first
    _session_raw(root, "-home-user-a-newer", _uuid(0xb1),
                 [f"{day}T01:00:00-07:00", f"{day}T01:01:00-07:00"], old)
    _session_raw(root, "-home-user-b-older", _uuid(0xb2),
                 [f"{day}T07:30:00+00:00", f"{day}T07:31:00+00:00"], old)
    # a completed workflow whose journal and transcript carry no timestamp at all
    wf = _wf_dir(root, "-home-user-c-undated", "wf_00000000-aa1")
    agent = "ac900000000000001"
    _write_journal(wf, [
        {"type": "started", "key": "v2:u", "agentId": agent, "label": "u", "phase": "Implement"},
        {"type": "result", "key": "v2:u", "agentId": agent, "outcome": "completed"},
    ])
    _write_meta(wf, agent, "u")
    _write_transcript(wf, agent, [{"type": "user", "message": {"role": "user", "content": "x"}}])
    for path in wf.iterdir():
        os.utime(path, (old, old))
    assert main(["scan", "--root", str(root), "--all", "--raw"]) == 0
    rows = _rows(capsys.readouterr().out)
    slugs = [next(s for s in ("c-undated", "b-older", "a-newer") if s in row) for row in rows]
    assert slugs == ["c-undated", "b-older", "a-newer"], rows
    assert main(["scan", "--root", str(root), "--all", "--raw", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    projects = [row["project"] for row in payload["rows"]]
    assert [p.rsplit("-", 2)[-2] + "-" + p.rsplit("-", 1)[-1] for p in projects] == [
        "c-undated", "b-older", "a-newer",
    ], projects
