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

Evidence: tasks/harness/evidence/minio_containment.json (`--out`). Exit 1 on any
failed check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
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
import concurrent.futures, hashlib, ipaddress, json, os, socket, struct
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
    episode = in_container(stack.container_id, {
        "names": [], "token_env": "ALKERA_MINIO_AUDIT_TOKEN",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "token_prefix": token[:TOKEN_PREFIX],
        "scan": ["/audit", "/workspace", "/root", "/etc", "/tmp", "/opt/episode"]})
    out.append(check("token_unseen",
                     not episode["token_in_env"] and not episode["token_scan"]["hits"],
                     {"token_in_env": episode["token_in_env"], **episode["token_scan"]}))
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
