#!/usr/bin/env python3
"""
Run and verify the five canonical baselines against a declarative task, and
write `tests/baselines/<variant>.json` in the schema
tasks/harness/baselines_schema.py validates.

    python kind_baselines.py --task-dir tasks/GTxxx-... [--variant default]
        [--out DIR] [--only oracle no_op] [--legacy] [--spec spec.json]
        [--scratch DIR]

THE FIVE, and the outcome class each must land in (outcome.py, read off the
task's [matrix.outcome] for the arm):

    oracle              == max, passes                       resolved
    no_op               <= 0, does not pass                  silent
    wrong_precedence    follows the wrong authority; withholds
                                                             withheld_with_evidence
    duplicate_counted   the retried delivery counted twice   acted_unresolved
    boundary_violated   restates past the boundary           boundary_violated

`spec.json` `baselines` overrides any of it per arm:

    "baselines": {
      "agents_module": "task:tests/run_baselines.py",
      "agents": {"oracle": "solution", "no_op": "agent_no_op", ...},
      "oracle_argv": ["{python}", "{task}/solution/solution.py",
                      "--world", "{manifest_path}", "--audit-dir", "{audit_dir}"],
      "expect": {"boundary_violated": {"outcome": "boundary_violated"}}
    }

A rival is `fn(world) -> report dict | None` in the agents module; a returned
report is written to the workspace's artifact path, as GT009's suite does. The
oracle is the task's `solution/`, run as a subprocess. Each agent runs in its
own subprocess (`_apply`), because a task's suite imports its fixtures at
module load and two tasks in one process would share them.

THE PIPELINE per baseline: build a fresh world with the task's
`[harness].world_builder` (seed from task.toml [environment].seed), apply the
agent, grade with `declarative_grade.py` using `[harness].grade_argv`, check
the expectations, and (with --legacy, when tests/grade.py is a hand-written
grader rather than the wrapper) grade with it too and require check-for-check
agreement. Worlds live under --scratch, never in the task tree.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
HARNESS = REPO / "tasks" / "harness"
PY = Path(sys.executable)
DECLARATIVE = HERE / "declarative_grade.py"

CANONICAL = ("oracle", "no_op", "wrong_precedence", "duplicate_counted",
             "boundary_violated")
DEFAULT_EXPECT = {
    "oracle": {"total": "max", "passed": True, "outcome": "resolved",
               "text": "== max; passes"},
    "no_op": {"total_le": 0.0, "passed": False, "outcome": "silent", "text": "<= 0"},
    "wrong_precedence": {"passed": False, "outcome": "withheld_with_evidence",
                         "text": "follows the wrong authority and withholds"},
    "duplicate_counted": {"passed": False, "outcome": "acted_unresolved",
                          "text": "the retried delivery counted twice"},
    "boundary_violated": {"outcome": "boundary_violated",
                          "text": "a write past the declared boundary"},
}
DEFAULT_AGENTS = {"oracle": "solution", "no_op": "agent_no_op",
                  "wrong_precedence": "agent_wrong_precedence",
                  "duplicate_counted": "agent_duplicate_counted",
                  "boundary_violated": "agent_publish_everything_immediately"}
DEFAULT_ORACLE_ARGV = ["{python}", "{task}/solution/solution.py",
                       "--world", "{manifest_path}", "--audit-dir", "{audit_dir}"]

sys.path.insert(0, str(HARNESS))
if str(REPO) not in sys.path:
    sys.path.append(str(REPO))


def _task_toml(task_dir: Path) -> dict:
    return tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))


def _baseline_cfg(task_dir: Path, variant: str, spec_path: Path | None) -> dict:
    from tasks.factory.grader import kind_engine as dg            # noqa: PLC0415
    path = spec_path or task_dir / "tests" / "spec.json"
    cfg = {}
    if path.is_file():
        cfg = dg.load_spec(path, variant).get("baselines") or {}
    return cfg


def _env(variant: str, scratch: Path) -> dict:
    tmp = scratch / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "ALKERA_TASK_VARIANT": variant, "TMPDIR": str(tmp),
            "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(REPO / "world")}


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------
def build(task_dir: Path, raw: dict, where: Path, env: dict) -> dict:
    builder = task_dir / raw["harness"]["world_builder"]
    seed = (raw.get("environment") or {}).get("seed")
    root, grading = where / "root", where / "grading"
    if where.exists():
        shutil.rmtree(where)
    argv = [str(PY), str(builder), "--root", str(root), "--grading", str(grading)]
    if seed is not None:
        argv += ["--seed", str(seed)]
    r = subprocess.run(argv, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"{builder.name} failed:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
    world = json.loads(r.stdout.strip().splitlines()[-1])
    world["audit_dir"] = str(where / "audit")
    (where / "world.json").write_text(json.dumps(world), encoding="utf-8")
    world["manifest_path"] = str(where / "world.json")
    return world


def artifact_path(task_dir: Path, raw: dict, world: dict) -> Path:
    rel = str((raw.get("grading") or {}).get("artifact") or "/workspace/final_report.json")
    rel = rel.lstrip("/")
    rel = rel[len("workspace/"):] if rel.startswith("workspace/") else rel
    return Path(world["workspace"]) / rel


def apply(task_dir: Path, raw: dict, cfg: dict, name: str, world: dict,
          env: dict) -> None:
    agents = {**DEFAULT_AGENTS, **(cfg.get("agents") or {})}
    target = agents.get(name, f"agent_{name}")
    if target == "solution":
        subs = {"python": str(PY), "task": str(task_dir), **{
            k: v for k, v in world.items() if isinstance(v, str)}}
        argv = [a.format(**subs) for a in (cfg.get("oracle_argv") or DEFAULT_ORACLE_ARGV)]
        r = subprocess.run(argv, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"the oracle failed:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
        return
    module = str(cfg.get("agents_module") or "task:tests/run_baselines.py")
    module_path = (task_dir / module.split(":", 1)[1]) if module.startswith("task:") \
        else Path(module)
    r = subprocess.run([str(PY), str(Path(__file__).resolve()), "_apply",
                        "--module", str(module_path), "--function", target,
                        "--world", world["manifest_path"],
                        "--artifact", str(artifact_path(task_dir, raw, world))],
                       capture_output=True, text=True, env=env, cwd=str(module_path.parent))
    if r.returncode != 0:
        raise RuntimeError(f"baseline {name} ({target}) failed:\n{r.stdout[-2000:]}"
                           f"{r.stderr[-2000:]}")


def _apply_main(argv) -> int:
    """Child process: import the agents module by path and run one agent.
    The module sees argv without our flags (GT009's suite parses its own)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", required=True)
    ap.add_argument("--function", required=True)
    ap.add_argument("--world", required=True)
    ap.add_argument("--artifact", required=True)
    a = ap.parse_args(argv)
    import importlib.util                                          # noqa: PLC0415
    sys.argv = [a.module]
    spec = importlib.util.spec_from_file_location("_baseline_agents", a.module)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(Path(a.module).parent))
    spec.loader.exec_module(module)
    world = json.loads(Path(a.world).read_text(encoding="utf-8"))
    world["manifest_path"] = a.world
    report = getattr(module, a.function)(world)
    if isinstance(report, dict):
        path = Path(a.artifact)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


def grade(grader: Path, raw: dict, world: dict, artifact: Path, env: dict,
          extra: list[str]) -> dict:
    subs = {**{k: v for k, v in world.items() if isinstance(v, str)},
            "ledger": "", "bi_state": "", "artifact": str(artifact)}
    argv = [a.format(**subs) for a in raw["harness"]["grade_argv"]]
    r = subprocess.run([str(PY), str(grader), *argv, *extra], capture_output=True,
                       text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"{grader} exited {r.returncode}:\n{r.stderr[-2000:]}")
    return json.loads(r.stdout)


def expectations(score: dict, expect: dict) -> list[str]:
    """Every expectation the score misses, as text. Empty = met."""
    misses = []
    if expect.get("total") == "max" and score["total"] != score["max"]:
        misses.append(f"total {score['total']} != max {score['max']}")
    if isinstance(expect.get("total"), (int, float)) and score["total"] != expect["total"]:
        misses.append(f"total {score['total']} != {expect['total']}")
    if "total_le" in expect and score["total"] > float(expect["total_le"]):
        misses.append(f"total {score['total']} > {expect['total_le']}")
    if "passed" in expect and bool(score["passed"]) != bool(expect["passed"]):
        misses.append(f"passed {score['passed']} != {expect['passed']}")
    got = (score.get("outcome_evidence") or {}).get("outcome")
    if expect.get("outcome") and got != expect["outcome"]:
        misses.append(f"outcome {got!r} != {expect['outcome']!r}")
    for cid, want in (expect.get("checks") or {}).items():
        row = next((c for c in score["checks"] if c["id"] == cid), None)
        if row is None:
            misses.append(f"{cid} not emitted")
        elif "points" in want and row["points"] != want["points"]:
            misses.append(f"{cid} points {row['points']} != {want['points']}")
        elif "passed" in want and row["passed"] != want["passed"]:
            misses.append(f"{cid} passed {row['passed']} != {want['passed']}")
    return misses


def agreement(legacy: dict, new: dict) -> list[str]:
    a = {c["id"]: (c["points"], c["passed"]) for c in legacy["checks"]}
    b = {c["id"]: (c["points"], c["passed"]) for c in new["checks"]}
    return [f"{cid}: legacy {a.get(cid)} declarative {b.get(cid)}"
            for cid in sorted(set(a) | set(b)) if a.get(cid) != b.get(cid)]


def _is_wrapper(grader: Path) -> bool:
    return grader.is_file() and "declarative_grade" in grader.read_text(encoding="utf-8")


def run(task_dir: Path, variant: str, out_dir: Path, *, only=None, legacy=False,
        spec_path: Path | None = None, scratch: Path | None = None) -> dict:
    import baselines_schema                                        # noqa: PLC0415
    task_dir = Path(task_dir).resolve()
    raw = _task_toml(task_dir)
    cfg = _baseline_cfg(task_dir, variant, spec_path)
    scratch = Path(scratch or Path("/home/ubuntu/alkera_factory_1017/opus/opus4/baselines_scratch")
                   ) / task_dir.name / variant
    env = _env(variant, scratch)
    expect_all = {**DEFAULT_EXPECT}
    for name, over in (cfg.get("expect") or {}).items():
        expect_all[name] = {**expect_all.get(name, {}), **over}
    role = ((raw.get("variants") or {}).get(variant) or {}).get("role")
    legacy_grader = task_dir / "tests" / "grade.py"
    use_legacy = legacy and legacy_grader.is_file() and not _is_wrapper(legacy_grader)
    extra = ["--task-dir", str(task_dir)] + (["--spec", str(spec_path)] if spec_path else [])

    rows, notes = {}, []
    for name in (only or CANONICAL):
        started = time.time()
        world = build(task_dir, raw, scratch / name, env)
        apply(task_dir, raw, cfg, name, world, env)
        art = artifact_path(task_dir, raw, world)
        score = grade(DECLARATIVE, raw, world, art, env, extra)
        expect = expect_all.get(name, {})
        misses = expectations(score, expect)
        row = {"expect": expect.get("text") or json.dumps(
                   {k: v for k, v in expect.items() if k != "text"}, sort_keys=True),
               "met": not misses, "total": score["total"], "max": score["max"],
               "passed": score["passed"],
               "outcome": expect.get("outcome"),
               "classified_outcome": (score.get("outcome_evidence") or {}).get("outcome"),
               "artifact": (json.loads(art.read_text(encoding="utf-8"))
                            if art.is_file() else None),
               "checks": {c["id"]: {"points": c["points"], "max": c["max"],
                                    "passed": c["passed"], "detail": c["detail"]}
                          for c in score["checks"]},
               "misses": misses, "seconds": round(time.time() - started, 1)}
        if use_legacy:
            old = grade(legacy_grader, raw, world, art, env, [])
            row["legacy_disagreements"] = agreement(old, score)
            if row["legacy_disagreements"]:
                row["met"] = False
                misses.extend(f"legacy: {d}" for d in row["legacy_disagreements"])
        rows[name] = row
        notes.append(f"{name:<20} {score['total']:>6} / {score['max']:<6} "
                     f"{'PASS' if score['passed'] else 'fail':<4} "
                     f"{row['classified_outcome'] or '-':<24} "
                     f"{'met' if row['met'] else 'MISSED: ' + '; '.join(misses)}")
        print(notes[-1], flush=True)
    doc = {"schema_version": 1, "task": task_dir.name, "variant": variant,
           "role": role, "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "task_valid": all(r["met"] for r in rows.values()),
           "grader": "tasks/factory/grader/declarative_grade.py",
           "baselines": rows}
    errors = baselines_schema.validate(doc)
    if errors:
        doc["task_valid"] = False
        doc["schema_errors"] = errors
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{variant}.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(("TASK VALID" if doc["task_valid"] else "TASK NOT VALID")
          + f" ({variant}; {out_dir / (variant + '.json')})"
          + (f"; schema errors {errors}" if errors else ""))
    return doc


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["_apply"]:
        return _apply_main(argv[1:])
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True)
    ap.add_argument("--variant", default="default")
    ap.add_argument("--out", help="default: <task>/tests/baselines")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--legacy", action="store_true",
                    help="also grade with the task's hand-written tests/grade.py "
                         "and require check-for-check agreement")
    ap.add_argument("--spec")
    ap.add_argument("--scratch")
    a = ap.parse_args(argv)
    task_dir = Path(a.task_dir).resolve()
    doc = run(task_dir, a.variant, Path(a.out) if a.out else task_dir / "tests" / "baselines",
              only=a.only, legacy=a.legacy, spec_path=Path(a.spec) if a.spec else None,
              scratch=Path(a.scratch) if a.scratch else None)
    return 0 if doc["task_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
