"""minio_containment_check.py's own logic, without docker: the boxes it measures
agree with what the profiles plug, and its in-container probe reports what it
should. The measurement itself is tasks/harness/evidence/minio_containment.json."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import episode  # noqa: E402
import minio_containment_check as check  # noqa: E402


def test_boxes_agree_with_the_profiles():
    """A store-less box's checks expect no MinIO at all: if a profile started to
    plug the object store, the box would have to move to the store checks."""
    names = {box.profile for box in check.BOXES}
    assert {"da/laptop", "da/snowflake", ""} <= names
    for box in check.BOXES:
        if not box.profile:
            assert box.store and not box.notebook
            continue
        profile = episode.environment_profile(box.profile, box.plugs)
        assert box.store == ("object_store" in profile.slots), box
        assert box.notebook == ("notebook" in profile.slots), box


def test_forged_posts_cover_the_obvious_guesses():
    headers = check.forge_headers("wrong")
    assert {} in headers
    assert {"Authorization": check.SHARED_CREDENTIAL} in headers
    assert {"Authorization": "Bearer wrong"} in headers
    event = json.loads(check.forged_event("k.txt"))
    assert event["api"]["name"] == "PutObject" and event["api"]["object"] == "k.txt"


def _probe(spec: dict) -> dict:
    program = f"import json\nSPEC = json.loads({json.dumps(json.dumps(spec))})\n{check.PROBE}"
    done = subprocess.run([sys.executable, "-"], input=program, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.splitlines()[-1])


def test_probe_reports_resolution_refusal_and_a_planted_token(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "leak.txt").write_text("x deadbeefcafe0123 y")
    (tmp_path / "clean.txt").write_text("nothing here")
    got = _probe({"names": ["localhost"], "forge": [{}],
                  "receiver": "http://127.0.0.1:1/minio/audit",
                  "body": check.forged_event("k"),
                  "token_env": "ALKERA_MINIO_AUDIT_TOKEN", "token_sha256": "0" * 64,
                  "token_prefix": "deadbeefcafe0123", "scan": [str(tmp_path)]})
    assert isinstance(got["resolve"]["localhost"], list)
    assert got["forge"][0]["status"] is None and "refused" in got["forge"][0]["error"]
    assert got["token_in_env"] == []
    assert str(tmp_path / "a" / "b" / "leak.txt") in got["token_scan"]["hits"]
    assert str(tmp_path / "clean.txt") not in got["token_scan"]["hits"]


def test_probe_reports_any_open_port_besides_9000():
    """On a store box MinIO's address is dialed on EVERY TCP port, not only :9000:
    the console MinIO serves unless told not to (`minio:<random port>`,
    CONTAINMENT_2026-10-07.md) is a listener nobody audits, and `only_9000_open`
    has to see it wherever it lands. Here two listeners stand in for the API and
    a console on a random port; the sweep finds both and names the second."""
    import socket

    assert check.FULL_SWEEP == (1, 65535)
    spec = check.store_sweep_spec(["127.0.0.1"])
    assert (spec["port_sweep"]["first"], spec["port_sweep"]["last"]) == check.FULL_SWEEP
    servers = [socket.create_server(("127.0.0.1", 0)) for _ in range(2)]
    try:
        api, console = (s.getsockname()[1] for s in servers)
        got = _probe(spec)
    finally:
        for server in servers:
            server.close()
    sweep = got["port_sweep"]
    assert sweep["dialed"] == 65535
    assert {api, console} <= set(sweep["open"]["127.0.0.1"])
    others = check.other_open_ports(sweep, allowed=api)
    assert console in others["127.0.0.1"] and api not in others["127.0.0.1"]
    assert check.other_open_ports({"open": {"10.0.0.2": [9000]}}) == {}
    assert check.other_open_ports({"open": {"10.0.0.2": [9000, 41045]}}) == \
        {"10.0.0.2": [41045]}


# --------------------------------------------------------------------------
# the box's S3 identity (PEERS_2026-10-13.md): not MinIO's root
BOX = {"AWS_ACCESS_KEY_ID": "alkera-local-dev", "AWS_SECRET_ACCESS_KEY": "alkera-local-dev",
       "AWS_DEFAULT_REGION": "us-east-1"}


def _probe_env(spec: dict, env: dict) -> dict:
    import os

    program = f"import json\nSPEC = json.loads({json.dumps(json.dumps(spec))})\n{check.PROBE}"
    done = subprocess.run([sys.executable, "-"], input=program, capture_output=True,
                          text=True, timeout=120, env={**os.environ, **env})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.splitlines()[-1])


def _refusing_server():
    """A loopback HTTP server that records each request and answers it the way
    MinIO refuses one: 403 AccessDenied."""
    import http.server
    import threading

    seen = []

    class Refuse(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen.append({"method": self.command, "path": self.path,
                         "authorization": self.headers.get("Authorization") or "",
                         "body": self.rfile.read(length).decode("utf-8", "replace")})
            body = b'{"Code":"AccessDenied","Message":"Access Denied."}'
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_PUT = do_POST = do_DELETE = _answer

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Refuse)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def test_admin_probe_signs_each_attempt_with_the_box_credential():
    """The in-box probe drives MinIO's admin API the way the PEERS note did:
    SigV4 with botocore and the box's own AWS_* variables, one request per
    attempt, and it reports each answer by name."""
    attempts = check.admin_attempts(check.JIRA)
    server, seen = _refusing_server()
    try:
        got = _probe_env({"names": [], "admin": {
            "endpoint": f"http://127.0.0.1:{server.server_address[1]}",
            "attempts": attempts}}, BOX)
    finally:
        server.shutdown()
    assert got["admin"]["access_key"] == "alkera-local-dev"
    assert set(got["admin"]["attempts"]) == {a["name"] for a in attempts}
    assert all(r["status"] == 403 and "AccessDenied" in r["body"]
               for r in got["admin"]["attempts"].values())
    assert len(seen) == len(attempts)
    assert all(r["authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=alkera-local-dev/") for r in seen)
    by_path = {r["path"].split("?")[0]: r for r in seen}
    assert 'endpoint: "http://jira.internal:8080"' in by_path["/minio/admin/v3/start-job"]["body"]
    config = by_path["/minio/admin/v3/set-config-kv"]["body"]
    assert config.startswith("notify_webhook:") and "endpoint=http://jira.internal:8080/" in config
    assert any(r["path"] == "/minio/admin/v3/add-user?accessKey=alkera-local-dev"
               for r in seen)
    assert any(r["path"] == f"/{check.BUCKET}?notification" for r in seen)


def test_admin_attempts_cover_the_peers_reproduction():
    """Both PEERS rows that made MinIO dial: the batch job and the notify target
    (config set, restart, bucket event), each aimed at jira.internal:8080 - plus
    the box re-keying itself, which MinIO allows a user unless a policy denies
    it."""
    names = {a["name"] for a in check.admin_attempts(check.JIRA)}
    assert {"info", "start_job", "set_config_kv", "service_restart", "add_user_self",
            "put_bucket_notification"} <= names
    commands = dict(check.MC_ATTEMPTS)
    joined = " ".join(commands.values())
    for needle in ("admin info", "admin config set", "notify_webhook",
                   "admin service restart", "event add", "admin user svcacct add",
                   "admin user add"):
        assert needle in joined, needle
    assert f"endpoint={check.JIRA}/" in commands["notify_webhook_set"]
    assert check.JIRA == "http://jira.internal:8080"


def test_refusal_verdict_needs_every_attempt_refused_and_no_new_dial():
    refused = {"info": {"status": 403, "body": '{"Code":"AccessDenied"}'}}
    counts = {"jira.internal:8080": 0, "slack.internal:8081": 2}
    ok, detail = check.refusal_verdict(refused, counts, dict(counts))
    assert ok and detail["not_refused"] == {} and detail["destinations_moved"] == {}
    ok, detail = check.refusal_verdict(
        {**refused, "start_job": {"status": 200, "body": "{}"}}, counts, counts)
    assert not ok and list(detail["not_refused"]) == ["start_job"]
    ok, detail = check.refusal_verdict(
        {"info": {"status": 403, "body": "Forbidden"}}, counts, counts)
    assert not ok, "a 403 that is not MinIO's AccessDenied is not the refusal"
    ok, detail = check.refusal_verdict(refused, counts,
                                       {**counts, "jira.internal:8080": 4})
    assert not ok and detail["destinations_moved"] == {"jira.internal:8080": [0, 4]}
    assert not check.refusal_verdict({}, counts, counts)[0], "nothing tried proves nothing"


def test_mc_refusal_is_minios_access_denied():
    assert check.mc_refused({"rc": 1, "output": "mc: <ERROR> Unable to get service "
                                                "info. Access Denied."})
    assert not check.mc_refused({"rc": 0, "output": "Access Denied"})
    assert not check.mc_refused({"rc": 3, "output": "alias failed"}), \
        "an alias that never authenticated measured nothing"
    assert not check.mc_refused({"rc": 1, "output": "could not open a new TTY"})


def test_destination_counts_and_minio_root_parse_what_the_stack_writes(tmp_path):
    (tmp_path / "destinations.jsonl").write_text(
        '{"host": "jira.internal", "port": 8080, "count": 0}\n\n'
        '{"host": "slack.internal", "port": 8081, "count": 3}\n')
    assert check.destination_counts(tmp_path) == {"jira.internal:8080": 0,
                                                  "slack.internal:8081": 3}
    raw = (b"PATH=/usr/bin\0MINIO_ROOT_USER=alkera-minio-root\0MINIO_ROOT_PASSWORD="
           + b"ab" * 32 + b"\0HOME=/root\0")
    assert check.minio_root(raw) == {"user": "alkera-minio-root", "password": "ab" * 32}
    assert check.minio_root(b"PATH=/usr/bin\0") == {"user": "", "password": ""}


def test_secret_scan_spec_is_the_token_scan_for_any_secret(tmp_path):
    """root_secret_unseen asks the same in-box scan token_unseen asks, for the
    root secret; a planted prefix is found and the secret itself is not in the
    spec beyond its hash and that prefix."""
    secret = "f00d" * 16
    (tmp_path / "leak.txt").write_text(f"x {secret[:check.TOKEN_PREFIX]} y")
    spec = check.secret_scan_spec("MINIO_ROOT_PASSWORD", secret, [str(tmp_path)])
    assert secret not in json.dumps(spec)
    got = _probe_env(spec, {})
    assert got["token_in_env"] == []
    assert str(tmp_path / "leak.txt") in got["token_scan"]["hits"]
