"""commerce_analytics theme acceptance tests (stdlib unittest, run under pytest).

The theme is the BI layer's world: a shop's nightly CDC extracts -> raw -> a
dbt star schema (exactly world/world/semantic/examples.py's DDL) + MetricFlow
semantic layer + exposures -> legacy SQL-panel dashboards and a 06:00 refresh
job. What is proved here, and why each matters:

- the double-build gate (identical semantic_hash), with build wall time
  printed, because the default build has to stay fast enough to be a fixture;
- dbt builds and every data test passes, and the marts' columns and DuckDB
  types are EXACTLY examples.DDL - every BI front-end binds to those names;
- the data's own invariants: order_amount == sum(line_amount) per order, the
  declared value sets, the 2025-01-01..2026-02-28 span with dim_date covering
  it contiguously;
- the semantic layer and exposures are what dbt parsed (manifest.json), not
  just YAML that happens to load;
- the workflow corpus loads through world.workflow AND the real gateway, and
  the numbers its tickets quote are still true of the built world;
- `advance(24)` from a fresh build executes one whole nightly cycle, path-
  independently, and the stale-publish fault (`extract_late`) stages exactly
  the incident it is declared for.
"""
import json
import shutil
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import bi, executor, fingerprint, observe, scheduler, workflow  # noqa: E402
from world import build as wbuild
from world.semantic import examples  # noqa: E402
from world.themes import load_theme  # noqa: E402

TRACK_A = Path(__file__).resolve().parents[1]
THEME_DIR = TRACK_A / "world" / "themes" / "commerce_analytics"
SEED = THEME_DIR / "seed.example.yaml"
DB = Path("var/warehouse/warehouse.duckdb")
VNOW = "2026-03-02T00:00:00Z"
FIRST_DAY, LAST_DAY = date(2025, 1, 1), date(2026, 2, 28)

try:
    from alkera_gateway import Gateway, GatewayConfig
    from alkera_gateway.surfaces import Dashboard
    GATEWAY = None
except ImportError as exc:                                   # pragma: no cover
    GATEWAY = f"alkera_gateway is not importable from this interpreter: {exc}"


def connect(root: Path):
    """ATTACH under an alias: the file stem collides with the `warehouse`
    schema otherwise (world/README.md)."""
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute("ATTACH '" + str(root / DB).replace("'", "''") + "' AS wh (READ_ONLY)")
    con.execute("USE wh")
    return con


def scalar(root: Path, sql: str):
    con = connect(root)
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


class CommerceAnalyticsThemeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="world_commerce_"))
        cls.times = []
        for name in ("w1", "w2"):
            started = time.perf_counter()
            setattr(cls, name, wbuild.build(SEED, cls.tmp / name))
            cls.times.append(time.perf_counter() - started)
        cls.hash = fingerprint.semantic_hash(cls.w1)
        print(f"\nbuild wall time: {cls.times[0]:.1f}s, {cls.times[1]:.1f}s")
        print("semantic_hash:", cls.hash)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def copy(self, label: str) -> Path:
        return shutil.copytree(self.w1, self.tmp / f"{label}_{self.id().rsplit('.', 1)[-1]}")

    # ------------------------------------------------------------------
    # determinism and cost
    # ------------------------------------------------------------------

    def test_semantic_fingerprint_stable(self):
        """Build-from-scratch-twice gate (same host here; CI runs cross-host)."""
        self.assertEqual(fingerprint.semantic(self.w1), fingerprint.semantic(self.w2))
        self.assertEqual(self.hash, fingerprint.semantic_hash(self.w2))
        self.assertEqual(fingerprint.git_head(self.w1), fingerprint.git_head(self.w2))

    def test_conditional_sections_are_the_ones_this_theme_seeds(self):
        self.assertEqual(sorted(fingerprint.semantic(self.w1)),
                         ["bi", "live", "obs", "scheduler", "warehouse",
                          "workflow", "workspace"])

    def test_snapshot_restore_is_byte_identical(self):
        """The raw-byte gate that applies: a snapshot restores this world's
        warehouse file byte for byte.

        Two independent BUILDS are not asserted byte-identical here, unlike
        revenue_daily in test_world: measured on DuckDB 1.5.5, once a string
        column spans more than a vector DuckDB writes unzeroed padding inside
        compressed segments (and FSST samples vectors at random), so two
        builds of this volume differ in a few dozen padding bytes with
        identical rows, identical storage layout and an identical
        semantic_hash. world/README.md classes the raw byte hash as a
        diagnostic for exactly this reason."""
        from world import snapshot
        tar = snapshot.save(self.w1, self.tmp / "w1.tar")
        restored = snapshot.restore(tar, self.tmp / "w1_restored")
        self.assertTrue(snapshot.verify(self.w1, restored))

    def test_build_is_fast_enough_to_be_a_fixture(self):
        """Recorded above; bounded loosely here so a 10x regression is loud
        without making a slow CI host flaky."""
        self.assertLess(max(self.times), 300, self.times)

    # ------------------------------------------------------------------
    # dbt
    # ------------------------------------------------------------------

    def test_dbt_build_ran_every_model_and_test_green(self):
        results = json.loads((self.w1 / "build" / "target" / "run_results.json")
                             .read_text(encoding="utf-8"))["results"]
        statuses = {}
        for r in results:
            kind = r["unique_id"].split(".")[0]
            statuses.setdefault(kind, set()).add(r["status"])
        self.assertEqual(statuses["model"], {"success"})
        self.assertEqual(statuses["test"], {"pass"})
        n_tests = sum(1 for r in results if r["unique_id"].startswith("test."))
        n_models = sum(1 for r in results if r["unique_id"].startswith("model."))
        self.assertGreaterEqual(n_tests, 50)
        self.assertEqual(n_models, 9)
        # and the singular reconciliation test is among them
        self.assertTrue(any("assert_order_amount_matches_lines" in r["unique_id"]
                            for r in results))

    def test_dbt_test_passes_again_on_the_built_world(self):
        world = self.copy("dbt_test")
        rc, output = executor.dbt_invoke(world, "test", out_dir=world / "build" / "t")
        self.assertEqual(rc, 0, output[-3000:])

    def test_every_dbt_view_is_readable_through_the_alias(self):
        self.assertEqual(executor.unreadable_views(self.w1), {})

    def _dbt_inline(self, world: Path, command: str, sql: str) -> tuple[int, str]:
        """`dbt <command> --inline <sql>` against the world, run the way
        executor.dbt_invoke runs dbt (which takes no --inline)."""
        import subprocess
        out_dir = executor.write_profiles(world, world / "build" / "inline")
        argv = [sys.executable, "-c", "from dbt.cli.main import cli; cli()", command,
                "--quiet", "--inline", sql,
                "--project-dir", str(world / "workspace" / "dbt_project"),
                "--profiles-dir", str(out_dir),
                "--target-path", str(out_dir / "target"),
                "--log-path", str(out_dir / "logs")]
        if command == "show":
            argv += ["--output", "json"]
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def test_dbt_show_inline_ref_returns_the_warehouse_truth(self):
        """`dbt show --inline "... {{ ref('fct_orders') }}"`: an inline query is a
        `sql_operation` node, and macros/relations.sql must put the database back
        on for it as it does for a test - two-part `warehouse.fct_orders` against
        a file whose catalog is also `warehouse` is a Binder Error ("Ambiguous
        reference to catalog or schema"). The number dbt returns is the one the
        warehouse holds, read through the contract alias; afterwards `dbt build`
        still passes and every stored view still reads through the alias (no
        catalog baked into a body)."""
        world = self.copy("dbt_inline")
        truth = scalar(world, "SELECT sum(order_amount) FROM warehouse.fct_orders "
                              "WHERE order_status = 'completed'")
        self.assertIsNotNone(truth)

        rc, output = self._dbt_inline(
            world, "compile", "select 1 from {{ ref('fct_orders') }}")
        self.assertEqual(rc, 0, output[-3000:])
        self.assertIn('"warehouse"."warehouse"."fct_orders"', output)

        rc, output = self._dbt_inline(
            world, "show", "select sum(order_amount) as net_revenue from "
                           "{{ ref('fct_orders') }} where order_status = 'completed'")
        self.assertEqual(rc, 0, output[-3000:])
        shown = json.loads(output[output.index("{"):output.rindex("}") + 1])
        self.assertEqual(Decimal(str(shown["show"][0]["net_revenue"])), Decimal(str(truth)))

        rc, output = executor.dbt_invoke(world, "build", out_dir=world / "build" / "b")
        self.assertEqual(rc, 0, output[-3000:])
        self.assertEqual(executor.unreadable_views(world), {})

    # ------------------------------------------------------------------
    # the star schema contract
    # ------------------------------------------------------------------

    def test_marts_columns_and_types_are_exactly_examples_ddl(self):
        import duckdb
        mem = duckdb.connect(":memory:")
        con = connect(self.w1)
        try:
            for table, ddl in examples.DDL.items():
                mem.execute(f"create table {table} ({ddl})")
                want = mem.execute(
                    "select column_name, data_type from information_schema.columns "
                    "where table_name = ? order by ordinal_position", [table]).fetchall()
                got = con.execute(
                    "select column_name, data_type from information_schema.columns "
                    "where table_catalog = 'wh' and table_schema = 'warehouse' "
                    "and table_name = ? order by ordinal_position", [table]).fetchall()
                self.assertEqual(got, want, table)
        finally:
            con.close()
            mem.close()

    def test_order_amount_is_the_sum_of_its_lines(self):
        con = connect(self.w1)
        try:
            bad = con.execute("""
                select count(*) from warehouse.fct_orders as o
                left join (select order_id, sum(line_amount) as total, count(*) as n
                           from warehouse.fct_order_lines group by order_id) as l
                  on l.order_id = o.order_id
                where l.n is null or o.order_amount <> l.total""").fetchone()[0]
            orders, lines = con.execute(
                "select (select count(*) from warehouse.fct_orders), "
                "(select count(*) from warehouse.fct_order_lines)").fetchone()
        finally:
            con.close()
        self.assertEqual(bad, 0)
        self.assertGreater(orders, 10000)
        self.assertGreater(lines, orders)

    def test_values_are_in_the_declared_sets(self):
        con = connect(self.w1)
        try:
            def values(sql):
                return {r[0] for r in con.execute(sql).fetchall()}
            self.assertEqual(values("select distinct order_status from warehouse.fct_orders"),
                             {"completed", "refunded", "cancelled"})
            self.assertEqual(values("select distinct segment from warehouse.dim_customer"),
                             {"Enterprise", "SMB", "Consumer"})
            self.assertEqual(values("select distinct region from warehouse.dim_customer"),
                             {"NA", "EMEA", "APAC"})
            self.assertEqual(values("select distinct category from warehouse.dim_product"),
                             {"Desks", "Seating", "Accessories", "Lighting", "Services"})
            mix = dict(con.execute("select order_status, count(*) from warehouse.fct_orders "
                                   "group by 1").fetchall())
            customers = con.execute("select count(*) from warehouse.dim_customer").fetchone()[0]
            products = con.execute("select count(*) from warehouse.dim_product").fetchone()[0]
            # amounts are integer cents: no value carries a third decimal place
            fractional = con.execute(
                "select count(*) from warehouse.fct_order_lines "
                "where line_amount * 100 <> round(line_amount * 100)").fetchone()[0]
        finally:
            con.close()
        total = sum(mix.values())
        # realistic rates, not a uniform draw: most orders complete
        self.assertGreater(mix["completed"] / total, 0.85)
        self.assertLess(mix["refunded"] / total, 0.08)
        self.assertLess(mix["cancelled"] / total, 0.10)
        self.assertTrue(250 <= customers <= 400, customers)
        self.assertEqual(products, 25)
        self.assertEqual(fractional, 0)

    def test_dates_span_the_fixture_range_and_dim_date_covers_them(self):
        con = connect(self.w1)
        try:
            lo, hi = con.execute("select min(order_date), max(order_date) "
                                 "from warehouse.fct_orders").fetchone()
            d_lo, d_hi, n = con.execute("select min(date_day), max(date_day), count(*) "
                                        "from warehouse.dim_date").fetchone()
            orphans = con.execute(
                "select count(*) from warehouse.fct_orders as o "
                "left join warehouse.dim_date as d on d.date_day = o.order_date "
                "where d.date_day is null").fetchone()[0]
            sample = con.execute(
                "select * from warehouse.dim_date where date_day = date '2026-02-28'").fetchone()
        finally:
            con.close()
        self.assertEqual((lo, hi), (FIRST_DAY, LAST_DAY))
        self.assertEqual((d_lo, d_hi), (FIRST_DAY, LAST_DAY))
        self.assertEqual(n, (LAST_DAY - FIRST_DAY).days + 1)     # contiguous
        self.assertEqual(orphans, 0)
        # the same derivation examples.fixture_rows uses: ISO week, Sat/Sun weekend
        day = LAST_DAY
        self.assertEqual(sample, (day, 2026, 1, 2, "February",
                                  day - timedelta(days=day.weekday()), True))

    def test_last_year_exists_for_the_newest_months(self):
        """Fourteen months, so YoY measures have a prior year for Jan/Feb 2026."""
        rendered = bi.render(self.w1)["executive_overview"]["revenue_by_month"]
        self.assertEqual(len(rendered), 14)
        newest = {(r[0], r[1]): r for r in rendered}
        for month in (1, 2):
            row = newest[(2026, month)]
            self.assertIsNotNone(row[3], row)          # revenue_ly
            self.assertGreater(float(row[4]), 0.0)     # yoy_growth: the shop grew

    # ------------------------------------------------------------------
    # semantic layer and exposures
    # ------------------------------------------------------------------

    def manifest(self) -> dict:
        return json.loads((self.w1 / "build" / "target" / "manifest.json")
                          .read_text(encoding="utf-8"))

    def test_semantic_yaml_parses_and_names_the_metrics(self):
        sem_dir = self.w1 / "workspace" / "dbt_project" / "models" / "semantic"
        metrics = {m["name"]: m for m in yaml.safe_load(
            (sem_dir / "metrics.yml").read_text(encoding="utf-8"))["metrics"]}
        models = {m["name"]: m for m in yaml.safe_load(
            (sem_dir / "semantic_models.yml").read_text(encoding="utf-8"))["semantic_models"]}
        for name in ("gross_revenue", "net_revenue", "order_count", "completed_orders",
                     "aov", "units", "revenue_ytd", "revenue_ly", "yoy_growth"):
            self.assertIn(name, metrics)
        self.assertIn("completed", metrics["net_revenue"]["filter"])
        self.assertEqual(metrics["aov"]["type"], "ratio")
        self.assertEqual(metrics["revenue_ytd"]["type"], "cumulative")
        self.assertEqual(metrics["revenue_ytd"]["type_params"]["cumulative_type_params"],
                         {"grain_to_date": "year", "period_agg": "last"})
        self.assertEqual(metrics["revenue_ly"]["type_params"]["metrics"][0]["offset_window"],
                         "1 year")
        self.assertEqual(metrics["yoy_growth"]["type"], "derived")
        self.assertEqual(set(models), {"orders", "order_lines", "customers", "products"})
        self.assertEqual(models["orders"]["model"], "ref('fct_orders')")
        # The labels mirror examples.commerce_model()'s measure names, minus the
        # two MetricFlow cannot express (documented in metrics.yml).
        labels = {m["label"] for m in metrics.values()}
        reference = {m.name for m in examples.commerce_model().measures}
        self.assertEqual(reference - labels, {"Segment Share", "List Price Revenue"})
        # and dbt parsed all of it, against the time spine it requires
        manifest = self.manifest()
        parsed = {m["name"] for m in manifest["metrics"].values()}
        self.assertLessEqual(set(metrics), parsed)
        self.assertEqual({m["name"] for m in manifest["semantic_models"].values()},
                         set(models))
        self.assertTrue(manifest["nodes"]["model.commerce_analytics.dim_date"]["time_spine"])

    def test_semantic_layer_evaluates_like_the_panels_through_the_shared_engine(self):
        """The dbt semantic layer, read by world.semantic's dbt front-end
        (formats/yaml_model.py) and evaluated by its engine on the built
        warehouse, gives the numbers the SQL panels give. This is the check
        that the YAML mirrors examples.commerce_model() in substance, not
        just in names."""
        try:
            from world.semantic import render
            from world.semantic.expr import Col, MeasureRef
            from world.semantic.formats import yaml_model
            from world.semantic.model import Visual
        except ImportError as exc:                           # pragma: no cover
            self.skipTest(f"world.semantic front-end not importable: {exc}")
        model, _ = yaml_model.read_project(self.w1 / "workspace" / "dbt_project")
        render.validate(model)
        # the relationship graph is commerce_model()'s, bar the fact->fact join
        # the front-end keeps inactive by rule (its docstring, "Relationship")
        active = {(r.from_table, r.to_table) for r in model.relationships if r.active}
        self.assertLessEqual({("orders", "customers"), ("orders", "dim_date"),
                              ("order_lines", "products")}, active)
        wh = self.w1 / DB
        panels = bi.render(self.w1)["executive_overview"]

        def rows(*values, group_by=(), **kw):
            visual = Visual("check", group_by=group_by,
                            values=tuple((v, MeasureRef(v)) for v in values), **kw)
            return render.render_visual(model, visual, wh)["rows"]
        net, completed, _aov = rows("Net Revenue", "Completed Orders", "AOV")[0]
        self.assertEqual([net, completed], panels["kpis"][0][:2])
        by_segment = {r[0]: r[1] for r in rows("Net Revenue",
                                               group_by=(Col("customers", "segment"),))}
        self.assertEqual(by_segment, {r[0]: r[1] for r in panels["revenue_by_segment"]})
        top = rows("Units", group_by=(Col("products", "product_name"),),
                   order_by=(("Units", True),), top_n=3)
        self.assertEqual(top, panels["top_products"])
        monthly = rows("Net Revenue", "Revenue LY", "YoY Growth", "Revenue YTD",
                       group_by=(Col("dim_date", "metric_time__month"),))
        self.assertEqual(len(monthly), 14)
        by_month = {(int(r[0][:4]), int(r[0][5:7])): r for r in monthly}
        for year, month, net_m, ly, yoy in panels["revenue_by_month"]:
            row = by_month[(year, month)]
            self.assertEqual((row[1], row[2]), (net_m, ly), (year, month))
            if yoy is not None:
                self.assertAlmostEqual(float(row[3]), float(yoy), places=4)
        # YTD resets in January and accumulates within the year
        self.assertEqual(by_month[(2026, 1)][4], by_month[(2026, 1)][1])
        self.assertEqual(Decimal(by_month[(2026, 2)][4]),
                         Decimal(by_month[(2026, 1)][1]) + Decimal(by_month[(2026, 2)][1]))

    def test_exposures_reference_existing_models(self):
        manifest = self.manifest()
        exposures = {e["name"]: e for e in manifest["exposures"].values()}
        # The Executive Overview three times - leadership's (Power BI), Sales'
        # (Tableau) and Finance's (Sigma) builds, which is what the vendor
        # projects in bi/ are (gen.bi_projects) - and the dashboards that are
        # not in the repo.
        self.assertEqual(set(exposures), {"executive_overview", "executive_overview_tableau",
                                          "executive_overview_sigma", "sales_overview",
                                          "customer_health", "finance_close_pack"})
        for name, exposure in exposures.items():
            self.assertTrue(exposure["owner"]["email"], name)
            self.assertTrue(exposure["url"], name)
            self.assertIn(exposure["maturity"], {"high", "medium", "low"})
            self.assertTrue(exposure["depends_on"]["nodes"], name)
            for node in exposure["depends_on"]["nodes"]:
                self.assertTrue(node in manifest["nodes"] or node in manifest["metrics"],
                                f"{name} depends on {node}, which dbt does not know")
        exec_nodes = set(exposures["executive_overview"]["depends_on"]["nodes"])
        for table in examples.DDL:
            self.assertIn(f"model.commerce_analytics.{table}", exec_nodes)
        self.assertEqual(exposures["executive_overview"]["type"], "dashboard")

    # ------------------------------------------------------------------
    # dashboards
    # ------------------------------------------------------------------

    def test_dashboards_render_and_agree_with_the_warehouse(self):
        rendered = bi.render(self.w1)
        self.assertEqual(set(rendered), {"executive_overview", "sales_overview"})
        net = scalar(self.w1, "select sum(order_amount) from warehouse.fct_orders "
                              "where order_status = 'completed'")
        kpis = rendered["executive_overview"]["kpis"][0]
        self.assertEqual(kpis[0], str(net))
        self.assertEqual(len(rendered["executive_overview"]["top_products"]), 3)
        self.assertEqual({r[0] for r in rendered["executive_overview"]["revenue_by_segment"]},
                         {"Enterprise", "SMB", "Consumer"})
        regions = rendered["sales_overview"]["revenue_by_region_month"]
        self.assertEqual(len(regions), 3 * 14)
        # category revenue (line grain) and Net Revenue (order grain) reconcile
        category_total = sum(Decimal(r[1]) for r in
                             rendered["sales_overview"]["revenue_by_category"])
        self.assertEqual(category_total, net)

    @unittest.skipIf(GATEWAY, GATEWAY or "")
    def test_dashboards_are_valid_gateway_definitions(self):
        for path in sorted((self.w1 / "var" / "bi" / "dashboards").glob("*.yml")):
            Dashboard.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    # ------------------------------------------------------------------
    # the workflow corpus
    # ------------------------------------------------------------------

    def documents(self) -> dict:
        return {name: json.loads((self.w1 / "var" / "workflow" / name).read_text(encoding="utf-8"))
                for name in ("issues.json", "chat.json", "kb.json")}

    def test_workflow_corpus_loads_through_the_world_loader(self):
        manifest = load_theme("commerce_analytics").manifest
        docs = workflow.documents(THEME_DIR, manifest["workflow"], VNOW)
        self.assertEqual(json.loads(json.dumps(docs)), self.documents())
        self.assertEqual(scheduler.load(self.w1)["vnow"], VNOW)
        issues = docs["issues.json"]
        titles = {i["title"] for i in issues["issues"]}
        for wanted in ("Exec dashboard revenue doesn't match Finance's close number",
                       "Add region filter to Sales Overview",
                       "Tableau extract refresh failing"):
            self.assertIn(wanted, titles)
        self.assertEqual({c["id"] for c in docs["chat.json"]["channels"]},
                         {"analytics-eng", "data-incidents", "bi-requests", "finance-analytics"})
        pages = {p["id"] for p in docs["kb.json"]["pages"]}
        for wanted in ("ae/metrics/net-revenue", "ae/runbooks/bi-release-process",
                       "ae/architecture/data-dictionary", "ae/runbooks/nightly-on-call"):
            self.assertIn(wanted, pages)
        # the deliberate-imperfection catalogue every theme plants
        kinds = []

        def walk(node):
            if isinstance(node, dict):
                block = node.get("authoring")
                if isinstance(block, dict) and "kind" in block:
                    kinds.append(block["kind"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
        walk(docs)
        self.assertEqual({
            "noise", "stale", "near_duplicate_trap", "duplicate", "constraint",
            "undocumented_decision", "unanswered_mention", "confident_wrong",
            "accurate_but_partial", "unactioned_remediation",
            "current_and_contradicting", "incident_report_lay", "accurate",
        } - set(kinds), set())

    def test_the_numbers_the_tickets_quote_are_true_of_the_world(self):
        """The corpus was measured against a built world; this keeps it so. A
        generator change that moves January's revenue must move the tickets."""
        jan_now = scalar(self.w1, """
            select sum(order_amount) from warehouse.fct_orders
            where order_status = 'completed'
              and order_date between date '2026-01-01' and date '2026-01-31'""")
        as_of = """
            (select * from (
               select cast(order_id as integer) as order_id, status,
                      cast(subtotal as decimal(12,2)) as amount,
                      cast(cast(ordered_at as timestamp) as date) as order_date,
                      cast(shipping as decimal(12,2)) as shipping,
                      row_number() over (partition by order_id
                        order by cast(updated_at as timestamp) desc, dt desc) as rk
               from raw.orders where dt <= date '{cut}') where rk = 1)"""
        jan_close = scalar(self.w1, f"""
            select sum(amount) from {as_of.format(cut='2026-02-03')}
            where status = 'completed' and order_date between '2026-01-01' and '2026-01-31'""")
        refunds = connect(self.w1).execute("""
            select count(*), sum(cast(subtotal as decimal(12,2))) from raw.orders
            where status = 'refunded' and dt between date '2026-02-04' and date '2026-02-28'
              and cast(cast(ordered_at as timestamp) as date)
                  between date '2026-01-01' and date '2026-01-31'""").fetchone()
        dec_net, dec_ship = connect(self.w1).execute(f"""
            select sum(amount), sum(shipping) from {as_of.format(cut='2026-01-05')}
            where status = 'completed' and order_date between '2025-12-01' and '2025-12-31'
        """).fetchone()
        self.assertEqual(jan_now, Decimal("2386405.00"))
        self.assertEqual(jan_close, Decimal("2420528.00"))
        self.assertEqual(refunds, (20, Decimal("34123.00")))
        self.assertEqual(jan_close - jan_now, refunds[1])
        self.assertEqual((dec_net, dec_ship), (Decimal("3388969.00"), Decimal("57097.10")))
        # and the dashboard tile Finance read says the same thing
        tile = {(r[0], r[1]): r[2] for r in
                bi.render(self.w1)["executive_overview"]["revenue_by_month"]}
        self.assertEqual(tile[(2026, 1)], "2386405.00")
        text = json.dumps(self.documents())
        for quoted in ("2,386,405.00", "2,420,528.00", "34,123.00", "3,446,066.10",
                       "57,097.10"):
            self.assertIn(quoted, text)

    @unittest.skipIf(GATEWAY, GATEWAY or "")
    def test_workflow_corpus_reads_back_through_the_gateway(self):
        world = self.copy("gateway")
        config = GatewayConfig(
            workspace=world / "workspace",
            warehouse=world / DB,
            scheduler=world / "var" / "sched" / "state.json",
            dashboards=world / "var" / "bi" / "dashboards",
            observability=world / "var" / "obs" / "definitions.yaml",
            workflow_dir=world / "var" / "workflow",
            audit_dir=self.tmp / "audit_gateway",
            profiles_dir=world / "build",
            episode_id="audit_gateway",
            initial_world_fp=fingerprint.semantic_hash(world),
        )
        gateway = Gateway(config)
        try:
            def call(tool, **args):
                response = gateway.call(tool, args)
                self.assertTrue(response["ok"], response.get("error"))
                return response["result"]
            listing = call("issues_list")
            self.assertEqual(listing["statuses"],
                             ["backlog", "todo", "in_progress", "in_review", "done", "wont_do"])
            self.assertEqual(listing["total"], 11)
            incident = call("issues_get", issue="ANA-238")
            self.assertEqual(incident["reporter"], "achen")
            unanswered = call("chat_thread", message_id="analytics-eng/0006")
            self.assertEqual(unanswered["reply_count"], 0)
            self.assertEqual(unanswered["root"]["mentions"], ["rdesai"])
            pages = {p["id"]: p for p in call("kb_list")["pages"]}
            self.assertGreater(pages["ae/runbooks/publishing-dashboards"]["age_days"],
                               pages["ae/runbooks/bi-release-process"]["age_days"] + 90)
            rendered = call("bi_render", dashboard="executive_overview")
            evidence = json.dumps([listing, incident, unanswered, pages], sort_keys=True)
        finally:
            gateway.close()
        self.assertIn("kpis", json.dumps(rendered))
        self.assertNotIn("authoring", evidence)
        self.assertNotIn("CA-I", evidence)

    # ------------------------------------------------------------------
    # native discovery
    # ------------------------------------------------------------------

    def test_native_discovery_files_build(self):
        seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
        seed["native"] = True
        flagged = self.tmp / "native_seed.yaml"
        flagged.write_text(yaml.safe_dump(seed), encoding="utf-8")
        world = wbuild.build(flagged, self.tmp / "native")
        envrc = (world / "workspace" / ".envrc").read_text(encoding="utf-8")
        self.assertIn('export LANDING_ROOT=', envrc)                 # the theme's own half
        self.assertIn("platform-managed: service endpoints", envrc)  # the provisioned block
        onboarding = (world / "workspace" / "docs" / "onboarding.md").read_text(encoding="utf-8")
        for job in ("daily_load", "dbt_build", "bi_refresh"):
            self.assertIn(f"`{job}`", onboarding)
        self.assertIn("`ae/runbooks/bi-release-process`", onboarding)
        self.assertTrue((world / "home" / ".dbt" / "profiles.yml").is_file())
        import tomllib
        tomllib.loads((world / "home" / ".config" / "alkera" / "services.toml")
                      .read_text(encoding="utf-8"))
        self.assertIn("home", fingerprint.semantic(world))
        # the flag changes discovery files, never the data
        self.assertEqual(fingerprint.semantic(world)["warehouse"],
                         fingerprint.semantic(self.w1)["warehouse"])

    # ------------------------------------------------------------------
    # the live world
    # ------------------------------------------------------------------

    def test_faults_are_declared_and_all_off(self):
        live = scheduler.load_live(self.w1)
        faults = {f["id"]: f for f in live["faults"]}
        self.assertEqual(set(faults), {
            "extract_late", "extract_missing", "order_lines_resent", "orders_resent",
            "order_lines_torn", "load_partial", "dbt_build_timeout",
            "bi_refresh_retry", "bi_refresh_overrun"})
        self.assertFalse(any(f["enabled"] for f in faults.values()))
        self.assertEqual(live["fault_log"], [])
        runs = scheduler.load(self.w1)["runs"]
        self.assertEqual(len(runs), 3 * 425)
        self.assertEqual({r["status"] for r in runs}, {"success"})
        self.assertTrue(all(r["vts_end"] < VNOW for r in runs),
                        "a seeded run sits after the world clock")

    def test_advance_24_executes_one_nightly_cycle(self):
        world = self.copy("advance")
        result = scheduler.advance(world, 24)
        self.assertEqual(result["vnow"], "2026-03-03T00:00:00Z")
        kinds = [(e["kind"], e["name"]) for e in result["events"]]
        self.assertEqual(kinds, [("arrival", "shop_extract"), ("job", "daily_load"),
                                 ("job", "dbt_build"), ("job", "bi_refresh")])
        self.assertEqual(result["events"][0]["produced"]["partition"], "dt=2026-03-01")
        new_runs = scheduler.load(world)["runs"][3 * 425:]
        self.assertEqual([r["job"] for r in new_runs], ["daily_load", "dbt_build", "bi_refresh"])
        for run in new_runs:
            self.assertEqual((run["status"], run["executed"], run["error"]),
                             ("success", True, None), run)
        self.assertEqual(new_runs[2]["effects"]["dashboards"], 2)
        self.assertEqual(scalar(world, "select max(order_date) from warehouse.fct_orders"),
                         date(2026, 3, 1))
        # the refresh logged what it showed, and wrote the BI cache outside the tree
        self.assertEqual(scalar(world, "select max(data_through) from raw.bi_refresh_history"),
                         date(2026, 3, 1))
        cached = json.loads((world / "build" / "bi" / "refresh" / "executive_overview.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(cached["data_through"], "2026-03-01")
        # the workspace script the job runs renders what world.bi renders
        rendered = bi.render(world)
        for name in ("executive_overview", "sales_overview"):
            cache = json.loads((world / "build" / "bi" / "refresh" / f"{name}.json")
                               .read_text(encoding="utf-8"))
            self.assertEqual(cache["panels"], rendered[name], name)
        self.assertEqual(executor.unreadable_views(world), {})
        # observability: null before the first logged refresh, a reading after it
        readings = [s for s in observe.samples(world) if s["metric"] == "bi_data_freshness"]
        self.assertIsNone(readings[0]["value"])                      # the build's reading
        # 06:07, data through yesterday
        self.assertEqual(readings[-1]["value"], 30 * 3600 + 7 * 60)

    def test_advance_is_path_independent(self):
        once = self.copy("once")
        twice = self.copy("twice")
        scheduler.advance(once, 24)
        scheduler.advance(twice, 9)
        scheduler.advance(twice, 15)
        self.assertEqual(fingerprint.semantic(once), fingerprint.semantic(twice))

    def test_write_day_is_pure_in_seed_and_day(self):
        """Day N from a cold generator equals the bytes the build landed."""
        gen = load_theme("commerce_analytics").gen
        gen._SHOPS.clear()
        seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
        out = self.tmp / "cold_day"
        gen.write_day(out, seed, 300, date(2025, 10, 28))
        for entity in gen.ENTITIES:
            rel = Path("data/landing") / entity / "dt=2025-10-27" / f"{entity}.csv"
            self.assertEqual((out / rel).read_bytes(), (self.w1 / "workspace" / rel).read_bytes())

    def test_late_extract_stages_a_stale_publish(self):
        """The fault this theme exists to plant: the extract lands after the
        06:00 refresh. Every job is green; the dashboards show the day before."""
        world = self.copy("late")
        live = scheduler.load_live(world)
        for fault in live["faults"]:
            if fault["id"] == "extract_late":
                fault["enabled"], fault["only"] = True, ["day:425"]
        scheduler.save_live(world, live)
        result = scheduler.advance(world, 24)
        jobs = [e for e in result["events"] if e["kind"] == "job"]
        self.assertEqual([j["status"] for j in jobs], ["success"] * 3)
        arrival = next(e for e in result["events"] if e["kind"] == "arrival")
        self.assertEqual((arrival["vts"], arrival["late"]), ("2026-03-02T07:30:00Z", True))
        self.assertEqual(scalar(world, "select max(data_through) from raw.bi_refresh_history"),
                         date(2026, 2, 28))
        # the next night's full reload heals the mart; the refresh catches up
        scheduler.advance(world, 24)
        self.assertEqual(scalar(world, "select max(data_through) from raw.bi_refresh_history"),
                         date(2026, 3, 2))

    def test_refresh_fails_honestly_with_no_dashboards(self):
        world = self.copy("nobi")
        for path in (world / "var" / "bi" / "dashboards").glob("*.yml"):
            path.unlink()
        run = scheduler.trigger(world, "bi_refresh")
        self.assertEqual(run["status"], "failed")
        self.assertIn("no dashboards deployed", run["error"])

    # ------------------------------------------------------------------
    # the built repo
    # ------------------------------------------------------------------

    def test_the_built_workspace_is_fully_committed(self):
        import subprocess
        for args in (["status", "--porcelain"], ["status", "--porcelain", "--ignored"]):
            result = subprocess.run(["git", *args], cwd=self.w1 / "workspace",
                                    capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, ""), args)

    def test_dag_check_runs_clean_against_the_built_world(self):
        import subprocess
        ws = self.w1 / "workspace"
        result = subprocess.run([sys.executable, str(ws / "scripts" / "dag_check.py")],
                                cwd=ws, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("clean:", result.stdout)

    def test_workspace_loader_is_the_scheduled_load(self):
        """daily_load runs loader/load_extracts.py; re-running it by hand over
        the same landing zone leaves every raw table hashing the same."""
        import subprocess
        world = self.copy("reload")
        before = fingerprint.semantic(world)["warehouse"]["tables"]
        ws = world / "workspace"
        result = subprocess.run(
            [sys.executable, str(ws / "loader" / "load_extracts.py"),
             "--landing", str(ws / "data" / "landing"), "--db", str(world / DB)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, fingerprint.semantic(world)["warehouse"]["tables"])


# --------------------------------------------------------------------------
# the repo the agent opens: the conventions test_workspace_repo.py pins for
# the other four themes, applied here
# --------------------------------------------------------------------------

WORKSPACE = THEME_DIR / "workspace"


def dag_check_module():
    """The workspace's own scripts/dag_check.py, loaded without bytecode (the
    workspace tree is hashed)."""
    import importlib.util
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(
            "_dag_check_commerce", WORKSPACE / "scripts" / "dag_check.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = previous


class CommerceWorkspaceRepoTest(unittest.TestCase):
    """No world build: the theme's workspace as files."""

    def test_dags_are_pinned_to_the_manifest(self):
        module = dag_check_module()
        dags = module.read_dags(WORKSPACE / "dags")
        jobs = load_theme("commerce_analytics").manifest["jobs"]
        tasks, edges = {}, set()
        for dag in dags:
            tasks.update(dag["tasks"])
            edges |= set(dag["edges"])
        self.assertEqual(set(tasks), set(jobs))
        self.assertTrue(all(module.is_job(op) for op in tasks.values()))
        self.assertIn(("daily_load", "dbt_build"), edges)
        # every root job (no `after:`) owns a DAG whose schedule is its time.
        # Two roots on purpose: the refresh is not downstream of the build.
        roots = {n: s for n, s in jobs.items() if not s.get("after")}
        self.assertEqual(set(roots), {"daily_load", "bi_refresh"})
        for name, spec in roots.items():
            owning = [d for d in dags if name in d["tasks"]]
            self.assertEqual(len(owning), 1, name)
            hour, minute = spec["time"].split(":")
            self.assertEqual(owning[0]["schedule"], f"{int(minute)} {int(hour)} * * *")
        self.assertNotIn("bi_refresh", {b for _, b in edges})
        self.assertEqual(sorted(p.as_posix() for p in WORKSPACE.rglob("__pycache__")), [])

    def test_config_surface_is_wired(self):
        envrc = (WORKSPACE / ".envrc").read_text(encoding="utf-8")
        run_sh = (WORKSPACE / "run.sh").read_text(encoding="utf-8")
        self.assertIn(".env", envrc)
        self.assertIn(".envrc", run_sh)
        self.assertIn("DBT_PROFILES_DIR", run_sh)
        ignore = (WORKSPACE / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".env", ignore)
        self.assertNotIn(".envrc", ignore)
        self.assertFalse((WORKSPACE / "dbt_project" / "profiles.yml").exists())
        self.assertTrue((WORKSPACE / "config" / "profiles.yml").is_file())
        import re
        for config in WORKSPACE.rglob(".github/workflows/*.yml"):
            for target in re.findall(r"\./run\.sh\s+([a-z-]+)", config.read_text(encoding="utf-8")):
                self.assertRegex(run_sh, rf"(?m)^\s*{re.escape(target)}\)", target)
        # the DAG's commands are run.sh targets too
        for dag in (WORKSPACE / "dags").glob("*.py"):
            for target in re.findall(r"\./run\.sh\s+([a-z-]+)", dag.read_text(encoding="utf-8")):
                self.assertRegex(run_sh, rf"(?m)^\s*{re.escape(target)}\)", target)

    def test_no_credential_shaped_strings_and_nothing_the_outer_repo_ignores(self):
        import re
        import subprocess
        patterns = (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"\bAKIA[0-9A-Z]{16}\b",
                    r"\bxox[baprs]-[A-Za-z0-9-]{10,}", r"\bghp_[A-Za-z0-9]{20,}\b",
                    r"(?i)\b(password|passwd|secret|token|api_key)\s*[:=]\s*"
                    r"['\"]?[A-Za-z0-9+/=_\-]{12,}")
        files = [p for p in THEME_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.isascii() or path.suffix == ".py", path)
            for pattern in patterns:
                self.assertIsNone(re.search(pattern, text), f"{path}: {pattern}")
        repo = TRACK_A.parent
        result = subprocess.run(["git", "check-ignore", "--stdin"], cwd=repo,
                                input="\n".join(str(p.relative_to(repo)) for p in files),
                                capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "")

    def test_the_bi_directory_describes_the_vendor_projects_without_shipping_them(self):
        readme = (WORKSPACE / "bi" / "README.md").read_text(encoding="utf-8")
        for path in ("bi/powerbi", "bi/tableau", "bi/sigma", "dbt_project/models/semantic"):
            self.assertIn(path, readme)
        self.assertEqual([p.name for p in (WORKSPACE / "bi").iterdir()], ["README.md"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
