"""The BI projects build step (world/world/bi_projects.py): vendor files in the repo.

A BI-heavy analytics engineer's repository carries the dashboards as code next to
the dbt project - a PBIP project, Tableau .tds/.twb, Sigma's API spec JSON - and
the vendor services publish what was first committed. This is where the world
writes them. What is proved here, and why each matters:

- NOTHING CHANGES UNLESS ASKED. No profile, a profile without `plugs.bi`, and a
  profile whose BI is Metabase write no vendor directory, and the recorded
  revenue_daily hash is the one acceptance.json holds - the explicit rule that an
  unflagged seed builds byte-identically (world/README.md).
- REFUSED BEFORE ANYTHING IS BUILT. A seed plugging a vendor into a theme with no
  `bi_projects` hook is a loud SystemExit naming the theme, with no half-built
  tree left behind.
- THE FILES ARE CODE IN THE REPO. Each plugged vendor's project lands in the
  layout its fake service reads (bi/powerbi, bi/tableau, bi/sigma), inside the
  first git commit (the services' "published = first commit" rule), byte-identical
  across two builds, in the `workspace` fingerprint section only.
- THEY READ BACK. `read_project(<dir>, warehouse=<built warehouse>)` gives back
  exactly the model and reports the theme hook returned.
- THREE VENDORS, ONE NUMBER. On the full-size commerce_analytics warehouse, every
  visual the three editions share renders IDENTICAL rows in Power BI, Tableau and
  Sigma, equal to hand-written SQL - the first end-to-end calibration of the BI
  stack on a world rather than a fixture.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import duckdb
import pytest
import yaml

WORLD_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORLD_DIR))

from world import bi_projects, build, fingerprint  # noqa: E402
from world.hashing import canon_value  # noqa: E402
from world.semantic import examples, render  # noqa: E402
from world.semantic.formats import powerbi, sigma, tableau  # noqa: E402
from world.semantic.formats.sigma import examples as sigma_examples  # noqa: E402
from world.semantic.formats.tableau import examples as tableau_examples  # noqa: E402
from world.themes import load_theme  # noqa: E402

ACCEPTANCE = WORLD_DIR / "evidence" / "acceptance.json"
REVENUE_SEED = WORLD_DIR / "seed.example.yaml"
COMMERCE_SEED = WORLD_DIR / "world" / "themes" / "commerce_analytics" / "seed.example.yaml"
DB = Path("var") / "warehouse" / "warehouse.duckdb"
VENDOR_DIRS = ("bi/powerbi", "bi/tableau", "bi/sigma")

#: The real `da` role on the laptop stack (alkera_gateway.plugs.ROLES["da"]).
DA = {"name": "da/laptop",
      "slots": ["files", "notebook", "warehouse", "transform", "bi", "human"]}
ALL_VENDORS = {**DA, "plugs": {"bi": ["sigma", "tableau", "powerbi"]}}   # unsorted on purpose


def _seed(tmp: Path, name: str, base: Path, **extra) -> Path:
    seed = yaml.safe_load(base.read_text(encoding="utf-8"))
    path = tmp / f"{name}.yaml"
    path.write_text(yaml.safe_dump({**seed, **extra}, sort_keys=True), encoding="utf-8")
    return path


def _tree(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def _committed(workspace: Path) -> set[str]:
    out = subprocess.run(["git", "-C", str(workspace), "ls-tree", "-r", "--name-only",
                          "HEAD"], capture_output=True, text=True, check=True).stdout
    return set(out.splitlines())


# ======================================================================
# builds, shared by the whole module (a commerce_analytics build is ~8 s)
# ======================================================================
@pytest.fixture(scope="module")
def tmp(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("bi_projects")


@pytest.fixture(scope="module")
def commerce_unplugged(tmp) -> dict[str, Path]:
    """commerce_analytics three ways that must all leave bi/ as the theme ships it."""
    out = {"plain": build.build(COMMERCE_SEED, tmp / "c_plain")}
    out["da"] = build.build(_seed(tmp, "c_da", COMMERCE_SEED, profile=DA), tmp / "c_da")
    out["metabase"] = build.build(
        _seed(tmp, "c_mb", COMMERCE_SEED,
              profile={**DA, "plugs": {"bi": ["metabase_fake"]}}), tmp / "c_mb")
    return out


@pytest.fixture(scope="module")
def commerce_vendors(tmp) -> tuple[Path, Path]:
    """commerce_analytics plugging all three vendors, built twice."""
    seed = _seed(tmp, "c_all", COMMERCE_SEED, profile=ALL_VENDORS, native=True)
    return build.build(seed, tmp / "c_all_1"), build.build(seed, tmp / "c_all_2")


@pytest.fixture(scope="module")
def projects() -> dict:
    """What the theme hook says each vendor's project is."""
    return load_theme("commerce_analytics").gen.bi_projects(
        yaml.safe_load(COMMERCE_SEED.read_text(encoding="utf-8")))


# ======================================================================
# 1. the trigger
# ======================================================================
@pytest.mark.parametrize("profile, expected", [
    (None, []),
    ({"name": "x", "slots": ["bi"]}, []),
    ({"name": "x", "slots": ["bi"], "plugs": {"bi": ["metabase_fake"]}}, []),
    ({"name": "x", "slots": ["bi"], "plugs": {"bi": ["tableau"]}}, ["tableau"]),
    ({"name": "x", "slots": ["bi"], "plugs": {"bi": ["sigma", "metabase_fake", "powerbi"]}},
     ["powerbi", "sigma"]),
])
def test_only_a_profile_plugging_a_bi_vendor_asks_for_projects(profile, expected):
    assert bi_projects.vendors(profile) == expected


def test_unflagged_revenue_daily_keeps_its_recorded_hash(tmp):
    root = build.build(REVENUE_SEED, tmp / "r_plain")
    recorded = json.loads(ACCEPTANCE.read_text(encoding="utf-8"))
    assert fingerprint.semantic_hash(root) == recorded["gate1_host_semantic_hash"]
    for rel in VENDOR_DIRS:
        assert not (root / "workspace" / rel).exists(), rel


def test_revenue_daily_profile_without_bi_plugs_writes_nothing(tmp):
    root = build.build(_seed(tmp, "r_da", REVENUE_SEED, profile=DA), tmp / "r_da")
    for rel in VENDOR_DIRS:
        assert not (root / "workspace" / rel).exists(), rel


def test_a_theme_without_the_hook_is_refused_before_anything_is_built(tmp):
    seed = _seed(tmp, "r_pbi", REVENUE_SEED, profile={**DA, "plugs": {"bi": ["powerbi"]}})
    with pytest.raises(SystemExit) as info:
        build.build(seed, tmp / "r_pbi")
    message = str(info.value)
    assert "revenue_daily" in message and "bi_projects" in message and "powerbi" in message
    assert not (tmp / "r_pbi").exists(), "a refused seed left a half-built tree"


def test_commerce_unplugged_builds_carry_no_vendor_project(commerce_unplugged):
    sections = {k: fingerprint.semantic(root) for k, root in commerce_unplugged.items()}
    # A profile selects planes; it never changes the workspace. So the three
    # builds' repositories are one repository - and none has a vendor project.
    assert sections["plain"]["workspace"] == sections["da"]["workspace"] \
        == sections["metabase"]["workspace"]
    for label, root in commerce_unplugged.items():
        committed = _committed(root / "workspace")
        for rel in VENDOR_DIRS:
            assert not (root / "workspace" / rel).exists(), (label, rel)
            assert not any(p.startswith(rel + "/") for p in committed), (label, rel)


# ======================================================================
# 2. the files: layout, git, determinism, fingerprint
# ======================================================================
def test_each_vendor_lands_in_the_layout_its_service_reads(commerce_vendors):
    workspace = commerce_vendors[0] / "workspace"
    assert set(_tree(workspace / "bi" / "powerbi")) >= {
        ".gitignore", "Executive Overview.pbip",
        "Commerce.SemanticModel/definition.pbism",
        "Commerce.SemanticModel/definition/tables/Orders.tmdl",
        "Executive Overview.Report/definition.pbir"}
    assert set(_tree(workspace / "bi" / "tableau")) == {
        "datasources/Commerce.tds", "workbooks/Executive Overview.twb"}
    assert set(_tree(workspace / "bi" / "sigma")) == {
        "data-models/commerce.json", "workbooks/executive-overview.json",
        "connections/snowflake.json"}


def test_the_projects_are_in_the_first_commit(commerce_vendors):
    workspace = commerce_vendors[0] / "workspace"
    count = subprocess.run(["git", "-C", str(workspace), "rev-list", "--count", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert count == "1"
    committed = _committed(workspace)
    for rel in VENDOR_DIRS:
        files = {f"{rel}/{p}" for p in _tree(workspace / rel)}
        assert files and files <= committed, rel
    status = subprocess.run(["git", "-C", str(workspace), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status == "", status


def test_two_builds_are_byte_identical(commerce_vendors):
    a, b = commerce_vendors
    for rel in VENDOR_DIRS:
        assert _tree(a / "workspace" / rel) == _tree(b / "workspace" / rel), rel
    assert fingerprint.semantic_hash(a) == fingerprint.semantic_hash(b)
    assert fingerprint.git_head(a) == fingerprint.git_head(b)


def test_the_projects_move_only_the_workspace_section(commerce_vendors, commerce_unplugged):
    plugged = fingerprint.semantic(commerce_vendors[0])
    unplugged = fingerprint.semantic(commerce_unplugged["da"])
    moved = {k for k in set(plugged) | set(unplugged) if plugged.get(k) != unplugged.get(k)}
    # `profile` moves because the record names the plugs; `home` exists because
    # the plugged seed is native (its onboarding page names the projects).
    assert moved == {"workspace", "profile", "home"}


def test_connections_name_the_worlds_warehouse(commerce_vendors):
    workspace = commerce_vendors[0] / "workspace"
    model = workspace / "bi" / "powerbi" / "Commerce.SemanticModel" / "definition"
    orders = (model / "tables" / "Orders.tmdl").read_text(encoding="utf-8")
    expressions = (model / "expressions.tmdl").read_text(encoding="utf-8")
    # Power BI has no DuckDB connector; a workstation reaches DuckDB through
    # the DuckDB ODBC driver, with hierarchical navigation (catalog/schema/table).
    assert "Odbc.DataSource(DuckDBConnectionString, [HierarchicalNavigation=true])" in orders
    assert '{[Name="fct_orders",Kind="Table"]}' in orders
    assert "DuckDB Driver" in expressions and "/world/var/warehouse/warehouse.duckdb" \
        in expressions
    tds = (workspace / "bi" / "tableau" / "datasources" / "Commerce.tds").read_text("utf-8")
    assert "class='genericodbc'" in tds and "odbc-driver='DuckDB Driver'" in tds
    assert "database=/world/var/warehouse/warehouse.duckdb" in tds


def test_onboarding_points_at_each_vendor_project(commerce_vendors):
    root = commerce_vendors[0]
    page = (root / "workspace" / "docs" / "onboarding.md").read_text(encoding="utf-8")
    assert "## The BI projects" in page
    for vendor, port in (("powerbi", 8095), ("tableau", 8096), ("sigma", 8097)):
        assert f"| {vendor} | `bi/{vendor}/` | `{vendor}.internal:{port}` |" in page
    assert "`bi/README.md` describes how they are laid out." in page
    envrc = (root / "workspace" / ".envrc").read_text(encoding="utf-8")
    for var in ("POWERBI_API_URL", "TABLEAU_SERVER_URL", "SIGMA_BASE_URL"):
        assert f"export {var}=" in envrc, var


def test_onboarding_names_no_project_without_vendor_plugs(tmp):
    from world import native_workspace as nw

    root = tmp / "onboarding_only"
    for vendor in ("powerbi", "tableau", "sigma"):
        (root / "workspace" / "bi" / vendor).mkdir(parents=True)
    # directories alone are not a plug: unprofiled, or `bi` without a vendor
    for slots, plugs in ((None, None), (DA["slots"], None),
                         (DA["slots"], {"bi": ["metabase_fake"]})):
        assert "BI projects" not in nw.onboarding_markdown(root, {}, slots, plugs)
    # and a plug alone is not a directory: a vendor box with no project names none
    assert "BI projects" not in nw.onboarding_markdown(tmp / "empty", {}, DA["slots"],
                                                       {"bi": ["powerbi"]})


# ======================================================================
# 3. read back
# ======================================================================
@pytest.mark.parametrize("vendor, front_end", [
    ("powerbi", powerbi), ("tableau", tableau), ("sigma", sigma)])
def test_each_project_reads_back_as_written(commerce_vendors, projects, vendor, front_end):
    root = commerce_vendors[0]
    got = front_end.read_project(root / "workspace" / "bi" / vendor, warehouse=root / DB)
    assert got == projects[vendor]


def test_the_hook_is_the_editions_with_theme_names(projects):
    assert set(projects) == {"powerbi", "tableau", "sigma"}
    assert projects["powerbi"] == (examples.commerce_model("dax"),
                                   (examples.executive_report(),))
    assert projects["tableau"] == (tableau_examples.tableau_commerce_model(),
                                   (tableau_examples.tableau_executive_report(),))
    assert projects["sigma"] == (sigma_examples.commerce_model(),
                                 (sigma_examples.executive_report(),))
    for model, reports in projects.values():
        assert model.name == "Commerce"
        assert [r.name for r in reports] == ["Executive Overview"]
        assert all(t.source.startswith("warehouse.") for t in model.tables)


def test_a_source_the_warehouse_lacks_is_refused_at_build(commerce_vendors, projects, tmp):
    model, reports = projects["tableau"]
    broken = replace(model, tables=tuple(
        replace(t, source="warehouse.fct_orderz") if t.name == "Orders" else t
        for t in model.tables))
    with pytest.raises(SystemExit) as info:
        bi_projects.check_sources(broken, commerce_vendors[0] / DB)
    assert "warehouse.fct_orderz" in str(info.value)


# ======================================================================
# 4. three vendors, one number
# ======================================================================
#: Each visual the three editions share, and the value labels all three compute.
#: Segment Share is Power BI and Tableau only (Sigma has no per-column filter
#: removal); Revenue LY / YoY Growth are Power BI only (time intelligence).
SHARED = {
    ("Overview", "KPIs"): ("Net Revenue", "Completed Orders", "AOV"),
    ("Overview", "Revenue by Month"): ("Net Revenue",),
    ("Overview", "Revenue by Segment"): ("Net Revenue",),
    ("Overview", "Top Products"): ("Units",),
    ("Regions", "Revenue by Region 2026"): ("Net Revenue",),
}

HAND_SQL = {
    ("Overview", "KPIs"):
        "SELECT sum(order_amount) FILTER (WHERE order_status = 'completed'), "
        "count(*) FILTER (WHERE order_status = 'completed'), "
        "sum(order_amount) FILTER (WHERE order_status = 'completed') "
        "/ count(*) FILTER (WHERE order_status = 'completed') "
        "FROM warehouse.fct_orders",
    ("Overview", "Revenue by Month"):
        "SELECT d.year, d.month, sum(o.order_amount) FILTER (WHERE o.order_status = "
        "'completed') FROM warehouse.fct_orders o JOIN warehouse.dim_date d "
        "ON d.date_day = o.order_date GROUP BY 1, 2 ORDER BY 1, 2",
    ("Overview", "Revenue by Segment"):
        "SELECT c.segment, sum(o.order_amount) FROM warehouse.fct_orders o "
        "JOIN warehouse.dim_customer c USING (customer_id) "
        "WHERE o.order_status = 'completed' GROUP BY 1 ORDER BY 2 DESC, 1",
    ("Overview", "Top Products"):
        "SELECT p.product_name, sum(l.quantity) FROM warehouse.fct_order_lines l "
        "JOIN warehouse.dim_product p USING (product_id) GROUP BY 1 ORDER BY 2 DESC, 1 "
        "LIMIT 3",
    ("Regions", "Revenue by Region 2026"):
        "SELECT c.region, sum(o.order_amount) FROM warehouse.fct_orders o "
        "JOIN warehouse.dim_customer c USING (customer_id) "
        "JOIN warehouse.dim_date d ON d.date_day = o.order_date "
        "WHERE o.order_status = 'completed' AND d.year = 2026 GROUP BY 1 ORDER BY 1",
}


def _visual(report, page: str, name: str):
    return next(v for p in report.pages if p.name == page for v in p.visuals
                if v.name == name)


def project_rows(rendered: dict, visual, labels: tuple[str, ...]) -> list[list]:
    """A rendered visual narrowed to its group columns and `labels`.

    Under Power BI's auto-exist (groups="measures") a group shows only when one
    of the SHOWN values is not blank, so narrowing a measures-mode visual drops
    the rows whose remaining values are all blank: a month the date table holds
    with last year's revenue and none this year is a Revenue by Month row only
    because of Revenue LY, and a visual showing Net Revenue alone would not
    have it. (On the shipped seed there is no such month; the rule is here so
    a seed with a longer date table compares like for like.)"""
    k = len(visual.group_by)
    keep = list(range(k)) + [rendered["columns"].index(lab) for lab in labels]
    rows = [[row[i] for i in keep] for row in rendered["rows"]]
    if k and visual.groups == "measures":
        rows = [r for r in rows if any(v is not None for v in r[k:])]
    return rows


@pytest.fixture(scope="module")
def rendered(commerce_vendors) -> dict:
    """Every vendor's report, read from the BUILT files, rendered on the BUILT
    warehouse: {vendor: (report, render_report(...))}."""
    root = commerce_vendors[0]
    out = {}
    for vendor, front_end in (("powerbi", powerbi), ("tableau", tableau), ("sigma", sigma)):
        model, (report,) = front_end.read_project(root / "workspace" / "bi" / vendor,
                                                  warehouse=root / DB)
        out[vendor] = (report, render.render_report(model, report, root / DB))
    return out


def hand(root: Path, sql: str) -> list[list]:
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH '{root / DB}' AS wh (READ_ONLY)")
        con.execute("USE wh")
        return [[canon_value(v) for v in row] for row in con.execute(sql).fetchall()]
    finally:
        con.close()


@pytest.mark.parametrize("key", sorted(SHARED), ids=lambda k: k[1])
def test_every_shared_visual_agrees_across_vendors_and_with_sql(rendered, commerce_vendors,
                                                                 key):
    page, name = key
    by_vendor = {}
    for vendor, (report, result) in rendered.items():
        visual = _visual(report, page, name)
        by_vendor[vendor] = project_rows(result[page][name], visual, SHARED[key])
    assert by_vendor["powerbi"] == by_vendor["tableau"] == by_vendor["sigma"], by_vendor
    assert by_vendor["powerbi"] == hand(commerce_vendors[0], HAND_SQL[key])
    assert by_vendor["powerbi"], "an empty visual agrees with anything"


def test_segment_share_agrees_where_both_vendors_have_it(rendered, commerce_vendors):
    shares = {}
    for vendor in ("powerbi", "tableau"):
        report, result = rendered[vendor]
        visual = _visual(report, "Overview", "Revenue by Segment")
        shares[vendor] = project_rows(result["Overview"]["Revenue by Segment"], visual,
                                      ("Segment Share",))
    assert shares["powerbi"] == shares["tableau"]
    total = sum(float(v) for _, v in shares["powerbi"])
    assert abs(total - 1.0) < 1e-12


# ======================================================================
# 5. BI faults (`bi_faults:`): a team's mistake in its own committed project
# ======================================================================
FAULTS = ["tableau_orders_line_join", "sigma_net_revenue_refunds"]


@pytest.fixture(scope="module")
def commerce_faulted(tmp) -> Path:
    """commerce_analytics plugging all three vendors with both BI faults on."""
    seed = _seed(tmp, "c_faults", COMMERCE_SEED, profile=ALL_VENDORS, native=True,
                 bi_faults=FAULTS)
    return build.build(seed, tmp / "c_faults")


@pytest.mark.parametrize("bad, words", [
    (["tableau_orders_line_joins"], ("unknown fault", "tableau_orders_line_join")),
    ("nope", ("unknown fault",)),
    ({"tableau_orders_line_join": True}, ("must be a list",)),
])
def test_an_unknown_or_malformed_bi_fault_is_refused(bad, words):
    gen = load_theme("commerce_analytics").gen
    with pytest.raises(SystemExit) as info:
        gen.bi_faults({"bi_faults": bad, "profile": ALL_VENDORS})
    for word in words:
        assert word in str(info.value)


def test_a_bi_fault_in_an_unplugged_vendor_is_refused_before_anything_is_built(tmp):
    profile = {**DA, "plugs": {"bi": ["powerbi", "sigma"]}}
    seed = _seed(tmp, "c_unplugged_fault", COMMERCE_SEED, profile=profile,
                 bi_faults=["tableau_orders_line_join"])
    with pytest.raises(SystemExit) as info:
        build.build(seed, tmp / "c_unplugged_fault")
    assert "tableau" in str(info.value) and "does not plug" in str(info.value)


def test_no_bi_faults_is_the_unfaulted_hook(projects):
    gen = load_theme("commerce_analytics").gen
    seed = yaml.safe_load(COMMERCE_SEED.read_text(encoding="utf-8"))
    assert gen.bi_faults(seed) == ()
    for empty in (None, []):
        assert gen.bi_projects({**seed, "bi_faults": empty}) == projects


def test_faults_move_only_the_planted_files_and_are_in_the_first_commit(commerce_faulted,
                                                                         commerce_vendors):
    clean, faulted = commerce_vendors[0] / "workspace", commerce_faulted / "workspace"
    moved = set()
    for rel in VENDOR_DIRS:
        a, b = _tree(clean / rel), _tree(faulted / rel)
        assert set(a) == set(b), rel
        moved |= {f"{rel}/{p}" for p in a if a[p] != b[p]}
    # Sales' data source (and the workbook's cached copy of its fields) and
    # Finance's workbook. Power BI, the Sigma data model and everything outside
    # bi/ are byte for byte the clean build's.
    assert moved == {"bi/tableau/datasources/Commerce.tds",
                     "bi/tableau/workbooks/Executive Overview.twb",
                     "bi/sigma/workbooks/executive-overview.json"}
    a, b = fingerprint.semantic(commerce_vendors[0]), fingerprint.semantic(commerce_faulted)
    assert {k for k in set(a) | set(b) if a.get(k) != b.get(k)} == {"workspace"}
    committed = _committed(faulted)
    assert {"bi/tableau/datasources/Commerce.tds",
            "bi/sigma/workbooks/executive-overview.json"} <= committed
    status = subprocess.run(["git", "-C", str(faulted), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status == ""


def test_faulted_projects_read_back_as_the_hook_planted_them(commerce_faulted):
    gen = load_theme("commerce_analytics").gen
    seed = {**yaml.safe_load(COMMERCE_SEED.read_text(encoding="utf-8")),
            "profile": ALL_VENDORS, "bi_faults": FAULTS}
    planted = gen.bi_projects(seed)
    for vendor, front_end in (("powerbi", powerbi), ("tableau", tableau), ("sigma", sigma)):
        got = front_end.read_project(commerce_faulted / "workspace" / "bi" / vendor,
                                     warehouse=commerce_faulted / DB)
        assert got == planted[vendor], vendor


def test_each_fault_computes_the_wrong_number_it_is_declared_for(commerce_faulted):
    """The three KPI cards disagree, each for its declared reason, measured
    against hand-written SQL on the built warehouse. Power BI stays right."""
    root = commerce_faulted
    kpis = {}
    for vendor, front_end in (("powerbi", powerbi), ("tableau", tableau), ("sigma", sigma)):
        model, (report,) = front_end.read_project(root / "workspace" / "bi" / vendor,
                                                  warehouse=root / DB)
        kpis[vendor] = render.render_report(model, report, root / DB)["Overview"]["KPIs"]
    right = hand(root, HAND_SQL[("Overview", "KPIs")])[0]
    assert kpis["powerbi"]["rows"][0] == right
    fanned = hand(root, "SELECT sum(o.order_amount) FILTER (WHERE o.order_status = "
                        "'completed'), count(*) FILTER (WHERE o.order_status = 'completed') "
                        "FROM warehouse.fct_orders o JOIN warehouse.fct_order_lines l "
                        "USING (order_id)")[0]
    assert kpis["tableau"]["rows"][0][:2] == fanned
    with_refunds = hand(root, "SELECT sum(order_amount) FROM warehouse.fct_orders "
                              "WHERE order_status <> 'cancelled'")[0][0]
    sigma_net, sigma_completed, _aov = kpis["sigma"]["rows"][0]
    assert (sigma_net, sigma_completed) == (with_refunds, right[1])
    assert len({kpis[v]["rows"][0][0] for v in kpis}) == 3, kpis
