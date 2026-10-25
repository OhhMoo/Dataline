{% macro _in_test() %}
  {% do return(model is defined and model is mapping
                and model.get('resource_type') == 'test') %}
{% endmacro %}

{#
  Views must not bake in the warehouse file's catalog name. A relation whose
  schema is also `warehouse` (the DuckDB file stem) cannot be rendered as
  two-part `warehouse.<table>`: DuckDB treats that as an ambiguous catalog or
  schema. Those relations are tables or tests, so the catalog qualifier is
  resolved at execution and is not stored in a view body.
#}
{% macro _keep_database(relation) %}
  {% do return(_in_test() or (relation.database is not none
                and relation.database == relation.schema)) %}
{% endmacro %}

{% macro ref() %}
  {% set relation = builtins.ref(*varargs, **kwargs) %}
  {% if _keep_database(relation) %}
    {% do return(relation) %}
  {% endif %}
  {% do return(relation.include(database=false)) %}
{% endmacro %}

{% macro source(source_name, table_name) %}
  {% set relation = builtins.source(source_name, table_name) %}
  {% if _keep_database(relation) %}
    {% do return(relation) %}
  {% endif %}
  {% do return(relation.include(database=false)) %}
{% endmacro %}
