# World — deterministic data-agent worlds

Seed → reproducible data-agent world, per `agent.md` §9.3 Track A (the
"world and determinism" track). The product here is not the tools; it is
that the world is **provably the same world** every time it is built, and
that the grader can never be reached during an episode.

## Layout

```
world/            the builder package (stdlib + duckdb + dbt-duckdb + pyyaml)
  build.py        seed.yaml -> world (python -m world.build --seed S --root R)
  themes/         load_theme(name): a world *shape* as data, not code
    <name>/       theme.yaml + gen.py hooks + workspace/ tree + bi/*.yml
                  + seed.example.yaml — see "Themes" below
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

## Themes

`seed["theme"]` (default `revenue_daily`) selects a directory under
`world/world/themes/`; the builder is generic and a new world never touches
build.py / scheduler.py / fingerprint.py / bi.py. A theme provides:

- `theme.yaml` — manifest: `name`, `dbt_profile` (matching the theme's
  dbt_project.yml), and `jobs` (scheduler run specs per past day).
- `gen.py` — two hooks: `write_landing(workspace, seed)` and
  `load_raw(workspace, db_path)`. Imported in place; themes are not packages.
- `workspace/` — static files (dbt project, README, docs) copied byte-verbatim
  into the built workspace.
- `bi/*.yml` — dashboard specs copied verbatim to `var/bi/dashboards/`.
- `seed.example.yaml` — an example seed for the theme.

Themes must load their base tables into schemas `raw`, `staging`, `warehouse`
— `fingerprint.py` hashes exactly those three.

## The two gates (and the diagnostic)

1. **Semantic fingerprint** — same seed built from scratch twice, on different
   hosts, must give the identical `semantic_hash`. Covers: warehouse schemas +
   canonically-ordered rows, scheduler state incl. v-clock, BI definitions,
   workspace files. Canonicalization rules live in `world/hashing.py`.
   `scripts/acceptance.sh` gate 3 proves this live: it builds the same seed on
   the host and inside the `alkera-world:0.1.0` container and requires equal
   semantic hashes (with no docker daemon it falls back to a weaker same-host
   double-build and says so).
2. **Build provenance** — `build/provenance.json` attests seed hash,
   generator version, dependency versions, requirements.lock hash, image
   digest. The gate is that it is *recorded* and matches claims.
3. **Raw byte hash** of `warehouse.duckdb` — a diagnostic, NOT correctness.
   It IS the gate for snapshot restore, where bytes must match exactly.

## Determinism rules enforced in the builder

- Theme gen hooks (revenue_daily): RNG is per-day `Random(f"{seed}:{day}")`;
  iteration order irrelevant. Amounts are integer cents rendered `%.2f` — no
  binary floats in the pipeline.
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
mounted, else builds from the seed at `$WORLD_SEED` (default `./seed.yaml`),
so a mounted theme seed boots without an image rebuild.

## Acceptance battery

`scripts/acceptance.sh` (venv auto-detected; docker gates need a daemon):

1. Host build → semantic fingerprint.
2. Snapshot restore → identical raw bytes.
3. **Cross-host determinism** — same seed built inside the container image;
   host and container semantic hashes must be equal. Only the semantic hash is
   compared (duckdb page bytes may differ across OS); `provenance.json` is
   attestation, not compared. No-daemon fallback: weaker same-host
   double-build, labelled as such.
4. Episode boot <60s; episode-zone egress impossible (`egress_test.py`).
5. **Quiescence** — fingerprint `/world` in the live episode, idle ~5s,
   re-fingerprint: must be equal. Then a health check plus one read-only
   `sql_query` through the gateway: a perturbation here is a Track B follow-up
   (gateway read path), reported as a warning, not patched from this track.
6. MCP smoke + read-only `sql_query` via the harness; image <2GB.
7. **/grading unreachability** (ground rule §1.1: the grader does not exist
   during the episode) — with episode+harness live: no `/grading` in episode
   or harness, no grading container in `compose ps`. Then grading is brought
   up post-episode: `NetworkMode` must be `none`, episode still has no
   `/grading`, and the `grading` hostname is unresolvable from the episode.

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
- Cross-host fingerprint equality is proven per-run by acceptance gate 3
  (host build vs in-container build of the same seed); CI rerunning
  `scripts/acceptance.sh` on a second host adds a second data point.
- No Airflow, no vendor BI, no Snowflake fidelity — five surfaces only.
