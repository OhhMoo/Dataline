"""Build the episode image from a clean worktree, then measure it from inside.

The pinned image exists because more than one agent edits `gateway/src` at a time:
an episode must run a build whose toolbelt is known, not whatever happens to be
uncommitted (see compose.episode.yml). This script is that recipe, made runnable so
the revision it was built from is stamped into the image instead of remembered.

    python gateway/docker/build_episode.py                 # HEAD, + pending docker/ files
    python gateway/docker/build_episode.py --no-overlay    # HEAD exactly
    python gateway/docker/build_episode.py --rev <sha> --tag alkera-episode:<sha>
    python gateway/docker/build_episode.py --toolbelt ds   # -> alkera-episode:head-ds
    python gateway/docker/build_episode.py --toolbelt da --warehouse-clients snowflake
                                                            # -> alkera-episode:head-da-snowflake

`--toolbelt` picks the role's lock (TOOLBELT.md, "Roles"): a tool a role does not get
is not installed in its image. `--warehouse-clients snowflake` adds
requirements-snowflake.lock to the gateway environment. Both become build-args, both
suffix the tag (de with no clients keeps the bare tag, so `alkera-episode:head` is
still the image every existing test and compose file means), and both are recorded
in evidence/image.json next to what the probe found installed.

`--overlay` (the default) copies the working tree's `gateway/docker/**` over the
checked-out revision, because the Dockerfile is the one file this build cannot take
from HEAD while its own change is still pending review. Nothing else is overlaid -
`gateway/src`, `world/` and the locks always come from the revision - and the file
list plus a content hash are stamped into `org.alkera.source.overlay` and written to
evidence/image.json. An image built with an overlay is still exactly reproducible
from the recorded inputs; it is simply not "HEAD", and says so.

The build is not trusted afterwards. `toolbelt_check.py` is piped into a container
made from the image and executes every binary, because reading the Dockerfile is not
evidence that the box ships what it claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CHECK = HERE / "toolbelt_check.py"
DEFAULT_TAG = "alkera-episode:head"
TOOLBELTS = ("de", "ds", "da")
WAREHOUSE_CLIENTS = ("snowflake",)
OVERLAY_DIR = "gateway/docker"


def run(argv, **kw):
    result = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(map(str, argv))}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def git(*args):
    return run(["git", "-C", str(REPO), *args]).strip()


def clients(value: str) -> list[str]:
    """`"snowflake"` / `"snowflake,"` / `""` -> a sorted, validated list."""
    names = sorted({c.strip() for c in value.split(",") if c.strip()})
    unknown = [c for c in names if c not in WAREHOUSE_CLIENTS]
    if unknown:
        raise SystemExit(f"unknown warehouse client(s) {unknown}; known: {WAREHOUSE_CLIENTS}")
    return names


def image_tag(base: str, toolbelt: str, warehouse_clients: str) -> str:
    """The bare tag is the de image with no extra clients; anything else says so."""
    suffix = [] if toolbelt == "de" else [toolbelt]
    suffix += clients(warehouse_clients)
    return "-".join([base, *suffix])


def build_args(toolbelt: str, warehouse_clients: str) -> list[str]:
    return [
        "--build-arg", f"ALKERA_TOOLBELT={toolbelt}",
        "--build-arg", f"ALKERA_WAREHOUSE_CLIENTS={','.join(clients(warehouse_clients))}",
    ]


def overlay_files() -> list[Path]:
    """Every file under gateway/docker that would ship, in a stable order."""
    src = REPO / OVERLAY_DIR
    return sorted(
        p
        for p in src.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.parent.name != "evidence"
    )


def overlay_hash(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(REPO).as_posix().encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def measure(tag: str) -> dict:
    """Execute the probe inside a container made from the image."""
    proc = subprocess.run(
        ["docker", "run", "--rm", "-i", "--entrypoint", "python", tag, "-"],
        input=CHECK.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    if not lines:
        raise RuntimeError(f"probe produced no JSON:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(lines[-1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--tag", default=None,
        help=f"default: {DEFAULT_TAG}, suffixed with the role (unless de) and any clients",
    )
    parser.add_argument("--toolbelt", choices=TOOLBELTS, default="de",
                        help="the role whose toolbelt lock the image installs")
    parser.add_argument("--warehouse-clients", default="",
                        help=f"comma-separated, from {WAREHOUSE_CLIENTS}; default none")
    parser.add_argument("--rev", default="HEAD")
    parser.add_argument(
        "--no-overlay",
        dest="overlay",
        action="store_false",
        help="build the revision exactly; do not carry pending gateway/docker files",
    )
    parser.add_argument("--out", type=Path, default=HERE / "evidence" / "image.json")
    parser.add_argument("--skip-build", action="store_true", help="measure an existing tag")
    args = parser.parse_args()
    if args.tag is None:
        args.tag = image_tag(DEFAULT_TAG, args.toolbelt, args.warehouse_clients)

    rev = git("rev-parse", args.rev)
    files = overlay_files() if args.overlay else []
    stamp = overlay_hash(files) if files else ""

    if not args.skip_build:
        with tempfile.TemporaryDirectory(prefix="alkera-wt-") as temp:
            tree = Path(temp) / "wt"
            git("worktree", "add", "--detach", str(tree), rev)
            try:
                for path in files:
                    target = tree / path.relative_to(REPO)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                print(f"building {args.tag} from {rev[:12]}"
                      f"{f' + {len(files)} pending {OVERLAY_DIR} files' if files else ''}")
                subprocess.run(
                    [
                        "docker", "build",
                        "--build-arg", f"ALKERA_SOURCE_REV={rev}",
                        "--build-arg", f"ALKERA_SOURCE_OVERLAY={stamp}",
                        *build_args(args.toolbelt, args.warehouse_clients),
                        "-t", args.tag,
                        "-f", str(tree / "gateway" / "docker" / "Dockerfile"),
                        str(tree),
                    ],
                    check=True,
                )
            finally:
                run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(tree)])

    spec = json.loads(run(["docker", "image", "inspect", args.tag]))[0]
    probe = measure(args.tag)
    labels = spec["Config"].get("Labels") or {}
    evidence = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "tag": args.tag,
        "image_id": spec["Id"],
        "repo_digests": spec.get("RepoDigests") or [],
        "size_bytes": spec.get("Size"),
        "labels": labels,
        "architecture": spec.get("Architecture"),
        # What was asked for, and what the image says it is. The probe's own
        # role_* fields are the third witness: what is actually importable.
        "toolbelt": {
            "requested": args.toolbelt,
            "label": labels.get("org.alkera.toolbelt"),
            "probe": probe.get("toolbelt_role"),
        },
        "warehouse_clients": {
            "requested": clients(args.warehouse_clients),
            "label": labels.get("org.alkera.warehouse_clients"),
        },
        "source_revision": rev,
        "head_revision": git("rev-parse", "HEAD"),
        "overlay": {
            "hash": stamp,
            "files": [p.relative_to(REPO).as_posix() for p in files],
        },
        "measured_inside_container": probe,
    }
    # An image labelled for another role (or for none: built before roles) is not
    # the image that was asked for, however green its probe is.
    evidence["toolbelt"]["matches"] = (
        evidence["toolbelt"]["label"] == args.toolbelt == evidence["toolbelt"]["probe"]
    )
    passed = probe["passed"] and evidence["toolbelt"]["matches"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print(
        json.dumps(
            {
                "artifact": str(args.out),
                "tag": args.tag,
                "image_id": spec["Id"],
                "size_mb": round(spec["Size"] / 1e6, 1),
                "source_revision": rev,
                "overlay_hash": stamp or None,
                "tool_count": probe["tool_count"],
                "binaries_ok": len(probe["binaries_ok"]),
                "binaries_failed": [r["binary"] for r in probe["binaries_failed"]],
                "python_not_shadowed": probe["python_not_shadowed"],
                "toolbelt": args.toolbelt,
                "warehouse_clients": clients(args.warehouse_clients),
                "architecture": spec.get("Architecture"),
                "role_forbidden_present": probe.get("role_forbidden_present"),
                "toolbelt_matches": evidence["toolbelt"]["matches"],
                "passed": passed,
            },
            indent=2,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
