"""Fix round 2 of field milestone F: R2-3 (docs/DESIGN.md 4.2, 4.13.3 FA4;
.reviews/v0.3/F-dispositions.md, Codex round 2).

The fork-boundary cache of S-m5 is keyed by (inode, size, mtime). A same-size rewrite whose mtime
was restored leaves that fingerprint unchanged, but the existing prefix digest detects the
rewrite and rebuilds the index; the rebuild must also rescan the fork boundary, or an inherited
TaskStop (or launch, notification, diagnostic) would count as the fork's own.

Fixtures are synthetic.
"""

from __future__ import annotations

import json
import os

import pytest

from agent_postmortem import reader
from tests.test_fa_fork import (
    FORK_ID,
    PARENT_ID,
    _assistant_tool,
    _at,
    _layout,
    _parent_history,
    _user,
    _with,
)
from tests.test_field_v03 import _no_config, _write_jsonl


@pytest.fixture
def counted(monkeypatch):
    calls = []
    real = reader._fork_scan

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(reader, "_fork_scan", counting)
    return calls


def _rewrite_last_line_in_place(path, old, new):
    """Same inode, same size, same mtime: only the last line's session id changes."""
    assert len(old) == len(new)
    stat = path.stat()
    data = path.read_bytes()
    at = data.rindex(old.encode())
    with path.open("r+b") as handle:
        handle.seek(at)
        handle.write(new.encode())
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        stat.st_ino, stat.st_size, stat.st_mtime_ns
    )


def _state(index):
    return {
        "skip_through": index._skip_through,
        "settled": index._boundary_settled,
        "task_stops": list(index.task_stops),
        "kill_sentinels": list(index.kill_sentinels),
        "kill_timestamps": list(index.kill_timestamps),
        "compaction": list(index.compaction_timestamps),
        "tool_result_agent_ids": set(index.tool_result_agent_ids),
        "generations": list(index.generations),
        "ambiguous": list(index.ambiguous_launches),
        "diagnostics": list(index.diagnostics),
    }


def _file(tmp_path, monkeypatch, entries):
    _no_config(monkeypatch, tmp_path)
    _, slug_dir = _layout(tmp_path)
    path = slug_dir / f"{FORK_ID}.jsonl"
    _write_jsonl(path, entries)
    reader.ParentIndex._cache.pop(path, None)
    return path


def test_fc2_a_same_fingerprint_rewrite_of_a_two_line_fork_file_rescans_the_boundary(
    tmp_path, monkeypatch, counted
):
    """Codex's exact input: line 1 holds a TaskStop, both lines name the parent session; line 2
    is then rewritten to the fork's own id with the mtime restored."""
    entries = [
        _with(_assistant_tool("ts1", "TaskStop", {"task_id": "tX"}, _at(1)), PARENT_ID),
        _with(_user("hello", _at(2)), PARENT_ID),
    ]
    path = _file(tmp_path, monkeypatch, entries)
    index = reader.ParentIndex.for_path(path)
    assert index._skip_through == 0 and index._boundary_settled is False
    assert len(index.task_stops) == 1
    before = len(counted)
    _rewrite_last_line_in_place(path, PARENT_ID, FORK_ID)
    again = reader.ParentIndex.for_path(path)
    assert again is index and again.rebuilds == 1
    assert (again._skip_through, again._boundary_settled) == (1, True)
    assert again.task_stops == []                       # the inherited TaskStop is not ours
    assert len(counted) == before + 1                   # one scan for this refresh, not two
    reader.ParentIndex._cache.pop(path, None)


def test_fc2_a_same_fingerprint_rewrite_matches_a_fresh_build_in_every_accumulator(
    tmp_path, monkeypatch, counted
):
    """A full inherited history (a TaskStop, an interrupt sentinel, a Workflow launch with its
    result, an agent notification) followed by one line that is rewritten from the parent's id
    to the fork's: the rebuilt index equals a fresh one, accumulator by accumulator."""
    own = _with(_user("fork: continue", _at(100)), PARENT_ID)
    path = _file(tmp_path, monkeypatch, _parent_history(PARENT_ID) + [own])
    index = reader.ParentIndex.for_path(path)
    assert index._boundary_settled is False
    assert index.task_stops and index.kill_sentinels
    _rewrite_last_line_in_place(path, PARENT_ID, FORK_ID)
    rebuilt = _state(reader.ParentIndex.for_path(path))
    reader.ParentIndex._cache.pop(path, None)
    fresh = _state(reader.ParentIndex.for_path(path))
    assert rebuilt == fresh
    assert rebuilt["skip_through"] == 10 and rebuilt["settled"] is True
    assert rebuilt["task_stops"] == [] and rebuilt["kill_sentinels"] == []
    assert rebuilt["generations"] == []
    reader.ParentIndex._cache.pop(path, None)


def test_fc2_a_rewrite_that_also_settles_a_new_own_entry_scans_once(
    tmp_path, monkeypatch, counted
):
    """No second invalidation in a refresh that already called the fork scan for a newly
    appeared own entry: appended own lines grow the file (the fingerprint changes), one scan."""
    entries = [_with(_user("hello", _at(2)), PARENT_ID)]
    path = _file(tmp_path, monkeypatch, entries)
    index = reader.ParentIndex.for_path(path)
    before = len(counted)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_with(_user("own", _at(3)), FORK_ID)) + "\n")
    reader.ParentIndex.for_path(path)
    assert len(counted) == before + 1
    assert index._boundary_settled is True
    reader.ParentIndex.for_path(path)
    assert len(counted) == before + 1
    reader.ParentIndex._cache.pop(path, None)
