"""Fix round 6b (W6-12) of field milestone F: the two detector titles that interpolate a path
first wrap it in literal backquotes (docs/DESIGN.md 4.13.4, 5.1), so the end-of-line rule of the
path scanner hides only the path and the tool's own sentence survives.

Every fixture is synthetic.
"""

from __future__ import annotations

import json
import re

import pytest

from agent_postmortem import cli
from agent_postmortem.detectors import claim_contradiction, missing_artifact
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor
from tests.test_fa_path_token import HOME, _field_session

ABSENT_TAIL = "is absent now; the tool cannot tell whether it existed when the run ended"
UNCHECKED_TAIL = (
    "could not be checked now; the tool cannot tell whether it existed when the run ended"
)
PATH = "/srv/work/cards/x.md"

# F fix 7 (W7-2): the titles are built from structured parts (`TitleParts`), not wrapped in
# backquotes; `build` is the parts builder of each detector
_TITLES = [
    pytest.param(missing_artifact._title_parts, id="missing_artifact"),
    pytest.param(claim_contradiction._report_path_title_parts, id="claim_contradiction"),
]


@pytest.mark.parametrize("build", _TITLES)
@pytest.mark.parametrize(
    ("state", "tail"), [("absent_now", ABSENT_TAIL), ("unknown", UNCHECKED_TAIL)]
)
def test_w612_title_wraps_the_path_in_literal_backquotes(build, state, tail):
    # F fix 7 (W7-2): renamed in spirit only (the id is kept); the backquotes are gone, the
    # title is the real-path sentence of a template and a path
    assert build(PATH, state).raw() == f"{PATH} {tail}"


@pytest.mark.parametrize("build", _TITLES)
@pytest.mark.parametrize(
    ("state", "tail"), [("absent_now", ABSENT_TAIL), ("unknown", UNCHECKED_TAIL)]
)
@pytest.mark.parametrize("level", ["share", "local"])
def test_w612_rendered_title_keeps_the_tool_sentence(build, state, tail, level):
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    names = r.fresh_names(detailed=(level == "local"))
    names.path(PATH)
    shown = r.title_parts(build(PATH, state))  # F fix 7 (W7-2): rendered from parts
    assert shown.endswith(tail), shown
    assert "work" not in shown or level == "local"
    assert "x.md" not in shown or level == "local"


def _missing(payload):
    found = [f for f in payload["findings"] if f["detector"] == "missing_artifact"]
    assert found
    return found


def test_w612_share_title_pseudonym_equals_the_one_in_metrics_paths(tmp_path, monkeypatch, capsys):
    main = _field_session(tmp_path, monkeypatch)
    cli.main(["run", str(main), "--json"])
    payload = json.loads(capsys.readouterr().out)
    for finding in _missing(payload):
        match = re.fullmatch(r"(path-\d+) " + re.escape(ABSENT_TAIL), finding["title"])
        assert match, finding["title"]  # F fix 7 (W7-2): no backquotes
        assert finding["metrics"]["paths"] == [match.group(1)]


def test_w612_local_title_keeps_the_sentence(tmp_path, monkeypatch, capsys):
    main = _field_session(tmp_path, monkeypatch)
    cli.main(["run", str(main), "--json", "--local"])
    payload = json.loads(capsys.readouterr().out)
    for finding in _missing(payload):
        assert finding["title"].endswith(ABSENT_TAIL), finding["title"]
        assert finding["title"].startswith("path-")  # F fix 7 (W7-2): no backquote


def test_w612_raw_title_shows_the_real_path_inside_the_backquotes(tmp_path, monkeypatch, capsys):
    main = _field_session(tmp_path, monkeypatch)
    cli.main(["run", str(main), "--json", "--raw"])
    payload = json.loads(capsys.readouterr().out)
    for finding in _missing(payload):
        match = re.fullmatch(r"(/\S+) " + re.escape(ABSENT_TAIL), finding["title"])  # F fix 7
        assert match, finding["title"]
        assert match.group(1).endswith("/scratchpad/cards/x.md")
        assert finding["metrics"]["paths"] == [match.group(1)]
