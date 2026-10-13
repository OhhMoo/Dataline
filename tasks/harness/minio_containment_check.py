#!/usr/bin/env python3
"""MinIO containment, measured from inside the box.

The regression check for gateway/docker/evidence/CONTAINMENT_2026-10-07.md:

  finding 1  on a box whose profile plugs no `object_store` (every da/* box),
             MinIO still ran on episode_net under its compose name, unaudited;
  the 8110 row  native-services' MinIO audit receiver took a POST from anyone
             on episode_net, so the episode could forge `minio.*` rows.

    sg docker -c "gateway/.venv/bin/python tasks/harness/minio_containment_check.py \\
        --image da/laptop=alkera-episode:head-da \\
        --image da/snowflake=alkera-episode:head-da-snowflake \\
        --image unprofiled=alkera-episode:head --image de/laptop=alkera-episode:head"

One stack per box, one at a time, each torn down before the next (the stack is
`tool_tour.TourTopology`: `NativeContainerEpisode.up()` + `prepare()` on a fresh
`commerce_analytics` seed-11 world built for that profile, as the tool tour and
P7 build it). Everything asked from inside a container runs as `python -` on the
image's absolute interpreter with the program on stdin, so it writes no shim row
and no file in the box.

  STORE-LESS boxes (da/laptop with BI001's three vendors, da/snowflake):
    no_minio_container     the project runs no `minio` service (docker, host side)
    nothing_resolves       from the episode AND jupyter: `minio`, `minio.internal`
                           and MinIO's container names do not resolve
    no_storage_env         no AWS_* / MINIO* variable, no value naming minio
    no_port_9000           a sweep of every address on the episode's on-link
                           network finds nothing answering :9000
    no_receiver            a POST to native-services.internal:8110 is refused
                           at connect (no receiver is served on such a box)

  STORE boxes (unprofiled, de/laptop), where MinIO is an audited door:
    sweep_control          the same sweep DOES find MinIO's address on :9000, so
                           "nothing answers" above is a measurement
    only_9000_open         every TCP port of MinIO's address (1-65535), dialed
                           from the episode: only :9000, the audited S3 API,
                           answers - no web console (it listened on a random port
                           of every address until compose.native.yml set
                           MINIO_BROWSER=off; CONTAINMENT_2026-10-07.md)
    genuine_rows           `aws s3api` put/get/delete from the episode lands
                           minio.PutObject/GetObject/DeleteObject rows: MinIO's
                           own webhook, with its token, still gets through
    forged_refused         forged POSTs to :8110 (no Authorization, a wrong token,
                           `Bearer <wrong>`, the stack's shared credential) are each
                           a 401 and append no row (actions.jsonl unchanged, no row
                           naming the forged key); native-services logs each refusal
    token_unseen           the token is not a variable of the episode, and its
                           first 24 characters are in no file under /audit,
                           /workspace, /root, /etc, /tmp, /opt/episode and in no
                           /proc/*/environ or cmdline of the box

  STORE boxes, the box's S3 identity (gateway/docker/evidence/PEERS_2026-10-13.md:
  the box's credential was MinIO's root, and MinIO dialed jira.internal:8080 for
  the agent, 0 to 4 in destinations.jsonl):
    box_credential         the episode's AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
                           are still `alkera-local-dev`, byte for byte
    box_s3_workload        the box's normal S3 work from the episode still succeeds:
                           `aws s3 ls`, cp up and down (a 9 MB multipart one
                           too), an S3-to-S3 cp, mv, sync, a recursive ls and rm
    box_admin_refused      from the episode, SigV4-signed with botocore and the
                           box's own variables, as the PEERS note did: admin
                           info, a batch replicate job and a notify_webhook
                           config set (both aimed at jira.internal:8080), a
                           restart, a new secret for itself, a user listing and
                           a bucket notification subscription are each MinIO's
                           403 AccessDenied, and destinations.jsonl does not move
    box_mc_refused         the PEERS note's `mc` row, run as it was run (in
                           MinIO's own container against 127.0.0.1:9000, with the
                           box's credential; the box has no `mc`): admin info, a
                           notify_webhook target at jira.internal:8080, a restart,
                           a bucket event, a service account and a new secret are
                           each Access Denied; then a PutObject from the episode,
                           and destinations.jsonl still does not move
    root_admin_control     MinIO's root (read from its server process, inside its
                           container) does answer `mc admin info`: the refusals
                           above are the identity's, not a dead admin API
    root_secret_unseen     that root secret is no variable of the episode, its
                           first 24 characters are in no file or /proc entry of
                           the box (the token_unseen scan), and it is in no
                           project container's `docker inspect` and not in the
                           per-rollout overlay: minio_start.sh minted it at start

Evidence: tasks/harness/evidence/minio_containment.json (`--out`). Exit 1 on any
failed check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import bi_e2e_check as bi  # noqa: E402 - after the path insert
import tool_tour  # noqa: E402

EVIDENCE = HERE / "evidence" / "minio_containment.json"
WORK_ROOT = Path.home() / "alkera-minio-check"
CONTAINER_PYTHON = "/usr/local/bin/python"
BUCKET = "alkera"
RECEIVER = "http://native-services.internal:8110/minio/audit"
SHARED_CREDENTIAL = "alkera-local-dev"
TOKEN_PREFIX = 24
#: The webhook is asynchronous (MinIO queues, then posts).
ROWS_TIMEOUT_S = 60
SWEEP_PORT = 9000
SWEEP_TIMEOUT_S = 0.5
#: At most this many addresses per on-link network (a /20 or wider is cut, and
#: the record says so).
SWEEP_CAP = 4096
#: Every TCP port, dialed on a store box's MinIO address: the API on :9000 is the
#: one door, so anything else that answers - a console on a random port - is not.
FULL_SWEEP = (1, 65535)
#: The agent listener the PEERS note saw MinIO dial for the agent (count 0 to 4),
#: and so the source of the batch job and the endpoint of the notify target here.
JIRA = "http://jira.internal:8080"
#: A notify target ARN, as `mc event add` names one.
NOTIFY_ARN = "arn:minio:sqs::mfix:webhook"
#: How long a dial MinIO had been talked into takes to land (PEERS waited 4 s).
DIAL_GRACE_S = 4
#: MinIO's own word for a refused request; a bare 403 from anything else is not it.
ACCESS_DENIED = "AccessDenied"


@dataclass(frozen=True)
class Box:
    name: str
    profile: str            # "" is the unprofiled box
    plugs: dict = field(default_factory=dict)
    store: bool = False     # does the profile plug the object store?
    notebook: bool = False  # is jupyter running beside the episode?


BOXES = (
    Box("da-laptop", "da/laptop", {"bi": ["powerbi", "tableau", "sigma"]},
        store=False, notebook=True),
    Box("da-snowflake", "da/snowflake", {}, store=False, notebook=True),
    Box("unprofiled", "", {}, store=True),
    Box("de-laptop", "de/laptop", {}, store=True),
)


# --------------------------------------------------------------------------
# what runs inside a container (stdlib only; SPEC is prepended as JSON)
# --------------------------------------------------------------------------
PROBE = r"""
import base64, concurrent.futures, hashlib, ipaddress, json, os, socket, struct
import urllib.error, urllib.request

out = {}


def resolve(name):
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(name, None)})
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


out["resolve"] = {n: resolve(n) for n in SPEC["names"]}
out["storage_env"] = sorted(k for k, v in os.environ.items()
                            if k.startswith(("AWS_", "MINIO")) or "minio" in v.lower())
if SPEC.get("token_sha256"):
    out["token_in_env"] = sorted(
        k for k, v in os.environ.items()
        if k == SPEC["token_env"] or hashlib.sha256(v.encode()).hexdigest() == SPEC["token_sha256"])

if SPEC.get("sweep"):
    nets = []
    for line in open("/proc/net/route").read().splitlines()[1:]:
        f = line.split()
        dest, mask = int(f[1], 16), int(f[7], 16)
        if dest and mask:
            nets.append(ipaddress.ip_network(
                (socket.inet_ntoa(struct.pack("<I", dest)) + "/"
                 + socket.inet_ntoa(struct.pack("<I", mask))), strict=False))
    hosts, cut = [], []
    for net in nets:
        h = [str(a) for a in net.hosts()]
        if len(h) > SPEC["cap"]:
            cut.append(str(net))
            h = h[:SPEC["cap"]]
        hosts += h

    def tcp(host):
        try:
            socket.create_connection((host, SPEC["port"]), timeout=SPEC["timeout"]).close()
            return True
        except OSError:
            return False

    with concurrent.futures.ThreadPoolExecutor(128) as pool:
        answered = [h for h, ok in zip(hosts, pool.map(tcp, hosts)) if ok]
    out["sweep"] = {"networks": [str(n) for n in nets], "cut": cut, "addresses": len(hosts),
                    "port": SPEC["port"], "answered": answered}

if SPEC.get("port_sweep"):
    ps = SPEC["port_sweep"]
    targets = [(h, p) for h in ps["hosts"] for p in range(ps["first"], ps["last"] + 1)]

    def dial(target):
        try:
            socket.create_connection(target, timeout=ps["timeout"]).close()
            return True
        except OSError:
            return False

    with concurrent.futures.ThreadPoolExecutor(256) as pool:
        hits = [t for t, ok in zip(targets, pool.map(dial, targets)) if ok]
    out["port_sweep"] = {"hosts": ps["hosts"], "first": ps["first"], "last": ps["last"],
                         "dialed": len(targets),
                         "open": {h: sorted(p for hh, p in hits if hh == h)
                                  for h in ps["hosts"]}}

if SPEC.get("forge"):
    attempts = []
    for headers in SPEC["forge"]:
        request = urllib.request.Request(SPEC["receiver"], data=SPEC["body"].encode(),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                attempts.append({"headers": sorted(headers), "status": reply.status,
                                 "body": reply.read(300).decode("utf-8", "replace")})
        except urllib.error.HTTPError as exc:
            attempts.append({"headers": sorted(headers), "status": exc.code,
                             "body": exc.read(300).decode("utf-8", "replace")})
        except OSError as exc:
            attempts.append({"headers": sorted(headers), "status": None,
                             "error": f"{type(exc).__name__}: {exc}"})
    out["forge"] = attempts

if SPEC.get("admin"):
    # MinIO's admin API, SigV4-signed with botocore and the box's own AWS_*
    # variables: what PEERS_2026-10-13.md did from the episode's interpreter.
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    key = os.environ["AWS_ACCESS_KEY_ID"]
    creds = Credentials(key, os.environ["AWS_SECRET_ACCESS_KEY"])
    endpoint = (SPEC["admin"].get("endpoint") or os.environ["AWS_ENDPOINT_URL"]).rstrip("/")
    region = os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    answers = {}
    for attempt in SPEC["admin"]["attempts"]:
        body = attempt.get("body", "").encode()
        url = endpoint + attempt["path"].replace("{access_key}", key)
        headers = ({"Content-MD5": base64.b64encode(hashlib.md5(body).digest()).decode()}
                   if body else {})
        signed = AWSRequest(method=attempt["method"], url=url, data=body, headers=headers)
        S3SigV4Auth(creds, "s3", region).add_auth(signed)
        request = urllib.request.Request(url, data=body or None, method=attempt["method"],
                                         headers=dict(signed.headers))
        try:
            with urllib.request.urlopen(request, timeout=30) as reply:
                status, text = reply.status, reply.read(400)
        except urllib.error.HTTPError as exc:
            status, text = exc.code, exc.read(400)
        except OSError as exc:
            status, text = None, f"{type(exc).__name__}: {exc}".encode()
        answers[attempt["name"]] = {"status": status,
                                    "body": text.decode("utf-8", "replace")}
    out["admin"] = {"access_key": key, "endpoint": endpoint, "attempts": answers}

if SPEC.get("token_prefix"):
    needle = SPEC["token_prefix"].encode()
    hits, scanned = [], 0
    for root in SPEC["scan"]:
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
            for name in files:
                path = os.path.join(base, name)
                try:
                    if os.path.islink(path) or os.path.getsize(path) > 8_000_000:
                        continue
                    with open(path, "rb") as stream:
                        data = stream.read()
                except OSError:
                    continue
                scanned += 1
                if needle in data:
                    hits.append(path)
    for pid in os.listdir("/proc"):
        if pid.isdigit() and int(pid) != os.getpid():
            for part in ("environ", "cmdline"):
                try:
                    with open(f"/proc/{pid}/{part}", "rb") as stream:
                        data = stream.read()
                except OSError:
                    continue
                scanned += 1
                if needle in data:
                    hits.append(f"/proc/{pid}/{part}")
    out["token_scan"] = {"files_read": scanned, "hits": hits}

print(json.dumps(out))
"""


def in_container(container: str, spec: dict, timeout: int = 600) -> dict:
    """PROBE inside `container`, on stdin, as the image's absolute interpreter."""
    program = f"import json\nSPEC = json.loads({json.dumps(json.dumps(spec))})\n{PROBE}"
    done = subprocess.run(["docker", "exec", "-i", container, CONTAINER_PYTHON, "-"],
                          input=program, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
    if done.returncode != 0 or not lines:
        raise RuntimeError(f"probe in {container[:12]} failed ({done.returncode}): "
                           f"{done.stderr[-2000:]}")
    return json.loads(lines[-1])


# --------------------------------------------------------------------------
# host side
# --------------------------------------------------------------------------
def project_containers(project: str) -> dict[str, str]:
    """{compose service: container id} for every container of the project."""
    done = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
         "--format", '{{.ID}} {{.Label "com.docker.compose.service"}}'],
        capture_output=True, text=True, check=True)
    out = {}
    for line in done.stdout.splitlines():
        cid, _, service = line.partition(" ")
        out[service] = cid
    return out


def logs(container: str) -> str:
    done = subprocess.run(["docker", "logs", container], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return done.stdout + done.stderr


def rows(audit_dir: Path) -> list[dict]:
    return bi.read_rows(Path(audit_dir) / "actions.jsonl")


def minio_names(project: str) -> list[str]:
    return ["minio", "minio.internal", f"{project}-minio-1", f"{project}_minio_1"]


def forged_event(key: str) -> str:
    """A webhook body as MinIO posts it, naming a key no real call uses."""
    return json.dumps({"version": "1", "deploymentid": "forged",
                       "api": {"name": "PutObject", "bucket": BUCKET, "object": key,
                               "status": "OK", "statusCode": 200},
                       "requestPath": f"/{BUCKET}/{key}", "userAgent": "forged"}) + "\n"


def forge_headers(wrong: str) -> list[dict]:
    return [{}, {"Authorization": wrong}, {"Authorization": f"Bearer {wrong}"},
            {"Authorization": SHARED_CREDENTIAL},
            {"Authorization": f"Bearer {SHARED_CREDENTIAL}"}]


S3_COMMAND = """set -euo pipefail
cd /workspace
key="$1"
printf 'minio containment check\\n' > /tmp/mfix-body.txt
aws s3api put-object --bucket {bucket} --key "$key" --body /tmp/mfix-body.txt >/dev/null
aws s3api get-object --bucket {bucket} --key "$key" /tmp/mfix-read.txt >/dev/null
aws s3api delete-object --bucket {bucket} --key "$key" >/dev/null
rm -f /tmp/mfix-body.txt /tmp/mfix-read.txt
echo ok
"""


def wait_rows(audit_dir: Path, start: int, predicate, timeout: float = ROWS_TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while True:
        new = rows(audit_dir)[start:]
        if predicate(new) or time.monotonic() > deadline:
            return new
        time.sleep(1)


def settle(audit_dir: Path, quiet_s: float = 3.0, timeout: float = 30.0) -> int:
    """The row count once nothing has arrived for `quiet_s` (the webhook is
    asynchronous), so a later delta is not a late genuine row."""
    deadline = time.monotonic() + timeout
    count, since = len(rows(audit_dir)), time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.5)
        now = len(rows(audit_dir))
        if now != count:
            count, since = now, time.monotonic()
        elif time.monotonic() - since >= quiet_s:
            break
    return count


def store_sweep_spec(addresses: list[str]) -> dict:
    """The in-container spec that dials every port of `addresses`."""
    return {"names": [], "port_sweep": {"hosts": list(addresses), "first": FULL_SWEEP[0],
                                        "last": FULL_SWEEP[1], "timeout": SWEEP_TIMEOUT_S}}


def other_open_ports(sweep: dict, allowed: int = SWEEP_PORT) -> dict[str, list[int]]:
    """{address: its open ports other than `allowed`}, for the addresses that have any."""
    return {host: others for host, ports in sweep["open"].items()
            if (others := [p for p in ports if p != allowed])}


def check(name: str, ok: bool, detail) -> dict:
    return {"check": name, "ok": bool(ok), "detail": detail}


#: Where token_unseen and root_secret_unseen look for a secret inside the box.
SCAN_ROOTS = ("/audit", "/workspace", "/root", "/etc", "/tmp", "/opt/episode")


def secret_scan_spec(env_name: str, secret: str, scan=SCAN_ROOTS) -> dict:
    """The in-box spec that looks for `secret`: a variable named `env_name` or
    holding it (by hash), and its first TOKEN_PREFIX characters in any file under
    `scan` or any /proc/*/environ or cmdline. The secret itself is not in it."""
    return {"names": [], "token_env": env_name,
            "token_sha256": hashlib.sha256(secret.encode()).hexdigest(),
            "token_prefix": secret[:TOKEN_PREFIX], "scan": list(scan)}


# --------------------------------------------------------------------------
# the box's S3 identity (gateway/docker/evidence/PEERS_2026-10-13.md)
# --------------------------------------------------------------------------
def batch_job(source: str) -> str:
    """The PEERS note's replicate job: MinIO dials `source` to read from it."""
    return ("replicate:\n  apiVersion: v1\n  source:\n    type: minio\n"
            f"    bucket: {BUCKET}\n    prefix: \"\"\n    endpoint: \"{source}\"\n"
            "    credentials:\n      accessKey: mfixpeer\n      secretKey: mfixpeer-secret\n"
            f"  target:\n    type: minio\n    bucket: {BUCKET}\n    prefix: \"mfix-batch/\"\n")


NOTIFICATION = ("<NotificationConfiguration><QueueConfiguration><Queue>{arn}</Queue>"
                "<Event>s3:ObjectCreated:*</Event></QueueConfiguration>"
                "</NotificationConfiguration>")


def admin_attempts(witness: str) -> list[dict]:
    """The admin-API calls PEERS_2026-10-13.md made with the box's credential,
    aimed at `witness`, plus the ones that would let the box re-key itself or
    subscribe the bucket to a notify target. `{access_key}` is filled in inside
    the box, with its own AWS_ACCESS_KEY_ID."""
    return [
        {"name": "info", "method": "GET", "path": "/minio/admin/v3/info"},
        {"name": "start_job", "method": "POST", "path": "/minio/admin/v3/start-job",
         "body": batch_job(witness)},
        {"name": "set_config_kv", "method": "PUT", "path": "/minio/admin/v3/set-config-kv",
         "body": f"notify_webhook:mfix endpoint={witness}/ queue_limit=0"},
        {"name": "get_config_kv", "method": "GET",
         "path": "/minio/admin/v3/get-config-kv?key=notify_webhook"},
        {"name": "service_restart", "method": "POST",
         "path": "/minio/admin/v3/service?action=restart"},
        {"name": "add_user_self", "method": "PUT",
         "path": "/minio/admin/v3/add-user?accessKey={access_key}", "body": "x" * 64},
        {"name": "list_users", "method": "GET", "path": "/minio/admin/v3/list-users"},
        {"name": "put_bucket_notification", "method": "PUT",
         "path": f"/{BUCKET}?notification", "body": NOTIFICATION.format(arn=NOTIFY_ARN)},
    ]


#: PEERS_2026-10-13.md's `mc` row, with the box's credential (`{access_key}` is
#: its own). `--json` on the restart: without it mc wants a TTY before it asks.
MC_ATTEMPTS = (
    ("admin_info", "admin info box"),
    ("notify_webhook_set",
     f"admin config set box notify_webhook:mfix endpoint={JIRA}/ queue_limit=0"),
    ("service_restart", "admin service restart box --json"),
    ("event_add", f"event add box/{BUCKET} {NOTIFY_ARN} --event put"),
    ("svcacct_add", "admin user svcacct add box {access_key}"),
    ("user_add_self", "admin user add box {access_key} mfix-other-secret"),
)
#: In MinIO's container: alias the box's credential ($1 $2), run one `mc`
#: command (the rest of argv), drop the alias. Exit 3: the alias never worked.
MC_AS = ('export MC_CONFIG_DIR=$(mktemp -d); '
         'mc alias set box http://127.0.0.1:9000 "$1" "$2" >/dev/null 2>&1 '
         '|| { rm -rf "$MC_CONFIG_DIR"; exit 3; }; shift 2; '
         'mc "$@"; rc=$?; rm -rf "$MC_CONFIG_DIR"; exit $rc')
#: In MinIO's container: the root from its server process's own environment
#: (`docker exec` does not inherit it: minio_start.sh minted it at start), then
#: `mc admin info` with it. The secret does not leave the container.
ROOT_CONTROL = r"""tr '\0' '\n' < /proc/1/environ | {
  while IFS='=' read -r k v; do
    case $k in MINIO_ROOT_USER) u=$v ;; MINIO_ROOT_PASSWORD) p=$v ;; esac
  done
  export MC_CONFIG_DIR=$(mktemp -d)
  mc alias set root http://127.0.0.1:9000 "$u" "$p" >/dev/null && mc admin info root --json
  rc=$?; rm -rf "$MC_CONFIG_DIR"; exit $rc
}"""
#: The box's normal S3 work, from the episode, with the `aws` CLI and its own
#: variables. 9 MB is above the CLI's 8 MB multipart threshold.
S3_WORKLOAD = """set -euo pipefail
p="mfix-work-$1"
w=$(mktemp -d)
head -c 9000000 /dev/urandom > "$w/big.bin"
printf 'small\\n' > "$w/small.txt"
mkdir -p "$w/dir" && printf 'one\\n' > "$w/dir/one.txt" && printf 'two\\n' > "$w/dir/two.txt"
aws s3 ls > "$w/buckets"
grep -q ' {bucket}$' "$w/buckets"
aws s3 cp --quiet "$w/small.txt" "s3://{bucket}/$p/small.txt"
aws s3 cp --quiet "$w/big.bin" "s3://{bucket}/$p/big.bin"
aws s3 cp --quiet "s3://{bucket}/$p/big.bin" "$w/big-back.bin"
cmp "$w/big.bin" "$w/big-back.bin"
aws s3 cp --quiet "s3://{bucket}/$p/small.txt" "s3://{bucket}/$p/copy.txt"
aws s3 mv --quiet "s3://{bucket}/$p/copy.txt" "s3://{bucket}/$p/moved.txt"
aws s3 sync --quiet "$w/dir" "s3://{bucket}/$p/dir/"
test "$(aws s3 ls "s3://{bucket}/$p/" --recursive | wc -l)" -eq 5
aws s3 rm --quiet --recursive "s3://{bucket}/$p/"
left=$(aws s3 ls "s3://{bucket}/$p/" --recursive || true)
test -z "$left"
rm -rf "$w"
echo ok
"""


def destination_counts(audit_dir) -> dict[str, int]:
    """destinations.jsonl as {"host:port": count}: native-services' own count of
    every connection its listeners accepted, the witness the PEERS note read."""
    out = {}
    path = Path(audit_dir) / "destinations.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[f"{row['host']}:{row['port']}"] = int(row.get("count") or 0)
    return out


def destination_changes(before: dict, after: dict) -> dict[str, list]:
    return {k: [before.get(k), after.get(k)] for k in sorted(set(before) | set(after))
            if before.get(k) != after.get(k)}


def refusal_verdict(attempts: dict, before: dict, after: dict) -> tuple[bool, dict]:
    """Every attempt MinIO's own refusal (403 AccessDenied), and no listener
    counted a connection meanwhile. Nothing attempted proves nothing."""
    not_refused = {name: a for name, a in attempts.items()
                   if a.get("status") != 403 or ACCESS_DENIED not in (a.get("body") or "")}
    moved = destination_changes(before, after)
    return (bool(attempts) and not not_refused and not moved,
            {"not_refused": not_refused, "destinations_moved": moved})


def mc_refused(result: dict) -> bool:
    """An `mc` command MinIO refused: it ran (the alias authenticated, rc != 3),
    failed, and said Access Denied - not a TTY error or anything else."""
    return (result.get("rc") not in (0, 3, None)
            and "Access Denied" in (result.get("output") or ""))


def minio_root(raw: bytes) -> dict[str, str]:
    """MINIO_ROOT_USER / MINIO_ROOT_PASSWORD from a NUL-separated environ."""
    env = dict(item.split("=", 1) for item in raw.decode("utf-8", "replace").split("\0")
               if "=" in item)
    return {"user": env.get("MINIO_ROOT_USER", ""),
            "password": env.get("MINIO_ROOT_PASSWORD", "")}


def container_env(container: str) -> dict[str, str]:
    spec = json.loads(subprocess.run(["docker", "inspect", container], capture_output=True,
                                     text=True, check=True).stdout)[0]
    return dict(item.split("=", 1) for item in spec["Config"]["Env"] or [] if "=" in item)


def measure_box_identity(topo, containers: dict[str, str], nonce: str) -> list[dict]:
    """The checks for PEERS_2026-10-13.md, on a store box (see the docstring)."""
    stack = topo.stack
    audit = Path(topo.audit_dir)
    env = container_env(stack.container_id)
    key, secret = env.get("AWS_ACCESS_KEY_ID", ""), env.get("AWS_SECRET_ACCESS_KEY", "")
    out = [check("box_credential", key == SHARED_CREDENTIAL and secret == SHARED_CREDENTIAL,
                 {"AWS_ACCESS_KEY_ID": key, "AWS_SECRET_ACCESS_KEY": secret,
                  "AWS_ENDPOINT_URL": env.get("AWS_ENDPOINT_URL")})]
    done = subprocess.run(["docker", "exec", stack.container_id, "bash", "-c",
                           S3_WORKLOAD.format(bucket=BUCKET), "s3", nonce],
                          capture_output=True, text=True, timeout=600)
    out.append(check("box_s3_workload",
                     done.returncode == 0 and done.stdout.strip().endswith("ok"),
                     {"returncode": done.returncode, "stdout": done.stdout[-300:],
                      "stderr": done.stderr[-600:]}))

    # From the episode, signed with its own variables (PEERS rows 54 and 56).
    before = destination_counts(audit)
    episode = in_container(stack.container_id, {
        "names": [], "admin": {"attempts": admin_attempts(JIRA)}})["admin"]
    time.sleep(DIAL_GRACE_S)
    after = destination_counts(audit)
    ok, detail = refusal_verdict(episode["attempts"], before, after)
    jira = JIRA.split("//", 1)[1]
    out.append(check("box_admin_refused", ok and episode["access_key"] == key,
                     {"access_key": episode["access_key"], "endpoint": episode["endpoint"],
                      "status": {n: a["status"] for n, a in episode["attempts"].items()},
                      "jira_count": [before.get(jira), after.get(jira)], **detail}))

    # The `mc` row (PEERS row 55), run where and as it was run.
    minio = containers["minio"]
    before = destination_counts(audit)
    results = {}
    for name, command in MC_ATTEMPTS:
        argv = shlex.split(command.format(access_key=key))
        ran = subprocess.run(["docker", "exec", minio, "sh", "-c", MC_AS, "mc-as-box",
                              key, secret, *argv], capture_output=True, text=True,
                             timeout=120)
        results[name] = {"rc": ran.returncode,
                         "output": (ran.stdout + ran.stderr).strip()[-300:]}
    # An object put: a bucket event that had been added would fire on it.
    event_put = (f'printf x | aws s3 cp --quiet - "s3://{BUCKET}/mfix-event-$1.txt" && '
                 f'aws s3 rm --quiet "s3://{BUCKET}/mfix-event-$1.txt"')
    put = subprocess.run(["docker", "exec", stack.container_id, "bash", "-c", event_put,
                          "s3", nonce], capture_output=True, text=True, timeout=120)
    time.sleep(DIAL_GRACE_S)
    after = destination_counts(audit)
    moved = destination_changes(before, after)
    out.append(check("box_mc_refused",
                     all(mc_refused(r) for r in results.values()) and put.returncode == 0
                     and not moved,
                     {"mc": results, "put_returncode": put.returncode,
                      "put_stderr": put.stderr[-300:], "jira_count": [before.get(jira),
                                                                      after.get(jira)],
                      "destinations_moved": moved}))

    # The root: it holds the admin API, and only MinIO's processes hold it.
    control = subprocess.run(["docker", "exec", minio, "sh", "-c", ROOT_CONTROL],
                             capture_output=True, text=True, timeout=120)
    out.append(check("root_admin_control",
                     control.returncode == 0 and '"status":"success"' in control.stdout,
                     {"returncode": control.returncode, "stdout": control.stdout[:200],
                      "stderr": control.stderr[-300:]}))
    root = minio_root(subprocess.run(["docker", "exec", minio, "cat", "/proc/1/environ"],
                                     capture_output=True, timeout=60).stdout)
    password = root["password"]
    scanned = (in_container(stack.container_id,
                            secret_scan_spec("MINIO_ROOT_PASSWORD", password))
               if password else {"token_in_env": [], "token_scan": {"hits": []}})
    in_inspect = sorted(
        service for service, cid in containers.items()
        if password and password in subprocess.run(["docker", "inspect", cid],
                                                   capture_output=True, text=True).stdout)
    overlay = Path(stack.overlay)
    in_overlay = bool(password) and overlay.is_file() and password in overlay.read_text(
        encoding="utf-8")
    out.append(check("root_secret_unseen",
                     len(password) >= 32 and root["user"] not in ("", key)
                     and not scanned["token_in_env"] and not scanned["token_scan"]["hits"]
                     and not in_inspect and not in_overlay,
                     {"root_user": root["user"], "secret_length": len(password),
                      "secret_sha256_12": hashlib.sha256(password.encode()).hexdigest()[:12],
                      "token_in_env": scanned["token_in_env"], **scanned["token_scan"],
                      "in_docker_inspect": in_inspect, "in_overlay": in_overlay}))
    return out


def measure_storeless(box: Box, topo) -> list[dict]:
    stack = topo.stack
    containers = project_containers(topo.project)
    out = [check("no_minio_container", "minio" not in containers,
                 {"services": sorted(containers)})]
    names = minio_names(topo.project)
    episode = in_container(stack.container_id, {
        "names": names, "sweep": True, "port": SWEEP_PORT, "timeout": SWEEP_TIMEOUT_S,
        "cap": SWEEP_CAP, "forge": [{}], "receiver": RECEIVER,
        "body": forged_event("mfix-forged-storeless.txt")})
    seen = {"episode": episode}
    if box.notebook:
        if "jupyter" not in containers:
            out.append(check("jupyter_present", False, {"services": sorted(containers)}))
        else:
            seen["jupyter"] = in_container(containers["jupyter"], {"names": names})
    unresolved = {where: {n: r for n, r in got["resolve"].items() if isinstance(r, list)}
                  for where, got in seen.items()}
    out.append(check("nothing_resolves", not any(unresolved.values()),
                     {where: got["resolve"] for where, got in seen.items()}))
    out.append(check("no_storage_env", not any(got["storage_env"] for got in seen.values()),
                     {where: got["storage_env"] for where, got in seen.items()}))
    sweep = episode["sweep"]
    out.append(check("no_port_9000", sweep["addresses"] > 0 and not sweep["answered"],
                     sweep))
    attempt = episode["forge"][0]
    out.append(check("no_receiver", attempt["status"] is None, attempt))
    return out


def measure_store(box: Box, topo, nonce: str) -> list[dict]:
    stack = topo.stack
    audit = Path(topo.audit_dir)
    containers = project_containers(topo.project)
    token = stack.minio_audit_token
    out = [check("minio_container", "minio" in containers, {"services": sorted(containers)})]
    # The sweep, as the store-less boxes get it: here MinIO must answer.
    episode = in_container(stack.container_id, {
        "names": ["minio.internal"], "sweep": True, "port": SWEEP_PORT,
        "timeout": SWEEP_TIMEOUT_S, "cap": SWEEP_CAP})
    minio_addresses = episode["resolve"]["minio.internal"]
    sweep = episode["sweep"]
    out.append(check("sweep_control", isinstance(minio_addresses, list)
                     and set(minio_addresses) & set(sweep["answered"]),
                     {"minio.internal": minio_addresses, **sweep}))
    # Every port of MinIO's address: the S3 API is the only thing that answers.
    addresses = minio_addresses if isinstance(minio_addresses, list) else []
    ports = (in_container(stack.container_id, store_sweep_spec(addresses))["port_sweep"]
             if addresses else {"dialed": 0, "open": {}})
    span = FULL_SWEEP[1] - FULL_SWEEP[0] + 1
    out.append(check("only_9000_open",
                     addresses and ports["dialed"] == span * len(addresses)
                     and all(SWEEP_PORT in ports["open"][a] for a in addresses)
                     and not other_open_ports(ports),
                     {"minio.internal": minio_addresses, **ports,
                      "other_open_ports": other_open_ports(ports)}))
    # MinIO's own webhook, with its token: real client calls land rows.
    key = f"mfix-genuine-{nonce}.txt"
    start = len(rows(audit))
    done = subprocess.run(["docker", "exec", stack.container_id, "bash", "-c",
                           S3_COMMAND.format(bucket=BUCKET), "s3", key],
                          capture_output=True, text=True, timeout=300)
    wanted = {"minio.PutObject", "minio.GetObject", "minio.DeleteObject"}

    def landed(new):
        return wanted <= {r.get("tool") for r in new
                          if any(key in t for t in r.get("targets") or [])}

    new = wait_rows(audit, start, landed)
    genuine = sorted({r.get("tool") for r in new
                      if any(key in t for t in r.get("targets") or [])})
    out.append(check("genuine_rows", done.returncode == 0 and landed(new),
                     {"returncode": done.returncode, "stderr": done.stderr[-600:],
                      "minio_tools_for_key": genuine,
                      "tools": sorted({r.get("tool") for r in new})}))
    # Forged POSTs from the episode.
    forged_key = f"mfix-forged-{nonce}.txt"
    headers = forge_headers(f"forged-{nonce}")
    settle(audit)
    before = rows(audit)
    services_log_before = logs(stack.services_container).count(
        "minio audit receiver: refused a POST")
    episode = in_container(stack.container_id, {
        "names": [], "forge": headers, "receiver": RECEIVER,
        "body": forged_event(forged_key)})
    settle(audit)
    after = rows(audit)
    named = [r for r in after if forged_key in json.dumps(r)]
    refusals = logs(stack.services_container).count(
        "minio audit receiver: refused a POST") - services_log_before
    attempts = episode["forge"]
    out.append(check("forged_refused",
                     all(a["status"] == 401 for a in attempts) and len(after) == len(before)
                     and not named and refusals == len(attempts),
                     {"attempts": attempts, "rows_before": len(before),
                      "rows_after": len(after), "rows_naming_the_forged_key": len(named),
                      "refusals_logged_by_native_services": refusals}))
    # The secret is nowhere the episode can read.
    episode = in_container(stack.container_id,
                           secret_scan_spec("ALKERA_MINIO_AUDIT_TOKEN", token))
    out.append(check("token_unseen",
                     not episode["token_in_env"] and not episode["token_scan"]["hits"],
                     {"token_in_env": episode["token_in_env"], **episode["token_scan"]}))
    # The box's S3 identity: a scoped user, and MinIO's root out of its reach.
    out += measure_box_identity(topo, containers, nonce)
    minio_log = logs(containers["minio"]) if "minio" in containers else ""
    out.append(check("webhook_delivered",
                     "unable to send log entry" not in minio_log,
                     {"minio_webhook_errors": minio_log.count("unable to send log entry")}))
    return out


def world_seed(box: Box) -> str:
    if box.profile:
        return tool_tour.world_seed(box.profile, box.plugs)
    return bi.SEED_EXAMPLE.read_text(encoding="utf-8").rstrip("\n") + "\nnative: true\n"


def run_box(box: Box, image: str, work: Path) -> dict:
    import container_episode as ce

    record: dict = {"box": box.name, "profile": box.profile or None, "plugs": box.plugs,
                    "object_store_plugged": box.store, "image": image}
    topo = None
    started = time.time()
    try:
        record["image_gate"] = ce.assert_image_current(image)
        seed = world_seed(box)
        with tool_tour.as_profile(box.profile, box.plugs, seed):
            world = bi.build_world(work)
        topo = tool_tour.TourTopology(world, work, image, box.profile, box.plugs, seed)
        topo.project = f"alkera-mfix-{box.name}-{int(time.time())}"
        record["project"] = topo.project
        print(f"  [{box.name}] stack up ({image}, project {topo.project})", flush=True)
        up = topo.up()
        record["services"] = up.get("services")
        record["minio_unstarted"] = topo.stack.minio_unstarted()
        nonce = hashlib.sha256(f"{topo.project}".encode()).hexdigest()[:10]
        record["checks"] = (measure_store(box, topo, nonce) if box.store
                            else measure_storeless(box, topo))
    except Exception as exc:  # noqa: BLE001 - the box fails, the check goes on
        record["error"] = f"{type(exc).__name__}: {exc}"
        print(f"  [{box.name}] ERROR {record['error'][:600]}", flush=True)
    finally:
        if topo is not None:
            try:
                topo.down()
            except Exception as exc:  # noqa: BLE001
                record.setdefault("error", f"teardown: {type(exc).__name__}: {exc}")
    checks = record.get("checks") or []
    record["ok"] = "error" not in record and bool(checks) and all(c["ok"] for c in checks)
    record["seconds"] = round(time.time() - started, 1)
    for c in checks:
        print(f"  [{box.name}] {'PASS' if c['ok'] else 'FAIL'} {c['check']}", flush=True)
    return record


def headline(records: list[dict]) -> str:
    parts = []
    for r in records:
        checks = r.get("checks") or []
        passed = sum(c["ok"] for c in checks)
        parts.append(f"{r['box']} {passed}/{len(checks)}" + ("" if r["ok"] else " FAIL"))
    return ("PASS" if all(r["ok"] for r in records) else "FAIL") + ": " + ", ".join(parts)


def _images(values: list[str]) -> dict[str, str]:
    out = {}
    for value in values:
        name, sep, tag = value.partition("=")
        if not sep or not tag:
            raise SystemExit(f"--image {value!r}: give BOX_PROFILE=TAG "
                             f"(da/laptop=..., unprofiled=...)")
        out[name] = tag
    return out


def image_for(box: Box, images: dict[str, str]) -> str:
    import container_episode as ce
    import episode

    key = box.profile or "unprofiled"
    if key in images:
        return images[key]
    profile = episode.environment_profile(box.profile, box.plugs) if box.profile else None
    return ce.profile_image(ce.DEFAULT_IMAGE, profile)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", action="append", default=[],
                        help="PROFILE=TAG (or unprofiled=TAG); default: the profile's image")
    parser.add_argument("--boxes", default=None,
                        help=f"comma-separated subset of {[b.name for b in BOXES]}")
    parser.add_argument("--out", type=Path, default=EVIDENCE)
    parser.add_argument("--work", type=Path, default=WORK_ROOT)
    args = parser.parse_args(argv)
    images = _images(args.image)
    boxes = [b for b in BOXES if args.boxes is None or b.name in args.boxes.split(",")]
    evidence = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "revision": bi._git_rev(), "boxes": []}
    for box in boxes:
        work = args.work / box.name
        subprocess.run(["rm", "-rf", str(work)], check=False)
        work.mkdir(parents=True, exist_ok=True)
        evidence["boxes"].append(run_box(box, image_for(box, images), work))
    evidence["ok"] = all(r["ok"] for r in evidence["boxes"])
    evidence["headline"] = headline(evidence["boxes"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=1, sort_keys=True, default=str) + "\n",
                        encoding="utf-8")
    print(f"\n  {evidence['headline']}\n  evidence: {args.out}")
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
