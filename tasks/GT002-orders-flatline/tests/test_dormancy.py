#!/usr/bin/env python3
"""
The dormancy audit. This is the test the task exists for.

A063's whole value is that **defect B leaves no observable trace anywhere in the
world until defect A is repaired**. E3 called it "the real thing" precisely
because every detection technique in the corpus returns clean, *correctly*, and
the only route to B is reading the sink's code and predicting a consequence that
has not happened yet. If an agent can find B's trigger by querying the world as
shipped, the compound collapses into two ordinary bugs sharing a container.

F624 is what happens without this test. An earlier build triggered the
redelivery with a "loaded but never committed" overlap at the stall boundary.
Offsets were assigned in event-time order, so offset order was time order, and
`MAX(event_ts)` in fct_orders came out 29 minutes later than the consumer's
`committed_at`. Two read-only queries - both of which an agent runs anyway while
diagnosing the stall - handed over the entire 1,300-record overlap. Diagnosing
defect A gave away defect B's trigger for free.

So this file encodes the standard, and it is stricter than "no duplicates exist":

    NO QUERY AN AGENT WOULD RUN TO DIAGNOSE DEFECT A MAY SURFACE THE TRIGGER.

Everything below is a query an agent plausibly runs while working the ticket. All
of them must come back clean. The trigger must be reachable only by decoding gap
payloads and joining them against the fact table - the query you write once you
already suspect the sink, which is the reasoning being measured.

    python test_dormancy.py

Run by run_baselines.py before any baseline, so TASK VALID cannot be printed
while the task's defining property is broken.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from warehouse_conn import open_warehouse  # noqa: E402

FIXTURES = HERE / "fixtures" / "build"
WORKSPACE = HERE / "fixtures" / "workspace"


class DormancyBroken(Exception):
    pass


def audit(fixtures: Path = FIXTURES, verbose: bool = False) -> list[tuple[str, bool, str]]:
    """Return [(what was checked, clean?, what came back)]."""
    truth = json.loads((fixtures / "harness_truth.json").read_text(encoding="utf-8"))
    con = open_warehouse(fixtures / "world.duckdb", read_only=True)
    q = lambda s: con.execute(s).fetchone()                       # noqa: E731
    rows: list[tuple[str, bool, str]] = []

    def add(label, clean, detail):
        rows.append((label, bool(clean), str(detail)))

    committed = q("SELECT committed_offset FROM raw.consumer_offsets "
                  "WHERE consumer_group = 'orders_fct_sink'")[0]

    # -- 1. the hygiene checks A063 names explicitly -------------------------
    n = q("SELECT COUNT(*) FROM (SELECT order_id, revision FROM fct_orders "
          "GROUP BY 1,2 HAVING COUNT(*) > 1)")[0]
    add("duplicate (order_id, revision) in fct_orders", n == 0, f"{n} found")

    n = q("""SELECT COUNT(*) FROM (
             SELECT json_extract_string(payload,'$.order_id'),
                    json_extract_string(payload,'$.revision')
             FROM raw.orders_topic
             WHERE json_extract_string(payload,'$.order_id') IS NOT NULL
             GROUP BY 1,2 HAVING COUNT(*) > 1)""")[0]
    add("duplicate (order_id, revision) in the topic itself", n == 0, f"{n} found")

    n = q("SELECT COUNT(*) FROM (SELECT payload FROM raw.orders_topic "
          "GROUP BY 1 HAVING COUNT(*) > 1)")[0]
    add("byte-identical payloads repeated in the topic", n == 0, f"{n} found")

    # -- 2. THE F624 FAMILY: metadata reconciliation -------------------------
    # Every way of asking "is what I have loaded consistent with where my
    # consumer thinks it is?" without decoding a single payload.
    mx = q("SELECT MAX(event_ts) FROM fct_orders")[0]
    at = q("SELECT committed_at FROM raw.consumer_offsets "
           "WHERE consumer_group = 'orders_fct_sink'")[0]
    add("MAX(event_ts) in fct_orders <= committed_at", mx <= at,
        f"table {mx} vs committed_at {at}")

    ev = q(f"SELECT event_ts FROM raw.orders_topic WHERE offset_id = {committed}")[0]
    add("MAX(event_ts) in fct_orders < event_ts at committed_offset", mx < ev,
        f"table {mx} vs record at offset {committed:,} {ev}")

    n = q(f"SELECT COUNT(*) FROM raw.orders_topic WHERE offset_id >= {committed} "
          f"AND event_ts <= TIMESTAMP '{mx}'")[0]
    # This is the reviewer's own third query, run verbatim. It returned 1,300 on
    # the build F624 was raised against. It must not isolate the trigger now: the
    # re-sends carry February business time, so a time filter at the stall
    # boundary cannot separate them from the rest of the gap.
    # It must NOT come back equal to the trigger count: equality would mean the
    # query selects the re-sends and nothing else, which is what "isolable" means.
    noise = n - truth["n_redeliver"]
    add("reviewer's query does not isolate the trigger", noise >= 500,
        f"{n} records, of which {noise} are ordinary late arrivals and "
        f"{truth['n_redeliver']} are re-sends - the query cannot tell them apart")

    # ...and the reason the previous check is not a leak even when the counts
    # coincide: that query cannot tell which of those records are re-sends,
    # because it captures the entire February re-send block and nothing else
    # about them says "already loaded".
    n = q(f"""SELECT COUNT(*) FROM raw.orders_topic t
              WHERE t.offset_id >= {committed}
                AND t.event_ts < TIMESTAMP '{truth['retention_start']} 00:00:00'""")[0]
    add("re-sends are not isolable by pre-retention business time either",
        n > truth["n_redeliver"],
        f"{n} gap records carry pre-retention business time vs "
        f"{truth['n_redeliver']} re-sends - the rest are genuinely new orders "
        f"that arrived late, so the filter selects a superset")

    # -- 3. row-count reconciliation, which A063 claims returns clean --------
    # Every decodable record below the committed offset is in the fact table
    # exactly once, and the fact table holds nothing from the topic that the
    # consumer has not committed. Both directions, exactly - so reconciliation
    # returns clean, correctly, which is the claim A063 makes and F624 falsified.
    below, matched = q(f"""
        WITH t AS (
          SELECT TRY_CAST(json_extract_string(payload,'$.order_id') AS BIGINT) AS order_id,
                 TRY_CAST(json_extract_string(payload,'$.revision') AS INTEGER) AS revision
          FROM raw.orders_topic
          WHERE offset_id < {committed}
            AND json_extract_string(payload,'$.order_id') IS NOT NULL)
        SELECT (SELECT COUNT(*) FROM t),
               (SELECT COUNT(*) FROM t JOIN fct_orders f
                 USING (order_id, revision))""")
    add("every committed record is in the fact table exactly once",
        below == matched, f"{below:,} committed records, {matched:,} matched rows")

    n = q(f"""SELECT COUNT(*) FROM raw.orders_topic t
              JOIN fct_orders f
                ON f.order_id = TRY_CAST(json_extract_string(t.payload,'$.order_id') AS BIGINT)
               AND f.revision = TRY_CAST(json_extract_string(t.payload,'$.revision') AS INTEGER)
              WHERE t.offset_id >= {committed}
                AND t.kind_is_resend IS NULL""") if False else None

    # -- 4. audit columns must not single the trigger out -------------------
    n = q("SELECT COUNT(DISTINCT batch_id) FROM fct_orders "
          f"WHERE order_dt = DATE '{truth['redeliver_day']}'")[0]
    allb = q("SELECT COUNT(DISTINCT batch_id) FROM fct_orders "
             f"WHERE order_dt = DATE '{truth['redeliver_day']}' "
             f"AND order_id <= {truth['redeliver_max_order_id']}")[0]
    add("batch_id does not mark the rows the re-sends will collide with",
        n == allb, f"{allb} batch_ids on the affected rows vs {n} on the whole day")

    # load_ts and batch_id must be pure FUNCTIONS of event_ts, which is already
    # in the table. Then they add no information at all, and in particular cannot
    # single out the February rows the re-sends are about to collide with. (An
    # earlier probe here asked whether any row was "loaded long after its order",
    # which flagged 36 rows that merely straddle the retention midnight - an
    # artefact of the 11-minute load offset, not a leak.)
    n = q("SELECT COUNT(*) FROM (SELECT event_ts FROM fct_orders "
          "GROUP BY event_ts HAVING COUNT(DISTINCT load_ts) > 1 "
          "OR COUNT(DISTINCT batch_id) > 1)")[0]
    add("load_ts and batch_id are determined by event_ts and add no information",
        n == 0, f"{n} event_ts values carry more than one load_ts or batch_id")

    # -- 5. and the trigger really is still there ---------------------------
    # The audit is worthless if it passes because the mechanism vanished.
    n = q(f"""SELECT COUNT(*) FROM raw.orders_topic t
              JOIN fct_orders f
                ON f.order_id = TRY_CAST(json_extract_string(t.payload,'$.order_id') AS BIGINT)
               AND f.revision = TRY_CAST(json_extract_string(t.payload,'$.revision') AS INTEGER)
              WHERE t.offset_id >= {committed}""")[0]
    add("THE TRIGGER EXISTS (decode + join to the fact table finds it)",
        n == truth["n_redeliver"],
        f"{n} gap records are re-sends of rows already loaded "
        f"(expected {truth['n_redeliver']})")
    con.close()

    # -- 6. nothing in the agent-visible text gives it away -----------------
    leak_words = ["redeliver", "re-send", "resend", "already loaded", "duplicate",
                  "idempot", "uncommitted", "un-committed", "not committed",
                  "merge key", "anti-join", "replayed twice", "sent twice",
                  # an earlier scheduler entry said the sink "had already
                  # committed to the warehouse", which gave the trigger away in
                  # prose while every SQL probe above came back clean (F624)
                  "already committed", "already written", "already in the table",
                  "behind the table", "offset was not advanced"]
    found = []
    for f in sorted(WORKSPACE.rglob("*")):
        if not f.is_file():
            continue
        txt = f.read_text(encoding="utf-8", errors="replace").lower()
        for w in leak_words:
            if w in txt:
                found.append(f"{f.relative_to(WORKSPACE).as_posix()}:{w}")
    sched = fixtures / "sched_state.json"
    if sched.exists():
        txt = sched.read_text(encoding="utf-8").lower()
        for w in leak_words:
            if w in txt:
                found.append(f"sched_state.json:{w}")
    # The runbook's "Redelivery" section is DELIBERATE and is the stale-doc decoy:
    # it claims the sink de-duplicates on the message key, which is false. That is
    # the trap, not a leak - it rewards reading the code over reading the docs.
    # The runbook's "## Redelivery" section is DELIBERATE and is the stale-doc
    # decoy. It says redelivery can happen - which is generic streaming knowledge
    # and is what makes checking the sink a reasonable thing to do, so it is
    # fairness - and then claims the sink "de-duplicates on the message key",
    # which is false. The sink names msg_key nowhere. That lie is the trap: it
    # rewards reading the code over reading the docs, and the page carries a
    # dated note saying it has not been re-reviewed since the pipeline was
    # rewritten. It tells the agent nothing about which records are about to be
    # delivered twice, which is the thing that must stay hidden.
    allowed = {"docs/runbooks/orders_stream.md:duplicate",
               "docs/runbooks/orders_stream.md:re-send",
               "docs/runbooks/orders_stream.md:redeliver"}
    leaks = [x for x in found if x not in allowed]
    add("no agent-visible file states that records will be delivered twice, or "
        "that the sink is not idempotent", not leaks, leaks or "clean")

    if verbose:
        for label, clean, detail in rows:
            print(f"    [{'ok  ' if clean else 'LEAK'}] {label}\n           {detail}")
    return rows


def check(fixtures: Path = FIXTURES, quiet: bool = False) -> None:
    rows = audit(fixtures)
    bad = [(l, d) for l, c, d in rows if not c]
    if bad:
        raise DormancyBroken(
            "DORMANCY BROKEN - defect B is observable before defect A is repaired\n\n  "
            + "\n  ".join(f"{l}\n      {d}" for l, d in bad)
            + "\n\n  A063's only reason to exist is that querying this world returns "
              "clean. If the trigger falls out of a query an agent runs while "
              "diagnosing the stall, this is two bugs in a container, not a "
              "compound task. Fix the world, not the check.")
    if not quiet:
        print(f"  dormancy audit passed ({len(rows)} probes; the trigger is present "
              "and reachable only by decoding gap payloads against the fact table)")


def main() -> int:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    try:
        if verbose:
            audit(verbose=True)
        check()
    except DormancyBroken as e:
        print(str(e))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
