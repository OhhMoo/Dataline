"""Live-world acceptance tests: the scheduler executes, and it still reproduces.

Two properties are under test and they pull against each other. The scheduler
must run real work (real dbt, real loaders, real DuckDB) so a run record can
honestly say what happened; and a world built twice from one seed, advanced the
same way, must still fingerprint identically. A live world that is not
reproducible would destroy the project's core contract, so every execution path
here is also a determinism assertion.

Cross-host determinism is the CI version of test_advance_is_deterministic: run
this suite on two hosts and compare the printed hashes.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import build as wbuild, executor, fingerprint, scheduler  # noqa: E402
from world.gateway_scheduler import WorldScheduler  # noqa: E402
from world.hashing import file_hash  # noqa: E402

TRACK_A = Path(__file__).resolve().parents[1]
SEED = TRACK_A / "seed.example.yaml"
LOCK = TRACK_A / "requirements.lock"
MODEL = Path("workspace/dbt_project/models/warehouse/fct_revenue.sql")
STATE = Path("var/sched/state.json")

# A column that does not exist: dbt compiles it, DuckDB's binder rejects it.
BROKEN = (b"select order_date, count(*) as orders, sum(amount) as revenue\n"
          b"from {{ ref('stg_revenue') }}\ngroup by order_dat\norder by order_date\n")
# A valid repair that also changes the model's output, so a successful build
# has something to move.
REPAIRED = (b"select order_date, count(*) as orders,\n"
            b"       count(distinct customer_id) as customers,\n"
            b"       sum(amount) as revenue\n"
            b"from {{ ref('stg_revenue') }}\ngroup by order_date\norder by order_date\n")

ADVANCE_H = 30          # far enough to land dt=2026-03-03 and run its 01:00 load


class LiveWorldTest(unittest.TestCase):
    # The determinism gate compares whole fingerprint dicts. Truncating that
    # diff to unittest's default 640 characters hides WHICH section moved,
    # which is the only thing a failure here needs to tell you.
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="live_"))
        lock = LOCK if LOCK.exists() else None
        cls.w1 = wbuild.build(SEED, cls.tmp / "w1", lock)
        cls.w2 = wbuild.build(SEED, cls.tmp / "w2", lock)
        cls.built_fp = fingerprint.semantic_hash(cls.w1)
        # two independent builds advanced the same way, and one advanced the
        # same distance in 30 steps instead of 1
        cls.a = shutil.copytree(cls.w1, cls.tmp / "a")
        cls.b = shutil.copytree(cls.w2, cls.tmp / "b")
        cls.c = shutil.copytree(cls.w1, cls.tmp / "c")
        cls.res_a = scheduler.advance(cls.a, ADVANCE_H)
        scheduler.advance(cls.b, ADVANCE_H)
        for _ in range(ADVANCE_H):
            scheduler.advance(cls.c, 1)
        print("\nbuilt semantic_hash:   ", cls.built_fp)
        print("advanced semantic_hash:", fingerprint.semantic_hash(cls.a))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def copy(self, name):
        return shutil.copytree(self.w1, self.tmp / name)

    # -- the frozen surface ------------------------------------------------

    def test_agent_visible_state_shape_is_unchanged(self):
        """state.json `jobs` still carries only {command, duration_s}: the
        live-world config lives in var/world/, which no gateway tool reads."""
        jobs = scheduler.load(self.w1)["jobs"]
        for spec in jobs.values():
            self.assertEqual(sorted(spec), ["command", "duration_s"])
        self.assertEqual(file_hash(self.w1 / STATE), file_hash(self.w2 / STATE))

    def test_live_control_plane_is_seeded_outside_the_agent_surface(self):
        live = scheduler.load_live(self.w1)
        self.assertEqual(live["theme"], "revenue_daily")
        self.assertEqual(live["days_landed"], 30)
        self.assertIn("daily_load", live["jobs"])
        self.assertEqual(live["arrival"]["op"], "write_day")
        # only the seed keys the theme declares are carried into the world
        self.assertEqual(sorted(live["params"]),
                         ["amount_cents", "customers", "orders_per_day", "seed"])
        self.assertFalse((self.w1 / "workspace" / "var").exists())

    def test_live_section_is_fingerprinted(self):
        self.assertIn("live", fingerprint.semantic(self.w1))

    # -- determinism -------------------------------------------------------

    def test_advance_is_deterministic(self):
        """THE GATE: two from-scratch builds + the same advance -> same world."""
        self.assertEqual(fingerprint.semantic(self.a), fingerprint.semantic(self.b))
        self.assertEqual(file_hash(self.a / STATE), file_hash(self.b / STATE))

    def test_advance_is_path_independent(self):
        """advance(1) x 30 lands exactly where advance(30) does."""
        self.assertEqual(fingerprint.semantic(self.a), fingerprint.semantic(self.c))

    def test_advance_moves_the_world(self):
        self.assertNotEqual(fingerprint.semantic_hash(self.a), self.built_fp)
        before = fingerprint.semantic(self.w1)["warehouse"]["hash"]
        after = fingerprint.semantic(self.a)["warehouse"]["hash"]
        self.assertNotEqual(before, after)

    def test_advance_lands_data_and_runs_the_schedule(self):
        kinds = [(e["kind"], e["name"]) for e in self.res_a["events"]]
        self.assertEqual(kinds, [("arrival", "daily_revenue"),
                                 ("job", "daily_load"), ("job", "dbt_build")])
        self.assertTrue((self.a / "workspace/data/landing/daily_revenue"
                         / "dt=2026-03-03/revenue.csv").is_file())
        self.assertTrue(all(e.get("status", "success") == "success"
                            for e in self.res_a["events"]))
        self.assertEqual(scheduler.load(self.a)["vnow"], "2026-03-04T06:00:00Z")
        self.assertEqual(scheduler.load_live(self.a)["days_landed"], 31)

    def test_advance_does_not_replay_recorded_history(self):
        """The span between vnow and the last seeded run is already logged;
        advancing over it must not fire those occurrences a second time."""
        w = self.copy("noreplay")
        before = len(scheduler.load(w)["runs"])
        scheduler.advance(w, 2)          # covers the 01:00 / 01:05 seeded pair
        self.assertEqual(len(scheduler.load(w)["runs"]), before)

    # -- the executor tells the truth --------------------------------------

    def test_broken_model_fails_with_the_engine_error(self):
        w = self.copy("broken")
        (w / MODEL).write_bytes(BROKEN)
        run = scheduler.trigger(w, "dbt_build")
        self.assertEqual(run["status"], "failed")
        self.assertTrue(run["executed"])
        self.assertIn("Binder Error", run["error"])
        self.assertIn("order_dat", run["error"])
        # nothing volatile reached the fingerprinted state file
        self.assertNotIn(str(w), run["error"])
        self.assertNotIn("\x1b", run["error"])
        self.assertEqual(scheduler.load(w)["runs"][-1]["status"], "failed")

    def test_repair_succeeds_and_moves_the_warehouse(self):
        w = self.copy("repair")
        (w / MODEL).write_bytes(BROKEN)
        self.assertEqual(scheduler.trigger(w, "dbt_build")["status"], "failed")
        broken_fp = fingerprint.semantic(w)["warehouse"]["hash"]
        (w / MODEL).write_bytes(REPAIRED)
        run = scheduler.trigger(w, "dbt_build")
        self.assertEqual(run["status"], "success")
        self.assertIsNone(run["error"])
        self.assertNotEqual(fingerprint.semantic(w)["warehouse"]["hash"], broken_fp)

    def test_engine_error_text_is_deterministic(self):
        """The normalized error lands in the fingerprinted state file, so two
        independent worlds must produce it byte-for-byte."""
        x, y = self.copy("err_x"), shutil.copytree(self.w2, self.tmp / "err_y")
        for w in (x, y):
            (w / MODEL).write_bytes(BROKEN)
        rx, ry = scheduler.trigger(x, "dbt_build"), scheduler.trigger(y, "dbt_build")
        self.assertEqual(rx, ry)
        self.assertEqual(file_hash(x / STATE), file_hash(y / STATE))

    def test_job_with_no_executable_work_never_reports_success(self):
        """A world with no live-world control plane - a hand-built fixture, for
        instance - must say `unsupported`, loudly, not `success`."""
        w = self.copy("nolive")
        scheduler.live_path(w).unlink()
        run = scheduler.trigger(w, "daily_load")
        self.assertEqual(run["status"], "failed")
        self.assertFalse(run["executed"])
        self.assertIn("no executable work", run["error"])

    def test_advance_without_a_control_plane_moves_only_the_clock(self):
        w = self.copy("nolive_advance")
        scheduler.live_path(w).unlink()
        before = fingerprint.semantic(w)["warehouse"]["hash"]
        res = scheduler.advance(w, 48)
        self.assertFalse(res["live"])
        self.assertEqual(res["events"], [])
        self.assertIn("nothing was executed", res["note"])
        self.assertEqual(fingerprint.semantic(w)["warehouse"]["hash"], before)

    def test_raw_engine_output_is_kept_outside_the_fingerprint(self):
        w = self.copy("rawlog")
        (w / MODEL).write_bytes(BROKEN)
        scheduler.trigger(w, "dbt_build")
        logs = sorted((w / executor.RUN_DIR / "logs").glob("*.log"))
        self.assertTrue(logs)
        self.assertIn("Binder Error", logs[-1].read_text(encoding="utf-8"))
        self.assertTrue(str(executor.RUN_DIR).startswith("build"))

    def test_normalize_output_strips_every_host_varying_thing(self):
        raw = ("\x1b[0m17:03:00  2 of 2 ERROR creating model "
               "[\x1b[31mERROR\x1b[0m in 0.16s]\n"
               "  Runtime Error in model fct (models\\warehouse\\fct.sql)\n"
               f"  at {self.w1}\\workspace\n")
        out = executor.normalize_output(raw, self.w1)
        for volatile in ("\x1b", "17:03:00", "0.16s", "\\", str(self.w1)):
            self.assertNotIn(volatile, out)
        self.assertIn("models/warehouse/fct.sql", out)
        self.assertIn("<world>", out)

    # -- the gateway-shaped surface ---------------------------------------

    def test_gateway_scheduler_keeps_the_frozen_response_shape(self):
        w = self.copy("gw")
        api = WorldScheduler(w)
        self.assertEqual(sorted(j["id"] for j in api.list()["jobs"]),
                         ["daily_load", "dbt_build"])
        state = api.state(None)
        self.assertTrue(all("id" in r for r in state["runs"]))
        self.assertEqual(api.state("run-000001")["id"], "run-000001")
        self.assertIn("runs", api.logs(None))
        (w / MODEL).write_bytes(BROKEN)
        run = api.trigger("dbt_build", "2026-03-03", {})
        for key in ("id", "job_id", "execution_date", "started_at",
                    "finished_at", "status"):
            self.assertIn(key, run)
        self.assertEqual(run["status"], "failed")
        self.assertIn("Binder Error", api.logs(run["id"])["result"]["error"])
        with self.assertRaises(ValueError):
            api.trigger("nope", "2026-03-03", {})
        with self.assertRaises(ValueError):
            api.trigger("dbt_build", "not-a-date", {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
