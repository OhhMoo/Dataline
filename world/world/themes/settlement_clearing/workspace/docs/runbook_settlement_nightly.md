# Runbook - settlement nightly

## Normal schedule (UTC)

- 04:00 - the network partition lands.
- 20:00 - `payments_nightly` reloads every feed partition and control file.
- 20:10 - `dbt_payments` rebuilds current payments, merchant terms, checkpoint,
  correction register, and interchange reconciliation, with tests.
- 20:20 - `settlement_publish` updates the bank handoff.
- 23:30 - `clearing_window_close` appends the bank's cutoff record.

The deployed order is also in `orchestration/schedule.yml`. A failed earlier job
must be investigated before assuming a later green job published current data.

## Reloads and recovery

The raw loader is a full reload over `data/payments_feed/dt=*/*.csv`; it has no
append mode and no date flags. Re-land the network's file under its own `dt=`
folder, then run `./run.sh load && ./run.sh build && ./run.sh publish` when a
manual run is authorized. Recovery replays are expected and do not require row
deletion.

Check `warehouse.load_checkpoint` against the partition folders. The checkpoint
moves only to a partition present in the raw reload. Never set it to the execution
date merely to make a gap disappear.

## Cutoff and corrections

Do not hand-edit `settlement_handoff` or `clearing_window`. Before changing a
closed day, follow `docs/correction_protocol.md` and register the approval in
`data/control/settlement_corrections.csv`. A closed day with changed current
payments but no registered approval is withheld from republication; open days
continue to publish.

The cutoff script is append-only. Re-running it does not rewrite a `SETTLED` day.
An `SLA_BREACH` is historical evidence, not a row to delete after recovery.

## Verification

`./run.sh check` parses the project and runs the nightly dbt selection. Reconcile
`fct_payments` to the handoff for open days, check the newest checkpoint, and
render the clearing dashboard before the bank cutoff. Escalation contacts are in
`docs/contacts.md`.
