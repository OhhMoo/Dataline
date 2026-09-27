# world/grading — the grading zone's mount point

`world/docker/docker-compose.yml` bind-mounts this directory read-only at `/grading` in
the `grading` service (profile `grading`, `network_mode: none`, post-episode only). This
README is the only tracked file; it keeps the directory in git so the bind mount has a
source. No code reads it.

The zone exists so `scripts/acceptance.sh` gate 7 can prove `/grading` is unreachable:
absent from the episode and harness containers while the episode runs, and, once the
grading container is up, still absent from the episode and its hostname unresolvable.

Real grading does not happen here. Each task's grader, reference state and manifest live
in `tasks/<task>/tests/` (`grade.py`, `manifest.yaml`, expected warehouses); the harness
stages a per-rollout grading directory outside the world root
(`tasks/harness/episode.py:build_world`, `tasks/harness/ledger.py:stage`) and never mounts
it into the episode container.
