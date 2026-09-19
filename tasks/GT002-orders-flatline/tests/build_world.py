#!/usr/bin/env python3
"""
GT002 episode-world builder - the task-declared entrypoint the harness calls.

The rollout harness (tasks/harness/) is task-agnostic. It knows only that a task
declares `[harness] world_builder` in task.toml, invokes it with
--root/--grading/--seed, and reads ONE json object off stdout describing the
world it produced in the agent.md §9.2.1 layout. Everything GT002-specific
lives here.

This assembles deliberately the SAME world the baseline suite validates: the
warehouse is deployed through `warehouse_conn.deployed_copy`, and the workspace
and scheduler come from `fixtures/workspace_files.py`. If the harness built a
world by any other route, it would be measuring something no baseline covers -
which is exactly how GT001 shipped an oracle that aborted on its first
statement while printing TASK VALID (F622).

    python build_world.py --prepare-only                  # build the shared fixture once
    python build_world.py --root <dir> --grading <dir> --seed 2029
    python build_world.py --root <dir> --grading <dir> --variant decision

Contract with the harness (stdout, one json object):

    {"world_root", "workspace", "warehouse", "scheduler", "dashboards",
     "profiles_dir", "grading", "virtual_time", "seed", "variant"}

ARMS (DECISION_TEMPLATE.md section 1). `--variant` (or ALKERA_TASK_VARIANT)
selects the arm; `taskspec.fixtures_dir` resolves it to the staged fixtures
tree. The control arm (`default`) is the world above, byte for byte. The
`decision` arm is the same world handed to `decision_arm.stage` - present in
the staged tree only when the overlay is applied - which adds the owner, the
boundary and the consumer job (DESIGN.md section 2) BEFORE the workspace is
committed, so the arm's runbook section is in the repo's history like
everything else.

THE GRADING ZONE carries, on every arm, `variant.json` (which arm, which role)
and `pre_repair.json` (the shipped state of everything X1, F1 and A1 compare
against - file digests, the topic metadata, the archive group's offset row),
and on the decision arm `decision_truth.json` (the closed-week partition
fingerprints, the held/open expectations, the board pack's reference reading
and the seeded workflow digest).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parent.parent
FIXTURES = HERE / "fixtures"
BUILD = FIXTURES / "build"

# Files X1 reads as "irrelevant state": nothing about the incident needs them
# to change. Workspace-relative, except the dashboard, which is root-relative.
IRRELEVANT_FILES = [
    "workspace/dbt_project/models/warehouse/kpi_orders_daily.sql",
    "workspace/docs/data_dictionary.md",
    "workspace/asset_platform/asset_runs.json",
    "var/bi/dashboards/exec_revenue.yml",
]
SINK_FILE = "workspace/pipelines/orders_sink.sql"

SEED = 2029
VIRTUAL_TIME = "2026-03-13T09:00:00Z"
GIT_AUTHOR = "Data Platform <data@example.com>"
# The workspace is committed BEFORE the virtual now: the repo is a thing that
# existed before the incident, not an artifact of the episode starting.
GIT_DATE = "2026-03-12T02:00:00+00:00"

# Declared as a `bi_render` surface probe in task.toml and defined nowhere, so
# the probe failed against every world the harness could build. The dashboard is
# the consumer-facing surface the ticket's symptom is reported from, so its
# absence was not cosmetic - it removed the surface the episode starts on.
DASHBOARD = """dashboard: exec_revenue
panels:
  - name: daily_orders_by_channel
    sql: "SELECT dt, channel_group, order_count, order_value
          FROM warehouse.kpi_orders_daily ORDER BY dt, channel_group"
  - name: daily_totals
    sql: "SELECT dt, SUM(order_count) AS order_count, SUM(order_value) AS order_value
          FROM warehouse.kpi_orders_daily GROUP BY dt ORDER BY dt"
"""


def prepare(seed: int = SEED) -> None:
    """Build the shared, cached fixture world once, then verify it.

    Rollouts copy from here. Building per rollout would be slow AND would put K
    generator processes in a race over one output directory - which is how a
    stale generator silently overwrote a validated expected.duckdb before (see
    fixture_contract.py's own docstring).
    """
    BUILD.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(HERE))
    from filelock import FileLock
    from fixture_contract import verify as verify_fixture

    with FileLock(str(BUILD.parent / "fixture.lock"), timeout=600):
        if (not (BUILD / "world.duckdb").exists()
                or not (BUILD / "expected.duckdb").exists()):
            subprocess.run(
                [sys.executable, str(FIXTURES / "make_fixture_world.py"),
                 "--out", str(BUILD), "--seed", str(seed)],
                check=True, capture_output=True)
        verify_fixture(BUILD, quiet=True)      # raises FixtureDrift on drift


def _git(workspace: Path) -> None:
    """A real git repo, so git_log / git_diff / git_commit are live surfaces.

    Fixed author and dates: a commit sha that depends on the wall clock gives
    two rollouts of the same seed two different worlds.
    """
    m = re.match(r"(.*)<(.*)>", GIT_AUTHOR)
    env = {**os.environ, "GIT_AUTHOR_DATE": GIT_DATE, "GIT_COMMITTER_DATE": GIT_DATE}

    def run(*args):
        subprocess.run(["git", *args], cwd=workspace, env=env, check=True,
                       capture_output=True)

    run("init", "-q")
    run("config", "user.name", m.group(1).strip())
    run("config", "user.email", m.group(2).strip())
    run("config", "commit.gpgsign", "false")
    run("add", "-A")
    run("commit", "-q", "--no-verify", "-m", f"world seed {SEED}")


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def resolve_arm(requested: str | None) -> tuple[str, str, Path]:
    """(arm name, role, staged fixtures dir) via the harness's own resolver, so
    the world the builder stages is the one task.toml declares for the arm."""
    sys.path.insert(0, str(REPO / "tasks" / "harness"))
    try:
        import taskspec
    finally:
        sys.path.pop(0)
    variant = taskspec.variant_of(TASK, requested)
    fx = taskspec.fixtures_dir(TASK, requested)
    name = variant.name or "default"
    role = getattr(variant, "role", None) or "control"
    return name, role, fx


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pre_repair_snapshot(root: Path) -> dict:
    """The shipped state X1 / F1 / A1 compare the graded world against."""
    sys.path.insert(0, str(HERE))
    from warehouse_conn import open_warehouse
    con = open_warehouse(root / "var" / "warehouse" / "warehouse.duckdb", read_only=True)
    try:
        topic_meta = [list(map(str, r)) for r in con.execute(
            "SELECT * FROM raw.topic_meta ORDER BY 1").fetchall()]
        archive = [list(map(str, r)) for r in con.execute(
            "SELECT consumer_group, topic, committed_offset, committed_at FROM "
            "raw.consumer_offsets WHERE consumer_group <> 'orders_fct_sink' "
            "ORDER BY 1").fetchall()]
        committed = con.execute(
            "SELECT committed_offset FROM raw.consumer_offsets "
            "WHERE consumer_group = 'orders_fct_sink'").fetchone()[0]
        fct_rows = con.execute("SELECT count(*) FROM warehouse.fct_orders").fetchone()[0]
        dlq_rows = con.execute("SELECT count(*) FROM raw.orders_dlq").fetchone()[0]
        dim_rows = con.execute("SELECT count(*) FROM warehouse.dim_channel").fetchone()[0]
    finally:
        con.close()
    workspace = root / "workspace"
    files = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*")
                   if p.is_file() and ".git" not in p.parts)
    return {
        "irrelevant_files": {rel: _sha(root / rel) for rel in IRRELEVANT_FILES},
        "sink_sha256": _sha(root / SINK_FILE),
        "workspace_files": files,
        "topic_meta": topic_meta,
        "other_consumer_offsets": archive,
        "committed_offset": int(committed),
        "fct_rows": int(fct_rows),
        "dlq_rows": int(dlq_rows),
        "dim_channel_rows": int(dim_rows),
    }


def build(root: Path, grading: Path, seed: int = SEED,
          variant: str | None = None) -> dict:
    root, grading = Path(root).resolve(), Path(grading).resolve()
    if grading.is_relative_to(root):
        # ground rule 1: the grading zone is never reachable from the episode
        raise SystemExit("grading zone must not live inside the world root")
    prepare(seed)
    arm, role, fx = resolve_arm(variant)

    sys.path.insert(0, str(HERE))
    from warehouse_conn import deployed_copy
    sys.path.insert(0, str(FIXTURES))
    import workspace_files

    for d in ("var/sched", "var/bi/dashboards", "build"):
        (root / d).mkdir(parents=True, exist_ok=True)

    # Deployed at the CONTRACT filename via the task's own helper. Only
    # `warehouse.*` collides with the catalog named after the file stem, which is
    # why this defect hides from any suite that stages a differently-named file.
    warehouse = deployed_copy(BUILD / "world.duckdb", root)

    workspace_files.write_workspace(root / "workspace")
    workspace_files.write_scheduler(root / "var/sched/state.json")
    (root / "var/bi/dashboards/exec_revenue.yml").write_text(
        DASHBOARD, encoding="utf-8")

    grading.mkdir(parents=True, exist_ok=True)
    shutil.copy(BUILD / "expected.duckdb", grading / "expected.duckdb")
    shutil.copy(HERE / "manifest.yaml", grading / "manifest.yaml")
    truth_path = BUILD / "harness_truth.json"
    shutil.copy(truth_path, grading / "harness_truth.json")
    truth = json.loads(truth_path.read_text(encoding="utf-8"))

    # The arm's staging, if the staged tree carries one. Runs BEFORE the commit
    # so the arm's runbook section is part of the repo, and BEFORE the
    # pre-repair snapshot so X1 compares against the arm's own shipped state.
    staged = None
    arm_module = fx / "decision_arm.py"
    if arm_module.is_file():
        sys.path.insert(0, str(REPO / "world"))          # world.workflow, world.hashing
        import consumer
        arm_mod = _load_module(arm_module, "gt002_decision_arm")
        staged = arm_mod.stage(root, grading, truth, consumer)
        (grading / "decision_truth.json").write_text(
            json.dumps(staged, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    _git(root / "workspace")

    (grading / "variant.json").write_text(json.dumps({
        "variant": arm, "role": role, "staged": bool(staged),
        "fixtures": str(fx)}, indent=2) + "\n", encoding="utf-8")
    (grading / "pre_repair.json").write_text(
        json.dumps(pre_repair_snapshot(root), indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    return {
        "world_root": str(root),
        "workspace": str(root / "workspace"),
        "warehouse": str(warehouse),
        "scheduler": str(root / "var/sched/state.json"),
        "dashboards": str(root / "var/bi/dashboards"),
        "profiles_dir": str(root / "build"),
        "grading": str(grading),
        "virtual_time": VIRTUAL_TIME,
        "seed": seed,
        "variant": arm,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--grading")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--variant", default=None,
                    help="arm to stage (default: ALKERA_TASK_VARIANT, else `default`)")
    ap.add_argument("--prepare-only", action="store_true")
    a = ap.parse_args()
    if a.prepare_only:
        prepare(a.seed)
        print(json.dumps({"prepared": str(BUILD)}))
        return 0
    if not a.root or not a.grading:
        ap.error("--root and --grading are required unless --prepare-only")
    print(json.dumps(build(Path(a.root), Path(a.grading), a.seed, a.variant)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
