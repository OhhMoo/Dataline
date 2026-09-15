{#
  Render every ref() and source() WITHOUT the database qualifier - except
  inside a test, which renders it WITH one. Two different failures, one file.

  1. STORED BODIES MUST CARRY NO CATALOG NAME.

  dbt bakes the reference it renders into the stored body of every view it
  creates. dbt opens var/warehouse/warehouse.duckdb directly, so at creation
  time the catalog is named `warehouse` (DuckDB names the catalog after the
  file stem) and a three-part reference reads `warehouse.raw.daily_revenue`.
  Every reader is required to ATTACH the same file under an alias instead
  (agent.md 9.2.1), because opening it directly makes the catalog and the
  `warehouse` SCHEMA collide. Under that alias the baked `warehouse.` no
  longer resolves, and every staging view is unreadable:

      Binder Error: Catalog "warehouse" does not exist!

  A two-part `raw.<table>` body binds against the view's own catalog,
  whatever that catalog happens to be called - verified under `ATTACH ... AS
  wh` both with and without `USE wh`, and when the file is opened directly. So
  the fix is to make the reference carry no catalog name at all, rather than to
  make the writer and the reader agree on one: agreement is a convention that
  the next tool to run dbt (the gateway's own `dbt_run`, with a profiles.yml
  this repo does not write) can silently break.

  `this` is deliberately untouched: the CREATE statement's target still renders
  three-part and resolves fine, because dbt is connected to the file it is
  writing to.

  2. TESTS ARE NOT STORED, AND TWO-PART BREAKS THEM.

  A test is a SELECT dbt executes immediately against the file it opened
  directly. Nothing about it is persisted, so rule 1 does not apply - and
  two-part is actively wrong here, because a model in the `warehouse` SCHEMA
  renders `warehouse.fct_*`, and against a catalog that is also called
  `warehouse` that is ambiguous rather than merely unqualified:

      Binder Error: Ambiguous reference to catalog or schema "warehouse"
      - use a fully qualified path like "warehouse.warehouse"

  Which is exactly what the three-part rendering produces. So inside a test -
  generic or singular, `model.resource_type` is `test` for both - the database
  qualifier goes back on. Without this branch NO dbt test can be written
  against any model in the `warehouse` schema, in any theme, and the failure
  is a binder error that says nothing about why.
#}
{% macro _in_test() %}
  {% do return(model is defined and model is mapping
                and model.get('resource_type') == 'test') %}
{% endmacro %}

{% macro ref() %}
  {% set relation = builtins.ref(*varargs, **kwargs) %}
  {% if _in_test() %}
    {% do return(relation) %}
  {% endif %}
  {% do return(relation.include(database=false)) %}
{% endmacro %}

{% macro source(source_name, table_name) %}
  {% set relation = builtins.source(source_name, table_name) %}
  {% if _in_test() %}
    {% do return(relation) %}
  {% endif %}
  {% do return(relation.include(database=false)) %}
{% endmacro %}
