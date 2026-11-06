"""scripts/check-deny-list.py: a publish stops when what goes out names something it must not (DESIGN.md
5.4). The public repository cannot spell the real entries, so every entry here is synthetic: the
script is run for real, as a subprocess, over real git repositories and real archives under tmp_path.
"""

from __future__ import annotations

import gzip
import io
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check-deny-list.py"
TERM = "zz-deny-term"
_IDENTITY = ("-c", "user.name=Example", "-c", "user.email=test@example.com",
             "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false")


def _git(repo: Path, *args: str, **env: str) -> None:
    subprocess.run(["git", "-C", str(repo), *_IDENTITY, *args], check=True, capture_output=True,
                   env={**os.environ, **env})


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("nothing to see\n", encoding="utf-8")
    _commit(repo)
    return repo


def _commit(repo: Path, message: str = "a commit") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)


def _list(tmp_path: Path, text: str = f"# a comment\n\n{TERM}\nre:ZZQ-[0-9]{{4}}\n") -> Path:
    path = tmp_path / "terms"
    path.write_text(text, encoding="utf-8")
    return path


def _run(listed: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env={**os.environ, "PUBLIC_DENY_LIST": str(listed)}, timeout=60, check=False)


def _built(folder: Path, *, wheel: dict[str, str] | None = None,
           sdist: dict[str, str] | None = None) -> Path:
    folder.mkdir(exist_ok=True)
    with zipfile.ZipFile(folder / "pkg-1.0-py3-none-any.whl", "w") as whl:
        for name, text in (wheel or {"pkg/__init__.py": "x = 1\n"}).items():
            whl.writestr(name, text)
    with tarfile.open(folder / "pkg-1.0.tar.gz", "w:gz") as tar:
        for name, text in (sdist or {"pkg-1.0/README.md": "readme\n"}).items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return folder


# --- no list, no pass ---------------------------------------------------------------------------


def test_a_missing_list_fails_and_says_nothing_was_proved(tmp_path):
    repo = _repo(tmp_path)
    done = _run(tmp_path / "absent", "--root", str(repo), "--tree")
    assert done.returncode == 2 and "nothing was proved" in done.stderr, done
    assert "no hits" not in done.stdout


@pytest.mark.parametrize("text", ["", "\n\n", "# only a comment\n"])
def test_a_list_with_no_entries_fails(tmp_path, text):
    repo = _repo(tmp_path)
    done = _run(_list(tmp_path, text), "--root", str(repo), "--tree")
    assert done.returncode == 2 and "no entries" in done.stderr, done


def test_a_list_that_is_a_folder_fails(tmp_path):
    repo = _repo(tmp_path)
    folder = tmp_path / "terms"
    folder.mkdir()
    assert _run(folder, "--root", str(repo), "--tree").returncode == 2


def test_a_bad_pattern_fails_without_repeating_it(tmp_path):
    repo = _repo(tmp_path)
    done = _run(_list(tmp_path, "ok\nre:zz-secret-(\n"), "--root", str(repo), "--tree")
    assert done.returncode == 2 and "line 2" in done.stderr and "zz-secret" not in done.stderr, done


def test_with_no_flags_every_part_must_be_there(tmp_path):
    repo = _repo(tmp_path)
    done = _run(_list(tmp_path), "--root", str(repo))
    assert done.returncode == 2 and "wheel" in done.stderr, done


def test_a_root_that_is_no_repository_fails(tmp_path):
    (tmp_path / "plain").mkdir()
    assert _run(_list(tmp_path), "--root", str(tmp_path / "plain"), "--tree").returncode == 2
    assert _run(_list(tmp_path), "--root", str(tmp_path / "plain"), "--history").returncode == 2


def test_a_repository_with_no_commit_is_not_a_pass(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    assert _run(_list(tmp_path), "--root", str(empty), "--tree").returncode == 2
    assert _run(_list(tmp_path), "--root", str(empty), "--history").returncode == 2


# --- a clean search says what it searched ----------------------------------------------------------


def test_clean_everywhere_passes_with_counts(tmp_path):
    repo = _repo(tmp_path)
    dist = _built(repo / "dist")
    done = _run(_list(tmp_path), "--root", str(repo), "--tree", "--history", "--artifacts", str(dist))
    assert done.returncode == 0, done
    assert "no hits (2 entries; tree 1 files, history 3 objects, artifacts 2 files, 2 members)" in done.stdout


def test_no_flags_means_all_three(tmp_path):
    repo = _repo(tmp_path)
    _built(repo / "dist")
    done = _run(_list(tmp_path), "--root", str(repo))
    assert done.returncode == 0 and "tree" in done.stdout and "history" in done.stdout \
        and "artifacts" in done.stdout, done


# --- the tree -------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [TERM, TERM.upper(), f"in the middle {TERM} of a line", "ZZQ-1234"])
def test_a_hit_in_a_tracked_file(tmp_path, text):
    repo = _repo(tmp_path)
    (repo / "notes.md").write_text(f"line one\nline two\n{text}\n", encoding="utf-8")
    _git(repo, "add", "notes.md")
    done = _run(_list(tmp_path), "--root", str(repo), "--tree")
    assert done.returncode == 1, done
    assert "tree: notes.md:3: deny-list entry" in done.stdout, done


def test_it_never_prints_the_entry_or_the_line(tmp_path):
    repo = _repo(tmp_path)
    (repo / "notes.md").write_text(f"private neighbour {TERM} words\n", encoding="utf-8")
    _git(repo, "add", "notes.md")
    done = _run(_list(tmp_path), "--root", str(repo), "--tree")
    assert done.returncode == 1
    for words in (TERM, "neighbour", "private"):
        assert words not in done.stdout + done.stderr, done


def test_a_hit_in_a_tracked_file_name_withholds_the_name(tmp_path):
    repo = _repo(tmp_path)
    (repo / f"{TERM}.md").write_text("fine\n", encoding="utf-8")
    _git(repo, "add", "-A")
    done = _run(_list(tmp_path), "--root", str(repo), "--tree")
    assert done.returncode == 1 and TERM not in done.stdout + done.stderr, done
    assert "name withheld" in done.stdout, done


def test_a_tracked_symlink_is_read_as_its_target_text(tmp_path):
    repo = _repo(tmp_path)
    (repo / "link").symlink_to(f"/tmp/{TERM}")
    _git(repo, "add", "-A")
    assert _run(_list(tmp_path), "--root", str(repo), "--tree").returncode == 1


def test_an_untracked_file_is_not_going_out(tmp_path):
    repo = _repo(tmp_path)
    (repo / "scratch.md").write_text(TERM, encoding="utf-8")
    assert _run(_list(tmp_path), "--root", str(repo), "--tree").returncode == 0


# --- the history ----------------------------------------------------------------------------------


def test_a_file_deleted_long_ago_is_still_found(tmp_path):
    repo = _repo(tmp_path)
    (repo / "old.md").write_text(f"{TERM}\n", encoding="utf-8")
    _commit(repo)
    (repo / "old.md").unlink()
    _commit(repo)
    assert _run(_list(tmp_path), "--root", str(repo), "--tree").returncode == 0
    done = _run(_list(tmp_path), "--root", str(repo), "--history")
    assert done.returncode == 1 and "(blob) old.md:1: deny-list entry 1" in done.stdout, done


def test_a_binary_blob_is_searched(tmp_path):
    repo = _repo(tmp_path)
    (repo / "image.bin").write_bytes(b"\x00\x01" + TERM.encode() + b"\xff\xfe")
    _commit(repo)
    assert _run(_list(tmp_path), "--root", str(repo), "--history").returncode == 1


def test_a_commit_message_is_searched(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, f"fix it for {TERM}")
    done = _run(_list(tmp_path), "--root", str(repo), "--history")
    assert done.returncode == 1 and "(commit)" in done.stdout, done


def test_an_author_is_searched(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "fine", f"--author=Someone <a@{TERM}.invalid>")
    assert _run(_list(tmp_path), "--root", str(repo), "--history").returncode == 1


def test_a_tag_message_is_searched(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "tag", "-a", "v1", "-m", f"release for {TERM}")
    done = _run(_list(tmp_path), "--root", str(repo), "--history")
    assert done.returncode == 1 and "(tag)" in done.stdout, done


def test_a_branch_name_is_searched_and_withheld(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "branch", TERM)
    done = _run(_list(tmp_path), "--root", str(repo), "--history")
    assert done.returncode == 1 and TERM not in done.stdout + done.stderr, done


def test_an_old_file_name_is_found_and_withheld(tmp_path):
    repo = _repo(tmp_path)
    (repo / f"{TERM}.md").write_text("fine\n", encoding="utf-8")
    _commit(repo)
    (repo / f"{TERM}.md").unlink()
    _commit(repo)
    done = _run(_list(tmp_path), "--root", str(repo), "--history")
    assert done.returncode == 1 and TERM not in done.stdout + done.stderr, done


def test_an_object_no_ref_reaches_is_not_going_out(tmp_path):
    repo = _repo(tmp_path)
    (repo / "dropped.md").write_text(TERM, encoding="utf-8")
    _git(repo, "add", "dropped.md")  # staged, then dropped: the blob stays, unreachable
    _git(repo, "rm", "-q", "--cached", "dropped.md")
    (repo / "dropped.md").unlink()
    assert _run(_list(tmp_path), "--root", str(repo), "--history").returncode == 0


# --- the built wheel and sdist ---------------------------------------------------------------------


def test_a_hit_inside_the_wheel(tmp_path):
    repo = _repo(tmp_path)
    dist = _built(tmp_path / "dist", wheel={"pkg/a.py": f"x = '{TERM}'\n"})
    done = _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist))
    assert done.returncode == 1 and "pkg-1.0-py3-none-any.whl: pkg/a.py:1" in done.stdout, done


def test_a_hit_inside_the_sdist(tmp_path):
    repo = _repo(tmp_path)
    dist = _built(tmp_path / "dist", sdist={"pkg-1.0/a.txt": f"one\n{TERM}\n"})
    done = _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist))
    assert done.returncode == 1 and "pkg-1.0.tar.gz: pkg-1.0/a.txt:2" in done.stdout, done


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_a_member_name_is_searched_and_withheld(tmp_path, kind):
    repo = _repo(tmp_path)
    dist = _built(tmp_path / "dist", **{kind: {f"pkg/{TERM}.py": "fine\n"}})
    done = _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist))
    assert done.returncode == 1 and TERM not in done.stdout + done.stderr, done


@pytest.mark.parametrize("missing", ["pkg-1.0-py3-none-any.whl", "pkg-1.0.tar.gz"])
def test_one_artifact_missing_is_not_a_pass(tmp_path, missing):
    repo = _repo(tmp_path)
    dist = _built(tmp_path / "dist")
    (dist / missing).unlink()
    done = _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist))
    assert done.returncode == 2 and "uv build" in done.stderr, done


def test_a_broken_archive_or_no_folder_is_not_a_pass(tmp_path):
    repo = _repo(tmp_path)
    assert _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(tmp_path / "none")).returncode == 2
    dist = _built(tmp_path / "dist")
    (dist / "pkg-1.0-py3-none-any.whl").write_bytes(b"not a zip")
    assert _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist)).returncode == 2
    dist = _built(tmp_path / "dist2")
    (dist / "pkg-1.0.tar.gz").write_bytes(b"not a tar")
    assert _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist)).returncode == 2


# --- check-privacy.sh runs it ----------------------------------------------------------------------


def _privacy(tmp_path: Path, *, ci: bool, listed: Path) -> subprocess.CompletedProcess:
    """The real script on this repository's tree, with no uv on PATH so it stays fast."""
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    if not (tools / "python3").exists():
        (tools / "python3").symlink_to(sys.executable)
    env = {key: value for key, value in os.environ.items() if key not in ("CI", "VIRTUAL_ENV")}
    env.update(PATH=f"{tools}{os.pathsep}/usr/bin{os.pathsep}/bin", PUBLIC_DENY_LIST=str(listed))
    if ci:
        env["CI"] = "true"
    return subprocess.run([str(ROOT / "scripts" / "check-privacy.sh"), "--tree"], capture_output=True,
                          text=True, env=env, timeout=120, check=False)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_check_privacy_fails_with_no_list(tmp_path):
    done = _privacy(tmp_path, ci=False, listed=tmp_path / "absent")
    assert done.returncode != 0 and "nothing was proved" in done.stderr, done


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_check_privacy_on_a_ci_runner_says_it_skipped(tmp_path):
    done = _privacy(tmp_path, ci=True, listed=tmp_path / "absent")
    assert done.returncode == 0 and "deny list: skipped" in done.stdout, done


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_check_privacy_fails_on_a_hit(tmp_path):
    # an entry this repository's tree must hold: the script's own first comment
    done = _privacy(tmp_path, ci=False, listed=_list(tmp_path, "re:Repository privacy audit\n"))
    assert done.returncode != 0 and "deny-list entry 1" in done.stdout, done


# --- further cases --------------------------------------------------------------------------------


def test_a_replacement_ref_cannot_hide_what_a_push_carries(tmp_path):
    repo = _repo(tmp_path)
    (repo / "old.md").write_text(f"{TERM}\n", encoding="utf-8")
    _commit(repo)
    (repo / "clean.md").write_text("fine\n", encoding="utf-8")
    _git(repo, "add", "clean.md")
    def _sha(spec: str) -> str:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", spec], capture_output=True,
                              text=True, check=True).stdout.strip()

    _git(repo, "replace", _sha("HEAD:old.md"), _sha(":clean.md"))
    assert _run(_list(tmp_path), "--root", str(repo), "--history").returncode == 1


def test_a_staged_file_gone_from_disk_is_still_searched(tmp_path):
    repo = _repo(tmp_path)
    (repo / "staged.md").write_text(f"{TERM}\n", encoding="utf-8")
    _git(repo, "add", "staged.md")
    (repo / "staged.md").unlink()
    done = _run(_list(tmp_path), "--root", str(repo), "--tree")
    assert done.returncode == 1 and "tree: staged.md:1: deny-list entry 1" in done.stdout, done


def test_what_is_staged_counts_even_when_the_file_on_disk_is_clean(tmp_path):
    repo = _repo(tmp_path)
    (repo / "staged.md").write_text(f"{TERM}\n", encoding="utf-8")
    _git(repo, "add", "staged.md")
    (repo / "staged.md").write_text("fine\n", encoding="utf-8")
    done = _run(_list(tmp_path), "--root", str(repo), "--tree")
    assert done.returncode == 1 and "(staged)" in done.stdout, done


def test_what_is_on_disk_counts_even_when_the_staged_file_is_clean(tmp_path):
    repo = _repo(tmp_path)
    (repo / "README.md").write_text(f"{TERM}\n", encoding="utf-8")
    done = _run(_list(tmp_path), "--root", str(repo), "--tree")
    assert done.returncode == 1 and "tree: README.md:1" in done.stdout, done


def test_a_clean_file_is_reported_once(tmp_path):
    repo = _repo(tmp_path)
    (repo / "notes.md").write_text(f"{TERM}\n", encoding="utf-8")
    _git(repo, "add", "notes.md")
    done = _run(_list(tmp_path), "--root", str(repo), "--tree")
    assert done.stdout.count("deny-list entry") == 1, done


@pytest.mark.parametrize("field", ["uname", "gname", "pax"])
def test_sdist_metadata_is_searched(tmp_path, field):
    repo = _repo(tmp_path)
    dist = _built(tmp_path / "dist")
    with tarfile.open(dist / "pkg-1.0.tar.gz", "w:gz", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("pkg-1.0/README.md")
        if field == "pax":
            info.pax_headers = {"comment": TERM}
        else:
            setattr(info, field, TERM)
        tar.addfile(info, io.BytesIO(b""))
    done = _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist))
    assert done.returncode == 1 and TERM not in done.stdout + done.stderr, done


def test_sdist_global_pax_headers_are_searched(tmp_path):
    repo = _repo(tmp_path)
    dist = _built(tmp_path / "dist")
    with tarfile.open(dist / "pkg-1.0.tar.gz", "w:gz", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": TERM}) as tar:
        tar.addfile(tarfile.TarInfo("pkg-1.0/README.md"), io.BytesIO(b""))
    assert _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist)).returncode == 1


@pytest.mark.parametrize("field", ["archive comment", "member comment", "extra"])
def test_wheel_metadata_is_searched(tmp_path, field):
    repo = _repo(tmp_path)
    dist = _built(tmp_path / "dist")
    with zipfile.ZipFile(dist / "pkg-1.0-py3-none-any.whl", "w") as whl:
        info = zipfile.ZipInfo("pkg/a.py")
        if field == "archive comment":
            whl.comment = TERM.encode()
        elif field == "member comment":
            info.comment = TERM.encode()
        else:
            data = TERM.encode()
            info.extra = (0x7a7a).to_bytes(2, "little") + len(data).to_bytes(2, "little") + data
        whl.writestr(info, "x = 1\n")
    done = _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist))
    assert done.returncode == 1 and TERM not in done.stdout + done.stderr, done


def test_an_archive_whose_own_name_holds_a_hit_is_not_repeated(tmp_path):
    repo = _repo(tmp_path)
    dist = _built(tmp_path / "dist", wheel={"pkg/a.py": f"x = '{TERM}'\n"})
    (dist / "pkg-1.0-py3-none-any.whl").rename(dist / f"{TERM}-1.0-py3-none-any.whl")
    done = _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist))
    assert done.returncode == 1 and TERM not in done.stdout + done.stderr, done


def test_an_encrypted_member_is_not_a_pass_and_not_a_traceback(tmp_path):
    repo = _repo(tmp_path)
    dist = _built(tmp_path / "dist", wheel={f"pkg/{TERM}.py": "fine\n"})
    path = dist / "pkg-1.0-py3-none-any.whl"
    raw = bytearray(path.read_bytes())
    central = raw.index(b"PK\x01\x02")
    raw[central + 8] |= 0x01  # the "encrypted" flag, which zipfile refuses to read past
    raw[raw.index(b"PK\x03\x04") + 6] |= 0x01
    path.write_bytes(bytes(raw))
    done = _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist))
    assert done.returncode in (1, 2) and TERM not in done.stdout + done.stderr, done
    assert "Traceback" not in done.stderr, done


def test_nothing_unexpected_reaches_the_screen(tmp_path):
    """Whatever fails in a search can hold a name, so only its type is printed."""
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("fine\n", encoding="utf-8")
    bad = tmp_path / "terms"
    bad.write_bytes(b"\xff\xfe not text\n")
    done = _run(bad, "--root", str(repo), "--tree")
    assert done.returncode == 2 and "Traceback" not in done.stderr, done


# --- further cases --------------------------------------------------------------------------------


def test_a_gzip_header_is_searched(tmp_path):
    repo = _repo(tmp_path)
    dist = _built(tmp_path / "dist")
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode="w") as tar:
        tar.addfile(tarfile.TarInfo("pkg-1.0/README.md"), io.BytesIO(b""))
    with gzip.GzipFile(filename=f"{TERM}.tar", mode="wb", fileobj=(dist / "pkg-1.0.tar.gz").open("wb")) as out:
        out.write(plain.getvalue())
    done = _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist))
    assert done.returncode == 1 and TERM not in done.stdout + done.stderr, done


def test_data_after_a_tars_end_is_searched(tmp_path):
    repo = _repo(tmp_path)
    dist = _built(tmp_path / "dist")
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode="w") as tar:
        tar.addfile(tarfile.TarInfo("pkg-1.0/README.md"), io.BytesIO(b""))
    (dist / "pkg-1.0.tar.gz").write_bytes(gzip.compress(plain.getvalue() + TERM.encode()))
    assert _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist)).returncode == 1


def test_an_extra_field_only_in_a_zip_local_header_is_searched(tmp_path):
    repo = _repo(tmp_path)
    dist = _built(tmp_path / "dist")
    path = dist / "pkg-1.0-py3-none-any.whl"
    field = (0x7a7a).to_bytes(2, "little") + len(TERM).to_bytes(2, "little") + TERM.encode()
    with zipfile.ZipFile(path, "w") as whl:
        info = zipfile.ZipInfo("pkg/a.py")
        info.extra = field
        whl.writestr(info, "x = 1\n")
    raw = bytearray(path.read_bytes())
    central = raw.index(b"PK\x01\x02")
    extra_at = raw.index(field, central)
    raw[extra_at:extra_at + len(field)] = b"\x7a\x7a" + len(TERM).to_bytes(2, "little") + b"x" * len(TERM)
    path.write_bytes(bytes(raw))
    done = _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist))
    assert done.returncode == 1 and TERM not in done.stdout + done.stderr, done


def test_a_staged_submodule_name_is_searched(tmp_path):
    repo = _repo(tmp_path)
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                            check=True).stdout.strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},{TERM}")
    done = _run(_list(tmp_path), "--root", str(repo), "--tree")
    assert done.returncode == 1 and TERM not in done.stdout + done.stderr, done


def test_a_list_it_cannot_use_does_not_print_where_it_is(tmp_path):
    repo = _repo(tmp_path)
    folder = tmp_path / TERM
    folder.mkdir()
    for listed, content in ((folder / "absent", None), (folder / "bad", b"\xff\xfe"),
                            (folder / "empty", b"# nothing\n")):
        if content is not None:
            listed.write_bytes(content)
        done = _run(listed, "--root", str(repo), "--tree")
        assert done.returncode == 2 and TERM not in done.stdout + done.stderr, done


def test_a_public_ci_like_run_ends_in_this_check_outside_the_runner():
    script = (ROOT / "scripts" / "ci-like.sh").read_text(encoding="utf-8")
    build = script.index('step "privacy"')
    step = script.index("check-deny-list.py", build)
    line = script[script.rindex("\n", 0, step):script.index("\n", step)]
    assert "--tree --history --artifacts" in line and "as_runner" not in line, line
    assert script.index("return 1", step) < script.index("plugin validation", step)


# --- a literal entry is a word, not any run of letters (other public repos, 2026-11-05) ------------


WORDS = "runzq\nxzroot\n"


def _tree_hits(tmp_path: Path, text: str, terms: str = WORDS) -> int:
    repo = _repo(tmp_path)
    (repo / "notes.md").write_bytes(text.encode("utf-8"))
    _git(repo, "add", "notes.md")
    done = _run(_list(tmp_path, terms), "--root", str(repo), "--tree")
    assert done.returncode in (0, 1), done
    return done.returncode


@pytest.mark.parametrize(
    "text",
    ["runzq", "RUNZQ", "Runzq", "the runzq tool", "runzq-worker", "runzq_worker", "runzq2", "2runzq",
     "/opt/runzq/bin", '"runzq"', "runzq.toml", "RunzqClient", "myRunzq", "myXzroot", "XzrootPath",
     "é-runzq", "xzroot", "runzqClient"],
)
def test_a_word_or_a_part_of_a_name_is_a_hit(tmp_path, text):
    assert _tree_hits(tmp_path, text) == 1, text


@pytest.mark.parametrize(
    "text",
    ["runzQueue", "runzQuestion", "runzqueue", "prerunzq", "mxzRoot", "mxzroot", "xzrootless",
     "MXZROOT", "RUNZQUEUE"],
)
def test_the_same_letters_inside_another_word_are_no_hit(tmp_path, text):
    assert _tree_hits(tmp_path, text) == 0, text


def test_a_pattern_is_used_as_written_so_it_can_match_inside_a_word(tmp_path):
    assert _tree_hits(tmp_path, "runzQueue", "re:runzq\n") == 1


def test_a_hit_in_a_binary_file_is_reported_as_any_other(tmp_path):
    repo = _repo(tmp_path)
    (repo / "image.bin").write_bytes(b"\x00\x01runzq\xff\xfe")
    _git(repo, "add", "image.bin")
    done = _run(_list(tmp_path, WORDS), "--root", str(repo), "--tree")
    assert done.returncode == 1 and "tree: image.bin:1: deny-list entry 1" in done.stdout, done


def test_the_same_letters_inside_a_word_in_a_binary_file_are_no_hit(tmp_path):
    repo = _repo(tmp_path)
    (repo / "image.bin").write_bytes(b"\x00\x01prerunzqueue\xff\xfe")
    _git(repo, "add", "image.bin")
    assert _run(_list(tmp_path, WORDS), "--root", str(repo), "--tree").returncode == 0


def test_a_file_name_and_a_commit_message_read_words_the_same_way(tmp_path):
    repo = _repo(tmp_path)
    (repo / "runzQueue.md").write_text("fine\n", encoding="utf-8")
    _commit(repo, "the runzQueue work")
    assert _run(_list(tmp_path, WORDS), "--root", str(repo), "--tree", "--history").returncode == 0
    _commit(repo, "for the runzq tool")
    assert _run(_list(tmp_path, WORDS), "--root", str(repo), "--history").returncode == 1


# --- the release review: all an archive carries, whatever its size -------------------------------


def test_bytes_between_a_wheels_members_and_its_directory_are_searched(tmp_path):
    repo = _repo(tmp_path)
    dist = _built(tmp_path / "dist")
    path = dist / "pkg-1.0-py3-none-any.whl"
    raw = path.read_bytes()
    at = raw.index(b"PK\x01\x02")
    extra = b"PK\x06\x08" + (len(TERM)).to_bytes(4, "little") + TERM.encode()
    data = bytearray(raw[:at] + extra + raw[at:])
    end = data.index(b"PK\x05\x06")
    data[end + 16:end + 20] = (at + len(extra)).to_bytes(4, "little")  # the directory moved on
    path.write_bytes(bytes(data))
    with zipfile.ZipFile(path) as whl:
        assert whl.read("pkg/__init__.py") == b"x = 1\n", "still a readable wheel"
    done = _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist))
    assert done.returncode == 1 and TERM not in done.stdout + done.stderr, done


def test_a_long_gzip_comment_is_searched(tmp_path):
    repo = _repo(tmp_path)
    dist = _built(tmp_path / "dist")
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode="w") as tar:
        tar.addfile(tarfile.TarInfo("pkg-1.0/README.md"), io.BytesIO(b""))
    body = gzip.compress(plain.getvalue())
    comment = b"x " * 2500 + TERM.encode() + b"\x00"
    # flags: FCOMMENT, in a header gzip.compress wrote with none
    (dist / "pkg-1.0.tar.gz").write_bytes(body[:3] + b"\x10" + body[4:10] + comment + body[10:])
    with tarfile.open(dist / "pkg-1.0.tar.gz", "r:gz") as tar:
        assert tar.getnames() == ["pkg-1.0/README.md"], "still a readable sdist"
    done = _run(_list(tmp_path), "--root", str(repo), "--artifacts", str(dist))
    assert done.returncode == 1 and TERM not in done.stdout + done.stderr, done
