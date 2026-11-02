"""docs/DESIGN.md 4.14.9 item 4: scan's transcript cache. When a run's inputs moved, scan reads
again only the transcripts whose files changed and takes the rest from the run's stored parses,
and prints exactly what a scan with no cache prints. A stored parse is used only from a private
file this user wrote, for the same run and the same package code."""

from __future__ import annotations

import json
import os
import pickle
import shutil
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from live_helpers import (
    FAKE_UUID,
    aid,
    assistant_text,
    backdate_tree,
    tool_result,
    tool_use,
    user,
    write_clean_workflow,
    write_jsonl,
)
from n3b_helpers import CASES, FIXTURE_ROOT

from agent_postmortem import cli, reader, report, state
from agent_postmortem.cli import main
from agent_postmortem.detectors import claim_contradiction

SLUG = "-home-user-project"
DEMO = Path(__file__).resolve().parent.parent / "src" / "agent_postmortem" / "demo"
CHILDREN = [aid(701 + i) for i in range(5)]
NOW = datetime.now(timezone.utc).replace(microsecond=0)


_CODE_DIGEST = cli._transcript_cache_code


@pytest.fixture(autouse=True)
def _fresh_code_digest():
    yield
    _CODE_DIGEST.cache_clear()


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(report, "_now", lambda: NOW + timedelta(minutes=1))
    monkeypatch.setattr(claim_contradiction, "_now", lambda: NOW + timedelta(minutes=1))


def _subagents(root: Path) -> Path:
    return root / SLUG / FAKE_UUID / "subagents"


def _root(tmp_path: Path) -> Path:
    """A live session with five subagents, and a workflow last written 30 days ago."""
    root = tmp_path / "projects"
    start = NOW - timedelta(minutes=30)
    entries = [user(None, start, "start")]
    for n, child in enumerate(CHILDREN):
        at = start + timedelta(minutes=n)
        tool_id = f"toolu_{n:02d}"
        entries.append(tool_use(None, at, tool_id, "Agent",
                                {"description": f"task {n}", "prompt": "do the task"}))
        entries.append(tool_result(None, at + timedelta(seconds=40), tool_id, "done",
                                   toolUseResult={"agentId": child, "status": "completed"}))
        write_jsonl(_subagents(root) / f"agent-{child}.jsonl", [
            user(child, at, "do the task"),
            assistant_text(child, at + timedelta(seconds=30), "done"),
        ])
        (_subagents(root) / f"agent-{child}.meta.json").write_text(
            json.dumps({"agentType": "general-purpose", "description": f"task {n}"}),
            encoding="utf-8",
        )
    write_jsonl(root / SLUG / f"{FAKE_UUID}.jsonl", entries)
    old = write_clean_workflow(root, "-home-user-other", "wf_00000000-753",
                               NOW - timedelta(days=30), agent=61)
    backdate_tree(old, NOW - timedelta(days=30))
    return root


def _scan(capsys, root: Path, *extra) -> tuple[int, str, str]:
    """(exit status, stdout, stderr) of one scan."""
    code = main(["scan", "--root", str(root), *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


_COLD = {"n": 0}


def _cold(capsys, monkeypatch, tmp_path: Path, root: Path, *extra) -> tuple[int, str, str]:
    """The same scan with no cache at all: a copy of the state directory (its disclosure secret
    names the pseudonyms) without the run cache and the transcript cache."""
    _COLD["n"] += 1
    kept = os.environ["XDG_STATE_HOME"]
    cold_home = tmp_path / f"cold-state-{_COLD['n']}"
    if state.state_dir().is_dir():
        shutil.copytree(state.state_dir(), cold_home / "agent-postmortem", symlinks=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(cold_home))
    state.scan_cache_path().unlink(missing_ok=True)
    if state.transcript_cache_dir().is_dir():
        shutil.rmtree(state.transcript_cache_dir())
    state.transcript_cache_dir().unlink(missing_ok=True)
    try:
        return _scan(capsys, root, *extra)
    finally:
        monkeypatch.setenv("XDG_STATE_HOME", kept)


def _parses(monkeypatch) -> list[Path]:
    seen: list[Path] = []
    real = reader._read_transcript

    def counting(path, *args, **kwargs):
        seen.append(Path(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(reader, "_read_transcript", counting)
    return seen


def _cache_files() -> list[Path]:
    directory = state.transcript_cache_dir()
    return sorted(directory.glob("*.pickle")) if directory.is_dir() else []


def _grow(root: Path, child: str) -> Path:
    path = _subagents(root) / f"agent-{child}.jsonl"
    write_jsonl(path, [assistant_text(child, NOW, "more")], mode="a")
    return path


@pytest.mark.parametrize("extra", [(), ("--all",), ("--json",)])
def test_an_append_parses_only_the_grown_transcript(tmp_path, capsys, monkeypatch, extra):
    root = _root(tmp_path)
    _scan(capsys, root, *extra)
    grown = _grow(root, CHILDREN[2])
    parses = _parses(monkeypatch)
    warm = _scan(capsys, root, *extra)
    assert parses == [grown]
    assert warm == _cold(capsys, monkeypatch, tmp_path, root, *extra)


def test_a_grown_main_transcript_is_the_only_one_parsed(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    _scan(capsys, root, "--json")
    main_path = root / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [assistant_text(None, NOW, "still here")], mode="a")
    parses = _parses(monkeypatch)
    warm = _scan(capsys, root, "--json")
    assert parses == [main_path]
    assert warm == _cold(capsys, monkeypatch, tmp_path, root, "--json")


def test_a_deleted_subagent_prints_what_a_cold_scan_prints(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    _scan(capsys, root, "--json")
    (_subagents(root) / f"agent-{CHILDREN[1]}.jsonl").unlink()
    (_subagents(root) / f"agent-{CHILDREN[1]}.meta.json").unlink()
    parses = _parses(monkeypatch)
    warm = _scan(capsys, root, "--json")
    assert parses == []
    assert warm == _cold(capsys, monkeypatch, tmp_path, root, "--json")


def _fixture_root(tmp_path: Path, case: str) -> Path | None:
    """The fixture case as a scan root with every file written just now; a case that is one
    flat workflow directory goes under the canonical layout."""
    source = FIXTURE_ROOT / case
    root = tmp_path / "projects"
    if reader.discover_runs(source):
        shutil.copytree(source, root)
    elif (source / "journal.jsonl").exists():
        shutil.copytree(source, root / SLUG / FAKE_UUID / "subagents" / "workflows"
                        / "wf_00000000-757")
    else:
        return None
    stamp = NOW.timestamp()
    for path in root.rglob("*"):
        os.utime(path, (stamp, stamp))
    return root


@pytest.mark.parametrize("extra", [("--json",), ("--all",)])
@pytest.mark.parametrize("case", [*CASES, "demo"])
def test_a_scan_from_stored_parses_prints_what_a_cold_scan_prints(
    tmp_path, capsys, monkeypatch, case, extra,
):
    if case == "demo":
        root = tmp_path / "projects"
        shutil.copytree(DEMO / SLUG, root / SLUG)
        stamp = NOW.timestamp()
        for path in root.rglob("*"):
            os.utime(path, (stamp, stamp))
    else:
        root = _fixture_root(tmp_path, case)
    if root is None or not reader.discover_runs(root):
        pytest.skip("not a scan root")
    cold = _scan(capsys, root, *extra)
    if any(path.name != "journal.jsonl" for path in root.rglob("*.jsonl")):
        assert _cache_files()
    state.scan_cache_path().unlink()  # every run is loaded again, from stored parses
    parses = _parses(monkeypatch)
    warm = _scan(capsys, root, *extra)
    assert parses == []
    assert warm == cold


def test_the_cache_is_private_and_kept_only_for_recently_written_runs(tmp_path, capsys):
    root = _root(tmp_path)
    _scan(capsys, root)
    files = _cache_files()
    assert len(files) == 1  # the live session; the workflow last written 30 days ago has none
    assert oct(state.transcript_cache_dir().stat().st_mode & 0o777) == "0o700"
    assert oct(files[0].stat().st_mode & 0o777) == "0o600"


def _symlinked(path: Path, tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "elsewhere.pickle"
    shutil.move(str(path), str(target))
    path.symlink_to(target)


def _group_readable(path: Path, tmp_path: Path, monkeypatch) -> None:
    path.chmod(0o640)


def _open_directory(path: Path, tmp_path: Path, monkeypatch) -> None:
    path.parent.chmod(0o755)


def _corrupt(path: Path, tmp_path: Path, monkeypatch) -> None:
    path.write_bytes(b"not a pickle")


def _another_run(path: Path, tmp_path: Path, monkeypatch) -> None:
    data = pickle.loads(path.read_bytes())
    data["run"] = str(tmp_path / "another.jsonl")
    path.write_bytes(pickle.dumps(data))


def _other_code(path: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_transcript_cache_code", lambda: "0" * 64)


def _other_python(path: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "version", sys.version + " (another build)")
    _CODE_DIGEST.cache_clear()


def _other_int_limit(path: Path, tmp_path: Path, monkeypatch) -> None:
    """json refuses an integer longer than this limit, so a parse depends on it."""
    real = sys.get_int_max_str_digits()
    monkeypatch.setattr(sys, "get_int_max_str_digits", lambda: 640 if real != 640 else 0)


def _other_recursion_limit(path: Path, tmp_path: Path, monkeypatch) -> None:
    """json gives up on a value nested deeper than this, so a parse depends on it."""
    real = sys.getrecursionlimit()
    monkeypatch.setattr(sys, "getrecursionlimit", lambda: real + 1)


def _planted(path: Path, tmp_path: Path, monkeypatch) -> None:
    from test_transcript_memo import _Plant

    path.write_bytes(pickle.dumps({"schema": 1, "entries": _Plant(tmp_path / "planted")}))


@pytest.mark.parametrize("tamper", [
    _symlinked, _group_readable, _open_directory, _corrupt, _another_run, _other_code,
    _other_python, _other_int_limit, _other_recursion_limit, _planted,
])
def test_a_cache_file_that_cannot_be_trusted_is_never_used(tmp_path, capsys, monkeypatch, tamper):
    root = _root(tmp_path)
    _scan(capsys, root, "--json")
    [path] = _cache_files()
    _grow(root, CHILDREN[0])
    tamper(path, tmp_path, monkeypatch)
    parses = _parses(monkeypatch)
    warm = _scan(capsys, root, "--json")
    # every transcript of the session is read again (an identity change reloads the old
    # workflow too, since the run cache shares the identity)
    assert len([path for path in parses if "-home-user-other" not in path.parts]) == 1 + len(CHILDREN)
    assert not (tmp_path / "planted").exists()
    assert warm == _cold(capsys, monkeypatch, tmp_path, root, "--json")


def test_a_skipped_line_is_reported_the_same_from_stored_parses(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    with (_subagents(root) / f"agent-{CHILDREN[3]}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    cold = _scan(capsys, root)
    assert "SKIPPED" in cold[1] + cold[2]
    state.scan_cache_path().unlink()
    parses = _parses(monkeypatch)
    warm = _scan(capsys, root)
    # only the 30-day-old workflow, which keeps no stored parses, is read again
    assert parses and all("-home-user-other" in path.parts for path in parses)
    assert warm == cold


def test_a_cache_file_owned_by_someone_else_is_not_read(tmp_path, capsys, monkeypatch):
    _scan(capsys, _root(tmp_path))
    [path] = _cache_files()
    assert state.read_private_bytes(path) == path.read_bytes()
    real = os.getuid()
    monkeypatch.setattr(state, "_current_uid", lambda: real + 1)
    assert state.read_private_bytes(path) is None


def test_no_cache_reads_no_stored_parse_and_stores_fresh_ones(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    _scan(capsys, root)
    parses = _parses(monkeypatch)
    _scan(capsys, root, "--no-cache")
    assert len(parses) == 1 + len(CHILDREN) + 1  # the session and the workflow
    grown = _grow(root, CHILDREN[4])
    parses.clear()
    _scan(capsys, root)
    assert parses == [grown]


def test_here_neither_reads_nor_writes_the_transcript_cache(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.chdir(tmp_path)
    main(["scan", "--root", str(root), "--here"])
    capsys.readouterr()
    assert _cache_files() == []


def test_a_cache_file_not_written_for_two_days_is_removed(tmp_path, capsys):
    directory = state.transcript_cache_dir()
    directory.mkdir(parents=True, mode=0o700)
    state.state_dir().chmod(0o700)
    stale = directory / ("a" * 32 + ".pickle")
    stale.write_bytes(b"x")
    stale.chmod(0o600)
    gone = (NOW - timedelta(days=3)).timestamp()
    os.utime(stale, (gone, gone))
    recent = directory / ("b" * 32 + ".pickle")
    recent.write_bytes(b"x")
    recent.chmod(0o600)
    _scan(capsys, _root(tmp_path))
    names = {path.name for path in _cache_files()}
    assert stale.name not in names
    assert recent.name in names
    assert len(names) == 2


def test_an_unusable_cache_directory_never_stops_a_scan(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    state.state_dir().mkdir(parents=True, mode=0o700)
    state.transcript_cache_dir().write_bytes(b"a file where the directory goes")
    out = _scan(capsys, root, "--json")
    assert out == _cold(capsys, monkeypatch, tmp_path, root, "--json")


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def _stale(path: Path) -> Path:
    path.write_bytes(b"x")
    path.chmod(0o600)
    gone = (NOW - timedelta(days=3)).timestamp()
    os.utime(path, (gone, gone))
    return path


def _swap_on_first(monkeypatch, name: str, directory: Path, elsewhere: Path, when) -> None:
    """Replace `directory` with a symlink to `elsewhere` just before the first os.<name> call
    that `when` picks: the race a check-then-use on paths loses."""
    real = getattr(os, name)
    done = []

    def swapping(*args, **kwargs):
        if not done and when(args):
            done.append(True)
            directory.rename(directory.with_name(directory.name + "-moved"))
            directory.symlink_to(elsewhere, target_is_directory=True)
        return real(*args, **kwargs)

    monkeypatch.setattr(os, name, swapping)


def test_pruning_stays_in_the_directory_it_checked(tmp_path, monkeypatch):
    state.state_dir().mkdir(parents=True, mode=0o700)
    directory = _private_dir(state.transcript_cache_dir())
    _stale(directory / ("a" * 32 + ".pickle"))
    elsewhere = _private_dir(tmp_path / "elsewhere")
    kept = _stale(elsewhere / ("b" * 32 + ".pickle"))
    _swap_on_first(monkeypatch, "listdir", directory, elsewhere, lambda args: True)
    state.prune_private_files(directory, NOW.timestamp())
    assert kept.exists()
    assert not (directory.with_name(directory.name + "-moved") / ("a" * 32 + ".pickle")).exists()


def test_pruning_removes_only_cache_file_names(tmp_path):
    state.state_dir().mkdir(parents=True, mode=0o700)
    directory = _private_dir(state.transcript_cache_dir())
    other = _stale(directory / "notes.pickle")
    temp = _stale(directory / ".cache-0123456789abcdef.tmp")
    state.prune_private_files(directory, NOW.timestamp())
    assert other.exists()
    assert not temp.exists()


def test_a_write_stays_in_the_directory_it_checked(tmp_path, monkeypatch):
    state.state_dir().mkdir(parents=True, mode=0o700)
    directory = _private_dir(state.transcript_cache_dir())
    elsewhere = _private_dir(tmp_path / "elsewhere")
    _swap_on_first(monkeypatch, "open", directory, elsewhere,
                   lambda args: ".cache-" in str(args[0]))
    state.write_private_bytes(directory / ("c" * 32 + ".pickle"), b"data")
    assert list(elsewhere.iterdir()) == []
    moved = directory.with_name(directory.name + "-moved")
    assert (moved / ("c" * 32 + ".pickle")).read_bytes() == b"data"


@pytest.mark.parametrize("source_readable", [True, False])
def test_another_int_limit_with_no_file_changed_prints_what_a_cold_scan_prints(
    tmp_path, capsys, monkeypatch, source_readable,
):
    """json refuses an integer longer than the limit, so a run's facts depend on it as well,
    whether or not the package source can be read for the transcript cache's digest."""
    if not source_readable:
        monkeypatch.setattr(cli, "_transcript_cache_code", lambda: "")
    root = _root(tmp_path)
    child = _subagents(root) / f"agent-{CHILDREN[1]}.jsonl"
    entry = json.dumps(assistant_text(CHILDREN[1], NOW - timedelta(minutes=5), "done"))
    with child.open("a", encoding="utf-8") as handle:
        handle.write(entry[:-1] + ', "n": ' + "9" * 700 + "}\n")
    real = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(0)
        unlimited = _scan(capsys, root)
        sys.set_int_max_str_digits(640)
        warm = _scan(capsys, root)
        cold = _cold(capsys, monkeypatch, tmp_path, root)
    finally:
        sys.set_int_max_str_digits(real)
    assert warm == cold
    assert warm != unlimited


def test_a_stored_parse_that_breaks_the_load_is_read_again(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    _scan(capsys, root, "--json")
    [path] = _cache_files()
    data = pickle.loads(path.read_bytes())
    [key] = [key for key in data["entries"] if key[0].endswith(f"{FAKE_UUID}.jsonl")]
    fingerprint, blob = data["entries"][key]
    stored = pickle.loads(zlib.decompress(blob))
    stored[0][0][0].ts = "not a time"  # passes the shape checks, breaks the session load
    data["entries"][key] = (fingerprint, zlib.compress(pickle.dumps(stored)))
    path.write_bytes(pickle.dumps(data))
    state.scan_cache_path().unlink()
    warm = _scan(capsys, root, "--json")
    assert warm == _cold(capsys, monkeypatch, tmp_path, root, "--json")
    grown = _grow(root, CHILDREN[0])
    parses = _parses(monkeypatch)
    _scan(capsys, root, "--json")
    assert parses == [grown]  # the file was written again from the fresh reads


def test_a_damaged_event_that_breaks_a_detector_is_read_again(tmp_path, capsys, monkeypatch):
    root = _fixture_root(tmp_path, "codex_write_denied")
    cold = _scan(capsys, root, "--json")
    [path] = _cache_files()
    data = pickle.loads(path.read_bytes())
    keys = [key for key in data["entries"] if key[0].endswith("agent-ac500000000000001.jsonl")]
    assert keys
    for key in keys:
        fingerprint, blob = data["entries"][key]
        stored = pickle.loads(zlib.decompress(blob))
        stored[0][0][2].text = None  # passes the shape checks, breaks a detector
        data["entries"][key] = (fingerprint, zlib.compress(pickle.dumps(stored)))
    path.write_bytes(pickle.dumps(data))
    state.scan_cache_path().unlink()
    assert _scan(capsys, root, "--json") == cold
