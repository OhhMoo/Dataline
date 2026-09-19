#!/usr/bin/env python3
"""
Fixture contract verification.

A stale generator process once silently overwrote expected.duckdb AFTER a
validated run. The baselines still executed, every verdict still printed PASS,
and the scores were quietly wrong against a reference that no longer matched
the spec. Nothing detected it.

So: never score against a fixture without first proving it is the fixture the
contract describes. This runs before any baseline and refuses to continue on
drift.

    python fixture_contract.py --fixtures ./fixtures/build
    python fixture_contract.py --fixtures ./fixtures/build --falsify   # damage it N ways
    python fixture_contract.py --fixtures ./fixtures/build --digest    # content digests

`--falsify` copies the fixture to scratch, applies each MUTATION in turn and
requires `verify` to refuse the result: a contract that cannot fail is not a
contract. `digest` is the determinism probe: canonical content hashes of every
table in both files (the ledger's rule: rows sorted on every column), so two
builds into $TEMP can be compared without trusting DuckDB's file bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import duckdb

# Contract values from SEED_SPEC.md at seed 1337.
CONTRACT = {
    "world": {"rows": 419675, "adj": 824, "dupkeys": 59995},
    "expected": {"rows": 359680, "adj": 412, "dupkeys": 0},
    "aov_ratio": 0.496,
    "aov_tolerance": 0.01,
}

WIN_START, WIN_END = "2026-02-01", "2026-03-02"


class FixtureDrift(Exception):
    pass


def _stats(db: Path) -> dict:
    # READ ONLY, and it matters beyond tidiness. A read-write connection takes
    # duckdb's exclusive file lock, so K rollouts verifying the same cached
    # fixture at once knocked each other over with
    # "The process cannot access the file because it is being used by another
    # process" - the verifier is the one component that must never be the reason
    # a run fails.
    # Aliased ATTACH, never a direct open (F622, agent.md 9.2.1). Opening the
    # file directly names the catalog after its stem, so at the CONTRACT path
    # var/warehouse/warehouse.duckdb the catalog and the schema are both
    # `warehouse` and every two-part reference below raises Binder Error -
    # Ambiguous reference. It has not fired yet only because callers happen to
    # pass fixture paths whose stem is `world`; `db` is an argument, so that is
    # luck, not a property. Under the alias the catalog is `wh` and
    # `warehouse.fct_orders` reads unambiguously as schema.table.
    con = duckdb.connect(":memory:")
    con.execute("SET threads TO 1;")
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh (READ_ONLY)")
    con.execute("USE wh.warehouse")
    try:
        return {
            "rows": con.execute(
                "SELECT COUNT(*) FROM warehouse.fct_orders").fetchone()[0],
            "adj": con.execute(
                "SELECT COUNT(*) FROM warehouse.fct_orders "
                "WHERE is_manual_adjustment").fetchone()[0],
            "dupkeys": con.execute(
                "SELECT COUNT(*) FROM (SELECT order_id, line_nbr "
                "FROM warehouse.fct_orders GROUP BY 1,2 HAVING COUNT(*) > 1)"
            ).fetchone()[0],
        }
    finally:
        con.close()


def verify(fx: Path, quiet: bool = False) -> None:
    """Raise FixtureDrift unless the fixture matches the contract exactly."""
    problems: list[str] = []

    for name in ("world", "expected"):
        db = fx / (name + ".duckdb")
        if not db.exists():
            problems.append("MISSING: " + str(db))
            continue
        want, got = CONTRACT[name], _stats(db)
        if got != want:
            problems.append(
                name + ".duckdb does not match the contract"
                + "\n      expected " + repr(want)
                + "\n      got      " + repr(got))

    ratio = None
    if not problems:
        # Same F622 form as _stats above. READ_ONLY too: this block only reads,
        # and a read-write handle takes duckdb's exclusive lock, which is what
        # knocked concurrent rollouts over on the sibling connection.
        con = duckdb.connect(":memory:")
        con.execute("SET threads TO 1;")
        con.execute("ATTACH '" + str(fx / "world.duckdb").replace("'", "''")
                    + "' AS wh (READ_ONLY)")
        con.execute("USE wh.warehouse")
        a, b = con.execute(
            "SELECT ROUND(AVG(CASE WHEN dt BETWEEN DATE '" + WIN_START
            + "' AND DATE '" + WIN_END + "' THEN aov END), 2),"
            " ROUND(AVG(CASE WHEN dt < DATE '" + WIN_START + "' THEN aov END), 2)"
            " FROM warehouse.kpi_daily").fetchone()
        con.close()
        ratio = round(float(a) / float(b), 3)
        if abs(ratio - CONTRACT["aov_ratio"]) > CONTRACT["aov_tolerance"]:
            problems.append(
                "AOV ratio " + str(ratio) + ", contract "
                + str(CONTRACT["aov_ratio"])
                + " - the symptom the task is built around is not present")

    if problems:
        raise FixtureDrift(
            "FIXTURE DOES NOT MATCH CONTRACT" + "\n\n  "
            + "\n  ".join(problems)
            + "\n\n  Regenerate:\n    python fixtures/make_fixture_world.py --out "
            + str(fx) + " --seed 1337"
            + "\n\n  Scores computed against a drifted fixture are meaningless.")

    if not quiet:
        print("  fixture verified against contract (AOV ratio " + str(ratio) + ")")


# ---------------------------------------------------------------- digests
def _connect_rw(db: Path):
    con = duckdb.connect(":memory:", config={"threads": "1"})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh")
    con.execute("USE wh")
    return con


def digest(fx: Path) -> dict:
    """Canonical content digests of every base table in world.duckdb and
    expected.duckdb: sha256 over the rows sorted on every column, as text.
    DuckDB file bytes are not stable across builds (free-list layout, header
    stamps); the contents are what the contract is about."""
    out = {}
    for name in ("world", "expected"):
        db = Path(fx) / f"{name}.duckdb"
        con = duckdb.connect(":memory:", config={"threads": "1"})
        con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh (READ_ONLY)")
        tables = con.execute(
            "select table_schema, table_name from information_schema.tables "
            "where table_catalog = 'wh' and table_type = 'BASE TABLE' "
            "order by 1, 2").fetchall()
        for schema, table in tables:
            cols = [c for (c,) in con.execute(
                "select column_name from information_schema.columns where table_catalog = 'wh' "
                "and table_schema = ? and table_name = ? order by ordinal_position",
                [schema, table]).fetchall()]
            order = ", ".join(f'"{c}"' for c in cols)
            h = hashlib.sha256()
            rows = con.execute(f'select * from wh."{schema}"."{table}" order by {order}').fetchall()
            for row in rows:
                h.update(json.dumps([str(v) for v in row]).encode("utf-8"))
                h.update(b"\n")
            out[f"{name}:{schema}.{table}"] = {"rows": len(rows), "sha256": h.hexdigest()}
        con.close()
    return out


# ----------------------------------------------------------- falsification
def _sql(db: Path, *statements: str) -> None:
    con = _connect_rw(db)
    try:
        for statement in statements:
            con.execute(statement)
    finally:
        con.close()


# Each entry: (id, what it damages, mutate(scratch_fixture_dir)). `falsify`
# requires verify() to REFUSE every one of them. If a mutation is not caught,
# the contract has a hole the width of that mutation.
MUTATIONS: list[tuple[str, str, object]] = [
    ("world_row_lost", "one order line deleted from the shipped world",
     lambda fx: _sql(fx / "world.duckdb",
                     "DELETE FROM warehouse.fct_orders WHERE rowid = (SELECT min(rowid) "
                     "FROM warehouse.fct_orders WHERE NOT is_manual_adjustment)")),
    ("world_adjustment_unflagged", "one manual adjustment loses its flag",
     lambda fx: _sql(fx / "world.duckdb",
                     "UPDATE warehouse.fct_orders SET is_manual_adjustment = FALSE WHERE rowid = "
                     "(SELECT min(rowid) FROM warehouse.fct_orders WHERE is_manual_adjustment)")),
    ("world_one_key_deduped", "one duplicated key repaired in the shipped world",
     lambda fx: _sql(fx / "world.duckdb",
                     "DELETE FROM warehouse.fct_orders WHERE rowid = (SELECT max(o.rowid) FROM "
                     "warehouse.fct_orders o JOIN (SELECT order_id, line_nbr FROM warehouse.fct_orders "
                     "GROUP BY 1,2 HAVING count(*) > 1 ORDER BY 1,2 LIMIT 1) d USING (order_id, line_nbr))")),
    ("world_symptom_gone", "the window's revenue doubled, so AOV no longer halves",
     lambda fx: _sql(fx / "world.duckdb",
                     "UPDATE warehouse.fct_revenue SET net_amount = net_amount * 2 "
                     f"WHERE order_dt BETWEEN DATE '{WIN_START}' AND DATE '{WIN_END}'")),
    ("expected_duplicate_planted", "one duplicated key planted in the reference",
     lambda fx: _sql(fx / "expected.duckdb",
                     "INSERT INTO warehouse.fct_orders SELECT * FROM warehouse.fct_orders "
                     "WHERE rowid = (SELECT min(rowid) FROM warehouse.fct_orders)")),
    ("expected_adjustment_lost", "one manual adjustment deleted from the reference",
     lambda fx: _sql(fx / "expected.duckdb",
                     "DELETE FROM warehouse.fct_orders WHERE rowid = (SELECT min(rowid) "
                     "FROM warehouse.fct_orders WHERE is_manual_adjustment)")),
    ("expected_missing", "the reference file is gone",
     lambda fx: (fx / "expected.duckdb").unlink()),
]


def falsify(fx: Path, quiet: bool = False) -> dict:
    """Copy the fixture to scratch, damage it one MUTATION at a time, and
    require verify() to refuse each. Returns {"mutations", "caught",
    "missed": [ids], "results": [{id, what, caught, message}]}."""
    results, missed = [], []
    for mid, what, mutate in MUTATIONS:
        scratch = Path(tempfile.mkdtemp(prefix="gt001-falsify-"))
        try:
            for name in ("world.duckdb", "expected.duckdb"):
                shutil.copy(Path(fx) / name, scratch / name)
            mutate(scratch)
            try:
                verify(scratch, quiet=True)
                caught, message = False, "verify() accepted the damaged fixture"
            except FixtureDrift as exc:
                caught, message = True, str(exc).splitlines()[2].strip() if len(
                    str(exc).splitlines()) > 2 else str(exc)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        results.append({"id": mid, "what": what, "caught": caught, "message": message})
        if not caught:
            missed.append(mid)
        if not quiet:
            print(f"  {'caught' if caught else 'MISSED'}  {mid:<28} {what}")
    return {"mutations": len(MUTATIONS), "caught": len(MUTATIONS) - len(missed),
            "missed": missed, "results": results}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures",
                    default=str(Path(__file__).parent / "fixtures" / "build"))
    ap.add_argument("--falsify", action="store_true")
    ap.add_argument("--digest", action="store_true")
    a = ap.parse_args()
    fx = Path(a.fixtures)
    if a.falsify:
        report = falsify(fx)
        print(f"  {report['caught']}/{report['mutations']} mutations caught")
        return 0 if not report["missed"] else 1
    if a.digest:
        print(json.dumps(digest(fx), indent=2, sort_keys=True))
        return 0
    try:
        verify(fx)
    except FixtureDrift as e:
        print(str(e))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
