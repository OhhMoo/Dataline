"""Small integration world; task generation remains Track A's responsibility."""

import json
import os
import subprocess
import time
from pathlib import Path

import duckdb
import yaml

from alkera_gateway.config import GatewayConfig
from alkera_gateway.records import Observation, digest
from alkera_gateway.warehouse import create_warehouse


def provision_storage(config):
    """Create buckets explicitly; the pinned image ignores initialBuckets."""
    if not config.s3_endpoint or not config.s3_buckets:
        return []
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    client = boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint,
        region_name="us-east-1",
        aws_access_key_id="episode",
        aws_secret_access_key="episode",
        config=Config(
            s3={"addressing_style": "path"},
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 0},
        ),
    )
    deadline = time.monotonic() + 120
    for bucket in config.s3_buckets:
        while True:
            try:
                client.create_bucket(Bucket=bucket)
                break
            except ClientError as exc:
                if exc.response["Error"]["Code"] in {
                    "BucketAlreadyExists",
                    "BucketAlreadyOwnedByYou",
                }:
                    break
                raise
            except (BotoCoreError, OSError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
    present = {item["Name"] for item in client.list_buckets().get("Buckets", [])}
    missing = sorted(set(config.s3_buckets) - present)
    if missing:
        raise RuntimeError(f"object storage did not create buckets: {missing}")
    return sorted(present)


def main():
    root = Path("/world")
    workspace = root / "workspace"
    (workspace / "docs").mkdir(parents=True, exist_ok=True)
    dashboards = root / "var/bi/dashboards"
    dashboards.mkdir(parents=True, exist_ok=True)
    warehouse = root / "var/warehouse/warehouse.duckdb"
    warehouse.parent.mkdir(parents=True, exist_ok=True)
    if not warehouse.exists():
        with duckdb.connect(":memory:") as conn:
            conn.execute(f"ATTACH '{warehouse}' AS wh")
            conn.execute("CREATE SCHEMA wh.warehouse")
            conn.execute("CREATE TABLE wh.warehouse.items(id INTEGER, amount DECIMAL(10,2))")
            conn.execute("INSERT INTO wh.warehouse.items VALUES (1, 10.50), (2, 20.25)")
        (workspace / "docs/metrics.md").write_text(
            "# Revenue\nFinance owns revenue. The dashboard uses a cached snapshot.\n"
        )
        (dashboards / "revenue.yml").write_text(
            yaml.safe_dump(
                {
                    "dashboard": "revenue",
                    "owner": "finance",
                    "metrics": [
                        {
                            "name": "revenue",
                            "expression": "sum(amount)",
                            "owner": "finance",
                            "doc": "docs/metrics.md",
                        }
                    ],
                    "panels": [
                        {
                            "name": "total",
                            "metric": "revenue",
                            "sql": "select sum(amount)::decimal(10,2) as amount "
                            "from warehouse.items",
                            "watermark_sql": "select max(id) as latest_id from warehouse.items",
                        }
                    ],
                }
            )
        )
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    # Additional warehouses (gateway/DUAL_WAREHOUSE_CONTRACT.md), from the
    # ALKERA_WAREHOUSES env compose.yml passes through. A DuckDB one whose file
    # does not exist is seeded here with the same two-row fixture the primary
    # gets, one amount changed, so a cross-warehouse reconciliation query has
    # exactly one row to find. Written with the duckdb driver, never the
    # connector: the audit log has not been opened yet and world construction
    # must not appear in it. A Postgres one is the compose `postgres` service,
    # already seeded by postgres.sql.
    warehouses = json.loads(os.environ.get("ALKERA_WAREHOUSES") or "[]")
    secondary_contents = {}
    for spec in warehouses:
        if spec.get("backend", "duckdb") != "duckdb":
            continue
        path = Path(spec["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with duckdb.connect(":memory:") as conn:
                conn.execute(f"ATTACH '{path}' AS peer")
                conn.execute("CREATE SCHEMA peer.warehouse")
                conn.execute("CREATE TABLE peer.warehouse.items(id INTEGER, amount DECIMAL(10,2))")
                conn.execute("INSERT INTO peer.warehouse.items VALUES (1, 10.50), (2, 99.99)")
        with duckdb.connect(str(path), read_only=True) as conn:
            secondary_contents[spec["name"]] = [
                [row[0], str(row[1])]
                for row in conn.execute(
                    f'SELECT * FROM "{path.stem}".warehouse.items ORDER BY id'
                ).fetchall()
            ]
    config = GatewayConfig(
        workspace=workspace,
        warehouse=warehouse,
        dashboards=dashboards,
        warehouse_backend=os.environ.get("ALKERA_BACKEND", "postgres"),
        warehouses=warehouses,
        audit_dir=Path("/audit"),
        bi_state_dir=Path("/service-state/bi"),
        initial_world_fp=digest({"fixture": "track-b-integration-v1"}),
        s3_endpoint="http://s3:9090",
        s3_buckets=["landing"],
    )
    manifest = config.audit_dir / "session.json"
    if manifest.exists():
        config.initial_world_fp = json.loads(manifest.read_text())["initial_world_fp"]
    else:
        contents = create_warehouse(config).execute(
            "SELECT * FROM warehouse.items ORDER BY id", Observation(), read_only=True
        )
        files = {
            p.relative_to(root).as_posix(): p.read_bytes().hex()
            for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.parts and p.suffix != ".duckdb"
        }
        fingerprint = {"warehouse": contents, "files": files, "backend": config.warehouse_backend}
        if warehouses:
            # Conditional, so the one-warehouse stack keeps the fingerprint it
            # always computed. The Postgres peer's contents are the primary's
            # own fixture when the primary is Postgres, and postgres.sql's
            # otherwise; the declaration is what the fingerprint pins.
            fingerprint["warehouses"] = {
                "declared": warehouses, "duckdb_contents": secondary_contents,
            }
        config.initial_world_fp = digest(fingerprint)
    print(f"object storage buckets: {provision_storage(config)}", flush=True)
    path = Path("/tmp/gateway.json")
    path.write_text(config.model_dump_json())
    os.execvp(
        "alkera-gateway", ["alkera-gateway", "serve", "--config", str(path), "--host", "0.0.0.0"]
    )


if __name__ == "__main__":
    main()
