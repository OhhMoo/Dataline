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
    """Create the world's buckets. The control plane owns this, not the image.

    Measured 2026-09-13 against the pinned ``adobe/s3mock`` digest: ``initialBuckets``
    is ignored - ``ListAllMyBuckets`` comes back empty and no file store is created -
    so every ``s3_*`` tool call returned ``NoSuchBucket`` and the S3 half of
    ``acceptance_inside.py`` had never once passed. ``root`` is ignored the same way,
    which makes the ``s3data`` volume and ``retainFilesOnExit`` decorative: object
    storage in this stack is in-memory and does not survive a restart of that service.
    Anything that needs S3 state across a restart needs a different mechanism.

    Failing loudly here is deliberate. A world that serves a missing bucket is a world
    where every storage task fails for a reason that has nothing to do with the agent.
    """
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
        config=Config(s3={"addressing_style": "path"}, connect_timeout=3, read_timeout=10,
                      retries={"max_attempts": 0}),
    )
    deadline = time.monotonic() + 120
    for bucket in config.s3_buckets:
        while True:
            try:
                client.create_bucket(Bucket=bucket)
                break
            except ClientError as exc:
                if exc.response["Error"]["Code"] in {
                    "BucketAlreadyExists", "BucketAlreadyOwnedByYou"
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
    config = GatewayConfig(
        workspace=workspace,
        warehouse=warehouse,
        dashboards=dashboards,
        warehouse_backend=os.environ.get("ALKERA_BACKEND", "postgres"),
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
        config.initial_world_fp = digest(
            {"warehouse": contents, "files": files, "backend": config.warehouse_backend}
        )
    print(f"object storage buckets: {provision_storage(config)}", flush=True)
    path = Path("/tmp/gateway.json")
    path.write_text(config.model_dump_json())
    os.execvp(
        "alkera-gateway", ["alkera-gateway", "serve", "--config", str(path), "--host", "0.0.0.0"]
    )


if __name__ == "__main__":
    main()
