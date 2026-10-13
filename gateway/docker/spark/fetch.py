"""Fetch the pinned Spark distribution and the Delta Lake jars, checksum-verified.

    python gateway/docker/spark/fetch.py spark --dest /opt/spark
    python gateway/docker/spark/fetch.py jars --dest /opt/spark/jars

Runs at IMAGE BUILD time only (gateway/docker/spark/Dockerfile.spark); the
engine container has no network and never fetches anything. Every byte is
pinned HERE: the Spark tarball by the sha512 Apache publishes beside it
(`<tgz>.sha512`), each Maven jar by its sha256, so a re-tagged or tampered
upstream artifact fails the build instead of becoming the engine an episode
runs against. The digest is checked before anything is unpacked or kept.
Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

SPARK_VERSION = "4.2.0"
SPARK_TGZ = f"spark-{SPARK_VERSION}-bin-hadoop3.tgz"
#: https://archive.apache.org/dist/spark/spark-4.2.0/spark-4.2.0-bin-hadoop3.tgz.sha512
SPARK_SHA512 = ("3a6559e8546ff387db8fe7a04b8fe4008853b467b972c2e343df8e14a8145053"
                "4777719fc5d4998dd96519a86fdf9de140cee753c61e2e94db044d5f9555ddc4")
#: Tried in order; the digest, not the host, is what is trusted.
SPARK_MIRRORS = (
    f"https://dlcdn.apache.org/spark/spark-{SPARK_VERSION}/{SPARK_TGZ}",
    f"https://archive.apache.org/dist/spark/spark-{SPARK_VERSION}/{SPARK_TGZ}",
)
MAVEN = "https://repo1.maven.org/maven2"
#: (group, artifact, version, sha256): Delta Lake 4.4.1 for Spark 4.2 (the only
#: Delta line built against 4.2) and the part of its compile-scope closure the
#: Spark distribution does not already carry. Measured 2026-10-13 by resolving
#: io.delta:delta-spark_4.2_2.13:4.4.1 with the distribution's own Ivy
#: (jars/ivy-*.jar) and dropping every artifact jars/ already has at a newer
#: version (jackson, parquet, hadoop-client-*, log4j, slf4j, RoaringBitmap,
#: scala-library, zstd, snappy, commons-*, aircompressor): one copy of each
#: class on the classpath, Spark's. jackson-datatype-jdk8 (which Spark lacks)
#: is taken at Spark's jackson version, 2.21.2, not the 2.13.5 Delta's POM
#: names, so the jackson modules on the classpath agree. Each sha256 was
#: computed from the Maven Central artifact and its published .sha1 checked.
JARS: tuple[tuple[str, str, str, str], ...] = (
    ("io.delta", "delta-spark_4.2_2.13", "4.4.1",
     "2b1d2cd411bf0bbced64b96fe621f8ffce7bb6e394c8064ab783fc6edce449be"),
    ("io.delta", "delta-storage", "4.4.1",
     "f585818d646cb3625b305270ec24f3e6f0b6ea63ed9bac1ae8f340f8faaeae8f"),
    ("io.delta", "delta-kernel-api", "4.4.1",
     "19c93b5e6740bb3001d3deca0b8c3fa5470197c520ffa36ffa58df98e91ae459"),
    ("io.delta", "delta-kernel-defaults", "4.4.1",
     "329995bbc9b2e5d74a9a7c9e968574c1e438bbb183aaf1b2c629a87ce8558fe2"),
    ("io.delta", "delta-kernel-unitycatalog", "4.4.1",
     "4d92b9248f8a5f9b3043fea2bb7666f0db26f5491e279c30868280be35450368"),
    ("io.unitycatalog", "unitycatalog-client", "0.6.0",
     "48257499a86b801a3e2372cec55ac226ca1c1c7eabe5c118e7302f29a1fdbc19"),
    ("io.unitycatalog", "unitycatalog-hadoop", "0.6.0",
     "c47b63c961c87102b9e1d63809a0915ba4c86b7652f57c8d39ca3a05b441a150"),
    ("com.fasterxml.jackson.datatype", "jackson-datatype-jdk8", "2.21.2",
     "e581bbf3fc5622d16b57366dd41e10fc56921fb202879de2ef487f87dd9b30e7"),
    ("javax.annotation", "javax.annotation-api", "1.3.2",
     "e04ba5195bcd555dc95650f7cc614d151e4bcd52d29a10b8aa2197f3ab89ab9b"),
)
CHUNK = 1 << 20


def _download(urls, digest: str, algorithm: str, out: Path) -> Path:
    errors = []
    for url in urls:
        for attempt in range(3):
            try:
                hasher = hashlib.new(algorithm)
                partial = out.with_name(out.name + ".partial")
                with urllib.request.urlopen(url, timeout=120) as response, \
                        open(partial, "wb") as stream:
                    while chunk := response.read(CHUNK):
                        hasher.update(chunk)
                        stream.write(chunk)
                if hasher.hexdigest() != digest:
                    partial.unlink(missing_ok=True)
                    raise SystemExit(f"{algorithm} mismatch for {url}: {hasher.hexdigest()}")
                partial.replace(out)
                return out
            except SystemExit:
                raise
            except Exception as exc:  # noqa: BLE001 - the next mirror, or the next try
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
                time.sleep(2 * (attempt + 1))
    raise SystemExit("download failed:\n  " + "\n  ".join(errors))


def fetch_spark(dest: Path, work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    tgz = _download(SPARK_MIRRORS, SPARK_SHA512, "sha512", work / SPARK_TGZ)
    dest.mkdir(parents=True, exist_ok=True)
    top = f"spark-{SPARK_VERSION}-bin-hadoop3/"
    with tarfile.open(tgz, mode="r:gz") as archive:
        members = []
        for member in archive.getmembers():
            if member.name == top.rstrip("/"):
                continue
            if not member.name.startswith(top):
                raise SystemExit(f"{SPARK_TGZ}: unexpected member {member.name!r}")
            member.name = member.name[len(top):]
            if member.name:
                members.append(member)
        archive.extractall(dest, members=members, filter="data")
    tgz.unlink()
    return dest


def fetch_jars(dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for group, artifact, version, sha256 in JARS:
        name = f"{artifact}-{version}.jar"
        url = f"{MAVEN}/{group.replace('.', '/')}/{artifact}/{version}/{name}"
        out.append(_download((url,), sha256, "sha256", dest / name))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("what", choices=("spark", "jars"))
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--work", type=Path, default=Path("/tmp/spark-fetch"))
    args = parser.parse_args(argv)
    if args.what == "spark":
        print(fetch_spark(args.dest, args.work))
        shutil.rmtree(args.work, ignore_errors=True)
    else:
        for path in fetch_jars(args.dest):
            print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
