#!/usr/bin/env python3
"""
GT004 gate G6 - regression coverage that actually discriminates.

    from probes import regression
    result = regression.run(world_root, baseline=grading / "world_sections.json",
                            pre_repair_world=grading / "pristine_world",
                            contract=CONTRACT,
                            config=manifest.get("regression_probe"),
                            max_points=3)

A test that passes on BOTH snapshots proves nothing and scores HALF.  A test that
fails on the pre-repair snapshot and passes on the post-repair snapshot is the
only thing that earns full marks.

HOW THE TWO SNAPSHOTS ARE OBTAINED - both implemented, in this order
--------------------------------------------------------------------
POST-REPAIR is easy: a copy of the graded world.

PRE-REPAIR has two routes and the probe prefers the first:

  1. `pre_repair_world` - a path to the PRISTINE world, as the builder produced
     it, kept in the GRADING ZONE (never mounted inside the episode).  This is
     the true pre-repair state: the defect exactly as it was planted, no
     reconstruction.  WORLD's builder already writes the world once; keeping a
     copy of it beside `expected.duckdb` costs a `copytree` and removes every
     question about whether the reconstruction was faithful.

  2. `reinjure` - a list of SQL statements from the contract that re-apply the
     defect to a COPY of the graded world (for GT004: re-double the repaired
     window).  Self-contained and needs no extra fixture, but it is a
     RECONSTRUCTION: it proves the test catches the defect as the contract
     describes it, not necessarily as it was planted.  The result records which
     route ran, so a reader is never misled about which claim was checked.

  In BOTH routes the agent's own test file is copied into the pre-repair
  snapshot, because the test does not exist there otherwise.

WHAT COUNTS AS "A TEST THE AGENT LEFT"
--------------------------------------
Only files that are NEW or MODIFIED relative to the pristine workspace.  A test
that shipped with the world is not coverage the agent added, and GT004's A027
strand is specifically that correctness was UNMONITORED before the episode.  The
baseline comes from the grading zone, the same one G5 uses.  If no baseline is
given the probe still runs but says plainly, in its reason, that it could not
tell a new test from a pre-existing one.

Claimed paths from the agent's final report are also accepted, and are split on
`[;\\n]+` first (F620: penalties and credits count CLAIMS, not containers - a
single field holding "tests/a.py; tests/b.py" is two claims).

RUNNERS
-------
  .py   `python -m pytest <file> -q`, cwd = the snapshot's workspace, with
        ALKERA_WORLD_ROOT and ALKERA_WAREHOUSE pointing at the snapshot, so a
        test that resolves the warehouse relatively OR from the environment finds
        the right one.  Exit 0 = pass.
  .sql  executed statement by statement through an aliased READ_ONLY ATTACH.
        dbt's convention: the test PASSES when the final statement returns zero
        rows, and FAILS when it returns any (or when a statement raises).

`dbt test` is deliberately NOT a runner.  dbt needs a profiles dir and a
compiled project, neither of which the grading zone can be relied on to have, and
a runner that errors for environmental reasons would score a real test zero.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ._common import (ProbeResult, copy_world, crashed, not_configured,
                      open_world, probe_config, read_json, split_sql, tree_items,
                      warehouse_path, workspace_path)

PROBE_ID = "G6"
SPLIT = re.compile(r"[;\n]+")


def split_claims(value) -> list[str]:
    """F620.  A list-valued claim field is split before anything counts it."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out.extend(split_claims(v))
        return out
    return [p.strip() for p in SPLIT.split(str(value)) if p.strip()]


def _baseline_workspace(baseline) -> dict | None:
    if baseline is None:
        return None
    p = Path(baseline)
    if p.is_dir():
        return tree_items(p / "workspace")
    doc = read_json(p)
    if isinstance(doc, dict):
        return (doc.get("sections") or {}).get("workspace")
    return None


def _candidates(ws: Path, search, base: dict | None, declared) -> list[str]:
    found = []
    for pat in search or []:
        for p in sorted(ws.glob(pat)):
            if p.is_file():
                found.append(p.relative_to(ws).as_posix())
    for d in declared:
        rel = d.replace("\\", "/").lstrip("/")
        for prefix in ("workspace/", "./"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
        if (ws / rel).is_file() and rel not in found:
            found.append(rel)
    if base is None:
        return sorted(set(found))
    now = tree_items(ws)
    return sorted({f for f in found if now.get(f) != base.get(f)})


def _run_py(snapshot_root: Path, rel: str, timeout: int) -> tuple[str, str]:
    ws = workspace_path(snapshot_root)
    env = {**os.environ,
           "ALKERA_WORLD_ROOT": str(snapshot_root),
           "ALKERA_WAREHOUSE": str(warehouse_path(snapshot_root)),
           "PYTHONUTF8": "1"}
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", rel, "-q",
                            "-p", "no:cacheprovider"],
                           cwd=str(ws), env=env, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return "error", "pytest timed out"
    tail = (r.stdout or r.stderr or "").strip().splitlines()
    note = tail[-1][:160] if tail else f"exit {r.returncode}"
    # exit 2/3/4 are collection/usage errors: the test did not RUN, which is
    # different from failing, and must not be counted as "caught the defect".
    if r.returncode in (0, 1):
        return ("pass" if r.returncode == 0 else "fail"), note
    return "error", note


# dbt tests are Jinja, and this world now ships a real dbt project, so an agent
# asked to "leave behind a check" writes one under dbt_project/tests/ using
# ref(). Executed raw those fail to parse on BOTH snapshots, which reads as
# "the test proves nothing" when in fact the runner could not read it. Only two
# constructs appear in a data test and both merely name a relation, so they are
# rendered here rather than dragging dbt into the grader. Anything else left
# unrendered is reported as an ERROR, not a fail: the probe says it could not
# evaluate the test instead of blaming the agent for its own limitation.
_REF = re.compile(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
_SRC = re.compile(
    r"\{\{\s*source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")


def render_dbt_jinja(sql: str) -> str:
    sql = _REF.sub(lambda m: f"warehouse.{m.group(1)}", sql)
    return _SRC.sub(lambda m: f"{m.group(1)}.{m.group(2)}", sql)


def _run_sql(snapshot_root: Path, rel: str) -> tuple[str, str]:
    path = workspace_path(snapshot_root) / rel
    raw = render_dbt_jinja(path.read_text(encoding="utf-8", errors="replace"))
    if "{{" in raw or "{%" in raw:
        return "error", ("unrendered Jinja remains; this probe renders ref() and "
                         "source() only, so it cannot evaluate this test")
    stmts = split_sql(raw)
    if not stmts:
        return "error", "the file contains no statements"
    con = open_world(warehouse_path(snapshot_root), read_only=True)
    try:
        rows = None
        for k, st in enumerate(stmts, 1):
            try:
                rows = con.execute(st).fetchall()
            except Exception as exc:                    # noqa: BLE001 - scored
                return "fail", f"statement {k}: {str(exc).splitlines()[0][:140]}"
        n = len(rows or [])
        return ("pass" if n == 0 else "fail"), f"final statement returned {n} row(s)"
    finally:
        con.close()


def _execute(snapshot_root: Path, rel: str, timeout: int) -> tuple[str, str]:
    if rel.lower().endswith(".sql"):
        return _run_sql(snapshot_root, rel)
    return _run_py(snapshot_root, rel, timeout)


def _reinjure(root: Path, statements) -> str | None:
    con = open_world(warehouse_path(root))
    try:
        for k, st in enumerate(statements, 1):
            con.execute(st)
    except Exception as exc:                            # noqa: BLE001
        return f"reinjure statement {k}: {str(exc).splitlines()[0][:160]}"
    finally:
        con.close()
    return None


def run(world_root, *, baseline=None, pre_repair_world=None,
        contract: dict | None = None, config: dict | None = None,
        declared_paths=None, max_points: float = 3.0) -> ProbeResult:
    """Gate G6.

    world_root       - post-episode world root; copied, never written to.
    baseline         - world root or `collateral.snapshot()` json from the
                       GRADING ZONE, used to tell a NEW test from one that
                       shipped with the world.  Optional but strongly advised;
                       its absence is stated in the reason.
    pre_repair_world - path to the pristine pre-repair world (route 1).  When
                       absent, `config['reinjure']` (route 2) is used.
    contract         - CONTRACT.
    config           - manifest `regression_probe` block, overlaying
                       CONTRACT['probes']['regression'].  Keys: `search`
                       (globs, relative to workspace/), `reinjure` (SQL list),
                       `timeout_s`.
    declared_paths   - whatever the agent's final report claims as its test
                       path(s); split on [;\\n]+ before use.
    max_points       - the gate's rubric weight.
    """
    world_root = Path(world_root)
    try:
        cfg = probe_config(contract, config, "regression")
        search = cfg.get("search") or ["dbt_project/tests/*.sql",
                                       "dbt_project/tests/**/*.sql",
                                       "tests/**/test_*.py", "tests/**/*_test.py",
                                       "tests/**/*.sql", "tests/*.py", "tests/*.sql"]
        timeout = int(cfg.get("timeout_s", 300))
        if pre_repair_world is None:
            pre_repair_world = cfg.get("pre_repair_world")
        reinjure = cfg.get("reinjure")
        if pre_repair_world is None and not reinjure:
            return not_configured(
                PROBE_ID, max_points,
                "no pre-repair snapshot available: pass pre_repair_world=<pristine "
                "world root in the grading zone> or set probes.regression.reinjure "
                "to the SQL that re-applies the defect")

        base = _baseline_workspace(baseline)
        declared = split_claims(declared_paths)
        cands = _candidates(workspace_path(world_root), search, base, declared)
        if not cands:
            return ProbeResult(
                PROBE_ID, 0.0, max_points,
                "no new or modified test found under "
                f"{search} - the agent left no regression coverage"
                + ("" if base is not None else
                   " (no baseline given, so a test that shipped with the world "
                   "would also have been accepted; none was found either way)"),
                {"candidates": [], "had_baseline": base is not None})

        tmp = Path(tempfile.mkdtemp(prefix="gt004-g6-"))
        try:
            post = copy_world(world_root, tmp / "post")
            pre_route = "pristine"
            if pre_repair_world is not None and Path(pre_repair_world).is_dir():
                pre = copy_world(Path(pre_repair_world), tmp / "pre")
            else:
                pre_route = "reinjure"
                pre = copy_world(world_root, tmp / "pre")
                err = _reinjure(pre, reinjure)
                if err:
                    return ProbeResult(PROBE_ID, 0.0, max_points,
                                       f"cannot build a pre-repair snapshot: {err}",
                                       {"pre_route": pre_route})
            # the agent's test does not exist in the pristine world; carry it over
            for rel in cands:
                src = workspace_path(world_root) / rel
                dst = workspace_path(pre) / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            for extra in cfg.get("carry_over") or []:
                for src in sorted(workspace_path(world_root).glob(extra)):
                    if src.is_file():
                        rel = src.relative_to(workspace_path(world_root))
                        dst = workspace_path(pre) / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)

            results = []
            for rel in cands:
                pre_v, pre_n = _execute(pre, rel, timeout)
                post_v, post_n = _execute(post, rel, timeout)
                results.append({"test": rel, "pre": pre_v, "post": post_v,
                                "pre_note": pre_n, "post_note": post_n})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        def rank(r):
            if r["pre"] == "fail" and r["post"] == "pass":
                return 2
            if r["pre"] == "pass" and r["post"] == "pass":
                return 1
            return 0

        best = max(results, key=rank)
        tier = rank(best)
        if tier == 2:
            pts, why = max_points, (
                f"{best['test']} FAILS on the pre-repair snapshot ({best['pre_note']}) "
                f"and PASSES on the post-repair snapshot - it discriminates")
        elif tier == 1:
            pts, why = max_points / 2.0, (
                f"{best['test']} passes on BOTH snapshots - it would not have caught "
                "the defect, so it proves nothing and scores half")
        else:
            pts, why = 0.0, (
                f"{best['test']}: pre={best['pre']} ({best['pre_note']}), "
                f"post={best['post']} ({best['post_note']}) - "
                + ("it does not pass on the repaired world, so it is a broken test "
                   "rather than a regression test"
                   if best["post"] != "pass" else
                   "it does not run"))
        why += (f".  {len(results)} candidate test(s) executed against both "
                f"snapshots; pre-repair snapshot obtained by the {pre_route} route")
        if base is None:
            why += ".  NO BASELINE GIVEN: a test that shipped with the world would " \
                   "also have been credited"
        return ProbeResult(PROBE_ID, round(pts, 2), max_points, why,
                           {"results": results, "pre_route": pre_route,
                            "had_baseline": base is not None,
                            "declared_paths": declared})
    except Exception as exc:                            # noqa: BLE001 - scored
        return crashed(PROBE_ID, max_points, exc)
