with ranked as (
  select *, row_number() over (
    partition by payment_id order by version desc, delivered_at desc, event_id desc
  ) as rn from {{ source('raw','payments_events') }}
), live as (
  select * from ranked where rn=1 and event_type <> 'void'
)
select payment_day as settlement_date, live.merchant_id, count(*) as payment_count,
       sum(amount_cents) as gross_cents,
       sum(cast(round(amount_cents * terms.interchange_bps / 10000.0) as bigint)) as interchange_cents,
       max(config.window_days) as window_days, max(config.cutoff_day) as cutoff_day
from live
join {{ source('raw','merchant_terms') }} as terms
  on terms.merchant_id = live.merchant_id
cross join {{ source('raw','settlement_config') }} as config
group by 1,2 order by 1,2
