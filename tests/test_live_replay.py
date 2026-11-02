"""The replay benchmark (scripts/live_replay.py, .reviews/live/RESULTS.md) as a regression guard:
every synthetic episode shaped like a corpus class is named, and nothing else is."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "live_replay.py"


def _module():
    spec = importlib.util.spec_from_file_location("live_replay", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_replay"] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


def test_replay_names_every_episode_and_nothing_else(monkeypatch, tmp_path):
    result = _module().evaluate(interval=2.0)
    misses = [r["scenario"] for r in result["scenarios"] if r["matched"] != r["expected"]]
    noise = [r["scenario"] for r in result["scenarios"] if r["emitted"] != r["matched"]]
    assert misses == [] and noise == []
    assert result["recall"] == 1.0 and result["precision"] == 1.0
    assert result["max_time_to_detect_seconds"] <= 2 * 2.0
