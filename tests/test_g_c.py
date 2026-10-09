"""G-C (fix round for G-B1, docs/DESIGN.md 4.1, 4.13.1, 7.2): GC-1 a transcript cut inside a
multibyte character is a half-written line, GC-2 the independent return implementation agrees
with the reader on every builder row, GC-3 the rows the G-B1 mutation review found missing.

Everything is built in tmp_path; ids are built in code.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from agent_postmortem import hooks_cli, ledger, reader, rescue
from agent_postmortem.cli import main
from tests import g_independent_return as ind
from tests import test_g_a1_return, test_g_return
from tests.g_corpus_support import collect_inv6
from tests.g_helpers import (
    SESSION,
    Session,
    X,
    Y,
    Z,
    async_launch,
    child_entries,
    note,
    result,
    send,
    sync_launch,
    use,
)
from tests.n5c2_helpers import run_cli

RET = ("returned", "none")
SENTINEL = "[Request interrupted by user]"
TORN_TEXT = "完成"  # two CJK characters, three bytes each


def torn_bytes() -> bytes:
    """A JSON line cut one byte short of the end of a 3-byte character, no newline."""
    line = json.dumps(
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": TORN_TEXT}], "usage": {}}},
        ensure_ascii=False,
    )
    head = line[: line.index(TORN_TEXT) + len(TORN_TEXT)].encode("utf-8")
    return head[:-1]


def append_bytes(path: Path, data: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(data)


def three_children(tmp_path) -> Session:
    s = Session(tmp_path)
    s.main(sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Y, 3), sync_launch("toolu_3", Z, 6))
    s.child(X).child(Y).child(Z)
    s.build()
    return s


def states_of(run) -> dict:
    return {a.agent_id: (a.return_state, a.return_reason) for a in run.attempts}



# --- GC-1: the reader --------------------------------------------------------------------------


def test_the_torn_bytes_really_are_not_utf8():
    with pytest.raises(UnicodeDecodeError):
        torn_bytes().decode("utf-8")
    assert not torn_bytes().endswith(b"\n")


def test_a_torn_last_line_of_one_transcript_leaves_every_child_returned(tmp_path, capsys):
    s = three_children(tmp_path)
    append_bytes(s.child_path(Z), torn_bytes())
    run = reader.load_session(s.main_path)
    assert states_of(run) == {X: RET, Y: RET, Z: RET}
    notes = [d for d in run.diagnostics if d.startswith(f"agent-{Z}.jsonl:3: ")]
    assert len(notes) == 1
    assert TORN_TEXT[:1] not in notes[0] and "assistant" not in notes[0]
    assert not any("unreadable" in d for d in run.diagnostics)
    _code, text, _err = run_cli(capsys, ["run", str(s.main_path), "--root", str(s.root)])
    assert "possibly running" not in text


def test_a_torn_last_line_counts_as_one_undecodable_line(tmp_path):
    s = three_children(tmp_path)
    append_bytes(s.child_path(Z), torn_bytes())
    diagnostics: list[str] = []
    state = ledger.TranscriptState()
    out = reader._read_transcript(s.child_path(Z), diagnostics, state=state)
    total_lines, bad_lines = out[3], out[4]
    assert (total_lines, bad_lines) == (3, 1)
    assert state.undecodable is True and state.unreadable is False
    tracked = reader._read_transcript_tracked(s.child_path(Z), [], state=ledger.TranscriptState())
    assert tracked[3] is False


def test_an_ascii_half_line_is_the_same_shape(tmp_path):
    s = three_children(tmp_path)
    append_bytes(s.child_path(Z), b'{"type": "assistant", "message": {"role"')
    diagnostics: list[str] = []
    state = ledger.TranscriptState()
    out = reader._read_transcript(s.child_path(Z), diagnostics, state=state)
    assert (out[3], out[4]) == (3, 1)
    assert state.undecodable is True and state.unreadable is False
    assert states_of(reader.load_session(s.main_path)) == {X: RET, Y: RET, Z: RET}


def test_the_same_bytes_followed_by_a_line_keep_the_transcript_unreadable(tmp_path):
    s = three_children(tmp_path)
    tail = json.dumps(child_entries(Z)[1]).encode("utf-8") + b"\n"
    append_bytes(s.child_path(Z), torn_bytes() + b"\n" + tail)
    run = reader.load_session(s.main_path)
    assert any(d.startswith(f"agent-{Z}.jsonl: unreadable: ") for d in run.diagnostics)
    assert states_of(run) == {
        X: ("unknown", "parent_unreadable"), Y: ("unknown", "parent_unreadable"),
        Z: ("unknown", "parent_unreadable"),
    }


def test_the_torn_bytes_followed_only_by_a_newline_keep_the_transcript_unreadable(tmp_path):
    s = three_children(tmp_path)
    append_bytes(s.child_path(Z), torn_bytes() + b"\n")
    run = reader.load_session(s.main_path)
    assert states_of(run)[Z] == ("unknown", "parent_unreadable")


def test_an_undecodable_byte_in_the_middle_keeps_the_transcript_unreadable(tmp_path):
    s = three_children(tmp_path)
    path = s.child_path(Z)
    first, second = path.read_bytes().split(b"\n")[:2]
    path.write_bytes(first + b"\n" + b'{"type": "x", "t": "\xe5\xae"}\n' + second + b"\n")
    run = reader.load_session(s.main_path)
    assert states_of(run)[Z] == ("unknown", "parent_unreadable")


def test_a_torn_last_line_of_the_main_transcript_is_a_half_line(tmp_path):
    s = three_children(tmp_path)
    append_bytes(s.main_path, torn_bytes())
    run = reader.load_session(s.main_path)
    assert states_of(run) == {X: RET, Y: RET, Z: RET}


# --- GC-1: the other read sites ------------------------------------------------------------------


def test_the_fork_scan_is_unchanged_by_a_torn_last_line(tmp_path):
    """The scan stops at the first own entry, so a tail behind it is never decoded; with no own
    entry the answer is (0, False) with or without the tail."""
    own = SESSION
    foreign = "other-session"
    path = tmp_path / f"{own}.jsonl"
    lines = [{"type": "user", "sessionId": foreign}, {"type": "user", "sessionId": foreign}]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    append_bytes(path, b'{"sessionId": "' + torn_bytes())
    assert reader._fork_scan(path) == (0, False)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "user", "sessionId": own}) + "\n")
    append_bytes(path, b'{"sessionId": "' + torn_bytes())
    assert reader._fork_scan(path) == (2, True)


def test_the_fork_scan_still_gives_up_on_a_byte_in_the_middle(tmp_path):
    path = tmp_path / f"{SESSION}.jsonl"
    path.write_bytes(b'{"sessionId": "\xe5\xae"}\n{"sessionId": "b"}\n')
    assert reader._fork_scan(path) == (0, False)


def test_a_file_of_one_torn_line_is_no_session(tmp_path):
    path = tmp_path / f"{SESSION}.jsonl"
    path.write_bytes(torn_bytes())
    assert reader._has_timestamped_conversation(path) is False
    assert reader._sniff_session_file(path) is False
    ascii_path = tmp_path / "second.jsonl"
    ascii_path.write_bytes(b'{"type": "assistant", "message": {"role"')
    assert reader._has_timestamped_conversation(ascii_path) is False
    assert reader._sniff_session_file(ascii_path) is False


def test_a_byte_in_the_middle_still_leaves_a_file_a_candidate_session(tmp_path):
    path = tmp_path / f"{SESSION}.jsonl"
    path.write_bytes(b'\xe5\xae\n{"sessionId": "b"}\n')
    assert reader._has_timestamped_conversation(path) is True
    assert reader._sniff_session_file(path) is True


def test_the_rescue_task_text_survives_a_torn_last_line(tmp_path):
    path = tmp_path / "t.jsonl"
    first = {"type": "user", "message": {"role": "user", "content": "Do the thing."}}
    path.write_text(json.dumps(first) + "\n", encoding="utf-8")
    append_bytes(path, torn_bytes())
    assert rescue._read_raw_line_text(path, 1) == "Do the thing."
    assert rescue._read_raw_line_text(path, 2) is None


def test_the_rescue_task_text_of_a_byte_in_the_middle_is_none(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b'{"type": "user", "message": {"content": "x"}}\n\xe5\xae\n')
    assert rescue._read_raw_line_text(path, 2) is None


def _write_child(path: Path, entries: list, tail: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    append_bytes(path, tail)


def sentinel_entry() -> dict:
    return {"type": "user", "message": {"role": "user", "content": SENTINEL}}


def test_the_kill_sentinel_check_treats_a_torn_tail_as_not_finished(tmp_path):
    path = tmp_path / "agent-x.jsonl"
    _write_child(path, [sentinel_entry()], torn_bytes())
    assert hooks_cli._subagent_ends_with_kill_sentinel(path) is False


def test_the_kill_sentinel_check_still_sees_a_sentinel_last_line(tmp_path):
    path = tmp_path / "agent-x.jsonl"
    _write_child(path, [sentinel_entry()], b"")
    assert hooks_cli._subagent_ends_with_kill_sentinel(path) is True


def test_the_kill_sentinel_check_treats_an_ascii_half_line_as_not_finished(tmp_path):
    path = tmp_path / "agent-x.jsonl"
    _write_child(path, [sentinel_entry()], b'{"type": "assis')
    assert hooks_cli._subagent_ends_with_kill_sentinel(path) is False


def test_the_kill_sentinel_check_never_raises_on_a_byte_in_the_middle(tmp_path):
    path = tmp_path / "agent-x.jsonl"
    path.write_bytes(b'\xe5\xae\n' + json.dumps(sentinel_entry()).encode() + b"\n")
    assert hooks_cli._subagent_ends_with_kill_sentinel(path) is False


@pytest.fixture
def hook_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def check(monkeypatch, capsys, s):
    payload = {
        "hook_event_name": "Stop", "transcript_path": str(s.main_path),
        "session_id": "s-g-c-hooks", "stop_hook_active": False,
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    main(["hook", "check"])
    return capsys.readouterr().out


def unit_state(s, child) -> dict:
    state = Path(os.environ["XDG_STATE_HOME"]) / "agent-postmortem" / "state.json"
    data = json.loads(state.read_text())
    key = hooks_cli._session_state_key(s.root, SESSION)
    return data["sessions"][key]["units"][f"subagents/agent-{child}.jsonl"]


def test_a_hook_check_analyzes_the_finished_unit_beside_a_torn_sibling(
    hook_state, monkeypatch, capsys, tmp_path,
):
    s = Session(tmp_path)
    s.main(sync_launch("toolu_1", X, 1), sync_launch("toolu_3", Z, 6))
    s.child(X).child(Z).build()
    append_bytes(s.child_path(Z), torn_bytes())
    check(monkeypatch, capsys, s)
    assert unit_state(s, X)["state"] == "analyzed"
    assert unit_state(s, Z)["state"] == "analyzed"


# --- GC-2: the independent implementation agrees with the reader -------------------------------


def _build(module, name, tmp_path):
    s = Session(tmp_path)
    module.ROWS[name](s)
    main_path = s.build()
    for child, tail in getattr(s, "tails", {}).items():
        with s.child_path(child).open("a", encoding="utf-8") as handle:
            handle.write(tail)
    return s, main_path


def _both(module, name, tmp_path):
    s, main_path = _build(module, name, tmp_path)
    unreadable = getattr(s, "unreadable", [])
    for child in unreadable:
        os.chmod(s.child_path(child), 0)
    try:
        got = states_of(reader.load_session(main_path))
        other = ind.states(main_path)
    finally:
        for child in unreadable:
            os.chmod(s.child_path(child), 0o644)
    return got, other


ROW_MODULES = (test_g_return, test_g_a1_return)
ALL_ROWS = [(m, name) for m in ROW_MODULES for name in sorted(m.ROWS)]

def test_the_row_tables_are_not_empty():
    assert len(test_g_return.ROWS) >= 99 and len(test_g_a1_return.ROWS) >= 40


def test_the_two_implementations_agree_on_every_builder_row(tmp_path):
    differences = []
    for index, (module, name) in enumerate(ALL_ROWS):
        got, other = _both(module, name, tmp_path / str(index))
        for child, pair in got.items():
            if child in other and other[child] != pair:
                differences.append((name, child, pair, other[child]))
    assert differences == []


def _codex_bad_id(tmp_path):
    s = Session(tmp_path / "bad-id")
    s.main(sync_launch("t1", X, 1), use(7, 10, "SendMessage", {"to": X, "recipient": X})).child(X)
    s.build()
    return s


def _codex_equal_instant(tmp_path):
    s = Session(tmp_path / "equal")
    s.main(sync_launch("tx", X, 1), note(4, Y, "ty", "completed")).child(X)
    s.child(X, child_entries(X), async_launch("ty", Y, 4)).child(Y, meta={"parentAgentId": X})
    s.build()
    return s


@pytest.mark.parametrize(
    "build,child,expected",
    [(_codex_bad_id, X, ("unknown", "malformed")),
     (_codex_equal_instant, Y, ("unknown", "ambiguous"))],
    ids=["non-string id", "equal instant"],
)
def test_codex_shapes_have_no_invariant_6_violation(tmp_path, build, child, expected):
    s = build(tmp_path)
    run = reader.load_session(s.main_path)
    tallies = collect_inv6(run, s.main_path)
    assert tallies["all"].checked >= 1
    assert tallies["all"].violations == 0
    assert states_of(run)[child] == expected
    assert ind.states(s.main_path)[child] == expected


# --- GC-3: rows the mutation review found missing ------------------------------------------------


def _denial_count(tmp_path, text):
    from agent_postmortem import detectors
    from tests.n4b2_helpers import result as n_result
    from tests.n4b2_helpers import use as n_use
    from tests.n4b2_helpers import workflow_run

    entries = [n_use("t1", "Bash", {"command": "ls"}), n_result("t1", text, is_error=True)]
    run = workflow_run(tmp_path, entries)
    found = [f for f in detectors.run_detectors(run) if f.detector == "permission_denied"]
    return [f.metrics["permission_rule"] for f in found]


def _rule(tool="Bash"):
    return f"Permission to use {tool} with command x has been denied."


@pytest.mark.parametrize(
    "name,text",
    [
        ("CRLF after the sentence", _rule() + "\r\n"),
        ("CRLF then more detail", _rule() + "\r\nmore detail"),
        ("a tool name starting with a digit", _rule("2Tool")),
    ],
)
def test_permission_rule_denial_rows(tmp_path, name, text):
    assert _denial_count(tmp_path, text) == [1], name


def both_states(main_path):
    """The reader's states, asserted equal to the independent side's."""
    got = states_of(reader.load_session(main_path))
    other = ind.states(main_path)
    assert {c: other[c] for c in got if c in other} == {c: got[c] for c in got if c in other}
    return got


def test_a_result_with_a_non_string_tool_use_id_and_no_bad_use_names_no_child(tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("toolu_1", X, 1))
    s.main(result(7, 5, tur={"agentId": Y}), result(None, 6, tur={"success": True, "resumedAgentId": Y}))
    s.child(X).child(Y)
    got = both_states(s.build())
    assert got[X] == RET
    assert got[Y] == ("unknown", "no_evidence")


def test_resumed_agent_id_is_the_only_link_of_a_bad_send_to_a_child(tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Y, 3))
    s.main(use(7, 10, "SendMessage", {}), result(7, 11, tur={"success": True, "resumedAgentId": X}))
    s.child(X).child(Y)
    got = both_states(s.build())
    assert got == {X: ("unknown", "malformed"), Y: RET}


def test_recipient_is_the_only_target_of_a_bad_send(tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("toolu_1", X, 1), sync_launch("toolu_2", Y, 3))
    s.main(use(7, 10, "SendMessage", {"recipient": X}))
    s.child(X).child(Y)
    got = both_states(s.build())
    assert got == {X: ("unknown", "malformed"), Y: RET}


def test_a_to_that_is_only_a_name_is_no_target_of_a_bad_send(tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("toolu_1", X, 1))
    s.main(use(7, 10, "SendMessage", {"recipient": "worker-one"}))
    s.child(X)
    assert both_states(s.build()) == {X: RET}


def _fingerprint(s, child):
    return hooks_cli._unit_fingerprint("subagent", s.child_path(child), s.main_path, {})


def _names(fingerprint):
    return {entry[0] for entry in fingerprint}


def test_the_unreadable_meta_token_is_in_the_fingerprint(tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("t1", X, 1)).child(X, meta={"description": "alpha"}).build()
    meta = s.child_path(X).with_name(f"agent-{X}.meta.json")
    assert "#unreadable:meta" not in _names(_fingerprint(s, X))
    os.chmod(meta, 0)
    try:
        assert "#unreadable:meta" in _names(_fingerprint(s, X))
    finally:
        os.chmod(meta, 0o644)


def test_the_missing_launcher_token_is_in_the_fingerprint(monkeypatch, tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("tx", X, 1), note(30, Y, "ty", "completed")).child(X)
    s.child(X, child_entries(X), async_launch("ty", Y, 4))
    s.child(Y, meta={"parentAgentId": X}).build()
    before = _fingerprint(s, Y)
    assert any(name.startswith("#launcher:") and name != "#launcher:missing"
               for name in _names(before))
    real = reader.session_child_return

    def gone(main_path, agent_path, cache=None):
        decision, digest, _launcher, unreadable = real(main_path, agent_path, cache)
        return decision, digest, tmp_path / "gone.jsonl", unreadable

    monkeypatch.setattr(reader, "session_child_return", gone)
    after = _fingerprint(s, Y)
    assert "#launcher:missing" in _names(after)
    assert after != before


def test_the_launcher_token_changes_when_the_launcher_changes(tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("tx", X, 1), note(30, Y, "ty", "completed")).child(X)
    s.child(X, child_entries(X), async_launch("ty", Y, 4))
    s.child(Y, meta={"parentAgentId": X}).build()
    before = _fingerprint(s, Y)
    with s.child_path(X).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(send("ts", 50, "worker-one", recipient="worker-one")) + "\n")
    assert _fingerprint(s, Y) != before


@pytest.mark.parametrize("followed", [False, True], ids=["torn tail", "torn then a line"])
def test_the_independent_side_reads_a_torn_tail_like_the_reader(tmp_path, followed):
    s = three_children(tmp_path)
    tail = torn_bytes()
    if followed:
        tail += b"\n" + json.dumps(child_entries(Z)[1]).encode("utf-8") + b"\n"
    append_bytes(s.child_path(Z), tail)
    run = reader.load_session(s.main_path)
    assert collect_inv6(run, s.main_path)["all"].violations == 0
    assert ind.states(s.main_path) == states_of(run)


# --- GC-1b: the torn-line rule costs nothing on a clean transcript -------------------------------
#
# Measured on the real corpus (248 runs, Python 3.11): scanning every line for an escaped byte
# added 4.3 s of CPU to `scan --all` (25.5 s -> 29.9 s). A clean transcript is read strictly, as
# before G-C; only a read that fails to decode, on a file whose last line is torn, is read again
# with the byte scan, continuing after the lines the strict read already gave.


class _NoScan:
    """Stands in for the byte-scan pattern: any call fails the test."""

    def search(self, *_args):
        raise AssertionError("a transcript without an undecodable byte was scanned for one")


def test_a_clean_transcript_is_never_scanned_for_undecodable_bytes(tmp_path, monkeypatch, capsys):
    s = three_children(tmp_path)
    monkeypatch.setattr(reader, "_UNDECODABLE_BYTE_RE", _NoScan())
    assert states_of(reader.load_session(s.main_path)) == {X: RET, Y: RET, Z: RET}
    assert reader._has_timestamped_conversation(s.main_path) is True
    assert reader._sniff_session_file(s.main_path) is True
    reader._fork_scan(s.main_path)
    assert hooks_cli._subagent_ends_with_kill_sentinel(s.child_path(Z)) is False
    rescue._read_raw_line_text(s.child_path(Z), 1)
    code, _text, _err = run_cli(capsys, ["run", str(s.main_path), "--root", str(s.root)])
    assert code in (0, 4)


def test_a_torn_tail_behind_many_lines_reads_every_line_once(tmp_path):
    """The strict read gives the lines of the first decode chunks before it fails on the last
    one; the second read must not give them again."""
    path = tmp_path / "agent-x.jsonl"
    entries = [e for n in range(400) for e in child_entries(f"x{n}", last_text=f"Done {n}.")]
    clean_path = tmp_path / "clean.jsonl"
    for target in (path, clean_path):
        target.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    assert path.stat().st_size > 8 * 8192
    append_bytes(path, torn_bytes())
    clean = reader._read_transcript(clean_path, [], state=ledger.TranscriptState())
    diagnostics: list[str] = []
    state = ledger.TranscriptState()
    torn = reader._read_transcript(path, diagnostics, state=state)
    assert [(e.line_no, e.kind) for e in torn[0]] == [(e.line_no, e.kind) for e in clean[0]]
    assert (torn[3], torn[4]) == (clean[3] + 1, clean[4] + 1)
    assert diagnostics == [f"agent-x.jsonl:{len(entries) + 1}: half-written last line (cut inside "
                           "a character)"]
    assert state.undecodable is True and state.unreadable is False


def test_a_byte_in_the_middle_behind_many_lines_is_still_unreadable(tmp_path):
    path = tmp_path / "agent-x.jsonl"
    entries = [e for n in range(400) for e in child_entries(f"x{n}")]
    body = "".join(json.dumps(e) + "\n" for e in entries).encode("utf-8")
    path.write_bytes(body + b'{"t": "\xe5\xae"}\n' + body + torn_bytes())
    tracked = reader._read_transcript_tracked(path, [], state=ledger.TranscriptState())
    assert tracked[3] is True and tracked[0] == []


def test_the_changelog_names_the_user_visible_g_changes():
    text = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    entry = " ".join(text[text.index("## [0.3.1]"):text.index("## [0.3.0]")].split())
    for phrase in ("cannot be read, every child's return is unknown", "agent name", "not a",
                   "analyzed again at once", "cut inside a multibyte character"):
        assert phrase in entry, phrase
    assert "\u2014" not in entry and "http" not in entry
