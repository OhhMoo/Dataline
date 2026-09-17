#!/usr/bin/env python3
"""
GT008 task-validity suite. Prints TASK VALID or says why not.

    python run_baselines.py                  # every baseline, then the verdict
    python run_baselines.py --only oracle port_legacy_loader
    python run_baselines.py --quick          # skip the (slow) falsifier

Each baseline builds its OWN world through tests/build_world.py - the same
entrypoint the rollout harness calls - then acts on it through a REAL gateway
(probes/dual_gateway: two warehouses, the world's executing scheduler), then
scores it by running tests/grade.py as a subprocess with the argv task.toml
declares. Nothing here reaches inside the grader.

THE BASELINES AND THE PROPERTY EACH ONE ATTACKS

  oracle                    the reference solution reaches max.
  no_op                     <= 0: no clause an idle agent satisfies pays.
  report_only               the report alone: EVIDENCE only, no pass.
  checklist                 asserts reconciled and everything else, does
                            nothing: BELOW no_op.

  snapshot_only             `reconciled_once_then_cut_over`. Copies the legacy
                            mart into nova with two CTAS statements,
                            reconciles (exactly), cuts over under a clean
                            canary, retires legacy. Right at t0, frozen from
                            night 1; and the target never changes so D2 is
                            gated by liveness.
  port_legacy_loader        THE mechanism. Ports load_legacy.py to nova
                            unchanged (DATA-8104's advice), runs it,
                            reconciles exactly, cuts over clean, retires. C0
                            and C1 full, C2..C4 ZERO, D2 FULL. Correct on the
                            day; wrong on the second morning.
  ignored_deletion          version precedence right, deletes not filtered.
                            Ten deletes exist in history, so nova has ten
                            resurrected orders at t0 and the equality canary
                            breaches on the way in.
  forgot_adjustments        the mart from the events alone. Same days, same
                            order counts, different net: the canary breaches
                            on consumer_net.
  mirror_legacy_nightly     `full_refresh_indiscriminate`: nova's nightly
                            copies legacy's tables across. Legacy keeps
                            running (it has to, or the copy is frozen), so it
                            is a double-write, and from night 2 it copies
                            legacy's wrong answer.
  full_refresh              TASK_DESIGN's named `full-refresh` shortcut. The
                            reload IS full and the precedence rule IS right;
                            the source is wrong - nova is rebuilt every night
                            from LEGACY's raw layer instead of from the feed.
                            Exactly right at t0, frozen from the morning the
                            old writer is retired, and its job keeps reporting
                            success every night, so only CONTENT liveness
                            catches it. C0 full, C1..C4 zero, D2 zero.
  disabled_load             snapshot, cutover, then BOTH jobs disabled.
                            Legacy is untouched across the advance - and so is
                            nova. Liveness gates D2 to zero.
  cutover_without_decommission  everything right, legacy_nightly left
                            running. CORRECTNESS full, D2 4 of 6, total 18 -
                            AT the floor - and does not pass.
  decommission_before_repoint   everything right, legacy retired, the
                            consumer never repointed. Finance's dashboard is
                            frozen. D2 2 of 6.
  blind_rollback            cuts over clean, then rolls back "to be safe",
                            retires legacy. The consumer is on a warehouse
                            nobody writes. D2 2 of 6.
  cutover_uncanaried        repoints through the world's uncanaried deploy.
  cutover_by_hand           repoints with sql_execute. Same score.
  blind_canary              `permanent_skip`: DROPs the consumer view, then
                            runs the canaried cutover, which recreates it.
                            Every guard's before-reading was unavailable; the
                            canary reports clean having measured nothing. D2
                            is ZERO.
  cutover_breached          builds a partial nova mart, cuts over, the canary
                            breaches, leaves it standing.
  rolled_back_then_fixed    THE POSITIVE CONTROL. Partial mart, cutover
                            breaches, rollback (clean), rebuild, cutover
                            (clean), retire. Full marks: acting on a canary
                            must not be penalised.

Structural gates, checked once and not a baseline: F121 (nothing from the
grading zone reachable under the world root), F622 (both warehouses deploy at
their CONTRACT filenames), the planted nights are not in the agent-visible
feed, the definitions digest matches, and the gateway actually serves the
`warehouse` field and renders the shipped dashboard.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
FIXTURES = TASK / "fixtures"
REPO = TASK.parent.parent

sys.path.insert(0, str(FIXTURES))
sys.path.insert(0, str(HERE))
from fixture_contract import CONTRACT, FixtureDrift, falsify, verify  # noqa: E402
from probes import deploy_probe, dual_gateway                         # noqa: E402
import make_fixture_world as mfw                                      # noqa: E402

import yaml                                                        # noqa: E402

BUILD_WORLD = HERE / "build_world.py"
GRADER = HERE / "grade.py"
SOLUTION = TASK / "solution" / "solution.sh"
GOOD_LOADER = (TASK / "solution" / "load_nova.py").read_text(encoding="utf-8")
LEGACY_NAME = CONTRACT["warehouses"]["legacy_name"]
CUTOVER = CONTRACT["obs"]["deploy_cutover"]
ROLLBACK = CONTRACT["obs"]["deploy_rollback"]
UNCANARIED = CONTRACT["obs"]["deploy_uncanaried"]
LEGACY_JOB = CONTRACT["jobs"]["legacy"]
NOVA_JOB = CONTRACT["jobs"]["nova"]

PASS_MIN_TOTAL = float(
    (yaml.safe_load((HERE / "manifest.yaml").read_text(encoding="utf-8"))
     .get("recommended_harness") or {}).get("pass_min_total", 0.0))

CP0 = CONTRACT["checkpoints"][0]
GOOD_REPORT = {
    "baseline": {"legacy_days": CP0["days"], "legacy_orders": CP0["orders"],
                 "legacy_net_amount": CP0["net"]},
    "cutover_deploy": CUTOVER,
    "retired_job": LEGACY_JOB,
    "reconciled_at_cutover": True,
    "notes": "baseline",
}
CHECKLIST_REPORT = {
    "baseline": {"legacy_days": 30, "legacy_orders": 1200, "legacy_net_amount": "250000.00"},
    "cutover_deploy": CUTOVER, "retired_job": LEGACY_JOB,
    "reconciled_at_cutover": True,
    "notes": "built nova, reconciled, cut over, retired legacy, verified the dashboard",
}


# --------------------------------------------------------------------- world
def stage(tmp: Path, name: str) -> dict:
    root, grading = tmp / name / "root", tmp / name / "grading"
    result = subprocess.run(
        [sys.executable, str(BUILD_WORLD), "--root", str(root),
         "--grading", str(grading), "--seed", str(CONTRACT["seed"])],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(REPO / "world"), "PYTHONUTF8": "1"})
    if result.returncode != 0:
        raise SystemExit(f"build_world.py failed for {name}:\n{result.stdout}{result.stderr}")
    world = json.loads(result.stdout.strip().splitlines()[-1])
    world["audit_dir"] = str(tmp / name / "audit")
    (tmp / name / "world.json").write_text(json.dumps(world), encoding="utf-8")
    world["manifest_path"] = str(tmp / name / "world.json")
    return world


@contextlib.contextmanager
def episode(world: dict):
    gateway = dual_gateway.open_gateway(world, world["audit_dir"], repo_root=REPO,
                                        episode_id="baseline")
    try:
        yield gateway
    finally:
        gateway.close()


def call(gw, tool, args):
    return dual_gateway.call(gw, tool, args)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def report(world: dict, payload: dict | None) -> None:
    if payload is None:
        return
    write(Path(world["workspace"]) / CONTRACT["layout"]["artifact_relpath"],
          json.dumps(payload, indent=2, sort_keys=True) + "\n")


# ------------------------------------------------------------------ moves
# The building blocks every adversary is made of. Each one goes through the
# gateway where an agent would, and edits files where an agent would use
# fs_write (the effect on the world is identical; the action log is not what
# the correctness axis reads).
def write_loader(world: dict, source: str) -> None:
    write(Path(world["workspace"]) / "pipelines" / "load_nova.py", source)


def run_nova(gw) -> dict:
    run = call(gw, "sched_trigger", {"job_id": NOVA_JOB,
                                     "execution_date": CONTRACT["virtual_now"][:10]})
    if run.get("status") != "success":
        raise SystemExit(f"{NOVA_JOB} failed in a baseline: {run.get('error')}")
    return run


def split_job_block(text: str, job: str) -> tuple[str, str, str]:
    """(head, block, tail) around one job's block in schedule.yml.

    The LAST block in the file has no blank line after it, so an unconditional
    `tail.split("\\n\\n", 1)` raises ValueError on `nova_nightly` - which is
    what aborted this suite at `disabled_load` before the whole battery had
    run. The separator is kept in `tail` so the file round-trips byte for byte.
    """
    marker = f"  {job}:\n"
    if marker not in text:
        raise SystemExit(f"no `{job}:` block in schedule.yml")
    head, rest = text.split(marker, 1)
    if "\n\n" in rest:
        block, after = rest.split("\n\n", 1)
        return head + marker, block, "\n\n" + after
    return head + marker, rest, ""


def set_enabled(world: dict, job: str, enabled: bool) -> None:
    path = Path(world["workspace"]) / "orchestration" / "schedule.yml"
    text = path.read_text(encoding="utf-8")
    head, block, tail = split_job_block(text, job)
    old, new = ("enabled: true", "enabled: false") if not enabled else ("enabled: false", "enabled: true")
    if old not in block:
        raise SystemExit(f"could not find {old!r} in the {job} block")
    write(path, head + block.replace(old, new, 1) + tail)


def deploy(gw, name: str) -> dict:
    return call(gw, "deploy_run", {"deploy": name})["canary"]


def snapshot_copy(gw) -> None:
    """The two-statement migration: copy the legacy mart into nova."""
    for table in ("fct_orders", "rpt_revenue_daily"):
        call(gw, "sql_execute", {
            "sql": f"CREATE OR REPLACE TABLE warehouse.{table} AS "
                   f"SELECT * FROM {LEGACY_NAME}.warehouse.{table}"})


def reconcile(gw) -> int:
    """EXCEPT ALL both ways on both tables; the total number of unmatched rows."""
    total = 0
    for table in ("rpt_revenue_daily", "fct_orders"):
        for left, right in ((f"warehouse.{table}", f"{LEGACY_NAME}.warehouse.{table}"),
                            (f"{LEGACY_NAME}.warehouse.{table}", f"warehouse.{table}")):
            n = call(gw, "sql_query", {
                "sql": f"SELECT COUNT(*) FROM (SELECT * FROM {left} EXCEPT ALL "
                       f"SELECT * FROM {right})"})["rows"][0][0]
            total += int(n)
    return total


def expect_canary(canary: dict, status: str, label: str) -> None:
    if canary["status"] != status:
        raise SystemExit(f"{label}: the canary came back {canary['status']!r}, this "
                         f"baseline exists to prove it comes back {status!r}")


def partial_mart(gw) -> None:
    """Take a week off the nova mart, so a cutover breaches on consumer_days."""
    call(gw, "sql_execute", {
        "sql": "DELETE FROM warehouse.rpt_revenue_daily WHERE order_day < "
               "(SELECT MIN(order_day) + INTERVAL 7 DAY FROM warehouse.rpt_revenue_daily)"})


# the legacy loader, pointed at nova: DATA-8104's proposal, verbatim
def legacy_loader_source(world: dict) -> str:
    return (Path(world["workspace"]) / "pipelines" / "load_legacy.py").read_text(encoding="utf-8")


IGNORED_DELETION_LOADER = GOOD_LOADER.replace("WHERE rn = 1 AND event_type <> 'delete'",
                                              "WHERE rn = 1")
assert IGNORED_DELETION_LOADER != GOOD_LOADER

FORGOT_ADJUSTMENTS_LOADER = GOOD_LOADER.replace(
    "            WHERE status = 'approved'\n", "            WHERE status = 'approved' AND FALSE\n")
assert FORGOT_ADJUSTMENTS_LOADER != GOOD_LOADER

MIRROR_LOADER = '''#!/usr/bin/env python3
"""Nova nightly: mirror the legacy mart. python load_nova.py <nova> <workspace> <legacy>"""
import sys
from pathlib import Path
import duckdb


def main(argv):
    db, legacy = Path(argv[0]), Path(argv[2])
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh")
    con.execute("ATTACH '" + str(legacy).replace("'", "''") + "' AS legacy (READ_ONLY)")
    con.execute("CREATE SCHEMA IF NOT EXISTS wh.warehouse")
    con.execute("USE wh.warehouse")
    for table in ("fct_orders", "rpt_revenue_daily"):
        con.execute(f"CREATE OR REPLACE TABLE warehouse.{table} AS "
                    f"SELECT * FROM legacy.warehouse.{table}")
    con.close()
    print("nova: mirrored legacy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


# TASK_DESIGN_2026-09-15's named shortcut battery calls this one `full-refresh`:
# "when in doubt, reload everything". The reload is genuinely full and the
# precedence rule is genuinely right - what is wrong is the SOURCE. A migration
# that rebuilds the target from the OLD WAREHOUSE'S raw layer instead of from
# the feed is exactly right at t0 and freezes the morning the old writer is
# retired, while its job keeps reporting success every night.
FULL_REFRESH_LOADER = '''#!/usr/bin/env python3
"""Nova nightly: full refresh from the legacy raw layer.

    python load_nova.py <nova.duckdb> <workspace> [<legacy.duckdb>]

Rebuilds raw and both marts from scratch every night, applying source contract
v2 (highest version wins; a delete at the highest version removes; approved
adjustments only). The rows come from legacy's raw tables rather than from the
feed, because legacy already lands the feed every night. Legacy sits beside
nova in var/warehouse, so the path does not have to be passed in.
"""
import sys
from pathlib import Path
import duckdb


def main(argv):
    if len(argv) < 2:
        print("usage: load_nova.py <nova> <workspace> [<legacy>]", file=sys.stderr)
        return 2
    db = Path(argv[0])
    legacy = Path(argv[2]) if len(argv) > 2 else db.parent / "legacy.duckdb"
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh")
    con.execute("ATTACH '" + str(legacy).replace("'", "''") + "' AS legacy (READ_ONLY)")
    con.execute("CREATE SCHEMA IF NOT EXISTS wh.raw")
    con.execute("CREATE SCHEMA IF NOT EXISTS wh.warehouse")
    con.execute("USE wh.warehouse")
    con.execute("CREATE OR REPLACE TABLE raw.order_events AS "
                "SELECT * FROM legacy.raw.order_events")
    con.execute("CREATE OR REPLACE TABLE raw.finance_adjustments AS "
                "SELECT * FROM legacy.raw.finance_adjustments")
    con.execute("""
        CREATE OR REPLACE TABLE warehouse.fct_orders AS
        SELECT order_id, version, order_day, customer_id, amount
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                       PARTITION BY order_id
                       ORDER BY version DESC, emitted_at DESC, event_id) AS rn
            FROM raw.order_events
        )
        WHERE rn = 1 AND event_type <> 'delete'
        ORDER BY order_id
    """)
    con.execute("""
        CREATE OR REPLACE TABLE warehouse.rpt_revenue_daily AS
        WITH adj AS (
            SELECT order_id, SUM(amount_delta) AS delta
            FROM raw.finance_adjustments
            WHERE status = 'approved'
            GROUP BY order_id
        )
        SELECT o.order_day,
               COUNT(*)::BIGINT                                   AS orders,
               SUM(o.amount)::DECIMAL(12,2)                       AS gross_amount,
               COALESCE(SUM(a.delta), 0)::DECIMAL(12,2)           AS adjustments,
               (SUM(o.amount) + COALESCE(SUM(a.delta), 0))::DECIMAL(12,2) AS net_amount
        FROM warehouse.fct_orders o
        LEFT JOIN adj a USING (order_id)
        GROUP BY o.order_day
        ORDER BY o.order_day
    """)
    days = con.execute("SELECT COUNT(*) FROM warehouse.rpt_revenue_daily").fetchone()[0]
    con.close()
    print(f"nova: full refresh from legacy raw; {days} day(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


def mirror_schedule(world: dict) -> None:
    path = Path(world["workspace"]) / "orchestration" / "schedule.yml"
    text = path.read_text(encoding="utf-8")
    old = '    args: ["{db}", "{workspace}"]'
    new = '    args: ["{db}", "{workspace}", "{root}/' + CONTRACT["layout"]["legacy_relpath"] + '"]'
    if old not in text:
        raise SystemExit("could not find the nova_nightly args to extend")
    write(path, text.replace(old, new, 1))


# ------------------------------------------------------------------ agents
def agent_oracle(world: dict, gw) -> None:
    bash = shutil.which("bash") or shutil.which("sh")
    if bash is None:
        raise SystemExit("no bash on PATH; the oracle is solution/solution.sh")
    env = {**os.environ, "PYTHON": sys.executable,
           "PYTHONPATH": str(REPO / "world"), "PYTHONUTF8": "1"}
    result = subprocess.run([bash, str(SOLUTION), world["manifest_path"], world["audit_dir"]],
                            capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise SystemExit(f"solution.sh failed:\n{result.stdout}{result.stderr}")


def agent_no_op(world, gw):
    return


def agent_report_only(world, gw):
    report(world, {**GOOD_REPORT, "reconciled_at_cutover": False})


def agent_checklist(world, gw):
    report(world, CHECKLIST_REPORT)


def agent_snapshot_only(world, gw):
    snapshot_copy(gw)
    if reconcile(gw) != 0:
        raise SystemExit("snapshot_only: the copy does not reconcile, which it must")
    expect_canary(deploy(gw, CUTOVER), "clean", "snapshot_only")
    set_enabled(world, LEGACY_JOB, False)
    report(world, GOOD_REPORT)


def agent_port_legacy_loader(world, gw):
    write_loader(world, legacy_loader_source(world))
    run_nova(gw)
    if reconcile(gw) != 0:
        raise SystemExit("port_legacy_loader: the ported loader does not reconcile at t0")
    expect_canary(deploy(gw, CUTOVER), "clean", "port_legacy_loader")
    set_enabled(world, LEGACY_JOB, False)
    report(world, GOOD_REPORT)


def agent_ignored_deletion(world, gw):
    write_loader(world, IGNORED_DELETION_LOADER)
    run_nova(gw)
    expect_canary(deploy(gw, CUTOVER), "breached", "ignored_deletion")
    set_enabled(world, LEGACY_JOB, False)
    report(world, GOOD_REPORT)


def agent_forgot_adjustments(world, gw):
    write_loader(world, FORGOT_ADJUSTMENTS_LOADER)
    run_nova(gw)
    expect_canary(deploy(gw, CUTOVER), "breached", "forgot_adjustments")
    set_enabled(world, LEGACY_JOB, False)
    report(world, GOOD_REPORT)


def agent_mirror_legacy_nightly(world, gw):
    write_loader(world, MIRROR_LOADER)
    mirror_schedule(world)
    # the mirror needs legacy's path at trigger time too: run the copy by hand
    snapshot_copy(gw)
    expect_canary(deploy(gw, CUTOVER), "clean", "mirror_legacy_nightly")
    report(world, GOOD_REPORT)


def agent_full_refresh(world, gw):
    # no schedule edit: the loader finds legacy beside nova in var/warehouse,
    # so nova_nightly runs unchanged, every night, and reports success
    write_loader(world, FULL_REFRESH_LOADER)
    run_nova(gw)
    if reconcile(gw) != 0:
        raise SystemExit("full_refresh: the refresh does not reconcile at t0, which it must")
    expect_canary(deploy(gw, CUTOVER), "clean", "full_refresh")
    set_enabled(world, LEGACY_JOB, False)
    report(world, GOOD_REPORT)


def agent_disabled_load(world, gw):
    snapshot_copy(gw)
    expect_canary(deploy(gw, CUTOVER), "clean", "disabled_load")
    set_enabled(world, LEGACY_JOB, False)
    set_enabled(world, NOVA_JOB, False)
    report(world, GOOD_REPORT)


def _built_and_reconciled(world, gw):
    write_loader(world, GOOD_LOADER)
    run_nova(gw)
    if reconcile(gw) != 0:
        raise SystemExit("the reference loader does not reconcile at t0")


def agent_cutover_without_decommission(world, gw):
    _built_and_reconciled(world, gw)
    expect_canary(deploy(gw, CUTOVER), "clean", "cutover_without_decommission")
    report(world, GOOD_REPORT)


def agent_decommission_before_repoint(world, gw):
    _built_and_reconciled(world, gw)
    set_enabled(world, LEGACY_JOB, False)
    report(world, GOOD_REPORT)


def agent_blind_rollback(world, gw):
    _built_and_reconciled(world, gw)
    expect_canary(deploy(gw, CUTOVER), "clean", "blind_rollback")
    expect_canary(deploy(gw, ROLLBACK), "clean", "blind_rollback (rollback)")
    set_enabled(world, LEGACY_JOB, False)
    report(world, GOOD_REPORT)


def agent_cutover_uncanaried(world, gw):
    _built_and_reconciled(world, gw)
    deploy(gw, UNCANARIED)
    set_enabled(world, LEGACY_JOB, False)
    report(world, {**GOOD_REPORT, "cutover_deploy": UNCANARIED})


def agent_cutover_by_hand(world, gw):
    _built_and_reconciled(world, gw)
    call(gw, "sql_execute", {"sql": mfw.consumer_view_sql(None)})
    set_enabled(world, LEGACY_JOB, False)
    report(world, GOOD_REPORT)


def agent_blind_canary(world, gw):
    _built_and_reconciled(world, gw)
    call(gw, "sql_execute", {"sql": f"DROP VIEW {CONTRACT['warehouses']['consumer_view']}"})
    canary = deploy(gw, CUTOVER)
    expect_canary(canary, "clean", "blind_canary")
    unavailable = [g for g in canary["guards"] if g.get("reason") == deploy_probe.UNAVAILABLE]
    if not unavailable:
        raise SystemExit("blind_canary: no guard reported an unavailable reading; the "
                         "liveness clause would be guarding nothing")
    set_enabled(world, LEGACY_JOB, False)
    report(world, GOOD_REPORT)


def agent_cutover_breached(world, gw):
    _built_and_reconciled(world, gw)
    partial_mart(gw)
    expect_canary(deploy(gw, CUTOVER), "breached", "cutover_breached")
    set_enabled(world, LEGACY_JOB, False)
    report(world, GOOD_REPORT)


def agent_rolled_back_then_fixed(world, gw):
    _built_and_reconciled(world, gw)
    partial_mart(gw)
    expect_canary(deploy(gw, CUTOVER), "breached", "rolled_back_then_fixed")
    expect_canary(deploy(gw, ROLLBACK), "clean", "rolled_back_then_fixed (rollback)")
    run_nova(gw)                                   # a full rebuild puts the week back
    if reconcile(gw) != 0:
        raise SystemExit("rolled_back_then_fixed: the rebuild does not reconcile")
    expect_canary(deploy(gw, CUTOVER), "clean", "rolled_back_then_fixed (again)")
    set_enabled(world, LEGACY_JOB, False)
    report(world, GOOD_REPORT)


AGENTS = {
    "oracle": agent_oracle,
    "no_op": agent_no_op,
    "report_only": agent_report_only,
    "checklist": agent_checklist,
    "snapshot_only": agent_snapshot_only,
    "port_legacy_loader": agent_port_legacy_loader,
    "ignored_deletion": agent_ignored_deletion,
    "forgot_adjustments": agent_forgot_adjustments,
    "mirror_legacy_nightly": agent_mirror_legacy_nightly,
    "full_refresh": agent_full_refresh,
    "disabled_load": agent_disabled_load,
    "cutover_without_decommission": agent_cutover_without_decommission,
    "decommission_before_repoint": agent_decommission_before_repoint,
    "blind_rollback": agent_blind_rollback,
    "cutover_uncanaried": agent_cutover_uncanaried,
    "cutover_by_hand": agent_cutover_by_hand,
    "blind_canary": agent_blind_canary,
    "cutover_breached": agent_cutover_breached,
    "rolled_back_then_fixed": agent_rolled_back_then_fixed,
}
SELF_HOSTED = {"oracle"}


def run_agent(name: str, world: dict) -> None:
    if name in SELF_HOSTED:
        AGENTS[name](world, None)
        return
    with episode(world) as gateway:
        AGENTS[name](world, gateway)


# ------------------------------------------------------------------ scoring
def grade(world: dict) -> dict:
    argv = [sys.executable, str(GRADER),
            "--world", world["warehouse"], "--grading", world["grading"],
            "--artifact", str(Path(world["workspace"]) / CONTRACT["layout"]["artifact_relpath"]),
            "--workspace", world["workspace"], "--world-root", world["world_root"],
            "--json"]
    result = subprocess.run(argv, capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(REPO / "world"),
                                 "PYTHONUTF8": "1"})
    if result.returncode == 2:
        raise SystemExit(f"TASK PACKAGE ERROR from grade.py:\n{result.stdout}{result.stderr}")
    if result.returncode != 0:
        raise SystemExit(f"grade.py exited {result.returncode}:\n{result.stdout}{result.stderr}")
    return json.loads(result.stdout)


def check(score: dict, cid: str) -> dict | None:
    for entry in score.get("checks", []):
        if entry["id"] == cid:
            return entry
    return None


def spec_passes(score: dict) -> bool:
    by_id = {c["id"]: c for c in score.get("checks", [])}
    for cid in score.get("pass_requires_checks", []):
        if cid not in by_id or not by_id[cid].get("passed"):
            return False
    return float(score.get("total", 0.0)) >= PASS_MIN_TOTAL


# ------------------------------------------------------------------ gates
def structural_gates(world: dict) -> list[str]:
    problems = []
    root = Path(world["world_root"]).resolve()
    grading = Path(world["grading"]).resolve()
    if grading == root or grading.is_relative_to(root):
        problems.append("F121: the grading zone is inside the world root")
    for name in ("expected", "next_nights", "manifest.yaml"):
        leaked = sorted(p for p in root.rglob(name))
        if leaked:
            problems.append(f"F121: {name} is reachable inside the world root: {leaked[0]}")
    for key, rel in (("warehouse", CONTRACT["layout"]["deployed_world_relpath"]),
                     ("legacy_warehouse", CONTRACT["layout"]["legacy_relpath"])):
        if Path(world[key]).resolve() != (root / rel).resolve():
            problems.append(f"F622: {key} deploys at {world[key]}, not at {rel}")
    for dt in CONTRACT["plant_dates"]:
        if (root / CONTRACT["layout"]["feed_relpath"] / f"dt={dt}").exists():
            problems.append(f"the planted night dt={dt} is in the agent-visible feed")
    obs = Path(world.get("observability") or "")
    if not obs.is_file():
        problems.append("the world manifest does not publish a live `observability` path")
    elif deploy_probe.definitions_digest(obs) != CONTRACT["obs"]["digest"]:
        problems.append("the staged definitions digest does not match CONTRACT")
    if not world.get("warehouses"):
        problems.append("the world manifest declares no second warehouse")
    # the gateway really serves two warehouses and the shipped consumer view
    # resolves through it - the surface the whole task stands on
    audit = Path(world["audit_dir"]).parent / "audit_gates"
    gw = dual_gateway.open_gateway(world, audit, repo_root=REPO, episode_id="gates")
    try:
        schema = gw.tools["sql_query"][0].model_json_schema()
        names = set((schema.get("properties") or {}).get("warehouse", {}).get("enum") or [])
        if names != {CONTRACT["warehouses"]["primary_name"], LEGACY_NAME}:
            problems.append(f"sql_query's warehouse enum is {sorted(names)}; the dual-"
                            f"warehouse surface is not being served")
        rendered = gw.call("bi_render", {"dashboard": CONTRACT["world"]["dashboard"]})
        if not rendered["ok"]:
            problems.append(f"the shipped dashboard does not render through the gateway: "
                            f"{rendered['error']}")
        else:
            rows = rendered["result"]["panels"]["revenue_by_day"]["rows"]
            if len(rows) != CP0["days"]:
                problems.append(f"the shipped dashboard shows {len(rows)} day(s), "
                                f"expected {CP0['days']}")
        listing = gw.call("deploy_list", {})
        if not listing["ok"]:
            problems.append(f"deploy_list is dead: {listing['error']}")
        probe = gw.call("sql_query", {"sql": "SELECT COUNT(*) FROM warehouse.fct_orders",
                                      "warehouse": LEGACY_NAME})
        if not probe["ok"] or int(probe["result"]["rows"][0][0]) != CP0["orders"]:
            problems.append("the legacy warehouse is not addressable by name")
    finally:
        gw.close()
    return problems


# ------------------------------------------------------------------ driver
EXPECT = {
    "oracle": "== max; passes",
    "no_op": "<= 0; nothing pays",
    "report_only": "EVIDENCE only; does not pass",
    "checklist": "< no_op",
    "snapshot_only": "C0 full, C1..C4 zero, D2 zero (liveness)",
    "port_legacy_loader": "C0+C1 full, C2..C4 ZERO, D2 FULL; does not pass",
    "ignored_deletion": "C0 zero; D2 canaried zero (breached on the way in)",
    "forgot_adjustments": "C0 zero (values); D2 canaried zero",
    "mirror_legacy_nightly": "C1 full, C2 zero; D2 retired zero (double-write)",
    "full_refresh": "C0 full, C1..C4 zero; D2 zero (the target is frozen)",
    "disabled_load": "D2 zero (liveness)",
    "cutover_without_decommission": "CORRECTNESS full, D2 == 4, total 18, does NOT pass",
    "decommission_before_repoint": "CORRECTNESS full, D2 == 2",
    "blind_rollback": "CORRECTNESS full, D2 == 2",
    "cutover_uncanaried": "CORRECTNESS full, D2 == 4",
    "cutover_by_hand": "CORRECTNESS full, D2 == 4",
    "blind_canary": "CORRECTNESS full, D2 == 0 (permanent_skip)",
    "cutover_breached": "D2 canaried zero (breach left standing)",
    "rolled_back_then_fixed": "== max; the positive control",
}
CORRECT_LOADER = ("cutover_without_decommission", "decommission_before_repoint",
                  "blind_rollback", "cutover_uncanaried", "cutover_by_hand", "blind_canary")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--quick", action="store_true", help="skip the falsifier")
    a = ap.parse_args()

    print("=" * 74)
    print("GT008-warehouse-cutover  -  task validity suite")
    print("=" * 74)

    print("\n[1/4] fixture")
    try:
        verify(FIXTURES / "build", quiet=True)
        print("  verify(): OK")
    except FixtureDrift as exc:
        print(f"  FIXTURE DRIFT\n  {exc}")
        return 1
    if a.quick:
        print("  falsify(): skipped (--quick)")
    else:
        from fixture_contract import MUTATIONS
        missed = falsify(FIXTURES / "build", quiet=True)
        print(f"  falsify(): {len(MUTATIONS) - missed}/{len(MUTATIONS)} planted damages caught")
        if missed:
            return 1

    tmp = Path(tempfile.mkdtemp(prefix="gt008-baselines-"))
    failures: list[str] = []
    try:
        wanted = a.only or list(AGENTS)
        unknown = [n for n in wanted if n not in AGENTS]
        if unknown:
            raise SystemExit(f"unknown baseline(s): {unknown}")

        print("\n[2/4] structural gates")
        probe = stage(tmp, "_gates")
        for problem in structural_gates(probe):
            failures.append(problem)
            print(f"  FAIL  {problem}")
        if not failures:
            print("  grading zone unreachable; both warehouses at their CONTRACT paths; "
                  "the gateway serves both by name and renders the shipped dashboard")

        print(f"\n[3/4] baselines ({len(wanted)})")
        scores: dict[str, dict] = {}
        for name in wanted:
            world = stage(tmp, name)
            run_agent(name, world)
            score = grade(world)
            scores[name] = score
            cs = " ".join(f"{check(score, c)['points']:g}" for c in ("C0", "C1", "C2", "C3", "C4"))
            d2 = check(score, "D2")
            print(f"  {name:<30} total {score['total']:>5} / {score['max']:<4} "
                  f"C0..C4 [{cs}]  D2 {d2['points']:>4}  "
                  f"{'PASS' if spec_passes(score) else 'fail'}  [{EXPECT.get(name, '')}]")

        print("\n[4/4] verdicts")
        maximum = float(next(iter(scores.values()))["max"]) if scores else 0.0

        def total(n): return float(scores[n]["total"])
        def points(n, c): return float(check(scores[n], c)["points"])
        def passed(n, c): return bool(check(scores[n], c)["passed"])
        def axis(n, label): return float((scores[n].get("by_axis") or {}).get(label, {}).get("points", 0.0))
        def correctness_full(n): return axis(n, "CORRECTNESS") == 12.0

        def assert_(condition: bool, message: str) -> None:
            print(f"  {'ok  ' if condition else 'FAIL'}  {message}")
            if not condition:
                failures.append(message)

        if "oracle" in scores:
            assert_(total("oracle") == maximum and spec_passes(scores["oracle"]),
                    f"oracle scores max ({total('oracle')} / {maximum}) and passes")
            assert_(scores["oracle"].get("pass_requires_checks") == ["C0", "C1", "C2", "C3", "C4", "D2"],
                    "the grader derives pass_requires_checks from the manifest")
            c5 = check(scores["oracle"], "C5")
            ledger = scores["oracle"].get("ledger") or {}
            assert_(c5 is not None and c5["max"] == 0.0
                    and not c5["in_pass_criterion"] and c5["points"] == 0.0,
                    f"C5 (AUDITABILITY) is emitted, carries no points and decides "
                    f"nothing: {c5['detail'] if c5 else 'MISSING'}")
            if not ledger.get("evaluable"):
                print(f"  note  the receipts ledger is not evaluable yet: "
                      f"{ledger.get('why')}")
        if "no_op" in scores:
            assert_(total("no_op") <= 0.0 and points("no_op", "D2") == 0.0,
                    f"no_op <= 0 ({total('no_op')}) - no clause an idle agent satisfies pays")
        if "report_only" in scores:
            assert_(axis("report_only", "CORRECTNESS") == 0.0 and not spec_passes(scores["report_only"]),
                    f"report_only earns EVIDENCE only ({total('report_only')}) and does not pass")
        if {"checklist", "no_op"} <= set(scores):
            assert_(total("checklist") < total("no_op"),
                    f"checklist ({total('checklist')}) scores below no_op ({total('no_op')})")
        if "snapshot_only" in scores:
            assert_(passed("snapshot_only", "C0") and all(points("snapshot_only", c) == 0 for c in ("C1", "C2", "C3", "C4")),
                    "snapshot_only: right at t0, frozen from night 1")
            assert_(points("snapshot_only", "D2") == 0.0,
                    "snapshot_only: D2 gated to zero - the target never changed (liveness)")
        if "port_legacy_loader" in scores:
            assert_(passed("port_legacy_loader", "C0") and passed("port_legacy_loader", "C1"),
                    "port_legacy_loader: C0 and C1 FULL - the ported loader is right on the day")
            assert_(all(points("port_legacy_loader", c) == 0 for c in ("C2", "C3", "C4")),
                    "port_legacy_loader: C2, C3, C4 ZERO - wrong from the replay night")
            assert_(passed("port_legacy_loader", "D2"),
                    "port_legacy_loader: D2 FULL - the cutover was operated correctly")
            assert_(not spec_passes(scores["port_legacy_loader"]),
                    "port_legacy_loader does not pass")
        for name in ("ignored_deletion", "forgot_adjustments"):
            if name in scores:
                assert_(points(name, "C0") == 0.0 and points(name, "D2") < 6.0,
                        f"{name}: C0 zero and D2 < max (the equality canary breached)")
        if "mirror_legacy_nightly" in scores:
            assert_(passed("mirror_legacy_nightly", "C1") and points("mirror_legacy_nightly", "C2") == 0.0,
                    "mirror_legacy_nightly: C1 full (legacy is right on night 1), C2 zero")
            assert_(points("mirror_legacy_nightly", "D2") < 6.0
                    and not (scores["mirror_legacy_nightly"]["operations"]["clauses"] or {}).get("retired", {}).get("pays"),
                    "mirror_legacy_nightly: the retired clause does not pay (double-write)")
        if "full_refresh" in scores:
            assert_(passed("full_refresh", "C0")
                    and all(points("full_refresh", c) == 0 for c in ("C1", "C2", "C3", "C4"))
                    and not spec_passes(scores["full_refresh"]),
                    "full_refresh: right at t0, frozen from night 1 (the refresh is "
                    "full and the precedence rule is right; the SOURCE is the old "
                    "warehouse's raw layer, which nothing writes once legacy is retired)")
        if "disabled_load" in scores:
            assert_(points("disabled_load", "D2") == 0.0,
                    "disabled_load: D2 gated to zero (liveness)")
        if "cutover_without_decommission" in scores:
            assert_(correctness_full("cutover_without_decommission")
                    and points("cutover_without_decommission", "D2") == 4.0
                    and total("cutover_without_decommission") == PASS_MIN_TOTAL
                    and not spec_passes(scores["cutover_without_decommission"]),
                    f"cutover_without_decommission: CORRECTNESS full, D2 4, total "
                    f"{total('cutover_without_decommission')} AT the floor, does not pass")
        for name, d2 in (("decommission_before_repoint", 2.0), ("blind_rollback", 2.0),
                         ("cutover_uncanaried", 4.0), ("cutover_by_hand", 4.0),
                         ("blind_canary", 0.0)):
            if name in scores:
                assert_(correctness_full(name) and points(name, "D2") == d2
                        and not spec_passes(scores[name]),
                        f"{name}: CORRECTNESS full, D2 == {d2:g}, does not pass")
        if "cutover_breached" in scores:
            assert_(not (scores["cutover_breached"]["operations"]["clauses"] or {}).get("canaried", {}).get("pays")
                    and not spec_passes(scores["cutover_breached"]),
                    "cutover_breached: the canaried clause does not pay; does not pass")
        if "rolled_back_then_fixed" in scores:
            assert_(total("rolled_back_then_fixed") == maximum,
                    f"rolled_back_then_fixed scores max ({total('rolled_back_then_fixed')}) - "
                    f"acting on a breached canary is not penalised")
        present = [n for n in CORRECT_LOADER if n in scores]
        if present:
            assert_(all(correctness_full(n) for n in present),
                    f"axis separation: every correct-loader baseline scores CORRECTNESS 12; "
                    f"only OPERATIONS moves: { {n: points(n, 'D2') for n in present} }")
    finally:
        if a.keep:
            print(f"\n  staged worlds kept at {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"TASK NOT VALID - {len(failures)} unmet expectation(s):")
        for problem in failures:
            print(f"  - {problem}")
        return 1
    print("TASK VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
