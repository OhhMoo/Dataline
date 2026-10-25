# Settlement feed - published source contract v3

Owner: card network integrations. The feed and its version rules are the source
of truth; the loaders implement this contract.

## Delivery and checkpoint

One partition lands per calendar day under `data/payments_feed/dt=<day>/`. It may
contain `events.csv` and recovery files. A recovery file can arrive in a later
partition and can repeat an event id or an older payment version.

`payment_day` is the day the payment settles on. It does not move when a later
partition corrects the payment. `warehouse.load_checkpoint.through_day` is the
newest partition actually read from disk, not the scheduler's current date.

## Event identity and version precedence

An `event_id` identifies one delivered event version; repeated deliveries of that
id are one event. Each `payment_id` is held at its highest delivered `version`.
Ties are resolved by `delivered_at`, then `event_id`, both descending. Delivery
time never makes a lower version supersede a higher one.

`settle` states the payment amount in integer cents. A highest-version `void`
withdraws the payment, so it has no row in `warehouse.fct_payments`. Replaying an
older version changes nothing.

## Settlement handoff

For each settlement day and merchant, the bank receives the count and sum of
`amount_cents` over current non-void payments. Its table layout is frozen:

```sql
CREATE TABLE warehouse.settlement_handoff (
    settlement_day DATE, merchant_id VARCHAR, payments BIGINT,
    net_cents BIGINT, correction_ref VARCHAR);
```

Ordinary rows have a null `correction_ref`. Open days may be republished as new
current events arrive. Once the bank has a `SETTLED` cutoff row, the day is closed
and `docs/correction_protocol.md` governs every change to its handoff.

Interchange does not alter that network settlement. Finance reconciles the fee in
`warehouse.interchange_ledger`: for each merchant and day, `interchange_cents` is
integer division of `gross_cents * interchange_bps` by 10,000. No floating-point
money or rounded-up fee is permitted.

## Clearing cutoff

The clearing bank cutoff is 23:30 UTC. The platform appends to
`warehouse.clearing_window`; it never updates a prior row. A day with a `SETTLED`
row is closed. A day observed in the feed with nothing in the handoff receives an
`SLA_BREACH` row. Later publication can add a `SETTLED` row but cannot erase the
breach. Each run examines the configured trailing `window_days`; already settled
days are immutable and are skipped.
