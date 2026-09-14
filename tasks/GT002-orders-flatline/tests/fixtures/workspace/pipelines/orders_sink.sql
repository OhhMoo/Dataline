-- orders_stream_sink :: task 3 of 4, orders DAG, every 30 minutes
--
-- Loads one poll window of orders.v2 into the orders fact table. Takes no
-- parameters: the window is read from raw.consumer_offsets, so this file is the
-- same on a scheduled run and on a manual catch-up.
INSERT INTO warehouse.fct_orders
SELECT CAST(json_extract_string(t.payload, '$.order_id')    AS BIGINT)        AS order_id,
       CAST(json_extract_string(t.payload, '$.revision')    AS INTEGER)       AS revision,
       CAST(json_extract_string(t.payload, '$.order_dt')    AS DATE)          AS order_dt,
       CAST(json_extract_string(t.payload, '$.event_ts')    AS TIMESTAMP)     AS event_ts,
       CAST(json_extract_string(t.payload, '$.customer_id') AS BIGINT)        AS customer_id,
            json_extract_string(t.payload, '$.channel_code')                  AS channel_code,
            json_extract_string(t.payload, '$.sku')                           AS sku,
       CAST(json_extract_string(t.payload, '$.qty')         AS INTEGER)       AS qty,
       CAST(json_extract_string(t.payload, '$.unit_price')  AS DECIMAL(10,2)) AS unit_price,
       ROUND(CAST(json_extract_string(t.payload, '$.qty')        AS INTEGER)
           * CAST(json_extract_string(t.payload, '$.unit_price') AS DECIMAL(10,2)), 2)
                                                            ::DECIMAL(12,2)   AS order_amount,
       now()                                                                  AS load_ts,
       'stream-' || strftime(t.event_ts, '%Y%m%d-%H')                         AS batch_id
FROM raw.orders_topic t
WHERE t.offset_id >= (SELECT committed_offset FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND t.offset_id <  (SELECT committed_offset + 2000 FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND json_extract_string(t.payload, '$.order_id') IS NOT NULL;
