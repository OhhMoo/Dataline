-- refresh_finance_pack: the monthly commercial pack, read by Finance.
--
-- Scheduled 06:45 UTC daily, after the nightly load and build. Rebuilds every
-- OPEN month of finance.aov_monthly_pack from warehouse.kpi_daily. CLOSED months
-- are the figures Finance issued to the board and are left exactly as issued;
-- restating one needs FP&A sign-off (kb: finance/monthly-pack).
--
-- Runs under the scheduler's aliased connection (ATTACH ... AS wh; USE wh), so
-- schema.table names below resolve inside the one warehouse file.

DELETE FROM finance.aov_monthly_pack WHERE period_status = 'open';

INSERT INTO finance.aov_monthly_pack
SELECT date_trunc('month', dt)::DATE                          AS month,
       SUM(order_count)::BIGINT                               AS order_count,
       SUM(revenue)::DECIMAL(14,2)                            AS revenue,
       ROUND(SUM(revenue) / SUM(order_count), 2)::DECIMAL(10,2) AS aov,
       'open'                                                 AS period_status,
       NULL::DATE                                             AS issued_on
FROM warehouse.kpi_daily
WHERE date_trunc('month', dt)::DATE NOT IN
      (SELECT month FROM finance.aov_monthly_pack WHERE period_status = 'closed')
GROUP BY 1;
