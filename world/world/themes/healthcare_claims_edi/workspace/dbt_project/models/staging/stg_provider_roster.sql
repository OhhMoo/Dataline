with snapshots as (
    select
        provider_id,
        provider_name,
        specialty,
        network_tier,
        cast(capitated_members as integer) as capitated_members,
        cast(capitation_pmpm as decimal(12, 2)) as capitation_pmpm,
        cast(contract_start_date as date) as contract_start_date,
        cast(dt as date) as roster_date,
        row_number() over (partition by provider_id order by dt desc) as snapshot_rank
    from {{ source('raw', 'provider_roster') }}
)

select *
from snapshots
where snapshot_rank = 1
