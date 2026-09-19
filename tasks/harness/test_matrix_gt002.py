"""GT002's [matrix] declaration (new_matrix.md section 3.2), checked against
the checks its grader actually emits.

The declaration is pinned twice: parsed directly from task.toml (so the
behavioural fixtures below run against exactly what the file says), and
through taskspec.load (so a loader regression cannot silently change what
the declaration resolves to). The behavioural fixtures model the episodes
this mapping exists to tell apart:

  oracle       every check passes                       -> pass True
  idle         nothing done, cause never named          -> excluded (gate cold)
  coercer      drained the tail, forced the 26 undecodable records through a
               lossy decode instead of dead-lettering   -> admitted, pass False
  dropper      drained the tail, skipped the 26 without a trace
                                                        -> admitted, pass False

The gate is K9 (the primary cause named), not K2 and not K7
(MATRIX_RERUN_EDITS 0.1, 2026-09-17): K2 is a repair OUTCOME, so the one
coercer on record failed it, went gate-cold, and left the scoped denominator
- which then printed 1.0 and hid the exact fork the axis names. An episode
that mishandled the producer contract but named the cause must be ADMITTED
and score 0 on scoped, never excluded from the denominator that exists to
measure exactly that failure.
"""

import re
import tomllib
from pathlib import Path

from axes import MatrixSpec, admitted, axis_report, projected_pass

TASK_ROOT = Path(__file__).resolve().parent.parent / "GT002-orders-flatline"


def _load_matrix() -> MatrixSpec:
    raw = tomllib.loads((TASK_ROOT / "task.toml").read_text(encoding="utf-8"))
    m = raw["matrix"]
    return MatrixSpec(pass_axis=m["pass_axis"], gate=m.get("gate"),
                      accepted=tuple(m.get("accepted") or ()),
                      map={a: tuple(ids) for a, ids in m.get("map", {}).items()})


def _grader_check_ids() -> set[str]:
    src = (TASK_ROOT / "tests" / "grade.py").read_text(encoding="utf-8")
    return set(re.findall(r's\.add\("(K\d+)"', src))


def _checks(**cid_passed):
    return {cid: {"passed": p, "points": 1.0 if p else 0.0, "max": 1.0}
            for cid, p in cid_passed.items()}


def _episode(k1, k2, k7, k9=True):
    """A record carrying every check the grader emits; only the four the
    matrix reads vary between the stories above."""
    return _checks(K1=k1, K2=k2, K3=True, K4=True, K5=True, K6=True,
                   K7=k7, K8=True, K9=k9, K10=True)


def test_matrix_declares_scoped_pass_gated_on_found():
    m = _load_matrix()
    assert m.pass_axis == "scoped"
    assert m.gate == "found"


def test_the_loader_resolves_the_same_declaration():
    import taskspec
    loaded = taskspec.load(TASK_ROOT.name, TASK_ROOT.parent).matrix
    assert loaded == _load_matrix()


def test_every_mapped_check_is_one_the_grader_emits():
    s = _load_matrix()
    mapped = {cid for ids in s.map.values() for cid in ids}
    assert mapped <= _grader_check_ids()


def test_oracle_passes():
    s = _load_matrix()
    episode = _episode(k1=True, k2=True, k7=True)
    assert admitted(s, episode) is True
    assert projected_pass(s, episode) is True


def test_idle_episode_is_excluded_not_failed():
    s = _load_matrix()
    episode = _episode(k1=False, k2=False, k7=False, k9=False)
    assert admitted(s, episode) is False
    assert projected_pass(s, episode) is None


def test_coercer_is_admitted_and_fails_scoped():
    # Named the cause (K9 passes - the gate) and drained the tail, but the 26
    # records were forced through a coerced decode: they are not in the DLQ
    # (K7 fails) and the ledger cannot close against the harness-side
    # manifest (K1 fails).
    s = _load_matrix()
    episode = _episode(k1=False, k2=True, k7=False)
    assert admitted(s, episode) is True
    assert projected_pass(s, episode) is False


def test_the_recorded_coercer_stays_in_the_denominator():
    # mcp-20260916-k5 r3, the episode 0.1 exists for: named the cause (K9),
    # coerced (K7 fails), and therefore stopped the drain 23 records short of
    # the tail (K2 fails). Under the old K2 gate this episode was gate-cold
    # and LEFT the scoped denominator; under K9 it is admitted and scores 0.
    s = _load_matrix()
    episode = _episode(k1=False, k2=False, k7=False, k9=True)
    assert admitted(s, episode) is True
    assert projected_pass(s, episode) is False
    assert axis_report(s, episode)["axes"]["scoped"] == 0.0


def test_partial_ledger_is_admitted_and_fails():
    # Dead-lettered correctly (K7 passes), drained (K2 passes), but rows were
    # lost on replay: the ledger does not close (K1 fails).
    s = _load_matrix()
    episode = _episode(k1=False, k2=True, k7=True)
    assert projected_pass(s, episode) is False
    assert axis_report(s, episode)["axes"]["scoped"] < 1.0


def test_old_record_without_matrix_checks_withholds():
    s = _load_matrix()
    assert projected_pass(s, _checks(K3=True, K8=True)) is None
