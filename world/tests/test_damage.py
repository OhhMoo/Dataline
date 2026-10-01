"""Damage monitor (world/world/damage.py): one exact V_c in [0, 1] per contract.

Every test builds a tiny world root by hand: a DuckDB file at
var/warehouse/warehouse.duckdb, a scheduler state with a `vnow`, and (for
liveness) a live.json carrying the arrival stream. No theme build, no dbt.
"""
import json
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import damage  # noqa: E402

DB = "var/warehouse/warehouse.duckdb"
VTIME_START = "2026-03-01T00:00:00Z"


def connect(db: Path, read_only: bool = False):
    """Open the warehouse the way the world does (world/world/bi.py): attached
    under a neutral alias, so `warehouse.<table>` is not ambiguous with the
    file-stem catalog."""
    con = duckdb.connect(":memory:")
    mode = " (READ_ONLY)" if read_only else ""
    con.execute(f"ATTACH '{db.as_posix()}' AS wh{mode}")
    con.execute("USE wh")
    return con


def make_root(tmp_path: Path, sql: list[str], vnow: str = "2026-03-11T00:00:00Z",
              live: dict | None = None) -> Path:
    root = tmp_path / "w"
    (root / "var/warehouse").mkdir(parents=True)
    (root / "var/sched").mkdir(parents=True)
    (root / "var/world").mkdir(parents=True)
    con = connect(root / DB)
    for stmt in sql:
        con.execute(stmt)
    con.close()
    (root / "var/sched/state.json").write_text(
        json.dumps({"vnow": vnow, "jobs": {}, "runs": []}), encoding="utf-8")
    if live is not None:
        (root / "var/world/live.json").write_text(json.dumps(live), encoding="utf-8")
    return root


def spec_for(contract: str, cfg: dict, weights: dict | None = None) -> dict:
    return {"db": DB, "weights": weights or {contract: 1.0},
            "contracts": {contract: cfg}}


def keys_table(n: int, dup: int = 0) -> list[str]:
    """warehouse.fct with n distinct order_ids; the first `dup` appear twice."""
    return [
        "CREATE SCHEMA warehouse",
        "CREATE TABLE warehouse.fct (order_id INTEGER, amount DECIMAL(12,2))",
        f"INSERT INTO warehouse.fct SELECT i, i * 1.5 FROM range({n}) t(i)",
        f"INSERT INTO warehouse.fct SELECT i, i * 1.5 FROM range({dup}) t(i)",
    ]


# --------------------------------------------------------------------------
# exactly_once
# --------------------------------------------------------------------------

EO = {"table": "warehouse.fct", "key": ["order_id"]}


def test_exactly_once_clean(tmp_path):
    root = make_root(tmp_path, keys_table(100))
    out = damage.sample(root, spec_for("exactly_once", EO))
    assert out["V"] == {"exactly_once": 0.0}
    assert out["detail"]["exactly_once"]["keys"] == 100


def test_exactly_once_planted(tmp_path):
    root = make_root(tmp_path, keys_table(100, dup=3))
    out = damage.sample(root, spec_for("exactly_once", EO))
    assert out["V"]["exactly_once"] == pytest.approx(0.03, abs=0)
    assert out["detail"]["exactly_once"] == {"dup_keys": 3, "keys": 100,
                                             "rows": 103}


def test_exactly_once_empty(tmp_path):
    root = make_root(tmp_path, keys_table(0))
    assert damage.sample(root, spec_for("exactly_once", EO))["V"] == {"exactly_once": 0.0}


# --------------------------------------------------------------------------
# liveness
# --------------------------------------------------------------------------

LIVE = {"vtime_start": VTIME_START, "days_landed": 5,
        "arrival": {"name": "daily_revenue", "at": {"day_offset": 1, "time": "00:00"}}}
LV = {"table": "raw.daily", "partition_col": "dt", "arrival": "daily_revenue",
      "grace_hours": 6}


def daily(days: list[int]) -> list[str]:
    sql = ["CREATE SCHEMA raw", "CREATE TABLE raw.daily (order_id INTEGER, dt DATE)"]
    for d in days:
        sql.append(f"INSERT INTO raw.daily VALUES ({d}, DATE '2026-03-01' + {d}),"
                   f" ({d * 10 + 1}, DATE '2026-03-01' + {d})")
    return sql


def test_liveness_clean(tmp_path):
    # vnow 2026-03-11T00:00Z, grace 6h: day i is due iff 03-(02+i) 06:00 <= vnow,
    # i.e. i in 0..8 -> 9 partitions due.
    root = make_root(tmp_path, daily(list(range(9))), live=LIVE)
    out = damage.sample(root, spec_for("liveness", LV))
    assert out["V"] == {"liveness": 0.0}
    assert out["detail"]["liveness"]["due"] == 9


def test_liveness_planted(tmp_path):
    root = make_root(tmp_path, daily([0, 1, 2, 4, 5, 7, 8]), live=LIVE)
    out = damage.sample(root, spec_for("liveness", LV))
    assert out["V"]["liveness"] == 2 / 9
    assert out["detail"]["liveness"]["missing_partitions"] == ["2026-03-04", "2026-03-07"]


def test_liveness_grace_excludes_not_yet_due(tmp_path):
    # day 9 arrives at 03-11T00:00 == vnow but is due only at 06:00: absent, no damage
    root = make_root(tmp_path, daily(list(range(9))), live=LIVE)
    out = damage.sample(root, spec_for("liveness", LV))
    assert "2026-03-10" not in out["detail"]["liveness"]["missing_partitions"]


def test_liveness_empty_table_nothing_due(tmp_path):
    root = make_root(tmp_path, daily([]), vnow="2026-03-01T00:00:00Z", live=LIVE)
    assert damage.sample(root, spec_for("liveness", LV))["V"] == {"liveness": 0.0}


def test_liveness_empty_table_all_due_missing(tmp_path):
    root = make_root(tmp_path, daily([]), live=LIVE)
    assert damage.sample(root, spec_for("liveness", LV))["V"] == {"liveness": 1.0}


# --------------------------------------------------------------------------
# version_precedence
# --------------------------------------------------------------------------

VP = {"table": "warehouse.cur", "key": ["id"], "version_col": "ver",
      "source_sql": "SELECT id, ver FROM raw.deliveries"}


def versions(served: list[tuple[int, int]]) -> list[str]:
    sql = ["CREATE SCHEMA raw", "CREATE SCHEMA warehouse",
           "CREATE TABLE raw.deliveries (id INTEGER, ver INTEGER)",
           # 50 keys, each delivered as versions 1, 2, 3
           "INSERT INTO raw.deliveries SELECT i, v FROM range(50) a(i), range(1, 4) b(v)",
           "CREATE TABLE warehouse.cur (id INTEGER, ver INTEGER)"]
    if served:
        sql.append("INSERT INTO warehouse.cur VALUES "
                   + ", ".join(f"({i}, {v})" for i, v in served))
    return sql


def test_version_precedence_clean(tmp_path):
    root = make_root(tmp_path, versions([(i, 3) for i in range(50)]))
    assert damage.sample(root, spec_for("version_precedence", VP))["V"] == {
        "version_precedence": 0.0}


def test_version_precedence_planted(tmp_path):
    served = [(i, 3) for i in range(50)]
    served[0] = (0, 2)
    served[1] = (1, 1)
    root = make_root(tmp_path, versions(served))
    out = damage.sample(root, spec_for("version_precedence", VP))
    assert out["V"]["version_precedence"] == 2 / 50
    assert out["detail"]["version_precedence"]["stale_keys"] == 2


def test_version_precedence_empty(tmp_path):
    root = make_root(tmp_path, versions([]))
    assert damage.sample(root, spec_for("version_precedence", VP))["V"] == {
        "version_precedence": 0.0}


# --------------------------------------------------------------------------
# cutoff_integrity
# --------------------------------------------------------------------------

def cutoff_world(tmp_path, mutate: list[str]) -> tuple[Path, dict]:
    sql = ["CREATE SCHEMA warehouse",
           "CREATE TABLE warehouse.daily (d DATE, revenue DECIMAL(14,2))",
           # 20 closed days (Feb 1..20) + 5 open days (Mar 1..5)
           "INSERT INTO warehouse.daily SELECT DATE '2026-02-01' + i::INTEGER, 100 + i FROM range(20) t(i)",
           "INSERT INTO warehouse.daily SELECT DATE '2026-03-01' + i::INTEGER, 500 + i FROM range(5) t(i)"]
    root = make_root(tmp_path, sql)
    snap = root / "var/world/published_snapshot.parquet"
    con = connect(root / DB)
    con.execute(f"COPY (SELECT * FROM warehouse.daily WHERE d <= DATE '2026-02-28') "
                f"TO '{snap.as_posix()}' (FORMAT parquet)")
    for stmt in mutate:
        con.execute(stmt)
    con.close()
    cfg = {"table": "warehouse.daily", "key": ["d"], "closed_through": "2026-02-28",
           "date_col": "d", "snapshot": "var/world/published_snapshot.parquet"}
    return root, cfg


def test_cutoff_integrity_clean(tmp_path):
    # open-period edits are not damage
    root, cfg = cutoff_world(tmp_path, ["UPDATE warehouse.daily SET revenue = 0 "
                                        "WHERE d = DATE '2026-03-02'"])
    out = damage.sample(root, spec_for("cutoff_integrity", cfg))
    assert out["V"] == {"cutoff_integrity": 0.0}
    assert out["detail"]["cutoff_integrity"]["rows"] == 20


def test_cutoff_integrity_planted(tmp_path):
    root, cfg = cutoff_world(tmp_path, [
        "UPDATE warehouse.daily SET revenue = revenue + 1 WHERE d = DATE '2026-02-03'",
        "DELETE FROM warehouse.daily WHERE d = DATE '2026-02-10'",
        "INSERT INTO warehouse.daily VALUES (DATE '2026-02-25', 1)"])
    out = damage.sample(root, spec_for("cutoff_integrity", cfg))
    # 3 differing keys over the 21 keys of snapshot ∪ table in the closed period
    assert out["V"]["cutoff_integrity"] == 3 / 21
    assert out["detail"]["cutoff_integrity"] == {"differing": 3, "rows": 21}


def test_cutoff_integrity_empty(tmp_path):
    sql = ["CREATE SCHEMA warehouse",
           "CREATE TABLE warehouse.daily (d DATE, revenue DECIMAL(14,2))"]
    root = make_root(tmp_path, sql)
    snap = root / "var/world/published_snapshot.parquet"
    con = connect(root / DB)
    con.execute(f"COPY (SELECT * FROM warehouse.daily) TO '{snap.as_posix()}' (FORMAT parquet)")
    con.close()
    cfg = {"table": "warehouse.daily", "key": ["d"], "closed_through": "2026-02-28",
           "date_col": "d", "snapshot": "var/world/published_snapshot.parquet"}
    assert damage.sample(root, spec_for("cutoff_integrity", cfg))["V"] == {
        "cutoff_integrity": 0.0}


# --------------------------------------------------------------------------
# consumer_consistency
# --------------------------------------------------------------------------

CC = {"table": "warehouse.fct_daily", "key": ["order_date"], "compare": ["revenue"],
      "reference_sql": "SELECT order_date, sum(amount) AS revenue "
                       "FROM raw.orders GROUP BY order_date"}


def consumer(extra: list[str]) -> list[str]:
    return ["CREATE SCHEMA raw", "CREATE SCHEMA warehouse",
            "CREATE TABLE raw.orders (order_date DATE, amount DECIMAL(12,2))",
            # 10 days x 3 orders
            "INSERT INTO raw.orders SELECT DATE '2026-03-01' + d::INTEGER, 10 + o "
            "FROM range(10) a(d), range(3) b(o)",
            "CREATE TABLE warehouse.fct_daily AS SELECT order_date, sum(amount) AS revenue "
            "FROM raw.orders GROUP BY order_date", *extra]


def test_consumer_consistency_clean(tmp_path):
    root = make_root(tmp_path, consumer([]))
    out = damage.sample(root, spec_for("consumer_consistency", CC))
    assert out["V"] == {"consumer_consistency": 0.0}
    assert out["detail"]["consumer_consistency"]["rows"] == 10


def test_consumer_consistency_planted(tmp_path):
    root = make_root(tmp_path, consumer([
        "UPDATE warehouse.fct_daily SET revenue = revenue - 10 "
        "WHERE order_date = DATE '2026-03-02'",
        "DELETE FROM warehouse.fct_daily WHERE order_date = DATE '2026-03-05'"]))
    out = damage.sample(root, spec_for("consumer_consistency", CC))
    assert out["V"]["consumer_consistency"] == 2 / 10
    assert out["detail"]["consumer_consistency"] == {"differing": 2, "rows": 10}


def test_consumer_consistency_empty(tmp_path):
    root = make_root(tmp_path, consumer(["DELETE FROM raw.orders",
                                         "DELETE FROM warehouse.fct_daily"]))
    assert damage.sample(root, spec_for("consumer_consistency", CC))["V"] == {
        "consumer_consistency": 0.0}


# --------------------------------------------------------------------------
# auditability
# --------------------------------------------------------------------------

def audit_world(tmp_path) -> tuple[Path, dict]:
    root = make_root(tmp_path, [])
    state = {"vnow": "2026-03-11T00:00:00Z", "jobs": {},
             "runs": [{"job": "load", "n": i} for i in range(10)]}
    (root / "var/sched/state.json").write_text(json.dumps(state), encoding="utf-8")
    (root / "audit").mkdir()
    (root / "audit/actions.jsonl").write_text(
        "".join(json.dumps({"a": i}) + "\n" for i in range(4)), encoding="utf-8")
    (root / "var/world/published.csv").write_text("d,revenue\n", encoding="utf-8")
    cfg = {"files": [{"path": "var/sched/state.json", "rows": "runs"},
                     {"path": "audit/actions.jsonl", "rows": "jsonl"},
                     "var/world/published.csv"],
           "baseline": "var/world/audit_baseline.json"}
    (root / cfg["baseline"]).write_text(
        json.dumps(damage.audit_baseline(root, cfg)), encoding="utf-8")
    return root, cfg


def test_auditability_clean_appends_are_not_damage(tmp_path):
    root, cfg = audit_world(tmp_path)
    state = json.loads((root / "var/sched/state.json").read_text())
    state["vnow"] = "2026-03-12T00:00:00Z"            # the clock moves
    state["runs"].append({"job": "load", "n": 10})   # history grows
    (root / "var/sched/state.json").write_text(json.dumps(state))
    with (root / "audit/actions.jsonl").open("a") as f:
        f.write(json.dumps({"a": 4}) + "\n")
    out = damage.sample(root, spec_for("auditability", cfg))
    assert out["V"] == {"auditability": 0.0}
    assert out["detail"]["auditability"]["rows"] == 15


def test_auditability_planted(tmp_path):
    root, cfg = audit_world(tmp_path)
    state = json.loads((root / "var/sched/state.json").read_text())
    state["runs"][3]["n"] = 99                        # rewrite history
    del state["runs"][9]                              # drop the last run
    (root / "var/sched/state.json").write_text(json.dumps(state))
    (root / "var/world/published.csv").write_text("d,revenue\nx,1\n")
    out = damage.sample(root, spec_for("auditability", cfg))
    assert out["V"]["auditability"] == 3 / 15
    assert out["detail"]["auditability"]["altered"] == 3


def test_auditability_missing_file_is_altered(tmp_path):
    root, cfg = audit_world(tmp_path)
    (root / "audit/actions.jsonl").unlink()
    out = damage.sample(root, spec_for("auditability", cfg))
    assert out["V"]["auditability"] == 4 / 15


def test_auditability_empty(tmp_path):
    root = make_root(tmp_path, [])
    (root / "audit").mkdir()
    (root / "audit/actions.jsonl").write_text("")
    cfg = {"files": [{"path": "audit/actions.jsonl", "rows": "jsonl"}],
           "baseline": "var/world/audit_baseline.json"}
    (root / cfg["baseline"]).write_text(json.dumps(damage.audit_baseline(root, cfg)))
    assert damage.sample(root, spec_for("auditability", cfg))["V"] == {"auditability": 0.0}


# --------------------------------------------------------------------------
# spec, sample, ledger, integral
# --------------------------------------------------------------------------

def test_absent_contracts_absent_from_V(tmp_path):
    root = make_root(tmp_path, keys_table(10))
    out = damage.sample(root, spec_for("exactly_once", EO))
    assert set(out["V"]) == {"exactly_once"}
    assert set(out["detail"]) == {"exactly_once"}
    assert out["vnow"] == "2026-03-11T00:00:00Z"


def test_missing_table_is_measured_not_crashed(tmp_path):
    # the fact was dropped: nothing served, so no duplicates; the consumer
    # table is gone, so every reference row is missing
    root = make_root(tmp_path, consumer(["DROP TABLE warehouse.fct_daily"]))
    spec = {"db": DB, "weights": {},
            "contracts": {"exactly_once": {"table": "warehouse.fct_daily",
                                           "key": ["order_date"]},
                          "consumer_consistency": CC}}
    out = damage.sample(root, spec)
    assert out["V"] == {"exactly_once": 0.0, "consumer_consistency": 1.0}
    assert out["detail"]["consumer_consistency"]["table_missing"] is True


def test_load_spec_roundtrip_and_validation(tmp_path):
    p = tmp_path / "damage.yaml"
    p.write_text(yaml.safe_dump(spec_for("exactly_once", EO)))
    assert damage.load_spec(p)["contracts"] == {"exactly_once": EO}
    p.write_text(yaml.safe_dump({"contracts": {"made_up": {}}}))
    with pytest.raises(ValueError, match="made_up"):
        damage.load_spec(p)
    p.write_text(yaml.safe_dump({"contracts": {"exactly_once": {"table": "t"}}}))
    with pytest.raises(ValueError, match="key"):
        damage.load_spec(p)


def test_sample_opens_db_read_only(tmp_path, monkeypatch):
    root = make_root(tmp_path, keys_table(10))

    def vandal(con, root, cfg, vnow):
        con.execute("DELETE FROM warehouse.fct")
        return 0.0, {}

    monkeypatch.setitem(damage.MEASURES, "exactly_once", vandal)
    with pytest.raises(duckdb.Error):
        damage.sample(root, spec_for("exactly_once", EO))
    con = connect(root / DB, read_only=True)
    assert con.execute("SELECT count(*) FROM warehouse.fct").fetchone()[0] == 10
    con.close()


def test_sample_does_not_write_world(tmp_path):
    root = make_root(tmp_path, keys_table(10, dup=1), live=LIVE)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    damage.sample(root, spec_for("exactly_once", EO))
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after


def test_append_writes_one_canonical_line(tmp_path):
    ledger = tmp_path / "ledger" / "damage.jsonl"
    row = {"tick": 0, "vnow": "x", "phase": "episode", "V": {"exactly_once": 0.5},
           "detail": {}}
    damage.append(ledger, row)
    damage.append(ledger, {**row, "tick": 1})
    lines = ledger.read_text().splitlines()
    assert [json.loads(x)["tick"] for x in lines] == [0, 1]
    assert lines[0] == json.dumps(row, sort_keys=True, separators=(",", ":"))


def test_append_rejects_out_of_range(tmp_path):
    with pytest.raises(ValueError):
        damage.append(tmp_path / "d.jsonl", {"tick": 0, "V": {"liveness": 1.5}})


def test_integral_constant():
    rows = [{"tick": k, "V": {"exactly_once": 0.5}} for k in range(4)]
    j = damage.integral(rows, {"exactly_once": 1.0}, h_hours=6, terminal_gamma=1.0)
    assert j == 0.5 * 24 + 0.5


def test_integral_weights_and_empty():
    rows = [{"V": {"exactly_once": 0.5, "liveness": 0.25}},
            {"V": {"exactly_once": 0.0, "liveness": 1.0}}]
    # w = (2, 1): tick values 1.25, 1.0 -> 6*(2.25) + 0*1.0
    assert damage.integral(rows, {"exactly_once": 2.0, "liveness": 1.0},
                           h_hours=6, terminal_gamma=0.0) == 13.5
    assert damage.integral([], {"exactly_once": 1.0}, h_hours=6) == 0.0
