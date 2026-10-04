"""seed.yaml -> world builder.

Produces the contract layout (agent.md §9.2.1) under <root>:
  workspace/    git repo: the theme's workspace/ tree + generated landing data
  var/warehouse/warehouse.duckdb   (raw loaded by the theme; staging/warehouse by dbt)
  var/sched/state.json             (scheduler history + v-clock)
  var/bi/dashboards/*.yml
  var/warehouse/<name>.duckdb       (only when seed `warehouses:` names one)
  build/warehouses.json            (same condition; what was declared)
  home/                            (only when seed `native: true`: the box's
                                    HOME, carrying ~/.dbt/profiles.yml and
                                    ~/.config/alkera/services.toml)
  var/world/profile.json           (only when seed `profile:` names one;
                                    `plugs` in it only when the seed gives them)
  workspace/data/{exports,api,events}/  (only when seed `files:` asks; files.py)
  workspace/bi/{powerbi,tableau,sigma}/ (only when the profile plugs that BI
                                    vendor under `plugs: {bi:}`; bi_projects.py)
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

from . import (
    __version__,
    bi_projects,
    executor,
    faults,
    files,
    native_workspace,
    observe,
    scheduler,
    warehouse_seed,
)
from .hashing import file_hash, json_canon, json_hash
from .themes import load_theme

# The gateway's `WarehouseSpec.name` rule (gateway/src/alkera_gateway/
# warehouse_spec.py:19), spelled here rather than imported: world/ never
# imports alkera_gateway (gateway_scheduler.py:8), and a name the gateway would
# refuse at boot should be refused at build, where the task author is looking.
WAREHOUSE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
# `primary` is the default `warehouse_name` the gateway gives the first
# warehouse; `warehouse` is the primary file's stem, so a second DuckDB file of
# that name would be the primary file.
RESERVED_WAREHOUSE_NAMES = ("primary", "warehouse")
WAREHOUSES_RECORD = Path("build") / "warehouses.json"

# The environment slots, in `alkera_gateway.plugs.SLOTS` order - spelled here,
# not imported, for the same layering reason as WAREHOUSE_NAME; world/tests/
# test_profile_world.py fails if the two lists drift.
SLOTS = ("files", "notebook", "compute", "warehouse", "transform", "orchestrator",
         "object_store", "ingest", "bi", "observability", "release", "human")
# Where a built world says which profile it was built for. Under var/, not
# build/: it is world state (fingerprint.py's `profile` section reads it) and
# must survive a snapshot restore, whose SCOPE is workspace + var.
PROFILE_RECORD = Path("var") / "world" / "profile.json"


def resolve_profile(seed: dict) -> dict | None:
    """The seed's `profile: {name, slots, plugs?}`, validated and canonicalised.

    The harness resolves which slots an environment profile plugs (that is
    `alkera_gateway.plugs`' job, and world/ never imports it) and writes the
    list into the seed; the world only honours it. Slots come back in SLOTS
    order, so the same set spelled in another order is the same profile and
    the same hash. Loud on anything malformed: an unknown slot is almost
    always a typo, and a typo'd `obsevability` that silently unplugged the
    real one would build a world without metrics and say nothing.

    `plugs: {slot: [plug, ...]}` is optional and says WHICH plug fills a
    slot, where the slot list alone cannot: `bi` is Metabase on the laptop
    stack and Power BI, Tableau or Sigma on another, and the discovery layer
    (native_workspace) names an explicit-only vendor only when this says so.
    Plug NAMES are the gateway's to know (world/ never imports it), so they are
    checked for shape only - non-empty strings, no repeats - and each slot must
    be one the profile plugs: a plug for a slot the box does not have is a
    contradiction, not a no-op. Recorded only when given and non-empty, plug
    lists sorted, so every profile record written before the key existed is
    byte for byte unchanged and `[tableau, powerbi]` hashes as `[powerbi,
    tableau]`.

    None when the seed has no `profile:` - the build every recorded hash
    was taken from.
    """
    if "profile" not in seed:
        return None
    spec = seed["profile"]
    if not isinstance(spec, dict) or not ({"name", "slots"} <= set(spec)
                                          <= {"name", "slots", "plugs"}):
        raise SystemExit("seed `profile:` must be a mapping with exactly `name` and "
                         "`slots`, and optionally `plugs`")
    name, slots = spec["name"], spec["slots"]
    if not isinstance(name, str) or not name:
        raise SystemExit("seed `profile: {name:}` must be a non-empty string")
    if not isinstance(slots, list):
        raise SystemExit("seed `profile: {slots:}` must be a list of slot names")
    unknown = [s for s in slots if s not in SLOTS]
    if unknown:
        raise SystemExit(f"seed profile {name!r} names unknown slot(s) {unknown!r} "
                         f"(known: {', '.join(SLOTS)})")
    if len(set(slots)) != len(slots):
        raise SystemExit(f"seed profile {name!r} names a slot twice")
    out = {"name": name, "slots": [s for s in SLOTS if s in slots]}
    plugs = _resolve_plugs(name, slots, spec.get("plugs"))
    if plugs:
        out["plugs"] = plugs
    return out


def _resolve_plugs(name: str, slots: list, plugs) -> dict:
    """`profile: {plugs:}` -> {slot: sorted plug names}, in SLOTS order."""
    if plugs is None:
        return {}
    if not isinstance(plugs, dict):
        raise SystemExit(f"seed profile {name!r}: `plugs:` must be a mapping of "
                         "slot -> list of plug names")
    for slot, names in plugs.items():
        if slot not in SLOTS:
            raise SystemExit(f"seed profile {name!r} plugs unknown slot {slot!r} "
                             f"(known: {', '.join(SLOTS)})")
        if slot not in slots:
            raise SystemExit(f"seed profile {name!r} plugs slot {slot!r}, which "
                             f"its `slots:` does not have")
        if not isinstance(names, list) or not names:
            raise SystemExit(f"seed profile {name!r}: `plugs: {{{slot}:}}` must be a "
                             "non-empty list of plug names")
        if not all(isinstance(n, str) and n for n in names):
            raise SystemExit(f"seed profile {name!r}: `plugs: {{{slot}:}}` must name "
                             f"each plug as a non-empty string, not {names!r}")
        if len(set(names)) != len(names):
            raise SystemExit(f"seed profile {name!r} names a {slot!r} plug twice")
    return {slot: sorted(plugs[slot]) for slot in SLOTS if slot in plugs}


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


def _seed_warehouses(root: Path, seed: dict, theme) -> list[dict]:
    """A SECOND (third, ...) live warehouse, seeded without the connector.

    Two declarations meet here. The theme's manifest carries the catalogue -
    which warehouses this world shape *can* run, under `warehouses:`, each
    with a backend, its read-only/attach flags and whether dbt may target it
    - and the seed file says which of them this world *does* run, under
    `warehouses: [name, ...]`. A seed that names none builds the world it
    always built: no file, no record, no fingerprint section (fingerprint.py's
    `warehouses` section is conditional on the file existing), so every
    recorded hash stays put. Same opt-in shape as `faults:`.

    The rows come from the theme's own gen hook, `seed_<name>(root, seed,
    manifest, name)` (or the `hook:` the manifest names), which returns the
    `tables` mapping `warehouse_seed.seed_duckdb` / `seed_postgres` take:
    `{"schema.table": (csv_path, {column: TYPE})}`. The hook writes CSVs; the
    engine bulk-loads them. Nothing here goes through `alkera_gateway` - a
    world seeded through the connector would put its own construction into
    the episode's action log (warehouse_seed.py, first paragraph).

    A DuckDB warehouse lives at `var/warehouse/<name>.duckdb`, and that is a
    rule rather than a default: fingerprint.py hashes "every *.duckdb beside
    the primary", executor.write_profiles renders a dbt output per such file
    named by its stem, and the gateway addresses the warehouse by `name`. One
    file name ties the three together, so the name IS the path.

    A Postgres warehouse is seeded into the server `dsn_env` names and its
    logical hash is recorded, because the gateway refuses a remote warehouse
    without an explicit `initial_world_fp` and the tree cannot vouch for a
    server (gateway/src/alkera_gateway/config.py:100-105).
    """
    wanted = seed.get("warehouses") or []
    if not isinstance(wanted, list):
        raise SystemExit("seed `warehouses:` must be a list of names")
    catalogue = theme.manifest.get("warehouses") or {}
    declared = []
    for name in wanted:
        if not WAREHOUSE_NAME.match(str(name)) or name in RESERVED_WAREHOUSE_NAMES:
            raise SystemExit(
                f"warehouse name {name!r} is not usable: must match "
                f"{WAREHOUSE_NAME.pattern} and not be one of {RESERVED_WAREHOUSE_NAMES}")
        spec = catalogue.get(name)
        if spec is None:
            raise SystemExit(
                f"seed names warehouse {name!r}, which theme {theme.name!r} does not "
                f"declare (declares: {', '.join(sorted(catalogue)) or 'none'})")
        if any(d["name"] == name for d in declared):
            raise SystemExit(f"seed names warehouse {name!r} twice")
        backend = spec.get("backend", "duckdb")
        hook_name = spec.get("hook", f"seed_{name}")
        hook = getattr(theme.gen, hook_name, None)
        if hook is None:
            raise SystemExit(
                f"theme {theme.name!r} declares warehouse {name!r} but its gen.py has no "
                f"`{hook_name}` hook to seed it")
        tables = hook(root, seed, theme.manifest, name)
        if not isinstance(tables, dict) or not tables:
            raise SystemExit(f"{hook_name} returned no tables for warehouse {name!r}")
        entry = {
            "name": name,
            "backend": backend,
            "read_only": bool(spec.get("read_only", False)),
            "attach": bool(spec.get("attach", True)),
            "tables": sorted(tables),
        }
        if spec.get("dbt"):
            # The dbt output the gateway's dbt_* tools select for this
            # warehouse (`--target`). For DuckDB it is the file stem, which
            # is the name, because that is what executor.write_profiles
            # renders; a Postgres target needs the dbt-postgres adapter, and
            # the gateway says so at boot if it is missing.
            if entry["read_only"]:
                # dbt opens the file directly and would write straight past
                # the engine-level read-only rule; a world that declared both
                # would be lying about one of them. The gateway refuses the
                # same pair at boot (warehouses.py, _peer_plan).
                raise SystemExit(
                    f"warehouse {name!r} is read-only and cannot also be a dbt target")
            entry["dbt_target"] = name
        if backend == "duckdb":
            path = root / "var" / "warehouse" / f"{name}.duckdb"
            if path.exists():
                path.unlink()
            warehouse_seed.seed_duckdb(path, tables)
            entry["path"] = path.relative_to(root).as_posix()
        elif backend == "postgres":
            dsn_env = spec.get("dsn_env", "ALKERA_POSTGRES_DSN")
            dsn = os.environ.get(dsn_env)
            if not dsn:
                raise SystemExit(
                    f"warehouse {name!r} is postgres and {dsn_env} is unset; the build "
                    "seeds the server directly and cannot proceed without it")
            warehouse_seed.seed_postgres(dsn, tables)
            entry["dsn_env"] = dsn_env
            entry["logical_hash"] = warehouse_seed.postgres_logical(dsn)["hash"]
        else:
            raise SystemExit(
                f"warehouse {name!r} is {backend!r}; the world builder seeds duckdb and "
                "postgres only (the Snowflake connector opens one session per statement "
                "and has no bulk path - warehouse_seed.py)")
        declared.append(entry)
    return declared


def declared_warehouses(root: Path) -> list[dict]:
    """What a built world declared, as `build/warehouses.json` recorded it.

    Empty for a world that declared nothing - there is no file. Paths come
    back ABSOLUTE under `root`, which is what a task's world builder publishes
    to the harness (`warehouses` in its manifest) and what the gateway config
    then carries; the record itself keeps them relative so the file is the
    same bytes wherever the tree is built.
    """
    root = Path(root)
    record = root / WAREHOUSES_RECORD
    if not record.is_file():
        return []
    out = []
    for entry in json.loads(record.read_text(encoding="utf-8")):
        entry = dict(entry)
        if entry.get("path"):
            entry["path"] = str(root / entry["path"])
        out.append(entry)
    return out


def gateway_warehouses(root: Path) -> tuple[list[dict], str | None]:
    """The `warehouses` list a gateway config takes, and the `initial_world_fp`
    it must carry when any of them is remote.

    Local (DuckDB) warehouses need no fingerprint of their own: they live in
    the tree, fingerprint.py's `warehouses` section covers them, and the
    gateway computes `initial_world_fp` from the tree at boot exactly as it
    does for one warehouse. A remote one is outside the tree, so the fp is
    composed here from the tree's semantic hash and each remote warehouse's
    logical hash - the shape `warehouse_seed.logical_hash` produces from a
    live server, and the same shape a grader can recompute after teardown.
    Returns `(specs, None)` when nothing is remote, so a builder that passes
    both through changes nothing for a local-only world.
    """
    from .fingerprint import semantic_hash

    specs, remote = [], {}
    for entry in declared_warehouses(root):
        spec = {k: entry[k] for k in ("name", "backend", "read_only", "attach")}
        for key in ("path", "dsn_env", "dbt_target"):
            if entry.get(key) is not None:
                spec[key] = entry[key]
        specs.append(spec)
        if entry.get("logical_hash"):
            remote[entry["name"]] = entry["logical_hash"]
    if not remote:
        return specs, None
    return specs, json_hash({"tree": semantic_hash(Path(root)), "remote": remote})


def build(seed_path: Path, root: Path, lock_path: Path | None = None) -> Path:
    seed = load_seed(seed_path)
    # Both opt-in keys are validated before anything is written, so a refused
    # seed leaves no half-built tree for a caller to mistake for a world.
    profile = resolve_profile(seed)
    formats = files.resolve(seed)
    if formats and profile is not None and "files" not in profile["slots"]:
        raise SystemExit(
            f"seed asks for `files:` but profile {profile['name']!r} does not plug "
            "the `files` slot")

    def plugged(slot: str) -> bool:
        # No profile plugs everything: the world every recorded hash came from.
        return profile is None or slot in profile["slots"]

    theme = load_theme(seed.get("theme", "revenue_daily"))
    # A profile that plugs a BI vendor needs a theme that can write its
    # project; refused here, with the other opt-in checks, before anything is
    # written. [] for every seed that does not plug a vendor.
    bi_vendors = bi_projects.require(theme, profile)
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
    # Additional warehouses (the source or the target of a migration), AFTER
    # the primary is built so a theme hook may read the primary if it wants
    # to, and BEFORE the scheduler state and the git commit so nothing here
    # can move a timestamp. Opt-in from the seed; a seed that names none
    # leaves the tree exactly as it was. The profiles file is re-rendered
    # afterwards because executor.write_profiles derives a dbt output from
    # every DuckDB file beside the primary, and the file did not exist when
    # the initial dbt build rendered it.
    extra = _seed_warehouses(root, seed, theme)
    if extra:
        _write(build_dir / WAREHOUSES_RECORD.name, json_canon(extra) + b"\n")
        executor.write_profiles(root, build_dir, theme.manifest["dbt_profile"])
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
    # From here down, every plane a profile can unplug is skipped outright when
    # it is: an unplugged slot leaves NO state behind - no file, no directory,
    # no fingerprint section - because state no tool can reach is a plane the
    # fingerprint hashes and the episode cannot see. The workspace, the
    # warehouse, the dbt build and the scheduler state (the clock) above are
    # always built; a profile selects planes, it does not change the data.
    write_ingest = getattr(theme.gen, "write_ingest", None)
    if write_ingest is not None and plugged("ingest"):
        write_ingest(root, seed, theme.manifest)
    # The workflow plane (gateway/SURFACES.md#workflow): the ticket the work
    # arrives in, the channel the context is scattered across, and the wiki
    # that is right in some places and out of date in others. Optional on the
    # same terms as write_ingest - a theme without the hook seeds nothing, gets
    # no var/workflow/, and keeps its recorded fingerprint. It runs AFTER
    # scheduler.save because the corpus is dated against the world clock, and
    # the hook reads `vnow` out of the state file rather than re-deriving it.
    write_workflow = getattr(theme.gen, "write_workflow", None)
    if write_workflow is not None and plugged("human"):
        write_workflow(root, seed, theme.manifest)
    # var/bi/dashboards/ itself is created above either way: fingerprint.py's
    # `bi` section is unconditional, and an empty directory is no dashboard.
    if plugged("bi"):
        for f in sorted((theme.dir / "bi").glob("*.yml")):
            shutil.copyfile(f, root / "var" / "bi" / "dashboards" / f.name)
    # The observability store (gateway/SURFACES.md). Definitions are world
    # state a theme seeds; the first sample is the reading the world held at
    # build time, which nothing can re-derive once advance has moved it on.
    # Unplugged, no definitions are seeded, and observe.record - here and on
    # every later advance/trigger - is a no-op without them (observe.py,
    # record), so the clock moving never resurrects var/obs.
    if plugged("observability"):
        _seed_observability(root, theme)
    observe.record(root, scheduler.load(root),
                   scheduler.parse_vtime(scheduler.load(root)["vnow"]), "build")
    # User files (the `files` slot; files.py). Derived from the raw tables the
    # build loaded above, and written BEFORE the git commit, because an export
    # somebody was handed is a file somebody checked in.
    if formats:
        files.write(root, seed, formats, scheduler.load(root)["vnow"])
    # The BI vendors' projects (bi_projects.py): the dashboards as code, in the
    # repository next to the dbt project. After the warehouse (each model's
    # sources are checked against it), BEFORE native discovery (the onboarding
    # page points at the project directories, which must exist to be named)
    # and BEFORE the git commit, because the vendor services publish what was
    # first committed. A seed whose profile plugs no vendor writes nothing.
    if bi_vendors:
        bi_projects.write(root, theme, seed, profile, db_path)
    # Native mode's discovery layer (gateway/NATIVE.md): .envrc, the
    # onboarding page, ~/.dbt/profiles.yml, ~/.config/alkera/services.toml.
    # Opt-in on the same terms as `faults:` and `warehouses:` - a seed without
    # `native: true` writes nothing, gets no `home/` directory, and keeps every
    # recorded fingerprint. It runs here because the onboarding page is
    # assembled from the workflow corpus and the dashboards, which exist by
    # now, and because the two files it writes into the workspace belong in the
    # git commit below: on a real box they are checked in.
    if seed.get("native"):
        native_workspace.write(root, theme,
                               slots=profile["slots"] if profile is not None else None,
                               plugs=profile.get("plugs") if profile is not None else None,
                               name=profile["name"] if profile is not None else None)
    # The profile record: the conditional `profile` fingerprint section, so
    # two profiles over one seed are two worlds. Written only under a profile,
    # so an unprofiled world has no file and keeps its recorded hash.
    if profile is not None:
        _write(root / PROFILE_RECORD, json_canon(profile) + b"\n")
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
    # Keys only a seed that opted in gets; an unflagged provenance keeps its bytes.
    if profile is not None:
        provenance["profile"] = profile
    if formats:
        provenance["files"] = formats
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
