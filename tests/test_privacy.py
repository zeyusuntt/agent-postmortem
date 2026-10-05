"""Repository privacy audit (DESIGN.md 5.4).

Scans every tracked file (`git ls-files`) for real data: a UUID other than
the fake one; a /Users/ path or a /home/ path outside /home/user; an email
outside example.com/noreply; a wf_ id missing from tests/synthetic-ids.txt;
an overlong line in a fixture; and any term in the optional local denylist
tests/privacy-denylist.local, when that file exists. Also scans dist/*.whl
and dist/*.tar.gz, when present.

The rule functions live in tests/privacy_rules.py, shared with
scripts/check-privacy.sh, so the tree check here and the script's tree,
history, and artifact checks can never drift apart. This replaces
tests/test_fixture_privacy.py, which scanned only tests/fixtures/; DESIGN.md
5.4 now requires every tracked file.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
import zipfile
from pathlib import Path

from tests import privacy_rules as rules

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_repository_tree_has_no_private_data():
    violations = rules.scan_tree(REPO_ROOT)
    assert violations == [], "\n".join(violations)


def test_built_artifacts_have_no_private_data():
    """A no-op when dist/ holds no wheel or sdist; CI builds the package
    first and this test then covers what it produced."""
    violations = rules.scan_artifacts(REPO_ROOT)
    assert violations == [], "\n".join(violations)


# --- scan_artifacts: a wheel/sdist member must get the same exemption and
# fixture rule as the repo-relative path it was built from -----------------


def _write_wheel(path: Path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)


def _write_sdist(path: Path, top: str, members: dict[str, str]) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name, content in members.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=f"{top}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_scan_artifacts_exempts_a_wheel_member_mapped_to_an_exempt_path(tmp_path):
    users_path_value = "/Users/" + "realaccountname"
    _write_wheel(
        tmp_path / "agent_postmortem-0.0.1-py3-none-any.whl",
        {"agent_postmortem/redact.py": f"# {users_path_value}\n"},
    )
    violations = rules.scan_artifacts(REPO_ROOT, artifacts_dir=tmp_path)
    assert violations == [], "\n".join(violations)


def test_scan_artifacts_still_flags_a_non_exempt_wheel_member(tmp_path):
    users_path_value = "/Users/" + "realaccountname"
    _write_wheel(
        tmp_path / "agent_postmortem-0.0.1-py3-none-any.whl",
        {"agent_postmortem/cli.py": f"# {users_path_value}\n"},
    )
    violations = rules.scan_artifacts(REPO_ROOT, artifacts_dir=tmp_path)
    assert violations
    assert any("users_path" in v for v in violations)


def test_scan_artifacts_exempts_an_sdist_member_mapped_to_an_exempt_path(tmp_path):
    users_path_value = "/Users/" + "realaccountname"
    _write_sdist(
        tmp_path / "agent_postmortem-0.0.1.tar.gz",
        "agent_postmortem-0.0.1",
        {"tests/test_redact.py": f"# {users_path_value}\n"},
    )
    violations = rules.scan_artifacts(REPO_ROOT, artifacts_dir=tmp_path)
    assert violations == [], "\n".join(violations)


def test_scan_artifacts_still_flags_a_non_exempt_sdist_member(tmp_path):
    users_path_value = "/Users/" + "realaccountname"
    _write_sdist(
        tmp_path / "agent_postmortem-0.0.1.tar.gz",
        "agent_postmortem-0.0.1",
        {"tests/test_reader.py": f"# {users_path_value}\n"},
    )
    violations = rules.scan_artifacts(REPO_ROOT, artifacts_dir=tmp_path)
    assert violations
    assert any("users_path" in v for v in violations)


def test_scan_artifacts_applies_the_fixture_line_length_rule_to_an_sdist_member(tmp_path):
    _write_sdist(
        tmp_path / "agent_postmortem-0.0.1.tar.gz",
        "agent_postmortem-0.0.1",
        {"tests/fixtures/runs/case/journal.jsonl": "x" * 2001 + "\n"},
    )
    violations = rules.scan_artifacts(REPO_ROOT, artifacts_dir=tmp_path)
    assert violations
    assert any("line_length" in v for v in violations)


def test_scan_artifacts_ignores_line_length_outside_a_fixture_path(tmp_path):
    _write_sdist(
        tmp_path / "agent_postmortem-0.0.1.tar.gz",
        "agent_postmortem-0.0.1",
        {"src/agent_postmortem/cli.py": "x" * 2001 + "\n"},
    )
    violations = rules.scan_artifacts(REPO_ROOT, artifacts_dir=tmp_path)
    assert violations == [], "\n".join(violations)


# --- scan_tree: a tracked symlink must be audited as git stores it, not as
# the filesystem happens to resolve it -------------------------------------


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def test_scan_tree_scans_a_dangling_symlinks_target_string(tmp_path):
    """`path.is_file()` is False for a dangling symlink, so the old code
    skipped it outright; the target string is still what `git archive`
    ships, and it can itself be a private path."""
    _init_git_repo(tmp_path)
    secret = "/Users/" + "realaccountname" + "/private/notes.md"
    link = tmp_path / "dangling_link.txt"
    link.symlink_to(secret)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    violations = rules.scan_tree(tmp_path)
    assert violations
    assert any("users_path" in v for v in violations)


def test_scan_tree_scans_a_live_symlinks_link_string_not_the_resolved_files_content(tmp_path):
    """`path.is_file()` is True for a live symlink and the old code then
    read the *target file's* bytes; git stores the link string itself as
    the blob content, and that is what must be scanned."""
    _init_git_repo(tmp_path)
    secret = "/Users/" + "realaccountname" + "/private/notes.md"
    target = tmp_path / "outside_secret.txt"
    target.write_text(secret + "\n", encoding="utf-8")
    link = tmp_path / "live_link.txt"
    link.symlink_to(target)
    # Only the symlink is tracked; the file it happens to resolve to on this
    # machine is not, so any violation here can only have come from reading
    # through the link rather than the tracked blob content.
    subprocess.run(["git", "add", "live_link.txt"], cwd=tmp_path, check=True)
    violations = rules.scan_tree(tmp_path)
    assert violations == [], "\n".join(violations)


# --- file discovery ---------------------------------------------------


def test_list_tracked_files_includes_a_file_known_to_be_tracked():
    tracked = {p.relative_to(REPO_ROOT).as_posix() for p in rules.list_tracked_files(REPO_ROOT)}
    assert "docs/DESIGN.md" in tracked
    assert "tests/privacy_rules.py" in tracked


def test_list_tracked_files_falls_back_to_a_skip_walk_when_git_is_unavailable(tmp_path, monkeypatch):
    (tmp_path / "keep.txt").write_text("hello\n", encoding="utf-8")
    skipped = tmp_path / "__pycache__"
    skipped.mkdir()
    (skipped / "ignored.txt").write_text("ignored\n", encoding="utf-8")

    def _no_git(*args, **kwargs):
        raise FileNotFoundError("git not installed")

    monkeypatch.setattr(rules.subprocess, "run", _no_git)
    found = {p.relative_to(tmp_path).as_posix() for p in rules.list_tracked_files(tmp_path)}
    assert found == {"keep.txt"}


def test_is_binary_detects_a_null_byte():
    assert rules.is_binary(b"abc\x00def") is True
    assert rules.is_binary(b"plain text") is False


def test_is_fixture_path_matches_fixtures_and_demo_but_not_other_source():
    assert rules._is_fixture_path(Path("tests/fixtures/runs/clean_run/journal.jsonl"))
    assert rules._is_fixture_path(Path("src/agent_postmortem/demo/-home-user-project/00000000-0000-4000-8000-000000000000/subagents/workflows/wf_00000000-d30/journal.jsonl"))
    assert not rules._is_fixture_path(Path("src/agent_postmortem/reader.py"))
    assert not rules._is_fixture_path(Path("tests/test_reader.py"))


# --- scan_history: every commit's per-file patches, refs, and annotated
# tags, each with the same exemption/fixture treatment scan_tree gives the
# same path -----------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_history_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.com")


def _commit_files(root: Path, files: dict[str, str], message: str) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def test_scan_history_exempts_a_commit_touching_an_exempt_path(tmp_path):
    _init_history_repo(tmp_path)
    users_path_value = "/Users/" + "realaccountname"
    _commit_files(tmp_path, {"src/agent_postmortem/redact.py": f"# {users_path_value}\n"}, "add redact.py")
    violations = rules.scan_history(tmp_path)
    assert violations == [], "\n".join(violations)


def test_scan_history_still_flags_a_non_exempt_commit(tmp_path):
    _init_history_repo(tmp_path)
    users_path_value = "/Users/" + "realaccountname"
    _commit_files(tmp_path, {"src/agent_postmortem/cli.py": f"# {users_path_value}\n"}, "add cli.py")
    violations = rules.scan_history(tmp_path)
    assert violations
    assert any("users_path" in v for v in violations)


def test_scan_history_applies_the_fixture_line_length_rule(tmp_path):
    _init_history_repo(tmp_path)
    _commit_files(
        tmp_path, {"tests/fixtures/runs/case/journal.jsonl": "x" * 2001 + "\n"}, "add an overlong fixture line",
    )
    violations = rules.scan_history(tmp_path)
    assert violations
    assert any("line_length" in v for v in violations)


def test_scan_history_flags_a_private_ref_name(tmp_path):
    _init_history_repo(tmp_path)
    _commit_files(tmp_path, {"a.txt": "hi\n"}, "init")
    branch_name = "notes-" + "11111111-2222-4000-8000-" + "333333333333"
    _git(tmp_path, "branch", branch_name)
    violations = rules.scan_history(tmp_path)
    assert violations
    assert any("uuid" in v for v in violations)


def test_scan_history_flags_data_in_an_annotated_tag(tmp_path):
    _init_history_repo(tmp_path)
    _commit_files(tmp_path, {"a.txt": "hi\n"}, "init")
    real_email = "real.person" + "@" + "realcompany.test"
    _git(tmp_path, "tag", "-a", "v1", "-m", f"tagged by {real_email}")
    violations = rules.scan_history(tmp_path)
    assert violations
    assert any("email" in v for v in violations)


def test_scan_history_flags_an_unexpected_commit_author_identity(tmp_path):
    _init_history_repo(tmp_path)
    _commit_files(tmp_path, {"a.txt": "hi\n"}, "init")
    violations = rules.scan_history(tmp_path, public_name="Public Name", public_email="public@example.com")
    assert any("commit author field" in v for v in violations)


def test_scan_history_accepts_the_matching_public_identity(tmp_path):
    _init_history_repo(tmp_path)
    _commit_files(tmp_path, {"a.txt": "hi\n"}, "init")
    violations = rules.scan_history(tmp_path, public_name="Test User", public_email="test@example.com")
    assert not any("commit author field" in v for v in violations)


def test_scan_history_returns_a_message_when_git_is_unavailable(tmp_path, monkeypatch):
    def _no_git(*args, **kwargs):
        raise FileNotFoundError("git not installed")

    monkeypatch.setattr(rules.subprocess, "run", _no_git)
    violations = rules.scan_history(tmp_path)
    assert violations
    assert "git" in violations[0]


# --- individual rules: a planted violation in tmp_path must be caught -----
#
# Every planted violation below is assembled from fragments at run time
# (never a contiguous literal) so this test file's own tracked source never
# contains a string that test_repository_tree_has_no_private_data would
# itself flag; only the value built in memory when the test runs is a real
# violation, and only that value is what the rule function sees.


def _plant(tmp_path: Path, name: str, *parts: str) -> str:
    content = "".join(parts) + "\n"
    target = tmp_path / name
    target.write_text(content, encoding="utf-8")
    return target.read_text(encoding="utf-8")


def test_rule_uuid_catches_a_planted_real_uuid(tmp_path):
    text = _plant(tmp_path, "uuid.txt", "session ", "11111111-2222-4000-8000-", "333333333333", " started")
    assert rules.rule_uuid("planted", text)


def test_rule_uuid_allows_the_fake_uuid(tmp_path):
    text = _plant(tmp_path, "uuid.txt", "session ", rules.FAKE_UUID, " started")
    assert rules.rule_uuid("planted", text) == []


def test_rule_users_path_catches_a_planted_real_account(tmp_path):
    text = _plant(tmp_path, "path.txt", "reading /Users/", "realaccountname", "/project/notes.md")
    assert rules.rule_users_path("planted", text)


def test_rule_users_path_exempt_flag_suppresses_the_hit(tmp_path):
    text = _plant(tmp_path, "path.txt", "reading /Users/", "realaccountname", "/project/notes.md")
    assert rules.rule_users_path("planted", text, exempt=True) == []


def test_rule_users_path_catches_a_unicode_account_name(tmp_path):
    text = _plant(tmp_path, "path.txt", "reading /Users/", "测试", "/private/notes.md")
    assert rules.rule_users_path("planted", text)


def test_rule_home_path_catches_a_planted_non_user_account(tmp_path):
    text = _plant(tmp_path, "path.txt", "reading /home/", "realaccountname", "/project/notes.md")
    assert rules.rule_home_path("planted", text)


def test_rule_home_path_catches_a_unicode_account_name(tmp_path):
    text = _plant(tmp_path, "path.txt", "reading /home/", "测试", "/private/notes.md")
    assert rules.rule_home_path("planted", text)


def test_rule_home_path_allows_home_user(tmp_path):
    text = _plant(tmp_path, "path.txt", "reading /home/user/project/notes.md")
    assert rules.rule_home_path("planted", text) == []


def test_rule_email_catches_a_planted_real_looking_email(tmp_path):
    text = _plant(tmp_path, "email.txt", "contact real.person", "@", "realcompany.test")
    assert rules.rule_email("planted", text)


def test_rule_email_allows_example_com_and_noreply(tmp_path):
    text = _plant(
        tmp_path, "email.txt",
        "contact test@example.com or 123+bot@users.noreply.github.com",
    )
    assert rules.rule_email("planted", text) == []


def test_rule_email_does_not_exempt_noreply_as_a_local_part_substring(tmp_path):
    text = _plant(tmp_path, "email.txt", "contact person-noreply", "@", "privatecompany.test")
    assert rules.rule_email("planted", text)


def test_rule_uuid_catches_a_uuid_immediately_surrounded_by_underscores(tmp_path):
    text = _plant(tmp_path, "uuid.txt", "_", "11111111-2222-4000-8000-", "333333333333", "_")
    assert rules.rule_uuid("planted", text)


def test_rule_wf_id_catches_an_id_immediately_followed_by_an_underscore(tmp_path):
    text = _plant(tmp_path, "run.txt", "see run ", "wf_deadbeef", "-123", "_notes", " for detail")
    assert rules.rule_wf_id("planted", text, synthetic_ids=frozenset())


def test_rule_wf_id_catches_an_id_missing_from_the_allowlist(tmp_path):
    text = _plant(tmp_path, "run.txt", "see run ", "wf_deadbeef", "-123", " for detail")
    assert rules.rule_wf_id("planted", text, synthetic_ids=frozenset())


def test_rule_wf_id_allows_a_listed_id(tmp_path):
    text = _plant(tmp_path, "run.txt", "see run ", "wf_deadbeef", "-123", " for detail")
    listed_id = "wf_deadbeef" + "-123"
    assert rules.rule_wf_id("planted", text, synthetic_ids=frozenset({listed_id})) == []


def test_rule_wf_id_catches_the_hash_fallback_form(tmp_path):
    """M8 review D8: reader._valid_workflow_run_id replaces a non-conforming
    run directory name with 'wf_' + sha256(name)[:8] + '-x' -- a generated
    id, not something anyone typed, but still a wf_ id the allowlist rule
    must recognize, or a generated one could slip through unreviewed."""
    text = _plant(tmp_path, "run.txt", "see run ", "wf_deadbeef", "-x", " for detail")
    assert rules.rule_wf_id("planted", text, synthetic_ids=frozenset())


def test_rule_wf_id_allows_a_listed_hash_fallback_id(tmp_path):
    text = _plant(tmp_path, "run.txt", "see run ", "wf_deadbeef", "-x", " for detail")
    listed_id = "wf_deadbeef" + "-x"
    assert rules.rule_wf_id("planted", text, synthetic_ids=frozenset({listed_id})) == []


def test_rule_line_length_catches_a_planted_overlong_line(tmp_path):
    text = _plant(tmp_path, "long.txt", "x" * 2001 + "\n")
    assert rules.rule_line_length("planted", text)


def test_rule_line_length_allows_a_line_at_the_limit(tmp_path):
    text = _plant(tmp_path, "long.txt", "x" * 2000 + "\n")
    assert rules.rule_line_length("planted", text) == []


def test_rule_denylist_catches_a_planted_term_case_insensitively(tmp_path):
    text = _plant(tmp_path, "denied.txt", "The Secret Project shipped today.\n")
    assert rules.rule_denylist("planted", text, ("secret project",))


def test_rule_denylist_is_a_noop_with_no_terms(tmp_path):
    text = _plant(tmp_path, "denied.txt", "anything at all\n")
    assert rules.rule_denylist("planted", text, ()) == []


# --- violation messages must never repeat the secret they caught ---------


def test_violation_messages_never_contain_the_matched_secret(tmp_path):
    uuid_value = "11111111-2222-4000-8000-" + "333333333333"
    users_path_value = "/Users/" + "realaccountname"
    home_path_value = "/home/" + "realaccountname"
    email_value = "real.person" + "@" + "realcompany.test"
    wf_value = "wf_deadbeef" + "-123"
    denylist_term = "topsecretcodename"
    text = _plant(
        tmp_path, "everything.txt",
        "uuid ", uuid_value, "\n",
        "users path ", users_path_value, "\n",
        "home path ", home_path_value, "\n",
        "email ", email_value, "\n",
        "run ", wf_value, "\n",
        denylist_term, " shipped\n",
    )
    violations = rules.check_text(
        "planted", text, synthetic_ids=frozenset(), denylist_terms=(denylist_term,),
    )
    # Sanity: the plant actually triggers every rule under test, so an empty
    # result would mean this test caught nothing rather than proving safety.
    assert len(violations) >= 6
    joined = "\n".join(violations)
    for secret in (uuid_value, users_path_value, home_path_value, email_value, wf_value, denylist_term):
        assert secret not in joined
