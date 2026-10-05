"""N3b fix round 2, R2-4 (decision D-p in .reviews/v0.3/N3b-spec.md), moved to
schema 3 by N5a-1: S1's name-first finding titles carry the label's pseudonym
instead of the label with structural placeholders in it. The goldens hold no
hostile label, so this test pins one.

One run (a copy of tests/fixtures/runs/attempt_failed_causes) whose first
attempt label holds a UUID, an email and a project-slug shape, built at run
time so no tracked file holds the literal. `run --json` bytes are pinned by
SHA-256 at raw and at share (a fixed installation secret makes the share
aliases stable). While schema 2 existed the raw digest was the output of
commit 95c5e36 and proved raw unchanged; schema 2 is retired, so the digests
now pin the schema-3 output."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import types
from pathlib import Path

import pytest

from agent_postmortem import reader, report
from agent_postmortem.cli import main
from tests.test_n5_schema3_goldens import (
    FAR_FUTURE_EPOCH,
    FILE_MTIME,
    FIXED_NOW,
    _seed_fixed_secret,
)

FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "attempt_failed_causes"
OLD_LABEL = "impl:usage-session"
HOSTILE = " ".join([
    "impl:u",
    "-".join(["55555555", "5555", "4555", "8555", "555555555555"]),
    "zzbob" + "@" + "corp.example.com",
    "-Users" + "-zzbob-proj",
])

# N8a K2: the digests changed only because `next` lost the generic hidden-findings
# item (4.13.8). N5a-1: the digests of the schema-3 output (they were the schema-2 output of
# 95c5e36 and of the titles-only correction, 0dc0b462... and 920dc6c2...). Share
# uses a fixed installation secret, so its aliases are stable bytes.
RAW_SHA256 = "e4c324f9540e7c7c511ca0d815827c9e4b72fd524d39f9647cd82e0336d03ab2"
SHARE_SHA256 = "ef43319b5ffda440771efdb4e0380d89598dc1243aec1d4e190c4a7c1f62c283"
# What share printed at 95c5e36 for the two titles that carry the label.
OLD_SHARE_TITLES = [
    "impl:u uuid:5555 email:29da95 project:f057c4 attempt 1 failed: "
    "session usage limit reached, resets 2pm; the key was not retried",
    "impl:u uuid:5555 email:29da95 project:b66f83 attempt 1 failed: "
    "session usage limit reached, resets 2pm; the key was not retried",
]
NEW_SHARE_TITLES = [
    "agent-1 attempt 1 failed: session usage limit reached, resets 2pm; "
    "the key was not retried",
    "agent-11 attempt 1 failed: session usage limit reached, resets 2pm; "
    "the key was not retried",
]


@pytest.fixture
def hostile_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _seed_fixed_secret()  # N5a-1: share JSON carries aliases, so its bytes need a fixed secret
    monkeypatch.setattr(report, "_now", lambda: FIXED_NOW)
    monkeypatch.setattr(reader, "time", types.SimpleNamespace(time=lambda: FAR_FUTURE_EPOCH))
    run_dir = tmp_path / "wf_00000000-c01"
    shutil.copytree(FIXTURE, run_dir)
    for path in sorted(run_dir.iterdir()):
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(OLD_LABEL, HOSTILE), encoding="utf-8")
    for path in [run_dir, *run_dir.iterdir()]:
        os.utime(path, (FILE_MTIME, FILE_MTIME))
    return run_dir


def _json(run_dir, capsys, *flags) -> str:
    capsys.readouterr()
    assert main(["run", str(run_dir), "--json", *flags]) in (0, 1)
    return capsys.readouterr().out


def _label_titles(out: str) -> list[str]:
    return [f["title"] for f in json.loads(out)["findings"] if f["title"].startswith(
        ("agent-1 attempt", "agent-11 attempt", "impl:u "))]


def test_raw_schema3_json_bytes_are_pinned_with_a_hostile_label(hostile_run, capsys):
    out = _json(hostile_run, capsys, "--no-redact")
    assert hashlib.sha256(out.encode("utf-8")).hexdigest() == RAW_SHA256
    assert HOSTILE in out


def test_share_schema3_json_bytes_are_pinned_and_the_two_label_titles_are_pseudonyms(
    hostile_run, capsys,
):
    out = _json(hostile_run, capsys)
    assert hashlib.sha256(out.encode("utf-8")).hexdigest() == SHARE_SHA256
    assert _label_titles(out) == NEW_SHARE_TITLES
    for fragment in ("uuid:5555", "email:", "project:", "zzbob", "corp.example", "55555555"):
        assert fragment not in out


def test_share_old_titles_documented_for_the_record():
    # the value 95c5e36 printed: a structural placeholder inside a real label
    assert all(title.startswith("impl:u uuid:5555 email:") for title in OLD_SHARE_TITLES)
    assert all("project:" in title for title in OLD_SHARE_TITLES)


def test_share_keeps_the_schema_3_shape(hostile_run, capsys):
    share = json.loads(_json(hostile_run, capsys))
    raw = json.loads(_json(hostile_run, capsys, "--no-redact"))

    def shape(value):
        if isinstance(value, dict):
            return {key: shape(item) for key, item in value.items()}
        if isinstance(value, list):
            return [shape(item) for item in value[:1]]
        return type(value).__name__

    assert shape(share) == shape(raw)
    assert share["schema"] == raw["schema"] == 3
