# finance close

The monthly close: workbooks in, `fct_gl_monthly` out, controller signs.

## Contents

- `finance/workbooks/CLOSE_YYYYMM.xlsx` — the close workbooks. **The source of
  record.** See `docs/adr/0002-workbook-is-the-source-of-record.md`.
- `loader/import_close.py` — the only path from the workbooks into `raw`.
  Standard library xlsx parse; no openpyxl, on purpose.
- `dbt_project/` — `stg_gl_lines` and `fct_gl_monthly`.
- `dags/close_monthly.py` — the scheduled pipeline. It runs daily; the reason
  is in the module docstring.
- `config/close.yml` — close calendar, review accounts, materiality.
- `config/profiles.yml` — dbt targets (`dev`, `ci`, `reporting`).
- `docs/signoff_checklist.md` — the human half. Read it before month end.
- `run.sh` — the commands.

## Running it

```sh
direnv allow          # optional; run.sh sources .envrc itself
./run.sh workbooks    # what will be imported, and whether anything is open
./run.sh import       # workbooks -> raw
./run.sh build        # dbt run + test
```

## Calendar

Day 1 08:00 UTC `close_import`, 08:10 `dbt_build`, then the controller reviews
`close_summary` against the workbook totals. Target is sign-off within three
working days of period end.

## Things that will catch you out

- The import re-reads **every** workbook every night, closed periods included.
  A prior period edited after sign-off will change in the warehouse and
  nothing will go red. The rule is that prior periods are never edited; there
  is no control enforcing it.
- The materiality threshold is 5000 in `config/close.yml` and 10000 in
  `.envrc`. They have disagreed since January. The BI panel reads
  the YAML.
- A workbook open in Excel leaves a `~$CLOSE_YYYYMM.xlsx` lock file. The
  import ignores it, but an open workbook is one that is still being edited.
- `MANUAL_ADJ` exists only in the workbook. There is no upstream system to
  reconcile it against, which is why every row needs an owner and a note.

## The monthly summary mail

Finance used to get a summary mail on day 2 from a scheduled job in this repo.
That job was removed when the dashboard replaced it; the section describing it
in the old close procedure on the share was not. If someone asks why they
stopped getting the mail, that is why.
