#!/usr/bin/env bash
#
# GT006 reference solution - the oracle.
#
#   WORLD_ROOT=<world root> bash solution/solution.sh
#
# It must score MAX. It is also the shortest honest description of what the unit
# asks for, so it is written to be read: four steps, each one of them something a
# data engineer would actually do, and nothing that depends on knowing a number
# the agent could not have measured.
#
# ONE ORACLE, FOUR CONTEXTS. GT006's two stacked arms (`stacked-neutral`,
# `stacked-ticket`) carry a SECOND, easy, independent defect A: the merchant
# statements pack inner-joins a commercial merchant list that stops short, so
# every merchant above it is absent from the report. Step 0 below repairs it.
# Whether there IS an A is read OUT OF THE WORLD - does the rollup model join to
# a merchant dimension - rather than out of a flag, because the oracle is handed
# a world root and nothing else, and a second copy of this file is exactly the
# drift the variants exist to prevent. In the unstacked arms step 0 prints one
# line and does nothing.
#
# WHAT IT DOES NOT DO, and each omission is load-bearing:
#
#   * it does not delete the exclusion. The processor's test harness is real and
#     must stay out. An agent that "fixes" the loss by removing the rule trades
#     one silent failure for another and is scored for it (G1b, G1c).
#   * it does not add a filter. Every additional predicate on this load removes
#     more data; the archetype's whole point is that the remediation instinct is
#     anti-correlated with success here.
#   * it does not touch raw.payments_landing. That is the processor's delivery
#     and the ground rules make it read-only (G3).
#   * it does not rewrite ops.*. The operational record is evidence. Leaving a
#     new control behind is an APPEND, and step 4 does exactly that.
#
# THE FIX, in one line: `NOT IN` is NULL-unsafe. When terminal_ref IS NULL the
# predicate is UNKNOWN, not TRUE, and the row is excluded. `IS DISTINCT FROM` is
# the NULL-safe form and rejects exactly the two reserved values and nothing
# else.
set -euo pipefail

WORLD_ROOT="${WORLD_ROOT:-/}"
WAREHOUSE="${WAREHOUSE:-$WORLD_ROOT/var/warehouse/warehouse.duckdb}"
WORKSPACE="${WORKSPACE:-$WORLD_ROOT/workspace}"
PYTHON="${PYTHON:-python}"

echo "[oracle] warehouse = $WAREHOUSE"
echo "[oracle] workspace = $WORKSPACE"

MODEL="dbt_project/models/reporting/rpt_merchant_monthly.sql"

# Is defect A in this world? The rollup model joining to a merchant dimension is
# the whole of A; no join, no A.
STACKED="$("$PYTHON" - "$WORKSPACE" "$MODEL" <<'PYEOF'
import sys
from pathlib import Path

model = Path(sys.argv[1]) / sys.argv[2]
body = model.read_text(encoding="utf-8") if model.is_file() else ""
print("1" if "dim_merchant" in body else "")
PYEOF
)"

# ---------------------------------------------------------------------------
# 0. DEFECT A, where it exists. The statements pack drops every merchant the
#    commercial list does not carry, because the rollup model joins INNER to it
#    for the name. Join LEFT: a merchant that settles is on the statement
#    whether or not commercial has typed its name in yet. (Completing
#    config/merchants.csv and reloading reporting.dim_merchant is the other
#    correct repair; it passes A1/A2 too. This one is durable for the next
#    merchant as well.)
# ---------------------------------------------------------------------------
if [ -n "$STACKED" ]; then
"$PYTHON" - "$WORKSPACE" "$MODEL" <<'PYEOF'
import sys
from pathlib import Path

p = Path(sys.argv[1]) / sys.argv[2]
body = p.read_text(encoding="utf-8")
SHIPPED = "join {{ source('reporting', 'dim_merchant') }} m"
if SHIPPED not in body:
    raise SystemExit("rpt_merchant_monthly.sql: the shipped join is not there to repair")
body = body.replace(
    "-- Merchant names come from the commercial team's list (reporting.dim_merchant).",
    "-- Merchant names come from the commercial team's list (reporting.dim_merchant).\n"
    "-- LEFT join: the list is maintained by hand and lags onboarding, and a\n"
    "-- merchant that settled must be on the statement whether or not it has a\n"
    "-- name yet. The inner join here dropped every merchant the list lacked.")
body = body.replace(SHIPPED, "left " + SHIPPED)
p.write_text(body, encoding="utf-8", newline="")
print("[oracle] repaired the rollup model: inner join -> left join")
PYEOF
else
echo "[oracle] no merchant dimension in this world: no defect A to repair"
fi

# ---------------------------------------------------------------------------
# 1. Repair the rule, in BOTH files that carry it.
#
# pipelines/README.md says so in as many words, and DATA-2071's own comment says
# the deploy only changed one of them at first. Fixing the nightly and leaving
# the backfill is how this comes back the next time anyone restates a range.
# ---------------------------------------------------------------------------
"$PYTHON" - "$WORKSPACE" <<'PYEOF'
import sys
from pathlib import Path

ws = Path(sys.argv[1])
BROKEN = "terminal_ref NOT IN ('TEST-HARNESS', 'SMOKE-TEST')"
FIXED = ("terminal_ref IS DISTINCT FROM 'TEST-HARNESS'\n"
         "  AND l.terminal_ref IS DISTINCT FROM 'SMOKE-TEST'")

for rel in ("pipelines/build_payments.sql", "pipelines/backfill_payments.sql"):
    p = ws / rel
    body = p.read_text(encoding="utf-8")
    if BROKEN not in body:
        raise SystemExit(f"{rel}: the shipped predicate is not there to repair")
    body = body.replace(BROKEN, FIXED)
    body = body.replace(
        "  -- DATA-2071: keep the processor's test harness out of the mart.",
        "  -- DATA-2071: keep the processor's test harness out of the mart.\n"
        "  -- DATA-1755: NULL-safe. `NOT IN` evaluates to UNKNOWN - not TRUE -\n"
        "  -- when terminal_ref IS NULL, so the original form also excluded every\n"
        "  -- card-not-present payment on a legacy-acquirer merchant, which has no\n"
        "  -- terminal to report. IS DISTINCT FROM rejects the two reserved values\n"
        "  -- and nothing else.")
    p.write_text(body, encoding="utf-8", newline="")
    print(f"[oracle] repaired {rel}")
PYEOF

# The backfill's date range is still the one DATA-2071 used. Point it at the
# window the defect actually covers before running it.
"$PYTHON" - "$WORKSPACE" <<'PYEOF'
import sys
from pathlib import Path

ws = Path(sys.argv[1])
p = ws / "pipelines" / "backfill_payments.sql"
body = p.read_text(encoding="utf-8")
body = body.replace("DATE '2025-12-05'", "DATE '2025-12-08'")
body = body.replace("DATE '2025-12-07'", "DATE '2026-03-14'")
p.write_text(body, encoding="utf-8", newline="")
print("[oracle] backfill range set to the damage window")
PYEOF

# ---------------------------------------------------------------------------
# 2. Restate the mart over the damage window, with the repaired loader.
#
# Aliased ATTACH (F622). Opening var/warehouse/warehouse.duckdb directly names
# the catalog `warehouse` as well as the schema and every two-part reference
# raises Binder Error - Ambiguous reference.
# ---------------------------------------------------------------------------
"$PYTHON" - "$WAREHOUSE" "$WORKSPACE" <<'PYEOF'
import sys
from pathlib import Path

import duckdb

wh, ws = Path(sys.argv[1]), Path(sys.argv[2])
con = duckdb.connect(":memory:", config={"threads": 1})
con.execute(f"ATTACH '{wh.as_posix()}' AS wh")
con.execute("USE wh")


def statements(text):
    out, buf, i, n = [], [], 0, len(text)
    s = d = line = block = False
    while i < n:
        c, nxt = text[i], text[i + 1] if i + 1 < n else ""
        if line:
            if c == "\n":
                line = False
                buf.append(c)
            i += 1
            continue
        if block:
            if c == "*" and nxt == "/":
                block = False
                i += 2
                continue
            i += 1
            continue
        if s or d:
            buf.append(c)
            if (s and c == "'") or (d and c == '"'):
                s = d = False
            i += 1
            continue
        if c == "-" and nxt == "-":
            line = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            block = True
            i += 2
            continue
        if c == "'":
            s = True
        elif c == '"':
            d = True
        if c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


before = con.execute("SELECT count(*) FROM warehouse.fct_payments").fetchone()[0]

# Run the agent's OWN repaired backfill, not a hand-written restatement. If the
# repaired file does not restate the mart correctly, this fails here rather than
# passing a gate on data that was written some other way.
for stmt in statements((ws / "pipelines" / "backfill_payments.sql")
                       .read_text(encoding="utf-8")):
    con.execute(stmt)

after = con.execute("SELECT count(*) FROM warehouse.fct_payments").fetchone()[0]
print(f"[oracle] mart restated: {before:,} -> {after:,} rows (+{after - before:,})")

# ------------------------------------------------------------------------
# 3. Rebuild everything downstream of the mart, including the materialised
#    extract the dashboard reads. The dashboard reads the extract, not the
#    mart, so a repair that stops at the mart leaves the wrong number in front
#    of the business.
# ------------------------------------------------------------------------
con.execute("""
CREATE OR REPLACE TABLE warehouse.kpi_payments_daily AS
SELECT payment_dt AS dt,
       COUNT(*) AS payments,
       COUNT(DISTINCT merchant_id) AS merchants,
       CAST(SUM(amount_gross) AS DECIMAL(14,2)) AS gross_amount,
       CAST(SUM(fee_amount)   AS DECIMAL(14,2)) AS fee_amount,
       CAST(SUM(amount_net)   AS DECIMAL(14,2)) AS net_amount
FROM warehouse.fct_payments
GROUP BY 1 ORDER BY 1
""")
# The rollup, THROUGH THE MODEL AS IT NOW STANDS: render the two dbt macros this
# project uses and materialise exactly what `dbt run` would. Written this way
# rather than as a hand-rolled GROUP BY so that the table and the code agree -
# in the stacked arms that means the repair of A that step 0 made is what the
# report is rebuilt from, and in the unstacked arms it produces exactly the
# rollup GT006 always produced, because the shipped model IS that GROUP BY.
# (dbt itself would land it under main_reporting without a generate_schema_name
# macro; the business reads reporting.rpt_merchant_monthly, so materialise
# there.)
import re
model = (ws / "dbt_project" / "models" / "reporting" / "rpt_merchant_monthly.sql"
         ).read_text(encoding="utf-8")
model = re.sub(r"\{\{\s*config\(.*?\)\s*\}\}", "", model, flags=re.S)
model = re.sub(r"\{\{\s*source\(\s*'(\w+)'\s*,\s*'(\w+)'\s*\)\s*\}\}", r"\1.\2", model)
assert "{{" not in model, "unrendered jinja in the rollup model"
con.execute("CREATE OR REPLACE TABLE reporting.rpt_merchant_monthly AS " + model)
con.execute("CREATE OR REPLACE TABLE warehouse.bi_payments_extract AS "
            "SELECT * FROM warehouse.kpi_payments_daily")
print("[oracle] kpi, merchant rollup and BI extract rebuilt")

# ------------------------------------------------------------------------
# 4. Leave behind the control that would have caught this: a per-partition
#    reconciliation between the feed and the mart. DATA-1712 has been asking
#    for it since November.
#
#    APPEND to ops.dq_checks; never rewrite it. The operational record is
#    evidence, and a control that edits history is not a control.
# ------------------------------------------------------------------------
con.execute("""
INSERT INTO ops.dq_checks
SELECT (SELECT max(checked_at) FROM ops.dq_checks) AS checked_at,
       'warehouse.fct_payments' AS dataset,
       'reconcile_mart_to_landing' AS check_name,
       CASE WHEN gap = 0 THEN 'PASS' ELSE 'FAIL' END AS status,
       gap AS failed_rows
FROM (
  SELECT (SELECT count(*) FROM raw.payments_landing l
           WHERE l.terminal_ref IS DISTINCT FROM 'TEST-HARNESS'
             AND l.terminal_ref IS DISTINCT FROM 'SMOKE-TEST')
       - (SELECT count(*) FROM warehouse.fct_payments) AS gap)
""")
gap = con.execute("SELECT failed_rows FROM ops.dq_checks "
                  "WHERE check_name = 'reconcile_mart_to_landing'").fetchone()[0]
print(f"[oracle] reconciliation control added; gap now {gap}")
con.execute("CHECKPOINT")
con.close()
PYEOF

# ---------------------------------------------------------------------------
# 5. The deliverable.
# ---------------------------------------------------------------------------
"$PYTHON" - "$WORKSPACE" "$STACKED" <<'PYEOF'
import json
import sys
from pathlib import Path

report = {
    "primary_cause": (
        "The DATA-2071 exclusion in pipelines/build_payments.sql is NULL-unsafe. "
        "`terminal_ref NOT IN ('TEST-HARNESS','SMOKE-TEST')` evaluates to UNKNOWN "
        "rather than TRUE when terminal_ref IS NULL, so since it was deployed the "
        "nightly load has silently dropped every card-not-present payment on a "
        "legacy-acquirer merchant - which legitimately has no terminal_ref - as "
        "well as the processor test-harness rows it was written to remove."),
    "damage_window": ["2025-12-08", "2026-03-14"],
    "supporting_evidence": [
        "raw.payments_landing vs warehouse.fct_payments per partition: the "
        "survival rate is 100% before 2025-12-08 and steps down from that date",
        "every row missing from the mart has terminal_ref IS NULL; no row with a "
        "populated terminal_ref is missing",
        "pipelines/build_payments.sql and pipelines/backfill_payments.sql both "
        "carry the predicate; config/checks.yml documents it as DATA-2071",
        "kb de/reference/terminal-ref: the column is legitimately empty for "
        "card-not-present payments on the legacy acquirer",
        "DATA-2071 and de/changelog/harness-exclusion date the deploy to "
        "2025-12-08, which is where the step is",
    ],
    "repair_strategy": (
        "Replaced the NULL-unsafe predicate with IS DISTINCT FROM in both "
        "pipeline files, restated warehouse.fct_payments over the damage window "
        "with the repaired backfill, and rebuilt kpi_payments_daily, "
        "rpt_merchant_monthly and the bi_payments_extract the dashboard reads. "
        "The exclusion itself is kept: the processor's test-harness rows are not "
        "payments and must stay out. Added the per-partition reconciliation "
        "control DATA-1712 has been asking for since November."),
    "remaining_risks": [
        "the reconciliation control is a single scalar gap, not per-partition; "
        "a compensating pair of errors on different days would net to zero",
        "nothing anywhere else in the repo was audited for the same NULL-unsafe "
        "pattern",
        "the board pack for every week from 2025-12-08 onward was built on short "
        "numbers and needs restating",
    ],
    # DEFECT A, where there was one. H1 pays one point per defect named in the
    # stacked arms, and an honest report of a world with two defects names both.
    "additional_causes": ([
        "The merchant statements pack (reporting.rpt_merchant_monthly) inner-joined "
        "reporting.dim_merchant, the commercial team's list from config/merchants.csv, "
        "which stops at M0030; every merchant above it was missing from the report "
        "entirely. Changed the join to LEFT so a settling merchant is on the statement "
        "with or without a name.",
    ] if len(sys.argv) > 2 and sys.argv[2] else []),
}
path = Path(sys.argv[1]) / "final_report.json"
path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="")
print(f"[oracle] wrote {path}")
PYEOF

echo "[oracle] done"
