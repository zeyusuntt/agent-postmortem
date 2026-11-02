"""Findings of the 0.4.0 release review (the whole branch against 0.3.6), each pinned by a test.

Live-mode findings are in tests/test_live_release_review.py; this file holds the scan, detector
and packaging ones.
"""

from __future__ import annotations

import importlib.util
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from live_helpers import write_clean_workflow

from agent_postmortem.cli import main
from agent_postmortem.detectors.missing_artifact import artifact_paths

REPO = Path(__file__).resolve().parent.parent
SLUG = "-home-user-project"


# --- a run that ends at the far end of the calendar (docs/DESIGN.md 4.14.9, 0.3.4's rule) ------


def test_a_workflow_ending_in_year_9999_does_not_abort_the_scan(tmp_path, capsys):
    """One run's end time must never stop a whole scan: the cache's expiry is the end time plus
    the 72-hour report window, which is past datetime.max for a run that ends in 9999."""
    root = tmp_path / "projects"
    write_clean_workflow(root, SLUG, "wf_00000000-75a", datetime(9999, 12, 30, tzinfo=timezone.utc))
    write_clean_workflow(root, SLUG, "wf_00000000-75b", datetime(2026, 10, 1, tzinfo=timezone.utc),
                         agent=2)
    for extra in ((), ("--no-cache",), ("--all", "--json")):
        code = main(["scan", "--root", str(root), *extra])
        out = capsys.readouterr().out
        assert code in (0, 3, 4), (extra, code)
        assert "wf_00000000-75b" in out or "CLEAN" in out or "runs" in out, (extra, out)


# --- the artifact path rule (docs/DESIGN.md 4.14.2 R4: templates are skipped) -----------------


def test_a_path_after_a_shell_or_brace_placeholder_is_not_an_absolute_path():
    assert artifact_paths("Write to ${OUTPUT_DIR}/review.md") == []
    assert artifact_paths("Write to {output_dir}/review.md") == []
    assert artifact_paths("save it as $(pwd)/out.json") == []
    # the rule still names a real absolute path
    assert artifact_paths("Write to /tmp/x/review.md") == ["/tmp/x/review.md"]
    assert artifact_paths("write the report to /tmp/x/report.md.") == ["/tmp/x/report.md"]


# --- build identity: an sdist unpacked inside another checkout (docs/DESIGN.md 8.1) -----------


def _hook_module():
    spec = importlib.util.spec_from_file_location("hatch_build_review", REPO / "hatch_build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def test_an_sdist_unpacked_inside_an_unrelated_checkout_is_not_stamped(tmp_path):
    """A wheel built from the sdist prints the plain version; the build root is not a checkout
    of its own even when a directory above it is one."""
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q", "-b", "main")
    _git(outer, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q",
         "--allow-empty", "-m", "outer")
    unpacked = outer / "agent_postmortem-9.9.9"
    unpacked.mkdir()
    (unpacked / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert _hook_module().build_info(str(unpacked), "9.9.9") is None
    # the checkout's own root is still stamped
    assert _hook_module().build_info(str(outer), "9.9.9") is not None
