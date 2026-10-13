"""What world-down folds into /audit for the backends, and the TLS health probe. No Docker.

  * a backend's own `destinations.<name>.jsonl` is merged into destinations.jsonl by
    (host, port), the backend's row winning (container_episode.fold_destinations'
    rule), only for a DECLARED backend, idempotently;
  * episode.json `statement_logs` is checked (a log under /var/lib/alkera, outside the
    copied service state; a tailer of alkera_gateway.native.tailers), and each log is
    folded into the action log by the real tailer through the services' own writer -
    seq contiguous, every byte once across world-downs, from the start again when the
    log is a new file - with /audit/.statement_logs.json saying what happened;
  * world-up probes a hosted TLS surface over https, verified against the bundle the
    agent trusts, after asking the aggregate which surfaces speak TLS.

    gateway/.venv/bin/python -m pytest -q harbor/tests/test_world_down_folds.py
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BIN = REPO / "harbor" / "template" / "environment" / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import alkera_runtime as rt  # noqa: E402

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="the runtime reads /proc")

PG_LOG = "/var/lib/alkera/postgres/log/postgresql.log"
TAILER = "alkera_gateway.native.tailers.postgres"


def backend(name: str, port: int) -> dict:
    return {"name": name, "argv": ["/bin/true"], "user": "nobody", "ports": [port],
            "hosts": [f"{name}.internal"], "health": "tcp"}


def write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n"
                            for r in rows), encoding="utf-8")


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# --------------------------------------------------------------- destinations
def test_backend_destinations_fold_into_the_one_log(tmp_path):
    layout = rt.Layout(tmp_path)
    layout.audit.mkdir(parents=True)
    serve = [{"host": "jira.internal", "port": 8080, "count": 2},
             {"host": "native-services.internal", "port": 8099, "count": 1}]
    write_rows(layout.audit / rt.DESTINATIONS, serve)
    write_rows(layout.audit / "destinations.snowflake.jsonl",
               [{"host": "snowflake.internal", "port": 8443, "count": 3}])
    # a file whose backend this episode does not declare is not the episode's
    write_rows(layout.audit / "destinations.stranger.jsonl",
               [{"host": "stranger.internal", "port": 9999, "count": 9}])
    episode = {"backends": [backend("snowflake", 8443)]}
    assert rt.fold_backend_destinations(layout, episode)
    folded = read_rows(layout.audit / rt.DESTINATIONS)
    assert {(r["host"], r["port"]): r["count"] for r in folded} == {
        ("jira.internal", 8080): 2, ("native-services.internal", 8099): 1,
        ("snowflake.internal", 8443): 3}
    first = (layout.audit / rt.DESTINATIONS).read_bytes()
    rt.fold_backend_destinations(layout, episode)
    assert (layout.audit / rt.DESTINATIONS).read_bytes() == first   # idempotent
    # nothing declared, nothing to fold, nothing touched
    assert rt.fold_backend_destinations(layout, {"backends": []}) == []
    assert rt.fold_backend_destinations(layout, {}) == []


def test_a_backend_row_wins_over_a_stale_one(tmp_path):
    layout = rt.Layout(tmp_path)
    layout.audit.mkdir(parents=True)
    write_rows(layout.audit / rt.DESTINATIONS,
               [{"host": "redshift.internal", "port": 5439, "count": 1}])
    write_rows(layout.audit / "destinations.redshift.jsonl",
               [{"host": "redshift.internal", "port": 5439, "count": 4}])
    rt.fold_backend_destinations(layout, {"backends": [backend("redshift", 5439)]})
    assert read_rows(layout.audit / rt.DESTINATIONS) == [
        {"host": "redshift.internal", "port": 5439, "count": 4}]


# ------------------------------------------------------------ statement logs
@pytest.mark.parametrize("bad,match", [
    ([{"log": "/world/x.log", "tailer": TAILER}], "is not a distinct normalised path"),
    ([{"log": "/var/lib/alkera", "tailer": TAILER}], "is not a distinct"),
    ([{"log": "/var/lib/alkera/service-state/x.log", "tailer": TAILER}], "outside"),
    ([{"log": "/var/lib/alkera/a/../../../etc/x", "tailer": TAILER}], "normalised"),
    ([{"log": PG_LOG, "tailer": TAILER}, {"log": PG_LOG, "tailer": TAILER}], "distinct"),
    ([{"log": PG_LOG, "tailer": "os"}], "is not a module of"),
    ([{"log": PG_LOG, "tailer": "alkera_gateway.native.tailers.x; rm"}], "not a module"),
    ([{"log": PG_LOG}], "exactly the keys"),
    ({"log": PG_LOG}, "must be a list"),
])
def test_statement_logs_contract_is_checked(bad, match):
    with pytest.raises(rt.RuntimeFailure, match=match):
        rt.load_statement_logs({"statement_logs": bad})


def test_statement_logs_absent_is_none():
    assert rt.load_statement_logs({}) == []
    assert rt.load_statement_logs({"statement_logs": [{"log": PG_LOG, "tailer": TAILER}]}) \
        == [{"log": PG_LOG, "tailer": TAILER}]


def _pg_entry(pid: int, sql: str, user: str = "alkera") -> str:
    return (f"2026-10-07 14:00:00.{pid % 1000:03d} UTC [{pid}] {user}@alkera LOG:  "
            f"statement: {sql}\n")


@pytest.fixture
def pg_box(tmp_path):
    """An audit directory with two rows already in it, a world clock, and a
    Postgres statement log under the backend's private directory."""
    if not (REPO / "gateway" / ".venv" / "bin" / "python").exists():
        pytest.skip("the gateway venv runs the tailer")
    layout = rt.Layout(tmp_path)
    layout.audit.mkdir(parents=True)
    layout.run_dir.mkdir(parents=True)
    sched = layout.world / "var" / "sched"
    sched.mkdir(parents=True)
    (sched / "state.json").write_text(json.dumps({"vnow": "2026-03-05T09:00:00Z",
                                                  "runs": []}), encoding="utf-8")
    env = dict(os.environ, ALKERA_NATIVE_AUDIT=str(layout.audit / rt.ACTIONS),
               ALKERA_SCHEDULER=str(sched / "state.json"))
    for tool in ("episode.start", "shim.dbt"):
        done = subprocess.run(
            [sys.executable, "-c", "import json, sys\n"
             "from alkera_gateway.native.audit import record\n"
             f"record(tool={tool!r}, args={{}}, op_class='read', targets=[], actor='t')"],
            env=env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
    log = layout.at(PG_LOG)
    log.parent.mkdir(parents=True)
    log.write_text(_pg_entry(101, "SELECT 1") + _pg_entry(102, "SELECT count(*) "
                                                               "FROM warehouse.fct_orders"),
                   encoding="utf-8")
    episode = {"statement_logs": [{"log": PG_LOG, "tailer": TAILER}],
               "env": {rt.CLOCK_ENV: "2026-03-05T09:00:00Z"}}
    return layout, episode, log


def test_statement_log_folds_once_through_the_writer(pg_box):
    layout, episode, log = pg_box
    report = rt.tail_statement_logs(layout, episode, sys.executable)
    assert any("2 row(s) folded" in line for line in report), report
    rows = read_rows(layout.audit / rt.ACTIONS)
    assert [r["seq"] for r in rows] == [1, 2, 3, 4]                # contiguous
    assert [r["tool"] for r in rows[2:]] == ["psql.statement", "psql.statement"]
    assert rows[3]["args"]["sql"].startswith("SELECT count(*)")
    marker = json.loads((layout.audit / rt.STATEMENT_LOGS_MARKER).read_text())
    assert marker["schema"] == rt.STATEMENT_LOGS_SCHEMA
    assert marker["logs"][0]["rows"] == 2 and marker["logs"][0]["error"] is None
    # a second world-down folds nothing again
    rt.tail_statement_logs(layout, episode, sys.executable)
    assert len(read_rows(layout.audit / rt.ACTIONS)) == 4
    # what the server logged since: exactly that
    with open(log, "a", encoding="utf-8") as stream:
        stream.write(_pg_entry(103, "SELECT 2"))
    rt.tail_statement_logs(layout, episode, sys.executable)
    rows = read_rows(layout.audit / rt.ACTIONS)
    assert [r["seq"] for r in rows] == [1, 2, 3, 4, 5]
    assert rows[4]["args"]["sql"] == "SELECT 2"
    assert json.loads((layout.audit / rt.STATEMENT_LOGS_MARKER).read_text()
                      )["logs"][0]["rows_total"] == 3
    # a new log file (a fresh server): folded from its start
    log.unlink()
    log.write_text(_pg_entry(104, "SELECT 3"), encoding="utf-8")
    rt.tail_statement_logs(layout, episode, sys.executable)
    assert [r["args"]["sql"] for r in read_rows(layout.audit / rt.ACTIONS)[5:]] == [
        "SELECT 3"]


def test_a_missing_or_failing_log_is_recorded_not_folded(pg_box):
    layout, episode, log = pg_box
    log.unlink()
    report = rt.tail_statement_logs(layout, episode, sys.executable)
    assert any("never wrote one" in line for line in report)
    marker = json.loads((layout.audit / rt.STATEMENT_LOGS_MARKER).read_text())
    assert marker["logs"][0]["error"]
    log.write_text(_pg_entry(105, "SELECT 9"), encoding="utf-8")
    broken = {**episode, "statement_logs": [{"log": PG_LOG,
                                             "tailer": "alkera_gateway.native.tailers.nope"}]}
    report = rt.tail_statement_logs(layout, broken, sys.executable)
    assert any("not folded" in line for line in report)
    assert len(read_rows(layout.audit / rt.ACTIONS)) == 2
    # the next world-down with a working tailer folds it all
    rt.tail_statement_logs(layout, episode, sys.executable)
    assert read_rows(layout.audit / rt.ACTIONS)[-1]["args"]["sql"] == "SELECT 9"


def test_episode_json_statement_logs_are_checked_at_load(tmp_path):
    layout = rt.Layout(tmp_path)
    layout.episode_json.parent.mkdir(parents=True)
    layout.episode_json.write_text(json.dumps({
        "schema": rt.EPISODE_SCHEMA, "env": {},
        "statement_logs": [{"log": "/tmp/x.log", "tailer": TAILER}]}))
    with pytest.raises(rt.RuntimeFailure, match="statement_logs"):
        rt.load_episode(layout)


# ------------------------------------------------------------------ TLS health
def test_tls_ports_come_from_the_aggregate():
    payload = {"services": [
        {"service": "jira", "port": 8080, "url": "http://jira.internal:8080"},
        {"service": "databricks", "port": 443, "url": "https://databricks.internal:443"}]}
    assert rt.tls_ports(payload) == {443}
    assert rt.tls_ports({}) == set()


def test_check_answers_asks_the_aggregate_first(monkeypatch):
    calls = []

    def fake(port, timeout=rt.HEALTH_TIMEOUT_S, *, tls=False):
        calls.append((port, tls))
        if port == 8099:
            return 200, {"service": "native-services", "ok": True, "missing": {},
                         "port_disagreements": {}, "items": 3,
                         "services": [{"port": 443, "url": "https://databricks.internal:443"},
                                      {"port": 8080, "url": "http://jira.internal:8080"}]}
        return 200, {"service": "x", "items": 1}

    monkeypatch.setattr(rt, "probe_health", fake)
    rows = [{"host": "databricks.internal", "port": 443},
            {"host": "jira.internal", "port": 8080},
            {"host": rt.AGGREGATE_HOST, "port": 8099}]
    answers = rt.check_answers(rows)
    assert calls == [(8099, False), (443, True), (8080, False)]
    assert set(answers) == {"databricks.internal:443", "jira.internal:8080",
                            f"{rt.AGGREGATE_HOST}:8099"}


def test_a_tls_surface_is_verified_against_the_agents_bundle(tmp_path, monkeypatch):
    """A real HTTPS server on loopback with a certificate minted the way the
    backends mint theirs (127.0.0.1 in its names): verified with the bundle
    REQUESTS_CA_BUNDLE names, refused with any other."""
    key, crt = tmp_path / "server.key", tmp_path / "server.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(crt), "-days", "1",
                    "-subj", "/CN=tls.internal",
                    "-addext", "subjectAltName=DNS:tls.internal,IP:127.0.0.1"],
                   check=True, capture_output=True)

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):                                    # noqa: N802
            body = json.dumps({"service": "tls", "items": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Health)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(crt), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(crt))
        assert rt.probe_health(port, tls=True) == (200, {"service": "tls", "items": 1})
        other = tmp_path / "other.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(tmp_path / "o.key"), "-out", str(other),
                        "-days", "1", "-subj", "/CN=other"], check=True, capture_output=True)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(other))
        with pytest.raises(rt.RuntimeFailure, match="did not answer"):
            rt.probe_health(port, tls=True)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE")
        with pytest.raises(rt.RuntimeFailure, match="REQUESTS_CA_BUNDLE"):
            rt.probe_health(port, tls=True)
    finally:
        server.shutdown()
        server.server_close()
