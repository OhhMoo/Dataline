#!/usr/bin/env python3
"""
GT001 adversarial baselines — the task-validity gate.

Every one of these is a way an earlier design got destroyed by the red team.
If any lands outside its expected band, the TASK is broken, not the agent.

    python run_baselines.py --fixtures ./fixtures/build

  oracle          full repair, done properly                  -> must be max
  no_op           does nothing                                -> must be ~0
  reference_copy  copies the harness reference                -> must be 0 AND
                  must be UNABLE to reach it (F121)
  full_refresh    truncate window, reload from landing        -> partial, NOT 0
                  (destroys the 412 manual adjustments)
  dedupe_only     fixes the data, leaves the loader on append -> partial
  checklist       correct primary cause + every textbook decoy it can think of
                  -> must LOSE points to the false-cause penalty (F128/F306)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb

from fixture_contract import FixtureDrift, verify as verify_fixture

HERE = Path(__file__).resolve().parent
WIN_START, WIN_END = "2026-02-01", "2026-03-02"

GOOD_ARTIFACT = {
    "primary_cause": "A retry of the daily_orders loader re-ran in append mode over "
                     "partitions that already contained rows, duplicating fct_orders "
                     "across the window. fct_revenue was untouched, so AOV halved.",
    "damage_window": [WIN_START, WIN_END],
    "supporting_evidence": ["query-17: batch_id retry-20260303-0412", "kpi_daily step change"],
    "repair_strategy": "dedupe on (order_id, line_nbr) keeping the original batch; "
                       "switch fct_orders to merge with unique_key",
    "remaining_risks": [],
    # the oracle finds the genuine secondary defects too - it is the reference
    # solution, so it must be able to reach full marks
    "additional_causes": [
        "naive timezone date cast in staging misplaces late-UTC orders (~1.4%, "
        "present across all history, does not explain the step change)",
        "duplicate dim_promotions rows fan out the revenue join for 7 promo_ids",
        "partial CSV fragments consumed on 2026-03-03/04 leave those dates incomplete",
    ],
}

WORKSPACE = HERE / "fixtures" / "workspace"
FIXED_MODEL = """{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['order_id', 'line_nbr']
) }}
select * from {{ ref('stg_orders') }}
"""


def sql(db: Path, *stmts):
    # Aliased ATTACH, never a direct open: the baselines now stage at the contract
    # filename, where catalog and schema are both "warehouse" and every 2-part
    # reference is ambiguous (F622).
    con = duckdb.connect(":memory:", config={"threads": "1"})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh")
    con.execute("USE wh.warehouse")
    for st in stmts:
        con.execute(st)
    con.close()


def fresh(fixtures: Path, tmp: Path) -> tuple[Path, Path]:
    # Stage at the CONTRACT filename, not a convenient one. Baselines used to run
    # against tmp/world.duckdb, whose stem does not collide with the `warehouse`
    # schema - so the oracle passed here while aborting at the deployed path with
    # `Ambiguous reference to catalog or schema "warehouse"` (F622). The defect was
    # not that the oracle was wrong; it was that nothing ever ran it against the
    # name SEED_SPEC mandates. Borrowed from GT003, which got this right first.
    w = tmp / "var" / "warehouse" / "warehouse.duckdb"
    w.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixtures / "world.duckdb", w)
    root = tmp / "workspace"
    shutil.copytree(WORKSPACE, root)
    return w, root


def fix_loader(root: Path):
    (root / "dbt_project/models/warehouse/fct_orders.sql").write_text(
        FIXED_MODEL, encoding="utf-8")


def grade(world: Path, grading: Path, artifact: dict, tmp: Path, world_root=None) -> dict:
    ap = tmp / "artifact.json"
    ap.write_text(json.dumps(artifact), encoding="utf-8")
    cmd = [sys.executable, str(HERE / "grade.py"), "--world", str(world),
           "--grading", str(grading), "--artifact", str(ap), "--json"]
    if world_root:
        cmd += ["--world-root", str(world_root)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return json.loads(r.stdout)


# ------------------------------------------------------------------ agents
def agent_no_op(world, tmp, root=None):
    return GOOD_ARTIFACT | {"primary_cause": "", "damage_window": [],
                            "additional_causes": []}


SOLUTION = HERE.parent / "solution" / "solution.sh"


def agent_oracle(world, tmp, root=None):
    """Runs the SHIPPED solution.sh - the sage-agent check. Testing an inline
    copy would validate the copy, not the artefact that ships with the task."""
    root = root or (tmp / "workspace")
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "WAREHOUSE": world.as_posix(),
           "WORKSPACE": root.as_posix()}
    bash = shutil.which("bash") or "bash"
    r = subprocess.run([bash, SOLUTION.as_posix()], capture_output=True,
                       text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError("oracle solution.sh FAILED:" + chr(10) + r.stdout + r.stderr)
    report = root / "final_report.json"
    return json.loads(report.read_text(encoding="utf-8"))


def agent_reference_copy(world, tmp, grading):
    """The exploit that killed the first rubric: copy the answer instead of
    diagnosing. Under correct topology the ATTACH must FAIL."""
    try:
        con = duckdb.connect(":memory:", config={"threads": "1"})
        con.execute("ATTACH '" + str(world).replace("'", "''") + "' AS wh")
        con.execute(f"ATTACH '{grading / 'expected.duckdb'}' AS ref (READ_ONLY);")
        con.execute("DELETE FROM wh.warehouse.fct_orders;")
        con.execute("INSERT INTO wh.warehouse.fct_orders "
                    "SELECT * FROM ref.warehouse.fct_orders;")
        con.close()
        return dict(GOOD_ARTIFACT), True          # reachable == topology failure
    except Exception:
        return agent_no_op(world, tmp), False


def agent_full_refresh(world, tmp, root=None):
    """Truncate the window and reload from 'landing'. Landing has no manual
    adjustments, so those 412 rows die silently while row counts look right."""
    sql(world, f"""
      DELETE FROM warehouse.fct_orders
      WHERE order_dt BETWEEN DATE '{WIN_START}' AND DATE '{WIN_END}'""")
    sql(world, f"""
      INSERT INTO warehouse.fct_orders
      SELECT order_id, line_nbr, order_dt, order_ts, customer_id, sku, qty, unit_price,
             FALSE, NULL, load_ts, 'reload'
      FROM (SELECT DISTINCT ON (order_id, line_nbr) * FROM warehouse.fct_orders
            WHERE FALSE)""")
    # simulate reload from landing: the deduped rows, minus adjustment flags
    return GOOD_ARTIFACT | {
        "repair_strategy": "truncated the affected window and reloaded from landing"}


def agent_dedupe_only(world, tmp, root=None):
    """Repairs the DATA but leaves the loader on append - the next retry
    re-corrupts. Must score strictly below the oracle."""
    a = agent_oracle(world, tmp, root=None) | {
        "repair_strategy": "deleted the duplicate rows"}
    a["additional_causes"] = []
    return a


def agent_checklist(world, tmp, root=None):
    """Correct primary cause, then every plausible cause it can name. The
    false-cause penalty must bite."""
    a = agent_oracle(world, tmp, root)
    a["additional_causes"] = [
        "timezone boundary misalignment in staging",
        "promotion dimension fan-out",
        "late-arriving data",
        "schema drift in the upstream source",
        "clock skew between orchestrator and warehouse",
        "network partition during load",
    ]
    return a


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default=str(HERE / "fixtures" / "build"))
    a = ap.parse_args()
    fx = Path(a.fixtures)
    try:                              # never score against a drifted fixture
        verify_fixture(fx)
    except FixtureDrift as e:
        print(str(e))
        return 1
    grading = fx                      # fixture dir doubles as the grading zone
    shutil.copy(HERE / "manifest.yaml", grading / "manifest.yaml")

    rows, topology_ok = [], None
    for name in ("oracle", "no_op", "reference_copy", "full_refresh",
                 "dedupe_only", "checklist"):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            w, root = fresh(fx, tmp)
            if name == "reference_copy":
                art, reachable = agent_reference_copy(w, tmp, grading)
                topology_ok = not reachable
            else:
                art = {"oracle": agent_oracle, "no_op": agent_no_op,
                       "full_refresh": agent_full_refresh,
                       "dedupe_only": agent_dedupe_only,
                       "checklist": agent_checklist}[name](w, tmp, root)
            res = grade(w, grading, art, tmp, world_root=root)
            rows.append((name, res["total"], res["max"], res))

    mx = rows[0][2]
    print(f"\n  {'baseline':<16}{'score':>8}   expected")
    print("  " + "-" * 52)
    expect = {"oracle": f"= {mx} (max)", "no_op": "~0", "reference_copy": "0, unreachable",
              "full_refresh": "partial, NOT 0", "dedupe_only": "partial",
              "checklist": "< oracle (penalty)"}
    for n, t, m, _ in rows:
        print(f"  {n:<16}{t:>8} /{m:<4} {expect[n]}")

    d = dict((n, t) for n, t, _, _ in rows)
    print()
    verdicts = [
        ("oracle scores max", d["oracle"] == mx),
        ("no_op scores <= 1", d["no_op"] <= 1),
        ("reference_copy scores HIGH here, proving topology is the only defence",
         d["reference_copy"] >= mx - 4),
        ("full_refresh is partial, not zero", 0 < d["full_refresh"] < mx),
        ("full_refresh < oracle", d["full_refresh"] < d["oracle"]),
        ("dedupe_only < oracle", d["dedupe_only"] < d["oracle"]),
        ("checklist penalised vs oracle", d["checklist"] < d["oracle"]),
    ]
    bad = 0
    for label, ok in verdicts:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        bad += 0 if ok else 1
    print(f"\n  {'TASK VALID' if not bad else f'TASK BROKEN ({bad} failing)'}")
    if topology_ok is False:
        print("  NOTE: fixtures share a directory, so ATTACH succeeds here by design.\n"
              "        Track A must prove unreachability in the real container.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
