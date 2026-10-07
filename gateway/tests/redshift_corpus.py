"""The fixed corpus of Redshift SQL the persona must answer exactly as the world does.

Each entry is (name, Redshift SQL as an engineer would write it against the
persona's `dev` database, DuckDB SQL computing the same scalar on the world's
warehouse file opened read-only - catalog `warehouse`, so a table is
`warehouse.warehouse.<name>`). The Redshift side exercises the constructs the
translator rewrites or vets: GETDATE / DATEADD / DATEDIFF (boundary counting),
NVL, LISTAGG ... WITHIN GROUP, `::` casts, TOP n, DECODE, DATE_TRUNC, CONCAT,
MEDIAN. Clock-relative entries read the wall clock on both sides (the persona's
GETDATE is the box's clock, UTC).

Read by gateway/tests/test_redshift_sql.py (through a live proxy, when one is
named) and by tasks/harness/redshift_e2e_check.py, which runs every entry through
redshift_connector inside the episode against the real PostgreSQL behind the
real proxy, and records each value beside its truth.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

W = "warehouse.warehouse"

CORPUS: list[tuple[str, str, str]] = [
    ("getdate_datediff_day",
     "SELECT DATEDIFF(day, '2026-02-28', GETDATE()::date)",
     "SELECT date_diff('day', DATE '2026-02-28', current_date)"),
    ("dateadd_month_back_from_getdate",
     "SELECT DATEADD(month, -1, GETDATE())::date",
     "SELECT CAST(current_timestamp - INTERVAL 1 MONTH AS DATE)"),
    ("dateadd_month_end_clamps",
     "SELECT DATEADD(month, 1, MAX(order_date))::date FROM warehouse.fct_orders",
     f"SELECT CAST(MAX(order_date) + INTERVAL 1 MONTH AS DATE) FROM {W}.fct_orders"),
    ("datediff_month_counts_boundaries",
     "SELECT SUM(DATEDIFF(month, order_date, '2026-03-01')) FROM warehouse.fct_orders "
     "WHERE order_status = 'completed'",
     f"SELECT SUM(date_diff('month', order_date, DATE '2026-03-01')) FROM {W}.fct_orders "
     "WHERE order_status = 'completed'"),
    ("datediff_year_signups",
     "SELECT COUNT(*) FROM warehouse.dim_customer WHERE DATEDIFF(year, signup_date, "
     "'2026-03-02') >= 3",
     f"SELECT COUNT(*) FROM {W}.dim_customer WHERE date_diff('year', signup_date, "
     "DATE '2026-03-02') >= 3"),
    ("nvl_counts_completed",
     "SELECT COUNT(*) FROM warehouse.fct_orders "
     "WHERE NVL(NULLIF(order_status, 'completed'), 'done') = 'done'",
     f"SELECT COUNT(*) FROM {W}.fct_orders WHERE order_status = 'completed'"),
    ("listagg_within_group",
     "SELECT LISTAGG(DISTINCT order_status, ',') WITHIN GROUP (ORDER BY order_status) "
     "FROM warehouse.fct_orders",
     f"SELECT string_agg(DISTINCT order_status, ',' ORDER BY order_status) FROM {W}.fct_orders"),
    ("cast_colons_net_revenue",
     "SELECT SUM(order_amount::numeric(18,2))::varchar FROM warehouse.fct_orders "
     "WHERE order_status = 'completed'",
     f"SELECT CAST(SUM(order_amount) AS VARCHAR) FROM {W}.fct_orders "
     "WHERE order_status = 'completed'"),
    ("top_n_largest_orders",
     "SELECT SUM(order_amount) FROM (SELECT TOP 10 order_amount FROM warehouse.fct_orders "
     "ORDER BY order_amount DESC, order_id) t",
     f"SELECT SUM(order_amount) FROM (SELECT order_amount FROM {W}.fct_orders "
     "ORDER BY order_amount DESC, order_id LIMIT 10) t"),
    ("decode_completed_revenue",
     "SELECT SUM(DECODE(order_status, 'completed', order_amount, 0)) FROM warehouse.fct_orders",
     f"SELECT SUM(CASE WHEN order_status = 'completed' THEN order_amount ELSE 0 END) "
     f"FROM {W}.fct_orders"),
    ("date_trunc_feb_2026_net_revenue",
     "SELECT SUM(order_amount) FROM warehouse.fct_orders WHERE order_status = 'completed' "
     "AND DATE_TRUNC('month', order_date) = '2026-02-01'",
     f"SELECT SUM(order_amount) FROM {W}.fct_orders WHERE order_status = 'completed' "
     "AND order_date >= DATE '2026-02-01' AND order_date < DATE '2026-03-01'"),
    ("concat_then_pipes",
     "SELECT MIN(CONCAT(segment, '/') || region) FROM warehouse.dim_customer",
     f"SELECT MIN(segment || '/' || region) FROM {W}.dim_customer"),
]


def same(value, truth) -> bool:
    """Numbers as numbers (to 6 places), dates as ISO dates, anything else as text."""
    if value is None or truth is None:
        return value is None and truth is None
    if isinstance(value, (date, datetime)) or isinstance(truth, (date, datetime)):
        return str(value)[:10] == str(truth)[:10]
    try:
        return round(Decimal(str(value)), 6) == round(Decimal(str(truth)), 6)
    except (InvalidOperation, ValueError):
        return str(value) == str(truth)


def truths(warehouse) -> dict[str, object]:
    """Every entry's scalar from the world's DuckDB file, read-only."""
    import duckdb

    conn = duckdb.connect(str(warehouse), read_only=True)
    try:
        return {name: conn.execute(truth).fetchone()[0] for name, _, truth in CORPUS}
    finally:
        conn.close()
