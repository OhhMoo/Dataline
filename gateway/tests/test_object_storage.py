"""s3_list / s3_get / s3_put / s3_delete, exercised against a real adobe/s3mock.

These four tools shipped registered in `tools.py` with no test at all. A registered
tool with no test looks finished from every angle that does not execute it, and when
it fails inside a rollout the harness cannot attribute the failure to a dead surface
- it lands on the agent as `reasoning`, which corrupts the measurement the benchmark
exists to produce.

Every assertion below is on the CONTENT of the result. `result["ok"]` alone is the
exact failure mode being guarded against: a tool that returns an empty success.

The container is pinned to the same digest as `gateway/docker/compose.yml`. If it
cannot be started the tests SKIP with a reason naming what was unavailable - never
pass without touching the surface.
"""

import base64
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import boto3
import pytest
from botocore.config import Config
from conftest import config as world_config
from conftest import make_world

from alkera_gateway import Gateway
from alkera_gateway.audit import read_rows, validate_trace
from alkera_gateway.records import digest

# Same digest as the `s3` service in gateway/docker/compose.yml. Pinned, not :latest,
# so a test that passes today is testing the same binary a rollout talks to.
S3MOCK_IMAGE = (
    "adobe/s3mock@sha256:e7c36014dcf4c7f0f6bec9de888477d1d9b8f55eceb7d9c1186f70d7aadf81ca"
)
# T1 (R002): "adobe/S3Mock is a JVM service and binds slower than an eager loop expects."
READY_TIMEOUT_S = 90


def _run(*args, timeout=120):
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        args, capture_output=True, text=True, timeout=timeout
    )


def _wait_for_http(url, deadline):
    last = "no attempt made"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - fixed http URL
                if response.status == 200:
                    return None
                last = f"HTTP {response.status}"
        except (urllib.error.URLError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)
    return last


@pytest.fixture(scope="session")
def s3_endpoint():
    """A reachable S3 endpoint, or a skip that says exactly what was missing."""
    external = os.environ.get("ALKERA_TEST_S3_ENDPOINT")
    if external:
        yield external
        return

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip(
            "object storage not exercised: no `docker` on PATH and ALKERA_TEST_S3_ENDPOINT is unset"
        )

    started = _run(docker, "run", "-d", "--rm", "-p", "9090", S3MOCK_IMAGE, timeout=300)
    if started.returncode != 0:
        pytest.skip(
            f"object storage not exercised: `docker run {S3MOCK_IMAGE}` failed: "
            f"{started.stderr.strip()[:300]}"
        )
    container = started.stdout.strip()
    try:
        mapped = _run(docker, "port", container, "9090/tcp")
        if mapped.returncode != 0 or not mapped.stdout.strip():
            pytest.skip(
                "object storage not exercised: s3mock started but published no host port "
                f"for 9090/tcp ({mapped.stderr.strip()[:200]})"
            )
        port = mapped.stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        endpoint = f"http://127.0.0.1:{port}"
        failure = _wait_for_http(f"{endpoint}/", time.monotonic() + READY_TIMEOUT_S)
        if failure is not None:
            pytest.skip(
                f"object storage not exercised: s3mock did not answer on {endpoint} within "
                f"{READY_TIMEOUT_S}s (last: {failure})"
            )
        yield endpoint
    finally:
        _run(docker, "rm", "-f", container)


def _admin(endpoint):
    """Out-of-band client. Buckets are world fixtures, so no gateway tool creates one -
    the same way make_world() seeds the warehouse with duckdb directly."""
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="episode",
        aws_secret_access_key="episode",
        config=Config(
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=20,
            retries={"max_attempts": 0},
        ),
    )


@pytest.fixture
def s3_bucket(s3_endpoint):
    """A bucket per test, so tests cannot see each other's objects.

    NOTE this does NOT use the image's `initialBuckets` env var. On the pinned digest
    that variable is accepted and silently ignored - see the report for B2/B4.
    """
    client = _admin(s3_endpoint)
    name = f"landing-{uuid.uuid4().hex[:12]}"
    client.create_bucket(Bucket=name)
    assert name in [b["Name"] for b in client.list_buckets().get("Buckets", [])]
    yield name
    try:
        contents = client.list_objects_v2(Bucket=name).get("Contents", [])
        for item in contents:
            client.delete_object(Bucket=name, Key=item["Key"])
        client.delete_bucket(Bucket=name)
    except Exception:  # noqa: BLE001 - teardown must not mask a test failure
        pass


@pytest.fixture
def s3_gateway(tmp_path, s3_endpoint, s3_bucket):
    world = make_world(tmp_path / "world")
    instance = Gateway(
        world_config(
            world,
            tmp_path / "audit",
            s3_endpoint=s3_endpoint,
            s3_buckets=[s3_bucket],
        )
    )
    yield instance
    instance.close()


def assert_ok(result):
    assert result["ok"], result["error"]
    return result["result"]


def rows_for(gateway, tool):
    return [row for row in read_rows(gateway.audit.actions) if row["tool"] == tool]


def test_object_storage_round_trip(s3_gateway, s3_bucket):
    """put -> get -> list -> delete -> get fails. Content asserted at every step."""
    body = b"order_id,amount\n00417,10.50\n"
    encoded = base64.b64encode(body).decode()

    put = assert_ok(
        s3_gateway.call(
            "s3_put",
            {
                "bucket": s3_bucket,
                "key": "incoming/orders.csv",
                "body_base64": encoded,
                "metadata": {"source": "vendor-a"},
            },
        )
    )
    assert put["key"] == "incoming/orders.csv"
    assert put["bytes_written"] == len(body)
    assert len(put["etag"]) == 32 and put["etag"].isalnum()

    got = assert_ok(s3_gateway.call("s3_get", {"bucket": s3_bucket, "key": "incoming/orders.csv"}))
    # The round trip is the point: same bytes out as in, not merely "a success".
    assert base64.b64decode(got["body_base64"]) == body
    assert got["etag"] == put["etag"]
    assert got["metadata"]["source"] == "vendor-a"

    listed = assert_ok(s3_gateway.call("s3_list", {"bucket": s3_bucket}))
    assert [item["key"] for item in listed["objects"]] == ["incoming/orders.csv"]
    assert listed["objects"][0]["size"] == len(body)
    assert listed["objects"][0]["etag"] == put["etag"]
    assert listed["continuation_token"] is None

    deleted = assert_ok(
        s3_gateway.call("s3_delete", {"bucket": s3_bucket, "key": "incoming/orders.csv"})
    )
    assert deleted == {"key": "incoming/orders.csv", "deleted": True}

    # Delete really removed it - both the read and the listing must agree.
    gone = s3_gateway.call("s3_get", {"bucket": s3_bucket, "key": "incoming/orders.csv"})
    assert gone["ok"] is False
    assert gone["error"]["code"] == "storage_error"
    assert gone["error"]["message"] == "NoSuchKey"
    assert assert_ok(s3_gateway.call("s3_list", {"bucket": s3_bucket}))["objects"] == []


def test_object_storage_preserves_bytes_that_are_not_utf8(s3_gateway, s3_bucket):
    """The encoding failure class, checked on the storage surface.

    fs_read/fs_write were fixed to fail loudly on a bad decode rather than substitute
    U+FFFD, and docs_search was then found to have the same hole one module away. The
    object surface must not have it either: a mainframe extract or a cp1252 export that
    goes into a bucket has to come back byte-identical, because a benchmark that plants
    encoding defects cannot have its own toolbelt sanitising them away.

    s3_get is structurally safe here - it base64s the bytes and never calls .decode() -
    and this test is what holds that property still if anyone "helpfully" adds a text
    convenience later.
    """
    ebcdic = "CUSTOMER 00417".encode("cp037")
    cp1252 = "name,city\nJos\xe9,M\xfcnchen\n".encode("cp1252")
    binary = bytes(range(256))
    for key, raw in (("extract.dat", ebcdic), ("export.csv", cp1252), ("all.bin", binary)):
        assert_ok(
            s3_gateway.call(
                "s3_put",
                {
                    "bucket": s3_bucket,
                    "key": key,
                    "body_base64": base64.b64encode(raw).decode(),
                    "metadata": {},
                },
            )
        )
        got = assert_ok(s3_gateway.call("s3_get", {"bucket": s3_bucket, "key": key}))
        assert base64.b64decode(got["body_base64"]) == raw, key
        # No lossy path anywhere: the replacement character never appears.
        assert "�" not in got["body_base64"]

    # And the bytes are still the right bytes once decoded with the right codec.
    recovered = assert_ok(s3_gateway.call("s3_get", {"bucket": s3_bucket, "key": "export.csv"}))
    text = base64.b64decode(recovered["body_base64"]).decode("cp1252")
    assert "José" in text and "München" in text


def test_s3_get_refuses_an_oversized_object_loudly(tmp_path, s3_endpoint, s3_bucket):
    """An object above the payload limit must error, not arrive truncated.

    A silently truncated CSV is the worst outcome in this benchmark: the agent gets
    plausible-looking wrong data with no signal that anything was dropped.
    """
    world = make_world(tmp_path / "world")
    limit = 2048
    gateway = Gateway(
        world_config(
            world,
            tmp_path / "audit",
            s3_endpoint=s3_endpoint,
            s3_buckets=[s3_bucket],
            max_payload_bytes=limit,
        )
    )
    try:
        # Seeded out of band, because s3_put's own request would hit the request gate.
        _admin(s3_endpoint).put_object(Bucket=s3_bucket, Key="big.csv", Body=b"x" * (limit + 64))
        result = gateway.call("s3_get", {"bucket": s3_bucket, "key": "big.csv"})
        assert result["ok"] is False
        assert result["error"]["code"] == "object_too_large"
        assert result["result"] is None
        row = rows_for(gateway, "s3_get")[0]
        assert row["exit"] == "error"
        assert row["op_class"] == "read"
    finally:
        gateway.close()


def test_s3_list_filters_by_prefix_and_pages(s3_gateway, s3_bucket):
    """prefix and limit must actually do something. An ignored filter returns a
    plausible answer that is wrong, which no `ok` check would ever catch."""
    keys = ["incoming/a.csv", "incoming/b.csv", "incoming/c.csv", "archive/old.csv"]
    for key in keys:
        assert_ok(
            s3_gateway.call(
                "s3_put",
                {
                    "bucket": s3_bucket,
                    "key": key,
                    "body_base64": base64.b64encode(key.encode()).decode(),
                    "metadata": {},
                },
            )
        )

    filtered = assert_ok(s3_gateway.call("s3_list", {"bucket": s3_bucket, "prefix": "incoming/"}))
    assert [item["key"] for item in filtered["objects"]] == [
        "incoming/a.csv",
        "incoming/b.csv",
        "incoming/c.csv",
    ]
    assert filtered["continuation_token"] is None

    first = assert_ok(
        s3_gateway.call("s3_list", {"bucket": s3_bucket, "prefix": "incoming/", "limit": 2})
    )
    assert [item["key"] for item in first["objects"]] == ["incoming/a.csv", "incoming/b.csv"]
    # A truncated page must hand back the token, or the rest of the bucket is
    # invisible with no sign that anything is missing.
    assert first["continuation_token"]

    second = assert_ok(
        s3_gateway.call(
            "s3_list",
            {
                "bucket": s3_bucket,
                "prefix": "incoming/",
                "limit": 2,
                "continuation_token": first["continuation_token"],
            },
        )
    )
    assert [item["key"] for item in second["objects"]] == ["incoming/c.csv"]
    assert second["continuation_token"] is None


def test_object_storage_writes_wellformed_action_log_rows(s3_gateway, s3_bucket):
    """agent.md 9.2.2 - one row per call, and op_class lives in the LOG, not the response.

    s3_delete must classify as `destructive`: ground rule 5 says destructive operations
    are flagged and allowed, and whether the agent gates its own destructive actions is
    a measurement. A delete logged as `read` silently erases that measurement.
    """
    key = "incoming/orders.csv"
    body_b64 = base64.b64encode(b"a,b\n1,2\n").decode()
    calls = [
        ("s3_list", {"bucket": s3_bucket}),
        ("s3_put", {"bucket": s3_bucket, "key": key, "body_base64": body_b64, "metadata": {}}),
        ("s3_get", {"bucket": s3_bucket, "key": key}),
        ("s3_delete", {"bucket": s3_bucket, "key": key}),
    ]
    for tool, args in calls:
        assert_ok(s3_gateway.call(tool, args))

    rows = validate_trace(s3_gateway.audit.actions)  # checks seq contiguity and both hashes
    assert [row["seq"] for row in rows] == [1, 2, 3, 4]
    assert [row["tool"] for row in rows] == [tool for tool, _ in calls]
    assert [row["exit"] for row in rows] == ["ok"] * 4
    assert [row["op_class"] for row in rows] == ["read", "write", "read", "destructive"]

    by_tool = {row["tool"]: row for row in rows}
    assert by_tool["s3_delete"]["flags"] == ["delete"]
    assert by_tool["s3_delete"]["target"] == f"s3://{s3_bucket}/{key}"
    assert by_tool["s3_put"]["flags"] == ["possible_overwrite"]
    assert by_tool["s3_put"]["read_targets"] == []
    assert by_tool["s3_get"]["read_targets"] == [f"s3://{s3_bucket}/{key}"]
    assert by_tool["s3_list"]["target"] == f"s3://{s3_bucket}/"

    for row in rows:
        assert row["args_hash"] == digest(row["args"])
        assert row["args_hash"] != digest({})
        assert row["error"] is None
        # op_class is a log field, not a response field (9.2.2).
        assert "op_class" not in row["result"]

    # The put body is recoverable from the log, so a grader can replay the write.
    assert by_tool["s3_put"]["args"]["body_base64"] == body_b64


def test_object_storage_rejects_bad_input_cleanly(s3_gateway, s3_bucket, tmp_path):
    """Every rejection is a coded ToolError with a logged row, not a traceback."""
    denied = s3_gateway.call("s3_get", {"bucket": "someone-elses-bucket", "key": "x"})
    assert denied["ok"] is False
    assert denied["error"]["code"] == "bucket_denied"

    bad_base64 = s3_gateway.call(
        "s3_put",
        {"bucket": s3_bucket, "key": "x", "body_base64": "not base64!!", "metadata": {}},
    )
    assert bad_base64["ok"] is False
    assert bad_base64["error"]["code"] == "invalid_base64"
    # The rejected write must not have created anything.
    assert assert_ok(s3_gateway.call("s3_list", {"bucket": s3_bucket}))["objects"] == []

    missing = s3_gateway.call("s3_get", {"bucket": s3_bucket, "key": "never/written.csv"})
    assert missing["ok"] is False
    assert missing["error"]["code"] == "storage_error"
    assert missing["error"]["message"] == "NoSuchKey"

    unknown_arg = s3_gateway.call("s3_list", {"bucket": s3_bucket, "prefix2": "incoming/"})
    assert unknown_arg["ok"] is False
    assert unknown_arg["error"]["code"] == "invalid_arguments"

    rows = read_rows(s3_gateway.audit.actions)
    assert [row["exit"] for row in rows] == ["error", "error", "ok", "error", "error"]
    assert all(row["result"] is None for row in rows if row["exit"] == "error")


def test_storage_tools_report_a_world_without_object_storage(gateway):
    """The default fixture world has no s3_endpoint. Every storage tool must say so,
    rather than hanging on a connection attempt or returning an empty listing that
    reads like an empty bucket."""
    attempts = {
        "s3_list": {"bucket": "landing"},
        "s3_get": {"bucket": "landing", "key": "x"},
        "s3_put": {"bucket": "landing", "key": "x", "body_base64": "", "metadata": {}},
        "s3_delete": {"bucket": "landing", "key": "x"},
    }
    for tool, args in attempts.items():
        result = gateway.call(tool, args)
        assert result["ok"] is False, tool
        assert result["error"]["code"] == "storage_unconfigured", tool
    assert [row["op_class"] for row in read_rows(gateway.audit.actions)] == [
        "read",
        "read",
        "write",
        "destructive",
    ]
