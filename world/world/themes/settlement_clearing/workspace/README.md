# settlement-clearing

The nightly turns the card network's versioned payment feed into the settlement
that the clearing bank reads, then records the bank's cutoff acknowledgement.

```
data/payments_feed/dt=<day>/*.csv     immutable network events and recovery replays
data/control/                         merchant terms, cutoff config, correction register
pipelines/load_payments.py            feed -> raw.payments_events and raw control tables
dbt_project/                          staging -> fct_payments and reconciliation marts
pipelines/publish_settlement.py       fct_payments -> settlement_handoff
pipelines/clearing_window_close.py    handoff -> append-only clearing_window
orchestration/schedule.yml            deployed nightly order and publication consumers
docs/                                 source contract, runbook and correction protocol
```

Warehouse objects are in `raw`, `staging`, and `warehouse` schemas. The bank-facing
handoff keeps the contract's frozen five-column layout. Interchange is reconciled
separately in `warehouse.interchange_ledger`; it is not subtracted from the network
settlement in `settlement_handoff.net_cents`.

Use `./run.sh` from this repository:

```
./run.sh load       # full raw reload from every landed partition
./run.sh build      # nightly dbt selection; does not rebuild consumer-owned tables
./run.sh publish    # publish open days and authorized corrections
./run.sh cutoff     # append newly observed cutoff outcomes
./run.sh check      # dbt parse and the nightly dbt tests
```

The warehouse lives outside the git repository at
`../var/warehouse/warehouse.duckdb`; `run.sh` resolves that path automatically.
