-- Every not-yet-closed handoff row must equal the current payment fold.
with current_payments as (
    select payment_day, merchant_id, count(*) as payments, sum(amount_cents) as net_cents
    from {{ ref('fct_payments') }}
    group by payment_day, merchant_id
), closed_days as (
    select distinct settlement_day
    from {{ ref('clearing_window') }}
    where status = 'SETTLED'
), mismatches as (
    select h.settlement_day, h.merchant_id
    from {{ ref('settlement_handoff') }} as h
    join current_payments as p
      on p.payment_day = h.settlement_day and p.merchant_id = h.merchant_id
    left join closed_days as c on c.settlement_day = h.settlement_day
    where c.settlement_day is null
      and (h.payments <> p.payments or h.net_cents <> p.net_cents)
)
select * from mismatches
