"""Where postmortem remembers which rules it has applied, and what it has
already reported to a session's transcript.

State path: $XDG_STATE_HOME/agent-postmortem/state.json, or
~/.local/state/agent-postmortem/state.json if XDG_STATE_HOME is unset or
empty. The containing directory is created mode 0700; state.json and
state.lock are written mode 0600. This is the one place raw session ids and
locators may be stored; nothing in it is ever printed by any renderer
(docs/DESIGN.md section 2).

Schema version 2:
    {"version": 2, "min_reader": 2,
     "applied": [{"rule_id", "target", "applied_at"}],
     "sessions": {"<session-id>": {"units": {...}, "last_check": iso}},
     "digest": {"pending": ["<unit-id>", ...]}}

transaction() is the only write path; update(fn) is a thin wrapper around it
for the common "read, mutate, write" case. transaction() acquires an
exclusive, non-blocking flock on state.lock, retrying in short
(LOCK_POLL_SECONDS) steps for up to LOCK_TIMEOUT_SECONDS before giving up and
raising StateLocked -- never a single blocking flock call, never a busy loop
longer than the timeout, and never a retry for an OSError from flock that
is not actual contention (BlockingIOError, EAGAIN, EACCES); any other OSError
propagates immediately as StateError. Once the lock is held it loads and
migrates the current file and yields it; on normal exit it writes the
(possibly mutated) result back atomically -- a temp file in the same
directory, fsynced, renamed over state.json, then the containing directory
itself fsynced -- and verifies the final permissions by stat before
releasing the lock. If the caller's code raises instead, nothing is written.
`applied` (docs/DESIGN.md 9) is legacy: nothing in this build ever appends a
new entry to it (rules.apply_rule() only ever reads a target file, never
writes one), but existing entries are still read here, migrated, and kept,
so a v0.1 state file's history still feeds `rules review`.

The state directory and state.lock are lstat-checked before every use and
rejected as StateError if either is a symlink or the wrong kind of thing
(directory expected for one, a regular file for the other); state.lock is
then opened with O_NOFOLLOW where the platform supports it. mkdir(exist_ok=
True) and a plain O_CREAT open both silently accept an existing symlink,
which is exactly the gap this closes.

A file whose "version", or whose "min_reader", is newer than this reader
understands raises StateTooNew before anything else is touched: never
written, never backed up, never migrated. A file that is not valid UTF-8,
is not valid JSON once decoded, or whose JSON is not the right shape at
all, is copied aside to "state.json.corrupt-<UTC timestamp with
microseconds>" (O_CREAT | O_EXCL, mode 0600, a numeric-suffix retry on a
same-microsecond collision -- never check-then-create) and a fresh v2 state
is used in its place. A file that parses and carries a version this reader
understands, but has a malformed namespace or a malformed entry inside an
otherwise well-formed one (for example one bad dict inside "applied", or
"sessions" being a string instead of a dict), is backed up the same way,
but only what is actually malformed is dropped -- every well-formed entry,
in every namespace, survives untouched. If the backup itself cannot be
created or written, that raises StateError and nothing is reset: docs/
DESIGN.md 7.5's "never reset without a completed backup".

A filesystem failure reading a file that does exist (permission denied, an
I/O error) is not corruption and is never treated as one: it propagates as
an OSError instead of silently resetting state that is actually fine but
unreadable right now.

load_state() is a read-only convenience for callers that only want the
current state (the CLI's `trend` command, for instance): it runs the same
load-and-migrate path as transaction(), including the StateTooNew guard,
but never writes anything -- not a migrated v1 file, not a repaired
namespace, and not a corrupt-file backup. Only transaction()/update(),
under the lock, ever persist a migration, a repair, or a backup.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

# open()/os.open() with O_NOFOLLOW refuses to follow a symlink in the final
# path component; not every platform has the flag (Windows does not), so
# this degrades to 0 (no extra protection) there rather than failing import.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# O_DIRECTORY makes an open() fail outright if the final path component is
# not a directory, instead of silently succeeding (N3a fix round 2,
# Finding 4). Same degrade-to-0 reasoning as _NOFOLLOW above.
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

# N3a fix round 3, new Finding 1: the real os.open/os.unlink/os.rename
# function objects, captured once at import time, before any test (or
# anything else) can have monkeypatched os.open etc. for an unrelated
# reason. _dir_fd_capable() below checks membership of THESE captured
# objects in the live os.supports_dir_fd set, not of whatever os.open
# currently resolves to: many existing tests wrap os.open in a tracking
# shim for reasons that have nothing to do with dir_fd support (fstat
# tampering, call counting, ...), and a tracking shim is never itself a
# member of os.supports_dir_fd -- checking the live os.open would make the
# capability check spuriously fail (and silently mask the real check this
# module actually needs to prove) the moment any such test wraps it.
# os.supports_dir_fd itself is still read live (os.supports_dir_fd, not a
# captured copy), so a test can monkeypatch that specific attribute to
# exercise the fail-closed path on a platform that is actually capable.
_DIR_FD_OPEN = os.open
_DIR_FD_UNLINK = os.unlink
_DIR_FD_RENAME = os.rename

_VERSION = 2
_MIN_READER = 2

LOCK_TIMEOUT_SECONDS = 2.0
LOCK_POLL_SECONDS = 0.05

# How many times _open_lock_file_at tries the descriptor-relative create of
# state.lock when it fails with ENOENT (see that function).
_LOCK_CREATE_ATTEMPTS = 5

# Only these mean "someone else holds the lock right now": retry until the
# timeout. Any other OSError from flock (EBADF, EINVAL, ...) is a real
# error, not contention, and must propagate right away.
_CONTENTION_ERRNOS = {errno.EAGAIN, errno.EACCES}
if hasattr(errno, "EWOULDBLOCK"):
    _CONTENTION_ERRNOS.add(errno.EWOULDBLOCK)


class StateError(Exception):
    """Base class for state store errors that a caller should report,
    never one it should silently swallow into "no state"."""


class StateLocked(StateError):
    """state.lock could not be acquired within LOCK_TIMEOUT_SECONDS. The
    CLI reports this to the user; a hook (docs/DESIGN.md 7.2) catches it
    and stays silent rather than failing a turn."""


class StateTooNew(StateError):
    """state.json's version is newer than this reader supports. The file
    is left exactly as it was: not written, not migrated, not backed up."""


class PostCommitVerificationError(StateError):
    """N3a fix round 2, Finding 1 (BLOCKER): raised only after a disclosure
    write's actual commit point has already passed -- os.replace() for the
    alias registry, or (for the installation secret, whose own commit
    point is simply "create_secret() returned without raising") the
    disclosure-specific strict directory fsync that runs right after it --
    by a step that is itself strictly post-commit (the strict directory
    fsync, or the final post-commit reverification: a reopen of the final
    path with O_NOFOLLOW, verified from that descriptor, never a later,
    separate, symlink-following stat() of the path). The file on disk
    already reflects the new content; a caller must never treat this the
    same way as a failure before the commit point. agent_postmortem.
    disclosure re-probes the exact file under the same lock and returns
    the committed result if it still reads back valid and equal to what
    was written, else falls back (docs/DESIGN.md 7.5a)."""


def now_iso() -> str:
    """The current time as an ISO 8601 UTC timestamp, second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso_micro() -> str:
    """UTC timestamp with microseconds, safe to use as (most of) a
    filename: no colons, no plus sign."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def default_state() -> dict:
    return {
        "version": _VERSION,
        "min_reader": _MIN_READER,
        "applied": [],
        "sessions": {},
        "digest": {"pending": []},
    }


def state_dir() -> Path:
    """$XDG_STATE_HOME/agent-postmortem, else ~/.local/state/agent-postmortem.

    Reads the environment at call time, not import time, so a test can
    monkeypatch XDG_STATE_HOME per test without reloading this module.
    """
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "agent-postmortem"


def state_path() -> Path:
    return state_dir() / "state.json"


def _lock_path(state_file: Path) -> Path:
    return state_file.parent / "state.lock"


def _backup_writer(state_file: Path) -> Callable[[bytes], None]:
    """A backup() callback for _load_and_migrate, bound to state_file.

    Must only be invoked while state.lock is held (update()/transaction()):
    the state lock is what makes the "no two backups collide, no backup
    check-then-create race" guarantee true. Creates
    'state.json.corrupt-<timestamp>[-N]' with O_CREAT | O_EXCL, mode 0600,
    retrying with a numeric suffix on FileExistsError rather than checking
    existence first and creating second. Raises StateError, never silently
    swallowed, if the backup cannot be created or written -- docs/
    DESIGN.md 7.5: never reset state without a completed backup.
    """

    def _write_backup(data: bytes) -> None:
        stamp = _now_iso_micro()
        suffix = 0
        while True:
            name = (
                f"{state_file.name}.corrupt-{stamp}"
                if suffix == 0
                else f"{state_file.name}.corrupt-{stamp}-{suffix}"
            )
            candidate = state_file.with_name(name)
            try:
                fd = os.open(
                    str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW, 0o600
                )
            except FileExistsError:
                suffix += 1
                continue
            except OSError as exc:
                raise StateError(
                    f"could not create corrupt-state backup {candidate}: {exc}"
                ) from exc
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise StateError(
                    f"could not write corrupt-state backup {candidate}: {exc}"
                ) from exc
            return

    return _write_backup


def _valid_applied_entry(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("rule_id"), str)
        and isinstance(entry.get("target"), str)
        and isinstance(entry.get("applied_at"), str)
    )


def _repair_applied(value: object) -> tuple[list, bool]:
    """(repaired applied list, whether anything was dropped or reset).

    docs/DESIGN.md 7.5: applied is a list of dicts with string rule_id,
    target, applied_at. If the whole thing is not a list, the namespace is
    reset to []. If it is a list, a malformed entry inside it is dropped;
    every well-formed entry survives, in order.
    """
    if not isinstance(value, list):
        return [], True
    kept = [entry for entry in value if _valid_applied_entry(entry)]
    return kept, len(kept) != len(value)


def _repair_sessions(value: object) -> tuple[dict, bool]:
    """docs/DESIGN.md 7.5: sessions is a dict of dicts. A value under a
    session id that is not itself a dict is dropped; the rest survive."""
    if not isinstance(value, dict):
        return {}, True
    kept = {
        key: val for key, val in value.items() if isinstance(key, str) and isinstance(val, dict)
    }
    return kept, len(kept) != len(value)


def _repair_digest(value: object) -> tuple[dict, bool]:
    """docs/DESIGN.md 7.5: digest is a dict whose pending is a list of
    strings. A non-string entry inside pending is dropped; the rest
    survive."""
    if not isinstance(value, dict):
        return {"pending": []}, True
    pending = value.get("pending")
    if not isinstance(pending, list):
        return {"pending": []}, True
    kept = [item for item in pending if isinstance(item, str)]
    return {"pending": kept}, kept != pending


def _load_and_migrate(state_file: Path, backup: Callable[[bytes], None] | None = None) -> dict:
    """Read state_file and return a well-formed, current-version dict.

    Never writes state.json itself -- callers decide whether the result
    gets persisted (update()/transaction()) or just handed back
    (load_state()). Raises StateTooNew for a version, or a min_reader,
    this reader does not support, before any migration or repair is even
    considered, and lets an OSError from a read of an existing-but-
    unreadable file propagate (see module docstring).

    Every other malformed case is repaired in memory. `backup`, when given,
    is called with the file's original bytes exactly once, before any
    repair is applied, whenever a repair is about to happen; it is the
    caller's job to actually persist that backup (see _backup_writer) --
    load_state() passes backup=None and therefore never creates one. If
    `backup` raises, that propagates immediately and no repaired data is
    ever returned: docs/DESIGN.md 7.5's "never reset without a completed
    backup".
    """
    if not state_file.exists():
        return default_state()

    raw_bytes = state_file.read_bytes()

    def _reset_whole_file() -> dict:
        if backup is not None:
            backup(raw_bytes)
        return default_state()

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _reset_whole_file()

    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return _reset_whole_file()

    if not isinstance(raw, dict):
        return _reset_whole_file()

    version = raw.get("version")
    if not isinstance(version, int):
        return _reset_whole_file()

    if version > _VERSION:
        raise StateTooNew(
            f"state.json version {version} is newer than this build of "
            f"agent-postmortem understands (max {_VERSION}); refusing to "
            "read or modify it. Upgrade agent-postmortem."
        )

    min_reader = raw.get("min_reader")
    if isinstance(min_reader, int) and min_reader > _MIN_READER:
        raise StateTooNew(
            f"state.json min_reader {min_reader} is newer than this build "
            f"of agent-postmortem understands (max {_MIN_READER}); refusing "
            "to read or modify it. Upgrade agent-postmortem."
        )

    if version < 1:
        return _reset_whole_file()

    if version == 1:
        applied, changed = _repair_applied(raw.get("applied"))
        if changed and backup is not None:
            backup(raw_bytes)
        return {
            "version": _VERSION,
            "min_reader": _MIN_READER,
            "applied": applied,
            "sessions": {},
            "digest": {"pending": []},
        }

    # version == 2: keep every well-formed entry in every namespace, drop
    # only what is malformed, and back up the original file once if
    # anything at all needed dropping.
    applied, applied_changed = _repair_applied(raw.get("applied"))
    sessions, sessions_changed = _repair_sessions(raw.get("sessions"))
    digest, digest_changed = _repair_digest(raw.get("digest"))

    if (applied_changed or sessions_changed or digest_changed) and backup is not None:
        backup(raw_bytes)

    return {
        "version": _VERSION,
        "min_reader": _MIN_READER,
        "applied": applied,
        "sessions": sessions,
        "digest": digest,
    }


def load_state(path: Path | None = None) -> dict:
    """The current state, migrated if needed. Read-only: see module docstring."""
    state_file = Path(path) if path is not None else state_path()
    return _load_and_migrate(state_file)


def _reject_symlink_or_wrong_kind(path: Path, *, expect_dir: bool, what: str) -> None:
    """lstat path (never following a symlink) and raise StateError if it is
    a symlink, or exists as the wrong kind of thing. Path.mkdir(exist_ok=
    True) and a plain os.open(O_CREAT) both happily accept an existing
    symlink -- mkdir's exist_ok check calls is_dir(), which follows it --
    so this must run first, before either is ever called (docs/DESIGN.md
    7.5)."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return  # does not exist yet: the caller is about to create it
    except OSError as exc:
        raise StateError(f"could not inspect {what} {path}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise StateError(f"refusing to use a symlinked {what}: {path}")
    if expect_dir and not stat.S_ISDIR(st.st_mode):
        raise StateError(f"{what} exists and is not a directory: {path}")
    if not expect_dir and not stat.S_ISREG(st.st_mode):
        raise StateError(f"{what} exists and is not a regular file: {path}")


def _ensure_state_dir(directory: Path) -> None:
    _reject_symlink_or_wrong_kind(directory, expect_dir=True, what="state directory")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass  # best effort: the final mode is verified for real in transaction()


def _open_lock_file(lock_file: Path) -> int:
    """lstat-check then open state.lock with O_NOFOLLOW (where the platform
    supports it) so a symlinked lock file is rejected outright instead of
    silently followed."""
    _reject_symlink_or_wrong_kind(lock_file, expect_dir=False, what="state lock file")
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR | _NOFOLLOW, 0o600)
    try:
        os.chmod(lock_file, 0o600)
    except OSError:
        pass  # best effort: the final mode is verified for real in transaction()
    return fd


def _verify_mode(path: Path, expected: int, what: str) -> None:
    """Confirm, by stat -- not by trusting an earlier chmod's return value
    -- that path actually carries the permissions we need. Raises
    StateError, fail closed, when it does not: a filesystem that silently
    ignores chmod must not be mistaken for one that enforced it."""
    try:
        found = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise StateError(f"could not verify permissions on {what} {path}: {exc}") from exc
    if found != expected:
        raise StateError(
            f"could not enforce {oct(expected)} permissions on {what} {path} (found {oct(found)})"
        )


def _fsync_dir(dir_path: Path) -> None:
    """Best-effort fsync of a directory's entry, so a rename into it is
    durable and not just sitting in a page cache. Never blocks a write that
    already landed: a directory that cannot be opened or fsynced (some
    filesystems do not support it) is not a reason to raise. Mirrors
    rules.py's own _fsync_dir (duplicated, not imported, to keep rules.py
    the only module that depends on state.py, never the reverse)."""
    try:
        fd = os.open(str(dir_path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_state(state_file: Path, data: dict) -> None:
    """Write data atomically: a temp file in the same directory, fsynced,
    renamed over state_file, then the containing directory is fsynced too
    so the rename itself is durable. mkstemp already creates the temp file
    mode 0600; chmod after is belt and braces."""
    directory = state_file.parent
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".state-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, state_file)
        _fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _is_lock_contention(exc: OSError) -> bool:
    """True only for the errnos that mean "someone else holds this lock
    right now" (BlockingIOError, EAGAIN, EACCES). Anything else (EBADF,
    EINVAL, ...) is a real error, not contention."""
    if isinstance(exc, BlockingIOError):
        return True
    return exc.errno in _CONTENTION_ERRNOS


def _acquire_lock(fd: int) -> None:
    """Exclusive, non-blocking flock, retried in LOCK_POLL_SECONDS steps
    until LOCK_TIMEOUT_SECONDS have elapsed, then StateLocked. Never a
    single blocking flock() call and never a sleep loop longer than the
    timeout: real elapsed time is checked with time.monotonic() every
    iteration, not counted in fixed steps. Only a contention error
    (_is_lock_contention) is retried; any other OSError propagates
    immediately as StateError, never silently retried for the full
    timeout and misreported as StateLocked."""
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if not _is_lock_contention(exc):
                raise StateError(f"could not lock state.lock: {exc}") from exc
            if time.monotonic() >= deadline:
                raise StateLocked(
                    f"could not acquire {LOCK_TIMEOUT_SECONDS:.0f}s lock on state.lock; "
                    "another agent-postmortem process is writing state"
                ) from None
            time.sleep(LOCK_POLL_SECONDS)


@contextmanager
def transaction(path: Path | None = None) -> Iterator[dict]:
    """The primitive update() is built on.

    Acquires state.lock (StateLocked on timeout), loads and migrates the
    current state -- backing up and repairing under the lock as needed --
    and yields the resulting dict for the caller's `with` block to read
    and/or mutate in place. On normal exit from that block the (possibly
    mutated) dict is written back atomically and its final permissions
    verified, still under the lock; if the block raises, none of that runs
    -- state.json is left exactly as it was, and the exception propagates
    after the lock is released. The lock is released, and its fd closed,
    in every case (commit, exception, or a raise from the load/migrate
    step itself).

    Because the caller's own code runs while the lock is held, a hook can
    make an external write of its own and a matching state record part of
    one transaction, the state lock then serializing concurrent operations
    against that same external target too, not only concurrent state.json
    writers. Nothing in this build does that for a rule any more: rules.
    apply_rule() (docs/DESIGN.md 9) only ever reads a target file, so it
    never calls this function at all.
    """
    state_file = Path(path) if path is not None else state_path()
    directory = state_file.parent
    _ensure_state_dir(directory)

    lock_file = _lock_path(state_file)
    fd = _open_lock_file(lock_file)
    try:
        _acquire_lock(fd)
        data = _load_and_migrate(state_file, backup=_backup_writer(state_file))
        yield data
        _write_state(state_file, data)
        _verify_mode(directory, 0o700, "state directory")
        _verify_mode(state_file, 0o600, "state file")
        _verify_mode(lock_file, 0o600, "state lock file")
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def update(fn: Callable[[dict], dict | None], path: Path | None = None) -> dict:
    """The only write path for state.json.

    A thin wrapper around transaction(): calls fn(state) -- which may
    mutate the dict in place, return a replacement dict, or both -- inside
    one transaction and returns the state that was written. Raises
    StateTooNew, propagates an OSError from an unreadable existing file,
    or propagates whatever fn itself raises; in every one of those cases
    nothing is written and the lock is still released.
    """
    with transaction(path) as data:
        result = fn(data)
        if isinstance(result, dict) and result is not data:
            data.clear()
            data.update(result)
    return data


# --- N3a: the disclosure secret and alias registry (docs/DESIGN.md 7.5) -----
#
# Two more files live beside state.json in the same directory and share its
# lock: "secret" (32 random bytes, mode 0600, created once) and
# "aliases.json" (the alias registry, mode 0600, written under the lock).
# Neither is state.json: lock_state_dir() below holds state.lock without
# loading or writing state.json at all, so agent_postmortem.disclosure can
# read or write the secret and the registry under the same lock that
# serializes state.json's own writers, without the two ever interleaving
# unsafely. Every actual filesystem call for either file lives here, never
# in disclosure.py (docs/DESIGN.md 5.4, tests/test_no_write_surface.py);
# disclosure.py decides *what* to read or write, this module is the only
# place that touches a file descriptor to do it.


class Probe(Enum):
    """The file-level outcome of reading the secret or the registry file:
    ABSENT (does not exist), VALID (passed every structural check and was
    read in full), or INVALID (exists but is a symlink, the wrong kind of
    thing, the wrong mode, the wrong owner, the wrong size, or -- for the
    registry -- too large to even read). INVALID and ABSENT are reported
    separately because docs/DESIGN.md 7.5's P6 initialization table treats
    them differently (an absent secret can sometimes be created; an invalid
    one never is)."""

    ABSENT = "absent"
    VALID = "valid"
    INVALID = "invalid"


_SECRET_SIZE = 32


def _current_uid() -> int:
    """The current process's uid, read through a function so a test can
    monkeypatch it to simulate 'owned by someone else' without actually
    running as another user."""
    return os.getuid()


def secret_path(directory: Path | None = None) -> Path:
    return (directory if directory is not None else state_dir()) / "secret"


def registry_path(directory: Path | None = None) -> Path:
    return (directory if directory is not None else state_dir()) / "aliases.json"


def _dir_fd_capable() -> bool:
    """N3a fix round 3, new Finding 1 (BLOCKER, fixed the robust way): every
    disclosure file operation that runs relative to the verified directory
    descriptor needs os.open, os.unlink, and os.rename (the registry's own
    atomic commit uses os.rename, not os.replace -- see write_registry_
    bytes's docstring for why) to actually honor a dir_fd argument on this
    platform. Checks membership of the real function objects captured at
    import time (_DIR_FD_OPEN/_DIR_FD_UNLINK/_DIR_FD_RENAME), never of
    whatever os.open/os.unlink/os.rename currently resolve to -- see those
    constants' own comment for why. os.supports_dir_fd itself is read live
    (checked at call time, not once at import), so a test can monkeypatch
    that specific attribute to exercise the fail-closed path without
    needing a platform that genuinely lacks it. If this is ever False,
    lock_state_dir() refuses disclosure file access outright (StateError,
    fail closed) rather than silently falling back to a path-based reopen
    that a directory-identity race could defeat (the exact vulnerability
    this fix closes)."""
    return all(
        fn in os.supports_dir_fd for fn in (_DIR_FD_OPEN, _DIR_FD_UNLINK, _DIR_FD_RENAME)
    )


def _open_verified_state_dir(dir_path: Path) -> int:
    """Open the state directory itself with O_NOFOLLOW|O_DIRECTORY and verify
    the open descriptor by fstat: a directory, mode exactly 0700, owned by
    the current uid (N3a fix round 2, Finding 4). Also requires
    _dir_fd_capable(). Returns the open, verified descriptor; raises
    StateError, fail closed, on any mismatch.

    N3a Codex round 4, MAJOR 1: this runs BEFORE the lock file is opened,
    and state.lock is then opened relative to this descriptor
    (_open_lock_file_at), so the held lock and every disclosure operation
    always name the same directory. Opening the lock by path first and the
    directory second (the earlier order) let a principal able to rename
    entries in the parent swap in a second valid directory in between:
    the lock would belong to one directory and the secret and registry to
    another, defeating serialization. Every later disclosure operation
    runs relative to this descriptor (dir_fd), never a reopen by path."""
    try:
        dir_fd = os.open(str(dir_path), os.O_RDONLY | _O_DIRECTORY | _NOFOLLOW)
    except OSError as exc:
        raise StateError(
            f"could not open state directory {dir_path} for verification: {exc}"
        ) from exc
    try:
        dir_st = os.fstat(dir_fd)
        if not stat.S_ISDIR(dir_st.st_mode):
            raise StateError(f"state directory {dir_path} is not a directory")
        if stat.S_IMODE(dir_st.st_mode) != 0o700:
            raise StateError(
                f"state directory {dir_path} is not mode 0700 (found "
                f"{oct(stat.S_IMODE(dir_st.st_mode))}); refusing disclosure file access"
            )
        if dir_st.st_uid != _current_uid():
            raise StateError(f"state directory {dir_path} is not owned by the current user")
        if not _dir_fd_capable():
            raise StateError(
                "this platform's os module does not support dir_fd for every "
                "operation disclosure files need (os.open/os.unlink/os.rename); "
                "refusing disclosure file access rather than fall back to an "
                "unprotected path-based reopen"
            )
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


def _open_lock_file_at(dir_fd: int) -> int:
    """Open (creating if needed) state.lock relative to the verified state
    directory descriptor, O_NOFOLLOW so a symlink is refused and O_NONBLOCK
    so a FIFO cannot hang the open, mode 0600; repair the mode on the open
    descriptor (best effort; _verify_lock_fd decides). Raises StateError
    if the open itself fails (N3a Codex round 4, MAJOR 1).

    N3b, main-session finding M1: on macOS APFS, when two processes create
    the same new name relative to a directory descriptor at the same
    moment, the loser's openat(O_CREAT) fails with ENOENT (measured 5 to 15
    of 40 pairs; a path-based open of the same name never did). The
    directory descriptor is held open, so the only other cause of ENOENT
    here is the directory itself having been removed, which fails on every
    attempt. So ENOENT is retried a bounded number of times; any other
    error, or ENOENT on every attempt, raises StateError as before."""
    last_exc: OSError | None = None
    for attempt in range(1, _LOCK_CREATE_ATTEMPTS + 1):
        try:
            fd = os.open(
                "state.lock", os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | _NOFOLLOW, 0o600,
                dir_fd=dir_fd,
            )
            break
        except FileNotFoundError as exc:
            last_exc = exc
            if attempt < _LOCK_CREATE_ATTEMPTS:
                # N3b S14: a short pause (1 ms times the attempt number) so
                # the retries do not depend on a lucky interleaving; the
                # attempt count is unchanged.
                time.sleep(0.001 * attempt)
        except OSError as exc:
            raise StateError(f"could not open state.lock in the state directory: {exc}") from exc
    else:
        raise StateError(
            f"could not open state.lock in the state directory: {last_exc}"
        ) from last_exc
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass  # best effort: _verify_lock_fd checks the real mode
    return fd


def _verify_lock_fd(lock_fd: int) -> None:
    """fstat the open lock descriptor: a regular file, mode exactly 0600,
    owned by the current uid (N3a fix round 2, Finding 4). Raises
    StateError, fail closed, on any mismatch."""
    lock_st = os.fstat(lock_fd)
    if not stat.S_ISREG(lock_st.st_mode):
        raise StateError("state lock file is not a regular file")
    if stat.S_IMODE(lock_st.st_mode) != 0o600:
        raise StateError(
            f"state lock file is not mode 0600 (found {oct(stat.S_IMODE(lock_st.st_mode))}); "
            "refusing disclosure file access"
        )
    if lock_st.st_uid != _current_uid():
        raise StateError("state lock file is not owned by the current user")


@dataclass(frozen=True)
class LockedStateDir:
    """What lock_state_dir() yields (N3a fix round 3, new Finding 1): the
    state directory's own path, for building the caller-facing Path values
    secret_path()/registry_path() already return, and the verified
    directory file descriptor every disclosure file operation must run
    relative to for the rest of the lock scope. dir_fd stays valid for
    exactly that scope -- lock_state_dir() closes it on exit -- and must
    never be used, or stored, past the `with` block that produced it."""

    path: Path
    dir_fd: int


@contextmanager
def lock_state_dir(directory: Path | None = None) -> Iterator[LockedStateDir]:
    """Hold state.lock -- the same lock, same file, same directory
    transaction() uses -- without loading or writing state.json itself.

    Used by agent_postmortem.disclosure to read or write the installation
    secret and the alias registry under the same serialization state.json's
    writers get, via the same _ensure_state_dir/_open_lock_file/
    _acquire_lock path (symlink and wrong-kind rejection, 0700 directory,
    0600 lock file, StateLocked after LOCK_TIMEOUT_SECONDS of contention),
    plus (N3a fix round 2, Finding 4) a stricter fstat-based re-check of
    both before yielding: transaction()'s own best-effort chmod calls are
    not enough of a guarantee for a secret file.

    N3a fix round 3, new Finding 1 (BLOCKER): yields a LockedStateDir
    carrying both the path and the verified directory descriptor, and keeps
    that descriptor open for the whole lock scope -- it is not reopened or
    re-verified by path again anywhere in this scope. N3a Codex round 4,
    MAJOR 1: the directory is opened and verified first and state.lock is
    opened relative to it, so the held lock always belongs to the
    directory every disclosure operation uses. The lock, and the directory
    descriptor, are both released and closed on every exit, normal or
    raised.
    """
    dir_path = directory if directory is not None else state_dir()
    _ensure_state_dir(dir_path)
    dir_fd = _open_verified_state_dir(dir_path)
    try:
        fd = _open_lock_file_at(dir_fd)
        try:
            _verify_lock_fd(fd)
            _acquire_lock(fd)
            yield LockedStateDir(path=dir_path, dir_fd=dir_fd)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


def _dir_relative(path: Path, dir_fd: int | None) -> str:
    """The path argument to actually pass to a dir_fd-aware os.* call: when
    dir_fd is given, every such function silently IGNORES it if path is
    absolute (os.open/os.unlink/os.rename's own documented behavior), so
    this must be path's bare name, never the full joined path, in that
    case -- using the full path would silently fall back to the
    unprotected, non-dir_fd-relative behavior with no error at all. When
    dir_fd is None, this is str(path) unchanged: today's exact behavior,
    for every caller that still addresses a disclosure file by path alone
    (every existing direct, non-locked test in tests/test_disclosure_
    primitives.py and tests/test_state.py)."""
    return path.name if dir_fd is not None else str(path)


def _open_probe_fd(path: Path, *, dir_fd: int | None = None) -> int | None:
    """O_RDONLY|O_NONBLOCK|O_NOFOLLOW open of path, for probing the secret
    or registry file. Returns None for "does not exist" (FileNotFoundError,
    or a non-directory component in the path); raises for every other
    OSError at open time (a real failure, such as permission denied, is not
    one of P5/P4's named violations and must propagate rather than being
    silently folded into "unavailable" -- same principle as the module
    docstring's "genuine filesystem failure is not corruption").

    O_NONBLOCK matters only for a FIFO: opening one O_RDONLY would otherwise
    block waiting for a writer, and a FIFO in this spot must be rejected
    (wrong kind of thing) instantly, not hang the caller. It has no effect
    on a regular file. A symlink in the final path component raises ELOOP
    with O_NOFOLLOW on every platform this ships for; that is reported the
    same as any other open-time OSError (caller maps it to INVALID) rather
    than being special-cased here, so this function's only two outcomes are
    "doesn't exist" (None) and "opened, or a real error propagates".

    N3a fix round 3, new Finding 1: dir_fd, when given, makes this open run
    relative to that verified directory descriptor (see _dir_relative)
    instead of by path -- see lock_state_dir/LockedStateDir.
    """
    try:
        return os.open(
            _dir_relative(path, dir_fd),
            os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW,
            dir_fd=dir_fd,
        )
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return None


def probe_secret(path: Path, *, dir_fd: int | None = None) -> tuple[Probe, bytes | None]:
    """(Probe.ABSENT, None) if path does not exist. (Probe.INVALID, None)
    for a symlink, a non-regular file (directory, FIFO, ...), a mode other
    than exactly 0600, an owner other than the current uid (_current_uid),
    or a size other than exactly 32 bytes -- every check made on the
    already-opened descriptor's fstat, never a separate stat() call that
    could race a swapped-in symlink. (Probe.VALID, <32 bytes>) otherwise.
    docs/DESIGN.md 7.5/P5: any of the INVALID cases "makes the secret
    unavailable"; this function only classifies, it never repairs or
    removes anything. dir_fd: see _open_probe_fd.
    """
    try:
        fd = _open_probe_fd(path, dir_fd=dir_fd)
    except OSError as exc:
        if _NOFOLLOW and exc.errno == errno.ELOOP:
            return (Probe.INVALID, None)
        raise
    if fd is None:
        return (Probe.ABSENT, None)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return (Probe.INVALID, None)
        if stat.S_IMODE(st.st_mode) != 0o600:
            return (Probe.INVALID, None)
        if st.st_uid != _current_uid():
            return (Probe.INVALID, None)
        if st.st_size != _SECRET_SIZE:
            return (Probe.INVALID, None)
        data = os.read(fd, _SECRET_SIZE + 1)
        if len(data) != _SECRET_SIZE:
            return (Probe.INVALID, None)
        return (Probe.VALID, data)
    finally:
        os.close(fd)


def _fsync_dir_strict(dir_path: Path, *, dir_fd: int | None = None) -> None:
    """Like _fsync_dir, but raises OSError instead of swallowing one.

    Used only for the disclosure secret and alias registry (docs/
    DESIGN.md 7.5a, N3a fix round 2, Finding 1): a durability failure for
    either of those two files must be reported and recovered from
    (agent_postmortem.disclosure re-probes and either returns the
    committed result or falls back), never silently accepted the way
    state.json's own best-effort _fsync_dir is. state.json keeps using
    _fsync_dir unchanged -- this is a stricter sibling, not a replacement.

    N3a fix round 3, new Finding 1: when dir_fd is given, it IS the
    already-open, already-verified state directory descriptor itself
    (lock_state_dir/LockedStateDir) -- fsync it directly rather than
    reopening the directory by path a second time."""
    if dir_fd is not None:
        os.fsync(dir_fd)
        return
    fd = os.open(str(dir_path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_directory(directory: Path, *, dir_fd: int | None = None) -> None:
    """Public name for _fsync_dir_strict, for disclosure.py's P6
    initialization: after create_secret() the state directory is fsynced
    so the secret's directory entry is durable, whether or not a registry
    write follows. Raises OSError on failure (N3a fix round 2, Finding 1):
    by the time this runs the secret (and, on a cold start, the registry)
    already exist, fully written and verified, so the caller must treat a
    failure here as durability-only and re-probe rather than assuming
    nothing changed. dir_fd: see _fsync_dir_strict."""
    _fsync_dir_strict(directory, dir_fd=dir_fd)


def _verify_fd(fd: int, *, expect_regular: bool, expected_mode: int, what: str) -> None:
    """fstat the already-open descriptor and require it is a regular file
    (when expect_regular), mode exactly expected_mode, and owned by the
    current uid. Raises StateError on the open descriptor, before the
    caller's commit point (docs/DESIGN.md 7.5a, N3a fix round 2, Finding 1
    and Finding 11): every check that can fail runs here, on the fd itself,
    never via a later, separate stat() of the path that could observe a
    swapped file."""
    st = os.fstat(fd)
    if expect_regular and not stat.S_ISREG(st.st_mode):
        raise StateError(f"{what}: the open descriptor is not a regular file")
    if stat.S_IMODE(st.st_mode) != expected_mode:
        raise StateError(
            f"{what}: wrong mode on the open descriptor (found "
            f"{oct(stat.S_IMODE(st.st_mode))}, expected {oct(expected_mode)})"
        )
    if st.st_uid != _current_uid():
        raise StateError(f"{what}: the open descriptor is not owned by the current user")


def _write_full(fd: int, data: bytes, *, what: str) -> None:
    """Write every byte of data to fd, looping until done.

    POSIX permits os.write() to return fewer bytes than requested without
    raising (N3a fix round 2, Finding 2): a single unchecked os.write()
    call can silently leave a short file that is reported as complete. A
    write that makes no progress at all (a zero-length return with data
    still remaining) is treated as an error rather than retried forever.
    """
    total = len(data)
    written = 0
    view = memoryview(data)
    while written < total:
        n = os.write(fd, view[written:])
        if n <= 0:
            raise StateError(
                f"{what}: os.write() returned {n} with {total - written} of "
                f"{total} bytes remaining"
            )
        written += n


def _verify_fd_size(fd: int, expected_size: int, *, what: str) -> None:
    """Confirm, by fstat on the still-open descriptor, that exactly
    expected_size bytes actually landed (N3a fix round 2, Finding 2): the
    write loop above only proves os.write() reported writing every byte,
    not that the file's own size agrees."""
    st = os.fstat(fd)
    if st.st_size != expected_size:
        raise StateError(
            f"{what}: wrote {expected_size} bytes but the descriptor now reports "
            f"size {st.st_size}"
        )


def create_secret(path: Path, *, dir_fd: int | None = None) -> bytes:
    """Create the installation secret: secrets.token_bytes(32), written to a
    brand-new file at path with O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW. Every
    check that can fail -- regular file, mode exactly 0600, owned by the
    current uid, then (after the write loop and fsync) size exactly 32 --
    runs on the open descriptor, before the fd is ever closed (docs/
    DESIGN.md 7.5a, N3a fix round 2, Finding 1): once this function
    returns, the secret is fully committed, and the only thing that can
    still fail is the caller's own, separate, durability-only directory
    fsync (fsync_directory). Raises FileExistsError if path already exists
    (the caller must have already probed ABSENT, under the lock, before
    calling this) and StateError/OSError for any other failure.
    Never repairs, rewrites, or replaces an existing secret (docs/DESIGN.md
    7.5/P5): the exclusive create is what enforces that. A failure before
    this function returns unlinks the file it just created rather than
    leaving a present-but-unverified secret behind: the observable state
    after any failure here is deterministically "absent" (one of the two
    states P6's table actually reasons about), never a third, undocumented
    state that happens to read back as VALID anyway.

    dir_fd: see _open_probe_fd/_dir_relative.
    """
    data = secrets.token_bytes(_SECRET_SIZE)
    fd = os.open(
        _dir_relative(path, dir_fd),
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        try:
            _verify_fd(fd, expect_regular=True, expected_mode=0o600, what="disclosure secret")
            _write_full(fd, data, what="disclosure secret")
            os.fsync(fd)
            _verify_fd_size(fd, _SECRET_SIZE, what="disclosure secret")
        finally:
            os.close(fd)
    except BaseException:
        try:
            os.unlink(_dir_relative(path, dir_fd), dir_fd=dir_fd)
        except OSError:
            pass
        raise
    return data


def probe_registry_bytes(
    path: Path, *, max_bytes: int, dir_fd: int | None = None
) -> tuple[Probe, bytes | None]:
    """File-level probe of the alias registry, exactly like probe_secret:
    ABSENT if missing; INVALID for a symlink, a non-regular file, a mode
    other than 0600, an owner other than the current uid, or a size over
    max_bytes; VALID with the raw bytes otherwise. JSON parsing and P4's
    schema validation are disclosure.py's job, not this function's -- this
    layer only establishes that the bytes came from a plain,
    correctly-permissioned, small-enough file in the first place. dir_fd:
    see _open_probe_fd.
    """
    try:
        fd = _open_probe_fd(path, dir_fd=dir_fd)
    except OSError as exc:
        if _NOFOLLOW and exc.errno == errno.ELOOP:
            return (Probe.INVALID, None)
        raise
    if fd is None:
        return (Probe.ABSENT, None)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return (Probe.INVALID, None)
        if stat.S_IMODE(st.st_mode) != 0o600:
            return (Probe.INVALID, None)
        if st.st_uid != _current_uid():
            return (Probe.INVALID, None)
        if st.st_size > max_bytes:
            return (Probe.INVALID, None)
        chunks = []
        remaining = st.st_size
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != st.st_size:
            return (Probe.INVALID, None)
        return (Probe.VALID, data)
    finally:
        os.close(fd)


def _create_exclusive_temp_file(
    directory: Path, prefix: str, *, dir_fd: int | None = None
) -> tuple[int, Path]:
    """O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW, mode 0600, retried with a
    numeric suffix on FileExistsError -- never check-then-create (same
    pattern as _backup_writer above). Returns (fd, candidate), candidate
    always the full directory-joined Path (for reporting/consistency); the
    caller uses candidate.name, not candidate, for any later dir_fd-relative
    operation on it (see _dir_relative). dir_fd: when given, the open
    itself runs relative to it, by the bare candidate name alone -- never
    the joined path, which would silently ignore dir_fd."""
    suffix = 0
    while True:
        name = f"{prefix}-{os.getpid()}" if suffix == 0 else f"{prefix}-{os.getpid()}-{suffix}"
        candidate = directory / name
        try:
            fd = os.open(
                name if dir_fd is not None else str(candidate),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            suffix += 1
            continue
        return fd, candidate


def _verify_registry_after_commit(
    path: Path, expected: bytes, *, dir_fd: int | None = None
) -> None:
    """N3a fix round 2 (continued), new Finding 6 (MINOR): the post-commit
    check right after write_registry_bytes's own os.replace() must not
    trust a separate, symlink-following stat() of the path -- the same
    class of TOCTOU _verify_fd already closes for the *pre*-commit
    descriptor checks, but _verify_mode (state.json's own helper, by path)
    does not close for this specific caller: a same-principal race right
    after the rename could swap aliases.json for a symlink to some other,
    legitimately mode-0600 file that would pass a plain stat(). Reopen the
    final path with O_NOFOLLOW, then verify everything that can fail --
    regular file, mode exactly 0600, owned by the current uid, and that the
    bytes actually on disk are the exact bytes this call just wrote -- from
    that one freshly opened descriptor. This is a disclosure-only sibling
    of _verify_mode; state.json's own writer keeps using _verify_mode
    unchanged, since that caller's own prerequisite checks (docs/
    DESIGN.md 7.5a) and threat model are different.

    N3a fix round 3, new Finding 2 (MAJOR): the open also carries
    O_NONBLOCK, for the same reason _open_probe_fd already does -- without
    it, a FIFO swapped in at the final path right after os.replace() would
    block this open indefinitely waiting for a writer that is never coming,
    while state.lock is held. O_NONBLOCK has no effect on a regular file,
    so the normal case is unchanged; a FIFO instead opens immediately and
    is rejected by the regular-file check below.

    dir_fd: see _open_probe_fd/_dir_relative.
    """
    try:
        fd = os.open(
            _dir_relative(path, dir_fd), os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW, dir_fd=dir_fd
        )
    except OSError as exc:
        raise StateError(
            f"could not reopen alias registry {path} for post-commit verification: {exc}"
        ) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise StateError(f"alias registry {path} is not a regular file after commit")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise StateError(
                f"alias registry {path} is not mode 0600 after commit (found "
                f"{oct(stat.S_IMODE(st.st_mode))})"
            )
        if st.st_uid != _current_uid():
            raise StateError(f"alias registry {path} is not owned by the current user after commit")
        read_back = os.read(fd, len(expected) + 1)
        if read_back != expected:
            raise StateError(f"alias registry {path} content does not match what was just written")
    finally:
        os.close(fd)


def write_registry_bytes(path: Path, data: bytes, *, dir_fd: int | None = None) -> None:
    """Atomically write data (the alias registry's own encoded JSON bytes,
    already bound-checked by the caller) to path: a fresh temp file in the
    same directory, every check that can fail (regular file, mode exactly
    0600, owned by the current uid, then size) run on the open descriptor,
    fsync, then an atomic rename over path. That rename is this write's
    commit point (docs/DESIGN.md 7.5a, N3a fix round 2, Finding 1): a
    failure before it unlinks the temp file and leaves path untouched,
    exactly as before. A failure after it (the strict directory fsync, or
    the final post-commit reverification, N3a fix round 2 continued,
    Finding 6: reopened with O_NOFOLLOW rather than a separate,
    symlink-following stat()) raises PostCommitVerificationError instead --
    path already holds the new content, so the caller must re-probe and
    decide, never assume nothing changed.

    dir_fd (N3a fix round 3, new Finding 1): when given, every operation
    here -- the temp file's own create, its rename over path, and the
    directory fsync -- runs relative to that verified directory descriptor
    instead of by path. The commit-point rename uses os.rename(...,
    src_dir_fd=dir_fd, dst_dir_fd=dir_fd) rather than os.replace(): POSIX's
    rename() is already an atomic overwrite (the same guarantee os.replace
    gives on every platform this ships for, since fcntl/flock already make
    this module POSIX-only), but os.replace's own dir_fd support is not
    reliably reported by os.supports_dir_fd even on a platform where it
    actually works, so the dir_fd-relative commit uses os.rename, the
    function os.supports_dir_fd actually lists (see _dir_fd_capable). When
    dir_fd is None, every call below is unchanged from before this fix:
    the same path-based os.replace(), for every existing direct,
    non-locked caller.
    """
    directory = path.parent
    fd, tmp_path = _create_exclusive_temp_file(directory, ".aliases.json.tmp", dir_fd=dir_fd)
    try:
        try:
            _verify_fd(
                fd, expect_regular=True, expected_mode=0o600, what="alias registry temp file"
            )
            _write_full(fd, data, what="alias registry")
            os.fsync(fd)
            _verify_fd_size(fd, len(data), what="alias registry")
        finally:
            os.close(fd)
    except BaseException:
        try:
            os.unlink(_dir_relative(tmp_path, dir_fd), dir_fd=dir_fd)
        except OSError:
            pass
        raise

    try:
        if dir_fd is not None:
            os.rename(tmp_path.name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            os.replace(str(tmp_path), str(path))
    except OSError:
        try:
            os.unlink(_dir_relative(tmp_path, dir_fd), dir_fd=dir_fd)
        except OSError:
            pass
        raise

    # COMMIT POINT PASSED: `path` now holds the new content. A failure from
    # here on is durability/verification only and must never be reported
    # the same way as a failure before this point.
    try:
        _fsync_dir_strict(directory, dir_fd=dir_fd)
        _verify_registry_after_commit(path, data, dir_fd=dir_fd)
    except (OSError, StateError) as exc:
        raise PostCommitVerificationError(
            f"alias registry write committed but could not be verified durable: {exc}"
        ) from exc
