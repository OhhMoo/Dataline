# Ad attribution workspace

Nightly impression, click, and S2S conversion exports feed a DuckDB/dbt
attribution pipeline and partner-finance handoff.

## Layout

- `data/landing/<entity>/dt=YYYY-MM-DD/` — immutable daily CSV exports.
- `data/config/` — partner terms, active bot exclusions, and window controls.
- `loader/load_events.py` — full raw reload with deterministic ordering.
- `dbt_project/` — typed staging views, attribution fact, payout handoff, locks.
- `dags/` — deployed nightly DAG documentation.
- `docs/` — attribution contract, data dictionary, and operations runbook.

## Commands

```sh
./run.sh load       # rebuild raw.* from every landed export
./run.sh attribute  # rebuild warehouse.fct_attributed_conversions and tests
./run.sh lock       # rebuild partner handoff and billing locks
./run.sh build      # complete dbt build and test suite
```

The warehouse is `../var/warehouse/warehouse.duckdb`. `run.sh` uses the
committed profile in `config/`; scheduled runs render an equivalent profile
outside the repository.

## Output grains

- `warehouse.fct_attributed_conversions`: one row per attributed conversion.
- `warehouse.partner_payout_handoff`: one row per event date and partner.
- `warehouse.billing_lock`: one row per locked event date and partner.

All monetary amounts are integer cents. Event and schedule timestamps are UTC.
