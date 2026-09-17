"""The publication ledger: the world records what a consumer could see.

Read world/README.md#publication-ledger first. Three claims are under test here and the
suite is built around the fact that they pull against each other:

- the ledger records a publication at the virtual moment it became visible,
  from the world's own execution, not from a declaration;
- it is append-only and deterministic, so two builds advanced the same way
  produce byte-identical receipts;
- it costs a world that declares no publications exactly nothing - no file, no
  fingerprint section, no change to any recorded hash.

No theme declares `publishes`, deliberately: adding one to revenue_daily would
move var/world/live.json, which is inside the semantic fingerprint, and every
recorded world hash with it. So the tests opt a built world in by writing the
declaration into its own live.json - which is also the exact shape a task
author uses, since a task world's live block is written by its fixture builder.
"""
import json
import shutil

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402

from world import build as wbuild, fingerprint, ledger, scheduler  # noqa: E402
from world.hashing import file_hash, json_canon  # noqa: E402

TRACK_A = Path(__file__).resolve().parents[1]
SEED = TRACK_A / "seed.example.yaml"
LOCK = TRACK_A / "requirements.lock"

ADVANCE_H = 30          # lands dt=2026-03-03 and runs its 01:00 load + 01:05 build
FACT = "warehouse.fct_revenue"
PUBLISHES = [{"target": "warehouse.warehouse.fct_revenue", "kind": "table",
              "consumer": "finance_daily"},
             {"target": "docs/data_dictionary.md", "kind": "file"}]


def last_for(receipts, target):
    """The last receipt for a target. The graders' own helper lives in
    tasks/harness/ledger.py; this is the world-side test's copy, because
    world/ never imports tasks/."""
    found = [r for r in receipts if r["target"] == target]
    return found[-1] if found else None


def declare(root: Path, job: str, publishes: list) -> None:
    """Opt a built world's job into publishing. The shape a task author writes."""
    live = scheduler.load_live(root)
    live["jobs"][job]["publishes"] = publishes
    scheduler.save_live(root, live)


class LedgerTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        # The writer lives in LEDGER.patch until that patch lands. Skipping is
        # the honest outcome on an unpatched tree: every assertion below would
        # fail for one reason - the hook is not there - and a wall of red would
        # say less than one line that names the missing piece.
        if not hasattr(scheduler, "_publish"):
            raise unittest.SkipTest(
                "LEDGER.patch is not applied: world/world/scheduler.py carries "
                "no publication hook (_publish)")
        cls.tmp = Path(tempfile.mkdtemp(prefix="ledger_"))
        lock = LOCK if LOCK.exists() else None
        cls.w1 = wbuild.build(SEED, cls.tmp / "w1", lock)
        cls.w2 = wbuild.build(SEED, cls.tmp / "w2", lock)
        # a: the reference run. b: an independent build, same declaration, same
        # advance - the determinism gate. c: advanced in 30 steps of one hour.
        cls.a, cls.b, cls.c = (shutil.copytree(src, cls.tmp / name)
                               for src, name in ((cls.w1, "a"), (cls.w2, "b"),
                                                 (cls.w1, "c")))
        for root in (cls.a, cls.b, cls.c):
            declare(root, "dbt_build", PUBLISHES)
        scheduler.advance(cls.a, ADVANCE_H)
        scheduler.advance(cls.b, ADVANCE_H)
        for _ in range(ADVANCE_H):
            scheduler.advance(cls.c, 1)
        cls.receipts = ledger.load(cls.a)
        print("\nreceipts:", len(cls.receipts),
              "ledger sha256:", file_hash(ledger.path(cls.a)))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def copy(self, name):
        return shutil.copytree(self.w1, self.tmp / name)

    # -- a world that declares nothing pays nothing -------------------------

    def test_a_world_without_publishes_writes_no_ledger(self):
        """The conditional-section rule: no declaration, no file, no cost."""
        plain = self.copy("plain")
        before = fingerprint.semantic_hash(plain)
        scheduler.advance(plain, ADVANCE_H)
        self.assertFalse(ledger.path(plain).exists())
        self.assertEqual(ledger.load(plain), [])
        self.assertNotEqual(before, fingerprint.semantic_hash(plain))  # it did run

    def test_the_ledger_is_not_a_fingerprint_section(self):
        """var/ledger is an OUTPUT of advances, like var/sched - and unlike
        var/obs, which is hashed. Forge a receipt and the world still hashes
        the same: the fingerprint is not the ledger's integrity mechanism, the
        agent's inability to reach the file is."""
        self.assertNotIn("ledger", fingerprint.semantic(self.a))
        before = fingerprint.semantic_hash(self.a)
        with ledger.path(self.a).open("ab") as stream:
            stream.write(b'{"forged":true}\n')
        self.assertEqual(before, fingerprint.semantic_hash(self.a))
        # leave the reference world as the rest of the suite expects it
        lines = ledger.path(self.a).read_bytes().splitlines(keepends=True)
        ledger.path(self.a).write_bytes(b"".join(lines[:-1]))

    # -- the receipt --------------------------------------------------------

    def test_one_receipt_per_publishes_entry_per_occurrence(self):
        runs = [r for r in scheduler.load(self.a)["runs"]
                if r["job"] == "dbt_build" and r.get("trigger") == "schedule"]
        self.assertTrue(runs, "the advance ran no scheduled dbt_build")
        self.assertEqual(len(self.receipts), len(runs) * len(PUBLISHES))
        self.assertEqual([r["seq"] for r in self.receipts],
                         list(range(1, len(self.receipts) + 1)))
        for receipt in self.receipts:
            self.assertEqual(sorted(receipt), sorted(ledger.KEYS))
            self.assertEqual(receipt["job"], "dbt_build")

    def test_vnow_is_the_run_end_and_day_is_the_execution_day(self):
        """A publication is visible when the job that made it finished, and it
        is filed under the day it processed - not the wall date."""
        run = next(r for r in scheduler.load(self.a)["runs"]
                   if r["job"] == "dbt_build" and r.get("trigger") == "schedule")
        first = self.receipts[0]
        self.assertEqual(first["vnow"], run["vts_end"])
        self.assertEqual(first["day"], run["execution_date"])
        self.assertLess(first["vnow"], scheduler.load(self.a)["vnow"])

    def test_consumer_is_carried_through_and_defaults_to_null(self):
        table = last_for(self.receipts, PUBLISHES[0]["target"])
        doc = last_for(self.receipts, PUBLISHES[1]["target"])
        self.assertEqual(table["consumer"], "finance_daily")
        self.assertIsNone(doc["consumer"])

    # -- canonical contents -------------------------------------------------

    def test_table_hash_is_the_fingerprint_rule(self):
        """THE property that makes the ledger comparable to anything else: a
        table receipt carries exactly the hash fingerprint.py computes for that
        table. One canonicalisation rule in this repository, not two."""
        last = last_for(self.receipts, PUBLISHES[0]["target"])
        self.assertEqual(
            last["canonical_hash"],
            fingerprint.semantic(self.a)["warehouse"]["tables"][FACT])

    def test_table_hash_is_contents_not_file_bytes(self):
        """The same logical table in two different files, written in different
        row order, hashes the same - and the files do not. This is the property
        the fingerprint rule exists for, and the reason the ledger reuses it
        rather than hashing the warehouse file."""
        root = self.copy("bytes")
        src = root / "var" / "warehouse" / "warehouse.duckdb"
        dst = root / "var" / "warehouse" / "shadow.duckdb"
        con = duckdb.connect(":memory:", config={"threads": 1})
        con.execute(f"ATTACH '{str(src)}' AS a (READ_ONLY)")
        con.execute(f"ATTACH '{str(dst)}' AS b")
        con.execute("CREATE SCHEMA b.warehouse")
        con.execute("CREATE TABLE b.warehouse.fct_revenue AS "
                    "SELECT * FROM a.warehouse.fct_revenue ORDER BY 1 DESC")
        con.close()
        self.assertNotEqual(file_hash(src), file_hash(dst))
        self.assertEqual(
            ledger.table_contents(root, "warehouse.warehouse.fct_revenue"),
            ledger.table_contents(root, "shadow.warehouse.fct_revenue"))

    def test_row_count_and_sample_are_bounded_and_ordered(self):
        last = last_for(self.receipts, PUBLISHES[0]["target"])
        self.assertIsInstance(last["row_count"], int)
        self.assertGreater(last["row_count"], ledger.SAMPLE_ROWS)
        self.assertEqual(len(last["sample"]), ledger.SAMPLE_ROWS)
        self.assertEqual(last["sample"], sorted(last["sample"]))
        for row in last["sample"]:
            self.assertIsInstance(row, list)

    def test_file_publication_hashes_the_bytes(self):
        doc = last_for(self.receipts, PUBLISHES[1]["target"])
        self.assertEqual(doc["kind"], "file")
        self.assertEqual(doc["canonical_hash"],
                         file_hash(self.a / "workspace" / PUBLISHES[1]["target"]))
        self.assertIsNone(doc["row_count"])
        self.assertIsNone(doc["sample"])

    def test_a_missing_target_is_recorded_not_raised(self):
        """The world moving must never crash because a model did not build.
        ABSENT is a sentinel, so 'nothing was there' cannot be confused with
        'an empty file was there'."""
        root = self.copy("missing")
        declare(root, "dbt_build",
                [{"target": "warehouse.no_such_table", "kind": "table"},
                 {"target": "docs/no_such_file.md", "kind": "file"}])
        scheduler.advance(root, ADVANCE_H)
        receipts = ledger.load(root)
        self.assertTrue(receipts)
        for receipt in receipts:
            self.assertEqual(receipt["canonical_hash"], ledger.ABSENT)
            self.assertIsNone(receipt["row_count"])
            self.assertIsNone(receipt["sample"])
        self.assertNotEqual(ledger.ABSENT,
                            __import__("hashlib").sha256(b"").hexdigest())

    def test_a_target_outside_the_workspace_is_refused(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.file_contents(self.a, "../var/sched/state.json")
        with self.assertRaises(ledger.LedgerError):
            ledger.publishes({"jobs": {"j": {"publishes": [{"target": "x"}]}}},
                             None, "j")

    # -- append-only and deterministic --------------------------------------

    def test_two_builds_advanced_the_same_way_are_byte_identical(self):
        self.assertEqual(file_hash(ledger.path(self.a)),
                         file_hash(ledger.path(self.b)))

    def test_advancing_in_steps_lands_the_same_ledger(self):
        """advance(1) x 30 and advance(30): same receipts, same order, same
        bytes. Path-independence has to hold for the record as well as for
        the world, or the record is not evidence of the world."""
        self.assertEqual(file_hash(ledger.path(self.a)),
                         file_hash(ledger.path(self.c)))

    def test_the_file_only_grows_and_earlier_lines_never_move(self):
        root = self.copy("grow")
        declare(root, "dbt_build", PUBLISHES[:1])
        scheduler.advance(root, ADVANCE_H)
        first = ledger.path(root).read_bytes()
        scheduler.advance(root, 24)
        second = ledger.path(root).read_bytes()
        self.assertTrue(second.startswith(first))
        self.assertGreater(len(second), len(first))
        seqs = [r["seq"] for r in ledger.load(root)]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

    def test_every_line_is_canonical_json(self):
        for line in ledger.path(self.a).read_bytes().splitlines():
            receipt = json.loads(line)
            self.assertEqual(line, json_canon(receipt))

    # -- when a job does NOT publish ----------------------------------------

    def test_a_job_the_scheduler_killed_publishes_nothing(self):
        """The three call sites are the three places the command actually ran.
        A timeout never reached the work, so there is nothing to publish and a
        receipt there would claim a publication the world did not make."""
        root = self.copy("timeout")
        live = scheduler.load_live(root)
        live["jobs"]["daily_load"]["publishes"] = [
            {"target": "raw.daily_revenue", "kind": "table"}]
        for fault in live["faults"]:
            if fault["id"] == "load_timeout":
                fault["enabled"], fault["rate"] = True, 1.0
        scheduler.save_live(root, live)
        scheduler.advance(root, ADVANCE_H)
        runs = [r for r in scheduler.load(root)["runs"]
                if r["job"] == "daily_load" and r.get("trigger") == "schedule"]
        self.assertTrue(runs and all(r["executed"] is False for r in runs),
                        "the fault did not fire; this test proves nothing")
        self.assertEqual(ledger.load(root), [])

    def test_a_manual_trigger_publishes_nothing(self):
        """Stated, not incidental: a trigger runs INSIDE the episode container,
        where var/ledger is shadowed by an empty read-only mount, so a receipt
        written there would be lost at teardown. README.md#publication-ledger carries the
        consequence - a task that wants triggered publications graded must
        advance the clock over them."""
        root = self.copy("triggered")
        declare(root, "dbt_build", PUBLISHES[:1])
        scheduler.trigger(root, "dbt_build", execution_date="2026-03-02")
        self.assertFalse(ledger.path(root).exists())


if __name__ == "__main__":
    unittest.main()
