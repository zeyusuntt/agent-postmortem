"""Guided rescue: `rescue plan` (a decision packet) and `rescue status`
(what was observed), docs/DESIGN.md 4.11, unit M4.

Both commands are read-only and stateless. Neither writes a file, runs a
check, or touches git beyond what `observe.py` already does. The main
Claude session or the human performs every action (`.reviews/proposals/
2026-09-v0.2-guided-rescue.md` section 3): this module only reads
transcripts, observes repo state through `observe.observe_run`, and prints
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

from . import config as config_module
from . import observe, reader, report
from .models import FACTS_UNKNOWN, Attempt, Finding, LogicalAgent, Run
from .redact import Redactor

SCHEMA_VERSION = 2

# docs/DESIGN.md 4.9: the same outcomes observe.py's own candidate-path rule
# uses -- a unit of work is one whose attempts include at least one of these.
_RISK_OUTCOMES = frozenset({"killed", "failed", "superseded"})

# docs/DESIGN.md 4.11: the NEVER list -- a rescue must never undo with any of
# these, and `rescue status` flags one found in the transcript after the run
# ended.
_NEVER_SUBCOMMANDS = ("checkout", "restore", "reset", "stash", "clean")
_NEVER_LIST_RE = re.compile(r"\bgit\s+(?:" + "|".join(_NEVER_SUBCOMMANDS) + r")\b")

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
    declared_checks: list[str]
    config_warnings: list[str]
    killed_brief: list[dict]
    failed_brief: list[dict]
    already_exists_paths: list[str]
    already_exists_commits: list[str]
    has_unknown_fact: bool
    exit_code: int = 0


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


def _unit_candidate_paths(unit: LogicalAgent) -> tuple[list[str], dict[str, list[Attempt]]]:
    """(order, writers): the same shape observe.py's own _candidate_paths
    builds, scoped to this one unit's risk attempts."""
    order: list[str] = []
    writers: dict[str, list[Attempt]] = {}
    for attempt in _unit_risk_attempts(unit):
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


def _unit_path_rows(
    run: Run, unit: LogicalAgent, observation, global_cwd: str | None,
) -> list[PathRow]:
    order, writers = _unit_candidate_paths(unit)
    rows: list[PathRow] = []
    for raw_path in order:
        writer_attempts = writers[raw_path]
        ordinals = sorted({attempt.ordinal for attempt in writer_attempts})
        later = observe._later_write_observed(run, writer_attempts, raw_path)
        if observation.checked:
            display = _display_key(raw_path, global_cwd, observation.repo_root)
            path_obs = observation.paths.get(display)
            if path_obs is None:
                display = raw_path
                path_obs = observation.paths.get(raw_path)
            if path_obs is not None:
                state, reason = path_obs.state, path_obs.reason
            else:
                state, reason = observe.UNKNOWN, "not found in the observation"
        else:
            display, state, reason = raw_path, observe.UNKNOWN, observation.reason
        rows.append(PathRow(
            raw_path=raw_path, display_key=display, state=state, state_reason=reason,
            attempt_ordinals=ordinals, later_write=later,
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


def _unit_plan(run: Run, unit: LogicalAgent, observation, global_cwd: str | None) -> UnitPlan:
    attempts_sorted = sorted(unit.attempts, key=lambda attempt: attempt.ordinal)
    last_attempt = attempts_sorted[-1]
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


def _already_exists(run: Run) -> tuple[list[str], list[str]]:
    paths: list[str] = []
    commits: list[str] = []
    for attempt in run.attempts:
        facts = attempt.facts
        if facts is None:
            continue
        written = facts.paths_written if isinstance(facts.paths_written, list) else []
        for path in written:
            if path not in paths:
                paths.append(path)
        commit_ids = facts.commits if isinstance(facts.commits, list) else []
        for commit_id in commit_ids:
            if commit_id not in commits:
                commits.append(commit_id)
    return paths, commits


def build_plan(
    run: Run, observation, findings: list[Finding], cfg: config_module.Config, *,
    possibly_live: bool = False,
) -> RescuePlan:
    """The full decision packet's raw facts (docs/DESIGN.md 4.11). `observation`
    is always observe.observe_run(run) -- the caller runs it once and passes
    it in so a test can also pass a hand-built one. `possibly_live` is
    computed by the caller (report.is_possibly_live), since it needs a file
    stat this module has no business doing on its own."""
    units_raw = qualifying_units(run)
    global_cwd = observe._discover_cwd(run) if observation.checked else None
    units = [_unit_plan(run, unit, observation, global_cwd) for unit in units_raw]

    declared_checks: list[str] = []
    if observation.checked and observation.repo_root:
        declared_checks = config_module.checks_for_repo(cfg, observation.repo_root)

    already_raw_paths, already_commits = _already_exists(run)
    already_paths: list[str] = []
    for raw_path in already_raw_paths:
        if observation.checked:
            display = _display_key(raw_path, global_cwd, observation.repo_root)
        else:
            display = raw_path
        if display not in already_paths:
            already_paths.append(display)
    has_unknown = (not observation.checked) or any(
        row.state == observe.UNKNOWN for unit in units for row in unit.paths
    ) or any(_unit_has_unknown_facts(unit) for unit in units_raw)

    nothing_to_rescue = not units
    # docs/DESIGN.md 4.11 (F6 hardening): "every attempt completed" is a
    # claim about a FINISHED run where every attempt's own outcome is
    # 'completed' -- not merely one with no killed/failed/superseded unit,
    # which is also true of an unfinished run, or one whose only attempt is
    # 'unknown' (no transcript at all, so not proven completed either).
    nothing_to_rescue_ok = nothing_to_rescue and run.finished and all(
        attempt.outcome == "completed" for attempt in run.attempts
    )

    if possibly_live:
        exit_code = 4
    elif nothing_to_rescue:
        exit_code = 0 if nothing_to_rescue_ok else 4
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
        nothing_to_rescue_ok=nothing_to_rescue_ok,
        units=units,
        checked=observation.checked,
        observation_reason=observation.reason,
        repo_root=observation.repo_root,
        declared_checks=declared_checks,
        config_warnings=list(cfg.warnings),
        killed_brief=_killed_brief_rows(run),
        failed_brief=_failed_brief_rows(run, findings),
        already_exists_paths=already_paths,
        already_exists_commits=already_commits,
        has_unknown_fact=has_unknown,
        exit_code=exit_code,
    )


# --- text rendering ------------------------------------------------------


_PATH_COL = 27
_STATE_COL = 11
_ATTEMPTS_COL = 10
_PATH_INDENT = " " * 12


def _register_plan_paths(plan: RescuePlan, redactor: Redactor) -> None:
    for unit in plan.units:
        for row in unit.paths:
            redactor.names.path(row.display_key)
        for entry in unit.in_flight:
            if entry.display_key is not None:
                redactor.names.path(entry.display_key)
    for path in plan.already_exists_paths:
        redactor.names.path(path)


def _register_plan_names(run: Run, redactor: Redactor) -> None:
    report._register_names(run, redactor)


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
        return _raw_claim_value(value) if not redactor.enabled else "set"
    if key == "report_path" and isinstance(value, str):
        return redactor.names.path(value)
    return _raw_claim_value(value)


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


def _task_lines(unit: UnitPlan, redactor: Redactor) -> list[str]:
    if unit.task is None:
        return ["  TASK      no prompt recorded"]
    pointer = f"full text: {unit.task.transcript_name}:{unit.task.line_no}"
    if not redactor.enabled:
        quoted = unit.task.text[:300]
        return [f'  TASK      "{quoted}"', f"            {pointer}"]
    return [f"  TASK      {pointer}", f"            prompt length: {unit.task.length} chars"]


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


def _commits_line(unit: UnitPlan) -> str:
    ids = ", ".join(unit.commits) if unit.commits else "0"
    return f"  COMMITS   reported by these attempts: {ids}"


def _in_flight_lines(unit: UnitPlan, redactor: Redactor) -> list[str]:
    if not unit.in_flight:
        return []
    lines = []
    for index, entry in enumerate(unit.in_flight):
        tool = entry.tool or "tool"
        path_display = redactor(entry.display_key) if entry.display_key else "(no path)"
        text = f"{tool} {path_display} (no result recorded)"
        prefix = "  IN FLIGHT at the kill: " if index == 0 else _PATH_INDENT
        lines.append(prefix + text)
    return lines


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
    return lines


def _checks_line(plan: RescuePlan, redactor: Redactor) -> str:
    if plan.declared_checks:
        # docs/DESIGN.md 4.11/5.1 (F7 hardening): a declared check's own
        # text gets its own check-N pseudonym by default -- the whole
        # command hidden, not merely a path embedded inside it.
        joined = "; ".join(redactor.names.check(check) for check in plan.declared_checks)
        return f"  CHECKS    declared for this repo: {joined}"
    if plan.checked and plan.repo_root:
        repo_display = plan.repo_root if not redactor.enabled else "this repository"
        return (
            f'  CHECKS    no checks declared for {repo_display}: add [[repos]] to the '
            "config, see docs"
        )
    return (
        "  CHECKS    no checks declared for this repository: add [[repos]] to the "
        "config, see docs"
    )


def _steps_lines(unit: UnitPlan, plan: RescuePlan, redactor: Redactor) -> list[str]:
    dirty = [row for row in unit.paths if row.state == observe.DIRTY_NOW]
    unchecked_note = ""
    if dirty:
        step_paths = dirty
    elif unit.paths and not plan.checked:
        step_paths = unit.paths
        unchecked_note = "   (repo state not checked; every path written is listed)"
    else:
        step_paths = []

    if not step_paths and plan.checked:
        # Observation ran and nothing the killed attempts wrote is dirty
        # now: there is nothing to inspect, keep, or commit. Say so instead
        # of printing commands with no paths.
        lines = [
            "  STEPS",
            "   1. [look]   no path is dirty now: what the killed attempts wrote is "
            "committed (CLEAN_NOW) or absent (MISSING_NOW); see PATHS",
        ]
        if plan.possibly_live:
            return lines
        if plan.declared_checks:
            lines.append(
                "   2. [decide] run the declared check from the repo root: "
                + "; ".join(redactor.names.check(check) for check in plan.declared_checks)
            )
        else:
            lines.append("   2. [decide] declare a check first")
        lines.append("   3. [decide] relaunch the remaining work")
        return lines

    path_str = " ".join(redactor(row.display_key) for row in step_paths) if step_paths else "<none>"

    lines = [
        "  STEPS",
        f"   1. [look]   git status --short -- {path_str}{unchecked_note}",
        f"   2. [look]   git diff -- {path_str}",
    ]
    # docs/DESIGN.md 4.11 (LIVE rule): a possibly-live run only ever gets the
    # two read-only look steps -- committing, relaunching, or even deciding
    # what to keep is unsafe advice while the run might still be writing to
    # this same repo.
    if plan.possibly_live:
        return lines

    if plan.declared_checks:
        check_text = "run the declared check from the repo root: " + "; ".join(
            redactor.names.check(check) for check in plan.declared_checks
        )
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


def _render_unit_text(unit: UnitPlan, plan: RescuePlan, redactor: Redactor) -> str:
    lines = [_unit_header_line(unit, redactor)]
    lines.extend(_task_lines(unit, redactor))
    lines.append(f"  REPORTED  {_reported_text(unit.reported, redactor)}")
    lines.extend(_paths_lines(unit, redactor))
    lines.append(_commits_line(unit))
    lines.extend(_in_flight_lines(unit, redactor))
    lines.extend(_unknown_lines(unit, plan, redactor))
    lines.append(_checks_line(plan, redactor))
    lines.extend(_steps_lines(unit, plan, redactor))
    lines.append(_never_line())
    return "\n".join(redactor(line) for line in lines)


def _brief_text(plan: RescuePlan, redactor: Redactor) -> str:
    killed_parts = []
    for row in plan.killed_brief:
        label = redactor.names.agent(row["label"])
        time_str = row["time"] or "unknown time"
        silence = row["silence_seconds"]
        silence_str = f"{int(silence)}s" if silence is not None else "unknown"
        killed_parts.append(
            f"{label} attempt {row['ordinal']} at {time_str}, after {silence_str} of silence"
        )
    killed_text = "; ".join(killed_parts) if killed_parts else "none"

    failed_parts = []
    for row in plan.failed_brief:
        label = redactor.names.agent(row["label"])
        cause_phrase = report._cause_phrase(row["cause"])
        piece = f"{label}: {cause_phrase}, retry_now {row['retry_now']}"
        if row["reset_time"]:
            piece += f", resets {row['reset_time']}"
        failed_parts.append(piece)
    failed_text = "; ".join(failed_parts) if failed_parts else "none"

    paths = [redactor(path) for path in plan.already_exists_paths]
    commits = list(plan.already_exists_commits)
    exists_bits = []
    if paths:
        exists_bits.append(f"{len(paths)} paths ({', '.join(paths)})")
    else:
        exists_bits.append("0 paths")
    if commits:
        exists_bits.append(f"{len(commits)} commits ({', '.join(commits)})")
    else:
        exists_bits.append("0 commits")

    lines = [
        "BRIEF FOR A RELAUNCH",
        f"  killed: {killed_text}   failed: {failed_text}",
        f"  already exists: {', '.join(exists_bits)}",
        "  start from what exists; do not redo it",
        "  caveat: resume re-runs downstream agents after a parallel fan-out "
        "(anthropics/claude-code #95076)",
    ]
    return "\n".join(redactor(line) for line in lines)


_LIVE_LINE = (
    "POSSIBLY LIVE: do not modify files in this repo. Confirm with the human "
    "that the run has stopped."
)


def render_plan_text(run: Run, plan: RescuePlan, redactor: Redactor) -> str:
    _register_plan_names(run, redactor)
    _register_plan_paths(plan, redactor)

    blocks: list[str] = []
    if plan.possibly_live:
        blocks.append(_LIVE_LINE)
    if plan.nothing_to_rescue:
        # docs/DESIGN.md 4.11: "every attempt completed" is a claim about a
        # finished run. A possibly-live run with no qualifying unit yet is a
        # different, honest claim -- nothing has needed rescuing SO FAR, not
        # that everything is done -- so it gets only the LIVE line above,
        # never this sentence.
        if not plan.possibly_live:
            if plan.nothing_to_rescue_ok:
                blocks.append("NOTHING TO RESCUE: every attempt completed.")
            else:
                # F6 hardening: an unfinished run (or one with an attempt
                # that is neither completed nor killed/failed/superseded)
                # has no qualifying unit YET, not because it is done.
                blocks.append(
                    "NOTHING TO RESCUE YET: the run has not finished and no "
                    "attempt was killed or failed."
                )
        return "\n".join(blocks) + "\n"

    for unit in plan.units:
        blocks.append(_render_unit_text(unit, plan, redactor))

    if not plan.possibly_live:
        blocks.append(_brief_text(plan, redactor))

    return "\n".join(blocks) + "\n"


# --- JSON rendering -----------------------------------------------------


def _export_path_row(row: PathRow, redactor: Redactor) -> dict:
    return {
        "path": redactor(row.display_key),
        "state": row.state,
        "reason": redactor(row.state_reason) if row.state_reason else None,
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
                exported_claims[key] = value
        return {"type": "dict", "claims": exported_claims}
    return None


def _export_unit(unit: UnitPlan, plan: RescuePlan, redactor: Redactor) -> dict:
    task = None
    if unit.task is not None:
        task = {
            "pointer": f"{unit.task.transcript_name}:{unit.task.line_no}",
            "length": unit.task.length,
            "text": unit.task.text[:300] if not redactor.enabled else None,
        }
    unknown = [_UNKNOWN_SENTENCE]
    if not plan.checked and plan.observation_reason:
        unknown.append(redactor(plan.observation_reason))
    if unit.facts_unknown:
        unknown.append("UNKNOWN facts: transcript missing")

    dirty = [row for row in unit.paths if row.state == observe.DIRTY_NOW]
    step_paths = dirty if dirty else (unit.paths if not plan.checked else [])
    path_str = " ".join(redactor(row.display_key) for row in step_paths) if step_paths else None
    steps = [
        {"grade": "look", "text": "git status", "command":
            f"git status --short -- {path_str}" if path_str else None},
        {"grade": "look", "text": "git diff", "command":
            f"git diff -- {path_str}" if path_str else None},
    ]
    # docs/DESIGN.md 4.11 (LIVE rule): steps 3-6 (decide/commit/relaunch) are
    # never offered for a possibly-live run, in JSON any more than in text.
    if not plan.possibly_live:
        if plan.declared_checks:
            check_command = "; ".join(
                redactor.names.check(check) for check in plan.declared_checks
            )
            step3_text = f"run the declared check from the repo root: {check_command}"
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
        "paths": [_export_path_row(row, redactor) for row in unit.paths],
        "commits": list(unit.commits),
        "in_flight": [
            {"tool": entry.tool, "path": redactor(entry.display_key) if entry.display_key else None}
            for entry in unit.in_flight
        ],
        "unknown": unknown,
        "checks": [redactor.names.check(check) for check in plan.declared_checks],
        "steps": steps,
    }


def plan_to_json(run: Run, plan: RescuePlan, redactor: Redactor) -> dict:
    _register_plan_names(run, redactor)
    _register_plan_paths(plan, redactor)

    killed = [
        {
            "agent": redactor.names.agent(row["label"]),
            "ordinal": row["ordinal"],
            "time": row["time"],
            "silence_seconds": row["silence_seconds"],
        }
        for row in plan.killed_brief
    ]
    failed = [
        {
            "agent": redactor.names.agent(row["label"]),
            "cause": row["cause"],
            "retry_now": row["retry_now"],
            "reset_time": row["reset_time"],
        }
        for row in plan.failed_brief
    ]
    brief = {
        "killed": killed,
        "failed": failed,
        "already_exists": {
            "paths": [redactor(path) for path in plan.already_exists_paths],
            "commits": list(plan.already_exists_commits),
        },
    }

    return {
        "schema": SCHEMA_VERSION,
        "run": {
            "run_id": plan.run_id, "kind": plan.kind, "finished": plan.finished,
            "possibly_live": plan.possibly_live,
        },
        "nothing_to_rescue": plan.nothing_to_rescue,
        "units": [_export_unit(unit, plan, redactor) for unit in plan.units],
        "brief": brief if (not plan.possibly_live and not plan.nothing_to_rescue) else None,
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


def _never_list_match(command: str) -> bool:
    return bool(_NEVER_LIST_RE.search(command))


def _classify_bash_call(call, declared_checks: list[str], repo_root: str | None):
    """(matched_check, reason) for one Bash call already known to be after
    run.ended_at: reason is None when it counts, else one of 'other cwd',
    'compound command', 'backgrounded', 'no result' -- docs/DESIGN.md 4.11's
    exact vocabulary. matched_check is the declared check string this call's
    command contains, or None when it contains none of them at all (the call
    is then irrelevant and never listed)."""
    command = call.command
    if not command:
        return None, None
    matched = None
    for check in declared_checks:
        if check in command:
            matched = check
            break
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

    declared_checks = []
    if observation.checked and observation.repo_root:
        declared_checks = config_module.checks_for_repo(cfg, observation.repo_root)

    counted: list[CountedRun] = []
    not_counted: list[NotCountedRow] = []
    never_matches: list[NeverRow] = []

    for call in relevant_calls:
        if call.tool_name != "Bash" or not call.command:
            continue
        if _never_list_match(call.command):
            never_matches.append(NeverRow(line_no=call.line_no, excerpt=call.command[:400]))
        matched, reason = _classify_bash_call(call, declared_checks, observation.repo_root)
        if matched is None:
            continue
        if reason is None:
            counted.append(CountedRun(
                line_no=call.line_no, ts=call.ts, command=matched, completion=call.completion,
            ))
        else:
            not_counted.append(NotCountedRow(
                line_no=call.line_no, reason=reason, excerpt=call.command[:400],
            ))

    counted.sort(key=lambda c: (c.ts or _MIN_TS, c.line_no))
    last_counted = counted[-1] if counted else None

    # docs/DESIGN.md 4.11 (F3 hardening): after the last counted check run,
    # ANY Bash call whose command is not proven read-only counts as a
    # possible write to the repository -- not only Edit/Write/NotebookEdit
    # to one of this unit's own listed paths (the per-unit loop below still
    # covers that). Skipped when the call already matched a declared check
    # substring (it is then already accounted for, with its own reason,
    # by the loop above) so the same call is never listed twice.
    bash_write_count = 0
    if last_counted is not None:
        last_order = (last_counted.ts or _MIN_TS, last_counted.line_no)
        for call in relevant_calls:
            if call.tool_name != "Bash" or not call.command:
                continue
            call_order = (call.ts or _MIN_TS, call.line_no)
            if call_order <= last_order:
                continue
            matched, _reason = _classify_bash_call(call, declared_checks, observation.repo_root)
            if matched is not None:
                continue
            if _proven_read_only(call.command):
                continue
            bash_write_count += 1
            not_counted.append(NotCountedRow(
                line_no=call.line_no,
                reason="a Bash command that may write ran after the check",
                excerpt=call.command[:400],
            ))

    global_cwd = observe._discover_cwd(run) if observation.checked else None
    units = qualifying_units(run)
    unit_statuses: list[UnitStatus] = []
    all_listed: list[str] = []
    for unit in units:
        order, _writers = _unit_candidate_paths(unit)
        listed_keys = {
            _display_key(raw, global_cwd, observation.repo_root) if observation.checked else raw
            for raw in order
        }
        all_listed.extend(key for key in listed_keys if key not in all_listed)

        if last_counted is None:
            word = "NOT_OBSERVED"
        elif last_counted.completion == "completed_ok":
            word = "EXIT_0_OBSERVED"
        else:
            word = "NONZERO_OBSERVED"

        writes_after = 0
        write_events = []
        for call in relevant_calls:
            if call.tool_name not in ("Edit", "Write", "NotebookEdit"):
                continue
            if call.completion != "completed_ok" or not call.path:
                continue
            if observation.checked:
                key = _display_key(call.path, call.cwd, observation.repo_root)
            else:
                key = call.path
            if key not in listed_keys:
                continue
            write_events.append(call)
            call_order = (call.ts or _MIN_TS, call.line_no)
            last_order = None
            if last_counted is not None:
                last_order = (last_counted.ts or _MIN_TS, last_counted.line_no)
            if last_order is not None and call_order > last_order:
                writes_after += 1

        # F3 hardening: a possible write is a possible write to THE
        # REPOSITORY, not attributable to one unit's own candidate paths --
        # so it counts for every unit alike, the same way never_matches
        # already applies transcript-wide rather than per unit.
        writes_after += bash_write_count

        # cycles: a counted run preceded, since the previous counted run, by
        # at least one observed write to one of this unit's listed paths.
        timeline: list[tuple] = []
        for c in counted:
            timeline.append((c.ts or _MIN_TS, c.line_no, "check", None))
        for call in write_events:
            timeline.append((call.ts or _MIN_TS, call.line_no, "write", None))
        timeline.sort(key=lambda item: (item[0], item[1]))

        cycles = 0
        write_since_last_check = False
        for _ts, _line, kind, _ in timeline:
            if kind == "write":
                write_since_last_check = True
            elif kind == "check":
                if write_since_last_check:
                    cycles += 1
                write_since_last_check = False

        unit_statuses.append(UnitStatus(
            key=unit.key, label=unit.label, checks_declared=len(declared_checks),
            last_counted=last_counted, word=word, writes_after=writes_after, cycles=cycles,
            never_command_observed=bool(never_matches),
        ))

    # docs/DESIGN.md 4.11 exit codes: 3 outranks 4 (the same precedence
    # cli._cmd_run gives a critical finding over a merely-INCOMPLETE status)
    # -- a check that actually failed is the more actionable signal.
    exit_code = 0
    if any(u.word == "NONZERO_OBSERVED" for u in unit_statuses):
        exit_code = 3
    elif transcript_unknown or any(
        u.word == "NOT_OBSERVED" or u.writes_after > 0 for u in unit_statuses
    ):
        exit_code = 4
    # F3 hardening: any command matching the NEVER list forces exit 4
    # regardless of everything else -- even a NONZERO_OBSERVED's exit 3.
    if never_matches:
        exit_code = 4

    return RescueStatus(
        run_id=run.run_id, units=unit_statuses, not_counted=not_counted,
        never_matches=never_matches, transcript_unknown=transcript_unknown,
        checked=observation.checked, repo_root=observation.repo_root,
        exit_code=exit_code, listed_paths=tuple(all_listed),
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


def _unit_status_lines(unit: UnitStatus, status: RescueStatus, redactor: Redactor) -> list[str]:
    agent = redactor.names.agent(unit.label)
    lines = [f"UNIT {agent}   checks declared: {unit.checks_declared}"]
    display_word = _display_word(unit)
    if unit.last_counted is not None:
        exit_word = "0" if unit.last_counted.completion == "completed_ok" else "non-zero"
        time_str = (
            unit.last_counted.ts.astimezone(timezone.utc).strftime("%H:%MZ")
            if unit.last_counted.ts else "unknown"
        )
        lines.append(
            f"  last counted run: {redactor.names.check(unit.last_counted.command)}  "
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
        excerpt = _display_excerpt(row.excerpt, status, redactor)
        lines.append(f"  commands matching the NEVER list: line {row.line_no}: {excerpt}")
    return lines


def _display_excerpt(excerpt: str, status: RescueStatus, redactor: Redactor) -> str:
    """docs/DESIGN.md 4.11/5.1 (F7 hardening): by default, only the
    command's own first word (git, sed, printf) -- the rest of a NEVER-list
    or not-counted command (its flags, its exact subcommand, an embedded
    path or prompt fragment) is private and must not appear in default-mode
    output at all, path-N substitution or not. The full, path-N-substituted
    60-char excerpt is still available, but only under --no-redact."""
    if not redactor.enabled:
        return _pseudonymize_excerpt(excerpt, status, redactor)[:60]
    parts = excerpt.split()
    return parts[0] if parts else ""


def _pseudonymize_excerpt(excerpt: str, status: RescueStatus, redactor: Redactor) -> str:
    """Replace every listed path in a command excerpt, in its absolute and
    repo-relative forms, with the path-N pseudonym the PATHS table uses, so
    a command echoed from the transcript never shows a real path under the
    default redaction. Longest paths first, so a path that is a prefix of
    another is never replaced inside it."""
    if not redactor.enabled:
        return excerpt
    forms: list[tuple[str, str]] = []
    for display in status.listed_paths:
        pseudonym = redactor.names.path(display)
        forms.append((display, pseudonym))
        if status.repo_root:
            forms.append((os.path.join(status.repo_root, display), pseudonym))
    for raw, pseudonym in sorted(forms, key=lambda pair: -len(pair[0])):
        excerpt = excerpt.replace(raw, pseudonym)
    return redactor(excerpt)


def _register_status_paths(run: Run, status: RescueStatus, redactor: Redactor) -> None:
    """The same path-N namespace `_register_plan_paths` fills for `rescue
    plan` (docs/DESIGN.md 5.1), so a declared check's own text -- shown
    verbatim on the 'last counted run' line -- is redacted consistently with
    the PATHS table `rescue plan` already prints for the same paths."""
    global_cwd = observe._discover_cwd(run) if status.checked else None
    for unit in qualifying_units(run):
        order, _writers = _unit_candidate_paths(unit)
        for raw_path in order:
            if status.checked:
                display = _display_key(raw_path, global_cwd, status.repo_root)
            else:
                display = raw_path
            redactor.names.path(display)


def render_status_text(run: Run, status: RescueStatus, redactor: Redactor) -> str:
    report._register_names(run, redactor)
    _register_status_paths(run, status, redactor)
    blocks: list[str] = []
    if not status.units:
        blocks.append("NOTHING TO RESCUE: every attempt completed.")
    else:
        for unit in status.units:
            blocks.append("\n".join(_unit_status_lines(unit, status, redactor)))
    blocks.append(_FRESHNESS_SENTENCE)
    text = "\n".join(blocks) + "\n"
    return "\n".join(redactor(line) for line in text.splitlines()) + "\n"


def status_to_json(run: Run, status: RescueStatus, redactor: Redactor) -> dict:
    report._register_names(run, redactor)
    _register_status_paths(run, status, redactor)
    units = []
    for unit in status.units:
        last = unit.last_counted
        units.append({
            "agent": redactor.names.agent(unit.label),
            "checks_declared": unit.checks_declared,
            "last_counted_run": None if last is None else {
                "command": redactor.names.check(last.command),
                "completion": last.completion,
                "at": last.ts.isoformat() if last.ts else None,
            },
            "word": _display_word(unit),
            "writes_after": unit.writes_after,
            "write_after_check": unit.writes_after > 0,
            "cycles": unit.cycles,
        })
    return {
        "schema": SCHEMA_VERSION,
        "run": {"run_id": status.run_id},
        "units": units,
        "not_counted": [
            {"line": row.line_no, "reason": row.reason,
             "excerpt": _display_excerpt(row.excerpt, status, redactor)}
            for row in status.not_counted
        ],
        "never_matches": [
            {"line": row.line_no,
             "excerpt": _display_excerpt(row.excerpt, status, redactor)}
            for row in status.never_matches
        ],
        "transcript_unknown": status.transcript_unknown,
        "exit_code": status.exit_code,
    }
