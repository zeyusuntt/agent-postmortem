"""N6 (plan I2, row A3): the agent ids that resolve to a run through `agent-<id>` are
exactly the agent ids of its loaded Run, for every fixture and the demo, and each one
resolves again through its `a:` alias once registered."""

from __future__ import annotations

import re
import shutil
from importlib import resources
from pathlib import Path

import pytest

from agent_postmortem import cli, disclosure, reader
from tests import n3b_helpers as h
from tests.n6_helpers import (
    SLUG_A,
    aid,
    build_identity_root,
    uid,
    workflow_dir,
    write_lines,
)

FIXTURES = Path(__file__).parent / "fixtures" / "runs"


def _refs_of(case: Path) -> list[reader.RunRef]:
    if (case / "journal.jsonl").exists() or any(case.glob("agent-*.jsonl")):
        return [reader.RunRef(kind="workflow", path=case, run_id=case.name)]
    return reader.discover_runs(case)


def _all_cases():
    cases = [(path.name, _refs_of(path)) for path in sorted(FIXTURES.iterdir()) if path.is_dir()]
    return cases


def _canonical_attempt_ids(run) -> set[str]:
    return {a.agent_id for a in run.attempts if reader._AGENT_ID_RE.fullmatch(a.agent_id)}


def _check_refs(refs):
    checked = 0
    for ref in refs:
        try:
            run = reader.load_run(ref)
        except reader.NotARun:
            continue
        expected = _canonical_attempt_ids(run)
        universe = expected | set().union(*(reader.agent_id_candidates(r) for r in refs))
        resolving = {i for i in universe if ref in cli._runs_for_stem(i, refs)}
        assert resolving == expected, ref.path
        if expected:
            book = disclosure.register([("agent", i) for i in sorted(expected)])
            assert book.available
            for agent_id in sorted(expected):
                found = cli._runs_for_alias(book.alias("agent", agent_id), refs)
                assert found is not None and ref in found, (ref.path, agent_id)
        checked += 1
    return checked


_WITH_RUNS = [(name, refs) for name, refs in _all_cases() if refs]


@pytest.mark.parametrize("name, refs", _WITH_RUNS, ids=[name for name, _ in _WITH_RUNS])
def test_a3_resolving_ids_equal_the_loaded_attempt_ids_for_every_fixture(name, refs):
    _check_refs(refs)


def test_a3_resolving_ids_equal_the_loaded_attempt_ids_for_the_demo():
    demo = resources.files("agent_postmortem") / "demo" / "-home-user-project" / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_00000000-d30"
    with resources.as_file(demo) as path:
        refs = [reader.RunRef(kind="workflow", path=Path(path), run_id=Path(path).name)]
        assert _check_refs(refs) == 1


def test_a3_the_fixture_set_is_not_vacuous():
    total = sum(_check_refs(refs) for _name, refs in _all_cases())
    assert total >= 20


# --- the prescribed rows ---------------------------------------------------------------


def test_a3_a_canonical_agent_id_on_a_started_event_the_loader_rejects_resolves_nowhere(tmp_path):
    root = tmp_path / "r"
    run_dir = workflow_dir(root, SLUG_A, uid(9), "wf_00000000-6a5")
    write_lines(run_dir / "journal.jsonl", [
        {"type": "started", "key": 5, "agentId": aid(7)},
        {"type": "started", "key": "k", "agentId": aid(8)},
    ])
    run = reader.load_run(run_dir)
    assert {a.agent_id for a in run.attempts} == {aid(8)}
    refs = reader.discover_runs(root)
    book = disclosure.register([("agent", aid(7)), ("agent", aid(8))])
    assert cli._runs_for_stem(aid(7), refs) == []
    assert cli._runs_for_alias(book.alias("agent", aid(7)), refs) == []
    assert [r.path for r in cli._runs_for_stem(aid(8), refs)] == [run_dir]
    assert [r.path for r in cli._runs_for_alias(book.alias("agent", aid(8)), refs)] == [run_dir]


def test_a3_a_late_journal_read_failure_keeps_the_earlier_agent_resolvable(tmp_path):
    root = tmp_path / "r"
    run_dir = workflow_dir(root, SLUG_A, uid(9), "wf_00000000-6a5")
    padding = [{"type": "note", "pad": "x" * 900} for _ in range(20)]
    write_lines(run_dir / "journal.jsonl", [
        {"type": "started", "key": "k", "agentId": aid(8)}, *padding,
    ])
    with (run_dir / "journal.jsonl").open("ab") as handle:
        handle.write(b"\xff\xfe\n")
    run = reader.load_run(run_dir)
    assert {a.agent_id for a in run.attempts} == {aid(8)}  # the loader kept the prefix
    refs = reader.discover_runs(root)
    book = disclosure.register([("agent", aid(8))])
    assert [r.path for r in cli._runs_for_stem(aid(8), refs)] == [run_dir]
    assert [r.path for r in cli._runs_for_alias(book.alias("agent", aid(8)), refs)] == [run_dir]


def test_a3_a_stray_transcript_named_by_no_journal_event_is_no_match(tmp_path):
    ident = build_identity_root(tmp_path)
    refs = reader.discover_runs(ident.root)
    book = disclosure.register([("agent", ident.stray)])
    assert cli._runs_for_stem(ident.stray, refs) == []
    assert cli._runs_for_alias(book.alias("agent", ident.stray), refs) == []


def test_a3_aliases_in_run_share_text_resolve_to_a_run_holding_that_agent(tmp_path, capsys):
    root = tmp_path / "root"
    target = workflow_dir(root, SLUG_A, uid(9), "wf_00000000-d30")
    demo = resources.files("agent_postmortem") / "demo" / "-home-user-project" / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_00000000-d30"
    with resources.as_file(demo) as path:
        shutil.copytree(path, target)
    code, out, _err = h.cli(capsys, "run", str(target), "--root", root)
    aliases = sorted(set(re.findall(r"a:[0-9a-f]{12,64}", out)))
    assert len(aliases) >= 2
    for form in aliases:
        assert cli._resolve_run_path(form, root) == target
