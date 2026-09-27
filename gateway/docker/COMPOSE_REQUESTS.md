# What the native stack needs from files COMPOSE-DISCOVERY does not own

Counterpart to `tasks/harness/NATIVE_DRIVER_REQUESTS.md`. Each item says what
is broken today and what breaks if it is not supplied, so none of them can be
dropped quietly. Owner of this file: COMPOSE-DISCOVERY.

## 1. The episode image must carry the `native` extra — owner: `gateway/docker/Dockerfile`

**Measured, and it is why P6 ran against stubs.** The nine service modules have
landed and import `fastapi`; the episode image does not have it:

    docker run --rm --entrypoint python alkera-native:probe -c \
      "from alkera_gateway.native import services; print(services.load()[1])"
    -> nine ModuleNotFoundError: No module named 'fastapi'

`gateway/pyproject.toml` now declares the extra (`fastapi`, `uvicorn`, `httpx`,
`jira`, `slack_sdk`, `atlassian-python-api`). The Dockerfile's
`pip install --no-cache-dir --no-deps /opt/gateway` has to become an install
that resolves it — and the repository installs from a `--require-hashes` lock,
so the honest change is a `gateway/requirements-native.lock` beside
`requirements.lock` plus one `RUN pip install ... -r` line.

Until then `compose.native.yml` needs `ALKERA_NATIVE_STUBS` to serve anything,
and `evidence/containment-native.json` says so in `stub_services`.

## 2. `<root>/home` is not fingerprinted — owner: `world/world/fingerprint.py`

`semantic()` hashes `workspace/`, so the native discovery layer's `.envrc` and
`docs/onboarding.md` are covered and `~/.dbt/profiles.yml` and
`~/.config/alkera/services.toml` are not. A conditional section on the pattern
of `workflow` and `obs` — hash `home/` when the directory exists — costs every
existing world nothing and closes the gap:

```python
home = root / "home"
if home.is_dir():
    sections["home"] = tree_hash(home)
```

`world/tests/test_native_workspace.py` asserts those two files' bytes directly
across two builds, which is the same guarantee by a weaker route: a home
directory edited *after* the build still passes `semantic_hash` today.

## 3. The MinIO audit tailer has to listen on `native-services.internal:8110` — owner: AUDIT

`compose.native.yml` enables MinIO's **audit** webhook (every API call,
including reads — not the notify webhook, which is object events only) and
points it at `${ALKERA_MINIO_AUDIT_ENDPOINT:-http://native-services.internal:8110/minio/audit}`.
Nothing listens there yet, which costs nothing: `MINIO_AUDIT_WEBHOOK_QUEUE_DIR`
spools every event on disk until something answers. `8110` and not `8099`
because `8099` is `serve.py`'s aggregate `/health`.

If the tailer wants a different path or port, change the default in
`compose.native.yml` — it is one line and it is ours.

## 4. `world/world/build.py` seed flag — owner: COMPOSE-DISCOVERY (done, noted here)

Three lines: an import, and `if seed.get("native"): native_workspace.write(root,
theme)` immediately before `_git(workspace, seed)`. Ordering is load-bearing
twice — after `write_workflow` because the onboarding page reads the corpus,
before `_git` because on a real box those two files are checked in and a file
the commit missed shows up as a dirty tree on the agent's first `git status`.

## 5. Airflow is declared and never started — owner: whoever wires the vclock bridge

`compose.native.yml` carries an `airflow` service behind `--profile airflow`
(`airflow standalone`, DAGs from `/world/workspace/dags`). Nothing in this
repository drives it: there is no DAG bundle and no bridge from
`_archive/world/world/airflow_bridge.py` (archived) to this container. It is declared so the alias
table is complete and the next builder edits a stanza instead of inventing one.
`evidence/containment-native.json` records it under
`backends_declared_but_not_running` and claims nothing about it.

## 6. Postgres identity in native mode — owner: SERVICES / AUDIT to confirm

`NATIVE_DRIVER_REQUESTS.md` item 7 ships `PGUSER=alkera`, `PGDATABASE=alkera`,
password `alkera-local-dev`. `compose.native.yml` sets `POSTGRES_DB: alkera` and
adds an inline `configs:` init script creating role `alkera` as a
**non-superuser** — the agent has `psql` now, and a superuser session would be
a second channel to everything the server can reach, which is the rule
containment P3 checks for the gateway's own role in MCP mode.

What is **not** done: nothing seeds the native world's tables into that server.
`world/world/warehouse_seed.seed_postgres` exists and takes a DSN; somebody has
to call it for the native stack, and `GRANT`s on tables created afterwards are
not covered by the `ON ALL TABLES` grant in the init script.

## 7. `s3` (adobe/s3mock) is switched off in native mode — informational

`compose.native.yml` puts the `s3` service behind `--profile mock-s3` so MinIO
takes its place. An overlay cannot remove a service, and a profile nobody
enables is the only way to keep one out of a stack. `compose.yml` is untouched
and the MCP stack is unaffected.
