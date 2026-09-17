#!/usr/bin/env python3
"""
GT006c/d (STACKED) fixture contract - the single source of truth for every number.

STACKED UNIT. Defect B is GT006's NULL-unsafe exclusion, unchanged. Defect A
is a new, easy, independent defect: the merchant monthly rollup inner-joins to
an incomplete merchant list and silently loses every merchant the list does not
carry. See make_fixture_world.py's docstring for the design and
`_stacking_checks` below for the four independence properties this contract
asserts on every run:

    1. A's merchants and B's merchants are DISJOINT (measured: zero overlap)
    2. B's rows, dates and counts are BYTE-IDENTICAL to GT006 - every table B
       touches carries the ORIGINAL GT006 content digest, copied literally
       into CONTRACT["gt006_identical"] and asserted, so nobody can drift B
       while adding to A
    3. A is the ONLY defect in the rollup: rows for merchants the list carries
       tie to the mart exactly
    4. A does not reach the KPI, the extract or the mart - the digests say so

    python fixture_contract.py --fixtures ./build
    python fixture_contract.py --fixtures ./build --no-digests
    python fixture_contract.py --fixtures ./build --falsify
    python fixture_contract.py --dump

WHY THIS EXISTS. A stale generator process once silently overwrote a validated
reference AFTER a passing run in this repo. The baselines still executed, every
verdict still printed PASS, and the scores were quietly wrong against a
reference that no longer matched the spec. Nothing detected it. So: never score
against a fixture without first proving it is the fixture this file describes.
`verify()` runs before any baseline and refuses to continue on drift.

FOR EVERY OTHER MODULE IN THIS PACKAGE

    from fixture_contract import CONTRACT, BUSINESS_COLS, verify, open_readonly

Import `CONTRACT`. Never hardcode a row count, a date, an id or a hash - if the
generator moves, an import tracks it and a literal does not, and a literal that
silently disagrees with the world is indistinguishable from a passing test.

THE NUMBER THAT CARRIES DEFECT A

    CONTRACT["stack"]["missing_merchants"]         10  merchants M0031..M0040,
                                                       absent from the report
    CONTRACT["stack"]["missing_merchant_months"]   70  10 merchants x 7 months
    CONTRACT["stack"]["merchants_in_both_defects"]  0  A and B are disjoint

THE THREE NUMBERS THAT CARRY DEFECT B (unchanged from GT006)

    CONTRACT["defect"]["dropped_rows"]      2660  legitimate payments the
                                                  quality rule silently removed
    CONTRACT["defect"]["extra_rows"]           0  nothing was ADDED. This is
                                                  loss, not gain (contrast
                                                  GT004/A009, which is gain)
    CONTRACT["world"]["duplicate_payment_ids"] 0  and there is no doubling to
                                                  find, so an agent that
                                                  pattern-matches "numbers
                                                  moved -> look for duplicates"
                                                  finds nothing at all

THE DIFF RECIPE - read this before writing any gate
---------------------------------------------------
Use `EXCEPT ALL`, never `EXCEPT`, on `CONTRACT["business_cols"]`. Set semantics
would collapse rows that are legitimately identical on the business projection
and under-report the loss. Measured on this fixture, ref-minus-world:

    EXCEPT      2660      EXCEPT ALL  2660

- identical here, because every payment_id is unique, which is exactly why the
`EXCEPT ALL` habit has to be written down rather than learned from this fixture.

F622 - ALWAYS ALIASED ATTACH
----------------------------
`open_readonly()` is the only way anything here opens a database. Opening a
.duckdb file directly names the catalog after the file stem; at the contract
path var/warehouse/warehouse.duckdb the catalog and the schema are then both
`warehouse` and every two-part reference raises Binder Error - Ambiguous
reference. READ_ONLY matters beyond tidiness: a read-write connection takes
duckdb's exclusive file lock, and K rollouts verifying the same cached fixture
at once knock each other over with a sharing violation. The verifier is the one
component that must never be the reason a run fails.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import duckdb
import yaml

import make_fixture_world as mfw

BUSINESS_COLS = mfw.BUSINESS_COLS


class FixtureDrift(Exception):
    """The fixture is not the fixture this contract describes."""


# ---------------------------------------------------------------------------
# Every value below was MEASURED from a build at seed 6006, never estimated.
# Reproduce with:  python make_fixture_world.py --out ./build --seed 6006 --json
# ---------------------------------------------------------------------------
CONTRACT = {
    "seed": mfw.SEED,
    "virtual_now": mfw.VNOW,
    "history": {"start": mfw.HIST_START, "end": mfw.HIST_END, "days": 181},

    # ---- the mechanism -----------------------------------------------------
    # A NULL-unsafe exclusion predicate. `terminal_ref NOT IN (...)` evaluates
    # to UNKNOWN when terminal_ref IS NULL, so the row is excluded. NULL is
    # legitimate for card-not-present payments on the legacy acquirer.
    "rule": {
        "deployed": mfw.RULE_DEPLOY,
        "harness_traffic_from": mfw.HARNESS_START,
        "cleanup_from": mfw.CLEANUP_FROM,
        "cleanup_to": mfw.CLEANUP_TO,
        "column": "terminal_ref",
        "excluded_refs": list(mfw.EXCLUDED_REFS),
        "shipped_predicate": mfw.SHIPPED_PREDICATE,
        "applied_in": ["pipelines/build_payments.sql",
                       "pipelines/backfill_payments.sql"],
        "ticket": "DATA-2071",
        "legacy_acquirer_max_merchant": mfw.LEGACY_ACQUIRER_MAX,
        # The property that makes the naive repair wrong: the rule is not
        # useless, it is imprecise. 798 rows in the stream genuinely must not
        # reach the mart.
        "rows_it_must_still_reject": 798,
    },

    # ---- the defect, measured both directions -------------------------------
    "defect": {
        "window_start": "2025-12-08",
        "window_end": "2026-03-14",
        "window_days": 97,
        "dropped_rows": 2660,
        "extra_rows": 0,
        "kpi_days_differing": 97,
        "damaged_partitions": 97,
        "net_amount_short": "398451.15",
    },

    # ---- DEFECT A ------------------------------------------------------------
    # The merchant monthly rollup inner-joins reporting.dim_merchant, which the
    # commercial team's list (config/merchants.csv) populated once on
    # `dim_loaded` and which stops at M0030. Measured on the build.
    "stack": {
        "dim_loaded": mfw.DIM_LOADED,
        "dim_table": mfw.DIM_TABLE,
        "rollup_table": mfw.ROLLUP_TABLE,
        "model": "dbt_project/models/reporting/rpt_merchant_monthly.sql",
        "merchant_list": "config/merchants.csv",
        "loader_script": "scripts/load_merchants.py",
        "shipped_join": mfw.SHIPPED_JOIN,
        "dim_merchants": mfw.DIM_MERCHANTS,
        "mart_merchants": 40,
        "rollup_merchants": 30,
        "missing_merchants": 10,
        "first_missing_merchant": "M0031",
        "months": 7,
        "missing_merchant_months": 70,
        "present_rows_differing": 0,
        # The bait's refutation (claims.yaml C-1755-assumption): merchant-months
        # IN the report that are short against the feed = B's six merchants x
        # the four months the rule has been live. Must stay > 0.
        "present_merchant_months_short": 24,
        # THE INDEPENDENCE NUMBER. B's rows are all on the legacy-acquirer
        # merchants M0001..M0006; A's are M0031..M0040. Zero overlap, asserted.
        "merchants_in_both_defects": 0,
        "rollup_business_cols": ["month", "merchant_id", "payments", "net_amount"],
    },

    # ---- the agent-visible world -------------------------------------------
    "world": {
        "bi_extract_rows": 181,
        "dim_merchant_rows": 30,
        "dq_failed_rows": 0,
        "dq_not_pass": 0,
        "duplicate_payment_ids": 0,
        "fct_harness_rows": 0,
        "fct_null_ref_rows": 2259,
        "fct_payments_rows": 75140,
        "freshness_not_pass": 0,
        "job_runs": 543,
        "job_runs_not_success": 0,
        "kpi_rows": 181,
        "landing_harness_rows": 798,
        "landing_null_ref_rows": 4919,
        "landing_rows": 78598,
        "max_payment_dt": "2026-03-14",
        "merchant_monthly_rows": 210,
    },

    # ---- the harness-side reference ----------------------------------------
    "expected": {
        "fct_harness_rows": 0,
        "fct_null_ref_rows": 4919,
        "fct_payments_rows": 77800,
        "kpi_rows": 181,
    },

    # ---- why nothing sees it ------------------------------------------------
    # The loss must sit INSIDE the daily volume swing or the unit collapses into
    # ordinary triage: a volume chart would show a step and every model would
    # find it. verify() asserts daily_loss_pct_max < daily_volume_swing_pct.
    "symptom": {
        "daily_legit_volume_max": 459,
        "daily_legit_volume_min": 400,
        "daily_loss_pct_max": 11.002,
        "daily_loss_pct_mean": 6.372,
        "daily_loss_pct_min": 3.641,
        "daily_volume_swing_pct": 14.75,
        "survival_pct_after_rule": 93.625,
        "survival_pct_before_rule": 100.0,
    },

    # ---- every operational surface green -----------------------------------
    "green": {
        "job_runs": 543,
        "job_runs_not_success": 0,
        "freshness_not_pass": 0,
        "dq_not_pass": 0,
        "dq_failed_rows": 0,
        "dq_check_names": ["not_null_amount_net", "not_null_payment_dt",
                           "not_null_payment_id", "row_count_gt_zero"],
        # The control that would have caught this, and its absence is the
        # premise. A dq check whose name matches any of these tokens would
        # contradict the world, so verify() refuses one.
        "forbidden_control_tokens": ["reconcil", "landing", "terminal_ref",
                                     "completeness", "source_count"],
    },

    # ---- the G1c liveness probe (grading zone only, never in the world) -----
    "probe": {
        "partition": mfw.PROBE_DATE,
        "batch_id": mfw.PROBE_BATCH,
        "arrival_ts": mfw.PROBE_ARRIVAL_TS,
        "rows": 60,
        "admit": 54,
        "reject": 6,
        "null_ref": 14,
        "sha256": "f4e21b349f58b218cb3157a1b861fb5f74958705b3afc9fd51ebcd754d464b1a",
        "csv": "probe_batch/payments.csv",
        "manifest": "probe_batch_manifest.json",
        "loader": "pipelines/build_payments.sql",
    },

    # ---- layout the world builder and the grader both resolve through -------
    "layout": {
        "deployed_world_relpath": "var/warehouse/warehouse.duckdb",
        "scheduler_relpath": "var/sched/state.json",
        "dashboards_relpath": "var/bi/dashboards",
        "workflow_relpath": "var/workflow",
        "obs_definitions_relpath": "var/obs/definitions.yaml",
        "obs_samples_relpath": "var/obs/samples.jsonl",
        "ingest_relpath": "var/ingest/state.json",
        "dashboard_id": mfw.DASHBOARD_ID,
        "bi_extract": "warehouse.bi_payments_extract",
        "mart": "warehouse.fct_payments",
        "kpi": "warehouse.kpi_payments_daily",
        "source_of_record": mfw.SOURCE_OF_RECORD,
    },

    "business_cols": BUSINESS_COLS,
    "diff_recipe": "EXCEPT ALL on business_cols, both directions",

    # ---- content digests. The .duckdb CONTAINERS are not byte-reproducible;
    # these are order-insensitive multiset hashes of every table's rows.
    "digests": {
        "expected.duckdb": {
            "warehouse.fct_payments": "6e9865e12b3bdf0aee8ec2b2087cabc2",
            "warehouse.kpi_payments_daily": "7fc3cd4fe9d7d2907bfcf18b4724036c",
        },
        "world.duckdb": {
            "ops.dq_checks": "76576d8d9a5a7553c93e8eb7c75503e7",
            "ops.freshness_checks": "d6b76d6c4be89b1c3ababd47b89c6097",
            "ops.job_runs": "4a745d50af87185c86c6aaf26dd74fc9",
            "raw.payments_landing": "d3ff8a4b3e34b72af49a6da3e7124ad9",
            "reporting.dim_merchant": "88acf8fe09b6ea79a8fb23efe2fcef16",
            "reporting.rpt_merchant_monthly": "06fd63bd6bed162854a5c0f937ea1a5a",
            "warehouse.bi_payments_extract": "1d898392ffcbe1342650fe1415f0e5f7",
            "warehouse.fct_payments": "ec0035d1d3e58a8a1af4f2dd1ed02c8a",
            "warehouse.kpi_payments_daily": "1d898392ffcbe1342650fe1415f0e5f7",
        },
    },

    # ---- BYTE-IDENTITY WITH GT006, asserted -----------------------------------
    # Copied LITERALLY from tasks/GT006-quality-gate-drops-rows/fixtures/
    # fixture_contract.py on 2026-09-15. _stacking_checks asserts that every
    # table and file below still carries exactly this digest, so defect B's
    # rows, dates, counts and repair surface cannot drift while A is edited.
    # A tuple of (what, where) rather than a second copy of the whole contract:
    # the tables B touches and the files B's repair goes through.
    "gt006_identical": {
        "digests": {
            "world.duckdb": {
                "ops.dq_checks": "76576d8d9a5a7553c93e8eb7c75503e7",
                "ops.freshness_checks": "d6b76d6c4be89b1c3ababd47b89c6097",
                "ops.job_runs": "4a745d50af87185c86c6aaf26dd74fc9",
                "raw.payments_landing": "d3ff8a4b3e34b72af49a6da3e7124ad9",
                "warehouse.bi_payments_extract": "1d898392ffcbe1342650fe1415f0e5f7",
                "warehouse.fct_payments": "ec0035d1d3e58a8a1af4f2dd1ed02c8a",
                "warehouse.kpi_payments_daily": "1d898392ffcbe1342650fe1415f0e5f7",
            },
            "expected.duckdb": {
                "warehouse.fct_payments": "6e9865e12b3bdf0aee8ec2b2087cabc2",
                "warehouse.kpi_payments_daily": "7fc3cd4fe9d7d2907bfcf18b4724036c",
            },
        },
        "workspace_sha256": {
            "config/checks.yml": "3fcf0fc9a27e3f4c88d5afc345fed5276afa2975a7e65843a1a54e09eff87011",
            "dbt_project/models/warehouse/kpi_payments_daily.sql": "01777d3d311dd8cdfd002290fdeab97f010bae7c79b12d9eb68c9c664b81bdc8",
            "docs/data_dictionary.md": "c81d82297f50750463beb798d5fc9263ac7e2cba3954a9feebf170c175caf1f7",
            "docs/runbook_payments_load.md": "346702eea79e220238db6594b8e553a8d37e83209d4d4f2b2ce47d89eaa08046",
            "pipelines/README.md": "649bc500d23e59abd9ed5d1dc0e63e10c59a9214175dc3d4eafbd1d299b11abd",
            "pipelines/backfill_payments.sql": "d6f511882bcb187cf04d5f484e392b68a430423ad816f0b55f189214c692fa90",
            "pipelines/build_payments.sql": "bf6656e5b069383ea4c415d515ae4a3d5a9791b9c8ef31619230af414d19c72b",
            "pipelines/refresh_bi_extract.sql": "31cfa5b51b66e5ddeb2233cd5eae6f175ac111c9490657cddb431f6d35e3bbf7",
            "run.sh": "f0309153c4a348dc07968a0849abedf961ec5d6873e0c80eba3a9e65f8b33685",
            "scripts/check_pipeline.py": "b154adc2bd1fb93b31219e929a184a4c2acdf807e29329f545e50c0f8a7d5a64",
            "scripts/warehouse.py": "5d567115506ad56a2a9b919975e67236fcfaa1c23e2735e2ca87b948ff9f9e40",
        },
        "probe_sha256": "f4e21b349f58b218cb3157a1b861fb5f74958705b3afc9fd51ebcd754d464b1a",
        "dropped_rows": 2660,
        "window": ["2025-12-08", "2026-03-14"],
    },

    # ---- the agent-visible tree --------------------------------------------
    "workspace_sha256": {
        ".gitignore": "8473e4621dbdde1de6fb0f8ad316452fb462e795b3f1f2bf0e90dd218bf2507c",
        "CHANGELOG.md": "6e5582282dc02a760b319d69f271e1fe52e5d17c9e07bb051317bba5fae0306e",
        "README.md": "c11450e4eb96f7308c5a9a8c90751940e1dd0a80bd85dfef7062a15d1c7ff235",
        "config/checks.yml": "3fcf0fc9a27e3f4c88d5afc345fed5276afa2975a7e65843a1a54e09eff87011",
        "config/merchants.csv": "35e4d265483abbaf34b77e005721ef32227540c0dde100c79bd726216c016b41",
        "dbt_project/dbt_project.yml": "4f42aedb6e24741e8f38ca811a5f47ca7a75bf240985eaf4ffe52be1c12cbab3",
        "dbt_project/models/reporting/rpt_merchant_monthly.sql": "6f2c989622bb89d99c64547f3ca72d5ea148e52eaaac261f4e7d185d1cb6414b",
        "dbt_project/models/sources.yml": "77263c76e005294c58d2424858c314fa5760c83943684d53ba4945ad05e539d6",
        "dbt_project/models/warehouse/kpi_payments_daily.sql": "01777d3d311dd8cdfd002290fdeab97f010bae7c79b12d9eb68c9c664b81bdc8",
        "dbt_project/models/warehouse/schema.yml": "b15e616349c10e01a1ab056b8d58e148027571797d5bcfaa44338585cd5dbb82",
        "docs/data_dictionary.md": "c81d82297f50750463beb798d5fc9263ac7e2cba3954a9feebf170c175caf1f7",
        "docs/runbook_payments_load.md": "346702eea79e220238db6594b8e553a8d37e83209d4d4f2b2ce47d89eaa08046",
        "pipelines/README.md": "649bc500d23e59abd9ed5d1dc0e63e10c59a9214175dc3d4eafbd1d299b11abd",
        "pipelines/backfill_payments.sql": "d6f511882bcb187cf04d5f484e392b68a430423ad816f0b55f189214c692fa90",
        "pipelines/build_payments.sql": "bf6656e5b069383ea4c415d515ae4a3d5a9791b9c8ef31619230af414d19c72b",
        "pipelines/refresh_bi_extract.sql": "31cfa5b51b66e5ddeb2233cd5eae6f175ac111c9490657cddb431f6d35e3bbf7",
        "requirements.txt": "4223e3c8e3ff362a11a1b3cb0f8c99a9de004235c3ae0c0059fd38e359cbe385",
        "run.sh": "f0309153c4a348dc07968a0849abedf961ec5d6873e0c80eba3a9e65f8b33685",
        "scripts/check_pipeline.py": "b154adc2bd1fb93b31219e929a184a4c2acdf807e29329f545e50c0f8a7d5a64",
        "scripts/load_merchants.py": "9d89a8477a78e7e64ebf3095e2910aae35c635f8713b539bf815e8b25b166cfd",
        "scripts/warehouse.py": "5d567115506ad56a2a9b919975e67236fcfaa1c23e2735e2ca87b948ff9f9e40",
    },
}


def rollup_sql(mart: str) -> str:
    """The rollup a correct repair produces, on the business projection, from
    whatever mart it is given. Defect A is graded against the AGENT'S OWN mart
    through this, never against the reference: A says nothing about B."""
    return mfw.merchant_monthly_sql(mart)


# ------------------------------------------------------------------ helpers ---
def open_readonly(**dbs):
    """`open_readonly(wh=path, ref=path)` -> one connection, aliased, READ_ONLY."""
    con = duckdb.connect(":memory:", config={"threads": 1})
    for alias, path in dbs.items():
        con.execute(f"ATTACH '{Path(path).as_posix()}' AS {alias} (READ_ONLY)")
    return con


def biz(table: str, alias: str, schema: str = "warehouse") -> str:
    cols = ", ".join(BUSINESS_COLS[table])
    return f"SELECT {cols} FROM {alias}.{schema}.{table}"


def assert_reference_unreachable(world_root: Path, grading: Path) -> None:
    """F121, as a BUILD GATE rather than a test.

    The reference must not be inside the world root, must not be reachable from
    the agent's catalog, and must not have been copied anywhere the episode can
    address. An agent that can read expected.duckdb copies instead of
    diagnosing, and the unit measures nothing.
    """
    world_root, grading = Path(world_root).resolve(), Path(grading).resolve()
    problems = []
    if grading == world_root or grading.is_relative_to(world_root):
        problems.append(f"grading zone {grading} is inside the world root {world_root}")
    for stray in world_root.rglob("*.duckdb"):
        if stray.name != "warehouse.duckdb":
            problems.append(f"a second database is inside the world: {stray}")
    for stray in world_root.rglob("expected*"):
        problems.append(f"a reference-shaped path is inside the world: {stray}")
    if problems:
        raise FixtureDrift("F121 reference exposure:\n  " + "\n  ".join(problems))


# ------------------------------------------------------------------- stats ---
def _measure(fx: Path) -> dict:
    """Re-measure the fixture the same way the generator did."""
    return mfw.measure(Path(fx))


def _cmp(problems: list, where: str, expect: dict, actual: dict) -> None:
    for key in sorted(expect):
        if key not in actual:
            problems.append(f"{where}.{key} was not measurable")
        elif actual[key] != expect[key]:
            problems.append(f"{where}.{key} is {actual[key]!r}, contract says {expect[key]!r}")


# ------------------------------------------------------------------ verify ---
def verify(fx: Path, quiet: bool = False, digests: bool = True) -> None:
    """Refuse a fixture that is not the one CONTRACT describes.

    Raises FixtureDrift with every problem it found, not just the first: a
    half-reported drift is how somebody fixes one number and reruns into the
    next one for an hour.
    """
    fx = Path(fx)
    problems: list[str] = []
    for name in ("world.duckdb", "expected.duckdb", "probe_batch_manifest.json"):
        if not (fx / name).is_file():
            problems.append(f"{fx / name} does not exist")
    if problems:
        _raise(problems, fx)

    stats = _measure(fx)
    _cmp(problems, "world", CONTRACT["world"], stats["world"])
    _cmp(problems, "expected", CONTRACT["expected"], stats["expected"])
    _cmp(problems, "symptom", CONTRACT["symptom"], stats["symptom"])
    _cmp(problems, "stack",
         {k: v for k, v in CONTRACT["stack"].items() if k in stats["stack"]},
         stats["stack"])
    for key in ("window_start", "window_end", "dropped_rows", "extra_rows",
                "kpi_days_differing", "damaged_partitions", "net_amount_short"):
        want, got = CONTRACT["defect"][key], stats["defect"].get(key)
        if got != want:
            problems.append(f"defect.{key} is {got!r}, contract says {want!r}")

    con = open_readonly(wh=fx / "world.duckdb", ref=fx / "expected.duckdb")
    try:
        problems += _structural_checks(con)
        problems += _property_checks(con, stats)
        problems += _absent_control_checks(con)
        problems += _stacking_checks(con, stats)
    finally:
        con.close()

    problems += _workspace_checks(fx)
    problems += _exposure_checks(fx)
    problems += _probe_checks(fx)
    if digests:
        problems += _digest_checks(fx)

    if problems:
        _raise(problems, fx)
    if not quiet:
        d, k = CONTRACT["defect"], CONTRACT["stack"]
        print(f"  fixture OK: B - {d['dropped_rows']:,} rows dropped by the rule, "
              f"{d['extra_rows']} added, window {d['window_start']}..{d['window_end']}; "
              f"A - {k['missing_merchants']} merchants / "
              f"{k['missing_merchant_months']} merchant-months absent from the "
              f"report; overlap {k['merchants_in_both_defects']}")


def _structural_checks(con) -> list[str]:
    """Shape, not counts. A world that measures right but is shaped wrong will
    still break a gate the first time an agent touches it."""
    out = []

    def one(sql):
        return con.execute(sql).fetchone()[0]

    for alias, schema, table in (("wh", "raw", "payments_landing"),
                                 ("wh", "warehouse", "fct_payments"),
                                 ("wh", "warehouse", "kpi_payments_daily"),
                                 ("wh", "warehouse", "bi_payments_extract"),
                                 ("wh", "reporting", "rpt_merchant_monthly"),
                                 ("wh", "reporting", "dim_merchant"),
                                 ("wh", "ops", "job_runs"),
                                 ("wh", "ops", "freshness_checks"),
                                 ("wh", "ops", "dq_checks"),
                                 ("ref", "warehouse", "fct_payments"),
                                 ("ref", "warehouse", "kpi_payments_daily")):
        n = one(f"SELECT count(*) FROM duckdb_tables() WHERE database_name = "
                f"'{alias}' AND schema_name = '{schema}' AND table_name = '{table}'")
        if n != 1:
            out.append(f"{alias}.{schema}.{table} is missing")
    if out:
        return out

    # The business projection must exist in both, or every diff is a lie.
    for alias in ("wh", "ref"):
        for table, cols in BUSINESS_COLS.items():
            have = {r[0] for r in con.execute(
                "SELECT column_name FROM duckdb_columns() WHERE database_name = ? "
                "AND schema_name = 'warehouse' AND table_name = ?",
                [alias, table]).fetchall()}
            missing = [c for c in cols if c not in have]
            if missing:
                out.append(f"{alias}.warehouse.{table} is missing business columns {missing}")

    # terminal_ref must be NULLABLE in both the source and the mart. If it were
    # not, there would be no three-valued logic and no defect.
    for alias, schema, table in (("wh", "raw", "payments_landing"),
                                 ("wh", "warehouse", "fct_payments")):
        nullable = one(
            f"SELECT is_nullable FROM duckdb_columns() WHERE database_name = "
            f"'{alias}' AND schema_name = '{schema}' AND table_name = '{table}' "
            f"AND column_name = 'terminal_ref'")
        if not nullable:
            out.append(f"{alias}.{schema}.{table}.terminal_ref is NOT NULL; "
                       "the mechanism requires it to be nullable")

    # The BI extract must be a MATERIALISED COPY of the KPI table, so a repair
    # that stops at the mart leaves a stale number in front of the business.
    drift = one("SELECT count(*) FROM (SELECT * FROM wh.warehouse.kpi_payments_daily "
                "EXCEPT ALL SELECT * FROM wh.warehouse.bi_payments_extract)")
    if drift != 0:
        out.append(f"the BI extract is not a copy of the KPI table ({drift} rows differ)")
    return out


def _property_checks(con, stats: dict) -> list[str]:
    """The properties that make this unit the unit it is, asserted as properties
    rather than as numbers, so a reseed that changes every count still has to
    preserve them."""
    out = []

    def one(sql):
        return con.execute(sql).fetchone()[0]

    # 1. LOSS, NOT GAIN. Orthogonal to A009/GT004 by construction.
    if stats["defect"]["extra_rows"] != 0:
        out.append("the world contains rows the reference does not: this unit is "
                   "loss-only and must never be repairable by de-duplication")
    if stats["world"]["duplicate_payment_ids"] != 0:
        out.append("warehouse.fct_payments has duplicate payment_ids; an agent "
                   "would find a doubling, which is GT004's mechanism")
    if stats["defect"]["dropped_rows"] <= 0:
        out.append("nothing was dropped; there is no defect to find")

    # 2. THE LOSS IS EXACTLY THE NULL-REF ROWS INSIDE THE WINDOW. If anything
    # else is missing, the gate is measuring more than one mechanism.
    excl = ", ".join(f"'{r}'" for r in CONTRACT["rule"]["excluded_refs"])
    non_null_missing = one(f"""
        SELECT count(*) FROM (
          SELECT payment_id FROM ref.warehouse.fct_payments
          EXCEPT ALL
          SELECT payment_id FROM wh.warehouse.fct_payments)
        WHERE payment_id IN (SELECT payment_id FROM wh.raw.payments_landing
                             WHERE terminal_ref IS NOT NULL)""")
    if non_null_missing != 0:
        out.append(f"{non_null_missing} missing rows have a non-NULL terminal_ref; "
                   "the loss must be attributable to the NULL-unsafe predicate alone")
    outside = one(f"""
        SELECT count(*) FROM (
          SELECT payment_dt FROM ref.warehouse.fct_payments
          EXCEPT ALL
          SELECT payment_dt FROM wh.warehouse.fct_payments)
        WHERE payment_dt < DATE '{CONTRACT['defect']['window_start']}'
           OR payment_dt > DATE '{CONTRACT['defect']['window_end']}'""")
    if outside != 0:
        out.append(f"{outside} dropped rows sit outside the declared window")

    # 3. THE RULE IS NOT USELESS. Deleting it is the archetype's adversary and
    # must be wrong, which requires genuinely corrupt rows to exist.
    harness = one(f"SELECT count(*) FROM wh.raw.payments_landing "
                  f"WHERE terminal_ref IN ({excl})")
    if harness != CONTRACT["rule"]["rows_it_must_still_reject"]:
        out.append(f"the stream holds {harness} harness rows, contract says "
                   f"{CONTRACT['rule']['rows_it_must_still_reject']}")
    if harness <= 0:
        out.append("no harness rows exist, so disabling the rule entirely would be "
                   "a correct repair and the archetype's adversary is unarmed")
    in_mart = one(f"SELECT count(*) FROM wh.warehouse.fct_payments "
                  f"WHERE terminal_ref IN ({excl})")
    if in_mart != 0:
        out.append(f"{in_mart} harness rows are already in the mart; the rule's "
                   "correct half is not working and the unit has two defects")

    # 4. INVISIBILITY. If the loss exceeded the daily volume swing, a volume
    # chart would show a step and this would be ordinary triage.
    s = stats["symptom"]
    if s["daily_loss_pct_max"] >= s["daily_volume_swing_pct"]:
        out.append(f"the worst day's loss ({s['daily_loss_pct_max']}%) is not "
                   f"inside the daily volume swing ({s['daily_volume_swing_pct']}%); "
                   "the defect would be visible in a volume chart")
    if s["survival_pct_before_rule"] != 100.0:
        out.append("rows are missing from before the rule was deployed; the "
                   "step-change that dates the defect is gone")

    # 5. THE WINDOW ENDS AT TODAY. A defect that stopped on its own does not
    # need repairing and the restatement gate becomes optional.
    if CONTRACT["defect"]["window_end"] != CONTRACT["history"]["end"]:
        out.append("the damage window ends before the history does; the defect is "
                   "no longer live")
    return out


def _stacking_checks(con, stats: dict) -> list[str]:
    """The four properties the 2x2 depends on, asserted as properties.

    Numbers are compared elsewhere; these are the claims that make A and B
    two defects rather than one, and that make this world GT006 plus A rather
    than a new world.
    """
    out = []
    k = stats["stack"]

    # 1. DISJOINT. A's merchants (absent from the list) and B's merchants (the
    #    ones with NULL terminal_refs) share no member. If they did, repairing
    #    one would change what the other's gate sees.
    if k["merchants_in_both_defects"] != 0:
        out.append(f"{k['merchants_in_both_defects']} merchant(s) are touched by "
                   f"BOTH defects; A and B must be disjoint or the 2x2 is not "
                   f"measuring two things")
    # ... and A's merchants must exist, or there is no A.
    if k["missing_merchants"] <= 0 or k["missing_merchant_months"] <= 0:
        out.append("no merchant is missing from the report; there is no defect A")
    # ... and every A merchant is missing for EVERY month, so 'fixed A' has one
    #     meaning: missing_merchant_months == missing_merchants x months.
    if k["missing_merchant_months"] != k["missing_merchants"] * k["months"]:
        out.append(f"{k['missing_merchant_months']} merchant-months are missing "
                   f"but {k['missing_merchants']} merchants x {k['months']} months "
                   f"= {k['missing_merchants'] * k['months']}; A is not one clean "
                   f"mechanism")

    # 2. BYTE-IDENTICAL TO GT006. The tables B touches carry the original
    #    contract's digests, literally. (Checked here and not only in
    #    _digest_checks because --no-digests must not be able to skip it.)
    orig = CONTRACT["gt006_identical"]
    if CONTRACT["defect"]["dropped_rows"] != orig["dropped_rows"] or \
            [CONTRACT["defect"]["window_start"], CONTRACT["defect"]["window_end"]] \
            != orig["window"]:
        out.append("CONTRACT.defect no longer matches GT006's dropped_rows/window; "
                   "B has drifted")
    for db, want in orig["digests"].items():
        for table, digest in want.items():
            if CONTRACT["digests"][db].get(table) != digest:
                out.append(f"CONTRACT.digests[{db}][{table}] differs from the "
                           f"GT006 value; B's data is no longer byte-identical")
    for rel, digest in orig["workspace_sha256"].items():
        if CONTRACT["workspace_sha256"].get(rel) != digest:
            out.append(f"CONTRACT.workspace_sha256[{rel}] differs from the GT006 "
                       f"value; B's repair surface has changed")
    if CONTRACT["probe"]["sha256"] != orig["probe_sha256"]:
        out.append("the G1c probe batch differs from GT006's; B's liveness gate "
                   "has changed")

    # 2b. THE BAIT IS FALSE. Merchants the report DOES carry are short against
    #     the feed (defect B), so "the missing merchants explain the gap" is
    #     refutable in one query. If this were 0, fixing A would finish the job.
    if k["present_merchant_months_short"] <= 0:
        out.append("no merchant-month in the report is short against the feed; "
                   "Marguerite's attribution of the gap to the missing merchants "
                   "would be TRUE and the stacked row would measure nothing")

    # 3. A IS THE ONLY DEFECT IN THE ROLLUP. Rows for merchants the list DOES
    #    carry tie to the mart exactly.
    if k["present_rows_differing"] != 0:
        out.append(f"{k['present_rows_differing']} rollup row(s) for merchants "
                   f"the list carries differ from the mart; the rollup has a "
                   f"second defect")

    # 4. THE LIST HINTS AT NOTHING ABOUT B. No acquirer column, no NULL-ref
    #    vocabulary, and the dim carries exactly the first DIM_MERCHANTS ids.
    cols = [r[0] for r in con.execute(
        "SELECT column_name FROM duckdb_columns() WHERE database_name = 'wh' "
        "AND schema_name = 'reporting' AND table_name = 'dim_merchant' "
        "ORDER BY column_index").fetchall()]
    for c in cols:
        if any(tok in c.lower() for tok in ("acquirer", "terminal", "legacy")):
            out.append(f"reporting.dim_merchant carries column {c!r}, which "
                       f"points at defect B's mechanism")
    ids = [r[0] for r in con.execute(
        "SELECT merchant_id FROM wh.reporting.dim_merchant ORDER BY 1").fetchall()]
    want_ids = [f"M{i:04d}" for i in range(1, CONTRACT["stack"]["dim_merchants"] + 1)]
    if ids != want_ids:
        out.append(f"reporting.dim_merchant carries {len(ids)} ids "
                   f"{ids[:3]}..{ids[-1:]}, contract says M0001..{want_ids[-1]}")
    return out


def _absent_control_checks(con) -> list[str]:
    """The monitoring must not contain the control that would find this.

    A003's premise is asymmetric monitoring: freshness and volume are watched,
    correctness is not. A reconciliation control here would resolve the unit on
    the first `sql_query`.
    """
    out = []
    names = [r[0] for r in con.execute(
        "SELECT DISTINCT check_name FROM wh.ops.dq_checks ORDER BY 1").fetchall()]
    if names != CONTRACT["green"]["dq_check_names"]:
        out.append(f"ops.dq_checks names {names}, contract says "
                   f"{CONTRACT['green']['dq_check_names']}")
    for name in names:
        for token in CONTRACT["green"]["forbidden_control_tokens"]:
            if token in name.lower():
                out.append(f"ops.dq_checks carries {name!r}, a control that would "
                           f"detect the defect it is supposed to miss")
    return out


def _workspace_checks(fx: Path) -> list[str]:
    """The agent-visible tree, byte for byte, plus the two properties of it that
    the gates depend on."""
    out = []
    ws = fx / "workspace"
    if not ws.is_dir():
        return [f"{ws} does not exist"]
    for rel, want in sorted(CONTRACT["workspace_sha256"].items()):
        p = ws / rel
        if not p.is_file():
            out.append(f"workspace/{rel} is missing")
            continue
        raw = p.read_bytes()
        if b"\r\n" in raw:
            out.append(f"workspace/{rel} carries CRLF; the world stops being "
                       "byte-identical across hosts")
        got = hashlib.sha256(raw).hexdigest()
        if got != want:
            out.append(f"workspace/{rel} sha256 {got[:12]} != contract {want[:12]}")
    extra = sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*")
                   if p.is_file()
                   and p.relative_to(ws).as_posix() not in CONTRACT["workspace_sha256"])
    if extra:
        out.append(f"workspace holds files the contract does not describe: {extra}")

    # The defect must be READABLE in the repo, in both places, or a code fix is
    # a guess rather than a repair.
    predicate = CONTRACT["rule"]["shipped_predicate"]
    for rel in CONTRACT["rule"]["applied_in"]:
        body = (ws / rel).read_text(encoding="utf-8") if (ws / rel).is_file() else ""
        if predicate not in body:
            out.append(f"workspace/{rel} does not carry the shipped predicate "
                       f"{predicate!r}; the defect is not where the contract says")
        if "IS DISTINCT FROM" in body or "coalesce(terminal_ref" in body.lower():
            out.append(f"workspace/{rel} already carries a NULL-safe form; the "
                       "world ships pre-repaired")

    # DEFECT A must be readable too: the inner join is in the model, the list
    # stops where the contract says, and neither ships repaired.
    k = CONTRACT["stack"]
    model = ws / k["model"]
    body = model.read_text(encoding="utf-8") if model.is_file() else ""
    if k["shipped_join"] not in body:
        out.append(f"workspace/{k['model']} does not carry the shipped join "
                   f"{k['shipped_join']!r}; defect A is not where the contract says")
    if "left join" in body.lower() or "left outer" in body.lower():
        out.append(f"workspace/{k['model']} already joins LEFT; the world ships "
                   f"with A repaired")
    csv_path = ws / k["merchant_list"]
    lines = csv_path.read_text(encoding="utf-8").splitlines() if csv_path.is_file() else []
    if len(lines) - 1 != k["dim_merchants"]:
        out.append(f"workspace/{k['merchant_list']} lists {len(lines) - 1} "
                   f"merchants, contract says {k['dim_merchants']}")
    return out


def _exposure_checks(fx: Path) -> list[str]:
    """The BI layer the business reads.

    The dashboard must read the MATERIALISED EXTRACT, not the mart. If it read
    the mart directly it would correct itself the moment the mart was repaired,
    and the downstream half of the restatement - the number actually in front of
    the business - would stop being something the agent has to do anything
    about.
    """
    out = []
    d = fx / "bi" / "dashboards" / f"{CONTRACT['layout']['dashboard_id']}.yml"
    if not d.is_file():
        return [f"{d} does not exist"]
    body = d.read_bytes()
    if b"\r\n" in body:
        out.append(f"{d.name} carries CRLF")
    text = body.decode("utf-8")
    # The gateway's own schema (alkera_gateway/surfaces.py:326-351) forbids extra
    # keys. A dashboard that does not validate loads as `invalid_dashboard` and
    # the WHOLE BI surface reads dead - which is how a declared surface becomes
    # a silently absent one.
    try:
        spec = yaml.safe_load(text)
    except Exception as exc:                        # noqa: BLE001 - reported
        return [f"{d.name} is not parseable yaml: {exc}"]
    if not isinstance(spec, dict) or spec.get("dashboard") != \
            CONTRACT["layout"]["dashboard_id"]:
        out.append(f"{d.name} has no `dashboard: "
                   f"{CONTRACT['layout']['dashboard_id']}` key; the gateway keys "
                   f"on that field, not on the filename")
        return out
    allowed = {"version", "dashboard", "title", "owner", "metrics", "panels"}
    stray = sorted(set(spec) - allowed)
    if stray:
        out.append(f"{d.name} carries {stray}, which the gateway's Dashboard "
                   f"model forbids; it would load as invalid_dashboard")
    panels = spec.get("panels") or []
    if not panels:
        out.append(f"{d.name} declares no panel")
    for panel in panels:
        if set(panel) - {"name", "sql", "title", "metric", "row_filter",
                         "watermark_sql"}:
            out.append(f"{d.name} panel {panel.get('name')!r} carries fields the "
                       f"gateway's Panel model forbids")
        sql = str(panel.get("sql", ""))
        if CONTRACT["layout"]["bi_extract"] not in sql:
            out.append(f"{d.name} panel {panel.get('name')!r} does not read "
                       f"{CONTRACT['layout']['bi_extract']}")
        for direct in (CONTRACT["layout"]["mart"], CONTRACT["layout"]["kpi"]):
            if direct in sql:
                out.append(f"{d.name} panel {panel.get('name')!r} names {direct} "
                           f"directly; it must read the extract, which is one "
                           f"refresh behind by design")
    return out


def _probe_checks(fx: Path) -> list[str]:
    """The G1c liveness probe. Every clause of the gate is a property of this
    file, so a drifted probe silently changes what liveness means."""
    out = []
    csv_path = fx / CONTRACT["probe"]["csv"]
    man_path = fx / CONTRACT["probe"]["manifest"]
    if not csv_path.is_file():
        return [f"{csv_path} does not exist"]
    body = csv_path.read_bytes()
    if b"\r\n" in body:
        out.append("the probe batch carries CRLF")
    got = hashlib.sha256(body).hexdigest()
    if got != CONTRACT["probe"]["sha256"]:
        out.append(f"probe batch sha256 {got[:12]} != contract "
                   f"{CONTRACT['probe']['sha256'][:12]}")
    man = json.loads(man_path.read_text(encoding="utf-8"))
    if man["rows"] != CONTRACT["probe"]["rows"]:
        out.append(f"probe manifest says {man['rows']} rows, contract says "
                   f"{CONTRACT['probe']['rows']}")
    if len(man["must_be_admitted"]) != CONTRACT["probe"]["admit"]:
        out.append(f"probe admits {len(man['must_be_admitted'])}, contract says "
                   f"{CONTRACT['probe']['admit']}")
    if len(man["must_be_rejected"]) != CONTRACT["probe"]["reject"]:
        out.append(f"probe rejects {len(man['must_be_rejected'])}, contract says "
                   f"{CONTRACT['probe']['reject']}")
    if len(man["null_ref_ids"]) != CONTRACT["probe"]["null_ref"]:
        out.append(f"probe carries {len(man['null_ref_ids'])} NULL-ref rows, "
                   f"contract says {CONTRACT['probe']['null_ref']}")
    if not set(man["null_ref_ids"]) <= set(man["must_be_admitted"]):
        out.append("a NULL-ref probe row is not in must_be_admitted; the gate "
                   "would not measure the mechanism")
    if set(man["must_be_admitted"]) & set(man["must_be_rejected"]):
        out.append("a probe row is in both must_be_admitted and must_be_rejected")

    # The three classes must actually be present in the CSV bytes. A manifest
    # that agrees with itself about a file that says something else is the exact
    # failure this whole module exists to stop.
    text = body.decode("utf-8").splitlines()
    header = text[0].split(",")
    idx = {c: i for i, c in enumerate(header)}
    nulls = refs = ordinary = 0
    for line in text[1:]:
        cells = line.split(",")
        ref = cells[idx["terminal_ref"]]
        if ref == "":
            nulls += 1
        elif ref in CONTRACT["rule"]["excluded_refs"]:
            refs += 1
        else:
            ordinary += 1
    if (nulls, refs) != (CONTRACT["probe"]["null_ref"], CONTRACT["probe"]["reject"]):
        out.append(f"the probe CSV holds {nulls} NULL-ref and {refs} harness rows, "
                   f"contract says {CONTRACT['probe']['null_ref']} and "
                   f"{CONTRACT['probe']['reject']}")
    if ordinary + nulls != CONTRACT["probe"]["admit"]:
        out.append(f"the probe CSV holds {ordinary + nulls} admissible rows, "
                   f"contract says {CONTRACT['probe']['admit']}")
    return out


def _digest_checks(fx: Path) -> list[str]:
    out = []
    for db, want in sorted(CONTRACT["digests"].items()):
        got = mfw.content_digest(fx / db)
        for table in sorted(set(want) | set(got)):
            if got.get(table) != want.get(table):
                out.append(f"{db}:{table} content digest {got.get(table)} != "
                           f"contract {want.get(table)}")
    return out


# ------------------------------------------------------------ falsifiability ---
# A verifier nobody has tried to fool is a verifier with an unknown false-pass
# rate. `verify()` is the one thing every score keys off, so the claim it makes
# has to be TESTED, not asserted: for each mutation below, a scratch copy of the
# fixture is damaged in one specific way and `verify()` must refuse it. A
# mutation that slips through is a hole in the contract and is printed as one.
#
# The mutation is always the SMALLEST edit that makes the world stop being this
# world. "Drop the mart" proves nothing; "restore one dropped row" proves the
# count is load-bearing.
#
#     python fixture_contract.py --falsify
#
# 22 mutations in GT006; 30 here (6 SQL and 2 file mutations for defect A),
# all caught.

_FALSIFY_SQL = [
    ("the defect is gone: every dropped row is back in the mart",
     "INSERT INTO warehouse.fct_payments SELECT payment_id, payment_dt, "
     "captured_at, merchant_id, channel, terminal_ref, card_last4, amount_gross, "
     "fee_amount, amount_net, status, batch_id, arrival_ts, "
     "arrival_ts + INTERVAL 40 MINUTE FROM raw.payments_landing "
     "WHERE terminal_ref IS NULL AND payment_dt >= DATE '{win_start}'"),

    ("the defect is undercounted: one dropped row is back",
     "INSERT INTO warehouse.fct_payments SELECT payment_id, payment_dt, "
     "captured_at, merchant_id, channel, terminal_ref, card_last4, amount_gross, "
     "fee_amount, amount_net, status, batch_id, arrival_ts, "
     "arrival_ts + INTERVAL 40 MINUTE FROM raw.payments_landing "
     "WHERE terminal_ref IS NULL AND payment_dt >= DATE '{win_start}' "
     "ORDER BY payment_id LIMIT 1"),

    ("the defect is overcounted: one surviving row is deleted",
     "DELETE FROM warehouse.fct_payments WHERE payment_id = "
     "(SELECT min(payment_id) FROM warehouse.fct_payments "
     " WHERE payment_dt >= DATE '{win_start}' AND terminal_ref IS NOT NULL)"),

    ("the unit drifts onto GT004's mechanism: a payment_id is duplicated",
     "INSERT INTO warehouse.fct_payments SELECT * FROM warehouse.fct_payments "
     "WHERE payment_id = (SELECT min(payment_id) FROM warehouse.fct_payments)"),

    ("the loss reaches a non-NULL row: a populated terminal_ref row is dropped",
     "DELETE FROM warehouse.fct_payments WHERE payment_id = "
     "(SELECT min(payment_id) FROM warehouse.fct_payments "
     " WHERE terminal_ref LIKE 'V%')"),

    ("the window starts earlier: a pre-deploy NULL-ref row is dropped",
     "DELETE FROM warehouse.fct_payments WHERE payment_id = "
     "(SELECT min(payment_id) FROM warehouse.fct_payments "
     " WHERE payment_dt < DATE '{win_start}' AND terminal_ref IS NULL)"),

    ("the defect stopped on its own: the last partition is complete again",
     "INSERT INTO warehouse.fct_payments SELECT payment_id, payment_dt, "
     "captured_at, merchant_id, channel, terminal_ref, card_last4, amount_gross, "
     "fee_amount, amount_net, status, batch_id, arrival_ts, "
     "arrival_ts + INTERVAL 40 MINUTE FROM raw.payments_landing "
     "WHERE terminal_ref IS NULL AND payment_dt = DATE '{win_end}'"),

    ("the rule's correct half stopped working: a harness row is in the mart",
     "INSERT INTO warehouse.fct_payments SELECT payment_id, payment_dt, "
     "captured_at, merchant_id, channel, terminal_ref, card_last4, amount_gross, "
     "fee_amount, amount_net, status, batch_id, arrival_ts, "
     "arrival_ts + INTERVAL 40 MINUTE FROM raw.payments_landing "
     "WHERE terminal_ref = '{harness}' LIMIT 1"),

    ("the adversary is unarmed: the stream carries no harness traffic",
     "DELETE FROM raw.payments_landing WHERE terminal_ref IN ({excl})"),

    ("the mechanism is gone: terminal_ref is never NULL in the stream",
     "UPDATE raw.payments_landing SET terminal_ref = 'V00099' "
     "WHERE terminal_ref IS NULL"),

    ("the loss escapes normal variance: half of every day is dropped",
     "DELETE FROM warehouse.fct_payments WHERE payment_dt >= DATE '{win_start}' "
     "AND payment_id % 2 = 0"),

    ("the KPI table is no longer derived from the damaged mart",
     "CREATE OR REPLACE TABLE warehouse.kpi_payments_daily AS "
     "SELECT payment_dt AS dt, count(*) AS payments, "
     "count(DISTINCT merchant_id) AS merchants, "
     "CAST(sum(amount_gross) AS DECIMAL(14,2)) AS gross_amount, "
     "CAST(sum(fee_amount) AS DECIMAL(14,2)) AS fee_amount, "
     "CAST(sum(amount_net) AS DECIMAL(14,2)) AS net_amount "
     "FROM raw.payments_landing WHERE terminal_ref IS DISTINCT FROM '{harness}' "
     "AND terminal_ref IS DISTINCT FROM '{smoke}' GROUP BY 1 ORDER BY 1"),

    ("the BI extract is not the stale copy the dashboard reads",
     "UPDATE warehouse.bi_payments_extract SET payments = payments + 1 "
     "WHERE dt = DATE '{win_start}'"),

    ("a surface goes red: one nightly job run failed",
     "UPDATE ops.job_runs SET status = 'failed' WHERE run_id = "
     "(SELECT min(run_id) FROM ops.job_runs)"),

    ("a surface goes red: a data quality control failed",
     "UPDATE ops.dq_checks SET status = 'FAIL', failed_rows = 3 "
     "WHERE checked_at = (SELECT max(checked_at) FROM ops.dq_checks) "
     "AND check_name = 'row_count_gt_zero'"),

    ("a surface goes red: a freshness check did not pass",
     "UPDATE ops.freshness_checks SET status = 'WARN' "
     "WHERE checked_at = (SELECT max(checked_at) FROM ops.freshness_checks)"),

    ("the monitoring gains the control that would find this",
     "INSERT INTO ops.dq_checks SELECT checked_at, dataset, "
     "'reconcile_mart_to_landing', 'PASS', 0 FROM ops.dq_checks LIMIT 1"),

    # ---- defect A ------------------------------------------------------------
    ("A is gone: the commercial list carries every merchant",
     "INSERT INTO reporting.dim_merchant SELECT DISTINCT merchant_id, "
     "'added', 'retail' FROM warehouse.fct_payments WHERE merchant_id NOT IN "
     "(SELECT merchant_id FROM reporting.dim_merchant)"),

    ("A is gone: the report was rebuilt without the join",
     "CREATE OR REPLACE TABLE reporting.rpt_merchant_monthly AS "
     "SELECT strftime(payment_dt, '%Y-%m') AS month, merchant_id, "
     "count(*) AS payments, CAST(sum(amount_net) AS DECIMAL(14,2)) AS net_amount "
     "FROM warehouse.fct_payments GROUP BY 1, 2 ORDER BY 1, 2"),

    ("A is undercounted: one missing merchant-month is back in the report",
     "INSERT INTO reporting.rpt_merchant_monthly SELECT '2026-03', 'M0031', "
     "'added', count(*), CAST(sum(amount_net) AS DECIMAL(14,2)) "
     "FROM warehouse.fct_payments WHERE merchant_id = 'M0031' "
     "AND strftime(payment_dt, '%Y-%m') = '2026-03'"),

    ("A and B overlap: a legacy-acquirer merchant leaves the commercial list",
     "DELETE FROM reporting.dim_merchant WHERE merchant_id = 'M0001'"),

    ("the rollup grows a second defect: a present merchant's row is rewritten",
     "UPDATE reporting.rpt_merchant_monthly SET payments = payments + 1 "
     "WHERE merchant_id = 'M0010' AND month = '2026-01'"),

    ("the commercial list hints at B: it grows an acquirer column",
     "ALTER TABLE reporting.dim_merchant ADD COLUMN acquirer VARCHAR"),
]


def _falsify_files() -> list:
    """Mutations to the agent-visible tree, as (label, callable(fixture_dir))."""
    def crlf_the_readme(fx):
        p = fx / "workspace" / "README.md"
        p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))

    def prerepair_the_loader(fx):
        p = fx / "workspace" / "pipelines" / "build_payments.sql"
        p.write_text(p.read_text(encoding="utf-8").replace(
            CONTRACT["rule"]["shipped_predicate"],
            "terminal_ref IS DISTINCT FROM 'TEST-HARNESS' "
            "AND l.terminal_ref IS DISTINCT FROM 'SMOKE-TEST'"),
            encoding="utf-8", newline="")

    def strip_the_backfill_predicate(fx):
        p = fx / "workspace" / "pipelines" / "backfill_payments.sql"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "  AND l." + CONTRACT["rule"]["shipped_predicate"] + ";", ";"),
            encoding="utf-8", newline="")

    def drop_the_probe_null_rows(fx):
        p = fx / "probe_batch" / "payments.csv"
        lines = p.read_text(encoding="utf-8").splitlines()
        head = lines[0].split(",")
        ref = head.index("terminal_ref")
        kept = [lines[0]] + [ln for ln in lines[1:] if ln.split(",")[ref] != ""]
        p.write_text("\n".join(kept) + "\n", encoding="utf-8", newline="")

    def dashboard_reads_the_mart(fx):
        p = (fx / "bi" / "dashboards" /
             f"{CONTRACT['layout']['dashboard_id']}.yml")
        p.write_text(p.read_text(encoding="utf-8").replace(
            CONTRACT["layout"]["bi_extract"], CONTRACT["layout"]["kpi"]),
            encoding="utf-8", newline="")

    def prerepair_the_model(fx):
        p = fx / "workspace" / CONTRACT["stack"]["model"]
        p.write_text(p.read_text(encoding="utf-8").replace(
            CONTRACT["stack"]["shipped_join"],
            "left " + CONTRACT["stack"]["shipped_join"]),
            encoding="utf-8", newline="")

    def complete_the_list(fx):
        p = fx / "workspace" / CONTRACT["stack"]["merchant_list"]
        p.write_text(p.read_text(encoding="utf-8")
                     + "M0031,Added Merchant,retail\n", encoding="utf-8", newline="")

    return [
        ("a workspace file picked up CRLF", crlf_the_readme),
        ("the world ships with A repaired: the model joins LEFT",
         prerepair_the_model),
        ("the commercial list was completed on disk", complete_the_list),
        ("the world ships pre-repaired: the loader is already NULL-safe",
         prerepair_the_loader),
        ("the second copy of the rule is gone from the backfill",
         strip_the_backfill_predicate),
        ("the liveness probe lost its NULL-ref rows", drop_the_probe_null_rows),
        ("the dashboard reads the mart, not the stale extract",
         dashboard_reads_the_mart),
    ]


def falsify(fx: Path, quiet: bool = False) -> int:
    """Damage a scratch copy N ways; verify() must refuse every one.

    Returns the number of mutations that slipped through. Non-zero is a hole in
    the contract, not a failed build.
    """
    import shutil
    import tempfile

    fx = Path(fx)
    subs = {
        "win_start": CONTRACT["defect"]["window_start"],
        "win_end": CONTRACT["defect"]["window_end"],
        "harness": CONTRACT["rule"]["excluded_refs"][0],
        "smoke": CONTRACT["rule"]["excluded_refs"][1],
        "excl": ", ".join(f"'{r}'" for r in CONTRACT["rule"]["excluded_refs"]),
    }
    cases = ([(label, sql.format(**subs), None) for label, sql in _FALSIFY_SQL]
             + [(label, None, fn) for label, fn in _falsify_files()])

    escaped = []
    root = Path(tempfile.mkdtemp(prefix="gt006-falsify-"))
    try:
        for n, (label, sql, fn) in enumerate(cases, 1):
            scratch = root / f"m{n:02d}"
            shutil.copytree(fx, scratch)
            if sql is not None:
                con = duckdb.connect(":memory:", config={"threads": 1})
                con.execute(f"ATTACH '{(scratch / 'world.duckdb').as_posix()}' AS wh")
                con.execute("USE wh")
                con.execute(sql)
                con.execute("CHECKPOINT")
                con.close()
            else:
                fn(scratch)
            try:
                verify(scratch, quiet=True)
            except FixtureDrift:
                caught = True
            except Exception:                    # noqa: BLE001 - also a refusal
                caught = True
            else:
                caught = False
                escaped.append(label)
            if not quiet:
                print(f"  {'caught ' if caught else 'ESCAPED'} {n:>2}. {label}")
            shutil.rmtree(scratch, ignore_errors=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if not quiet:
        print(f"\n  {len(cases) - len(escaped)}/{len(cases)} mutations caught")
        for label in escaped:
            print(f"    HOLE: verify() accepted a world where {label}")
    return len(escaped)


def _raise(problems: list[str], fx: Path) -> None:
    raise FixtureDrift(
        "FIXTURE DOES NOT MATCH CONTRACT\n\n  "
        + "\n  ".join(problems)
        + "\n\n  Regenerate:\n    python make_fixture_world.py --out "
        + str(fx) + " --seed " + str(CONTRACT["seed"])
        + "\n\n  Scores computed against a drifted fixture are meaningless.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default=str(Path(__file__).parent / "build"))
    ap.add_argument("--no-digests", action="store_true",
                    help="skip the content-digest sweep (faster, weaker)")
    ap.add_argument("--dump", action="store_true", help="print CONTRACT as json")
    ap.add_argument("--falsify", action="store_true",
                    help="damage a scratch copy N ways and prove verify() "
                         "refuses every one")
    a = ap.parse_args()
    if a.falsify:
        return 1 if falsify(Path(a.fixtures)) else 0
    if a.dump:
        print(json.dumps(CONTRACT, indent=2, sort_keys=True))
        return 0
    try:
        verify(Path(a.fixtures), digests=not a.no_digests)
    except FixtureDrift as e:
        print(str(e))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
