"""`watch RUN_ID` with stdout to a pipe ("watch under Monitor
delivers nothing until the run ends"; docs/DESIGN.md 4.12).

A Claude Code Monitor reads the watch command's stdout through a pipe. Python block-buffers
stdout when it is not a terminal, so before the fix every line (START, OK, KILL) sat in the
buffer until the run ended and the process exited. These tests start the real CLI in a child
process with stdout to a pipe and PYTHONUNBUFFERED unset, and read each line while the run is
still going.

Synthetic data only: fake session id 00000000-0000-4000-8000-000000000000, fake project
directory, agent ids of 17 hex characters starting with 'a'.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
SESSION_ID = "00000000-0000-4000-8000-000000000000"
RUN_ID = "wf_00000000-e48"
AGENT_ID = "a0000000000000e48"
# Generous: a child interpreter starts in well under a second; a line that is flushed
# arrives within one poll (0.2 s). A buffered line never arrives while the run is open.
LINE_WAIT_SECONDS = 15.0

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="select() on a pipe")


def _run_dir(root: Path) -> Path:
    run_dir = root / "-home-user-project" / SESSION_ID / "subagents" / "workflows" / RUN_ID
    run_dir.mkdir(parents=True)
    return run_dir


def _append(path: Path, entry: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def _start_watch(run_dir: Path, *extra: str) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=str(SRC))
    env.pop("PYTHONUNBUFFERED", None)
    return subprocess.Popen(
        [sys.executable, "-m", "agent_postmortem.cli", "watch", str(run_dir),
         "--interval", "0.2", "--done-grace", "3600", *extra],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
    )


def _read_line(proc: subprocess.Popen, timeout: float = LINE_WAIT_SECONDS) -> str | None:
    """One stdout line, or None when none arrived within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    buffered = b""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], deadline - time.monotonic())
        if not ready:
            break
        chunk = os.read(proc.stdout.fileno(), 1)
        if not chunk:
            break
        buffered += chunk
        if chunk == b"\n":
            return buffered.decode("utf-8").rstrip("\n")
    return None


@pytest.fixture
def watched(tmp_path):
    run_dir = _run_dir(tmp_path)
    journal = run_dir / "journal.jsonl"
    _append(journal, {
        "type": "started", "timestamp": "2026-11-03T10:00:00Z", "key": "k1",
        "agentId": AGENT_ID, "label": "synthetic step", "phase": "Implement",
    })
    procs: list[subprocess.Popen] = []

    def start(*extra: str) -> subprocess.Popen:
        proc = _start_watch(run_dir, *extra)
        procs.append(proc)
        return proc

    yield journal, start
    for proc in procs:
        proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()


@pytest.mark.parametrize("json_flag", [(), ("--json",)], ids=["text", "json"])
def test_watch_lines_reach_a_pipe_while_the_run_is_still_open(watched, json_flag):
    journal, start = watched
    proc = start(*json_flag)

    first = _read_line(proc)
    assert first is not None, "START never reached the pipe while the run was open"
    if json_flag:
        assert json.loads(first)["event"] == "START"
    else:
        assert first.startswith(f"START  {RUN_ID}: 1 agents started so far")

    _append(journal, {
        "type": "result", "timestamp": "2026-11-03T10:01:00Z", "key": "k1",
        "agentId": AGENT_ID, "result": {"ok": True},
    })
    second = _read_line(proc)
    assert second is not None, "OK never reached the pipe while the run was open"
    if json_flag:
        assert json.loads(second)["event"] == "OK"
    else:
        assert second.startswith("OK ") and "attempt 1 returned a result." in second
    assert proc.poll() is None, "watch ended; the line may have come from the exit flush"


def test_watch_ends_quietly_when_its_reader_closes_the_pipe(tmp_path):
    # a flushed line meets a closed reader at once; watch must stop without a
    # traceback or the interpreter's "Exception ignored" note, with the shell's SIGPIPE code
    run_dir = _run_dir(tmp_path)
    _append(run_dir / "journal.jsonl", {
        "type": "started", "timestamp": "2026-11-03T10:00:00Z", "key": "k1",
        "agentId": AGENT_ID, "label": "synthetic step", "phase": "Implement",
    })
    read_end, write_end = os.pipe()
    os.close(read_end)
    env = dict(os.environ, PYTHONPATH=str(SRC))
    env.pop("PYTHONUNBUFFERED", None)
    proc = subprocess.run(
        [sys.executable, "-m", "agent_postmortem.cli", "watch", str(run_dir),
         "--interval", "0.2", "--done-grace", "3600"],
        stdout=write_end, stderr=subprocess.PIPE, env=env, timeout=60,
    )
    os.close(write_end)
    stderr = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 141, stderr
    assert "Traceback" not in stderr and "Exception ignored" not in stderr, stderr
    assert "unexpected" not in stderr, stderr
