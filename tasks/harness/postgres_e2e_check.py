"""End-to-end check of the Postgres warehouse in the CONTAINER topology.

    sg docker -c "gateway/.venv/bin/python tasks/harness/postgres_e2e_check.py \\
        --image alkera-episode:head"

Two stacks, one after the other, each torn down before the next.

A `de/postgres` box (a `commerce_analytics` world, seed 11). `prepare()` copies the
world's `warehouse.*` into the stack's Postgres (`postgres_live.seed`); then, from
INSIDE the episode container, as the agent's role with the box's own PG* variables:

  (0) the seeded schema belongs to a NOLOGIN role, and `episode` - compose's owner,
      whose statements the server does NOT log and whose credentials the box's
      ALKERA_POSTGRES_DSN carries - is refused on it, so the seeded copy is reached
      through the audited `alkera` role only;
  (a) psql: every seeded table's row count and February 2026 Net Revenue equal the
      world's DuckDB warehouse read on the host; a scratch CREATE + INSERT in
      `staging`, and a DROP of a seeded table the role does not own (refused by the
      server);
  (b) psycopg: the extended protocol, a bound parameter;
  (c) dbt-postgres: `dbt debug` and `dbt run` of a table model over a `source()` of
      the seeded mart, its value read back with psql;
  (d) the audit: no `psql.statement` row exists before teardown; after it, one row
      per statement the server logged (the log is parsed on the host with the
      tailer's own parser just before teardown, so "1:1" is measured, not assumed),
      every statement the probes sent among them in order, each a valid 9.2.2 row at
      the world's clock, the writes classed as writes, the refused DROP as an error,
      and `seq` contiguous across every writer (the dbt shim's rows too);
  (e) the world: its semantic fingerprint is unchanged by the writes, which landed
      in the seeded copy; seeding itself put no statement in the server log.

An UNPROFILED box (the same theme, no profile): `prepare()` seeds nothing, and the
server holds what compose gave it - `warehouse.items`, 2 rows, in a schema `episode`
owns - read with psql and audited at teardown the same way.

Evidence: tasks/harness/evidence/postgres_e2e.json. Exit 1 on any FAIL.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

PROFILE_NAME = "de/postgres"
DEFAULT_IMAGE = "alkera-episode:head"
EVIDENCE = HERE / "evidence" / "postgres_e2e.json"
WORK_ROOT = Path.home() / "alkera-pg-e2e"
IN_DIR = "/tmp/pg-e2e"
PYTHON = "/usr/local/bin/python"   # absolute: the probe is the check's, not a shim row
TOOL = "psql.statement"

#: The world's facts, in Postgres' spelling (database `alkera`, schema `warehouse`).
FEB_SQL = ("SELECT SUM(order_amount) FROM warehouse.fct_orders WHERE order_status = "
           "'completed' AND order_date >= DATE '2026-02-01' AND order_date < "
           "DATE '2026-03-01'")
#: The same fact over the world's DuckDB file on the host.
FEB_SQL_DUCKDB = FEB_SQL.replace("warehouse.fct_orders", "warehouse.warehouse.fct_orders")
SCRATCH = ["CREATE TABLE staging.pg_e2e_scratch (id integer, note text)",
           "INSERT INTO staging.pg_e2e_scratch VALUES (1, 'alpha'), (2, 'beta')",
           "SELECT id, note FROM staging.pg_e2e_scratch ORDER BY id"]
REFUSED = "DROP TABLE warehouse.fct_orders"
#: compose's schema owner (gateway/docker/postgres.sql), against the server's real
#: database: the role is the one the box's ALKERA_POSTGRES_DSN names.
EPISODE_URL = "postgresql://episode:episode-local@postgres.internal:5432/alkera"
EPISODE_PROBE = "SELECT count(*) FROM warehouse.fct_orders"
OWNER_SQL = ("SELECT r.rolname || '|' || CASE WHEN r.rolcanlogin THEN 't' ELSE 'f' END "
             "FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner "
             "WHERE n.nspname = 'warehouse'")

PSYCOPG = r'''
import json, os, psycopg
with psycopg.connect(connect_timeout=20) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM warehouse.fct_orders WHERE order_status = %s",
                    ["completed"])
        print("PG-E2E " + json.dumps({"completed": cur.fetchone()[0]}))
'''

DBT_PROJECT = """name: pg_e2e
version: "1.0"
profile: pg_e2e
config-version: 2
model-paths: [models]
"""
DBT_PROFILES = """pg_e2e:
  target: postgres
  outputs:
    postgres:
      type: postgres
      host: "{{ env_var('PGHOST') }}"
      port: "{{ env_var('PGPORT') | as_number }}"
      user: "{{ env_var('PGUSER') }}"
      password: "{{ env_var('PGPASSWORD') }}"
      dbname: "{{ env_var('PGDATABASE') }}"
      schema: staging
      threads: 1
      connect_timeout: 20
"""
DBT_SOURCES = """version: 2
sources:
  - name: warehouse
    schema: warehouse
    tables:
      - name: fct_orders
"""
DBT_MODEL = """{{ config(materialized='table') }}
select sum(order_amount) as net_revenue
from {{ source('warehouse', 'fct_orders') }}
where order_status = 'completed'
  and order_date >= date '2026-02-01' and order_date < date '2026-03-01'
"""


def expected_from_duckdb(path: Path) -> dict:
    """Per-table row counts of the `warehouse` schema and February 2026 Net Revenue."""
    import duckdb

    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog = "
            "current_database() AND table_schema = 'warehouse' AND table_type = "
            "'BASE TABLE' ORDER BY 1").fetchall()]
        counts = {t: con.execute(f'SELECT count(*) FROM warehouse.warehouse."{t}"')
                  .fetchone()[0] for t in tables}
        feb = con.execute(FEB_SQL_DUCKDB).fetchone()[0]
        completed = con.execute("SELECT count(*) FROM warehouse.warehouse.fct_orders "
                                "WHERE order_status = 'completed'").fetchone()[0]
    finally:
        con.close()
    return {"counts": counts, "net_revenue_feb_2026": str(feb), "completed": completed}


def world_fingerprint(world_root: str) -> dict:
    """`world.fingerprint.semantic_hash`, and the per-plane digests it is made of."""
    import episode

    if str(episode.TRACK_A) not in sys.path:
        sys.path.insert(0, str(episode.TRACK_A))
    from world.fingerprint import semantic, semantic_hash

    planes = semantic(Path(world_root))
    return {"hash": semantic_hash(Path(world_root)),
            "planes": {k: v if isinstance(v, str) else json.dumps(v, sort_keys=True,
                                                                     default=str)[:200]
                       for k, v in sorted(planes.items())}}


def read_rows(audit: Path) -> list[dict]:
    path = audit / "actions.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def server_log(topo) -> str:
    """The server's statement log, read from native-services (pgdata:/pgdata:ro)."""
    import postgres_live

    done = subprocess.run(["docker", "exec", topo.stack.services_container, "cat",
                           postgres_live.SERVER_LOG], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    return done.stdout


def logged_statements(text: str) -> list[str]:
    from alkera_gateway.native.tailers.postgres import iter_entries, iter_statements

    return [s.sql for s in iter_statements(iter_entries(text.splitlines(True)))]


def psql(topo, sql: str):
    return topo.run(f"psql -X -tA -v ON_ERROR_STOP=1 -c {json.dumps(sql)}", timeout=120)


class Checks:
    def __init__(self):
        self.items: list[dict] = []

    def __call__(self, name, ok, detail=None):
        self.items.append({"check": name, "result": "PASS" if ok else "FAIL",
                           "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if not ok and detail
                                                      else ""), flush=True)

    @property
    def passed(self) -> bool:
        return all(c["result"] == "PASS" for c in self.items)


def world_for(work: Path, profile: str) -> tuple[dict, str]:
    import bi_e2e_check as bi
    import tool_tour

    if profile:
        seed = tool_tour.world_seed(profile, {})
    else:
        seed = bi.SEED_EXAMPLE.read_text(encoding="utf-8").rstrip("\n") + "\nnative: true\n"
    with tool_tour.as_profile(profile, {}, seed):
        world = bi.build_world(work)
    return world, seed


def run_profiled(args, check: Checks, record: dict) -> None:
    import agent_under_test as aut
    import bi_e2e_check as bi
    import blindness
    import tool_tour

    work = args.work / "de-postgres"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    world, seed = world_for(work, PROFILE_NAME)
    expected = expected_from_duckdb(Path(world["warehouse"]))
    record["expected"] = expected
    fingerprint_before = None
    topo = tool_tour.TourTopology(world, work, args.image, PROFILE_NAME, {}, seed)
    sent: list[str] = []
    logged: list[str] = []
    audit = Path(topo.audit_dir)
    try:
        up = topo.up()
        record["stack"] = {k: up.get(k) for k in ("container_id", "image", "image_digest",
                                                  "services", "episode_endpoints")}
        record["pg_env"] = {k: v for k, v in topo.container_env.items()
                            if k.startswith("PG")}
        # After up(): bringing the box up provisions the world (the workspace CLI,
        # the discovery files, sanitised world files), which is not the probes' doing.
        fingerprint_before = world_fingerprint(world["world_root"])
        seeded = up["prepared"].get("postgres_seed") or {}
        record["seed"] = seeded
        check("prepare() seeded warehouse.* from the world (counts equal the DuckDB file)",
              seeded.get("tables") == expected["counts"],
              {"seeded": seeded.get("tables"), "world": expected["counts"]})
        before_log = logged_statements(server_log(topo))
        check("seeding put no statement in the server log (superuser, unlogged role)",
              before_log == [], before_log[:5])

        # The seeded copy is reachable through the audited role only. `episode`
        # (compose's schema owner, NOT logged) is the role ALKERA_POSTGRES_DSN in
        # the box's environment names; it keeps nothing on the seeded schema.
        record["episode_dsn_in_box"] = topo.container_env.get("ALKERA_POSTGRES_DSN")
        as_episode = {}
        for label, url in (("box_dsn", record["episode_dsn_in_box"]),
                           ("episode_on_alkera_db", EPISODE_URL)):
            if not url:
                continue
            done = topo.run(f"psql {json.dumps(url)} -X -tA -c "
                            f"{json.dumps(EPISODE_PROBE)}", timeout=120)
            as_episode[label] = {"rc": done.returncode, "out": done.stdout.strip(),
                                 "err": done.stderr.strip()[-300:]}
        record["as_episode"] = as_episode
        denied = as_episode.get("episode_on_alkera_db") or {}
        check("the episode role (unlogged; the box's ALKERA_POSTGRES_DSN) cannot read "
              "the seeded warehouse.fct_orders",
              denied.get("rc") not in (0, None)
              and "permission denied for schema warehouse" in denied.get("err", ""),
              as_episode)
        sent.append(OWNER_SQL)
        owner = psql(topo, OWNER_SQL)
        record["seeded_schema_owner"] = owner.stdout.strip()
        check("the seeded schema's owner is a NOLOGIN role, not episode",
              owner.stdout.strip() == "warehouse_owner|f", owner.stdout + owner.stderr)

        # (a) psql
        counts = {}
        for table in expected["counts"]:
            sql = f"SELECT count(*) FROM warehouse.{table}"
            sent.append(sql)
            out = psql(topo, sql)
            counts[table] = int(out.stdout.strip() or -1) if out.returncode == 0 else None
        check("psql as the audited alkera role: every seeded table's row count equals "
              "the world's", counts == expected["counts"], {"psql": counts})
        sent.append(FEB_SQL)
        feb = psql(topo, FEB_SQL)
        record["psql_net_revenue_feb_2026"] = feb.stdout.strip()
        check("psql: February 2026 Net Revenue equals the world's DuckDB",
              feb.returncode == 0 and tool_tour.same_value(feb.stdout.strip(),
                                                           expected["net_revenue_feb_2026"]),
              {"psql": feb.stdout.strip() or feb.stderr[-300:],
               "world": expected["net_revenue_feb_2026"]})
        scratch = []
        for sql in SCRATCH:
            sent.append(sql)
            scratch.append(psql(topo, sql))
        check("psql: a scratch table in staging is created, written and read back",
              all(s.returncode == 0 for s in scratch)
              and scratch[-1].stdout.split() == ["1|alpha", "2|beta"],
              [(s.returncode, s.stdout.strip(), s.stderr.strip()[-200:]) for s in scratch])
        sent.append(REFUSED)
        refused = psql(topo, REFUSED)
        check("psql: DROP of a seeded table the agent's role does not own is refused",
              refused.returncode != 0 and "must be owner" in refused.stderr,
              refused.stderr[-300:])

        # (b) psycopg
        topo.exec_raw(["mkdir", "-p", f"{IN_DIR}/models"])
        topo.put("psycopg_probe.py", PSYCOPG)
        done = topo.exec_raw([PYTHON, topo.script("psycopg_probe.py")], timeout=300)
        line = next((ln for ln in done.stdout.splitlines() if ln.startswith("PG-E2E ")), "")
        got = json.loads(line[7:]) if line else {}
        record["psycopg"] = got or (done.stdout + done.stderr)[-800:]
        check("psycopg: a bound-parameter count equals the world's",
              got.get("completed") == expected["completed"],
              {"psycopg": got, "world": expected["completed"]})
        sent.append("SELECT count(*) FROM warehouse.fct_orders WHERE order_status = $1")

        # (c) dbt-postgres, through the box's own `dbt` (the shim)
        for rel, text in {"dbt_project.yml": DBT_PROJECT, "profiles.yml": DBT_PROFILES,
                          "models/sources.yml": DBT_SOURCES,
                          "models/pg_net_revenue_feb.sql": DBT_MODEL}.items():
            local = work / "dbt" / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(text, encoding="utf-8")
            topo.stack.copy_in(local, f"{IN_DIR}/{rel}")
        dbt = {}
        for verb in (["debug"], ["run", "-s", "pg_net_revenue_feb"]):
            res = topo.run(f"cd {IN_DIR} && dbt {' '.join(verb)} --project-dir {IN_DIR} "
                           f"--profiles-dir {IN_DIR}", timeout=600)
            out = res.stdout + res.stderr
            dbt[verb[0]] = {"rc": res.returncode, "tail": out[-1500:]}
            check(f"dbt-postgres: `dbt {' '.join(verb)}` against the live server",
                  res.returncode == 0 and ("Registered adapter: postgres" in out
                                           or verb[0] == "run"), out[-800:])
        record["dbt"] = dbt
        sent.append("SELECT net_revenue FROM staging.pg_net_revenue_feb")
        model = psql(topo, "SELECT net_revenue FROM staging.pg_net_revenue_feb")
        check("dbt-postgres: the model's value equals the world's DuckDB",
              model.returncode == 0 and tool_tour.same_value(
                  model.stdout.strip(), expected["net_revenue_feb_2026"]),
              {"model": model.stdout.strip() or model.stderr[-300:],
               "world": expected["net_revenue_feb_2026"]})

        # (d) the audit, before teardown
        rows_before = read_rows(audit)
        check("no psql.statement row exists before teardown",
              not [r for r in rows_before if r["tool"] == TOOL],
              len([r for r in rows_before if r["tool"] == TOOL]))
        logged = logged_statements(server_log(topo))
        record["server_log_statements"] = len(logged)
    finally:
        topo.down()
    rows = read_rows(audit)
    pg_rows = [r for r in rows if r["tool"] == TOOL]
    audited = [r["args"]["sql"] for r in pg_rows]
    record["counts"] = {"server_log_statements": len(logged), "psql_rows": len(pg_rows),
                        "rows_total": len(rows), "probe_statements": len(sent),
                        "shim_rows": len([r for r in rows if r["tool"].startswith("shim.")])}
    check("rows landed at teardown: one psql.statement per logged statement (1:1, in order)",
          audited == logged and len(audited) > 0,
          {"logged": len(logged), "rows": len(audited)})
    check("every statement the probes sent is exactly one row",
          all(audited.count(sql) == 1 for sql in sent),
          {s[:60]: audited.count(s) for s in sent if audited.count(s) != 1})
    # In server order, the REFUSED one included: the tailer emits a failing
    # statement at its ERROR (no `duration:` follows an error), not when its
    # backend's stream ends, which put it last of all before 2026-10-07.
    it = iter(audited)
    check("every statement is a row in the order it was sent, the refused DROP included",
          all(any(sql == a for a in it) for sql in sent))
    record["refused_row_position"] = {"sent_index": sent.index(REFUSED),
                                      "row_index": audited.index(REFUSED)
                                      if REFUSED in audited else None,
                                      "rows": len(audited)}
    seqs = [r["seq"] for r in rows]
    check("seq is contiguous across every writer (shims, then the tailer)",
          seqs == list(range(1, len(seqs) + 1)), seqs[-5:])
    clock = tool_tour._clock(world)
    bad = {r["seq"]: p for r in pg_rows if (p := bi.validate_row(r, vnow=clock))}
    check("every psql.statement row is a valid 9.2.2 row at the world's clock",
          not bad, dict(list(bad.items())[:3]))
    by_sql = {r["args"]["sql"]: r for r in pg_rows}
    classes = {sql: (by_sql.get(sql) or {}).get("op_class") for sql in SCRATCH[:2]}
    check("the scratch writes are classed as writes (ddl, write)",
          classes == {SCRATCH[0]: "ddl", SCRATCH[1]: "write"}, classes)
    drop = by_sql.get(REFUSED) or {}
    check("the refused DROP is a destructive row with exit=error",
          drop.get("op_class") == "destructive" and drop.get("exit") == "error",
          {k: drop.get(k) for k in ("op_class", "exit", "error")})
    dbt_rows = [r for r in pg_rows if r["args"]["sql"] not in sent]
    check("dbt-postgres's own statements are psql.statement rows too",
          len(dbt_rows) > 0, len(dbt_rows))
    record["audit_sample"] = pg_rows[:2] + pg_rows[-2:]
    record["op_classes"] = sorted({r["op_class"] for r in pg_rows})
    try:
        from alkera_gateway.audit import validate_trace

        validate_trace(audit / "actions.jsonl")
        trace = True
    except Exception as exc:  # noqa: BLE001
        trace = f"{type(exc).__name__}: {exc}"
    check("the whole action log validates as a trace", trace is True, trace)
    # (e) the world
    fingerprint_after = world_fingerprint(world["world_root"])
    record["fingerprint"] = {"before": fingerprint_before, "after": fingerprint_after}
    moved = sorted(k for k in fingerprint_after["planes"]
                   if (fingerprint_before or {}).get("planes", {}).get(k)
                   != fingerprint_after["planes"][k])
    check("a write through Postgres leaves the world fingerprint unchanged",
          fingerprint_before is not None
          and fingerprint_before["hash"] == fingerprint_after["hash"],
          {"planes_moved": moved})
    destinations = tool_tour.read_destinations(audit) or []
    record["destinations"] = destinations
    allowed = aut.allowed_destinations(record["stack"]["episode_endpoints"])
    violations = [str(v) for v in blindness.network_violations(
        destinations, allowed, aut.normalise_host)]
    check("destinations hold no violation", not violations, violations)
    record["postgres_in_destinations"] = [d for d in destinations
                                          if d.get("host") == "postgres.internal"]


def run_unprofiled(args, check: Checks, record: dict) -> None:
    import tool_tour

    work = args.work / "unprofiled"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    world, seed = world_for(work, "")
    topo = tool_tour.TourTopology(world, work, args.image, "", {}, seed)
    sent = ["SELECT string_agg(table_name, ',' ORDER BY table_name) FROM "
            "information_schema.tables WHERE table_schema = 'warehouse'",
            "SELECT count(*) FROM warehouse.items", OWNER_SQL]
    out = []
    try:
        up = topo.up()
        record["prepared_keys"] = sorted(up["prepared"])
        check("unprofiled: prepare() seeds nothing", "postgres_seed" not in up["prepared"],
              up["prepared"].get("postgres_seed"))
        out = [psql(topo, sql) for sql in sent]
    finally:
        topo.down()
    got = [o.stdout.strip() for o in out]
    record["server"] = got
    check("unprofiled: the server holds only warehouse.items, with 2 rows, its schema "
          "owned by episode as compose made it", got == ["items", "2", "episode|t"], got)
    rows = [r for r in read_rows(Path(topo.audit_dir)) if r["tool"] == TOOL]
    check("unprofiled: the three psql statements are audited at teardown",
          [r["args"]["sql"] for r in rows] == sent, [r["args"]["sql"] for r in rows])


def main(argv=None) -> int:
    import container_episode as ce

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--work", type=Path, default=WORK_ROOT)
    parser.add_argument("--out", type=Path, default=EVIDENCE)
    parser.add_argument("--skip-unprofiled", action="store_true")
    args = parser.parse_args(argv)
    args.work.mkdir(parents=True, exist_ok=True)
    started = time.time()
    check = Checks()
    record: dict = {"profile": PROFILE_NAME, "image": args.image,
                    "image_gate": ce.assert_image_current(args.image),
                    "revision": subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                                               capture_output=True, text=True).stdout.strip()}
    record["de_postgres"] = {}
    try:
        run_profiled(args, check, record["de_postgres"])
    except Exception as exc:  # noqa: BLE001 - recorded, and a FAIL
        check("de/postgres stack ran to the end", False, f"{type(exc).__name__}: {exc}")
    if not args.skip_unprofiled:
        record["unprofiled"] = {}
        try:
            run_unprofiled(args, check, record["unprofiled"])
        except Exception as exc:  # noqa: BLE001
            check("unprofiled stack ran to the end", False, f"{type(exc).__name__}: {exc}")
    record["checks"] = check.items
    record["seconds"] = round(time.time() - started, 1)
    record["result"] = "PASS" if check.passed else "FAIL"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n",
                        encoding="utf-8")
    print(f"{record['result']}: {sum(c['result'] == 'PASS' for c in check.items)}/"
          f"{len(check.items)} -> {args.out}")
    return 0 if check.passed else 1


if __name__ == "__main__":
    sys.exit(main())
