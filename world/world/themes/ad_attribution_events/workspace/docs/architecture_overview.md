# Architecture overview

How a night's network export becomes attributed conversions and locked
partner billing, and which pieces are load-bearing when something disagrees.

## System at a glance

The ad network export for event date D lands at D+1 00:30 UTC. It lands as
three partitions, partitioned by event date. They are
`data/landing/impressions/dt=D/`, `data/landing/clicks/dt=D/`, and
`data/landing/conversions/dt=D/`. The S2S endpoint can deliver the same
conversion more than once. A postback volume above the unique conversion
volume is ordinary retry traffic. It is not a discrepancy. The pipeline
runs on the arrival calendar day, UTC:

1. `ingest_events` (01:00) rebuilds all six raw tables (the three event
   streams and the three control feeds) from every partition on disk. It
   is a full reload. Rerunning it also catches up an older missed
   delivery, because the loader reads every partition.
2. `attribute_conversions` (starts 10 minutes after the ingest) builds
   `warehouse.fct_attributed_conversions`. It also runs the tests:
   conversion uniqueness, the lookback boundary, and bot-click exclusion.
   A failed test fails the job. It leaves the fact holding the previous
   night's rows.
3. `lock_partner_billing` (starts 10 minutes after attribution) rebuilds
   the payout handoff and the billing lock together. Their reconciliation
   test must be green before Partner Finance consumes the handoff over
   SFTP.

The deployed job order is recorded in `orchestration/schedule.yml`. Treat
that file, not memory, as the schedule of record.

## DuckDB schema overview

One database file, two schemas.

`raw` mirrors the export and the control feeds, partitioned by `dt`:

| table | grain | source |
|---|---|---|
| `raw.impressions` | one row per `impression_id` | `data/landing/impressions/dt=D/` |
| `raw.clicks` | one row per `click_id`, linked to its impression | `data/landing/clicks/dt=D/` |
| `raw.conversions` | one row per S2S delivery (`postback_id`); `conversion_id` repeats across retries | `data/landing/conversions/dt=D/` |
| `raw.partners` | one row per partner, with `revshare_bps` commercial terms | control feed |
| `raw.bot_exclusions` | one row per active `ip_prefix` or `user_agent` exclusion | control feed |
| `raw.attribution_rules` | the `network_default` rule: `lookback_days` and `billing_lag_days` | control feed |

`warehouse` holds the attributed fact and the partner-facing surfaces:

| table | grain |
|---|---|
| `warehouse.fct_attributed_conversions` | one row per deduplicated `conversion_id` |
| `warehouse.partner_payout_handoff` | one row per conversion date per partner |
| `warehouse.billing_lock` | one row per locked partner per date |

Attribution is the last eligible click within the lookback configured in
`raw.attribution_rules` (the shipped rule is seven days). The highest
`click_id` breaks ties. Payout is `floor(revenue_cents * revshare_bps /
10000)` in USD integer cents. Clicks matching an active bot exclusion are
not eligible. Conversions without an eligible click stay absent from the
fact. They do not receive a synthetic partner. Staging views are
operational relations, not partner-facing APIs.

## Change control

The marts and the lock are reviewed artefacts. A locked partner/date
amount is a close record. The routine nightly job never reopens it. A
manual correction requires Partner Finance approval and a separate
adjustment. Before altering any pipeline parameter (lookback windows,
revshare or split treatments, lag offsets encoded in job code), confirm
the live state. Do not rely on intention:

- active tickets in `fixtures/workflow/issues.yaml` (a parked ticket is not
  an approved change),
- the daemon trails in `var/logs/ingest_daemon.log` and
  `var/logs/sftp_sync.log`,
- the corrections register `warehouse.factory_corrections`, which records
  which billing dates were adjusted, by whom, and under which ticket.

If those three disagree with what a document says, the document is stale,
not the state.

## Legacy migration notes

The attributed fact replaced the pre-cutover partner reporting extracts in
the 2024 cutover. Two conventions from that programme are still
load-bearing:

- The pre-cutover extracts applied their own split multipliers and fee
  treatments downstream of the warehouse. Anything of that sort still
  present in job code retires under one of two rules. A locked partner
  billing date waits for controller sign-off naming the date in
  `warehouse.factory_corrections`. A pre-cutover feed split multiplier or
  fee retention haircut retires on log evidence. Once
  `var/logs/ingest_daemon.log` records the cutover active, the feed
  delivers net `amount_cents` directly. The bridge then double-adjusts
  every affected row and must be removed. Do not delete or "fix" such
  logic on the strength of a comment alone. Do not assume it is active
  business policy either. Check `fixtures/workflow/issues.yaml` and the
  audit lines in `var/logs/` first. Confirm against
  `warehouse.factory_corrections`.
- Partner and reseller splits were standardised at cutover to be computed
  from attributed revenue in the marts, never multiplied into raw postback
  rows. Any deviation between `warehouse.partner_payout_handoff` and the
  fact is evidence to reconcile, not to copy forward.

Historical cutover review notes live with the decision records under
`docs/`. When a discrepancy smells like a cutover leftover, read those
before touching the pipeline.
