"""The publication ledger: what a consumer could see, recorded when it could see it.

The problem this exists for. A grader that reads only the final world cannot
tell a pipeline that was right all along from one that published a wrong number
on Tuesday and quietly corrected it on Thursday. The warehouse holds the latest
state and nothing else; `var/sched/state.json` holds run records, which say a
job succeeded, not what it made visible. So a repair that crosses a publication
boundary - the one failure mode worth measuring in a recovery task - is
invisible by construction. The ledger closes that: at every moment the world
publishes something, it records the canonical contents of what was published,
under the virtual time at which it happened.

Three properties, and each one is load-bearing:

- **Written by the world, never by the agent.** The only writer is
  `world/world/scheduler.py`, running outside the episode container. No gateway
  tool reaches `var/ledger` (the fs_* root is `<world_root>/workspace`), and in
  the container topology the path is shadowed by an empty read-only mount, so
  the agent's shell sees an empty directory rather than a denied one.
- **Append-only.** `append_receipt` opens the file with mode "ab" and writes one
  canonical JSON line. Nothing here rewrites, truncates or re-orders. A ledger
  that can be edited after the fact is not evidence - the same argument
  var/obs/samples.jsonl already makes (agent.md 9.2.6).
- **Deterministic.** Same seed plus the same advances must produce a
  byte-identical receipts.jsonl. Everything that goes into a line comes from the
  world: `vnow` is virtual time from the scheduler, never a wall clock; the
  table hash is the canonical-contents rule `fingerprint.py` already uses
  (sorted rows, typed values, not file bytes); the sample is the first rows of
  that same total order.

Schema - one JSON object per line, exactly these keys (`KEYS`):

    seq             int, 1-based, contiguous within a world
    vnow            ISO-8601 virtual time (the run's vts_end: a publication is
                    visible when the job that made it has finished)
    day             ISO date - the EXECUTION day the occurrence is responsible
                    for, not the wall date
    job             scheduler job name
    target          "catalog.schema.table" / "schema.table", or a
                    workspace-relative file path
    kind            "table" | "file"
    canonical_hash  sha256 of canonical logical contents (see below)
    row_count       int, or null when the target does not exist / is a file
    sample          <= 5 rows as lists in the same canonical order, or null
    consumer        the consumer named in the spec, or null

Canonical hashing.

    table   exactly fingerprint._warehouse_hash's per-table rule: sha256 over
            json_canon({"table": "<schema>.<table>", "columns": [[name, type]...]})
            followed by one json_canon([canon_value(c) for c in row]) + b"\n"
            per row, rows ordered by every column in ordinal position. For a
            target in the primary warehouse this hash IS the value
            fingerprint.semantic()["warehouse"]["tables"][target] carries, which
            is the point: one canonicalisation rule in this repository, not two.
    file    sha256 of the bytes.
    absent  a target that does not exist hashes to ABSENT - a fixed sentinel,
            not sha256(b"") - so "there was nothing there" is distinguishable
            from "there was an empty file there". row_count and sample are null.

A missing target is recorded rather than raised on purpose. `advance` is the
world moving; a publication step that crashed the clock because a model failed
to build would be a worse failure than a receipt that honestly says the table
was not there.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb

from .hashing import canon_value, json_canon

LEDGER = Path("var") / "ledger" / "receipts.jsonl"
WAREHOUSE_DIR = Path("var") / "warehouse"
PRIMARY = "warehouse"                  # var/warehouse/warehouse.duckdb
KEYS = ("seq", "vnow", "day", "job", "target", "kind", "canonical_hash",
        "row_count", "sample", "consumer")
KINDS = ("table", "file")
SAMPLE_ROWS = 5

# The hash of a target that is not there. A literal sentinel, so it can never
# collide with the hash of any real contents.
ABSENT = hashlib.sha256(b"alkera-ledger/absent/v1\n").hexdigest()


class LedgerError(Exception):
    """A publication spec the world cannot honour. Raised at build/advance time,
    never swallowed: a `publishes` entry nobody can resolve is an author defect,
    and a ledger that silently skips it records a world that did not happen."""


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def path(root: Path) -> Path:
    return Path(root) / LEDGER


def load(root: Path) -> list[dict]:
    """Every receipt, in file order. Missing file -> []."""
    file = path(root)
    if not file.is_file():
        return []
    out = []
    for line in file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def next_seq(root: Path) -> int:
    return len(load(root)) + 1


# --------------------------------------------------------------------------
# publication specs
# --------------------------------------------------------------------------

def publishes(live: dict | None, state: dict | None, job: str) -> list[dict]:
    """The `publishes` list declared for `job`, normalised, in declared order.

    Read from three places, first hit wins, because two file shapes have to be
    able to carry it:

      live["jobs"][job]["publishes"]          - the theme manifest's job block,
                                                carried into var/world/live.json
                                                verbatim by initial_live()
      live["jobs"][job]["exec"]["publishes"]  - beside the work it describes
      state["jobs"][job]["publishes"]         - a hand-written fixture world
                                                that has no live control plane

    state.json's `jobs` shape is frozen at {command, duration_s} (agent.md 9.2),
    and nothing here writes to it: a task author who puts `publishes` there is
    opting in, and `sched_list` in an ordinary world returns exactly what it
    always did.
    """
    job_spec = ((live or {}).get("jobs", {}) or {}).get(job) or {}
    spec = (job_spec.get("publishes")
            or (job_spec.get("exec") or {}).get("publishes")
            or (((state or {}).get("jobs", {}) or {}).get(job) or {}).get("publishes")
            or [])
    if not isinstance(spec, list):
        raise LedgerError(f"job {job!r}: `publishes` must be a list, got "
                          f"{type(spec).__name__}")
    out = []
    for entry in spec:
        if not isinstance(entry, dict):
            raise LedgerError(f"job {job!r}: each `publishes` entry must be a "
                              f"mapping, got {type(entry).__name__}")
        target, kind = entry.get("target"), entry.get("kind")
        if not target or not isinstance(target, str):
            raise LedgerError(f"job {job!r}: a `publishes` entry has no target")
        if kind not in KINDS:
            raise LedgerError(
                f"job {job!r}: publication {target!r} declares kind {kind!r}; "
                f"known kinds are {', '.join(KINDS)}")
        consumer = entry.get("consumer")
        if consumer is not None and not isinstance(consumer, str):
            raise LedgerError(f"job {job!r}: publication {target!r} has a "
                              f"non-string consumer")
        out.append({"job": job, "target": target, "kind": kind,
                    "consumer": consumer})
    return out


# --------------------------------------------------------------------------
# canonical contents
# --------------------------------------------------------------------------

def _split_table(target: str) -> tuple[str, str, str]:
    """catalog.schema.table, or schema.table in the primary warehouse."""
    parts = target.split(".")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return PRIMARY, parts[0], parts[1]
    raise LedgerError(
        f"publication target {target!r} is not a table name: expected "
        f"catalog.schema.table or schema.table")


def _connect(db: Path):
    """ATTACH under an alias - never open the warehouse file directly (9.2.1)."""
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh (READ_ONLY)")
    con.execute("USE wh")
    return con


def table_contents(root: Path, target: str) -> dict:
    """{"canonical_hash", "row_count", "sample"} for one table.

    The hash rule is fingerprint._warehouse_hash's, reproduced here rather than
    imported because that function hashes a whole warehouse and this needs one
    table - and because the ledger must keep producing the same value if the
    fingerprint ever grows a section. The equality of the two is asserted by a
    test (world/tests/test_ledger.py), which is the honest way to share a rule
    that cannot be shared by a call.
    """
    catalog, schema, table = _split_table(target)
    db = Path(root) / WAREHOUSE_DIR / f"{catalog}.duckdb"
    if not db.is_file():
        return {"canonical_hash": ABSENT, "row_count": None, "sample": None}
    con = _connect(db)
    try:
        cols = con.execute(
            "select column_name, data_type from information_schema.columns "
            "where table_schema = ? and table_name = ? "
            "order by ordinal_position", [schema, table]).fetchall()
        if not cols:
            return {"canonical_hash": ABSENT, "row_count": None, "sample": None}
        order = ", ".join(f'"{c}"' for c, _ in cols)
        rows = con.execute(
            f'select * from wh."{schema}"."{table}" order by {order}').fetchall()
    except duckdb.Error:
        # Catalog/binder failure: the object is not readable as a table.
        return {"canonical_hash": ABSENT, "row_count": None, "sample": None}
    finally:
        con.close()
    h = hashlib.sha256()
    h.update(json_canon({"table": f"{schema}.{table}", "columns": cols}))
    canon = []
    for index, row in enumerate(rows):
        values = [canon_value(c) for c in row]
        h.update(json_canon(values) + b"\n")
        if index < SAMPLE_ROWS:
            canon.append(values)
    return {"canonical_hash": h.hexdigest(), "row_count": len(rows),
            "sample": canon}


def file_contents(root: Path, target: str) -> dict:
    """{"canonical_hash", "row_count", "sample"} for one workspace file.

    `target` is workspace-relative (agent.md 9.2.1: /workspace is the repo
    root), and it may not leave the workspace - a ledger entry that reads
    ../grading would be a hole in ground rule 1 opened by an author typo.
    row_count and sample are null: bytes are what a file consumer receives, and
    inventing a row model for an arbitrary file would be a second
    canonicalisation rule with no oracle behind it.
    """
    workspace = (Path(root) / "workspace").resolve()
    file = (workspace / target).resolve()
    if not file.is_relative_to(workspace):
        raise LedgerError(f"publication target {target!r} leaves the workspace")
    if not file.is_file():
        return {"canonical_hash": ABSENT, "row_count": None, "sample": None}
    return {"canonical_hash": hashlib.sha256(file.read_bytes()).hexdigest(),
            "row_count": None, "sample": None}


def contents(root: Path, spec: dict) -> dict:
    if spec["kind"] == "table":
        return table_contents(root, spec["target"])
    return file_contents(root, spec["target"])


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def append_receipt(root: Path, spec: dict, vnow: str, day: str | None) -> dict:
    """Read the target's canonical contents and append one receipt. Returns it.

    `spec` is one normalised `publishes` entry plus its job:
    {"job", "target", "kind", "consumer"}. `vnow` is the virtual time the
    publication became visible (the run's vts_end) and `day` is the execution
    day - both supplied by the caller, because this module never reads a clock.
    """
    root = Path(root)
    receipt = {"seq": next_seq(root),
               "vnow": vnow,
               "day": day,
               "job": spec["job"],
               "target": spec["target"],
               "kind": spec["kind"],
               "consumer": spec.get("consumer"),
               **contents(root, spec)}
    if set(receipt) != set(KEYS):
        raise LedgerError(f"receipt keys {sorted(receipt)} != {sorted(KEYS)}")
    file = path(root)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("ab") as stream:          # append-only, and nothing else
        stream.write(json_canon({k: receipt[k] for k in KEYS}) + b"\n")
    return receipt


def record(root: Path, live: dict | None, state: dict | None, job: str,
           vnow: str, day: str | None) -> list[dict]:
    """Every receipt this occurrence of `job` owes, in declared order.

    The scheduler's only entry point. A job with no `publishes` writes nothing
    and creates no file, so a world that declares no publications is
    byte-identical to one built before this module existed - the same
    conditional-section rule the fingerprint and the fault layer follow.
    """
    return [append_receipt(root, spec, vnow, day)
            for spec in publishes(live, state, job)]


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        prog="world.ledger", description="read the publication ledger")
    p.add_argument("root", type=Path)
    a = p.parse_args(argv)
    print(json.dumps(load(a.root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
