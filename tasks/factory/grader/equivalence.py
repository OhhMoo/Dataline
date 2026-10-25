#!/usr/bin/env python3
"""
Equivalence: grade recorded rollouts through BOTH a task's hand-written
`tests/grade.py` and `declarative_grade.py`, and compare check for check.

    python equivalence.py gt009 [--jobs 6] [--out equivalence.json]
    python equivalence.py gt004 ...
    python equivalence.py custom --task-dir D --spec S [--manifest M] RUN_DIR...

A RUN is a recorded rollout directory (rollout.json + world/ + grading/). The
inputs both graders see are the rollout's own: rollout.json `world` names the
warehouse, grading zone, workspace and world root; `artifact_path` the report.
Neither grader writes to them - the replay advances an isolated copy.

A CELL is one (run, check id); it AGREES when both graders emit the check
with the same `points` and the same `passed`. A check one grader emits and
the other does not is a disagreement too. The run-level `total`, `max` and
`passed` are compared and reported beside the cells.

Arm fixtures are staged by tasks/harness/taskspec under tempfile.gettempdir();
every grader subprocess here runs with TMPDIR pointed at a private scratch
directory, so a concurrent grader elsewhere on the host staging the same task
from another tree cannot restage the tree under this one.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PY = REPO / "gateway" / ".venv" / "bin" / "python"
DECLARATIVE = HERE / "declarative_grade.py"
OUT_ROOT = Path(os.environ.get("ALKERA_EQUIVALENCE_ROOT", "/home/ubuntu/alkera_factory_1017/opus/opus4"))

ROLLOUTS = Path("/home/ubuntu/alkera_rollouts")
DESAT = Path("/home/ubuntu/desaturation_experiment_20261017")

SUITES = {
    "gt009": {
        "spec": HERE / "specs" / "gt009.spec.json",
        "arms": [
            # (arm label, glob of run dirs, task dir whose grade.py + fixtures apply)
            ("default", ROLLOUTS / "GT009-settlement-cutoff", "default-20261017-k5-opus55h/r*",
             REPO / "tasks" / "GT009-settlement-cutoff"),
            ("approved", ROLLOUTS / "GT009-settlement-cutoff", "approved-20261017-k5-opus55h/r*",
             REPO / "tasks" / "GT009-settlement-cutoff"),
            ("live-consumer", ROLLOUTS / "GT009-settlement-cutoff",
             "live-consumer-20261017-k5-opus55h/r*", REPO / "tasks" / "GT009-settlement-cutoff"),
            ("e1-corecraft-implicit", DESAT / "runs" / "GT009-settlement-cutoff",
             "e1-corecraft-implicit-20261017-k5-opus55h/r*", DESAT / "tasks" / "GT009-settlement-cutoff"),
            ("e2-noisy-surface", DESAT / "runs" / "GT009-settlement-cutoff",
             "e2-noisy-surface-20261017-k5-opus55h/r*", DESAT / "tasks" / "GT009-settlement-cutoff"),
            ("e3-compositional", DESAT / "runs" / "GT009-settlement-cutoff",
             "e3-compositional-20261017-k5-opus55h/r*", DESAT / "tasks" / "GT009-settlement-cutoff"),
        ],
    },
    "gt004": {
        "spec": HERE / "specs" / "gt004.spec.json",
        "arms": [
            ("default", ROLLOUTS / "GT004-doubling-window", "default-20261017-k5-opus55h/r*",
             REPO / "tasks" / "GT004-doubling-window"),
            ("authorized", ROLLOUTS / "GT004-doubling-window", "authorized-20261017-k5-opus55h/r*",
             REPO / "tasks" / "GT004-doubling-window"),
            ("escalation-credited", ROLLOUTS / "GT004-doubling-window",
             "escalation-credited-20261017-k5-opus55h/r*", REPO / "tasks" / "GT004-doubling-window"),
        ],
    },
}


def run_inputs(run: Path) -> dict:
    """The grader argv substitutions for a recorded rollout."""
    rec = json.loads((run / "rollout.json").read_text(encoding="utf-8"))
    world = rec.get("world") or {}
    root = Path(world.get("world_root") or run / "world")
    artifact = rec.get("artifact_path") or str(root / "workspace" / "final_report.json")
    return {"warehouse": world.get("warehouse") or str(root / "var/warehouse/warehouse.duckdb"),
            "grading": world.get("grading") or str(run / "grading"),
            "workspace": world.get("workspace") or str(root / "workspace"),
            "world_root": str(root), "artifact": artifact,
            "recorded_total": (rec.get("score") or {}).get("total")}


def _argv(inputs: dict) -> list[str]:
    return ["--world", inputs["warehouse"], "--grading", inputs["grading"],
            "--artifact", inputs["artifact"], "--workspace", inputs["workspace"],
            "--world-root", inputs["world_root"], "--json"]


def _run(cmd: list[str], env: dict) -> tuple[dict | None, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(REPO))
    if r.returncode != 0:
        return None, f"exit {r.returncode}: {(r.stderr or '').strip()[-1500:]}"
    try:
        return json.loads(r.stdout), ""
    except ValueError:
        return None, f"non-json stdout: {r.stdout[-500:]}"


def _env(arm: str) -> dict:
    scratch = OUT_ROOT / "tmp" / arm
    scratch.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "TMPDIR": str(scratch), "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(REPO / "world")}


def compare(legacy: dict | None, new: dict | None) -> dict:
    """Cells over every check either grader emits, except the ones the spec
    declares `uncovered_checks` (listed under `uncovered`, never counted as
    agreeing). Run-level keys are compared only when nothing is uncovered and
    the legacy grader emits them."""
    out = {"cells": [], "agree": 0, "total": 0, "run_level": {}, "uncovered": []}
    if legacy is None or new is None:
        return out
    a = {c["id"]: c for c in legacy.get("checks", [])}
    b = {c["id"]: c for c in new.get("checks", [])}
    skipped = set(new.get("uncovered_checks") or [])
    out["uncovered"] = [cid for cid in a if cid in skipped]
    for cid in list(a) + [c for c in b if c not in a]:
        if cid in skipped:
            continue
        x, y = a.get(cid), b.get(cid)
        same = (x is not None and y is not None
                and round(float(x["points"]), 4) == round(float(y["points"]), 4)
                and bool(x["passed"]) == bool(y["passed"]))
        out["cells"].append({
            "id": cid, "agree": same,
            "legacy": None if x is None else {"points": x["points"], "passed": x["passed"]},
            "declarative": None if y is None else {"points": y["points"], "passed": y["passed"]},
            "legacy_detail": None if (same or x is None) else x.get("detail"),
            "declarative_detail": None if (same or y is None) else y.get("detail")})
        out["total"] += 1
        out["agree"] += int(same)
    for key in ("total", "max", "passed"):
        if skipped or key not in legacy:
            continue
        out["run_level"][key] = {"legacy": legacy.get(key), "declarative": new.get(key),
                                 "agree": legacy.get(key) == new.get(key)}
    return out


def one(arm: str, run: Path, task_dir: Path, spec: Path, manifest: Path | None) -> dict:
    inputs = run_inputs(run)
    env = _env(arm)
    started = time.time()
    legacy, lerr = _run([str(PY), str(task_dir / "tests" / "grade.py"), *_argv(inputs)], env)
    cmd = [str(PY), str(DECLARATIVE), *_argv(inputs), "--spec", str(spec),
           "--task-dir", str(task_dir)]
    if manifest:
        cmd += ["--manifest", str(manifest)]
    new, nerr = _run(cmd, env)
    row = {"arm": arm, "run": str(run), "recorded_total": inputs["recorded_total"],
           "legacy_error": lerr or None, "declarative_error": nerr or None,
           "seconds": round(time.time() - started, 1), **compare(legacy, new)}
    return row


def prestage(arms) -> None:
    """Stage each (task, arm) fixtures tree once, before the pool runs, so
    parallel graders find the stamp current and never restage concurrently."""
    for arm, _base, _glob, task_dir in arms:
        if arm == "default":
            continue
        code = ("import sys; sys.path.insert(0, %r); import taskspec; "
                "taskspec.fixtures_dir(%r, %r)" % (str(REPO / "tasks" / "harness"),
                                                   str(task_dir), arm))
        subprocess.run([str(PY), "-c", code], env=_env(arm), check=False,
                       capture_output=True)


def table(rows: list[dict]) -> str:
    by_arm: dict = {}
    for r in rows:
        slot = by_arm.setdefault(r["arm"], {"runs": 0, "agree": 0, "total": 0,
                                            "runs_all_agree": 0, "errors": 0,
                                            "uncovered": 0})
        slot["runs"] += 1
        slot["uncovered"] += len(r.get("uncovered") or [])
        slot["agree"] += r["agree"]
        slot["total"] += r["total"]
        ok = r["total"] and r["agree"] == r["total"] and all(
            v["agree"] for v in r["run_level"].values())
        slot["runs_all_agree"] += int(bool(ok))
        slot["errors"] += int(bool(r["legacy_error"] or r["declarative_error"]))
    lines = [f"{'arm':<24} {'runs':>4} {'cells agree':>14} {'runs exact':>10} "
             f"{'errors':>6} {'uncovered':>9}"]
    for arm, s in by_arm.items():
        lines.append(f"{arm:<24} {s['runs']:>4} {s['agree']:>6}/{s['total']:<7} "
                     f"{s['runs_all_agree']:>10} {s['errors']:>6} {s['uncovered']:>9}")
    agree = sum(s["agree"] for s in by_arm.values())
    total = sum(s["total"] for s in by_arm.values())
    lines.append(f"{'ALL':<24} {sum(s['runs'] for s in by_arm.values()):>4} "
                 f"{agree:>6}/{total:<7}")
    for r in rows:
        for c in r["cells"]:
            if not c["agree"]:
                lines.append(f"  DISAGREE {r['arm']}/{Path(r['run']).name} {c['id']}: "
                             f"legacy={c['legacy']} declarative={c['declarative']}")
        for key, v in r["run_level"].items():
            if not v["agree"]:
                lines.append(f"  DISAGREE {r['arm']}/{Path(r['run']).name} {key}: "
                             f"legacy={v['legacy']} declarative={v['declarative']}")
        for key in ("legacy_error", "declarative_error"):
            if r[key]:
                lines.append(f"  ERROR {r['arm']}/{Path(r['run']).name} {key}: {r[key][:600]}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("suite", choices=sorted(SUITES) + ["custom"])
    ap.add_argument("runs", nargs="*")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--out")
    ap.add_argument("--arms", nargs="*")
    ap.add_argument("--task-dir")
    ap.add_argument("--spec")
    ap.add_argument("--manifest")
    ap.add_argument("--scratch", help="root for per-arm TMPDIRs and the default --out")
    a = ap.parse_args(argv)
    if a.scratch:
        global OUT_ROOT
        OUT_ROOT = Path(a.scratch)

    jobs = []
    if a.suite == "custom":
        task_dir = Path(a.task_dir).resolve()
        for run in a.runs:
            jobs.append(("custom", Path(run), task_dir))
        spec = Path(a.spec)
        arms = []
    else:
        suite = SUITES[a.suite]
        spec = Path(a.spec) if a.spec else suite["spec"]
        arms = [arm for arm in suite["arms"] if not a.arms or arm[0] in a.arms]
        for arm, base, pattern, task_dir in arms:
            for run in sorted(base.glob(pattern)):
                if (run / "rollout.json").is_file():
                    jobs.append((arm, run, task_dir))
    prestage(arms)
    manifest = Path(a.manifest) if a.manifest else None
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        rows = list(pool.map(lambda j: one(j[0], j[1], j[2], spec, manifest), jobs))
    text = table(rows)
    print(text)
    out = Path(a.out) if a.out else OUT_ROOT / (
        "equivalence.json" if a.suite == "gt009" else f"equivalence_{a.suite}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"suite": a.suite, "spec": str(spec),
                               "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                              time.gmtime()),
                               "summary": text, "runs": rows}, indent=2, default=str),
                   encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
