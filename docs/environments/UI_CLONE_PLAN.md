# UI clones: Tableau Server and Sigma in the browser

Written 2026-10-06. Phase 5 of `BI_BUILD_PLAN.md`. Sizes: S ≈ days, M ≈ a week, L ≈ multi-week.

## North star

What an analyst sees and does in Tableau Server / Sigma in a browser: browse projects
and workbooks, open a dashboard and read its numbers (tables, bar and line charts),
inspect and edit a calculated field / column formula, save and publish. Every UI action
lands in the same audited, published state that the REST API serves and the graders
read. There is one source of truth, and the UI is one more client of it.

## Decisions

1. **Server-rendered HTML, no SPA.** Jinja-free f-string templates, inline CSS, no
   JavaScript, no build toolchain. Why:
   - The DOM is deterministic, so an agent can read it through a browser tool, a text
     browser or `curl`.
   - A test can parse it with `html.parser`.
   - The same state gives the same bytes, which keeps rollouts reproducible.
   - There is no node toolchain to keep in the arm64 image.

   An SPA would buy drag-and-drop authoring and nothing an agent needs for the target
   tasks.
2. **Mount into the vendor's own FastAPI app (`mount(app)`).** The UI imports the
   service module and uses its `Store`, `_LOCK`, file lock, publish methods and `_Row`
   audit writer. That gives one process, one writer and one audit row per UI action.

   A separate UI service that imports the store classes would also be correct, because
   the file lock serializes writers. But it would add a port, a second process
   touching state, and a second place to forget a row. `standalone()` exists only for
   tests or a separate port, and it shares the same `Context`.
3. **Driving it: Playwright + headless Chromium in the episode image, `curl`/HTML as
   the fallback.**
   - Playwright (Apache-2.0) ships Chromium (BSD) for linux-arm64. Headless Chromium
     plus its deps add about 300 MB (the estimate; measured 2026-10-13 on the da image: ~900 MB with fonts and Chromium's shared libraries, libLLVM via libgbm1 included).
   - A computer-use desktop (Xvfb + WM + VNC + full Chromium) costs about 0.8–1 GB and
     needs screenshot-grounded actions. Defer it until a task needs pixels (chart
     reading only, no table). The HTML already exposes every number as text, so a
     browser tool's accessibility tree is enough.
   - The text-browser fallback is plain HTTP: forms are real `<form method=post>`, so
     the whole flow works with `curl -c/-b` or `w3m`.
4. **Fidelity = URLs, page structure, labels and refusal text.**
   - Tableau Server routes its pages client-side (`/#/workbooks`, `/#/views/<Wb>/<Sheet>`).
     A fragment never reaches a server, so the same paths are served without the `#/`.
   - `/views/<Wb>/<Sheet>` and `/views/<Wb>/<Sheet>.csv` are Tableau Server's real
     server-side URLs. The session cookie is `workgroup_session_id`.
   - Labels are Tableau's: Sign In, Workbooks, Data Sources, Calculated Fields, "The
     calculation contains errors", Download Data.
   - Sigma (next) uses `/workbook/<name>-<urlId>`, `/workbook/.../page/...` and
     element/column naming from its docs.
   - No vendor logos, fonts, icons, colours or CSS. Branding reads "Tableau Server
     (Alkera)".
5. **Anti-reward-hack.**
   - UI writes go through the service's publish method (`Store.publish_datasource`
     with overwrite), not a side door. A save is one `write` row with targets and
     flags like the REST publish.
   - Graders keep re-rendering the published state staged after teardown. The UI adds
     no grader input.
   - Every number on a page is a visible table cell. Charts repeat the table. There
     are no hidden fields, comments or data attributes carrying values.
   - A formula is validated by the front-end's `parse_expression`, then by re-reading
     the edited document. A refusal shows the vendor's error verbatim
     (`unsupported (Tableau): …`) and publishes nothing.
   - The workspace (git) is never written: publishing is the act, as on the real server.
6. **First vendor: Tableau.**
   - Its view-data endpoint gives a byte-exact oracle for the page (`<view>.csv` ≡
     `views/{id}/data`).
   - Its app has no catch-all route, so `mount` cannot be shadowed. Sigma's service
     ends in `/{path:path}`, so mounting there must insert routes before it.
   - Calculated fields live in one published `.tds`, so a save is a one-attribute diff.

## Built (slice 1, Tableau)

- `gateway/src/alkera_gateway/native/ui/html.py` holds the vendor-neutral shell, tables
  and inline-SVG bar and line charts.
- `gateway/src/alkera_gateway/native/ui/tableau.py` serves these pages:
  - sign in and sign out
  - `/workbooks` and `/workbooks/{id}/views`
  - `/views/{wb}/{sheet}` (dashboards show every zone's sheet) and its `.csv`
  - `/datasources` and `/datasources/{id}`
  - the calculation editor `/datasources/{id}/calculations/{name}` (GET/POST)

  Tool names are `tableau.ui_*`.
- `gateway/tests/test_ui_tableau.py` (8 tests) runs on a built `commerce_analytics`
  world. It checks that:
  - page tables equal REST CSV and `render_visual`
  - a UI calc save changes REST view data and the published `.tds` (one attribute)
    but not the workspace
  - refused formulas show the front-end's message and publish nothing
  - each request writes exactly one row
  - `mount` into `services.tableau.app` coexists with the API

## Built (phase 2, Tableau, 2026-10-13)

- **Mounted.** `services/tableau.py` mounts the UI into its own app at import (the
  last lines of the module), so `tableau.internal:8096` serves the API and the
  browser from one process, one writer and one audit stream. Its `__all__` names
  what the UI uses (`Row`, `LOCK`, `Store`, `find`, `all_views`, `visual_for`,
  `extract_owner`, `view_csv`, `visual_relations`, `with_url_filters`, ...), now
  public names rather than private helpers.
- **Projects.** `/projects` lists the site's projects (the REST `projects` list:
  Default) with their workbook and data source counts; `/projects/{id}` lists a
  project's content; the workbooks page takes each workbook's project from it.
- **URL filters.** `/views/<Wb>/<Sheet>?region=EMEA` (and its `.csv`) filters every
  sheet on the page exactly as the REST API's `vf_region=EMEA`; `:`-parameters
  (`:embed=y`) are ignored, and a field the data source lacks is listed on the
  page as not applied. Both match a field by the name Tableau shows for it (its
  caption, else its name: `order_id` vs `order_id (fct_order_lines)`), through
  `services.tableau.with_url_filters`.
- **A workbook's own calculations.** `/workbooks/{id}/views` lists them and
  `/workbooks/{id}/calculations/{name}` edits them: the workbook-local calculations
  over a published source, or every calculation of a source the workbook embeds.
  A save checks the edited workbook as the server will read it and publishes it
  through `Store.publish_workbook`; every copy of the formula (the data source's,
  each sheet's) changes, nothing else. A published calculation a workbook only
  caches is refused there, with a link to its data source.
- **Packaged files.** `.tdsx` and `.twbx` are edited too: the archive is repacked
  with only its top-level document replaced, every other member (extracts,
  images) byte for byte as it was.
- **Its own sign-in.** The browser's password is `services.tableau.UI_PASSWORD`,
  not the REST token. The REST sign-in refuses it, the browser refuses the REST
  token, and browser sessions (`ui_sessions` in the manifest) are not
  `X-Tableau-Auth` tokens. It is deliberately in no box env and no discovery file
  (`.envrc`, `services.toml`, `onboarding.md`, the plug env): those are part of the
  BI001/BI002 da/laptop worlds, which stay byte-identical (wiring it moves BI001's
  workspace digest and three discovery/overlay goldens). A UI-only task arm (phase
  4) hands it out through its own brief.
- `gateway/tests/test_ui_tableau.py` (14 tests) adds the checks for all of the
  above; `test_tableau_service.py` adds `test_url_filter_matches_by_caption`.

## Built (phase 3, Sigma, 2026-10-13)

- `gateway/src/alkera_gateway/native/ui/sigma.py`, mounted at the end of
  `services/sigma.py` (`_catch_all_last(_ui.mount(app))`), so `sigma.internal:8097`
  serves the API and the browser from one process. Its pages are under `/alkera`
  (the org of a document's API `url`): sign in and out, the workbook and data
  model list, `/alkera/workbook/<slug>-<urlId>[/page/<pageId>]` (each element's
  table, chart or KPIs, and columns), a column's formula editor
  (`.../element/<id>/column/<id>`, GET/POST) and `/alkera/data-model/<slug>-<urlId>`
  (read-only). Tool names are `sigma.ui_*`.
- An element's table is the `POST /v2/workbooks/{id}/export` CSV, cell for cell. A
  formula is published through `_publish_workbook`, as `PUT .../contents` does; a
  refused one shows the front-end's message and publishes nothing, and a page
  rendered from an older `documentVersion` gets Sigma's 409.
- The `/{path:path}` fallback stays the last route, so every path outside `/alkera`,
  `/v2/...` included, is still a `sigma.unknown_route` 404.
- Its own sign-in: a cookie session (`sigma_session`, `uiSessions` in the state)
  that only `services.sigma.UI_PASSWORD` opens. The token endpoint refuses that
  password, the form refuses the API secret, and a session is not a bearer token.
- Not imitated: editing anything but an existing column's formula, drafts, folders,
  sharing, version history (the module docstring lists them).
- `gateway/tests/test_ui_sigma.py` checks the above, including
  `test_unknown_api_routes_still_404_after_ui_mount` and
  `test_ui_password_is_not_an_api_token`.

**UI passwords are service constants, not box env.** `services.tableau.UI_PASSWORD`
and `services.sigma.UI_PASSWORD` are in no box env and no discovery file. A task's
brief hands them out; the browser tour probes (`tasks/harness/tool_tour_probes/
browser.py`) state them, as oracles. The discovery files are part of the BI001/BI002
da/laptop worlds: wiring a UI password into them moves BI001's workspace digest
(measured with Tableau's, phase 2 above).

## Phases

| # | What | Size |
|---|------|------|
| 1 | Tableau slice (above) | done |
| 2 | Wire `mount` into `services/tableau.py`; publicize helpers; add a `/projects` page; workbook-local calcs and embedded `.twb` data sources; `.tdsx` repack; URL filters (`?region=EMEA`, as Tableau view URLs take them); a UI password of its own | done |
| 3 | Sigma: `/workbook/<slug>-<id>` pages, elements as tables/charts, a column-formula editor through `_publish_workbook` (insert routes before the catch-all), data-model page | done |
| 4 | Agent harness: Playwright MCP / browser tool in the episode image (arm64 build check; +~900 MB measured); a task variant that is UI-only (no REST token in the brief); trajectory checks that a UI save produced the row | partial: Playwright and chromium-headless-shell are in the da/ds images (plan decision D5; +900 MB on the da image, `gateway/docker/TOOLBELT.md` "Cost, measured"; arm64 inputs resolve, not built), and the browser tour probes pass (`tasks/harness/evidence/tool_tour_browser.json`, 4/4). Open: a browser MCP and a UI-only graded arm |
| 5 | Authoring depth: create calc, rename, revert/version history, new sheet from shelves (rows/cols/marks as forms, still server-rendered) | L |
| 6 | Computer-use desktop (Xvfb/VNC), only if a task needs pixel-level chart reading | L |
