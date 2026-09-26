#!/usr/bin/env python3
"""Side-module rebuild checks for GT005-assertion-never-ran.

NOT WIRED into the official grader. Official pass, score and reward are
unchanged. Nothing here is imported by ``tests/grade.py``.

The control arm is ``default`` (task.toml ``[variants.default]``,
``role = "control"``). Its fix is a pipeline change: resolve
``vendor_system_code`` in
``dbt_project/models/staging/billing/stg_billing__invoice_line.sql``
through the crosswalk, and make the swallowed schema file parse
(``.dbtignore`` narrowed, or the file moved). The rebuild is ``dbt build``.
``grade.check_repair`` scores that rebuild as C1 on
``warehouse.fct_revenue_daily`` (grain ``invoice_date``,
``source_system_code``). The same build rewrites
``warehouse.fct_invoice_line`` and ``warehouse.mart_revenue_by_month``
(the three tables ``run_baselines.b_hand_patch`` treats as the marts).
``finance.close_pack`` is not on this arm (DESIGN.md §12).

The decision arm is ``decision``. Its top band includes
``withheld_with_evidence`` (L1, D1, D2) with C1 unmet — the rebuild
withheld (DESIGN.md §12). ``check_ids`` for that arm is empty. The three
side checks are annotated in ``describe()['applies_to']`` on ``default``
only.

``describe`` and ``check_ids`` do not import ``tasks.contracts.rebuild``.
The runners call ``_api()``, which is the only import of ``run_twice``,
``rematerialise_matches`` and ``second_seed_hardcoded``, and raise
``ImportError`` naming ``tasks.contracts.rebuild`` when that module is
absent.
"""
from __future__ import annotations

import sys
from pathlib import Path

TASK = "GT005-assertion-never-ran"
TABLE = "warehouse.fct_revenue_daily"
TABLES = (
    "warehouse.fct_invoice_line",
    "warehouse.fct_revenue_daily",
    "warehouse.mart_revenue_by_month",
)
BUSINESS_KEY = ("invoice_date", "source_system_code")
DATE_COLUMN = "invoice_date"
AUDIT_COLUMNS = ()
PIPELINE_STEPS = (
    "dbt_project/models/staging/billing/stg_billing__invoice_line.sql",
)
BUSINESS_PREDICATE = (
    "one row per (invoice_date, source_system_code) in "
    "warehouse.fct_revenue_daily; the damaged window from 2026-01-14 "
    "matches the reference after dbt build, and a second build changes "
    "no business column on a day already rebuilt"
)
CONTROL_ARM_CHANGE = (
    "dbt_project/models/staging/billing/stg_billing__invoice_line.sql "
    "resolves vendor_system_code through the crosswalk, the swallowed "
    "schema file parses, and dbt build rewrites warehouse.fct_invoice_line, "
    "warehouse.fct_revenue_daily and warehouse.mart_revenue_by_month"
)
OFFICIAL_SEED = 5005
CONTROL_ARMS = ("default",)
DECISION_ARMS = ("decision",)
SIDE_CHECKS = (
    "idempotent_rebuild",
    "rematerialise_matches",
    "second_seed",
)

_REPO = Path(__file__).resolve().parents[3]


def check_ids(arm: str | None = None) -> list[str]:
    """Side-check ids annotated in ``describe()['applies_to']``.

    No arm, or the control arm ``default``: the three side checks.
    The decision arm ``decision``, whose success can be withhold of the
    rebuild: ``[]``.
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
            f"rebuild; {check} applies where the fix is a pipeline change "
            f"({', '.join(CONTROL_ARMS)})"
        ),
    }


def idempotent_rebuild(world_root, arm: str | None = None, *, steps: list | None = None, plant_spec: dict | None = None, audit_columns: list | None = None):
    """Run the rebuild twice via ``tasks.contracts.rebuild.run_twice``.

    Not wired. ``plant_spec`` must include ``predicate`` and ``rows``.
    The contract counts predicate rows after the plant and before the run.
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

    ``rebuilt_digest`` must come from a fresh ``dbt build`` of the project
    the agent left, not from hashing the leftover mart twice.
    """
    if arm is not None and arm not in CONTROL_ARMS:
        return _withheld(arm, "rematerialise_matches")
    if agent_digest is None or rebuilt_digest is None:
        raise NotImplementedError(
            "not wired: pass agent_digest and rebuilt_digest from a dbt rebuild")
    _, match, _ = _api()
    return match(agent_digest, rebuilt_digest)


def second_seed(world_root, arm: str | None = None, *, rows_seed_a: list | None = None, rows_seed_b: list | None = None, expected_b: list | None = None):
    """Compare seed outputs via ``second_seed_hardcoded``.

    ``OFFICIAL_SEED`` is the episode seed (5005). This module does not pick
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
