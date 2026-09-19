#!/usr/bin/env python3
"""
Deployed-path regression test. This is the check GT001 did not have.

GT001 printed TASK VALID for a day while its own oracle aborted on statement one
of four. Nothing caught it because `run_baselines.py` copied the world to
`tmp/world.duckdb`, and a file stem that is not "warehouse" does not collide with
the schema of the same name. Every test passed against a file that does not exist
in production.

So this test refuses to take the harness's word for anything:

  1. the hazard is REAL on this DuckDB build - if a direct connect stops raising,
     say so loudly rather than quietly passing a test that no longer tests
  2. the shipped pipeline SQL fails for the RIGHT reason at the contract path
     (Conversion Error - the poison record), not a Binder Error
  3. the shipped solution.sh runs end to end at the contract path and the shipped
     grader scores it at max
  4. run_baselines.fresh() still deploys at the contract path - the structural
     guard, so nobody can quietly reintroduce a convenient file stem

    python test_deployed_path.py

Run by run_baselines.py before any baseline, so TASK VALID cannot be printed
without it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from warehouse_conn import CONTRACT_RELPATH, deployed_copy, open_warehouse  # noqa: E402

FIXTURES = HERE / "fixtures" / "build"
WORKSPACE = HERE / "fixtures" / "workspace"
SOLUTION = HERE.parent / "solution" / "solution.sh"


class DeployedPathFailure(Exception):
    pass


def check(fixtures: Path = FIXTURES, quiet: bool = False) -> None:
    problems: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="gt002-deployed-"))
    try:
        world = deployed_copy(fixtures / "world.duckdb", tmp)
        if world.as_posix()[-len(CONTRACT_RELPATH.as_posix()):] != CONTRACT_RELPATH.as_posix():
            problems.append(f"deployed_copy did not use the contract path: {world}")

        # ---- 1. the hazard is real on this build -------------------------
        raw = duckdb.connect(str(world))
        try:
            raw.execute("SELECT COUNT(*) FROM warehouse.fct_orders").fetchone()
            problems.append(
                "a DIRECT connect to warehouse.duckdb no longer raises on "
                "warehouse.fct_orders. Either DuckDB changed or the world was "
                "renamed. This test is no longer testing anything - re-derive it "
                "before trusting any other result.")
        except duckdb.Error as exc:
            if "Ambiguous reference" not in str(exc):
                problems.append(f"direct connect failed for an unexpected reason: "
                                f"{str(exc).splitlines()[0][:110]}")
        finally:
            raw.close()

        # ---- 2. the pipeline fails for the RIGHT reason ------------------
        sink = (WORKSPACE / "pipelines" / "orders_sink.sql").read_text(encoding="utf-8")
        probe = deployed_copy(fixtures / "world.duckdb", tmp / "p2")
        con = open_warehouse(probe)
        try:
            con.execute(sink)
            problems.append("the shipped sink does not raise at the contract path - "
                            "defect A is absent from the deployed world")
        except duckdb.Error as exc:
            msg = str(exc).splitlines()[0]
            if "Ambiguous reference" in msg:
                problems.append(
                    "the shipped pipeline SQL raises a BINDER error at the contract "
                    "path, not the conversion error it is supposed to. The world's "
                    "own pipeline cannot run where the world is deployed.")
            elif "Conversion Error" not in msg:
                problems.append(f"the sink raised the wrong error: {msg[:110]}")
        finally:
            con.close()

        # ---- 3. the oracle runs end to end AT THE CONTRACT PATH ----------
        # The world the rollout harness builds (tests/build_world.py, control
        # arm): the contract path, the grading zone beside it with the
        # pre-repair snapshot the reform's checks read.
        import build_world
        info = build_world.build(tmp / "w" / "root", tmp / "w" / "grading",
                                 variant="default")
        world, root = Path(info["warehouse"]), Path(info["workspace"])
        grading = Path(info["grading"])
        env = {**os.environ, "WAREHOUSE": world.as_posix(), "WORKSPACE": root.as_posix()}
        bash = shutil.which("bash") or "bash"
        r = subprocess.run([bash, SOLUTION.as_posix()], capture_output=True,
                           text=True, env=env)
        report = root / "final_report.json"
        if r.returncode != 0:
            problems.append("solution.sh FAILED at the deployed path "
                            f"(exit {r.returncode}):\n      "
                            + (r.stdout + r.stderr).strip().splitlines()[-1][:200]
                            if (r.stdout + r.stderr).strip() else "")
        elif not report.exists():
            problems.append("solution.sh exited 0 but never wrote final_report.json")
        else:
            # ---- 4. and the shipped grader scores it at max --------------
            g = subprocess.run(
                [sys.executable, str(HERE / "grade.py"), "--world", str(world),
                 "--grading", str(grading), "--artifact", str(report),
                 "--world-root", str(root), "--json"],
                capture_output=True, text=True)
            if g.returncode != 0:
                problems.append(f"grader failed on the oracle's world: {g.stderr[-200:]}")
            else:
                res = json.loads(g.stdout)
                if res["total"] != res["max"]:
                    failed = [c["id"] for c in res["checks"] if not c["passed"]]
                    problems.append(
                        f"oracle scored {res['total']}/{res['max']} at the deployed "
                        f"path (failing {failed}). A task whose own solution does not "
                        "score max is misconfigured.")

        # ---- 5. structural guard on the baseline harness -----------------
        import run_baselines
        with tempfile.TemporaryDirectory() as td:
            w, _root = run_baselines.fresh(fixtures, Path(td))
            if w.parent.name != "warehouse" or w.name != "warehouse.duckdb":
                problems.append(
                    f"run_baselines.fresh() deploys to {w}, not the contract path. "
                    "Baselines run against a non-colliding file stem prove nothing "
                    "about the deployed world - this is the exact hole that hid "
                    "GT001's broken oracle.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if problems:
        raise DeployedPathFailure(
            "DEPLOYED-PATH CHECK FAILED\n\n  " + "\n  ".join(problems)
            + "\n\n  The task is not shippable: it has only been tested against a "
              "file name that does not exist in production.")
    if not quiet:
        print("  deployed-path check passed (oracle scores max at "
              "var/warehouse/warehouse.duckdb; the collision is real and handled)")


def main() -> int:
    try:
        check()
    except DeployedPathFailure as e:
        print(str(e))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
