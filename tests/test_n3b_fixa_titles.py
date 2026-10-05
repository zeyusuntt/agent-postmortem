"""N3b fix A, S1: a label, phase, or description that holds a UUID, an email,
or a project-slug shape is replaced by its pseudonym in finding titles at
share (names are substituted before structural redaction)."""

from __future__ import annotations

import pytest

from agent_postmortem.cli import main
from agent_postmortem.redact import Redactor
from tests import n3b_planted as plant

SHAPES = {
    "uuid": "uuidlbl " + "-".join(["11111111", "2222", "4333", "8444", "555555555555"]) + " tail",
    "email": "mail zzbob@corp.example.com end",
    "slug": "slug -Users-zzbob-proj end",
}


def _rewrite(planted, old: str, new: str) -> None:
    for path in planted.run_dir.iterdir():
        if path.suffix in (".json", ".jsonl"):
            text = path.read_text(encoding="utf-8")
            if old in text:
                path.write_text(text.replace(old, new), encoding="utf-8")


def _run(planted, capsys, *flags):
    capsys.readouterr()
    code = main(["run", str(planted.run_dir), *flags])
    out = capsys.readouterr().out
    return code, out


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("field", ["label", "phase"])
def test_title_never_prints_a_structural_shaped_name(tmp_path, capsys, shape, field):
    planted = plant.build(tmp_path)
    value = SHAPES[shape]
    _rewrite(planted, getattr(planted, field), value)
    for flags in (("--verbose",), ("--bug-report",)):
        _code, out = _run(planted, capsys, *flags)
        for fragment in ("uuidlbl", "zzbob", "11111111", "corp.example", "uuid:5555", "email:"):
            assert fragment not in out, (flags, fragment, out)


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_title_unit_substitutes_the_registered_name_first(shape):
    redactor = Redactor(enabled=True)
    name = SHAPES[shape]
    pseudonym = redactor.names.agent(name)
    assert redactor.title(f"{name} attempt 1 was killed") == f"{pseudonym} attempt 1 was killed"


def test_title_without_a_registered_name_is_structural_only():
    redactor = Redactor(enabled=True)
    assert redactor.title("see zzbob@corp.example.com") == redactor("see zzbob@corp.example.com")
