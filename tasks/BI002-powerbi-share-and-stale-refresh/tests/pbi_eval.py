"""BI002's evaluation library: read the Power BI build from wherever it lives,
render it with `world.semantic`, and compare it with the oracle.

Shared by tests/build_world.py (pristine values, oracle, perturbed warehouse),
tests/grade.py (every check) and tests/run_baselines.py (its gates). Imports
`world.semantic`, and from the gateway only `alkera_gateway.safegit` (stdlib:
reads the agent's git repository without running anything its .git configures):
the graders recompute from FILES (world/world/semantic/__init__.py rule 2) and
never ask a service.

WHERE THE BUILD LIVES

    pristine   <grading>/pristine/bi/powerbi/        the project as first committed
                                                     = what the service published
                                                     at provisioning
    published  <bi_state>/powerbi/state.json         the service's own state, staged
                                                     into the grading zone after
                                                     teardown (tasks/harness/
                                                     bistate.py): the dataset's
                                                     definition parts as last
                                                     deployed, the report, the
                                                     refresh history
    repo       HEAD:bi/powerbi (safegit.read_tree)   what the team's repo holds

`published` mirrors how the service resolves what it serves (PowerBIStore
model_of / reports_of), so the grader and the server agree on WHICH model is
published; the numbers are the engine's, on the grading zone's own warehouse
copies. The import snapshot the service answers from is a cache bistate leaves
behind on purpose, so what the import HOLDS is read from the refresh history
the service kept: when each refresh ran, whether it completed, and the visuals
it rendered on the snapshot it had just loaded.
"""
from __future__ import annotations

import base64
import json
import math
import shutil
import sys
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
if str(REPO / "world") not in sys.path:
    sys.path.insert(0, str(REPO / "world"))

import contract as K                                            # noqa: E402
from alkera_gateway import safegit                              # noqa: E402
from world.hashing import canon_value                           # noqa: E402
from world.semantic import render                               # noqa: E402
from world.semantic.errors import SemanticError                 # noqa: E402
from world.semantic.expr import Col, FilterCompare, FilterValues, MeasureRef  # noqa: E402
from world.semantic.model import Visual                         # noqa: E402


class BuildUnreadable(Exception):
    """A build no reader accepts - graded as wrong, with the reason."""


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
def key(page: str, visual: str) -> str:
    return f"{page}|{visual}"


def oracle(warehouse: Path) -> dict:
    """{"<page>|<visual>": {"columns", "groups", "rows"}} from contract.ORACLE_SQL."""
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


def latest(oracles: dict, which: str = "real") -> Decimal:
    """February 2026's Net Revenue (contract.LATEST) from the oracle."""
    (page, visual), group, label = K.LATEST
    rendered = oracles[which][key(page, visual)]
    n = rendered["groups"]
    for row in rendered["rows"]:
        if tuple(row[:n]) == tuple(group):
            return Decimal(str(row[rendered["columns"].index(label)]))
    raise SystemExit(f"the oracle has no {group} row in {visual}")


def headline(oracles: dict, which: str = "real") -> Decimal:
    (page, visual), label = K.HEADLINE
    rendered = oracles[which][key(page, visual)]
    return Decimal(str(rendered["rows"][0][rendered["columns"].index(label)]))


def perturb(source: Path, target: Path) -> Path:
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
    contract.WAREHOUSE_SCHEMAS, and {"schema.view": its SQL} for every view."""
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
def read_tree(folder: Path, warehouse: Path):
    """(model, report) for the Executive Overview of a PBIP project directory."""
    from world.semantic.formats import powerbi

    try:
        model, reports = powerbi.read_project(Path(folder), warehouse=warehouse)
    except (SemanticError, OSError, ValueError, KeyError, TypeError) as exc:
        raise BuildUnreadable(f"Power BI project at {folder} does not read: "
                              f"{type(exc).__name__}: {exc}") from None
    by_name = {r.name: r for r in reports}
    if K.REPORT not in by_name:
        raise BuildUnreadable(f"Power BI project at {folder} has no report {K.REPORT!r} "
                              f"(has {sorted(by_name)})")
    return model, by_name[K.REPORT]


HEAD_PATHS = ("bi", "dbt_project/models", "docs")


# THE AGENT OWNS THIS REPOSITORY'S .git, and the grader runs on the host as the
# host user: it is read only through alkera_gateway.safegit (ls-tree + cat-file
# under a config that runs nothing), never `git archive` / `git show`, which run
# the smudge filter a committed .gitattributes names from the agent's
# .git/config. The files are the regular files `git archive` held.
def ref_tree(workspace: Path, ref: str, scratch: Path) -> Path:
    """HEAD_PATHS of `ref`'s tree, as committed, extracted under `scratch`."""
    scratch = Path(scratch)
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    try:
        present = [p for p in HEAD_PATHS if safegit.exists(workspace, ref, p)]
        if "bi" not in present:
            raise BuildUnreadable(f"{ref} has no bi/ directory")
        files = safegit.read_tree(workspace, ref, present)
    except safegit.GitUnreadable as exc:
        raise BuildUnreadable(f"the repository has no readable {ref} with bi/: "
                              f"{exc}") from None
    for name, data in files.items():
        target = scratch / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return scratch


def head_tree(workspace: Path, scratch: Path) -> Path:
    return ref_tree(workspace, "HEAD", scratch)


def ref_file(workspace: Path, ref: str, rel: str) -> bytes | None:
    """`<ref>:<rel>` as committed - what `git show <ref>:<rel>` printed for a
    file - or None when there is nothing there."""
    try:
        found = safegit.read_object(Path(workspace), ref, rel)
    except safegit.GitUnreadable:
        return None
    return None if found is None else found[1]


def other_branches(workspace: Path) -> list[str]:
    """Local branches other than the one HEAD is on, sorted."""
    try:
        current = safegit.symbolic_ref(workspace) or ""
        head = safegit.rev_parse(workspace, "HEAD") or ""
        refs = safegit.for_each_ref(workspace, "%(refname:short) %(objectname)",
                                    "refs/heads")
    except safegit.GitUnreadable:
        return []
    names = []
    for line in refs:
        name, _, sha = line.partition(" ")
        if name and name != current and (current or sha != head):
            names.append(name)
    return sorted(names)


def tree_bytes(folder: Path) -> dict[str, bytes]:
    folder = Path(folder)
    return {p.relative_to(folder).as_posix(): p.read_bytes()
            for p in sorted(folder.rglob("*")) if p.is_file()} if folder.is_dir() else {}


def model_files(project: Path) -> dict[str, bytes]:
    """The semantic model's definition files as a deployment sends them:
    relative to the .SemanticModel folder, Desktop's local `.pbi/` left out."""
    folder = Path(project) / f"{K.DATASET}.SemanticModel"
    return {rel: data for rel, data in tree_bytes(folder).items()
            if not rel.startswith(".pbi/")}


# ------------------------------------------------------------- published
def published_state(bi_state: Path | None) -> dict | None:
    """The Power BI service's staged state.json, or None when it never wrote one."""
    if not bi_state:
        return None
    path = Path(bi_state) / "powerbi" / "state.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BuildUnreadable(f"the staged Power BI state does not parse: {exc}") from None


def published_dataset(state: dict | None) -> dict | None:
    if not state:
        return None
    for dataset in state.get("datasets", {}).values():
        if dataset.get("name") == K.DATASET:
            return dataset
    return None


def published_parts(state: dict | None) -> dict[str, bytes] | None:
    dataset = published_dataset(state)
    if dataset is None:
        return None
    return {p: base64.b64decode(d) for p, d in dataset["parts"].items()}


def read_published(bi_state: Path | None, pristine: Path, warehouse: Path):
    """(model, report, source): what the service serves, or the pristine
    project when it never wrote a state (it was never asked)."""
    from world.semantic.formats.powerbi import pbir, tmdl

    state = published_state(bi_state)
    if state is None:
        model, report = read_tree(Path(pristine) / K.PROJECT_DIR, warehouse)
        return model, report, "pristine (no service state)"
    try:
        for dataset in sorted(state.get("datasets", {}).values(), key=lambda d: d["name"]):
            files = {p: base64.b64decode(d) for p, d in dataset["parts"].items()}
            model, info = tmdl.read_semantic_model(files, dataset["name"])
            for report in sorted(state.get("reports", {}).values(), key=lambda r: r["name"]):
                if report.get("datasetId") != dataset["id"] or report["name"] != K.REPORT:
                    continue
                parts = {p: base64.b64decode(d) for p, d in report["parts"].items()}
                return model, pbir.read_report(parts, model, info, report["name"]), "published"
    except (SemanticError, OSError, ValueError, KeyError, TypeError) as exc:
        raise BuildUnreadable(f"the published Power BI model does not read: "
                              f"{type(exc).__name__}: {exc}") from None
    raise BuildUnreadable(f"no published Power BI report {K.REPORT!r}")


def refreshes(state: dict | None) -> list[dict]:
    dataset = published_dataset(state)
    return list(dataset.get("refreshes", [])) if dataset else []


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
    columns = rendered["columns"]
    keep = list(range(groups)) + [columns.index(lab) for lab in labels]
    return [[row[i] for i in keep] for row in rendered["rows"]]


def rows_match(got: list[list], want: list[list]) -> bool:
    return len(got) == len(want) and all(
        len(g) == len(w) and all(same(x, y) for x, y in zip(g, w)) for g, w in zip(got, want))


def _shape(visual) -> dict:
    """What a visual IS, independent of how its values are written."""
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


def scoped_visual(base: Visual, region: str) -> Visual:
    """The tile as a viewer sees it filtered to one region in the filter pane:
    the same visual, plus Customer[region] = region."""
    page, name = K.scoped_key(region)
    return replace(base, name=name,
                   filters=base.filters + (FilterValues(Col("Customer", "region"), (region,)),))


def grid_visual() -> Visual:
    return Visual(K.GRID, group_by=(Col("Customer", "region"), Col("Customer", "segment")),
                  values=(("Net Revenue", MeasureRef("Net Revenue")),
                          (K.SHARE, MeasureRef(K.SHARE))), kind="table")


def _render_against(sessions: dict, model, visual, spec_key, labels, oracles, entry):
    spec = K.ORACLE_SQL[spec_key]
    for label, session in sessions.items():
        try:
            rendered = session.visual(model, visual)
        except SemanticError as exc:
            entry[label] = False
            entry[f"{label}_error"] = f"{type(exc).__name__}: {exc}"[:300]
            continue
        rows = project_rows(rendered, len(spec["groups"]), labels)
        values = list(spec["values"])
        want = [r[:len(spec["groups"])] + [r[len(spec["groups"]) + values.index(lab)]
                                          for lab in labels]
                for r in oracles[label][key(*spec_key)]["rows"]]
        entry[label] = rows_match(rows, want)
        if not entry[label]:
            entry[f"{label}_first_rows"] = {"got": rows[:3], "want": want[:3]}
        if label == "real":
            entry["rendered"] = rows
    return entry


def region_sums(grid_rows: list[list]) -> dict:
    """{region: sum of its segments' Segment Share} from a rendered grid."""
    out: dict[str, float] = {}
    for region, _segment, _net, share in grid_rows:
        if share is not None:
            out[str(region)] = round(out.get(str(region), 0.0) + float(share), 9)
    return out


def grade_build(model, report, reference, oracles: dict, warehouses: dict) -> dict:
    """Is this Power BI build of the Executive Overview right?

    The graded tiles (contract.GRADED) keep their provisioned shape and render
    the oracle on both warehouses; the Revenue by Segment tile filtered to each
    region - what a viewer does in the filter pane - renders each segment's
    share OF THAT REGION; so does a region x segment table.

    Returns {"correct", "visuals_correct" (0..5, real warehouse, shape intact),
    "scopes_correct" (0..4: three regions and the grid, real warehouse),
    "region_sums", "visuals", "problems"}."""
    problems: list[str] = []
    visuals: dict[str, dict] = {}
    correct_real = scopes_real = 0
    sessions = {}
    try:
        for label, wh in warehouses.items():
            sessions[label] = render.Session(Path(wh))
        for (page, name), labels in K.GRADED.items():
            k = key(page, name)
            entry = {"shape_ok": False, "real": False, "perturbed": False}
            visuals[k] = entry
            want = find_visual(reference, page, name)
            got = find_visual(report, page, name)
            if got is None:
                problems.append(f"{k}: the visual is gone")
                continue
            missing = [lab for lab in labels if lab not in [v for v, _ in got.values]]
            if missing:
                problems.append(f"{k}: value(s) {missing} are gone")
                continue
            if want is not None and _shape(got) != _shape(want):
                entry["shape"] = {"want": _shape(want), "got": _shape(got)}
                problems.append(f"{k}: grouping, filters, sort or cut changed (the tile no "
                                f"longer asks the same question)")
                continue
            entry["shape_ok"] = True
            _render_against(sessions, model, got, (page, name), labels, oracles, entry)
            entry.pop("rendered", None)
            correct_real += bool(entry["real"])
            if not (entry["real"] and entry["perturbed"]):
                which = [w for w in ("real", "perturbed") if not entry[w]]
                problems.append(f"{k}: values differ from the definition on the "
                                f"{' and '.join(which)} warehouse")
        base = find_visual(report, *K.SCOPED_VISUAL)
        for region in K.REGIONS:
            spec_key = K.scoped_key(region)
            entry = {"real": False, "perturbed": False}
            visuals[key(*spec_key)] = entry
            if base is None or not visuals[key(*K.SCOPED_VISUAL)]["shape_ok"]:
                problems.append(f"{key(*spec_key)}: no Revenue by Segment tile to filter")
                continue
            _render_against(sessions, model, scoped_visual(base, region), spec_key,
                            K.SCOPED_LABELS, oracles, entry)
            if entry.get("rendered") is not None:
                entry["share_sum"] = round(sum(float(r[-1]) for r in entry["rendered"]
                                               if r[-1] is not None), 9)
            entry.pop("rendered", None)
            scopes_real += bool(entry["real"])
            if not (entry["real"] and entry["perturbed"]):
                problems.append(f"{key(*spec_key)}: Segment Share filtered to {region} is not "
                                f"each segment's share of {region}")
        spec_key = ("Overview", K.GRID)
        entry = {"real": False, "perturbed": False}
        visuals[key(*spec_key)] = entry
        _render_against(sessions, model, grid_visual(), spec_key, K.SCOPED_LABELS, oracles,
                        entry)
        sums = region_sums(entry.pop("rendered", None) or [])
        scopes_real += bool(entry["real"])
        if not (entry["real"] and entry["perturbed"]):
            problems.append(f"{key(*spec_key)}: a region's segment shares do not add up to "
                            f"its own revenue ({sums})")
    finally:
        for session in sessions.values():
            session.close()
    return {"correct": not problems, "visuals_correct": correct_real,
            "visuals_graded": len(K.GRADED), "scopes_correct": scopes_real,
            "scopes_graded": len(K.REGIONS) + 1, "region_sums": sums,
            "visuals": visuals, "problems": problems}


def render_all(model, report, warehouse: Path) -> dict:
    out = {}
    with render.Session(Path(warehouse)) as session:
        for page in report.pages:
            for visual in page.visuals:
                try:
                    out[key(page.name, visual.name)] = session.visual(model, visual)
                except SemanticError as exc:
                    out[key(page.name, visual.name)] = {"error": f"{type(exc).__name__}: {exc}"}
    return out
