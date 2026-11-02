"""Hatchling build hook: a wheel built from a git checkout records which commit it came from
(docs/DESIGN.md 8.1), so `agent-postmortem --version` can say `0.3.5 (live 7edc932,
2026-10-30)` and a field report can name the build it ran.

Nothing is recorded, and the plain version is printed, for a release: a wheel built from the
sdist (no git there, the release workflow's `uv build`), from the commit tagged `v<version>`, or
an editable install. Build-time code only: the package itself never runs git.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ImportError:  # imported by the tests, which call build_info() alone
    BuildHookInterface = object

STAMP_PATH = "agent_postmortem/_build_info.json"


def build_info(root: str, version: str) -> dict | None:
    """{branch, commit, date, dirty} of the checkout at `root`, or None when `root` is not a git
    checkout of its own (a directory inside another repository is not), git fails, or HEAD is the
    commit tagged v<version>. `dirty` means a tracked file differs from HEAD; untracked files do
    not count."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", root, *args], check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()

    try:
        if git("rev-parse", "--is-inside-work-tree") != "true":
            return None
        # the build root must be the checkout's own top level: an sdist unpacked inside another
        # repository is a release build, not a checkout of this one
        if os.path.realpath(git("rev-parse", "--show-toplevel")) != os.path.realpath(root):
            return None
        if f"v{version}" in git("tag", "--points-at", "HEAD").split():
            return None
        return {
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "commit": git("rev-parse", "--short", "HEAD"),
            "date": git("log", "-1", "--format=%cs", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        }
    except (OSError, subprocess.SubprocessError):
        return None


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        self._stamp = None
        if version == "editable":
            return
        info = build_info(self.root, self.metadata.version)
        if info is None:
            return
        handle, path = tempfile.mkstemp(prefix="agent-postmortem-build-", suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(info, out)
        self._stamp = path
        build_data["force_include"][path] = STAMP_PATH

    def finalize(self, version: str, build_data: dict, artifact_path: str) -> None:
        if getattr(self, "_stamp", None):
            try:
                os.unlink(self._stamp)
            except OSError:
                pass
