"""Tests for the Claude Code plugin (docs/DESIGN.md 7.1-7.4).

Every subprocess test that runs plugin/hooks/run.sh pins PATH to a minimal,
explicit set of directories (never the real developer PATH): the standard
utility locations run.sh itself depends on (cat, mktemp, sed, grep, find,
date, mkdir, rm, kill, sleep -- all present under /usr/bin and /bin on both
of DESIGN.md 8.3's CI runners) plus, per test, a tmp_path bin/ holding a
fake `agent-postmortem` and/or `uv` script. This keeps the tests hermetic
(no real network install, no accidentally picking up a real CLI this
machine happens to have installed) and deterministic.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import signal
import stat
import subprocess
import time
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugin"
RUN_SH = PLUGIN_ROOT / "hooks" / "run.sh"
SAFE_SYSTEM_PATH = "/usr/bin:/bin"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- manifests: parse and carry the required keys ---------------------------


def test_marketplace_json_parses_and_has_required_keys():
    data = _load_json(REPO_ROOT / ".claude-plugin" / "marketplace.json")
    assert data["name"] == "agent-postmortem"
    assert data["owner"] == {"name": "zeyusuntt"}
    plugins = data["plugins"]
    assert len(plugins) == 1
    entry = plugins[0]
    assert entry["name"] == "agent-postmortem"
    assert entry["source"] == "./plugin"
    assert isinstance(entry["description"], str) and entry["description"].strip()


def test_plugin_json_parses_and_has_required_keys():
    data = _load_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
    assert data["name"] == "agent-postmortem"
    assert isinstance(data["version"], str) and data["version"]
    assert isinstance(data["description"], str) and data["description"].strip()
    # docs/DESIGN.md 7.1's D7: the public handle, not a personal name.
    assert data["author"] == {"name": "zeyusuntt", "url": "https://github.com/zeyusuntt"}
    assert data["hooks"] == "./hooks/hooks.json"
    assert data["skills"] == "./skills/"


# The one positioning line (launch plan section 2): the plugin manifest and its marketplace
# entry carry the same words, and the marketplace itself has a description, so
# `claude plugin validate --strict` passes for both (a marketplace with no description is a
# warning, and --strict treats warnings as errors).
POSITIONING = (
    "Reads the Claude Code logs already on your machine and tells you which agent's 'done' "
    "you can't trust, and what to run next."
)
REPOSITORY_URL = "https://github.com/zeyusuntt/agent-postmortem"
GIT_SOURCE = f"git+{REPOSITORY_URL}"
# docs/DESIGN.md 7.3 (0.3.5): the CLI comes from PyPI, pinned to the plugin version; the
# matching GitHub tag is the fallback when that install fails.
PYPI_SPEC = "agent-postmortem=="


def _manifest_version() -> str:
    return _load_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")["version"]


def test_plugin_and_marketplace_entry_carry_the_one_positioning_line():
    plugin = _load_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
    marketplace = _load_json(REPO_ROOT / ".claude-plugin" / "marketplace.json")
    assert plugin["description"] == POSITIONING
    assert marketplace["plugins"][0]["description"] == POSITIONING


def test_marketplace_has_a_description_and_plugin_names_its_homepage_and_repository():
    marketplace = _load_json(REPO_ROOT / ".claude-plugin" / "marketplace.json")
    assert isinstance(marketplace.get("description"), str) and marketplace["description"].strip()
    plugin = _load_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
    assert plugin["homepage"] == REPOSITORY_URL
    assert plugin["repository"] == REPOSITORY_URL


def test_ci_validates_the_plugin_and_the_marketplace_strictly():
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.search(r"^\s*claude plugin validate --strict \./plugin\s*$", ci, re.M)
    assert re.search(r"^\s*claude plugin validate --strict \.\s*$", ci, re.M)
    assert not re.search(r"claude plugin validate (?!--strict)", ci)


def test_plugin_version_equals_package_version():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_version = pyproject["project"]["version"]
    plugin = _load_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
    assert plugin["version"] == package_version


def test_pyproject_plugin_and_package_versions_are_all_0_3_6():
    """docs/DESIGN.md 7.1's D1: the literal, not just mutual equality --
    a version bump that only kept two of the three in sync would still
    pass a pure-equality check."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == "0.3.6"
    plugin = _load_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
    assert plugin["version"] == "0.3.6"
    import agent_postmortem

    assert agent_postmortem.__version__ == "0.3.6"


def test_pyproject_declares_agent_postmortem_as_primary_and_postmortem_as_alias():
    """docs/DESIGN.md 7.1's D1: every hook and the skill invoke
    `agent-postmortem`; pyproject.toml previously installed only
    `postmortem`, so a real `uv tool install` never produced the binary
    resolution step (1) looks for."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]
    assert scripts["agent-postmortem"] == "agent_postmortem.cli:main"
    assert scripts["postmortem"] == "agent_postmortem.cli:main"


# --- hooks.json: matches the docs/DESIGN.md 7.1 table exactly ---------------

# event -> (matcher or None, run.sh subcommand, timeout)
_EXPECTED_HOOKS = {
    "SessionStart": (None, "session-start", 90),
    "Stop": (None, "check", 10),
    "StopFailure": (None, "check", 10),
    "SessionEnd": (None, "session-end", 20),
    "PostToolUse": ("^Workflow$", "workflow-launched", 10),
}


def test_hooks_json_matches_the_design_table():
    data = _load_json(PLUGIN_ROOT / "hooks" / "hooks.json")
    hooks = data["hooks"]
    assert set(hooks) == set(_EXPECTED_HOOKS)
    for event, (matcher, subcommand, timeout) in _EXPECTED_HOOKS.items():
        entries = hooks[event]
        assert len(entries) == 1, f"{event}: expected exactly one hook-group entry"
        entry = entries[0]
        if matcher is None:
            assert "matcher" not in entry, f"{event}: unexpected matcher {entry.get('matcher')!r}"
        else:
            assert entry["matcher"] == matcher
        commands = entry["hooks"]
        assert len(commands) == 1, f"{event}: expected exactly one command"
        command_entry = commands[0]
        assert command_entry["type"] == "command"
        assert command_entry["timeout"] == timeout
        assert command_entry["command"] == f'"${{CLAUDE_PLUGIN_ROOT}}/hooks/run.sh" {subcommand}'


# --- run.sh: shape ------------------------------------------------------------


def test_run_sh_is_executable():
    mode = RUN_SH.stat().st_mode
    assert mode & stat.S_IXUSR, "plugin/hooks/run.sh must be executable"


def test_run_sh_passes_bash_syntax_check():
    result = subprocess.run(["bash", "-n", str(RUN_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --- skills/postmortem/SKILL.md: frontmatter ---------------------------------


def test_skill_md_has_the_expected_frontmatter():
    text = (PLUGIN_ROOT / "skills" / "postmortem" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, frontmatter, body = text.split("---", 2)
    assert "name: postmortem" in frontmatter
    assert "description:" in frontmatter
    # docs/DESIGN.md 7.4/9 (unit M6): no argument / latest / a run id, paste
    # the verdict, point at `rules propose` instead of writing anything, and
    # ask before pasting a rule's printed block into the user's own file.
    assert "scan" in body
    assert "latest" in body
    assert "propose" in body
    assert "--write" not in body
    assert "ask" in body.lower()


def test_postmortem_skill_points_at_rescue_for_work_at_risk():
    """docs/DESIGN.md 7.4/4.11: the /postmortem skill gets one line pointing
    at /rescue for when WORK AT RISK is non-empty, rather than leaving the
    reader to discover the rescue skill on their own."""
    text = (PLUGIN_ROOT / "skills" / "postmortem" / "SKILL.md").read_text(encoding="utf-8")
    assert "WORK AT RISK" in text
    assert "/rescue" in text


# --- skills/rescue/SKILL.md: frontmatter and the 7-step protocol ------------


def test_rescue_skill_md_has_the_expected_frontmatter():
    text = (PLUGIN_ROOT / "skills" / "rescue" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, frontmatter, body = text.split("---", 2)
    assert "name: rescue" in frontmatter
    assert "description:" in frontmatter
    # docs/DESIGN.md 4.11/7.4: what the tool can and cannot do, stated
    # plainly, before the protocol itself.
    assert "read" in body.lower()
    assert "never edits" in body.lower() or "never edit" in body.lower()
    assert "never runs a check" in body.lower() or "never run a check" in body.lower()
    assert "evidence" in body.lower()


# `.reviews/proposals/2026-09-v0.2-guided-rescue.md` section 8.4's 7-step
# protocol, one required phrase per step so a rewrite of the skill's wording
# cannot silently drop a step's own substance.
_RESCUE_SKILL_STEP_PHRASES = (
    # N3c (docs/DESIGN.md 4.13.5, D-o): the packet comes from
    # `rescue plan --local --evidence`, never --no-redact
    # (tests/test_field_v03_more.py pins that absence).
    ("1", ("rescue plan", "--local --evidence", "POSSIBLY LIVE")),
    ("2", ("diff",)),
    ("3", ("declared check", "Bash", "ask")),
    ("4", ("2 cycles",)),
    ("5", ("checkout", "restore", "reset", "stash", "clean", "cp")),
    ("6", ("agent-postmortem rescue status <run-id> --local", "commit", "relaunch")),
    ("7", ("instruction file", "CLAUDE.md")),
)


def test_rescue_skill_md_contains_every_protocol_steps_key_phrases():
    text = (PLUGIN_ROOT / "skills" / "rescue" / "SKILL.md").read_text(encoding="utf-8")
    for step, phrases in _RESCUE_SKILL_STEP_PHRASES:
        for phrase in phrases:
            assert phrase in text, f"step {step}: missing phrase {phrase!r} in rescue SKILL.md"


def test_rescue_skill_md_never_names_write_flag_or_writes_instructions():
    """docs/DESIGN.md 4.11 R3/R7: the skill must never tell the model to run
    `apply --write` (apply is proposals-only) and must explicitly forbid
    editing an instruction file as part of a rescue."""
    text = (PLUGIN_ROOT / "skills" / "rescue" / "SKILL.md").read_text(encoding="utf-8")
    assert "--write" not in text
    assert "Never touch an instruction file" in text or "never edit" in text.lower()


def test_plugin_readme_documents_install_hooks_network_and_uninstall():
    text = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    assert "/plugin marketplace add" in text
    assert "/plugin install" in text
    assert "uninstall" in text.lower()
    assert "AGENT_POSTMORTEM_NO_AUTO_INSTALL" in text


# --- run.sh: integration, against fake CLIs on PATH --------------------------

# A fake `agent-postmortem` whose `hook capabilities` reports protocol 1
# and whose every other invocation echoes the hook payload it received on
# stdin back as a systemMessage -- proof run.sh passes stdin through
# untouched. Avoids python3 deliberately (see run.sh's own _looks_like_json:
# a restricted PATH used to isolate these tests may not carry a working
# python3 -- e.g. macOS's /usr/bin/python3 stub refuses to run at all until
# the Xcode license is accepted), so this uses plain bash string
# replacement to JSON-escape the payload instead.
_FAKE_CLI_ECHO = r"""#!/usr/bin/env bash
if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then
  printf '{"protocol": 1, "version": "0.0.1"}\n'
  exit 0
fi
payload="$(cat)"
escaped="${payload//\\/\\\\}"
escaped="${escaped//\"/\\\"}"
printf '{"systemMessage": "%s"}\n' "$escaped"
exit 0
"""

# A fake `agent-postmortem` that resolves (protocol 1) but whose real hook
# invocation always fails with no output.
_FAKE_CLI_EXITS_NONZERO = r"""#!/usr/bin/env bash
if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then
  printf '{"protocol": 1, "version": "0.0.1"}\n'
  exit 0
fi
cat >/dev/null
exit 1
"""


def _write_fake_cli(bin_dir: Path, name: str, script: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _run_run_sh(event: str, *, path_entries: list[str], plugin_data: Path, stdin: str = "{}",
                 extra_env: dict[str, str] | None = None,
                 timeout: float = 15) -> subprocess.CompletedProcess:
    env = {
        "PATH": ":".join([*path_entries, SAFE_SYSTEM_PATH]),
        "CLAUDE_PLUGIN_DATA": str(plugin_data),
        "HOME": str(plugin_data),  # a harmless, isolated $HOME; nothing here reads it directly
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(RUN_SH), event],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_run_sh_resolves_a_path_cli_and_passes_stdin_through(tmp_path):
    bin_dir = tmp_path / "bin"
    _write_fake_cli(bin_dir, "agent-postmortem", _FAKE_CLI_ECHO)
    plugin_data = tmp_path / "plugin-data"
    payload = '{"hook_event_name": "Stop", "transcript_path": "/tmp/x.jsonl"}'

    result = _run_run_sh("check", path_entries=[str(bin_dir)], plugin_data=plugin_data, stdin=payload)

    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out == {"systemMessage": payload}


def test_run_sh_resolves_the_plugin_data_bin_cli_before_probing_path(tmp_path):
    plugin_data = tmp_path / "plugin-data"
    _write_fake_cli(plugin_data / "bin", "agent-postmortem", _FAKE_CLI_ECHO)
    # Nothing on PATH at all -- if run.sh tried the PATH probe first (or
    # only), this would fall through to the missing-CLI hint instead.
    payload = '{"a": 1}'

    result = _run_run_sh("check", path_entries=[], plugin_data=plugin_data, stdin=payload)

    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out == {"systemMessage": payload}


def test_run_sh_swallows_a_nonzero_exit_into_silent_success(tmp_path):
    bin_dir = tmp_path / "bin"
    _write_fake_cli(bin_dir, "agent-postmortem", _FAKE_CLI_EXITS_NONZERO)
    plugin_data = tmp_path / "plugin-data"

    result = _run_run_sh("check", path_entries=[str(bin_dir)], plugin_data=plugin_data, stdin="{}")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_run_sh_prints_the_missing_cli_hint_once_a_day(tmp_path):
    plugin_data = tmp_path / "plugin-data"

    first = _run_run_sh("check", path_entries=[], plugin_data=plugin_data, stdin="{}")
    second = _run_run_sh("check", path_entries=[], plugin_data=plugin_data, stdin="{}")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    hint = json.loads(first.stdout)
    assert hint == {
        "systemMessage": "agent-postmortem CLI not found; run: uv tool install agent-postmortem"
    }
    assert second.stdout == ""


# --- run.sh: docs/DESIGN.md 7.3 one-time install, session-start only -------

_FAKE_UV_RECORDS_INVOCATION = r"""#!/usr/bin/env bash
echo "$@" >> "$AGENT_POSTMORTEM_TEST_UV_LOG"
exit 0
"""


def test_run_sh_attempts_install_once_on_session_start_when_uv_is_on_path(tmp_path):
    uv_bin = tmp_path / "uv-bin"
    uv_log = tmp_path / "uv-invocations.log"
    _write_fake_cli(uv_bin, "uv", _FAKE_UV_RECORDS_INVOCATION)
    plugin_data = tmp_path / "plugin-data"

    first = _run_run_sh(
        "session-start", path_entries=[str(uv_bin)], plugin_data=plugin_data,
        stdin='{"source": "startup"}', extra_env={"AGENT_POSTMORTEM_TEST_UV_LOG": str(uv_log)},
    )
    second = _run_run_sh(
        "session-start", path_entries=[str(uv_bin)], plugin_data=plugin_data,
        stdin='{"source": "startup"}', extra_env={"AGENT_POSTMORTEM_TEST_UV_LOG": str(uv_log)},
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    invocations = uv_log.read_text(encoding="utf-8").splitlines() if uv_log.exists() else []
    assert len(invocations) == 1, f"expected exactly one uv invocation, got {invocations!r}"
    # 0.3.5: PyPI first, pinned to the plugin version; a PyPI install that succeeds is the
    # only one attempted.
    pinned = f"{PYPI_SPEC}{_manifest_version()}"
    assert invocations[0].startswith(f"tool install {pinned} ")
    install_log = plugin_data / "install.log"
    assert install_log.exists()
    assert f"uv tool install {pinned}" in install_log.read_text(encoding="utf-8")


_FAKE_UV_FAILS_ON_PYPI = r"""#!/usr/bin/env bash
echo "$@" >> "$AGENT_POSTMORTEM_TEST_UV_LOG"
case "$*" in
    *agent-postmortem==*) exit 1 ;;
esac
exit 0
"""


def test_run_sh_falls_back_to_the_matching_github_tag_when_the_pypi_install_fails(tmp_path):
    uv_bin = tmp_path / "uv-bin"
    uv_log = tmp_path / "uv-invocations.log"
    _write_fake_cli(uv_bin, "uv", _FAKE_UV_FAILS_ON_PYPI)
    plugin_data = tmp_path / "plugin-data"

    result = _run_run_sh(
        "session-start", path_entries=[str(uv_bin)], plugin_data=plugin_data,
        stdin='{"source": "startup"}', extra_env={"AGENT_POSTMORTEM_TEST_UV_LOG": str(uv_log)},
    )

    assert result.returncode == 0, result.stderr
    invocations = uv_log.read_text(encoding="utf-8").splitlines()
    version = _manifest_version()
    assert len(invocations) == 2, f"expected PyPI then the GitHub tag, got {invocations!r}"
    assert invocations[0].startswith(f"tool install {PYPI_SPEC}{version} ")
    assert invocations[1].startswith(f"tool install {GIT_SOURCE}@v{version} ")
    log = (plugin_data / "install.log").read_text(encoding="utf-8")
    assert f"uv tool install {PYPI_SPEC}{version}" in log
    assert f"{GIT_SOURCE}@v{version}" in log


def test_run_sh_never_installs_when_no_auto_install_is_set(tmp_path):
    uv_bin = tmp_path / "uv-bin"
    uv_log = tmp_path / "uv-invocations.log"
    _write_fake_cli(uv_bin, "uv", _FAKE_UV_RECORDS_INVOCATION)
    plugin_data = tmp_path / "plugin-data"

    result = _run_run_sh(
        "session-start", path_entries=[str(uv_bin)], plugin_data=plugin_data,
        stdin='{"source": "startup"}',
        extra_env={
            "AGENT_POSTMORTEM_TEST_UV_LOG": str(uv_log),
            "AGENT_POSTMORTEM_NO_AUTO_INSTALL": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert not uv_log.exists()
    assert not (plugin_data / "install.log").exists()


def test_run_sh_never_installs_on_a_non_session_start_event(tmp_path):
    uv_bin = tmp_path / "uv-bin"
    uv_log = tmp_path / "uv-invocations.log"
    _write_fake_cli(uv_bin, "uv", _FAKE_UV_RECORDS_INVOCATION)
    plugin_data = tmp_path / "plugin-data"

    result = _run_run_sh(
        "check", path_entries=[str(uv_bin)], plugin_data=plugin_data, stdin="{}",
        extra_env={"AGENT_POSTMORTEM_TEST_UV_LOG": str(uv_log)},
    )

    assert result.returncode == 0, result.stderr
    assert not uv_log.exists()


# --- run.sh: exact stdin passthrough (docs/DESIGN.md 7.2's D10) -------------

_FAKE_CLI_DUMPS_STDIN = r"""#!/usr/bin/env bash
if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then
  printf '{"protocol": 1, "version": "0.1.0"}\n'
  exit 0
fi
cat > "$AGENT_POSTMORTEM_TEST_STDIN_DUMP"
printf '{"systemMessage": "dumped"}\n'
exit 0
"""


def test_run_sh_passes_exact_stdin_bytes_including_trailing_newline(tmp_path):
    bin_dir = tmp_path / "bin"
    _write_fake_cli(bin_dir, "agent-postmortem", _FAKE_CLI_DUMPS_STDIN)
    plugin_data = tmp_path / "plugin-data"
    dump_path = tmp_path / "stdin-dump.bin"
    payload = '{"a": 1}\n\n'  # deliberately unusual trailing whitespace/newlines

    result = _run_run_sh(
        "check", path_entries=[str(bin_dir)], plugin_data=plugin_data, stdin=payload,
        extra_env={"AGENT_POSTMORTEM_TEST_STDIN_DUMP": str(dump_path)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert dump_path.read_text(encoding="utf-8") == payload


# --- run.sh: JSON validation, docs/DESIGN.md 7.2's D2 -----------------------

_FAKE_CLI_NON_INTEGER_PROTOCOL = r"""#!/usr/bin/env bash
if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then
  printf '{"protocol": 1.5, "version": "0.1.0"}\n'
  exit 0
fi
cat >/dev/null
printf '{"systemMessage": "should never be reached"}\n'
exit 0
"""


def test_run_sh_rejects_a_non_integer_protocol_in_the_probe(tmp_path):
    bin_dir = tmp_path / "bin"
    _write_fake_cli(bin_dir, "agent-postmortem", _FAKE_CLI_NON_INTEGER_PROTOCOL)
    plugin_data = tmp_path / "plugin-data"

    result = _run_run_sh("check", path_entries=[str(bin_dir)], plugin_data=plugin_data, stdin="{}")

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    # protocol 1.5 never resolves as protocol 1 -- falls through to the hint.
    assert "CLI not found" in result.stdout


_FAKE_CLI_CONCATENATED_OUTPUT = r"""#!/usr/bin/env bash
if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then
  printf '{"protocol": 1, "version": "0.1.0"}\n'
  exit 0
fi
cat >/dev/null
printf '{"a": 1}{"b": 2}\n'
exit 0
"""


def test_run_sh_suppresses_concatenated_json_objects(tmp_path):
    bin_dir = tmp_path / "bin"
    _write_fake_cli(bin_dir, "agent-postmortem", _FAKE_CLI_CONCATENATED_OUTPUT)
    plugin_data = tmp_path / "plugin-data"

    result = _run_run_sh("check", path_entries=[str(bin_dir)], plugin_data=plugin_data, stdin="{}")

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == ""


_FAKE_CLI_PLAIN_GARBAGE = r"""#!/usr/bin/env bash
if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then
  printf '{"protocol": 1, "version": "0.1.0"}\n'
  exit 0
fi
cat >/dev/null
printf 'Traceback (most recent call last):\n  not-json-at-all\n'
exit 0
"""


def test_run_sh_suppresses_a_plain_text_stack_trace(tmp_path):
    bin_dir = tmp_path / "bin"
    _write_fake_cli(bin_dir, "agent-postmortem", _FAKE_CLI_PLAIN_GARBAGE)
    plugin_data = tmp_path / "plugin-data"

    result = _run_run_sh("check", path_entries=[str(bin_dir)], plugin_data=plugin_data, stdin="{}")

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == ""


# --- run.sh: deadlines and process-group kills, docs/DESIGN.md 7.2's D3/D4 --

def _fake_cli_that_hangs(marker_dir: Path, *, hang_in: str) -> str:
    """A fake CLI that records `started`, sleeps 60s, then records `finished`.

    hang_in="capabilities" hangs the PATH probe; hang_in="main" answers the
    probe at once and hangs the real hook call. `finished` is written only if
    the 60s sleep ran to its end, so its absence proves a deadline kill."""
    hang = f"""
  echo started > "{marker_dir}/started"
  sleep 60
  echo finished > "{marker_dir}/finished"
"""
    if hang_in == "capabilities":
        return f"""#!/usr/bin/env bash
if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then{hang}  printf '{{"protocol": 1, "version": "0.1.0"}}\\n'
  exit 0
fi
cat >/dev/null
printf '{{"systemMessage": "should never run"}}\\n'
exit 0
"""
    return f"""#!/usr/bin/env bash
if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then
  printf '{{"protocol": 1, "version": "0.1.0"}}\\n'
  exit 0
fi
cat >/dev/null{hang}printf '{{"systemMessage": "should never print"}}\\n'
"""


def test_run_sh_probe_gives_up_after_two_seconds(tmp_path):
    """The probe's own 2s deadline killed the hanging probe: the fake started,
    never reached the end of its 60s sleep, and run.sh fell through to the
    'CLI not found' hint. A deadline that never fires cannot pass: run.sh
    would still be waiting on the 60s sleep when the 30s subprocess ceiling
    (half the child's own runtime, so load cannot trip it) expires."""
    bin_dir = tmp_path / "bin"
    markers = tmp_path / "markers"
    markers.mkdir()
    _write_fake_cli(bin_dir, "agent-postmortem", _fake_cli_that_hangs(markers, hang_in="capabilities"))
    plugin_data = tmp_path / "plugin-data"

    result = _run_run_sh(
        "check", path_entries=[str(bin_dir)], plugin_data=plugin_data, stdin="{}", timeout=30
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert (markers / "started").exists(), "the probe never ran the fake"
    assert not (markers / "finished").exists(), "the probe was not killed before its sleep ended"
    assert "CLI not found" in result.stdout


def test_run_sh_honors_the_hook_deadline_override_env_var(tmp_path):
    """docs/DESIGN.md 7.2's D3/12's D-B: AGENT_POSTMORTEM_HOOK_DEADLINE_SECONDS,
    when set, replaces the event's own built-in deadline (8/18/80s). With a
    1s override and a CLI that would sleep 60s, run.sh returns with empty
    stdout, the fake started but never finished, and the 30s ceiling is far
    below the child's 60s runtime, so only a deadline that fired lets it pass."""
    bin_dir = tmp_path / "bin"
    markers = tmp_path / "markers"
    markers.mkdir()
    _write_fake_cli(bin_dir, "agent-postmortem", _fake_cli_that_hangs(markers, hang_in="main"))
    plugin_data = tmp_path / "plugin-data"

    result = _run_run_sh(
        "check", path_entries=[str(bin_dir)], plugin_data=plugin_data, stdin="{}",
        extra_env={"AGENT_POSTMORTEM_HOOK_DEADLINE_SECONDS": "1"}, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""  # killed before it could print anything
    assert (markers / "started").exists(), "the real hook call never ran the fake"
    assert not (markers / "finished").exists(), "the child was not killed before its sleep ended"


def _processes_with_command(command: str) -> list[int]:
    """Pids whose full command line equals `command` exactly (not a substring)."""
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    pids = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if cmd.strip() == command:
            pids.append(int(pid))
    return pids


def test_run_sh_deadline_kill_reaps_a_backgrounded_child_too(tmp_path):
    """docs/DESIGN.md 7.2's D3/D4: killing a hung CLI must kill its whole
    process group, not just the one pid run.sh launched, or a CLI that
    backgrounds a subprocess of its own leaves it running as an orphan
    after run.sh has already exited. The deadline is overridden to 1s
    (docs/DESIGN.md 12's D-B). The orphan is a `sleep` with a duration unique
    to this run, so no other process on the machine can match it: the test
    proves the orphan existed (its pid file, command line equal to the
    marker), that run.sh returned empty after killing the group, and that no
    process with exactly that command line is left. The finally block kills
    only processes with exactly this run's marker."""
    marker = f"sleep 9876{random.randint(1000, 999999)}"
    orphan_pid_file = tmp_path / "orphan.pid"
    fake = f"""#!/usr/bin/env bash
if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then
  printf '{{"protocol": 1, "version": "0.1.0"}}\\n'
  exit 0
fi
cat >/dev/null
{marker} &
echo $! > "{orphan_pid_file}"
sleep 60
"""
    bin_dir = tmp_path / "bin"
    _write_fake_cli(bin_dir, "agent-postmortem", fake)
    plugin_data = tmp_path / "plugin-data"

    try:
        result = _run_run_sh(
            "check", path_entries=[str(bin_dir)], plugin_data=plugin_data, stdin="{}",
            extra_env={"AGENT_POSTMORTEM_HOOK_DEADLINE_SECONDS": "1"}, timeout=30,
        )

        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert result.stdout == ""
        assert orphan_pid_file.exists(), "the fake never backgrounded its child"

        deadline = time.monotonic() + 10  # grace window for the kill to finish reaping
        survivors = _processes_with_command(marker)
        while survivors and time.monotonic() < deadline:
            time.sleep(0.2)
            survivors = _processes_with_command(marker)
        assert not survivors, f"orphan(s) survived the deadline kill: {survivors}"
    finally:
        for pid in _processes_with_command(marker):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# --- run.sh: unwritable data dir, docs/DESIGN.md 7.2's D6 -------------------


def test_run_sh_is_silent_with_an_unwritable_data_dir(tmp_path):
    """No hint, no marker, no log, nothing on stdout or stderr, exit 0."""
    read_only_parent = tmp_path / "read-only-parent"
    read_only_parent.mkdir()
    read_only_parent.chmod(0o500)
    plugin_data = read_only_parent / "plugin-data"
    try:
        result = _run_run_sh("check", path_entries=[], plugin_data=plugin_data, stdin="{}")
    finally:
        read_only_parent.chmod(0o700)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


# --- run.sh: post-install resolution, docs/DESIGN.md 7.1/7.3's D1/D9 -------

_FAKE_UV_INSTALLS_A_WORKING_CLI = r"""#!/usr/bin/env bash
mkdir -p "$UV_TOOL_BIN_DIR"
cat > "$UV_TOOL_BIN_DIR/agent-postmortem" <<'INNER'
#!/usr/bin/env bash
if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then
  printf '{"protocol": 1, "version": "0.1.0"}\n'
  exit 0
fi
payload="$(cat)"
escaped="${payload//\\/\\\\}"
escaped="${escaped//\"/\\\"}"
printf '{"systemMessage": "post-install: %s"}\n' "$escaped"
exit 0
INNER
chmod +x "$UV_TOOL_BIN_DIR/agent-postmortem"
exit 0
"""


def test_run_sh_resolves_the_freshly_installed_cli_in_the_same_invocation(tmp_path):
    uv_bin = tmp_path / "uv-bin"
    _write_fake_cli(uv_bin, "uv", _FAKE_UV_INSTALLS_A_WORKING_CLI)
    plugin_data = tmp_path / "plugin-data"

    result = _run_run_sh(
        "session-start", path_entries=[str(uv_bin)], plugin_data=plugin_data,
        stdin='{"source": "startup"}',
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert (plugin_data / "bin" / "agent-postmortem").is_file()
    assert "post-install:" in result.stdout


# --- run.sh: a data dir path with spaces ------------------------------------


def test_run_sh_handles_a_plugin_data_path_with_spaces(tmp_path):
    bin_dir = tmp_path / "bin"
    _write_fake_cli(bin_dir, "agent-postmortem", _FAKE_CLI_ECHO)
    plugin_data = tmp_path / "plugin data with spaces"
    payload = '{"a": 1}'

    result = _run_run_sh("check", path_entries=[str(bin_dir)], plugin_data=plugin_data, stdin=payload)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    out = json.loads(result.stdout)
    assert out == {"systemMessage": payload}


# --- run.sh: the daily hint gate is race-safe, docs/DESIGN.md 7.2's D5 -----


def test_run_sh_concurrent_hint_gate_prints_at_most_once(tmp_path):
    """Several `check` invocations racing the missing-CLI hint's
    once-a-day marker must never all see it as due at once: the mkdir
    lock (docs/DESIGN.md 7.2's D5) means only whichever one wins the race
    ever prints it."""
    plugin_data = tmp_path / "plugin-data"
    env = {
        "PATH": SAFE_SYSTEM_PATH,
        "CLAUDE_PLUGIN_DATA": str(plugin_data),
        "HOME": str(plugin_data),
    }
    procs = [
        subprocess.Popen(
            ["bash", str(RUN_SH), "check"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, text=True,
        )
        for _ in range(8)
    ]
    outputs = []
    for proc in procs:
        out, err = proc.communicate(input="{}", timeout=15)
        assert proc.returncode == 0, err
        assert err == ""
        outputs.append(out)

    hints = [out for out in outputs if "CLI not found" in out]
    assert len(hints) == 1, outputs


# --- real `uv tool install`: docs/DESIGN.md 7.1/7.3's D1 --------------------


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_real_uv_tool_install_resolves_through_step_one(tmp_path):
    """A real `uv tool install` of this repository checkout must produce a
    bin/agent-postmortem shim -- the exact name run.sh's resolution step
    (1) and every hook/skill invoke -- and run.sh must then resolve the CLI
    through that step without ever falling back to a PATH probe.

    Proven, not just asserted: PATH also carries a distinguishable fake
    `agent-postmortem` (the same echo fixture the PATH-probe tests above
    use) that would answer with a non-empty systemMessage if it were ever
    reached. Empty stdout here is only possible if $CLAUDE_PLUGIN_DATA/bin
    /agent-postmortem answered instead -- step (1), the real installed CLI,
    on a transcript_path that names no real session (so it prints nothing).
    """
    plugin_data = tmp_path / "plugin-data"
    tool_dir = tmp_path / "uv-tool-dir"
    install_env = dict(os.environ)
    install_env["UV_TOOL_DIR"] = str(tool_dir)
    install_env["UV_TOOL_BIN_DIR"] = str(plugin_data / "bin")

    result = subprocess.run(
        ["uv", "tool", "install", str(REPO_ROOT)],
        env=install_env, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    installed_cli = plugin_data / "bin" / "agent-postmortem"
    assert installed_cli.is_file() and os.access(installed_cli, os.X_OK), (
        f"expected {installed_cli} after `uv tool install`; "
        f"bin/ holds: {sorted(p.name for p in (plugin_data / 'bin').iterdir())}"
    )

    path_bin = tmp_path / "path-bin"
    _write_fake_cli(path_bin, "agent-postmortem", _FAKE_CLI_ECHO)
    payload = '{"hook_event_name": "Stop", "transcript_path": "/tmp/does-not-exist-x.jsonl"}'

    run_result = _run_run_sh(
        "check", path_entries=[str(path_bin)], plugin_data=plugin_data, stdin=payload,
    )
    assert run_result.returncode == 0, run_result.stderr
    assert run_result.stdout == ""


# --- claude plugin validate ./plugin ----------------------------------------


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_claude_plugin_validate_passes():
    result = subprocess.run(
        ["claude", "plugin", "validate", str(PLUGIN_ROOT)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
