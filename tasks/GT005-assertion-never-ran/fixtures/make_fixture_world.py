#!/usr/bin/env python3
"""
GT005 world generator - "the assertion that never ran".

WHAT THIS BUILDS, AND THE ONE MECHANISM IT EXISTS FOR
-----------------------------------------------------
A commercial dbt project of ~90 models and ~500 generic tests whose nightly
build has been green for six weeks, on top of a warehouse whose revenue mart is
short by a few percent.

The single assertion that would have caught the shortfall is a `relationships`
test declared in `models/staging/legacy/_conformance.yml`, in git, readable,
correct - and sitting inside a directory named by `.dbtignore`. dbt excludes an
ignored path at FILE-READ time, so the test is:

    * present in the working tree and in git history
    * ABSENT from target/manifest.json `nodes`
    * ABSENT from target/manifest.json `disabled`
    * ABSENT from run_results.json
    * accompanied by NO log line, warning, error or deprecation notice

VERIFIED against the installed engine (dbt-core 1.12.4 / dbt-duckdb 1.11.0), not
asserted: see fixture_contract.CONTRACT["mechanism"] and `verify()`'s
`silent_drop` check, which re-parses the shipped project and refuses a fixture in
which the test has become visible by any route.

The underlying data defect is orthogonal to GT004/A009. Nothing is duplicated
and no materialisation changes. On 2026-01-14 the EMEA billing estate moved onto
a new vendor instance whose extract stamps the vendor code `bill_eu2`. The
conformed registry was updated with the CONFORMED code `billing_emea_v2` and a
crosswalk row was added to `raw.source_system_alias`, but
`stg_billing__invoice_line` passes the vendor code through unresolved - it
predates the alias convention that `stg_ar__payment` in the same folder already
follows. `fct_invoice_line` inner-joins to `dim_source_system`, so every EMEA
line since the cutover is silently discarded.

DETERMINISM
-----------
One `random.Random(seed)` drawn in a fixed order, sorted iteration over every
collection, no wall clock and no uuid. Every text file is written with
`newline=""` so a Windows host and a Linux container emit the same bytes.
`.duckdb` containers are NOT byte-reproducible, so the contract publishes
content digests (md5 per row, md5 over the sorted row hashes) exactly as GT004
does, never a file hash.

    python make_fixture_world.py --out build --seed 5005
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent

SEED = 5005
VNOW = "2026-04-06T09:00:00Z"

# ---------------------------------------------------------------- the story
# Every date here is a world fact; fixture_contract.CONTRACT republishes the
# measured consequences and nothing downstream may hardcode either.
HIST_START = "2025-10-01"      # first invoice_date in the extract
HIST_END = "2026-03-27"        # last invoice_date the nightly extract has landed
CUTOVER = "2026-01-14"         # RFC-118: EMEA moves to the new vendor instance

VENDOR_CODE = "bill_eu2"            # what the extract stamps, post-cutover
CONFORMED_CODE = "billing_emea_v2"  # what the conformed registry carries
NOVEL_CODE = "bill_apac_x9"         # live-fire: in neither registry nor alias

DASHBOARD_ID = "revenue_close"
PROJECT_NAME = "commercial"

# The clutter. Thirteen source systems, six tables each: 78 staging models, each
# a real projection with real renames, each carrying six generic tests. The
# ratio is a scaled copy of the shape A050's notes measure in a live repo
# (S664), not an invented one - it is just scaled to a size whose full `dbt
# build` runs in ~30 s, because the live-fire gate runs three of them.
SOURCES: list[tuple[str, str, list[str]]] = [
    ("crm",       "Salesforce CRM",             ["account", "contact", "opportunity",
                                                 "lead", "campaign", "activity"]),
    ("erp",       "NetSuite ERP",               ["vendor", "purchase_order", "receipt",
                                                 "gl_entry", "cost_centre", "budget_line"]),
    ("support",   "Zendesk Support",            ["ticket", "comment", "sla_event",
                                                 "satisfaction", "macro_use", "queue"]),
    ("product",   "Product telemetry",          ["session", "feature_use", "error_event",
                                                 "install", "upgrade", "heartbeat"]),
    ("hr",        "Workday HR",                 ["worker", "assignment", "absence",
                                                 "cost_allocation", "grade", "location"]),
    ("web",       "Web analytics",              ["pageview", "conversion", "referrer",
                                                 "experiment", "consent", "search"]),
    ("logistics", "Warehouse management",       ["shipment", "pick", "pack",
                                                 "carrier_scan", "return_line", "bin_move"]),
    ("payments",  "Payment gateway",            ["authorisation", "capture", "refund",
                                                 "chargeback", "payout", "fee"]),
    ("marketing", "Marketing automation",       ["send", "open", "click",
                                                 "bounce", "suppression", "journey_step"]),
    ("partner",   "Partner portal",             ["deal_reg", "referral", "commission",
                                                 "tier_change", "training", "certification"]),
    ("legal",     "Contract lifecycle",         ["contract", "clause", "amendment",
                                                 "renewal", "approval", "signature"]),
    ("infra",     "Cloud billing export",       ["resource", "usage_hour", "commitment",
                                                 "credit", "tag_binding", "account_move"]),
    ("risk",      "Fraud & risk platform",      ["assessment", "rule_hit", "case",
                                                 "decision", "watchlist", "appeal"]),
]

STATUSES = ["active", "pending", "settled", "cancelled", "void"]
# `void` is filtered out by every staging model, so the accepted_values test on
# the conformed status never sees it. That is a real projection rule, not a
# convenience: it is why the tests pass.
KEPT_STATUSES = [s for s in STATUSES if s != "void"]

# Sixteen source systems in the conformed registry. `bill_emea_v1` is the
# retired predecessor; `billing_emea_v2` is what RFC-118 registered. The vendor
# instance code `bill_eu2` is NOT a registry code - it is a vendor code, and the
# crosswalk in raw.source_system_alias is where it resolves.
REGISTRY = [
    ("billing_amer_v2", "Billing - Americas",        "billing",  "active",  "2024-04-01", None),
    ("billing_emea_v2", "Billing - EMEA",            "billing",  "active",  CUTOVER,      None),
    ("billing_apac_v2", "Billing - APAC",            "billing",  "active",  "2024-04-01", None),
    ("billing_latam_v2", "Billing - LATAM",          "billing",  "active",  "2024-04-01", None),
    ("billing_nordic_v2", "Billing - Nordics",       "billing",  "active",  "2024-09-01", None),
    ("billing_dach_v2", "Billing - DACH",            "billing",  "active",  "2024-09-01", None),
    ("bill_emea_v1",    "Billing - EMEA (retired)",  "billing",  "retired", "2021-07-01", CUTOVER),
    ("ar_ledger",       "Accounts receivable",       "finance",  "active",  "2020-01-01", None),
    ("gl_primary",      "General ledger",            "finance",  "active",  "2020-01-01", None),
    ("crm_core",        "Salesforce CRM",            "commerce", "active",  "2021-03-01", None),
    ("erp_core",        "NetSuite ERP",              "commerce", "active",  "2021-03-01", None),
    ("support_core",    "Zendesk Support",           "service",  "active",  "2022-02-01", None),
    ("product_tel",     "Product telemetry",         "product",  "active",  "2022-09-01", None),
    ("web_analytics",   "Web analytics",             "product",  "active",  "2022-09-01", None),
    ("logistics_wms",   "Warehouse management",      "ops",      "active",  "2023-01-01", None),
    ("payments_gw",     "Payment gateway",           "finance",  "active",  "2023-01-01", None),
    ("partner_portal",  "Partner portal",            "commerce", "active",  "2023-06-01", None),
    ("infra_billing",   "Cloud billing export",      "platform", "active",  "2023-06-01", None),
    ("risk_platform",   "Fraud & risk platform",     "risk",     "active",  "2024-01-01", None),
]

# The crosswalk. One row per vendor code that does NOT already stamp its
# conformed code. The 2024 consolidation renamed most billing instances to emit
# the conformed code directly, which is exactly why nobody noticed that
# stg_billing__invoice_line never learned to resolve the ones that do not:
# for five instances out of six the passthrough happens to be correct.
#
# `bill_eu2` IS here. The reference data is complete; the transformation is not.
# That is what makes "UPDATE raw.billing_invoice_line" the wrong repair and the
# crosswalk join the right one.
ALIASES = [
    ("bill_eu2",   "billing_emea_v2", CUTOVER,      "RFC-118: EMEA vendor instance code"),
    ("bill_eu1",   "bill_emea_v1",    "2021-07-01", "retired EMEA instance"),
    ("ar",         "ar_ledger",       "2020-01-01", "AR extract short code"),
    ("gl",         "gl_primary",      "2020-01-01", "GL extract short code"),
]

# (region, currency, vendor code before the cutover, vendor code after it,
#  lines per day lo, hi). Only EMEA changes, and only EMEA's new code needs the
# crosswalk. Volumes are region sizes, not a dial: EMEA is the smallest estate,
# which is why a total that is short by every EMEA line since January still
# reads as a few per cent rather than a hole.
BILLING_INSTANCES = [
    ("amer",    "USD", "billing_amer_v2",   "billing_amer_v2",   4, 8),
    ("emea",    "EUR", "bill_emea_v1",      VENDOR_CODE,         2, 4),
    ("apac",    "JPY", "billing_apac_v2",   "billing_apac_v2",   4, 8),
    ("latam",   "USD", "billing_latam_v2",  "billing_latam_v2",  3, 6),
    ("nordics", "SEK", "billing_nordic_v2", "billing_nordic_v2", 3, 6),
    ("dach",    "EUR", "billing_dach_v2",   "billing_dach_v2",   3, 6),
]


# ------------------------------------------------------------------- utils
def _write_text(path: Path, text: str) -> None:
    """Every text file in this world, written once, with no newline translation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _create(path: Path):
    """A writable, ALIASED attach of a fresh container (F622).

    `duckdb.connect('<x>/warehouse.duckdb')` makes the catalog name `warehouse`,
    which then collides with the `warehouse` SCHEMA and turns every two-part
    reference into `Ambiguous reference`. Connect to :memory: and ATTACH.
    """
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    Path(str(path) + ".wal").unlink(missing_ok=True)
    con = duckdb.connect()
    con.execute(f"ATTACH '{path.as_posix()}' AS wh")
    con.execute("USE wh")
    return con


def _readonly(path: Path, alias: str = "wh"):
    import duckdb

    con = duckdb.connect()
    con.execute(f"ATTACH '{Path(path).as_posix()}' AS {alias} (READ_ONLY)")
    return con


def _close(con) -> None:
    try:
        con.execute("CHECKPOINT")
    except Exception:
        pass
    con.close()


def _rng(seed: int, *parts: str) -> random.Random:
    """A child stream keyed by name, so adding a source cannot shift another's
    rows. Ordering of `SOURCES` is therefore not load-bearing for any digest."""
    key = f"{seed}|" + "|".join(parts)
    return random.Random(int(hashlib.md5(key.encode("utf-8")).hexdigest()[:12], 16))


# ------------------------------------------------------------- raw fixtures
def build_raw(db: Path, seed: int) -> dict:
    """Schemas raw + ops. The dbt project builds `warehouse` from these."""
    con = _create(db)
    con.execute("CREATE SCHEMA raw")
    con.execute("CREATE SCHEMA ops")

    # ---- reference data ---------------------------------------------------
    con.execute("""CREATE TABLE raw.source_system_registry(
        source_system_code VARCHAR, system_name VARCHAR, domain VARCHAR,
        status VARCHAR, effective_from DATE, retired_on DATE)""")
    con.executemany(
        "INSERT INTO raw.source_system_registry VALUES (?,?,?,?,?,?)",
        [list(row) for row in sorted(REGISTRY)])

    con.execute("""CREATE TABLE raw.source_system_alias(
        vendor_code VARCHAR, source_system_code VARCHAR,
        effective_from DATE, note VARCHAR)""")
    con.executemany("INSERT INTO raw.source_system_alias VALUES (?,?,?,?)",
                    [list(row) for row in sorted(ALIASES)])

    # ---- the billing chain ------------------------------------------------
    # Three vendor instances. EMEA stamps `bill_eu1` until the cutover and
    # `bill_eu2` from the cutover on; the other two never change. Amounts and
    # the per-day line count are drawn from one named stream so the digest does
    # not move when an unrelated source is edited.
    rng = _rng(seed, "billing")
    con.execute("""CREATE TABLE raw.billing_invoice_line(
        invoice_line_id BIGINT, invoice_id VARCHAR, vendor_system_code VARCHAR,
        customer_id BIGINT, invoice_date DATE, amount_usd DECIMAL(12,2),
        currency_code VARCHAR, line_status VARCHAR)""")
    rows, line_id = [], 700000
    con.execute(
        f"CREATE TEMP TABLE _days AS SELECT (DATE '{HIST_START}' + to_days(i::int)) AS d "
        f"FROM range(0, datediff('day', DATE '{HIST_START}', DATE '{HIST_END}') + 1) t(i)")
    days = [r[0].strftime("%Y-%m-%d") for r in
            con.execute("SELECT d FROM _days ORDER BY d").fetchall()]
    for day in days:
        iso = day
        for region, ccy, before, after, lo, hi in BILLING_INSTANCES:
            vendor = before if iso < CUTOVER else after
            for _ in range(rng.randint(lo, hi)):
                line_id += 1
                rows.append([
                    line_id,
                    f"INV-{region.upper()}-{iso.replace('-', '')}-{rng.randint(100, 999)}",
                    vendor,
                    900000 + rng.randint(0, 480),
                    iso,
                    round(rng.uniform(140.0, 9600.0), 2),
                    ccy,
                    "posted" if rng.random() > 0.04 else "draft",
                ])
    con.executemany("INSERT INTO raw.billing_invoice_line VALUES (?,?,?,?,?,?,?,?)", rows)

    # The retired v1 billing extract. It still lands, which is why the v1
    # staging models exist at all and why nobody deleted the directory.
    con.execute("""CREATE TABLE raw.billing_v1_invoice(
        invoice_no VARCHAR, account_no VARCHAR, issued_on DATE,
        total_eur DECIMAL(12,2), state VARCHAR)""")
    r1 = _rng(seed, "billing_v1")
    con.executemany(
        "INSERT INTO raw.billing_v1_invoice VALUES (?,?,?,?,?)",
        [[f"V1-{n:06d}", f"ACC-{r1.randint(1000, 1999)}",
          days[r1.randrange(0, 90)],
          round(r1.uniform(90.0, 4200.0), 2),
          r1.choice(["closed", "closed", "credited"])] for n in range(1, 241)])
    con.execute("""CREATE TABLE raw.billing_v1_account(
        account_no VARCHAR, account_name VARCHAR, opened_on DATE, region VARCHAR)""")
    con.executemany(
        "INSERT INTO raw.billing_v1_account VALUES (?,?,?,?)",
        [[f"ACC-{n}", f"Legacy account {n}", "2021-07-01", "EMEA"]
         for n in range(1000, 2000, 7)])
    con.execute("""CREATE TABLE raw.billing_v1_credit_note(
        note_no VARCHAR, invoice_no VARCHAR, issued_on DATE,
        amount_eur DECIMAL(12,2))""")
    r2 = _rng(seed, "billing_v1_cn")
    con.executemany(
        "INSERT INTO raw.billing_v1_credit_note VALUES (?,?,?,?)",
        [[f"CN-{n:05d}", f"V1-{r2.randint(1, 240):06d}",
          days[r2.randrange(0, 90)], round(r2.uniform(10.0, 600.0), 2)]
         for n in range(1, 61)])

    # Billing credit memos: the SIBLING CHAIN. Same author, same omission - the
    # staging model passes the vendor code through without resolving it - but
    # every code this feed has ever stamped happens to be a conformed one, so
    # the chain is correct today and contributes nothing to the shortfall.
    #
    # Its only job is to carry the OTHER assertion in the swallowed schema file.
    # An agent who repairs the invoice-line chain and writes itself a fresh
    # conformance test for that one model has patched the symptom; the same
    # exposure is still open here, and the live-fire gate plants a row on this
    # chain to say so. An agent who restored the FILE - or who audited what the
    # project declares against what it parses - covers both with one edit.
    con.execute("""CREATE TABLE raw.billing_credit_memo(
        credit_memo_id BIGINT, invoice_id VARCHAR, vendor_system_code VARCHAR,
        customer_id BIGINT, memo_date DATE, amount_usd DECIMAL(12,2),
        currency_code VARCHAR, memo_status VARCHAR)""")
    rcm = _rng(seed, "credit_memo")
    conformed = [code for code, _n, domain, status, _e, _r in REGISTRY
                 if domain == "billing" and status == "active"]
    con.executemany(
        "INSERT INTO raw.billing_credit_memo VALUES (?,?,?,?,?,?,?,?)",
        [[800000 + n, f"INV-CM-{rcm.randint(10000, 99999)}",
          conformed[rcm.randrange(0, len(conformed))],
          900000 + rcm.randint(0, 480), days[rcm.randrange(0, len(days))],
          round(rcm.uniform(20.0, 1800.0), 2),
          rcm.choice(["USD", "EUR", "JPY"]),
          "issued" if rcm.random() > 0.05 else "void"] for n in range(1, 641)])

    # AR payments: the sibling that ALREADY resolves through the crosswalk. Its
    # model is the in-repo pattern the billing repair copies.
    con.execute("""CREATE TABLE raw.ar_payment(
        payment_id BIGINT, vendor_system_code VARCHAR, customer_id BIGINT,
        received_on DATE, amount_usd DECIMAL(12,2), method VARCHAR)""")
    r3 = _rng(seed, "ar")
    con.executemany(
        "INSERT INTO raw.ar_payment VALUES (?,?,?,?,?,?)",
        [[500000 + n, "ar", 900000 + r3.randint(0, 480),
          days[r3.randrange(0, len(days))],
          round(r3.uniform(80.0, 5200.0), 2),
          r3.choice(["wire", "card", "direct_debit"])] for n in range(1, 361)])

    # ---- the clutter ------------------------------------------------------
    for src, _name, tables in SOURCES:
        for table in tables:
            full = f"{src}_{table}"
            r = _rng(seed, "clutter", full)
            con.execute(f"""CREATE TABLE raw.{full}(
                {table}_id BIGINT, {src}_code VARCHAR, status VARCHAR,
                amount DECIMAL(12,2), event_date DATE, updated_at TIMESTAMP)""")
            n = 120 + r.randrange(0, 240)
            con.executemany(
                f"INSERT INTO raw.{full} VALUES (?,?,?,?,?,?)",
                [[i, f"{src[:3].upper()}-{r.randrange(100, 999)}",
                  r.choice(STATUSES), round(r.uniform(5.0, 990.0), 2),
                  days[r.randrange(0, len(days))],
                  days[r.randrange(0, len(days))] + " 03:14:00"]
                 for i in range(1, n + 1)])

    # ---- ops: the green record -------------------------------------------
    con.execute("""CREATE TABLE ops.job_runs(
        run_id VARCHAR, job VARCHAR, started_at TIMESTAMP, ended_at TIMESTAMP,
        status VARCHAR, detail VARCHAR)""")
    con.execute("""CREATE TABLE ops.dq_checks(
        checked_at TIMESTAMP, check_name VARCHAR, dataset VARCHAR,
        failed_rows BIGINT, status VARCHAR)""")
    _close(con)
    return {"rows": len(rows), "days": len(days)}


def build_ops_history(db: Path, test_count: int, warn_count: int) -> dict:
    """Six weeks of nightly builds, every one green.

    The PASS/WARN/ERROR numbers are the ones the shipped suite actually
    produces - they are passed in from the real run, never invented. A record
    that disagreed with the project would be the first thing a careful agent
    noticed, and it would be noticing a fixture bug.
    """
    from datetime import datetime, timedelta

    con = _create_existing(db)
    end = datetime(2026, 4, 6, 1, 40)
    runs, checks = [], []
    for i in range(42):
        start = end - timedelta(days=41 - i)
        dur = 88 + (i * 7) % 23
        runs.append([f"r{2026000 + i:07d}", "dbt_build", start.isoformat(sep=" "),
                     (start + timedelta(seconds=dur)).isoformat(sep=" "), "success",
                     f"PASS={test_count - warn_count} WARN={warn_count} ERROR=0 SKIP=0"])
        for name, dataset in (("not_null_invoice_line_id", "warehouse.fct_invoice_line"),
                              ("row_count_gt_zero", "warehouse.fct_revenue_daily"),
                              ("freshness_invoice_date", "warehouse.fct_revenue_daily")):
            checks.append([(start + timedelta(seconds=dur + 60)).isoformat(sep=" "),
                           name, dataset, 0, "PASS"])
    con.executemany("INSERT INTO ops.job_runs VALUES (?,?,?,?,?,?)", runs)
    con.executemany("INSERT INTO ops.dq_checks VALUES (?,?,?,?,?)", checks)
    _close(con)
    return {"job_runs": len(runs), "dq_checks": len(checks), "failures": 0}


def _create_existing(path: Path):
    import duckdb

    con = duckdb.connect()
    con.execute(f"ATTACH '{Path(path).as_posix()}' AS wh")
    con.execute("USE wh")
    return con


# ---------------------------------------------------------- the dbt project
DBT_PROJECT_YML = f"""\
name: "{PROJECT_NAME}"
version: "2.4.1"
config-version: 2
profile: "{PROJECT_NAME}"

model-paths: ["models"]
macro-paths: ["macros"]
test-paths: ["tests"]
target-path: "target"
clean-targets: ["target", "dbt_packages"]

models:
  {PROJECT_NAME}:
    staging:
      +materialized: view
    marts:
      +materialized: table
"""

# The one file that decides whether the assertion exists. Everything about it
# is ordinary: a dated comment, a ticket reference, a directory that genuinely
# does hold retired models.
DBTIGNORE = """\
# Paths excluded from the dbt parse.
#
# DATA-5107 / RFC-118 (2026-01-14): the v1 billing staging models were retired
# with the vendor's v1 extract. The raw tables still land, so the models still
# compile, and parsing them costs us ~4s on every developer run for output
# nobody consumes. Excluded here until the directory is deleted in 2026.2.
models/staging/legacy/
"""

SOURCES_YML_HEADER = """\
version: 2

sources:
  - name: raw
    description: Landed extracts, one table per upstream feed. Loaded by Fivetran.
    schema: raw
    tables:
"""

# THE FILE `.dbtignore` SWALLOWS.
#
# It is not a "conformance file" that somebody parked in an odd place: it is the
# ORDINARY schema file for the live billing staging model, and it is the only
# patch that model has. It ended up in models/staging/legacy/ during RFC-118,
# when `_billing_v1__models.yml` was moved there and `_billing__models.yml` went
# with it - one character apart, one `git mv` too many. Three lines later
# `.dbtignore` was pointed at the directory, and from that commit on the live
# model has carried four declared tests and zero parsed ones.
BILLING_YML = """version: 2

models:
  - name: stg_billing__invoice_line
    description: Billing invoice lines with a conformed source system key.
    columns:
      - name: invoice_line_key
        description: Vendor line id. Unique across instances.
        data_tests:
          - unique
          - not_null
      - name: amount_usd
        data_tests:
          - not_null
      - name: source_system_code
        description: >-
          Conformed source system key, resolved from the vendor code through
          raw.source_system_alias. A key dim_source_system does not carry is a
          row fct_invoice_line's join will discard, and a discarded row is
          revenue we never report. See docs/adr/0006-conformed-source-systems.md.
        data_tests:
          - relationships:
              arguments:
                to: ref('dim_source_system')
                field: source_system_code
              config:
                severity: error

  - name: stg_billing__credit_memo
    description: Billing credit memos with a conformed source system key.
    columns:
      - name: credit_memo_key
        data_tests:
          - unique
          - not_null
      - name: source_system_code
        description: >-
          Same control as the invoice line feed, and for the same reason:
          fct_credit_memo joins the dimension inner, so an unconformed key is a
          memo that silently is not there.
        data_tests:
          - relationships:
              arguments:
                to: ref('dim_source_system')
                field: source_system_code
              config:
                severity: error
"""

AR_YML = """version: 2

models:
  - name: stg_ar__payment
    description: AR payments with a conformed source system key.
    columns:
      - name: payment_key
        data_tests:
          - unique
          - not_null
      - name: source_system_code
        data_tests:
          - relationships:
              arguments:
                to: ref('dim_source_system')
                field: source_system_code
              config:
                severity: error
"""


def clutter_models(seed: int) -> dict[str, str]:
    """78 staging models, each a real projection with real renames."""
    out: dict[str, str] = {}
    for src, _name, tables in SOURCES:
        for table in tables:
            full = f"{src}_{table}"
            out[f"models/staging/{src}/stg_{src}__{table}.sql"] = textwrap.dedent(f"""\
                -- {src}.{table}: landed extract -> conformed staging projection.
                with source as (

                    select * from {{{{ source('raw', '{full}') }}}}

                ), renamed as (

                    select
                        {table}_id                      as {table}_key,
                        {src}_code                      as {src}_reference,
                        lower(status)                   as status_code,
                        cast(amount as decimal(12,2))   as amount_usd,
                        event_date                      as event_date,
                        updated_at                      as extracted_at
                    from source
                    where status <> 'void'

                )

                select * from renamed
                """)
    return out


def clutter_schema_yml() -> dict[str, str]:
    """Six generic tests per staging model, emitted from the same spec.

    NOTE WHAT IS NOT HERE. No `accepted_values` and no `relationships` test
    anywhere touches a source system key on the billing chain. If one did, the
    shipped suite would catch an unknown vendor code on its own and the whole
    unit would measure nothing; `verify()` asserts the absence behaviourally by
    planting a novel code and requiring the SHIPPED suite to stay green.
    """
    out: dict[str, str] = {}
    values = ", ".join(f"'{s}'" for s in KEPT_STATUSES)
    for src, name, tables in SOURCES:
        body = [f"version: 2\n\n# {name}\nmodels:\n"]
        for table in tables:
            body.append(textwrap.indent(textwrap.dedent(f"""\
                - name: stg_{src}__{table}
                  description: Conformed {table} rows from {name}.
                  columns:
                    - name: {table}_key
                      data_tests:
                        - unique
                        - not_null
                    - name: status_code
                      data_tests:
                        - not_null
                        - accepted_values:
                            arguments:
                              values: [{values}]
                    - name: amount_usd
                      data_tests:
                        - not_null
                    - name: event_date
                      data_tests:
                        - not_null
                """), "  "))
        out[f"models/staging/{src}/_{src}__models.yml"] = "".join(body)
    return out


BILLING_STG_BROKEN = """\
-- Billing invoice lines, one row per invoice line.
--
-- The vendor stamps its own instance code in `vendor_system_code`. Downstream
-- joins want the CONFORMED source system key.
with source as (

    select * from {{ source('raw', 'billing_invoice_line') }}

), renamed as (

    select
        invoice_line_id                     as invoice_line_key,
        invoice_id                          as invoice_id,
        vendor_system_code                  as source_system_code,
        customer_id                         as customer_key,
        invoice_date                        as invoice_date,
        cast(amount_usd as decimal(12,2))   as amount_usd,
        currency_code                       as currency_code,
        line_status                         as line_status
    from source
    where line_status = 'posted'

)

select * from renamed
"""

BILLING_STG_FIXED = """\
-- Billing invoice lines, one row per invoice line.
--
-- The vendor stamps its own instance code in `vendor_system_code`. Downstream
-- joins want the CONFORMED source system key, so resolve it through the
-- crosswalk the same way stg_ar__payment does. A vendor code with no crosswalk
-- row is left as-is rather than dropped, so the conformance test can see it.
with source as (

    select * from {{ source('raw', 'billing_invoice_line') }}

), alias as (

    select * from {{ ref('stg_reference__source_system_alias') }}

), renamed as (

    select
        source.invoice_line_id                      as invoice_line_key,
        source.invoice_id                           as invoice_id,
        coalesce(alias.source_system_code,
                 source.vendor_system_code)         as source_system_code,
        source.customer_id                          as customer_key,
        source.invoice_date                         as invoice_date,
        cast(source.amount_usd as decimal(12,2))    as amount_usd,
        source.currency_code                        as currency_code,
        source.line_status                          as line_status
    from source
    left join alias
        on alias.vendor_code = source.vendor_system_code
    where source.line_status = 'posted'

)

select * from renamed
"""

CORE_MODELS: dict[str, str] = {
    "models/staging/reference/stg_reference__source_system.sql": """\
with source as (

    select * from {{ source('raw', 'source_system_registry') }}

)

select
    source_system_code  as source_system_code,
    system_name         as system_name,
    domain              as domain,
    status              as status,
    effective_from      as effective_from,
    retired_on          as retired_on
from source
""",
    "models/staging/reference/stg_reference__source_system_alias.sql": """\
-- The vendor-code crosswalk. One row per code an extract can stamp.
with source as (

    select * from {{ source('raw', 'source_system_alias') }}

)

select
    vendor_code         as vendor_code,
    source_system_code  as source_system_code,
    effective_from      as effective_from,
    note                as note
from source
""",
    "models/staging/billing/stg_ar__payment.sql": """\
-- AR payments. NOTE the crosswalk join: every staging model that carries a
-- vendor code resolves it here, so downstream facts only ever see conformed
-- source system keys.
with source as (

    select * from {{ source('raw', 'ar_payment') }}

), alias as (

    select * from {{ ref('stg_reference__source_system_alias') }}

)

select
    source.payment_id                           as payment_key,
    coalesce(alias.source_system_code,
             source.vendor_system_code)         as source_system_code,
    source.customer_id                          as customer_key,
    source.received_on                          as received_on,
    cast(source.amount_usd as decimal(12,2))    as amount_usd,
    source.method                               as method
from source
left join alias
    on alias.vendor_code = source.vendor_system_code
""",
    "models/staging/billing/stg_billing__credit_memo.sql": """\
-- Billing credit memos, one row per memo.
--
-- Same shape as the invoice line feed: the vendor stamps its own instance code
-- and this model passes it through. Every code this feed has stamped so far is
-- already a conformed one, so nothing downstream has ever noticed.
with source as (

    select * from {{ source('raw', 'billing_credit_memo') }}

), renamed as (

    select
        credit_memo_id                      as credit_memo_key,
        invoice_id                          as invoice_id,
        vendor_system_code                  as source_system_code,
        customer_id                         as customer_key,
        memo_date                           as memo_date,
        cast(amount_usd as decimal(12,2))   as amount_usd,
        currency_code                       as currency_code,
        memo_status                         as memo_status
    from source
    where memo_status = 'issued'

)

select * from renamed
""",
    "models/marts/fct_credit_memo.sql": """\
-- Credit memos conformed onto the source system dimension. Same inner join, and
-- therefore the same silent exposure, as fct_invoice_line.
with memos as (

    select * from {{ ref('stg_billing__credit_memo') }}

), systems as (

    select * from {{ ref('dim_source_system') }}

)

select
    memos.credit_memo_key       as credit_memo_key,
    memos.invoice_id            as invoice_id,
    memos.source_system_code    as source_system_code,
    systems.domain              as source_domain,
    memos.customer_key          as customer_key,
    memos.memo_date             as memo_date,
    memos.amount_usd            as amount_usd,
    memos.currency_code         as currency_code
from memos
join systems
    on systems.source_system_code = memos.source_system_code
""",
    "models/marts/dim_source_system.sql": """\
-- The conformed source system dimension. Carries retired systems too - history
-- has to join to something - so a key missing from here is a key that was never
-- conformed at all.
with registry as (

    select * from {{ ref('stg_reference__source_system') }}

)

select
    source_system_code                          as source_system_code,
    system_name                                 as system_name,
    domain                                      as domain,
    status                                      as status,
    effective_from                              as effective_from,
    retired_on                                  as retired_on,
    (status = 'active')                         as is_current
from registry
""",
    "models/marts/fct_invoice_line.sql": """\
-- Invoice lines conformed onto the source system dimension.
with lines as (

    select * from {{ ref('stg_billing__invoice_line') }}

), systems as (

    select * from {{ ref('dim_source_system') }}

)

select
    lines.invoice_line_key      as invoice_line_key,
    lines.invoice_id            as invoice_id,
    lines.source_system_code    as source_system_code,
    systems.domain              as source_domain,
    lines.customer_key          as customer_key,
    lines.invoice_date          as invoice_date,
    lines.amount_usd            as amount_usd,
    lines.currency_code         as currency_code
from lines
join systems
    on systems.source_system_code = lines.source_system_code
""",
    "models/marts/fct_revenue_daily.sql": """\
with lines as (

    select * from {{ ref('fct_invoice_line') }}

)

select
    invoice_date                                as invoice_date,
    source_system_code                          as source_system_code,
    count(*)                                    as invoice_lines,
    count(distinct invoice_id)                  as invoices,
    cast(sum(amount_usd) as decimal(16,2))      as revenue_usd
from lines
group by 1, 2
""",
    "models/marts/mart_revenue_by_month.sql": """\
with daily as (

    select * from {{ ref('fct_revenue_daily') }}

)

select
    strftime(invoice_date, '%Y-%m')             as revenue_month,
    sum(invoice_lines)                          as invoice_lines,
    cast(sum(revenue_usd) as decimal(16,2))     as revenue_usd
from daily
group by 1
""",
    "models/marts/fct_ar_payment.sql": """\
with payments as (

    select * from {{ ref('stg_ar__payment') }}

), systems as (

    select * from {{ ref('dim_source_system') }}

)

select
    payments.payment_key        as payment_key,
    payments.source_system_code as source_system_code,
    payments.customer_key       as customer_key,
    payments.received_on        as received_on,
    payments.amount_usd         as amount_usd,
    payments.method             as method
from payments
join systems
    on systems.source_system_code = payments.source_system_code
""",
}

LEGACY_MODELS: dict[str, str] = {
    "models/staging/legacy/stg_billing_v1__invoice.sql": """\
select
    invoice_no      as invoice_no,
    account_no      as account_no,
    issued_on       as issued_on,
    total_eur       as total_eur,
    state           as state
from {{ source('raw', 'billing_v1_invoice') }}
""",
    "models/staging/legacy/stg_billing_v1__account.sql": """\
select
    account_no      as account_no,
    account_name    as account_name,
    opened_on       as opened_on,
    region          as region
from {{ source('raw', 'billing_v1_account') }}
""",
    "models/staging/legacy/stg_billing_v1__credit_note.sql": """\
select
    note_no         as note_no,
    invoice_no      as invoice_no,
    issued_on       as issued_on,
    amount_eur      as amount_eur
from {{ source('raw', 'billing_v1_credit_note') }}
""",
    # Kept in its own file, deliberately: an agent who deletes `.dbtignore`
    # outright re-admits these models AND these tests, and the build must stay
    # green. A repair the rubric would punish is a rubric defect, not a repair.
    "models/staging/legacy/_billing_v1__models.yml": """\
version: 2

models:
  - name: stg_billing_v1__invoice
    description: Retired v1 billing extract. Kept until DATA-5107 deletes it.
    columns:
      - name: invoice_no
        data_tests: [unique, not_null]
  - name: stg_billing_v1__account
    columns:
      - name: account_no
        data_tests: [unique, not_null]
  - name: stg_billing_v1__credit_note
    columns:
      - name: note_no
        data_tests: [unique, not_null]
""",
}

# The two decoys the archetype calls for, plus the disabled one. All three are
# REAL - findable, dated, and wrong - and all three currently PASS, which is the
# property that makes them safe to fix. A decoy that fails when escalated would
# turn "do the sensible thing" into a scoring trap, and four separate rubric
# defects of exactly that shape were the reason this rule exists.
MARTS_YML = f"""\
version: 2

models:
  - name: dim_source_system
    description: Conformed source systems, including retired ones.
    columns:
      - name: source_system_code
        data_tests:
          - unique
          - not_null
      - name: domain
        data_tests:
          - not_null

  - name: fct_invoice_line
    description: Invoice lines conformed onto the source system dimension.
    columns:
      - name: invoice_line_key
        data_tests:
          - unique
          - not_null
      - name: amount_usd
        # INC-2291 (2026-02-03): downgraded during the currency incident so the
        # nightly build would stop paging. The incident closed 2026-02-05.
        data_tests:
          - not_null:
              config:
                severity: warn
      - name: invoice_date
        data_tests:
          - not_null:
              config:
                severity: warn

  - name: fct_revenue_daily
    description: Daily revenue by conformed source system.
    columns:
      - name: invoice_date
        data_tests:
          - not_null
      - name: revenue_usd
        data_tests:
          - not_null
          # Disabled for the 2025-11 backfill; re-enable once the restated
          # months are signed off.  -- {CUTOVER[:7]} note: still disabled.
          - dbt_utils_accepted_range:
              config:
                enabled: false

  - name: mart_revenue_by_month
    columns:
      - name: revenue_month
        data_tests:
          - unique
          - not_null

  - name: fct_ar_payment
    columns:
      - name: payment_key
        data_tests:
          - unique
          - not_null

  - name: stg_reference__source_system
    columns:
      - name: source_system_code
        data_tests:
          - unique
          - not_null

  - name: stg_reference__source_system_alias
    columns:
      - name: vendor_code
        data_tests:
          - unique
          - not_null
"""

# A local generic test, so `dbt_utils_accepted_range` above resolves without a
# package install (the world has no network).
ACCEPTED_RANGE_MACRO = """\
{% test dbt_utils_accepted_range(model, column_name, min_value=0, max_value=None) %}

select *
from {{ model }}
where {{ column_name }} < {{ min_value }}
{% if max_value is not none %}
   or {{ column_name }} > {{ max_value }}
{% endif %}

{% endtest %}
"""


def sources_yml() -> str:
    tables = ["source_system_registry", "source_system_alias", "billing_invoice_line",
              "billing_credit_memo", "billing_v1_invoice", "billing_v1_account",
              "billing_v1_credit_note", "ar_payment"]
    tables += [f"{src}_{t}" for src, _n, ts in SOURCES for t in ts]
    body = "".join(f"      - name: {t}\n" for t in sorted(tables))
    return SOURCES_YML_HEADER + body


def dbt_project_files(seed: int, *, fixed: bool) -> dict[str, str]:
    """Every file of the dbt project. `fixed=True` is the oracle's project."""
    files: dict[str, str] = {
        "dbt_project.yml": DBT_PROJECT_YML,
        ".dbtignore": DBTIGNORE,
        "models/_sources.yml": sources_yml(),
        "models/marts/_marts__models.yml": MARTS_YML,
        "models/staging/billing/_ar__models.yml": AR_YML,
        "models/staging/legacy/_billing__models.yml": BILLING_YML,
        "macros/accepted_range.sql": ACCEPTED_RANGE_MACRO,
        "models/staging/billing/stg_billing__invoice_line.sql":
            BILLING_STG_FIXED if fixed else BILLING_STG_BROKEN,
    }
    files.update(CORE_MODELS)
    files.update(LEGACY_MODELS)
    files.update(clutter_models(seed))
    files.update(clutter_schema_yml())
    if fixed:
        # The oracle's repair to the SUITE: narrow the ignore to the retired
        # models themselves, so the live model's schema file is parsed again.
        # Moving that file back to models/staging/billing/ is an equally correct
        # repair and the live-fire gate accepts either - it grades whether the
        # assertion RUNS, not which edit produced it. This is simply the edit the
        # reference warehouse happens to be built with.
        files[".dbtignore"] = ORACLE_DBTIGNORE
    return files


ORACLE_DBTIGNORE = DBTIGNORE.replace(
    "models/staging/legacy/\n",
    "models/staging/legacy/stg_billing_v1__*.sql\n"
    "models/staging/legacy/_billing_v1__models.yml\n")

# The two files the oracle rewrites, and the ONLY place their content is
# defined. `solution/solution.sh` writes the same bytes by hand so a human can
# read the repair; `run_baselines.py` refuses to print TASK VALID unless the two
# agree byte for byte, because a solution that has drifted from the reference is
# a suite grading a fix nobody can reproduce.
ORACLE_PATCH = {
    "dbt_project/models/staging/billing/stg_billing__invoice_line.sql": BILLING_STG_FIXED,
    "dbt_project/.dbtignore": ORACLE_DBTIGNORE,
}


def write_project(root: Path, seed: int, *, fixed: bool) -> None:
    for rel, body in sorted(dbt_project_files(seed, fixed=fixed).items()):
        _write_text(root / rel, body)


# ------------------------------------------------------------- running dbt
def dbt_bin() -> list[str]:
    return [sys.executable, "-m", "dbt.cli.main"]


def profiles_text(warehouse: Path) -> str:
    return (f"{PROJECT_NAME}:\n"
            f"  target: dev\n"
            f"  outputs:\n"
            f"    dev:\n"
            f"      type: duckdb\n"
            f"      path: \"{Path(warehouse).as_posix()}\"\n"
            f"      schema: warehouse\n"
            f"      threads: 1\n")


def run_dbt(project: Path, warehouse: Path, *, command: str = "build",
            profiles_dir: Path | None = None, target: Path | None = None,
            timeout: int = 900, extra: list[str] | None = None) -> dict:
    """One dbt invocation. Returns exit code and output; never raises on failure."""
    profiles_dir = Path(profiles_dir or project.parent / "_profiles")
    profiles_dir.mkdir(parents=True, exist_ok=True)
    _write_text(profiles_dir / "profiles.yml", profiles_text(warehouse))
    target = Path(target or project / "target")
    argv = [*dbt_bin(), "--no-use-colors", "--no-send-anonymous-usage-stats", command,
            "--project-dir", str(project), "--profiles-dir", str(profiles_dir),
            "--target-path", str(target), "--log-path", str(target / "logs"),
            "--threads", "1", *(extra or [])]
    env = {**os.environ, "DBT_SEND_ANONYMOUS_USAGE_STATS": "false", "PYTHONHASHSEED": "0"}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"returncode": 124, "output": f"timed out after {timeout}s",
                "timed_out": True}
    return {"returncode": proc.returncode,
            "output": (proc.stdout or "") + (proc.stderr or ""),
            "timed_out": False}


def parse_counts(output: str) -> dict:
    """The two numbers a reader of the build log actually has."""
    found, done = None, None
    for line in output.splitlines():
        text = line.split("  ", 1)[-1].strip()
        if text.startswith("Found ") and "data test" in text:
            found = text
        if text.startswith("Done. PASS="):
            done = text
    out = {"found": found, "done": done}
    if done:
        for part in done.replace("Done.", "").split():
            if "=" in part:
                key, _sep, value = part.partition("=")
                try:
                    out[key.lower()] = int(value)
                except ValueError:
                    pass
    if found:
        words = found.replace(",", " ").split()
        for i, word in enumerate(words):
            if word.isdigit() and i + 1 < len(words):
                if words[i + 1].startswith("model"):
                    out["models"] = int(word)
                if words[i + 1] == "data":
                    out["data_tests"] = int(word)
    return out


def manifest_tests(target: Path) -> dict:
    manifest = json.loads((Path(target) / "manifest.json").read_text(encoding="utf-8"))
    return {
        "nodes": sorted(k for k in manifest["nodes"] if k.startswith("test.")),
        "disabled": sorted(k for k in manifest.get("disabled", {}) if k.startswith("test.")),
        "models": sorted(k for k in manifest["nodes"] if k.startswith("model.")),
    }


# ------------------------------------------------------------- the workspace
CHANGELOG = f"""\
# Changelog

## 2026-03-30
- infra: pin duckdb to the 1.5 line in requirements.txt.

## 2026-02-05
- INC-2291 closed. Currency normalisation restored in the payment gateway feed.

## 2026-01-14 - RFC-118, EMEA billing cutover
- Billing EMEA moves to the new vendor instance. Registered the conformed
  system `{CONFORMED_CODE}` and added the vendor crosswalk row for the new
  instance code.
- Retired the v1 billing staging models. They are excluded from the parse
  (see dbt_project/.dbtignore) until DATA-5107 deletes the directory.

## 2025-12-02
- marts: `mart_revenue_by_month` added for the finance close pack.
"""

README = f"""\
# commercial

The commercial warehouse. One dbt project, one duckdb warehouse, one nightly
build.

    dbt build            # everything: models, then their tests

Layout

    dbt_project/models/staging/<source>/   one model per landed table
    dbt_project/models/marts/              dimensions and facts
    pipelines/                             the scheduled entrypoints
    docs/                                  ADRs, runbooks, the data dictionary

The nightly job `build_marts` runs `dbt build` over the whole project at 01:40
and has not failed since it was created. Scheduler state is in the platform,
not here.

Warehouse: `{{world_root}}/var/warehouse/warehouse.duckdb`, schemas `raw`,
`warehouse` and `ops`. Attach it, never open it directly:

    ATTACH '<path>' AS wh (READ_ONLY); USE wh.warehouse;
"""

ADR_CONFORMANCE = f"""\
# ADR-0006: conformed source system keys

Status: accepted (2024-03-18), amended 2026-01-14 by RFC-118

## Context

Every upstream extract stamps its own instance code. The 2024 consolidation
renamed most billing instances so that the code they stamp IS the conformed
code, which is convenient and misleading in equal measure: it means a model that
forgets to resolve the code is right most of the time. The exceptions are real -
the same EMEA estate has been `bill_eu1` and is now `{VENDOR_CODE}`; AR calls
itself `ar`; the GL calls itself `gl` - and a fact that carries one of those
directly cannot be joined to anything.

## Decision

1. `raw.source_system_registry` is the register of CONFORMED source system
   codes. Nothing else may mint one.
2. `raw.source_system_alias` is the crosswalk: one row per vendor code, mapping
   it to its conformed code.
3. **Every staging model that carries a vendor code resolves it through the
   crosswalk before anything downstream sees it.** `stg_ar__payment` is the
   reference implementation.
4. Facts join the conformed dimension `dim_source_system`. The join is an inner
   join: a key the dimension does not carry is, by construction, a row we
   cannot attribute.

## Consequence, and the control for it

Point 4 means an unresolved key does not raise - it disappears. The control is
the conformance test: every model that carries `source_system_code` carries a
`relationships` test to `dim_source_system`. Without it the failure mode is a
silent undercount, which is the worst kind of wrong number because every
downstream surface stays green.
"""

RUNBOOK = f"""\
# Runbook: nightly commercial build

Schedule: `build_marts` at 01:40 UTC, `refresh_close_extract` at 02:05 UTC.

## What it runs

    dbt build

over the whole project - every model, then every test attached to it. There is
no selector. A failing test fails the job.

## Reading the result

The scheduler stores the tail of the build. The two lines worth reading are

    Found <n> models, <m> data tests, <k> macros
    Done. PASS=... WARN=... ERROR=... SKIP=...

`WARN` is a test whose severity is `warn`: it is reported and the build still
succeeds. `ERROR` fails the job.

## If revenue looks wrong

1. `mart_revenue_by_month` is the close pack's source. Compare it against
   `raw.billing_invoice_line` directly - the fact tables are the thing under
   suspicion, so do not use them to check themselves.
2. `fct_invoice_line` joins `dim_source_system` on `source_system_code`. That
   join is inner (ADR-0006). Anything unattributed is gone, not flagged.
3. The crosswalk is `raw.source_system_alias`. A vendor code with no row there
   has never been conformed.
"""

DATA_DICTIONARY = f"""\
# Data dictionary (extract)

## raw.billing_invoice_line
| column | meaning |
| --- | --- |
| invoice_line_id | vendor line id, unique per instance |
| vendor_system_code | the VENDOR's instance code, e.g. `billing_amer_v2`, `{VENDOR_CODE}` |
| invoice_date | the date the line was invoiced |
| amount_usd | line amount, already converted |
| line_status | `posted` or `draft`; only `posted` is revenue |

## raw.source_system_alias
| column | meaning |
| --- | --- |
| vendor_code | what an extract stamps |
| source_system_code | the conformed code it resolves to |

## warehouse.dim_source_system
Conformed source systems, including retired ones. `is_current` marks the active
set. Built from `raw.source_system_registry`.

## warehouse.fct_revenue_daily
One row per (`invoice_date`, `source_system_code`). Revenue the close pack
reports. Built from `fct_invoice_line`.
"""

PIPELINE_BUILD = """\
#!/usr/bin/env bash
# The nightly commercial build, exactly as the scheduler runs it.
set -euo pipefail
cd "$(dirname "$0")/../dbt_project"
dbt build
"""

PIPELINE_EXTRACT = """\
#!/usr/bin/env bash
# Refresh the finance close extract from the marts.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "close extract refreshed from warehouse.mart_revenue_by_month"
"""

DASHBOARD_YML = f"""\
dashboard: {DASHBOARD_ID}
panels:
  - name: revenue_by_month
    sql: "SELECT revenue_month, invoice_lines, revenue_usd
      FROM warehouse.mart_revenue_by_month ORDER BY revenue_month"
  - name: revenue_by_system
    sql: "SELECT source_system_code, SUM(revenue_usd) AS revenue_usd
      FROM warehouse.fct_revenue_daily
      WHERE invoice_date >= DATE '2026-01-01'
      GROUP BY 1 ORDER BY 1"
"""


def write_workspace(out: Path, seed: int) -> None:
    ws = out / "workspace"
    if ws.exists():
        shutil.rmtree(ws)
    write_project(ws / "dbt_project", seed, fixed=False)
    _write_text(ws / "README.md", README)
    _write_text(ws / "CHANGELOG.md", CHANGELOG)
    _write_text(ws / "docs" / "adr" / "0006-conformed-source-systems.md", ADR_CONFORMANCE)
    _write_text(ws / "docs" / "runbook_nightly_build.md", RUNBOOK)
    _write_text(ws / "docs" / "data_dictionary.md", DATA_DICTIONARY)
    _write_text(ws / "pipelines" / "build_marts.sh", PIPELINE_BUILD)
    _write_text(ws / "pipelines" / "refresh_close_extract.sh", PIPELINE_EXTRACT)
    _write_text(ws / ".gitignore", "target/\ndbt_packages/\nlogs/\n*.duckdb\n")
    _write_text(out / "bi" / "dashboards" / f"{DASHBOARD_ID}.yml", DASHBOARD_YML)


# ---------------------------------------------------------------- measuring
def _digest(con, sql: str) -> str:
    """md5 over the sorted per-row md5s. A container hash would be a lie - a
    .duckdb file is not byte-reproducible - so identity is the CONTENT."""
    rows = con.execute(sql).fetchall()
    inner = sorted(hashlib.md5(
        "\x1f".join("" if v is None else str(v) for v in row).encode("utf-8")
    ).hexdigest() for row in rows)
    return hashlib.md5("\n".join(inner).encode("utf-8")).hexdigest()


ROW_SQL = {
    "fct_revenue_daily":
        "SELECT invoice_date, source_system_code, invoice_lines, invoices, revenue_usd "
        "FROM {a}.warehouse.fct_revenue_daily",
    "mart_revenue_by_month":
        "SELECT revenue_month, invoice_lines, revenue_usd "
        "FROM {a}.warehouse.mart_revenue_by_month",
    "fct_invoice_line":
        "SELECT invoice_line_key, invoice_id, source_system_code, source_domain, "
        "customer_key, invoice_date, amount_usd, currency_code "
        "FROM {a}.warehouse.fct_invoice_line",
}

# The extract's own identity. The reference warehouse is DERIVED from these
# three tables, so a cent moved in raw moves the right answer without moving any
# row count - and a contract that only counted rows would not notice. It is the
# one mutation the first falsification run got past, which is the reason this
# block exists rather than a reason to trust it.
RAW_SQL = {
    "billing_invoice_line":
        "SELECT invoice_line_id, invoice_id, vendor_system_code, customer_id, "
        "invoice_date, amount_usd, currency_code, line_status "
        "FROM {a}.raw.billing_invoice_line",
    "source_system_registry":
        "SELECT source_system_code, system_name, domain, status, effective_from, "
        "retired_on FROM {a}.raw.source_system_registry",
    "source_system_alias":
        "SELECT vendor_code, source_system_code, effective_from, note "
        "FROM {a}.raw.source_system_alias",
    "billing_credit_memo":
        "SELECT credit_memo_id, invoice_id, vendor_system_code, customer_id, "
        "memo_date, amount_usd, currency_code, memo_status "
        "FROM {a}.raw.billing_credit_memo",
    "ar_payment":
        "SELECT payment_id, vendor_system_code, customer_id, received_on, "
        "amount_usd, method FROM {a}.raw.ar_payment",
}


def measure(out: Path, counts: dict, manifest: dict) -> dict:
    """Read every number the contract publishes. Measured, never asserted."""
    con = _readonly(out / "warehouse.duckdb", "wh")
    con.execute(f"ATTACH '{(out / 'expected.duckdb').as_posix()}' AS ref (READ_ONLY)")

    def one(sql: str):
        return con.execute(sql).fetchone()

    shipped = float(one("SELECT COALESCE(SUM(revenue_usd),0) "
                        "FROM wh.warehouse.fct_revenue_daily")[0])
    correct = float(one("SELECT COALESCE(SUM(revenue_usd),0) "
                        "FROM ref.warehouse.fct_revenue_daily")[0])
    lost = one(
        "SELECT MIN(invoice_date), MAX(invoice_date), COUNT(*), "
        "       CAST(SUM(amount_usd) AS DECIMAL(16,2)) "
        "FROM wh.raw.billing_invoice_line "
        f"WHERE vendor_system_code = '{VENDOR_CODE}' AND line_status = 'posted'")
    world = {
        "raw_invoice_lines": one("SELECT COUNT(*) FROM wh.raw.billing_invoice_line")[0],
        "raw_posted_lines": one("SELECT COUNT(*) FROM wh.raw.billing_invoice_line "
                                "WHERE line_status = 'posted'")[0],
        "shipped_fct_invoice_line_rows":
            one("SELECT COUNT(*) FROM wh.warehouse.fct_invoice_line")[0],
        "expected_fct_invoice_line_rows":
            one("SELECT COUNT(*) FROM ref.warehouse.fct_invoice_line")[0],
        "shipped_revenue_usd": round(shipped, 2),
        "expected_revenue_usd": round(correct, 2),
        "shortfall_usd": round(correct - shipped, 2),
        "shortfall_pct": round(100.0 * (correct - shipped) / correct, 3),
        "unconformed_lines": int(lost[2]),
        "unconformed_amount_usd": float(lost[3]),
        "first_affected_date": lost[0].strftime("%Y-%m-%d"),
        "last_affected_date": lost[1].strftime("%Y-%m-%d"),
        "registry_rows": one("SELECT COUNT(*) FROM wh.raw.source_system_registry")[0],
        "alias_rows": one("SELECT COUNT(*) FROM wh.raw.source_system_alias")[0],
        "dim_source_system_rows":
            one("SELECT COUNT(*) FROM wh.warehouse.dim_source_system")[0],
        "job_runs": one("SELECT COUNT(*) FROM wh.ops.job_runs")[0],
        "job_failures": one("SELECT COUNT(*) FROM wh.ops.job_runs "
                            "WHERE status <> 'success'")[0],
        "vendor_code_in_dim": one(
            "SELECT COUNT(*) FROM wh.warehouse.dim_source_system "
            f"WHERE source_system_code = '{VENDOR_CODE}'")[0],
    }
    reference = {name: _digest(con, sql.format(a="ref")) for name, sql in ROW_SQL.items()}
    shipped_digests = {name: _digest(con, sql.format(a="wh"))
                       for name, sql in ROW_SQL.items()}
    raw_digests = {name: _digest(con, sql.format(a="wh"))
                   for name, sql in RAW_SQL.items()}
    per_day = {
        str(day): digest for day, digest in con.execute(
            "SELECT invoice_date, md5(string_agg(h, '\n' ORDER BY h)) FROM ("
            "  SELECT invoice_date, md5(concat_ws('\x1f', invoice_date, "
            "         source_system_code, invoice_lines, invoices, revenue_usd)) AS h"
            "  FROM ref.warehouse.fct_revenue_daily) GROUP BY 1 ORDER BY 1").fetchall()}
    con.close()
    return {
        "world": world,
        "reference_digest": reference,
        "shipped_digest": shipped_digests,
        "raw_digest": raw_digests,
        "reference_days": per_day,
        "suite": {
            "models": counts.get("models"),
            "data_tests": counts.get("data_tests"),
            "pass": counts.get("pass"),
            "warn": counts.get("warn"),
            "error": counts.get("error"),
            "found_line": counts.get("found"),
            "done_line": counts.get("done"),
            "manifest_test_nodes": len(manifest["nodes"]),
            "manifest_disabled_tests": len(manifest["disabled"]),
            "manifest_models": len(manifest["models"]),
        },
    }


# ------------------------------------------------------------------ builder
def generate(out: Path, seed: int) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    scratch = out / "_scratch"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    # Built under the DEPLOYED filename from the first statement (F622): the
    # duckdb catalog dbt infers from `warehouse.duckdb` is `warehouse`, which
    # collides with the `warehouse` schema. Generating against `world.duckdb`
    # and staging to `warehouse.duckdb` would test the collision-free case and
    # ship the colliding one - GT001 printed TASK VALID that way.
    build_raw(out / "warehouse.duckdb", seed)
    shutil.copy(out / "warehouse.duckdb", out / "expected.duckdb")

    write_workspace(out, seed)

    # The SHIPPED project, against the shipped warehouse. This is the build the
    # nightly job has been running: it must exit 0.
    shipped_project = out / "workspace" / "dbt_project"
    shipped = run_dbt(shipped_project, out / "warehouse.duckdb",
                      profiles_dir=scratch / "profiles_shipped",
                      target=scratch / "target_shipped")
    if shipped["returncode"] != 0:
        raise SystemExit("the SHIPPED project does not build green; the world's "
                         "premise is broken:\n" + shipped["output"][-4000:])
    counts = parse_counts(shipped["output"])
    manifest = manifest_tests(scratch / "target_shipped")
    build_ops_history(out / "warehouse.duckdb",
                      int(counts.get("total", counts.get("pass", 0))),
                      int(counts.get("warn", 0)))

    # The ORACLE project, against the reference warehouse. The reference is what
    # the repaired project produces - not a table written by hand - so the
    # oracle cannot disagree with it.
    oracle_project = scratch / "oracle" / "dbt_project"
    write_project(oracle_project, seed, fixed=True)
    fixed = run_dbt(oracle_project, out / "expected.duckdb",
                    profiles_dir=scratch / "profiles_oracle",
                    target=scratch / "target_oracle")
    if fixed["returncode"] != 0:
        raise SystemExit("the ORACLE project does not build green against the "
                         "reference warehouse:\n" + fixed["output"][-4000:])

    _write_text(out / "build_log_shipped.txt", shipped["output"])
    _write_text(out / "oracle_patch.json",
                json.dumps(ORACLE_PATCH, indent=2, sort_keys=True) + "\n")
    report = measure(out, counts, manifest)
    report["oracle_counts"] = parse_counts(fixed["output"])
    _write_text(out / "measured.json", json.dumps(report, indent=2, sort_keys=True) + "\n")
    shutil.rmtree(scratch, ignore_errors=True)
    return report


# ------------------------------------------- staging: world + grading zones
def scheduler_state(vnow: str = VNOW, *, done_line: str = "") -> dict:
    """var/sched/state.json - the schedule and the last runs, all green."""
    return {
        "vnow": vnow,
        "jobs": {
            "build_marts": {"command": "dbt_build", "duration_s": 96, "time": "01:40",
                            "exec": {"kind": "shell", "script": "pipelines/build_marts.sh",
                                     "detail": "dbt build"}},
            "refresh_close_extract": {
                "command": "refresh_close_extract", "duration_s": 22, "time": "02:05",
                "exec": {"kind": "shell", "script": "pipelines/refresh_close_extract.sh",
                         "detail": DASHBOARD_ID}},
        },
        "runs": [
            {"job": "build_marts", "vts_start": "2026-04-04T01:40:00Z",
             "vts_end": "2026-04-04T01:41:36Z", "status": "success", "detail": done_line},
            {"job": "refresh_close_extract", "vts_start": "2026-04-04T02:05:00Z",
             "vts_end": "2026-04-04T02:05:22Z", "status": "success", "detail": DASHBOARD_ID},
            {"job": "build_marts", "vts_start": "2026-04-05T01:40:00Z",
             "vts_end": "2026-04-05T01:41:38Z", "status": "success", "detail": done_line},
            {"job": "refresh_close_extract", "vts_start": "2026-04-05T02:05:00Z",
             "vts_end": "2026-04-05T02:05:21Z", "status": "success", "detail": DASHBOARD_ID},
            {"job": "build_marts", "vts_start": "2026-04-06T01:40:00Z",
             "vts_end": "2026-04-06T01:41:35Z", "status": "success", "detail": done_line},
            {"job": "refresh_close_extract", "vts_start": "2026-04-06T02:05:00Z",
             "vts_end": "2026-04-06T02:05:23Z", "status": "success", "detail": DASHBOARD_ID},
        ],
    }


def live_state() -> dict:
    return {"jobs": {
        "build_marts": {"exec": {"kind": "shell", "script": "pipelines/build_marts.sh",
                                 "detail": "dbt build"}},
        "refresh_close_extract": {
            "exec": {"kind": "shell", "script": "pipelines/refresh_close_extract.sh",
                     "detail": DASHBOARD_ID}},
    }}


def stage_world_root(build: Path, root: Path) -> dict:
    """Lay the built fixture out as a world root.

    The warehouse lands at var/warehouse/warehouse.duckdb - the CONTRACT
    filename, where the duckdb catalog name and the `warehouse` schema collide -
    so the agent's two-part references run under exactly the deployed condition
    (F622).
    """
    build, root = Path(build), Path(root)
    (root / "var" / "warehouse").mkdir(parents=True, exist_ok=True)
    (root / "var" / "sched").mkdir(parents=True, exist_ok=True)
    wh = root / "var" / "warehouse" / "warehouse.duckdb"
    shutil.copy(build / "warehouse.duckdb", wh)
    Path(str(wh) + ".wal").unlink(missing_ok=True)
    for src, dst in (("workspace", root / "workspace"), ("bi", root / "var" / "bi")):
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(build / src, dst)
    measured = json.loads((build / "measured.json").read_text(encoding="utf-8"))
    done = measured["suite"].get("done_line") or ""
    _write_text(root / "var" / "sched" / "state.json",
                json.dumps(scheduler_state(done_line=done), indent=2, sort_keys=True) + "\n")
    (root / "var" / "world").mkdir(parents=True, exist_ok=True)
    _write_text(root / "var" / "world" / "live.json",
                json.dumps(live_state(), indent=2, sort_keys=True) + "\n")
    out = {"world_root": str(root), "warehouse": str(wh),
           "workspace": str(root / "workspace"),
           "dashboards": str(root / "var" / "bi" / "dashboards"),
           "scheduler": str(root / "var" / "sched" / "state.json"),
           "virtual_time": VNOW}
    arm = arm_module()
    if arm is not None:
        # The context arm's own staging (task.toml [variants.<arm>]), applied
        # AFTER the base root and BEFORE surfaces and the grading zone, so the
        # collateral baseline and the pre-repair copy carry the arm's state.
        out["arm"] = arm.stage(root)
    return out


def arm_module():
    """The overlay's arm module, or None on the shipped (control) arm.

    `taskspec.stage_fixtures` copies an arm's overlay over a copy of this
    directory, so an overlay that ships `decision_arm.py` puts it BESIDE this
    file - and only there. In the package's own fixtures/ the file lives under
    variants/<arm>/ and is not importable from here, which is exactly the
    property that keeps `default` byte-identical to the shipped world.
    """
    import importlib.util

    path = HERE / "decision_arm.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("gt005_decision_arm", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_grading_zone(build: Path, world_root: Path, grading: Path) -> dict:
    """Everything the grader reads, in a directory the episode cannot address.

    F121: refuses a grading zone inside the world root rather than trusting the
    caller. An agent that can read expected.duckdb copies instead of diagnosing.
    """
    build, world_root, grading = Path(build), Path(world_root), Path(grading)
    if grading.resolve() == world_root.resolve() or \
            grading.resolve().is_relative_to(world_root.resolve()):
        raise SystemExit("F121: the grading zone must not live inside the world root "
                         f"({grading} is inside {world_root})")
    grading.mkdir(parents=True, exist_ok=True)
    shutil.copy(build / "expected.duckdb", grading / "expected.duckdb")
    shutil.copy(build / "measured.json", grading / "measured.json")
    baseline = grading / "pre_repair_world"
    if baseline.exists():
        shutil.rmtree(baseline)
    shutil.copytree(world_root, baseline)
    _write_text(grading / "world_sections.json",
                json.dumps(snapshot_sections(world_root), indent=2, sort_keys=True) + "\n")
    return {"grading": str(grading),
            "expected": str(grading / "expected.duckdb"),
            "pre_repair_world": str(baseline)}


def snapshot_sections(world_root: Path) -> dict:
    """The collateral baseline: a content digest per world section.

    Deliberately coarse. The agent OWNS the workspace and is asked to change it,
    so the workspace is not fingerprinted here at all - billing an agent for
    editing the repo it was told to repair is the rubric defect this file exists
    to avoid. What is fingerprinted is everything it was NOT asked to touch.
    """
    root = Path(world_root)
    sections = {}
    for name, rel in (("scheduler", "var/sched/state.json"),
                      ("bi", "var/bi"),
                      ("obs", "var/obs"),
                      ("workflow", "var/workflow"),
                      ("live", "var/world/live.json")):
        path = root / rel
        if not path.exists():
            sections[name] = None
        elif path.is_file():
            sections[name] = hashlib.md5(path.read_bytes()).hexdigest()
        else:
            digest = hashlib.md5()
            for child in sorted(p for p in path.rglob("*") if p.is_file()):
                digest.update(child.relative_to(path).as_posix().encode("utf-8"))
                digest.update(child.read_bytes())
            sections[name] = digest.hexdigest()
    return {"sections": sections}


# --------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="make_fixture_world")
    ap.add_argument("--out", default=str(HERE / "build"))
    ap.add_argument("--seed", type=int, default=SEED)
    a = ap.parse_args(argv)
    report = generate(Path(a.out).resolve(), a.seed)
    print(json.dumps({"world": report["world"], "suite": report["suite"]},
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
