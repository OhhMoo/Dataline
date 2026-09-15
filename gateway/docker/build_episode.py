"""Build the episode image from a clean worktree, then measure it from inside.

The pinned image exists because more than one agent edits `gateway/src` at a time:
an episode must run a build whose toolbelt is known, not whatever happens to be
uncommitted (see compose.episode.yml). This script is that recipe, made runnable so
the revision it was built from is stamped into the image instead of remembered.

    python gateway/docker/build_episode.py                 # HEAD, + pending docker/ files
    python gateway/docker/build_episode.py --no-overlay    # HEAD exactly
    python gateway/docker/build_episode.py --rev <sha> --tag alkera-episode:<sha>

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
    parser.add_argument("--tag", default=DEFAULT_TAG)
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
    evidence = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "tag": args.tag,
        "image_id": spec["Id"],
        "repo_digests": spec.get("RepoDigests") or [],
        "size_bytes": spec.get("Size"),
        "labels": spec["Config"].get("Labels") or {},
        "source_revision": rev,
        "head_revision": git("rev-parse", "HEAD"),
        "overlay": {
            "hash": stamp,
            "files": [p.relative_to(REPO).as_posix() for p in files],
        },
        "measured_inside_container": probe,
    }
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
                "passed": probe["passed"],
            },
            indent=2,
        )
    )
    return 0 if probe["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
