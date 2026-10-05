"""Byte-for-byte goldens of every versioned (schema 3) JSON command.

N5a-1 (docs/DESIGN.md 5.3b). This file replaces tests/test_schema2_goldens.py
and pins the exact bytes of run, demo, scan (triage and --all), trend, rules
(list, --all, --show), rules propose, rules review, rescue plan, and rescue
status, at share (the default), at local (--local) and at raw (--no-redact;
demo has no level flag, so share only), over the packaged demo run and the
same representative fleet built from tests/fixtures/runs.

Schema 3 is not frozen the way schema 2 was: a later N5 part that changes
behavior (severity, observation groups) changes the goldens it affects, and
lists each change with its cause in that part's commit and report.

Stability. Every input is copied into tmp_path first (nothing is ever
written under tests/fixtures), file mtimes are set with os.utime, and every
wall-clock read the commands make is pinned (report._now, the claim
contradiction detector's _now, trend.weekly_counts, observe.observe_groups,
and reader.time.time). HOME is a throwaway directory so the bare-account-name
rule of redact() cannot depend on the machine, and the installation secret is
a fixed one seeded into the throwaway state directory so share aliases are the
same bytes on every run.

The only transformation applied to captured output is `_normalize`: it
replaces the per-test temporary root (and the fake home) with a fixed
placeholder. test_normalize_is_only_a_root_substitution pins that.

Regeneration is explicit and off by default: set AP_REGEN_GOLDENS=1 and run
this file; the changed files then show up in `git diff`. Nothing regenerates
implicitly, and a golden must only change in the commit that changes the
behavior it records.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

import agent_postmortem
from agent_postmortem import disclosure, observe, reader, report, state, trend
from agent_postmortem.cli import main
from agent_postmortem.detectors import claim_contradiction

FIXTURE_RUNS = Path(__file__).parent / "fixtures" / "runs"
GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden" / "schema3"
FAKE_UUID = "00000000-0000-4000-8000-000000000000"

FIXED_NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
# Far in the future so reader.activity_word reads "stale" for every file,
# including the packaged demo files whose real mtimes are not ours to set.
FAR_FUTURE_EPOCH = datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()
FILE_MTIME = FIXED_NOW.timestamp() - 86400.0

REGEN = os.environ.get("AP_REGEN_GOLDENS") == "1"

# Bare run directories (journal.jsonl directly inside) are placed into the
# fleet under wf ids that tests/synthetic-ids.txt lists.
_BARE_RUN_NAMES = (
    "attempt_failed_causes",
    "attempt_failed_retry_tail",
    "bash_backgrounded_timeout",
    "claim_contradiction",
    "clean_run",
    "codex_write_denied",
    "evidence_noise",
    "incomplete_journal",
    "journal_state_orphan_failed",
    "journal_state_resolved",
    "journal_state_unresolved",
    "missing_artifact",
    "self_reference",
    "structured_output_corrupt",
)

# The runs the per-run commands (run, rescue plan, rescue status) cover.
_PER_RUN_LABELS = (
    "reader_minimal:wf_a0000000-001",
    "reader_minimal:wf_b0000000-002",
    "reader_minimal:wf_c0000000-003",
    "journal_malformed_fields:wf_00000000-af1",
    "journal_malformed_fields:session",
    "hook_session:wf_00000000-710",
    "hook_session:session",
    "session_human_interrupt:session",
    "session_parent_stop:session",
    "redaction_adversarial:wf_00000000-960",
    "attempt_facts:wf_00000000-af0",
    "bare:attempt_failed_causes",
    "bare:attempt_failed_retry_tail",
    "bare:clean_run",
    "bare:codex_write_denied",
    "bare:incomplete_journal",
    "bare:journal_state_resolved",
    "bare:journal_state_unresolved",
    "bare:journal_state_orphan_failed",
    "bare:missing_artifact",
    "bare:claim_contradiction",
    "synthetic:no_canonical_agent_id",
    "synthetic:status_flip",
)


def _synthetic_journal() -> str:
    """A run C-5 (a started event with no canonical agentId) and P10b (a
    diagnostic that quotes a journal key) both change. Synthetic keys and
    ids only."""
    lines = [
        {"type": "launched", "timestamp": "2026-09-20T09:00:00.000Z"},
        {
            "type": "started", "timestamp": "2026-09-20T09:00:01.000Z",
            "key": "v2:syn-no-agent-id", "label": "impl:no-id", "phase": "Implement",
        },
        {
            "type": "started", "timestamp": "2026-09-20T09:01:00.000Z",
            "key": "v2:syn-null-agent-id", "agentId": None,
            "label": "impl:null-id", "phase": "Implement",
        },
        {
            "type": "started", "timestamp": "2026-09-20T09:02:00.000Z",
            "key": "v2:syn-bad-agent-id", "agentId": "not-an-agent-id",
            "label": "impl:bad-id", "phase": "Implement",
        },
        {
            "type": "started", "timestamp": "2026-09-20T09:03:00.000Z",
            "key": "v2:syn-sensitive-key", "agentId": "ac900000000000001",
            "label": "impl:first", "phase": "Implement",
        },
        {
            "type": "started", "timestamp": "2026-09-20T09:04:00.000Z",
            "key": "v2:syn-sensitive-key", "agentId": "ac900000000000002",
            "label": "impl:second", "phase": "Implement",
        },
        {
            "type": "result", "timestamp": "2026-09-20T09:05:00.000Z",
            "key": "v2:syn-sensitive-key", "agentId": "ac900000000000002",
            "result": {"ok": True},
        },
        {
            "type": "failed", "timestamp": "2026-09-20T09:06:00.000Z",
            "key": "v2:syn-never-started", "agentId": "ac900000000000003",
        },
    ]
    return "\n".join(json.dumps(line) for line in lines) + "\n"


# A valid-shaped key ("v2:" plus 64 hex) that embeds a planted agent id.
_EMBEDDING_KEY = "v2:" + "ac900000000000004" + "0123456789abcdef" * 2 + "0123456789abcde"


def _status_flip_journal() -> str:
    """A run that is COMPLETE before C-5 and INCOMPLETE after it, and whose
    superseded attempt makes a diagnostic quote a valid-shaped journal key
    (P10b)."""
    lines = [
        {"type": "launched", "timestamp": "2026-09-21T09:00:00.000Z"},
        {
            "type": "started", "timestamp": "2026-09-21T09:00:01.000Z",
            "key": "v2:syn-flip", "label": "impl:flip", "phase": "Implement",
        },
        {
            "type": "result", "timestamp": "2026-09-21T09:01:00.000Z",
            "key": "v2:syn-flip", "result": {"ok": True},
        },
        {
            "type": "started", "timestamp": "2026-09-21T09:02:00.000Z",
            "key": _EMBEDDING_KEY, "agentId": "ac900000000000005",
            "label": "impl:one", "phase": "Implement",
        },
        {
            "type": "started", "timestamp": "2026-09-21T09:03:00.000Z",
            "key": _EMBEDDING_KEY, "agentId": "ac900000000000006",
            "label": "impl:two", "phase": "Implement",
        },
        {
            "type": "result", "timestamp": "2026-09-21T09:04:00.000Z",
            "key": _EMBEDDING_KEY, "agentId": "ac900000000000006", "result": {"ok": True},
        },
    ]
    return "\n".join(json.dumps(line) for line in lines) + "\n"


class _Env:
    def __init__(self, root: Path, home: Path, fleet: Path, run_paths: dict[str, Path]):
        self.root = root
        self.home = home
        self.fleet = fleet
        self.run_paths = run_paths


def _copy_tree(src: Path, dest: Path) -> None:
    shutil.copytree(src, dest, dirs_exist_ok=True)


def _build_fleet(root: Path) -> tuple[Path, dict[str, Path]]:
    fleet = root / "fleet"
    paths: dict[str, Path] = {}
    bare_slug = "-home-user-project"

    for index, name in enumerate(_BARE_RUN_NAMES, start=1):
        wf_id = f"wf_00000000-c{index:02x}"
        dest = fleet / bare_slug / FAKE_UUID / "subagents" / "workflows" / wf_id
        _copy_tree(FIXTURE_RUNS / name, dest)
        paths[f"bare:{name}"] = dest

    for case in sorted(FIXTURE_RUNS.iterdir()):
        if not case.is_dir() or case.name in _BARE_RUN_NAMES:
            continue
        for slug_dir in sorted(p for p in case.iterdir() if p.is_dir()):
            dest = fleet / f"{slug_dir.name}-{case.name}"
            _copy_tree(slug_dir, dest)
            for wf in sorted(dest.glob("*/subagents/workflows/wf_*")):
                paths[f"{case.name}:{wf.name}"] = wf
            for session in sorted(dest.glob("*.jsonl")):
                paths[f"{case.name}:session"] = session

    synthetic = (
        fleet / bare_slug / FAKE_UUID / "subagents" / "workflows" / "wf_00000000-c20"
    )
    synthetic.mkdir(parents=True)
    (synthetic / "journal.jsonl").write_text(_synthetic_journal(), encoding="utf-8")
    paths["synthetic:no_canonical_agent_id"] = synthetic

    flip = fleet / bare_slug / FAKE_UUID / "subagents" / "workflows" / "wf_00000000-c21"
    flip.mkdir(parents=True)
    (flip / "journal.jsonl").write_text(_status_flip_journal(), encoding="utf-8")
    paths["synthetic:status_flip"] = flip

    for path in fleet.rglob("*"):
        os.utime(path, (FILE_MTIME, FILE_MTIME))
    return fleet, paths


FIXED_SECRET = bytes(range(32))


def _seed_fixed_secret() -> None:
    """A fixed installation secret and an empty registry in the throwaway state
    directory (the registry is valid and empty, the secret valid: P6's normal
    row), so every share alias in a golden is the same bytes on every run."""
    directory = state.state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    state.write_registry_bytes(
        state.registry_path(directory), disclosure._dumps(disclosure.empty_registry())
    )
    descriptor = os.open(state.secret_path(directory), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(FIXED_SECRET)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "fakehome-zz"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = tmp_path / "golden-config.toml"
    config_path.write_text(
        '[[adopted]]\nrule = "R-GIT-CWD"\ndate = "2026-09-10"\ntarget = "workflow.md"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_path))
    state.update(
        lambda s: s["applied"].append(
            {
                "rule_id": "R-RETRY-SAFE", "target": "/home/user/CLAUDE.md",
                "applied_at": "2026-09-15T00:00:00Z",
            }
        )
    )

    _seed_fixed_secret()

    monkeypatch.setattr(report, "_now", lambda: FIXED_NOW)
    monkeypatch.setattr(claim_contradiction, "_now", lambda: FIXED_NOW)
    monkeypatch.setattr(reader, "time", types.SimpleNamespace(time=lambda: FAR_FUTURE_EPOCH))

    real_weekly_counts = trend.weekly_counts
    monkeypatch.setattr(
        trend, "weekly_counts",
        lambda runs, weeks, now=None: real_weekly_counts(runs, weeks, now=FIXED_NOW),
    )
    # N5c-1: rescue and `run --observe` observe the repository groups.
    real_observe_groups = observe.observe_groups
    monkeypatch.setattr(
        observe, "observe_groups",
        lambda run, now=None, groups=None: real_observe_groups(run, now=FIXED_NOW, groups=groups),
    )

    fleet, run_paths = _build_fleet(tmp_path)
    return _Env(tmp_path, home, fleet, run_paths)


def _normalize(text: str, env: _Env) -> str:
    """The one transformation applied to captured output: the fake home and
    the per-test temporary root become fixed placeholders. Home first (it
    lives under the root)."""
    for variant in sorted({str(env.home), str(env.home.resolve())}, key=len, reverse=True):
        text = text.replace(variant, "<HOME>")
    for variant in sorted({str(env.root), str(env.root.resolve())}, key=len, reverse=True):
        text = text.replace(variant, "<ROOT>")
    return text


def _invoke(argv: list[str], env: _Env, capsys) -> str:
    capsys.readouterr()
    code = main(argv)
    captured = capsys.readouterr()
    return (
        f"exit={code}\n<<<STDOUT>>>\n{_normalize(captured.out, env)}<<<STDERR>>>\n"
        f"{_normalize(captured.err, env)}<<<END>>>\n"
    )


def _per_run(command: list[str], levelflags: list[str], env: _Env, capsys) -> str:
    sections = []
    for label in _PER_RUN_LABELS:
        argv = [*command, str(env.run_paths[label]), "--json", *levelflags]
        sections.append(f"##### case {label}\n" + _invoke(argv, env, capsys))
    return "".join(sections)


def _fleet(command: list[str], levelflags: list[str], env: _Env, capsys) -> str:
    argv = [*command, "--root", str(env.fleet), *levelflags]
    return "##### case fleet\n" + _invoke(argv, env, capsys)


def _rules_show(levelflags: list[str], env: _Env, capsys) -> str:
    argv = ["rules", "--show", "R-RETRY-SAFE", "--json", *levelflags]
    return "##### case show\n" + _invoke(argv, env, capsys)


def _demo(env: _Env, capsys) -> str:
    return "##### case demo\n" + _invoke(["demo", "--json"], env, capsys)


_LEVELS = {"share": [], "local": ["--local"], "raw": ["--no-redact"]}

_FLEET_COMMANDS = {
    "scan": ["scan", "--json"],
    "scan_all": ["scan", "--all", "--json"],
    "trend": ["trend", "--weeks", "52", "--json"],
    "rules": ["rules", "--json"],
    "rules_all": ["rules", "--all", "--json"],
    "rules_propose": ["rules", "propose", "--json"],
    "rules_review": ["rules", "review", "--json"],
}


def _check_golden(name: str, actual: str) -> None:
    golden = GOLDEN_DIR / f"{name}.txt"
    if REGEN:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual, encoding="utf-8")
    assert golden.exists(), f"missing golden {golden.name}; run once with AP_REGEN_GOLDENS=1"
    assert actual == golden.read_text(encoding="utf-8"), (
        f"{golden.name} changed. Regenerate only with AP_REGEN_GOLDENS=1, in the commit "
        "that intentionally changes it, and list the cause in that commit."
    )


@pytest.mark.parametrize("level", sorted(_LEVELS))
@pytest.mark.parametrize("command", ["run", "rescue_plan", "rescue_status"])
def test_per_run_json_golden(command, level, env, capsys):
    prefix = {
        "run": ["run"],
        "rescue_plan": ["rescue", "plan"],
        "rescue_status": ["rescue", "status"],
    }[command]
    actual = _per_run(prefix, _LEVELS[level], env, capsys)
    _check_golden(f"{command}__{level}", actual)


@pytest.mark.parametrize("level", sorted(_LEVELS))
@pytest.mark.parametrize("command", sorted(_FLEET_COMMANDS))
def test_fleet_json_golden(command, level, env, capsys):
    # `--json` is already part of the command; the fleet root and the level
    # flag go after it.
    actual = _fleet(_FLEET_COMMANDS[command], _LEVELS[level], env, capsys)
    _check_golden(f"{command}__{level}", actual)


@pytest.mark.parametrize("level", sorted(_LEVELS))
def test_rules_show_json_golden(level, env, capsys):
    _check_golden(f"rules_show__{level}", _rules_show(_LEVELS[level], env, capsys))


def test_demo_json_golden_at_share(env, capsys):
    _check_golden("demo__share", _demo(env, capsys))


def test_goldens_exist_for_every_command_and_level():
    expected = {
        f"{c}__{level}" for c in ("run", "rescue_plan", "rescue_status", "rules_show")
        for level in _LEVELS
    }
    expected |= {f"{c}__{level}" for c in _FLEET_COMMANDS for level in _LEVELS}
    expected.add("demo__share")
    assert {p.stem for p in GOLDEN_DIR.glob("*.txt")} == expected


def test_goldens_carry_no_temporary_root_or_real_home():
    for golden in GOLDEN_DIR.glob("*.txt"):
        text = golden.read_text(encoding="utf-8")
        assert "pytest-of-" not in text, golden.name
        assert "/private/var/" not in text, golden.name
        assert str(Path.home()) not in text, golden.name


def test_normalize_is_only_a_root_substitution(env):
    root = str(env.root)
    home = str(env.home)
    sample = f"a {root}/x b {home}/y c plain /home/user/project"
    assert _normalize(sample, env) == (
        "a <ROOT>/x b <HOME>/y c plain /home/user/project"
    )
    assert _normalize("nothing to replace", env) == "nothing to replace"


def test_demo_run_is_the_packaged_one():
    assert (Path(agent_postmortem.__file__).parent / "demo" / "-home-user-project" / "00000000-0000-4000-8000-000000000000" / "subagents" / "workflows" / "wf_00000000-d30").is_dir()


def test_trend_json_does_not_depend_on_the_hash_seed(env, tmp_path):
    """trend.weekly_counts used to add detectors in set-iteration order, so
    `trend --json` key order changed from process to process. Goldens (and
    any consumer diffing two runs) need one order; sorted by detector id."""
    repo_src = str(Path(agent_postmortem.__file__).resolve().parent.parent)
    outputs = set()
    for seed in ("1", "2", "3", "4"):
        child_env = {
            **os.environ,
            "PYTHONPATH": repo_src,
            "PYTHONHASHSEED": seed,
            "HOME": str(env.home),
        }
        done = subprocess.run(
            [sys.executable, "-m", "agent_postmortem.cli", "trend", "--root", str(env.fleet),
             "--weeks", "52", "--json"],
            capture_output=True, text=True, env=child_env, check=False,
        )
        assert done.returncode == 0, done.stderr
        outputs.add(done.stdout)
    assert len(outputs) == 1
