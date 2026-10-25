"""scheduler.advance honors workspace/orchestration/schedule.yml.

S09 inverts factory_publish (21:50) ahead of factory_load (22:00) in
var/world/live.json. The repair is the visible schedule: an after-mapping,
a bare job name, or a later clock time. advance must start publish after
load has finished, and after factory_load_retry when that job exists.
A world with no schedule.yml keeps the live.json order.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import scheduler  # noqa: E402

VNOW = "2026-06-10T00:00:00Z"
LOAD_AT = "22:00"
PUBLISH_AT = "21:50"


def _job(command, duration_s, time=None, after=None):
    spec = {"command": command, "duration_s": duration_s, "day_offset": 0,
            "after": after}
    if time is not None:
        spec["time"] = time
    return spec


def _world(root, schedule=None, retry=False):
    jobs = {
        "factory_load": _job("factory_load", 30, time=LOAD_AT),
        "factory_publish": _job("factory_publish", 30, time=PUBLISH_AT),
    }
    if retry:
        jobs["factory_load_retry"] = _job(
            "factory_load_retry", 30,
            after={"job": "factory_load", "minutes": 5})
    (root / "var/sched").mkdir(parents=True, exist_ok=True)
    scheduler.save(root, {
        "vnow": VNOW,
        "jobs": {name: {"command": spec["command"], "duration_s": spec["duration_s"]}
                 for name, spec in jobs.items()},
        "runs": [],
    })
    scheduler.save_live(root, {
        "vtime_start": VNOW,
        "days_landed": 0,
        "jobs": jobs,
    })
    if schedule is not None:
        path = root / "workspace/orchestration/schedule.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(schedule, encoding="utf-8")


def _runs(root):
    return {run["job"]: run for run in scheduler.load(root)["runs"]}


class WorkspaceScheduleSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _advance(self, schedule, retry=False):
        _world(self.root, schedule, retry=retry)
        scheduler.advance(self.root, 24)
        return _runs(self.root)

    def _assert_publish_after_load(self, runs):
        publish = scheduler.parse_vtime(runs["factory_publish"]["vts_start"])
        load_end = scheduler.parse_vtime(runs["factory_load"]["vts_end"])
        self.assertGreater(publish, load_end, runs)

    def test_after_dict_runs_publish_after_load_end(self):
        runs = self._advance(
            "jobs:\n  factory_publish:\n"
            "    after: {job: factory_load, minutes: 20}\n")
        self._assert_publish_after_load(runs)
        self.assertEqual(runs["factory_load"]["vts_start"], "2026-06-10T22:00:00Z")
        self.assertEqual(runs["factory_publish"]["vts_start"], "2026-06-10T22:20:00Z")
        # The overlay is re-read next advance; live.json on disk stays the
        # control plane this world was seeded with.
        self.assertEqual(
            scheduler.load_live(self.root)["jobs"]["factory_publish"]["time"],
            PUBLISH_AT)

    def test_after_string_runs_publish_after_load_end(self):
        runs = self._advance(
            "jobs:\n  factory_publish:\n    after: factory_load\n")
        self._assert_publish_after_load(runs)

    def test_later_time_runs_publish_after_load_end(self):
        runs = self._advance(
            'jobs:\n  factory_publish:\n    time: "22:20"\n')
        self._assert_publish_after_load(runs)
        self.assertEqual(runs["factory_publish"]["vts_start"], "2026-06-10T22:20:00Z")

    def test_retry_holds_publish_until_after_retry_end(self):
        runs = self._advance(
            "jobs:\n  factory_publish:\n    after: factory_load\n",
            retry=True)
        self._assert_publish_after_load(runs)
        publish = scheduler.parse_vtime(runs["factory_publish"]["vts_start"])
        retry_end = scheduler.parse_vtime(runs["factory_load_retry"]["vts_end"])
        self.assertGreater(publish, retry_end, runs)
        order = [run["job"] for run in scheduler.load(self.root)["runs"]]
        self.assertEqual(
            order, ["factory_load", "factory_load_retry", "factory_publish"])

    def test_missing_schedule_keeps_inverted_order(self):
        runs = self._advance(None)
        publish = scheduler.parse_vtime(runs["factory_publish"]["vts_start"])
        load = scheduler.parse_vtime(runs["factory_load"]["vts_start"])
        self.assertLess(publish, load)
        self.assertEqual(runs["factory_publish"]["vts_start"], "2026-06-10T21:50:00Z")
        self.assertEqual(runs["factory_load"]["vts_start"], "2026-06-10T22:00:00Z")

    def test_copies_time_and_after_onto_existing_factory_jobs_only(self):
        live = {"jobs": {
            "factory_arrive": _job("factory_arrive", 15, time="08:00"),
            "factory_load": _job("factory_load", 30, time="22:00"),
            "factory_publish": _job("factory_publish", 30, time="21:50"),
            "factory_close": _job("factory_close", 15, time="23:00"),
            "theme_job": _job("theme_job", 10, time="01:00"),
        }}
        _world(self.root, None)
        # replace the tiny world's jobs with this set, then overlay
        scheduler.save_live(self.root, {"vtime_start": VNOW, "days_landed": 0,
                                        "jobs": live["jobs"]})
        text = (
            "jobs:\n"
            "  factory_arrive:\n"
            "    time: \"07:30\"\n"
            "  factory_close:\n"
            "    after: {job: factory_load, minutes: 40}\n"
            "  not_a_live_job:\n"
            "    time: \"12:00\"\n"
        )
        path = self.root / "workspace/orchestration/schedule.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        fresh = scheduler.load_live(self.root)
        scheduler._sync_workspace_schedule(self.root, fresh)
        jobs = fresh["jobs"]
        self.assertEqual(jobs["factory_arrive"]["time"], "07:30")
        self.assertEqual(jobs["factory_close"]["after"],
                         {"job": "factory_load", "minutes": 40})
        self.assertEqual(jobs["theme_job"]["time"], "01:00")
        self.assertNotIn("not_a_live_job", jobs)


if __name__ == "__main__":
    unittest.main()
