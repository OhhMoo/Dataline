with versions as (
    select
        remit_id,
        claim_id,
        cast(claim_version as integer) as claim_version,
        provider_id,
        cast(remit_date as date) as remit_date,
        adjudication_status,
        cast(allowed_amount as decimal(18, 2)) as allowed_amount,
        cast(payer_paid_amount as decimal(18, 2)) as payer_paid_amount,
        cast(member_responsibility_amount as decimal(18, 2)) as member_responsibility_amount,
        cast(contractual_adjustment_amount as decimal(18, 2)) as contractual_adjustment_amount,
        cast(cob_adjustment_amount as decimal(18, 2)) as cob_adjustment_amount,
        cast(other_payer_paid_amount as decimal(18, 2)) as other_payer_paid_amount,
        denial_reason_code,
        cast(dt as date) as landing_date,
        row_number() over (
            partition by claim_id
            order by remit_date desc, claim_version desc, remit_id desc
        ) as remit_rank
    from {{ source('raw', 'edi_835_remits') }}
)

select *
from versions
where remit_rank = 1
