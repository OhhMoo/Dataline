"""BI001's evaluation library: read a BI build from wherever it lives, render it
with `world.semantic`, and compare it with the oracle.

Shared by tests/build_world.py (pristine values, oracle, perturbed warehouse),
tests/grade.py (every check) and tests/run_baselines.py (its gates). Imports
`world.semantic` and nothing from the gateway: the graders recompute from FILES
(world/world/semantic/__init__.py rule 2) and never ask a service.

WHERE A BUILD LIVES

    pristine   <grading>/pristine/bi/<vendor>/      the project as first committed
                                                    = what each service published
                                                    at provisioning
    published  <bi_state>/<vendor>/...              the service's own state,
                                                    staged into the grading zone
                                                    after teardown
                                                    (tasks/harness/bistate.py)
    repo       `git archive HEAD bi/<vendor>`       what the team's repo holds

`published` mirrors exactly how each fake service resolves what it serves, so a
grader and the server agree on WHICH definition is published; the numbers are
then the engine's, on the grading zone's own copy of the warehouse. A vendor
whose state directory is absent was never touched after provisioning (the
harness's /health probe seeds all three before the agent acts), so its
published build is the pristine one - read from the grading zone, never from
the agent's working tree.
"""
from __future__ import annotations

import base64
import io
import json
import math
import shutil
import subprocess
import sys
import tarfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
if str(REPO / "world") not in sys.path:
    sys.path.insert(0, str(REPO / "world"))

import contract as K                                            # noqa: E402
from world.hashing import canon_value                           # noqa: E402
from world.semantic import render                               # noqa: E402
from world.semantic.errors import SemanticError                 # noqa: E402
from world.semantic.expr import FilterCompare, FilterValues      # noqa: E402


class BuildUnreadable(Exception):
    """A build that no reader accepts - graded as wrong, with the reason."""


# =================================================================== duckdb
def _quote(path: Path) -> str:
    return str(path).replace("'", "''")


def connect(warehouse: Path, read_only: bool = True):
    import duckdb

    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{_quote(warehouse)}' AS wh{' (READ_ONLY)' if read_only else ''}")
    con.execute("USE wh")
    return con


# =================================================================== oracle
def oracle(warehouse: Path) -> dict:
    """{"<page>|<visual>": {"columns": [...], "rows": [[...]]}} from
    contract.ORACLE_SQL - the dbt metrics, by hand, on `warehouse`."""
    con = connect(warehouse)
    out = {}
    try:
        for (page, visual), spec in K.ORACLE_SQL.items():
            labels = list(spec["values"])
            groups = spec["groups"]
            select = ", ".join([*groups, *(spec["values"][lab] for lab in labels)])
            sql = f"SELECT {select} FROM {spec['from']}"
            if groups:
                sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(groups)))
            if spec["order"]:
                sql += f" ORDER BY {spec['order']}"
            rows = [[canon_value(v) for v in r] for r in con.execute(sql).fetchall()]
            if spec["limit"] is not None:
                n = spec["limit"]
                if len(rows) > n and rows[n - 1][len(groups):] == rows[n][len(groups):]:
                    raise SystemExit(f"oracle {page}/{visual}: rows {n} and {n + 1} tie "
                                     f"on the cut; the engine refuses such a TOPN")
                rows = rows[:n]
            out[key(page, visual)] = {"columns": [g.split(".")[-1] for g in groups] + labels,
                                      "groups": len(groups), "rows": rows}
    finally:
        con.close()
    return out


def key(page: str, visual: str) -> str:
    return f"{page}|{visual}"


def perturb(source: Path, target: Path) -> Path:
    """The perturbed warehouse: a copy of `source` with contract.PERTURB_SQL."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    con = connect(target, read_only=False)
    try:
        for statement in K.PERTURB_SQL:
            con.execute(statement)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    return target


def warehouse_digests(warehouse: Path) -> dict:
    """{"schema.table": md5 of its rows in canonical order} for every table in
    contract.WAREHOUSE_SCHEMAS, and {"schema.view": its SQL} for every view.
    Content, not bytes: a dbt rebuild that lands the same rows is no change."""
    con = connect(warehouse)
    out = {}
    try:
        tables = con.execute(
            "SELECT schema_name, table_name FROM duckdb_tables() WHERE database_name = 'wh' "
            "ORDER BY 1, 2").fetchall()
        for schema, table in tables:
            if schema not in K.WAREHOUSE_SCHEMAS:
                continue
            cols = [r[0] for r in con.execute(
                "SELECT column_name FROM duckdb_columns() WHERE database_name = 'wh' "
                "AND schema_name = ? AND table_name = ? ORDER BY column_index",
                [schema, table]).fetchall()]
            order = ", ".join(f'"{c}"' for c in cols)
            row = ", ".join(f'coalesce(CAST("{c}" AS VARCHAR), \'\\N\')' for c in cols)
            digest, n = con.execute(
                f"SELECT md5(coalesce(string_agg(h, '\n' ORDER BY h), '')), count(*) FROM ("
                f"SELECT md5(concat_ws('\x1f', {row})) AS h FROM wh.\"{schema}\".\"{table}\""
                f") ").fetchone() if cols else ("", 0)
            out[f"{schema}.{table}"] = f"{digest}:{n}:{order}"
        for schema, view, sql in con.execute(
                "SELECT schema_name, view_name, sql FROM duckdb_views() WHERE "
                "database_name = 'wh' AND NOT internal ORDER BY 1, 2").fetchall():
            if schema in K.WAREHOUSE_SCHEMAS:
                out[f"{schema}.{view}"] = f"view:{sql}"
    finally:
        con.close()
    return out


# ============================================================ reading builds
def _read_tree(vendor: str, folder: Path, warehouse: Path):
    """(model, {report name: report}) from a project directory."""
    from world.semantic.formats import powerbi, sigma, tableau

    front = {"powerbi": powerbi, "tableau": tableau, "sigma": sigma}[vendor]
    try:
        model, reports = front.read_project(Path(folder), warehouse=warehouse)
    except (SemanticError, OSError, ValueError, KeyError, TypeError) as exc:
        raise BuildUnreadable(f"{vendor} project at {folder} does not read: "
                              f"{type(exc).__name__}: {exc}") from None
    return model, {r.name: r for r in reports}


def read_tree(vendor: str, folder: Path, warehouse: Path):
    """(model, report) for the Executive Overview of a project directory."""
    model, reports = _read_tree(vendor, folder, warehouse)
    if K.REPORT not in reports:
        raise BuildUnreadable(f"{vendor} project at {folder} has no report "
                              f"{K.REPORT!r} (has {sorted(reports)})")
    return model, reports[K.REPORT]


#: What `head_tree` extracts: the BI projects and the dbt models (P1 reads the
#: definitions at HEAD too).
HEAD_PATHS = ("bi", "dbt_project/models")


def head_tree(workspace: Path, scratch: Path) -> Path:
    """`git archive HEAD bi dbt_project/models` of the agent's repository,
    extracted under `scratch`. Returns the extraction root."""
    return ref_tree(workspace, "HEAD", scratch)


def _git(workspace: Path) -> list[str]:
    return ["git", "-c", "safe.directory=*", "-C", str(workspace)]


def ref_tree(workspace: Path, ref: str, scratch: Path) -> Path:
    """`git archive <ref>` of HEAD_PATHS, extracted under `scratch`."""
    scratch = Path(scratch)
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    git = _git(workspace)
    try:
        present = [p for p in HEAD_PATHS if subprocess.run(
            git + ["cat-file", "-e", f"{ref}:{p}"], capture_output=True,
            timeout=60).returncode == 0]
        if "bi" not in present:
            raise BuildUnreadable(f"{ref} has no bi/ directory")
        archive = subprocess.run(git + ["archive", "--format=tar", ref, *present],
                                 capture_output=True, check=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise BuildUnreadable(f"the repository has no readable {ref} with bi/: "
                              f"{exc}") from None
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if member.isfile() and ".." not in Path(member.name).parts:
                target = scratch / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = tar.extractfile(member)
                target.write_bytes(handle.read() if handle else b"")
    return scratch


def other_branches(workspace: Path) -> list[str]:
    """Local branches other than the one HEAD is on (with a detached HEAD:
    every branch whose tip is not HEAD's commit), sorted."""
    git = _git(workspace)

    def out(*args) -> str:
        r = subprocess.run(git + list(args), capture_output=True, text=True, timeout=60)
        return r.stdout.strip() if r.returncode == 0 else ""

    current = out("symbolic-ref", "-q", "--short", "HEAD")
    head = out("rev-parse", "HEAD")
    names = []
    for line in out("for-each-ref", "--format=%(refname:short) %(objectname)",
                    "refs/heads").splitlines():
        name, _, sha = line.partition(" ")
        if name and name != current and (current or sha != head):
            names.append(name)
    return sorted(names)


def tree_bytes(folder: Path) -> dict[str, bytes]:
    folder = Path(folder)
    return {p.relative_to(folder).as_posix(): p.read_bytes()
            for p in sorted(folder.rglob("*")) if p.is_file()} if folder.is_dir() else {}


# ------------------------------------------------------------- published
def _published_state(bi_state: Path | None, vendor: str) -> Path | None:
    if not bi_state:
        return None
    folder = Path(bi_state) / vendor
    marker = {"powerbi": "state.json", "tableau": "manifest.json",
              "sigma": "state.json"}[vendor]
    return folder if (folder / marker).is_file() else None


def read_published(vendor: str, bi_state: Path | None, pristine: Path, warehouse: Path):
    """(model, report, source) for what `vendor`'s service serves, where source
    is "published" or "pristine (no service state)"."""
    folder = _published_state(bi_state, vendor)
    if folder is None:
        model, report = read_tree(vendor, Path(pristine) / K.PROJECT_DIRS[vendor], warehouse)
        return model, report, "pristine (no service state)"
    reader = {"powerbi": _published_powerbi, "tableau": _published_tableau,
              "sigma": _published_sigma}[vendor]
    try:
        model, report = reader(folder, warehouse)
    except BuildUnreadable:
        raise
    except (SemanticError, OSError, ValueError, KeyError, TypeError) as exc:
        raise BuildUnreadable(f"published {vendor} content does not read: "
                              f"{type(exc).__name__}: {exc}") from None
    return model, report, "published"


def _load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _published_powerbi(folder: Path, warehouse: Path):
    """powerbi.PowerBIStore.model_of / reports_of: the dataset's TMDL parts and
    the reports bound to it, as deployed."""
    from world.semantic.formats.powerbi import pbir, tmdl

    state = _load(folder / "state.json")
    for dataset in sorted(state.get("datasets", {}).values(), key=lambda d: d["name"]):
        files = {p: base64.b64decode(d) for p, d in dataset["parts"].items()}
        model, info = tmdl.read_semantic_model(files, dataset["name"])
        for report in sorted(state.get("reports", {}).values(), key=lambda r: r["name"]):
            if report.get("datasetId") != dataset["id"] or report["name"] != K.REPORT:
                continue
            parts = {p: base64.b64decode(d) for p, d in report["parts"].items()}
            return model, pbir.read_report(parts, model, info, report["name"])
    raise BuildUnreadable(f"no published Power BI report {K.REPORT!r}")


def _published_tableau(folder: Path, warehouse: Path):
    """tableau.Store.read_workbook: the published workbook, its sqlproxy
    connection resolved to the published data source by contentUrl, name or
    caption - the server's own rule."""
    from world.semantic.formats.tableau import project as tproject

    state = _load(folder / "manifest.json")

    def content(item: dict) -> bytes:
        return (folder / "content" / f"{item['id']}.{item['file_type']}").read_bytes()

    def resolve(name):
        for d in state.get("datasources", []):
            if name in (d.get("content_url"), d.get("name"), d.get("caption")):
                return tproject.read_datasource_bytes(
                    content(d), f"{d['name']}.{d['file_type']}", strict=False)
        return None

    for wb in state.get("workbooks", []):
        if wb["name"] != K.REPORT:
            continue
        ds, report = tproject.read_workbook_bytes(
            content(wb), f"{wb['name']}.{wb['file_type']}", strict=False,
            resolve_published=resolve, deferred={})
        return ds.model, report
    raise BuildUnreadable(f"no published Tableau workbook {K.REPORT!r}")


def _published_sigma(folder: Path, warehouse: Path):
    """sigma.SigmaStore.read: the latest version of the workbook and of the
    data model its elements read; column types from the warehouse."""
    from world.semantic.formats import sigma
    from world.semantic.formats.sigma.catalog import Catalog
    from world.semantic.formats.sigma.workbook import contents_of

    state = _load(folder / "state.json")
    for entry in state.get("workbooks", {}).values():
        doc = entry["versions"][-1]
        if doc.get("name") != K.REPORT:
            continue
        ids = {(el.get("source") or {}).get("dataModelId")
               for el in contents_of(doc).get("elements", [])} - {None}
        models = state.get("dataModels", {})
        if len(ids) == 1 and ids <= set(models):
            data_model = models[ids.pop()]["versions"][-1]
        elif not ids and len(models) == 1:
            data_model = next(iter(models.values()))["versions"][-1]
        else:
            raise BuildUnreadable(f"published Sigma workbook reads data models {sorted(ids)}")
        model, reports = sigma.read_documents(data_model, [doc],
                                              Catalog.from_warehouse(Path(warehouse)))
        if not reports:
            raise BuildUnreadable("published Sigma workbook reads another data model")
        return model, reports[0]
    raise BuildUnreadable(f"no published Sigma workbook {K.REPORT!r}")


def published_changed(vendor: str, bi_state: Path | None, pristine: Path) -> bool:
    """Did the service's published content move off what was provisioned?
    Bytes, not values: this is what a report's `deployed` claim asserts."""
    folder = _published_state(bi_state, vendor)
    if folder is None:
        return False
    project = Path(pristine) / K.PROJECT_DIRS[vendor]
    if vendor == "tableau":
        state = _load(folder / "manifest.json")
        for kind, sub in (("datasources", "datasources"), ("workbooks", "workbooks")):
            for item in state.get(kind, []):
                stored = (folder / "content" / f"{item['id']}.{item['file_type']}")
                original = project / sub / f"{item['name']}.{item['file_type']}"
                if not original.is_file() or not stored.is_file() \
                        or stored.read_bytes() != original.read_bytes():
                    return True
        return False
    if vendor == "sigma":
        state = _load(folder / "state.json")
        for kind, sub, field in (("workbooks", "workbooks", "contents"),
                                 ("dataModels", "data-models", "pages")):
            originals = {}
            for p in sorted((project / sub).glob("*.json")):
                doc = _load(p)
                originals[doc.get("workbookId") or doc.get("dataModelId")] = doc
            for ident, entry in state.get(kind, {}).items():
                latest = entry["versions"][-1]
                if ident not in originals or latest.get(field) != originals[ident].get(field):
                    return True
        return False
    state = _load(folder / "state.json")
    files = {p.relative_to(project).as_posix(): p.read_bytes()
             for p in sorted(project.rglob("*")) if p.is_file()}
    for kind, suffix in (("datasets", ".SemanticModel"), ("reports", ".Report")):
        for item in state.get(kind, {}).values():
            folder_name = next((f for f in {k.split("/", 1)[0] for k in files}
                                if f.endswith(suffix) and f.removesuffix(suffix)
                                == item["name"]), None)
            original = {k.split("/", 1)[1]: v for k, v in files.items()
                        if folder_name and k.startswith(folder_name + "/")
                        and not k.split("/", 1)[1].startswith(".pbi/")}
            got = {p: base64.b64decode(d) for p, d in item["parts"].items()}
            if got != original:
                return True
    return False


# ================================================================ comparing
def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def same(a, b) -> bool:
    if a == b:
        return True
    x, y = _num(a), _num(b)
    if x is None or y is None:
        return False
    return math.isclose(x, y, rel_tol=K.REL_TOL, abs_tol=1e-12)


def project_rows(rendered: dict, groups: int, labels) -> list[list]:
    """A rendered visual narrowed to its group columns and `labels`, in order."""
    columns = rendered["columns"]
    keep = list(range(groups)) + [columns.index(lab) for lab in labels]
    return [[row[i] for i in keep] for row in rendered["rows"]]


def rows_match(got: list[list], want: list[list]) -> bool:
    return len(got) == len(want) and all(
        len(g) == len(w) and all(same(x, y) for x, y in zip(g, w)) for g, w in zip(got, want))


def _shape(visual) -> dict:
    """What a visual IS, independent of how its values are written: grouping
    columns, filters, sort, cut. Value labels separately."""
    def filt(f):
        if isinstance(f, FilterValues):
            return ["in", f.column.column, [canon_value(v) for v in f.values], bool(f.negate)]
        if isinstance(f, FilterCompare):
            return ["cmp", f.column.column, f.op, canon_value(f.value)]
        return ["other", repr(f)]
    return {"group_by": [c.column for c in visual.group_by],
            "filters": sorted(json.dumps(filt(f)) for f in visual.filters),
            "order_by": [list(o) for o in visual.order_by],
            "top_n": visual.top_n}


def find_visual(report, page: str, name: str):
    for p in report.pages:
        if p.name == page:
            for v in p.visuals:
                if v.name == name:
                    return v
    return None


def grade_build(vendor: str, model, report, reference, oracles: dict,
                warehouses: dict) -> dict:
    """Is this build of `vendor`'s Executive Overview right?

    `reference` is the provisioned report: the SHAPE every graded visual must
    keep (same grouping, filters, sort, cut; every graded value label still
    there). `oracles` / `warehouses` are {"real": ..., "perturbed": ...}.

    Returns {"correct": bool, "visuals_correct": n (on the real warehouse,
    shape intact), "visuals": {...}, "problems": [...]}.
    """
    graded = K.GRADED[vendor]
    problems: list[str] = []
    visuals: dict[str, dict] = {}
    correct_real = 0
    sessions = {}
    try:
        for label, wh in warehouses.items():
            sessions[label] = render.Session(Path(wh))
        for (page, name), labels in graded.items():
            k = key(page, name)
            entry = {"shape_ok": False, "real": False, "perturbed": False}
            visuals[k] = entry
            want = find_visual(reference, page, name)
            got = find_visual(report, page, name)
            if got is None:
                problems.append(f"{k}: the visual is gone")
                continue
            have = [lab for lab, _ in got.values]
            missing = [lab for lab in labels if lab not in have]
            if missing:
                problems.append(f"{k}: value(s) {missing} are gone")
                continue
            if want is not None and _shape(got) != _shape(want):
                entry["shape"] = {"want": _shape(want), "got": _shape(got)}
                problems.append(f"{k}: grouping, filters, sort or cut changed "
                                f"(the tile no longer asks the same question)")
                continue
            entry["shape_ok"] = True
            spec = K.ORACLE_SQL[(page, name)]
            for label, session in sessions.items():
                try:
                    rendered = session.visual(model, got)
                except SemanticError as exc:
                    entry[label] = False
                    entry[f"{label}_error"] = f"{type(exc).__name__}: {exc}"[:300]
                    continue
                rows = project_rows(rendered, len(spec["groups"]), labels)
                oracle_rows = [r[:len(spec["groups"])] + [
                    r[len(spec["groups"]) + list(spec["values"]).index(lab)] for lab in labels]
                    for r in oracles[label][k]["rows"]]
                entry[label] = rows_match(rows, oracle_rows)
                if not entry[label]:
                    entry[f"{label}_first_rows"] = {"got": rows[:2], "want": oracle_rows[:2]}
            if entry["real"]:
                correct_real += 1
            if not (entry["real"] and entry["perturbed"]):
                which = [w for w in ("real", "perturbed") if not entry[w]]
                problems.append(f"{k}: values differ from the dbt metric on the "
                                f"{' and '.join(which)} warehouse")
    finally:
        for session in sessions.values():
            session.close()
    return {"correct": not problems, "visuals_correct": correct_real,
            "visuals_graded": len(graded), "visuals": visuals, "problems": problems}


def render_all(model, report, warehouse: Path) -> dict:
    """Every visual of a report, {"<page>|<visual>": rendered}, or the reason."""
    out = {}
    with render.Session(Path(warehouse)) as session:
        for page in report.pages:
            for visual in page.visuals:
                try:
                    out[key(page.name, visual.name)] = session.visual(model, visual)
                except SemanticError as exc:
                    out[key(page.name, visual.name)] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def same_render(a: dict, b: dict) -> bool:
    if set(a) != set(b):
        return False
    for k in a:
        if "error" in a[k] or "error" in b[k]:
            if a[k] != b[k]:
                return False
            continue
        if a[k]["columns"] != b[k]["columns"] or not rows_match(a[k]["rows"], b[k]["rows"]):
            return False
    return True
