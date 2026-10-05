"""Release demo: the packaged demo is a complete workflow run in the standard
layout with its parent session transcript (docs/DESIGN.md 4.13.3, 8.2)."""
import sys
import zipfile
from pathlib import Path

import pytest

from agent_postmortem import cli, reader
from agent_postmortem.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_RUN = (
    REPO_ROOT / "src" / "agent_postmortem" / "demo" / "-home-user-project"
    / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_00000000-d30"
)


def test_demo_output_names_no_missing_launch(capsys):
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "could not be placed" not in out
    assert "may be incomplete" not in out
    assert out.startswith("RUN wf_00000000-d30  DEGRADED")


def test_demo_run_has_a_finished_lifecycle_and_every_attempt_placed():
    run = reader.load_run(DEMO_RUN)
    assert run.lifecycle.terminal == "completed"
    assert run.lifecycle.source == "notification"
    assert run.lifecycle.generations == 1
    assert run.finished is True
    assert run.risk_population_completeness == "complete"
    assert run.risk_population_completeness_reasons == []
    assert len(run.attempts) == 4


def test_a_zip_traversable_is_extracted_on_3_12_and_refused_with_a_message_on_3_11(
    tmp_path, monkeypatch,
):
    """Codex release review 1: importlib.resources.as_file extracts a directory only from Python
    3.12 on. From a zip (a wheel on sys.path) the demo runs there; on 3.11 it says what to do
    instead of failing with IsADirectoryError, and it never copies the tree out itself (no write
    outside the state directory, docs/DESIGN.md 5.4). A zipfile.Path stands in for that
    traversable on every Python version."""
    source = REPO_ROOT / "src" / "agent_postmortem" / "demo"
    archive = tmp_path / "demo.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(source.parent).as_posix())
    ref = zipfile.Path(archive) / "demo" / "-home-user-project"
    if sys.version_info >= (3, 12):
        with cli._demo_project_dir(ref) as project:
            run = reader.load_run(project.joinpath(*cli._DEMO_RUN_PARTS))
            assert run.finished is True and len(run.attempts) == 4
    monkeypatch.setattr(cli, "_AS_FILE_TAKES_DIRECTORIES", False)
    with pytest.raises(cli.DemoUnavailable, match="installed on disk on Python 3.11"):
        with cli._demo_project_dir(ref):
            pass
    monkeypatch.setattr(cli, "_DEMO_PROJECT_REF", ref)
    assert main(["demo"]) == 2


def test_a_filesystem_demo_project_is_used_in_place():
    ref = REPO_ROOT / "src" / "agent_postmortem" / "demo" / "-home-user-project"
    with cli._demo_project_dir(ref) as project:
        assert project == ref
    assert ref.exists()
