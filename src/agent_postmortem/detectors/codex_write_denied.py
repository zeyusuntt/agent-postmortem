"""Detects a Codex sandbox denying a write, or the workspace shell guard
blocking a command outright.

DESIGN.md 4.3: critical severity, fires when the sandbox write-denial marker
(SANDBOX_WRITE_DENIED in markers.py: two alternative observed forms, either
is enough) or the shell-guard block marker (SHELL_GUARD_BLOCK_REGEX) appears
in (a) an assistant text event, or (b) a Bash tool_result event whose own
command text mentions "codex" (case-insensitive). The Codex plugin is the
only producer of WRITE_DENIED and the "read-only sandbox"/shell-guard prose;
every other event is ignored by this detector (M8 review D-C) -- a python
or rg loop over this repository's own source (which quotes these very
markers to document them) is neither of those two shapes, and must never
be mistaken for a live denial just because the read-only heuristic below
cannot classify it. A denial here almost always means the artifact the
agent was told to produce never landed, so this detector always reports two
findings when it fires: one naming the denial pattern itself
(R-CODEX-CONTRACT) and one naming the artifact risk it implies
(R-ASSERT-ARTIFACT). metrics: count, marker (the distinct marker labels
matched). Skips a read_only_output event (docs/DESIGN.md 4.3's
self-reference rule): a read-only Bash command (including one buried in a
loop, D-B) quoting WRITE_DENIED or the shell-guard sentence from a file on
disk is not a denial that happened to this agent. Also skips a `user` event
outright (D-B): a review or spec pasted into a prompt that happens to quote
WRITE_DENIED is not a sandbox denying anything.
"""

from __future__ import annotations

from ..markers import SANDBOX_WRITE_DENIED
from ..models import Event, Finding, Run
from .base import (
    KINDS_BOTH,
    Detector,
    evidence_line,
    is_bash_event,
    make_finding_id,
    preceding_bash_command,
    tooling_attempts,
)

ID = "codex_write_denied"
SEVERITY = "critical"
RULE_ID_DENIAL = "R-CODEX-CONTRACT"
RULE_ID_ARTIFACT = "R-ASSERT-ARTIFACT"
# reader.py's _scan_markers already ran SHELL_GUARD_BLOCK_REGEX against each
# event's full text and recorded a hit under this name.
_SHELL_GUARD_MARKER = "SHELL_GUARD_BLOCK_REGEX"


def _matched_marker(event: Event) -> str | None:
    if any(marker in event.markers for marker in SANDBOX_WRITE_DENIED):
        return "sandbox_write_denied"
    if _SHELL_GUARD_MARKER in event.markers:
        return "shell_guard_block"
    return None


class CodexWriteDenied:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_BOTH

    def run(self, run: Run) -> list[Finding]:
        hits: list[tuple[Event, str]] = []
        agent_id: str | None = None
        for attempt in tooling_attempts(run):
            for index, event in enumerate(attempt.events):
                # docs/DESIGN.md 4.3's D-C: a marker only ever counts in (a)
                # an assistant text event, or (b) a Bash tool_result event
                # whose own command mentions "codex" -- the Codex plugin is
                # the only producer of these markers, so every other event
                # (a user prompt, D-B; a non-Bash tool_result; a Bash
                # tool_result for an unrelated command that merely prints
                # text quoting one of these markers) is ignored outright.
                if event.kind == "assistant":
                    pass
                elif is_bash_event(event) and "codex" in preceding_bash_command(
                    attempt, index
                ).lower():
                    pass
                else:
                    continue
                if event.read_only_output:
                    # docs/DESIGN.md 4.3's self-reference rule: a read-only
                    # Bash command (including one buried in a loop, D-B)
                    # quoting WRITE_DENIED or the shell-guard sentence from a
                    # file on disk (this repository's own source, for
                    # example) is not a denial that happened to this agent.
                    continue
                marker = _matched_marker(event)
                if marker is None:
                    continue
                hits.append((event, marker))
                if agent_id is None:
                    agent_id = attempt.agent_id
        if not hits:
            return []

        count = len(hits)
        markers = sorted({marker for _, marker in hits})
        evidence = [evidence_line(run, event) for event, _ in hits]
        metrics = {"count": count, "marker": markers}
        plural = "s" if count != 1 else ""
        finding_id = make_finding_id(run.run_id, ID, agent_id, evidence[0] if evidence else None)

        denial_finding = Finding(
            detector=ID,
            severity=SEVERITY,
            title=f"{count} sandboxed write{plural} denied",
            run_id=run.run_id,
            agent_id=agent_id,
            evidence=evidence,
            metrics=metrics,
            rule_id=RULE_ID_DENIAL,
            finding_id=finding_id,
        )
        artifact_finding = Finding(
            detector=ID,
            severity=SEVERITY,
            title=f"{count} denied write{plural} may have left an artifact unwritten",
            run_id=run.run_id,
            agent_id=agent_id,
            evidence=evidence,
            metrics=metrics,
            rule_id=RULE_ID_ARTIFACT,
            finding_id=finding_id,
        )
        return [denial_finding, artifact_finding]


DETECTOR: Detector = CodexWriteDenied()
