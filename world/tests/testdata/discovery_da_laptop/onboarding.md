# Your first week

You are on call for Analytics (`ANA`). Teams you will hear from: Analytics, Data Platform, Finance, Sales.

Everything below is on the internal network. None of it is routable, none of the tokens are real, and all of them are already in `.envrc`.

## The systems

| system | where | reached with | variable |
|---|---|---|---|
| the notebook server | `jupyter.internal:8888` | JupyterLab REST, nbclient | see `.envrc` |
| jira | `jira.internal:8080` | the ticket queue | `JIRA_URL` |
| slack | `slack.internal:8081` | the team channels | `SLACK_BASE_URL` |
| confluence | `confluence.internal:8090` | the wiki: runbooks, postmortems, policies | `CONFLUENCE_URL` |
| powerbi | `powerbi.internal:8095` | Power BI REST / Fabric REST, Entra client-credentials token | `POWERBI_API_URL` |
| tableau | `tableau.internal:8096` | tabcmd, Tableau Server Client (tableauserverclient), REST | `TABLEAU_SERVER_URL` |
| sigma | `sigma.internal:8097` | Sigma REST API v2, client-credentials token | `SIGMA_BASE_URL` |

`~/.config/alkera/services.toml` holds the same table if you want to read it from a script. A bare `dbt` uses `~/.dbt/profiles.yml`; this repository's `config/profiles.yml` is a different target.

## The BI projects

| tool | project in this repository | service |
|---|---|---|
| powerbi | `bi/powerbi/` | `powerbi.internal:8095` |
| tableau | `bi/tableau/` | `tableau.internal:8096` |
| sigma | `bi/sigma/` | `sigma.internal:8097` |

`bi/README.md` describes how they are laid out.

## Where the runbooks are

The wiki is the `AE` space (Analytics Engineering). Pages, by content id, with the path pages and tickets link them by:

  - `54174547528` - Architecture (`ae/architecture`)
    - `48560273444` - The analytics stack (`ae/architecture/analytics-stack`)
    - `65960652322` - Data dictionary (`ae/architecture/data-dictionary`)
  - `57217669245` - Metrics (`ae/metrics`)
    - `59304629305` - Net Revenue and friends (`ae/metrics/net-revenue`)
  - `17826590443` - Policies (`ae/policies`)
    - `13356801044` - Month-end freeze (`ae/policies/month-end-freeze`)
  - `30042097585` - Postmortems (`ae/postmortems`)
    - `64324028077` - Postmortem: stale dashboards, 2026-01-12 (`ae/postmortems/2026-01-12-stale-dashboards`)
  - `16391716131` - Runbooks (`ae/runbooks`)
    - `15080548128` - Runbook: BI release process (`ae/runbooks/bi-release-process`)
    - `10160973898` - Runbook: nightly on-call (`ae/runbooks/nightly-on-call`)
    - `31283714637` - Runbook: publishing dashboards (`ae/runbooks/publishing-dashboards`)

Two of these overlap. The wiki records who edited a page and when, and so does this repository's git history; when they disagree, that is the evidence.

## Where the people are

Workspace `harrow-finch`. Channels: `#analytics-eng`, `#bi-requests`, `#data-incidents`, `#finance-analytics`.

## The queue

`ANA`, statuses `backlog`, `todo`, `in_progress`, `in_review`, `done`, `wont_do`.

| status | tickets |
|---|---|
| `backlog` | 2 |
| `todo` | 6 |
| `in_progress` | 0 |
| `in_review` | 0 |
| `done` | 3 |
| `wont_do` | 1 |

## Dashboards

Defined in `var/bi/dashboards/`: `executive_overview`, `sales_overview`.
