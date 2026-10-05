"""N3b fix A, S12: `demo` text and JSON do not depend on the account name.
The demo's disclosure context carries a fixed home that matches nothing on
any machine, so the bare-account-name rule never rewrites a word of it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_postmortem.cli import main

HOMES = ["/srv/people/agent", "/srv/people/run", "/srv/people/postmortem"]
README = Path(__file__).resolve().parent.parent / "README.md"


def _demo(capsys, *args):
    capsys.readouterr()
    main(["demo", *args])
    return capsys.readouterr().out


@pytest.mark.parametrize("home", HOMES)
@pytest.mark.parametrize("args", [(), ("--json",)])
def test_demo_output_is_the_same_whatever_the_home_directory(home, args, monkeypatch, capsys):
    monkeypatch.setenv("HOME", "/srv/zz-neutral-home-4q")
    baseline = _demo(capsys, *args)
    monkeypatch.setenv("HOME", home)
    assert _demo(capsys, *args) == baseline


@pytest.mark.parametrize("home", HOMES)
def test_the_readme_block_is_a_substring_of_the_demo_whatever_the_home(home, monkeypatch, capsys):
    monkeypatch.setenv("HOME", home)
    out = _demo(capsys)
    block = re.search(r"```\n(RUN wf_.*?\n)```", README.read_text(encoding="utf-8"), re.S)
    assert block, "README demo block not found"
    assert block.group(1) in out
