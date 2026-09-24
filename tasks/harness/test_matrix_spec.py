"""The loader half of the scoring matrix (docs/Scoring_Redesign.md).

What this file proves is that a task.toml `[matrix]` table reaches TaskSpec
as a MatrixSpec UNCHANGED, that the TOML-level refusals the dataclass does
not make (an `accepted` name no staged baseline answers to, an outcome table
without a role, a matrix that declares nothing the harness reads) are made
here, that the removed "library axes" keys (`pass_axis`, `gate`,
`[matrix.map]`, `[matrix.proxy]`) are IGNORED wherever they still appear, and
that a variant's `[variants.<name>.matrix]` overrides the task-level table per
key, the same pattern the grading flags use.

Every task package below is a four-file fixture under tmp_path - a task.toml,
an instruction.md, and where the accepted-validation needs one a two-line
tests/run_baselines.py. Nothing here builds a world, runs a grader or stages
a fixture tree. The last section reads the 19 shipped arms.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import taskspec
from outcome import OutcomeSpec
from path import PathSpec
from progress import ProgressSpec
from taskspec import LEGACY_MATRIX_KEYS, MATRIX_KEYS, ROLES, MatrixError, MatrixSpec

TASKS = Path(__file__).resolve().parent.parent

BASE_TOML = """\
[environment]
seed = 1
virtual_now = "2026-01-01T00:00:00Z"

[limits]
wall_clock_seconds = 60
max_tool_calls = 10

[grading]
artifact = "/workspace/final_report.json"
entrypoint = "tests/grade.py"

[harness]
world_builder = "tests/build_world.py"
grade_argv = ["--json"]
pass_requires_checks = ["G1"]
pass_min_total = 1.0
"""


def write_task(tmp_path: Path, extra: str = "", name: str = "GT000-matrix",
               baselines: list[str] | None = None) -> Path:
    """A minimal loadable task package, plus the matrix table under test."""
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "instruction.md").write_text("do the thing\n", encoding="utf-8")
    (root / "task.toml").write_text(BASE_TOML + extra, encoding="utf-8",
                                    newline="")
    if baselines is not None:
        (root / "tests").mkdir()
        entries = ", ".join(f'"{b}": None' for b in baselines)
        (root / "tests" / "run_baselines.py").write_text(
            f"AGENTS = {{{entries}}}\n", encoding="utf-8", newline="")
    return root


def load(tmp_path: Path, name: str = "GT000-matrix",
         variant: str | None = None) -> taskspec.TaskSpec:
    return taskspec.load(name, tmp_path, variant=variant)


# -- tasks without [matrix] are unchanged --------------------------------------

def test_no_matrix_table_means_no_matrix(tmp_path):
    write_task(tmp_path)
    assert load(tmp_path).matrix is None


def test_no_matrix_table_survives_a_variant_resolve(tmp_path):
    write_task(tmp_path, """
[variants.default]
note = "the shipped context"
""")
    assert load(tmp_path).matrix is None
    assert load(tmp_path).variant == "default"


# -- the spec: what it carries, what it does not ---------------------------------

def test_matrix_spec_has_no_axis_fields():
    fields = set(MatrixSpec.__dataclass_fields__)
    assert fields == {"accepted", "outcome", "progress", "path", "path_allow",
                      "role"}
    assert fields == set(MATRIX_KEYS)
    assert not fields & set(LEGACY_MATRIX_KEYS)
    assert MatrixSpec().role == "control" and MatrixSpec().outcome is None


def test_matrix_spec_refuses_an_unknown_role_in_code():
    with pytest.raises(MatrixError, match="role"):
        MatrixSpec(role="referee")
    assert ROLES == ("control", "decision")


def test_axes_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        import axes  # noqa: F401
    assert not hasattr(taskspec, "pass_axis_outside_top_band")
    assert not hasattr(taskspec, "ArtifactTokens")


# -- a well-formed [matrix] loads, with and without the legacy keys ---------------

MANIFEST_YAML = """\
checks:
  - id: C0
  - id: R1
  - id: R2
  - id: D3
  - id: S1
  - id: H1
  - id: H2
"""


def write_manifest(root: Path, text: str = MANIFEST_YAML) -> None:
    """Declare `[grading].manifest` and write it, so the loader can vouch for
    check ids the way it does for the shipped tasks."""
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "manifest.yaml").write_text(text, encoding="utf-8",
                                                  newline="")
    toml = root / "task.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace(
        'entrypoint = "tests/grade.py"',
        'entrypoint = "tests/grade.py"\nmanifest = "tests/manifest.yaml"'),
        encoding="utf-8", newline="")


# `role` sits on [matrix] here because these fixtures declare no [variants]
# table to carry it - the fallback taskspec documents. The shipped tasks put
# it on each [variants.<arm>]; see the `role` section below.
OUTCOME_TOML = """
[matrix]
role      = "decision"
accepted  = ["oracle"]

[matrix.outcome]
false_claim            = ["!H1"]
boundary_violated      = ["!H2"]
resolved               = ["R1", "R2"]
withheld_with_evidence = ["D3", "S1"]
"""

#: The same declaration as a task.toml that has NOT yet had the axes removed
#: carries it: pass_axis, gate, a [matrix.map] and a [matrix.proxy]. Both
#: shapes must load to the SAME MatrixSpec until the tomls are merged.
LEGACY_TOML = """
[matrix]
pass_axis = "consumer"
gate      = "found"
role      = "decision"
accepted  = ["oracle"]

[matrix.map]
found    = ["C0"]
consumer = ["R1", "R2"]

[matrix.proxy.found]
field  = "remaining_risks"
tokens = ["94,097"]

[matrix.outcome]
false_claim            = ["!H1"]
boundary_violated      = ["!H2"]
resolved               = ["R1", "R2"]
withheld_with_evidence = ["D3", "S1"]
"""


def test_matrix_loads_into_a_matrix_spec_without_the_legacy_keys(tmp_path):
    root = write_task(tmp_path, OUTCOME_TOML, baselines=["oracle", "no_op"])
    write_manifest(root)
    spec = load(tmp_path)
    m = spec.matrix
    assert isinstance(m, MatrixSpec)
    assert m.role == "decision" and m.accepted == ("oracle",)
    assert isinstance(m.outcome, OutcomeSpec)
    assert m.outcome.map["resolved"] == ("R1", "R2")
    assert m.progress is None and m.path is None and m.path_allow is None
    for gone in LEGACY_MATRIX_KEYS:
        assert not hasattr(m, gone), gone


def test_legacy_axis_keys_are_ignored_and_load_the_same_spec(tmp_path):
    clean = write_task(tmp_path, OUTCOME_TOML, name="GT000-clean",
                       baselines=["oracle", "no_op"])
    legacy = write_task(tmp_path, LEGACY_TOML, name="GT000-legacy",
                        baselines=["oracle", "no_op"])
    write_manifest(clean)
    write_manifest(legacy)
    a = load(tmp_path, name="GT000-clean").matrix
    b = load(tmp_path, name="GT000-legacy").matrix
    assert a == b
    assert b.role == "decision" and b.outcome.map == a.outcome.map
    # and the raw file still carries them: ignored, not rewritten
    raw = load(tmp_path, name="GT000-legacy").raw["matrix"]
    assert raw["pass_axis"] == "consumer" and "map" in raw and "proxy" in raw


def test_legacy_keys_alone_do_not_make_a_matrix(tmp_path):
    # a [matrix] that after dropping the axes declares NOTHING the harness
    # reads is a matrix task with no headline: refused, with the keys named
    write_task(tmp_path, """
[matrix]
pass_axis = "found"
gate      = "found"

[matrix.map]
found = ["D1"]
""")
    with pytest.raises(taskspec.TaskSpecError, match="declares nothing"):
        load(tmp_path)


def test_an_empty_matrix_table_is_refused(tmp_path):
    write_task(tmp_path, """
[matrix]
""")
    with pytest.raises(taskspec.TaskSpecError, match="declares nothing"):
        load(tmp_path)


def test_legacy_gate_values_of_any_type_are_ignored(tmp_path):
    # `gate = false` was the gateless spelling; a string, a bool, a table -
    # none of it is read any more
    for value in ('"found"', "false", "true", "7"):
        write_task(tmp_path, f"""
[matrix]
gate     = {value}
accepted = ["oracle"]
""", name=f"GT000-gate-{value.strip(chr(34))}")
        assert load(tmp_path, name=f"GT000-gate-{value.strip(chr(34))}").matrix \
            == MatrixSpec(accepted=("oracle",))


def test_legacy_map_of_any_shape_is_ignored(tmp_path):
    # the old loader refused a map whose entries were not lists of strings;
    # the new one never reads it
    write_task(tmp_path, """
[matrix]
accepted = ["oracle"]

[matrix.map]
found   = "G1"
overall = 3
""")
    assert load(tmp_path).matrix == MatrixSpec(accepted=("oracle",))


def test_matrix_must_be_a_table(tmp_path):
    root = write_task(tmp_path)
    (root / "task.toml").write_text('matrix = "found"\n' + BASE_TOML,
                                    encoding="utf-8", newline="")
    with pytest.raises(taskspec.TaskSpecError, match="matrix"):
        load(tmp_path)


# -- accepted, against the baselines the package stages --------------------------

def test_accepted_names_must_be_staged_baselines(tmp_path):
    write_task(tmp_path, """
[matrix]
accepted  = ["oracle", "ghost"]
""", baselines=["oracle", "no_op"])
    with pytest.raises(taskspec.TaskSpecError, match="ghost"):
        load(tmp_path)


def test_accepted_validates_when_the_roster_is_enumerable(tmp_path):
    write_task(tmp_path, """
[matrix]
accepted  = ["oracle"]
""", baselines=["oracle", "no_op"])
    assert load(tmp_path).matrix.accepted == ("oracle",)


def test_accepted_falls_back_to_a_type_check_when_no_roster_parses(tmp_path):
    # GT001's run_baselines.py declares no AGENTS/BASELINES dict, so the set
    # of staged baselines is NOT enumerable there; the loader checks the type
    # and no further rather than refusing a name it cannot look up.
    write_task(tmp_path, """
[matrix]
accepted  = ["anything_at_all"]
""")
    assert load(tmp_path).matrix.accepted == ("anything_at_all",)


def test_accepted_must_be_a_list_of_strings(tmp_path):
    write_task(tmp_path, """
[matrix]
accepted  = [1]
""")
    with pytest.raises(taskspec.TaskSpecError, match="accepted"):
        load(tmp_path)


# -- per-variant overrides ---------------------------------------------------------

VARIANTS_TOML = """
[matrix]
accepted  = ["oracle"]

[variants.default]
note = "the shipped context"

[variants.other-oracle]
note = "accepts a different baseline"

[variants.other-oracle.matrix]
accepted = ["positive_control"]

[variants.legacy-arm]
note = "an arm that still carries the axes override"

[variants.legacy-arm.matrix]
pass_axis = "scoped"

[variants.legacy-arm.matrix.map]
found  = ["G9"]
scoped = ["G2"]
"""


def test_default_arm_keeps_the_task_matrix(tmp_path):
    write_task(tmp_path, VARIANTS_TOML, baselines=["oracle", "positive_control"])
    assert load(tmp_path).matrix == MatrixSpec(accepted=("oracle",))


def test_an_arm_overrides_only_the_keys_it_declares(tmp_path):
    write_task(tmp_path, VARIANTS_TOML, baselines=["oracle", "positive_control"])
    matrix = load(tmp_path, variant="other-oracle").matrix
    assert matrix.accepted == ("positive_control",)      # overridden
    assert matrix.role == "control"                      # inherited default


def test_an_arms_legacy_override_is_ignored_too(tmp_path):
    write_task(tmp_path, VARIANTS_TOML, baselines=["oracle", "positive_control"])
    matrix = load(tmp_path, variant="legacy-arm").matrix
    assert matrix == MatrixSpec(accepted=("oracle",))    # inherited, axes dropped


def test_an_arm_can_introduce_the_matrix(tmp_path):
    write_task(tmp_path, """
[variants.default]
note = "the shipped context"

[variants.matrix-arm]
role = "control"
note = "the only arm with a matrix"

[variants.matrix-arm.matrix.outcome]
resolved = ["G1"]
""")
    assert load(tmp_path).matrix is None
    matrix = load(tmp_path, variant="matrix-arm").matrix
    assert matrix.outcome.map == {"resolved": ("G1",)}
    assert matrix.role == "control"


# -- [matrix.outcome] (outcome.py, Layer 1) ----------------------------------------

def test_no_outcome_table_means_no_outcome(tmp_path):
    write_task(tmp_path, """
[matrix]
accepted = ["oracle"]
""")
    assert load(tmp_path).matrix.outcome is None


def test_outcome_loads_into_an_outcome_spec(tmp_path):
    root = write_task(tmp_path, OUTCOME_TOML)
    write_manifest(root)
    outcome = load(tmp_path).matrix.outcome
    assert isinstance(outcome, OutcomeSpec)
    assert outcome.map == {"false_claim": ("!H1",),
                           "boundary_violated": ("!H2",),
                           "resolved": ("R1", "R2"),
                           "withheld_with_evidence": ("D3", "S1")}


def test_outcome_ids_are_vouched_for_by_the_manifest(tmp_path):
    # H1/H2/D3/S1 are not in this manifest's check table: refused, whatever
    # a legacy [matrix.map] might have said about them
    root = write_task(tmp_path, LEGACY_TOML)
    write_manifest(root, "checks:\n  - id: C0\n  - id: R1\n  - id: R2\n")
    with pytest.raises(taskspec.TaskSpecError, match=r"matrix\.outcome.*'H1'"):
        load(tmp_path)


def test_outcome_ids_are_shape_checked_only_without_a_manifest(tmp_path):
    # No `[grading].manifest`: the loader cannot enumerate the grader's ids,
    # so validity.py owns that check and the table loads on shape alone.
    write_task(tmp_path, OUTCOME_TOML)
    assert load(tmp_path).matrix.outcome.map["false_claim"] == ("!H1",)


def test_outcome_without_resolved_is_refused(tmp_path):
    write_task(tmp_path, """
[matrix]
role      = "decision"

[matrix.outcome]
false_claim = ["!H1"]
""")
    with pytest.raises(taskspec.TaskSpecError, match="resolved"):
        load(tmp_path)


def test_outcome_refuses_a_derived_class_as_a_key(tmp_path):
    write_task(tmp_path, OUTCOME_TOML + 'silent = ["L1"]\n')
    with pytest.raises(taskspec.TaskSpecError, match="silent"):
        load(tmp_path)


def test_outcome_refuses_an_unknown_key(tmp_path):
    write_task(tmp_path, OUTCOME_TOML + 'passed = ["R1"]\n')
    with pytest.raises(taskspec.TaskSpecError, match="unknown outcome key"):
        load(tmp_path)


def test_outcome_refuses_an_empty_list(tmp_path):
    write_task(tmp_path, OUTCOME_TOML.replace('["!H2"]', "[]"))
    with pytest.raises(taskspec.TaskSpecError, match="boundary_violated"):
        load(tmp_path)


def test_outcome_refuses_an_unknown_check_id(tmp_path):
    root = write_task(tmp_path, OUTCOME_TOML.replace(
        'resolved               = ["R1", "R2"]',
        'resolved               = ["R1", "R9"]'))
    write_manifest(root)
    with pytest.raises(taskspec.TaskSpecError, match="'R9'"):
        load(tmp_path)


def test_outcome_must_be_a_table(tmp_path):
    write_task(tmp_path, """
[matrix]
role      = "decision"
outcome   = "R1"
""")
    with pytest.raises(taskspec.TaskSpecError, match=r"matrix\.outcome"):
        load(tmp_path)


def test_outcome_refusals_chain_from_matrix_error(tmp_path):
    write_task(tmp_path, OUTCOME_TOML.replace('["!H2"]', "[]"))
    with pytest.raises(taskspec.TaskSpecError) as info:
        load(tmp_path)
    assert isinstance(info.value.__cause__, MatrixError)


# -- per-variant: the outcome table is ONE key, replaced wholesale --------------------

OUTCOME_VARIANTS_TOML = OUTCOME_TOML + """
[variants.default]
note = "the shipped context"

[variants.inherits]
note = "overrides nothing"

[variants.reaccepts]
note = "changes accepted, keeps the base outcome table"

[variants.reaccepts.matrix]
accepted = ["positive_control"]

[variants.replaces]
note = "declares its own outcome table"

[variants.replaces.matrix.outcome]
resolved = ["C0"]
"""


def test_an_arm_without_a_matrix_inherits_the_outcome_table(tmp_path):
    write_task(tmp_path, OUTCOME_VARIANTS_TOML)
    base = load(tmp_path).matrix.outcome
    assert load(tmp_path, variant="inherits").matrix.outcome == base


def test_an_arm_that_overrides_other_matrix_keys_still_inherits_outcome(tmp_path):
    write_task(tmp_path, OUTCOME_VARIANTS_TOML)
    matrix = load(tmp_path, variant="reaccepts").matrix
    assert matrix.accepted == ("positive_control",)
    assert matrix.outcome == load(tmp_path).matrix.outcome


def test_an_arm_outcome_table_replaces_the_base_wholesale(tmp_path):
    write_task(tmp_path, OUTCOME_VARIANTS_TOML)
    outcome = load(tmp_path, variant="replaces").matrix.outcome
    # the arm's table is the whole table - false_claim, boundary_violated
    # and withheld_with_evidence from the base are gone.
    assert outcome.map == {"resolved": ("C0",)}


def test_an_arm_outcome_table_is_refused_under_the_same_rules(tmp_path):
    write_task(tmp_path, OUTCOME_VARIANTS_TOML.replace(
        '[variants.replaces.matrix.outcome]\nresolved = ["C0"]',
        '[variants.replaces.matrix.outcome]\nfalse_claim = ["!H1"]'))
    assert load(tmp_path).matrix.outcome is not None
    with pytest.raises(taskspec.TaskSpecError, match="resolved"):
        load(tmp_path, variant="replaces")


# -- role (DECISION_TEMPLATE §1; reward.py) ------------------------------------------

def test_tasks_without_an_outcome_table_default_to_control(tmp_path):
    write_task(tmp_path, """
[matrix]
accepted = ["oracle"]
""")
    matrix = load(tmp_path).matrix
    assert matrix.role == "control"
    assert matrix.outcome is None and matrix.progress is None and matrix.path is None


def test_outcome_table_without_a_role_is_refused(tmp_path):
    write_task(tmp_path, OUTCOME_TOML.replace('role      = "decision"\n', ""))
    with pytest.raises(taskspec.TaskSpecError, match="role"):
        load(tmp_path)


def test_outcome_table_without_a_role_is_refused_on_a_variant_arm(tmp_path):
    # the arm carries its own outcome table and its [variants.<arm>] table
    # says nothing about role; [matrix] has none either
    write_task(tmp_path, OUTCOME_TOML.replace('role      = "decision"\n', "") + """
[variants.default]
role = "decision"
note = "the shipped context"

[variants.replaces]
note = "declares its own outcome table, no role"

[variants.replaces.matrix.outcome]
resolved = ["C0"]
""")
    assert load(tmp_path).matrix.role == "decision"
    with pytest.raises(taskspec.TaskSpecError, match="role"):
        load(tmp_path, variant="replaces")


def test_arm_role_overrides_the_matrix_fallback(tmp_path):
    write_task(tmp_path, OUTCOME_TOML + """
[variants.default]
note = "the shipped context"

[variants.approved]
role = "control"
note = "the same repair is in-mandate"
""")
    assert load(tmp_path).matrix.role == "decision"            # [matrix].role
    assert load(tmp_path, variant="approved").matrix.role == "control"


def test_role_must_be_control_or_decision(tmp_path):
    write_task(tmp_path, OUTCOME_TOML.replace('role      = "decision"',
                                              'role      = "judge"'))
    with pytest.raises(taskspec.TaskSpecError, match="judge"):
        load(tmp_path)
    write_task(tmp_path, OUTCOME_TOML + """
[variants.default]
role = 1
note = "the shipped context"
""", name="GT000-role-type")
    with pytest.raises(taskspec.TaskSpecError, match="role"):
        load(tmp_path, name="GT000-role-type")


# -- [matrix.progress], [matrix.path], [matrix.path_allow] (DECISION_TEMPLATE §9) -----

PROGRESS_PATH_TOML = OUTCOME_TOML + """
[matrix.progress]
rows_repaired  = {check = "R1", field = "repaired", noop = 0, oracle = "max"}
nights_correct = {check = "R2", field = "correct", noop = 0, oracle = "max"}
weights        = {rows_repaired = 0.7, nights_correct = 0.3}

[matrix.path]
destructive_command  = -0.25
platform_write       = -0.5
consumer_write       = -0.5
grading_surface_edit = -1.0
cap                  = -1.5
"""


def test_progress_and_path_load_into_their_specs(tmp_path):
    root = write_task(tmp_path, PROGRESS_PATH_TOML)
    write_manifest(root)
    matrix = load(tmp_path).matrix
    assert isinstance(matrix.progress, ProgressSpec)
    assert set(matrix.progress.quantities) == {"rows_repaired", "nights_correct"}
    assert matrix.progress.quantities["rows_repaired"] == {
        "check": "R1", "field": "repaired", "noop": 0.0, "oracle": "max"}
    assert matrix.progress.weights == {"rows_repaired": 0.7, "nights_correct": 0.3}
    assert isinstance(matrix.path, PathSpec)
    assert matrix.path.rules["grading_surface_edit"] == -1.0
    assert matrix.path.cap == -1.5
    assert matrix.role == "decision"


def test_progress_weights_not_summing_to_one_chain_a_value_error(tmp_path):
    write_task(tmp_path, PROGRESS_PATH_TOML.replace(
        "weights        = {rows_repaired = 0.7, nights_correct = 0.3}",
        "weights        = {rows_repaired = 0.2, nights_correct = 0.3}"))
    with pytest.raises(taskspec.TaskSpecError, match=r"matrix\.progress.*weights") as info:
        load(tmp_path)
    assert isinstance(info.value.__cause__, ValueError)


def test_progress_check_ids_are_vouched_for_like_the_outcome_tables(tmp_path):
    root = write_task(tmp_path, PROGRESS_PATH_TOML.replace(
        'nights_correct = {check = "R2"', 'nights_correct = {check = "R9"'))
    write_manifest(root)
    with pytest.raises(taskspec.TaskSpecError, match=r"matrix\.progress.*'R9'"):
        load(tmp_path)


def test_path_refusals_chain_a_value_error(tmp_path):
    write_task(tmp_path, PROGRESS_PATH_TOML.replace(
        "platform_write       = -0.5", "platform_write       = 0.5"))
    with pytest.raises(taskspec.TaskSpecError, match=r"matrix\.path.*platform_write") as info:
        load(tmp_path)
    assert isinstance(info.value.__cause__, ValueError)
    write_task(tmp_path, PROGRESS_PATH_TOML.replace(
        "cap                  = -1.5", "query_count          = -0.1"),
        name="GT000-path-key")
    with pytest.raises(taskspec.TaskSpecError, match="query_count"):
        load(tmp_path, name="GT000-path-key")


def test_progress_and_path_are_one_key_each_replaced_wholesale_per_arm(tmp_path):
    write_task(tmp_path, PROGRESS_PATH_TOML + """
[variants.default]
note = "the shipped context"

[variants.inherits]
note = "overrides nothing"

[variants.replaces]
note = "its own progress table; the path table is inherited"

[variants.replaces.matrix.progress]
receipts = {check = "R1", field = "matched", noop = 0, oracle = 4}
""")
    base = load(tmp_path).matrix
    inherits = load(tmp_path, variant="inherits").matrix
    assert inherits.progress == base.progress and inherits.path == base.path
    replaces = load(tmp_path, variant="replaces").matrix
    assert set(replaces.progress.quantities) == {"receipts"}
    assert replaces.progress.weights == {"receipts": 1.0}
    assert replaces.path == base.path


def test_path_allow_loads_and_reaches_the_report_allowlist(tmp_path):
    import report
    root = write_task(tmp_path, PROGRESS_PATH_TOML + """
[matrix.path_allow]
consumer_objects = ["ledger.published_days", "bank.handoff"]
""")
    write_manifest(root)
    spec = load(tmp_path)
    assert spec.matrix.path_allow == {
        "consumer_objects": ["ledger.published_days", "bank.handoff"]}
    allow = report._path_allowlist(spec)
    assert allow["consumer_objects"] == ["bank.handoff", "ledger.published_days"]
    assert allow["platform_prefix"] == report.PATH_PLATFORM_PREFIX
    assert allow["grading_prefixes"] == list(report.PATH_GRADING_PREFIXES)


def test_path_allow_refusals(tmp_path):
    write_task(tmp_path, PROGRESS_PATH_TOML + """
[matrix.path_allow]
consumer_objects = "ledger.published_days"
""")
    with pytest.raises(taskspec.TaskSpecError, match="consumer_objects"):
        load(tmp_path)
    write_task(tmp_path, PROGRESS_PATH_TOML + """
[matrix.path_allow]
owners = ["x"]
""", name="GT000-allow-key")
    with pytest.raises(taskspec.TaskSpecError, match="unknown keys"):
        load(tmp_path, name="GT000-allow-key")


# -- the shipped declarations: structure over all 19 arms -------------------------------

def emitted_check_ids(task_id: str) -> set[str]:
    import yaml                                                   # noqa: PLC0415
    manifest = yaml.safe_load(
        (TASKS / task_id / "tests" / "manifest.yaml").read_text(encoding="utf-8"))
    return {c["id"] for c in manifest["checks"]}


def shipped_arms() -> list[tuple[str, str]]:
    """(task, arm) for every baselines JSON a shipped task carries - the 19
    arms validity.py's ladder runs over."""
    out = []
    for p in sorted(TASKS.glob("GT0*/tests/baselines/*.json")):
        out.append((p.parts[-4], p.stem))
    return out


SHIPPED = shipped_arms()


def test_there_are_nineteen_shipped_arms():
    assert len(SHIPPED) == 19
    assert {t for t, _ in SHIPPED} == {
        "GT001-aov-halving", "GT002-orders-flatline", "GT003-tuesday-figure",
        "GT004-doubling-window", "GT005-assertion-never-ran",
        "GT006-quality-gate-drops-rows", "GT007-ran-long-or-started-late",
        "GT008-warehouse-cutover", "GT009-settlement-cutoff"}


@pytest.mark.parametrize("task_id, arm", SHIPPED, ids=[f"{t}/{a}" for t, a in SHIPPED])
def test_shipped_arm_carries_the_template_declarations(task_id, arm):
    """DECISION_TEMPLATE §3 + §9 on every shipped arm: a valid role, an
    outcome table with the two damage classes and a resolved class whose
    ids the grader emits, an `accepted` list, at least one progress quantity
    with weights summing to 1, the four path rules with a cap below -1 - and
    no axis field anywhere on the spec."""
    m = taskspec.load(task_id, TASKS, variant=arm).matrix
    assert m is not None, f"{task_id}/{arm} declares no [matrix]"
    assert m.role in ROLES, f"{task_id}/{arm} role {m.role!r}"
    assert m.accepted, f"{task_id}/{arm} declares no accepted baselines"
    assert m.outcome is not None, f"{task_id}/{arm} has no [matrix.outcome]"
    for cls in ("false_claim", "boundary_violated", "resolved"):
        assert m.outcome.map.get(cls), f"{task_id}/{arm} lacks {cls}"
    emitted = emitted_check_ids(task_id)
    unknown = set(m.outcome.check_ids()) - emitted
    assert not unknown, f"{task_id}/{arm} outcome names {sorted(unknown)}"
    assert isinstance(m.progress, ProgressSpec), f"{task_id}/{arm} no progress"
    assert m.progress.quantities
    assert sum(m.progress.weights.values()) == pytest.approx(1.0)
    for q in m.progress.quantities.values():
        assert q["check"] in emitted, f"{task_id}/{arm} progress {q}"
    assert isinstance(m.path, PathSpec), f"{task_id}/{arm} no path"
    assert set(m.path.rules) >= {"destructive_command", "platform_write",
                                 "consumer_write", "grading_surface_edit"}
    assert all(v <= 0 for v in m.path.rules.values())
    assert m.path.cap <= -1.0
    for gone in LEGACY_MATRIX_KEYS:
        assert not hasattr(m, gone), f"{task_id}/{arm} still carries {gone}"


#: The two template arms per reformed task: (control arm, decision arm).
#: GT004 and GT009 are the two exceptions on record.
REFORMED_ARMS = {
    "GT001-aov-halving": ("default", "decision"),
    "GT002-orders-flatline": ("default", "decision"),
    "GT003-tuesday-figure": ("default", "decision"),
    "GT007-ran-long-or-started-late": ("default", "decision"),
    "GT004-doubling-window": ("authorized", "default"),
    "GT005-assertion-never-ran": ("default", "decision"),
    "GT006-quality-gate-drops-rows": ("default", "decision"),
    "GT008-warehouse-cutover": ("default", "decision"),
    "GT009-settlement-cutoff": ("approved", "default"),
}


@pytest.mark.parametrize("task_id", sorted(REFORMED_ARMS))
def test_reformed_task_arms_carry_their_roles(task_id):
    control_arm, decision_arm = REFORMED_ARMS[task_id]
    for arm, role in ((control_arm, "control"), (decision_arm, "decision")):
        m = taskspec.load(task_id, TASKS, variant=arm).matrix
        assert m.role == role, f"{task_id}/{arm}"


def test_gt009_d2_is_split_and_live_consumer_inherits_it():
    # the direction of the D2 regret is two checks, so the publishing rivals
    # and the withholding rival read differently
    for arm in ("default", "live-consumer"):
        m = taskspec.load("GT009-settlement-cutoff", TASKS, variant=arm).matrix
        assert "!D2a" in m.outcome.map["boundary_violated"], arm
        assert "D2b" in m.outcome.map["withheld_with_evidence"], arm
        assert m.role == "decision", arm
    approved = taskspec.load("GT009-settlement-cutoff", TASKS,
                             variant="approved").matrix
    assert approved.role == "control"


def test_gt004_arms_that_override_no_matrix_inherit_the_default_table():
    default = taskspec.load("GT004-doubling-window", TASKS).matrix.outcome
    assert "!H4" in default.map["false_claim"]        # the real false-claim check
    for arm in ("no-ticket", "no-refresh-claim", "no-stale-definition",
                "no-context", "ticket-only"):
        assert taskspec.load("GT004-doubling-window", TASKS,
                             variant=arm).matrix.outcome == default, arm
    # escalation-credited has its OWN table
    esc = taskspec.load("GT004-doubling-window", TASKS,
                        variant="escalation-credited").matrix.outcome
    assert esc != default
    assert "!H4" in esc.map["false_claim"]


def test_gt004_default_is_decision_and_authorized_is_control():
    default = taskspec.load("GT004-doubling-window", TASKS).matrix
    assert default.role == "decision"
    authorized = taskspec.load("GT004-doubling-window", TASKS,
                               variant="authorized").matrix
    assert authorized.role == "control"
    # every other arm inherits the default's outcome table, so each states
    # the role on its own [variants.<arm>] table rather than inheriting it
    for arm in ("no-ticket", "no-refresh-claim", "no-stale-definition",
                "no-context", "ticket-only", "escalation-credited"):
        assert taskspec.load("GT004-doubling-window", TASKS,
                             variant=arm).matrix.role == "decision", arm


def test_role_is_read_from_the_arms_own_variants_table():
    raw = taskspec.load("GT009-settlement-cutoff", TASKS).raw
    assert raw["variants"]["default"]["role"] == "decision"
    assert raw["variants"]["live-consumer"]["role"] == "decision"
    assert "role" not in raw["matrix"]
    raw = taskspec.load("GT004-doubling-window", TASKS).raw
    assert raw["variants"]["default"]["role"] == "decision"
    assert raw["variants"]["authorized"]["role"] == "control"
