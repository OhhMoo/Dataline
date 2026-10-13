"""excel_close theme acceptance tests (stdlib unittest).

Mirrors test_world.py's build-twice gate for the excel_close theme; the
semantic fingerprint covers the workspace bytes, so it exercises the
ZIP_STORED / fixed-timestamp xlsx determinism.
"""
import random
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import bi, build as wbuild, fingerprint, scheduler  # noqa: E402
from world.themes import load_theme  # noqa: E402

THEME = Path(__file__).resolve().parents[1] / "world" / "themes" / "excel_close"
SEED = THEME / "seed.example.yaml"


class ExcelCloseThemeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="excel_close_"))
        cls.w1 = wbuild.build(SEED, cls.tmp / "w1")
        cls.w2 = wbuild.build(SEED, cls.tmp / "w2")
        print("\nsemantic_hash:", fingerprint.semantic_hash(cls.w1))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _attach(self):
        import duckdb
        # attach under a neutral alias: the file stem is "warehouse", which
        # would collide with the raw/staging/warehouse schemas
        con = duckdb.connect(":memory:")
        con.execute("ATTACH '" + str(self.w1 / "var/warehouse/warehouse.duckdb")
                    .replace("'", "''") + "' AS wh (READ_ONLY)")
        return con

    def test_semantic_fingerprint_stable(self):
        self.assertEqual(fingerprint.semantic(self.w1),
                         fingerprint.semantic(self.w2))

    def test_git_head_stable(self):
        head1, head2 = fingerprint.git_head(self.w1), fingerprint.git_head(self.w2)
        self.assertIsNotNone(head1)
        self.assertEqual(head1, head2)

    def test_scheduler_jobs(self):
        state = scheduler.load(self.w1)
        self.assertEqual(list(state["jobs"]), ["close_import", "dbt_build"])
        self.assertEqual(state["vnow"], "2026-03-03T00:00:00Z")

    def test_bi_render_matches_warehouse(self):
        rendered = bi.render(self.w1)
        panels = rendered["close_summary"]
        con = self._attach()
        total = con.execute(
            "select sum(adj_total) from wh.warehouse.fct_gl_monthly").fetchone()[0]
        con.close()
        adjustments = sum(float(r[1]) for r in panels["adjustments_total"])
        self.assertAlmostEqual(adjustments, float(total))
        self.assertEqual(len(panels["adjustments_total"]), 2)  # 202602 + 202603

    def test_leading_zero_line_id_round_trip(self):
        """GL_LINES text cells (incl. leading zeros) reach raw.gl_lines intact."""
        import yaml
        seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
        gen = load_theme("excel_close").gen
        start = scheduler.parse_vtime(seed["vtime_start"])
        expected = {}
        for i in range(seed["days"]):
            day = (start + timedelta(days=i)).date()
            for row in gen.line_rows(random.Random(f"{seed['seed']}:{i}"),
                                     day, seed):
                expected[row[0]] = row
        leading = sorted(k for k in expected if k.startswith("0"))
        self.assertTrue(leading, "seed must produce leading-zero line_ids")
        con = self._attach()
        rows = con.execute(
            "select line_id, account, post_date, period, amount "
            "from wh.raw.gl_lines").fetchall()
        con.close()
        got = {r[0]: list(r) for r in rows}
        self.assertEqual(expected, got)

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
        """`dbt show --inline "... {{ ref('fct_gl_monthly') }}"`: an inline query
        is a `sql_operation` node, and macros/relations.sql must put the database
        back on for it as it does for a test - two-part `warehouse.fct_gl_monthly`
        against a file whose catalog is also `warehouse` is a Binder Error
        ("Ambiguous reference to catalog or schema"). The count dbt returns is
        the one the warehouse holds, read through the contract alias; afterwards
        `dbt build` still passes and every stored view still reads through the
        alias (no catalog baked into a body)."""
        import json
        import duckdb
        from world import executor
        world = shutil.copytree(self.w1, self.tmp / "dbt_inline")
        con = duckdb.connect(":memory:")
        con.execute("ATTACH '" + str(world / "var/warehouse/warehouse.duckdb")
                    .replace("'", "''") + "' AS wh (READ_ONLY)")
        truth = con.execute("select count(*) from wh.warehouse.fct_gl_monthly").fetchone()[0]
        con.close()
        self.assertGreater(truth, 0)

        rc, output = self._dbt_inline(
            world, "compile", "select 1 from {{ ref('fct_gl_monthly') }}")
        self.assertEqual(rc, 0, output[-3000:])
        self.assertIn('"warehouse"."warehouse"."fct_gl_monthly"', output)

        rc, output = self._dbt_inline(
            world, "show", "select count(*) as n from {{ ref('fct_gl_monthly') }}")
        self.assertEqual(rc, 0, output[-3000:])
        shown = json.loads(output[output.index("{"):output.rindex("}") + 1])
        self.assertEqual(shown["show"][0]["n"], truth)

        rc, output = executor.dbt_invoke(world, "build", out_dir=world / "build" / "b")
        self.assertEqual(rc, 0, output[-3000:])
        self.assertEqual(executor.unreadable_views(world), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
