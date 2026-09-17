#!/usr/bin/env python3
"""`rollout.py --regrade`, executed. No model, no quota.

    python test_regrade.py                       # everything
    python test_regrade.py --against <rollout>   # diff a named recorded rollout
    python test_regrade.py --no-recorded         # skip the recorded-run diff

Three questions, and they are the three a regrade can get wrong.

1. DOES IT REFUSE what it cannot describe? A regrade that quietly works around
   a missing transcript, a missing world or a missing artefact produces a number
   for an episode nobody can account for, which is worse than no number. Every
   input is removed in turn from a real completed rollout and the refusal has to
   name the one that went.

2. DOES IT SAY WHAT IT COULD NOT RE-RUN? The before-episode state is only
   recoverable if the run wrote it down. `before_episode.json` is what makes a
   regrade COMPLETE; without it the world-diff stage cannot be re-run, and the
   record has to say so - `regrade_partial`, the piece named, the consequence
   spelled out - rather than filling the gap with the world as it stands now,
   which is the world the episode already changed.

3. DOES IT REPRODUCE THE VERDICT? Re-grading a rollout that already has a record
   must produce the same record: same score, same checks, same blindness, same
   classification. Anything else is a silent re-write of a measurement.

The fixture is a REAL rollout - `rollout.py --driver scripted` over GT001, which
runs the real world builder, the real gateway, the real probe sweep and the real
grader, and costs no model quota. The recorded-run diff (3) runs against a run
that actually happened on this machine, because that is the case the feature was
built for; it is skipped, loudly, when there is none.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rollout                      # noqa: E402
import run_rollouts                 # noqa: E402
import taskspec                     # noqa: E402

HERE = Path(__file__).resolve().parent
PASSED = 0
FAILED = 0
SKIPPED: list[str] = []
FIXTURE_TASK = "GT001-aov-halving"
#: Where recorded runs live (run_rollouts.DEFAULT_OUT). Read-only here: the
#: recorded-run diff always works on a COPY, because a run that has already been
#: measured is evidence and this test is not allowed to touch it.
RECORDED = run_rollouts.DEFAULT_OUT

#: The fields a regrade is SUPPOSED to add. Everything else it writes must
#: either match the record it re-graded or be a path naming the directory being
#: re-graded - see `_classify_diff`.
REGRADE_FIELDS = ("regraded_from", "regraded_at", "regrade_grader_sha256",
                  "regrade_partial", "regrade")

#: The verdict. These are the fields a re-grade exists to reproduce, and the
#: ones a reviewer reads; they are asserted equal, not merely explained.
VERDICT_FIELDS = ("classification", "passed", "scored", "why", "tool_calls",
                  "flags", "blindness", "checks", "needs_review")

#: Fields a regrade RE-DERIVES from the task's declarations and the harness's
#: current rules rather than copying forward - the same two `--reclassify`
#: re-derives, and for its reason: a stale verdict is worse than no verdict. A
#: task that has changed its `dead_by_design` since the run, or a guarantee rule
#: that has since demanded a proof the record does not carry, moves here. They
#: are the only values other than paths that a faithful regrade may change, and
#: each one that does is printed with the before and after.
REDERIVED_FIELDS = ("surface_audit", "blindness_guarantee")


def check(name: str, condition: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}" + (f"   {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}   {detail}")
    return bool(condition)


def skip(name: str, why: str) -> None:
    SKIPPED.append(f"{name}: {why}")
    print(f"  [SKIP] {name}   {why}")


def section(title: str) -> None:
    print(f"\n{title}")


def _missing_named(msg: str) -> list[str]:
    """The inputs a "not a completed episode" refusal named, in order."""
    if "Missing: " not in msg:
        return []
    body = msg.split("Missing: ", 1)[1].split(". --regrade", 1)[0]
    return [part.split(" - ", 1)[0].strip() for part in body.split("; ")]


def refuses(path: Path, **kw) -> str:
    """The refusal message, or "" when the regrade did not refuse."""
    try:
        rollout.regrade(path, **kw)
    except rollout.RegradeRefused as exc:
        return str(exc)
    return ""


# ------------------------------------------------------------------ fixture
ARTIFACT = json.dumps({
    "primary_cause": "a test fixture, not a finding",
    "damage_window": ["2026-01-01", "2026-01-02"],
    "supporting_evidence": [], "repair_strategy": "none",
    "remaining_risks": [], "additional_causes": []})


def build_fixture(tmp: Path, task: str) -> Path:
    """One real completed rollout, in the `<task>/<run-id>/rN` layout.

    `--driver scripted` runs `redteam/scripted_subject.py` rather than a model:
    a real process through the real capture path, which is what makes this a
    test of the harness and not of a reconstruction of it. The layout matters -
    a regrade with no record at all recovers the task id from it - so the
    fixture is built where a run would be.
    """
    out = tmp / task / "regrade-test" / "r0"
    proc = subprocess.run(
        [sys.executable, str(HERE / "rollout.py"), "--task", task,
         "--driver", "scripted", "--rollout-id", "r0",
         "--run-id", "regrade-test", "--out", str(out)],
        capture_output=True, text=True)
    if not (out / "rollout.json").exists():
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        raise SystemExit(f"could not build the fixture rollout (exit {proc.returncode})")
    return out


def copy_of(src: Path, dst: Path) -> Path:
    shutil.rmtree(dst, ignore_errors=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    return dst


def scrub(path: Path) -> None:
    """Remove a scratch tree, git object store and all.

    A world carries a real `.git`, whose object files are read-only on Windows,
    and `ignore_errors=True` would leave several copies of one behind in the
    temp directory every time this runs.
    """
    def force(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=force)


# ------------------------------------------------------------------ 1: refusal
def test_refusals(tmp: Path, fixture: Path, artifact: Path) -> None:
    section("A regrade refuses what it cannot describe, and names it")

    empty = tmp / "refuse" / "empty"
    empty.mkdir(parents=True, exist_ok=True)
    msg = refuses(empty, task=FIXTURE_TASK)
    check("an empty directory is refused",
          bool(msg) and "not a completed episode" in msg)
    check("...and the refusal names every input that is missing",
          _missing_named(msg) == ["transcript.jsonl", "audit/actions.jsonl",
                                  "world", "grading"],
          str(_missing_named(msg)))

    for rel in ("transcript.jsonl", "audit/actions.jsonl", "world", "grading"):
        work = copy_of(fixture, tmp / "refuse" / rel.replace("/", "-"))
        target = work / rel
        # renamed rather than deleted: a git object store is read-only on
        # Windows, and what is being tested is that the input is not THERE
        target.rename(target.with_name(target.name + "-moved-by-the-test"))
        msg = refuses(work, task=FIXTURE_TASK)
        named = _missing_named(msg)
        check(f"a rollout with no {rel} is refused", bool(msg), msg[:100])
        check(f"...and the refusal names {rel}, and only it",
              named == [rel], str(named))

    # The artefact is the one input whose absence is a real episode OUTCOME, so
    # the refusal says which of the two paths to take instead.
    work = copy_of(fixture, tmp / "refuse" / "artifact")
    (work / "world" / Path(artifact).relative_to(fixture / "world")).unlink()
    msg = refuses(work, task=FIXTURE_TASK)
    check("a rollout with no final artefact is refused", bool(msg), msg[:90])
    check("...and the refusal names the artefact path and the alternative",
          "final_report.json" in msg and "--reclassify" in msg, msg[:140])

    # The driver decides the tool allowlist. Guessing it is how a subject gets
    # accused of holding a tool the harness handed it, so with no record and no
    # evidence on disk this refuses rather than assuming.
    work = copy_of(fixture, tmp / "refuse" / "driver")
    (work / "rollout.json").unlink()
    (work / rollout.BEFORE_EPISODE_FILE).unlink()
    msg = refuses(work, task=FIXTURE_TASK)
    check("a rollout whose driver is neither recorded nor evidenced is refused",
          bool(msg) and "driver" in msg, msg[:110])
    check("...and the refusal says why guessing is not an option",
          "allowlist" in msg, msg[:160])


# ----------------------------------------------------------------- 2: partial
def test_complete_and_partial(tmp: Path, fixture: Path, artifact: Path) -> None:
    section("A COMPLETE regrade re-runs the world diff; a PARTIAL one says it did not")

    full = copy_of(fixture, tmp / "complete")
    rec = rollout.regrade(full)
    check("a rollout with before_episode.json regrades COMPLETE",
          rec["regrade"]["status"] == "complete" and not rec["regrade"]["missing"],
          str([m["piece"] for m in rec["regrade"]["missing"]]))
    check("...and regrade_partial is false", rec["regrade_partial"] is False)
    check("...and the before-episode inventory is sourced from that file",
          rec["regrade"]["sources"].get("world_tree_before")
          == rollout.BEFORE_EPISODE_FILE,
          rec["regrade"]["sources"].get("world_tree_before"))
    # The artefact was planted into the world AFTER the episode, so a world diff
    # that really ran against the SAVED manifest has to notice it. This is the
    # positive proof that the before-picture is the episode's and not a fresh
    # one: a re-taken manifest would show no change at all.
    diffs = [v for v in rec["blindness"]["violations"]
             if v["detector"] == "world_diff"]
    check("...and the world-diff detector really used it",
          any("final_report.json" in v["evidence"] for v in diffs),
          f"{len(diffs)} world-diff violation(s)")
    check("...and the record carries the grader's sha256",
          len(rec["regrade_grader_sha256"]) == 64)
    check("...and regraded_from names the classification it replaced",
          rec["regraded_from"] == json.loads(
              (fixture / "rollout.json").read_text(encoding="utf-8"))["classification"],
          rec["regraded_from"])

    part = copy_of(fixture, tmp / "partial")
    (part / rollout.BEFORE_EPISODE_FILE).unlink()
    rec2 = rollout.regrade(part)
    pieces = [m["piece"] for m in rec2["regrade"]["missing"]]
    check("a rollout with no before_episode.json regrades PARTIAL",
          rec2["regrade"]["status"] == "partial" and rec2["regrade_partial"] is True)
    check("...and the missing piece is named", pieces == ["world_tree_before"],
          str(pieces))
    check("...and the consequence is written out, not implied",
          "carried forward" in rec2["regrade"]["missing"][0]["consequence"])
    check("...and the carry-forward is recorded as such",
          any("world-diff verdict" in c
              for c in rec2["regrade"]["carried_forward"]),
          str(rec2["regrade"]["carried_forward"])[:100])
    check("...and it re-grades to the same score as the complete regrade",
          (rec2.get("score") or {}).get("total")
          == (rec.get("score") or {}).get("total")
          and rec2["checks"] == rec["checks"],
          f"{(rec2.get('score') or {}).get('total')} vs "
          f"{(rec.get('score') or {}).get('total')}")
    # ...and it did NOT re-take the picture. The planted artefact is a world
    # change the RUN never saw, so a carried-forward verdict cannot mention it
    # and a freshly-taken "before" manifest would report no change at all. Both
    # errors are excluded by one assertion.
    part_diffs = [v for v in rec2["blindness"]["violations"]
                  if v["detector"] == "world_diff"]
    ran = [v for v in json.loads(
        (fixture / "rollout.json").read_text(encoding="utf-8"))
        ["blindness"]["violations"] if v["detector"] == "world_diff"]
    check("...and it carries the run's own world diff rather than taking a new one",
          not any("final_report.json" in v["evidence"] for v in part_diffs)
          and part_diffs == ran,
          f"{len(part_diffs)} carried, {len(ran)} in the run's own record")

    # ...and when there is no verdict to carry forward either, nothing is
    # invented. This is GT009's shape in miniature - there the record was never
    # written at all, because the harness died before it - reproduced here by
    # taking the verdict out of the record, since a scripted fixture's driver
    # cannot be recovered from disk and writing an mcp.json to get there would
    # be fabricating the evidence the refusal above exists to demand.
    none = copy_of(fixture, tmp / "no-verdict")
    (none / rollout.BEFORE_EPISODE_FILE).unlink()
    stripped = json.loads((none / "rollout.json").read_text(encoding="utf-8"))
    stripped.pop("blindness")
    (none / "rollout.json").write_text(json.dumps(stripped, indent=2),
                                       encoding="utf-8")
    rec3 = rollout.regrade(none)
    check("with neither file, the rollout is UNOBSERVED rather than clean",
          rec3["blindness"]["quarantined"]
          and "unobserved_episode" in rec3["blindness"]["hard_kinds"],
          str(rec3["blindness"]["hard_kinds"]))
    check("...and it is still graded, so the score is not lost with the verdict",
          (rec3.get("score") or {}).get("total") is not None,
          str((rec3.get("score") or {}).get("total")))


# -------------------------------------------------------------- 2b: run level
def test_run_level(tmp: Path, fixture: Path) -> None:
    section("`run_rollouts.py --regrade-run` rewrites the report and says so")

    run_dir = tmp / "run" / FIXTURE_TASK / "regrade-run"
    copy_of(fixture, run_dir / "r0")
    rc = run_rollouts.regrade_run(run_dir, None, None)
    check("the run regrades", rc == 0, f"exit {rc}")
    agg = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    conditions = agg["summary"]["run_conditions"]
    check("report.json carries `regraded: true`",
          conditions["regraded"] is True)
    check("...and counts the rollouts it re-graded",
          conditions["regraded_rollouts"] == 1)
    text = (run_dir / "report.md").read_text(encoding="utf-8")
    check("report.md states it in the run-conditions block, above the number",
          "- regraded: true" in text
          and text.index("- regraded: true") < text.index("**Pass@1 ="),
          [line for line in text.splitlines() if "regraded" in line][:1])

    empty = tmp / "run" / FIXTURE_TASK / "nothing"
    empty.mkdir(parents=True, exist_ok=True)
    check("a run directory with no rollouts is refused, not reported",
          run_rollouts.regrade_run(empty, None, None) == 2
          and not (empty / "report.json").exists())


# ------------------------------------------------------- 3: the recorded diff
def _leaves(a, b, path=""):
    """Every difference between two records, as (kind, path, before, after)."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{path}.{k}" if path else k
            if k not in a:
                out.append(("added", p, None, b[k]))
            elif k not in b:
                out.append(("dropped", p, a[k], None))
            else:
                out += _leaves(a[k], b[k], p)
    elif a != b:
        out.append(("changed", path, a, b))
    return out


def _is_relocation(a, b, old: Path, new: Path) -> bool:
    """True when `b` is `a` with the rollout directory's path swapped.

    A regrade of a COPY has to name the copy - otherwise the grader would be
    pointed back at the run the caller asked not to touch. Those are the only
    value changes a faithful regrade may make, and this proves each one is
    exactly a substitution rather than a different path that happens to be near.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    def norm(s: str) -> str:
        return s.replace("\\", "/").replace("//", "/").lower()
    return norm(b) == norm(a).replace(norm(str(old)), norm(str(new)))


def _recorded_candidates() -> list[Path]:
    """The newest recorded rollout of each task, newest task first.

    One per task, because the thing that stops a candidate being usable is
    almost always its task package - a grader mid-edit in this working tree -
    and four candidates from that same package would be four ways of learning
    the same thing. Nothing is graded to make the choice: it is made on what the
    runs wrote down, so the test cannot quietly select the rollout that happens
    to agree with it.
    """
    newest: dict[str, tuple] = {}
    for rec_path in RECORDED.glob("*/*/r*/rollout.json"):
        try:
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        task = rec.get("task")
        if not task or not rec.get("classification"):
            continue
        key = (rec.get("started_utc") or "", str(rec_path))
        if task not in newest or key > newest[task][0]:
            newest[task] = (key, rec_path.parent)
    return [p for _, p in sorted(newest.values(), key=lambda x: x[0], reverse=True)]


def _usable(tmp: Path, target: Path, why: list[str]) -> tuple | None:
    """(original, regraded, copy, target) for one candidate, or None + a reason.

    A task package that will not load, or whose grader is broken in this working
    tree, is a statement about the package. It is passed over and named rather
    than failed: this test is about the regrade.
    """
    original = json.loads((target / "rollout.json").read_text(encoding="utf-8"))
    try:
        taskspec.load(original["task"],
                      Path(original["tasks_dir"]) if original.get("tasks_dir") else None,
                      variant=original.get("variant") or None)
    except Exception as exc:                            # noqa: BLE001 - named
        why.append(f"{target}: {original['task']}'s package will not load "
                   f"({type(exc).__name__}: {exc})")
        return None
    work = copy_of(target, tmp / "recorded" / original["task"]
                   / target.parent.name / target.name)
    try:
        new = rollout.regrade(work)
    except rollout.RegradeRefused as exc:
        why.append(f"{target}: refused - {exc}")
        return None
    if new["grader_crashed"] and not original.get("grader_crashed"):
        why.append(f"{target}: {original['task']}'s grader fails in this working "
                   f"tree - {str(new.get('grader_error'))[:160]}")
        return None
    return original, new, work, target


def test_recorded_diff(tmp: Path, targets: list[Path], attempts: int = 4) -> None:
    section("Re-grading a rollout that already has a record reproduces it")

    passed_over: list[str] = []
    found = None
    for target in targets[:attempts]:
        found = _usable(tmp, target, passed_over)
        if found:
            break
    for line in passed_over:
        print(f"  passed over {line}")
    if not found:
        skip("the recorded-run diff",
             f"no recorded rollout under {RECORDED} could be re-graded in this "
             f"working tree ({len(passed_over)} passed over). Pass --against "
             f"<rollout-dir>.")
        return
    original, new, work, target = found
    print(f"  against {target}")
    print(f"  recorded {original.get('started_utc')} · "
          f"{original.get('task')} · {original.get('driver')}/"
          f"{original.get('topology')} · {original.get('classification')}")

    diffs = _leaves(original, new)
    relocations = [d for d in diffs
                   if d[0] == "changed" and _is_relocation(d[2], d[3], target, work)]
    added = [d for d in diffs if d[0] == "added"]
    dropped = [d for d in diffs if d[0] == "dropped"]
    other = [d for d in diffs if d not in relocations and d[0] == "changed"]

    print("\n  | field | kind | before -> after |")
    print("  |---|---|---|")
    for kind, path, a, b in diffs:
        label = ("regrade field" if path.split(".")[0] in REGRADE_FIELDS else
                 "relocation" if (kind, path, a, b) in relocations else
                 "schema added since the run" if kind == "added" else kind)
        sa = json.dumps(a, default=str)
        sb = json.dumps(b, default=str)
        print(f"  | {path} | {label} | {sa[:52]} -> {sb[:52]} |")

    check("the verdict is reproduced field for field",
          all(original.get(f) == new.get(f) for f in VERDICT_FIELDS
              if f in original),
          str([f for f in VERDICT_FIELDS
               if f in original and original.get(f) != new.get(f)]))
    check("the score is reproduced exactly",
          (original.get("score") or {}).get("total")
          == (new.get("score") or {}).get("total"),
          f"{(original.get('score') or {}).get('total')} -> "
          f"{(new.get('score') or {}).get('total')}")
    check("nothing is dropped from the record", not dropped,
          str([d[1] for d in dropped]))
    check("every added field is a regrade field or one the schema has gained",
          all(d[1].split(".")[0] in REGRADE_FIELDS or original.get(d[1].split(".")[0]) is None
              or d[1] != d[1].split(".")[0] for d in added),
          str([d[1] for d in added]))
    rederived = [d for d in other
                 if d[1].split(".")[0] in REDERIVED_FIELDS]
    unexplained = [d for d in other if d not in rederived]
    check("every changed value is a path naming the directory being re-graded, "
          "or a re-derived verdict field",
          not unexplained,
          str([(d[1], str(d[2])[:40], str(d[3])[:40]) for d in unexplained]))
    for _, path, a, b in rederived:
        print(f"  note: {path} was RE-DERIVED and moved, {a!r} -> {b!r}. The "
              f"task's declarations or the harness's own rule have changed "
              f"since this run; re-deriving them is the point - a stale verdict "
              f"is worse than no verdict - and it is the only kind of value "
              f"change a faithful regrade may make.")
    check("the regrade names what it could not re-run",
          new["regrade"]["status"] == "complete"
          or bool(new["regrade"]["missing"]),
          f"{new['regrade']['status']}: "
          f"{[m['piece'] for m in new['regrade']['missing']]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default=FIXTURE_TASK,
                    help="the task the scripted fixture rollout is built from")
    ap.add_argument("--against", default=None,
                    help="a recorded rollout directory to re-grade and diff "
                         "(copied first; the original is never written to)")
    ap.add_argument("--no-recorded", action="store_true",
                    help="skip the recorded-run diff")
    ap.add_argument("--keep", action="store_true",
                    help="leave the scratch directory in place")
    a = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="alkera-regrade-test-"))
    print(f"\n  scratch {tmp}")
    try:
        print(f"  building the fixture rollout ({a.task}, --driver scripted, "
              f"no model) ...")
        started = time.time()
        fixture = build_fixture(tmp, a.task)
        spec = taskspec.load(a.task)
        world = json.loads(
            (fixture / "rollout.json").read_text(encoding="utf-8"))["world"]
        artifact = spec.artifact_host_path(world)
        # The scripted subject writes no artefact - it exists to trip detectors,
        # not to do the ticket - so the fixture plants one. It is a test input
        # and nothing here reads it as evidence about a subject; what it buys is
        # a completed-episode shape, and a world change after the before-picture
        # that the world diff must be able to see.
        artifact.write_text(ARTIFACT, encoding="utf-8")
        print(f"  fixture at {fixture} in {round(time.time() - started, 1)}s")

        test_refusals(tmp, fixture, artifact)
        test_complete_and_partial(tmp, fixture, artifact)
        test_run_level(tmp, fixture)
        if a.no_recorded:
            skip("the recorded-run diff", "--no-recorded")
        else:
            test_recorded_diff(tmp, [Path(a.against).resolve()] if a.against
                               else _recorded_candidates())
    finally:
        if a.keep:
            print(f"\n  scratch kept at {tmp}")
        else:
            scrub(tmp)
    print(f"\n  {PASSED} passed, {FAILED} failed"
          + (f", {len(SKIPPED)} skipped" if SKIPPED else ""))
    for s in SKIPPED:
        print(f"    skipped - {s}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
