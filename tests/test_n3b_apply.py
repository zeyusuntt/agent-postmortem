"""N3b Part 2: `apply` at the three levels (P18, C-4, P19, D-n).

share: no byte derived from the target file. The target prints as path-N, the
requested rule's canonical directive, and one note line saying the actual
patch needs --local or --raw; the already-present message and every error use
path-N too.
local: a unified diff with 3 context lines whose header is the P19 form.
`apply` makes no observation, so it has no repository root (D-n) and every
path counts as outside the repository: the header is `~/.../<basename>` inside
home and `path-N` elsewhere, never absolute.
raw: today's output exactly (usable with git apply) plus the first line.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_postmortem import disclosure, rules
from tests import n3b_helpers as h

PLANT = "zzTarget" + "Plant" + "7e1"
PLANT_BLOCK = "zzBlock" + "Plant" + "7e2"
RULE = "R-RETRY-SAFE"
DIRECTIVE = f"- {RULE}: {rules.RULES[RULE].text}"


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


def _lines(*extra: str) -> str:
    return "".join(f"line {n} {PLANT}\n" for n in range(1, 9)) + "".join(extra)


@pytest.fixture
def plain_target(tmp_path) -> Path:
    target = tmp_path / ("zzDir" + "Plant" + "7e3") / "CLAUDE.md"
    target.parent.mkdir()
    target.write_text(_lines(), encoding="utf-8")
    return target


@pytest.fixture
def edited_target(tmp_path) -> Path:
    """A managed block the user edited by hand: an extra directive of their
    own inside it, between the markers."""
    target = tmp_path / ("zzDir" + "Plant" + "7e7") / "CLAUDE.md"
    target.parent.mkdir()
    target.write_text(
        _lines(
            f"{rules.BEGIN_MARKER}\n",
            f"- R-GIT-CWD: {rules.RULES['R-GIT-CWD'].text} {PLANT_BLOCK}\n",
            f"- my own rule: {PLANT_BLOCK}\n",
            f"{rules.END_MARKER}\n",
            f"tail {PLANT}\n",
        ),
        encoding="utf-8",
    )
    return target


@pytest.fixture
def present_target(tmp_path) -> Path:
    target = tmp_path / ("zzDir" + "Plant" + "7e6") / "CLAUDE.md"
    target.parent.mkdir()
    target.write_text(
        _lines(f"{rules.BEGIN_MARKER}\n", f"{DIRECTIVE}\n", f"{rules.END_MARKER}\n"),
        encoding="utf-8",
    )
    return target


def _apply(capsys, target, *flags, rule=RULE):
    return h.cli(capsys, "apply", rule, "--target", target, *flags)


def _assert_no_target_bytes(out: str, target: Path):
    for fragment in (PLANT, PLANT_BLOCK, "line 1", "my own rule", "tail", str(target),
                     str(target.parent), target.parent.name, "@@"):
        assert fragment not in out, fragment


# --- share ------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["plain_target", "edited_target"])
def test_share_prints_the_target_as_path_n_the_directive_and_one_note(
    fixture, request, capsys,
):
    target = request.getfixturevalue(fixture)
    code, out, _err = _apply(capsys, target)
    assert code == 0
    assert "path-1" in out
    assert DIRECTIVE in out
    notes = [line for line in out.splitlines() if "--local or --raw" in line]
    assert len(notes) == 1
    _assert_no_target_bytes(out, target)
    assert "disclosure:" not in out
    assert not out.startswith("---")


def test_share_output_does_not_depend_on_what_the_target_contains(
    plain_target, edited_target, capsys,
):
    """The two targets differ in every byte; the share output is the same
    but for the one question the already-present message answers."""
    _code, first, _err = _apply(capsys, plain_target)
    _code, second, _err = _apply(capsys, edited_target)
    assert first == second


def test_share_already_present_names_path_n_and_carries_no_target_byte(present_target, capsys):
    code, out, _err = _apply(capsys, present_target)
    assert code == 0
    assert f"postmortem: {RULE} already present in path-1; nothing to add" in out
    _assert_no_target_bytes(out, present_target)
    assert "--local or --raw" not in out


def test_share_never_writes_the_target(plain_target, capsys):
    before = (plain_target.read_bytes(), plain_target.stat().st_mtime_ns)
    _apply(capsys, plain_target)
    assert (plain_target.read_bytes(), plain_target.stat().st_mtime_ns) == before


def test_share_hook_rule_prints_the_hook_note_without_an_absolute_path(plain_target, capsys):
    _code, out, _err = _apply(capsys, plain_target, rule="R-MODEL-ALLOCATION")
    assert "Hook template:" in out
    assert "model_allocation_guard.mjs" in out
    _assert_no_target_bytes(out, plain_target)


# --- share: errors ----------------------------------------------------------


def test_share_error_unterminated_block_names_path_n_and_no_target_byte(tmp_path, capsys):
    target = tmp_path / "bad" / "CLAUDE.md"
    target.parent.mkdir()
    target.write_text(f"{PLANT}\n{rules.BEGIN_MARKER}\n- R-X: {PLANT_BLOCK}\n", encoding="utf-8")
    code, out, err = _apply(capsys, target)
    assert code == 1
    assert out == ""
    assert "path-1" in err
    assert "unterminated" in err
    _assert_no_target_bytes(err, target)


def test_share_error_target_that_is_not_text_carries_no_byte_offsets(tmp_path, capsys):
    target = tmp_path / "bin" / "CLAUDE.md"
    target.parent.mkdir()
    target.write_bytes(PLANT.encode() + b"\xff\xfe" + PLANT_BLOCK.encode())
    code, _out, err = _apply(capsys, target)
    assert code == 1
    assert "path-1" in err
    assert "UTF-8" in err
    assert "position" not in err and "byte 0x" not in err
    _assert_no_target_bytes(err, target)


def test_share_error_unreadable_target_names_path_n_only(tmp_path, capsys):
    target = tmp_path / ("zzDir" + "Plant" + "7e4")
    target.mkdir()
    code, _out, err = _apply(capsys, target)
    assert code == 1
    assert "path-1" in err
    assert str(target) not in err and target.name not in err


def test_share_error_no_such_rule_names_the_expected_kind_and_not_the_value(tmp_path, capsys):
    code, _out, err = _apply(capsys, tmp_path / "x.md", rule="R-ZZ" + "Plant" + "7e5")
    assert code == 1
    assert "no such rule" in err
    assert "R-ZZPlant7e5" not in err
    assert "expected a rule id" in err
    assert "value-1" in err


def test_share_error_refusing_a_run_directory_names_path_n(tmp_path, capsys):
    run_dir = tmp_path / "-Users-zzplant-Proj" / "subagents" / "workflows" / "wf_00000000-b10"
    run_dir.mkdir(parents=True)
    code, _out, err = _apply(capsys, run_dir)
    assert code == 1
    assert "refusing to target a run directory (path-1)" in err
    assert "zzplant" not in err and str(tmp_path) not in err
    assert "never a run directory" in err


# --- local ------------------------------------------------------------------


def test_local_prints_a_unified_diff_with_three_context_lines_and_a_p19_header(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / "zzhome"
    target = home / "proj" / "CLAUDE.md"
    target.parent.mkdir(parents=True)
    target.write_text(_lines(), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    _code, out, _err = _apply(capsys, target, "--local")
    assert out.splitlines()[0] == h.LOCAL_LABEL
    assert out.splitlines()[1] == "--- ~/.../CLAUDE.md"
    assert out.splitlines()[2] == "+++ ~/.../CLAUDE.md"
    hunk = out.splitlines()[3]
    assert hunk == "@@ -6,3 +6,7 @@"
    context = [line for line in out.splitlines() if line.startswith(" line ")]
    assert len(context) == 3
    assert f"line 8 {PLANT}" in out  # the target's own lines are the point at local
    assert DIRECTIVE in out
    assert str(home) not in out and str(target) not in out
    assert out.rstrip().splitlines()[-1] == (
        "Apply it yourself: paste the block into the target file. This diff is for reading; "
        "--raw prints one git apply can use."
    )


def test_local_outside_home_the_header_is_path_n(plain_target, capsys):
    _code, out, _err = _apply(capsys, plain_target, "--local")
    assert out.splitlines()[1] == "--- path-1"
    assert out.splitlines()[2] == "+++ path-1"
    assert str(plain_target) not in out
    assert str(plain_target.parent) not in out


def test_local_a_relative_target_is_placed_against_the_working_directory(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / "zzhome"
    (home / "work").mkdir(parents=True)
    (home / "work" / "CLAUDE.md").write_text(_lines(), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(home / "work")
    _code, out, _err = h.cli(capsys, "apply", RULE, "--target", "CLAUDE.md", "--local")
    assert out.splitlines()[1] == "--- ~/.../CLAUDE.md"


def test_local_user_edited_block_diff_and_already_present_message(
    edited_target, present_target, capsys,
):
    _code, out, _err = _apply(capsys, edited_target, "--local")
    assert "@@" in out and PLANT_BLOCK in out
    h.assert_no_absolute_path(out, type("P", (), {
        "repo": edited_target.parent, "repo_file": edited_target})())
    _code, out, _err = _apply(capsys, present_target, "--local")
    assert f"postmortem: {RULE} already present in path-1; nothing to add" in out


def test_local_hook_note_has_no_absolute_path(plain_target, capsys):
    _code, out, _err = _apply(capsys, plain_target, "--local", rule="R-MODEL-ALLOCATION")
    assert "Hook template:" in out
    for token in out.split():
        assert not (token.startswith("/") and len(token) > 1), token


# --- raw --------------------------------------------------------------------


def test_raw_is_todays_output_after_the_first_line(plain_target, capsys):
    _code, out, _err = _apply(capsys, plain_target, "--raw")
    assert out.splitlines()[0] == h.RAW_LABEL
    expected = rules.apply_rule(RULE, plain_target)
    assert expected.startswith(f"--- {plain_target}\n+++ {plain_target}\n")
    assert out.partition("\n")[2] == (
        expected
        + "Apply it yourself: paste the block into the target file, or save this "
        "output and run git apply.\n"
    )


def test_raw_also_accepts_the_no_redact_spelling(plain_target, capsys):
    _code, raw, _err = _apply(capsys, plain_target, "--raw")
    _code, legacy, _err = _apply(capsys, plain_target, "--no-redact")
    assert raw == legacy


def test_raw_already_present_message_is_todays(present_target, capsys):
    _code, out, _err = _apply(capsys, present_target, "--raw")
    assert f"postmortem: {RULE} already present in {present_target}; nothing to add" in out


def test_raw_error_messages_are_todays(tmp_path, capsys):
    code, _out, err = _apply(capsys, tmp_path / "x.md", "--raw", rule="R-ZZ-NONE")
    assert code == 1
    assert err == "postmortem: no such rule 'R-ZZ-NONE'\n"
    run_dir = tmp_path / "-Users-p" / "subagents" / "workflows" / "wf_00000000-b10"
    run_dir.mkdir(parents=True)
    _code, _out, err = _apply(capsys, run_dir, "--raw")
    assert err == (
        f"postmortem: refusing to target a run directory ({run_dir}); "
        "apply targets a file you maintain, never a run directory\n"
    )


# --- registration -----------------------------------------------------------


@pytest.mark.parametrize("level", [[], ["--local"], ["--raw"]])
def test_apply_registers_nothing_and_prints_no_diagnostic(
    level, plain_target, books, tmp_path, monkeypatch, capsys,
):
    h.make_secret_unavailable(monkeypatch, tmp_path)
    _code, _out, err = _apply(capsys, plain_target, *level)
    assert books == []
    assert err == ""


def test_raw_output_is_applicable_by_git(
    plain_target, tmp_path, capsys,
):
    """raw is the only level whose output git can apply (the header carries the
    real path)."""
    import subprocess

    _code, out, _err = _apply(capsys, plain_target, "--raw")
    patch = out.partition("\n")[2].rsplit("Apply it yourself", 1)[0]
    patch_file = tmp_path / "p.diff"
    patch_file.write_text(patch, encoding="utf-8")
    result = subprocess.run(
        ["git", "apply", "--check", "--unsafe-paths", "-p0", "--directory=/", str(patch_file)],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert re.search(r"^\+- R-RETRY-SAFE", patch, re.MULTILINE)
