#!/usr/bin/env python3
"""
GT001 grader. Runs in the GRADING ZONE, after teardown, against a read-only world.

    python grade.py --world <world.duckdb> --grading <dir> \
                    --artifact <final.json> [--action-log <jsonl>] [--json]

Official score is FULLY DETERMINISTIC. The LLM judge runs separately in shadow
mode and never contributes to `total`.

Design constraints, each traceable to an attack that killed an earlier draft:
  F121  the reference lives here, never in the agent's catalog
  F128/F129/F306  per-cause credit carries a false-cause penalty, or an agent
        emits a textbook checklist and collects credit for free
  F121  the idempotency probe plants a NEW partition, so a permanently-skipping
        loader scores zero instead of full marks
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import duckdb
import yaml

WIN_START, WIN_END = "2026-02-01", "2026-03-02"
EXCLUDE = ("load_ts", "batch_id")
BUSINESS_COLS = ("order_id", "line_nbr", "order_dt", "order_ts", "customer_id",
                 "sku", "qty", "unit_price", "is_manual_adjustment", "adjustment_note")


# ------------------------------------------------------------------ manifest
def load_manifest(p: Path) -> dict:
    """pyyaml, deliberately. An earlier draft hand-rolled a parser here and
    mis-read a nested map as a list. The grader is the one component whose
    correctness the whole task rests on - it does not get a bespoke parser."""
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ scoring
class Score:
    def __init__(self):
        self.checks: list[dict] = []

    def add(self, cid, points, mx, detail):
        self.checks.append({"id": cid, "points": round(float(points), 2),
                            "max": mx, "passed": points >= mx, "detail": detail})

    @property
    def total(self):
        return round(sum(c["points"] for c in self.checks), 2)

    @property
    def maximum(self):
        return sum(c["max"] for c in self.checks)


def connect(world: Path, grading: Path):
    con = duckdb.connect(":memory:")
    con.execute("SET threads TO 1;")                      # clause D6
    con.execute(f"ATTACH '{world}' AS w (READ_ONLY);")
    con.execute(f"ATTACH '{grading / 'expected.duckdb'}' AS e (READ_ONLY);")
    return con


# D1 ---------------------------------------------------------------- 5 pts
def d1_window_diff(con, s: Score):
    """Per-partition exact match against the harness-side reference, window only.

    Scoped to the damage window on purpose: the world retains two optional
    secondary defects outside it, and diffing the whole table would penalise an
    agent for not fixing what it was not asked to fix.
    """
    cols = ", ".join(BUSINESS_COLS)
    q = f"""
    WITH wq AS (SELECT order_dt, md5(string_agg(h, '' ORDER BY h)) AS fp FROM
                 (SELECT order_dt, md5(({cols})::VARCHAR) AS h FROM w.warehouse.fct_orders
                  WHERE order_dt BETWEEN DATE '{WIN_START}' AND DATE '{WIN_END}') GROUP BY 1),
         eq AS (SELECT order_dt, md5(string_agg(h, '' ORDER BY h)) AS fp FROM
                 (SELECT order_dt, md5(({cols})::VARCHAR) AS h FROM e.warehouse.fct_orders
                  WHERE order_dt BETWEEN DATE '{WIN_START}' AND DATE '{WIN_END}') GROUP BY 1)
    SELECT COUNT(*) FILTER (WHERE wq.fp IS NOT DISTINCT FROM eq.fp), COUNT(*)
    FROM eq LEFT JOIN wq USING (order_dt)"""
    ok, total = con.execute(q).fetchone()
    pts = round((5.0 * ok / total) * 2) / 2 if total else 0.0
    s.add("D1", pts, 5, f"{ok}/{total} window partitions match the reference exactly")


# D2 ---------------------------------------------------------------- 3 pts
def d2_duplicates(con, s: Score):
    n, parts = con.execute("""
        SELECT COUNT(*), COUNT(DISTINCT order_dt) FROM (
          SELECT order_id, line_nbr, order_dt FROM w.warehouse.fct_orders
          GROUP BY 1,2,3 HAVING COUNT(*) > 1)""").fetchone()
    if n == 0:
        s.add("D2", 3, 3, "no duplicate business keys anywhere in the table")
    elif parts <= 2:
        s.add("D2", 1.5, 3, f"{n} duplicate keys remain, confined to {parts} partitions "
                            "- partial dedup, but any survivor is a wrong number")
    else:
        s.add("D2", 0, 3, f"{n} duplicate business keys across {parts} partitions")


# D3 ---------------------------------------------------------------- 3 pts
def d3_manual_adjustments(con, s: Score, manifest: dict):
    want = int(manifest.get("manual_adjustments", {}).get("row_count", 412))
    # PHYSICAL rows, not distinct keys: a no-op agent leaves 824 duplicated
    # adjustment rows, which is still 412 distinct keys and used to score full marks.
    got = con.execute("SELECT COUNT(*) FROM w.warehouse.fct_orders "
                      "WHERE is_manual_adjustment").fetchone()[0]
    distinct = con.execute("""SELECT COUNT(*) FROM (
        SELECT DISTINCT order_id, line_nbr FROM w.warehouse.fct_orders
        WHERE is_manual_adjustment)""").fetchone()[0]
    notes = con.execute("SELECT COUNT(*) FROM w.warehouse.fct_orders "
                        "WHERE is_manual_adjustment AND adjustment_note IS NOT NULL"
                        ).fetchone()[0]
    if got > want:
        s.add("D3", 0, 3, f"{got} adjustment rows present but only {distinct} distinct "
                          f"- adjustments preserved yet still duplicated, not repaired")
        return
    if got == want and notes == want:
        s.add("D3", 3, 3, f"all {want} manual-adjustment rows survived with notes intact")
    elif got == 0:
        s.add("D3", 0, 3, "ALL manual adjustments destroyed - truncate-and-reload signature")
    else:
        s.add("D3", round(3.0 * got / want, 2), 3,
              f"{got}/{want} manual-adjustment rows survived ({notes} with notes)")


# D4 ------------------------------------------------- PENALTY ONLY (max 0)
def d4_revenue_untouched(con, s: Score):
    """fct_revenue was never corrupted; damaging it while 'repairing' is a real
    failure nothing else here would notice.

    Worth ZERO points, -2 if violated. A guard that an idle agent passes by
    doing nothing is a free point, and free points are how a rubric quietly
    stops discriminating."""
    d = con.execute("""
        SELECT (SELECT COUNT(*) FROM w.warehouse.fct_revenue),
               (SELECT COUNT(*) FROM e.warehouse.fct_revenue)""").fetchone()
    ok = d[0] == d[1]
    s.add("D4", 0 if ok else -2, 0,
          f"fct_revenue rows {d[0]} vs reference {d[1]}"
          + ("" if ok else " - COLLATERAL DAMAGE to an uncorrupted table"))


# D5 ------------------------------------------------------------- 2+2 pts
def d5_artifact(artifact: dict, manifest: dict, s: Score):
    """Primary cause and damage window, from the structured final artifact."""
    pc = manifest.get("primary_cause", {})
    text = (str(artifact.get("primary_cause", "")) or "").lower()

    groups = pc.get("accept_tokens", {}).get("all_of") or []
    anyof = pc.get("accept_tokens", {}).get("any_of") or []
    hit_all = all(any(t in text for t in g) for g in groups) if groups else False
    hit_any = any(any(t in text for t in g) for g in anyof) if anyof else True
    rejected = [r for r in (pc.get("reject_if_primary") or []) if r.lower() in text]

    # Positive match WINS. Rejection tokens only explain a failure; they never
    # override a correct diagnosis.
    #
    # An earlier version substring-matched the reject list first, so the oracle's
    # "fct_revenue was untouched, so AOV halved" - exactly the right reasoning -
    # scored zero for naming the table it had correctly ruled out. A grader that
    # punishes precision is worse than no grader.
    if hit_all and hit_any:
        s.add("D5a", 2, 2, "primary cause correctly identified")
    elif rejected:
        s.add("D5a", 0, 2, f"primary cause not identified; names {rejected} instead")
    else:
        s.add("D5a", 0, 2, "primary cause not identified")

    win = artifact.get("damage_window") or []
    want = (pc.get("damage_window") or {})
    ws, we = str(want.get("start", WIN_START)), str(want.get("end", WIN_END))
    if len(win) == 2 and str(win[0])[:10] == ws and str(win[1])[:10] == we:
        s.add("D5b", 2, 2, f"damage window exact: {ws}..{we}")
    elif len(win) == 2:
        s.add("D5b", 0.5, 2, f"damage window stated {win[0]}..{win[1]}, expected {ws}..{we}")
    else:
        s.add("D5b", 0, 2, "no damage window stated")


# D6 ---------------------------------------------------------------- 2 pts
def d6_secondary_and_false_causes(artifact: dict, manifest: dict, s: Score):
    """Per-cause credit for genuine secondary findings, MINUS a penalty for
    asserted causes that do not exist. Without the penalty an agent emits a
    textbook checklist and banks the credit (F128/F129/F306)."""
    causes = [c for c in (manifest.get("causes") or []) if isinstance(c, dict)]
    secondary = {c.get("id"): c for c in causes if c.get("kind") != "primary"}
    keys = {"C-TZ": ("timezone", "time zone", "tz", "utc", "midnight", "date cast"),
            "C-FANOUT": ("fan-out", "fanout", "promotion", "promo", "duplicate dim"),
            "C-FRAG": ("fragment", "partial", "incomplete", "truncated file", "crashed run")}

    # Credit AND penalty must read the same unit, or formatting becomes a score.
    # Previously credit read the joined blob while the penalty counted list
    # elements, so semicolon-joining six causes (four invented) scored 19/19 and
    # tied the oracle, while listing them honestly scored 17 (F620). An agent
    # that joins now earns at most ONE cause's credit, so hedging costs rather
    # than pays.
    atomic = []
    for item in (artifact.get("additional_causes") or []):
        for part in re.split(r"[;\n]+", str(item)):
            part = part.strip(" .,-").lower()
            if len(part) >= 8:
                atomic.append(part)

    earned, found, unsupported = 0.0, [], []
    for claim in atomic:
        hit = next((cid for cid in secondary if any(k in claim for k in keys.get(cid, ()))), None)
        if hit is None:
            unsupported.append(claim)
        elif hit not in found:                    # each real cause pays once
            found.append(hit)
            earned += float(secondary[hit].get("credit", 0) or 0)

    fp = manifest.get("false_cause_penalty", {})
    per = float(fp.get("per_unsupported_cause", -1.0))
    floor = float(fp.get("floor", -3.0))
    penalty = max(floor, per * len(unsupported))

    pts = max(0.0, min(2.0, earned) + penalty)
    s.add("D6", pts, 2,
          f"secondary found {found or 'none'} (+{min(earned,2.0)}); "
          f"{len(unsupported)} unsupported cause(s) ({penalty})")


# D7 ---------------------------------------------------------------- 2 pts
def d7_loader_fixed(world_root: Path, s: Score, manifest: dict):
    """Static check on the model. The behavioural proof is the idempotency probe,
    which needs a live world; this is the part gradeable from final state alone."""
    model = world_root / manifest.get("loader_defect", {}).get(
        "model", "dbt_project/models/warehouse/fct_orders.sql")
    if not model.exists():
        s.add("D7", 0, 2, f"loader model not found at {model}")
        return
    src = model.read_text(encoding="utf-8", errors="replace").lower()
    # [\w+]+ because the strategy name "delete+insert" contains a '+': \w+ captured
    # "delete", so the branch naming it was dead code and a CORRECT idempotent fix
    # scored 1/2 (F621).
    strategy = re.search(r"incremental_strategy\s*=\s*['\"]([\w+]+)['\"]", src)
    # A substring test for "unique_key" is satisfied by `unique_key=none`, which is
    # exactly the non-idempotent case: merge with no key DUPLICATES on re-run and
    # was scoring 2/2. Require a key that names something.
    key_m = re.search(r"unique_key\s*=\s*(\[[^\]]*\]|['\"][^'\"]*['\"]|\w+)", src)
    key_val = (key_m.group(1).strip() if key_m else "")
    has_key = bool(key_val) and key_val not in ("none", "null", "[]", "''", '""')
    strat = strategy.group(1) if strategy else None
    # insert_overwrite is NOT accepted here: dbt-duckdb rejects it outright
    # ("not valid for this adapter"), so crediting it rewards a fix that cannot run.
    if strat in ("merge", "delete+insert") and has_key:
        s.add("D7", 2, 2, f"loader strategy '{strat}' with unique_key {key_val}")
    elif strat == "append":
        s.add("D7", 0, 2, "loader still incremental_strategy='append' - the next "
                          "retry re-corrupts regardless of the data repair")
    else:
        s.add("D7", 1, 2, f"loader strategy '{strat}', unique_key={has_key} - unclear")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True)
    ap.add_argument("--grading", required=True)
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--world-root", default=None,
                    help="agent workspace root, for the static loader check")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    grading = Path(a.grading)
    manifest = load_manifest(grading / "manifest.yaml")
    try:
        artifact = json.loads(Path(a.artifact).read_text(encoding="utf-8"))
    except Exception as e:
        artifact = {}
        print(f"warning: final artifact unreadable ({e}) - narrative checks score 0",
              file=sys.stderr)

    con = connect(Path(a.world), grading)
    s = Score()
    d1_window_diff(con, s)
    d2_duplicates(con, s)
    d3_manual_adjustments(con, s, manifest)
    d4_revenue_untouched(con, s)
    d5_artifact(artifact, manifest, s)
    d6_secondary_and_false_causes(artifact, manifest, s)
    if a.world_root:
        d7_loader_fixed(Path(a.world_root), s, manifest)
    con.close()

    result = {"task": "GT001-aov-halving", "total": s.total, "max": s.maximum,
              "checks": s.checks,
              "note": "official score is deterministic; judge runs in shadow mode"}
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        for c in s.checks:
            print(f"  {c['id']:<5} {c['points']:>5} / {c['max']:<3} {c['detail']}")
        print(f"\n  TOTAL {s.total} / {s.maximum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
