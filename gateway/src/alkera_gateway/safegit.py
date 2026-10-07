"""Read a git repository someone else wrote, without running anything it says to.

WHY. The agent owns its workspace repository: the committed tree AND `.git/`.
Afterwards something more privileged reads that repository - every BI task's
grader on the host (tasks/harness/episode.py `grade()`), the Power BI service
in native-services (`powerbi.git_project`, which provisions the unforgeable
published state). Git runs commands a repository names for itself: a
committed `.gitattributes` line `* filter=x` makes `git archive`, `checkout`
and `diff` pipe file contents through `filter.x.smudge` / `filter.x.process`
from `.git/config` (gateway/tests/test_safegit.py shows `git archive` doing it
on a weaponized repository, as the BI graders called it); `core.fsmonitor`
runs on index refreshes; hooks run on ref updates; `diff.<driver>.textconv` and
`diff.external` run on diffs; a partial clone fetches missing objects through
its promisor remote, over any transport its config allows (`ext::` is a shell,
and a repository's own `protocol.ext.allow` outranks a command-line
`protocol.allow`). A grader that ran `git archive` on such a repository ran
the agent's code as the host user.

HOW. Only plumbing that applies no conversion is used - `ls-tree` to list a
tree, `cat-file --batch` for object bytes (no filters, no textconv unless asked,
and nothing here asks), `rev-parse`, `rev-list`, `symbolic-ref`,
`for-each-ref` - under `git_argv` (fsmonitor off, hooks and global attributes
pointed at /dev/null, every protocol refused) and `git_env` (no system or
global config, no system attributes, no prompts, no lazy fetch, no protocol
allowed; no GIT_* variable inherited, so a caller's GIT_DIR / GIT_CONFIG_* /
GIT_CONFIG_PARAMETERS cannot redirect or reconfigure the read). The repository
is `<repo>/.git` and nothing else: GIT_DIR is pinned to it (and GIT_WORK_TREE to
`<repo>`, whatever core.worktree says), and a `.git` that is a file (`gitdir:`
redirect), a symlink or missing is refused rather than followed - on the host
those would point wherever the agent liked, and an invalid `.git` directory
would otherwise send git's discovery up into the parent directories. A ref is
never parsed as an option (`--end-of-options`, or stdin; branch names are the
agent's too), and a path with `..` is refused.

FIDELITY. On a repository without conversion attributes every read is
byte-for-byte what the command it replaces returned: `read_tree` is the
regular files of `git archive <ref> <paths>` (symlinks and submodules, which a
tar extraction of regular files skipped, are skipped), and a path that matches
nothing fails as `git archive` failed. What differs on a repository WITH such
attributes is exactly the point: the committed bytes, not the filter's output
(and `export-ignore` / `export-subst`, which only `git archive` honoured, are
not applied).

Stdlib only: the graders import it (they run in the harness venv, or in
Harbor's verifier with gateway/src on the path), and world/world/fingerprint.py
keeps a private copy of `git_env` / `git_argv` that gateway/tests/test_safegit.py
pins to these.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

__all__ = ["GitUnreadable", "git_env", "git_argv", "read_tree", "read_object",
           "rev_parse", "exists", "rev_list", "symbolic_ref", "for_each_ref"]

#: Seconds any one git call may take. A repository is local; a read that takes
#: longer is a repository built to stall the reader.
TIMEOUT = 120.0

_HEADER = re.compile(rb"^([0-9a-f]{40}|[0-9a-f]{64}) (blob|tree|commit|tag) (\d+)$")
_MISSING = (b" missing", b" ambiguous", b" dangling", b" loop", b" notdir",
            b" submodule")


class GitUnreadable(Exception):
    """The repository, or what was asked of it, cannot be read safely."""


def git_env() -> dict[str, str]:
    """The current environment without any GIT_* variable, plus the settings
    that keep config, attributes, prompts and transports out of a read."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        # A partial clone fetches a missing object from its promisor remote,
        # over whatever transport the REPOSITORY's config allows: no fetching,
        # and (for gits older than 2.45, which ignore the former) no protocol.
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "",
    })
    return env


def git_argv(repo: Path) -> list[str]:
    """`git` with the repository's own command-running settings overridden."""
    return ["git", "-c", "safe.directory=*", "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null",
            "-c", "protocol.allow=never", "-C", str(repo)]


# ------------------------------------------------------------------ running
def _git_dir(repo: Path) -> Path:
    git_dir = Path(repo).absolute() / ".git"
    if git_dir.is_symlink():
        raise GitUnreadable(f"{git_dir} is a symlink; only a plain .git directory is read")
    if git_dir.is_file():
        raise GitUnreadable(f"{git_dir} is a file (a gitdir: redirect); only a plain "
                            f".git directory is read")
    if not git_dir.is_dir():
        raise GitUnreadable(f"{repo} has no .git directory")
    return git_dir


def _run(repo: Path, args: Sequence[str], stdin: bytes | None = None,
         timeout: float = TIMEOUT) -> subprocess.CompletedProcess:
    env = git_env()
    env["GIT_DIR"] = str(_git_dir(repo))
    env["GIT_WORK_TREE"] = str(Path(repo).absolute())
    try:
        return subprocess.run([*git_argv(Path(repo)), *args], input=stdin,
                              capture_output=True, env=env, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitUnreadable(f"git {args[0]} in {repo}: {exc}") from None


def _failed(repo: Path, args: Sequence[str], done: subprocess.CompletedProcess):
    err = done.stderr.decode("utf-8", "replace").strip().splitlines()
    return GitUnreadable(f"git {args[0]} in {repo} exited {done.returncode}: "
                         f"{err[-1] if err else ''}"[:400])


def _answer(repo: Path, args: Sequence[str], done: subprocess.CompletedProcess) -> str | None:
    """A `-q` query's answer: its output, None for exit 1 (git's "no"), and
    GitUnreadable for anything else (128: not a repository, corrupt, ...)."""
    if done.returncode == 0:
        return _text(done.stdout).strip() or None
    if done.returncode == 1:
        return None
    raise _failed(repo, args, done)


def _text(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def _line_safe(*parts: str) -> bool:
    return not any(c in part for part in parts for c in "\n\r\0")


def _rel(path: str) -> str:
    """A repository-relative path as given, refused if it could leave the tree."""
    pure = PurePosixPath(path)
    if not path or not _line_safe(path) or pure.is_absolute() or ".." in pure.parts:
        raise GitUnreadable(f"refusing path {path!r}")
    return path


def _batch(repo: Path, names: Sequence[str]) -> list[tuple[str, bytes] | None]:
    """(type, raw bytes) for each object name via `cat-file --batch`, None for
    one that does not resolve. No filter, no textconv: the stored bytes."""
    if not names:
        return []
    args = ["cat-file", "--batch"]
    done = _run(repo, args, stdin="".join(f"{n}\n" for n in names).encode(
        "utf-8", "surrogateescape"))
    if done.returncode:
        raise _failed(repo, args, done)
    out, pos, found = done.stdout, 0, []
    for name in names:
        end = out.find(b"\n", pos)
        if end < 0:
            raise GitUnreadable(f"git cat-file in {repo}: truncated output at {name!r}")
        header = out[pos:end]
        match = _HEADER.match(header)
        if match is None:
            if not header.endswith(_MISSING):
                raise GitUnreadable(f"git cat-file in {repo}: unexpected {header[:200]!r}")
            found.append(None)
            pos = end + 1
            continue
        size = int(match.group(3))
        start = end + 1
        if out[start + size:start + size + 1] != b"\n":
            raise GitUnreadable(f"git cat-file in {repo}: short object {name!r}")
        found.append((match.group(2).decode(), out[start:start + size]))
        pos = start + size + 1
    return found


# -------------------------------------------------------------------- reads
def read_tree(repo: Path, ref: str, paths: Sequence[str] = ()) -> dict[str, bytes]:
    """{path: bytes} of the regular files under `paths` (all of them when
    empty) in `ref`'s tree - what `git archive <ref> <paths>` held as files,
    as committed. Symlinks and submodules are skipped. GitUnreadable when the
    repository or ref does not read, a path leaves the tree, or a path matches
    nothing (as `git archive` refused)."""
    wanted = [_rel(p) for p in paths]
    if not _line_safe(ref):
        raise GitUnreadable(f"refusing ref {ref!r}")
    # No "--": after --end-of-options ls-tree would take it for a path.
    args = ["ls-tree", "-r", "-z", "--full-tree", "--end-of-options", ref, *wanted]
    done = _run(repo, args)
    if done.returncode:
        raise _failed(repo, args, done)
    entries, matched = [], set()
    for record in done.stdout.split(b"\0"):
        if not record:
            continue
        meta, tab, name = record.partition(b"\t")
        fields = meta.split(b" ")
        if not tab or len(fields) != 3:
            raise GitUnreadable(f"git ls-tree in {repo}: unexpected {record[:200]!r}")
        path = _text(name)
        matched.update(p for p in wanted if path == p or path.startswith(p.rstrip("/") + "/"))
        mode, kind, oid = int(fields[0], 8), fields[1], _text(fields[2])
        parts = PurePosixPath(path).parts
        if kind != b"blob" or mode & 0o170000 != 0o100000 or not parts \
                or any(part in ("", ".", "..") for part in parts) or path.startswith("/"):
            continue
        entries.append((path, oid))
    unmatched = [p for p in wanted if p not in matched]
    if unmatched:
        raise GitUnreadable(f"{ref} has nothing at {', '.join(unmatched)}")
    blobs = _batch(repo, [oid for _, oid in entries])
    out = {}
    for (path, oid), blob in zip(entries, blobs, strict=True):
        if blob is None or blob[0] != "blob":
            raise GitUnreadable(f"{ref}:{path} ({oid}) is not a readable blob")
        out[path] = blob[1]
    return out


def read_object(repo: Path, ref: str, path: str) -> tuple[str, bytes] | None:
    """(type, raw bytes) of the object at `<ref>:<path>`, None when there is
    none. For a blob that is exactly what `git show <ref>:<path>` printed;
    for a tree it is the raw tree object (git show printed a listing)."""
    if not _line_safe(ref, path):
        return None
    return _batch(repo, [f"{ref}:{path}"])[0]


def rev_parse(repo: Path, ref: str) -> str | None:
    """The object name `ref` resolves to, or None."""
    if not _line_safe(ref):
        return None
    args = ["rev-parse", "--verify", "-q", "--end-of-options", ref]
    return _answer(repo, args, _run(repo, args))


def exists(repo: Path, ref: str, path: str) -> bool:
    """`git cat-file -e <ref>:<path>`: is there an object at that path."""
    if not _line_safe(ref, path):
        return False
    args = ["cat-file", "--batch-check"]
    done = _run(repo, args, stdin=f"{ref}:{path}\n".encode("utf-8", "surrogateescape"))
    if done.returncode:
        raise _failed(repo, args, done)
    return _HEADER.match(done.stdout.rstrip(b"\n")) is not None


def rev_list(repo: Path, ref: str, path: str, reverse: bool = True) -> list[str]:
    """The commits reachable from `ref` that touch `path` (`git rev-list
    [--reverse] <ref> -- <path>`), oldest first by default."""
    if not _line_safe(ref):
        raise GitUnreadable(f"refusing ref {ref!r}")
    args = ["rev-list", *(["--reverse"] if reverse else []), "--end-of-options", ref,
            "--", _rel(path)]
    done = _run(repo, args)
    if done.returncode:
        raise _failed(repo, args, done)
    return _text(done.stdout).split()


def symbolic_ref(repo: Path, name: str = "HEAD", short: bool = True) -> str | None:
    """The branch `name` points at (`git symbolic-ref -q [--short] HEAD`), None
    when it is detached."""
    args = ["symbolic-ref", "-q", *(["--short"] if short else []), name]
    return _answer(repo, args, _run(repo, args))


def for_each_ref(repo: Path, fmt: str, *patterns: str) -> list[str]:
    """`git for-each-ref --format=<fmt> <patterns>`, one string per ref."""
    args = ["for-each-ref", f"--format={fmt}", *patterns]
    done = _run(repo, args)
    if done.returncode:
        raise _failed(repo, args, done)
    return _text(done.stdout).splitlines()
