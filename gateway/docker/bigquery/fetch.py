"""Fetch the pinned goccy/bigquery-emulator release binary, sha256-verified.

    python gateway/docker/bigquery/fetch.py            # this host's arch
    python gateway/docker/bigquery/fetch.py --arch arm64 --dest /opt/bq

Both linux arches are pinned HERE, copied from the release's checksums.txt, so a
re-tagged or tampered upstream asset fails the download instead of becoming the
emulator an episode runs against. The tarball digest is checked before anything
is unpacked. Prints the binary's path on success.
"""

import argparse
import hashlib
import io
import os
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "v0.8.1"
SHA256 = {
    "amd64": "8dbe6d3ebe3e8963523bada4a88a012d6a1bbc2ea779fbdc2e31d2e8c8c51c36",
    "arm64": "4c6913619c90b4a8a9378993e797774390289f9c94deba483d2a6db9cefbc8c5",
}
URL = (
    "https://github.com/goccy/bigquery-emulator/releases/download/"
    "{version}/bigquery-emulator_{version}_linux_{arch}.tar.gz"
)
ARCHES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
BINARY = "bigquery-emulator"


def default_dest() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "alkera" / "bigquery-emulator" / VERSION


def host_arch() -> str:
    machine = platform.machine().lower()
    if machine not in ARCHES:
        raise SystemExit(f"bigquery-emulator: no pinned linux binary for {machine!r}")
    return ARCHES[machine]


def fetch(arch: str | None = None, dest: Path | None = None) -> Path:
    arch = arch or host_arch()
    dest = (dest or default_dest()) / arch
    binary = dest / BINARY
    stamp = dest / "sha256"
    if binary.exists() and stamp.exists() and stamp.read_text().strip() == SHA256[arch]:
        return binary
    url = URL.format(version=VERSION, arch=arch)
    with urllib.request.urlopen(url, timeout=120) as response:
        payload = response.read()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != SHA256[arch]:
        raise SystemExit(f"bigquery-emulator: sha256 mismatch for {url}: {actual}")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        member = next(
            (m for m in archive.getmembers() if m.isfile() and Path(m.name).name == BINARY),
            None,
        )
        if member is None:
            raise SystemExit(f"bigquery-emulator: {url} contains no {BINARY}")
        data = archive.extractfile(member).read()
    dest.mkdir(parents=True, exist_ok=True)
    partial = binary.with_suffix(".partial")
    partial.write_bytes(data)
    partial.chmod(0o755)
    partial.replace(binary)
    stamp.write_text(SHA256[arch] + "\n")
    return binary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arch", choices=sorted(SHA256))
    parser.add_argument("--dest", type=Path)
    args = parser.parse_args(argv)
    print(fetch(args.arch, args.dest))


if __name__ == "__main__":
    sys.exit(main())
