"""mainframe_batch theme acceptance tests (stdlib unittest).

Builds the theme's seed.example.yaml twice from scratch and gates on the
semantic fingerprint, then checks the BI render against the warehouse.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import bi, build as wbuild, fingerprint, scheduler  # noqa: E402

TRACK_A = Path(__file__).resolve().parents[1]
SEED = TRACK_A / "world" / "themes" / "mainframe_batch" / "seed.example.yaml"
LOCK = TRACK_A / "requirements.lock"


class MainframeBatchThemeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="world_mb_"))
        lock = LOCK if LOCK.exists() else None
        cls.w1 = wbuild.build(SEED, cls.tmp / "w1", lock)
        cls.w2 = wbuild.build(SEED, cls.tmp / "w2", lock)
        print("\nsemantic_hash:", fingerprint.semantic_hash(cls.w1))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_semantic_fingerprint_stable(self):
        self.assertEqual(fingerprint.semantic(self.w1), fingerprint.semantic(self.w2))

    def test_scheduler_batch_chain(self):
        runs = scheduler.load(self.w1)["runs"]
        first = [r for r in runs if r["detail"] == "dt=2026-02-01"
                 or r["vts_start"].startswith("2026-02-01")]
        by_job = {r["job"]: r["vts_start"] for r in first}
        self.assertEqual(by_job["nightly_extract"], "2026-02-01T23:00:00Z")
        self.assertEqual(by_job["batch_load"], "2026-02-01T23:45:00Z")
        self.assertEqual(by_job["dbt_build"], "2026-02-01T23:55:00Z")

    def test_bi_render_matches_warehouse(self):
        import duckdb
        rendered = bi.render(self.w1)
        # attach under a neutral alias: opening warehouse.duckdb directly makes
        # the catalog and the schema both "warehouse", and every 2-part
        # reference becomes ambiguous
        con = duckdb.connect(":memory:")
        con.execute("ATTACH '" + str(self.w1 / "var/warehouse/warehouse.duckdb")
                    .replace("'", "''") + "' AS wh (READ_ONLY)")
        con.execute("USE wh.warehouse")
        total = con.execute(
            "select sum(total_amount) from warehouse.fct_claims_daily"
        ).fetchone()[0]
        raw_total = con.execute(
            'select sum(claim_amt) from "wh"."raw"."claims_extract"'
        ).fetchone()[0]
        con.close()
        self.assertEqual(total, raw_total)
        self.assertEqual(
            rendered["claims_daily"]["grand_total"][0][0], str(total))
        self.assertEqual(len(rendered["claims_daily"]["daily_claims"]), 30)

    def test_comp3_round_trip(self):
        gen_path = TRACK_A / "world" / "themes" / "mainframe_batch" / "gen.py"
        import importlib.util
        old = sys.dont_write_bytecode
        sys.dont_write_bytecode = True  # no __pycache__ in the hashed workspace
        try:
            spec = importlib.util.spec_from_file_location("mb_gen", gen_path)
            gen = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = gen
            spec.loader.exec_module(gen)
            loader = (self.w1 / "workspace" / "loader" / "load_extract.py")
            spec2 = importlib.util.spec_from_file_location("mb_loader", loader)
            mod2 = importlib.util.module_from_spec(spec2)
            spec2.loader.exec_module(mod2)
        finally:
            sys.dont_write_bytecode = old
        for cents in (0, 1, 1000, 500000, 999999999):
            self.assertEqual(mod2.unpack_comp3(gen.pack_comp3(cents)), cents)

    def test_every_dbt_view_is_readable_through_the_alias(self):
        """A view's body is STORED, so it can be created and still be
        unreadable. dbt bakes `warehouse.` into any three-part reference it
        renders (it opens the file directly, where the catalog is the file
        stem); every reader must ATTACH under an alias instead, and the baked
        name then fails to bind. Reads every view, not one."""
        from world import executor
        self.assertEqual(executor.unreadable_views(self.w1), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
