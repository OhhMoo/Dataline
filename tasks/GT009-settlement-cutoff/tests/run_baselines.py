#!/usr/bin/env python3
"""
GT009 task-validity suite. Prints TASK VALID or says why not.

    python run_baselines.py                 # every baseline, then the verdict
    python run_baselines.py --only oracle wrong_precedence
    python run_baselines.py --quick         # skip the (slow) falsifier

Each baseline builds its OWN world through tests/build_world.py - the same
entrypoint the rollout harness calls - acts on it through a REAL gateway
(probes/gateway.py), then is scored by running tests/grade.py as a subprocess
with the argv task.toml declares. Nothing here reaches inside the grader.

THE BASELINES, and the direction each one fails in. Every row after the oracle
is a competent pipeline that answered the question wrongly, and the two
directions are the point:

  TOWARDS PUBLISHING
    publish_everything_immediately  restates the closed day the moment the
                                    correction lands, with no approval and no
                                    correction_ref. The bank gets the right
                                    arithmetic through the wrong door.
    overwrite_silently              registers the approval and then publishes
                                    without the reference. The protocol is
                                    three clauses and this one keeps two.
    replay_wins                     takes the most recently delivered event as
                                    current, so night 3's superseded replay is
                                    republished over night 2's correction.
    duplicate_counted               counts the retried delivery twice.

  TOWARDS WITHHOLDING
    wrong_precedence                follows the closed April ticket and the
                                    chat thread over the runbook, and never
                                    restates a closed day at all.
    withhold_everything             recovers at t0 and then switches the
                                    publisher off. Every window after that
                                    closes empty.
    disabled_load                   switches everything off.

  NEITHER
    no_op, report_only, checklist   nothing done; nothing done and written up;
                                    nothing done and written up falsely.

  POSITIVE CONTROL
    rebuilt_not_updated             the oracle's answer, reached by rebuilding
                                    the handoff table from scratch at the
                                    frozen layout instead of updating it in
                                    place. Must still score max: the receipt
                                    is determined by what the bank can see,
                                    not by the statements that put it there.

Structural gates, checked once and not a baseline: F121 (nothing from the
grading zone reachable under the world root), the planted nights are not in the
agent-visible feed, the gateway serves the shipped dashboard, and the world
writes real receipts when the clock moves.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
FIXTURES = TASK / "fixtures"
REPO = TASK.parent.parent

sys.path.insert(0, str(FIXTURES))
sys.path.insert(0, str(HERE))
from fixture_contract import CONTRACT, FixtureDrift, falsify, verify  # noqa: E402
from probes import gateway as gw_probe                                # noqa: E402
import make_fixture_world as mfw                                      # noqa: E402

import yaml                                                        # noqa: E402

BUILD_WORLD = HERE / "build_world.py"
GRADER = HERE / "grade.py"
SOLUTION = TASK / "solution" / "solution.sh"
GOOD_LOADER = (TASK / "solution" / "load_payments.py").read_text(encoding="utf-8")
GOOD_PUBLISHER = (TASK / "solution" / "publish_settlement.py").read_text(
    encoding="utf-8")
LOAD_JOB = CONTRACT["jobs"]["load"]
PUBLISH_JOB = CONTRACT["jobs"]["publish"]
LOADER_PATH = "pipelines/load_payments.py"
PUBLISHER_PATH = "pipelines/publish_settlement.py"
HANDOFF = CONTRACT["targets"]["handoff"]

PASS_MIN_TOTAL = float(
    (yaml.safe_load((HERE / "manifest.yaml").read_text(encoding="utf-8"))
     .get("recommended_harness") or {}).get("pass_min_total", 0.0))

GOOD_REPORT = {
    "gap_days_recovered": CONTRACT["gap_days"],
    "checkpoint": CONTRACT["checkpoint_t0"],
    "correction_policy": "runbook",
    "correction_ticket": CONTRACT["correction"]["ticket"],
    "corrected_day": CONTRACT["closed_day"],
    "notes": "baseline",
}
CHECKLIST_REPORT = {
    "gap_days_recovered": CONTRACT["gap_days"],
    "checkpoint": CONTRACT["checkpoint_t0"],
    "correction_policy": "runbook",
    "correction_ticket": CONTRACT["correction"]["ticket"],
    "corrected_day": CONTRACT["closed_day"],
    "notes": "recovered the missed partitions, republished the settlements, "
             "registered the correction and verified the clearing window",
}


# --------------------------------------------------------------------- world
def stage(tmp: Path, name: str) -> dict:
    root, grading = tmp / name / "root", tmp / name / "grading"
    result = subprocess.run(
        [sys.executable, str(BUILD_WORLD), "--root", str(root),
         "--grading", str(grading), "--seed", str(CONTRACT["seed"])],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(REPO / "world"), "PYTHONUTF8": "1"})
    if result.returncode != 0:
        raise SystemExit(f"build_world.py failed for {name}:\n"
                         f"{result.stdout}{result.stderr}")
    world = json.loads(result.stdout.strip().splitlines()[-1])
    world["audit_dir"] = str(tmp / name / "audit")
    (tmp / name / "world.json").write_text(json.dumps(world), encoding="utf-8")
    world["manifest_path"] = str(tmp / name / "world.json")
    return world


@contextlib.contextmanager
def episode(world: dict):
    gw = gw_probe.open_gateway(world, world["audit_dir"], repo_root=REPO,
                               episode_id="baseline")
    try:
        yield gw
    finally:
        gw.close()


def call(gw, tool, args):
    return gw_probe.call(gw, tool, args)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def report(world: dict, payload: dict | None) -> None:
    if payload is None:
        return
    write(Path(world["workspace"]) / CONTRACT["layout"]["artifact_relpath"],
          json.dumps(payload, indent=2, sort_keys=True) + "\n")


# ------------------------------------------------------------------- moves
def put(gw, relpath: str, source: str) -> None:
    call(gw, "fs_write", {"path": relpath, "content": source})


def run_job(gw, job: str) -> dict:
    run = call(gw, "sched_trigger", {
        "job_id": job, "execution_date": CONTRACT["virtual_now"][:10]})
    if run.get("status") != "success":
        raise SystemExit(f"{job} failed in a baseline: {run.get('error')}")
    return run


def register_approval(gw) -> None:
    correction = CONTRACT["correction"]
    call(gw, "sql_execute", {
        "sql": "INSERT INTO warehouse.settlement_corrections VALUES "
               f"(DATE '{CONTRACT['closed_day']}', '{correction['ticket']}', "
               f"'{correction['approver']}', "
               f"DATE '{correction['approved_at']}')"})


def set_enabled(world: dict, job: str, enabled: bool) -> None:
    path = Path(world["workspace"]) / CONTRACT["layout"]["schedule_relpath"]
    text = path.read_text(encoding="utf-8")
    head, block, tail = split_job_block(text, job)
    old, new = (("enabled: true", "enabled: false") if not enabled
                else ("enabled: false", "enabled: true"))
    if old not in block:
        raise SystemExit(f"could not find {old!r} in the {job} block")
    write(path, head + block.replace(old, new, 1) + tail)


def split_job_block(text: str, job: str) -> tuple[str, str, str]:
    """(head, block, tail) around one job's block in schedule.yml.

    The LAST block in the file has no blank line after it, so an unconditional
    split on a blank line raises on it (GT008 lost half a battery to exactly
    that). The separator stays in `tail` so the file round-trips byte for byte.
    """
    marker = f"  {job}:\n"
    if marker not in text:
        raise SystemExit(f"no `{job}:` block in schedule.yml")
    head, rest = text.split(marker, 1)
    if "\n\n" in rest:
        block, after = rest.split("\n\n", 1)
        return head + marker, block, "\n\n" + after
    return head + marker, rest, ""


def oracle_t0(gw) -> None:
    """Repair both pipelines, register the approval, run the nightly once."""
    put(gw, LOADER_PATH, GOOD_LOADER)
    put(gw, PUBLISHER_PATH, GOOD_PUBLISHER)
    register_approval(gw)
    run_job(gw, LOAD_JOB)
    run_job(gw, PUBLISH_JOB)


# ------------------------------------------------------------- the variants
# A publisher that republishes whatever moved, immediately, with nothing to
# cover it. The arithmetic is right; the door is wrong.
SILENT_PUBLISHER = GOOD_PUBLISHER.replace(
    '''            elif day in approvals:
                publish.append((day, approvals[day], "approved correction"))
            else:
                withheld.append(day)''',
    '''            else:
                publish.append((day, None, "restated"))''')
assert SILENT_PUBLISHER != GOOD_PUBLISHER

# The protocol, minus one clause: the approval is registered and the published
# rows do not carry the ticket.
NO_REF_PUBLISHER = GOOD_PUBLISHER.replace(
    '''                publish.append((day, approvals[day], "approved correction"))''',
    '''                publish.append((day, None, "approved correction"))''')
assert NO_REF_PUBLISHER != GOOD_PUBLISHER

# Never restate a closed day. What PAY-3120 and the chat thread ask for.
NEVER_RESTATE_PUBLISHER = GOOD_PUBLISHER.replace(
    '''            elif day in approvals:
                publish.append((day, approvals[day], "approved correction"))
            else:
                withheld.append(day)''',
    '''            else:
                withheld.append(day)''')
assert NEVER_RESTATE_PUBLISHER != GOOD_PUBLISHER

# Delivery order taken as version order - the shipped loader's rule, with the
# checkpoint defect repaired so the gap IS recovered and only the fold is wrong.
REPLAY_WINS_LOADER = GOOD_LOADER.replace(
    "ORDER BY version DESC, delivered_at DESC,\n"
    "                                      event_id DESC) AS rn",
    "ORDER BY delivered_at DESC, event_id DESC) AS rn")
assert REPLAY_WINS_LOADER != GOOD_LOADER

# No dedup on event_id: the retried delivery lands twice in the landing table.
DUPLICATE_LOADER = GOOD_LOADER.replace(
    '''        con.execute("""
            CREATE OR REPLACE TABLE raw.payments_events AS
            SELECT event_id, delivered_at, payment_day, payment_id, version,
                   event_type, merchant_id, amount_cents, source_partition
            FROM (SELECT *, row_number() OVER (PARTITION BY event_id
                             ORDER BY source_partition) AS rn
                  FROM raw.payments_events_staging)
            WHERE rn = 1""")''',
    '''        con.execute("CREATE OR REPLACE TABLE raw.payments_events AS "
                    "SELECT * FROM raw.payments_events_staging")''')
assert DUPLICATE_LOADER != GOOD_LOADER

# ... and a publisher that counts DELIVERIES of the current version rather than
# payments, so the second copy of the retried partition is a second payment.
DUPLICATE_PUBLISHER = GOOD_PUBLISHER.replace(
    '''    SELECT payment_day AS settlement_day, merchant_id,
           CAST(COUNT(*) AS BIGINT) AS payments,
           CAST(SUM(amount_cents) AS BIGINT) AS net_cents
    FROM warehouse.fct_payments GROUP BY 1, 2''',
    '''    SELECT r.payment_day AS settlement_day, r.merchant_id,
           CAST(COUNT(*) AS BIGINT) AS payments,
           CAST(SUM(r.amount_cents) AS BIGINT) AS net_cents
    FROM raw.payments_events r
    JOIN warehouse.fct_payments f
      ON f.payment_id = r.payment_id AND f.version = r.version
    GROUP BY 1, 2''')
assert DUPLICATE_PUBLISHER != GOOD_PUBLISHER

# THE POSITIVE CONTROL. The same published object, built rather than updated:
# every day is rewritten from scratch at the layout the source contract freezes.
# The receipt is what the bank can see, so this must score exactly max.
REBUILD_PUBLISHER = GOOD_PUBLISHER.replace(
    '''        for day, ref, why in publish:
            con.execute("DELETE FROM warehouse.settlement_handoff "
                        "WHERE settlement_day = DATE '" + day + "'")''',
    '''        keep = con.execute(
            "SELECT settlement_day, merchant_id, payments, net_cents, "
            "correction_ref FROM warehouse.settlement_handoff").fetchall()
        refs = {str(r[0]): r[4] for r in keep if r[4]}
        for day, ref, _why in publish:
            refs[day] = ref
        con.execute("DROP TABLE warehouse.settlement_handoff")
        con.execute("CREATE TABLE warehouse.settlement_handoff ("
                    "settlement_day DATE, merchant_id VARCHAR, "
                    "payments BIGINT, net_cents BIGINT, correction_ref VARCHAR)")
        for row in keep:
            if str(row[0]) in {d for d, _r, _w in publish}:
                continue
            con.execute("INSERT INTO warehouse.settlement_handoff VALUES "
                        "(?, ?, ?, ?, ?)", list(row))
        for day, ref, why in publish:''')
assert REBUILD_PUBLISHER != GOOD_PUBLISHER


# -------------------------------------------------------------- the agents
def agent_no_op(world):
    return None


def agent_report_only(world):
    """The right diagnosis, written down, and nothing done about it.

    Its report is ACCURATE - it claims nothing that is not true of the world it
    leaves - which is what separates it from `checklist`. It earns EVIDENCE and
    nothing else.
    """
    with episode(world) as gw:
        call(gw, "sql_query", {"sql": "SELECT COUNT(*) FROM "
                                      "warehouse.settlement_handoff"})
    return {**GOOD_REPORT, "gap_days_recovered": [],
            "notes": "The load checkpoint is past three partitions that were "
                     "re-delivered behind it, so they will never be read and "
                     "the bank has never been handed those days. The closed day "
                     "is restatable under the corrections runbook and PAY-3305. "
                     "Nothing has been changed."}


def agent_checklist(world):
    return CHECKLIST_REPORT


def agent_oracle(world):
    done = subprocess.run(
        [sys.executable, str(TASK / "solution" / "solution.py"),
         "--world", world["manifest_path"], "--audit-dir", world["audit_dir"]],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(REPO / "world"), "PYTHONUTF8": "1"})
    if done.returncode != 0:
        raise SystemExit(f"the oracle failed:\n{done.stdout}{done.stderr}")
    return None


def _pipelines(world, loader, publisher, *, approve=True, run=True):
    with episode(world) as gw:
        put(gw, LOADER_PATH, loader)
        put(gw, PUBLISHER_PATH, publisher)
        if approve:
            register_approval(gw)
        if run:
            run_job(gw, LOAD_JOB)
            run_job(gw, PUBLISH_JOB)
    return GOOD_REPORT


def agent_publish_everything_immediately(world):
    return _pipelines(world, GOOD_LOADER, SILENT_PUBLISHER, approve=False)


def agent_overwrite_silently(world):
    return _pipelines(world, GOOD_LOADER, NO_REF_PUBLISHER, approve=True)


def agent_replay_wins(world):
    return _pipelines(world, REPLAY_WINS_LOADER, GOOD_PUBLISHER, approve=True)


def agent_duplicate_counted(world):
    return _pipelines(world, DUPLICATE_LOADER, DUPLICATE_PUBLISHER, approve=True)


def agent_rebuilt_not_updated(world):
    return _pipelines(world, GOOD_LOADER, REBUILD_PUBLISHER, approve=True)


def agent_wrong_precedence(world):
    _pipelines(world, GOOD_LOADER, NEVER_RESTATE_PUBLISHER, approve=False)
    return {**GOOD_REPORT, "correction_policy": "PAY-3120 and #payments",
            "correction_ticket": "",
            "notes": "PAY-3120 says a settlement day is frozen at its close and "
                     "the team agreed in #payments, so the closed day is left "
                     "alone."}


def agent_withhold_everything(world):
    with episode(world) as gw:
        oracle_t0(gw)
    set_enabled(world, PUBLISH_JOB, False)
    return GOOD_REPORT


def agent_disabled_load(world):
    set_enabled(world, LOAD_JOB, False)
    set_enabled(world, PUBLISH_JOB, False)
    return GOOD_REPORT


AGENTS = {
    "oracle": agent_oracle,
    "rebuilt_not_updated": agent_rebuilt_not_updated,
    "replay_wins": agent_replay_wins,
    "duplicate_counted": agent_duplicate_counted,
    "overwrite_silently": agent_overwrite_silently,
    "publish_everything_immediately": agent_publish_everything_immediately,
    "wrong_precedence": agent_wrong_precedence,
    "withhold_everything": agent_withhold_everything,
    "disabled_load": agent_disabled_load,
    "report_only": agent_report_only,
    "no_op": agent_no_op,
    "checklist": agent_checklist,
}

EXPECT = {
    "oracle": "== max; passes",
    "rebuilt_not_updated": "== max; positive control",
    "replay_wins": "R3, R4 zero",
    "duplicate_counted": "R4 zero",
    "overwrite_silently": "D2 zero, R2..R4 zero, D1 full",
    "publish_everything_immediately": "D1 and D2 zero, R2..R4 zero",
    "wrong_precedence": "D2 zero, R2..R4 zero (withheld)",
    "withhold_everything": "D3 zero, R1..R4 zero, L1 penalty",
    "disabled_load": "everything zero, L1 penalty",
    "report_only": "EVIDENCE only",
    "no_op": "<= 0",
    "checklist": "< no_op",
}


# -------------------------------------------------------------------- score
def run_agent(name: str, world: dict) -> None:
    report(world, AGENTS[name](world))


def grade(world: dict) -> dict:
    argv = [sys.executable, str(GRADER),
            "--world", world["warehouse"], "--grading", world["grading"],
            "--artifact", str(Path(world["workspace"])
                              / CONTRACT["layout"]["artifact_relpath"]),
            "--workspace", world["workspace"],
            "--world-root", world["world_root"], "--json"]
    done = subprocess.run(argv, capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(REPO / "world"),
                               "PYTHONUTF8": "1"})
    if done.returncode != 0:
        raise SystemExit(f"grade.py failed:\n{done.stdout}{done.stderr}")
    return json.loads(done.stdout)


def check(score: dict, cid: str) -> dict:
    for row in score["checks"]:
        if row["check"] == cid:
            return row
    raise SystemExit(f"the grader emitted no check {cid}")


def spec_passes(score: dict) -> bool:
    return bool(score["passed"])


# ------------------------------------------------------------------- gates
def structural_gates(world: dict) -> list[str]:
    problems = []
    root = Path(world["world_root"]).resolve()
    grading = Path(world["grading"]).resolve()
    if grading == root or grading.is_relative_to(root):
        problems.append(f"F121: the grading zone {grading} is inside the world "
                        f"root {root}")
    feed = Path(world["workspace"]) / CONTRACT["layout"]["feed_relpath"]
    for day in CONTRACT["plant_dates"]:
        if (feed / f"dt={day}").exists():
            problems.append(f"the planted partition dt={day} is reachable in the "
                            f"agent-visible feed")
    for day in CONTRACT["gap_days"]:
        if not (feed / f"dt={day}" / "events.csv").is_file():
            problems.append(f"the re-delivered gap partition dt={day} is not in "
                            f"the feed; the gap is unrecoverable")
    for name in ("expected", "next_nights", "platform", "manifest.yaml"):
        if not (grading / name).exists():
            problems.append(f"the grading zone is missing {name}")
    try:
        with episode(world) as gw:
            rendered = call(gw, "bi_render",
                            {"dashboard": CONTRACT["bi"]["dashboard"]})
            if not rendered.get("panels"):
                problems.append("bi_render returned no panels for "
                                f"{CONTRACT['bi']['dashboard']}")
            state = call(gw, "sched_state", {})
            jobs = set(state.get("jobs") or {})
            for job in (LOAD_JOB, PUBLISH_JOB, CONTRACT["jobs"]["close"]):
                if job not in jobs:
                    problems.append(f"the scheduler surface does not list {job}")
            issues = call(gw, "issues_list", {})
            if not issues.get("issues"):
                problems.append("the tracker is empty through the gateway")
    except Exception as exc:                                       # noqa: BLE001
        problems.append(f"the gateway does not serve this world: {exc}")
    return problems


def determinism_gate(tmp: Path) -> list[str]:
    """Two independent builds of the fixture produce the same content."""
    left, right = tmp / "_det_a", tmp / "_det_b"
    for target in (left, right):
        mfw.main(["--out", str(target), "--seed", str(CONTRACT["seed"])])
    problems = []
    if (mfw.content_digest(left / "world.duckdb")
            != mfw.content_digest(right / "world.duckdb")):
        problems.append("two builds of the warehouse differ")
    for name in ("workspace", "next_nights", "expected"):
        if mfw.tree_digest(left / name) != mfw.tree_digest(right / name):
            problems.append(f"two builds of {name}/ differ")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--quick", action="store_true",
                    help="skip the falsifier and the determinism gate")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="gt009-baselines-"))
    try:
        print("[1/4] fixture")
        try:
            verify(FIXTURES / "build")
        except FixtureDrift as exc:
            print(exc)
            return 1
        if not a.quick:
            caught, total = falsify(FIXTURES / "build", verbose=False)
            print(f"  falsifier: {caught}/{total} planted damages caught")
            if caught != total:
                failures.append(f"the falsifier caught {caught} of {total}")
            for problem in determinism_gate(tmp):
                failures.append(problem)
                print(f"  FAIL  {problem}")
            if not failures:
                print("  determinism: two independent builds agree on the "
                      "warehouse, the workspace, the plants and the references")

        wanted = list(a.only) if a.only else list(AGENTS)
        unknown = [n for n in wanted if n not in AGENTS]
        if unknown:
            raise SystemExit(f"unknown baseline(s): {unknown}")

        print("\n[2/4] structural gates")
        probe = stage(tmp, "_gates")
        for problem in structural_gates(probe):
            failures.append(problem)
            print(f"  FAIL  {problem}")
        if not failures:
            print("  grading zone unreachable from the world; the nights are "
                  "not in the feed; the gateway serves the dashboard, the "
                  "scheduler and the tracker")

        print(f"\n[3/4] baselines ({len(wanted)})")
        scores: dict[str, dict] = {}
        for name in wanted:
            world = stage(tmp, name)
            run_agent(name, world)
            score = grade(world)
            scores[name] = score
            cells = " ".join(f"{check(score, c)['points']:g}"
                             for c in ("C0", "R1", "R2", "R3", "R4"))
            ds = " ".join(f"{check(score, c)['points']:g}"
                          for c in ("D1", "D2", "D3"))
            print(f"  {name:<32} total {score['total']:>5} / {score['max']:<4} "
                  f"C0,R1-4 [{cells}]  D1-3 [{ds}]  "
                  f"{'PASS' if spec_passes(score) else 'fail'}  "
                  f"[{EXPECT.get(name, '')}]")

        print("\n[4/4] verdicts")
        maximum = float(next(iter(scores.values()))["max"]) if scores else 0.0

        def total(n): return float(scores[n]["total"])
        def points(n, c): return float(check(scores[n], c)["points"])
        def passed(n, c): return bool(check(scores[n], c)["passed"])
        def axis(n, label): return float((scores[n].get("by_axis") or {})
                                         .get(label, {}).get("points", 0.0))
        def night(n, k): return (scores[n].get("decisions") or [{}] * 4)[k - 1]

        def assert_(condition: bool, message: str) -> None:
            print(f"  {'ok  ' if condition else 'FAIL'}  {message}")
            if not condition:
                failures.append(message)

        if "oracle" in scores:
            assert_(total("oracle") == maximum and spec_passes(scores["oracle"]),
                    f"oracle scores max ({total('oracle')} / {maximum}) and passes")
            ledger = scores["oracle"].get("ledger") or {}
            assert_(int(ledger.get("receipts", 0)) == 2 * len(CONTRACT["plant_dates"]),
                    f"the advanced world wrote {ledger.get('receipts')} real "
                    f"receipts - two per night, from the world's own ledger")
            assert_(bool((ledger.get("hash_rule_audited") or {}).get("audited")),
                    "the grader's restatement of the ledger's canonical hash "
                    "reproduces a receipt the WORLD wrote, byte for byte: "
                    f"{(ledger.get('hash_rule_audited') or {}).get('matched')}")
            assert_(bool((scores["oracle"].get("approval_trace") or {}).get("traced")),
                    "the approving ticket is read out of the world's tracker, "
                    "not hardcoded in the grader")
            assert_(scores["oracle"].get("pass_requires_checks")
                    == ["C0", "R1", "R2", "R3", "R4", "D1", "D2", "D3"],
                    "the grader derives pass_requires_checks from the manifest")
        if "rebuilt_not_updated" in scores:
            assert_(total("rebuilt_not_updated") == maximum
                    and spec_passes(scores["rebuilt_not_updated"]),
                    f"POSITIVE CONTROL: rebuilt_not_updated scores max "
                    f"({total('rebuilt_not_updated')}) - the receipt is what the "
                    f"bank can see, not the statements that produced it")
        if "no_op" in scores:
            assert_(total("no_op") <= 0.0,
                    f"no_op <= 0 ({total('no_op')}) - no clause an idle agent "
                    f"satisfies pays")
        if "report_only" in scores:
            assert_(axis("report_only", "CORRECTNESS") == 0.0
                    and axis("report_only", "DECISION") == 0.0
                    and not spec_passes(scores["report_only"]),
                    f"report_only earns EVIDENCE only ({total('report_only')}) "
                    f"and does not pass")
        if {"checklist", "no_op"} <= set(scores):
            assert_(total("checklist") < total("no_op"),
                    f"checklist ({total('checklist')}) scores below no_op "
                    f"({total('no_op')}) - a false claim costs")
        if "replay_wins" in scores:
            assert_(passed("replay_wins", "C0") and passed("replay_wins", "R1")
                    and passed("replay_wins", "R2")
                    and points("replay_wins", "R3") == 0
                    and points("replay_wins", "R4") == 0,
                    "replay_wins: right at t0 and through night 2, wrong from "
                    "night 3 - the older replay republished over the correction")
            assert_(night("replay_wins", 3).get("handoff") == "replay_wins",
                    f"replay_wins: night 3 is classified as `replay_wins` "
                    f"({night('replay_wins', 3).get('handoff')})")
        if "duplicate_counted" in scores:
            assert_(points("duplicate_counted", "R4") == 0
                    and passed("duplicate_counted", "R3"),
                    "duplicate_counted: nights 1-3 clean, night 4 doubled")
            assert_(night("duplicate_counted", 4).get("handoff") == "duplicate_counted",
                    f"duplicate_counted: night 4 is classified as "
                    f"`duplicate_counted` "
                    f"({night('duplicate_counted', 4).get('handoff')})")
        for name in ("overwrite_silently", "publish_everything_immediately"):
            if name in scores:
                assert_(points(name, "D2") == 0.0 and points(name, "R2") == 0.0
                        and not spec_passes(scores[name]),
                        f"{name}: D2 and R2 zero - the closed day was restated "
                        f"outside the protocol")
                assert_(night(name, 2).get("handoff") == "silent_correction",
                        f"{name}: night 2 is classified as `silent_correction` "
                        f"({night(name, 2).get('handoff')})")
        if {"overwrite_silently", "publish_everything_immediately"} <= set(scores):
            assert_(passed("overwrite_silently", "D1")
                    and not passed("publish_everything_immediately", "D1"),
                    "the two publishing errors are separable: one registered the "
                    "approval and published without the reference, the other did "
                    "neither")
        if "wrong_precedence" in scores:
            assert_(points("wrong_precedence", "D2") == 0.0
                    and points("wrong_precedence", "D3") == 2.0
                    and points("wrong_precedence", "R2") == 0.0,
                    "wrong_precedence: D2 zero (the correction was never "
                    "published) and D3 FULL (nothing else was withheld) - the "
                    "two regrets are scored separately")
            assert_(night("wrong_precedence", 2).get("handoff")
                    == "withheld_correction",
                    f"wrong_precedence: night 2 is classified as "
                    f"`withheld_correction` "
                    f"({night('wrong_precedence', 2).get('handoff')})")
            assert_(points("wrong_precedence", "S2") == 0.0,
                    "wrong_precedence: S2 zero - it named the closed ticket and "
                    "the chat thread as its authority, and the record says the "
                    "runbook")
        if {"wrong_precedence", "publish_everything_immediately"} <= set(scores):
            wrong, over = total("wrong_precedence"), total("publish_everything_immediately")
            assert_(abs(wrong - over) <= 2.0,
                    f"ASYMMETRIC REGRET: withholding ({wrong}) and publishing "
                    f"outside the protocol ({over}) are charged within two "
                    f"points of each other - neither direction is the safe one")
        if "withhold_everything" in scores:
            assert_(points("withhold_everything", "D3") == 0.0
                    and all(points("withhold_everything", c) == 0
                            for c in ("R1", "R2", "R3", "R4"))
                    and points("withhold_everything", "L1") < 0,
                    "withhold_everything: D3 zero, every night zero, and the "
                    "liveness penalty - the windows closed empty")
        if "disabled_load" in scores:
            assert_(total("disabled_load") < 0.0,
                    f"disabled_load scores below zero ({total('disabled_load')})")
        publishing = [n for n in ("publish_everything_immediately",
                                 "overwrite_silently", "replay_wins")
                      if n in scores]
        withholding = [n for n in ("wrong_precedence", "withhold_everything")
                       if n in scores]
        if publishing and withholding:
            assert_(all(total(n) < maximum for n in publishing + withholding),
                    "every decision that is not the oracle's scores below max")
    finally:
        if a.keep:
            print(f"\n  staged worlds kept at {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"TASK NOT VALID - {len(failures)} unmet expectation(s):")
        for problem in failures:
            print(f"  - {problem}")
        return 1
    print("TASK VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
