select
    review_id,
    merchant_id,
    legal_name,
    merchant_category,
    country,
    cast(reviewed_at as timestamp) as reviewed_at,
    cast(reviewed_at as date) as review_date,
    review_type,
    lower(kyc_status) as kyc_status,
    lower(risk_tier) as risk_tier,
    reviewer_id,
    cast(next_review_date as date) as next_review_date,
    dt as landed_on
from {{ source('raw', 'kyc_reviews') }}
