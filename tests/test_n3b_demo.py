"""N3b D-i: `demo` is share only. It renders with a fixed, public demo secret
and an in-memory registry (the demo's identities registered in one fixed
order); it never reads or writes the state directory, never takes the lock,
and never prints the unavailable diagnostic; a slow path, if reached, also
stays in memory. So `demo` text is identical on every machine, and the README
demo block stays a byte-for-byte copy of it.
"""

from __future__ import annotations

import os

import pytest

from agent_postmortem import disclosure, report, state
from agent_postmortem.cli import main


def _agent(n: int) -> str:
    return format(n, "017x")


# --- the in-memory book ----------------------------------------------------


def test_demo_secret_is_a_public_32_byte_constant():
    assert isinstance(disclosure.DEMO_SECRET, bytes)
    assert len(disclosure.DEMO_SECRET) == 32


def test_in_memory_book_assigns_from_the_demo_secret_without_touching_state(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the demo must never touch the state directory")

    monkeypatch.setattr(state, "lock_state_dir", boom)
    monkeypatch.setattr(state, "state_dir", boom)
    identities = [("project", "demo"), ("agent", _agent(1)), ("agent", _agent(2))]
    book = disclosure.register_in_memory(identities, disclosure.DEMO_SECRET)
    assert book.available and book.in_memory
    for kind, real in identities:
        expected = disclosure.hmac_hex(disclosure.DEMO_SECRET, real)[:12]
        assert book.alias(kind, real) == expected
    assert book.slow_path_count == 0


def test_in_memory_slow_path_stays_in_memory(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the demo must never touch the state directory")

    monkeypatch.setattr(state, "lock_state_dir", boom)
    book = disclosure.register_in_memory([("agent", _agent(1))], disclosure.DEMO_SECRET)
    late = _agent(9)
    alias = book.alias("agent", late)
    assert alias == disclosure.hmac_hex(disclosure.DEMO_SECRET, late)[:12]
    assert book.slow_path_count == 1
    assert book.available
    assert book.alias("agent", late) == alias  # stable on a second ask
    assert book.slow_path_count == 1


def test_in_memory_registration_order_is_the_collision_tiebreak(monkeypatch):
    """Two identities whose HMACs share their first 12 hex characters: the
    one registered first keeps 12, the second gets 13 (P3), deterministically."""
    first, second = _agent(1), _agent(2)
    monkeypatch.setattr(
        disclosure, "hmac_hex",
        lambda secret, real_id: "ab" * 6 + ("1" if real_id == first else "2") + "c" * 51,
    )
    book = disclosure.register_in_memory(
        [("agent", first), ("agent", second)], disclosure.DEMO_SECRET,
    )
    assert len(book.alias("agent", first)) == 12
    assert len(book.alias("agent", second)) == 13


def test_in_memory_book_rejects_a_non_canonical_id_like_register_does():
    with pytest.raises(ValueError):
        disclosure.register_in_memory([("agent", "not-canonical")], disclosure.DEMO_SECRET)


def test_in_memory_book_names_a_non_canonical_id_positionally_without_a_slow_path():
    book = disclosure.register_in_memory([], disclosure.DEMO_SECRET)
    assert book.alias("agent", "not-canonical") == "agent-1"
    assert book.slow_path_count == 0 and book.available


# --- the demo command ------------------------------------------------------


def _demo(capsys, *args):
    capsys.readouterr()
    code = main(["demo", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_two_demo_runs_with_different_state_homes_print_identical_text(
    tmp_path, monkeypatch, capsys,
):
    outputs = []
    for name in ("first", "second"):
        home = tmp_path / name
        monkeypatch.setenv("XDG_STATE_HOME", str(home))
        _code, out, err = _demo(capsys)
        assert err == ""
        outputs.append(out)
        assert not home.exists() or list(home.rglob("*")) == []
    assert outputs[0] == outputs[1]
    assert outputs[0].startswith("RUN wf_00000000-d30")


def test_demo_uses_the_in_memory_book_and_never_the_state_directory(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(home))

    def boom(*args, **kwargs):
        raise AssertionError("the demo must never touch the state directory")

    monkeypatch.setattr(state, "lock_state_dir", boom)
    monkeypatch.setattr(disclosure, "register", boom)
    seen = []
    real_render = report.render_run_text

    def spy(run, findings, redactor, *args, **kwargs):
        seen.append(redactor.context.book)
        return real_render(run, findings, redactor, *args, **kwargs)

    monkeypatch.setattr(report, "render_run_text", spy)
    code, out, err = _demo(capsys)
    assert code in (0, 3, 4)
    assert err == ""
    assert len(seen) == 1 and seen[0].in_memory and seen[0].available
    assert seen[0].slow_path_count == 0
    assert not home.exists()
    assert os.listdir(tmp_path) == []  # nothing created next to it either


def test_demo_never_prints_the_diagnostic_even_when_state_is_unusable(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / "bad-xdg"
    directory = home / "agent-postmortem"
    directory.mkdir(parents=True, mode=0o700)
    secret = directory / "secret"
    secret.write_bytes(b"s" * 32)
    os.chmod(secret, 0o644)
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    _code, out, err = _demo(capsys)
    assert err == ""
    assert "unavailable" not in out
    assert sorted(p.name for p in directory.iterdir()) == ["secret"]


def test_demo_json_is_unchanged_and_registers_nothing(tmp_path, monkeypatch, capsys):
    home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    _code, out, err = _demo(capsys, "--json")
    assert err == ""
    assert not home.exists()
    assert '"schema"' in out


def test_demo_takes_no_level_flag(capsys):
    with pytest.raises(SystemExit):
        main(["demo", "--local"])
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["demo", "--raw"])


def test_demo_share_text_has_its_structural_lines_and_no_raw_id(capsys):
    """The per-sink share check for demo: a sink that emits nothing fails, and
    none of the demo's raw agent ids may appear."""
    _code, out, _err = _demo(capsys)
    assert out.startswith("RUN wf_00000000-d30  DEGRADED")
    assert "AGENT     PHASE" in out
    assert "agent-1" in out and "agent-2" in out
    assert "STORIES (2)" in out
    for n in (1, 2, 3, 4):
        assert f"ac20000000000000{n}" not in out
    assert "disclosure:" not in out
