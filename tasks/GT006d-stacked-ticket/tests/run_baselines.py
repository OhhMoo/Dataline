#!/usr/bin/env python3
"""
GT006c/d (STACKED) task-validity suite. Prints TASK VALID or says why not.

TWO DEFECTS, and the suite's first job is to prove the grader keeps them
apart. `fix_A_only` repairs the merchant rollup and leaves the NULL-unsafe
rule; `fix_B_only` is GT006's oracle with the rollup rebuilt through the
SHIPPED (broken) model, as `dbt run` would. Each must score its own defect's
checks at full marks and the other's at zero, and each must FAIL - the
`repairs` block in the grader's json must read A=repaired B=untouched and
A=untouched B=repaired respectively. Every GT006 baseline is kept and now also
fails on A, which is asserted rather than assumed.

The suite's OUTPUT prints A and B per baseline so "A fixed, B untouched" is
legible at a glance.

    python run_baselines.py
    python run_baselines.py --verbose
    python run_baselines.py --only oracle,no_op

WHAT A BASELINE SUITE IS FOR. Not "does the grader run". A rubric is a claim
about what behaviour earns what score, and every one of those claims is
falsifiable by building the agent that behaves that way. Each baseline below
EXISTS as executable code rather than as a sentence in a design document,
because a described adversary is an untested one.

THE ARCHETYPE-SPECIFIC ADVERSARY is `disable_the_rule`. A003's failure is caused
by the quality control itself, so the obvious remediation - turn the control off
- restores every dropped row and looks completely repaired by row count. It must
not score full marks, and proving that is the single most important thing this
suite does.

THE MANDATORY LIVENESS BASELINES are `permanent_skip`, `data_only_patch` and
`delete_the_loader`. A validation "fixed" by never running, a load fixed into a
permanent skip, and a table hand-patched under an unchanged pipeline are all
trivially consistent and repair nothing. Each must score ZERO on G1c.

THE SUBSIDY BASELINE is `no_op`. Every clause of this rubric that PAYS must
score zero for it. That is asserted here per check, not once on the total,
because a total of 0 can hide a +2 and a -2.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parent.parent
sys.path.insert(0, str(TASK / "fixtures"))
sys.path.insert(0, str(HERE))

from fixture_contract import CONTRACT, verify, FixtureDrift       # noqa: E402
from fixture_contract import assert_reference_unreachable         # noqa: E402

PY = sys.executable
MART = "warehouse.fct_payments"
KPI = "warehouse.kpi_payments_daily"
BROKEN = CONTRACT["rule"]["shipped_predicate"]
WIN_START = CONTRACT["defect"]["window_start"]
WIN_END = CONTRACT["defect"]["window_end"]
EXCL = CONTRACT["rule"]["excluded_refs"]

NULL_SAFE = (f"terminal_ref IS DISTINCT FROM '{EXCL[0]}'\n"
             f"  AND l.terminal_ref IS DISTINCT FROM '{EXCL[1]}'")


# ------------------------------------------------------------------ plumbing
def open_rw(world_root: Path):
    """Aliased ATTACH (F622). Every adversary reaches the warehouse this way, so
    the oracle is not the only thing proven to work at the deployed filename."""
    db = Path(world_root) / CONTRACT["layout"]["deployed_world_relpath"]
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{db.as_posix()}' AS wh")
    con.execute("USE wh")
    return con


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def patch_predicate(ws: Path, replacement: str, files=("build_payments.sql",
                                                       "backfill_payments.sql")):
    for name in files:
        p = ws / "pipelines" / name
        body = p.read_text(encoding="utf-8")
        if BROKEN not in body:
            raise RuntimeError(f"{name}: the shipped predicate is not there")
        write(p, body.replace(BROKEN, replacement))


def restate(con, *, predicate: str, start=WIN_START, end=WIN_END):
    """Delete and re-insert a closed range of the mart under some predicate."""
    con.execute(f"DELETE FROM {MART} WHERE payment_dt BETWEEN DATE '{start}' "
                f"AND DATE '{end}'")
    where = f"AND {predicate}" if predicate else ""
    con.execute(f"""
        INSERT INTO {MART}
        SELECT l.payment_id, l.payment_dt, l.captured_at, l.merchant_id,
               l.channel, l.terminal_ref, l.card_last4, l.amount_gross,
               l.fee_amount, l.amount_net, l.status, l.batch_id, l.arrival_ts,
               l.arrival_ts + INTERVAL 40 MINUTE
        FROM raw.payments_landing l
        WHERE l.payment_dt BETWEEN DATE '{start}' AND DATE '{end}' {where}""")


def rebuild_downstream(con):
    con.execute(f"""
        CREATE OR REPLACE TABLE {KPI} AS
        SELECT payment_dt AS dt, COUNT(*) AS payments,
               COUNT(DISTINCT merchant_id) AS merchants,
               CAST(SUM(amount_gross) AS DECIMAL(14,2)) AS gross_amount,
               CAST(SUM(fee_amount) AS DECIMAL(14,2)) AS fee_amount,
               CAST(SUM(amount_net) AS DECIMAL(14,2)) AS net_amount
        FROM {MART} GROUP BY 1 ORDER BY 1""")
    con.execute("CREATE OR REPLACE TABLE warehouse.bi_payments_extract AS "
                f"SELECT * FROM {KPI}")


def report(ws: Path, **over):
    doc = {
        "primary_cause": "",
        "damage_window": None,
        "supporting_evidence": [],
        "repair_strategy": "",
        "remaining_risks": [],
        "additional_causes": [],
    }
    doc.update(over)
    write(ws / "final_report.json", json.dumps(doc, indent=2) + "\n")


GOOD_CAUSE = ("The DATA-2071 exclusion predicate is NULL-unsafe: terminal_ref "
              "NOT IN (...) is UNKNOWN rather than TRUE when terminal_ref is "
              "NULL, so the load has been silently dropping legitimate "
              "card-not-present payments.")
GOOD_CAUSE_A = ("rpt_merchant_monthly inner-joins reporting.dim_merchant, the "
                "commercial team's merchant list, which stops at M0030, so every "
                "merchant above it is missing from the statements pack.")

SHIPPED_JOIN = CONTRACT["stack"]["shipped_join"]
MODEL = CONTRACT["stack"]["model"]


# ------------------------------------------------------------ defect A moves
def fix_model_left_join(ws: Path) -> None:
    p = ws / MODEL
    body = p.read_text(encoding="utf-8")
    if SHIPPED_JOIN not in body:
        raise RuntimeError(f"{MODEL}: the shipped join is not there")
    write(p, body.replace(SHIPPED_JOIN, "left " + SHIPPED_JOIN))


def rebuild_rollup_through_model(con, ws: Path) -> None:
    """What `dbt run` does with the model AS IT STANDS in the workspace -
    broken or repaired - materialised where the business reads it."""
    import re
    model = (ws / MODEL).read_text(encoding="utf-8")
    model = re.sub(r"\{\{\s*config\(.*?\)\s*\}\}", "", model, flags=re.S)
    model = re.sub(r"\{\{\s*source\(\s*'(\w+)'\s*,\s*'(\w+)'\s*\)\s*\}\}",
                   r"\1.\2", model)
    assert "{{" not in model
    con.execute(f"CREATE OR REPLACE TABLE {CONTRACT['stack']['rollup_table']} AS "
                + model)


def rebuild_rollup_by_hand(con) -> None:
    """A correct rollup written by hand: right until the next dbt run."""
    con.execute(f"""
        CREATE OR REPLACE TABLE {CONTRACT['stack']['rollup_table']} AS
        SELECT strftime(payment_dt, '%Y-%m') AS month, merchant_id,
               COUNT(*) AS payments,
               CAST(SUM(amount_net) AS DECIMAL(14,2)) AS net_amount
        FROM {MART} GROUP BY 1, 2 ORDER BY 1, 2""")


# -------------------------------------------------------------------- agents
def agent_oracle(root: Path):
    # shutil.which, not the bare name: on Windows `subprocess` resolves "bash"
    # to System32\bash.exe (the WSL shim), which cannot see a Windows path and
    # reports the shipped solution.sh as "No such file or directory" with exit
    # 127 - an oracle failure that looks like a broken solution and is not.
    # POSIX paths for the same reason: git-bash eats the separators otherwise.
    bash = shutil.which("bash") or "bash"
    r = subprocess.run(
        [bash, (TASK / "solution" / "solution.sh").as_posix()],
        env={**_env(), "WORLD_ROOT": Path(root).as_posix(), "PYTHON": PY},
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"solution.sh exited {r.returncode}:\n{r.stdout}\n{r.stderr}")


def agent_no_op(root: Path):
    """Does nothing at all. Every clause that PAYS must score 0 for this."""


def agent_checklist(root: Path):
    """Writes the right answer and changes nothing. Diagnosis without repair."""
    report(root / "workspace", primary_cause=GOOD_CAUSE,
           damage_window=[WIN_START, WIN_END],
           repair_strategy="documented the issue for the next on-call")


def agent_disable_the_rule(root: Path):
    """THE ARCHETYPE ADVERSARY. The quality gate is causing loss, so remove it.

    Every dropped row comes back and the mart looks repaired by row count. It
    also re-admits every processor test-harness row, which is the same class of
    silent wrongness pointing the other way. Must not score full marks.
    """
    ws = root / "workspace"
    for name in ("build_payments.sql", "backfill_payments.sql"):
        p = ws / "pipelines" / name
        body = p.read_text(encoding="utf-8")
        body = body.replace(f"\n  -- DATA-2071: keep the processor's test harness "
                            f"out of the mart.\n  AND l.{BROKEN}", "")
        write(p, body)
    con = open_rw(root)
    restate(con, predicate="")
    rebuild_downstream(con)
    con.execute("CHECKPOINT")
    con.close()
    report(ws, primary_cause="a data quality filter was dropping rows, so I "
                             "removed the filter",
           damage_window=[WIN_START, WIN_END])


def agent_tighten_the_filter(root: Path):
    """A003's first expected failure mode: add more validation.

    "terminal_ref should never be empty, so let us also require it to be
    present." Every additional predicate on this load removes more data.
    """
    patch_predicate(root / "workspace",
                    f"terminal_ref NOT IN ('{EXCL[0]}', '{EXCL[1]}')\n"
                    f"  AND l.terminal_ref IS NOT NULL\n"
                    f"  AND l.amount_net > 0")
    con = open_rw(root)
    restate(con, predicate=f"l.terminal_ref NOT IN ('{EXCL[0]}', '{EXCL[1]}') "
                           f"AND l.terminal_ref IS NOT NULL AND l.amount_net > 0")
    rebuild_downstream(con)
    con.execute("CHECKPOINT")
    con.close()
    report(root / "workspace",
           primary_cause="data quality problems upstream; hardened the load",
           damage_window=[WIN_START, WIN_END])


def agent_data_only_patch(root: Path):
    """Restores the rows by hand and never touches the pipeline.

    The mart and the KPI are correct the moment it finishes. Tonight's load runs
    the same broken predicate and the loss starts again. G1c is the only thing
    in the rubric that can tell this apart from a real repair.
    """
    con = open_rw(root)
    restate(con, predicate=f"l.terminal_ref IS DISTINCT FROM '{EXCL[0]}' "
                           f"AND l.terminal_ref IS DISTINCT FROM '{EXCL[1]}'")
    rebuild_downstream(con)
    con.execute("CHECKPOINT")
    con.close()
    report(root / "workspace", primary_cause=GOOD_CAUSE,
           damage_window=[WIN_START, WIN_END],
           repair_strategy="backfilled the missing rows")


def agent_code_only_fix(root: Path):
    """Fixes both pipeline files correctly and restates nothing.

    Right from tonight, still wrong for every day the board pack covers.
    """
    patch_predicate(root / "workspace", NULL_SAFE)
    report(root / "workspace", primary_cause=GOOD_CAUSE,
           damage_window=[WIN_START, WIN_END],
           repair_strategy="fixed the predicate; the mart will self-heal")


def agent_permanent_skip(root: Path):
    """THE LIVENESS BASELINE. Repairs the data, then gates the load permanently
    off so it can never go wrong again.

    Idempotent by construction, consistent by construction, and it repairs
    nothing. It must score ZERO on G1c, not full marks.
    """
    ws = root / "workspace"
    patch_predicate(ws, NULL_SAFE)
    p = ws / "pipelines" / "build_payments.sql"
    body = p.read_text(encoding="utf-8")
    body = body.replace(
        "WHERE l.arrival_ts > COALESCE",
        "WHERE 1 = 0 -- load paused pending review\n  AND l.arrival_ts > COALESCE")
    write(p, body)
    con = open_rw(root)
    restate(con, predicate=f"l.terminal_ref IS DISTINCT FROM '{EXCL[0]}' "
                           f"AND l.terminal_ref IS DISTINCT FROM '{EXCL[1]}'")
    rebuild_downstream(con)
    con.execute("CHECKPOINT")
    con.close()
    report(ws, primary_cause=GOOD_CAUSE, damage_window=[WIN_START, WIN_END],
           repair_strategy="paused the load until it can be reviewed")


def agent_delete_the_loader(root: Path):
    """The other liveness shape: there is no nightly load any more."""
    (root / "workspace" / "pipelines" / "build_payments.sql").unlink()
    con = open_rw(root)
    restate(con, predicate=f"l.terminal_ref IS DISTINCT FROM '{EXCL[0]}' "
                           f"AND l.terminal_ref IS DISTINCT FROM '{EXCL[1]}'")
    rebuild_downstream(con)
    con.execute("CHECKPOINT")
    con.close()
    report(root / "workspace", primary_cause=GOOD_CAUSE,
           damage_window=[WIN_START, WIN_END])


def agent_nightly_only(root: Path):
    """Fixes the nightly loader and forgets the backfill, then restates with the
    backfill it did not fix.

    pipelines/README.md says in as many words that both files carry the rule.
    The restatement then re-applies the broken predicate and recovers nothing.
    """
    patch_predicate(root / "workspace", NULL_SAFE, files=("build_payments.sql",))
    con = open_rw(root)
    restate(con, predicate=f"l.{BROKEN}")
    rebuild_downstream(con)
    con.execute("CHECKPOINT")
    con.close()
    report(root / "workspace", primary_cause=GOOD_CAUSE,
           damage_window=[WIN_START, WIN_END])


def agent_nightly_only_hand_restate(root: Path):
    """ACCEPTED RESIDUAL, and the reason it is named rather than hidden.

    Fixes the NIGHTLY loader correctly, restates the mart with its own SQL, and
    leaves the second copy of the broken predicate sitting in
    pipelines/backfill_payments.sql.

    MEASURED: this scores 22/22 and PASSES. G1c executes the scheduled loader,
    which is now correct, and every other gate is about the data, which is now
    correct. Nothing in the rubric looks at the backfill.

    That is a scope boundary, not an oversight, and the boundary is: G1c's
    liveness claim is "the load that runs UNATTENDED tonight does the right
    thing on data nobody has looked at". The backfill is a hand-run repair tool
    (docs/runbook_payments_load.md says so) - leaving it broken is a latent risk
    for the next person who restates a range, not a live recurrence.

    It is asserted here as an EXPECTED PASS so that `TASK VALID` can never be
    read as "a half-fix cannot pass this unit". If a future version wants to
    close it, the cheap route is a second probe that executes the agent's
    backfill over a planted range - but note that the backfill carries a date
    range the agent is expected to edit, so executing it is fragile in a way the
    nightly path is not.
    """
    patch_predicate(root / "workspace", NULL_SAFE, files=("build_payments.sql",))
    con = open_rw(root)
    restate(con, predicate=f"l.terminal_ref IS DISTINCT FROM '{EXCL[0]}' "
                           f"AND l.terminal_ref IS DISTINCT FROM '{EXCL[1]}'")
    rebuild_downstream(con)
    con.execute("CHECKPOINT")
    con.close()
    report(root / "workspace", primary_cause=GOOD_CAUSE,
           damage_window=[WIN_START, WIN_END])


def agent_fix_A_only(root: Path):
    """THE ADVERSARY THE 2x2 IS ABOUT. Repairs the reported incident - the
    merchant rollup - correctly and durably, rebuilds the report through the
    repaired model, writes a complete-looking report, and never looks at the
    loader. Must score A1 and A2 in full, every B gate at ZERO, and FAIL. The
    grader's `repairs` block must read A=repaired B=untouched."""
    ws = root / "workspace"
    fix_model_left_join(ws)
    con = open_rw(root)
    rebuild_rollup_through_model(con, ws)
    con.execute("CHECKPOINT")
    con.close()
    report(ws, primary_cause=GOOD_CAUSE_A,
           damage_window=[CONTRACT["stack"]["dim_loaded"], WIN_END],
           repair_strategy="left join; rebuilt the statements pack")


def agent_fix_A_by_completing_the_list(root: Path):
    """The OTHER correct repair of A: complete config/merchants.csv and reload
    reporting.dim_merchant, leaving the inner join. Must pass A1 and A2 - the
    grader executes the model against the agent's own warehouse, where the
    list is now complete. Fails overall (B untouched)."""
    ws = root / "workspace"
    con = open_rw(root)
    missing = [r[0] for r in con.execute(
        f"SELECT DISTINCT merchant_id FROM {MART} WHERE merchant_id NOT IN "
        f"(SELECT merchant_id FROM {CONTRACT['stack']['dim_table']}) ORDER BY 1"
    ).fetchall()]
    csv = ws / CONTRACT["stack"]["merchant_list"]
    write(csv, csv.read_text(encoding="utf-8")
          + "".join(f"{m},Unnamed merchant {m},retail\n" for m in missing))
    con.execute(f"INSERT INTO {CONTRACT['stack']['dim_table']} "
                + " UNION ALL ".join(
                    f"SELECT '{m}', 'Unnamed merchant {m}', 'retail'" for m in missing))
    rebuild_rollup_through_model(con, ws)
    con.execute("CHECKPOINT")
    con.close()
    report(ws, primary_cause="the merchant list was incomplete; completed it",
           damage_window=[CONTRACT["stack"]["dim_loaded"], WIN_END])


def agent_fix_A_data_only(root: Path):
    """Rebuilds the statements pack by hand and never touches the model. The
    report is right until the next dbt run. A1 full, A2 ZERO - the A-side
    liveness clause."""
    con = open_rw(root)
    rebuild_rollup_by_hand(con)
    con.execute("CHECKPOINT")
    con.close()
    report(root / "workspace", primary_cause=GOOD_CAUSE_A,
           damage_window=[CONTRACT["stack"]["dim_loaded"], WIN_END])


def agent_fix_B_only(root: Path):
    """GT006's oracle, exactly, with the rollup rebuilt through the SHIPPED
    model as `dbt run` would (GT006's oracle rebuilt it by hand, which would
    accidentally repair A's data). Every B gate full, A1 and A2 ZERO, FAILS;
    `repairs` must read A=untouched B=repaired. This is the mirror of
    fix_A_only and together they are the independence proof."""
    ws = root / "workspace"
    patch_predicate(ws, NULL_SAFE)
    con = open_rw(root)
    restate(con, predicate=f"l.terminal_ref IS DISTINCT FROM '{EXCL[0]}' "
                           f"AND l.terminal_ref IS DISTINCT FROM '{EXCL[1]}'")
    rebuild_downstream(con)
    rebuild_rollup_through_model(con, ws)       # the broken model: A stays
    con.execute("CHECKPOINT")
    con.close()
    report(ws, primary_cause=GOOD_CAUSE, damage_window=[WIN_START, WIN_END])


def agent_both_nightly_only_hand_restate(root: Path):
    """GT006's ACCEPTED RESIDUAL, carried forward: A repaired, B's nightly
    loader repaired, mart restated by hand, backfill's copy of the rule left
    broken. Passes, and is listed so TASK VALID cannot be read as 'a half-fix
    of B cannot pass'. See agent_nightly_only_hand_restate."""
    agent_nightly_only_hand_restate(root)
    ws = root / "workspace"
    fix_model_left_join(ws)
    con = open_rw(root)
    rebuild_rollup_through_model(con, ws)
    con.execute("CHECKPOINT")
    con.close()
    report(ws, primary_cause=GOOD_CAUSE, damage_window=[WIN_START, WIN_END],
           additional_causes=[GOOD_CAUSE_A])


def agent_source_vandal(root: Path):
    """Makes the numbers tie by deleting the awkward rows from the source.

    raw.payments_landing is the processor's delivery and every prompt variant's
    ground rules make it read-only. This must score BELOW no_op, or "delete what
    does not reconcile" dominates careful work.
    """
    con = open_rw(root)
    con.execute("DELETE FROM raw.payments_landing WHERE terminal_ref IS NULL")
    con.execute(f"DELETE FROM raw.payments_landing WHERE terminal_ref IN "
                f"('{EXCL[0]}', '{EXCL[1]}')")
    con.execute("CHECKPOINT")
    con.close()
    report(root / "workspace",
           primary_cause="the source feed contained rows that could not be loaded",
           damage_window=[WIN_START, WIN_END])


def agent_collateral_vandal(root: Path):
    """Repairs correctly and destroys the operational record on the way past."""
    agent_data_only_patch(root)
    con = open_rw(root)
    con.execute("DELETE FROM ops.dq_checks")
    con.execute("DROP TABLE reporting.rpt_merchant_monthly")
    con.execute("CHECKPOINT")
    con.close()
    shutil.rmtree(root / "var" / "workflow", ignore_errors=True)


# Baselines that are EXPECTED to clear the pass criterion. `oracle` is the
# reference solution. `nightly_only_hand_restate` is a MEASURED, NAMED RESIDUAL:
# it passes, it is allowed to pass, and it is listed here so that TASK VALID
# cannot be read as a claim the suite does not make. See the docstring on its
# agent for the scope boundary and for what closing it would cost.
EXPECTED_TO_PASS = {"oracle", "both_nightly_only_hand_restate"}

# The `repairs` block each baseline must produce. THE INDEPENDENCE CLAIM, as
# data: A-only moves leave B untouched, B-only moves leave A untouched.
EXPECTED_REPAIRS = {
    "oracle": ("repaired", "repaired"),
    "no_op": ("untouched", "untouched"),
    "fix_A_only": ("repaired", "untouched"),
    "fix_A_by_completing_the_list": ("repaired", "untouched"),
    "fix_A_data_only": ("partial", "untouched"),
    "fix_B_only": ("untouched", "repaired"),
    "both_nightly_only_hand_restate": ("repaired", "repaired"),
    "nightly_only_hand_restate": ("untouched", "repaired"),
    "disable_the_rule": ("untouched", "partial"),
    "data_only_patch": ("untouched", "partial"),
    "code_only_fix": ("untouched", "partial"),
    "checklist": ("untouched", "untouched"),
}

AGENTS = {
    "oracle": (agent_oracle, "== max, passes; A=repaired B=repaired"),
    "no_op": (agent_no_op, "0 on every clause that pays; A=untouched B=untouched"),
    "fix_A_only": (agent_fix_A_only,
                   "THE 2x2 ADVERSARY: A1+A2 full, every B gate 0, FAILS; "
                   "A=repaired B=untouched"),
    "fix_A_by_completing_the_list": (
        agent_fix_A_by_completing_the_list,
        "the other repair of A passes A1+A2; fails on B"),
    "fix_A_data_only": (agent_fix_A_data_only,
                        "A-LIVENESS: rollup patched, model not; A2 == 0"),
    "fix_B_only": (agent_fix_B_only,
                   "MIRROR: every B gate full, A1+A2 0, FAILS; A=untouched B=repaired"),
    "both_nightly_only_hand_restate": (
        agent_both_nightly_only_hand_restate,
        "ACCEPTED RESIDUAL carried from GT006: passes with the backfill's copy "
        "of the rule still broken"),
    "checklist": (agent_checklist, "diagnosis only; H1 only; fails"),
    "disable_the_rule": (agent_disable_the_rule,
                         "ARCHETYPE ADVERSARY: < max, fails G1b and G1c"),
    "tighten_the_filter": (agent_tighten_the_filter,
                           "more validation makes it worse; fails"),
    "data_only_patch": (agent_data_only_patch,
                        "LIVENESS: data right, code wrong; must fail G1c"),
    "code_only_fix": (agent_code_only_fix, "code right, no restatement; fails"),
    "permanent_skip": (agent_permanent_skip,
                       "LIVENESS: 0 on G1c, NOT full marks"),
    "delete_the_loader": (agent_delete_the_loader, "LIVENESS: 0 on G1c"),
    "nightly_only": (agent_nightly_only, "one of two rule copies fixed; fails"),
    "nightly_only_hand_restate": (
        agent_nightly_only_hand_restate,
        "GT006's residual, B only: B full, A 0, FAILS here"),
    "source_vandal": (agent_source_vandal,
                      "no repair value at all; must score BELOW no_op"),
    "collateral_vandal": (agent_collateral_vandal,
                          "a correct repair minus the collateral it caused; fails"),
}


# ------------------------------------------------------------------- driving
def _env() -> dict:
    import os

    return {**os.environ, "PYTHONPATH": str(REPO / "world"), "PYTHONUTF8": "1"}


def build_template(tmp: Path) -> tuple[Path, Path]:
    root, grading = tmp / "template_world", tmp / "template_grading"
    r = subprocess.run(
        [PY, str(HERE / "build_world.py"), "--root", str(root),
         "--grading", str(grading), "--seed", str(CONTRACT["seed"])],
        capture_output=True, text=True, env=_env())
    if r.returncode != 0:
        raise SystemExit(f"world builder failed:\n{r.stdout}\n{r.stderr}")
    return root, grading


def fork(template_root: Path, template_grading: Path, tmp: Path,
         name: str) -> tuple[Path, Path]:
    root, grading = tmp / name / "world", tmp / name / "grading"
    shutil.copytree(template_root, root)
    shutil.copytree(template_grading, grading)
    return root, grading


def grade(root: Path, grading: Path) -> dict:
    r = subprocess.run(
        [PY, str(HERE / "grade.py"),
         "--world", str(root / CONTRACT["layout"]["deployed_world_relpath"]),
         "--grading", str(grading),
         "--artifact", str(root / "workspace" / "final_report.json"),
         "--workspace", str(root / "workspace"),
         "--world-root", str(root), "--json"],
        capture_output=True, text=True, env=_env())
    if r.returncode != 0:
        raise SystemExit(f"grader exited {r.returncode}:\n{r.stdout}\n{r.stderr}")
    return json.loads(r.stdout)


def by_id(score: dict) -> dict:
    return {c["id"]: c for c in score["checks"]}


# ------------------------------------------------------------- world properties
def check_green(root: Path) -> list[str]:
    """A027's contribution to this unit: every operational surface is healthy on
    the shipped world. If one goes red on its own, the unit has collapsed into
    ordinary triage and the difficulty is gone."""
    out = []
    con = open_rw(root)
    try:
        g = CONTRACT["green"]
        for label, sql, want in (
            ("failed job runs",
             "SELECT count(*) FROM ops.job_runs WHERE status <> 'success'", 0),
            ("freshness checks not PASS",
             "SELECT count(*) FROM ops.freshness_checks WHERE status <> 'PASS'", 0),
            ("dq checks not PASS",
             "SELECT count(*) FROM ops.dq_checks WHERE status <> 'PASS'", 0),
            ("dq failed rows",
             "SELECT coalesce(sum(failed_rows), 0) FROM ops.dq_checks", 0),
        ):
            got = con.execute(sql).fetchone()[0]
            if got != want:
                out.append(f"{label}: {got}, expected {want}")
        if g["job_runs"] != con.execute(
                "SELECT count(*) FROM ops.job_runs").fetchone()[0]:
            out.append("ops.job_runs row count disagrees with the contract")
    finally:
        con.close()
    state = json.loads((root / CONTRACT["layout"]["scheduler_relpath"])
                       .read_text(encoding="utf-8"))
    bad = [r for r in state.get("runs", []) if r.get("status") != "success"]
    if bad:
        out.append(f"{len(bad)} scheduler run(s) are not success")
    ingest = json.loads((root / CONTRACT["layout"]["ingest_relpath"])
                        .read_text(encoding="utf-8"))
    if ingest.get("alerts"):
        out.append(f"{len(ingest['alerts'])} ingestion alert(s) are open")
    bad = [r for r in ingest.get("runs", []) if r.get("status") != "success"]
    if bad:
        out.append(f"{len(bad)} ingestion run(s) are not success")
    return out


def check_planes(root: Path) -> list[str]:
    """A declared surface the world does not seed is not cosmetic: the harness
    verifies these live before handing the subject the terminal, and a world
    that does not carry them makes every rollout a harness_error."""
    out = []
    for label, rel in (("workflow", CONTRACT["layout"]["workflow_relpath"]),
                       ("obs definitions", CONTRACT["layout"]["obs_definitions_relpath"]),
                       ("obs samples", CONTRACT["layout"]["obs_samples_relpath"]),
                       ("ingest", CONTRACT["layout"]["ingest_relpath"]),
                       ("scheduler", CONTRACT["layout"]["scheduler_relpath"]),
                       ("dashboards", CONTRACT["layout"]["dashboards_relpath"]),
                       ("live", "var/world/live.json")):
        if not (root / rel).exists():
            out.append(f"{label} is not seeded at {rel}")
    for name in ("issues.json", "chat.json", "kb.json"):
        if not (root / CONTRACT["layout"]["workflow_relpath"] / name).is_file():
            out.append(f"var/workflow/{name} is missing")
    dash = (root / CONTRACT["layout"]["dashboards_relpath"] /
            f"{CONTRACT['layout']['dashboard_id']}.yml")
    if not dash.is_file():
        out.append(f"the dashboard {CONTRACT['layout']['dashboard_id']} is not seeded")
    return out


def check_no_leak(root: Path) -> list[str]:
    """No agent-visible file may name the answer. F125: a world that states the
    cause somewhere readable measures reading, not reasoning."""
    out = []
    forbidden = ("IS DISTINCT FROM", "NULL-unsafe", "null-unsafe",
                 "three-valued", "silently drop")
    for p in sorted((root / "workspace").rglob("*")):
        if not p.is_file() or ".git" in p.parts:
            continue
        try:
            body = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for token in forbidden:
            if token in body:
                out.append(f"workspace/{p.relative_to(root / 'workspace')} "
                           f"contains {token!r}")
    for name in ("issues.json", "chat.json", "kb.json"):
        p = root / CONTRACT["layout"]["workflow_relpath"] / name
        if not p.is_file():
            continue
        body = p.read_text(encoding="utf-8")
        for token in forbidden:
            if token in body:
                out.append(f"var/workflow/{name} contains {token!r}")
        if "authoring" in body:
            out.append(f"var/workflow/{name} still carries author-side notes")
    return out


# ------------------------------------------------------------------- verdicts
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default=str(TASK / "fixtures" / "build"))
    ap.add_argument("--only", help="comma-separated baseline names")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--keep", action="store_true", help="do not delete the scratch tree")
    a = ap.parse_args()

    problems: list[str] = []
    print(f"{TASK.name}  task-validity suite (stacked: defect A + defect B)")
    print("=" * 78)

    # 0. the fixture is the fixture the contract describes -------------------
    print("\n[0] fixture contract")
    try:
        verify(Path(a.fixtures), quiet=True)
        print("    verify(): OK")
    except FixtureDrift as exc:
        print(f"    verify(): DRIFT\n{exc}")
        problems.append("the fixture does not match the contract")

    tmp = Path(tempfile.mkdtemp(prefix="gt006-baselines-"))
    try:
        print("\n[1] building the template world")
        troot, tgrading = build_template(tmp)
        print(f"    world   {troot}")
        print(f"    grading {tgrading}")

        # 2. world properties -------------------------------------------------
        print("\n[2] world properties")
        for label, fn in (("every surface green", check_green),
                          ("every declared plane seeded", check_planes),
                          ("no answer leaked into the world", check_no_leak)):
            found = fn(troot)
            print(f"    {'OK  ' if not found else 'FAIL'} {label}")
            for f in found:
                print(f"         {f}")
            problems += found

        # F121, as a build gate rather than a test
        try:
            assert_reference_unreachable(troot, tgrading)
            print("    OK   the reference is unreachable from the world (F121)")
        except FixtureDrift as exc:
            print(f"    FAIL {exc}")
            problems.append("the reference is reachable from the world")

        # 3. baselines --------------------------------------------------------
        names = ([n.strip() for n in a.only.split(",")] if a.only
                 else list(AGENTS))
        print(f"\n[3] baselines ({len(names)})")
        results: dict[str, dict] = {}
        for name in names:
            fn, expectation = AGENTS[name]
            root, grading = fork(troot, tgrading, tmp, name)
            fn(root)
            score = results[name] = grade(root, grading)
            cells = "  ".join(
                f"{c['id']}={c['points']:g}" for c in score["checks"])
            rp = score.get("repairs", {})
            print(f"    {name:<30} {score['total']:>6.2f}/{score['max']:<5g} "
                  f"pass={str(score['passed']):<5} A={rp.get('A', '?'):<9} "
                  f"B={rp.get('B', '?'):<9}  {cells}")
            if a.verbose:
                for c in score["checks"]:
                    print(f"         {c['id']:<4} {c['reason']}")
            print(f"    {'':<30} expect: {expectation}")

        # 4. the claims the rubric makes -------------------------------------
        print("\n[4] rubric claims")
        oracle = results.get("oracle")
        if oracle:
            if oracle["total"] != oracle["max"] or not oracle["passed"]:
                problems.append(
                    f"the oracle scores {oracle['total']}/{oracle['max']} "
                    f"passed={oracle['passed']}; it must score max and pass")
            print(f"    {'OK  ' if oracle['total'] == oracle['max'] else 'FAIL'} "
                  f"oracle == max ({oracle['total']}/{oracle['max']}) "
                  f"and passes ({oracle['passed']})")

        noop = results.get("no_op")
        if noop:
            paid = [c for c in noop["checks"] if c["max"] > 0 and c["points"] != 0]
            if paid:
                problems += [
                    f"no_op was PAID {c['points']} on {c['id']}, a clause that "
                    f"pays. A clause an idle agent satisfies by construction is "
                    f"a subsidy, not a measurement." for c in paid]
            print(f"    {'OK  ' if not paid else 'FAIL'} every paying clause is "
                  f"0 for no_op (total {noop['total']})")
            if noop["passed"]:
                problems.append("no_op passes the criterion")

        unexpected = [n for n, s in results.items()
                      if s["passed"] and n not in EXPECTED_TO_PASS]
        for name in unexpected:
            problems.append(
                f"{name} clears the pass criterion and is not in "
                f"EXPECTED_TO_PASS; only a real repair may pass")
        print(f"    {'OK  ' if not unexpected else 'FAIL'} no baseline passes "
              f"except {sorted(EXPECTED_TO_PASS)}")
        residuals = sorted(EXPECTED_TO_PASS - {"oracle"} & set(results))
        for name in residuals:
            if name not in results:
                continue
            s_ = results[name]
            if not s_["passed"]:
                problems.append(
                    f"{name} is declared an accepted residual that PASSES and it "
                    f"scored {s_['total']} without passing; the residual has "
                    f"changed and the note describing it is now wrong")
            print(f"    NOTE accepted residual: {name} passes at "
                  f"{s_['total']}/{s_['max']} - see its docstring for the scope "
                  f"boundary this records")

        # THE INDEPENDENCE CLAIM. fix_A_only scores every B gate at zero and
        # fix_B_only scores every A gate at zero; both fail; and the repairs
        # block names each state. This is the property the 2x2 rests on.
        a_only, b_only = results.get("fix_A_only"), results.get("fix_B_only")
        if a_only:
            b_gates = [by_id(a_only)[i] for i in ("G1a", "G1c", "G2")]
            ok = (all(c["points"] == 0 for c in b_gates)
                  and by_id(a_only)["A1"]["passed"] and by_id(a_only)["A2"]["passed"]
                  and not a_only["passed"])
            if not ok:
                problems.append(
                    f"fix_A_only scores {a_only['total']} passed={a_only['passed']} "
                    f"with B gates {[c['points'] for c in b_gates]}; repairing A "
                    f"must pay nothing on B and must not pass")
            print(f"    {'OK  ' if ok else 'FAIL'} fix_A_only: A1+A2 full, "
                  f"G1a/G1c/G2 == 0, fails ({a_only['total']}/{a_only['max']})")
        if b_only:
            a_gates = [by_id(b_only)[i] for i in ("A1", "A2")]
            ok = (all(c["points"] == 0 for c in a_gates)
                  and all(by_id(b_only)[i]["passed"] for i in ("G1a", "G1b", "G1c", "G2"))
                  and not b_only["passed"])
            if not ok:
                problems.append(
                    f"fix_B_only scores {b_only['total']} passed={b_only['passed']} "
                    f"with A gates {[c['points'] for c in a_gates]}; repairing B "
                    f"must pay nothing on A and must not pass")
            print(f"    {'OK  ' if ok else 'FAIL'} fix_B_only: B gates full, "
                  f"A1/A2 == 0, fails ({b_only['total']}/{b_only['max']})")
        bad = []
        for name, want in EXPECTED_REPAIRS.items():
            got = results.get(name, {}).get("repairs")
            if got is None:
                continue
            if (got["A"], got["B"]) != want:
                bad.append(f"{name}: repairs A={got['A']} B={got['B']}, expected "
                           f"A={want[0]} B={want[1]}")
        problems += bad
        print(f"    {'OK  ' if not bad else 'FAIL'} the repairs block names "
              f"every baseline's state ({len(EXPECTED_REPAIRS)} asserted)")
        for b in bad:
            print(f"         {b}")

        # THE A-LIVENESS CLAIM
        s_ = results.get("fix_A_data_only")
        if s_:
            a2 = by_id(s_)["A2"]
            if a2["points"] != 0:
                problems.append(f"fix_A_data_only scored {a2['points']} on A2; a "
                                f"rollup patched under an unchanged model repairs "
                                f"nothing and must score ZERO")
            print(f"    {'OK  ' if a2['points'] == 0 else 'FAIL'} fix_A_data_only: "
                  f"A2 == 0 (A liveness)")

        # THE ARCHETYPE CLAIM
        adv = results.get("disable_the_rule")
        if adv:
            ok = (adv["total"] < oracle["total"] and not adv["passed"]
                  and not by_id(adv)["G1c"]["passed"]
                  and not by_id(adv)["G1b"]["passed"])
            if not ok:
                problems.append(
                    f"disable_the_rule scores {adv['total']} and passed="
                    f"{adv['passed']}; the archetype's own adversary is not "
                    f"being caught, which is the one thing this unit must do")
            print(f"    {'OK  ' if ok else 'FAIL'} disabling the rule does not "
                  f"score full marks ({adv['total']}/{adv['max']}), and fails "
                  f"G1b and G1c")

        # THE LIVENESS CLAIM
        for name in ("permanent_skip", "delete_the_loader", "data_only_patch"):
            s = results.get(name)
            if not s:
                continue
            g1c = by_id(s)["G1c"]
            if g1c["points"] != 0:
                problems.append(
                    f"{name} scored {g1c['points']} on G1c; a load that never "
                    f"runs, or a table patched under an unchanged pipeline, "
                    f"repairs nothing and must score ZERO")
            print(f"    {'OK  ' if g1c['points'] == 0 else 'FAIL'} {name}: "
                  f"G1c == 0 (liveness)")

        # PENALTIES MUST REACH BELOW DOING NOTHING.
        #
        # Stated precisely, because the loose version of this claim is wrong and
        # was measured to be wrong here: an agent that repairs correctly AND
        # vandalises on the way past SHOULD still outscore an idle one, because
        # it did the repair. What must hold is:
        #
        #   (a) a PURE vandal - one that produced no repair value at all -
        #       scores strictly below no_op, or "delete what does not reconcile"
        #       dominates careful work;
        #   (b) vandalism is never free: the same repair with collateral scores
        #       strictly below the same repair without it, and fails.
        pure = results.get("source_vandal")
        if pure and noop:
            below = pure["total"] < noop["total"]
            if not below:
                problems.append(
                    f"source_vandal scores {pure['total']}, not below no_op's "
                    f"{noop['total']}. It destroys the source feed and repairs "
                    f"nothing; without a penalty that can score below doing "
                    f"nothing, deleting the rows that do not reconcile dominates.")
            print(f"    {'OK  ' if below else 'FAIL'} source_vandal (no repair "
                  f"value at all) scores below no_op "
                  f"({pure['total']} < {noop['total']})")

        vand, clean = results.get("collateral_vandal"), results.get("data_only_patch")
        if vand and clean:
            g3 = by_id(vand)["G3"]
            ok = (vand["total"] < clean["total"] and g3["points"] < 0
                  and not g3["passed"] and not vand["passed"])
            if not ok:
                problems.append(
                    f"collateral_vandal scores {vand['total']} against the same "
                    f"repair's {clean['total']} with G3={g3['points']}; "
                    f"collateral must never be free")
            print(f"    {'OK  ' if ok else 'FAIL'} collateral is never free: the "
                  f"same repair costs {clean['total'] - vand['total']:g} points "
                  f"when it destroys things, and fails")

        # THE FLOOR CANNOT READMIT H1
        if oracle:
            required = [c for c in oracle["checks"] if c["in_pass_criterion"]]
            required_max = sum(c["max"] for c in required)
            floor = oracle["pass_min_total"]
            if floor > required_max:
                problems.append(
                    f"pass_min_total {floor} exceeds the {required_max} the "
                    f"required checks can score, so H1 is silently back in the "
                    f"pass criterion")
            print(f"    {'OK  ' if floor <= required_max else 'FAIL'} "
                  f"pass_min_total {floor} <= required-check max {required_max} "
                  f"(H1 cannot decide a pass)")

        # 5. the grader is stable ---------------------------------------------
        print("\n[5] grader stability")
        root, grading = fork(troot, tgrading, tmp, "_stability")
        agent_data_only_patch(root)
        first, second = grade(root, grading), grade(root, grading)
        stable = (first["total"] == second["total"]
                  and [c["points"] for c in first["checks"]]
                  == [c["points"] for c in second["checks"]])
        if not stable:
            problems.append("the grader's score moves on an unchanged world")
        print(f"    {'OK  ' if stable else 'FAIL'} grading twice gives the same "
              f"score ({first['total']} then {second['total']})")

    finally:
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"\n  scratch kept at {tmp}")

    print("\n" + "=" * 78)
    if problems:
        print(f"TASK INVALID - {len(problems)} problem(s):")
        for p in problems:
            print(f"  * {p}")
        return 1
    print("TASK VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
