"""World clock (offline) and replay.

The clock ticks on agent command boundaries, never on wall time, and the only
thing that moves the world is `scheduler.advance`. These tests pin the tick
rule (commands m, 2m, ...), the post-episode run to T (last step shortened,
nothing when already at/after T), and that `replay` of a recorded schedule
lands the world the live clock did.
"""
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import build as wbuild, clock, scheduler  # noqa: E402
from world.hashing import file_hash  # noqa: E402

TRACK_A = Path(__file__).resolve().parents[1]
SEED = TRACK_A / "seed.example.yaml"
LOCK = TRACK_A / "requirements.lock"
STATE = Path("var/sched/state.json")

# 7 commands with m=3 tick before commands 3 and 6 (12h); T=27 then needs
# post steps of 6, 6 and a shortened 3.
SPEC = clock.TickSpec(m=3, h_hours=6.0, T_hours=27.0)
COMMANDS = 7


def vnow(root):
    return scheduler.load(root)["vnow"]


def hours_between(a, b):
    return (scheduler.parse_vtime(b) - scheduler.parse_vtime(a)) / timedelta(hours=1)


class NoOp:
    name = "noop"

    def act(self, root, tick, vnow):
        return []


class Recording:
    """Logs every act and every tick, so the call order can be asserted."""
    name = "recording"

    def __init__(self, log):
        self.log = log

    def act(self, root, tick, now):
        self.log.append(("act", tick, now, vnow(root)))
        return [f"looked@{tick}"]


class ClockTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="clock_"))
        lock = LOCK if LOCK.exists() else None
        cls.base = wbuild.build(SEED, cls.tmp / "base", lock)
        cls.t0 = vnow(cls.base)

        # the live run: COMMANDS commands, then finish to T
        cls.live = shutil.copytree(cls.base, cls.tmp / "live")
        cls.ticked = []
        c = clock.Clock(cls.live, SPEC,
                        on_tick=lambda root, rec: cls.ticked.append(
                            (rec["tick"], vnow(root))))
        cls.fired = [c.before_command() for _ in range(COMMANDS)]
        cls.post = c.finish()
        cls.schedule = c.schedule()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def copy(self, name):
        return shutil.copytree(self.base, self.tmp / name)

    # -- episode ticks -----------------------------------------------------

    def test_before_command_fires_exactly_on_multiples_of_m(self):
        fired_on = [n for n, rec in enumerate(self.fired, start=1) if rec]
        self.assertEqual(fired_on, [3, 6])
        for rec in filter(None, self.fired):
            self.assertEqual(rec["phase"], "episode")
            self.assertEqual(hours_between(rec["vnow_before"], rec["vnow_after"]), 6.0)
        first, second = self.fired[2], self.fired[5]
        self.assertEqual((first["tick"], first["after_command"]), (0, 3))
        self.assertEqual((second["tick"], second["after_command"]), (1, 6))
        self.assertEqual(first["vnow_before"], self.t0)
        self.assertEqual(second["vnow_before"], first["vnow_after"])

    # -- post-episode ticks ------------------------------------------------

    def test_finish_steps_to_T_and_shortens_the_last_step(self):
        steps = [hours_between(r["vnow_before"], r["vnow_after"]) for r in self.post]
        self.assertEqual(steps, [6.0, 6.0, 3.0])
        self.assertEqual([r["tick"] for r in self.post], [2, 3, 4])
        for r in self.post:
            self.assertEqual(r["phase"], "post")
            self.assertIsNone(r["after_command"])
        self.assertEqual(hours_between(self.t0, vnow(self.live)), 27.0)

    def test_on_tick_runs_after_each_advance(self):
        ticks = [r for r in self.schedule["ticks"]]
        self.assertEqual(self.ticked,
                         [(r["tick"], r["vnow_after"]) for r in ticks])

    def test_schedule_shape(self):
        s = self.schedule
        self.assertEqual((s["m"], s["h_hours"], s["T_hours"]), (3, 6.0, 27.0))
        self.assertEqual([r["tick"] for r in s["ticks"]], [0, 1, 2, 3, 4])
        for r in s["ticks"]:
            self.assertEqual(sorted(r), ["after_command", "phase", "tick",
                                         "vnow_after", "vnow_before"])

    def test_finish_is_a_noop_at_or_after_T(self):
        # Review Focus 4: the episode already passed T -> no post ticks and
        # no negative advance
        root = self.copy("past_T")
        c = clock.Clock(root, clock.TickSpec(m=1, h_hours=6.0, T_hours=4.0))
        self.assertIsNotNone(c.before_command())
        after = vnow(root)
        self.assertEqual(c.finish(), [])
        self.assertEqual(vnow(root), after)
        # exactly at T: also none
        root = self.copy("at_T")
        c = clock.Clock(root, clock.TickSpec(m=1, h_hours=6.0, T_hours=6.0))
        c.before_command()
        self.assertEqual(c.finish(), [])
        self.assertEqual(hours_between(self.t0, vnow(root)), 6.0)

    def test_finish_twice_adds_nothing(self):
        root = self.copy("twice")
        c = clock.Clock(root, clock.TickSpec(m=5, h_hours=6.0, T_hours=0.0))
        self.assertEqual(c.finish(), [])
        self.assertEqual(c.finish(), [])
        self.assertEqual(vnow(root), self.t0)

    # -- replay ------------------------------------------------------------

    def test_replay_noop_lands_the_live_world(self):
        root = self.copy("replay")
        out = clock.replay(root, self.schedule, NoOp())
        self.assertEqual(vnow(root), vnow(self.live))
        self.assertEqual(file_hash(root / STATE), file_hash(self.live / STATE))
        self.assertEqual([(r["tick"], r["vnow_before"], r["vnow_after"]) for r in out],
                         [(r["tick"], r["vnow_before"], r["vnow_after"])
                          for r in self.schedule["ticks"]])

    def test_replay_acts_first_at_minus_one_then_before_each_advance(self):
        root = self.copy("order")
        log = []
        out = clock.replay(root, self.schedule, Recording(log),
                           on_tick=lambda r, rec: log.append(
                               ("tick", rec["tick"], rec["vnow_after"], vnow(r))))
        expected = [("act", -1, self.t0, self.t0)]
        for r in self.schedule["ticks"]:
            expected.append(("act", r["tick"], r["vnow_before"], r["vnow_before"]))
            expected.append(("tick", r["tick"], r["vnow_after"], r["vnow_after"]))
        self.assertEqual(log, expected)
        self.assertEqual([r["actions"] for r in out],
                         [[f"looked@{r['tick']}"] for r in self.schedule["ticks"]])

    def test_replay_refuses_a_schedule_from_another_start(self):
        root = self.copy("wrong_start")
        scheduler.advance(root, 1)
        with self.assertRaises(ValueError):
            clock.replay(root, self.schedule, NoOp())

    # -- path independence -------------------------------------------------

    def test_ticked_run_equals_one_advance(self):
        root = self.copy("single")
        scheduler.advance(root, SPEC.T_hours)
        self.assertEqual(file_hash(root / STATE), file_hash(self.live / STATE))


class TickSpecTest(unittest.TestCase):
    def test_rejects_bad_specs(self):
        for bad in [dict(m=0, h_hours=6.0, T_hours=1.0),
                    dict(m=1, h_hours=0.0, T_hours=1.0),
                    dict(m=1, h_hours=6.0, T_hours=-1.0),
                    dict(m=1, h_hours=scheduler.MAX_HOURS + 1, T_hours=1.0)]:
            with self.assertRaises(ValueError, msg=bad):
                clock.TickSpec(**bad)


if __name__ == "__main__":
    unittest.main()
