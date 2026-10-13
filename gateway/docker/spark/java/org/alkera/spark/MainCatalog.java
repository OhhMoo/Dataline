package org.alkera.spark;

import org.apache.spark.sql.connector.catalog.DelegatingCatalogExtension;
import org.apache.spark.sql.util.CaseInsensitiveStringMap;

/**
 * Unity Catalog's default catalog name, {@code main}, over Spark's own session catalog.
 *
 * <p>Databricks addresses a table as {@code main.warehouse.fct_orders}; open-source Spark
 * has one built-in catalog, {@code spark_catalog}, and no way to rename it. This plugin
 * (registered as {@code spark.sql.catalog.main}) delegates every call to the session
 * catalog - Delta's {@code DeltaCatalog} - so {@code main.s.t} and {@code s.t} are the
 * same table. {@code name()} stays the delegate's, on purpose: Spark treats a catalog
 * named {@code spark_catalog} as the session catalog, which is what lets views and
 * non-Delta tables resolve through {@code main} as they do through two-part names. The
 * cost, written down in docs/environments/DATABRICKS_SPIKE.md: {@code current_catalog()}
 * answers {@code spark_catalog}.
 */
public final class MainCatalog extends DelegatingCatalogExtension {
  @Override
  public void initialize(String name, CaseInsensitiveStringMap options) {
    org.apache.spark.sql.classic.SparkSession spark =
        (org.apache.spark.sql.classic.SparkSession) org.apache.spark.sql.SparkSession.active();
    setDelegateCatalog(spark.sessionState().catalogManager().v2SessionCatalog());
  }
}
