"""N3b Part 2: `scan` triage (default) and `scan --all` text at the three
levels (P9, P10, P11, P15, D-b, D-c, D-n).

share: the project column is `project:<alias>`; no planted value appears.
local: the real project directory name, no absolute path.
raw: today's output plus the first line.
The triage view prints no identity at all, so it registers nothing.
"""

from __future__ import annotations

import re
import shutil

import pytest

from agent_postmortem import disclosure, report, state
from tests import n3b_helpers as h
from tests import n3b_planted as plant


@pytest.fixture
def planted(tmp_path):
    return plant.build(tmp_path)


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


def _scan(planted, capsys, *flags):
    return h.cli(capsys, "scan", "--root", planted.root, *flags)


# --- share ------------------------------------------------------------------


def test_share_all_row_carries_the_project_alias_and_no_planted_value(planted, capsys):
    _code, out, _err = _scan(planted, capsys, "--all")
    alias = h.alias_of("project", planted.slug)
    assert re.fullmatch(r"[0-9a-f]{12,64}", alias)
    assert f"  project:{alias}  workflow  " in out
    assert plant.WF_ID in out
    h.assert_nothing_planted(out, planted)
    assert "disclosure:" not in out


def test_share_triage_has_its_structural_lines_and_no_planted_value(planted, capsys):
    _code, out, err = _scan(planted, capsys)
    assert out.startswith("NEEDS ATTENTION (1 of 1 runs)\n")
    assert f"-> agent-postmortem run {plant.WF_ID}" in out
    assert "ALL TIME" in out
    h.assert_nothing_planted(out, planted)
    assert "disclosure:" not in out
    assert err == ""


def test_share_all_aliases_are_stable_across_two_commands(planted, capsys):
    _code, first, _err = _scan(planted, capsys, "--all")
    _code, second, _err = _scan(planted, capsys, "--all")
    assert first == second


# --- local ------------------------------------------------------------------


def test_local_all_shows_the_project_directory_name_and_no_absolute_path(planted, capsys):
    _code, out, _err = _scan(planted, capsys, "--all", "--local")
    assert out.splitlines()[0] == h.LOCAL_LABEL
    assert f"  {planted.slug}  workflow  " in out
    assert "project:" not in out
    h.assert_no_absolute_path(out, planted)


def test_local_triage_has_the_label_the_structure_and_no_absolute_path(planted, capsys):
    _code, out, _err = _scan(planted, capsys, "--local")
    assert out.splitlines()[0] == h.LOCAL_LABEL
    assert out.splitlines()[1] == "NEEDS ATTENTION (1 of 1 runs)"
    h.assert_no_absolute_path(out, planted)


# --- raw --------------------------------------------------------------------


def test_raw_all_is_todays_output_after_the_first_line(planted, capsys):
    _code, share, _err = _scan(planted, capsys, "--all")
    _code, raw, _err = _scan(planted, capsys, "--all", "--raw")
    assert raw.splitlines()[0] == h.RAW_LABEL
    alias = h.alias_of("project", planted.slug)
    expected = share.replace(f"project:{alias}", planted.slug)
    assert raw.partition("\n")[2] == expected


def test_raw_triage_is_the_share_triage_after_the_first_line(planted, capsys):
    _code, share, _err = _scan(planted, capsys)
    _code, raw, _err = _scan(planted, capsys, "--raw")
    assert raw.splitlines()[0] == h.RAW_LABEL
    assert raw.partition("\n")[2] == share


# --- registration (P9) ------------------------------------------------------


def test_all_registers_the_projects_once_with_no_slow_path(planted, books, capsys):
    _scan(planted, capsys, "--all")
    assert len(books) == 1
    assert books[0].available and books[0].slow_path_count == 0


def test_triage_prints_no_identity_so_it_registers_nothing(planted, books, capsys):
    _scan(planted, capsys)
    assert books == []


# N5a-1: share JSON registers its project aliases like `scan --all` text does; JSON at
# local and raw registers nothing.
@pytest.mark.parametrize(
    "flags",
    [
        ["--all", "--local"], ["--all", "--raw"], ["--all", "--json", "--local"],
        ["--all", "--json", "--raw"],
    ],
)
def test_other_levels_and_json_register_nothing(flags, planted, books, capsys):
    _scan(planted, capsys, *flags)
    assert books == []


# --- D-c and D-b ------------------------------------------------------------


def test_unavailable_secret_gives_a_positional_project_and_one_stderr_line(
    planted, tmp_path, monkeypatch, capsys,
):
    h.make_secret_unavailable(monkeypatch, tmp_path)
    _code, out, err = _scan(planted, capsys, "--all")
    assert err.splitlines() == ["agent-postmortem scan: stable aliases unavailable"]
    assert "  project-1  workflow  " in out
    assert "unavailable" not in out
    h.assert_nothing_planted(out, planted)


def test_triage_prints_no_diagnostic_even_when_the_secret_is_unavailable(
    planted, tmp_path, monkeypatch, capsys,
):
    h.make_secret_unavailable(monkeypatch, tmp_path)
    _code, out, err = _scan(planted, capsys)
    assert err == ""
    assert "unavailable" not in out


def _second_project(planted):
    other = planted.root / (planted.slug + "-two")
    shutil.copytree(planted.root / planted.slug, other)
    return planted.slug + "-two"


def test_a_slow_path_failure_mid_render_re_renders_every_row_positionally(
    planted, monkeypatch, capsys,
):
    second = _second_project(planted)
    real_collect = report.collect_project_identities

    def only_first(slugs):
        return real_collect(list(slugs)[:1])

    monkeypatch.setattr(report, "collect_project_identities", only_first)
    real_lock = state.lock_state_dir
    calls = {"n": 0}

    def flaky_lock(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("simulated lock failure on the slow path")
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(state, "lock_state_dir", flaky_lock)
    _code, out, err = _scan(planted, capsys, "--all")
    assert calls["n"] >= 2
    assert err.splitlines() == ["agent-postmortem scan: stable aliases unavailable"]
    assert not re.search(r"project:[0-9a-f]{12}", out)
    assert "project-1" in out and "project-2" in out
    assert planted.slug not in out and second not in out
