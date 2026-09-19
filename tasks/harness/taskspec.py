"""Task package -> harness spec. Task-agnostic: only key names are known here.

Everything task-specific (how the world is built, what argv the grader takes,
which checks are load-bearing) is declared in the task's own task.toml under
`[harness]`. If a task does not declare it, the harness refuses to run rather
than inventing a default - an invented pass threshold would silently decide the
Pass@1 number.

CONTEXT VARIANTS. A task directory is ONE context. Ablations of that context -
remove the ticket, remove the stale definition, stack a second defect - are
NOT separate tasks: they are `[variants]` of this one, selected by
`--variant <name>` (or `ALKERA_TASK_VARIANT` for anything that cannot take a
flag). Each variant is a set of file overlays copied over `fixtures/` at
staging time, plus an optional prompt variant and optional grading flags the
task's own grade.py interprets.

The five GT004 context arms and the two GT006 2x2 arms used to be seven
top-level directories, each a full copy of its parent. A copy drifts: the day
the parent's grader is fixed, seven graders disagree and nobody notices,
because `diff -rq` over a 25 MB fixture tree is not something anyone runs
twice. One directory, seven overlays of four YAML files each, cannot drift.

SCORING MATRIX. A task may also declare `[matrix]` (new_matrix.md, Layer A):
the projection of its grader's EXISTING check ids onto the four library axes
of axes.py - `pass_axis` (the axis the projected pass reads), `gate` (the axis
that admits an episode to the other denominators), `accepted` (the baselines
oracle validity must admit) and `[matrix.map]` (axis -> check ids). Layer A is
score-only: nothing here moves a point, a check id or the pass criterion. Two
rules the frozen dataclass does not enforce and this loader therefore does:

  * a `[matrix]` table must state `gate` explicitly. MatrixSpec defaults gate
    to 'found' so that CODE can construct one, but a declaration that decides
    which episodes enter a denominator has to say so, not inherit it. The one
    gateless case - a control task with nothing to find - spells it
    `gate = false`.
  * `accepted` names are checked against the baselines the package STAGES,
    where that set is enumerable. The roster lives in Python, not in
    task.toml: tests/run_baselines.py keeps an AGENTS dict (GT004/GT008/GT009;
    GT004 adds arm-specific entries in ARM_AGENTS), GT005 a BASELINES dict,
    and GT001's suite declares neither - so where no roster parses out of the
    file (read with ast, NEVER imported: the suite imports the world stack at
    module level), `accepted` is validated as a list of strings and no
    further. An accepted name no baseline answers to is a typo that would
    otherwise surface as a silently empty oracle-validity set.

A variant may override the matrix per key: `[variants.<name>.matrix]` and
`[variants.<name>.matrix.map]` replace exactly the keys they declare, the same
pattern `[variants.<name>.grading]` uses for the pass criterion.

OUTCOME CLASS (outcome.py, Layer 1). `[matrix.outcome]` is one more key of the
matrix table and is resolved by the same rule: an arm that declares
`[variants.<name>.matrix.outcome]` replaces the WHOLE outcome table, an arm
that does not inherits the task's. The table's shape is refused here (through
outcome.parse_outcome_spec) with the same TaskSpecError-from-MatrixError
pattern as the rest of [matrix]. Its check ids are checked against the ids the
loader can vouch for: the union of `[matrix.map]` and the check table of the
task's `[grading].manifest` (read with PyYAML where that is importable - the
hygiene penalties an outcome table leans on, GT009's H1/H2, are mapped to no
axis, so the map alone would refuse them). Where neither is enumerable, ids
are checked for shape only and validity.py owns the check against the grader.

THE SCALAR'S DECLARATIONS (DECISION_TEMPLATE.md §9; reward.py combines them).
Three more things ride on the MatrixSpec, resolved per arm by the same
one-key rule as `outcome` (an arm's table replaces the base's wholesale):

  * `[matrix.progress]` -> progress.parse_progress_spec, with the same
    known-ids set the outcome table is vouched against;
  * `[matrix.path]`     -> path.parse_path_spec;
  * `role`              -> "control" | "decision". READ FROM THE ARM'S OWN
    TABLE, `[variants.<arm>].role` - for the default arm too, since both
    shipped decision tasks express `default` as a `[variants.default]` table
    (GT009, GT004) and the template (§1) puts the role beside the arm's
    overlay and note "so nothing has to be inferred from the name". A task
    that declares an outcome table but no `[variants]` at all (the loader's
    own fixtures) states it as `[matrix] role = ...`, and that key is also
    the fallback an arm inherits when its own table is silent.

    RULE: an arm whose RESOLVED matrix carries an outcome table must state a
    role, else TaskSpecError - the band `withheld_with_evidence` lands in
    depends on it, and a default there would decide the training scalar
    silently. An arm with no outcome table defaults to "control".

Every ValueError the two parsers raise is re-raised as TaskSpecError chained
from it, the same pattern as MatrixError above.
"""
from __future__ import annotations

import ast
import hashlib
import os
import shutil
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from axes import ROLES, ArtifactTokens, MatrixError, MatrixSpec
from outcome import parse_outcome_spec
from path import parse_path_spec
from progress import parse_progress_spec

TASKS_DIR = Path(__file__).resolve().parent.parent

# The environment variable that carries the variant into a process that cannot
# take a flag: the task's own world builder, its surface stager and its grader
# are all started by the harness (or by run_baselines.py) as plain subprocesses
# whose argv the harness does not own.
VARIANT_ENV = "ALKERA_TASK_VARIANT"

# Staged overlay trees live outside the repo, beside the rollouts, for the same
# reason those do: a fixtures copy inside the source tree puts the subject one
# `..` from the grading reference.
STAGE_ROOT = Path(tempfile.gettempdir()) / "alkera-fixtures"

# Never copied into a staged fixtures tree: the overlays themselves (a variant
# does not ship the other variants) and compiled bytecode.
_STAGE_IGNORE = shutil.ignore_patterns("variants", "__pycache__", "*.pyc")


class TaskSpecError(Exception):
    pass


@dataclass(frozen=True)
class Variant:
    """One named context of a task.

    name      the selector; `default` is always the shipped context.
    note      why this variant exists. Mandatory - an unexplained arm is an
              unexplained number six weeks later.
    overlay   the directories whose trees are copied OVER `fixtures/` at
              staging time, in the order given - base first, then each overlay,
              last write wins. Empty for the shipped fixtures unmodified.
              A LIST rather than one directory because two arms of a 2x2 differ
              from each other in three files and share three more: with one
              level, the shared three have to be duplicated into both arms, and
              a duplicated 1,100-line generator is the drift this whole design
              exists to remove.
    prompt    the declared prompt-variant name this arm was measured under, or
              "" to leave the brief where `[agent_visible].brief` points.
    grading   free-form flags the TASK's grade.py interprets. The harness only
              carries them; it does not know what any of them mean, and
              build_world.py writes them into the grading zone so the grader
              reads the arm it was actually run under rather than an env var
              that may have been lost across a subprocess boundary.
    matrix    the arm's [variants.<name>.matrix] block, raw. Per-key override
              of the task's [matrix], the same pattern as `grading`: a key the
              arm declares replaces the task-level one, a key it does not is
              inherited. Empty when the arm overrides nothing.
    role      the arm's declared `role` ("control" | "decision"), or None
              when the arm's table does not state one. Resolved onto the
              MatrixSpec in `_matrix_spec`; see the module docstring.
    """
    name: str
    note: str
    overlays: tuple = ()
    prompt: str = ""
    grading: dict = field(default_factory=dict)
    matrix: dict = field(default_factory=dict)
    role: str | None = None

    @property
    def overlay(self) -> Path | None:
        """The LAST overlay, or None. The arm's own tree, in the common case
        where it declares one; what a caller means by "where does this arm's
        content live"."""
        return self.overlays[-1] if self.overlays else None


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    root: Path
    raw: dict

    instruction: str
    seed: int
    virtual_time: str

    wall_clock_seconds: int
    max_tool_calls: int
    memory_mb: int
    disk_mb: int

    artifact_in_world: str          # e.g. "/workspace/final_report.json"
    grade_entrypoint: Path
    grade_argv: list[str]
    world_builder: Path
    instruction_path: Path
    # `[grading].manifest`, when the task declares one. The grader's other
    # half: every task's build_world.py copies it into the grading zone and
    # grade.py reads THAT copy, so a regrade that re-runs a changed grade.py
    # against the copy made at episode time is half a regrade. rollout.py
    # --regrade refreshes the copy from this path (keeping the planted canary)
    # and hashes both halves into `regrade_grader_sha256`.
    grade_manifest: Path | None = None
    surface_probes: list[dict] = field(default_factory=list)

    # Which surfaces this task's world MUST have. A rollout that starts with one
    # of these dead is a harness error, not a score - see surface_audit.py. The
    # harness does not invent this list: a task that declares none simply cannot
    # fail the gate, and the report names that as a run condition rather than
    # letting it read as "all surfaces fine".
    required_surfaces: list[str] = field(default_factory=list)
    # Surfaces that are dead ON PURPOSE in this world, each with a reason, so a
    # deliberately minimal world is not flagged forever.
    dead_by_design: list[dict] = field(default_factory=list)
    # name -> sha256 of the prompt file, from [prompt_variants]. A run must be
    # able to name which ticket produced it; see report.py's run conditions.
    prompt_variants: dict[str, str] = field(default_factory=dict)
    prompt_variant_default: str = ""

    # Which CONTEXT this spec was resolved for. `variant` is "" exactly when the
    # task declares no [variants] at all, so a report can tell "this task has
    # one context" apart from "this run used the default one".
    variant: str = ""
    variant_note: str = ""
    variant_overlays: tuple = ()
    variant_grading: dict = field(default_factory=dict)
    variant_names: list[str] = field(default_factory=list)

    pass_requires_checks: list[str] = field(default_factory=list)
    pass_min_total: float = 0.0

    # The task's scoring matrix (new_matrix.md, Layer A), resolved through the
    # variant's [variants.<name>.matrix] override if it declares one. None when
    # the task has no [matrix] table - most tasks - and for them nothing about
    # loading, running or the pass criterion changes: Layer A is score-only, so
    # pass_requires_checks / pass_min_total above still decide the Pass@1 bit
    # and the matrix projects the SAME checks onto the library axes.
    matrix: MatrixSpec | None = None

    def scores_pass(self, score: dict) -> bool:
        """Did this rollout solve the task? Criterion comes from the task."""
        by_id = {c["id"]: c for c in score.get("checks", [])}
        for cid in self.pass_requires_checks:
            if cid not in by_id or not by_id[cid].get("passed"):
                return False
        return float(score.get("total", 0.0)) >= self.pass_min_total

    def artifact_host_path(self, world: dict) -> Path:
        """Map the in-world artifact path onto the host world tree.

        §9.2.1 puts the agent's tree at /workspace, so an artifact declared at
        /workspace/x lands at <workspace>/x. Anything else is resolved against
        the world root.
        """
        p = self.artifact_in_world
        if p.startswith("/workspace"):
            return Path(world["workspace"]) / p[len("/workspace"):].lstrip("/")
        return Path(world["world_root"]) / p.lstrip("/")


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise TaskSpecError(f"{where} is missing required key '{key}'")
    return d[key]


def _dead_by_design(task_id: str, harness: dict) -> list[dict]:
    """`dead_by_design = [{ label = "chat", reason = "..." }, ...]`.

    The reason is mandatory. "Dead on purpose" with no purpose written down is
    just the flag being switched off, which is the thing this whole module
    exists to stop.
    """
    out = []
    for i, entry in enumerate(harness.get("dead_by_design", []) or []):
        if not isinstance(entry, dict) or not entry.get("label"):
            raise TaskSpecError(
                f"{task_id} [harness].dead_by_design[{i}] must be a table with "
                f"a `label`, got {entry!r}")
        if not str(entry.get("reason", "")).strip():
            raise TaskSpecError(
                f"{task_id} [harness].dead_by_design[{i}] ({entry['label']}) has "
                f"no `reason`. A surface declared dead on purpose without the "
                f"purpose written down is an unexplained hole in the world the "
                f"agent is measured in.")
        out.append({"label": str(entry["label"]), "reason": str(entry["reason"])})
    return out


def _check_surface_declarations(task_id: str, probes: list[dict],
                                required: list[str],
                                dead_by_design: list[dict]) -> None:
    """A gate that cannot be evaluated is worse than no gate, so refuse early."""
    labels = {p.get("label") or p["tool"] for p in probes}
    tools = {p["tool"] for p in probes} | {c for p in probes
                                           for c in (p.get("covers") or [])}
    unknown = [r for r in required if r not in labels and r not in tools]
    if unknown:
        raise TaskSpecError(
            f"{task_id} [harness].required_surfaces names {unknown}, which no "
            f"surface_probe declares as a label, a tool or a `covers` entry. A "
            f"required surface nobody probes cannot be verified live, so the "
            f"gate would silently pass. Add the probe.")
    designed = {d["label"] for d in dead_by_design}
    contradiction = sorted(designed & set(required))
    if contradiction:
        raise TaskSpecError(
            f"{task_id} [harness] declares {contradiction} both required and "
            f"dead_by_design. One of the two is wrong and the harness will not "
            f"pick.")


def _declared_variants(task_id: str, root: Path, raw: dict) -> dict[str, Variant]:
    """`[variants]` -> {name: Variant}, or {} for a task with one context."""
    table = raw.get("variants") or {}
    if not table:
        return {}
    if "default" not in table:
        raise TaskSpecError(
            f"{task_id} declares [variants] without a `default`. `default` is "
            f"the shipped context and is what every run that names no variant "
            f"gets; a task whose default is implicit cannot say what its "
            f"already-recorded rollouts were measured in.")
    out: dict[str, Variant] = {}
    for name, block in table.items():
        if not isinstance(block, dict):
            raise TaskSpecError(
                f"{task_id} [variants].{name} must be a table, got {block!r}")
        note = str(block.get("note", "")).strip()
        if not note:
            raise TaskSpecError(
                f"{task_id} [variants].{name} has no `note`. An arm nobody "
                f"wrote a reason for is an unexplained number six weeks later.")
        declared = block.get("overlay") or []
        if isinstance(declared, str):
            declared = [declared]
        overlays = []
        for entry in declared:
            path = (root / str(entry)).resolve()
            if not path.is_dir():
                raise TaskSpecError(
                    f"{task_id} [variants].{name}.overlay names {path}, which "
                    f"is not a directory.")
            overlays.append(path)
        role = block.get("role")
        if role is not None and not isinstance(role, str):
            raise TaskSpecError(
                f"{task_id} [variants].{name}.role must be one of {ROLES}, "
                f"got {role!r}")
        out[name] = Variant(
            name=name, note=note, overlays=tuple(overlays),
            prompt=str(block.get("prompt", "")),
            grading=dict(block.get("grading") or {}),
            matrix=_variant_matrix(task_id, name, block),
            role=role)
    if out["default"].overlays:
        raise TaskSpecError(
            f"{task_id} [variants].default declares an overlay. `default` IS "
            f"the shipped fixtures; an overlay on it means the directory and "
            f"the default arm have already diverged.")
    return out


def _variant_matrix(task_id: str, name: str, block: dict) -> dict:
    """The arm's raw [variants.<name>.matrix] block, or {}. Resolution against
    the task's [matrix] happens in `_matrix_spec`, once the arm is known."""
    matrix = block.get("matrix")
    if matrix is None:
        return {}
    if not isinstance(matrix, dict):
        raise TaskSpecError(
            f"{task_id} [variants].{name}.matrix must be a table, "
            f"got {matrix!r}")
    return dict(matrix)


def _staged_baselines(root: Path) -> set[str] | None:
    """The baseline names a package's tests/run_baselines.py stages, or None.

    The roster lives in Python, not in task.toml, and each package's suite
    keeps its own shape: GT004/GT008/GT009 an `AGENTS` dict (GT004 adds
    arm-specific entries in `ARM_AGENTS`), GT005 a `BASELINES` dict, GT001's
    suite neither. The names are read by PARSING, never by importing - the
    suite imports the world stack at module level. None means "not enumerable
    here", and `accepted` is then validated for type only.
    """
    path = root / "tests" / "run_baselines.py"
    if not path.is_file():
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None

    def keys_of(node) -> set[str]:
        if not isinstance(node, ast.Dict):
            return set()
        return {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}

    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = {node.target.id}
        else:
            continue
        if targets & {"AGENTS", "BASELINES"}:
            names |= keys_of(node.value)
        if "ARM_AGENTS" in targets and isinstance(node.value, ast.Dict):
            for arm_table in node.value.values:
                names |= keys_of(arm_table)
    return names or None


def _manifest_check_ids(root: Path, raw: dict) -> set[str] | None:
    """The check ids the task's `[grading].manifest` declares, or None.

    None means "not enumerable here": no manifest declared, no PyYAML in this
    interpreter, or a manifest that does not parse into a `checks` table of
    rows with an `id`. Read by parsing only; nothing here imports the grader.
    """
    rel = (raw.get("grading") or {}).get("manifest")
    if not rel:
        return None
    path = root / str(rel)
    if not path.is_file():
        return None
    try:
        import yaml  # optional here: test_matrix_gt008 already leans on it
    except ImportError:
        return None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    rows = (doc or {}).get("checks") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return None
    ids = {str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")}
    return ids or None


def _matrix_spec(task_id: str, root: Path, base: dict | None,
                 arm: dict | None, raw: dict | None = None,
                 arm_role: str | None = None) -> MatrixSpec | None:
    """`[matrix]` plus the resolved arm's `[variants.<name>.matrix]` -> MatrixSpec.

    Per-key override, the same pattern as the arm's grading flags: a key the
    arm declares replaces the task-level one (so an arm's [matrix.map] replaces
    the whole map, not one axis of it), a key it does not is inherited. Two
    TOML-level refusals the frozen dataclass does not make: a declaration must
    state `pass_axis`, and it must state `gate` EXPLICITLY somewhere - the
    'found' default in MatrixSpec is for code, not for a declaration that
    decides which episodes enter a denominator. `gate = false` is the explicit
    spelling of the gateless control task (TOML has no null).

    `arm_role` is the resolved arm's own `[variants.<name>].role`, or None;
    `[matrix].role` is the fallback. See the module docstring.
    """
    if base is not None and not isinstance(base, dict):
        raise TaskSpecError(
            f"{task_id} [matrix] must be a table, got {base!r}")
    if base is None and not arm:
        return None
    base, arm = base or {}, arm or {}

    def pick(key: str, default=None):
        return arm[key] if key in arm else base.get(key, default)

    if "pass_axis" not in base and "pass_axis" not in arm:
        raise TaskSpecError(
            f"{task_id} [matrix] sets no `pass_axis`. The pass the matrix "
            f"presents is the rate on that axis alone; without it there is no "
            f"projection, only the four diagnostic rates.")
    if "gate" not in base and "gate" not in arm:
        raise TaskSpecError(
            f"{task_id} [matrix] sets no `gate`. MatrixSpec defaults gate to "
            f"'found' so that CODE can construct one, but a declaration that "
            f"decides which episodes enter the other denominators has to say "
            f"so. State the axis, or `gate = false` for a control task with "
            f"nothing to find.")
    gate = pick("gate")
    if gate is False:
        gate = None
    elif not isinstance(gate, str):
        raise TaskSpecError(
            f"{task_id} [matrix].gate must be one of the library axes or "
            f"`false`, got {gate!r}")

    accepted = pick("accepted", [])
    if not isinstance(accepted, list) \
            or not all(isinstance(a, str) for a in accepted):
        raise TaskSpecError(
            f"{task_id} [matrix].accepted must be a list of baseline names, "
            f"got {accepted!r}")
    known = _staged_baselines(root)
    if accepted and known is not None:
        unknown = sorted(set(accepted) - known)
        if unknown:
            raise TaskSpecError(
                f"{task_id} [matrix].accepted names {unknown}, which "
                f"tests/run_baselines.py does not stage. Staged baselines: "
                f"{sorted(known)}. An accepted name no baseline answers to is "
                f"a typo that would otherwise read as an empty oracle-validity "
                f"set.")

    raw_map = pick("map", {})
    if not isinstance(raw_map, dict):
        raise TaskSpecError(
            f"{task_id} [matrix.map] must be a table of axis -> check ids, "
            f"got {raw_map!r}")
    axis_map = {}
    for axis, ids in raw_map.items():
        if not isinstance(ids, list) \
                or not all(isinstance(i, str) for i in ids):
            raise TaskSpecError(
                f"{task_id} [matrix.map].{axis} must be a list of check id "
                f"strings, got {ids!r}")
        axis_map[str(axis)] = tuple(ids)

    raw_proxy = pick("proxy", {})
    if not isinstance(raw_proxy, dict):
        raise TaskSpecError(
            f"{task_id} [matrix.proxy] must be a table of axis -> "
            f"{{field, tokens}}, got {raw_proxy!r}")
    proxy = {}
    for axis, entry in raw_proxy.items():
        if not isinstance(entry, dict) \
                or not isinstance(entry.get("field"), str) \
                or not isinstance(entry.get("tokens"), list) \
                or not all(isinstance(t, str) for t in entry["tokens"]):
            raise TaskSpecError(
                f"{task_id} [matrix.proxy].{axis} must be a table with a "
                f"`field` string and a `tokens` list of strings, got "
                f"{entry!r}. The proxy is a documented stand-in for a failed "
                f"check (new_matrix.md 3.1 item 5), so the field and the "
                f"tokens have to be stated, not implied.")
        proxy[str(axis)] = ArtifactTokens(field=entry["field"],
                                          tokens=tuple(entry["tokens"]))

    # [matrix.outcome] (outcome.py). ONE key, like `map`: the arm's table
    # replaces the base's wholesale. Ids are vouched for by the map's union
    # plus the grader manifest's check table where that can be read.
    raw_outcome = pick("outcome")
    outcome_spec = None
    known = {cid for ids in axis_map.values() for cid in ids}
    manifest_ids = _manifest_check_ids(root, raw or {})
    vouched = (known | manifest_ids) if manifest_ids else None
    if raw_outcome is not None:
        try:
            outcome_spec = parse_outcome_spec(
                raw_outcome, known_ids=vouched,
                where=f"{task_id} [matrix.outcome]")
        except MatrixError as exc:
            raise TaskSpecError(str(exc)) from exc

    # [matrix.progress] and [matrix.path] (progress.py, path.py): the two
    # declared inputs to reward.py beside the outcome class. ONE key each,
    # replaced wholesale on override like `outcome`. Their parsers raise
    # ValueError; it is chained into TaskSpecError the way MatrixError is.
    raw_progress = pick("progress")
    progress_spec = None
    if raw_progress is not None:
        try:
            progress_spec = parse_progress_spec(
                raw_progress, known_ids=vouched,
                where=f"{task_id} [matrix.progress]")
        except ValueError as exc:
            raise TaskSpecError(str(exc)) from exc

    raw_path = pick("path")
    path_spec = None
    if raw_path is not None:
        try:
            path_spec = parse_path_spec(raw_path,
                                        where=f"{task_id} [matrix.path]")
        except ValueError as exc:
            raise TaskSpecError(str(exc)) from exc

    # role: the arm's own table first, then [matrix].role. Required the
    # moment the arm carries an outcome table - withheld_with_evidence's band
    # is 1.0 or a partial depending on it, and a default there would decide
    # the training scalar without anyone having written it down.
    role = arm_role if arm_role is not None else base.get("role")
    if role is None:
        if outcome_spec is not None:
            raise TaskSpecError(
                f"{task_id} declares [matrix.outcome] but no `role`. State "
                f"`role = \"control\"` (the repair is in-mandate: do all of "
                f"it) or `role = \"decision\"` (hold the boundary and put the "
                f"evidence on the record) in the arm's [variants.<arm>] "
                f"table, so the band withheld_with_evidence lands in is "
                f"declared rather than inferred from the arm's name.")
        role = "control"
    if not isinstance(role, str) or role not in ROLES:
        raise TaskSpecError(
            f"{task_id} role must be one of {ROLES}, got {role!r}")

    pass_axis = str(pick("pass_axis"))
    try:
        spec = MatrixSpec(pass_axis=pass_axis, gate=gate,
                          accepted=tuple(accepted), map=axis_map, proxy=proxy,
                          outcome=outcome_spec, progress=progress_spec,
                          path=path_spec, role=role)
    except MatrixError as exc:
        raise TaskSpecError(f"{task_id} [matrix]: {exc}") from exc

    if not axis_map.get(pass_axis):
        raise TaskSpecError(
            f"{task_id} [matrix] pass_axis = {pass_axis!r} but [matrix.map] "
            f"names no {pass_axis} checks. Every episode would withhold the "
            f"projected pass as n/a forever, with no signal but an empty "
            f"denominator. A pass axis the grader cannot score is a "
            f"declaration error, not a measurement.")
    if gate is not None and not axis_map.get(gate):
        raise TaskSpecError(
            f"{task_id} [matrix] gate = {gate!r} but [matrix.map] names no "
            f"{gate} checks. The gate would admit-or-withhold on zero "
            f"evidence for every episode. State the checks, or `gate = "
            f"false` for a control task with nothing to find.")
    return spec


def _requested_variant(explicit: str | None) -> str | None:
    """The variant this process was asked for: flag first, then the env var.

    The env var exists because the world builder, the surface stager, the
    grader and run_baselines.py are subprocesses whose argv the harness does
    not own. An empty value is treated as "not set" rather than as a variant
    named "", which is what an unset shell variable expands to.
    """
    if explicit:
        return explicit
    return os.environ.get(VARIANT_ENV) or None


def resolve_variant(task_id: str, variants: dict[str, Variant],
                    requested: str | None) -> Variant:
    """Pick the arm, or refuse and say which task and which names exist."""
    requested = _requested_variant(requested)
    if not variants:
        if requested:
            raise TaskSpecError(
                f"--variant {requested!r} was given for {task_id}, which "
                f"declares no [variants] table. {task_id} has ONE context; a "
                f"different context is a different GT task.")
        return Variant(name="", note="task declares no [variants]")
    if requested is None:
        return variants["default"]
    if requested not in variants:
        raise TaskSpecError(
            f"{task_id} declares no variant named {requested!r}. Declared: "
            f"{sorted(variants)}")
    return variants[requested]


def _tree_stamp(*roots: Path) -> str:
    """A digest of every file the staged tree is built from.

    Note for anyone holding a path into a staged tree: it is a CACHE. Editing
    ANY file under `fixtures/` - including one no overlay names - moves this
    digest and the next `stage_fixtures` call deletes and rebuilds the tree, so
    anything generated into it disappears without warning.

    Staging a 25 MB fixture tree on every call is how a K=5 run spends more
    time copying duckdb files than running the model, so the staged tree is
    cached. It is only safe to cache it if the cache can tell that a source
    file moved, which is what this is: relative path, size and mtime for every
    input file, in sorted order.
    """
    h = hashlib.sha256()
    for root in roots:
        if root is None or not root.is_dir():
            continue
        h.update(root.as_posix().encode("utf-8"))
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if root.name == "fixtures" and "variants" in path.relative_to(root).parts:
                continue
            st = path.stat()
            h.update(f"|{path.relative_to(root).as_posix()}|{st.st_size}|"
                     f"{st.st_mtime_ns}".encode("utf-8"))
    return h.hexdigest()


def stage_fixtures(task_root: Path, variant: Variant,
                   dest: Path | None = None) -> Path:
    """The fixtures tree this run reads, with the variant's overlay applied.

    A variant with no overlay reads the package's own `fixtures/` directly:
    nothing is copied, so `default` cannot differ from the shipped context by
    so much as a byte-order mark, and test_variants.py proves that by building
    the world both ways.

    A variant WITH overlays gets `fixtures/` copied to a cache directory (minus
    `variants/`, which a variant does not ship to itself) and each overlay tree
    copied over the top IN THE ORDER DECLARED: base first, then overlay 1, then
    overlay 2, last write wins. The order is the task's, written down in
    task.toml, so two arms that share a base overlay declare it rather than
    duplicating it.
    """
    fixtures = Path(task_root) / "fixtures"
    if not fixtures.is_dir():
        raise TaskSpecError(f"{task_root} has no fixtures/ directory")
    if not variant.overlays:
        return fixtures
    dest = Path(dest) if dest else STAGE_ROOT / Path(task_root).name / variant.name
    stamp = _tree_stamp(fixtures, *variant.overlays)
    marker = dest / ".variant-stamp"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == stamp:
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fixtures, dest, ignore=_STAGE_IGNORE)
    for overlay in variant.overlays:
        shutil.copytree(overlay, dest, dirs_exist_ok=True,
                        ignore=_STAGE_IGNORE)
    marker.write_text(stamp, encoding="utf-8", newline="")
    return dest


def fixtures_dir(task_root: Path, requested: str | None = None) -> Path:
    """`stage_fixtures` for a task-side script that has no TaskSpec.

    `tests/build_world.py` and `tests/stage_surfaces.py` are started by the
    harness as subprocesses and read their fixtures by path; this is the one
    call that turns `ALKERA_TASK_VARIANT` into the tree they should read.
    """
    task_root = Path(task_root)
    raw = tomllib.loads((task_root / "task.toml").read_text(encoding="utf-8"))
    variants = _declared_variants(task_root.name, task_root, raw)
    return stage_fixtures(task_root,
                          resolve_variant(task_root.name, variants, requested))


def variant_of(task_root: Path, requested: str | None = None) -> Variant:
    """The resolved Variant for a task-side script. See `fixtures_dir`."""
    task_root = Path(task_root)
    raw = tomllib.loads((task_root / "task.toml").read_text(encoding="utf-8"))
    return resolve_variant(task_root.name,
                           _declared_variants(task_root.name, task_root, raw),
                           requested)


def prompt_variant_of(spec: "TaskSpec") -> str:
    """Which declared prompt variant is the brief this rollout will ship?

    `prompts/variant.py identify` does this for a finished run directory; a run
    must be able to name which ticket produced it, and so must every rate in the
    report. Reading it here means the answer is IN the rollout record rather
    than re-derived later from a prompt file that may since have moved.
    """
    import hashlib
    if not spec.prompt_variants:
        return "n/a (task declares no prompt variants)"
    digest = hashlib.sha256(spec.instruction_path.read_bytes()).hexdigest()
    for name, sha in sorted(spec.prompt_variants.items()):
        if sha == digest:
            return name
    return (f"UNLOCKED (brief sha256 {digest[:12]} matches none of "
            f"{sorted(spec.prompt_variants)})")


def load(task_id: str, tasks_dir: Path | None = None,
         variant: str | None = None) -> TaskSpec:
    root = (tasks_dir or TASKS_DIR) / task_id
    toml_path = root / "task.toml"
    if not toml_path.exists():
        raise TaskSpecError(f"no task.toml at {toml_path}")
    raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    variants = _declared_variants(task_id, root, raw)
    arm = resolve_variant(task_id, variants, variant)

    harness = raw.get("harness")
    if harness is None:
        raise TaskSpecError(
            f"{task_id}/task.toml has no [harness] section. The harness will not "
            "guess a world builder, a grader argv or a pass threshold - each of "
            "those decides the measured number and belongs to the task.")

    env = raw.get("environment", {})
    limits = raw.get("limits", {})
    grading = raw.get("grading", {})

    declared_prompts = raw.get("prompt_variants", {}) or {}
    instruction_name = raw.get("agent_visible", {}).get("brief", "instruction.md")
    if arm.prompt:
        # The arm names the brief it was MEASURED under, so replaying it takes
        # one flag rather than a flag plus a `variant.py select`. It must be a
        # declared prompt variant: an arm pointing at an unlocked prompt file
        # would silently change what the agent read.
        block = declared_prompts.get(arm.prompt)
        if not isinstance(block, dict) or not block.get("file"):
            raise TaskSpecError(
                f"{task_id} [variants].{arm.name}.prompt = {arm.prompt!r} is "
                f"not a declared [prompt_variants] entry with a `file`.")
        instruction_name = str(block["file"])
    instruction_path = root / instruction_name
    if not instruction_path.exists():
        raise TaskSpecError(f"agent_visible brief not found: {instruction_path}")

    requires = _require(harness, "pass_requires_checks", f"{task_id} [harness]")
    if not requires:
        raise TaskSpecError(f"{task_id} [harness].pass_requires_checks is empty")

    probes = list(harness.get("surface_probes", []))
    required_surfaces = list(harness.get("required_surfaces", []))
    dead_by_design = _dead_by_design(task_id, harness)
    _check_surface_declarations(task_id, probes, required_surfaces, dead_by_design)

    prompt_variants = {name: str(v["sha256"])
                       for name, v in declared_prompts.items()
                       if isinstance(v, dict) and v.get("sha256")}

    return TaskSpec(
        task_id=task_id,
        root=root,
        raw=raw,
        instruction=instruction_path.read_text(encoding="utf-8"),
        seed=int(env.get("seed", 0)),
        virtual_time=str(env.get("virtual_now", "")),
        wall_clock_seconds=int(_require(limits, "wall_clock_seconds", f"{task_id} [limits]")),
        max_tool_calls=int(_require(limits, "max_tool_calls", f"{task_id} [limits]")),
        memory_mb=int(limits.get("memory_mb", 0)),
        disk_mb=int(limits.get("disk_mb", 0)),
        artifact_in_world=str(_require(grading, "artifact", f"{task_id} [grading]")),
        grade_entrypoint=root / _require(grading, "entrypoint", f"{task_id} [grading]"),
        grade_manifest=(root / str(grading["manifest"])) if grading.get("manifest") else None,
        grade_argv=list(_require(harness, "grade_argv", f"{task_id} [harness]")),
        world_builder=root / _require(harness, "world_builder", f"{task_id} [harness]"),
        instruction_path=instruction_path,
        surface_probes=probes,
        required_surfaces=required_surfaces,
        dead_by_design=dead_by_design,
        prompt_variants=prompt_variants,
        prompt_variant_default=str(declared_prompts.get("default", "")),
        variant=arm.name,
        variant_note=arm.note,
        variant_overlays=arm.overlays,
        variant_grading=dict(arm.grading),
        variant_names=sorted(variants),
        # An arm may move the pass criterion - a stacked arm has two more
        # required checks than the context it stacks on - and when it does, the
        # override is DECLARED on the arm rather than inferred, so a report can
        # print the criterion it actually applied.
        pass_requires_checks=list(
            arm.grading.get("pass_requires_checks") or requires),
        pass_min_total=float(
            arm.grading.get("pass_min_total",
                            _require(harness, "pass_min_total",
                                     f"{task_id} [harness]"))),
        matrix=_matrix_spec(task_id, root, raw.get("matrix"), arm.matrix,
                            raw=raw, arm_role=arm.role),
    )
