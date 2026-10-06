"""End-to-end check of the Snowflake warehouse plug in the CONTAINER topology.

    sg docker -c "uv run --project gateway python tasks/harness/snowflake_e2e_check.py"

Builds a real `commerce_analytics` world for the `da/snowflake` profile, brings
up `container_episode.NativeContainerEpisode` with that profile (the compose
profile `snowflake` starts the service, seeded from the world's own DuckDB
warehouse, reachable as `snowflake.internal:8443` over TLS the episode trusts),
and from INSIDE the episode container:

  (a) snowflake-connector-python: SELECTs whose values must equal the world's
      DuckDB warehouse read on the host, then CREATE + INSERT + SELECT on a
      scratch table;
  (b) dbt-snowflake: `dbt debug` and `dbt run -s sf_check` against a snowflake
      profile target, the model reading a seeded table;
  (c) the audit: every statement the proxy forwarded (its private statement
      log, in the service's own volume) is ONE `snowflake.statement` row in the
      rollout's actions.jsonl, in order, each a valid 9.2.2 row; the probe's own
      statements appear exactly once each; destinations.jsonl names
      snowflake.internal:8443 after teardown.

Evidence: tasks/harness/evidence/snowflake_e2e.json. Exit 1 on any FAIL.
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

PROFILE_NAME = "da/snowflake"
PROFILE_SLOTS = ("warehouse", "transform", "human")
SEED_EXAMPLE = REPO / "world" / "world" / "themes" / "commerce_analytics" / "seed.example.yaml"
DEFAULT_IMAGE = "alkera-episode:head-da-snowflake"
EVIDENCE = HERE / "evidence" / "snowflake_e2e.json"
IN_DIR = "/tmp/sf-e2e"
PYTHON = "/usr/local/bin/python"  # absolute: the probe is the check's, not a shim row
TOOL = "snowflake.statement"


def seed_text(example: str) -> str:
    return (example.rstrip("\n") + "\n"
            "native: true\n"
            "profile:\n"
            f"  name: {PROFILE_NAME}\n"
            f"  slots: [{', '.join(PROFILE_SLOTS)}]\n")


def build_world(work: Path) -> dict:
    import episode

    root, grading = work / "world", work / "grading"
    shutil.rmtree(root, ignore_errors=True)
    grading.mkdir(parents=True, exist_ok=True)
    seed = work / "seed.yaml"
    seed.write_text(seed_text(SEED_EXAMPLE.read_text(encoding="utf-8")), encoding="utf-8")
    done = subprocess.run([str(episode.PY), "-m", "world.build", "--seed", str(seed),
                           "--root", str(root)], capture_output=True, text=True,
                          env=episode.venv_env())
    if done.returncode != 0:
        raise SystemExit(f"world.build failed:\n{done.stdout}\n{done.stderr}")
    return {"world_root": str(root), "workspace": str(root / "workspace"),
            "warehouse": str(root / "var" / "warehouse" / "warehouse.duckdb"),
            "scheduler": str(root / "var" / "sched" / "state.json"),
            "dashboards": str(root / "var" / "bi" / "dashboards"),
            "profiles_dir": str(root / "build"),
            "workflow_dir": str(root / "var" / "workflow"),
            "grading": str(grading)}


def expected_from_duckdb(path: Path) -> dict:
    """Per-table row counts and one numeric sum, read from the world's file."""
    import duckdb

    conn = duckdb.connect(str(path), read_only=True)
    try:
        # Fully qualified: a file named warehouse.duckdb is catalog `warehouse`
        # holding schema `warehouse`, and a two-part name is then ambiguous.
        cat = conn.execute("SELECT current_database()").fetchone()[0]
        tables = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_catalog = current_database() AND table_schema NOT IN ('information_schema','pg_catalog') "
            "ORDER BY 1, 2").fetchall()
        counts = {f"{s}.{t}".upper(): conn.execute(
            f'SELECT count(*) FROM "{cat}"."{s}"."{t}"').fetchone()[0] for s, t in tables}
        pick = None
        for s, t in tables:
            cols = conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=? "
                "AND table_name=? AND data_type IN ('INTEGER','BIGINT','DOUBLE','HUGEINT') "
                "OR (table_schema=? AND table_name=? AND data_type LIKE 'DECIMAL%') "
                "ORDER BY ordinal_position", [s, t, s, t]).fetchall()
            if cols and counts[f"{s}.{t}".upper()] > 0:
                pick = (s, t, cols[0][0])
                break
        total = None
        if pick:
            total = conn.execute(
                f'SELECT CAST(sum("{pick[2]}") AS VARCHAR) FROM "{cat}"."{pick[0]}"."{pick[1]}"'
            ).fetchone()[0]
        return {"counts": counts, "sum": {"table": f"{pick[0]}.{pick[1]}".upper(),
                                          "column": pick[2], "value": total} if pick else None}
    finally:
        conn.close()


PROBE = r'''
import json, os, sys
import snowflake.connector as sc
spec = json.loads(sys.argv[1])
out = {"statements": [], "env": {k: os.environ.get(k) for k in
       ("SNOWFLAKE_HOST", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "REQUESTS_CA_BUNDLE")}}
conn = sc.connect(account=os.environ["SNOWFLAKE_ACCOUNT"], user=os.environ["SNOWFLAKE_USER"],
                  password=os.environ["SNOWFLAKE_PASSWORD"], host=os.environ["SNOWFLAKE_HOST"],
                  port=8443, protocol="https", database=spec["database"],
                  login_timeout=20, network_timeout=30)
cur = conn.cursor()
def run(sql):
    out["statements"].append(sql)
    cur.execute(sql)
    return [[str(v) for v in row] for row in cur.fetchall()]
out["counts"] = {}
for name in spec["tables"]:
    out["counts"][name] = int(run(f"SELECT count(*) FROM {spec['database']}.{name}")[0][0])
if spec.get("sum"):
    s = spec["sum"]
    out["sum"] = run(f"SELECT CAST(sum({s['column']}) AS VARCHAR) FROM {spec['database']}.{s['table']}")[0][0]
run(f"CREATE OR REPLACE TABLE {spec['database']}.PUBLIC.SF_E2E_SCRATCH (id INT, note VARCHAR)")
out["insert"] = run(f"INSERT INTO {spec['database']}.PUBLIC.SF_E2E_SCRATCH VALUES (1, 'alpha'), (2, 'beta')")
out["read_back"] = run(f"SELECT id, note FROM {spec['database']}.PUBLIC.SF_E2E_SCRATCH ORDER BY id")
cur.close(); conn.close()
print("SF-E2E " + json.dumps(out))
'''

DBT_PROJECT = """name: sf_e2e
version: "1.0"
profile: sf_e2e
config-version: 2
model-paths: [models]
"""

DBT_PROFILES = """sf_e2e:
  target: snowflake
  outputs:
    snowflake:
      type: snowflake
      account: "{{ env_var('SNOWFLAKE_ACCOUNT') }}"
      host: "{{ env_var('SNOWFLAKE_HOST') }}"
      port: 8443
      protocol: https
      user: "{{ env_var('SNOWFLAKE_USER') }}"
      password: "{{ env_var('SNOWFLAKE_PASSWORD') }}"
      database: DATABASE_NAME
      warehouse: COMPUTE_WH
      schema: DBT_E2E
      threads: 1
      connect_timeout: 20
"""


def main(argv=None) -> int:
    import container_episode as ce
    import native_stack

    from alkera_gateway.native.audit import ROW_KEYS

    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--work", type=Path, default=Path.home() / "alkera-sf-e2e")
    parser.add_argument("--out", type=Path, default=EVIDENCE)
    parser.add_argument("--keep", action="store_true", help="leave the stack up")
    args = parser.parse_args(argv)

    work = args.work
    work.mkdir(parents=True, exist_ok=True)
    for name in ("audit", "rollout"):
        shutil.rmtree(work / name, ignore_errors=True)
    world = build_world(work)
    warehouse = Path(world["warehouse"])
    database = warehouse.stem.upper()
    expected = expected_from_duckdb(warehouse)
    checks: list[dict] = []

    def check(name, ok, detail=None):
        checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if not ok and detail else ""))

    record: dict = {"profile": PROFILE_NAME, "image": args.image, "database": database,
                    "expected": expected}
    stack = ce.NativeContainerEpisode(
        project=f"alkera-sf-e2e-{int(time.time())}", world=world,
        audit_dir=work / "audit", rollout_dir=work / "rollout", image=args.image,
        profile=PROFILE_NAME, plugs=None)
    try:
        record["discovery"] = native_stack.stage_discovery(world)
        record["container_id"] = stack.up()[:12]
        record["prepared"] = stack.prepare()
        record["overlay"] = stack.overlay.read_text(encoding="utf-8")
        ids = stack._compose("ps", "-q", "snowflake").stdout.split()
        snowflake_container = ids[0] if ids else None
        check("snowflake service running under the profile", bool(snowflake_container))
        seedlog = subprocess.run(["docker", "logs", snowflake_container],
                                 capture_output=True, text=True).stdout
        record["seed_log"] = [json.loads(line) for line in seedlog.splitlines()
                              if line.startswith('{"seed"')]

        stack.exec_(["mkdir", "-p", f"{IN_DIR}/models"])
        probe = work / "probe.py"
        probe.write_text(PROBE, encoding="utf-8")
        stack.copy_in(probe, f"{IN_DIR}/probe.py")
        spec = {"database": database, "tables": sorted(expected["counts"]),
                "sum": expected["sum"]}
        audit_file = work / "audit" / "actions.jsonl"
        before = len(audit_file.read_text(encoding="utf-8").splitlines()) \
            if audit_file.exists() else 0
        done = stack.exec_([PYTHON, f"{IN_DIR}/probe.py", json.dumps(spec)], check=False,
                           timeout=300)
        line = next((ln for ln in done.stdout.splitlines() if ln.startswith("SF-E2E ")), None)
        check("connector probe ran inside the episode", line is not None,
              (done.stdout + done.stderr)[-1500:])
        got = json.loads(line[7:]) if line else {}
        record["probe"] = got
        check("TLS: episode trusts the proxy through REQUESTS_CA_BUNDLE",
              got.get("env", {}).get("REQUESTS_CA_BUNDLE") == ce.SNOWFLAKE_BUNDLE,
              got.get("env"))
        check("seeded: every table's row count equals the world's DuckDB",
              got.get("counts") == expected["counts"],
              {"got": got.get("counts"), "expected": expected["counts"]})
        if expected["sum"]:
            check("seeded: a numeric sum equals the world's DuckDB",
                  got.get("sum") is not None and float(got["sum"]) == float(
                      expected["sum"]["value"]),
                  {"got": got.get("sum"), "expected": expected["sum"]})
        check("scratch INSERT reads back", got.get("read_back") == [["1", "alpha"],
                                                                     ["2", "beta"]],
              got.get("read_back"))
        rows = [json.loads(ln) for ln in audit_file.read_text(encoding="utf-8").splitlines()]
        probe_rows = [r for r in rows[before:] if r["tool"] == TOOL]
        sent = got.get("statements", [])
        check("audit: one snowflake.statement row per probe statement, in order",
              [r["args"]["sql"] for r in probe_rows if r["args"]["sql"] in sent] == sent,
              {"sent": len(sent), "rows": len(probe_rows)})
        record["audit_sample"] = probe_rows[-3:]

        # dbt-snowflake, from the episode's own PATH (the dbt shim).
        table = sorted(expected["counts"])[0]
        files = {"dbt_project.yml": DBT_PROJECT,
                 "profiles.yml": DBT_PROFILES.replace("DATABASE_NAME", database),
                 "models/sf_check.sql":
                     "{{ config(materialized='table') }}\n"
                     f"select count(*) as n from {database}.{table}\n"}
        for rel, text in files.items():
            local = work / "dbt" / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(text, encoding="utf-8")
            stack.copy_in(local, f"{IN_DIR}/{rel}")
        dbt_before = len(audit_file.read_text(encoding="utf-8").splitlines())
        dbt = {}
        for verb in (["debug"], ["run", "-s", "sf_check"]):
            res = stack.exec_(["sh", "-c", f"cd {IN_DIR} && dbt {' '.join(verb)} "
                               f"--project-dir {IN_DIR} --profiles-dir {IN_DIR}"],
                              check=False, timeout=600)
            dbt[verb[0]] = {"rc": res.returncode, "tail": (res.stdout + res.stderr)[-1200:]}
            check(f"dbt {' '.join(verb)} against the snowflake target", res.returncode == 0,
                  dbt[verb[0]]["tail"])
        record["dbt"] = dbt
        verify = stack.exec_([PYTHON, "-c", (
            "import os,snowflake.connector as sc;c=sc.connect(account=os.environ"
            "['SNOWFLAKE_ACCOUNT'],user=os.environ['SNOWFLAKE_USER'],password=os.environ"
            "['SNOWFLAKE_PASSWORD'],host=os.environ['SNOWFLAKE_HOST'],port=8443,"
            f"protocol='https');cur=c.cursor();cur.execute('SELECT n FROM {database}"
            ".DBT_E2E.SF_CHECK');print(cur.fetchone()[0])")], check=False)
        check("dbt model's value equals the world's DuckDB",
              verify.stdout.strip() == str(expected["counts"][table]),
              {"got": verify.stdout.strip() or verify.stderr[-400:],
               "expected": expected["counts"][table]})
        rows = [json.loads(ln) for ln in audit_file.read_text(encoding="utf-8").splitlines()]
        dbt_rows = [r for r in rows[dbt_before:] if r["tool"] == TOOL]
        check("audit: dbt's statements are snowflake.statement rows", len(dbt_rows) > 0,
              len(dbt_rows))

        # 1:1 against the proxy's own statement log.
        log = subprocess.run(["docker", "exec", snowflake_container, "cat",
                              "/snowflake-data/statements.jsonl"], capture_output=True,
                             text=True).stdout
        logged = [json.loads(ln)["sql"] for ln in log.splitlines() if ln.strip()]
        audited = [r["args"]["sql"] for r in rows if r["tool"] == TOOL]
        check("audit: every forwarded statement is exactly one row (statement log 1:1)",
              logged == audited, {"statement_log": len(logged), "audit_rows": len(audited)})
        bad = [r["seq"] for r in rows if r["tool"] == TOOL and set(r) != set(ROW_KEYS)]
        check("audit: rows carry exactly the 9.2.2 keys", not bad, bad[:5])
        seqs = [r["seq"] for r in rows]
        check("audit: seq is contiguous across writers", seqs == list(range(1, len(seqs) + 1)),
              seqs[-5:])
        record["counts"] = {"statement_log": len(logged), "audit_rows": len(audited),
                            "dbt_rows": len(dbt_rows), "probe_rows": len(probe_rows)}
        record["op_classes"] = sorted({(r["op_class"], r["args"]["sql"][:40]) for r in rows
                                       if r["tool"] == TOOL and r["args"]["sql"] in sent})
    finally:
        if not args.keep:
            stack.down()
    if not args.keep:
        dest = [json.loads(ln) for ln in (work / "audit" / "destinations.jsonl")
                .read_text(encoding="utf-8").splitlines() if ln.strip()]
        sf_dest = [d for d in dest if d.get("host") == "snowflake.internal"
                   and d.get("port") == 8443]
        check("destinations.jsonl counts snowflake.internal:8443 after teardown",
              bool(sf_dest) and sf_dest[0]["count"] > 0, sf_dest)
        record["destinations"] = dest
    record["checks"] = checks
    record["result"] = "PASS" if all(c["result"] == "PASS" for c in checks) else "FAIL"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n",
                        encoding="utf-8")
    print(f"{record['result']}: {sum(c['result'] == 'PASS' for c in checks)}/{len(checks)} "
          f"-> {args.out}")
    return 0 if record["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
