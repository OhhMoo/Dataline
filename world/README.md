# World — deterministic data-agent worlds

Seed → reproducible data-agent world, per `agent.md` §9.3 Track A (the
"world and determinism" track). The product here is not the tools; it is
that the world is **provably the same world** every time it is built, and
that the grader can never be reached during an episode.

## Layout

```
world/            the builder package (stdlib + duckdb + dbt-duckdb + pyyaml)
  build.py        seed.yaml -> world (python -m world.build --seed S --root R)
  gen.py          per-day seeded RNG; integer cents only, never floats
  fingerprint.py  the two determinism gates + raw-byte diagnostic
  scheduler.py    deterministic job state machine + the virtual clock
  bi.py           dashboards = YAML of SQL panels -> computed values
  snapshot.py     deterministic tar save/restore + restore verify
  hashing.py      canonicalization spec (this IS the fingerprint contract)
seed.example.yaml placeholder schema — Track C's frozen §2.1 spec replaces it
docker/           image + 3-zone compose + egress gate
tests/            acceptance tests (stdlib unittest)
scripts/acceptance.sh   the full battery
grading/          skeleton for Track C's post-episode zone
requirements.lock pinned dependency closure (build provenance)
```

World layout (contract §2.1), built under `<root>` (`/world` in the episode
container): `workspace/` git repo (dbt project, landing CSVs, docs),
`var/warehouse/warehouse.duckdb`, `var/sched/state.json`, `var/bi/dashboards/`,
`build/provenance.json`.

## The two gates (and the diagnostic)

1. **Semantic fingerprint** — same seed built from scratch twice, on different
   hosts, must give the identical `semantic_hash`. Covers: warehouse schemas +
   canonically-ordered rows, scheduler state incl. v-clock, BI definitions,
   workspace files. Canonicalization rules live in `world/hashing.py`.
2. **Build provenance** — `build/provenance.json` attests seed hash,
   generator version, dependency versions, requirements.lock hash, image
   digest. The gate is that it is *recorded* and matches claims.
3. **Raw byte hash** of `warehouse.duckdb` — a diagnostic, NOT correctness.
   It IS the gate for snapshot restore, where bytes must match exactly.

## Determinism rules enforced in the builder

- RNG is per-day `Random(f"{seed}:{day}")`; iteration order irrelevant.
- Amounts are integer cents rendered `%.2f` — no binary floats in the pipeline.
- `PRAGMA threads=1` + `ORDER BY` on the raw load; dbt `threads: 1`.
- Git author/committer dates pinned to `vtime_start`; commit hashes reproduce.
- No wall-clock reads anywhere. All time comes from `var/sched/state.json`
  (`vnow`), which the scheduler advances only by seeded job durations.
- dbt runs as a subprocess (in-process `dbtRunner` leaks duckdb file locks)
  with `--target-path`/`--log-path` outside the fingerprinted workspace.

## Resolved contract wart: catalog/schema name collision

The duckdb catalog takes the file stem (`warehouse`), which collides with the
`warehouse` schema: `warehouse.fct_revenue` is ambiguous when the file is
opened directly. **Resolution (integration commit 1f7bd8c): never open the
warehouse file unprefixed — always `ATTACH` under an alias.** `bi.py` attaches
as `wh` + `USE wh.warehouse`, and Track B's gateway does the same, so world
content stays clean two-part SQL. `fingerprint.py` hashes BASE TABLES only —
a view is a deterministic function of its inputs, so hashing it is redundant
and was fragile.

## Trust zones

`docker/docker-compose.yml`: `episode` (internal network — egress impossible),
`harness` (reaches episode only), `grading` (`network_mode: none`, world
read-only, starts post-episode only). Boot restores `/snapshot/world.tar` if
mounted, else builds from seed.

## Run it

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.lock
bash scripts/acceptance.sh          # the full battery (venv auto-detected)
.venv/Scripts/python -m unittest discover -s tests   # just the tests
```

## Deliberate limits (out of scope per §3)

- Scheduler job `command`s record intent only — Track C wires real behavior
  through the same state transitions.
- Placeholder schema stands in for Track C's frozen §2.1 spec.
- Cross-host fingerprint equality is asserted by CI rerunning
  `scripts/acceptance.sh` on a second host and comparing hashes (gate 1
  prints the hash for this purpose).
- No Airflow, no vendor BI, no Snowflake fidelity — five surfaces only.
