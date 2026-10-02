"""The `files` slot: the user files an analyst is actually handed.

Opt-in from the seed, on the same terms as `faults:`, `warehouses:` and
`native:`:

    files: {formats: [parquet, json, jsonl]}

A seed without the key writes nothing here and keeps every recorded
fingerprint. With it, the build writes, into the workspace and before the git
commit (they are files somebody checked in, so they are in the repository):

    workspace/data/exports/<raw table>/part-00000.parquet   parquet
    workspace/data/exports/<raw table>/part-00001.parquet
    workspace/data/api/<subject>.json                       json
    workspace/data/events/<subject>.jsonl                   jsonl

Every one is DERIVED from the theme's `raw` schema, which is what makes this
theme-agnostic: no theme declares anything, and the files cannot disagree
with the warehouse they claim to be an export of. The SUBJECT is the first raw
table (by name) that has a money column - one whose values are decimals - and
it is the only table the API dump and the event log are cut from.

Each format carries ONE planted pathology. They are documented here and
nowhere the agent can read, because a README that says "the amount column
drifts" is a hint, and a file that is clean is not what anybody is handed:

- parquet: the subject's money column is DECIMAL(18,2) in part-00000 and
  DOUBLE in part-00001 - the second export went through a dataframe that had
  already turned it into a float. `read_parquet([...])` over both parts either
  refuses or silently unifies to DOUBLE, depending on the reader.
- json: a JSON:API-shaped dump (`data[]` of `{type, id, attributes,
  relationships}`). `relationships` is OPTIONAL and the API omits it, rather
  than nulling it, on about 2% of records - so code that indexes
  `rec["relationships"]["customer"]` works on the first page it is tried on
  and raises KeyError on the full file. A subject with no `*_id` column drops
  its last attribute instead.
- jsonl: one event redelivered (same `event_id`, identical payload) two lines
  after the original, which also puts it out of order, plus one adjacent pair
  of events swapped. At-least-once delivery, unordered, as event logs are.

Determinism. Every value is a function of the warehouse the build already
wrote and of `Random(f"{seed}:files:<format>")` - the per-purpose PRNG rule
the theme generators follow. JSON is written with fixed key order,
`ensure_ascii`, LF. Parquet goes through DuckDB's `COPY` with `PRAGMA
threads=1` and an `ORDER BY` over every column; DuckDB's footer carries its own
version string (`created_by`), which is a function of the locked dependency,
not of the run - the same reason `warehouse.duckdb`'s raw bytes reproduce on
one host. `test_file_formats.py` builds twice and compares the bytes.
"""
import json
import random
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb

#: Canonical order; a seed listing them in another order writes the same files.
FORMATS = ("parquet", "json", "jsonl")
EXPORTS = Path("data") / "exports"
API = Path("data") / "api"
EVENTS = Path("data") / "events"


def resolve(seed: dict) -> list[str] | None:
    """The formats a seed asks for, canonically ordered; None when it asks
    for none. Loud on anything malformed, because a typo'd format that wrote
    nothing would be a task whose files silently are not there."""
    if "files" not in seed:
        return None
    spec = seed["files"]
    if not isinstance(spec, dict) or set(spec) != {"formats"}:
        raise SystemExit("seed `files:` must be a mapping with exactly one key, "
                         "`formats: [parquet | json | jsonl, ...]`")
    formats = spec["formats"]
    if not isinstance(formats, list) or not formats:
        raise SystemExit("seed `files: {formats:}` must be a non-empty list")
    unknown = [f for f in formats if f not in FORMATS]
    if unknown:
        raise SystemExit(f"seed `files:` names unknown format(s) {unknown!r} "
                         f"(known: {', '.join(FORMATS)})")
    if len(set(formats)) != len(formats):
        raise SystemExit("seed `files: {formats:}` names a format twice")
    return [f for f in FORMATS if f in formats]


# --------------------------------------------------------------------------
# the subject table
# --------------------------------------------------------------------------

def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _connect(db_path: Path):
    """In-memory engine with the warehouse ATTACHed read-only under an alias -
    never opened unprefixed (README, "The catalog/schema collision")."""
    con = duckdb.connect()
    con.execute("PRAGMA threads=1")
    con.execute(f"ATTACH '{db_path.as_posix()}' AS wh (READ_ONLY)")
    return con


def _raw_tables(con) -> dict[str, list[tuple[str, str]]]:
    rows = con.execute(
        "select table_name, column_name, data_type from information_schema.columns "
        "where table_catalog = 'wh' and table_schema = 'raw' "
        "order by table_name, ordinal_position").fetchall()
    tables: dict[str, list[tuple[str, str]]] = {}
    for table, column, dtype in rows:
        tables.setdefault(table, []).append((column, dtype))
    return tables


def _money_column(con, table: str, cols) -> str | None:
    """The first column after the key that holds decimals: a DECIMAL/DOUBLE,
    or a VARCHAR every non-null value of which casts to DECIMAL(18,2) and at
    least one of which has a decimal point (so an account number is not
    money)."""
    for column, dtype in cols[1:]:
        upper = dtype.upper()
        if upper.startswith(("DECIMAL", "DOUBLE", "FLOAT", "REAL")):
            return column
        if upper != "VARCHAR":
            continue
        c = _q(column)
        present, casts, dotted = con.execute(
            f"select count({c}), count(try_cast({c} as decimal(18,2))), "
            f"coalesce(bool_or({c} like '%.%'), false) from wh.raw.{_q(table)}"
        ).fetchone()
        if present and present == casts and dotted:
            return column
    return None


def _subject(con) -> tuple[str, list[tuple[str, str]], str]:
    tables = _raw_tables(con)
    if not tables:
        raise SystemExit("seed asks for `files:` but the theme loaded no raw tables")
    for table in sorted(tables):
        money = _money_column(con, table, tables[table])
        if money is not None:
            return table, tables[table], money
    raise SystemExit(
        "seed asks for `files:` but no raw table has a money column to plant the "
        f"parquet type drift on (raw tables: {', '.join(sorted(tables))})")


def _order(cols) -> str:
    return ", ".join(_q(c) for c, _ in cols)


def _rows(con, table: str, cols) -> list[dict]:
    cur = con.execute(f"select * from wh.raw.{_q(table)} order by {_order(cols)}")
    names = [c for c, _ in cols]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _jsonable(value):
    """Money as a string (`"123.45"`), the way APIs that care about cents
    send it; dates ISO 8601. Never a float out of a Decimal."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


# --------------------------------------------------------------------------
# writers
# --------------------------------------------------------------------------

def _write_parquet(con, workspace: Path, tables: dict, subject: str, money: str) -> list[Path]:
    """Every raw table as two halves; the subject's money column drifts."""
    written = []
    for table in sorted(tables):
        cols = tables[table]
        order = _order(cols)
        (count,) = con.execute(f"select count(*) from wh.raw.{_q(table)}").fetchone()
        half = (count + 1) // 2
        out = workspace / EXPORTS / table
        out.mkdir(parents=True, exist_ok=True)
        for part, (cast, window) in enumerate((
                ("DECIMAL(18,2)", f"limit {half}"),
                ("DOUBLE", f"limit {count - half} offset {half}"))):
            select = ", ".join(
                f"cast({_q(c)} as {cast}) as {_q(c)}"
                if table == subject and c == money else _q(c)
                for c, _ in cols)
            path = out / f"part-{part:05d}.parquet"
            con.execute(
                f"copy (select {select} from wh.raw.{_q(table)} order by {order} "
                f"{window}) to '{path.as_posix()}' (format parquet)")
            written.append(path)
    return written


def _split(cols):
    """(key, references, attributes): column 0 is the record id; `<x>_id`
    columns are relationships to `<x>`; `_`-prefixed loader bookkeeping
    (`_source_file`) is not something an API returns."""
    key = cols[0][0]
    refs = [c for c, _ in cols[1:] if c.endswith("_id") and not c.startswith("_")]
    attrs = [c for c, _ in cols[1:] if c not in refs and not c.startswith("_")]
    return key, refs, attrs


def _write_json(workspace: Path, seed: dict, table: str, cols, rows, vnow: str) -> Path:
    key, refs, attrs = _split(cols)
    rng = random.Random(f"{seed['seed']}:files:json")
    omit = set(rng.sample(range(len(rows)), max(1, len(rows) // 50)))
    records = []
    for i, row in enumerate(rows):
        rec = {"type": table, "id": str(_jsonable(row[key])),
               "attributes": {c: _jsonable(row[c]) for c in attrs}}
        if refs:
            rec["relationships"] = {
                c[:-3]: {"data": {"type": c[:-3], "id": _jsonable(row[c])}} for c in refs}
            if i in omit:
                del rec["relationships"]
        elif i in omit and attrs:
            del rec["attributes"][attrs[-1]]
        records.append(rec)
    doc = {"data": records,
           "meta": {"count": len(records), "fetched_at": vnow},
           "links": {"self": f"/api/v1/{table}?page[size]={len(records)}", "next": None}}
    path = workspace / API / f"{table}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((_dump(doc) + "\n").encode("ascii"))
    return path


def _date_column(con, table: str, cols) -> str | None:
    for column, dtype in cols[1:]:
        upper = dtype.upper()
        if upper == "DATE" or upper.startswith("TIMESTAMP"):
            return column
        if upper == "VARCHAR":
            c = _q(column)
            present, casts = con.execute(
                f"select count({c}), count(try_cast({c} as date)) "
                f"from wh.raw.{_q(table)}").fetchone()
            if present and present == casts:
                return column
    return None


def _write_jsonl(con, workspace: Path, seed: dict, table: str, cols, rows) -> Path:
    when = _date_column(con, table, cols)
    start = datetime.strptime(seed["vtime_start"], "%Y-%m-%dT%H:%M:%SZ")
    rng = random.Random(f"{seed['seed']}:files:jsonl")
    stamped = []
    for i, row in enumerate(rows):
        if when is not None and row[when] is not None:
            day = row[when] if isinstance(row[when], date) else date.fromisoformat(
                str(row[when])[:10])
            at = datetime.combine(day, datetime.min.time()) + timedelta(
                seconds=rng.randrange(86400))
        else:
            at = start + timedelta(minutes=i)
        stamped.append((at, i, row))
    stamped.sort(key=lambda t: (t[0], t[1]))
    events = [{
        "event_id": f"evt_{n:07d}",
        "event_type": f"{table}.created",
        "occurred_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": {c: _jsonable(v) for c, v in row.items() if not c.startswith("_")},
    } for n, (at, _, row) in enumerate(stamped, start=1)]
    # The pathology. Indices drawn from their own PRNG so the timestamps above
    # never depend on it; the swap is placed in the second half and the
    # redelivery in the first so the two cannot overlap.
    pick = random.Random(f"{seed['seed']}:files:jsonl:pathology")
    n = len(events)
    if n >= 8:
        s = pick.randrange(n // 2, n - 1)
        while s < n - 1 and events[s]["occurred_at"] == events[s + 1]["occurred_at"]:
            s += 1
        if s < n - 1:
            events[s], events[s + 1] = events[s + 1], events[s]
        d = pick.randrange(0, n // 2 - 3)
        events.insert(d + 3, dict(events[d]))
    path = workspace / EVENTS / f"{table}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("".join(_dump(e) + "\n" for e in events).encode("ascii"))
    return path


def write(root: Path, seed: dict, formats: list[str], vnow: str) -> list[Path]:
    """Write the requested formats. Returns the paths, in the order written."""
    root = Path(root)
    workspace = root / "workspace"
    con = _connect(root / "var" / "warehouse" / "warehouse.duckdb")
    try:
        tables = _raw_tables(con)
        subject, cols, money = _subject(con)
        rows = _rows(con, subject, cols)
        written = []
        if "parquet" in formats:
            written += _write_parquet(con, workspace, tables, subject, money)
        if "json" in formats:
            written.append(_write_json(workspace, seed, subject, cols, rows, vnow))
        if "jsonl" in formats:
            written.append(_write_jsonl(con, workspace, seed, subject, cols, rows))
    finally:
        con.close()
    return written
