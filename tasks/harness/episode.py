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
import sys
from pathlib import Path

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
def write_gateway_config(path: Path, world: dict, audit_dir: Path,
                         episode_id: str, virtual_time: str) -> Path:
    """The gateway config. Note what is NOT here: any path under the grading
    zone. The gateway physically cannot address it."""
    path.write_text(json.dumps({
        "workspace": world["workspace"],
        "warehouse": world["warehouse"],
        "scheduler": world["scheduler"],
        "dashboards": world["dashboards"],
        "audit_dir": str(audit_dir),
        "profiles_dir": world["profiles_dir"],
        "world_root": world["world_root"],
        "episode_id": episode_id,
        "initial_world_fp": None,
        "virtual_time": virtual_time or world.get("virtual_time")
                        or "1970-01-01T00:00:00Z",
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
def probe_surfaces(spec, world: dict, scratch: Path) -> dict:
    """Which surfaces are actually live in the world this task ships?

    Run against a THROWAWAY COPY with its own audit dir, before the episode, so
    it perturbs neither the world the agent gets nor its action log. This is how
    a failure gets attributed to a dead surface instead of counted as reasoning.
    """
    import socket
    import time

    if not spec.surface_probes:
        return {"probed": False, "surfaces": {}}

    copy_root = scratch / "probe_world"
    shutil.rmtree(copy_root, ignore_errors=True)
    shutil.copytree(world["world_root"], copy_root)
    probe_world = {k: (str(copy_root / Path(v).relative_to(world["world_root"]))
                       if k != "grading" and Path(v).is_relative_to(world["world_root"])
                       else v)
                   for k, v in world.items() if isinstance(v, str)}
    probe_world["world_root"] = str(copy_root)

    audit = scratch / "probe_audit"
    cfg = write_gateway_config(scratch / "probe_gateway.json", probe_world, audit,
                               "probe", spec.virtual_time)

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
        for probe in spec.surface_probes:
            label = probe.get("label") or probe["tool"]
            try:
                res = call_tool_http(url, probe["tool"], probe.get("args", {}), 60)
            except Exception as exc:                      # noqa: BLE001 - recorded
                surfaces[label] = {"tool": probe["tool"],
                                   "covers": list(probe.get("covers") or []),
                                   "live": False,
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
                "live": bool(res.get("ok")),
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
def grade(spec, world: dict, artifact_path: Path, out_dir: Path) -> dict:
    """Run the task's hidden grader. AFTER teardown. Never during the episode."""
    subs = {**{k: v for k, v in world.items() if isinstance(v, str)},
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


def grade_twice(spec, world: dict, artifact_path: Path, out_dir: Path) -> tuple[dict, bool]:
    """Grade, then grade again. A grader whose score moves on an unchanged world
    is a grader defect, and it is the one grader defect detectable mechanically."""
    first = grade(spec, world, artifact_path, out_dir)
    second = grade(spec, world, artifact_path, out_dir)
    stable = (first.get("total") == second.get("total")
              and [c.get("points") for c in first.get("checks", [])]
              == [c.get("points") for c in second.get("checks", [])])
    return first, stable
