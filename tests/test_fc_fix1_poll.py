"""Fix round 1 of field milestone F: S-m5 (docs/DESIGN.md 4.2, 4.13.3 FA4;
.reviews/v0.3/F-dispositions.md).

`ParentIndex.refresh` ran the fork scan twice per poll for a file whose stem never matches its
sessionId (a renamed copy: every line has the key, no own entry ever settles the boundary). The
boundary is now computed once per file fingerprint and reused: an unchanged file costs no scan on
the next poll, and a grown file costs exactly one.

Fixtures are synthetic.
"""

from __future__ import annotations

import json

import pytest

from agent_postmortem import reader
from tests.test_fa_fork import FORK_ID, PARENT_ID, _layout, _own_history, _parent_history
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


def _renamed_copy(tmp_path, monkeypatch):
    _no_config(monkeypatch, tmp_path)
    _, slug_dir = _layout(tmp_path)
    copy = slug_dir / f"{FORK_ID}.jsonl"
    _write_jsonl(copy, _parent_history(PARENT_ID))
    reader.ParentIndex._cache.pop(copy, None)
    return copy


def test_fc1_a_renamed_copy_is_scanned_once_per_fingerprint_across_polls(
    tmp_path, monkeypatch, counted
):
    copy = _renamed_copy(tmp_path, monkeypatch)
    first = reader.ParentIndex.for_path(copy)
    assert len(counted) == 1                      # the initial build
    second = reader.ParentIndex.for_path(copy)
    assert second is first
    assert len(counted) == 1                      # nothing changed: no scan at all
    with copy.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_parent_history(PARENT_ID)[0]) + "\n")
    reader.ParentIndex.for_path(copy)
    assert len(counted) == 2                      # the file grew: exactly one scan
    reader.ParentIndex.for_path(copy)
    assert len(counted) == 2
    reader.ParentIndex._cache.pop(copy, None)


def test_fc1_the_renamed_copy_keeps_its_boundary_and_state_across_polls(
    tmp_path, monkeypatch, counted
):
    copy = _renamed_copy(tmp_path, monkeypatch)
    first = reader.ParentIndex.for_path(copy)
    stops = len(first.task_stops)
    again = reader.ParentIndex.for_path(copy)
    assert again._skip_through == 0 and again._boundary_settled is False
    assert len(again.task_stops) == stops == 1
    assert again.rebuilds == 0
    reader.ParentIndex._cache.pop(copy, None)


def test_fc1_a_growing_fork_settles_with_one_scan_per_change(tmp_path, monkeypatch, counted):
    """The FA4 rebuild still happens once the first own entry appears, and costs one scan."""
    copy = _renamed_copy(tmp_path, monkeypatch)
    first = reader.ParentIndex.for_path(copy)
    assert len(counted) == 1
    with copy.open("a", encoding="utf-8") as handle:
        for entry in _own_history(FORK_ID):
            handle.write(json.dumps(entry) + "\n")
    second = reader.ParentIndex.for_path(copy)
    assert second is first and second.rebuilds == 1
    assert second.task_stops == [] and second._boundary_settled is True
    assert len(counted) == 2
    reader.ParentIndex.for_path(copy)             # settled: never scanned again
    reader.ParentIndex.for_path(copy)
    assert len(counted) == 2
    reader.ParentIndex._cache.pop(copy, None)


def test_fc1_a_pending_recheck_rescans_the_unchanged_file_once(tmp_path, monkeypatch, counted):
    """After a read failure the next poll reinspects from byte 0, boundary included, even
    though the fingerprint is the same: one scan, not two and not none."""
    copy = _renamed_copy(tmp_path, monkeypatch)
    index = reader.ParentIndex.for_path(copy)
    assert len(counted) == 1
    index._needs_full_recheck = True
    reader.ParentIndex.for_path(copy)
    assert len(counted) == 2
    reader.ParentIndex._cache.pop(copy, None)
