"""World-factory acceptance tests (stdlib unittest).

Cross-host determinism (same seed, two different machines -> identical
semantic fingerprint) is the CI version of test_semantic_fingerprint_stable:
run this suite on two hosts and compare the printed hashes.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import bi, build as wbuild, fingerprint, scheduler, snapshot  # noqa: E402
from world.hashing import file_hash  # noqa: E402

TRACK_A = Path(__file__).resolve().parents[1]
SEED = TRACK_A / "seed.example.yaml"
LOCK = TRACK_A / "requirements.lock"


class WorldTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="world_"))
        lock = LOCK if LOCK.exists() else None
        cls.w1 = wbuild.build(SEED, cls.tmp / "w1", lock)
        cls.w2 = wbuild.build(SEED, cls.tmp / "w2", lock)
        print("\nsemantic_hash:", fingerprint.semantic_hash(cls.w1))
        print("raw_byte_hash:", fingerprint.report(cls.w1)["raw_byte_hash"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_semantic_fingerprint_stable(self):
        """Build-from-scratch-twice gate (same host here; CI runs cross-host)."""
        self.assertEqual(fingerprint.semantic(self.w1), fingerprint.semantic(self.w2))

    def test_no_ingest_section_without_control_plane(self):
        """revenue_daily seeds no var/ingest, so its fingerprint keeps the
        original four sections and its recorded semantic hash cannot move."""
        sem = fingerprint.semantic(self.w1)
        self.assertEqual(sorted(sem), ["bi", "scheduler", "warehouse", "workspace"])

    def test_git_head_stable(self):
        head1, head2 = fingerprint.git_head(self.w1), fingerprint.git_head(self.w2)
        self.assertIsNotNone(head1)
        self.assertEqual(head1, head2)

    def test_raw_byte_hash_same_host(self):
        """Diagnostic gate: identical host+deps must give identical duckdb bytes."""
        db = Path("var/warehouse/warehouse.duckdb")
        self.assertEqual(file_hash(self.w1 / db), file_hash(self.w2 / db))

    def test_snapshot_restore_byte_identical(self):
        tar = snapshot.save(self.w1, self.tmp / "w1.tar")
        restored = snapshot.restore(tar, self.tmp / "w1r")
        self.assertTrue(snapshot.verify(self.w1, restored))

    def test_snapshot_tar_deterministic(self):
        """Same world contents -> identical tar bytes (host-independent paths)."""
        t1 = snapshot.save(self.w1, self.tmp / "a.tar")
        t2 = snapshot.save(self.w2, self.tmp / "b.tar")
        self.assertEqual(file_hash(t1), file_hash(t2))

    def test_scheduler_deterministic(self):
        a = shutil.copytree(self.w1, self.tmp / "s1")
        b = shutil.copytree(self.w1, self.tmp / "s2")
        ra, rb = scheduler.trigger(a, "daily_load"), scheduler.trigger(b, "daily_load")
        self.assertEqual(ra, rb)
        self.assertEqual(ra["vts_start"], "2026-03-03T00:00:00Z")
        self.assertEqual(ra["vts_end"], "2026-03-03T00:00:45Z")
        state = "var/sched/state.json"
        self.assertEqual((a / state).read_bytes(), (b / state).read_bytes())
        scheduler.advance(a, 6)
        self.assertEqual(scheduler.load(a)["vnow"], "2026-03-03T06:00:45Z")

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
            "select sum(revenue) from warehouse.fct_revenue").fetchone()[0]
        con.close()
        self.assertEqual(rendered["revenue_daily"]["total_revenue"][0][0], str(total))
        self.assertEqual(len(rendered["revenue_daily"]["daily_revenue"]), 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
