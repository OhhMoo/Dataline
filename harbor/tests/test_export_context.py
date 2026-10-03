"""context.txt: the published grammar, and what resolving it copies."""
from __future__ import annotations

import os
import subprocess

import pytest

import test_export_support  # noqa: F401  (puts harbor/ on sys.path)
from alkera_harbor.context import (ContextError, matches, parse_context,
                                   resolve_context, resolve_entries)


def _repo(tmp_path):
    """A small git repo: tracked, untracked and ignored files."""
    repo = tmp_path / "repo"
    files = {
        "pkg/a.py": "print('a')\n",
        "pkg/sub/b.py": "print('b')\n",
        "pkg/sub/data.json": "{}\n",
        "pkg/__pycache__/a.cpython-311.pyc": "x",
        "pkg/stale.pyc": "x",
        "pkg/.venv/lib.py": "x",
        "pkg/.pytest_cache/v": "x",
        "pkg/w.duckdb.wal": "x",
        "pkg/cache/build.duckdb": "generated",
        "pkg/new_untracked.py": "print('new')\n",
        "tasks/GT001-x/tests/grade.py": "grade\n",
        "tasks/GT001-x/solution/solve.sh": "#!/bin/sh\n",
        "tasks/GT001-x/prompts/vague.md": "the brief\n",
        "tasks/GT001-x/prompts/guided.md": "a stronger hint\n",
        "tasks/GT001-x/task.toml": "[task]\n",
        "single.txt": "one\n",
        ".gitignore": "*.duckdb\ncache/\n",
    }
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (repo / "pkg" / "run.sh").write_text("#!/bin/sh\n")
    (repo / "pkg" / "run.sh").chmod(0o750)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "add", "pkg/a.py", "pkg/sub", "pkg/run.sh",
                    "tasks", "single.txt", ".gitignore"], check=True, env=env)
    return repo


def _copied(result):
    return sorted(dest for _src, dest in result.files)


# ------------------------------------------------------------------ grammar
def test_parse_comments_blank_lines_and_options():
    text = """
# a comment
   # an indented comment

world/world   world/world
gateway/src   gateway/src   exclude=*.egg-info,tests/   # trailing comment
tasks/{task}  repo/tasks/{task}  exclude={task}.lock gitignored=include
"""
    entries = parse_context(text, task="GT001-aov-halving", origin="ctx")
    assert [(e.src, e.dest) for e in entries] == [
        ("world/world", "world/world"),
        ("gateway/src", "gateway/src"),
        ("tasks/GT001-aov-halving", "repo/tasks/GT001-aov-halving")]
    assert entries[1].excludes == ("*.egg-info", "tests/")
    assert entries[2].excludes == ("GT001-aov-halving.lock",)
    assert entries[2].gitignored and not entries[0].gitignored
    assert entries[0].origin == "ctx:5"


@pytest.mark.parametrize("line, needle", [
    ("/abs/path  dest", "relative"),
    ("src  /abs", "relative"),
    ("../up  dest", "'..'"),
    ("src  a/../b", "'..'"),
    ("src", "expected"),
    ("src dest bogus=1", "unknown option"),
    ("src dest exclude=", "at least one glob"),
    ("src dest gitignored=yes", "only 'include'"),
    ("{nope}/x dest", "unknown placeholder"),
    ("C:/win dest", "relative"),
])
def test_parse_rejects_bad_lines(line, needle):
    with pytest.raises(ContextError, match=needle):
        parse_context(line, task="T")


def test_task_placeholder_needs_a_task():
    with pytest.raises(ContextError, match="no task id"):
        parse_context("tasks/{task} x", task=None)


def test_dest_dot_means_context_root():
    (entry,) = parse_context("pkg .", task=None)
    assert entry.dest == "."


@pytest.mark.parametrize("rel, patterns, hit", [
    ("a/b/c.pyc", ["*.pyc"], True),
    ("a/__pycache__/c", ["__pycache__/"], True),
    ("a/__pycache__", ["__pycache__/"], False),      # a FILE of that name
    ("a/node/x", ["node"], True),                     # component, any depth
    ("fixtures/build/x.duckdb", ["fixtures/build"], True),   # anchored, parent
    ("deep/fixtures/build/x", ["fixtures/build"], False),    # anchored at src
    ("tests/x.py", ["tests/"], True),
    ("tests", ["tests/"], False),
    ("a/b.txt", ["*.py"], False),
])
def test_exclude_glob_semantics(rel, patterns, hit):
    assert matches(rel, patterns) is hit


# ------------------------------------------------------------------ resolving
def test_resolve_copies_tracked_and_untracked_skips_ignored_and_always(tmp_path):
    repo = _repo(tmp_path)
    out = tmp_path / "ctx"
    result = resolve_entries(parse_context("pkg  lib/pkg"), out, repo=repo)
    assert _copied(result) == ["lib/pkg/a.py", "lib/pkg/new_untracked.py",
                               "lib/pkg/run.sh", "lib/pkg/sub/b.py",
                               "lib/pkg/sub/data.json"]
    # executable bit survives, normalised
    assert oct(os.stat(out / "lib/pkg/run.sh").st_mode & 0o777) == "0o755"
    assert oct(os.stat(out / "lib/pkg/a.py").st_mode & 0o777) == "0o644"


def test_gitignored_include_opts_in_but_always_list_still_applies(tmp_path):
    repo = _repo(tmp_path)
    result = resolve_entries(parse_context("pkg lib gitignored=include"),
                             tmp_path / "ctx", repo=repo)
    copied = _copied(result)
    assert "lib/cache/build.duckdb" in copied
    for junk in ("stale.pyc", "__pycache__", ".venv", ".pytest_cache", ".wal"):
        assert not any(junk in c for c in copied), junk


def test_entry_excludes_and_file_entries(tmp_path):
    repo = _repo(tmp_path)
    text = ("pkg  lib  exclude=sub/,run.sh\n"
            "single.txt  docs/one.txt\n"
            "single.txt  into/\n")
    result = resolve_entries(parse_context(text), tmp_path / "ctx", repo=repo)
    assert _copied(result) == ["docs/one.txt", "into/single.txt", "lib/a.py",
                               "lib/new_untracked.py"]


def test_missing_src_is_an_error(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(ContextError, match="does not exist"):
        resolve_entries(parse_context("nope  x"), tmp_path / "ctx", repo=repo)


def test_two_entries_writing_one_file_is_an_error(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(ContextError, match="written twice"):
        resolve_entries(parse_context("single.txt a.txt\nsingle.txt a.txt\n"),
                        tmp_path / "ctx", repo=repo)


def test_symlinked_directory_is_an_error(tmp_path):
    repo = _repo(tmp_path)
    (repo / "pkg" / "link").symlink_to(repo / "pkg" / "sub")
    with pytest.raises(ContextError, match="symlinked directories"):
        resolve_entries(parse_context("pkg lib gitignored=include"),
                        tmp_path / "ctx", repo=repo)


def test_task_solution_and_prompts_stripped_but_brief_forced_back(tmp_path):
    repo = _repo(tmp_path)
    result = resolve_entries(
        parse_context("tasks/{task} repo/tasks/{task}", task="GT001-x"),
        tmp_path / "ctx", repo=repo,
        repo_excludes=("tasks/*/solution/", "tasks/*/prompts/"),
        force_include=("tasks/GT001-x/prompts/vague.md",))
    assert _copied(result) == ["repo/tasks/GT001-x/prompts/vague.md",
                               "repo/tasks/GT001-x/task.toml",
                               "repo/tasks/GT001-x/tests/grade.py"]
    assert result.forced == ["tasks/GT001-x/prompts/vague.md"]


def test_forbid_predicate_turns_a_match_into_an_error(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(ContextError, match="never in the image"):
        resolve_entries(parse_context("tasks lib"), tmp_path / "ctx", repo=repo,
                        forbid=lambda rel: "never in the image"
                        if rel.startswith("tasks/GT") else None)


def test_resolve_context_reads_the_file(tmp_path):
    repo = _repo(tmp_path)
    ctx = tmp_path / "context.txt"
    ctx.write_text("tasks/{task}/tests  t\n")
    result = resolve_context(ctx, tmp_path / "out", repo=repo, task="GT001-x")
    assert _copied(result) == ["t/grade.py"]
    with pytest.raises(ContextError, match="does not exist"):
        resolve_context(tmp_path / "missing.txt", tmp_path / "out", repo=repo)


# ------------------------------------------------------------------ git snapshots
def test_a_file_created_after_the_snapshot_is_not_mistaken_for_ignored(tmp_path):
    """Other packages edit their templates while an export runs: a file that
    appears after `git ls-files` ran must still be copied."""
    from alkera_harbor.context import GitView
    repo = _repo(tmp_path)
    view = GitView(repo)
    (repo / "pkg" / "late.py").write_text("print('late')\n")
    assert view.ignored(["pkg/late.py", "pkg/cache/build.duckdb"]) == \
        {"pkg/cache/build.duckdb"}


def test_template_copy_refuses_ignored_files_but_takes_new_ones(tmp_path):
    from alkera_harbor.adapter import ExportError, copy_template
    repo = _repo(tmp_path)
    template = repo / "pkg" / "sub"
    (template / "context.txt").write_text("# not copied\n")
    (template / "fresh.sh").write_text("#!/bin/sh\n")          # untracked, not ignored
    assert sorted(copy_template(template, tmp_path / "out", repo=repo)) == \
        ["b.py", "data.json", "fresh.sh"]
    (template / "cache").mkdir()
    (template / "cache" / "x.bin").write_text("generated")      # ignored by cache/
    with pytest.raises(ExportError, match="git-ignored"):
        copy_template(template, tmp_path / "out2", repo=repo)
