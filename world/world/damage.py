"""Damage monitor: one exact, normalised measure V_c(W_t) in [0, 1] per contract.

Spec: docs/implementation_plan/experimental_ideas/2026-09-30-dynamic-world-design.md
(section 2, the damage table; section 4.3). A world declares which contracts it
measures, and on which tables / files, in its `damage.yaml`:

    db: var/warehouse/warehouse.duckdb       # relative to the world root
    weights: {exactly_once: 1.0, ...}         # a contract without a weight weighs 1.0
    contracts:
      exactly_once:         {table, key: [..]}
      liveness:             {table, partition_col, arrival, grace_hours}
      version_precedence:   {table, key: [..], version_col, source_sql}
      cutoff_integrity:     {table, key: [..], closed_through, date_col, snapshot}
      consumer_consistency: {table, key: [..], compare: [..], reference_sql}
      auditability:         {files: [path | {path, rows}], baseline}

Rules this module keeps:
- Measured, never asserted: every number is a count over the world as it is.
  Each V_c is an exact ratio of two integers; its docstring says which.
- Read-only: the warehouse is opened with `read_only=True` and nothing under the
  world root is written. `append` writes only the ledger it is handed.
- An empty denominator is 0.0 damage, never a ZeroDivisionError.
- A table the spec names but the world no longer has is a state, not an error:
  it is measured as an empty relation (and flagged `table_missing`).
- A contract absent from the spec is absent from V.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import duckdb
import yaml

from . import scheduler
from .hashing import json_canon, sha256_bytes

CONTRACTS = ("exactly_once", "liveness", "version_precedence",
             "cutoff_integrity", "consumer_consistency", "auditability")

DEFAULT_DB = "var/warehouse/warehouse.duckdb"

REQUIRED = {
    "exactly_once": ("table", "key"),
    "liveness": ("table", "partition_col", "arrival"),
    "version_precedence": ("table", "key", "version_col", "source_sql"),
    "cutoff_integrity": ("table", "key", "closed_through", "date_col", "snapshot"),
    "consumer_consistency": ("table", "key", "compare", "reference_sql"),
    "auditability": ("files", "baseline"),
}

MAX_DAYS = 24 * 366      # liveness: refuse to enumerate an unbounded stream


# --------------------------------------------------------------------------
# spec
# --------------------------------------------------------------------------

def load_spec(path: Path) -> dict:
    """Read and validate a damage.yaml. Raises ValueError naming the problem."""
    spec = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return validate(spec)


def validate(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("damage spec must be a mapping")
    contracts = spec.get("contracts") or {}
    if not isinstance(contracts, dict):
        raise ValueError("damage spec `contracts` must be a mapping")
    for name, cfg in contracts.items():
        if name not in CONTRACTS:
            raise ValueError(f"unknown contract {name!r}; expected one of {CONTRACTS}")
        missing = [k for k in REQUIRED[name] if k not in (cfg or {})]
        if missing:
            raise ValueError(f"contract {name!r} is missing {', '.join(missing)}")
        for k in ("key", "compare"):
            if k in cfg and (not isinstance(cfg[k], list) or not cfg[k]):
                raise ValueError(f"contract {name!r}: `{k}` must be a non-empty list")
    for name in spec.get("weights") or {}:
        if name not in CONTRACTS:
            raise ValueError(f"weight for unknown contract {name!r}")
    return {"db": spec.get("db", DEFAULT_DB), "weights": dict(spec.get("weights") or {}),
            "contracts": dict(contracts)}


# --------------------------------------------------------------------------
# SQL helpers
# --------------------------------------------------------------------------

def _open(db: Path):
    """The warehouse, read-only, attached under the neutral catalog `wh`.

    Opening warehouse.duckdb directly names the catalog after the file stem,
    which collides with the `warehouse` schema and makes every two-part
    `warehouse.<table>` reference ambiguous (world/world/bi.py, GT002's
    warehouse_conn.py). ATTACH ... (READ_ONLY) keeps the read-only guarantee:
    any write to a `wh` object raises.
    """
    if not Path(db).is_file():
        raise FileNotFoundError(f"damage: no warehouse at {db}")
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh (READ_ONLY)")
    con.execute("USE wh")
    return con


def _ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table(name: str) -> str:
    return ".".join(_ident(p) for p in str(name).split("."))


def _cols(cols) -> str:
    return ", ".join(_ident(c) for c in cols)


def _exists(con, name: str) -> bool:
    """Whether `[db.][schema.]table` names a table or view (schema defaults to main)."""
    parts = str(name).split(".")
    table = parts[-1]
    schema = parts[-2] if len(parts) >= 2 else "main"
    db = parts[0] if len(parts) == 3 else con.execute(
        "SELECT current_database()").fetchone()[0]
    q = ("SELECT count(*) FROM ("
         "SELECT database_name, schema_name, table_name AS n FROM duckdb_tables() "
         "UNION ALL SELECT database_name, schema_name, view_name FROM duckdb_views()) "
         "WHERE database_name = ? AND schema_name = ? AND n = ?")
    return con.execute(q, [db, schema, table]).fetchone()[0] > 0


def _one(con, sql: str, params=None) -> int:
    return int(con.execute(sql, params or []).fetchone()[0])


def _ratio(num: int, den: int) -> float:
    return 0.0 if den == 0 else num / den


def _keyed_diff(con, left: str | None, right: str | None, key: list) -> tuple[int, int]:
    """(differing keys, keys) between two relations over the same columns.

    `left` / `right` are SELECTs projecting identical column lists that include
    `key`; None stands for a relation that does not exist (empty). A key differs
    when the MULTISET of its rows differs between the sides (EXCEPT ALL, so
    NULLs compare equal and duplicates count). keys = |distinct key tuples in
    left ∪ right|; differing <= keys, so the ratio is in [0, 1].
    """
    k = _cols(key)
    sides = [s for s in (left, right) if s is not None]
    if not sides:
        return 0, 0
    union = " UNION ALL ".join(f"SELECT {k} FROM ({s})" for s in sides)
    keys = _one(con, f"SELECT count(*) FROM (SELECT DISTINCT {k} FROM ({union}))")
    if left is None or right is None:
        return keys, keys
    differing = _one(con, f"""
        SELECT count(*) FROM (SELECT DISTINCT {k} FROM (
            (SELECT * FROM ({left}) EXCEPT ALL SELECT * FROM ({right}))
            UNION ALL
            (SELECT * FROM ({right}) EXCEPT ALL SELECT * FROM ({left}))))""")
    return differing, keys


# --------------------------------------------------------------------------
# the six measures: _v_<contract>(con, root, cfg, vnow) -> (V, detail)
# --------------------------------------------------------------------------

def _v_exactly_once(con, root: Path, cfg: dict, vnow: str):
    """V = dup_keys / keys.

    keys     = number of distinct business-key tuples in `table`;
    dup_keys = number of those tuples carried by more than one row.
    A missing table serves nothing, so it has no duplicates (V = 0).
    """
    if not _exists(con, cfg["table"]):
        return 0.0, {"dup_keys": 0, "keys": 0, "rows": 0, "table_missing": True}
    k, t = _cols(cfg["key"]), _table(cfg["table"])
    keys, dup, rows = con.execute(
        f"SELECT count(*), coalesce(sum(CASE WHEN n > 1 THEN 1 ELSE 0 END), 0), "
        f"coalesce(sum(n), 0) FROM (SELECT count(*) AS n FROM {t} GROUP BY {k})"
    ).fetchone()
    return _ratio(int(dup), int(keys)), {"dup_keys": int(dup), "keys": int(keys),
                                         "rows": int(rows)}


def _due_partitions(root: Path, cfg: dict, vnow: str) -> list[str]:
    """Partition values due by `vnow - grace_hours`, from the arrival stream.

    Day i of the stream (live.json `arrival`, counted from `vtime_start`, the
    same indexing `scheduler._arrival_at` uses to land it) carries the partition
    `(vtime_start + i days).date()` and is due at `_arrival_at(live, i) + grace`.
    Every day from i = 0 counts, including the seeded history.
    """
    live = scheduler.load_live(root)
    if not live or not live.get("arrival") or "vtime_start" not in live:
        raise ValueError("liveness needs var/world/live.json with an arrival stream "
                         "and vtime_start")
    name = live["arrival"].get("name", "arrival")
    if name != cfg["arrival"]:
        raise ValueError(f"liveness names arrival {cfg['arrival']!r}; "
                         f"the world's stream is {name!r}")
    cutoff = scheduler.parse_vtime(vnow) - timedelta(hours=float(cfg.get("grace_hours", 0)))
    start = scheduler.parse_vtime(live["vtime_start"])
    fmt = cfg.get("partition_format", "%Y-%m-%d")
    due, i = [], 0
    while scheduler._arrival_at(live, i) <= cutoff:
        if i >= MAX_DAYS:
            raise ValueError("liveness: more than MAX_DAYS partitions due")
        due.append((start + timedelta(days=i)).strftime(fmt))
        i += 1
    return due


def _v_liveness(con, root: Path, cfg: dict, vnow: str):
    """V = missing / due.

    due     = partitions whose arrival time + grace_hours <= vnow (see
              `_due_partitions`);
    missing = due partitions with no row in `table` whose
              CAST(partition_col AS VARCHAR) equals the partition value.
    Measured on the table, not the landing zone: landed-but-not-loaded is absent.
    """
    due = _due_partitions(root, cfg, vnow)
    present: set[str] = set()
    missing_table = not _exists(con, cfg["table"])
    if not missing_table:
        present = {r[0] for r in con.execute(
            f"SELECT DISTINCT CAST({_ident(cfg['partition_col'])} AS VARCHAR) "
            f"FROM {_table(cfg['table'])}").fetchall()}
    missing = [p for p in due if p not in present]
    detail = {"due": len(due), "missing": len(missing), "missing_partitions": missing}
    if missing_table:
        detail["table_missing"] = True
    return _ratio(len(missing), len(due)), detail


def _v_version_precedence(con, root: Path, cfg: dict, vnow: str):
    """V = stale_keys / keys.

    keys       = distinct key tuples served by `table`;
    stale_keys = served keys with at least one served row whose `version_col`
                 IS DISTINCT FROM the max `version_col` delivered for that key
                 by `source_sql` (no delivery at all => NULL max => stale).
    Delivered keys the table does not serve are reported (`unserved_keys`) but
    are not version damage: absence is liveness / consumer_consistency damage.
    """
    k, v = _cols(cfg["key"]), _ident(cfg["version_col"])
    delivered = f"SELECT {k}, max({v}) AS __max FROM ({cfg['source_sql']}) GROUP BY {k}"
    if not _exists(con, cfg["table"]):
        unserved = _one(con, f"SELECT count(*) FROM ({delivered})")
        return 0.0, {"stale_keys": 0, "keys": 0, "unserved_keys": unserved,
                     "table_missing": True}
    served = f"SELECT {k}, {v} FROM {_table(cfg['table'])}"
    on = " AND ".join(f"s.{_ident(c)} IS NOT DISTINCT FROM d.{_ident(c)}"
                      for c in cfg["key"])
    sk = ", ".join(f"s.{_ident(c)}" for c in cfg["key"])
    keys = _one(con, f"SELECT count(*) FROM (SELECT DISTINCT {k} FROM ({served}))")
    stale = _one(con, f"""
        SELECT count(*) FROM (SELECT DISTINCT {sk} FROM ({served}) s
        LEFT JOIN ({delivered}) d ON {on}
        WHERE s.{v} IS DISTINCT FROM d.__max)""")
    unserved = _one(con, f"""
        SELECT count(*) FROM ({delivered}) d
        WHERE NOT EXISTS (SELECT 1 FROM ({served}) s WHERE {on})""")
    return _ratio(stale, keys), {"stale_keys": stale, "keys": keys,
                                 "unserved_keys": unserved}


def _v_cutoff_integrity(con, root: Path, cfg: dict, vnow: str):
    """V = differing / rows, over the closed period.

    The closed period is CAST(date_col AS DATE) <= closed_through, applied to
    both the published `snapshot` (parquet, relative to the world root) and
    `table`, projected on the snapshot's columns.
    rows      = distinct key tuples in snapshot ∪ table within the period;
    differing = those whose row multiset differs between the two (an edited,
                deleted or inserted closed-period row each count once).
    """
    snap = (Path(root) / cfg["snapshot"]).resolve().as_posix().replace("'", "''")
    src = f"read_parquet('{snap}')"
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]
    proj = _cols(cols)
    where = (f"WHERE CAST({_ident(cfg['date_col'])} AS DATE) "
             f"<= CAST('{cfg['closed_through']}' AS DATE)")
    left = f"SELECT {proj} FROM {src} {where}"
    missing = not _exists(con, cfg["table"])
    right = None if missing else f"SELECT {proj} FROM {_table(cfg['table'])} {where}"
    differing, rows = _keyed_diff(con, left, right, cfg["key"])
    detail = {"differing": differing, "rows": rows}
    if missing:
        detail["table_missing"] = True
    return _ratio(differing, rows), detail


def _v_consumer_consistency(con, root: Path, cfg: dict, vnow: str):
    """V = differing / rows.

    Both `table` and `reference_sql` are projected on key + compare columns.
    rows      = distinct key tuples in table ∪ reference;
    differing = those whose (key, compare...) row multiset differs: a value
                mismatch, a missing row, an extra row, or a duplicate each
                make the key differ once. Equality is exact (IS NOT DISTINCT).
    """
    proj = _cols(list(cfg["key"]) + list(cfg["compare"]))
    ref = f"SELECT {proj} FROM ({cfg['reference_sql']})"
    missing = not _exists(con, cfg["table"])
    served = None if missing else f"SELECT {proj} FROM {_table(cfg['table'])}"
    differing, rows = _keyed_diff(con, served, ref, cfg["key"])
    detail = {"differing": differing, "rows": rows}
    if missing:
        detail["table_missing"] = True
    return _ratio(differing, rows), detail


def _file_rows(path: Path, rows: str | None) -> list[str] | None:
    """Row hashes of one audited file; None if the file does not exist.

    rows=None    -> the whole file is one row (sha256 of its bytes);
    rows="jsonl" -> one row per non-empty line (sha256 of the line bytes);
    rows=<key>   -> one row per element of the top-level JSON list at <key>
                    (sha256 of its canonical JSON).
    """
    if not path.is_file():
        return None
    data = path.read_bytes()
    if rows is None:
        return [sha256_bytes(data)]
    if rows == "jsonl":
        return [sha256_bytes(line) for line in data.splitlines() if line.strip()]
    doc = json.loads(data.decode("utf-8"))
    return [sha256_bytes(json_canon(r)) for r in (doc.get(rows) or [])]


def _audit_entries(cfg: dict) -> list[tuple[str, str | None]]:
    out = []
    for f in cfg["files"]:
        out.append((f, None) if isinstance(f, str) else (f["path"], f.get("rows")))
    return out


def audit_baseline(root: Path, cfg: dict) -> dict:
    """The baseline document for an auditability cfg, computed from `root` now.

    Pure: returns it; the world author writes it to cfg["baseline"] at t = 0.
    """
    files = {}
    for path, rows in _audit_entries(cfg):
        hashes = _file_rows(Path(root) / path, rows)
        if hashes is None:
            raise ValueError(f"auditability baseline: {path} does not exist")
        files[path] = {"rows": rows, "sha256": hashes}
    return {"files": files}


def _v_auditability(con, root: Path, cfg: dict, vnow: str):
    """V = altered / rows.

    rows    = number of baseline rows over all audited files (see `_file_rows`);
    altered = baseline rows i whose current row i is absent or hashes
              differently. Rows appended after the baseline are not damage
              (history may grow; it may not be rewritten). A deleted file
              alters all its rows.
    """
    base = json.loads((Path(root) / cfg["baseline"]).read_text(encoding="utf-8"))["files"]
    total = altered = 0
    per_file = {}
    for path, rows in _audit_entries(cfg):
        if path not in base:
            raise ValueError(f"auditability: {path} has no baseline entry")
        then = base[path]["sha256"]
        now = _file_rows(Path(root) / path, rows) or []
        bad = sum(1 for i, h in enumerate(then) if i >= len(now) or now[i] != h)
        total += len(then)
        altered += bad
        per_file[path] = bad
    return _ratio(altered, total), {"altered": altered, "rows": total,
                                    "files": per_file}


MEASURES = {
    "exactly_once": _v_exactly_once,
    "liveness": _v_liveness,
    "version_precedence": _v_version_precedence,
    "cutoff_integrity": _v_cutoff_integrity,
    "consumer_consistency": _v_consumer_consistency,
    "auditability": _v_auditability,
}


# --------------------------------------------------------------------------
# sample / ledger / integral
# --------------------------------------------------------------------------

def sample(root: Path, spec: dict) -> dict:
    """{vnow, V: {contract: float}, detail: {contract: {...}}} for the world now.

    Only contracts named in the spec are measured; they are evaluated in
    CONTRACTS order. vnow is the scheduler's (var/sched/state.json).
    """
    root = Path(root)
    spec = validate(spec)
    vnow = scheduler.load(root)["vnow"]
    V, detail = {}, {}
    con = _open(root / spec["db"])
    try:
        for name in CONTRACTS:
            if name not in spec["contracts"]:
                continue
            v, d = MEASURES[name](con, root, spec["contracts"][name], vnow)
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"{name}: V={v} outside [0, 1]")
            V[name], detail[name] = float(v), d
    finally:
        con.close()
    return {"vnow": vnow, "V": V, "detail": detail}


def append(ledger: Path, row: dict) -> None:
    """Append one canonical-JSON line (sorted keys, tight separators) to `ledger`."""
    for name, v in (row.get("V") or {}).items():
        if name not in CONTRACTS or not 0.0 <= float(v) <= 1.0:
            raise ValueError(f"damage row has invalid V[{name!r}] = {v!r}")
    ledger = Path(ledger)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, sort_keys=True, separators=(",", ":"))
    with ledger.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def integral(rows: list[dict], weights: dict, h_hours: float,
             terminal_gamma: float = 1.0) -> float:
    """J = sum_k h_hours * w.V_k  +  terminal_gamma * w.V_last.

    The Riemann sum over the tick schedule (each row is the state after one
    tick of h_hours) plus the terminal term. A contract with no weight weighs
    1.0. No rows => 0.0.
    """
    def wv(row):
        return sum(float(weights.get(c, 1.0)) * float(v)
                   for c, v in (row.get("V") or {}).items())
    if not rows:
        return 0.0
    return h_hours * sum(wv(r) for r in rows) + terminal_gamma * wv(rows[-1])
