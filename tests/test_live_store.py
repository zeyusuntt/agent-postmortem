"""docs/DESIGN.md 4.14.8: the live store and the scan cache are JSON files beside state.json, each
with its own lock and the same 0700/0600 primitives; a cache that cannot be parsed is started
again empty, since nothing in it is the only copy of anything."""

from __future__ import annotations

import fcntl
import json
import os
import stat

import pytest

from agent_postmortem import state


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_cache_transaction_creates_a_private_file_and_persists_mutations(tmp_path):
    path = tmp_path / "xdg-state" / "agent-postmortem" / "live.json"
    with state.cache_transaction(path) as data:
        assert data == {}
        data["sessions"] = {"k": {"reported": ["a:end"]}}
    assert json.loads(path.read_text(encoding="utf-8")) == {"sessions": {"k": {"reported": ["a:end"]}}}
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert _mode(path.with_suffix(".lock")) == 0o600
    with state.cache_transaction(path) as data:
        assert data["sessions"]["k"]["reported"] == ["a:end"]


def test_cache_transaction_starts_empty_over_a_corrupt_file(tmp_path):
    path = tmp_path / "xdg-state" / "agent-postmortem" / "live.json"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("{not json", encoding="utf-8")
    with state.cache_transaction(path) as data:
        assert data == {}


def test_cache_transaction_writes_nothing_when_the_block_raises(tmp_path):
    path = tmp_path / "xdg-state" / "agent-postmortem" / "live.json"
    with state.cache_transaction(path) as data:
        data["x"] = 1
    with pytest.raises(RuntimeError):
        with state.cache_transaction(path) as data:
            data["x"] = 2
            raise RuntimeError("boom")
    assert json.loads(path.read_text(encoding="utf-8")) == {"x": 1}


def test_cache_transaction_gives_up_when_the_lock_is_held(tmp_path, monkeypatch):
    path = tmp_path / "xdg-state" / "agent-postmortem" / "live.json"
    with state.cache_transaction(path):
        pass
    monkeypatch.setattr(state, "LOCK_TIMEOUT_SECONDS", 0.2)
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(state.StateLocked):
            with state.cache_transaction(path):
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_read_cache_never_writes(tmp_path):
    path = tmp_path / "xdg-state" / "agent-postmortem" / "live.json"
    assert state.read_cache(path) == {}
    assert not path.exists()


def test_live_and_scan_cache_paths_sit_beside_state_json(tmp_path):
    assert state.live_store_path().parent == state.state_path().parent
    assert state.live_store_path().name == "live.json"
    assert state.scan_cache_path().name == "scan-cache.json"
