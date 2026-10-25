with recognition as (
  select
    strftime(metric_date, '%Y-%m') as period,
    account_id,
    account_name,
    min(metric_date) as window_start,
    max(metric_date) as window_end,
    count(*) as recognition_days,
    sum(recognized_revenue_cents) as recognized_revenue_cents
  from {{ ref('fct_mrr_daily') }}
  group by 1, 2, 3
),
billing as (
  select
    strftime(invoice_date, '%Y-%m') as period,
    account_id,
    sum(case when amount_cents > 0 then amount_cents else 0 end) as billed_cents,
    sum(case when invoice_type = 'proration_credit' then amount_cents else 0 end) as proration_credit_cents,
    sum(amount_cents) as net_billed_cents
  from {{ ref('stg_invoice_webhooks') }}
  group by 1, 2
),
current_window as (
  select strftime(max(metric_date), '%Y-%m') as open_period
  from {{ ref('fct_mrr_daily') }}
),
ledger as (
  select
    r.period || ':' || r.account_id as close_id,
    r.period,
    r.account_id,
    r.account_name,
    r.window_start,
    r.window_end,
    case when r.period < c.open_period then 'closed' else 'open' end as close_status,
    r.recognition_days,
    coalesce(b.billed_cents, 0) as billed_cents,
    coalesce(b.proration_credit_cents, 0) as proration_credit_cents,
    coalesce(b.net_billed_cents, 0) as net_billed_cents,
    r.recognized_revenue_cents
  from recognition r
  cross join current_window c
  left join billing b
    on b.period = r.period and b.account_id = r.account_id
)
select
  close_id,
  period,
  account_id,
  account_name,
  window_start,
  window_end,
  close_status,
  recognition_days,
  billed_cents,
  proration_credit_cents,
  net_billed_cents,
  recognized_revenue_cents,
  sum(net_billed_cents - recognized_revenue_cents) over (
    partition by account_id
    order by period
    rows between unbounded preceding and current row
  ) as deferred_revenue_cents
from ledger
order by period, account_id
