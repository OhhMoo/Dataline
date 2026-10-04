"""Where Tableau content lives on disk, and the four front-end functions.

REPO CONVENTION. A data engineer keeps Tableau content as text in git:

    <workspace>/bi/tableau/
        datasources/<Data Source>.tds     the published data source(s): the model
        workbooks/<Workbook>.twb          workbooks connected to them (sqlproxy)

`read_project` accepts that folder (any layout under it: every .tds/.tdsx and
.twb/.twbx found, Tableau's `~*.twbr` recovery files and `*.twb~` backups
skipped), a single .twb/.twbx (its published data source is looked up beside it:
the same folder, then `../datasources/`), or a single .tds/.tdsx. The contract
returns ONE model, so a folder must hold one data source (or none, when every
workbook embeds the same one); several distinct ones are a ModelError naming
them, never a silent pick.

Packaged files (.twbx/.tdsx) are zip archives with the .twb/.tds at the top;
any extract (.hyper) inside is ignored - see tds.py's EXTRACTS note.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from ...errors import ModelError
from ...expr import Expr
from ...model import Model, Report
from . import calc, tds, twb
from .xmlio import parse, serialize

WORKBOOK_SUFFIXES = (".twb", ".twbx")
DATASOURCE_SUFFIXES = (".tds", ".tdsx")


def document_bytes(data: bytes, filename: str) -> bytes:
    """The XML of a Tableau file: a .twb/.tds as is, the one document at the top
    of a .twbx/.tdsx archive otherwise."""
    if not data.startswith(b"PK"):
        return data
    want = ".tds" if Path(filename).suffix.lower() in (".tds", ".tdsx") else ".twb"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            top = [n for n in z.namelist() if "/" not in n and n.lower().endswith(want)]
            if len(top) != 1:
                raise ModelError(f"{filename}: a packaged Tableau file holds exactly one "
                                 f"{want} at its top level, found {top}")
            return z.read(top[0])
    except zipfile.BadZipFile:
        raise ModelError(f"{filename}: not a valid packaged Tableau file") from None


def read_datasource_bytes(data: bytes, filename: str, *, strict: bool = True) -> tds.DataSource:
    root = parse(document_bytes(data, filename), filename)
    if root.tag != "datasource":
        raise ModelError(f"{filename}: a .tds holds a <datasource>, found <{root.tag}>")
    return tds.read(root, name_hint=Path(filename).stem, strict=strict)


def read_workbook_bytes(data: bytes, filename: str, *, strict: bool = True,
                        resolve_published=None, deferred: dict | None = None
                        ) -> tuple[tds.DataSource, Report]:
    root = parse(document_bytes(data, filename), filename)
    if root.tag != "workbook":
        raise ModelError(f"{filename}: a .twb holds a <workbook>, found <{root.tag}>")
    return twb.read(root, name=Path(filename).stem, strict=strict,
                    resolve_published=resolve_published, deferred=deferred)


def _skip(path: Path) -> bool:
    return (path.name.startswith(("~", ".")) or path.name.endswith("~")
            or any(part.startswith(".") for part in path.parts))


def _keys(ds: tds.DataSource, stem: str) -> set[str]:
    return {ds.caption, stem, tds.content_url(ds.caption), tds.content_url(stem)}


def read_project(path: Path, *, warehouse: Path | None = None,
                 strict: bool = True) -> tuple[Model, tuple[Report, ...]]:
    """`warehouse` is accepted for the common front-end signature and unused:
    a Tableau data source carries its column types in <metadata-records>."""
    path = Path(path)
    if path.is_file():
        suffix = path.suffix.lower()
        if suffix in DATASOURCE_SUFFIXES:
            return read_datasource_bytes(path.read_bytes(), path.name, strict=strict).model, ()
        if suffix in WORKBOOK_SUFFIXES:
            published = _published_near(path, strict)
            ds, report = read_workbook_bytes(path.read_bytes(), path.name, strict=strict,
                                             resolve_published=published)
            return ds.model, (report,)
        raise ModelError(f"{path}: not a Tableau workbook or data source")
    if not path.is_dir():
        raise ModelError(f"{path}: no such file or folder")
    files = sorted(p for p in path.rglob("*") if p.is_file() and not _skip(p.relative_to(path)))
    sources: list[tuple[Path, tds.DataSource]] = []
    for p in files:
        if p.suffix.lower() in DATASOURCE_SUFFIXES:
            sources.append((p, read_datasource_bytes(p.read_bytes(), p.name, strict=strict)))
    distinct = {ds.model for _, ds in sources}
    if len(distinct) > 1:
        raise ModelError(f"{path} holds {len(sources)} different data sources "
                         f"({', '.join(str(p.relative_to(path)) for p, _ in sources)}); read "
                         f"one of them, or one workbook, instead")
    lookup = {}
    for p, ds in sources:
        for key in _keys(ds, p.stem):
            lookup[key] = ds
    models = set(distinct)
    reports = []
    for p in files:
        if p.suffix.lower() in WORKBOOK_SUFFIXES:
            ds, report = read_workbook_bytes(p.read_bytes(), p.name, strict=strict,
                                             resolve_published=lookup.get)
            models.add(ds.model)
            reports.append(report)
    if not models:
        raise ModelError(f"{path} holds no Tableau data source or workbook")
    if len(models) > 1:
        raise ModelError(f"{path}: its workbooks embed data sources that differ from each "
                         f"other or from the published one; a project has one model")
    return models.pop(), tuple(reports)


def _published_near(path: Path, strict: bool):
    """A resolver for the published data source of a lone workbook."""
    found: dict[str, tds.DataSource] = {}
    for folder in (path.parent, path.parent.parent / "datasources", path.parent / "datasources"):
        if not folder.is_dir():
            continue
        for p in sorted(folder.iterdir()):
            if p.is_file() and p.suffix.lower() in DATASOURCE_SUFFIXES and not _skip(
                    Path(p.name)):
                ds = read_datasource_bytes(p.read_bytes(), p.name, strict=strict)
                for key in _keys(ds, p.stem):
                    found.setdefault(key, ds)
    return found.get


def write_project(model: Model, reports: tuple[Report, ...], dest: Path, *,
                  warehouse: str = "duckdb", extract: bool = False,
                  server: str = "tableau.internal", port: int = 8096) -> list[Path]:
    """Write `<dest>/datasources/<model>.tds` and `<dest>/workbooks/<report>.twb`.

    `warehouse` picks the connection class (tds.CONNECTIONS: duckdb, postgres,
    snowflake, redshift, databricks, bigquery); `extract` marks the data source as
    an extract (refreshable on the server); `server`/`port` are where the
    workbooks find the published data source. Deterministic: same inputs, same
    bytes."""
    if warehouse not in tds.CONNECTIONS:
        raise ModelError(f"unknown warehouse {warehouse!r}; one of "
                         f"{', '.join(sorted(tds.CONNECTIONS))}")
    dest = Path(dest)
    written = []
    ds_path = dest / "datasources" / f"{model.name}.tds"
    ds_path.parent.mkdir(parents=True, exist_ok=True)
    ds_path.write_bytes(serialize(tds.write(model, tds.CONNECTIONS[warehouse], root=True,
                                            extract=extract)))
    written.append(ds_path)
    names = [r.name for r in reports]
    if len(set(names)) != len(names):
        raise ModelError("two reports with one name would be one workbook file")
    for report in reports:
        wb_path = dest / "workbooks" / f"{report.name}.twb"
        wb_path.parent.mkdir(parents=True, exist_ok=True)
        wb_path.write_bytes(serialize(twb.write(model, report, server=server, port=port)))
        written.append(wb_path)
    return sorted(written)


def parse_expression(text: str, model: Model, *, table: str | None = None) -> Expr:
    return calc.parse_expression(text, model, table=table)


def format_expression(expr: Expr, model: Model, *, table: str | None = None) -> str:
    return calc.format_expression(expr, model, table=table)
