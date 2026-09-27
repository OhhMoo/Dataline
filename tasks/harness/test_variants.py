#!/usr/bin/env python3
"""Executed proof of the context-variant machinery. No model, no quota.

    gateway/.venv/Scripts/python.exe tasks/harness/test_variants.py
    gateway/.venv/Scripts/python.exe tasks/harness/test_variants.py --fast

WHAT IS REALLY EXECUTED. Every task package in tasks/ is loaded through
taskspec for every arm it declares; the overlay trees are staged on disk and
compared file by file against their sources; and - unless --fast is given - the
GT004 world is BUILT TWICE, once with no variant at all and once with
`--variant default`, and the two world roots are diffed byte for byte. That
last one is the load-bearing claim of the whole design: `default` is the
shipped context, so folding seven arm directories into one package must not
have moved the world the already-recorded rollouts were measured in.

It is a script and not a pytest module for the same reason test_native_stack.py
is: it builds worlds and shells out, and a run that half-failed has to print
what it got rather than a collection error.

TWO DIFFERENCES ARE EXPECTED between two builds of one world, and they are
named rather than tolerated silently:

  var/dbt/profiles.yml   embeds the ABSOLUTE path of the warehouse it points
                         at, which is the temporary directory each build was
                         given. Two builds at two paths cannot agree on it.
  workspace/.git/**      git's index records inode and ctime for every staged
                         file. `world/fingerprint.py` excludes .git from the
                         world's identity for exactly this reason.

Anything else that differs is a finding, and the check prints the file.
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS = HERE.parent
REPO = TASKS.parent
PY = REPO / "gateway" / ".venv" / "Scripts" / "python.exe"

sys.path.insert(0, str(HERE))
import taskspec                                                    # noqa: E402

# The two paths a second build of one world cannot reproduce. See the header.
PATH_BOUND = ("var/dbt/profiles.yml",)
VOLATILE_DIRS = (".git",)

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    (PASSED if ok else FAILED).append(label)
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"\n         {detail}" if detail and not ok
                                   else ""))
    return ok


def tasks_with_variants() -> list[str]:
    """Every task package that declares a [variants] table."""
    out = []
    for toml in sorted(TASKS.glob("GT*/task.toml")):
        if "[variants." in toml.read_text(encoding="utf-8"):
            out.append(toml.parent.name)
    return out


def tasks_without_variants() -> list[str]:
    return [t.parent.name for t in sorted(TASKS.glob("GT*/task.toml"))
            if "[variants." not in t.read_text(encoding="utf-8")]


# ---------------------------------------------------------------- resolution
def test_resolution() -> None:
    """Every declared arm resolves, and resolves to ITS overlay and ITS brief."""
    print("\n  resolution")
    declared = tasks_with_variants()
    check("at least one task declares [variants]", bool(declared),
          "no task declares an arm; the machinery is untested by construction")
    for task in declared:
        spec = taskspec.load(task)
        check(f"{task}: no --variant resolves to `default`",
              spec.variant == "default", f"got {spec.variant!r}")
        check(f"{task}: `default` declares no overlay",
              not spec.variant_overlays, f"got {spec.variant_overlays}")
        for name in spec.variant_names:
            arm = taskspec.load(task, variant=name)
            check(f"{task}/{name}: resolves and carries its own note",
                  arm.variant == name and bool(arm.variant_note.strip()))
            for overlay in arm.variant_overlays:
                check(f"{task}/{name}: overlay {overlay.name} exists",
                      overlay.is_dir(), str(overlay))
            base = taskspec.load(task)
            if arm.raw.get("variants", {}).get(name, {}).get("prompt"):
                check(f"{task}/{name}: its prompt is a DECLARED, locked variant",
                      arm.instruction_path != base.instruction_path
                      or arm.instruction_path.name in
                      {Path(v["file"]).name
                       for v in arm.raw.get("prompt_variants", {}).values()
                       if isinstance(v, dict) and v.get("file")})


def test_environment_variable() -> None:
    """ALKERA_TASK_VARIANT is what carries the arm into a subprocess."""
    print("\n  the environment variable")
    task = tasks_with_variants()[0]
    names = [n for n in taskspec.load(task).variant_names if n != "default"]
    if not names:
        check(f"{task} declares a non-default arm to test the env var with",
              False, "only `default` is declared")
        return
    target = names[0]
    before = os.environ.get(taskspec.VARIANT_ENV)
    try:
        os.environ[taskspec.VARIANT_ENV] = target
        check(f"{taskspec.VARIANT_ENV}={target} is honoured when no flag is given",
              taskspec.load(task).variant == target)
        check("an explicit variant BEATS the environment variable",
              taskspec.load(task, variant="default").variant == "default")
        os.environ[taskspec.VARIANT_ENV] = ""
        check("an EMPTY environment variable means `not set`, not a variant "
              "called ''", taskspec.load(task).variant == "default")
    finally:
        if before is None:
            os.environ.pop(taskspec.VARIANT_ENV, None)
        else:
            os.environ[taskspec.VARIANT_ENV] = before


# -------------------------------------------------------------------- errors
def test_errors() -> None:
    """A refusal has to NAME the task and the alternatives, or the operator
    reads it as a harness bug rather than as a typo."""
    print("\n  refusals")
    task = tasks_with_variants()[0]
    try:
        taskspec.load(task, variant="no-such-arm")
        check(f"{task}: an unknown variant is refused", False, "it loaded")
    except taskspec.TaskSpecError as exc:
        msg = str(exc)
        check(f"{task}: an unknown variant is refused",
              "no-such-arm" in msg and task in msg, msg)
        check(f"{task}: and the refusal lists the arms that DO exist",
              "default" in msg, msg)

    plain = tasks_without_variants()
    check("at least one task declares no [variants], so the unsupported case "
          "is testable", bool(plain))
    for one in plain[:1]:
        try:
            taskspec.load(one, variant="anything")
            check(f"{one}: --variant on a task with no [variants] is an error",
                  False, "it loaded")
        except taskspec.TaskSpecError as exc:
            check(f"{one}: --variant on a task with no [variants] is an error "
                  "that NAMES the task", one in str(exc), str(exc))

    _bad_declaration("a [variants] table with no `default`",
                     '[variants.only-arm]\nnote = "x"\n', "default")
    _bad_declaration("an arm with no `note`",
                     '[variants.default]\nnote = "the shipped context"\n'
                     '[variants.nameless]\n', "note")
    _bad_declaration("a `default` that declares an overlay",
                     '[variants.default]\nnote = "x"\noverlay = "fixtures"\n',
                     "default")


def _bad_declaration(label: str, block: str, must_mention: str) -> None:
    """Copy a real task package's task.toml, break its [variants] table the one
    way this case is about, and require the refusal to say so."""
    source = TASKS / tasks_with_variants()[0] / "task.toml"
    text = source.read_text(encoding="utf-8")
    head = text.split("[variants.")[0]
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "GT000-broken"
        (root / "fixtures").mkdir(parents=True)
        with open(root / "task.toml", "w", encoding="utf-8", newline="") as fh:
            fh.write(head + block)
        try:
            taskspec.load("GT000-broken", Path(td))
            check(f"refused: {label}", False, "it loaded")
        except taskspec.TaskSpecError as exc:
            check(f"refused: {label}", must_mention in str(exc), str(exc))
        except Exception as exc:                                # noqa: BLE001
            check(f"refused: {label}", False,
                  f"raised {type(exc).__name__} rather than TaskSpecError: {exc}")


# ------------------------------------------------------------------ staging
def test_staging_overlay_order() -> None:
    """Base first, overlays in order, last write wins - nothing else moves."""
    pristine = tempfile.mkdtemp(prefix="alkera-variants-staged-")
    try:
        _staging_checks(pristine)
    finally:
        shutil.rmtree(pristine, ignore_errors=True)


def _staging_checks(pristine: str) -> None:
    """The body of test_staging_overlay_order, given a scratch directory."""
    print("\n  staging")
    for task in tasks_with_variants():
        root = TASKS / task
        fixtures = root / "fixtures"
        base = taskspec.load(task)
        check(f"{task}/default: staging returns the PACKAGE fixtures, uncopied",
              taskspec.stage_fixtures(root, _variant(base)) == fixtures)
        for name in base.variant_names:
            arm = taskspec.load(task, variant=name)
            if not arm.variant_overlays:
                continue
            # Staged into a FRESH directory, not into the shared cache. The
            # cache is a working tree: a task whose `fixtures/build/` is
            # generated and gitignored regenerates it in place for the arm (see
            # docs/implementation_plan/harness/VARIANTS_REQUESTS.md item 2), so the cached tree legitimately
            # stops being base+overlay the first time its world is built. What
            # `stage_fixtures` PRODUCES is what this checks.
            staged = taskspec.stage_fixtures(root, _variant(arm),
                                             dest=Path(pristine) / task / name)
            # LAST WRITE WINS: a file two overlays both carry must come from the
            # last one that declares it, which is what this dict rebuilds.
            source_of = {}
            for overlay in arm.variant_overlays:
                for f in overlay.rglob("*"):
                    if f.is_file() and "__pycache__" not in f.parts:
                        source_of[f.relative_to(overlay)] = f
            overlaid = sorted(source_of)
            wrong = [str(r) for r in overlaid
                     if not filecmp.cmp(source_of[r], staged / r, shallow=False)]
            check(f"{task}/{name}: every overlay file WINS over the base "
                  f"({len(overlaid)} file(s))", not wrong, str(wrong))

            untouched, moved = 0, []
            for src in fixtures.rglob("*"):
                if not src.is_file() or "__pycache__" in src.parts:
                    continue
                rel = src.relative_to(fixtures)
                if rel.parts and rel.parts[0] == "variants":
                    continue
                if rel in overlaid:
                    continue
                if not filecmp.cmp(src, staged / rel, shallow=False):
                    moved.append(str(rel))
                untouched += 1
            check(f"{task}/{name}: every file the overlay does NOT name is "
                  f"byte-identical to the package ({untouched} file(s))",
                  not moved, str(moved[:5]))
            check(f"{task}/{name}: the staged tree does not carry variants/",
                  not (staged / "variants").exists())

            marker = staged / ".variant-stamp"
            before = marker.stat().st_mtime_ns if marker.is_file() else None
            again = taskspec.stage_fixtures(root, _variant(arm),
                                            dest=Path(pristine) / task / name)
            check(f"{task}/{name}: re-staging is a cache hit, not a rebuild",
                  again == staged and marker.is_file()
                  and marker.stat().st_mtime_ns == before)


def _variant(spec) -> taskspec.Variant:
    """The Variant a loaded spec resolved to. TaskSpec flattens it for the
    harness; staging wants it whole."""
    return taskspec.Variant(name=spec.variant, note=spec.variant_note,
                            overlays=spec.variant_overlays,
                            grading=spec.variant_grading)


# ----------------------------------------- default == a build with no variant
def _tree(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(part in VOLATILE_DIRS for part in p.relative_to(root).parts):
            continue
        out[rel] = p
    return out


def _build(task: str, dest: Path, variant: str | None) -> Path:
    env = {**os.environ, "PYTHONPATH": str(REPO / "world"), "PYTHONUTF8": "1"}
    if variant:
        env[taskspec.VARIANT_ENV] = variant
    else:
        env.pop(taskspec.VARIANT_ENV, None)
    spec = taskspec.load(task, variant=variant)
    r = subprocess.run([str(PY), str(spec.world_builder),
                        "--root", str(dest / "root"),
                        "--grading", str(dest / "grading")],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"world builder failed for {task}/{variant}:\n"
                           f"{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return dest


def test_default_is_the_shipped_world(task: str) -> None:
    """THE claim the seven deleted directories rest on.

    A build with no variant at all and a build with `--variant default` must
    produce the same world root, byte for byte, except for the two paths the
    header names. If they do not, every rollout recorded before the variants
    existed was measured in a world this package can no longer produce.
    """
    print(f"\n  {task}: `default` vs no variant at all")
    tmp = Path(tempfile.mkdtemp(prefix="alkera-variants-"))
    try:
        a = _build(task, tmp / "novariant", None)
        b = _build(task, tmp / "default", "default")
        ta, tb = _tree(a / "root"), _tree(b / "root")
        check(f"{task}: the two builds contain the same files",
              set(ta) == set(tb),
              str(sorted(set(ta) ^ set(tb))[:5]))
        differ = [rel for rel in sorted(set(ta) & set(tb))
                  if not filecmp.cmp(ta[rel], tb[rel], shallow=False)]
        unexplained = [r for r in differ if r not in PATH_BOUND]
        check(f"{task}: every file is byte-identical except the "
              f"{len(PATH_BOUND)} path-bound one(s)", not unexplained,
              str(unexplained[:10]))
        check(f"{task}: and the path-bound difference is exactly the one named",
              set(differ) <= set(PATH_BOUND), str(differ))
        va = json.loads((a / "grading" / "variant.json").read_text("utf-8"))
        vb = json.loads((b / "grading" / "variant.json").read_text("utf-8"))
        check(f"{task}: both grading zones name the same arm",
              va["variant"] == vb["variant"] == "default",
              f"{va['variant']!r} vs {vb['variant']!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true",
                    help="skip the two world builds. The staging checks still "
                         "run; the byte-identity claim does NOT, and the "
                         "summary says so.")
    ap.add_argument("--task", default="GT004-doubling-window",
                    help="which package to build twice for the byte-identity "
                         "check")
    a = ap.parse_args()

    print("\n  tasks/harness/test_variants.py - context variants, executed")
    test_resolution()
    test_environment_variable()
    test_errors()
    test_staging_overlay_order()
    if a.fast:
        print("\n  --fast: the `default` == no-variant world build was NOT run. "
              "That is the\n  claim the seven deleted arm directories rest on, "
              "and this run does not make it.")
    else:
        test_default_is_the_shipped_world(a.task)

    total = len(PASSED) + len(FAILED)
    print(f"\n  {len(PASSED)} / {total} checks passed")
    if FAILED:
        print("  FAILED:")
        for f in FAILED:
            print("    " + f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
