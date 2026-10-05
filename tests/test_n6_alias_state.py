"""N6 (docs/DESIGN.md 4.13.5, 4.13.6; plan I3): an agent alias lookup is
read-only, goes through the verified state directory descriptor, and every
failure means "unavailable" (exit 2, never a traceback, never a write). Rows R4
and R4b of the plan's acceptance checklist."""

from __future__ import annotations

import os
import shutil

import pytest

from agent_postmortem import cli, disclosure, state
from tests import n3b_helpers as h
from tests.n6_helpers import EXPECTED_UNAVAILABLE, build_identity_root


@pytest.fixture
def ident(tmp_path):
    return build_identity_root(tmp_path)


def _prepare(tmp_path, monkeypatch, ident):
    xdg = tmp_path / "xdg-n6"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    book = disclosure.register([("agent", ident.w2a)])
    assert book.available
    return xdg / "agent-postmortem", book.alias("agent", ident.w2a)


def _snapshot(path):
    if not os.path.lexists(path):
        return "absent"
    if os.path.islink(path):
        return ("link", os.readlink(path), _snapshot(os.path.realpath(path)))
    info = os.lstat(path)
    out = {"mode": info.st_mode, "mtime": info.st_mtime_ns}
    entries = {}
    for name in sorted(os.listdir(path)):
        child = os.path.join(path, name)
        child_info = os.lstat(child)
        data = None
        if os.access(child, os.R_OK):
            with open(child, "rb") as handle:
                data = handle.read()
        entries[name] = (child_info.st_mode, child_info.st_mtime_ns, data)
    out["entries"] = entries
    return out


class _Forbidden(BaseException):
    """Not an Exception, so the lookup's own `except Exception` can never hide a call."""


def _forbid_writes(monkeypatch):
    def refuse(name):
        def fail(*args, **kwargs):
            raise _Forbidden(f"{name} must never be called by an alias lookup")
        return fail

    for name in ("_ensure_state_dir", "lock_state_dir", "create_secret", "write_registry_bytes"):
        monkeypatch.setattr(state, name, refuse(name))


def _break_absent(directory, tmp_path):
    shutil.rmtree(directory)


def _break_symlink(directory, tmp_path):
    real = directory.parent / "real-state"
    directory.rename(real)
    os.symlink(real, directory)


def _break_mode(directory, tmp_path):
    os.chmod(directory, 0o755)


def _break_secret_missing(directory, tmp_path):
    (directory / "secret").unlink()


def _break_registry_invalid(directory, tmp_path):
    registry = directory / "aliases.json"
    registry.write_text("not json\n", encoding="utf-8")
    os.chmod(registry, 0o600)


def _break_secret_unreadable(directory, tmp_path):
    os.chmod(directory / "secret", 0o000)


ROWS = {
    "state directory absent": _break_absent,
    "state directory is a symlink": _break_symlink,
    "state directory mode 0755": _break_mode,
    "secret missing": _break_secret_missing,
    "registry invalid": _break_registry_invalid,
    "secret unreadable": _break_secret_unreadable,
}


@pytest.mark.parametrize("name", sorted(ROWS))
def test_r4_unavailable_state_is_exit_2_and_changes_nothing(name, ident, tmp_path, monkeypatch, capsys):
    if name == "secret unreadable" and os.geteuid() == 0:
        pytest.skip("mode 000 does not stop root")
    directory, alias = _prepare(tmp_path, monkeypatch, ident)
    ROWS[name](directory, tmp_path)
    before = _snapshot(directory)
    parent_before = sorted(os.listdir(directory.parent))
    _forbid_writes(monkeypatch)
    code, out, err = h.cli(capsys, "run", f"a:{alias}", "--root", ident.root)
    try:
        assert (code, out) == (2, "")
        assert err == (
            "postmortem: stable aliases unavailable on this machine, so 'value-1' "
            f"cannot be resolved ({EXPECTED_UNAVAILABLE})\n"
        )
        assert "Traceback" not in err
        assert _snapshot(directory) == before
        assert sorted(os.listdir(directory.parent)) == parent_before
        if name == "state directory absent":
            assert not os.path.lexists(directory)
    finally:
        if name == "secret unreadable":
            os.chmod(directory / "secret", 0o600)


def test_r4_a_valid_lookup_passes_under_the_same_instrumentation(ident, tmp_path, monkeypatch):
    directory, alias = _prepare(tmp_path, monkeypatch, ident)
    before = _snapshot(directory)
    _forbid_writes(monkeypatch)
    assert cli._resolve_run_path(f"a:{alias}", ident.root) == ident.paths["wf2"]
    got = disclosure.lookup_agent_alias(alias, {"ref": frozenset({ident.w2a, ident.w2b})})
    assert got == {"ref": frozenset({ident.w2a})}
    assert _snapshot(directory) == before


def test_r4_a_lookup_that_finds_nothing_is_an_empty_result_not_unavailable(
    ident, tmp_path, monkeypatch,
):
    directory, alias = _prepare(tmp_path, monkeypatch, ident)
    got = disclosure.lookup_agent_alias("0" * 12, {"ref": frozenset({ident.w2a})})
    assert got == {}
    assert disclosure.lookup_agent_alias(alias, {}) == {}


# --- R4b: the lookup binds both probes to the one verified descriptor ------------


class _Spies:
    def __init__(self, monkeypatch):
        self.opened: list[int] = []
        self.probes: list[tuple[str, object]] = []
        self.closed: list[int] = []
        real_open = state._open_verified_state_dir
        real_secret = state.probe_secret
        real_registry = disclosure._probe_registry
        real_close = os.close

        def spy_open(path):
            fd = real_open(path)
            self.opened.append(fd)
            return fd

        def spy_secret(path, *, dir_fd=None):
            self.probes.append(("secret", dir_fd))
            if getattr(self, "secret_raises", False):
                raise OSError("simulated read failure")
            return real_secret(path, dir_fd=dir_fd)

        def spy_registry(directory, *, dir_fd=None):
            self.probes.append(("registry", dir_fd))
            return real_registry(directory, dir_fd=dir_fd)

        def spy_close(fd):
            self.closed.append(fd)
            return real_close(fd)

        monkeypatch.setattr(state, "_open_verified_state_dir", spy_open)
        monkeypatch.setattr(state, "probe_secret", spy_secret)
        monkeypatch.setattr(disclosure, "_probe_registry", spy_registry)
        monkeypatch.setattr(os, "close", spy_close)

    def assert_bound(self, expected_probes: int):
        assert len(self.opened) == 1
        fd = self.opened[0]
        assert isinstance(fd, int) and fd >= 0
        assert len(self.probes) == expected_probes
        assert all(dir_fd == fd for _name, dir_fd in self.probes), self.probes
        assert self.closed.count(fd) == 1


def test_r4b_a_valid_lookup_binds_both_probes_and_closes_the_descriptor_once(
    ident, tmp_path, monkeypatch,
):
    _directory, alias = _prepare(tmp_path, monkeypatch, ident)
    spies = _Spies(monkeypatch)
    got = disclosure.lookup_agent_alias(alias, {"ref": frozenset({ident.w2a})})
    assert got == {"ref": frozenset({ident.w2a})}
    assert [name for name, _fd in spies.probes] == ["secret", "registry"]
    spies.assert_bound(2)


def test_r4b_a_probe_that_raises_still_closes_the_descriptor_once(ident, tmp_path, monkeypatch):
    _directory, alias = _prepare(tmp_path, monkeypatch, ident)
    spies = _Spies(monkeypatch)
    spies.secret_raises = True
    assert disclosure.lookup_agent_alias(alias, {"ref": frozenset({ident.w2a})}) is None
    spies.assert_bound(1)


def test_r4b_an_invalid_registry_binds_both_probes_and_closes_once(ident, tmp_path, monkeypatch):
    directory, alias = _prepare(tmp_path, monkeypatch, ident)
    registry = directory / "aliases.json"
    registry.write_text("not json\n", encoding="utf-8")
    os.chmod(registry, 0o600)
    spies = _Spies(monkeypatch)
    assert disclosure.lookup_agent_alias(alias, {"ref": frozenset({ident.w2a})}) is None
    spies.assert_bound(2)
