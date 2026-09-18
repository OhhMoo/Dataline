"""GT008's [matrix] declaration, checked against the frozen axes contract.

new_matrix.md Layer A is score-only: the declaration in
tasks/GT008-warehouse-cutover/task.toml must load into axes.MatrixSpec
unchanged, name only check ids tests/grade.py actually emits (the manifest
check table is the set audit_check_set enforces), and reproduce the two
behaviours the redesign exists for - the port_legacy_loader episode leaves
the denominators instead of scoring 0 on consumer, and an oracle episode
passes on the consumer axis alone.

Nothing here runs a rollout or the grader; every episode record is synthetic.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

from axes import MatrixSpec, admitted, axis_rate, projected_pass

TASK_ROOT = Path(__file__).resolve().parent.parent / "GT008-warehouse-cutover"


def load_spec() -> MatrixSpec:
    raw = tomllib.loads((TASK_ROOT / "task.toml").read_text(encoding="utf-8"))
    matrix = raw["matrix"]
    return MatrixSpec(
        pass_axis=matrix["pass_axis"],
        gate=matrix.get("gate"),
        accepted=tuple(matrix.get("accepted") or ()),
        map={axis: tuple(ids) for axis, ids in (matrix.get("map") or {}).items()})


def emitted_check_ids() -> set[str]:
    manifest = yaml.safe_load(
        (TASK_ROOT / "tests" / "manifest.yaml").read_text(encoding="utf-8"))
    return {c["id"] for c in manifest["checks"]}


def record(**cid_passed: bool) -> dict[str, dict]:
    return {cid: {"passed": passed} for cid, passed in cid_passed.items()}


# -- the declaration ----------------------------------------------------------

def test_matrix_is_the_gt008_row():
    spec = load_spec()
    assert spec.pass_axis == "consumer"
    assert spec.gate == "found"
    # The oracle baseline is enumerable from tests/run_baselines.py's AGENTS.
    assert spec.accepted == ("oracle",)


def test_mapped_checks_are_ones_the_grader_emits():
    spec = load_spec()
    for axis in ("found", "warranted", "scoped", "consumer"):
        unknown = set(spec.checks_for(axis)) - emitted_check_ids()
        assert not unknown, f"[matrix.map].{axis} names {sorted(unknown)}, " \
                            "which grade.py never emits"


def test_found_maps_to_the_diagnosis_checkpoints():
    # C2 (a byte-identical replay of a superseded version) and C3 (v2
    # arriving after v3) are the two checkpoints the ported legacy loader
    # fails; C0/C1 stay out because the ported loader passes them.
    spec = load_spec()
    assert spec.checks_for("found") == ("C2", "C3")
    assert spec.checks_for("consumer") == ("C5",)
    # warranted and scoped are unmapped: n/a for this task, never 0.
    assert spec.checks_for("warranted") == ()
    assert spec.checks_for("scoped") == ()


# -- the behaviours the redesign exists for -------------------------------------

def test_oracle_episode_passes_on_consumer_alone():
    spec = load_spec()
    oracle = record(C0=True, C1=True, C2=True, C3=True, C4=True, D2=True,
                    S1=True, S2=True, H1=True, C5=True)
    assert admitted(spec, oracle) is True
    assert projected_pass(spec, oracle) is True


def test_port_legacy_loader_leaves_the_denominators():
    # Measured 2026-09-16 (tests/run_baselines.py): C0 and C1 full, C2..C4
    # zero, D2 full. Under the matrix that episode never found the defect, so
    # the gate is cold and it is EXCLUDED from the consumer denominator -
    # not scored 0 on it.
    spec = load_spec()
    ported = record(C0=True, C1=True, C2=False, C3=False, C4=False, D2=True,
                    S1=True, S2=True, H1=True, C5=True)
    assert admitted(spec, ported) is False
    assert projected_pass(spec, ported) is None
    assert axis_rate(ported, spec.checks_for("consumer")) == 1.0


def test_a_record_without_receipt_evidence_withholds():
    # C5 pre-dates the publish hook: an episode graded before it lands (or an
    # old rollout record) carries no C5, so consumer is n/a and the pass bit
    # withholds rather than reading as 0.
    spec = load_spec()
    legacy = record(C0=True, C1=True, C2=True, C3=True, C4=True, D2=True)
    assert admitted(spec, legacy) is True
    assert axis_rate(legacy, spec.checks_for("consumer")) is None
    assert projected_pass(spec, legacy) is None


def test_a_bad_publication_fails_consumer_once_admitted():
    spec = load_spec()
    bad_receipt = record(C0=True, C1=True, C2=True, C3=True, C4=True,
                         D2=True, C5=False)
    assert projected_pass(spec, bad_receipt) is False
