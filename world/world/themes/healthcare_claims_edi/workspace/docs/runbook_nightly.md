# Nightly claims EDI runbook

## Schedule and outputs

| job | schedule | output |
|---|---|---|
| `nightly_edi_load` | 01:10 UTC | three `raw` tables rebuilt from landed CSV |
| `adjudication_build` | 10 minutes after load | current claim fact and provider dimension |
| `settlement_handoffs` | 15 minutes after adjudication | reserve and capitation handoffs |
| `cms_window_close` | 10 minutes after handoffs | CMS encounter cutoff windows |

The arrival is due at 00:30 UTC. Every scheduled step executes the same loader
or dbt project available in this workspace.

## Routine checks

1. Inspect the first non-success run in scheduler history.
2. Compare raw 837 and 835 counts by `dt`; every claim version needs a remit.
3. Check `warehouse.fct_adjudicated_claims` for one row per `claim_id`.
4. Reconcile COB and financial-balance dbt tests before using downstream tables.
5. Check the data-through date on the reserve handoff and the acceptance as-of
   date on the CMS window before distributing either output.

## Corrected deliveries

Replace the CSV in its existing `dt=` partition and rerun the nightly chain in
DAG order. The raw loader uses `CREATE OR REPLACE TABLE` over every partition,
so no row-level deletion or manual deduplication is needed. Staging selects the
latest claim version by received date, version number, and control number.

A raw load by itself does not refresh any warehouse consumer. Conversely, a dbt
rerun cannot repair a missing or malformed landing file.

## CMS and provider distribution

Do not treat late claims as eligible for a CMS window to make counts agree.
Late, denied, and void-excluded claims are separate measures. Capitation is a
PMPM contract settlement less its quality withhold; fee-for-service payments
and actuarial reserves are not subtracted from it.
