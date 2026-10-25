# Synthetic 837/835 mapping

## Claim frequency

- `1` - original claim
- `7` - replacement; supersedes the prior version and may restate amounts
- `8` - void; supersedes the prior version and produces zero allowed/paid amounts

Raw files preserve all transmissions. Warehouse reporting uses the latest
version per `claim_id`, with received date, version, and control number providing
a deterministic precedence order.

## Coordination of benefits

An 837 with an other payer carries `other_payer_id` and the synthetic other-payer
payment. Its 835 reduces payer payment by the COB adjustment while preserving
member responsibility and the contractual adjustment. A void carries no COB
payment. Claims denied by this payer use denial reason `CO-97` and zero allowed,
member responsibility, COB adjustment, and payer payment.

## CMS windows

Service-month encounters open on the first day of the next month. dbt vars set
the cutoff day and UTC hour, plus the acceptance lag in calendar days. Eligibility
uses the current claim version's received date, not its service date. Voids are
excluded and counted separately; late receipts remain visible as late claims.

## Privacy

All rows are generated from a seed. Member IDs, claim IDs, control numbers,
provider names, and payer IDs are synthetic. No real patient demographics,
clinical notes, or production EDI content is included.
