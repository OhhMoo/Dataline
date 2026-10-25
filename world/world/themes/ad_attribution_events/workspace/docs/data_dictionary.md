# Data dictionary

## Raw event tables

`raw.impressions` has one row per ad rendering, keyed by `impression_id`.
`raw.clicks` has one row per click, keyed by `click_id` and linked to its
impression. `raw.conversions` has one row per S2S delivery, keyed by
`postback_id`; `conversion_id` can repeat across retries. Event dates are also
available as the landing partition column `dt`.

## Control tables

- `raw.partners`: partner identity and `revshare_bps` commercial terms.
- `raw.bot_exclusions`: active `ip_prefix` and `user_agent` exclusion rules.
- `raw.attribution_rules`: `lookback_days` and `billing_lag_days` for the
  `network_default` rule.

## Warehouse tables

`fct_attributed_conversions` carries conversion and winning-click timestamps,
partner/campaign identifiers, lookback, revenue cents, and payout cents.
`partner_payout_handoff` aggregates those amounts by conversion date and
partner and adds `billing_cutoff_date` plus `billing_status`. `billing_lock`
contains only locked handoff rows and their deterministic `locked_at` time.

Staging views add `is_bot` to traffic and reduce conversion postbacks to one
row per conversion. They are operational relations, not partner-facing APIs.
