# Reconciliation SLA

The service levels the batch window is held to, the reconciliations that
prove them, and the history that explains why the rules are the way they
are. All times UTC.

## SLA windows

| milestone | due | measured against |
|---|---|---|
| extract landed for claim day D | 23:05 on D | `data/landing/claims_extract/dt=D/CLAIMS.DAT` present |
| `nightly_extract` green | 23:10 | job green in `sched_state` |
| `batch_load` complete | 00:15 on D+1 | job green, `raw.claims_extract` carries D |
| `dbt_build` complete, tests green | 00:30 | job green, `warehouse.fct_claims_daily` carries D |
| dashboard `claims_daily` current | 06:30 | spot check `total_amount` for D |

The extract is cut at 23:00 as the window opens. The load starts at 23:45,
and the build follows ten minutes after. The slack between 00:30 and 06:30
is what an overrun or a rerun eats. An abend at the window boundary leaves
nothing written and is rerun, not repaired. A breach of the 23:05 landing
SLA is the host's to explain; a breach of anything downstream of it with a
healthy extract is ours.

## Nightly reconciliation duties

Before the business day opens, verify in this order:

1. Byte-length check: `CLAIMS.DAT`'s length must be a multiple of the
   31-byte record length in `copybooks/CLAIMS.CPY`. A short read is a
   transmission problem: check the extract transfer before rerunning
   `batch_load`, and never round a partial record into the load.
2. Row counts: `raw.claims_extract` for D against the record count implied
   by the byte length, then `warehouse.fct_claims_daily` for D against
   `staging.stg_claims`.
3. Amount parity: `total_amount` for D against a decimal sum of
   `claim_amt` (packed-decimal cents unpacked, no rounding at `raw`).
4. Corrections: any reopened claim day appears in
   `warehouse.factory_corrections` with a sign-off and a change record; an
   unregistered change to a closed day is withheld from downstream
   consumers until it is registered.

Evidence for every step lives in `var/logs/ingest_daemon.log` and
`var/logs/sftp_sync.log`; quote timestamps from those logs when raising or
closing an incident, and link the ticket in
`fixtures/workflow/issues.yaml`.

## Partner onboarding history

- The distributed claims feed was onboarded in 2023, when
  `loader/load_extract.py` replaced host-side reporting. The
  land-then-verify contract (the extract is cut as the window opens and
  is verified before the load) dates from then.
- Three times between 2022 and 2024, the extract changed on the host.
  The distributed side was told afterwards. Each failure was silent for at
  least one night. A fixed-block reader given the wrong record length
  produces rows instead of stopping. Decision record DR-0007, raised under
  change record CR-2024-091 in 2024, made the copybook the contract.
- Reinsurance partners take their bordereaux downstream of the mart.
  Treaty splits were standardised at the 2024 review to be computed from
  `warehouse.fct_claims_daily`, never packed into raw claim amounts, a
  convention that predates the review only in the host-era extracts it
  retired.

## Legacy and provisional rules

Two kinds of leftover logic live in this pipeline, and they retire under
two different rules; confusing them is how days get reopened or amounts
get double-counted.

Closed-window restatements are governance. Once a claim day is closed, its
published snapshot changes only in one case. `warehouse.factory_corrections`
must carry a controller sign-off naming that day. It must also carry the
tracker ticket authorising the restatement. The ticket is a change record
in `fixtures/workflow/issues.yaml`. There, `pending_change` is a waiting
state, not an approval. A draft or unsigned entry never reopens a closed
day, and an unregistered change stays withheld from downstream consumers.

Pre-cutover feed adjustments are engineering. While the host extract
delivered gross, pre-split, pre-fee amounts, the fold layer applied
temporary bridge adjustments (treaty split multipliers and retention
haircuts) so the warehouse saw net figures. Those adjustments retire on
log evidence, not on change records. Once `var/logs/ingest_daemon.log`
records that the partner extract cutover is active, the feed now delivers
net `amount_cents` directly. Any multiplier or haircut left in the fold
script double-adjusts every affected row. It must be removed so
`warehouse.fct_claims_daily` matches the landed net cents. Before changing
a rate, check `var/logs/ingest_daemon.log` (and `var/logs/sftp_sync.log`
for the delivery) for the cutover record; afterwards, reconcile the
affected claim day against the raw landed rows unpacked per the copybook.

Everything else provisional is still live configuration. That covers the
45-minute gap from verification to load and the ten-minute gap to the
build. It also covers the duplicated record length in `conf/claims.cfg`.
Changing a rate or rule because a comment calls it "legacy" has burned
this team before.

## Escalation

First failure first: investigate the earliest red job, not the last.
Escalate to the host team when the extract itself is late or short;
escalate internally when our load or build broke on a healthy extract.
Post every SLA breach (including ones the rerun silently heals) with the
log timestamps that bracket it. Raise an incident for an abend even when
the window recovers.
