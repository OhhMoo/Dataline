{% macro _in_test() %}
  {% do return(model is defined and model is mapping
                and model.get('resource_type') == 'test') %}
{% endmacro %}

{#
  Strip the catalog from stored bodies (views) so they stay readable when the
  warehouse file is attached under an alias, but KEEP it whenever the
  relation's schema collides with the catalog name: the DuckDB catalog here is
  `warehouse` (file stem) and the warehouse models live in schema `warehouse`,
  so a two-part `warehouse.fct_mrr_daily` is an ambiguous catalog-or-schema
  reference at CREATE time (Binder Error: Ambiguous reference to catalog or
  schema "warehouse"). Only table materializations live in that schema, and
  tables persist no query body, so the baked catalog name is safe there.
#}
{% macro _collides_with_catalog(relation) %}
  {% do return(relation.database is not none
                and relation.database == relation.schema) %}
{% endmacro %}

{% macro ref() %}
  {% set relation = builtins.ref(*varargs, **kwargs) %}
  {% if _in_test() or _collides_with_catalog(relation) %}
    {% do return(relation) %}
  {% endif %}
  {% do return(relation.include(database=false)) %}
{% endmacro %}

{% macro source(source_name, table_name) %}
  {% set relation = builtins.source(source_name, table_name) %}
  {% if _in_test() or _collides_with_catalog(relation) %}
    {% do return(relation) %}
  {% endif %}
  {% do return(relation.include(database=false)) %}
{% endmacro %}
