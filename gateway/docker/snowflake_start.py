"""The Snowflake-shaped socket: fake hostname, local TLS, connector wire protocol.

B4 is the socket and nothing else - Snowflake semantics belong to the upstream mock.
What this script owns is that a real ``snowflake-connector-python`` client, handed a
hostname and no special flags, believes it has reached Snowflake:

* **hostname** - compose publishes ``HOSTNAME`` as a network alias on ``backend_net``,
  the LocalStack shape (a real DNS name that resolves locally) applied to our topology.
* **TLS** - a certificate whose CN and SAN cover that hostname, served on 8443.
* **trust** - the certificate is concatenated onto certifi and published, public half
  only, to the shared read-only trust volume. ``REQUESTS_CA_BUNDLE`` in the episode
  image already names that path, so the driver *verifies* rather than skips.
* **wire** - ``alkera_gateway.snowflake_proxy`` forwards only the connector routes.
  ``/api/v2/statements`` is not implemented upstream and is not proxied (S951).

Measured 2026-09-13 in this image, snowflake-connector-python 4.7.3:
``REQUESTS_CA_BUNDLE`` is the only trust knob the driver honours. ``SSL_CERT_FILE``
never reaches it and ``insecure_mode``/``disable_ocsp_checks`` govern OCSP only, not
chain verification - each of those fails the handshake on its own.
"""

import subprocess
import threading
import time
from pathlib import Path

import certifi
import uvicorn

HOSTNAME = "acme.snowflake.local"
SUBJECT_ALT_NAMES = f"DNS:{HOSTNAME},DNS:*.snowflake.local,DNS:localhost,IP:127.0.0.1"

TLS = Path("/tls")                          # private: key + certificate, this service only
TRUST = Path("/etc/alkera/snowflake-tls")   # shared: certificate + bundle, no key
DATA = Path("/snowflake-data")              # writable state, outside the fingerprinted tree
UPSTREAM_PORT = 64616


def certificate() -> Path:
    """Regenerate every boot so the served certificate and the published bundle
    can never disagree - a stale certificate in a surviving volume is the one way
    this plumbing fails silently."""
    TLS.mkdir(parents=True, exist_ok=True)
    key, crt = TLS / "server.key", TLS / "server.crt"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(crt), "-days", "30",
            "-subj", f"/CN={HOSTNAME}", "-addext", f"subjectAltName={SUBJECT_ALT_NAMES}",
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    return crt


def publish_trust(crt: Path) -> Path:
    """The private key never leaves this container; the certificate always does."""
    TRUST.mkdir(parents=True, exist_ok=True)
    (TRUST / "server.crt").write_bytes(crt.read_bytes())
    bundle = TRUST / "bundle.pem"
    bundle.write_bytes(Path(certifi.where()).read_bytes() + b"\n" + crt.read_bytes())
    return bundle


def upstream() -> None:
    """fakesnow on loopback, so only the TLS front door can reach it.

    Driven directly rather than through ``fakesnow.server()`` for two reasons:
    importing the ``fakesnow.server`` *module* shadows the same-named context
    manager on the package, and the shared instance has to be rebound before any
    login lands. fakesnow only honours ``FAKESNOW_DB_PATH`` when a *client* sends
    it in the login request, so passing it to ``server(session_parameters=...)``
    is a no-op - measured 2026-09-13, ``/snowflake-data`` stayed empty while state
    went to per-session ``/tmp/fakesnow_bucket_*``. Binding ``shared_fs`` here is
    what puts the fake warehouse's writable state in the declared volume.
    """
    import fakesnow.server as fakesnow_server
    from fakesnow.instance import FakeSnow

    DATA.mkdir(parents=True, exist_ok=True)
    fakesnow_server.shared_fs = FakeSnow(db_path=str(DATA))
    server = uvicorn.Server(
        uvicorn.Config(fakesnow_server.app, host="127.0.0.1", port=UPSTREAM_PORT, log_level="info")
    )
    thread = threading.Thread(target=server.run, name="fakesnow", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while thread.is_alive() and not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    if not server.started:
        raise RuntimeError("fakesnow upstream failed to start")


def main() -> None:
    from alkera_gateway.snowflake_proxy import main as serve

    publish_trust(certificate())
    upstream()
    serve()


if __name__ == "__main__":
    main()
