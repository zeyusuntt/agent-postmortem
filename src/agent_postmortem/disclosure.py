"""Disclosure levels and stable aliases (docs/DESIGN.md 4.13.5, 4.13.11's
"N3, disclosure" items, 7.5). N3a only: pure logic plus the orchestration
that drives agent_postmortem.state's disclosure I/O primitives. Nothing
here is wired into a renderer, a CLI flag, a hook, or `watch` yet -- that
is N3b. No command's output changes because this module exists.

LEVELS names the three disclosure levels (share/local/raw); the alias
kinds are agent, session, project. An AliasBook, returned by register(),
is the one object a caller holds for the lifetime of one command: it was
handed every identity the command already knows it will render, assigned
each of them a stable alias under one lock hold (docs/DESIGN.md 7.5/P9),
and from then on answers alias(kind, real_id) from memory. A registration
that cannot complete for any reason (secret or registry unavailable, lock
contention, a failed write, a bound that would be exceeded) returns a
fallback book instead: positional agent-N/session-N/project-N per kind, in
first-use order, available=False. A pre-commit failure leaves the real
secret and registry exactly as they were, byte-for-byte and mtime
untouched; a post-commit failure (docs/DESIGN.md 7.5a) is the one
exception -- the new file may already be installed, and this fallback is
what register() returns only once re-probing it and requiring a fresh
directory-durability confirmation have both still failed to confirm it,
never by silently claiming nothing changed. A fallback book never
contains a real id, by construction -- it is never handed one.

Every actual file read or write happens in agent_postmortem.state, never
here (docs/DESIGN.md 5.4, tests/test_no_write_surface.py): this module
calls state.lock_state_dir(), state.probe_secret(), state.create_secret(),
state.probe_registry_bytes(), and state.write_registry_bytes(), and
decides, in memory, what those calls should do and what they returned.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid as uuid_module
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent_postmortem import state

LEVELS: tuple[str, ...] = ("share", "local", "raw")

# N3b P15: the very first line of text output at local and raw. Share
# prints no such line, and JSON never does.
FIRST_LINE: dict[str, str] = {
    "local": "disclosure: local (real names and paths; do not paste)",
    "raw": "disclosure: raw (real names, absolute paths; do not paste)",
}

KINDS: tuple[str, ...] = ("agent", "session", "project")

# docs/DESIGN.md 7.5/P4: both bounds are module constants a test can
# override (monkeypatch.setattr(disclosure, "MAX_ENTRIES", ...)) to exercise
# the fallback without actually building a 4 MiB or 50,000-entry file.
MAX_ENTRIES = 50_000
MAX_BYTES = 4 * 1024 * 1024  # 4 MiB

MIN_ALIAS_LEN = 12
MAX_ALIAS_LEN = 64

# N3b prints this; N3a only defines it, so there is exactly one spelling of
# the diagnostic for every future caller to share (docs/DESIGN.md 7.5/P8).
UNAVAILABLE_DIAGNOSTIC = "stable aliases unavailable"

_REGISTRY_VERSION = 1
_REGISTRY_TOP_KEYS = frozenset({"version", "agent", "session", "project"})

# docs/DESIGN.md 5.1: an agent id is 17 lowercase hex characters (the
# existing reader._AGENT_ID_RE additionally requires the first to be "a";
# this plan's P2 only requires "17 lowercase hex", the broader of the two,
# so every id reader.py accepts is also canonical here).
_AGENT_ID_RE = re.compile(r"[0-9a-f]{17}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class DisclosureContext:
    """N3b D-a: the one object a command builds once from its parsed level
    and hands to its Redactor. `level` is one of LEVELS. `book` is the
    AliasBook the command registered its identities with, or None for a
    command that renders no alias (every command in Part 1a, and a versioned
    JSON command always, D-k). `home` is the home directory structural
    redaction replaces with "~". `repo_root` is the run's repository root
    when an observation the command already made resolved one (D-n), else
    None. `identities` is what the command collected for its one
    registration. Nothing here reads the filesystem except Path.home() for
    the default home."""

    level: str
    book: "AliasBook | None" = None
    home: Path = field(default_factory=Path.home)
    repo_root: str | None = None
    # N3b P9/P11: every (kind, real_id) the command collected before it
    # rendered (the same list it registered with the book). A free-text
    # sink at share substitutes these by their alias, the run's project slug
    # before structural redaction.
    identities: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.level, str) or self.level not in LEVELS:
            raise ValueError(f"unknown disclosure level: {self.level!r}")

    @property
    def enabled(self) -> bool:
        """Structural redaction is on at every level but raw."""
        return self.level != "raw"


# --- rendered forms (P10) -----------------------------------------------------

_POSITIONAL_ALIAS_RE = re.compile(r"(?:agent|session|project)-[0-9]+")
_FORM_PREFIX = {"agent": "a:", "session": "s:", "project": "project:"}
_FILE_STEM_PREFIX = {"agent": "agent-", "session": "session-"}


def is_positional_alias(alias: str) -> bool:
    """A fallback book's name (agent-3, session-1, project-2). A stable
    alias is lowercase hex, so the two never collide."""
    return _POSITIONAL_ALIAS_RE.fullmatch(alias) is not None


def rendered_form(kind: str, alias: str) -> str:
    """P10: the text form of an identity: a:<alias>, s:<alias>,
    project:<alias>. A positional name is already its own form."""
    if is_positional_alias(alias):
        return alias
    return _FORM_PREFIX[kind] + alias


def file_stem(kind: str, alias: str) -> str:
    """P10: the file-name stem of an agent or session identity in an
    evidence pointer: agent-<alias>, session-<alias> (bare hex, no "a:"). A
    positional name is its own stem (agent-3, session-1)."""
    if is_positional_alias(alias):
        return alias
    return _FILE_STEM_PREFIX[kind] + alias


def _under(root: str, path: str) -> bool:
    """Whether `path` is `root` or lies below it, by spelling: both must be
    absolute and already normalized. A ValueError from commonpath (mixed
    drives, or one relative path) means outside."""
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


def local_path(
    value: object,
    *,
    repo_root: str | None,
    home: Path | str | None,
    base: str | None,
    outside: Callable[[str], str],
) -> str:
    """N3b P19 (D-m): the one function every sink uses to show a path at
    `local`; never returns an absolute path.

    The path is made absolute against `base` (the documented base, for
    example the run's cwd for a relative transcript path) when it is
    relative, then os.path.normpath-ed; symlinks are never resolved, so a
    path counts by how it is spelled. Inside the repository
    (os.path.commonpath([repo_root, path]) == repo_root) it is
    repo-relative ("." for the root itself); inside home but outside the
    repository it is the structural "~/.../<basename>"; outside both it is
    `outside(value)`, the caller's path-N pseudonym. `repo_root` comes only
    from an observation the command already made (D-n); None, or a root
    that is not absolute, means every path counts as outside the repository.
    A relative path with no absolute base cannot be placed and counts as
    outside. A value that is not a non-empty string without NUL is passed
    to `outside` as is.
    """
    if not isinstance(value, str) or not value or "\x00" in value:
        return outside(value) if isinstance(value, str) else outside(str(value))
    if _SCHEME_RE.match(value):
        # S11: a URL or other `scheme:` spelling is never a place on disk.
        return outside(value)
    if value == "~" or value.startswith("~/"):
        # S11: `~/` is expanded against the context's home, then the rule.
        home_text = os.path.normpath(str(home)) if home is not None else ""
        if not (home_text and os.path.isabs(home_text)):
            return outside(value)
        value = home_text + value[1:]
    elif value.startswith("//"):
        # S11: POSIX normpath keeps exactly two leading slashes; they name the
        # same place as one, so collapse them before the containment check.
        value = "/" + value.lstrip("/")
    if os.path.isabs(value):
        candidate = value
    elif base and os.path.isabs(base):
        candidate = os.path.join(base, value)
    else:
        return outside(value)
    normalized = os.path.normpath(candidate)

    if repo_root and os.path.isabs(repo_root):
        root = os.path.normpath(repo_root)
        if _under(root, normalized):
            relative = normalized[len(root):].lstrip(os.sep)
            return relative or "."

    home_str = os.path.normpath(str(home)) if home is not None else ""
    if home_str and os.path.isabs(home_str) and home_str != os.sep and _under(home_str, normalized):
        if normalized == home_str:
            return "~"
        basename = os.path.basename(normalized)
        return f"~/.../{basename}" if basename else "~"

    return outside(value)


class DisclosureError(Exception):
    """Base class for disclosure.py's own errors (not state.py's -- a
    StateError/StateLocked/OSError from the I/O layer is caught by
    register()/AliasBook.alias() and turned into a fallback, never raised
    to the caller)."""


# --- canonical id checks (P2) ------------------------------------------------


def is_canonical_id(kind: str, value: object) -> bool:
    """P2's exact shape for each kind. agent: 17 lowercase hex. session: a
    UUID, canonical lowercase-hyphenated form only (str(uuid.UUID(value))
    must round-trip to the same string, so "AAAA...", a braced form, or a
    32-hex-no-dashes spelling are all rejected even though uuid.UUID() would
    parse them). project: a non-empty string with no NUL and no newline
    (docs/DESIGN.md: "the same string `local` prints")."""
    if kind == "agent":
        return isinstance(value, str) and bool(_AGENT_ID_RE.fullmatch(value))
    if kind == "session":
        if not isinstance(value, str):
            return False
        try:
            parsed = uuid_module.UUID(value)
        except (ValueError, AttributeError, TypeError):
            return False
        return str(parsed) == value
    if kind == "project":
        return isinstance(value, str) and value != "" and "\x00" not in value and "\n" not in value
    raise ValueError(f"unknown alias kind: {kind!r}")


def require_canonical_id(kind: str, value: object) -> str:
    """is_canonical_id's gate, raising ValueError on failure: "non-canonical
    ids raise ValueError at registration" (the renderer decides positional
    names for them in N3b; N3a never silently accepts one)."""
    if not is_canonical_id(kind, value):
        raise ValueError(f"not a canonical {kind} id: {value!r}")
    return value  # type: ignore[return-value]


# --- HMAC (P1) ----------------------------------------------------------------


def hmac_hex(secret: bytes, real_id: str) -> str:
    """HMAC-SHA256 of real_id's UTF-8 bytes under secret, lowercase 64 hex
    (hexdigest() is already lowercase). A module-level function, called by
    bare name from _assign_all below, so a test can monkeypatch
    disclosure.hmac_hex to force a chosen collision without needing to
    brute-force one against a real secret."""
    return hmac.new(secret, real_id.encode("utf-8"), hashlib.sha256).hexdigest()


# --- registry schema (P4) ------------------------------------------------------


def empty_registry() -> dict:
    return {"version": _REGISTRY_VERSION, "agent": {}, "session": {}, "project": {}}


def is_empty_registry(registry: dict) -> bool:
    return all(not registry[kind] for kind in KINDS)


def count_entries(registry: dict) -> int:
    return sum(len(registry[kind]) for kind in KINDS)


def validate_registry(data: object) -> dict | None:
    """P4's exact schema, validated with nothing coerced:
    {"version": 1, "agent": {...}, "session": {...}, "project": {...}},
    no other top-level key; each table a dict mapping a lowercase 64-hex
    key to an int -- never a bool, which is a subclass of int in Python, so
    the bool check must run before the int check -- in 12..64; per kind,
    the assigned aliases (key[:length]) are unique. Returns a validated
    dict (plain dicts, independent of whatever container type the input
    used) on success, or None for any violation at all: a malformed
    top-level shape, a malformed key, a malformed length, or a duplicate
    alias. MAX_ENTRIES/MAX_BYTES are checked by the caller against the
    registry as a whole (they depend on what is about to be *added*, which
    this function does not know about), not here.
    """
    if not isinstance(data, dict):
        return None
    # N3a fix round 2, Finding 6: a plain `!= 1` comparison accepts both
    # `True` (bool is an int subclass in Python, and True == 1) and `1.0`
    # (float equality) as version 1. isinstance(x, bool) must be checked
    # before the int check, exactly as for an alias length below.
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != _REGISTRY_VERSION:
        return None
    if set(data.keys()) != _REGISTRY_TOP_KEYS:
        return None

    result: dict = {"version": _REGISTRY_VERSION}
    for kind in KINDS:
        table = data[kind]
        if not isinstance(table, dict):
            return None
        seen_aliases: set[str] = set()
        validated_table: dict[str, int] = {}
        for key, length in table.items():
            if not isinstance(key, str) or not _HEX64_RE.fullmatch(key):
                return None
            if isinstance(length, bool) or not isinstance(length, int):
                return None
            if not (MIN_ALIAS_LEN <= length <= MAX_ALIAS_LEN):
                return None
            alias = key[:length]
            if alias in seen_aliases:
                return None
            seen_aliases.add(alias)
            validated_table[key] = length
        result[kind] = validated_table
    return result


def _dumps(registry: dict) -> bytes:
    """Canonical encoding of a registry dict: stable key order (sort_keys)
    so two writers who agree on content produce byte-identical files, and a
    trailing newline, matching state.py's own state.json convention."""
    return json.dumps(registry, indent=2, sort_keys=True).encode("utf-8") + b"\n"


# --- assignment (P3) -----------------------------------------------------------


def assign_alias(table: dict[str, int], hashed: str) -> int:
    """The smallest n in MIN_ALIAS_LEN..MAX_ALIAS_LEN such that hashed[:n]
    is not already the assigned alias (key[:length]) of another entry in
    table. Does not mutate table; the caller decides whether and when to
    record the assignment, so a sequence of calls across several identities
    of a batch can see each other's just-assigned aliases by mutating the
    table in between (P3: "deterministic given registration order")."""
    existing_aliases = {key[:length] for key, length in table.items()}
    for n in range(MIN_ALIAS_LEN, MAX_ALIAS_LEN + 1):
        if hashed[:n] not in existing_aliases:
            return n
    raise DisclosureError(
        f"exhausted alias length space ({MIN_ALIAS_LEN}..{MAX_ALIAS_LEN}) for one hash"
    )


def _assign_all(
    registry: dict, secret: bytes, identities: list[tuple[str, str]]
) -> tuple[dict, dict[tuple[str, str], str], bool]:
    """Apply P3 to every (kind, real_id) in identities, in order, against a
    deep-enough copy of registry (new dicts per kind table, so the caller's
    own registry argument is never mutated in place). Returns (the possibly
    updated registry, {(kind, real_id): alias} for every identity in
    identities, whether anything actually changed). A duplicate identity in
    the same batch is only assigned once; both look-ups return the same
    alias."""
    updated = {"version": registry["version"]}
    for kind in KINDS:
        updated[kind] = dict(registry[kind])

    aliases: dict[tuple[str, str], str] = {}
    changed = False
    for kind, real_id in identities:
        key = (kind, real_id)
        if key in aliases:
            continue
        hashed = hmac_hex(secret, real_id)
        table = updated[kind]
        length = table.get(hashed)
        if length is None:
            length = assign_alias(table, hashed)
            table[hashed] = length
            changed = True
        aliases[key] = hashed[:length]
    return updated, aliases, changed


# --- initialization state machine (P6) -----------------------------------------


# N3a fix round 2 (continued), new Finding 1 (BLOCKER): how many times the
# state directory's own strict fsync is attempted before a registration
# gives up. A transient failure (fails once, succeeds on the next attempt)
# still succeeds; a persistent failure (fails on every attempt) does not
# retry forever -- it is treated exactly like any other unavailable outcome.
_DIRECTORY_FSYNC_ATTEMPTS = 2


def _directory_is_durable(directory: Path, *, dir_fd: int | None = None) -> bool:
    """N3a fix round 2 (continued), new Finding 1 (BLOCKER): a successful
    read-back of the secret or the registry only proves the namespace
    change is visible right now -- it says nothing about whether the
    directory entry that makes it visible would survive a crash, because
    the strict directory fsync (state.fsync_directory) can itself fail
    strictly after the content it is confirming was already committed
    (create_secret()'s own post-return fsync in _initialize_or_probe;
    write_registry_bytes's own PostCommitVerificationError path, recovered
    in _finalize_registration). Before accepting ANY valid secret-and-
    registry pair as grounds to hand out stable aliases, this makes a
    fresh attempt at that same strict fsync, retried up to
    _DIRECTORY_FSYNC_ATTEMPTS times total. Returns True the moment one
    attempt succeeds (a transient failure still returns True); returns
    False only once every attempt has failed (a persistent failure), which
    every caller here treats exactly like any other unavailable outcome:
    positional fallback, the real secret and registry left exactly as they
    are. dir_fd: see state.lock_state_dir/LockedStateDir (N3a fix round 3,
    new Finding 1) -- when given, the fsync runs on that verified directory
    descriptor directly, never a path-based reopen."""
    for _ in range(_DIRECTORY_FSYNC_ATTEMPTS):
        try:
            state.fsync_directory(directory, dir_fd=dir_fd)
            return True
        except OSError:
            continue
    return False


def _probe_registry(
    directory: Path, *, dir_fd: int | None = None
) -> tuple[state.Probe, dict | None]:
    """probe_registry_bytes's file-level outcome, upgraded to a content-level
    one: a file that reads back as Probe.VALID bytes but fails
    validate_registry (bad JSON, wrong schema, ...) is reported as
    Probe.INVALID here, exactly as P4 requires -- the byte-level and
    schema-level checks are both "the registry is unavailable" to anyone
    outside this module.

    N3a fix round 2, Finding 3: a decode failure also includes
    RecursionError -- pathologically nested JSON (for example 10,000 nested
    arrays) is well under MAX_BYTES but exhausts the decoder's recursion
    budget. That must classify the registry as invalid, the same as any
    other decode failure, not propagate out of this function.

    N3a fix round 2, Finding 5: MAX_ENTRIES is also enforced here, on read,
    not only by the write path (_write_if_within_bounds): an existing
    registry already over the entry bound -- however small its byte size --
    is unavailable the moment it is read.

    dir_fd: see state.lock_state_dir/LockedStateDir (N3a fix round 3, new
    Finding 1).
    """
    probe, raw = state.probe_registry_bytes(
        state.registry_path(directory), max_bytes=MAX_BYTES, dir_fd=dir_fd
    )
    if probe is not state.Probe.VALID:
        return probe, None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return state.Probe.INVALID, None
    validated = validate_registry(parsed)
    if validated is None:
        return state.Probe.INVALID, None
    if count_entries(validated) > MAX_ENTRIES:
        return state.Probe.INVALID, None
    return state.Probe.VALID, validated


def _initialize_or_probe(
    directory: Path, *, dir_fd: int | None = None, _retried: bool = False
) -> tuple[bytes | None, dict | None]:
    """Run P6's table once, assuming the caller already holds
    state.lock_state_dir(directory). dir_fd: see state.lock_state_dir/
    LockedStateDir (N3a fix round 3, new Finding 1) -- when given, every
    secret/registry operation below runs relative to that verified
    directory descriptor instead of by path.

    Returns (secret, registry) once both
    are available -- either because they already were ("normal") or because
    this call just created what P6 says is safe to create -- or (None,
    None) for every row P6 calls "unavailable". Never creates the secret
    when the registry is valid and non-empty (that would silently re-key
    every existing alias) and never creates the registry when the secret
    already exists (a fresh registry next to an old secret could reassign
    an alias that secret already vouches for). A crash between writing the
    registry and creating the secret leaves exactly "registry valid and
    empty, secret absent", the row this same function also handles by
    completing the secret -- so a retried call after a mid-flight failure
    converges correctly without any special-cased recovery path.

    N3a fix round 2, Finding 1 (BLOCKER): creating the registry can fail
    strictly after its own commit point (the registry's os.replace) with a
    state.PostCommitVerificationError. That means the file already has its
    new, correct content -- so instead of propagating (which would make the
    caller report "nothing happened" while something did), this re-runs the
    whole probe exactly once (_retried guards against looping on a
    persistent failure): by then the just-committed file reads back through
    the normal P6 rows above, so no separate recovery path is needed.

    N3a fix round 2 (continued), new Finding 1 (BLOCKER): a read-back alone
    is not enough, for the registry or the secret -- it only proves the
    content is visible right now, not that the directory entry making it
    visible would survive a crash. Every path through this function that is
    about to return a freshly created or already-valid pair first requires
    _directory_is_durable(directory) to succeed (its own bounded retry,
    distinct from this function's _retried guard): a persistent fsync
    failure there is unavailable, exactly like any other row P6 calls
    unavailable, never a recovered result.
    """
    secret_probe, secret_bytes = state.probe_secret(state.secret_path(directory), dir_fd=dir_fd)
    registry_probe, registry = _probe_registry(directory, dir_fd=dir_fd)

    if secret_probe is state.Probe.INVALID or registry_probe is state.Probe.INVALID:
        return None, None

    def _recover_once() -> tuple[bytes | None, dict | None]:
        if _retried:
            return None, None
        return _initialize_or_probe(directory, dir_fd=dir_fd, _retried=True)

    if registry_probe is state.Probe.ABSENT and secret_probe is state.Probe.ABSENT:
        fresh = empty_registry()
        try:
            state.write_registry_bytes(
                state.registry_path(directory), _dumps(fresh), dir_fd=dir_fd
            )
        except state.PostCommitVerificationError:
            return _recover_once()
        # create_secret() itself is NOT wrapped here: a failure inside it is
        # always pre-commit (it cleans up the file it just created and
        # raises), so it must propagate normally, exactly as before this
        # fix -- only the separate, strictly post-commit directory
        # durability confirmation below can fail without undoing anything.
        new_secret = state.create_secret(state.secret_path(directory), dir_fd=dir_fd)
        if not _directory_is_durable(directory, dir_fd=dir_fd):
            return None, None
        return new_secret, fresh

    if registry_probe is state.Probe.VALID and is_empty_registry(registry) and (
        secret_probe is state.Probe.ABSENT
    ):
        new_secret = state.create_secret(state.secret_path(directory), dir_fd=dir_fd)
        if not _directory_is_durable(directory, dir_fd=dir_fd):
            return None, None
        return new_secret, registry

    if registry_probe is state.Probe.VALID and not is_empty_registry(registry) and (
        secret_probe is state.Probe.ABSENT
    ):
        return None, None  # a new secret would re-key every existing alias

    if registry_probe is state.Probe.ABSENT and secret_probe is state.Probe.VALID:
        return None, None  # a new registry could reassign an alias the secret vouches for

    # registry_probe is VALID and secret_probe is VALID: normal, but a prior
    # process's successful read-back here cannot distinguish a genuinely
    # durable commit from one whose own directory fsync failed right after
    # the file became visible (N3a fix round 2 continued, new Finding 1).
    # Require a fresh, successful directory fsync before trusting this pair
    # enough to hand out stable aliases; a persistent failure here is
    # unavailable, exactly like any invalid file.
    if not _directory_is_durable(directory, dir_fd=dir_fd):
        return None, None
    return secret_bytes, registry


def _write_if_within_bounds(directory: Path, registry: dict, *, dir_fd: int | None = None) -> bool:
    """Encode registry and write it, unless doing so would take the file
    over MAX_BYTES or MAX_ENTRIES, in which case nothing is written at all
    and this returns False (the caller treats that exactly like any other
    registration failure: fall back, leave the file untouched). May raise
    state.PostCommitVerificationError (N3a fix round 2, Finding 1): the
    write's own commit point already passed; the caller decides whether to
    recover rather than assume nothing changed. dir_fd: see state.
    lock_state_dir/LockedStateDir (N3a fix round 3, new Finding 1)."""
    if count_entries(registry) > MAX_ENTRIES:
        return False
    encoded = _dumps(registry)
    if len(encoded) > MAX_BYTES:
        return False
    state.write_registry_bytes(state.registry_path(directory), encoded, dir_fd=dir_fd)
    return True


def _finalize_registration(
    directory: Path,
    registry: dict,
    secret: bytes,
    canonical: list[tuple[str, str]],
    *,
    dir_fd: int | None = None,
) -> dict[tuple[str, str], str] | None:
    """Compute aliases for every (kind, real_id) in canonical against
    registry/secret (P3), persist the result if anything changed, and
    return the {(kind, real_id): alias} mapping -- or None if this
    registration cannot complete (a bound would be exceeded, or a
    post-commit recovery attempt itself does not confirm the write).
    dir_fd: see state.lock_state_dir/LockedStateDir (N3a fix round 3, new
    Finding 1) -- threaded into every call below.

    N3a fix round 2, Finding 1 (BLOCKER): a state.PostCommitVerificationError
    from _write_if_within_bounds means the write's own commit point
    (os.replace) already passed -- aliases.json already holds `updated`.
    Re-probe the registry fresh, under the same lock: if it reads back
    valid and byte-for-byte equal (as parsed dicts) to what was just
    written, aliases is still correct and this call succeeded; otherwise
    report failure rather than silently claim nothing changed.

    N3a fix round 2 (continued), new Finding 1 (BLOCKER): content equality
    alone is not enough either -- it only proves the registry is visible
    with the right bytes right now, not that its directory entry would
    survive a crash. Require a fresh, successful directory fsync too before
    accepting this recovery; a persistent failure there reports failure the
    same as a content mismatch.
    """
    updated, aliases, changed = _assign_all(registry, secret, canonical)
    if not changed:
        return aliases
    try:
        if not _write_if_within_bounds(directory, updated, dir_fd=dir_fd):
            return None
    except state.PostCommitVerificationError:
        reprobed_probe, reprobed = _probe_registry(directory, dir_fd=dir_fd)
        if reprobed_probe is not state.Probe.VALID or reprobed != updated:
            return None
        if not _directory_is_durable(directory, dir_fd=dir_fd):
            return None
    return aliases


# --- AliasBook and registration (P7, P8, P9) -----------------------------------


@dataclass
class AliasBook:
    """One batch's worth of stable aliases, answered from memory after one
    locked registration (P9). available is False for a fallback book:
    every alias it hands out is positional, and it never holds a real id
    anywhere in its own state (it is simply never given one -- a fallback
    book is built from the same canonical (kind, real_id) pairs as a real
    one, but only ever stores the positional name assigned to each).
    """

    available: bool
    slow_path_count: int = 0
    _directory: Path | None = field(default=None, repr=False)
    _aliases: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)
    _positional: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)
    _positional_counts: dict[str, int] = field(default_factory=dict, repr=False)
    # N3b D-i: an in-memory book (the demo) keeps its registry and secret
    # here and never touches the state directory, not even on the slow path.
    _memory: dict | None = field(default=None, repr=False)
    _secret: bytes | None = field(default=None, repr=False)

    @property
    def in_memory(self) -> bool:
        return self._memory is not None

    def alias(self, kind: str, real_id: str) -> str:
        """The alias for (kind, real_id). An identity this book was already
        registered with answers from memory, instantly. An identity it was
        not given at construction takes the slow path (docs/DESIGN.md 7.5/
        P9): one more locked registration attempt for just this id,
        counted in slow_path_count regardless of whether that attempt
        itself succeeds or falls back; a fallback book has no lock to take
        and simply extends its own positional numbering instead, without
        touching slow_path_count at all.
        """
        key = (kind, real_id)
        if key in self._aliases:
            return self._aliases[key]
        if key in self._positional:
            return self._positional[key]

        if not self.available:
            return self._assign_positional(kind, real_id)

        # N3a fix round 2, Finding 7: the slow path must apply register()'s
        # own P2 validation before doing anything else. A kind alias() does
        # not recognize, or an id that is not canonical for its kind, is
        # never eligible for a stable alias at all -- sending it on to
        # _finalize_registration would HMAC and persist a non-canonical id,
        # or index a per-kind table with a kind that was never one of them
        # (a raw KeyError). This is not a registration failure, just an
        # identity that was never going to get a stable alias, so it does
        # not take the lock and does not count against slow_path_count or
        # degrade the rest of the book (contrast Finding 8 below).
        if kind not in KINDS or not is_canonical_id(kind, real_id):
            return self._assign_positional(kind, real_id)

        if self._memory is not None:
            return self._alias_in_memory(kind, real_id)

        self.slow_path_count += 1
        try:
            with state.lock_state_dir(self._directory) as locked:
                secret, registry = _initialize_or_probe(locked.path, dir_fd=locked.dir_fd)
                if secret is None or registry is None:
                    raise DisclosureError("disclosure state became unavailable")
                aliases = _finalize_registration(
                    locked.path, registry, secret, [(kind, real_id)], dir_fd=locked.dir_fd
                )
                if aliases is None:
                    raise DisclosureError("registry bound exceeded on the slow path")
            alias = aliases[key]
            self._aliases[key] = alias
            return alias
        except Exception:
            # N3a fix round 2, Finding 3: an ordinary bug anywhere in
            # probing, assignment, or writing (not just the state/disclosure
            # errors this module itself defines) must still fall back,
            # never raise out of alias(). KeyboardInterrupt/SystemExit are
            # not ordinary Exceptions and still propagate.
            #
            # N3a fix round 2, Finding 8: a genuine slow-path failure means
            # this book can no longer vouch for any alias it already handed
            # out from memory either -- a mix of stable and positional
            # aliases within one render is exactly what P8 forbids. Degrade
            # the whole book atomically rather than falling back for only
            # the one identity that triggered the failure.
            return self._degrade_and_assign_positional(kind, real_id)

    def _alias_in_memory(self, kind: str, real_id: str) -> str:
        """The slow path of an in-memory book: one more assignment against
        the in-memory registry, counted like the locked one, never a file."""
        key = (kind, real_id)
        self.slow_path_count += 1
        try:
            assert self._memory is not None and self._secret is not None
            updated, aliases, _changed = _assign_all(self._memory, self._secret, [key])
            self._memory = updated
            alias = aliases[key]
            self._aliases[key] = alias
            return alias
        except Exception:
            return self._degrade_and_assign_positional(kind, real_id)

    def _assign_positional(self, kind: str, real_id: str) -> str:
        key = (kind, real_id)
        if key in self._positional:
            return self._positional[key]
        n = self._positional_counts.get(kind, 0) + 1
        self._positional_counts[kind] = n
        name = f"{kind}-{n}"
        self._positional[key] = name
        return name

    def _degrade_and_assign_positional(self, kind: str, real_id: str) -> str:
        """N3a fix round 2, Finding 8: flip available to False and replace
        every alias this book had already handed out from memory with a
        positional one, in the order those identities were first
        registered (self._aliases already holds them in that order, since
        register() builds it from one ordered pass over its identities
        list), before assigning the new, also positional, name. A caller
        that already printed an earlier stable alias cannot have that
        un-said -- P8's one-shot re-render rule (N3b) is what makes a
        one-shot command notice this mid-render and start over before
        anything reaches real output; `watch`'s own rule (C-1), that
        already-emitted lines stand and only the current and later events
        go positional, falls out of the same "available flips to False from
        here on" behavior for free.
        """
        self.available = False
        previously_stable = list(self._aliases.keys())
        self._aliases.clear()
        for prior_kind, prior_id in previously_stable:
            self._assign_positional(prior_kind, prior_id)
        return self._assign_positional(kind, real_id)


def _fallback_book(identities: list[tuple[str, str]]) -> AliasBook:
    book = AliasBook(available=False)
    for kind, real_id in identities:
        book._assign_positional(kind, real_id)
    return book


def fallback_book(identities: list[tuple[str, str]] | None = None) -> AliasBook:
    """A positional-only book (available=False) numbered in the order of
    `identities`, then in first-use order. N3b D-b uses it to re-render one
    output once, from the same collected identities, when a stable book
    degraded mid-render. Touches no file."""
    return _fallback_book(list(identities or []))


def lookup_agent_alias(
    alias_hex: str, ids_by_ref, *, state_dir: Path | None = None
) -> dict | None:
    """docs/DESIGN.md 4.13.6 (N6): which of the agent ids in `ids_by_ref` (a mapping from
    an opaque ref to the agent ids to test) hold exactly the registered alias
    `alias_hex`. Returns {ref: frozenset of matching ids}, only refs with a match, or
    None when stable aliases are unavailable on this machine.

    Read-only: it opens the state directory with state._open_verified_state_dir (a
    directory, mode 0700, owned by the current user, no symlink) and reads the secret
    and the registry relative to that one descriptor, closing it once before returning
    (a close OSError is unavailable too). It takes no lock, creates nothing, repairs
    nothing, writes nothing. An absent directory, a StateError, an OSError, or a probe
    result other than VALID all mean unavailable; no
    exception escapes. An id never registered has no alias and never matches; an alias
    matches only the identity whose registered alias is exactly `alias_hex` (a longer
    registered alias that merely starts with it belongs to another identity)."""
    directory = state_dir if state_dir is not None else state.state_dir()
    try:
        dir_fd = state._open_verified_state_dir(directory)
    except Exception:
        return None
    try:
        result = _lookup_with(directory, dir_fd, alias_hex, ids_by_ref)
    except Exception:
        result = None
    try:
        os.close(dir_fd)
    except OSError:
        return None
    return result


def _lookup_with(directory: Path, dir_fd: int, alias_hex: str, ids_by_ref) -> dict | None:
    """The probes and the match of lookup_agent_alias, over the one verified descriptor."""
    secret_probe, secret = state.probe_secret(state.secret_path(directory), dir_fd=dir_fd)
    if secret_probe is not state.Probe.VALID or secret is None:
        return None
    registry_probe, registry = _probe_registry(directory, dir_fd=dir_fd)
    if registry_probe is not state.Probe.VALID or registry is None:
        return None
    table = registry["agent"]
    matched: dict = {}
    for ref, agent_ids in ids_by_ref.items():
        hits = set()
        for agent_id in agent_ids:
            if not is_canonical_id("agent", agent_id):
                continue
            hashed = hmac_hex(secret, agent_id)
            length = table.get(hashed)
            if length is not None and hashed[:length] == alias_hex:
                hits.add(agent_id)
        if hits:
            matched[ref] = frozenset(hits)
    return matched


def lazy_book(*, state_dir: Path | None = None) -> AliasBook:
    """N3b C-1: an available book that has registered nothing yet. `watch`
    uses it when no agent is known at start: the first canonical identity it
    is asked for takes the slow path (one locked registration), and a
    non-canonical one is named positionally without touching any file."""
    directory = state_dir if state_dir is not None else state.state_dir()
    return AliasBook(available=True, _directory=directory)


# N3b D-i: the secret `demo` renders with. It is PUBLIC on purpose (it is in
# the source of every copy of the tool): the demo shows one synthetic run, so
# its aliases must be the same on every machine, and it must never read or
# write a real installation secret. 32 bytes, derived from a fixed phrase.
DEMO_SECRET: bytes = hashlib.sha256(b"agent-postmortem public demo secret").digest()


def register_in_memory(
    identities: list[tuple[str, str]], secret: bytes
) -> AliasBook:
    """N3b D-i: the demo's registration. P3 assignment against an empty,
    in-memory registry, the identities in the order given (one fixed order,
    which is the collision tiebreak), under `secret`. Touches no file: no
    state directory, no lock, no secret, no registry. The book's own slow
    path also stays in memory. Non-canonical ids raise ValueError, as in
    register()."""
    canonical = [(kind, require_canonical_id(kind, real_id)) for kind, real_id in identities]
    registry, aliases, _changed = _assign_all(empty_registry(), secret, canonical)
    book = AliasBook(available=True)
    book._memory = registry
    book._secret = secret
    book._aliases.update(aliases)
    return book


def register(
    identities: list[tuple[str, str]], *, state_dir: Path | None = None
) -> AliasBook:
    """One locked registration for every identity a command already knows
    it may render (docs/DESIGN.md 7.5/P9). identities is a list of
    (kind, real_id) pairs, kind one of KINDS; every one is validated as
    canonical (require_canonical_id) before the lock is even taken, so a
    caller's bug (a non-canonical id slipping through) raises ValueError
    immediately rather than silently becoming a positional name here (N3b's
    renderer is what chooses a positional name for a non-canonical id).

    Returns an AliasBook. On any failure in P4/P6/P7/P8 terms --
    unavailable secret or registry, lock contention, a failed write, a
    bound that would be exceeded -- returns a fallback book instead
    (available=False, positional names only). A pre-commit failure leaves
    the secret and the registry byte-for-byte and mtime untouched: nothing
    between taking the lock and returning ever partially writes one of the
    two files and then gives up. A post-commit failure (docs/DESIGN.md
    7.5a) is the one exception: the new file may already be installed, and
    this fallback is what register() returns only once re-probing it and
    requiring a fresh directory-durability confirmation
    have both still failed to confirm it, never by silently pretending
    nothing changed.
    """
    directory = state_dir if state_dir is not None else state.state_dir()
    canonical = [(kind, require_canonical_id(kind, real_id)) for kind, real_id in identities]

    try:
        with state.lock_state_dir(directory) as locked:
            secret, registry = _initialize_or_probe(locked.path, dir_fd=locked.dir_fd)
            if secret is None or registry is None:
                return _fallback_book(canonical)
            aliases = _finalize_registration(
                locked.path, registry, secret, canonical, dir_fd=locked.dir_fd
            )
            if aliases is None:
                return _fallback_book(canonical)
    except Exception:
        # N3a fix round 2, Finding 3: fall back on any ordinary exception
        # from probing, assignment, or writing -- not just the specific
        # state/disclosure error types this module already knew about.
        # require_canonical_id above still raises ValueError before the
        # lock is ever taken, for a caller's own bug.
        return _fallback_book(canonical)

    book = AliasBook(available=True, _directory=directory)
    book._aliases.update(aliases)
    return book
