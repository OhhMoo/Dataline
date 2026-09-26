#!/usr/bin/env python3
"""Side-module rebuild checks for GT008-warehouse-cutover.

NOT WIRED into the official grader. Official pass, score and reward are
unchanged. Nothing here is imported by ``tests/grade.py``.

The control arm is ``default`` (task.toml ``[variants.default]``,
``role = "control"``). The fix is ``pipelines/load_nova.py`` on the nova
warehouse (the primary). It full-reloads ``raw.order_events`` and
``raw.finance_adjustments``, then rebuilds ``warehouse.fct_orders``
(one row per ``order_id``, highest version, deletes absent) and
``warehouse.rpt_revenue_daily``. ``grade.compare`` scores those two
marts as C0–C4. ``legacy.warehouse.fct_orders`` and
``legacy.warehouse.rpt_revenue_daily`` are consumer-owned; the mandate
reads them and does not write them (task.toml ``[matrix.path_allow]``).
``warehouse.v_revenue_daily`` is the view the cutover repoints, not a
table the loader rebuilds.

The decision arm is ``decision``. ``withheld_with_evidence`` is priced
at the top of the band and is not the accepted trajectory (DESIGN.md
§2: the record already authorised retirement; holding ``legacy_nightly``
is the over-withhold). ``check_ids`` for ``decision`` is empty. The
three side checks are annotated in ``describe()['applies_to']`` on
``default`` only.

``describe`` and ``check_ids`` do not import ``tasks.contracts.rebuild``.
The runners call ``_api()``, which is the only import of ``run_twice``,
``rematerialise_matches`` and ``second_seed_hardcoded``.
"""
from __future__ import annotations

import sys
from pathlib import Path

TASK = "GT008-warehouse-cutover"
TABLE = "warehouse.fct_orders"
TABLES = (
    "raw.order_events",
    "raw.finance_adjustments",
    "warehouse.fct_orders",
    "warehouse.rpt_revenue_daily",
)
BUSINESS_KEY = ("order_id",)
DATE_COLUMN = "order_day"
AUDIT_COLUMNS = ()
PIPELINE_STEPS = (
    "pipelines/load_nova.py",
)
BUSINESS_PREDICATE = (
    "one row per order_id in warehouse.fct_orders at the highest "
    "delivered version; a delete at that version is absent; "
    "warehouse.rpt_revenue_daily is that mart rolled to order_day"
)
CONTROL_ARM_CHANGE = (
    "pipelines/load_nova.py: full reload of the orders feed and approved "
    "adjustments into nova, then warehouse.fct_orders and "
    "warehouse.rpt_revenue_daily rebuilt under version precedence"
)
OFFICIAL_SEED = 8008
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
    The decision arm ``decision``, whose success can withhold the
    cutover: ``[]``.
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
            f"cutover; {check} applies where the fix is a pipeline change "
            f"({', '.join(CONTROL_ARMS)})"
        ),
    }


def idempotent_rebuild(world_root, arm: str | None = None, *, steps: list | None = None, plant_spec: dict | None = None, audit_columns: list | None = None):
    """Run the nova loader twice via ``tasks.contracts.rebuild.run_twice``.

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

    ``rebuilt_digest`` must come from re-running ``pipelines/load_nova.py``
    from the feed, not from hashing the leftover mart twice.
    """
    if arm is not None and arm not in CONTROL_ARMS:
        return _withheld(arm, "rematerialise_matches")
    if agent_digest is None or rebuilt_digest is None:
        raise NotImplementedError(
            "not wired: pass agent_digest and rebuilt_digest from a from-feed reload")
    _, match, _ = _api()
    return match(agent_digest, rebuilt_digest)


def second_seed(world_root, arm: str | None = None, *, rows_seed_a: list | None = None, rows_seed_b: list | None = None, expected_b: list | None = None):
    """Compare seed outputs via ``second_seed_hardcoded``.

    ``OFFICIAL_SEED`` is the episode seed (8008). This module does not pick
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
