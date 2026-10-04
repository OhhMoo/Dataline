"""User file formats: the `files` slot's parquet / JSON / JSONL.

A seed may say `files: {formats: [parquet, json, jsonl]}` and the build writes
the files an analyst actually gets handed - an export, an API dump, an event
log - into the workspace, before the git commit. Three things are claimed:

- they are real files in their format (pyarrow and DuckDB read the parquet,
  `json` parses the dump, every JSONL line parses);
- each carries its one planted pathology (world/world/files.py says which,
  in a code comment, because no agent-visible file may);
- they are the same bytes every build. The workspace tree is hashed byte for
  byte, so a parquet writer that stamped anything variable into the footer
  would move the fingerprint of a world nobody changed.

And an unflagged build has none of it.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import yaml

WORLD_DIR = Path(__file__).resolve().parents[1]
SEED = WORLD_DIR / "seed.example.yaml"
MAINFRAME_SEED = WORLD_DIR / "world" / "themes" / "mainframe_batch" / "seed.example.yaml"
LOCK = WORLD_DIR / "requirements.lock"

sys.path.insert(0, str(WORLD_DIR))

from world import build, files, fingerprint  # noqa: E402
from world.hashing import tree_items  # noqa: E402

ALL = {"formats": ["parquet", "json", "jsonl"]}


def _seed_with(tmp: Path, name: str, base: Path = SEED, **extra) -> Path:
    seed = yaml.safe_load(base.read_text(encoding="utf-8"))
    path = tmp / f"{name}.yaml"
    path.write_text(yaml.safe_dump({**seed, **extra}, sort_keys=True), encoding="utf-8")
    return path


class FileFormatsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="files-"))
        seed = _seed_with(cls.tmp, "files", files=ALL)
        cls.a = build.build(seed, cls.tmp / "a", LOCK)
        cls.b = build.build(seed, cls.tmp / "b", LOCK)
        cls.plain = build.build(SEED, cls.tmp / "plain", LOCK)
        cls.data = cls.a / "workspace" / "data"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- presence -----------------------------------------------------------

    def _parts(self):
        parts = sorted((self.data / "exports" / "daily_revenue").glob("*.parquet"))
        self.assertEqual(len(parts), 2, parts)
        return parts

    def test_unflagged_build_writes_no_user_files(self):
        data = self.plain / "workspace" / "data"
        for sub in ("exports", "api", "events"):
            self.assertFalse((data / sub).exists(), sub)

    def test_files_are_committed(self):
        import subprocess
        tracked = subprocess.run(["git", "ls-files", "data/exports", "data/api",
                                  "data/events"], cwd=self.a / "workspace",
                                 capture_output=True, text=True, check=True).stdout.split()
        self.assertTrue(any(t.endswith(".parquet") for t in tracked), tracked)
        self.assertTrue(any(t.endswith(".json") for t in tracked), tracked)
        self.assertTrue(any(t.endswith(".jsonl") for t in tracked), tracked)

    # -- readable -----------------------------------------------------------

    def test_parquet_reads_with_pyarrow_and_duckdb(self):
        con = duckdb.connect()
        total = 0
        for part in self._parts():
            table = pq.read_table(part)
            self.assertGreater(table.num_rows, 0)
            n = con.execute("select count(*) from read_parquet(?)", [str(part)]).fetchone()[0]
            self.assertEqual(n, table.num_rows)
            total += n
        raw = duckdb.connect(str(self.a / "var" / "warehouse" / "warehouse.duckdb"),
                             read_only=True)
        try:
            self.assertEqual(total, raw.execute(
                'select count(*) from "warehouse"."raw"."daily_revenue"').fetchone()[0])
        finally:
            raw.close()

    def test_json_dump_parses(self):
        dumps = sorted((self.data / "api").glob("*.json"))
        self.assertEqual(len(dumps), 1)
        doc = json.loads(dumps[0].read_text(encoding="utf-8"))
        self.assertIsInstance(doc["data"], list)
        self.assertEqual(doc["meta"]["count"], len(doc["data"]))
        first = doc["data"][0]
        self.assertIsInstance(first["attributes"], dict)

    def test_jsonl_lines_parse(self):
        logs = sorted((self.data / "events").glob("*.jsonl"))
        self.assertEqual(len(logs), 1)
        raw = logs[0].read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r", raw)
        for line in raw.decode("ascii").splitlines():
            event = json.loads(line)
            self.assertEqual(set(event), {"event_id", "event_type", "occurred_at", "data"})

    # -- pathologies (one per format) ----------------------------------------

    def test_parquet_column_type_drifts_between_partitions(self):
        first, second = (pq.read_schema(p) for p in self._parts())
        self.assertEqual(first.names, second.names)
        drifted = [n for n in first.names
                   if str(first.field(n).type) != str(second.field(n).type)]
        self.assertEqual(drifted, ["amount"])
        self.assertTrue(str(first.field("amount").type).startswith("decimal"))
        self.assertEqual(str(second.field("amount").type), "double")

    def test_json_optional_nested_field_absent_from_some_records(self):
        doc = json.loads(next((self.data / "api").glob("*.json")).read_text())
        records = doc["data"]
        missing = [r for r in records if "relationships" not in r]
        present = [r for r in records if "relationships" in r]
        self.assertTrue(missing, "no record lacks the optional field")
        self.assertTrue(present)
        self.assertLess(len(missing), len(records) * 0.1, "pathology is not modest")
        self.assertIn("customer", present[0]["relationships"])

    def test_jsonl_has_one_duplicate_and_one_out_of_order_event(self):
        events = [json.loads(line) for line in
                  next((self.data / "events").glob("*.jsonl")).read_text().splitlines()]
        ids = [e["event_id"] for e in events]
        dupes = {i for i in ids if ids.count(i) > 1}
        self.assertEqual(len(dupes), 1)
        backwards = [k for k in range(1, len(events))
                     if events[k]["occurred_at"] < events[k - 1]["occurred_at"]]
        self.assertGreaterEqual(len(backwards), 1)
        self.assertLessEqual(len(backwards), 2, "pathology is not modest")

    # -- determinism ------------------------------------------------------------

    def test_two_builds_are_byte_identical(self):
        def files_of(root):
            data = root / "workspace" / "data"
            return [(sub, rel, blob) for sub in ("exports", "api", "events")
                    for rel, blob in tree_items(data / sub)]
        a, b = files_of(self.a), files_of(self.b)
        self.assertTrue(a)
        self.assertEqual([(s, r) for s, r, _ in a], [(s, r) for s, r, _ in b])
        for (sub, rel, x), (_, _, y) in zip(a, b):
            self.assertEqual(x, y, f"{sub}/{rel} differs between two builds")
        self.assertEqual(fingerprint.semantic_hash(self.a), fingerprint.semantic_hash(self.b))

    def test_files_move_only_the_workspace_section(self):
        flagged, plain = fingerprint.semantic(self.a), fingerprint.semantic(self.plain)
        self.assertEqual(set(flagged), set(plain))
        diff = {k for k in plain if flagged[k] != plain[k]}
        self.assertEqual(diff, {"workspace"})


class FormatSelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="files-select-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_only_listed_formats_are_written(self):
        root = build.build(_seed_with(self.tmp, "json", files={"formats": ["json"]}),
                           self.tmp / "w", LOCK)
        data = root / "workspace" / "data"
        self.assertTrue(list((data / "api").glob("*.json")))
        self.assertFalse((data / "exports").exists())
        self.assertFalse((data / "events").exists())

    def test_unknown_format_is_refused_before_building(self):
        with self.assertRaises(SystemExit) as ctx:
            build.build(_seed_with(self.tmp, "bad", files={"formats": ["json", "xlsx"]}),
                        self.tmp / "w", LOCK)
        self.assertIn("xlsx", str(ctx.exception))
        self.assertFalse((self.tmp / "w" / "workspace").exists())

    def test_malformed_files_key_is_refused(self):
        for bad in ({"formats": []}, {"formats": "json"}, ["json"],
                    {"formats": ["json", "json"]}, {"formats": ["json"], "extra": 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    files.resolve({"files": bad})

    def test_another_theme(self):
        """Theme-agnostic: mainframe_batch's money column is a native
        DECIMAL(9,2) (`claim_amt`) where revenue_daily's is a VARCHAR, and the
        drift lands on it all the same."""
        root = build.build(_seed_with(self.tmp, "mf", base=MAINFRAME_SEED, files=ALL),
                           self.tmp / "mf", LOCK)
        data = root / "workspace" / "data"
        parts = sorted((data / "exports" / "claims_extract").glob("*.parquet"))
        self.assertEqual(len(parts), 2)
        a, b = (pq.read_schema(p) for p in parts)
        self.assertNotEqual(str(a.field("claim_amt").type), str(b.field("claim_amt").type))
        json.loads(next((data / "api").glob("*.json")).read_text())


if __name__ == "__main__":
    unittest.main()
