"""The clocked multi-night receipt comparison engine, with no theme in it.

Lifted from `tasks/GT009-settlement-cutoff/tests/grade.py` (`_canonical_hash`,
`_audit_restated_rule`, `reference_hashes`, `_night_spans`, `load_receipts`,
`receipts_by_night`, `classify`, `score_c0`) and made parameter-driven: every
table name, grain key, night count, clock origin and reference layout arrives in
a plain-dict `spec` (the declarative `tests/spec.json`). GT009 is one spec among
many; nothing here knows what a settlement is.

WHAT IS GRADED IS THE RECEIPTS. `world/world/ledger.py` appends one receipt at
the virtual instant each publication becomes visible; `replay_isolation.replay`
advances a COPY of the world over held-out nights and returns them. A night is
classified against every named reference trajectory (the oracle and the rivals:
competent pipelines that decided differently), and only the oracle pays - the
detail names the rival matched, so a zero says which decision was taken.

THE CANONICAL HASH IS RECOMPUTED HERE (`canonical_hash`), not imported from the
world: a task knows the world's layout and not its internals (agent.md 10.2),
and a restated rule is audited instead of trusted - `audit_hash_rule` requires a
receipt the world wrote to be reproduced byte for byte by this file, and every
hash verdict is reported beside that bit.

PROGRESS IS A PRODUCT QUANTITY. `progress_quantities` returns q0 (fraction of
t0 grain keys / rows repaired to the oracle t0 snapshot) and q1..qN (fraction of
night k's governed targets whose receipt carries the oracle's hash), the smooth
inputs `tasks/harness/progress.py` normalises. `score_t0` and `classify_night`
also expose them as numeric fields so a check's `detail` dict can carry them.

SPEC KEYS READ HERE (all others are ignored; see `replay_isolation` for the
replay-side keys):

    targets          list[str] | {role: table}. The governed publications
                     classified each night ("schema.table"). With the mapping
                     form each night's result also carries `result[role]` =
                     the trajectory that role's receipt matched.
    nights           int. Number of held-out nights (R1..RN).
    virtual_now      ISO-8601 t0 instant; night k spans
                     [virtual_now + (k-1)h, virtual_now + k*h).
    hours_per_night  float, the `h` above.
    ddl              {table: "CREATE TABLE ..."} - the frozen layout reference
                     rows are materialised at before hashing (the ledger hashes
                     the column list, so layout is part of the answer).
    trajectories     {
                       "dir": "expected",               # under <grading>
                       "names": ["oracle", "rival", ...],
                       "pays": "oracle",
                       "file": "variant_{name}.json",
                       "nights_key": "nights",          # list of night entries
                       "night_field": "night",          # 1-based night number
                       "t0_file": "t0.json",            # optional
                       "t0_blocks": ["t0", "oracle_t0"],
                       "aliases": {"fct": "warehouse.fct_payments"},
                       "hash_tables": [...]             # extra tables hashed
                     }
                     A night entry is {night_field: k, table: rows, ...}; rows
                     are positional lists in the DDL's column order.
    t0_tables        [{"table": str,
                       "grain_key": [col name | col index, ...] (optional),
                       "reference": "oracle_t0",        # a t0 block name
                       "mode": "exact" | "superset",    # superset: every
                                                        # reference row kept
                       "weight": 1.0,
                       "groups": {"column": name|index, "values": [...],
                                  "field": "gap_days_handed_over"}}]
                     The t0 warehouse-repair check (C0). `groups` counts the
                     listed values whose row block equals the reference's and
                     reports it under `field` (GT009's progress quantity).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[3]
HARNESS_LEDGER = REPO / "tasks" / "harness" / "ledger.py"


class SpecError(ValueError):
    """The spec does not say what this engine needs to know. Raised, never
    defaulted: a missing table name guessed at is a grader of another task."""


def first_line(value, limit: int = 220) -> str | None:
    if value is None:
        return None
    text = str(value).strip().splitlines()
    return text[0][:limit] if text else None


# ===========================================================================
# spec accessors
# ===========================================================================
def targets_of(spec: dict) -> list[str]:
    raw = spec.get("targets")
    if isinstance(raw, dict):
        return [str(t) for t in raw.values()]
    if isinstance(raw, (list, tuple)) and raw:
        return [str(t) for t in raw]
    raise SpecError("spec['targets'] must be a non-empty list or {role: table}")


def roles_of(spec: dict) -> dict:
    """{table: role} when `targets` is the mapping form, else {}."""
    raw = spec.get("targets")
    return {str(t): str(r) for r, t in raw.items()} if isinstance(raw, dict) else {}


def _traj(spec: dict) -> dict:
    raw = spec.get("trajectories")
    if not isinstance(raw, dict) or not raw.get("names"):
        raise SpecError("spec['trajectories'] must name at least one trajectory")
    return raw


def trajectory_names(spec: dict) -> list[str]:
    return [str(n) for n in _traj(spec)["names"]]


def paying(spec: dict) -> str:
    return str(_traj(spec).get("pays", "oracle"))


def _require(spec: dict, key: str):
    if key not in spec:
        raise SpecError(f"spec[{key!r}] is required")
    return spec[key]


# ===========================================================================
# the canonical hash, restated from world/README.md#publication-ledger
# ===========================================================================
def _json_canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _canon_value(value):
    if value is None or isinstance(value, (int, str, bool)):
        return value
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"uncanonicalizable value: {type(value)!r}")


def _split(target: str) -> tuple[str, str]:
    """(schema, table) - a three-part name hashes under its last two parts,
    which is what the world ledger writes into the hash header."""
    parts = target.split(".")
    if len(parts) < 2:
        raise SpecError(f"target {target!r} is not schema.table")
    return parts[-2], parts[-1]


def canonical_hash(con, target: str) -> dict:
    """sha256 over (schema.table, columns) then one canonical row per line,
    rows ordered by every column in ordinal position - the ledger's rule.

    `columns` and every canonical `rows` are kept beside the hash so a receipt
    that matches no trajectory can be READ against the oracle's table
    (`name_difference`) instead of guessed at from a count and a sample."""
    schema, table = _split(target)
    cols = con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
        [schema, table]).fetchall()
    if not cols:
        return {"canonical_hash": None, "row_count": None, "sample": None,
                "columns": [], "rows": None}
    order = ", ".join(f'"{name}"' for name, _ in cols)
    rows = con.execute(f'SELECT * FROM "{schema}"."{table}" ORDER BY {order}'
                       ).fetchall()
    digest = hashlib.sha256()
    digest.update(_json_canon({"table": f"{schema}.{table}", "columns": cols}))
    sample, canon = [], []
    for index, row in enumerate(rows):
        values = [_canon_value(cell) for cell in row]
        digest.update(_json_canon(values) + b"\n")
        canon.append(values)
        if index < 5:
            sample.append(values)
    return {"canonical_hash": digest.hexdigest(), "row_count": len(rows),
            "sample": sample, "columns": [list(c) for c in cols],
            "rows": canon}


def materialise(con, table: str, rows: list, ddl: dict) -> None:
    """Create `table` at its frozen DDL and insert `rows` positionally."""
    if table not in ddl:
        raise SpecError(f"spec['ddl'] has no CREATE TABLE for {table}")
    schema, name = _split(table)
    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    con.execute(f'DROP TABLE IF EXISTS "{schema}"."{name}"')
    con.execute(ddl[table])
    if rows:
        width = ", ".join("?" * len(rows[0]))
        con.executemany(f'INSERT INTO "{schema}"."{name}" VALUES ({width})',
                        [list(r) for r in rows])


# ===========================================================================
# naming an unmatched receipt
# ===========================================================================
def _layout_difference(got_cols: list, ref_cols: list) -> str:
    got = {name: kind for name, kind in got_cols}
    ref = {name: kind for name, kind in ref_cols}
    parts = []
    missing = [name for name in ref if name not in got]
    unexpected = [name for name in got if name not in ref]
    retyped = [f"{name} {got[name]} (oracle {ref[name]})"
               for name in ref if name in got and got[name] != ref[name]]
    if missing:
        parts.append(f"missing {missing}")
    if unexpected:
        parts.append(f"unexpected {unexpected}")
    if retyped:
        parts.append(f"retyped {retyped}")
    if not parts:
        parts.append(f"reordered to {[name for name, _ in got_cols]}")
    return "layout differs: " + "; ".join(parts)


def _rows_difference(got_rows: list, ref_rows: list, columns: list) -> str:
    got = Counter(tuple(_json_canon(v) for v in row) for row in got_rows)
    ref = Counter(tuple(_json_canon(v) for v in row) for row in ref_rows)
    extra = list((got - ref).elements())
    absent = list((ref - got).elements())
    if len(extra) != len(absent):
        return (f"content differs: {len(extra)} row(s) the oracle does not "
                f"hold, {len(absent)} oracle row(s) not published")

    def shown(cell: bytes) -> str:
        value = json.loads(cell.decode("utf-8"))
        return "NULL" if value is None else str(value)

    described = []
    for index, (name, _kind) in enumerate(columns):
        mine = sorted(row[index] for row in extra)
        theirs = sorted(row[index] for row in absent)
        if mine == theirs:
            continue
        mine, theirs = sorted(set(mine)), sorted(set(theirs))
        if len(mine) <= 3 and len(theirs) <= 3:
            described.append(f"{name}: {', '.join(map(shown, mine))} vs oracle "
                             f"{', '.join(map(shown, theirs))}")
        else:
            described.append(name)
    return (f"content differs: {len(extra)} row(s), columns "
            f"[{'; '.join(described)}]")


def name_difference(snapshot: dict, ref: dict) -> tuple[str, str]:
    """(label, detail) for a receipt no trajectory matches, read off the table
    the receipt hashed. Layout vs content is decided by comparing COLUMN
    LISTS, never inferred from counts:

      oracle_rows_differ           same layout, different rows
      oracle_content_other_layout  different layout, identical rows
      other_layout                 different layout and different rows
    """
    if snapshot["columns"] != ref["columns"]:
        why = (_layout_difference(snapshot["columns"], ref["columns"])
               + " - the source contract freezes the layout")
        if snapshot["rows"] == ref["rows"]:
            return "oracle_content_other_layout", why + "; the rows are the oracle's"
        return "other_layout", why
    return ("oracle_rows_differ",
            _rows_difference(snapshot["rows"], ref["rows"], ref["columns"]))


# ===========================================================================
# reference trajectories
# ===========================================================================
def load_trajectories(grading: Path, spec: dict) -> dict:
    """{"variants": {name: {night: {target: hash-dict}}},
        "t0": {block: {"hashes": {target: hash-dict}, "rows": {table: rows}}},
        "rows": {name: {night: {table: rows}}}}

    Every reference table is materialised at the spec's DDL in a throwaway
    duckdb and hashed by `canonical_hash`; columns and canonical rows ride
    along so an unmatched receipt's difference can be named. `rows` keeps the
    raw reference rows per night for spec-driven decision checks that read
    content rather than hashes (GT009's D2b reads closed-day rows)."""
    traj = _traj(spec)
    ddl = spec.get("ddl") or {}
    expected = Path(grading) / str(traj.get("dir", "expected"))
    aliases = dict(traj.get("aliases") or {})
    hashed = set(targets_of(spec)) | set(traj.get("hash_tables") or ())
    night_field = str(traj.get("night_field", "night"))
    nights_key = str(traj.get("nights_key", "nights"))
    pattern = str(traj.get("file", "variant_{name}.json"))
    out: dict = {"variants": {}, "t0": {}, "rows": {}}
    scratch = Path(tempfile.mkdtemp(prefix="receipt-engine-ref-"))
    try:
        con = duckdb.connect(str(scratch / "ref.duckdb"), config={"threads": 1})
        try:
            t0_file = traj.get("t0_file")
            if t0_file:
                path = expected / str(t0_file)
                if not path.is_file():
                    raise FileNotFoundError(
                        f"the t0 reference {path} is missing from the grading zone")
                doc = json.loads(path.read_text(encoding="utf-8"))
                for label in traj.get("t0_blocks") or ():
                    block = {aliases.get(k, k): v
                             for k, v in (doc.get(label) or {}).items()}
                    hashes = {}
                    for table in sorted(t for t in block if t in hashed and t in ddl):
                        materialise(con, table, block[table], ddl)
                        hashes[table] = canonical_hash(con, table)
                    out["t0"][label] = {"hashes": hashes,
                                        "rows": {t: list(v) for t, v in block.items()
                                                 if isinstance(v, list)}}
            for name in trajectory_names(spec):
                path = expected / pattern.format(name=name)
                if not path.is_file():
                    raise FileNotFoundError(
                        f"the reference trajectory {name} is missing from the "
                        f"grading zone ({path})")
                entries = json.loads(path.read_text(encoding="utf-8"))[nights_key]
                per_night, raw = {}, {}
                for entry in entries:
                    night = int(entry[night_field])
                    entry = {aliases.get(k, k): v for k, v in entry.items()}
                    snapshot = {}
                    for table in [t for t in entry if t in hashed and t in ddl]:
                        materialise(con, table, entry[table], ddl)
                        snapshot[table] = canonical_hash(con, table)
                    per_night[night] = snapshot
                    raw[night] = {t: v for t, v in entry.items() if isinstance(v, list)}
                out["variants"][name] = per_night
                out["rows"][name] = raw
        finally:
            con.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return out


def audit_hash_rule(receipts: list, trajectories: dict) -> dict:
    """Does this file's restatement of the ledger rule reproduce a hash the
    WORLD wrote? Reported, and qualifies every hash-based verdict."""
    for receipt in receipts:
        for name, nights in trajectories["variants"].items():
            for night, snapshot in nights.items():
                ref = snapshot.get(receipt["target"])
                if ref and ref["canonical_hash"] == receipt["canonical_hash"]:
                    return {"audited": True, "matched": f"{name}/night{night}",
                            "target": receipt["target"], "seq": receipt["seq"],
                            "why": "a receipt the world wrote is reproduced "
                                   "byte for byte by this grader's own "
                                   "implementation of the canonical rule"}
    return {"audited": False, "matched": None, "target": None, "seq": None,
            "why": "no receipt matched any reference hash, so the restated "
                   "canonical rule is unconfirmed on this run; hash verdicts "
                   "below are reported but the agreement is not demonstrated"}


# ===========================================================================
# the ledger
# ===========================================================================
def _harness_ledger():
    """tasks/harness/ledger.py, loaded by path under a private name: the
    harness reader validates seq contiguity and the key set, and a bare
    `import ledger` would bind whichever `ledger` is first on sys.path."""
    spec = importlib.util.spec_from_file_location("_alkera_harness_ledger",
                                                  HARNESS_LEDGER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_receipts(root: Path) -> tuple[list, str]:
    """The world's publication ledger, through the harness's own reader."""
    try:
        ledger = _harness_ledger()
    except Exception as exc:                                       # noqa: BLE001
        return [], f"tasks/harness/ledger.py is not importable: {exc}"
    try:
        receipts = ledger.load_receipts(root)
    except Exception as exc:                                       # noqa: BLE001
        return [], f"the ledger is not readable as evidence: {first_line(exc)}"
    if not receipts:
        return [], ("the advanced world published no receipts: no job that ran "
                    "declares `publishes:`")
    return receipts, f"{len(receipts)} receipt(s) from the advanced world"


def _vtime(value: str) -> datetime:
    return datetime.fromisoformat(
        str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def night_spans(spec: dict) -> list[tuple[datetime, datetime]]:
    """Half-open [start, end) per night, so consecutive nights partition the
    ledger and a receipt belongs to exactly one."""
    start = _vtime(_require(spec, "virtual_now"))
    step = timedelta(hours=float(_require(spec, "hours_per_night")))
    return [(start + step * k, start + step * (k + 1))
            for k in range(int(_require(spec, "nights")))]


def receipts_by_night(receipts: list, targets, spec: dict) -> dict:
    """{night: {target: receipt}} - the LAST receipt per target in the span
    (file order is publication order), which is what the consumer held."""
    wanted = set(targets if targets is not None else targets_of(spec))
    out = {}
    for k, (low, high) in enumerate(night_spans(spec), start=1):
        per_target = {}
        for receipt in receipts:
            if low <= _vtime(receipt["vnow"]) < high and receipt["target"] in wanted:
                per_target[receipt["target"]] = receipt
        out[k] = per_target
    return out


# ===========================================================================
# classification
# ===========================================================================
def classify_night(night: int, per_target: dict, trajectories: dict,
                   snapshots: dict, spec: dict, *, targets=None) -> dict:
    """Which reference trajectory does this night's set of receipts match?

    Per target: every trajectory whose night-k hash equals the receipt's;
    several can agree on a night that does not separate them, so the paying
    trajectory is preferred and the tie reported. A receipt that matches none
    is NAMED off the end-of-night snapshot when that snapshot hashes the same
    as the receipt (the table the consumer was shown). The night `matched` a
    trajectory only when every governed target matched the same one.

    Numeric fields for progress: `receipts_matching` (targets whose receipt
    is the paying trajectory's) and `fraction` (that over the target count).
    """
    names = trajectory_names(spec)
    pays = paying(spec)
    governed = list(targets) if targets is not None else targets_of(spec)
    roles = roles_of(spec)
    result = {"night": night, "matched": None, "per_target": {}}
    found_names = []
    for target in governed:
        receipt = per_target.get(target)
        found = None
        detail = "no receipt: nothing was published to this target tonight"
        if receipt is not None:
            detail = f"{receipt['row_count']} row(s) at {receipt['vnow']}"
            matches = [name for name in names
                       if ((trajectories["variants"].get(name) or {}).get(night) or {})
                       .get(target, {}).get("canonical_hash") == receipt["canonical_hash"]]
            if matches:
                found = pays if pays in matches else matches[0]
                if len(matches) > 1:
                    detail += f"; indistinguishable tonight from {matches}"
            if found is None:
                ref = ((trajectories["variants"].get(pays) or {}).get(night) or {}
                       ).get(target)
                snapshot = ((snapshots or {}).get(night) or {}).get(target)
                if (ref and snapshot and snapshot.get("rows") is not None
                        and snapshot["canonical_hash"] == receipt["canonical_hash"]):
                    found, why = name_difference(snapshot, ref)
                    detail += "; " + why
                else:
                    detail += (f"; unrecognised - the {pays} has "
                               f"{(ref or {}).get('row_count')} row(s) here"
                               + ("; the table had moved on by the end of the "
                                  "night, so the difference is not readable "
                                  "off it" if snapshot else ""))
        result["per_target"][target] = {"variant": found, "detail": detail}
        found_names.append(found)
        if target in roles:
            result[roles[target]] = found
    result["matched"] = (found_names[0]
                         if found_names and len(set(found_names)) == 1 else None)
    matching = sum(1 for f in found_names if f == pays)
    result["receipts_matching"] = matching
    result["fraction"] = round(matching / len(governed), 6) if governed else 0.0
    return result


def classify_all(receipts: list, trajectories: dict, snapshots: dict,
                 spec: dict) -> dict:
    """{night: classify_night(...)} for every night, or {} with no receipts."""
    if not receipts:
        return {}
    per_night = receipts_by_night(receipts, None, spec)
    return {k: classify_night(k, per_night[k], trajectories, snapshots, spec)
            for k in range(1, int(spec["nights"]) + 1)}


# ===========================================================================
# t0: the warehouse repair
# ===========================================================================
def _norm_rows(rows) -> list:
    """Rows as the bank prints them: every cell a string, NULL as "", sorted.
    duckdb dates/ints and JSON reference cells compare equal this way."""
    return sorted([["" if v is None else str(v) for v in row] for row in rows])


def read_tables(db: Path, tables) -> dict:
    """{table: normalised rows | None} read through an aliased READ_ONLY
    ATTACH (agent.md 9.2.1), never a direct open."""
    out = {}
    if not Path(db).is_file():
        return {name: None for name in tables}
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute("ATTACH '" + Path(db).as_posix().replace("'", "''")
                    + "' AS wh (READ_ONLY)")
        con.execute("USE wh")
        for name in tables:
            schema, table = _split(name)
            exists = con.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE "
                "table_schema = ? AND table_name = ?", [schema, table]).fetchone()[0]
            if not exists:
                out[name] = None
                continue
            rows = con.execute(f'SELECT * FROM "{schema}"."{table}"').fetchall()
            out[name] = _norm_rows(rows)
    finally:
        con.close()
    return out


def _ddl_columns(table: str, ddl: dict) -> list[str]:
    if table not in ddl:
        return []
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        materialise(con, table, [], ddl)
        schema, name = _split(table)
        return [r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE "
            "table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [schema, name]).fetchall()]
    finally:
        con.close()


def _index(col, columns: list[str], table: str) -> int:
    if isinstance(col, int):
        return col
    if col in columns:
        return columns.index(col)
    raise SpecError(f"column {col!r} is not in {table}'s DDL {columns}")


def _table_parity(entry: dict, got, want: list, columns: list[str]) -> dict:
    table = entry["table"]
    mode = str(entry.get("mode", "exact"))
    out = {"table": table, "mode": mode, "reference": entry.get("reference"),
           "rows_expected": len(want), "rows_present": None if got is None else len(got)}
    if got is None:
        out.update(passed=False, fraction=0.0, rows_matching=0,
                   problem=f"{table} is not there")
        return out
    got_c = Counter(map(tuple, got))
    want_c = Counter(map(tuple, want))
    common = sum((got_c & want_c).values())
    out["rows_matching"] = common
    if mode == "superset":
        kept = set(map(tuple, want)) <= set(got_c)
        out["passed"] = kept
        out["fraction"] = round(sum(1 for r in set(map(tuple, want)) if r in got_c)
                                / len(set(map(tuple, want))), 6) if want else 1.0
        if not kept:
            out["problem"] = (f"{table}: {len(set(map(tuple, want)) - set(got_c))} "
                              f"reference row(s) no longer present")
        return out
    out["passed"] = got == want
    denominator = max(len(want), len(got))
    out["row_fraction"] = round(common / denominator, 6) if denominator else 1.0
    key = entry.get("grain_key")
    if key:
        idx = [_index(c, columns, table) for c in key]

        def blocks(rows):
            grouped: dict = {}
            for row in rows:
                grouped.setdefault(tuple(row[i] for i in idx), []).append(tuple(row))
            return {k: sorted(v) for k, v in grouped.items()}

        g, w = blocks(got), blocks(want)
        keys = set(g) | set(w)
        same = sum(1 for k in keys if g.get(k) == w.get(k))
        out["keys_expected"] = len(w)
        out["keys_matching"] = same
        out["fraction"] = round(same / len(keys), 6) if keys else 1.0
    else:
        out["fraction"] = out["row_fraction"]
    if not out["passed"]:
        out["problem"] = (f"{table} differs from the {entry.get('reference')} "
                          f"snapshot ({len(got)} rows vs {len(want)}; "
                          f"{common} matching)")
    return out


def score_t0(warehouse: Path, trajectories: dict, spec: dict) -> dict:
    """C0: is the t0 warehouse the oracle's t0 warehouse?

    Per `spec["t0_tables"]` entry, the table as it stands at teardown is
    compared to a t0 reference block: `exact` requires the same multiset of
    rows; `superset` requires every reference row still present (seeded
    history the agent must not rewrite). `fraction` per table is grain-key
    parity (keys whose full row block equals the reference's, over the union
    of keys) when a grain key is declared, else matching rows over
    max(expected, present); the overall fraction is their weighted mean.
    `passed` is the conjunction - GT009 `score_c0` semantics.
    """
    entries = list(_require(spec, "t0_tables"))
    ddl = spec.get("ddl") or {}
    now = read_tables(warehouse, [e["table"] for e in entries])
    per_table, problems, extras = [], [], {}
    weighted = weight_sum = 0.0
    for entry in entries:
        table = entry["table"]
        block = str(entry.get("reference", "oracle_t0"))
        ref_rows = ((trajectories.get("t0") or {}).get(block) or {}).get("rows", {})
        if table not in ref_rows:
            raise SpecError(f"t0 reference block {block!r} carries no rows for {table}")
        want = _norm_rows(ref_rows[table])
        columns = _ddl_columns(table, ddl)
        parity = _table_parity(entry, now.get(table), want, columns)
        groups = entry.get("groups")
        if groups:
            col = _index(groups["column"], columns, table)
            got = now.get(table) or []
            count = 0
            for value in groups.get("values") or ():
                want_v = [r for r in want if r[col] == str(value)]
                got_v = [r for r in got if r[col] == str(value)]
                if want_v and got_v == want_v:
                    count += 1
            parity[groups.get("field", "groups_matching")] = count
            extras[groups.get("field", "groups_matching")] = count
        per_table.append(parity)
        if parity.get("problem"):
            problems.append(parity["problem"])
        w = float(entry.get("weight", 1.0))
        weighted += w * parity["fraction"]
        weight_sum += w
    fraction = round(weighted / weight_sum, 6) if weight_sum else 0.0
    passed = all(p["passed"] for p in per_table)
    detail = ("t0: " + ("; ".join(problems) if problems else
                        "every t0 table matches its reference ("
                        + ", ".join(f"{p['table']} {p['rows_present']} rows"
                                    for p in per_table) + ")"))
    return {"passed": passed, "fraction": fraction, "detail": detail,
            "per_table": per_table, **extras}


# ===========================================================================
# progress
# ===========================================================================
def progress_quantities(t0: dict, decisions: dict, spec: dict) -> dict:
    """{"q0": t0 repair fraction, "q1".."qN": night k's governed targets
    matching the paying trajectory, as a fraction}. A night with no decision
    (gated replay, no receipts) is 0.0, not absent: the consumer was shown
    nothing correct that night."""
    out = {"q0": float((t0 or {}).get("fraction") or 0.0)}
    n = len(targets_of(spec))
    for k in range(1, int(_require(spec, "nights")) + 1):
        d = (decisions or {}).get(k) or (decisions or {}).get(str(k))
        if not d:
            out[f"q{k}"] = 0.0
            continue
        if "fraction" in d:
            out[f"q{k}"] = float(d["fraction"])
        else:
            matching = sum(1 for info in d["per_target"].values()
                           if info["variant"] == paying(spec))
            out[f"q{k}"] = round(matching / n, 6) if n else 0.0
    return out
