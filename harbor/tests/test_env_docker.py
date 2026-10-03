"""The agent image, really built and really run (opt-in: needs Docker).

    ALKERA_DOCKER_TESTS=1 gateway/.venv/bin/python -m pytest -q harbor/tests/test_env_docker.py

For GT001 and GT008, each arm: assemble a build context the way the generator
does (the template copied verbatim, context.txt resolved into alkera/, the
episode payload under episode/ - world, workspace, home, episode.json and the
manifest-derived gateway.json - built here, by test_env_runtime's fixture),
`docker build` it, run it with no network, start the world as root with
`/opt/alkera/bin/world-up` (real /etc/hosts, port offset 0), check from a
shell as `engineer` that the services answer on *.internal and that the
permission model holds, check the action log, then `world-down`. For GT008,
also that the second warehouse (`legacy`) is attached THROUGH the services.

Images are tagged alkera-harbor-test/env:<arm> and kept (the build cache is
the point); containers are always removed. ALKERA_DOCKER overrides the docker
command (e.g. "sudo docker").
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

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(REPO / "harbor") not in sys.path:
    sys.path.insert(0, str(REPO / "harbor"))

import test_env_dockerfile as static  # noqa: E402
import test_env_runtime as fixture  # noqa: E402

DOCKER = shlex.split(os.environ.get("ALKERA_DOCKER", "docker"))
TAG = "alkera-harbor-test/env"


def docker(*args, timeout=1800, check=True, stdin=None):
    result = subprocess.run([*DOCKER, *args], capture_output=True, text=True,
                            timeout=timeout, input=stdin)
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args[:3])} failed ({result.returncode}):\n"
                             f"{result.stdout[-3000:]}\n{result.stderr[-3000:]}")
    return result


def _docker_ready() -> bool:
    try:
        return subprocess.run([*DOCKER, "info"], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(
    os.environ.get("ALKERA_DOCKER_TESTS") != "1" or not _docker_ready(),
    reason="set ALKERA_DOCKER_TESTS=1 with a working docker to build the image")

#: Run as `engineer` inside the container; one PASS/FAIL line per check.
ENGINEER_CHECKS = r"""
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; }
check() { local n="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$n"; else fail "$n"; fi; }
deny()  { local n="$1"; shift; if "$@" >/dev/null 2>&1; then fail "$n (allowed)"; else pass "$n"; fi; }
[ "$(id -un)" = engineer ] && pass "running as engineer" || fail "running as $(id -un)"
for v in JIRA_URL SLACK_BASE_URL CONFLUENCE_URL METABASE_URL PROMETHEUS_URL ALERTMANAGER_URL LOKI_URL AIRBYTE_URL DEPLOY_URL SCHED_URL; do
  url="${!v}"
  case "$url" in http://*.internal:*) ;; *) fail "$v is $url"; continue;; esac
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url/health")
  case "$code" in 200|503) pass "$v answers ($code)";; *) fail "$v -> $code";; esac
done
deny  "cannot list /world" ls /world
deny  "cannot list /world/var" ls /world/var
deny  "cannot read the workflow plane" cat /world/var/workflow/issues.json
deny  "cannot list the workflow plane" ls /world/var/workflow
deny  "cannot read the scheduler plane" cat /world/var/sched/state.json
deny  "cannot list the ledger" ls /world/var/ledger
deny  "cannot list /world/build" ls /world/build
deny  "cannot read episode.json" cat /opt/alkera/episode.json
deny  "cannot read the services' gateway config" cat /opt/alkera/gateway.json
deny  "cannot list the runtime" ls /opt/alkera/bin
deny  "cannot write /etc/hosts" sh -c 'echo 10.0.0.1 jira.internal >> /etc/hosts'
check "lists the warehouse dir" ls /world/var/warehouse
check "/world/workspace is /workspace" test "$(readlink -f /world/workspace)" = /workspace
check "writes /workspace" sh -c 'echo x > /workspace/.probe && rm /workspace/.probe'
check "rw on the warehouse (duckdb shim)" duckdb /world/var/warehouse/warehouse.duckdb -c "CREATE TABLE probe_t AS SELECT 1 AS x; DROP TABLE probe_t"
check "appends to the action log (git shim)" git -C /workspace status --short
deny  "cannot create files in /audit" touch /audit/forged.json
deny  "cannot replace the watermark" sh -c 'echo {} > /audit/.episode_start.json'
deny  "cannot unlink the action log" rm -f /audit/actions.jsonl
check "*.internal resolves to loopback" sh -c 'getent hosts jira.internal | grep -q 127.0.0.1'
check "./ops/sched talks to the plane" sh -c 'cd /workspace && ./ops/sched list'
check "the toolbelt is there" sh -c 'aws --version && psql --version && dbt --version'
check "discovery files" sh -c 'test -f /workspace/.envrc && test -f /workspace/docs/onboarding.md && test -f ~/.config/alkera/services.toml'
check "login shells keep the shims first" sh -c 'bash -lc "echo \$PATH" | grep -q "^/opt/alkera/shims:"'
"""

#: GT008 only, as `engineer`: finance's card and the consumer metric both read
#: warehouse.v_revenue_daily, a view over legacy.warehouse.rpt_revenue_daily.
LEGACY_CHECKS = r"""
card=$(curl -s "$METABASE_URL/api/card" | python3 -c 'import json,sys; print(next(c["id"] for c in json.load(sys.stdin) if c["name"] == "revenue_by_day"))')
rows=$(curl -s -X POST -H 'content-type: application/json' -d '{}' "$METABASE_URL/api/card/$card/query" | python3 -c 'import json,sys; print(json.load(sys.stdin)["row_count"])')
[ "${rows:-0}" -gt 0 ] && echo "PASS legacy-backed card answers ($rows rows)" || echo "FAIL legacy-backed card: $rows"
days=$(curl -s "$PROMETHEUS_URL/api/v1/query?query=consumer_days" | python3 -c 'import json,sys; print(int(float(json.load(sys.stdin)["data"]["result"][0]["value"][1])))')
[ "${days:-0}" -gt 0 ] && echo "PASS legacy-backed metric answers ($days days)" || echo "FAIL legacy-backed metric: $days"
"""
CASES = [(fixture.TASK, arm) for arm in fixture.ARMS] + \
        [(fixture.GT008, arm) for arm in fixture.ARMS]


@pytest.fixture(scope="module")
def base_image_path():
    froms = [rest for k, rest in static.instructions() if k == "FROM"]
    docker("pull", "-q", froms[0], timeout=900)
    out = docker("image", "inspect", froms[0], "--format",
                 "{{range .Config.Env}}{{println .}}{{end}}").stdout
    return next(line[5:] for line in out.splitlines() if line.startswith("PATH="))


def test_base_image_path_is_the_one_the_dockerfile_extends(base_image_path):
    assert base_image_path == static.BASE_IMAGE_PATH


def assemble_context(arm: str, base: Path, task: str = fixture.TASK) -> tuple[Path, dict]:
    """environment/ as the generator lays it out, for one arm."""
    from alkera_harbor.adapter import copy_template
    from alkera_harbor.context import resolve_context

    context = base / "environment"
    copy_template(static.ENV_TEMPLATE, context)
    resolve_context(static.CONTEXT, context / "alkera", repo=REPO, task=task)
    world = fixture.build_world(arm, base, topology="container", task=task)
    document = fixture.arrange(world, context, arm, layout="context")
    return context, document


@pytest.mark.parametrize("task, arm", CASES)
def test_image_builds_starts_and_holds_the_permission_model(task, arm, tmp_path, capsys):
    context, document = assemble_context(arm, tmp_path, task)
    assert (context / "episode" / "gateway.json").is_file()
    tag = f"{TAG}:{arm}" if task == fixture.TASK else f"{TAG}:{task.split('-')[0].lower()}-{arm}"
    started = time.monotonic()
    docker("build", "-q", "-t", tag, str(context), timeout=3600)
    build_s = time.monotonic() - started
    size = docker("image", "inspect", tag, "--format", "{{.Size}}").stdout.strip()
    name = f"alkera-env-test-{task.split('-')[0].lower()}-{arm}-{os.getpid()}"
    env_flags = [flag for k, v in document["env"].items() for flag in ("-e", f"{k}={v}")]
    try:
        docker("run", "-d", "--name", name, "--network", "none", *env_flags, tag)
        up = docker("exec", name, "/opt/alkera/bin/world-up", check=False, timeout=300)
        assert up.returncode == 0, up.stderr + up.stdout
        again = docker("exec", name, "/opt/alkera/bin/world-up", check=False, timeout=300)
        assert again.returncode == 0 and "re-check" in again.stdout, again.stderr

        script = ENGINEER_CHECKS + (LEGACY_CHECKS if task == fixture.GT008 else "")
        checks = docker("exec", "-i", "-u", "engineer", "-w", "/workspace", name, "bash", "-s",
                        stdin=script, check=False, timeout=600)
        lines = [ln for ln in checks.stdout.splitlines() if ln.startswith(("PASS", "FAIL"))]
        failed = [ln for ln in lines if ln.startswith("FAIL")]
        assert lines and not failed, "\n".join(failed) + checks.stderr

        audit = json.loads(docker("exec", name, "python3", "-c", (
            "import json;rows=[json.loads(l) for l in open('/audit/actions.jsonl')];"
            "mark=json.load(open('/audit/.episode_start.json'));"
            "start=rows[mark['actions_lines']-1];"
            "print(json.dumps({'mark': mark, 'start': start['tool'], 'start_seq': start['seq'],"
            " 'tools': [r['tool'] for r in rows[mark['actions_lines']:]]}))"
        )).stdout)
        assert audit["start"] == "episode.start"
        assert audit["start_seq"] == audit["mark"]["actions_lines"] >= 1
        argv = docker("exec", name, "sh", "-c",
                      "tr '\\0' ' ' < /proc/$(cat /opt/alkera/run/serve.json | python3 -c "
                      "'import json,sys; print(json.load(sys.stdin)[\"pid\"])')/cmdline").stdout
        assert "--gateway-config /opt/alkera/gateway.json" in argv, argv
        tools = audit["tools"]
        assert "duckdb.statement" in tools and "shim.git" in tools, tools
        assert "sched.sched_list" in tools, tools
        before = len(tools) + audit["mark"]["actions_lines"]
        # root's calls through the same PATH (Harbor's agent installer) are not the agent's
        docker("exec", name, "bash", "-c", "python -c pass && git --version >/dev/null")
        after = int(docker("exec", name, "sh", "-c", "wc -l < /audit/actions.jsonl").stdout)
        assert after == before

        docker("exec", "-d", "-u", "engineer", name, "sleep", "1000")
        down = docker("exec", name, "/opt/alkera/bin/world-down", check=False, timeout=300)
        assert down.returncode == 0
        assert "leftover agent process" in down.stdout and "services stopped" in down.stdout
        left = docker("exec", name, "sh", "-c",
                      "ps -eo stat=,user=,args= | grep -v '^Z' | grep -E 'serve|sleep 1000' "
                      "| grep -v grep || true").stdout
        assert left.strip() == "", left
        assert docker("exec", name, "/opt/alkera/bin/world-down", check=False).returncode == 0
    finally:
        docker("rm", "-f", name, check=False)
    with capsys.disabled():
        print(f"\n[{task} {arm}] docker build {build_s:.0f}s, image {int(size) / 1e6:.0f} MB "
              f"(content size), {len(lines)} engineer checks passed")


#: As `engineer` after a publish round trip: the CLI the round trip stripped.
ROUND_TRIP_CHECKS = r"""
check() { local n="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS $n"; else echo "FAIL $n"; fi; }
check "./ops/sched is executable" test -x /workspace/ops/sched
check "./ops/sched runs" sh -c 'cd /workspace && ./ops/sched list'
check "git still works" sh -c 'git -C /workspace status --short && git -C /workspace log --oneline -1'
"""


def test_a_published_task_builds_and_runs(tmp_path, capsys):
    """`harbor publish` keeps file bytes only: every file 0644 (bin/world-up
    included) and no empty directory. The image must come out the same."""
    context, document = assemble_context("decision", tmp_path, fixture.TASK)
    document["restore"]["empty_dirs"].append("/tests/episode/grading/pre_repair_world/build")
    (context / "episode" / "episode.json").write_text(json.dumps(document))
    fixture.publish_round_trip(context)
    assert not os.access(context / "bin" / "world-up", os.X_OK)
    assert not (context / "episode" / "world" / "build").exists()
    assert not os.access(context / "episode" / "workspace" / "ops" / "sched", os.X_OK)

    tag = f"{TAG}:published"
    built = docker("build", "--progress=plain", "-t", tag, str(context), timeout=3600)
    log = built.stdout + built.stderr
    assert "left 1 verifier-zone restore entry to the verifier" in log, log[-3000:]
    name = f"alkera-env-test-published-{os.getpid()}"
    env_flags = [flag for k, v in document["env"].items() for flag in ("-e", f"{k}={v}")]
    try:
        docker("run", "-d", "--name", name, "--network", "none", *env_flags, tag)
        modes = docker("exec", name, "sh", "-c",
                       "stat -c '%a %U %n' /opt/alkera/bin/world-up /opt/alkera/bin/world-down "
                       "/opt/alkera/bin/world-seal /world/build /world/var/ledger "
                       "/workspace/ops/sched; test ! -e /tests && echo no-tests").stdout
        assert "700 root /opt/alkera/bin/world-up" in modes, modes
        assert "700 root /opt/alkera/bin/world-down" in modes, modes
        assert "700 root /world/build" in modes and "700 root /world/var/ledger" in modes, modes
        assert "755 engineer /workspace/ops/sched" in modes, modes
        assert "no-tests" in modes, modes
        up = docker("exec", name, "/opt/alkera/bin/world-up", check=False, timeout=300)
        assert up.returncode == 0, up.stderr + up.stdout
        checks = docker("exec", "-i", "-u", "engineer", "-w", "/workspace", name, "bash", "-s",
                        stdin=ROUND_TRIP_CHECKS, check=False, timeout=300)
        lines = [ln for ln in checks.stdout.splitlines() if ln.startswith(("PASS", "FAIL"))]
        assert lines and not [ln for ln in lines if ln.startswith("FAIL")], checks.stdout
        assert docker("exec", name, "/opt/alkera/bin/world-down",
                      check=False, timeout=300).returncode == 0
    finally:
        docker("rm", "-f", name, check=False)
    with capsys.disabled():
        print(f"\n[published round trip] {len(lines)} engineer checks passed")
