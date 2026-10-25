{#
  Render a ref WITH the database qualifier (three-part), for table models
  whose SELECT is executed immediately against the directly-opened
  warehouse.duckdb. There the catalog is also named `warehouse`, so a
  two-part `warehouse.<model>` reference into the `warehouse` SCHEMA is
  ambiguous:

      Binder Error: Ambiguous reference to catalog or schema "warehouse"
      - use a fully qualified path like "warehouse.warehouse"

  Never use this in a view: stored view bodies must stay two-part so they
  bind against whatever catalog a reader ATTACHes the file under
  (see relations.sql). Table models store data, not SQL, so the baked
  catalog name is never persisted.
#}
{% macro ref_db() %}
  {% do return(builtins.ref(*varargs, **kwargs)) %}
{% endmacro %}
