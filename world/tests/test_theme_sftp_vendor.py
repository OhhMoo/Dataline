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


if __name__ == "__main__":
    unittest.main(verbosity=2)
