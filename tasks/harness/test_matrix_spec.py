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
from axes import MatrixSpec

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
