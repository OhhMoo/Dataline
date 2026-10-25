"""Workspace schedule sync: the agent-editable schedule.yml drives execution.

Stressor S09 (FT09/FT14) shipped worlds whose visible deployment file,
`workspace/orchestration/schedule.yml`, was documentation only: `advance`
read execution order solely from var/world/live.json, so a repaired
schedule.yml changed nothing and the broken DAG replayed every night.
`_sync_workspace_schedule` overlays the file onto the in-memory jobs each
advance - in memory only, live.json on disk stays canonical - and must
never crash on the file, however malformed.

Two levels of proof. The unit classes pin the field-level sync rules and
the file-level guards on bare directories; the end-to-end class builds one
real settlement_clearing world and walks the S09 repair path: baseline
order, a re-chained `after` swapping the executed order, every
schedule.yml form (time, after-dict, bare-string after, wait_minutes,
day_offset), and every negative (missing file, malformed YAML, unknown
jobs, unknown `after` targets, dependency loops). The shipped schedule.yml
mirrors the manifest, so syncing it must be byte-for-byte a no-op - the
backwards-compatibility gate.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import build as wbuild, scheduler  # noqa: E402

THEME = (Path(__file__).resolve().parents[1]
         / "world/themes/settlement_clearing")
SEED = THEME / "seed.example.yaml"
OUT = Path("/tmp/p3_sync_world")
SCHEDULE = Path("workspace/orchestration/schedule.yml")

# The canonical night: payments at 20:00, then the after-chain +10/+10/+20.
BASELINE = [("payments_nightly", "20:00"), ("dbt_payments", "20:10"),
            ("settlement_publish", "20:20"), ("clearing_window_close", "20:40")]


def job_order(res):
    """(name, HH:MM) per executed job in the span, in execution order."""
    return [(e["name"], e["vts"][11:16]) for e in res["events"]
            if e["kind"] == "job"]


class SyncRuleUnits(unittest.TestCase):
    """The field-level sync rules, without a world build."""

    def job(self):
        return {"time": "20:00", "day_offset": 0}

    def test_valid_time_replaces(self):
        job = self.job()
        scheduler._sync_job_fields(job, {"time": "21:45"}, {"j": job})
        self.assertEqual(job["time"], "21:45")

    def test_invalid_time_is_skipped(self):
        # an unquoted 20:00 is the YAML sexagesimal int 1200 - wrong type,
        # not a time; every one of these must leave the job untouched
        for bad in (1200, "25:00", "20:60", "20:00:00", "8pm", None, True, 20.0):
            job = self.job()
            scheduler._sync_job_fields(job, {"time": bad}, {"j": job})
            self.assertEqual(job["time"], "20:00", repr(bad))

    def test_after_dict_requires_a_known_target(self):
        jobs = {"a": self.job(), "b": {"after": {"job": "a", "minutes": 10}}}
        scheduler._sync_job_fields(
            jobs["b"], {"after": {"job": "a", "minutes": 30}}, jobs)
        self.assertEqual(jobs["b"]["after"], {"job": "a", "minutes": 30})
        scheduler._sync_job_fields(
            jobs["b"], {"after": {"job": "ghost", "minutes": 5}}, jobs)
        # unknown target: skipped, the canonical after survives - job_starts
        # would have raised 'unresolvable job schedule' otherwise
        self.assertEqual(jobs["b"]["after"], {"job": "a", "minutes": 30})

    def test_after_dict_minutes_defaults_to_zero(self):
        jobs = {"a": self.job(), "b": {}}
        scheduler._sync_job_fields(jobs["b"], {"after": {"job": "a"}}, jobs)
        self.assertEqual(jobs["b"]["after"], {"job": "a", "minutes": 0})
        scheduler._sync_job_fields(
            jobs["b"], {"after": {"job": "a", "minutes": "ten"}}, jobs)
        self.assertEqual(jobs["b"]["after"], {"job": "a", "minutes": 0})

    def test_bare_string_after_and_wait_minutes(self):
        jobs = {"a": self.job(), "b": {}}
        scheduler._sync_job_fields(
            jobs["b"], {"after": "a", "wait_minutes": 30}, jobs)
        self.assertEqual(jobs["b"]["after"], {"job": "a", "minutes": 30})
        scheduler._sync_job_fields(jobs["b"], {"after": "a"}, jobs)
        self.assertEqual(jobs["b"]["after"], {"job": "a", "minutes": 0})
        scheduler._sync_job_fields(
            jobs["b"], {"after": "a", "wait_minutes": "soon"}, jobs)
        self.assertEqual(jobs["b"]["after"], {"job": "a", "minutes": 0})
        scheduler._sync_job_fields(jobs["b"], {"after": "ghost"}, jobs)
        self.assertEqual(jobs["b"]["after"], {"job": "a", "minutes": 0})

    def test_day_offset_only_for_time_jobs(self):
        timed = self.job()
        scheduler._sync_job_fields(timed, {"day_offset": 1}, {"j": timed})
        self.assertEqual(timed["day_offset"], 1)
        chained = {"after": {"job": "j", "minutes": 5}}
        scheduler._sync_job_fields(chained, {"day_offset": 1}, {"j": chained})
        self.assertNotIn("day_offset", chained)
        for bad in (True, "1", 1.5):
            job = self.job()
            scheduler._sync_job_fields(job, {"day_offset": bad}, {"j": job})
            self.assertEqual(job["day_offset"], 0, repr(bad))


class SyncHookUnits(unittest.TestCase):
    """The hook's file-level guards, on a bare directory (no world build)."""

    def setUp(self):
        OUT.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="sync_hook_", dir=OUT))
        self.live = {"jobs": {
            "a": {"time": "20:00", "day_offset": 0},
            "b": {"after": {"job": "a", "minutes": 10}}}}
        self.jobs_before = self.live["jobs"]

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, text):
        path = self.root / SCHEDULE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_missing_file_is_a_noop(self):
        scheduler._sync_workspace_schedule(self.root, self.live)
        self.assertIs(self.live["jobs"], self.jobs_before)

    def test_malformed_yaml_is_a_noop(self):
        self.write("jobs:\n  a: [1, 2\n  b: }{")
        scheduler._sync_workspace_schedule(self.root, self.live)
        self.assertIs(self.live["jobs"], self.jobs_before)

    def test_wrong_document_shapes_are_noops(self):
        for text in ("- just\n- a\n- list\n",
                     "jobs: 42\n",
                     'feed_window: {lands_at: "04:00"}\n'):
            self.write(text)
            scheduler._sync_workspace_schedule(self.root, self.live)
            self.assertIs(self.live["jobs"], self.jobs_before, repr(text))

    def test_looping_rechain_is_discarded_wholesale(self):
        # a after b while b is after a: unresolvable, so the canonical jobs
        # survive untouched instead of crashing advance in job_starts
        self.write("jobs:\n  a:\n    after: b\n")
        scheduler._sync_workspace_schedule(self.root, self.live)
        self.assertIs(self.live["jobs"], self.jobs_before)

    def test_valid_overlay_swaps_in_a_copy(self):
        self.write('jobs:\n  a:\n    time: "06:30"\n  ghost: {time: "01:00"}\n')
        scheduler._sync_workspace_schedule(self.root, self.live)
        self.assertIsNot(self.live["jobs"], self.jobs_before)
        self.assertEqual(self.live["jobs"]["a"]["time"], "06:30")
        self.assertEqual(self.live["jobs"]["b"]["after"],
                         {"job": "a", "minutes": 10})
        self.assertNotIn("ghost", self.live["jobs"])
        # the pristine object the caller restores before save_live was
        # never touched - the overlay is a deep copy
        self.assertEqual(self.jobs_before["a"]["time"], "20:00")


class SettlementSyncEndToEnd(unittest.TestCase):
    """The S09 repair path, on a real settlement_clearing world."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        shutil.rmtree(OUT, ignore_errors=True)
        OUT.mkdir(parents=True)
        cls.base = OUT / "base"
        wbuild.build(SEED, cls.base)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="case_", dir=OUT))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def world(self, name="w"):
        return shutil.copytree(self.base, self.tmp / name)

    def write_schedule(self, w, text):
        (w / SCHEDULE).write_text(text, encoding="utf-8")

    def test_baseline_order_and_times(self):
        w = self.world()
        res = scheduler.advance(w, 24)
        self.assertEqual(job_order(res), BASELINE)
        self.assertTrue(all(e.get("status") == "success"
                            for e in res["events"] if e["kind"] == "job"))

    def test_time_edit_moves_the_chain(self):
        w = self.world()
        self.write_schedule(w, 'jobs:\n  payments_nightly:\n    time: "21:30"\n')
        res = scheduler.advance(w, 24)
        self.assertEqual(job_order(res), [
            ("payments_nightly", "21:30"), ("dbt_payments", "21:40"),
            ("settlement_publish", "21:50"), ("clearing_window_close", "22:10")])

    def test_after_dict_rechains_and_swaps_order(self):
        """The S09 repair: re-chaining an after in schedule.yml changes the
        executed order on the very next advance, and every later one."""
        w = self.world()
        self.write_schedule(
            w, "jobs:\n  clearing_window_close:\n"
               "    after: {job: payments_nightly, minutes: 5}\n")
        swapped = [("payments_nightly", "20:00"),
                   ("clearing_window_close", "20:05"),
                   ("dbt_payments", "20:10"),
                   ("settlement_publish", "20:20")]
        res = scheduler.advance(w, 24)
        self.assertEqual(job_order(res), swapped)
        self.assertTrue(all(e.get("status") == "success"
                            for e in res["events"] if e["kind"] == "job"))
        # the overlay re-reads the file each advance: the repaired order
        # replays on the next span without anyone touching live.json
        res2 = scheduler.advance(w, 24)
        self.assertEqual(job_order(res2), swapped)
        # and live.json on disk stayed canonical throughout
        self.assertEqual(scheduler.load_live(w)["jobs"],
                         scheduler.load_live(self.base)["jobs"])

    def test_bare_string_after_and_wait_minutes(self):
        w = self.world()
        self.write_schedule(
            w, "jobs:\n  dbt_payments:\n"
               "    after: payments_nightly\n    wait_minutes: 30\n")
        res = scheduler.advance(w, 24)
        self.assertEqual(job_order(res), [
            ("payments_nightly", "20:00"), ("dbt_payments", "20:30"),
            ("settlement_publish", "20:40"), ("clearing_window_close", "21:00")])

    def test_bare_string_after_defaults_to_zero_minutes(self):
        w = self.world()
        self.write_schedule(
            w, "jobs:\n  dbt_payments:\n    after: payments_nightly\n")
        res = scheduler.advance(w, 24)
        times = dict(job_order(res))
        self.assertEqual(times["payments_nightly"], "20:00")
        self.assertEqual(times["dbt_payments"], "20:00")

    def test_day_offset_shifts_the_processing_day(self):
        """day_offset syncs for a time job: with a daily cadence the clock
        slots are unchanged, but the occurrence belongs to another day."""
        w = self.world()
        self.write_schedule(w, "jobs:\n  payments_nightly:\n    day_offset: 1\n")
        res = scheduler.advance(w, 24)
        self.assertEqual(job_order(res), BASELINE)
        runs = [r for r in scheduler.load(w)["runs"]
                if r["job"] == "payments_nightly"]
        self.assertEqual(runs[-1]["detail"], "payments feed dt=2026-06-11")

    def test_missing_file_keeps_canonical_order(self):
        w = self.world()
        (w / SCHEDULE).unlink()
        res = scheduler.advance(w, 24)
        self.assertEqual(job_order(res), BASELINE)

    def test_malformed_yaml_never_crashes(self):
        w = self.world()
        (w / SCHEDULE).write_bytes(b"jobs:\n  a: [1, 2\n  b: }{")
        res = scheduler.advance(w, 24)
        self.assertEqual(job_order(res), BASELINE)
        # a well-formed YAML document of the wrong shape is ignored too
        self.write_schedule(w, "- just\n- a\n- list\n")
        res = scheduler.advance(w, 24)
        self.assertEqual(job_order(res), BASELINE)

    def test_after_to_unknown_job_is_skipped(self):
        w = self.world()
        self.write_schedule(
            w, "jobs:\n"
               "  clearing_window_close:\n"
               "    after: {job: ghost_job, minutes: 5}\n"
               "  dbt_payments:\n"
               "    after: ghost_job\n")
        res = scheduler.advance(w, 24)
        self.assertEqual(job_order(res), BASELINE)
        self.assertTrue(all(e.get("status") == "success"
                            for e in res["events"] if e["kind"] == "job"))

    def test_unknown_jobs_in_file_are_ignored(self):
        w = self.world()
        self.write_schedule(w, 'jobs:\n  phantom_job:\n    time: "12:00"\n')
        res = scheduler.advance(w, 24)
        self.assertEqual(job_order(res), BASELINE)

    def test_looping_rechain_never_crashes(self):
        # payments after dbt while dbt is after payments: the overlay would
        # be unresolvable, so it is discarded and the canonical night runs
        w = self.world()
        self.write_schedule(
            w, "jobs:\n  payments_nightly:\n    after: dbt_payments\n")
        res = scheduler.advance(w, 24)
        self.assertEqual(job_order(res), BASELINE)

    def test_shipped_file_is_byte_identical_to_no_file(self):
        """BACKWARDS COMPATIBILITY: the shipped schedule.yml mirrors the
        manifest, so syncing it is a no-op - a world with the file and a
        world without advance to byte-identical state."""
        a, b = self.world("a"), self.world("b")
        (b / SCHEDULE).unlink()
        res_a, res_b = scheduler.advance(a, 24), scheduler.advance(b, 24)
        self.assertEqual(res_a["events"], res_b["events"])
        for rel in ("var/sched/state.json", "var/world/live.json",
                    "var/ledger/receipts.jsonl"):
            self.assertEqual((a / rel).read_bytes(), (b / rel).read_bytes(), rel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
