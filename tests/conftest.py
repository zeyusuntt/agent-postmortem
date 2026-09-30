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
