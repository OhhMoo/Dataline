# Data dictionary

- `raw.claims_extract` — as-landed claim rows, unpacked by
  `loader/load_extract.py` from the nightly mainframe extract
  (`data/landing/claims_extract/dt=*/CLAIMS.DAT`). The extract is a
  fixed-block EBCDIC (code page 037) file with no record delimiters; the
  record layout is the copybook `copybooks/CLAIMS.CPY`. `CLAIM-AMT` is
  `PIC S9(7)V99 COMP-3` (packed decimal, 5 bytes, stored as cents) and
  arrives in `raw` as `claim_amt` decimal(9,2); `claim_date` is still the
  as-landed YYYYMMDD string here.
- `staging.stg_claims` — typed claim rows (`claim_date` date,
  `claim_amt` decimal(11,2)).
- `warehouse.fct_claims_daily` — one row per `claim_date`: `claims`,
  `total_amount`.

Dashboard `claims_daily` reads `warehouse.fct_claims_daily`.
