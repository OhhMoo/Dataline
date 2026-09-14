{{ config(materialized='table', schema='staging') }}

-- Customer dimension, flattened from the vendor's customer master.
--
-- One row in, one row out. The master is loaded as a full replace each night so
-- there is nothing to deduplicate here.

select
    customer_id,
    region,
    customer_segment
from {{ source('raw', 'customer_master') }}
