# Data dictionary

## Raw feeds

### raw.subscription_events

One full subscription snapshot per event. `occurred_at` is when Billing emitted the event; `effective_date` is when the contract change takes effect. The feed includes starts, seat upgrades, seat downgrades, and churn. `seats`, `plan_code`, `unit_price_cents`, and `revrec_days` are repeated on every event so each row can be folded without a price lookup.

### raw.invoice_webhooks

Finalized recurring invoices, proration charges, and credit notes. `amount_cents` is signed: charges are positive and credits are negative. Service-period end dates are exclusive. `invoice_date` is the posting date and can be later than a backdated service-period start.

## Warehouse models

### warehouse.fct_mrr_daily

Grain: `metric_date, account_id`.

The model selects the latest event effective on each date, with `occurred_at` and `event_id` as deterministic tie-breakers. Active MRR is `seats * unit_price_cents`; ARR is twelve times MRR. Churn sets seats and recurring metrics to zero from its effective date. `recognized_revenue_cents` is the daily straight-line amount over `revrec_days`.

### warehouse.dim_account

Grain: `account_id`. Latest plan, seat, status, MRR, and ARR attributes for Finance and customer-success consumers.

### warehouse.revrec_close_window

Grain: `period, account_id` (`close_id` is their stable concatenation).

- `billed_cents`: positive finalized invoice and proration amounts posted in the period.
- `proration_credit_cents`: signed credit-note subset of billing.
- `net_billed_cents`: all signed finalized billing amounts.
- `recognized_revenue_cents`: sum of daily recognition in the window.
- `deferred_revenue_cents`: cumulative net billed less cumulative recognized revenue for the account.
- `close_status`: `closed` before the latest materialized period; otherwise `open`.
