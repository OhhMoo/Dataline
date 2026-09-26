"""Phase 1 acceptance for native-mode auditing (gateway/NATIVE.md).

Two claims are proved here and nothing else is.

(a) SHAPE. Every row a tailer or a shim writes carries the agent.md 9.2.2 key
    set the MCP gateway writes, and the resulting file passes the gateway's own
    `audit.validate_trace` - contiguous `seq`, `args_hash` and `result_hash`
    agreeing with the canonical JSON. The key set is compared against a *real*
    MCP row: a recorded rollout under %TEMP%/alkera-rollouts when this machine
    has one, else a row this test makes the gateway write.

(b) TARGET-SET PARITY. One scripted sequence - a SELECT, an UPDATE, an object
    put and a file edit - is run twice, once through the MCP gateway and once
    through psql, boto3 and a PATH shim, and the set of `targets` in the two
    action logs is asserted equal. That set is what blindness detection and the
    graders consume, so it is the thing that has to survive the transport swap.

WHAT A SKIP MEANS HERE
----------------------
Postgres and MinIO come from containers. When Docker cannot provide one the test
skips with the reason in the message - it never quietly falls back to a parser
test and reports a pass, because a vacuous pass on this file would say the two
transports agree when nothing compared them.

THE ONE PLACE THE TWO LEGS TALK TO DIFFERENT SERVERS
----------------------------------------------------
`storage.ObjectStorage` hardcodes `episode`/`episode` as its S3 credentials, and
`episode` is seven characters - below MinIO's eight-character minimum for a root
secret - so the gateway cannot authenticate against the MinIO whose audit webhook
produces the native rows. The MCP leg therefore puts its object into the pinned
`adobe/s3mock` that `docker/compose.yml` and `test_object_storage.py` already
use, and the native leg puts the same key into MinIO. Both are real servers and
both legs really store an object; the parity claim is about the target strings
the two logs contain. `native/AUDIT_REQUESTS.md` asks for the credentials to
become configurable so one server can serve both.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from conftest import clone_world
from conftest import config as world_config

from alkera_gateway import Gateway
from alkera_gateway.audit import read_rows, validate_trace
from alkera_gateway.native import audit
from alkera_gateway.native.shims import install as shim_install
from alkera_gateway.native.shims import run as shim_run
from alkera_gateway.native.tailers import airflow as airflow_tailer
from alkera_gateway.native.tailers import minio as minio_tailer
from alkera_gateway.native.tailers import postgres as postgres_tailer

# Same digests the stack pins, so a green test is about the servers a rollout
# talks to. Postgres mirrors test_warehouse_backends.py, s3mock mirrors
# test_object_storage.py, MinIO is pinned here because nothing else pins it yet.
PG_IMAGE = "postgres@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
S3MOCK_IMAGE = (
    "adobe/s3mock@sha256:e7c36014dcf4c7f0f6bec9de888477d1d9b8f55eceb7d9c1186f70d7aadf81ca"
)
MINIO_IMAGE = "docker.io/pgsty/minio@sha256:b6bfe7239bfc83fb90d31612d9704d86039dd714f7904b3f1ad68f211e602372"

PG_PASSWORD = "alkera-test"
# Obviously local, never a realistic key format (repo rule), and long enough to
# clear MinIO's eight-character minimum.
MINIO_USER = "alkera-local-dev"
MINIO_SECRET = "alkera-local-dev-secret"

START_TIMEOUT_S = 90
# The webhook round-trip crosses Docker Desktop's VM; a loaded CI host has
# measured beyond 20 s. The wait is a polling bound, not a latency assertion.
WEBHOOK_TIMEOUT_S = 60

SEED = (
    "CREATE SCHEMA IF NOT EXISTS warehouse",
    "CREATE TABLE IF NOT EXISTS warehouse.items(id INTEGER, amount DECIMAL(10,2))",
    "INSERT INTO warehouse.items VALUES (1, 10.50), (2, 20.25)",
)

SELECT_SQL = "SELECT id, amount FROM warehouse.items ORDER BY id"
UPDATE_SQL = "UPDATE warehouse.items SET amount = 12.75 WHERE id = 1"
BUCKET = "episode"
KEY = "exports/report.csv"
BODY = b"dt,revenue\n2026-03-05,1200\n"
EDITED_FILE = "docs/runbook.md"
EDITED_CONTENT = "The daily load is idempotent. Re-running it is safe.\n"

ROLLOUTS = Path(os.environ.get("TEMP", "")) / "alkera-rollouts"


def docker(*args, timeout=300):
    """`docker` with an explicit codec: this host's default is not UTF-8."""
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout
    )


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def published_port(container, port):
    mapped = docker("port", container, f"{port}/tcp", timeout=30)
    lines = mapped.stdout.strip().splitlines()
    if mapped.returncode or not lines:
        return None
    return int(lines[0].rsplit(":", 1)[1])


def wait_for(probe, timeout_s):
    """Call `probe` until it stops raising; return the last failure, or None."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            probe()
            return None
        except Exception as exc:  # noqa: BLE001 - the reason is what we report
            last = exc
            time.sleep(0.5)
    return last


def s3_client(endpoint, key, secret):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        config=Config(
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=20,
            retries={"max_attempts": 0},
        ),
    )


@pytest.fixture(scope="session")
def docker_daemon():
    """A usable Docker daemon, or a skip naming what was missing."""
    if shutil.which("docker") is None:
        pytest.skip("native audit not measured: no `docker` on PATH")
    try:
        version = docker("version", "--format", "{{.Server.Version}}", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"native audit not measured: docker is unusable ({exc})")
    if version.returncode:
        pytest.skip(
            "native audit not measured: no docker daemon "
            f"({version.stderr.strip().splitlines()[:1]})"
        )
    return version.stdout.strip()


@pytest.fixture(scope="session")
def logged_postgres(docker_daemon):
    """A Postgres started the way native mode starts it: every statement logged.

    `log_min_duration_statement=0` is what puts a `duration:` entry after each
    statement, and that entry is the only source of a real `latency_ms` on the
    native path - the client is never asked.
    """
    started = docker(
        "run", "-d", "--rm",
        "-p", "127.0.0.1::5432",
        "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
        "-e", "POSTGRES_DB=episode",
        PG_IMAGE,
        "-c", "log_statement=all",
        "-c", "log_min_duration_statement=0",
        "-c", "log_line_prefix=%m [%p] ",
    )
    if started.returncode:
        pytest.skip(f"native audit not measured: postgres would not start: "
                    f"{started.stderr.strip()[:300]}")
    container = started.stdout.strip()
    try:
        port = published_port(container, 5432)
        if port is None:
            pytest.skip("native audit not measured: docker published no host port for postgres")
        dsn = f"postgresql://postgres:{PG_PASSWORD}@127.0.0.1:{port}/episode"

        import psycopg

        def connect():
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                conn.execute("SELECT 1")

        failure = wait_for(connect, START_TIMEOUT_S)
        if failure is not None:
            pytest.skip(f"native audit not measured: postgres never accepted a connection "
                        f"within {START_TIMEOUT_S}s ({failure})")
        with psycopg.connect(dsn, autocommit=True) as conn:
            for statement in SEED:
                conn.execute(statement)
        yield container
    finally:
        docker("rm", "-f", container, timeout=60)


def psql(container, *statements):
    """Real psql, as a subprocess, against the logged server.

    The client runs inside the server's container when the host has no psql,
    which every Windows developer machine here is. It is the same binary talking
    the same protocol either way, and the row is produced by the server's log
    rather than by the client, which is the whole point.
    """
    arguments = []
    for statement in statements:
        arguments += ["-c", statement]
    if shutil.which("psql"):
        port = published_port(container, 5432)
        return subprocess.run(
            ["psql", f"postgresql://postgres:{PG_PASSWORD}@127.0.0.1:{port}/episode",
             "-v", "ON_ERROR_STOP=0", *arguments],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
    return docker(
        "exec", "-e", f"PGPASSWORD={PG_PASSWORD}", container,
        "psql", "-U", "postgres", "-d", "episode", "-v", "ON_ERROR_STOP=0", *arguments,
        timeout=120,
    )


def server_log(container):
    """The container's log. Postgres writes its statement log to stderr, so both
    streams are read; concatenating them is safe because only one is ever used."""
    captured = docker("logs", container)
    return captured.stdout.splitlines() + captured.stderr.splitlines()


@pytest.fixture(scope="session")
def s3mock(docker_daemon):
    """The pinned adobe/s3mock the MCP leg puts its object into."""
    started = docker("run", "-d", "--rm", "-p", "127.0.0.1::9090", S3MOCK_IMAGE)
    if started.returncode:
        pytest.skip(f"native audit not measured: s3mock would not start: "
                    f"{started.stderr.strip()[:300]}")
    container = started.stdout.strip()
    try:
        port = published_port(container, 9090)
        if port is None:
            pytest.skip("native audit not measured: docker published no host port for s3mock")
        endpoint = f"http://127.0.0.1:{port}"
        client = s3_client(endpoint, "episode", "episode")
        failure = wait_for(client.list_buckets, START_TIMEOUT_S)
        if failure is not None:
            pytest.skip(f"native audit not measured: s3mock did not answer on {endpoint} "
                        f"within {START_TIMEOUT_S}s ({failure})")
        client.create_bucket(Bucket=BUCKET)
        yield endpoint
    finally:
        docker("rm", "-f", container, timeout=60)


@pytest.fixture(scope="session")
def minio(docker_daemon):
    """MinIO with its audit webhook aimed at an in-process receiver.

    The container reaches the test process through `host.docker.internal`, whose
    IPv4 address is resolved from inside a container rather than assumed:
    `--add-host ...:host-gateway` publishes an AAAA record too and MinIO picked
    it, then failed with `network is unreachable` (measured, 2026-09-16).
    """
    import uvicorn

    resolved = docker("run", "--rm", "--entrypoint", "getent", MINIO_IMAGE,
                      "hosts", "host.docker.internal", timeout=120)
    if resolved.returncode or not resolved.stdout.split():
        pytest.skip("native audit not measured: cannot resolve host.docker.internal from a "
                    f"container, so MinIO cannot reach the receiver ({resolved.stderr[:200]})")
    host_ip = resolved.stdout.split()[0]

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(minio_tailer.build_app(), host="0.0.0.0", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    failure = wait_for(lambda: socket.create_connection(("127.0.0.1", port), 2).close(), 15)
    if failure is not None:
        server.should_exit = True
        pytest.skip(f"native audit not measured: the audit receiver never bound {port} "
                    f"({failure})")

    started = docker(
        "run", "-d", "--rm",
        "-p", "127.0.0.1::9000",
        "-e", f"MINIO_ROOT_USER={MINIO_USER}",
        "-e", f"MINIO_ROOT_PASSWORD={MINIO_SECRET}",
        "-e", "MINIO_AUDIT_WEBHOOK_ENABLE_alkera=on",
        "-e", f"MINIO_AUDIT_WEBHOOK_ENDPOINT_alkera=http://alkera-audit-host:{port}/audit",
        "--add-host", f"alkera-audit-host:{host_ip}",
        MINIO_IMAGE, "server", "/data",
    )
    if started.returncode:
        server.should_exit = True
        pytest.skip(f"native audit not measured: MinIO would not start: "
                    f"{started.stderr.strip()[:300]}")
    container = started.stdout.strip()
    try:
        mapped = published_port(container, 9000)
        if mapped is None:
            pytest.skip("native audit not measured: docker published no host port for MinIO")
        endpoint = f"http://127.0.0.1:{mapped}"
        client = s3_client(endpoint, MINIO_USER, MINIO_SECRET)
        failure = wait_for(client.list_buckets, START_TIMEOUT_S)
        if failure is not None:
            pytest.skip(f"native audit not measured: MinIO did not answer on {endpoint} "
                        f"within {START_TIMEOUT_S}s ({failure})")
        client.create_bucket(Bucket=BUCKET)
        yield endpoint
    finally:
        docker("rm", "-f", container, timeout=60)
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def native_log(tmp_path, monkeypatch):
    """A private action log for one test, and no ambient audit directory."""
    path = tmp_path / "native-audit" / "actions.jsonl"
    monkeypatch.delenv("ALKERA_AUDIT_DIR", raising=False)
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(path))
    monkeypatch.setenv("ALKERA_VTS", "2026-03-05T09:00:00Z")
    return path


def wait_for_rows(path, count, timeout_s=WEBHOOK_TIMEOUT_S):
    """Block until the log holds `count` rows; the webhook is asynchronous."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if audit.count_rows(path) >= count:
            return True
        time.sleep(0.2)
    return False


def recorded_mcp_row():
    """One row from a recorded rollout on this machine, or None."""
    for path in sorted(ROLLOUTS.glob("*/*/r0/audit/actions.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                return json.loads(line)
    return None


def targets_in(path):
    return {target for row in read_rows(path) for target in row["targets"]}


# --------------------------------------------------------------------------
# (a) shape


def test_row_keys_match_an_mcp_row(gateway, native_log):
    """A native row and an MCP row carry the same 23 keys - no more, no fewer."""
    recorded = recorded_mcp_row()
    if recorded is None:
        gateway.call("fs_list", {"path": "."})
        recorded = read_rows(gateway.audit.actions)[0]
    native = audit.record(
        tool="psql.statement", args={"sql": SELECT_SQL}, op_class="read",
        targets=["warehouse.items"], actor="postgres-tailer",
    )
    assert set(native) == set(recorded)
    assert set(native) == set(audit.ROW_KEYS)


def test_every_native_writer_produces_a_valid_trace(native_log, tmp_path, monkeypatch):
    """Rows from all three tailers and the shim, in one file, pass validate_trace."""
    postgres_tailer.run(SAMPLE_POSTGRES_LOG)
    minio_tailer.row_from_event(json.loads(SAMPLE_MINIO_EVENT))
    airflow_tailer.run(SAMPLE_AIRFLOW_LOG)
    bin_dir = tmp_path / "bin"
    shim_install.install(bin_dir, ("python",))
    monkeypatch.setenv("ALKERA_SHIM_BIN", str(bin_dir))
    monkeypatch.setenv("ALKERA_SHIM_WORKSPACE", str(tmp_path / "empty"))
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent),
                                 os.environ["PATH"]])
    )
    shim_run.main(["python", "-c", "pass"])

    rows = validate_trace(native_log)
    assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))
    assert all(set(row) == set(audit.ROW_KEYS) for row in rows)
    assert {row["tool"] for row in rows} >= {
        "psql.statement", "minio.PutObject", "airflow.dag_trigger", "shim.python"
    }


def test_record_refuses_a_directory_a_gateway_holds(gateway, monkeypatch):
    """Two sequence allocators on one log is corruption; the writer says so."""
    monkeypatch.setattr(audit, "LOCK_TIMEOUT_S", 0.2)
    monkeypatch.delenv("ALKERA_NATIVE_AUDIT", raising=False)
    monkeypatch.setenv("ALKERA_AUDIT_DIR", str(gateway.audit.directory))
    with pytest.raises(RuntimeError, match="held by another writer"):
        audit.record(tool="psql.statement", args={}, op_class="read", targets=[])


def test_record_says_so_when_it_has_nowhere_to_write(monkeypatch):
    """No audit directory is reported in the row, never silently dropped."""
    monkeypatch.delenv("ALKERA_NATIVE_AUDIT", raising=False)
    monkeypatch.delenv("ALKERA_AUDIT_DIR", raising=False)
    row = audit.record(tool="psql.statement", args={}, op_class="read", targets=[])
    assert row["flags"] == ["unrecorded"] and row["seq"] == 0


# --------------------------------------------------------------------------
# tailers and shims against the real thing


def test_postgres_tailer_reproduces_a_psql_session(logged_postgres, native_log):
    """Statements sent by a real psql come back as rows, classified by sql.py."""
    before = len(server_log(logged_postgres))
    marker = f"warehouse.items_{uuid.uuid4().hex[:8]}"
    psql(
        logged_postgres,
        SELECT_SQL,
        UPDATE_SQL,
        f"CREATE TABLE warehouse.{marker}(id INTEGER)",
        f"DROP TABLE warehouse.{marker}",
        "SELECT * FROM warehouse.absent",
    )
    rows = postgres_tailer.run(server_log(logged_postgres)[before:])
    by_class = {row["args"]["sql"]: row for row in rows}
    assert by_class[SELECT_SQL]["op_class"] == "read"
    assert by_class[SELECT_SQL]["targets"] == ["warehouse.items"]
    assert by_class[SELECT_SQL]["latency_ms"] > 0
    assert by_class[UPDATE_SQL]["op_class"] == "write"
    assert by_class[f"CREATE TABLE warehouse.{marker}(id INTEGER)"]["op_class"] == "ddl"
    assert by_class[f"DROP TABLE warehouse.{marker}"]["op_class"] == "destructive"
    failed = by_class["SELECT * FROM warehouse.absent"]
    assert failed["exit"] == "error" and failed["error"]["code"] == "sql_error"


def test_postgres_tailer_handles_what_sql_py_refuses(logged_postgres, native_log):
    """psql's meta-commands, COPY and a duration-only log line.

    `\\d` is eight catalog SELECTs the agent never typed, so they are flagged
    `catalog_only` rather than dropped; `COPY ... TO` reads where the verb table
    says write; and `duration: N ms  statement: ...` - what the server writes
    when only `log_min_duration_statement` covers a statement - is a statement,
    not a duration to be discarded.
    """
    before = len(server_log(logged_postgres))
    psql(
        logged_postgres,
        "\\d warehouse.items",
        "COPY warehouse.items TO STDOUT",
        "COPY (SELECT * FROM warehouse.items) TO STDOUT",
    )
    rows = postgres_tailer.run(server_log(logged_postgres)[before:])
    catalog = [row for row in rows if "catalog_only" in row["flags"]]
    copies = [row for row in rows if row["args"]["sql"].startswith("COPY")]
    assert len(catalog) >= 5, "psql's own catalog queries were not flagged"
    assert all(row["op_class"] == "read" for row in catalog)
    assert [row["op_class"] for row in copies] == ["read", "read"]
    assert all(row["targets"] == ["warehouse.items"] for row in copies)

    combined = postgres_tailer.run(
        ["2026-09-16 22:49:49.608 UTC [89] LOG:  duration: 0.773 ms  "
         "statement: SELECT count(*) FROM warehouse.items"]
    )
    assert len(combined) == 1
    assert combined[0]["latency_ms"] == 0.773
    assert combined[0]["targets"] == ["warehouse.items"]


def test_minio_tailer_records_what_boto3_did(minio, native_log):
    """The server's own audit webhook, not the client, produces the rows."""
    client = s3_client(minio, MINIO_USER, MINIO_SECRET)
    client.put_object(Bucket=BUCKET, Key=KEY, Body=BODY)
    client.get_object(Bucket=BUCKET, Key=KEY)["Body"].read()
    client.delete_object(Bucket=BUCKET, Key=KEY)
    assert wait_for_rows(native_log, 3), "MinIO posted no audit entries"
    rows = {row["tool"]: row for row in read_rows(native_log)}
    assert rows["minio.PutObject"]["op_class"] == "write"
    assert rows["minio.PutObject"]["targets"] == [f"s3://{BUCKET}/{KEY}"]
    assert rows["minio.GetObject"]["op_class"] == "read"
    assert rows["minio.DeleteObject"]["op_class"] == "destructive"
    assert rows["minio.DeleteObject"]["flags"][:1] == ["delete"]


def test_airflow_tailer_routes_a_real_access_log(native_log):
    """The three lines below were emitted by Airflow 3.0.2; see native/PARITY.md."""
    rows = airflow_tailer.run(SAMPLE_AIRFLOW_LOG)
    assert [row["tool"] for row in rows] == [
        "airflow.dag_list", "airflow.dag_get", "airflow.dag_trigger"
    ]
    assert rows[2]["targets"] == ["scheduler:vendor_feeds", "dbt_project"]
    assert rows[1]["exit"] == "error"
    # /auth/token is not part of the REST surface and must not become a row.
    assert len(rows) == len(SAMPLE_AIRFLOW_LOG) - 1


def test_shim_names_the_files_the_binary_changed(world, native_log, tmp_path, monkeypatch):
    """A binary that names no target on its command line still gets targets."""
    bin_dir = tmp_path / "bin"
    written = shim_install.install(bin_dir, ("python",))
    assert bin_dir / "python" in written
    workspace = world / "workspace"
    monkeypatch.setenv("ALKERA_SHIM_BIN", str(bin_dir))
    monkeypatch.setenv("ALKERA_SHIM_WORKSPACE", str(workspace))
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent),
                                 os.environ["PATH"]])
    )
    wrapper = bin_dir / ("python.cmd" if os.name == "nt" else "python")
    completed = subprocess.run(
        [str(wrapper), "-c",
         f"import pathlib;pathlib.Path({EDITED_FILE!r}).write_text({EDITED_CONTENT!r})"],
        cwd=workspace, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    row = read_rows(native_log)[-1]
    assert row["tool"] == "shim.python" and row["op_class"] == "exec"
    assert row["targets"] == [EDITED_FILE]
    assert row["latency_ms"] > 0


def test_shim_records_a_binary_it_cannot_find(native_log, tmp_path, monkeypatch):
    """A missing binary is an audited failure, not a silent 127."""
    monkeypatch.setenv("ALKERA_SHIM_BIN", str(tmp_path))
    monkeypatch.setenv("ALKERA_SHIM_WORKSPACE", str(tmp_path))
    assert shim_run.main([f"absent-{uuid.uuid4().hex[:8]}"]) == 127
    row = read_rows(native_log)[-1]
    assert row["exit"] == "error" and row["error"]["code"] == "binary_missing"


# --------------------------------------------------------------------------
# (b) target-set parity


def test_target_sets_are_identical_over_mcp_and_native(
    logged_postgres, minio, s3mock, world, tmp_path, native_log
):
    """The Phase 1 acceptance: one sequence, two transports, one target set."""
    import base64

    mcp_audit = tmp_path / "mcp-audit"
    gateway = Gateway(
        world_config(world, mcp_audit, s3_endpoint=s3mock, s3_buckets=[BUCKET])
    )
    try:
        for tool, args in (
            ("sql_query", {"sql": SELECT_SQL}),
            ("sql_execute", {"sql": UPDATE_SQL}),
            ("s3_put", {"bucket": BUCKET, "key": KEY,
                        "body_base64": base64.b64encode(BODY).decode()}),
            ("fs_write", {"path": EDITED_FILE, "content": EDITED_CONTENT}),
        ):
            answer = gateway.call(tool, args)
            assert answer["ok"], (tool, answer["error"])
    finally:
        gateway.close()
    mcp_targets = targets_in(gateway.audit.actions)

    native_world = clone_world(world, tmp_path / "native-world")
    before = len(server_log(logged_postgres))
    psql(logged_postgres, SELECT_SQL, UPDATE_SQL)
    postgres_tailer.run(server_log(logged_postgres)[before:])

    s3_client(minio, MINIO_USER, MINIO_SECRET).put_object(Bucket=BUCKET, Key=KEY, Body=BODY)
    expected = audit.count_rows(native_log) + 1
    assert wait_for_rows(native_log, expected), "MinIO posted no audit entry for the put"

    bin_dir = tmp_path / "bin"
    shim_install.install(bin_dir, ("python",))
    environment = {
        **os.environ,
        "ALKERA_SHIM_BIN": str(bin_dir),
        "ALKERA_SHIM_WORKSPACE": str(native_world / "workspace"),
        "PATH": os.pathsep.join(
            [str(bin_dir), str(Path(sys.executable).parent), os.environ["PATH"]]
        ),
    }
    completed = subprocess.run(
        [str(bin_dir / ("python.cmd" if os.name == "nt" else "python")), "-c",
         f"import pathlib;pathlib.Path({EDITED_FILE!r}).write_text({EDITED_CONTENT!r})"],
        cwd=native_world / "workspace", env=environment,
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert completed.returncode == 0, completed.stderr

    native_targets = targets_in(native_log)
    assert native_targets == mcp_targets
    assert native_targets == {"warehouse.items", f"s3://{BUCKET}/{KEY}", EDITED_FILE}


# --------------------------------------------------------------------------
# Captured server output. Every line below was emitted by the real server named
# in its comment on 2026-09-16; the commands that produced them are in
# native/PARITY.md, so these literals can be regenerated rather than trusted.

# postgres 16.15, `log_statement=all log_min_duration_statement=0`
SAMPLE_POSTGRES_LOG = (
    "2026-09-16 22:34:26.471 UTC [82] LOG:  statement: SELECT id, amount FROM "
    "warehouse.items ORDER BY id",
    "2026-09-16 22:34:26.472 UTC [82] LOG:  duration: 1.135 ms",
    "2026-09-16 22:34:26.472 UTC [82] LOG:  statement: UPDATE warehouse.items SET "
    "amount = 12.75 WHERE id = 1",
    "2026-09-16 22:34:26.475 UTC [82] LOG:  duration: 2.241 ms",
    "2026-09-16 22:34:26.475 UTC [82] LOG:  statement: SELECT * FROM warehouse.nope",
    '2026-09-16 22:34:26.475 UTC [82] ERROR:  relation "warehouse.nope" does not '
    "exist at character 15",
    "2026-09-16 22:34:26.475 UTC [82] STATEMENT:  SELECT * FROM warehouse.nope",
    "2026-09-16 22:34:26.475 UTC [82] LOG:  statement: SET statement_timeout = 0",
    "2026-09-16 22:34:26.475 UTC [82] LOG:  duration: 0.066 ms",
)

# MinIO RELEASE.2025-09-07T16-13-09Z, audit webhook
SAMPLE_MINIO_EVENT = json.dumps(
    {
        "version": "1",
        "api": {"name": "PutObject", "bucket": BUCKET, "object": KEY, "status": "OK",
                "statusCode": 200, "rx": len(BODY), "tx": 0,
                "timeToResponseInNS": "9890641"},
        "requestPath": f"/{BUCKET}/{KEY}",
        "requestQuery": None,
        "userAgent": "Boto3/1.43.93",
    }
)

# Airflow 3.0.2 api-server (uvicorn access log on stdout)
SAMPLE_AIRFLOW_LOG = (
    'INFO:     127.0.0.1:38496 - "POST /auth/token HTTP/1.1" 201 Created',
    'INFO:     127.0.0.1:38508 - "GET /api/v2/dags HTTP/1.1" 200 OK',
    'INFO:     127.0.0.1:40750 - "GET /api/v2/dags/nope HTTP/1.1" 404 Not Found',
    'INFO:     127.0.0.1:40758 - "POST /api/v2/dags/vendor_feeds/dagRuns HTTP/1.1" '
    "422 Unprocessable Entity",
)
