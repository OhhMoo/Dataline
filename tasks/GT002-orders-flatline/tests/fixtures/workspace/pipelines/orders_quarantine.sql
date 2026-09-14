-- orders_stream_quarantine :: task 2 of 4, orders DAG, every 30 minutes
--
-- Routes records that break the schema contract into the dead-letter table so
-- that one bad producer message cannot hold the whole topic up. Safe to re-run:
-- a record already dead-lettered is not dead-lettered a second time.
INSERT INTO raw.orders_dlq
SELECT t.offset_id,
       t.event_ts,
       t.payload,
       'schema: required field order_id is null',
       now()
FROM raw.orders_topic t
WHERE t.offset_id >= (SELECT committed_offset FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND t.offset_id <  (SELECT committed_offset + 2000 FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND json_extract_string(t.payload, '$.order_id') IS NULL
  AND NOT EXISTS (SELECT 1 FROM raw.orders_dlq d WHERE d.offset_id = t.offset_id);
