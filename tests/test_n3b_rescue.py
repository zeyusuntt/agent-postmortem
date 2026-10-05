"""N3b Part 2: `rescue plan` and `rescue status` text at the three levels
(P9, P10, P16, P19, D-b, D-c, D-n).

Both commands already observe the repository, so local paths are
repo-relative where git placed them inside it. share: the transcript pointer
names an aliased file and the unit is `agent-N`; no prompt text, label, phase,
path, agent id, session id or project directory name. local: real label, phase,
agent id, check text; repo-relative paths; never an absolute path. raw: the
real values and absolute paths; no prompt quote and no command excerpt, because
neither command has --evidence (D10).
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone

import pytest

from agent_postmortem import disclosure, report, state
from tests import n3b_helpers as h
from tests import n3b_planted as plant

CHECK_TAIL = "zzCheck" + "Plant" + "7c6"
PROMPT = "Fix the module."


@pytest.fixture
def planted(tmp_path):
    # N5c-1: the observable variant, so the repository group is checked and the
    # plan has a repository root to render repo-relative paths against.
    return plant.build(tmp_path, observable=True)


@pytest.fixture
def books(monkeypatch):
    created: list[disclosure.AliasBook] = []
    real_register = disclosure.register

    def recording_register(identities, **kwargs):
        book = real_register(identities, **kwargs)
        created.append(book)
        return book

    monkeypatch.setattr(disclosure, "register", recording_register)
    return created


def _plan(planted, capsys, *flags):
    return h.cli(capsys, "rescue", "plan", planted.run_dir, *flags)


def _status(planted, capsys, *flags):
    return h.cli(capsys, "rescue", "status", planted.run_dir, *flags)


def _configure_status(planted, tmp_path, monkeypatch) -> str:
    """One declared check inside the repository, one counted run of it, then
    a NEVER-list command, all after the run ended (so status has something to
    say). Returns the declared check text."""
    check = f"pytest -q {planted.repo}/tests/{CHECK_TAIL}"
    config = tmp_path / "rescue-config.toml"
    config.write_text(
        f'[[repos]]\npath = "{planted.repo}"\nchecks = ["{check}"]\n', encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config))
    after = datetime.now(timezone.utc) - timedelta(minutes=5)

    def stamp(offset):
        return (after + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def call(tool_id, command, offset):
        return {"type": "assistant", "timestamp": stamp(offset), "cwd": str(planted.repo),
                "sessionId": planted.session, "message": {"role": "assistant", "usage": {},
                "content": [{"type": "tool_use", "id": tool_id, "name": "Bash",
                             "input": {"command": command}}]}}

    def result(tool_id, offset, error=False):
        return {"type": "user", "timestamp": stamp(offset), "cwd": str(planted.repo),
                "sessionId": planted.session, "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": "ok",
                     "is_error": error}]}}

    rows = [
        call("c1", check, 0), result("c1", 1),
        call("c2", f"git checkout -- {planted.repo_file}", 2), result("c2", 3),
    ]
    main_transcript = planted.root / planted.slug / f"{planted.session}.jsonl"
    # N5c-1: keep the launch result already in the parent transcript (it places
    # the run's attempts in their generation); the calls below follow it.
    existing = main_transcript.read_text(encoding="utf-8") if main_transcript.exists() else ""
    main_transcript.write_text(
        existing + "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8",
    )
    return check


# --- rescue plan, share -----------------------------------------------------


def test_plan_share_aliases_the_task_pointer_and_hides_every_planted_value(planted, capsys):
    _code, out, _err = _plan(planted, capsys)
    alias = h.alias_of("agent", planted.agent1)
    assert f"  TASK      full text: agent-{alias}.jsonl:1" in out
    assert "prompt length: 15 chars" in out
    assert "UNIT agent-1  (phase-1)  2 attempts, last one completed" in out
    assert "path-1  " in out
    h.assert_nothing_planted(out, planted)
    assert PROMPT not in out
    assert "disclosure:" not in out


def test_plan_share_aliases_are_stable_across_two_commands(planted, capsys):
    _code, first, _err = _plan(planted, capsys)
    _code, second, _err = _plan(planted, capsys)
    assert first == second


# --- rescue plan, local -----------------------------------------------------


def test_plan_local_shows_real_names_repo_relative_paths_and_no_absolute_path(planted, capsys):
    _code, out, _err = _plan(planted, capsys, "--local")
    assert out.splitlines()[0] == h.LOCAL_LABEL
    assert f"UNIT {planted.label}  ({planted.phase})  2 attempts, last one completed" in out
    assert f"  TASK      full text: agent-{planted.agent1}.jsonl:1" in out
    assert "src/module.py" in out
    assert "git status --short -- src/module.py" in out
    assert "CHECKS    no checks declared for this repository" in out
    assert PROMPT not in out
    h.assert_no_absolute_path(out, planted)


def test_plan_local_path_matrix_outside_home_and_repo(tmp_path, monkeypatch, capsys):
    base = tmp_path / "matrix"
    base.mkdir()
    home = tmp_path / "zzhome"
    (home / "notes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    outside = "/opt/zzOutside/" + "Plant" + "7c7.py"
    in_home = str(home / "notes" / "n7c8.md")
    sibling = str(base.resolve() / ("zzRepo" + "Plant" + "7c5")) + "2/src/other.py"
    rebuilt = plant.build(
        base, extra_edits=[outside, in_home, sibling, "rel/inside.py"], observable=True,
    )
    _code, out, _err = h.cli(capsys, "rescue", "plan", rebuilt.run_dir, "--local")
    assert "path-" in out
    assert "~/.../n7c8.md" in out
    assert "rel/inside.py" in out
    assert "/opt/zzOutside" not in out
    assert str(home) not in out
    assert "zzRepoPlant7c52" not in out
    h.assert_no_absolute_path(out, rebuilt)


# --- rescue plan, raw -------------------------------------------------------


def test_plan_raw_has_the_real_values_and_no_prompt_quote(planted, capsys):
    _code, out, _err = _plan(planted, capsys, "--raw")
    assert out.splitlines()[0] == h.RAW_LABEL
    assert f"UNIT {planted.label}  ({planted.phase})  2 attempts" in out
    assert f"  TASK      full text: agent-{planted.agent1}.jsonl:1" in out
    assert f"no checks declared for {planted.repo}:" in out
    assert "src/module.py  " in out
    assert PROMPT not in out


def test_plan_raw_is_the_local_text_with_the_repository_root_spelled_out(planted, capsys):
    _code, local, _err = _plan(planted, capsys, "--local")
    _code, raw, _err = _plan(planted, capsys, "--raw")
    expected = local.partition("\n")[2].replace("this repository", str(planted.repo))
    assert raw.partition("\n")[2] == expected.replace(h.LOCAL_LABEL, h.RAW_LABEL)


# --- rescue status ----------------------------------------------------------


def test_status_share_hides_the_check_text_and_the_excerpt(planted, tmp_path, monkeypatch, capsys):
    _configure_status(planted, tmp_path, monkeypatch)
    _code, out, _err = _status(planted, capsys)
    assert "UNIT agent-1   repository path-1   checks declared: 1" in out
    assert "last counted run: check-1" in out
    assert re.search(r"commands matching the NEVER list: line \d+: git checkout$", out, re.MULTILINE)
    h.assert_nothing_planted(out, planted)
    assert CHECK_TAIL not in out
    assert "--" not in out.split("NEVER list")[1].splitlines()[0]


def test_status_local_shows_the_real_check_with_repo_relative_paths(
    planted, tmp_path, monkeypatch, capsys,
):
    _configure_status(planted, tmp_path, monkeypatch)
    _code, out, _err = _status(planted, capsys, "--local")
    assert out.splitlines()[0] == h.LOCAL_LABEL
    assert f"UNIT {planted.label}   repository .   checks declared: 1" in out
    # FA1: the complete `./tests/...` token is rendered whole by the local path function,
    # which normalizes it to the repo-relative form (the leading `./` is gone)
    assert f"last counted run: pytest -q tests/{CHECK_TAIL}  exit 0" in out
    assert re.search(r"commands matching the NEVER list: line \d+: git checkout$", out, re.MULTILINE)
    h.assert_no_absolute_path(out, planted)


def test_status_raw_has_the_real_check_and_absolute_path_but_only_the_first_word(
    planted, tmp_path, monkeypatch, capsys,
):
    check = _configure_status(planted, tmp_path, monkeypatch)
    _code, out, _err = _status(planted, capsys, "--raw")
    assert out.splitlines()[0] == h.RAW_LABEL
    assert f"UNIT {planted.label}   repository {planted.repo}   checks declared: 1" in out
    assert f"last counted run: {check}  exit 0" in out
    assert re.search(r"commands matching the NEVER list: line \d+: git checkout$", out, re.MULTILINE)


# --- registration (P9) and the exit codes stay ------------------------------


def test_plan_registers_the_run_identities_once_with_no_slow_path(planted, books, capsys):
    _plan(planted, capsys)
    assert len(books) == 1
    assert books[0].available and books[0].slow_path_count == 0


def test_status_registers_the_run_identities_once_with_no_slow_path(
    planted, books, tmp_path, monkeypatch, capsys,
):
    _configure_status(planted, tmp_path, monkeypatch)
    _status(planted, capsys)
    assert len(books) == 1
    assert books[0].slow_path_count == 0


# N5a-1: JSON at share registers like the text of the same verb; JSON at local and
# raw registers nothing.
@pytest.mark.parametrize(
    "flags", [["--local"], ["--raw"], ["--json", "--local"], ["--json", "--raw"]],
)
@pytest.mark.parametrize("verb", ["plan", "status"])
def test_other_levels_and_json_register_nothing(verb, flags, planted, books, capsys):
    h.cli(capsys, "rescue", verb, planted.run_dir, *flags)
    assert books == []


@pytest.mark.parametrize("verb", ["plan", "status"])
def test_the_exit_code_does_not_depend_on_the_level(verb, planted, capsys):
    codes = {}
    for flags in ([], ["--local"], ["--raw"]):
        codes[tuple(flags)] = h.cli(capsys, "rescue", verb, planted.run_dir, *flags)[0]
    assert len(set(codes.values())) == 1


# --- D-c and D-b ------------------------------------------------------------


@pytest.mark.parametrize("verb", ["plan", "status"])
def test_unavailable_secret_gives_one_stderr_line_and_stays_private(
    verb, planted, tmp_path, monkeypatch, capsys,
):
    secret = h.make_secret_unavailable(monkeypatch, tmp_path)
    before = (secret.read_bytes(), secret.stat().st_mtime_ns, stat.S_IMODE(secret.stat().st_mode))
    _code, out, err = h.cli(capsys, "rescue", verb, planted.run_dir)
    assert err.splitlines() == [f"agent-postmortem rescue {verb}: stable aliases unavailable"]
    assert "unavailable" not in out
    h.assert_nothing_planted(out, planted)
    assert (secret.read_bytes(), secret.stat().st_mtime_ns,
            stat.S_IMODE(secret.stat().st_mode)) == before


def test_plan_unavailable_names_the_task_file_positionally(planted, tmp_path, monkeypatch, capsys):
    h.make_secret_unavailable(monkeypatch, tmp_path)
    _code, out, _err = _plan(planted, capsys)
    assert h.POSITIONAL_AGENT_FILE.search(out)
    assert not h.STABLE_AGENT_FILE.search(out)


def test_a_slow_path_failure_mid_render_re_renders_with_positional_names_only(
    planted, monkeypatch, capsys,
):
    real_collect = report.collect_identities

    def without_agents(run, findings=None):
        return [item for item in real_collect(run, findings) if item[0] != "agent"]

    monkeypatch.setattr(report, "collect_identities", without_agents)
    real_lock = state.lock_state_dir
    calls = {"n": 0}

    def flaky_lock(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("simulated lock failure on the slow path")
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(state, "lock_state_dir", flaky_lock)
    _code, out, err = _plan(planted, capsys)
    assert calls["n"] >= 2
    assert err.splitlines() == ["agent-postmortem rescue plan: stable aliases unavailable"]
    assert h.POSITIONAL_AGENT_FILE.search(out)
    assert not h.STABLE_AGENT_FILE.search(out)
    assert planted.agent1 not in out


# --- the process never writes inside the run directory ----------------------


def test_rescue_text_changes_no_file_in_the_run_directory(planted, capsys):
    def snapshot():
        return {
            str(path): (path.stat().st_mtime_ns, path.stat().st_size)
            for path in planted.run_dir.rglob("*") if path.is_file()
        }

    before = snapshot()
    for flags in ([], ["--local"], ["--raw"]):
        _plan(planted, capsys, *flags)
        _status(planted, capsys, *flags)
    assert snapshot() == before
    assert os.path.isdir(planted.run_dir)


# --- D-o: --evidence on rescue plan and rescue status -----------------------

TASK_LINE = f'  TASK      "{PROMPT}"'


@pytest.mark.parametrize("verb", ["plan", "status"])
def test_evidence_at_share_adds_only_the_one_note_line(verb, planted, tmp_path, monkeypatch, capsys):
    _configure_status(planted, tmp_path, monkeypatch)
    run = _plan if verb == "plan" else _status
    _code, plain, _err = run(planted, capsys)
    _code, with_flag, _err = run(planted, capsys, "--evidence")
    assert with_flag.count(report.EXCERPT_NOTE) == 1
    assert with_flag.replace(report.EXCERPT_NOTE + "\n", "", 1) == plain
    assert PROMPT not in with_flag
    h.assert_nothing_planted(with_flag, planted)


@pytest.mark.parametrize("flag", ["--local", "--raw"])
def test_plan_evidence_quotes_the_task_prompt_like_v02(flag, planted, capsys):
    _code, plain, _err = _plan(planted, capsys, flag)
    _code, out, _err = _plan(planted, capsys, flag, "--evidence")
    assert TASK_LINE in out
    assert f"            full text: agent-{planted.agent1}.jsonl:1" in out
    assert "prompt length:" not in out.split(TASK_LINE)[1].splitlines()[1]
    assert PROMPT not in plain
    assert report.EXCERPT_NOTE not in out
    if flag == "--local":
        h.assert_no_absolute_path(out, planted)


@pytest.mark.parametrize("flag", ["--local", "--raw"])
def test_status_evidence_shows_the_never_command_excerpt_like_v02(
    flag, planted, tmp_path, monkeypatch, capsys,
):
    _configure_status(planted, tmp_path, monkeypatch)
    _code, plain, _err = _status(planted, capsys, flag)
    _code, out, _err = _status(planted, capsys, flag, "--evidence")
    if flag == "--raw":
        expected = f"git checkout -- {planted.repo_file}"[:60]
    else:
        expected = "git checkout -- src/module.py"
    assert re.search(rf"commands matching the NEVER list: line \d+: {re.escape(expected)}$",
                     out, re.MULTILINE)
    assert re.search(r"commands matching the NEVER list: line \d+: git checkout$", plain, re.MULTILINE)
    if flag == "--local":
        h.assert_no_absolute_path(out, planted)


@pytest.mark.parametrize("verb", ["plan", "status"])
@pytest.mark.parametrize("flags", [[], ["--raw"]])
def test_evidence_changes_nothing_in_json(verb, flags, planted, tmp_path, monkeypatch, capsys):
    _configure_status(planted, tmp_path, monkeypatch)
    run = _plan if verb == "plan" else _status
    _code, plain, _err = run(planted, capsys, "--json", *flags)
    code, with_flag, _err = run(planted, capsys, "--json", "--evidence", *flags)
    assert code in (0, 1, 2, 3, 4, 5)
    assert json.loads(with_flag)
    if not flags:
        # share: --evidence changes nothing in JSON (no excerpt key exists there).
        assert with_flag == plain
        return
    # N5a-1 (5.3b): at raw --evidence adds only the excerpt keys (`task.text`,
    # `excerpt`) and changes nothing else.
    flagged = json.loads(with_flag)
    for unit in flagged.get("units", []):
        if isinstance(unit.get("task"), dict):
            unit["task"].pop("text", None)
    for key in ("not_counted", "never_matches"):
        for row in flagged.get(key, []):
            row.pop("excerpt", None)
    assert flagged == json.loads(plain)
