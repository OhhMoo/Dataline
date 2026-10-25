select
    provider_id,
    provider_name,
    specialty,
    network_tier,
    capitated_members,
    capitation_pmpm,
    contract_start_date,
    roster_date
from {{ ref('stg_provider_roster') }}
order by provider_id
