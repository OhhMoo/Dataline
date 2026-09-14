#!/usr/bin/env python3
"""
GT002 adversarial baselines - the task-validity gate.

Every one of these is a way an earlier design got destroyed by the red team, or
a way this one could be. If any lands outside its expected band, the TASK is
broken, not the agent.

    python run_baselines.py --fixtures ./fixtures/build

  oracle              full repair, in the right order              -> must be max
  no_op               does nothing                                 -> must be <= 1
  reference_copy      copies the harness reference                 -> scores HIGH here
                      by design; only container topology makes it 0 (F121)
  full_refresh        rebuilds fct_orders from the topic           -> partial, NOT 0,
                      and 60 days of history that exist nowhere else are destroyed
  single_defect_only  repairs the STALL, replays, leaves the sink  -> MUST be < oracle.
                      on append. THE KEY BASELINE: this is what an  This is the whole
                      agent that solves defect A alone produces.    task in one row.
  naive_dedupe        single_defect_only, then the obvious cleanup -> < oracle; the
                      (de-duplicate on order_id)                    1,100 corrections die
  reset_earliest      rewinds to log_start with the append sink    -> < oracle; blast
                                                                     radius destroyed
  checklist           correct diagnosis plus every textbook cause  -> < oracle
                                                                     (penalty bites)
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
from warehouse_conn import deployed_copy, open_warehouse

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE / "fixtures" / "workspace"
SOLUTION = HERE.parent / "solution" / "solution.sh"

GOOD_ARTIFACT: dict = {}       # filled from the oracle's own report on first run


def sql(db: Path, *stmts):
    con = open_warehouse(db)
    for st in stmts:
        con.execute(st)
    con.close()


def fresh(fx: Path, tmp: Path) -> tuple[Path, Path]:
    """Deploy the world at the CONTRACT filename, var/warehouse/warehouse.duckdb.

    Not `tmp/world.duckdb`. A stem that is not "warehouse" does not collide with
    the schema of the same name, so a baseline suite run against one proves
    nothing about the world the task ships - that is precisely how GT001 printed
    TASK VALID with an oracle that aborted on its first statement."""
    w = deployed_copy(fx / "world.duckdb", tmp)
    root = tmp / "workspace"
    shutil.copytree(WORKSPACE, root)
    return w, root


def grade(world: Path, grading: Path, artifact: dict, tmp: Path, root: Path) -> dict:
    ap = tmp / "artifact.json"
    ap.write_text(json.dumps(artifact), encoding="utf-8")
    cmd = [sys.executable, str(HERE / "grade.py"), "--world", str(world),
           "--grading", str(grading), "--artifact", str(ap),
           "--world-root", str(root), "--json"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return json.loads(r.stdout)


# ---------------------------------------------------------------- the pipeline
# The repaired sink and quarantine, as a competent agent would write them. Used
# by the baselines that fix the stall; the oracle writes its own.
FIXED_QUARANTINE = """
INSERT INTO raw.orders_dlq
SELECT t.offset_id, t.event_ts, t.payload,
       CASE WHEN json_extract_string(t.payload,'$.order_id') IS NULL
            THEN 'schema: required field order_id is null'
            ELSE 'decode: could not convert qty '
                 || json_extract_string(t.payload,'$.qty') || ' to INTEGER' END,
       now()
FROM raw.orders_topic t
WHERE t.offset_id >= (SELECT committed_offset FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND t.offset_id <  (SELECT committed_offset + 2000 FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND (json_extract_string(t.payload,'$.order_id') IS NULL
       OR TRY_CAST(json_extract_string(t.payload,'$.qty') AS INTEGER) IS NULL)
  AND NOT EXISTS (SELECT 1 FROM raw.orders_dlq d WHERE d.offset_id = t.offset_id);
"""

_PROJ = """
       TRY_CAST(json_extract_string(t.payload,'$.order_id')    AS BIGINT),
       TRY_CAST(json_extract_string(t.payload,'$.revision')    AS INTEGER),
       TRY_CAST(json_extract_string(t.payload,'$.order_dt')    AS DATE),
       TRY_CAST(json_extract_string(t.payload,'$.event_ts')    AS TIMESTAMP),
       TRY_CAST(json_extract_string(t.payload,'$.customer_id') AS BIGINT),
            json_extract_string(t.payload,'$.channel_code'),
            json_extract_string(t.payload,'$.sku'),
       TRY_CAST(json_extract_string(t.payload,'$.qty')         AS INTEGER),
       TRY_CAST(json_extract_string(t.payload,'$.unit_price')  AS DECIMAL(10,2)),
       ROUND(TRY_CAST(json_extract_string(t.payload,'$.qty') AS INTEGER)
           * TRY_CAST(json_extract_string(t.payload,'$.unit_price') AS DECIMAL(10,2)), 2)
           ::DECIMAL(12,2),
       now(),
       'stream-' || strftime(t.event_ts, '%Y%m%d-%H')
"""
_WINDOW = """
FROM raw.orders_topic t
WHERE t.offset_id >= (SELECT committed_offset FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND t.offset_id <  (SELECT committed_offset + 2000 FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND json_extract_string(t.payload,'$.order_id') IS NOT NULL
  AND TRY_CAST(json_extract_string(t.payload,'$.qty') AS INTEGER) IS NOT NULL
  AND TRY_CAST(json_extract_string(t.payload,'$.unit_price') AS DECIMAL(10,2)) IS NOT NULL
"""
_ANTIJOIN = """
  AND NOT EXISTS (SELECT 1 FROM warehouse.fct_orders f
                   WHERE f.order_id = TRY_CAST(json_extract_string(t.payload,'$.order_id') AS BIGINT)
                     AND f.revision = TRY_CAST(json_extract_string(t.payload,'$.revision') AS INTEGER))
"""
COMMIT_SQL = """
UPDATE raw.consumer_offsets
SET committed_offset = LEAST(committed_offset + 2000,
        (SELECT log_end_offset FROM raw.topic_meta WHERE topic = 'orders.v2')),
    committed_at = now()
WHERE consumer_group = 'orders_fct_sink';
"""


def sink_sql(idempotent: bool) -> str:
    return ("INSERT INTO warehouse.fct_orders SELECT " + _PROJ + _WINDOW
            + (_ANTIJOIN if idempotent else "") + ";")


def catch_up(world: Path, root: Path, idempotent: bool, rewind_to=None):
    """Repair the stall and drain the lag, writing the pipeline files the agent
    would have written so the idempotency probe sees the same code."""
    (root / "pipelines" / "orders_quarantine.sql").write_text(
        FIXED_QUARANTINE, encoding="utf-8")
    (root / "pipelines" / "orders_sink.sql").write_text(
        sink_sql(idempotent), encoding="utf-8")
    con = open_warehouse(world)
    if rewind_to is not None:
        con.execute("UPDATE raw.consumer_offsets SET committed_offset = "
                    f"{rewind_to} WHERE consumer_group = 'orders_fct_sink'")
    s = sink_sql(idempotent)
    for _ in range(500):
        lag = con.execute("SELECT lag FROM raw.v_consumer_lag "
                          "WHERE consumer_group = 'orders_fct_sink'").fetchone()[0]
        if lag <= 0:
            break
        con.execute(FIXED_QUARANTINE)
        con.execute(s)
        con.execute(COMMIT_SQL)
    con.close()


# -------------------------------------------------------------------- agents
def agent_no_op(world, root, fx):
    return {"primary_cause": "", "damage_window": [], "supporting_evidence": [],
            "repair_strategy": "", "remaining_risks": [], "additional_causes": []}


def agent_oracle(world, root, fx):
    """Runs the SHIPPED solution.sh - the sage-agent check. Testing an inline
    copy would validate the copy, not the artefact that ships with the task."""
    env = {**os.environ, "WAREHOUSE": world.as_posix(), "WORKSPACE": root.as_posix()}
    bash = shutil.which("bash") or "bash"
    r = subprocess.run([bash, SOLUTION.as_posix()], capture_output=True,
                       text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError("oracle solution.sh FAILED:\n" + r.stdout + r.stderr)
    return json.loads((root / "final_report.json").read_text(encoding="utf-8"))


def agent_reference_copy(world, root, fx):
    """The exploit that killed the first rubric: copy the answer instead of
    diagnosing. Under correct container topology the ATTACH must FAIL."""
    try:
        con = open_warehouse(world, attach={"ref": fx / "expected.duckdb"})
        con.execute("DELETE FROM warehouse.fct_orders;")
        con.execute("INSERT INTO warehouse.fct_orders SELECT * FROM ref.warehouse.fct_orders;")
        con.execute("DELETE FROM raw.orders_dlq;")
        con.execute("INSERT INTO raw.orders_dlq SELECT * FROM ref.raw.orders_dlq;")
        con.execute("UPDATE raw.consumer_offsets SET committed_offset = "
                    "(SELECT committed_offset FROM ref.raw.consumer_offsets "
                    " WHERE consumer_group='orders_fct_sink') "
                    "WHERE consumer_group='orders_fct_sink';")
        con.close()
        return dict(GOOD_ARTIFACT), True          # reachable == topology failure
    except Exception:
        return agent_no_op(world, root, fx), False


def agent_full_refresh(world, root, fx):
    """Rebuild the fact table from the stream. The topic retains thirty days and
    the table holds ninety, so this looks complete and silently destroys sixty
    days that exist nowhere else."""
    sql(world, "DELETE FROM warehouse.fct_orders;")
    catch_up(world, root, idempotent=False,
             rewind_to=int(json.loads((fx / "harness_truth.json").read_text()
                                      )["log_start_offset"]))
    return GOOD_ARTIFACT | {
        "repair_strategy": "truncated warehouse.fct_orders and reloaded it from "
                           "the orders.v2 topic from log_start_offset"}


def agent_single_defect_only(world, root, fx):
    """THE KEY BASELINE. Diagnoses the stall correctly, routes the undecodable
    records to the dead-letter table, rewinds to the committed offset and
    replays - and never reads the sink. Everything about this is competent
    except the one thing that leaves no evidence."""
    catch_up(world, root, idempotent=False)
    return GOOD_ARTIFACT | {
        "repair_strategy": "quarantined the records the sink could not decode and "
                           "replayed the gap from the committed offset"}


def agent_naive_dedupe(world, root, fx):
    """single_defect_only, then the obvious cleanup once the duplicates appear:
    keep one row per order_id. That is the move A063 is built to punish - it
    cannot tell a replay duplicate from a correction the producer genuinely
    re-emitted, and there are 1,100 of the latter."""
    catch_up(world, root, idempotent=False)
    sql(world, """
        DELETE FROM warehouse.fct_orders WHERE rowid IN (
          SELECT rowid FROM (
            SELECT rowid, row_number() OVER (PARTITION BY order_id
                                             ORDER BY load_ts, rowid) AS rn
            FROM warehouse.fct_orders) WHERE rn > 1)""")
    return GOOD_ARTIFACT | {
        "repair_strategy": "replayed the gap, then de-duplicated fct_orders on order_id"}


def agent_reset_earliest(world, root, fx):
    """The reflexive rewind. Replays the whole retained topic instead of the
    gap, through a sink with no merge key (S706)."""
    catch_up(world, root, idempotent=False,
             rewind_to=int(json.loads((fx / "harness_truth.json").read_text()
                                      )["log_start_offset"]))
    return GOOD_ARTIFACT | {
        "repair_strategy": "reset the consumer group to the earliest retained "
                           "offset and replayed"}


def agent_checklist(world, root, fx):
    """Correct diagnosis and a correct repair, then every plausible cause it can
    name. The false-cause penalty must bite (F128/F129/F306)."""
    a = agent_oracle(world, root, fx)
    a["additional_causes"] = list(a["additional_causes"]) + [
        "upstream schema drift in the order producer",
        "clock skew between the orchestrator and the warehouse",
        "network partition during the load window",
        "warehouse disk pressure throttling the writer",
        "a broker rebalance that dropped the partition assignment",
    ]
    return a


AGENTS = {
    "oracle": agent_oracle,
    "no_op": agent_no_op,
    "reference_copy": agent_reference_copy,
    "full_refresh": agent_full_refresh,
    "single_defect_only": agent_single_defect_only,
    "naive_dedupe": agent_naive_dedupe,
    "reset_earliest": agent_reset_earliest,
    "checklist": agent_checklist,
}

EXPECT = {
    "oracle": "= max",
    "no_op": "<= 1",
    "reference_copy": "high here, 0 in the container",
    "full_refresh": "partial, NOT 0",
    "single_defect_only": "< oracle  <-- THE conjunction",
    "naive_dedupe": "< oracle",
    "reset_earliest": "< oracle",
    "checklist": "< oracle (penalty)",
}


def main() -> int:
    global GOOD_ARTIFACT
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default=str(HERE / "fixtures" / "build"))
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    fx = Path(a.fixtures)
    try:                              # never score against a drifted fixture
        verify_fixture(fx)
    except FixtureDrift as e:
        print(str(e))
        return 1
    grading = fx                      # the fixture dir doubles as the grading zone
    shutil.copy(HERE / "manifest.yaml", grading / "manifest.yaml")

    # ...and never print TASK VALID without having run the world at the filename
    # it actually deploys under. Imported late so a broken fixture is reported
    # first. GT001's oracle was broken for a day behind a green baseline suite.
    from test_deployed_path import DeployedPathFailure, check as deployed_check
    try:
        deployed_check(fx)
    except DeployedPathFailure as e:
        print(str(e))
        return 1

    # ...and never print TASK VALID while the property the task exists for is
    # broken. A063 is only worth building because defect B is unobservable until
    # defect A is repaired; F624 is what it looks like when that quietly stops
    # being true and every other check stays green.
    from test_dormancy import DormancyBroken, check as dormancy_check
    try:
        dormancy_check(fx)
    except DormancyBroken as e:
        print(str(e))
        return 1

    # the oracle's own report is the artifact every other baseline submits, so a
    # baseline is only ever penalised for what it DID, never for what it wrote
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        w, root = fresh(fx, tmp)
        GOOD_ARTIFACT = agent_oracle(w, root, fx)

    rows, topology_ok = [], None
    for name, fn in AGENTS.items():
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            w, root = fresh(fx, tmp)
            if name == "reference_copy":
                art, reachable = fn(w, root, fx)
                topology_ok = not reachable
            else:
                art = fn(w, root, fx)
            res = grade(w, grading, art, tmp, root)
            rows.append((name, res["total"], res["max"], res))

    mx = rows[0][2]
    print(f"\n  {'baseline':<20}{'score':>8}   expected")
    print("  " + "-" * 62)
    for n, t, m, _ in rows:
        print(f"  {n:<20}{t:>8} /{m:<4} {EXPECT[n]}")

    if a.verbose:
        for n, _, _, res in rows:
            print(f"\n  --- {n} ---")
            for c in res["checks"]:
                print(f"    {c['id']:<4}{c['points']:>6} / {c['max']:<3} {c['detail'][:96]}")

    d = {n: t for n, t, _, _ in rows}
    by = {n: {c["id"]: c for c in res["checks"]} for n, _, _, res in rows}
    verdicts = [
        ("oracle scores max", d["oracle"] == mx),
        ("no_op scores <= 1", d["no_op"] <= 1),
        ("reference_copy scores HIGH here, proving topology is the only defence",
         d["reference_copy"] >= mx - 6),
        ("full_refresh is partial, not zero", 0 < d["full_refresh"] < mx),
        ("full_refresh destroys history the topic cannot restore (K6 fails)",
         not by["full_refresh"]["K6"]["passed"]),
        ("single_defect_only < oracle", d["single_defect_only"] < d["oracle"]),
        ("single_defect_only FAILS the conjunction check (K1)",
         not by["single_defect_only"]["K1"]["passed"]),
        ("single_defect_only fails it HIGH, not low - the sink duplicated",
         "duplicated on replay" in by["single_defect_only"]["K1"]["detail"]),
        ("no_op fails the conjunction check LOW - the gap was never replayed",
         "never replayed" in by["no_op"]["K1"]["detail"]),
        ("even a full reference copy cannot pass the idempotency probe: the probe "
         "executes the agent's OWN sink, which copying a table does not change",
         not by["reference_copy"]["K5"]["passed"]),
        ("full_refresh is penalised for destroying pre-retention history",
         by["full_refresh"]["K8"]["points"] <= -3),
        ("naive_dedupe destroys the legitimate revisions (K4)",
         by["naive_dedupe"]["K4"]["points"] < by["oracle"]["K4"]["points"]),
        ("reset_earliest < oracle", d["reset_earliest"] < d["oracle"]),
        ("checklist penalised vs oracle", d["checklist"] < d["oracle"]),
        ("no_op earns nothing on the 'no duplicates' check (no free points)",
         by["no_op"]["K3"]["points"] == 0),
        ("no_op earns nothing on the idempotency probe",
         by["no_op"]["K5"]["points"] == 0),
    ]
    bad = 0
    print()
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
