"""N3b P9 for the Part 2 sinks: one registration per command over every
identity its text may show, and the slow-path counter stays 0 over the
packaged demo and every fixture run, so a missed collection site fails here.

`scan --all` (projects), `rescue plan` and `rescue status` (the run's
identities) register exactly once; `scan` triage, `rules`, `rules --show`,
`rules --run`, `rules propose`, `rules review`, `trend` and `apply` print no
identity, so they register nothing. Parametrized over the fixture directory
listing: a new fixture is covered automatically. (Written after the code it
checks; mutation-checked by hand, see the Part 2 report.)
"""

from __future__ import annotations

from importlib import resources

import pytest

from agent_postmortem import disclosure, reader
from agent_postmortem.cli import main
from tests.n3b_helpers import CASES, FIXTURE_ROOT

# a bridge-only session transcript is not a run (see test_n3b_slow_path.py)
NOT_A_RUN = frozenset({"session_bridge_only"})


@pytest.fixture
def books(monkeypatch):
    created: list[disclosure.AliasBook] = []
    real_register = disclosure.register

    def recording_register(identities, **kwargs):
        book = real_register(identities, **kwargs)
        created.append(book)
        return book

    monkeypatch.setattr(disclosure, "register", recording_register)
    return created


def _run_paths(case: str):
    case_dir = FIXTURE_ROOT / case
    refs = reader.discover_runs(case_dir)
    if refs:
        return [ref.path for ref in refs]
    return [case_dir] if (case_dir / "journal.jsonl").exists() else []


def _check(books, *, registers: bool):
    assert len(books) == (1 if registers else 0)
    for book in books:
        assert book.available
        assert book.slow_path_count == 0


@pytest.mark.parametrize("case", CASES)
def test_scan_over_every_fixture_root(case, books, capsys):
    root = FIXTURE_ROOT / case
    if case in NOT_A_RUN:
        return
    if not reader.discover_runs(root):
        # a fixture that is one flat run directory is not a scan root; the
        # rescue test below covers it as a run, and an empty root registers
        # nothing
        capsys.readouterr()
        assert main(["scan", "--all", "--root", str(root)]) == 2
        _check(books, registers=False)
        return
    capsys.readouterr()
    main(["scan", "--all", "--root", str(root)])
    _check(books, registers=True)
    books.clear()
    main(["scan", "--root", str(root)])
    _check(books, registers=False)
    books.clear()
    main(["trend", "--root", str(root)])
    main(["rules", "--all", "--root", str(root)])
    main(["rules", "propose", "--root", str(root)])
    _check(books, registers=False)


@pytest.mark.parametrize("verb", ["plan", "status"])
@pytest.mark.parametrize("case", CASES)
def test_rescue_over_every_fixture_run(case, verb, books, capsys):
    if case in NOT_A_RUN:
        return
    runs = _run_paths(case)
    assert runs, f"{case}: no run found; list it in NOT_A_RUN on purpose"
    for run_path in runs:
        books.clear()
        capsys.readouterr()
        code = main(["rescue", verb, str(run_path)])
        assert code in (0, 1, 2, 3, 4), f"{case}: exit {code}"
        _check(books, registers=True)
        books.clear()
        main(["rules", "--run", str(run_path)])
        _check(books, registers=False)


@pytest.mark.parametrize("verb", ["plan", "status"])
def test_rescue_over_the_packaged_demo_run(verb, books, capsys):
    ref = resources.files("agent_postmortem") / "demo" / "-home-user-project" / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_00000000-d30"
    with resources.as_file(ref) as demo_path:
        capsys.readouterr()
        main(["rescue", verb, str(demo_path)])
        _check(books, registers=True)
        books.clear()
        main(["rules", "--run", str(demo_path)])
        _check(books, registers=False)


# N5a-1: JSON at share registers like its text sibling (see tests/test_n5a1_schema3.py);
# JSON at local and raw still registers nothing.
@pytest.mark.parametrize(
    "flags", [["--local"], ["--raw"], ["--json", "--local"], ["--json", "--raw"]],
)
def test_other_levels_and_json_register_nothing_for_every_part_2_sink(flags, books, capsys):
    root = FIXTURE_ROOT / "reader_minimal"
    run = _run_paths("reader_minimal")[0]
    for argv in (
        ["scan", "--all", "--root", str(root)],
        ["scan", "--root", str(root)],
        ["rescue", "plan", str(run)],
        ["rescue", "status", str(run)],
        ["trend", "--root", str(root)],
        ["rules", "--root", str(root)],
        ["rules", "propose", "--root", str(root)],
    ):
        main([*argv, *flags])
    assert books == []
