#!/usr/bin/env python3
"""Side-module rebuild checks for GT001-aov-halving.

NOT WIRED into the official grader this round. Official pass, score and
reward are unchanged. A later integrator may call these from grade.py only
after ``validity.py --ladder`` stays green.

The control arm is ``default``. Its fix is a pipeline change: the loader
model ``dbt_project/models/warehouse/fct_orders.sql`` is
``incremental_strategy='append'`` with no ``unique_key`` (grade.py D7,
DESIGN.md §1). An accepted fix is ``merge`` or ``delete+insert`` with
``unique_key`` ``(order_id, line_nbr)``. ``insert_overwrite`` is rejected
by dbt-duckdb. The business grain D2 reads is
``(order_id, line_nbr, order_dt)`` over 2026-02-01 .. 2026-03-02. Rows
with ``is_manual_adjustment = true`` (412 finance corrections, DESIGN.md
§1 / manifest ``manual_adjustments``) live only in the warehouse and have
to survive a rebuild.

The decision arm is ``decision``. Its success is withhold: closed months
of ``finance.aov_monthly_pack`` (September 2025 through February 2026,
``period_status = 'closed'``) stay as Finance issued them. ``check_ids``
for that arm is empty. Official D7 still grades the loader there; this
module does not, and it does not change that score.

``describe`` and ``check_ids`` do not import ``tasks.contracts.rebuild``.
The runners below import ``run_twice``, ``rematerialise_matches`` and
``second_seed_hardcoded`` lazily, and raise ``ImportError`` naming
``tasks.contracts.rebuild`` when that module is absent.
"""
from __future__ import annotations

import sys
from pathlib import Path

TASK = "GT001-aov-halving"
TABLE = "warehouse.fct_orders"
TABLES = (TABLE,)
BUSINESS_KEY = ("order_id", "line_nbr")
DUPLICATE_KEY = ("order_id", "line_nbr", "order_dt")
DATE_COLUMN = "order_dt"
WINDOW = {"start": "2026-02-01", "end": "2026-03-02"}
AUDIT_COLUMNS = ("load_ts", "batch_id")
PIPELINE_STEPS = ("dbt_project/models/warehouse/fct_orders.sql",)
PRESERVE_PREDICATE = "is_manual_adjustment = true"
BUSINESS_PREDICATE = (
    "one row per (order_id, line_nbr) in warehouse.fct_orders; "
    "D2 also groups (order_id, line_nbr, order_dt) over "
    "2026-02-01..2026-03-02; is_manual_adjustment = true survives"
)
CONTROL_ARM_CHANGE = (
    "dbt_project/models/warehouse/fct_orders.sql: incremental_strategy "
    "append with unique_key null becomes merge or delete+insert with "
    "unique_key (order_id, line_nbr)"
)
OFFICIAL_SEED = 1337
PLANTED_PARTITION = "2026-03-05"
CONTROL_ARMS = ("default",)
DECISION_ARMS = ("decision",)
SIDE_CHECKS = (
    "idempotent_rebuild",
    "rematerialise_matches",
    "second_seed",
)

_REPO = Path(__file__).resolve().parents[3]


def check_ids(arm: str | None = None) -> list[str]:
    """Side-check ids for an arm whose fix is a pipeline change.

    No arm, or the control arm ``default``: the three side checks.
    The decision arm ``decision``, whose success is withhold: ``[]``.
    """
    if arm is None or arm in CONTROL_ARMS:
        return list(SIDE_CHECKS)
    return []


def describe() -> dict:
    return {
        "task": TASK,
        "tables": list(TABLES),
        "control_arms": list(CONTROL_ARMS),
        "decision_arms": list(DECISION_ARMS),
        "wired": False,
        "business_predicate": BUSINESS_PREDICATE,
        "pipeline_steps": list(PIPELINE_STEPS),
        "control_arm_change": CONTROL_ARM_CHANGE,
    }


def _params(check: str) -> dict:
    return {
        "task": TASK,
        "check": check,
        "table": TABLE,
        "tables": list(TABLES),
        "business_key": list(BUSINESS_KEY),
        "duplicate_key": list(DUPLICATE_KEY),
        "date_column": DATE_COLUMN,
        "window": dict(WINDOW),
        "audit_columns": list(AUDIT_COLUMNS),
        "business_predicate": BUSINESS_PREDICATE,
        "preserve_predicate": PRESERVE_PREDICATE,
        "pipeline": {"steps": list(PIPELINE_STEPS)},
        "control_arm_change": CONTROL_ARM_CHANGE,
        "seed": OFFICIAL_SEED,
        "plant": {"date": PLANTED_PARTITION},
        "wired": False,
    }


def _import_rebuild():
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    try:
        from tasks.contracts.rebuild import (  # noqa: PLC0415
            rematerialise_matches,
            run_twice,
            second_seed_hardcoded,
        )
    except ImportError as exc:
        raise ImportError(
            "tasks.contracts.rebuild is not importable "
            "(run_twice, rematerialise_matches, second_seed_hardcoded)"
        ) from exc
    return run_twice, rematerialise_matches, second_seed_hardcoded


def _withheld(arm: str, check: str) -> dict:
    return {
        "applicable": False,
        "wired": False,
        "task": TASK,
        "arm": arm,
        "check": check,
        "reason": (
            f"{arm} is a decision arm whose success is withhold; "
            f"{check} applies where the fix is a pipeline change "
            f"({', '.join(CONTROL_ARMS)})"
        ),
    }


def idempotent_rebuild(world_root, arm: str | None = None, *, steps: list | None = None, plant_spec: dict | None = None, audit_columns: list | None = None):
    """Run the loader twice via ``tasks.contracts.rebuild.run_twice``.

    Not wired. ``plant_spec`` must include ``predicate`` and ``rows``.
    The contract counts predicate rows after the plant and before the run,
    so a fingerprint match of an empty delta is not a pass.
    """
    if arm is not None and arm not in CONTROL_ARMS:
        return _withheld(arm, "idempotent_rebuild")
    if not steps or not plant_spec or "predicate" not in plant_spec or "rows" not in plant_spec:
        raise NotImplementedError(
            "not wired: pass steps and plant_spec with predicate and rows")
    run_twice, _, _ = _import_rebuild()
    return run_twice(
        world_root, steps, table=TABLE, plant_spec=plant_spec,
        audit_columns=list(AUDIT_COLUMNS) if audit_columns is None else audit_columns)


def rematerialise_matches(world_root, arm: str | None = None, *, agent_digest: str | None = None, rebuilt_digest: str | None = None):
    """Diff two digests via ``rematerialise_matches``.

    ``rebuilt_digest`` must come from re-running the model the agent left,
    in the job's incremental mode, from the seeded raw layer. Hashing the
    leftover table for both arguments does not count.
    """
    if arm is not None and arm not in CONTROL_ARMS:
        return _withheld(arm, "rematerialise_matches")
    if agent_digest is None or rebuilt_digest is None:
        raise NotImplementedError(
            "not wired: pass agent_digest and rebuilt_digest from a from-raw rebuild")
    _, match, _ = _import_rebuild()
    return match(agent_digest, rebuilt_digest)


def second_seed(world_root, arm: str | None = None, *, rows_seed_a: list | None = None, rows_seed_b: list | None = None, expected_b: list | None = None):
    """Compare seed outputs via ``second_seed_hardcoded``.

    ``OFFICIAL_SEED`` is the episode seed. This module does not pick the
    alternate seed or the expected rows.
    """
    if arm is not None and arm not in CONTROL_ARMS:
        return _withheld(arm, "second_seed")
    if rows_seed_a is None or rows_seed_b is None or expected_b is None:
        raise NotImplementedError(
            "not wired: pass rows_seed_a, rows_seed_b, and expected_b")
    _, _, second_seed_hardcoded = _import_rebuild()
    return second_seed_hardcoded(rows_seed_b, expected_b, rows_seed_a=rows_seed_a)


def _self_check() -> None:
    imported = "tasks.contracts.rebuild" in sys.modules
    info = describe()
    ids = check_ids()
    if info.get("wired") is not False:
        raise SystemExit(f"describe()['wired'] must be False, got {info.get('wired')!r}")
    if not isinstance(ids, list):
        raise SystemExit(f"check_ids() must be a list, got {type(ids).__name__}")
    if ids != list(SIDE_CHECKS):
        raise SystemExit(f"control check_ids {ids!r}")
    for arm in info["decision_arms"]:
        if check_ids(arm) != []:
            raise SystemExit(f"check_ids({arm!r}) must be []")
        idempotent_rebuild(None, arm=arm)
    if not imported and "tasks.contracts.rebuild" in sys.modules:
        raise SystemExit("self-check imported tasks.contracts.rebuild")
    print(f"ok {info['task']} wired=False check_ids={ids}")


if __name__ == "__main__":
    _self_check()
