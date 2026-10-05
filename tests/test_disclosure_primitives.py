"""N3a: disclosure levels and stable aliases, the primitives only (docs/
DESIGN.md 4.13.5, 4.13.11's "N3, disclosure" items, 7.5). Covers
agent_postmortem.disclosure (canonical id checks, HMAC, registry schema
validation, assignment, the P6 initialization state machine, AliasBook, the
positional fallback, and single-locked-registration batching) and the new
secret/registry primitives added to agent_postmortem.state.

N3a changes no command output -- nothing here is wired into a renderer, a
CLI flag, a hook, or `watch` (that is N3b) -- so every test here exercises
disclosure.py and state.py directly, never the CLI.

Every test uses its own tmp_path state directory, either via an explicit
state_dir= argument or via XDG_STATE_HOME (monkeypatch.setenv); never the
real state directory agent-postmortem would use on this machine. A test
that needs several distinct canonical ids builds them at run time (hex
formatting, uuid.UUID(int=n)) rather than spelling a contiguous UUID or
agent id out as a literal, so tests/test_privacy.py's repository-wide scan
never has anything to catch in this file's own tracked source.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import sys
import textwrap
import time
import uuid as uuid_module
from pathlib import Path

import pytest

from agent_postmortem import disclosure, state

# --- id builders: assembled at run time, never a contiguous literal --------


def _agent_id(n: int) -> str:
    return format(n, "017x")


def _session_id(n: int) -> str:
    return str(uuid_module.UUID(int=n))


def _project_id(n: int) -> str:
    return f"project-{n}"


HEX64_A = "a" * 64
HEX64_B = "b" * 64


def _hand_registry(**tables) -> dict:
    reg = disclosure.empty_registry()
    reg.update(tables)
    return reg


def _write_raw(path: Path, data: bytes) -> None:
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns


# ============================================================================
# canonical id checks (P2)
# ============================================================================


def test_canonical_agent_id_is_17_lowercase_hex():
    assert disclosure.is_canonical_id("agent", _agent_id(1)) is True


@pytest.mark.parametrize(
    "value",
    [
        "A" * 17,  # uppercase
        "a" * 16,  # too short
        "a" * 18,  # too long
        "g" * 17,  # non-hex char
        "",
        None,
        12345,
    ],
)
def test_non_canonical_agent_id_is_rejected(value):
    assert disclosure.is_canonical_id("agent", value) is False


def test_canonical_session_id_is_a_uuid():
    assert disclosure.is_canonical_id("session", _session_id(1)) is True


_SESSION_WITH_HEX_LETTERS = str(uuid_module.UUID(int=0xA1B2C3))


@pytest.mark.parametrize(
    "value",
    [
        _SESSION_WITH_HEX_LETTERS.upper(),
        _session_id(1).replace("-", ""),
        "{" + _session_id(1) + "}",
        "not-a-uuid-at-all",
        "",
        None,
        12345,
    ],
)
def test_non_canonical_session_id_is_rejected(value):
    assert disclosure.is_canonical_id("session", value) is False


def test_canonical_project_id_is_a_non_empty_string():
    assert disclosure.is_canonical_id("project", _project_id(1)) is True


@pytest.mark.parametrize("value", ["", "has\x00nul", "has\nnewline", None, 12345])
def test_non_canonical_project_id_is_rejected(value):
    assert disclosure.is_canonical_id("project", value) is False


def test_is_canonical_id_rejects_unknown_kind():
    with pytest.raises(ValueError):
        disclosure.is_canonical_id("phase", "whatever")


@pytest.mark.parametrize(
    "kind,value",
    [("agent", "not-canonical"), ("session", "not-a-uuid"), ("project", "")],
)
def test_require_canonical_id_raises_value_error_for_a_bad_id(kind, value):
    with pytest.raises(ValueError):
        disclosure.require_canonical_id(kind, value)


def test_require_canonical_id_returns_the_value_for_a_good_id():
    agent = _agent_id(1)
    assert disclosure.require_canonical_id("agent", agent) == agent


# ============================================================================
# HMAC (P1)
# ============================================================================


def test_hmac_hex_is_deterministic_lowercase_64_hex():
    secret = b"x" * 32
    h = disclosure.hmac_hex(secret, _agent_id(1))
    assert len(h) == 64
    assert h == h.lower()
    assert all(c in "0123456789abcdef" for c in h)
    assert disclosure.hmac_hex(secret, _agent_id(1)) == h


def test_hmac_hex_differs_for_different_secrets():
    agent = _agent_id(1)
    assert disclosure.hmac_hex(b"x" * 32, agent) != disclosure.hmac_hex(b"y" * 32, agent)


# ============================================================================
# registry schema validation (P4)
# ============================================================================


def test_empty_registry_is_valid():
    assert disclosure.validate_registry(disclosure.empty_registry()) == disclosure.empty_registry()


def test_registry_with_one_valid_entry_per_kind_is_valid():
    reg = _hand_registry(agent={HEX64_A: 12}, session={HEX64_B: 13}, project={HEX64_A: 64})
    assert disclosure.validate_registry(reg) == reg


@pytest.mark.parametrize(
    "data",
    [
        ["not", "a", "dict"],
        "not-a-dict",
        None,
        {"version": 2, "agent": {}, "session": {}, "project": {}},  # wrong version
        {"agent": {}, "session": {}, "project": {}},  # missing version
        {"version": 1, "agent": {}, "session": {}, "project": {}, "extra": {}},  # extra key
        {"version": 1, "agent": {}, "session": {}},  # missing project table
        {"version": 1, "agent": [], "session": {}, "project": {}},  # table not a dict
    ],
)
def test_malformed_top_level_registry_shapes_are_rejected(data):
    assert disclosure.validate_registry(data) is None


@pytest.mark.parametrize("version", [True, 1.0])
def test_non_int_version_equal_to_one_is_rejected(version):
    """N3a fix round 2, Finding 6: `data.get("version") != 1` accepts both
    `True` (bool is an int subclass in Python, True == 1) and `1.0` (float
    equality). Only the exact int 1 is version 1. (`False` and the string
    "1" are already rejected by the plain `!=` check today, for an
    incidental reason -- see test_version_not_equal_to_one_is_rejected --
    so they are not part of this RED case.)"""
    reg = disclosure.empty_registry()
    reg["version"] = version
    assert disclosure.validate_registry(reg) is None


@pytest.mark.parametrize("version", [False, "1"])
def test_version_not_equal_to_one_is_rejected(version):
    """Companion to test_non_int_version_equal_to_one_is_rejected above:
    these two already fail validate_registry's plain `!=` comparison today
    (False == 0, "1" != 1 as a string/int comparison) and must keep doing
    so once the stricter `type(version) is int and version == 1` check
    (finding 6) replaces it."""
    reg = disclosure.empty_registry()
    reg["version"] = version
    assert disclosure.validate_registry(reg) is None


def test_uppercase_key_is_rejected():
    reg = _hand_registry(agent={HEX64_A.upper(): 12})
    assert disclosure.validate_registry(reg) is None


def test_short_key_is_rejected():
    reg = _hand_registry(agent={"a" * 63: 12})
    assert disclosure.validate_registry(reg) is None


def test_long_key_is_rejected():
    reg = _hand_registry(agent={"a" * 65: 12})
    assert disclosure.validate_registry(reg) is None


def test_non_hex_key_is_rejected():
    reg = _hand_registry(agent={"g" * 64: 12})
    assert disclosure.validate_registry(reg) is None


def test_bool_length_is_rejected():
    reg = _hand_registry(agent={HEX64_A: True})
    assert disclosure.validate_registry(reg) is None


@pytest.mark.parametrize("value", [True, False])
def test_bool_length_is_rejected_even_when_the_bounds_alone_would_accept_it(monkeypatch, value):
    """N3a review Finding 2 / mutation M6: with the real MIN_ALIAS_LEN of 12,
    every bool is already out of range (True == 1, False == 0), so
    test_bool_length_is_rejected above passes even without the dedicated
    isinstance(length, bool) check -- the very next line's range check
    rejects it anyway, for the wrong reason. Widen MIN_ALIAS_LEN to 0 so 0
    and 1 are in range, and only the bool check itself can still reject
    them."""
    monkeypatch.setattr(disclosure, "MIN_ALIAS_LEN", 0)
    reg = _hand_registry(agent={HEX64_A: value})
    assert disclosure.validate_registry(reg) is None


@pytest.mark.parametrize("length", [11, 65, 0, -1, 1000])
def test_out_of_range_length_is_rejected(length):
    reg = _hand_registry(agent={HEX64_A: length})
    assert disclosure.validate_registry(reg) is None


@pytest.mark.parametrize("length", [12, 64])
def test_boundary_lengths_are_accepted(length):
    reg = _hand_registry(agent={HEX64_A: length})
    assert disclosure.validate_registry(reg) is not None


def test_duplicate_alias_within_one_kind_is_rejected():
    # Both keys share the same first-12 prefix once truncated to length 12.
    key_a = "a" * 64
    key_b = "a" * 12 + "b" * 52
    reg = _hand_registry(agent={key_a: 12, key_b: 12})
    assert disclosure.validate_registry(reg) is None


def test_equal_alias_across_different_kinds_is_allowed():
    reg = _hand_registry(agent={HEX64_A: 12}, session={HEX64_A: 12})
    assert disclosure.validate_registry(reg) is not None


# ============================================================================
# secret: file-level probe matrix (state.py)
# ============================================================================


def test_secret_absent_is_reported_absent(tmp_path):
    probe, data = state.probe_secret(tmp_path / "secret")
    assert probe is state.Probe.ABSENT
    assert data is None


def test_create_secret_is_32_bytes_mode_0600(tmp_path):
    path = tmp_path / "secret"
    data = state.create_secret(path)
    assert len(data) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    probe, read_back = state.probe_secret(path)
    assert probe is state.Probe.VALID
    assert read_back == data


def test_create_secret_refuses_to_overwrite_an_existing_one(tmp_path):
    path = tmp_path / "secret"
    first = state.create_secret(path)
    with pytest.raises(FileExistsError):
        state.create_secret(path)
    probe, data = state.probe_secret(path)
    assert probe is state.Probe.VALID
    assert data == first


def test_secret_symlink_is_invalid_and_untouched(tmp_path):
    real = tmp_path / "elsewhere.bin"
    real.write_bytes(b"x" * 32)
    link = tmp_path / "secret"
    link.symlink_to(real)
    probe, data = state.probe_secret(link)
    assert probe is state.Probe.INVALID
    assert data is None
    assert real.read_bytes() == b"x" * 32  # never followed, never touched


def test_secret_symlink_to_a_target_passing_every_other_check_is_still_invalid(tmp_path):
    """N3a review Finding 1 / mutation M1: the symlink target above
    (world-default mode, not owned-and-0600) is accidentally also rejected
    by the mode check, so it cannot tell O_NOFOLLOW apart from the mode/uid/
    size checks below it. Build a target with create_secret itself -- 0600,
    this uid, exactly 32 bytes -- so only O_NOFOLLOW can still produce
    INVALID here."""
    real = tmp_path / "elsewhere-secret"
    target_data = state.create_secret(real)
    link = tmp_path / "secret"
    link.symlink_to(real)

    probe, data = state.probe_secret(link)

    assert probe is state.Probe.INVALID
    assert data is None
    assert real.read_bytes() == target_data  # never followed, never touched


def test_secret_directory_is_invalid(tmp_path):
    path = tmp_path / "secret"
    path.mkdir()
    probe, data = state.probe_secret(path)
    assert probe is state.Probe.INVALID
    assert data is None


def test_secret_fifo_is_invalid_and_does_not_block(tmp_path):
    path = tmp_path / "secret"
    os.mkfifo(path)
    probe, data = state.probe_secret(path)  # must return promptly, never block
    assert probe is state.Probe.INVALID
    assert data is None


def test_secret_wrong_mode_is_invalid(tmp_path):
    path = tmp_path / "secret"
    state.create_secret(path)
    os.chmod(path, 0o644)
    probe, data = state.probe_secret(path)
    assert probe is state.Probe.INVALID
    assert data is None


def test_secret_wrong_owner_is_invalid(tmp_path, monkeypatch):
    path = tmp_path / "secret"
    state.create_secret(path)
    monkeypatch.setattr(state, "_current_uid", lambda: os.getuid() + 1)
    probe, data = state.probe_secret(path)
    assert probe is state.Probe.INVALID
    assert data is None


def test_secret_wrong_size_is_invalid(tmp_path):
    path = tmp_path / "secret"
    _write_raw(path, b"x" * 16)
    os.chmod(path, 0o600)
    probe, data = state.probe_secret(path)
    assert probe is state.Probe.INVALID
    assert data is None


def test_secret_probe_metadata_comes_from_the_open_descriptor_not_the_path(tmp_path, monkeypatch):
    """N3a fix round 2, Finding 9: probe_secret must classify and read the
    secret from the descriptor it already opened, never from a second,
    separate look at the path -- otherwise a TOCTOU swap of the file right
    after open would be observed. Swap the path's content and mode
    immediately after the real open, to something that would read as
    INVALID by path (wrong mode, wrong size); the already-open descriptor
    still refers to the original, valid inode, so a correct probe_secret
    must still report the original data. Mutation M12 (state.fstat(fd) -> path.stat())
    fails this test; the unmodified implementation passes it."""
    path = tmp_path / "secret"
    original = state.create_secret(path)
    real_open_probe_fd = state._open_probe_fd

    def _swap_after_open(p, *, dir_fd=None):
        fd = real_open_probe_fd(p, dir_fd=dir_fd)
        if fd is not None and Path(p) == path:
            os.unlink(str(path))
            _write_raw(path, b"y" * 16)  # wrong size by path
            os.chmod(path, 0o644)  # wrong mode by path
        return fd

    monkeypatch.setattr(state, "_open_probe_fd", _swap_after_open)

    probe, data = state.probe_secret(path)

    assert probe is state.Probe.VALID
    assert data == original


def test_create_secret_fails_before_commit_when_the_open_descriptor_reports_the_wrong_mode(
    tmp_path, monkeypatch
):
    """N3a fix round 2, Finding 11 (test half, paired with Finding 1): inject
    a wrong mode on the just-opened descriptor (simulating a filesystem
    that silently granted a different mode than requested) and assert the
    call fails before the secret is left on disk -- proving the mode check
    really runs on the open descriptor, pre-commit, rather than trusting
    the os.open() mode argument. Mutation M17 (remove this check) fails
    this test; the unmodified implementation passes it."""
    path = tmp_path / "secret"
    real_open = os.open
    real_fstat = os.fstat
    target_fd: dict[str, int | None] = {"fd": None}

    def _tracking_open(p, flags, mode=0o777, *args, **kwargs):
        fd = real_open(p, flags, mode, *args, **kwargs)
        if str(p) == str(path):
            target_fd["fd"] = fd
        return fd

    def _tampering_fstat(fd):
        st = real_fstat(fd)
        if fd == target_fd["fd"]:
            tampered_mode = stat.S_IFREG | 0o644  # the filesystem "granted" 0644, not 0600
            return os.stat_result((tampered_mode,) + tuple(st)[1:])
        return st

    monkeypatch.setattr(os, "open", _tracking_open)
    monkeypatch.setattr(os, "fstat", _tampering_fstat)

    with pytest.raises(state.StateError):
        state.create_secret(path)

    assert not path.exists()  # pre-commit failure: nothing left behind


def test_write_registry_bytes_fails_before_commit_when_the_temp_descriptor_reports_the_wrong_mode(
    tmp_path, monkeypatch
):
    """N3a fix round 2, Finding 11 (test half, paired with Finding 1),
    registry side: same proof as the secret above, against the temp file's
    own descriptor. Mutation M18 (remove this check) fails this test; the
    unmodified implementation passes it."""
    path = tmp_path / "aliases.json"
    real_open = os.open
    real_fstat = os.fstat
    target_fd: dict[str, int | None] = {"fd": None}

    def _tracking_open(p, flags, mode=0o777, *args, **kwargs):
        fd = real_open(p, flags, mode, *args, **kwargs)
        if ".aliases.json.tmp" in str(p):
            target_fd["fd"] = fd
        return fd

    def _tampering_fstat(fd):
        st = real_fstat(fd)
        if fd == target_fd["fd"]:
            tampered_mode = stat.S_IFREG | 0o644
            return os.stat_result((tampered_mode,) + tuple(st)[1:])
        return st

    monkeypatch.setattr(os, "open", _tracking_open)
    monkeypatch.setattr(os, "fstat", _tampering_fstat)

    with pytest.raises(state.StateError):
        state.write_registry_bytes(path, b'{"ok": true}\n')

    assert not path.exists()  # pre-commit failure: never replaced into place
    assert list(tmp_path.glob("*.tmp*")) == []  # and its own temp file is gone


def test_a_second_process_reads_the_same_secret(tmp_path):
    state_home = tmp_path / "xdg-state"
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    script = textwrap.dedent(
        f"""
        import os, sys
        os.environ["XDG_STATE_HOME"] = {str(state_home)!r}
        sys.path.insert(0, {src_dir!r})
        from agent_postmortem import state
        book_dir = state.state_dir()
        probe, data = state.probe_secret(state.secret_path(book_dir))
        sys.stdout.write(data.hex() if probe is state.Probe.VALID else "NONE")
        """
    )
    directory = state_home / "agent-postmortem"
    directory.mkdir(parents=True)
    created = state.create_secret(state.secret_path(directory))
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout == created.hex()


# ============================================================================
# registry: file-level probe matrix (state.py) + schema (disclosure.py)
# ============================================================================


def test_registry_absent_is_reported_absent(tmp_path):
    probe, data = state.probe_registry_bytes(tmp_path / "aliases.json", max_bytes=4096)
    assert probe is state.Probe.ABSENT
    assert data is None


def test_registry_symlink_is_invalid(tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "aliases.json"
    link.symlink_to(real)
    probe, data = state.probe_registry_bytes(link, max_bytes=4096)
    assert probe is state.Probe.INVALID
    assert real.read_text(encoding="utf-8") == "{}"


def test_registry_symlink_to_a_target_passing_every_other_check_is_still_invalid(tmp_path):
    """N3a review Finding 1 / mutation M1, registry side: build a target
    with write_registry_bytes itself -- regular, 0600, this uid, well
    within max_bytes -- so only O_NOFOLLOW can still produce INVALID for a
    symlink pointed at it."""
    real = tmp_path / "elsewhere.json"
    content = disclosure._dumps(disclosure.empty_registry())
    state.write_registry_bytes(real, content)
    link = tmp_path / "aliases.json"
    link.symlink_to(real)

    probe, data = state.probe_registry_bytes(link, max_bytes=4096)

    assert probe is state.Probe.INVALID
    assert data is None
    assert real.read_bytes() == content  # never followed, never touched


def test_registry_directory_is_invalid(tmp_path):
    path = tmp_path / "aliases.json"
    path.mkdir()
    probe, data = state.probe_registry_bytes(path, max_bytes=4096)
    assert probe is state.Probe.INVALID


def test_registry_fifo_is_invalid_and_does_not_block(tmp_path):
    path = tmp_path / "aliases.json"
    os.mkfifo(path)
    probe, data = state.probe_registry_bytes(path, max_bytes=4096)
    assert probe is state.Probe.INVALID


def test_registry_wrong_mode_is_invalid(tmp_path):
    path = tmp_path / "aliases.json"
    state.write_registry_bytes(path, b"{}")
    os.chmod(path, 0o644)
    probe, data = state.probe_registry_bytes(path, max_bytes=4096)
    assert probe is state.Probe.INVALID


def test_registry_wrong_owner_is_invalid(tmp_path, monkeypatch):
    path = tmp_path / "aliases.json"
    state.write_registry_bytes(path, b"{}")
    monkeypatch.setattr(state, "_current_uid", lambda: os.getuid() + 1)
    probe, data = state.probe_registry_bytes(path, max_bytes=4096)
    assert probe is state.Probe.INVALID


def test_registry_oversize_bytes_is_invalid(tmp_path):
    path = tmp_path / "aliases.json"
    state.write_registry_bytes(path, b"x" * 100)
    probe, data = state.probe_registry_bytes(path, max_bytes=50)
    assert probe is state.Probe.INVALID


def test_registry_malformed_json_is_invalid_via_disclosure_probe(tmp_path):
    directory = tmp_path / "agent-postmortem"
    directory.mkdir()
    state.write_registry_bytes(state.registry_path(directory), b"{not json")
    probe, registry = disclosure._probe_registry(directory)
    assert probe is state.Probe.INVALID
    assert registry is None


def test_registry_wrong_schema_is_invalid_via_disclosure_probe(tmp_path):
    directory = tmp_path / "agent-postmortem"
    directory.mkdir()
    bad = json.dumps({"version": 1, "agent": {}, "session": {}}).encode("utf-8")
    state.write_registry_bytes(state.registry_path(directory), bad)
    probe, registry = disclosure._probe_registry(directory)
    assert probe is state.Probe.INVALID


def test_registry_recursion_error_is_invalid_via_disclosure_probe(tmp_path):
    """N3a fix round 2, Finding 3: `_probe_registry` only catches
    `UnicodeDecodeError`/`JSONDecodeError` around `json.loads`. 10,000
    nested JSON arrays are only 20,000 bytes -- well under MAX_BYTES -- but
    raise `RecursionError` while parsing. That must classify the registry
    as invalid, not propagate."""
    directory = tmp_path / "agent-postmortem"
    directory.mkdir()
    nested = (b"[" * 10_000) + (b"]" * 10_000)
    state.write_registry_bytes(state.registry_path(directory), nested)
    before = state.registry_path(directory).read_bytes()
    before_mtime = _mtime_ns(state.registry_path(directory))

    probe, registry = disclosure._probe_registry(directory)

    assert probe is state.Probe.INVALID
    assert registry is None
    assert state.registry_path(directory).read_bytes() == before
    assert _mtime_ns(state.registry_path(directory)) == before_mtime


def test_registry_probe_metadata_comes_from_the_open_descriptor_not_the_path(tmp_path, monkeypatch):
    """N3a fix round 2, Finding 9, registry side: same proof as the secret
    test above. Mutation M16 (state.fstat(fd) -> path.stat()) fails this
    test; the unmodified implementation passes it."""
    path = tmp_path / "aliases.json"
    content = disclosure._dumps(disclosure.empty_registry())
    state.write_registry_bytes(path, content)
    real_open_probe_fd = state._open_probe_fd

    def _swap_after_open(p, *, dir_fd=None):
        fd = real_open_probe_fd(p, dir_fd=dir_fd)
        if fd is not None and Path(p) == path:
            os.unlink(str(path))
            _write_raw(path, b"not json at all, and also the wrong mode")
            os.chmod(path, 0o644)
        return fd

    monkeypatch.setattr(state, "_open_probe_fd", _swap_after_open)

    probe, data = state.probe_registry_bytes(path, max_bytes=4096)

    assert probe is state.Probe.VALID
    assert data == content


def test_write_registry_bytes_does_not_clobber_a_preexisting_predictable_temp_name(tmp_path):
    """N3a fix round 2, Finding 10: the registry's own temp file name is
    predictable (prefix plus this process's pid). Pre-create a sentinel at
    that exact name and prove the writer never opens it non-exclusively: it
    must retry with a numeric suffix instead, leaving the sentinel
    untouched. Mutation M13 (drop O_EXCL) fails this test -- it would
    truncate and overwrite the sentinel instead of raising FileExistsError;
    the unmodified implementation passes it."""
    path = tmp_path / "aliases.json"
    predictable = tmp_path / f".aliases.json.tmp-{os.getpid()}"
    sentinel = b"SENTINEL-DO-NOT-TOUCH"
    _write_raw(predictable, sentinel)

    state.write_registry_bytes(path, b'{"ok": true}\n')

    assert predictable.read_bytes() == sentinel
    assert path.read_bytes() == b'{"ok": true}\n'
    leftover = [p for p in tmp_path.glob(".aliases.json.tmp-*") if p != predictable]
    assert leftover == []


def test_write_registry_bytes_is_atomic_and_leaves_no_temp_file_on_success(tmp_path):
    path = tmp_path / "aliases.json"
    state.write_registry_bytes(path, b'{"ok": true}\n')
    assert path.read_bytes() == b'{"ok": true}\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.glob("*.tmp*")) == []


def test_write_registry_bytes_failure_leaves_no_temp_file_and_no_change(tmp_path, monkeypatch):
    path = tmp_path / "aliases.json"
    state.write_registry_bytes(path, b"original")

    def _boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        state.write_registry_bytes(path, b"new content")

    assert path.read_bytes() == b"original"
    assert list(tmp_path.glob("*.tmp*")) == []


# ============================================================================
# N3a fix round 2: Finding 1 (BLOCKER) and Finding 2 (short write)
# ============================================================================


def test_create_secret_treats_a_persistent_zero_length_write_as_an_error(tmp_path, monkeypatch):
    path = tmp_path / "secret"
    monkeypatch.setattr(os, "write", lambda fd, data: 0)

    with pytest.raises(state.StateError):
        state.create_secret(path)

    assert not path.exists()


def test_create_secret_completes_a_short_write_instead_of_reporting_it_as_valid(
    tmp_path, monkeypatch
):
    path = tmp_path / "secret"
    real_write = os.write
    calls = {"n": 0}

    def _short_once(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, bytes(data)[:16])
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", _short_once)

    secret = state.create_secret(path)

    assert len(secret) == 32
    assert path.read_bytes() == secret


def test_write_registry_bytes_treats_a_persistent_zero_length_write_as_an_error(
    tmp_path, monkeypatch
):
    path = tmp_path / "aliases.json"
    monkeypatch.setattr(os, "write", lambda fd, data: 0)

    with pytest.raises(state.StateError):
        state.write_registry_bytes(path, b'{"ok": true}\n')

    assert not path.exists()
    assert list(path.parent.glob("*.tmp*")) == []


def test_write_registry_bytes_completes_a_short_write_instead_of_reporting_it_as_valid(
    tmp_path, monkeypatch
):
    """Registry-side companion to the secret's short-write test above (N3a
    fix round 2, Finding 2): now that the registry write goes through the
    same raw os.write() loop as the secret, a short underlying write is
    completed rather than silently accepted as a truncated file."""
    path = tmp_path / "aliases.json"
    data = disclosure._dumps(disclosure.empty_registry())
    real_write = os.write
    calls = {"n": 0}

    def _short_once(fd, chunk):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, bytes(chunk)[:4])
        return real_write(fd, chunk)

    monkeypatch.setattr(os, "write", _short_once)

    state.write_registry_bytes(path, data)

    assert path.read_bytes() == data


def test_secret_directory_fsync_failure_after_commit_does_not_lie_about_what_happened(
    tmp_path, monkeypatch
):
    """N3a fix round 2 (continued), new Finding 1 (BLOCKER): this test used
    to assert that a *persistent* directory-fsync failure after creating
    the secret still returned available=True, on the theory that
    create_secret() itself had already committed and a read-back proves
    the content is fine. The reviewer's round-2 correction is that a
    read-back only proves the namespace change is visible right now, not
    that the directory entry making it visible would survive a crash: a
    persistent failure here must fall back, exactly like any other
    unavailable outcome, leaving the real secret and registry as a
    positional-only render would. This is a contract correction, not a
    weakened test -- the old assertion was the bug the BLOCKER describes."""
    directory = tmp_path

    def _boom(path, *, dir_fd=None):
        raise OSError("simulated directory fsync failure, content already committed")

    monkeypatch.setattr(state, "fsync_directory", _boom)

    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)

    assert book.available is False
    assert book.alias("agent", _agent_id(1)) == "agent-1"


def test_registry_post_commit_reverification_glitch_does_not_lie_about_what_happened(
    tmp_path, monkeypatch
):
    """Distinguish a durability/verification glitch (the registry's new
    content is genuinely fine on disk; only the post-commit recheck itself
    had a transient problem) from an actual persistent corruption: the
    fake here raises without touching the real file at all, so a correct
    implementation's re-probe of the real file finds it valid and returns
    the committed result.

    N3a fix round 2 (continued), new Finding 6: the post-commit check this
    glitch targets is now state._verify_registry_after_commit (reopened
    with O_NOFOLLOW), not the old by-path _verify_mode call -- updated to
    monkeypatch the call site write_registry_bytes actually makes."""
    directory = tmp_path
    first = disclosure.register([("agent", _agent_id(1))], state_dir=directory)
    assert first.available is True

    def _glitch_once_for_the_registry(path, expected, *, dir_fd=None):
        raise state.StateError("simulated transient verification glitch")

    monkeypatch.setattr(state, "_verify_registry_after_commit", _glitch_once_for_the_registry)

    second = disclosure.register([("agent", _agent_id(2))], state_dir=directory)

    assert second.available is True
    on_disk = json.loads(state.registry_path(directory).read_text(encoding="utf-8"))
    validated = disclosure.validate_registry(on_disk)
    assert validated is not None
    assert disclosure.count_entries(validated) == 2


def test_persistent_post_commit_directory_fsync_failure_falls_back_and_does_not_loop_forever(
    tmp_path, monkeypatch
):
    """N3a fix round 2 (continued), new Finding 1 (BLOCKER): a cold-start
    registration (absent registry, absent secret) whose post-commit
    directory fsync keeps failing on every attempt must fall back
    (available=False) -- the old assertion that this "still recovers" was
    exactly the bug the BLOCKER describes: the secret and registry being
    readable right now says nothing about whether the directory entry that
    makes them readable would survive a crash. The number of attempts stays
    bounded either way (no unbounded recursion); this is a contract
    correction, not a weakened test."""
    directory = tmp_path
    attempts = {"n": 0}

    def _always_boom(path, *, dir_fd=None):
        attempts["n"] += 1
        raise OSError("simulated persistent durability failure")

    monkeypatch.setattr(state, "fsync_directory", _always_boom)

    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)

    assert book.available is False
    assert attempts["n"] <= 2  # bounded, not unbounded recursion
    assert book.alias("agent", _agent_id(1)) == "agent-1"


def test_persistent_directory_fsync_failure_on_an_already_valid_pair_falls_back(
    tmp_path, monkeypatch
):
    """N3a fix round 2 (continued), new Finding 1 (BLOCKER), the other half:
    the BLOCKER is not limited to the cold-start creation path -- P6's own
    "normal" row (both files already valid) must also require a fresh,
    successful directory fsync before handing out stable aliases, because a
    later process reading an already-committed pair cannot tell a
    genuinely durable commit from one whose directory fsync failed right
    after the file became visible. A persistent failure here falls back
    and leaves the real registry exactly as it was."""
    directory = tmp_path
    first = disclosure.register([("agent", _agent_id(1))], state_dir=directory)
    assert first.available is True
    before = state.registry_path(directory).read_bytes()
    before_mtime = _mtime_ns(state.registry_path(directory))

    def _always_boom(path, *, dir_fd=None):
        raise OSError("simulated persistent directory fsync failure")

    monkeypatch.setattr(state, "fsync_directory", _always_boom)

    second = disclosure.register([("agent", _agent_id(2))], state_dir=directory)

    assert second.available is False
    assert state.registry_path(directory).read_bytes() == before
    assert _mtime_ns(state.registry_path(directory)) == before_mtime


def test_finalize_registration_persistent_directory_fsync_failure_after_recovery_falls_back(
    tmp_path, monkeypatch
):
    """N3a fix round 2 (continued), new Finding 1 (BLOCKER), the registry-
    rename half named explicitly in the review: 'the same issue exists
    after a registry rename: _finalize_registration accepts parsed-dict
    equality after PostCommitVerificationError without establishing
    directory durability.' Make write_registry_bytes actually commit the
    correct content (so the content-equality re-probe alone would wrongly
    accept it) and then raise PostCommitVerificationError, while the
    directory fsync a correct recovery must also attempt keeps failing."""
    directory = tmp_path
    first = disclosure.register([("agent", _agent_id(1))], state_dir=directory)
    assert first.available is True

    def _commit_for_real_then_raise(path, data, *, dir_fd=None):
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        raise state.PostCommitVerificationError(
            "simulated post-commit failure; content on disk is actually fine"
        )

    monkeypatch.setattr(state, "write_registry_bytes", _commit_for_real_then_raise)

    def _always_boom(path, *, dir_fd=None):
        raise OSError("simulated persistent directory fsync failure")

    monkeypatch.setattr(state, "fsync_directory", _always_boom)

    second = disclosure.register([("agent", _agent_id(2))], state_dir=directory)

    assert second.available is False


def test_transient_directory_fsync_failure_during_cold_start_still_returns_stable_aliases(
    tmp_path, monkeypatch
):
    """N3a fix round 2 (continued), new Finding 1 (BLOCKER): a transient
    directory-fsync failure (fails once, succeeds on the very next attempt)
    must still produce a fully available, stable-alias book -- proving the
    fix actually retries rather than giving up after a single attempt."""
    directory = tmp_path
    calls = {"n": 0}
    real_fsync_directory = state.fsync_directory

    def _fail_once_then_succeed(path, *, dir_fd=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated transient directory fsync failure")
        return real_fsync_directory(path, dir_fd=dir_fd)

    monkeypatch.setattr(state, "fsync_directory", _fail_once_then_succeed)

    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)

    assert book.available is True
    assert calls["n"] == 2
    assert book.alias("agent", _agent_id(1))


def test_transient_directory_fsync_failure_on_an_already_valid_pair_still_succeeds(
    tmp_path, monkeypatch
):
    """Same proof against the steady-state "normal" (valid secret, valid
    registry) branch, not just the cold-start creation path."""
    directory = tmp_path
    first = disclosure.register([("agent", _agent_id(1))], state_dir=directory)
    assert first.available is True

    calls = {"n": 0}
    real_fsync_directory = state.fsync_directory

    def _fail_once_then_succeed(path, *, dir_fd=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated transient directory fsync failure")
        return real_fsync_directory(path, dir_fd=dir_fd)

    monkeypatch.setattr(state, "fsync_directory", _fail_once_then_succeed)

    second = disclosure.register([("agent", _agent_id(2))], state_dir=directory)

    assert second.available is True
    assert calls["n"] == 2


def test_finalize_registration_transient_directory_fsync_failure_after_recovery_still_succeeds(
    tmp_path, monkeypatch
):
    """Registry-rename companion to the two tests above: a transient
    directory-fsync failure during _finalize_registration's post-commit
    recovery (content already correct on disk, PostCommitVerificationError
    raised) must still succeed once the retried fsync goes through. Only
    the fsync attempt(s) made *after* the simulated post-commit failure are
    made to fail once -- the earlier, unrelated directory-durability check
    _initialize_or_probe's own "normal" branch makes before this call is
    even reached must succeed normally, or this test would not isolate the
    behavior it claims to."""
    directory = tmp_path
    first = disclosure.register([("agent", _agent_id(1))], state_dir=directory)
    assert first.available is True

    state_flags = {"write_committed": False}

    def _commit_for_real_then_raise(path, data, *, dir_fd=None):
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        state_flags["write_committed"] = True
        raise state.PostCommitVerificationError(
            "simulated post-commit failure; content on disk is actually fine"
        )

    monkeypatch.setattr(state, "write_registry_bytes", _commit_for_real_then_raise)

    real_fsync_directory = state.fsync_directory
    calls_after_write = {"n": 0}

    def _fail_once_after_write_then_succeed(path, *, dir_fd=None):
        if not state_flags["write_committed"]:
            return real_fsync_directory(path, dir_fd=dir_fd)
        calls_after_write["n"] += 1
        if calls_after_write["n"] == 1:
            raise OSError("simulated transient directory fsync failure")
        return real_fsync_directory(path, dir_fd=dir_fd)

    monkeypatch.setattr(state, "fsync_directory", _fail_once_after_write_then_succeed)

    second = disclosure.register([("agent", _agent_id(2))], state_dir=directory)

    assert second.available is True
    assert calls_after_write["n"] == 2


# ============================================================================
# N3a fix round 2 review (fix2): Findings 1 and 2, mutation-coverage gaps
#
# Every existing fsync-failure test above either forces a different,
# unrelated fsync (a content-level write, which is pre-commit) or
# monkeypatches state.fsync_directory wholesale, which bypasses
# _fsync_dir_strict's own body entirely. None of them distinguishes a
# strict directory-fsync implementation from a best-effort one. The two
# tests below patch the real os.fsync / os.replace so every content-level
# operation still runs for real, and fail only the one call that matters,
# so a regression in _fsync_dir_strict or in write_registry_bytes's
# post-commit _verify_mode call is actually caught.
# ============================================================================


def test_write_registry_bytes_surfaces_a_real_directory_fsync_failure(tmp_path, monkeypatch):
    """N3a fix2 review, Finding 1: a real fsync(2) failure on the registry
    directory's own descriptor must surface as PostCommitVerificationError,
    not be silently swallowed inside _fsync_dir_strict. Unlike the tests
    above, os.fsync here is left wired to the real syscall for every
    regular-file descriptor (the temp file's own content fsync succeeds
    normally) and fails only when handed the directory's own descriptor,
    identified by tracking the exact os.open() call _fsync_dir_strict makes
    for the directory. A future regression that quietly re-adds
    `except OSError: pass` inside _fsync_dir_strict fails this test."""
    directory = tmp_path
    path = directory / "aliases.json"
    real_open = os.open
    dir_fd: dict[str, int | None] = {"fd": None}

    def _tracking_open(p, flags, mode=0o777, *args, **kwargs):
        fd = real_open(p, flags, mode, *args, **kwargs)
        if str(p) == str(directory) and flags == os.O_RDONLY:
            dir_fd["fd"] = fd
        return fd

    real_fsync = os.fsync

    def _failing_fsync(fd):
        if fd == dir_fd["fd"]:
            raise OSError("simulated real directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "open", _tracking_open)
    monkeypatch.setattr(os, "fsync", _failing_fsync)

    with pytest.raises(state.PostCommitVerificationError):
        state.write_registry_bytes(path, b'{"ok": true}\n')

    assert dir_fd["fd"] is not None  # the directory really was opened and tracked
    # the commit point (os.replace) already passed: the content is really on disk.
    assert path.read_bytes() == b'{"ok": true}\n'


def test_create_secret_directory_fsync_real_failure_is_not_swallowed(tmp_path, monkeypatch):
    """N3a fix2 review, Finding 1, secret side: state.fsync_directory (the
    public name disclosure.py calls right after create_secret() returns)
    must propagate a real fsync(2) failure on the directory's own
    descriptor, even though the secret's own content fsync (inside
    create_secret, a different, regular-file fd) succeeds normally. This
    exercises _fsync_dir_strict's real body directly, unlike the tests
    above that only ever mock state.fsync_directory as a whole."""
    directory = tmp_path
    secret_path = directory / "secret"
    state.create_secret(secret_path)  # succeeds for real; content already committed

    real_open = os.open
    dir_fd: dict[str, int | None] = {"fd": None}

    def _tracking_open(p, flags, mode=0o777, *args, **kwargs):
        fd = real_open(p, flags, mode, *args, **kwargs)
        if str(p) == str(directory) and flags == os.O_RDONLY:
            dir_fd["fd"] = fd
        return fd

    real_fsync = os.fsync

    def _failing_fsync(fd):
        if fd == dir_fd["fd"]:
            raise OSError("simulated real directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "open", _tracking_open)
    monkeypatch.setattr(os, "fsync", _failing_fsync)

    with pytest.raises(OSError):
        state.fsync_directory(directory)

    assert dir_fd["fd"] is not None


def test_write_registry_bytes_surfaces_a_real_post_commit_mode_mismatch(tmp_path, monkeypatch):
    """N3a fix2 review, Finding 2: the post-commit, by-path
    _verify_mode(path, 0o600, "alias registry") call (state.py, right after
    _fsync_dir_strict in write_registry_bytes) is the one check Finding 11's
    own tests do not cover -- those only pin the two pre-commit, by-
    descriptor mode checks. Here the pre-commit descriptor check passes
    normally (the temp file really is created 0600) and os.replace really
    commits the new content; only the mode of the file now sitting at the
    final path is wrong, as if some other process had chmod'd it in the
    narrow window between the replace and this verification. Removing the
    _verify_mode call at that call site makes this test fail."""
    directory = tmp_path
    path = directory / "aliases.json"
    real_replace = os.replace

    def _replace_then_loosen_mode(src, dst):
        real_replace(src, dst)
        os.chmod(dst, 0o644)  # simulate the mode changing right after commit

    monkeypatch.setattr(os, "replace", _replace_then_loosen_mode)

    with pytest.raises(state.PostCommitVerificationError):
        state.write_registry_bytes(path, b'{"ok": true}\n')

    # the commit point already passed: content is on disk despite the bad mode.
    assert path.read_bytes() == b'{"ok": true}\n'


def test_write_registry_bytes_post_commit_check_rejects_a_symlink_swapped_in_after_replace(
    tmp_path, monkeypatch
):
    """N3a fix round 2, new Finding 6 (MINOR): '_verify_mode uses
    Path.stat(). A same-principal race after os.replace can swap
    aliases.json for a symlink to a mode-0600 target, and this check
    follows it.' Build a target that would itself pass every mode/owner
    check (a real, valid, mode-0600 file, written by write_registry_bytes
    itself), then swap the just-replaced final path for a symlink to it
    immediately after os.replace -- the post-commit check must still
    reject this, by reopening the final path with O_NOFOLLOW rather than
    following the symlink via a separate stat() call.

    N3a fix round 3, new Finding 3 / mutation N20: the review found that
    the symlink target here (the old "elsewhere" content) differed from
    the content just committed, so the exact-content check alone already
    rejected it -- removing only O_NOFOLLOW still passed. The target is
    now byte-identical to what this call writes, mode 0600, this uid, so
    only O_NOFOLLOW can still produce PostCommitVerificationError here."""
    directory = tmp_path
    path = directory / "aliases.json"
    content = b'{"ok": true}\n'
    elsewhere = directory / "elsewhere.json"
    state.write_registry_bytes(elsewhere, content)  # real, valid, 0600, byte-identical
    real_replace = os.replace

    def _replace_then_symlink_swap(src, dst):
        real_replace(src, dst)
        os.unlink(str(dst))
        os.symlink(str(elsewhere), str(dst))

    monkeypatch.setattr(os, "replace", _replace_then_symlink_swap)

    with pytest.raises(state.PostCommitVerificationError):
        state.write_registry_bytes(path, content)


def test_post_commit_verifier_rejects_a_non_regular_descriptor_even_when_mode_and_content_match(
    tmp_path, monkeypatch
):
    """N3a fix round 3, new Finding 3 / mutation N21: isolate the
    regular-file check in _verify_registry_after_commit on its own, without
    depending on a real FIFO's blocking-open behavior (new Finding 2's fix
    for that is a separate item below). A real, regular, mode-0600 file
    whose content is exactly `expected` has its already-open descriptor's
    own fstat tampered to report a FIFO's type bits (mode bits unchanged);
    only the regular-file type check can still reject this -- a mutation
    that removes just that check (N21) would otherwise let it through."""
    path = tmp_path / "aliases.json"
    expected = disclosure._dumps(disclosure.empty_registry())
    _write_raw(path, expected)
    os.chmod(path, 0o600)
    _tamper_fstat_for_path(
        monkeypatch, path, lambda st: _stat_with(st, st_mode=stat.S_IFIFO | 0o600)
    )

    with pytest.raises(state.StateError):
        state._verify_registry_after_commit(path, expected)


def test_post_commit_verifier_rejects_a_wrong_owner_even_when_type_mode_and_content_match(
    tmp_path, monkeypatch
):
    """N3a fix round 3, new Finding 3 / mutation N22: isolate the owner
    check on its own. A regular, mode-0600 file whose content is exactly
    `expected` passes every other check; only the descriptor's tampered uid
    can still reject it."""
    path = tmp_path / "aliases.json"
    expected = disclosure._dumps(disclosure.empty_registry())
    _write_raw(path, expected)
    os.chmod(path, 0o600)
    _tamper_fstat_for_path(monkeypatch, path, lambda st: _stat_with(st, st_uid=st.st_uid + 1))

    with pytest.raises(state.StateError):
        state._verify_registry_after_commit(path, expected)


def test_post_commit_verifier_rejects_a_content_mismatch_even_when_type_mode_and_owner_match(
    tmp_path,
):
    """N3a fix round 3, new Finding 3 / mutation N23: isolate the
    exact-content check on its own. A regular, mode-0600, this-uid file
    whose bytes simply differ from `expected` passes every other check;
    only the content comparison can still reject it."""
    path = tmp_path / "aliases.json"
    _write_raw(path, b'{"different": true}\n')
    os.chmod(path, 0o600)

    with pytest.raises(state.StateError):
        state._verify_registry_after_commit(path, b'{"ok": true}\n')


def test_post_commit_reopen_does_not_block_on_a_fifo_swapped_in_after_the_rename(tmp_path):
    """N3a fix round 3, new Finding 2 (MAJOR): _verify_registry_after_commit
    reopens the final path with O_RDONLY|O_NOFOLLOW but, before this fix,
    without O_NONBLOCK. If the final registry is replaced by a FIFO right
    after os.replace(), opening it for read blocks indefinitely waiting for
    a writer -- while state.lock is held, so the command and every
    competing state operation can stall rather than reaching the positional
    fallback. Runs the write in a bounded subprocess (a hard wall-clock
    timeout well under pytest's own default) so a regression that drops
    O_NONBLOCK makes this test fail by timing out rather than hanging the
    whole suite. The subprocess swaps a FIFO in immediately after
    os.replace() and expects PostCommitVerificationError without ever
    writing to the FIFO."""
    path = tmp_path / "aliases.json"
    script = textwrap.dedent(
        f"""
        import os, sys
        from pathlib import Path
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
        from agent_postmortem import state

        path = Path({str(path)!r})
        real_replace = os.replace

        def _replace_then_fifo(src, dst):
            real_replace(src, dst)
            os.unlink(dst)
            os.mkfifo(dst)
            os.chmod(dst, 0o600)

        os.replace = _replace_then_fifo
        try:
            state.write_registry_bytes(path, b'{{"ok": true}}\\n')
        except state.PostCommitVerificationError:
            sys.exit(0)
        except BaseException as exc:
            sys.stderr.write(f"wrong exception: {{exc!r}}\\n")
            sys.exit(4)
        sys.stderr.write("write_registry_bytes unexpectedly did not raise\\n")
        sys.exit(3)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)


# ============================================================================
# P6 initialization state machine
# ============================================================================


def test_p6_absent_absent_creates_both(tmp_path):
    directory = tmp_path / "agent-postmortem"
    directory.mkdir()
    secret, registry = disclosure._initialize_or_probe(directory)
    assert secret is not None and len(secret) == 32
    assert registry == disclosure.empty_registry()
    assert stat.S_IMODE(state.secret_path(directory).stat().st_mode) == 0o600
    assert stat.S_IMODE(state.registry_path(directory).stat().st_mode) == 0o600


def test_p6_valid_empty_registry_absent_secret_creates_secret_only(tmp_path):
    directory = tmp_path / "agent-postmortem"
    directory.mkdir()
    state.write_registry_bytes(
        state.registry_path(directory), disclosure._dumps(disclosure.empty_registry())
    )
    secret, registry = disclosure._initialize_or_probe(directory)
    assert secret is not None and len(secret) == 32
    assert registry == disclosure.empty_registry()


def test_p6_valid_nonempty_registry_absent_secret_is_unavailable_and_untouched(tmp_path):
    directory = tmp_path / "agent-postmortem"
    directory.mkdir()
    nonempty = _hand_registry(agent={HEX64_A: 12})
    state.write_registry_bytes(state.registry_path(directory), disclosure._dumps(nonempty))
    before = state.registry_path(directory).read_bytes()

    secret, registry = disclosure._initialize_or_probe(directory)

    assert (secret, registry) == (None, None)
    assert not state.secret_path(directory).exists()
    assert state.registry_path(directory).read_bytes() == before


def test_p6_absent_registry_valid_secret_is_unavailable_and_untouched(tmp_path):
    directory = tmp_path / "agent-postmortem"
    directory.mkdir()
    real_secret = state.create_secret(state.secret_path(directory))

    secret, registry = disclosure._initialize_or_probe(directory)

    assert (secret, registry) == (None, None)
    assert not state.registry_path(directory).exists()
    assert state.secret_path(directory).read_bytes() == real_secret


def test_p6_valid_valid_is_normal(tmp_path):
    directory = tmp_path / "agent-postmortem"
    directory.mkdir()
    real_secret = state.create_secret(state.secret_path(directory))
    nonempty = _hand_registry(agent={HEX64_A: 12})
    state.write_registry_bytes(state.registry_path(directory), disclosure._dumps(nonempty))

    secret, registry = disclosure._initialize_or_probe(directory)

    assert secret == real_secret
    assert registry == nonempty


@pytest.mark.parametrize("break_secret,break_registry", [(True, True), (True, False), (False, True)])
def test_p6_any_invalid_is_unavailable(tmp_path, break_secret, break_registry):
    directory = tmp_path / "agent-postmortem"
    directory.mkdir()
    if break_secret:
        _write_raw(state.secret_path(directory), b"x" * 10)  # wrong size
    if break_registry:
        state.write_registry_bytes(state.registry_path(directory), b"{not json")

    secret, registry = disclosure._initialize_or_probe(directory)
    assert (secret, registry) == (None, None)


def test_p6_crash_recovery_row_is_completed_by_the_next_run(tmp_path):
    """A valid, empty registry with an absent secret (P6's own crash row)
    is exactly the "valid-empty, absent" row: the next call completes it by
    creating the secret, with no special-cased recovery path needed."""
    directory = tmp_path / "agent-postmortem"
    directory.mkdir()
    state.write_registry_bytes(
        state.registry_path(directory), disclosure._dumps(disclosure.empty_registry())
    )

    secret, registry = disclosure._initialize_or_probe(directory)

    assert secret is not None
    assert registry == disclosure.empty_registry()


def test_fsync_failure_during_initial_registry_write_is_recovered_by_next_run(tmp_path, monkeypatch):
    directory = tmp_path / "agent-postmortem"
    directory.mkdir()

    def _boom(*args, **kwargs):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", _boom)
    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)
    assert book.available is False
    assert not state.registry_path(directory).exists()
    assert not state.secret_path(directory).exists()
    assert list(directory.glob("*.tmp*")) == []

    monkeypatch.undo()
    book2 = disclosure.register([("agent", _agent_id(1))], state_dir=directory)
    assert book2.available is True


def test_replace_failure_during_initial_registry_write_is_recovered_by_next_run(tmp_path, monkeypatch):
    """N3a fix round 3, new Finding 1: register() always holds a verified
    directory descriptor now, so the registry's own atomic commit goes
    through os.rename(..., src_dir_fd=..., dst_dir_fd=...), not os.replace
    (see write_registry_bytes's own docstring for why) -- both are
    monkeypatched here so this test keeps covering the commit-point failure
    regardless of which one is the operative call."""
    directory = tmp_path / "agent-postmortem"
    directory.mkdir()

    def _boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    monkeypatch.setattr(os, "rename", _boom)
    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)
    assert book.available is False
    assert not state.registry_path(directory).exists()
    assert list(directory.glob("*.tmp*")) == []

    monkeypatch.undo()
    book2 = disclosure.register([("agent", _agent_id(1))], state_dir=directory)
    assert book2.available is True


def test_fsync_failure_during_secret_creation_is_recovered_by_next_run(tmp_path, monkeypatch):
    directory = tmp_path / "agent-postmortem"
    directory.mkdir()
    calls = {"n": 0}
    real_fsync = os.fsync

    def _counting_fsync(fd):
        calls["n"] += 1
        if calls["n"] == 3:  # the registry's own two fsyncs pass; the secret's fails
            raise OSError("simulated fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _counting_fsync)
    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)
    assert book.available is False
    assert not state.secret_path(directory).exists()  # cleaned up, never left dangling

    monkeypatch.undo()
    book2 = disclosure.register([("agent", _agent_id(1))], state_dir=directory)
    assert book2.available is True
    assert book2.alias("agent", _agent_id(1))


# ============================================================================
# assignment (P3)
# ============================================================================


def test_assign_alias_gives_the_shortest_non_colliding_length():
    table: dict[str, int] = {}
    n = disclosure.assign_alias(table, "a" * 64)
    assert n == 12


def test_forced_12_char_collision_gives_13(tmp_path, monkeypatch):
    first = "a" * 64
    second = "a" * 12 + "f" * 52  # shares the first 12 hex chars with `first`

    def _fake_hmac(secret, real_id):
        return {_agent_id(1): first, _agent_id(2): second}[real_id]

    monkeypatch.setattr(disclosure, "hmac_hex", _fake_hmac)
    book = disclosure.register(
        [("agent", _agent_id(1)), ("agent", _agent_id(2))], state_dir=tmp_path
    )
    assert book.alias("agent", _agent_id(1)) == first[:12]
    assert book.alias("agent", _agent_id(2)) == second[:13]


def test_three_way_collision_gives_12_13_14_in_registration_order(tmp_path, monkeypatch):
    hashed = {
        _agent_id(1): "a" * 64,
        _agent_id(2): "a" * 13 + "f" * 51,
        _agent_id(3): "a" * 14 + "e" * 50,
    }

    def _fake_hmac(secret, real_id):
        return hashed[real_id]

    monkeypatch.setattr(disclosure, "hmac_hex", _fake_hmac)
    book = disclosure.register(
        [("agent", _agent_id(1)), ("agent", _agent_id(2)), ("agent", _agent_id(3))],
        state_dir=tmp_path,
    )
    assert book.alias("agent", _agent_id(1)) == hashed[_agent_id(1)][:12]
    assert book.alias("agent", _agent_id(2)) == hashed[_agent_id(2)][:13]
    assert book.alias("agent", _agent_id(3)) == hashed[_agent_id(3)][:14]


def test_equal_aliases_across_kinds_are_allowed(tmp_path, monkeypatch):
    shared_hash = "c" * 64

    def _fake_hmac(secret, real_id):
        return shared_hash

    monkeypatch.setattr(disclosure, "hmac_hex", _fake_hmac)
    book = disclosure.register(
        [("agent", _agent_id(1)), ("session", _session_id(1))], state_dir=tmp_path
    )
    assert book.alias("agent", _agent_id(1)) == shared_hash[:12]
    assert book.alias("session", _session_id(1)) == shared_hash[:12]


def test_alias_is_stable_across_two_separate_registration_calls(tmp_path):
    agent = _agent_id(1)
    first = disclosure.register([("agent", agent)], state_dir=tmp_path)
    alias1 = first.alias("agent", agent)

    second = disclosure.register([("agent", agent), ("agent", _agent_id(2))], state_dir=tmp_path)
    assert second.alias("agent", agent) == alias1


def test_register_called_twice_with_identical_identities_writes_nothing_the_second_time(
    tmp_path, monkeypatch
):
    """N3a review Finding 5: when every identity in the batch was already
    registered, `changed` is False and _write_if_within_bounds must never
    even be invoked -- not just "the file ends up unchanged". Fail loudly
    if a future refactor drops the `changed` gate."""
    agent = _agent_id(1)
    first = disclosure.register([("agent", agent)], state_dir=tmp_path)
    assert first.available is True
    alias1 = first.alias("agent", agent)

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("write_registry_bytes must not run when nothing changed")

    monkeypatch.setattr(state, "write_registry_bytes", _must_not_be_called)

    second = disclosure.register([("agent", agent)], state_dir=tmp_path)
    assert second.available is True
    assert second.alias("agent", agent) == alias1


def test_a_second_process_sees_the_same_alias(tmp_path):
    state_home = tmp_path / "xdg-state"
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    agent = _agent_id(1)

    first = disclosure.register([("agent", agent)], state_dir=state_home / "agent-postmortem")
    alias1 = first.alias("agent", agent)

    script = textwrap.dedent(
        f"""
        import os, sys
        os.environ["XDG_STATE_HOME"] = {str(state_home)!r}
        sys.path.insert(0, {src_dir!r})
        from agent_postmortem import disclosure
        book = disclosure.register([("agent", {agent!r})])
        sys.stdout.write(book.alias("agent", {agent!r}))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout == alias1


def test_two_concurrent_subprocesses_registering_overlapping_sets_agree_on_the_shared_id(tmp_path):
    """Two subprocesses each register one shared agent id plus one id of
    their own, launched against the same temporary XDG_STATE_HOME with a
    start barrier so they actually contend for the lock. Both must print
    the same alias for the shared id, and the registry file left behind
    must still be exactly one, valid file."""
    state_home = tmp_path / "xdg-state"
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    shared = _agent_id(1)
    only_a = _agent_id(2)
    only_b = _agent_id(3)
    go_file = tmp_path / "go"
    ready_a = tmp_path / "ready-a"
    ready_b = tmp_path / "ready-b"
    out_a = tmp_path / "out-a.txt"
    out_b = tmp_path / "out-b.txt"

    def _worker(idx: str, ready_path: Path, own_id: str, out_path: Path) -> str:
        return textwrap.dedent(
            f"""
            import os, sys, time
            os.environ["XDG_STATE_HOME"] = {str(state_home)!r}
            sys.path.insert(0, {src_dir!r})
            from agent_postmortem import disclosure
            with open({str(ready_path)!r}, "w") as fh:
                fh.write("ready")
            while not os.path.exists({str(go_file)!r}):
                time.sleep(0.005)
            book = disclosure.register([("agent", {shared!r}), ("agent", {own_id!r})])
            with open({str(out_path)!r}, "w") as fh:
                fh.write(book.alias("agent", {shared!r}) + "\\n")
                fh.write(str(book.available))
            """
        )

    procs = [
        subprocess.Popen([sys.executable, "-c", _worker("a", ready_a, only_a, out_a)]),
        subprocess.Popen([sys.executable, "-c", _worker("b", ready_b, only_b, out_b)]),
    ]
    deadline = time.monotonic() + 10.0
    while not (ready_a.exists() and ready_b.exists()):
        if time.monotonic() >= deadline:
            for p in procs:
                p.kill()
            raise AssertionError("workers did not signal ready in time")
        time.sleep(0.01)
    go_file.write_text("go", encoding="utf-8")

    for p in procs:
        assert p.wait(timeout=30) == 0

    text_a = out_a.read_text(encoding="utf-8").splitlines()
    text_b = out_b.read_text(encoding="utf-8").splitlines()
    assert text_a[0] == text_b[0]  # same alias for the shared id
    assert text_a[1] == "True" and text_b[1] == "True"

    registry_path = state_home / "agent-postmortem" / "aliases.json"
    parsed = json.loads(registry_path.read_text(encoding="utf-8"))
    validated = disclosure.validate_registry(parsed)
    assert validated is not None
    assert disclosure.count_entries(validated) == 3  # shared, only_a, only_b


def _enoent_lock_open(monkeypatch, failures: int | None) -> dict:
    """Make the descriptor-relative open of state.lock raise ENOENT, the way
    macOS APFS answers the loser of two processes creating the same new
    name relative to a directory descriptor at the same moment. failures is
    how many opens fail before one succeeds; None means every one fails."""
    import errno

    real_open = os.open
    calls = {"failed": 0}

    def _open(path, flags, mode=0o777, *, dir_fd=None):
        if path == "state.lock" and dir_fd is not None and (
            failures is None or calls["failed"] < failures
        ):
            calls["failed"] += 1
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(state.os, "open", _open)
    return calls


def test_transient_enoent_on_lock_create_is_retried(tmp_path, monkeypatch):
    calls = _enoent_lock_open(monkeypatch, failures=2)
    book = disclosure.register([("agent", _agent_id(1))], state_dir=tmp_path)
    assert calls["failed"] == 2
    assert book.available is True
    assert not book.alias("agent", _agent_id(1)).startswith("agent-")


def test_persistent_enoent_on_lock_create_is_bounded_and_falls_back(tmp_path, monkeypatch):
    """ENOENT on every attempt (the directory itself is gone) must stop after
    a bounded number of attempts and fall back, fail closed, writing
    nothing."""
    calls = _enoent_lock_open(monkeypatch, failures=None)
    book = disclosure.register([("agent", _agent_id(1))], state_dir=tmp_path)
    assert book.available is False
    assert 1 <= calls["failed"] <= 10
    assert not state.secret_path(tmp_path).exists()
    assert not state.registry_path(tmp_path).exists()


def test_registry_at_entry_limit_falls_back_and_leaves_file_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(disclosure, "MAX_ENTRIES", 1)
    first = disclosure.register([("agent", _agent_id(1))], state_dir=tmp_path)
    assert first.available is True
    before = state.registry_path(tmp_path).read_bytes()
    before_mtime = _mtime_ns(state.registry_path(tmp_path))

    second = disclosure.register([("agent", _agent_id(2))], state_dir=tmp_path)

    assert second.available is False
    assert state.registry_path(tmp_path).read_bytes() == before
    assert _mtime_ns(state.registry_path(tmp_path)) == before_mtime


def test_registry_at_byte_limit_falls_back_and_leaves_file_untouched(tmp_path):
    first = disclosure.register([("agent", _agent_id(1))], state_dir=tmp_path)
    assert first.available is True
    current_size = len(state.registry_path(tmp_path).read_bytes())
    before = state.registry_path(tmp_path).read_bytes()
    before_mtime = _mtime_ns(state.registry_path(tmp_path))

    import agent_postmortem.disclosure as disclosure_module

    old_max = disclosure_module.MAX_BYTES
    disclosure_module.MAX_BYTES = current_size
    try:
        second = disclosure.register([("agent", _agent_id(2))], state_dir=tmp_path)
    finally:
        disclosure_module.MAX_BYTES = old_max

    assert second.available is False
    assert state.registry_path(tmp_path).read_bytes() == before
    assert _mtime_ns(state.registry_path(tmp_path)) == before_mtime


def test_registry_over_entry_bound_is_invalid_on_read_even_under_byte_bound(tmp_path, monkeypatch):
    """N3a fix round 2, Finding 5: `_write_if_within_bounds` only checks
    MAX_ENTRIES when a *new* identity would change the registry; a registry
    that is already over the bound on disk -- well under MAX_BYTES -- must
    also be unavailable the moment it is read, before any write is even
    attempted."""
    directory = tmp_path
    reg = _hand_registry(agent={HEX64_A: 12, HEX64_B: 13})
    state.write_registry_bytes(state.registry_path(directory), disclosure._dumps(reg))
    before = state.registry_path(directory).read_bytes()
    before_mtime = _mtime_ns(state.registry_path(directory))

    monkeypatch.setattr(disclosure, "MAX_ENTRIES", 1)

    probe, validated = disclosure._probe_registry(directory)
    assert probe is state.Probe.INVALID
    assert validated is None

    book = disclosure.register([("agent", _agent_id(5))], state_dir=directory)
    assert book.available is False
    assert state.registry_path(directory).read_bytes() == before
    assert _mtime_ns(state.registry_path(directory)) == before_mtime


# ============================================================================
# fallback books
# ============================================================================


def test_fallback_book_assigns_positional_names_in_first_use_order(tmp_path, monkeypatch):
    def _always_locked(*args, **kwargs):
        raise state.StateLocked("simulated contention")

    monkeypatch.setattr(state, "lock_state_dir", _always_locked)
    book = disclosure.register(
        [("agent", _agent_id(1)), ("agent", _agent_id(2)), ("session", _session_id(1))],
        state_dir=tmp_path,
    )
    assert book.available is False
    assert book.alias("agent", _agent_id(1)) == "agent-1"
    assert book.alias("agent", _agent_id(2)) == "agent-2"
    assert book.alias("session", _session_id(1)) == "session-1"


def test_fallback_book_never_contains_a_real_id(tmp_path, monkeypatch):
    def _always_locked(*args, **kwargs):
        raise state.StateLocked("simulated contention")

    monkeypatch.setattr(state, "lock_state_dir", _always_locked)
    agent = _agent_id(1)
    book = disclosure.register([("agent", agent)], state_dir=tmp_path)
    alias = book.alias("agent", agent)
    assert agent not in alias
    assert alias == "agent-1"


def test_fallback_leaves_secret_and_registry_untouched_when_both_already_exist(tmp_path, monkeypatch):
    directory = tmp_path
    real_secret = state.create_secret(state.secret_path(directory))
    reg = disclosure.empty_registry()
    state.write_registry_bytes(state.registry_path(directory), disclosure._dumps(reg))
    before_secret = state.secret_path(directory).read_bytes()
    before_secret_mtime = _mtime_ns(state.secret_path(directory))
    before_registry = state.registry_path(directory).read_bytes()
    before_registry_mtime = _mtime_ns(state.registry_path(directory))

    def _always_locked(*args, **kwargs):
        raise state.StateLocked("simulated contention")

    monkeypatch.setattr(state, "lock_state_dir", _always_locked)
    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)

    assert book.available is False
    assert state.secret_path(directory).read_bytes() == before_secret
    assert _mtime_ns(state.secret_path(directory)) == before_secret_mtime
    assert state.registry_path(directory).read_bytes() == before_registry
    assert _mtime_ns(state.registry_path(directory)) == before_registry_mtime
    assert real_secret == before_secret


def test_fallback_on_non_canonical_id_raises_before_touching_disk(tmp_path):
    with pytest.raises(ValueError):
        disclosure.register([("agent", "not-canonical")], state_dir=tmp_path)
    assert not state.secret_path(tmp_path).exists()
    assert not state.registry_path(tmp_path).exists()


# ============================================================================
# slow path (P9)
# ============================================================================


def test_slow_path_registers_an_unseen_identity_and_increments_the_counter(tmp_path):
    book = disclosure.register([("agent", _agent_id(1))], state_dir=tmp_path)
    assert book.slow_path_count == 0

    alias2 = book.alias("agent", _agent_id(2))  # never passed to register()
    assert book.slow_path_count == 1
    assert alias2

    # Stability: a fresh registration of both ids agrees with the slow path.
    fresh = disclosure.register(
        [("agent", _agent_id(1)), ("agent", _agent_id(2))], state_dir=tmp_path
    )
    assert fresh.alias("agent", _agent_id(2)) == alias2


def test_slow_path_counter_is_zero_when_every_identity_was_pre_registered(tmp_path):
    book = disclosure.register(
        [("agent", _agent_id(1)), ("agent", _agent_id(2))], state_dir=tmp_path
    )
    book.alias("agent", _agent_id(1))
    book.alias("agent", _agent_id(2))
    assert book.slow_path_count == 0


def test_slow_path_on_a_fallback_book_does_not_increment_the_counter(tmp_path, monkeypatch):
    def _always_locked(*args, **kwargs):
        raise state.StateLocked("simulated contention")

    monkeypatch.setattr(state, "lock_state_dir", _always_locked)
    book = disclosure.register([("agent", _agent_id(1))], state_dir=tmp_path)
    assert book.available is False

    alias2 = book.alias("agent", _agent_id(2))
    assert book.slow_path_count == 0
    assert alias2 == "agent-2"


def test_slow_path_with_a_non_canonical_id_is_positional_and_does_not_touch_the_registry(tmp_path):
    """N3a fix round 2, Finding 7: AliasBook.alias() must apply register()'s
    own P2 canonical-id check before doing anything else on the slow path.
    An id that is not canonical for its kind is never eligible for a stable
    alias -- it must not be HMACed, must not be written, and must not even
    attempt the lock (so it does not count against slow_path_count)."""
    book = disclosure.register([], state_dir=tmp_path)
    before = state.registry_path(tmp_path).read_bytes()
    before_mtime = _mtime_ns(state.registry_path(tmp_path))

    alias = book.alias("agent", "not-canonical")

    assert alias == "agent-1"
    assert book.available is True
    assert book.slow_path_count == 0
    assert state.registry_path(tmp_path).read_bytes() == before
    assert _mtime_ns(state.registry_path(tmp_path)) == before_mtime


def test_slow_path_with_an_unknown_kind_is_positional_not_a_keyerror(tmp_path):
    """N3a fix round 2, Finding 7: a kind alias() does not recognize (not
    one of agent/session/project) must get a positional name, never a raw
    KeyError from indexing the registry's per-kind tables with a key that
    was never one of them."""
    book = disclosure.register([], state_dir=tmp_path)

    alias = book.alias("phase", "anything")

    assert alias == "phase-1"
    assert book.available is True
    assert book.slow_path_count == 0


def test_slow_path_failure_degrades_the_whole_book_atomically(tmp_path, monkeypatch):
    """N3a fix round 2, Finding 8: a slow-path failure must not leave a mix
    of stable and positional aliases in one book (docs/DESIGN.md 7.5a/P8).
    The agent already registered with a stable alias must lose it the
    moment a later identity's slow-path registration fails: the whole book
    degrades atomically (available=False, every already-known identity
    renamed positionally in registration order), not just the new one.
    Replaces the old test that accepted the mixed stable/positional state
    (N3a fix round 2, per the approved plan)."""
    book = disclosure.register([("agent", _agent_id(1))], state_dir=tmp_path)
    assert book.available is True
    stable_alias_1 = book.alias("agent", _agent_id(1))

    def _always_locked(*args, **kwargs):
        raise state.StateLocked("simulated contention")

    monkeypatch.setattr(state, "lock_state_dir", _always_locked)
    alias_2 = book.alias("agent", _agent_id(2))

    assert book.available is False
    assert book.slow_path_count == 1
    assert alias_2 == "agent-2"
    # The previously stable alias is gone: re-asking for the same identity
    # now returns a positional name instead, assigned first (registration
    # order), and consistently on repeated calls.
    assert book.alias("agent", _agent_id(1)) == "agent-1"
    assert book.alias("agent", _agent_id(1)) != stable_alias_1


# --- P6: the secret's directory entry is made durable (main-session review) ---


@pytest.mark.parametrize("registry_present", [False, True])
def test_creating_the_secret_fsyncs_the_state_directory_afterwards(
    tmp_path, monkeypatch, registry_present
):
    if registry_present:
        state.write_registry_bytes(
            state.registry_path(tmp_path), json.dumps(disclosure.empty_registry()).encode()
        )
    events: list[str] = []
    real_create = state.create_secret
    real_fsync_dir_strict = state._fsync_dir_strict

    def spy_create(path, *, dir_fd=None):
        data = real_create(path, dir_fd=dir_fd)
        events.append("create_secret")
        return data

    def spy_fsync_dir(path, *, dir_fd=None):
        real_fsync_dir_strict(path, dir_fd=dir_fd)
        if Path(path) == tmp_path:
            events.append("fsync_dir")

    monkeypatch.setattr(state, "create_secret", spy_create)
    # N3a fix round 2, Finding 1: the directory fsync after create_secret()
    # is now the strict, disclosure-specific sibling of the best-effort
    # _fsync_dir state.json's own writer keeps.
    monkeypatch.setattr(state, "_fsync_dir_strict", spy_fsync_dir)

    book = disclosure.register([], state_dir=tmp_path)

    assert book.available is True
    assert "create_secret" in events
    assert "fsync_dir" in events[events.index("create_secret") + 1:]


# ============================================================================
# N3a fix round 2 review (new Findings 2, 3, 4): mutation-coverage gaps with
# no code defect behind them. Each check these tests exercise already
# exists in the shipped implementation (added to close the original
# review's findings 1, 3, and 4); the gap was that no test exercised it
# against the exact mutation the reviewer tried, so every one of these
# passes unmodified against the current source. .reviews/v0.3/
# N3a-fix3-report.md records, for N03, N04, and N08 through N15, which test
# below fails when that specific check is removed from a throwaway copy.
# ============================================================================


# --- new Finding 2: the registry post-commit re-probe and equality check ---


def test_finalize_registration_requires_fallback_when_post_commit_file_is_invalid(
    tmp_path, monkeypatch
):
    """N3a fix round 2, new Finding 2 / mutation N03: after
    PostCommitVerificationError, _finalize_registration must re-probe the
    real file on disk and reject it if invalid -- never trust the aliases
    it already computed in memory. Make the write actually commit invalid
    content (as if corrupted in the narrow post-replace window new Finding
    6 separately closes) and then raise the post-commit error; a mutation
    that skips the re-probe entirely (N03) wrongly accepts it."""
    directory = tmp_path
    first = disclosure.register([("agent", _agent_id(1))], state_dir=directory)
    assert first.available is True

    def _commit_invalid_then_raise(path, data, *, dir_fd=None):
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, b"{not valid json")
        finally:
            os.close(fd)
        raise state.PostCommitVerificationError("simulated post-commit failure")

    monkeypatch.setattr(state, "write_registry_bytes", _commit_invalid_then_raise)

    second = disclosure.register([("agent", _agent_id(2))], state_dir=directory)

    assert second.available is False


def test_finalize_registration_requires_fallback_when_post_commit_file_is_a_different_valid_registry(
    tmp_path, monkeypatch
):
    """N3a fix round 2, new Finding 2 / mutation N04: the re-probed registry
    must be byte-for-byte (as parsed dicts) equal to what this call just
    computed, not merely "some other valid registry". A mutation that
    re-probes but drops the equality comparison (N04) wrongly accepts a
    different, but perfectly valid, registry."""
    directory = tmp_path
    first = disclosure.register([("agent", _agent_id(1))], state_dir=directory)
    assert first.available is True

    different_valid = disclosure._dumps(_hand_registry(agent={HEX64_A: 12}))

    def _commit_different_valid_then_raise(path, data, *, dir_fd=None):
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, different_valid)
        finally:
            os.close(fd)
        raise state.PostCommitVerificationError("simulated post-commit failure")

    monkeypatch.setattr(state, "write_registry_bytes", _commit_different_valid_then_raise)

    second = disclosure.register([("agent", _agent_id(2))], state_dir=directory)

    assert second.available is False


# --- new Finding 3: the disclosure prerequisite checks (state dir, lock) ---


def _stat_with(st: os.stat_result, **overrides) -> os.stat_result:
    """A copy of st with the named fields replaced, the rest carried over
    unchanged. Generalizes the single-field tampering already used above
    (test_create_secret_fails_before_commit_when_the_open_descriptor_
    reports_the_wrong_mode) to any field, so the prerequisite tests below
    can tamper mode, uid, or type independently."""
    fields = (
        "st_mode",
        "st_ino",
        "st_dev",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_atime",
        "st_mtime",
        "st_ctime",
    )
    values = [overrides.get(name, getattr(st, name)) for name in fields]
    return os.stat_result(tuple(values))


def _tamper_fstat_for_path(monkeypatch, target_path, tamper):
    """Monkeypatch os.open/os.fstat so that, for exactly the fd opened for
    target_path, fstat reports tamper(real_stat) instead of the real
    result, while every other descriptor's fstat is untouched. target_path
    is matched by exact string equality against the path os.open() was
    called with; each prerequisite check below opens its own file once, at
    one fixed path, so this is unambiguous."""
    real_open = os.open
    real_fstat = os.fstat
    target: dict[str, int | None] = {"fd": None}

    def _tracking_open(p, flags, mode=0o777, *args, **kwargs):
        fd = real_open(p, flags, mode, *args, **kwargs)
        relative = kwargs.get("dir_fd") is not None and str(p) == target_path.name
        if str(p) == str(target_path) or relative:
            target["fd"] = fd
        return fd

    def _tampering_fstat(fd):
        st = real_fstat(fd)
        if fd == target["fd"]:
            return tamper(st)
        return st

    monkeypatch.setattr(os, "open", _tracking_open)
    monkeypatch.setattr(os, "fstat", _tampering_fstat)


def _track_disclosure_probing(monkeypatch) -> dict:
    """Record whether register() ever reaches _initialize_or_probe at all,
    without changing its behavior (the spy calls through to the real
    function) -- used by the prerequisite tests below to prove the state-
    directory/lock checks reject before either disclosure file is ever
    probed. Stubbing this to raise instead of calling through would be
    swallowed by register()'s own broad except-Exception-and-fall-back,
    making "the book is a fallback" true either way and the assertion
    vacuous; recording the call directly is not."""
    probed = {"called": False}
    real = disclosure._initialize_or_probe

    def _spy(*args, **kwargs):
        probed["called"] = True
        return real(*args, **kwargs)

    monkeypatch.setattr(disclosure, "_initialize_or_probe", _spy)
    return probed


def test_state_directory_wrong_mode_is_rejected_before_either_disclosure_file_is_probed(
    tmp_path, monkeypatch
):
    """N3a fix round 2, new Finding 3 / mutation N08: force the best-effort
    repair chmod to fail (as it would against attacker-controlled metadata
    a chmod cannot fix), then tamper the freshly opened descriptor's own
    fstat to report a mode other than 0700. register() must fall back
    before ever reaching _initialize_or_probe."""
    directory = tmp_path / "agent-postmortem"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(
        os, "chmod", lambda *a, **k: (_ for _ in ()).throw(OSError("simulated chmod repair failure"))
    )
    _tamper_fstat_for_path(
        monkeypatch, directory, lambda st: _stat_with(st, st_mode=stat.S_IFDIR | 0o755)
    )
    probed = _track_disclosure_probing(monkeypatch)

    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)

    assert book.available is False
    assert probed["called"] is False


def test_state_directory_wrong_owner_is_rejected_before_either_disclosure_file_is_probed(
    tmp_path, monkeypatch
):
    """N3a fix round 2, new Finding 3 / mutation N09."""
    directory = tmp_path / "agent-postmortem"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(
        os, "chmod", lambda *a, **k: (_ for _ in ()).throw(OSError("simulated chmod repair failure"))
    )
    _tamper_fstat_for_path(monkeypatch, directory, lambda st: _stat_with(st, st_uid=st.st_uid + 1))
    probed = _track_disclosure_probing(monkeypatch)

    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)

    assert book.available is False
    assert probed["called"] is False


def test_lock_file_wrong_mode_is_rejected_before_either_disclosure_file_is_probed(
    tmp_path, monkeypatch
):
    """N3a fix round 2, new Finding 3 / mutation N10."""
    directory = tmp_path / "agent-postmortem"
    directory.mkdir(mode=0o700)
    lock_path = directory / "state.lock"
    monkeypatch.setattr(
        os, "chmod", lambda *a, **k: (_ for _ in ()).throw(OSError("simulated chmod repair failure"))
    )
    _tamper_fstat_for_path(
        monkeypatch, lock_path, lambda st: _stat_with(st, st_mode=stat.S_IFREG | 0o644)
    )
    probed = _track_disclosure_probing(monkeypatch)

    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)

    assert book.available is False
    assert probed["called"] is False


def test_lock_file_wrong_owner_is_rejected_before_either_disclosure_file_is_probed(
    tmp_path, monkeypatch
):
    """N3a fix round 2, new Finding 3 / mutation N11."""
    directory = tmp_path / "agent-postmortem"
    directory.mkdir(mode=0o700)
    lock_path = directory / "state.lock"
    monkeypatch.setattr(
        os, "chmod", lambda *a, **k: (_ for _ in ()).throw(OSError("simulated chmod repair failure"))
    )
    _tamper_fstat_for_path(monkeypatch, lock_path, lambda st: _stat_with(st, st_uid=st.st_uid + 1))
    probed = _track_disclosure_probing(monkeypatch)

    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)

    assert book.available is False
    assert probed["called"] is False


def test_lock_file_not_a_regular_file_is_rejected_before_either_disclosure_file_is_probed(
    tmp_path, monkeypatch
):
    """N3a fix round 2, new Finding 3 / mutation N12."""
    directory = tmp_path / "agent-postmortem"
    directory.mkdir(mode=0o700)
    lock_path = directory / "state.lock"
    monkeypatch.setattr(
        os, "chmod", lambda *a, **k: (_ for _ in ()).throw(OSError("simulated chmod repair failure"))
    )
    _tamper_fstat_for_path(
        monkeypatch, lock_path, lambda st: _stat_with(st, st_mode=stat.S_IFDIR | 0o600)
    )
    probed = _track_disclosure_probing(monkeypatch)

    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)

    assert book.available is False
    assert probed["called"] is False


def test_state_directory_symlink_swapped_in_after_the_lock_is_acquired_is_rejected(
    tmp_path, monkeypatch
):
    """N3a fix round 2, new Finding 3 / mutation N13: _verify_disclosure_
    prerequisites must open the state directory itself with O_NOFOLLOW|
    O_DIRECTORY, never trust _ensure_state_dir's earlier, separate lstat
    check -- a same-principal race can swap the directory for a symlink in
    between. Swap the real directory for a symlink to a different,
    otherwise-valid directory right after the lock is acquired (the point
    in lock_state_dir between _ensure_state_dir's check and
    _open_verified_state_dir's own open), and require a fallback
    rather than silently using whatever the symlink points to."""
    directory = tmp_path / "agent-postmortem"
    directory.mkdir(mode=0o700)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)

    real_ensure_state_dir = state._ensure_state_dir

    def _ensure_then_swap(dir_path):
        real_ensure_state_dir(dir_path)
        os.rename(str(directory), str(directory) + ".real")
        os.symlink(str(elsewhere), str(directory))

    # N3a Codex round 4: the directory is now opened and verified before the
    # lock, so the window this test targets (after _ensure_state_dir's lstat
    # check, before the directory's own O_NOFOLLOW open) sits right after
    # _ensure_state_dir returns.
    monkeypatch.setattr(state, "_ensure_state_dir", _ensure_then_swap)
    probed = _track_disclosure_probing(monkeypatch)

    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)

    assert book.available is False
    assert probed["called"] is False


def test_state_directory_renamed_to_a_symlink_after_prerequisite_verification_never_touches_the_alternate_directory(
    tmp_path, monkeypatch
):
    """N3a fix round 3, new Finding 1 (BLOCKER, re-rated MAJOR and fixed the
    robust way per the dispositions): the verified state-directory identity
    must not be discarded before disclosure access. Before this fix,
    _open_verified_state_dir fstats the directory and the lock file
    on freshly opened descriptors but returns nothing, and lock_state_dir
    closes its own directory descriptor and yields only the path -- every
    secret, registry, temp-file, rename, and directory-fsync operation that
    follows reopens the directory by path. A same-principal attacker with
    write access to the state directory's own parent (the threat model the
    dispositions name: a misconfigured XDG_STATE_HOME) can, in the window
    between that verification and the first disclosure file operation,
    rename the just-verified directory away and put a symlink to a
    different, otherwise-valid directory in its place.

    The fix: lock_state_dir() keeps the verified directory descriptor open
    for the whole lock scope and every disclosure file operation runs
    relative to that descriptor (dir_fd-relative syscalls), so a later
    path-level swap cannot redirect anything -- a file descriptor is immune
    to its own path later being renamed or replaced by a symlink.

    This wraps the real prerequisite verification so the swap happens right
    after it succeeds, then requires that the alternate directory's own
    secret and registry are never read or changed (bytes and mtime) and
    that register() either keeps using the original, now path-orphaned
    directory, or returns a positional book -- never silently operating on
    the alternate directory the symlink now points at."""
    directory = tmp_path / "agent-postmortem"
    directory.mkdir(mode=0o700)
    renamed_away = tmp_path / "agent-postmortem.orphaned"

    alternate = tmp_path / "alternate"
    alternate.mkdir(mode=0o700)
    state.create_secret(state.secret_path(alternate))
    state.write_registry_bytes(
        state.registry_path(alternate), disclosure._dumps(disclosure.empty_registry())
    )
    alt_secret_before = state.secret_path(alternate).read_bytes()
    alt_secret_mtime = _mtime_ns(state.secret_path(alternate))
    alt_registry_before = state.registry_path(alternate).read_bytes()
    alt_registry_mtime = _mtime_ns(state.registry_path(alternate))

    real_verify = state._open_verified_state_dir

    def _verify_then_swap(dir_path):
        result = real_verify(dir_path)
        os.rename(str(dir_path), str(renamed_away))
        os.symlink(str(alternate), str(dir_path))
        return result

    monkeypatch.setattr(state, "_open_verified_state_dir", _verify_then_swap)

    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)

    # The alternate directory's own files must never be read or changed,
    # whether register() succeeded or fell back.
    assert state.secret_path(alternate).read_bytes() == alt_secret_before
    assert _mtime_ns(state.secret_path(alternate)) == alt_secret_mtime
    assert state.registry_path(alternate).read_bytes() == alt_registry_before
    assert _mtime_ns(state.registry_path(alternate)) == alt_registry_mtime

    if book.available:
        # Succeeded: it must have been the original, now path-orphaned
        # directory (still reachable through the kept-open descriptor),
        # never the alternate one the symlink now points at.
        assert (renamed_away / "secret").exists()
        on_disk = json.loads((renamed_away / "aliases.json").read_text(encoding="utf-8"))
        assert disclosure.validate_registry(on_disk) is not None
    else:
        assert book.alias("agent", _agent_id(1)) == "agent-1"


def test_dir_fd_unsupported_platform_fails_closed_to_a_positional_book(tmp_path, monkeypatch):
    """N3a fix round 3, new Finding 1 (BLOCKER), the dispositions' own
    closing instruction: 'fail closed if os.supports_dir_fd lacks a needed
    function.' A platform whose os module cannot actually honor dir_fd for
    the operations disclosure files need (os.open, os.unlink, os.rename)
    must never silently fall back to the unprotected path-based mode --
    _open_verified_state_dir refuses disclosure file access
    outright and register() returns a positional book, exactly like any
    other unavailable outcome. Checked at call time (monkeypatching the
    live os.supports_dir_fd, not a module-level constant computed once at
    import) so this is provable without a platform that genuinely lacks
    it."""
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())

    book = disclosure.register([("agent", _agent_id(1))], state_dir=tmp_path)

    assert book.available is False
    assert book.alias("agent", _agent_id(1)) == "agent-1"
    assert not state.secret_path(tmp_path).exists()
    assert not state.registry_path(tmp_path).exists()


# ============================================================================
# N3a fix4 review, Finding 1 (MAJOR): the dir_fd mechanism itself (the above
# swap test's own fix) had no per-primitive regression test. That swap test
# is end-to-end: it does not independently prove that each primitive which
# accepts dir_fd actually uses the descriptor it is given, as opposed to
# silently falling back to a path-based open while still accepting (and
# ignoring) the keyword -- a mutation that makes exactly one primitive
# (probe_secret) always probe by path, dir_fd or not, passed the full
# existing suite. Each test below opens a real descriptor to one directory
# (A) and passes a path/directory argument naming a different directory (B),
# then asserts the primitive observably used the descriptor, never the path.
# ============================================================================


def _open_dir_fd(directory: Path) -> int:
    return os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)


def test_probe_secret_uses_the_given_dir_fd_not_the_path(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir(mode=0o700)
    dir_b.mkdir(mode=0o700)
    secret_a = state.create_secret(dir_a / "secret")
    # b has no "secret" file at all: a path-based probe of b/secret sees
    # ABSENT, never secret_a's bytes -- so a VALID result with secret_a's
    # exact bytes proves the descriptor, not the path, was used.
    fd_a = _open_dir_fd(dir_a)
    try:
        probe, data = state.probe_secret(dir_b / "secret", dir_fd=fd_a)
    finally:
        os.close(fd_a)
    assert probe is state.Probe.VALID
    assert data == secret_a


def test_create_secret_uses_the_given_dir_fd_not_the_path(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir(mode=0o700)
    dir_b.mkdir(mode=0o700)
    fd_a = _open_dir_fd(dir_a)
    try:
        data = state.create_secret(dir_b / "secret", dir_fd=fd_a)
    finally:
        os.close(fd_a)
    assert (dir_a / "secret").exists()
    assert (dir_a / "secret").read_bytes() == data
    assert not (dir_b / "secret").exists()


def test_probe_registry_bytes_uses_the_given_dir_fd_not_the_path(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir(mode=0o700)
    dir_b.mkdir(mode=0o700)
    payload = disclosure._dumps(disclosure.empty_registry())
    state.write_registry_bytes(dir_a / "aliases.json", payload)
    # b has no registry file at all: a path-based probe of b/aliases.json
    # sees ABSENT, never payload.
    fd_a = _open_dir_fd(dir_a)
    try:
        probe, data = state.probe_registry_bytes(
            dir_b / "aliases.json", max_bytes=4 * 1024 * 1024, dir_fd=fd_a
        )
    finally:
        os.close(fd_a)
    assert probe is state.Probe.VALID
    assert data == payload


def test_create_exclusive_temp_file_uses_the_given_dir_fd_not_the_directory_argument(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir(mode=0o700)
    dir_b.mkdir(mode=0o700)
    fd_a = _open_dir_fd(dir_a)
    try:
        fd, candidate = state._create_exclusive_temp_file(dir_b, ".tmp-dirfd-test", dir_fd=fd_a)
        os.close(fd)
    finally:
        os.close(fd_a)
    assert (dir_a / candidate.name).exists()
    assert list(dir_b.iterdir()) == []


def test_verify_registry_after_commit_uses_the_given_dir_fd_not_the_path(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir(mode=0o700)
    dir_b.mkdir(mode=0o700)
    payload_a = disclosure._dumps(disclosure.empty_registry())
    payload_b = disclosure._dumps(_hand_registry(agent={HEX64_A: 12}))
    state.write_registry_bytes(dir_a / "aliases.json", payload_a)
    state.write_registry_bytes(dir_b / "aliases.json", payload_b)
    # b's registry holds different, also-valid bytes: a path-based reopen of
    # b/aliases.json would raise on the content mismatch against payload_a.
    fd_a = _open_dir_fd(dir_a)
    try:
        state._verify_registry_after_commit(dir_b / "aliases.json", payload_a, dir_fd=fd_a)
    finally:
        os.close(fd_a)


def test_write_registry_bytes_commit_uses_the_given_dir_fd_not_the_path(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir(mode=0o700)
    dir_b.mkdir(mode=0o700)
    before_a = disclosure._dumps(disclosure.empty_registry())
    before_b = disclosure._dumps(_hand_registry(agent={HEX64_A: 12}))
    state.write_registry_bytes(dir_a / "aliases.json", before_a)
    state.write_registry_bytes(dir_b / "aliases.json", before_b)
    new_payload = disclosure._dumps(_hand_registry(agent={HEX64_B: 13}))
    fd_a = _open_dir_fd(dir_a)
    try:
        state.write_registry_bytes(dir_b / "aliases.json", new_payload, dir_fd=fd_a)
    finally:
        os.close(fd_a)
    # the whole commit (temp file, rename, directory fsync, post-commit
    # reverification) must have landed in a, not b: b's own file, named by
    # the path argument, must be untouched.
    assert (dir_a / "aliases.json").read_bytes() == new_payload
    assert (dir_b / "aliases.json").read_bytes() == before_b


def test_fsync_directory_uses_the_given_dir_fd_not_the_path(tmp_path):
    dir_a = tmp_path / "a"
    dir_a.mkdir(mode=0o700)
    # a directory argument that does not exist at all: a path-based fsync
    # would raise FileNotFoundError opening it; the descriptor path never
    # touches this argument.
    missing = tmp_path / "does-not-exist-at-all"
    fd_a = _open_dir_fd(dir_a)
    try:
        state.fsync_directory(missing, dir_fd=fd_a)
    finally:
        os.close(fd_a)


# --- new Finding 4: the broad `except Exception` boundaries -----------------


def test_register_falls_back_on_a_plain_runtime_error_from_probing(tmp_path, monkeypatch):
    """N3a fix round 2, new Finding 4 / mutation N14: register()'s own
    try/except must catch an ordinary Exception, not only the disclosure/
    state error types this module already knows about -- a future refactor
    that narrows it (N14) would let a plain bug escape uncaught instead of
    falling back."""

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated ordinary bug, not a disclosure/state error")

    monkeypatch.setattr(disclosure, "_initialize_or_probe", _boom)

    book = disclosure.register([("agent", _agent_id(1))], state_dir=tmp_path)

    assert book.available is False
    assert book.alias("agent", _agent_id(1)) == "agent-1"


def test_slow_path_runtime_error_degrades_the_whole_book_atomically(tmp_path, monkeypatch):
    """N3a fix round 2, new Finding 4 / mutation N15: the AliasBook slow
    path's try/except must likewise catch an ordinary Exception and degrade
    the whole book atomically (docs/DESIGN.md 7.5a/P8), not just fall back
    for the one identity that triggered it -- replays
    test_slow_path_failure_degrades_the_whole_book_atomically with a plain
    RuntimeError in place of StateLocked, which a narrowed except (N15)
    would let escape uncaught."""
    book = disclosure.register([("agent", _agent_id(1))], state_dir=tmp_path)
    assert book.available is True
    stable_alias_1 = book.alias("agent", _agent_id(1))

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated ordinary bug in the slow path")

    monkeypatch.setattr(disclosure, "_initialize_or_probe", _boom)

    alias_2 = book.alias("agent", _agent_id(2))

    assert book.available is False
    assert book.slow_path_count == 1
    assert alias_2 == "agent-2"
    assert book.alias("agent", _agent_id(1)) == "agent-1"
    assert book.alias("agent", _agent_id(1)) != stable_alias_1


# ============================================================================
# N3a fix round 2 review, new Finding 5 (MAJOR): the untouched-state promise
# must be qualified, not contradicted, after a post-commit failure
# ============================================================================

_DESIGN_MD = Path(__file__).resolve().parents[1] / "docs" / "DESIGN.md"
_UNQUALIFIED_PROMISE = (
    "a positional alias for the whole render,\nthe real secret and registry "
    "left byte-for-byte and mtime untouched."
)


def test_design_doc_qualifies_the_untouched_state_promise_after_a_post_commit_failure():
    text = _DESIGN_MD.read_text(encoding="utf-8")
    section = text.split("### 7.5a", 1)[1].split("## 8. Distribution", 1)[0]
    assert _UNQUALIFIED_PROMISE not in section
    assert "pre-commit" in section.lower() and "post-commit" in section.lower()


def test_disclosure_module_docstring_qualifies_the_untouched_state_promise():
    text = disclosure.__doc__ or ""
    assert (
        "fallback book instead: positional agent-N/session-N/project-N per kind, in\n"
        "first-use order, available=False, with the real secret and registry left\n"
        "byte-for-byte and mtime untouched."
    ) not in text
    assert "pre-commit" in text.lower() and "post-commit" in text.lower()


def test_register_docstring_qualifies_the_untouched_state_promise():
    text = disclosure.register.__doc__ or ""
    assert (
        "returns a fallback book instead\n"
        "    (available=False, positional names only) and leaves the secret and the\n"
        "    registry byte-for-byte and mtime untouched"
    ) not in text
    assert "pre-commit" in text.lower() and "post-commit" in text.lower()


# ============================================================================
# N3a fix3 review, new finding (MAJOR): 7.5a must not call the post-commit
# registry check "by-path" -- item d (Finding 6) replaced that check with a
# reopen of the final path under O_NOFOLLOW, verified from the descriptor, so
# the contract text describing it as "by-path" now contradicts the code and
# its own neighboring paragraph.
# ============================================================================


def test_design_doc_post_commit_check_is_not_described_as_by_path():
    text = _DESIGN_MD.read_text(encoding="utf-8")
    sentence = text.split("A failure strictly *after* the commit point", 1)[1]
    sentence = sentence.split("raises `PostCommitVerificationError`", 1)[0]
    assert "by-path" not in sentence
    assert "O_NOFOLLOW" in sentence


def test_post_commit_verification_error_docstring_is_not_described_as_by_path():
    text = state.PostCommitVerificationError.__doc__ or ""
    assert "by-path" not in text


# ============================================================================
# N3a fix4 review, Finding 2 (MINOR): 7.5a's post-commit reverification
# sentence names O_NOFOLLOW but not O_NONBLOCK, even though the actual reopen
# (_verify_registry_after_commit) carries both, for the same FIFO-blocking
# reason the general probing sentence two paragraphs earlier already gives.
# A future implementer reading only this sentence as the contract for that
# specific reopen could reconstruct the pre-round-3 flags (O_RDONLY|
# O_NOFOLLOW, no O_NONBLOCK) and believe they matched it.
# ============================================================================


def test_design_doc_post_commit_reverification_mentions_o_nonblock():
    text = _DESIGN_MD.read_text(encoding="utf-8")
    sentence = text.split("A failure strictly *after* the commit point", 1)[1]
    sentence = sentence.split("raises `PostCommitVerificationError`", 1)[0]
    assert "O_NONBLOCK" in sentence


def test_held_lock_and_verified_directory_name_the_same_directory_when_replaced_after_locking(
    tmp_path, monkeypatch
):
    """N3a Codex round 4, MAJOR 1: a principal able to rename entries in the
    state directory's parent replaces the directory with a second real,
    valid 0700 directory right after the lock is acquired. The lock held
    must be the state.lock inside the directory whose descriptor every
    disclosure operation uses, and the replacement directory's registry
    must not be changed under a lock that belongs to another directory."""
    directory = tmp_path / "agent-postmortem"
    directory.mkdir(mode=0o700)
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    assert disclosure.register([], state_dir=replacement).available is True
    replacement_registry = state.registry_path(replacement)
    before = (replacement_registry.read_bytes(), replacement_registry.stat().st_mtime_ns)

    held: dict = {}
    real_acquire_lock = state._acquire_lock

    def _acquire_then_replace(fd):
        real_acquire_lock(fd)
        held["lock_ino"] = os.fstat(fd).st_ino
        if not held.get("replaced"):
            held["replaced"] = True
            os.rename(str(directory), str(tmp_path / "parked"))
            os.rename(str(replacement), str(directory))

    monkeypatch.setattr(state, "_acquire_lock", _acquire_then_replace)

    real_lock_state_dir = state.lock_state_dir

    @contextlib.contextmanager
    def _checking_lock_state_dir(*args, **kwargs):
        with real_lock_state_dir(*args, **kwargs) as locked:
            held["same_directory"] = (
                os.stat("state.lock", dir_fd=locked.dir_fd, follow_symlinks=False).st_ino
                == held["lock_ino"]
            )
            yield locked

    monkeypatch.setattr(state, "lock_state_dir", _checking_lock_state_dir)

    disclosure.register([("agent", _agent_id(1))], state_dir=directory)

    assert held["replaced"] is True
    assert held["same_directory"] is True
    now_at_path = state.registry_path(directory)  # the replacement now lives here
    assert (now_at_path.read_bytes(), now_at_path.stat().st_mtime_ns) == before


def test_lock_file_symlink_is_refused_on_the_disclosure_path(tmp_path, monkeypatch):
    """N3a Codex round 4, MAJOR 1 fix: state.lock is opened relative to the
    verified directory with O_NOFOLLOW. A state.lock that is a symlink to an
    otherwise valid 0600 file elsewhere would pass every fstat check if it
    were followed, while serializing on a different file than the one
    other processes lock; it must be refused before either disclosure file
    is probed."""
    directory = tmp_path / "agent-postmortem"
    directory.mkdir(mode=0o700)
    elsewhere = tmp_path / "elsewhere.lock"
    elsewhere.write_bytes(b"")
    os.chmod(elsewhere, 0o600)
    os.symlink(str(elsewhere), str(directory / "state.lock"))
    probed = _track_disclosure_probing(monkeypatch)

    book = disclosure.register([("agent", _agent_id(1))], state_dir=directory)

    assert book.available is False
    assert probed["called"] is False
    assert (directory / "state.lock").is_symlink()


def test_held_lock_matches_the_verified_directory_when_replaced_just_before_the_directory_open(
    tmp_path, monkeypatch
):
    """N3a Codex round 4, MAJOR 1, the reviewer's own window: the directory
    is replaced by a second real, valid 0700 directory after the path setup
    but right before the directory is opened for verification. Whatever
    directory ends up verified, the lock held must be that directory's own
    state.lock: a lock opened by path before the directory open (the old
    order) would belong to the parked directory instead."""
    directory = tmp_path / "agent-postmortem"
    directory.mkdir(mode=0o700)
    assert disclosure.register([], state_dir=directory).available is True  # creates its state.lock
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    assert disclosure.register([], state_dir=replacement).available is True

    held: dict = {}
    real_open_verified = state._open_verified_state_dir

    def _replace_then_open(dir_path):
        if not held.get("replaced"):
            held["replaced"] = True
            os.rename(str(directory), str(tmp_path / "parked"))
            os.rename(str(replacement), str(directory))
        return real_open_verified(dir_path)

    monkeypatch.setattr(state, "_open_verified_state_dir", _replace_then_open)

    real_acquire_lock = state._acquire_lock

    def _recording_acquire(fd):
        real_acquire_lock(fd)
        held["lock_ino"] = os.fstat(fd).st_ino

    monkeypatch.setattr(state, "_acquire_lock", _recording_acquire)

    real_lock_state_dir = state.lock_state_dir

    @contextlib.contextmanager
    def _checking_lock_state_dir(*args, **kwargs):
        with real_lock_state_dir(*args, **kwargs) as locked:
            held["same_directory"] = (
                os.stat("state.lock", dir_fd=locked.dir_fd, follow_symlinks=False).st_ino
                == held["lock_ino"]
            )
            yield locked

    monkeypatch.setattr(state, "lock_state_dir", _checking_lock_state_dir)

    disclosure.register([("agent", _agent_id(1))], state_dir=directory)

    assert held["replaced"] is True
    assert held["same_directory"] is True
