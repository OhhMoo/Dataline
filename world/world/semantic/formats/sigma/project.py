"""The repo layout of a Sigma project, and the four front-end functions.

A team that version-controls Sigma commits what the API returns for each
document (the phData "Sigma API to Git" pattern commits the workbook spec
`workbooks/<name>.yaml`; Sigma's own docs/manage-workbooks-as-code and
docs/manage-data-models-as-code name the endpoints). This project commits JSON:

    <workspace>/bi/sigma/
      data-models/<name>.json   GET /v2/dataModels/{dataModelId}/spec
      workbooks/<name>.json     GET /v2/workbooks/{workbookId}?includeContents=true
      connections/<name>.json   GET /v2/connections/{connectionId} + synced tables
                                (catalog.py: why, and the one added key)

`write_project` writes all three, deterministic to the byte: keys sorted,
two-space indent, ids from names, constant timestamps. It does not delete
other files under `dest`. `read_project` reads exactly one data model and every
workbook built on it; reports come back ordered by workbook name.
"""
from __future__ import annotations

import json
from pathlib import Path

from ...errors import ModelError
from ...model import Model, Report
from ._common import CONNECTION_NAME, slug
from .catalog import Catalog, connection_document
from .datamodel import read_data_model, write_data_model
from .workbook import contents_of, read_workbook, write_workbook

DATA_MODELS = "data-models"
WORKBOOKS = "workbooks"
CONNECTIONS = "connections"


def dumps(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ModelError(f"{path} is not JSON: {exc}") from None


def read_documents(data_model: dict, workbooks: list[dict],
                   catalog: Catalog) -> tuple[Model, tuple[Report, ...]]:
    """Model and reports from in-memory documents (the fake service's
    published state, or a project's files)."""
    model, index = read_data_model(data_model, catalog)
    reports = []
    for doc in sorted(workbooks, key=lambda d: d.get("name", "")):
        elements = contents_of(doc).get("elements", [])
        sources = {(el.get("source") or {}).get("dataModelId") for el in elements} - {None}
        if sources and index.data_model_id not in sources:
            continue  # a workbook on another data model
        reports.append(read_workbook(doc, model, index))
    return model, tuple(reports)


def read_project(path: Path, *, warehouse: Path | None = None
                 ) -> tuple[Model, tuple[Report, ...]]:
    """`path` is the project directory (`<workspace>/bi/sigma`). Column types
    come from the connection snapshots there, overridden by `warehouse` (a
    DuckDB file) when given - Sigma's files carry none (catalog.py)."""
    path = Path(path)
    models = sorted((path / DATA_MODELS).glob("*.json"))
    if len(models) != 1:
        raise ModelError(f"{path / DATA_MODELS} holds {len(models)} data models; "
                         f"a Sigma project here has exactly one")
    catalog = Catalog.from_snapshots([_load(p) for p in
                                      sorted((path / CONNECTIONS).glob("*.json"))])
    if warehouse is not None:
        catalog = catalog.merged(Catalog.from_warehouse(Path(warehouse)))
    workbooks = [_load(p) for p in sorted((path / WORKBOOKS).glob("*.json"))]
    return read_documents(_load(models[0]), workbooks, catalog)


def write_project(model: Model, reports: tuple[Report, ...], dest: Path) -> list[Path]:
    dest = Path(dest)
    files = {dest / DATA_MODELS / f"{slug(model.name)}.json": write_data_model(model),
             dest / CONNECTIONS / f"{slug(CONNECTION_NAME)}.json": connection_document(model)}
    for report in reports:
        target = dest / WORKBOOKS / f"{slug(report.name)}.json"
        if target in files:
            raise ModelError(f"two reports write {target.name}")
        files[target] = write_workbook(report, model)
    written = []
    for target, doc in files.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dumps(doc), encoding="utf-8")
        written.append(target)
    return sorted(written)
