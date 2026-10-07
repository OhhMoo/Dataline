"""AlkeraHarborAdapter: every (task, arm) -> one Harbor task directory.

Harbor's adapter pattern (`adapters/<name>/src/<pkg>/adapter.py`):
`AlkeraHarborAdapter(output_dir, limit, overwrite, task_ids, **kw).run()`.

Per arm (see worldbuild.py for the world itself):

    <out>/<task-id-lowercase>__<arm>/
      task.toml  instruction.md  README.md
      environment/  template (minus context.txt) + alkera/ (context) +
                    episode/{world,workspace,home,episode.json}
      tests/        template (minus context.txt) + alkera/ (context, laid out
                    as the repo) + episode/{episode.json,grading,
                    before/workspace,baselines.json}
      solution/     tasks/<T>/solution/* (solve.sh = the native oracle, or a
                    stub that fails loudly when it is not ported yet)

A task is assembled in `<out>/.staging/` and renamed into place only after
every release gate passed, so `harbor run -p <out>` never sees a half-written
task. Then `<out>/dataset.toml` (digests), `<out>/manifest.json` and
`<out>/README.md` are rewritten to describe every task present in `<out>`.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import __version__, checks, digest, dockerenv, harness, render, tomlw
from .constants import (AGENT_NETWORK_MODES, CANARY_HASH, CONTEXT_FILE,
                        DEFAULT_DATASET_NAME, DEFAULT_DATASET_VERSION, MANIFEST_SCHEMA,
                        ORG, REPO, REWARD_MODES, TASKS_DIR, TEMPLATE_DIR)
from .context import (ALWAYS_EXCLUDE, ContextEntry, GitView, copy_file, matches,
                      parse_context, resolve_context, resolve_entries)
from .naming import ArmRef, check_unique, select
from .reroot import iter_files, needles_for, scan
from .variant import UNPROFILED, Variant, render_dockerfile, render_tests_dockerfile
from .worldbuild import (CANONICAL_STAGE_ROOT, GATEWAY_CONFIG_IN_CONTAINER,
                         StagedArm, stage_arm)

STAGING_DIR = ".staging"
#: Top-level names the generator writes into environment/ and tests/; a
#: template may not ship its own.
RESERVED_TEMPLATE_NAMES = ("alkera", "episode")


class ExportError(Exception):
    pass


@dataclass
class TaskResult:
    selector: str
    name: str
    status: str                       # ok | skipped | error
    reason: str = ""
    role: str | None = None
    oracle: str | None = None
    anchors: str | None = None
    digest: str | None = None
    files: int = 0
    bytes: int = 0
    seconds: float = 0.0
    instruction_sha256: str | None = None
    validation: dict | None = None
    #: The agent image's variant key (variant.Variant.key)
    variant: str | None = None


@dataclass
class ExportReport:
    output_dir: str
    results: list[TaskResult] = field(default_factory=list)
    dataset_toml: str | None = None
    seconds: float = 0.0
    dataset_problems: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[TaskResult]:
        return [r for r in self.results if r.status == "error"]


def _git_out(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def source_state() -> tuple[str, bool]:
    """(HEAD sha, working tree dirty?) of the repository being exported."""
    sha = _git_out("rev-parse", "HEAD") or "unknown"
    dirty = bool(_git_out("status", "--porcelain", "--untracked-files=no"))
    return sha, dirty


def _normalize_modes(root: Path) -> None:
    """Directories 0755, files 0644 or 0755 (any exec bit): deterministic image
    layers whatever the builder's umask was."""
    for dirpath, _dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            path = Path(dirpath) / name
            mode = os.stat(path).st_mode
            os.chmod(path, 0o755 if mode & 0o111 else 0o644)


def _move_tree(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))


def copy_template(src: Path, dest: Path, *, repo: Path | None = REPO,
                  keep_context: bool = False) -> list[str]:
    """Copy a template directory verbatim, minus its context.txt.

    `__pycache__/`, `*.pyc` and the rest of the ALWAYS list are skipped. A
    template file that git ignores is an ERROR, not a skip: it would be missing
    from a clean checkout, so an export from the commit would differ from this
    one. `repo=None` skips that check (copying from a snapshot already made).
    `keep_context` keeps context.txt (used to take the snapshot itself).
    """
    src = Path(src)
    if not src.is_dir():
        raise ExportError(f"template {src} does not exist")
    src_rel = None
    if repo is not None:
        try:
            src_rel = src.resolve().relative_to(Path(repo).resolve()).as_posix()
        except ValueError:
            src_rel = None                   # a template outside the repo (tests)
    files = []
    for path in iter_files(src):
        rel = path.relative_to(src).as_posix()
        if (rel == CONTEXT_FILE and not keep_context) or matches(rel, ALWAYS_EXCLUDE):
            continue
        if rel.split("/", 1)[0] in RESERVED_TEMPLATE_NAMES:
            raise ExportError(f"template {src} ships {rel}; "
                              f"{'/'.join(RESERVED_TEMPLATE_NAMES)} are written by "
                              f"the generator")
        files.append((path, rel))
    if src_rel is not None:
        ignored = GitView(repo).ignored(f"{src_rel}/{rel}" for _p, rel in files)
        if ignored:
            raise ExportError(f"template file(s) {sorted(ignored)} are git-ignored; "
                              f"they would not exist in a clean checkout")
    for path, rel in files:
        copy_file(path, dest / rel)
    return [rel for _p, rel in files]


class AlkeraHarborAdapter:
    """Generate Harbor tasks for every (task, arm) of the Alkera benchmark."""

    def __init__(self, output_dir: Path, limit: int | None = None,
                 overwrite: bool = False, task_ids: list[str] | None = None, *,
                 reward_mode: str = "pass", agent_network: str = "no-network",
                 dataset_name: str = DEFAULT_DATASET_NAME,
                 dataset_version: str = DEFAULT_DATASET_VERSION,
                 template_dir: Path = TEMPLATE_DIR, tasks_dir: Path = TASKS_DIR,
                 work_dir: Path | None = None, validate: bool = False,
                 harbor_python: str | None = None, log=None, **kwargs):
        if reward_mode not in REWARD_MODES:
            raise ExportError(f"--reward must be one of {REWARD_MODES}")
        if agent_network not in AGENT_NETWORK_MODES:
            raise ExportError(f"--agent-network must be one of {AGENT_NETWORK_MODES}")
        if limit is not None and limit < 1:
            raise ExportError("--limit must be >= 1")
        self.output_dir = Path(output_dir).resolve()
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.reward_mode = reward_mode
        self.agent_network = agent_network
        self.dataset_name = dataset_name
        self.dataset_version = dataset_version
        self.template_dir = Path(template_dir)
        self.tasks_dir = Path(tasks_dir)
        self.work_dir = Path(work_dir) if work_dir else None
        self.validate = validate
        self.harbor_python = harbor_python
        self.log = log or _print_flush
        self.extra = kwargs
        self._fingerprints: dict[str, str] | None = None
        self._source: tuple[str, bool] | None = None
        #: The templates every task of one run is built from. `run()` points it
        #: at a snapshot taken once, so a template edited mid-export (packages B
        #: and C work concurrently) cannot give two tasks different bytes.
        self._templates: Path = self.template_dir
        self._templates_snapshot = False
        #: variant key -> the image's ENV minus PATH (static_env)
        self._static_env: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------ discovery
    def available(self) -> list[ArmRef]:
        refs = [ArmRef(task, arm) for task in harness.task_ids(self.tasks_dir)
                for arm in harness.arms_of(task, self.tasks_dir)]
        check_unique(refs)
        return sorted(refs)

    def get_all_task_ids(self) -> list[str]:
        """Every exportable unit, as TASK:ARM (the `--task-ids` spelling)."""
        return [ref.selector for ref in self.available()]

    def selected(self) -> list[ArmRef]:
        refs = select(self.available(), self.task_ids)
        return refs[: self.limit] if self.limit else refs

    def describe(self, ref: ArmRef) -> dict:
        spec = harness.load_spec(ref.task_id, ref.arm, self.tasks_dir)
        root = self.tasks_dir / ref.task_id
        return {
            "selector": ref.selector, "name": ref.harbor_name,
            "role": harness.role_of(spec),
            "brief": spec.instruction_path.relative_to(root).as_posix(),
            "oracle": "native" if (root / "solution" / "solve.sh").is_file() else "missing",
            "anchors": "present" if (root / "tests" / "baselines"
                                     / f"{ref.arm}.json").is_file() else "missing",
        }

    @property
    def source(self) -> tuple[str, bool]:
        if self._source is None:
            self._source = source_state()
        return self._source

    @property
    def fingerprints(self) -> dict[str, str]:
        if self._fingerprints is None:
            self._fingerprints = checks.grading_fingerprints(self.tasks_dir)
        return self._fingerprints

    # ------------------------------------------------------------ one task
    def generate_task(self, ref: ArmRef, dest: Path) -> TaskResult:
        """Write the task for `ref` into `dest` (which must not exist)."""
        started = time.time()
        staged = stage_arm(ref, work_root=self.work_dir, log=self.log)
        try:
            result = self._write_task(ref, staged, Path(dest))
        finally:
            staged.cleanup()
        result.seconds = round(time.time() - started, 1)
        return result

    def _write_task(self, ref: ArmRef, staged: StagedArm, out: Path) -> TaskResult:
        spec = staged.spec
        role = harness.role_of(spec)
        variant = staged.variant
        task_root = self.tasks_dir / ref.task_id
        commit, dirty = self.source
        out.mkdir(parents=True)

        # ---- solution/ (first: the oracle status goes into task.toml)
        oracle = self._write_solution(ref, task_root, out / "solution")

        # ---- environment/: the template verbatim, its Dockerfile rendered for
        # a profiled variant (variant.py), and the context plus the variant's
        # extra locks
        env_dir = out / "environment"
        repo_check = None if self._templates_snapshot else REPO
        copy_template(self._templates / "environment", env_dir, repo=repo_check)
        if variant.profiled:
            (env_dir / "Dockerfile").write_text(self.dockerfile(variant),
                                                encoding="utf-8", newline="")
        self._environment_context(variant, ref.task_id, env_dir / "alkera")
        episode_dir = env_dir / "episode"
        _move_tree(staged.world_dir, episode_dir / "world")
        shutil.copytree(staged.workspace_dir, episode_dir / "workspace", symlinks=True)
        _move_tree(staged.home_dir, episode_dir / "home")
        shutil.copyfile(staged.gateway_config, episode_dir / "gateway.json")

        # ---- tests/
        tests_dir = out / "tests"
        copy_template(self._templates / "tests", tests_dir, repo=repo_check)
        if variant.profiled:
            (tests_dir / "Dockerfile").write_text(self.tests_dockerfile(variant),
                                                  encoding="utf-8", newline="")
        brief = spec.instruction_path.resolve().relative_to(REPO).as_posix()
        resolve_context(self._templates / "tests" / CONTEXT_FILE,
                        tests_dir / "alkera", repo=REPO, task=ref.task_id,
                        repo_excludes=("tasks/*/solution/", "tasks/*/prompts/"),
                        force_include=(brief,))
        verifier_episode = tests_dir / "episode"
        _move_tree(staged.grading_dir, verifier_episode / "grading")
        _move_tree(staged.workspace_dir, verifier_episode / "before" / "workspace")
        baselines = task_root / "tests" / "baselines" / f"{ref.arm}.json"
        anchors = "present" if baselines.is_file() else "missing"
        (verifier_episode / "baselines.json").write_bytes(
            baselines.read_bytes() if baselines.is_file() else b"{}\n")

        # ---- the env both sides agree on: the per-arm variables (episode.json),
        # and in task.toml also the image's ENV minus PATH, for providers that
        # apply Dockerfile ENV at build time only (dockerenv.py)
        env = {"ALKERA_TASK_VARIANT": ref.arm, "ALKERA_SHIM_WATCH": staged.shim_watch}
        if variant.profiled and staged.clock:
            # the world's clock for the rows the agent's shims write, as the
            # measured container overlay sets it (dockerenv.PER_ARM_KEYS)
            env["ALKERA_VIRTUAL_TIME"] = staged.clock
        document = render.task_document(
            ref=ref, spec=spec, role=role, reward_mode=self.reward_mode,
            agent_network=self.agent_network, env={**self.static_env(variant), **env},
            source_commit=commit, source_dirty=dirty, oracle=oracle, variant=variant)
        task_toml = render.task_toml(document)
        (out / "task.toml").write_text(task_toml, encoding="utf-8", newline="")
        episode = render.episode_json(render.episode_document(
            ref=ref, spec=spec, role=role, reward_mode=self.reward_mode,
            world=staged.world, env=env, source_commit=commit, source_dirty=dirty,
            task_toml_sha256=hashlib.sha256(
                (task_root / "task.toml").read_bytes()).hexdigest(),
            anchors=anchors, oracle=oracle, restore=staged.restore,
            gateway_config=GATEWAY_CONFIG_IN_CONTAINER,
            # the supervised backends (environment runtime): none yet for any
            # profile the image serves; the key is there so the contract is
            backends=[] if variant.profiled else None))
        for path in (episode_dir / "episode.json", verifier_episode / "episode.json"):
            path.write_text(episode, encoding="utf-8", newline="")

        (out / "instruction.md").write_text(render.instruction_md(staged.prompt),
                                            encoding="utf-8", newline="")
        (out / "README.md").write_text(render.readme(
            ref=ref, spec=spec, role=role, reward_mode=self.reward_mode,
            oracle=oracle, anchors=anchors, agent_network=self.agent_network,
            n_checks=_count_checks(task_root, spec), variant=variant),
            encoding="utf-8", newline="")
        _normalize_modes(out)

        # ---- release gates
        problems: list[str] = []
        problems += checks.canary_problems(out)
        problems += checks.dockerfile_problems(out)
        problems += checks.environment_problems(out, self.fingerprints,
                                                verifier_episode / "grading")
        problems += checks.unpublishable_problems(out)
        problems += checks.payload_problems(out)
        problems += checks.environment_env_problems(out)
        problems += scan(out, self._needles(staged))
        problems = list(dict.fromkeys(problems))
        if problems:
            raise ExportError("release gates failed:\n  " + "\n  ".join(problems[:40])
                              + (f"\n  ... {len(problems) - 40} more"
                                 if len(problems) > 40 else ""))
        files = iter_files(out)
        return TaskResult(
            selector=ref.selector, name=ref.harbor_name, status="ok", role=role,
            oracle=oracle, anchors=anchors, digest=digest.task_digest(out),
            files=len(files), bytes=sum(p.stat().st_size for p in files),
            instruction_sha256=hashlib.sha256(staged.prompt.encode()).hexdigest(),
            variant=variant.key)

    def _environment_context(self, variant: Variant, task_id: str, dest: Path) -> None:
        """environment/alkera/: the template's context.txt, plus the locks a
        profiled variant's Dockerfile COPYs (Variant.context_files), resolved
        together (so two entries can never write one file)."""
        context = self._templates / "environment" / CONTEXT_FILE
        if not variant.profiled:
            resolve_context(context, dest, repo=REPO, task=task_id,
                            forbid=_forbid_in_environment)
            return
        entries = parse_context(context.read_text(encoding="utf-8"), task=task_id,
                                origin=str(context))
        entries += [ContextEntry(origin=f"variant {variant.key}", src=rel, dest=rel)
                    for rel in variant.context_files()]
        resolve_entries(entries, dest, repo=REPO, forbid=_forbid_in_environment)

    def _needles(self, staged: StagedArm):
        m = harness.modules()
        extra = []
        if str(m.taskspec.STAGE_ROOT) != CANONICAL_STAGE_ROOT:
            extra.append(("fixture stage root", m.taskspec.STAGE_ROOT))
        return needles_for(export_dirs=[staged.temp], repo=REPO, home=Path.home(),
                           extra=extra)

    def _write_solution(self, ref: ArmRef, task_root: Path, dest: Path) -> str:
        source = task_root / "solution"
        dest.mkdir(parents=True)
        if source.is_dir():
            files = [(path, path.relative_to(source).as_posix(),
                      path.relative_to(REPO).as_posix()) for path in iter_files(source)]
            files = [f for f in files if not matches(f[1], ALWAYS_EXCLUDE)]
            ignored = GitView(REPO).ignored(repo_rel for _p, _r, repo_rel in files)
            for path, rel, repo_rel in files:
                if repo_rel not in ignored:
                    copy_file(path, dest / rel)
        solve = dest / "solve.sh"
        if solve.is_file():
            text = solve.read_text(encoding="utf-8")
            solve.write_text(render.with_canary(text), encoding="utf-8", newline="")
            oracle = "native"
        else:
            solve.write_text(render.solve_stub(ref), encoding="utf-8", newline="")
            oracle = "missing"
        os.chmod(solve, 0o755)
        return oracle

    # ------------------------------------------------------------ the run
    def run(self) -> ExportReport:
        started = time.time()
        report = ExportReport(output_dir=str(self.output_dir))
        refs = self.selected()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        staging_root = self.output_dir / STAGING_DIR
        commit, dirty = self.source
        self.log(f"exporting {len(refs)} arm(s) from {commit[:12]}"
                 f"{' (dirty tree)' if dirty else ''} -> {self.output_dir}"
                 f" [reward={self.reward_mode}, agent network={self.agent_network}]")
        try:
            self._snapshot_templates(staging_root / ".templates")
        except ExportError as exc:
            report.dataset_problems.append(str(exc))
            refs = []
        for index, ref in enumerate(refs, start=1):
            final = self.output_dir / ref.short_name
            prefix = f"[{index}/{len(refs)}] {ref.selector}"
            if final.exists() and not self.overwrite:
                self.log(f"{prefix}: skipped (exists; pass --overwrite)")
                report.results.append(TaskResult(ref.selector, ref.harbor_name,
                                                 "skipped", "exists"))
                continue
            staging = staging_root / ref.short_name
            shutil.rmtree(staging, ignore_errors=True)
            try:
                result = self.generate_task(ref, staging)
            except Exception as exc:                     # noqa: BLE001 - per-arm
                # one arm's failure is recorded and the export goes on; the
                # exit status reports it (spider2-dbt's error_count pattern)
                shutil.rmtree(staging, ignore_errors=True)
                reason = f"{type(exc).__name__}: {exc}"
                self.log(f"{prefix}: ERROR {reason}")
                report.results.append(TaskResult(ref.selector, ref.harbor_name,
                                                 "error", reason))
                continue
            if final.exists():
                shutil.rmtree(final)
            staging.rename(final)
            self.log(f"{prefix}: ok {result.digest[:19]} oracle={result.oracle} "
                     f"{result.bytes / 1e6:.1f} MB {result.files} files "
                     f"{result.seconds}s")
            report.results.append(result)
        shutil.rmtree(staging_root, ignore_errors=True)
        self._templates, self._templates_snapshot = self.template_dir, False
        self._static_env = {}
        self._check_static_parts(report)

        tasks = self.collect_existing()
        report.dataset_toml = str(self.write_dataset_files(tasks))
        if self.validate:
            self._validate(report, tasks)
        report.seconds = round(time.time() - started, 1)
        return report

    def _snapshot_templates(self, root: Path) -> None:
        """Copy both templates once (ignore check included); every task of
        this run is then built from the copy."""
        shutil.rmtree(root, ignore_errors=True)
        for part in ("environment", "tests"):
            copy_template(self.template_dir / part, root / part, keep_context=True)
        self._templates, self._templates_snapshot = root, True
        self._static_env = {}
        self.static_env()

    def dockerfile(self, variant: Variant = UNPROFILED) -> str:
        """The agent Dockerfile for `variant`: the template's bytes for the
        unprofiled tasks, rendered by variant.render_dockerfile otherwise."""
        path = self._templates / "environment" / "Dockerfile"
        try:
            text = path.read_text(encoding="utf-8")
            return render_dockerfile(text, variant, dockerenv.parse_env(text))
        except (OSError, ValueError) as exc:
            raise ExportError(f"{path} ({variant.key}): {exc}") from None

    def tests_dockerfile(self, variant: Variant = UNPROFILED) -> str:
        """The verifier Dockerfile for `variant` (variant.render_tests_dockerfile):
        the template's bytes for the unprofiled tasks."""
        path = self._templates / "tests" / "Dockerfile"
        try:
            return render_tests_dockerfile(
                path.read_text(encoding="utf-8"), variant,
                (self._templates / "environment" / "Dockerfile").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ExportError(f"{path} ({variant.key}): {exc}") from None

    def static_env(self, variant: Variant = UNPROFILED) -> dict[str, str]:
        """The image's ENV minus `PATH` (dockerenv.NOT_MIRRORED), parsed from
        the variant's Dockerfile (dockerenv.py)."""
        if variant.key not in self._static_env:
            try:
                self._static_env[variant.key] = dockerenv.static_agent_env(
                    self.dockerfile(variant))
            except dockerenv.DockerEnvError as exc:
                raise ExportError(f"the {variant.key} Dockerfile: {exc}") from None
        return self._static_env[variant.key]

    def _check_static_parts(self, report: ExportReport) -> None:
        """Every task of one run and one variant must share its task-independent
        bytes (the templates, the variant's Dockerfile and the context
        sources); a source edited mid-run breaks that, and the run then fails
        rather than ship a mixed dataset."""
        groups: dict[str, dict[str, list[str]]] = {}
        for r in report.results:
            if r.status != "ok":
                continue
            short = r.name.split("/", 1)[1]
            task_id = r.selector.split(":", 1)[0]
            fp = checks.static_fingerprint(self.output_dir / short, task_id)
            groups.setdefault(r.variant or UNPROFILED.key, {}).setdefault(
                fp, []).append(short)
        for key, versions in sorted(groups.items()):
            if len(versions) > 1:
                sizes = sorted((len(v), v[0]) for v in versions.values())
                report.dataset_problems.append(
                    f"task-independent sources (templates, context sources) changed "
                    f"while the export ran: {len(versions)} different versions across "
                    f"this run's {key} tasks (e.g. {sizes}); re-run with --overwrite "
                    f"on a quiet tree")

    # ------------------------------------------------------------ dataset level
    def collect_existing(self) -> list[dict]:
        """Every generated task present in the output directory, digested now
        (so dataset.toml always describes the directory `harbor run -p` runs)."""
        found = []
        for task_dir in sorted(p for p in self.output_dir.iterdir()
                               if p.is_dir() and (p / "task.toml").is_file()):
            raw = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
            name = (raw.get("task") or {}).get("name", "")
            if not name.startswith(f"{ORG}/"):
                continue
            meta = raw.get("metadata") or {}
            found.append({
                "name": name, "dir": task_dir.name,
                "task": meta.get("alkera_task"), "arm": meta.get("alkera_arm"),
                "role": meta.get("alkera_role"), "oracle": meta.get("oracle"),
                "profile": meta.get("alkera_profile"),
                "reward_mode": meta.get("reward_mode"),
                "source_commit": meta.get("source_commit"),
                "source_dirty": meta.get("source_dirty"),
                "anchors": _anchors_of(task_dir),
                "digest": digest.task_digest(task_dir),
            })
        return found

    def write_dataset_files(self, tasks: list[dict]) -> Path:
        commit, dirty = self.source
        modes = sorted({t["reward_mode"] for t in tasks if t["reward_mode"]})
        document = {
            "dataset": {
                "name": self.dataset_name,
                "version": self.dataset_version,
                "description": (
                    "Alkera data-engineering benchmark: on-call incidents in a "
                    "closed production data stack (DuckDB warehouse, dbt, git, "
                    "and the internal services each task's box plugs: tracker, "
                    "chat, wiki, BI, scheduler ...), one Harbor task per "
                    "(task, context arm); a separate deterministic verifier "
                    "grades the end state of the world."),
                "authors": [{"name": render.AUTHOR}],
                "keywords": ["data-engineering", "duckdb", "dbt", "agentic",
                             "incident-response"],
            },
            "tasks": [{"name": t["name"], "digest": t["digest"]} for t in tasks],
        }
        path = self.output_dir / "dataset.toml"
        path.write_text(tomlw.dumps(document, header=[
            CANARY_HASH,
            f"# Generated by harbor/export.py (alkera_harbor {__version__}) from "
            f"{commit}{' (dirty tree)' if dirty else ''}.",
            "# Digests are Harbor's Packager.compute_content_hash; regenerate, "
            "never hand-edit.", ""], inline=("dataset.authors",)),
            encoding="utf-8", newline="")
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "dataset": {"name": self.dataset_name, "version": self.dataset_version},
            "generator": {"name": "alkera_harbor", "version": __version__},
            "exported_at": _dt.datetime.now(_dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "source_commit": commit,
            "source_dirty": dirty,
            "reward_modes": modes,
            "tasks": tasks,
        }
        (self.output_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="")
        (self.output_dir / "README.md").write_text(
            _dataset_readme(self.dataset_name, self.dataset_version, tasks, commit,
                            dirty, modes), encoding="utf-8", newline="")
        return path

    # ------------------------------------------------------------ validation
    def _validate(self, report: ExportReport, tasks: list[dict]) -> None:
        from .validate import ValidationError, validate_dataset, validate_tasks
        try:
            seen = validate_dataset(Path(report.dataset_toml),
                                    harbor_python=self.harbor_python)
            ours = [{"name": t["name"], "digest": t["digest"]} for t in tasks]
            if seen["tasks"] != ours or seen["name"] != self.dataset_name:
                report.dataset_problems.append(
                    "dataset.toml as Harbor reads it differs from the tasks on disk")
            else:
                self.log(f"dataset.toml: {len(ours)} task(s), accepted by Harbor's "
                         f"DatasetManifest (content hash {seen['content_hash'][:12]})")
        except ValidationError as exc:
            report.dataset_problems.append(str(exc))
        by_dir = {t["dir"]: t for t in tasks}
        dirs = [self.output_dir / r.name.split("/", 1)[1] for r in report.results
                if r.status == "ok"]
        if not dirs:
            return
        try:
            verdicts = validate_tasks(
                dirs, harbor_python=self.harbor_python,
                instruction_sha256={r.name.split("/", 1)[1]: r.instruction_sha256
                                    for r in report.results if r.status == "ok"})
        except ValidationError as exc:
            for r in report.results:
                if r.status == "ok":
                    r.status, r.reason = "error", f"validation unavailable: {exc}"
            return
        for r in report.results:
            if r.status != "ok":
                continue
            short = r.name.split("/", 1)[1]
            verdict = verdicts.get(short) or {"ok": False, "error": "no verdict"}
            r.validation = verdict
            ours = by_dir.get(short, {}).get("digest")
            problems = list(verdict.get("problems") or [])
            if not verdict.get("ok"):
                problems.append(verdict.get("error", "harbor could not load the task"))
            elif verdict.get("digest") != ours:
                problems.append(f"digest mismatch: harbor {verdict.get('digest')} "
                                f"vs ours {ours}")
            if problems:
                r.status, r.reason = "error", "validation: " + "; ".join(problems)
                self.log(f"{r.selector}: VALIDATION FAILED {r.reason}")
            else:
                self.log(f"{r.selector}: validated by harbor "
                         f"{verdict.get('harbor_version')} (digest equal)")


def _print_flush(*args) -> None:
    print(*args, flush=True)


def _forbid_in_environment(repo_rel: str) -> str | None:
    """The agent image never carries any task package file (GT*, BI*, ...:
    harness.is_task_package_name)."""
    parts = repo_rel.split("/")
    if len(parts) >= 2 and parts[0] == "tasks" and harness.is_task_package_name(parts[1]):
        return ("task package files (graders, fixtures, oracles, briefs) never "
                "enter the agent environment")
    return None


def _anchors_of(task_dir: Path) -> str | None:
    path = task_dir / "tests" / "episode" / "episode.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("anchors")
    except (OSError, ValueError):
        return None


def _count_checks(task_root: Path, spec) -> int | None:
    manifest = spec.grade_manifest
    if not manifest or not Path(manifest).is_file():
        return None
    try:
        import yaml
        doc = yaml.safe_load(Path(manifest).read_text(encoding="utf-8")) or {}
    except Exception:                                   # noqa: BLE001 - optional
        return None
    rows = doc.get("checks") if isinstance(doc, dict) else None
    return len(rows) if isinstance(rows, list) else None


def _dataset_readme(name, version, tasks, commit, dirty, modes) -> str:
    rows = "\n".join(
        f"| `{t['dir']}` | `{t['task']}` | `{t['arm']}` | {t['role']} | "
        f"{t['oracle']} | `{(t['digest'] or '')[:19]}` |" for t in tasks)
    missing = [t["dir"] for t in tasks if t["oracle"] != "native"]
    oracle_note = ("Every task ships a native oracle." if not missing else
                   f"{len(missing)} task(s) do not ship a native oracle yet; their "
                   f"`solution/solve.sh` exits 1 so `-a oracle` reports them as "
                   f"failures instead of passes: " + ", ".join(f"`{d}`" for d in missing)
                   + ".")
    return "\n".join([
        f"# {name} {version}",
        "",
        "On-call data-engineering incidents in a closed, simulated production "
        "stack. Each task is one context arm of a source task: the agent works a "
        "ticket at a real shell (DuckDB warehouse, dbt, git and the internal "
        "services its box plugs, which it discovers from its environment: the "
        "nine on-call services for the GT tasks, a profile's own set - Power BI, "
        "Tableau, Sigma, the tracker, chat and wiki for the BI tasks) and is "
        "graded on the end state of the world plus the report it leaves (for a "
        "BI task, also what each vendor's service published).",
        "",
        f"Generated by `harbor/export.py` from `{commit}`"
        f"{' (dirty tree)' if dirty else ''}; reward mode(s): "
        f"{', '.join(modes) or 'n/a'}. Canary: `{render.CANARY_TEXT}`.",
        "",
        "## Run",
        "",
        "```bash",
        "harbor run -p . -a oracle            # every native oracle must score 1",
        "harbor run -p . -a nop               # doing nothing must score 0",
        "harbor run -p . -a claude-code -m anthropic/<model> \\",
        "    --allow-agent-host api.anthropic.com",
        "```",
        "",
        "Agents run as the unprivileged user `engineer` with no network except "
        "the hosts the operator allows; the world's services run inside the same "
        "container on 127.0.0.1. A separate verifier container regrades from the "
        "collected `/world`, `/workspace` and `/audit` artifacts; it never needs "
        "the network.",
        "",
        "## Oracle status",
        "",
        oracle_note,
        "",
        "## Tasks",
        "",
        "| directory | source task | arm | role | oracle | digest |",
        "|---|---|---|---|---|---|",
        rows,
        "",
        "`manifest.json` lists the same tasks with their digests, reward mode and "
        "provenance. Each task's own README.md explains its outcome classes and "
        "pass criterion.",
        "",
    ])


def report_json(report: ExportReport) -> str:
    return json.dumps({"output_dir": report.output_dir, "seconds": report.seconds,
                       "dataset_toml": report.dataset_toml,
                       "dataset_problems": report.dataset_problems,
                       "results": [asdict(r) for r in report.results]}, indent=2)
