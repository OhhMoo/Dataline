# Your first week

Everything below is on the internal network. None of it is routable, none of the tokens are real, and all of them are already in `.envrc`.

## The systems

| system | where | reached with | variable |
|---|---|---|---|
| the warehouse | `postgres.internal:5432` | psql, dbt, psycopg | see `.envrc` |
| object storage | `minio.internal:9000` | aws s3, boto3 | see `.envrc` |
| the scheduler | `airflow.internal:8080` | airflow CLI, REST | see `.envrc` |
| the second warehouse | `snowflake.internal:8443` | snowsql, the connector | see `.envrc` |
| metabase | `metabase.internal:3000` | dashboards and saved questions | `METABASE_URL` |
| loki | `loki.internal:3100` | job logs; logcli reads LOKI_ADDR | `LOKI_URL` |
| jira | `jira.internal:8080` | the ticket queue | `JIRA_URL` |
| slack | `slack.internal:8081` | the team channels | `SLACK_BASE_URL` |
| airbyte | `airbyte.internal:8001` | connector control plane | `AIRBYTE_URL` |
| deploy | `deploy.internal:8085` | releases; ops/deploy wraps it | `DEPLOY_URL` |
| confluence | `confluence.internal:8090` | the wiki: runbooks, postmortems, policies | `CONFLUENCE_URL` |
| prometheus | `prometheus.internal:9090` | metrics | `PROMETHEUS_URL` |
| alertmanager | `alertmanager.internal:9093` | alarms | `ALERTMANAGER_URL` |

`~/.config/alkera/services.toml` holds the same table if you want to read it from a script. A bare `dbt` uses `~/.dbt/profiles.yml`; this repository's `config/profiles.yml` is a different target.

## The pipeline

| job | command | runs after |
|---|---|---|
| `daily_load` | `load_daily_revenue` | - |
| `dbt_build` | `dbt_build` | - |

## Dashboards

Defined in `var/bi/dashboards/`: `commercial_daily`.
