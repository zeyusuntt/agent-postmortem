"""Tests for agent_postmortem.config (docs/DESIGN.md 4.10): the user config
file that silences a detector without ever hiding it from --json or trend.

Every test points AGENT_POSTMORTEM_CONFIG at its own tmp file, so
load_config()'s process-level cache (keyed by resolved path) never sees the
same path twice across tests and a real machine's own config file is never
touched.
"""

from __future__ import annotations

from pathlib import Path

from agent_postmortem import config
from agent_postmortem.models import Finding


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _finding(detector: str, severity: str = "major") -> Finding:
    return Finding(
        detector=detector, severity=severity, title=f"{detector} fired",
        run_id="wf_00000000-001", agent_id=None, evidence=[], metrics={}, rule_id=None,
        finding_id=f"{detector}0001",
    )


# --- config_path() -----------------------------------------------------


def test_config_path_uses_the_env_override(tmp_path, monkeypatch):
    target = tmp_path / "somewhere" / "config.toml"
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(target))
    assert config.config_path() == target


def test_config_path_falls_back_to_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_POSTMORTEM_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.config_path() == tmp_path / "agent-postmortem" / "config.toml"


def test_config_path_falls_back_to_home_dot_config(monkeypatch):
    monkeypatch.delenv("AGENT_POSTMORTEM_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert config.config_path() == Path.home() / ".config" / "agent-postmortem" / "config.toml"


# --- load_config(): missing file, malformed, unknown id, critical id -------


def test_missing_file_silences_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "does-not-exist.toml"))
    cfg = config.load_config()
    assert cfg.detectors_off == frozenset()
    assert cfg.warnings == []
    assert cfg.critical_rejected == ()


def test_malformed_toml_warns_and_silences_nothing(tmp_path, monkeypatch):
    path = _write(tmp_path, "this is not [valid toml")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.detectors_off == frozenset()
    assert cfg.critical_rejected == ()
    assert len(cfg.warnings) == 1
    assert "could not parse" in cfg.warnings[0]


def test_no_detectors_table_silences_nothing(tmp_path, monkeypatch):
    path = _write(tmp_path, "[other]\nkey = 1\n")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.detectors_off == frozenset()
    assert cfg.warnings == []


def test_a_known_detector_id_is_silenced(tmp_path, monkeypatch):
    path = _write(tmp_path, '[detectors]\noff = ["bash_backgrounded"]\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.detectors_off == frozenset({"bash_backgrounded"})
    assert cfg.warnings == []
    assert cfg.critical_rejected == ()


def test_multiple_known_detector_ids_are_all_silenced(tmp_path, monkeypatch):
    path = _write(tmp_path, '[detectors]\noff = ["bash_backgrounded", "bash_timeout"]\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.detectors_off == frozenset({"bash_backgrounded", "bash_timeout"})


def test_unknown_detector_id_warns_and_is_ignored(tmp_path, monkeypatch):
    path = _write(tmp_path, '[detectors]\noff = ["not_a_real_detector"]\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.detectors_off == frozenset()
    assert cfg.critical_rejected == ()
    assert len(cfg.warnings) == 1
    assert "not_a_real_detector" in cfg.warnings[0]


def test_unknown_id_alongside_a_known_one_only_the_known_one_is_silenced(tmp_path, monkeypatch):
    path = _write(tmp_path, '[detectors]\noff = ["bash_backgrounded", "no_such_thing"]\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.detectors_off == frozenset({"bash_backgrounded"})
    assert len(cfg.warnings) == 1


def test_critical_detector_id_is_never_silenced_and_is_reported_separately(tmp_path, monkeypatch):
    path = _write(tmp_path, '[detectors]\noff = ["codex_write_denied"]\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.detectors_off == frozenset()
    assert cfg.critical_rejected == ("codex_write_denied",)
    # A critical rejection is not folded into the generic warnings list --
    # the CLI needs to tell "refuse to run" from "just warn" apart cheaply.
    assert cfg.warnings == []


def test_missing_artifact_is_also_a_critical_detector_and_cannot_be_silenced(tmp_path, monkeypatch):
    path = _write(tmp_path, '[detectors]\noff = ["missing_artifact"]\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.detectors_off == frozenset()
    assert cfg.critical_rejected == ("missing_artifact",)


def test_off_not_a_list_warns_and_silences_nothing(tmp_path, monkeypatch):
    path = _write(tmp_path, '[detectors]\noff = "bash_backgrounded"\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.detectors_off == frozenset()
    assert len(cfg.warnings) == 1


def test_non_string_entry_in_off_warns_and_is_ignored(tmp_path, monkeypatch):
    path = _write(tmp_path, "[detectors]\noff = [1, \"bash_backgrounded\"]\n")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.detectors_off == frozenset({"bash_backgrounded"})
    assert len(cfg.warnings) == 1


# --- caching: per resolved path, not a single global flag ------------------


def test_load_config_is_cached_per_resolved_path(tmp_path, monkeypatch):
    path = _write(tmp_path, '[detectors]\noff = ["bash_backgrounded"]\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    first = config.load_config()
    # Mutate the file on disk; a cache keyed on the unchanged path must not
    # re-read it (docs/DESIGN.md 4.10: "cached per process").
    path.write_text('[detectors]\noff = ["bash_timeout"]\n', encoding="utf-8")
    second = config.load_config()
    assert second is first
    assert second.detectors_off == frozenset({"bash_backgrounded"})


def test_a_different_path_gets_its_own_fresh_parse(tmp_path, monkeypatch):
    first_path = _write(tmp_path, '[detectors]\noff = ["bash_backgrounded"]\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(first_path))
    first = config.load_config()
    assert first.detectors_off == frozenset({"bash_backgrounded"})

    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second_path = _write(second_dir, '[detectors]\noff = ["bash_timeout"]\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(second_path))
    second = config.load_config()
    assert second.detectors_off == frozenset({"bash_timeout"})


# --- partition_silenced() ---------------------------------------------


def test_partition_silenced_splits_by_detector_id_preserving_order():
    findings = [_finding("bash_backgrounded"), _finding("bash_timeout"), _finding("git_cwd_mismatch")]
    cfg = config.Config(detectors_off=frozenset({"bash_timeout"}))
    shown, silenced = config.partition_silenced(findings, cfg)
    assert [f.detector for f in shown] == ["bash_backgrounded", "git_cwd_mismatch"]
    assert [f.detector for f in silenced] == ["bash_timeout"]


def test_partition_silenced_with_empty_config_keeps_everything_shown():
    findings = [_finding("bash_backgrounded"), _finding("bash_timeout")]
    cfg = config.Config(detectors_off=frozenset())
    shown, silenced = config.partition_silenced(findings, cfg)
    assert shown == findings
    assert silenced == []


# --- [[adopted]] (docs/DESIGN.md 9, unit M6: rules review's own input) -----


def test_no_adopted_table_is_an_empty_tuple(tmp_path, monkeypatch):
    path = _write(tmp_path, "[other]\nkey = 1\n")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.adopted == ()
    assert cfg.warnings == []


def test_a_well_formed_adopted_table_is_kept(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        '[[adopted]]\nrule = "R-GIT-CWD"\ndate = "2026-08-15"\ntarget = "workflow.md"\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.adopted == (config.AdoptedRule(rule="R-GIT-CWD", date="2026-08-15", target="workflow.md"),)
    assert cfg.warnings == []


def test_adopted_table_missing_target_defaults_to_empty_string(tmp_path, monkeypatch):
    path = _write(tmp_path, '[[adopted]]\nrule = "R-GIT-CWD"\ndate = "2026-08-15"\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.adopted == (config.AdoptedRule(rule="R-GIT-CWD", date="2026-08-15", target=""),)


def test_adopted_table_naming_an_unknown_rule_warns_and_is_dropped(tmp_path, monkeypatch):
    path = _write(tmp_path, '[[adopted]]\nrule = "R-NOT-A-RULE"\ndate = "2026-08-15"\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.adopted == ()
    assert len(cfg.warnings) == 1
    assert "R-NOT-A-RULE" in cfg.warnings[0]


def test_adopted_table_with_an_unparsable_date_warns_and_is_dropped(tmp_path, monkeypatch):
    path = _write(tmp_path, '[[adopted]]\nrule = "R-GIT-CWD"\ndate = "not-a-date"\n')
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.adopted == ()
    assert len(cfg.warnings) == 1


def test_adopted_not_a_list_warns_and_is_dropped(tmp_path, monkeypatch):
    path = _write(tmp_path, "adopted = 1\n")
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.adopted == ()
    assert len(cfg.warnings) == 1


def test_one_malformed_adopted_table_does_not_drop_a_well_formed_sibling(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        '[[adopted]]\nrule = "R-NOT-A-RULE"\ndate = "2026-08-15"\n\n'
        '[[adopted]]\nrule = "R-GIT-CWD"\ndate = "2026-08-15"\ntarget = "workflow.md"\n',
    )
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(path))
    cfg = config.load_config()
    assert cfg.adopted == (config.AdoptedRule(rule="R-GIT-CWD", date="2026-08-15", target="workflow.md"),)
    assert len(cfg.warnings) == 1
