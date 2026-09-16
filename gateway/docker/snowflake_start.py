"""Start loopback fakesnow behind local TLS and publish its public trust bundle."""

import subprocess
import threading
import time
from pathlib import Path

import certifi
import uvicorn

HOSTNAME = "acme.snowflake.local"
SUBJECT_ALT_NAMES = f"DNS:{HOSTNAME},DNS:*.snowflake.local,DNS:localhost,IP:127.0.0.1"

TLS = Path("/tls")  # private: key + certificate, this service only
TRUST = Path("/etc/alkera/snowflake-tls")  # shared: certificate + bundle, no key
DATA = Path("/snowflake-data")  # writable state, outside the fingerprinted tree
UPSTREAM_PORT = 64616


def certificate() -> Path:
    """Regenerate the certificate and trust bundle together on each boot."""
    TLS.mkdir(parents=True, exist_ok=True)
    key, crt = TLS / "server.key", TLS / "server.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-days",
            "30",
            "-subj",
            f"/CN={HOSTNAME}",
            "-addext",
            f"subjectAltName={SUBJECT_ALT_NAMES}",
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
    """Bind the shared instance to persistent storage before accepting logins."""
    import fakesnow.server as fakesnow_server
    from fakesnow.instance import FakeSnow

    import snowflake_fidelity

    DATA.mkdir(parents=True, exist_ok=True)
    fakesnow_server.shared_fs = FakeSnow(db_path=str(DATA))
    # Patch fakesnow before the first login: qualified temp relations, the
    # 500 -> retry-storm catch-all, strict mode, the silent-wrong fences and session
    # identity. See gateway/docker/snowflake_fidelity.py; ALKERA_SNOWFLAKE_* tunes it.
    app = snowflake_fidelity.apply(fakesnow_server)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=UPSTREAM_PORT, log_level="info")
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
