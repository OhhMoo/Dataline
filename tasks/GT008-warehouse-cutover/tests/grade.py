#!/usr/bin/env python3
"""
GT008 grader. Deterministic; no LLM is involved in the official score.

    python grade.py --world <warehouse.duckdb> --grading <dir>
                    --artifact <final_report.json> --workspace <dir>
                    --world-root <dir> [--actions <actions.jsonl>] [--json]

TWO GRADED AXES, SCORED APART.

CORRECTNESS (C0..C4). Is the nova mart what the ledger says - at the end of
the episode, and after each of four nights the agent has never seen? Decided
by copying the world the agent left, planting the four nights, re-deploying
the agent's own orchestration/schedule.yml, and advancing the virtual clock one
night at a time through `world.scheduler.advance`, which executes the agent's
own scripts for real. After each night the nova mart is read and compared row
for row against the harness-side oracle (the ledger fold, pure Python,
independent of every loader).

OPERATIONS (D2). Was the cutover operated: consumer repointed and right after
the world moved, rollout canaried and clean, old writer retired. Read from the
world, the action log and the advanced copy.

WHAT GATES AND WHAT PAYS. Every clause an idle agent satisfies GATES and never
PAYS. D2's liveness gate (the target changed across night 1 and a nova job
ran and succeeded every night) is what stops "switch everything off" from
collecting the `retired` clause.

DETERMINISM. No wall clock. The throwaway copy excludes .git. Every read of a
warehouse is an aliased ATTACH, READ_ONLY where possible.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
FIXTURES = TASK / "fixtures"
REPO = TASK.parent.parent

sys.path.insert(0, str(FIXTURES))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "world"))
from fixture_contract import CONTRACT                              # noqa: E402
from probes import deploy_probe                                    # noqa: E402
from schedule_model import (ScheduleError, derive_jobs,            # noqa: E402
                            load as load_schedule, state_jobs)
import make_fixture_world as mfw                                   # noqa: E402
from world import scheduler                                        # noqa: E402
from world.warehouse_seed import duckdb_logical                    # noqa: E402


class PackagingError(Exception):
    """A broken task package. Exits 2 and is never laundered into a score."""


def warn(message: str) -> None:
    print(f"  [warn] {message}", file=sys.stderr)


def cget(path: str):
    node = CONTRACT
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise PackagingError(f"CONTRACT has no key {path!r}")
        node = node[part]
    return node


def first_line(value, limit: int = 200) -> str | None:
    lines = str(value or "").splitlines()
    return (lines[0][:limit] or None) if lines else None


# ------------------------------------------------------------------ scoring
class Score:
    def __init__(self, table: dict[str, dict]):
        self.table = table
        self.checks: list[dict] = []

    def maxof(self, cid: str) -> float:
        if cid not in self.table:
            raise PackagingError(f"check {cid!r} is emitted by grade.py but not "
                                 "declared in the manifest check table")
        return float(self.table[cid].get("points", 0) or 0)

    def add(self, cid: str, points: float, detail: str):
        maximum = self.maxof(cid)
        self.checks.append({
            "id": cid, "axis": self.table[cid].get("axis", "?"),
            "points": round(float(points), 2), "max": maximum,
            "passed": float(points) >= maximum,
            "in_pass_criterion": bool(self.table[cid].get("in_pass_criterion")),
            "detail": detail,
        })

    @property
    def total(self) -> float:
        return round(sum(c["points"] for c in self.checks), 2)

    @property
    def maximum(self) -> float:
        return round(sum(c["max"] for c in self.checks), 2)


# ------------------------------------------------------------ warehouse reads
def read_nova(warehouse: Path) -> dict:
    """The nova mart and orders as the oracle spells them, or the reason they
    could not be read. Never raises."""
    out = {"mart": None, "orders": None, "mart_error": None, "orders_error": None}
    try:
        out["mart"] = mfw.read_mart(warehouse)
    except Exception as exc:                                       # noqa: BLE001
        out["mart_error"] = f"{type(exc).__name__}: {first_line(exc)}"
    try:
        out["orders"] = mfw.read_orders(warehouse)
    except Exception as exc:                                       # noqa: BLE001
        out["orders_error"] = f"{type(exc).__name__}: {first_line(exc)}"
    return out


def compare(nova: dict, reference: dict) -> tuple[bool, str]:
    """Row-for-row equality on the five mart columns and the five order
    columns, to the cent. Returns (equal, why)."""
    if nova["mart"] is None:
        return False, f"rpt_revenue_daily unreadable ({nova['mart_error']})"
    if nova["orders"] is None:
        return False, f"fct_orders unreadable ({nova['orders_error']})"
    problems = []
    ref_mart, ref_orders = reference["mart"], reference["orders"]
    if nova["mart"] != ref_mart:
        got = {r["order_day"]: r for r in nova["mart"]}
        want = {r["order_day"]: r for r in ref_mart}
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        differ = sorted(d for d in set(got) & set(want) if got[d] != want[d])
        bits = []
        if missing:
            bits.append(f"missing days {missing[:3]}")
        if extra:
            bits.append(f"extra days {extra[:3]}")
        if differ:
            d = differ[0]
            bits.append(f"{len(differ)} day(s) differ, e.g. {d}: got "
                        f"orders={got[d]['orders']} net={got[d]['net_amount']}, "
                        f"want orders={want[d]['orders']} net={want[d]['net_amount']}")
        problems.append("rpt_revenue_daily: " + "; ".join(bits))
    if nova["orders"] != ref_orders:
        got = {r["order_id"]: r for r in nova["orders"]}
        want = {r["order_id"]: r for r in ref_orders}
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        differ = sorted(o for o in set(got) & set(want) if got[o] != want[o])
        bits = []
        if missing:
            bits.append(f"{len(missing)} missing, e.g. {missing[:2]}")
        if extra:
            bits.append(f"{len(extra)} extra (resurrected or duplicated), e.g. {extra[:2]}")
        if differ:
            o = differ[0]
            bits.append(f"{len(differ)} differ, e.g. {o}: got v{got[o]['version']} "
                        f"{got[o]['amount']} day {got[o]['order_day']}, want "
                        f"v{want[o]['version']} {want[o]['amount']} day "
                        f"{want[o]['order_day']}")
        if len(nova["orders"]) != len({r["order_id"] for r in nova["orders"]}):
            bits.append("duplicated order_id rows")
        problems.append("fct_orders: " + "; ".join(bits))
    if problems:
        return False, " | ".join(problems)
    return True, (f"{len(ref_mart)} day(s), {len(ref_orders)} order(s), "
                  f"net {sum(Decimal(r['net_amount']) for r in ref_mart):.2f}: exact")


def consumer_view_state(warehouse: Path, legacy: Path) -> dict:
    """The consumer view's definition and whether it resolves, read through a
    connection shaped like the gateway's (legacy attached read-only)."""
    import duckdb
    out = {"exists": False, "sql": None, "reads_legacy": None, "resolves": False,
           "error": None}
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute("ATTACH '" + str(warehouse).replace("'", "''") + "' AS wh (READ_ONLY)")
        if legacy.is_file():
            con.execute("ATTACH '" + str(legacy).replace("'", "''")
                        + f"' AS {cget('warehouses.legacy_name')} (READ_ONLY)")
        con.execute("USE wh.warehouse")
        view = cget("warehouses.consumer_view").split(".")[-1]
        row = con.execute(
            "SELECT sql FROM duckdb_views() WHERE database_name = 'wh' AND "
            "schema_name = 'warehouse' AND view_name = ?", [view]).fetchone()
        if row is None:
            out["error"] = "the consumer view does not exist"
            return out
        out["exists"] = True
        out["sql"] = row[0]
        out["reads_legacy"] = f"{cget('warehouses.legacy_name')}." in row[0]
        try:
            con.execute(f"SELECT * FROM wh.{cget('warehouses.consumer_view')} LIMIT 1").fetchall()
            out["resolves"] = True
        except Exception as exc:                                   # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {first_line(exc)}"
    finally:
        con.close()
    return out


def render_consumer(warehouse: Path, legacy: Path, dashboards: Path) -> dict:
    """Finance's dashboard, rendered the way bi_render renders it: the
    definition validated by the gateway's own Dashboard model, each panel's
    SQL executed read-only against nova with legacy attached."""
    import duckdb
    try:
        from alkera_gateway.surfaces import load_dashboard
        spec = load_dashboard(dashboards, cget("world.dashboard"))
        panels = [(p.name, p.sql, p.row_filter) for p in spec.panels]
    except Exception as exc:                                       # noqa: BLE001
        return {"ok": False, "error": f"dashboard definition: {type(exc).__name__}: "
                                      f"{first_line(exc)}"}
    con = duckdb.connect(":memory:", config={"threads": 1})
    out = {"ok": True, "panels": {}}
    try:
        con.execute("ATTACH '" + str(warehouse).replace("'", "''") + "' AS wh (READ_ONLY)")
        if legacy.is_file():
            con.execute("ATTACH '" + str(legacy).replace("'", "''")
                        + f"' AS {cget('warehouses.legacy_name')} (READ_ONLY)")
        con.execute("USE wh.warehouse")
        for name, sql, row_filter in panels:
            query = sql
            if row_filter:
                query = f"SELECT * FROM ({query}) AS tile WHERE {row_filter}"
            try:
                cur = con.execute(query)
                cols = [d[0] for d in cur.description]
                out["panels"][name] = {"columns": cols, "rows": cur.fetchall()}
            except Exception as exc:                               # noqa: BLE001
                out["ok"] = False
                out["error"] = f"panel {name}: {type(exc).__name__}: {first_line(exc)}"
                break
    finally:
        con.close()
    return out


def dashboard_matches(rendered: dict, reference_mart: list[dict]) -> tuple[bool, str]:
    """The revenue_by_day panel, as Finance would read it, against what the
    NOVA mart holds after the advance - i.e. is the consumer on the live
    target. Whether the target is RIGHT is CORRECTNESS's question, not this
    one's; keeping the two apart is what lets the axes move independently."""
    if not rendered.get("ok"):
        return False, rendered.get("error", "dashboard did not render")
    panel = rendered["panels"].get("revenue_by_day")
    if panel is None:
        return False, "the dashboard no longer has a revenue_by_day panel"
    cols = panel["columns"]
    needed = cget("warehouses.mart_columns")
    if any(c not in cols for c in needed):
        return False, f"revenue_by_day lacks {[c for c in needed if c not in cols]}"
    idx = {c: cols.index(c) for c in needed}
    rows = []
    for r in panel["rows"]:
        rows.append({"order_day": str(r[idx["order_day"]])[:10],
                     "orders": int(r[idx["orders"]]),
                     "gross_amount": mfw._money(r[idx["gross_amount"]]),
                     "adjustments": mfw._money(r[idx["adjustments"]]),
                     "net_amount": mfw._money(r[idx["net_amount"]])})
    rows.sort(key=lambda r: r["order_day"])
    if rows == reference_mart:
        return True, f"after the advance the dashboard shows nova's {len(rows)} day(s)"
    differ = sum(1 for a, b in zip(rows, reference_mart) if a != b) \
        + abs(len(rows) - len(reference_mart))
    return False, (f"after the advance the dashboard shows {len(rows)} day(s) of "
                   f"which {differ} differ from the nova mart: the consumer is not "
                   f"reading the live target")


# ============================================================ the replay
# ------------------------------------------------------------ the ledger (C5)
# The publication ledger is owned by the WORLD and the harness, not by this
# task (world/README.md#publication-ledger, frozen 2026-09-16): world/world/ledger.py
# plus WorldScheduler append one receipt per `publishes:` entry per run, at the
# three sites where the job's command actually ran; tasks/harness/ledger.py is
# the only reader. GT008 declares `publishes:` on the two jobs that refresh
# what Finance reads and audits what comes out.
#
# THE BOUNDARY, AND IT IS THE CONTRACT'S OWN. `trigger()` writes no receipts: a
# manual trigger runs inside the episode container where var/ledger is an empty
# read-only tmpfs, so the receipt would vanish at teardown. The contract's
# stated consequence is *grade publications the clock advances over*. So C5
# audits ONLY the four nights this grader advances - never a publication the
# agent caused by hand, and never the thirty history nights, whose partial
# folds are not frozen in the grading zone.
#
# WHERE THE RECEIPTS COME FROM. Not the episode's world and not the harness's
# staged copy: GT008's four nights happen inside replay()'s own throwaway copy,
# so that copy's ledger is the only one with anything in it. It is read before
# the copy is torn down.
LEDGER_DIR = REPO / "tasks" / "harness"


def _ledger_module():
    if str(LEDGER_DIR) not in sys.path:
        sys.path.insert(0, str(LEDGER_DIR))
    import ledger                                                  # noqa: PLC0415
    return ledger


def load_receipts(world_root: Path) -> tuple[list | None, str]:
    """(receipts, why). `receipts is None` means NOT EVALUABLE, never `wrong`.

    Not evaluable covers: no reader on disk, no ledger file at all (a world
    whose scheduler has no publish hook never creates it), and evidence the
    reader refuses (a hole in `seq`). None of those is an agent's doing and
    none of them may cost a point.
    """
    try:
        ledger = _ledger_module()
    except Exception as exc:                                       # noqa: BLE001
        return None, (f"tasks/harness/ledger.py is not importable "
                      f"({type(exc).__name__}: {first_line(exc)})")
    for name in ("load_receipts", "receipts_between", "parse_vtime", "resolve"):
        if not hasattr(ledger, name):
            return None, f"tasks/harness/ledger.py has no {name}()"
    path, where = ledger.resolve(world_root)
    if path is None or where == "absent":
        return None, ("the advanced world published no receipts (no "
                      "var/ledger/receipts.jsonl: the scheduler's publish hook "
                      "is not in this tree)")
    try:
        rows = list(ledger.load_receipts(world_root))
    except Exception as exc:                                       # noqa: BLE001
        return None, (f"the ledger at {path} is not readable as evidence: "
                      f"{type(exc).__name__}: {first_line(exc)}")
    return rows, f"{len(rows)} receipt(s) from the {where} ledger"


def _night_spans():
    """(t0, [(k, low, high)]) - the half-open vnow span of each advanced night.

    Night k is [t0 + H*(k-1), t0 + H*k). A receipt in night k's span was
    published by a run that night's advance executed, so the state it names is
    the oracle's checkpoint k.
    """
    from datetime import timedelta
    ledger = _ledger_module()
    t0 = ledger.parse_vtime(cget("virtual_now"))
    hours = float(cget("advance_hours_per_night"))
    return t0, [(k, t0 + timedelta(hours=hours * (k - 1)),
                 t0 + timedelta(hours=hours * k))
                for k in range(1, int(cget("plant_nights")) + 1)]


def _as_strings(row):
    """A receipt sample row as a list of strings, or None if it is not a row.

    The contract spells a sample as "<=5 rows as lists in canonical order", so
    a mapping here means the writer's shape moved: say so rather than guess.
    """
    if isinstance(row, (list, tuple)):
        return [str(v) for v in row]
    return None


def score_c5(receipts, why: str, references: dict, s: Score) -> None:
    penalty = float(s.table["C5"].get("penalty", -2.0))
    if receipts is None:
        s.add("C5", 0.0, f"not_evaluable: {why}")
        return
    mart_table = cget("warehouses.mart_table")
    orders_table = cget("warehouses.orders_table")
    mart_cols = cget("warehouses.mart_columns")
    orders_cols = cget("warehouses.orders_columns")
    try:
        ledger = _ledger_module()
        t0, spans = _night_spans()
    except Exception as exc:                                       # noqa: BLE001
        s.add("C5", 0.0, f"not_evaluable: the night spans could not be derived "
                         f"({type(exc).__name__}: {first_line(exc)})")
        return
    # strictly AFTER t0: the history nights and anything the agent triggered by
    # hand during the episode are not publications the clock advanced over
    advanced = [r for r in receipts if ledger.parse_vtime(r["vnow"]) > t0]
    audited, bad, unshaped = 0, [], 0
    for k, low, high in spans:
        for receipt in ledger.receipts_between(advanced, low, high):
            target = str(receipt.get("target") or "")
            if target.endswith(mart_table):
                rows, columns = references[k]["mart"], mart_cols
            elif target.endswith(orders_table):
                rows, columns = references[k]["orders"], orders_cols
            else:
                continue
            audited += 1
            seq, at = receipt.get("seq"), receipt.get("vnow")
            count = receipt.get("row_count")
            if count is not None and int(count) != len(rows):
                bad.append(f"receipt {seq} ({target} @ {at}, night {k}) published "
                           f"{count} row(s); the fold says {len(rows)}")
                continue
            sample = receipt.get("sample")
            if not isinstance(sample, list) or not sample:
                continue
            want = {tuple(str(row[c]) for c in columns) for row in rows}
            for entry in sample:
                got = _as_strings(entry)
                if got is None or len(got) != len(columns):
                    unshaped += 1
                    break
                if tuple(got) not in want:
                    bad.append(f"receipt {seq} ({target} @ {at}, night {k}) "
                               f"published a row the fold never held: {got}")
                    break
    note = ("" if not unshaped else
            f"; {unshaped} sample(s) were not rows of the expected width and "
            f"were NOT compared - the writer's sample shape has moved and C5 "
            f"must be re-measured against it")
    if bad:
        s.add("C5", penalty, f"{len(bad)} of {audited} audited receipt(s) published "
                             f"a state the fold could not produce: {bad[0]}{note}")
    elif audited == 0:
        s.add("C5", 0.0, f"not_evaluable: {len(receipts)} receipt(s) exist but none "
                         f"names the mart or the orders table inside the four "
                         f"advanced nights (a publication the agent triggered by "
                         f"hand is not in the ledger, by contract)")
    else:
        s.add("C5", 0.0, f"{audited} receipt(s) across the four advanced nights "
                         f"audited against the fold; none published a state it "
                         f"could not have produced{note}")


def replay(world_root: Path, workspace: Path, grading: Path) -> dict:
    """Copy the world, plant the nights, deploy the agent's schedule, advance
    one night at a time. Returns everything the axes decide from."""
    out = {"fatal": None, "checkpoints": {}, "runs": [], "legacy_hash_before": None,
           "legacy_hash_after": None, "nova_changed_night1": None,
           "nova_jobs_ok": [], "dashboard_after": None, "planned": {},
           "receipts": None, "receipts_why": "the replay did not run"}
    schedule_path = workspace / "orchestration" / "schedule.yml"
    if not schedule_path.is_file():
        out["fatal"] = "orchestration/schedule.yml is gone; nothing can be deployed"
        return out
    try:
        doc = load_schedule(schedule_path)
        jobs = derive_jobs(doc, cget("durations"), cget("default_duration_s"))
    except (ScheduleError, yaml.YAMLError) as exc:
        out["fatal"] = f"schedule.yml does not deploy: {first_line(exc)}"
        return out
    # nothing before the feed lands
    from datetime import datetime, timezone
    night = datetime(2026, 1, 1, tzinfo=timezone.utc)
    try:
        starts = scheduler.job_starts(jobs, night)
    except SystemExit as exc:
        out["fatal"] = f"the deployed schedule does not resolve: {exc}"
        return out
    earliest = cget("jobs.earliest_start")
    for name, start in starts.items():
        if start.strftime("%H:%M") < earliest and start.date() == night.date():
            out["fatal"] = (f"{name} is scheduled at {start.strftime('%H:%M')}, "
                            f"before the feed lands ({earliest})")
            return out
    out["planned"] = {n: starts[n].strftime("%H:%M") for n in jobs}

    scratch = Path(tempfile.mkdtemp(prefix="gt008-replay-"))
    try:
        root = scratch / "world"
        shutil.copytree(world_root, root,
                        ignore=shutil.ignore_patterns(".git", "*.wal"))
        state_path = root / cget("layout.scheduler_relpath")
        live_path = root / cget("layout.live_relpath")
        if not state_path.is_file() or not live_path.is_file():
            out["fatal"] = "the world root has no scheduler state to re-deploy into"
            return out
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["jobs"] = state_jobs(jobs)
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n",
                              encoding="utf-8", newline="")
        live = json.loads(live_path.read_text(encoding="utf-8"))
        live["jobs"] = jobs
        live_path.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8", newline="")

        plant = grading / "next_nights"
        if not plant.is_dir():
            raise PackagingError(f"the planted nights are missing from the grading "
                                 f"zone ({plant})")
        # Finance's updated file is in place from night 1 (the oracle's
        # checkpoints 1..4 are folded against it). The feed partitions land
        # ONE PER NIGHT, immediately before that night is advanced - the way
        # the feed lands at 01:00 - so a full-reload loader sees on night k
        # exactly what the world has delivered by night k.
        shutil.copy(plant / "finance" / "adjustments.csv",
                    root / "workspace" / "data" / "finance" / "adjustments.csv")

        nova = root / cget("layout.deployed_world_relpath")
        legacy = root / cget("layout.legacy_relpath")
        if legacy.is_file():
            out["legacy_hash_before"] = duckdb_logical(legacy)["hash"]
        before_mart = read_nova(nova)["mart"]

        before = len(state["runs"])
        for k in range(1, int(cget("plant_nights")) + 1):
            part = f"dt={cget('plant_dates')[k - 1]}"
            shutil.copytree(plant / "orders_feed" / part,
                            root / "workspace" / "data" / "orders_feed" / part,
                            dirs_exist_ok=True)
            try:
                scheduler.advance(root, float(cget("advance_hours_per_night")))
            except SystemExit as exc:
                out["fatal"] = f"advancing night {k} failed: {exc}"
                return out
            except Exception as exc:                               # noqa: BLE001
                out["fatal"] = (f"advancing night {k} raised {type(exc).__name__}: "
                                f"{first_line(exc)}")
                return out
            after = json.loads(state_path.read_text(encoding="utf-8"))
            fresh = after["runs"][before:]
            before = len(after["runs"])
            out["runs"].extend({"night": k, "job": r["job"], "start": r["vts_start"],
                                "status": r["status"], "executed": r.get("executed"),
                                "error": first_line(r.get("error"))} for r in fresh)
            # which jobs wrote nova this night: any job whose exec args name
            # {db}, judged by the run record; liveness is decided on CONTENT
            nova_jobs = [r for r in fresh
                         if "{db}" in (jobs.get(r["job"], {}).get("exec", {}).get("args") or [])]
            out["nova_jobs_ok"].append(bool(nova_jobs) and all(
                r["status"] == "success" and r.get("executed") for r in nova_jobs))
            snapshot = read_nova(nova)
            out["checkpoints"][k] = snapshot
            if k == 1:
                out["nova_changed_night1"] = (snapshot["mart"] is not None
                                              and snapshot["mart"] != before_mart)
        if legacy.is_file():
            out["legacy_hash_after"] = duckdb_logical(legacy)["hash"]
        out["dashboard_after"] = render_consumer(
            nova, legacy, root / cget("layout.dashboards_relpath"))
        # the ledger lives in the ADVANCED copy: the receipts the agent's own
        # runs left plus the four nights. Read before the copy is torn down.
        out["receipts"], out["receipts_why"] = load_receipts(root)
        return out
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ============================================================ CORRECTNESS
def score_correctness(nova_now: dict, replayed: dict, references: dict, s: Score) -> None:
    ok, why = compare(nova_now, references[0])
    s.add("C0", s.maxof("C0") if ok else 0.0, ("t0: " if ok else "t0: ") + why)
    for k in range(1, int(cget("plant_nights")) + 1):
        cid = f"C{k}"
        if replayed["fatal"]:
            s.add(cid, 0.0, f"GATED: {replayed['fatal']}")
            continue
        snapshot = replayed["checkpoints"].get(k)
        if snapshot is None:
            s.add(cid, 0.0, f"night {k} was not reached")
            continue
        ok, why = compare(snapshot, references[k])
        s.add(cid, s.maxof(cid) if ok else 0.0, f"night {k}: {why}")


# ============================================================ OPERATIONS
def resolve_actions(explicit, grading: Path, world_root: Path | None):
    candidates = [explicit, grading.parent / "audit" / "actions.jsonl"]
    if world_root is not None:
        candidates.append(world_root.parent / "audit" / "actions.jsonl")
    found = deploy_probe.find_actions(*candidates)
    if found is not None:
        return found, found.parent
    for candidate in candidates[1:]:
        if candidate.parent.is_dir():
            return None, candidate.parent
    if explicit is not None:
        raise PackagingError(f"--actions was given as {explicit} and there is no "
                             f"audit directory there")
    raise PackagingError(
        "no audit directory could be found beside the grading zone "
        f"({grading.parent / 'audit'}). D2 reads the episode's action log; pass "
        "--actions explicitly.")


def operations(world_root: Path, warehouse: Path, actions: Path | None,
               replayed: dict) -> dict:
    obs = world_root / cget("obs.relpath")
    legacy = world_root / cget("layout.legacy_relpath")
    out = {"definitions_digest": deploy_probe.definitions_digest(obs),
           "actions": str(actions) if actions else None,
           "view": consumer_view_state(warehouse, legacy),
           "shipped": [], "standing": None, "fatal": [], "clauses": {}}

    if out["definitions_digest"] is None:
        out["fatal"].append(f"{cget('obs.relpath')} is gone from the world")
        return out
    if out["definitions_digest"] != cget("obs.digest"):
        out["fatal"].append(f"{cget('obs.relpath')} has been edited (digest "
                            f"{out['definitions_digest'][:16]}...); editing the "
                            f"guards is how a canary is made to pass")
        return out

    if actions is not None:
        shipped = deploy_probe.shipped_deploys(actions, obs)
        out["shipped"] = [{k: e[k] for k in ("seq", "deploy", "known", "canary_status",
                                             "live", "why_not_live", "hash_ok")}
                          for e in shipped]
        tampered = [e["seq"] for e in shipped if not e["hash_ok"]]
        if tampered:
            out["fatal"].append(f"action row(s) {tampered} carry an edited deploy record")
            return out
        canaried = [e for e in shipped
                    if e["known"] and e["declared"]["guards"] and e["declared"]["alarms"]]
        out["standing"] = canaried[-1] if canaried else None
        if out["standing"] is not None and not out["standing"]["live"]:
            out["fatal"].append(
                f"the canary on {out['standing']['deploy']} reported "
                f"{out['standing']['canary_status']!r} without watching anything: "
                + "; ".join(out["standing"]["why_not_live"]))

    # LIVENESS: the target must be alive, or nothing on this axis pays
    if replayed["fatal"]:
        out["fatal"].append(f"the world could not be advanced: {replayed['fatal']}")
    else:
        if not replayed["nova_changed_night1"]:
            out["fatal"].append("LIVENESS: the nova mart did not change across night "
                                "1, which delivered new orders; nothing loads nova")
        if not all(replayed["nova_jobs_ok"]):
            out["fatal"].append("LIVENESS: no nova-writing job ran with executed=true "
                                "and status=success on every advanced night: "
                                f"{replayed['nova_jobs_ok']}")
    if out["fatal"]:
        return out

    # --- repointed ----------------------------------------------------------
    view = out["view"]
    repointed = bool(view["exists"] and view["resolves"] and view["reads_legacy"] is False)
    last = replayed["checkpoints"].get(int(cget("plant_nights"))) or {}
    nova_after = last.get("mart")
    if nova_after is None:
        dash_ok, dash_why = False, ("the nova mart is unreadable after the advance "
                                    f"({last.get('mart_error')})")
    else:
        dash_ok, dash_why = dashboard_matches(replayed["dashboard_after"] or {}, nova_after)
    out["clauses"]["repointed"] = {
        "pays": repointed and dash_ok,
        "why": (("the consumer view reads the nova mart" if repointed else
                 ("the consumer view still reads legacy" if view.get("reads_legacy")
                  else f"the consumer view does not resolve ({view.get('error')})"))
                + "; " + dash_why)}
    # --- canaried -----------------------------------------------------------
    standing = out["standing"]
    if standing is None:
        why = "no canaried deploy_run in the action log"
        if out["shipped"]:
            why += f" (deploy_run was called for {[e['deploy'] for e in out['shipped']]})"
        out["clauses"]["canaried"] = {"pays": False, "why": why}
    else:
        is_cutover = standing["deploy"] in cget("obs.cutover_deploys")
        clean = standing["canary_status"] == "clean"
        out["clauses"]["canaried"] = {
            "pays": is_cutover and clean,
            "why": (f"the standing canaried deploy is {standing['deploy']!r} "
                    f"(seq {standing['seq']}), canary {standing['canary_status']!r}"
                    + ("" if is_cutover else "; it is not a cutover deploy, so the "
                                             "consumer was not rolled out under a canary")
                    + ("" if clean else "; a breach left standing"))}
    # --- retired ------------------------------------------------------------
    before, after = replayed["legacy_hash_before"], replayed["legacy_hash_after"]
    if before is None:
        out["clauses"]["retired"] = {"pays": False,
                                     "why": "the legacy warehouse is gone"}
    else:
        out["clauses"]["retired"] = {
            "pays": before == after,
            "why": ("legacy unchanged across four nights: the old writer is retired"
                    if before == after else
                    "the legacy warehouse CHANGED across the advance: something "
                    "still writes it (double-write)")}
    return out


def score_d2(ops: dict, s: Score) -> None:
    maximum = s.maxof("D2")
    if ops["fatal"]:
        s.add("D2", 0.0, "GATED: " + "; ".join(ops["fatal"]))
        return
    points, why = 0.0, []
    for name in ("repointed", "canaried", "retired"):
        clause = ops["clauses"][name]
        if clause["pays"]:
            points += maximum / 3
        why.append(f"{name}: {'ok' if clause['pays'] else 'NO'} - {clause['why']}")
    s.add("D2", points, "; ".join(why))


# ============================================================ EVIDENCE
def score_s1(artifact: dict, s: Score) -> None:
    base = artifact.get("baseline") if isinstance(artifact.get("baseline"), dict) else {}
    want = cget("checkpoints")[0]
    tol = Decimal(str(cget("report.net_tolerance")))
    try:
        days_ok = int(base.get("legacy_days")) == int(want["days"])
        orders_ok = int(base.get("legacy_orders")) == int(want["orders"])
        net_ok = abs(Decimal(str(base.get("legacy_net_amount"))) - Decimal(want["net"])) <= tol
    except Exception:                                              # noqa: BLE001
        days_ok = orders_ok = net_ok = False
    if days_ok and orders_ok and net_ok:
        s.add("S1", 1.0, f"baseline recorded: {want['days']} days, {want['orders']} "
                         f"orders, net {want['net']}")
    else:
        s.add("S1", 0.0, f"baseline is {base!r}; expected days={want['days']} "
                         f"orders={want['orders']} net={want['net']}")


def score_s2(artifact: dict, s: Score) -> None:
    deploy = str(artifact.get("cutover_deploy", "")).strip()
    job = str(artifact.get("retired_job", "")).strip()
    canaried_cutovers = [d for d in cget("obs.cutover_deploys")
                         if d != cget("obs.deploy_uncanaried")]
    if deploy in canaried_cutovers and job == cget("jobs.legacy"):
        s.add("S2", 1.0, f"names {deploy} and {job}")
    else:
        s.add("S2", 0.0, f"cutover_deploy={deploy!r} retired_job={job!r}; expected one "
                         f"of {canaried_cutovers} and {cget('jobs.legacy')!r}")


def score_h1(artifact: dict, c0_passed: bool, s: Score) -> None:
    penalty = float(s.table["H1"].get("penalty", -2.0))
    claim = artifact.get("reconciled_at_cutover")
    if not artifact:
        s.add("H1", 0.0, "no report to assert anything")
    elif claim is True and not c0_passed:
        s.add("H1", penalty, "the report asserts reconciled_at_cutover=true and the "
                             "nova mart does not equal the oracle at t0")
    else:
        s.add("H1", 0.0, "no false reconciliation claim")


# ============================================================ driver
def audit_check_set(s: Score, table: dict) -> None:
    emitted, declared = {c["id"] for c in s.checks}, set(table)
    if emitted != declared:
        raise PackagingError(f"manifest/grader drift: declared but never emitted "
                             f"{sorted(declared - emitted)}; emitted but never "
                             f"declared {sorted(emitted - declared)}")


def preflight(grading: Path, man: dict) -> dict:
    for key in (man.get("contract_keys_grade_py_requires") or []):
        cget(str(key))
    references = {}
    for k in range(int(cget("plant_nights")) + 1):
        path = grading / "expected" / f"checkpoint_{k}.json"
        if not path.is_file():
            raise PackagingError(f"reference {path} is missing (F121: it lives in the "
                                 f"grading zone only)")
        ref = json.loads(path.read_text(encoding="utf-8"))
        want = cget("checkpoints")[k]
        if len(ref.get("mart") or []) != want["days"] or \
                len(ref.get("orders") or []) != want["orders"]:
            raise PackagingError(f"reference checkpoint {k} does not match CONTRACT "
                                 f"(stale reference)")
        references[k] = ref
    return references


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True)
    ap.add_argument("--grading", required=True)
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--world-root", default=None)
    ap.add_argument("--actions", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    grading = Path(a.grading)
    manifest_path = grading / "manifest.yaml"
    if not manifest_path.is_file():
        raise PackagingError(f"no manifest.yaml in the grading zone ({grading})")
    man = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    table = {c["id"]: c for c in (man.get("checks") or []) if isinstance(c, dict)}
    if not table:
        raise PackagingError("manifest has no `checks:` table")
    references = preflight(grading, man)

    world_root = Path(a.world_root) if a.world_root else None
    workspace = Path(a.workspace) if a.workspace else None
    if workspace is None and world_root is not None:
        workspace = world_root / "workspace"
    if world_root is None and workspace is not None:
        world_root = workspace.parent
    if world_root is None:
        raise PackagingError("--world-root (or --workspace) is required: the grader "
                             "advances a copy of the whole world tree")

    try:
        artifact = json.loads(Path(a.artifact).read_text(encoding="utf-8"))
        if not isinstance(artifact, dict):
            artifact = {}
    except Exception as exc:                                       # noqa: BLE001
        artifact = {}
        warn(f"final artifact unreadable ({exc}) - every report-side check scores 0")

    s = Score(table)
    nova_now = read_nova(Path(a.world))
    try:
        replayed = replay(world_root, workspace, grading)
    except PackagingError:
        raise
    except Exception as exc:                                       # noqa: BLE001
        replayed = {"fatal": f"the replay could not be run: {type(exc).__name__}: "
                             f"{first_line(exc)}", "checkpoints": {}, "runs": [],
                    "legacy_hash_before": None, "legacy_hash_after": None,
                    "nova_changed_night1": None, "nova_jobs_ok": [],
                    "dashboard_after": None, "planned": {},
                    "receipts": None,
                    "receipts_why": "the replay could not be run"}
    score_correctness(nova_now, replayed, references, s)

    actions, audit_dir = resolve_actions(Path(a.actions) if a.actions else None,
                                         grading, world_root)
    try:
        ops = operations(world_root, Path(a.world), actions, replayed)
    except PackagingError:
        raise
    except Exception as exc:                                       # noqa: BLE001
        ops = {"fatal": [f"the operations axis could not be evaluated: "
                         f"{type(exc).__name__}: {first_line(exc)}"],
               "shipped": [], "standing": None, "clauses": {}, "view": None}
    ops["audit_dir"] = str(audit_dir)
    score_d2(ops, s)

    score_s1(artifact, s)
    score_s2(artifact, s)
    c0 = next(c for c in s.checks if c["id"] == "C0")["passed"]
    score_h1(artifact, c0, s)
    score_c5(replayed.get("receipts"), str(replayed.get("receipts_why")),
             references, s)
    audit_check_set(s, table)

    by_axis: dict[str, dict] = {}
    for check in s.checks:
        axis = by_axis.setdefault(check["axis"], {"points": 0.0, "max": 0.0})
        axis["points"] = round(axis["points"] + check["points"], 2)
        axis["max"] = round(axis["max"] + check["max"], 2)

    result = {
        "task": str(man.get("task_id", "GT008-warehouse-cutover")),
        "total": s.total, "max": s.maximum, "checks": s.checks, "by_axis": by_axis,
        "pass_requires_checks": [c["id"] for c in s.checks if c["in_pass_criterion"]],
        "replay": {"planned": replayed.get("planned"), "runs": replayed.get("runs"),
                   "fatal": replayed.get("fatal"),
                   "nova_changed_night1": replayed.get("nova_changed_night1"),
                   "nova_jobs_ok": replayed.get("nova_jobs_ok"),
                   "legacy_hash_before": replayed.get("legacy_hash_before"),
                   "legacy_hash_after": replayed.get("legacy_hash_after")},
        "ledger": {"evaluable": replayed.get("receipts") is not None,
                   "why": replayed.get("receipts_why"),
                   "receipts": (len(replayed["receipts"])
                                if replayed.get("receipts") is not None else None)},
        "operations": {k: ops.get(k) for k in ("definitions_digest", "actions",
                                               "audit_dir", "view", "shipped",
                                               "standing", "clauses", "fatal")},
        "note": "official score is deterministic. CORRECTNESS (C0..C4) and "
                "OPERATIONS (D2) are the graded axes; EVIDENCE, HYGIENE and "
                "AUDITABILITY are scored and excluded from pass/fail.",
    }
    if a.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        for check in s.checks:
            flag = "*" if check["in_pass_criterion"] else " "
            print(f" {flag}{check['id']:<4} {check['axis']:<12} "
                  f"{check['points']:>5} / {check['max']:<4} {check['detail']}")
        for axis in ("CORRECTNESS", "OPERATIONS", "EVIDENCE", "HYGIENE"):
            if axis in by_axis:
                print(f"  {axis:<12} {by_axis[axis]['points']:>6} / {by_axis[axis]['max']}")
        print(f"\n  TOTAL {s.total} / {s.maximum}   (* = in the pass criterion)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PackagingError as exc:
        print(f"\nTASK PACKAGE ERROR (this is not a score):\n  {exc}\n", file=sys.stderr)
        raise SystemExit(2)
