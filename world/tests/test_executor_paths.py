"""Executor containment when the workspace is reached through a symlink.

The Harbor image links `/world/workspace -> /workspace`, so a world root's
`workspace` entry can be a link. A `sql`/`script`/`scan` job must still find
its own workspace files, and the containment check must still refuse a file
outside the workspace.
"""
import os
import tempfile
import unittest
from pathlib import Path

import duckdb

from world import executor


class SymlinkedWorkspaceTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "world"
        (self.root / executor.DB).parent.mkdir(parents=True)
        duckdb.connect(str(self.root / executor.DB)).close()
        self.workspace = base / "workspace"
        (self.workspace / "pipelines").mkdir(parents=True)
        os.symlink("../workspace", self.root / "workspace")

    def tearDown(self):
        self._tmp.cleanup()

    def test_sql_script_inside_a_linked_workspace_runs(self):
        (self.workspace / "pipelines" / "make.sql").write_text(
            "CREATE TABLE t AS SELECT 1 AS n", encoding="utf-8")
        result = executor.execute(self.root, "make", {
            "kind": "sql", "script": "pipelines/make.sql", "tables": ["t"]})
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(result["effects"]["t"]["rows_after"], 1)

    def test_sql_script_outside_the_workspace_is_refused(self):
        (self.root / "outside.sql").write_text("SELECT 1", encoding="utf-8")
        result = executor.execute(self.root, "escape", {
            "kind": "sql", "script": "../world/outside.sql"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_kind"], "unsupported")


if __name__ == "__main__":
    unittest.main()
