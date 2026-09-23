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
Because the caller's own code runs under the lock, apply_rule() (docs/
DESIGN.md 9) uses transaction() directly to make writing its target file and
recording that write into state one atomic unit.

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
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# open()/os.open() with O_NOFOLLOW refuses to follow a symlink in the final
# path component; not every platform has the flag (Windows does not), so
# this degrades to 0 (no extra protection) there rather than failing import.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

_VERSION = 2
_MIN_READER = 2

LOCK_TIMEOUT_SECONDS = 2.0
LOCK_POLL_SECONDS = 0.05

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
    """The primitive both update() and apply_rule() are built on.

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

    Because the caller's own code runs while the lock is held, a rule
    library or hook can make an external write (docs/DESIGN.md 9: writing
    the target file apply_rule edits) and its matching state record part
    of one transaction: the state lock then also serializes concurrent
    operations against that same external target, not only concurrent
    state.json writers.
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
