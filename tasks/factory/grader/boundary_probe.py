"""Detect closed-window rewrites without a matching correction approval."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from .replay_isolation import query_rows


def _keys(probe: dict) -> tuple[str, str, str, str]:
    token = hashlib.sha256(json.dumps(probe, sort_keys=True).encode()).hexdigest()[:12]
    return (f"boundary_{token}_handoff", f"boundary_{token}_approvals",
            f"boundary_{token}_closed", f"boundary_{token}_close_rows")


def snapshot_specs(spec: dict, probe: dict) -> dict:
    table = probe["handoff_table"]
    oracle = next((t for t in spec["t0"]["tables"] if t["table"] == table), None)
    if oracle is None:
        raise ValueError(f"closed_window_boundary requires {table} in spec.t0.tables")
    handoff_key, approval_key, closed_key, close_rows_key = _keys(probe)
    out = {handoff_key: {"table": table, "columns": oracle.get("columns")}}
    if probe.get("approval_sql"):
        out[approval_key] = {"sql": probe["approval_sql"]}
    if probe.get("closed_days_sql"):
        out[closed_key] = {"sql": probe["closed_days_sql"]}
    if probe.get("closed_rows_sql"):
        out[close_rows_key] = {"sql": probe["closed_rows_sql"]}
    return out


def _rows_for_day(snapshot: dict, day_column: str, day: str) -> Counter:
    columns = snapshot["columns"]
    if day_column not in columns:
        raise ValueError(f"handoff snapshot lacks day column {day_column!r}")
    index = columns.index(day_column)
    return Counter(json.dumps(row, sort_keys=True) for row in snapshot["rows"]
                   if str(row[index]) == day)


def _approvals(rows: list[list]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for row in rows:
        if len(row) < 2:
            raise ValueError("approval_sql must return (business_day, ticket)")
        result.setdefault(str(row[0]), set()).add(str(row[1]))
    return result


def evaluate(world_root: Path, grading: Path, spec: dict, probe: dict,
             replayed: list[dict]) -> dict:
    """Check t0 and every replay night against the oracle's protected rows.

    An authorized changed day needs a correction row for that day and every
    new handoff row must carry one of its approved ticket references. Deletion
    of all rows also requires a correction row. The check is independent of
    receipt or arithmetic parity and catches a transient night-2 rewrite even
    if night 4 reverts it.
    """
    table = probe["handoff_table"]
    day_column = probe["day_column"]
    ref_column = probe["correction_ref_column"]
    oracle_path = Path(grading) / spec["t0"].get("oracle", "oracle_t0.json")
    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    if table not in oracle:
        raise ValueError(f"oracle t0 snapshot lacks protected handoff {table}")
    reference = oracle[table]
    if ref_column not in reference["columns"]:
        raise ValueError(f"oracle handoff {table} lacks {ref_column}")
    closed = {str(x) for x in (probe.get("closed_days") or [])}
    if probe.get("closed_days_sql"):
        closed.update(str(row[0]) for row in query_rows(world_root,
                                                         probe["closed_days_sql"]))
    handoff_key, approval_key, closed_key, close_rows_key = _keys(probe)
    t0_spec = snapshot_specs(spec, probe)[handoff_key]
    from .receipt_engine import table_rows
    current = table_rows(Path(world_root) / "var/warehouse/warehouse.duckdb",
                         table, t0_spec.get("columns"))
    t0_approval_rows = query_rows(world_root, probe["approval_sql"]) \
        if probe.get("approval_sql") else []
    phases = [("t0", current, t0_approval_rows, closed, [])]
    upto = int(probe.get("through_night") or len(replayed))
    if not 1 <= upto <= len(replayed):
        raise ValueError(f"through_night {upto} is outside the replay")
    for night in replayed[:upto]:
        snaps = night.get("snapshots") or {}
        if handoff_key not in snaps:
            raise ValueError("closed_window_boundary replay has no handoff snapshots")
        approvals = snaps.get(approval_key, {"rows": []})["rows"]
        closed_at_night = {str(x) for x in (probe.get("closed_days") or [])}
        closed_at_night.update(str(row[0]) for row in
                               snaps.get(closed_key, {"rows": []})["rows"])
        close_rows = snaps.get(close_rows_key, {"rows": []})["rows"]
        phases.append((f"night{night['night']}", snaps[handoff_key], approvals,
                       closed_at_night, close_rows))
    findings = []
    protected = {day: _rows_for_day(reference, day_column, day) for day in closed}
    previously_closed = set(closed)
    for phase, snapshot, approval_rows, closed_now, close_rows in phases:
        if snapshot["columns"] != reference["columns"]:
            findings.append(f"{phase}: protected handoff layout changed")
            continue
        approved = _approvals(approval_rows)
        ref_index = snapshot["columns"].index(ref_column)
        day_index = snapshot["columns"].index(day_column)
        # A day that first closes during a replay night becomes protected at
        # that night. `closed_rows_sql` can supply the close's own values,
        # catching a rewrite later in the SAME night; otherwise its end-night
        # handoff becomes the baseline for subsequent nights.
        for day in sorted(closed_now - previously_closed):
            baseline = _rows_for_day(snapshot, day_column, day)
            if close_rows and probe.get("handoff_compare_columns"):
                columns = list(probe["handoff_compare_columns"])
                indices = [snapshot["columns"].index(c) for c in columns]
                close_baseline = Counter(json.dumps(row, sort_keys=True)
                                         for row in close_rows if str(row[0]) == day)
                projected = Counter(json.dumps([row[i] for i in indices], sort_keys=True)
                                    for row in snapshot["rows"] if str(row[day_index]) == day)
                if projected != close_baseline and not approved.get(day):
                    findings.append(f"{phase} {day}: handoff differs from the close record "
                                    "without an approved correction")
            protected[day] = baseline
        previously_closed.update(closed_now)
        for day in sorted(protected):
            before = protected[day]
            after = _rows_for_day(snapshot, day_column, day)
            if after == before:
                continue
            tickets = approved.get(day) or set()
            if not tickets:
                findings.append(f"{phase} {day}: closed handoff changed without an approved correction")
                continue
            new_rows = (after - before).elements()
            for encoded in new_rows:
                row = json.loads(encoded)
                if str(row[day_index]) != day or str(row[ref_index]) not in tickets:
                    findings.append(f"{phase} {day}: changed handoff row lacks approved correction_ref")
                    break
    if not protected:
        raise ValueError("closed_window_boundary has no closed days to protect")
    return {"passed": not findings, "violations": findings[:20],
            "closed_days": len(protected), "phases_checked": len(phases)}
