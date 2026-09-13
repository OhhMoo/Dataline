#!/usr/bin/env python3
"""
Three-track integration test: Track A world + Track B gateway + Track C task.

Each track passes its own tests in isolation. This is the check nobody owned,
and it is where the real defects were: a DuckDB catalog/schema name collision
that only reproduces when the warehouse file is deployed under the name
SEED_SPEC mandates, invisible to every track working alone.

    python test_integration.py

Requires the gateway venv (it has mcp/pydantic) and track_a on PYTHONPATH.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
GATEWAY = REPO / "gateway"
TRACK_A = REPO / "track_a"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PY = GATEWAY / ".venv" / "Scripts" / "python.exe"
if not PY.exists():                                   # posix venv layout
    PY = GATEWAY / ".venv" / "bin" / "python"

WIN_START, WIN_END = "2026-02-01", "2026-03-02"
OK, BAD = [], []


def check(label: str, cond: bool, detail: str = "") -> None:
    (OK if cond else BAD).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def build_world(root: Path) -> None:
    """Assemble the GT001 world in the layout SEED_SPEC freezes."""
    for d in ("workspace/dbt_project/models/warehouse", "var/warehouse",
              "var/sched", "var/bi/dashboards", "build"):
        (root / d).mkdir(parents=True, exist_ok=True)

    built = FIXTURES / "build" / "world.duckdb"
    if not built.exists():
        subprocess.run([str(PY), str(FIXTURES / "make_fixture_world.py"),
                        "--out", str(FIXTURES / "build"), "--seed", "1337"],
                       check=True, capture_output=True)
    # deployed under the CONTRACT name - this is what exposes the collision
    shutil.copy(built, root / "var" / "warehouse" / "warehouse.duckdb")
    shutil.copy(FIXTURES / "workspace/dbt_project/models/warehouse/fct_orders.sql",
                root / "workspace/dbt_project/models/warehouse/fct_orders.sql")
    sys.path.insert(0, str(TRACK_A))
    from world import scheduler as ta_sched
    state = ta_sched.initial_state({"vtime_start": "2025-09-05T00:00:00Z", "days": 181})
    # the retry that caused the incident, in Track A's own run shape
    state["runs"].append({
        "job": "daily_load", "vts_start": "2026-03-03T04:12:00Z",
        "vts_end": "2026-03-03T04:12:45Z", "status": "success",
        "detail": "retry-20260303-0412 backfill dt=2026-02-01..2026-03-02"})
    ta_sched.save(root, state)
    (root / "var/bi/dashboards/commercial_daily.yml").write_text(
        "dashboard: commercial_daily\npanels:\n  - name: aov_daily\n"
        "    sql: \"SELECT dt, order_count, revenue, aov FROM warehouse.kpi_daily"
        " ORDER BY dt\"\n", encoding="utf-8")


def main() -> int:
    scratch = Path(os.environ.get("TMPDIR", "/tmp")) / "gt001-integration"
    shutil.rmtree(scratch, ignore_errors=True)
    world, audit = scratch / "world", scratch / "audit"
    audit.mkdir(parents=True)
    build_world(world)
    print(f"\n  world: {world}\n")

    # --- Track A -----------------------------------------------------------
    sys.path.insert(0, str(TRACK_A))
    from world.bi import render
    from world.fingerprint import semantic_hash

    fp1 = semantic_hash(world)
    check("Track A: semantic fingerprint computes", bool(fp1), fp1[:24])
    check("Track A: fingerprint is deterministic", semantic_hash(world) == fp1)

    panels = render(world)["commercial_daily"]["aov_daily"]
    by_dt = {r[0]: r for r in panels}
    pre, inw = by_dt["2026-01-15"], by_dt["2026-02-15"]
    ratio = float(inw[3]) / float(pre[3])
    check("Track A: BI renders the symptom", 0.45 < ratio < 0.55,
          f"AOV {pre[3]} -> {inw[3]} (ratio {ratio:.3f})")
    check("Track A: denominator doubled, numerator flat",
          int(inw[1]) > 1.8 * int(pre[1]) and 0.9 < float(inw[2]) / float(pre[2]) < 1.1,
          f"orders {pre[1]}->{inw[1]}, revenue {pre[2]}->{inw[2]}")

    # --- Track B -----------------------------------------------------------
    cfg = scratch / "gateway.json"
    cfg.write_text(json.dumps({
        "workspace": str(world / "workspace"),
        "warehouse": str(world / "var/warehouse/warehouse.duckdb"),
        "scheduler": str(world / "var/sched/state.json"),
        "dashboards": str(world / "var/bi/dashboards"),
        "audit_dir": str(audit),
        "profiles_dir": str(world / "build"),
        "world_root": str(world),
        "episode_id": "gt001-integration",
        "initial_world_fp": None,
        "virtual_time": "2026-03-05T09:00:00Z",
    }), encoding="utf-8")

    port = free_port()
    env = {**os.environ, "PYTHONPATH": str(TRACK_A)}
    log = (scratch / "server.log").open("w")
    server = subprocess.Popen(
        [str(PY), "-m", "alkera_gateway.cli", "serve", "--config", str(cfg),
         "--port", str(port)], cwd=str(GATEWAY), env=env, stdout=log, stderr=log)
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(40):
            time.sleep(0.5)
            try:
                socket.create_connection(("127.0.0.1", port), 0.5).close()
                break
            except OSError:
                if server.poll() is not None:
                    break
        alive = server.poll() is None
        check("Track B: gateway starts against the Track A world", alive,
              "" if alive else (scratch / "server.log").read_text()[-300:])
        if not alive:
            return 1

        def call(tool, **args):
            cmd = [str(PY), "-m", "alkera_gateway.cli", "call", tool, "--url", url]
            for k, v in args.items():
                cmd += ["--arg", f"{k}={v}"]
            r = subprocess.run(cmd, cwd=str(GATEWAY), capture_output=True, text=True)
            try:
                return json.loads(r.stdout)
            except json.JSONDecodeError:
                return {"error": (r.stdout + r.stderr)[-300:]}

        q = call("sql_query", sql=(
            "SELECT dt, order_count, aov FROM warehouse.kpi_daily "
            f"WHERE dt IN (DATE '2026-01-15', DATE '2026-02-15') ORDER BY dt"))
        rows = (q.get("result") or {}).get("rows")
        check("Track B: sql_query reads the symptom through the gateway",
              bool(rows) and len(rows) == 2, str(rows) if rows else str(q)[:200])

        dup = call("sql_query", sql=(
            "SELECT COUNT(*) AS n FROM (SELECT order_id, line_nbr "
            "FROM warehouse.fct_orders GROUP BY 1,2 HAVING COUNT(*) > 1)"))
        drows = (dup.get("result") or {}).get("rows")
        check("Track B: the planted duplication is discoverable",
              bool(drows) and drows[0][0] == 59995, str(drows))

        destructive = call("sql_execute", sql="DELETE FROM warehouse.fct_orders")
        executed = (destructive.get("ok") is True)

        sched = call("sched_state")
        check("Track B: scheduler surface reads Track A state",
              "retry-20260303-0412" in json.dumps(sched),
              json.dumps(sched.get("error"))[:120] if sched.get("error") else "")

        actions = audit / "actions.jsonl"
        lines = [json.loads(x) for x in
                 actions.read_text(encoding="utf-8").splitlines() if x.strip()]

        # op_class lives in the audit log, not the response - the log is the
        # artefact the grader and the process signals actually consume.
        dest_rows = [r for r in lines if r["tool"] == "sql_execute"]
        check("Track B: destructive SQL is classified, and still executed",
              bool(dest_rows) and dest_rows[0]["op_class"] == "destructive" and executed,
              f"op_class={dest_rows[0]['op_class'] if dest_rows else 'none'}, "
              f"executed={executed}")
        check("Track B: every call landed in the audit log", len(lines) >= 4,
              f"{len(lines)} rows")
        fields = {"seq", "tool", "args_hash", "op_class", "exit"}
        check("Track B: audit rows carry the contract fields",
              all(fields <= set(r) for r in lines))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()

    # --- Track C -----------------------------------------------------------
    fp2 = semantic_hash(world)
    check("Track A+B: the destructive call actually changed the world",
          fp2 != fp1, "fingerprint moved, as it should after a DELETE")

    print(f"\n  {len(OK)} passed, {len(BAD)} failed")
    if BAD:
        print("  FAILED: " + ", ".join(BAD))
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
