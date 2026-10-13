"""sftp_vendor theme acceptance tests (stdlib unittest).

Same double-build gate as test_world, plus BI render and content checks
against the drop folders and raw tables.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import bi, build as wbuild, fingerprint  # noqa: E402

TRACK_A = Path(__file__).resolve().parents[1]
SEED = TRACK_A / "world" / "themes" / "sftp_vendor" / "seed.example.yaml"
DB = Path("var/warehouse/warehouse.duckdb")


class SftpVendorThemeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="world_sftp_vendor_"))
        cls.w1 = wbuild.build(SEED, cls.tmp / "w1")
        cls.w2 = wbuild.build(SEED, cls.tmp / "w2")
        print("\nsemantic_hash:", fingerprint.semantic_hash(cls.w1))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_semantic_fingerprint_stable(self):
        """Build-from-scratch-twice gate (same host here; CI runs cross-host)."""
        self.assertEqual(fingerprint.semantic(self.w1), fingerprint.semantic(self.w2))

    def test_bi_render_matches_warehouse(self):
        import duckdb
        rendered = bi.render(self.w1)
        # attach under a neutral alias: catalog == schema == "warehouse" otherwise
        con = duckdb.connect(":memory:")
        con.execute("ATTACH '" + str(self.w1 / DB).replace("'", "''")
                    + "' AS wh (READ_ONLY)")
        con.execute("USE wh.warehouse")
        net = con.execute(
            "select sum(net_revenue) from warehouse.fct_orders_daily"
        ).fetchone()[0]
        con.close()
        self.assertEqual(rendered["vendor_daily"]["totals"][0][2], str(net))
        self.assertEqual(len(rendered["vendor_daily"]["daily_net_revenue"]), 30)

    def test_ingest_state_seeded_and_consistent(self):
        """The connector control plane: one sync per connector per day whose
        row counts reconcile with raw exactly, and whose failures are the same
        nights the scheduler recorded a failed poll on.

        It used to seed a GREEN sync unconditionally. Once the poll became able
        to fail, that made six nights where `ingest_runs` reported rows out of
        a drop folder `sched_logs` says was empty for another two hours. The
        late nights are read back out of the scheduler here - not re-derived
        from the fault catalogue - so this asserts the two planes agree rather
        than asserting that one of them can compute itself.
        """
        import json
        state = json.loads(
            (self.w1 / "var" / "ingest" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(state["connectors"]), ["acme_orders", "globex_returns"])
        self.assertEqual(len(state["runs"]), 60)
        self.assertEqual(len(state["schemas"]), 2)
        sched_state = json.loads(
            (self.w1 / "var" / "sched" / "state.json").read_text(encoding="utf-8"))
        withheld = {r["vts_start"][:10] for r in sched_state["runs"]
                    if r["job"] == "sftp_poll" and r["status"] == "failed"
                    and "file(s) matching" in (r.get("error") or "")}
        # Non-vacuous on both sides: nights where the poll failed exist, and
        # nights where it did not exist too. Without the first, "every sync is
        # green" would pass this; without the second, "every sync failed" would.
        self.assertTrue(withheld, "no withheld delivery in the backstory")
        failed = [r for r in state["runs"] if r["status"] == "failed"]
        green = [r for r in state["runs"] if r["status"] == "success"]
        self.assertTrue(green)
        self.assertEqual({r["started_at"][:10] for r in failed}, withheld)
        self.assertEqual(len(failed), 2 * len(withheld))   # both connectors
        for run in failed:
            self.assertTrue(all(c["rows_inserted"] == 0
                                for c in run["tables"].values()), run)
            self.assertEqual(run["timeline"][-1]["event"], "failed")
        # every failure raised exactly one alert, and nothing else did
        self.assertEqual(len(state["alerts"]), len(failed))
        self.assertEqual({a["run_id"] for a in state["alerts"]},
                         {r["id"] for r in failed})
        for alert in state["alerts"]:
            self.assertEqual(alert["kind"], "sync_failed")
            self.assertIs(alert["acknowledged"], False)
        # sync history stays inside the scheduler's run window: every sync
        # finishes before the last dbt_build and starts after the first poll
        window_end = max(r["vts_end"] for r in sched_state["runs"])
        window_start = min(r["vts_start"] for r in sched_state["runs"])
        self.assertTrue(all(r["finished_at"] <= window_end for r in state["runs"]))
        self.assertTrue(all(r["started_at"] >= window_start for r in state["runs"]))
        # per-table inserted totals match the raw tables the loader produced.
        # A withheld night does not lose its rows, it delays them: the delivery
        # lands hours before the connector's next slot, so the next morning's
        # sync carries both nights and the sum is unchanged. That only holds
        # while the LAST seeded night arrived - otherwise its rows are still
        # outstanding when the history ends, which is the honest reading and
        # not this assertion. Measured: the last night is not a withheld one.
        self.assertNotIn(max(r["started_at"][:10] for r in state["runs"]),
                         withheld, "the last seeded night was withheld: its "
                                   "rows are still outstanding, so the totals "
                                   "below cannot reconcile with raw")
        import duckdb
        totals = {}
        for run in state["runs"]:
            for table, counts in run["tables"].items():
                totals[table] = totals.get(table, 0) + counts["rows_inserted"]
        con = duckdb.connect(str(self.w1 / DB), read_only=True)
        try:
            for full, expected in totals.items():
                schema, table = full.split(".")
                n = con.execute(
                    f'select count(*) from "warehouse"."{schema}"."{table}"'
                ).fetchone()[0]
                self.assertEqual(n, expected)
        finally:
            con.close()
        # the fingerprint carries the conditional ingest section
        self.assertIn("ingest", fingerprint.semantic(self.w1))

    def test_drop_layout_and_raw_counts(self):
        """Every CSV has a .done sentinel; raw row counts match the files."""
        import duckdb
        drop = self.w1 / "workspace" / "data" / "drop"
        for sub, prefix in (("acme", "ORDERS_"), ("globex", "RETURNS_")):
            csvs = sorted((drop / sub).glob(f"{prefix}*.csv"))
            self.assertEqual(len(csvs), 30)
            for f in csvs:
                self.assertTrue(f.with_suffix(".done").is_file())
        con = duckdb.connect(str(self.w1 / DB), read_only=True)
        try:
            for sub, prefix, table in (
                    ("acme", "ORDERS_", "vendor_orders"),
                    ("globex", "RETURNS_", "vendor_returns")):
                expected = sum(len(f.read_text().splitlines()) - 1
                               for f in (drop / sub).glob(f"{prefix}*.csv"))
                n = con.execute(
                    f'select count(*) from "warehouse"."raw"."{table}"'
                ).fetchone()[0]
                self.assertEqual(n, expected)
        finally:
            con.close()

    def test_every_dbt_view_is_readable_through_the_alias(self):
        """A view's body is STORED, so it can be created and still be
        unreadable. dbt bakes `warehouse.` into any three-part reference it
        renders (it opens the file directly, where the catalog is the file
        stem); every reader must ATTACH under an alias instead, and the baked
        name then fails to bind. Reads every view, not one."""
        from world import executor
        self.assertEqual(executor.unreadable_views(self.w1), {})

    def _dbt_inline(self, world: Path, command: str, sql: str) -> tuple[int, str]:
        """`dbt <command> --inline <sql>` against the world, run the way
        executor.dbt_invoke runs dbt (which takes no --inline)."""
        import subprocess
        from world import executor
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

    def test_dbt_show_inline_ref_resolves(self):
        """`dbt show --inline "... {{ ref('fct_orders_daily') }}"`: an inline
        query is a `sql_operation` node, and macros/relations.sql must put the
        database back on for it as it does for a test - two-part
        `warehouse.fct_orders_daily` against a file whose catalog is also
        `warehouse` is a Binder Error ("Ambiguous reference to catalog or
        schema"). The count dbt returns is the one the warehouse holds, read
        through the contract alias; afterwards `dbt build` still passes and
        every stored view still reads through the alias (no catalog baked into
        a body)."""
        import json
        import duckdb
        from world import executor
        world = shutil.copytree(self.w1, self.tmp / "dbt_inline")
        con = duckdb.connect(":memory:")
        con.execute("ATTACH '" + str(world / DB).replace("'", "''") + "' AS wh (READ_ONLY)")
        truth = con.execute("select count(*) from wh.warehouse.fct_orders_daily").fetchone()[0]
        con.close()
        self.assertGreater(truth, 0)

        rc, output = self._dbt_inline(
            world, "compile", "select 1 from {{ ref('fct_orders_daily') }}")
        self.assertEqual(rc, 0, output[-3000:])
        self.assertIn('"warehouse"."warehouse"."fct_orders_daily"', output)

        rc, output = self._dbt_inline(
            world, "show", "select count(*) as n from {{ ref('fct_orders_daily') }}")
        self.assertEqual(rc, 0, output[-3000:])
        shown = json.loads(output[output.index("{"):output.rindex("}") + 1])
        self.assertEqual(shown["show"][0]["n"], truth)

        rc, output = executor.dbt_invoke(world, "build", out_dir=world / "build" / "b")
        self.assertEqual(rc, 0, output[-3000:])
        self.assertEqual(executor.unreadable_views(world), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
