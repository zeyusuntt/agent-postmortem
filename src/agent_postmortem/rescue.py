"""Guided rescue: `rescue plan` (a decision packet) and `rescue status`
(what was observed), docs/DESIGN.md 4.11, unit M4.

Both commands are read-only and stateless. Neither writes a file, runs a
check, or touches git beyond what `observe.py` already does. The main
Claude session or the human performs every action (`.reviews/proposals/
2026-09-v0.2-guided-rescue.md` section 3): this module only reads
transcripts, observes repo state through `observe.observe_groups`, and prints
facts. Per R8, it never claims to have confirmed something by running it --
it reports what it observed and names what it could not.

Two phases per command: a builder (`build_plan`/`build_status`) that
produces a plain dataclass tree of RAW, unredacted values, and a renderer
(text or JSON) that applies the active Redactor at the very end -- the same
split `report.py` uses (`_story_records` then both text and JSON consume
it), so text and JSON output can never drift apart on the facts, only on
formatting.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import commands, ledger, observe, reader, report
from . import config as config_module
from .models import FACTS_UNKNOWN, Attempt, Finding, LedgerEntry, LogicalAgent, Run
from .redact import Redactor

SCHEMA_VERSION = 3

# docs/DESIGN.md 4.9: the same outcomes observe.py's own candidate-path rule
# uses -- a unit of work is one whose attempts include at least one of these.
_RISK_OUTCOMES = frozenset({"killed", "failed", "superseded"})

# docs/DESIGN.md 4.11: the NEVER list -- a rescue must never undo with any of
# these, and `rescue status` flags one found in the transcript after the run
# ended.
_NEVER_SUBCOMMANDS = ("checkout", "restore", "reset", "stash", "clean")
_NEVER_LIST_RE = re.compile(r"\bgit\s+(" + "|".join(_NEVER_SUBCOMMANDS) + r")\b")

# docs/DESIGN.md 4.11 (F3 hardening): whether a Bash command run after the
# last counted check is PROVEN read-only, for rescue's own "did this session
# possibly write to the repository" question -- deliberately its own,
# narrower heuristic from reader._is_read_only_bash_command (docs/DESIGN.md
# 4.3), which answers a different question (can this tool_result's own text
# be trusted as a faithful quote of something already on disk) and, for that
# reason, treats `git status` as NOT read-only (it can trigger an index
# refresh) -- see tests/test_reader.py::test_bash_git_status_is_not_read_
# only_output, a deliberately pinned, unrelated distinction this function
# must not disturb. Here the question is only "did it write": `git status`
# never does, and neither does a `sed` invocation without an in-place flag;
# everything else follows the same shape reader._is_read_only_bash_command
# already uses (a fixed read command list, a redirection check, an
# interpreter/test-runner denylist).
_RESCUE_READ_ONLY_RE = re.compile(
    r"\b(?:cat|sed|head|tail|grep|rg|nl|less|more|bat|wc|diff|ls|find)\b"
    r"|\bgit\s+(?:show|diff|log|grep|blame|status)\b"
)
_RESCUE_NOT_READ_ONLY_RE = re.compile(
    r"\b(?:node|codex|python|pytest|npm|npx|curl|make|tee)\b|\buv\s+run\b"
)
_SED_INPLACE_RE = re.compile(r"\bsed\b[^\n]*(?:-i\b|--in-place\b)")


def _proven_read_only(command: str | None) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    if ">" in command:
        return False
    if _SED_INPLACE_RE.search(command):
        return False
    if _RESCUE_NOT_READ_ONLY_RE.search(command):
        return False
    return bool(_RESCUE_READ_ONLY_RE.search(command))


# A timestamp floor for sorting tool calls that carry no `ts` at all (a
# missing timestamp) alongside ones that do, so a None never crashes the
# sort and always sorts first (oldest), never last.
_MIN_TS = datetime.min.replace(tzinfo=timezone.utc)


# --- shared data model -------------------------------------------------------


@dataclass
class PathRow:
    raw_path: str
    display_key: str  # repo-relative when resolvable, else raw_path
    state: str  # observe.DIRTY_NOW / CLEAN_NOW / MISSING_NOW / UNKNOWN
    state_reason: str | None
    attempt_ordinals: list[int]
    later_write: bool
    # docs/DESIGN.md 5.3b, 4.13.11: one (attempt ordinal, ledger entry) pair per
    # ledger entry that names this path, in attempt order, nothing collapsed.
    ledger: list[tuple[int, LedgerEntry]] = field(default_factory=list)
    # docs/DESIGN.md 5.3b (N5c-1): the index of the observation group that lists
    # this path (None when none does), the path's own observation (None when its
    # group was refused, unknown, or not checked, or the path lay outside the
    # toplevel), and whether the path still needs a person to look at it because
    # its group was never checked.
    group: int | None = None
    observation: "observe.PathObservation | None" = None
    unchecked: bool = False
    # docs/DESIGN.md 4.11 (N5 fix 1, C3): the normalized absolute target the row is keyed by
    # (with `group`); None for a hand-built row.
    target: str | None = None


@dataclass
class InFlightRow:
    tool: str | None
    raw_path: str | None
    display_key: str | None


@dataclass
class TaskInfo:
    transcript_name: str
    line_no: int
    text: str  # full, untruncated, harness-noise-stripped
    length: int  # true length of `text`, not merely len(excerpt)


@dataclass
class UnitPlan:
    key: str
    label: str
    phase: str
    attempts_count: int
    last_end_word: str
    task: TaskInfo | None
    reported: dict | None  # the LAST attempt's own Attempt.reported
    paths: list[PathRow]
    commits: list[str]
    in_flight: list[InFlightRow]
    agent_ids: list[str] = field(default_factory=list)
    # docs/DESIGN.md 4.11 (F6 hardening): true when at least one of this
    # unit's own killed/failed/superseded attempts has facts is None (no
    # transcript at all) -- distinct from the checked/reason UNKNOWN line
    # below, which is about the repo observation, not the transcript.
    facts_unknown: bool = False
    # docs/DESIGN.md 4.11 (N5c-2, plan S7): the declared `[[repos]]` roots that own
    # this unit's paths (lexically, in order of first appearance) and their declared
    # checks, in that order. Never taken from an observation.
    repos: list[str] = field(default_factory=list)
    declared_checks: list[str] = field(default_factory=list)
    # docs/DESIGN.md 4.11 (N5 fix 1, S1): see `_unit_unknown_group`.
    unknown_group: bool = False


@dataclass
class RescuePlan:
    run_id: str
    kind: str
    finished: bool
    possibly_live: bool
    nothing_to_rescue: bool
    # docs/DESIGN.md 4.11 (F6 hardening): meaningful only when
    # nothing_to_rescue is True. True is the cheerful "every attempt
    # completed" case (run.finished and every attempt's outcome is
    # 'completed'); False is "NOTHING TO RESCUE YET" -- an unfinished run,
    # or one with an attempt that is neither completed nor killed/failed/
    # superseded (for example 'unknown'), so the claim "every attempt
    # completed" would not be true.
    nothing_to_rescue_ok: bool
    units: list[UnitPlan]
    checked: bool
    observation_reason: str | None
    repo_root: str | None
    config_warnings: list[str]
    killed_brief: list[dict]
    failed_brief: list[dict]
    already_exists_paths: list[str]
    already_exists_commits: list[str]
    has_unknown_fact: bool
    exit_code: int = 0
    # N8 fix 1, S2: with no unit, the size of the risk population when it is not empty or not
    # complete on a finished all-completed run (the sentence names it); None otherwise.
    unchecked_attempts: int | None = None
    # N3b P19: display string -> the raw spelling it came from, so local can
    # place a repo-relative key by its original path.
    already_exists_sources: dict[str, str] = field(default_factory=dict)
    # docs/DESIGN.md 4.9, 5.3b (N5c-1): the run's observation, whose groups the
    # JSON export lists and whose indexes the path rows name.
    observation: "observe.RunObservation | None" = None
    # docs/DESIGN.md 4.11 (N8b, K4.2): true only for a workflow run in which two attempts of
    # different keys overlapped in time; the relaunch brief's fan-out caveat prints only then.
    fan_out_overlap: bool = False


# --- building the packet ------------------------------------------------------


def _facts_lists(facts) -> tuple[list, list]:
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


def _unit_risk_attempts(unit: LogicalAgent) -> list[Attempt]:
    return [a for a in unit.attempts if a.outcome in _RISK_OUTCOMES]


def qualifying_units(run: Run) -> list[LogicalAgent]:
    """docs/DESIGN.md 4.11: one unit of work per logical agent whose
    attempts include a killed, failed, or superseded one."""
    return [unit for unit in run.agents if _unit_risk_attempts(unit)]


@dataclass
class _Contribution:
    """One way a unit's killed, failed, or superseded attempt names a path (N5 fix 1, C3 and
    S1): from its facts (a file tool's `paths_written` or `in_flight`, with the spelling the call
    used) or from a ledger entry (any tool the ledger gives a path). `target` is the normalized
    absolute path (the raw spelling itself when nothing places a relative one); `repo` is the
    call's candidate repository, the group key the ledger gives it."""

    attempt: Attempt
    target: str
    raw: str | None
    repo: str | None
    entry: LedgerEntry | None = None


_PATH_EFFECTS = ("observed", "claimed_target", "attempted")


def _unit_contributions(unit: LogicalAgent) -> list[_Contribution]:
    """Every path contribution of the unit's risk attempts: the facts paths of all attempts
    first (the same union `observe.py`'s own rule 2 computes, scoped to this one unit), then
    every ledger entry path (so a MultiEdit or a path named in a shell command has a row)."""
    found: list[_Contribution] = []
    attempts = _unit_risk_attempts(unit)
    for attempt in attempts:
        written, in_flight = _facts_lists(attempt.facts)
        seen: set[str] = set()
        for raw in list(written) + _in_flight_paths(in_flight):
            if raw in seen:
                continue
            seen.add(raw)
            placed = False
            for call in attempt.tool_calls:
                if call.path != raw:
                    continue
                cwd = call.cwd if isinstance(call.cwd, str) and call.cwd.startswith("/") else None
                base = commands.normalize_path(cwd) if cwd is not None else None
                resolved = commands.resolve_target(base, raw)
                if resolved is not None and resolved[0] is not None:
                    found.append(_Contribution(attempt, resolved[0], raw, base))
                    placed = True
            if not placed:
                target = commands.normalize_path(raw) if raw.startswith("/") else raw
                found.append(_Contribution(attempt, target, raw, None))
    for attempt in sorted(attempts, key=lambda item: item.ordinal):
        if attempt.ledger is None:
            continue
        for entry in attempt.ledger.entries:
            if entry.path is not None and entry.effect in _PATH_EFFECTS:
                found.append(_Contribution(attempt, entry.path, None, entry.repo, entry))
    return found


def _unit_targets(unit: LogicalAgent) -> dict[str, list[_Contribution]]:
    """target -> its contributions, targets in first-seen order (facts paths first)."""
    targets: dict[str, list[_Contribution]] = {}
    for contribution in _unit_contributions(unit):
        targets.setdefault(contribution.target, []).append(contribution)
    return targets


def _listed_spellings(unit: LogicalAgent) -> list[str]:
    """The spellings the status text may need to pseudonymize, first seen first: the raw
    spelling a facts path used, else the target of a ledger-only path."""
    listed: list[str] = []
    for contribution in _unit_contributions(unit):
        spelling = contribution.raw if contribution.raw is not None else contribution.target
        if spelling not in listed:
            listed.append(spelling)
    return listed


@dataclass
class _RowSpec:
    """One future path row: a target, the observation group that lists it (None when no group
    does), and the contributions that belong to that group."""

    target: str
    index: int | None
    group: "observe.GroupObservation | None"
    contributions: list[_Contribution]


def _row_specs(unit: LogicalAgent, observation) -> list[_RowSpec]:
    """Rows keyed by (normalized absolute target, group), never by the raw spelling (C3). A
    contribution goes to the listing group whose candidate repository it names, else to the
    first listing group; a group that lists the target but holds none of this unit's
    contributions gets no row."""
    specs: list[_RowSpec] = []
    for target, contributions in _unit_targets(unit).items():
        listing = [
            (index, group) for index, group in enumerate(observation.groups)
            if target in group.paths or target in group.unobserved_paths
        ]
        if not listing:
            specs.append(_RowSpec(target, None, None, contributions))
            continue
        assigned: dict[int, list[_Contribution]] = {index: [] for index, _group in listing}
        for contribution in contributions:
            home = next(
                (index for index, group in listing if group.repo == contribution.repo),
                listing[0][0],
            )
            assigned[home].append(contribution)
        for index, group in listing:
            if assigned[index]:
                specs.append(_RowSpec(target, index, group, assigned[index]))
    return specs


def _display_key(raw_path: str, cwd: str | None, repo_root: str | None) -> str:
    """The repo-relative form of raw_path, the same normalization
    observe.py's own containment rule (4.9 rule 6) applies -- a pure string
    operation, no filesystem access. Falls back to raw_path itself when
    repo_root or cwd is unknown, or when the path does not resolve inside
    repo_root (observe.py would have recorded it as UNKNOWN too)."""
    if not repo_root:
        return raw_path
    abs_path = raw_path if os.path.isabs(raw_path) else os.path.join(cwd or "", raw_path)
    norm = os.path.normpath(abs_path)
    prefix = repo_root.rstrip(os.sep) + os.sep
    if norm.startswith(prefix):
        return norm[len(prefix):]
    return raw_path


# --- declared repositories (docs/DESIGN.md 4.11, N5c-2, plan S7) -----------------------


def declared_roots(cfg: config_module.Config) -> dict[str, list[str]]:
    """The `[[repos]]` roots of the configuration, lexically normalized, never resolved
    through git, each with its declared checks (the first table wins for a duplicated
    root, which `duplicate_roots` reports as a configuration error)."""
    roots: dict[str, list[str]] = {}
    for repo in cfg.repos:
        roots.setdefault(commands.normalize_path(repo.path), list(repo.checks))
    return roots


def duplicate_roots(cfg: config_module.Config) -> list[str]:
    """Normalized roots that two or more `[[repos]]` tables name, in order of first repeat."""
    seen: set[str] = set()
    repeated: list[str] = []
    for repo in cfg.repos:
        root = commands.normalize_path(repo.path)
        if root in seen and root not in repeated:
            repeated.append(root)
        seen.add(root)
    return repeated


def _owner(path: str | None, roots) -> str | None:
    """The longest declared root that contains `path` component-wise (`/work/repo2` is not
    inside `/work/repo`), or None. Pure string logic; both must be absolute."""
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    best: str | None = None
    for root in roots:
        if not root.startswith("/"):
            continue
        try:
            inside = os.path.commonpath([root, path]) == root
        except ValueError:
            inside = False
        if inside and (best is None or len(root) > len(best)):
            best = root
    return best


def _target_owners(specs: list[_RowSpec], roots) -> dict[str, list[str]]:
    """target -> the declared roots that own it: the owner of the target itself, and when it
    has none, the owner of the candidate repository of each group that lists it (a candidate
    repository belongs to the longest declared root too)."""
    owners: dict[str, list[str]] = {}
    for spec in specs:
        found = owners.setdefault(spec.target, [])
        root = _owner(spec.target, roots)
        if root is None and spec.group is not None and spec.group.repo is not None:
            root = _owner(commands.normalize_path(spec.group.repo), roots)
        if root is not None and root not in found:
            found.append(root)
    return owners


def _unit_repos(owners: dict[str, list[str]]) -> list[str]:
    repos: list[str] = []
    for roots_of_target in owners.values():
        for root in roots_of_target:
            if root not in repos:
                repos.append(root)
    return repos


def _path_ledger(unit: LogicalAgent, raw_path: str) -> list[tuple[int, LedgerEntry]]:
    """The ledger entries of the unit's killed, failed, or superseded attempts
    that name `raw_path`, as (ordinal, entry) in attempt order. The facts keep
    the path as the file tool spelled it; the ledger keeps it normalized
    against the call's working directory (4.13.1), so each call that wrote
    `raw_path` contributes its own normalized spelling to match on."""
    pairs: list[tuple[int, LedgerEntry]] = []
    for attempt in sorted(_unit_risk_attempts(unit), key=lambda item: item.ordinal):
        if attempt.ledger is None:
            continue
        spellings: set[str] = set()
        for call in attempt.tool_calls:
            if call.path != raw_path:
                continue
            cwd = call.cwd if isinstance(call.cwd, str) and call.cwd.startswith("/") else None
            base = commands.normalize_path(cwd) if cwd is not None else None
            resolved = commands.resolve_target(base, raw_path)
            if resolved is not None and resolved[0] is not None:
                spellings.add(resolved[0])
        if not spellings:
            continue
        pairs.extend(
            (attempt.ordinal, entry) for entry in attempt.ledger.entries
            if entry.path in spellings
        )
    return pairs


def _unit_path_rows(
    run: Run, unit: LogicalAgent, observation, global_cwd: str | None,
) -> list[PathRow]:
    rows: list[PathRow] = []
    for spec in _row_specs(unit, observation):
        writer_attempts: list[Attempt] = []
        for contribution in spec.contributions:
            if not any(contribution.attempt is seen for seen in writer_attempts):
                writer_attempts.append(contribution.attempt)
        ordinals = sorted({attempt.ordinal for attempt in writer_attempts})
        raws = [c.raw for c in spec.contributions if c.raw is not None]
        raw_path = raws[0] if raws else spec.target
        later = observe._later_write(run, writer_attempts, spec.target) or any(
            observe._later_write_observed(run, writer_attempts, raw) for raw in raws
        )
        path_obs = None
        unchecked = False
        display = raw_path
        if spec.group is None:
            state, reason = observe.UNKNOWN, "not found in the observation"
        else:
            group = spec.group
            path_obs = group.paths.get(spec.target)
            if path_obs is not None:
                state, reason = path_obs.state, path_obs.reason
                if group.repo_root:
                    display = spec.target[len(group.repo_root.rstrip(os.sep)) + 1:]
            else:
                state = observe.UNKNOWN
                reason = group.reason if not group.checked else observe.REASON_OUTSIDE
                unchecked = not group.checked
        ledger_pairs = sorted(
            ((c.attempt.ordinal, c.entry) for c in spec.contributions if c.entry is not None),
            key=lambda pair: pair[0],
        )
        rows.append(PathRow(
            raw_path=raw_path, display_key=display, state=state, state_reason=reason,
            attempt_ordinals=ordinals, later_write=later, ledger=ledger_pairs,
            group=spec.index, observation=path_obs, unchecked=unchecked, target=spec.target,
        ))
    return rows


def _unit_in_flight_rows(unit: LogicalAgent, global_cwd, repo_root, checked) -> list[InFlightRow]:
    rows: list[InFlightRow] = []
    for attempt in _unit_risk_attempts(unit):
        _written, in_flight = _facts_lists(attempt.facts)
        for entry in in_flight:
            if not isinstance(entry, dict):
                continue
            tool = entry.get("tool")
            raw_path = entry.get("path")
            raw_path = raw_path if isinstance(raw_path, str) and raw_path else None
            display = None
            if raw_path is not None:
                display = _display_key(raw_path, global_cwd, repo_root) if checked else raw_path
            rows.append(InFlightRow(
                tool=tool if isinstance(tool, str) else None, raw_path=raw_path,
                display_key=display,
            ))
    return rows


def _unit_commits(unit: LogicalAgent) -> list[str]:
    seen: list[str] = []
    for attempt in _unit_risk_attempts(unit):
        facts = attempt.facts
        commits = facts.commits if facts is not None and isinstance(facts.commits, list) else []
        for commit_id in commits:
            if commit_id not in seen:
                seen.append(commit_id)
    return seen


def _unit_has_unknown_facts(unit: LogicalAgent) -> bool:
    """docs/DESIGN.md 4.11 (F6 hardening): a risk attempt (killed/failed/
    superseded) with facts is None -- no transcript at all -- counts as
    UNKNOWN too, not only one whose transcript exists but could not be read
    (FACTS_UNKNOWN). The old `continue` here silently dropped the None case
    from the UNKNOWN accounting, so a unit whose only risk attempt had no
    transcript could pass as fully known."""
    for attempt in _unit_risk_attempts(unit):
        facts = attempt.facts
        if facts is None:
            return True
        for value in (
            facts.paths_written, facts.in_flight, facts.commits, facts.cwds,
            facts.background_unfinished, facts.last_tool,
            facts.seconds_since_last_tool_at_end,
        ):
            if value == FACTS_UNKNOWN:
                return True
    return False


def _first_user_event(attempt: Attempt):
    for event in attempt.events:
        if event.kind == "user":
            return event
    return None


def _read_raw_line_text(path: Path, line_no: int) -> str | None:
    """The full, untruncated user-message text on line_no of path, flattened
    the same way reader._user_events does, then harness-noise-stripped the
    same way reader._excerpt does -- everything but the 400-char bound,
    since this is used only to report a true prompt length and to quote the
    first 300 characters, both under the caller's own control. Read-only;
    returns None on any parse trouble rather than raising."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            for current, line in enumerate(handle, start=1):
                if current != line_no:
                    continue
                line = line.strip()
                if not line:
                    return None
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    return None
                if not isinstance(entry, dict):
                    return None
                message = entry.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str):
                    text = content
                else:
                    text = reader._flatten_block_text(content)
                return reader._strip_harness_noise(text)
    except OSError:
        return None
    return None


def _unit_task(unit: LogicalAgent) -> TaskInfo | None:
    first_attempt = min(unit.attempts, key=lambda attempt: attempt.ordinal)
    if first_attempt.transcript_path is None:
        return None
    event = _first_user_event(first_attempt)
    if event is None:
        return None
    text = _read_raw_line_text(first_attempt.transcript_path, event.line_no)
    if text is None:
        text = event.text  # already truncated/stripped fallback
    return TaskInfo(
        transcript_name=first_attempt.transcript_path.name, line_no=event.line_no,
        text=text, length=len(text),
    )


def _unit_unknown_group(run: Run, unit: LogicalAgent, observation) -> bool:
    """docs/DESIGN.md 4.11 (N5 fix 1, S1): one of the unit's risk attempts sits in a refused
    or unknown group, or in a group with unobserved paths, so what the unit wrote is not fully
    seen (a shell write, an attempt with no recorded path)."""
    refs = ledger.attempt_refs(run)
    mine = {refs[id(attempt)] for attempt in _unit_risk_attempts(unit) if id(attempt) in refs}
    return any(
        (group.refused or group.unobserved_paths) and mine.intersection(group.attempts)
        for group in observation.groups
    )


def _unit_plan(
    run: Run, unit: LogicalAgent, observation, global_cwd: str | None,
    roots: dict[str, list[str]],
) -> UnitPlan:
    attempts_sorted = sorted(unit.attempts, key=lambda attempt: attempt.ordinal)
    last_attempt = attempts_sorted[-1]
    repos = _unit_repos(_target_owners(_row_specs(unit, observation), roots))
    declared_checks: list[str] = []
    for root in repos:
        for check in roots[root]:
            if check not in declared_checks:
                declared_checks.append(check)
    return UnitPlan(
        key=unit.key,
        label=unit.label,
        phase=unit.phase,
        attempts_count=len(attempts_sorted),
        last_end_word=_end_word(last_attempt),
        task=_unit_task(unit),
        reported=last_attempt.reported,
        paths=_unit_path_rows(run, unit, observation, global_cwd),
        commits=_unit_commits(unit),
        in_flight=_unit_in_flight_rows(
            unit, global_cwd, observation.repo_root, observation.checked,
        ),
        agent_ids=[attempt.agent_id for attempt in unit.attempts],
        facts_unknown=any(a.facts is None for a in _unit_risk_attempts(unit)),
        repos=repos,
        declared_checks=declared_checks,
        unknown_group=_unit_unknown_group(run, unit, observation),
    )


_END_KILLED_CAUSE = {"parent_stop": "stopped by parent", "human": "interrupted"}


def _end_word(attempt: Attempt) -> str:
    if attempt.outcome == "killed":
        cause = attempt.interrupt.cause if attempt.interrupt is not None else None
        return _END_KILLED_CAUSE.get(cause, "killed")
    return attempt.outcome


def _killed_brief_rows(run: Run) -> list[dict]:
    rows = []
    for attempt in run.attempts:
        if attempt.outcome != "killed" or attempt.interrupt is None:
            continue
        kill_ts = attempt.interrupt.kill_ts
        time_str = kill_ts.astimezone(timezone.utc).strftime("%H:%MZ") if kill_ts else None
        rows.append({
            "label": attempt.label, "ordinal": attempt.ordinal, "time": time_str,
            "silence_seconds": attempt.interrupt.silence_seconds,
        })
    return rows


def _failed_brief_rows(run: Run, findings: list[Finding]) -> list[dict]:
    label_by_agent_id = {attempt.agent_id: attempt.label for attempt in run.attempts}
    rows = []
    for finding in findings:
        if finding.detector != "attempt_failed":
            continue
        label = label_by_agent_id.get(finding.agent_id, finding.agent_id or "?")
        rows.append({
            "label": label,
            "cause": finding.metrics.get("cause", "unattributed"),
            "retry_now": finding.metrics.get("retry_now"),
            "reset_time": finding.metrics.get("reset_time"),
        })
    return rows


_CONFIRMED_WRITE_EFFECTS = frozenset({"observed"})


def _already_exists(run: Run) -> tuple[list[str], list[str]]:
    """The brief's `already exists` (docs/DESIGN.md 4.11, 4.13.8, K4.4): confirmed writes only,
    every attempt's ledger paths of effect `observed` plus `facts.paths_written`. A
    `claimed_target` or `attempted` path never enters it, because neither effect proves that a
    write happened. Commit ids are the summary-shaped ones the facts carry."""
    paths: list[str] = []
    commits: list[str] = []
    for attempt in run.attempts:
        for path in ledger.attempt_ledger_paths(
            attempt, _CONFIRMED_WRITE_EFFECTS, include_facts_written=True,
        ):
            if path not in paths:
                paths.append(path)
        facts = attempt.facts
        if facts is None:
            continue
        commit_ids = facts.commits if isinstance(facts.commits, list) else []
        for commit_id in commit_ids:
            if commit_id not in commits:
                commits.append(commit_id)
    return paths, commits


def _fan_out_overlap(run: Run) -> bool:
    """docs/DESIGN.md 4.11 (K4.2): a workflow run in which two attempts of different keys
    overlapped in time. Both attempts need a known first and last timestamp and the closed
    intervals must intersect; an unknown timestamp never triggers it."""
    if run.kind != "workflow":
        return False
    timed = [
        (attempt.key, attempt.first_ts, attempt.last_ts)
        for attempt in run.attempts
        if attempt.key is not None and attempt.first_ts is not None
        and attempt.last_ts is not None
    ]
    for index, (key_a, first_a, last_a) in enumerate(timed):
        for key_b, first_b, last_b in timed[index + 1:]:
            if key_a != key_b and first_a <= last_b and first_b <= last_a:
                return True
    return False


def empty_unit_state(run: Run) -> tuple[str, int]:
    """docs/DESIGN.md 4.11 (F6 hardening, N8b K4.3, N8 fix 1 S2): what a run with no unit may say.
    ('ok', 0): a FINISHED run whose every attempt's own outcome is `completed` AND whose 4.13.1
    risk population is empty and complete, so "every attempt completed" is the whole truth.
    ('risk', n): the same finished all-completed run, but the risk population has `n` attempts
    or is not complete, so WORK AT RISK may still hold unchecked work. ('yet', 0): everything
    else. One rule for the sentence and the exit status of both `rescue plan` and `rescue
    status`."""
    if run.finished is not True or not all(a.outcome == "completed" for a in run.attempts):
        return "yet", 0
    population = ledger.risk_population(run)
    completeness = run.risk_population_completeness
    if completeness is None:
        completeness = ledger.risk_population_completeness(run)[0]
    if population or completeness != "complete":
        return "risk", len(population)
    return "ok", 0


def nothing_to_rescue_ok(run: Run) -> bool:
    """True exactly when `empty_unit_state` is 'ok': the one predicate for the cheerful
    sentence and the exit 0 of both `rescue plan` and `rescue status` (K4.3, S2)."""
    return empty_unit_state(run)[0] == "ok"


def _unchecked_sentence(count: int, run_id: str) -> str:
    if count == 0:  # N8 fix 2, R2-1: an empty but incomplete population has no exact count
        return (
            "NO KILLED OR FAILED ATTEMPT: the tool could not establish which attempts were at "
            f"risk; see WORK AT RISK in agent-postmortem run {run_id}."
        )
    return (
        f"NO KILLED OR FAILED ATTEMPT: {count} attempt(s) may still hold unchecked work; "
        f"see WORK AT RISK in agent-postmortem run {run_id}."
    )


def _lifecycle_unknown(run: Run) -> bool:
    """docs/DESIGN.md 4.13.3: the run's lifecycle terminal is `unknown` (a session has no
    lifecycle, so never)."""
    return run.lifecycle is not None and run.lifecycle.terminal == "unknown"


def _observation_reason(observation) -> str | None:
    """Why the repo state was not checked, for the plan's one-line note: the
    first distinct reason of a group that was not checked, or the fixed sentence
    when there is no group at all; None when some group was checked."""
    if observation.checked:
        return None
    for group in observation.groups:
        if group.reason:
            return group.reason
    return "no paths written by a killed or failed attempt"


def build_plan(
    run: Run, observation, findings: list[Finding], cfg: config_module.Config, *,
    possibly_live: bool = False,
) -> RescuePlan:
    """The full decision packet's raw facts (docs/DESIGN.md 4.11). `observation`
    is always observe.observe_groups(run) (N5c-1) -- the caller runs it once and
    passes it in so a test can also pass a hand-built one. `possibly_live` is
    computed by the caller (report.is_possibly_live), since it needs a file
    stat this module has no business doing on its own."""
    units_raw = qualifying_units(run)
    global_cwd = observe._discover_cwd(run) if observation.checked else None
    roots = declared_roots(cfg)
    units = [_unit_plan(run, unit, observation, global_cwd, roots) for unit in units_raw]

    already_raw_paths, already_commits = _already_exists(run)
    already_paths: list[str] = []
    already_sources: dict[str, str] = {}
    for raw_path in already_raw_paths:
        if observation.checked:
            display = _display_key(raw_path, global_cwd, observation.repo_root)
        else:
            display = raw_path
        if display not in already_paths:
            already_paths.append(display)
            already_sources[display] = raw_path
    has_unknown = (not observation.checked) or any(
        row.state == observe.UNKNOWN for unit in units for row in unit.paths
    ) or any(_unit_has_unknown_facts(unit) for unit in units_raw) or any(
        group.refused or group.unobserved_paths for group in observation.groups
    )

    empty_state, unchecked_count = empty_unit_state(run) if not units else ("yet", 0)
    unchecked = empty_state == "risk"
    nothing_to_rescue = not units and not unchecked
    # docs/DESIGN.md 4.11 (F6 hardening): "every attempt completed" is a
    # claim about a FINISHED run where every attempt's own outcome is
    # 'completed' -- not merely one with no killed/failed/superseded unit,
    # which is also true of an unfinished run, or one whose only attempt is
    # 'unknown' (no transcript at all, so not proven completed either).
    all_completed = empty_state == "ok"

    # docs/DESIGN.md 4.11, 4.13.11 (N5c-2): a lifecycle terminal `unknown` is an UNKNOWN
    # fact of the whole run, whatever the units say.
    lifecycle_unknown = _lifecycle_unknown(run)
    if possibly_live or lifecycle_unknown or unchecked:
        exit_code = 4
    elif nothing_to_rescue:
        exit_code = 0 if all_completed else 4
    elif has_unknown:
        exit_code = 4
    else:
        exit_code = 0

    return RescuePlan(
        run_id=run.run_id,
        kind=run.kind,
        finished=run.finished,
        possibly_live=possibly_live,
        nothing_to_rescue=nothing_to_rescue,
        nothing_to_rescue_ok=all_completed,
        units=units,
        checked=observation.checked,
        observation_reason=_observation_reason(observation),
        repo_root=observation.repo_root if observation.checked else None,
        config_warnings=list(cfg.warnings),
        killed_brief=_killed_brief_rows(run),
        failed_brief=_failed_brief_rows(run, findings),
        already_exists_paths=already_paths,
        already_exists_commits=already_commits,
        has_unknown_fact=has_unknown,
        exit_code=exit_code,
        already_exists_sources=already_sources,
        observation=observation,
        fan_out_overlap=_fan_out_overlap(run),
        unchecked_attempts=unchecked_count if unchecked else None,
    )


# --- text rendering ------------------------------------------------------


_PATH_COL = 27
_STATE_COL = 11
_ATTEMPTS_COL = 10
_PATH_INDENT = " " * 12


def _register_plan_paths(plan: RescuePlan, redactor: Redactor, run: Run | None = None) -> None:
    """Every path the plan text may print, in one namespace. At local (N3b
    P19) each display key is placed by the path function from the raw
    spelling it came from, against the run's working directory; the
    repository root is the one the plan's own observation resolved (D-n)."""
    cwd = observe._discover_cwd(run) if run is not None else None
    for unit in plan.units:
        for row in unit.paths:
            redactor.names.path(row.display_key, cwd, resolve_from=row.raw_path)
        for entry in unit.in_flight:
            if entry.display_key is not None:
                redactor.names.path(entry.display_key, cwd, resolve_from=entry.raw_path)
    for path in plan.already_exists_paths:
        redactor.names.path(path, cwd, resolve_from=plan.already_exists_sources.get(path))


def _register_plan_names(run: Run, redactor: Redactor, *, detailed: bool = False) -> None:
    report._register_names(run, redactor, detailed=detailed)


def _piece(redactor: Redactor, value):
    """One piece of a rescue line or record, scanned where it enters it (docs/DESIGN.md 5.1; F fix
    7, W7-4): a string goes through the one free-text scanner, anything else (a number, a bool,
    None) is not text and is returned as it is. An assembled line is never scanned again, so
    every piece that can hold text goes through this (or a pseudonym call) before it is joined."""
    return redactor(value) if isinstance(value, str) else value


def _raw_claim_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def _format_claim_value(key: str, value, redactor: Redactor) -> str:
    # docs/DESIGN.md 4.11 (F7 hardening): reported.status is arbitrary,
    # up-to-32-char free text (docs/DESIGN.md section 3, _REPORTED_STATUS_
    # LIMIT) an agent can put almost anything into -- by default it prints
    # as the fixed word "set", never its own value; --no-redact still shows
    # the real string.
    if key == "status":
        # at local the claim is free text of up to 32 characters: scanned like any other value
        return "set" if redactor.level == "share" else redactor(_raw_claim_value(value))
    if key == "report_path" and isinstance(value, str):
        return redactor.names.path(value)
    # a claim value is free text: scanned here, as its own piece (the assembled line is not
    # scanned again, docs/DESIGN.md 5.1: the end of a line belongs to the path that starts in it)
    return redactor(_raw_claim_value(value))


def _reported_text(reported: dict | None, redactor: Redactor) -> str:
    if reported is None:
        return "no result event"
    if reported.get("type") == "text":
        return f"text result, {reported.get('text_length', 0)} chars, not kept"
    if reported.get("type") == "dict":
        claims = reported.get("claims") or {}
        if not claims:
            return "dict result, no allowlisted claims"
        parts = [
            f"{key}: {_format_claim_value(key, value, redactor)}" for key, value in claims.items()
        ]
        return ", ".join(parts)
    return "no result event"


def _state_display(row: PathRow, redactor: Redactor) -> str:
    if row.state == observe.UNKNOWN and row.state_reason:
        return f"{observe.UNKNOWN} ({redactor(row.state_reason)})"
    return row.state


def _unit_header_line(unit: UnitPlan, redactor: Redactor) -> str:
    agent = redactor.names.agent(unit.label) or "agent"
    phase_part = ""
    if unit.phase:
        phase_part = f"  ({redactor.names.phase(unit.phase)})"
    plural = "attempt" if unit.attempts_count == 1 else "attempts"
    return (
        f"UNIT {agent}{phase_part}  {unit.attempts_count} {plural}, "
        f"last one {unit.last_end_word}"
    )


def _task_lines(unit: UnitPlan, view: "report.EvidenceView", redactor: Redactor) -> list[str]:
    """The prompt is quoted only with --evidence at local and raw (N3b D10,
    D-o), exactly as v0.2 printed it under --no-redact: the first 300
    characters, then the pointer. Otherwise the pointer to where it is and
    its length. At share the pointer names an aliased file; at local and raw
    the real one."""
    if unit.task is None:
        return ["  TASK      no prompt recorded"]
    pointer = view.pointer(unit.task.transcript_name, unit.task.line_no)
    if view.show_excerpts and pointer is not None:
        return [
            f'  TASK      "{redactor(unit.task.text[:300])}"', f"            full text: {pointer}",
        ]
    lines = []
    if pointer is not None:
        lines.append(f"  TASK      full text: {pointer}")
        lines.append(f"            prompt length: {unit.task.length} chars")
    else:
        lines.append(f"  TASK      prompt length: {unit.task.length} chars")
    return lines


def _paths_lines(unit: UnitPlan, redactor: Redactor) -> list[str]:
    lines = ["  PATHS     " + f"{'path':<{_PATH_COL}}{'state':<{_STATE_COL}}"
             f"{'attempts':<{_ATTEMPTS_COL}}later write"]
    if not unit.paths:
        lines.append(_PATH_INDENT + "none")
        return lines
    states = [_state_display(row, redactor) for row in unit.paths]
    # The state column grows to the widest state in this table (an UNKNOWN
    # with its reason is longer than the fixed width), so the columns after
    # it never run together.
    state_col = max(_STATE_COL, max(len(state) for state in states) + 2)
    for row, state in zip(unit.paths, states):
        display = redactor(row.display_key)
        attempts_str = ", ".join(str(o) for o in row.attempt_ordinals)
        later = "yes" if row.later_write else "no"
        lines.append(
            _PATH_INDENT
            + f"{display:<{_PATH_COL}}{state:<{state_col}}{attempts_str:<{_ATTEMPTS_COL}}{later}"
        )
    return lines


def _commits_line(unit: UnitPlan, redactor: Redactor) -> str:
    ids = ", ".join(redactor(commit) for commit in unit.commits) if unit.commits else "0"
    return f"  COMMITS   reported by these attempts: {ids}"


def _in_flight_lines(unit: UnitPlan, redactor: Redactor) -> list[str]:
    if not unit.in_flight:
        return []
    lines = []
    for index, entry in enumerate(unit.in_flight):
        tool = redactor(entry.tool or "tool")
        path_display = redactor(entry.display_key) if entry.display_key else "(no path)"
        text = f"{tool} {path_display} (no result recorded)"
        prefix = "  IN FLIGHT at the kill: " if index == 0 else _PATH_INDENT
        lines.append(prefix + text)
    return lines


_UNKNOWN_GROUP_SENTENCE = (
    "(repo state not seen for some of this unit's attempts: a shell command or an "
    "unobserved path may hide what they wrote)"
)

_UNKNOWN_SENTENCE = (
    "whose content is in the dirty paths; whether the task's goal was reached; "
    "what remains of the task"
)


def _unknown_lines(unit: UnitPlan, plan: RescuePlan, redactor: Redactor) -> list[str]:
    lines = [f"  UNKNOWN   {_UNKNOWN_SENTENCE}"]
    if not plan.checked and plan.observation_reason:
        lines.append(f"            (repo state not checked: {redactor(plan.observation_reason)})")
    # docs/DESIGN.md 4.11 (F6 hardening): a risk attempt with no transcript
    # at all (facts is None) is its own, distinct UNKNOWN reason.
    if unit.facts_unknown:
        lines.append("            UNKNOWN facts: transcript missing")
    if unit.unknown_group and plan.checked:
        # (when nothing was checked, the line above already says the state was not seen)
        lines.append(f"            {_UNKNOWN_GROUP_SENTENCE}")
    return lines


def _checks_line(unit: UnitPlan, plan: RescuePlan, redactor: Redactor) -> str:
    if unit.declared_checks:
        # docs/DESIGN.md 4.11/5.1 (F7 hardening): a declared check's own
        # text gets its own check-N pseudonym by default -- the whole
        # command hidden, not merely a path embedded inside it.
        joined = "; ".join(redactor.check_text(check) for check in unit.declared_checks)
        if len(unit.repos) > 1:
            return f"  CHECKS    declared across {len(unit.repos)} repositories: {joined}"
        return f"  CHECKS    declared for this repo: {joined}"
    if plan.checked and plan.repo_root:
        repo_display = plan.repo_root if redactor.level == "raw" else "this repository"
        return (
            f'  CHECKS    no checks declared for {repo_display}: add [[repos]] to the '
            "config, see docs"
        )
    return (
        "  CHECKS    no checks declared for this repository: add [[repos]] to the "
        "config, see docs"
    )


def _step_paths(unit: UnitPlan, plan: RescuePlan) -> tuple[list[PathRow], str]:
    """The rows the `git status`/`git diff` steps name, and the note that goes
    with them. Dirty paths always; when the repo state was not checked at all,
    every path written; a path whose group was refused or not checked is listed
    too, since nothing looked at it."""
    dirty = [row for row in unit.paths if row.state == observe.DIRTY_NOW]
    if not plan.checked and unit.paths:
        return (dirty or list(unit.paths)), (
            "" if dirty else "   (repo state not checked; every path written is listed)"
        )
    unchecked = [row for row in unit.paths if row.unchecked]
    listed = [
        row for row in unit.paths if row.state == observe.DIRTY_NOW or row.unchecked
    ]
    if unchecked and listed:
        return listed, "   (repo state not checked for some paths; those are listed too)"
    return dirty, ""


def _run_check_text(unit: UnitPlan, redactor: Redactor) -> str:
    """The `[decide]` step that runs the unit's declared checks: from the repo root for
    one repository, from each repository's own root for several."""
    joined = "; ".join(redactor.check_text(check) for check in unit.declared_checks)
    if len(unit.repos) > 1:
        return f"run the declared checks, each from its own repository root: {joined}"
    return f"run the declared check from the repo root: {joined}"


def _steps_lines(unit: UnitPlan, plan: RescuePlan, redactor: Redactor) -> list[str]:
    step_paths, unchecked_note = _step_paths(unit, plan)

    if not step_paths and plan.checked:
        # Observation ran and nothing the killed attempts wrote is dirty
        # now: there is nothing to inspect, keep, or commit. Say so instead
        # of printing commands with no paths. A unit whose attempts sit in a
        # refused or unobserved group (N5 fix 1, S1) is never told that: what
        # it wrote was not seen.
        if unit.unknown_group:
            first = (
                "   1. [look]   no path was observed for this unit: a shell command or an "
                "unobserved repository may hide what it wrote; run git status --short in the "
                "repository the agent worked in"
            )
        else:
            first = (
                "   1. [look]   no path is dirty now: what the killed attempts wrote is "
                "committed (CLEAN_NOW) or absent (MISSING_NOW); see PATHS"
            )
        lines = ["  STEPS", first]
        if plan.possibly_live:
            return lines
        if unit.declared_checks:
            lines.append(
                f"   2. [decide] {_run_check_text(unit, redactor)}"
            )
        else:
            lines.append("   2. [decide] declare a check first")
        lines.append("   3. [decide] relaunch the remaining work")
        return lines

    # docs/DESIGN.md 4.11 (K4.1): a unit with no path to name gets both whole-tree commands, no
    # pathspec; the literal `<none>` is never printed.
    if step_paths:
        path_str = " ".join(redactor(row.display_key) for row in step_paths)
        status_command = f"git status --short -- {path_str}{unchecked_note}"
        diff_command = f"git diff -- {path_str}"
    else:
        status_command = "git status --short"
        diff_command = "git diff"

    lines = [
        "  STEPS",
        f"   1. [look]   {status_command}",
        f"   2. [look]   {diff_command}",
    ]
    # docs/DESIGN.md 4.11 (LIVE rule): a possibly-live run only ever gets the
    # two read-only look steps -- committing, relaunching, or even deciding
    # what to keep is unsafe advice while the run might still be writing to
    # this same repo.
    if plan.possibly_live:
        return lines

    if unit.declared_checks:
        check_text = _run_check_text(unit, redactor)
    else:
        check_text = "declare a check first"

    lines.extend([
        f"   3. [decide] {check_text}",
        "   4. [decide] keep, fix, or set aside each dirty path. A passing check is "
        "evidence, not a decision.",
        "   5. [decide] commit, after reading the diff",
        "   6. [decide] relaunch the remaining work",
    ])
    return lines


def _never_line() -> str:
    return "  NEVER  git checkout / restore / reset / stash / clean on these paths"


def _render_unit_text(
    unit: UnitPlan, plan: RescuePlan, redactor: Redactor, view: "report.EvidenceView",
) -> str:
    lines = [_unit_header_line(unit, redactor)]
    lines.extend(_task_lines(unit, view, redactor))
    lines.append(f"  REPORTED  {_reported_text(unit.reported, redactor)}")
    lines.extend(_paths_lines(unit, redactor))
    lines.append(_commits_line(unit, redactor))
    lines.extend(_in_flight_lines(unit, redactor))
    lines.extend(_unknown_lines(unit, plan, redactor))
    lines.append(_checks_line(unit, plan, redactor))
    lines.extend(_steps_lines(unit, plan, redactor))
    lines.append(_never_line())
    # every piece above was scanned where it entered the line; an assembled line is not scanned
    # again, because a path hides the rest of its line (docs/DESIGN.md 5.1)
    return "\n".join(lines)


def _brief_text(plan: RescuePlan, redactor: Redactor) -> str:
    killed_parts = []
    for row in plan.killed_brief:
        label = redactor.names.agent(row["label"])
        time_str = redactor(row["time"]) if row["time"] else "unknown time"
        silence = row["silence_seconds"]
        silence_str = f"{int(silence)}s" if silence is not None else "unknown"
        killed_parts.append(
            f"{label} attempt {row['ordinal']} at {time_str}, after {silence_str} of silence"
        )
    killed_text = "; ".join(killed_parts) if killed_parts else "none"

    failed_parts = []
    for row in plan.failed_brief:
        label = redactor.names.agent(row["label"])
        cause_phrase = redactor(report._cause_phrase(row["cause"]))
        piece = f"{label}: {cause_phrase}, retry_now {_piece(redactor, row['retry_now'])}"
        if row["reset_time"]:
            piece += f", resets {redactor(row['reset_time'])}"
        failed_parts.append(piece)
    failed_text = "; ".join(failed_parts) if failed_parts else "none"

    paths = [redactor(path) for path in plan.already_exists_paths]
    commits = [redactor(commit) for commit in plan.already_exists_commits]
    # docs/DESIGN.md 4.11, 4.13.1 (N5c-2): a commit id is a summary-shaped output
    # line the tool has not confirmed, so the brief says commits were reported, never
    # that they exist.
    paths_text = f"{len(paths)} paths ({', '.join(paths)})" if paths else "0 paths"
    commits_text = (
        f"commits reported: {len(commits)} ({', '.join(commits)})" if commits
        else "commits reported: 0"
    )

    lines = [
        "BRIEF FOR A RELAUNCH",
        f"  killed: {killed_text}   failed: {failed_text}",
        f"  already exists: {paths_text}; {commits_text}",
        "  start from what exists; do not redo it",
    ]
    # docs/DESIGN.md 4.11 (K4.2): only when two attempts of different keys overlapped in time.
    if plan.fan_out_overlap:
        lines.append(
            "  caveat: resume re-runs downstream agents after a parallel fan-out "
            "(anthropics/claude-code #95076)"
        )
    return "\n".join(lines)


_NOTHING_YET = (
    "NOTHING TO RESCUE YET: the run has not finished and no attempt was killed or failed."
)

# docs/DESIGN.md 4.11 (field milestone F, FB3): the first line of `rescue plan` text on a
# possibly-live run, one of three, by who may still be writing.
_NOTHING_YET_LIVE = (
    "NOTHING TO RESCUE YET: the run may still be running; no attempt has been killed or "
    "failed so far."
)

_LIVE_OTHER = (
    "POSSIBLY LIVE: the run may still be running, and another agent may be writing the files "
    "listed below. Do not modify them until it has stopped; confirm with the human."
)

_LIVE_OWN = (
    "YOUR CURRENT SESSION: this run is the session you are working in. Check the files listed "
    "below yourself before relaunching the killed work."
)


def _is_callers_session(run: Run, caller_session: str | None) -> bool:
    """FB3: a SESSION run whose transcript stem equals the caller's own session id (a
    canonical UUID, compared in lowercase). A workflow run is never the caller's session,
    even one its session launched: its workers may still be writing."""
    return (
        caller_session is not None
        and run.kind == "session"
        and run.path.stem.lower() == caller_session.lower()
    )


def render_plan_text(
    run: Run, plan: RescuePlan, redactor: Redactor, *, evidence: bool = False,
    caller_session: str | None = None,
) -> str:
    _register_plan_names(run, redactor, detailed=True)
    _register_plan_paths(plan, redactor, run)
    view = report.EvidenceView(run, redactor, show_excerpts=evidence)

    blocks: list[str] = []
    if evidence and view.note() is not None:
        blocks.append(view.note())
    if plan.possibly_live and plan.units:
        # FB3: who may still be writing the listed files decides the wording.
        blocks.append(_LIVE_OWN if _is_callers_session(run, caller_session) else _LIVE_OTHER)
    if plan.nothing_to_rescue:
        # docs/DESIGN.md 4.11: "every attempt completed" is a claim about a
        # finished run. A possibly-live run with no qualifying unit yet is a
        # different, honest claim -- nothing has needed rescuing SO FAR, not
        # that everything is done -- so it gets its own sentence (FB3), never
        # the cheerful one and never a do-not-modify warning with no file to protect.
        if plan.possibly_live:
            blocks.append(_NOTHING_YET_LIVE)
        else:
            if plan.nothing_to_rescue_ok:
                blocks.append("NOTHING TO RESCUE: every attempt completed.")
            else:
                # F6 hardening: an unfinished run (or one with an attempt
                # that is neither completed nor killed/failed/superseded)
                # has no qualifying unit YET, not because it is done.
                blocks.append(_NOTHING_YET)
        return "\n".join(blocks) + "\n"

    if plan.unchecked_attempts is not None:
        # N8 fix 1, S2: no unit, but the risk population is not empty or not complete
        if plan.possibly_live:
            # unreachable from the CLI (an unchecked population needs a finished run); a
            # hand-built plan keeps the warning rather than printing nothing
            blocks.append(_LIVE_OTHER)
        else:
            blocks.append(redactor(_unchecked_sentence(plan.unchecked_attempts, plan.run_id)))
        return "\n".join(blocks) + "\n"

    for unit in plan.units:
        blocks.append(_render_unit_text(unit, plan, redactor, view))

    if not plan.possibly_live:
        blocks.append(_brief_text(plan, redactor))

    return "\n".join(blocks) + "\n"


# --- JSON rendering -----------------------------------------------------


def _export_path_row(row: PathRow, redactor: Redactor, view: "report.EvidenceView") -> dict:
    """docs/DESIGN.md 5.3b: observation state and ledger facts in separate
    fields. `group` is the index of the path's group in the plan's `groups`
    array (None when no group lists the path); `observation` is the path's own
    observation, null when its group was refused, unknown, or not checked (N5c-1);
    `ledger` is the list of this path's ledger entries in attempt order, `[]`
    when none names it."""
    return {
        "path": redactor(row.display_key),
        "group": row.group,
        "observation": (
            {
                "state": row.state,
                "reason": redactor(row.state_reason) if row.state_reason else None,
            }
            if row.observation is not None else None
        ),
        "ledger": [
            {
                "attempt": ordinal,
                "effect": entry.effect,
                "basis": entry.basis,
                "completion": entry.completion,
                "line": view.ledger_line(entry.line),
            }
            for ordinal, entry in row.ledger
        ],
        "attempts": row.attempt_ordinals,
        "later_write_observed": row.later_write,
    }


def _export_reported(reported: dict | None, redactor: Redactor) -> dict | None:
    if reported is None:
        return None
    if reported.get("type") == "text":
        return {"type": "text", "text_length": reported.get("text_length")}
    if reported.get("type") == "dict":
        claims = reported.get("claims") or {}
        exported_claims = {}
        for key, value in claims.items():
            if key == "report_path" and isinstance(value, str):
                exported_claims[key] = redactor.names.path(value)
            elif key == "status":
                # F7 hardening: same "status: set" treatment as the text
                # renderer's _format_claim_value.
                exported_claims[key] = value if not redactor.enabled else "set"
            else:
                # a number, a bool, or a list of commit ids: every string in it is scanned
                exported_claims[key] = report.redact_value(value, redactor)
        return {"type": "dict", "claims": exported_claims}
    return None


def _export_unit(
    unit: UnitPlan, plan: RescuePlan, redactor: Redactor, view: "report.EvidenceView",
) -> dict:
    task = None
    if unit.task is not None:
        # docs/DESIGN.md 5.3b: the pointer follows the `line` convention (null
        # for an attempt with no canonical agent id at share); `text` exists
        # only at local and raw with --evidence, structurally redacted there.
        task = {
            "pointer": view.pointer(unit.task.transcript_name, unit.task.line_no),
            "length": unit.task.length,
        }
        if view.show_excerpts:
            task["text"] = redactor(unit.task.text[:300])
    unknown = [_UNKNOWN_SENTENCE]
    if not plan.checked and plan.observation_reason:
        unknown.append(redactor(plan.observation_reason))
    if unit.facts_unknown:
        unknown.append("UNKNOWN facts: transcript missing")
    if unit.unknown_group and plan.checked:
        unknown.append(_UNKNOWN_GROUP_SENTENCE)

    step_paths, _note = _step_paths(unit, plan)
    path_str = " ".join(redactor(row.display_key) for row in step_paths) if step_paths else None
    # docs/DESIGN.md 4.11 (K4.1): both look commands are always non-null strings, the
    # whole-tree forms when no path is named.
    steps = [
        {"grade": "look", "text": "git status", "command":
            f"git status --short -- {path_str}" if path_str else "git status --short"},
        {"grade": "look", "text": "git diff", "command":
            f"git diff -- {path_str}" if path_str else "git diff"},
    ]
    # docs/DESIGN.md 4.11 (LIVE rule): steps 3-6 (decide/commit/relaunch) are
    # never offered for a possibly-live run, in JSON any more than in text.
    if not plan.possibly_live:
        if unit.declared_checks:
            check_command = "; ".join(
                redactor.check_text(check) for check in unit.declared_checks
            )
            step3_text = _run_check_text(unit, redactor)
        else:
            check_command = None
            step3_text = "declare a check first"
        step3_command = check_command if check_command else None
        steps.extend([
            {"grade": "decide", "text": step3_text, "command": step3_command},
            {"grade": "decide", "text": "keep, fix, or set aside each dirty path. A passing "
                                         "check is evidence, not a decision.", "command": None},
            {"grade": "decide", "text": "commit, after reading the diff", "command": None},
            {"grade": "decide", "text": "relaunch the remaining work", "command": None},
        ])

    return {
        "agent": redactor.names.agent(unit.label),
        "phase": redactor.names.phase(unit.phase) if unit.phase else None,
        "attempts": unit.attempts_count,
        "end": unit.last_end_word,
        "task": task,
        "reported": _export_reported(unit.reported, redactor),
        "paths": [_export_path_row(row, redactor, view) for row in unit.paths],
        "commits": [redactor(commit) for commit in unit.commits],
        "in_flight": [
            {
                "tool": _piece(redactor, entry.tool),
                "path": redactor(entry.display_key) if entry.display_key else None,
            }
            for entry in unit.in_flight
        ],
        "unknown": unknown,
        "checks": [redactor.check_text(check) for check in unit.declared_checks],
        "steps": steps,
    }


def plan_to_json(
    run: Run, plan: RescuePlan, redactor: Redactor, *, evidence: bool = False,
) -> dict:
    _register_plan_names(run, redactor, detailed=True)
    _register_plan_paths(plan, redactor, run)
    view = report.EvidenceView(run, redactor, show_excerpts=evidence)

    killed = [
        {
            "agent": redactor.names.agent(row["label"]),
            "ordinal": row["ordinal"],
            "time": _piece(redactor, row["time"]),
            "silence_seconds": row["silence_seconds"],
        }
        for row in plan.killed_brief
    ]
    failed = [
        {
            "agent": redactor.names.agent(row["label"]),
            "cause": _piece(redactor, row["cause"]),
            "retry_now": _piece(redactor, row["retry_now"]),
            "reset_time": _piece(redactor, row["reset_time"]),
        }
        for row in plan.failed_brief
    ]
    brief = {
        "killed": killed,
        "failed": failed,
        "already_exists": {
            "paths": [redactor(path) for path in plan.already_exists_paths],
            "commits": [redactor(commit) for commit in plan.already_exists_commits],
        },
    }

    return {
        "schema": SCHEMA_VERSION,
        "disclosure": redactor.level,
        "run": {
            "run_id": plan.run_id, "kind": plan.kind, "finished": plan.finished,
            "possibly_live": plan.possibly_live,
        },
        "nothing_to_rescue": plan.nothing_to_rescue,
        "groups": (
            report.export_observation_groups(plan.observation, redactor, omit_empty_unknown=True)
            if plan.observation is not None else []
        ),
        "units": [_export_unit(unit, plan, redactor, view) for unit in plan.units],
        "brief": (
            brief if (
                not plan.possibly_live and not plan.nothing_to_rescue
                and plan.unchecked_attempts is None
            ) else None
        ),
        "exit_code": plan.exit_code,
    }


# =============================================================================
# rescue status: what was observed (docs/DESIGN.md 4.11, proposal 8.3)
# =============================================================================


@dataclass
class NotCountedRow:
    line_no: int
    reason: str
    excerpt: str
    category: str = "command"  # fixed text from code; never transcript bytes


@dataclass
class CountedRun:
    line_no: int
    ts: datetime | None
    command: str
    completion: str  # 'completed_ok' | 'completed_error'


@dataclass
class NeverRow:
    line_no: int
    excerpt: str
    category: str = "command"  # the matched rule's own spelling, e.g. "git checkout"


@dataclass
class UnitStatus:
    key: str
    label: str
    checks_declared: int
    last_counted: CountedRun | None
    word: str  # EXIT_0_OBSERVED | NONZERO_OBSERVED | NOT_OBSERVED
    writes_after: int
    cycles: int
    # docs/DESIGN.md 4.11 (F3 hardening): a NEVER-list command was observed
    # anywhere in the relevant transcript window. Kept separate from `word`
    # itself (never_matches is transcript-wide, not attributable to one
    # unit) so equality checks against the plain word ("NONZERO_OBSERVED",
    # etc.) elsewhere are unaffected; a renderer appends "NEVER_COMMAND_
    # OBSERVED" to the displayed word when this is true.
    never_command_observed: bool = False
    # docs/DESIGN.md 4.11 (N5c-2): the declared repository this row is about (None for a
    # unit no declared repository contains).
    repo: str | None = None


@dataclass
class RescueStatus:
    run_id: str
    units: list[UnitStatus]
    not_counted: list[NotCountedRow]
    never_matches: list[NeverRow]
    transcript_unknown: bool
    checked: bool
    repo_root: str | None
    exit_code: int = 0
    listed_paths: tuple[str, ...] = ()  # display keys of every unit's candidate paths
    # docs/DESIGN.md 4.11 (K4.3): with no unit, true only for a finished run whose every attempt
    # completed (the one predicate `rescue plan` uses).
    nothing_to_rescue_ok: bool = False
    # N8 fix 1, S2: see RescuePlan.unchecked_attempts.
    unchecked_attempts: int | None = None


def _never_list_match(command: str) -> bool:
    return bool(_NEVER_LIST_RE.search(command))


def _never_category(command: str) -> str:
    """The matched NEVER rule's own canonical spelling (`git checkout`),
    built from the rule's fixed vocabulary, never from the transcript's
    bytes (N3b C1). A command that matches no rule gets the fixed word
    `command`."""
    match = _NEVER_LIST_RE.search(command)
    if match is None:
        return "command"
    return f"git {match.group(1)}"


def _matched_check(command: str, declared_checks: list[str]) -> str | None:
    """The declared check this command is, or merely contains: an exact (trailing
    whitespace stripped) match first, else the first one it contains."""
    stripped = command.rstrip()
    if stripped in declared_checks:
        return stripped
    for check in declared_checks:
        if check in command:
            return check
    return None


def _classify_bash_call(call, declared_checks: list[str], repo_root: str | None):
    """(matched_check, reason) for one Bash call already known to be after
    run.ended_at: reason is None when it counts, else one of 'other cwd',
    'compound command', 'backgrounded', 'no result' -- docs/DESIGN.md 4.11's
    exact vocabulary. matched_check is the declared check string this call's
    command contains, or None when it contains none of them at all (the call
    is then irrelevant and never listed). `repo_root` is the declared repository
    the checks belong to: a call counts only at its exact working directory."""
    command = call.command
    if not command:
        return None, None
    matched = _matched_check(command, declared_checks)
    if matched is None:
        return None, None

    # docs/DESIGN.md 4.2/4.11 (F5 hardening): a call whose tool_use_id was
    # duplicated in the transcript can never be trusted as a genuine check
    # run -- neither its exit state nor which result truly belongs to it is
    # known -- so it is never counted, regardless of what its own command
    # text says.
    if call.completion == "ambiguous":
        return matched, "ambiguous tool id"
    if call.completion == "no_result":
        return matched, "no result"
    if call.completion == "backgrounded":
        return matched, "backgrounded"
    normalized_cwd = os.path.normpath(call.cwd) if call.cwd else None
    normalized_root = os.path.normpath(repo_root) if repo_root else None
    if normalized_cwd != normalized_root:
        return matched, "other cwd"
    if command.rstrip() != matched:
        return matched, "compound command"
    return matched, None


_WRITE_SHELL_TOOLS = ("Bash", "Monitor")
_WRITE_FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _file_tool_path(call) -> str | None:
    """The target a file-tool call named: MultiEdit keeps it in `multiedit_path`
    (4.13.1), the other file tools in `path`."""
    path = call.multiedit_path if call.tool_name == "MultiEdit" else call.path
    return path if isinstance(path, str) and path else None


def _shell_cwd(call) -> str | None:
    """The working directory a shell call's later segments run in (4.13.1, L9): the call's
    own absolute cwd, replaced by a literal absolute leading `cd DIR &&`; None when it is
    missing, relative, or changed by any other shape (an opaque, unrecorded, or later cd)."""
    if not call.command:
        return None
    lexed = commands.lex(call.command)
    if not lexed.placed or not lexed.segments:
        return None
    bases = commands.segment_bases(lexed, call.cwd)
    start = 1 if commands.cd_operand(lexed.segments[0]) is not None else 0
    rest = bases[start:] or bases[:1]
    if any(base is None for base in rest):
        return None
    return rest[0]


def _write_order(call) -> tuple:
    return (call.ts or _MIN_TS, call.line_no)


def _file_write_key(call) -> str:
    """The absolute, normalized spelling of a file-tool call's target, or its own text when
    nothing places it."""
    path = _file_tool_path(call) or ""
    if path.startswith("/"):
        return commands.normalize_path(path)
    if isinstance(call.cwd, str) and call.cwd.startswith("/"):
        return commands.normalize_path(os.path.join(call.cwd, path))
    return path


def build_status(run: Run, cfg: config_module.Config, observation) -> RescueStatus:
    parent_path = reader._parent_transcript_path(run)
    transcript_unknown = False
    tool_calls: list = []
    if parent_path is not None and parent_path.is_file():
        main_attempt, _version, _sid, diagnostics, incomplete, _ts = reader._parse_main_attempt(
            parent_path
        )
        tool_calls = list(main_attempt.tool_calls)
        transcript_unknown = bool(diagnostics) or incomplete
    else:
        transcript_unknown = True

    ended_at = run.ended_at
    relevant_calls = [
        call for call in tool_calls
        if ended_at is None or (call.ts is not None and call.ts > ended_at)
    ]

    # docs/DESIGN.md 4.11 (N5c-2, plan S7): everything below is per (unit, declared
    # repository). The roots are the configuration's own, lexical, never from git.
    roots = declared_roots(cfg)
    units = qualifying_units(run)
    unit_rows = []  # (unit, listed spellings, target -> owning roots, repos)
    relevant_roots: list[str] = []
    for unit in units:
        specs = _row_specs(unit, observation)
        owners = _target_owners(specs, roots)
        repos = _unit_repos(owners)
        for root in repos:
            if root not in relevant_roots:
                relevant_roots.append(root)
        unit_rows.append((unit, _listed_spellings(unit), owners, repos))

    counted: dict[str, list[CountedRun]] = {root: [] for root in relevant_roots}
    counted_call_ids: set[int] = set()
    not_counted: list[NotCountedRow] = []
    never_matches: list[NeverRow] = []

    for call in relevant_calls:
        if call.tool_name != "Bash" or not call.command:
            continue
        if _never_list_match(call.command):
            never_matches.append(NeverRow(
                line_no=call.line_no, excerpt=call.command[:400],
                category=_never_category(call.command),
            ))
        counted_somewhere = False
        first_reason = None
        for root in relevant_roots:
            matched, reason = _classify_bash_call(call, roots[root], root)
            if matched is None:
                continue
            if reason is None:
                counted[root].append(CountedRun(
                    line_no=call.line_no, ts=call.ts, command=matched,
                    completion=call.completion,
                ))
                counted_somewhere = True
                counted_call_ids.add(id(call))
            elif first_reason is None:
                first_reason = reason
        if not counted_somewhere and first_reason is not None:
            not_counted.append(NotCountedRow(
                line_no=call.line_no, reason=first_reason, excerpt=call.command[:400],
                category=_never_category(call.command),
            ))

    for root in relevant_roots:
        counted[root].sort(key=lambda c: (c.ts or _MIN_TS, c.line_no))

    # docs/DESIGN.md 4.11 (F3 hardening, N5c-2): a shell call that is not proven read-only
    # is a possible write. It belongs to the one declared repository that owns its working
    # directory (after a literal absolute leading cd); a call whose directory is missing,
    # unknown, or owned by no declared root may have written in every repository of the run
    # (fail closed). Only a call counted as a declared check for some repository is skipped
    # (N5 fix 1, C1): a call that merely contains a check string but was rejected (other
    # cwd, compound, backgrounded, no result, ambiguous id) is a possible write like any other.
    shell_writes: list[tuple[tuple, object, frozenset[str]]] = []
    for call in relevant_calls:
        if call.tool_name not in _WRITE_SHELL_TOOLS:
            continue
        if id(call) in counted_call_ids:
            continue
        if call.command and _proven_read_only(call.command):
            continue
        cwd = _shell_cwd(call)
        owner = _owner(cwd, roots) if cwd is not None else None
        affected = frozenset({owner}) if owner is not None else frozenset(relevant_roots)
        shell_writes.append((_write_order(call), call, affected))

    file_writes = [
        call for call in relevant_calls
        if call.tool_name in _WRITE_FILE_TOOLS and call.completion == "completed_ok"
        and _file_tool_path(call)
    ]

    flagged_shell_calls: set[int] = set()
    unit_statuses: list[UnitStatus] = []
    all_listed: list[str] = []
    global_cwd = observe._discover_cwd(run) if observation.checked else None
    for unit, spellings, owners, repos in unit_rows:
        listed_keys = {
            _display_key(raw, global_cwd, observation.repo_root) if observation.checked else raw
            for raw in spellings
        }
        all_listed.extend(key for key in listed_keys if key not in all_listed)

        for repo in (repos or [None]):
            counted_here = counted.get(repo, []) if repo is not None else []
            last_counted = counted_here[-1] if counted_here else None
            if last_counted is None:
                word = "NOT_OBSERVED"
            elif last_counted.completion == "completed_ok":
                word = "EXIT_0_OBSERVED"
            else:
                word = "NONZERO_OBSERVED"

            writes: list[tuple] = []
            shell_after: list[object] = []
            if repo is not None:
                for call in file_writes:
                    if repo in owners.get(_file_write_key(call), ()):
                        writes.append(_write_order(call))
                for order_key, call, affected in shell_writes:
                    if repo in affected:
                        writes.append(order_key)
                        shell_after.append((order_key, call))

            writes_after = 0
            if last_counted is not None:
                last_order = (last_counted.ts or _MIN_TS, last_counted.line_no)
                writes_after = sum(1 for key in writes if key > last_order)
                for order_key, call in shell_after:
                    if order_key > last_order:
                        flagged_shell_calls.add(id(call))

            # cycles: a counted run preceded, since the previous counted run, by at least one
            # write this repository owns.
            timeline = [((c.ts or _MIN_TS, c.line_no), "check") for c in counted_here]
            timeline.extend((key, "write") for key in writes)
            timeline.sort(key=lambda item: (item[0], item[1] == "check"))
            cycles = 0
            write_since_last_check = False
            for _key, kind in timeline:
                if kind == "write":
                    write_since_last_check = True
                else:
                    if write_since_last_check:
                        cycles += 1
                    write_since_last_check = False

            unit_statuses.append(UnitStatus(
                key=unit.key, label=unit.label,
                checks_declared=len(roots[repo]) if repo is not None else 0,
                last_counted=last_counted, word=word, writes_after=writes_after,
                cycles=cycles, never_command_observed=bool(never_matches), repo=repo,
            ))

    listed_lines = {row.line_no for row in not_counted}
    for _order_key, call, _affected in shell_writes:
        if id(call) in flagged_shell_calls and call.line_no not in listed_lines:
            command = call.command or ""
            not_counted.append(NotCountedRow(
                line_no=call.line_no,
                reason="a Bash command that may write ran after the check",
                excerpt=command[:400],
                category=_never_category(command),
            ))

    # docs/DESIGN.md 4.11, 4.13.11 (N5c-2), the first condition that applies wins: 3 for
    # any failed check, ahead of every 4 condition (a NEVER-list match, an unknown
    # lifecycle, a transcript that could not be read, a check not observed, a write after
    # a check); else 0.
    # K4.3: no unit at all exits 0 only for a finished run whose every attempt completed.
    empty_state, empty_count = empty_unit_state(run) if not unit_statuses else ("yet", 0)
    empty_not_ok = not unit_statuses and empty_state != "ok"
    if any(u.word == "NONZERO_OBSERVED" for u in unit_statuses):
        exit_code = 3
    elif (
        transcript_unknown or never_matches or _lifecycle_unknown(run) or empty_not_ok
        or any(u.word == "NOT_OBSERVED" or u.writes_after > 0 for u in unit_statuses)
    ):
        exit_code = 4
    else:
        exit_code = 0

    return RescueStatus(
        run_id=run.run_id, units=unit_statuses, not_counted=not_counted,
        never_matches=never_matches, transcript_unknown=transcript_unknown,
        checked=observation.checked, repo_root=observation.repo_root,
        exit_code=exit_code, listed_paths=tuple(all_listed),
        nothing_to_rescue_ok=empty_state == "ok",
        unchecked_attempts=empty_count if empty_state == "risk" else None,
    )


_FRESHNESS_SENTENCE = (
    "Writes made outside this session after the check cannot be seen; "
    "freshness beyond the transcript is UNKNOWN."
)


def _display_word(unit: UnitStatus) -> str:
    """docs/DESIGN.md 4.11 (F3 hardening): the plain word (EXIT_0_OBSERVED /
    NONZERO_OBSERVED / NOT_OBSERVED) with NEVER_COMMAND_OBSERVED appended
    when a NEVER-list command was observed -- appended, never substituted,
    so both signals stay visible on the same line."""
    if unit.never_command_observed:
        return f"{unit.word} NEVER_COMMAND_OBSERVED"
    return unit.word


def _unit_status_lines(
    unit: UnitStatus, status: RescueStatus, redactor: Redactor, *, evidence: bool = False,
) -> list[str]:
    agent = redactor.names.agent(unit.label)
    repo_part = f"   repository {redactor.names.path(unit.repo)}" if unit.repo is not None else ""
    lines = [f"UNIT {agent}{repo_part}   checks declared: {unit.checks_declared}"]
    display_word = _display_word(unit)
    if unit.last_counted is not None:
        exit_word = "0" if unit.last_counted.completion == "completed_ok" else "non-zero"
        time_str = (
            unit.last_counted.ts.astimezone(timezone.utc).strftime("%H:%MZ")
            if unit.last_counted.ts else "unknown"
        )
        lines.append(
            f"  last counted run: {redactor.check_text(unit.last_counted.command)}  "
            f"exit {exit_word}  at {time_str}      -> {display_word}"
        )
    else:
        lines.append(f"  last counted run: none      -> {display_word}")
    write_suffix = "  -> WRITE_AFTER_CHECK" if unit.writes_after > 0 else ""
    lines.append(f"  writes to listed paths after that run: {unit.writes_after}{write_suffix}")
    lines.append(f"  check cycles observed: {unit.cycles} of 2")
    for row in status.not_counted:
        lines.append(f"  not counted: line {row.line_no}: {row.reason}")
    for row in status.never_matches:
        if evidence and redactor.level != "share":
            # D-o: v0.2's --no-redact line: the paths replaced the way the
            # PATHS table spells them at this level, the first 60 characters.
            excerpt = _pseudonymize_excerpt(row.excerpt, status, redactor)[:60]
        else:
            excerpt = row.category
        lines.append(f"  commands matching the NEVER list: line {row.line_no}: {excerpt}")
    return lines


def _pseudonymize_excerpt(excerpt: str, status: RescueStatus, redactor: Redactor) -> str:
    """A command excerpt as `rescue status --evidence` prints it: every listed path, in its
    display (repo-relative) and absolute spellings, registered in the active path namespace so it
    prints as the path-N the PATHS table uses (at local the P19 display), and the excerpt then
    goes through the one free-text scanner (docs/DESIGN.md 5.1). No raw substring replacement:
    the scanner renders the complete shell word that holds a listed path, so a continuation
    after an escaped space or a quote is never left behind, and a prefix-related or a second
    listed path is found independently."""
    if not redactor.enabled:
        return excerpt
    for display in status.listed_paths:
        redactor.names.path(display)
        if status.repo_root:
            redactor.names.alias_path(os.path.join(status.repo_root, display), display)
    return redactor(excerpt)


def _register_status_paths(run: Run, status: RescueStatus, redactor: Redactor) -> None:
    """The same path-N namespace `_register_plan_paths` fills for `rescue
    plan` (docs/DESIGN.md 5.1), so a declared check's own text -- shown
    verbatim on the 'last counted run' line -- is redacted consistently with
    the PATHS table `rescue plan` already prints for the same paths."""
    global_cwd = observe._discover_cwd(run) if status.checked else None
    cwd = observe._discover_cwd(run)
    for row in status.units:
        if row.repo is not None:
            redactor.names.path(row.repo)
    for unit in qualifying_units(run):
        for raw_path in _listed_spellings(unit):
            if status.checked:
                display = _display_key(raw_path, global_cwd, status.repo_root)
            else:
                display = raw_path
            redactor.names.path(display, cwd, resolve_from=raw_path)


def render_status_text(
    run: Run, status: RescueStatus, redactor: Redactor, *, evidence: bool = False,
) -> str:
    report._register_names(run, redactor, detailed=True)
    _register_status_paths(run, status, redactor)
    blocks: list[str] = []
    if evidence and redactor.level == "share":
        blocks.append(report.EXCERPT_NOTE)
    if not status.units:
        if status.unchecked_attempts is not None:
            blocks.append(_unchecked_sentence(status.unchecked_attempts, status.run_id))
        elif status.nothing_to_rescue_ok:
            blocks.append("NOTHING TO RESCUE: every attempt completed.")
        else:
            blocks.append(_NOTHING_YET)
    else:
        for unit in status.units:
            blocks.append("\n".join(_unit_status_lines(unit, status, redactor, evidence=evidence)))
    blocks.append(_FRESHNESS_SENTENCE)
    return "\n".join(blocks) + "\n"


def status_to_json(
    run: Run, status: RescueStatus, redactor: Redactor, *, evidence: bool = False,
) -> dict:
    report._register_names(run, redactor, detailed=True)
    _register_status_paths(run, status, redactor)
    units = []
    for unit in status.units:
        last = unit.last_counted
        units.append({
            "agent": redactor.names.agent(unit.label),
            "repo": redactor.names.path(unit.repo) if unit.repo is not None else None,
            "checks_declared": unit.checks_declared,
            "last_counted_run": None if last is None else {
                "command": redactor.check_text(last.command),
                "completion": last.completion,
                "at": last.ts.isoformat() if last.ts else None,
            },
            "word": _display_word(unit),
            "writes_after": unit.writes_after,
            "write_after_check": unit.writes_after > 0,
            "cycles": unit.cycles,
        })
    # docs/DESIGN.md 5.3b: `category` is the matched rule's fixed spelling (or
    # the word `command`), never transcript text; the quoted excerpt exists
    # only at local and raw with --evidence, the same 60 characters the text
    # shows there.
    quote = evidence and redactor.level != "share"

    def excerpt_part(row) -> dict:
        if not quote:
            return {}
        return {"excerpt": _pseudonymize_excerpt(row.excerpt, status, redactor)[:60]}

    return {
        "schema": SCHEMA_VERSION,
        "disclosure": redactor.level,
        "run": {"run_id": status.run_id},
        "units": units,
        "not_counted": [
            {"line": row.line_no, "reason": row.reason, "category": row.category,
             **excerpt_part(row)}
            for row in status.not_counted
        ],
        "never_matches": [
            {"line": row.line_no, "category": row.category, **excerpt_part(row)}
            for row in status.never_matches
        ],
        "transcript_unknown": status.transcript_unknown,
        "exit_code": status.exit_code,
    }
