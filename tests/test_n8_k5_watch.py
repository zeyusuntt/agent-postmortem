"""N8b K5 (docs/DESIGN.md 4.12, 4.13.8, 5.3b; .reviews/v0.3/N8-plan.md rev 3): watch's START and
OK lines, the poll order, and DONE from lifecycle terminal evidence.

Every test drives `watch` through `cli.main(["watch", ...])` against a run directory laid out
the standard way (`<root>/<slug>/<session>/subagents/workflows/<run id>`, the parent transcript
beside the session directory), with `watch._now` and `watch._sleep` patched: `_sleep` runs the
next scripted action (append to the journal or the parent transcript, snapshot the output so
far) and then advances the fake clock, so a test sees exactly what each poll printed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import cli, reader
from tests.test_field_v03 import (
    _aid,
    _assistant_tool,
    _envelope,
    _notification_entry,
    _parent_path,
    _wf_dir,
    _workflow_launch,
    _workflow_launch_result,
)
from tests.test_watch import (
    _agent_form,
    _append_lines,
    _assistant_text,
    _Clock,
    _journal_line,
    _kill_sentinel,
    _meta,
    _patch_clock,
    _patch_scripted_sleep,
    _result,
    _started,
    _transcript_path,
    _user_text,
    _write_lines,
    _write_transcript,
)

T0 = datetime(2026, 2, 1, tzinfo=timezone.utc)
JT = "2026-02-01T00:00:00.000Z"
OLD_LAUNCH_LINE = 1


class Scn:
    """One workflow run in the standard layout, with its parent transcript."""

    def __init__(self, tmp_path: Path, suffix: str, *, journal_exists: bool = True) -> None:
        root = tmp_path / "root"
        self.run_id = f"wf_00000000-{suffix}"
        self.run_dir = _wf_dir(root, self.run_id)
        self.run_dir.mkdir(parents=True)
        self.parent = _parent_path(root)
        self.journal = self.run_dir / "journal.jsonl"
        self.clock = _Clock()
        self.n = 0
        if journal_exists:
            self.journal.write_text("", encoding="utf-8")

    # --- builders -------------------------------------------------------------------------

    def agent(self, *, kill: bool = False) -> str:
        self.n += 1
        agent_id = _aid(0x800 + self.n + 0x40 * int(self.run_id[-1], 16))
        _meta(self.run_dir, agent_id)
        lines = [_user_text(JT, "Work."), _assistant_text("2026-02-01T00:00:05.000Z", "Working.")]
        if kill:
            lines.append(_kill_sentinel("2026-02-01T00:06:00.000Z"))
        _write_transcript(_transcript_path(self.run_dir, agent_id), lines)
        return agent_id

    def parent_write(self, entries: list[dict]) -> None:
        self.parent.parent.mkdir(parents=True, exist_ok=True)
        self.parent.write_text(
            "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8",
        )

    def parent_append(self, entries: list[dict]) -> None:
        _append_lines(self.parent, [json.dumps(entry) for entry in entries])

    def launch(self, status: str = "async_launched", tool: str = "toolu_A", task: str = "tA"):
        return [
            _workflow_launch(tool, T0),
            _workflow_launch_result(tool, T0 + timedelta(seconds=1), self.run_id, task, status),
        ]

    def notify(self, status: str, tool: str = "toolu_A", task: str = "tA") -> dict:
        return _notification_entry(T0 + timedelta(seconds=30), _envelope(task, tool, status))

    def taskstop(self, task: str = "tA") -> dict:
        return _assistant_tool("toolu_stop", "TaskStop", {"task_id": task}, T0 + timedelta(seconds=30))


def run_watch(monkeypatch, capsys, scn: Scn, *, actions=(), extra=(), json_mode=False, interval=1):
    _patch_clock(monkeypatch, scn.clock)
    _patch_scripted_sleep(monkeypatch, list(actions), scn.clock)
    argv = ["watch", str(scn.run_dir), "--interval", str(interval), *extra]
    if json_mode:
        argv.append("--json")
    code = cli.main(argv)
    return code, capsys.readouterr().out


def snapshot(capsys, sink: list) -> None:
    sink.append(capsys.readouterr().out)


def snapshots(capsys, count: int, sink: list) -> list:
    return [lambda: snapshot(capsys, sink) for _ in range(count)]


def events(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def lines_of(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.strip()]


LIMITED = ("--max-hours", "0.05")
FOREVER = ("--done-grace", "3600", "--max-hours", "0.01")  # ends in LIMIT after about 36 s


# =============================================================================================
# START
# =============================================================================================


def test_k5_start_is_the_first_byte_on_a_late_journal_text(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8c0", journal_exists=False)
    agent = scn.agent()

    def create_journal():
        _write_lines(scn.journal, [_started(JT, "v2:x", agent, "impl"), _result(JT, "v2:x", agent)])

    code, out = run_watch(
        monkeypatch, capsys, scn, actions=[create_journal], extra=("--done-grace", "0", *LIMITED),
    )
    assert out.startswith(f"START  {scn.run_id}: 1 agents started so far, 1 attempts.\n")
    assert [line.split()[0] for line in lines_of(out)] == ["START", "OK", "DONE"]
    assert code == 0


def test_k5_start_is_the_first_event_on_a_late_journal_json(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8c1", journal_exists=False)
    agent = scn.agent()

    def create_journal():
        _write_lines(scn.journal, [_started(JT, "v2:x", agent, "impl"), _result(JT, "v2:x", agent)])

    _code, out = run_watch(
        monkeypatch, capsys, scn, actions=[create_journal], extra=("--done-grace", "0", *LIMITED),
        json_mode=True,
    )
    assert out.startswith('{"event": "START", "run": "wf_00000000-8c1", "agents": 1, "attempts": 1}\n')
    assert [event["event"] for event in events(out)] == ["START", "OK", "DONE"]


@pytest.mark.parametrize("json_mode", [False, True])
def test_k5_start_comes_before_malformed_on_the_first_poll(tmp_path, monkeypatch, capsys, json_mode):
    scn = Scn(tmp_path, "8c2")
    agent = scn.agent()
    _write_lines(scn.journal, [
        '"not an object"', _started(JT, "v2:x", agent, "impl"), _result(JT, "v2:x", agent),
    ])
    code, out = run_watch(
        monkeypatch, capsys, scn, extra=("--done-grace", "0", *LIMITED), json_mode=json_mode,
    )
    if json_mode:
        kinds = [event["event"] for event in events(out)]
        assert out.startswith('{"event": "START"')
    else:
        kinds = [line.split()[0] for line in lines_of(out)]
        assert out.startswith(f"START  {scn.run_id}: 1 agents started so far, 1 attempts.\n")
    assert kinds[:2] == ["START", "MALFORMED"]
    assert kinds[-1] == "LIMIT"  # DONE is impossible once the journal carries a malformed line
    assert code == 4


@pytest.mark.parametrize("json_mode", [False, True])
def test_k5_start_comes_before_an_actionable_event_on_the_first_poll(
    tmp_path, monkeypatch, capsys, json_mode,
):
    scn = Scn(tmp_path, "8c3")
    a1, a2 = scn.agent(kill=True), scn.agent()
    _write_lines(scn.journal, [
        _started(JT, "v2:x", a1, "impl"), _started(JT, "v2:x", a2, "impl"),
    ])
    _code, out = run_watch(monkeypatch, capsys, scn, extra=FOREVER, json_mode=json_mode)
    if json_mode:
        first = events(out)[:2]
        assert first[0] == {"event": "START", "run": scn.run_id, "agents": 1, "attempts": 2}
        assert first[1]["event"] == "KILL"
    else:
        first = lines_of(out)[:2]
        assert first[0] == f"START  {scn.run_id}: 1 agents started so far, 2 attempts."
        assert first[1].startswith("KILL")


def test_k5_start_is_printed_once_and_counts_distinct_keys(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8c4")
    a1, a2, a3 = scn.agent(), scn.agent(), scn.agent()
    _write_lines(scn.journal, [
        _started(JT, "v2:a", a1, "x"), _started(JT, "v2:b", a2, "y"), _started(JT, "v2:a", a3, "x"),
    ])
    _code, out = run_watch(monkeypatch, capsys, scn, extra=FOREVER)
    starts = [line for line in lines_of(out) if line.startswith("START")]
    assert starts == [f"START  {scn.run_id}: 2 agents started so far, 3 attempts."]


def test_k5_start_is_printed_for_a_journal_with_no_started_event(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8c5")
    _write_lines(scn.journal, [_journal_line(type="launched", timestamp=JT)])
    _code, out = run_watch(monkeypatch, capsys, scn, extra=FOREVER)
    assert lines_of(out)[0] == f"START  {scn.run_id}: 0 agents started so far, 0 attempts."


# =============================================================================================
# OK
# =============================================================================================


def test_k5_ok_line_and_json_shape(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8c6")
    agent = scn.agent()
    _write_lines(scn.journal, [_started(JT, "v2:x", agent, "impl"), _result(JT, "v2:x", agent)])
    _code, out = run_watch(monkeypatch, capsys, scn, extra=("--done-grace", "0", *LIMITED))
    assert f"OK     {_agent_form(agent)} attempt 1 returned a result." in lines_of(out)

    scn2 = Scn(tmp_path / "again", "8c6")
    agent2 = scn2.agent()
    _write_lines(scn2.journal, [_started(JT, "v2:x", agent2, "impl"), _result(JT, "v2:x", agent2)])
    _code, out2 = run_watch(
        monkeypatch, capsys, scn2, extra=("--done-grace", "0", *LIMITED), json_mode=True,
    )
    ok = [event for event in events(out2) if event["event"] == "OK"]
    assert ok == [{"event": "OK", "agent": _agent_form(agent2), "attempt": 1}]


def test_k5_ok_prints_once_per_result_across_polls(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8c7")
    a1, a2 = scn.agent(), scn.agent()
    _write_lines(scn.journal, [
        _started(JT, "v2:a", a1, "x"), _result(JT, "v2:a", a1),
        _started(JT, "v2:b", a2, "y"), _result(JT, "v2:b", a2),
    ])
    _code, out = run_watch(monkeypatch, capsys, scn, extra=FOREVER)
    assert [line for line in lines_of(out) if line.startswith("OK")] == [
        f"OK     {_agent_form(a1)} attempt 1 returned a result.",
        f"OK     {_agent_form(a2)} attempt 1 returned a result.",
    ]


def test_k5_ok_for_a_result_that_arrives_later(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8c8")
    agent = scn.agent()
    _write_lines(scn.journal, [_started(JT, "v2:x", agent, "impl")])

    def result_arrives():
        _append_lines(scn.journal, [_result(JT, "v2:x", agent)])

    _code, out = run_watch(
        monkeypatch, capsys, scn, actions=[lambda: None, result_arrives], extra=FOREVER,
    )
    assert [line.split()[0] for line in lines_of(out)][:2] == ["START", "OK"]


def _kill_then_completed(scn: Scn) -> tuple[str, str]:
    a1, a2 = scn.agent(kill=True), scn.agent()
    _write_lines(scn.journal, [
        _started(JT, "v2:x", a1, "impl"), _started(JT, "v2:x", a2, "impl"),
        _result(JT, "v2:x", a2),
    ])
    return a1, a2


def test_k5_ok_is_rate_limited_per_agent_and_retried(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8c9")
    _a1, a2 = _kill_then_completed(scn)
    snaps: list[str] = []
    _code, out = run_watch(
        monkeypatch, capsys, scn, actions=snapshots(capsys, 2, snaps),
        extra=("--done-grace", "3600", "--max-hours", "0.02"), interval=30,
    )
    # poll 1 printed the KILL for the key, so the same key's OK waits out the 60 s limit
    assert "KILL" in snaps[0] and "OK" not in snaps[0]
    assert "OK" not in snaps[1]
    final = snaps[1] + out
    assert final.count(f"OK     {_agent_form(a2)} attempt 2 returned a result.") == 1


def test_k5_ok_is_forced_on_the_twelfth_unresolved_poll(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8ca")
    _kill_then_completed(scn)
    snaps: list[str] = []
    _code, out = run_watch(
        monkeypatch, capsys, scn, actions=snapshots(capsys, 14, snaps),
        extra=("--done-grace", "3600", "--max-hours", "0.05"),
    )
    assert all("OK" not in snap for snap in snaps[:11])
    assert "OK" in snaps[11]  # poll 12: the bounded retry forces it through the rate limit
    assert ("".join(snaps[12:]) + out).count("OK ") == 0  # and it is never printed again


# =============================================================================================
# ordering in one poll
# =============================================================================================


def test_k5_order_in_one_poll_is_kill_failed_ok(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8cb")
    k1a, k1b = scn.agent(kill=True), scn.agent()
    k2, k3 = scn.agent(), scn.agent()
    # journal order is deliberately the reverse of the print order
    _write_lines(scn.journal, [
        _started(JT, "v2:k3", k3, "c"), _result(JT, "v2:k3", k3),
        _started(JT, "v2:k2", k2, "b"), _journal_line(type="failed", timestamp=JT, key="v2:k2", agentId=k2),
        _started(JT, "v2:k1", k1a, "a"), _started(JT, "v2:k1", k1b, "a"),
    ])
    _code, out = run_watch(monkeypatch, capsys, scn, extra=FOREVER)
    words = [line.split()[0] for line in lines_of(out) if line[0] != " "]
    assert words[:4] == ["START", "KILL", "FAILED", "OK"]


def test_k5_ok_comes_before_done_in_the_same_poll(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8cc")
    agent = scn.agent()
    _write_lines(scn.journal, [_started(JT, "v2:x", agent, "impl"), _result(JT, "v2:x", agent)])
    _code, out = run_watch(monkeypatch, capsys, scn, extra=("--done-grace", "0", *LIMITED))
    assert [line.split()[0] for line in lines_of(out)] == ["START", "OK", "DONE"]


# =============================================================================================
# DONE from lifecycle terminal evidence
# =============================================================================================


def _open_run(scn: Scn) -> str:
    agent = scn.agent()
    _write_lines(scn.journal, [_started(JT, "v2:x", agent, "impl")])
    return agent


@pytest.mark.parametrize("kind,code", [
    ("notification", 0),
    ("stopped_notification", None),
    ("taskstop", None),
])
def test_k5_done_at_once_on_parent_evidence_that_arrives_later(
    tmp_path, monkeypatch, capsys, kind, code,
):
    scn = Scn(tmp_path, "8cd")
    _open_run(scn)
    scn.parent_write(scn.launch())
    evidence = {
        "notification": scn.notify("completed"),
        "stopped_notification": scn.notify("killed"),
        "taskstop": scn.taskstop(),
    }[kind]
    snaps: list[str] = []
    _code, out = run_watch(
        monkeypatch, capsys, scn,
        actions=[
            lambda: snapshot(capsys, snaps),
            lambda: (scn.parent_append([evidence]), snapshot(capsys, snaps)),
        ],
        extra=("--done-grace", "3600", "--max-hours", "0.5"), interval=5,
    )
    assert "DONE" not in snaps[0]
    assert "DONE" in out  # poll 2: no grace wait, the journal is still open
    if code is not None:
        assert _code == code


def test_k5_done_at_once_on_a_completed_synchronous_launch_result(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8ce")
    _open_run(scn)
    scn.parent_write(scn.launch(status="completed"))
    code, out = run_watch(monkeypatch, capsys, scn, extra=("--done-grace", "3600", *LIMITED))
    assert [line.split()[0] for line in lines_of(out)] == ["START", "DONE"]
    assert code == 0


def test_k5_a_failed_synchronous_launch_result_is_done_with_exit_3(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8cf")
    _open_run(scn)
    scn.parent_write(scn.launch(status="failed"))
    code, out = run_watch(monkeypatch, capsys, scn, extra=("--done-grace", "3600", *LIMITED))
    done = lines_of(out)[-1]
    assert done.startswith("DONE")
    assert "FAILED" in done
    assert code == 3


def test_k5_a_journal_resolved_run_waits_out_the_grace_period(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8d0")
    agent = scn.agent()
    _write_lines(scn.journal, [_started(JT, "v2:x", agent, "impl"), _result(JT, "v2:x", agent)])
    scn.parent_write(scn.launch())  # launched, no terminal evidence yet
    snaps: list[str] = []
    code, out = run_watch(
        monkeypatch, capsys, scn, actions=snapshots(capsys, 4, snaps),
        extra=("--done-grace", "30", "--max-hours", "1"), interval=10,
    )
    assert len(snaps) == 3 and all("DONE" not in snap for snap in snaps)  # t = 0, 10, 20
    assert "DONE" in out  # t = 30
    assert code == 0


def test_k5_never_done_on_an_unknown_terminal_while_the_journal_is_open(
    tmp_path, monkeypatch, capsys,
):
    scn = Scn(tmp_path, "8d1")
    _open_run(scn)
    scn.parent_write([*scn.launch(), scn.notify("exploded")])
    code, out = run_watch(monkeypatch, capsys, scn, extra=FOREVER)
    assert "DONE" not in out
    assert lines_of(out)[-1].startswith("LIMIT")
    assert code == 4


def test_k5_never_done_while_nothing_says_the_run_ended(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8d2")
    _open_run(scn)
    scn.parent_write(scn.launch())
    code, out = run_watch(monkeypatch, capsys, scn, extra=FOREVER)
    assert "DONE" not in out
    assert code == 4


def test_k5_malformed_journal_still_makes_done_impossible_with_terminal_evidence(
    tmp_path, monkeypatch, capsys,
):
    scn = Scn(tmp_path, "8d3")
    agent = scn.agent()
    _write_lines(scn.journal, ['"not an object"', _started(JT, "v2:x", agent, "impl")])
    scn.parent_write([*scn.launch(), scn.notify("completed")])
    code, out = run_watch(monkeypatch, capsys, scn, extra=("--done-grace", "0", *LIMITED))
    assert "DONE" not in out
    assert lines_of(out)[-1] == "LIMIT  journal has malformed lines; run state unknown."
    assert code == 4


# =============================================================================================
# no dropped events
# =============================================================================================


def test_k5_a_kill_then_completed_result_with_parent_evidence_drops_nothing(
    tmp_path, monkeypatch, capsys,
):
    scn = Scn(tmp_path, "8d4")
    a1, a2 = _kill_then_completed(scn)
    scn.parent_write([*scn.launch(), scn.notify("completed")])
    code, out = run_watch(monkeypatch, capsys, scn, extra=("--done-grace", "3600", *LIMITED))
    words = [line.split()[0] for line in lines_of(out) if line[0] != " "]
    assert words == ["START", "KILL", "OK", "DONE"]
    assert f"OK     {_agent_form(a2)} attempt 2 returned a result." in lines_of(out)
    assert code == 0


def test_k5_a_pending_ok_after_a_failed_attempt_is_not_dropped_by_an_immediate_done(
    tmp_path, monkeypatch, capsys,
):
    scn = Scn(tmp_path, "8d5")
    a1, a2 = scn.agent(), scn.agent()
    _write_lines(scn.journal, [
        _started(JT, "v2:x", a1, "impl"),
        _journal_line(type="failed", timestamp=JT, key="v2:x", agentId=a1),
        _started(JT, "v2:x", a2, "impl"), _result(JT, "v2:x", a2),
    ])
    scn.parent_write([*scn.launch(), scn.notify("completed")])
    _code, out = run_watch(monkeypatch, capsys, scn, extra=("--done-grace", "3600", *LIMITED))
    words = [line.split()[0] for line in lines_of(out) if line[0] != " "]
    assert words == ["START", "FAILED", "OK", "DONE"]


# =============================================================================================
# operation counts
# =============================================================================================


class _Counter:
    def __init__(self, monkeypatch, scn: Scn) -> None:
        self.refreshes = 0
        self.journal_lines = 0
        original = reader.ParentIndex._refresh
        counter = self

        def counting_refresh(index):
            counter.refreshes += 1
            return original(index)

        monkeypatch.setattr(reader.ParentIndex, "_refresh", counting_refresh)
        from agent_postmortem import watch

        original_read = watch._read_new_journal_lines

        def counting_read(path, offset):
            lines, new_offset, truncated = original_read(path, offset)
            counter.journal_lines += len(lines)
            return lines, new_offset, truncated

        monkeypatch.setattr(watch, "_read_new_journal_lines", counting_read)
        self.scn = scn

    @property
    def index(self):
        index = reader.ParentIndex._cache.get(self.scn.parent)
        assert index is not None, "watch never read the parent transcript"
        return index


def test_k5_unchanged_polls_parse_no_journal_line_and_read_no_parent_byte(
    tmp_path, monkeypatch, capsys,
):
    scn = Scn(tmp_path, "8d6")
    _open_run(scn)
    scn.parent_write(scn.launch())
    counter = _Counter(monkeypatch, scn)
    _code, _out = run_watch(monkeypatch, capsys, scn, extra=FOREVER)
    assert counter.journal_lines == 1  # the one started line, parsed once across every poll
    # the parent index was refreshed on the first poll only (the loop's own LIMIT path loads no
    # run); every later poll found its (inode, size, mtime, ctime) unchanged
    assert counter.refreshes == 1
    assert counter.index.rebuilds == 0


def test_k5_a_parent_append_parses_exactly_the_appended_lines(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8d7")
    _open_run(scn)
    scn.parent_write(scn.launch())
    counter = _Counter(monkeypatch, scn)
    parsed: list[int] = []

    def remember():
        parsed.append(counter.index.lines_parsed)

    def append_two():
        scn.parent_append([
            _assistant_tool("toolu_x", "Read", {}, T0 + timedelta(seconds=5)),
            _assistant_tool("toolu_y", "Read", {}, T0 + timedelta(seconds=6)),
        ])

    _code, _out = run_watch(
        monkeypatch, capsys, scn, actions=[remember, append_two, remember], extra=FOREVER,
    )
    # the bytes before the append are rehashed (the N1 prefix check) but not parsed again
    assert parsed[1] - parsed[0] == 2
    assert counter.refreshes == 2
    assert counter.index.rebuilds == 0


def test_k5_a_same_size_parent_rewrite_refreshes_and_rebuilds(tmp_path, monkeypatch, capsys):
    scn = Scn(tmp_path, "8d8")
    _open_run(scn)
    scn.parent_write(scn.launch())
    counter = _Counter(monkeypatch, scn)

    def rewrite_same_size():
        text = scn.parent.read_text(encoding="utf-8")
        assert "2026-02-01T00:00:00" in text
        changed = text.replace("2026-02-01T00:00:00", "2026-02-01T00:00:09", 1)
        assert len(changed) == len(text)
        scn.parent.write_text(changed, encoding="utf-8")
        stat = scn.parent.stat()
        os.utime(scn.parent, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    _code, _out = run_watch(
        monkeypatch, capsys, scn, actions=[rewrite_same_size], extra=FOREVER,
    )
    assert counter.refreshes == 2
    assert counter.index.rebuilds == 1


def test_k5_a_transient_parent_read_failure_is_retried_on_the_next_unchanged_poll(
    tmp_path, monkeypatch, capsys,
):
    scn = Scn(tmp_path, "8d9")
    _open_run(scn)
    scn.parent_write([*scn.launch(), scn.notify("completed")])
    original = reader.ParentIndex._parse_increment
    state = {"failed": False}

    def flaky(index, size):
        if not state["failed"]:
            state["failed"] = True
            raise OSError("transient")
        return original(index, size)

    monkeypatch.setattr(reader.ParentIndex, "_parse_increment", flaky)
    snaps: list[str] = []
    code, out = run_watch(
        monkeypatch, capsys, scn, actions=snapshots(capsys, 2, snaps),
        extra=("--done-grace", "3600", "--max-hours", "0.5"), interval=5,
    )
    assert state["failed"]
    assert "DONE" not in snaps[0]  # poll 1: the parent could not be read, so nothing is known
    assert len(snaps) == 1 and "DONE" in out  # poll 2: unchanged file, read again, DONE
    assert code == 0


# =============================================================================================
# journal epochs: RESET
# =============================================================================================


def test_k5_reset_with_an_old_generation_terminal_prints_no_done_and_a_newer_one_finishes(
    tmp_path, monkeypatch, capsys,
):
    scn = Scn(tmp_path, "8da")
    a1, a2 = scn.agent(kill=True), scn.agent()
    # epoch 1: key x was killed and restarted, and the restart returned a result (its OK is
    # pending behind the KILL's rate limit); a second key is open, so the journal is long
    _write_lines(scn.journal, [
        _started(JT, "v2:x", a1, "impl"), _started(JT, "v2:x", a2, "impl"),
        _result(JT, "v2:x", a2), _started(JT, "v2:y", scn.agent(), "other"),
    ])
    scn.parent_write(scn.launch())
    fresh = scn.agent()
    snaps: list[str] = []

    def replace_journal_and_finish_the_old_generation():
        snapshot(capsys, snaps)  # poll 1
        # the journal is replaced by a shorter fresh run that reuses the directory and run id,
        # while the old generation's terminal notification lands in the parent
        _write_lines(scn.journal, [_started(JT, "v2:z", fresh, "fresh")])
        scn.parent_append([scn.notify("completed")])

    def newer_generation_finishes():
        snapshot(capsys, snaps)  # poll 2: RESET and the open replacement attempt
        scn.parent_append([
            *scn.launch("async_launched", tool="toolu_B", task="tB"),
            scn.notify("completed", tool="toolu_B", task="tB"),
        ])

    code, out = run_watch(
        monkeypatch, capsys, scn,
        actions=[replace_journal_and_finish_the_old_generation, newer_generation_finishes],
        extra=("--done-grace", "3600", "--max-hours", "0.5"), interval=5,
    )
    assert "KILL" in snaps[0]
    assert "RESET" in snaps[1]
    assert "DONE" not in snaps[1]  # the old generation's terminal never overrides the open attempt
    assert "OK" not in snaps[1] and "OK" not in out  # the old pending OK died with the epoch
    assert "DONE" in out  # poll 3: a generation newer than the ended epoch's watermark
    assert (snaps[0] + snaps[1] + out).count("START") == 1
    assert code == 0


def test_k5_after_a_reset_the_journal_resolved_path_still_finishes_after_the_grace(
    tmp_path, monkeypatch, capsys,
):
    scn = Scn(tmp_path, "8db")
    old = scn.agent()
    _write_lines(scn.journal, [
        _started(JT, "v2:x", old, "impl"), _started(JT, "v2:y", scn.agent(), "other"),
    ])
    scn.parent_write(scn.launch())
    fresh = scn.agent()

    def replace_and_resolve():
        _write_lines(scn.journal, [_started(JT, "v2:z", fresh, "fresh"), _result(JT, "v2:z", fresh)])
        scn.parent_append([scn.notify("completed")])  # the old generation's terminal

    code, out = run_watch(
        monkeypatch, capsys, scn, actions=[replace_and_resolve],
        extra=("--done-grace", "10", "--max-hours", "0.5"), interval=5,
    )
    words = [line.split()[0] for line in lines_of(out) if line[0] != " "]
    assert words.count("RESET") == 1
    assert words[-1] == "DONE"
    assert code == 0


def test_k5_a_pending_failed_event_is_not_dropped_by_an_immediate_done(
    tmp_path, monkeypatch, capsys,
):
    """The KILL of a key puts its same-poll FAILED behind the per-agent rate limit; the parent
    terminal makes DONE eligible at once; DONE still waits for the FAILED."""
    scn = Scn(tmp_path, "8dc")
    a1, a2 = scn.agent(kill=True), scn.agent()
    _write_lines(scn.journal, [
        _started(JT, "v2:x", a1, "impl"), _started(JT, "v2:x", a2, "impl"),
        _journal_line(type="failed", timestamp=JT, key="v2:x", agentId=a2),
    ])
    scn.parent_write([*scn.launch(), scn.notify("completed")])
    _code, out = run_watch(monkeypatch, capsys, scn, extra=("--done-grace", "3600", *LIMITED))
    words = [line.split()[0] for line in lines_of(out) if line[0] != " "]
    assert words == ["START", "KILL", "FAILED", "DONE"]


def test_k5_a_pending_kill_is_not_dropped_by_an_immediate_done(tmp_path, monkeypatch, capsys):
    """Two kills of one key in one poll: the second waits behind the first's rate limit."""
    scn = Scn(tmp_path, "8dd")
    a1, a2, a3 = scn.agent(kill=True), scn.agent(kill=True), scn.agent()
    _write_lines(scn.journal, [
        _started(JT, "v2:x", a1, "impl"), _started(JT, "v2:x", a2, "impl"),
        _started(JT, "v2:x", a3, "impl"),
    ])
    scn.parent_write([*scn.launch(), scn.notify("completed")])
    _code, out = run_watch(monkeypatch, capsys, scn, extra=("--done-grace", "3600", *LIMITED))
    words = [line.split()[0] for line in lines_of(out) if line[0] != " "]
    assert words == ["START", "KILL", "KILL", "DONE"]
    kills = [line for line in lines_of(out) if line.startswith("KILL")]
    assert "attempt 1 " in kills[0] and "attempt 2 " in kills[1]


def test_k5_ok_sets_the_per_agent_rate_limit_for_the_next_event(tmp_path, monkeypatch, capsys):
    """After an OK for a key, that key's QUIET (a later attempt gone quiet) waits out the 60 s."""
    scn = Scn(tmp_path, "8de")
    a1, a2 = scn.agent(), scn.agent()
    old = scn.clock.now - 1000
    os.utime(_transcript_path(scn.run_dir, a2), (old, old))
    _write_lines(scn.journal, [
        _started(JT, "v2:x", a1, "impl"), _result(JT, "v2:x", a1),
        _started(JT, "v2:x", a2, "impl"),
    ])
    snaps: list[str] = []
    _code, out = run_watch(
        monkeypatch, capsys, scn, actions=snapshots(capsys, 3, snaps),
        extra=("--done-grace", "3600", "--max-hours", "0.03"), interval=20,
    )
    assert "OK" in snaps[0]
    assert all("QUIET" not in snap for snap in snaps)  # t = 0, 20, 40: inside the 60 s
    assert "QUIET" in out  # t = 60


def test_k5_a_result_in_a_replacement_journal_prints_ok_again_for_the_same_key_and_ordinal(
    tmp_path, monkeypatch, capsys,
):
    scn = Scn(tmp_path, "8df")
    old, fresh = scn.agent(), scn.agent()
    _write_lines(scn.journal, [
        _started(JT, "v2:x", old, "impl"), _result(JT, "v2:x", old),
        _started(JT, "v2:y", scn.agent(), "other"),
    ])

    def replace():
        _write_lines(scn.journal, [_started(JT, "v2:x", fresh, "impl"), _result(JT, "v2:x", fresh)])

    _code, out = run_watch(
        monkeypatch, capsys, scn, actions=[replace],
        extra=("--done-grace", "3600", "--max-hours", "0.01"),
    )
    oks = [line for line in lines_of(out) if line.startswith("OK")]
    assert oks == [
        f"OK     {_agent_form(old)} attempt 1 returned a result.",
        f"OK     {_agent_form(fresh)} attempt 1 returned a result.",
    ]
    assert out.count("RESET") == 1


def test_k5_never_done_on_an_unknown_terminal_even_when_the_journal_is_resolved(
    tmp_path, monkeypatch, capsys,
):
    scn = Scn(tmp_path, "8e1")
    agent = scn.agent()
    _write_lines(scn.journal, [_started(JT, "v2:x", agent, "impl"), _result(JT, "v2:x", agent)])
    scn.parent_write([*scn.launch(), scn.notify("exploded")])
    code, out = run_watch(monkeypatch, capsys, scn, extra=("--done-grace", "0", *LIMITED))
    assert "DONE" not in out
    assert lines_of(out)[-1].startswith("LIMIT")
    assert code == 4
