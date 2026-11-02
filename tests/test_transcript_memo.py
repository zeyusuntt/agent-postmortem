"""docs/DESIGN.md 4.14.9 item 4: the per-transcript memo. A transcript whose file is unchanged
gives back, from a stored parse, exactly what a fresh read gives: the parse result, the
diagnostics it appended, the state and launch log it filled, and the lines it counted as
skipped. A transcript whose file moved in any way is read again."""

from __future__ import annotations

import os
import pickle
import shutil
import zlib
from pathlib import Path

import pytest

from agent_postmortem import ledger, reader, returns

TESTS = Path(__file__).resolve().parent
TRANSCRIPTS = sorted(
    [*TESTS.joinpath("fixtures").rglob("*.jsonl"),
     *TESTS.parent.joinpath("src", "agent_postmortem", "demo").rglob("*.jsonl")]
)
# the three shapes the loaders call with: a session transcript, a workflow attempt, evidence
SHAPES = {"state_and_log": (True, True), "state_only": (True, False), "log_only": (False, True)}


def _read(path: Path, with_state: bool, with_log: bool, skip: int = 0):
    """One call the way a loader makes it, under its own skip counter: everything it gives
    back or leaves behind."""
    diagnostics: list[str] = ["earlier"]
    state = ledger.TranscriptState() if with_state else None
    log = reader._LaunchLog() if with_log else None
    skips = reader._ReadSkips()
    token = reader._READ_SKIPS.set(skips)
    try:
        result = reader._read_transcript_tracked(
            path, diagnostics, state=state, launch_log=log, skip_through_line=skip,
        )
    finally:
        reader._READ_SKIPS.reset(token)
    return {
        "result": result,
        "diagnostics": diagnostics,
        "diagnostic_types": [type(item) for item in diagnostics],
        "state": None if state is None else vars(state),
        "log": None if log is None else vars(log),
        "skips": (skips.lines, skips.files),
    }


def _stored(memo: reader.TranscriptMemo) -> reader.TranscriptMemo:
    """What another process finds: the entries this memo used, through bytes."""
    return reader.TranscriptMemo(pickle.loads(pickle.dumps(memo.used)))


def _parses(monkeypatch) -> list[Path]:
    seen: list[Path] = []
    real = reader._read_transcript

    def counting(path, *args, **kwargs):
        seen.append(Path(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(reader, "_read_transcript", counting)
    return seen


def test_the_fixture_set_is_not_empty():
    assert len(TRANSCRIPTS) > 100


def test_the_absent_sentinels_survive_a_pickle_round_trip():
    assert pickle.loads(pickle.dumps(returns.ABSENT)) is returns.ABSENT
    assert pickle.loads(pickle.dumps(reader._ABSENT)) is reader._ABSENT


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_stored_parse_gives_back_what_a_fresh_read_gives(shape, monkeypatch):
    with_state, with_log = SHAPES[shape]
    first = reader.TranscriptMemo()
    with reader.transcript_memo(first):
        for path in TRANSCRIPTS:
            _read(path, with_state, with_log)
    stored = _stored(first)
    fresh = {path: _read(path, with_state, with_log) for path in TRANSCRIPTS}
    parses = _parses(monkeypatch)
    with reader.transcript_memo(stored):
        reused = {path: _read(path, with_state, with_log) for path in TRANSCRIPTS}
    assert parses == []
    assert stored.reused == len(TRANSCRIPTS)
    for path in TRANSCRIPTS:
        assert reused[path] == fresh[path], path


def test_two_hits_on_one_stored_parse_share_no_object(tmp_path):
    path = TESTS / "fixtures" / "runs" / "session_clean"
    transcript = sorted(path.rglob("*.jsonl"))[0]
    first = reader.TranscriptMemo()
    with reader.transcript_memo(first):
        _read(transcript, True, True)
    stored = _stored(first)
    with reader.transcript_memo(stored):
        one = _read(transcript, True, True)
        two = _read(transcript, True, True)
    one["result"][0].clear()
    one["log"]["launches"].append(("Agent", "x", 1, None))
    assert two["result"][0] != []
    assert ("Agent", "x", 1, None) not in two["log"]["launches"]


def test_a_fork_skip_is_part_of_the_key(monkeypatch):
    transcript = sorted((TESTS / "fixtures" / "runs" / "session_clean").rglob("*.jsonl"))[0]
    first = reader.TranscriptMemo()
    with reader.transcript_memo(first):
        _read(transcript, True, True, skip=0)
    stored = _stored(first)
    expected = _read(transcript, True, True, skip=2)
    parses = _parses(monkeypatch)
    with reader.transcript_memo(stored):
        skipped = _read(transcript, True, True, skip=2)
    assert parses == [transcript]
    assert skipped == expected


def _copy(tmp_path: Path) -> Path:
    source = sorted((TESTS / "fixtures" / "runs" / "session_clean").rglob("*.jsonl"))[0]
    target = tmp_path / "t" / source.name
    target.parent.mkdir()
    shutil.copy2(source, target)
    return target


def _append(path: Path) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "user", "timestamp": "2026-01-01T00:00:09Z", '
                     '"message": {"role": "user", "content": "more"}}\n')


def _same_size_rewrite(path: Path) -> None:
    st = path.stat()
    data = bytearray(path.read_bytes())
    index = data.index(b'"user"')
    data[index + 1:index + 5] = b"USER"
    path.write_bytes(bytes(data))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert path.stat().st_size == st.st_size


def _truncate(path: Path) -> None:
    data = path.read_bytes()
    path.write_bytes(data[: data.index(b"\n") + 1])


def _replace(path: Path) -> None:
    other = path.with_name("other.jsonl")
    shutil.copy2(path, other)
    _append(other)
    os.replace(other, path)


@pytest.mark.parametrize("change", [_append, _same_size_rewrite, _truncate, _replace])
def test_a_transcript_that_moved_is_read_again(tmp_path, monkeypatch, change):
    transcript = _copy(tmp_path)
    first = reader.TranscriptMemo()
    with reader.transcript_memo(first):
        _read(transcript, True, True)
    stored = _stored(first)
    change(transcript)
    expected = _read(transcript, True, True)
    parses = _parses(monkeypatch)
    with reader.transcript_memo(stored):
        again = _read(transcript, True, True)
    assert parses == [transcript]
    assert stored.reused == 0
    assert again == expected


def test_a_state_or_log_that_is_not_fresh_bypasses_the_memo(tmp_path):
    transcript = _copy(tmp_path)
    memo = reader.TranscriptMemo()
    state = ledger.TranscriptState()
    state.undecodable = True
    log = reader._LaunchLog()
    log.use_counts["toolu_x"] = 1
    with reader.transcript_memo(memo):
        reader._read_transcript_tracked(transcript, [], state=state)
        reader._read_transcript_tracked(transcript, [], launch_log=log)
    assert memo.used == {}
    assert state.undecodable is True
    assert log.use_counts["toolu_x"] == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_an_unreadable_transcript_is_never_stored(tmp_path):
    transcript = _copy(tmp_path)
    transcript.chmod(0)
    memo = reader.TranscriptMemo()
    try:
        with reader.transcript_memo(memo):
            result = reader._read_transcript_tracked(transcript, [], state=ledger.TranscriptState())
    finally:
        transcript.chmod(0o600)
    assert result[3] is True
    assert memo.used == {}


def test_a_stored_entry_that_does_not_unpickle_is_read_again(tmp_path, monkeypatch):
    transcript = _copy(tmp_path)
    first = reader.TranscriptMemo()
    with reader.transcript_memo(first):
        _read(transcript, True, True)
    broken = {key: (fingerprint, b"not a pickle") for key, (fingerprint, _blob) in first.used.items()}
    stored = reader.TranscriptMemo(broken)
    with reader.transcript_memo(stored):
        again = _read(transcript, True, True)
    assert stored.reused == 0
    assert again == _read(transcript, True, True)


def test_no_memo_means_no_memo_work(tmp_path):
    transcript = _copy(tmp_path)
    memo = reader.TranscriptMemo()
    _read(transcript, True, True)
    assert memo.used == {}


@pytest.mark.parametrize("envelope", [
    lambda fingerprint, blob: (),
    lambda fingerprint, blob: (fingerprint,),
    lambda fingerprint, blob: (fingerprint, blob, "extra"),
    lambda fingerprint, blob: [fingerprint, blob],
    lambda fingerprint, blob: None,
])
def test_a_stored_entry_of_the_wrong_shape_is_read_again(tmp_path, envelope):
    transcript = _copy(tmp_path)
    first = reader.TranscriptMemo()
    with reader.transcript_memo(first):
        _read(transcript, True, True)
    stored = reader.TranscriptMemo(
        {key: envelope(fingerprint, blob) for key, (fingerprint, blob) in first.used.items()}
    )
    with reader.transcript_memo(stored):
        again = _read(transcript, True, True)
    assert stored.reused == 0
    assert again == _read(transcript, True, True)


def test_a_parse_that_cannot_be_stored_is_still_given_back(tmp_path, monkeypatch):
    """Pickling can fail on a value json accepted (deep nesting on some Python builds)."""
    transcript = _copy(tmp_path)
    expected = _read(transcript, True, True)

    def refuse(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded while pickling")

    monkeypatch.setattr(pickle, "dumps", refuse)
    memo = reader.TranscriptMemo()
    with reader.transcript_memo(memo):
        got = _read(transcript, True, True)
    assert got == expected
    assert memo.used == {}
    assert memo.parsed == 1


class _Plant:
    """Unpickling this makes a directory: the shape of a stored file that names a function."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __reduce__(self):
        return (os.mkdir, (str(self.path),))


def test_a_stored_entry_that_names_a_function_never_runs_it(tmp_path):
    transcript = _copy(tmp_path)
    first = reader.TranscriptMemo()
    with reader.transcript_memo(first):
        _read(transcript, True, True)
    marker = tmp_path / "planted"
    planted = zlib.compress(pickle.dumps((_Plant(marker), [], None, None, (0, 0))))
    stored = reader.TranscriptMemo(
        {key: (fingerprint, planted) for key, (fingerprint, _blob) in first.used.items()}
    )
    with reader.transcript_memo(stored):
        again = _read(transcript, True, True)
    assert not marker.exists()
    assert stored.reused == 0
    assert again == _read(transcript, True, True)


def test_plain_unpickling_refuses_every_global(tmp_path):
    marker = tmp_path / "planted"
    with pytest.raises(pickle.UnpicklingError):
        reader.unpickle_plain(pickle.dumps({"entries": _Plant(marker)}))
    assert not marker.exists()
    assert reader.unpickle_plain(pickle.dumps({"a": (1, b"x", [None, True])})) == {
        "a": (1, b"x", [None, True])
    }


def _rebuilt(stored, slot, value):
    """The stored tuple with one slot of its parse result replaced."""
    result = list(stored[0])
    result[slot] = value
    return (tuple(result), *stored[1:])


def _with_attr(obj, name, value):
    vars(obj)[name] = value
    return obj


@pytest.mark.parametrize("damage", [
    lambda stored: _rebuilt(stored, 0, None),
    lambda stored: _rebuilt(stored, 0, ["not an event"]),
    lambda stored: _rebuilt(stored, 1, 7),
    lambda stored: _rebuilt(stored, 3, "no"),
    lambda stored: _rebuilt(stored, 5, None),
    lambda stored: _rebuilt(stored, 6, [object.__new__(ledger.TranscriptState)]),
    lambda stored: (stored[0], [1], *stored[2:]),
    lambda stored: (*stored[:2], _with_attr(stored[2], "cwds", None), *stored[3:]),
    lambda stored: (*stored[:3], _with_attr(stored[3], "launches", None), stored[4]),
    lambda stored: (*stored[:4], (-1, 0)),
    lambda stored: (*stored[:4], (True, 0)),
])
def test_a_stored_parse_with_damaged_contents_is_read_again(tmp_path, damage):
    transcript = _copy(tmp_path)
    first = reader.TranscriptMemo()
    with reader.transcript_memo(first):
        _read(transcript, True, True)
    damaged = {
        key: (fingerprint, zlib.compress(pickle.dumps(damage(pickle.loads(zlib.decompress(blob))))))
        for key, (fingerprint, blob) in first.used.items()
    }
    stored = reader.TranscriptMemo(damaged)
    with reader.transcript_memo(stored):
        again = _read(transcript, True, True)
    assert stored.reused == 0
    assert again == _read(transcript, True, True)
