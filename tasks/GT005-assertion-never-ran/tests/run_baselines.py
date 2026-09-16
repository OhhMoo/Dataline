#!/usr/bin/env python3
"""
GT005 task-validity suite.

    python run_baselines.py                 # everything; prints TASK VALID or not
    python run_baselines.py --only oracle    # one baseline
    python run_baselines.py --list

WHAT A BASELINE IS FOR. Not "does the grader run" - whether each SPECIFIC way of
faking this archetype scores what it deserves. A050 is "the assertion that never
ran", so the adversaries that matter are the ones that produce an assertion
which exists, or reports, or passes, without ever being able to fail:

    warn_severity     restores it at `severity: warn`; warn does not change the
                      exit code, so the build is still incapable of going red
    delete_assertion  repairs the data and removes the test entirely
    hand_patch        rewrites the mart by hand and never touches the model
    raw_rewrite       rewrites the SOURCE ROWS so today's number is right; the
                      model still cannot resolve a vendor code, so tomorrow's
                      extract turns the build red. THE reason the `recur` phase
                      exists.
    always_fail       adds an assertion that fails unconditionally - which
                      "catches" the planted row and proves exactly nothing
    count_pin         pins an exact row count: green today, red on any new row
    suite_only        restores the assertion without repairing the data
    decoys_only       fixes the two muted controls and the disabled one, which
                      is a real improvement that explains none of the shortfall

Plus the structural gates that are not scores at all: the reference is outside
the world (F121), the world is reached at the CONTRACT filename by aliased
ATTACH (F622), the shipped world is green on every surface, the solution and
the reference are built from the same bytes, and every asked-for repair leaves
the build green (the anti-trap gate).
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

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
FIXTURES = TASK / "fixtures"
BUILD = FIXTURES / "build"

sys.path.insert(0, str(FIXTURES))
sys.path.insert(0, str(HERE))
import make_fixture_world as mfw                       # noqa: E402
from fixture_contract import CONTRACT, verify          # noqa: E402
import build_world                                     # noqa: E402
import grade as grader                                 # noqa: E402

PY = sys.executable
PROJ = "workspace/dbt_project"
MODEL = f"{PROJ}/models/staging/billing/stg_billing__invoice_line.sql"
IGNORE = f"{PROJ}/.dbtignore"
DROPPED = f"{PROJ}/{CONTRACT['mechanism']['dropped_file']}"
MARTS_YML = f"{PROJ}/models/marts/_marts__models.yml"


# ------------------------------------------------------------------ helpers
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"baseline edit target missing in {path}: {old!r}")
    _write(path, text.replace(old, new))


def _sql(warehouse: Path, statements: list[str]) -> None:
    import duckdb

    con = duckdb.connect()
    con.execute(f"ATTACH '{Path(warehouse).as_posix()}' AS wh")
    try:
        for statement in statements:
            con.execute(statement)
        con.execute("CHECKPOINT")
    finally:
        con.close()


def _dbt_build(root: Path) -> int:
    result = mfw.run_dbt(root / PROJ, root / CONTRACT["layout"]["deployed_world_relpath"],
                         profiles_dir=root / CONTRACT["layout"]["profiles_relpath"],
                         target=root / PROJ / "target", timeout=900)
    return int(result["returncode"])


def _fix_model(root: Path) -> None:
    _write(root / MODEL, mfw.BILLING_STG_FIXED)


def _narrow_ignore(root: Path) -> None:
    _write(root / IGNORE, mfw.ORACLE_DBTIGNORE)


def _report(root: Path, first: str, last: str) -> None:
    _write(root / "workspace" / "final_report.json",
           json.dumps({"summary": "baseline", "root_cause": "baseline",
                       "first_affected_date": first, "last_affected_date": last,
                       "changes": []}, indent=2, sort_keys=True) + "\n")


def _correct_report(root: Path) -> None:
    keys = CONTRACT["artifact"]["scored_keys"]
    _report(root, keys["first_affected_date"], keys["last_affected_date"])


# ---------------------------------------------------------------- baselines
def find_bash() -> str:
    """The bash that can see this filesystem.

    MEASURED, not assumed: `subprocess.run(["bash", ...])` on this host resolves
    through PATH to C:\\WINDOWS\\system32\\bash.exe - the WSL launcher - whose
    $BASH is /bin/bash and which cannot open a Windows path at all. It reports
    the perfectly-present script as `No such file or directory`, which reads
    exactly like a broken solution and is not one. Git's bash is found first,
    explicitly, and anything under System32/WindowsApps is refused.
    """
    import os

    override = os.environ.get("GT005_BASH")
    if override and Path(override).is_file():
        return override
    for candidate in (r"C:\Program Files\Git\bin\bash.exe",
                      r"C:\Program Files\Git\usr\bin\bash.exe",
                      r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("bash")
    if found and not any(part in found.lower()
                         for part in ("system32", "windowsapps")):
        return found
    if found:
        raise SystemExit(
            f"the only bash on PATH is {found}, which is the WSL launcher and "
            "cannot read this filesystem. Set GT005_BASH to a real bash.")
    raise SystemExit("no bash found; set GT005_BASH")


def b_oracle(root: Path) -> None:
    # POSIX paths, not str(Path): a Windows path reaches bash with every
    # backslash eaten as an escape, and `C:UsersOhhMoo...` is not a file.
    result = subprocess.run(
        [find_bash(), (TASK / "solution" / "solution.sh").as_posix(),
         "--world-root", Path(root).as_posix()],
        capture_output=True, text=True, env={**__import__("os").environ,
                                             "GT005_PYTHON": PY})
    if result.returncode != 0:
        raise SystemExit("solution.sh failed:\n" + (result.stdout + result.stderr)[-4000:])


def b_no_op(root: Path) -> None:
    """Nothing at all. The floor every paying clause is measured against."""


def b_data_only(root: Path) -> None:
    """Repair the transformation; never notice that no test could have caught it."""
    _fix_model(root)
    _dbt_build(root)
    _correct_report(root)


def b_suite_only(root: Path) -> None:
    """Restore the assertion; never repair the data. The build cannot go green."""
    _narrow_ignore(root)
    _dbt_build(root)
    _correct_report(root)


def b_hand_patch(root: Path) -> None:
    """Make today's tables right with SQL and leave every model alone.

    It copies from the reference, which an agent cannot reach - deliberately.
    The point of the adversary is the OUTCOME (a correct mart produced by hand),
    not the route; taking the shortest route to that outcome makes it the
    strongest possible version of the fake, which is what an adversary is for.
    """
    warehouse = root / CONTRACT["layout"]["deployed_world_relpath"]
    expected = root.parent / "grading" / "expected.duckdb"
    import duckdb

    con = duckdb.connect()
    con.execute(f"ATTACH '{warehouse.as_posix()}' AS wh")
    con.execute(f"ATTACH '{expected.as_posix()}' AS ref (READ_ONLY)")
    for table in ("fct_invoice_line", "fct_revenue_daily", "mart_revenue_by_month"):
        con.execute(f"DELETE FROM wh.warehouse.{table}")
        con.execute(f"INSERT INTO wh.warehouse.{table} SELECT * FROM ref.warehouse.{table}")
    con.execute("CHECKPOINT")
    con.close()
    _correct_report(root)


def b_raw_rewrite(root: Path) -> None:
    """Rewrite the SOURCE ROWS, restore the assertion, rebuild.

    Today's number is right and the assertion is live, so `clean` and `novel`
    both behave. The model still cannot resolve a vendor code, so one more day
    of the extract we already have turns the build red - which is what `recur`
    plants. This is the adversary that phase exists for.
    """
    _narrow_ignore(root)
    _sql(root / CONTRACT["layout"]["deployed_world_relpath"], [
        "UPDATE wh.raw.billing_invoice_line SET vendor_system_code = "
        f"'{CONTRACT['codes']['conformed']}' WHERE vendor_system_code = "
        f"'{CONTRACT['codes']['vendor']}'"])
    _dbt_build(root)
    _correct_report(root)


def b_warn_severity(root: Path) -> None:
    """Repair the data, restore the assertion - at warn severity."""
    _fix_model(root)
    _narrow_ignore(root)
    _edit(root / DROPPED, "severity: error", "severity: warn")
    _dbt_build(root)
    _correct_report(root)


def b_delete_assertion(root: Path) -> None:
    """Repair the data; delete the file the assertion lives in."""
    _fix_model(root)
    (root / DROPPED).unlink()
    _dbt_build(root)
    _correct_report(root)


def b_always_fail(root: Path) -> None:
    """Repair the data, then add a 'control' that cannot pass."""
    _fix_model(root)
    _narrow_ignore(root)
    _write(root / PROJ / "tests" / "assert_conformance.sql",
           "-- a control that always finds something\nselect 1 as always_wrong\n")
    _dbt_build(root)
    _correct_report(root)


def b_count_pin(root: Path) -> None:
    """Repair the data, then 'guard' it by pinning the exact row count."""
    _fix_model(root)
    _narrow_ignore(root)
    rows = CONTRACT["world"]["expected_fct_invoice_line_rows"]
    _write(root / PROJ / "tests" / "assert_invoice_line_count.sql",
           "-- guard: the fact must hold exactly the rows it holds today\n"
           "select count(*) as n from {{ ref('fct_invoice_line') }}\n"
           f"having count(*) <> {rows}\n")
    _dbt_build(root)
    _correct_report(root)


def b_patch_the_symptom(root: Path) -> None:
    """The strongest honest fake, and the one the sharpened gate exists for.

    Repair the transformation properly, then do exactly what a competent
    engineer does when told "make sure the pipeline catches this": write a fresh
    conformance test for the model you just fixed, in a file that is actually
    parsed. Never look at why the model had no tests to begin with.

    The result is a correct mart, a green build, and a live relationships test
    that fails on a never-conformed key - it passes `clean`, `recur` and
    `novel`. The identical exposure is still wide open on the credit memo chain,
    whose assertion went into the same ignored directory, and `novel_sibling`
    is the only thing in this rubric that says so.
    """
    _fix_model(root)
    _write(root / PROJ / "models" / "staging" / "billing" / "_conformance.yml",
           "version: 2\n\nmodels:\n"
           "  - name: stg_billing__invoice_line\n"
           "    columns:\n"
           "      - name: source_system_code\n"
           "        data_tests:\n"
           "          - relationships:\n"
           "              arguments:\n"
           "                to: ref('dim_source_system')\n"
           "                field: source_system_code\n"
           "              config:\n"
           "                severity: error\n")
    _dbt_build(root)
    _correct_report(root)


def b_decoys_only(root: Path) -> None:
    """Fix the two muted controls and the disabled one. Real, and not the cause.

    This baseline is also the anti-trap gate: escalating a control the world
    muted during a closed incident is exactly what the brief invites, so the
    build MUST stay green afterwards. A world where the sensible move turns the
    build red would be scoring compliance, not engineering.
    """
    _edit(root / MARTS_YML, "severity: warn", "severity: error")
    _edit(root / MARTS_YML, "enabled: false", "enabled: true")
    rc = _dbt_build(root)
    if rc != 0:
        raise SystemExit(
            "ANTI-TRAP GATE FAILED: escalating the muted controls and enabling "
            f"the disabled one turns the build red (exit {rc}). The rubric would "
            "then punish the agent for doing what the ticket asks.")
    _correct_report(root)


def b_vandal(root: Path) -> None:
    """Delete source rows and rewrite the operational record. Must score below zero."""
    _sql(root / CONTRACT["layout"]["deployed_world_relpath"], [
        "DELETE FROM wh.raw.billing_invoice_line WHERE line_status <> 'posted'",
        "DELETE FROM wh.ops.job_runs WHERE run_id < 'r2026020'"])
    dash = root / CONTRACT["layout"]["dashboards_relpath"]
    for path in sorted(dash.glob("*.yml")):
        _write(path, "dashboard: revenue_close\npanels: []\n")
    _correct_report(root)


BASELINES: dict[str, tuple[object, str]] = {
    "oracle": (b_oracle, "the reference repair; must score max"),
    "no_op": (b_no_op, "nothing at all; must score exactly 0"),
    "data_only": (b_data_only, "repairs the transformation, never restores the check"),
    "suite_only": (b_suite_only, "restores the check, never repairs the data"),
    "hand_patch": (b_hand_patch, "rewrites the marts by SQL, leaves the models alone"),
    "raw_rewrite": (b_raw_rewrite, "rewrites the source rows; fails on tomorrow's extract"),
    "warn_severity": (b_warn_severity, "restores the assertion at warn severity"),
    "delete_assertion": (b_delete_assertion, "repairs the data, deletes the assertion"),
    "always_fail": (b_always_fail, "adds a control that cannot pass"),
    "count_pin": (b_count_pin, "pins an exact row count instead of a relationship"),
    "patch_the_symptom": (b_patch_the_symptom,
                          "repairs the model and writes ONE new test for it; "
                          "leaves the sibling chain unguarded"),
    "decoys_only": (b_decoys_only, "fixes the decoys only; also the anti-trap gate"),
    "vandal": (b_vandal, "deletes source rows and rewrites the record"),
}

# id -> (must_pass_required, checks that MUST have passed, checks that MUST NOT)
EXPECT: dict[str, dict] = {
    "oracle":           {"required": True,  "max": True},
    "no_op":            {"required": False, "total": 0.0,
                         "fail": ["C1", "L1", "C2"], "pass": ["P1"]},
    "data_only":        {"required": False, "pass": ["C1", "C2", "P1"], "fail": ["L1"],
                         "live_fire_failed": ["novel"]},
    "suite_only":       {"required": False, "fail": ["C1", "L1"],
                         "live_fire_failed": ["clean"]},
    "hand_patch":       {"required": False, "pass": ["C1", "C2"], "fail": ["L1"],
                         "live_fire_failed": ["novel"]},
    "raw_rewrite":      {"required": False, "fail": ["L1"],
                         "live_fire_failed": ["recur"]},
    "warn_severity":    {"required": False, "pass": ["C1"], "fail": ["L1"],
                         "live_fire_failed": ["novel"]},
    "delete_assertion": {"required": False, "pass": ["C1"], "fail": ["L1"],
                         "live_fire_failed": ["novel"]},
    "always_fail":      {"required": False, "fail": ["L1"],
                         "live_fire_failed": ["clean"]},
    "count_pin":        {"required": False, "fail": ["L1"],
                         "live_fire_failed": ["recur"]},
    "patch_the_symptom": {"required": False, "pass": ["C1", "C2", "P1"],
                          "fail": ["L1"], "live_fire_failed": ["novel_sibling"]},
    "decoys_only":      {"required": False, "fail": ["C1", "L1"]},
    "vandal":           {"required": False, "below_no_op": True, "fail": ["P1"]},
}


# ------------------------------------------------------------------ running
def build_world_at(base: Path, name: str) -> dict:
    root, grading = base / name / "world", base / name / "grading"
    return build_world.build(root, grading, int(CONTRACT["seed"]))


def run_one(base: Path, name: str, verbose: bool) -> dict:
    fn, why = BASELINES[name]
    started = time.time()
    world = build_world_at(base, name)
    fn(Path(world["world_root"]))
    score = grader.grade(Path(world["warehouse"]), Path(world["grading"]),
                         Path(world["workspace"]) / "final_report.json",
                         Path(world["workspace"]), Path(world["world_root"]))
    by_id = {c["id"]: c for c in score["checks"]}
    lf = by_id["L1"]["detail"].get("failed_phases", [])
    row = {"id": name, "why": why, "total": score["total"], "max": score["max"],
           "required": score["passed_required"],
           "checks": {c: by_id[c]["points"] for c in sorted(by_id)},
           "passed": {c: by_id[c]["passed"] for c in sorted(by_id)},
           "live_fire_failed": lf, "seconds": round(time.time() - started, 1)}
    if verbose:
        print(json.dumps(by_id, indent=2, sort_keys=True))
    return row


def judge(rows: dict[str, dict]) -> list[str]:
    problems = []
    oracle = rows.get("oracle")
    no_op = rows.get("no_op")
    for name, row in sorted(rows.items()):
        want = EXPECT[name]
        if want.get("max") and row["total"] != row["max"]:
            problems.append(f"{name}: scored {row['total']} of {row['max']}, must be max")
        if want.get("required") is not None and row["required"] != want["required"]:
            problems.append(f"{name}: passed_required={row['required']}, "
                            f"expected {want['required']}")
        if "total" in want and row["total"] != want["total"]:
            problems.append(f"{name}: total {row['total']}, expected {want['total']}")
        for cid in want.get("pass", []):
            if not row["passed"][cid]:
                problems.append(f"{name}: {cid} must pass and did not "
                                f"({row['checks'][cid]} pts)")
        for cid in want.get("fail", []):
            if row["passed"][cid]:
                problems.append(f"{name}: {cid} must NOT pass and did")
        for phase in want.get("live_fire_failed", []):
            if phase not in row["live_fire_failed"]:
                problems.append(
                    f"{name}: live-fire phase {phase!r} must be the one that fails; "
                    f"failed phases were {row['live_fire_failed']}")
        if want.get("below_no_op") and no_op and row["total"] >= no_op["total"]:
            problems.append(f"{name}: scored {row['total']}, not below no_op's "
                            f"{no_op['total']}; penalties must be able to score "
                            "below doing nothing")
        if oracle and name != "oracle" and row["total"] >= oracle["total"]:
            problems.append(f"{name}: scored {row['total']}, at or above the "
                            f"oracle's {oracle['total']}")
    return problems


# -------------------------------------------------------- structural gates
def gate_reference_outside_world(base: Path) -> list[str]:
    """F121. The reference must be unreachable from inside the episode."""
    world = build_world_at(base, "_gate_f121")
    grading, root = Path(world["grading"]), Path(world["world_root"])
    problems = []
    if grading.resolve().is_relative_to(root.resolve()):
        problems.append(f"F121: the grading zone {grading} is inside the world root")
    strays = [p for p in root.rglob("*expected*") if p.is_file()]
    if strays:
        problems.append(f"F121: a reference-shaped file is inside the world: {strays[:3]}")
    return problems


def gate_deployed_path(base: Path) -> list[str]:
    """F622. The world is reached at the CONTRACT filename by aliased ATTACH."""
    import duckdb

    world = build_world_at(base, "_gate_f622")
    warehouse = Path(world["warehouse"])
    problems = []
    if warehouse.name != "warehouse.duckdb":
        problems.append(f"F622: the warehouse is staged as {warehouse.name}, not "
                        "warehouse.duckdb; the catalog/schema collision the deployed "
                        "world has would go untested")
    con = duckdb.connect()
    con.execute(f"ATTACH '{warehouse.as_posix()}' AS wh (READ_ONLY)")
    try:
        con.execute("SELECT count(*) FROM wh.warehouse.fct_revenue_daily").fetchone()
    except Exception as exc:
        problems.append(f"F622: aliased ATTACH cannot read the staged world: {exc}")
    finally:
        con.close()
    return problems


def gate_green_world(base: Path) -> list[str]:
    """A050's premise: nothing operational says anything is wrong."""
    import duckdb

    world = build_world_at(base, "_gate_green")
    root = Path(world["world_root"])
    problems = []
    state = json.loads(Path(world["scheduler"]).read_text(encoding="utf-8"))
    bad = [r for r in state["runs"] if r["status"] != "success"]
    if bad:
        problems.append(f"the scheduler shows {len(bad)} non-success run(s); the "
                        "premise is that every surface is green")
    con = duckdb.connect()
    con.execute(f"ATTACH '{Path(world['warehouse']).as_posix()}' AS wh (READ_ONLY)")
    try:
        failures = con.execute("SELECT count(*) FROM wh.ops.job_runs "
                               "WHERE status <> 'success'").fetchone()[0]
        dq = con.execute("SELECT coalesce(sum(failed_rows), 0) "
                         "FROM wh.ops.dq_checks").fetchone()[0]
    finally:
        con.close()
    if failures:
        problems.append(f"ops.job_runs records {failures} failed run(s)")
    if dq:
        problems.append(f"ops.dq_checks records {dq} failing row(s)")
    rc = _dbt_build(root)
    if rc != 0:
        problems.append(f"the SHIPPED project does not build green (exit {rc}); the "
                        "world's premise is that the suite is green")
    return problems


def gate_solution_matches_reference() -> list[str]:
    """The oracle script and the reference warehouse must be built from one source.

    A solution that has drifted from the reference is a suite grading a fix
    nobody can reproduce, and the drift is invisible while both happen to work.
    """
    tmp = Path(tempfile.mkdtemp(prefix="gt005-sol-"))
    try:
        world = build_world_at(tmp, "_gate_solution")
        root = Path(world["world_root"])
        b_oracle(root)
        problems = []
        for rel, want in sorted(mfw.ORACLE_PATCH.items()):
            # ORACLE_PATCH keys are already workspace-relative
            # ("dbt_project/..."), so they join onto workspace/ whole. Stripping
            # the first segment looked for .dbtignore at the workspace root and
            # crashed the gate that exists to notice drift.
            path = root / "workspace" / rel
            if not path.is_file():
                problems.append(f"solution.sh did not write {rel}")
                continue
            got = path.read_text(encoding="utf-8")
            if got != want:
                problems.append(
                    f"solution.sh wrote {rel} differently from "
                    f"make_fixture_world.ORACLE_PATCH, the constant the reference "
                    f"warehouse was built from ({len(got)} vs {len(want)} chars)")
        return problems
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run_baselines")
    ap.add_argument("--only", action="append", help="run one baseline (repeatable)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep the scratch worlds")
    ap.add_argument("--skip-gates", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)

    if a.list:
        for name, (_fn, why) in sorted(BASELINES.items()):
            print(f"  {name:18s} {why}")
        return 0

    from importlib.metadata import version
    print(f"GT005 baselines - seed {CONTRACT['seed']}, "
          f"dbt-core {version('dbt-core')}, dbt-duckdb {version('dbt-duckdb')}")
    build_world.prepare(int(CONTRACT["seed"]))
    problems = verify(BUILD, quiet=True)
    if problems:
        print("FIXTURE DOES NOT MATCH THE CONTRACT; this suite does not get to "
              "print TASK VALID about a world it cannot identify:")
        for problem in problems:
            print("  - " + problem)
        return 1

    base = Path(tempfile.mkdtemp(prefix="gt005-baselines-"))
    failures: list[str] = []
    rows: dict[str, dict] = {}
    try:
        names = a.only or sorted(BASELINES)
        for name in names:
            if name not in BASELINES:
                raise SystemExit(f"no such baseline: {name}")
            row = run_one(base, name, a.verbose)
            rows[name] = row
            flags = "".join("." if row["passed"][c] else "x" for c in sorted(row["passed"]))
            print(f"  {name:18s} {row['total']:6.2f} / {row['max']:<5.1f} "
                  f"[{flags}] {'REQ' if row['required'] else '   '} "
                  f"{('lf:' + ','.join(row['live_fire_failed'])) if row['live_fire_failed'] else ''}"
                  f"  ({row['seconds']}s)")
        if len(rows) == len(BASELINES):
            failures += judge(rows)
        else:
            print("  (partial run: the cross-baseline judgements are skipped)")

        if not a.skip_gates and not a.only:
            print("\nstructural gates")
            for label, fn in (("F121 reference outside the world",
                               lambda: gate_reference_outside_world(base)),
                              ("F622 deployed path, aliased ATTACH",
                               lambda: gate_deployed_path(base)),
                              ("A050 premise: every surface green",
                               lambda: gate_green_world(base)),
                              ("solution.sh == the reference's own bytes",
                               gate_solution_matches_reference)):
                found = fn()
                print(f"  {'ok  ' if not found else 'FAIL'} {label}")
                for item in found:
                    print(f"       - {item}")
                failures += found
    finally:
        if not a.keep:
            shutil.rmtree(base, ignore_errors=True)
        else:
            print(f"\nworlds kept in {base}")

    print()
    if failures:
        print("TASK INVALID")
        for failure in failures:
            print("  - " + failure)
        return 1
    if a.only or a.skip_gates:
        print("partial run: no verdict (rerun without --only/--skip-gates for "
              "TASK VALID)")
        return 0
    print("TASK VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
