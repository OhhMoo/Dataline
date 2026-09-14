"""B4 acceptance: does a real Snowflake driver believe this socket is Snowflake?

Writes ``evidence/snowflake_socket.json``. Scope is the socket - hostname, TLS, wire.
Snowflake *semantics* are Andrew's and are not asserted here.

The load-bearing check is the negative control. A client that trusts everything will
"pass" a TLS test that proves nothing, so this runs the same connection twice: once
with the published bundle, once with the default trust store. The first must succeed
and the second must fail. Only that pair shows the certificate is being verified.

    python snowflake_acceptance.py                  # brings the stack up itself
    python snowflake_acceptance.py --keep-up
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from containment import HERE, Stack, out

HOSTNAME = "acme.snowflake.local"
PORT = 8443
BUNDLE = "/etc/alkera/snowflake-tls/bundle.pem"
PROBE_IMAGE = "alkera-snowflake:plumbing"

INSIDE = r'''
import json, os, socket, ssl, sys
import snowflake.connector as sc

HOST, PORT, BUNDLE = "acme.snowflake.local", 8443, "/etc/alkera/snowflake-tls/bundle.pem"
result = {"driver_version": sc.__version__ if hasattr(sc, "__version__") else "unknown"}

try:
    result["dns"] = sorted({i[4][0] for i in socket.getaddrinfo(HOST, PORT)})
except OSError as exc:
    result["dns"] = f"{type(exc).__name__}: {exc}"

def handshake(cafile):
    ctx = ssl.create_default_context(cafile=cafile)
    with socket.create_connection((HOST, PORT), 5) as raw:
        with ctx.wrap_socket(raw, server_hostname=HOST) as tls:
            cert = tls.getpeercert()
            return {"ok": True, "tls_version": tls.version(),
                    "subject": [list(map(list, r)) for r in cert["subject"]],
                    "subject_alt_name": [list(p) for p in cert.get("subjectAltName", ())]}

for tag, cafile in [("published_bundle", BUNDLE), ("default_trust", None)]:
    try:
        result[f"handshake_{tag}"] = handshake(cafile)
    except Exception as exc:
        result[f"handshake_{tag}"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

BASE = dict(account="acme", user="alkera", password="local", host=HOST, port=PORT,
            protocol="https", login_timeout=10, network_timeout=10)

def roundtrip(bundle):
    if bundle:
        os.environ["REQUESTS_CA_BUNDLE"] = bundle
    else:
        os.environ.pop("REQUESTS_CA_BUNDLE", None)
    conn = sc.connect(**BASE)
    cur = conn.cursor()
    rows = {}
    for tag, sql in [
        ("select", "select 1 as n"),
        ("create_db", "create database if not exists analytics"),
        ("create_schema", "create schema if not exists analytics.warehouse"),
        ("create_table",
         "create table if not exists analytics.warehouse.socket_check(id int, amount decimal(10,2))"),
        ("insert", "insert into analytics.warehouse.socket_check values (1, 10.50)"),
        ("read_back", "select id, amount from analytics.warehouse.socket_check order by id"),
        ("drop", "drop table analytics.warehouse.socket_check"),
    ]:
        cur.execute(sql)
        rows[tag] = [[str(v) for v in row] for row in cur.fetchall()]
    cur.close(); conn.close()
    return rows

# With the published bundle: must round-trip.
try:
    result["driver_with_bundle"] = {"ok": True, "rows": roundtrip(BUNDLE)}
except Exception as exc:
    result["driver_with_bundle"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

# Negative control: without it the driver must refuse, or the test above proved nothing.
try:
    roundtrip(None)
    result["driver_without_bundle"] = {"ok": True, "error": None}
except Exception as exc:
    result["driver_without_bundle"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

# S951 says fakesnow implements the connector wire protocol and not the public REST
# API. Confirm both halves against this socket rather than restating the claim.
os.environ["REQUESTS_CA_BUNDLE"] = BUNDLE
import requests
wire = {}
for path, payload in [("/session/v1/login-request",
                       {"data": {"SESSION_PARAMETERS": {}, "ACCOUNT_NAME": "acme",
                                 "LOGIN_NAME": "alkera", "PASSWORD": "local"}}),
                      ("/api/v2/statements", {"statement": "select 1"}),
                      ("/telemetry/send", {})]:
    try:
        response = requests.post(f"https://{HOST}:{PORT}{path}", json=payload,
                                 verify=BUNDLE, timeout=10)
        body = response.text[:160]
        wire[path] = {"status": response.status_code, "body": body}
    except Exception as exc:
        wire[path] = {"status": None, "error": f"{type(exc).__name__}: {exc}"[:160]}
result["wire_routes"] = wire
print(json.dumps(result))
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="alkera-b2b4-snowflake")
    parser.add_argument("--out", type=Path, default=HERE / "evidence" / "snowflake_socket.json")
    parser.add_argument("--keep-up", action="store_true")
    args = parser.parse_args()

    stack = Stack(args.project, ["snowflake"])
    evidence = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "project": args.project, "hostname": HOSTNAME, "port": PORT}
    try:
        stack.compose("up", "-d", "--wait")
        specs = {c["Config"]["Labels"]["com.docker.compose.service"]: c for c in stack.containers()}
        snowflake = specs["snowflake"]
        networks = snowflake["NetworkSettings"]["Networks"]
        backend = next(iter(networks))
        evidence["service_networks"] = {
            name: {"ip": net["IPAddress"], "aliases": net.get("Aliases") or []}
            for name, net in networks.items()
        }
        trust = next(m["Name"] for m in snowflake["Mounts"]
                     if m["Destination"] == "/etc/alkera/snowflake-tls")
        evidence["trust_volume"] = trust
        evidence["private_key_mounted_into_episode"] = any(
            m["Destination"] == "/tls" for m in specs["episode"]["Mounts"])

        # The client runs in its own container so DNS, TLS and the wire are all
        # crossed for real; nothing is proved from inside the server.
        evidence["client"] = json.loads(out([
            "docker", "run", "--rm", "--network", backend,
            "-v", f"{trust}:/etc/alkera/snowflake-tls:ro",
            "--entrypoint", "python", PROBE_IMAGE, "-c", INSIDE,
        ]))

        # The gateway's own container is the intended client: it must resolve the
        # hostname and already carry the bundle.
        evidence["episode_view"] = json.loads(stack.compose(
            "exec", "-T", "episode", "python", "-c",
            "import json,os,socket;"
            "r={};"
            "\ntry: r['dns']=sorted({i[4][0] for i in socket.getaddrinfo('%s',%d)})"
            "\nexcept OSError as e: r['dns']=type(e).__name__"
            "\nr['bundle_env']=os.environ.get('REQUESTS_CA_BUNDLE')"
            "\nr['bundle_bytes']=os.path.getsize(r['bundle_env']) if r['bundle_env'] "
            "and os.path.exists(r['bundle_env']) else None"
            "\nr['sees_private_key']=os.path.exists('/tls/server.key')"
            "\nprint(json.dumps(r))" % (HOSTNAME, PORT),
        ).stdout.strip())

        # The agent's zone must not see the socket at all.
        evidence["agent_view"] = stack.run_json("harness", (
            "import json,socket\n"
            "r={}\n"
            "try:\n"
            " r['dns']=sorted({i[4][0] for i in socket.getaddrinfo('%s',%d)})\n"
            "except OSError as e:\n"
            " r['dns']=type(e).__name__\n"
            "try:\n"
            " socket.create_connection(('%s',%d),timeout=2).close(); r['tcp']='CONNECTED'\n"
            "except OSError as e:\n"
            " r['tcp']=type(e).__name__\n"
            "print(json.dumps(r))" % (HOSTNAME, PORT, HOSTNAME, PORT)))
    finally:
        if not args.keep_up:
            stack.compose("down", "-v", "-t", "5", check=False)

    client, episode, agent = evidence["client"], evidence["episode_view"], evidence["agent_view"]
    checks = {
        "hostname_resolves_in_backend_zone": isinstance(client["dns"], list) and bool(client["dns"]),
        "hostname_resolves_for_gateway": isinstance(episode["dns"], list) and bool(episode["dns"]),
        "hostname_hidden_from_agent_zone": not isinstance(agent["dns"], list)
                                           and agent["tcp"] != "CONNECTED",
        "tls_verifies_against_published_bundle": client["handshake_published_bundle"]["ok"],
        "tls_rejected_without_bundle": not client["handshake_default_trust"]["ok"],
        "san_covers_hostname": any(
            HOSTNAME in pair for pair in
            client.get("handshake_published_bundle", {}).get("subject_alt_name", [])),
        "driver_roundtrip": client["driver_with_bundle"]["ok"],
        "driver_refuses_untrusted": not client["driver_without_bundle"]["ok"],
        "gateway_carries_bundle": bool(episode.get("bundle_bytes")),
        "private_key_stays_in_snowflake_service": not evidence["private_key_mounted_into_episode"]
                                                  and not episode["sees_private_key"],
        "connector_wire_route_served": client["wire_routes"]
            ["/session/v1/login-request"]["status"] == 200,
        "rest_api_absent_as_documented": client["wire_routes"]
            ["/api/v2/statements"]["status"] == 404,
    }
    evidence["checks"] = checks
    evidence["passed"] = all(checks.values())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"artifact": str(args.out), **checks, "passed": evidence["passed"]}, indent=2))
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
