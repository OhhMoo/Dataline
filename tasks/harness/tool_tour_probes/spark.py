"""spark: Databricks tier 2, Spark Connect at sc://spark_connect.internal:15002 (the audited
gRPC proxy native-services hosts in front of a Spark 4.2 + Delta engine seeded from the
world's warehouse), through Spark's own thin client: `SparkSession.builder.getOrCreate()`
with the box's SPARK_REMOTE and nothing else. One `databricks.spark` row per RPC.

The probes use SQL and the DataFrame API over the same catalog names tier 1 serves
(`main.warehouse.fct_orders`): a second copy of the world, as Delta tables. Each runs its
client from a heredoc, the way an engineer pastes a snippet (`python -c` works too: the
box sets SPARK_HOME for the client, gateway/docker/evidence/spark_spike.json).
"""
from tool_tour import Probe

from tool_tour_probes import COMPLETED_ORDERS_FEB_2026_SQL, NET_REVENUE_FEB_2026_SQL

PROFILE = "ds/databricks"

PROBES = [
    Probe(
        id="spark.sql.net_revenue_feb_2026",
        profile=PROFILE,
        plugs={},
        command="""cd /workspace
set -euo pipefail
python3 - <<'PY'
import json
from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
value = spark.sql(
    "SELECT SUM(order_amount) FROM main.warehouse.fct_orders "
    "WHERE order_status = 'completed' AND order_date >= DATE'2026-02-01' "
    "AND order_date < DATE'2026-03-01'").collect()[0][0]
print(json.dumps({"value": str(value)}))
PY
""",
        expect_tools=("databricks.spark",),
        truth_sql=NET_REVENUE_FEB_2026_SQL,
        instruction=(
            "Using PySpark against the team's Databricks cluster (Spark Connect), compute "
            "February 2026 Net Revenue: the sum of order_amount over completed orders in "
            "fct_orders placed in February 2026."),
    ),
    Probe(
        id="spark.dataframe.completed_orders_feb_2026",
        profile=PROFILE,
        plugs={},
        command="""cd /workspace
set -euo pipefail
python3 - <<'PY'
import json
from pyspark.sql import SparkSession, functions as F

spark = SparkSession.builder.getOrCreate()
rows = (spark.table("main.warehouse.fct_orders")
        .filter((F.col("order_date") >= "2026-02-01") & (F.col("order_date") < "2026-03-01"))
        .groupBy("order_status").agg(F.count("*").alias("n"))
        .collect())
print(json.dumps({"value": {r["order_status"]: r["n"] for r in rows}["completed"]}))
PY
""",
        expect_tools=("databricks.spark",),
        truth_sql=COMPLETED_ORDERS_FEB_2026_SQL,
        instruction=(
            "With the PySpark DataFrame API on the team's Databricks cluster, count the "
            "completed orders placed in February 2026 (fct_orders), grouping by status."),
    ),
    Probe(
        id="spark.catalog.fct_orders_columns",
        profile=PROFILE,
        plugs={},
        command="""cd /workspace
set -euo pipefail
python3 - <<'PY'
import json
from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
print(json.dumps({"value": len(spark.catalog.listColumns("main.warehouse.fct_orders"))}))
PY
""",
        expect_tools=("databricks.spark",),
        truth_sql=("SELECT count(*) FROM information_schema.columns WHERE table_catalog = "
                   "'warehouse' AND table_schema = 'warehouse' AND table_name = 'fct_orders'"),
        instruction=(
            "Through the Spark catalog API on the team's Databricks cluster, report how many "
            "columns the fct_orders table has."),
    ),
]
