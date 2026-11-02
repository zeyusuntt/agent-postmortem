"""The README maps the words of the live lines to what `scan` and `run` print (field report
2026-10-30, "rule words in CLAUDE.md do not match the tool's finding names": an instruction to
settle FAILED, KILL or CLAIM lines met scan rows that say codex_unavailable,
claim_contradiction and silent_retry)."""

from __future__ import annotations

import re
from pathlib import Path

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


def _section() -> str:
    match = re.search(r"^### Live words and report words\n(.*?)(?=^#)", README, re.M | re.S)
    assert match, "README has no 'Live words and report words' section"
    return match.group(1)


def test_each_live_word_names_the_detectors_and_verdicts_it_corresponds_to():
    rows = {line.split("|")[1].strip(): line for line in _section().splitlines()
            if line.startswith("| `")}
    assert set(rows) == {"`KILL`", "`FAILED`", "`CLAIM`"}
    assert "`mislabeled_kill`" in rows["`KILL`"] and "`silent_retry`" in rows["`KILL`"]
    assert "`attempt_failed`" in rows["`FAILED`"] and "`workflow_failed`" in rows["`FAILED`"]
    assert "`claim_contradiction`" in rows["`CLAIM`"] and "`missing_artifact`" in rows["`CLAIM`"]
    assert "`codex_unavailable`" in rows["`CLAIM`"]
