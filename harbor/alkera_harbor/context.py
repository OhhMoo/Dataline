"""`context.txt`: which repository files a template ships, and where.

The environment template (`harbor/template/environment/context.txt`, package B)
and the verifier template (`harbor/template/tests/context.txt`, package C) each
list the static sources their image needs. The generator resolves the list into
`environment/alkera/` and `tests/alkera/` of every generated task.

GRAMMAR (published in harbor/README.md; this docstring is the reference)

    # comment                         full-line comments start with '#'
    <src>  <dest>  [option ...]       one entry per line, whitespace-separated

  * Blank lines are ignored. A '#' preceded by whitespace starts a trailing
    comment. Paths cannot contain whitespace.
  * <src> is relative to the repository root; <dest> is relative to the
    context directory (`environment/alkera/` or `tests/alkera/`). Neither may
    be absolute or contain '..'. <dest> may be '.' (the context root).
  * `{task}` in either path is replaced by the source task id
    (e.g. `tasks/{task}` -> `tasks/GT001-aov-halving`). Any other `{...}` is
    an error.
  * A directory <src> is copied recursively INTO <dest>. A file <src> is
    copied TO <dest>, or into it under its own name when <dest> ends in '/'.
  * Options:
      exclude=<glob>[,<glob>...]  paths to skip, relative to <src>. A glob
                                  without '/' matches any path component
                                  (file or directory name) at any depth; a
                                  trailing '/' restricts it to directories; a
                                  glob containing '/' is anchored at <src> and
                                  matches the path or any of its parents.
                                  Glob syntax is Python fnmatch (case-sensitive).
      gitignored=include          also copy files git ignores (see below).
  * ALWAYS excluded, whatever the entry says: `__pycache__/`, `*.pyc`,
    `.venv/`, `.pytest_cache/`, `*.duckdb.wal`.
  * A directory walk copies only files git would track
    (`git ls-files --cached --others --exclude-standard`): build caches such as
    `fixtures/build/` and generated `*.duckdb` never enter a context, so a
    context is a function of the source tree, not of what a previous run left
    on disk. `gitignored=include` opts one entry out. Outside a git checkout
    the filter is skipped (with the ALWAYS list still applied).
  * Symlinks are copied as the file they point to (inside the repository
    only); a symlinked directory is an error.
  * An entry whose <src> does not exist, two entries writing the same file,
    and an unknown option are export errors.

The caller may add repository-anchored excludes (the generator always strips
`tasks/*/solution/` and `tasks/*/prompts/` from the verifier context), files
to force back in, and a `forbid` predicate that turns a match into an error
(the environment context may never contain a task's own files).
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ALWAYS_EXCLUDE = ("__pycache__/", "*.pyc", ".venv/", ".pytest_cache/", "*.duckdb.wal")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_OPTIONS = ("exclude", "gitignored")


class ContextError(Exception):
    pass


@dataclass(frozen=True)
class ContextEntry:
    origin: str                 # "<file>:<line>" for error messages
    src: str                    # repo-relative, placeholders filled
    dest: str                   # context-relative
    excludes: tuple[str, ...] = ()
    gitignored: bool = False


@dataclass
class ContextResult:
    entries: list[ContextEntry] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)   # (src, dest)
    bytes: int = 0
    forced: list[str] = field(default_factory=list)


def _clean_rel(path: str, what: str, origin: str, allow_dot: bool = False) -> str:
    if not path:
        raise ContextError(f"{origin}: empty {what}")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path) or "\\" in path:
        raise ContextError(f"{origin}: {what} {path!r} must be a relative POSIX path")
    trailing = path.endswith("/")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ContextError(f"{origin}: {what} {path!r} may not contain '..'")
    if not parts:
        if allow_dot:
            return "."
        raise ContextError(f"{origin}: {what} {path!r} names no path")
    return "/".join(parts) + ("/" if trailing else "")


def _substitute(value: str, task: str | None, origin: str) -> str:
    def repl(match: re.Match) -> str:
        name = match.group(1)
        if name != "task":
            raise ContextError(f"{origin}: unknown placeholder {{{name}}} "
                               f"(only {{task}} is defined)")
        if not task:
            raise ContextError(f"{origin}: {{task}} used but no task id was given")
        return task
    return _PLACEHOLDER.sub(repl, value)


def parse_context(text: str, *, task: str | None = None,
                  origin: str = "context.txt") -> list[ContextEntry]:
    """Parse a context.txt body. Pure: touches no file."""
    entries: list[ContextEntry] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        where = f"{origin}:{number}"
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.split(r"\s#", line, maxsplit=1)[0].strip()
        fields = line.split()
        if len(fields) < 2:
            raise ContextError(f"{where}: expected '<src> <dest> [option...]', "
                               f"got {raw.strip()!r}")
        src = _clean_rel(_substitute(fields[0], task, where), "src", where)
        dest = _clean_rel(_substitute(fields[1], task, where), "dest", where,
                          allow_dot=True)
        excludes: list[str] = []
        gitignored = False
        for option in fields[2:]:
            key, sep, value = option.partition("=")
            if not sep or key not in _OPTIONS:
                raise ContextError(f"{where}: unknown option {option!r} "
                                   f"(known: exclude=<glob,...>, gitignored=include)")
            if key == "exclude":
                globs = [g for g in value.split(",") if g]
                if not globs:
                    raise ContextError(f"{where}: exclude= needs at least one glob")
                excludes.extend(_substitute(g, task, where) for g in globs)
            elif value == "include":
                gitignored = True
            else:
                raise ContextError(f"{where}: gitignored= accepts only 'include'")
        entries.append(ContextEntry(origin=where, src=src.rstrip("/"), dest=dest,
                                    excludes=tuple(excludes), gitignored=gitignored))
    return entries


def matches(rel: str, patterns) -> bool:
    """Does the POSIX path `rel` match any of the exclude globs (see GRAMMAR)?"""
    parts = rel.split("/")
    for pattern in patterns:
        dir_only = pattern.endswith("/")
        body = pattern.strip("/")
        if not body:
            continue
        if "/" in body:
            last = len(parts) - 1 if dir_only else len(parts)
            if any(fnmatch.fnmatchcase("/".join(parts[:i]), body)
                   for i in range(1, last + 1)):
                return True
        else:
            names = parts[:-1] if dir_only else parts
            if any(fnmatch.fnmatchcase(name, body) for name in names):
                return True
    return False


class GitView:
    """What git would track under `repo`, from a FRESH snapshot.

    `visible` is `git ls-files --cached --others --exclude-standard` taken when
    the view is made (cheap: ~20 ms here). A file that is not in it is only
    treated as ignored after `git check-ignore` confirms it, in one batch, so a
    file created after the snapshot (another package editing its template
    mid-export) is never mistaken for an ignored one. Outside a git checkout
    `available` is False and nothing is filtered.
    """

    def __init__(self, repo: Path):
        self.repo = Path(repo).resolve()
        self.visible: set[str] = set()
        self.available = True
        try:
            out = subprocess.run(
                ["git", "-C", str(self.repo), "ls-files", "-z", "--cached",
                 "--others", "--exclude-standard"],
                capture_output=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            self.available = False
            return
        self.visible = {p for p in out.decode("utf-8", "surrogateescape").split("\0") if p}

    def ignored(self, paths) -> set[str]:
        """The subset of repo-relative `paths` git ignores."""
        misses = sorted({p for p in paths if p not in self.visible})
        if not self.available or not misses:
            return set()
        r = subprocess.run(["git", "-C", str(self.repo), "check-ignore", "-z", "--stdin"],
                           input="\0".join(misses).encode("utf-8", "surrogateescape"),
                           capture_output=True)
        if r.returncode not in (0, 1):
            raise ContextError(f"git check-ignore failed: "
                               f"{r.stderr.decode(errors='replace')[:300]}")
        return {p for p in r.stdout.decode("utf-8", "surrogateescape").split("\0") if p}


def git_files(repo: Path) -> set[str] | None:
    """A fresh `git ls-files --cached --others --exclude-standard`, or None
    outside a git checkout."""
    view = GitView(repo)
    return view.visible if view.available else None


def _mode_for(path: Path) -> int:
    return 0o755 if os.stat(path).st_mode & 0o111 else 0o644


def copy_file(src: Path, dest: Path) -> int:
    """Copy bytes (following a file symlink) with a normalised mode."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    os.chmod(dest, _mode_for(src))
    return dest.stat().st_size


def _walk(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in sorted(dirnames):
            if (here / name).is_symlink():
                raise ContextError(f"{here / name}: symlinked directories are not "
                                   f"copied into a context")
        dirnames.sort()
        for name in sorted(filenames):
            out.append(here / name)
    return out


def resolve_entries(entries: list[ContextEntry], dest_root: Path, *, repo: Path,
                    repo_excludes: tuple[str, ...] = (),
                    force_include: tuple[str, ...] = (),
                    forbid: Callable[[str], str | None] | None = None,
                    ) -> ContextResult:
    """Copy every entry into `dest_root`. Returns what was copied.

    repo_excludes  repository-anchored globs (e.g. `tasks/*/solution/`) applied
                   on top of each entry's own excludes.
    force_include  repository paths copied even when an exclude matched them
                   (only if some entry's walk reached them).
    forbid         called with each repository-relative path that would be
                   copied; a non-None return is the error message.
    """
    repo = Path(repo).resolve()
    dest_root = Path(dest_root)
    git = GitView(repo)
    result = ContextResult(entries=list(entries))
    written: dict[str, str] = {}
    forced = set(force_include)

    def emit(src_path: Path, src_rel: str, dest_rel: str, origin: str) -> None:
        if forbid is not None:
            problem = forbid(src_rel)
            if problem:
                raise ContextError(f"{origin}: {src_rel}: {problem}")
        previous = written.get(dest_rel)
        if previous is not None:
            raise ContextError(f"{origin}: {dest_rel} is written twice "
                               f"(from {previous} and {src_rel})")
        target = src_path.resolve()
        if not target.is_relative_to(repo):
            raise ContextError(f"{origin}: {src_rel} resolves outside the "
                               f"repository ({target})")
        written[dest_rel] = src_rel
        result.bytes += copy_file(src_path, dest_root / dest_rel)
        result.files.append((src_rel, dest_rel))

    for entry in entries:
        src_path = repo / entry.src
        if not src_path.exists():
            raise ContextError(f"{entry.origin}: src {entry.src!r} does not exist")
        if src_path.is_file():
            if matches(entry.src, ALWAYS_EXCLUDE):
                raise ContextError(f"{entry.origin}: {entry.src} is always excluded")
            dest = entry.dest
            if dest == "." or dest.endswith("/"):
                dest = (dest.rstrip("/") + "/" if dest != "." else "") + src_path.name
            emit(src_path, entry.src, dest, entry.origin)
            continue
        if not src_path.is_dir():
            raise ContextError(f"{entry.origin}: src {entry.src!r} is not a file "
                               f"or directory")
        base = entry.dest.rstrip("/")
        candidates: list[tuple[Path, str, str]] = []
        for path in _walk(src_path):
            rel = path.relative_to(src_path).as_posix()
            repo_rel = f"{entry.src}/{rel}"
            if matches(repo_rel, ALWAYS_EXCLUDE) or not path.is_file():
                continue
            if repo_rel not in forced and (matches(rel, entry.excludes)
                                           or matches(repo_rel, repo_excludes)):
                continue
            candidates.append((path, rel, repo_rel))
        ignored = set() if entry.gitignored else git.ignored(
            repo_rel for _p, _r, repo_rel in candidates if repo_rel not in forced)
        for path, rel, repo_rel in candidates:
            if repo_rel in ignored:
                continue
            dest = rel if base == "." else f"{base}/{rel}"
            emit(path, repo_rel, dest, entry.origin)
            if repo_rel in forced:
                result.forced.append(repo_rel)
    result.files.sort()
    return result


def resolve_context(context_file: Path, dest_root: Path, *, repo: Path,
                    task: str | None = None, **kwargs) -> ContextResult:
    """Parse `context_file` and copy its entries into `dest_root`."""
    context_file = Path(context_file)
    if not context_file.is_file():
        raise ContextError(f"{context_file} does not exist")
    entries = parse_context(context_file.read_text(encoding="utf-8"), task=task,
                            origin=str(context_file))
    return resolve_entries(entries, dest_root, repo=repo, **kwargs)
