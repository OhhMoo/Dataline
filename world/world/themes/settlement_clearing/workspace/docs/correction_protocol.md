# Correction protocol - closed settlement days

A settlement day is closed when `warehouse.clearing_window` contains a `SETTLED`
row for it. Corrections are not rolled into another payment day.

A closed day may be republished only after all of these are true:

1. A clearing controller has approved a tracker ticket naming that settlement day.
2. `data/control/settlement_corrections.csv` contains the day, ticket key,
   approver, and approval date, and the nightly raw/dbt jobs have loaded it into
   `warehouse.settlement_corrections`.
3. Every republished merchant row for the day carries that ticket key in
   `correction_ref`.

Without the register entry, the publisher leaves the closed handoff unchanged and
reports the day as withheld. An approval covers one settlement day, not unrelated
days and not a licence to bypass version precedence.

A day never previously handed to the bank is not a closed-day restatement. Publish
it late with a null `correction_ref`; its existing `SLA_BREACH` history remains.
