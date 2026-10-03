"""Job execution: the scheduler's status is *derived*, never asserted.

Every run record here describes work that was actually attempted against the
real world - the real dbt project, the real DuckDB file, the real loader
script in the workspace. `status` comes from what the engine returned. A job
that declares no executable work is reported `unsupported` and FAILS; it is
never reported as success (agent.md 9.1 rule 7: loud unsupported is fine,
silent wrong is disqualifying).

Determinism rules (agent.md 7.4), because everything written here lands in
var/sched/state.json, which is inside the semantic fingerprint:

- No wall clock. Virtual timestamps are supplied by the scheduler; this module
  never reads the time.
- Engine output is NORMALIZED before it reaches the run record. dbt prints
  exactly four host-varying things - ANSI colour, a wall-clock HH:MM:SS stamp
  per line, an elapsed `in 0.16s`, and host-shaped paths (`models\\warehouse\\x`
  on Windows, `models/warehouse/x` elsewhere) - and `normalize_output` removes
  all four. Measured, not assumed: see docs/implementation_plan/structure/rounds/R003/LIVE-WORLD.md for the verbatim
  captured output this was written against.
- The raw, un-normalized engine output is kept at build/sched/logs/, which is
  outside both the fingerprint (fingerprint.py hashes var/ + workspace/) and
  the snapshot scope (snapshot.SCOPE = workspace, var). Nothing volatile is
  ever fingerprinted.
- dbt runs as a subprocess with threads: 1 and its target/log paths under
  build/, for the same reason build.py does: an in-process dbtRunner keeps the
  duckdb file locked after it returns.
"""
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import duckdb
import yaml

from .hashing import json_canon

DB = Path("var/warehouse/warehouse.duckdb")
RUN_DIR = Path("build") / "sched"          # never fingerprinted, never snapshotted

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LEAD_STAMP = re.compile(r"^\d{2}:\d{2}:\d{2}\s{0,2}")
ELAPSED = re.compile(r"\bin \d+(?:\.\d+)?s\b")
CLOCK = re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b")
MAX_LINES, MAX_CHARS = 40, 4000

# Call-argument tokens a theme `exec` block may name, resolved from the context.
ARG_TOKENS = ("root", "workspace", "db", "seed")


class Unsupported(Exception):
    """Raised when a job declares work this executor cannot run."""


def normalize_output(text: str, root: Path) -> str:
    """Engine output -> a deterministic, bounded string safe to fingerprint."""
    absolute = str(Path(root).resolve())
    lines = []
    for raw in text.splitlines():
        line = ANSI.sub("", raw)
        line = LEAD_STAMP.sub("", line)
        line = line.replace(absolute, "<world>")
        line = line.replace(absolute.replace("\\", "/"), "<world>")
        line = line.replace("\\", "/")
        line = ELAPSED.sub("in <elapsed>", line)
        line = CLOCK.sub("<time>", line)
        line = line.rstrip()
        if line.strip():
            lines.append(line)
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES] + [f"... ({len(lines) - MAX_LINES} more lines)"]
    out = "\n".join(lines)
    return out if len(out) <= MAX_CHARS else out[:MAX_CHARS] + "\n... (truncated)"


def dbt_profile_name(workspace: Path) -> str:
    project = Path(workspace) / "dbt_project" / "dbt_project.yml"
    return yaml.safe_load(project.read_text(encoding="utf-8"))["profile"]


def extra_warehouses(root: Path) -> list[Path]:
    """Every DuckDB file beside the primary, sorted. The one rule three modules
    share: fingerprint.py hashes each as a `warehouses` section, build.py seeds
    one at `var/warehouse/<name>.duckdb`, and write_profiles below renders a
    dbt output per file named by its stem - so a warehouse's NAME is its dbt
    target is its file, and none of the three needs a config the others could
    drift from. A world with one warehouse finds nothing here."""
    return sorted(p for p in (Path(root) / DB.parent).glob("*.duckdb")
                  if p.name != DB.name)


def write_profiles(root: Path, out_dir: Path, profile: str | None = None) -> Path:
    """profiles.yml with an absolute warehouse path, written OUTSIDE workspace/.

    The profile carries a root-dependent path, so it must not live in the
    fingerprinted tree; build/ is the only writable place that is in neither
    the fingerprint nor the snapshot.

    `target` stays `dev`, the primary, so every dbt invocation that names no
    target builds what it always built. An additional DuckDB warehouse beside
    the primary (extra_warehouses) gets an output of its own, named by its
    file stem, which is how `dbt run --target legacy` - from the scheduler's
    `dbt` job kind or from the gateway's `dbt_run(warehouse="legacy")` -
    reaches the second warehouse. A world with one file writes the byte-
    identical profiles.yml it wrote before the second output existed.
    """
    root, out_dir = Path(root), Path(out_dir)
    profile = profile or dbt_profile_name(root / "workspace")
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {"dev": {"type": "duckdb", "path": str(root / DB), "threads": 1}}
    for extra in extra_warehouses(root):
        outputs[extra.stem] = {"type": "duckdb", "path": str(extra), "threads": 1}
    (out_dir / "profiles.yml").write_text(yaml.safe_dump({profile: {
        "target": "dev",
        "outputs": outputs}}),
        encoding="utf-8")
    return out_dir


def dbt_invoke(root: Path, command: str, *, selector: str | None = None,
               variables: dict | None = None, full_refresh: bool = False,
               out_dir: Path | None = None, profile: str | None = None,
               quiet: bool = True, target: str | None = None) -> tuple[int, str]:
    """Run one dbt command against the world. Returns (returncode, output).

    Single implementation, shared by build.py's initial build and by the
    scheduler's `dbt` job kind, so the build and the episode run dbt the
    same way.

    `target` names a profiles.yml output other than the primary's (the stem
    of a second DuckDB file, see write_profiles); None runs what always ran.
    """
    root = Path(root)
    out_dir = write_profiles(root, Path(out_dir or root / RUN_DIR), profile)
    argv = [sys.executable, "-c", "from dbt.cli.main import cli; cli()", command]
    if quiet:
        argv.append("--quiet")
    argv += ["--project-dir", str(root / "workspace" / "dbt_project"),
             "--profiles-dir", str(out_dir),
             "--target-path", str(out_dir / "target"),
             "--log-path", str(out_dir / "logs")]
    if target:
        argv += ["--target", target]
    if selector and selector != "*":
        argv += ["--select", selector]
    if full_refresh:
        argv.append("--full-refresh")
    if variables:
        argv += ["--vars", json_canon(variables).decode()]
    # stdin=DEVNULL, never inherited. When this runs inside the episode's MCP
    # stdio server the parent's stdin IS the JSON-RPC input pipe, and the
    # server's reader thread holds a pending synchronous ReadFile on it. On
    # Windows the child's own lseek(fd 0) during interpreter startup queues
    # behind that read, so the child cannot finish starting until the client
    # next writes - measured at ~1810s per call, 7 calls across 5 GT007
    # rollouts, 3h31m of dead wall clock. The gateway has carried exactly this
    # guard for the same reason since it was written (commands.py:18-24); this
    # module never needed it until scheduling.py put it on the in-episode path.
    r = subprocess.run(argv, env={**os.environ, "PYTHONUTF8": "1"},
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _connect(root: Path, read_only: bool):
    """ATTACH under an alias - never open warehouse.duckdb directly (9.2.1)."""
    con = duckdb.connect(":memory:", config={"threads": 1})
    mode = " (READ_ONLY)" if read_only else ""
    con.execute("ATTACH '" + str(Path(root) / DB).replace("'", "''")
                + f"' AS wh{mode}")
    con.execute("USE wh")
    return con


def unreadable_views(root: Path) -> dict:
    """Every dbt-created view, read through the contract alias. {} means clean.

    This exists because a view is the one object whose *body* is stored, so it
    can be created successfully and still be unreadable later. dbt opens
    var/warehouse/warehouse.duckdb directly, where DuckDB names the catalog
    after the file stem, so anything dbt renders three-part bakes in the name
    `warehouse`; every reader is required to ATTACH the same file under an
    alias instead (agent.md 9.2.1), and under that alias the baked name does
    not resolve:

        Binder Error: Catalog "warehouse" does not exist!

    The themes' `macros/relations.sql` suppresses the database qualifier on
    ref() and source() so bodies bind against whatever catalog they find
    themselves in. This function is the gate on that: it reads EVERY view
    duckdb_views() reports, not the one someone happened to look at. A
    hand-written view (GT001's fixture) passes trivially and proves nothing,
    which is exactly why the defect survived until a theme world was queried.
    """
    con = _connect(root, read_only=True)
    broken = {}
    try:
        views = con.execute(
            "select schema_name, view_name from duckdb_views() "
            "where not internal order by 1, 2").fetchall()
        for schema, view in views:
            try:
                con.execute(f'select * from wh."{schema}"."{view}" limit 1').fetchall()
            except duckdb.Error as exc:
                broken[f"{schema}.{view}"] = str(exc).splitlines()[0]
    finally:
        con.close()
    return broken


def table_rows(root: Path, tables) -> dict:
    """Row counts for declared tables; a missing table reports None, not 0.

    Only a MISSING TABLE reports None. An engine that would not answer - a file
    lock, an IO error - raises, because this result lands in the run record's
    `effects`, which is inside var/sched/state.json, which is fingerprinted.
    Turning a transient into a recorded `null` there makes the world rebuild to
    a different hash depending on how loaded the machine was. Same reasoning as
    observe.MISSING_OBJECT, and the same conclusion: loud, not quiet.
    """
    if not tables:
        return {}
    counts, con = {}, _connect(root, read_only=True)
    try:
        for name in tables:
            try:
                counts[name] = con.execute(
                    f"select count(*) from {name}").fetchone()[0]
            except (duckdb.CatalogException, duckdb.BinderException):
                counts[name] = None
    finally:
        con.close()
    return counts


# --------------------------------------------------------------------------
# job kinds. Each returns (ok, detail, error, extra_effects).
# --------------------------------------------------------------------------

def _resolve_args(names, ctx):
    out = []
    for name in names:
        if name not in ARG_TOKENS:
            raise Unsupported(f"unknown arg token {name!r} "
                              f"(known: {', '.join(ARG_TOKENS)})")
        out.append(ctx[name])
    return out


def _kind_dbt(root, spec, ctx):
    command = spec.get("command", "build")
    if command not in ("build", "run", "test", "compile", "seed", "snapshot"):
        raise Unsupported(f"dbt command {command!r} is not allowed")
    selector = spec.get("selector")
    # `target`: the second warehouse's dbt output (its file stem, see
    # write_profiles). A job that names one the world does not have fails
    # in dbt's own words ("The profile ... does not have a target named"),
    # recorded as the run's error rather than asserted here.
    target = spec.get("target")
    rc, output = dbt_invoke(root, command, selector=selector,
                            variables=ctx.get("variables") or None,
                            full_refresh=bool(spec.get("full_refresh")),
                            target=target)
    ctx["raw_output"] = output
    detail = (f"dbt {command}" + (f" --target {target}" if target else "")
              + (f" --select {selector}" if selector else ""))
    error = None if rc == 0 else normalize_output(output, root)
    return rc == 0, detail, error, {"returncode": rc}


def _kind_sql(root, spec, ctx):
    statements = spec.get("statements")
    if spec.get("script"):
        path = (ctx["workspace"] / spec["script"]).resolve()
        if not path.is_relative_to(ctx["workspace"]):
            raise Unsupported("sql script must live inside workspace/")
        if not path.is_file():
            return False, f"sql {spec['script']}", (
                f"script not found: {spec['script']}"), {}
        statements = [path.read_text(encoding="utf-8")]
    if not statements:
        raise Unsupported("sql job declares neither `script` nor `statements`")
    con = _connect(root, read_only=False)
    try:
        for statement in statements:
            con.execute(statement)
    except duckdb.Error as exc:
        return False, spec.get("detail", "sql"), normalize_output(str(exc), root), {}
    finally:
        con.close()
    return True, spec.get("detail") or f"sql ({len(statements)} statement(s))", None, {}


def _kind_script(root, spec, ctx):
    path = (ctx["workspace"] / spec["path"]).resolve()
    if not path.is_relative_to(ctx["workspace"]):
        raise Unsupported("script must live inside workspace/")
    if not path.is_file():
        return False, f"script {spec['path']}", f"script not found: {spec['path']}", {}
    argv = [sys.executable, str(path)]
    for arg in spec.get("args", []):
        argv.append(str(arg).format(root=root, workspace=ctx["workspace"],
                                    db=root / DB))
    # stdin=DEVNULL, never inherited. When this runs inside the episode's MCP
    # stdio server the parent's stdin IS the JSON-RPC input pipe, and the
    # server's reader thread holds a pending synchronous ReadFile on it. On
    # Windows the child's own lseek(fd 0) during interpreter startup queues
    # behind that read, so the child cannot finish starting until the client
    # next writes - measured at ~1810s per call, 7 calls across 5 GT007
    # rollouts, 3h31m of dead wall clock. The gateway has carried exactly this
    # guard for the same reason since it was written (commands.py:18-24); this
    # module never needed it until scheduling.py put it on the in-episode path.
    r = subprocess.run(argv, env={**os.environ, "PYTHONUTF8": "1"},
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    output = (r.stdout or "") + (r.stderr or "")
    ctx["raw_output"] = output
    error = None if r.returncode == 0 else normalize_output(output, root)
    return r.returncode == 0, f"script {spec['path']}", error, {"returncode": r.returncode}


def _kind_theme(root, spec, ctx):
    gen = ctx.get("gen")
    if gen is None:
        raise Unsupported("theme job on a world with no live-world control "
                          "plane (var/world/live.json)")
    op = getattr(gen, spec["op"], None)
    if op is None:
        raise Unsupported(f"theme {ctx['theme']!r} has no gen hook {spec['op']!r}")
    args = _resolve_args(spec.get("args", []), ctx)
    try:
        returned = op(*args)
    # SystemExit as well as Exception: three of the four themes' load_raw hooks
    # raise SystemExit when their loader subprocess fails, and SystemExit is a
    # BaseException, so catching Exception alone let a failing loader escape and
    # kill `advance` instead of failing the job. Harmless while nothing made
    # loaders fail; the fault layer makes them fail routinely.
    except (Exception, SystemExit) as exc:              # the theme's own failure
        return False, f"theme:{spec['op']}", normalize_output(
            f"{type(exc).__name__}: {exc}", root), {}
    extra = returned if isinstance(returned, dict) else {}
    return True, f"theme:{spec['op']}", None, extra


def day_tokens(day) -> dict:
    """The spellings of the execution day a theme may name in a scan spec.

    A delivery folder is laid out in whatever date dialect the upstream system
    speaks, and the two that ship differ: sftp_vendor writes
    `data/drop/<vendor>/ORDERS_20260305.csv` (+ `.done`) and mainframe_batch
    writes `data/landing/claims_extract/dt=2026-03-05/CLAIMS.DAT`. So a scan
    needs BOTH spellings, and a theme author gets them as named tokens rather
    than as a format spec: `{day}` and `{day_compact}`, never `{day:%Y%m%d}`.

    `day` is the job's execution day - the day the delivery it is polling for
    belongs to - not the wall date the job happens to run on. The two differ by
    `day_offset` in every theme that polls the morning after.
    """
    if day in (None, ""):
        return {}
    d = date.fromisoformat(str(day)[:10])
    return {"day": d.isoformat(), "day_compact": d.strftime("%Y%m%d")}


def _template(text: str, tokens: dict, what: str) -> str:
    """Resolve `{day}`-style tokens, LOUDLY. Same idea as `detail` and `args`.

    An unresolvable token raises `Unsupported`, which the run record reports as
    a failure. It must never fall through to the literal string: a scan for
    `*/*_{day_compact}.done` that silently matched nothing would look exactly
    like a vendor who did not deliver, and the scheduler would be asserting an
    incident it did not observe.
    """
    try:
        return str(text).format(**tokens)
    except KeyError as exc:
        raise Unsupported(
            f"scan {what} {text!r} names {exc.args[0]!r}, which this run does "
            f"not know (have: {', '.join(sorted(tokens)) or 'nothing'}). A "
            f"scheduled run carries its execution day; a manual trigger takes "
            f"it from `execution_date`.") from exc
    except (IndexError, ValueError) as exc:
        raise Unsupported(f"scan {what} {text!r} is not a valid template: "
                          f"{type(exc).__name__}: {exc}") from exc


def _kind_scan(root, spec, ctx):
    """Poll a delivery folder: real files, real count, real failure.

    `path` and `pattern` are templated with `day_tokens`, which is what makes
    the poll able to fail at all. Globbing the delivery tree UNSCOPED was
    cumulative - every day ever delivered matched - so with any `min` at or
    below the number of days already landed the count could not fall below the
    minimum and the scan could not fail after day one. Two themes shipped
    `late_arrival` enabled against that, so a withheld delivery cost nothing
    the scheduler could see. Scoping the pattern to the execution day's own
    delivery is what makes "the vendor did not deliver tonight" observable.
    """
    tokens = day_tokens(ctx.get("day"))
    rel = _template(spec["path"], tokens, "path")
    base = (ctx["workspace"] / rel).resolve()
    if not base.is_relative_to(ctx["workspace"]):
        raise Unsupported("scan path must live inside workspace/")
    pattern = _template(spec.get("pattern", "**/*"), tokens, "pattern")
    if not base.is_dir():
        return False, f"scan {rel}", (
            f"delivery folder missing: {rel}"), {"files": 0}
    files = sorted(p for p in base.glob(pattern) if p.is_file())
    minimum = int(spec.get("min", 1))
    detail = f"scan {rel}: {len(files)} file(s)"
    if len(files) < minimum:
        return False, detail, (f"{rel} holds {len(files)} file(s) "
                               f"matching {pattern}, expected at least "
                               f"{minimum}"), {"files": len(files)}
    return True, detail, None, {"files": len(files)}


KINDS = {"dbt": _kind_dbt, "sql": _kind_sql, "script": _kind_script,
         "theme": _kind_theme, "scan": _kind_scan}


def execute(root: Path, job: str, spec: dict | None, *, gen=None,
            theme: str | None = None, seed: dict | None = None,
            variables: dict | None = None, day: str | None = None,
            log_name: str | None = None) -> dict:
    """Run one job's declared work. Returns a result, never raises on failure.

    {"ok", "detail", "error", "error_kind", "effects"}

    `spec` is the job's `exec` block. When it is absent or names a kind this
    executor does not implement, the result is ok=False with
    error_kind="unsupported" - the run record then says, in the agent's own
    log, that the scheduler could not run the job. It never says success.

    `day` is the execution day, and it is a separate channel from `variables`
    ON PURPOSE. `variables` is the agent's: `sched_trigger` forwards it
    verbatim to dbt as `--vars`, and that contract is frozen. Folding the
    scheduler's own execution day into it would silently start passing
    `--vars {"day": ...}` to every scheduled dbt build - a change to what the
    engine is invoked with, to buy a token only `scan` reads. `variables["day"]`
    is still honoured when the caller sets it, so an agent can aim a manual
    poll at a specific logical date.
    """
    root = Path(root)
    # Resolved, because the containment checks compare it against resolved
    # script paths: a workspace reached through a symlink (the Harbor image
    # links /world/workspace -> /workspace) must still contain its own files.
    ctx = {"root": root, "workspace": (root / "workspace").resolve(), "db": root / DB,
           "seed": seed, "gen": gen, "theme": theme,
           "variables": variables or {},
           "day": day if day is not None else (variables or {}).get("day"),
           "raw_output": None}
    if not spec:
        return {"ok": False, "detail": None, "effects": {},
                "error_kind": "unsupported",
                "error": f"job {job!r} declares no executable work; the world "
                         f"scheduler will not report a status it did not observe"}
    kind = spec.get("kind")
    if kind not in KINDS:
        return {"ok": False, "detail": None, "effects": {},
                "error_kind": "unsupported",
                "error": f"job {job!r} declares exec kind {kind!r}; known kinds "
                         f"are {', '.join(sorted(KINDS))}"}

    tables = spec.get("tables") or []
    before = table_rows(root, tables)
    try:
        ok, detail, error, extra = KINDS[kind](root, spec, ctx)
        error_kind = None if ok else kind
    except Unsupported as exc:
        ok, detail, error, extra, error_kind = False, None, str(exc), {}, "unsupported"
    after = table_rows(root, tables)

    effects = dict(extra)
    for name in tables:
        effects[name] = {"rows_before": before.get(name), "rows_after": after.get(name)}
    if ctx["raw_output"] and log_name:
        path = root / RUN_DIR / "logs" / f"{log_name}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ctx["raw_output"], encoding="utf-8")
    return {"ok": ok, "detail": detail, "error": error,
            "error_kind": error_kind, "effects": effects}


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="world.executor")
    p.add_argument("root", type=Path)
    p.add_argument("job")
    p.add_argument("--day", help="execution day (YYYY-MM-DD) a `scan` job "
                                 "polls for; the scheduler supplies its own")
    a = p.parse_args(argv)
    from .scheduler import load_live, live_gen, live_seed
    live = load_live(a.root)
    spec = ((live or {}).get("jobs", {}).get(a.job) or {}).get("exec")
    print(json.dumps(execute(a.root, a.job, spec,
                             gen=live_gen(live), theme=(live or {}).get("theme"),
                             seed=live_seed(live), day=a.day),
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
