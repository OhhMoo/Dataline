"""The PBIP project: `<Report>.pbip` + `<Report>.Report/` + `<Model>.SemanticModel/`.

Power BI Desktop saves a project as one `.pbip` shortcut per report, the
report's PBIR folder and the semantic model's TMDL folder side by side; a
report reaches its model through `definition.pbir`'s relative `byPath`. Several
thin reports sharing one model is the layout teams check in, so that is what
`write_project` produces for (model, reports):

    <dest>/.gitignore                  .pbi/localSettings.json, .pbi/cache.abf (as Desktop)
    <dest>/<Model>.SemanticModel/...   tmdl.py
    <dest>/<Report>.Report/...         pbir.py, one per report
    <dest>/<Report>.pbip               {"artifacts": [{"report": {"path": "<Report>.Report"}}]}

`read_project(path)` takes a `.pbip` file (that report and its model), a
`.Report` or `.SemanticModel` folder, or a folder holding projects (every
`.pbip` in it; they must share one semantic model, or the folder must hold
exactly one `.SemanticModel` and no `.pbip`).

DETERMINISM. Same inputs, same bytes: ids are derived from names (ids.py), JSON
is written in a fixed key order with two-space indent, nothing reads a clock.
`write_project` clears the `definition/` folders it is about to regenerate, so
a page or table that no longer exists does not survive as a stale file.

Sources: Power BI Desktop projects (learn.microsoft.com/power-bi/developer/
projects/projects-overview), the .pbip file schema github.com/microsoft/
json-schemas fabric/pbip/pbipProperties/1.0.0 (MIT).
"""
from __future__ import annotations

import posixpath
import shutil
from pathlib import Path

from ...errors import ModelError
from ...model import Model, Report
from . import pbir, tmdl
from .mquery import DEFAULT_CONNECTOR
from .tmdl import file_name, json_bytes, json_document

PBIP_SCHEMA = ("https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/"
               "1.0.0/schema.json")
GITIGNORE = b"**/.pbi/localSettings.json\n**/.pbi/cache.abf\n"
#: Files Desktop keeps beside a project that are not part of its definition.
LOCAL_ONLY = (".pbi/",)


def project_files(model: Model, reports: tuple[Report, ...] = (), *,
                  connector: str = DEFAULT_CONNECTOR) -> dict[str, bytes]:
    """Every file `write_project` writes, keyed by path relative to `dest`."""
    model_folder = f"{file_name(model.name)}.SemanticModel"
    files = {".gitignore": GITIGNORE}
    for rel, data in tmdl.semantic_model_files(model, connector=connector).items():
        files[f"{model_folder}/{rel}"] = data
    seen = set()
    for report in reports:
        folder = f"{file_name(report.name)}.Report"
        if folder.lower() in seen or folder.lower() == model_folder.lower():
            raise ModelError(f"two items of the project are named {report.name!r}")
        seen.add(folder.lower())
        for rel, data in pbir.report_files(report, model, model_folder).items():
            files[f"{folder}/{rel}"] = data
        files[f"{file_name(report.name)}.pbip"] = json_bytes({
            "$schema": PBIP_SCHEMA, "version": "1.0",
            "artifacts": [{"report": {"path": folder}}],
            "settings": {"enableAutoRecovery": True}})
    return files


def write_project(model: Model, reports: tuple[Report, ...], dest: Path, *,
                  connector: str = DEFAULT_CONNECTOR) -> list[Path]:
    dest = Path(dest)
    files = project_files(model, tuple(reports), connector=connector)
    for folder in {rel.split("/", 1)[0] for rel in files if "/" in rel}:
        stale = dest / folder / "definition"
        if stale.is_dir():
            shutil.rmtree(stale)
    written = []
    for rel in sorted(files):
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files[rel])
        written.append(path)
    return written


def load_folder(folder: Path) -> dict[str, bytes]:
    """Every file under `folder` (relative posix paths), minus Desktop's local
    state."""
    out = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            rel = path.relative_to(folder).as_posix()
            if not rel.startswith(LOCAL_ONLY):
                out[rel] = path.read_bytes()
    return out


def read_model_folder(folder: Path) -> tuple[Model, tmdl.SemanticModelInfo]:
    if not folder.is_dir():
        raise ModelError(f"semantic model folder {folder} does not exist")
    return tmdl.read_semantic_model(load_folder(folder), folder.name.removesuffix(
        ".SemanticModel"))


def report_target(report_folder: Path) -> Path:
    files = {"definition.pbir": (report_folder / "definition.pbir").read_bytes()} \
        if (report_folder / "definition.pbir").is_file() else {}
    rel = pbir.dataset_path(files)
    return Path(posixpath.normpath((report_folder / rel).as_posix()))


def read_report_folder(folder: Path, model: Model, info) -> Report:
    return pbir.read_report(load_folder(folder), model, info,
                            folder.name.removesuffix(".Report"))


def pbip_report(pbip: Path) -> Path:
    doc = json_document(pbip.read_bytes(), pbip.name)
    for artifact in doc.get("artifacts", []):
        path = (artifact.get("report") or {}).get("path")
        if path:
            return Path(posixpath.normpath((pbip.parent / path).as_posix()))
    raise ModelError(f"{pbip.name} references no report")


def read_project(path: Path, *, warehouse: Path | None = None
                 ) -> tuple[Model, tuple[Report, ...]]:
    """`warehouse` is accepted for the common front-end signature
    (world.semantic) and unused: TMDL carries every column's data type."""
    path = Path(path)
    if path.is_file() and path.suffix.lower() == ".pbip":
        report_folders = [pbip_report(path)]
    elif path.is_dir() and path.name.endswith(".Report"):
        report_folders = [path]
    elif path.is_dir() and path.name.endswith(".SemanticModel"):
        model, _ = read_model_folder(path)
        return model, ()
    elif path.is_dir():
        pbips = sorted(path.glob("*.pbip"))
        if not pbips:
            models = sorted(p for p in path.glob("*.SemanticModel") if p.is_dir())
            if len(models) != 1:
                raise ModelError(f"{path} holds no .pbip and {len(models)} semantic models; "
                                 f"point at one project")
            model, _ = read_model_folder(models[0])
            return model, ()
        report_folders = [pbip_report(p) for p in pbips]
    else:
        raise ModelError(f"{path} is neither a .pbip file nor a project folder")
    targets = {report_target(folder) for folder in report_folders}
    if len(targets) != 1:
        raise ModelError("the reports are bound to different semantic models: "
                         + ", ".join(sorted(t.name for t in targets)))
    model, info = read_model_folder(targets.pop())
    reports = tuple(read_report_folder(folder, model, info) for folder in report_folders)
    return model, reports


__all__ = ["load_folder", "project_files", "read_project", "write_project"]
