"""N7 step 1 (docs/DESIGN.md 4.13.7, 4.13.11 "N7"): detector discovery runs
once per process. `detectors._reset_registry()` is the documented test hook
that clears the cached list; `all_detectors()` hands out a copy, so a caller
that edits its list cannot change what the next caller sees."""

from __future__ import annotations

import pkgutil

from agent_postmortem import detectors


def _count_iter_modules(monkeypatch) -> dict:
    calls = {"n": 0}
    original = pkgutil.iter_modules

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(detectors.pkgutil, "iter_modules", _counting)
    return calls


def test_repeated_calls_discover_once(monkeypatch):
    detectors._reset_registry()
    calls = _count_iter_modules(monkeypatch)
    first = detectors.all_detectors()
    second = detectors.all_detectors()
    assert calls["n"] == 1
    assert [d.id for d in first] == [d.id for d in second]


def test_reset_registry_forces_one_more_discovery(monkeypatch):
    detectors.all_detectors()
    calls = _count_iter_modules(monkeypatch)
    detectors._reset_registry()
    detectors.all_detectors()
    detectors.all_detectors()
    assert calls["n"] == 1


def test_all_detectors_returns_a_copy_of_the_cached_list():
    detectors._reset_registry()
    handed_out = detectors.all_detectors()
    expected = [d.id for d in handed_out]
    handed_out.clear()
    assert [d.id for d in detectors.all_detectors()] == expected


def test_the_cached_list_holds_the_module_level_instances_sorted_by_id():
    ids = [d.id for d in detectors.all_detectors()]
    assert ids == sorted(ids)
    assert "mislabeled_kill" in ids and "wasted_time" in ids


def test_an_empty_discovery_is_cached_too_and_each_call_gets_its_own_list(monkeypatch):
    """N8 fix 2, R2-2: a cached empty list is a cache hit (an `is None` guard, not truthiness)."""
    calls = {"n": 0}

    def _empty(*args, **kwargs):
        calls["n"] += 1
        return iter(())

    monkeypatch.setattr(detectors.pkgutil, "iter_modules", _empty)
    detectors._reset_registry()
    try:
        first = detectors.all_detectors()
        second = detectors.all_detectors()
        assert first == [] and second == []
        assert first is not second
        assert calls["n"] == 1
    finally:
        monkeypatch.undo()
        detectors._reset_registry()
