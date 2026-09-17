# world/ — the world factory

**Purpose.** Turn a seed into a data engineer's working environment that rebuilds
byte-for-byte: warehouse, dbt project, scheduler on a virtual clock, human-context
files, faults. Nothing here is the agent's interface; the gateway is.

```
world/            the package (python -m world.build --seed S --root R)
  build.py gen.py themes/     seed -> world; four themes; opt-in seed flags `warehouses:`, `native:`
  fingerprint.py              the semantic hash every determinism claim rests on
  scheduler.py executor.py    virtual clock + the job runner (what a scheduled run really does)
  faults.py                   seeded ways the world breaks on its own
  ledger.py                   protected receipts of what the world published (grading layer 4)
  native_workspace.py         .envrc / profiles.yml / onboarding.md for native mode
  warehouse_seed.py vclock.py airflow_bridge.py   Postgres seeding, Airflow under the clock
docker/           episode container, compose
scripts/acceptance.sh   the 7-gate determinism battery -> evidence/acceptance.json
tests/            166 tests + 81 subtests (run with gateway/.venv's interpreter)
```

Read next: `README.md` (full guide), `LEDGER_CONTRACT.md`, `agent.md` §9.2.1, §11.3.
