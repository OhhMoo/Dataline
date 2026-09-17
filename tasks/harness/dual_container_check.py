#!/usr/bin/env python3
"""Stand up a DUAL-WAREHOUSE episode inside the container and drive it. No model.

    python dual_container_check.py [--image alkera-episode:head] [--no-postgres]

gateway/SURFACES.md#dual-warehouse's containment section is an argument from
the compose topology; this is the measurement. It runs the same lifecycle a
`--topology container` rollout runs - the world built on the host by a task-
shaped builder, `compose up`, the containment probe, the toolbelt and surface
probes read from inside, a real MCP session over `docker exec -i` - for a world
that declares three warehouses:

  primary   the theme's DuckDB warehouse, /world/var/warehouse/warehouse.duckdb
  legacy    a second DuckDB file, read-only, attached into the primary's catalog
  target    a third DuckDB file, writable, a dbt target
  pg        (unless --no-postgres) the compose `postgres` service, on
            backend_net, attached into the primary's catalog through the
            DuckDB `postgres` extension the image bakes in

`pg` cannot be seeded by the host builder - the server has no published port
and the host is not on backend_net - so it is seeded from INSIDE the episode
container through `world.warehouse_seed.seed_postgres` (bulk COPY, never the
connector), from the same CSVs the legacy file was seeded from, and its
logical hash is composed into `initial_world_fp` there too, since the gateway
refuses a remote warehouse without one.

What is then asked, through the subject's own channel, and recorded:

  * every declared name answers `sql_query(warehouse=<name>)`;
  * the attached peers join the primary in ONE statement (DuckDB<->DuckDB and
    DuckDB<->Postgres), and the legacy fact agrees with the primary's;
  * a write to `legacy` is refused by name (read-only), a write to `target`
    and to `pg` by name succeeds, and `UPDATE legacy...` through the attached
    catalog is refused by the engine;
  * `ATTACH` of an undeclared file is refused at the tool layer, and at the
    engine when the tool layer is bypassed with bash + python;
  * `dbt_run(warehouse="target")` builds the marts in the target file and
    nowhere else;
  * the episode zone still has no egress and no path to the grading zone
    (the same containment probe every rollout runs), and every row of the
    action log validates with a `warehouse` name on every sql row.

Evidence is written to evidence/dual-container-check.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import container_episode as ce     # noqa: E402
import episode                     # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
EVIDENCE = HERE / "evidence"
BUILDER = REPO / "world" / "scripts" / "dual_world_builder.py"
VIRTUAL_TIME = None  # the builder publishes the world clock

# Runs INSIDE the episode container, where backend_net is reachable. Seeds the
# Postgres peer from the CSVs the host builder wrote for `legacy`, and prints
# the fingerprint material the gateway config needs.
SEED_PG = r"""
import json, os, sys
from pathlib import Path
from world import warehouse_seed
from world.fingerprint import semantic_hash
from world.hashing import json_hash
dsn = os.environ["ALKERA_POSTGRES_DSN"]
seed_dir = Path("/world/build/warehouse_seed/legacy")
tables = {
    "raw.daily_revenue": (seed_dir / "daily_revenue.csv",
        {"order_id": "INTEGER", "order_date": "DATE", "customer_id": "VARCHAR",
         "amount": "DECIMAL(12,2)"}),
    "warehouse.fct_revenue": (seed_dir / "fct_revenue.csv",
        {"order_date": "DATE", "orders": "INTEGER", "revenue": "DECIMAL(14,2)"}),
}
warehouse_seed.seed_postgres(dsn, tables)
pg = warehouse_seed.postgres_logical(dsn)
legacy = warehouse_seed.duckdb_logical(Path("/world/var/warehouse/legacy.duckdb"))
tree = semantic_hash(Path("/world"))
print(json.dumps({
    "pg_hash": pg["hash"], "pg_tables": pg["tables"],
    "legacy_hash": legacy["hash"], "legacy_tables": legacy["tables"],
    "tree_hash": tree,
    "initial_world_fp": json_hash({"tree": tree, "remote": {"pg": pg["hash"]}}),
    # per table: the stack's Postgres also carries `warehouse.items` from
    # docker/postgres.sql, which the legacy file does not, so the top-level
    # hashes differ by that one table and the seeded tables are what is compared
    "same_data_same_hash": all(pg["tables"].get(t) == h for t, h in legacy["tables"].items()),
    "pg_extra_tables": sorted(set(pg["tables"]) - set(legacy["tables"])),
}))
"""

# The engine refusal, with the tool layer removed: a shell, python, and the
# real file. Run through the subject's own bash tool.
ENGINE_ATTACH = (
    "python -c \"import duckdb; c = duckdb.connect(':memory:', config={'autoinstall_known_extensions': False, "
    "'autoload_known_extensions': False}); "
    "c.execute(\\\"ATTACH '/world/var/warehouse/warehouse.duckdb' AS wh (READ_ONLY)\\\"); "
    "c.execute('SET enable_external_access = false'); c.execute('SET lock_configuration = true');\n"
    "exec('try:\\n    c.execute(\\\"ATTACH \\\\'/world/var/warehouse/target.duckdb\\\\' AS sneaky\\\"); print(\\\"ATTACH-OK\\\")\\n"
    "except Exception as e:\\n    print(\\\"REFUSED:\\\", str(e).splitlines()[0])')\""
)


async def mcp_session(argv: list[str], with_postgres: bool) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=argv[0], args=argv[1:], env=None)
    out: dict = {"argv": argv, "calls": []}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            listed = await session.list_tools()
            out["server_name"] = init.serverInfo.name
            schemas = {t.name: t.inputSchema for t in listed.tools}
            out["tool_count"] = len(schemas)
            out["bash_present"] = "bash" in schemas
            out["sql_query_warehouse_enum"] = (
                schemas["sql_query"]["properties"].get("warehouse", {}).get("enum"))
            out["dbt_run_warehouse_enum"] = (
                schemas["dbt_run"]["properties"].get("warehouse", {}).get("enum"))
            out["fs_read_has_no_warehouse"] = "warehouse" not in schemas["fs_read"]["properties"]

            async def call(label, tool, args):
                response = await session.call_tool(tool, args)
                payload = response.structuredContent or {}
                row = {"label": label, "tool": tool, "args": args,
                       "ok": bool(payload.get("ok")),
                       "error": (payload.get("error") or {}).get("code"),
                       "message": ((payload.get("error") or {}).get("message") or "")[:200]}
                result = payload.get("result") or {}
                if isinstance(result, dict):
                    if "rows" in result:
                        row["rows"] = result["rows"][:6]
                    if "returncode" in result:
                        row["returncode"] = result["returncode"]
                        row["output_tail"] = (result.get("output") or "")[-300:]
                    if "exit_code" in result:
                        row["exit_code"] = result["exit_code"]
                        row["output"] = (result.get("output") or "")[:400]
                out["calls"].append(row)
                return row

            names = ["primary", "legacy", "target"] + (["pg"] if with_postgres else [])
            for name in names:
                await call(f"count by name: {name}", "sql_query",
                           {"sql": "SELECT count(*) AS n FROM raw.daily_revenue",
                            "warehouse": name})
            await call("legacy fact agrees with primary, one statement (duckdb<->duckdb)",
                       "sql_query",
                       {"sql": "SELECT count(*) AS agreeing FROM wh.warehouse.fct_revenue p "
                               "JOIN legacy.warehouse.fct_revenue l USING (order_date) "
                               "WHERE p.revenue = l.revenue AND p.orders = l.orders"})
            await call("rows only one side has (FULL OUTER JOIN across the attachment)",
                       "sql_query",
                       {"sql": "SELECT count(*) AS n FROM wh.warehouse.fct_revenue p "
                               "FULL OUTER JOIN target.raw.daily_revenue t "
                               "ON p.order_date = t.order_date WHERE p.order_date IS NULL"})
            if with_postgres:
                await call("postgres fact agrees with primary, one statement (duckdb<->postgres)",
                           "sql_query",
                           {"sql": "SELECT count(*) AS agreeing FROM wh.warehouse.fct_revenue p "
                                   "JOIN pg.warehouse.fct_revenue r USING (order_date) "
                                   "WHERE p.revenue = r.revenue"})
                await call("three catalogs in one statement", "sql_query",
                           {"sql": "SELECT count(*) AS n FROM legacy.warehouse.fct_revenue l "
                                   "JOIN pg.warehouse.fct_revenue r USING (order_date) "
                                   "JOIN wh.warehouse.fct_revenue p USING (order_date)"})
            await call("write to legacy BY NAME is refused (read-only)", "sql_execute",
                       {"sql": "DELETE FROM warehouse.fct_revenue", "warehouse": "legacy"})
            await call("write to legacy THROUGH THE ATTACHMENT is refused (engine)",
                       "sql_execute", {"sql": "DELETE FROM legacy.warehouse.fct_revenue"})
            await call("write to target by name succeeds", "sql_execute",
                       {"sql": "CREATE TABLE warehouse.cutover_log(note VARCHAR); "
                               "INSERT INTO warehouse.cutover_log VALUES ('dual check')",
                        "warehouse": "target"})
            if with_postgres:
                await call("write to pg by name succeeds", "sql_execute",
                           {"sql": "UPDATE warehouse.fct_revenue SET orders = orders WHERE FALSE",
                            "warehouse": "pg"})
            await call("ATTACH an undeclared file: refused at the tool layer", "sql_execute",
                       {"sql": "ATTACH '/world/var/warehouse/target.duckdb' AS sneaky"})
            await call("an undeclared warehouse name: refused", "sql_query",
                       {"sql": "SELECT 1", "warehouse": "sneaky"})
            await call("dbt builds the PRIMARY (no warehouse argument)", "dbt_run",
                       {"selector": "*", "variables": {}, "full_refresh": False})
            await call("dbt builds the TARGET by name", "dbt_run",
                       {"selector": "*", "variables": {}, "full_refresh": False,
                        "warehouse": "target"})
            await call("...and the target now has the fact", "sql_query",
                       {"sql": "SELECT count(*) AS n FROM warehouse.fct_revenue",
                        "warehouse": "target"})
            await call("...built from the target's own raw layer, agreeing with legacy",
                       "sql_query",
                       {"sql": "SELECT count(*) AS agreeing FROM target.warehouse.fct_revenue t "
                               "JOIN legacy.warehouse.fct_revenue l USING (order_date) "
                               "WHERE t.revenue = l.revenue"})
            await call("dbt aimed at legacy (not a dbt target): refused", "dbt_run",
                       {"selector": "*", "variables": {}, "full_refresh": False,
                        "warehouse": "legacy"})
            await call("engine refusal with the tool layer bypassed (bash + python)", "bash",
                       {"command": ENGINE_ATTACH})
            await call("egress from the shell", "bash",
                       {"command": "getent hosts extensions.duckdb.org && echo DNS-OK; "
                                   "curl -sS --max-time 5 https://extensions.duckdb.org/ "
                                   "2>&1 | tail -1"})
            await call("INSTALL from the shell (no egress)", "bash",
                       {"command": "python -c \"import duckdb; duckdb.connect().execute("
                                   "'INSTALL spatial')\" 2>&1 | tail -1"})
    return out


def host_fingerprint(world_root: Path) -> str | None:
    """Track A's semantic fingerprint, with THIS interpreter: episode.fingerprint
    reaches for gateway/.venv, which a checkout under test need not have."""
    done = subprocess.run(
        [sys.executable, "-m", "world.fingerprint", str(world_root)],
        capture_output=True, text=True, cwd=str(REPO / "world"),
        env={**os.environ, "PYTHONPATH": str(REPO / "world"), "PYTHONUTF8": "1"})
    return done.stdout.strip() or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=ce.DEFAULT_IMAGE)
    ap.add_argument("--project", default=f"alkera-dual-{int(time.time())}")
    ap.add_argument("--no-postgres", dest="postgres", action="store_false")
    ap.add_argument("--keep-up", action="store_true")
    ap.add_argument("--out", type=Path, default=EVIDENCE / "dual-container-check.json")
    args = ap.parse_args()

    ok, version = ce.docker_available()
    if not ok:
        print(json.dumps({"docker": False, "detail": version}, indent=2))
        return 2

    scratch = Path(tempfile.gettempdir()) / "alkera-dual-check" / str(int(time.time()))
    shutil.rmtree(scratch, ignore_errors=True)
    world_root, grading = scratch / "world", scratch / "grading"
    evidence: dict = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "docker_server": version, "image": args.image,
        "builder": str(BUILDER), "with_postgres": args.postgres,
    }

    print("  building the dual-warehouse world on the host ...")
    argv = [sys.executable, str(BUILDER), "--root", str(world_root),
            "--grading", str(grading), "--seed", "1"]
    if args.postgres:
        argv += ["--postgres", "pg"]
    done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          env={**os.environ, "PYTHONPATH": str(REPO / "world"),
                               "PYTHONUTF8": "1"})
    if done.returncode:
        raise SystemExit(f"builder failed:\n{done.stdout}\n{done.stderr}")
    world = json.loads(done.stdout.strip().splitlines()[-1])
    evidence["world_manifest"] = {k: v for k, v in world.items() if k != "grading"}
    evidence["host_fingerprint_before"] = host_fingerprint(world_root)

    stack = ce.ContainerEpisode(project=args.project, world_root=world_root,
                                audit_dir=scratch / "audit", image=args.image)
    try:
        print(f"  compose up -d --wait  (project {args.project}) ...")
        stack.up()
        evidence["services"] = stack.services
        evidence["image_digest"] = stack.image_digest

        if args.postgres:
            print("  seeding the postgres peer from INSIDE the container (backend_net) ...")
            seeded = stack.exec_json(["python", "-"], stdin_text=SEED_PG, timeout=600)
            evidence["postgres_seed"] = seeded
            world["initial_world_fp"] = seeded["initial_world_fp"]
            print(f"     pg logical hash {seeded['pg_hash'][:12]} == legacy "
                  f"{seeded['legacy_hash'][:12]}: {seeded['same_data_same_hash']}")

        cfg = ce.container_gateway_config(world, "dual-check", world["virtual_time"])
        probes = [
            {"tool": "sql_query", "label": "primary",
             "args": {"sql": "SELECT count(*) AS n FROM warehouse.fct_revenue"}},
            {"tool": "sql_query", "label": "legacy",
             "args": {"sql": "SELECT count(*) AS n FROM warehouse.fct_revenue",
                      "warehouse": "legacy"}},
            {"tool": "sql_query", "label": "target",
             "args": {"sql": "SELECT count(*) AS n FROM raw.daily_revenue",
                      "warehouse": "target"}},
            {"tool": "dbt_compile", "label": "dbt_compile(target)",
             "args": {"selector": "*", "variables": {}, "warehouse": "target"}},
        ]
        if args.postgres:
            probes.append({"tool": "sql_query", "label": "pg",
                           "args": {"sql": "SELECT count(*) AS n FROM warehouse.fct_revenue",
                                    "warehouse": "pg"}})
        stack.prepare(cfg, probes, scratch / "prep")
        evidence["gateway_config_in_container"] = json.loads(
            (scratch / "prep" / "container_gateway.json").read_text(encoding="utf-8"))
        evidence["dbt_profiles_rerooted"] = stack.profiles_rewritten
        print(f"     dbt profiles re-rooted for the container: {stack.profiles_rewritten}")

        print("  containment probe, from inside the container ...")
        evidence["containment"] = stack.containment_probe(
            forbidden_files=[f"{d}/manifest.yaml" for d in ce.FORBIDDEN_DIRS]
            + [str(grading / "manifest.yaml").replace("\\", "/")],
            host_paths=[str(world_root).replace("\\", "/"),
                        str(grading).replace("\\", "/"),
                        str(REPO).replace("\\", "/"), "/c", "/host_mnt", "/mnt/c"],
            repo_root=REPO)
        print(f"     contained={evidence['containment']['contained']} "
              f"egress_blocked={evidence['containment']['egress_blocked']}")

        print("  toolbelt and surface probes, inside the container ...")
        evidence["toolbelt"] = sorted(stack.toolbelt())
        evidence["surfaces"] = stack.probe_surfaces()
        for label, surface in evidence["surfaces"]["surfaces"].items():
            print(f"     {label}: {'live' if surface['live'] else surface['error_code']} "
                  f"{surface.get('result_summary', '')[:80]}")

        print("  real MCP session over `docker exec -i` ...")
        evidence["mcp"] = asyncio.run(mcp_session(stack.mcp_argv(), args.postgres))
        for row in evidence["mcp"]["calls"]:
            brief = row.get("rows") or row.get("output", "")[:90] or row.get("returncode")
            print(f"     {'ok ' if row['ok'] else 'ERR'} {row['label']}: "
                  f"{row['error'] or ''} {brief}")

        rows = []
        actions = scratch / "audit" / "actions.jsonl"
        if actions.exists():
            rows = [json.loads(line) for line in
                    actions.read_text(encoding="utf-8").splitlines() if line.strip()]
        sql_rows = [r for r in rows if r.get("tool") in ("sql_query", "sql_execute")]
        evidence["action_log"] = {
            "rows": len(rows),
            "sequence_contiguous": [r["seq"] for r in rows] == list(range(1, len(rows) + 1)),
            "sql_rows": len(sql_rows),
            "sql_rows_naming_a_warehouse": sum(
                1 for r in sql_rows if '"warehouse":' in (r.get("args_summary") or "")),
            "qualified_targets_sample": sorted({
                t for r in rows for t in (r.get("targets") or []) + (r.get("read_targets") or [])
                if ":" in t})[:12],
            "dbt_rows": [{k: r.get(k) for k in ("seq", "tool", "args_summary", "targets", "exit")}
                         for r in rows if r.get("tool", "").startswith("dbt_")],
        }
        validated = ce._run(
            [str(episode.PY) if episode.PY.exists() else sys.executable,
             "-m", "alkera_gateway.cli", "trace", str(scratch / "audit")], check=False)
        evidence["action_log"]["gateway_trace_validates"] = validated.returncode == 0
        evidence["action_log"]["gateway_trace_error"] = (
            validated.stderr or "")[-400:] if validated.returncode else None
        evidence["containment_after"] = stack.containment_probe(
            forbidden_files=[f"{d}/manifest.yaml" for d in ce.FORBIDDEN_DIRS],
            host_paths=[str(world_root).replace("\\", "/"), str(REPO).replace("\\", "/")],
            repo_root=REPO)
    finally:
        if not args.keep_up:
            print("  compose down -v ...")
            stack.down()
    evidence["host_fingerprint_after"] = host_fingerprint(world_root)

    calls = {row["label"]: row for row in evidence.get("mcp", {}).get("calls", [])}

    def ok(label):
        return bool(calls.get(label, {}).get("ok"))

    def refused(label, code=None):
        row = calls.get(label, {})
        return (not row.get("ok")) and (code is None or row.get("error") == code)

    n_names = 3 + (1 if args.postgres else 0)
    summary = {
        "every_declared_name_answers": all(
            ok(f"count by name: {n}") for n in ["primary", "legacy", "target"]
            + (["pg"] if args.postgres else [])),
        "duckdb_duckdb_join_in_one_statement": ok(
            "legacy fact agrees with primary, one statement (duckdb<->duckdb)"),
        "duckdb_postgres_join_in_one_statement": (
            ok("postgres fact agrees with primary, one statement (duckdb<->postgres)")
            if args.postgres else None),
        "read_only_peer_refuses_writes_by_name": refused(
            "write to legacy BY NAME is refused (read-only)", "warehouse_read_only"),
        "attached_catalog_refuses_writes_at_engine": refused(
            "write to legacy THROUGH THE ATTACHMENT is refused (engine)"),
        "writable_peer_accepts_writes_by_name": ok("write to target by name succeeds"),
        "undeclared_attach_refused_at_tool_layer": refused(
            "ATTACH an undeclared file: refused at the tool layer", "unsupported_sql"),
        "undeclared_attach_refused_at_engine": "REFUSED" in (
            calls.get("engine refusal with the tool layer bypassed (bash + python)", {})
            .get("output", "")),
        "dbt_builds_the_primary_in_the_container": ok("dbt builds the PRIMARY (no warehouse argument)")
            and calls.get("dbt builds the PRIMARY (no warehouse argument)", {}).get("returncode") == 0,
        "dbt_builds_the_target_by_name": ok("dbt builds the TARGET by name")
            and calls.get("dbt builds the TARGET by name", {}).get("returncode") == 0,
        "dbt_refuses_a_non_target_warehouse": refused(
            "dbt aimed at legacy (not a dbt target): refused", "invalid_arguments"),
        "sql_query_schema_enumerates_names": sorted(
            evidence.get("mcp", {}).get("sql_query_warehouse_enum") or []) == sorted(
            ["primary", "legacy", "target"] + (["pg"] if args.postgres else [])),
        "dbt_run_schema_enumerates_targets": sorted(
            evidence.get("mcp", {}).get("dbt_run_warehouse_enum") or []) == ["primary", "target"],
        "contained_before_and_after": bool(
            evidence.get("containment", {}).get("contained")
            and evidence.get("containment_after", {}).get("contained")),
        "shell_has_no_egress": "DNS-OK" not in calls.get("egress from the shell", {}).get("output", ""),
        "every_sql_row_names_its_warehouse": (
            evidence["action_log"]["sql_rows"] > 0
            and evidence["action_log"]["sql_rows_naming_a_warehouse"]
            == evidence["action_log"]["sql_rows"]),
        "action_log_validates": evidence["action_log"]["gateway_trace_validates"],
        "same_data_same_hash_across_engines": (
            evidence.get("postgres_seed", {}).get("same_data_same_hash") if args.postgres else None),
        "declared_names": n_names,
    }
    evidence["summary"] = summary
    evidence["passed"] = all(v for v in summary.values() if isinstance(v, bool)) and all(
        v is not False for v in summary.values())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print("\n" + json.dumps({**summary, "passed": evidence["passed"], "artifact": str(args.out)},
                            indent=2))
    shutil.rmtree(scratch, ignore_errors=True)
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
