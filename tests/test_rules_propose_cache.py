"""`rules propose` reads the fleet through scan's run cache (docs/DESIGN.md 9, 4.14.9; field
report 2026-09-30, "rules propose: slow, history-wide": 13 s then, 101 s on 2026-10-30 for 382
runs, every run loaded cold on every call).

A second call with no input changed loads no run and prints exactly what the first printed; a
run whose files changed is loaded again. Each finding's rule ids are part of the cached facts.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_postmortem import reader
from agent_postmortem.cli import main
from tests import rule_fixtures

DAY1 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def _fleet(tmp_path):
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e06", DAY1, agent_n=1)
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e07", DAY1, agent_n=2)
    rule_fixtures.build_git_cwd_mismatch_run(tmp_path, "wf_00000000-e08", DAY2, agent_n=3)


def _loads(monkeypatch) -> list[str]:
    loaded: list[str] = []
    real = reader.load_run

    def counting(ref):
        loaded.append(getattr(ref, "run_id", str(ref)))
        return real(ref)

    monkeypatch.setattr(reader, "load_run", counting)
    return loaded


def _propose(capsys, tmp_path, *extra) -> tuple[int, str]:
    code = main(["rules", "propose", "--root", str(tmp_path), *extra])
    return code, capsys.readouterr().out


def test_a_second_propose_loads_no_run_and_prints_the_same(tmp_path, capsys, monkeypatch):
    _fleet(tmp_path)
    cold = _propose(capsys, tmp_path)
    assert cold[0] == 0 and "R-GIT-CWD" in cold[1]
    loaded = _loads(monkeypatch)
    assert _propose(capsys, tmp_path) == cold
    assert loaded == []
    assert _propose(capsys, tmp_path, "--json") == _propose(capsys, tmp_path, "--json")


def test_a_warm_scan_also_warms_propose(tmp_path, capsys, monkeypatch):
    _fleet(tmp_path)
    main(["scan", "--root", str(tmp_path)])
    capsys.readouterr()
    loaded = _loads(monkeypatch)
    code, out = _propose(capsys, tmp_path)
    assert code == 0 and "R-GIT-CWD" in out
    assert loaded == []


def test_a_run_whose_files_changed_is_loaded_again(tmp_path, capsys, monkeypatch):
    _fleet(tmp_path)
    _propose(capsys, tmp_path)
    transcript = next(tmp_path.rglob("wf_00000000-e07/agent-*.jsonl"))
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    loaded = _loads(monkeypatch)
    _propose(capsys, tmp_path)
    assert loaded == ["wf_00000000-e07"]
