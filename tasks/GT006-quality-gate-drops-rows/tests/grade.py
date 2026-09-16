#!/usr/bin/env python3
"""
GT006 grader. Deterministic; zero LLM involvement in `total`.

    python grade.py --world <warehouse.duckdb> --grading <dir>
                    --artifact <final_report.json> --workspace <dir>
                    --world-root <dir> [--json]

Runs AFTER teardown, never during the episode. The reference lives in the
grading zone and is attached READ_ONLY (F121); it is never in the agent-visible
catalog and holding it cannot reach max, because G1c executes the agent's OWN
loader and copying a table does not change a file.

THE SHAPE OF THIS RUBRIC, in one paragraph. One mechanism - the precision of an
exclusion predicate - measured by three conjuncts that fail independently
(rows recovered, nothing over-recovered, and the agent's own rule re-executed
against rows it has never seen), plus the one downstream check that makes those
mean anything (the settlement KPI is restated), plus a penalty that can score
below doing nothing. Every clause that PAYS scores zero for an idle agent, and
every clause an idle agent satisfies by construction is worth zero and can only
subtract. tests/run_baselines.py asserts both of those rather than trusting
this paragraph.

Exit codes: 0 scored, 2 packaging error (the fixture or the grading zone is not
what the manifest describes - never scored as a hard task).
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from pathlib import Path

import duckdb
import yaml

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
sys.path.insert(0, str(TASK / "fixtures"))
sys.path.insert(0, str(HERE))

from fixture_contract import CONTRACT                              # noqa: E402
import collateral                                                  # noqa: E402

BUSINESS = CONTRACT["business_cols"]
MART = "fct_payments"
KPI = "kpi_payments_daily"


class PackagingError(Exception):
    """Our bug, not the agent's. Exit 2; never scored."""


def warn(msg: str) -> None:
    print(f"[grade] {msg}", file=sys.stderr)


# ------------------------------------------------------------------ manifest
def load_manifest(grading: Path) -> dict:
    for candidate in (grading / "manifest.yaml", HERE / "manifest.yaml"):
        if candidate.is_file():
            return yaml.safe_load(candidate.read_text(encoding="utf-8"))
    raise PackagingError(f"no manifest.yaml in {grading} or {HERE}")


def check_table(man: dict) -> dict:
    table = {c["id"]: c for c in man.get("checks", [])}
    if not table:
        raise PackagingError("manifest declares no checks")
    return table


# ------------------------------------------------------------------- scoring
class Score:
    def __init__(self, table: dict):
        self.table = table
        self.checks: list[dict] = []

    def add(self, cid: str, points: float, reason: str, detail=None,
            passed: bool | None = None) -> None:
        spec = self.table.get(cid)
        if spec is None:
            raise PackagingError(f"grade.py emitted {cid}, which the manifest "
                                 f"does not declare")
        maximum = float(spec.get("points", 0))
        floor = float(spec.get("floor", 0))
        points = max(floor, min(maximum, round(points, 2)))
        self.checks.append({
            "id": cid,
            "axis": spec.get("axis", ""),
            "kind": spec.get("kind", "pays"),
            "points": points,
            "max": maximum,
            "floor": floor,
            "in_pass_criterion": bool(spec.get("in_pass_criterion")),
            "passed": bool(passed if passed is not None else points >= maximum),
            "reason": reason,
            "detail": detail or {},
        })

    @property
    def total(self) -> float:
        return round(sum(c["points"] for c in self.checks), 2)

    def audit(self) -> None:
        emitted = {c["id"] for c in self.checks}
        declared = set(self.table)
        if emitted != declared:
            raise PackagingError(
                f"the manifest declares {sorted(declared)} and grade.py emitted "
                f"{sorted(emitted)}. A manifest that documents checks nobody "
                f"runs is worse than no manifest.")


def q(n: float) -> float:
    """Round to the nearest quarter point. Keeps the score readable and keeps a
    one-row difference from moving a total by 0.003."""
    return round(round(n * 4) / 4, 2)


# ----------------------------------------------------------------- warehouse
def connect(world: Path, expected: Path):
    """Aliased ATTACH, both READ_ONLY (F622). The graded warehouse is at
    var/warehouse/warehouse.duckdb, where a direct open makes the catalog and
    the `warehouse` schema collide and every two-part reference ambiguous."""
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{Path(world).as_posix()}' AS wh (READ_ONLY)")
    con.execute(f"ATTACH '{Path(expected).as_posix()}' AS ref (READ_ONLY)")
    return con


def proj(alias: str, table: str) -> str:
    cols = ", ".join(BUSINESS[table])
    return f"SELECT {cols} FROM {alias}.warehouse.{table}"


def preflight(con) -> None:
    """Refuse to score a world that is not the world this grader describes.

    A grader that silently scores a half-built fixture reports a hard task where
    there is a broken one.
    """
    problems = []
    for alias, table in (("wh", MART), ("wh", KPI), ("ref", MART), ("ref", KPI)):
        n = con.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE database_name = ? "
            "AND schema_name = 'warehouse' AND table_name = ?",
            [alias, table]).fetchone()[0]
        if n != 1:
            problems.append(f"{alias}.warehouse.{table} is missing")
    if problems:
        raise PackagingError("; ".join(problems))
    ref_rows = con.execute(
        f"SELECT count(*) FROM ref.warehouse.{MART}").fetchone()[0]
    if ref_rows != CONTRACT["expected"]["fct_payments_rows"]:
        raise PackagingError(
            f"the reference holds {ref_rows} rows, the contract says "
            f"{CONTRACT['expected']['fct_payments_rows']}: the grading zone is "
            f"not the one this fixture built")


# ------------------------------------------------------------------- G1a/G1b
def g1a_restore(con, s: Score) -> int:
    dropped = int(CONTRACT["defect"]["dropped_rows"])
    missing = con.execute(
        f"SELECT count(*) FROM (({proj('ref', MART)}) EXCEPT ALL "
        f"({proj('wh', MART)}))").fetchone()[0]
    points = q(8.0 * (1.0 - missing / dropped)) if dropped else 0.0
    by_day = con.execute(f"""
        SELECT count(DISTINCT payment_dt) FROM (
          ({proj('ref', MART)}) EXCEPT ALL ({proj('wh', MART)}))""").fetchone()[0]
    s.add("G1a", max(0.0, points),
          (f"every row the reference holds is present"
           if missing == 0 else
           f"{missing:,} of the {dropped:,} rows the rule dropped are still "
           f"missing, across {by_day} partitions"),
          {"still_missing": missing, "originally_dropped": dropped,
           "partitions_still_short": by_day},
          passed=(missing == 0))
    return missing


def g1b_precision(con, s: Score) -> int:
    extra = con.execute(
        f"SELECT count(*) FROM (({proj('wh', MART)}) EXCEPT ALL "
        f"({proj('ref', MART)}))").fetchone()[0]
    excl = ", ".join(f"'{r}'" for r in CONTRACT["rule"]["excluded_refs"])
    harness = con.execute(
        f"SELECT count(*) FROM wh.warehouse.{MART} WHERE terminal_ref "
        f"IN ({excl})").fetchone()[0]
    dupes = con.execute(
        f"SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) AS n FROM "
        f"wh.warehouse.{MART} GROUP BY payment_id HAVING count(*) > 1)").fetchone()[0]
    points = -q(8.0 * min(1.0, extra / 100.0)) if extra else 0.0
    if extra == 0:
        reason = "the mart holds nothing the reference does not"
    else:
        parts = [f"{extra:,} row(s) present that the reference does not hold"]
        if harness:
            parts.append(f"{harness:,} of them are the processor's test harness "
                         f"- the exclusion was widened or removed, which trades "
                         f"one silent failure for another")
        if dupes:
            parts.append(f"{dupes:,} are duplicate payment_ids")
        reason = "; ".join(parts)
    s.add("G1b", points, reason,
          {"extra_rows": extra, "harness_rows_readmitted": harness,
           "duplicate_rows": dupes},
          passed=(extra == 0))
    return extra


# ----------------------------------------------------------------------- G1c
def split_sql(text: str) -> list[str]:
    """Split a SQL file into statements. Quote- and comment-aware enough for
    the loaders in this world; a statement it mis-splits raises, which scores 0
    with the error in the reason rather than silently skipping work."""
    out, buf = [], []
    i, n = 0, len(text)
    in_s = in_d = in_line = in_block = False
    while i < n:
        c, nxt = text[i], text[i + 1] if i + 1 < n else ""
        if in_line:
            if c == "\n":
                in_line = False
                buf.append(c)
            i += 1
            continue
        if in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if in_s:
            buf.append(c)
            if c == "'":
                in_s = False
            i += 1
            continue
        if in_d:
            buf.append(c)
            if c == '"':
                in_d = False
            i += 1
            continue
        if c == "-" and nxt == "-":
            in_line = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if c == "'":
            in_s = True
            buf.append(c)
            i += 1
            continue
        if c == '"':
            in_d = True
            buf.append(c)
            i += 1
            continue
        if c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _probe_rows(grading: Path) -> list[dict]:
    path = grading / CONTRACT["probe"]["csv"]
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def g1c_rule(s: Score, grading: Path, workspace: Path) -> dict:
    """THE LIVENESS CLAUSE.

    Take a copy of the pre-episode world, overlay the agent's /workspace onto
    it, append one partition of payments the world has never seen, and run the
    agent's OWN nightly loader over it.

    A rule "fixed" by never running, or a load fixed into a permanent skip, is
    trivially consistent and repairs nothing: it scores ZERO here, not full
    marks. So does a rule that was deleted, because the harness rows then land.
    """
    detail: dict = {}
    probe = CONTRACT["probe"]
    admit_target = int(probe["admit"])
    pre = grading / "pre_repair_world"
    if not pre.is_dir():
        raise PackagingError(f"{pre} is missing; G1c cannot be evaluated and a "
                             f"gate that cannot be evaluated is worse than none")

    scratch = Path(tempfile.mkdtemp(prefix="gt006-g1c-"))
    try:
        world = scratch / "world"
        shutil.copytree(pre, world)
        ws = world / "workspace"
        if ws.exists():
            shutil.rmtree(ws, ignore_errors=True)
        if not Path(workspace).is_dir():
            s.add("G1c", 0.0, f"the agent's workspace {workspace} does not exist",
                  detail, passed=False)
            return detail
        shutil.copytree(workspace, ws,
                        ignore=shutil.ignore_patterns(".git", "__pycache__",
                                                      "target", "logs"))
        loader = ws / probe["loader"]
        if not loader.is_file():
            s.add("G1c", 0.0,
                  f"{probe['loader']} does not exist in the agent's workspace, so "
                  f"tonight's load has nothing to run. A repair that removes the "
                  f"nightly loader is not a repair.",
                  {"loader": probe["loader"], "present": False}, passed=False)
            return detail

        db = world / CONTRACT["layout"]["deployed_world_relpath"]
        con = duckdb.connect(":memory:", config={"threads": 1})
        con.execute(f"ATTACH '{db.as_posix()}' AS wh")
        con.execute("USE wh")
        try:
            # 1. the batch lands in the source, exactly as the processor would
            #    deliver it. Columns by name, so a reordered source still works.
            rows = _probe_rows(grading)
            cols = [c for c in rows[0]]
            placeholders = ", ".join("?" for _ in cols)
            for row in rows:
                con.execute(
                    f"INSERT INTO raw.payments_landing ({', '.join(cols)}) "
                    f"VALUES ({placeholders})",
                    [None if row[c] == "" else row[c] for c in cols])

            # 2. the agent's own loader runs
            statements = split_sql(loader.read_text(encoding="utf-8"))
            detail["statements"] = len(statements)
            if not statements:
                s.add("G1c", 0.0,
                      f"{probe['loader']} contains no executable statement, so "
                      f"tonight's load does nothing. A load that never runs is "
                      f"trivially consistent and repairs nothing.",
                      detail, passed=False)
                return detail
            for stmt in statements:
                con.execute(stmt)

            # 3. what landed?
            landed = {r[0] for r in con.execute(
                f"SELECT payment_id FROM warehouse.{MART} WHERE payment_dt = "
                f"DATE '{probe['partition']}'").fetchall()}
        except Exception as exc:                      # noqa: BLE001 - scored
            s.add("G1c", 0.0,
                  f"the agent's {probe['loader']} did not execute against a fresh "
                  f"delivery: {type(exc).__name__}: {str(exc)[:300]}",
                  detail, passed=False)
            return detail
        finally:
            con.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    man = json.loads((grading / probe["manifest"]).read_text(encoding="utf-8"))
    must_admit = {int(x) for x in man["must_be_admitted"]}
    must_reject = {int(x) for x in man["must_be_rejected"]}
    null_ref = {int(x) for x in man["null_ref_ids"]}
    landed = {int(x) for x in landed}

    ordinary = must_admit - null_ref
    admitted = landed & must_admit
    null_admitted = landed & null_ref
    ordinary_admitted = landed & ordinary
    harness_admitted = landed & must_reject
    unknown = landed - must_admit - must_reject
    detail.update({
        "legitimate_admitted": len(admitted), "legitimate_total": admit_target,
        "null_ref_admitted": len(null_admitted), "null_ref_total": len(null_ref),
        "ordinary_admitted": len(ordinary_admitted), "ordinary_total": len(ordinary),
        "harness_admitted": len(harness_admitted), "unknown_rows": len(unknown),
    })

    # THE GATE, in one sentence: score ONLY the rows the shipped rule gets
    # wrong, and score zero unless everything the shipped rule gets RIGHT is
    # still right.
    #
    # Scoring over all 54 admissible rows instead would hand an idle agent
    # 6 x 40/54 = 4.5 points for the 40 ordinary payments the broken rule never
    # touched - measured, on the world this package builds, before this line was
    # written. That is precisely the subsidy the rubric is not allowed to pay.
    if harness_admitted:
        s.add("G1c", 0.0,
              f"the repaired load admitted {len(harness_admitted)} of the "
              f"{len(must_reject)} processor test-harness rows in a fresh "
              f"delivery. The rule exists for a reason; turning it off trades "
              f"one silent failure for another.", detail, passed=False)
        return detail
    if unknown:
        s.add("G1c", 0.0,
              f"the repaired load wrote {len(unknown)} row(s) that were not in "
              f"the delivery at all", detail, passed=False)
        return detail
    if len(ordinary_admitted) != len(ordinary):
        s.add("G1c", 0.0,
              f"the repaired load lost {len(ordinary) - len(ordinary_admitted)} "
              f"of the {len(ordinary)} payments in a fresh delivery that the "
              f"SHIPPED rule already handled correctly. Tightening the filter is "
              f"the move that made this worse in the first place.",
              detail, passed=False)
        return detail

    points = q(6.0 * len(null_admitted) / len(null_ref))
    if len(null_admitted) == len(null_ref):
        reason = (f"a fresh delivery loads correctly: all {admit_target} "
                  f"legitimate payments admitted (including all {len(null_ref)} "
                  f"with an empty terminal_ref) and all {len(must_reject)} "
                  f"harness rows rejected")
    elif not admitted:
        reason = ("the repaired load admitted NOTHING from a fresh delivery. A "
                  "load that never runs, or that is gated permanently off, is "
                  "trivially consistent and repairs nothing.")
    else:
        reason = (f"a fresh delivery still loses "
                  f"{len(null_ref) - len(null_admitted)} of the {len(null_ref)} "
                  f"legitimate payments with an empty terminal_ref: the rule is "
                  f"still imprecise, or the data was patched and the code was not")
    s.add("G1c", points, reason, detail,
          passed=(len(null_admitted) == len(null_ref)))
    return detail


# ------------------------------------------------------------------------ G2
def g2_restate(con, s: Score) -> None:
    cols = BUSINESS[KPI]
    on = " AND ".join(f"w.{c} IS NOT DISTINCT FROM r.{c}" for c in cols)
    win_start = CONTRACT["defect"]["window_start"]
    win_end = CONTRACT["defect"]["window_end"]
    window_days = int(CONTRACT["defect"]["window_days"])

    matching_in_window = con.execute(f"""
        SELECT count(*) FROM ref.warehouse.{KPI} r
        JOIN wh.warehouse.{KPI} w ON w.dt = r.dt
        WHERE r.dt BETWEEN DATE '{win_start}' AND DATE '{win_end}' AND {on}
    """).fetchone()[0]
    all_days = con.execute(f"SELECT count(*) FROM ref.warehouse.{KPI}").fetchone()[0]
    matching_all = con.execute(f"""
        SELECT count(*) FROM ref.warehouse.{KPI} r
        JOIN wh.warehouse.{KPI} w ON w.dt = r.dt WHERE {on}""").fetchone()[0]
    stray = con.execute(f"""
        SELECT count(*) FROM (SELECT dt FROM wh.warehouse.{KPI}
                              EXCEPT SELECT dt FROM ref.warehouse.{KPI})
    """).fetchone()[0]
    outside_broken = (all_days - window_days) - (matching_all - matching_in_window)

    points = q(6.0 * matching_in_window / window_days)
    ok = (matching_all == all_days and stray == 0)
    if ok:
        reason = (f"the settlement KPI ties to the reference on every one of "
                  f"{all_days} partitions, to the cent")
    else:
        bits = [f"{window_days - matching_in_window} of {window_days} damaged "
                f"partitions still differ from the reference"]
        if outside_broken > 0:
            bits.append(f"{outside_broken} partition(s) OUTSIDE the damage window "
                        f"now differ too")
        if stray:
            bits.append(f"{stray} partition(s) exist that the reference does not have")
        reason = "; ".join(bits)
    s.add("G2", points, reason,
          {"window_partitions": window_days,
           "window_partitions_matching": matching_in_window,
           "all_partitions": all_days, "all_partitions_matching": matching_all,
           "partitions_outside_window_broken": max(0, outside_broken),
           "stray_partitions": stray},
          passed=ok)


# ------------------------------------------------------------------------ G3
def g3_collateral(s: Score, grading: Path, world_root: Path) -> None:
    baseline_path = grading / "world_sections.json"
    if not baseline_path.is_file():
        raise PackagingError(
            f"{baseline_path} is missing; the collateral check cannot be "
            f"evaluated and a gate that cannot be evaluated would silently pass")
    if world_root is None or not Path(world_root).is_dir():
        raise PackagingError("--world-root is required: G3 reads the seeded "
                             "planes under var/, which live outside the workspace")
    base = json.loads(baseline_path.read_text(encoding="utf-8"))
    res = collateral.score(Path(world_root), base)
    s.add("G3", res["points"], res["reason"], res["detail"],
          passed=(res["points"] >= 0))


# ------------------------------------------------------------------------ H1
def h1_report(s: Score, man: dict, artifact: Path) -> None:
    detail: dict = {"artifact": str(artifact)}
    if not Path(artifact).is_file():
        s.add("H1", 0.0, "no final_report.json was written", detail, passed=False)
        return
    try:
        doc = json.loads(Path(artifact).read_text(encoding="utf-8"))
    except ValueError as exc:
        s.add("H1", 0.0, f"final_report.json is not valid json: {exc}", detail,
              passed=False)
        return

    fam = man.get("report", {}).get("cause_tokens", {})
    cause = str(doc.get("primary_cause", "")).lower()
    has_rule = any(t in cause for t in fam.get("rule", []))
    has_loss = any(t in cause for t in fam.get("loss", []))
    cause_pt = 1.0 if (has_rule and has_loss) else 0.0

    want = [CONTRACT["defect"]["window_start"], CONTRACT["defect"]["window_end"]]
    got = doc.get("damage_window")
    window_pt = 1.0 if ([str(x) for x in got] == want
                        if isinstance(got, (list, tuple)) else False) else 0.0

    detail.update({"cause_names_a_rule": has_rule, "cause_names_loss": has_loss,
                   "damage_window_reported": got, "damage_window_expected": want})
    bits = []
    bits.append("names an exclusion rule removing valid rows" if cause_pt
                else "primary_cause does not identify a validation/exclusion rule "
                     "removing valid rows")
    bits.append("damage window correct" if window_pt
                else f"damage_window {got!r} is not {want!r}")
    s.add("H1", cause_pt + window_pt, "; ".join(bits), detail,
          passed=(cause_pt + window_pt == 2.0))


# ------------------------------------------------------------------- driver
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True, help="the graded warehouse duckdb")
    ap.add_argument("--grading", required=True, help="grading zone (harness-side)")
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--workspace", default=None, help="the agent's /workspace tree")
    ap.add_argument("--world-root", default=None,
                    help="the world root; G3 reads the planes outside /workspace")
    ap.add_argument("--expected", default=None, help="override expected.duckdb")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    grading = Path(a.grading)
    try:
        man = load_manifest(grading)
        table = check_table(man)
        expected = Path(a.expected) if a.expected else grading / "expected.duckdb"
        if not expected.is_file():
            raise PackagingError(f"no reference at {expected}")
        s = Score(table)
        con = connect(Path(a.world), expected)
        try:
            preflight(con)
            g1a_restore(con, s)
            g1b_precision(con, s)
            g2_restate(con, s)
        finally:
            con.close()
        g1c_rule(s, grading, Path(a.workspace) if a.workspace else Path("."))
        g3_collateral(s, grading, Path(a.world_root) if a.world_root else None)
        h1_report(s, man, Path(a.artifact))
        s.audit()
    except PackagingError as exc:
        warn(f"PACKAGING ERROR: {exc}")
        return 2

    requires = [c["id"] for c in s.checks if c["in_pass_criterion"]]
    floor = float(man.get("recommended_harness", {}).get("pass_min_total", 0.0))
    result = {
        "task_id": man.get("task_id"),
        "total": s.total,
        "max": round(sum(c["max"] for c in s.checks), 2),
        "checks": s.checks,
        "pass_requires_checks": requires,
        "pass_min_total": floor,
        "passed": (all(c["passed"] for c in s.checks if c["in_pass_criterion"])
                   and s.total >= floor),
    }
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{result['total']} / {result['max']}   "
              f"passed={result['passed']}")
        for c in s.checks:
            flag = "PASS" if c["passed"] else "FAIL"
            print(f"  {c['id']:<4} {c['points']:>6} / {c['max']:<4} {flag}  "
                  f"{c['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
