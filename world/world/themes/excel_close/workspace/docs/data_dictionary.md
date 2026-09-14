# Data dictionary

- `raw.gl_lines` — GL detail exactly as imported from each workbook's
  GL_LINES sheet: `line_id`, `account`, `post_date`, `period`, `amount`.
  Every column is a string at this layer; typing happens in staging.
- `raw.manual_adj` — the MANUAL_ADJ sheet: manual adjustments keyed by the
  close team (`adj_id`, `account`, `period`, `amount`, `owner`, `note`).
  This sheet lives only in the workbook — there is no upstream system.
- `staging.stg_gl_lines` — typed GL lines (`amount` decimal(12,2),
  `post_date` date; `line_id` stays varchar).
- `warehouse.fct_gl_monthly` — one row per `period` + `account`: `lines`,
  `net_amount`, and `adj_total` joined from `raw.manual_adj`.

Dashboard `close_summary` reads `warehouse.fct_gl_monthly`.
