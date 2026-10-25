# Nightly billing and close runbook

All schedule times are UTC. A source day lands at 00:00 on the following day.

| Stage | Start | Result |
|---|---:|---|
| `ingest_webhooks` | 01:00 | Rebuild both raw webhook tables in deterministic event order |
| `fold_subscriptions` | 01:10 | Build staging views, daily MRR facts, and current account dimension |
| `revrec_window_close` | 01:20 | Build and publish the account-period close ledger and Finance handoff |

## Operator checks

1. Confirm all three scheduler runs succeeded for the same execution date.
2. Compare the maximum raw `source_date` with the execution date.
3. Check `fct_mrr_daily` for one row per account and metric date.
4. Reconcile signed credit notes to `proration_credit_cents` in the open close window.
5. Review the Finance ARR dashboard and the ASC-606 close ledger only after the close stage completes.

The raw loader is a full rebuild. To replay a source day, restore its two landing partitions and rerun all three stages in order. Do not update warehouse tables manually.
