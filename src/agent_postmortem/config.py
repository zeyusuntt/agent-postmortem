"""User configuration: quiet a detector without losing it from JSON or trend
(docs/DESIGN.md 4.10), and declare per-repo rescue checks (docs/DESIGN.md
4.11, unit M4).

`${XDG_CONFIG_HOME:-~/.config}/agent-postmortem/config.toml`, overridable by
`AGENT_POSTMORTEM_CONFIG` (a path, tests only). Section `[detectors]`, key
`off = ["<detector id>", ...]`. A silenced detector still runs: its findings
still populate `--json` (each finding gains a `silenced` bool) and `trend`'s
counts; text renderers (`run`, `scan`'s reason lines and top detector,
`demo`) drop them entirely -- never a STORIES entry, never in `--verbose`
FINDINGS, never in WHAT TO DO, never in the account sentence, never in the
verdict -- and a hook never notifies one or counts it toward the hook
verdict word (docs/DESIGN.md 4.6/4.7/7.2).

A critical-severity detector id in `off` is never actually silenced -- a
silenced critical would change the exit code (docs/DESIGN.md 4.6), which is
the one thing a config file must never be able to do quietly. `load_config()`
drops it from `detectors_off` unconditionally and records it in
`critical_rejected` instead of `warnings`, so a caller can tell the two
apart: the CLI (`run`/`scan`/`demo`) turns a non-empty `critical_rejected`
into a stderr message and exit 1 (refusing to run with a config that tried
to do this at all); a hook just proceeds without those ids ever landing in
`detectors_off`, silently, since a hook must never fail a turn.

Missing file: nothing silenced, no warnings. Malformed TOML, a non-list
`off`, a non-string entry, or an unknown detector id: a warning on stderr
(the caller's job to print, redacted), nothing silenced for that entry;
these never raise and never change any exit code.

`[[repos]]` tables (docs/DESIGN.md 4.11), each `{path = "...", checks =
[...]}`: the commands `rescue plan`/`rescue status` treat as this repo's
declared checks, matched by comparing the observed git toplevel (normalized,
`~`-expanded) against `path`, normalized the same way at load time. A
declared check that contains `&&`, `||`, `;`, `|`, a newline, or starts with
`cd ` is a compound or directory-changing command this tool must never be
handed to run verbatim from a fixed repo root; it is rejected at load with a
warning naming it and is never listed as declared. `checks_for_repo()` is the
one place that match happens; nothing else in this module or rescue.py
re-derives it.

`[[adopted]]` tables (docs/DESIGN.md 9, unit M6), each `{rule = "R-...",
date = "YYYY-MM-DD", target = "..."}`: a rule the owner has already put into
a prompt, a hook, or a project's own instructions, by hand, outside this
tool -- `rules review` uses the date to split a run's own detector rate into
before/after windows. `rule` must name a rule in `rules.RULES`; `date` must
parse as a calendar date; `target` is free text (where the owner put it),
never checked against the filesystem. A malformed table, or one naming an
unknown rule or an unparsable date, adds one warning and is dropped; every
other well-formed `[[adopted]]` table is still kept.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import detectors as _detectors
from .models import Finding
from .rules import RULES

_CONFIG_ENV = "AGENT_POSTMORTEM_CONFIG"
_XDG_ENV = "XDG_CONFIG_HOME"

# docs/DESIGN.md 4.11: a declared check containing any of these, or starting
# with "cd ", is rejected at load -- this tool runs a declared check as one
# fixed argument list from the repo root (rescue's own protocol step 3), and
# a compound or directory-changing command could route around that.
_FORBIDDEN_CHECK_FRAGMENTS = ("&&", "||", ";", "|", "\n")


def _check_is_rejected(check: str) -> bool:
    if any(fragment in check for fragment in _FORBIDDEN_CHECK_FRAGMENTS):
        return True
    return check.lstrip().startswith("cd ")


@dataclass(frozen=True)
class RepoConfig:
    """One `[[repos]]` table (docs/DESIGN.md 4.11): `path`, normalized and
    `~`-expanded at load time so `checks_for_repo` is a plain string
    comparison, and `checks`, already filtered of any rejected command."""

    path: str
    checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdoptedRule:
    """One `[[adopted]]` table (docs/DESIGN.md 9): a rule the owner already
    put somewhere by hand, outside this tool. `date` is a plain "YYYY-MM-DD"
    string, already validated as a real calendar date at load time."""

    rule: str
    date: str
    target: str = ""


@dataclass(frozen=True)
class Config:
    detectors_off: frozenset[str]
    warnings: list[str] = field(default_factory=list)
    # A critical detector id named in [detectors].off, never honored (see
    # module docstring). Kept separate from `warnings` so a caller can tell
    # "silence this unknown id anyway, just warn" from "refuse to run".
    critical_rejected: tuple[str, ...] = ()
    # docs/DESIGN.md 4.11: every successfully parsed [[repos]] table, a
    # rejected check already dropped from its own `checks` (with a warning
    # recorded above, not here).
    repos: tuple[RepoConfig, ...] = ()
    # docs/DESIGN.md 9: every successfully parsed [[adopted]] table --
    # `rules review`'s own input, alongside the state store's legacy
    # `applied` entries (7.5).
    adopted: tuple[AdoptedRule, ...] = ()


_EMPTY = Config(detectors_off=frozenset(), warnings=[])

# Process-level cache keyed by the resolved config path (the same pattern
# reader.ParentIndex uses for a parent transcript): re-reading the same path
# twice in one process is wasted work, but a different path (a test's own
# tmp_path, via AGENT_POSTMORTEM_CONFIG) always gets its own fresh parse.
_cache: dict[str, Config] = {}


def config_path() -> Path:
    """`$AGENT_POSTMORTEM_CONFIG`, else
    `${XDG_CONFIG_HOME:-~/.config}/agent-postmortem/config.toml` (docs/
    DESIGN.md 4.8/4.10)."""
    override = os.environ.get(_CONFIG_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get(_XDG_ENV, "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "agent-postmortem" / "config.toml"


def _known_detector_ids() -> frozenset[str]:
    return frozenset(detector.id for detector in _detectors.all_detectors())


def _critical_detector_ids() -> frozenset[str]:
    return frozenset(
        detector.id for detector in _detectors.all_detectors()
        if detector.severity_default == "critical"
    )


def _parse_repos(data: dict, path: Path, warnings: list[str]) -> list[RepoConfig]:
    """Every usable `[[repos]]` table (docs/DESIGN.md 4.11): a malformed
    table, a malformed `path`, a malformed `checks` list, or an individual
    rejected check each add one warning and are dropped -- never raise,
    never abort parsing the rest of the file."""
    raw_repos = data.get("repos")
    if raw_repos is None:
        return []
    if not isinstance(raw_repos, list):
        warnings.append(f"agent-postmortem: {path}: [[repos]] must be an array of tables")
        return []

    repos: list[RepoConfig] = []
    for entry in raw_repos:
        if not isinstance(entry, dict):
            warnings.append(f"agent-postmortem: {path}: a [[repos]] entry must be a table")
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            warnings.append(f"agent-postmortem: {path}: a [[repos]] table needs a non-empty 'path'")
            continue
        norm_path = os.path.normpath(os.path.expanduser(raw_path))

        raw_checks = entry.get("checks", [])
        if not isinstance(raw_checks, list):
            warnings.append(
                f"agent-postmortem: {path}: [[repos]] 'checks' must be a list of strings "
                f"({norm_path})"
            )
            raw_checks = []

        checks: list[str] = []
        for check in raw_checks:
            if not isinstance(check, str) or not check.strip():
                warnings.append(
                    f"agent-postmortem: {path}: a declared check must be a non-empty string "
                    f"({norm_path})"
                )
                continue
            if _check_is_rejected(check):
                warnings.append(
                    f"agent-postmortem: {path}: declared check rejected, compound or "
                    f"directory-changing command: {check!r}"
                )
                continue
            checks.append(check)

        repos.append(RepoConfig(path=norm_path, checks=tuple(checks)))
    return repos


def _parse_adopted(data: dict, path: Path, warnings: list[str]) -> list[AdoptedRule]:
    """Every usable `[[adopted]]` table (docs/DESIGN.md 9): a malformed
    table, an unknown rule id, or an unparsable date each add one warning
    and are dropped -- never raise, never abort parsing the rest of the
    file."""
    raw_adopted = data.get("adopted")
    if raw_adopted is None:
        return []
    if not isinstance(raw_adopted, list):
        warnings.append(f"agent-postmortem: {path}: [[adopted]] must be an array of tables")
        return []

    adopted: list[AdoptedRule] = []
    for entry in raw_adopted:
        if not isinstance(entry, dict):
            warnings.append(f"agent-postmortem: {path}: an [[adopted]] entry must be a table")
            continue
        rule_id = entry.get("rule")
        if not isinstance(rule_id, str) or not rule_id:
            warnings.append(
                f"agent-postmortem: {path}: an [[adopted]] table needs a non-empty 'rule'"
            )
            continue
        if rule_id not in RULES:
            warnings.append(f"agent-postmortem: {path}: unknown rule id '{rule_id}' in [[adopted]]")
            continue

        date = entry.get("date")
        if not isinstance(date, str):
            warnings.append(
                f"agent-postmortem: {path}: an [[adopted]] table needs 'date' as YYYY-MM-DD "
                f"({rule_id})"
            )
            continue
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            warnings.append(
                f"agent-postmortem: {path}: [[adopted]] 'date' is not a valid YYYY-MM-DD date "
                f"({rule_id}: {date!r})"
            )
            continue

        target = entry.get("target", "")
        if not isinstance(target, str):
            warnings.append(
                f"agent-postmortem: {path}: [[adopted]] 'target' must be a string ({rule_id})"
            )
            target = ""

        adopted.append(AdoptedRule(rule=rule_id, date=date, target=target))
    return adopted


def _parse(path: Path) -> Config:
    try:
        raw = path.read_bytes()
    except OSError:
        return _EMPTY

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValueError) as exc:
        return Config(
            detectors_off=frozenset(),
            warnings=[f"agent-postmortem: {path}: could not parse config: {exc}"],
        )

    warnings: list[str] = []
    critical_rejected: list[str] = []
    kept: set[str] = set()

    detectors_section = data.get("detectors")
    if detectors_section is not None:
        if not isinstance(detectors_section, dict):
            warnings.append(f"agent-postmortem: {path}: [detectors] must be a table")
        else:
            off = detectors_section.get("off", [])
            if not isinstance(off, list):
                warnings.append(
                    f"agent-postmortem: {path}: [detectors].off must be a list of strings"
                )
            else:
                known = _known_detector_ids()
                critical = _critical_detector_ids()
                for item in off:
                    if not isinstance(item, str) or not item:
                        warnings.append(
                            f"agent-postmortem: {path}: [detectors].off entries must be "
                            "non-empty strings"
                        )
                        continue
                    if item in critical:
                        critical_rejected.append(item)
                        continue
                    if item not in known:
                        warnings.append(
                            f"agent-postmortem: {path}: unknown detector id '{item}' in "
                            "[detectors].off"
                        )
                        continue
                    kept.add(item)

    repos = _parse_repos(data, path, warnings)
    adopted = _parse_adopted(data, path, warnings)

    return Config(
        detectors_off=frozenset(kept),
        warnings=warnings,
        critical_rejected=tuple(critical_rejected),
        repos=tuple(repos),
        adopted=tuple(adopted),
    )


def load_config() -> Config:
    """The active Config, cached per process by resolved path (docs/
    DESIGN.md 4.10). Never raises: a missing file, a malformed one, or one
    naming an unknown or critical detector id all return a usable Config,
    the problem (if any) recorded in `warnings`/`critical_rejected` for the
    caller to print."""
    path = config_path()
    key = str(path)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    parsed = _parse(path)
    _cache[key] = parsed
    return parsed


def partition_silenced(
    findings: list[Finding], config: Config,
) -> tuple[list[Finding], list[Finding]]:
    """(shown, silenced): findings whose detector id is in
    config.detectors_off go to `silenced`, everything else to `shown`, both
    in the findings' original order. The one split every renderer (docs/
    DESIGN.md 4.6/4.7) and every hook (7.2) uses, so "what counts as
    silenced" is never redefined twice."""
    shown: list[Finding] = []
    silenced: list[Finding] = []
    for finding in findings:
        if finding.detector in config.detectors_off:
            silenced.append(finding)
        else:
            shown.append(finding)
    return shown, silenced


def checks_for_repo(cfg: Config, repo_root: str) -> list[str]:
    """The declared checks for repo_root (docs/DESIGN.md 4.11): the observed
    git toplevel, normalized and `~`-expanded the same way `_parse_repos`
    normalized every `[[repos]]` table's own `path` at load time, so this is
    a plain string comparison. `[]` when no `[[repos]]` table names this
    repo -- rescue.py's own "no checks declared" wording, not this
    function's job."""
    norm_root = os.path.normpath(os.path.expanduser(repo_root))
    for repo in cfg.repos:
        if repo.path == norm_root:
            return list(repo.checks)
    return []
