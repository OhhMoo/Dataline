"""The seven grader checkpoints must be visible to harness matrix.progress."""
from __future__ import annotations

from tasks.factory.compiler.phase2_grader_bridge import embed_progress_quantities
from tasks.harness.progress import episode_progress, parse_progress_spec


def test_progress_quantities_feed_harness_check_details() -> None:
    result = {"progress_quantities": {
        "q0_raw_loaded": 0.5, "q3_r1_parity": 0.75},
        "checks": [{"id": "C0", "detail": {"passed": False}},
                   {"id": "R1", "detail": {"passed": False}}]}
    embed_progress_quantities(result)
    checks = {row["id"]: row for row in result["checks"]}
    spec = parse_progress_spec({
        "q0_raw_loaded": {"check": "C0", "field": "q0_raw_loaded",
                          "noop": "min", "oracle": "max"},
        "q3_r1_parity": {"check": "R1", "field": "q3_r1_parity",
                         "noop": "min", "oracle": "max"},
        "weights": {"q0_raw_loaded": 0.5, "q3_r1_parity": 0.5},
    }, known_ids={"C0", "R1"})
    reward = episode_progress(spec, checks, {
        "q0_raw_loaded": {"noop": 0.0, "oracle": 1.0},
        "q3_r1_parity": {"noop": 0.0, "oracle": 1.0},
    })
    assert reward == 0.625


def test_progress_quantities_embed_in_baseline_check_mapping() -> None:
    row = {"progress_quantities": {"q6_r4_parity": 0.25},
           "checks": {"R4": {"detail": {"matched_receipts": 2}}}}
    embed_progress_quantities(row)
    assert row["checks"]["R4"]["detail"] == {
        "matched_receipts": 2, "q6_r4_parity": 0.25}
