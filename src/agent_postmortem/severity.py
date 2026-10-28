"""Severity as a function of recorded inputs (docs/DESIGN.md 4.3 "Inputs and
rule per detector", 4.13.4; milestone N5b).

Every detector asks `resolve()` for its finding's severity, passing the inputs
its row names; none of them chooses a severity any other way. A tuple no row
lists (including a value outside an input's vocabulary) is a contract error:
the answer is the detector's highest row, the run's status becomes
`INCOMPLETE`, and the run's diagnostics gain one line naming the detector and
the tuple. The module also holds the readers of the derived inputs
(`recovered`, `work_risk`, `absence`, `position`, `silence_class`), each one
fail-closed: whatever cannot be established is `unknown`.

Standard library only; no filesystem access except the one `os.stat` in
`absence`.
"""

from __future__ import annotations

import os

from . import ledger
from .models import Attempt, Run

SEVERITY_ORDER = ("critical", "major", "minor", "info")

# The one 300 s silence rule of docs/DESIGN.md 4.4.
LONG_SILENCE_SECONDS = 300.0

_TERMINAL_WORDS = frozenset({"completed", "failed", "stopped", "journal_resolved"})

# 4.13.4: the ledger effects that make `work_risk` present.
_PRESENT_EFFECTS = frozenset({"observed", "claimed_target", "attempted"})

# Vocabulary of every input (4.13.4 and the 4.3 table). A value outside its
# vocabulary matches no row.
VOCAB: dict[str, frozenset[str]] = {
    "terminal": frozenset({
        "completed", "failed", "stopped", "journal_resolved", "unknown", "none",
    }),
    "recovered": frozenset({"yes", "no", "unknown"}),
    "work_risk": frozenset({"none", "present", "unknown"}),
    "cause": frozenset({"runtime", "unattributed", "parent_stop", "human"}),
    "silence": frozenset({"long", "short", "unknown"}),
    "position": frozenset({"final", "non_final"}),
    "clause": frozenset({
        "tests_failed", "suite_failed", "ok_false", "ready_false", "blockers",
        "report_path_missing", "self_reported_failure", "self_reported_blocked",
    }),
    "absence": frozenset({"present_now", "absent_now", "unknown", "none"}),
    "obligation": frozenset({"none", "prompt_path", "claimed_path"}),
    "outcome": frozenset({"completed", "other"}),
    "later_success": frozenset({"yes", "no"}),
}

# detector id -> its input names, in the order the diagnostic prints them.
INPUT_NAMES: dict[str, tuple[str, ...]] = {
    "workflow_failed": ("terminal",),
    "attempt_failed": ("recovered",),
    "silent_retry": ("recovered", "work_risk"),
    "mislabeled_kill": ("cause", "silence", "position"),
    "claim_contradiction": ("clause", "absence"),
    "missing_artifact": ("obligation", "absence"),
    "structured_output_failure": ("recovered",),
    "codex_write_denied": (),
    "codex_unavailable": ("outcome", "later_success"),
    "bash_backgrounded": (),
    "bash_timeout": (),
    "tool_json_error": (),
    "classifier_unavailable": (),
    "git_cwd_mismatch": (),
    "permission_denied": (),
    "wasted_time": (),
}

_ANY = None  # a row element that matches every value in the input's vocabulary

# detector id -> rows of (one element per input, severity). An element is a value,
# a tuple of values, or _ANY. First match wins (the rows never overlap).
RULES: dict[str, list[tuple[tuple, str]]] = {
    "workflow_failed": [
        (("failed",), "major"),
    ],
    "attempt_failed": [
        (("no",), "major"),
        (("unknown",), "major"),
        (("yes",), "minor"),
    ],
    "silent_retry": [
        ((("no", "unknown"), _ANY), "major"),
        (("yes", ("present", "unknown")), "major"),
        (("yes", "none"), "minor"),
    ],
    "mislabeled_kill": [
        ((("runtime", "unattributed"), "long", "final"), "major"),
        ((("runtime", "unattributed"), "long", "non_final"), "minor"),
        ((("runtime", "unattributed"), ("short", "unknown"), _ANY), "info"),
        ((("parent_stop", "human"), _ANY, _ANY), "info"),
    ],
    "claim_contradiction": [
        ((("tests_failed", "suite_failed", "ok_false", "ready_false", "blockers",
           "self_reported_failure", "self_reported_blocked"), "none"),
         "major"),
        (("report_path_missing", ("absent_now", "unknown")), "info"),
    ],
    "missing_artifact": [
        (("prompt_path", ("absent_now", "unknown")), "info"),
    ],
    "structured_output_failure": [
        (("no",), "major"),
        (("unknown",), "major"),
        (("yes",), "minor"),
    ],
    "codex_write_denied": [((), "minor")],
    "codex_unavailable": [
        (("completed", "no"), "major"),
        (("completed", "yes"), "minor"),
        (("other", _ANY), "minor"),
    ],
    "bash_backgrounded": [((), "minor")],
    "bash_timeout": [((), "minor")],
    "tool_json_error": [((), "minor")],
    "classifier_unavailable": [((), "minor")],
    "git_cwd_mismatch": [((), "minor")],
    "permission_denied": [((), "minor")],
    "wasted_time": [((), "info")],
}


def highest(detector: str) -> str:
    """The most severe row the detector has: what a contract error answers."""
    severities = {severity for _pattern, severity in RULES[detector]}
    return next(level for level in SEVERITY_ORDER if level in severities)


def worst(levels: list[str]) -> str:
    """The most severe of several severities (a finding with several parts)."""
    return next(level for level in SEVERITY_ORDER if level in levels)


def _element_matches(element, name: str, value) -> bool:
    if not isinstance(value, str) or value not in VOCAB[name]:
        return False
    if element is _ANY:
        return True
    if isinstance(element, tuple):
        return value in element
    return value == element


def _row_for(detector: str, inputs: dict) -> str | None:
    names = INPUT_NAMES[detector]
    if set(inputs) != set(names):
        return None
    for pattern, severity in RULES[detector]:
        if all(
            _element_matches(element, name, inputs[name])
            for element, name in zip(pattern, names)
        ):
            return severity
    return None


def resolve(run: Run, detector: str, **inputs) -> str:
    """The severity the table gives `detector` for these inputs. A tuple the table
    does not list is a contract error (4.13.4): the highest row, the run's status
    `INCOMPLETE`, and one diagnostic per distinct detector and tuple. An unknown
    detector id is a programming error (KeyError), not a contract error."""
    names = INPUT_NAMES[detector]
    found = _row_for(detector, inputs)
    if found is not None:
        return found
    answer = highest(detector)
    shown = ", ".join(f"{name}={inputs.get(name, '<missing>')}" for name in names)
    line = (
        f"severity: {detector} has no row for ({shown}); used the highest row, {answer}"
    )
    if line not in run.diagnostics:
        run.diagnostics.append(line)
    run.status = "INCOMPLETE"
    return answer


# --- derived inputs -----------------------------------------------------------------------


def silence_class(seconds: float | None) -> str:
    """`long` at 300 s or more (the one 4.4 rule), `short` below, `unknown` for None."""
    if seconds is None:
        return "unknown"
    return "long" if seconds >= LONG_SILENCE_SECONDS else "short"


def run_is_terminal(run: Run) -> bool:
    """The run's lifecycle terminal (4.13.3) is one of completed, failed, stopped,
    journal_resolved. A session has no lifecycle and is never terminal."""
    lifecycle = run.lifecycle
    return lifecycle is not None and lifecycle.terminal in _TERMINAL_WORDS


def _same_key(run: Run, attempt: Attempt) -> list[Attempt]:
    if attempt.key is None:
        return [attempt]
    group = [other for other in run.attempts if other.key == attempt.key]
    return sorted(group, key=lambda other: other.ordinal)


def _completed_with_result(attempt: Attempt) -> bool:
    """The journal closed this attempt with a `result` event. An attempt with no
    journal record never completes with a result: nothing recorded one."""
    return attempt.journal_state == "completed"


def position(run: Run, attempt: Attempt) -> str:
    """`final` when no attempt of the same key has a higher ordinal (or the
    attempt has no key), `non_final` otherwise."""
    if attempt.key is None:
        return "final"
    later = any(
        other.key == attempt.key and other.ordinal > attempt.ordinal for other in run.attempts
    )
    return "non_final" if later else "final"


def recovered(run: Run, attempt: Attempt, *, include_self: bool) -> str:
    """`yes`, `no`, or `unknown` (4.13.4, 4.3 "How the derived inputs are read").

    Candidates are the attempts of the same key with a higher ordinal, plus the
    attempt itself when `include_self`. `yes` when any candidate completed with a
    result. Otherwise the key's last attempt decides: `no` when it ended without
    completing or the run is terminal, `unknown` when it has no terminal record
    and the run is not terminal."""
    group = _same_key(run, attempt)
    later = [other for other in group if other.ordinal > attempt.ordinal]
    candidates = ([attempt] if include_self else []) + later
    if any(_completed_with_result(candidate) for candidate in candidates):
        return "yes"
    last = later[-1] if later else attempt
    if last.journal_state in ("failed", "superseded") or last.outcome in (
        "killed", "failed", "superseded",
    ):
        return "no"
    if run_is_terminal(run):
        return "no"
    return "unknown"


def work_risk(run: Run) -> str:
    """Run-level, in S2's order: a partial population or any partial (or absent)
    population ledger is `unknown`; otherwise an `observed`, `claimed_target`, or
    `attempted` entry in a population ledger is `present`; otherwise `none`."""
    if run.risk_population_completeness != "complete":
        return "unknown"
    population = ledger.risk_population(run)
    ledgers = [attempt.ledger for attempt in population]
    if any(item is None or item.completeness != "complete" for item in ledgers):
        return "unknown"
    for item in ledgers:
        if any(entry.effect in _PRESENT_EFFECTS for entry in item.entries):
            return "present"
    return "none"


def absence(path: str) -> str:
    """`present_now`, `absent_now`, or `unknown` for one path. Not absolute, a
    stat error other than not-found, or a path stat rejects outright is
    `unknown`; every check is `observed_later` (D2), never an end-time fact."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return "unknown"
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "absent_now"
    except (OSError, ValueError):
        return "unknown"
    return "present_now"
