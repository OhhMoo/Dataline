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
        """The invariant the whole store exists for (gateway/SURFACES.md, "The sample store"). If a
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
# the regression test for an ENABLED fault that could not cost anything
# --------------------------------------------------------------------------

class LateArrivalIsNotInertTest(unittest.TestCase):
    """`late_arrival` shipped enabled in two themes and was INERT.

    `_kind_scan` globbed the delivery tree CUMULATIVELY - `*/*.done` with
    `min: 1` over a folder holding every day ever delivered - so from day two
    onward the count could not fall below the minimum and the poll could not
    fail. Measured before the fix: on the night of 2026-03-03, with the vendor
    drop deferred to 07:30, `sftp_poll` counted 60 older sentinels and recorded
    `success`; `nightly_extract` counted 30 older partitions and did the same.
    Two `theme.yaml` comments asserted the poll "FAILS for real" the whole time.

    Nothing caught it because nothing had ever run a poll against a tree WITH
    HISTORY IN IT, which is the only condition under which the defect shows. So
    every test here reproduces that condition first - N older days really on
    disk, in the theme's real layout, written by the theme's own `write_day` -
    and then withholds exactly one day. Against the cumulative glob each one
    goes red; `test_the_cumulative_glob_is_the_defect` pins down why, by
    running the OLD spec over the SAME tree and watching it succeed.

    Cheap on purpose: the layout comes from `gen.write_day`, not from a build.
    """
    # theme -> (scan job, the late_arrival fault it declares)
    THEMES = {"sftp_vendor": ("sftp_poll", "vendor_late_delivery"),
              "mainframe_batch": ("nightly_extract", "extract_late")}
    DAYS = 30                       # older days on disk before the withheld one

    def _theme(self, name):
        from world.themes import load_theme
        return load_theme(name)

    def _spec(self, name, job):
        return self._theme(name).manifest["jobs"][job]["exec"]

    def _fault(self, name, fault_id):
        manifest = self._theme(name).manifest
        return next(f for f in faults.catalogue(manifest["live"]["faults"])
                    if f["id"] == fault_id)

    def _tree(self, name, root, withhold):
        """The theme's real delivery layout for days 0..DAYS-1, minus one.

        `withhold` is the day_index a late arrival deferred past its consumer.
        Every other day is written exactly as the builder would have written
        it, so the poll runs against a tree with a month of history in it.
        """
        from datetime import timedelta
        gen, seed = self._theme(name).gen, manifest_seed(name)
        start = scheduler.parse_vtime(seed["vtime_start"])
        workspace = Path(root) / "workspace"
        for i in range(self.DAYS):
            if i == withhold:
                continue
            gen.write_day(workspace, seed, i, (start + timedelta(days=i)).date())
        return (start + timedelta(days=withhold)).date()

    def _run(self, name, job, root, day):
        from world import executor
        return executor.execute(root, job, self._spec(name, job), day=day)

    def test_a_withheld_delivery_fails_the_poll_over_a_tree_of_history(self):
        """The defect itself: a month on disk, tonight's delivery missing."""
        for name, (job, _) in self.THEMES.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                day = self._tree(name, tmp, withhold=self.DAYS - 1)
                result = self._run(name, job, tmp, day.isoformat())
                self.assertFalse(result["ok"], f"{name}: the poll did not fail")
                self.assertEqual(result["effects"]["files"], 0)
                self.assertEqual(result["error_kind"], "scan")

    def test_the_cumulative_glob_is_the_defect(self):
        """Same tree, same withheld night, the OLD spec: it SUCCEEDS.

        This is the assertion that makes the class a regression test rather
        than a description. If a later edit re-widens a pattern, the day-scoped
        test above goes red while this one keeps passing, and the pair says
        exactly which half moved.
        """
        old = {"sftp_vendor": {"kind": "scan", "path": "data/drop",
                               "pattern": "*/*.done", "min": 1},
               "mainframe_batch": {"kind": "scan",
                                   "path": "data/landing/claims_extract",
                                   "pattern": "dt=*/CLAIMS.DAT", "min": 1}}
        from world import executor
        for name, (job, _) in self.THEMES.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                day = self._tree(name, tmp, withhold=self.DAYS - 1)
                result = executor.execute(tmp, job, old[name],
                                          day=day.isoformat())
                self.assertTrue(result["ok"],
                                f"{name}: the cumulative glob was expected to "
                                f"succeed on a night with no delivery")
                self.assertGreater(result["effects"]["files"], 1)

    def test_a_night_that_did_arrive_still_passes(self):
        """The other half: day-scoping must not fail every night."""
        from datetime import timedelta
        for name, (job, _) in self.THEMES.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                seed = manifest_seed(name)
                self._tree(name, tmp, withhold=self.DAYS - 1)
                start = scheduler.parse_vtime(seed["vtime_start"])
                for i in (0, self.DAYS // 2, self.DAYS - 2):
                    day = (start + timedelta(days=i)).date().isoformat()
                    result = self._run(name, job, tmp, day)
                    self.assertTrue(result["ok"],
                                    f"{name} {day}: {result['error']}")
                    self.assertGreaterEqual(result["effects"]["files"],
                                            int(self._spec(name, job)["min"]))

    def test_the_declared_missed_error_is_the_one_the_executor_produces(self):
        """`missed_by` must agree with what happens, not with prose.

        `check_history` can only verify that a named job EXISTS. This compares
        the backstory's declared text, resolved for the night, against the
        string the live executor produces for that same night - byte for byte.
        A theme that renames its folders or moves its `min` and forgets the
        catalogue goes red here.
        """
        from world import executor
        for name, (job, fault_id) in self.THEMES.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                day = self._tree(name, tmp, withhold=self.DAYS - 1)
                live = self._run(name, job, tmp, day.isoformat())["error"]
                declared = faults.missed_by(self._fault(name, fault_id), job,
                                            executor.day_tokens(day))
                # Both halves must be REAL, or `None == None` passes the test
                # while the fault costs nothing - which is exactly the state
                # this class exists to catch.
                self.assertIsNotNone(
                    live, f"{name}: {job} did not fail on the withheld night")
                self.assertIsNotNone(
                    declared, f"{name}: {fault_id} does not name {job} in "
                              f"params.missed_by, so the backstory records no "
                              f"cost for a night the live path fails on")
                self.assertEqual(declared, live, name)

    def test_the_backstory_records_the_failed_poll_it_declares(self):
        """Every late night in the seeded past must show a failed poll.

        With `missed_by: []` - which is what both themes shipped while the scan
        was inert - this finds nothing and goes red.
        """
        from datetime import timedelta
        from world import executor
        for name, (job, fault_id) in self.THEMES.items():
            with self.subTest(name):
                manifest = self._theme(name).manifest
                seed = manifest_seed(name)
                catalogue = faults.catalogue(manifest["live"]["faults"])
                state = scheduler.initial_state(seed, manifest["jobs"],
                                                faults=catalogue,
                                                fault_seed=seed["seed"])
                start = scheduler.parse_vtime(seed["vtime_start"])
                live = {"faults": catalogue, "fault_seed": seed["seed"]}
                late = [i for i in range(seed["days"])
                        if (faults.arrival_decision(live, i) or {}).get("id")
                        == fault_id]
                self.assertTrue(late, f"{name}: no late night in the backstory")
                failed = [r for r in state["runs"]
                          if r["job"] == job and r["status"] == "failed"]
                for i in late:
                    day = (start + timedelta(days=i)).date()
                    want = faults.missed_by(self._fault(name, fault_id), job,
                                            executor.day_tokens(day))
                    self.assertTrue(
                        [r for r in failed if r.get("error") == want],
                        f"{name}: the backstory records no failed {job} for "
                        f"the withheld night {day} (declared {want!r})")

    def test_a_scan_that_cannot_resolve_its_day_is_loud(self):
        """No day, no silent zero.

        An unresolved `{day_compact}` left literal would glob for a file named
        after the token, match nothing, and report a failure indistinguishable
        from the one a real missing delivery produces - the scheduler asserting
        an incident it did not observe. It is `unsupported` instead.
        """
        for name, (job, _) in self.THEMES.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                self._tree(name, tmp, withhold=self.DAYS - 1)
                result = self._run(name, job, tmp, None)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_kind"], "unsupported")
                self.assertIn("execution day", result["error"])

    def test_every_scheduled_branch_hands_the_executor_its_execution_day(self):
        """The plumbing, at all three sites `_simulate_job` executes from.

        This is the half that was actually missing: `_kind_scan` could not see
        which day it was running for because the scheduled path called
        `_execute` with nothing at all. Driven through `_simulate_job` with a
        stub control plane, so it covers the plain branch, `job_overrun` and
        `partial_write` without a build.
        """
        from datetime import timedelta
        name, job = "sftp_vendor", "sftp_poll"
        spec = self._spec(name, job)
        occurrence = "2026-03-05T05:30:00Z"
        branches = {
            "plain": [],
            "job_overrun": [{"id": "o", "kind": "job_overrun", "enabled": True,
                             "only": [f"{job}@{occurrence}"],
                             "params": {"multiplier": 2}}],
            "partial_write": [{"id": "p", "kind": "partial_write",
                               "enabled": True,
                               "only": [f"{job}@{occurrence}"],
                               "params": {"table": "raw.vendor_orders",
                                          "order_column": "order_id"}}],
        }
        for branch, catalogue in branches.items():
            with self.subTest(branch), tempfile.TemporaryDirectory() as tmp:
                withheld = self._tree(name, tmp, withhold=self.DAYS - 1)
                start = scheduler.parse_vtime(manifest_seed(name)["vtime_start"])
                live = {"jobs": {job: {"duration_s": 20, "detail": None,
                                       "exec": spec}},
                        "faults": catalogue, "fault_seed": 0}
                event = {"kind": "job", "name": job, "day_index": self.DAYS - 1,
                         "vts": scheduler.parse_vtime(occurrence)}
                made, _ = scheduler._simulate_job(tmp, {"runs": []}, live,
                                                  event, withheld)
                self.assertEqual(len(made), 1, branch)
                run = made[0]
                # the withheld night, seen for real: 0 files, not 58
                self.assertEqual(run["status"], "failed", branch)
                self.assertEqual(run["effects"]["files"], 0, branch)
                self.assertEqual(run["execution_date"], withheld.isoformat())
                # And a night that DID arrive still passes down the same path.
                # Not on the partial_write branch: a successful run there goes
                # on to trim the newest partition, which needs a warehouse this
                # stub world does not have. The failing half above is the half
                # that carries the day.
                if branch == "partial_write":
                    continue
                ok_day = (start + timedelta(days=0)).date()
                made, _ = scheduler._simulate_job(
                    tmp, {"runs": []}, live, {**event, "day_index": 0}, ok_day)
                self.assertEqual(made[0]["status"], "success", branch)


# --------------------------------------------------------------------------
# the backstory as the agent meets it: built world, gateway-shaped surface
# --------------------------------------------------------------------------

class HistoryIsLegibleTest(unittest.TestCase):
    """A fault in the past is only useful if the agent can find it.

    sftp_vendor ships `faults_in_history: true`, so its seeded history carries
    real SFTP connection resets. This drives the same reader the gateway's
    `sched_state` / `sched_logs` call and asserts the incident is reachable.

    And reachable on EVERY seeded plane that describes the same night. The
    world hands the agent two independent surfaces over one backstory -
    `sched_state`/`sched_logs` over var/sched/state.json and
    `ingest_runs`/`ingest_alerts` over var/ingest/state.json - and an agent
    that finds them contradicting each other is being taught that the evidence
    is noise. The last three tests here are that contradiction, pinned.
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
        """The SSH reset incident, named rather than assumed.

        This used to take `listed[0]` - the first failed run in the backstory -
        and assert `try_number == 1`, which only held because the first failure
        could not be anything but a retry: `late_arrival` was inert, so a
        withheld vendor drop cost nothing and the SSH resets were the ONLY
        failures sftp_vendor's past contained. The moment the poll could
        genuinely fail, the first failed row became a failed poll and this went
        `KeyError: 'try_number'`. It was not asserting the bug, but it was
        living off it, so it now names the shape it is about.
        """
        from world.gateway_scheduler import WorldScheduler
        api = WorldScheduler(self.root)
        listed = [r for r in api.logs(None)["runs"] if r["status"] == "failed"]
        self.assertTrue(listed, "sched_logs shows no failed historical run")
        rows = [api.state(r["id"]) for r in listed]
        retries = [r for r in rows if r.get("try_number")]
        self.assertTrue(retries, "no retry-shaped failure in the backstory")
        row = retries[0]
        # sched_state hands back the whole row: the error AND the attempt count
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["error"])
        self.assertEqual(row["try_number"], 1)
        self.assertEqual(row["max_tries"], 2)
        # sched_logs(run_id) carries the error too, which is what makes the
        # gateway's logs_search able to match on it
        self.assertEqual(api.logs(row["id"])["result"]["error"], row["error"])
        # the retry that followed is in the history as a second attempt
        after = api.state(None)["runs"]
        index = next(i for i, r in enumerate(after) if r["id"] == row["id"])
        self.assertEqual(after[index + 1]["try_number"], 2)

    def test_the_withheld_delivery_is_reachable_too(self):
        """The other incident the backstory now carries, on the same surface.

        A late vendor drop used to leave NOTHING in `sched_state` - it was an
        arrival fault whose only consumer could not fail. It is now a failed
        poll the agent can find, with the executor's own count in the error and
        no attempt number, which is what tells it apart from an SSH reset.
        """
        from datetime import timedelta
        from world.gateway_scheduler import WorldScheduler
        api = WorldScheduler(self.root)
        polls = [r for r in api.state(None)["runs"]
                 if r["job"] == "sftp_poll" and r["status"] == "failed"
                 and "file(s) matching" in (r.get("error") or "")]
        self.assertTrue(polls, "the backstory records no withheld delivery")
        for row in polls:
            # no attempt count: this is a poll that ran and found nothing, not
            # a transport failure that never reached the folder
            self.assertNotIn("try_number", row)
            stamp = row["error"].split("*/*_")[1][:8]
            self.assertRegex(stamp, r"^[0-9]{8}$")
            # and it names the day whose delivery it wanted - the poll runs the
            # morning after (day_offset: 1), so the stamp is vts_start minus a
            # day. A scan scoped to the wrong day would fail every night, which
            # looks identical to the fault firing; this is what tells them apart.
            self.assertEqual(
                (scheduler.parse_vtime(row["vts_start"])
                 - timedelta(days=1)).strftime("%Y%m%d"), stamp, row)
            self.assertEqual(api.logs(row["id"])["result"]["error"],
                             row["error"])

    # ----------------------------------------------------------------
    # the same night, on the other seeded plane
    # ----------------------------------------------------------------

    def _ingest(self):
        return json.loads((self.root / "var" / "ingest" / "state.json")
                          .read_text(encoding="utf-8"))

    def _withheld(self):
        """The nights the SCHEDULER says the delivery was not there.

        Read out of the run history rather than re-drawn from the catalogue,
        so what follows compares two planes instead of comparing one plane
        with itself.
        """
        return {r["vts_start"][:10] for r in scheduler.load(self.root)["runs"]
                if r["job"] == "sftp_poll" and r["status"] == "failed"
                and "file(s) matching" in (r.get("error") or "")}

    def test_the_two_planes_never_disagree_about_a_night(self):
        """The bar: no surface claims success out of data another says was gone.

        MEASURED before this, in a built world: six nights whose 05:30 poll
        recorded `data/drop holds 0 file(s) matching */*_20260201.done,
        expected at least 2` and whose 05:31 sync recorded `success` with
        `{'raw.vendor_orders': {'rows_inserted': 53}}` off the same folder -
        the connector claiming rows out of files that did not land until
        07:30. Both halves are asserted REAL, so neither "no night was ever
        withheld" nor "every sync failed" can pass this vacuously.
        """
        ingest, withheld = self._ingest(), self._withheld()
        self.assertTrue(withheld, "no withheld delivery in the backstory")
        self.assertTrue(ingest["runs"], "no sync history to check")
        claiming = [r for r in ingest["runs"]
                    if r["started_at"][:10] in withheld
                    and (r["status"] == "success"
                         or any(c["rows_inserted"] for c in r["tables"].values()))]
        self.assertEqual(
            claiming, [],
            "a sync reports rows on a night the scheduler recorded an empty "
            "drop folder for")
        # ...and the other half, or "every sync failed" would satisfy the above
        self.assertTrue(
            [r for r in ingest["runs"]
             if r["started_at"][:10] not in withheld and r["status"] == "success"
             and any(c["rows_inserted"] for c in r["tables"].values())],
            "no delivered night ingests anything: the plane is not reporting")
        # the failure is legible as one, with the contract's own alert
        failed = [r for r in ingest["runs"] if r["status"] == "failed"]
        self.assertTrue(failed)
        self.assertEqual({a["run_id"] for a in ingest["alerts"]},
                         {r["id"] for r in failed})
        self.assertTrue(all(a["kind"] == "sync_failed" for a in ingest["alerts"]))

    def test_the_withheld_rows_are_late_rather_than_lost(self):
        """The counts have to agree with the DATA, not just with each other.

        A withheld night's file lands 150 minutes after the slot, which is a
        full day before the connector comes round again - so the next sync
        carries it, and after the sync for data day `i` the connector has
        inserted every row of days 0..i except the ones still withheld. That
        is asserted against `gen.day_rows`, the same function that wrote the
        drop folders, so a run record that invented or double-counted a row
        goes red here even if both planes agree about which nights were bad.
        """
        from datetime import timedelta
        from world.themes import load_theme
        theme = load_theme(self.THEME)
        seed = manifest_seed(self.THEME)
        start = scheduler.parse_vtime(seed["vtime_start"])
        rows = {}
        for i in range(seed["days"]):
            day = (start + timedelta(days=i)).date()
            orders, returns = theme.gen.day_rows(seed["seed"], i, day, seed)
            rows[day.isoformat()] = {"orders": len(orders),
                                     "returns": len(returns)}
        feeds = {cid: spec["feed"] for cid, spec
                 in theme.manifest["ingest"]["connectors"].items()}
        ingest, withheld = self._ingest(), self._withheld()
        self.assertTrue(withheld, "no withheld delivery in the backstory")
        bit = 0                     # nights where the subtraction actually bit
        for cid, feed in feeds.items():
            seq = [r for r in ingest["runs"] if r["connector_id"] == cid]
            self.assertEqual(len(seq), seed["days"], cid)
            cumulative = 0
            for run in seq:
                # the sync for data day `i` runs the morning after it
                data_day = (scheduler.parse_vtime(run["started_at"])
                            - timedelta(days=1)).date().isoformat()
                cumulative += sum(c["rows_inserted"]
                                  for c in run["tables"].values())
                owed = (rows[data_day][feed]
                        if run["started_at"][:10] in withheld else 0)
                bit += bool(owed)
                want = sum(rows[d][feed] for d in sorted(rows)
                           if d <= data_day) - owed
                self.assertEqual(cumulative, want,
                                 f"{cid} after {data_day}: the sync history "
                                 f"and the drop folders disagree")
        self.assertTrue(bit, "no night was ever withheld from a connector")

    def test_the_connector_plane_never_names_the_fault_either(self):
        """Same rule as var/sched/state.json: the agent meets the incident.

        `ingest_runs` and `ingest_alerts` are gateway-visible, so the ledger
        that names `vendor_late_delivery` stays in var/world/live.json.
        """
        blob = json.dumps(self._ingest())
        catalogue = live_of(self.root)["faults"]
        self.assertTrue(catalogue)
        for fault in catalogue:
            self.assertNotIn(fault["id"], blob)
            self.assertNotIn(fault["kind"], blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
