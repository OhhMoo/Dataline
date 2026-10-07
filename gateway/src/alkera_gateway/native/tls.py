"""A TLS backend's certificate, minted at boot, and the trust it publishes.

One function, shared by every backend whose clients speak TLS (registry rows
with `tls=True` in `alkera_gateway.backends`): Snowflake's proxy
(gateway/docker/snowflake_start.py) today. Extracted from that file unchanged -
same openssl invocation, same files, same bundle bytes - so the Snowflake
container behaves exactly as it did.

Two directories, on purpose:

  private_dir   server.key + server.crt. The key never leaves the backend's own
                container (or native-services, for a hosted backend).
  trust_dir     server.crt + bundle.pem, where bundle.pem is certifi's CA list
                followed by this certificate. A shared volume mounts it
                read-only into the episode, whose REQUESTS_CA_BUNDLE names it -
                the only trust knob snowflake-connector-python honours
                (measured 2026-09-13; SSL_CERT_FILE never reaches it).

Regenerated on every boot: a certificate is never carried from one rollout to
the next.
"""
from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path


def subject_alt_names(host: str, alt_names: Sequence[str] = ()) -> str:
    """`DNS:<host>`, `DNS:<alt>` for each alias, then loopback (for a client
    inside the backend's own container)."""
    names = [f"DNS:{host}", *(f"DNS:{name}" for name in alt_names),
             "DNS:localhost", "IP:127.0.0.1"]
    return ",".join(names)


def mint(host: str, trust_dir: Path, *, private_dir: Path,
         alt_names: Sequence[str] = ()) -> tuple[Path, Path]:
    """A fresh self-signed certificate for `host` (CN) and its aliases.

    Writes `private_dir/server.key` (0600) and `private_dir/server.crt`, then
    publishes `trust_dir/server.crt` and `trust_dir/bundle.pem` = certifi + a
    newline + the certificate. Returns (certificate, key), the paths a TLS
    server is started with.
    """
    import certifi

    private_dir, trust_dir = Path(private_dir), Path(trust_dir)
    private, trust = private_dir.resolve(), trust_dir.resolve()
    if private == trust or trust in private.parents:
        # The trust directory is a volume the episode mounts: a key written
        # anywhere under it is a key the agent can read.
        raise ValueError(f"private_dir {private_dir} is inside trust_dir {trust_dir}; "
                         "the key must never be where the public bundle is published")
    private_dir.mkdir(parents=True, exist_ok=True)
    key, crt = private_dir / "server.key", private_dir / "server.crt"
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
            f"/CN={host}",
            "-addext",
            f"subjectAltName={subject_alt_names(host, alt_names)}",
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    # The private key never leaves the private directory; the certificate
    # always does.
    trust_dir.mkdir(parents=True, exist_ok=True)
    (trust_dir / "server.crt").write_bytes(crt.read_bytes())
    (trust_dir / "bundle.pem").write_bytes(
        Path(certifi.where()).read_bytes() + b"\n" + crt.read_bytes())
    return crt, key
