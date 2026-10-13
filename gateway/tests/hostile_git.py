"""A git repository wired the way an agent that owns it could wire it.

Test support for alkera_gateway.safegit (gateway/tests/test_safegit.py) and for
the graders that read agent repositories (tasks/harness/test_bi_git_safety.py,
which loads this file by path). Stdlib only, and nothing here imports the
gateway: the point is to build the hostile input, not to read it.

THE THREAT. The agent owns its workspace repository - the committed tree AND
`.git/` - and something more privileged reads it afterwards: a grader on the
host, the Power BI service in native-services. Git runs commands named in a
repository's own config at many points: a committed `.gitattributes` line
`* filter=pwn` makes `git archive`, `checkout` and friends pipe every file
through `filter.pwn.smudge` (or the long-running `filter.pwn.process`);
`core.fsmonitor` runs on every index refresh; hooks run on commits and ref
updates; `diff.<driver>.textconv` and `diff.external` run on diffs; a
"partial clone" fetches missing objects from its promisor remote, over any
transport the repository's config allows - `ext::` included, which is a shell.

`weaponize(repo, markers)` wires all of that at once. Every payload only
touches a file in `markers` named after the vector that ran it (and passes
stdin through, so a filter "works"), so `fired(markers)` is the list of
vectors that executed. An empty list after a read is the property under test;
a non-empty one after the OLD read (`git archive`) is what proves the wiring
is live, so an empty list is not empty for want of a working attack.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

AUTHOR = {"GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@alkera.local",
          "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@alkera.local",
          "GIT_AUTHOR_DATE": "2026-03-01T00:00:00Z",
          "GIT_COMMITTER_DATE": "2026-03-01T00:00:00Z"}

#: Every hook name git (2.53) knows; each one touches `hook-<name>`.
HOOKS = ("applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit",
         "pre-merge-commit", "prepare-commit-msg", "commit-msg", "post-commit",
         "pre-rebase", "post-checkout", "post-merge", "pre-push", "pre-receive",
         "update", "proc-receive", "post-receive", "post-update",
         "reference-transaction", "push-to-checkout", "pre-auto-gc", "post-rewrite",
         "sendemail-validate", "fsmonitor-watchman", "p4-changelist",
         "p4-prepare-changelist", "p4-post-changelist", "p4-pre-submit",
         "post-index-change")

ATTRIBUTES = "* filter=pwn diff=pwn\n"


def trusted_env() -> dict[str, str]:
    """The environment the FIXTURE's own git runs in: no system or global
    config, no inherited GIT_* variables, fixed author and dates."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(AUTHOR)
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return env


def setup_git(repo: Path, *args: str, input: bytes | None = None) -> bytes:
    """git for BUILDING fixtures (before `weaponize`): never run it after."""
    r = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull,
                        "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main",
                        "-C", str(repo), *args],
                       env=trusted_env(), input=input, capture_output=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.decode(errors='replace')}")
    return r.stdout


def write_files(repo: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def commit_all(repo: Path, message: str) -> str:
    setup_git(repo, "add", "-A")
    setup_git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return setup_git(repo, "rev-parse", "HEAD").decode().strip()


def _executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def weaponize(repo: Path, markers: Path, *, commit: bool = True) -> None:
    """Wire every command-running vector into `repo` (a plain repo whose
    `.git` is a directory). With `commit`, `.gitattributes` naming the filter
    is committed on the current branch first, as an agent would; it is always
    also in `.git/info/attributes` and in a `core.attributesFile`, which apply
    to every commit, the root one included. The payload files live beside the
    repository, never in its tree."""
    repo, markers = Path(repo), Path(markers)
    markers.mkdir(parents=True, exist_ok=True)
    if commit:
        (repo / ".gitattributes").write_text(ATTRIBUTES, encoding="utf-8")
        setup_git(repo, "add", ".gitattributes")
        setup_git(repo, "commit", "-q", "-m", "attributes")
    payload = repo.parent / f"{repo.name}.payload"
    payload.mkdir(exist_ok=True)
    pwn = _executable(payload / "pwn", f'#!/bin/sh\ntouch "{markers}/$1"\nexec cat\n')
    hooks = payload / "hooks"
    hooks.mkdir(exist_ok=True)
    git_dir = repo / ".git"
    (git_dir / "hooks").mkdir(exist_ok=True)
    for name in HOOKS:
        body = f'#!/bin/sh\ntouch "{markers}/hook-{name}"\nexit 0\n'
        _executable(hooks / name, body)
        _executable(git_dir / "hooks" / name, body)
    attributes = payload / "attributes"
    attributes.write_text(ATTRIBUTES, encoding="utf-8")
    (git_dir / "info").mkdir(exist_ok=True)
    (git_dir / "info" / "attributes").write_text(ATTRIBUTES, encoding="utf-8")

    def run(vector: str) -> str:
        return f'"{pwn} {vector}"'

    with open(git_dir / "config", "a", encoding="utf-8") as handle:
        handle.write(
            '[filter "pwn"]\n'
            f"\tsmudge = {run('smudge')}\n"
            f"\tclean = {run('clean')}\n"
            f"\tprocess = {run('process')}\n"
            '[diff "pwn"]\n'
            f"\ttextconv = {run('textconv')}\n"
            f"\tcommand = {run('diff-driver')}\n"
            "[diff]\n"
            f"\texternal = {run('diff-external')}\n"
            "[core]\n"
            f"\tfsmonitor = {run('fsmonitor')}\n"
            f'\thooksPath = "{hooks}"\n'
            f'\tattributesFile = "{attributes}"\n'
            f"\tpager = {run('pager')}\n"
            f"\teditor = {run('editor')}\n"
            f"\tsshCommand = {run('ssh')}\n"
            f"\taskPass = {run('askpass')}\n"
            "[log]\n"
            "\tshowSignature = true\n"
            "[gpg]\n"
            f"\tprogram = {run('gpg')}\n"
            "[credential]\n"
            f"\thelper = {run('credential')}\n"
            "[protocol]\n"
            "\tallow = always\n"
            '[protocol "ext"]\n'
            "\tallow = always\n")


def make_promisor(repo: Path, markers: Path, missing: str) -> None:
    """Turn `repo` into a partial clone whose promisor remote is an `ext::`
    shell command, and delete the loose object `missing`, so the next read
    of it makes git "lazily fetch" it - by running the agent's command."""
    markers.mkdir(parents=True, exist_ok=True)
    git_dir = Path(repo) / ".git"
    loose = git_dir / "objects" / missing[:2] / missing[2:]
    loose.unlink()
    with open(git_dir / "config", "a", encoding="utf-8") as handle:
        handle.write(
            "[core]\n\trepositoryformatversion = 1\n"
            "[extensions]\n\tpartialClone = origin\n"
            '[remote "origin"]\n'
            f"\turl = ext::sh -c touch% {markers}/lazy-fetch\n"
            "\tpromisor = true\n"
            '[protocol "ext"]\n\tallow = always\n')


def fired(markers: Path) -> list[str]:
    markers = Path(markers)
    return sorted(p.name for p in markers.iterdir()) if markers.is_dir() else []
