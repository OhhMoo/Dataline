# ADR 0001 — one warehouse file per environment, and none of them in the repo

Status: accepted (2026-01-27)

## Context

The warehouse is a single DuckDB file. It started inside the repo, next to the
dbt project, because that was the shortest path to a working `dbt build`.

Three things went wrong with that:

1. `git clean -xfd` deleted it. Twice.
2. Everyone's `~/.dbt/profiles.yml` pointed somewhere slightly different, so
   "it works for me" was true and useless.
3. The file is named `warehouse.duckdb` and one of the schemas inside it is
   also called `warehouse`. DuckDB names the catalog after the file stem, so
   opening the file directly makes `warehouse.fct_revenue` ambiguous — catalog
   or schema? Every two-part reference then fails to bind.

## Decision

- The warehouse file lives outside the repo, at `../var/warehouse/warehouse.duckdb`
  relative to the repo root. One file per environment, never shared.
- The path comes from `DBT_WAREHOUSE_PATH`, with that relative path as the
  fallback, so a fresh checkout works with no configuration at all.
- `config/profiles.yml` is committed and `run.sh` exports `DBT_PROFILES_DIR`
  so that it is the profile anything run through `run.sh` uses.
- Anything that opens the file for reading **attaches it under an alias**
  rather than opening it directly:

  ```sql
  ATTACH '<path>' AS wh (READ_ONLY);
  USE wh.warehouse;
  ```

  dbt itself is the exception — it opens the file directly because it is the
  writer, and the project's `macros/relations.sql` strips the database
  qualifier off every `ref()` and `source()` so that the view bodies dbt
  stores do not bake in a catalog name.

## Consequences

- A developer checkout and the scheduler read the same file by different
  routes: the checkout through `config/profiles.yml`, the scheduler through a
  `profiles.yml` it renders itself. They agree today. Nothing enforces that
  they keep agreeing, which is the one loose end in this decision.
- `dbt --target prod` from a developer checkout fails on a missing mount.
  That is intended: it should not quietly fall back to dev.
