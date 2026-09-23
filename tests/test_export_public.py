"""scripts/export-public.sh: destination handling (DESIGN.md 5.4).

These tests run the real script against a small synthetic source repository
built in tmp_path, never against this project's own repository, so they
stay fast and cannot touch real history. check-privacy.sh inside the
synthetic repo is a trivial stand-in that always exits 0; the export
script's own behavior around the destination argument is what is under
test here, not the privacy rules (covered by tests/test_privacy.py).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPORT_SCRIPT = REPO_ROOT / "scripts" / "export-public.sh"

ENV = {**os.environ, "PUBLIC_NAME": "Public Name", "PUBLIC_EMAIL": "public@example.com"}


def _make_source_repo(root: Path) -> None:
    scripts_dir = root / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "export-public.sh").write_text(
        EXPORT_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8",
    )
    (scripts_dir / "export-public.sh").chmod(0o755)
    (scripts_dir / "check-privacy.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (scripts_dir / "check-privacy.sh").chmod(0o755)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Owner"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "owner@example.com"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def test_export_public_creates_a_new_public_repo(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _make_source_repo(source)
    dest = tmp_path / "dest"

    result = subprocess.run(
        ["bash", str(source / "scripts" / "export-public.sh"), str(dest)],
        cwd=source, env=ENV, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (dest / "README.md").exists()
    log = subprocess.run(
        ["git", "log", "--format=%s"], cwd=dest, capture_output=True, text=True, check=True,
    )
    assert log.stdout.strip() == "Initial release v0.1.0"


def test_export_public_resolves_a_relative_destination_against_the_callers_cwd(tmp_path):
    """The historical bug: the existence check ran against the caller's cwd
    before the script cd'd to the repository root, but later steps used the
    same relative DEST after that cd, so a relative destination that
    happened to share a name with a directory at the repository root (here,
    "scripts") would pass the check and then extract into the repository's
    own directory instead."""
    source = tmp_path / "source"
    source.mkdir()
    _make_source_repo(source)
    caller_cwd = tmp_path / "elsewhere"
    caller_cwd.mkdir()

    result = subprocess.run(
        ["bash", str(source / "scripts" / "export-public.sh"), "scripts"],
        cwd=caller_cwd, env=ENV, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (caller_cwd / "scripts" / "README.md").exists()
    assert not (source / "scripts" / "README.md").exists()


def test_export_public_refuses_an_existing_destination(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _make_source_repo(source)
    dest = tmp_path / "dest"
    dest.mkdir()

    result = subprocess.run(
        ["bash", str(source / "scripts" / "export-public.sh"), str(dest)],
        cwd=source, env=ENV, capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "refusing" in result.stderr


def test_export_public_refuses_a_destination_inside_the_source_repository(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _make_source_repo(source)

    result = subprocess.run(
        ["bash", str(source / "scripts" / "export-public.sh"), str(source / "nested-export")],
        cwd=source, env=ENV, capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "source repository" in result.stderr


def test_export_public_requires_the_destinations_parent_to_exist(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _make_source_repo(source)

    result = subprocess.run(
        ["bash", str(source / "scripts" / "export-public.sh"), str(tmp_path / "missing" / "dest")],
        cwd=source, env=ENV, capture_output=True, text=True,
    )

    assert result.returncode != 0
