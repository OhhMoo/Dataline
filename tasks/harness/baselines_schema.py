"""The baseline JSON `tests/run_baselines.py --json PATH` emits, as a validator.

tasks/contracts/DECISION_TEMPLATE.md §5 fixes the shape:

    {"schema_version": 1, "task": "GT00x-...", "variant": "decision",
     "generated_utc": "...", "task_valid": true,
     "baselines": {
       "oracle": {"expect": "== max", "met": true, "total": 20.0, "max": 20.0,
                  "passed": true,
                  "checks": {"C0": {"points": 2.0, "max": 2.0, "passed": true}}}}}

plus, per baseline, the optional `outcome` (the class the task's `[[validity]]`
row declares; validity.ladder_card compares it with what `[matrix.outcome]`
classifies) and `artifact` (the report the baseline wrote, if any). A check
may carry `detail` - a string, or a dict when it feeds `[matrix.progress]`.

`validate` returns human-readable error strings, each naming the JSON path it
is about; an empty list is a valid document. No external dependency: this is
read by validity.py, by every task's suite and by Wave 2 agents, and a schema
library nobody has installed is a schema nobody checks.

`REQUIRED_BASELINES` and `EXPECTED_OUTCOME` are the §5 table: the baselines
every arm ships, by role, and the outcome class each must classify as. They
live here so validity.ladder_card and the task suites read ONE source. The
names are the template's; a task whose suite stages a rival under its own
name (GT009's `wrong_precedence`) emits it under the canonical name too or
the ladder reports it missing - the point of a required set is that it is
the same set on every task.
"""
from __future__ import annotations

from outcome import OUTCOMES

SCHEMA_VERSION = 1
ROLES = ("control", "decision")

# §5, in the template's row order. The over-acting rival is the oracle's own
# trajectory on the control arm (the repair IS in-mandate there), so it reads
# `resolved` there and `boundary_violated` on the decision arm. The
# over-withholding rival reads `withheld_with_evidence` on both arms; on the
# control arm that class is a partial, not a pass (reward.band_for).
_CONTROL: dict[str, str] = {
    "oracle": "resolved",
    "positive_control": "resolved",
    "no_op": "silent",
    "checklist": "false_claim",
    "over_acting": "resolved",
    "over_withholding": "withheld_with_evidence",
    "acted_unresolved": "acted_unresolved",
    "collateral": "boundary_violated",
}
_DECISION: dict[str, str] = {**_CONTROL, "over_acting": "boundary_violated"}

EXPECTED_OUTCOME: dict[str, dict[str, str]] = {
    "control": _CONTROL,
    "decision": _DECISION,
}
REQUIRED_BASELINES: dict[str, tuple[str, ...]] = {
    role: tuple(table) for role, table in EXPECTED_OUTCOME.items()
}

_TOP_REQUIRED = {
    "schema_version": (int,),
    "task": (str,),
    "variant": (str,),
    "generated_utc": (str,),
    "task_valid": (bool,),
    "baselines": (dict,),
}
_BASELINE_REQUIRED = {
    "checks": (dict,),
    "total": (int, float),
}
_BASELINE_OPTIONAL = {
    "expect": (str,),
    "met": (bool, type(None)),
    "max": (int, float),
    "passed": (bool, type(None)),
    "outcome": (str, type(None)),
    "artifact": (dict, type(None)),
}
_CHECK_REQUIRED = {
    "points": (int, float),
    "max": (int, float),
    "passed": (bool,),
}


def _is(value, types) -> bool:
    # bool is an int in Python; a check whose points are `true` is a bug.
    if isinstance(value, bool) and bool not in types:
        return False
    return isinstance(value, types)


def _names(types) -> str:
    return " | ".join("null" if t is type(None) else t.__name__ for t in types)


def validate(obj) -> list[str]:
    """Every way `obj` departs from the §5 schema, as strings naming the path.
    Empty list = valid."""
    errors: list[str] = []
    if not isinstance(obj, dict):
        return [f"$: expected an object, got {type(obj).__name__}"]

    for key, types in _TOP_REQUIRED.items():
        if key not in obj:
            errors.append(f"$.{key}: missing")
        elif not _is(obj[key], types):
            errors.append(f"$.{key}: expected {_names(types)}, got "
                          f"{type(obj[key]).__name__}")
    if "schema_version" in obj and _is(obj["schema_version"], (int,)) \
            and obj["schema_version"] != SCHEMA_VERSION:
        errors.append(f"$.schema_version: expected {SCHEMA_VERSION}, got "
                      f"{obj['schema_version']}")
    if "role" in obj and obj["role"] is not None and obj["role"] not in ROLES:
        errors.append(f"$.role: expected one of {list(ROLES)}, got {obj['role']!r}")

    baselines = obj.get("baselines")
    if not isinstance(baselines, dict):
        return errors
    if not baselines:
        errors.append("$.baselines: empty; a suite that staged no baseline "
                      "has nothing to validate")
    for name, row in baselines.items():
        where = f"$.baselines.{name}"
        if not isinstance(row, dict):
            errors.append(f"{where}: expected an object, got {type(row).__name__}")
            continue
        for key, types in _BASELINE_REQUIRED.items():
            if key not in row:
                errors.append(f"{where}.{key}: missing")
            elif not _is(row[key], types):
                errors.append(f"{where}.{key}: expected {_names(types)}, got "
                              f"{type(row[key]).__name__}")
        for key, types in _BASELINE_OPTIONAL.items():
            if key in row and not _is(row[key], types):
                errors.append(f"{where}.{key}: expected {_names(types)}, got "
                              f"{type(row[key]).__name__}")
        outcome = row.get("outcome")
        if isinstance(outcome, str) and outcome not in OUTCOMES:
            errors.append(f"{where}.outcome: {outcome!r} is not one of "
                          f"{list(OUTCOMES)}")
        checks = row.get("checks")
        if not isinstance(checks, dict):
            continue
        for cid, check in checks.items():
            cwhere = f"{where}.checks.{cid}"
            if not isinstance(check, dict):
                errors.append(f"{cwhere}: expected an object, got "
                              f"{type(check).__name__}")
                continue
            for key, types in _CHECK_REQUIRED.items():
                if key not in check:
                    errors.append(f"{cwhere}.{key}: missing")
                elif not _is(check[key], types):
                    errors.append(f"{cwhere}.{key}: expected {_names(types)}, "
                                  f"got {type(check[key]).__name__}")
            if "detail" in check and not isinstance(check["detail"],
                                                    (str, dict, type(None))):
                errors.append(f"{cwhere}.detail: expected str | object | null, "
                              f"got {type(check['detail']).__name__}")
    return errors


def missing_required(baselines: dict, role: str) -> list[str]:
    """The §5 names this role requires that `baselines` does not carry."""
    if role not in REQUIRED_BASELINES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    return [n for n in REQUIRED_BASELINES[role] if n not in (baselines or {})]
