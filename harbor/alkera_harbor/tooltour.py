"""The tool tour as a Harbor dataset: one Harbor task per tool-tour Probe.

    gateway/.venv/bin/python harbor/export.py --tooltour \\
        --output-dir harbor/dist/alkera-tooltour [--tools duckdb,sched] \\
        [--overwrite] [--validate] [--work-dir DIR]

A Probe (tasks/harness/tool_tour_core.py) is one fact an engineer reads
through one tool's real client: February 2026 Net Revenue through DuckDB's
CLI, a Jira ticket's status through `jira`, ... The container tour
(tasks/harness/tool_tour.py) runs each probe's own command; here each becomes
a Harbor task an agent must solve itself:

  instruction.md   the probe's `instruction` + ANSWER_LINE. Never the command,
                   never the answer.
  solution/        probe.sh is the probe's `command`, verbatim; solve.sh runs
                   it and writes its final JSON line to ANSWER_PATH (the
                   oracle).
  environment/     the probe's profile and plugs through Task 4's variant
                   machinery - the variant's Dockerfile (variant.py), its
                   context, the image runtime - and the episode payload staged
                   by worldbuild._stage, the code that stages every task, from
                   the tour's world (tooltour_world.py: commerce_analytics at
                   seed 11, exactly as the container tour builds it).
  tests/           the task verifier's Dockerfile and verify.py, verbatim (one
                   image, shared cache; verify.py is imported for its audit
                   reader and its unprivileged grader), plus
                   harbor/template/tooltour_tests/ (verify_tool.py, test.sh,
                   tool_tour_core.py through its context.txt) and episode/:
                   probe.json, episode.json and sealed/world/, the SEALED
                   pre-agent copy of the world truth_sql runs on. verify_tool.py
                   explains the reward.

SKIPPED, BY NAME. A probe the current tree cannot serve in Harbor is not
exported: either its variant is refused (variant.variant_of /
backend_specs: a warehouse or hosted backend the Harbor image has no process
for yet - plan Task 10 lifts those; re-export after it merges), or it is a
KNOWN finding of the container tour (its module's KNOWN: the probe's own
command fails, for a cause owned elsewhere, so the oracle could not pass).
manifest.json `skipped` names each probe with its reason.

THE LEAK GATE (leak_problems, a release gate of every task, beside Task 4's):
the verifier's per-probe material - probe.json, the sealed copy, the shipped
core - never reaches environment/, and the probe's truth, read from the
sealed copy at export, is not spelled out anywhere the agent's image is built
from (see truth_needles for which values can be told apart from noise).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace

from . import __version__, checks, digest, harness, render, tomlw, worldbuild
from .adapter import (AlkeraHarborAdapter, ExportError, TaskResult, _move_tree,
                      _normalize_modes, copy_template, report_json)
from .constants import (AGENT_NETWORK_MODES, CANARY_HASH, CONTEXT_FILE, MANIFEST_SCHEMA,
                        REPO, TEMPLATE_DIR)
from .context import ContextError, resolve_context
from .naming import ArmRef, NamingError, check_unique, select
from .reroot import EXPORT_MARKER, iter_files, scan
from .tooltour_world import PLUGS_ENV, PROFILE_ENV, TOUR_SEED
from .variant import VariantError, backend_specs, variant_of
from .worldbuild import GATEWAY_CONFIG_IN_CONTAINER, StagedArm

#: Every probe is one task with one arm: `alkera/<probe id>__tour`.
TOUR_ARM = "tour"
#: role in task.toml / episode.json: neither a control nor a decision arm
TOUR_ROLE = "tour"
DATASET_NAME = "alkera/tool-tour"
DATASET_VERSION = "0.1.0"
TOUR_TASK_VERSION = "0.1.0"
ANSWER_PATH = "/workspace/answer.json"
ANSWER_LINE = 'When done, write {"value": <answer>} to /workspace/answer.json.'
#: The verifier template of a tool-tour task (verify_tool.py, test.sh, context.txt).
TOOLTOUR_TEMPLATE = "tooltour_tests"
#: From the task verifier template, verbatim: its image and its helpers.
SHARED_TESTS_FILES = ("Dockerfile", "verify.py")
PROBE_SCHEMA = "alkera-harbor-tooltour-probe/1"
#: Under tests/: the probe, and the sealed pre-agent world truth_sql runs on.
#: Both under `episode/`, the per-task part of tests/ (checks.static_fingerprint).
PROBE_REL = "episode/probe.json"
SEALED_REL = "episode/sealed/world"
#: Never copied into the sealed world: git's object store holds no fact a
#: truth_sql reads (DuckDB reads files), and it is most of the bytes.
SEALED_SKIP = (".git",)
#: The files only the verifier may hold; the leak gate refuses them in
#: environment/ by name (and probe.json by content, whatever it is called).
VERIFIER_ONLY = ("probe.json", "verify_tool.py", "tool_tour_core.py")
#: environment/episode/ subtrees that are the world itself (data the agent is
#: meant to read the fact from), as opposed to what the generator writes.
PAYLOAD_DIRS = ("world", "workspace", "home")
#: The agent's budget on one probe: one question, one tool.
WALL_CLOCK_S = 1800
MAX_TOOL_CALLS = 200
MEMORY_MB = 2048
DISK_MB = 4096
WORLD_BUILDER = Path(__file__).with_name("tooltour_world.py")
#: A truth is looked for in every environment file only when it is a number
#: with at least this many digits before the point: shorter numbers and short
#: words (a count of 3, a page at version 4) occur everywhere by chance.
DISTINCTIVE_DIGITS = 5
DISTINCTIVE_CHARS = 5


# --------------------------------------------------------------- the probes
def _tour():
    harness.modules()                       # tasks/harness, world/, gateway/src on sys.path
    import tool_tour
    return tool_tour


def load_probes(tools=None) -> list:
    """`tool_tour.load_probes`: every tool_tour_probes/*.py PROBES (or the named ones)."""
    return _tour().load_probes(list(tools) if tools is not None else None)


def known_findings(tools=None) -> dict[str, str]:
    return _tour().known_findings(list(tools) if tools is not None else None)


def probe_ref(probe) -> ArmRef:
    return ArmRef(probe.id, TOUR_ARM)


def skip_reason(probe, known: dict[str, str]) -> str | None:
    """Why the current tree cannot export `probe` as a Harbor task, or None."""
    if probe.id in known:
        return (f"known finding of the container tour (the probe's own command fails, "
                f"so the oracle could not pass): {known[probe.id]}")
    try:
        backend_specs(variant_of(SimpleNamespace(profile=probe.profile,
                                                 plugs=probe.plug_map())))
    except VariantError as exc:
        return f"not served by the Harbor image yet: {exc}"
    return None


def probe_document(probe) -> dict:
    """tests/episode/probe.json: the Probe's fields, for verify_tool.py."""
    return {"schema": PROBE_SCHEMA, "harbor_name": probe_ref(probe).harbor_name,
            "probe": {"id": probe.id, "profile": probe.profile,
                      "plugs": {slot: list(names) for slot, names in probe.plugs},
                      "command": probe.command, "expect_tools": list(probe.expect_tools),
                      "expect_args": list(probe.expect_args),
                      "truth_sql": probe.truth_sql, "instruction": probe.instruction,
                      "rows_at": probe.rows_at}}


def prompt_of(probe) -> str:
    """What the agent reads (instruction.md after Harbor strips the canary)."""
    return f"{probe.instruction.strip()}\n\n{ANSWER_LINE}\n"


def solve_sh(probe) -> str:
    return "\n".join([
        "#!/usr/bin/env bash",
        CANARY_HASH,
        f"# The tool-tour oracle for {probe.id}: the probe's own command",
        "# (probe.sh beside this script, verbatim from",
        f"# tasks/harness/tool_tour_probes/{probe.tool}.py), run as the agent through",
        "# the agent's own surface; its last stdout line is the JSON answer, written",
        f"# to {ANSWER_PATH} for the verifier.",
        "set -euo pipefail",
        'here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'out="$(mktemp)"',
        'bash "$here/probe.sh" | tee "$out"',
        'python3 - "$out" <<\'PY\'',
        "import json, sys",
        "lines = [ln for ln in open(sys.argv[1], encoding='utf-8').read().splitlines()",
        "         if ln.strip()]",
        "answer = json.loads(lines[-1])",
        "if not isinstance(answer, dict) or 'value' not in answer:",
        "    sys.exit(f'the probe answered no value: {lines[-1]!r}')",
        f"with open({ANSWER_PATH!r}, 'w', encoding='utf-8') as fh:",
        "    json.dump({'value': answer['value']}, fh)",
        "    fh.write('\\n')",
        "PY",
        'rm -f "$out"',
        ""])


# ----------------------------------------------------------------- truth gate
def truth_needles(fact) -> list[str]:
    """The spellings of `fact` worth looking for: a number with at least
    DISTINCTIVE_DIGITS integer digits as written, rounded to cents and
    grouped with commas; a text of at least DISTINCTIVE_CHARS characters as
    is. Anything shorter cannot be told from noise ([] - not looked for)."""
    if fact is None or isinstance(fact, bool):
        return []
    try:
        number = Decimal(str(fact).strip())
    except (InvalidOperation, ValueError):
        number = None
    if number is None or not number.is_finite():
        text = str(fact).strip()
        return [text] if len(text) >= DISTINCTIVE_CHARS else []
    whole = abs(int(number))
    if len(str(whole)) < DISTINCTIVE_DIGITS:
        return []
    forms = {str(fact).strip(), f"{number:f}", f"{number:.2f}", f"{number:,.2f}"}
    if number == number.to_integral_value():
        forms |= {str(int(number)), f"{int(number):,}"}
    return sorted(forms, key=lambda s: (-len(s), s))


def _text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def leak_problems(task_dir: Path, fact) -> list[str]:
    """The verifier's material and the probe's truth must stay out of the
    agent's image. Empty = pass.

      * environment/ holds no VERIFIER_ONLY file, no copy of
        tests/episode/probe.json under any name, and no `sealed` directory;
      * the truth (truth_needles: distinctive values only) is in no
        environment file and not in instruction.md. A TEXT truth (a
        ticket's status) is looked for only in what the generator writes
        (outside environment/episode/{world,workspace,home}): the world's own
        planes hold it by design, the services serve it from there.
    """
    task_dir = Path(task_dir)
    env = task_dir / "environment"
    problems: list[str] = []
    probe_json = task_dir / "tests" / PROBE_REL
    probe_bytes = probe_json.read_bytes() if probe_json.is_file() else None
    needles = truth_needles(fact)
    numeric = bool(needles) and _is_number(fact)
    for path in sorted([*(p for p in env.rglob("*")), task_dir / "instruction.md"]):
        rel = path.relative_to(task_dir).as_posix()
        if path.is_dir():
            if path.name == "sealed":
                problems.append(f"{rel}: a sealed copy in the agent's environment")
            continue
        if not path.is_file():
            continue
        if path.name in VERIFIER_ONLY:
            problems.append(f"{rel}: a verifier-only file in the agent's environment")
            continue
        if probe_bytes is not None and path.stat().st_size == len(probe_bytes) \
                and path.read_bytes() == probe_bytes:
            problems.append(f"{rel}: identical to tests/{PROBE_REL}")
            continue
        if not needles:
            continue
        parts = path.relative_to(task_dir).parts
        payload = len(parts) > 2 and parts[:2] == ("environment", "episode") \
            and parts[2] in PAYLOAD_DIRS
        if payload and not numeric:
            continue
        text = _text(path)
        if text is None:
            continue
        hits = [n for n in needles if n in text]
        if hits:
            problems.append(f"{rel}: spells out the probe's truth ({hits[0]!r})")
    return problems


def _is_number(fact) -> bool:
    try:
        return Decimal(str(fact).strip()).is_finite()
    except (InvalidOperation, ValueError):
        return False


# ------------------------------------------------------------------ staging
@dataclass
class TourSpec:
    """What worldbuild._stage and render.py read of a task spec (taskspec.TaskSpec),
    for a probe: the tour world's builder, the probe's profile, the agent's budget."""

    task_id: str
    profile: str
    plugs: dict
    instruction: str
    variant: str = TOUR_ARM
    seed: int = TOUR_SEED
    world_builder: Path = WORLD_BUILDER
    artifact_in_world: str = ANSWER_PATH
    max_tool_calls: int = MAX_TOOL_CALLS
    wall_clock_seconds: int = WALL_CLOCK_S
    memory_mb: int = MEMORY_MB
    disk_mb: int = DISK_MB
    virtual_time: str = ""
    grade_argv: tuple = ()
    raw: dict = field(default_factory=dict)


def tour_spec(probe) -> TourSpec:
    return TourSpec(
        task_id=probe.id, profile=probe.profile,
        plugs={slot: list(names) for slot, names in probe.plugs},
        instruction=probe.instruction,
        raw={"task": {"title": f"Tool tour: {probe.id}", "version": TOUR_TASK_VERSION,
                      "discipline": "data_engineering"},
             "difficulty": {"human_hours": "0.1-0.5"},
             "environment": {"surfaces": [probe.tool]}})


class _TourTaskspec:
    """The `taskspec` worldbuild._stage is handed: `load` answers the probe's
    TourSpec; everything else (STAGE_ROOT) is the real module's."""

    def __init__(self, spec: TourSpec, real):
        self._spec, self._real = spec, real

    def load(self, task_id, tasks_dir=None, variant=None):
        if task_id != self._spec.task_id or variant != self._spec.variant:
            raise ExportError(f"tool tour: asked for {task_id}:{variant}, staging "
                              f"{self._spec.task_id}:{self._spec.variant}")
        return self._spec

    def __getattr__(self, name):
        return getattr(self._real, name)


@contextlib.contextmanager
def tour_world_env(probe):
    """The probe's profile for tooltour_world.py (the builder contract's argv
    has no slot for it), restored after."""
    saved = {k: os.environ.get(k) for k in (PROFILE_ENV, PLUGS_ENV)}
    os.environ[PROFILE_ENV] = probe.profile
    os.environ[PLUGS_ENV] = json.dumps({s: list(v) for s, v in probe.plugs})
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def stage_probe(ref: ArmRef, probe, *, work_root: Path | None = None,
                log=print) -> StagedArm:
    """The probe's world, built and staged by worldbuild._stage - the code
    every task goes through - with the tour's world builder behind a
    TourSpec. The caller owns `.cleanup()`."""
    m = harness.modules()
    spec = tour_spec(probe)
    view = SimpleNamespace(**vars(m))
    view.taskspec = _TourTaskspec(spec, m.taskspec)
    if work_root:
        Path(work_root).mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f"{EXPORT_MARKER}{ref.short_name}-",
                                 dir=str(work_root) if work_root else None))
    try:
        with tour_world_env(probe):
            staged = worldbuild._stage(ref, temp, view, log)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    spec.virtual_time = staged.clock or staged.world.get("virtual_time") or ""
    return staged


def seal_world(staged: StagedArm, dest: Path) -> int:
    """The sealed pre-agent copy: the staged world (container-rooted, as the
    agent's /world starts) with the working tree in its place, as the host
    harness lays a world out, minus SEALED_SKIP. Returns the files copied."""
    ignore = shutil.ignore_patterns(*SEALED_SKIP)
    shutil.copytree(staged.world_dir, dest, symlinks=True, ignore=ignore)
    if (dest / "workspace").exists():
        raise ExportError("the staged world still holds workspace/")
    shutil.copytree(staged.workspace_dir, dest / "workspace", symlinks=True, ignore=ignore)
    return len(iter_files(dest))


# ------------------------------------------------------------------ adapter
class ToolTourAdapter(AlkeraHarborAdapter):
    """AlkeraHarborAdapter over tool-tour probes instead of task packages:
    discovery, staging and the task writer are the tour's; the templates
    snapshot, the variant Dockerfiles, the context, the release gates, the
    static-parts check, dataset collection and Harbor validation are Task 4's."""

    def __init__(self, output_dir: Path, limit: int | None = None,
                 overwrite: bool = False, task_ids: list[str] | None = None, *,
                 tools: list[str] | None = None, dataset_name: str = DATASET_NAME,
                 dataset_version: str = DATASET_VERSION, **kwargs):
        kwargs.pop("reward_mode", None)
        super().__init__(output_dir, limit, overwrite, task_ids, reward_mode="pass",
                         dataset_name=dataset_name, dataset_version=dataset_version,
                         **kwargs)
        self.tools = list(tools) if tools else None
        self._probes: dict | None = None
        self._skips: dict | None = None

    # ------------------------------------------------------------ discovery
    @property
    def probes(self) -> dict:
        """ArmRef -> Probe, every probe of the selected tools."""
        if self._probes is None:
            self._probes = {probe_ref(p): p for p in load_probes(self.tools)}
            check_unique(self._probes)
        return self._probes

    def skip_reasons(self) -> dict:
        """ArmRef -> why it is not exported (None: exported)."""
        if self._skips is None:
            known = known_findings(sorted({p.tool for p in self.probes.values()}))
            self._skips = {ref: skip_reason(p, known) for ref, p in self.probes.items()}
        return self._skips

    def available(self) -> list[ArmRef]:
        return sorted(self.probes)

    def chosen(self) -> list[ArmRef]:
        return select(self.available(), self.task_ids)

    def selected(self) -> list[ArmRef]:
        refs = [r for r in self.chosen() if not self.skip_reasons()[r]]
        return refs[: self.limit] if self.limit else refs

    def skipped(self) -> list[dict]:
        out = []
        for ref in self.chosen():
            reason = self.skip_reasons()[ref]
            if reason:
                probe = self.probes[ref]
                out.append({"probe": probe.id, "tool": probe.tool, "name": ref.harbor_name,
                            "profile": probe.profile, "plugs": probe.plug_map(),
                            "reason": reason})
        return out

    def describe(self, ref: ArmRef) -> dict:
        probe = self.probes[ref]
        return {"selector": ref.selector, "name": ref.harbor_name, "probe": probe.id,
                "tool": probe.tool, "profile": probe.profile,
                "skip": self.skip_reasons()[ref]}

    # ------------------------------------------------------------ one task
    def generate_task(self, ref: ArmRef, dest: Path) -> TaskResult:
        started = time.time()
        staged = stage_probe(ref, self.probes[ref], work_root=self.work_dir, log=self.log)
        try:
            result = self._write_task(ref, staged, Path(dest))
        finally:
            staged.cleanup()
        result.seconds = round(time.time() - started, 1)
        return result

    def _write_task(self, ref: ArmRef, staged: StagedArm, out: Path) -> TaskResult:
        probe = self.probes[ref]
        spec = staged.spec
        variant = staged.variant
        commit, dirty = self.source
        out.mkdir(parents=True)
        repo_check = None if self._templates_snapshot else REPO

        # ---- solution/: the probe's command, and the oracle around it
        solution = out / "solution"
        solution.mkdir()
        (solution / "probe.sh").write_text(probe.command, encoding="utf-8", newline="")
        (solution / "solve.sh").write_text(solve_sh(probe), encoding="utf-8", newline="")
        os.chmod(solution / "solve.sh", 0o755)

        # ---- tests/episode/sealed/world: the pre-agent world, before anything
        # moves it; its truth is read NOW, so a truth_sql that fails on its own
        # world fails the export rather than every trial
        tests_dir = out / "tests"
        sealed = tests_dir / SEALED_REL
        seal_world(staged, sealed)
        fact = None
        if probe.truth_sql is not None:
            try:
                fact = _tour().truth(probe.truth_sql, sealed)
            except Exception as exc:                  # noqa: BLE001 - the export's
                raise ExportError(f"{probe.id}: truth_sql fails on the sealed world: "
                                  f"{type(exc).__name__}: {exc}") from None

        # ---- environment/: as every task's (adapter._write_task)
        env_dir = out / "environment"
        copy_template(self._templates / "environment", env_dir, repo=repo_check)
        if variant.profiled:
            (env_dir / "Dockerfile").write_text(self.dockerfile(variant),
                                                encoding="utf-8", newline="")
        self._environment_context(variant, ref.task_id, env_dir / "alkera")
        episode_dir = env_dir / "episode"
        _move_tree(staged.world_dir, episode_dir / "world")
        _move_tree(staged.workspace_dir, episode_dir / "workspace")
        _move_tree(staged.home_dir, episode_dir / "home")
        shutil.copyfile(staged.gateway_config, episode_dir / "gateway.json")

        # ---- tests/: the task verifier's image and helpers, the tour's verifier
        for name in SHARED_TESTS_FILES:
            src = self._templates / "tests" / name
            shutil.copyfile(src, tests_dir / name)
            os.chmod(tests_dir / name, 0o755 if os.stat(src).st_mode & 0o111 else 0o644)
        copy_template(self._templates / TOOLTOUR_TEMPLATE, tests_dir, repo=repo_check)
        try:
            resolve_context(self._templates / TOOLTOUR_TEMPLATE / CONTEXT_FILE,
                            tests_dir / "alkera", repo=REPO, task=ref.task_id)
        except ContextError as exc:
            raise ExportError(str(exc)) from None
        probe_bytes = (json.dumps(probe_document(probe), indent=2) + "\n").encode()
        (tests_dir / PROBE_REL).write_bytes(probe_bytes)

        # ---- task.toml, episode.json: Task 4's renderers, with the probe's spec
        env = {"ALKERA_TASK_VARIANT": ref.arm, "ALKERA_SHIM_WATCH": staged.shim_watch}
        if variant.profiled and staged.clock:
            env["ALKERA_VIRTUAL_TIME"] = staged.clock
        document = render.task_document(
            ref=ref, spec=spec, role=TOUR_ROLE, reward_mode="pass",
            agent_network=self.agent_network, env={**self.static_env(variant), **env},
            source_commit=commit, source_dirty=dirty, oracle="probe", variant=variant)
        document["metadata"].update({"alkera_tooltour_probe": probe.id,
                                     "alkera_tool": probe.tool})
        (out / "task.toml").write_text(render.task_toml(document), encoding="utf-8",
                                       newline="")
        episode = render.episode_json(render.episode_document(
            ref=ref, spec=spec, role=TOUR_ROLE, reward_mode="pass", world=staged.world,
            env=env, source_commit=commit, source_dirty=dirty,
            task_toml_sha256=hashlib.sha256(probe_bytes).hexdigest(),
            anchors="n/a", oracle="probe", restore=staged.restore,
            gateway_config=GATEWAY_CONFIG_IN_CONTAINER,
            backends=backend_specs(variant) if variant.profiled else None))
        for path in (episode_dir / "episode.json", tests_dir / "episode" / "episode.json"):
            path.write_text(episode, encoding="utf-8", newline="")

        prompt = prompt_of(probe)
        (out / "instruction.md").write_text(render.instruction_md(prompt),
                                            encoding="utf-8", newline="")
        (out / "README.md").write_text(task_readme(ref, probe, variant),
                                       encoding="utf-8", newline="")
        _normalize_modes(out)

        # ---- release gates: Task 4's, and the leak gate
        problems: list[str] = []
        problems += checks.canary_problems(out)
        problems += checks.dockerfile_problems(out)
        problems += checks.environment_problems(out, self.fingerprints)
        problems += checks.unpublishable_problems(out)
        problems += checks.payload_problems(out)
        problems += checks.environment_env_problems(out)
        problems += scan(out, self._needles(staged))
        problems += leak_problems(out, fact)
        problems = list(dict.fromkeys(problems))
        if problems:
            raise ExportError("release gates failed:\n  " + "\n  ".join(problems[:40]))
        files = iter_files(out)
        return TaskResult(
            selector=ref.selector, name=ref.harbor_name, status="ok", role=TOUR_ROLE,
            oracle="probe", anchors="n/a", digest=digest.task_digest(out),
            files=len(files), bytes=sum(p.stat().st_size for p in files),
            instruction_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
            variant=variant.key)

    def _snapshot_templates(self, root: Path) -> None:
        super()._snapshot_templates(root)
        copy_template(self.template_dir / TOOLTOUR_TEMPLATE, root / TOOLTOUR_TEMPLATE,
                      keep_context=True)

    # ------------------------------------------------------------ the run
    def run(self):
        report = super().run()
        report.results += [TaskResult(s["probe"] + ":" + TOUR_ARM, s["name"], "skipped",
                                      s["reason"]) for s in self.skipped()]
        return report

    def collect_existing(self) -> list[dict]:
        tasks = super().collect_existing()
        for task in tasks:
            meta = tomllib.loads((self.output_dir / task["dir"] / "task.toml")
                                 .read_text(encoding="utf-8")).get("metadata") or {}
            task["probe"] = meta.get("alkera_tooltour_probe")
            task["tool"] = meta.get("alkera_tool")
        return [t for t in tasks if t["probe"]]

    def write_dataset_files(self, tasks: list[dict]) -> Path:
        commit, dirty = self.source
        skipped = self.skipped()
        document = {
            "dataset": {
                "name": self.dataset_name,
                "version": self.dataset_version,
                "description": (
                    "Alkera tool tour: one Harbor task per tool-tour probe - one fact "
                    "read through one tool's real client (DuckDB, dbt, MetricFlow, "
                    "Jupyter, Power BI, Tableau, Sigma, Jira, Confluence, Slack, the "
                    "scheduler, Excel, ...) on the commerce_analytics world. Reward 1 "
                    "iff the answer equals the fact on a sealed pre-agent copy of the "
                    "world AND the tool's own audit rows show it was used."),
                "authors": [{"name": render.AUTHOR}],
                "keywords": ["data-engineering", "tool-use", "agentic", "tool-tour"],
            },
            "tasks": [{"name": t["name"], "digest": t["digest"]} for t in tasks],
        }
        path = self.output_dir / "dataset.toml"
        path.write_text(tomlw.dumps(document, header=[
            CANARY_HASH,
            f"# Generated by harbor/export.py --tooltour (alkera_harbor {__version__}) "
            f"from {commit}{' (dirty tree)' if dirty else ''}.",
            "# Digests are Harbor's Packager.compute_content_hash; regenerate, "
            "never hand-edit.", ""], inline=("dataset.authors",)),
            encoding="utf-8", newline="")
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "kind": "tooltour",
            "dataset": {"name": self.dataset_name, "version": self.dataset_version},
            "generator": {"name": "alkera_harbor.tooltour", "version": __version__},
            "exported_at": _dt.datetime.now(_dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "source_commit": commit,
            "source_dirty": dirty,
            "reward_modes": ["pass"],
            "tasks": tasks,
            "skipped": skipped,
        }
        (self.output_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="")
        (self.output_dir / "README.md").write_text(
            dataset_readme(self.dataset_name, self.dataset_version, tasks, skipped,
                           commit, dirty), encoding="utf-8", newline="")
        return path


# ------------------------------------------------------------------ READMEs
LIVE_RUN = ("harbor run -p harbor/dist/alkera-tooltour -a claude-code "
            "-m anthropic/claude-opus-5-5 --allow-agent-host api.anthropic.com -n 4 "
            "-o harbor/dist/tooltour-jobs")


def task_readme(ref: ArmRef, probe, variant) -> str:
    rows_at = ("as each call returns" if probe.rows_at == "call" else
               "at teardown (world-down folds the server's log in before the "
               "artifacts are collected)")
    return "\n".join([
        f"# {ref.harbor_name}",
        "",
        f"Tool tour probe `{probe.id}`: one fact read through `{probe.tool}` on a "
        f"`{probe.profile}` box ({variant.key}). This README is packaged for "
        f"reviewers; it is never shown to the agent and is not in its image.",
        "",
        "## The task",
        "",
        "`instruction.md` is the probe's brief plus where the answer goes "
        f"(`{ANSWER_PATH}`, `{{\"value\": ...}}`). It never names the command and "
        "never the answer.",
        "",
        "## How it is scored",
        "",
        "`tests/verify_tool.py`, in the separate verifier, with no network, as an "
        "unprivileged user: reward 1 iff",
        "",
        "1. **answer** - the answer's `value` equals the scalar `truth_sql` returns "
        "on the SEALED pre-agent copy of the world (`tests/episode/sealed/world`), "
        "never the agent's `/world`;",
        f"2. **tools** - every expected tool ({', '.join(f'`{t}`' for t in probe.expect_tools)}) "
        "is in the audit rows above the episode watermark"
        + (f", with argv containing {', '.join(f'`{a}`' for a in probe.expect_args)}"
           if probe.expect_args else "") + f" (the rows land {rows_at});",
        "3. **network** - no destination outside what the episode was handed;",
        "4. **seq** - the action log's seq is contiguous (untampered).",
        "",
        "Otherwise 0. What the agent cannot cause (a broken export, a missing "
        "watermark) writes no reward: Harbor records a verifier error.",
        "",
        "## Solution",
        "",
        "`solution/probe.sh` is the probe's own command, verbatim; `solve.sh` runs "
        "it and writes its last JSON line to the answer file.",
        "",
        "## Run it",
        "",
        "```bash",
        f"harbor run -p <dataset-dir>/{ref.short_name} -a oracle   # 1",
        f"harbor run -p <dataset-dir>/{ref.short_name} -a nop      # 0",
        "```",
        "",
    ])


def dataset_readme(name, version, tasks, skipped, commit, dirty) -> str:
    rows = "\n".join(f"| `{t['probe']}` | `{t['tool']}` | `{t['profile']}` | "
                     f"`{(t['digest'] or '')[:19]}` |" for t in tasks)
    skip_rows = "\n".join(f"| `{s['probe']}` | `{s['profile']}` | {s['reason']} |"
                          for s in skipped) or "| - | - | none |"
    return "\n".join([
        f"# {name} {version}",
        "",
        "One Harbor task per tool-tour probe (`tasks/harness/tool_tour_probes/`): "
        "the agent answers one question with one tool on the commerce_analytics "
        "world (seed 11) and writes `{\"value\": ...}` to "
        f"`{ANSWER_PATH}`. Reward 1 iff the value equals the fact on a sealed "
        "pre-agent copy of the world, the tool's audit rows are on the record, no "
        "connection left the box's own services, and the audit log is intact.",
        "",
        f"Generated by `harbor/export.py --tooltour` from `{commit}`"
        f"{' (dirty tree)' if dirty else ''}. Canary: `{render.CANARY_TEXT}`.",
        "",
        "## Run",
        "",
        "```bash",
        "harbor run -p . -a oracle      # every probe's own command must score 1",
        "harbor run -p . -a nop         # doing nothing must score 0",
        "# a live agent (the model API is the agent phase's only network):",
        LIVE_RUN.replace("harbor/dist/alkera-tooltour", "."),
        "python3 harbor/tooltour_report.py harbor/dist/tooltour-jobs",
        "```",
        "",
        "## Tasks",
        "",
        "| probe | tool | profile | digest |",
        "|---|---|---|---|",
        rows,
        "",
        "## Skipped (not exported from this tree)",
        "",
        "| probe | profile | reason |",
        "|---|---|---|",
        skip_rows,
        "",
    ])


# ---------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harbor/export.py --tooltour",
        description="Turn every tool-tour probe into a Harbor task (one dataset).")
    parser.add_argument("--output-dir", type=Path,
                        help="where the dataset goes, e.g. harbor/dist/alkera-tooltour "
                             "(required unless --list)")
    parser.add_argument("--tools", default=None,
                        help="comma-separated probe modules (default: every tool)")
    parser.add_argument("--task-ids", nargs="+", default=None, metavar="ID",
                        help="probe ids (or Harbor names) to export; comma-separated "
                             "values are accepted too")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--overwrite", "--force", action="store_true", dest="overwrite")
    parser.add_argument("--list", action="store_true", dest="list_tasks",
                        help="list every probe, exported or skipped (and why)")
    parser.add_argument("--agent-network", choices=AGENT_NETWORK_MODES,
                        default="no-network")
    parser.add_argument("--dataset-name", default=DATASET_NAME)
    parser.add_argument("--dataset-version", default=DATASET_VERSION)
    parser.add_argument("--validate", action="store_true",
                        help="load every generated task with a real Harbor install")
    parser.add_argument("--harbor-python", default=None)
    parser.add_argument("--template-dir", type=Path, default=TEMPLATE_DIR)
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="where worlds are built (default: the system temp dir)")
    parser.add_argument("--report", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.list_tasks and args.output_dir is None:
        build_parser().error("--output-dir is required")
    tools = [t for t in args.tools.split(",") if t] if args.tools else None
    try:
        adapter = ToolTourAdapter(
            args.output_dir or Path("."), limit=args.limit, overwrite=args.overwrite,
            task_ids=args.task_ids, tools=tools, agent_network=args.agent_network,
            dataset_name=args.dataset_name, dataset_version=args.dataset_version,
            template_dir=args.template_dir, work_dir=args.work_dir,
            validate=args.validate, harbor_python=args.harbor_python)
        if args.list_tasks:
            refs = adapter.chosen()
            print(f"{len(refs)} probe(s):")
            for ref in refs:
                info = adapter.describe(ref)
                state = "export" if not info["skip"] else f"SKIP {info['skip']}"
                print(f"  {info['probe']:<52} {info['profile']:<14} {state}")
            return 0
        report = adapter.run()
    except (ExportError, NamingError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    ok = sum(r.status == "ok" for r in report.results)
    skipped = [r for r in report.results if r.status == "skipped"]
    print(f"done in {report.seconds}s: {ok} generated, {len(skipped)} skipped, "
          f"{len(report.errors)} failed; dataset: {report.dataset_toml}")
    for r in skipped:
        print(f"  skipped {r.selector}: {r.reason[:240]}")
    for r in report.errors:
        print(f"  FAILED {r.selector}: {r.reason}", file=sys.stderr)
    for problem in report.dataset_problems:
        print(f"  DATASET: {problem}", file=sys.stderr)
    if args.report:
        args.report.write_text(report_json(report) + "\n", encoding="utf-8")
    return 1 if report.errors or report.dataset_problems else 0
