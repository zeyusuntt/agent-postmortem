"""Fix round 7 (W7-1, W7-3, W7-4) of field milestone F: an end-to-end probe through the real CLI.

A planted run whose label, phase, description or transcript excerpt holds a private name spelled
the way the review rounds broke the scanner (an operator then a space, a typographic closer, a
time, an ampersand, a Windows path) is read by every command that prints text from a run, at share
and at local, as text, as JSON, with and without `--evidence`. No marker of the private name may
reach stdout or stderr. This is the proof the inventory rows of
`.reviews/tooling/f/inventory_fix7.py` that name an output family point at.

Every fixture is synthetic.
"""

from __future__ import annotations

import json

import pytest

from tests import n3b_helpers as h
from tests import n3b_planted as plant

MARKERS = ("Zq7a", "Zq7b", "Zq7c")

NAMES = {
    "operator_space": "fix /srv/Zq7a; Zq7b/Zq7c.md now",
    "pipe_space": "fix /srv/Zq7a | Zq7b/Zq7c.md now",
    "ampersand": "see /srv/Zq7a & Zq7b/Zq7c.md now",
    "curly_closer": "see /srv/Zq7a\u2019 Zq7b/Zq7c.md now",
    "time": "see /srv/Zq7a 10:30 Zq7b/Zq7c.md now",
    "parenthesis": "see /srv/Zq7a (copy) Zq7b/Zq7c.md now",
    "evidence_shape": "see /srv/Zq7a:12: Zq7b/Zq7c.md now",
    "windows_drive": "open C:\\Docs\\Zq7a Zq7b\\Zq7c.md now",
    "windows_unc": "open \\\\host\\share\\Zq7a Zq7b\\Zq7c.md now",
    "windows_slash": "open C:/Docs/Zq7a Zq7b/Zq7c.md now",
}
FIELDS = ["label", "phase", "description", "excerpt"]


def _rewrite(planted, field, value):
    escaped = json.dumps(value)[1:-1]
    for path in sorted(planted.run_dir.iterdir()):
        if path.suffix not in (".json", ".jsonl"):
            continue
        text = path.read_text(encoding="utf-8")
        if field == "label":
            text = text.replace(plant.LABEL, escaped)
        elif field == "phase":
            text = text.replace(plant.PHASE, escaped)
        elif field == "excerpt":
            text = text.replace(plant.EXCERPT, escaped)
        elif field == "description" and path.name.endswith(".meta.json"):
            data = json.loads(text)
            data["description"] = value
            text = json.dumps(data)
        path.write_text(text, encoding="utf-8")


def _commands(planted):
    run, root = str(planted.run_dir), str(planted.root)
    return {
        "run": ["run", run],
        "run --verbose": ["run", run, "--verbose"],
        "run --verbose --evidence": ["run", run, "--verbose", "--evidence"],
        "run --json": ["run", run, "--json"],
        "run --json --evidence": ["run", run, "--json", "--evidence"],
        "run --bug-report": ["run", run, "--bug-report"],
        "run --bug-report --evidence": ["run", run, "--bug-report", "--evidence"],
        "scan": ["scan", "--root", root],
        "scan --all": ["scan", "--all", "--root", root],
        "scan --json": ["scan", "--json", "--root", root],
        "rescue plan": ["rescue", "plan", run],
        "rescue plan --evidence": ["rescue", "plan", run, "--evidence"],
        "rescue plan --json": ["rescue", "plan", run, "--json"],
        "rescue status": ["rescue", "status", run],
        "rescue status --evidence": ["rescue", "status", run, "--evidence"],
        "rescue status --json": ["rescue", "status", run, "--json"],
        "rules --run": ["rules", "--run", run],
        "rules propose": ["rules", "propose", "--root", root],
        "rules review": ["rules", "review", "--root", root],
        "trend": ["trend", "--root", root],
    }


def _build(tmp_path, field, value):
    planted = plant.build(tmp_path)
    for agent in (planted.agent1, planted.agent2):
        # the meta file carries the description the commands print
        meta = planted.run_dir / f"agent-{agent}.meta.json"
        meta.write_text(
            json.dumps({"agentType": "general-purpose", "description": "plain words"}),
            encoding="utf-8",
        )
    _rewrite(planted, field, value)
    return planted


# a meta description is registered as a name but no command prints it, so raw cannot show it
@pytest.mark.parametrize("field", ["label", "phase", "excerpt"])
def test_fc7_the_probe_is_not_vacuous_raw_prints_the_marker_of_each_field(field, tmp_path, capsys):
    planted = _build(tmp_path, field, NAMES["operator_space"])
    seen = []
    for command, argv in _commands(planted).items():
        _code, out, err = h.cli(capsys, *argv, "--raw")
        if "Zq7a" in out or "Zq7a" in err:
            seen.append(command)
    assert seen, field


@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("name", list(NAMES))
def test_fc7_no_command_prints_a_marker_of_a_private_name(name, field, level, tmp_path, capsys):
    planted = _build(tmp_path, field, NAMES[name])
    leaks = []
    for command, argv in _commands(planted).items():
        _code, out, err = h.cli(capsys, *argv, *(["--local"] if level == "local" else []))
        for marker in MARKERS:
            if marker in out or marker in err:
                leaks.append((command, marker))
    assert not leaks, (name, field, level, leaks[:6])
