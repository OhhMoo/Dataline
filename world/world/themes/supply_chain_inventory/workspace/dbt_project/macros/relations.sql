{# Stored view bodies must not retain the DuckDB file's catalog name: readers
   ATTACH the warehouse file under an alias, where a baked catalog does not
   resolve (Binder Error: Catalog "warehouse" does not exist). But a two-part
   name is only safe while the schema differs from the catalog. The catalog
   here is `warehouse` (the file stem) and the warehouse models live in schema
   `warehouse`, so a two-part `warehouse.fct_inventory_position` is an
   ambiguous catalog-or-schema reference while dbt has the file open directly
   (Binder Error: Ambiguous reference to catalog or schema "warehouse").
   Relations whose schema collides with the catalog keep the database
   qualifier; only table materializations point at that schema in this
   project, and tables persist no query body, so the baked catalog is safe. #}
{% macro _in_test() %}
  {% do return(model is defined and model is mapping
                and model.get('resource_type') == 'test') %}
{% endmacro %}

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
