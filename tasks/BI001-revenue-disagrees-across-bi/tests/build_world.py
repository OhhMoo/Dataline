#!/usr/bin/env python3
"""
BI001 episode-world builder - the entrypoint `[harness] world_builder` names.

    python build_world.py --root <dir> --grading <dir> [--seed 11]

Prints ONE json object (the harness contract):

    {"world_root", "workspace", "warehouse", "scheduler", "dashboards",
     "profiles_dir", "grading", "virtual_time", "seed", "workflow_dir",
     "variant"}

WHAT IT BUILDS. Not a fixture: the world factory's own commerce_analytics
theme, from `fixtures/seed.yaml` - an analyst's laptop (`da/laptop`) with Power
BI, Tableau and Sigma plugged and both of the theme's BI faults on
(world/world/themes/commerce_analytics/gen.py BI_FAULTS). world.build writes
each vendor's project into workspace/bi/ BEFORE the repository's first commit,
so the faulty projects are what every fake vendor service publishes at
provisioning (world/world/bi_projects.py).

THEN THE INCIDENT. The arm's `fixtures/workflow/` (ANA-241 and its thread, two
messages in #finance-analytics) is merged into the theme's corpus and the
workflow plane is re-seeded through world.workflow - every rule the theme's
corpus obeys holds for the overlay too. The onboarding page counts the queue,
so it is regenerated with the same call world.build made, and the repository's
single commit is amended with the world's pinned dates: the result is one
commit, byte-identical across builds, exactly as a world.build of that corpus
would have made it.

THE GRADING ZONE (a sibling of the world root; never mounted, never named in
any gateway config):

    manifest.yaml, variant.json     the rubric and the arm, as every GT task
    pristine/bi/<vendor>/           the projects as committed = as provisioned
    pristine/workflow/              var/workflow before the episode (D1, P1)
    pristine/definitions/           the dbt files that define Net Revenue (P1)
    pristine/warehouse.duckdb       the warehouse before the episode: the graders
                                    render every build on THIS copy
    perturbed.duckdb                the same with contract.PERTURB_SQL applied
    oracle.json                     contract.ORACLE_SQL on both warehouses
    baseline.json                   warehouse content digests, the pristine
                                    Power BI render, definition file hashes
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parent.parent

sys.path.insert(0, str(TASK.parent / "harness"))
import taskspec                                                    # noqa: E402

sys.path.insert(0, str(HERE))
if str(REPO / "world") not in sys.path:
    sys.path.insert(0, str(REPO / "world"))
import bi_eval as E                                                # noqa: E402
import contract as K                                               # noqa: E402

VARIANT = taskspec.variant_of(TASK)
FIXTURES = taskspec.stage_fixtures(TASK, VARIANT)
SEED_FILE = FIXTURES / "seed.yaml"


def _seed() -> dict:
    return yaml.safe_load(SEED_FILE.read_text(encoding="utf-8"))


SEED = int(_seed()["seed"])


# ------------------------------------------------------------ the incident
def _by_id(items: list, new: list) -> list:
    """`items` with each of `new` replacing the entry of the same id, or appended."""
    out = list(items)
    index = {item["id"]: i for i, item in enumerate(out)}
    for item in new:
        if item["id"] in index:
            out[index[item["id"]]] = item
        else:
            out.append(item)
    return out


def merged_corpus(theme_dir: Path, overlay: Path, dest: Path) -> Path:
    """The theme's workflow corpus with the arm's overlay merged in, written as
    a corpus directory world.workflow can seed from. Issues, pages and users
    are keyed by id (an overlay entry replaces the theme's); a channel the
    overlay names gets the overlay's messages appended."""
    src = Path(theme_dir) / "workflow"
    parts = {name: yaml.safe_load((src / f"{name}.yaml").read_text(encoding="utf-8"))
             for name in ("users", "issues", "chat", "kb")}
    for name, field in (("users", "users"), ("issues", "issues"), ("kb", "pages")):
        extra = overlay / f"{name}.yaml"
        if extra.is_file():
            doc = yaml.safe_load(extra.read_text(encoding="utf-8")) or {}
            parts[name][field] = _by_id(parts[name][field], doc.get(field) or [])
    extra = overlay / "chat.yaml"
    if extra.is_file():
        doc = yaml.safe_load(extra.read_text(encoding="utf-8")) or {}
        channels = {c["id"]: c for c in parts["chat"]["channels"]}
        for channel in doc.get("channels") or []:
            if channel["id"] in channels:
                channels[channel["id"]].setdefault("messages", []).extend(
                    channel.get("messages") or [])
            else:
                parts["chat"]["channels"].append(channel)
    out = Path(dest) / "workflow"
    out.mkdir(parents=True, exist_ok=True)
    for name, doc in parts.items():
        (out / f"{name}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False,
                                                         allow_unicode=False),
                                          encoding="utf-8", newline="")
    return Path(dest)


def stage_incident(root: Path, seed: dict) -> None:
    """Merge the arm's overlay into var/workflow, regenerate the onboarding
    page, amend the repository's one commit."""
    from world import build as wbuild
    from world import native_workspace, scheduler, workflow
    from world.themes import load_theme

    theme = load_theme(seed["theme"])
    overlay = FIXTURES / "workflow"
    with tempfile.TemporaryDirectory(prefix="bi001-corpus-") as tmp:
        corpus = merged_corpus(theme.dir, overlay, Path(tmp))
        workflow.seed(root, corpus, theme.manifest["workflow"],
                      scheduler.load(root)["vnow"])
    profile = wbuild.resolve_profile(seed)
    native_workspace.write(root, theme, slots=profile["slots"],
                           plugs=profile.get("plugs"))
    env = {**os.environ, "GIT_AUTHOR_DATE": seed["vtime_start"],
           "GIT_COMMITTER_DATE": seed["vtime_start"]}
    workspace = root / "workspace"
    for args in (["add", "-A"],
                 ["-c", "commit.gpgsign=false", "commit", "-q", "--amend", "--no-edit"]):
        subprocess.run(["git", *args], cwd=workspace, env=env, check=True,
                       capture_output=True)


# ------------------------------------------------------------ grading zone
def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def definition_files(workspace: Path) -> dict[str, str]:
    """{workspace-relative path: sha256} for contract.DEFINITION_GLOBS."""
    out = {}
    for pattern in K.DEFINITION_GLOBS:
        for path in sorted(Path(workspace).glob(pattern)):
            if path.is_file():
                out[path.relative_to(workspace).as_posix()] = _sha(path)
    return out


def write_grading_zone(root: Path, grading: Path) -> None:
    root, grading = Path(root).resolve(), Path(grading).resolve()
    if grading.is_relative_to(root):
        raise SystemExit(f"the grading zone {grading} is inside the world root {root}")
    workspace = root / "workspace"
    warehouse = root / "var" / "warehouse" / "warehouse.duckdb"
    pristine = grading / "pristine"
    if pristine.exists():
        shutil.rmtree(pristine)
    for vendor in K.VENDORS:
        shutil.copytree(workspace / K.PROJECT_DIRS[vendor], pristine / K.PROJECT_DIRS[vendor])
    shutil.copytree(root / "var" / "workflow", pristine / "workflow")
    for rel in definition_files(workspace):
        target = pristine / "definitions" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(workspace / rel, target)
    wh = pristine / "warehouse.duckdb"
    shutil.copyfile(warehouse, wh)
    perturbed = E.perturb(wh, grading / "perturbed.duckdb")
    oracles = {"real": E.oracle(wh), "perturbed": E.oracle(perturbed)}
    _dump(grading / "oracle.json", oracles)
    model, report = E.read_tree("powerbi", pristine / K.PROJECT_DIRS["powerbi"], wh)
    _dump(grading / "baseline.json", {
        "warehouse": E.warehouse_digests(warehouse),
        "definitions": definition_files(workspace),
        "powerbi_render": E.render_all(model, report, wh),
    })
    shutil.copyfile(HERE / "manifest.yaml", grading / "manifest.yaml")
    _dump(grading / "variant.json", {"variant": VARIANT.name, "note": VARIANT.note,
                                     "role": VARIANT.role, "grading": VARIANT.grading})


def _dump(path: Path, doc) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n")


# ------------------------------------------------------------------ build
def build(root: Path, grading: Path, seed: int = SEED) -> dict:
    from world import build as wbuild

    root, grading = Path(root).resolve(), Path(grading).resolve()
    spec = _seed()
    if int(seed) != int(spec["seed"]):
        raise SystemExit(f"--seed {seed} but fixtures/seed.yaml is seed {spec['seed']}; "
                         f"the world's numbers (ANA-241's included) are measured on it")
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"{root} is not empty; build into a fresh directory")
    wbuild.build(SEED_FILE, root)
    stage_incident(root, spec)
    grading.mkdir(parents=True, exist_ok=True)
    write_grading_zone(root, grading)
    state = json.loads((root / "var" / "sched" / "state.json").read_text(encoding="utf-8"))
    return {
        "world_root": str(root),
        "workspace": str(root / "workspace"),
        "warehouse": str(root / "var" / "warehouse" / "warehouse.duckdb"),
        "scheduler": str(root / "var" / "sched" / "state.json"),
        "dashboards": str(root / "var" / "bi" / "dashboards"),
        "profiles_dir": str(root / "build"),
        "grading": str(grading),
        "virtual_time": state["vnow"],
        "seed": int(seed),
        # The human plane. `observability` and `ingest` are NOT published: an
        # analyst's box plugs neither, the world has neither, and task.toml
        # declares the profile that says so.
        "workflow_dir": str(root / "var" / "workflow"),
        "variant": VARIANT.name,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--grading", required=True)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--variant", default=None,
                    help="confirm the arm (resolved from ALKERA_TASK_VARIANT at import)")
    a = ap.parse_args(argv)
    if a.variant and a.variant != VARIANT.name:
        ap.error(f"--variant {a.variant!r} disagrees with the arm this process staged "
                 f"({VARIANT.name!r}); set {taskspec.VARIANT_ENV} instead")
    print(json.dumps(build(Path(a.root), Path(a.grading), a.seed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
