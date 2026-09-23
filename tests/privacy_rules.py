"""Shared privacy rules (DESIGN.md 5.4).

Every rule here is a small pure function: a label identifying the item being
checked plus its decoded text in, a list of violation strings out. Both
tests/test_privacy.py and scripts/check-privacy.sh run the same rules over
the same categories of data (the working tree, `git log --all -p`, commit
author fields, and any built dist/ artifact) so the two checks can never
drift apart; the script invokes this module as `python -m tests.privacy_rules`
(or `uv run python -m tests.privacy_rules`). This module lives under tests/
and is a dev-only tool: runtime code under src/agent_postmortem never
imports it.

A handful of files that define or test path redaction itself
(PATH_RULE_EXEMPT_FILES) are exempt from the /Users/ and non-/home/user path
rules only; they must hold literal macOS- and Linux-style example paths
(synthetic account names such as "alice", "bob", "tester") to exercise
redact() at all, and DESIGN.md 5.4 says so.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path

FAKE_UUID = "00000000-0000-4000-8000-000000000000"

# Lookarounds on the hex alphabet, not \b: a word boundary does not fire
# between a hex digit and an adjacent underscore (both are "word" chars to
# re), so a real UUID with an underscore butted against each end (as a
# variable or file-name suffix might produce) would otherwise dodge this
# rule entirely. FAKE_UUID above, surrounded the same way, is the case this
# guards without becoming a self-triggering example: see
# test_rule_uuid_catches_a_uuid_immediately_surrounded_by_underscores.
UUID_RE = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}(?![0-9a-fA-F])"
)
# A name-like segment is required after the prefix so that DESIGN.md's own
# prose describing this rule (backtick-quoted "/Users/" with nothing after
# it) and redact.py's "/Users/<name>/..." placeholder do not self-trigger.
# \w is Unicode-aware by default in Python's re, so a non-ASCII account name
# segment is still caught; see
# test_rule_users_path_catches_a_unicode_account_name.
USERS_PATH_RE = re.compile(r"/Users/[\w.\-]+")
HOME_PATH_RE = re.compile(r"/home/([\w.\-]+)")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
# Same hex-lookaround reasoning as UUID_RE: a synthetic wf_ id immediately
# followed by an underscore (as a longer identifier's prefix, or a file-name
# suffix might produce) must still match; see
# test_rule_wf_id_catches_an_id_immediately_followed_by_an_underscore.
#
# The second alternative (-x) is reader.py's own hash-fallback spelling
# (docs/DESIGN.md 5.1: a non-conforming run directory name becomes 'wf_' +
# sha256(name)[:8] + '-x'): a generated id nobody typed, but still a wf_ id
# that must be checked against the allowlist like any other, or a fallback
# id could slip through unreviewed (M8 review D8).
WF_ID_RE = re.compile(r"wf_[0-9a-f]{8}-(?:[0-9a-f]{3}(?![0-9a-f])|x(?![0-9a-zA-Z]))")

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNTHETIC_IDS_PATH = REPO_ROOT / "tests" / "synthetic-ids.txt"
DENYLIST_LOCAL_PATH = REPO_ROOT / "tests" / "privacy-denylist.local"

# DESIGN.md 5.4: redact.py and its three test files must hold literal
# "/Users/<name>" and "/home/<name>" example paths to test path redaction;
# every other tracked file stays subject to the plain rule.
PATH_RULE_EXEMPT_FILES = frozenset(
    {
        "src/agent_postmortem/redact.py",
        "tests/test_redact.py",
        "tests/test_bug_report.py",
        "tests/test_detectors_core.py",
    }
)

WALK_SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "dist", "build"}

MAX_LINE_LEN = 2000


def load_synthetic_ids(path: Path = SYNTHETIC_IDS_PATH) -> frozenset[str]:
    if not path.exists():
        return frozenset()
    return frozenset(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def load_denylist_terms(path: Path = DENYLIST_LOCAL_PATH) -> tuple[str, ...]:
    if not path.exists():
        return ()
    return tuple(
        line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    )


def digest(value: str) -> str:
    """A short, one-way fingerprint of a matched value.

    A regression in these rules means real private data got tracked; the
    violation message that reports it must not become a second copy of that
    data in pytest output or a CI log. Every message below carries this
    digest, a line number, and a rule id instead of the match itself. A
    developer holding a candidate value can confirm a hit by hashing it the
    same way, locally.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _violation(label: str, rule_id: str, text: str, start: int, matched_value: str) -> str:
    line_no = text.count("\n", 0, start) + 1
    return f"{label}:{line_no}: {rule_id} (digest {digest(matched_value)})"


def rule_uuid(label: str, text: str) -> list[str]:
    violations = []
    for match in UUID_RE.finditer(text):
        if match.group(0) != FAKE_UUID:
            violations.append(_violation(label, "uuid", text, match.start(), match.group(0)))
    return violations


def rule_users_path(label: str, text: str, *, exempt: bool = False) -> list[str]:
    if exempt:
        return []
    return [
        _violation(label, "users_path", text, match.start(), match.group(0))
        for match in USERS_PATH_RE.finditer(text)
    ]


def rule_home_path(label: str, text: str, *, exempt: bool = False) -> list[str]:
    if exempt:
        return []
    violations = []
    for match in HOME_PATH_RE.finditer(text):
        if match.group(1) != "user":
            violations.append(_violation(label, "home_path", text, match.start(), match.group(0)))
    return violations


def rule_email(label: str, text: str) -> list[str]:
    violations = []
    for match in EMAIL_RE.finditer(text):
        email = match.group(0)
        domain = match.group(1).lower()
        if domain == "example.com" or domain.endswith(".example.com"):
            continue
        # The exemption is for anonymized addresses such as GitHub's
        # "<id>+<user>@users.noreply.github.com", where "noreply" is a
        # domain label. It is not for any local part that merely contains
        # the substring "noreply" while the domain is a real one; see
        # test_rule_email_does_not_exempt_noreply_as_a_local_part_substring.
        if "noreply" in domain.split("."):
            continue
        violations.append(_violation(label, "email", text, match.start(), email))
    return violations


def rule_wf_id(label: str, text: str, synthetic_ids: frozenset[str] | None = None) -> list[str]:
    ids = synthetic_ids if synthetic_ids is not None else load_synthetic_ids()
    violations = []
    for match in WF_ID_RE.finditer(text):
        if match.group(0) not in ids:
            violations.append(_violation(label, "wf_id", text, match.start(), match.group(0)))
    return violations


def rule_line_length(label: str, text: str, max_len: int = MAX_LINE_LEN) -> list[str]:
    violations = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if len(line) > max_len:
            violations.append(f"{label}:{line_no}: line_length ({len(line)} chars, over {max_len})")
    return violations


def rule_denylist(label: str, text: str, terms: tuple[str, ...]) -> list[str]:
    if not terms:
        return []
    lowered = text.lower()
    violations = []
    for term in terms:
        start = lowered.find(term.lower())
        if start != -1:
            violations.append(_violation(label, "denylist", text, start, term))
    return violations


def check_text(
    label: str,
    text: str,
    *,
    synthetic_ids: frozenset[str] | None = None,
    denylist_terms: tuple[str, ...] = (),
    check_line_length: bool = False,
    max_line_len: int = MAX_LINE_LEN,
    path_rules_exempt: bool = False,
) -> list[str]:
    """Run every rule against one item's text and return every violation.

    `label` identifies the item in the violation strings: a tracked file's
    repo-relative path, an archive member inside a dist/ artifact, or a fixed
    string such as "git log --all -p" for history.
    """
    violations: list[str] = []
    violations += rule_uuid(label, text)
    violations += rule_users_path(label, text, exempt=path_rules_exempt)
    violations += rule_home_path(label, text, exempt=path_rules_exempt)
    violations += rule_email(label, text)
    violations += rule_wf_id(label, text, synthetic_ids)
    if denylist_terms:
        violations += rule_denylist(label, text, denylist_terms)
    if check_line_length:
        violations += rule_line_length(label, text, max_line_len)
    return violations


def is_binary(data: bytes) -> bool:
    return b"\x00" in data


def decode_or_none(data: bytes) -> str | None:
    if is_binary(data):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def list_tracked_files(root: Path) -> list[Path]:
    """Every tracked file under root, via `git ls-files`; falls back to an
    os.walk that skips .git, .venv, __pycache__, .pytest_cache, dist, and
    build when git itself is unavailable (DESIGN.md 5.4)."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        files = []
        for path in sorted(root.rglob("*")):
            if not (path.is_file() or path.is_symlink()):
                continue
            if any(part in WALK_SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            files.append(path)
        return files
    names = result.stdout.decode("utf-8").split("\x00")
    return [root / name for name in names if name]


def _is_fixture_path(rel: Path) -> bool:
    parts = rel.parts
    return parts[:2] == ("tests", "fixtures") or parts[:3] == ("src", "agent_postmortem", "demo")


def scan_tree(root: Path = REPO_ROOT, extra_denylist_terms: tuple[str, ...] = ()) -> list[str]:
    synthetic_ids = load_synthetic_ids(root / "tests" / "synthetic-ids.txt")
    denylist_terms = load_denylist_terms(root / "tests" / "privacy-denylist.local") + extra_denylist_terms
    violations: list[str] = []
    for path in list_tracked_files(root):
        rel = path.relative_to(root)
        rel_str = rel.as_posix()
        if path.is_symlink():
            # git stores a symlink's blob content as the link target string
            # itself, which is exactly what `git archive` ships; scanning
            # through the link (or skipping a dangling one because
            # path.is_file() is False) would audit the wrong thing, or
            # nothing at all.
            try:
                text = os.readlink(path)
            except OSError:
                continue
        else:
            if not path.is_file():
                continue
            text = decode_or_none(path.read_bytes())
            if text is None:
                continue
        violations += check_text(
            rel_str,
            f"{rel_str}\n{text}",
            synthetic_ids=synthetic_ids,
            denylist_terms=denylist_terms,
            check_line_length=_is_fixture_path(rel),
            path_rules_exempt=rel_str in PATH_RULE_EXEMPT_FILES,
        )
    return violations


DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+) b/(.+)$", re.MULTILINE)


def _split_commit_patch(patch_text: str) -> list[tuple[str | None, str | None, str]]:
    """Split one `git show <commit>` patch into (a_path, b_path, segment)
    pieces: the commit message and metadata first (paths None), then one
    piece per changed file, still carrying both the pre-image ("a/") and
    post-image ("b/") path so a rename cannot dodge an exemption meant for
    either side of it."""
    headers = list(DIFF_HEADER_RE.finditer(patch_text))
    if not headers:
        return [(None, None, patch_text)]
    pieces: list[tuple[str | None, str | None, str]] = [(None, None, patch_text[: headers[0].start()])]
    for i, match in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(patch_text)
        pieces.append((match.group(1), match.group(2), patch_text[match.start() : end]))
    return pieces


def scan_history(
    root: Path = REPO_ROOT,
    public_name: str | None = None,
    public_email: str | None = None,
    extra_denylist_terms: tuple[str, ...] = (),
) -> list[str]:
    """Scan every commit reachable from any ref, every ref name, and every
    annotated tag object (DESIGN.md 5.4: "every ref ... tags, and commit
    author fields").

    Each commit's patch is split per file so the same path exemption and
    fixture line-length rule scan_tree applies can be applied here too;
    `git log --all -p` mashes every file's diff (and the commit message)
    into one blob per commit, which cannot honor a file-specific exemption
    and previously produced false violations from files (such as
    src/agent_postmortem/redact.py) that are exempt because they hold
    literal example paths to test path redaction itself.
    """
    synthetic_ids = load_synthetic_ids(root / "tests" / "synthetic-ids.txt")
    denylist_terms = load_denylist_terms(root / "tests" / "privacy-denylist.local") + extra_denylist_terms
    violations: list[str] = []

    try:
        commits_result = subprocess.run(
            ["git", "rev-list", "--all"], cwd=root, capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return [f"git rev-list --all failed: {exc}"]
    commits = [c for c in commits_result.stdout.decode("utf-8", errors="replace").splitlines() if c]

    for commit in commits:
        show = subprocess.run(
            ["git", "show", "--no-color", commit], cwd=root, capture_output=True, check=True,
        )
        patch_text = show.stdout.decode("utf-8", errors="replace")
        short = commit[:12]
        for a_path, b_path, segment in _split_commit_patch(patch_text):
            if b_path is None:
                label = f"commit {short} (message)"
                check_line_length = False
                path_rules_exempt = False
            else:
                label = f"commit {short} -- {b_path}"
                check_line_length = _is_fixture_path(Path(a_path)) or _is_fixture_path(Path(b_path))
                path_rules_exempt = (
                    a_path in PATH_RULE_EXEMPT_FILES and b_path in PATH_RULE_EXEMPT_FILES
                )
            violations += check_text(
                label, segment,
                synthetic_ids=synthetic_ids, denylist_terms=denylist_terms,
                check_line_length=check_line_length,
                path_rules_exempt=path_rules_exempt,
            )

    identity = subprocess.run(
        ["git", "log", "--all", "--format=%an%n%ae"], cwd=root, capture_output=True, check=True,
    )
    id_text = identity.stdout.decode("utf-8", errors="replace")
    if public_name or public_email:
        allowed = {value for value in (public_name, public_email) if value}
        for line in id_text.splitlines():
            if line and line not in allowed:
                violations.append(f"commit author field: unexpected identity (digest {digest(line)})")

    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)"], cwd=root, capture_output=True, check=True,
    )
    for refname in (r for r in refs.stdout.decode("utf-8", errors="replace").splitlines() if r):
        violations += check_text(
            "ref name", refname, synthetic_ids=synthetic_ids, denylist_terms=denylist_terms,
        )

    tags = subprocess.run(
        ["git", "for-each-ref", "--format=%(objectname) %(objecttype)", "refs/tags"],
        cwd=root, capture_output=True, check=True,
    )
    for line in tags.stdout.decode("utf-8", errors="replace").splitlines():
        oid, _, kind = line.partition(" ")
        if not oid or kind != "tag":
            # A lightweight tag has no tag object of its own (kind is
            # "commit"); the commit it names is already scanned above.
            continue
        cat = subprocess.run(["git", "cat-file", "-p", oid], cwd=root, capture_output=True, check=True)
        tag_text = cat.stdout.decode("utf-8", errors="replace")
        violations += check_text(
            f"annotated tag {oid[:12]}", tag_text,
            synthetic_ids=synthetic_ids, denylist_terms=denylist_terms,
        )

    return violations


# The wheel's package root; hatchling ships src/agent_postmortem/* as
# agent_postmortem/* with the src/ prefix stripped (see
# [tool.hatch.build.targets.wheel] in pyproject.toml), so a wheel member
# under this prefix maps back to the same repo-relative path that
# PATH_RULE_EXEMPT_FILES and _is_fixture_path already know about.
WHEEL_PACKAGE_PREFIX = "agent_postmortem/"
WHEEL_PACKAGE_REPO_PREFIX = "src/agent_postmortem/"


def _wheel_member_repo_path(name: str) -> str:
    if name.startswith(WHEEL_PACKAGE_PREFIX):
        return WHEEL_PACKAGE_REPO_PREFIX + name[len(WHEEL_PACKAGE_PREFIX) :]
    return name


def _sdist_member_repo_path(name: str) -> str:
    # An sdist member is "<project>-<version>/<repo-relative path>"; drop
    # that one leading directory to recover the exact repo-relative path.
    _, sep, rest = name.partition("/")
    return rest if sep else name


def scan_artifacts(
    root: Path = REPO_ROOT,
    artifacts_dir: Path | None = None,
    extra_denylist_terms: tuple[str, ...] = (),
) -> list[str]:
    """Scan every file inside dist/*.whl and dist/*.tar.gz.

    Each archive member is mapped back to the repo-relative path it was
    built from, so it gets the same PATH_RULE_EXEMPT_FILES exemption and
    fixture line-length rule that scan_tree applies to the same file in the
    working tree (DESIGN.md 5.4). Without that mapping, a file exempted
    because it holds literal example paths to test path redaction (for
    instance src/agent_postmortem/redact.py) produces false /Users/
    violations every time it is scanned as a built artifact.
    """
    artifacts_dir = artifacts_dir if artifacts_dir is not None else root / "dist"
    synthetic_ids = load_synthetic_ids(root / "tests" / "synthetic-ids.txt")
    denylist_terms = load_denylist_terms(root / "tests" / "privacy-denylist.local") + extra_denylist_terms
    violations: list[str] = []
    if not artifacts_dir.exists():
        return violations
    for path in sorted(artifacts_dir.glob("*.whl")) + sorted(artifacts_dir.glob("*.tar.gz")):
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    text = decode_or_none(zf.read(name))
                    if text is None:
                        continue
                    rel_str = _wheel_member_repo_path(name)
                    violations += check_text(
                        f"{path.name}:{name}", text,
                        synthetic_ids=synthetic_ids, denylist_terms=denylist_terms,
                        check_line_length=_is_fixture_path(Path(rel_str)),
                        path_rules_exempt=rel_str in PATH_RULE_EXEMPT_FILES,
                    )
        else:
            with tarfile.open(path) as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    text = decode_or_none(extracted.read())
                    if text is None:
                        continue
                    rel_str = _sdist_member_repo_path(member.name)
                    violations += check_text(
                        f"{path.name}:{member.name}", text,
                        synthetic_ids=synthetic_ids, denylist_terms=denylist_terms,
                        check_line_length=_is_fixture_path(Path(rel_str)),
                        path_rules_exempt=rel_str in PATH_RULE_EXEMPT_FILES,
                    )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan for private data (DESIGN.md 5.4).")
    parser.add_argument("--tree", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--artifacts", metavar="DIR", default=None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--public-name", default=None)
    parser.add_argument("--public-email", default=None)
    parser.add_argument(
        "--denylist-term", action="append", default=[],
        help="An extra literal term to search for, on top of tests/privacy-denylist.local "
        "(repeatable; scripts/check-privacy.sh uses this for the current account name).",
    )
    args = parser.parse_args(argv)
    extra_terms = tuple(args.denylist_term)

    nothing_selected = not (args.tree or args.history or args.artifacts)
    violations: list[str] = []
    if args.tree or nothing_selected:
        violations += scan_tree(args.root, extra_denylist_terms=extra_terms)
    if args.history or nothing_selected:
        violations += scan_history(
            args.root, args.public_name, args.public_email, extra_denylist_terms=extra_terms
        )
    if args.artifacts or nothing_selected:
        artifacts_dir = Path(args.artifacts) if args.artifacts else args.root / "dist"
        violations += scan_artifacts(args.root, artifacts_dir, extra_denylist_terms=extra_terms)

    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
