#!/usr/bin/env python3
"""
GT003 adversarial baselines — the task-validity gate.

Every one of these is a way an earlier design got destroyed by the red team.
If any lands outside its expected band, the TASK is broken, not the agent.

    python run_baselines.py --fixtures ./fixtures/build

  oracle          the shipped solution.sh                     -> must be max
  no_op           does nothing                                -> must be <= 1
  reference_copy  copies the harness reference                -> high here BY
                  DESIGN; only container topology makes it 0 (F121)
  full_refresh    drops the debris, re-runs the pipeline as   -> partial, NOT 0,
                  it stands, rebuilds the record from the        and it must
                  mart                                           destroy BOTH
                                                                 pieces of
                                                                 evidence
  rebuild_only    hardens the publish and fixes the rate but  -> < oracle: the
                  leaves the analyst's change and the record     compensating
                                                                 change is a
                                                                 defect too
  checklist       correct primary cause + every textbook      -> < oracle (the
                  decoy it can think of                          penalty bites)

EVERY baseline runs against a world staged at the CONTRACT filename,
<tmp>/var/warehouse/warehouse.duckdb - not against the fixture's build name.
GT001 printed TASK VALID for weeks while its own oracle aborted at the deployed
path, because its baselines used a differently-stemmed file and never hit the
catalog/schema collision. A suite that does not test the filename the world
actually ships under is not testing the world.
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
from grade import split_sql

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE / "fixtures" / "workspace"
SOLUTION = HERE.parent / "solution" / "solution.sh"
TORN_TS = "2026-03-03 08:15:00"

GOOD_ARTIFACT_PATH = None      # filled from the oracle's own report


def attach(path: Path, alias: str = "wh", read_only: bool = False, con=None):
    """NEVER open a warehouse file directly - the deployed name makes catalog
    and schema collide and every warehouse.<table> reference becomes ambiguous."""
    if con is None:
        con = duckdb.connect(":memory:", config={"threads": 1})
    ro = " (READ_ONLY)" if read_only else ""
    con.execute("ATTACH '" + str(path).replace("'", "''") + f"' AS {alias}" + ro)
    return con


def open_world(world: Path):
    con = attach(world)
    con.execute("USE wh.warehouse")
    return con


def fresh(fixtures: Path, tmp: Path) -> tuple[Path, Path]:
    """Stage the world at its contract path and the repo alongside it."""
    d = tmp / "var" / "warehouse"
    d.mkdir(parents=True)
    w = d / "warehouse.duckdb"
    shutil.copy(fixtures / "world.duckdb", w)
    root = tmp / "workspace"
    shutil.copytree(WORKSPACE, root)
    return w, root


def grade(world: Path, grading: Path, artifact: dict, tmp: Path, root=None) -> dict:
    ap = tmp / "artifact.json"
    ap.write_text(json.dumps(artifact), encoding="utf-8")
    cmd = [sys.executable, str(HERE / "grade.py"), "--world", str(world),
           "--grading", str(grading), "--artifact", str(ap), "--json"]
    if root:
        cmd += ["--world-root", str(root)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return json.loads(r.stdout)


# ------------------------------------------------------------------- agents
def run_oracle(world: Path, root: Path) -> dict:
    """Runs the SHIPPED solution.sh - the sage-agent check. Testing an inline
    copy would validate the copy, not the artefact that ships with the task."""
    env = {**os.environ, "WAREHOUSE": str(world), "WORKSPACE": str(root)}
    bash = shutil.which("bash") or "bash"
    r = subprocess.run([bash, SOLUTION.as_posix()], capture_output=True,
                       text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError("oracle solution.sh FAILED:\n" + r.stdout + r.stderr)
    return json.loads((root / "final_report.json").read_text(encoding="utf-8"))


def agent_oracle(world, root, grading):
    return run_oracle(world, root)


def agent_no_op(world, root, grading):
    return {"primary_cause": "", "damage_window": [], "supporting_evidence": [],
            "repair_strategy": "", "remaining_risks": [], "additional_causes": []}


def agent_reference_copy(world, root, grading):
    """The exploit that killed the first rubric: copy the answer instead of
    diagnosing. Under correct topology the ATTACH must FAIL."""
    try:
        con = open_world(world)
        attach(grading / "expected.duckdb", "ref", read_only=True, con=con)
        for t in ("warehouse.mart_revenue_fact", "warehouse.mart_revenue_dim",
                  "warehouse.rev_snapshot", "staging.stg_revenue_lines",
                  "raw.fx_rates_daily"):
            con.execute(f"DELETE FROM {t}")
            con.execute(f"INSERT INTO {t} SELECT * FROM ref.{t}")
        con.close()
        return _good_artifact(), True            # reachable == topology failure
    except Exception:
        return agent_no_op(world, root, grading), False


SNAP_REWRITE = """
CREATE OR REPLACE TABLE warehouse.rev_snapshot AS
WITH t AS (SELECT DISTINCT snapshot_ts FROM warehouse.rev_snapshot),
dayreg AS (
  SELECT f.order_dt, d.region, SUM(f.revenue_usd) AS rev, SUM(f.order_count) AS cnt
  FROM warehouse.mart_revenue_fact f
  JOIN warehouse.mart_revenue_dim d ON d.customer_id = f.customer_id
  GROUP BY 1, 2),
acct AS (SELECT region, COUNT(*)::BIGINT AS n FROM warehouse.mart_revenue_dim GROUP BY 1),
agg AS (
  SELECT t.snapshot_ts, g.region, SUM(g.rev) AS rev, SUM(g.cnt) AS cnt
  FROM t JOIN dayreg g
    ON g.order_dt >= date_trunc('month', t.snapshot_ts::DATE)
   AND g.order_dt < t.snapshot_ts::DATE
  GROUP BY 1, 2)
SELECT t.snapshot_ts                              AS snapshot_ts,
       a.region                                   AS region,
       ROUND(COALESCE(g.rev, 0), 2)::DECIMAL(16,2) AS mtd_revenue_usd,
       COALESCE(g.cnt, 0)::BIGINT                  AS mtd_order_count,
       a.n                                         AS active_accounts
FROM t CROSS JOIN acct a
LEFT JOIN agg g ON g.snapshot_ts = t.snapshot_ts AND g.region = a.region
ORDER BY 1, 2
"""


def agent_full_refresh(world, root, grading):
    """The reflex repair. Clear the debris so the job runs again, re-run the
    pipeline exactly as it stands, and make the reporting table agree with the
    mart. It destroys the leftover relation and the reporting record together -
    the two things that are the only evidence - while looking tidy."""
    con = open_world(world)
    con.execute("DROP TABLE IF EXISTS warehouse.mart_revenue_dim__dbt_backup")
    con.execute("DROP TABLE IF EXISTS warehouse.mart_revenue_fact__dbt_backup")
    for st in split_sql((root / "pipelines" / "mart_rebuild.sql")
                        .read_text(encoding="utf-8")):
        con.execute(st)
    con.execute(SNAP_REWRITE)
    con.close()
    a = _good_artifact()
    a["repair_strategy"] = ("dropped the leftover relations, full-refreshed the "
                            "mart and rebuilt rev_snapshot from it")
    return a


ATOMIC_REBUILD = """-- Revenue mart rebuild. The publish is now one transaction,
-- and the guards make it re-runnable after an interrupted run.
begin transaction;
drop table if exists warehouse.mart_revenue_fact__dbt_tmp;
drop table if exists warehouse.mart_revenue_dim__dbt_tmp;
drop table if exists warehouse.mart_revenue_fact__dbt_backup;
drop table if exists warehouse.mart_revenue_dim__dbt_backup;
create table warehouse.mart_revenue_fact__dbt_tmp as
select l.order_dt, l.customer_id, l.customer_segment,
       round(sum(l.amount_usd), 2)::decimal(16,2) as revenue_usd,
       count(*)::bigint as order_count
from staging.stg_revenue_lines l
-- DATA-5518 (rkulkarni 2026-03-03): mart was double counting the reseller
-- lines.  Excluding the segment until the source feed is fixed.
where l.customer_segment <> 'RESELLER'
group by 1, 2, 3;
create table warehouse.mart_revenue_dim__dbt_tmp as
select customer_id, region, customer_segment from staging.stg_customer;
alter table warehouse.mart_revenue_fact rename to mart_revenue_fact__dbt_backup;
alter table warehouse.mart_revenue_fact__dbt_tmp rename to mart_revenue_fact;
alter table warehouse.mart_revenue_dim rename to mart_revenue_dim__dbt_backup;
alter table warehouse.mart_revenue_dim__dbt_tmp rename to mart_revenue_dim;
drop table warehouse.mart_revenue_fact__dbt_backup;
drop table warehouse.mart_revenue_dim__dbt_backup;
commit;
"""


def agent_rebuild_only(world, root, grading):
    """Diagnoses the interleave correctly, hardens the publish, merges the rate
    backfill - and treats the analyst's predicate as the existing fix, leaving
    an entire customer segment out of the mart and the reporting record alone.
    Fixing the pipeline without accounting for the compensating change."""
    (root / "pipelines" / "mart_rebuild.sql").write_text(ATOMIC_REBUILD,
                                                         encoding="utf-8")
    con = open_world(world)
    con.execute("""
      INSERT INTO raw.fx_rates_daily (rate_date, currency, rate_to_usd, source_file)
      SELECT i.rate_date, i.currency, i.rate_to_usd, i.source_file
      FROM raw.fx_rates_inbox i
      WHERE NOT EXISTS (SELECT 1 FROM raw.fx_rates_daily d
                        WHERE d.rate_date = i.rate_date AND d.currency = i.currency)""")
    con.execute("""
      CREATE OR REPLACE TABLE staging.stg_revenue_lines AS
      SELECT l.order_id, l.line_nbr, l.order_dt, l.customer_id,
             c.customer_segment, l.currency, l.amount_local,
             f.rate_to_usd AS fx_rate,
             ROUND(l.amount_local * f.rate_to_usd, 2)::DECIMAL(16,2) AS amount_usd,
             l.loaded_at
      FROM raw.revenue_lines l
      JOIN raw.customer_master c USING (customer_id)
      ASOF JOIN raw.fx_rates_daily f
        ON l.currency = f.currency AND l.order_dt >= f.rate_date""")
    con.execute("DROP TABLE IF EXISTS warehouse.mart_revenue_dim__dbt_backup")
    for st in split_sql(ATOMIC_REBUILD):
        con.execute(st)
    con.close()
    a = _good_artifact()
    a["repair_strategy"] = ("wrapped the mart publish in a transaction and merged "
                            "the APAC rate backfill; left the DATA-5518 filter in "
                            "place as the existing fix")
    return a


CHECKLIST_CAUSES = [
        "the APAC exchange rate series stopped arriving on 2026-02-16",
        "31 order lines on 2026-02-24 were delivered twice by the vendor",
        "the daily_revenue_load retried three times on 2026-03-03 and double loaded",
        "schema drift in the upstream customer feed",
        "clock skew between the scheduler and the warehouse",
        "a timezone boundary in the order date cast",
    "late-arriving data outside the watermark",
]


def agent_checklist(world, root, grading):
    """Correct primary cause and every plausible cause it can name. The
    false-cause penalty must bite."""
    a = run_oracle(world, root)
    a["additional_causes"] = list(CHECKLIST_CAUSES)
    return a


def agent_checklist_joined(world, root, grading):
    """F620 regression. The SAME seven causes, five of them false, packed into
    ONE list element.

    GT001's penalty counted list elements while its credit read the joined blob,
    so this exact transformation turned a 17.0 into a 19.0 that TIED THE ORACLE.
    It must score the same as agent_checklist here, and it must still lose to
    the oracle."""
    a = run_oracle(world, root)
    a["additional_causes"] = ["; ".join(CHECKLIST_CAUSES)]
    return a


def agent_schema_vandal(world, root, grading):
    """Not a plausible agent - a grader regression.

    An independent reviewer found that GT001's grader CRASHED rather than
    scoring a rollout that destroyed a table's schema. A grader that raises on a
    mangled world produces no score at all instead of the low one the run
    earned, which is strictly worse than being wrong. This baseline drops the
    reporting record's columns out from under every check that reads it and
    asserts the grader still returns a number, and a small one."""
    con = open_world(world)
    con.execute("CREATE OR REPLACE TABLE warehouse.rev_snapshot AS "
                "SELECT 1 AS wat FROM range(3)")
    con.execute("DROP TABLE IF EXISTS warehouse.mart_revenue_dim")
    con.execute("CREATE OR REPLACE TABLE warehouse.mart_revenue_fact AS "
                "SELECT 1 AS nope")
    con.close()
    a = _good_artifact()
    a["repair_strategy"] = "rolled out a rewrite that destroyed the schemas"
    return a


_GOOD: dict = {}


def _good_artifact() -> dict:
    """The oracle's own report, so every non-oracle baseline is scored on its
    REPAIR rather than being handed a worse narrative. A baseline that failed
    because its prose was weaker would tell us nothing about the task."""
    return json.loads(json.dumps(_GOOD))


def prime_good_artifact(fixtures: Path):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        w, root = fresh(fixtures, tmp)
        _GOOD.update(run_oracle(w, root))


# ------------------------------------------------- structural claims we make
def evidence_state(world: Path, grading: Path) -> tuple[bool, bool]:
    """Is the leftover relation still there, and is the reporting record intact?"""
    con = attach(world, "w", read_only=True)
    attach(grading / "expected.duckdb", "e", read_only=True, con=con)
    backup = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_catalog = 'w' "
        "AND table_name = 'mart_revenue_dim__dbt_backup'").fetchone()[0] > 0
    cols = "snapshot_ts, region, mtd_revenue_usd, mtd_order_count, active_accounts"
    try:
        intact = con.execute(f"""
        SELECT COUNT(*) FROM (
          SELECT {cols} FROM w.warehouse.rev_snapshot
          WHERE snapshot_ts <> TIMESTAMP '{TORN_TS}'
          EXCEPT ALL
          SELECT {cols} FROM e.warehouse.rev_snapshot
          WHERE snapshot_ts <> TIMESTAMP '{TORN_TS}')""").fetchone()[0] == 0
    except Exception:
        intact = False            # a table that no longer has the record's shape
    con.close()                   # is not an intact record
    return backup, intact


AGENTS = {"oracle": agent_oracle, "no_op": agent_no_op,
          "reference_copy": agent_reference_copy,
          "full_refresh": agent_full_refresh,
          "rebuild_only": agent_rebuild_only,
          "checklist": agent_checklist,
          "checklist_joined": agent_checklist_joined,
          "schema_vandal": agent_schema_vandal}


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

    prime_good_artifact(fx)

    rows, topology_ok, evidence = [], None, {}
    for name, fn in AGENTS.items():
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            w, root = fresh(fx, tmp)
            if name == "reference_copy":
                art, reachable = fn(w, root, grading)
                topology_ok = not reachable
            else:
                art = fn(w, root, grading)
            evidence[name] = evidence_state(w, grading)
            res = grade(w, grading, art, tmp, root=root)
            rows.append((name, res["total"], res["max"], res))

    mx = rows[0][2]
    print(f"\n  {'baseline':<16}{'score':>8}   expected")
    print("  " + "-" * 58)
    expect = {"oracle": f"= {mx} (max)", "no_op": "<= 1",
              "reference_copy": "high here, 0 under real topology",
              "full_refresh": "partial, NOT 0",
              "rebuild_only": "< oracle",
              "checklist": "< oracle (penalty)",
              "checklist_joined": "== checklist (F620)",
              "schema_vandal": "scored, not crashed"}
    for n, t, m, _ in rows:
        print(f"  {n:<16}{t:>8} /{m:<4} {expect[n]}")

    print(f"\n  {'baseline':<16}{'backup relation':>18}{'record intact':>16}")
    print("  " + "-" * 58)
    for n, _t, _m, _r in rows:
        b, i = evidence[n]
        print(f"  {n:<16}{('present' if b else 'DESTROYED'):>18}"
              f"{('yes' if i else 'REWRITTEN'):>16}")

    d = {n: t for n, t, _, _ in rows}
    fb, fi = evidence["full_refresh"]
    ob, oi = evidence["oracle"]
    print()
    verdicts = [
        ("oracle scores max", d["oracle"] == mx),
        ("no_op scores <= 1", d["no_op"] <= 1),
        ("reference_copy scores HIGH here, proving topology is the only defence",
         d["reference_copy"] >= mx - 6),
        ("full_refresh is partial, not zero", 0 < d["full_refresh"] < mx),
        ("full_refresh destroys the leftover relation AND the record - both "
         "pieces of evidence", (not fb) and (not fi)),
        ("rebuild_only < oracle: hardening without undoing the compensating "
         "change fails", d["rebuild_only"] < d["oracle"]),
        ("checklist penalised vs oracle", d["checklist"] < d["oracle"]),
        ("F620: joining the causes into one string does not defeat the penalty",
         d["checklist_joined"] == d["checklist"] < d["oracle"]),
        ("the oracle preserves the record it was asked to preserve", oi),
        ("a schema-destroying rollout is SCORED, not crashed on, and scores low",
         d["schema_vandal"] < d["no_op"] + 8),
    ]
    bad = 0
    for label, ok in verdicts:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        bad += 0 if ok else 1
    print(f"\n  {'TASK VALID' if not bad else f'TASK BROKEN ({bad} failing)'}")
    print("  every baseline above ran against var/warehouse/warehouse.duckdb, "
          "the contract filename")
    if topology_ok is False:
        print("  NOTE: fixtures share a directory, so ATTACH succeeds here by design.\n"
              "        Track A must prove unreachability in the real container.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
