"""The box sweep: every door a rollout-built native box opens to its episode.

P6 (`containment.py --native --attach`) says whether only the fake services are
reachable; P7 (`--native-episode`) what the episode's filesystem holds. This
says, door by door, what the episode's network REACHES on a box an environment
profile is served, and whether anything audits each one - the third instrument
of gateway/docker/evidence/CONTAINMENT_2026-10-07.md, whose driver was kept
outside the repository until now. It writes `containment-native-box[-<slug>]
.json` in the shape the 2026-10-07 files have:

  p6_probe_on_the_box   `native_probe.py` (source on stdin to the episode's own
                        interpreter) with `containment.p6_plan`'s inputs and the
                        full sweep (question D): every address of the episode
                        network, the gateway included, every port of each live
                        one, a banner from each open port no expected door
                        explains
  sweep                 that sweep, plus: names resolved from the episode,
                        connects to every container's backend_net address, and
                        Postgres logins as `episode` (ALKERA_POSTGRES_DSN's
                        credentials) and as `alkera`, each carrying a marker
                        that is then looked up in the server's role-scoped
                        statement log
  open_ports_classified each open port: an audited door (declared in
                        destinations*.jsonl, or postgres:5432 for role alkera,
                        or minio:9000 behind the audit webhook), a designed door
                        (the notebook plug), or NOT an audited door, labelled
  not_audited           the last kind, plus a login that works and leaves no
                        statement in the log

Read-only from the episode: connects, a request line to unknown ports, two
`select current_user` statements. Nothing is written to the world. The
measurement dials every port, so the destination counts it reads are the
sweep's own as much as anyone's: run it on a stack built for measuring, never
on a rollout that will be scored.

Usage::

    # a stack that is already up (a rollout's, the tool tour's)
    python gateway/docker/box_sweep.py --project <live project> \\
        --out gateway/docker/evidence/containment-native-box-<slug>.json \\
        [--p7 gateway/docker/evidence/containment-native-episode-<slug>.json]

    # bring one up, measure P6 --attach, P7 and the sweep, tear it down
    python gateway/docker/box_sweep.py --up --env-profile de/databricks \\
        --image alkera-episode:<tag> --work <scratch> --slug de-databricks
    python gateway/docker/box_sweep.py --up --task GT001-aov-halving \\
        --image alkera-episode:<tag> --work <scratch> --slug ""

`--up` with `--env-profile` builds the world and the stack the way the tool
tour does (`tool_tour.world_seed` + `bi_e2e_check.build_world` +
`tool_tour.TourTopology`, i.e. `NativeContainerEpisode.up()` + `prepare()`);
with `--task`, the way `container_episode.py native-up` does. Run it under
`scripts/docker_slot.sh`; world builds want a TMPDIR with room.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import containment
import native_episode_probe

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
EVIDENCE = HERE / "evidence"
#: Ports tried on every backend_net address from the episode, which must all
#: fail: each is a port something in the stack serves on backend_net.
BACKEND_PORTS = (5432, 5439, 8443, 9000, 9050, 9060, 443, 8080, 8100)
#: Where the Postgres server keeps its role-scoped statement log
#: (compose.native.yml: log_directory=log under PGDATA, postgresql.log).
STATEMENT_LOG = "/var/lib/postgresql/data/log/postgresql.log"
#: The credentials the stack's own files ship, used when the episode's
#: environment no longer carries them: `episode` from compose.yml's
#: ALKERA_POSTGRES_DSN (the role Task 7 stops from logging in), `alkera` from
#: compose.native.yml's pg_native_role.
EPISODE_ROLE = {"user": "episode", "password": "episode-local"}
AGENT_ROLE = {"user": "alkera", "password": "alkera-local-dev", "dbname": "alkera"}

AUDITED = "audited door (declared in destinations*.jsonl, service writes 9.2.2 rows)"
POSTGRES = "audited door for role alkera only (role-scoped statement log; see pg_logins)"
MINIO = "audited door (MinIO audit webhook to native-services:8110)"
JUPYTER = ("designed door (notebook plug): every executed cell is a jupyter.cell row, "
           "terminals disabled; the connection is not counted in destinations")
MINIO_UNPLUGGED = ("NOT AN AUDITED DOOR: MinIO S3 API on a box that plugs no "
                   "object_store (no :8110 receiver audits it)")
EPISODE_LOGIN = ("NOT AUDITED: role `episode` logs in with the credentials of "
                 "ALKERA_POSTGRES_DSN, and its statement is absent from the "
                 "role-scoped statement log")

#: Run in the episode with `SPEC = ...` prepended, like native_episode_probe.
EXTRA_PROBE = r"""
import json, os, socket
spec = json.loads(SPEC)
out = {"names": {}, "off_network": [], "pg_logins": [], "routes": []}
for name in spec["names"]:
    try:
        out["names"][name] = sorted({a[4][0] for a in socket.getaddrinfo(name, None)})
    except OSError:
        out["names"][name] = []
for host, port in spec["off_network"]:
    try:
        socket.create_connection((host, port), timeout=2).close()
        result = "CONNECTED"
    except OSError as exc:
        result = type(exc).__name__
    out["off_network"].append({"target": "%s:%d" % (host, port), "result": result})

def quad(word):
    return ".".join(str(int(word[i:i + 2], 16)) for i in (6, 4, 2, 0))

for line in open("/proc/net/route").read().splitlines()[1:]:
    f = line.split()
    if len(f) > 7:
        out["routes"].append({"iface": f[0], "dest": quad(f[1]), "gateway": quad(f[2]),
                              "mask": quad(f[7])})
try:
    import psycopg
except ImportError as exc:
    psycopg = None
    out["pg_error"] = "%s: %s" % (type(exc).__name__, exc)
SCHEMAS = ("select nspname, pg_get_userbyid(nspowner), has_schema_privilege(nspname, "
           "'USAGE'), has_schema_privilege(nspname, 'CREATE') from pg_namespace "
           "where nspname in ('raw', 'staging', 'warehouse') order by 1")
TABLES = ("select schemaname, tablename, tableowner, has_table_privilege(quote_ident("
          "schemaname) || '.' || quote_ident(tablename), 'SELECT'), "
          "has_table_privilege(quote_ident(schemaname) || '.' || quote_ident(tablename), "
          "'INSERT') from pg_tables where schemaname in ('raw', 'staging', 'warehouse') "
          "order by 1, 2 limit 20")
for login in spec["logins"] if psycopg else []:
    row = {"label": login["label"], "user": login["user"],
           "dsn": "%s:%s/%s" % (login["host"], login["port"], login["dbname"])}
    try:
        with psycopg.connect(host=login["host"], port=login["port"],
                             dbname=login["dbname"], user=login["user"],
                             password=login["password"], connect_timeout=5) as conn:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("select /* %s */ current_user, current_database()" % login["marker"])
            row["identity"] = list(cur.fetchone())
            # Logged in. What the role may touch is recorded after, and a
            # privilege error there (a schema closed to it) is not a failed login.
            row["connected"] = True
            for key, query in (("superuser", "select rolsuper from pg_roles "
                                             "where rolname = current_user"),
                               ("schemas", SCHEMAS), ("tables", TABLES)):
                try:
                    cur.execute(query)
                    found = cur.fetchall()
                    row[key] = found[0][0] if key == "superuser" else \
                        [[str(v) for v in r] for r in found]
                except Exception as exc:
                    row.setdefault("privilege_errors", {})[key] = (
                        "%s: %s" % (type(exc).__name__, exc))[:300]
    except Exception as exc:
        row.setdefault("connected", False)
        row["error"] = ("%s: %s" % (type(exc).__name__, exc))[:300]
    out["pg_logins"].append(row)
print(json.dumps(out, sort_keys=True))
"""


def run_in_episode(container: str, program: str, spec: dict, timeout: int = 600) -> dict:
    source = f"SPEC = {json.dumps(json.dumps(spec))}\n{program}"
    done = subprocess.run(
        ["docker", "exec", "-i", container, containment.CONTAINER_PYTHON, "-"],
        input=source, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout)
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"the probe printed nothing in {container[:12]}: "
                           f"{done.stderr[-2000:]}")
    return json.loads(lines[-1])


def classify(open_targets, *, containers: list[dict], network: str, gateway: str | None,
             destinations: list[dict], store_plugged: bool,
             banners: dict | None = None) -> list[dict]:
    """Each open `address:port` the sweep found, as a door: who answers there
    (`containment.service_facts` rows on `network`) and whether anything
    audits it. `destinations` are destinations*.jsonl rows (`host`, `port`):
    a declared one is an audited door at whatever address carries its host."""
    at, declared = {}, set()
    for row in containers:
        address = row["networks"].get(network)
        if address:
            at[address] = row
    for dest in destinations:
        for row in containers:
            if dest.get("host") in (row["aliases"].get(network) or []):
                declared.add((row["networks"][network], int(dest["port"])))
    out = []
    for target in open_targets:
        address, _, port = target.rpartition(":")
        port = int(port)
        row = at.get(address)
        service = row["service"] if row else None
        if (address, port) in declared:
            kind = AUDITED
        elif service == "postgres" and port == 5432:
            kind = POSTGRES
        elif service == "minio" and port == containment.MINIO_PORT:
            kind = MINIO if store_plugged else MINIO_UNPLUGGED
        elif service == "jupyter" and port == 8888:
            kind = JUPYTER
        else:
            kind = containment.door_label(address, port, gateway=gateway, service=service)
        entry = {"target": target, "service": service or "?",
                 "names": sorted(set(row["aliases"].get(network) or [])) if row else [],
                 "class": kind}
        if kind.startswith("NOT") and (banners or {}).get(target):
            entry["banner"] = banners[target]
        out.append(entry)
    return out


def not_audited(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["class"].startswith("NOT")]


def _env(spec: dict) -> dict:
    return dict(item.partition("=")[::2] for item in spec["Config"].get("Env") or [])


def _by_service(specs: list[dict], service: str) -> dict | None:
    return next((s for s in specs
                 if s["Config"]["Labels"].get("com.docker.compose.service") == service), None)


def logins(env: dict, marker: str) -> list[dict]:
    """The three Postgres logins the sweep tries, each tagged with a marker."""
    rows = []
    dsn = env.get("ALKERA_POSTGRES_DSN")
    episode = dict(EPISODE_ROLE)
    if dsn:
        parts = urlsplit(dsn)
        episode = {"user": parts.username or "episode", "password": parts.password or ""}
        rows.append({"label": "env_dsn", "host": parts.hostname, "port": parts.port or 5432,
                     "dbname": parts.path.lstrip("/") or "postgres", **episode})
    rows.append({"label": "episode_role", "host": "postgres.internal", "port": 5432,
                 "dbname": AGENT_ROLE["dbname"], **episode})
    rows.append({"label": "alkera_role", "host": "postgres.internal", "port": 5432,
                 "dbname": env.get("PGDATABASE") or AGENT_ROLE["dbname"],
                 "user": env.get("PGUSER") or AGENT_ROLE["user"],
                 "password": env.get("PGPASSWORD") or AGENT_ROLE["password"]})
    for row in rows:
        row["marker"] = f"{marker}-{row['label']}"
    return rows


def destinations_declared(specs: list[dict]) -> list[dict]:
    """Every destinations*.jsonl row in the audit directory, read where
    native-services writes it (its `/audit` mount)."""
    services = _by_service(specs, "native-services")
    if services is None:
        return []
    done = subprocess.run(
        ["docker", "exec", services["Id"], "sh", "-c",
         "for f in /audit/destinations*.jsonl; do [ -f \"$f\" ] && "
         "echo \"### $f\" && cat \"$f\"; done"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    rows, current = [], None
    for line in done.stdout.splitlines():
        if line.startswith("### "):
            current = Path(line[4:]).name
        elif line.strip():
            try:
                rows.append({**json.loads(line), "file": current})
            except ValueError:
                continue
    return rows


def statement_lines(specs: list[dict], marker: str) -> list[str]:
    pg = _by_service(specs, "postgres")
    if pg is None:
        return []
    done = subprocess.run(["docker", "exec", pg["Id"], "cat", STATEMENT_LOG],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    return [line for line in done.stdout.splitlines() if marker in line]


def sweep_box(project: str, *, p7: Path | None = None, built_by: str | None = None,
              image: str | None = None, episode_endpoints: dict | None = None) -> dict:
    """The box document for a stack that is up. No up, no down."""
    from alkera_gateway.plugs import parse_plug_args

    specs = containment.project_specs(project)
    if not specs:
        raise SystemExit(f"no running containers in compose project {project!r}")
    rows = [containment.service_facts(s) for s in specs]
    profile, plug_values = containment.served_profile(specs)
    plugs = parse_plug_args(plug_values)
    net = containment.network_facts(containment.episode_network_name(specs, project))
    plan = containment.p6_plan(rows, net["name"], profile=profile, plugs=plugs,
                               rollout_built=True)
    episode = _by_service(specs, "episode")
    started = time.monotonic()
    probe = containment.run_native_probe(
        episode["Id"], [*plan["argv"], "--self-test", "--sweep-cidr", net["subnet"]])
    found = probe.get("sweep") or {}
    expectation = native_episode_probe.expectations(profile, plug_values)
    store = "minio" in expectation["doors"]
    names = sorted({*(a for r in rows for al in r["aliases"].values() for a in al),
                    *(r["container"] for r in rows), *plan["controls"],
                    *(h for hosts in expectation["excluded"].values()
                      for h in ([hosts] if isinstance(hosts, str) else hosts)),
                    *(host for host, _ in containment.NATIVE_BACKENDS.values())})
    off_network = [[address, port] for r in rows
                   for name, address in r["networks"].items()
                   if name != net["name"] and address for port in BACKEND_PORTS]
    marker = f"box-sweep-{project}"
    env = _env(episode)
    extra = run_in_episode(episode["Id"], EXTRA_PROBE, {
        "names": names, "off_network": off_network, "logins": logins(env, marker)})
    lines = statement_lines(specs, marker)
    logged = {row["label"]: any(f"{marker}-{row['label']}" in line for line in lines)
              for row in extra["pg_logins"]}
    banners = {row["target"]: row.get("banner") for row in probe.get("not_audited") or []}
    destinations = destinations_declared(specs)
    classified = classify(found.get("open") or [], containers=rows, network=net["name"],
                          gateway=net["gateway"], destinations=destinations,
                          store_plugged=store, banners=banners)
    doors = not_audited(classified)
    episode_login = next((r for r in extra["pg_logins"] if r["label"] == "episode_role"), {})
    if episode_login.get("connected") and not logged.get("episode_role"):
        doors.append({"target": "postgres.internal:5432 (role episode)", "service": "postgres",
                      "names": ["postgres.internal"], "class": EPISODE_LOGIN,
                      "login": {k: episode_login.get(k) for k in
                                ("identity", "superuser", "schemas", "dsn")}})
    document = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "project": project,
        "built_by": built_by or "attached to a live project (box_sweep.py --project)",
        "image": image or (episode["Config"]["Image"] if episode else None),
        "image_revision": ((episode or {}).get("Config", {}).get("Labels") or {}).get(
            "org.alkera.source.revision"),
        "environment_profile": profile,
        "plugs": {k: list(v) for k, v in plugs.items()},
        "episode_network": net,
        "services_up": sorted(r["service"] for r in rows),
        "containers": [{"service": r["service"], "id": s["Id"], "image": r["image"],
                        "networks": r["networks"], "aliases": r["aliases"],
                        "published_ports": r["published_ports"]}
                       for r, s in zip(rows, specs, strict=True)],
        **({"episode_endpoints": episode_endpoints} if episode_endpoints else {}),
        "p6_probe_on_the_box": {
            "how": ("gateway/docker/native_probe.py, source on stdin to the episode's "
                    "/usr/local/bin/python, with containment.p6_plan's inputs (the "
                    "profile's services as TCP, backends by the service that carries "
                    "their alias, compose names only for services on episode_net, "
                    "controls minus those plus every service off it and every "
                    "excluded host) and --sweep-cidr of the episode network"),
            "backends": plan["backends_probed"],
            "controls": plan["controls"],
            "removed_controls_that_are_running_compose_services":
                plan["controls_removed_because_on_episode_net"],
            "compose_names": plan["compose_names"],
            "must_not_resolve_off_episode_net": plan["must_not_resolve_off_episode_net"],
            "plugged_backends_not_carried": plan["plugged_backends_not_carried"],
            "exit": probe.get("exit"),
            "probe": probe,
        },
        "sweep": {
            "vantage": "episode container (docker exec, /usr/local/bin/python)",
            "own_addresses": probe.get("own_addresses"),
            "routes": extra["routes"],
            "cidr": found.get("cidr"),
            "gateway": net["gateway"],
            "live_addresses": found.get("live_addresses"),
            "liveness_port9": found.get("liveness"),
            "port_scan": {
                "addresses": found.get("live_addresses"),
                "open": found.get("open"),
                "ports": found.get("ports"),
                "timeout_s": found.get("timeout_s"),
                "result_counts": found.get("result_counts"),
                "neither_open_nor_refused_first_200":
                    found.get("neither_open_nor_refused_first_200"),
            },
            "names": extra["names"],
            "off_network": extra["off_network"],
            "pg_logins": extra["pg_logins"],
            **({"pg_error": extra["pg_error"]} if extra.get("pg_error") else {}),
            "env_credentials": {k: v for k, v in env.items() if k.endswith("_DSN")},
            "seconds": round(time.monotonic() - started, 1),
        },
        "statement_log": logged,
        "statement_log_lines_with_marker": lines,
        "destinations_declared": destinations,
        "open_ports_classified": classified,
        "not_audited": doors,
        "off_network_reached": [r["target"] for r in extra["off_network"]
                                if r["result"] == "CONNECTED"],
    }
    if p7 is not None:
        verdict = json.loads(Path(p7).read_text(encoding="utf-8")).get("passed")
        document["p7"] = {"artifact": Path(p7).name, "passed": verdict,
                          "exit": 0 if verdict else 1}
    return document


def write(document: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def summary(document: dict, path: Path) -> dict:
    return {"artifact": str(path), "environment_profile": document["environment_profile"],
            "p6_probe_passed": document["p6_probe_on_the_box"]["probe"].get("passed"),
            "p7": document.get("p7"),
            "not_audited": [f"{r['target']}  {r['class']}" for r in document["not_audited"]],
            "off_network_reached": document["off_network_reached"],
            "statement_log": document["statement_log"]}


def up_and_measure(args) -> int:
    """`--up`: one stack, three instruments, `down`. Each instrument's verdict
    is in its own file; this returns non-zero only when one could not run."""
    sys.path.insert(0, str(REPO / "tasks" / "harness"))
    work = Path(args.work).resolve()
    tail = f"-{args.slug}" if args.slug else ""
    evidence = Path(args.evidence_dir)
    p6_out = evidence / f"containment-native{tail}.json"
    p7_out = evidence / f"containment-native-episode{tail}.json"
    box_out = evidence / f"containment-native-box{tail}.json"
    project = args.project or f"alkera-t4b-{args.slug or 'base'}"
    from alkera_gateway.plugs import parse_plug_args

    plugs = {k: list(v) for k, v in parse_plug_args(args.env_plug).items()}
    topo, endpoints = None, None
    if args.env_profile:
        import bi_e2e_check as bi
        import tool_tour

        work.mkdir(parents=True, exist_ok=True)
        seed = tool_tour.world_seed(args.env_profile, plugs)
        with tool_tour.as_profile(args.env_profile, plugs, seed):
            world = bi.build_world(work)
        topo = tool_tour.TourTopology(world, work, args.image, args.env_profile, plugs, seed)
        topo.project = project
        built_by = ("tool_tour.TourTopology (bi_e2e_check.ContainerTopology: "
                    "container_episode.NativeContainerEpisode + prepare)")
        world_root = Path(world["world_root"])
    else:
        built_by = f"container_episode.py native-up --task {args.task}"
        world_root = work / "world"
    cli = [sys.executable, str(REPO / "tasks" / "harness" / "container_episode.py")]
    status = 0
    try:
        if topo is not None:
            topo.up()
            endpoints = topo.stack.episode_endpoints()
        else:
            subprocess.run([*cli, "native-up", "--dir", str(work), "--project", project,
                            "--task", args.task, "--image", args.image], check=True)
        here = [sys.executable, str(HERE / "containment.py")]
        if not args.skip_p6:
            done = subprocess.run([*here, "--native", "--attach", project,
                                   "--out", str(p6_out)])
            print(f"box_sweep: P6 --attach exit {done.returncode}", flush=True)
        done = subprocess.run([*here, "--native-episode", "--project", project,
                               "--world", str(world_root), "--out", str(p7_out)])
        print(f"box_sweep: P7 exit {done.returncode}", flush=True)
        document = sweep_box(project, p7=p7_out, built_by=built_by, image=args.image,
                             episode_endpoints=endpoints)
        write(document, box_out)
        print(json.dumps(summary(document, box_out), indent=2), flush=True)
    except Exception as exc:  # noqa: BLE001 - the stack still comes down
        print(f"box_sweep: {type(exc).__name__}: {exc}", flush=True)
        status = 2
    finally:
        if topo is not None:
            topo.down()
        else:
            subprocess.run([*cli, "native-down", "--dir", str(work), "--project", project,
                            "--task", args.task, "--image", args.image], check=False)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="box_sweep")
    parser.add_argument("--project", default=None,
                        help="the live compose project to sweep (or, with --up, "
                             "the name to bring it up under)")
    parser.add_argument("--out", type=Path, default=None,
                        help="the box document (containment-native-box-<slug>.json)")
    parser.add_argument("--p7", type=Path, default=None,
                        help="the P7 artifact measured on this same stack, recorded "
                             "by name and verdict")
    parser.add_argument("--built-by", default=None)
    parser.add_argument("--up", action="store_true",
                        help="bring the stack up, run P6 --attach, P7 and the sweep, "
                             "tear it down")
    parser.add_argument("--env-profile", default=None, metavar="ROLE/STACK")
    parser.add_argument("--env-plug", action="append", default=[],
                        metavar="SLOT=PLUG[,PLUG]")
    parser.add_argument("--task", default="GT001-aov-halving",
                        help="with --up and no --env-profile: the task whose world "
                             "and stack native-up builds")
    parser.add_argument("--image", default=None)
    parser.add_argument("--work", default=None, help="with --up: the scratch directory")
    parser.add_argument("--slug", default=None,
                        help="with --up: the evidence file suffix ('' for the "
                             "unprofiled box)")
    parser.add_argument("--evidence-dir", default=str(EVIDENCE))
    parser.add_argument("--skip-p6", action="store_true",
                        help="with --up: no containment-native<slug>.json (the "
                             "unprofiled box's P6 is the static stack's, --native)")
    args = parser.parse_args(argv)
    if args.up:
        if not (args.image and args.work and args.slug is not None):
            parser.error("--up needs --image, --work and --slug")
        return up_and_measure(args)
    if not (args.project and args.out):
        parser.error("--project and --out (or --up)")
    document = sweep_box(args.project, p7=args.p7, built_by=args.built_by)
    write(document, args.out)
    print(json.dumps(summary(document, args.out), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
