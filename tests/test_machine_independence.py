"""The suite says the same on any machine, and CI runs the Python it names (0.5.1, CI run 42145711237).

Four tests passed here and failed on CI. Two read the plugin installed under this machine's home, and
two left "too deep" to the interpreter's own recursion limit. CI's matrix had also never selected a
Python: setup-uv@v3 has no python-version input, so every job ran whatever uv found first.
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_postmortem import doctor
from tests import n3b_helpers as h

REPO = Path(__file__).resolve().parent.parent
CI = REPO / ".github" / "workflows" / "ci.yml"
RELEASE = REPO / ".github" / "workflows" / "release.yml"
CI_LIKE = REPO / "scripts" / "ci-like.sh"
VERSION_CHECK = REPO / "scripts" / "check-python-version.py"
_AT_COLLECTION = dict(os.environ)


# --- a test reads nothing of this machine -------------------------------------------------------


def test_a_test_has_a_home_of_its_own(tmp_path):
    assert Path.home() == tmp_path / "home"
    assert os.path.expanduser("~") == str(tmp_path / "home")


def test_a_copy_of_the_environment_taken_during_collection_holds_no_machine_home():
    """Collection runs before any fixture, and tests/test_export_public.py keeps such a copy."""
    assert _AT_COLLECTION["HOME"] != pwd.getpwuid(os.getuid()).pw_dir
    assert not [name for name in _AT_COLLECTION if name.startswith("CLAUDE")]
    assert "XDG_CONFIG_HOME" not in _AT_COLLECTION


def test_a_child_process_is_given_the_same_home(tmp_path):
    out = subprocess.run([sys.executable, "-c", "import pathlib; print(pathlib.Path.home())"],
                         capture_output=True, text=True, check=True).stdout
    assert out.strip() == str(tmp_path / "home")


def test_no_setting_of_the_session_running_the_suite_reaches_a_test():
    assert not [name for name in os.environ if name.startswith("CLAUDE")]
    assert [name for name in os.environ if name.startswith("AGENT_POSTMORTEM_")] == [
        "AGENT_POSTMORTEM_CONFIG"]  # conftest's own, a file that is not there
    assert "XDG_CONFIG_HOME" not in os.environ


def test_doctor_finds_no_plugin_whatever_this_machine_has(tmp_path):
    assert doctor._plugins_root() == tmp_path / "home" / ".claude" / "plugins"
    assert not doctor._is_installed(doctor._plugins_root())


def test_a_test_cannot_reach_the_real_claude():
    assert doctor._claude_path() is None


def test_uv_keeps_its_downloads_outside_a_test_home(tmp_path):
    """A test home holds nothing, so a uv run in a test would fetch everything again without this."""
    for name in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR"):
        assert os.path.isabs(os.environ[name]), name
        assert not os.environ[name].startswith(str(tmp_path)), name


# --- the path check knows a slash command from a path -------------------------------------------

PLANTED = SimpleNamespace(repo=Path("/zzNowhere/repo"), repo_file=Path("/zzNowhere/repo/file"))


@pytest.mark.parametrize("text", [
    "plugin: not installed; the CLI alone tells nobody: /plugin install agent-postmortem@agent-postmortem",
    "plugin: installed but turned off: /plugin enable agent-postmortem@agent-postmortem",
    "hooks: the plugin's hooks changed after this session started: /reload-plugins or restart to get them",
    "this session is not running the plugin's hooks: /reload-plugins or restart.",
])
def test_a_command_the_tool_tells_you_to_type_is_not_a_path(text):
    h.assert_no_absolute_path(text, PLANTED)


@pytest.mark.parametrize("text", [
    "/opt/zzPlant/file",
    "/etc",
    "see /tmp",
    "/plugin",
    "/plugin install",
    "/plugin install someone-else@market",
    "/plugin uninstall agent-postmortem@agent-postmortem",
    "/plugin install agent-postmortem@agent-postmortem/../zz",
    "/plugin install agent-postmortem@agent-postmortem /opt/zzPlant/file",
    "/plugin/install agent-postmortem@agent-postmortem",
    "/plugins install agent-postmortem@agent-postmortem",
    "/reload-plugins",
    "/reload-plugins private/report.json",
    "/reload-plugins/zz",
    "/reload-pluginsZZ",
    "/reload-plugins /opt/zzPlant/file",
    "/reload-plugins or restart/opt/zzPlant",
])
def test_a_path_does_not_pass_for_a_command(text):
    with pytest.raises(AssertionError):
        h.assert_no_absolute_path(text, PLANTED)


# --- CI runs what it names ----------------------------------------------------------------------


def _matrix_versions(text: str) -> list[str]:
    found = re.search(r"python-version:\s*\[([^\]]*)\]", text)
    assert found, "no python-version list in the matrix"
    return re.findall(r'"([0-9.]+)"', found.group(1))


def test_ci_selects_the_python_of_each_matrix_row():
    text = CI.read_text(encoding="utf-8")
    assert re.search(r"^\s+UV_PYTHON:\s*\$\{\{\s*matrix\.python-version\s*\}\}\s*$", text, re.M)


@pytest.mark.parametrize("workflow", [CI, RELEASE], ids=lambda path: path.name)
def test_no_workflow_passes_setup_uv_an_input_it_ignores(workflow):
    """setup-uv@v3 warns about python-version and carries on: every job ran what uv found first."""
    text = workflow.read_text(encoding="utf-8")
    assert "setup-uv@v3" in text
    assert not re.search(r"setup-uv@v3\n\s+with:\n(?:\s+\S+:.*\n)*?\s+python-version:", text)


def test_each_ci_job_fails_when_it_is_on_another_python():
    text = CI.read_text(encoding="utf-8")
    assert "uv run python scripts/check-python-version.py ${{ matrix.python-version }}" in text
    assert text.index("check-python-version.py") < text.index("uv run pytest")


def test_the_release_build_names_its_python_as_well():
    text = RELEASE.read_text(encoding="utf-8")
    assert re.search(r'^\s+UV_PYTHON:\s*"3\.\d+"\s*$', text, re.M)
    assert "check-python-version.py" in text


def test_the_classifiers_list_exactly_the_versions_ci_runs():
    meta = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    listed = [c.rsplit(" :: ", 1)[1] for c in meta["project"]["classifiers"]
              if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)]
    assert listed == _matrix_versions(CI.read_text(encoding="utf-8"))


def _version_check(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(VERSION_CHECK), *argv],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


def test_the_version_check_passes_on_the_python_it_was_asked_for():
    running = "%d.%d" % sys.version_info[:2]
    done = _version_check(running)
    assert done.returncode == 0, done.stderr
    assert running in done.stdout


@pytest.mark.parametrize("argv", [("2.7",), ("3",), ("3.1",), ()], ids=["other", "major", "prefix", "none"])
def test_the_version_check_fails_on_any_other(argv):
    done = _version_check(*argv)
    assert done.returncode != 0
    assert done.stderr.strip()


# --- the gate before a push imitates CI ---------------------------------------------------------


def _ci_commands() -> list[str]:
    """Each one-line `run:` of the CI job, in order. The plugin validation block needs `claude`."""
    return re.findall(r"^\s+run:\s+(\S.*)$", CI.read_text(encoding="utf-8"), re.M)


def test_the_gate_is_executable_and_parses():
    assert CI_LIKE.stat().st_mode & stat.S_IXUSR
    done = subprocess.run(["bash", "-n", str(CI_LIKE)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_the_gate_runs_every_step_ci_runs_in_the_same_order():
    script = CI_LIKE.read_text(encoding="utf-8")
    commands = [c.replace("${{ matrix.python-version }}", "$version") for c in _ci_commands()
                if not c.startswith("|")]
    assert len(commands) >= 6, commands
    at = 0
    for command in commands:
        found = script.find(command, at)
        assert found >= 0, f"CI runs {command!r} and the gate does not, or not in this order"
        at = found


def test_the_gate_covers_every_version_ci_runs():
    script = CI_LIKE.read_text(encoding="utf-8")
    default = re.search(r'^VERSIONS="([0-9. ]+)"$', script, re.M)
    assert default, "no default list of versions"
    assert default.group(1).split() == _matrix_versions(CI.read_text(encoding="utf-8"))


@pytest.mark.parametrize("needed", [
    "env -i",                     # nothing of the caller's environment
    "HOME=",                      # an empty home
    "CLAUDE_CONFIG_DIR=",         # an empty config dir
    "UV_PYTHON_PREFERENCE=only-managed",  # uv's standalone build, never a Python found on this Mac
    "CI=true",                    # what a runner sets
    "--no-bin",                   # fetching a Python leaves no link on this machine's PATH
])
def test_the_gate_isolates_what_a_runner_would_not_have(needed):
    assert needed in CI_LIKE.read_text(encoding="utf-8")


def test_the_gate_also_runs_the_python_a_mac_runner_finds_first():
    """A macOS runner's uv picked Homebrew's build. It parsed what uv's standalone build of the same
    minor version refused, so standalone builds alone did not show CI run 42145711237's failure."""
    script = CI_LIKE.read_text(encoding="utf-8")
    assert 'VERSIONS="$VERSIONS brew:$' in script
    assert "UV_PYTHON_PREFERENCE=only-system" in script


def test_the_gate_refuses_a_path_with_claude_on_it():
    assert "command -v claude" in CI_LIKE.read_text(encoding="utf-8")


def test_the_gate_keeps_the_load_rules():
    script = CI_LIKE.read_text(encoding="utf-8")
    assert "--deadline" in script       # no step runs without one
    assert "mkdir \"$LOCK\"" in script  # one run at a time on this machine
    assert "memory_pressure" in script and "vm.loadavg" in script


# --- the gate fails when it should --------------------------------------------------------------

BASH = shutil.which("bash")
needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")


def _deadline(seconds: int, script: str, **kwargs) -> subprocess.CompletedProcess:
    env = {**os.environ, "CI_LIKE_GRACE": "1"}
    return subprocess.run([BASH, str(CI_LIKE), "--run-with-deadline", str(seconds), BASH, "-c", script],
                          env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=60, **kwargs)


def _gone(pid: int, within: float = 5.0) -> bool:
    end = time.monotonic() + within
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def _pid_from(path: Path, within: float = 10.0) -> int:
    end = time.monotonic() + within
    while time.monotonic() < end:
        text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        if text:
            return int(text)
        time.sleep(0.05)
    raise AssertionError(f"nothing wrote {path}")


def test_a_step_within_its_deadline_keeps_its_exit_code():
    assert _deadline(30, "exit 7").returncode == 7


def test_a_step_past_its_deadline_fails_though_it_exits_0_when_told_to_stop():
    done = _deadline(1, 'trap "exit 0" TERM; while :; do sleep 0.2; done')
    assert done.returncode == 124, (done.stdout, done.stderr)


def test_a_child_that_ignores_being_told_to_stop_does_not_outlive_the_deadline(tmp_path):
    pidfile = tmp_path / "child"
    child = f'trap "" TERM; echo $$ > "{pidfile}"; while :; do sleep 0.2; done'
    done = _deadline(1, f"{BASH} -c '{child}' & wait")
    assert done.returncode == 124, (done.stdout, done.stderr)
    assert _gone(_pid_from(pidfile))


def test_what_a_finished_step_left_running_is_stopped(tmp_path):
    """A runner ends a job's processes with the job, and this run goes on to delete the folder."""
    pidfile = tmp_path / "child"
    done = _deadline(30, f'{BASH} -c \'echo $$ > "{pidfile}"; while :; do sleep 0.2; done\' & '
                         f'while [ ! -s "{pidfile}" ]; do sleep 0.05; done; exit 0')
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert _gone(_pid_from(pidfile))


def test_stopping_the_gate_stops_the_step_it_is_running(tmp_path):
    pidfile = tmp_path / "child"
    gate = subprocess.Popen([BASH, str(CI_LIKE), "--run-with-deadline", "60", BASH, "-c",
                             f'echo $$ > "{pidfile}"; while :; do sleep 0.2; done'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        pid = _pid_from(pidfile)
        gate.send_signal(signal.SIGTERM)
        assert gate.wait(timeout=20) != 0
        assert _gone(pid)
    finally:
        gate.kill()
        gate.wait()


def test_stopping_the_gate_while_it_waits_for_a_free_machine_ends_it_soon(tmp_path):
    """Waiting inside a pipeline held the signal back for up to the whole wait, lock and all."""
    env = {**os.environ, "TMPDIR": str(tmp_path), "CI_LIKE_MIN_FREE": "101"}
    gate = subprocess.Popen([BASH, str(CI_LIKE), "--python", "3.13", "--wait", "600",
                             "--out", str(tmp_path / "out")], cwd=REPO, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lock = tmp_path / "agent-postmortem-ci-like.lock"
    try:
        end = time.monotonic() + 20
        while time.monotonic() < end and "waiting for room" not in (
                (tmp_path / "out" / "ci-like-summary.txt").read_text(encoding="utf-8")
                if (tmp_path / "out" / "ci-like-summary.txt").exists() else ""):
            time.sleep(0.1)
        assert lock.is_dir()
        gate.send_signal(signal.SIGTERM)
        assert gate.wait(timeout=30) == 143
        assert not lock.exists()
    finally:
        gate.kill()
        gate.wait()


def _gate(tmp_path, *argv: str, path: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    if path is not None:
        env["PATH"] = path
    return subprocess.run([BASH, str(CI_LIKE), *argv], cwd=REPO, env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60)


@pytest.mark.parametrize("versions", ["", "   "], ids=["empty", "blank"])
def test_the_gate_refuses_to_test_no_version_at_all(tmp_path, versions):
    done = _gate(tmp_path, "--python", versions)
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "PASS" not in done.stdout


@needs_uv
@pytest.mark.parametrize("holder", ["", "2147483646"], ids=["no pid yet", "a pid that is gone"])
def test_the_gate_never_takes_a_lock_it_did_not_make(tmp_path, holder):
    """A run between mkdir and writing its pid looks the same as a stale lock, so neither is taken."""
    lock = tmp_path / "agent-postmortem-ci-like.lock"
    lock.mkdir()
    if holder:
        (lock / "pid").write_text(holder, encoding="utf-8")
    done = _gate(tmp_path, "--python", "3.13")
    assert done.returncode == 3, (done.stdout, done.stderr)
    assert str(lock) in done.stderr
    assert lock.is_dir()


@needs_uv
def test_the_gate_refuses_to_run_without_node(tmp_path):
    """Without node the hook tests skip, so the gate could pass what fails on a runner."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("uv", "git"):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    done = _gate(tmp_path, "--python", "3.13", path=str(bin_dir))
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "node" in done.stderr
