"""Render a Run and its Findings as text or JSON.

Verdict-first, small functions. render_bug_report drafts an upstream bug
report from the same Run and Findings render_run_text uses, in the section
order docs/DESIGN.md section 4.4 specifies: Title, Environment, What
happened, Reproduction, Evidence, Suggested fixes.
"""

from __future__ import annotations

import hashlib
import platform
import textwrap
from datetime import datetime, timedelta, timezone

from .detectors.base import attempt_seconds, format_duration
from .models import Attempt, Finding, Run
from .redact import Pseudonyms, Redactor
from .rules import RULES, rules_for

_SEVERITIES = ("critical", "major", "minor", "info")

# docs/DESIGN.md 4.7 (unit M3a): the age computed for the header's "ended ...
# ago" clause, and every "within 72 hours" question WORK AT RISK/WHAT TO DO
# ask, use this instead of datetime.now(...) directly, the same pattern
# detectors/claim_contradiction.py already uses -- a test monkeypatches
# report._now to pin the answer instead of racing the real clock.
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """value, given a UTC tzinfo if it somehow has none (defensive: every
    real reader-produced timestamp is already aware, but a hand-built test
    Run is not required to bother)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _plural(count: int, word: str) -> str:
    return word if count == 1 else f"{word}s"


_STALE_AFTER = timedelta(hours=72)


def _ended_recently(run: Run, now: datetime) -> bool:
    """docs/DESIGN.md 4.7 (unit M3a): the same 72-hour rule observe.py's own
    staleness guard uses (4.9) -- a run with no known end time is treated as
    recent, since nothing says it is old."""
    if run.ended_at is None:
        return True
    return (now - _aware(run.ended_at)) <= _STALE_AFTER


def _ended_within_72h_strict(run: Run, now: datetime) -> bool:
    """unit M3b's NEEDS ATTENTION candidate rules 1(b)/1(c): unlike
    _ended_recently's "an unknown end time counts as recent" convention
    (used everywhere else a run's own report reasons about its own age), a
    candidate rule keyed off a real elapsed age must not fire just because
    the age happens to be unknown -- both rules spell out "ended_at not
    None" explicitly."""
    if run.ended_at is None:
        return False
    return (now - _aware(run.ended_at)) <= _STALE_AFTER

# A fixed sentence for a detector whose findings carry no rule_id, because
# the failure is a runtime defect rather than something a prompt can route
# around (docs/DESIGN.md section 4.3). Used by _bug_suggested_fixes_lines.
_DETECTOR_BUG_SENTENCES = {
    "tool_json_error": (
        "This is a runtime defect, not something a prompt can route around: the "
        "tool's own input failed to parse as JSON. File it upstream with the "
        "evidence below."
    ),
    "classifier_unavailable": (
        "The safety classifier the runtime relies on for Bash was temporarily "
        "unavailable. This is a transient runtime issue, not something the "
        "agent's prompt controls."
    ),
}


def verdict_word(findings: list[Finding], status: str = "COMPLETE") -> str:
    """CLEAN (no major or critical), DEGRADED (major, no critical), FAILED
    (any critical), always suffixed with ' (INCOMPLETE)' when status is
    INCOMPLETE (docs/DESIGN.md 4.6)."""
    severities = {finding.severity for finding in findings}
    if "critical" in severities:
        word = "FAILED"
    elif "major" in severities:
        word = "DEGRADED"
    else:
        word = "CLEAN"
    if status == "INCOMPLETE":
        word = f"{word} (INCOMPLETE)"
    return word


def _register_observation_paths(observation, redactor: Redactor) -> None:
    """docs/DESIGN.md 4.9/5.1: every path key a RepoObservation carries gets
    its own path-N pseudonym, the same "path" namespace report_path claims
    use (5.1) -- a repo-relative path is not itself raw-home or raw-tmp text
    that structural redact() would catch, so without this it would leak by
    default. Called before either the REPO STATE text block or the JSON
    observation export is built, same as _register_names runs before
    everything else."""
    if observation is None:
        return
    for path in observation.paths:
        redactor.names.path(path)


def _register_names(run: Run, redactor: Redactor) -> None:
    """docs/DESIGN.md 5.1: every public renderer entry point builds a fresh
    Pseudonyms map for its own render (so numbering restarts at agent-1
    every time and two reports can never be linked by a repeated pseudonym),
    then registers every child attempt's label, meta description, phase,
    and agent_type before rendering anything. Doing the full pass up front
    -- not lazily, the first time each renderer happens to touch a
    particular attempt -- is what lets Finding.title (redactor.title())
    catch a name that only ever appears embedded in a title sentence, not
    as a structured field of its own. run.main is deliberately excluded:
    its label is the fixed literal 'main', never user text, and 5.3 already
    excludes it from export.

    Also registers every attempt's own report_path claim, if it has one
    (docs/DESIGN.md 5.1, M1 review finding 1): unlike the four name fields
    above, a registered path is substituted everywhere the plain Redactor
    renders text (Redactor.__call__ itself, not only title()), so this pass
    is what lets a report_path embedded in a title or evidence line -- not
    only the structured reported/metrics fields that call
    redactor.names.path() directly at export time -- still get the same
    path-N pseudonym in a text or bug report render, which never calls
    export."""
    redactor.names = Pseudonyms(enabled=redactor.enabled)
    for attempt in run.attempts:
        redactor.names.agent(attempt.label)
        meta = attempt.meta if isinstance(attempt.meta, dict) else {}
        description = meta.get("description")
        if isinstance(description, str):
            redactor.names.description(description, label=attempt.label)
        redactor.names.phase(attempt.phase)
        redactor.names.type(attempt.agent_type)
        reported = attempt.reported
        if isinstance(reported, dict):
            claims = reported.get("claims")
            if isinstance(claims, dict):
                report_path = claims.get("report_path")
                if isinstance(report_path, str) and report_path:
                    redactor.names.path(report_path)


def _register_facts_paths(run: Run, redactor: Redactor) -> None:
    """docs/DESIGN.md 5.1/5.3 (F4 hardening): register every attempt's own
    facts.paths_written, facts.in_flight[].path, and facts.cwds through the
    same "path" namespace report_path and observation paths already share,
    so `_export_attempt`'s plain-Redactor pass (redact_value, same treatment
    as `reported`) substitutes a path-N pseudonym instead of leaving a
    repo-relative path, or an absolute path outside home/tmp, unchanged.

    Deliberately a separate function from `_register_names` above, called
    only by `export_run` (the one place these raw values are actually
    printed, in `run --json`'s facts export): `_register_names` is also
    called directly by rescue.py, which has its own, repo-relative
    "display_key" registration for the very same underlying files
    (`_register_plan_paths`/`_register_status_paths`) -- folding this in
    there too would burn path-N ordinals on the raw absolute form before
    rescue.py ever gets to register its own, preferred relative one, so the
    same file could end up with two different pseudonyms in one rescue
    render."""
    for attempt in run.attempts:
        facts = attempt.facts
        if facts is None:
            continue
        if isinstance(facts.paths_written, list):
            for facts_path in facts.paths_written:
                if isinstance(facts_path, str) and facts_path:
                    redactor.names.path(facts_path)
        if isinstance(facts.in_flight, list):
            for entry in facts.in_flight:
                if isinstance(entry, dict):
                    in_flight_path = entry.get("path")
                    if isinstance(in_flight_path, str) and in_flight_path:
                        redactor.names.path(in_flight_path)
        if isinstance(facts.cwds, list):
            for cwd in facts.cwds:
                if isinstance(cwd, str) and cwd:
                    redactor.names.path(cwd)


def redact_value(value, redactor: Redactor):
    """Recursively apply redactor to every string in a JSON-shaped value."""
    if isinstance(value, str):
        return redactor(value)
    if isinstance(value, list):
        return [redact_value(item, redactor) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item, redactor) for key, item in value.items()}
    return value


def _format_range(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "no timestamps recorded"
    # Convert to UTC first: a timestamp that kept a non-UTC offset would
    # otherwise print its local wall-clock value with a "Z" suffix that
    # falsely claims it is UTC.
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    start_str = start.strftime("%Y-%m-%d %H:%MZ")
    end_str = (
        end.strftime("%H:%MZ") if start.date() == end.date() else end.strftime("%Y-%m-%d %H:%MZ")
    )
    return f"{start_str} to {end_str}"


def silent_retry_count(run: Run) -> int:
    """How many attempts were thrown away by a restart: for every logical
    agent, every attempt but the last one. Attempts with no key (an unkeyed
    'started' journal event) are not part of any LogicalAgent and must never
    be counted here; subtracting len(run.attempts) - len(run.agents) would
    silently attribute one to an agent that was never actually retried."""
    return sum(len(agent.attempts) - 1 for agent in run.agents if agent.attempts)


def _summary_line(run: Run) -> str:
    agent_count = len(run.agents)
    attempt_count = len(run.attempts)
    retries = silent_retry_count(run)
    wasted_seconds = sum(
        attempt_seconds(attempt)
        for agent in run.agents
        for attempt in sorted(agent.attempts, key=lambda a: a.ordinal)[:-1]
    )
    killed = sum(1 for attempt in run.attempts if attempt.outcome == "killed")
    return (
        f"{agent_count} agents became {attempt_count} attempts; "
        f"{retries} silent retries burned {format_duration(wasted_seconds)}; "
        f"{killed} kills recorded as user interrupts"
    )


def _session_summary_line(run: Run) -> str:
    """docs/DESIGN.md 4.7: a session's summary reports subagents, not
    logical agents becoming attempts -- a session's subagents are never
    retried in a way v0.1 can observe, so it says so plainly instead of
    printing a 'silent retries' count that would always read zero."""
    run_seconds = 0.0
    if run.started_at is not None and run.ended_at is not None:
        run_seconds = max((run.ended_at - run.started_at).total_seconds(), 0.0)
    subagents = len(run.attempts)
    interrupted = sum(1 for attempt in run.attempts if attempt.interrupt is not None)
    unattributed = sum(
        1 for attempt in run.attempts
        if attempt.interrupt is not None and attempt.interrupt.cause == "unattributed"
    )
    return (
        f"session ran {format_duration(run_seconds)}; {subagents} subagents, "
        f"{interrupted} interrupted ({unattributed} unattributed); retries not observable"
    )


def _summary_line_for(run: Run) -> str:
    return _session_summary_line(run) if run.kind == "session" else _summary_line(run)


def summary_line(run: Run) -> str:
    """The one-line run summary docs/DESIGN.md 4.7 renders first inside
    render_run_text/render_bug_report: 'N agents became M attempts; ...' for
    a workflow run, 'session ran T; N subagents, ...' for a session. Public
    so a caller outside this module (hooks_cli.py's notification lines,
    docs/DESIGN.md 7.2) reuses the exact same sentence instead of
    re-deriving it."""
    return _summary_line_for(run)


def _project_line(run: Run, redactor: Redactor) -> str:
    project = redactor(run.project_slug)
    version = run.claude_version or "unknown"
    time_range = _format_range(run.started_at, run.ended_at)
    return f"project {project}   claude {version}   {time_range}"


# --- unit M3a: the redesigned `run` text report -----------------------------
#
# docs/DESIGN.md 4.7 (rewritten by this unit): header, account, WORK AT RISK,
# WHAT TO DO, AGENT table, STORIES. The old v0.1 header/summary-line/
# project-line/FINDINGS/SUGGESTED RULES/NEXT ACTION shape is gone from
# render_run_text; render_bug_report keeps the v0.1 section order untouched
# (it has its own _bug_* helpers and never calls anything below).

_AGENT_END_KILLED_CAUSE = {"parent_stop": "stopped by parent", "human": "interrupted"}


def _age_string(ended_at: datetime, now: datetime, short: bool = False) -> str:
    """'<N> days|hours|minutes ago', floor-rounded, for the header's 'ended
    ... ago' clause (docs/DESIGN.md 4.7). short=True (unit M3b's scan triage
    age column) abbreviates the unit ('<N>d/h/m ago') instead of spelling it
    out; the rounding and threshold logic is exactly the same either way, so
    a triage row's age and the run report's header age can never disagree
    about which unit a given duration falls into."""
    seconds = max((now - _aware(ended_at)).total_seconds(), 0.0)
    days = int(seconds // 86400)
    if days >= 1:
        return f"{days}d ago" if short else f"{days} {_plural(days, 'day')} ago"
    hours = int(seconds // 3600)
    if hours >= 1:
        return f"{hours}h ago" if short else f"{hours} {_plural(hours, 'hour')} ago"
    minutes = int(seconds // 60)
    return f"{minutes}m ago" if short else f"{minutes} {_plural(minutes, 'minute')} ago"


def _run_header_line(run: Run, findings: list[Finding], show_age: bool = True) -> str:
    """'RUN <id>  <VERDICT>[ (INCOMPLETE)][ (unfinished)]  ended <ts> (<age>)'
    -- 'SESSION' instead of 'RUN' for a session run (docs/DESIGN.md 4.7 rule
    1). ended_at is never redacted (it carries no user text, same as a run
    id); 'ended: unknown' when it is None."""
    kind_word = "SESSION" if run.kind == "session" else "RUN"
    verdict = verdict_word(findings, run.status)
    unfinished_tag = " (unfinished)" if not run.finished else ""
    if run.ended_at is None:
        ended = "ended: unknown"
    else:
        ended_utc = _aware(run.ended_at).astimezone(timezone.utc)
        ended = f"ended {ended_utc.strftime('%Y-%m-%d %H:%MZ')}"
        if show_age:
            ended += f" ({_age_string(run.ended_at, _now())})"
    return f"{kind_word} {run.run_id}  {verdict}{unfinished_tag}  {ended}"


# docs/DESIGN.md 4.2/4.7: the phrase each attempt_failed cause renders as,
# shared by the account's failed-sentence, WHAT TO DO's relaunch guidance,
# and a story's own attempt_failed clause -- one table instead of three
# copies of the same wording.
_CAUSE_PHRASES = {
    "usage_limit_session": "session usage limit reached",
    "usage_limit_weekly": "weekly usage limit reached",
    "machine_sleep": "the machine went to sleep",
    "api_error": "a server error occurred",
    "interrupted": "interrupted before finishing",
    "unattributed": "cause not recorded in the transcript",
}


def _cause_phrase(cause: str) -> str:
    return _CAUSE_PHRASES.get(cause, cause)


def _workflow_account_lines(run: Run, findings: list[Finding]) -> list[str]:
    """docs/DESIGN.md 4.7 rule 2: 1 to 3(+) plain sentences, verdict already
    said in the header -- this is the 'what happened' account. Any sentence
    whose count is 0 is omitted outright, never printed as '0 ...'."""
    agent_count = len(run.agents)
    attempt_count = len(run.attempts)
    sentences = [
        f"{agent_count} {_plural(agent_count, 'agent')} took "
        f"{attempt_count} {_plural(attempt_count, 'attempt')}."
    ]

    runtime = 0
    parent_stop = 0
    human = 0
    unattributed = 0
    for attempt in run.attempts:
        interrupt = attempt.interrupt
        if interrupt is None:
            continue
        if interrupt.cause == "runtime":
            runtime += 1
        elif interrupt.cause == "parent_stop":
            parent_stop += 1
        elif interrupt.cause == "human":
            human += 1
        elif interrupt.cause == "unattributed":
            unattributed += 1

    if runtime:
        was_were = "was" if runtime == 1 else "were"
        sentences.append(
            f"{runtime} {_plural(runtime, 'attempt')} {was_were} killed by "
            "the runtime and restarted."
        )
    if parent_stop:
        was_were = "was" if parent_stop == 1 else "were"
        sentences.append(f"{parent_stop} {was_were} stopped by the parent session.")
    if human:
        was_were = "was" if human == 1 else "were"
        sentences.append(f"{human} {was_were} interrupted with the parent session.")
    if unattributed:
        was_were = "was" if unattributed == 1 else "were"
        sentences.append(f"{unattributed} {was_were} interrupted, cause unattributed.")

    failed_sentence = _failed_clause(findings)
    if failed_sentence:
        sentences.append(failed_sentence)

    wasted_seconds = 0.0
    for finding in findings:
        if finding.detector == "wasted_time":
            wasted_seconds = finding.metrics.get("wasted_seconds", 0.0) or 0.0
    if wasted_seconds:
        sentences.append(
            f"{format_duration(wasted_seconds)} was spent in attempts that returned no result."
        )

    return sentences


def _failed_clause(findings: list[Finding]) -> str | None:
    """'<F> failed (<causes, comma separated, with counts>).' (docs/
    DESIGN.md 4.7 rule 2), or None when there are no attempt_failed findings
    at all -- true of every session run today (attempt_failed is workflow-
    only), so the session account line simply never grows this clause."""
    failed = [f for f in findings if f.detector == "attempt_failed"]
    if not failed:
        return None
    causes: list[list] = []
    index_by_cause: dict[str, int] = {}
    for finding in failed:
        cause = finding.metrics.get("cause", "unattributed")
        if cause not in index_by_cause:
            index_by_cause[cause] = len(causes)
            causes.append([cause, 0])
        causes[index_by_cause[cause]][1] += 1
    cause_text = ", ".join(f"{_cause_phrase(cause)} ({count})" for cause, count in causes)
    return f"{len(failed)} failed ({cause_text})."


def _session_account_lines(run: Run, findings: list[Finding]) -> list[str]:
    """docs/DESIGN.md 4.7 rule 2: the existing session summary sentence, plus
    the failed sentence when one applies (attempt_failed is workflow-only
    today, so this never actually fires yet -- kept so the two account
    builders share the same failed-clause code path)."""
    sentences = [f"{_session_summary_line(run)}."]
    failed_sentence = _failed_clause(findings)
    if failed_sentence:
        sentences.append(failed_sentence)
    return sentences


def _account_lines(run: Run, findings: list[Finding]) -> list[str]:
    if run.kind == "session":
        return _session_account_lines(run, findings)
    return _workflow_account_lines(run, findings)


# docs/DESIGN.md 4.9/4.7: the outcomes whose facts feed WORK AT RISK, the
# AGENT table's PATHS/COMMITS columns, and a story's own facts clause --
# the same set observe.py's own _CANDIDATE_OUTCOMES already uses.
_RISK_OUTCOMES = frozenset({"killed", "failed", "superseded"})


def _risk_paths_and_commits(run: Run) -> tuple[set[str], set[str]]:
    """The union of paths_written/commits over every killed, failed, or
    superseded attempt of the run (the same population _work_at_risk_lines
    and _what_to_do_items already walk) -- shared by unit M3b's scan triage
    (the NEEDS ATTENTION candidate rules, the paths_at_risk JSON field, and
    the --observe next-command decision) so there is exactly one definition
    of "paths at risk" in this module."""
    qualifying = [a for a in run.attempts if a.outcome in _RISK_OUTCOMES]
    paths: set[str] = set()
    commits: set[str] = set()
    for attempt in qualifying:
        facts = attempt.facts
        if facts is None:
            continue
        if isinstance(facts.paths_written, list):
            paths.update(facts.paths_written)
        if isinstance(facts.commits, list):
            commits.update(facts.commits)
    return paths, commits


def _observation_summary_line(run: Run, observation, redactor: Redactor) -> str:
    """docs/DESIGN.md 4.7 rule 3: WORK AT RISK's optional second line."""
    if observation is not None:
        if observation.checked:
            dirty = sum(1 for p in observation.paths.values() if p.state == "DIRTY_NOW")
            missing = sum(1 for p in observation.paths.values() if p.state == "MISSING_NOW")
            clean = sum(1 for p in observation.paths.values() if p.state == "CLEAN_NOW")
            return f"{dirty} paths dirty now, {missing} missing, {clean} clean"
        reason = redactor(observation.reason) if observation.reason else "not checked"
        return f"repo state not checked ({reason})"
    if _ended_recently(run, _now()):
        return "repo state: run with --observe"
    return "historical: repo state not checked (run older than 72h)"


def _work_at_risk_lines(run: Run, observation, redactor: Redactor) -> list[str]:
    """docs/DESIGN.md 4.7 rule 3."""
    qualifying = [a for a in run.attempts if a.outcome in _RISK_OUTCOMES]
    if not qualifying:
        return ["WORK AT RISK  none: killed or failed attempts wrote no paths."]

    if all(a.facts is None for a in qualifying):
        return ["WORK AT RISK  unknown: transcripts missing."]

    paths: set[str] = set()
    commits: set[str] = set()
    for attempt in qualifying:
        facts = attempt.facts
        if facts is None:
            continue
        if isinstance(facts.paths_written, list):
            paths.update(facts.paths_written)
        if isinstance(facts.commits, list):
            commits.update(facts.commits)

    if not paths:
        return ["WORK AT RISK  none: killed or failed attempts wrote no paths."]

    n = len(qualifying)
    p = len(paths)
    c = len(commits)
    first_line = (
        f"WORK AT RISK  {n} killed or failed {_plural(n, 'attempt')} wrote "
        f"{p} {_plural(p, 'path')}; {c} {_plural(c, 'commit')} reported."
    )
    lines = [first_line]
    lines.append(f"  {_observation_summary_line(run, observation, redactor)}")
    return lines


def _finding_ids_by_detector(findings: list[Finding], detector: str) -> list[Finding]:
    return [f for f in findings if f.detector == detector]


def _what_to_do_items(
    run: Run, findings: list[Finding], observation, observe_given: bool, hidden_count: int,
) -> list[dict]:
    """docs/DESIGN.md 4.7 rule 4: deterministic, at most 3 items, each
    {'grade': 'look'|'decide', 'text': str, 'command': str|None}."""
    items: list[dict] = []

    qualifying = [a for a in run.attempts if a.outcome in _RISK_OUTCOMES]
    any_known_paths = any(
        a.facts is not None
        and isinstance(a.facts.paths_written, list)
        and a.facts.paths_written
        for a in qualifying
    )
    if any_known_paths and not observe_given and _ended_recently(run, _now()):
        items.append({
            "grade": "look",
            "text": "see which paths are dirty",
            "command": f"agent-postmortem run {run.run_id} --observe",
        })

    failed = _finding_ids_by_detector(findings, "attempt_failed")
    wait_item = None
    safe_item = None
    for finding in failed:
        if finding.severity != "major":
            continue  # the key was already retried to completion; nothing to decide
        retry_now = finding.metrics.get("retry_now")
        reset_time = finding.metrics.get("reset_time")
        if wait_item is None and retry_now == "no" and reset_time:
            wait_item = {
                "grade": "decide",
                "text": f"wait until {reset_time} before relaunching",
                "command": None,
            }
        if safe_item is None and retry_now == "yes":
            cause = finding.metrics.get("cause", "unattributed")
            safe_item = {
                "grade": "decide",
                "text": f"relaunch is safe now: {_cause_phrase(cause)}",
                "command": None,
            }
    if len(items) < 3:
        if wait_item is not None:
            items.append(wait_item)
        elif safe_item is not None:
            items.append(safe_item)

    if len(items) < 3:
        # docs/DESIGN.md 4.7 rule 4c / 9 (unit M6): `run` never scans other
        # runs to learn a real r-of-R rate, so this always points at the
        # fleet-wide proposal command instead of naming this one run --
        # `rules --run <id>` still exists, just no longer what `run` prints.
        backing_rule_ids = {
            rule_id
            for finding in findings
            if finding.severity in ("major", "critical")
            for rule_id in finding.rule_ids
        }
        if backing_rule_ids:
            items.append({
                "grade": "decide",
                "text": "rules that would have prevented this",
                "command": "agent-postmortem rules propose",
            })

    if len(items) < 3 and hidden_count > 0:
        items.append({
            "grade": "look",
            "text": "see the findings hidden here",
            "command": f"agent-postmortem run {run.run_id} --verbose",
        })

    return items[:3]


def _what_to_do_content(item: dict) -> str:
    grade, text, command = item["grade"], item["text"], item["command"]
    if grade == "look":
        return f"{command}   {text}"
    if command:
        return f"{text}: {command}"
    return text


def _what_to_do_lines(items: list[dict]) -> list[str]:
    lines = ["WHAT TO DO"]
    for index, item in enumerate(items, start=1):
        tag_field = f"[{item['grade']}]"
        lines.append(f"  {index}. {tag_field:<9}{_what_to_do_content(item)}")
    return lines


def _agent_end(final: Attempt | None) -> str:
    """docs/DESIGN.md 4.7 rule 5: 'completed', 'killed', 'failed', 'stopped
    by parent', 'interrupted', 'superseded', or 'unknown', derived from the
    final attempt's own outcome and interrupt cause."""
    if final is None:
        return "unknown"
    if final.outcome != "killed":
        return final.outcome
    cause = final.interrupt.cause if final.interrupt is not None else None
    return _AGENT_END_KILLED_CAUSE.get(cause, "killed")


def _agent_facts_totals(agent) -> tuple[str, int]:
    """(paths display, commit count) for the AGENT table's PATHS/COMMITS
    columns and a story's own facts clause: the union of paths_written/
    commits over every attempt of this logical agent. PATHS shows '?' when
    no attempt's facts could be established at all (docs/DESIGN.md 4.7 rule
    5); COMMITS has no such marker and simply counts what is known."""
    known = False
    paths: set[str] = set()
    commits: set[str] = set()
    for attempt in agent.attempts:
        facts = attempt.facts
        if facts is None:
            continue
        if isinstance(facts.paths_written, list):
            known = True
            paths.update(facts.paths_written)
        if isinstance(facts.commits, list):
            commits.update(facts.commits)
    return (str(len(paths)) if known else "?"), len(commits)


def _agent_table(run: Run, redactor: Redactor) -> list[str]:
    """docs/DESIGN.md 4.7 rule 5: AGENT, [PHASE], ATTEMPTS, END, KILLED,
    PATHS, COMMITS, WASTED. PHASE is kept only when at least one logical
    agent of this run actually carries one (a session's subagents never
    do)."""
    show_phase = any(agent.phase for agent in run.agents)
    header = f"{'AGENT':<10}"
    if show_phase:
        header += f"{'PHASE':<11}"
    header += f"{'ATTEMPTS':<10}{'END':<19}{'KILLED':<8}{'PATHS':<7}{'COMMITS':<9}WASTED"
    lines = [header]

    for agent in run.agents:
        attempts = sorted(agent.attempts, key=lambda a: a.ordinal)
        final = attempts[-1] if attempts else None
        wasted_seconds = sum(attempt_seconds(a) for a in attempts[:-1])
        killed = sum(1 for a in attempts if a.interrupt is not None)
        paths_display, commits_count = _agent_facts_totals(agent)

        row = f"{redactor.names.agent(agent.label):<10}"
        if show_phase:
            row += f"{redactor.names.phase(agent.phase):<11}"
        row += (
            f"{len(attempts):<10}{_agent_end(final):<19}{killed:<8}"
            f"{paths_display:<7}{commits_count:<9}{format_duration(wasted_seconds)}"
        )
        lines.append(row)
    return lines


def _repo_state_section(observation, redactor: Redactor) -> list[str]:
    """docs/DESIGN.md 4.9 rule 10: checked/reason, then per path a path-N
    pseudonym by default (the real repo-relative path under --no-redact),
    state, later write yes/no, and the ordinals of every attempt that wrote
    it. Only ever called when the caller actually ran --observe (observation
    is not None); render_run_text checks that before calling this."""
    lines = ["REPO STATE"]
    if not observation.checked:
        reason = redactor(observation.reason) if observation.reason else "not checked"
        lines.append(f"  checked: no ({reason})")
        return lines
    lines.append("  checked: yes")
    if not observation.paths:
        lines.append("  (no paths)")
        return lines
    for path, path_observation in observation.paths.items():
        display = redactor.names.path(path)
        state = path_observation.state
        if path_observation.reason:
            state = f"{state} ({redactor(path_observation.reason)})"
        later = "yes" if path_observation.later_write_observed else "no"
        ordinals = ", ".join(str(entry["ordinal"]) for entry in path_observation.attempts)
        lines.append(f"  {display}  {state}  later write: {later}  attempts: {ordinals}")
    return lines


def _findings_section(findings: list[Finding], redactor: Redactor) -> list[str]:
    counts = {severity: 0 for severity in _SEVERITIES}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1

    lines = [
        f"FINDINGS (critical {counts['critical']}, major {counts['major']}, "
        f"minor {counts['minor']})"
    ]
    for finding in findings:
        lines.append(f"[{finding.severity}] {finding.detector}  {redactor.title(finding.title)}")
        for evidence in finding.evidence:
            lines.append(f"  {redactor(evidence)}")
    return lines


_TOOLING_STORY_PHRASES = {
    "bash_backgrounded": "Bash commands backgrounded",
    "bash_timeout": "Bash commands timed out",
    "tool_json_error": "tool input JSON errors",
    "structured_output_failure": "structured output failures",
    "codex_write_denied": "sandbox write denials",
    "classifier_unavailable": "classifier outages",
    "git_cwd_mismatch": "git cwd mismatches",
}

_CLAIM_CLAUSE_PHRASES = {
    "ok_false": "ok is false",
    "ready_false": "ready is false",
    "report_path_missing": "a report_path that does not exist",
}


def _claim_contradiction_phrases(metrics: dict) -> list[str]:
    """The human phrases backing a claim_contradiction finding's clauses
    (docs/DESIGN.md 4.7 rule 6), rebuilt from Finding.metrics (clause ids
    plus the claims dict) rather than re-parsing the finding's own title --
    the same information the detector used to build its title in the first
    place (detectors/claim_contradiction.py's _clauses)."""
    claims = metrics.get("claims") if isinstance(metrics.get("claims"), dict) else {}
    clause_ids = metrics.get("clauses") or []
    phrases = []
    for clause_id in clause_ids:
        if clause_id == "tests_failed":
            n = claims.get("tests_failed", 0)
            phrases.append(f"{n} failed {_plural(n, 'test')}")
        elif clause_id == "suite_failed":
            n = claims.get("suite_failed", 0)
            phrases.append(f"{n} failed suite {_plural(n, 'check')}")
        elif clause_id == "blockers":
            n = claims.get("blockers_count", 0)
            phrases.append(f"{n} {_plural(n, 'blocker')}")
        else:
            phrases.append(_CLAIM_CLAUSE_PHRASES.get(clause_id, clause_id))
    return phrases or ["a contradiction"]


def _finding_ordinal(finding: Finding, attempts_sorted: list[Attempt]) -> int:
    for attempt in attempts_sorted:
        if attempt.agent_id == finding.agent_id:
            return attempt.ordinal
    return 1


def _agent_findings_pairs(
    run: Run, findings: list[Finding],
) -> tuple[list[tuple], list[Finding]]:
    """[(logical agent, its findings)] for every agent of the run, in
    run.agents order, plus the findings with no agent_id at all (docs/
    DESIGN.md 4.7 rule 6: these form the one 'run' story). A finding whose
    agent_id names no attempt of this run (should not happen in practice) is
    folded into the run-level list too, rather than silently dropped."""
    owner_by_agent_id: dict[str, object] = {}
    for agent in run.agents:
        for attempt in agent.attempts:
            owner_by_agent_id[attempt.agent_id] = agent
    findings_by_agent: dict[int, list[Finding]] = {id(agent): [] for agent in run.agents}
    run_level: list[Finding] = []
    for finding in findings:
        owner = owner_by_agent_id.get(finding.agent_id) if finding.agent_id else None
        if owner is None:
            run_level.append(finding)
        else:
            findings_by_agent[id(owner)].append(finding)
    pairs = [(agent, findings_by_agent[id(agent)]) for agent in run.agents]
    return pairs, run_level


def _agent_story_text(agent, agent_findings: list[Finding]) -> str:
    """docs/DESIGN.md 4.7 rule 6: one deterministic sentence per clause type
    present, in a fixed order, joined into a paragraph. Not yet redacted or
    wrapped -- the caller does both, once, the same way for text and JSON."""
    by_detector: dict[str, list[Finding]] = {}
    for finding in agent_findings:
        by_detector.setdefault(finding.detector, []).append(finding)

    attempts_sorted = sorted(agent.attempts, key=lambda a: a.ordinal)
    non_final = attempts_sorted[:-1]
    final = attempts_sorted[-1] if attempts_sorted else None
    mislabeled_by_agent_id = {f.agent_id: f for f in by_detector.get("mislabeled_kill", [])}

    clauses: list[str] = []

    lead = None
    silent_retry_findings = by_detector.get("silent_retry", [])
    if silent_retry_findings:
        k = silent_retry_findings[0].metrics.get("attempts", len(attempts_sorted)) - 1
        k = max(k, 0)
        silences = []
        all_runtime = bool(non_final)
        for attempt in non_final:
            mf = mislabeled_by_agent_id.get(attempt.agent_id)
            if mf is not None and mf.metrics.get("cause") == "runtime":
                silence = mf.metrics.get("silence_seconds")
                silences.append(f"{int(round(silence))}s" if silence is not None else "unknown")
            else:
                all_runtime = False
        if all_runtime and silences:
            silence_text = ", ".join(silences)
            lead = f"killed {k} {_plural(k, 'time')} by the runtime ({silence_text} of silence)"
        else:
            lead = f"restarted {k} {_plural(k, 'time')}"

    parent_stop = False
    if final is not None:
        mf = mislabeled_by_agent_id.get(final.agent_id)
        parent_stop = mf is not None and mf.metrics.get("cause") == "parent_stop"

    if lead is not None and parent_stop:
        clauses.append(f"{lead}, then stopped by the parent")
    elif lead is not None:
        clauses.append(lead)
    elif parent_stop:
        clauses.append("stopped by the parent")

    for finding in by_detector.get("attempt_failed", []):
        ordinal = _finding_ordinal(finding, attempts_sorted)
        phrase = _cause_phrase(finding.metrics.get("cause", "unattributed"))
        reset_time = finding.metrics.get("reset_time")
        text = f"attempt {ordinal} failed: {phrase}"
        if reset_time:
            text += f", resets {reset_time}"
        clauses.append(text)

    for finding in by_detector.get("claim_contradiction", []):
        ordinal = _finding_ordinal(finding, attempts_sorted)
        phrases = _claim_contradiction_phrases(finding.metrics)
        clauses.append(f"attempt {ordinal} completed but reported {'; '.join(phrases)}")

    paths_display, commits_count = _agent_facts_totals(agent)
    facts_known = paths_display != "?"
    paths_count = 0 if not facts_known else int(paths_display)
    was_killed = final is not None and final.outcome == "killed"
    if paths_count or commits_count:
        clauses.append(
            f"Wrote {paths_count} {_plural(paths_count, 'path')}, "
            f"reported {commits_count} {_plural(commits_count, 'commit')}."
        )
    elif facts_known and was_killed:
        clauses.append("Wrote no paths.")

    tooling_bits = []
    for detector_id, phrase in _TOOLING_STORY_PHRASES.items():
        for finding in by_detector.get(detector_id, []):
            count = finding.metrics.get("count", 1)
            tooling_bits.append(f"{count} {phrase}")
    if tooling_bits:
        clauses.append("; ".join(tooling_bits))

    if not clauses and agent_findings:
        clauses.append(agent_findings[0].title)

    return _sentences(clauses)


def _sentences(clauses: list[str]) -> str:
    """Join clauses as sentences: each starts with a capital and the whole
    ends with one period. Finding titles start lowercase by convention
    (docs/DESIGN.md 4.3), so this is where a story gets its capitals."""
    parts = [clause.rstrip(".") for clause in clauses if clause]
    text = ". ".join(c[:1].upper() + c[1:] for c in parts)
    return f"{text}." if text else ""


def _story_records(run: Run, findings: list[Finding], verbose: bool) -> list[dict]:
    """docs/DESIGN.md 4.7 rule 6: one record per logical agent that earns a
    story (at least one non-info finding, or --verbose with any finding at
    all), worst severity first then by (pre-pseudonym) label, plus one
    trailing 'run' record for findings with no agent_id. Each record is
    {'agent': label_or_"run", 'severity': str, 'text': str,
    'finding_ids': [...], 'findings': [...]} -- 'findings' is
    report-internal (kept for evidence lookups) and is stripped before this
    goes anywhere public (text rendering, JSON export)."""
    pairs, run_level = _agent_findings_pairs(run, findings)

    entries = []
    for agent, agent_findings in pairs:
        if not agent_findings:
            continue
        non_info = [f for f in agent_findings if f.severity != "info"]
        if not non_info and not verbose:
            continue
        relevant = agent_findings if verbose else non_info
        rank = min(_SEVERITIES.index(f.severity) for f in relevant)
        entries.append({
            "agent": agent.label,
            "severity": _SEVERITIES[rank],
            "text": _agent_story_text(agent, relevant),
            "finding_ids": [f.finding_id for f in relevant],
            "findings": relevant,
            "_rank": rank,
        })
    # Worst severity first; ties broken by wasted time (biggest loss first),
    # then by the agent's position in the run, which is the order the AGENT
    # table and the agent-N pseudonyms follow. Sorting by the raw label would
    # look random once the redactor has replaced it.
    position = {agent.label: index for index, agent in enumerate(run.agents)}

    def wasted(entry: dict) -> float:
        return sum(
            (f.metrics.get("wasted_seconds", 0) or 0)
            for f in entry["findings"] if f.detector == "silent_retry"
        )

    entries.sort(
        key=lambda entry: (entry["_rank"], -wasted(entry), position.get(entry["agent"], 1 << 30))
    )
    for entry in entries:
        del entry["_rank"]

    run_relevant = [f for f in run_level if verbose or f.severity != "info"]
    if run_relevant:
        rank = min(_SEVERITIES.index(f.severity) for f in run_relevant)
        text = _sentences([f.title for f in run_relevant])
        entries.append({
            "agent": "run",
            "severity": _SEVERITIES[rank],
            "text": text,
            "finding_ids": [f.finding_id for f in run_relevant],
            "findings": run_relevant,
        })

    return entries


def _wrap_story(label: str, text: str) -> list[str]:
    """Story text wrapped at 78 columns, at most 2 lines (docs/DESIGN.md 4.7
    rule 8), the first line prefixed by the (already pseudonymized/redacted)
    label, continuation lines indented to the same column."""
    indent = " " * (len(label) + 2)
    width = max(78 - len(indent), 20)
    wrapped = textwrap.wrap(text, width=width) or [text]
    wrapped = wrapped[:2]
    lines = [f"  {label}  {wrapped[0]}"]
    for extra in wrapped[1:]:
        lines.append(f"{indent}{extra}")
    return lines


def _stories_section(
    run: Run, findings: list[Finding], redactor: Redactor, verbose: bool, evidence: bool,
) -> list[str]:
    entries = _story_records(run, findings, verbose)
    lines = [f"STORIES ({len(entries)})   details: --verbose   evidence: --evidence"]
    for entry in entries:
        label = "run" if entry["agent"] == "run" else redactor.names.agent(entry["agent"])
        text = redactor.title(entry["text"])
        lines.extend(_wrap_story(label, text))
        if evidence:
            shown = 0
            for finding in entry["findings"]:
                for line in finding.evidence:
                    if shown >= 2:
                        break
                    lines.append(f"    {redactor(line)}")
                    shown += 1
                if shown >= 2:
                    break
    return lines, entries


def _silenced_detectors_line(silenced_detectors: frozenset) -> str | None:
    """docs/DESIGN.md 4.7/4.10: the last line of `run`'s and `scan`'s text
    output, printed whenever [detectors].off silences at least one detector
    -- a config-level fact, independent of whether that detector actually
    fired on this particular run or scan."""
    if not silenced_detectors:
        return None
    ids = ", ".join(sorted(silenced_detectors))
    n = len(silenced_detectors)
    return f"{n} {_plural(n, 'detector')} silenced by config: {ids}"


def render_run_text(
    run: Run,
    findings: list[Finding],
    redactor: Redactor,
    observation=None,
    verbose: bool = False,
    evidence: bool = False,
    show_age: bool = True,
    silenced_detectors: frozenset = frozenset(),
) -> str:
    """docs/DESIGN.md 4.7 (rewritten by unit M3a): header, account, WORK AT
    RISK, WHAT TO DO, AGENT table, [REPO STATE], STORIES, then either the
    hidden-info-count line or (--verbose) the full v0.1 FINDINGS list, then
    (unit M3c, 4.10) a last line naming every detector [detectors].off
    silenced, when there is at least one.
    show_age=False drops the header's "(N days ago)", which depends on the
    wall clock; `demo` uses it so README's byte-compared demo block stays
    stable from one day to the next.

    `silenced_detectors` (docs/DESIGN.md 4.6/4.7): findings from these
    detector ids are dropped before anything else in this function looks at
    `findings` -- the header's verdict, the account sentences, WORK AT RISK,
    WHAT TO DO, the AGENT table's story-adjacent totals, STORIES, and the
    --verbose FINDINGS list all see only the shown (non-silenced) findings,
    exactly like a caller that filtered before calling ever existed. The
    caller (config.partition_silenced) still has the full list for --json."""
    findings = [f for f in findings if f.detector not in silenced_detectors]

    _register_names(run, redactor)
    _register_observation_paths(observation, redactor)

    info_count = sum(1 for finding in findings if finding.severity == "info")
    hidden_count = 0 if verbose else info_count

    lines = [_run_header_line(run, findings, show_age)]
    lines.extend(redactor(sentence) for sentence in _account_lines(run, findings))
    lines.append("")
    lines.extend(redactor(line) for line in _work_at_risk_lines(run, observation, redactor))
    lines.append("")
    items = _what_to_do_items(run, findings, observation, observation is not None, hidden_count)
    lines.extend(redactor(line) for line in _what_to_do_lines(items))
    lines.append("")
    lines.extend(_agent_table(run, redactor))
    lines.append("")
    # docs/DESIGN.md 4.9 rule 10: the REPO STATE block appears only when the
    # caller actually ran --observe; without it, text output carries no
    # trace of observation at all.
    if observation is not None:
        lines.extend(_repo_state_section(observation, redactor))
        lines.append("")
    stories_lines, _entries = _stories_section(run, findings, redactor, verbose, evidence)
    lines.extend(stories_lines)

    if verbose:
        lines.append("")
        lines.extend(_findings_section(findings, redactor))
    elif hidden_count:
        lines.append("")
        hidden_word = _plural(hidden_count, "finding")
        lines.append(f"{hidden_count} info {hidden_word} not shown (--verbose)")

    silenced_line = _silenced_detectors_line(silenced_detectors)
    if silenced_line is not None:
        lines.append(silenced_line)

    return "\n".join(lines).rstrip("\n") + "\n"


# --- unit M3b: scan triage (NEEDS ATTENTION is the default; --all keeps ---
# --- the row-per-run list above/below unchanged) ---------------------------
#
# A run needing attention is ranked, not merely listed: possibly-live runs
# first, then runs whose killed/failed attempts left the most paths behind,
# then by verdict, then by wasted time, then by recency. Every function here
# is pure (no filesystem access): the CLI stats a run's own files once and
# hands the result in as `newest_mtime`, the same "inject the clock" pattern
# _now()/_age_string already use for the wall clock.

_VERDICT_RANK = {"FAILED": 0, "DEGRADED": 1, "CLEAN": 2}


def _verdict_bare(findings: list[Finding], status: str) -> str:
    """verdict_word() without its trailing ' (INCOMPLETE)' suffix, for a
    ranking or a candidate rule that only cares about FAILED/DEGRADED/CLEAN
    itself, never whether the run's own inspection was complete."""
    return verdict_word(findings, status).split(" ", 1)[0]


def is_possibly_live(
    run: Run, newest_mtime: float | None, now: datetime, quiet_seconds: float,
) -> bool:
    """unit M3b / docs/DESIGN.md 7.2's UNKNOWN_QUIET_SECONDS: the best a cold
    `scan` can say about liveness without hook state or a live task list --
    a workflow that has not finished and was touched inside the quiet window
    might still be running. Never true for a session (docs/DESIGN.md: a
    session run is always finished) or a workflow scan already knows has
    finished. `newest_mtime` is None when no file could be statted at all
    (already gone by the time this runs), which is never "recently touched"."""
    if run.finished or run.kind != "workflow":
        return False
    if newest_mtime is None:
        return False
    return (now.timestamp() - newest_mtime) < quiet_seconds


def scan_age_string(run: Run, now: datetime, live: bool) -> str:
    """The triage age column and the JSON `age` field: 'possibly live' when
    live, else the abbreviated '<N>d/h/m ago' built from ended_at, or
    'unknown' when ended_at itself is unknown (never None -- age is always a
    str)."""
    if live:
        return "possibly live"
    if run.ended_at is None:
        return "unknown"
    return _age_string(run.ended_at, now, short=True)


def scan_paths_at_risk(run: Run) -> int:
    """The JSON `paths_at_risk` field and the ranking key's own path count:
    the number of distinct paths written by a killed, failed, or superseded
    attempt of this run."""
    paths, _commits = _risk_paths_and_commits(run)
    return len(paths)


def unfinished_state(run: Run, live: bool) -> str | None:
    """unit M3b rule 7: 'possibly live' | 'failed' | 'abandoned' | None.

    None when every logical agent's own last attempt completed (a workflow
    that genuinely finished cleanly has nothing to explain here, whatever
    Run.finished says about journal bookkeeping) or this is not a workflow
    run at all. Otherwise: 'failed' when every one of those not-completed
    keys' last attempt is itself outcome 'failed' -- which can only ever
    come from the journal's own 'failed' event (docs/DESIGN.md 4.2), never
    from a still-open attempt -- so a run in this bucket really did run
    every retry to a real, journaled failure, not merely stop responding;
    'abandoned' otherwise (some key's last attempt is open, killed,
    superseded, or unknown -- the journal never got a final word on it)."""
    if live:
        return "possibly live"
    if run.kind != "workflow":
        return None
    last_outcomes = []
    for agent in run.agents:
        attempts_sorted = sorted(agent.attempts, key=lambda a: a.ordinal)
        if attempts_sorted:
            last_outcomes.append(attempts_sorted[-1].outcome)
    not_completed = [outcome for outcome in last_outcomes if outcome != "completed"]
    if not not_completed:
        return None
    if all(outcome == "failed" for outcome in not_completed):
        return "failed"
    return "abandoned"


def is_attention_candidate(run: Run, findings: list[Finding], now: datetime, live: bool) -> bool:
    """unit M3b rule 1: a of a-d, first match wins (they are not mutually
    exclusive, but only one needs to fire)."""
    if live:  # (a) possibly live
        return True
    verdict = _verdict_bare(findings, run.status)
    if verdict == "FAILED":  # (d) verdict FAILED regardless of age
        return True
    if _ended_within_72h_strict(run, now):
        if verdict == "DEGRADED":  # (b) ended recently, FAILED already handled above
            return True
        paths, _commits = _risk_paths_and_commits(run)
        if paths:  # (c) ended recently, a killed/failed/superseded attempt wrote a path
            return True
    return False


def _scan_top_detector(findings: list[Finding]) -> str | None:
    """The same rule cli._top_detector applies (docs/DESIGN.md 4.7's unit
    M1d: an info-only finding, present on every run, is never the "top"
    detector), duplicated here rather than imported so this module never
    depends on cli.py."""
    counts: dict[str, int] = {}
    for finding in findings:
        if finding.severity not in ("major", "critical"):
            continue
        counts[finding.detector] = counts.get(finding.detector, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda detector_id: counts[detector_id])


def attention_reason(run: Run, findings: list[Finding]) -> str:
    """unit M3b rule 3: the one-line NEEDS ATTENTION reason, deterministic,
    first that applies."""
    failed = [f for f in findings if f.detector == "attempt_failed"]
    if failed:
        k = len(failed)
        causes: list[str] = []
        for finding in failed:
            phrase = _cause_phrase(finding.metrics.get("cause", "unattributed"))
            if phrase not in causes:
                causes.append(phrase)
        text = f"{k} {_plural(k, 'attempt')} failed: {', '.join(causes)}"
        reset_time = next(
            (f.metrics.get("reset_time") for f in failed if f.metrics.get("reset_time")), None,
        )
        if reset_time:
            text += f", resets {reset_time}"
        return text

    runtime_kills = [
        f for f in findings
        if f.detector == "mislabeled_kill" and f.metrics.get("cause") == "runtime"
    ]
    if runtime_kills:
        k = len(runtime_kills)
        paths, commits = _risk_paths_and_commits(run)
        return (
            f"{k} {_plural(k, 'attempt')} killed by the runtime, "
            f"{len(paths)} {_plural(len(paths), 'path')} written, "
            f"{len(commits)} {_plural(len(commits), 'commit')}"
        )

    top = _scan_top_detector(findings)
    if top is not None:
        count = sum(
            1 for f in findings if f.detector == top and f.severity in ("major", "critical")
        )
        return f"{top} x{count}"

    if run.kind == "session":
        interrupted = sum(1 for attempt in run.attempts if attempt.interrupt is not None)
        if interrupted:
            return f"{interrupted} {_plural(interrupted, 'subagent')} interrupted"

    return "clean"


def attention_next_command(run: Run, now: datetime) -> str:
    """unit M3b rule 3's second line: --observe only when a killed or
    failed attempt is known to have written a path and the run is recent
    enough that a repo might still show it -- the same "unknown end counts
    as recent" convention _what_to_do_items already uses for this exact
    decision on the `run` report."""
    paths, _commits = _risk_paths_and_commits(run)
    if paths and _ended_recently(run, now):
        return f"agent-postmortem run {run.run_id} --observe"
    return f"agent-postmortem run {run.run_id}"


def attention_sort_key(
    run: Run, findings: list[Finding], live: bool, paths_at_risk: int, now: datetime,
) -> tuple:
    """unit M3b rule 1's ranking key, ascending: possibly live first, then
    runs that ended within 72 hours (a rescue is only possible while the
    working tree still holds the work), then paths at risk descending, then
    verdict (FAILED, DEGRADED, CLEAN), then wasted seconds descending, then
    ended_at descending (unknown last)."""
    recent = _ended_within_72h_strict(run, now)
    verdict_rank = _VERDICT_RANK.get(_verdict_bare(findings, run.status), 3)
    wasted_seconds = 0.0
    for finding in findings:
        if finding.detector == "wasted_time":
            wasted_seconds = finding.metrics.get("wasted_seconds", 0.0) or 0.0
    ended_ts = _aware(run.ended_at).timestamp() if run.ended_at is not None else float("-inf")
    return (
        0 if live else 1, 0 if recent else 1, -paths_at_risk, verdict_rank,
        -wasted_seconds, -ended_ts,
    )


def _scan_triage_totals_line(label: str, totals: dict) -> str:
    """One LAST 7 DAYS / ALL TIME line (unit M3b rule 5): label padded to
    14 columns, then the same workflow/session numbers _scan_totals_lines
    prints, condensed onto one line with format_duration instead of a
    '%.1fh' float -- wasted_hours is recovered back to seconds rather than
    threading a parallel seconds accumulator through _cmd_scan, since
    totals['workflow'] is also the JSON payload and test_cli_smoke.py's
    test_scan_totals_are_stratified_by_kind pins its exact key set."""
    workflow = totals.get("workflow", {})
    session = totals.get("session", {})
    wasted_seconds = workflow.get("wasted_hours", 0.0) * 3600.0
    return (
        f"{label:<14}{workflow.get('runs', 0)} workflow runs, "
        f"{workflow.get('runs_with_retries', 0)} with retries, "
        f"{format_duration(wasted_seconds)} wasted; "
        f"{session.get('runs', 0)} session runs, "
        f"{session.get('interrupted', 0)} subagents interrupted"
    )


def render_scan_triage_text(
    shown_candidates: list[dict],
    total_candidates: int,
    total_runs: int,
    totals_7d: dict,
    totals_all: dict,
    redactor: Redactor,
    silenced_detectors: frozenset = frozenset(),
) -> str:
    """unit M3b: the new default `scan` text output. `shown_candidates` is
    already sorted and already truncated to --limit by the caller;
    `total_candidates` is the true count before truncation, so the header
    can say "(3 of 172 runs)" even when only 10 are printed. Each candidate
    dict carries 'run', 'age', 'verdict', 'reason', 'command' (attention_*
    above build these; the caller assembles the dict once per row so this
    renderer stays a pure formatter, the same division of labor
    render_run_text keeps with its own _work_at_risk_lines/_what_to_do_items
    callers)."""
    redactor.names = Pseudonyms(enabled=redactor.enabled)
    lines = [f"NEEDS ATTENTION ({total_candidates} of {total_runs} runs)"]
    for candidate in shown_candidates:
        run = candidate["run"]
        lines.append(
            f"  {run.run_id}  {candidate['age']}  {candidate['verdict']}  "
            f"{redactor(candidate['reason'])}"
        )
        lines.append(f"    -> {redactor(candidate['command'])}")
    lines.append(_scan_triage_totals_line("LAST 7 DAYS", totals_7d))
    lines.append(_scan_triage_totals_line("ALL TIME", totals_all))
    lines.append("  full list: agent-postmortem scan --all")
    silenced_line = _silenced_detectors_line(silenced_detectors)
    if silenced_line is not None:
        lines.append(silenced_line)
    return "\n".join(lines).rstrip("\n") + "\n"


def _scan_row_line(row: dict, redactor: Redactor) -> str:
    # docs/DESIGN.md 4.7: id, date, project, kind, agents, attempts,
    # verdict, top detector, in that order.
    project = redactor(row.get("project", ""))
    kind = row.get("kind", "workflow")
    # docs/DESIGN.md 12's D-C: the verdict is left exactly as computed (a
    # real critical finding stays visible) with "unfinished" appended
    # after it, not substituted for it, when this run has not finished --
    # so `latest` can skip it, but a run can be both genuinely broken and
    # still in progress and the row says both.
    verdict = row.get("verdict", "")
    if row.get("finished") is False:
        verdict = f"{verdict} unfinished"
    line = (
        f"{row.get('run_id', '')}  {row.get('date', '')}  {project}  {kind}  "
        f"{row.get('agents', 0)} agents  {row.get('attempts', 0)} attempts  "
        f"{verdict}  {row.get('top_detector') or '-'}"
    )
    return line


def _scan_totals_lines(totals: dict) -> list[str]:
    """docs/DESIGN.md 4.7: totals stratified by kind -- workflow runs, runs
    with retries, attempts, wasted hours; session runs, subagents,
    interrupted -- then findings per detector (each already counted only
    over the runs of the kinds that detector applies to, since a detector
    never fires on a run outside its own kinds)."""
    workflow = totals.get("workflow", {})
    session = totals.get("session", {})
    lines = [
        f"{workflow.get('runs', 0)} workflow runs, "
        f"{workflow.get('runs_with_retries', 0)} with retries, "
        f"{workflow.get('attempts', 0)} attempts, {workflow.get('wasted_hours', 0.0):.1f}h wasted",
        f"{session.get('runs', 0)} session runs, {session.get('subagents', 0)} subagents, "
        f"{session.get('interrupted', 0)} interrupted",
    ]
    findings_per_detector = totals.get("findings_per_detector", {})
    for detector_id in sorted(findings_per_detector):
        lines.append(f"  {detector_id}: {findings_per_detector[detector_id]}")
    return lines


def render_scan_text(
    rows: list[dict], totals: dict, redactor: Redactor, silenced_detectors: frozenset = frozenset(),
) -> str:
    # docs/DESIGN.md 5.1: every public renderer entry point gets a fresh
    # Pseudonyms map, even one like this that never prints a per-agent
    # name (scan lists runs, not agents) -- a stale mapping left over from
    # an earlier render must never leak into this one.
    redactor.names = Pseudonyms(enabled=redactor.enabled)
    lines = [_scan_row_line(row, redactor) for row in rows]
    lines.append("")
    lines.extend(_scan_totals_lines(totals))
    silenced_line = _silenced_detectors_line(silenced_detectors)
    if silenced_line is not None:
        lines.append(silenced_line)
    return "\n".join(lines).rstrip("\n") + "\n"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _key_digest(key: str | None) -> str | None:
    """sha256(key)[:8]: an opaque, one-way stand-in for a journal key
    (docs/DESIGN.md 5.3). Unconditional on the redactor: a workflow key
    often embeds the attempt's own label verbatim (e.g. "v2:silent-alpha"),
    so the export never carries the raw key, --no-redact or not."""
    if key is None:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def _export_attempt(attempt: Attempt, redactor: Redactor) -> dict:
    """docs/DESIGN.md 5.3's Attempt allowlist. Calling redactor.names here
    (rather than the plain redactor()) both returns the pseudonym and
    registers it, so a finding title or evidence line rendered afterward
    with the same redactor picks up the same substitution via apply()."""
    interrupt = None
    if attempt.interrupt is not None:
        interrupt = {
            "cause": attempt.interrupt.cause,
            "silence_seconds": attempt.interrupt.silence_seconds,
        }
    agent_type = redactor.names.type(attempt.agent_type) if attempt.agent_type else None
    # docs/DESIGN.md 5.3: reported is not a structured name field (it never
    # embeds a label the way a title does), so it goes through the plain
    # Redactor via redact_value -- structural redaction only, same as an
    # evidence line or a metrics value. This is what redacts a report_path
    # claim (a path) without a pseudonym ever touching it.
    reported = redact_value(attempt.reported, redactor) if attempt.reported is not None else None
    # docs/DESIGN.md 5.3's v0.2 reader changes: facts is exported, tool_calls
    # is not (a command is private, never leaves the tool). facts holds
    # paths (paths_written, in_flight[].path, cwds) that go through the same
    # plain Redactor as any other path, exactly like `reported` above -- not
    # a structured name field, so never through Pseudonyms. Built from
    # AttemptFacts' own fields directly, the same way `interrupt` above is
    # built from Interrupt's fields, never through its private complete-dict
    # method (that form is for tests only -- report.py never calls it).
    facts = None
    if attempt.facts is not None:
        facts = redact_value(
            {
                "paths_written": attempt.facts.paths_written,
                "in_flight": attempt.facts.in_flight,
                "commits": attempt.facts.commits,
                "cwds": attempt.facts.cwds,
                "background_unfinished": attempt.facts.background_unfinished,
                "last_tool": attempt.facts.last_tool,
                "seconds_since_last_tool_at_end": attempt.facts.seconds_since_last_tool_at_end,
            },
            redactor,
        )
    return {
        "agent_id": attempt.agent_id,
        "key": _key_digest(attempt.key),
        "label": redactor.names.agent(attempt.label),
        "phase": redactor.names.phase(attempt.phase),
        "agent_type": agent_type,
        "ordinal": attempt.ordinal,
        "outcome": attempt.outcome,
        "first_ts": _iso(attempt.first_ts),
        "last_ts": _iso(attempt.last_ts),
        "usage": attempt.usage,
        "event_count": len(attempt.events),
        "interrupt": interrupt,
        "reported": reported,
        "facts": facts,
    }


def _export_finding(finding: Finding, redactor: Redactor, silenced_detectors: frozenset) -> dict:
    """docs/DESIGN.md 5.3: 'all fields, evidence redacted'. title goes
    through redactor.title() (structural redact() plus pseudonym
    substitution: a title routinely embeds a label verbatim, since a
    detector formats it straight into a sentence). evidence and metrics go
    through the plain redactor only -- structural redact(), never a
    pseudonym substitution: docs/DESIGN.md 5.2 keeps evidence (and a
    metrics value that can carry raw text of its own, e.g.
    bash_backgrounded's commands) a faithful quote of transcript text.
    rule_ids (unit M1d) carries no user text, so it needs no redaction, the
    same as rule_id.

    `silenced` (unit M3c, docs/DESIGN.md 4.10): true when this finding's
    detector is named in [detectors].off in the user's config file. A
    silenced finding is never dropped from --json -- only from text
    rendering and hook notification -- so this bool is what lets a JSON
    consumer tell the two apart."""
    return {
        "detector": finding.detector,
        "severity": finding.severity,
        "title": redactor.title(finding.title),
        "run_id": finding.run_id,
        "agent_id": finding.agent_id,
        "evidence": [redactor(line) for line in finding.evidence],
        "metrics": redact_value(finding.metrics, redactor),
        "rule_id": finding.rule_id,
        "rule_ids": list(finding.rule_ids),
        "finding_id": finding.finding_id,
        "silenced": finding.detector in silenced_detectors,
    }


def _export_observation(observation, redactor: Redactor) -> dict:
    """docs/DESIGN.md 4.9/5.3: {checked, reason, paths}, paths keyed by the
    same path-N pseudonym (or real repo-relative path under --no-redact) the
    text renderer uses. repo_root, head, and git_version are reader-internal
    only and are never exported, the same way Attempt.tool_calls and
    reported_line never are: a repo_root is a real absolute path with
    nothing to pseudonymize it, and neither the head commit nor the git
    version is a fact this milestone's report needs to expose."""
    paths = {}
    for path, path_observation in observation.paths.items():
        key = redactor.names.path(path)
        paths[key] = {
            "state": path_observation.state,
            "reason": (
                redactor(path_observation.reason) if path_observation.reason is not None else None
            ),
            "later_write_observed": path_observation.later_write_observed,
            "attempts": list(path_observation.attempts),
        }
    return {
        "checked": observation.checked,
        "reason": redactor(observation.reason) if observation.reason is not None else None,
        "paths": paths,
    }


def export_run(
    run: Run,
    findings: list[Finding],
    redactor: Redactor,
    observation=None,
    silenced_detectors: frozenset = frozenset(),
) -> dict:
    """The JSON export allowlist (docs/DESIGN.md 5.3): {run, findings}, with
    run holding exactly run_id, kind, status, claude_version, started_at,
    ended_at, counts, diagnostics, and attempts (each attempt allowlisted by
    _export_attempt). No meta, no cwd, no branch, no event list, no
    transcript text -- and this never reaches for a dataclass's own private,
    complete dict form (that form exists for tests only).

    _register_names runs first (docs/DESIGN.md 5.1): a fresh Pseudonyms map
    registered with every attempt's label/meta description/phase/agent_type
    before either attempts or findings are exported, so a finding title
    that embeds one of those verbatim (an ordinary detector does exactly
    that) is pseudonymized correctly regardless of which findings happen to
    reference which attempt.

    `silenced_detectors` (unit M3c, docs/DESIGN.md 4.10) never removes a
    finding from `findings` here -- every finding a silenced detector fired
    still exports, with `silenced: true` (5.3) -- callers that must not
    show a silenced finding (the text renderers, the verdict, stories,
    next) filter beforehand with config.partition_silenced() instead.
    """
    _register_names(run, redactor)
    _register_facts_paths(run, redactor)
    _register_observation_paths(observation, redactor)
    attempts = [_export_attempt(attempt, redactor) for attempt in run.attempts]
    exported_findings = [
        _export_finding(finding, redactor, silenced_detectors) for finding in findings
    ]
    interrupted = sum(1 for attempt in run.attempts if attempt.outcome == "killed")

    payload = {
        "run": {
            "run_id": run.run_id,
            "kind": run.kind,
            "status": run.status,
            "claude_version": run.claude_version,
            "started_at": _iso(run.started_at),
            "ended_at": _iso(run.ended_at),
            "counts": {
                "agents": len(run.agents),
                "attempts": len(run.attempts),
                "interrupted": interrupted,
            },
            "diagnostics": [redactor(diagnostic) for diagnostic in run.diagnostics],
            "attempts": attempts,
        },
        "findings": exported_findings,
    }
    # docs/DESIGN.md 4.9 rule 10: the key is present at all only when the
    # caller actually ran --observe; without it, JSON output is unchanged.
    if observation is not None:
        payload["observation"] = _export_observation(observation, redactor)
    return payload


def _export_story(entry: dict, redactor: Redactor) -> dict:
    """docs/DESIGN.md 4.7 rule 9/5.3: {agent, severity, text, finding_ids}.
    agent goes through the same Pseudonyms.agent() the AGENT table and text
    STORIES section use (the literal 'run' label for a run-level story is
    never a user-authored name and is left alone); text through
    redactor.title() (5.1/5.2: a story sentence routinely embeds a label
    verbatim, the same reason Finding.title does)."""
    agent = "run" if entry["agent"] == "run" else redactor.names.agent(entry["agent"])
    return {
        "agent": agent,
        "severity": entry["severity"],
        "text": redactor.title(entry["text"]),
        "finding_ids": list(entry["finding_ids"]),
    }


def _export_next(item: dict, redactor: Redactor) -> dict:
    """docs/DESIGN.md 4.7 rule 9/5.3: {grade, text, command}. Neither field
    is a structured name field (a command names no user, only a run id, and
    'text' is one of this module's own fixed phrases), so both go through
    the plain Redactor exactly like a metrics value -- structural redact()
    only."""
    return {
        "grade": item["grade"],
        "text": redactor(item["text"]),
        "command": redactor(item["command"]) if item["command"] else None,
    }


SCHEMA_VERSION = 2


def to_json(
    run: Run,
    findings: list[Finding],
    redactor: Redactor | None = None,
    observation=None,
    silenced_detectors: frozenset = frozenset(),
) -> dict:
    """docs/DESIGN.md 4.7/5.3: {schema, run, findings, rules, verdict,
    status, stories, next}, run and findings built by export_run (5.3's
    allowlist), rules and verdict derived the same way the text report
    derives them, status duplicated at the top level for a consumer that
    does not want to dig into run. observation (v0.2, unit M2b) is forwarded
    to export_run unchanged and is absent from the payload entirely unless
    the caller passed one.

    stories/next (unit M3a) are the same records render_run_text derives,
    exported through the allowlist of docs/DESIGN.md 4.7 rule 9/5.3 rather
    than report.py's internal, wrapped/truncated text-rendering shape:
    stories always computed with verbose=False, so an info-only agent never
    backs a JSON story either, same as the default text report.

    `silenced_detectors` (unit M3c, docs/DESIGN.md 4.10): `findings` itself
    is exported whole -- every finding, silenced or not, each getting its
    own `silenced` bool (5.3) -- but `verdict`, `rules`, `stories`, and
    `next` are all derived from the shown (non-silenced) findings only, the
    same rule the text report follows (docs/DESIGN.md 4.6/4.7): a silenced
    detector must never flip the verdict, suggest its own rule, or narrate
    its own story, even though its raw finding is still right there in
    `findings` for a caller that wants it.

    Defaults to redaction on (docs/DESIGN.md 5.2/5.3): a caller that wants
    raw output must pass an explicit Redactor(enabled=False), never get it
    by omission."""
    redactor = redactor or Redactor(enabled=True)
    payload = export_run(run, findings, redactor, observation, silenced_detectors)
    payload["schema"] = SCHEMA_VERSION
    shown = [finding for finding in findings if finding.detector not in silenced_detectors]
    payload["rules"] = [{"rule_id": rule.id, "count": count} for rule, count in rules_for(shown)]
    payload["verdict"] = verdict_word(shown, run.status)
    payload["status"] = run.status

    # export_run already ran _register_names/_register_observation_paths, so
    # every pseudonym _export_story below needs is already registered.
    story_entries = _story_records(run, shown, verbose=False)
    payload["stories"] = [_export_story(entry, redactor) for entry in story_entries]

    hidden_count = sum(1 for finding in shown if finding.severity == "info")
    next_items = _what_to_do_items(
        run, shown, observation, observation is not None, hidden_count,
    )
    payload["next"] = [_export_next(item, redactor) for item in next_items]
    return payload


def _bug_title(run: Run, findings: list[Finding], redactor: Redactor) -> str:
    verdict = verdict_word(findings, run.status)
    summary = redactor(_summary_line_for(run))
    return f"# {redactor(run.run_id)} ({verdict}): {summary}"


def _bug_environment_lines(run: Run) -> list[str]:
    return [
        "## Environment",
        f"- Claude Code version: {run.claude_version or 'unknown'}",
        f"- OS: {platform.platform()}",
    ]


def _bug_what_happened_lines(run: Run, findings: list[Finding], redactor: Redactor) -> list[str]:
    counts = {severity: 0 for severity in _SEVERITIES}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return [
        "## What happened",
        f"{verdict_word(findings, run.status)}: {redactor(_summary_line_for(run))}",
        "",
        f"- agents: {len(run.agents)}",
        f"- attempts: {len(run.attempts)}",
        f"- findings: critical {counts['critical']}, major {counts['major']}, "
        f"minor {counts['minor']}, info {counts['info']}",
    ]


def _bug_reproduction_lines(run: Run, redactor: Redactor) -> list[str]:
    lines = [
        "## Reproduction",
        f"project {redactor(run.project_slug)}, {len(run.agents)} logical agent(s):",
    ]
    for agent in run.agents:
        attempts = sorted(agent.attempts, key=lambda a: a.ordinal)
        models = sorted({attempt.agent_type for attempt in attempts if attempt.agent_type})
        pseudo_models = sorted({redactor.names.type(model) for model in models})
        model_str = ", ".join(pseudo_models) if pseudo_models else "unknown model"
        label = redactor.names.agent(agent.label)
        phase = redactor.names.phase(agent.phase)
        lines.append(f"- {label} (phase {phase}, {model_str}): {len(attempts)} attempt(s)")
    return lines


def _bug_evidence_lines(run: Run, findings: list[Finding], redactor: Redactor) -> list[str]:
    lines = ["## Evidence", f"From run {redactor(run.run_id)}:"]
    if not findings:
        lines.append("(no findings)")
        return lines
    for finding in findings:
        lines.append(f"- [{finding.severity}] {finding.detector}: {redactor.title(finding.title)}")
        for evidence in finding.evidence:
            lines.append(f"  - {redactor(evidence)}")
    return lines


def _bug_suggested_fixes_lines(findings: list[Finding]) -> list[str]:
    # See _suggested_rules_section: counts a finding once per rule in
    # rule_ids, not just its first rule_id, so a finding backing more than
    # one rule is never silently dropped from every rule but its first.
    lines = ["## Suggested fixes"]
    rule_ids = sorted({rule_id for finding in findings for rule_id in finding.rule_ids})
    detector_ids_without_rule = sorted(
        {
            finding.detector
            for finding in findings
            if not finding.rule_ids and finding.detector in _DETECTOR_BUG_SENTENCES
        }
    )
    if not rule_ids and not detector_ids_without_rule:
        lines.append("(no suggested fix in this build)")
        return lines
    for rule_id in rule_ids:
        rule = RULES.get(rule_id)
        if rule is None:
            continue
        count = sum(1 for finding in findings if rule_id in finding.rule_ids)
        plural = "" if count == 1 else "s"
        lines.append(f"- {rule_id} (backed by {count} finding{plural}): {rule.text}")
    for detector_id in detector_ids_without_rule:
        lines.append(f"- {detector_id}: {_DETECTOR_BUG_SENTENCES[detector_id]}")
    return lines


# docs/DESIGN.md 5.2's D2: the report is redacted by default, but an
# evidence line is a transcript excerpt quoted as-is -- never claim the
# whole output is safe to paste without being read first.
_BUG_REPORT_FOOTER = (
    "Redacted: paths, ids, emails, account names, agent names. "
    "Not redacted: evidence lines, which are transcript excerpts. "
    "Read them before posting."
)


def render_bug_report(run: Run, findings: list[Finding], redactor: Redactor) -> str:
    """Markdown bug report: Title, Environment, What happened, Reproduction,
    Evidence, Suggested fixes, in that order (docs/DESIGN.md section 4.4),
    then a footer (5.2's D2) spelling out what is and is not redacted."""
    _register_names(run, redactor)
    lines = [_bug_title(run, findings, redactor), ""]
    lines.extend(_bug_environment_lines(run))
    lines.append("")
    lines.extend(_bug_what_happened_lines(run, findings, redactor))
    lines.append("")
    lines.extend(_bug_reproduction_lines(run, redactor))
    lines.append("")
    lines.extend(_bug_evidence_lines(run, findings, redactor))
    lines.append("")
    lines.extend(_bug_suggested_fixes_lines(findings))
    lines.append("")
    lines.append(_BUG_REPORT_FOOTER)
    return "\n".join(lines).rstrip("\n") + "\n"
