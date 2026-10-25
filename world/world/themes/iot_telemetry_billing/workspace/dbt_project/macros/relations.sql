{# See commerce_analytics' relations.sql for the full rationale. Summary:
   stored VIEW bodies must render two-part (no catalog name) so they still
   resolve for readers that ATTACH the file under an alias; SQL that dbt
   executes immediately (tests, inline queries, TABLE materializations -
   nothing is persisted) must render three-part, because the file's catalog
   is named `warehouse` and a two-part `warehouse.<table>` is an ambiguous
   catalog/schema reference. This project's warehouse models ref each other
   (invoice_run_handoff -> billing_period_lock, fct_billable_usage), which is
   what forces the table branch. #}
{% macro _fully_qualified() %}
  {% do return(model is defined and model is mapping
                and (model.get('resource_type') in ('test', 'sql_operation')
                     or (model.get('resource_type') == 'model'
                         and model.get('materialized') not in ('view', 'ephemeral')))) %}
{% endmacro %}

{% macro ref() %}
  {% set relation = builtins.ref(*varargs, **kwargs) %}
  {% if _fully_qualified() %}
    {% do return(relation) %}
  {% endif %}
  {% do return(relation.include(database=false)) %}
{% endmacro %}

{% macro source(source_name, table_name) %}
  {% set relation = builtins.source(source_name, table_name) %}
  {% if _fully_qualified() %}
    {% do return(relation) %}
  {% endif %}
  {% do return(relation.include(database=false)) %}
{% endmacro %}
