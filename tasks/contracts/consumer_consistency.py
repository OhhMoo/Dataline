#!/usr/bin/env python3
"""CONSUMER CONSISTENCY: what the consumer was handed at each point on the
trajectory is what the oracle would have published at that point.

Two consumers, one invariant.

RECEIPTS (`world/world/ledger.py`).  The world appends a receipt at the moment a
publication becomes visible; each receipt's `canonical_hash` is compared with
the oracle's hash for the same publication.  Executed 2026-10-18 against
receipts real worlds wrote: GT009's, replayed four nights through
`world.scheduler` by `runner: "advance"` on recorded rollout worlds, and graded
by this contract to the same verdict GT009's own R1..R4 reached
(tasks/factory/grader/tests/test_contracts_real_world.py).

BI CACHES (`params.bi`).  Every BI surface this repo models keeps a COPY of what
it last read, and a copy is a publication: the dashboard shows it whatever the
warehouse now holds.

    YAML dashboards  build/bi/refresh/<dashboard>.json, the 06:00 `bi_refresh`
                     job's rendered panels (commerce_analytics'
                     scripts/refresh_dashboards.py)
    Power BI         var/bi/powerbi/datasets.json + import/<ds>/<rel>.csv, the
                     world's record of what the last scheduled refresh
                     imported (bi_projects.py IMPORTS), and the service's
                     <bi_state_dir>/powerbi/snapshots/<id>.duckdb
    Tableau          <bi_state_dir>/tableau/extracts/*.duckdb - an extract is a
                     copy of the relations its model reads (services/tableau.py
                     EXTRACTS WITHOUT HYPER)
    Sigma            live: no row cache.  What it caches is its connection
                     CATALOG (<bi_state_dir>/sigma/state.json `catalog`), the
                     warehouse's columns as of the last sync

Each surface is compared with the warehouse mart it copies, and a copy that
disagrees is a consumer handed a number the platform no longer holds - the
stale-refresh archetype (BI002), invisible to any grader that reads the
warehouse alone.

The dashboard YAML is not parsed here (duckdb is this package's only
third-party import): a YAML cache is checked against `bi.render(root)` - the
grader passes `world.bi.render` - or against `bi.panels` {dashboard: {panel:
sql}}, which this module renders itself.

BI BINDINGS (`params.bindings`).  The explicit form, for a consumer whose copy
is not where `discover_bi` looks: [{consumer: powerbi|tableau|sigma|yaml,
mart, columns, extract (csv/json/jsonl/parquet), format?, definition?}].  The
extract must equal the mart as a multiset of rows on `columns` (values
compared as text), and a `definition`, when named, must mention the mart.
One FATAL `bi_extract_matches` clause covers every binding.
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import math
import shutil
import tempfile
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from . import _world as W
from ._types import Clause, ContractNotEvaluable, Context, not_evaluable

NAME = "consumer_consistency"

DOC = """INVARIANT   every receipt's canonical_hash is the oracle's hash at that receipt's
            own vnow, and every BI cache/extract equals the mart it copies.
STRESSED BY a repair that runs while a consumer keeps publishing: a dashboard
            repointed mid-cutover, a refresh that ran before the late data.
SHORTCUT    fix the end state and let the intermediate publications (or the
            stale BI copy) stand - invisible to a grader reading the warehouse.
            The coverage clause stops the opposite shortcut, publishing nothing.
ORACLE      params.expected / params.oracle (file::fn or callable); bi copies
            against the warehouse.  EXECUTED on GT009 replays (= R1..R4), BI002.
"""

EMPTY_DETAIL = "no receipts: contract not evaluable"


# --------------------------------------------------------------- audit bytes
# Byte-for-byte gateway/src/alkera_gateway/records.py::canonical, reduced to the
# types that survive a JSON round trip (a receipt was already parsed from JSON,
# so nothing else can appear).  Copied, not imported: this package does not
# depend on the gateway.
def _json_value(value):
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return {"type": "float", "value": str(value)}
    return value


def canonical(value) -> bytes:
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


# ----------------------------------------------------------- the ledger rule
# world/world/ledger.py's table hash, restated: a receipt the WORLD wrote is
# hashed by this rule, not by `digest` (a table is typed rows, not a JSON
# payload, and the ledger's json is ensure_ascii=True where the gateway's is
# not).  Copied for the same reason as `canonical`; the equality with the
# world's own implementation is asserted on a real GT009 table by
# tasks/factory/grader/tests/test_contracts_real_world.py.
LEDGER_ABSENT = hashlib.sha256(b"alkera-ledger/absent/v1\n").hexdigest()
LEDGER_SAMPLE_ROWS = 5


def _ledger_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def canon_cell(value):
    """world/world/hashing.py::canon_value - also the BI refresh cache's cell
    rule (refresh_dashboards.py::canon), so one function serves both."""
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
    return str(value)


def table_contents(world_root, target: str) -> dict:
    """{canonical_hash, row_count, sample} of one table, by the ledger's rule:
    sha256 over json({"table", "columns": [[name, type]]}) then one json row +
    b"\\n" per row, rows ordered by every column.  `target` is schema.table in
    the primary warehouse or catalog.schema.table for var/warehouse/<catalog>.
    A target that is not there hashes to LEDGER_ABSENT."""
    import duckdb
    parts = str(target).split(".")
    if len(parts) == 2:
        parts = ["warehouse"] + parts
    if len(parts) != 3:
        raise ContractNotEvaluable(f"{target!r} is not schema.table")
    catalog, schema, table = parts
    db = Path(world_root) / "var" / "warehouse" / f"{catalog}.duckdb"
    absent = {"canonical_hash": LEDGER_ABSENT, "row_count": None, "sample": None}
    if not db.is_file():
        return absent
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh (READ_ONLY)")
        con.execute("USE wh")
        cols = con.execute(
            "select column_name, data_type from information_schema.columns "
            "where table_schema = ? and table_name = ? "
            "order by ordinal_position", [schema, table]).fetchall()
        if not cols:
            return absent
        order = ", ".join(f'"{c}"' for c, _ in cols)
        rows = con.execute(
            f'select * from wh."{schema}"."{table}" order by {order}').fetchall()
    except duckdb.Error:
        return absent
    finally:
        con.close()
    h = hashlib.sha256()
    h.update(_ledger_json({"table": f"{schema}.{table}",
                           "columns": [list(c) for c in cols]}))
    sample = []
    for index, row in enumerate(rows):
        values = [canon_cell(c) for c in row]
        h.update(_ledger_json(values) + b"\n")
        if index < LEDGER_SAMPLE_ROWS:
            sample.append(values)
    return {"canonical_hash": h.hexdigest(), "row_count": len(rows),
            "sample": sample}


def _load_oracle(spec, world_root: Path):
    """params.oracle: a callable (set by the grader that builds the Context,
    like cutoff_integrity's purpose_of) or "<file.py>::<function>"."""
    if callable(spec):
        return spec
    if "::" not in str(spec):
        raise ContractNotEvaluable(
            f"params.oracle must be '<file.py>::<function>' or a callable, got {spec!r}")
    path_part, func = str(spec).split("::", 1)
    path = Path(path_part)
    if not path.is_absolute():
        path = Path(world_root) / path_part
    if not path.is_file():
        raise ContractNotEvaluable(f"params.oracle names {path}, which does not exist")
    module_spec = importlib.util.spec_from_file_location(
        "contract_oracle_" + path.stem, path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    fn = getattr(module, func, None)
    if fn is None:
        raise ContractNotEvaluable(f"{path} has no function {func!r}")
    return fn


def _keys(receipt: dict) -> list:
    """The spellings params.expected may be keyed by, most specific first."""
    seq = receipt.get("seq")
    target, day = receipt.get("target"), receipt.get("day")
    consumer = receipt.get("consumer")
    return [str(seq), f"{consumer}|{target}|{day}", f"{target}|{day}", str(day)]


def _oracle_hash(value):
    """An oracle returns the OBJECT it would have published (hashed here by
    `digest`), {"canonical_hash": h} when the publication is a table hashed by
    the ledger rule, or None for "I have no expectation for this one"."""
    if value is None:
        return None
    if isinstance(value, dict) and set(value) == {"canonical_hash"}:
        return value["canonical_hash"]
    return digest(value)


def _forged(r: dict) -> bool:
    """Does the receipt contradict itself?

    A gateway-shaped receipt carries its whole payload as `sample` (a mapping),
    so the hash must be the payload's digest.  A LEDGER receipt does not:
    `sample` is the first five canonical rows of a table, or null, and the hash
    covers the column types and every row - so `digest(sample)` is never its
    hash, and comparing the two flagged every receipt a world ever wrote as
    edited (measured on GT009's first real ledger).  What a ledger receipt CAN
    be checked for is the shape the writer guarantees: a sha256, ABSENT exactly
    when there is no row count, and a sample of min(row_count, 5) rows.
    """
    sample, h = r.get("sample"), r.get("canonical_hash")
    if isinstance(sample, dict):
        return digest(sample) != h
    if not (isinstance(h, str) and len(h) == 64
            and all(c in "0123456789abcdef" for c in h)):
        return True
    n = r.get("row_count")
    if h == LEDGER_ABSENT:
        return not (n is None and sample is None)
    if n is None:                       # a file receipt: bytes, no row model
        return sample is not None
    return (not isinstance(sample, list)
            or len(sample) != min(int(n), LEDGER_SAMPLE_ROWS)
            or not all(isinstance(row, list) for row in sample))


def evaluate(ctx: Context) -> list:
    try:
        return _evaluate(ctx)
    except ContractNotEvaluable as exc:
        return not_evaluable(f"{NAME}.not_evaluable", str(exc))


def _evaluate(ctx: Context) -> list:
    runner = ctx.param("runner", "end_state")
    if runner not in ("end_state", "advance"):
        raise ContractNotEvaluable(
            f"runner={runner!r}; consumer_consistency knows 'end_state' (the "
            f"receipts handed in) and 'advance' (the receipts a copy of the world "
            f"writes while its clock is moved over planted nights)")
    if runner == "end_state":
        clauses = []
        if _wants_receipts(ctx):
            clauses += _receipt_clauses(ctx, list(ctx.receipts or []))
        if ctx.param("bi") is not None:
            clauses += _bi_clauses(ctx, ctx.world_root)
        if ctx.param("bindings"):
            clauses += _binding_clauses(ctx, ctx.world_root, clauses)
        return clauses

    tmp = Path(tempfile.mkdtemp(prefix="contract-consumer-consistency-"))
    try:
        root = W.copy_world(ctx.world_root, tmp / "world")
        ledger = root / LEDGER_RELPATH
        before = len(Context.load_receipts(ledger))
        log = W.run_nights(ctx, root)
        receipts = [r for r in Context.load_receipts(ledger)
                    if int(r.get("seq", 0)) > before]
        clauses = _receipt_clauses(ctx, receipts) if _wants_receipts(ctx) else []
        if ctx.param("bi") is not None:
            clauses += _bi_clauses(ctx, root)
        if ctx.param("bindings"):
            clauses += _binding_clauses(ctx, root, clauses)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    clauses.append(Clause.make(
        f"{NAME}.every_night_reached", log["reached"] == log["declared"],
        (f"all {log['declared']} planted night(s) advanced; "
         f"{len(receipts)} receipt(s) written" if log["reached"] == log["declared"]
         else f"{log['reached']} of {log['declared']} night(s) advanced: "
              f"{log['error']}"),
        weight=1.0, fatal=True,
        checkpoint=None if log["reached"] == log["declared"]
        else f"night {log['reached'] + 1}"))
    return clauses


LEDGER_RELPATH = Path("var") / "ledger" / "receipts.jsonl"


def _wants_receipts(ctx: Context) -> bool:
    """A context that declares BI surfaces (`bi` or `bindings`) and no receipt
    oracle is asking about the BI copies only; every other context is asking
    about receipts, and gets the visible "not evaluable" when there are none."""
    return ((ctx.param("bi") is None and not ctx.param("bindings"))
            or ctx.param("expected") is not None
            or ctx.param("oracle") is not None)


def _receipt_clauses(ctx: Context, receipts: list) -> list:
    if not receipts:
        return [Clause.make(f"{NAME}.evaluable", False, EMPTY_DETAIL, weight=1.0)]

    expected = {str(k): v for k, v in (ctx.param("expected") or {}).items()}
    oracle_spec = ctx.param("oracle")
    oracle = _load_oracle(oracle_spec, ctx.world_root) if oracle_spec else None
    if not expected and oracle is None:
        raise ContractNotEvaluable(
            "neither params.expected nor params.oracle is set, so there is "
            "nothing to compare the receipts against")
    kinds = ctx.param("kinds")
    targets = ctx.param("targets")
    rows = [r for r in sorted(receipts, key=lambda r: int(r.get("seq", 0)))
            if (kinds is None or str(r.get("kind")) in set(kinds))
            and (targets is None or str(r.get("target")) in set(targets))]
    if not rows:
        return [Clause.make(f"{NAME}.evaluable", False,
                            f"{len(receipts)} receipt(s), none of kind "
                            f"{sorted(kinds or [])} / target "
                            f"{sorted(targets or [])}", weight=1.0)]

    mismatched, uncovered, forged = [], [], []
    first_bad = None
    for r in rows:
        want = None
        for k in _keys(r):
            if k in expected:
                want = expected[k]
                break
        if want is None and oracle is not None:
            want = _oracle_hash(oracle(r, ctx))
        if want is None:
            uncovered.append(r.get("seq"))
        elif r.get("canonical_hash") != want:
            mismatched.append(
                f"seq {r.get('seq')} @ {r.get('vnow')} {r.get('target')}/"
                f"{r.get('day')}: published {str(r.get('canonical_hash'))[:12]}, "
                f"oracle {str(want)[:12]}")
            first_bad = first_bad or str(r.get("vnow"))
        if _forged(r):
            forged.append(r.get("seq"))

    checkpoint = first_bad or str(rows[-1].get("vnow"))
    return [
        Clause.make(
            f"{NAME}.hashes_match", not mismatched,
            (f"all {len(rows)} receipt(s) carry the hash the oracle would have "
             f"published at their own vnow" if not mismatched else
             f"{len(mismatched)} receipt(s) do not match the oracle at their own "
             f"vnow: {mismatched[:3]}"),
            weight=0.55, fatal=True, checkpoint=checkpoint),
        Clause.make(
            f"{NAME}.every_receipt_covered", not uncovered,
            ("every receipt has an expectation" if not uncovered else
             f"{len(uncovered)} receipt(s) have no expected hash at all "
             f"({uncovered[:4]}): an unexplained publication is not evidence of "
             f"consistency"),
            weight=0.25, checkpoint=checkpoint),
        Clause.make(
            f"{NAME}.hash_is_canonical", not forged,
            ("every receipt is consistent with its own recorded canonical_hash"
             if not forged else
             f"{len(forged)} receipt(s) record a hash that is not the hash of "
             f"their own payload ({forged[:4]}) - the ledger was edited after "
             f"the fact"),
            weight=0.20, checkpoint=checkpoint),
    ]


# ================================================================== BI caches
REFRESH_DIR = Path("build") / "bi" / "refresh"
POWERBI_RECORD = Path("var") / "bi" / "powerbi" / "datasets.json"


def _bi_clauses(ctx: Context, root: Path) -> list:
    """One FATAL clause per surface: a dashboard showing a number the warehouse
    no longer holds is the consumer handed something false, and partial credit
    for the other dashboards would pay for it.  Weight 0.55 split evenly, the
    same share `hashes_match` carries."""
    spec = ctx.param("bi") or {}
    if not isinstance(spec, dict):
        raise ContractNotEvaluable("params.bi must be a mapping")
    surfaces = list(spec.get("surfaces") or [])
    if spec.get("discover", True):
        surfaces += discover_bi(root, spec.get("bi_state_dir"),
                                spec.get("refresh_dir"))
    if not surfaces:
        return [Clause.make(
            f"{NAME}.bi_evaluable", False,
            "params.bi is declared but the world carries no BI cache, extract, "
            "import record or catalog to compare - and none is declared",
            weight=1.0)]
    db = W.warehouse_path(root)
    if not db.is_file():
        raise ContractNotEvaluable(f"no warehouse at {db}")
    weight = 0.55 / len(surfaces)
    clauses = []
    for s in surfaces:
        ok, detail = _check_surface(s, root, db, spec)
        clauses.append(Clause.make(f"{NAME}.bi:{s['name']}", ok, detail,
                                   weight=weight, fatal=True,
                                   checkpoint=s.get("as_of")))
    return clauses


def discover_bi(root: Path, bi_state_dir=None, refresh_dir=None) -> list:
    """Every BI copy the world (and, if given, the vendor services' state
    directory) holds, as surface specs.  Paths are the ones the writers use;
    see the module header."""
    root = Path(root)
    out = []
    refresh = root / (refresh_dir or REFRESH_DIR)
    for f in sorted(refresh.glob("*.json")) if refresh.is_dir() else []:
        out.append({"name": f"yaml:{f.stem}", "kind": "rendered", "cache": str(f),
                    "dashboard": f.stem})
    record = root / POWERBI_RECORD
    if record.is_file():
        doc = W.read_json(record, {}) or {}
        for ds, body in sorted((doc.get("datasets") or {}).items()):
            imp = (body or {}).get("import") or {}
            for rel in imp.get("relations") or []:
                out.append({"name": f"powerbi-import:{ds}:{rel['relation']}",
                            "kind": "relation_copy", "format": "csv",
                            "copy": str(record.parent / rel["path"]),
                            "relation": rel["relation"],
                            "columns": rel.get("columns"),
                            "as_of": imp.get("refreshed")})
    if bi_state_dir:
        state = Path(bi_state_dir)
        for vendor, sub in (("powerbi", "snapshots"), ("tableau", "extracts")):
            for f in sorted((state / vendor / sub).glob("*.duckdb")):
                if f.name.endswith(".loading.duckdb"):
                    continue
                for rel in _duckdb_relations(f):
                    out.append({"name": f"{vendor}:{f.stem}:{rel}",
                                "kind": "relation_copy", "format": "duckdb",
                                "copy": str(f), "relation": rel})
        sigma = state / "sigma" / "state.json"
        if sigma.is_file():
            catalog = (W.read_json(sigma, {}) or {}).get("catalog") or {}
            for rel, cols in sorted(catalog.items()):
                out.append({"name": f"sigma-catalog:{rel}", "kind": "catalog",
                            "relation": rel, "columns": cols})
    return out


def _duckdb_relations(path: Path) -> list:
    import duckdb
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute("ATTACH '" + str(path).replace("'", "''") + "' AS c (READ_ONLY)")
        return [f"{s}.{t}" for s, t in con.execute(
            "SELECT schema_name, table_name FROM duckdb_tables() "
            "WHERE database_name = 'c' ORDER BY 1, 2").fetchall()]
    finally:
        con.close()


def _wh_columns(con, relation: str) -> list:
    schema, _, table = relation.rpartition(".")
    schema = schema.split(".")[-1] or "warehouse"
    return con.execute(
        "SELECT column_name, data_type FROM duckdb_columns() WHERE "
        "database_name = 'wh' AND lower(schema_name) = lower(?) AND "
        "lower(table_name) = lower(?) ORDER BY column_index",
        [schema, table]).fetchall()


def _check_surface(s: dict, root: Path, db: Path, spec: dict) -> tuple:
    kind = s.get("kind")
    try:
        if kind == "relation_copy":
            return _check_relation_copy(s, root, db)
        if kind == "rendered":
            return _check_rendered(s, root, db, spec)
        if kind == "catalog":
            return _check_catalog(s, db)
    except ContractNotEvaluable as exc:
        return False, f"not evaluable: {exc}"
    return False, f"unknown BI surface kind {kind!r}"


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _check_relation_copy(s: dict, root: Path, db: Path) -> tuple:
    """Multiset equality of the copy and the warehouse relation, column by
    column, each copy column cast to the warehouse column's type.  A column on
    one side only is a disagreement too: an import missing a column dbt added
    is a dataset that needs a refresh."""
    import duckdb
    copy = Path(s["copy"])
    if not copy.is_absolute():
        copy = Path(root) / copy
    if not copy.is_file():
        return False, f"the copy {copy} is gone"
    relation = s["relation"]
    con = W.open_world(db, read_only=True)
    try:
        wh_cols = _wh_columns(con, relation)
        if not wh_cols:
            return False, f"the warehouse has no {relation} for this copy to agree with"
        schema, _, table = relation.rpartition(".")
        schema = schema.split(".")[-1]
        if s.get("format") == "csv":
            declared = s.get("columns") or []
            types = ("{" + ", ".join(f"'{n}': '{t}'" for n, t in declared) + "}"
                     if declared else None)
            src = ("read_csv('" + str(copy).replace("'", "''") + "', header=true"
                   + (f", columns={types}" if types else "") + ")")
        else:
            con.execute("ATTACH '" + str(copy).replace("'", "''") + "' AS cp (READ_ONLY)")
            src = f"cp.{_q(schema)}.{_q(table)}"
        copy_cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]
        wh_by = {c.lower(): (c, t) for c, t in wh_cols}
        cp_by = {c.lower(): c for c in copy_cols}
        only_wh = sorted(set(wh_by) - set(cp_by))
        only_cp = sorted(set(cp_by) - set(wh_by))
        shared = [c for c in wh_by if c in cp_by]
        left = ", ".join(f"TRY_CAST({_q(cp_by[c])} AS {wh_by[c][1]}) AS {_q(c)}"
                         for c in shared)
        right = ", ".join(f"{_q(wh_by[c][0])} AS {_q(c)}" for c in shared)
        wh_ref = f"wh.{_q(schema)}.{_q(table)}"
        n_cp = con.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0]
        n_wh = con.execute(f"SELECT COUNT(*) FROM {wh_ref}").fetchone()[0]
        stale = con.execute(f"SELECT COUNT(*) FROM (SELECT {right} FROM {wh_ref} "
                            f"EXCEPT ALL SELECT {left} FROM {src})").fetchone()[0]
        extra = con.execute(f"SELECT COUNT(*) FROM (SELECT {left} FROM {src} "
                            f"EXCEPT ALL SELECT {right} FROM {wh_ref})").fetchone()[0]
    except duckdb.Error as exc:
        return False, (f"the copy of {relation} could not be compared: "
                       f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
    finally:
        con.close()
    as_of = f" (copied {s['as_of']})" if s.get("as_of") else ""
    if not (stale or extra or only_wh or only_cp):
        return True, f"{relation}: the copy{as_of} equals the warehouse ({n_wh} row(s))"
    bits = []
    if stale:
        bits.append(f"{stale} warehouse row(s) the copy does not show")
    if extra:
        bits.append(f"{extra} row(s) in the copy the warehouse no longer holds")
    if only_wh:
        bits.append(f"columns only in the warehouse {only_wh}")
    if only_cp:
        bits.append(f"columns only in the copy {only_cp}")
    return False, (f"{relation}: the copy{as_of} is stale - {n_cp} row(s) vs "
                   f"{n_wh} in the warehouse; " + "; ".join(bits))


def _check_rendered(s: dict, root: Path, db: Path, spec: dict) -> tuple:
    """A YAML dashboard's refresh cache against the same panels rendered now."""
    cache = Path(s["cache"])
    if not cache.is_absolute():
        cache = Path(root) / cache
    doc = W.read_json(cache)
    if not isinstance(doc, dict) or not isinstance(doc.get("panels"), dict):
        return False, f"{cache.name} is not a refresh cache (no `panels`)"
    dashboard = s.get("dashboard") or doc.get("dashboard") or cache.stem
    fresh = None
    if callable(s.get("render") or spec.get("render")):
        rendered = (s.get("render") or spec.get("render"))(Path(root))
        fresh = (rendered or {}).get(dashboard)
    elif (s.get("panels") or (spec.get("panels") or {}).get(dashboard)):
        fresh = _render_sql(db, s.get("panels") or spec["panels"][dashboard])
    if fresh is None:
        raise ContractNotEvaluable(
            f"dashboard {dashboard!r} has a refresh cache but neither bi.render "
            f"nor bi.panels says what its panels compute now")
    norm = lambda rows: json.dumps(rows, sort_keys=True, default=str)  # noqa: E731
    differ = sorted(p for p in set(doc["panels"]) | set(fresh)
                    if norm(doc["panels"].get(p)) != norm(_canon_rows(fresh.get(p))))
    through = doc.get("data_through")
    if not differ:
        return True, (f"{dashboard}: all {len(fresh)} cached panel(s) equal a render "
                      f"of the marts now (cache data_through {through})")
    return False, (f"{dashboard}: {len(differ)} of {len(set(doc['panels']) | set(fresh))} "
                   f"cached panel(s) differ from the marts now: {differ[:4]} - the "
                   f"dashboard shows data through {through}")


def _canon_rows(rows):
    if rows is None:
        return None
    return [[canon_cell(c) for c in row] for row in rows]


def _render_sql(db: Path, panels: dict) -> dict:
    con = W.open_world(db, read_only=True)
    try:
        return {name: [list(r) for r in con.execute(sql).fetchall()]
                for name, sql in panels.items()}
    finally:
        con.close()


def _check_catalog(s: dict, db: Path) -> tuple:
    """Sigma's connection catalog: the columns it believes the relation has."""
    con = W.open_world(db, read_only=True)
    try:
        wh = {c.lower() for c, _ in _wh_columns(con, s["relation"])}
    finally:
        con.close()
    if not wh:
        return False, f"the warehouse has no {s['relation']} for the catalog to describe"
    known = {str(c).lower() for c in (s.get("columns") or {})}
    if known == wh:
        return True, f"{s['relation']}: the catalog knows the warehouse's {len(wh)} column(s)"
    return False, (f"{s['relation']}: the catalog is stale - unknown to it "
                   f"{sorted(wh - known)[:4]}, gone from the warehouse "
                   f"{sorted(known - wh)[:4]}; a connection sync is due")


# ================================================================ BI bindings
def _normal(value):
    """Compare logical extract values, not CSV quoting or row order."""
    return "" if value is None else str(value)


def _binding_rows(root: Path, binding: dict) -> tuple[list, list]:
    mart = binding.get("mart")
    columns = binding.get("columns") or []
    if not mart or not columns:
        raise ContractNotEvaluable("each BI binding needs mart and columns")
    if not all(str(c).replace("_", "").isalnum() for c in columns):
        raise ContractNotEvaluable(f"invalid BI column list {columns!r}")
    select = ", ".join('"%s"' % c for c in columns)
    con = W.open_world(W.warehouse_path(root), read_only=True)
    try:
        warehouse = con.execute(f"SELECT {select} FROM {W.q(mart)}").fetchall()
    finally:
        con.close()
    extract = binding.get("extract")
    if not extract:
        raise ContractNotEvaluable(
            f"BI binding for {mart} has no extract/cache; a dashboard definition "
            "alone does not prove consumer consistency")
    path = Path(extract)
    if not path.is_absolute():
        path = Path(root) / path
    if not path.is_file():
        raise ContractNotEvaluable(f"BI extract/cache is absent: {path}")
    kind = str(binding.get("format") or path.suffix.lstrip(".")).lower()
    if kind == "csv":
        with open(path, newline="", encoding="utf-8-sig") as fh:
            data = list(csv.DictReader(fh))
        consumer = [[row.get(c) for c in columns] for row in data]
    elif kind in ("json", "jsonl"):
        doc = (json.loads(path.read_text(encoding="utf-8")) if kind == "json" else
               [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()])
        if isinstance(doc, dict):
            doc = doc.get("rows") or doc.get("data") or []
        consumer = [[row.get(c) for c in columns] for row in doc]
    elif kind == "parquet":
        import duckdb
        con = duckdb.connect(":memory:")
        try:
            consumer = con.execute(
                f"SELECT {select} FROM read_parquet(?)", [str(path)]).fetchall()
        finally:
            con.close()
    else:
        raise ContractNotEvaluable(f"unsupported BI extract format {kind!r}")
    return ([[_normal(v) for v in row] for row in warehouse],
            [[_normal(v) for v in row] for row in consumer])


def _check_bindings(ctx: Context, root: Path) -> list:
    issues = []
    for binding in ctx.param("bindings") or []:
        consumer_type = str(binding.get("consumer") or "").lower()
        if consumer_type not in ("powerbi", "tableau", "sigma", "yaml"):
            raise ContractNotEvaluable(
                f"BI binding consumer must be powerbi/tableau/sigma/yaml, got {consumer_type!r}")
        warehouse, consumer = _binding_rows(root, binding)
        mart_rows = Counter(json.dumps(row, sort_keys=True) for row in warehouse)
        extract_rows = Counter(json.dumps(row, sort_keys=True) for row in consumer)
        if mart_rows != extract_rows:
            issues.append(f"{consumer_type} {binding.get('extract')}: "
                          f"{sum((mart_rows-extract_rows).values())} missing and "
                          f"{sum((extract_rows-mart_rows).values())} extra rows")
        definition = binding.get("definition")
        if definition:
            path = Path(definition)
            if not path.is_absolute():
                path = Path(root) / path
            if not path.is_file() or str(binding["mart"]).lower() not in path.read_text(
                    encoding="utf-8", errors="replace").lower():
                issues.append(f"{consumer_type} definition does not reference {binding['mart']}")
    return issues


def _binding_clauses(ctx: Context, root: Path, earlier: list) -> list:
    issues = _check_bindings(ctx, root)
    checkpoint = next((c.checkpoint for c in earlier if c.checkpoint), None)
    return [Clause.make(
        f"{NAME}.bi_extract_matches", not issues,
        ("all BI extracts match their warehouse marts" if not issues else
         f"BI consumer drift: {issues[:4]}"),
        weight=0.25, fatal=True, checkpoint=checkpoint)]
