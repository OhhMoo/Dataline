#!/usr/bin/env python3
"""
Shared machinery for the GT004 lifecycle probes.

Everything in this package obeys four rules, each of which is a failure this
repo has already paid for:

  1. NEVER mutate the graded world.  Every probe copies the world root and works
     on the copy.  `grade_twice` runs the grader twice on an unchanged world and
     compares - a probe that wrote to the world would make the score unstable and
     be reported as a grader defect.

  2. ALWAYS aliased ATTACH (F622).  `duckdb.connect(":memory:")`, then
     `ATTACH '<path>' AS wh`, then `USE wh.warehouse`.  The world deploys the
     warehouse at var/warehouse/warehouse.duckdb and the schema inside it is also
     called `warehouse`; opened directly, catalog == schema and every two-part
     reference in the agent's own SQL raises `Binder Error: Ambiguous reference
     to catalog or schema "warehouse"`.  READ_ONLY wherever we only read: a
     read-write handle takes duckdb's exclusive file lock and knocks a concurrent
     rollout over.

  3. NEVER hardcode a number.  Every row count, date, table name and dashboard id
     comes from `fixtures/fixture_contract.py::CONTRACT` (or from the grading
     manifest, which overlays it).  A missing key is reported as
     "probe not configured" with zero points and a precise message naming the
     key - never guessed at, and never silently defaulted into a check that
     grades something other than what it says it grades.

  4. `newline=""` on every text file this package writes.  A CRLF that changes no
     row count has broken determinism in this repo four separate times.

Two environment defects are routed around rather than fixed; both were measured
this session and neither is GT004's to repair:

  * `sched_trigger` reports success WITHOUT executing.  `FileScheduler.trigger`
    (gateway/src/alkera_gateway/surfaces.py:69-82) only reaches `commands.dbt`
    when the job's command is `dbt_build`/`dbt_run` or a selector is set;
    otherwise it returns `{"intent": command}` and marks the run success.  NO
    probe here triggers a job.  Every probe executes the agent's own pipeline
    FILES directly, the way GT002's K5 and GT003's D6 do.

  * `late_arrival` faults are inert - `_kind_scan` globs the cumulative folder
    with `min: 1`, so after day one a poll cannot fail.  `late_arrival.py` models
    late arrival through the arrival stream and the clock instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

# The deployed layout (agent.md 9.2.1).  Staging anywhere else would run the
# agent's SQL under a file stem that does not collide, which is how GT001 shipped
# TASK VALID with an oracle that aborted on its first statement.
WAREHOUSE_RELPATH = Path("var") / "warehouse" / "warehouse.duckdb"
SCHED_RELPATH = Path("var") / "sched" / "state.json"
DASHBOARDS_RELPATH = Path("var") / "bi" / "dashboards"
WORKSPACE_RELDIR = "workspace"


class ProbeNotConfigured(Exception):
    """A required contract key is absent.  Reported, never defaulted around."""


@dataclass
class Clause:
    """One assertion inside a probe.  `weight` is a share of the probe's max."""
    name: str
    ok: bool
    weight: float
    detail: str
    fatal: bool = False          # False here forces the WHOLE probe to zero


@dataclass
class ProbeResult:
    id: str
    points: float
    max: float
    reason: str
    detail: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.points >= self.max

    def as_check(self) -> dict:
        """The dict shape GT002/GT003 graders append to `Score.checks`."""
        return {"id": self.id, "points": round(float(self.points), 2),
                "max": self.max, "passed": self.passed, "detail": self.reason}


def score_clauses(pid: str, clauses: list[Clause], max_points: float,
                  extra: dict | None = None) -> ProbeResult:
    """Fold clauses into a result.

    A failed FATAL clause zeroes the probe outright.  That is the liveness rule:
    a load "fixed" into a permanent skip is idempotent by construction and
    repairs nothing, so it must score ZERO rather than collect the partial credit
    its other clauses would earn.
    """
    # A FATAL CLAUSE GATES; IT DOES NOT PAY. The shipped world already satisfies
    # some of these - the stock loader IS live, and an agent that does nothing
    # breaks nothing - so paying for them handed a no-op 5.2 of G2's 8 points
    # for behaviour it never produced. Measured before this change: no_op 6.25
    # of 33, of which 6.25 came entirely from mandatory clauses passing by
    # construction. Fatal clauses are now excluded from the weight base, so they
    # can only take the gate to zero, never add to it.
    gating = [c for c in clauses if c.fatal]
    scored = [c for c in clauses if not c.fatal]
    fatal_fail = [c for c in gating if not c.ok]
    if fatal_fail:
        points = 0.0
    elif not scored:
        # every clause is a gate: all of them passing IS the whole result
        points = max_points
    else:
        total_weight = sum(c.weight for c in scored) or 1.0
        points = max_points * sum(c.weight for c in scored if c.ok) / total_weight
    head = "; ".join(f"{'PASS' if c.ok else 'FAIL'} {c.name}: {c.detail}" for c in clauses)
    if fatal_fail:
        head = (f"ZERO - the mandatory clause '{fatal_fail[0].name}' failed, so no "
                f"partial credit is available. " + head)
    detail = {"clauses": [{"name": c.name, "ok": c.ok, "weight": c.weight,
                           "fatal": c.fatal, "detail": c.detail} for c in clauses]}
    detail.update(extra or {})
    return ProbeResult(pid, round(points, 2), max_points, head, detail)


def not_configured(pid: str, max_points: float, message: str) -> ProbeResult:
    return ProbeResult(pid, 0.0, max_points,
                       f"probe not configured: {message}", {"configured": False})


def crashed(pid: str, max_points: float, exc: BaseException) -> ProbeResult:
    return ProbeResult(pid, 0.0, max_points,
                       f"probe raised {type(exc).__name__}: "
                       f"{str(exc).splitlines()[0][:200]}", {"crashed": True})


# --------------------------------------------------------------- configuration
def probe_config(contract: dict | None, config: dict | None, key: str) -> dict:
    """`CONTRACT['probes'][key]` overlaid by the grading manifest's own block.

    The manifest wins so GRADER can retune a probe without a fixture rebuild;
    the contract is the source of truth for anything numeric.
    """
    base = ((contract or {}).get("probes") or {}).get(key) or {}
    over = config or {}
    merged = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


def require(cfg: dict, *keys: str, where: str = "") -> None:
    missing = [k for k in keys if cfg.get(k) in (None, "", [], {})]
    if missing:
        raise ProbeNotConfigured(
            f"missing key(s) {missing} in {where or 'the probe config'}")


def world_config(contract: dict | None, config: dict | None) -> dict:
    """The shared description of the graded fact table and the repaired window."""
    cfg = probe_config(contract, (config or {}).get("world"), "world")
    require(cfg, "fact_table", "business_key", "date_column", "window",
            where="probes.world")
    w = cfg["window"]
    require(w, "start", "end", where="probes.world.window")
    return cfg


# ------------------------------------------------------------------- the world
def copy_world(world_root: Path, dst: Path) -> Path:
    """Copy a whole world root, warehouse and workspace together.

    The warehouse lands at var/warehouse/warehouse.duckdb inside the copy - the
    CONTRACT filename - so the agent's own two-part `warehouse.<table>`
    references are executed under exactly the collision the deployed world has.
    """
    dst = Path(dst)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(world_root, dst)
    # A stray WAL from an unclean shutdown would be replayed into the copy and
    # make the probe non-deterministic.  duckdb writes it beside the database.
    wal = dst / WAREHOUSE_RELPATH
    Path(str(wal) + ".wal").unlink(missing_ok=True)
    return dst


def warehouse_path(world_root: Path) -> Path:
    return Path(world_root) / WAREHOUSE_RELPATH


def workspace_path(world_root: Path) -> Path:
    return Path(world_root) / WORKSPACE_RELDIR


def open_world(db_path: Path, *, read_only: bool = False,
               attach: dict | None = None):
    """The ONE way anything in this package opens a warehouse.  See rule 2."""
    con = duckdb.connect(":memory:", config={"threads": 1})
    mode = " (READ_ONLY)" if read_only else ""
    con.execute(f"ATTACH '{Path(db_path).as_posix()}' AS wh{mode}")
    con.execute("USE wh.warehouse")
    for alias, p in (attach or {}).items():
        con.execute(f"ATTACH '{Path(p).as_posix()}' AS {alias} (READ_ONLY)")
    return con


def q(table: str) -> str:
    """`schema.table` -> a fully qualified, quoted THREE-part reference.

    Three-part on purpose: the alias is `wh`, but a probe that emitted
    `warehouse.fct_x` would break the moment someone attached the world under an
    alias called `warehouse`.  Quoting survives a table name with a dot in it.
    """
    parts = str(table).split(".")
    if len(parts) == 1:
        parts = ["warehouse", parts[0]]
    if len(parts) != 2:
        raise ProbeNotConfigured(f"table must be '<schema>.<table>', got {table!r}")
    return 'wh."%s"."%s"' % (parts[0], parts[1])


def write_text(path: Path, text: str) -> None:
    """Rule 4.  newline='' so nothing is translated on the way to disk."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


# ------------------------------------------------------------------ executing
def split_sql(text: str) -> list[str]:
    """Statement splitter that ignores ';' inside comments and quoted literals.

    Same splitter GT003's D6 uses.  The agent's pipeline files are executed
    statement by statement so a failure names the statement that failed instead
    of the whole file.
    """
    out, cur, i, n = [], [], 0, len(text)
    while i < n:
        c = text[i]
        if text[i:i + 2] == "--":
            j = text.find("\n", i)
            j = n if j < 0 else j + 1
            cur.append(text[i:j]); i = j; continue
        if text[i:i + 2] == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            cur.append(text[i:j]); i = j; continue
        if c in ("'", '"'):
            j = i + 1
            while j < n:
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:
                        j += 2; continue
                    j += 1; break
                j += 1
            cur.append(text[i:j]); i = j; continue
        if c == ";":
            chunk = "".join(cur).strip()
            if _has_sql(chunk):
                out.append(chunk)
            cur = []; i += 1; continue
        cur.append(c); i += 1
    chunk = "".join(cur).strip()
    if _has_sql(chunk):
        out.append(chunk)
    return out


def _has_sql(s: str) -> bool:
    body, i, n = [], 0, len(s)
    while i < n:
        if s[i:i + 2] == "--":
            j = s.find("\n", i); i = n if j < 0 else j + 1; continue
        if s[i:i + 2] == "/*":
            j = s.find("*/", i + 2); i = n if j < 0 else j + 2; continue
        body.append(s[i]); i += 1
    return bool("".join(body).strip())


def _substitute(value: str, world_root: Path) -> str:
    return (str(value)
            .replace("{world_root}", Path(world_root).as_posix())
            .replace("{workspace}", workspace_path(world_root).as_posix())
            .replace("{db}", warehouse_path(world_root).as_posix()))


def normalise_steps(steps) -> list[dict]:
    """A pipeline step is either a path string or {path, argv, optional}."""
    out = []
    for s in steps or []:
        if isinstance(s, str):
            out.append({"path": s, "argv": None, "optional": False})
        elif isinstance(s, dict) and s.get("path"):
            out.append({"path": s["path"], "argv": s.get("argv"),
                        "optional": bool(s.get("optional"))})
        else:
            raise ProbeNotConfigured(f"unreadable pipeline step: {s!r}")
    return out


def run_pipeline(world_root: Path, steps: list[dict],
                 python: str | None = None) -> list[dict]:
    """Execute the agent's OWN pipeline files against a copied world.

    NOT through the scheduler.  `sched_trigger` reports success without executing
    for every command that is not dbt_build/dbt_run, so a probe built on it would
    grade a world nothing ran against.

    Each step gets its own connection, opened and closed around it, so a `.py`
    step is never fighting a `.sql` step for duckdb's exclusive file lock.
    Failures are RECORDED, not raised: a pipeline that crashes is a finding, and
    the probe that called this is the thing entitled to decide what it is worth.
    """
    db = warehouse_path(world_root)
    log = []
    for step in steps:
        path = Path(world_root) / step["path"]
        if not path.exists():
            # try workspace-relative too; manifests name either
            alt = workspace_path(world_root) / step["path"]
            path = alt if alt.exists() else path
        entry = {"step": step["path"], "ran": False, "error": None,
                 "statements": 0, "path": str(path)}
        if not path.exists():
            entry["error"] = "missing from the workspace"
            log.append(entry)
            if step["optional"]:
                continue
            continue
        try:
            if path.suffix.lower() == ".sql":
                sql = path.read_text(encoding="utf-8", errors="replace")
                stmts = split_sql(sql)
                entry["statements"] = len(stmts)
                # cwd = the workspace, so a relative path inside the agent's own
                # SQL resolves against the world it was written for.  (Through
                # the gateway such a statement could not read a file at all:
                # DuckDBWarehouse.connect sets enable_external_access = false.
                # Here it can, so the cwd has to be right or the probe would
                # grade a file-read that pointed at the grader's directory.)
                was = os.getcwd()
                con = open_world(db)
                try:
                    os.chdir(workspace_path(world_root))
                    for k, st in enumerate(stmts, 1):
                        try:
                            con.execute(st)
                        except Exception as exc:       # noqa: BLE001 - recorded
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
                                   cwd=str(workspace_path(world_root)),
                                   timeout=600)
                entry["returncode"] = r.returncode
                if r.returncode != 0:
                    tail = (r.stderr or r.stdout or "").strip().splitlines()
                    entry["error"] = tail[-1][:180] if tail else f"exit {r.returncode}"
            entry["ran"] = entry["error"] is None
        except Exception as exc:                       # noqa: BLE001 - recorded
            entry["error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:180]}"
        log.append(entry)
    return log


def pipeline_failed(log: list[dict]) -> str | None:
    for e in log:
        if e["error"]:
            return f"{e['step']}: {e['error']}"
    return None


# ------------------------------------------------------------- reading tables
def table_exists(con, table: str) -> bool:
    schema, name = (table.split(".") + ["", ""])[:2] if "." in table else ("warehouse", table)
    row = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?", [schema, name]).fetchone()
    return bool(row and row[0])


def business_columns(con, table: str, audit: list[str] | None = None) -> list[str]:
    """Every column except the audit columns.

    load_ts / batch_id / _loaded_at legitimately differ after any repair, so they
    are excluded from every fingerprint.  Excluding them is what lets
    "idempotence" mean "changed no BUSINESS column" rather than "wrote nothing".
    """
    schema, name = table.split(".") if "." in table else ("warehouse", table)
    cols = [c for (c,) in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
        [schema, name]).fetchall()]
    drop = {c.lower() for c in (audit or [])}
    return [c for c in cols if c.lower() not in drop]


def _row_fp(cols: list[str]) -> str:
    inner = ", ".join('"%s"' % c for c in cols)
    return f"md5(({inner})::VARCHAR)"


def business_fingerprint(con, table: str, cols: list[str]) -> str:
    """Order-independent hash of a table's business content."""
    sql = (f"SELECT COALESCE(md5(string_agg({_row_fp(cols)}, '' ORDER BY 1)), 'EMPTY') "
           f"FROM {q(table)}")
    return str(con.execute(sql).fetchone()[0])


def per_date(con, table: str, date_col: str, cols: list[str],
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


def key_count(con, table: str, key_cols: list[str], predicate: str) -> int:
    """PHYSICAL rows matching a predicate - not distinct keys.

    Physical on purpose: a duplicated row must count twice, or a non-idempotent
    replay balances the very ledger it just broke (GT002's K5 lesson)."""
    return int(con.execute(
        f"SELECT COUNT(*) FROM {q(table)} WHERE {predicate}").fetchone()[0])


def sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def tree_items(root: Path, exclude=(".git",)) -> dict:
    """{relpath -> sha256} for every file under root.

    `world.fingerprint` hashes whole trees to ONE digest, which answers only
    "did this section move".  Attribution needs per-file, so the collateral probe
    computes both and uses the tree hash for the headline and this for the blame.
    """
    root = Path(root)
    out = {}
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in exclude for part in rel.parts):
            continue
        if p.is_file() and not p.is_symlink():
            out[rel.as_posix()] = sha256_file(p)
    return out


def read_json(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:                                   # noqa: BLE001
        return default
