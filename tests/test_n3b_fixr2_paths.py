"""N3b fix round 2, R2-2: the free-text path scrub also catches a leading
`//` (alone and quoted with spaces) and a bare `/`. The matched spelling is
normalized before placement, so `//a/b` and `/a/b` are one path and one
path-N. At local through every structured name field and --evidence
excerpts; at share through a config warning and an exception text."""

from __future__ import annotations

import pytest

from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor
from tests import n3b_helpers as h
from tests import n3b_planted as plant
from tests.test_n3b_fixa_names import _assert_clean, _rewrite
from tests.test_n3b_local_scrub import _render

MARK = "zz" + "slash" + "7c4"

SPELLINGS = {
    "double": f"//srv/{MARK}/share/file.py",
    "triple": f"///srv/{MARK}/file.py",
    "quoted": f"'//srv/{MARK} path with space/file.py'",
    "dquoted": f'"//srv/{MARK} path with space/file.py"',
}


def _name_cases():
    cases = []
    for kind in sorted(SPELLINGS):
        for field in ("label", "phase", "type", "description"):
            for flags in (("--verbose",), ("--bug-report",)):
                cases.append(pytest.param(kind, field, flags))
    return cases


@pytest.mark.parametrize(("kind", "field", "flags"), _name_cases())
def test_local_names_never_keep_a_leading_double_slash_path(kind, field, flags, tmp_path, capsys):
    planted = plant.build(tmp_path)
    _rewrite(planted, field, SPELLINGS[kind])
    _code, out, _err = h.cli(capsys, "run", planted.run_dir, "--local", *flags)
    _assert_clean(out, planted)
    assert MARK not in out, out
    assert "//srv" not in out


@pytest.mark.parametrize("kind", sorted(SPELLINGS))
def test_local_evidence_excerpts_never_keep_a_leading_double_slash_path(kind):
    out = _render(f"opened {SPELLINGS[kind]} then stopped")
    assert MARK not in out, out
    assert "//srv" not in out
    assert "opened" in out
    if kind in ("quoted", "dquoted"):
        # F fix 6 (W6-2): an unquoted path hides the rest of its line; a closed quote ends the path
        assert "stopped" in out


def test_a_double_slash_path_and_its_single_slash_spelling_are_one_path():
    out = _render(f"a //opt/{MARK}/x.py b /opt/{MARK}/x.py c")
    # F fix 6 (W6-2): the first path hides the rest of its line, the second spelling with it
    assert "a path-1 [text hidden]" in out


def test_a_quoted_double_slash_path_keeps_its_quotes():
    out = _render(f"open '//opt/{MARK} a b/x.py' now")
    # F fix 5 (W5-1): the quoted span holds a space, so it carries the marker
    assert "open 'path-1 [text hidden]' now" in out


def test_a_bare_root_slash_renders_as_path_n_at_local():
    # F fix 7 (W7-1, R1): a path start hides the rest of its line, so the three shapes of the old
    # one-line input are three inputs, each with its own line
    out = _render("(cwd /) and '/' too, then ls /")
    assert "(cwd path-1 [text hidden]" in out
    assert "(cwd path-1)" in _render("(cwd /)")
    assert "see 'path-1' too" in _render("see '/' too")
    assert "then ls path-1" in _render("then ls /")


def test_a_bare_root_slash_as_a_structured_name_at_local():
    redactor = Redactor(context=DisclosureContext(level="local"))
    redactor.fresh_names(detailed=True)
    assert redactor.names.local_text("/") == "path-1"
    assert redactor.names.local_text("//") == "//"
    assert redactor.names.local_text(f"//srv/{MARK}/f") == "path-2"


@pytest.mark.parametrize("text", [
    "either and/or both",
    "a ratio of 3/4 today",
    "see https://example.org/a/b now",
    "see http://example.org/x now",
    "host://srv/x/y is a url",
    "a // comment marker",
    "git checkout / restore / reset on these",
    "read / write",
    "path/to/file stays",
])
def test_ordinary_slashes_stay_alone(text):
    assert text in _render(text)


def _share() -> Redactor:
    return Redactor(context=DisclosureContext(level="share"))


def test_share_config_warning_with_a_double_slash_path_is_scrubbed():
    text = f"agent-postmortem: cannot read config //srv/{MARK}/x.toml: bad value"
    shown = _share().scrub_leftover_paths(text)
    assert MARK not in shown and "//srv" not in shown
    # F fix 6 (W6-2): the path hides the rest of its line
    assert "cannot read config path-1 [text hidden]" in shown


def test_share_config_warning_with_a_quoted_double_slash_path_with_spaces():
    text = f"agent-postmortem: cannot read '//srv/{MARK} a b/x.toml': bad value"
    shown = _share().scrub_leftover_paths(text)
    assert MARK not in shown and "a b" not in shown
    # F fix 5 (W5-1): the quoted span holds a space, so it carries the marker
    assert "cannot read 'path-1 [text hidden]': bad value" in shown


def test_share_exception_text_with_a_double_slash_and_a_bare_root():
    text = f"FileNotFoundError: [Errno 2] No such file: //srv/{MARK}/f.json (cwd /)"
    shown = _share().scrub_leftover_paths(text)
    assert MARK not in shown and "//srv" not in shown
    # F fix 6 (W6-2): the path hides the rest of its line up to the closing parenthesis
    assert "No such file: path-1 [text hidden])" in shown


def test_share_treats_double_and_single_slash_spellings_as_one_path():
    shown = _share().scrub_leftover_paths(f"a //srv/{MARK}/f b /srv/{MARK}/f c")
    # F fix 6 (W6-2): the first path hides the rest of its line, the second spelling with it
    assert "a path-1 [text hidden]" in shown
