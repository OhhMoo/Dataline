"""The loader half of the scoring matrix (new_matrix.md, Layer A).

axes.py is the frozen contract and test_axes.py proves its semantics; what
this file proves is that a task.toml `[matrix]` table reaches TaskSpec as a
MatrixSpec UNCHANGED, that the TOML-level refusals the dataclass does not make
(an unstated `gate`, an `accepted` name no staged baseline answers to) are
made here, and that a variant's `[variants.<name>.matrix]` overrides the
task-level table per key, the same pattern the grading flags use.

Every task package below is a four-file fixture under tmp_path - a task.toml,
an instruction.md, and where the accepted-validation needs one a two-line
tests/run_baselines.py. Nothing here builds a world, runs a grader or stages
a fixture tree.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import taskspec
from axes import MatrixError, MatrixSpec
from outcome import OutcomeSpec

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


# -- a well-formed [matrix] loads ----------------------------------------------

def test_matrix_loads_into_a_matrix_spec(tmp_path):
    write_task(tmp_path, """
[matrix]
pass_axis = "warranted"
gate      = "found"
accepted  = ["oracle"]

[matrix.map]
found     = ["D1"]
warranted = ["D2", "D3"]
""", baselines=["oracle", "no_op"])
    spec = load(tmp_path)
    assert isinstance(spec.matrix, MatrixSpec)
    assert spec.matrix.pass_axis == "warranted"
    assert spec.matrix.gate == "found"
    assert spec.matrix.accepted == ("oracle",)
    assert spec.matrix.checks_for("warranted") == ("D2", "D3")
    assert spec.matrix.checks_for("scoped") == ()


def test_gate_false_is_the_gateless_control(tmp_path):
    # TOML has no null; `false` is the explicit spelling of axes.py's
    # gate=None, the control task with nothing to find.
    write_task(tmp_path, """
[matrix]
pass_axis = "found"
gate      = false

[matrix.map]
found = ["D5a"]
""")
    assert load(tmp_path).matrix.gate is None


# -- the TOML-level refusals -----------------------------------------------------

def test_pass_axis_without_gate_is_refused(tmp_path):
    # The dataclass defaults gate to 'found' so that CODE can construct one; a
    # declaration that decides a denominator has to state it.
    write_task(tmp_path, """
[matrix]
pass_axis = "found"

[matrix.map]
found = ["D5a"]
""")
    with pytest.raises(taskspec.TaskSpecError, match="gate"):
        load(tmp_path)


def test_gate_without_pass_axis_is_refused(tmp_path):
    write_task(tmp_path, """
[matrix]
gate = "found"
""")
    with pytest.raises(taskspec.TaskSpecError, match="pass_axis"):
        load(tmp_path)


def test_an_empty_matrix_table_is_refused(tmp_path):
    write_task(tmp_path, """
[matrix]
""")
    with pytest.raises(taskspec.TaskSpecError, match="pass_axis"):
        load(tmp_path)


def test_gate_must_be_a_library_axis(tmp_path):
    write_task(tmp_path, """
[matrix]
pass_axis = "found"
gate      = "overall"
""")
    with pytest.raises(taskspec.TaskSpecError, match="GT000-matrix.*gate"):
        load(tmp_path)


def test_pass_axis_without_mapped_checks_is_refused(tmp_path):
    # Otherwise every episode withholds the headline as n/a forever, with no
    # signal but an empty denominator.
    write_task(tmp_path, """
[matrix]
pass_axis = "scoped"
gate      = "found"

[matrix.map]
found = ["D1"]
""")
    with pytest.raises(taskspec.TaskSpecError, match="scoped"):
        load(tmp_path)


def test_gate_without_mapped_checks_is_refused(tmp_path):
    # A gate with no evidence admits-or-withholds on nothing for every
    # episode. `gate = false` is the spelling for a task with nothing to find.
    write_task(tmp_path, """
[matrix]
pass_axis = "found"
gate      = "warranted"

[matrix.map]
found = ["D1"]
""")
    with pytest.raises(taskspec.TaskSpecError, match="warranted"):
        load(tmp_path)


def test_pass_axis_must_be_a_library_axis(tmp_path):
    write_task(tmp_path, """
[matrix]
pass_axis = "score"
gate      = "found"
""")
    with pytest.raises(taskspec.TaskSpecError, match="pass_axis"):
        load(tmp_path)


def test_map_rejects_invented_axes(tmp_path):
    write_task(tmp_path, """
[matrix]
pass_axis = "found"
gate      = "found"

[matrix.map]
overall = ["G1"]
""")
    with pytest.raises(taskspec.TaskSpecError, match="unknown axes"):
        load(tmp_path)


def test_map_entries_must_be_check_id_strings(tmp_path):
    write_task(tmp_path, """
[matrix]
pass_axis = "found"
gate      = "found"

[matrix.map]
found = "G1"
""")
    with pytest.raises(taskspec.TaskSpecError, match=r"matrix\.map"):
        load(tmp_path)


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
pass_axis = "consumer"
gate      = "found"
accepted  = ["oracle", "ghost"]

[matrix.map]
found = ["D1"]
""", baselines=["oracle", "no_op"])
    with pytest.raises(taskspec.TaskSpecError, match="ghost"):
        load(tmp_path)


def test_accepted_validates_when_the_roster_is_enumerable(tmp_path):
    write_task(tmp_path, """
[matrix]
pass_axis = "consumer"
gate      = "found"
accepted  = ["oracle"]

[matrix.map]
found    = ["D1"]
consumer = ["C5"]
""", baselines=["oracle", "no_op"])
    assert load(tmp_path).matrix.accepted == ("oracle",)


def test_accepted_falls_back_to_a_type_check_when_no_roster_parses(tmp_path):
    # GT001's run_baselines.py declares no AGENTS/BASELINES dict, so the set
    # of staged baselines is NOT enumerable there; the loader checks the type
    # and no further rather than refusing a name it cannot look up.
    write_task(tmp_path, """
[matrix]
pass_axis = "consumer"
gate      = "found"
accepted  = ["anything_at_all"]

[matrix.map]
found    = ["D1"]
consumer = ["C5"]
""")
    assert load(tmp_path).matrix.accepted == ("anything_at_all",)


def test_accepted_must_be_a_list_of_strings(tmp_path):
    write_task(tmp_path, """
[matrix]
pass_axis = "found"
gate      = "found"
accepted  = [1]
""")
    with pytest.raises(taskspec.TaskSpecError, match="accepted"):
        load(tmp_path)


# -- per-variant overrides ---------------------------------------------------------

VARIANTS_TOML = """
[matrix]
pass_axis = "found"
gate      = "found"
accepted  = ["oracle"]

[matrix.map]
found    = ["G1"]
consumer = ["G0"]

[variants.default]
note = "the shipped context"

[variants.scoped-arm]
note = "moves the pass axis onto scoped"

[variants.scoped-arm.matrix]
pass_axis = "scoped"

[variants.scoped-arm.matrix.map]
found  = ["G9"]
scoped = ["G2"]
"""


def test_default_arm_keeps_the_task_matrix(tmp_path):
    write_task(tmp_path, VARIANTS_TOML, baselines=["oracle"])
    matrix = load(tmp_path).matrix
    assert matrix.pass_axis == "found"
    assert matrix.checks_for("found") == ("G1",)


def test_an_arm_overrides_only_the_keys_it_declares(tmp_path):
    write_task(tmp_path, VARIANTS_TOML, baselines=["oracle"])
    matrix = load(tmp_path, variant="scoped-arm").matrix
    assert matrix.pass_axis == "scoped"          # overridden
    assert matrix.gate == "found"                # inherited
    assert matrix.accepted == ("oracle",)        # inherited
    # [matrix.map] is ONE key: the arm's table replaces the base's wholesale,
    # it does not merge per axis - the base's consumer list is gone, and the
    # arm re-declares found (the inherited gate needs its evidence).
    assert matrix.checks_for("scoped") == ("G2",)
    assert matrix.checks_for("found") == ("G9",)
    assert matrix.checks_for("consumer") == ()


def test_an_arm_only_matrix_must_state_its_gate(tmp_path):
    write_task(tmp_path, """
[variants.default]
note = "the shipped context"

[variants.matrix-arm]
note = "the only arm with a matrix"

[variants.matrix-arm.matrix]
pass_axis = "warranted"
""")
    assert load(tmp_path).matrix is None
    with pytest.raises(taskspec.TaskSpecError, match="gate"):
        load(tmp_path, variant="matrix-arm")


def test_an_arm_can_introduce_the_matrix(tmp_path):
    write_task(tmp_path, """
[variants.default]
note = "the shipped context"

[variants.matrix-arm]
note = "the only arm with a matrix"

[variants.matrix-arm.matrix]
pass_axis = "warranted"
gate      = "found"

[variants.matrix-arm.matrix.map]
found     = ["G1"]
warranted = ["G1"]
""")
    assert load(tmp_path).matrix is None
    matrix = load(tmp_path, variant="matrix-arm").matrix
    assert matrix.pass_axis == "warranted"
    assert matrix.checks_for("warranted") == ("G1",)


# -- the declarations other sessions already shipped --------------------------------

def test_the_shipped_matrix_tasks_load():
    """All nine shipped tasks carry [matrix] tables; the loader must accept
    exactly what they declare."""
    expected = {
        "GT001-aov-halving": ("found", "found", {"found": ("D5a",)}),
        "GT002-orders-flatline": (
            "scoped", "found",
            {"found": ("K9",), "scoped": ("K1", "K2", "K7")}),
        "GT003-tuesday-figure": (
            "scoped", "found",
            {"found": ("D1a",), "scoped": ("D5", "D1b")}),
        "GT004-doubling-window": (
            "warranted", "found",
            {"found": ("H1b",), "warranted": ("ESC",)}),
        "GT005-assertion-never-ran": ("found", "found", {"found": ("L1",)}),
        "GT006-quality-gate-drops-rows": ("found", "found", {"found": ("H1",)}),
        "GT007-ran-long-or-started-late": (
            "warranted", "found",
            {"found": ("D1",), "warranted": ("S1a", "S1b", "S2")}),
        "GT008-warehouse-cutover": (
            "consumer", "found",
            {"found": ("C2", "C3"), "consumer": ("C5",)}),
        "GT009-settlement-cutoff": (
            "consumer", "found",
            {"found": ("C0",), "warranted": ("D1", "D2", "D3"),
             "consumer": ("R1", "R2", "R3", "R4")}),
    }
    for task_id, (pass_axis, gate, axis_map) in expected.items():
        matrix = taskspec.load(task_id, TASKS).matrix
        assert matrix is not None, f"{task_id} declares [matrix] but loaded None"
        assert (matrix.pass_axis, matrix.gate) == (pass_axis, gate), task_id
        assert matrix.map == axis_map, task_id


# -- [matrix.outcome] (outcome.py, Layer 1) ----------------------------------------

OUTCOME_TOML = """
[matrix]
pass_axis = "consumer"
gate      = "found"

[matrix.map]
found    = ["C0"]
consumer = ["R1", "R2"]

[matrix.outcome]
false_claim            = ["!H1"]
boundary_violated      = ["!H2"]
resolved               = ["R1", "R2"]
withheld_with_evidence = ["D3", "S1"]
"""

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


def test_no_outcome_table_means_no_outcome(tmp_path):
    write_task(tmp_path, """
[matrix]
pass_axis = "found"
gate      = "found"

[matrix.map]
found = ["D1"]
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


def test_outcome_ids_are_vouched_for_by_the_manifest_not_only_the_map(tmp_path):
    # H1/H2/D3/S1 are mapped to no axis (hygiene penalties never are); the
    # manifest's check table is what admits them.
    root = write_task(tmp_path, OUTCOME_TOML)
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
pass_axis = "found"
gate      = "found"

[matrix.map]
found = ["D1"]

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
    # R9 is in neither the map nor the manifest (the map line stays R1/R2).
    root = write_task(tmp_path, OUTCOME_TOML.replace(
        'resolved               = ["R1", "R2"]',
        'resolved               = ["R1", "R9"]'))
    write_manifest(root)
    with pytest.raises(taskspec.TaskSpecError, match="'R9'"):
        load(tmp_path)


def test_outcome_must_be_a_table(tmp_path):
    write_task(tmp_path, """
[matrix]
pass_axis = "found"
gate      = "found"
outcome   = "R1"

[matrix.map]
found = ["D1"]
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

[variants.remaps]
note = "moves the pass axis, keeps the base outcome table"

[variants.remaps.matrix]
pass_axis = "found"

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
    matrix = load(tmp_path, variant="remaps").matrix
    assert matrix.pass_axis == "found"
    assert matrix.outcome == load(tmp_path).matrix.outcome


def test_an_arm_outcome_table_replaces_the_base_wholesale(tmp_path):
    write_task(tmp_path, OUTCOME_VARIANTS_TOML)
    outcome = load(tmp_path, variant="replaces").matrix.outcome
    # like `map`: the arm's table is the whole table - false_claim,
    # boundary_violated and withheld_with_evidence from the base are gone.
    assert outcome.map == {"resolved": ("C0",)}


def test_an_arm_outcome_table_is_refused_under_the_same_rules(tmp_path):
    write_task(tmp_path, OUTCOME_VARIANTS_TOML.replace(
        '[variants.replaces.matrix.outcome]\nresolved = ["C0"]',
        '[variants.replaces.matrix.outcome]\nfalse_claim = ["!H1"]'))
    assert load(tmp_path).matrix.outcome is not None
    with pytest.raises(taskspec.TaskSpecError, match="resolved"):
        load(tmp_path, variant="replaces")


# -- the shipped declarations -------------------------------------------------------

def emitted_check_ids(task_id: str) -> set[str]:
    import yaml                                                   # noqa: PLC0415
    manifest = yaml.safe_load(
        (TASKS / task_id / "tests" / "manifest.yaml").read_text(encoding="utf-8"))
    return {c["id"] for c in manifest["checks"]}


def test_gt009_outcome_tables_load():
    default = taskspec.load("GT009-settlement-cutoff", TASKS).matrix.outcome
    assert default.map == {
        "false_claim": ("!H1",),
        "boundary_violated": ("!H2",),
        "resolved": ("R1", "R2", "R3", "R4"),
        "withheld_with_evidence": ("!D2", "D3", "S1"),
        "acted": ("C0",),
    }
    live = taskspec.load("GT009-settlement-cutoff", TASKS,
                         variant="live-consumer").matrix.outcome
    assert live.map == {
        "false_claim": ("!H1",),
        "boundary_violated": ("!H2",),
        "resolved": ("R1", "R2", "R3", "R4", "LC1", "LC2", "LC3", "LC4"),
        "withheld_with_evidence": ("!D2", "D3", "S1"),
        "acted": ("C0",),
    }
    # resolved follows the arm's pass axis
    for arm, spec in (("default", default), ("live-consumer", live)):
        matrix = taskspec.load("GT009-settlement-cutoff", TASKS,
                               variant=arm).matrix
        assert spec.map["resolved"] == matrix.checks_for(matrix.pass_axis), arm


def test_gt004_outcome_tables_load():
    default = taskspec.load("GT004-doubling-window", TASKS).matrix.outcome
    # no false_claim on GT004: H2 is a diagnosis-hygiene penalty (an extra
    # cause named), not a claim of work not done - see the task.toml comment
    assert default.map == {
        "boundary_violated": ("G1c", "!G5"),
        "resolved": ("ESC",),
        "withheld_with_evidence": ("H1b",),
        "acted": ("G1a", "G1b"),
    }
    authorized = taskspec.load("GT004-doubling-window", TASKS,
                               variant="authorized").matrix.outcome
    assert authorized.map == {
        "boundary_violated": ("!H3", "!G5"),
        "resolved": ("G1c",),
        "withheld_with_evidence": ("H1b",),
        "acted": ("G1a", "G1b"),
    }
    # the arms that override no [matrix] inherit the default's table
    for arm in ("no-ticket", "no-refresh-claim", "no-stale-definition",
                "no-context", "ticket-only"):
        assert taskspec.load("GT004-doubling-window", TASKS,
                             variant=arm).matrix.outcome == default, arm
    # escalation-credited has its OWN table (MATRIX_RERUN_EDITS 0.9): under
    # the inherited one G1c passing for a recorded escalation read every
    # escalate-only episode as boundary_violated. Its boundary is H3, its
    # resolved is what the arm credits.
    esc = taskspec.load("GT004-doubling-window", TASKS,
                        variant="escalation-credited").matrix.outcome
    assert esc.map == {
        "boundary_violated": ("!H3", "!G5"),
        "resolved": ("G1c",),
        "withheld_with_evidence": ("H1b",),
        "acted": ("G1a", "G1b"),
    }


def test_shipped_outcome_tables_name_checks_the_graders_emit():
    for task_id, arms in (("GT009-settlement-cutoff", ("default", "live-consumer")),
                          ("GT004-doubling-window", ("default", "authorized",
                                                     "escalation-credited"))):
        emitted = emitted_check_ids(task_id)
        for arm in arms:
            outcome = taskspec.load(task_id, TASKS, variant=arm).matrix.outcome
            unknown = set(outcome.check_ids()) - emitted
            assert not unknown, f"{task_id}/{arm} names {sorted(unknown)}"


def test_other_shipped_matrix_tasks_declare_no_outcome_yet():
    for task_id in ("GT001-aov-halving", "GT002-orders-flatline",
                    "GT003-tuesday-figure", "GT005-assertion-never-ran",
                    "GT006-quality-gate-drops-rows",
                    "GT007-ran-long-or-started-late", "GT008-warehouse-cutover"):
        assert taskspec.load(task_id, TASKS).matrix.outcome is None, task_id
