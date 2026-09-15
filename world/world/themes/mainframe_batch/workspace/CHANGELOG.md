# CLAIMS BATCH — release history

Entries are by change request. Do not add an entry without one.

## 4.12.0 — CR-2026-014 — 2026-02-19
- Dependency pin review. No version changes; `requirements.txt` re-attested.
- `run.sh` `verify` target added so the record-length check can be run by an
  operator without running the load.

## 4.11.0 — CR-2025-203 — 2025-11-06
- Settle window between `nightly_extract` and `batch_load` raised from 30 to
  45 minutes after three consecutive short reads. `conf/claims.cfg`
  `[landing] settle_minutes` and `dags/claims_nightly.py` `SETTLE_MINUTES`
  both updated.

## 4.10.2 — CR-2025-158 — 2025-09-12
- `loader/load_extract.py` rejects a partition whose byte length is not a
  multiple of the record length. Previously it read as many whole records as
  it could and discarded the remainder.

## 4.10.0 — CR-2025-041 — 2025-04-02
- dbt project introduced for the staging and warehouse layers. The previous
  hand-written SQL in `sql/` is retired; the directory was removed.

## 4.9.0 — CR-2024-091 — 2024-10-15
- `copybooks/CLAIMS.CPY` brought into the repository as the interface
  contract. See docs/decisions/DR-0007-copybook-is-the-contract.md.

## 4.8.0 — CR-2024-077 — 2024-06-11
- Transmission moved from FTP to Connect:Direct. The `[ftp]` section in
  `conf/claims.cfg` is retained, commented, under records policy; the account
  it referenced was deleted at cutover.
