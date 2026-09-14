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

from . import __version__, scheduler
from .hashing import file_hash, json_canon
from .themes import load_theme


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


def _dbt_build(workspace: Path, db_path: Path, build_dir: Path,
               profile: str) -> None:
    proj = workspace / "dbt_project"
    profiles = {profile: {"target": "dev", "outputs": {"dev": {
        "type": "duckdb", "path": str(db_path), "threads": 1}}}}
    # profiles carry an absolute path, so they live outside the fingerprinted
    # workspace (in build/) to keep workspace content root-location-independent.
    _write(build_dir / "profiles.yml", yaml.safe_dump(profiles))
    # subprocess, not dbtRunner: dbt's in-process adapter pool would keep the
    # duckdb file locked after the build returns (and past snapshot/restore).
    r = subprocess.run(
        [sys.executable, "-c", "from dbt.cli.main import cli; cli()",
         "build", "--quiet", "--project-dir", str(proj),
         "--profiles-dir", str(build_dir),
         "--target-path", str(build_dir / "target"),
         "--log-path", str(build_dir / "logs")],
        env={**os.environ, "PYTHONUTF8": "1"}, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"dbt build failed:\n{r.stdout}\n{r.stderr}")


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
    _dbt_build(workspace, db_path, build_dir, theme.manifest["dbt_profile"])
    scheduler.save(root, scheduler.initial_state(seed, theme.manifest["jobs"]))
    write_ingest = getattr(theme.gen, "write_ingest", None)
    if write_ingest is not None:
        write_ingest(root, seed, theme.manifest)
    for f in sorted((theme.dir / "bi").glob("*.yml")):
        shutil.copyfile(f, root / "var" / "bi" / "dashboards" / f.name)
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
