# /grading — post-episode grading zone (owned by Track C)

This directory is mounted read-only into the `grading` container, which has
no network and never runs during the episode (split_of_work.md §1.1, §2.1).

Track C fills it per §2.3/§2.4:

- `grade.py`       — in: final world (ro), this dir, action-log path, agent's
                     final artifact; out: `score.json`
- `expected/`      — expected.duckdb and friends (reference state)
- `manifest.yaml`  — planted defects, for per-cause credit
- `solution.sh`    — oracle (sage baseline must score 100%)

Track A's only obligations to this zone: the world volume restores
byte-identically from a snapshot, and `world.fingerprint` gives the grader a
stable semantic identity for the world it is scoring.
