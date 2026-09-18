"""benchmark.json and the README row (export.py).

    python -m pytest test_export.py -q

Two properties, both about what LEAVES the harness: the public artefact
carries every rate with its denominator and nothing that would hand a model
the grader; and a run that is not a clean measurement publishes WITHHELD,
never a number with a footnote. The learning-signal modules are faked the
same way test_matrix_report.py fakes them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import export          # noqa: E402
import report          # noqa: E402
from test_matrix_report import (  # noqa: E402
    MATRIX, MatrixWithOutcome, StubSpec, install_fakes, rec)

# words that name grader internals; none may appear anywhere in the artefact
FORBIDDEN = ("detail", "why", "canary", "artifact", "transcript", "rollout_dir")

SCHEMA_KEYS = {
    "schema_version", "task", "run_id", "arm", "prompt_variant", "models",
    "driver", "topology", "blindness_guarantee", "K", "scored", "quarantined",
    "measurement_valid", "invalidated_because", "withheld", "pass_at_1",
    "pass_at_k", "pass_hat_k", "matrix", "learning_signal",
}


def leaky_records():
    """Records stuffed with everything the export must NOT carry."""
    matrix = MatrixWithOutcome(pass_axis="warranted", gate="found",
                               map=dict(MATRIX.map), outcome=object())
    records = [
        rec("r0", {"H1b": True, "H3": True, "G1c": True, "C1": True},
            why="the canary string leaked into the transcript",
            artifact={"report": "secret"}, rollout_dir="/tmp/run/r0",
            variant="default", prompt_variant="ticket-a",
            blindness_guarantee="container", topology="container"),
        rec("r1", {"H1b": True, "H3": False, "G1c": False, "C1": False},
            classification="reasoning", passed=False,
            why="declined", artifact={"report": "x"}, rollout_dir="/tmp/run/r1",
            variant="default", prompt_variant="ticket-a",
            blindness_guarantee="container", topology="container"),
        rec("r2", {"H1b": False, "H3": True, "G1c": True, "C1": True},
            classification="reasoning", passed=False,
            variant="default", prompt_variant="ticket-a",
            blindness_guarantee="container", topology="container"),
    ]
    for r in records:
        for c in r["checks"].values():
            c["detail"] = "grader detail text that must not ship"
    return records, StubSpec(matrix, variant="default")


@pytest.fixture
def bench(monkeypatch):
    install_fakes(monkeypatch)
    records, spec = leaky_records()
    agg = report.aggregate(records, spec=spec)
    assert agg["measurement_valid"], agg["invalidated_because"]
    return export.build("GT999-no-such-task", "run-x", agg, records, spec=spec)


def test_schema_keys_are_exactly_the_documented_ones(bench):
    assert set(bench) == SCHEMA_KEYS
    assert bench["schema_version"] == 1
    assert bench["task"] == "GT999-no-such-task" and bench["run_id"] == "run-x"
    assert bench["arm"] == "default" and bench["prompt_variant"] == "ticket-a"
    assert bench["models"] == ["stub-model"] and bench["driver"] == "scripted"
    assert bench["topology"] == "container"
    assert bench["blindness_guarantee"] == "container"
    assert (bench["K"], bench["scored"], bench["quarantined"]) == (3, 3, 0)
    assert bench["withheld"] is False and bench["measurement_valid"] is True


def test_every_rate_travels_with_its_denominator(bench):
    # strict criterion, with interval and n
    assert bench["pass_at_1"] == {"rate": 0.333, "ci95": [0.38, 0.96], "n": 3}
    assert set(bench["pass_at_k"]) == {"n", "c", "pass_at_k", "pass_hat_k"}
    assert bench["pass_at_k"]["n"] == 3
    assert bench["pass_hat_k"] == {"pass_hat_k": 0.0, "k": 3, "complete": True}
    m = bench["matrix"]
    assert set(m) == {"pass_axis", "gate", "projected_pass",
                      "projected_pass_ci95", "denominator", "axis_means",
                      "outcome_counts"}
    assert m["pass_axis"] == "warranted" and m["projected_pass"] == 0.5
    assert m["denominator"] == 2                      # the gate-cold one left
    assert m["axis_means"]["scoped"] is None          # unmapped: n/a, never 0
    # the fake classifier reads checks, not the gate, so gate-cold r2 counts
    assert m["outcome_counts"] == {"resolved": 2, "silent": 1}
    ls = bench["learning_signal"]
    assert set(ls) == {"band", "group", "axis_separation"}
    assert ls["group"] == {"n": 5, "p_one_minus_p": 0.16, "collapsed": False}
    assert ls["axis_separation"]["separated"] is True


def test_nothing_from_the_grader_leaks(bench):
    blob = json.dumps(bench).lower()
    for word in FORBIDDEN:
        assert word not in blob, word
    assert "secret" not in blob and "/tmp/" not in blob
    assert "per_check" not in blob and "per_rollout" not in blob
    assert "reward" not in blob                        # per-episode scalars stay home


def test_round_trips_through_disk(bench, tmp_path, monkeypatch):
    install_fakes(monkeypatch)
    records, spec = leaky_records()
    agg = report.aggregate(records, spec=spec)
    written = export.write(tmp_path / "site", "GT999-no-such-task", "run-x",
                           agg, records, spec=spec)
    loaded = json.loads((tmp_path / "site" / "benchmark.json")
                        .read_text(encoding="utf-8"))
    assert loaded == written == bench


def test_row_and_header_agree_on_columns(bench):
    header = export.render_header().splitlines()
    assert len(header) == 2
    n_cols = header[0].count("|") - 1
    assert header[1] == "|" + "---|" * n_cols
    row = export.render_row(bench)
    assert row.count("|") - 1 == n_cols
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[0] == "GT999-no-such-task" and cells[1] == "default"
    assert cells[2] == "stub-model" and cells[3] == "warranted"
    assert cells[4] == "0.50 [0.38, 0.96] (n=2)"       # projected pass
    assert cells[5] == "0.33 [0.38, 0.96] (n=3)"       # Pass@1
    assert cells[6] == "0.0 (k=3)"                      # Pass^k bit
    assert cells[7:11] == ["1.00", "0.50", "n/a", "0.50"]
    assert cells[11] == "core" and cells[12] == "yes"


def test_withheld_run_prints_withheld_never_a_number(monkeypatch):
    install_fakes(monkeypatch)
    records, spec = leaky_records()
    records[0]["redteam_detector_test"] = True         # not a measurement
    agg = report.aggregate(records, spec=spec)
    assert agg["measurement_valid"] is False
    bench = export.build("GT999-no-such-task", "run-x", agg, records, spec=spec)
    assert bench["withheld"] is True
    assert bench["pass_at_1"] is None and bench["pass_at_k"] is None
    assert bench["pass_hat_k"]["pass_hat_k"] is None
    assert bench["pass_hat_k"]["k"] == 3                # the count stays
    assert bench["matrix"]["projected_pass"] is None
    assert all(v is None for v in bench["matrix"]["axis_means"].values())
    assert bench["learning_signal"] is None
    assert bench["invalidated_because"]                  # the reason travels
    row = export.render_row(bench)
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[4:12] == ["WITHHELD"] * 8
    assert cells[12] == "no"
    # and no rate-looking number anywhere in the rate cells
    assert not any(ch.isdigit() for ch in "".join(cells[4:12]))


def test_non_matrix_task_has_no_matrix_and_no_axis_numbers(monkeypatch):
    install_fakes(monkeypatch)
    records = [rec("r0", {"D1": True}), rec("r1", {"D1": False}, passed=False,
                                            classification="reasoning")]
    agg = report.aggregate(records)
    bench = export.build("T", "run", agg, records)
    assert bench["matrix"] is None and bench["arm"] is None
    cells = [c.strip() for c in export.render_row(bench).strip("|").split("|")]
    assert cells[3] == "n/a" and cells[4] == "n/a"
    assert cells[5] == "0.50 [0.38, 0.96] (n=2)"
    assert cells[7:11] == ["n/a"] * 4


if __name__ == "__main__":
    raise SystemExit(pytest.main([str(Path(__file__).resolve()), "-q"]))
