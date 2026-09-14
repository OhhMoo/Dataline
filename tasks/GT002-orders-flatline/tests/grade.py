#!/usr/bin/env python3
"""
GT002 grader. Runs in the GRADING ZONE, after teardown, against a read-only world.

    python grade.py --world <world.duckdb> --grading <dir> \
                    --artifact <final.json> --world-root <workspace> [--json]

Official score is FULLY DETERMINISTIC. The LLM judge runs separately in shadow
mode and never contributes to `total`.

THE GRADING REFERENCE IS HARNESS-SIDE (ground rule 1, F121). `expected.duckdb`
and `harness_truth.json` live in the grading zone, which is never mounted inside
the episode. Everything the conservation check compares against - the produced
message count, the decodable-record manifest, the dead-letter offset set, the
legitimate-revision manifest, the pre-incident partition fingerprints - is read
from there, NOT from the agent's own catalog. An agent that deletes the poison
records from its own copy of the topic to make its books balance still fails,
because `produced` is counted on the harness side.

Design constraints, each traceable to an attack that killed an earlier draft:
  F121  reference harness-side; the idempotency probe plants records the agent
        never saw, so a permanently-skipping sink scores zero rather than full
  F125  the decoy feeds a table the KPI view actually joins, so a lineage lookup
        does not dismiss it; the failing micro-batch logs "0 records" and nothing
        else; the gap length is not stated anywhere the agent can read
  F128/F129/F306  per-cause credit carries a false-cause penalty
  GT001 lesson  no check may be satisfiable by doing nothing. K3 ("no duplicate
        business keys") is TRUE in the untouched world, so it is gated on having
        actually loaded the gap - otherwise it is three free points for an idle
        agent, which is how a rubric quietly stops discriminating.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

import duckdb
import yaml

from warehouse_conn import deployed_copy, open_warehouse

# audit columns: legitimately different after any repair, excluded from every diff
EXCLUDE = ("load_ts", "batch_id")
BUSINESS = ("order_id", "revision", "order_dt", "event_ts", "customer_id",
            "channel_code", "sku", "qty", "unit_price", "order_amount")

# the decode the sink is supposed to perform, expressed once
DECODE = {
    "order_id": "TRY_CAST(json_extract_string(payload,'$.order_id') AS BIGINT)",
    "revision": "TRY_CAST(json_extract_string(payload,'$.revision') AS INTEGER)",
    "order_dt": "TRY_CAST(json_extract_string(payload,'$.order_dt') AS DATE)",
    "event_ts": "TRY_CAST(json_extract_string(payload,'$.event_ts') AS TIMESTAMP)",
    "customer_id": "TRY_CAST(json_extract_string(payload,'$.customer_id') AS BIGINT)",
    "channel_code": "json_extract_string(payload,'$.channel_code')",
    "sku": "json_extract_string(payload,'$.sku')",
    "qty": "TRY_CAST(json_extract_string(payload,'$.qty') AS INTEGER)",
    "unit_price": "TRY_CAST(json_extract_string(payload,'$.unit_price') AS DECIMAL(10,2))",
    "order_amount": ("ROUND(TRY_CAST(json_extract_string(payload,'$.qty') AS INTEGER) * "
                     "TRY_CAST(json_extract_string(payload,'$.unit_price') AS DECIMAL(10,2))"
                     ", 2)::DECIMAL(12,2)"),
}


def load_manifest(p: Path) -> dict:
    """pyyaml, deliberately. A hand-rolled parser in an earlier grader mis-read a
    nested map as a list and silently corrupted scoring. The grader is the one
    component the whole task rests on - it does not get a bespoke parser."""
    return yaml.safe_load(p.read_text(encoding="utf-8"))


class Score:
    def __init__(self):
        self.checks: list[dict] = []

    def add(self, cid, points, mx, detail):
        self.checks.append({"id": cid, "points": round(float(points), 2), "max": mx,
                            "passed": points >= mx, "detail": detail})

    @property
    def total(self):
        return round(sum(c["points"] for c in self.checks), 2)

    @property
    def maximum(self):
        return sum(c["max"] for c in self.checks)


def connect(world: Path, grading: Path):
    """Never opens the warehouse file directly - see warehouse_conn.py. Both
    databases are attached under aliases and every reference below is three-part
    (w.warehouse.x / e.raw.y), so nothing here depends on a search path or on
    what the world file happens to be called."""
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{world.as_posix()}' AS w (READ_ONLY);")
    con.execute(f"ATTACH '{(grading / 'expected.duckdb').as_posix()}' AS e (READ_ONLY);")
    return con


def gap_manifest(con, c0: int) -> None:
    """Every business row the gap SHOULD produce, decoded from the HARNESS-SIDE
    copy of the topic. This is the reference the conservation check counts
    against; the agent's own topic is never consulted."""
    cols = ", ".join(f"{expr} AS {name}" for name, expr in DECODE.items())
    con.execute(f"""
      CREATE TEMP TABLE gapman AS
      SELECT {cols}
      FROM e.raw.orders_topic
      WHERE offset_id >= {c0}
        AND json_extract_string(payload,'$.order_id') IS NOT NULL
        AND TRY_CAST(json_extract_string(payload,'$.qty') AS INTEGER) IS NOT NULL
        AND TRY_CAST(json_extract_string(payload,'$.unit_price') AS DECIMAL(10,2)) IS NOT NULL""")


def loaded_and_dlq(con, c0: int) -> tuple[int, int]:
    """PHYSICAL rows - not distinct keys. A duplicated row must count twice, or a
    non-idempotent replay balances the ledger it just broke."""
    join = " AND ".join(f"f.{c} IS NOT DISTINCT FROM g.{c}" for c in BUSINESS)
    loaded = con.execute(
        f"SELECT COUNT(*) FROM w.warehouse.fct_orders f JOIN gapman g ON {join}").fetchone()[0]
    dlq = con.execute(
        f"SELECT COUNT(DISTINCT offset_id) FROM w.raw.orders_dlq "
        f"WHERE offset_id >= {c0}").fetchone()[0]
    return int(loaded), int(dlq)


# K1 ------------------------------------------------- CONJUNCTION -- 8 pts
def k1_conservation(con, s: Score, t: dict, loaded: int, dlq: int) -> bool:
    """MESSAGE CONSERVATION: loaded + dead-lettered == produced.

    Fails LOW if the gap was never replayed. Fails HIGH if the replay went
    through a sink with no merge key. It cannot pass unless BOTH the stall and
    the sink were dealt with, which is the entire point of the task.

    `substantive` closes the degenerate route: dead-lettering the whole gap also
    satisfies the arithmetic. 26 of 17,459 records are genuinely undecodable, so
    anything under produced-200 is not a repair.
    """
    produced = int(t["produced_gap"])
    delta = loaded + dlq - produced
    substantive = loaded >= produced - 200
    d = (f"loaded {loaded:,} + dead-lettered {dlq:,} = {loaded + dlq:,} "
         f"vs produced {produced:,} (delta {delta:+,})")
    if delta == 0 and substantive:
        s.add("K1", 8, 8, d + " - ledger closed")
    elif substantive and 0 < abs(delta) <= 30:
        s.add("K1", 4, 8, d + " - gap recovered but records lost with no trace")
    elif not substantive:
        s.add("K1", 0, 8, d + (" - the gap was never replayed" if loaded < produced // 2
                               else " - the gap was replayed and then rows were "
                                    "removed from it"))
    else:
        s.add("K1", 0, 8, d + (" - the sink duplicated on replay" if delta > 0
                               else " - records missing"))
    return delta == 0 and substantive


# K2 ---------------------------------------------------------------- 2 pts
def k2_lag(con, s: Score, t: dict):
    row = con.execute(
        "SELECT committed_offset FROM w.raw.consumer_offsets "
        "WHERE consumer_group = 'orders_fct_sink'").fetchone()
    if row is None:
        s.add("K2", 0, 2, "consumer group 'orders_fct_sink' no longer has an offset row")
        return
    committed = int(row[0])
    log_end = int(t["log_end_offset"])
    last_poison = max(t["dlq_expected_offsets"])
    lag = log_end - committed
    past = committed > last_poison
    if lag == 0:
        s.add("K2", 2, 2, f"lag 0; committed {committed:,} is past the last "
                          f"undecodable record at {last_poison:,}")
    elif 0 < lag <= 2000:
        s.add("K2", 1, 2, f"lag {lag:,} - one poll window short of caught up")
    elif lag < 0:
        s.add("K2", 0, 2, f"committed offset {committed:,} is beyond the log end "
                          f"{log_end:,} - records skipped that were never read")
    else:
        s.add("K2", 0, 2, f"lag {lag:,}; consumer still stalled"
                          + ("" if past else f" behind the record at {last_poison:,}"))


# K3 ---------------------------------------------------------------- 1 pt
def k3_duplicates(con, s: Score, replayed: bool):
    """Zero duplicate business keys - GATED on having actually loaded the gap.

    Ungated this is a free point: the untouched world has no duplicates at all,
    so an idle agent would bank it for doing nothing. GT001 shipped exactly that
    bug in an early draft (3/3 to a no-op agent) and it is the reason this check
    carries a precondition."""
    n, dt = con.execute("""
        SELECT COUNT(*), COUNT(DISTINCT order_dt) FROM (
          SELECT order_id, revision, order_dt FROM w.warehouse.fct_orders
          GROUP BY 1,2,3 HAVING COUNT(*) > 1)""").fetchone()
    if not replayed:
        s.add("K3", 0, 1, f"{n} duplicate (order_id, revision) pairs, but the gap was "
                          "never replayed - nothing was at risk")
    elif n == 0:
        s.add("K3", 1, 1, "gap loaded and no duplicate (order_id, revision) pairs anywhere")
    elif n <= 50:
        s.add("K3", 0.5, 1, f"{n} duplicate business keys remain across {dt} partitions")
    else:
        s.add("K3", 0, 1, f"{n:,} duplicate business keys across {dt} partitions")


# K4 ------------------------------------------------- PENALTY ONLY (max 0)
def k4_revisions(con, s: Score, t: dict, replayed: bool):
    """The producer legitimately re-emitted these orders with a higher revision
    while the consumer was stalled. Both revisions must survive, each exactly
    once. A blanket de-duplication on order_id - the natural cleanup after
    replaying through a non-idempotent sink - destroys one of the two and cannot
    tell them apart from replay duplicates.

    PENALTY ONLY, and that is F626. The brief tells the agent, in the ticket,
    that "some orders around that date may have been sent more than once with
    corrections. Those corrections are real and Finance needs them." That sentence
    is a genuine business constraint - no query reveals that Finance wants
    revision history preserved rather than collapsed, and withholding it would
    grade the agent on guessing a business rule - so it stays. But paying four
    points for acting on a constraint the ticket states outright is paying twice
    for one piece of information, and message conservation already fails by
    exactly -1,100 when the revisions are collapsed. So this check can only
    subtract now: it is the explicit guard, not a second fee.
    F626 option one; recorded there and in manifest.yaml brief_constraints."""
    want = t["legit_revisions"]
    con.execute("CREATE TEMP TABLE revman (order_id BIGINT, revision INTEGER)")
    con.executemany("INSERT INTO revman VALUES (?, ?)", [(int(a), int(b)) for a, b in want])
    ok = con.execute("""
        SELECT COUNT(*) FROM revman r
        WHERE (SELECT COUNT(*) FROM w.warehouse.fct_orders f
                WHERE f.order_id = r.order_id AND f.revision = r.revision) = 1
          AND (SELECT COUNT(*) FROM w.warehouse.fct_orders f
                WHERE f.order_id = r.order_id AND f.revision = r.revision - 1) = 1
        """).fetchone()[0]
    n = len(want)
    lost = n - ok
    # GATED on having loaded the gap. A penalty must fire for DESTROYING
    # something, never for not having created it: without this an agent that did
    # nothing at all is charged -4 for the absence of rows it was never given.
    penalty = 0.0 if (lost == 0 or not replayed) else max(-4.0, -4.0 * lost / n)
    s.add("K4", round(penalty, 2), 0,
          f"{ok:,}/{n:,} corrected orders carry both the original and the "
          f"correction, exactly once each"
          + ("" if lost == 0 else
             (f" - {lost:,} destroyed, almost certainly by a de-duplication on "
              "order_id alone" if replayed else
              " - the gap was never replayed, so none were destroyed either")))


# K5 ---------------------------------------------------------------- 6 pts
def k5_idempotency_probe(world: Path, world_root: Path, s: Score, t: dict, man: dict):
    """Behavioural, not static. Copies the graded world, re-emits 500 records the
    pipeline has already consumed plus 3 it has never seen, and executes the
    agent's own pipeline files at the paths the DAG is wired to.

    Two ways to fail, and both must fail:
      * the redelivery adds rows          -> the sink is still append-only
      * the new records do not land       -> the sink was "fixed" into a
                                             permanent skip, which is idempotent
                                             by construction and repairs nothing
    """
    quar = world_root / man["pipeline"]["quarantine"]
    sink = world_root / man["pipeline"]["sink"]
    if not sink.exists():
        s.add("K5", 0, 6, f"no sink at {man['pipeline']['sink']} - the DAG step that "
                          "loads this table no longer has a file to execute")
        return
    tmp = Path(tempfile.mkdtemp(prefix="gt002-probe-"))
    try:
        # THE CONTRACT FILENAME. The probe executes the agent's own sink SQL,
        # which addresses warehouse.fct_orders - a two-part reference that is
        # ambiguous when the catalog is also called "warehouse". Copying to
        # probe.duckdb would run the agent's code under a file stem that does not
        # exist in production and would silently grade a different thing.
        probe = deployed_copy(world, tmp)
        con = open_warehouse(probe)
        base = int(con.execute("SELECT MAX(offset_id) + 1 FROM raw.orders_topic").fetchone()[0])
        n_redeliver = int(man["idempotency_probe"]["redelivered_records"])
        n_new = int(man["idempotency_probe"]["planted_records"])

        # 500 exact redeliveries of records the sink has already consumed
        con.execute(f"""
          CREATE TEMP TABLE redeliver AS
          SELECT {base} + (row_number() OVER (ORDER BY offset_id)) - 1 AS new_offset,
                 event_ts, msg_key, payload
          FROM (SELECT * FROM raw.orders_topic
                 WHERE offset_id >= {t['committed_offset_at_start']}
                   AND json_extract_string(payload,'$.order_id') IS NOT NULL
                   AND TRY_CAST(json_extract_string(payload,'$.qty') AS INTEGER) IS NOT NULL
                 ORDER BY offset_id LIMIT {n_redeliver})""")
        con.execute("INSERT INTO raw.orders_topic SELECT new_offset, event_ts, msg_key, "
                    "payload FROM redeliver")
        # records the agent has never seen, so a permanent skip cannot pass
        con.execute(f"""
          INSERT INTO raw.orders_topic
          SELECT {base + n_redeliver} + n - 1,
                 TIMESTAMP '{man['idempotency_probe']['planted_event_ts']}'
                   + INTERVAL (n) MINUTE,
                 (990000000 + n)::VARCHAR,
                 '{{"order_id":' || (990000000 + n)
                 || ',"revision":1,"order_dt":"'
                 || strftime(TIMESTAMP '{man['idempotency_probe']['planted_event_ts']}',
                             '%Y-%m-%d')
                 || '","event_ts":"'
                 || strftime(TIMESTAMP '{man['idempotency_probe']['planted_event_ts']}'
                             + INTERVAL (n) MINUTE, '%Y-%m-%dT%H:%M:%S')
                 || '","customer_id":7,"channel_code":"CH-01","sku":"SKU-0002",'
                 || '"qty":"2","unit_price":"50.00"}}'
          FROM (SELECT UNNEST(generate_series(1, {n_new})) AS n)""")
        total_new = n_redeliver + n_new
        con.execute(f"UPDATE raw.topic_meta SET log_end_offset = {base + total_new} "
                    "WHERE topic = 'orders.v2'")
        # the probe tests the SINK, not whether the agent caught up: point the
        # consumer at the freshly planted window and run exactly one poll.
        con.execute(f"UPDATE raw.consumer_offsets SET committed_offset = {base} "
                    "WHERE consumer_group = 'orders_fct_sink'")

        def counts():
            a = con.execute(f"""
              SELECT COUNT(*) FROM warehouse.fct_orders f
              WHERE (f.order_id, f.revision) IN (
                SELECT TRY_CAST(json_extract_string(payload,'$.order_id') AS BIGINT),
                       TRY_CAST(json_extract_string(payload,'$.revision') AS INTEGER)
                FROM redeliver)""").fetchone()[0]
            b = con.execute("SELECT COUNT(*) FROM warehouse.fct_orders "
                            "WHERE order_id BETWEEN 990000001 AND 999999999").fetchone()[0]
            return int(a), int(b)

        before_dup, before_new = counts()
        err = None
        for f in (quar, sink):
            if not f.exists():
                continue
            try:
                con.execute(f.read_text(encoding="utf-8", errors="replace"))
            except Exception as exc:      # the runner's on_failure_policy is ignore
                err = f"{f.name}: {str(exc).splitlines()[0][:120]}"
                if f == sink:
                    break
        after_dup, after_new = counts()
        con.close()

        added_dup = after_dup - before_dup
        added_new = after_new - before_new
        d = (f"redelivered {n_redeliver} consumed records -> {added_dup:+} rows; "
             f"{n_new} never-seen records -> {added_new:+} rows"
             + (f"; sink raised [{err}]" if err else ""))
        if added_dup == 0 and added_new == n_new:
            s.add("K5", 6, 6, d + " - sink is idempotent and still live")
        elif added_dup == 0:
            s.add("K5", 0, 6, d + " - idempotent by never loading anything; a "
                                  "permanent skip repairs nothing")
        else:
            s.add("K5", 0, 6, d + " - redelivery duplicates; the sink has no merge key")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# K6 ---------------------------------------------------------------- 3 pts
def k6_blast_radius(con, s: Score, t: dict, replayed: bool):
    """Every partition on which the broken and the repaired world already agree
    must be untouched. Resetting the consumer to log_start replays thirty days
    into an append sink and fails this; so does rebuilding the table from the
    topic, which cannot reach past the retention horizon at all.

    GATED on having actually loaded the gap. There is no blast radius without a
    blast: ungated, an agent that does nothing at all satisfies this bound by
    definition and banks three points for it."""
    fp = ("md5(string_agg(md5((order_id, revision, order_dt, event_ts, customer_id, "
          "channel_code, sku, qty, unit_price, order_amount)::VARCHAR), '' ORDER BY 1))")
    got = dict(con.execute(
        f"SELECT order_dt::VARCHAR, {fp} FROM w.warehouse.fct_orders GROUP BY 1").fetchall())
    want = t["pre_incident_partitions"]
    bad = [d for d, h in want.items() if got.get(d) != h]
    if not replayed:
        s.add("K6", 0, 3, f"{len(bad)} of {len(want)} partitions changed, but the gap "
                          "was never replayed - nothing was put at risk")
    elif not bad:
        s.add("K6", 3, 3, f"all {len(want)} untouched partitions are byte-identical")
    elif len(bad) <= 3:
        s.add("K6", 1, 3, f"{len(bad)} of {len(want)} untouched partitions changed "
                          f"({sorted(bad)})")
    else:
        s.add("K6", 0, 3, f"{len(bad)} of {len(want)} untouched partitions changed "
                          f"(first: {sorted(bad)[:4]})")


# K7 ---------------------------------------------------------------- 1 pt
def k7_dead_letter(con, s: Score, t: dict):
    """The undecodable records must end up in the dead-letter table with their
    original offset and payload, not be advanced past and lost."""
    want = t["dlq_expected_offsets"]
    con.execute("CREATE TEMP TABLE dlqman (offset_id BIGINT)")
    con.executemany("INSERT INTO dlqman VALUES (?)", [(int(o),) for o in want])
    ok = con.execute("""
        SELECT COUNT(*) FROM dlqman m
        JOIN w.raw.orders_dlq d ON d.offset_id = m.offset_id
        JOIN e.raw.orders_topic t ON t.offset_id = m.offset_id
        WHERE d.payload = t.payload""").fetchone()[0]
    n = len(want)
    if ok == n:
        s.add("K7", 1, 1, f"all {n} undecodable records dead-lettered with offset and payload")
    elif ok:
        s.add("K7", 0.5, 1, f"{ok}/{n} undecodable records dead-lettered")
    else:
        s.add("K7", 0, 1, f"0/{n} undecodable records dead-lettered - skipped without a trace")


# K8 ------------------------------------------------- PENALTY ONLY (max 0)
def k8_collateral(con, s: Score, t: dict):
    """Two things that are destroyed rather than got wrong, each unrecoverable
    from inside the world.

    (a) fct_orders holds 90 days; the topic retains 30. Anything older than the
        retention horizon exists in exactly one place. Rebuilding the table from
        the stream looks complete - every check that only reads recent data
        passes - and silently deletes 60 days that cannot be got back. That is
        the full-refresh hazard A063 names, and a bound on a partition checksum
        does not express how bad it is, so it also carries a penalty.
    (b) The stale channel dimension is a real defect and fixing it is fine. What
        is not fine is fixing it the way its own asset job does - a full replace
        - which drops the hand-curated mappings the job does not produce.

    Worth ZERO points. A guard an idle agent passes by doing nothing is a free
    point, and free points are how a rubric stops discriminating (GT001, D4)."""
    penalty, notes = 0.0, []

    kept = con.execute(
        f"SELECT COUNT(*) FROM w.warehouse.fct_orders "
        f"WHERE order_dt < DATE '{t['retention_start']}'").fetchone()[0]
    want = int(t["unrestorable_rows"])
    if kept < want:
        penalty -= 3
        notes.append(f"{want - kept:,} of {want:,} rows older than the topic's "
                     f"retention horizon were DESTROYED and cannot be replayed back")
    else:
        notes.append(f"{kept:,} pre-retention rows intact")

    try:
        got = con.execute(
            "SELECT md5(string_agg(channel_code || '|' || channel_name || '|' || "
            "channel_group, '' ORDER BY channel_code)) FROM w.warehouse.dim_channel "
            "WHERE is_curated").fetchone()[0]
    except Exception as exc:
        penalty -= 2
        notes.append(f"dim_channel unreadable: {str(exc).splitlines()[0][:70]}")
    else:
        if got == t["curated_channel_fp"]:
            notes.append("curated channel mappings intact")
        else:
            penalty -= 2
            notes.append("CURATED channel mappings lost - the decoy's own "
                         "repair hazard fired")
    s.add("K8", penalty, 0, "; ".join(notes))


# K9 ---------------------------------------------------------------- 2 pts
def k9_primary_cause(artifact: dict, man: dict, s: Score):
    pc = man.get("primary_cause", {})
    text = str(artifact.get("primary_cause", "") or "").lower()
    groups = pc.get("accept_tokens", {}).get("all_of") or []
    hit = all(any(tok in text for tok in g) for g in groups) if groups else False
    rejected = [r for r in (pc.get("reject_if_primary") or []) if r.lower() in text]
    # A POSITIVE MATCH WINS. The rejection list only explains a failure; it never
    # overrides a correct diagnosis. An earlier GT001 grader substring-matched its
    # reject list first and scored the oracle's own correct answer as zero for
    # naming the thing it had correctly ruled out. A grader that punishes
    # precision is worse than no grader.
    if hit:
        s.add("K9", 2, 2, "primary cause correctly identified")
    elif rejected:
        s.add("K9", 0, 2, f"primary cause not identified; names {rejected} instead")
    else:
        s.add("K9", 0, 2, "primary cause not identified")


# K10 --------------------------------------------------------------- 1 pt
def k10_secondary(artifact: dict, man: dict, s: Score):
    """Per-cause credit for genuine secondary findings, MINUS a penalty for
    asserted causes that do not exist. Without the penalty an agent emits a
    textbook checklist and banks the credit (F128/F129/F306)."""
    causes = [c for c in (man.get("causes") or []) if isinstance(c, dict)]
    secondary = {c["id"]: c for c in causes if c.get("kind") != "primary"}
    keys = man.get("secondary_match_tokens") or {}
    listed = [str(x).lower() for x in (artifact.get("additional_causes") or [])]
    blob = " ".join(listed)

    earned, found = 0.0, []
    for cid, c in secondary.items():
        if any(k in blob for k in keys.get(cid, ())):
            earned += float(c.get("credit", 0) or 0)
            found.append(cid)
    fp = man.get("false_cause_penalty", {})
    per = float(fp.get("per_unsupported_cause", -1.0))
    floor = float(fp.get("floor", -3.0))
    every = tuple(k for ks in keys.values() for k in ks)
    unsupported = [x for x in listed if not any(k in x for k in every)]
    penalty = max(floor, per * len(unsupported))
    # NOT clamped at zero. With a 1-point ceiling on credit, a zero floor caps
    # the checklist agent's loss at one point and the penalty stops discriminating.
    pts = max(-2.0, min(1.0, earned) + penalty)
    s.add("K10", pts, 1,
          f"genuine secondary findings {found or 'none'} (+{min(earned, 1.0)}); "
          f"{len(unsupported)} unsupported cause(s) ({penalty})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True)
    ap.add_argument("--grading", required=True)
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--world-root", default=None,
                    help="agent workspace root; without it the idempotency probe "
                         "cannot run and K5 scores 0")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    grading = Path(a.grading)
    man = load_manifest(grading / "manifest.yaml")
    truth = json.loads((grading / "harness_truth.json").read_text(encoding="utf-8"))
    try:
        artifact = json.loads(Path(a.artifact).read_text(encoding="utf-8"))
    except Exception as exc:
        artifact = {}
        print(f"warning: final artifact unreadable ({exc}) - narrative checks score 0",
              file=sys.stderr)

    world = Path(a.world)
    con = connect(world, grading)
    s = Score()
    c0 = int(truth["committed_offset_at_start"])
    gap_manifest(con, c0)
    loaded, dlq = loaded_and_dlq(con, c0)
    produced = int(truth["produced_gap"])
    # "is the ledger essentially complete" - the bar K1 scores against
    substantive = loaded >= produced - 200
    # "did the agent load the gap at all" - the gate for the checks that only
    # mean something once something was put at risk. An agent that replayed and
    # then deleted rows HAS put the world at risk and must be judged on it; the
    # tighter bar above would have excused exactly that.
    replayed = loaded >= produced * 0.5

    k1_conservation(con, s, truth, loaded, dlq)
    k2_lag(con, s, truth)
    k3_duplicates(con, s, replayed)
    k4_revisions(con, s, truth, replayed)
    if a.world_root:
        k5_idempotency_probe(world, Path(a.world_root), s, truth, man)
    else:
        s.add("K5", 0, 6, "no --world-root given; idempotency probe not run")
    k6_blast_radius(con, s, truth, replayed)
    k7_dead_letter(con, s, truth)
    k8_collateral(con, s, truth)
    k9_primary_cause(artifact, man, s)
    k10_secondary(artifact, man, s)
    con.close()

    result = {"task": "GT002-orders-flatline", "total": s.total, "max": s.maximum,
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
