create or replace table warehouse.treasury_reserve_handoff as
select e.merchant_id, e.event_date, e.disputed_cents,
       coalesce(h.hold_cents,0) as held_cents,
       e.disputes_30d::double / nullif(e.transactions_30d,0) as chargeback_ratio,
       e.risk_officer_approved,
       case when e.risk_officer_approved and e.disputes_30d::double/nullif(e.transactions_30d,0)<0.01
            then coalesce(h.hold_cents,0) else 0 end as releasable_cents
from warehouse.fct_merchant_exposure e
left join raw.payout_holds h on e.merchant_id=h.merchant_id and e.event_date=h.hold_date
order by 1,2;
create or replace table warehouse.risk_committee_signoff as
select merchant_id, bool_and(risk_officer_approved) as approved,
       max(chargeback_ratio) as max_chargeback_ratio
from warehouse.treasury_reserve_handoff group by 1 order by 1;
