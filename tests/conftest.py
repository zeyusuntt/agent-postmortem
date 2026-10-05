"""Shared fixtures for the whole suite."""

from __future__ import annotations

import pytest


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
