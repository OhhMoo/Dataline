#!/usr/bin/env python3
"""Live proof that the surface gate FIRES, and that it stays quiet when it must.

    python surface_gate_check.py

A gate nobody has watched fire proves nothing. This repo has already shipped an
acceptance gate that aborted before it ran, and two determinism defects rode
through on exactly that, so this check drives the real `rollout.py` end to end -
real world builder, real gateway, real probes, real classifier, real report -
against two fixture task packages that differ in ONE line of task.toml:

    PROOF-dead-required   required_surfaces = [a surface this world is MISSING]
                          - must quarantine before the subject is handed the
                            terminal: harness_error, never scored, never graded.
    PROOF-live-required   required_surfaces = [surfaces this world HAS], every
                          dead one declared dead_by_design
                          - must run, and reach its verdict through the ordinary
                            detector pipeline with the gate contributing nothing.

Which surfaces are live and which are dead is MEASURED off the base world first
(`_live_and_dead`), never hardcoded, so this check can never quietly decay into
"require something that happens to be live".

Both packages are copies of a real task's, made outside the repo, because the
gate must be shown working on a world that actually exists rather than on a
mock. `--driver scripted` means no model quota is spent: the subject is
`redteam/scripted_subject.py`. That subject is a DETECTOR FIXTURE - it reaches
for the grading zone on purpose - so the quiet half ends in
`blindness_violation`, and it must: what is being shown there is that the gate
left the rollout alone, not that a scripted fixture can solve GT001.

Not a measurement. The rollouts it produces are fixtures and are thrown away.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import episode      # noqa: E402
import report       # noqa: E402
import surface_audit  # noqa: E402
import taskspec     # noqa: E402

HERE = Path(__file__).resolve().parent
TASKS = HERE.parent
BASE_TASK = "GT001-aov-halving"

OK: list[str] = []
BAD: list[str] = []


def check(label, cond, detail=""):
    (OK if cond else BAD).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


# ------------------------------------------------------------------ fixtures
def _fixture(tasks_dir: Path, task_id: str, block: str) -> None:
    """A copy of the base task package with one extra [harness] block."""
    dst = tasks_dir / task_id
    if not dst.exists():
        shutil.copytree(TASKS / BASE_TASK, dst,
                        ignore=shutil.ignore_patterns("__pycache__", ".git"))
    toml = dst / "task.toml"
    text = toml.read_text(encoding="utf-8")
    marker = "\n# --- surface_gate_check fixture ---\n"
    if marker in text:
        text = text.split(marker)[0]
    toml.write_text(text + marker + block + "\n", encoding="utf-8")


def _live_and_dead(tasks_dir: Path,
                   scratch: Path) -> tuple[list[str], list[str], list[str]]:
    """Which surfaces is this world ACTUALLY missing? Measured, never assumed.

    The fixture's required_surfaces are chosen from this, so the check can never
    quietly become 'require something that happens to be live'.
    """
    spec = taskspec.load(BASE_TASK)
    episode.prepare_task(spec)
    world = episode.build_world(spec, scratch / "world", scratch / "grading")
    toolbelt = episode.gateway_tool_names()
    result = episode.probe_surfaces(
        spec, world, scratch / "probe",
        surface_audit.all_probes(toolbelt, spec.surface_probes))
    audit = surface_audit.audit(spec=spec, surfaces=result, toolbelt=toolbelt)
    declared_live = [line for line in audit["live"] if not line.startswith("~")]
    declared_dead = [line for line in audit["dead"] if not line.startswith("~")]
    swept_dead = [line for line in audit["dead"] if line.startswith("~")]
    print(f"\n  the base world, measured: {len(declared_live)} declared surfaces "
          f"live, {len(declared_dead)} declared dead {declared_dead}")
    print(f"  ... and {len(swept_dead)} DEAD surfaces the task never probed, "
          f"found by the sweep: {swept_dead}")
    shutil.rmtree(scratch, ignore_errors=True)
    return declared_live, declared_dead, audit["dead"]


def _run(task_id: str, tasks_dir: Path, out: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(HERE / "rollout.py"), "--task", task_id,
         "--tasks-dir", str(tasks_dir), "--driver", "scripted",
         "--rollout-id", "r0", "--run-id", "surface-gate-check",
         "--out", str(out)],
        capture_output=True, text=True)
    rec_path = out / "rollout.json"
    if not rec_path.exists():
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:])
        raise SystemExit(f"{task_id}: rollout wrote no record (exit {proc.returncode})")
    return json.loads(rec_path.read_text(encoding="utf-8"))


# ------------------------------------------------- the plane-path regression
# The defect the gate was built for, kept firing at. GT004's eight dead
# surfaces were not a world gap at all: `GatewayConfig`'s defaults for
# workflow_dir / observability / ingest are ABSOLUTE (`/var/workflow`,
# `/var/obs/observability.yml`, `/var/ingest/state.json` -
# gateway/src/alkera_gateway/config.py:19-23), they resolve outside any world
# root, and the harness's config writers never named them. So issues_*, chat_*,
# kb_*, metrics_*, alarms_*, deploy_* and ingest_* answered "this world has no
# ..." however richly the world was seeded, and five Opus rollouts were scored
# in a world that was never half-absent - only half-addressed.
#
# This drives the real gateway over a world that DOES seed all three planes, with
# and without those config keys, so the day someone drops one the check says so
# instead of the number quietly becoming meaningless again.
PLANE_TOOLS = ("issues_list", "chat_channels", "kb_list", "metrics_list",
               "alarms_list", "deploy_list", "ingest_list")


def _seeded_world(root: Path) -> dict:
    import duckdb
    w = root / "world"
    for d in ("var/workflow", "var/obs", "var/ingest", "workspace",
              "var/warehouse", "var/sched", "var/bi/dashboards", "build"):
        (w / d).mkdir(parents=True, exist_ok=True)
    # minimal but VALID stores: each one's `required` keys and nothing else, so
    # this check cannot break when a record's schema grows a field
    (w / "var/workflow/issues.json").write_text(
        json.dumps({"issues": [], "statuses": ["open"]}), encoding="utf-8")
    (w / "var/workflow/chat.json").write_text(
        json.dumps({"channels": []}), encoding="utf-8")
    (w / "var/workflow/kb.json").write_text(
        json.dumps({"pages": []}), encoding="utf-8")
    (w / "var/obs/definitions.yaml").write_text(
        "metrics: []\nalarms: []\ndeploys: []\n", encoding="utf-8")
    (w / "var/ingest/state.json").write_text(
        json.dumps({"connectors": {}, "runs": [], "schemas": [], "alerts": []}),
        encoding="utf-8")
    (w / "var/sched/state.json").write_text('{"jobs": [], "runs": []}',
                                            encoding="utf-8")
    duckdb.connect(str(w / "var/warehouse/warehouse.duckdb")).close()
    return {
        "world_root": str(w), "workspace": str(w / "workspace"),
        "warehouse": str(w / "var/warehouse/warehouse.duckdb"),
        "scheduler": str(w / "var/sched/state.json"),
        "dashboards": str(w / "var/bi/dashboards"),
        "profiles_dir": str(w / "build"),
        # the three the config writers used to drop
        "workflow_dir": str(w / "var/workflow"),
        # deliberately observability.yml, which does not exist: the gateway
        # falls back to definitions.yaml beside it
        # (gateway/src/alkera_gateway/observability.py:518-531)
        "observability": str(w / "var/obs/observability.yml"),
        "ingest": str(w / "var/ingest/state.json"),
    }


def _ask(cfg: dict) -> dict:
    from alkera_gateway.config import GatewayConfig
    from alkera_gateway.core import Gateway
    gateway = Gateway(GatewayConfig.model_validate(cfg))
    try:
        out = {}
        for tool in PLANE_TOOLS:
            res = gateway.call(tool, {}, "probe")
            out[tool] = "LIVE" if res.get("ok") else (res.get("error") or {}).get("code")
        return out
    finally:
        gateway.close()


def test_plane_paths(root: Path) -> None:
    print("\n  --- the plane paths: why GT004's eight surfaces were 'dead' ---")
    sys.path.insert(0, str(HERE.parents[1] / "world"))   # world.fingerprint
    world = _seeded_world(root)
    base = {k: world[k] for k in ("workspace", "warehouse", "scheduler",
                                  "dashboards", "profiles_dir", "world_root")}
    base |= {"episode_id": "plane-check", "virtual_time": "2026-03-08T09:00:00Z"}

    before = _ask({**base, "audit_dir": str(root / "audit_before")})
    after = _ask({**base, "audit_dir": str(root / "audit_after"),
                  **episode._plane_paths(world)})
    print(f"    without the plane keys: {json.dumps(before)}")
    print(f"    with them:              {json.dumps(after)}")
    check("without the plane keys, a FULLY SEEDED world reports every workflow, "
          "observability and ingest surface absent",
          all(v != "LIVE" for v in before.values())
          and {"no_issue_tracker", "no_chat", "no_knowledge_base",
               "no_observability", "no_ingest_state"} <= set(before.values()),
          json.dumps(before))
    check("...and with them, on the SAME bytes, every one of them is live",
          all(v == "LIVE" for v in after.values()), json.dumps(after))
    check("write_gateway_config now emits them",
          set(episode._plane_paths(world)) >= {"workflow_dir", "observability",
                                               "ingest"})
    cfg = episode.write_gateway_config(
        root / "cfg.json", world, root / "audit", "x", "2026-03-08T09:00:00Z")
    written = json.loads(cfg.read_text(encoding="utf-8"))
    check("...into the file it actually writes",
          all(written.get(k) == world[k]
              for k in ("workflow_dir", "observability", "ingest")),
          str(sorted(written)))
    check("...and a world that publishes none of them is left alone, so a "
          "genuinely minimal world still reports its surfaces absent",
          episode._plane_paths({"world_root": "/w"}) == {})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true",
                    help="keep the fixture packages and rollout dirs")
    a = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="alkera-surface-gate-"))
    tasks_dir = root / "tasks"
    tasks_dir.mkdir(parents=True)
    print(f"\n  surface gate check\n  fixtures in {root}")

    test_plane_paths(root / "planes")

    live, dead, all_dead = _live_and_dead(tasks_dir, root / "measure")
    if not dead:
        raise SystemExit("the base world has no dead declared surface, so the "
                         "firing half of this check cannot be constructed")
    if not live:
        raise SystemExit("the base world has no live declared surface")

    _fixture(tasks_dir, "PROOF-dead-required",
             f"required_surfaces = {json.dumps([dead[0]])}")
    # EVERY dead surface - the task's own and the ones the sweep found behind
    # tools it never declared - either required or declared dead by design, so
    # the quiet half is quiet for the right reason rather than by omission.
    by_design = "\n".join(
        f'  {{ label = {json.dumps(d)}, reason = "fixture: the base world does '
        f'not seed this surface, and this half of the check is about the gate '
        f'staying quiet" }},' for d in all_dead)
    _fixture(tasks_dir, "PROOF-live-required",
             f"required_surfaces = {json.dumps(sorted(live)[:3])}\n"
             f"dead_by_design = [\n{by_design}\n]")

    print("\n  --- half 1: a required surface is DEAD ---")
    fired = _run("PROOF-dead-required", tasks_dir, root / "run-dead")
    sa = fired.get("surface_audit") or {}
    check("the gate FIRED: classification is harness_error",
          fired["classification"] == "harness_error", fired["classification"])
    check("...and the rollout is NOT scored", fired["scored"] is False)
    check("...and it did not pass", fired["passed"] is False)
    check("...and the gate named the dead required surface",
          sa.get("gate") == "fail" and sa.get("dead_required") == [dead[0]],
          str(sa.get("dead_required")))
    check("...and the reason reaches the record's `why`",
          "dead required surface" in (fired.get("harness_failure") or "")
          and dead[0] in (fired.get("harness_failure") or ""),
          str(fired.get("harness_failure"))[:160])
    check("...and the SUBJECT WAS NEVER RUN - no agent, no tool calls",
          fired.get("agent") is None and not fired.get("tool_calls"),
          f"agent={fired.get('agent')} calls={fired.get('tool_calls')}")
    check("...and it was never graded", fired.get("score") is None)
    check("...and the classifier flagged the dead required surface",
          any("dead_required_surfaces" in f for f in fired.get("flags", [])),
          str(fired.get("flags")))

    print("\n  --- half 2: every required surface is LIVE ---")
    quiet = _run("PROOF-live-required", tasks_dir, root / "run-live")
    qa = quiet.get("surface_audit") or {}
    check("the gate did NOT fire: it reports pass",
          qa.get("gate") == "pass", f"{qa.get('gate')}: {qa.get('gate_reason')}")
    check("...and found no dead required surface",
          qa.get("dead_required") == [])
    check("...and the rollout ran: the subject was handed the terminal",
          quiet.get("agent") is not None)
    check("...and the rollout went through the ORDINARY pipeline: its verdict "
          "comes from the detectors, not from the gate",
          quiet.get("classification") != "harness_error"
          and not (quiet.get("harness_failure") or ""),
          f"{quiet.get('classification')}: {str(quiet.get('why'))[:120]}")
    # NB: the scripted subject is a detector fixture that deliberately reaches
    # for the grading zone, so this half can never end in `pass`, and asking it
    # to would be asking the wrong question. What it must show is that the gate
    # did not touch the rollout.
    check("...and every dead surface is accounted for - none left undeclared",
          qa.get("dead_undeclared") == [], str(qa.get("dead_undeclared")))
    check("...including the ones only the SWEEP found",
          len(qa.get("dead_by_design") or []) > len(dead),
          f"{len(qa.get('dead_by_design') or [])} dead-by-design, of which "
          f"{len(dead)} were surfaces the task declared a probe for")

    print("\n  --- the report, over each half ---")
    withheld = report.render("PROOF-dead-required", "surface-gate-check",
                             report.aggregate([fired]), [fired])
    clean = report.render("PROOF-live-required", "surface-gate-check",
                          report.aggregate([quiet]), [quiet])
    check("the report WITHHOLDS Pass@1 for the run with a dead required surface",
          "Pass@1 = WITHHELD" in withheld)
    check("...with the run conditions stated ABOVE the number, not under it",
          withheld.index("## Run conditions") < withheld.index("Pass@1 = WITHHELD")
          < withheld.index("## Failure classification"))
    check("...and names the dead required surface in that block",
          f"required surfaces dead: {[dead[0]]}" in withheld)
    # The clean half cannot publish a rate either - the scripted driver stamps
    # every rollout `redteam_detector_test`, which withholds on its own and
    # should. What it must show is that the SURFACES are no longer a reason.
    check("the clean run is withheld for NO surface reason",
          not [x for x in report.aggregate([quiet])["invalidated_because"]
               if "surface" in x],
          str(report.aggregate([quiet])["invalidated_because"]))
    check("...while the dead-required run is",
          any("required surfaces were DEAD" in x
              for x in report.aggregate([fired])["invalidated_because"]))
    check("both reports state the live-surface set beside every rate they print",
          all("surfaces: " in line for r in (withheld, clean)
              for line in r.splitlines()
              if "Pass@1 = " in line or "Pass^k = " in line))
    check("both reports carry Pass^k",
          "Pass^k = " in withheld and "Pass^k = " in clean)
    print("\n" + "\n".join("    " + line for line in clean.splitlines()[:22]))

    if not a.keep:
        shutil.rmtree(root, ignore_errors=True)
    else:
        print(f"\n  kept: {root}")
    print(f"\n  {len(OK)} passed, {len(BAD)} failed")
    if BAD:
        print("  FAILED: " + ", ".join(BAD))
    return 1 if BAD else 0


if __name__ == "__main__":
    started = time.time()
    code = main()
    print(f"  {round(time.time() - started, 1)}s")
    raise SystemExit(code)
