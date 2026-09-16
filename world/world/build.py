"""seed.yaml -> world builder.

Produces the contract layout (agent.md §9.2.1) under <root>:
  workspace/    git repo: the theme's workspace/ tree + generated landing data
  var/warehouse/warehouse.duckdb   (raw loaded by the theme; staging/warehouse by dbt)
  var/sched/state.json             (scheduler history + v-clock)
  var/bi/dashboards/*.yml
  build/provenance.json

The world *shape* lives in world/themes/<name>/ (manifest + gen hooks + static
files); seed["theme"] (default "revenue_daily") selects it, and this module is
only the assembly line. Determinism rules enforced here: byte-verbatim copies,
duckdb PRAGMA threads=1 + dbt threads: 1, git author/committer dates pinned to
vtime_start, no wall-clock reads anywhere.
"""
import argparse
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import yaml

from . import __version__, executor, faults, observe, scheduler
from .hashing import file_hash, json_canon
from .themes import load_theme


def _seed_observability(root: Path, theme) -> None:
    """Copy the theme's metric/alarm definitions into var/obs/, verbatim.

    Verbatim, because the definitions are fingerprinted and a re-serialized
    YAML is a different set of bytes on a different libyaml. A theme with no
    obs/ directory seeds nothing, writes no samples, and keeps the fingerprint
    section count it had.
    """
    for name in observe.DEFINITION_NAMES:
        src = theme.dir / "obs" / name
        if src.is_file():
            dest = root / observe.OBS / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            return


def load_seed(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _write(path: Path, content: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)


def _git(workspace: Path, seed: dict) -> None:
    m = re.match(r"(.*)<(.*)>", seed["git_author"])
    env = {**os.environ,
           "GIT_AUTHOR_DATE": seed["vtime_start"],
           "GIT_COMMITTER_DATE": seed["vtime_start"]}

    def run(*args):
        subprocess.run(["git", *args], cwd=workspace, env=env, check=True,
                       capture_output=True)

    run("init", "-q")
    run("config", "user.name", m.group(1).strip())
    run("config", "user.email", m.group(2).strip())
    run("add", "-A")
    run("commit", "-q", "-m", f"world seed {seed['seed']}")


def _dbt_build(root: Path, build_dir: Path, profile: str) -> None:
    """Initial build. Same invocation the scheduler's `dbt` job kind uses -
    one implementation (executor.dbt_invoke), so the build and the episode run
    dbt identically. profiles.yml carries an absolute path, so it is written
    into build/, outside the fingerprinted workspace."""
    rc, output = executor.dbt_invoke(root, "build", out_dir=build_dir,
                                     profile=profile)
    if rc != 0:
        raise SystemExit(f"dbt build failed:\n{output}")


def build(seed_path: Path, root: Path, lock_path: Path | None = None) -> Path:
    seed = load_seed(seed_path)
    theme = load_theme(seed.get("theme", "revenue_daily"))
    root = Path(root)
    workspace, build_dir = root / "workspace", root / "build"
    db_path = root / "var" / "warehouse" / "warehouse.duckdb"
    for d in (workspace, build_dir, db_path.parent,
              root / "var" / "sched", root / "var" / "bi" / "dashboards"):
        d.mkdir(parents=True, exist_ok=True)

    shutil.copytree(theme.dir / "workspace", workspace, dirs_exist_ok=True)
    theme.gen.write_landing(workspace, seed)
    theme.gen.load_raw(workspace, db_path)
    _dbt_build(root, build_dir, theme.manifest["dbt_profile"])
    # Faults in the seeded backstory are opt-in per theme (`live.faults_in_
    # history`) and cover job faults only. Left off, initial_state produces the
    # byte-identical history it always did - which GT001 depends on, since it
    # calls initial_state directly.
    # `history_catalogue` is the single definition, because `write_ingest`
    # below reads the same list: the nights the scheduler records a failed poll
    # on and the nights the connector plane records a failed sync on must be
    # the same nights, decided once.
    history_faults = faults.history_catalogue(theme.manifest, seed) or None
    scheduler.save(root, scheduler.initial_state(
        seed, theme.manifest["jobs"], faults=history_faults,
        fault_seed=seed["seed"]))
    # The live-world control plane: the schedule, each job's exec block and the
    # arrival stream, so the world keeps evolving after the seed file is gone
    # (a restored snapshot has no seed). Outside the agent-visible surface.
    live = scheduler.initial_live(seed, theme)
    if live is not None:
        scheduler.save_live(root, live)
    write_ingest = getattr(theme.gen, "write_ingest", None)
    if write_ingest is not None:
        write_ingest(root, seed, theme.manifest)
    # The workflow plane (gateway/WORKFLOW_CONTRACT.md): the ticket the work
    # arrives in, the channel the context is scattered across, and the wiki
    # that is right in some places and out of date in others. Optional on the
    # same terms as write_ingest - a theme without the hook seeds nothing, gets
    # no var/workflow/, and keeps its recorded fingerprint. It runs AFTER
    # scheduler.save because the corpus is dated against the world clock, and
    # the hook reads `vnow` out of the state file rather than re-deriving it.
    write_workflow = getattr(theme.gen, "write_workflow", None)
    if write_workflow is not None:
        write_workflow(root, seed, theme.manifest)
    for f in sorted((theme.dir / "bi").glob("*.yml")):
        shutil.copyfile(f, root / "var" / "bi" / "dashboards" / f.name)
    # The observability store (OBS_STORE_CONTRACT.md). Definitions are world
    # state a theme seeds; the first sample is the reading the world held at
    # build time, which nothing can re-derive once advance has moved it on.
    _seed_observability(root, theme)
    observe.record(root, scheduler.load(root),
                   scheduler.parse_vtime(scheduler.load(root)["vnow"]), "build")
    _git(workspace, seed)

    provenance = {
        "seed_sha256": file_hash(Path(seed_path)),
        "generator_version": __version__,
        "theme": theme.name,
        "duckdb_version": duckdb.__version__,
        "dbt_version": importlib.metadata.version("dbt-core"),
        "dbt_duckdb_version": importlib.metadata.version("dbt-duckdb"),
        "requirements_lock_sha256": file_hash(lock_path) if lock_path else None,
        "image_digest": None,  # filled by the image build, never by hand
    }
    _write(build_dir / "provenance.json", json_canon(provenance) + b"\n")
    return root


def main(argv=None):
    p = argparse.ArgumentParser(prog="world.build")
    p.add_argument("--seed", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--lock", type=Path, default=None,
                   help="requirements.lock to attest in provenance")
    a = p.parse_args(argv)
    build(a.seed, a.root, a.lock)
    print(f"world built at {a.root}")


if __name__ == "__main__":
    sys.exit(main())
