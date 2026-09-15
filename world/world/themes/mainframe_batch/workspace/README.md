# CLAIMS BATCH

Nightly claims extract from POLICYMASTER, landed on the distributed side,
loaded into the warehouse, and aggregated for the `claims_daily` dashboard.

## Contents

- `copybooks/CLAIMS.CPY` — record layout for the extract. This is the
  interface contract with the system of record; see
  `docs/decisions/DR-0007-copybook-is-the-contract.md`.
- `jcl/CLMEXTR.jcl` — reference copy of the host job that cuts the extract.
  Held for reading only; the authoritative member is in PROCLIB.
- `loader/load_extract.py` — landing -> `raw.claims_extract`.
- `dbt_project/` — staging view and `fct_claims_daily`.
- `dags/claims_nightly.py` — the scheduled pipeline. Read `dags/README.md`.
- `conf/claims.cfg` — site configuration, under change control.
- `conf/profiles.yml` — dbt targets.
- `data/landing/claims_extract/dt=*/CLAIMS.DAT` — transmitted partitions.
- `docs/` — data dictionary, runbook, decision records.
- `run.sh` — operator task runner. Every runbook step is a target.

## Operating

```sh
./run.sh env       # report resolved configuration
./run.sh verify    # presence and record-length check over the landing zone
./run.sh load      # batch load step
./run.sh build     # dbt run and test
```

## Batch window

`nightly_extract` at 23:00 UTC, `batch_load` 45 minutes later,
`dbt_build` 10 minutes after that. The settle window exists because
Connect:Direct reports a transfer complete before the file is closed on the
receiving side.

## Notes for new staff

The warehouse is a DuckDB file held outside the repository at
`../var/warehouse/warehouse.duckdb`. If you open it yourself, attach it under
an alias — the file stem and one of the schemas inside it are both called
`warehouse`, and a two-part reference is ambiguous otherwise.

The `sql/` directory referenced by the older operations documents on the share
was removed at CR-2025-041 when the dbt project replaced it. Those documents
have not been withdrawn.
