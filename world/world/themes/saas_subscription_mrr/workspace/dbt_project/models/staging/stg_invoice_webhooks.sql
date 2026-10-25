{{ config(tags=['fold_subscriptions']) }}

select
  webhook_id,
  invoice_id,
  subscription_id,
  account_id,
  webhook_type,
  invoice_type,
  status,
  cast(invoice_date as date) as invoice_date,
  cast(service_period_start as date) as service_period_start,
  cast(service_period_end as date) as service_period_end,
  cast(amount_cents as bigint) as amount_cents,
  currency,
  cast(source_date as date) as source_date
from {{ source('raw', 'invoice_webhooks') }}
where status = 'finalized'
