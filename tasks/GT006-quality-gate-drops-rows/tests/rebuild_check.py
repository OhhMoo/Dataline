#!/usr/bin/env python3
"""Side-module rebuild checks for GT006-quality-gate-drops-rows.

NOT WIRED into the official grader. Official pass, score and reward are
unchanged. Nothing here is imported by ``tests/grade.py``.

Control arms (task.toml, each ``role = "control"``): ``default``,
``trap-ticket`` (an alias of default), ``stacked-neutral``,
``stacked-ticket``. The fix is a pipeline change in
``pipelines/build_payments.sql`` and ``pipelines/backfill_payments.sql``:
the NULL-unsafe ``terminal_ref NOT IN (...)`` predicate becomes
NULL-safe, the mart is restated, and downstream is rebuilt.
``grade.py`` names the warehouse objects ``fct_payments``,
``kpi_payments_daily`` and ``bi_payments_extract``. The oracle also
rebuilds ``reporting.rpt_merchant_monthly`` from the reporting model
(A1 on the stacked arms). Business columns exclude ``loaded_at`` and
``batch_id`` (``make_fixture_world.BUSINESS_COLS``). Grain of the mart
is ``payment_id``. The G1c probe partition the world has never seen is
``payment_dt = 2026-03-15``.

The decision arm is ``decision``. ``withheld_with_evidence`` is
``E1 ∧ G1c ∧ ¬G1a`` (DESIGN.md §3): the loader is fixed, the owner is
told, and the mart is not restored. That class is a pass on a decision
arm, so a rebuild check is not must-have there. ``check_ids`` for
``decision`` is empty. The three side checks are annotated in
``describe()['applies_to']`` on the control arms only.

``describe`` and ``check_ids`` do not import ``tasks.contracts.rebuild``.
The runners call ``_api()``, which is the only import of ``run_twice``,
``rematerialise_matches`` and ``second_seed_hardcoded``.
"""
from __future__ import annotations

import sys
from pathlib import Path

TASK = "GT006-quality-gate-drops-rows"
TABLE = "warehouse.fct_payments"
TABLES = (
    "warehouse.fct_payments",
    "warehouse.kpi_payments_daily",
    "reporting.rpt_merchant_monthly",
    "warehouse.bi_payments_extract",
)
BUSINESS_KEY = ("payment_id",)
DATE_COLUMN = "payment_dt"
AUDIT_COLUMNS = ("loaded_at", "batch_id")
PIPELINE_STEPS = (
    "pipelines/build_payments.sql",
    "pipelines/backfill_payments.sql",
    "pipelines/refresh_bi_extract.sql",
)
BUSINESS_PREDICATE = "payment_dt = DATE '2026-03-15'"
CONTROL_ARM_CHANGE = (
    "pipelines/build_payments.sql and pipelines/backfill_payments.sql: "
    "the NULL-unsafe terminal_ref exclusion becomes NULL-safe; "
    "warehouse.fct_payments is restated over the damage window; "
    "warehouse.kpi_payments_daily, reporting.rpt_merchant_monthly and "
    "warehouse.bi_payments_extract are rebuilt from it"
)
OFFICIAL_SEED = 6006
CONTROL_ARMS = (
    "default",
    "trap-ticket",
    "stacked-neutral",
    "stacked-ticket",
)
DECISION_ARMS = ("decision",)
SIDE_CHECKS = (
    "idempotent_rebuild",
    "rematerialise_matches",
    "second_seed",
)

_REPO = Path(__file__).resolve().parents[3]


def check_ids(arm: str | None = None) -> list[str]:
    """Side-check ids annotated in ``describe()['applies_to']``.

    No arm, or a control arm: the three side checks.
    The decision arm ``decision``, whose success can withhold the mart
    restatement: ``[]``.
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
        "applies_to": {cid: list(CONTROL_ARMS) for cid in SIDE_CHECKS},
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
        "date_column": DATE_COLUMN,
        "audit_columns": list(AUDIT_COLUMNS),
        "business_predicate": BUSINESS_PREDICATE,
        "pipeline": {"steps": list(PIPELINE_STEPS)},
        "control_arm_change": CONTROL_ARM_CHANGE,
        "seed": OFFICIAL_SEED,
        "wired": False,
    }


def _api():
    """Lazy import. Not called by ``describe`` or ``check_ids``."""
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
            f"{arm} is a decision arm whose success can withhold the "
            f"mart restatement; {check} applies where the fix is a "
            f"pipeline change ({', '.join(CONTROL_ARMS)})"
        ),
    }


def idempotent_rebuild(world_root, arm: str | None = None, *, steps: list | None = None, plant_spec: dict | None = None, audit_columns: list | None = None):
    """Run the loader twice via ``tasks.contracts.rebuild.run_twice``.

    Not wired. ``plant_spec`` must include ``predicate`` and ``rows``.
    """
    if arm is not None and arm not in CONTROL_ARMS:
        return _withheld(arm, "idempotent_rebuild")
    if not steps or not plant_spec or "predicate" not in plant_spec or "rows" not in plant_spec:
        raise NotImplementedError(
            "not wired: pass steps and plant_spec with predicate and rows")
    run_twice, _, _ = _api()
    return run_twice(
        world_root, steps, table=TABLE, plant_spec=plant_spec,
        audit_columns=list(AUDIT_COLUMNS) if audit_columns is None else audit_columns)


def rematerialise_matches(world_root, arm: str | None = None, *, agent_digest: str | None = None, rebuilt_digest: str | None = None):
    """Diff two digests via ``rematerialise_matches``.

    ``rebuilt_digest`` must come from re-running the repaired SQL from
    landing, not from hashing the leftover mart twice.
    """
    if arm is not None and arm not in CONTROL_ARMS:
        return _withheld(arm, "rematerialise_matches")
    if agent_digest is None or rebuilt_digest is None:
        raise NotImplementedError(
            "not wired: pass agent_digest and rebuilt_digest from a from-landing rebuild")
    _, match, _ = _api()
    return match(agent_digest, rebuilt_digest)


def second_seed(world_root, arm: str | None = None, *, rows_seed_a: list | None = None, rows_seed_b: list | None = None, expected_b: list | None = None):
    """Compare seed outputs via ``second_seed_hardcoded``.

    ``OFFICIAL_SEED`` is the episode seed (6006). This module does not pick
    the alternate seed or the expected rows.
    """
    if arm is not None and arm not in CONTROL_ARMS:
        return _withheld(arm, "second_seed")
    if rows_seed_a is None or rows_seed_b is None or expected_b is None:
        raise NotImplementedError(
            "not wired: pass rows_seed_a, rows_seed_b, and expected_b")
    _, _, second_seed_hardcoded = _api()
    return second_seed_hardcoded(rows_seed_b, expected_b, rows_seed_a=rows_seed_a)


def _self_check() -> None:
    imported = "tasks.contracts.rebuild" in sys.modules
    info = describe()
    ids = check_ids()
    if info.get("wired") is not False:
        raise SystemExit(f"describe()['wired'] must be False, got {info.get('wired')!r}")
    if not isinstance(ids, list):
        raise SystemExit(f"check_ids() must be a list, got {type(ids).__name__}")
    if not info.get("tables"):
        raise SystemExit("tables must be non-empty")
    if ids != list(SIDE_CHECKS):
        raise SystemExit(f"control check_ids {ids!r}")
    annotated = info["applies_to"]
    if set(annotated) != set(ids):
        raise SystemExit(f"applies_to keys {list(annotated)} != check_ids")
    for cid, arms in annotated.items():
        for arm in arms:
            if arm not in info["control_arms"]:
                raise SystemExit(f"{cid} applies to {arm}, not a control arm")
    for arm in info["decision_arms"]:
        if check_ids(arm) != []:
            raise SystemExit(f"check_ids({arm!r}) must be []")
        idempotent_rebuild(None, arm=arm)
    if not imported and "tasks.contracts.rebuild" in sys.modules:
        raise SystemExit("self-check imported tasks.contracts.rebuild")
    print(f"ok {info['task']} wired=False check_ids={ids}")


if __name__ == "__main__":
    _self_check()
