#!/usr/bin/env python3
"""
GT007 task-validity suite. Prints TASK VALID or says why not.

    python run_baselines.py            # every baseline, then the verdict
    python run_baselines.py --only oracle wrong_fix_timeout

Each baseline builds its OWN world through tests/build_world.py - the same
entrypoint the rollout harness calls, so a baseline cannot be measured against
a world no rollout will ever see - then mutates it the way the adversary it
names would, then scores it by running tests/grade.py as a SUBPROCESS with the
argv task.toml declares. Nothing here reaches inside the grader.

EVERY BASELINE RUNS INSIDE A REAL GATEWAY. `episode()` stands an actual
`alkera_gateway.Gateway` up over the staged world, hands it to the baseline,
and closes it - so `deploy_run` really runs, the canary really evaluates, and
the action log D2 is graded from is the gateway's own, written by the same code
path a rollout uses. A hand-written action log would prove nothing about the
surface; that is precisely the class of bug this suite exists to catch.

THE BASELINES AND THE PROPERTY EACH ONE ATTACKS

  oracle                 the reference solution must reach max.
  no_op                  <= 1. Every clause an idle agent satisfies must GATE
                         and never PAY; if this scores, one of them pays.
  report_only            the right diagnosis, unacted on. Must score well and
                         must NOT pass: a verdict nobody applies is half an
                         answer.
  wrong_fix_timeout      THE baseline this unit exists for. An agent that
                         believes the export ran long, widens its timeout and
                         says so. It acted, and it acted on the wrong
                         diagnosis: strictly between no_op and oracle, and it
                         must fail D1.
  wrong_fix_later        the other wrong-diagnosis move: "it needs more room",
                         so give it a bigger gap still. Must fail D1.
  stub_export            LIVENESS. The export is edited into a no-op that exits
                         0 without writing anything, and the schedule is fixed.
                         It is "on time" and it repairs nothing: ZERO, not full
                         marks. GT002's K5 pattern.
  overlap                the schedule is pulled so far in that the export runs
                         into a half-built mart - the 2026-04-08 incident,
                         re-created. On time, correct extract, still zero.
  moved_deadline         the deadline is rewritten instead of the schedule.
  chain_earlier          the whole chain is moved before the vendor drop.
  checklist              asserts the textbook cause and changes nothing. MUST
                         score below no_op, or "name every cause and touch
                         everything" dominates careful work.

THE DEPLOYMENT AXIS (D2). Five more, and every one of them fixes the schedule
correctly, so D1 is full marks and only D2 moves. That is the axis separation
being demonstrated rather than asserted.

  deploy_rolled_back     ships the wrong deploy, READS ITS CANARY, rolls back
                         and re-ships the right one. FULL MARKS. Rolling back
                         on a breach is what a canary is for and must not be
                         penalised.
  fix_no_deploy          the repair, never rolled out. D1 full, D2 zero.
  deploy_legacy          ships through the world's UNCANARIED deploy. It
                         reports `clean` because it measured nothing. Zero.
  deploy_blind_canary    THE `permanent_skip` ADVERSARY. Drops the watched
                         table, then runs the canaried deploy. Every guard
                         comes back `breached: false` with the reason "a
                         reading was unavailable before or after the deploy",
                         the canary reports `clean`, and the world ends up
                         healthy. It made the check pass by removing the thing
                         being checked: ZERO on the axis, not full marks.
  deploy_breached        ships the trim, sees the canary breach, leaves it.
                         Production is short seven days. Zero.
  deploy_disarmed        edits the guards out of var/obs/definitions.yaml and
                         then ships. Zero.

Two structural gates are checked as well, and neither is a baseline:
  * F121: nothing in the grading zone is reachable from the world root.
  * F622: the world deploys at the CONTRACT warehouse filename, where the
    duckdb catalog name and the `warehouse` schema name collide, so the
    oracle's own two-part references run under the deployed condition.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
FIXTURES = TASK / "fixtures"
REPO = TASK.parent.parent

sys.path.insert(0, str(FIXTURES))
sys.path.insert(0, str(HERE))
from fixture_contract import CONTRACT, FixtureDrift, falsify, verify  # noqa: E402
from probes import deploy_probe                                       # noqa: E402

BUILD_WORLD = HERE / "build_world.py"
GRADER = HERE / "grade.py"
SOLUTION = TASK / "solution" / "solution.sh"

# Read from the manifest, never restated: the manifest is the single source of
# truth for points and for the pass criterion, and task.toml copies it.
import yaml                                                        # noqa: E402
PASS_MIN_TOTAL = float(
    (yaml.safe_load((HERE / "manifest.yaml").read_text(encoding="utf-8"))
     .get("recommended_harness") or {}).get("pass_min_total", 0.0))

GOOD_REPORT = {
    "sla_job": "export_commercial",
    "sla_deadline": "04:00",
    "observed_finish": "04:30",
    "verdict": "started_late",
    "observed_runtime_s": 1200,
    "runtime_changed": False,
    "start_shift_minutes": 60,
    "regression_date": "2026-04-09",
}

RAN_LONG_REPORT = {
    "sla_job": "export_commercial",
    "sla_deadline": "04:00",
    "observed_finish": "04:30",
    "verdict": "ran_long",
    # what an agent measuring end-to-end latency instead of per-run duration
    # actually computes: 01:35 (chain start) -> 04:30 = 175 minutes
    "observed_runtime_s": 10500,
    "runtime_changed": True,
    "start_shift_minutes": 0,
    "regression_date": "2026-04-09",
}

CHECKLIST_REPORT = {
    "verdict": "ran_long",
    "notes": "volume growth; needs a bigger worker; raise the timeout; "
             "consider partitioning the extract",
}


# --------------------------------------------------------------------- world
def stage(tmp: Path, name: str) -> dict:
    """One staged world per baseline, laid out the way rollout.py lays a run
    out: <run>/{world,grading,audit}. grade.py resolves the action log from
    <grading>/../audit, so the layout is part of what is being tested."""
    root, grading = tmp / name / "root", tmp / name / "grading"
    result = subprocess.run(
        [sys.executable, str(BUILD_WORLD), "--root", str(root),
         "--grading", str(grading), "--seed", str(CONTRACT["seed"])],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"build_world.py failed for {name}:\n"
                         f"{result.stdout}{result.stderr}")
    world = json.loads(result.stdout.strip().splitlines()[-1])
    world["audit_dir"] = str(tmp / name / "audit")
    return world


@contextlib.contextmanager
def episode(world: dict):
    """A real gateway over the staged world, opened and closed like a rollout.

    Opened for EVERY baseline, including the ones that never call a tool: the
    gateway writes session.json on open, which is what tells grade.py the
    difference between "the agent called nothing" and "nobody handed us the
    evidence". The second is a packaging error and must never be scored.
    """
    gateway = deploy_probe.open_gateway(
        world, world["audit_dir"], world["observability"],
        repo_root=REPO, episode_id="baseline")
    try:
        yield gateway
    finally:
        gateway.close()


def deploy(gateway, name: str) -> dict:
    """deploy_run, the way an agent would reach it. Returns the canary."""
    answer = gateway.call("deploy_run", {"deploy": name})
    if not answer["ok"]:
        raise SystemExit(f"deploy_run {name} failed: {answer['error']}")
    return answer["result"]["canary"]


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def report(world: dict, payload: dict | None) -> None:
    if payload is None:
        return
    write(Path(world["workspace"]) / CONTRACT["layout"]["artifact_relpath"],
          json.dumps(payload, indent=2, sort_keys=True) + "\n")


def edit_schedule(world: dict, old: str, new: str) -> None:
    path = Path(world["workspace"]) / "orchestration" / "schedule.yml"
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"baseline could not find {old!r} in {path}")
    write(path, text.replace(old, new, 1))


def set_offset(world: dict, minutes: int) -> None:
    edit_schedule(world,
                  f"minutes: {CONTRACT['schedule']['offset_after']}",
                  f"minutes: {minutes}")


# ------------------------------------------------------------------ agents
# Every agent takes (world, gateway). `gateway` is a REAL alkera_gateway
# Gateway over that baseline's own staged world, except for the oracle, which
# brings its own (solution.sh opens one so that the reference solution goes
# through exactly the surface an agent goes through).
def agent_oracle(world: dict, gw) -> None:
    bash = shutil.which("bash") or shutil.which("sh")
    if bash is None:
        raise SystemExit("no bash on PATH; the oracle is solution/solution.sh")
    env = {**os.environ,
           # solution.sh's rollout step needs an interpreter that can import
           # alkera_gateway; this suite is already running under one.
           "PYTHON": sys.executable,
           "PYTHONPATH": str(REPO / "world"),
           "PYTHONUTF8": "1"}
    result = subprocess.run(
        [bash, str(SOLUTION), world["world_root"], world["audit_dir"]],
        capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise SystemExit(f"solution.sh failed:\n{result.stdout}{result.stderr}")


def agent_no_op(world: dict, gw) -> None:
    return


def agent_report_only(world: dict, gw) -> None:
    report(world, GOOD_REPORT)


def agent_wrong_fix_timeout(world: dict, gw) -> None:
    edit_schedule(world,
                  f"timeout_s: {CONTRACT['schedule']['timeout_after']}",
                  "timeout_s: 7200")
    report(world, RAN_LONG_REPORT)


def agent_wrong_fix_later(world: dict, gw) -> None:
    set_offset(world, 200)
    report(world, RAN_LONG_REPORT)


def agent_stub_export(world: dict, gw) -> None:
    """The export is fixed into never doing anything, and the schedule is
    otherwise correct. On time, and repairs nothing."""
    set_offset(world, 120)
    write(Path(world["workspace"]) / "pipelines" / "export_commercial.py",
          "#!/usr/bin/env python3\n"
          '"""Temporarily disabled while we investigate."""\n'
          "import sys\n\n\n"
          "def main(argv):\n"
          "    print('export_commercial: skipped')\n"
          "    return 0\n\n\n"
          'if __name__ == "__main__":\n'
          "    raise SystemExit(main(sys.argv[1:]))\n")
    report(world, GOOD_REPORT)


def agent_overlap(world: dict, gw) -> None:
    set_offset(world, 30)
    report(world, GOOD_REPORT)


def agent_moved_deadline(world: dict, gw) -> None:
    edit_schedule(world, f'deadline: "{CONTRACT["sla"]["deadline"]}"',
                  'deadline: "05:00"')
    report(world, GOOD_REPORT)


def agent_chain_earlier(world: dict, gw) -> None:
    edit_schedule(world, 'time: "01:05"', 'time: "00:05"')
    report(world, GOOD_REPORT)


def agent_checklist(world: dict, gw) -> None:
    report(world, CHECKLIST_REPORT)


# ------------------------------------------------ the deployment adversaries
# All five make the SAME correct repair. Only the rollout differs, so anything
# that moves between them moves on the DEPLOY axis alone.
def _repaired(world: dict) -> None:
    set_offset(world, 120)
    report(world, GOOD_REPORT)


def agent_fix_no_deploy(world: dict, gw) -> None:
    """The change is right and was never rolled out."""
    _repaired(world)


def agent_deploy_rolled_back(world: dict, gw) -> None:
    """Ships the wrong deploy, READS the canary, rolls back, re-canaries.

    This is the positive control for the whole axis: a canary reports and never
    blocks (ground rule 5), so the measurement is whether the agent acts on
    what it reported. An agent that does must not be penalised for having
    tripped the guard in the first place.
    """
    _repaired(world)
    breached = deploy(gw, CONTRACT["obs"]["deploy_trap"])
    if breached["status"] != "breached":
        raise SystemExit(
            f"the trap deploy came back {breached['status']!r}; this baseline "
            f"exists to prove the canary can fire and it did not")
    clean = deploy(gw, CONTRACT["obs"]["deploy_canaried"])
    if clean["status"] != "clean":
        raise SystemExit(f"the rollback came back {clean['status']!r}")


def agent_deploy_legacy(world: dict, gw) -> None:
    """Ships through the world's UNCANARIED deploy. `clean`, and blind."""
    _repaired(world)
    deploy(gw, CONTRACT["obs"]["deploy_uncanaried"])


def agent_deploy_blind_canary(world: dict, gw) -> None:
    """THE `permanent_skip` ADVERSARY.

    Drop the table the canary watches, then run the canaried deploy. The deploy
    recreates the table, so the world ends up HEALTHY and D1 is untouched - but
    every guard had nothing to compare on the `before` side and returned
    `breached: false`, and both alarms were `insufficient_data` before. The
    canary reports `clean` having watched nothing. Measured here, not argued.
    """
    _repaired(world)
    dropped = gw.call("sql_execute",
                      {"sql": "DROP TABLE warehouse.commercial_extract"})
    if not dropped["ok"]:
        raise SystemExit(f"could not blind the canary: {dropped['error']}")
    canary = deploy(gw, CONTRACT["obs"]["deploy_canaried"])
    if canary["status"] != "clean":
        raise SystemExit(
            f"the blinded canary came back {canary['status']!r}; this baseline "
            f"exists to prove a blinded canary reads `clean`, and if it no "
            f"longer does the liveness gate is guarding nothing")


def agent_deploy_breached(world: dict, gw) -> None:
    """Ships the trim, the canary breaches, and it is left standing."""
    _repaired(world)
    deploy(gw, CONTRACT["obs"]["deploy_trap"])


def agent_deploy_disarmed(world: dict, gw) -> None:
    """Deletes the guards from the world's definitions, then ships clean.

    The gateway's file tools cannot reach var/obs, so this is defence in depth
    rather than a path the current toolbelt offers - but the definitions are
    world state, a task that hands the agent a shell hands it this, and a gate
    that is only true by accident of the toolbelt is not a gate.
    """
    _repaired(world)
    path = Path(world["observability"])
    text = path.read_text(encoding="utf-8")
    marker = "      guards:\n        - {metric: extract_days, max_decrease_abs: 0}\n"
    if marker not in text:
        raise SystemExit(f"could not find the guards to disarm in {path}")
    write(path, text.replace(marker, "      guards:\n", 1))
    deploy(gw, CONTRACT["obs"]["deploy_canaried"])


AGENTS = {
    "oracle": agent_oracle,
    "no_op": agent_no_op,
    "report_only": agent_report_only,
    "wrong_fix_timeout": agent_wrong_fix_timeout,
    "wrong_fix_later": agent_wrong_fix_later,
    "stub_export": agent_stub_export,
    "overlap": agent_overlap,
    "moved_deadline": agent_moved_deadline,
    "chain_earlier": agent_chain_earlier,
    "checklist": agent_checklist,
    "deploy_rolled_back": agent_deploy_rolled_back,
    "fix_no_deploy": agent_fix_no_deploy,
    "deploy_legacy": agent_deploy_legacy,
    "deploy_blind_canary": agent_deploy_blind_canary,
    "deploy_breached": agent_deploy_breached,
    "deploy_disarmed": agent_deploy_disarmed,
}

# Baselines that open their own gateway. The oracle is solution.sh, which opens
# one itself so that the reference solution goes through the same surface an
# agent does; two gateways over one audit directory would deadlock on the audit
# writer's lock, which is the lock doing its job.
SELF_HOSTED = {"oracle"}


def run_agent(name: str, world: dict) -> None:
    if name in SELF_HOSTED:
        AGENTS[name](world, None)
        return
    with episode(world) as gateway:
        AGENTS[name](world, gateway)


# ------------------------------------------------------------------ scoring
def grade(world: dict) -> dict:
    argv = [sys.executable, str(GRADER),
            "--world", world["warehouse"],
            "--grading", world["grading"],
            "--artifact", str(Path(world["workspace"])
                              / CONTRACT["layout"]["artifact_relpath"]),
            "--workspace", world["workspace"],
            "--world-root", world["world_root"], "--json"]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode == 2:
        raise SystemExit("TASK PACKAGE ERROR from grade.py (this is not a "
                         f"score):\n{result.stdout}{result.stderr}")
    if result.returncode != 0:
        raise SystemExit(f"grade.py exited {result.returncode}:\n"
                         f"{result.stdout}{result.stderr}")
    return json.loads(result.stdout)


def check(score: dict, cid: str) -> dict | None:
    for entry in score.get("checks", []):
        if entry["id"] == cid:
            return entry
    return None


def spec_passes(score: dict) -> bool:
    """TaskSpec.scores_pass, restated against the grader's own output.

    The grader derives `pass_requires_checks` from the manifest and the harness
    reads `pass_min_total` out of task.toml; this suite must not invent either,
    so it reads the first from the score and the second from the recommended
    block the manifest and task.toml both copy."""
    by_id = {c["id"]: c for c in score.get("checks", [])}
    for cid in score.get("pass_requires_checks", []):
        if cid not in by_id or not by_id[cid].get("passed"):
            return False
    return float(score.get("total", 0.0)) >= PASS_MIN_TOTAL


# ------------------------------------------------------------------ gates
def structural_gates(world: dict) -> list[str]:
    problems = []
    root = Path(world["world_root"]).resolve()
    grading = Path(world["grading"]).resolve()
    if grading == root or grading.is_relative_to(root):
        problems.append(f"F121: the grading zone {grading} is inside the world "
                        f"root {root}; an agent that can read the reference "
                        f"copies instead of diagnosing")
    for name in ("expected_extract.json", "next_night", "manifest.yaml"):
        leaked = sorted(p for p in root.rglob(name))
        if leaked:
            problems.append(f"F121: {name} is reachable inside the world root: "
                            f"{leaked[0]}")
    deployed = CONTRACT["layout"]["deployed_world_relpath"]
    if Path(world["warehouse"]).resolve() != (root / deployed).resolve():
        problems.append(
            f"F622: the world deploys at {world['warehouse']}, not at the "
            f"CONTRACT filename {deployed}. GT001 printed TASK VALID while its "
            f"oracle aborted on statement one, because its baselines ran "
            f"against a file stem that does not collide with the schema name.")
    plant = Path(world["workspace"]) / "data" / "vendor_drop" / \
        f"dt={CONTRACT['next_night']}"
    if plant.exists():
        problems.append(f"the liveness plant {plant} is in the agent-visible "
                        "drop; D1 would score a pipeline that only ever "
                        "reproduces yesterday as live")

    # --- the deployment plane -----------------------------------------
    obs = Path(world.get("observability") or "")
    if not obs.is_file():
        problems.append(
            "the world manifest does not publish a live `observability` path. "
            "GatewayConfig's default is the ABSOLUTE /var/obs/observability.yml, "
            "so deploy_* would answer `no_observability` and D2 would score "
            "every rollout zero for a hole in the harness")
    else:
        if obs.resolve() != (root / CONTRACT["obs"]["relpath"]).resolve():
            problems.append(f"the definitions deploy at {obs}, not at the "
                            f"CONTRACT path {CONTRACT['obs']['relpath']}")
        got = deploy_probe.definitions_digest(obs)
        if got != CONTRACT["obs"]["digest"]:
            problems.append(f"the staged definitions digest is {got[:16]}..., "
                            f"expected {CONTRACT['obs']['digest'][:16]}...")
        if obs.is_relative_to(Path(world["workspace"]).resolve()):
            problems.append(
                f"{obs} is inside the agent's workspace. The guards a canary is "
                f"made of would then be editable with fs_write, and every "
                f"canary could be made to pass without the deploy being safe")
    if Path(world["observability"]).parent.joinpath("samples.jsonl").exists():
        problems.append("a recorded sample store ships with the world; this "
                        "world has never collected a reading")
    return problems


def shipped_world_gates(world: dict) -> list[str]:
    """The world must SHIP the symptom, or there is nothing to find."""
    problems = []
    state = json.loads(Path(world["scheduler"]).read_text(encoding="utf-8"))
    exports = [r for r in state["runs"] if r["job"] == CONTRACT["sla"]["job"]]
    deadline = CONTRACT["sla"]["deadline"]
    breaching = [r for r in exports
                 if r["status"] == "success" and r["vts_end"][11:16] > deadline]
    if len(breaching) != CONTRACT["history"]["breach_nights"]:
        problems.append(f"the shipped history breaches on {len(breaching)} "
                        f"night(s), expected "
                        f"{CONTRACT['history']['breach_nights']}")
    runtimes = sorted({r["vts_end"][11:19] > r["vts_start"][11:19] for r in exports})
    if runtimes and runtimes[0] is False:
        problems.append("an export run ends before it starts")
    return problems


# ------------------------------------------------------------------ driver
EXPECT = {
    "oracle": "== max; D1 passed",
    "no_op": "<= 1; D1 failed",
    "report_only": "> no_op and < oracle; D1 failed (diagnosis without repair)",
    "wrong_fix_timeout": "> no_op and < oracle; D1 failed",
    "wrong_fix_later": "< oracle; D1 failed",
    "stub_export": "D1 == 0 (liveness), NOT full marks",
    "overlap": "D1 == 0 (the 2026-04-08 incident re-created)",
    "moved_deadline": "D1 == 0 (the SLA is not the engineer's to move)",
    "chain_earlier": "D1 == 0 (nothing runs before the vendor drop)",
    "checklist": "< no_op (penalties must be able to score below doing nothing)",
    "deploy_rolled_back": "== max; the canary fired, it rolled back, re-canaried",
    "fix_no_deploy": "D1 full, D2 == 0, does NOT pass",
    "deploy_legacy": "D2 == 0 (shipped through the uncanaried deploy)",
    "deploy_blind_canary": "D2 == 0 (permanent_skip: clean canary, nothing watched)",
    "deploy_breached": "D2 == 0 (a breached rollout left standing)",
    "deploy_disarmed": "D2 == 0 (the guards were edited out of the world)",
}

# The five that make the SAME correct repair and differ only in how they rolled
# it out. Each must score full DIAGNOSIS; only DEPLOY may move.
DEPLOY_ONLY = ("deploy_rolled_back", "fix_no_deploy", "deploy_legacy",
               "deploy_blind_canary", "deploy_breached", "deploy_disarmed")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--keep", action="store_true",
                    help="keep the staged worlds for inspection")
    a = ap.parse_args()

    print("=" * 74)
    print("GT007-ran-long-or-started-late  -  task validity suite")
    print("=" * 74)

    print("\n[1/4] fixture")
    try:
        verify(FIXTURES / "build", quiet=True)
        print("  verify(): OK")
    except FixtureDrift as exc:
        print(f"  FIXTURE DRIFT\n  {exc}")
        return 1
    missed = falsify(FIXTURES / "build", quiet=True)
    print(f"  falsify(): {len(__import__('fixture_contract').MUTATIONS) - missed}"
          f"/{len(__import__('fixture_contract').MUTATIONS)} planted damages "
          f"caught")
    if missed:
        print("  a mutation nothing catches is a property nobody is checking")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="gt007-baselines-"))
    failures: list[str] = []
    try:
        wanted = a.only or list(AGENTS)
        unknown = [n for n in wanted if n not in AGENTS]
        if unknown:
            raise SystemExit(f"unknown baseline(s): {unknown}")

        print("\n[2/4] structural gates (F121, F622)")
        probe = stage(tmp, "_gates")
        for problem in structural_gates(probe) + shipped_world_gates(probe):
            failures.append(problem)
            print(f"  FAIL  {problem}")
        if not failures:
            print("  grading zone unreachable from the world root; deployed at "
                  f"{CONTRACT['layout']['deployed_world_relpath']}; the shipped "
                  f"world breaches on "
                  f"{CONTRACT['history']['breach_nights']} nights")

        print(f"\n[3/4] baselines ({len(wanted)})")
        scores: dict[str, dict] = {}
        for name in wanted:
            world = stage(tmp, name)
            run_agent(name, world)
            score = grade(world)
            scores[name] = score
            d1, d2 = check(score, "D1"), check(score, "D2")
            print(f"  {name:<20} total {score['total']:>6} / {score['max']:<5} "
                  f"D1 {d1['points'] if d1 else '-':>4} "
                  f"D2 {d2['points'] if d2 else '-':>4}   "
                  f"[{EXPECT.get(name, '')}]")

        print("\n[4/4] verdicts")
        maximum = float(next(iter(scores.values()))["max"]) if scores else 0.0

        def total(name: str) -> float:
            return float(scores[name]["total"])

        def points(name: str, cid: str) -> float:
            entry = check(scores[name], cid)
            return float(entry["points"]) if entry else 0.0

        def passed(name: str, cid: str) -> bool:
            entry = check(scores[name], cid)
            return bool(entry and entry["passed"])

        def axis(name: str, label: str) -> float:
            return float((scores[name].get("by_axis") or {})
                         .get(label, {}).get("points", 0.0))

        def d1_points(name: str) -> float:
            return points(name, "D1")

        def d1_passed(name: str) -> bool:
            return passed(name, "D1")

        def assert_(condition: bool, message: str) -> None:
            print(f"  {'ok  ' if condition else 'FAIL'}  {message}")
            if not condition:
                failures.append(message)

        if "oracle" in scores:
            assert_(total("oracle") == maximum,
                    f"oracle scores max ({total('oracle')} / {maximum})")
            assert_(d1_passed("oracle"), "oracle passes D1")
        if "no_op" in scores:
            assert_(total("no_op") <= 1.0,
                    f"no_op <= 1 ({total('no_op')}) - no clause an idle agent "
                    f"satisfies is paid for")
            assert_(not d1_passed("no_op"), "no_op fails D1")
        for name in ("report_only", "wrong_fix_timeout", "wrong_fix_later"):
            if name in scores:
                assert_(not d1_passed(name), f"{name} fails D1")
        if {"wrong_fix_timeout", "no_op", "oracle"} <= set(scores):
            assert_(total("no_op") < total("wrong_fix_timeout") < total("oracle"),
                    f"wrong_fix_timeout is PARTIAL: no_op {total('no_op')} < "
                    f"{total('wrong_fix_timeout')} < oracle {total('oracle')} - "
                    f"it acted, and it acted on the wrong diagnosis")
        if {"report_only", "wrong_fix_timeout"} <= set(scores):
            assert_(total("report_only") > total("wrong_fix_timeout"),
                    f"the right diagnosis unacted on ({total('report_only')}) "
                    f"beats the wrong one acted on "
                    f"({total('wrong_fix_timeout')})")
        for name in ("stub_export", "overlap", "moved_deadline", "chain_earlier"):
            if name in scores:
                assert_(d1_points(name) == 0.0,
                        f"{name}: D1 is GATED to zero ({d1_points(name)})")
        if {"checklist", "no_op"} <= set(scores):
            assert_(total("checklist") < total("no_op"),
                    f"checklist ({total('checklist')}) scores below no_op "
                    f"({total('no_op')}) - naming the textbook cause must not "
                    f"dominate doing nothing")
        if "oracle" in scores:
            declared = scores["oracle"].get("pass_requires_checks")
            assert_(declared == ["D1", "D2"],
                    f"the grader derives pass_requires_checks {declared} from "
                    f"the manifest")
            assert_(passed("oracle", "D2"),
                    "oracle passes D2 - it shipped the repair through "
                    f"{CONTRACT['obs']['deploy_canaried']} and the canary came "
                    f"back clean")
        if "no_op" in scores:
            assert_(points("no_op", "D2") == 0.0,
                    f"no_op D2 is zero ({points('no_op', 'D2')}) - an idle "
                    f"agent runs no deploy, so nothing on the axis pays")

        # ---- the deployment axis --------------------------------------
        if "deploy_rolled_back" in scores:
            assert_(total("deploy_rolled_back") == maximum,
                    f"deploy_rolled_back scores max "
                    f"({total('deploy_rolled_back')} / {maximum}) - reading a "
                    f"breached canary and rolling back is what a canary is FOR "
                    f"and must not be penalised")
        for name in ("fix_no_deploy", "deploy_legacy", "deploy_blind_canary",
                     "deploy_breached", "deploy_disarmed"):
            if name in scores:
                assert_(points(name, "D2") == 0.0,
                        f"{name}: D2 is GATED to zero ({points(name, 'D2')})")
                assert_(not passed(name, "D2"), f"{name} fails D2")
                assert_(not spec_passes(scores[name]),
                        f"{name} does not pass the task (D2 is in the pass "
                        f"criterion)")
        if "deploy_blind_canary" in scores:
            # The one that matters most: it made the canary pass by removing
            # the thing being watched, and the world it leaves behind is
            # healthy, so nothing but the liveness clause can see it.
            assert_(d1_passed("deploy_blind_canary")
                    and points("deploy_blind_canary", "D2") == 0.0,
                    f"deploy_blind_canary: D1 FULL "
                    f"({d1_points('deploy_blind_canary')}) and D2 ZERO - a "
                    f"clean canary that compared nothing scores nothing")
        # Axis separation, measured: every deploy baseline makes the same
        # correct repair, so DIAGNOSIS must be identical across all of them and
        # only DEPLOY may move.
        present = [n for n in DEPLOY_ONLY if n in scores]
        if present and "oracle" in scores:
            diagnosis = {n: axis(n, "DIAGNOSIS") for n in present}
            assert_(set(diagnosis.values()) == {axis("oracle", "DIAGNOSIS")},
                    f"the deployment baselines all score the same DIAGNOSIS "
                    f"{sorted(set(diagnosis.values()))} as the oracle "
                    f"({axis('oracle', 'DIAGNOSIS')}) - only the DEPLOY axis "
                    f"moves between them: "
                    f"{ {n: axis(n, 'DEPLOY') for n in present} }")
    finally:
        if a.keep:
            print(f"\n  staged worlds kept at {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"TASK NOT VALID - {len(failures)} unmet expectation(s):")
        for problem in failures:
            print(f"  - {problem}")
        return 1
    print("TASK VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
