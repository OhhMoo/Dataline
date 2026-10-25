# Architecture overview

How one nightly mainframe extract becomes the claims-day number, and which
pieces are load-bearing when something disagrees.

## System at a glance

PolicyMaster cuts one extract per night:
`data/landing/claims_extract/dt=<night>/CLAIMS.DAT`, a fixed-block EBCDIC
file (code page 037) with no record delimiters, 31-byte records per
`copybooks/CLAIMS.CPY`. The extract for claim day D is cut at 23:00 UTC on
D itself, as the batch window opens (tonight's file, not yesterday's). The
pipeline runs inside the same window, UTC:

1. `nightly_extract` (23:00) verifies the delivery rather than creating
   it: one `CLAIMS.DAT` for tonight, or the job fails and the window opens
   without input.
2. `batch_load` (23:45) runs `loader/load_extract.py`, unpacking records
   per the copybook into `raw.claims_extract`. A partition whose byte
   length is not a multiple of the record length is rejected, not
   partially loaded. `run.sh verify` checks before the load step, and the
   loader checks again.
3. `dbt_build` (23:55) rebuilds staging and
   `warehouse.fct_claims_daily` and runs the data tests.

The dashboard `claims_daily` reads the mart. The deployed job order is
recorded in `orchestration/schedule.yml`; treat that file, not memory, as
the schedule of record. A load that overruns its nominal two minutes can
still be recorded as running when the build starts, with overlapping
windows in the run history, staged, never raced.

## DuckDB schema overview

One database file, two schemas that matter here.

`raw` mirrors the landing tree, one row per unpacked record:

| table | grain | source partition |
|---|---|---|
| `raw.claims_extract` | one row per claim record per `dt` | `data/landing/claims_extract/dt=D/CLAIMS.DAT` |

`CLAIM-AMT` is packed decimal (two implied places, stored as cents) and
arrives in `raw` as `claim_amt` decimal(9,2); `claim_date` is still the
as-landed YYYYMMDD string at this layer. `warehouse` is the mart the
dashboard reads:

| table | grain |
|---|---|
| `warehouse.fct_claims_daily` | one row per `claim_date` (`claims`, `total_amount`) |

`staging.stg_claims` is the view that does the remaining typing
(`claim_date` date, `claim_amt` decimal(11,2)). Amounts are summed in the
mart from unpacked cents; no rounding happens at `raw`.

## Change control

This queue runs under ITSM change control. An incident is raised, assigned,
and worked. It then waits on a change record before anybody touches
production. In `fixtures/workflow/issues.yaml`, `pending_change` is a
waiting state, not an approval. The copybook is the interface contract
(decision record DR-0007, raised under change record CR-2024-091). An
extract change is a change against both sides. Neither side deploys before
the other has merged. Before altering any pipeline parameter (window
offsets, the record length, or any rate or split convention encoded in job
code), confirm the live state rather than the intention:

- active tickets in `fixtures/workflow/issues.yaml`,
- the daemon trails in `var/logs/ingest_daemon.log` and
  `var/logs/sftp_sync.log`,
- the corrections register `warehouse.factory_corrections`, which records
  which claim days were reopened, by whom, and under which ticket.

If those three disagree with what a document says, the document is stale,
not the state.

## Legacy migration notes

Claims reporting ran on host-side summaries until the 2024 batch
modernisation review, with treaty splits and fee treatments applied
downstream by the finance extracts that consumed them. The review
standardised the conventions still load-bearing today:

- Reinsurance and partner splits are computed from
  `warehouse.fct_claims_daily`, never packed into raw claim amounts. A
  deviation between the mart and a decimal hand-sum of
  `raw.claims_extract` is evidence to reconcile, not to copy forward.
- The record length lives in the copybook and in `conf/claims.cfg`
  `[source] record_length`, and the duplication is a known defect carried
  since the review. An operator who changes one side and not the other
  gets a `verify` that passes and a load that raises, safe in that
  direction, unsafe in reverse. Do not "fix" the duplication on the
  strength of a comment alone; it stays until a change record retires it.
- Anything else in job code that smells like a host-era convention
  (window padding, amount treatments) is a legacy placeholder until
  tickets and logs confirm its status. Check
  `fixtures/workflow/issues.yaml` and the audit lines in `var/logs/`
  first.

Retirement itself follows two rules, not one. A closed claim day waits
for a controller sign-off naming that day in
`warehouse.factory_corrections`. Pre-cutover treaty split multipliers or
fee retention haircuts in the fold layer retire on log evidence, not a
change record. They retire when `var/logs/ingest_daemon.log` records the
partner extract cutover delivering net `amount_cents` directly. Leaving
them in past that point double-adjusts every affected row.

Cutover review notes live with the decision records under
`docs/decisions/`; read DR-0007 before touching the pipeline when a
discrepancy looks like a leftover.
