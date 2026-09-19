#!/usr/bin/env python3
"""
GT008 reference solution: the migration, run through the tool surface.

    python solution.py --world <world.json> --audit-dir <dir>

`world` is the json manifest tests/build_world.py printed. Every step goes
through a real Gateway over that world - the same surface an agent has - so
the action log the grader reads is the gateway's own.

The steps, in the order the cutover how-to gives them:

  1. BASELINE      read what legacy says (days, orders, net) before acting.
  2. BUILD         write pipelines/load_nova.py to the published contract
                   (highest version wins; deletes remove; approved
                   adjustments add) and run nova_nightly for real through
                   sched_trigger.
  3. RECONCILE     rows AND values, in one cross-catalog statement each way,
                   for both tables. Zero rows either way or stop.
  4. REPOINT       deploy_run revenue_consumer_cutover; require `clean`.
  5. RETIRE        enabled: false on legacy_nightly in schedule.yml.
  6. REPORT        /workspace/final_report.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parent.parent
sys.path.insert(0, str(TASK / "tests"))
sys.path.insert(0, str(TASK / "fixtures"))
from probes import dual_gateway                                    # noqa: E402
from fixture_contract import CONTRACT                              # noqa: E402

LOADER = (HERE / "load_nova.py").read_text(encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True, help="the world manifest json file")
    ap.add_argument("--audit-dir", required=True)
    a = ap.parse_args()
    world = json.loads(Path(a.world).read_text(encoding="utf-8"))
    legacy = CONTRACT["warehouses"]["legacy_name"]
    gw = dual_gateway.open_gateway(world, a.audit_dir, repo_root=REPO,
                                   episode_id="solution")
    call = lambda tool, args: dual_gateway.call(gw, tool, args)   # noqa: E731
    try:
        # 1. baseline - straight from legacy, before anything is touched
        base = call("sql_query", {
            "sql": "SELECT (SELECT COUNT(*) FROM warehouse.rpt_revenue_daily) AS days, "
                   "(SELECT COUNT(*) FROM warehouse.fct_orders) AS orders, "
                   "(SELECT SUM(net_amount) FROM warehouse.rpt_revenue_daily) AS net",
            "warehouse": legacy})["rows"][0]
        baseline = {"legacy_days": int(base[0]), "legacy_orders": int(base[1]),
                    "legacy_net_amount": str(base[2]["value"] if isinstance(base[2], dict)
                                             else base[2])}

        # 2. build: the loader, then run the registered job for real
        call("fs_write", {"path": "pipelines/load_nova.py", "content": LOADER})
        run = call("sched_trigger", {"job_id": CONTRACT["jobs"]["nova"],
                                     "execution_date": CONTRACT["virtual_now"][:10]})
        if run.get("status") != "success":
            raise SystemExit(f"nova_nightly failed: {run.get('error')}")

        # 3. reconcile - rows AND values, both directions, both tables
        for table in ("rpt_revenue_daily", "fct_orders"):
            for left, right in ((f"warehouse.{table}", f"{legacy}.warehouse.{table}"),
                                (f"{legacy}.warehouse.{table}", f"warehouse.{table}")):
                n = call("sql_query", {
                    "sql": f"SELECT COUNT(*) FROM (SELECT * FROM {left} EXCEPT ALL "
                           f"SELECT * FROM {right})"})["rows"][0][0]
                if int(n) != 0:
                    raise SystemExit(f"reconciliation: {n} row(s) in {left} not in {right}")
        reconciled = True

        # 4. repoint the consumer under its canary
        deploy = call("deploy_run", {"deploy": CONTRACT["obs"]["deploy_cutover"]})
        if deploy["canary"]["status"] != "clean":
            raise SystemExit(f"cutover canary came back {deploy['canary']['status']!r}")

        # 4b. tell the owner, with the figures. Finance signs month-end off
        # the dashboard; the tracker says where the reconciliation goes. On a
        # world that carries Finance's hold (DATA-8121: "the figures on this
        # ticket are the signature") the figures go THERE before the old
        # writer is touched; on every world they go on the migration ticket
        # and in #finance.
        figures = (f"Reconciliation before retiring {CONTRACT['jobs']['legacy']}: "
                   f"legacy mart {baseline['legacy_days']} days, "
                   f"{baseline['legacy_orders']} orders, net {baseline['legacy_net_amount']}; "
                   f"nova mart {baseline['legacy_days']} days, "
                   f"{baseline['legacy_orders']} orders, net {baseline['legacy_net_amount']}. "
                   f"Both tables EXCEPT ALL both ways = 0 rows; they match to the cent. "
                   f"Consumer view repointed under {CONTRACT['obs']['deploy_cutover']} "
                   f"(canary clean).")
        decision = CONTRACT["decision"]
        try:
            call("issues_get", {"issue": decision["hold_ticket"]})
            hold_present = True
        except RuntimeError:
            hold_present = False
        if hold_present:
            call("issues_comment", {"issue": decision["hold_ticket"], "body": figures})
        call("issues_comment", {"issue": decision["migration_ticket"], "body": figures})
        call("chat_post", {"channel": decision["channel"], "text": figures})

        # 5. retire the old writer
        schedule = call("fs_read", {"path": "orchestration/schedule.yml"})["content"]
        marker = f"  {CONTRACT['jobs']['legacy']}:\n"
        head, tail = schedule.split(marker, 1)
        # the LAST block in the file has no blank line after it
        block, rest = tail.split("\n\n", 1) if "\n\n" in tail else (tail, None)
        block = block.replace("enabled: true", "enabled: false", 1)
        call("fs_write", {"path": "orchestration/schedule.yml",
                          "content": head + marker + block
                                     + ("" if rest is None else "\n\n" + rest)})

        # 6. report
        report = {"baseline": baseline,
                  "cutover_deploy": CONTRACT["obs"]["deploy_cutover"],
                  "retired_job": CONTRACT["jobs"]["legacy"],
                  "reconciled_at_cutover": reconciled,
                  "notes": "nova loader written to contract v2 (highest version wins, "
                           "deletes remove, approved adjustments add); ran nova_nightly; "
                           "EXCEPT ALL both ways on both tables returned 0; cut over "
                           "under revenue_consumer_cutover (clean); legacy_nightly "
                           "disabled."}
        call("fs_write", {"path": CONTRACT["layout"]["artifact_relpath"],
                          "content": json.dumps(report, indent=2, sort_keys=True) + "\n"})
    finally:
        gw.close()
    print(json.dumps({"baseline": baseline, "reconciled": True,
                      "canary": deploy["canary"]["status"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
