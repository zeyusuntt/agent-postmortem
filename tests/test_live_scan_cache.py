"""docs/DESIGN.md 4.14.9 item 3: scan's run cache. A warm scan prints exactly what a cold scan
prints, reads no run whose inputs are unchanged, and reads again every run whose inputs moved:
an append, a same-size rewrite, a replacement, a parent transcript change, a config change, a
filesystem probe that now answers differently, the 72-hour report window closing, a new
package version."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from live_helpers import (
    FAKE_UUID,
    aid,
    assistant_text,
    backdate_tree,
    user,
    write_clean_workflow,
    write_jsonl,
)

from agent_postmortem import cli, reader, state
from agent_postmortem.cli import main

SLUG = "-home-user-project"
DEMO = Path(__file__).resolve().parent.parent / "src" / "agent_postmortem" / "demo"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    shutil.copytree(DEMO / "-home-user-project", root / "-home-user-demo")
    now = datetime.now(timezone.utc)
    write_clean_workflow(root, SLUG, "wf_00000000-753", now - timedelta(days=2), agent=61)
    old = write_clean_workflow(root, SLUG, "wf_00000000-754", now - timedelta(days=30), agent=62)
    backdate_tree(old, now - timedelta(days=30))
    return root


def _loads(monkeypatch) -> list[str]:
    loaded: list[str] = []
    real = reader.load_run

    def counting(ref):
        loaded.append(getattr(ref, "run_id", str(ref)))
        return real(ref)

    monkeypatch.setattr(reader, "load_run", counting)
    return loaded


def _scan(capsys, root, *extra) -> str:
    main(["scan", "--root", str(root), *extra])
    return capsys.readouterr().out


@pytest.mark.parametrize("extra", [(), ("--all",), ("--json",)])
def test_a_warm_scan_prints_what_a_cold_scan_prints(tmp_path, capsys, monkeypatch, extra):
    root = _root(tmp_path)
    cold = _scan(capsys, root, *extra)
    loaded = _loads(monkeypatch)
    warm = _scan(capsys, root, *extra)
    assert warm == cold
    assert loaded == []
    bypass = _scan(capsys, root, *extra, "--no-cache")
    assert bypass == cold
    assert len(loaded) >= 3


def test_an_append_reads_only_that_run_again(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    _scan(capsys, root)
    run_dir = root / SLUG / FAKE_UUID / "subagents" / "workflows" / "wf_00000000-753"
    agent = aid(61)
    write_jsonl(run_dir / f"agent-{agent}.jsonl",
                [assistant_text(agent, datetime.now(timezone.utc), "more")], mode="a")
    loaded = _loads(monkeypatch)
    _scan(capsys, root)
    assert loaded == ["wf_00000000-753"]


def test_a_same_size_rewrite_with_the_old_mtime_is_still_read_again(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    _scan(capsys, root)
    journal = root / SLUG / FAKE_UUID / "subagents" / "workflows" / "wf_00000000-754" / "journal.jsonl"
    st = journal.stat()
    text = journal.read_text(encoding="utf-8")
    journal.write_text(text.replace('"ok": true', '"ok": 1234'), encoding="utf-8")
    assert journal.stat().st_size == st.st_size
    os.utime(journal, ns=(st.st_atime_ns, st.st_mtime_ns))
    loaded = _loads(monkeypatch)
    _scan(capsys, root)
    assert "wf_00000000-754" in loaded


def test_a_parent_transcript_change_reads_its_workflows_again(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    _scan(capsys, root)
    parent = root / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(parent, [user(None, datetime.now(timezone.utc), "hello")], mode="a")
    loaded = _loads(monkeypatch)
    _scan(capsys, root)
    assert "wf_00000000-753" in loaded and "wf_00000000-754" in loaded


def test_a_config_change_reads_everything_again(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    _scan(capsys, root)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[detectors]\noff = ["wasted_time"]\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(cfg))
    loaded = _loads(monkeypatch)
    _scan(capsys, root)
    assert len(loaded) >= 3


def test_a_probe_that_answers_differently_reads_the_run_again(tmp_path, capsys, monkeypatch):
    root = tmp_path / "projects"
    now = datetime.now(timezone.utc)
    target = tmp_path / "out" / "report.md"
    run_dir = write_clean_workflow(root, SLUG, "wf_00000000-755", now - timedelta(days=5), agent=63)
    agent = aid(63)
    write_jsonl(run_dir / f"agent-{agent}.jsonl", [
        user(agent, now - timedelta(days=5), f"Write the report to {target}"),
        assistant_text(agent, now - timedelta(days=5), "done"),
    ])
    backdate_tree(run_dir, now - timedelta(days=5))
    first = _scan(capsys, root, "--json")
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    loaded = _loads(monkeypatch)
    second = _scan(capsys, root, "--json")
    assert loaded == ["wf_00000000-755"]
    assert json.loads(first)["totals"]["findings_per_detector"].get("missing_artifact") == 1
    assert "missing_artifact" not in json.loads(second)["totals"]["findings_per_detector"]


def test_an_entry_expires_when_the_report_window_closes(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    _scan(capsys, root)
    later = datetime.now(timezone.utc) + timedelta(days=4)
    from agent_postmortem import report
    from agent_postmortem.detectors import claim_contradiction

    monkeypatch.setattr(report, "_now", lambda: later)
    monkeypatch.setattr(claim_contradiction, "_now", lambda: later)
    loaded = _loads(monkeypatch)
    _scan(capsys, root)
    assert "wf_00000000-753" in loaded  # ended 2 days ago: inside the window when cached
    assert "wf_00000000-754" not in loaded  # ended 30 days ago: never inside it


def test_a_new_package_version_reads_everything_again(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    _scan(capsys, root)
    monkeypatch.setattr(cli, "__version__", "99.0.0")
    loaded = _loads(monkeypatch)
    _scan(capsys, root)
    assert len(loaded) >= 3


def test_here_never_uses_the_cache(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    _scan(capsys, root)
    monkeypatch.chdir(tmp_path)
    loaded = _loads(monkeypatch)
    main(["scan", "--root", str(root), "--here"])
    capsys.readouterr()
    assert len(loaded) >= 3


def test_the_cache_is_a_private_file_beside_state_json(tmp_path, capsys):
    root = _root(tmp_path)
    _scan(capsys, root)
    path = state.scan_cache_path()
    assert path.is_file()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == cli._SCAN_CACHE_SCHEMA
    assert len(data["runs"]) >= 3


def test_a_workflow_directory_that_disappears_reads_its_session_again(tmp_path, capsys, monkeypatch):
    """A session's launch reconciliation reads which workflow directories exist (4.14.9)."""
    root = _root(tmp_path)
    _scan(capsys, root, "--json")
    demo_runs = root / "-home-user-demo" / FAKE_UUID / "subagents" / "workflows"
    shutil.move(str(demo_runs), str(tmp_path / "moved-away"))
    warm = _scan(capsys, root, "--json")
    cold = _scan(capsys, root, "--json", "--no-cache")
    assert warm == cold
