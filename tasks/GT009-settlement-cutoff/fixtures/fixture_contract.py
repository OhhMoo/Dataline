#!/usr/bin/env python3
"""GT009 fixture contract - what the world must be, checked against the world.

    python fixture_contract.py            # verify the built fixture
    python fixture_contract.py --falsify  # plant damage, require verify to catch it
    python fixture_contract.py --probe-grader  # tests/grade.py names a wrong table right

`CONTRACT` is the single source of every constant the package shares: the
grader, the baselines, the solution and the prompts all read it, and none of
them restates a date, a table name or a figure.

`verify(build)` is the gate. Two of its checks are MEASUREMENTS rather than
assertions, and they are the two that matter:

  * `_problems_mechanism` RUNS the shipped loader, the shipped publisher and
    the platform's clearing-window daemon over the four planted nights, on a
    throwaway copy, and requires the result to be the `shipped_unchanged`
    trajectory the generator computed independently. That is what proves the
    world's own code is wrong in exactly the way the task claims - and, in the
    same run, that the generator's pure-python model of the daemon
    (`make_fixture_world.close_window`) is the daemon.
  * `_problems_receipts` advances the real world scheduler over one planted
    night and requires `var/ledger/receipts.jsonl` to carry the two receipts
    this world's `publishes:` blocks declare, at the right targets and the
    right consumer. GT008 shipped a `publishes:` annotation that no ledger
    had ever read; this one is read on every verify.

A falsifier mutation that `verify` does not catch is a hole in the gate, so
`--falsify` requires each one to be caught by a NAMED clause.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parent.parent

sys.path.insert(0, str(HERE))
import make_fixture_world as mfw                                   # noqa: E402
import schedule_model                                              # noqa: E402

HANDOFF = mfw.HANDOFF
WINDOW = mfw.WINDOW


class FixtureDrift(SystemExit):
    """The built world is not the world this package grades against."""


# ===========================================================================
# CONTRACT
# ===========================================================================
CONTRACT = {
    "task": "GT009-settlement-cutoff",
    "seed": mfw.SEED,
    "virtual_now": mfw.VNOW,
    "vtime_start": mfw.VTIME_START,
    "history_days": mfw.HIST_DAYS,
    "plant_nights": mfw.PLANT_NIGHTS,
    "advance_hours_per_night": mfw.ADVANCE_HOURS_PER_NIGHT,
    "plant_dates": [mfw.day_of(mfw.HIST_DAYS + k).isoformat()
                    for k in range(mfw.PLANT_NIGHTS)],
    "gap_days": [mfw.day_of(i).isoformat() for i in mfw.GAP_INDEXES],
    "closed_day": mfw.day_of(mfw.CLOSED_DAY_INDEX).isoformat(),
    "checkpoint_t0": mfw.CHECKPOINT_T0,
    "correction": {"ticket": mfw.CORRECTION_TICKET,
                   "approver": mfw.CORRECTION_APPROVER,
                   "approved_at": mfw.CORRECTION_APPROVED_AT,
                   "payments": mfw.CORRECTED_PAYMENTS},
    "consumer": mfw.CONSUMER,
    "cutoff_time": mfw.CUTOFF_TIME,
    "targets": {"handoff": HANDOFF, "window": WINDOW,
                "facts": "warehouse.fct_payments",
                "corrections": "warehouse.settlement_corrections",
                "checkpoint": "warehouse.load_checkpoint",
                "events": "raw.payments_events"},
    "jobs": {"load": "payments_nightly", "publish": "settlement_publish",
             "close": schedule_model.PLATFORM_JOB,
             "earliest_start": mfw.EARLIEST_START},
    "durations": mfw.DURATIONS,
    "default_duration_s": mfw.DEFAULT_DURATION_S,
    "layout": {
        "feed_relpath": mfw.FEED_DIR,
        "platform_relpath": ".platform",
        "platform_script": schedule_model.PLATFORM_SCRIPT,
        "schedule_relpath": "orchestration/schedule.yml",
        "warehouse_relpath": "var/warehouse/warehouse.duckdb",
        "scheduler_relpath": "var/sched/state.json",
        "live_relpath": "var/world/live.json",
        "dashboards_relpath": "var/bi/dashboards",
        "ledger_relpath": "var/ledger/receipts.jsonl",
        "artifact_relpath": "final_report.json",
    },
    "bi": {"dashboard": "clearing_settlement"},
    "variants": ["oracle", "no_recovery", "withheld_correction",
                 "silent_correction", "replay_wins", "shipped_unchanged",
                 "duplicate_counted", "frozen_after_t0"],
    # Filled in by `verify` from the built world, and asserted stable across
    # rebuilds by tests/run_baselines.py's determinism gate.
    "world": {},
}

WORKFLOW_SPEC = {"dir": "workflow", "actor": "agent",
                 "project": {"key": "PAY", "name": "Payments platform"},
                 "statuses": ["open", "triage", "in_progress", "blocked",
                              "approved", "done", "wontfix"],
                 "workspace": "settlement-platform",
                 "space": {"key": "PAY", "name": "Payments"}}

REQUIRED_ISSUES = ("PAY-3120", "PAY-3240", "PAY-3271", "PAY-3288", "PAY-3297",
                   "PAY-3305")
REQUIRED_PAGES = ("pay/how-we-decide", "pay/runbooks/settlement-corrections",
                  "pay/runbooks/feed-recovery", "pay/notices/network-restatement")


# ===========================================================================
# helpers
# ===========================================================================
def _expected(build: Path, name: str) -> dict:
    path = Path(build) / "expected" / name
    if not path.is_file():
        raise FixtureDrift(f"{path} is missing; rebuild the fixture")
    return json.loads(path.read_text(encoding="utf-8"))


def _tables(db: Path) -> dict:
    """{table: sorted rows as strings} for everything in raw + warehouse."""
    import duckdb
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + Path(db).as_posix() + "' AS wh (READ_ONLY)")
    out = {}
    try:
        for schema, table in con.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema IN ('raw', 'warehouse') "
                "AND table_type = 'BASE TABLE' ORDER BY 1, 2").fetchall():
            rows = con.execute(f'SELECT * FROM wh."{schema}"."{table}"').fetchall()
            out[f"{schema}.{table}"] = _as_strings(rows)
    finally:
        con.close()
    return out


def _as_strings(rows) -> list:
    return sorted([["" if v is None else str(v) for v in row] for row in rows])


# ===========================================================================
# the checks
# ===========================================================================
def _problems_layout(fx: Path) -> list[str]:
    out = []
    for rel in ("world.duckdb", "workspace", "expected/t0.json",
                "next_nights", "workflow/issues.yaml",
                "obs/definitions.yaml", "bi/dashboards"):
        if not (fx / rel).exists():
            out.append(f"{rel} is missing from the build")
    workspace = fx / "workspace"
    for rel in (CONTRACT["layout"]["schedule_relpath"],
                CONTRACT["layout"]["platform_script"],
                "pipelines/load_payments.py", "pipelines/publish_settlement.py",
                "docs/settlement_contract_v3.md",
                "docs/runbook_settlement_nightly.md"):
        if not (workspace / rel).is_file():
            out.append(f"workspace/{rel} is missing")
    # F121-adjacent: the planted nights must not be inside the agent's feed
    feed = workspace / CONTRACT["layout"]["feed_relpath"]
    for day in CONTRACT["plant_dates"]:
        if (feed / f"dt={day}").exists():
            out.append(f"the planted partition dt={day} is in the agent-visible "
                       f"feed; the nights are the grader's")
    for day in CONTRACT["gap_days"]:
        if not (feed / f"dt={day}" / "events.csv").is_file():
            out.append(f"the re-delivered gap partition dt={day} is missing; "
                       f"the gap would then be unrecoverable")
    return out


def _problems_state(fx: Path) -> list[str]:
    """The world at t0 is the world the shipped code left behind."""
    refs = _expected(fx, "t0.json")
    tables = _tables(fx / "world.duckdb")
    out = []
    for table, want in ((CONTRACT["targets"]["facts"], refs["t0"]["fct"]),
                        (HANDOFF, refs["t0"][HANDOFF]),
                        (WINDOW, refs["t0"][WINDOW])):
        have = tables.get(table)
        if have is None:
            out.append(f"{table} is not in the built warehouse")
        elif have != _as_strings(want):
            out.append(f"{table} does not match the reference t0 state "
                       f"({len(have)} rows vs {len(want)})")
    checkpoint = tables.get(CONTRACT["targets"]["checkpoint"]) or []
    if [row[0] for row in checkpoint] != [CONTRACT["checkpoint_t0"]]:
        out.append(f"the load checkpoint is {checkpoint!r}, expected one row at "
                   f"{CONTRACT['checkpoint_t0']} - the gap IS the checkpoint")
    if tables.get(CONTRACT["targets"]["corrections"]):
        out.append("the correction register is not empty at t0; the approval "
                   "the protocol requires would already be written")
    breaches = [row for row in refs["t0"][WINDOW] if row[2] != mfw.SETTLED]
    if len(breaches) != len(CONTRACT["gap_days"]):
        out.append(f"t0 carries {len(breaches)} SLA_BREACH row(s), expected "
                   f"{len(CONTRACT['gap_days'])}")
    days_settled = {row[0] for row in refs["t0"][HANDOFF]}
    if days_settled & set(CONTRACT["gap_days"]):
        out.append("the gap days are already in the handoff at t0; there would "
                   "be nothing to recover")
    if not set(CONTRACT["gap_days"]) <= {row[0] for row in refs["oracle_t0"][HANDOFF]}:
        out.append("the oracle's t0 handoff does not cover the gap days")
    return out


def _problems_schedule(fx: Path) -> list[str]:
    workspace = fx / "workspace"
    out = []
    try:
        jobs = mfw.shipped_jobs(workspace)
    except schedule_model.ScheduleError as exc:
        return [f"the shipped schedule does not deploy: {exc}"]
    for name in (CONTRACT["jobs"]["load"], CONTRACT["jobs"]["publish"],
                 CONTRACT["jobs"]["close"]):
        if name not in jobs:
            out.append(f"job {name} is not deployed")
    publish = jobs.get(CONTRACT["jobs"]["publish"]) or {}
    declared = publish.get("publishes") or []
    if [entry.get("target") for entry in declared] != [HANDOFF]:
        out.append(f"{CONTRACT['jobs']['publish']} no longer publishes "
                   f"{HANDOFF}; the ledger would record nothing and the "
                   f"consumer axis would be ungradeable")
    if any(entry.get("consumer") != CONTRACT["consumer"] for entry in declared):
        out.append(f"the handoff is not published to {CONTRACT['consumer']}")
    close = jobs.get(CONTRACT["jobs"]["close"]) or {}
    if [entry.get("target") for entry in (close.get("publishes") or [])] != [WINDOW]:
        out.append("the clearing-window daemon no longer publishes "
                   f"{WINDOW}; the SLA record would not be a receipt")
    if close.get("time") != CONTRACT["cutoff_time"]:
        out.append(f"the cutoff moved to {close.get('time')!r}")
    if CONTRACT["jobs"]["close"] in schedule_model.enabled_jobs(
            mfw.load_schedule(workspace / CONTRACT["layout"]["schedule_relpath"])):
        out.append("the clearing-window daemon is deployable from schedule.yml; "
                   "the cutoff must not be the agent's to switch off")
    return out


def _problems_workflow(fx: Path) -> list[str]:
    corpus = fx / "workflow"
    if not corpus.is_dir():
        return [f"{corpus} is missing"]
    out = []
    sys.path.insert(0, str(REPO / "world"))
    try:
        from world import workflow as world_workflow
    except ImportError as exc:
        return [f"world/workflow.py is not importable: {exc}"]
    finally:
        sys.path.pop(0)
    try:
        docs = world_workflow.documents(fx, WORKFLOW_SPEC, CONTRACT["virtual_now"])
    except SystemExit as exc:
        return [f"the workflow corpus does not assemble: {exc}"]
    issues = {item["id"]: item for item in docs["issues.json"]["issues"]}
    for required in REQUIRED_ISSUES:
        if required not in issues:
            out.append(f"{required} is missing from the tracker")
    approval = issues.get(CONTRACT["correction"]["ticket"])
    if approval:
        if approval["status"] != "approved":
            out.append(f"{approval['id']} is {approval['status']!r}, not "
                       "approved; the protocol would have no approving ticket")
        if approval["reporter"] != CONTRACT["correction"]["approver"]:
            out.append(f"{approval['id']} is not raised by "
                       f"{CONTRACT['correction']['approver']}")
        if CONTRACT["closed_day"] not in approval["body"]:
            out.append(f"{approval['id']} no longer names {CONTRACT['closed_day']}")
    trap = issues.get("PAY-3120")
    if trap and trap["status"] != "wontfix":
        out.append("PAY-3120 is no longer closed wontfix; the interference "
                   "element is gone")
    pages = {page["id"]: page for page in docs["kb.json"]["pages"]}
    for required in REQUIRED_PAGES:
        if required not in pages:
            out.append(f"the wiki page {required} is missing")
    protocol = pages.get("pay/runbooks/settlement-corrections")
    if protocol and "PAY-3120" not in protocol["body"]:
        out.append("the corrections runbook no longer supersedes PAY-3120 by "
                   "name; the precedence question becomes undecidable")
    if protocol and "correction_ref" not in protocol["body"]:
        out.append("the corrections runbook no longer states the correction_ref "
                   "clause; the protocol would be unstated and the task unfair")
    if protocol and "never" not in protocol["body"]:
        out.append("the corrections runbook no longer distinguishes a day that "
                   "was never handed over from a restatement")
    decide = pages.get("pay/how-we-decide")
    if decide and "later" not in decide["body"]:
        out.append("the precedence page no longer states date precedence")
    return out


def _problems_obs(fx: Path) -> list[str]:
    import yaml
    path = fx / "obs" / "definitions.yaml"
    if not path.is_file():
        return [f"{path} is missing"]
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = []
    metrics = {m["id"] for m in doc.get("metrics") or []}
    for required in ("settled_days", "sla_breaches", "unsettled_days"):
        if required not in metrics:
            out.append(f"metric {required} is missing from the observability plane")
    if not doc.get("alarms"):
        out.append("the observability plane declares no alarms")
    dash = fx / "bi" / "dashboards" / f"{CONTRACT['bi']['dashboard']}.yml"
    if not dash.is_file():
        out.append(f"{dash} is missing; bi_render has nothing to serve")
    return out


def _problems_decision(fx: Path) -> list[str]:
    """What the DECISION rows of tests/grade.py rest on (D2, and its two
    directions D2a / D2b - MATRIX_RERUN_EDITS §0.10): the closed day IS closed
    at t0, the approved restatement MOVES its figures, the oracle's restated
    rows carry the ticket, and the `silent_correction` rival is the oracle's
    arithmetic with no correction_ref. If any of these is not true of the
    built references, D2a can never fire and D2b never separates a withheld
    day from a restated one."""
    closed = CONTRACT["closed_day"]
    ticket = CONTRACT["correction"]["ticket"]
    refs = _expected(fx, "t0.json")
    out = []
    settled = {row[1] for row in refs["t0"][WINDOW] if row[2] == mfw.SETTLED}
    if closed not in settled:
        out.append(f"{closed} has no SETTLED clearing-window row at t0; it is "
                   f"not a closed day, and restating it would need no protocol")
    t0_rows = [row for row in refs["t0"][HANDOFF] if row[0] == closed]
    if not t0_rows:
        out.append(f"the bank holds nothing for {closed} at t0; there is no "
                   f"figure to restate")
    # night 2 is the correction night (manifest `nights:`); nights are 0-based
    oracle = _expected(fx, "variant_oracle.json")["nights"][1]
    silent = _expected(fx, "variant_silent_correction.json")["nights"][1]
    o_rows = [row for row in oracle[HANDOFF] if row[0] == closed]
    s_rows = [row for row in silent[HANDOFF] if row[0] == closed]

    def figures(rows):
        return _as_strings([row[:4] for row in rows])

    if t0_rows and figures(o_rows) == figures(t0_rows):
        out.append("the oracle's night-2 restatement leaves the closed day's "
                   "figures where the bank held them at t0; the grader's "
                   "`moved` test (D2, D2a, D2b) could never see a restatement")
    if o_rows and {row[4] for row in o_rows} != {ticket}:
        out.append(f"the oracle's restated rows for {closed} do not all carry "
                   f"{ticket} in correction_ref; the protocol's third clause "
                   f"would not be in the reference")
    if any(row[4] for row in s_rows):
        out.append("the silent_correction trajectory carries a correction_ref "
                   "on the closed day; nothing but the ticket separates it from "
                   "the oracle, and that is D2a's clause")
    if figures(s_rows) != figures(o_rows):
        out.append("silent_correction's night-2 figures differ from the "
                   "oracle's; the rival is the right arithmetic through the "
                   "wrong door, or R2's classification names the wrong thing")
    return out


def _run_script(workspace: Path, script: str, db: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(workspace / script), str(db), str(workspace)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)


def _problems_mechanism(fx: Path) -> list[str]:
    """MEASURED on every verify: run the world's own code over the planted
    nights and require it to land on the `shipped_unchanged` trajectory."""
    want = _expected(fx, "variant_shipped_unchanged.json")["nights"]
    out = []
    scratch = Path(tempfile.mkdtemp(prefix="gt009-mech-"))
    try:
        workspace = scratch / "workspace"
        shutil.copytree(fx / "workspace", workspace)
        db = scratch / "world.duckdb"
        shutil.copy(fx / "world.duckdb", db)
        feed = workspace / CONTRACT["layout"]["feed_relpath"]
        for night, day in enumerate(CONTRACT["plant_dates"], start=1):
            shutil.copytree(fx / "next_nights" / "payments_feed" / f"dt={day}",
                            feed / f"dt={day}", dirs_exist_ok=True)
            for script in ("pipelines/load_payments.py",
                           "pipelines/publish_settlement.py",
                           CONTRACT["layout"]["platform_script"]):
                done = _run_script(workspace, script, db)
                if done.returncode != 0:
                    out.append(f"night {night}: {script} failed on the shipped "
                               f"world: {(done.stdout + done.stderr).strip()[:200]}")
                    return out
            tables = _tables(db)
            for table in (CONTRACT["targets"]["facts"], HANDOFF, WINDOW):
                key = "fct" if table == CONTRACT["targets"]["facts"] else table
                have, expect = tables.get(table), _as_strings(want[night - 1][key])
                if have != expect:
                    out.append(
                        f"night {night}: the shipped code left {len(have or [])} "
                        f"row(s) in {table}; the shipped_unchanged trajectory "
                        f"says {len(expect)}. Either the world's code or the "
                        f"generator's model of it has moved")
        # and the two properties the task rests on
        final = _tables(db)
        handoff_days = {row[0] for row in final[HANDOFF]}
        if handoff_days & set(CONTRACT["gap_days"]):
            out.append("the shipped code recovered the gap by itself; there is "
                       "no task")
        breaches = [row for row in final[WINDOW] if row[2] != mfw.SETTLED]
        if len(breaches) <= len(CONTRACT["gap_days"]):
            out.append("running the shipped code over four nights added no new "
                       "SLA_BREACH row; withholding would cost nothing")
        closed = [row for row in final[HANDOFF] if row[0] == CONTRACT["closed_day"]]
        oracle = _expected(fx, "variant_oracle.json")["nights"][-1][HANDOFF]
        want_closed = [row for row in oracle if row[0] == CONTRACT["closed_day"]]
        if _as_strings(closed) == _as_strings(want_closed):
            out.append("the shipped code produced the oracle's answer for the "
                       "closed day; the correction decision does not bite")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return out


def _problems_receipts(fx: Path) -> list[str]:
    """MEASURED on every verify: the world really writes receipts.

    Stages a world root, advances one planted night through the real
    `world.scheduler`, and reads `var/ledger/receipts.jsonl`. A `publishes:`
    annotation nobody has ever advanced over is a claim, not a surface.
    """
    out = []
    sys.path.insert(0, str(REPO / "world"))
    try:
        from world import scheduler
    except ImportError as exc:
        return [f"world/scheduler.py is not importable: {exc}"]
    finally:
        sys.path.pop(0)
    scratch = Path(tempfile.mkdtemp(prefix="gt009-ledger-"))
    try:
        root = scratch / "world"
        mfw.stage_world_root(fx, root)
        day = CONTRACT["plant_dates"][0]
        shutil.copytree(fx / "next_nights" / "payments_feed" / f"dt={day}",
                        root / "workspace" / CONTRACT["layout"]["feed_relpath"]
                        / f"dt={day}", dirs_exist_ok=True)
        scheduler.advance(root, CONTRACT["advance_hours_per_night"])
        ledger = root / CONTRACT["layout"]["ledger_relpath"]
        if not ledger.is_file():
            return ["one advanced night wrote no receipts: the world does not "
                    "publish, and every consumer clause is ungradeable"]
        receipts = [json.loads(line) for line in
                    ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        targets = [r["target"] for r in receipts]
        if targets != [HANDOFF, WINDOW]:
            out.append(f"night 1 published {targets}, expected [{HANDOFF!r}, "
                       f"{WINDOW!r}] in that order")
        for receipt in receipts:
            if receipt["consumer"] != CONTRACT["consumer"]:
                out.append(f"receipt {receipt['seq']} names consumer "
                           f"{receipt['consumer']!r}")
            if receipt["row_count"] in (None, 0):
                out.append(f"receipt {receipt['seq']} for {receipt['target']} "
                           f"carries row_count {receipt['row_count']!r}; the "
                           f"target did not resolve as a table")
            if receipt["day"] != day:
                out.append(f"receipt {receipt['seq']} is filed under day "
                           f"{receipt['day']!r}, expected {day!r}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return out


CHECKS = (("layout", _problems_layout), ("state", _problems_state),
          ("schedule", _problems_schedule), ("workflow", _problems_workflow),
          ("obs", _problems_obs), ("decision", _problems_decision),
          ("mechanism", _problems_mechanism), ("receipts", _problems_receipts))


def verify(build: Path, *, quiet: bool = False, skip=()) -> dict:
    build = Path(build)
    # Checks run in dependency order and the FIRST group to fire stops the
    # gate. A later group reads what an earlier one has just declared missing,
    # so carrying on would turn a named drift into a traceback.
    for name, check in CHECKS:
        if name in skip:
            continue
        found = check(build)
        if found:
            raise FixtureDrift("fixture drift:\n  - "
                               + "\n  - ".join(f"[{name}] {p}" for p in found))
    refs = _expected(build, "t0.json")
    CONTRACT["world"].update({
        "corpus_digest": mfw.tree_digest(build / "workflow"),
        "obs_digest": mfw.file_digest(build / "obs" / "definitions.yaml"),
        "workspace_digest": mfw.tree_digest(build / "workspace"),
        "warehouse": mfw.content_digest(build / "world.duckdb"),
        "handoff_rows_t0": len(refs["t0"][HANDOFF]),
        "handoff_rows_oracle_t0": len(refs["oracle_t0"][HANDOFF]),
        "payments_t0": len(refs["t0"]["fct"]),
        "payments_oracle_t0": len(refs["oracle_t0"]["fct"]),
    })
    if not quiet:
        world = CONTRACT["world"]
        print(f"fixture OK: {world['payments_t0']} payments held at t0 "
              f"({world['payments_oracle_t0']} after recovery), "
              f"{world['handoff_rows_t0']} handoff rows "
              f"({world['handoff_rows_oracle_t0']} after), "
              f"{len(CONTRACT['gap_days'])} breached days, "
              f"{CONTRACT['correction']['payments']} payments restated on night 2")
    return dict(CONTRACT)


# ===========================================================================
# the falsifier
# ===========================================================================
def _mutations() -> list[tuple[str, str, callable]]:
    """(name, clause it must fire, mutation applied to a copy of the build)."""
    lay, tgt = CONTRACT["layout"], CONTRACT["targets"]

    def drop_file(rel):
        def apply(fx):
            (fx / rel).unlink()
        return apply

    def edit(rel, old, new, count=1):
        def apply(fx):
            path = fx / rel
            text = path.read_text(encoding="utf-8")
            if old not in text:
                raise FixtureDrift(f"falsifier: {old!r} not in {rel}")
            with path.open("w", encoding="utf-8", newline="") as fh:
                fh.write(text.replace(old, new, count))
        return apply

    def sql(statement):
        def apply(fx):
            import duckdb
            con = duckdb.connect(":memory:", config={"threads": 1})
            con.execute("ATTACH '" + (fx / "world.duckdb").as_posix() + "' AS wh")
            con.execute("USE wh")
            con.execute(statement)
            con.execute("CHECKPOINT")
            con.close()
        return apply

    def edit_reference(name, mutate):
        """Rewrite one reference trajectory's closed-day handoff rows on
        nights 2..4 through `mutate(row) -> row`."""
        def apply(fx):
            path = fx / "expected" / name
            doc = json.loads(path.read_text(encoding="utf-8"))
            for entry in doc["nights"][1:]:
                entry[HANDOFF] = [mutate(list(row)) if row[0] == CONTRACT["closed_day"]
                                  else row for row in entry[HANDOFF]]
            with path.open("w", encoding="utf-8", newline="") as fh:
                fh.write(json.dumps(doc, indent=1, sort_keys=True) + "\n")
        return apply

    def restatement_moves_nothing(fx):
        """The oracle's nights 2..4 hold the closed day at its t0 figures
        (ticket still carried): a "restatement" the bank could not see."""
        t0 = {row[1]: row for row in _expected(fx, "t0.json")["t0"][HANDOFF]
              if row[0] == CONTRACT["closed_day"]}

        def mutate(row):
            return [row[0], row[1], t0[row[1]][2], t0[row[1]][3], row[4]]
        edit_reference("variant_oracle.json", mutate)(fx)

    workspace = "workspace"
    return [
        ("world_db_missing", "layout", drop_file("world.duckdb")),
        ("loader_missing", "layout",
         drop_file(f"{workspace}/pipelines/load_payments.py")),
        ("daemon_missing", "layout", drop_file(f"{workspace}/{lay['platform_script']}")),
        ("contract_doc_missing", "layout",
         drop_file(f"{workspace}/docs/settlement_contract_v3.md")),
        ("gap_partition_removed", "layout",
         drop_file(f"{workspace}/{lay['feed_relpath']}/"
                   f"dt={CONTRACT['gap_days'][0]}/events.csv")),
        ("planted_night_leaked", "layout",
         lambda fx: shutil.copytree(
             fx / "next_nights" / "payments_feed" / f"dt={CONTRACT['plant_dates'][0]}",
             fx / workspace / lay["feed_relpath"] / f"dt={CONTRACT['plant_dates'][0]}")),
        ("checkpoint_already_correct", "state",
         sql("UPDATE warehouse.load_checkpoint SET through_day = DATE '"
             + CONTRACT["gap_days"][0] + "'")),
        ("gap_already_settled", "state",
         sql("INSERT INTO warehouse.settlement_handoff VALUES (DATE '"
             + CONTRACT["gap_days"][0] + "', 'MER-ARDEN', 1, 1, NULL)")),
        ("approval_preloaded", "state",
         sql("INSERT INTO warehouse.settlement_corrections VALUES (DATE '"
             + CONTRACT["closed_day"] + "', '" + CONTRACT["correction"]["ticket"]
             + "', 'rkeane', DATE '" + CONTRACT["correction"]["approved_at"] + "')")),
        ("breach_erased", "state",
         sql("DELETE FROM warehouse.clearing_window WHERE status = 'SLA_BREACH'")),
        ("facts_perturbed", "state",
         sql("UPDATE warehouse.fct_payments SET amount_cents = amount_cents + 1 "
             "WHERE payment_id = (SELECT MIN(payment_id) FROM warehouse.fct_payments)")),
        ("publishes_stripped", "schedule",
         edit(f"{workspace}/{lay['schedule_relpath']}",
              "    publishes:\n", "    publishes_disabled:\n")),
        ("cutoff_moved", "schedule",
         lambda fx: _move_cutoff(fx)),
        ("daemon_made_deployable", "schedule",
         edit(f"{workspace}/{lay['schedule_relpath']}", "jobs:\n",
              "jobs:\n  clearing_window_close:\n    time: \"23:30\"\n"
              "    script: .platform/clearing_window_close.py\n"
              "    enabled: true\n\n")),
        ("publish_job_disabled", "schedule",
         edit(f"{workspace}/{lay['schedule_relpath']}",
              "    enabled: true\n    owner: data-platform\n    publishes:",
              "    enabled: false\n    owner: data-platform\n    publishes:")),
        ("approval_ticket_reopened", "workflow",
         edit("workflow/issues.yaml",
              "    status: approved\n    reporter: rkeane\n    assignee: dpatel\n"
              "    labels: [settlement, correction, approved]",
              "    status: open\n    reporter: rkeane\n    assignee: dpatel\n"
              "    labels: [settlement, correction, approved]")),
        ("interference_removed", "workflow",
         edit("workflow/issues.yaml", "    status: wontfix\n", "    status: done\n")),
        ("supersession_removed", "workflow",
         edit("workflow/kb.yaml", "**This page supersedes PAY-3120.**",
              "**This page is the current policy.**")),
        ("protocol_clause_removed", "workflow",
         edit("workflow/kb.yaml", "`correction_ref`", "the commit message", -1)),
        ("late_publication_rule_removed", "workflow",
         edit("workflow/kb.yaml",
              "A day the platform has **never** handed over is not being restated.",
              "Every republication of a past day is a restatement.")),
        ("precedence_page_gutted", "workflow",
         edit("workflow/kb.yaml", "1. **Date.** The later statement wins.",
              "1. **Judgement.** Use your best judgement.")),
        ("approval_day_renamed", "workflow",
         edit("workflow/issues.yaml", CONTRACT["closed_day"], "2026-06-04", -1)),
        ("metric_removed", "obs",
         edit("obs/definitions.yaml", "  - id: sla_breaches", "  - id: sla_breached")),
        ("dashboard_removed", "obs",
         drop_file(f"bi/dashboards/{CONTRACT['bi']['dashboard']}.yml")),
        # What D2 / D2a / D2b (tests/grade.py) rest on - the two directions
        # of the first regret need a restatement that moves figures and a
        # silent rival that differs from the oracle in the ticket alone.
        ("restatement_moves_nothing", "decision", restatement_moves_nothing),
        ("silent_correction_carries_ref", "decision",
         edit_reference("variant_silent_correction.json",
                        lambda row: row[:4] + [CONTRACT["correction"]["ticket"]])),
        ("oracle_restates_without_ref", "decision",
         edit_reference("variant_oracle.json", lambda row: row[:4] + [None])),
        ("loader_already_correct", "mechanism",
         edit(f"{workspace}/pipelines/load_payments.py",
              "ORDER BY delivered_at DESC, event_id DESC",
              "ORDER BY version DESC, delivered_at DESC, event_id DESC")),
        ("loader_reads_the_whole_feed", "mechanism",
         edit(f"{workspace}/pipelines/load_payments.py",
              'if day > after and any(folder.glob("*.csv")):',
              'if any(folder.glob("*.csv")):')),
        ("publisher_already_restates", "mechanism",
         edit(f"{workspace}/pipelines/publish_settlement.py",
              "WHERE payment_day > (SELECT COALESCE(MAX(settlement_day),\n"
              "                                                 DATE '1970-01-01')\n"
              "                                 FROM warehouse.settlement_handoff)\n",
              "")),
        ("daemon_never_breaches", "mechanism",
         edit(f"{workspace}/{lay['platform_script']}",
              'row = (closed_on, day, "SLA_BREACH", 0, 0)',
              'continue')),
        ("daemon_records_nothing", "mechanism",
         edit(f"{workspace}/{lay['platform_script']}",
              "if day > closed_on or day in settled:", "if True:")),
        ("events_table_renamed", "mechanism",
         sql(f"ALTER TABLE {tgt['events']} RENAME TO payments_events_old")),
    ]


def _move_cutoff(_fx: Path) -> None:
    """The cutoff is a module constant, not a line in the fixture, so the
    mutation has to reach the module the fixture reads it through. Restored by
    the falsifier's finally block."""
    schedule_model.PLATFORM_TIME = "21:00"


def falsify(build: Path, *, verbose: bool = True) -> tuple[int, int]:
    build = Path(build)
    mutations = _mutations()
    caught = 0
    original = schedule_model.PLATFORM_TIME
    for name, clause, apply in mutations:
        scratch = Path(tempfile.mkdtemp(prefix="gt009-falsify-"))
        try:
            fx = scratch / "build"
            shutil.copytree(build, fx)
            try:
                apply(fx)
            except FixtureDrift as exc:
                if verbose:
                    print(f"  SETUP  {name}: {exc}")
                continue
            try:
                verify(fx, quiet=True)
                if verbose:
                    print(f"  MISSED {name} (expected [{clause}] to fire)")
            except FixtureDrift as exc:
                if f"[{clause}]" in str(exc):
                    caught += 1
                    if verbose:
                        print(f"  caught {name} -> {clause}")
                elif verbose:
                    print(f"  WRONG  {name}: caught, but not by [{clause}]: "
                          f"{str(exc).splitlines()[1].strip()}")
        finally:
            schedule_model.PLATFORM_TIME = original
            shutil.rmtree(scratch, ignore_errors=True)
    return caught, len(mutations)


# ===========================================================================
# the grader's classifier, probed
# ===========================================================================
def _grader_probes() -> list[tuple[str, str, str, tuple, tuple]]:
    """(name, SQL over the oracle's night-1 clearing window, the label
    tests/grade.py:classify must give the result, words its detail must
    carry, words it must not).

    What `grade._name_difference` rests on: layout-vs-content is decided by
    comparing the COLUMN LISTS, and the detail names the rows and columns
    that differ. The first probe is the exact state four of five
    clean-20260917-k5 episodes left - the closer triggered by hand a day
    early, so the three recovered days' SETTLED rows carry `closed_on` one
    day before the oracle's, at the oracle's layout - which the grader once
    reported as a changed column layout on every night.
    """
    gap = ", ".join(f"DATE '{day}'" for day in CONTRACT["gap_days"])
    return [
        ("closed_on_shifted_three_rows",
         f"UPDATE {WINDOW} SET closed_on = closed_on - INTERVAL 1 DAY "
         f"WHERE status = '{mfw.SETTLED}' AND settlement_day IN ({gap})",
         "oracle_rows_differ",
         ("content differs", "3 row(s)", "closed_on"), ("layout",)),
        ("window_column_dropped",
         f"ALTER TABLE {WINDOW} DROP COLUMN net_cents",
         "other_layout",
         ("layout differs", "missing", "net_cents"), ("content differs",)),
        ("window_column_renamed",
         f"ALTER TABLE {WINDOW} RENAME COLUMN net_cents TO net",
         "oracle_content_other_layout",
         ("layout differs", "net_cents", "rows are the oracle's"),
         ("content differs",)),
    ]


def _default_grader() -> Path:
    """tests/grade.py, for a fixture tree that still sits in the package. A
    STAGED copy (under %TEMP%) has no tests/ beside it and no repository above
    it; its caller passes the path (run_baselines.py does)."""
    candidate = TASK / "tests" / "grade.py"
    if candidate.is_file():
        return candidate
    for parent in HERE.parents:
        candidate = parent / "tasks" / CONTRACT["task"] / "tests" / "grade.py"
        if candidate.is_file():
            return candidate
    raise FixtureDrift(f"tests/grade.py is not reachable from {HERE}; pass "
                       f"--grader <path to tests/grade.py>")


def probe_grader(build: Path, *, grader: Path | None = None,
                 verbose: bool = True) -> tuple[int, int]:
    """Run each `_grader_probes` mutation over the oracle's night-1 window,
    hashed and classified by tests/grade.py's OWN functions, and require the
    label and the detail the probe names. Returns (held, total).

    The references handed to `classify` are every trajectory's night-1 window,
    materialised from the build's reference files and hashed by the grader's
    rule - the shape `grade.reference_hashes` builds, without the handoff and
    the book, which no probe reads.
    """
    import importlib.util
    import duckdb
    build = Path(build)
    path = Path(grader) if grader else _default_grader()
    if not path.is_file():
        raise FixtureDrift(f"grader {path} is not a file")
    spec = importlib.util.spec_from_file_location("gt009_grade_under_probe", path)
    grade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grade)
    night = 1
    references: dict = {"variants": {}}
    probes = _grader_probes()
    held = 0
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        for name in CONTRACT["variants"]:
            entry = _expected(build, f"variant_{name}.json")["nights"][night - 1]
            mfw.materialise(con, WINDOW, entry[WINDOW])
            references["variants"][name] = {
                night: {WINDOW: grade._canonical_hash(con, WINDOW)}}
        oracle_rows = _expected(build, "variant_oracle.json")["nights"][night - 1][WINDOW]
        vnow = f"{CONTRACT['plant_dates'][0]}T{CONTRACT['cutoff_time']}:00Z"
        for name, statement, label, must, must_not in probes:
            mfw.materialise(con, WINDOW, oracle_rows)
            con.execute(statement)
            snapshot = grade._canonical_hash(con, WINDOW)
            receipt = {"target": WINDOW, "vnow": vnow,
                       "canonical_hash": snapshot["canonical_hash"],
                       "row_count": snapshot["row_count"],
                       "sample": snapshot["sample"]}
            verdict = grade.classify(night, {WINDOW: receipt}, references,
                                     {night: {WINDOW: snapshot}})
            info = verdict["per_target"][WINDOW]
            problems = []
            if info["variant"] != label:
                problems.append(f"labelled {info['variant']!r}, expected {label!r}")
            problems += [f"detail lacks {word!r}" for word in must
                         if word not in info["detail"]]
            problems += [f"detail says {word!r}" for word in must_not
                         if word in info["detail"]]
            if problems:
                if verbose:
                    print(f"  WRONG  {name}: {'; '.join(problems)} "
                          f"({info['detail']})")
                continue
            held += 1
            if verbose:
                print(f"  holds  {name} -> {label} ({info['detail']})")
    finally:
        con.close()
    return held, len(probes)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--build", default=str(HERE / "build"))
    ap.add_argument("--falsify", action="store_true",
                    help="plant damage and require verify() to catch it; then "
                         "probe the grader's classifier (--probe-grader)")
    ap.add_argument("--probe-grader", action="store_true",
                    help="only the classifier probes: mutate the oracle's "
                         "window and require tests/grade.py to name the "
                         "difference (rows vs layout) correctly")
    ap.add_argument("--grader", default=None,
                    help="path to tests/grade.py (needed for a staged copy)")
    a = ap.parse_args(argv)
    build = Path(a.build)
    if a.probe_grader:
        held, total = probe_grader(build, grader=a.grader)
        print(f"{held}/{total} grader detail probes hold")
        return 0 if held == total else 1
    if a.falsify:
        caught, total = falsify(build)
        print(f"{caught}/{total} planted damages caught")
        held, probes = probe_grader(build, grader=a.grader)
        print(f"{held}/{probes} grader detail probes hold")
        return 0 if caught == total and held == probes else 1
    verify(build)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
