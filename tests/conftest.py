"""Shared fixtures for the whole suite."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Where uv keeps what it downloaded, read once from the machine: a test's home holds nothing, and a
# uv run inside a test should not fetch every package and Python again.
_UV_FOLDERS = {
    "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR")
    or str(Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "uv"),
    "UV_PYTHON_INSTALL_DIR": os.environ.get("UV_PYTHON_INSTALL_DIR")
    or str(Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "uv" / "python"),
}


def _is_outside_setting(name: str) -> bool:
    """Set by a session running the suite, or on this machine. AGENT_POSTMORTEM_CONFIG is left alone
    here because the fixture below sets it for each test, to a file that is not there."""
    return (name.startswith("CLAUDE") or name == "XDG_CONFIG_HOME"
            or (name.startswith("AGENT_POSTMORTEM_") and name != "AGENT_POSTMORTEM_CONFIG"))


# Collection runs before any fixture, and a module can keep a copy of the environment to use later,
# as tests/test_export_public.py does: that copy holds no home of this machine's either.
_SUITE_HOME = tempfile.mkdtemp(prefix="ap-suite-home-")
atexit.register(shutil.rmtree, _SUITE_HOME, ignore_errors=True)
os.environ["HOME"] = _SUITE_HOME
os.environ.update(_UV_FOLDERS)
for _name in list(os.environ):
    if _is_outside_setting(_name):
        del os.environ[_name]


@pytest.fixture(autouse=True)
def _isolate_this_machine(tmp_path, monkeypatch):
    """A test reads nothing of the machine it runs on: its home is its own and not there yet, so no
    plugin is installed and no session is live unless the test builds one. CI run 42145711237
    failed on a line doctor prints only without the plugin, which this machine has, so every local
    gate had passed. The real `claude` is out of reach too. A test that wants either sets its own,
    which simply shadows this default."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("agent_postmortem.doctor._claude_path", lambda: None)


@pytest.fixture(autouse=True)
def _isolate_agent_postmortem_config(tmp_path, monkeypatch):
    """Every test gets its own, deliberately missing config file by default
    (docs/DESIGN.md 4.10), so a real ~/.config/agent-postmortem/config.toml
    on whatever machine runs the suite can never silently silence a
    detector, change a verdict, or change an exit code out from under a
    test that never asked for that. A test that wants to exercise the
    config file itself overrides AGENT_POSTMORTEM_CONFIG explicitly, which
    simply shadows this default for its own duration."""
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "unused-agent-postmortem-config.toml"))


@pytest.fixture(autouse=True)
def _isolate_agent_postmortem_state_dir(tmp_path, monkeypatch):
    """N3b D-j: every test gets its own XDG_STATE_HOME, so no test ever
    creates the installation secret, the alias registry, or state.json in
    the real state directory. Subprocess-based tests inherit it through the
    environment. A test that wants a specific state directory overrides it
    with its own monkeypatch.setenv, which simply shadows this default."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


@pytest.fixture(autouse=True)
def _isolate_caller_session_id(monkeypatch):
    """Field milestone F, FB1 and FB3 (docs/DESIGN.md 4.13.6, 4.11): `run --here` and
    `rescue plan` read CLAUDE_CODE_SESSION_ID, which Claude Code sets in its own
    environment, so the suite may itself run inside a session that has it. Every test
    starts without it; a test that wants a caller session sets it with its own
    monkeypatch.setenv, which simply shadows this default."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
