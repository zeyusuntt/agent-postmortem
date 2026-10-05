"""N3b fix A, S4: at local a `/`-run counts as a path after `:`, `=`, a short
option, `-`, and as a `file://` URL; a quoted path with spaces is replaced
whole.
"""

from __future__ import annotations

import pytest

from tests.test_n3b_local_scrub import _render

SECRET = "zz" + "secret" + "9d1"


# --- S4 ---------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    f"PYTHONPATH=/opt/{SECRET}/a:/opt/{SECRET}/b",
    f"run -C/opt/{SECRET}/c now",
    f"run -C/opt/{SECRET} now",
    f"open file:///opt/{SECRET}/h now",
    f"open '/opt/{SECRET}/e f/g' now",
    f'open "/opt/{SECRET}/e f/g" now',
    f"id R-NOPE-/opt/{SECRET}/x here",
    f"host:/opt/{SECRET}/y done",
])
def test_local_excerpt_keeps_no_absolute_path_after_a_glued_prefix(text):
    out = _render(text)
    assert SECRET not in out, out
    assert "f/g" not in out


@pytest.mark.parametrize("text", [
    f"see (/opt/{SECRET}/d) there",
    f"key=/opt/{SECRET}/j,/opt/{SECRET}/k end",
])
def test_paths_already_scrubbed_stay_scrubbed(text):
    assert SECRET not in _render(text)


@pytest.mark.parametrize("text", [
    "either and/or both",
    "a ratio of 3/4 today",
    "see https://example.org/a/b now",
    "see http://example.org/x now",
    "read-/write is prose",
    "note:/x is prose",
])
def test_local_scrub_leaves_ordinary_slashes_alone(text):
    assert text in _render(text)


def test_the_quoted_path_keeps_its_quotes_and_the_scheme_is_dropped():
    out = _render(f"open '/opt/{SECRET}/e f/g' and file:///opt/{SECRET}/h")
    # F fix 5 (W5-1): the quoted span holds a space, so it carries the marker
    assert "open 'path-1 [text hidden]' and path-2" in out


