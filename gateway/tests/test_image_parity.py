"""The shipped image must be the box we say it is.

Two measured defects on 2026-09-14 produced this file:

* `gateway/docker/Dockerfile` installed `git` and `openssl` and nothing else, while
  `tasks/harness/container_episode.py` drew an architecture diagram promising the
  agent "bash, git, dbt, psql, python". `psql` was not installed.
* The episode image served 36 tools. Nobody noticed, because nothing compared the
  running container against `alkera_gateway.tools.TOOLS`.

Every assertion here is taken from a container made from the image and probed from
inside it. Reading the Dockerfile proves nothing: `adobe/s3mock` ignored
`initialBuckets` on its pinned digest and the storage half of the acceptance script
reported green for weeks without ever having passed once.

Skips are deliberate and each names its reason. The one skip that is *not* cosmetic
is `test_image_ships_the_gateways_tools`: the episode image is built from a clean
worktree by contract, so it cannot contain tools that exist only in someone's
uncommitted working tree. When `gateway/src` is dirty that test skips and prints the
exact drift; when `gateway/src` is clean it fails loudly, which is the signal the
36-vs-63 gap needed and did not have.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from alkera_gateway.tools import TOOLS

REPO = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPO / "gateway" / "docker"
PROBE = DOCKER_DIR / "toolbelt_check.py"
TOOLS_SOURCE = "gateway/src/alkera_gateway/tools.py"
IMAGE = os.environ.get("ALKERA_EPISODE_IMAGE", "alkera-episode:head")
BUILD_RECIPE = (
    f"python gateway/docker/build_episode.py --tag {IMAGE}   "
    "(clean worktree at HEAD; records evidence/image.json)"
)


def _run(*argv, **kw):
    return subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw
    )


def _git(*argv):
    return _run("git", "-C", str(REPO), *argv)


def _tools_declared_at(rev: str) -> set[str]:
    """Tool names in `tools.py` at a revision, parsed rather than imported.

    Importing would need that revision's whole package on sys.path; the TOOLS table
    is a module-level dict literal, so the names are readable from the AST.
    """
    done = _git("show", f"{rev}:{TOOLS_SOURCE}")
    if done.returncode:
        return set()
    tree = ast.parse(done.stdout)
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "TOOLS" for t in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    return set()


@pytest.fixture(scope="session")
def image_report():
    """Run gateway/docker/toolbelt_check.py inside a container made from the image.

    The script is piped in over stdin rather than invoked from `/opt/stack`, so the
    test measures the image's *behaviour* and does not depend on where the probe
    happens to be baked - `container_episode.py` strips `/opt/stack` for episodes.
    """
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("image parity not exercised: no `docker` on PATH")
    if _run(docker, "image", "inspect", IMAGE, timeout=120).returncode:
        pytest.skip(
            f"image parity not exercised: `docker image inspect {IMAGE}` found nothing. "
            f"Build it with: {BUILD_RECIPE}"
        )
    done = subprocess.run(
        [docker, "run", "--rm", "-i", "--entrypoint", "python", IMAGE, "-"],
        input=PROBE.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    payload = [line for line in done.stdout.splitlines() if line.startswith("{")]
    assert payload, (
        f"the probe produced no JSON inside {IMAGE}; that is a broken image, not a "
        f"broken test.\nstdout: {done.stdout[-2000:]}\nstderr: {done.stderr[-2000:]}"
    )
    return json.loads(payload[-1])


@pytest.fixture(scope="session")
def image_labels():
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("image parity not exercised: no `docker` on PATH")
    done = _run(docker, "image", "inspect", IMAGE, "--format", "{{json .Config.Labels}}")
    if done.returncode:
        pytest.skip(
            f"image parity not exercised: {IMAGE} is not built. Build it with: {BUILD_RECIPE}"
        )
    return json.loads(done.stdout.strip() or "null") or {}


@pytest.fixture(scope="session")
def image_revision(image_labels):
    rev = image_labels.get("org.alkera.source.revision")
    if not rev:
        pytest.skip(
            f"{IMAGE} carries no org.alkera.source.revision label, so what source it "
            f"holds cannot be established. Rebuild with: {BUILD_RECIPE}"
        )
    return rev


def test_every_shipped_binary_actually_runs(image_report):
    """The toolbelt, executed. `which` alone would pass for a broken venv shim."""
    failed = image_report["binaries_failed"]
    assert not failed, (
        f"{len(failed)} binaries the image claims to ship do not run inside {IMAGE}: "
        + json.dumps(failed, indent=2)
    )
    # The diagram in tasks/harness/container_episode.py promises these by name.
    promised = {"bash", "git", "dbt", "psql", "python"}
    missing = promised - set(image_report["binaries_ok"])
    assert not missing, (
        f"container_episode.py's diagram promises the agent {sorted(promised)}; "
        f"{sorted(missing)} do not run in {IMAGE}. A comment describing a box we do "
        "not ship is the defect this file exists to stop."
    )


def test_image_ships_the_duckdb_postgres_extension(image_report):
    """A DuckDB primary attaches a Postgres peer with `LOAD postgres`
    (gateway/src/alkera_gateway/warehouses.py). The episode zone has no egress,
    so the extension cannot be INSTALLed at run time; the Dockerfile bakes it in
    and this asserts it LOADS inside the image, on a connection with
    autoinstall/autoload off - the way the gateway opens every connection."""
    extensions = image_report.get("duckdb_extensions")
    assert extensions is not None, (
        f"{IMAGE} was measured by a toolbelt_check.py that predates the extension "
        "probe; rebuild and re-measure: " + BUILD_RECIPE
    )
    postgres = extensions.get("postgres") or {}
    assert postgres.get("loaded"), (
        f"`LOAD postgres` does not work inside {IMAGE}: {postgres}. A dual-warehouse "
        "world with a Postgres peer cannot boot there. " + BUILD_RECIPE
    )
    assert postgres.get("sha256"), "the extension file was loaded but not found on disk"


def test_the_toolbelt_venv_does_not_shadow_the_gateways_python(image_report):
    """/opt/toolbelt must be appended to PATH, never prepended.

    Its `python` has neither the gateway nor its locked dependencies; if it won the
    PATH lookup, every tool in the image would break in a way the Dockerfile reads
    as fine.
    """
    assert image_report["python_not_shadowed"], (
        f"`python` resolves to {image_report['python_on_path']} inside {IMAGE}, not "
        f"/usr/local/bin/python. PATH is {image_report['path']}"
    )


def test_image_tool_table_matches_the_source_it_was_built_from(image_report, image_revision):
    """The build shipped what its own revision declares - no silently dropped module."""
    declared = _tools_declared_at(image_revision)
    if not declared:
        pytest.skip(
            f"{IMAGE} names revision {image_revision[:12]}, which this checkout cannot "
            f"read ({TOOLS_SOURCE} not resolvable there)."
        )
    shipped = set(image_report["tools"])
    assert shipped == declared, (
        f"{IMAGE} serves {len(shipped)} tools but revision {image_revision[:12]} declares "
        f"{len(declared)}. missing from image: {sorted(declared - shipped)}; "
        f"extra in image: {sorted(shipped - declared)}"
    )


def test_image_was_built_from_current_head(image_revision):
    """The image must not be a generation behind the committed gateway.

    Scoped to the paths that go into the image, so a commit touching only `tasks/`
    or `implementation_structure/` does not falsely condemn a current image.
    """
    head = _git("rev-parse", "HEAD").stdout.strip()
    if image_revision == head:
        return
    drift = _git("diff", "--name-only", image_revision, head, "--", "gateway", "world")
    if drift.returncode:
        pytest.skip(
            f"{IMAGE} names revision {image_revision[:12]}, which is not in this "
            "checkout; its currency cannot be established here."
        )
    changed = [line for line in drift.stdout.splitlines() if line.strip()]
    assert not changed, (
        f"{IMAGE} was built from {image_revision[:12]} but HEAD is {head[:12]}, and "
        f"{len(changed)} image-bearing files changed between them: {changed[:10]}. "
        f"Every rollout against this image measures a stale box. Rebuild: {BUILD_RECIPE}"
    )


def test_image_ships_the_gateways_tools(image_report):
    """The literal parity check: container tool set vs `alkera_gateway.tools.TOOLS`.

    Fails loudly when they diverge. Skips - loudly, printing the whole drift - only
    when `tools.py` is uncommitted-modified, because the episode image is built from
    a clean worktree by contract and holding it to source no commit contains would
    make this test permanently and uninformatively red.
    """
    shipped = set(image_report["tools"])
    live = set(TOOLS)
    if shipped == live:
        return
    dirty = _git("status", "--porcelain", "--", TOOLS_SOURCE).stdout.strip()
    detail = (
        f"image {IMAGE} serves {len(shipped)} tools; alkera_gateway.tools.TOOLS has "
        f"{len(live)}. missing from image: {sorted(live - shipped)}; "
        f"extra in image: {sorted(shipped - live)}"
    )
    if dirty:
        pytest.skip(
            f"{detail}. NOT the image's fault: {TOOLS_SOURCE} is uncommitted "
            f"({dirty!r}), and the episode image is built from a clean worktree so a "
            "rollout runs a known toolbelt. Commit the tools, then rebuild: "
            f"{BUILD_RECIPE}"
        )
    raise AssertionError(
        f"{detail}. {TOOLS_SOURCE} is committed, so this image is simply stale and "
        f"every container rollout is running a toolbelt a generation behind. {BUILD_RECIPE}"
    )
