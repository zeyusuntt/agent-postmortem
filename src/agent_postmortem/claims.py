"""What a returned result says about its own failures (docs/DESIGN.md 4.3, 4.13.4; G3, v0.3.1).

One shared predicate, used by the reader, the claim_contradiction detector, the audit and the
corpus check, so none of them can disagree about it. It reads the top level of a journal `result`
dict only: no free-text key, no nested value. Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass

# The keys the legacy claim_contradiction clauses read; the predicate leaves them to those clauses.
LEGACY_FAILED_KEYS = frozenset({"tests_failed", "suite_failed"})
_FAILED_SUFFIX = "_failed"


@dataclass(frozen=True)
class SelfReport:
    """`failed_counts`: how many top-level keys ending in `_failed` hold an int above 0 (a bool is
    not one; the legacy keys are excluded). `blocked`: the key `blocked` is exactly `true`."""

    failed_counts: int = 0
    blocked: bool = False

    def __bool__(self) -> bool:
        return self.failed_counts > 0 or self.blocked


def self_reported_problems(result: object) -> SelfReport:
    """The SelfReport of one journal `result` value (empty for anything but a dict)."""
    if not isinstance(result, dict):
        return SelfReport()
    counts = 0
    for key, value in result.items():
        if (
            isinstance(key, str)
            and key.endswith(_FAILED_SUFFIX)
            and key not in LEGACY_FAILED_KEYS
            and type(value) is int
            and value > 0
        ):
            counts += 1
    return SelfReport(failed_counts=counts, blocked=result.get("blocked") is True)
