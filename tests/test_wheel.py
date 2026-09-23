"""Wheel packaging tests (docs/DESIGN.md 8.1's Package, 8.2's Demo, D4/D5).

Skipped entirely when `uv` is not on PATH (CONTRIBUTING.md: unpacked
installs only, in that case). Both tests here build one wheel each with
`uv build`, kept well under the 30s budget on this machine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = REPO_ROOT / "src" / "agent_postmortem" / "demo" / "wf_00000000-d30"

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")


def _build_wheel(tmp_path: Path) -> Path:
    dist_dir = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "-o", str(dist_dir)],
        cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    )
    return next(dist_dir.glob("*.whl"))


def test_demo_works_imported_straight_from_the_wheel_zip(tmp_path):
    """docs/DESIGN.md 8.2's D4: the demo path is resolved through
    importlib.resources.as_file, so it must work with agent_postmortem
    imported directly out of the .whl (itself a zip) on sys.path -- never
    unpacked to a real directory at all."""
    wheel = _build_wheel(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); "
         "from agent_postmortem.cli import main; sys.exit(main(['demo']))",
         str(wheel)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("RUN ")


def test_installed_wheel_scripts_and_demo_work_from_an_empty_cwd(tmp_path):
    """docs/DESIGN.md 8.1/8.2's D5: a real `uv tool install` of the built
    wheel, isolated under tmp_path -- both console scripts answering
    --version and `demo`/`demo --json` running cleanly from an empty cwd
    with nothing written to it -- plus the wheel's own RECORD listing every
    packaged demo file and the plugin's hook script."""
    wheel = _build_wheel(tmp_path)

    with zipfile.ZipFile(wheel) as zf:
        record_name = next(n for n in zf.namelist() if n.endswith(".dist-info/RECORD"))
        record_text = zf.read(record_name).decode("utf-8")
    for demo_file in DEMO_DIR.iterdir():
        assert f"agent_postmortem/demo/wf_00000000-d30/{demo_file.name}" in record_text
    assert "agent_postmortem/hooks/model_allocation_guard.mjs" in record_text

    tool_dir = tmp_path / "uv-tool-dir"
    tool_bin_dir = tmp_path / "uv-tool-bin"
    env = {**os.environ, "UV_TOOL_DIR": str(tool_dir), "UV_TOOL_BIN_DIR": str(tool_bin_dir)}
    subprocess.run(
        ["uv", "tool", "install", str(wheel)], check=True, capture_output=True, text=True, env=env,
    )

    empty_cwd = tmp_path / "empty"
    empty_cwd.mkdir()
    for script in ("agent-postmortem", "postmortem"):
        result = subprocess.run(
            [str(tool_bin_dir / script), "--version"], capture_output=True, text=True, cwd=empty_cwd,
        )
        assert result.returncode == 0, result.stderr
        assert "agent-postmortem" in result.stdout

    result = subprocess.run(
        [str(tool_bin_dir / "agent-postmortem"), "demo"], capture_output=True, text=True, cwd=empty_cwd,
    )
    assert result.returncode in (0, 3), result.stderr
    assert result.stdout.startswith("RUN ")

    result = subprocess.run(
        [str(tool_bin_dir / "agent-postmortem"), "demo", "--json"],
        capture_output=True, text=True, cwd=empty_cwd,
    )
    assert result.returncode in (0, 3), result.stderr
    json.loads(result.stdout)

    assert list(empty_cwd.iterdir()) == []
