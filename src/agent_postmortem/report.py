"""Render a Run and its Findings as text or JSON.

Verdict-first, small functions. render_bug_report drafts an upstream bug
report from the same Run and Findings render_run_text uses, in the section
order docs/DESIGN.md section 4.4 specifies: Title, Environment, What
happened, Reproduction, Evidence, Suggested fixes.
"""

from __future__ import annotations

import hashlib
import platform
from datetime import datetime, timezone

from .detectors.base import attempt_seconds, format_duration
from .models import Attempt, Finding, Run
from .redact import Pseudonyms, Redactor
from .rules import RULES, rules_for

_SEVERITIES = ("critical", "major", "minor", "info")

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
    excludes it from export."""
    redactor.names = Pseudonyms(enabled=redactor.enabled)
    for attempt in run.attempts:
        redactor.names.agent(attempt.label)
        meta = attempt.meta if isinstance(attempt.meta, dict) else {}
        description = meta.get("description")
        if isinstance(description, str):
            redactor.names.description(description, label=attempt.label)
        redactor.names.phase(attempt.phase)
        redactor.names.type(attempt.agent_type)


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


def _agent_table(run: Run, redactor: Redactor) -> list[str]:
    lines = [f"{'AGENT':<22}{'PHASE':<11}{'ATTEMPTS':<10}{'OUTCOME':<11}WALL-CLOCK"]
    for agent in run.agents:
        attempts = sorted(agent.attempts, key=lambda a: a.ordinal)
        final = attempts[-1] if attempts else None
        wall_seconds = sum(attempt_seconds(a) for a in attempts)
        killed = sum(1 for a in attempts if a.outcome == "killed")
        wasted_seconds = sum(attempt_seconds(a) for a in attempts[:-1])

        detail_parts = []
        if killed:
            detail_parts.append(f"{killed} killed")
        if wasted_seconds:
            detail_parts.append(f"{format_duration(wasted_seconds)} wasted")
        detail = f" ({', '.join(detail_parts)})" if detail_parts else ""

        outcome = final.outcome if final else "unknown"
        label = redactor.names.agent(agent.label)
        phase = redactor.names.phase(agent.phase)
        lines.append(
            f"{label:<22}{phase:<11}{len(attempts):<10}{outcome:<11}"
            f"{format_duration(wall_seconds)}{detail}"
        )
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


def _suggested_rules_section(findings: list[Finding]) -> list[str]:
    lines = ["SUGGESTED RULES"]
    rule_ids = sorted({finding.rule_id for finding in findings if finding.rule_id})
    if not rule_ids:
        lines.append("(none: no findings carry a rule id in this build)")
        return lines
    for rule_id in rule_ids:
        count = sum(1 for finding in findings if finding.rule_id == rule_id)
        plural = "" if count == 1 else "s"
        lines.append(
            f"{rule_id}  (backed by {count} finding{plural} in this run)  "
            f"agent-postmortem rules --show {rule_id}"
        )
    return lines


def _next_action_section(findings: list[Finding]) -> list[str]:
    lines = ["NEXT ACTION"]
    counts: dict[str, int] = {}
    for finding in findings:
        if finding.rule_id:
            counts[finding.rule_id] = counts.get(finding.rule_id, 0) + 1
    if not counts:
        lines.append("(no rule to apply yet)")
        return lines
    top_rule_id = max(sorted(counts), key=lambda rule_id: counts[rule_id])
    lines.append(f"agent-postmortem apply {top_rule_id} --target ~/.claude/CLAUDE.md")
    return lines


def render_run_text(run: Run, findings: list[Finding], redactor: Redactor) -> str:
    _register_names(run, redactor)
    kind_tag = " (session)" if run.kind == "session" else ""
    # docs/DESIGN.md 12's D-C: a workflow that has not finished still gets
    # a real verdict computed over whatever it has so far -- the header
    # just says so, rather than the old "RUNNING (no verdict yet)" line
    # that replaced the verdict outright. A session is always finished
    # (models.py's Run.finished), so this never applies to one.
    unfinished_tag = " (unfinished)" if not run.finished else ""
    lines = [
        f"RUN {run.run_id}{kind_tag}  {verdict_word(findings, run.status)}{unfinished_tag}",
        _summary_line_for(run),
        _project_line(run, redactor),
        "",
    ]
    lines.extend(_agent_table(run, redactor))
    lines.append("")
    lines.extend(_findings_section(findings, redactor))
    lines.append("")
    lines.extend(_suggested_rules_section(findings))
    lines.append("")
    lines.extend(_next_action_section(findings))
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


def render_scan_text(rows: list[dict], totals: dict, redactor: Redactor) -> str:
    # docs/DESIGN.md 5.1: every public renderer entry point gets a fresh
    # Pseudonyms map, even one like this that never prints a per-agent
    # name (scan lists runs, not agents) -- a stale mapping left over from
    # an earlier render must never leak into this one.
    redactor.names = Pseudonyms(enabled=redactor.enabled)
    lines = [_scan_row_line(row, redactor) for row in rows]
    lines.append("")
    lines.extend(_scan_totals_lines(totals))
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
    }


def _export_finding(finding: Finding, redactor: Redactor) -> dict:
    """docs/DESIGN.md 5.3: 'all fields, evidence redacted'. title goes
    through redactor.title() (structural redact() plus pseudonym
    substitution: a title routinely embeds a label verbatim, since a
    detector formats it straight into a sentence). evidence and metrics go
    through the plain redactor only -- structural redact(), never a
    pseudonym substitution: docs/DESIGN.md 5.2 keeps evidence (and a
    metrics value that can carry raw text of its own, e.g.
    bash_backgrounded's commands) a faithful quote of transcript text."""
    return {
        "detector": finding.detector,
        "severity": finding.severity,
        "title": redactor.title(finding.title),
        "run_id": finding.run_id,
        "agent_id": finding.agent_id,
        "evidence": [redactor(line) for line in finding.evidence],
        "metrics": redact_value(finding.metrics, redactor),
        "rule_id": finding.rule_id,
        "finding_id": finding.finding_id,
    }


def export_run(run: Run, findings: list[Finding], redactor: Redactor) -> dict:
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
    """
    _register_names(run, redactor)
    attempts = [_export_attempt(attempt, redactor) for attempt in run.attempts]
    exported_findings = [_export_finding(finding, redactor) for finding in findings]
    interrupted = sum(1 for attempt in run.attempts if attempt.outcome == "killed")

    return {
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


def to_json(run: Run, findings: list[Finding], redactor: Redactor | None = None) -> dict:
    """docs/DESIGN.md 4.7: {run, findings, rules, verdict, status}, run and
    findings built by export_run (5.3's allowlist), rules and verdict
    derived the same way the text report derives them, status duplicated at
    the top level for a consumer that does not want to dig into run.

    Defaults to redaction on (docs/DESIGN.md 5.2/5.3): a caller that wants
    raw output must pass an explicit Redactor(enabled=False), never get it
    by omission."""
    redactor = redactor or Redactor(enabled=True)
    payload = export_run(run, findings, redactor)
    payload["rules"] = [{"rule_id": rule.id, "count": count} for rule, count in rules_for(findings)]
    payload["verdict"] = verdict_word(findings, run.status)
    payload["status"] = run.status
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
    lines = ["## Suggested fixes"]
    rule_ids = sorted({finding.rule_id for finding in findings if finding.rule_id})
    detector_ids_without_rule = sorted(
        {
            finding.detector
            for finding in findings
            if not finding.rule_id and finding.detector in _DETECTOR_BUG_SENTENCES
        }
    )
    if not rule_ids and not detector_ids_without_rule:
        lines.append("(no suggested fix in this build)")
        return lines
    for rule_id in rule_ids:
        rule = RULES.get(rule_id)
        if rule is None:
            continue
        count = sum(1 for finding in findings if finding.rule_id == rule_id)
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
