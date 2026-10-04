"""Episode plumbing: build the world, stand the gateway up, tear it down, grade.

Task-agnostic. The only task-specific knowledge is read out of TaskSpec, which
read it out of the task's own task.toml.

Ground rule 1 is structural here, not a convention: the grading zone is built
into a directory that is a SIBLING of the world root, the gateway config never
names it, and the grader is not invoked until the episode process is dead.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import bistate
import ledger as ledgerlib

REPO = Path(__file__).resolve().parents[2]
GATEWAY = REPO / "gateway"
TRACK_A = REPO / "world"

PY = GATEWAY / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = GATEWAY / ".venv" / "bin" / "python"


class HarnessError(Exception):
    """Our bug, not the agent's. Classified as harness_error, never scored."""


def venv_env(extra: dict | None = None) -> dict:
    env = {**os.environ, "PYTHONPATH": str(TRACK_A), "PYTHONUTF8": "1"}
    env.update(extra or {})
    return env


# ----------------------------------------------------------------- world
def prepare_task(spec) -> None:
    """One-time, before any rollout forks. Builds whatever the task caches.

    K rollouts racing to generate the same shared fixture is how a stale
    generator once silently overwrote a validated reference.
    """
    r = subprocess.run([str(PY), str(spec.world_builder), "--prepare-only",
                        "--seed", str(spec.seed)],
                       capture_output=True, text=True, env=venv_env())
    if r.returncode != 0:
        raise HarnessError(f"world builder --prepare-only failed:\n{r.stdout}\n{r.stderr}")


def build_world(spec, world_root: Path, grading: Path) -> dict:
    r = subprocess.run(
        [str(PY), str(spec.world_builder), "--root", str(world_root),
         "--grading", str(grading), "--seed", str(spec.seed)],
        capture_output=True, text=True, env=venv_env())
    if r.returncode != 0:
        raise HarnessError(f"world builder failed:\n{r.stdout}\n{r.stderr}")
    try:
        world = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise HarnessError(f"world builder printed no json manifest: {r.stdout!r}") from exc
    missing = {"world_root", "workspace", "warehouse", "scheduler", "dashboards",
               "profiles_dir", "grading"} - set(world)
    if missing:
        raise HarnessError(f"world manifest missing keys: {sorted(missing)}")
    if Path(world["grading"]).resolve().is_relative_to(Path(world["world_root"]).resolve()):
        raise HarnessError("grading zone is inside the world root - ground rule 1")
    return world


def fingerprint(world_root: Path) -> str | None:
    """Track A's semantic fingerprint. None if it cannot be computed (recorded,
    never silently treated as 'unchanged')."""
    r = subprocess.run([str(PY), "-m", "world.fingerprint", str(world_root)],
                       capture_output=True, text=True, cwd=str(TRACK_A), env=venv_env())
    return r.stdout.strip() or None


def tree_manifest(root: Path) -> dict:
    """path -> (size, sha256) for every file under the world, .git excluded.

    Used only to detect world changes the action log cannot account for, i.e.
    something touched the world outside the gateway.
    """
    out = {}
    root = Path(root)
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(root).as_posix()
        if rel.startswith(".git/") or "/.git/" in rel:
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        out[rel] = [len(data), hashlib.sha256(data).hexdigest()]
    return out


# ----------------------------------------------------------------- gateway
# Planes whose GatewayConfig default is an ABSOLUTE path outside any world root
# (gateway/src/alkera_gateway/config.py:19-23,31: `/var/ingest/state.json`,
# `/var/obs/observability.yml`, `/var/workflow`, all `.resolve()`d). A config
# that does not name them leaves the gateway looking somewhere the world is not,
# and issues_*/chat_*/kb_*/metrics_*/alarms_*/deploy_*/ingest_* answer
# `no_issue_tracker` / `no_observability` / `no_ingest_state` NO MATTER WHAT THE
# WORLD SEEDS. That is what made GT004's five Opus rollouts a measurement of a
# half-absent world: the content was there and the harness never said where.
#
# Optional on purpose: a task whose world builder does not publish these keys is
# unaffected, so a world that genuinely has no workflow plane still reports one
# absent rather than being handed a path to nothing.
WORLD_PLANE_KEYS = ("workflow_dir", "observability", "obs_samples", "ingest")

# The same pattern for a SECOND warehouse (gateway/SURFACES.md#dual-warehouse).
# A world builder that runs one publishes `warehouses` - the list GatewayConfig
# takes, each entry a WarehouseSpec with an ABSOLUTE `path` for a DuckDB file
# (world.build.gateway_warehouses produces exactly this) - and may publish
# `warehouse_name` for the primary. `initial_world_fp` is published only when a
# declared warehouse is remote: the gateway refuses a remote warehouse without
# one, because a hash of the local tree cannot vouch for a server, and the
# builder is the one party that seeded it and can hash it. A builder that
# publishes none of these changes nothing: the config it gets is the one it
# always got, `initial_world_fp: null` included, which the gateway resolves to
# the tree's semantic hash at boot exactly as before.
WORLD_WAREHOUSE_KEYS = ("warehouse_name", "warehouses", "initial_world_fp")


# Where each plane lives on the 9.2.1 layout, for a builder that seeds a plane
# without publishing its key (GT002's builder seeds `var/workflow` on its
# decision arm and publishes no `workflow_dir`). Only a plane that EXISTS on
# disk is filled in, so a world that genuinely has none still reports it
# absent, exactly as above. The same paths `serve.world_gateway_config` uses,
# which is what the container topology always ran on.
WORLD_PLANE_LAYOUT = {"workflow_dir": "var/workflow",
                      "observability": "var/obs/observability.yml",
                      "ingest": "var/ingest/state.json"}


def _plane_paths(world: dict) -> dict:
    out = {k: world[k] for k in WORLD_PLANE_KEYS if world.get(k)}
    root = world.get("world_root")
    for key, rel in WORLD_PLANE_LAYOUT.items():
        if key not in out and root and (Path(root) / rel).exists():
            out[key] = str(Path(root) / rel)
    return out


def _warehouse_keys(world: dict) -> dict:
    return {k: world[k] for k in WORLD_WAREHOUSE_KEYS if world.get(k)}


def reroot_warehouses(specs, old_root, new_root) -> list:
    """The `warehouses` list with every DuckDB `path` under `old_root` moved
    under `new_root`, POSIX-joined. Nested inside a list, so the top-level
    string re-rooting the probe copy and the container mount already do
    cannot see it; a spec whose path is not under the world root (or a remote
    spec, which has none) is passed through unchanged. Used for the probe's
    throwaway copy on the host and for the /world mount in the container."""
    old = Path(old_root).resolve()
    out = []
    for spec in specs or []:
        spec = dict(spec)
        path = spec.get("path")
        if path:
            try:
                rel = Path(path).resolve().relative_to(old)
            except (ValueError, OSError):
                rel = None
            if rel is not None:
                spec["path"] = f"{str(new_root).rstrip('/')}/{rel.as_posix()}"
        out.append(spec)
    return out


# ------------------------------------------------------ environment profile
def environment_profile(profile: str = "", plugs: dict | None = None):
    """The task's `alkera_gateway.plugs.Profile`, or None when it declares none.

    Resolved WITHOUT `allow_planned`: the task loader accepts a profile authored
    ahead of its tools, and this is where the harness refuses to run one -
    before a world is built or a container started, with the catalog's own text.
    """
    if not profile:
        if plugs:
            raise HarnessError("[environment.plugs] adjusts a profile, but no "
                               "profile is set")
        return None
    from alkera_gateway.plugs import ProfileError, resolve

    try:
        return resolve(profile, plugs or {})
    except ProfileError as exc:
        raise HarnessError(str(exc)) from exc


#: Where `world.build` records the seed's `profile: {name, slots, plugs?}`
#: (world/README.md).
WORLD_PROFILE_RECORD = Path("var") / "world" / "profile.json"


def _bi_vendor(plugs) -> bool:
    """Does this `bi` plug list name an explicit-only BI vendor (a plug that
    mounts one of `alkera_gateway.native.services.PLUG_NAMES`)?"""
    from alkera_gateway.native.services import PLUG_NAMES
    from alkera_gateway.plugs import PLUGS

    return any(set(PLUGS[name].services) & set(PLUG_NAMES) for name in plugs)


def check_world_profile(world_root: Path, profile) -> None:
    """The world the task built must be the box its profile describes.

    A task's world comes from its own `build_world.py` and seed, written by the
    task author; the tools the agent is served come from `task.toml`. If the two
    disagree the agent is handed tools over planes that were never seeded (or
    the reverse), so the rollout stops here, in either direction. A world with
    no record and a task with no profile is today's case and passes untouched.

    Slots are not enough for BI: `bi` is Metabase on one box and Power BI on
    another, and a world's BI project (`<workspace>/bi/powerbi/`) and its
    discovery files are built for one vendor. So when the task plugs a BI
    vendor (Power BI, Tableau, Sigma), or the world's record says which BI it
    was built for (`plugs.bi`), the two lists must be the same plugs. A task
    on Metabase over a record with no `plugs` - every world built before the
    key existed - is not compared, and passes as it always did.
    """
    path = Path(world_root) / WORLD_PROFILE_RECORD
    record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    task_bi = sorted(profile.slots.get("bi", ())) if profile is not None else []
    vendor = _bi_vendor(task_bi)
    if profile is None:
        if record is not None:
            raise HarnessError(
                f"the world was built for profile {record.get('name')!r} "
                f"(slots {record.get('slots')}), but the task declares no profile")
        return
    if record is None:
        needs = f", plugs: {{bi: {task_bi}}}" if vendor else ""
        raise HarnessError(
            f"the task declares profile {profile.name!r}, but its world was built "
            f"without one: the seed needs `profile: {{name: {profile.name!r}, "
            f"slots: {sorted(profile.slots)}{needs}}}`")
    if record.get("name") != profile.name:
        raise HarnessError(
            f"the world was built for profile {record.get('name')!r}, the task "
            f"declares {profile.name!r}")
    if sorted(record.get("slots") or []) != sorted(profile.slots):
        raise HarnessError(
            f"the world's slots {sorted(record.get('slots') or [])} are not the "
            f"slots profile {profile.name!r} plugs with its [environment.plugs]: "
            f"{sorted(profile.slots)}")
    world_bi = (record.get("plugs") or {}).get("bi")
    if (vendor or world_bi is not None) and sorted(world_bi or []) != task_bi:
        built = (f"for BI {sorted(world_bi)}" if world_bi is not None
                 else "without saying which BI vendor")
        raise HarnessError(
            f"the world was built {built}, but profile {profile.name!r} serves "
            f"BI {task_bi}: the seed needs `profile: {{..., plugs: {{bi: {task_bi}}}}}`")


def profile_keys(profile: str = "", plugs: dict | None = None) -> dict:
    """`{"profile", "plugs"}` for a gateway config, or `{}` with no profile, so
    an unprofiled config is byte for byte what it was before profiles."""
    if not profile:
        return {}
    return {"profile": profile,
            "plugs": {slot: list(names) for slot, names in (plugs or {}).items()}}


def spec_profile_keys(spec) -> dict:
    return profile_keys(getattr(spec, "profile", ""), getattr(spec, "plugs", None))


def profile_argv(profile: str = "", plugs: dict | None = None) -> list[str]:
    """`--profile ROLE/STACK --plug SLOT=PLUG[,PLUG]...` for `native.serve`."""
    if not profile:
        return []
    argv = ["--profile", profile]
    for slot, names in (plugs or {}).items():
        argv += ["--plug", f"{slot}={','.join(names)}"]
    return argv


def profile_toolbelt(toolbelt: dict, profile=None) -> dict:
    """The toolbelt a profiled gateway serves: `gateway_tool_names()` and the
    container shim's `tools` read the whole `TOOLS` table, and `build_tools`
    drops the unplugged slots' tools from what the config actually serves."""
    if profile is None:
        return toolbelt
    return {name: d for name, d in toolbelt.items()
            if name not in profile.excluded_tools}


def write_gateway_config(path: Path, world: dict, audit_dir: Path,
                         episode_id: str, virtual_time: str, *,
                         profile: str = "", plugs: dict | None = None) -> Path:
    """The gateway config. Note what is NOT here: any path under the grading
    zone. The gateway physically cannot address it."""
    path.write_text(json.dumps({
        "workspace": world["workspace"],
        "warehouse": world["warehouse"],
        "scheduler": world["scheduler"],
        "dashboards": world["dashboards"],
        **_plane_paths(world),
        "audit_dir": str(audit_dir),
        "profiles_dir": world["profiles_dir"],
        "world_root": world["world_root"],
        "episode_id": episode_id,
        "initial_world_fp": None,
        "virtual_time": virtual_time or world.get("virtual_time")
                        or "1970-01-01T00:00:00Z",
        # after the literal null above on purpose: a world that publishes a
        # fingerprint overrides it, a world that does not keeps it
        **_warehouse_keys(world),
        # last, and only when the task names one (see `profile_keys`)
        **profile_keys(profile, plugs),
    }, indent=2), encoding="utf-8")
    return path


def gateway_tool_names() -> list[str]:
    r = subprocess.run(
        [str(PY), "-c",
         "import json;from alkera_gateway.tools import TOOLS;"
         "print(json.dumps({k: v[1] for k, v in TOOLS.items()}))"],
        capture_output=True, text=True, env=venv_env())
    if r.returncode != 0:
        raise HarnessError(f"cannot read the gateway toolbelt:\n{r.stderr}")
    return json.loads(r.stdout)


def call_tool_http(url: str, tool: str, args: dict, timeout: int = 180) -> dict:
    import urllib.request
    req = urllib.request.Request(
        url.rstrip("/") + "/call",
        data=json.dumps({"tool": tool, "args": args}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


# ----------------------------------------------------------------- probes
def probe_surfaces(spec, world: dict, scratch: Path,
                   probes: list[dict] | None = None) -> dict:
    """Which surfaces are actually live in the world this task ships?

    Run against a THROWAWAY COPY with its own audit dir, before the episode, so
    it perturbs neither the world the agent gets nor its action log. This is how
    a failure gets attributed to a dead surface instead of counted as reasoning.

    `probes` defaults to the task's own declarations. The caller passes a longer
    list - the task's probes plus surface_audit.sweep_probes over the rest of
    the toolbelt - so that a dead surface cannot hide behind a tool the task
    never thought to declare.
    """
    import socket
    import time

    probes = spec.surface_probes if probes is None else probes
    if not probes:
        return {"probed": False, "surfaces": {}}

    copy_root = scratch / "probe_world"
    shutil.rmtree(copy_root, ignore_errors=True)
    shutil.copytree(world["world_root"], copy_root)
    probe_world = {k: (str(copy_root / Path(v).relative_to(world["world_root"]))
                       if k != "grading" and Path(v).is_relative_to(world["world_root"])
                       else v)
                   for k, v in world.items() if isinstance(v, str)}
    probe_world["world_root"] = str(copy_root)
    # A second DuckDB warehouse lives in the tree that was just copied, so the
    # probe must open the copy's file and not the episode's. A remote one has
    # no path and is probed where it is - it is the same server either way.
    if world.get("warehouses"):
        probe_world["warehouses"] = reroot_warehouses(
            world["warehouses"], world["world_root"], copy_root.as_posix())

    audit = scratch / "probe_audit"
    cfg = write_gateway_config(scratch / "probe_gateway.json", probe_world, audit,
                               "probe", spec.virtual_time,
                               **spec_profile_keys(spec))

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    log = (scratch / "probe_server.log").open("w")
    proc = subprocess.Popen(
        [str(PY), "-m", "alkera_gateway.cli", "serve", "--config", str(cfg),
         "--port", str(port)], stdout=log, stderr=log, env=venv_env())
    surfaces = {}
    try:
        for _ in range(60):
            time.sleep(0.5)
            try:
                socket.create_connection(("127.0.0.1", port), 0.5).close()
                break
            except OSError:
                if proc.poll() is not None:
                    raise HarnessError("probe gateway died; see probe_server.log")
        url = f"http://127.0.0.1:{port}"
        for probe in probes:
            label = probe.get("label") or probe["tool"]
            declared = bool(probe.get("declared", True))
            try:
                res = call_tool_http(url, probe["tool"], probe.get("args", {}), 60)
            except Exception as exc:                      # noqa: BLE001 - recorded
                surfaces[label] = {"tool": probe["tool"],
                                   "covers": list(probe.get("covers") or []),
                                   "declared": declared,
                                   "live": False,
                                   "error_code": "probe_transport_failure",
                                   "error": f"{type(exc).__name__}: {exc}"}
                continue
            err = res.get("error")
            # the result summary matters as much as live/dead: a tool that
            # answers OK with nothing in it (docs_search over an empty tree) is
            # a world gap too, and "live=True" alone would hide it
            surfaces[label] = {
                "tool": probe["tool"],
                # a probe may speak for its siblings: dbt_compile standing in
                # for dbt_run and dbt_test, which cannot be probed read-only
                "covers": list(probe.get("covers") or []),
                # whether the TASK declared this probe or the harness swept it
                "declared": declared,
                "live": bool(res.get("ok")),
                # the code, separately: `no_chat` means the world has no chat,
                # `invalid_arguments` means the probe could not even be formed,
                # and conflating the two is how a dead surface reads as a bad
                # probe (or, worse, the other way round)
                "error_code": None if res.get("ok") else (err or {}).get("code"),
                "error": None if res.get("ok") else json.dumps(err)[:300],
                "result_summary": json.dumps(res.get("result"))[:300],
            }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(copy_root, ignore_errors=True)
    return {"probed": True, "surfaces": surfaces}


# ----------------------------------------------------------------- grading
def grade(spec, world: dict, artifact_path: Path, out_dir: Path,
          bi_state_dir: Path | None = None) -> dict:
    """Run the task's hidden grader. AFTER teardown. Never during the episode.

    `bi_state_dir` is where this episode's BI vendor services kept their
    published state; None means the harness default, `<out_dir>/audit/bi`
    (bistate.py says why that is the same path on every topology)."""
    # The publication ledger moves into the grading zone first
    # (world/README.md#publication-ledger). The grader reads that copy and not
    # <world_root>/var/ledger, for the same reason it reads no other evidence
    # out of the tree the subject just worked in. A world that published
    # nothing stages nothing and `{ledger}` substitutes to the empty string;
    # a grader must handle a world with no publications anyway.
    staged = ledgerlib.stage(world["world_root"], world["grading"])
    # The BI vendors' published state, on the same terms (bistate.py): staged
    # out of the services' state directory, which lives outside the world
    # tree, into the grading zone; `{bi_state}` is that copy or "".
    staged_bi = bistate.stage(bi_state_dir if bi_state_dir is not None
                              else bistate.source_for(out_dir), world["grading"])
    subs = {**{k: v for k, v in world.items() if isinstance(v, str)},
            "ledger": str(staged) if staged else "",
            "bi_state": str(staged_bi) if staged_bi else "",
            "artifact": str(artifact_path)}
    argv = [a.format(**subs) for a in spec.grade_argv]
    r = subprocess.run([str(PY), str(spec.grade_entrypoint), *argv],
                       capture_output=True, text=True, env=venv_env())
    (out_dir / "grader.stderr.txt").write_text(r.stderr or "", encoding="utf-8")
    if r.returncode != 0:
        raise HarnessError(f"grader exited {r.returncode}:\n{r.stderr[-2000:]}")
    try:
        score = json.loads(r.stdout)
    except ValueError as exc:
        raise HarnessError(f"grader printed non-json:\n{r.stdout[-2000:]}") from exc
    score["_stderr"] = (r.stderr or "").strip()
    return score


def grade_twice(spec, world: dict, artifact_path: Path, out_dir: Path,
                bi_state_dir: Path | None = None) -> tuple[dict, bool]:
    """Grade, then grade again. A grader whose score moves on an unchanged world
    is a grader defect, and it is the one grader defect detectable mechanically."""
    first = grade(spec, world, artifact_path, out_dir, bi_state_dir)
    second = grade(spec, world, artifact_path, out_dir, bi_state_dir)
    stable = (first.get("total") == second.get("total")
              and [c.get("points") for c in first.get("checks", [])]
              == [c.get("points") for c in second.get("checks", [])])
    return first, stable
