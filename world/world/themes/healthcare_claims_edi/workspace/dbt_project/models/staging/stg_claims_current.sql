with versions as (
    select
        claim_id,
        cast(claim_version as integer) as claim_version,
        frequency_code,
        transaction_control_number,
        provider_id,
        member_id,
        cast(service_date as date) as service_date,
        cast(received_date as date) as received_date,
        service_code,
        diagnosis_code,
        cast(billed_amount as decimal(18, 2)) as billed_amount,
        other_payer_id,
        cast(other_payer_paid_amount as decimal(18, 2)) as other_payer_paid_amount,
        cast(dt as date) as landing_date,
        row_number() over (
            partition by claim_id
            order by received_date desc, claim_version desc, transaction_control_number desc
        ) as version_rank
    from {{ source('raw', 'edi_837_claims') }}
)

select *
from versions
where version_rank = 1
