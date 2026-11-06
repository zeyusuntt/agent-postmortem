#!/usr/bin/env python3
"""Search what is about to go public against a deny list kept outside the repository (DESIGN 5.4).

A public repository cannot spell what it must never mention, so the list lives at
~/.config/public-deny-list/terms, or wherever $PUBLIC_DENY_LIST names. One entry a line: a literal,
matched as a word without regard to ASCII case, or `re:` and a regular expression over bytes, used
as written, which can also match inside a word. Blank lines and lines starting with # are skipped.

A literal is a word when no ASCII letter touches it, or where a name starts a new word, so an entry
`abc` hits `abc-tool`, `abc_tool`, `abc2`, `AbcTool` and `abcTool`, not `abCde`, `abcde` or `xabc`.

Prints where a hit is and the entry's number, never the entry or the text around it, and no name
that itself holds a hit. Exit 0: searched, no hit. Exit 1: a hit. Exit 2: it could not search, as
with a list that is missing or empty, or nothing to search: that is never a pass.

Usage: scripts/check-deny-list.py [--tree] [--history] [--artifacts DIR] [--root DIR]
No flags means all three, with DIR the root's dist/. Standard library only.
"""

from __future__ import annotations

import argparse
import gzip
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

DEFAULT_LIST = "~/.config/public-deny-list/terms"
_GIT_OBJECT_TYPES = (b"blob", b"commit", b"tag", b"tree")
# Case matters here though not in the entry: a lower-case letter then an upper-case one is where a
# new word starts in a name. The entry itself is matched under (?i:...) between these.
_WORD_START = rb"(?:(?<![A-Za-z])|(?<=[a-z])(?=[A-Z]))"
_WORD_END = rb"(?:(?![A-Za-z])|(?<=[a-z])(?=[A-Z]))"


class CannotSearch(Exception):
    """Anything that means the search did not happen, which must not read as a pass."""


def load_terms(path: Path) -> list[re.Pattern[bytes]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CannotSearch(f"cannot read the deny list: {type(error).__name__}. It lives outside "
                           f"the repository, at {DEFAULT_LIST} unless $PUBLIC_DENY_LIST names "
                           "another place; a check with no list has checked nothing") from None
    terms = []
    for number, raw in enumerate(text.splitlines(), 1):
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        try:
            if entry.startswith("re:"):
                terms.append(re.compile(entry[3:].encode("utf-8"), re.IGNORECASE))
            else:
                word = b"(?i:" + re.escape(entry).encode("utf-8") + b")"
                terms.append(re.compile(_WORD_START + word + _WORD_END))
        except re.error as error:
            raise CannotSearch(f"line {number} of the deny list is not a valid pattern") from error
    if not terms:
        raise CannotSearch("the deny list has no entries")
    return terms


class Search:
    def __init__(self, terms: list[re.Pattern[bytes]]) -> None:
        self.terms = terms
        self.hits: list[str] = []

    def data(self, where: str, data: bytes) -> None:
        for number, term in enumerate(self.terms, 1):
            first = term.search(data)
            if first is None:
                continue
            line = data.count(b"\n", 0, first.start()) + 1
            self.hits.append(f"{where}:{line}: deny-list entry {number}")

    def clean_name(self, name: str, what: str) -> str:
        """The name to print: one that holds a hit is reported by itself and not repeated."""
        raw = os.fsencode(name)
        found = [number for number, term in enumerate(self.terms, 1) if term.search(raw)]
        for number in found:
            self.hits.append(f"{what} (name withheld): deny-list entry {number}")
        return "(name withheld)" if found else name


def _git_command(root: Path, *args: str) -> list[str]:
    # a replacement ref shows other content here and a push still carries the original
    return ["git", "-C", str(root), "--no-replace-objects", *args]


def _git(root: Path, *args: str) -> bytes:
    try:
        done = subprocess.run(_git_command(root, *args), capture_output=True, check=False)
    except OSError as error:
        raise CannotSearch(f"cannot run git: {type(error).__name__}") from error
    if done.returncode != 0:
        raise CannotSearch(f"`git {args[0]}` failed in the root ({done.returncode})")
    return done.stdout


def _objects(root: Path, shas: list[str]) -> Iterator[tuple[str, str, bytes]]:
    """(sha, type, content) of each object, every one or CannotSearch."""
    with tempfile.TemporaryFile() as wanted:
        wanted.write("\n".join(shas).encode("ascii") + b"\n")
        wanted.seek(0)
        with subprocess.Popen(_git_command(root, "cat-file", "--batch"), stdin=wanted,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as batch:
            read = 0
            while True:
                header = batch.stdout.readline()
                if not header:
                    break
                fields = header.split()
                if len(fields) != 3 or fields[1] not in _GIT_OBJECT_TYPES:
                    raise CannotSearch("`git cat-file` gave an answer this cannot read")
                content = batch.stdout.read(int(fields[2]) + 1)[:-1]
                yield fields[0].decode("ascii"), fields[1].decode("ascii"), content
                read += 1
        if batch.returncode != 0 or read != len(shas):
            raise CannotSearch(f"`git cat-file` read {read} of {len(shas)} objects")


def search_tree(root: Path, search: Search) -> str:
    """What a build reads and what a commit takes: the file on disk, and the staged one."""
    staged: list[tuple[str, str]] = []
    for row in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        fields, _tab, name = row.partition(b"\t")
        if not name:
            continue
        if fields.startswith(b"160000 "):  # a submodule's commit is not here, its name is
            search.clean_name(os.fsdecode(name), "tree: a tracked file's name")
        else:
            staged.append((os.fsdecode(name), fields.split()[1].decode("ascii")))
    if not staged:
        raise CannotSearch("the root has no tracked files")
    wanted = sorted({sha for _name, sha in staged})
    contents = {sha: content for sha, _kind, content in _objects(root, wanted)}
    for name, sha in staged:
        shown = search.clean_name(name, "tree: a tracked file's name")
        path = root / name
        try:
            disk = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        except FileNotFoundError:
            disk = contents[sha]  # gone from disk: the staged copy is what a commit takes
        except OSError as error:
            raise CannotSearch(f"cannot read a tracked file: {type(error).__name__}") from error
        search.data(f"tree: {shown}", disk)
        if disk != contents[sha]:
            search.data(f"tree: {shown} (staged)", contents[sha])
    return f"tree {len(staged)} files"


def search_history(root: Path, search: Search) -> str:
    """Every object reachable from any ref, which is what a push can carry: blobs of files long
    deleted, binary ones too, file names in trees, messages and authors in commits and tags."""
    refs = _git(root, "for-each-ref", "--format=%(refname)")
    for ref in refs.decode("utf-8", "surrogateescape").splitlines():
        search.clean_name(ref, "history: a ref's name")
    listing = _git(root, "rev-list", "--objects", "--all").decode("utf-8", "surrogateescape")
    paths: dict[str, str] = {}
    for line in listing.splitlines():
        sha, _space, path = line.partition(" ")
        paths.setdefault(sha, path)
    if not paths:
        raise CannotSearch("the root has no history")
    for sha, kind, content in _objects(root, list(paths)):
        named = paths.get(sha, "")
        shown = search.clean_name(named, f"history: {sha[:10]} ({kind})") if named else ""
        search.data(f"history: {sha[:10]} ({kind}){' ' + shown if shown else ''}", content)
    return f"history {len(paths)} objects"


def _search_zip_outside_payloads(raw: bytes, wheel: zipfile.ZipFile, label: str,
                                 search: Search) -> None:
    """Every byte but the compressed data, where a short entry could match by chance: each local
    header with its name and extra field, whatever lies between members, and the central directory
    on. The members' own contents are read as members."""
    at = 0
    for info in sorted(wheel.infolist(), key=lambda item: item.header_offset):
        start = info.header_offset
        lengths = int.from_bytes(raw[start + 26:start + 28], "little") \
            + int.from_bytes(raw[start + 28:start + 30], "little")
        search.data(f"{label}: its headers", raw[at:start])
        search.data(f"{label}: its headers", raw[start:start + 30 + lengths])
        at = start + 30 + lengths + info.compress_size
    search.data(f"{label}: its headers", raw[at:wheel.start_dir])
    search.data(f"{label}: its headers", raw[wheel.start_dir:])


def _gzip_header_end(raw: bytes) -> int:
    """Where a gzip file's header ends: its optional name and comment can be any length."""
    if raw[:2] != b"\x1f\x8b" or len(raw) < 10:
        raise ValueError("not gzip")
    flags, at = raw[3], 10
    if flags & 4:  # FEXTRA
        at += 2 + int.from_bytes(raw[at:at + 2], "little")
    for flag in (8, 16):  # FNAME, FCOMMENT: each up to a NUL
        if flags & flag:
            at = raw.index(b"\0", at) + 1
    return at + (2 if flags & 2 else 0)  # FHCRC


def _search_archive(archive: Path, label: str, search: Search) -> int:
    """Members and their names, which say where a hit is, and all else an archive carries."""
    members = 0
    raw = archive.read_bytes()
    if archive.name.endswith(".whl"):
        with zipfile.ZipFile(archive) as wheel:
            _search_zip_outside_payloads(raw, wheel, label, search)
            search.data(f"{label}: its comment", wheel.comment)
            for info in wheel.infolist():
                shown = search.clean_name(info.filename, f"{label}: a member's name")
                # apart as well: in the headers a field touches its neighbour, which is no word end
                search.data(f"{label}: {shown} (metadata)", info.comment + b"\n" + info.extra)
                search.data(f"{label}: {shown}", wheel.read(info))
                members += 1
        return members
    search.data(f"{label}: its gzip header", raw[:_gzip_header_end(raw)])
    search.data(f"{label}: its tar stream", gzip.decompress(raw))  # metadata, and past the end
    with tarfile.open(archive, "r:gz") as sdist:
        for info in sdist:
            shown = search.clean_name(info.name, f"{label}: a member's name")
            if info.isfile():
                handle = sdist.extractfile(info)
                search.data(f"{label}: {shown}", handle.read() if handle else b"")
            members += 1
    return members


def search_artifacts(folder: Path, search: Search) -> str:
    try:
        built = sorted(folder.iterdir())
    except FileNotFoundError as error:
        raise CannotSearch("there is no artifacts folder, which must hold a wheel and an sdist: "
                           "run `uv build` first") from error
    except OSError as error:
        raise CannotSearch(f"cannot list the artifacts folder: {type(error).__name__}") from error
    wheels = [path for path in built if path.name.endswith(".whl")]
    sdists = [path for path in built if path.name.endswith(".tar.gz")]
    if not wheels or not sdists:
        raise CannotSearch(f"the artifacts folder must hold a wheel and an sdist, and holds "
                           f"{len(wheels)} and {len(sdists)}: run `uv build` first")
    members = 0
    for archive in [*wheels, *sdists]:
        label = search.clean_name(archive.name, "artifacts: a file's name")
        try:
            members += _search_archive(archive, label, search)
        except Exception as error:  # zipfile's own errors can hold a member's name
            raise CannotSearch(f"cannot read a built artifact: {type(error).__name__}") from None
    return f"artifacts {len(wheels) + len(sdists)} files, {members} members"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search what goes public against a private deny list.")
    parser.add_argument("--tree", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--artifacts", metavar="DIR", default=None)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    everything = not (args.tree or args.history or args.artifacts)
    listed = Path(os.environ.get("PUBLIC_DENY_LIST") or DEFAULT_LIST).expanduser()
    root = args.root.expanduser()
    done = []
    try:
        search = Search(load_terms(listed))
        if args.tree or everything:
            done.append(search_tree(root, search))
        if args.history or everything:
            done.append(search_history(root, search))
        if args.artifacts or everything:
            done.append(search_artifacts(Path(args.artifacts).expanduser() if args.artifacts
                                         else root / "dist", search))
    except CannotSearch as problem:
        print(f"deny list: FAILED, nothing was proved: {problem}", file=sys.stderr)
        return 2
    except Exception as error:  # a traceback can hold a name, or an entry
        print(f"deny list: FAILED, nothing was proved: {type(error).__name__}", file=sys.stderr)
        return 2
    for hit in search.hits:
        print(hit)
    words = f"{len(search.terms)} entries; {', '.join(done)}"
    if search.hits:
        print(f"deny list: FAILED, {len(search.hits)} hits ({words})", file=sys.stderr)
        return 1
    print(f"deny list: no hits ({words})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
