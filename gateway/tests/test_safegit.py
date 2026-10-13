"""alkera_gateway.safegit: an agent-written repository is read, never run.

hostile_git.weaponize wires every command-running vector git has into a
repository (smudge / clean / process filters named by a COMMITTED
.gitattributes, .git/info/attributes and core.attributesFile; core.fsmonitor;
hooks; textconv; diff.external; pager; gpg; credential helper; ext::). Each
payload touches a marker. What is proved:

1. The wiring is live: the read the graders and the Power BI service used to
   do (`git archive`) fires it on this git.
2. Every safegit read returns the committed bytes and fires nothing.
3. On a benign repository every read is byte-for-byte what the old git
   commands returned (`git archive`, `rev-list`, `show`, `for-each-ref`...).
4. A `.git` that is a file (`gitdir:` redirect), a symlink or missing is
   refused with GitUnreadable, and so is a ref that is an option.
5. A partial clone cannot lazily fetch through its own `ext::` remote.
6. world/world/fingerprint.py's private copy of the env and argv is this one.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import hostile_git as H
import pytest

from alkera_gateway import safegit

REPO = Path(__file__).resolve().parents[2]

FILES = {
    "README.md": b"# workspace\n",
    "bi/powerbi/Commerce.SemanticModel/definition/model.tmdl": b"model Model\n\tculture: en-US\n",
    "bi/powerbi/Commerce.Report/definition.pbir": b'{"version": "4.0"}\r\n',
    "bi/powerbi/blob.bin": bytes(range(256)) * 3,
    "bi/tableau/workbooks/Exec Overview.twb": b"<workbook/>\n",
    "bi/sigma/ümlaut é.json": "{\"name\": \"é\"}\n".encode(),
    "bi/empty.txt": b"",
    "bix/not-bi.txt": b"a sibling whose name starts with bi\n",
    "dbt_project/models/fct_orders.sql": b"select 1 as id\n",
    "dbt_project/macros/m.sql": b"{% macro m() %}{% endmacro %}\n",
    "docs/notes.md": b"$Id$ $Format:%H$\n",
}


def build(root: Path) -> Path:
    """A benign repository: two commits on main, a branch, a symlink, an
    executable, non-ASCII and space-bearing names, a CRLF file, binary bytes."""
    repo = root / "workspace"
    repo.mkdir(parents=True)
    H.setup_git(repo, "init", "-q")
    H.write_files(repo, FILES)
    (repo / "bi" / "powerbi" / "link").symlink_to("blob.bin")
    run = repo / "bi" / "run.sh"
    run.write_bytes(b"#!/bin/sh\necho hi\n")
    run.chmod(0o755)
    H.commit_all(repo, "seed")
    (repo / "bi/powerbi/Commerce.SemanticModel/definition/model.tmdl").write_bytes(
        b"model Model\n\tculture: en-GB\n")
    H.commit_all(repo, "culture")
    H.setup_git(repo, "switch", "-q", "-c", "feature")
    H.write_files(repo, {"bi/sigma/feature.json": b"{\"feature\": true}\n"})
    H.commit_all(repo, "feature")
    H.setup_git(repo, "switch", "-q", "main")
    return repo


def raw(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """The OLD call: the graders' and powerbi.git_project's exact argv, in the
    caller's environment (system/global config shut out so the host's own
    config cannot change the answer either way)."""
    return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), *args],
                          capture_output=True, check=check, env=H.trusted_env())


def archived(repo: Path, ref: str, paths) -> dict[str, bytes]:
    """{path: bytes} of the regular files `git archive <ref> <paths>` emits."""
    tar = tarfile.open(fileobj=io.BytesIO(raw(repo, "archive", "--format=tar", ref,
                                               *paths).stdout))
    return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


@pytest.fixture
def benign(tmp_path) -> Path:
    return build(tmp_path)


@pytest.fixture
def hostile(tmp_path):
    repo = build(tmp_path)
    markers = tmp_path / "markers"
    H.weaponize(repo, markers)
    return repo, markers


# -------------------------------------------------------------- the pins
def test_argv_is_the_decided_one():
    assert safegit.git_argv(Path("/w")) == [
        "git", "-c", "safe.directory=*", "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null",
        "-c", "protocol.allow=never", "-C", "/w"]


def test_env_shuts_out_config_and_inherited_git_variables(monkeypatch):
    for name, value in {"GIT_DIR": "/elsewhere/.git", "GIT_WORK_TREE": "/elsewhere",
                        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "filter.x.smudge",
                        "GIT_CONFIG_VALUE_0": "touch /tmp/x", "GIT_CONFIG": "/tmp/c",
                        "GIT_CONFIG_PARAMETERS": "'core.fsmonitor'='touch /tmp/x'",
                        "GIT_INDEX_FILE": "/tmp/index", "GIT_EXTERNAL_DIFF": "touch",
                        "GIT_ALLOW_PROTOCOL": "ext", "GIT_NAMESPACE": "x"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ALKERA_UNRELATED", "kept")
    env = safegit.git_env()
    assert {k: v for k, v in env.items() if k.startswith("GIT_")} == {
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""}
    assert env["ALKERA_UNRELATED"] == "kept" and env["PATH"] == os.environ["PATH"]


def test_world_fingerprint_carries_the_same_env_and_argv(monkeypatch):
    """world/ must not import the gateway, so fingerprint.git_head keeps a
    private copy; this pins the copy to the original."""
    sys.path.insert(0, str(REPO / "world"))
    from world import fingerprint

    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
    assert fingerprint._git_env() == safegit.git_env()
    assert fingerprint._git_argv(Path("/w")) == safegit.git_argv(Path("/w"))


def test_world_fingerprint_reads_a_hostile_workspace_head(hostile, tmp_path):
    sys.path.insert(0, str(REPO / "world"))
    from world import fingerprint

    repo, markers = hostile
    assert fingerprint.git_head(repo.parent) == safegit.rev_parse(repo, "HEAD")
    redirected = tmp_path / "redirected"
    (redirected / "workspace").mkdir(parents=True)
    (redirected / "workspace" / ".git").write_text(f"gitdir: {repo / '.git'}\n")
    assert fingerprint.git_head(redirected) is None
    assert H.fired(markers) == []


# ------------------------------------------------------- 1. live wiring
def test_the_old_read_runs_the_agents_filter(hostile):
    """Control: `git archive`, as the graders called it, runs the agent's code
    on this git - so the empty marker lists below mean something."""
    repo, markers = hostile
    raw(repo, "archive", "--format=tar", "HEAD", "bi", check=False)
    assert set(H.fired(markers)) & {"smudge", "process"}, H.fired(markers)


# --------------------------------------------- 2. nothing runs, bytes right
def test_reads_on_a_hostile_repository_run_nothing(hostile, tmp_path):
    repo, markers = hostile
    head = safegit.rev_parse(repo, "HEAD")
    assert head and len(head) == 40
    assert safegit.symbolic_ref(repo) == "main"
    assert safegit.exists(repo, "HEAD", "bi") and not safegit.exists(repo, "HEAD", "nope")
    tree = safegit.read_tree(repo, "HEAD", ["bi", "dbt_project/models"])
    expected = {rel: data for rel, data in FILES.items()
                if rel.startswith(("bi/", "dbt_project/models/"))}
    expected["bi/powerbi/Commerce.SemanticModel/definition/model.tmdl"] = \
        b"model Model\n\tculture: en-GB\n"
    expected["bi/run.sh"] = b"#!/bin/sh\necho hi\n"
    assert tree == expected
    assert safegit.read_tree(repo, "HEAD", [".gitattributes"]) == {
        ".gitattributes": H.ATTRIBUTES.encode()}
    first = safegit.rev_list(repo, "HEAD", "bi/powerbi")
    assert len(first) == 2
    assert safegit.read_tree(repo, first[0], ["bi/powerbi"])[
        "bi/powerbi/Commerce.SemanticModel/definition/model.tmdl"] == \
        FILES["bi/powerbi/Commerce.SemanticModel/definition/model.tmdl"]
    assert safegit.read_object(repo, "feature", "bi/sigma/feature.json") == \
        ("blob", b"{\"feature\": true}\n")
    assert safegit.read_object(repo, "feature", "nope") is None
    refs = safegit.for_each_ref(repo, "%(refname:short) %(objectname)", "refs/heads")
    assert sorted(line.split()[0] for line in refs) == ["feature", "main"]
    assert H.fired(markers) == []


def test_a_partial_clone_cannot_fetch_through_its_own_remote(benign, tmp_path):
    """The decided argv alone (`protocol.allow=never`) does not stop this: a
    repository's own `protocol.ext.allow = always` outranks it. The env does
    (GIT_NO_LAZY_FETCH, and GIT_ALLOW_PROTOCOL overrides every protocol.*)."""
    markers = tmp_path / "markers"
    blob = H.setup_git(benign, "rev-parse", "HEAD:bi/run.sh").decode().strip()
    H.make_promisor(benign, markers, blob)
    leaky = {k: v for k, v in safegit.git_env().items()
             if k not in ("GIT_NO_LAZY_FETCH", "GIT_ALLOW_PROTOCOL")}
    subprocess.run([*safegit.git_argv(benign), "cat-file", "-p", blob],
                   capture_output=True, env=leaky, timeout=60)
    assert H.fired(markers) == ["lazy-fetch"], "control: the vector is live"
    (markers / "lazy-fetch").unlink()
    with pytest.raises(safegit.GitUnreadable):
        safegit.read_tree(benign, "HEAD", ["bi"])
    assert safegit.exists(benign, "HEAD", "bi/run.sh") is False
    assert safegit.read_object(benign, "HEAD", "bi/run.sh") is None
    assert H.fired(markers) == []


# ------------------------------------------------ 3. byte-identical reads
@pytest.mark.parametrize("ref, paths", [
    ("HEAD", ("bi", "dbt_project/models")),
    ("HEAD", ("bi", "dbt_project/models", "docs")),
    ("feature", ("bi",)),
    ("main", ("bi/powerbi",)),
    ("HEAD~1", ("bi/powerbi",)),
    ("HEAD", ("README.md", "bi/sigma")),
    ("HEAD", ()),
])
def test_read_tree_is_git_archive_on_a_benign_repository(benign, ref, paths):
    assert safegit.read_tree(benign, ref, paths) == archived(benign, ref, paths)


def test_a_path_that_matches_nothing_fails_as_git_archive_does(benign):
    assert raw(benign, "archive", "--format=tar", "HEAD", "bi", "nope",
               check=False).returncode != 0
    with pytest.raises(safegit.GitUnreadable):
        safegit.read_tree(benign, "HEAD", ["bi", "nope"])
    with pytest.raises(safegit.GitUnreadable):
        safegit.read_tree(benign, "no-such-ref", ["bi"])


def test_the_small_reads_answer_as_the_old_commands(benign):
    def out(*args) -> str:
        r = raw(benign, *args, check=False)
        return r.stdout.decode().strip() if r.returncode == 0 else ""

    assert safegit.rev_parse(benign, "HEAD") == out("rev-parse", "HEAD")
    assert safegit.rev_parse(benign, "feature") == out("rev-parse", "feature")
    assert safegit.rev_parse(benign, "no-such-ref") is None
    assert safegit.symbolic_ref(benign) == out("symbolic-ref", "-q", "--short", "HEAD")
    for p in ("bi", "bix", "docs/notes.md", "nope", "bi/powerbi/link"):
        assert safegit.exists(benign, "HEAD", p) == (
            raw(benign, "cat-file", "-e", f"HEAD:{p}", check=False).returncode == 0)
    assert safegit.rev_list(benign, "HEAD", "bi/powerbi") == \
        out("rev-list", "--reverse", "HEAD", "--", "bi/powerbi").split()
    assert safegit.rev_list(benign, "HEAD", "bi/powerbi", reverse=False) == \
        out("rev-list", "HEAD", "--", "bi/powerbi").split()
    assert safegit.rev_list(benign, "HEAD", "nope") == []
    fmt = "%(refname:short) %(objectname)"
    assert safegit.for_each_ref(benign, fmt, "refs/heads") == \
        out("for-each-ref", f"--format={fmt}", "refs/heads").splitlines()
    shown = raw(benign, "show", "feature:bi/sigma/feature.json").stdout
    assert safegit.read_object(benign, "feature", "bi/sigma/feature.json") == ("blob", shown)
    kind, _ = safegit.read_object(benign, "HEAD", "bi")
    assert kind == "tree"
    H.setup_git(benign, "checkout", "-q", "--detach", "HEAD~1")
    assert safegit.symbolic_ref(benign) is None
    assert safegit.rev_parse(benign, "HEAD") == out("rev-parse", "HEAD")


def test_symlinks_and_submodules_are_not_files(benign):
    blob = H.setup_git(benign, "rev-parse", "HEAD:README.md").decode().strip()
    head = H.setup_git(benign, "rev-parse", "HEAD").decode().strip()
    H.setup_git(benign, "update-index", "--add", "--cacheinfo", f"160000,{head},bi/sub")
    H.setup_git(benign, "update-index", "--add", "--cacheinfo", f"120000,{blob},bi/l2")
    H.setup_git(benign, "commit", "-q", "-m", "gitlink and symlink")
    tree = safegit.read_tree(benign, "HEAD", ["bi"])
    assert "bi/sub" not in tree and "bi/l2" not in tree and "bi/powerbi/link" not in tree
    assert tree == archived(benign, "HEAD", ["bi"])


# ---------------------------------------------------------- 4. refusals
def _helpers(repo: Path):
    return {
        "read_tree": lambda: safegit.read_tree(repo, "HEAD", ["bi"]),
        "rev_parse": lambda: safegit.rev_parse(repo, "HEAD"),
        "exists": lambda: safegit.exists(repo, "HEAD", "bi"),
        "rev_list": lambda: safegit.rev_list(repo, "HEAD", "bi"),
        "symbolic_ref": lambda: safegit.symbolic_ref(repo),
        "for_each_ref": lambda: safegit.for_each_ref(repo, "%(refname)", "refs/heads"),
        "read_object": lambda: safegit.read_object(repo, "HEAD", "bi/run.sh"),
    }


@pytest.mark.parametrize("shape", ["gitdir-file", "symlink", "missing"])
def test_a_git_that_is_not_a_plain_directory_is_refused(benign, tmp_path, shape):
    other = tmp_path / "redirected"
    other.mkdir()
    H.write_files(other, {"bi/x.txt": b"x\n"})
    if shape == "gitdir-file":
        (other / ".git").write_text(f"gitdir: {benign / '.git'}\n", encoding="utf-8")
    elif shape == "symlink":
        (other / ".git").symlink_to(benign / ".git")
    # "missing": no .git at all - and tmp_path is not inside a repository, but
    # git would walk up to look for one; safegit never does.
    for name, call in _helpers(other).items():
        with pytest.raises(safegit.GitUnreadable):
            call()
            pytest.fail(f"{name} read through a {shape} .git")


def test_a_git_directory_that_is_not_a_repository_is_refused(tmp_path):
    """An invalid .git directory would send git's discovery to the parent
    directories; safegit pins GIT_DIR, so it is simply unreadable."""
    parent = build(tmp_path)
    inner = parent / "inner"
    (inner / ".git").mkdir(parents=True)
    with pytest.raises(safegit.GitUnreadable):
        safegit.read_tree(inner, "HEAD", ["bi"])
    with pytest.raises(safegit.GitUnreadable):
        safegit.rev_parse(inner, "HEAD")


@pytest.mark.parametrize("ref", ["--output=pwned", "-h", "--all", "HEAD\nHEAD"])
def test_a_ref_that_is_an_option_is_not_one(benign, ref):
    assert safegit.rev_parse(benign, ref) is None
    assert safegit.exists(benign, ref, "bi") is False
    assert safegit.read_object(benign, ref, "bi") is None
    with pytest.raises(safegit.GitUnreadable):
        safegit.read_tree(benign, ref, ["bi"])
    with pytest.raises(safegit.GitUnreadable):
        safegit.rev_list(benign, ref, "bi")
    assert not (benign / "pwned").exists()


def test_a_branch_whose_name_starts_with_a_dash_is_read(benign):
    H.setup_git(benign, "update-ref", "refs/heads/-dash", "feature")
    assert "bi/sigma/feature.json" in safegit.read_tree(benign, "-dash", ["bi"])
    assert safegit.rev_parse(benign, "-dash") == safegit.rev_parse(benign, "feature")
    assert safegit.exists(benign, "-dash", "bi/sigma/feature.json")


@pytest.mark.parametrize("path", ["../x", "bi/../../x", "/etc", "", "bi\nx"])
def test_a_path_outside_the_tree_is_refused(benign, path):
    with pytest.raises(safegit.GitUnreadable):
        safegit.read_tree(benign, "HEAD", [path])
