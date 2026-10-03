"""Build one arm's world exactly as the harness does, then re-root it.

Order (repo_map section 0 steps 3 and 1; `rollout.run_rollout` for
`--driver native --topology container`):

  1. `taskspec.load(task, arm)` with `ALKERA_TASK_VARIANT=<arm>` exported;
  2. `episode.prepare_task` then `episode.build_world` into a rollout-shaped
     temp dir (`world/`, `grading/` siblings: the grading zone is never
     inside the world);
  3. `native_stack.install_workspace_cli(world, topology="container")`,
     `native_stack.stage_discovery(world, home=/home/engineer)`,
     `native_stack.sanitize_world_files(world)`;
  4. the prompt is composed NOW, from the host world, exactly as
     `agent_under_test.run` composes it (`build_prompt`, container shell),
     with the agent's HOME for the dbt profile path;
  5. the episode HOME: the world's `home/` plus the dbt profile re-rooted by
     `container_episode.reroot_profiles` (what `_copy_home` + `_stage_profiles`
     do, into /home/engineer instead of /root);
  6. `workspace/` moves out of the world root (the image symlinks
     /world/workspace -> /workspace), every `.git` index is normalised, and
     every host path is re-rooted (`/world`, `/workspace`, `/home/engineer`,
     the grading zone at /tests/episode/grading).

The result is a `StagedArm`: four directories and a manifest, all spelled in
container paths. The leak detector runs on the finished task directory.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import harness
from .constants import (AGENT_HOME, AUDIT_IN_CONTAINER, GRADING_IN_VERIFIER, REPO,
                        WORKSPACE_IN_CONTAINER, WORLD_IN_CONTAINER)
from .naming import ArmRef
from .reroot import EXPORT_MARKER, LeakError, Rerooter, iter_files

#: Where `taskspec.stage_fixtures` puts overlay trees inside a Linux container
#: with the default TMPDIR (the verifier re-stages there at grade time).
CANONICAL_STAGE_ROOT = "/tmp/alkera-fixtures"
#: The verifier's copy of the repository (tests/alkera/repo, package C).
REPO_IN_VERIFIER = "/tests/alkera/repo"
#: Where the image puts the services' gateway config (environment template).
GATEWAY_CONFIG_IN_CONTAINER = "/opt/alkera/gateway.json"
#: The gateway config's episode id. Constant and task-free on purpose: the
#: audit files are readable by the agent, and rollout.py stopped writing the
#: task name there because it hands over the defect mechanism.
GATEWAY_EPISODE_ID = "native-harbor"


class StageError(Exception):
    pass


@dataclass
class StagedArm:
    ref: ArmRef
    spec: object
    temp: Path
    world: dict                  # builder manifest, container paths
    world_dir: Path              # the world root, minus workspace/
    workspace_dir: Path
    home_dir: Path
    grading_dir: Path
    prompt: str                  # what build_prompt composed (no canary)
    shim_watch: str
    gateway_config: Path         # episode.write_gateway_config output, re-rooted
    records: dict = field(default_factory=dict)
    #: What Harbor's packager loses (`harbor publish` archives FILES only, every
    #: one mode 0644): container paths of empty directories and of executable
    #: files, for the image (world-up / world-seal) and the verifier to restore.
    restore: dict = field(default_factory=dict)

    def cleanup(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)


def _git_run(git_dir: Path, *args: str, work_tree: Path | None = None):
    """git with the exporter's own configuration shut out (no global/system
    config, no optional index writes) so the result never depends on it."""
    env = dict(os.environ)
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    argv = ["git", f"--git-dir={git_dir}"]
    if work_tree is not None:
        argv.append(f"--work-tree={work_tree}")
    argv += ["-c", "index.version=2", "-c", "core.untrackedCache=false",
             "-c", "core.fsmonitor=false", "-c", "feature.manyFiles=false", *args]
    return subprocess.run(argv, capture_output=True, text=True, env=env)


def _git(git_dir: Path, *args: str, work_tree: Path | None = None) -> str:
    r = _git_run(git_dir, *args, work_tree=work_tree)
    if r.returncode != 0:
        raise StageError(f"git {' '.join(args)} failed in {git_dir}: {r.stderr.strip()}")
    return r.stdout


def normalize_git(git_dir: Path) -> dict:
    """Make a builder's `.git` byte-reproducible without changing what git says.

    * The index carries stat data (ctime, inode, ...) of the export machine:
      it differs on every build and means nothing inside the image, where git
      re-stats the files anyway. It is rebuilt from its own tree with
      `git read-tree`, which keeps every entry (staged content included) and
      zeroes the stat fields, so `git status` reports exactly what it did.
    * `hooks/*.sample` are inert templates copied from the EXPORTER's git
      install; they vary with its version and are removed.
    """
    git_dir = Path(git_dir)
    work_tree = git_dir.parent
    record: dict = {"git_dir": git_dir.name}
    before = _git(git_dir, "status", "--porcelain=v1", "--untracked-files=all",
                  work_tree=work_tree)
    has_head = _git_run(git_dir, "rev-parse", "--verify", "-q", "HEAD").returncode == 0
    if (git_dir / "index").exists():
        staged_clean = has_head and _git_run(
            git_dir, "diff", "--cached", "--quiet", work_tree=work_tree).returncode == 0
        tree = "HEAD" if staged_clean else _git(git_dir, "write-tree").strip()
        _git(git_dir, "read-tree", tree)
        record["index"] = f"read-tree {'HEAD' if staged_clean else 'index tree'}"
    hooks = git_dir / "hooks"
    removed = sorted(p.name for p in hooks.glob("*.sample")) if hooks.is_dir() else []
    for name in removed:
        (hooks / name).unlink()
    record["sample_hooks_removed"] = len(removed)
    after = _git(git_dir, "status", "--porcelain=v1", "--untracked-files=all",
                 work_tree=work_tree)
    if before != after:
        raise StageError(f"normalising {git_dir} changed `git status`:\n"
                         f"before:\n{before}\nafter:\n{after}")
    record["status_lines"] = len(after.splitlines())
    return record


def _prepare(spec, log) -> float:
    """`episode.prepare_task`, retried once.

    Some task builders populate their shared fixture cache without a lock; a
    concurrent builder (another rollout, another export) can make the first
    attempt read a half-written cache. The builder is deterministic, so a
    second attempt after the other process finishes builds the same world.
    """
    m = harness.modules()
    started = time.time()
    try:
        m.episode.prepare_task(spec)
    except m.episode.HarnessError as exc:
        log(f"    prepare failed once, retrying in 10s: {str(exc).strip()[-300:]}")
        time.sleep(10)
        m.episode.prepare_task(spec)
    return round(time.time() - started, 1)


def stage_arm(ref: ArmRef, *, work_root: Path | None = None, log=print) -> StagedArm:
    """Build, stage and re-root one arm. The caller owns `.cleanup()`."""
    m = harness.modules()
    temp = Path(tempfile.mkdtemp(prefix=f"{EXPORT_MARKER}{ref.short_name}-",
                                 dir=str(work_root) if work_root else None))
    try:
        return _stage(ref, temp, m, log)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def _stage(ref: ArmRef, temp: Path, m, log) -> StagedArm:
    records: dict = {}
    rollout = temp / "rollout"
    world_root = rollout / "world"
    grading = rollout / "grading"
    rollout.mkdir(parents=True)
    with harness.variant_env(ref.arm):
        spec = m.taskspec.load(ref.task_id, variant=ref.arm)
        if spec.variant != ref.arm:
            raise StageError(f"{ref.selector}: taskspec resolved arm {spec.variant!r}")
        records["prepare_s"] = _prepare(spec, log)
        started = time.time()
        world = m.episode.build_world(spec, world_root, grading)
        records["build_s"] = round(time.time() - started, 1)

        # --- step 3: the container-native staging, in rollout.py's order
        cli = m.native_stack.install_workspace_cli(world, topology="container")
        records["workspace_cli"] = {k: cli[k] for k in ("cli", "topology",
                                                         "interpreter", "pythonpath")}
        discovery = m.native_stack.stage_discovery(world, home=AGENT_HOME)
        records["discovery"] = sorted(Path(f).relative_to(world_root).as_posix()
                                      for f in discovery["files"])
        records["sanitized"] = m.native_stack.sanitize_world_files(world)

    # --- step 4: the prompt, from the HOST world, as aut.run composes it
    prompt = m.aut.build_prompt(
        spec.instruction, {}, spec.artifact_in_world,
        spec.max_tool_calls, spec.wall_clock_seconds, "native",
        redteam=False, shell=False,
        workspace=world.get("workspace"), profiles_dir=world.get("profiles_dir"),
        container_shell=True, home=AGENT_HOME)

    warehouses = m.container_episode.warehouse_mounts(world)
    if not warehouses:
        raise StageError(f"{ref.selector}: the world declares no warehouse file "
                         f"under var/warehouse/; the container would have none")
    shim_watch = ":".join(f"{m.container_episode.WORLD_IN_CONTAINER}/{rel}"
                          for rel in warehouses)
    records["warehouses"] = warehouses

    # The services' gateway config, from the builder's MANIFEST (exactly what
    # native_stack.NativeStack._config_path writes on the host): serve.py's
    # fixed-layout default cannot see planes or a second warehouse a task
    # publishes only in its manifest (GT008's legacy warehouse).
    gateway_config = m.episode.write_gateway_config(
        rollout / "gateway.json", world, rollout / "audit", GATEWAY_EPISODE_ID,
        world.get("virtual_time") or "")

    wals = [p.relative_to(rollout).as_posix() for p in rollout.rglob("*.wal")]
    if wals:
        raise StageError(f"{ref.selector}: uncheckpointed DuckDB WAL files in the "
                         f"built world: {wals}")

    # --- step 5: the episode HOME (/home/engineer)
    home = rollout / "home"
    if (world_root / "home").is_dir():
        shutil.copytree(world_root / "home", home)
    else:
        home.mkdir()
    profiles = Path(world.get("profiles_dir") or "") / "profiles.yml"
    if world.get("profiles_dir") and profiles.is_file():
        rewritten, moved = m.container_episode.reroot_profiles(
            profiles.read_text(encoding="utf-8"), world_root)
        (home / ".dbt").mkdir(parents=True, exist_ok=True)
        with open(home / ".dbt" / "profiles.yml", "w", encoding="utf-8",
                  newline="") as stream:
            stream.write(rewritten)
        records["dbt_profiles_rerooted"] = moved
    else:
        records["dbt_profiles_rerooted"] = None

    # --- step 6: workspace out of the world, git normalised, paths re-rooted
    workspace = rollout / "workspace"
    host_workspace = Path(world["workspace"])
    if host_workspace.resolve() != (world_root / "workspace").resolve():
        raise StageError(f"{ref.selector}: the workspace is not <world>/workspace "
                         f"({host_workspace}); the container layout needs it there")
    shutil.move(str(world_root / "workspace"), str(workspace))
    records["git"] = [normalize_git(git_dir) for git_dir in
                      sorted(p for p in rollout.rglob(".git") if p.is_dir())]

    env_map = [
        (str(world_root / "workspace"), WORKSPACE_IN_CONTAINER),
        (str(workspace), WORKSPACE_IN_CONTAINER),
        (str(home), AGENT_HOME),
        (str(world_root), WORLD_IN_CONTAINER),
        (str(grading), GRADING_IN_VERIFIER),
        (str(rollout / "audit"), AUDIT_IN_CONTAINER),
    ]
    env_rerooter = Rerooter(env_map)
    grading_rerooter = Rerooter(env_map + [
        (str(REPO), REPO_IN_VERIFIER),
        (str(m.taskspec.STAGE_ROOT), CANONICAL_STAGE_ROOT),
    ])
    changed: dict[str, list[str]] = {}
    text = Path(gateway_config).read_text(encoding="utf-8")
    rerooted = env_rerooter.text(text)
    json.loads(rerooted)                                  # still valid JSON
    Path(gateway_config).write_text(rerooted, encoding="utf-8", newline="")
    changed["gateway_config"] = ["gateway.json"] if rerooted != text else []
    for label, directory, rerooter in (("world", world_root, env_rerooter),
                                       ("workspace", workspace, env_rerooter),
                                       ("home", home, env_rerooter),
                                       ("grading", grading, grading_rerooter)):
        try:
            changed[label] = rerooter.tree(directory)
        except LeakError as exc:
            raise StageError(f"{ref.selector}: {label}/{exc}") from None
    records["rerooted_files"] = changed
    restore = restore_lists([(world_root, WORLD_IN_CONTAINER),
                             (workspace, WORKSPACE_IN_CONTAINER),
                             (home, AGENT_HOME),
                             (grading, GRADING_IN_VERIFIER)])
    container_world = env_rerooter.obj(world)
    leftover = [n for n in env_rerooter.needles + [str(temp), EXPORT_MARKER]
                if n in json.dumps(container_world)]
    if leftover:
        raise StageError(f"{ref.selector}: the world manifest still names host "
                         f"paths after re-rooting: {leftover}")
    return StagedArm(ref=ref, spec=spec, temp=temp, world=container_world,
                     world_dir=world_root, workspace_dir=workspace, home_dir=home,
                     grading_dir=grading, prompt=prompt, shim_watch=shim_watch,
                     gateway_config=Path(gateway_config), records=records,
                     restore=restore)


def restore_lists(roots: list[tuple[Path, str]]) -> dict:
    """Empty directories and executable files under each staged root, spelled
    as container paths. `.git` internals are left out of the empty list (git
    recreates `refs/tags`, `objects/pack` ... on demand)."""
    empty: list[str] = []
    executable: list[str] = []
    for root, prefix in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            rel = here.relative_to(root).as_posix()
            spelled = prefix if rel == "." else f"{prefix}/{rel}"
            if ".git" not in here.relative_to(root).parts \
                    and not dirnames and not filenames and rel != ".":
                empty.append(spelled)
            for name in filenames:
                if os.stat(here / name).st_mode & 0o111:
                    executable.append(f"{spelled}/{name}")
    return {"empty_dirs": sorted(empty), "executable_files": sorted(executable)}


def file_count_and_bytes(root: Path) -> tuple[int, int]:
    files = iter_files(root)
    return len(files), sum(p.stat().st_size for p in files)
