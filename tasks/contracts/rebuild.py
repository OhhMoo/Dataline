#!/usr/bin/env python3
"""Deterministic checks for a repair that must keep loading.

`run_twice` never writes the world it is given. It copies that world, runs the
pipeline once so the repair is in place, plants one delivery the pipeline has
not seen, then runs the pipeline twice more. The first of those runs is the one
that must ingest the delivery. The second must not ingest it again, and must
not change a business column on any row that was already there.

`plant` writes the delivery (a landing file, or SQL into a landing table). The
warehouse table grows only when a later pipeline run loads it, so the planted
row count is measured on `table` after `plant` returns and again after each of
the two following runs.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import _world as W

# business_fingerprint hashes a whole table. The planted delivery is new, so the
# comparison has to drop those rows or a correct load looks like a rewrite.
# There is no date column on this helper, so the slice is plant_spec['predicate'].
_SCRATCH = "warehouse.__rebuild_preexisting"


@dataclass(frozen=True)
class RunTwiceResult:
    business_identical: bool
    planted_once: bool
    liveness_failed: bool
    detail: str


@dataclass(frozen=True)
class RematerialiseResult:
    matches: bool
    detail: str


@dataclass(frozen=True)
class SecondSeedResult:
    hardcoded: bool
    detail: str


def run_twice(world_root: Path, steps: list, *, table: str, plant_spec: dict,
              audit_columns: list[str] | None = None) -> RunTwiceResult:
    """Copy `world_root`, repair, plant, ingest, replay.

    Three pipeline runs, all on the copy:

    1. Repair. Fingerprint business columns of `table` before and after
       (`audit_columns` excluded). The after-repair fingerprint of every row
       that does not match `plant_spec['predicate']` is the window that later
       runs must leave alone.
    2. Plant via `_world.plant` (see `_spec_for_plant`), then run `steps`.
       `count_before` is `key_count(predicate)` after plant and before this
       run. `count_mid` is the same count after it.
    3. Run `steps` again. `count_after` is the count after this replay.

    `planted_once` is `(count_mid - count_before) == int(plant_spec['rows'])`
    and `count_after == count_mid`. `liveness_failed` is that difference being
    0: the post-plant run ingested nothing. `business_identical` requires both
    the predicate-excluded fingerprint after the replay to equal the
    after-repair fingerprint, and the full business fingerprint after the
    replay to equal the fingerprint after the first post-plant run. A replay
    that rewrites the rows just loaded is not identical.

    A pipeline error does not raise. The result is all flags false, except
    `liveness_failed` when the post-plant run ingested nothing. `ValueError`
    if `plant_spec` has no `predicate` or no `rows`.
    """
    predicate, expected_rows = _require_plant(plant_spec)
    audit = list(audit_columns or [])
    normalised = W.normalise_steps(steps)
    with tempfile.TemporaryDirectory(prefix="contract-rebuild-") as tmp:
        root = W.copy_world(world_root, Path(tmp) / "world")
        return _execute(root, normalised, table, plant_spec, predicate,
                        expected_rows, audit)


def rematerialise_matches(agent_digest: str, rebuilt_digest: str) -> RematerialiseResult:
    """True only when the agent digest is non-empty and equals the rebuild."""
    if not agent_digest:
        return RematerialiseResult(False, "empty: agent digest is empty")
    if agent_digest == rebuilt_digest:
        return RematerialiseResult(
            True, "match: agent digest equals the rebuilt digest")
    return RematerialiseResult(
        False, "mismatch: agent digest does not equal the rebuilt digest")


def second_seed_hardcoded(rows_seed_b: list, expected_b: list, *,
                          rows_seed_a: list | None = None) -> SecondSeedResult:
    """True means the seed-B output was copied from seed A.

    That is a failure: seed B reproduced seed A's rows and those rows are not
    `expected_b`. An honest bug (seed B differs from both) is not hardcoded.
    Without seed A's rows the check is not evaluated.
    """
    if rows_seed_a is None:
        return SecondSeedResult(False, "not evaluated")
    hardcoded = rows_seed_b == rows_seed_a and rows_seed_b != expected_b
    if hardcoded:
        detail = "seed-B output equals seed-A and differs from expected_b"
    elif rows_seed_b == expected_b:
        detail = "seed-B output matches expected_b"
    else:
        detail = "seed-B output differs from expected_b without copying seed-A"
    return SecondSeedResult(hardcoded, detail)


def _require_plant(spec: dict) -> tuple[str, int]:
    if not isinstance(spec, dict):
        raise ValueError("plant_spec must be a dict with 'predicate' and 'rows'")
    predicate = spec.get("predicate")
    if not isinstance(predicate, str) or not predicate.strip():
        raise ValueError("plant_spec lacks 'predicate'")
    if "rows" not in spec or spec.get("rows") is None or spec.get("rows") == "":
        raise ValueError("plant_spec lacks 'rows'")
    try:
        rows = int(spec["rows"])
    except (TypeError, ValueError) as exc:
        raise ValueError("plant_spec['rows'] must be an integer") from exc
    return predicate, rows


def _spec_for_plant(spec: dict) -> dict:
    """Adapt a plant spec to `_world.plant` without a second planter.

    `plant` accepts mode=sql (`sql`) and mode=landing (`dir`, `filename`,
    `header`, `row_template`), plus `date`, `rows`, `id_base`, `amount`.
    `landing_sql` is the same statement as `sql` for callers that named it
    that way.
    """
    out = dict(spec)
    if out.get("landing_sql") and not out.get("sql"):
        out["sql"] = out["landing_sql"]
        out.setdefault("mode", "sql")
    return out


def _failed(detail: str, *, ingested: bool) -> RunTwiceResult:
    # A failed run does not get credit for a stable window or a clean ingest.
    # liveness_failed stays true when that run added no predicate rows.
    return RunTwiceResult(False, False, not ingested, detail)


def _snapshot(root: Path, table: str, audit: list, predicate: str) -> tuple[str, str, int]:
    con = W.open_world(W.warehouse_path(root))
    try:
        cols = W.business_columns(con, table, audit)
        if cols:
            full = W.business_fingerprint(con, table, cols)
        else:
            full = "NO_BUSINESS_COLUMNS"
        excluded = _fingerprint_excluding(con, table, cols, predicate)
        count = W.key_count(con, table, predicate)
        return full, excluded, count
    finally:
        con.close()


def _fingerprint_excluding(con, table: str, cols: list, predicate: str) -> str:
    """`business_fingerprint` of the rows that do not match `predicate`.

    The helper hashes every row of one table, so the excluded window is
    materialised in the copy and removed before this connection closes.
    """
    if not cols:
        return "NO_BUSINESS_COLUMNS"
    listed = ", ".join('"%s"' % c for c in cols)
    scratch = W.q(_SCRATCH)
    con.execute(f"DROP TABLE IF EXISTS {scratch}")
    con.execute(
        f"CREATE TABLE {scratch} AS SELECT {listed} FROM {W.q(table)} "
        f"WHERE NOT ({predicate})")
    try:
        return W.business_fingerprint(con, _SCRATCH, cols)
    finally:
        con.execute(f"DROP TABLE IF EXISTS {scratch}")


def _pipeline(root: Path, steps: list) -> str | None:
    return W.pipeline_failed(W.run_pipeline(root, steps))


def _execute(root: Path, steps: list, table: str, plant_spec: dict,
             predicate: str, expected_rows: int, audit: list) -> RunTwiceResult:
    try:
        _full0, excl_before, _n0 = _snapshot(root, table, audit, predicate)
    except Exception as exc:                                # noqa: BLE001
        return _failed(
            f"could not fingerprint {table} before the repair run: "
            f"{type(exc).__name__}: {exc}",
            ingested=False)

    err = _pipeline(root, steps)
    if err:
        return _failed(f"repair run failed: {err}", ingested=False)

    try:
        full_repair, excl_repair, _n_repair = _snapshot(root, table, audit, predicate)
    except Exception as exc:                                # noqa: BLE001
        return _failed(
            f"could not fingerprint {table} after the repair run: "
            f"{type(exc).__name__}: {exc}",
            ingested=False)

    try:
        planted = W.plant(root, _spec_for_plant(plant_spec))
    except Exception as exc:                                # noqa: BLE001
        return _failed(f"plant failed: {type(exc).__name__}: {exc}", ingested=False)

    try:
        _full_b, _excl_b, count_before = _snapshot(root, table, audit, predicate)
    except Exception as exc:                                # noqa: BLE001
        return _failed(
            f"could not count {table} after plant: {type(exc).__name__}: {exc}",
            ingested=False)

    err = _pipeline(root, steps)
    try:
        full_mid, _excl_m, count_mid = _snapshot(root, table, audit, predicate)
    except Exception as exc:                                # noqa: BLE001
        return _failed(
            f"post-plant run failed: {err or type(exc).__name__}: {exc}",
            ingested=False)
    if err:
        return _failed(
            f"post-plant run failed: {err}",
            ingested=(count_mid - count_before) != 0)

    err = _pipeline(root, steps)
    try:
        full_final, excl_final, count_after = _snapshot(root, table, audit, predicate)
    except Exception as exc:                                # noqa: BLE001
        return _failed(
            f"replay run failed: {err or type(exc).__name__}: {exc}",
            ingested=(count_mid - count_before) != 0)
    if err:
        return _failed(
            f"replay run failed: {err}",
            ingested=(count_mid - count_before) != 0)

    delta = count_mid - count_before
    planted_once = delta == expected_rows and count_after == count_mid
    liveness_failed = delta == 0
    business_identical = excl_final == excl_repair and full_mid == full_final
    window = ("kept the repaired business fingerprint"
              if business_identical else
              "changed business columns after the repaired fingerprint was taken")
    detail = (
        f"rows outside `{predicate}` {window} "
        f"({excl_repair} -> {excl_final}); "
        f"full-table fingerprint after repair {full_repair}; "
        f"predicate count {count_before} -> {count_mid} -> {count_after} "
        f"against {expected_rows} planted row(s). {planted}"
    )
    if excl_before != excl_repair:
        detail += " The repair run itself changed pre-existing business columns."
    return RunTwiceResult(business_identical, planted_once, liveness_failed, detail)
