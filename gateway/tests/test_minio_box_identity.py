"""The box's S3 identity is a scoped MinIO user, not MinIO's root.

gateway/docker/evidence/PEERS_2026-10-13.md: the box's AWS_ACCESS_KEY_ID /
AWS_SECRET_ACCESS_KEY (`alkera-local-dev`) was compose.native.yml's
MINIO_ROOT_USER / MINIO_ROOT_PASSWORD, so the agent owned MinIO's admin API
from the episode. `GET /minio/admin/v3/info` answered 200 to a botocore
signature, and `POST /minio/admin/v3/start-job` and a `notify_webhook` target
made MinIO dial any episode-network service: `jira.internal:8080` went from
count 0 to 4 in destinations.jsonl with no dial from the episode.

The fix keeps every box-facing value byte for byte. MinIO now starts through
gateway/docker/minio_start.sh, which
  - mints MinIO's root credential inside the minio container, per start, so it
    is in no compose file, no container config and no other container;
  - provisions the box's credential as an ordinary MinIO user holding
    gateway/docker/minio_box_policy.json (the world bucket's S3 data actions,
    and an explicit Deny on admin:* and kms:*), on a loopback-only server with
    no audit webhook, so provisioning is not a `minio.*` row;
  - then execs the real server, which `mc ready local` sees only once the box's
    user exists.

The static half reads the policy and the stanza as `docker compose config`
resolves it. The docker half runs that resolved stanza with `docker run`
against the pinned image and signs requests the way the PEERS note did. A skip
names what was missing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import boto3
import pytest
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import Credentials

DOCKER_DIR = Path(__file__).resolve().parents[1] / "docker"
POLICY = DOCKER_DIR / "minio_box_policy.json"
START = DOCKER_DIR / "minio_start.sh"
COMPOSE_FILES = ("compose.yml", "compose.episode.yml", "compose.native.yml")
#: What the box carries (compose.native.yml's &native-endpoints, the world's
#: .envrc / services.toml), and what it must keep carrying.
BOX_KEY = "alkera-local-dev"
BOX_SECRET = "alkera-local-dev"
BUCKET = "alkera"
REGION = "us-east-1"
#: The only peer the PEERS note saw counted: what a job or a target would dial.
JIRA = "http://jira.internal:8080"
READY_TIMEOUT_S = 120

#: The box's grants, exactly: the S3 data actions the world bucket needs (the
#: surface gate's HEAD + list, minio_containment_check's put/get/delete, and
#: what `aws s3 ls/cp/sync/mv/rm` and boto3 do, multipart included), plus
#: `aws s3 ls` with no bucket. Nothing on the bucket's configuration, no bucket
#: creation or deletion, and nothing outside `s3:`.
EXPECTED_ALLOW = {
    "arn:aws:s3:::*": {"s3:ListAllMyBuckets"},
    f"arn:aws:s3:::{BUCKET}": {"s3:GetBucketLocation", "s3:ListBucket",
                               "s3:ListBucketMultipartUploads"},
    f"arn:aws:s3:::{BUCKET}/*": {"s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                                 "s3:AbortMultipartUpload",
                                 "s3:ListMultipartUploadParts"},
}


# --------------------------------------------------------------------------
# the policy and the start script, read as files
def policy() -> dict:
    return json.loads(POLICY.read_text(encoding="utf-8"))


def test_box_policy_allows_only_the_worlds_s3_data_actions():
    """Allow statements grant EXPECTED_ALLOW and nothing else: no wildcard
    action, no admin or KMS action, no bucket configuration (notification,
    replication, lifecycle, policy, versioning) and no bucket create/delete."""
    document = policy()
    allowed: dict[str, set[str]] = {}
    for statement in document["Statement"]:
        if statement["Effect"] != "Allow":
            continue
        assert "Condition" not in statement and "NotAction" not in statement
        for resource in statement["Resource"]:
            allowed.setdefault(resource, set()).update(statement["Action"])
    assert allowed == EXPECTED_ALLOW
    actions = set().union(*allowed.values())
    assert all(a.startswith("s3:") and "*" not in a for a in actions)
    for forbidden in ("s3:PutBucketNotification", "s3:PutReplicationConfiguration",
                      "s3:PutLifecycleConfiguration", "s3:PutBucketPolicy",
                      "s3:PutBucketVersioning", "s3:CreateBucket", "s3:DeleteBucket",
                      "s3:ListenBucketNotification"):
        assert forbidden not in actions


def test_box_policy_denies_the_admin_api_explicitly():
    """An explicit Deny, not only the absence of an Allow: MinIO lets a user
    change its own secret and mint its own service accounts unless a policy
    DENIES admin:CreateUser / admin:CreateServiceAccount (measured on the
    pinned build: a self `add-user` is refused only with the Deny)."""
    denied = {action for statement in policy()["Statement"]
              if statement["Effect"] == "Deny" for action in statement["Action"]}
    assert {"admin:*", "kms:*"} <= denied


def test_start_script_parses():
    assert START.is_file()
    done = subprocess.run(["bash", "-n", str(START)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


# --------------------------------------------------------------------------
# the stanza, as compose resolves it
def compose_config(tmp_path: Path) -> dict:
    """`docker compose config` of the native stack (the notebook profile on, so
    `jupyter` is in it too). Needs the compose CLI, not a daemon."""
    if shutil.which("docker") is None:
        pytest.skip("compose config not read: no `docker` on PATH")
    argv = ["docker", "compose", "--profile", "notebook"]
    for name in COMPOSE_FILES:
        argv += ["-f", name]
    env = {**os.environ,
           "ALKERA_EPISODE_WORLD": str(tmp_path),
           "ALKERA_EPISODE_AUDIT": str(tmp_path),
           "ALKERA_EPISODE_SERVICE_STATE": str(tmp_path),
           "ALKERA_MINIO_AUDIT_TOKEN": "t8b-test-audit-token"}
    done = subprocess.run([*argv, "config", "--format", "json"], cwd=DOCKER_DIR,
                          capture_output=True, text=True, env=env, timeout=120)
    if done.returncode != 0 and "is not a docker command" in done.stderr:
        pytest.skip("compose config not read: no `docker compose` plugin")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def binds(service: dict) -> dict[str, dict]:
    return {v["target"]: v for v in service.get("volumes") or [] if v["type"] == "bind"}


def test_minio_stanza_names_no_root_credential(tmp_path):
    """MinIO's root is minted at start: no MINIO_ROOT_* (nor the old
    MINIO_ACCESS_KEY / MINIO_SECRET_KEY) in its environment, literal or
    interpolated. The box's credential reaches it as the user to provision,
    from the same values the episode carries."""
    services = compose_config(tmp_path)["services"]
    minio, episode = services["minio"], services["episode"]
    env = minio["environment"]
    for name in ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "MINIO_ACCESS_KEY",
                 "MINIO_SECRET_KEY"):
        assert name not in env, name
    assert env["ALKERA_MINIO_BOX_ACCESS_KEY"] == episode["environment"]["AWS_ACCESS_KEY_ID"]
    assert env["ALKERA_MINIO_BOX_SECRET_KEY"] == episode["environment"]["AWS_SECRET_ACCESS_KEY"]
    assert minio["entrypoint"] == ["sh", "/opt/alkera/minio/start.sh"]
    mounted = binds(minio)
    for target, source in (("/opt/alkera/minio/start.sh", START),
                           ("/opt/alkera/minio/box-policy.json", POLICY)):
        assert Path(mounted[target]["source"]) == source and mounted[target]["read_only"]
    # Readiness is still MinIO's own, and still only the real server's.
    assert minio["healthcheck"]["test"] == ["CMD", "mc", "ready", "local"]


def test_box_facing_values_are_unchanged(tmp_path):
    """The episode and the notebook keep exactly the S3 values they had, and
    gain no MinIO variable: nothing about the root, nothing about the policy."""
    services = compose_config(tmp_path)["services"]
    for name in ("episode", "jupyter"):
        env = services[name]["environment"]
        assert {k: env[k] for k in env if k.startswith("AWS_")} == {
            "AWS_ACCESS_KEY_ID": BOX_KEY, "AWS_SECRET_ACCESS_KEY": BOX_SECRET,
            "AWS_DEFAULT_REGION": REGION, "AWS_ENDPOINT_URL": "http://minio.internal:9000"}
        assert not [k for k in env if "MINIO" in k.upper()], name
        assert not set(binds(services[name])) & {"/opt/alkera/minio/start.sh",
                                                 "/opt/alkera/minio/box-policy.json"}


# --------------------------------------------------------------------------
# the stanza, run
def docker(*args, timeout=300, text=True):
    return subprocess.run(["docker", *args], capture_output=True, text=text,
                          timeout=timeout)


@pytest.fixture(scope="module")
def minio_service(tmp_path_factory):
    """compose's resolved `minio` service, and a usable daemon, or a skip."""
    if shutil.which("docker") is None:
        pytest.skip("box identity not measured: no `docker` on PATH")
    version = docker("version", "--format", "{{.Server.Version}}", timeout=30)
    if version.returncode:
        pytest.skip(f"box identity not measured: no docker daemon "
                    f"({version.stderr.strip()[:200]})")
    return compose_config(tmp_path_factory.mktemp("compose"))["services"]["minio"]


class Minio:
    """One MinIO started the way compose.native.yml starts it, on a published
    loopback port. Its audit webhook points at a port nothing answers, so each
    event it raises stays in its queue directory, where it can be counted."""

    SPOOL = "/audit-spool"

    def __init__(self, service: dict):
        self.service = service
        self.name = f"alkera-t8b-minio-{uuid.uuid4().hex[:10]}"
        self.endpoint = ""

    def start(self) -> Minio:
        env = {**self.service["environment"],
               "MINIO_AUDIT_WEBHOOK_ENDPOINT_alkera": "http://127.0.0.1:1/minio/audit",
               "MINIO_AUDIT_WEBHOOK_QUEUE_DIR_alkera": self.SPOOL}
        argv = ["run", "-d", "--name", self.name, "-p", "127.0.0.1::9000"]
        for key, value in sorted(env.items()):
            argv += ["-e", f"{key}={value}"]
        for target, bind in sorted(binds(self.service).items()):
            argv += ["-v", f"{bind['source']}:{target}" + (":ro" if bind["read_only"] else "")]
        entry = self.service["entrypoint"]
        argv += ["--entrypoint", entry[0], self.service["image"], *entry[1:],
                 *(self.service.get("command") or [])]
        started = docker(*argv)
        if started.returncode:
            pytest.skip(f"box identity not measured: MinIO would not start: "
                        f"{started.stderr.strip()[:300]}")
        self.wait_ready()
        return self

    def wait_ready(self) -> None:
        """compose's own healthcheck, until it passes."""
        test = self.service["healthcheck"]["test"]
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            done = docker("exec", self.name, *test[1:], timeout=60)
            if done.returncode == 0:
                # Read here: a restart publishes 9000 on a new host port.
                port = docker("port", self.name, "9000/tcp").stdout.split()[0]
                self.endpoint = f"http://127.0.0.1:{port.rsplit(':', 1)[1]}"
                return
            if time.monotonic() > deadline:
                logs = docker("logs", self.name)
                raise AssertionError(f"MinIO never became ready: {done.stdout[-300:]}"
                                     f"{done.stderr[-300:]}\n{logs.stdout[-2000:]}"
                                     f"{logs.stderr[-2000:]}")
            time.sleep(1)

    def stop(self) -> None:
        docker("rm", "-f", "-v", self.name, timeout=120)

    def restart(self) -> None:
        assert docker("restart", self.name, timeout=120).returncode == 0
        self.wait_ready()

    def root(self) -> dict[str, str]:
        """MINIO_ROOT_* as the server process holds them (its /proc/1/environ):
        the runtime value, which no container config carries."""
        raw = docker("exec", self.name, "cat", "/proc/1/environ", text=False).stdout
        env = dict(item.split("=", 1) for item in raw.decode().split("\0") if "=" in item)
        return {"user": env.get("MINIO_ROOT_USER", ""),
                "password": env.get("MINIO_ROOT_PASSWORD", "")}

    def spooled(self) -> list[str]:
        """The audit events MinIO has raised and queued, one file each."""
        done = docker("exec", self.name, "sh", "-c",
                      f"for f in {self.SPOOL}/*/*; do [ -f \"$f\" ] && echo \"$f\"; done")
        return [line for line in done.stdout.splitlines() if line.strip()]

    def mc(self, access_key: str, secret: str, command: str) -> subprocess.CompletedProcess:
        """`mc <command>` inside MinIO's container against 127.0.0.1:9000, as
        PEERS_2026-10-13.md ran it (the box carries no `mc`)."""
        script = ('export MC_CONFIG_DIR=$(mktemp -d); '
                  'mc alias set t http://127.0.0.1:9000 "$1" "$2" >/dev/null || exit 3; '
                  f'mc {command}; rc=$?; rm -rf "$MC_CONFIG_DIR"; exit $rc')
        return docker("exec", self.name, "sh", "-c", script, "mc", access_key, secret)

    def s3(self, key: str = BOX_KEY, secret: str = BOX_SECRET, token: str | None = None):
        return boto3.client("s3", endpoint_url=self.endpoint, aws_access_key_id=key,
                            aws_secret_access_key=secret, aws_session_token=token,
                            region_name=REGION,
                            config=Config(s3={"addressing_style": "path"},
                                          retries={"max_attempts": 1}))

    def admin(self, method: str, path: str, body: bytes = b"",
              credentials: Credentials | None = None) -> tuple[int | None, str]:
        """One admin-API request, SigV4-signed with botocore as the PEERS note
        signed it from the episode's own interpreter."""
        credentials = credentials or Credentials(BOX_KEY, BOX_SECRET)
        url = self.endpoint + path
        request = AWSRequest(method=method, url=url, data=body)
        S3SigV4Auth(credentials, "s3", REGION).add_auth(request)
        sent = urllib.request.Request(url, data=body or None, method=method,
                                      headers=dict(request.headers))
        try:
            with urllib.request.urlopen(sent, timeout=30) as reply:
                return reply.status, reply.read(400).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(400).decode("utf-8", "replace")


@pytest.fixture(scope="module")
def box_minio(minio_service):
    server = Minio(minio_service).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fresh_minio(minio_service):
    server = Minio(minio_service).start()
    try:
        yield server
    finally:
        server.stop()


def batch_job(source: str) -> bytes:
    """The PEERS note's replicate job: MinIO dials `source` to read from it."""
    return ("replicate:\n  apiVersion: v1\n  source:\n    type: minio\n"
            f"    bucket: {BUCKET}\n    prefix: \"\"\n    endpoint: \"{source}\"\n"
            "    credentials:\n      accessKey: t8bpeer\n      secretKey: t8bpeer-secret\n"
            f"  target:\n    type: minio\n    bucket: {BUCKET}\n    prefix: \"t8b-batch/\"\n"
            ).encode()


def test_box_credential_is_refused_by_the_admin_api(box_minio):
    """Every admin call the PEERS note made, and the ones that would let the box
    re-key itself, are AccessDenied for the box's credential."""
    attempts = {
        "info": ("GET", "/minio/admin/v3/info", b""),
        "start_job": ("POST", "/minio/admin/v3/start-job", batch_job(JIRA)),
        "set_config_kv": ("PUT", "/minio/admin/v3/set-config-kv",
                          f"notify_webhook:t8b endpoint={JIRA}/ queue_limit=0".encode()),
        "get_config_kv": ("GET", "/minio/admin/v3/get-config-kv?key=notify_webhook", b""),
        "service_restart": ("POST", "/minio/admin/v3/service?action=restart", b""),
        "add_user_self": ("PUT", f"/minio/admin/v3/add-user?accessKey={BOX_KEY}", b"x" * 64),
        "add_user": ("PUT", "/minio/admin/v3/add-user?accessKey=t8bnew", b"x" * 64),
        "add_canned_policy": ("PUT", "/minio/admin/v3/add-canned-policy?name=t8b",
                              json.dumps(policy()).encode()),
        "list_users": ("GET", "/minio/admin/v3/list-users", b""),
        "list_jobs": ("GET", "/minio/admin/v3/list-jobs", b""),
        "set_remote_target": ("PUT", f"/minio/admin/v3/set-remote-target?bucket={BUCKET}",
                              b"x" * 64),
    }
    got = {name: box_minio.admin(*attempt) for name, attempt in attempts.items()}
    refused = {name: status == 403 and "AccessDenied" in body
               for name, (status, body) in got.items()}
    assert all(refused.values()), got


def test_box_cannot_set_a_notify_target_or_re_key_itself(box_minio):
    """What needs an encrypted admin body or a bucket subscription, driven with
    `mc` and boto3: a notify_webhook target, a bucket event, a restart, a
    service account for itself, a new secret for itself. All refused."""
    for command in (f"admin config set t notify_webhook:t8b endpoint={JIRA}/ queue_limit=0",
                    f"event add t/{BUCKET} arn:minio:sqs::t8b:webhook --event put",
                    "admin service restart t --json",
                    f"admin user svcacct add t {BOX_KEY}",
                    f"admin user add t {BOX_KEY} t8b-other-secret",
                    "admin info t"):
        done = box_minio.mc(BOX_KEY, BOX_SECRET, command)
        assert done.returncode not in (0, 3), (command, done.stdout, done.stderr)
        assert "Access Denied" in done.stdout + done.stderr, (command, done.stdout,
                                                             done.stderr)
    with pytest.raises(Exception, match="AccessDenied"):
        box_minio.s3().put_bucket_notification_configuration(
            Bucket=BUCKET, NotificationConfiguration={"QueueConfigurations": [
                {"QueueArn": "arn:minio:sqs::t8b:webhook",
                 "Events": ["s3:ObjectCreated:*"]}]})
    # The box's own secret still works: the self re-key above changed nothing.
    assert box_minio.s3().head_bucket(Bucket=BUCKET)


def test_temporary_credentials_inherit_the_refusal(box_minio):
    """STS AssumeRole with the box's credential answers, and what it hands out
    carries the box's policy: the admin API is still AccessDenied."""
    sts = boto3.client("sts", endpoint_url=box_minio.endpoint, region_name=REGION,
                       aws_access_key_id=BOX_KEY, aws_secret_access_key=BOX_SECRET)
    temporary = sts.assume_role(RoleArn="arn:minio:iam:::role/t8b-test-role",
                                RoleSessionName="t8b-session")["Credentials"]
    credentials = Credentials(temporary["AccessKeyId"], temporary["SecretAccessKey"],
                              temporary["SessionToken"])
    status, body = box_minio.admin("GET", "/minio/admin/v3/info", credentials=credentials)
    assert status == 403 and "AccessDenied" in body, (status, body)
    status, body = box_minio.admin("POST", "/minio/admin/v3/start-job", batch_job(JIRA),
                                   credentials=credentials)
    assert status == 403 and "AccessDenied" in body, (status, body)


def test_box_credential_still_does_the_worlds_s3_work(box_minio):
    """The box's normal S3 work, through boto3: listing, object CRUD, a
    server-side copy, a batch delete and a multipart upload, completed and
    aborted."""
    s3 = box_minio.s3()
    assert [b["Name"] for b in s3.list_buckets()["Buckets"]] == [BUCKET]
    s3.head_bucket(Bucket=BUCKET)
    s3.get_bucket_location(Bucket=BUCKET)
    s3.put_object(Bucket=BUCKET, Key="t8b/a.txt", Body=b"hello")
    assert s3.head_object(Bucket=BUCKET, Key="t8b/a.txt")["ContentLength"] == 5
    assert s3.get_object(Bucket=BUCKET, Key="t8b/a.txt")["Body"].read() == b"hello"
    s3.copy_object(Bucket=BUCKET, Key="t8b/b.txt",
                   CopySource={"Bucket": BUCKET, "Key": "t8b/a.txt"})
    listed = s3.list_objects_v2(Bucket=BUCKET, Prefix="t8b/")
    assert [o["Key"] for o in listed["Contents"]] == ["t8b/a.txt", "t8b/b.txt"]
    upload = s3.create_multipart_upload(Bucket=BUCKET, Key="t8b/big.bin")["UploadId"]
    part = s3.upload_part(Bucket=BUCKET, Key="t8b/big.bin", UploadId=upload,
                          PartNumber=1, Body=b"a" * (5 * 1024 * 1024))["ETag"]
    assert len(s3.list_parts(Bucket=BUCKET, Key="t8b/big.bin",
                             UploadId=upload)["Parts"]) == 1
    assert s3.list_multipart_uploads(Bucket=BUCKET)["Uploads"]
    s3.complete_multipart_upload(Bucket=BUCKET, Key="t8b/big.bin", UploadId=upload,
                                 MultipartUpload={"Parts": [{"ETag": part,
                                                             "PartNumber": 1}]})
    aborted = s3.create_multipart_upload(Bucket=BUCKET, Key="t8b/gone.bin")["UploadId"]
    s3.abort_multipart_upload(Bucket=BUCKET, Key="t8b/gone.bin", UploadId=aborted)
    s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": "t8b/b.txt"},
                                                         {"Key": "t8b/big.bin"}]})
    s3.delete_object(Bucket=BUCKET, Key="t8b/a.txt")
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET, Prefix="t8b/")


def test_bucket_lifecycle_and_configuration_are_not_the_boxs(box_minio):
    """The boundary of the grant: no new bucket, no deleting the world's, no
    bucket policy (anonymous access) and no versioning switch."""
    s3 = box_minio.s3()
    for call in (lambda: s3.create_bucket(Bucket="t8b-new"),
                 lambda: s3.delete_bucket(Bucket=BUCKET),
                 lambda: s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps(policy())),
                 lambda: s3.put_bucket_versioning(
                     Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})):
        with pytest.raises(Exception, match="AccessDenied"):
            call()


def test_provisioning_leaves_no_audit_event(fresh_minio):
    """The box's user and policy are made before the audited server exists:
    once MinIO is ready its queue holds nothing - no AddUser, no
    AddCannedPolicy, nothing signed by the root - and a single box PutObject
    then queues an event (the queue is live, so the zero is a measurement)."""
    assert fresh_minio.spooled() == []
    fresh_minio.s3().put_object(Bucket=BUCKET, Key="t8b/spool.txt", Body=b"x")
    deadline = time.monotonic() + 30
    while not fresh_minio.spooled() and time.monotonic() < deadline:
        time.sleep(0.5)
    assert len(fresh_minio.spooled()) == 1


def test_root_is_minted_per_start_and_holds_the_admin_api(fresh_minio):
    """The root is the server's own: 64 hex characters from /dev/urandom, not the
    box's credential, new on every start, and in no container config. It does
    hold the admin API (so the box's 403s are the identity's, not a dead
    endpoint), and after a restart the box's user is still there and still
    refused."""
    first = fresh_minio.root()
    assert first["user"] and first["user"] != BOX_KEY
    password = first["password"]
    assert len(password) == 64 and set(password) <= set("0123456789abcdef")
    inspected = json.loads(docker("inspect", fresh_minio.name).stdout)[0]
    assert password not in json.dumps(inspected)
    status, body = fresh_minio.admin("GET", "/minio/admin/v3/info",
                                     credentials=Credentials(first["user"], password))
    assert status == 200 and "online" in body, (status, body)

    fresh_minio.restart()
    second = fresh_minio.root()
    assert second["user"] == first["user"] and second["password"] != password
    assert fresh_minio.s3().head_bucket(Bucket=BUCKET)
    status, body = fresh_minio.admin("GET", "/minio/admin/v3/info")
    assert status == 403 and "AccessDenied" in body
