# Nightly attribution runbook

1. Check the 00:30 export and the `ingest_events` run. The loader reads every
   partition, so rerunning it also catches up an older missed delivery.
2. Check `attribute_conversions`. dbt tests cover conversion uniqueness, the
   lookback boundary, and bot-click exclusion.
3. Check `lock_partner_billing`. The handoff and lock are rebuilt together;
   their reconciliation test must be green before Partner Finance consumes the
   handoff.
4. Review `partner_attribution` for volume, payout, and lock coverage. A raw
   postback count above the unique conversion count is normal S2S retry traffic,
   not a payout discrepancy.

Investigate the first failed run rather than only the latest run. Do not modify
a locked partner/date row to resolve a source discrepancy; use the adjustment
process in the attribution contract.
