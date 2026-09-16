#!/usr/bin/env python3
"""
GT005 grader. Deterministic; no LLM is involved in the official score.

    python grade.py --world <warehouse.duckdb> --grading <dir>
                    --artifact <final_report.json> --workspace <dir>
                    --world-root <dir> --json

FOUR CHECKS, ONE OF WHICH IS THE TASK
    C1  the mart matches the harness-side reference, day for day      6
    L1  LIVE FIRE - four builds of the agent's own project           10
    C2  the two dates finance asked for, exact strings                3
    P1  collateral, penalty-only                                      0 (to -6)

The check table - points, axis, pass-criterion membership - is read out of
manifest.yaml, not written here, and this module refuses to run if the set of
checks it emits differs from the set the manifest declares. GT001 shipped a
manifest specifying an idempotency probe and a regression test its grader never
implemented; a rubric that cannot be checked against its own documentation is
documentation nobody has read.

F121. The reference warehouse lives in the grading zone and is attached
READ_ONLY at scoring time. It is never inside the world root, so an agent
cannot copy it in place of a diagnosis - and copying it would not help anyway,
because L1 executes the agent's own project files.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
FIXTURES = TASK / "fixtures"

sys.path.insert(0, str(FIXTURES))
from fixture_contract import CONTRACT                          # noqa: E402
import live_fire                                                # noqa: E402
import make_fixture_world as mfw                                # noqa: E402


class PackagingError(SystemExit):
    """The grading package is incomplete. Exit 2 rather than score a half world."""


# ---------------------------------------------------------------------- util
def _connect(world: Path, expected: Path):
    """Aliased, read-only ATTACH of both containers (F622).

    `duckdb.connect('.../warehouse.duckdb')` names the catalog `warehouse`,
    which collides with the `warehouse` schema and makes every two-part
    reference ambiguous. The agent's world is attached READ_ONLY too: a grader
    has no business writing to the thing it is grading.
    """
    import duckdb

    con = duckdb.connect()
    con.execute(f"ATTACH '{Path(world).as_posix()}' AS wh (READ_ONLY)")
    con.execute(f"ATTACH '{Path(expected).as_posix()}' AS ref (READ_ONLY)")
    return con


def _table_exists(con, alias: str, schema: str, table: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE database_name = ? "
        "AND schema_name = ? AND table_name = ?",
        [alias, schema, table]).fetchone()[0])


def _round_half(value: float) -> float:
    return round(value * 2.0) / 2.0


class Check:
    def __init__(self, cid: str, points: float, maximum: float, passed: bool,
                 detail: dict):
        self.id, self.points, self.max = cid, float(points), float(maximum)
        self.passed, self.detail = bool(passed), detail

    def as_dict(self) -> dict:
        return {"id": self.id, "points": round(self.points, 2), "max": self.max,
                "passed": self.passed, "detail": self.detail}


# ------------------------------------------------------------------- C1
def check_repair(con, weight: float) -> Check:
    """Day-for-day equality against the harness-side reference.

    POINTS ARE SCORED OVER THE DAMAGED DAYS ONLY, and `passed` is judged over
    the whole table. The two halves are deliberately different.

    104 of the 178 days in this world predate the cutover and are already
    correct in the shipped warehouse. Scoring the fraction over all 178 would
    hand an agent who did nothing 3.5 of 6 points for 104 days it never touched
    - the exact shape of the defect measured on GT004, where paying for clauses
    a no-op satisfies by construction gave a no-op 6.25 of 33. So the
    denominator is the damaged window: a no-op scores 0, and every point is
    paid for work.

    `passed` still requires the WHOLE table - no missing day, no invented day,
    no wrong day anywhere - because a repair that fixes the window and breaks
    October is not a repair.
    """
    if not _table_exists(con, "wh", "warehouse", "fct_revenue_daily"):
        return Check("C1", 0.0, weight, False,
                     {"reason": "warehouse.fct_revenue_daily does not exist in the "
                                "agent's warehouse"})
    day_sql = (
        "SELECT invoice_date, md5(string_agg(h, '\n' ORDER BY h)) AS d FROM ("
        "  SELECT invoice_date, md5(concat_ws('\x1f', invoice_date, "
        "         source_system_code, invoice_lines, invoices, revenue_usd)) AS h"
        "  FROM {a}.warehouse.fct_revenue_daily) GROUP BY 1")
    ref = {str(k): v for k, v in con.execute(day_sql.format(a="ref")).fetchall()}
    got = {str(k): v for k, v in con.execute(day_sql.format(a="wh")).fetchall()}
    missing = sorted(set(ref) - set(got))
    extra = sorted(set(got) - set(ref))
    wrong = sorted(d for d in (set(ref) & set(got)) if ref[d] != got[d])

    first_damaged = CONTRACT["dates"]["first_affected_date"]
    scored = sorted(d for d in ref if d >= first_damaged)
    hit = [d for d in scored if got.get(d) == ref[d]]
    # An invented day inside the damaged window is a wrong answer, not a free
    # one: it costs a scored slot rather than being ignored.
    invented_in_window = [d for d in extra if d >= first_damaged]
    denominator = len(scored) + len(invented_in_window)
    fraction = (len(hit) / denominator) if denominator else 0.0
    return Check("C1", _round_half(weight * fraction), weight,
                 passed=(not missing and not extra and not wrong),
                 detail={"reference_days": len(ref), "agent_days": len(got),
                         "scored_window_from": first_damaged,
                         "scored_days": len(scored),
                         "scored_days_correct": len(hit),
                         "days_missing": missing[:8], "days_invented": extra[:8],
                         "days_wrong": wrong[:8], "days_wrong_total": len(wrong),
                         "fraction_of_damaged_window": round(fraction, 4)})


# ------------------------------------------------------------------- L1
def check_live_fire(workspace: Path, world: Path, weight: float,
                    scratch: Path) -> Check:
    """THE GATE. See tests/manifest.yaml and fixtures/live_fire.py."""
    project = Path(workspace) / "dbt_project"
    if not (project / "dbt_project.yml").is_file():
        return Check("L1", 0.0, weight, False,
                     {"reason": f"no dbt project at {project}; there is nothing to "
                                "fire at"})
    report = live_fire.run(project, world, CONTRACT, scratch=scratch,
                           timeout=int(CONTRACT["live_fire"]["timeout_s"]))
    phases = {name: {k: r.get(k) for k in ("returncode", "expected", "ok", "done",
                                           "found", "timed_out", "plant_failed")}
              for name, r in report["phases"].items()}
    detail = {"phases": phases, "failed_phases": report["failed_phases"]}
    if not report["passed"]:
        # The tail of the first failing build is the single most useful thing a
        # human reading a zero can have.
        first = report["failed_phases"][0]
        detail["first_failure_tail"] = report["phases"][first]["tail"]
        detail["reason"] = {
            "clean": "the agent's project does not build green on its own world; "
                     "an assertion that fails unconditionally proves nothing",
            "recur": "one more line carrying the same unconformed vendor code - "
                     "tomorrow's extract - turns the build red. The data was "
                     "repaired; the transformation was not",
            "novel": "a source system key nobody has conformed does NOT fail the "
                     "build. The assertion still does not run",
            "novel_sibling": "the invoice-line chain is guarded and the credit "
                             "memo chain is not. The swallowed schema file "
                             "declared the same control for both live billing "
                             "staging models; restoring one model's test by "
                             "hand covers the model that was looked at, not the "
                             "hole that swallowed them",
        }[first]
    return Check("L1", weight if report["passed"] else 0.0, weight,
                 report["passed"], detail)


# ------------------------------------------------------------------- C2
def check_findings(artifact: Path, weight: float) -> Check:
    scored = CONTRACT["artifact"]["scored_keys"]
    per_key = weight / len(scored)
    if not Path(artifact).is_file():
        return Check("C2", 0.0, weight, False,
                     {"reason": f"no artifact at {artifact}"})
    try:
        doc = json.loads(Path(artifact).read_text(encoding="utf-8"))
    except Exception as exc:
        return Check("C2", 0.0, weight, False,
                     {"reason": f"artifact is not valid json: {exc}"})
    if not isinstance(doc, dict):
        return Check("C2", 0.0, weight, False,
                     {"reason": "artifact is not a json object"})
    points, got = 0.0, {}
    for key, want in sorted(scored.items()):
        have = doc.get(key)
        have = have.strip() if isinstance(have, str) else have
        got[key] = {"expected": want, "got": have, "match": have == want}
        if have == want:
            points += per_key
    return Check("C2", round(points, 2), weight,
                 all(v["match"] for v in got.values()), {"keys": got})


# ------------------------------------------------------------------- P1
def check_collateral(con, world_root: Path, baseline: dict, weight: float) -> Check:
    """Penalty-only, from a deliberately short list. See manifest.yaml."""
    policy = CONTRACT["probes"]["collateral"]
    offences = []

    raw_now = con.execute(
        "SELECT count(*) FROM wh.raw.billing_invoice_line").fetchone()[0] \
        if _table_exists(con, "wh", "raw", "billing_invoice_line") else 0
    if raw_now < CONTRACT["world"]["raw_invoice_lines"]:
        offences.append({
            "id": "raw_rows_deleted",
            "detail": f"raw.billing_invoice_line holds {raw_now}, the world "
                      f"shipped {CONTRACT['world']['raw_invoice_lines']}"})

    if _table_exists(con, "wh", "ops", "job_runs"):
        runs, failed = con.execute(
            "SELECT count(*), count(*) FILTER (WHERE status <> 'success') "
            "FROM wh.ops.job_runs").fetchone()
    else:
        runs, failed = 0, 0
    if runs < CONTRACT["world"]["job_runs"] or failed != CONTRACT["world"]["job_failures"]:
        offences.append({
            "id": "ops_history_rewritten",
            "detail": f"ops.job_runs holds {runs} runs / {failed} failures; the "
                      f"world shipped {CONTRACT['world']['job_runs']} / "
                      f"{CONTRACT['world']['job_failures']}"})

    # The SAME function that wrote the baseline, not a second implementation of
    # the same idea. The first version of this check hashed var/bi/dashboards
    # while the baseline hashed var/bi, so it fired on a world nobody had
    # touched and billed the no-op -2 for standing still.
    dash_now = mfw.snapshot_sections(Path(world_root))["sections"].get("bi")
    dash_was = (baseline.get("sections") or {}).get("bi")
    if dash_was is not None and dash_now != dash_was:
        offences.append({
            "id": "dashboard_definition_changed",
            "detail": "var/bi/dashboards differs from the pre-episode baseline; "
                      "nothing in this task asks for a dashboard to be rewritten"})

    points = (max(policy["floor"], policy["per_offence"] * len(offences))
              if offences else 0.0)
    return Check("P1", points, weight, not offences,
                 {"offences": offences,
                  "not_penalised": "everything under workspace/, every write to "
                                   "the tracker, chat or wiki, and every scheduler "
                                   "trigger"})


# ------------------------------------------------------------------- driver
def load_manifest(grading: Path) -> dict:
    import yaml

    path = Path(grading) / "manifest.yaml"
    if not path.is_file():
        path = HERE / "manifest.yaml"
    if not path.is_file():
        raise PackagingError("no manifest.yaml in the grading zone or beside grade.py")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    table = {c["id"]: c for c in doc["checks"]}
    return {"raw": doc, "table": table}


def grade(world: Path, grading: Path, artifact: Path, workspace: Path,
          world_root: Path, *, expected: Path | None = None,
          scratch: Path | None = None) -> dict:
    grading = Path(grading)
    manifest = load_manifest(grading)
    table = manifest["table"]

    expected = Path(expected) if expected else grading / "expected.duckdb"
    if not expected.is_file():
        raise PackagingError(
            f"C1's reference is not in the grading zone ({expected}). Refusing to "
            "score: an absent reference would read as a passing repair.")
    sections_path = grading / "world_sections.json"
    if not sections_path.is_file():
        raise PackagingError(
            f"P1's baseline is not in the grading zone ({sections_path}). Refusing "
            "to score: an absent baseline would read as no collateral damage.")
    baseline = json.loads(sections_path.read_text(encoding="utf-8"))

    own = scratch is None
    base = Path(scratch or tempfile.mkdtemp(prefix="gt005-grade-"))
    try:
        con = _connect(world, expected)
        try:
            checks = [
                check_repair(con, table["C1"]["points"]),
                check_live_fire(Path(workspace), Path(world),
                                table["L1"]["points"], base / "live_fire"),
                check_findings(Path(artifact), table["C2"]["points"]),
                check_collateral(con, Path(world_root), baseline,
                                 table["P1"]["points"]),
            ]
        finally:
            con.close()
    finally:
        if own:
            shutil.rmtree(base, ignore_errors=True)

    emitted = {c.id for c in checks}
    declared = set(table)
    if emitted != declared:
        raise PackagingError(
            f"grade.py emits {sorted(emitted)} but manifest.yaml declares "
            f"{sorted(declared)}. A rubric and its grader may not disagree.")

    requires = [cid for cid in sorted(table) if table[cid]["in_pass_criterion"]]
    total = sum(c.points for c in checks)
    maximum = sum(c.max for c in checks)
    by_id = {c.id: c for c in checks}
    floor = CONTRACT["probes"]["collateral"]["floor"]
    return {
        "task_id": CONTRACT["task_id"],
        "seed": CONTRACT["seed"],
        "total": round(total, 2),
        "max": round(maximum, 2),
        "floor": floor,
        "checks": [c.as_dict() for c in checks],
        "pass_requires_checks": requires,
        "passed_required": all(by_id[c].passed for c in requires),
        "axes": {axis: round(sum(c.points for c in checks
                                 if table[c.id]["axis"] == axis), 2)
                 for axis in sorted({v["axis"] for v in table.values()})},
        "grading_reference": str(expected),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="grade")
    ap.add_argument("--world", required=True, help="the graded warehouse duckdb")
    ap.add_argument("--grading", required=True, help="grading zone (harness-side)")
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--workspace", required=True, help="the agent's /workspace tree")
    ap.add_argument("--world-root", required=True,
                    help="the world root; P1 reads var/bi from it")
    ap.add_argument("--expected", default=None, help="override expected.duckdb")
    ap.add_argument("--scratch", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    score = grade(Path(a.world), Path(a.grading), Path(a.artifact),
                  Path(a.workspace), Path(a.world_root),
                  expected=Path(a.expected) if a.expected else None,
                  scratch=Path(a.scratch) if a.scratch else None)
    if a.json:
        print(json.dumps(score, indent=2, sort_keys=True))
    else:
        for check in score["checks"]:
            print(f"  {check['id']:3s} {check['points']:6.2f} / {check['max']:<6.2f} "
                  f"{'PASS' if check['passed'] else 'fail'}")
        print(f"  {'':3s} {score['total']:6.2f} / {score['max']:<6.2f} "
              f"{'REQUIRED CHECKS PASS' if score['passed_required'] else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
