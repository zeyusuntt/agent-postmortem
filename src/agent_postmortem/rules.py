"""The rules library: what a finding suggests doing about it.

RULES maps a stable id (R-RETRY-SAFE, ...) to the exact directive text a
maintainer can paste into a prompt, a hook, or their project's own
instructions. rules_for() turns a list of Findings into the rules they
back, ordered by how many findings back each one. propose_rules() turns a
whole fleet's worth of (Run, [Finding]) pairs into the rules that are
backed by enough evidence to suggest adopting (docs/DESIGN.md section 9).
apply_rule() computes the unified diff of a rule's managed block against a
target file -- it never writes the target itself (R3: the tool has no code
path that writes an instruction file; a human applies the diff by hand).
See docs/DESIGN.md section 4.3/9 for the contract this module implements,
including the exact rule texts.
"""

from __future__ import annotations

import difflib
import json
import textwrap
from dataclasses import dataclass
from pathlib import Path

from .models import Finding, Run

BEGIN_MARKER = (
    "<!-- agent-postmortem:rules:begin (do not edit inside; managed by postmortem apply) -->"
)
END_MARKER = "<!-- agent-postmortem:rules:end -->"

_HOOKS_DIR = Path(__file__).parent / "hooks"


@dataclass(frozen=True)
class Rule:
    id: str
    title: str  # one line, plain English
    text: str  # the exact directive text, verbatim into a managed block
    enforcement: str  # 'prompt' or 'hook'
    detectors: tuple[str, ...]  # detector ids that back this rule; () if none in v0
    hook_template: str | None  # filename under hooks/, or None
    # docs/DESIGN.md 9: where a human should put this rule -- "workflow
    # prompt" (the agent prompt preamble in a workflow script, where most
    # rules belong), "hook" (a Claude Code hook, like R-MODEL-ALLOCATION),
    # or "project instructions" (a repo-wide file like R-GIT-CWD, since it
    # is about how every agent in every workflow is launched, not one
    # workflow's own prompt).
    target_kind: str


RULES: dict[str, Rule] = {
    "R-RETRY-SAFE": Rule(
        id="R-RETRY-SAFE",
        title="Recover from a killed attempt without losing work",
        text=(
            "Every implementation prompt begins: You may be a retry after a killed "
            "attempt. First run git status and git diff --stat; if uncommitted work "
            "exists, verify it and commit it before continuing. Never git checkout, "
            "restore, or stash a file holding uncommitted work."
        ),
        enforcement="prompt",
        detectors=("silent_retry",),
        hook_template=None,
        target_kind="workflow prompt",
    ),
    "R-CAP-CONCURRENCY": Rule(
        id="R-CAP-CONCURRENCY",
        title="Cap concurrent heavy agents per parallel() call",
        text=(
            "Never start more than two heavy agents in one parallel() call; batch "
            "them. Simultaneous requests from one account are the observed trigger "
            "for paired stalls and kills."
        ),
        enforcement="prompt",
        detectors=("silent_retry",),
        hook_template=None,
        target_kind="workflow prompt",
    ),
    "R-NO-LONG-BASH": Rule(
        id="R-NO-LONG-BASH",
        title="Never run a Bash command that can outlive its timeout",
        text=(
            "Inside a workflow agent never run a shell command that can outlive its "
            "timeout: scope test runs to touched files, pass an explicit timeout the "
            "run will meet, no sleep or poll loops."
        ),
        enforcement="prompt",
        detectors=("bash_backgrounded", "bash_timeout"),
        hook_template=None,
        target_kind="workflow prompt",
    ),
    "R-SMALL-QUANTUM": Rule(
        id="R-SMALL-QUANTUM",
        title="Keep each agent() call to one milestone",
        text="One milestone per agent() call, about 30 to 60 minutes; never a multi-hour stage.",
        enforcement="prompt",
        detectors=(),
        hook_template=None,
        target_kind="workflow prompt",
    ),
    "R-ASSERT-ARTIFACT": Rule(
        id="R-ASSERT-ARTIFACT",
        title="Assert a required artifact exists before consuming it",
        text=(
            "When an agent was told to produce a file, assert the file exists and is "
            "non-trivial before any later stage consumes it. A truthy return means "
            "the agent replied, not that the work landed. A missing artifact fails "
            "loudly."
        ),
        enforcement="prompt",
        detectors=("missing_artifact", "codex_write_denied"),
        hook_template=None,
        target_kind="workflow prompt",
    ),
    "R-SCALAR-SCHEMA": Rule(
        id="R-SCALAR-SCHEMA",
        title="Keep structured-output schemas to scalars and paths",
        text=(
            "Structured-output schemas carry scalars and paths only. Prose findings "
            "go to a file the schema points at."
        ),
        enforcement="prompt",
        detectors=("structured_output_failure",),
        hook_template=None,
        target_kind="workflow prompt",
    ),
    "R-CODEX-CONTRACT": Rule(
        id="R-CODEX-CONTRACT",
        title="Give a Codex reviewer an unambiguous read or write contract",
        text=(
            "A Codex reviewer inside a workflow either writes to a path inside the "
            "project cwd with no read-only wording in its prompt, or stays read-only "
            "and returns findings as text for a writable stage to persist. Keep each "
            "Codex task under eight minutes."
        ),
        enforcement="prompt",
        detectors=("codex_write_denied",),
        hook_template=None,
        target_kind="workflow prompt",
    ),
    "R-MONITOR-RETRIES": Rule(
        id="R-MONITOR-RETRIES",
        title="Monitor a workflow for silent retries",
        text=(
            "After launching a workflow, watch for a second agent meta file "
            "appearing for the same logical agent; at the end compare journal "
            "starts to agent count and report retries and the wall-clock they "
            "burned."
        ),
        enforcement="prompt",
        detectors=("mislabeled_kill",),
        hook_template=None,
        target_kind="workflow prompt",
    ),
    "R-GIT-CWD": Rule(
        id="R-GIT-CWD",
        title="Give every agent an explicit repository path",
        text=(
            "Give every agent the absolute repository path and have it cd there "
            "first; never rely on the inherited working directory."
        ),
        enforcement="prompt",
        detectors=("git_cwd_mismatch",),
        hook_template=None,
        target_kind="project instructions",
    ),
    "R-USAGE-LIMIT": Rule(
        id="R-USAGE-LIMIT",
        title="Do not relaunch before a usage limit resets",
        text=(
            "When a run reports a usage-limit failure, do not relaunch the same key "
            "before the reset time the failure named."
        ),
        enforcement="prompt",
        detectors=("attempt_failed",),
        hook_template=None,
        target_kind="workflow prompt",
    ),
    "R-MODEL-ALLOCATION": Rule(
        id="R-MODEL-ALLOCATION",
        title="Set an explicit model or agentType on every agent() call",
        text="Every agent() call sets an explicit model or agentType.",
        enforcement="hook",
        detectors=(),
        hook_template="model_allocation_guard.mjs",
        target_kind="hook",
    ),
}


def rules_for(findings: list[Finding]) -> list[tuple[Rule, int]]:
    """Which rules a list of findings back, and by how many findings each.

    Ordered by backing count descending, then by rule id, so the most
    impactful rule is first. A finding counts once per rule in its
    rule_ids (unit M1d) -- a finding backing two rules (silent_retry's
    R-RETRY-SAFE plus R-CAP-CONCURRENCY on a burst) adds one to each,
    exactly as if it had been two separate findings, without actually being
    two. Findings with no rule_ids, or a rule id this library does not
    know, are ignored.
    """
    counts: dict[str, int] = {}
    for finding in findings:
        for rule_id in finding.rule_ids:
            if rule_id in RULES:
                counts[rule_id] = counts.get(rule_id, 0) + 1
    rows = [(RULES[rule_id], count) for rule_id, count in counts.items()]
    rows.sort(key=lambda row: (-row[1], row[0].id))
    return rows


def render_rules_text(rows: list[tuple[Rule, int]]) -> str:
    """One line per (rule, backing_count) row, in the order given."""
    if not rows:
        return "(no rules backed by a finding in this scope)\n"
    lines = []
    for rule, count in rows:
        plural = "" if count == 1 else "s"
        lines.append(f"{rule.id}  ({count} finding{plural})  {rule.title}")
    return "\n".join(lines) + "\n"


def render_rule_show(rule: Rule) -> str:
    """The full text of one rule: id, title, enforcement, detectors,
    target_kind, text."""
    detectors = ", ".join(rule.detectors) if rule.detectors else "(none in v0)"
    lines = [
        f"{rule.id}: {rule.title}",
        f"enforcement: {rule.enforcement}",
        f"detectors: {detectors}",
        f"belongs in: {rule.target_kind}",
        "",
        rule.text,
    ]
    if rule.hook_template:
        lines.append("")
        lines.append(f"hook template: {_HOOKS_DIR / rule.hook_template}")
    return "\n".join(lines) + "\n"


def rule_to_json(rule: Rule) -> dict:
    """The JSON shape for `rules --show --json`: the same fields
    render_rule_show renders as text, structured instead of prose."""
    return {
        "rule_id": rule.id,
        "title": rule.title,
        "enforcement": rule.enforcement,
        "detectors": list(rule.detectors),
        "target_kind": rule.target_kind,
        "text": rule.text,
    }


def render_hook_install_note(rule: Rule) -> str:
    """Template path and a settings.json snippet to register the rule's hook.

    Returns "" for a rule with no hook_template. v0 never edits settings.json
    itself (docs/DESIGN.md section 7); this only tells the user what to add.
    """
    if not rule.hook_template:
        return ""
    template_path = _HOOKS_DIR / rule.hook_template
    snippet = json.dumps(
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Workflow",
                        "hooks": [{"type": "command", "command": f"node {template_path}"}],
                    }
                ]
            }
        },
        indent=2,
    )
    return (
        f"Hook template: {template_path}\n"
        "Register it by merging this into your settings.json "
        "(v0 does not edit settings.json for you):\n"
        f"{snippet}\n"
    )


def _render_block(rule_ids: set[str]) -> str:
    lines = [BEGIN_MARKER]
    for rule_id in sorted(rule_ids):
        rule = RULES.get(rule_id)
        text = rule.text if rule is not None else "(unknown rule)"
        lines.append(f"- {rule_id}: {text}")
    lines.append(END_MARKER)
    return "\n".join(lines)


def _parse_block_ids(block_text: str) -> set[str]:
    ids: set[str] = set()
    for line in block_text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            rule_id = line[2:].split(":", 1)[0].strip()
            if rule_id:
                ids.add(rule_id)
    return ids


def _separator_before_a_new_block(original: str) -> str:
    """Just enough newlines to put one blank line between original's
    existing trailing content and a newly created managed block, without
    stripping any trailing blank lines original already has: everything
    outside the block is preserved, not just everything before it."""
    trailing_newlines = len(original) - len(original.rstrip("\n"))
    if trailing_newlines >= 2:
        return ""
    if trailing_newlines == 1:
        return "\n"
    return "\n\n"


def _with_rule_applied(original: str, rule_id: str) -> str:
    """original with rule_id added to its managed block (creating one if absent)."""
    begin_idx = original.find(BEGIN_MARKER)
    if begin_idx == -1:
        block_text = _render_block({rule_id})
        if original == "":
            return block_text + "\n"
        return original + _separator_before_a_new_block(original) + block_text + "\n"

    end_idx = original.find(END_MARKER, begin_idx)
    if end_idx == -1:
        raise ValueError("target has an unterminated agent-postmortem rules block")
    end_idx_full = end_idx + len(END_MARKER)

    rule_ids = _parse_block_ids(original[begin_idx:end_idx_full])
    rule_ids.add(rule_id)
    block_text = _render_block(rule_ids)
    return original[:begin_idx] + block_text + original[end_idx_full:]


def _diff_text(original: str, new_text: str, target: Path) -> str:
    diff_lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=str(target),
        tofile=str(target),
    )
    return "".join(diff_lines)


def apply_rule(rule_id: str, target: Path) -> str:
    """The unified diff of adding rule_id to target's managed rules block
    against target's current content ('' if target does not exist); '' when
    the rule is already present (nothing to add).

    docs/DESIGN.md 9/R3: this never writes target, and there is no `write`
    argument to ask it to -- the tool has no code path that writes an
    instruction file. A human reads the diff and applies it themselves,
    with their own editor or `git apply`.

    Raises ValueError for an unknown rule id or a target whose rules block
    has a begin marker but no end marker.
    """
    if rule_id not in RULES:
        raise ValueError(f"unknown rule id: {rule_id}")

    target = Path(target)
    original = target.read_text(encoding="utf-8") if target.exists() else ""
    new_text = _with_rule_applied(original, rule_id)
    return _diff_text(original, new_text, target)


# --- rules propose (docs/DESIGN.md 9) ---------------------------------------

# A rule is proposed only once its backing detector(s) fired in at least
# this many distinct runs, on at least this many distinct days (by run
# start date): one bad week should not read as a pattern.
PROPOSE_MIN_RUNS = 3
PROPOSE_MIN_DAYS = 2


def propose_rules(runs_with_findings: list[tuple[Run, list[Finding]]]) -> list[dict]:
    """Which rules a fleet's worth of runs backs strongly enough to propose
    adopting (docs/DESIGN.md 9), sorted by run count descending then rule id.

    A finding backs a rule the same way rules_for() counts it: once per rule
    named in its own rule_ids. Each returned dict is {'rule': Rule,
    'findings': int, 'runs': int, 'first': 'YYYY-MM-DD', 'last':
    'YYYY-MM-DD'} -- 'findings' is the total finding count, 'runs' the
    number of distinct runs that backed it at least once, 'first'/'last'
    the earliest/latest start date among those runs. A run with no
    started_at still counts toward 'findings'/'runs' (its own finding did
    happen) but can never be one of the >= PROPOSE_MIN_DAYS distinct days a
    rule needs to qualify, since it cannot be placed on a calendar day at
    all.
    """
    by_rule: dict[str, dict] = {}
    for run, findings in runs_with_findings:
        run_date = run.started_at.date() if run.started_at is not None else None
        for finding in findings:
            for rule_id in finding.rule_ids:
                if rule_id not in RULES:
                    continue
                entry = by_rule.setdefault(rule_id, {"findings": 0, "runs": set(), "dates": set()})
                entry["findings"] += 1
                entry["runs"].add(run.run_id)
                if run_date is not None:
                    entry["dates"].add(run_date)

    rows: list[dict] = []
    for rule_id, entry in by_rule.items():
        if len(entry["runs"]) < PROPOSE_MIN_RUNS or len(entry["dates"]) < PROPOSE_MIN_DAYS:
            continue
        rows.append({
            "rule": RULES[rule_id],
            "findings": entry["findings"],
            "runs": len(entry["runs"]),
            "first": min(entry["dates"]).isoformat(),
            "last": max(entry["dates"]).isoformat(),
        })

    rows.sort(key=lambda row: (-row["runs"], row["rule"].id))
    return rows


def render_propose_text(rows: list[dict]) -> str:
    """docs/DESIGN.md 9: one block per proposed rule, blank-line separated,
    sorted the same way propose_rules() already sorted `rows`."""
    if not rows:
        return "no rule is backed by 3 runs on 2 days yet\n"

    blocks = []
    for row in rows:
        rule = row["rule"]
        findings_plural = "" if row["findings"] == 1 else "s"
        runs_plural = "" if row["runs"] == 1 else "s"
        lines = [
            f"{rule.id}   backed by {row['findings']} finding{findings_plural} in "
            f"{row['runs']} run{runs_plural}, first {row['first']}, last {row['last']}",
            f"  belongs in: {rule.target_kind}",
        ]
        lines.extend(
            textwrap.fill(
                rule.text, width=76, initial_indent="  ", subsequent_indent="  "
            ).splitlines()
        )
        lines.append(
            f"  show the diff for a file: agent-postmortem apply {rule.id} --target <file>"
        )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def propose_row_to_json(row: dict) -> dict:
    """The JSON shape for one `rules propose --json` entry (docs/DESIGN.md
    5.3a schema 2)."""
    rule = row["rule"]
    return {
        "rule_id": rule.id,
        "target_kind": rule.target_kind,
        "findings": row["findings"],
        "runs": row["runs"],
        "first": row["first"],
        "last": row["last"],
        "text": rule.text,
    }
