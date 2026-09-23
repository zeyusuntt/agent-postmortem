"""Detector registry.

Adding a detector means adding a module to this package that exports a
module-level DETECTOR instance; nothing here needs to change. all_detectors()
imports every module in the package and collects the ones that export it.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

from .base import Detector

if TYPE_CHECKING:
    from ..models import Finding, Run

__all__ = [
    "Detector", "all_detectors", "run_detectors", "run_tooling_detectors", "TOOLING_DETECTOR_IDS",
]

# docs/DESIGN.md 4.3: the seven tooling detectors, scanned by a session's
# main thread as well as every child attempt. Named here once so a caller
# that must run only these (docs/DESIGN.md 7.2's session-end hook, D5) does
# not have to spell the list out itself.
TOOLING_DETECTOR_IDS: frozenset[str] = frozenset({
    "bash_backgrounded",
    "bash_timeout",
    "tool_json_error",
    "structured_output_failure",
    "codex_write_denied",
    "classifier_unavailable",
    "git_cwd_mismatch",
})


def all_detectors() -> list[Detector]:
    """Return every registered detector, sorted by id.

    Returns an empty list when the package holds no detector modules (v0
    ships this package with base.py only; detector modules land later).
    """
    found: list[Detector] = []
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name in ("base", "__init__"):
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        detector = getattr(module, "DETECTOR", None)
        if detector is not None:
            found.append(detector)
    return sorted(found, key=lambda detector: detector.id)


def run_detectors(run: "Run") -> list["Finding"]:
    """Every finding from every registered detector whose kinds include
    run.kind (docs/DESIGN.md 4.3): the one place cli.py, report.py, and
    trend.py all get findings from, so a run kind's applicable detectors are
    never decided in more than one place.
    """
    findings: list[Finding] = []
    for detector in all_detectors():
        if run.kind in detector.kinds:
            findings.extend(detector.run(run))
    return findings


def run_tooling_detectors(run: "Run") -> list["Finding"]:
    """Every finding from just the seven tooling detectors (docs/
    DESIGN.md 4.3, TOOLING_DETECTOR_IDS), for a caller that must not run
    mislabeled_kill or missing_artifact -- docs/DESIGN.md 7.2's session-end
    hook (D5), which analyzes only the main thread and has no children to
    attribute an interrupt or a missing artifact to."""
    findings: list[Finding] = []
    for detector in all_detectors():
        if detector.id in TOOLING_DETECTOR_IDS and run.kind in detector.kinds:
            findings.extend(detector.run(run))
    return findings
