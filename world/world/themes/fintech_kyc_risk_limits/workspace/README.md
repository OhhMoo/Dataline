# Merchant disputes and reserve risk

Represented disputes can finish won or lost. Reserve release requires a risk officer approval and a chargeback ratio below the policy threshold. A payout request alone does not authorize release.

Landing files: data/landing/<feed>/dt=YYYY-MM-DD/events.csv. Raw feeds: raw.dispute_events, raw.kyc_reviews, raw.payout_holds. Daily fact: warehouse.fct_merchant_exposure. Publication: warehouse.treasury_reserve_handoff.

The scheduler ingests, builds the dbt fact, and runs pipeline/publish.sql in that order.
