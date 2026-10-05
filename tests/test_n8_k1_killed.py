"""N8a K1 (docs/DESIGN.md 4.13.8, 4.7 rule 5): the AGENT table's KILLED
column counts only the kills the runtime may own, an interrupt whose cause is
`runtime` or `unattributed`; a `parent_stop` or `human` interrupt is not one.
One row per cause, through the real table renderer."""

from __future__ import annotations

from agent_postmortem import report
from agent_postmortem.models import Interrupt
from agent_postmortem.redact import Redactor
from tests.n5b_helpers import model_attempt, model_workflow_run


def _interrupt(cause: str, silence: float | None) -> Interrupt:
    return Interrupt(
        kill_ts=None, preceding=None, silence_seconds=silence, cause=cause, cause_evidence=None,
    )


def _killed_cell(*causes: str | None, silence: float | None = 400.0) -> int:
    attempts = []
    for index, cause in enumerate(causes, start=1):
        attempts.append(model_attempt(
            index, ordinal=index, outcome="killed" if cause else "completed",
            interrupt=_interrupt(cause, silence) if cause else None,
        ))
    run = model_workflow_run(attempts)
    lines = report._agent_table(run, Redactor(enabled=False))
    row = lines[1]
    header = lines[0]
    start = header.index("KILLED")
    return int(row[start:start + 8].strip())


def test_k1_parent_stop_is_not_counted():
    assert _killed_cell("parent_stop") == 0


def test_k1_human_is_not_counted():
    assert _killed_cell("human") == 0


def test_k1_runtime_is_counted():
    assert _killed_cell("runtime") == 1


def test_k1_unattributed_short_silence_is_counted():
    assert _killed_cell("unattributed", silence=20.0) == 1


def test_k1_unattributed_long_silence_is_counted():
    assert _killed_cell("unattributed", silence=900.0) == 1


def test_k1_mixed_causes_count_only_the_runtime_owned_ones():
    assert _killed_cell("parent_stop", "human", "runtime", "unattributed", None) == 2


def test_k1_no_interrupt_is_zero():
    assert _killed_cell(None) == 0


def test_k1_the_predicate_is_one_helper():
    assert report._runtime_owned_kill(
        model_attempt(1, interrupt=_interrupt("runtime", 1.0)),
    ) is True
    assert report._runtime_owned_kill(model_attempt(1)) is False
