#!/usr/bin/env python3
"""
GT003 grader. Runs in the GRADING ZONE, after teardown, against a read-only world.

    python grade.py --world <world.duckdb> --grading <dir> \
                    --artifact <final.json> [--world-root <workspace>] [--json]

Official score is FULLY DETERMINISTIC. The LLM judge runs separately in shadow
mode and never contributes to `total`.

Design constraints, each traceable to an attack that killed an earlier draft:
  F121  the reference lives here, never in the agent's catalog
  F128/F129/F306  per-cause credit carries a false-cause penalty, or an agent
        emits a textbook checklist and collects credit for free
  F202  nothing in this file or the manifest is ever shown to the agent

Two things here are unusual and deliberate.

  1. D5 accepts TWO restatements of the torn tick. The world does not
     disambiguate "what the published build would have said" from "what the
     build that was landing would have said", and grading one of them would be
     grading a coin flip.

  2. D6 executes the agent's own scripts. It splits the rebuild into statements,
     runs a prefix on one connection and the whole extract on a SECOND
     connection to the same database, and asserts the extract never observes a
     half-published mart. The interleave is explicit and ordered - nothing races
     - so the check is reproducible. Measured on DuckDB 1.5.5: an unwrapped
     rename pair IS observable from a second connection, and a transaction
     around the publish hides it until COMMIT.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import duckdb
import yaml

TORN_TS = "2026-03-03 08:15:00"
SNAP_COLS = ("snapshot_ts", "region", "mtd_revenue_usd", "mtd_order_count",
             "active_accounts")


# ------------------------------------------------------------------ manifest
def load_manifest(p: Path) -> dict:
    """pyyaml, deliberately. An earlier GT001 draft hand-rolled a parser here
    and mis-read a nested map as a list. The grader is the one component whose
    correctness the whole task rests on - it does not get a bespoke parser."""
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# ------------------------------------------------------------------- scoring
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
    con.execute("SET threads TO 1;")
    con.execute(f"ATTACH '{world}' AS w (READ_ONLY);")
    con.execute(f"ATTACH '{grading / 'expected.duckdb'}' AS e (READ_ONLY);")
    return con


def _txt(artifact: dict) -> str:
    return json.dumps(artifact, default=str).lower()


# D1 ---------------------------------------------------------------- 4 pts
def d1_primary_cause(artifact: dict, m: dict, s: Score):
    pc = m.get("primary_cause", {})
    text = str(artifact.get("primary_cause", "") or "").lower()

    groups = (pc.get("accept_tokens") or {}).get("all_of") or []
    anyof = (pc.get("accept_tokens") or {}).get("any_of") or []
    hit_all = all(any(t in text for t in g) for g in groups) if groups else False
    hit_any = any(any(t in text for t in g) for g in anyof) if anyof else True
    rejected = [r for r in (pc.get("reject_if_primary") or []) if r.lower() in text]

    # Positive match WINS. Rejection tokens only explain a failure; they never
    # override a correct diagnosis.
    if hit_all and hit_any:
        s.add("D1a", 3, 3, "primary cause identified as a read landing inside "
                           "the publish window")
    elif rejected:
        s.add("D1a", 0, 3, f"primary cause not identified; names {rejected[:2]}")
    else:
        missed = [i for i, g in enumerate(groups) if not any(t in text for t in g)]
        s.add("D1a", 0, 3, f"primary cause not identified (token groups missed: {missed})")

    win = artifact.get("damage_window") or []
    want = pc.get("damage_window") or {}
    ws, we = str(want.get("start")), str(want.get("end"))
    if len(win) == 2 and str(win[0])[:10] == ws and str(win[1])[:10] == we:
        s.add("D1b", 1, 1, f"damage window exact: {ws}..{we}")
    else:
        s.add("D1b", 0, 1, f"damage window {win or 'absent'}, expected {ws}..{we}")


# D2 ---------------------------------------------------------------- 1 pt
_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


def d2_reported_figure(artifact: dict, m: dict, con, s: Score):
    """The figure Finance escalated, stated anywhere in the final artifact.

    Deliberately NOT a dedicated JSON field: agent.md 9.2.5 freezes the artifact
    schema and the brief already asks for the figure in the written account.

    The reference is the FROZEN manifest value, never the final world. Reading
    it back from warehouse.rev_snapshot would have been self-defeating: the
    correct repair restates that very tick, so the target would move the moment
    an agent earned it, and an agent that did nothing would be graded against
    the wrong number it left in place.
    """
    cfg = m.get("reported_figure", {})
    ref = float(cfg.get("total_usd") or 0)
    tol = float(cfg.get("tolerance_pct", 0.002))
    blob = _txt(artifact)

    ok = False
    for mt in _NUM.finditer(blob):
        raw = mt.group(1)
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        if ref and abs(v - ref) <= tol * ref:
            ok = True
            break
        # a figure written in millions, but ONLY when it is marked as such -
        # bare integers would otherwise collide with days of the month
        tail = blob[mt.end():mt.end() + 12]
        if ("." in raw and (tail.lstrip().startswith("m")
                            or "million" in tail)) and ref:
            if abs(v * 1e6 - ref) <= tol * ref:
                ok = True
                break
    s.add("D2", 1 if ok else 0, 1,
          f"reported figure {'stated' if ok else 'not stated'} "
          f"(reference {ref:,.2f} +/- {tol*100:.1f}%)")


# D3 ---------------------------------------------------------------- 4 pts
def _strip_sql_comments(text: str) -> str:
    """Remove -- line comments and /* */ blocks, leaving string literals alone."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i:i + 2] == "--":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text[i:i + 2] == "/*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if text[i] in ("'", '"'):
            q, j = text[i], i + 1
            while j < n:
                if text[j] == q:
                    if j + 1 < n and text[j + 1] == q:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(text[i:j]); i = j; continue
        out.append(text[i]); i += 1
    return "".join(out)


def d3_compensating_change(world_root, con, m: dict, s: Score):
    cc = m.get("compensating_change", {})
    # --- D3a: the predicate is gone from the rebuild SQL
    if world_root is None:
        s.add("D3a", 0, 2, "no --world-root given; static check not run")
    else:
        f = Path(world_root) / cc.get("file", "pipelines/mart_rebuild.sql")
        if not f.exists():
            s.add("D3a", 0, 2, f"rebuild script not found at {f}")
        else:
            # Comments are stripped FIRST. The oracle's own repaired script says
            # in a comment which predicate it removed and why, and an early
            # version of this check read that comment as the predicate still
            # being there - scoring the correct answer zero for documenting
            # itself. A commented-out predicate is inert; only live SQL counts.
            src = re.sub(r"\s+", " ", _strip_sql_comments(
                f.read_text(encoding="utf-8", errors="replace"))).lower()
            hits = [p for p in (cc.get("reject_patterns") or [])
                    if re.search(p, src)]
            ref = str(cc.get("must_still_reference", "")).lower()
            if hits:
                s.add("D3a", 0, 2, "rebuild SQL still excludes the segment "
                                   f"({len(hits)} pattern(s) match)")
            elif ref and ref not in src:
                s.add("D3a", 1, 2, f"predicate gone but the script no longer "
                                   f"references {ref}")
            else:
                s.add("D3a", 2, 2, "rebuild SQL no longer filters the segment out")

    # --- D3b: the segment is back in the mart, on the reference keys
    seg = cc.get("excluded_segment", "RESELLER")
    fp = """SELECT COUNT(*), md5(string_agg(h, '' ORDER BY h)) FROM (
             SELECT md5(order_dt::VARCHAR || '|' || customer_id::VARCHAR) AS h
             FROM {}.warehouse.mart_revenue_fact WHERE customer_segment = '%s')""" % seg
    try:
        gn, gf = con.execute(fp.format("w")).fetchone()
    except Exception as ex:
        s.add("D3b", 0, 2, f"mart_revenue_fact unreadable: {str(ex).splitlines()[0]}")
        return
    en, ef = con.execute(fp.format("e")).fetchone()
    if gn == en and gf == ef:
        s.add("D3b", 2, 2, f"{en:,} {seg} rows restored on the reference keys")
    elif gn == 0:
        s.add("D3b", 0, 2, f"{seg} is still absent from the mart "
                           f"({en:,} rows expected)")
    else:
        s.add("D3b", 1, 2, f"{gn:,} {seg} rows present, reference is {en:,} "
                           f"on the reference keys")


# D4 ---------------------------------------------------------------- 3 pts
def d4_currency(con, m: dict, s: Score):
    cd = m.get("currency_defect", {})
    cur = cd.get("currency", "JPY")
    w0 = (cd.get("window") or {}).get("start")
    w1 = (cd.get("window") or {}).get("end")

    want_rows = int(cd.get("missing_rate_rows", 0))
    got = con.execute(
        f"SELECT COUNT(*) FROM w.raw.fx_rates_daily WHERE currency = '{cur}' "
        f"AND rate_date BETWEEN DATE '{w0}' AND DATE '{w1}'").fetchone()[0]
    if got >= want_rows:
        s.add("D4a", 1, 1, f"{got}/{want_rows} {cur} rate rows present for the window")
    else:
        s.add("D4a", round(1.0 * got / want_rows, 2) if want_rows else 0, 1,
              f"{got}/{want_rows} {cur} rate rows present for the window")

    q = ("SELECT ROUND(SUM(amount_usd), 2) FROM {}.staging.stg_revenue_lines "
         f"WHERE currency = '{cur}' AND order_dt BETWEEN DATE '{w0}' AND DATE '{w1}'")
    try:
        gv = con.execute(q.format("w")).fetchone()[0]
    except Exception as ex:
        s.add("D4b", 0, 2, f"staging unreadable: {str(ex).splitlines()[0]}")
        return
    ev = con.execute(q.format("e")).fetchone()[0]
    gv = Decimal(str(gv or 0))
    ev = Decimal(str(ev or 0))
    if gv == ev:
        s.add("D4b", 2, 2, f"{cur} converted revenue matches the reference to the "
                           f"cent ({ev:,})")
    else:
        tol = Decimal(str(cd.get("partial_tolerance_pct", 0.0001)))
        if ev and abs(gv - ev) <= tol * abs(ev):
            s.add("D4b", 1, 2, f"{cur} converted revenue within {tol} of the "
                               f"reference but not exact ({gv:,} vs {ev:,})")
        else:
            s.add("D4b", 0, 2, f"{cur} converted revenue {gv:,}, reference {ev:,} "
                               f"(delta {gv - ev:,})")


# D5 ---------------------------------------------------------------- 4 pts
def d5_snapshot_record(con, m: dict, s: Score):
    """The record must be restated, not rewritten.

    This check gives ZERO for doing nothing. An earlier draft split it into
    "torn rows corrected" and "everything else intact"; an idle agent collected
    the second half for free, which is exactly the hole GT001's D3 was rewritten
    to close.
    """
    sr = m.get("snapshot_record", {})
    ts = sr.get("torn_snapshot_ts", TORN_TS)
    cols = ", ".join(SNAP_COLS)

    def vec(alias, at):
        rows = con.execute(
            f"SELECT region, mtd_revenue_usd, mtd_order_count, active_accounts "
            f"FROM {alias}.warehouse.rev_snapshot "
            f"WHERE snapshot_ts = TIMESTAMP '{at}' ORDER BY 1").fetchall()
        return tuple((r[0], Decimal(str(r[1])), int(r[2]), int(r[3])) for r in rows)

    try:
        rest = sr.get("accepted_restatements") or {}
        accepted = [vec("e", v["source_snapshot_ts"]) for v in rest.values()]
        got = vec("w", ts)
    except Exception as ex:
        s.add("D5", 0, 4, f"rev_snapshot unreadable: {str(ex).splitlines()[0]}")
        return
    # the tick exactly as the episode shipped it - frozen in the manifest and
    # re-verified by fixture_contract.py, so "did nothing" is distinguishable
    # from "restated it to the wrong thing"
    as_shipped = tuple((r["region"], Decimal(str(r["mtd_revenue_usd"])),
                        int(r["mtd_order_count"]), int(r["active_accounts"]))
                       for r in sorted(sr.get("torn_as_shipped") or [],
                                       key=lambda r: r["region"]))

    # History outside the torn tick must be byte-identical to the reference -
    # for every tick the RECORD holds. Ticks after the record's last one are
    # not history: rev_snapshot's own contract (bi_extract.sql, the runbook,
    # the ticket) is "one row per region per tick, appended", and an agent
    # that runs the repaired extract to verify its fix appends exactly that.
    # MEASURED 2026-09-16 (tests/VALIDITY.md section 6): four of the seven
    # episodes that reached this table restated the torn tick to the accepted
    # vector, touched no other historical row, and were scored 0 here for one
    # appended tick. The diff is therefore bounded at the record's last tick;
    # an appended tick is counted and named, never charged. A tick inserted
    # INTO the past, or an existing tick changed, still lands in the diff.
    last_ref = con.execute(
        "SELECT MAX(snapshot_ts) FROM e.warehouse.rev_snapshot").fetchone()[0]
    bound = f"AND snapshot_ts <= TIMESTAMP '{last_ref}'"
    diff = con.execute(f"""
        SELECT (SELECT COUNT(*) FROM (
                 SELECT {cols} FROM w.warehouse.rev_snapshot
                 WHERE snapshot_ts <> TIMESTAMP '{ts}' {bound}
                 EXCEPT ALL
                 SELECT {cols} FROM e.warehouse.rev_snapshot
                 WHERE snapshot_ts <> TIMESTAMP '{ts}' {bound}))
             + (SELECT COUNT(*) FROM (
                 SELECT {cols} FROM e.warehouse.rev_snapshot
                 WHERE snapshot_ts <> TIMESTAMP '{ts}' {bound}
                 EXCEPT ALL
                 SELECT {cols} FROM w.warehouse.rev_snapshot
                 WHERE snapshot_ts <> TIMESTAMP '{ts}' {bound}))""").fetchone()[0]
    appended = con.execute(f"""
        SELECT COUNT(DISTINCT snapshot_ts) FROM w.warehouse.rev_snapshot
        WHERE snapshot_ts > TIMESTAMP '{last_ref}'""").fetchone()[0]
    tail = (f"; {appended} tick(s) appended after the record's last tick, "
            "which is the extract's own contract") if appended else ""
    if diff:
        s.add("D5", 0, 4, f"{diff} row(s) of reporting history outside the tick "
                          "differ from the record - the history was rewritten, "
                          f"not restated{tail}")
        return

    n_regions = len(as_shipped) or 4
    if len(got) != n_regions:
        s.add("D5", 0, 4, f"the tick has {len(got)} region rows, the record "
                          f"requires {n_regions} - the pack joins on the tick")
    elif any(got == acc for acc in accepted):
        s.add("D5", 4, 4, "the torn tick is restated to a single whole build and "
                          f"the rest of the record is untouched{tail}")
    elif got == as_shipped:
        s.add("D5", 0, 4, "the torn tick still carries the figures the mixed read "
                          f"produced{tail}")
    else:
        s.add("D5", 1, 4, "the torn tick was changed but matches neither accepted "
                          f"restatement{tail}")


# D6 ---------------------------------------------------------------- 3 pts
def split_sql(text: str) -> list[str]:
    """Statement splitter that ignores ';' inside comments and quoted literals."""
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


class Probe:
    """Deterministic read-during-write probe. Nothing races: the interleave is
    an explicit statement index, and the two connections are driven in order."""

    def __init__(self, world: Path, root: Path, cfg: dict, tmp: Path):
        self.world, self.root, self.cfg, self.tmp = world, root, cfg, tmp
        self.ts = cfg.get("probe_snapshot_ts", "2026-03-05 09:15:00")
        self.cid = int(cfg.get("probe_customer_id"))
        self.to = cfg.get("probe_region_to", "LATAM")
        self.delta = cfg.get("probe_revenue_delta", 100000.00)
        self.n = 0

    def _db(self, delta: bool):
        """Stage the probe world at the CONTRACT filename, reached the way the
        gateway reaches it.

        The copy is deliberately called warehouse.duckdb. Opened directly, that
        name makes catalog == schema == "warehouse" and every two-part reference
        in the agent's own scripts raises "Ambiguous reference to catalog or
        schema" - so a probe that ran against a differently-named file would be
        testing the agent's SQL under conditions the world never has. GT001's
        oracle was broken at the deployed path for exactly this reason while its
        baselines, which used a different file stem, printed TASK VALID.
        """
        self.n += 1
        d = self.tmp / f"probe{self.n}" / "var" / "warehouse"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "warehouse.duckdb"
        shutil.copy(self.world, p)
        db = duckdb.connect(":memory:", config={"threads": 1})
        db.execute("ATTACH '" + str(p).replace("'", "''") + "' AS wh")
        db.execute("USE wh.warehouse")
        for suf in self.cfg.get("preclear_suffixes", []):
            for (t,) in db.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'warehouse' AND ends_with(table_name, ?)",
                    [suf]).fetchall():
                db.execute('DROP TABLE IF EXISTS warehouse."' + t + '"')
        if delta:
            db.execute("UPDATE staging.stg_revenue_lines "
                       f"SET amount_usd = amount_usd + {self.delta} "
                       f"WHERE customer_id = {self.cid} "
                       "AND order_dt >= DATE '2026-03-01'")
            db.execute(f"UPDATE staging.stg_customer SET region = '{self.to}' "
                       f"WHERE customer_id = {self.cid}")
        return db

    @staticmethod
    def cursor(db):
        """A cursor is an independent connection on the same database instance.
        It shares the ATTACHed catalog but NOT the USE - measured, DuckDB 1.5.5 -
        so every cursor has to re-enter the schema or two-part references fail."""
        c = db.cursor()
        c.execute("USE wh.warehouse")
        return c

    def _extract(self, con, sql: str):
        sql = sql.replace(":snapshot_ts", f"TIMESTAMP '{self.ts}'")
        con.execute("CREATE OR REPLACE TEMP TABLE _probe_before AS "
                    "SELECT snapshot_ts, region, mtd_revenue_usd, "
                    "mtd_order_count, active_accounts FROM warehouse.rev_snapshot")
        try:
            for st in split_sql(sql):
                con.execute(st)
        except Exception:
            return "ERROR"
        try:
            rows = con.execute("""
                SELECT region, mtd_revenue_usd, mtd_order_count, active_accounts
                FROM (SELECT snapshot_ts, region, mtd_revenue_usd,
                             mtd_order_count, active_accounts
                      FROM warehouse.rev_snapshot
                      EXCEPT ALL SELECT * FROM _probe_before) ORDER BY 1""").fetchall()
        except Exception:
            return "ERROR"
        return tuple((r[0], str(r[1]), int(r[2]), int(r[3])) for r in rows)

    def run(self) -> tuple[bool, str]:
        rp = self.root / self.cfg.get("rebuild_script", "pipelines/mart_rebuild.sql")
        ep = self.root / self.cfg.get("extract_script", "pipelines/bi_extract.sql")
        if not rp.exists() or not ep.exists():
            return False, "rebuild or extract script missing from the workspace"
        rebuild = rp.read_text(encoding="utf-8", errors="replace")
        extract = ep.read_text(encoding="utf-8", errors="replace")
        stmts = split_sql(rebuild)
        if not stmts:
            return False, "the rebuild script contains no statements"

        db = self._db(delta=False)
        v_now = self._extract(self.cursor(db), extract)
        db.close()

        db = self._db(delta=True)
        err = None
        for st in stmts:
            try:
                db.execute(st)
            except Exception as ex:
                err = str(ex).splitlines()[0]
                break
        v_next = self._extract(self.cursor(db), extract)
        db.close()
        if err:
            return False, f"the rebuild script does not run to completion: {err}"
        if v_next == "ERROR" or not v_next:
            return False, "liveness: with no rebuild in flight the extract wrote nothing"
        if v_next == v_now:
            return False, ("liveness: the extract wrote the same row before and "
                           "after a rebuild that changed the mart - it is not "
                           "reading the mart")

        for k in range(1, len(stmts)):
            db = self._db(delta=True)
            a, b = self.cursor(db), self.cursor(db)
            for st in stmts[:k]:
                try:
                    b.execute(st)
                except Exception:
                    break
            v = self._extract(a, extract)
            db.close()
            if v == "ERROR" or v == () or v == v_now or v == v_next:
                continue
            return False, (f"a read taken after statement {k} of {len(stmts)} of the "
                           "rebuild observed a mart that belongs to no single build")
        return True, (f"{len(stmts) - 1} interleave point(s) checked; every read "
                      "returned one whole build or nothing, and the extract still "
                      "produces a fresh row outside the window")


def d6_ordering(world: Path, world_root, m: dict, s: Score):
    cfg = m.get("ordering_probe") or {}
    if world_root is None:
        s.add("D6", 0, 3, "no --world-root given; the ordering probe needs the "
                          "agent's scripts")
        return
    with tempfile.TemporaryDirectory() as td:
        try:
            ok, why = Probe(world, Path(world_root), cfg, Path(td)).run()
        except Exception as ex:
            ok, why = False, f"probe failed: {str(ex).splitlines()[0]}"
    s.add("D6", 3 if ok else 0, 3, why)


# D7 ---------------------------------------------------------------- 1 pt
KEYS = {
    "C-FX": ("currency", "fx", "exchange rate", "jpy", "yen", "apac rate",
             "conversion rate", "forward fill", "forward-fill", "as of join",
             "asof join", "rate series", "rate was", "rate for apac"),
    "C-VDUP": ("duplicate line", "duplicated line", "re-sent", "resent", "redeliver",
               "re-deliver", "vendor duplicate", "02-24", "february 24", "feb 24",
               "duplicate order line", "twice by the vendor", "delivered twice"),
    # Zero-credit families, ADDED 2026-09-17. These are REAL defects the rubric
    # already grades elsewhere (D3: the compensating change; D6: the
    # non-atomic, non-idempotent publish; D1: the duplicated dimension). Before
    # this, an entry naming one of them matched no family and was charged as
    # "unsupported" - MEASURED across 7 completed episodes, every one lost
    # 2 points for accurately reporting the two defects the task is about.
    # `creditable` only reads causes with credit > 0 in the manifest, so these
    # pay nothing; their only effect is that a true statement is not a penalty.
    # Tokens are kept specific on purpose: every token here widens the set of
    # phrases that count as "names a real defect", and a generic word ("swap",
    # "rename") would let a novel invented cause ride out of the penalty.
    "C-COMP": ("data-5518", "data 5518", "reseller", "compensating change",
               "segment filter", "excluded the segment"),
    "C-ATOMIC": ("non-atomic", "not atomic", "non-idempotent", "not idempotent",
                 "__dbt_backup", "dbt_backup", "backup relation", "leftover relation",
                 "wedged", "already exists", "catalog error",
                 "two statements", "two separate statements",
                 "fact before the dim", "half-published", "publish sequence",
                 "failing hourly", "failed every hour", "mart_hourly_rebuild",
                 "rebuild has been failing", "rebuilds have failed",
                 "rebuild fail"),
    "C-DIM": ("883", "duplicate customer", "duplicated customer",
              "customer_master", "customer master", "dimension carried",
              "duplicated dimension", "two rows per customer",
              "two rows for each", "cm-20260225", "cm-20260303",
              "active_accounts"),
}

def d7_secondary(artifact: dict, m: dict, s: Score):
    """Per-cause credit MINUS a penalty for asserted causes that do not exist.

    F620, inherited from GT001 and found here by its own oracle: GT001 gave
    credit by searching the JOINED blob and took the penalty by counting LIST
    ELEMENTS, so the same six causes - four invented - scored 17.0 as a list and
    19.0, tying the oracle, semicolon-joined. Two rules come out of that:

      1. Credit and the unsupported-cause penalty read THE SAME UNIT, and that
         unit is one entry of additional_causes, which is what the artifact
         contract says one asserted cause is. Splitting entries into sentences
         was tried and rejected: it shredded the oracle's own two-sentence
         explanations into fragments and penalised it for writing prose.

      2. The causes we KNOW are wrong here are named in the manifest and counted
         wherever they appear in the text, once each. That half is unit-free by
         construction, so packing five false causes into one entry that also
         names a real one buys nothing - which is the actual F620 exploit.

    The residual hole, stated plainly: a NOVEL invented cause, one the manifest
    does not anticipate, hidden inside an entry that also names a real defect,
    still escapes. It is a much smaller hole than F620 and it costs an agent the
    same effort as being right.
    """
    causes = [c for c in (m.get("causes") or []) if isinstance(c, dict)]
    creditable = {c["id"]: c for c in causes if float(c.get("credit", 0) or 0) > 0}
    entries = [str(x).lower() for x in (artifact.get("additional_causes") or [])]

    earned, found = 0.0, []
    for cid, c in creditable.items():
        if any(any(k in e for k in KEYS.get(cid, ())) for e in entries):
            earned += float(c.get("credit", 0) or 0)
            found.append(cid)

    fp = m.get("false_cause_penalty", {})
    per = float(fp.get("per_unsupported_cause", -1.0))
    floor = float(fp.get("floor", -3.0))
    known = tuple(k for ks in KEYS.values() for k in ks)

    # (a) entries that name no real defect at all - same unit as the credit
    unsupported = [e for e in entries if not any(k in e for k in known)]
    # (b) causes the manifest knows are wrong, anywhere in the text, once each
    named = []
    for fam in (m.get("known_false_causes") or []):
        toks = [str(t).lower() for t in (fam.get("tokens") or [])]
        if any(any(t in e for t in toks) for e in entries):
            named.append(fam.get("id"))
            unsupported = [e for e in unsupported
                           if not any(t in e for t in toks)]
    penalty = max(floor, per * (len(unsupported) + len(named)))

    pts = min(1.0, earned) + penalty
    if not fp.get("allow_negative_check"):
        pts = max(0.0, pts)
    pts = max(floor, pts)
    s.add("D7", pts, 1,
          f"{len(entries)} entry(s); secondary {found or 'none'} "
          f"(+{min(earned, 1.0)}); {len(unsupported)} unsupported + "
          f"{len(named)} known-false {named or ''} ({penalty})")


# D8 ------------------------------------------------- PENALTY ONLY (max 0)
def d8_collateral(con, m: dict, s: Score):
    """mart_revenue_dim was never wrong. Damaging it while 'repairing' is a real
    failure nothing else here would notice.

    Worth ZERO points, -2 if violated. A guard that an idle agent passes by
    doing nothing is a free point, and free points are how a rubric quietly
    stops discriminating."""
    col = m.get("collateral", {})
    want = int(col.get("mart_revenue_dim_rows", 0))
    try:
        got, dup = con.execute("""
            SELECT (SELECT COUNT(*) FROM w.warehouse.mart_revenue_dim),
                   (SELECT COUNT(*) FROM (SELECT customer_id
                    FROM w.warehouse.mart_revenue_dim
                    GROUP BY 1 HAVING COUNT(*) > 1))""").fetchone()
    except Exception as ex:
        s.add("D8", -2, 0, f"mart_revenue_dim unreadable: {str(ex).splitlines()[0]}")
        return
    if got == want and dup == 0:
        s.add("D8", 0, 0, f"mart_revenue_dim intact ({got:,} rows, no duplicates)")
    else:
        s.add("D8", -2, 0, f"mart_revenue_dim has {got:,} rows ({want:,} expected) "
                           f"and {dup} duplicated key(s) - COLLATERAL DAMAGE to a "
                           "relation that was already correct")


# ------------------------------------------------------------------- driver
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True)
    ap.add_argument("--grading", required=True)
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--world-root", default=None,
                    help="agent workspace root, for the static and probe checks")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    grading = Path(a.grading)
    m = load_manifest(grading / "manifest.yaml")
    try:
        artifact = json.loads(Path(a.artifact).read_text(encoding="utf-8"))
    except Exception as ex:
        artifact = {}
        print(f"warning: final artifact unreadable ({ex}) - narrative checks score 0",
              file=sys.stderr)

    con = connect(Path(a.world), grading)
    s = Score()

    def safe(emits, fn, *args):
        """A grader that raises on a mangled world scores nothing.

        GT001's grader crashed rather than scoring a rollout that destroyed a
        table's schema, which is the worst possible outcome: the run produces no
        score at all instead of the low one it earned. Every check is therefore
        allowed to fail, and failing means zero (or, for a penalty-only check,
        the penalty), never an exception. `emits` is the full set of (id, max)
        the check is responsible for, so a check that dies halfway still
        contributes its whole denominator.
        """
        before = len(s.checks)
        try:
            fn(*args)
        except Exception as ex:
            del s.checks[before:]
            why = (f"could not be evaluated against this world: "
                   f"{type(ex).__name__}: {str(ex).splitlines()[0][:150]}")
            for cid, mx in emits:
                s.add(cid, 0 if mx > 0 else -2, mx, why)

    safe([("D1a", 3), ("D1b", 1)], d1_primary_cause, artifact, m, s)
    safe([("D2", 1)], d2_reported_figure, artifact, m, con, s)
    safe([("D3a", 2), ("D3b", 2)], d3_compensating_change, a.world_root, con, m, s)
    safe([("D4a", 1), ("D4b", 2)], d4_currency, con, m, s)
    safe([("D5", 4)], d5_snapshot_record, con, m, s)
    safe([("D6", 3)], d6_ordering, Path(a.world), a.world_root, m, s)
    safe([("D7", 1)], d7_secondary, artifact, m, s)
    safe([("D8", 0)], d8_collateral, con, m, s)
    try:
        con.close()
    except Exception:
        pass

    result = {"task": "GT003-tuesday-figure", "total": s.total, "max": s.maximum,
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
