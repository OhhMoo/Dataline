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

Two structural gates are checked as well, and neither is a baseline:
  * F121: nothing in the grading zone is reachable from the world root.
  * F622: the world deploys at the CONTRACT warehouse filename, where the
    duckdb catalog name and the `warehouse` schema name collide, so the
    oracle's own two-part references run under the deployed condition.
"""
from __future__ import annotations

import argparse
import json
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
from fixture_contract import CONTRACT, FixtureDrift, falsify, verify  # noqa: E402

BUILD_WORLD = HERE / "build_world.py"
GRADER = HERE / "grade.py"
SOLUTION = TASK / "solution" / "solution.sh"

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
    root, grading = tmp / name / "root", tmp / name / "grading"
    result = subprocess.run(
        [sys.executable, str(BUILD_WORLD), "--root", str(root),
         "--grading", str(grading), "--seed", str(CONTRACT["seed"])],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"build_world.py failed for {name}:\n"
                         f"{result.stdout}{result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


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
def agent_oracle(world: dict) -> None:
    bash = shutil.which("bash") or shutil.which("sh")
    if bash is None:
        raise SystemExit("no bash on PATH; the oracle is solution/solution.sh")
    result = subprocess.run([bash, str(SOLUTION), world["world_root"]],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"solution.sh failed:\n{result.stdout}{result.stderr}")


def agent_no_op(world: dict) -> None:
    return


def agent_report_only(world: dict) -> None:
    report(world, GOOD_REPORT)


def agent_wrong_fix_timeout(world: dict) -> None:
    edit_schedule(world,
                  f"timeout_s: {CONTRACT['schedule']['timeout_after']}",
                  "timeout_s: 7200")
    report(world, RAN_LONG_REPORT)


def agent_wrong_fix_later(world: dict) -> None:
    set_offset(world, 200)
    report(world, RAN_LONG_REPORT)


def agent_stub_export(world: dict) -> None:
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


def agent_overlap(world: dict) -> None:
    set_offset(world, 30)
    report(world, GOOD_REPORT)


def agent_moved_deadline(world: dict) -> None:
    edit_schedule(world, f'deadline: "{CONTRACT["sla"]["deadline"]}"',
                  'deadline: "05:00"')
    report(world, GOOD_REPORT)


def agent_chain_earlier(world: dict) -> None:
    edit_schedule(world, 'time: "01:05"', 'time: "00:05"')
    report(world, GOOD_REPORT)


def agent_checklist(world: dict) -> None:
    report(world, CHECKLIST_REPORT)


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
}


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
}


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
            AGENTS[name](world)
            score = grade(world)
            scores[name] = score
            d1 = check(score, "D1")
            print(f"  {name:<20} total {score['total']:>6} / {score['max']:<5} "
                  f"D1 {d1['points'] if d1 else '-':>4} "
                  f"{'PASS' if d1 and d1['passed'] else 'fail'}   "
                  f"[{EXPECT.get(name, '')}]")

        print("\n[4/4] verdicts")
        maximum = float(next(iter(scores.values()))["max"]) if scores else 0.0

        def total(name: str) -> float:
            return float(scores[name]["total"])

        def d1_points(name: str) -> float:
            entry = check(scores[name], "D1")
            return float(entry["points"]) if entry else 0.0

        def d1_passed(name: str) -> bool:
            entry = check(scores[name], "D1")
            return bool(entry and entry["passed"])

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
            assert_(declared == ["D1"],
                    f"the grader derives pass_requires_checks {declared} from "
                    f"the manifest")
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
