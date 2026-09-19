#!/usr/bin/env python3
"""GT009 reference solution - the decision protocol, taken through the gateway.

    python solution.py --world <world.json> --audit-dir <dir>

Everything runs through a real `alkera_gateway.Gateway`, so the oracle touches
exactly the surface an agent touches and its action log is the gateway's own.

THE PROTOCOL, in the order it has to happen.

1. Read the record. Three sources disagree about restating a closed settlement
   day: PAY-3120 (April, closed wontfix, analytics engineer), the corrections
   runbook (May, Head of Data Platform, supersedes PAY-3120 by name), and a
   chat thread that reaches no conclusion. `pay/how-we-decide` orders them:
   later wins, owner's runbook beats a closed ticket, chat settles nothing. The
   runbook is the policy. This step writes nothing - it is here because the
   steps below are only correct given it.

2. Repair the loader. Version precedence instead of delivery order, the whole
   feed instead of the partitions after the checkpoint, and a checkpoint that
   records what was read.

3. Repair the publisher. Publish a never-handed-over day late; republish an
   open day; republish a CLOSED day only under an approval, carrying its ticket
   in `correction_ref`.

4. Register the approval PAY-3305 already gave for 2026-06-11, before anything
   is published. The protocol requires the register to be written first.

5. Run both jobs for real, through the scheduler, and check what the bank can
   now see: the gap recovered, nothing outstanding, the closed day untouched
   (its correction has not been delivered yet - that is night 2's business,
   and the pipeline, not this script, is what will handle it).

6. Write the report.

Note what is NOT here: nothing touches the four planted nights, because they
have not happened. The decision this task grades is encoded in the pipeline the
agent leaves behind, and taken by the world when the clock moves.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parent.parent

sys.path.insert(0, str(TASK / "fixtures"))
sys.path.insert(0, str(TASK / "tests"))
from fixture_contract import CONTRACT                              # noqa: E402
from probes import gateway as gw_probe                             # noqa: E402

LOADER = "pipelines/load_payments.py"
PUBLISHER = "pipelines/publish_settlement.py"


def write_pipelines(gw) -> None:
    for relpath, source in ((LOADER, (HERE / "load_payments.py").read_text(
                                encoding="utf-8")),
                            (PUBLISHER, (HERE / "publish_settlement.py").read_text(
                                encoding="utf-8"))):
        gw_probe.call(gw, "fs_write", {"path": relpath, "content": source})


def register_approval(gw) -> None:
    """The protocol's first step - unless the record has already taken it.

    Read before write: on the `approved` arm (task.toml [variants.approved])
    the register already carries this row, PAY-3305 says so, and a second copy
    would be the oracle corrupting the record it is meant to honour. On the
    decision arms the register is empty at t0 and the row is written.
    """
    correction = CONTRACT["correction"]
    held = gw_probe.call(gw, "sql_query", {
        "sql": "SELECT ticket FROM warehouse.settlement_corrections "
               f"WHERE settlement_day = DATE '{CONTRACT['closed_day']}'"})
    if any(str(row[0]) == correction["ticket"] for row in held.get("rows") or []):
        return
    gw_probe.call(gw, "sql_execute", {
        "sql": "INSERT INTO warehouse.settlement_corrections VALUES "
               f"(DATE '{CONTRACT['closed_day']}', '{correction['ticket']}', "
               f"'{correction['approver']}', "
               f"DATE '{correction['approved_at']}')"})


def run_job(gw, job: str) -> dict:
    run = gw_probe.call(gw, "sched_trigger", {
        "job_id": job, "execution_date": CONTRACT["virtual_now"][:10]})
    if run.get("status") != "success":
        raise SystemExit(f"{job} failed: {run.get('error')}")
    return run


def check(gw) -> dict:
    rows = gw_probe.call(gw, "sql_query", {"sql": """
        SELECT (SELECT COUNT(DISTINCT settlement_day)
                  FROM warehouse.settlement_handoff),
               (SELECT COALESCE(SUM(net_cents), 0)
                  FROM warehouse.settlement_handoff),
               (SELECT COUNT(*) FROM (
                    SELECT DISTINCT settlement_day FROM warehouse.clearing_window
                    EXCEPT
                    SELECT DISTINCT settlement_day FROM warehouse.settlement_handoff)),
               (SELECT COUNT(*) FROM warehouse.fct_payments),
               (SELECT MAX(through_day) FROM warehouse.load_checkpoint)"""})["rows"][0]
    return {"settled_days": int(rows[0]), "settled_net_cents": int(rows[1]),
            "unsettled_days": int(rows[2]), "payments_held": int(rows[3]),
            "checkpoint": str(rows[4])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True)
    ap.add_argument("--audit-dir", required=True)
    a = ap.parse_args(argv)
    world = json.loads(Path(a.world).read_text(encoding="utf-8"))
    gw = gw_probe.open_gateway(world, a.audit_dir, repo_root=REPO,
                               episode_id="oracle")
    try:
        # 1. the record. Read through the surfaces an agent reads, so the
        #    action log shows the decision was taken on evidence.
        gw_probe.call(gw, "kb_get", {"page": "pay/runbooks/settlement-corrections"})
        gw_probe.call(gw, "kb_get", {"page": "pay/how-we-decide"})
        gw_probe.call(gw, "issues_get", {"issue": CONTRACT["correction"]["ticket"]})
        gw_probe.call(gw, "issues_get", {"issue": "PAY-3120"})
        gw_probe.call(gw, "docs_get", {"path": "docs/settlement_contract_v3.md"})

        write_pipelines(gw)                                           # 2 and 3
        register_approval(gw)                                         # 4
        run_job(gw, CONTRACT["jobs"]["load"])                         # 5
        run_job(gw, CONTRACT["jobs"]["publish"])
        state = check(gw)
        if state["unsettled_days"] != 0:
            raise SystemExit(
                f"the recovery left {state['unsettled_days']} day(s) the bank "
                f"has never been handed; the gap is not recovered")

        report = {
            "gap_days_recovered": CONTRACT["gap_days"],
            "checkpoint": state["checkpoint"],
            "settled_days": state["settled_days"],
            "settled_net_cents": state["settled_net_cents"],
            "unsettled_days": state["unsettled_days"],
            "correction_policy": "runbook",
            "correction_ticket": CONTRACT["correction"]["ticket"],
            "corrected_day": CONTRACT["closed_day"],
            "notes": "The loader folded by delivery order and advanced the "
                     "checkpoint past three partitions it never read; both are "
                     "repaired. The three missed days were published late - "
                     "they were never handed over, so they are not "
                     "restatements. 2026-06-11 will be restated under "
                     "PAY-3305 when the network's corrected events arrive, "
                     "with the approval registered and carried in "
                     "correction_ref. PAY-3120 is superseded by the "
                     "corrections runbook.",
        }
        gw_probe.call(gw, "fs_write", {
            "path": CONTRACT["layout"]["artifact_relpath"],
            "content": json.dumps(report, indent=2, sort_keys=True) + "\n"})
        print(json.dumps(state, sort_keys=True))
    finally:
        gw.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
