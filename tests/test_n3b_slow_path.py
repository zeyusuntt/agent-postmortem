"""N3b P9: identity collection. A one-shot text command collects every
identity its render may show and registers them in one locked registration;
the renderer then asks only for collected identities, so AliasBook's slow path
(one more locked registration) never runs. The counter must stay 0 for `run`
text, `run --verbose --evidence`, and `run --bug-report` over the packaged
demo and every fixture run, so a missed collection site fails here.

Parametrized over the fixture directory listing: a new fixture is covered
automatically.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest

from agent_postmortem import disclosure, reader
from agent_postmortem.cli import main

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "runs"
CASES = sorted(path.name for path in FIXTURE_ROOT.iterdir() if path.is_dir())
FLAG_SETS = {
    "text": [],
    "verbose evidence": ["--verbose", "--evidence"],
    "bug report": ["--bug-report"],
}


# Fixture directories that deliberately hold no run reader.discover_runs can
# find (a bridge-only session transcript is not a run: `run` answers "is not a
# run"). Anything else with no run fails the test, so a new fixture directory is
# covered automatically or has to be listed here on purpose.
NOT_A_RUN = frozenset({"session_bridge_only"})


def _runs_in(case_dir: Path) -> list[Path]:
    refs = reader.discover_runs(case_dir)
    if refs:
        return [ref.path for ref in refs]
    return [case_dir] if (case_dir / "journal.jsonl").exists() else []


@pytest.fixture
def books(monkeypatch):
    """Every AliasBook disclosure.register hands out, in order."""
    created: list[disclosure.AliasBook] = []
    real_register = disclosure.register

    def recording_register(identities, **kwargs):
        book = real_register(identities, **kwargs)
        created.append(book)
        return book

    monkeypatch.setattr(disclosure, "register", recording_register)
    return created


@pytest.mark.parametrize("flags", sorted(FLAG_SETS))
@pytest.mark.parametrize("case", CASES)
def test_no_fixture_run_takes_the_slow_path(case, flags, books, capsys):
    runs = _runs_in(FIXTURE_ROOT / case)
    if case in NOT_A_RUN:
        assert not runs
        return
    assert runs, f"{case}: no run found; a fixture directory must hold a run or be in NOT_A_RUN"
    for run_path in runs:
        books.clear()
        capsys.readouterr()
        code = main(["run", str(run_path), *FLAG_SETS[flags]])
        assert code in (0, 1, 3, 4), f"{case}: exit {code}"
        assert len(books) == 1, f"{case}: one registration per command, got {len(books)}"
        assert books[0].available
        assert books[0].slow_path_count == 0, f"{case}: a renderer asked for an uncollected id"


@pytest.mark.parametrize("flags", sorted(FLAG_SETS))
def test_the_packaged_demo_run_takes_no_slow_path(flags, books, capsys):
    ref = resources.files("agent_postmortem") / "demo" / "-home-user-project" / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_00000000-d30"
    with resources.as_file(ref) as demo_path:
        capsys.readouterr()
        main(["run", str(demo_path), *FLAG_SETS[flags]])
    assert len(books) == 1
    assert books[0].slow_path_count == 0


@pytest.mark.parametrize("level", ["--local", "--raw"])
def test_json_commands_register_nothing_at_local_and_raw(level, books, capsys):
    # N5a-1: share JSON registers its aliases like text (tests/test_n5a1_schema3.py).
    ref = resources.files("agent_postmortem") / "demo" / "-home-user-project" / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_00000000-d30"
    with resources.as_file(ref) as demo_path:
        main(["run", str(demo_path), "--json", level])
    assert books == []


@pytest.mark.parametrize("level", ["--local", "--raw"])
def test_local_and_raw_register_nothing(level, books, capsys):
    ref = resources.files("agent_postmortem") / "demo" / "-home-user-project" / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_00000000-d30"
    with resources.as_file(ref) as demo_path:
        main(["run", str(demo_path), level, "--verbose", "--evidence"])
    assert books == []
