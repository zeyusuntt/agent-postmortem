"""Read-only repo observation of paths a killed or failed attempt wrote.

docs/DESIGN.md 4.9. observe_run(run) looks at the attempts of one Run whose
outcome is 'killed', 'failed', or 'superseded' and, for every path one of
them wrote or left in flight, asks git (and only git, and only five exact
argument lists) whether that path is dirty, clean, or missing right now.

It never writes anything: no index update, no stash, no object, no config
change, no repo-controlled process. Standard library only: subprocess, os,
re, shutil, dataclasses, datetime.

This module is not called by the reader, a detector, or a hook in this
unit. It is wired into `postmortem run` only behind the `--observe` flag
(cli.py), so it is testable end to end; a guided-rescue command that calls
it directly is a later unit.

Every guard below exists because the repository being observed is not
trusted: it can configure a clean/process filter that runs an arbitrary
program on `git status`, or simply not exist, or not even be a git
repository at all. Rule of thumb for anyone touching this file: if a change
would make git run something the repo's own configuration chose, it is
wrong -- the five argument lists below are the only ones this module is
ever allowed to run, and no path derived from a repository (a filter name,
a config value) may become part of an eventual command.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import ledger
from .models import Attempt, Run

# --- public states (docs/DESIGN.md 4.9) -------------------------------------

DIRTY_NOW = "DIRTY_NOW"
CLEAN_NOW = "CLEAN_NOW"
MISSING_NOW = "MISSING_NOW"
UNKNOWN = "UNKNOWN"

# Outcomes whose paths_written/in_flight are candidates for observation
# (docs/DESIGN.md 4.9 rule 2).
_CANDIDATE_OUTCOMES = frozenset({"killed", "failed", "superseded"})

# The exact regex argument to the filter-driver guard (rule 4/5): matches any
# git config key of the form filter.<name>.clean or filter.<name>.process,
# the two hooks `git status`/`git add` can invoke.
_FILTER_CONFIG_PATTERN = r"^filter\..*\.(clean|process)$"

_GIT_VERSION_RE = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")
_MIN_GIT_VERSION = (2, 36, 0)

_STALE_AFTER = timedelta(hours=72)

_SUBPROCESS_TIMEOUT = 10

# The closed list of group-level reasons (docs/DESIGN.md 4.9, N5c-1; plan S6).
REASON_REFUSED = "refused"
REASON_HISTORICAL = "historical"
REASON_NOT_GIT = "not a git repository"
REASON_GIT_UNAVAILABLE = "git unavailable"
REASON_GUARD = "guard refused"
REASON_OUTSIDE = "outside the work tree"
GROUP_REASONS = (
    REASON_REFUSED, REASON_HISTORICAL, REASON_NOT_GIT, REASON_GIT_UNAVAILABLE, REASON_GUARD,
    REASON_OUTSIDE,
)


@dataclass
class PathObservation:
    """One candidate path's observed git state (docs/DESIGN.md 4.9).

    state is one of DIRTY_NOW / CLEAN_NOW / MISSING_NOW / UNKNOWN. reason is
    set only for a path this module refused to ask git about at all (outside
    the work tree) -- never for one it actually asked git and got an answer
    for. attempts holds, for every attempt (of the run this observation came
    from) that wrote or left this path in flight, {'agent_id', 'ordinal'},
    in the order those attempts were found.
    """

    state: str
    later_write_observed: bool
    attempts: list[dict] = field(default_factory=list)
    reason: str | None = None


@dataclass
class RepoObservation:
    """The result of observe_run() (docs/DESIGN.md 4.9).

    checked is False whenever no git process was ever started (the run is
    stale, there were no candidate paths, the cwd could not be resolved, or
    a guard refused); reason then names why. paths is keyed by the path
    relative to repo_root for a path git was actually asked about, or by the
    original candidate path string for one refused as outside the work tree
    -- the same string a renderer shows under --no-redact and pseudonymizes
    as path-N otherwise.
    """

    checked: bool
    reason: str | None = None
    repo_root: str | None = None
    head: str | None = None
    git_version: str | None = None
    paths: dict[str, PathObservation] = field(default_factory=dict)


def _not_checked(
    reason: str, *, git_version: str | None = None, repo_root: str | None = None,
    head: str | None = None,
) -> RepoObservation:
    return RepoObservation(
        checked=False, reason=reason, repo_root=repo_root, head=head,
        git_version=git_version, paths={},
    )


# --- facts plumbing (defensive: a fact can be the literal string 'UNKNOWN') --


def _facts_lists(facts) -> tuple[list, list]:
    """(paths_written, in_flight) as real lists, or ([], []) when this
    attempt has no facts or the reader could not read its transcript
    (models.FACTS_UNKNOWN is a string, never iterate it as if it were a
    list -- that would yield one candidate per character)."""
    if facts is None:
        return [], []
    written = facts.paths_written if isinstance(facts.paths_written, list) else []
    in_flight = facts.in_flight if isinstance(facts.in_flight, list) else []
    return written, in_flight


def _in_flight_paths(in_flight: list) -> list[str]:
    paths = []
    for entry in in_flight:
        if isinstance(entry, dict):
            path = entry.get("path")
            if isinstance(path, str) and path:
                paths.append(path)
    return paths


def _attempt_has_path(attempt, raw_path: str) -> bool:
    written, in_flight = _facts_lists(attempt.facts)
    if raw_path in written:
        return True
    return raw_path in _in_flight_paths(in_flight)


def _candidate_paths(run: Run) -> tuple[list[str], dict[str, list]]:
    """The union of paths_written and in_flight[].path over every attempt
    whose outcome is killed/failed/superseded (rule 2), in first-seen
    order, plus which attempt objects wrote each one (also first-seen
    order, and each attempt listed at most once per path even when it
    appears in both paths_written and in_flight)."""
    order: list[str] = []
    writers: dict[str, list] = {}
    for attempt in run.attempts:
        if attempt.outcome not in _CANDIDATE_OUTCOMES:
            continue
        written, in_flight = _facts_lists(attempt.facts)
        seen_this_attempt: set[str] = set()
        for raw_path in list(written) + _in_flight_paths(in_flight):
            if raw_path in seen_this_attempt:
                continue
            seen_this_attempt.add(raw_path)
            if raw_path not in writers:
                writers[raw_path] = []
                order.append(raw_path)
            writers[raw_path].append(attempt)
    return order, writers


def _discover_cwd(run: Run) -> str | None:
    """The most common cwd among the candidate-outcome attempts' own
    facts.cwds, ties broken by first-seen order (rule 3). max() over a list
    built in first-seen order already breaks ties this way: it only
    replaces its current pick on a strictly greater count, so the first cwd
    to reach the eventual maximum keeps it."""
    counts: dict[str, int] = {}
    order: list[str] = []
    for attempt in run.attempts:
        if attempt.outcome not in _CANDIDATE_OUTCOMES:
            continue
        facts = attempt.facts
        cwds = facts.cwds if facts is not None and isinstance(facts.cwds, list) else []
        for cwd in cwds:
            if not isinstance(cwd, str) or not cwd:
                continue
            if cwd not in counts:
                counts[cwd] = 0
                order.append(cwd)
            counts[cwd] += 1
    if not order:
        return None
    return max(order, key=lambda cwd: counts[cwd])


def _later_write_observed(run: Run, writer_attempts: list, raw_path: str) -> bool:
    """True when an attempt of the same key with a higher ordinal than one
    of writer_attempts also wrote raw_path (rule 8)."""
    for writer in writer_attempts:
        if writer.key is None:
            continue
        for other in run.attempts:
            if other.key == writer.key and other.ordinal > writer.ordinal:
                if _attempt_has_path(other, raw_path):
                    return True
    return False


# --- the exact, from-scratch environment (rule 4) ----------------------------


def _build_env() -> dict[str, str]:
    """An environment built from scratch (rule 4): PATH and HOME copied from
    the current process, a fixed set of git-behavior variables, and nothing
    else -- no GIT_DIR, no GIT_WORK_TREE, no GIT_CONFIG_*, no
    GIT_EXTERNAL_DIFF, none of which this process happens to have set, but
    none of which git should ever be handed even if it did."""
    env: dict[str, str] = {}
    path = os.environ.get("PATH")
    if path is not None:
        env["PATH"] = path
    home = os.environ.get("HOME")
    if home is not None:
        env["HOME"] = home
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    return env


def _run_git(
    git_path: str, args: list[str], env: dict[str, str],
) -> subprocess.CompletedProcess | None:
    """Run exactly `[git_path, *args]` with the restricted environment,
    never a shell, never inheriting a cwd of its own (callers pass `-C
    <dir>` instead). `git_path` is always the one absolute path
    `observe_run` resolved once with `shutil.which("git")` -- never the
    bare string "git" -- so every one of this module's five calls runs the
    exact same binary rather than each doing its own separate PATH
    resolution (docs/DESIGN.md 4.9 rule 4). Any failure to even start the
    process, or a timeout, comes back as None so the caller turns it into
    `checked=False` with its own reason rather than letting an exception
    escape observe_run."""
    try:
        return subprocess.run(
            [git_path, *args],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _parse_git_version(version_text: str | None) -> tuple[int, int, int] | None:
    if not version_text:
        return None
    match = _GIT_VERSION_RE.search(version_text)
    if not match:
        return None
    major, minor, patch = match.group(1), match.group(2), match.group(3)
    return (int(major), int(minor), int(patch) if patch is not None else 0)


def _parse_porcelain_v2(raw: str) -> set[str]:
    """The set of repo-relative paths git reports a status entry for, from
    `status --porcelain=v2 -z` output (rule 7).

    Every record is one or more NUL-separated chunks. Type '1' (ordinary,
    staged or not) and 'u' (unmerged) are one chunk each; '2'
    (rename/copy -- never produced here, --no-renames is always passed, but
    parsed correctly anyway) is two chunks, path then origPath, and only the
    new path is dirty; '?' (untracked) and '!' (ignored) are one chunk with
    no numeric fields at all. The path is taken as the remainder after the
    fixed number of space-separated fields so an embedded space in a real
    path is never split apart.
    """
    chunks = [chunk for chunk in raw.split("\0") if chunk]
    dirty: set[str] = set()
    i = 0
    n = len(chunks)
    while i < n:
        chunk = chunks[i]
        type_char = chunk[0]
        if type_char == "1":
            parts = chunk.split(" ", 8)
            dirty.add(parts[-1])
            i += 1
        elif type_char == "u":
            parts = chunk.split(" ", 10)
            dirty.add(parts[-1])
            i += 1
        elif type_char == "2":
            parts = chunk.split(" ", 9)
            dirty.add(parts[-1])
            i += 2  # skip the origPath chunk that follows
        elif type_char in ("?", "!"):
            dirty.add(chunk[2:])
            i += 1
        else:
            i += 1
    return dirty


def observe_run(run: Run, now: datetime | None = None) -> RepoObservation:
    """docs/DESIGN.md 4.9. Read-only. Starts a git process only after every
    prior guard has passed, and only ever the five argument lists rule 4
    lists -- never a subcommand a repository could steer. No exception ever
    escapes this function: every failure mode becomes checked=False with a
    reason naming the step."""
    now = now if now is not None else datetime.now(timezone.utc)

    # Rule 1: staleness. No process started at all for a run that ended
    # more than 72 hours ago -- the working tree has long since moved on.
    if run.ended_at is not None and (now - run.ended_at) > _STALE_AFTER:
        return _not_checked("historical: run older than 72 hours")

    # Rule 2: candidate paths.
    order, writers = _candidate_paths(run)
    if not order:
        return _not_checked("no paths written by a killed or failed attempt")

    # Rule 3: repo discovery.
    cwd = _discover_cwd(run)
    if cwd is None or not os.path.isdir(cwd):
        return _not_checked("cwd not found")

    # Rule 5, guard 1: git on PATH. Resolved to one absolute path here and
    # reused for every call below (docs/DESIGN.md 4.9 rule 4) -- never a
    # bare "git" that would let each of the five calls do its own,
    # independently timed PATH search.
    git_path = shutil.which("git")
    if git_path is None:
        return _not_checked("git not found on PATH")

    env = _build_env()

    # Rule 5, guard 2: git 2.36 or newer. An unparsable --version output (a
    # failed process, a timeout, unexpected text) counts as too old -- there
    # is no version we can trust it to be.
    version_result = _run_git(git_path, ["--version"], env)
    git_version = (
        version_result.stdout.strip()
        if version_result is not None and version_result.returncode == 0
        else None
    )
    parsed_version = _parse_git_version(git_version)
    if parsed_version is None or parsed_version < _MIN_GIT_VERSION:
        return _not_checked("git is older than 2.36, or its version could not be read",
                             git_version=git_version)

    # Rule 5, guard 3: this is actually a git work tree.
    toplevel_result = _run_git(git_path, ["-C", cwd, "rev-parse", "--show-toplevel"], env)
    if toplevel_result is None or toplevel_result.returncode != 0:
        return _not_checked("cwd is not inside a git repository", git_version=git_version)
    toplevel = toplevel_result.stdout.strip()
    if not toplevel:
        return _not_checked("cwd is not inside a git repository", git_version=git_version)

    head_result = _run_git(git_path, ["-C", cwd, "rev-parse", "HEAD"], env)
    if head_result is None or head_result.returncode != 0:
        return _not_checked(
            "git rev-parse HEAD failed", git_version=git_version, repo_root=toplevel,
        )
    head = head_result.stdout.strip()

    # Rule 6: containment. A path outside the work tree (after being made
    # absolute against cwd and normalized) is UNKNOWN and never reaches git.
    paths: dict[str, PathObservation] = {}
    contained: list[tuple[str, str]] = []  # (raw_path, repo-relative path)
    prefix = toplevel.rstrip(os.sep) + os.sep
    for raw_path in order:
        writer_attempts = writers[raw_path]
        later = _later_write_observed(run, writer_attempts, raw_path)
        attempts = [
            {"agent_id": attempt.agent_id, "ordinal": attempt.ordinal}
            for attempt in writer_attempts
        ]

        abs_path = raw_path if os.path.isabs(raw_path) else os.path.join(cwd, raw_path)
        norm = os.path.normpath(abs_path)
        if not norm.startswith(prefix):
            paths[raw_path] = PathObservation(
                state=UNKNOWN, later_write_observed=later, attempts=attempts,
                reason="outside the work tree",
            )
            continue
        rel_path = norm[len(prefix):]
        contained.append((raw_path, rel_path))
        paths[rel_path] = PathObservation(
            state=UNKNOWN, later_write_observed=later, attempts=attempts,
        )

    if contained:
        # Rule 5, guard 4: no clean/process filter driver configured. Exit 0
        # with output means at least one is; exit 1 (git's own "no such key"
        # code for --get-regexp) means none is, the normal case. Run only
        # when `status` is actually about to run, and as the call
        # immediately before it -- no other subprocess call in between --
        # so the window in which a repository-controlled process could
        # install a filter driver between this guard and `status` is as
        # small as this module can make it without a stronger isolation
        # primitive than the standard library offers (the remaining race is
        # documented in docs/DESIGN.md 4.9 as a known limit).
        filter_result = _run_git(
            git_path, ["-C", cwd, "config", "--get-regexp", _FILTER_CONFIG_PATTERN], env,
        )
        if filter_result is None:
            return _not_checked(
                "git config --get-regexp failed", git_version=git_version,
                repo_root=toplevel, head=head,
            )
        if filter_result.returncode == 0:
            return _not_checked(
                "a clean or process filter driver is configured for this repository",
                git_version=git_version, repo_root=toplevel, head=head,
            )
        if filter_result.returncode != 1:
            return _not_checked(
                "git config --get-regexp failed", git_version=git_version,
                repo_root=toplevel, head=head,
            )

        rel_paths = [rel for _, rel in contained]
        status_result = _run_git(
            git_path,
            [
                "-C", cwd, "--no-optional-locks", "-c", "core.fsmonitor=false",
                "status", "--porcelain=v2", "-z", "--no-renames",
                "--ignore-submodules=all", "--", *rel_paths,
            ],
            env,
        )
        if status_result is None or status_result.returncode != 0:
            return _not_checked(
                "git status failed", git_version=git_version, repo_root=toplevel, head=head,
            )
        dirty = _parse_porcelain_v2(status_result.stdout)
        for _raw_path, rel_path in contained:
            observation = paths[rel_path]
            if rel_path in dirty:
                observation.state = DIRTY_NOW
            else:
                exists = os.path.exists(os.path.join(toplevel, rel_path))
                observation.state = CLEAN_NOW if exists else MISSING_NOW

    return RepoObservation(
        checked=True, reason=None, repo_root=toplevel, head=head,
        git_version=git_version, paths=paths,
    )


# =============================================================================
# Per-repository observation (docs/DESIGN.md 4.9, N5c-1; plan S5 and S6)
# =============================================================================

_PATH_EFFECTS = frozenset({"observed", "claimed_target", "attempted"})


@dataclass
class GroupObservation:
    """One repository group of a run (docs/DESIGN.md 4.9, N5c-1). `repo` is the
    candidate repository (None for the unknown group); `refused` is true for a
    group a partial ledger contributes to and for the unknown group; `checked`
    is true only when `git status` actually ran. `reason` is one of GROUP_REASONS
    for a group that was not checked, else None. `repo_root`, `head`, and
    `git_version` are internal (never exported). `paths` holds a PathObservation
    per observed path (the absolute ledger path), only for a checked group;
    `unobserved_paths` is every ledger path no process looked at. `attempts` is
    the internal list of attempt references (`main`, `attempts[<i>]`)."""

    repo: str | None
    refused: bool
    checked: bool
    reason: str | None
    repo_root: str | None = None
    head: str | None = None
    git_version: str | None = None
    paths: dict[str, PathObservation] = field(default_factory=dict)
    unobserved_paths: list[str] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)


@dataclass
class RunObservation:
    """The result of observe_groups(): the ordered group list, named
    repositories in sorted candidate order, then the unknown group."""

    groups: list[GroupObservation] = field(default_factory=list)

    @property
    def checked(self) -> bool:
        return any(group.checked for group in self.groups)

    @property
    def repo_root(self) -> str | None:
        """The first checked group's toplevel: the one repository root the
        disclosure context knows at `local` (docs/DESIGN.md 4.13.5, D-n)."""
        for group in self.groups:
            if group.checked and group.repo_root:
                return group.repo_root
        return None


def _entry_paths(attempt: Attempt) -> set[str]:
    built = attempt.ledger
    if built is None:
        return set()
    return {
        entry.path for entry in built.entries
        if entry.path is not None and entry.effect in _PATH_EFFECTS
    }


def _later_write(run: Run, writers: list[Attempt], path: str) -> bool:
    """True when an attempt of the same key with a higher ordinal than one of the
    path's writers also recorded the path (rule 8, on the ledger)."""
    for writer in writers:
        if writer.key is None:
            continue
        for other in run.attempts:
            if (
                other.key == writer.key and other.ordinal > writer.ordinal
                and path in _entry_paths(other)
            ):
                return True
    return False


def _path_observation(
    run: Run, members: list[Attempt], path: str, state: str,
) -> PathObservation:
    writers = [attempt for attempt in members if path in _entry_paths(attempt)]
    attempts = [
        {
            "agent_id": "main" if attempt is run.main else attempt.agent_id,
            "ordinal": attempt.ordinal,
        }
        for attempt in writers
    ]
    return PathObservation(
        state=state, later_write_observed=_later_write(run, writers, path), attempts=attempts,
    )


class _GitSession:
    """The one absolute git path, the scrubbed environment, and the version
    guard, resolved lazily and once for a whole run (docs/DESIGN.md 4.9 rules 4
    and 5). `ready()` is None when git can be used, else the closed reason."""

    def __init__(self) -> None:
        self._resolved = False
        self._reason: str | None = None
        self.git_path: str | None = None
        self.env: dict[str, str] = {}
        self.version: str | None = None

    def ready(self) -> str | None:
        if self._resolved:
            return self._reason
        self._resolved = True
        git_path = shutil.which("git")
        if git_path is None:
            self._reason = REASON_GIT_UNAVAILABLE
            return self._reason
        self.git_path = git_path
        self.env = _build_env()
        result = _run_git(git_path, ["--version"], self.env)
        self.version = (
            result.stdout.strip() if result is not None and result.returncode == 0 else None
        )
        parsed = _parse_git_version(self.version)
        if parsed is None or parsed < _MIN_GIT_VERSION:
            self._reason = REASON_GIT_UNAVAILABLE
        return self._reason


def _unchecked(
    repo: str | None, group, reason: str, *, refused: bool, session: "_GitSession | None" = None,
    repo_root: str | None = None, head: str | None = None,
) -> GroupObservation:
    return GroupObservation(
        repo=repo, refused=refused, checked=False, reason=reason, repo_root=repo_root,
        head=head, git_version=session.version if session is not None else None,
        paths={}, unobserved_paths=sorted(group.paths), attempts=list(group.attempts),
    )


def _observe_named_group(
    run: Run, repo: str, group, members: list[Attempt], session: _GitSession, stale: bool,
) -> GroupObservation:
    """The guarded 4.9 sequence for one named repository group that is not
    refused. Every failure is an unchecked group with a closed reason; nothing
    raises."""
    if stale:
        return _unchecked(repo, group, REASON_HISTORICAL, refused=False)
    if not os.path.isdir(repo):
        return _unchecked(repo, group, REASON_NOT_GIT, refused=False)
    if session.ready() is not None:
        return _unchecked(repo, group, REASON_GIT_UNAVAILABLE, refused=False)
    git_path, env = session.git_path, session.env
    assert git_path is not None
    toplevel_result = _run_git(git_path, ["-C", repo, "rev-parse", "--show-toplevel"], env)
    if toplevel_result is None or toplevel_result.returncode != 0:
        return _unchecked(repo, group, REASON_NOT_GIT, refused=False, session=session)
    toplevel = toplevel_result.stdout.strip()
    if not toplevel:
        return _unchecked(repo, group, REASON_NOT_GIT, refused=False, session=session)
    head_result = _run_git(git_path, ["-C", repo, "rev-parse", "HEAD"], env)
    if head_result is None or head_result.returncode != 0:
        return _unchecked(
            repo, group, REASON_GUARD, refused=False, session=session, repo_root=toplevel,
        )
    head = head_result.stdout.strip()

    # Rule 6, containment: only a path inside the toplevel ever reaches git.
    prefix = toplevel.rstrip(os.sep) + os.sep
    contained: list[tuple[str, str]] = []
    outside: list[str] = []
    for path in sorted(group.paths):
        norm = os.path.normpath(path)
        if norm.startswith(prefix):
            contained.append((path, norm[len(prefix):]))
        else:
            outside.append(path)
    if not contained:
        return _unchecked(
            repo, group, REASON_OUTSIDE, refused=False, session=session,
            repo_root=toplevel, head=head,
        )

    # The filter-driver guard is the call immediately before `status`.
    filter_result = _run_git(
        git_path, ["-C", repo, "config", "--get-regexp", _FILTER_CONFIG_PATTERN], env,
    )
    if filter_result is None or filter_result.returncode != 1:
        return _unchecked(
            repo, group, REASON_GUARD, refused=False, session=session,
            repo_root=toplevel, head=head,
        )
    status_result = _run_git(
        git_path,
        [
            "-C", repo, "--no-optional-locks", "-c", "core.fsmonitor=false", "status",
            "--porcelain=v2", "-z", "--no-renames", "--ignore-submodules=all", "--",
            *[rel for _path, rel in contained],
        ],
        env,
    )
    if status_result is None or status_result.returncode != 0:
        return _unchecked(
            repo, group, REASON_GUARD, refused=False, session=session,
            repo_root=toplevel, head=head,
        )
    dirty = _parse_porcelain_v2(status_result.stdout)
    paths: dict[str, PathObservation] = {}
    for path, rel in contained:
        if rel in dirty:
            state = DIRTY_NOW
        else:
            state = CLEAN_NOW if os.path.exists(os.path.join(toplevel, rel)) else MISSING_NOW
        paths[path] = _path_observation(run, members, path, state)
    return GroupObservation(
        repo=repo, refused=False, checked=True, reason=None, repo_root=toplevel, head=head,
        git_version=session.version, paths=paths, unobserved_paths=outside,
        attempts=list(group.attempts),
    )


def observe_groups(
    run: Run, now: datetime | None = None, groups: "ledger.RepoGroups | None" = None,
) -> RunObservation:
    """docs/DESIGN.md 4.9 (N5c-1), plan S5 and S6. Read-only. The repository
    groups are partitioned before any process starts (`ledger.group_by_repo`, a
    pure function); a refused group and the unknown group start no process at
    all; a group that is not refused runs the guarded sequence with only its own
    ledger paths. No exception escapes: every failure is an unchecked group with
    a reason from GROUP_REASONS."""
    now = now if now is not None else datetime.now(timezone.utc)
    grouped = groups if groups is not None else ledger.group_by_repo(run)
    ended = run.ended_at
    if ended is not None and ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    stale = ended is not None and (now - ended) > _STALE_AFTER

    by_ref: dict[str, Attempt] = {}
    for attempt in ([run.main] if run.main is not None else []) + list(run.attempts):
        by_ref[ledger.attempt_refs(run)[id(attempt)]] = attempt
    session = _GitSession()

    result: list[GroupObservation] = []
    for repo, group in grouped.repos.items():
        if group.refused:
            result.append(_unchecked(repo, group, REASON_REFUSED, refused=True))
            continue
        members = [by_ref[ref] for ref in group.attempts if ref in by_ref]
        result.append(_observe_named_group(run, repo, group, members, session, stale))
    unknown = grouped.unknown
    if unknown.attempts or unknown.paths:
        result.append(_unchecked(None, unknown, REASON_REFUSED, refused=True))
    return RunObservation(result)
