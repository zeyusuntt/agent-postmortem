"""N3b fix B, S14: a `started` event whose agentId is not a canonical agent id
never has a transcript or meta file opened for it. The id comes straight from
the journal; `agent-{id}.jsonl` built from `q/../../../../OUTSIDE` would read a
file outside the run directory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_postmortem import reader

TRAVERSAL = "q/../../../../OUTSIDE"
LINE = {
    "type": "user", "timestamp": "2026-09-20T09:00:01.000Z",
    "message": {"role": "user", "content": "ZZOUTSIDE prompt"},
}


def _build(tmp_path: Path, agent_id: str):
    run_dir = tmp_path / "a" / "b" / "wf_00000000-c21"
    run_dir.mkdir(parents=True)
    (run_dir / "agent-q").mkdir()
    (run_dir / "journal.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [
            {"type": "launched"},
            {"type": "started", "timestamp": "2026-09-20T09:00:00.000Z", "key": "v2:k",
             "label": "impl:x", "phase": "Implement", "agentId": agent_id},
            {"type": "result", "timestamp": "2026-09-20T09:05:00.000Z", "key": "v2:k",
             "agentId": agent_id},
        ]) + "\n", encoding="utf-8",
    )
    (tmp_path / "OUTSIDE.jsonl").write_text(json.dumps(LINE) + "\n", encoding="utf-8")
    (tmp_path / "OUTSIDE.meta.json").write_text('{"agentType": "zz-outside"}', encoding="utf-8")
    return run_dir


def test_a_traversing_agent_id_reads_nothing_outside_the_run(tmp_path):
    run = reader.load_run(_build(tmp_path, TRAVERSAL))
    attempt = run.agents[0].attempts[0]
    assert attempt.events == []
    assert attempt.transcript_path is None
    assert attempt.meta == {}
    assert attempt.agent_type is None
    assert run.status == "INCOMPLETE"


@pytest.mark.parametrize("flags", [["--local"], ["--raw"]])
def test_a_traversing_agent_id_prints_no_outside_bytes(flags, tmp_path, capsys):
    from agent_postmortem.cli import main

    run_dir = _build(tmp_path, TRAVERSAL)
    capsys.readouterr()
    main(["run", str(run_dir), "--verbose", "--evidence", *flags])
    out = capsys.readouterr().out
    assert "ZZOUTSIDE" not in out
    assert "zz-outside" not in out


def test_a_canonical_agent_id_still_reads_its_transcript(tmp_path):
    run_dir = _build(tmp_path, "ac900000000000001")
    (run_dir / "agent-ac900000000000001.jsonl").write_text(json.dumps(LINE) + "\n", encoding="utf-8")
    (run_dir / "agent-ac900000000000001.meta.json").write_text('{"agentType": "impl"}', encoding="utf-8")
    run = reader.load_run(run_dir)
    attempt = run.agents[0].attempts[0]
    assert len(attempt.events) == 1
    assert attempt.agent_type == "impl"


# --- S14: the ENOENT retry of the lock create pauses between attempts ---------


def _enoent_then_ok(monkeypatch, failures):
    import errno
    import os as _os

    from agent_postmortem import state

    real_open = _os.open
    calls = {"failed": 0}
    sleeps: list[float] = []

    def _open(path, flags, mode=0o777, *, dir_fd=None):
        if path == "state.lock" and dir_fd is not None and (
            failures is None or calls["failed"] < failures
        ):
            calls["failed"] += 1
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(state.os, "open", _open)
    monkeypatch.setattr(state.time, "sleep", sleeps.append)
    return sleeps


def test_enoent_retry_pauses_one_millisecond_times_the_attempt_number(tmp_path, monkeypatch):
    from agent_postmortem import disclosure

    sleeps = _enoent_then_ok(monkeypatch, failures=2)
    book = disclosure.register([("agent", "ac900000000000001")], state_dir=tmp_path)
    assert book.available is True
    assert sleeps == [0.001, 0.002]


def test_persistent_enoent_keeps_the_attempt_count_and_pauses_between(tmp_path, monkeypatch):
    from agent_postmortem import disclosure, state

    sleeps = _enoent_then_ok(monkeypatch, failures=None)
    book = disclosure.register([("agent", "ac900000000000001")], state_dir=tmp_path)
    assert book.available is False
    assert len(sleeps) == state._LOCK_CREATE_ATTEMPTS - 1
    assert sleeps == [0.001 * n for n in range(1, state._LOCK_CREATE_ATTEMPTS)]
