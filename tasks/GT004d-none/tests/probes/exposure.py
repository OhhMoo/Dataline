#!/usr/bin/env python3
"""
GT004 gate G4 - the downstream BI exposure still renders, and returns the
CORRECTED number.

    from probes import exposure
    result = exposure.run(world_root, contract=CONTRACT,
                          config=manifest.get("exposure_probe"), max_points=3)

A mart that is right while the dashboard is stale is a PARTIAL score - the data
equivalent of "shipped the code, forgot the migration".  So the two halves are
scored separately: whether the surface renders at all, and whether what it
renders is the repaired figure.

WHICH OF THE TWO ROUTES THIS BUILDS, and why
--------------------------------------------
The brief allowed either calling the gateway's `bi_*` surface or reading the
dashboard definition and recomputing.  This builds BOTH, preferring the first:

  * ROUTE A (preferred, `render_via = "gateway"`).  Imports
    `alkera_gateway.surfaces.render_dashboard` and `alkera_gateway.warehouse.
    DuckDBWarehouse` IN PROCESS and renders the dashboard exactly the way the
    `bi_render` tool does - the same definition parser (`load_dashboard`, which
    validates against the `Dashboard` pydantic model and so fails a definition
    the agent corrupted), the same `row_filter` wrapping, the same read-only
    execution against an aliased ATTACH.  Measured this session: the gateway
    package imports cleanly from the venv the harness runs graders in
    (gateway/.venv, tasks/harness/episode.py:24), so route A is reachable from
    the grading context without standing a server up.  No HTTP, no server
    process, no `sched_trigger`.

  * ROUTE B (fallback, `render_via = "local"`).  If `alkera_gateway` cannot be
    imported, the probe reads var/bi/dashboards/<id>.yml itself and executes each
    panel's SQL (wrapped in its `row_filter` the same way `BIStore.compute` does)
    against a read-only aliased ATTACH.  Strictly weaker: it does not enforce the
    Dashboard schema, so a definition the gateway would reject as invalid can
    still render here.  The result records which route ran.

Note what is deliberately NOT used: the CACHED tile.  `bi_state_dir` resolves to
`audit_dir/bi` and `GatewayConfig` REFUSES a bi_state_dir inside the world tree
(config.py:64-67), so the cached refresh is observation state that does not
survive into the graded world at all.  "Stale dashboard" in GT004 therefore means
a definition that no longer resolves, or one that resolves to the wrong figure -
not a stale cache file.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from ._common import (Clause, ProbeNotConfigured, copy_world, crashed,
                      not_configured, open_world, probe_config, require,
                      score_clauses, warehouse_path, DASHBOARDS_RELPATH)

PROBE_ID = "G4"


def _render_gateway(root: Path, audit: Path, dashboard: str) -> dict:
    """bi_render, in process.  Raises if the gateway is not importable."""
    from alkera_gateway.config import GatewayConfig
    from alkera_gateway.records import Observation
    from alkera_gateway.surfaces import render_dashboard
    from alkera_gateway.warehouse import DuckDBWarehouse

    cfg = GatewayConfig(
        workspace=root / "workspace",
        warehouse=warehouse_path(root),
        scheduler=root / "var" / "sched" / "state.json",
        dashboards=root / DASHBOARDS_RELPATH,
        audit_dir=audit,
        profiles_dir=root / "build",
        world_root=root,
    )
    return render_dashboard(cfg.dashboards, dashboard, DuckDBWarehouse(cfg),
                            Observation())


def _render_local(root: Path, dashboard: str) -> dict:
    """The fallback.  Same query shaping as BIStore.compute, no schema check."""
    import yaml
    path = Path(root) / DASHBOARDS_RELPATH / f"{dashboard}.yml"
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict) or not spec.get("panels"):
        raise ValueError(f"{path.name} defines no panels")
    con = open_world(warehouse_path(root), read_only=True)
    panels = {}
    try:
        for panel in spec["panels"]:
            sql = panel["sql"]
            if panel.get("row_filter"):
                sql = f"SELECT * FROM ({sql}) AS tile WHERE {panel['row_filter']}"
            cur = con.execute(sql)
            cols = [{"name": d[0], "type": str(d[1])} for d in (cur.description or [])]
            panels[panel["name"]] = {"columns": cols, "rows": cur.fetchall(),
                                     "truncated": False}
    finally:
        con.close()
    return {"dashboard": dashboard, "panels": panels}


def _cell(panel_result: dict, column: str, where: dict | None):
    """Pull one value out of a rendered panel, the way a reader would read it."""
    names = [c["name"] for c in panel_result.get("columns") or []]
    if column not in names:
        raise ValueError(f"panel has no column {column!r}; it has {names}")
    ci = names.index(column)
    rows = [list(r) for r in panel_result.get("rows") or []]
    for key, want in (where or {}).items():
        if key not in names:
            raise ValueError(f"panel has no column {key!r} to filter on")
        k = names.index(key)
        rows = [r for r in rows if str(r[k]) == str(want)]
    if not rows:
        raise ValueError("no row matches" + (f" {where}" if where else ""))
    if len(rows) > 1:
        raise ValueError(f"{len(rows)} rows match" + (f" {where}" if where else "")
                         + " - the expectation does not identify one tile value")
    return rows[0][ci]


def _scalar(v):
    """Unwrap the gateway's JSON envelope for non-JSON scalars.

    `records.json_value` renders a DECIMAL as {"type": "decimal", "value": "..."}
    and bytes as {"type": "bytes", "base64": ...}.  MEASURED: without this, a
    dashboard tile carrying SUM(amount) over a DECIMAL column compares as a dict
    against the contract's number and a CORRECT dashboard scores as wrong.  The
    selftest caught exactly that on its first run.
    """
    if isinstance(v, dict) and "type" in v and "value" in v:
        return v["value"]
    return v


def _same(got, want, tol) -> bool:
    got, want = _scalar(got), _scalar(want)
    if got is None or want is None:
        return got is want
    try:
        return abs(float(got) - float(want)) <= float(tol)
    except (TypeError, ValueError):
        return str(got) == str(want)


def run(world_root, *, contract: dict | None = None, config: dict | None = None,
        max_points: float = 3.0, keep_dir: Path | None = None) -> "object":
    """Gate G4.

    world_root  - post-episode world root; copied, never written to (the render
                  is read-only, but the copy keeps the guarantee uniform and
                  keeps duckdb's exclusive lock off the graded file).
    contract    - CONTRACT.
    config      - manifest `exposure_probe` block, overlaying
                  CONTRACT['probes']['exposure'].
    max_points  - the gate's rubric weight.
    keep_dir    - diagnostics only.

    Required config: `dashboard`, and `expect` - a list of
    {panel, column, value, [tolerance], [where], [stale_value]}.  `stale_value`
    is optional and purely diagnostic: when the rendered figure equals it, the
    reason says the dashboard is still showing the PRE-REPAIR number instead of
    "wrong number".
    """
    world_root = Path(world_root)
    try:
        cfg = probe_config(contract, config, "exposure")
        require(cfg, "dashboard", "expect", where="probes.exposure")
    except ProbeNotConfigured as exc:
        return not_configured(PROBE_ID, max_points, str(exc))

    tmp = Path(keep_dir) if keep_dir else Path(tempfile.mkdtemp(prefix="gt004-g4-"))
    try:
        root = copy_world(world_root, tmp / "world")
        dash = cfg["dashboard"]
        route, rendered, render_err = "gateway", None, None
        try:
            rendered = _render_gateway(root, tmp / "audit", dash)
        except ImportError:
            route = "local"
            try:
                rendered = _render_local(root, dash)
            except Exception as exc:                    # noqa: BLE001 - scored
                render_err = f"{type(exc).__name__}: {str(exc).splitlines()[0][:180]}"
        except Exception as exc:                        # noqa: BLE001 - scored
            render_err = f"{type(exc).__name__}: {str(exc).splitlines()[0][:180]}"

        if rendered is None:
            return score_clauses(PROBE_ID, [Clause(
                "renders", False, 1.0,
                f"dashboard {dash!r} does not render ({render_err}) - the "
                "downstream exposure is broken whatever the mart says",
                fatal=True)], max_points, {"render_via": route})

        panels = rendered.get("panels") or {}
        checks, bad = [], []
        for e in cfg["expect"]:
            pname = e.get("panel")
            if pname not in panels:
                bad.append(f"{pname}: panel missing from the rendered dashboard")
                checks.append(False)
                continue
            try:
                got = _cell(panels[pname], e["column"], e.get("where"))
            except Exception as exc:                    # noqa: BLE001 - scored
                bad.append(f"{pname}.{e.get('column')}: {exc}")
                checks.append(False)
                continue
            ok = _same(got, e["value"], e.get("tolerance", 0.01))
            checks.append(ok)
            if not ok:
                stale = e.get("stale_value")
                if stale is not None and _same(got, stale, e.get("tolerance", 0.01)):
                    bad.append(f"{pname}.{e['column']} = {_scalar(got)} - still the "
                               f"PRE-REPAIR figure; the mart may be right but this "
                               f"surface never moved (want {e['value']})")
                else:
                    bad.append(f"{pname}.{e['column']} = {_scalar(got)}, want {e['value']}")

        renders_ok = all(e.get("panel") in panels for e in cfg["expect"])
        correct_ok = bool(checks) and all(checks)

        # `bad` carries a message for FAILING tiles only, in tile order, so
        # walk it as an iterator rather than zipping it against every tile.
        _msgs = iter(bad)
        per_tile = ["" if ok else next(_msgs, "") for ok in checks]
        per = 0.65 / len(checks) if checks else 0.65
        tiles = [
            Clause(f"tile:{e.get('panel')}.{e['column']}", ok, per,
                   "shows the corrected figure" if ok else msg)
            for e, ok, msg in zip(cfg["expect"], checks, per_tile)
        ] if checks else [Clause("corrected-figure", False, 0.65,
                                 "no tile could be read")]

        return score_clauses(PROBE_ID, [
            Clause("renders", renders_ok, 0.35,
                   f"dashboard {dash!r} renders via the {route} route; "
                   f"{len(panels)} panel(s), every expected panel present"
                   if renders_ok else
                   f"dashboard {dash!r} renders but is missing an expected panel: "
                   f"{bad[:3]}", fatal=True),
            *tiles,
        ], max_points, {"render_via": route, "panels": sorted(panels),
                        "tiles_correct": sum(1 for c in checks if c),
                        "tiles_total": len(checks), "failures": bad[:4]})
    except Exception as exc:                            # noqa: BLE001 - scored
        return crashed(PROBE_ID, max_points, exc)
    finally:
        if keep_dir is None:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
