"""Every backend the Harbor image serves, really built and really run (opt-in: Docker).

    ALKERA_DOCKER_TESTS=1 ALKERA_DOCKER_SG=1 TMPDIR=<disk> \\
        gateway/.venv/bin/python -m pytest -q harbor/tests/test_env_docker_backends.py \\
        --basetemp=<disk>
    (ALKERA_DOCKER_SG=1 when the shell is not in the docker group: every docker call
    then runs through `sg docker -c`; ALKERA_BACKEND_EVIDENCE=<dir> writes one JSON
    record per backend there.)

One test per backend, `test_backend_<name>_up_probe_down`. Each builds what the
exporter builds for a task on that backend's profile, from the exporter's own code:

  world      the tool tour's world for the backend's probes (commerce_analytics, seed
             11, built for the probe's profile: tool_tour.world_seed), staged by
             worldbuild.stage_native + stage_built_world (the export's steps 3-6:
             discovery kept, the box dbt profile, the gateway config with the
             profile, every path re-rooted);
  image      variant.render_dockerfile over the environment template, the context
             the adapter resolves (context.txt, the variant's locks and support
             files), episode.json from render.episode_document with the variant's
             `backends` and `statement_logs`;

then `docker build`, a container with no network, `world-up` as root (timed against
its 90 s budget; the backend's own start-up line from its log), and every probe of
tasks/harness/tool_tour_probes/<name>.py run as `engineer`, judged by the tour's own
`verdict`: the value equals truth_sql on the world as built (read before world-up),
every expected tool is in the rows after the probe began. Then:

  doors      as `engineer`: no TCP listener but the services' declared ports and the
             backends' (an upstream the agent could reach around its audited door
             fails here), plus each backend's own checks (its private state
             unreadable, its superuser unreachable);
  world-down the backends and services stop, a backend's destinations file is
             folded into destinations.jsonl, a statement log becomes rows (the
             `rows_at="teardown"` probes are judged now), seq stays contiguous.

BigQuery is refused at export by name (variant.REFUSED_WAREHOUSES): its emulator
listens on loopback TCP, which every process in a one-container image can reach.

Images are tagged alkera-harbor-test/backend:<name> and kept (the build cache is the
point); containers are always removed.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
HARNESS = REPO / "tasks" / "harness"
for path in (HERE, REPO / "harbor", HARNESS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from alkera_harbor import adapter, dockerenv, render, variant, worldbuild  # noqa: E402
from alkera_harbor.constants import TEMPLATE_DIR  # noqa: E402
from alkera_harbor.naming import ArmRef  # noqa: E402

TAG = "alkera-harbor-test/backend"
ENV_TEMPLATE = TEMPLATE_DIR / "environment"
#: world-up's own budget (alkera_runtime.DEFAULT_UP_TIMEOUT_S).
UP_BUDGET_S = 90.0
PROBE_TIMEOUT_S = 600
#: The image's interpreter by its path: `python3` on PATH is the audited shim, and
#: the test's own reads must not be rows of the agent's.
PY = "/usr/local/bin/python3"


def _sg() -> bool:
    return os.environ.get("ALKERA_DOCKER_SG") == "1"


def docker(*args: str, timeout: float = 3600, check: bool = True,
           stdin: str | None = None) -> subprocess.CompletedProcess:
    argv = ["docker", *args]
    if _sg():
        argv = ["sg", "docker", "-c", shlex.join(argv)]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          input=stdin)
    if check and done.returncode != 0:
        raise AssertionError(f"docker {' '.join(args[:3])} failed ({done.returncode}):\n"
                             f"{done.stdout[-3000:]}\n{done.stderr[-3000:]}")
    return done


def _docker_ready() -> bool:
    try:
        return docker("info", timeout=60, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_docker = pytest.mark.skipif(
    os.environ.get("ALKERA_DOCKER_TESTS") != "1" or not _docker_ready(),
    reason="set ALKERA_DOCKER_TESTS=1 with a working docker to build the images")


# ------------------------------------------------------------------ the episode
def probes_for(*tools: str) -> list:
    import tool_tour

    return tool_tour.load_probes(list(tools))


def stage(name: str, probes: list, base: Path) -> dict:
    """The world, image context and episode.json an exported task on these probes'
    profile would get, built under `base`."""
    import bi_e2e_check as bi
    import tool_tour

    from alkera_harbor import harness

    m = harness.modules()
    first = probes[0]
    profile, plugs = first.profile, {s: list(v) for s, v in first.plug_map().items()}
    v = variant.variant_of(SimpleNamespace(profile=profile, plugs=plugs))
    rollout = base / "rollout"
    seed = tool_tour.world_seed(profile, first.plug_map())
    started = time.monotonic()
    with tool_tour.as_profile(profile, first.plug_map(), seed):
        world = bi.build_world(rollout)
    build_s = round(time.monotonic() - started, 1)
    # the truths BEFORE anything runs, on the world as built (the tour's rule)
    truths = tool_tour.world_truths(probes, world["world_root"])
    clock = m.container_episode.world_clock(world)
    records: dict = {}
    worldbuild.stage_native(world, v, label=name, records=records)
    staged = worldbuild.stage_built_world(world, rollout, variant=v, profile=profile,
                                          plugs=plugs, label=name, records=records,
                                          temp=base)

    context = base / "environment"
    adapter.copy_template(ENV_TEMPLATE, context)
    template = (ENV_TEMPLATE / "Dockerfile").read_text(encoding="utf-8")
    dockerfile = variant.render_dockerfile(template, v, dockerenv.parse_env(template))
    (context / "Dockerfile").write_text(dockerfile, encoding="utf-8", newline="")
    exporter = adapter.AlkeraHarborAdapter(output_dir=base / "unused", log=lambda *a: None)
    exporter._environment_context(v, f"tooltour-{name}", context / "alkera")
    episode = context / "episode"
    episode.mkdir()
    shutil.move(str(staged["world_dir"]), str(episode / "world"))
    shutil.copytree(staged["workspace_dir"], episode / "workspace", symlinks=True)
    shutil.move(str(staged["home_dir"]), str(episode / "home"))
    shutil.copyfile(staged["gateway_config"], episode / "gateway.json")
    env = {"ALKERA_TASK_VARIANT": "default", "ALKERA_SHIM_WATCH": staged["shim_watch"]}
    if clock:
        env["ALKERA_VIRTUAL_TIME"] = clock
    spec = SimpleNamespace(seed=11, virtual_time=clock or "", profile=profile, plugs=plugs,
                           artifact_in_world="/workspace/answer.json",
                           wall_clock_seconds=1800, max_tool_calls=200)
    ref = ArmRef(f"TOOLTOUR-{name}", "default")
    document = render.episode_document(
        ref=ref, spec=spec, role="control", reward_mode="pass", world=staged["world"],
        env=env, source_commit="test", source_dirty=True, task_toml_sha256="0" * 64,
        anchors="missing", oracle="missing", restore=staged["restore"],
        gateway_config=worldbuild.GATEWAY_CONFIG_IN_CONTAINER,
        backends=variant.backend_specs(v), statement_logs=variant.statement_logs(v))
    (episode / "episode.json").write_text(render.episode_json(document), encoding="utf-8",
                                          newline="")
    return {"variant": v, "context": context, "document": document, "truths": truths,
            "world_build_s": build_s, "dockerfile": dockerfile, "env": env}


# --------------------------------------------------------------- in the container
_SNAPSHOT = r"""
import glob, json, os
rows = [json.loads(l) for l in open('/audit/actions.jsonl') if l.strip()]
dest = {}
for path in sorted(glob.glob('/audit/destinations*.jsonl')):
    dest[os.path.basename(path)] = [json.loads(l) for l in open(path) if l.strip()]
try:
    vnow = json.load(open('/world/var/sched/state.json')).get('vnow')
except Exception:
    vnow = None
print(json.dumps({'rows': rows, 'dest': dest, 'vnow': vnow}))
"""

#: As `engineer`: every TCP port in LISTEN, from /proc (no connection).
_LISTENERS = r"""
import json
ports = set()
for table in ('/proc/net/tcp', '/proc/net/tcp6'):
    for line in open(table).read().splitlines()[1:]:
        f = line.split()
        if f[3] == '0A':
            ports.add(int(f[1].rsplit(':', 1)[1], 16))
print(json.dumps(sorted(ports)))
"""


#: Every live process but pid 1 (the image's `sleep infinity`) and this reader.
_PROCESSES = r"""
import json, os
out = []
for pid in os.listdir('/proc'):
    if not pid.isdigit() or int(pid) in (1, os.getpid()):
        continue
    try:
        state = open(f'/proc/{pid}/stat').read().rsplit(')', 1)[1].split()[0]
        argv = open(f'/proc/{pid}/cmdline', 'rb').read().split(b'\0')
    except OSError:
        continue
    if state not in ('Z', 'X'):
        out.append(' '.join(a.decode('utf-8', 'replace') for a in argv if a))
print(json.dumps(out))
"""


def snapshot(name: str) -> dict:
    out = docker("exec", "-i", name, PY, "-", stdin=_SNAPSHOT, timeout=120)
    return json.loads(out.stdout.strip().splitlines()[-1])


def destination_counts(dest: dict) -> dict[tuple[str, int], int]:
    """(host, port) -> count, summed over files, a row folded verbatim into
    destinations.jsonl counted once (tool_tour.read_destinations' rule)."""
    rows: dict = {}
    seen: dict = {}
    for _file, file_rows in sorted(dest.items()):
        for row in file_rows:
            key = (row.get("host"), int(row.get("port") or 0))
            if row in seen.setdefault(key, []):
                continue
            seen[key].append(row)
            rows[key] = rows.get(key, 0) + int(row.get("count") or 0)
    return rows


def delta(before: dict, after: dict) -> dict[tuple[str, int], int]:
    a, b = destination_counts(after), destination_counts(before)
    return {k: n - b.get(k, 0) for k, n in a.items() if n - b.get(k, 0) > 0}


def as_engineer(name: str, command: str, timeout: float = PROBE_TIMEOUT_S):
    return docker("exec", "-u", "engineer", "-w", "/workspace", name,
                  "timeout", "-k", "10", str(int(timeout)), "bash", "-c", command,
                  check=False, timeout=timeout + 60)


def judge(probe, start: dict, end: dict, outcome, truths: dict, allowed: set) -> dict:
    import bi_e2e_check as bi
    import tool_tour

    rows = end["rows"][len(start["rows"]):]
    clocks = tuple(c for c in dict.fromkeys((start["vnow"], end["vnow"])) if c)
    fact, error = truths.get(probe.id, (None, "no truth"))
    result = tool_tour.verdict(
        probe, returncode=outcome.returncode, stdout=outcome.stdout, rows=rows,
        destinations=delta(start["dest"], end["dest"]), allowed=allowed, truth=fact,
        truth_error=error, validate_row=lambda r: bi.validate_row(r, vnow=clocks or None))
    result["stderr_tail"] = outcome.stderr[-1500:]
    return result


def contiguous(rows: list[dict]) -> bool:
    return [r.get("seq") for r in rows] == list(range(1, len(rows) + 1))


def backend_log_tail(name: str, backend: str) -> str:
    return docker("exec", name, "sh", "-c",
                  f"tail -n 40 /var/log/alkera/backend-{backend}.log 2>/dev/null || true",
                  check=False).stdout


# ------------------------------------------------------------------- the driver
def up_probe_down(name: str, tools: tuple[str, ...], tmp_path: Path,
                  engineer_checks: str = "") -> dict:
    probes = probes_for(*tools)
    staged = stage(name, probes, tmp_path)
    tag = f"{TAG}:{name}"
    started = time.monotonic()
    docker("build", "-q", "-t", tag, str(staged["context"]), timeout=3600)
    record: dict = {"backend": name, "profile": probes[0].profile,
                    "plugs": {s: list(v) for s, v in probes[0].plug_map().items()},
                    "variant": staged["variant"].key, "image": tag,
                    "world_build_s": staged["world_build_s"],
                    "image_build_s": round(time.monotonic() - started, 1),
                    "backends": staged["document"].get("backends"),
                    "statement_logs": staged["document"].get("statement_logs")}
    container = f"alkera-backend-test-{name}-{os.getpid()}"
    env_flags = [f for k, v in staged["env"].items() for f in ("-e", f"{k}={v}")]
    try:
        docker("run", "-d", "--name", container, "--network", "none", *env_flags, tag)
        began = time.monotonic()
        up = docker("exec", container, "/opt/alkera/bin/world-up", check=False, timeout=300)
        record["world_up_s"] = round(time.monotonic() - began, 1)
        record["world_up"] = up.stdout.splitlines()
        assert up.returncode == 0, (up.stderr + up.stdout
                                    + "".join(backend_log_tail(container, b["name"])
                                              for b in staged["document"].get("backends")
                                              or []))
        assert record["world_up_s"] < UP_BUDGET_S, record["world_up_s"]
        record["backend_logs"] = {b["name"]: backend_log_tail(container, b["name"])
                                  for b in staged["document"].get("backends") or []}
        again = docker("exec", container, "/opt/alkera/bin/world-up", check=False)
        assert again.returncode == 0 and "re-check" in again.stdout, again.stderr

        mark = json.loads(docker("exec", container, "cat",
                                 "/audit/.episode_start.json").stdout)
        declared = {tuple(k.rsplit(":", 1)) for k in mark["destinations_baseline"]}
        allowed = {(h, int(p)) for h, p in declared} | {
            (h, p) for b in staged["document"].get("backends") or []
            for h in b["hosts"] for p in b["ports"]}
        # every door the agent has is one the audit sees
        listening = set(json.loads(docker("exec", "-i", "-u", "engineer", container,
                                          PY, "-", stdin=_LISTENERS).stdout))
        expected = {p for _, p in allowed}
        record["listening"] = sorted(listening)
        assert listening <= expected, (f"listeners the audit does not see: "
                                       f"{sorted(listening - expected)}")
        if engineer_checks:
            checks = as_engineer(container, engineer_checks, timeout=300)
            lines = [ln for ln in checks.stdout.splitlines()
                     if ln.startswith(("PASS", "FAIL"))]
            record["engineer_checks"] = lines
            assert lines and not [ln for ln in lines if ln.startswith("FAIL")], \
                "\n".join(lines) + checks.stderr[-2000:]

        results, pending = [], []
        for probe in probes:
            start = snapshot(container)
            t0 = time.monotonic()
            outcome = as_engineer(container, probe.command)
            seconds = round(time.monotonic() - t0, 1)
            if probe.rows_at == "teardown":
                pending.append((probe, start, outcome, seconds))
                continue
            result = judge(probe, start, snapshot(container), outcome,
                           staged["truths"], allowed)
            result["seconds"] = seconds
            results.append(result)

        before_down = snapshot(container)
        down = docker("exec", container, "/opt/alkera/bin/world-down", check=False,
                      timeout=300)
        record["world_down"] = down.stdout.splitlines()
        assert down.returncode == 0, down.stderr
        end = snapshot(container)
        for probe, start, outcome, seconds in pending:
            result = judge(probe, start, end, outcome, staged["truths"], allowed)
            result["seconds"] = seconds
            results.append(result)
        record["probes"] = [{k: r.get(k) for k in ("id", "verdict", "failures", "value",
                                                   "truth", "rows", "tools", "seconds",
                                                   "destinations")}
                            for r in results]
        record["rows_at_teardown"] = len(end["rows"]) - len(before_down["rows"])
        record["destinations_folded"] = end["dest"].get("destinations.jsonl")
        assert contiguous(end["rows"]), "seq is not contiguous after world-down"
        failed = [r for r in results if r["verdict"] != "PASS"]
        assert not failed, json.dumps(failed, indent=1, default=str)[-6000:]
        left = json.loads(docker("exec", "-i", container, PY, "-",
                                 stdin=_PROCESSES).stdout)
        record["left_running"] = left
        assert not left, left
        return record
    finally:
        out = os.environ.get("ALKERA_BACKEND_EVIDENCE")
        if out:
            Path(out).mkdir(parents=True, exist_ok=True)
            (Path(out) / f"{name}.json").write_text(
                json.dumps(record, indent=1, default=str) + "\n", encoding="utf-8")
        docker("rm", "-f", container, check=False, timeout=120)


def folded_backend_row(record: dict, host: str, port: int) -> dict:
    rows = record["destinations_folded"] or []
    found = [r for r in rows if r.get("host") == host and r.get("port") == port]
    assert found and found[0]["count"] >= 1, rows
    return found[0]

