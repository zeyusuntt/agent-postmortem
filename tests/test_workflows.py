"""Tests for .github/workflows/*.yml (docs/DESIGN.md 8.3).

No YAML library is a dev dependency, so these are plain line-based checks
(the same style plugin/hooks/run.sh's own sed-based `_plugin_version`
takes) rather than a full parse.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def _top_level_lines(text: str) -> list[str]:
    """Every line before the first `jobs:` key, i.e. the workflow-level
    section a top-level `permissions:` block belongs in."""
    lines = text.splitlines()
    return lines[: lines.index("jobs:")]


def _assert_top_level_read_only_permissions(workflow_name: str) -> None:
    text = (WORKFLOWS_DIR / workflow_name).read_text(encoding="utf-8")
    top = _top_level_lines(text)
    assert "permissions:" in top, f"{workflow_name}: no top-level permissions key"
    index = top.index("permissions:")
    assert top[index + 1].strip() == "contents: read", (
        f"{workflow_name}: top-level permissions must be exactly contents: read"
    )


def test_ci_workflow_declares_top_level_contents_read_only():
    """docs/DESIGN.md 8.3: without a declared top-level permissions block,
    every job inherits whatever the repository's own default token
    permissions happen to be -- explicit read-only here is what actually
    pins it down regardless of that setting."""
    _assert_top_level_read_only_permissions("ci.yml")


def test_release_workflow_declares_top_level_contents_read_only():
    _assert_top_level_read_only_permissions("release.yml")


def test_release_publish_job_still_keeps_its_own_id_token_write():
    """The top-level read-only default must not remove the one elevated
    permission the publish job actually needs for PyPI trusted publishing."""
    text = (WORKFLOWS_DIR / "release.yml").read_text(encoding="utf-8")
    lines = text.splitlines()
    publish_index = lines.index("  publish:")
    publish_block = lines[publish_index:]
    assert "    permissions:" in publish_block
    assert "      id-token: write" in publish_block
