"""Tests for state.py: the versioned, locked state store (docs/DESIGN.md
7.5). Covers the v1 to v2 migration, corrupt/too-new file handling, the
tolerant-vs-propagating distinction for a genuinely unreadable file, and
state.update() as the only write path, including its locking under real
cross-process contention.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agent_postmortem import state


def _write_raw(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# --- default shape and missing file ----------------------------------------


def test_default_state_is_schema_version_2():
    assert state.default_state() == {
        "version": 2,
        "min_reader": 2,
        "applied": [],
        "sessions": {},
        "digest": {"pending": []},
    }


def test_missing_file_returns_default_state(tmp_path):
    path = tmp_path / "state.json"
    assert state.load_state(path) == state.default_state()


# --- whole-file corruption ---------------------------------------------------


def test_corrupt_json_returns_default_state_and_load_state_writes_nothing(tmp_path):
    """docs/DESIGN.md 7.5: load_state() never writes -- not even a corrupt
    backup. Only update()/transaction() create one, and only under the
    lock (see the backup tests in the update()/transaction() section
    below)."""
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")

    result = state.load_state(path)

    assert result == state.default_state()
    assert not list(tmp_path.glob("state.json.corrupt-*"))
    assert path.read_text(encoding="utf-8") == "{not json"


def test_wrong_shape_json_returns_default_state_and_load_state_writes_nothing(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(["not", "the", "right", "shape"]), encoding="utf-8")

    result = state.load_state(path)

    assert result == state.default_state()
    assert not list(tmp_path.glob("state.json.corrupt-*"))


def test_missing_version_field_is_treated_as_whole_file_corrupt(tmp_path):
    path = tmp_path / "state.json"
    _write_raw(path, {"applied": []})

    result = state.load_state(path)

    assert result == state.default_state()
    assert not list(tmp_path.glob("state.json.corrupt-*"))


def test_invalid_utf8_is_treated_as_whole_file_corrupt(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b'{"version": 2, "applied": "\xff\xfe not valid utf-8"}')

    result = state.load_state(path)

    assert result == state.default_state()
    assert not list(tmp_path.glob("state.json.corrupt-*"))


# --- v1 to v2 migration -------------------------------------------------------


def test_v1_file_migrates_to_v2_preserving_applied_entry_for_entry(tmp_path):
    path = tmp_path / "state.json"
    applied = [{"rule_id": "R-GIT-CWD", "target": "/home/user/project/CLAUDE.md", "applied_at": "2026-01-01T00:00:00Z"}]
    _write_raw(path, {"version": 1, "applied": applied})

    result = state.load_state(path)

    assert result == {
        "version": 2,
        "min_reader": 2,
        "applied": applied,
        "sessions": {},
        "digest": {"pending": []},
    }
    # Entry for entry, not just equal after reparsing: the same rule_id,
    # target, applied_at in the same order, nothing added or dropped.
    assert json.dumps(result["applied"], sort_keys=True) == json.dumps(applied, sort_keys=True)
    # load_state() never writes: the file on disk is still v1.
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_v1_file_migration_is_idempotent_across_repeated_loads(tmp_path):
    path = tmp_path / "state.json"
    _write_raw(path, {"version": 1, "applied": [{"rule_id": "R-GIT-CWD", "target": "t", "applied_at": "2026-01-01T00:00:00Z"}]})

    first = state.load_state(path)
    second = state.load_state(path)

    assert first == second


def test_update_persists_the_v1_to_v2_migration_and_a_second_update_is_a_no_op_upgrade(tmp_path):
    path = tmp_path / "state.json"
    applied = [{"rule_id": "R-GIT-CWD", "target": "t", "applied_at": "2026-01-01T00:00:00Z"}]
    _write_raw(path, {"version": 1, "applied": applied})

    first = state.update(lambda s: None, path)
    assert first["version"] == 2
    assert first["applied"] == applied
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] == 2
    assert on_disk["applied"] == applied
    assert on_disk["sessions"] == {}
    assert on_disk["digest"] == {"pending": []}

    second = state.update(lambda s: None, path)
    assert second == first


# --- v2 namespace-level corruption -------------------------------------------


def test_v2_file_with_malformed_sessions_keeps_applied_and_resets_only_sessions(tmp_path):
    path = tmp_path / "state.json"
    applied = [{"rule_id": "R-GIT-CWD", "target": "t", "applied_at": "2026-01-01T00:00:00Z"}]
    _write_raw(path, {"version": 2, "min_reader": 2, "applied": applied, "sessions": "not-a-dict", "digest": {"pending": []}})

    result = state.load_state(path)

    assert result["applied"] == applied
    assert result["sessions"] == {}
    assert not list(tmp_path.glob("state.json.corrupt-*"))  # load_state() never writes


def test_v2_file_with_malformed_digest_keeps_applied_and_sessions(tmp_path):
    path = tmp_path / "state.json"
    applied = [{"rule_id": "R-GIT-CWD", "target": "t", "applied_at": "2026-01-01T00:00:00Z"}]
    sessions = {"sess-1": {"units": {}, "last_check": "2026-01-01T00:00:00Z"}}
    _write_raw(path, {"version": 2, "min_reader": 2, "applied": applied, "sessions": sessions, "digest": ["not", "a", "dict"]})

    result = state.load_state(path)

    assert result["applied"] == applied
    assert result["sessions"] == sessions
    assert result["digest"] == {"pending": []}


def test_v2_file_with_one_malformed_applied_entry_drops_only_that_entry(tmp_path):
    """docs/DESIGN.md 7.5: namespace validation is recursive -- applied is a
    list of dicts with string rule_id, target, applied_at. A malformed entry
    inside an otherwise well-formed list drops only that entry; the rest of
    applied, and every other namespace, survive untouched."""
    path = tmp_path / "state.json"
    good_entry = {"rule_id": "R-GIT-CWD", "target": "t", "applied_at": "2026-01-01T00:00:00Z"}
    bad_entry = {"rule_id": "R-GIT-CWD", "target": 12345, "applied_at": "2026-01-01T00:00:00Z"}
    sessions = {"sess-1": {"units": {}, "last_check": "2026-01-01T00:00:00Z"}}
    _write_raw(
        path,
        {
            "version": 2,
            "min_reader": 2,
            "applied": [good_entry, bad_entry, 1, "not-a-dict"],
            "sessions": sessions,
            "digest": {"pending": []},
        },
    )

    result = state.load_state(path)

    assert result["applied"] == [good_entry]
    assert result["sessions"] == sessions


def test_v2_file_with_one_malformed_session_drops_only_that_session(tmp_path):
    """sessions is a dict of dicts; a value that is not itself a dict is
    dropped, the rest of sessions and every other namespace survive."""
    path = tmp_path / "state.json"
    good_session = {"units": {}, "last_check": "2026-01-01T00:00:00Z"}
    applied = [{"rule_id": "R-GIT-CWD", "target": "t", "applied_at": "2026-01-01T00:00:00Z"}]
    _write_raw(
        path,
        {
            "version": 2,
            "min_reader": 2,
            "applied": applied,
            "sessions": {"sess-good": good_session, "sess-bad": "not-a-dict"},
            "digest": {"pending": []},
        },
    )

    result = state.load_state(path)

    assert result["sessions"] == {"sess-good": good_session}
    assert result["applied"] == applied


def test_v2_file_with_non_string_digest_pending_entries_drops_only_those(tmp_path):
    """digest is a dict whose pending is a list of strings; a non-string
    item is dropped, the rest of pending is kept."""
    path = tmp_path / "state.json"
    applied = [{"rule_id": "R-GIT-CWD", "target": "t", "applied_at": "2026-01-01T00:00:00Z"}]
    _write_raw(
        path,
        {
            "version": 2,
            "min_reader": 2,
            "applied": applied,
            "sessions": {},
            "digest": {"pending": ["sess-1/agent-a0000000000000001", 42, None]},
        },
    )

    result = state.load_state(path)

    assert result["digest"] == {"pending": ["sess-1/agent-a0000000000000001"]}
    assert result["applied"] == applied


def test_well_formed_v2_file_round_trips_with_no_backup(tmp_path):
    path = tmp_path / "state.json"
    good = {
        "version": 2,
        "min_reader": 2,
        "applied": [{"rule_id": "R-GIT-CWD", "target": "t", "applied_at": "2026-01-01T00:00:00Z"}],
        "sessions": {"sess-1": {"units": {}, "last_check": "2026-01-01T00:00:00Z"}},
        "digest": {"pending": ["sess-1/agent-a0000000000000001"]},
    }
    _write_raw(path, good)

    result = state.load_state(path)

    assert result == good
    assert not list(tmp_path.glob("state.json.corrupt-*"))


# --- version newer than this reader supports ---------------------------------


def test_version_newer_than_reader_raises_state_too_new_and_leaves_file_untouched(tmp_path):
    path = tmp_path / "state.json"
    original_text = json.dumps({"version": 3, "min_reader": 3, "applied": [], "sessions": {}, "digest": {"pending": []}})
    path.write_text(original_text, encoding="utf-8")

    with pytest.raises(state.StateTooNew):
        state.load_state(path)

    assert path.read_text(encoding="utf-8") == original_text
    assert not list(tmp_path.glob("state.json.corrupt-*"))


def test_min_reader_above_current_raises_state_too_new_even_when_version_is_supported(tmp_path):
    """docs/DESIGN.md 7.5: min_reader is the writer's way of forcing every
    reader to upgrade even on a version this reader otherwise understands.
    Checked before any migration or repair, same as the version check."""
    path = tmp_path / "state.json"
    original_text = json.dumps(
        {"version": 2, "min_reader": 3, "applied": [], "sessions": {}, "digest": {"pending": []}}
    )
    path.write_text(original_text, encoding="utf-8")

    with pytest.raises(state.StateTooNew):
        state.load_state(path)

    assert path.read_text(encoding="utf-8") == original_text
    assert not list(tmp_path.glob("state.json.corrupt-*"))


def test_update_on_a_too_new_file_raises_and_never_writes_or_backs_up(tmp_path):
    path = tmp_path / "state.json"
    original_text = json.dumps({"version": 99, "min_reader": 99, "applied": [], "sessions": {}, "digest": {"pending": []}})
    path.write_text(original_text, encoding="utf-8")

    with pytest.raises(state.StateTooNew):
        state.update(lambda s: s.setdefault("applied", []).append({"x": 1}), path)

    assert path.read_text(encoding="utf-8") == original_text
    assert not list(tmp_path.glob("state.json.corrupt-*"))


# --- genuine filesystem failure, not corruption -------------------------------


def test_filesystem_error_on_an_existing_file_propagates_instead_of_resetting_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state.default_state()), encoding="utf-8")

    def _raise_open(self, *args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "open", _raise_open)

    with pytest.raises(OSError):
        state.load_state(path)

    # And, critically, no backup was written: this was never treated as
    # corruption in the first place.
    assert not list(tmp_path.glob("state.json.corrupt-*"))


# --- transaction(): the primitive update() is built on -----------------------


def test_transaction_writes_the_yielded_dict_on_normal_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    with state.transaction() as data:
        data["applied"].append({"rule_id": "R-X", "target": "t", "applied_at": state.now_iso()})

    reloaded = state.load_state()
    assert len(reloaded["applied"]) == 1
    assert reloaded["applied"][0]["rule_id"] == "R-X"


def test_transaction_writes_nothing_when_the_body_raises(tmp_path, monkeypatch):
    """docs/DESIGN.md 7.5/9: apply_rule --write runs entirely inside one
    transaction so a failure partway through (state locked, too new, or
    the caller's own error) never leaves a half-applied change on disk."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with state.transaction() as data:
            data["applied"].append({"rule_id": "R-X", "target": "t", "applied_at": state.now_iso()})
            raise _Boom("body failed after mutating the yielded dict")

    assert not state.state_path().exists()


# --- backups: created only under the lock, inside update()/transaction() -----


def test_corrupt_file_is_backed_up_by_update_not_by_load_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = state.state_path()
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")

    state.load_state()
    assert not list(path.parent.glob("state.json.corrupt-*"))  # read-only: no backup yet

    state.update(lambda s: None)
    backups = list(path.parent.glob("state.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "not json"


def test_backup_filename_collision_is_resolved_with_a_numeric_suffix(tmp_path, monkeypatch):
    """docs/DESIGN.md 7.5: the backup file is created with O_CREAT|O_EXCL
    and a numeric-suffix retry on FileExistsError, never check-then-create,
    so two backups landing in the same microsecond both survive."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(state, "_now_iso_micro", lambda: "20260101T000000.000000Z")

    path = state.state_path()
    path.parent.mkdir(parents=True)
    path.write_text("not json 1", encoding="utf-8")
    state.update(lambda s: None)  # backs up "not json 1", resets to a fresh default

    path.write_text("not json 2", encoding="utf-8")
    state.update(lambda s: None)  # same stamp: must not clobber the first backup

    backups = sorted(path.parent.glob("state.json.corrupt-20260101T000000.000000Z*"))
    assert len(backups) == 2
    assert {b.read_text(encoding="utf-8") for b in backups} == {"not json 1", "not json 2"}


def test_backup_failure_raises_state_error_and_resets_nothing(tmp_path, monkeypatch):
    """docs/DESIGN.md 7.5: if the backup cannot be created, update() raises
    StateError instead of silently resetting state it never actually
    preserved a copy of."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = state.state_path()
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")

    real_open = os.open

    def _flaky_open(file, flags, mode=0o777, *args, **kwargs):
        if ".corrupt-" in str(file):
            raise PermissionError("simulated: cannot create backup")
        return real_open(file, flags, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", _flaky_open)

    with pytest.raises(state.StateError):
        state.update(lambda s: None)

    assert path.read_text(encoding="utf-8") == "not json"  # never reset
    assert not list(path.parent.glob("state.json.corrupt-*"))


# --- symlinks and wrong-kind paths are rejected, never followed --------------


def test_symlinked_state_directory_is_rejected(tmp_path, monkeypatch):
    state_home = tmp_path / "xdg-state"
    state_home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (state_home / "agent-postmortem").symlink_to(elsewhere)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    with pytest.raises(state.StateError):
        state.update(lambda s: None)

    assert not (elsewhere / "state.json").exists()


def test_state_directory_path_occupied_by_a_plain_file_is_rejected(tmp_path, monkeypatch):
    state_home = tmp_path / "xdg-state"
    state_home.mkdir()
    (state_home / "agent-postmortem").write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    with pytest.raises(state.StateError):
        state.update(lambda s: None)


def test_symlinked_lock_file_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    directory = state.state_dir()
    directory.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch", encoding="utf-8")
    (directory / "state.lock").symlink_to(victim)

    with pytest.raises(state.StateError):
        state.update(lambda s: None)

    assert victim.read_text(encoding="utf-8") == "do not touch"


def test_lock_path_occupied_by_a_directory_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    directory = state.state_dir()
    directory.mkdir(parents=True)
    (directory / "state.lock").mkdir()

    with pytest.raises(state.StateError):
        state.update(lambda s: None)


# --- final permissions are verified, not just attempted ----------------------


def test_update_raises_state_error_when_final_permissions_cannot_be_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(os, "chmod", lambda path, mode: None)  # a chmod that silently does nothing

    with pytest.raises(state.StateError):
        state.update(lambda s: None)


# --- update(): directory/file permissions and locking -------------------------


def test_update_creates_state_dir_mode_0700_and_state_file_mode_0600(tmp_path, monkeypatch):
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    state.update(lambda s: s["applied"].append({"rule_id": "R-X", "target": "t", "applied_at": state.now_iso()}))

    directory = state.state_dir()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(state.state_path().stat().st_mode) == 0o600


def test_update_is_the_only_write_path_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    entry = {"rule_id": "R-GIT-CWD", "target": "/tmp/x.md", "applied_at": state.now_iso()}
    result = state.update(lambda s: s["applied"].append(entry))

    assert result["applied"] == [entry]
    reloaded = state.load_state()
    assert reloaded["applied"] == [entry]


def test_update_fsyncs_the_containing_directory_after_the_rename(tmp_path, monkeypatch):
    """docs/DESIGN.md 7.5: the temp file is fsynced, then renamed over
    state.json, then the containing directory is fsynced too, so the
    rename itself is durable (same pattern as rules.py's _fsync_dir)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    fsync_calls = []
    real_fsync = os.fsync

    def _tracking_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _tracking_fsync)

    state.update(lambda s: s["applied"].append({"rule_id": "R-X", "target": "t", "applied_at": state.now_iso()}))

    # One fsync for the temp file's own contents, and a second for the
    # directory entry created by the rename (fd numbers may be reused
    # once closed, so this counts calls, not distinct fds).
    assert len(fsync_calls) >= 2


def test_two_sequential_updates_of_different_namespaces_both_survive(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    state.update(lambda s: s["applied"].append({"rule_id": "R-GIT-CWD", "target": "t", "applied_at": state.now_iso()}))
    state.update(
        lambda s: s["sessions"].update({"sess-1": {"units": {}, "last_check": state.now_iso()}})
    )

    result = state.load_state()
    assert len(result["applied"]) == 1
    assert "sess-1" in result["sessions"]


def test_update_raises_state_locked_when_lock_already_held(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(state, "LOCK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(state, "LOCK_POLL_SECONDS", 0.02)

    path = state.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / "state.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(state.StateLocked):
            state.update(lambda s: None)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    # The lock being held the whole time means nothing was written.
    assert not path.exists()


def test_non_contention_flock_error_propagates_immediately_as_state_error(tmp_path, monkeypatch):
    """docs/DESIGN.md 7.5: only BlockingIOError/EAGAIN/EACCES from flock are
    contention and get retried; any other OSError (EBADF, EINVAL, ...) is a
    real error and must propagate right away as StateError, not be retried
    for the full 2s timeout and misreported as StateLocked."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(state, "LOCK_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(state, "LOCK_POLL_SECONDS", 0.02)

    import fcntl as fcntl_module

    acquire_attempts = []
    real_flock = fcntl_module.flock

    def _raise_ebadf(fd, op):
        if op == fcntl_module.LOCK_EX | fcntl_module.LOCK_NB:
            acquire_attempts.append((fd, op))
            raise OSError(9, "Bad file descriptor")  # EBADF, never a contention errno
        return real_flock(fd, op)  # let the cleanup LOCK_UN through untouched

    monkeypatch.setattr(fcntl_module, "flock", _raise_ebadf)

    start = time.monotonic()
    with pytest.raises(state.StateError) as excinfo:
        state.update(lambda s: None)
    elapsed = time.monotonic() - start

    assert not isinstance(excinfo.value, state.StateLocked)
    assert len(acquire_attempts) == 1  # never retried
    assert elapsed < 1.0  # never waited out the (5s) timeout


# --- real cross-process contention --------------------------------------------


def test_concurrent_subprocess_updates_lose_no_increment(tmp_path):
    """4 subprocesses each run 25 state.update() increments against the
    same 25 shared counters (100 increments total, 4 per counter). If
    update()'s locking ever let two processes interleave a read-modify-
    write, some increments would be lost and a counter would end below 4.

    A start barrier (each worker signals ready, then spins on a 'go' file
    the parent creates only once all 4 have signalled) plus a 5ms sleep
    inside the locked mutation force real overlap: without both, 4 fast
    processes can simply never contend for the lock at the same instant,
    and this test would pass even against a broken, unlocked update()."""
    state_home = tmp_path / "xdg-state"
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    go_file = tmp_path / "go"
    ready_files = [tmp_path / f"ready-{i}" for i in range(4)]

    worker_script = textwrap.dedent(
        f"""
        import os
        os.environ["XDG_STATE_HOME"] = {str(state_home)!r}
        import sys
        import time
        sys.path.insert(0, {src_dir!r})
        from agent_postmortem import state

        idx = sys.argv[1]
        with open({str(tmp_path)!r} + f"/ready-{{idx}}", "w") as fh:
            fh.write("ready")
        go_file = {str(go_file)!r}
        while not os.path.exists(go_file):
            time.sleep(0.005)

        def bump(key):
            def fn(current):
                counters = current.setdefault("sessions", {{}}).setdefault("_counters", {{}})
                counters[key] = counters.get(key, 0) + 1
                time.sleep(0.005)  # widen the window; still holding the lock
            return fn

        for i in range(25):
            state.update(bump(f"counter-{{i}}"))
        """
    )

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", worker_script, str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for i in range(4)
    ]

    deadline = time.monotonic() + 10.0
    while not all(f.exists() for f in ready_files):
        if time.monotonic() >= deadline:
            for proc in procs:
                proc.kill()
            raise AssertionError("not all 4 worker subprocesses signalled ready in time")
        time.sleep(0.01)
    go_file.write_text("go", encoding="utf-8")

    outputs = [proc.communicate(timeout=30) for proc in procs]
    for proc, (_out, err) in zip(procs, outputs):
        assert proc.returncode == 0, err.decode("utf-8", "replace")

    result = state.load_state(state_home / "agent-postmortem" / "state.json")
    counters = result["sessions"]["_counters"]
    assert len(counters) == 25
    assert all(count == 4 for count in counters.values()), counters
    assert sum(counters.values()) == 100
