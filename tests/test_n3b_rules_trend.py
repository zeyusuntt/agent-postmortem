"""N3b Part 2: `rules` (bare list), `rules --show`, `rules propose`,
`rules review` and `trend` text at the three levels (P9, P15, P19).

Four of these print rule text and counts only: nothing in them depends on the
level, they print no evidence pointer and no identity, so they register
nothing and local and raw equal share after the first line. `trend` prints a
rule target: `target-N (basename)` at share (unchanged), the P19 form at local
(`~/.../name` inside home, `path-N` outside it, never absolute; trend makes no
observation, so no repository root, D-n), the target as recorded at raw.
"""

from __future__ import annotations

import pytest

from agent_postmortem import disclosure, state
from tests import n3b_helpers as h
from tests import n3b_planted as plant

TARGET_DIR = "zzTarget" + "Plant" + "7d1"


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


def _adopt(target: str) -> None:
    state.update(
        lambda s: s["applied"].append(
            {"rule_id": "R-RETRY-SAFE", "target": target, "applied_at": "2026-08-15T00:00:00Z"}
        )
    )


SINKS = {
    "rules": ["rules"],
    "rules --all": ["rules", "--all"],
    "rules --show": ["rules", "--show", "R-RETRY-SAFE"],
    "rules --run": ["rules", "--run"],
    "rules propose": ["rules", "propose"],
    "rules review": ["rules", "review"],
    "trend": ["trend"],
}


def _argv(name, planted):
    argv = list(SINKS[name])
    if name == "rules --run":
        return [*argv, str(planted.run_dir)]
    if name != "rules --show":
        argv += ["--root", str(planted.root)]
    return argv


# --- the sinks whose text does not depend on the level ----------------------


@pytest.mark.parametrize("name", [name for name in SINKS if name != "trend"])
def test_share_text_has_its_structural_line_and_no_planted_value(name, planted, capsys):
    code, out, err = h.cli(capsys, *_argv(name, planted))
    assert code == 0, err
    expected = {
        "rules": "R-",
        "rules --all": "R-RETRY-SAFE",
        "rules --show": "R-RETRY-SAFE",
        "rules --run": "R-",
        "rules propose": "no rule is backed by 3 runs on 2 days yet",
        "rules review": "no adopted rule to review",
    }[name]
    assert expected in out
    h.assert_nothing_planted(out, planted)
    assert "disclosure:" not in out


@pytest.mark.parametrize("name", [name for name in SINKS if name != "trend"])
def test_local_and_raw_equal_share_after_the_first_line(name, planted, capsys):
    """Nothing these four sinks print depends on the level, so the only
    difference is the label. (A real local test, not the interim one: it also
    pins that no absolute path and no planted value appears.)"""
    _code, share, _err = h.cli(capsys, *_argv(name, planted))
    _code, local, _err = h.cli(capsys, *_argv(name, planted), "--local")
    _code, raw, _err = h.cli(capsys, *_argv(name, planted), "--raw")
    assert local.splitlines()[0] == h.LOCAL_LABEL
    assert raw.splitlines()[0] == h.RAW_LABEL
    assert local.partition("\n")[2] == share
    assert raw.partition("\n")[2] == share
    h.assert_nothing_planted(local, planted)
    h.assert_no_absolute_path(local, planted)


@pytest.mark.parametrize("name", [name for name in SINKS if name != "trend"])
@pytest.mark.parametrize("level", [[], ["--local"], ["--raw"]])
def test_these_sinks_register_nothing_and_print_no_diagnostic(
    name, level, planted, books, tmp_path, monkeypatch, capsys,
):
    h.make_secret_unavailable(monkeypatch, tmp_path)
    _code, _out, err = h.cli(capsys, *_argv(name, planted), *level)
    assert books == []
    assert err == ""


# --- trend ------------------------------------------------------------------


def test_trend_share_keeps_target_n_and_the_basename(planted, capsys):
    _adopt(f"/opt/{TARGET_DIR}/CLAUDE.md")
    _code, out, _err = h.cli(capsys, *_argv("trend", planted))
    assert "target target-1 (CLAUDE.md)" in out
    assert TARGET_DIR not in out
    h.assert_nothing_planted(out, planted)
    assert "disclosure:" not in out


def test_trend_local_outside_home_is_a_numbered_path(planted, capsys):
    _adopt(f"/opt/{TARGET_DIR}/CLAUDE.md")
    _code, out, _err = h.cli(capsys, *_argv("trend", planted), "--local")
    assert out.splitlines()[0] == h.LOCAL_LABEL
    assert "target path-1" in out
    assert "target-1" not in out
    assert TARGET_DIR not in out
    h.assert_no_absolute_path(out, planted)


def test_trend_local_inside_home_is_collapsed_to_the_basename(planted, tmp_path, monkeypatch, capsys):
    home = tmp_path / "zzhome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _adopt(str(home / TARGET_DIR / "notes" / "CLAUDE.md"))
    _code, out, _err = h.cli(capsys, *_argv("trend", planted), "--local")
    assert "target ~/.../CLAUDE.md" in out
    assert TARGET_DIR not in out
    assert str(home) not in out


def test_trend_local_relative_target_cannot_be_placed_so_it_is_numbered(planted, capsys):
    _adopt("CLAUDE.md")
    _code, out, _err = h.cli(capsys, *_argv("trend", planted), "--local")
    assert "target path-1" in out


def test_trend_local_numbers_two_targets_in_order(planted, capsys):
    _adopt(f"/opt/{TARGET_DIR}/a/CLAUDE.md")
    _adopt(f"/opt/{TARGET_DIR}/b/CLAUDE.md")
    _code, out, _err = h.cli(capsys, *_argv("trend", planted), "--local")
    assert "target path-1" in out and "target path-2" in out


def test_trend_raw_shows_the_target_as_recorded(planted, capsys):
    target = f"/opt/{TARGET_DIR}/CLAUDE.md"
    _adopt(target)
    _code, out, _err = h.cli(capsys, *_argv("trend", planted), "--raw")
    assert out.splitlines()[0] == h.RAW_LABEL
    assert f"target {target}" in out


@pytest.mark.parametrize("level", [[], ["--local"], ["--raw"]])
def test_trend_registers_nothing(level, planted, books, capsys):
    _adopt(f"/opt/{TARGET_DIR}/CLAUDE.md")
    h.cli(capsys, *_argv("trend", planted), *level)
    assert books == []


def test_trend_local_without_a_rule_adopted_has_the_label_and_structure(planted, capsys):
    _code, out, _err = h.cli(capsys, *_argv("trend", planted), "--local")
    assert out.splitlines()[0] == h.LOCAL_LABEL
    assert "WEEKLY DETECTOR COUNTS" in out
    assert "(no rule adopted yet" in out
