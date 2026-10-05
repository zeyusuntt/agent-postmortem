"""Part FA1 (field milestone F, T2): one path-token scanner for substitution and scrubbing
(docs/DESIGN.md 5.1; .reviews/v0.3/F-plan.md FA1).

The leak: `Pseudonyms.apply_paths` replaced a registered path as a plain substring, so a
registered `/tmp/claude-501` inside the longer `/private/tmp/claude-501/<slug>/<uuid>/
scratchpad/cards/x.md` was replaced mid-token: `/private` + `path-1` + the private tail, and the
structural scrub then no longer saw a path. A registered value is now compared against the
complete path TOKEN that encloses it (the character class `_ABS_PATH_RE` admits, plus `/`):

- the token is exactly the registered value: its pseudonym;
- the token is longer (the value sits inside it, or is a prefix of it): the complete token is
  redacted whole through the single-path renderer of the active level, absolute or relative,
  and neither the registered value nor the continuation survives.

Fixtures are synthetic; the only UUID literal is the fake one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import cli, redact
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor
from tests.test_field_v03 import (
    FAKE_SLUG,
    FAKE_UUID,
    _aid,
    _assistant_text,
    _assistant_tool,
    _no_config,
    _tool_result,
    _user,
    _write_jsonl,
)

REGISTERED = "/tmp/claude-501"
TAIL = f"{FAKE_SLUG}/{FAKE_UUID}/scratchpad/cards/x.md"
HOSTILE = f"/private/tmp/claude-501/{TAIL}"
HOME = Path("/home/user")


def _share(*registered):
    r = Redactor(context=DisclosureContext(level="share", home=HOME))
    names = r.fresh_names()
    for value in registered:
        names.path(value)
    return r


def _local(*registered, repo_root=None):
    r = Redactor(context=DisclosureContext(level="local", home=HOME, repo_root=repo_root))
    names = r.fresh_names(detailed=True)
    for value in registered:
        names.path(value)
    return r


def _both_sinks(r, text):
    """The two text sinks: the plain Redactor call (evidence, diagnostics, metrics values) and
    Redactor.title (Finding.title)."""
    return [r(text), r.title(text)]


# --- the reproduced hostile case -----------------------------------------------------------


def test_fa1_the_reproduced_leak_a_registered_tmp_dir_inside_a_longer_private_tmp_path():
    r = _share(REGISTERED)
    for out in _both_sinks(r, f"{HOSTILE} is absent now"):
        assert "/privatepath" not in out and "/private" + "path-" not in out
        for leaked in ("scratchpad", "cards", FAKE_SLUG, FAKE_UUID, "uuid:"):
            assert leaked not in out, out
        # F fix 6 (W6-2): the path hides the rest of its line
        assert out.endswith(" [text hidden]") and "absent" not in out


def test_fa1_the_hostile_token_without_a_slug_gets_only_the_5_1_collapse():
    r = _share(REGISTERED)
    token = "/private/tmp/claude-501/aa/bb/scratchpad/cards/x.md"
    out = r(f"wrote {token} ok")
    assert out == "wrote path-2 [text hidden]"  # F fix 6 (W6-2)


def test_fa1_the_registered_value_alone_is_still_its_pseudonym():
    r = _share(REGISTERED)
    # F fix 6 (W6-2): the span keeps the registered pseudonym and hides the rest of the line
    assert r(f"cwd {REGISTERED} here") == "cwd path-1 [text hidden]"
    assert r.title(f"cwd {REGISTERED} here") == "cwd path-1 [text hidden]"


def test_fa1_the_same_registered_value_twice_is_the_same_pseudonym():
    r = _share(REGISTERED)
    # F fix 6 (W6-2): one path per line
    assert r(f"{REGISTERED}\n{REGISTERED}") == "path-1\npath-1"


# --- absolute registered value inside a ./ or ../ token, relative inside relative -------------


@pytest.mark.parametrize("lead", ["./", "../", "../../", "x/"])
def test_fa1_an_absolute_value_inside_a_relative_token_is_redacted_whole(lead):
    r = _share("/srv/work/out")
    for out in _both_sinks(r, f"see {lead}srv/work/out/deep/file.md now"):
        assert "work" not in out and "deep" not in out and "file.md" not in out, out
        assert out.startswith("see path-") and out.endswith(" [text hidden]"), out  # F fix 6


def test_fa1_an_absolute_value_after_dot_dot_slash_with_the_slash_inside_the_token():
    r = _share("/srv/work/out")
    out = r("see ../srv/work/out now")
    assert "work" not in out and out.startswith("see path-")


def test_fa1_a_relative_value_inside_a_longer_relative_token():
    r = _share("cards/x.md")
    for out in _both_sinks(r, "wrote scratchpad/cards/x.md ok"):
        assert "scratchpad" not in out and "cards" not in out and "x.md" not in out, out
        assert out.startswith("wrote path-") and out.endswith(" [text hidden]"), out  # F fix 6


def test_fa1_a_relative_value_alone_is_its_pseudonym():
    r = _share("cards/x.md")
    assert r("wrote cards/x.md ok") == "wrote path-1 [text hidden]"  # F fix 6 (W6-2)
    assert r("wrote cards/x.md") == "wrote path-1"


def test_fa1_a_relative_value_glued_to_a_word_character_is_redacted_whole():
    r = _share("out.md")
    out = r("wrote my-out.md and data_out.md")
    assert "my-" not in out and "data_" not in out and "out.md" not in out


# --- every character the structural grammar admits ------------------------------------------


def test_fa1_the_token_alphabet_is_the_one_the_structural_grammar_admits():
    for char in "@+=,%-._":
        assert redact._ABS_PATH_RE.fullmatch("/a" + char + "b"), char
        assert redact._PATH_TOKEN_CHAR_RE.fullmatch(char), char
    assert redact._PATH_TOKEN_CHAR_RE.fullmatch("/")
    for char in " \t\n\"'():;<>|&*?[]{}$`\\~":
        assert not redact._PATH_TOKEN_CHAR_RE.fullmatch(char), repr(char)


@pytest.mark.parametrize("char", list("@+=,%-._"))
def test_fa1_each_admitted_character_right_after_a_registered_value_extends_the_token(char):
    r = _share("/srv/work/out")
    for out in _both_sinks(r, f"see /srv/work/out{char}tail now"):
        assert "work" not in out and "tail" not in out and "out" not in out.replace("path", ""), out
        assert out.startswith("see path-") and out.endswith(" [text hidden]"), out  # F fix 6


@pytest.mark.parametrize("char", list("@+=,%-._"))
def test_fa1_each_admitted_character_right_before_a_registered_value_extends_the_token(char):
    r = _share("/srv/work/out")
    out = r(f"see head{char}/srv/work/out now")
    if char == "=":
        # F fix 4 (W2): `head=` is a proven `NAME=` prefix and stays; the path after it is hidden
        assert out == "see head=path-1 [text hidden]", out  # F fix 6 (W6-2)
    else:
        assert "head" not in out and "work" not in out, out


def test_fa1_a_value_that_is_a_prefix_of_a_longer_path_never_leaves_a_glued_raw_tail():
    r = _share("/srv/work/out")
    out = r("see /srv/work/outputs/final.md now")
    assert "puts" not in out and "final" not in out and "-1s" not in out, out
    assert out.startswith("see path-") and out.endswith(" [text hidden]")  # F fix 6 (W6-2)


# --- punctuation that ends a token ----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("wrote /srv/work/out.", "wrote path-1."),
        ("wrote /srv/work/out, then", "wrote path-1 [text hidden]"),  # F fix 6 (W6-2)
        ("(see /srv/work/out).", "(see path-1)."),
        ("(see /srv/work/out)", "(see path-1)"),
        ("[/srv/work/out]", "[path-1]"),
        ("'/srv/work/out'", "'path-1'"),
        ('"/srv/work/out"', '"path-1"'),
        ("/srv/work/out:12", "path-1:12"),
        ("/srv/work/out:12:3", "path-1:12:3"),
        ("/srv/work/out;", "path-1;"),
    ],
)
def test_fa1_trailing_punctuation_and_pointers_end_the_token(text, expected):
    r = _share("/srv/work/out")
    assert r(text) == expected


def test_fa1_a_glued_assignment_is_one_token_and_is_redacted_whole():
    r = _share("/srv/work/out")
    out = r("PYTHONPATH=/srv/work/out ls")
    # F fix 4 (W2, W3): `NAME=` is a proven prefix and stays; a colon is path text, so a list
    # is one path rendered once
    # F fix 6 (W6-2): the path hides the rest of its line
    assert out == "PYTHONPATH=path-1 [text hidden]" and "work" not in out
    assert r("PATH=/usr/bin:/srv/work/out ls") == "PATH=path-2 [text hidden]"


def test_fa1_a_dot_before_a_slash_stays_in_the_token_and_a_dot_before_whitespace_ends_it():
    r = _share("/srv/work/out")
    assert r("/srv/work/out. Next") == "path-1 [text hidden]"  # F fix 6 (W6-2)
    assert r("/srv/work/out.\nNext") == "path-1.\nNext"
    assert "work" not in r("/srv/work/out./sub")


# --- a registered value that contains a space ------------------------------------------------


@pytest.mark.parametrize(
    "template",
    ['"{}/sub/file.md"', "{}/sub/file.md", "'{}/sub/file.md'", "see {}/sub/file.md now"],
)
def test_fa1_a_registered_value_with_a_space_followed_by_a_continuation(template):
    r = _share("/srv/my work/out")
    text = template.format("/srv/my work/out")
    for out in _both_sinks(r, text):
        for leaked in ("my work", "sub", "file.md", "work"):
            assert leaked not in out, out
        assert "path-" in out


def test_fa1_a_registered_value_with_a_space_alone_is_its_pseudonym():
    r = _share("/srv/my work/out")
    # F fix 5 (W5-1): a span whose decoded text holds whitespace carries the marker, even when
    # it is exactly a registered value
    assert r("at /srv/my work/out now") == "at path-1 [text hidden]"  # F fix 6 (W6-2)
    assert r('at "/srv/my work/out" now') == 'at "path-1 [text hidden]" now'


# --- tokens that are not absolute, and mixed registered values ------------------------------


def test_fa1_longest_registered_value_wins_when_two_overlap():
    r = _share("/srv/work", "/srv/work/out")
    # F fix 6 (W6-2): a path hides the rest of its line (the pseudonym is the registered one)
    assert r("a /srv/work/out b") == "a path-2 [text hidden]"
    assert r("a /srv/work b") == "a path-1 [text hidden]"
    # one token holding both registered values is one span, rendered once as its own path
    assert r("a /srv/work/out/deeper b") == "a path-3 [text hidden]"


def test_fa1_a_pseudonym_is_never_rematched_by_a_shorter_registered_value():
    r = _share("/srv/a", "th-1")
    assert r("x /srv/a y") == "x path-1 [text hidden]"  # F fix 6 (W6-2)


def test_fa1_two_separate_occurrences_each_become_their_own_token():
    r = _share("/srv/work/out")
    # F fix 6 (W6-2): one path per line
    out = r("/srv/work/out\n../x/srv/work/out/more.md")
    assert out.startswith("path-1\npath-")
    assert "more.md" not in out and "work" not in out


def test_fa1_the_root_value_is_never_substituted():
    r = _share("/")
    assert r("a / b /srv/x").startswith("a / b")


def test_fa1_text_with_no_registered_value_is_unchanged():
    # F fix 4 (W4): the plain share call scrubs an unregistered absolute path too (the old
    # assertion pinned `nothing to hide in /srv/other/place here` unchanged); prose is unchanged
    r = _share("/srv/work/out")
    # F fix 6 (W6-2): the path hides the rest of its line
    assert r("nothing to hide in /srv/other/place here") == "nothing to hide in path-2 [text hidden]"
    text = "nothing to hide in prose, and/or https://x.example/y here"
    assert r(text) == text


# --- a label that contains a path ------------------------------------------------------------


def test_fa1_a_label_that_contains_a_path_is_covered_in_title_and_evidence():
    r = _share("/srv/work/out.md")
    label = "build /srv/work/out.md report"
    r.names.agent(label)
    assert r.title(f"{label} failed") == "agent-1 failed"
    assert r(f"{label} failed") == "build path-1 [text hidden]"  # F fix 6 (W6-2)


# --- local level: the same rule through the local renderer ---------------------------------


def test_fa1_local_the_hostile_case_is_redacted_whole_through_the_local_path_function():
    r = _local(REGISTERED)
    for out in _both_sinks(r, f"{HOSTILE} is absent now"):
        for leaked in ("scratchpad", "cards", FAKE_UUID, "claude-501"):
            assert leaked not in out, out
        assert out.endswith(" [text hidden]") and "absent" not in out  # F fix 6 (W6-2)


def test_fa1_local_a_relative_token_becomes_its_own_path_n():
    r = _local("cards/x.md")
    out = r("wrote scratchpad/cards/x.md ok")
    assert "scratchpad" not in out and "cards" not in out
    assert out.startswith("wrote path-")


def test_fa1_local_an_exact_registered_value_keeps_its_local_display():
    r = _local("/srv/work/out")
    assert r("at /srv/work/out now") == "at path-1 [text hidden]"  # F fix 6 (W6-2)
    assert r("at /srv/work/out") == "at path-1"


def test_fa1_local_a_longer_token_inside_home_keeps_its_home_form():
    r = _local("/home/user/proj")
    # F fix 6 (W6-2): the path ends the line (a trailing word would be hidden with it)
    out = r("at /home/user/proj/deep/er/file.md")
    assert out == "at ~/.../file.md"


# --- a home-relative token `~/...` (the display form of a path) ------------------------------


def test_fa1_an_already_rendered_home_form_keeps_its_form_when_a_registered_value_ends_it():
    """`apply` registers the relative target `CLAUDE.md` and prints a label that is already its
    local display `~/.../CLAUDE.md`; passing that label through the redactor again must not
    split the token at the `~`."""
    local = _local("CLAUDE.md")
    assert local("--- ~/.../CLAUDE.md") == "--- ~/.../CLAUDE.md"
    share = _share("CLAUDE.md")
    # R2-2: no display-form exemption at share; the enclosed registered value makes the whole
    # token its own path-N
    assert share("--- ~/.../CLAUDE.md") == "--- path-2"


def test_fa1_a_tilde_that_is_not_a_home_prefix_is_not_part_of_the_token():
    # F fix 4 (W2): nothing but a proven prefix survives in front of a registered match, so the
    # glued `a~` is part of the path (the old assertion kept `a~`)
    r = _share("out.md")
    assert r("a~out.md b") == "path-2 [text hidden]"  # F fix 6 (W6-2)


# --- raw is untouched -----------------------------------------------------------------------


def test_fa1_raw_prints_the_text_as_is():
    r = Redactor(context=DisclosureContext(level="raw", home=HOME))
    r.fresh_names().path(REGISTERED)
    assert r(HOSTILE) == HOSTILE


# --- Pseudonyms alone (no renderer) ---------------------------------------------------------


def test_fa1_pseudonyms_apply_paths_alone_uses_its_own_numbering_for_a_longer_token():
    names = redact.Pseudonyms()
    names.path("/srv/work/out")
    # F fix 6 (W6-2): one path per line (the first path would hide the rest of its line)
    out = names.apply_paths("see /srv/work/outputs/final.md\nand /srv/work/out")
    assert out.endswith("\nand path-1")
    assert "final.md" not in out and "puts" not in out
    assert names.apply_paths("/srv/work/out") == "path-1"
    assert redact.Pseudonyms(enabled=False).apply_paths("/srv/work/out") == "/srv/work/out"


# --- public: share run --json (the field case) ----------------------------------------------

SUB = _aid(0x1F)


def _field_session(tmp_path, monkeypatch):
    """A session whose subagent worked in the registered cwd /tmp/claude-501 and was told to
    write a card under /private/tmp/claude-501/<slug>/<uuid>/scratchpad/cards/ (absent now)."""
    _no_config(monkeypatch, tmp_path)
    root = tmp_path / "root"
    base = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    main = root / FAKE_SLUG / f"{FAKE_UUID}.jsonl"
    _write_jsonl(main, [
        {**_user("Plan.", base), "sessionId": FAKE_UUID},
        {**_assistant_text("Delegating.", base + timedelta(seconds=1)), "sessionId": FAKE_UUID},
    ])
    absent = f"/private/tmp/claude-501/{FAKE_SLUG}/{FAKE_UUID}/scratchpad/cards/x.md"
    sub = main.parent / FAKE_UUID / "subagents" / f"agent-{SUB}.jsonl"
    bash = _assistant_tool("tb", "Bash", {"command": "ls"}, base + timedelta(seconds=3), SUB)
    bash["cwd"] = REGISTERED
    _write_jsonl(sub, [
        _user(f"Please write the card notes to {absent} when done.", base + timedelta(seconds=2), SUB),
        bash,
        _tool_result("tb", "ok", base + timedelta(seconds=4), SUB),
        _assistant_text("Done.", base + timedelta(seconds=5), SUB),
    ])
    return main


def test_fa1_share_run_json_leaks_no_suffix_of_the_longer_path(tmp_path, monkeypatch, capsys):
    main = _field_session(tmp_path, monkeypatch)
    cli.main(["run", str(main), "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    missing = [f for f in payload["findings"] if f["detector"] == "missing_artifact"]
    assert missing, "the fixture must produce the missing_artifact finding of the field case"
    for needle in ("scratchpad", "cards", "/privatepath"):
        assert needle not in out, needle
    for finding in missing:
        # F fix 1, S1: the collapsed form still holds the registered /tmp/claude-501, so the
        # whole token is its own path-N
        # F fix 7 (W7-2): the title renders from structured parts (no backquotes), so the
        # sentence survives and the title's pseudonym is the one in metrics.paths
        assert finding["title"] == (
            "path-2 is absent now; the tool cannot tell whether it existed when the run ended"
        )
        assert finding["metrics"]["paths"] == ["path-2"]
        assert finding["evidence"] == [f"agent-{SUB[1:]}.jsonl:1"] or "scratchpad" not in json.dumps(
            finding["evidence"]
        )


def test_fa1_share_run_text_leaks_no_suffix_of_the_longer_path(tmp_path, monkeypatch, capsys):
    main = _field_session(tmp_path, monkeypatch)
    cli.main(["run", str(main), "--verbose", "--evidence"])
    out = capsys.readouterr().out
    for needle in ("scratchpad", "cards"):
        assert needle not in out, needle
