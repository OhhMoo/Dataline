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

    def test_conditional_sections_track_what_the_theme_actually_seeds(self):
        """A conditional section appears only when the world seeds the surface.

        revenue_daily has no connector control plane, so there is no `ingest`
        section; it does seed a live-world control plane (R003) and an
        observability store (R003 FAULTS, per OBS_STORE_CONTRACT.md), so there
        are `live` and `obs` ones. R004 WORKFLOW seeds the human-context
        surface (issues/chat/kb), so there is a `workflow` one. A theme that
        seeds none keeps the original four.
        """
        sem = fingerprint.semantic(self.w1)
        self.assertEqual(sorted(sem),
                         ["bi", "live", "obs", "scheduler", "warehouse",
                          "workflow", "workspace"])
        self.assertNotIn("ingest", sem)
        # and the obs section really covers the appended record, not only the
        # definitions: a COPY that has advanced must hash differently. (A copy,
        # because w1 is shared with the raw-byte gate below and advancing it
        # would move the duckdb file under that test's feet.)
        moved = shutil.copytree(self.w1, self.tmp / "obs_moved")
        scheduler.advance(moved, 30)
        self.assertNotEqual(sem["obs"], fingerprint.semantic(moved)["obs"])

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
