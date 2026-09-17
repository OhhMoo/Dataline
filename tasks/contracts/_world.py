#!/usr/bin/env python3
"""
The execution machinery the behavioural contracts need, COPIED (not imported)
from tasks/GT004-doubling-window/tests/probes/_common.py.

Four rules, each of which is a failure this repo has already paid for:

  1. NEVER mutate the graded world.  Every contract that executes anything
     copies the world root and works on the copy.

  2. ALWAYS aliased ATTACH (F622).  See _types.Context.connect.

  3. NEVER hardcode a number.  Every table name, key, date and row count comes
     from `params` or from `before`.  A missing key is reported as
     "contract not evaluable" naming the key - never guessed.

  4. newline="" on every text file this package writes.  A CRLF that changes no
     row count has broken determinism in this repo four separate times.

And one routing decision inherited with the code: NOTHING here triggers a job
through the scheduler.  `FileScheduler.trigger` reports success WITHOUT
executing for every command that is not dbt_build/dbt_run
(gateway/src/alkera_gateway/surfaces.py), so a run count of two there proves
nothing ran at all.  The declared pipeline FILES are executed directly.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ._types import ContractNotEvaluable, WAREHOUSE_RELPATH

WORKSPACE_RELDIR = "workspace"
SCHED_RELPATH = Path("var") / "sched" / "state.json"


# ------------------------------------------------------------------ the world
def warehouse_path(world_root) -> Path:
    return Path(world_root) / WAREHOUSE_RELPATH


def workspace_path(world_root) -> Path:
    return Path(world_root) / WORKSPACE_RELDIR


def copy_world(world_root, dst) -> Path:
    """Copy a whole world root, warehouse and workspace together.

    The warehouse lands at var/warehouse/warehouse.duckdb inside the copy - the
    CONTRACT filename - so the agent's own two-part `warehouse.<table>`
    references run under exactly the collision the deployed world has.
    """
    dst = Path(dst)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(world_root, dst, ignore=shutil.ignore_patterns(".git", "*.wal"))
    # a stray WAL from an unclean shutdown would be replayed into the copy and
    # make the run non-deterministic
    Path(str(dst / WAREHOUSE_RELPATH) + ".wal").unlink(missing_ok=True)
    return dst


def open_world(db_path, *, read_only: bool = False):
    """Aliased ATTACH, read-write by default (planting needs it)."""
    import duckdb
    con = duckdb.connect(":memory:", config={"threads": 1})
    mode = " (READ_ONLY)" if read_only else ""
    con.execute(f"ATTACH '{Path(db_path).as_posix()}' AS wh{mode}")
    try:
        con.execute("USE wh.warehouse")
    except Exception:                                       # noqa: BLE001
        con.execute("USE wh")
    return con


def q(table: str) -> str:
    """`schema.table` -> a fully qualified, quoted THREE-part reference.

    Three-part on purpose: the alias is `wh`, but a probe that emitted
    `warehouse.fct_x` would break the moment someone attached the world under an
    alias called `warehouse`.
    """
    parts = str(table).split(".")
    if len(parts) == 1:
        parts = ["warehouse", parts[0]]
    if len(parts) != 2:
        raise ContractNotEvaluable(f"table must be '<schema>.<table>', got {table!r}")
    return 'wh."%s"."%s"' % (parts[0], parts[1])


def write_text(path, text: str) -> None:
    """Rule 4.  newline='' so nothing is translated on the way to disk."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return default


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------------- reading tables
def table_exists(con, table: str) -> bool:
    schema, name = table.split(".") if "." in table else ("warehouse", table)
    row = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?", [schema, name]).fetchone()
    return bool(row and row[0])


def business_columns(con, table: str, audit=None) -> list:
    """Every column except the audit columns.

    load_ts / batch_id / _loaded_at legitimately differ after any repair, so
    they are excluded from every fingerprint.  Excluding them is what lets
    "idempotence" mean "changed no BUSINESS column" rather than "wrote nothing".
    """
    schema, name = table.split(".") if "." in table else ("warehouse", table)
    cols = [c for (c,) in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
        [schema, name]).fetchall()]
    drop = {c.lower() for c in (audit or [])}
    return [c for c in cols if c.lower() not in drop]


def _row_fp(cols: list) -> str:
    inner = ", ".join('"%s"' % c for c in cols)
    return f"md5(({inner})::VARCHAR)"


def business_fingerprint(con, table: str, cols: list) -> str:
    """Order-independent hash of a table's business content."""
    sql = (f"SELECT COALESCE(md5(string_agg({_row_fp(cols)}, '' ORDER BY 1)), 'EMPTY') "
           f"FROM {q(table)}")
    return str(con.execute(sql).fetchone()[0])


def per_date(con, table: str, date_col: str, cols: list,
             start: str | None = None, end: str | None = None) -> dict:
    """{date -> [row_count, fingerprint]} - the shape every window check needs."""
    where = ""
    if start and end:
        where = (f'WHERE "{date_col}"::DATE BETWEEN DATE \'{start}\' '
                 f"AND DATE '{end}'")
    sql = (f'SELECT "{date_col}"::VARCHAR AS d, COUNT(*), '
           f"md5(string_agg({_row_fp(cols)}, '' ORDER BY 1)) "
           f"FROM {q(table)} {where} GROUP BY 1 ORDER BY 1")
    return {d: [int(n), str(h)] for d, n, h in con.execute(sql).fetchall()}


def key_count(con, table: str, predicate: str) -> int:
    """PHYSICAL rows matching a predicate - not distinct keys.

    Physical on purpose: a duplicated row must count twice, or a non-idempotent
    replay balances the very ledger it just broke (GT002's K5 lesson).
    """
    return int(con.execute(
        f"SELECT COUNT(*) FROM {q(table)} WHERE {predicate}").fetchone()[0])


def duplicate_keys(con, table: str, key_cols: list) -> tuple:
    """(duplicated key count, example keys).  GT004's G1b, generalised."""
    key = ", ".join(f'"{c}"' for c in key_cols)
    n = int(con.execute(
        f"SELECT COUNT(*) FROM (SELECT {key} FROM {q(table)} "
        f"GROUP BY {key} HAVING COUNT(*) > 1)").fetchone()[0])
    sample = con.execute(
        f"SELECT {key}, COUNT(*) FROM {q(table)} GROUP BY {key} "
        f"HAVING COUNT(*) > 1 ORDER BY 1 LIMIT 3").fetchall()
    return n, sample


# ------------------------------------------------------------------ executing
def split_sql(text: str) -> list:
    """Statement splitter that ignores ';' inside comments and quoted literals."""
    out, cur, i, n = [], [], 0, len(text)
    while i < n:
        c = text[i]
        if text[i:i + 2] == "--":
            j = text.find("\n", i)
            j = n if j < 0 else j + 1
            cur.append(text[i:j])
            i = j
            continue
        if text[i:i + 2] == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            cur.append(text[i:j])
            i = j
            continue
        if c in ("'", '"'):
            j = i + 1
            while j < n:
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            cur.append(text[i:j])
            i = j
            continue
        if c == ";":
            chunk = "".join(cur).strip()
            if _has_sql(chunk):
                out.append(chunk)
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    chunk = "".join(cur).strip()
    if _has_sql(chunk):
        out.append(chunk)
    return out


def _has_sql(s: str) -> bool:
    body, i, n = [], 0, len(s)
    while i < n:
        if s[i:i + 2] == "--":
            j = s.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if s[i:i + 2] == "/*":
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        body.append(s[i])
        i += 1
    return bool("".join(body).strip())


def _substitute(value: str, world_root) -> str:
    return (str(value)
            .replace("{world_root}", Path(world_root).as_posix())
            .replace("{workspace}", workspace_path(world_root).as_posix())
            .replace("{db}", warehouse_path(world_root).as_posix()))


def normalise_steps(steps) -> list:
    """A pipeline step is either a path string or {path, argv, optional}."""
    out = []
    for s in steps or []:
        if isinstance(s, str):
            out.append({"path": s, "argv": None, "optional": False})
        elif isinstance(s, dict) and s.get("path"):
            out.append({"path": s["path"], "argv": s.get("argv"),
                        "optional": bool(s.get("optional"))})
        else:
            raise ContractNotEvaluable(f"unreadable pipeline step: {s!r}")
    return out


def run_pipeline(world_root, steps: list, python: str | None = None) -> list:
    """Execute the declared pipeline files against a COPIED world.

    Failures are RECORDED, not raised: a pipeline that crashes is a finding, and
    the contract that called this is the thing entitled to decide what it is
    worth.
    """
    db = warehouse_path(world_root)
    log = []
    for step in steps:
        path = Path(world_root) / step["path"]
        if not path.exists():
            alt = workspace_path(world_root) / step["path"]
            path = alt if alt.exists() else path
        entry = {"step": step["path"], "ran": False, "error": None,
                 "statements": 0, "path": str(path)}
        if not path.exists():
            entry["error"] = "missing from the workspace"
            log.append(entry)
            continue
        try:
            if path.suffix.lower() == ".sql":
                stmts = split_sql(path.read_text(encoding="utf-8", errors="replace"))
                entry["statements"] = len(stmts)
                was = os.getcwd()
                con = open_world(db)
                try:
                    ws = workspace_path(world_root)
                    if ws.is_dir():
                        os.chdir(ws)
                    for k, st in enumerate(stmts, 1):
                        try:
                            con.execute(st)
                        except Exception as exc:            # noqa: BLE001
                            entry["error"] = (f"statement {k}/{len(stmts)}: "
                                              f"{str(exc).splitlines()[0][:180]}")
                            break
                finally:
                    con.close()
                    os.chdir(was)
            else:
                argv = [_substitute(a, world_root) for a in (step["argv"] or [])]
                r = subprocess.run([python or sys.executable, str(path), *argv],
                                   capture_output=True, text=True,
                                   cwd=str(workspace_path(world_root)), timeout=600)
                entry["returncode"] = r.returncode
                if r.returncode != 0:
                    tail = (r.stderr or r.stdout or "").strip().splitlines()
                    entry["error"] = tail[-1][:180] if tail else f"exit {r.returncode}"
            entry["ran"] = entry["error"] is None
        except Exception as exc:                            # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:180]}"
        log.append(entry)
    return log


def pipeline_failed(log: list) -> str | None:
    for e in log:
        if e["error"]:
            return f"{e['step']}: {e['error']}"
    return None


def steps_from(ctx) -> list:
    """The pipeline the contract is allowed to run, from params.pipeline.steps.

    A contract that has no pipeline cannot make a behavioural claim, so this
    raises rather than quietly grading the end state instead.
    """
    steps = normalise_steps(ctx.param("pipeline.steps") or ctx.param("steps"))
    if not steps:
        raise ContractNotEvaluable(
            "params.pipeline.steps is not set, so there is no pipeline to run and "
            "no behavioural claim can be made about this world")
    return steps


def plant(world_root, spec: dict) -> str:
    """Materialise a partition/delivery the pipeline has never seen.

    mode=sql      execute spec.sql once per row against the copied warehouse
    mode=landing  write a CSV into the landing zone the loader reads
    """
    mode = spec.get("mode", "sql")
    date = spec.get("date")
    rows = int(spec.get("rows", 0))
    id_base = int(spec.get("id_base", 990000001))
    if mode == "landing":
        for key in ("dir", "filename", "header", "row_template"):
            if not spec.get(key):
                raise ContractNotEvaluable(f"plant.{key} is required for mode=landing")
        target = (workspace_path(world_root) / spec["dir"].format(date=date)
                  / spec["filename"])
        lines = [spec["header"]]
        for i in range(rows):
            lines.append(spec["row_template"].format(
                n=i + 1, date=date, id=id_base + i, amount=spec.get("amount", "10.00")))
        write_text(target, "\n".join(lines) + "\n")
        return f"wrote {rows} row(s) to {target.relative_to(Path(world_root)).as_posix()}"
    if mode == "sql":
        if not spec.get("sql"):
            raise ContractNotEvaluable("plant.sql is required for mode=sql")
        con = open_world(warehouse_path(world_root))
        try:
            for i in range(rows):
                con.execute(spec["sql"].format(
                    n=i + 1, date=date, id=id_base + i,
                    amount=spec.get("amount", "10.00")))
        finally:
            con.close()
        return f"inserted {rows} row(s) through plant.sql"
    raise ContractNotEvaluable(f"unknown plant.mode {mode!r}")


def advance_clock(world_root, to: str | None) -> str:
    """Move the world's v-clock forward.  Nothing is triggered.

    The scheduler's v-now is the world's clock
    advancing it is what makes an
    arrival LATE rather than merely additional.
    """
    if not to:
        return "clock not advanced (no advance_to given)"
    p = Path(world_root) / SCHED_RELPATH
    state = read_json(p)
    if not isinstance(state, dict) or "vnow" not in state:
        return f"clock not advanced: {p.name} carries no vnow"
    was = state["vnow"]
    state["vnow"] = to
    write_text(p, json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n")
    return f"v-clock {was} -> {to}"
