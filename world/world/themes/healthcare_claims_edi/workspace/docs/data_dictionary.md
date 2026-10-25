# Claims data dictionary

## Raw layer

`raw.edi_837_claims` stores one row per transmitted claim version. Its grain is
`claim_id`, `claim_version`, and landing partition `dt`. Important fields are
`frequency_code`, `transaction_control_number`, synthetic `member_id`,
`provider_id`, service and received dates, billed amount, and other-payer fields.
Money remains text in raw and is cast in staging.

`raw.edi_835_remits` stores the remittance paired with each 837 version. It
carries adjudication status, allowed and payer-paid amounts, member
responsibility, contractual and COB adjustments, other-payer payment, and an
optional denial reason.

`raw.provider_roster` stores synthetic practice names, specialty, network tier,
capitated member count, monthly PMPM, and contract start date. It has no patient
or subscriber attributes.

## Warehouse layer

`warehouse.fct_adjudicated_claims` has one current row per `claim_id`. The
latest 837 joins to the 835 with the same claim version. `claim_status` is paid,
adjusted, denied, or voided. `cob_flag` identifies a non-void claim carrying a
coordination-of-benefits payer.

`warehouse.dim_provider` is the current provider contract snapshot and the
source of capitation terms.

`warehouse.actuarial_reserve_handoff` has one row per service date. Incurred
amount is the sum of allowed amounts on non-void claims. IBNR starts at the
configured basis-point rate of incurred amount and declines linearly to zero at
the configured reserve window. `data_through_date` is the maximum received date.

`warehouse.provider_capitation_settlement` has one row per service month and
provider. Gross settlement is `member_months * capitation_pmpm`; net settlement
subtracts `quality_withhold_amount`. `payment_date` is the configured day of the
following month.

`warehouse.cms_submission_window` has one row per service month. It defines the
next-month opening date, cutoff timestamp, and acceptance as-of date, then
classifies current claims as eligible, late, denied in window, or void-excluded.
`eligible_payer_paid_amount` sums only eligible non-void claims.

## Financial identity

For every current claim:

```text
billed = payer_paid + member_responsibility + contractual_adjustment + cob_adjustment
other_payer_paid = cob_adjustment
```

The other-payer payment is therefore not added again on the first line.
