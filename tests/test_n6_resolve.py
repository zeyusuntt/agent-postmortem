"""N6 (docs/DESIGN.md 4.8, 4.13.6): `RUN_ID` accepts a session UUID, an agent
transcript stem, and an agent alias, in the one shared resolver, so run, rescue
plan, rescue status, and rules --run all get them. Rows R1 to R3 and R5 of the
plan's acceptance checklist, plus the candidate function of I2."""

from __future__ import annotations

import pytest

from agent_postmortem import cli, disclosure, reader, state
from tests import n3b_helpers as h
from tests.n6_helpers import (
    EXACT_ADVICE,
    EXPECTED_RUN,
    WF1,
    WF2,
    WF3,
    aid,
    build_identity_root,
    uid,
)


@pytest.fixture
def ident(tmp_path):
    return build_identity_root(tmp_path)


def register_agents(*ids: str) -> dict[str, str]:
    book = disclosure.register([("agent", agent_id) for agent_id in ids])
    assert book.available
    return {agent_id: book.alias("agent", agent_id) for agent_id in ids}


def run_ids_at(root, *paths) -> list[str]:
    return [ref.run_id for ref in reader.discover_runs(root) if ref.path in paths]


# --- R1: each form resolves to the expected run ---------------------------------


def test_r1_each_form_resolves_to_the_expected_run(ident):
    aliases = register_agents(ident.s1a, ident.w1b, ident.ghost, ident.w2a, ident.w3b)
    p = ident.paths
    cases = {
        "session uuid": (ident.s1, p["s1"]),
        "session child stem": (f"agent-{ident.s1a}", p["s1"]),
        "workflow attempt stem": (f"agent-{ident.w2a}", p["wf2"]),
        "transcript-less attempt stem": (f"agent-{ident.ghost}", p["wf1"]),
        "attempt in the second project stem": (f"agent-{ident.w3b}", p["wf3"]),
        "session child alias": (f"a:{aliases[ident.s1a]}", p["s1"]),
        "workflow attempt alias": (f"a:{aliases[ident.w2a]}", p["wf2"]),
        "transcript-less attempt alias": (f"a:{aliases[ident.ghost]}", p["wf1"]),
        "attempt in the second project alias": (f"a:{aliases[ident.w3b]}", p["wf3"]),
        "second attempt alias": (f"a:{aliases[ident.w1b]}", p["wf1"]),
    }
    for name, (form, expected) in cases.items():
        assert cli._resolve_run_path(form, ident.root) == expected, name


def test_r1_a_stray_transcript_named_by_no_journal_event_does_not_resolve(ident):
    aliases = register_agents(ident.stray, ident.w2a)
    assert cli._resolve_run_path(f"agent-{ident.w2a}", ident.root) == ident.paths["wf2"]
    assert cli._resolve_run_path(f"a:{aliases[ident.w2a]}", ident.root) == ident.paths["wf2"]
    with pytest.raises(FileNotFoundError):
        cli._resolve_run_path(f"agent-{ident.stray}", ident.root)
    with pytest.raises(FileNotFoundError):
        cli._resolve_run_path(f"a:{aliases[ident.stray]}", ident.root)


def test_r1_the_forms_work_through_the_command_line(ident, capsys):
    import json

    code, out, _err = h.cli(capsys, "run", ident.s1, "--root", ident.root, "--json")
    assert json.loads(out)["run"]["run_id"] == run_ids_at(ident.root, ident.paths["s1"])[0]
    code, out, _err = h.cli(
        capsys, "run", f"agent-{ident.w2b}", "--root", ident.root, "--json",
    )
    assert json.loads(out)["run"]["run_id"] == WF2
    assert code in (0, 3, 4)


# --- I2: the candidate set --------------------------------------------------------


def _ref(ident, key, kind):
    path = ident.paths[key]
    return reader.RunRef(kind=kind, path=path, run_id=path.name)


def test_i2_candidates_are_a_superset_of_every_attempt_id(ident):
    wf1 = _ref(ident, "wf1", "workflow")
    assert reader.agent_id_candidates(wf1) == {ident.w1a, ident.w1b, ident.ghost}
    wf2 = _ref(ident, "wf2", "workflow")
    assert reader.agent_id_candidates(wf2) == {ident.w2a, ident.w2b, ident.stray}
    s1 = _ref(ident, "s1", "session")
    assert reader.agent_id_candidates(s1) == {ident.s1a, ident.s1b}


def test_i2_a_non_canonical_id_is_dropped_and_a_missing_directory_contributes_nothing(
    tmp_path,
):
    from tests.n6_helpers import workflow_dir, write_lines

    run_dir = workflow_dir(tmp_path / "r", "-home-user-n6a", uid(9), "wf_00000000-6a5")
    write_lines(run_dir / "journal.jsonl", [
        {"type": "started", "key": "k", "agentId": "NOT-AN-AGENT-ID"},
        {"type": "started", "key": "j", "agentId": aid(5)},
    ])
    ref = reader.RunRef(kind="workflow", path=run_dir, run_id=run_dir.name)
    assert reader.agent_id_candidates(ref) == {aid(5)}
    gone = reader.RunRef(kind="workflow", path=tmp_path / "gone", run_id="wf_00000000-6a6")
    assert reader.agent_id_candidates(gone) == frozenset()
    gone_session = reader.RunRef(kind="session", path=tmp_path / "gone.jsonl", run_id="s_x")
    assert reader.agent_id_candidates(gone_session) == frozenset()


def test_i2_confirmation_loads_the_candidate_run_so_a_rejected_started_event_is_no_match(
    tmp_path,
):
    from tests.n6_helpers import workflow_dir, write_lines

    root = tmp_path / "r"
    run_dir = workflow_dir(root, "-home-user-n6a", uid(9), "wf_00000000-6a5")
    write_lines(run_dir / "journal.jsonl", [
        {"type": "started", "key": 5, "agentId": aid(7)},
        {"type": "started", "key": "k", "agentId": aid(8)},
    ])
    with pytest.raises(FileNotFoundError):
        cli._resolve_run_path(f"agent-{aid(7)}", root)
    assert cli._resolve_run_path(f"agent-{aid(8)}", root) == run_dir


# --- R2: ambiguity ----------------------------------------------------------------


def _expected_ambiguity(level, arg, shown, ids, root):
    joined = ", ".join(ids)
    if level == "--raw":
        return f"postmortem: '{arg}' matches more than one run under {root}: {joined}\n"
    return (
        f"postmortem: '{shown}' matches more than one run under path-1: {joined} "
        f"({EXACT_ADVICE})\n"
    )


@pytest.mark.parametrize("level", ["", "--local", "--raw"])
def test_r2_the_same_session_uuid_in_two_projects_is_ambiguous(ident, level, capsys):
    flags = [level] if level else []
    code, out, err = h.cli(capsys, "run", ident.s2, "--root", ident.root, *flags)
    ids = run_ids_at(ident.root, ident.paths["s2a"], ident.paths["s2b"])
    assert len(ids) == 2 and ids[0] != ids[1]
    assert (code, out) == (1, "")
    assert err == _expected_ambiguity(level, ident.s2, f"uuid:{ident.s2[-4:]}", ids, ident.root)


@pytest.mark.parametrize("level", ["", "--local", "--raw"])
def test_r2_the_same_agent_id_in_two_runs_is_ambiguous_by_stem_and_by_alias(
    ident, level, capsys,
):
    aliases = register_agents(ident.w1a)
    flags = [level] if level else []
    ids = run_ids_at(ident.root, ident.paths["wf1"], ident.paths["wf3"])
    assert ids == [WF1, WF3]
    for arg in (f"agent-{ident.w1a}", f"a:{aliases[ident.w1a]}"):
        code, out, err = h.cli(capsys, "run", arg, "--root", ident.root, *flags)
        assert (code, out) == (1, "")
        assert err == _expected_ambiguity(level, arg, "value-1", ids, ident.root)


def test_r2_the_ambiguity_carries_its_dispatch_kind(ident):
    aliases = register_agents(ident.w1a)
    kinds = {}
    for name, arg in (
        ("uuid", ident.s2), ("agent_stem", f"agent-{ident.w1a}"),
        ("alias", f"a:{aliases[ident.w1a]}"), ("prefix", "wf_00000000-6a"),
    ):
        with pytest.raises(cli._AmbiguousRun) as caught:
            cli._resolve_run_path(arg, ident.root)
        kinds[name] = caught.value.kind
    assert kinds == {"uuid": "uuid", "agent_stem": "agent_stem", "alias": "alias",
                     "prefix": "prefix"}


# --- R3: no match -----------------------------------------------------------------


def _secret() -> bytes:
    probe, secret = state.probe_secret(state.secret_path())
    assert probe is state.Probe.VALID
    return secret


def test_r3_no_match_forms_exit_2_with_the_form_naming_sentence(ident, capsys):
    register_agents(ident.w2a)  # a valid state directory
    unregistered_alias = disclosure.hmac_hex(_secret(), ident.w2b)[:12]
    cases = (
        uid(777), f"agent-{aid(777)}", f"a:{unregistered_alias}", f"a:{'0' * 12}",
    )
    for arg in cases:
        code, out, err = h.cli(capsys, "run", arg, "--root", ident.root)
        assert (code, out) == (2, ""), arg
        assert EXPECTED_RUN in err, arg
        assert err.startswith("postmortem: no run matches "), arg


def test_r3_a_twelve_hex_prefix_of_a_longer_alias_of_a_different_identity_is_no_match(
    ident, monkeypatch, capsys,
):
    other = aid(900)
    forced = {other: "c" * 12 + "1" * 52, ident.w2a: "c" * 12 + "2" * 52}
    real = disclosure.hmac_hex
    monkeypatch.setattr(
        disclosure, "hmac_hex",
        lambda secret, real_id: forced.get(real_id) or real(secret, real_id),
    )
    aliases = register_agents(other, ident.w2a)
    assert len(aliases[other]) == 12 and len(aliases[ident.w2a]) == 13
    code, _out, err = h.cli(capsys, "run", f"a:{'c' * 12}", "--root", ident.root)
    assert code == 2 and EXPECTED_RUN in err
    assert cli._resolve_run_path(f"a:{aliases[ident.w2a]}", ident.root) == ident.paths["wf2"]
    assert len(aliases[ident.w2a]) == 13


# --- R5: the same forms through the other commands ---------------------------------


@pytest.mark.parametrize("argv", [
    ["rescue", "plan"], ["rescue", "status"], ["rules", "--run"],
])
def test_r5_the_forms_resolve_through_every_run_resolving_command(ident, argv, capsys):
    aliases = register_agents(ident.w2a)
    expected = h.cli(capsys, *argv, ident.paths["wf2"], "--root", ident.root)
    assert expected[0] in (0, 3, 4)
    for form in (f"agent-{ident.w2a}", f"a:{aliases[ident.w2a]}"):
        got = h.cli(capsys, *argv, form, "--root", ident.root)
        assert got == expected, form
    session = h.cli(capsys, *argv, ident.paths["s1"], "--root", ident.root)
    assert h.cli(capsys, *argv, ident.s1, "--root", ident.root) == session
