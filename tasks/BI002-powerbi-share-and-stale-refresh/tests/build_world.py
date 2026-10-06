#!/usr/bin/env python3
"""
BI002 episode-world builder - the entrypoint `[harness] world_builder` names.

    python build_world.py --root <dir> --grading <dir> [--seed 11]

Prints ONE json object (the harness contract):

    {"world_root", "workspace", "warehouse", "scheduler", "dashboards",
     "profiles_dir", "grading", "virtual_time", "seed", "workflow_dir",
     "variant"}

WHAT IT BUILDS. Not a fixture: the world factory's own commerce_analytics
theme, from `fixtures/seed.yaml` - an analyst's laptop (`da/laptop`) with Power
BI the one BI vendor plugged and both of the theme's Power BI faults on
(world/world/themes/commerce_analytics/gen.py BI_FAULTS):

    powerbi_share_scope   Segment Share's denominator removes every Customer
                          filter: planted in bi/powerbi BEFORE the repository's
                          first commit, so it is what the Power BI service
                          publishes at provisioning (world/world/bi_projects.py)
    powerbi_stale_import  the world's last scheduled refresh ran before
                          Saturday's extract landed: gen.bi_imports replays
                          that night and writes what the refresh imported to
                          var/bi/powerbi/, which the service provisions the
                          dataset from (services/powerbi.py IMPORT MODE)

THEN THE INCIDENT. The arm's `fixtures/workflow/` (ANA-242 and its thread,
Sunday's late extract in #data-incidents) is merged into the theme's corpus and
the workflow plane is re-seeded through world.workflow - every rule the
theme's corpus obeys holds for the overlay too. The onboarding page counts the
queue, so it is regenerated with the same call world.build made, and the
repository's single commit is amended with the world's pinned dates.

THE GRADING ZONE (a sibling of the world root; never mounted, never named in
any gateway config):

    manifest.yaml, variant.json     the rubric and the arm
    pristine/bi/powerbi/            the project as committed = as provisioned
    pristine/workflow/              var/workflow before the episode (D1, P1)
    pristine/definitions/           the files that define Net Revenue and
                                    Segment Share (P1)
    pristine/warehouse.duckdb       the warehouse before the episode: the graders
                                    render every build on THIS copy
    perturbed.duckdb                the same with contract.PERTURB_SQL applied
    oracle.json                     contract.ORACLE_SQL on both warehouses
    baseline.json                   warehouse content digests, definition file
                                    hashes, the provisioned model's files and
                                    measures, and the world's import timeline
                                    (when the late extract landed, which refresh
                                    loaded the import): what R1 reads
"""
from __future__ import annotations

import argparse
import base64
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
import contract as K                                               # noqa: E402
import pbi_eval as E                                               # noqa: E402

VARIANT = taskspec.variant_of(TASK)
FIXTURES = taskspec.stage_fixtures(TASK, VARIANT)
SEED_FILE = FIXTURES / "seed.yaml"


def _seed() -> dict:
    return yaml.safe_load(SEED_FILE.read_text(encoding="utf-8"))


SEED = int(_seed()["seed"])


# ------------------------------------------------------------ the incident
def _by_id(items: list, new: list) -> list:
    out = list(items)
    index = {item["id"]: i for i, item in enumerate(out)}
    for item in new:
        if item["id"] in index:
            out[index[item["id"]]] = item
        else:
            out.append(item)
    return out


def merged_corpus(theme_dir: Path, overlay: Path, dest: Path) -> Path:
    """The theme's workflow corpus with the arm's overlay merged in. Issues,
    pages and users are keyed by id (an overlay entry replaces the theme's); a
    channel the overlay names gets the overlay's messages appended."""
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
    with tempfile.TemporaryDirectory(prefix="bi002-corpus-") as tmp:
        corpus = merged_corpus(theme.dir, overlay, Path(tmp))
        workflow.seed(root, corpus, theme.manifest["workflow"],
                      scheduler.load(root)["vnow"])
    profile = wbuild.resolve_profile(seed)
    native_workspace.write(root, theme, slots=profile["slots"],
                           plugs=profile.get("plugs"), name=profile["name"])
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
    out = {}
    for pattern in K.DEFINITION_GLOBS:
        for path in sorted(Path(workspace).glob(pattern)):
            if path.is_file():
                out[path.relative_to(workspace).as_posix()] = _sha(path)
    return out


def import_timeline(root: Path) -> dict:
    """What the world says about the dataset's import (gen.bi_imports): the
    late extract's landing, and the refresh the published import came from."""
    record = json.loads((Path(root) / K.IMPORT_RECORD).read_text(encoding="utf-8"))
    dataset = record["datasets"][K.DATASET]
    return {"landed": dataset["timeline"]["landed"],
            "partition": dataset["timeline"]["partition"],
            "reloaded": dataset["timeline"]["reloaded"],
            "refreshed": dataset["import"]["refreshed"],
            "seeded_refreshes": len(dataset["refreshes"])}


def write_grading_zone(root: Path, grading: Path) -> None:
    root, grading = Path(root).resolve(), Path(grading).resolve()
    if grading.is_relative_to(root):
        raise SystemExit(f"the grading zone {grading} is inside the world root {root}")
    workspace = root / "workspace"
    warehouse = root / "var" / "warehouse" / "warehouse.duckdb"
    pristine = grading / "pristine"
    if pristine.exists():
        shutil.rmtree(pristine)
    shutil.copytree(workspace / K.PROJECT_DIR, pristine / K.PROJECT_DIR)
    shutil.copytree(root / "var" / "workflow", pristine / "workflow")
    for rel in definition_files(workspace):
        target = pristine / "definitions" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(workspace / rel, target)
    wh = pristine / "warehouse.duckdb"
    shutil.copyfile(warehouse, wh)
    perturbed = E.perturb(wh, grading / "perturbed.duckdb")
    _dump(grading / "oracle.json", {"real": E.oracle(wh), "perturbed": E.oracle(perturbed)})
    model, _report = E.read_tree(pristine / K.PROJECT_DIR, wh)
    _dump(grading / "baseline.json", {
        "warehouse": E.warehouse_digests(warehouse),
        "definitions": definition_files(workspace),
        "model_files": {p: base64.b64encode(d).decode("ascii")
                        for p, d in E.model_files(pristine / K.PROJECT_DIR).items()},
        "measures": sorted(m.name for m in model.measures),
        "import": import_timeline(root),
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
                         f"the world's numbers (ANA-242's included) are measured on it")
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
        # analyst's box plugs neither, and task.toml declares the profile that
        # says so.
        "workflow_dir": str(root / "var" / "workflow"),
        "variant": VARIANT.name,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--grading")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--prepare-only", action="store_true",
                    help="the harness's one-time step before rollouts fork "
                         "(episode.prepare_task). BI002 caches nothing - every "
                         "rollout builds its own world from the seed in seconds - "
                         "so this only proves the arm and the seed resolve.")
    ap.add_argument("--variant", default=None,
                    help="confirm the arm (resolved from ALKERA_TASK_VARIANT at import)")
    a = ap.parse_args(argv)
    if a.variant and a.variant != VARIANT.name:
        ap.error(f"--variant {a.variant!r} disagrees with the arm this process staged "
                 f"({VARIANT.name!r}); set {taskspec.VARIANT_ENV} instead")
    if a.prepare_only:
        if a.seed != SEED:
            ap.error(f"--seed {a.seed} is not this task's seed ({SEED}, {SEED_FILE.name})")
        return 0
    if not (a.root and a.grading):
        ap.error("--root and --grading are required unless --prepare-only")
    print(json.dumps(build(Path(a.root), Path(a.grading), a.seed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
