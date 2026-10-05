"""Detects an attempt told to write a file that never showed up.

DESIGN.md 4.3, 4.13.4: info severity (D2), fires when an attempt's first user
event names an absolute path it must write (a write/save/append/create/persist
verb followed within 80 chars by a path ending .md/.json/.txt/.jsonl) and the
path is not present at scan time. This is the only detector that touches
anything outside the run directory, with claim_contradiction: one
severity.absence() (an os.stat) on the named path and nothing else. The
check is `observed_later`: the file may have existed when the run ended and
been moved or removed since, and the finding's title says so. The `verified`
metric records whether the path is under the home directory or /tmp.
rule_id: R-ASSERT-ARTIFACT.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from .. import severity as severity_rules
from ..models import Attempt, Event, Finding, Run, TitleParts
from .base import KINDS_BOTH, Detector, evidence_line, make_finding_id

ID = "missing_artifact"
SEVERITY = "info"  # the highest (only) row of docs/DESIGN.md 4.3
RULE_ID = "R-ASSERT-ARTIFACT"
_MAX_GAP = 80

# A write/save/append/create/persist verb, then up to _MAX_GAP arbitrary
# characters, then an absolute path ending in one of the artifact
# extensions. The path itself is not length-limited, only its start. The
# trailing (?!...) boundary keeps a longer filename that merely contains one
# of the extensions (report.md.backup) from being truncated into a shorter
# path that does not actually exist as its own file (report.md).
_ARTIFACT_PATH_RE = re.compile(
    r"""\b(?:write|save|append|create|persist)\b.{0,80}?(/[^\s,;:"'()]+\.(?:md|json|txt|jsonl))(?![A-Za-z0-9_.-])""",
    re.IGNORECASE | re.DOTALL,
)

# A negation immediately before the verb ("Do not write ..."). Checked in a
# short fixed window before the verb rather than a variable-length regex
# lookbehind, which Python's re module does not support.
_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|does\s+not|don't|never|must\s+not|should\s+not|won't|cannot|can't)\s*$",
    re.IGNORECASE,
)
_NEGATION_WINDOW = 20

# /tmp is kept literally (it is the conventional POSIX temp root real
# prompts name, and what DESIGN.md's own fixtures use) and supplemented with
# tempfile.gettempdir(), since on some machines and on any non-POSIX system
# the OS temp root is elsewhere. gettempdir() is resolved lazily, inside
# _allowed_roots() below, not here at import time: some sandboxes have no
# discoverable temp directory at all, and gettempdir() raises in that case
# (docs/DESIGN.md 4.3). Since detectors/__init__.py imports every detector
# module eagerly, a module-level call here would crash importing
# agent_postmortem itself -- including `agent-postmortem demo`, which needs
# no filesystem access at all.
_STATIC_ALLOWED_ROOTS = (Path.home(), Path("/tmp"))


def _allowed_roots() -> tuple[Path, ...]:
    """Path.home() and /tmp, plus tempfile.gettempdir() when it can be
    resolved. A gettempdir() failure is tolerated as simply 'no extra temp
    root' rather than propagated -- it is not evidence that a path is
    untrusted, only that this one extra check could not run."""
    try:
        extra = Path(tempfile.gettempdir())
    except OSError:
        return _STATIC_ALLOWED_ROOTS
    return _STATIC_ALLOWED_ROOTS + (extra,)

def _first_user_event(attempt: Attempt) -> Event | None:
    for event in attempt.events:
        if event.kind == "user":
            return event
    return None


def _is_under_allowed_root(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for root in _allowed_roots():
        try:
            resolved.relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


_ABSENT_TEMPLATE = (
    "{path} is absent now; the tool cannot tell whether it existed when the run ended"
)
_UNCHECKED_TEMPLATE = (
    "{path} could not be checked now; the tool cannot tell whether it existed "
    "when the run ended"
)


def _title_parts(path: str, state: str) -> TitleParts:
    """D2 (docs/DESIGN.md 4.13.4): a check made now says nothing about the end of the run. The
    title is a fixed template and the path (F fix 7, W7-2): a renderer formats it once and never
    scans the result."""
    template = _ABSENT_TEMPLATE if state == "absent_now" else _UNCHECKED_TEMPLATE
    return TitleParts(template, path)


def _title(path: str, state: str) -> str:
    """The title with the real path (what `raw` prints)."""
    return _title_parts(path, state).raw()


class MissingArtifact:
    id = ID
    severity_default = SEVERITY
    kinds = KINDS_BOTH

    def run(self, run: Run) -> list[Finding]:
        findings: list[Finding] = []
        for attempt in run.attempts:
            event = _first_user_event(attempt)
            if event is None:
                continue
            for match in _ARTIFACT_PATH_RE.finditer(event.text):
                preceding = event.text[max(0, match.start() - _NEGATION_WINDOW):match.start()]
                if _NEGATION_RE.search(preceding):
                    continue
                path_str = match.group(1)
                state = severity_rules.absence(path_str)
                if state == "present_now":
                    continue
                verified = _is_under_allowed_root(Path(path_str))
                severity = severity_rules.resolve(
                    run, ID, obligation="prompt_path", absence=state,
                )
                evidence = [evidence_line(run, event)]
                parts = _title_parts(path_str, state)
                findings.append(
                    Finding(
                        detector=ID,
                        severity=severity,
                        title=parts.raw(),
                        title_parts=parts,
                        run_id=run.run_id,
                        agent_id=attempt.agent_id,
                        evidence=evidence,
                        metrics={"paths": [path_str], "verified": verified},
                        rule_id=RULE_ID,
                        finding_id=make_finding_id(run.run_id, ID, attempt.agent_id, evidence[0]),
                    )
                )
        return findings


DETECTOR: Detector = MissingArtifact()
