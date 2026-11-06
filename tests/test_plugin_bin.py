"""plugin/bin/agent-postmortem: the CLI for a session's Bash tool when the plugin is the only install
("/postmortem names a CLI a plugin-only user does not have on PATH"). Claude
Code 2.1.318 puts a plugin's bin/ at the end of the Bash tool's PATH and not on a hook's, measured
with a throwaway plugin. Every line a hook prints that names `agent-postmortem` needs this too.

Synthetic data only: everything under tmp_path.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "plugin" / "bin" / "agent-postmortem"
DATA = "plugins/data/agent-postmortem-agent-postmortem/bin/agent-postmortem"


def _copy(path: Path, *, says: str = "the copy", code: int = 0, executable: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{says}: $*"\ncat\nexit {code}\n', encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)
    return path


def _run(tmp_path: Path, *args: str, stdin: str = "", **env: str) -> subprocess.CompletedProcess:
    base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")}
    return subprocess.run([str(WRAPPER), *args], input=stdin, capture_output=True, text=True,
                          env={**base, **env}, timeout=10, check=False)


def test_it_ships_executable_and_sound():
    assert WRAPPER.stat().st_mode & stat.S_IXUSR
    assert subprocess.run(["sh", "-n", str(WRAPPER)], capture_output=True, check=False).returncode == 0


def test_it_runs_the_copy_the_plugin_installed(tmp_path):
    _copy(tmp_path / "home" / ".claude" / DATA)
    done = _run(tmp_path, "status", "--local")
    assert done.returncode == 0 and done.stdout == "the copy: status --local\n", done


def test_its_answer_is_the_copys(tmp_path):
    _copy(tmp_path / "home" / ".claude" / DATA, code=3)
    done = _run(tmp_path, "doctor", stdin="from the pipe\n")
    assert done.returncode == 3 and done.stdout == "the copy: doctor\nfrom the pipe\n", done


def test_an_argument_with_spaces_stays_one(tmp_path):
    _copy(tmp_path / "home" / ".claude" / DATA)
    (tmp_path / "home" / ".claude" / DATA).write_text('#!/bin/sh\necho "$#"\n', encoding="utf-8")
    assert _run(tmp_path, "a b", "c").stdout == "2\n"


def test_it_follows_the_config_folder(tmp_path):
    _copy(tmp_path / "home" / ".claude" / DATA, says="default folder")
    _copy(tmp_path / "other" / DATA, says="config folder")
    done = _run(tmp_path, "x", CLAUDE_CONFIG_DIR=f"{tmp_path / 'other'},{tmp_path / 'more'}")
    assert done.stdout.startswith("config folder"), done


def test_a_marketplace_of_another_name_is_found(tmp_path):
    _copy(tmp_path / "home" / ".claude" / "plugins/data/agent-postmortem-a-fork/bin/agent-postmortem")
    assert _run(tmp_path, "x").stdout.startswith("the copy"), "the data folder is named for both"


def test_the_fallback_directory_is_found(tmp_path):
    """run.sh uses it when a hook is given no data directory."""
    _copy(tmp_path / "xdg" / "agent-postmortem" / "plugin" / "bin" / "agent-postmortem")
    assert _run(tmp_path, "x", XDG_STATE_HOME=str(tmp_path / "xdg")).stdout.startswith("the copy")
    _copy(tmp_path / "home" / ".local/state/agent-postmortem/plugin/bin/agent-postmortem")
    assert _run(tmp_path, "x").stdout.startswith("the copy")


def test_another_plugins_data_directory_is_not_read(tmp_path):
    """Measured: in a session's commands $CLAUDE_PLUGIN_DATA can name another plugin's directory."""
    _copy(tmp_path / "other-plugin" / "bin" / "agent-postmortem", says="another plugin")
    done = _run(tmp_path, "x", CLAUDE_PLUGIN_DATA=str(tmp_path / "other-plugin"))
    assert done.returncode == 127 and "another plugin" not in done.stdout, done


def test_with_no_copy_it_says_what_to_do_and_installs_nothing(tmp_path):
    done = _run(tmp_path, "scan")
    assert done.returncode == 127 and done.stdout == "", done
    assert "uv tool install agent-postmortem" in done.stderr, done
    assert not (tmp_path / "home").exists(), "it writes nothing"


def test_a_copy_it_cannot_run_is_no_copy(tmp_path):
    _copy(tmp_path / "home" / ".claude" / DATA, executable=False)
    assert _run(tmp_path, "x").returncode == 127
    (tmp_path / "home" / ".claude" / DATA).unlink()
    (tmp_path / "home" / ".claude" / DATA).mkdir()
    assert _run(tmp_path, "x").returncode == 127


def test_capabilities_answers_with_its_stdin_left_open():
    """It read stdin to its end, so a terminal waited for Ctrl-D and a
    release script sat 5 minutes. It needs no input, and run.sh gives it /dev/null anyway."""
    code = "from agent_postmortem.entry import main; raise SystemExit(main(['hook', 'capabilities']))"
    held = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True,
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    try:
        assert held.wait(timeout=10) == 0
        assert '"protocol": 1' in held.stdout.read()
    finally:
        held.kill()
        held.communicate()


@pytest.mark.parametrize("writer", ["{}", ""])
def test_capabilities_still_answers_a_writer(writer):
    code = "from agent_postmortem.entry import main; raise SystemExit(main(['hook', 'capabilities']))"
    done = subprocess.run([sys.executable, "-c", code], input=writer, capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, timeout=20, check=False)
    assert done.returncode == 0 and '"protocol": 1' in done.stdout, done


# --- further cases --------------------------------------------------------------------------------


def test_the_official_copy_comes_before_another_marketplaces(tmp_path):
    _copy(tmp_path / "home" / ".claude/plugins/data/agent-postmortem-a-fork/bin/agent-postmortem",
          says="a fork")
    _copy(tmp_path / "home" / ".claude" / DATA, says="official")
    assert _run(tmp_path, "x").stdout.startswith("official")


def test_a_copy_linked_back_to_this_script_does_not_loop(tmp_path):
    link = tmp_path / "home" / ".claude" / DATA
    link.parent.mkdir(parents=True)
    link.symlink_to(WRAPPER)
    done = _run(tmp_path, "x")
    assert done.returncode == 127 and "uv tool install" in done.stderr, done
