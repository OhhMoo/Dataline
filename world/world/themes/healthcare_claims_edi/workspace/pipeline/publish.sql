create or replace table warehouse.actuarial_reserve_handoff as
select service_date, provider_id, count(*) as claims, sum(reserve_cents) as reserve_cents
from warehouse.fct_adjudicated_claims group by 1,2 order by 1,2;
create or replace table warehouse.cms_submission_window as
select service_date,
       date_diff('day', service_date, (select max(dt) from raw.edi_837_claims)) >= 5 as closed,
       count(*) as submitted_claims
from warehouse.fct_adjudicated_claims group by 1 order by 1;
