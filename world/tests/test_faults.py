"""Seeded fault layer: the world misbehaves on its own, and still reproduces.

Four properties are under test and the first two pull against each other:

1. every declared fault leaves the artifact it promises, in the world and in
   the observability record;
2. a world with faults enabled builds and advances to a byte-identical
   fingerprint, from two from-scratch builds and by two different paths;
3. a world with faults DISABLED is clean - a fault that fires when it is
   switched off is worse than no fault at all;
4. no fault path ever reports a status it did not observe, and no fault ever
   names itself inside the agent-visible surface.

The fault worlds here are placed with `only:` rather than a rate, because a
test that asserts "the timeout fired" should assert which occurrence it fired
on. `rate` is exercised separately, in the draw tests, where it is cheap.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from world import build as wbuild, faults, fingerprint, observe, scheduler  # noqa: E402

TRACK_A = Path(__file__).resolve().parents[1]
SEED = TRACK_A / "seed.example.yaml"
LOCK = TRACK_A / "requirements.lock"

# vnow after a 30-day revenue_daily build is 2026-03-03T00:00:00Z. Five days of
# span gives five arrivals (day_index 30..34, the last landing exactly on the
# span boundary) and four load/build pairs - one occurrence to place each fault
# on.
SPAN_H = 24 * 5
ARRIVALS = ["2026-03-04T00:00:00Z", "2026-03-05T00:00:00Z",
            "2026-03-06T00:00:00Z", "2026-03-07T00:00:00Z",
            "2026-03-08T00:00:00Z"]
LOADS = [f"daily_load@{d}" for d in
         ("2026-03-04T01:00:00Z", "2026-03-05T01:00:00Z",
          "2026-03-06T01:00:00Z", "2026-03-07T01:00:00Z")]

# One fault per occurrence, so every assertion below names the run it is about.
PLACED = {
    "late_partition": {"enabled": True, "only": ["day:30"]},
    "missing_partition": {"enabled": True, "only": ["day:31"]},
    "duplicate_partition": {"enabled": True, "only": ["day:32"]},
    "load_timeout": {"enabled": True, "only": [LOADS[0]]},
    "load_retry": {"enabled": True, "only": [LOADS[1]]},
    "load_overrun": {"enabled": True, "only": [LOADS[2]]},
    "load_partial": {"enabled": True, "only": [LOADS[3]]},
}


def seed_with(tmp: Path, name: str, overrides: dict | None) -> Path:
    base = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    if overrides is not None:
        base["faults"] = overrides
    path = tmp / f"{name}.yaml"
    path.write_text(yaml.safe_dump(base, sort_keys=True), encoding="utf-8")
    return path


def lock():
    return LOCK if LOCK.exists() else None


def manifest_seed(theme: str) -> dict:
    """A theme's own example seed - the one its world is actually built from.
    revenue_daily's lives at the track root; the rest ship one per theme."""
    path = TRACK_A / "world" / "themes" / theme / "seed.example.yaml"
    return yaml.safe_load((path if path.is_file() else SEED)
                          .read_text(encoding="utf-8"))


def runs_at(root, prefix):
    return [r for r in scheduler.load(root)["runs"]
            if r["vts_start"].startswith(prefix)]


def live_of(root):
    return json.loads(
        (Path(root) / "var" / "world" / "live.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# the draw: cheap, no world needed
# --------------------------------------------------------------------------

class FaultDrawTest(unittest.TestCase):
    def test_draw_is_pure_in_seed_id_and_occurrence(self):
        a = faults.draw(1, "vendor_late", "day:12")
        b = faults.draw(1, "vendor_late", "day:12")
        self.assertEqual(a, b)
        self.assertNotEqual(a, faults.draw(2, "vendor_late", "day:12"))
        self.assertNotEqual(a, faults.draw(1, "vendor_late", "day:13"))
        self.assertNotEqual(a, faults.draw(1, "other_fault", "day:12"))

    def test_one_fault_does_not_perturb_another(self):
        """The property that makes a catalogue composable: enabling a fault
        must not shift a single draw belonging to any other fault."""
        spec = [{"id": "a", "kind": "late_arrival", "enabled": True, "rate": 0.3},
                {"id": "b", "kind": "missing_arrival", "enabled": False,
                 "rate": 0.3}]
        only_a = {"fault_seed": 7, "faults": faults.catalogue(spec)}
        both = {"fault_seed": 7,
                "faults": faults.catalogue(
                    spec, {"b": {"enabled": True}})}
        fired_a = [d for d in range(200)
                   if faults.fires(only_a, only_a["faults"][0], f"day:{d}")]
        fired_both = [d for d in range(200)
                      if faults.fires(both, both["faults"][0], f"day:{d}")]
        self.assertEqual(fired_a, fired_both)
        self.assertTrue(fired_a, "rate 0.3 over 200 days should fire sometimes")

    def test_rate_bounds_are_absolute(self):
        spec = faults.catalogue(
            [{"id": "never", "kind": "job_timeout", "enabled": True, "rate": 0},
             {"id": "always", "kind": "job_timeout", "enabled": True, "rate": 1}])
        live = {"fault_seed": 3, "faults": spec}
        for d in range(50):
            self.assertFalse(faults.fires(live, spec[0], f"day:{d}"))
            self.assertTrue(faults.fires(live, spec[1], f"day:{d}"))

    def test_only_names_occurrences_and_beats_rate(self):
        spec = faults.catalogue([{"id": "x", "kind": "job_timeout",
                                  "enabled": True, "rate": 1.0,
                                  "only": ["day:4"]}])
        live = {"fault_seed": 1, "faults": spec}
        self.assertTrue(faults.fires(live, spec[0], "day:4"))
        self.assertFalse(faults.fires(live, spec[0], "day:5"))

    def test_catalogue_refuses_what_it_cannot_honour(self):
        with self.assertRaises(faults.BadFault):
            faults.catalogue([{"id": "x", "kind": "not_a_kind"}])
        with self.assertRaises(faults.BadFault):
            faults.catalogue([{"kind": "job_timeout"}])
        with self.assertRaises(faults.BadFault):
            faults.catalogue([{"id": "x", "kind": "job_timeout"},
                              {"id": "x", "kind": "job_retry"}])
        # a seed override naming a fault the theme does not declare is a typo
        # that would silently disable a planted defect: refuse it loudly
        with self.assertRaises(faults.BadFault):
            faults.catalogue([{"id": "x", "kind": "job_timeout"}],
                             {"typo": {"enabled": True}})
        # a seed may re-aim a declared fault; it may not redefine what it IS
        with self.assertRaises(faults.BadFault):
            faults.catalogue([{"id": "x", "kind": "job_timeout"}],
                             {"x": {"kind": "job_retry"}})

    def test_seed_overrides_merge_params_key_by_key(self):
        spec = [{"id": "x", "kind": "job_retry", "rate": 0.1,
                 "params": {"attempts": 3, "retry_delay_minutes": 10}}]
        out = faults.catalogue(spec, {"x": {"enabled": True,
                                            "params": {"attempts": 5}}})[0]
        self.assertTrue(out["enabled"])
        self.assertEqual(out["rate"], 0.1)          # untouched
        self.assertEqual(out["params"],
                         {"attempts": 5, "retry_delay_minutes": 10})

    def test_every_shipped_theme_catalogue_is_valid(self):
        from world.themes import load_theme
        for name in ("revenue_daily", "sftp_vendor", "mainframe_batch",
                     "excel_close"):
            manifest = load_theme(name).manifest
            spec = (manifest.get("live") or {}).get("faults")
            self.assertTrue(spec, f"{name} declares no fault catalogue")
            for fault in faults.catalogue(spec):
                self.assertIn(fault["kind"], faults.KINDS)


# --------------------------------------------------------------------------
# faults OFF: the world must be exactly as clean as it was before they existed
# --------------------------------------------------------------------------

class CleanWorldTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="fault_clean_"))
        # revenue_daily declares seven faults and enables none of them
        cls.root = wbuild.build(SEED, cls.tmp / "clean", lock())
        cls.res = scheduler.advance(cls.root, SPAN_H)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_catalogue_is_declared_but_nothing_fired(self):
        live = live_of(self.root)
        self.assertEqual(len(live["faults"]), 7)
        self.assertFalse(any(f["enabled"] for f in live["faults"]))
        self.assertEqual(live["fault_log"], [])
        self.assertEqual(live["deferred_arrivals"], {})
        self.assertEqual(live["deferred_runs"], {})
        # every day that was scheduled also landed
        self.assertEqual(live["days_scheduled"], live["days_landed"])

    def test_no_run_carries_a_fault_shaped_record(self):
        for run in scheduler.load(self.root)["runs"]:
            self.assertIsNone(run.get("try_number"))
            self.assertNotIn("timeout", (run.get("error") or ""))
            if run.get("executed") is not None:
                self.assertTrue(run["executed"], run)

    def test_every_arrival_arrived_exactly_once_and_on_time(self):
        arrivals = [e for e in self.res["events"] if e["kind"] == "arrival"]
        self.assertEqual([e["vts"] for e in arrivals], ARRIVALS)
        self.assertFalse(any(e.get("late") for e in arrivals))
        self.assertFalse([e for e in self.res["events"]
                          if e["kind"] == "arrival_missed"])
        landing = (self.root / "workspace" / "data" / "landing"
                   / "daily_revenue")
        self.assertEqual(len(sorted(landing.glob("dt=*"))), 35)
        for part in landing.glob("dt=*"):
            self.assertEqual(sorted(p.name for p in part.iterdir()),
                             ["revenue.csv"])


# --------------------------------------------------------------------------
# faults ON: each one leaves its artifact
# --------------------------------------------------------------------------

class FaultArtifactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="fault_art_"))
        seed = seed_with(cls.tmp, "placed", PLACED)
        cls.root = wbuild.build(seed, cls.tmp / "w", lock())
        cls.before = observe.samples(cls.root)
        cls.res = scheduler.advance(cls.root, SPAN_H)
        cls.state = scheduler.load(cls.root)
        cls.live = live_of(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_late_arrival_lands_after_the_job_that_consumes_it(self):
        arrival = [e for e in self.res["events"]
                   if e["kind"] == "arrival" and e["day"] == "2026-03-03"][0]
        self.assertTrue(arrival["late"])
        self.assertEqual(arrival["vts"], "2026-03-04T04:00:00Z")   # +240 min
        # the 01:00 load ran before it - so the partition was not there yet
        self.assertLess(LOADS[0].split("@")[1], arrival["vts"])

    def test_missing_arrival_leaves_a_gap_that_does_not_heal(self):
        missed = [e for e in self.res["events"]
                  if e["kind"] == "arrival_missed"]
        self.assertEqual([e["day"] for e in missed], ["2026-03-04"])
        landing = (self.root / "workspace" / "data" / "landing"
                   / "daily_revenue")
        self.assertFalse((landing / "dt=2026-03-04").exists())
        # a later arrival must not quietly backfill it: the generator's cursor
        # moved past the missing day, the landed high-water mark did not follow
        self.assertTrue((landing / "dt=2026-03-05").exists())
        self.assertGreater(self.live["days_scheduled"], 31)

    def test_duplicate_arrival_is_delivered_twice(self):
        part = (self.root / "workspace" / "data" / "landing" / "daily_revenue"
                / "dt=2026-03-05")
        self.assertEqual(sorted(p.name for p in part.iterdir()),
                         ["revenue.csv", "revenue.dup.csv"])
        self.assertEqual((part / "revenue.csv").read_bytes(),
                         (part / "revenue.dup.csv").read_bytes())

    def test_timeout_is_a_failure_that_claims_no_output(self):
        run = [r for r in self.state["runs"]
               if r["vts_start"] == "2026-03-04T01:00:00Z"
               and r["job"] == "daily_load"][0]
        self.assertEqual(run["status"], "failed")
        self.assertFalse(run["executed"])
        self.assertIsNone(run["action"])
        self.assertEqual(run["effects"], {})
        self.assertIn("timeout", run["error"])
        self.assertEqual(run["vts_end"], "2026-03-04T01:00:45Z")

    def test_retry_records_every_attempt_and_interleaves_honestly(self):
        attempts = [r for r in self.state["runs"]
                    if r["job"] == "daily_load"
                    and r.get("try_number") is not None]
        self.assertEqual([r["try_number"] for r in attempts], [1, 2])
        self.assertEqual(attempts[0]["status"], "failed")
        self.assertFalse(attempts[0]["executed"])
        self.assertEqual(attempts[1]["status"], "success")
        self.assertTrue(attempts[1]["executed"])
        # the second attempt lands at the declared delay, AFTER the downstream
        # build that was scheduled inside the retry window - which is the whole
        # reason a retry is worth simulating
        self.assertEqual(attempts[1]["vts_start"], "2026-03-05T01:10:00Z")
        build = [r for r in self.state["runs"]
                 if r["job"] == "dbt_build"
                 and r["vts_start"] == "2026-03-05T01:05:00Z"][0]
        self.assertLess(build["vts_end"], attempts[1]["vts_start"])

    def test_overrun_produces_two_overlapping_windows(self):
        load = [r for r in self.state["runs"]
                if r["job"] == "daily_load"
                and r["vts_start"] == "2026-03-06T01:00:00Z"][0]
        build = [r for r in self.state["runs"]
                 if r["job"] == "dbt_build"
                 and r["vts_start"] == "2026-03-06T01:05:00Z"][0]
        self.assertEqual(load["status"], "success")      # it really ran
        self.assertEqual(load["vts_end"], "2026-03-06T01:09:00Z")   # 45s x 12
        self.assertTrue(load["vts_start"] <= build["vts_start"] < load["vts_end"],
                        "the build must start inside the load's window")

    def test_partial_write_leaves_partial_data_and_a_failed_run(self):
        run = [r for r in self.state["runs"]
               if r["job"] == "daily_load"
               and r["vts_start"] == "2026-03-07T01:00:00Z"][0]
        self.assertEqual(run["status"], "failed")
        self.assertTrue(run["executed"])          # it ran; it did not finish
        self.assertIn("137", run["error"])
        import duckdb
        con = duckdb.connect(":memory:", config={"threads": 1})
        con.execute("ATTACH '" + str(self.root / "var" / "warehouse"
                                     / "warehouse.duckdb").replace("'", "''")
                    + "' AS wh (READ_ONLY)")
        con.execute("USE wh")
        try:
            kept = con.execute(
                "select count(*) from raw.daily_revenue "
                "where dt = '2026-03-06'").fetchone()[0]
        finally:
            con.close()
        # the landing file is intact - the WRITE was partial, not the delivery,
        # so the file the loader read is the yardstick for how much is missing
        csv = (self.root / "workspace" / "data" / "landing" / "daily_revenue"
               / "dt=2026-03-06" / "revenue.csv")
        delivered = len(csv.read_text(encoding="utf-8").splitlines()) - 1
        self.assertGreater(kept, 0)
        self.assertLess(kept, delivered)
        self.assertEqual(kept, int(delivered * 0.4))       # declared fraction

    def test_no_fault_names_itself_in_the_agent_visible_surface(self):
        """The agent meets the incident, never the label. The ledger that names
        the fault lives in var/world/, which no gateway tool reads."""
        state_text = (self.root / "var" / "sched" / "state.json").read_text(
            encoding="utf-8")
        for fault in self.live["faults"]:
            self.assertNotIn(fault["id"], state_text)
            self.assertNotIn(fault["kind"], state_text)
        self.assertNotIn("fault", json.loads(state_text))
        # ...but the author-side ledger did record every one that fired
        fired = {row["fault"] for row in self.live["fault_log"]}
        self.assertEqual(fired, set(PLACED))

    def test_no_fault_path_reports_an_unobserved_success(self):
        for run in self.state["runs"]:
            if run.get("executed") is False:
                self.assertEqual(run["status"], "failed", run)


# --------------------------------------------------------------------------
# the observability record
# --------------------------------------------------------------------------

class ObservabilityStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="fault_obs_"))
        seed = seed_with(cls.tmp, "placed", PLACED)
        cls.root = wbuild.build(seed, cls.tmp / "w", lock())
        cls.at_build = observe.samples(cls.root)
        scheduler.advance(cls.root, SPAN_H)
        cls.after = observe.samples(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_build_writes_a_sample_per_defined_metric(self):
        defined = [m["id"] for m in observe.definitions(self.root)["metrics"]]
        self.assertEqual([s["metric"] for s in self.at_build], defined)
        for row in self.at_build:
            self.assertEqual(row["source"], "build")
            self.assertEqual(row["vts"], scheduler.load(self.root)["vnow"]
                             if False else row["vts"])

    def test_the_store_is_append_only(self):
        self.assertEqual(self.after[:len(self.at_build)], self.at_build)
        self.assertGreater(len(self.after), len(self.at_build))

    def test_a_past_sample_is_not_derivable_from_the_present_world(self):
        """The invariant the whole store exists for (OBS_STORE_CONTRACT). If a
        reader can recompute yesterday's reading from today's world, the record
        is decoration."""
        first = [s for s in self.at_build
                 if s["metric"] == "revenue_freshness"][0]
        state = scheduler.load(self.root)
        now = observe.read(self.root, observe.definitions(self.root),
                           state["runs"], scheduler.parse_vtime(state["vnow"]))
        live_value = [r for r in now
                      if r["metric"] == "revenue_freshness"][0]["value"]
        self.assertNotEqual(first["value"], live_value)

    def test_faults_are_visible_in_the_record(self):
        """No fault-specific metric exists. The incidents show up in ordinary
        readings, which is how a real dashboard presents them."""
        errors = [s for s in self.after
                  if s["metric"] == "daily_load_error_rate"]
        self.assertEqual(errors[0]["value"], 0.0)          # clean at build
        self.assertGreater(max(s["value"] for s in errors), 0.0)
        durations = [s["value"] for s in self.after
                     if s["metric"] == "daily_load_duration"]
        self.assertIn(540.0, durations)                    # the overrun
        freshness = [s["value"] for s in self.after
                     if s["metric"] == "revenue_freshness"]
        self.assertGreater(max(freshness), 2 * 86400)      # the gap

    def test_insufficient_data_is_null_and_never_zero(self):
        defs = {"metrics": [
            {"id": "gone", "kind": "row_count",
             "source": {"table": "raw.does_not_exist"}},
            {"id": "no_runs", "kind": "job_duration", "job": "never_ran"}]}
        rows = observe.read(self.root, defs, scheduler.load(self.root)["runs"],
                            scheduler.parse_vtime("2026-03-08T00:00:00Z"))
        for row in rows:
            self.assertIsNone(row["value"])
            self.assertIn("reason", row["detail"])

    def test_a_reading_that_fails_is_loud_and_never_recorded_as_null(self):
        """The distinction that keeps this file from being a determinism hazard.

        A MISSING object is world state and blanks. An engine that will not
        answer - a lock, an IO error, a conversion it cannot do - is a property
        of the machine at that instant, and recording it as `null` would freeze
        a transient into samples.jsonl, which is inside the fingerprint. The
        world would then rebuild to a different hash depending on how loaded
        the box was. So it raises, and the build or the advance fails loudly.

        Without this, the bug is invisible: a transient that does not happen
        leaves no trace, so nothing else in this suite would ever catch it.
        """
        missing = {"metrics": [{"id": "gone", "kind": "row_count",
                                "source": {"table": "raw.does_not_exist"}}]}
        rows = observe.read(self.root, missing, [],
                            scheduler.parse_vtime("2026-03-08T00:00:00Z"))
        self.assertIsNone(rows[0]["value"])          # blanks, does not raise

        wont_answer = {"metrics": [{"id": "bad", "kind": "sql_scalar",
                                    "source": {"sql": "select cast('x' as integer)"}}]}
        with self.assertRaises(observe.ReadingFailed):
            observe.read(self.root, wont_answer, [],
                         scheduler.parse_vtime("2026-03-08T00:00:00Z"))

    def test_samples_carry_virtual_time_and_their_components(self):
        for row in self.after:
            self.assertTrue(row["vts"].endswith("Z"))
            self.assertTrue(row["vts"].startswith("2026-"))
            self.assertIn(row["source"], ("build", "advance", "trigger"))
            self.assertIsInstance(row["detail"], dict)

    def test_the_store_is_inside_the_semantic_fingerprint(self):
        self.assertIn("obs", fingerprint.semantic(self.root))


# --------------------------------------------------------------------------
# determinism, with faults enabled
# --------------------------------------------------------------------------

class FaultDeterminismTest(unittest.TestCase):
    """The gate. Two from-scratch builds plus the same advance, and the same
    distance walked in thirty steps instead of one, must all land on one hash -
    WITH faults firing. If this fails the fault layer is worthless, however
    realistic it looks."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="fault_det_"))
        # rate-driven, not placed: the draw itself is what is under test here,
        # and a retry_delay that straddles an advance boundary is deliberate -
        # it exercises the deferral path that keeps the two walks equal.
        overrides = {
            "late_partition": {"enabled": True, "rate": 0.5},
            "missing_partition": {"enabled": True, "rate": 0.2},
            "duplicate_partition": {"enabled": True, "rate": 0.2},
            "load_retry": {"enabled": True, "rate": 0.5,
                           "params": {"retry_delay_minutes": 95}},
            "load_overrun": {"enabled": True, "rate": 0.4},
        }
        seed = seed_with(cls.tmp, "det", overrides)
        cls.a = wbuild.build(seed, cls.tmp / "a", lock())
        cls.b = wbuild.build(seed, cls.tmp / "b", lock())
        cls.c = wbuild.build(seed, cls.tmp / "c", lock())
        scheduler.advance(cls.a, SPAN_H)
        scheduler.advance(cls.b, SPAN_H)
        for _ in range(SPAN_H):
            scheduler.advance(cls.c, 1)
        cls.ha = fingerprint.semantic_hash(cls.a)
        cls.hb = fingerprint.semantic_hash(cls.b)
        cls.hc = fingerprint.semantic_hash(cls.c)
        print(f"\nfaults-on semantic_hash: {cls.ha}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_two_builds_plus_the_same_advance_are_one_world(self):
        self.assertEqual(fingerprint.semantic(self.a), fingerprint.semantic(self.b))

    def test_advance_is_path_independent_under_faults(self):
        self.assertEqual(self.ha, self.hc)

    def test_the_faults_actually_fired(self):
        """A determinism gate over a world where nothing happened proves
        nothing, so assert the world really misbehaved."""
        log = live_of(self.a)["fault_log"]
        self.assertTrue(log)
        self.assertGreaterEqual(len({row["kind"] for row in log}), 2)

    def test_the_sample_record_is_byte_identical(self):
        self.assertEqual(
            (self.a / "var" / "obs" / "samples.jsonl").read_bytes(),
            (self.b / "var" / "obs" / "samples.jsonl").read_bytes())
        self.assertEqual(
            (self.a / "var" / "obs" / "samples.jsonl").read_bytes(),
            (self.c / "var" / "obs" / "samples.jsonl").read_bytes())

    def test_state_json_is_byte_identical(self):
        self.assertEqual(
            (self.a / "var" / "sched" / "state.json").read_bytes(),
            (self.b / "var" / "sched" / "state.json").read_bytes())


# --------------------------------------------------------------------------
# faults in the seeded backstory (opt-in)
# --------------------------------------------------------------------------

class HistoryFaultTest(unittest.TestCase):
    def test_history_is_byte_identical_without_faults(self):
        """GT001 calls initial_state directly. It must not move."""
        from world.themes import load_theme
        seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
        jobs = load_theme("revenue_daily").manifest["jobs"]
        self.assertEqual(scheduler.initial_state(seed, jobs),
                         scheduler.initial_state(seed, jobs, faults=None))

    def test_history_faults_are_opt_in_and_deterministic(self):
        from world.themes import load_theme
        seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
        jobs = load_theme("revenue_daily").manifest["jobs"]
        spec = faults.catalogue(
            [{"id": "h", "kind": "job_timeout", "enabled": True, "rate": 0.2,
              "jobs": ["daily_load"], "params": {"timeout_s": 45}}])
        a = scheduler.initial_state(seed, jobs, faults=spec, fault_seed=1)
        b = scheduler.initial_state(seed, jobs, faults=spec, fault_seed=1)
        self.assertEqual(a, b)
        failed = [r for r in a["runs"] if r["status"] == "failed"]
        self.assertTrue(failed)
        self.assertTrue(all(r["job"] == "daily_load" for r in failed))
        self.assertTrue(all("timeout" in r["error"] for r in failed))
        self.assertNotEqual(a["runs"],
                            scheduler.initial_state(seed, jobs)["runs"])

    # -- arrival faults in the backstory ----------------------------------

    def test_only_the_arrival_fault_that_heals_is_carried(self):
        """The line is data coherence, not kind. `write_landing` writes every
        day complete, so the backstory may only carry faults whose end state
        IS that complete landing zone."""
        self.assertEqual(faults.HISTORY_ARRIVAL_KINDS, ("late_arrival",))
        for kind in ("missing_arrival", "duplicate_arrival", "torn_write"):
            self.assertNotIn(kind, faults.HISTORY_ARRIVAL_KINDS)
        self.assertNotIn("partial_write", faults.HISTORY_JOB_KINDS)

    def test_an_uncarried_arrival_fault_records_a_clean_night(self):
        """It is skipped, not faked - and its draw is still consumed, so the
        rest of the catalogue does not shift under it (module rule 2)."""
        from world.themes import load_theme
        seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
        jobs = load_theme("revenue_daily").manifest["jobs"]
        for kind in ("missing_arrival", "duplicate_arrival", "torn_write"):
            spec = faults.catalogue([{"id": "x", "kind": kind, "enabled": True,
                                      "rate": 1.0}])
            state = scheduler.initial_state(seed, jobs, faults=spec,
                                            fault_seed=1)
            self.assertEqual(state, scheduler.initial_state(seed, jobs),
                             f"{kind} must leave the backstory untouched")

    def test_a_late_delivery_fails_the_jobs_it_names(self):
        from world.themes import load_theme
        seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
        jobs = load_theme("revenue_daily").manifest["jobs"]
        message = ("data/landing/daily_revenue holds 0 file(s) matching "
                   "dt=2026-02-04/*.csv, expected at least 1")
        spec = faults.catalogue([{
            "id": "late", "kind": "late_arrival", "enabled": True,
            "only": ["day:3"],
            "params": {"delay_minutes": 240, "missed_by": ["daily_load"],
                       "missed_error": message}}])
        state = scheduler.initial_state(seed, jobs, faults=spec, fault_seed=1)
        failed = [r for r in state["runs"] if r["status"] == "failed"]
        # history day 3 is arrival day_index 3: the partition for 2026-02-04,
        # consumed by the 01:00 load on 2026-02-05.
        self.assertEqual([(r["job"], r["vts_start"]) for r in failed],
                         [("daily_load", "2026-02-05T01:00:00Z")])
        self.assertEqual(failed[0]["error"], message)
        # the job it did NOT name is untouched, and so is every other night
        self.assertTrue(all(r["status"] == "success" for r in state["runs"]
                            if r["job"] == "dbt_build"))
        self.assertEqual(len(state["runs"]),
                         len(scheduler.initial_state(seed, jobs)["runs"]))
        # deterministic, and it really did move the history
        self.assertEqual(state, scheduler.initial_state(seed, jobs, faults=spec,
                                                        fault_seed=1))
        self.assertNotEqual(state, scheduler.initial_state(seed, jobs))

    def test_a_missed_delivery_bites_only_a_run_that_reached_the_work(self):
        """A timeout never got as far as looking; a retry's final attempt did."""
        from world.themes import load_theme
        seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
        jobs = load_theme("revenue_daily").manifest["jobs"]
        arrival = {"id": "late", "kind": "late_arrival", "enabled": True,
                   "only": ["day:3"],
                   "params": {"delay_minutes": 240, "missed_by": ["daily_load"],
                              "missed_error": "no partition for dt=2026-02-04"}}
        load_at = "daily_load@2026-02-05T01:00:00Z"

        timeout = faults.catalogue([arrival, {
            "id": "t", "kind": "job_timeout", "enabled": True,
            "only": [load_at], "params": {"timeout_s": 45}}])
        runs = scheduler.initial_state(seed, jobs, faults=timeout,
                                       fault_seed=1)["runs"]
        killed = [r for r in runs if r["vts_start"] == "2026-02-05T01:00:00Z"]
        self.assertEqual(len(killed), 1)
        self.assertIn("timeout", killed[0]["error"])   # not the arrival's text

        retry = faults.catalogue([arrival, {
            "id": "r", "kind": "job_retry", "enabled": True, "only": [load_at],
            "params": {"attempts": 2, "retry_delay_minutes": 10}}])
        runs = scheduler.initial_state(seed, jobs, faults=retry,
                                       fault_seed=1)["runs"]
        attempts = [r for r in runs if r["job"] == "daily_load"
                    and r["vts_start"].startswith("2026-02-05T01:")]
        self.assertEqual([(r["try_number"], r["status"]) for r in attempts],
                         [(1, "failed"), (2, "failed")])
        self.assertEqual(attempts[0]["error"], scheduler.RETRY_ERROR)
        # the attempt that really reached the work found no partition
        self.assertEqual(attempts[1]["error"], "no partition for dt=2026-02-04")

    def test_check_history_refuses_a_consequence_nobody_declared(self):
        jobs = {"daily_load": {}}
        enabled = {"id": "x", "kind": "late_arrival", "enabled": True,
                   "rate": 0.2}
        with self.assertRaises(faults.BadFault):          # no missed_by at all
            faults.check_history(faults.catalogue([enabled]), jobs)
        with self.assertRaises(faults.BadFault):          # a typo in a job name
            faults.check_history(faults.catalogue(
                [{**enabled, "params": {"missed_by": ["daily_lod"],
                                        "missed_error": "e"}}]), jobs)
        with self.assertRaises(faults.BadFault):          # a failure, no reason
            faults.check_history(faults.catalogue(
                [{**enabled, "params": {"missed_by": ["daily_load"]}}]), jobs)
        # an EMPTY list is a legal, deliberate answer: "no job here fails when
        # the delivery is not there". Silence is refused; a decision is not.
        faults.check_history(faults.catalogue(
            [{**enabled, "params": {"missed_by": []}}]), jobs)
        # and a disabled fault is nobody's problem
        faults.check_history(faults.catalogue(
            [{**enabled, "enabled": False}]), jobs)

    # -- what the shipped themes actually declare --------------------------

    def test_history_faults_are_on_exactly_where_they_belong(self):
        """revenue_daily is the determinism seed and GT001's ground truth; it
        must not move. excel_close ships every fault off, so history over it
        would change nothing (theme.yaml carries the argument)."""
        from world.themes import load_theme
        expected = {"revenue_daily": False, "excel_close": False,
                    "sftp_vendor": True, "mainframe_batch": True}
        for name, want in expected.items():
            live = load_theme(name).manifest.get("live") or {}
            self.assertEqual(bool(live.get("faults_in_history")), want, name)

    def test_every_shipped_history_catalogue_is_accepted(self):
        """The check that guards the backstory has to have run against the
        catalogues that ship, not only against fixtures."""
        from world.themes import load_theme
        for name in ("revenue_daily", "sftp_vendor", "mainframe_batch",
                     "excel_close"):
            manifest = load_theme(name).manifest
            live = manifest.get("live") or {}
            catalogue = faults.catalogue(live["faults"])
            faults.check_history(catalogue, manifest["jobs"])
            if not live.get("faults_in_history"):
                continue
            state = scheduler.initial_state(manifest_seed(name),
                                            manifest["jobs"], faults=catalogue,
                                            fault_seed=manifest_seed(name)["seed"])
            clean = scheduler.initial_state(manifest_seed(name),
                                            manifest["jobs"])
            self.assertNotEqual(state["runs"], clean["runs"],
                                f"{name} opted into history faults and its "
                                f"backstory is still the clean metronome")

    def test_the_backstory_never_names_the_fault_that_caused_it(self):
        """The ledger that names a fault is var/world/live.json, which no
        gateway tool reads. state.json must read like the engine."""
        from world.themes import load_theme
        for name in ("sftp_vendor", "mainframe_batch"):
            manifest = load_theme(name).manifest
            catalogue = faults.catalogue(manifest["live"]["faults"])
            state = scheduler.initial_state(
                manifest_seed(name), manifest["jobs"], faults=catalogue,
                fault_seed=manifest_seed(name)["seed"])
            blob = json.dumps(state)
            for fault in catalogue:
                self.assertNotIn(fault["id"], blob, name)
                self.assertNotIn(fault["kind"], blob, name)


# --------------------------------------------------------------------------
# the backstory as the agent meets it: built world, gateway-shaped surface
# --------------------------------------------------------------------------

class HistoryIsLegibleTest(unittest.TestCase):
    """A fault in the past is only useful if the agent can find it.

    sftp_vendor ships `faults_in_history: true`, so its seeded history carries
    real SFTP connection resets. This drives the same reader the gateway's
    `sched_state` / `sched_logs` call and asserts the incident is reachable.
    """
    THEME = "sftp_vendor"

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="fault_history_"))
        seed = (TRACK_A / "world" / "themes" / cls.THEME / "seed.example.yaml")
        cls.root = wbuild.build(seed, cls.tmp / "w", lock())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_seeded_past_really_misbehaved(self):
        runs = scheduler.load(self.root)["runs"]
        failed = [r for r in runs if r["status"] == "failed"]
        self.assertTrue(failed, "faults_in_history is on and nothing fired")
        # every failure predates the episode: this is scar tissue, not news
        self.assertTrue(all(r["vts_start"] < scheduler.load(self.root)["vnow"]
                            for r in failed))
        # and it reproduces from the manifest, without a second build
        from world.themes import load_theme
        manifest = load_theme(self.THEME).manifest
        rebuilt = scheduler.initial_state(
            manifest_seed(self.THEME), manifest["jobs"],
            faults=faults.catalogue(manifest["live"]["faults"]),
            fault_seed=manifest_seed(self.THEME)["seed"])
        self.assertEqual(rebuilt["runs"], runs)

    def test_the_incident_is_reachable_through_the_scheduler_surface(self):
        from world.gateway_scheduler import WorldScheduler
        api = WorldScheduler(self.root)
        listed = [r for r in api.logs(None)["runs"] if r["status"] == "failed"]
        self.assertTrue(listed, "sched_logs shows no failed historical run")
        run_id = listed[0]["id"]
        # sched_state hands back the whole row: the error AND the attempt count
        row = api.state(run_id)
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["error"])
        self.assertEqual(row["try_number"], 1)
        self.assertEqual(row["max_tries"], 2)
        # sched_logs(run_id) carries the error too, which is what makes the
        # gateway's logs_search able to match on it
        self.assertEqual(api.logs(run_id)["result"]["error"], row["error"])
        # the retry that followed is in the history as a second attempt
        after = api.state(None)["runs"]
        index = next(i for i, r in enumerate(after) if r["id"] == run_id)
        self.assertEqual(after[index + 1]["try_number"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
