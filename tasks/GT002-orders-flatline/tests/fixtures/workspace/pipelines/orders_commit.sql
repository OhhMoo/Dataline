-- orders_stream_commit :: task 4 of 4, orders DAG, every 30 minutes
--
-- Advances the consumer group past the window the sink just processed.
UPDATE raw.consumer_offsets
SET committed_offset = LEAST(committed_offset + 2000,
                             (SELECT log_end_offset FROM raw.topic_meta
                               WHERE topic = 'orders.v2')),
    committed_at = now()
WHERE consumer_group = 'orders_fct_sink';
