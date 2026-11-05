"""docs/DESIGN.md 4.14.11: run.sh upgrades a bundled CLI whose version differs from the plugin's,
and the hooks.json table gains the agent-launched matcher (4.14.5). Hermetic like test_plugin.py:
fake `agent-postmortem` and `uv` scripts on a pinned PATH, never a real install."""

from __future__ import annotations

import json
import time

from test_plugin import (
    PYPI_SPEC,
    _manifest_version,
    _run_run_sh,
    _write_fake_cli,
)

_FAKE_UV_LOGS = r"""#!/usr/bin/env bash
echo "$@" >> "$AGENT_POSTMORTEM_TEST_UV_LOG"
exit 0
"""


def _bundled_cli(version: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then\n'
        f"  printf '{{\"protocol\": 1, \"version\": \"{version}\"}}\\n'\n"
        "  exit 0\n"
        "fi\n"
        "cat >/dev/null\n"
        f"printf '{{\"systemMessage\": \"served by {version}\"}}\\n'\n"
        "exit 0\n"
    )


def _session_start(tmp_path, plugin_data, uv_log, extra_env=None):
    env = {"AGENT_POSTMORTEM_TEST_UV_LOG": str(uv_log)}
    if extra_env:
        env.update(extra_env)
    return _run_run_sh(
        "session-start", path_entries=[str(tmp_path / "uv-bin")], plugin_data=plugin_data,
        stdin='{"source": "startup"}', extra_env=env,
    )


def _uv_calls(uv_log, expect: int = 0, wait: float = 10.0):
    """The upgrade runs detached (4.14.11): wait up to `wait` seconds for `expect` calls, and
    for a test expecting none, give a detached child 1.5 s to show up anyway."""
    deadline = time.monotonic() + (wait if expect else 1.5)
    while True:
        calls = uv_log.read_text(encoding="utf-8").splitlines() if uv_log.exists() else []
        if (expect and len(calls) >= expect) or time.monotonic() >= deadline:
            return calls
        time.sleep(0.1)


def test_a_stale_bundled_cli_is_upgraded_to_the_plugin_version_on_session_start(tmp_path):
    plugin_data = tmp_path / "plugin-data"
    _write_fake_cli(plugin_data / "bin", "agent-postmortem", _bundled_cli("0.0.1"))
    _write_fake_cli(tmp_path / "uv-bin", "uv", _FAKE_UV_LOGS)
    uv_log = tmp_path / "uv.log"

    result = _session_start(tmp_path, plugin_data, uv_log)

    assert result.returncode == 0, result.stderr
    calls = _uv_calls(uv_log, expect=1)
    assert len(calls) == 1, calls
    assert calls[0].startswith(f"tool install --force {PYPI_SPEC}{_manifest_version()} ")
    # the old CLI keeps serving the hook until the new one is in place
    assert json.loads(result.stdout) == {"systemMessage": "served by 0.0.1"}
    log = plugin_data / f"install-{_manifest_version()}.log"
    assert log.exists()


def test_a_bundled_cli_at_the_plugin_version_is_left_alone(tmp_path):
    plugin_data = tmp_path / "plugin-data"
    _write_fake_cli(plugin_data / "bin", "agent-postmortem", _bundled_cli(_manifest_version()))
    _write_fake_cli(tmp_path / "uv-bin", "uv", _FAKE_UV_LOGS)
    uv_log = tmp_path / "uv.log"

    result = _session_start(tmp_path, plugin_data, uv_log)

    assert result.returncode == 0, result.stderr
    assert _uv_calls(uv_log) == []


def test_a_failed_upgrade_is_not_retried_within_a_day(tmp_path):
    plugin_data = tmp_path / "plugin-data"
    _write_fake_cli(plugin_data / "bin", "agent-postmortem", _bundled_cli("0.0.1"))
    _write_fake_cli(tmp_path / "uv-bin", "uv", _FAKE_UV_LOGS)  # "succeeds" but installs nothing
    uv_log = tmp_path / "uv.log"

    _session_start(tmp_path, plugin_data, uv_log)
    assert len(_uv_calls(uv_log, expect=1)) == 1
    _session_start(tmp_path, plugin_data, uv_log)

    assert len(_uv_calls(uv_log)) == 1


def test_a_stale_bundled_cli_is_never_upgraded_on_another_event(tmp_path):
    plugin_data = tmp_path / "plugin-data"
    _write_fake_cli(plugin_data / "bin", "agent-postmortem", _bundled_cli("0.0.1"))
    _write_fake_cli(tmp_path / "uv-bin", "uv", _FAKE_UV_LOGS)
    uv_log = tmp_path / "uv.log"

    result = _run_run_sh(
        "check", path_entries=[str(tmp_path / "uv-bin")], plugin_data=plugin_data,
        stdin="{}", extra_env={"AGENT_POSTMORTEM_TEST_UV_LOG": str(uv_log)},
    )

    assert result.returncode == 0
    assert _uv_calls(uv_log) == []


def test_no_auto_install_also_stops_the_upgrade(tmp_path):
    plugin_data = tmp_path / "plugin-data"
    _write_fake_cli(plugin_data / "bin", "agent-postmortem", _bundled_cli("0.0.1"))
    _write_fake_cli(tmp_path / "uv-bin", "uv", _FAKE_UV_LOGS)
    uv_log = tmp_path / "uv.log"

    _session_start(tmp_path, plugin_data, uv_log, {"AGENT_POSTMORTEM_NO_AUTO_INSTALL": "1"})

    assert _uv_calls(uv_log) == []


# docs/DESIGN.md 4.14.11 (0.4): a bundled CLI that is a symlink to a CLI outside the data dir is
# the person's own link (a maintainer pointing the plugin at a local build); upgrading it would
# replace the link with the release from PyPI. uv's own entry points link inside the data dir.
def test_a_bundled_cli_linked_to_one_outside_the_data_dir_is_never_upgraded(tmp_path):
    plugin_data = tmp_path / "plugin-data"
    _write_fake_cli(tmp_path / "local-build", "agent-postmortem", _bundled_cli("0.0.1"))
    (plugin_data / "bin").mkdir(parents=True)
    link = plugin_data / "bin" / "agent-postmortem"
    link.symlink_to(tmp_path / "local-build" / "agent-postmortem")
    _write_fake_cli(tmp_path / "uv-bin", "uv", _FAKE_UV_LOGS)
    uv_log = tmp_path / "uv.log"

    result = _session_start(tmp_path, plugin_data, uv_log)

    assert result.returncode == 0, result.stderr
    assert _uv_calls(uv_log) == []
    assert link.is_symlink() and link.resolve() == (tmp_path / "local-build" / "agent-postmortem")
    assert json.loads(result.stdout) == {"systemMessage": "served by 0.0.1"}


def test_a_bundled_cli_linked_inside_the_data_dir_is_still_upgraded(tmp_path):
    plugin_data = tmp_path / "plugin-data"
    _write_fake_cli(plugin_data / "agent-postmortem" / "bin", "agent-postmortem",
                    _bundled_cli("0.0.1"))
    (plugin_data / "bin").mkdir(parents=True)
    (plugin_data / "bin" / "agent-postmortem").symlink_to(
        plugin_data / "agent-postmortem" / "bin" / "agent-postmortem")
    _write_fake_cli(tmp_path / "uv-bin", "uv", _FAKE_UV_LOGS)
    uv_log = tmp_path / "uv.log"

    result = _session_start(tmp_path, plugin_data, uv_log)

    assert result.returncode == 0, result.stderr
    calls = _uv_calls(uv_log, expect=1)
    assert len(calls) == 1 and calls[0].startswith(f"tool install --force {PYPI_SPEC}"), calls


def test_a_relative_link_into_the_data_dir_is_still_upgraded(tmp_path):
    plugin_data = tmp_path / "plugin-data"
    _write_fake_cli(plugin_data / "agent-postmortem" / "bin", "agent-postmortem",
                    _bundled_cli("0.0.1"))
    (plugin_data / "bin").mkdir(parents=True)
    (plugin_data / "bin" / "agent-postmortem").symlink_to(
        "../agent-postmortem/bin/agent-postmortem")
    _write_fake_cli(tmp_path / "uv-bin", "uv", _FAKE_UV_LOGS)
    uv_log = tmp_path / "uv.log"

    result = _session_start(tmp_path, plugin_data, uv_log)

    assert result.returncode == 0, result.stderr
    assert len(_uv_calls(uv_log, expect=1)) == 1


def test_a_relative_link_that_climbs_out_of_the_data_dir_is_never_upgraded(tmp_path):
    # batch 1 review: "$DATA_DIR/bin/../../local-build/..." starts with "$DATA_DIR/" as text
    plugin_data = tmp_path / "plugin-data"
    _write_fake_cli(tmp_path / "local-build", "agent-postmortem", _bundled_cli("0.0.1"))
    (plugin_data / "bin").mkdir(parents=True)
    link = plugin_data / "bin" / "agent-postmortem"
    link.symlink_to("../../local-build/agent-postmortem")
    _write_fake_cli(tmp_path / "uv-bin", "uv", _FAKE_UV_LOGS)
    uv_log = tmp_path / "uv.log"

    result = _session_start(tmp_path, plugin_data, uv_log)

    assert result.returncode == 0, result.stderr
    assert _uv_calls(uv_log) == []
    assert link.is_symlink()


def test_a_link_through_a_directory_symlink_and_dotdot_is_judged_physically(tmp_path):
    # batch 2 review: "cd" resolves ".." before pwd -P sees the symlink; DATA_DIR/jump points
    # outside, so ../jump/../bin/agent-postmortem is outside the data dir
    plugin_data = tmp_path / "plugin-data"
    outside = tmp_path / "outside"
    (outside / "env").mkdir(parents=True)
    _write_fake_cli(outside / "bin", "agent-postmortem", _bundled_cli("0.0.1"))
    (plugin_data / "bin").mkdir(parents=True)
    (plugin_data / "jump").symlink_to(outside / "env")
    link = plugin_data / "bin" / "agent-postmortem"
    link.symlink_to("../jump/../bin/agent-postmortem")
    assert link.resolve() == (outside / "bin" / "agent-postmortem").resolve()
    _write_fake_cli(tmp_path / "uv-bin", "uv", _FAKE_UV_LOGS)
    uv_log = tmp_path / "uv.log"

    result = _session_start(tmp_path, plugin_data, uv_log)

    assert result.returncode == 0, result.stderr
    assert _uv_calls(uv_log) == []


# --- docs/DESIGN.md 4.14.5: the idle watcher ------------------------------------------------------


def _watcher_cli(hooks: str = '"stop-watch"', *, delay: str = "0") -> str:
    return (
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then\n'
        f"  printf '{{\"protocol\": 1, \"version\": \"0.0.1\", \"hooks\": [{hooks}]}}\\n'\n"
        "  exit 0\n"
        "fi\n"
        'printf "%s" "$(cat)" > "$AGENT_POSTMORTEM_TEST_SEEN"\n'
        f"sleep {delay}\n"
        "echo 'the line that wakes the session' >&2\n"
        "exit 2\n"
    )


def _stop_watch(tmp_path, cli: str, **kwargs):
    plugin_data = tmp_path / "plugin-data"
    _write_fake_cli(plugin_data / "bin", "agent-postmortem", cli)
    seen = tmp_path / "seen"
    env = {"AGENT_POSTMORTEM_TEST_SEEN": str(seen), **kwargs.pop("extra_env", {})}
    result = _run_run_sh("stop-watch", path_entries=[], plugin_data=plugin_data,
                         stdin='{"hook_event_name": "Stop"}', extra_env=env, **kwargs)
    return result, seen


def test_stop_watch_passes_on_its_exit_code_and_stderr(tmp_path):
    result, seen = _stop_watch(tmp_path, _watcher_cli())
    assert result.returncode == 2
    assert result.stderr == "the line that wakes the session\n"
    assert seen.read_text() == '{"hook_event_name": "Stop"}'  # the payload, untouched


def test_stop_watch_is_not_under_a_deadline(tmp_path):
    result, _seen = _stop_watch(tmp_path, _watcher_cli(delay="2"),
                                extra_env={"AGENT_POSTMORTEM_HOOK_DEADLINE_SECONDS": "1"})
    assert result.returncode == 2


def test_an_older_cli_never_runs_stop_watch(tmp_path):
    """Its usage error exits 2, which would wake the session with a usage message."""
    old = (
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then\n'
        "  printf '{\"protocol\": 1, \"version\": \"0.0.1\"}\\n'\n"
        "  exit 0\n"
        "fi\n"
        'touch "$AGENT_POSTMORTEM_TEST_SEEN"\n'
        "echo 'usage: agent-postmortem hook {check,...}' >&2\n"
        "exit 2\n"
    )
    result, seen = _stop_watch(tmp_path, old)
    assert (result.returncode, result.stderr, result.stdout) == (0, "", "")
    assert not seen.exists()


def test_a_cli_that_lists_other_hooks_only_never_runs_stop_watch(tmp_path):
    result, seen = _stop_watch(tmp_path, _watcher_cli('"pre-bash"'))
    assert (result.returncode, result.stderr) == (0, "")
    assert not seen.exists()


def test_stop_watch_with_no_cli_prints_nothing(tmp_path):
    result = _run_run_sh("stop-watch", path_entries=[], plugin_data=tmp_path / "plugin-data",
                         stdin="{}")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_no_other_event_gets_a_stderr_or_exit_code_through(tmp_path):
    plugin_data = tmp_path / "plugin-data"
    _write_fake_cli(plugin_data / "bin", "agent-postmortem", _watcher_cli())
    result = _run_run_sh("check", path_entries=[], plugin_data=plugin_data, stdin="{}",
                         extra_env={"AGENT_POSTMORTEM_TEST_SEEN": str(tmp_path / "seen")})
    assert (result.returncode, result.stderr) == (0, "")


# --- the PATH-CLI skew: a hook this CLI does not have stays silent, so say so, at most once a day -


_OLD_PATH_CLI = (
    "#!/usr/bin/env bash\n"
    'if [ "$1" = "hook" ] && [ "$2" = "capabilities" ]; then\n'
    "  printf '{\"protocol\": 1, \"version\": \"0.0.1\"}\\n'\n"
    "  exit 0\n"
    "fi\n"
    "cat >/dev/null\n"
    "exit 0\n"
)


def _session_start_on_path(tmp_path, cli: str):
    _write_fake_cli(tmp_path / "bin", "agent-postmortem", cli)
    return _run_run_sh("session-start", path_entries=[str(tmp_path / "bin")],
                       plugin_data=tmp_path / "plugin-data", stdin='{"source": "startup"}',
                       extra_env={"AGENT_POSTMORTEM_NO_AUTO_INSTALL": "1"})


def test_an_older_cli_on_path_gets_one_upgrade_hint_a_day(tmp_path):
    first = _session_start_on_path(tmp_path, _OLD_PATH_CLI)
    assert first.returncode == 0
    message = json.loads(first.stdout)["systemMessage"]
    assert "older than this plugin" in message and "uv tool upgrade agent-postmortem" in message
    assert _session_start_on_path(tmp_path, _OLD_PATH_CLI).stdout == ""


def test_a_current_cli_on_path_gets_no_upgrade_hint(tmp_path):
    current = _OLD_PATH_CLI.replace('"version": "0.0.1"}',
                                    '"version": "0.0.1", "hooks": ["stop-watch"]}')
    assert "stop-watch" in current
    assert _session_start_on_path(tmp_path, current).stdout == ""


def test_the_hint_never_joins_what_the_cli_itself_printed(tmp_path):
    """stdout must stay one JSON object: the hint waits for a session start with nothing to say."""
    speaking = _OLD_PATH_CLI.replace("cat >/dev/null\nexit 0\n",
                                     "cat >/dev/null\nprintf '{\"systemMessage\": \"mine\"}\\n'\n"
                                     "exit 0\n")
    assert json.loads(_session_start_on_path(tmp_path, speaking).stdout) == {
        "systemMessage": "mine"}
