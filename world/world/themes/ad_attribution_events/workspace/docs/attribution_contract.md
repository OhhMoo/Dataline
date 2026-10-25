# Attribution and partner billing contract

## Conversion identity

The S2S endpoint can deliver the same conversion more than once. `conversion_id`
is the business identity; `postback_id` identifies a delivery attempt. The
warehouse processes one conversion per `conversion_id`, choosing the earliest
`received_at` and then the lowest `postback_id` as a deterministic tie-break.

## Click eligibility and precedence

A conversion is attributed to the latest eligible click by the same `user_id`
at or before the conversion. The click must be inside the lookback configured
in `raw.attribution_rules`; the shipped rule is seven days. Ties are resolved by
the highest `click_id`.

Clicks matching an active IP-prefix or user-agent exclusion in
`raw.bot_exclusions` are not eligible. Impressions use the same exclusion feed
for traffic-quality reporting. Direct conversions and conversions without an
eligible click remain absent from the attributed fact rather than receiving a
synthetic partner.

## Payout

Partner terms in `raw.partners` define a revenue share in basis points. Each
attributed conversion calculates `floor(revenue_cents * revshare_bps / 10000)`.
The handoff sums those per-conversion payouts at event-date/partner grain.
Currency is USD and amounts never use floating-point arithmetic.

## Billing close

On a night whose newest attributed event date is D, dates through
`D - billing_lag_days` are locked. The shipped lag is one day. Open dates remain
in the handoff for partner visibility but do not have a `billing_lock` row.
Once locked, a partner/date amount is a close record and must not be reopened by
the routine nightly job. Any manual correction requires Partner Finance approval
and a separate adjustment rather than an edit to the lock.
