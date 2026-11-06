"""A run id that matches nothing is echoed when it is a public id, and the message says where
the ids are (docs/DESIGN.md 4.8, N3b P13, "run <full session uuid>
fails and names a different id": the message showed a label the person never typed).

A public run id (`wf_...`, `s_...`) is what `scan` prints, so it is not private and the message
repeats it as typed. Anything else (a UUID, a path, a prefix) is still shown as a span. Either
way the message ends with the command that lists the run ids.
"""

from __future__ import annotations

import uuid

from agent_postmortem.cli import main
from tests.test_share_label_case import _workflow

HINT = "next: agent-postmortem scan --all lists every run id"


def _no_match(capsys, root, argument, *flags):
    code = main(["run", argument, "--root", str(root), *flags])
    captured = capsys.readouterr()
    assert code == 2 and captured.out == ""
    return captured.err


def test_a_public_run_id_that_matches_nothing_is_repeated_as_typed(tmp_path, monkeypatch, capsys):
    from tests.test_field_v03 import _no_config
    _no_config(monkeypatch, tmp_path)
    root, _run, _main = _workflow(tmp_path)
    for typed in ("s_ffffffffff", "wf_ffffffff-fff"):
        err = _no_match(capsys, root, typed)
        assert f"no run matches '{typed}'" in err, err
        assert HINT in err


def test_a_uuid_that_matches_nothing_stays_hidden_and_gets_the_hint(tmp_path, monkeypatch, capsys):
    from tests.test_field_v03 import _no_config
    _no_config(monkeypatch, tmp_path)
    root, _run, _main = _workflow(tmp_path)
    typed = str(uuid.UUID(int=0xE24E24E24E24E24E24E24E24E24E24E2))
    err = _no_match(capsys, root, typed)
    assert typed not in err
    assert HINT in err
