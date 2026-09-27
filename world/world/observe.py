"""The observability *writer*: /var/obs/samples.jsonl (gateway/SURFACES.md, "The sample store").

OPS-SURFACE shipped a metrics surface that computes what *is*. The moment
`advance` runs, the state that produced yesterday's reading is gone and the
recorded sample becomes the only evidence it ever held that value. This module
writes that record. `gateway/src/alkera_gateway/observability.py` (OBS-HISTORY)
reads it; this file and that one share nothing but the frozen contract.

**Why it lives in the fault layer's half of the tree.** A fault that leaves no
trace in the observability record is a fault nobody can investigate. Freshness
spiking on a late partition, a job's error rate stepping up on a retry storm, a
duration jumping on an overrun - those are the readings the incident is *made
of*, and they only exist because something took them at the time. So the writer
is not decoration bolted next to the faults; it is how the faults become
evidence.

Rules taken verbatim from the contract:

- `vts` is virtual time, never wall clock. The reading is stamped at the moment
  it was taken: the run's `vts_end`.
- `value: null` means *insufficient data*, and is not `0.0`. A metric over a
  missing table blanks; it never zeroes. Silent-wrong is disqualifying.
- Appending is the only operation. Nothing here rewrites or reorders history.
- Same seed + same span => byte-identical `samples.jsonl`.

**One honest deviation from "ordered by vts", and it is forced.** Samples are
appended in *measurement* order - the order jobs start - because that order is
path-independent: `advance(1)` thirty times walks the same event sequence as
`advance(30)`, so the file comes out identical either way. Stamping order by
`vts_end` instead would be strictly vts-sorted within one call and *not*
path-independent, because a `job_overrun` run finishes after events that a
later, smaller `advance` has not simulated yet. With `job_overrun` active, a
line can therefore carry an earlier `vts` than the line above it. A reader that
needs vts order must sort; determinism won the tie, as the brief requires.
"""
import json
from datetime import datetime
from pathlib import Path

import duckdb
import yaml

OBS = Path("var") / "obs"
SAMPLES = OBS / "samples.jsonl"
# The frozen store contract (now in gateway/SURFACES.md#observability) names
# definitions.yaml; that doc named observability.yml first. Read either, prefer the frozen seam's name.
DEFINITION_NAMES = ("definitions.yaml", "observability.yml", "observability.yaml")

KINDS = ("freshness", "row_count", "null_rate", "job_duration",
         "job_error_rate", "sql_scalar")

# The distinction that keeps this file from being a determinism hazard.
#
# A reading that cannot be taken because the OBJECT is not there - a dropped
# table, an unknown column - is world state. The contract says record it as
# insufficient data (`null` with a reason), never as zero.
#
# A reading that cannot be taken because the ENGINE would not answer - a file
# lock, an IO error, out of memory - is NOT world state. It is a property of
# the machine at that instant. Recording it as `null` writes a transient
# permanently into samples.jsonl, which is inside the semantic fingerprint, so
# the world stops rebuilding to the same hash *depending on how loaded the box
# was*. That is the shape of bug that makes a determinism gate pass in
# isolation and fail under a full suite, and it is a silent-wrong conversion of
# "could not measure" into "measured nothing" - which this project treats as
# disqualifying. So it is loud instead: the build or the advance fails.
MISSING_OBJECT = (duckdb.CatalogException, duckdb.BinderException)


class ReadingFailed(RuntimeError):
    """The engine would not answer. Not insufficient data - a failed run."""


def _parse(s: str) -> datetime:
    from .scheduler import parse_vtime
    return parse_vtime(s)


def definitions_path(root: Path) -> Path | None:
    for name in DEFINITION_NAMES:
        path = Path(root) / OBS / name
        if path.is_file():
            return path
    return None


def definitions(root: Path) -> dict | None:
    path = definitions_path(root)
    if path is None:
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _connect(root: Path):
    """ATTACH under an alias - never open warehouse.duckdb directly (9.2.1)."""
    from . import executor
    return executor._connect(root, read_only=True)


# --------------------------------------------------------------------------
# readings. Every one returns (value, detail); value None == insufficient data.
# --------------------------------------------------------------------------

def _scalar(con, sql: str):
    try:
        row = con.execute(sql).fetchone()
    except MISSING_OBJECT as exc:
        # The object is not there. That IS world state, and the contract says
        # to record it as insufficient data rather than as zero.
        return None, {"reason": type(exc).__name__}
    except duckdb.Error as exc:
        # The engine would not answer: a lock, an IO error, out of memory.
        # That is NOT world state. See ReadingFailed.
        raise ReadingFailed(
            f"observability reading failed ({type(exc).__name__}): {sql}") from exc
    return (None, {"reason": "empty_result"}) if row is None else (row[0], None)


def _as_datetime(value):
    """A DATE measures to midnight - that is the honest reading for a column
    with no time of day, not a rounding error (SURFACES.md#observability)."""
    from datetime import date
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _freshness(con, metric, vts: datetime):
    src = metric.get("source") or {}
    table, column = src.get("table"), src.get("timestamp_column")
    if not table or not column:
        return None, {"reason": "definition_incomplete"}
    value, detail = _scalar(con, f"select max({column}) from {table}")
    if value is None:
        return None, detail or {"reason": "no_rows"}
    watermark = _as_datetime(value)
    if watermark is None:
        # A column we cannot read as a time is "could not measure", not a
        # freshness of zero. Never fabricate a reading.
        return None, {"reason": "unreadable_timestamp", "raw": str(value)}
    watermark = watermark.replace(tzinfo=vts.tzinfo)
    return ((vts - watermark).total_seconds(),
            {"watermark": watermark.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "as_of": vts.strftime("%Y-%m-%dT%H:%M:%SZ")})


def _row_count(con, metric, vts):
    src = metric.get("source") or {}
    if not src.get("table"):
        return None, {"reason": "definition_incomplete"}
    value, detail = _scalar(con, f"select count(*) from {src['table']}")
    return (None, detail) if value is None else (int(value), {"table": src["table"]})


def _null_rate(con, metric, vts):
    src = metric.get("source") or {}
    table, column = src.get("table"), src.get("column")
    if not table or not column:
        return None, {"reason": "definition_incomplete"}
    row, detail = _scalar(
        con, f"select count(*) || ':' || count({column}) from {table}")
    if row is None:
        return None, detail
    total, present = (int(x) for x in str(row).split(":"))
    if total == 0:
        # zero rows is "could not measure", not "no nulls". Never 0.0.
        return None, {"reason": "no_rows", "rows": 0}
    return ((total - present) / total,
            {"rows": total, "non_null": present})


def _finished(runs, job, vts: datetime):
    """Runs of `job` that had finished by `vts`, oldest first.

    A run that has not finished by `vts` is not history and contributes to
    nothing (SURFACES.md#observability). Sorted on `vts_end` rather than trusted
    to be in order: a retried occurrence appends its second attempt after the
    downstream job that was scheduled between them, so the run list is in
    completion order, not clock order, and "the latest finished run" has to be
    asked for by time.
    """
    done = [r for r in runs
            if r.get("job") == job and _parse(r["vts_end"]) <= vts]
    return sorted(done, key=lambda r: (r["vts_end"], r["vts_start"]))


def _job_duration(runs, metric, vts):
    job = metric.get("job") or (metric.get("source") or {}).get("job")
    if not job:
        return None, {"reason": "definition_incomplete"}
    done = _finished(runs, job, vts)
    if not done:
        return None, {"reason": "no_finished_runs", "job": job}
    last = done[-1]
    seconds = (_parse(last["vts_end"]) - _parse(last["vts_start"])).total_seconds()
    return seconds, {"job": job, "vts_start": last["vts_start"],
                     "vts_end": last["vts_end"],
                     "status_of_run": last.get("status")}


def _job_error_rate(runs, metric, vts):
    job = metric.get("job") or (metric.get("source") or {}).get("job")
    if not job:
        return None, {"reason": "definition_incomplete"}
    window = int(metric.get("window_runs", 10))
    done = _finished(runs, job, vts)[-window:]
    if not done:
        return None, {"reason": "no_finished_runs", "job": job}
    bad = [r for r in done if r.get("status") != "success"]
    return (len(bad) / len(done),
            {"job": job, "runs": len(done), "failed": len(bad),
             "window_runs": window})


def _sql_scalar(con, metric, vts):
    sql = metric.get("sql") or (metric.get("source") or {}).get("sql")
    if not sql:
        return None, {"reason": "definition_incomplete"}
    value, detail = _scalar(con, sql)
    return (None, detail) if value is None else (value, {"sql": sql})


def _canon(value):
    """Sample values must serialize identically on every host."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    from decimal import Decimal
    if isinstance(value, Decimal):
        return float(value)
    from datetime import date
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def read(root: Path, defs: dict, runs: list, vts: datetime) -> list:
    """One reading per defined metric, in declaration order."""
    metrics = defs.get("metrics") or []
    if not metrics:
        return []
    needs_db = any(m.get("kind") in ("freshness", "row_count", "null_rate",
                                     "sql_scalar") for m in metrics)
    con = _connect(root) if needs_db else None
    out = []
    try:
        for metric in metrics:
            kind = metric.get("kind")
            if kind == "freshness":
                value, detail = _freshness(con, metric, vts)
            elif kind == "row_count":
                value, detail = _row_count(con, metric, vts)
            elif kind == "null_rate":
                value, detail = _null_rate(con, metric, vts)
            elif kind == "job_duration":
                value, detail = _job_duration(runs, metric, vts)
            elif kind == "job_error_rate":
                value, detail = _job_error_rate(runs, metric, vts)
            elif kind == "sql_scalar":
                value, detail = _sql_scalar(con, metric, vts)
            else:
                value, detail = None, {"reason": f"unknown_kind:{kind}"}
            out.append({"metric": metric.get("id", "?"),
                        "value": _canon(value), "detail": detail or {}})
    finally:
        if con is not None:
            con.close()
    return out


# --------------------------------------------------------------------------
# the append
# --------------------------------------------------------------------------

def run_id(index: int, job: str) -> str:
    """Stable per-run identifier: the job plus its 1-based position in history."""
    return f"{job}-{index:04d}"


def record(root: Path, state: dict, vts: datetime, source: str,
           run_id_str: str | None = None) -> int:
    """Take one reading per defined metric and append it. Returns lines written.

    A world whose theme seeds no definitions writes nothing and creates no
    file - the conditional-section rule the rest of the tree already follows,
    so a world without observability keeps its recorded fingerprint.
    """
    root = Path(root)
    defs = definitions(root)
    if not defs:
        return 0
    rows = read(root, defs, state.get("runs") or [], vts)
    if not rows:
        return 0
    stamp = vts.strftime("%Y-%m-%dT%H:%M:%SZ")
    path = root / SAMPLES
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as fh:
        for row in rows:
            line = {"vts": stamp, "metric": row["metric"], "value": row["value"],
                    "source": source, "run_id": run_id_str,
                    "detail": row["detail"]}
            fh.write(json.dumps(line, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=True).encode("utf-8") + b"\n")
    return len(rows)


def samples(root: Path) -> list:
    path = Path(root) / SAMPLES
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="world.observe")
    p.add_argument("root", type=Path)
    p.add_argument("--metric", default=None)
    a = p.parse_args(argv)
    for row in samples(a.root):
        if a.metric in (None, row["metric"]):
            print(f"{row['vts']} {row['metric']:<28} {row['value']!s:<12} "
                  f"{row['source']:<8} {row['run_id'] or ''}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
