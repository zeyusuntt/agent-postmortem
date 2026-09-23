"""The rules library: what a finding suggests doing about it.

RULES maps a stable id (R-RETRY-SAFE, ...) to the exact directive text a
maintainer can paste into a CLAUDE.md or similar prompt file. rules_for()
turns a list of Findings into the rules they back, ordered by how many
findings back each one. apply_rule() maintains a small managed block inside
a target file so the same rule can be applied more than once without
duplicating it. See docs/DESIGN.md section 4.5 for the contract this module
implements, including the exact rule texts.
"""

from __future__ import annotations

import difflib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import state
from .models import Finding
from .state import now_iso

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
    ),
    "R-SMALL-QUANTUM": Rule(
        id="R-SMALL-QUANTUM",
        title="Keep each agent() call to one milestone",
        text="One milestone per agent() call, about 30 to 60 minutes; never a multi-hour stage.",
        enforcement="prompt",
        detectors=(),
        hook_template=None,
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
    ),
    "R-MODEL-ALLOCATION": Rule(
        id="R-MODEL-ALLOCATION",
        title="Set an explicit model or agentType on every agent() call",
        text="Every agent() call sets an explicit model or agentType.",
        enforcement="hook",
        detectors=(),
        hook_template="model_allocation_guard.mjs",
    ),
}


def rules_for(findings: list[Finding]) -> list[tuple[Rule, int]]:
    """Which rules a list of findings back, and by how many findings each.

    Ordered by backing count descending, then by rule id, so the most
    impactful rule is first. Findings with no rule_id, or a rule_id this
    library does not know, are ignored.
    """
    counts: dict[str, int] = {}
    for finding in findings:
        if finding.rule_id and finding.rule_id in RULES:
            counts[finding.rule_id] = counts.get(finding.rule_id, 0) + 1
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
    """The full text of one rule: id, title, enforcement, detectors, text."""
    detectors = ", ".join(rule.detectors) if rule.detectors else "(none in v0)"
    lines = [
        f"{rule.id}: {rule.title}",
        f"enforcement: {rule.enforcement}",
        f"detectors: {detectors}",
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


class ApplyDiff(str):
    """The diff text apply_rule() returns (possibly empty), plus one extra
    bit: `already_present_unrecorded`, true when the diff is empty because
    the rule is already present in the target but the state store has no
    applied entry for this (rule_id, target) pair yet -- drift, not a true
    no-op. A plain str everywhere it is used as one (every existing caller
    only ever looked at the diff text); the CLI reads the extra attribute
    to choose between "nothing to do" and "already present, recorded"."""

    already_present_unrecorded: bool

    def __new__(cls, diff_text: str, *, already_present_unrecorded: bool = False) -> "ApplyDiff":
        obj = str.__new__(cls, diff_text)
        obj.already_present_unrecorded = already_present_unrecorded
        return obj


def _diff_text(original: str, new_text: str, target: Path) -> str:
    diff_lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=str(target),
        tofile=str(target),
    )
    return "".join(diff_lines)


def _has_matching_applied_entry(applied: list, rule_id: str, target: str) -> bool:
    return any(
        (
            isinstance(entry, dict)
            and entry.get("rule_id") == rule_id
            and entry.get("target") == target
        )
        for entry in applied
    )


def _fsync_dir(dir_path: Path) -> None:
    """Best-effort fsync of a directory's entry, so a rename into it is
    durable and not just sitting in a page cache. Never blocks a write that
    already landed: a directory that cannot be opened or fsynced (some
    filesystems do not support it) is not a reason to raise."""
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


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically: a temp file in the same directory,
    fsynced before and after the rename, replacing path only once the write
    is durable. Preserves path's existing mode: os.replace() otherwise
    leaves the target with mkstemp's own 0600, silently loosening or
    tightening permissions on every write. Refuses to write through a
    symlink: os.replace() swaps the symlink itself for a regular file
    instead of updating whatever it points to, which is not what "edit
    this file" means when the file is a symlink.
    """
    if path.is_symlink():
        raise ValueError(f"refusing to write through a symlink: {path}")

    original_stat = path.stat() if path.exists() else None

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if original_stat is not None:
            try:
                os.chmod(tmp_name, original_stat.st_mode)
            except OSError:
                pass  # best effort: preserve mode only where permitted
            if hasattr(os, "chown"):
                try:
                    os.chown(tmp_name, original_stat.st_uid, original_stat.st_gid)
                except OSError:
                    pass  # best effort: preserve ownership only where permitted
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def apply_rule(rule_id: str, target: Path, write: bool = False) -> ApplyDiff:
    """Add rule_id to target's managed rules block; return a unified diff.

    Without write, nothing on disk changes -- not the target, not the
    state store. With write, target and state change together inside one
    state.transaction() (docs/DESIGN.md 9): read target, compute the diff,
    write target atomically if the diff is non-empty, append the applied
    entry, commit. If the transaction fails for any reason (state locked,
    too new, a filesystem error), nothing was written -- never a target
    already rewritten with no matching state record. Because the target's
    own read-modify-write now happens under the same lock, two concurrent
    --write calls against the same target are fully serialized, not just
    their state records.

    Idempotent against drift: if the diff is empty because the rule is
    already present but no applied entry names this (rule_id, target) pair
    yet, --write records one anyway, and both --write and a dry run report
    that through the returned ApplyDiff's `already_present_unrecorded`
    flag rather than the plain "nothing to do" case. A second --write
    after that, or once the rule was applied normally, is a true no-op:
    nothing written, nothing recorded again.

    Raises ValueError for an unknown rule id or a target whose rules block
    has a begin marker but no end marker; propagates state.StateLocked,
    state.StateTooNew, or any other state.StateError from the transaction.
    """
    if rule_id not in RULES:
        raise ValueError(f"unknown rule id: {rule_id}")

    target = Path(target)
    target_str = str(target)

    if not write:
        original = target.read_text(encoding="utf-8") if target.exists() else ""
        new_text = _with_rule_applied(original, rule_id)
        diff_text = _diff_text(original, new_text, target)
        if diff_text:
            return ApplyDiff(diff_text)
        applied = state.load_state().get("applied", [])
        already_recorded = _has_matching_applied_entry(applied, rule_id, target_str)
        return ApplyDiff("", already_present_unrecorded=not already_recorded)

    with state.transaction() as current:
        original = target.read_text(encoding="utf-8") if target.exists() else ""
        new_text = _with_rule_applied(original, rule_id)
        diff_text = _diff_text(original, new_text, target)

        applied = current.setdefault("applied", [])
        already_recorded = _has_matching_applied_entry(applied, rule_id, target_str)

        if diff_text:
            _atomic_write(target, new_text)
            applied.append({"rule_id": rule_id, "target": target_str, "applied_at": now_iso()})
            return ApplyDiff(diff_text)

        if not already_recorded:
            applied.append({"rule_id": rule_id, "target": target_str, "applied_at": now_iso()})
            return ApplyDiff("", already_present_unrecorded=True)

        return ApplyDiff("")
