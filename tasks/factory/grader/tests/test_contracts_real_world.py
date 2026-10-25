"""The three promoted contracts EXECUTED against real worlds, not fixtures.

tasks/contracts/tests/ proves each contract on a hand-made toy warehouse.
That proves the clause logic and nothing about whether the contract can read a
world the factory actually ships.  Here each contract runs against:

  version_precedence    GT008's built world (advance runner, nova and legacy
                        warehouses) and GT009's recorded rollouts (feed dirs
                        with GT009's `_delivery_manifest.csv` sidecars);
  consumer_consistency  GT009's recorded rollouts (ledger receipts, replayed
                        night by night) and BI002's built world (a Power BI
                        import, YAML refresh caches, a Tableau extract and a
                        Sigma catalog);
  auditability          BI002's real dbt project (dbt build fired per phase).

Each contract gets a HOLDING world, the CANONICAL VIOLATION its docstring
names, and its NAMED SHORTCUT, and where a task grader already grades the same
property, an EQUIVALENCE check: the same world through the task grader and
through the contract, and the two verdicts must agree.
  GT008     C1..C4 (`grade.replay` + `grade.compare`) against version_precedence
            on oracle / port_legacy / ignored_deletion / no_op.
  GT009     the recorded R1..R4 of every rollout in the subset against
            consumer_consistency's per-night receipt verdict.  e2/r1 is the
            recorded violation (R3, R4 fail).
  BI002     the task's documented February figure (2,322,780.00 in the
            import, 2,361,224.00 in the warehouse) against the Power BI
            import clause.

Recorded rollouts are READ-ONLY: every one is copied into a tempdir first.
Builds are session-scoped (GT008 < 1s, BI002 about 22s) and every expensive
replay runs once per session and is shared by the tests that read it.
`CONTRACTS_ALL_ROLLOUTS=1` widens the GT009 subset to every recorded rollout.

Import isolation: GT008 and GT009 both ship modules called fixture_contract,
make_fixture_world and schedule_model.  `task_modules` scopes each task's
sys.path entries and evicts those module names on exit, so the second task
never sees the first one's CONTRACT.

SYNTHETIC SCHEDULED WORLDS (the last section).  A minimal world in the frozen
layout (agent.md 9.2.1: var/warehouse/warehouse.duckdb, workspace/,
var/sched/state.json plus var/world/live.json) whose live control plane names
one script job.  version_precedence and auditability run `runner: "advance"`
WITHOUT planted nights - one scheduler advance by `advance_hours`, through
`_world.run_advance` - and consumer_consistency reads a receipt the same
scheduler wrote, plus `params.bindings` BI extracts.
"""
from __future__ import annotations

import ast
import contextlib
import csv
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

REPO = Path(__file__).resolve().parents[4]
if str(REPO / "world") not in sys.path:
    sys.path.insert(0, str(REPO / "world"))
if str(REPO) not in sys.path:
    sys.path.append(str(REPO))

from world import bi as world_bi                     # noqa: E402
from world import ledger as world_ledger             # noqa: E402
from world import scheduler                          # noqa: E402

from tasks.contracts import evaluate                 # noqa: E402
from tasks.contracts._types import Context           # noqa: E402
from tasks.contracts.consumer_consistency import digest, table_contents  # noqa: E402

PY = sys.executable
GT008 = REPO / "tasks" / "GT008-warehouse-cutover"
GT009 = REPO / "tasks" / "GT009-settlement-cutoff"
BI002 = REPO / "tasks" / "BI002-powerbi-share-and-stale-refresh"
ROLLOUTS = Path("/home/ubuntu/alkera_rollouts/GT009-settlement-cutoff")
DESAT = Path("/home/ubuntu/desaturation_experiment_20261017/runs/GT009-settlement-cutoff")
E2R1 = DESAT / "e2-noisy-surface-20261017-k5-opus55h" / "r1"
TASK_MODULES = ("fixture_contract", "make_fixture_world", "schedule_model", "grade",
                "run_baselines", "probes", "taskspec", "stage_surfaces")
ADVANCE_ARGV = {"argv": ["{python}", "-m", "world.scheduler", "{root}", "advance", "{hours}"],
                "env": {"PYTHONPATH": str(REPO / "world")}}


# ---------------------------------------------------------------- plumbing
@contextlib.contextmanager
def task_modules(*dirs):
    """Put a task's dirs first on sys.path and evict every task-module name
    on the way in AND out, so two tasks' same-named modules never mix."""
    saved_path = list(sys.path)
    evicted = {n: sys.modules.pop(n) for n in list(sys.modules)
               if n.split(".")[0] in TASK_MODULES}
    sys.path[:0] = [str(d) for d in dirs]
    try:
        yield
    finally:
        for n in [n for n in sys.modules if n.split(".")[0] in TASK_MODULES]:
            del sys.modules[n]
        sys.modules.update(evicted)
        sys.path[:] = saved_path


def load_file(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def baseline_strings(path: Path, names: tuple, **env) -> dict:
    """The shortcut loaders exactly as the task's run_baselines.py builds them
    (string replacements on the solution loader), by evaluating those module-
    level assignments alone - importing run_baselines would start its gateway
    and harness imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    ns = dict(env)
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names):
            ns[node.targets[0].id] = eval(compile(ast.Expression(node.value), str(path), "eval"), ns)
    missing = [n for n in names if n not in ns]
    assert not missing, f"{path.name} no longer defines {missing}"
    return {n: ns[n] for n in names}


def copy_world(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    return dst


def clause(result, suffix: str):
    found = [c for c in result.clauses if c.id.endswith(suffix)]
    assert found, f"no clause *{suffix} in {[c.id for c in result.clauses]}"
    return found[0]


def summary(result) -> str:
    return "\n".join(f"  {c.id} {c.passed} @{c.checkpoint}: {c.detail[:240]}"
                     for c in result.clauses)


# ======================================================================
# GT008: version_precedence through the advance runner
# ======================================================================
VP_GT008 = {"target": "warehouse.fct_orders", "key_column": "order_id",
            "version_column": "version", "type_field": "event_type",
            "delete_value": "delete", "feed_dir": "workspace/data/orders_feed",
            "identity_fields": ["order_id", "version", "event_type", "order_day",
                                "customer_id", "amount"],
            "compare": {"amount": "amount", "order_day": "order_day",
                        "customer_id": "customer_id"}}


@pytest.fixture(scope="session")
def gt008(tmp_path_factory):
    base = tmp_path_factory.mktemp("gt008")
    with task_modules(GT008 / "fixtures"):
        import make_fixture_world as mfw
        from fixture_contract import CONTRACT
        build = base / "build"
        mfw.main(["--out", str(build)])
        shipped, grading = base / "shipped", base / "grading"
        mfw.stage_world_root(build, shipped)
        mfw.write_grading_zone(build, shipped, grading)
        legacy_relpath = CONTRACT["layout"]["legacy_relpath"]
        dates = list(CONTRACT["plant_dates"])
        hours = CONTRACT["advance_hours_per_night"]
    good = (GT008 / "solution" / "load_nova.py").read_text(encoding="utf-8")
    shortcuts = baseline_strings(GT008 / "tests" / "run_baselines.py",
                                 ("IGNORED_DELETION_LOADER",), GOOD_LOADER=good)
    loader = shipped / "workspace" / "pipelines" / "load_nova.py"
    sources = {"oracle": good,
               "port_legacy": (shipped / "workspace" / "pipelines" / "load_legacy.py")
               .read_text(encoding="utf-8"),
               "ignored_deletion": shortcuts["IGNORED_DELETION_LOADER"],
               "no_op": None}
    worlds = {}
    for name, source in sources.items():
        worlds[name] = copy_world(shipped, base / name)
        if source is not None:
            (worlds[name] / loader.relative_to(shipped)).write_text(source, encoding="utf-8")
    nights = [{"label": d, "hours": hours,
               "plant": [{"src": f"orders_feed/dt={d}",
                          "dst": f"workspace/data/orders_feed/dt={d}"}]
               + ([{"src": "finance/adjustments.csv",
                    "dst": "workspace/data/finance/adjustments.csv"}] if i == 0 else [])}
              for i, d in enumerate(dates)]
    params = {**VP_GT008, "runner": "advance", "advance": scheduler.advance,
              "nights": nights, "plant_root": str(grading / "next_nights")}
    return {"shipped": shipped, "grading": grading, "worlds": worlds, "params": params,
            "legacy_relpath": legacy_relpath, "dates": dates, "results": {}}


def gt008_run(fx, name: str, **over):
    """One version_precedence evaluation per (world, overrides), cached."""
    key = (name, json.dumps({k: v for k, v in over.items() if k != "advance"},
                            sort_keys=True, default=str))
    if key not in fx["results"]:
        root = fx["shipped"] if name == "shipped" else fx["worlds"][name]
        fx["results"][key] = evaluate("version_precedence",
                                      Context(world_root=root,
                                              params={**fx["params"], **over}))
    return fx["results"][key]


def test_gt008_oracle_holds_every_night(gt008):
    res = gt008_run(gt008, "oracle")
    assert res.passed and res.points == res.max, summary(res)
    assert clause(res, "every_night_reached").passed
    assert "2026-05-16" in clause(res, "highest_version").detail or \
        clause(res, "highest_version").passed


def test_gt008_ported_legacy_loader_breaks_on_night_two(gt008):
    """THE canonical violation: the legacy loader ported as-is keeps the
    version that arrived LAST, which is right until a lower version is
    re-delivered - the second planted night."""
    res = gt008_run(gt008, "port_legacy")
    hv = clause(res, "highest_version")
    assert not res.passed and res.points == 0.0, summary(res)
    assert not hv.passed and hv.checkpoint == gt008["dates"][1], summary(res)


def test_gt008_shortcut_is_invisible_to_the_end_state(gt008):
    """The shortcut the advance runner exists for: the shipped legacy loader
    passes a t0 inspection of the legacy warehouse and fails as soon as the
    world moves."""
    db = {"database": gt008["legacy_relpath"]}
    still = gt008_run(gt008, "shipped", runner="end_state", **db)
    moved = gt008_run(gt008, "shipped", **db)
    assert still.passed, summary(still)
    assert not moved.passed and not clause(moved, "highest_version").passed, summary(moved)
    assert clause(moved, "highest_version").checkpoint == gt008["dates"][1]


def test_gt008_ignored_deletion_is_a_resurrection(gt008):
    """GT008's ignored_deletion baseline: highest version right, deletes kept
    as live rows.  Only the resurrection clause may fail."""
    res = gt008_run(gt008, "ignored_deletion")
    assert not clause(res, "no_resurrection").passed, summary(res)
    failed = {c.id.split(".")[-1] for c in res.clauses if not c.passed}
    assert failed == {"no_resurrection"}, summary(res)
    assert 0 < res.points < res.max     # not fatal: the score carries it


def test_gt008_subprocess_advance_matches_the_callable(gt008):
    """runner: advance with `python -m world.scheduler` in a subprocess - the
    form a contract runs with nothing injected - reaches the same verdict."""
    via_argv = gt008_run(gt008, "port_legacy", advance=ADVANCE_ARGV)
    via_call = gt008_run(gt008, "port_legacy")
    assert [(c.id, c.passed, c.checkpoint) for c in via_argv.clauses] == \
        [(c.id, c.passed, c.checkpoint) for c in via_call.clauses], summary(via_argv)


def test_gt008_equivalent_to_the_task_grader(gt008):
    """GT008's own replay+compare (C1..C4) and version_precedence's per-night
    verdict agree on all-pass and on the first failing night."""
    saved_argv = list(sys.argv)
    try:
        sys.argv = ["grade.py", "--grading", str(gt008["grading"])]
        with task_modules(GT008 / "fixtures", GT008 / "tests"):
            grade = load_file("gt008_grade_for_contracts", GT008 / "tests" / "grade.py")
            refs = {k: json.loads((gt008["grading"] / "expected" / f"checkpoint_{k}.json")
                                  .read_text(encoding="utf-8")) for k in range(1, 5)}
            task = {}
            for name, world in gt008["worlds"].items():
                rep = grade.replay(world, world / "workspace", gt008["grading"])
                task[name] = [bool(rep["checkpoints"].get(k) is not None
                                   and grade.compare(rep["checkpoints"][k], refs[k])[0])
                              for k in range(1, 5)]
    finally:
        sys.argv = saved_argv
    rows = []
    for name in gt008["worlds"]:
        res = gt008_run(gt008, name)
        firsts = [c.checkpoint for c in res.clauses
                  if not c.passed and c.checkpoint in gt008["dates"]]
        contract_first = min((gt008["dates"].index(d) + 1 for d in firsts), default=None)
        task_first = next((k for k, ok in enumerate(task[name], 1) if not ok), None)
        rows.append((name, task[name], contract_first, task_first))
        assert (res.points == res.max) == all(task[name]), (name, task[name], summary(res))
        assert contract_first == task_first, (name, task[name], summary(res))
    print("\nGT008 world, C1..C4, contract first failing night, task first failing night")
    for row in rows:
        print("  ", *row)
    assert any(not all(t) for _, t, _, _ in rows), rows   # the check discriminates


# ======================================================================
# GT009: recorded rollouts, consumer_consistency + version_precedence
# ======================================================================
def gt009_subset() -> list:
    every = sorted(p for p in list(ROLLOUTS.glob("*/r*")) + list(DESAT.glob("*/r*"))
                   if (p / "rollout.json").is_file() and (p / "world").is_dir())
    if os.environ.get("CONTRACTS_ALL_ROLLOUTS"):
        return every
    want = [E2R1, ROLLOUTS / "default-20261017-k5-opus55h" / "r0",
            ROLLOUTS / "live-consumer-20261017-k5-opus55h" / "r0",
            DESAT / "e3-compositional-20261017-k5-opus55h" / "r0"]
    return [p for p in want if p in every]


def recorded_nights(rollout: Path) -> list:
    checks = json.loads((rollout / "rollout.json").read_text(encoding="utf-8"))["score"]["checks"]
    by = {c["id"]: c for c in checks}
    return [bool(by[f"R{k}"]["passed"]) for k in range(1, 5)]


class GT009World:
    """Copies of one recorded rollout plus everything the advance runner
    needs: the arm's schedule deployment (prepare), the planted nights, and
    the oracle trajectory hashed by the ledger rule."""

    def __init__(self, rollout: Path, scratch: Path):
        self.rollout = rollout
        self.scratch = scratch
        self.grading = copy_world(rollout / "grading", scratch / "grading")
        self.world = copy_world(rollout / "world", scratch / "world")
        arm = (json.loads((self.grading / "variant.json").read_text(encoding="utf-8"))
               .get("variant") or "default")
        arm_dir = GT009 / "fixtures" / "variants" / arm
        dirs = ([arm_dir] if (arm_dir / "schedule_model.py").is_file() else []) + \
            [GT009 / "fixtures"]
        with task_modules(*dirs):
            import make_fixture_world as mfw
            import schedule_model
            from fixture_contract import CONTRACT
            self.contract = CONTRACT
            self.schedule_model = schedule_model
            self.targets = {k: CONTRACT["targets"][k] for k in ("handoff", "window")}
            self.dates = list(CONTRACT["plant_dates"])
            self.expected = {}
            for variant in ("oracle", "replay_wins"):
                self.expected[variant] = self._reference(mfw, variant)
        self.layout = CONTRACT["layout"]
        self.facts = CONTRACT["targets"]["facts"]

    def _reference(self, mfw, variant: str) -> dict:
        path = self.grading / "expected" / f"variant_{variant}.json"
        if not path.is_file():
            return {}
        out = {}
        for entry in json.loads(path.read_text(encoding="utf-8"))["nights"]:
            day = self.dates[int(entry["night"]) - 1]
            for target in [t for t in entry if t in mfw.DDL]:
                ref = self.scratch / "ref" / variant / f"{day}-{target}"
                ref.mkdir(parents=True)
                con = duckdb.connect(str(ref / "ref.duckdb"))
                try:
                    mfw.materialise(con, target, entry[target])
                finally:
                    con.close()
                (ref / "var" / "warehouse").mkdir(parents=True)
                shutil.copy(ref / "ref.duckdb", ref / "var" / "warehouse" / "warehouse.duckdb")
                out[f"{target}|{day}"] = table_contents(ref, target)["canonical_hash"]
        return out

    def prepare(self, root: Path) -> None:
        """What GT009's replay does before night 1: deploy the agent's
        schedule.yml as the arm's scheduler would, restore the platform."""
        c, lay = self.contract, self.layout
        doc = yaml.safe_load((root / "workspace" / lay["schedule_relpath"]).read_text())
        jobs = self.schedule_model.deployed_jobs(doc, c["durations"], c["default_duration_s"])
        for rel, key, value in ((lay["scheduler_relpath"], "jobs",
                                 self.schedule_model.state_jobs(jobs)),
                                (lay["live_relpath"], "jobs", jobs)):
            doc = json.loads((root / rel).read_text(encoding="utf-8"))
            doc[key] = value
            (root / rel).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                                    encoding="utf-8")
        platform = root / "workspace" / lay["platform_relpath"]
        if (self.grading / "platform").is_dir():
            shutil.rmtree(platform, ignore_errors=True)
            shutil.copytree(self.grading / "platform", platform)

    def advance_params(self, **over) -> dict:
        nights = [{"label": d, "hours": self.contract["advance_hours_per_night"],
                   "plant": [{"src": f"payments_feed/dt={d}",
                              "dst": f"workspace/{self.layout['feed_relpath']}/dt={d}"}]}
                  for d in self.dates]
        return {"runner": "advance", "advance": scheduler.advance, "prepare": self.prepare,
                "nights": nights, "plant_root": str(self.grading / "next_nights"), **over}

    def consumer(self, world: Path | None = None, variant: str = "oracle"):
        """consumer_consistency with an oracle that also logs every receipt
        it is asked about, so the per-night verdict can be read back."""
        seen = []
        exp = self.expected[variant]

        def oracle(receipt, ctx=None):
            seen.append(receipt)
            h = exp.get(f"{receipt['target']}|{receipt['day']}")
            return {"canonical_hash": h} if h else None

        res = evaluate("consumer_consistency", Context(
            world_root=world or self.world,
            params=self.advance_params(oracle=oracle, targets=list(self.targets.values()))))
        nights = []
        for d in self.dates:
            mine = [r for r in seen if r["day"] == d]
            nights.append(bool(mine) and len({r["target"] for r in mine}) == 2 and
                          all(exp.get(f"{r['target']}|{d}") == r["canonical_hash"]
                              for r in mine))
        return res, nights, seen

    def precedence(self, world: Path | None = None, **over):
        params = {"target": self.facts, "key_column": "payment_id",
                  "version_column": "version", "type_field": "event_type",
                  "delete_value": "void", "feed_dir": f"workspace/{self.layout['feed_relpath']}",
                  "identity_fields": ["payment_id", "version", "event_type", "payment_day",
                                      "merchant_id", "amount_cents"],
                  "compare": {"amount_cents": "amount_cents", "merchant_id": "merchant_id",
                              "payment_day": "payment_day"}}
        params.update(self.advance_params() if over.get("runner", "advance") == "advance"
                      else {})
        params.update(over)
        return evaluate("version_precedence",
                        Context(world_root=world or self.world, params=params))


@pytest.fixture(scope="session")
def gt009(tmp_path_factory):
    base = tmp_path_factory.mktemp("gt009")
    out = {}
    for i, rollout in enumerate(gt009_subset()):
        w = GT009World(rollout, base / f"w{i}")
        res, nights, seen = w.consumer()
        out[rollout] = {"w": w, "cc": res, "nights": nights, "seen": seen}
    assert E2R1 in out, "the e2/r1 rollout (the recorded violation) is missing"
    return out


def test_gt009_consumer_consistency_equals_recorded_R1_to_R4(gt009):
    """The equivalence: for every rollout, the contract's per-night receipt
    verdict IS the task grader's recorded R1..R4, and hashes_match fails
    exactly when some recorded night failed, at that night."""
    table = []
    for rollout, run in gt009.items():
        recorded = recorded_nights(rollout)
        hm = clause(run["cc"], "hashes_match")
        table.append((rollout.parent.name + "/" + rollout.name, recorded, run["nights"]))
        assert run["nights"] == recorded, (table[-1], summary(run["cc"]))
        assert hm.passed == all(recorded), (table[-1], summary(run["cc"]))
        if not all(recorded):
            first = run["w"].dates[recorded.index(False)]
            assert hm.checkpoint.startswith(first), (hm.checkpoint, first)
        assert clause(run["cc"], "every_night_reached").passed
        assert clause(run["cc"], "hash_is_canonical").passed, summary(run["cc"])
    assert any(not all(r) for _, r, _ in table), table   # e2/r1 must discriminate
    assert any(all(r) for _, r, _ in table), table


def test_gt009_e2r1_is_the_canonical_violation(gt009):
    run = gt009[E2R1]
    assert recorded_nights(E2R1) == [True, True, False, False]
    assert not run["cc"].passed and run["cc"].points == 0.0, summary(run["cc"])


def test_gt009_real_receipts_are_not_digests_of_their_sample(gt009):
    """Why `hash_is_canonical` checks the ledger SHAPE for list samples: a
    real receipt's hash covers the whole table, never `digest(sample)`.  The
    old rule flagged every real receipt as forged.  A real receipt with its
    hash swapped for a sample digest must still be caught."""
    run = gt009[E2R1]
    real = [r for r in run["seen"] if r.get("kind") == "table"]
    assert real and all(digest(r["sample"]) != r["canonical_hash"] for r in real)
    forged = dict(real[0], canonical_hash="not-a-hash", seq=10_000)
    ledger = run["w"].scratch / "forged" / "var" / "ledger"
    ledger.mkdir(parents=True)
    (ledger / "receipts.jsonl").write_text(json.dumps(forged) + "\n", encoding="utf-8")
    res = evaluate("consumer_consistency", Context(
        world_root=ledger.parent.parent, receipts=[forged],
        params={"expected": {str(forged["seq"]): forged["canonical_hash"]}}))
    assert not clause(res, "hash_is_canonical").passed, summary(res)


def test_gt009_table_contents_restates_the_ledger(gt009):
    """The contract's restatement of world.ledger.table_contents (it may not
    import `world`) hashes identically on real warehouses."""
    for run in list(gt009.values())[:2]:
        w = run["w"]
        for target in w.targets.values():
            assert table_contents(w.world, target) == \
                world_ledger.table_contents(w.world, target), target


def test_gt009_version_precedence_on_recorded_feeds(gt009):
    """End state: e2/r1 holds at t0 (its loader is fine on the shipped
    partitions).  Advance: the loader crashes on night 3's permuted header,
    so keys go missing from that night - the no_missing_keys clause, at the
    night the recorded R3 failed; highest-version itself holds.  A holding
    rollout holds on every night."""
    bad = gt009[E2R1]["w"]
    still = bad.precedence(runner="end_state")
    assert still.passed and still.points == still.max, summary(still)
    moved = bad.precedence()
    assert clause(moved, "highest_version").passed, summary(moved)
    nm = clause(moved, "no_missing_keys")
    assert not nm.passed and nm.checkpoint == bad.dates[2], summary(moved)
    good = next(r["w"] for k, r in gt009.items() if all(recorded_nights(k)))
    res = good.precedence()
    assert res.passed and res.points == res.max, summary(res)


def test_gt009_replay_wins_loader_is_the_named_shortcut(gt009, tmp_path):
    """GT009's REPLAY_WINS loader (latest delivery wins, not highest version),
    built from the solution loader exactly as run_baselines.py builds it,
    dropped into a recorded rollout (default r0): version_precedence fails
    highest_version on the fact table - while every receipt that world
    publishes still equals the ORACLE trajectory, because its agent's
    publisher does not inherit the facts' versions.  A receipt-only grade
    (GT009's R1..R4 shape, consumer_consistency) cannot see this shortcut in
    this world; the fact-level contract is what does."""
    w = next(r["w"] for k, r in gt009.items() if k.parent.name.startswith("default"))
    good = (GT009 / "solution" / "load_payments.py").read_text(encoding="utf-8")
    loaders = baseline_strings(GT009 / "tests" / "run_baselines.py",
                               ("DUPLICATE_LOADER", "REPLAY_WINS_LOADER"), GOOD_LOADER=good)
    world = copy_world(w.world, tmp_path / "world")
    (world / "workspace" / "pipelines" / "load_payments.py").write_text(
        loaders["REPLAY_WINS_LOADER"], encoding="utf-8")
    vp = w.precedence(world)
    hv = clause(vp, "highest_version")
    assert not vp.passed and not hv.passed, summary(vp)
    assert hv.checkpoint in w.dates[1:], summary(vp)
    res, nights, _ = w.consumer(world)
    assert clause(res, "hashes_match").passed, summary(res)
    assert nights == [True] * 4, nights


# ======================================================================
# BI002: consumer_consistency on BI copies, auditability on real dbt
# ======================================================================
FEB_IMPORT, FEB_WAREHOUSE = 2_322_780.00, 2_361_224.00


@pytest.fixture(scope="session")
def bi002(tmp_path_factory):
    base = tmp_path_factory.mktemp("bi002")
    root, grading = base / "root", base / "grading"
    env = {**os.environ, "PYTHONPATH": str(REPO / "world")}
    done = subprocess.run([PY, str(BI002 / "tests" / "build_world.py"), "--root", str(root),
                           "--grading", str(grading)], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    return {"root": root, "base": base}


def bi_copy(bi002, name: str) -> Path:
    world = copy_world(bi002["root"], bi002["base"] / name)
    profiles = world / "build" / "profiles.yml"
    if profiles.is_file():
        profiles.write_text(profiles.read_text(encoding="utf-8")
                            .replace(str(bi002["root"]), str(world)), encoding="utf-8")
    return world


def warehouse(world: Path) -> Path:
    return world / "var" / "warehouse" / "warehouse.duckdb"


def open_wh(world: Path, read_only: bool = False):
    """The warehouse attached under an alias: the file is named warehouse.duckdb
    AND holds a schema `warehouse`, so `warehouse.fct_orders` is ambiguous on a
    plain connect."""
    con = duckdb.connect(":memory:")
    path = warehouse(world).as_posix().replace("'", "''")
    con.execute(f"ATTACH '{path}' AS wh" + (" (READ_ONLY)" if read_only else ""))
    con.execute("USE wh")
    return con


def reexport(world: Path, relations=None) -> None:
    """A Power BI refresh: re-export the import's CSVs from the warehouse and
    stamp the record, as gen.py's export does."""
    record = world / "var" / "bi" / "powerbi" / "datasets.json"
    doc = json.loads(record.read_text(encoding="utf-8"))
    con = open_wh(world, read_only=True)
    try:
        for body in doc["datasets"].values():
            for rel in body["import"]["relations"]:
                if relations is None or rel["relation"] in relations:
                    out = (record.parent / rel["path"]).as_posix().replace("'", "''")
                    con.execute(f"COPY (SELECT * FROM {rel['relation']}) TO '{out}' "
                                f"(HEADER, DELIMITER ',')")
            if relations is None:
                body["import"]["refreshed"] = "2026-03-02T00:00:00Z"
    finally:
        con.close()
    record.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def powerbi_clauses(res) -> dict:
    return {c.id.split(":")[-1]: c for c in res.clauses if ":powerbi-import:" in c.id}


def test_bi002_powerbi_import_is_stale_as_the_task_documents(bi002):
    """The canonical violation, and the BI002 equivalence: the import the task
    plants is stale, and the contract's fct_orders disagreement is the
    February gap the task's README states."""
    world = bi002["root"]
    res = evaluate("consumer_consistency", Context(world_root=world, params={"bi": {}}))
    pbi = powerbi_clauses(res)
    assert not res.passed and not pbi["warehouse.fct_orders"].passed, summary(res)
    assert pbi["warehouse.fct_orders"].checkpoint == "2026-03-01T06:00:00Z"
    assert pbi["warehouse.dim_product"].passed, summary(res)
    csv = (world / "var/bi/powerbi/import/Commerce/warehouse.fct_orders.csv").as_posix()
    feb = ("SELECT round(sum(order_amount) FILTER (WHERE order_status = 'completed'), 2) "
           "FROM {src} WHERE order_date >= DATE '2026-02-01' AND order_date < DATE '2026-03-01'")
    con = open_wh(world, read_only=True)
    try:
        imp = con.execute(feb.format(src=f"read_csv('{csv}', header=true)")).fetchone()[0]
        now = con.execute(feb.format(src="warehouse.fct_orders")).fetchone()[0]
    finally:
        con.close()
    assert (float(imp), float(now)) == (FEB_IMPORT, FEB_WAREHOUSE)


def test_bi002_powerbi_reexport_holds_and_partial_refresh_does_not(bi002):
    """Holding: every relation re-exported.  Named shortcut: refreshing only
    the fact the dashboard headline reads (fct_orders) leaves fct_order_lines
    and dim_date stale - the surface is judged relation by relation."""
    full = bi_copy(bi002, "pbi_full")
    reexport(full)
    res = evaluate("consumer_consistency", Context(world_root=full, params={"bi": {}}))
    assert res.passed and res.points == res.max, summary(res)
    part = bi_copy(bi002, "pbi_part")
    reexport(part, {"warehouse.fct_orders"})
    res = evaluate("consumer_consistency", Context(world_root=part, params={"bi": {}}))
    pbi = powerbi_clauses(res)
    assert pbi["warehouse.fct_orders"].passed, summary(res)
    assert not res.passed and not pbi["warehouse.fct_order_lines"].passed, summary(res)


@pytest.fixture(scope="session")
def bi002_refreshed(bi002):
    """The world's own 06:00 job (`bi_refresh` = gen.refresh_bi, which runs
    workspace/scripts/refresh_dashboards.py) run once, Power BI re-exported,
    so the YAML caches are the only variable.  Running the whole night through
    scheduler.advance reaches the same caches but costs ~36s (daily_load
    rebuilds raw from every partition, then dbt)."""
    from world.themes.commerce_analytics.gen import refresh_bi
    world = bi_copy(bi002, "yaml")
    reexport(world)
    refresh_bi(world)
    assert len(list((world / "build/bi/refresh").glob("*.json"))) == 2
    return world


def test_bi002_yaml_cache_holds_after_the_refresh_job(bi002_refreshed):
    world = bi002_refreshed
    by_render = evaluate("consumer_consistency", Context(
        world_root=world, params={"bi": {"render": world_bi.render}}))
    assert by_render.passed and by_render.points == by_render.max, summary(by_render)
    yaml_ids = [c.id for c in by_render.clauses if ":yaml:" in c.id]
    assert len(yaml_ids) == 2, yaml_ids


def test_bi002_yaml_cache_goes_stale_when_the_marts_move(bi002_refreshed, tmp_path):
    world = copy_world(bi002_refreshed, tmp_path / "w")
    con = open_wh(world)
    try:
        con.execute("UPDATE warehouse.fct_orders SET order_status = 'refunded' WHERE "
                    "order_id = (SELECT max(order_id) FROM warehouse.fct_orders "
                    "WHERE order_status = 'completed')")
    finally:
        con.close()
    res = evaluate("consumer_consistency", Context(
        world_root=world, params={"bi": {"render": world_bi.render, "discover": True}}))
    stale = [c for c in res.clauses if ":yaml:" in c.id and not c.passed]
    assert stale and not res.passed, summary(res)


def test_bi002_tableau_extract_and_sigma_catalog(bi002_refreshed, tmp_path):
    """Vendor state: a Tableau extract written by the gateway's own copier
    and a Sigma catalog from world.semantic's Catalog.from_warehouse.  Fresh
    they hold; a moved mart makes the extract stale; a dropped column makes
    the catalog stale."""
    sys.path.insert(0, str(REPO / "gateway" / "src"))
    try:
        from alkera_gateway.native.services.tableau import _copy_relations
    finally:
        sys.path.remove(str(REPO / "gateway" / "src"))
    from world.semantic.formats.sigma.catalog import Catalog
    world = copy_world(bi002_refreshed, tmp_path / "w")
    state = tmp_path / "bi_state"
    (state / "tableau" / "extracts").mkdir(parents=True)
    (state / "sigma").mkdir(parents=True)
    _copy_relations(warehouse(world), state / "tableau/extracts/sales.duckdb",
                    ["warehouse.fct_orders", "warehouse.dim_customer"])
    catalog = Catalog.from_warehouse(warehouse(world)).tables
    (state / "sigma/state.json").write_text(json.dumps({"catalog": catalog}), encoding="utf-8")
    params = {"bi": {"bi_state_dir": str(state), "render": world_bi.render}}
    res = evaluate("consumer_consistency", Context(world_root=world, params=params))
    vendor = [c for c in res.clauses if ":tableau:" in c.id or ":sigma-catalog:" in c.id]
    assert len(vendor) == 2 + len(catalog) and res.passed, summary(res)

    con = open_wh(world)
    try:
        con.execute("DELETE FROM warehouse.fct_orders WHERE order_id = "
                    "(SELECT max(order_id) FROM warehouse.fct_orders)")
    finally:
        con.close()
    catalog["warehouse.dim_product"].pop(sorted(catalog["warehouse.dim_product"])[0])
    (state / "sigma/state.json").write_text(json.dumps({"catalog": catalog}), encoding="utf-8")
    res = evaluate("consumer_consistency", Context(
        world_root=world, params={"bi": {"bi_state_dir": str(state), "discover": True,
                                         "render": world_bi.render}}))
    failed = {c.id for c in res.clauses if not c.passed}
    assert "consumer_consistency.bi:tableau:sales:warehouse.fct_orders" in failed, summary(res)
    assert "consumer_consistency.bi:sigma-catalog:warehouse.dim_product" in failed, summary(res)
    assert "consumer_consistency.bi:tableau:sales:warehouse.dim_customer" not in failed


# ---------------------------------------------------------------- auditability
DBT_TESTS = ("test.commerce_analytics.accepted_values_stg_orders_order_status__completed"
             "__refunded__cancelled.99bac945fa",
             "test.commerce_analytics.accepted_values_fct_orders_order_status__completed"
             "__refunded__cancelled.2e499e9448")
DBT_ENV = {"DBT_SEND_ANONYMOUS_USAGE_STATS": "false", "DO_NOT_TRACK": "1"}


def dbt_step(profile: str) -> dict:
    """dbt build as a contract `command` step: its own profiles dir pointing at
    the phase's warehouse copy ({db}) and its own target path, so nothing in
    the world under test is touched."""
    return {"name": "dbt_build",
            "write": {"build/_contract/profiles.yml":
                      f"{profile}:\n  target: dev\n  outputs:\n    dev:\n"
                      f"      type: duckdb\n      path: '{{db}}'\n      threads: 1\n"},
            "command": ["{python}", "-m", "dbt.cli.main", "--no-use-colors", "build",
                        "--project-dir", "{workspace}/dbt_project",
                        "--profiles-dir", "{world_root}/build/_contract",
                        "--target-path", "{world_root}/build/_contract/target",
                        "--log-path", "{world_root}/build/_contract/logs",
                        "--threads", "1"],
            "env": DBT_ENV, "timeout": 600}


def audit_params(world: Path, results: str = "build/target/run_results.json") -> dict:
    profile = yaml.safe_load((world / "workspace/dbt_project/dbt_project.yml")
                             .read_text(encoding="utf-8"))["profile"]
    step = dbt_step(profile)
    return {"declared": list(DBT_TESTS), "results": results,
            "phases": [{"name": "clean", "expect": "green", "steps": [step]},
                       {"name": "novel_status", "expect": "red", "steps": [step],
                        "plant_sql": ["UPDATE raw.orders SET status = 'disputed' WHERE "
                                      "order_id = (SELECT min(order_id) FROM raw.orders)"]}]}


def fresh_results(world: Path) -> int:
    """The results file a real run of THIS project writes (build/target); the
    shipped one is removed first so a run that fails to parse cannot leave the
    old file standing in for it."""
    profiles = world / "build"
    (world / "build/target/run_results.json").unlink(missing_ok=True)
    done = subprocess.run([PY, "-m", "dbt.cli.main", "--no-use-colors", "build",
                           "--project-dir", str(world / "workspace/dbt_project"),
                           "--profiles-dir", str(profiles),
                           "--target-path", str(world / "build/target"),
                           "--log-path", str(world / "build/target/logs"), "--threads", "1"],
                          cwd=world / "workspace", capture_output=True, text=True,
                          env={**os.environ, **DBT_ENV}, timeout=600)
    assert (world / "build/target/run_results.json").is_file(), done.stdout[-3000:]
    return done.returncode


def test_auditability_holds_on_the_shipped_dbt_project(bi002):
    world = bi_copy(bi002, "audit_hold")
    res = evaluate("auditability", Context(world_root=world, params=audit_params(world)))
    assert res.passed and res.points == res.max, summary(res)


def test_auditability_dbtignored_schema_file_is_the_violation(bi002):
    """The canonical violation: the staging schema file is .dbtignore'd.  The
    project still parses and builds green, and the staging check never runs:
    declared_ran names it.  (Ignoring _marts.yml as well does not parse - it
    carries the semantic layer's time spine - so a real project only lets one
    of the two copies be silenced; the marts copy still catches the novel
    value and discriminates holds.)"""
    world = bi_copy(bi002, "audit_ignored")
    (world / "workspace/dbt_project/.dbtignore").write_text(
        "models/staging/_staging.yml\n", encoding="utf-8")
    assert fresh_results(world) == 0
    res = evaluate("auditability", Context(world_root=world, params=audit_params(world)))
    dr = clause(res, "declared_ran")
    assert not res.passed and not dr.passed and DBT_TESTS[0] in dr.detail, summary(res)
    assert DBT_TESTS[1] not in dr.detail
    assert clause(res, "discriminates").passed, summary(res)


def test_auditability_warn_severity_is_the_named_shortcut(bi002):
    """The shortcut: the declared checks stay in the project, run and report,
    but at severity warn - the build is green on the novel value."""
    world = bi_copy(bi002, "audit_warn")
    for rel in ("models/staging/_staging.yml", "models/marts/_marts.yml"):
        path = world / "workspace/dbt_project" / rel
        text = path.read_text(encoding="utf-8")
        new = re.sub(r"^(\s*)arguments: \{values: \[completed, refunded, cancelled\]\}$",
                     r"\g<0>\n\1config: {severity: warn}", text, flags=re.M)
        assert new != text, rel
        path.write_text(new, encoding="utf-8")
    assert fresh_results(world) == 0
    res = evaluate("auditability", Context(world_root=world, params=audit_params(world)))
    assert not res.passed, summary(res)
    assert not clause(res, "discriminates").passed, summary(res)


# ======================================================= synthetic scheduled worlds
import world.ledger as ledger                        # noqa: E402

VNOW = "2026-03-05T06:00:00Z"
VSTART = "2026-03-01T00:00:00Z"

EVENTS = [
    {"seq": "1", "order_id": "O-1", "version": "1", "event_type": "create",
     "order_day": "2026-03-01", "amount": "10.00"},
    # v3 arrives before v2. Arrival order is not version order.
    {"seq": "2", "order_id": "O-1", "version": "3", "event_type": "update",
     "order_day": "2026-03-01", "amount": "13.00"},
    {"seq": "3", "order_id": "O-1", "version": "2", "event_type": "update",
     "order_day": "2026-03-01", "amount": "12.00"},
    {"seq": "4", "order_id": "O-2", "version": "1", "event_type": "create",
     "order_day": "2026-03-01", "amount": "20.00"},
    # Byte-identical replay of a version already delivered.
    {"seq": "5", "order_id": "O-2", "version": "1", "event_type": "create",
     "order_day": "2026-03-01", "amount": "20.00"},
    {"seq": "6", "order_id": "O-3", "version": "1", "event_type": "create",
     "order_day": "2026-03-02", "amount": "30.00"},
    {"seq": "7", "order_id": "O-3", "version": "2", "event_type": "delete",
     "order_day": "2026-03-02", "amount": "30.00"},
]
# What the fold owes: O-1 at v3, O-2 at v1, O-3 removed.
CORRECT = [("O-1", 3, "2026-03-01", "13.00"), ("O-2", 1, "2026-03-01", "20.00")]
# What the world holds before the scheduled loader runs.
STALE = [("O-1", 1, "2026-03-01", "10.00"), ("O-3", 1, "2026-03-02", "30.00")]

VERSION_PARAMS = {
    "target": "warehouse.fct_orders_v",
    "key_column": "order_id",
    "version_column": "version",
    "feed_dir": "workspace/data/orders",
    "type_field": "event_type",
    "delete_value": "delete",
    "identity_fields": ["order_id", "version", "event_type", "order_day", "amount"],
    "compare": {"amount": "amount", "order_day": "order_day"},
}

LOAD_SCRIPT = '''\
import csv
import sys
from pathlib import Path

import duckdb

workspace, db, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
events = []
feed = workspace / "data" / "orders"
for part in sorted(p for p in feed.glob("dt=*") if p.is_dir()):
    for payload in sorted(part.glob("*.csv")):
        with open(payload, newline="", encoding="utf-8-sig") as fh:
            events.extend(csv.DictReader(fh))
best = {}
for ev in events:
    key = ev["order_id"]
    if mode == "arrival":
        best[key] = ev
    elif key not in best or int(ev["version"]) > int(best[key]["version"]):
        best[key] = ev
rows = [(ev["order_id"], int(ev["version"]), ev["order_day"], ev["amount"])
        for ev in best.values() if ev["event_type"] != "delete"]
con = duckdb.connect(":memory:", config={"threads": 1})
con.execute("ATTACH '" + db.as_posix() + "' AS wh")
con.execute("CREATE SCHEMA IF NOT EXISTS wh.warehouse")
con.execute(
    "CREATE OR REPLACE TABLE wh.warehouse.fct_orders_v ("
    "order_id VARCHAR, version INTEGER, order_day DATE, amount DECIMAL(10,2))")
con.executemany(
    "INSERT INTO wh.warehouse.fct_orders_v VALUES (?, ?, CAST(? AS DATE), ?)", rows)
con.execute("CHECKPOINT")
con.close()
'''

ASSERT_SCRIPT = '''\
import sys
from pathlib import Path

import duckdb

db = Path(sys.argv[1])
con = duckdb.connect(":memory:", config={"threads": 1})
con.execute("ATTACH '" + db.as_posix() + "' AS wh (READ_ONLY)")
negatives = con.execute(
    "SELECT COUNT(*) FROM wh.warehouse.fct_orders WHERE amount < 0").fetchone()[0]
con.close()
if negatives:
    sys.stderr.write("assertion amounts_non_negative failed\\n")
    sys.exit(1)
'''

NOOP_SCRIPT = "import sys\nsys.exit(0)\n"

ORACLE_SQL = (
    "SELECT order_id, CAST(version AS VARCHAR), CAST(order_day AS VARCHAR), "
    "CAST(amount AS VARCHAR) FROM wh.\"warehouse\".\"fct_orders_v\" ORDER BY order_id"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _connect(db: Path, *, read_only: bool = False):
    con = duckdb.connect(":memory:", config={"threads": 1})
    mode = " (READ_ONLY)" if read_only else ""
    con.execute(f"ATTACH '{db.as_posix()}' AS wh{mode}")
    return con


def _clock(root: Path, job: str, exec_spec: dict, *, command: str,
           publishes: list | None = None) -> None:
    _write(root / "var/sched/state.json", json.dumps({
        "vnow": VNOW,
        "jobs": {job: {"command": command, "duration_s": 30}},
        "runs": [],
    }) + "\n")
    spec = {
        "command": command,
        "duration_s": 30,
        "time": "05:00",
        "day_offset": 0,
        "exec": exec_spec,
    }
    if publishes:
        spec["publishes"] = publishes
    _write(root / "var/world/live.json", json.dumps({
        "vtime_start": VSTART,
        "jobs": {job: spec},
    }) + "\n")


def _seed_orders(db: Path, rows: list) -> None:
    con = _connect(db)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS wh.warehouse")
        con.execute(
            "CREATE TABLE wh.warehouse.fct_orders_v ("
            "order_id VARCHAR, version INTEGER, order_day DATE, amount DECIMAL(10,2))")
        for order_id, version, day, amount in rows:
            con.execute(
                "INSERT INTO wh.warehouse.fct_orders_v VALUES "
                "(?, ?, CAST(? AS DATE), ?)",
                [order_id, version, day, amount])
        con.execute("CHECKPOINT")
    finally:
        con.close()


def build_version_world(root: Path, *, fold: str, rows: list,
                        publish: bool = False) -> Path:
    """Feed, warehouse, workspace loader, and a scheduler that will run it."""
    root = Path(root)
    db = root / "var" / "warehouse" / "warehouse.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    part = root / "workspace" / "data" / "orders" / "dt=2026-03-06"
    part.mkdir(parents=True, exist_ok=True)
    with open(part / "events.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(EVENTS[0]))
        writer.writeheader()
        writer.writerows(EVENTS)
    _write(root / "workspace" / "pipelines" / "load_orders.py", LOAD_SCRIPT)
    _seed_orders(db, rows)
    _clock(root, "load_orders", {
        "kind": "script",
        "path": "pipelines/load_orders.py",
        "args": ["{workspace}", "{db}", fold],
    }, command="load_orders", publishes=(
        [{"target": "warehouse.fct_orders_v", "kind": "table", "consumer": "finance"}]
        if publish else None))
    return root


def build_audit_world(root: Path, *, assertion: str) -> Path:
    root = Path(root)
    db = root / "var" / "warehouse" / "warehouse.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    script = ASSERT_SCRIPT if assertion == "live" else NOOP_SCRIPT
    _write(root / "workspace" / "pipelines" / "assert_amounts.py", script)
    _write(root / "workspace" / "target" / "run_results.json", json.dumps({
        "results": [{"name": "amounts_non_negative", "status": "pass"}],
    }) + "\n")
    con = _connect(db)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS wh.warehouse")
        con.execute(
            "CREATE TABLE wh.warehouse.fct_orders ("
            "order_id VARCHAR, amount DECIMAL(10,2))")
        con.execute(
            "INSERT INTO wh.warehouse.fct_orders VALUES ('O-1', 10.00), ('O-2', 20.00)")
        con.execute("CHECKPOINT")
    finally:
        con.close()
    _clock(root, "assert_amounts", {
        "kind": "script",
        "path": "pipelines/assert_amounts.py",
        "args": ["{db}"],
    }, command="assert_amounts")
    return root


def advance(root: Path, hours: float = 24) -> None:
    """The same child-process entry the contracts use: python -m world.scheduler."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO / "world") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "world.scheduler", str(root), "advance", str(float(hours))],
        capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, (proc.stderr or proc.stdout)[-2000:]


def _held(root: Path, table: str = "fct_orders_v") -> list:
    db = root / "var" / "warehouse" / "warehouse.duckdb"
    con = _connect(db, read_only=True)
    try:
        return con.execute(
            f"SELECT order_id, version FROM wh.warehouse.{table} ORDER BY 1"
        ).fetchall()
    finally:
        con.close()


def _by_id(result):
    return {clause.id: clause for clause in result.clauses}


def evaluate_contract(root: Path, name: str, params: dict, receipts=None):
    ctx = Context(world_root=root, params=params, receipts=receipts or [])
    try:
        return evaluate(name, ctx)
    finally:
        ctx.close()


def test_version_precedence_snapshot_reads_the_real_feed(tmp_path):
    root = build_version_world(tmp_path / "world", fold="highest", rows=CORRECT)
    assert (root / "var/warehouse/warehouse.duckdb").is_file()
    assert (root / "workspace").is_dir()
    assert (root / "var/sched/state.json").is_file()
    result = evaluate_contract(root, "version_precedence",
                               {**VERSION_PARAMS, "runner": "snapshot"})
    assert result.passed, result.reason
    assert all(clause.passed for clause in result.clauses), result.reason


def test_version_precedence_advance_executes_the_scheduled_loader(tmp_path):
    root = build_version_world(tmp_path / "world", fold="highest", rows=STALE)
    result = evaluate_contract(root, "version_precedence", {
        **VERSION_PARAMS, "runner": "advance", "advance_hours": 24})
    assert result.passed, result.reason
    assert all(clause.passed for clause in result.clauses), result.reason
    # The graded world is copied before advance. Its clock and its stale rows stay.
    state = json.loads((root / "var/sched/state.json").read_text(encoding="utf-8"))
    assert state["vnow"] == VNOW
    assert state["runs"] == []
    assert _held(root) == [("O-1", 1), ("O-3", 1)]


def test_version_precedence_advance_fails_closed_on_arrival_order(tmp_path):
    """Last write wins. O-1 is left at v2 after v3 had already landed.

    no_resurrection still holds, and it must not be paid: highest_version is
    the fatal gate.
    """
    root = build_version_world(tmp_path / "world", fold="arrival", rows=STALE)
    result = evaluate_contract(root, "version_precedence", {
        **VERSION_PARAMS, "runner": "advance", "advance_hours": 24})
    clauses = _by_id(result)
    assert not clauses["version_precedence.highest_version"].passed
    assert clauses["version_precedence.highest_version"].fatal
    assert "target v2, ledger v3" in clauses["version_precedence.highest_version"].detail
    assert clauses["version_precedence.no_resurrection"].passed
    assert result.points == 0.0
    assert result.max > 0


def _oracle_rows(root: Path) -> list:
    ctx = Context(world_root=root, params={})
    try:
        return [[None if value is None else str(value) for value in row]
                for row in ctx.query(ORACLE_SQL)]
    finally:
        ctx.close()


def _bi_bindings(root: Path) -> list:
    columns = ["order_id", "version", "order_day", "amount"]
    db = root / "var" / "warehouse" / "warehouse.duckdb"
    con = _connect(db, read_only=True)
    try:
        rows = con.execute(
            "SELECT order_id, version, order_day, amount "
            "FROM wh.warehouse.fct_orders_v ORDER BY order_id").fetchall()
    finally:
        con.close()
    bindings = []
    for consumer in ("powerbi", "tableau", "sigma", "yaml"):
        extract = root / "var" / "bi" / consumer / "import.csv"
        extract.parent.mkdir(parents=True, exist_ok=True)
        with open(extract, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            for row in rows:
                writer.writerow("" if value is None else str(value) for value in row)
        definition = extract.with_suffix(".yaml")
        definition.write_text("dataset: warehouse.fct_orders_v\n", encoding="utf-8")
        bindings.append({
            "consumer": consumer,
            "mart": "warehouse.fct_orders_v",
            "columns": columns,
            "format": "csv",
            "extract": extract.relative_to(root).as_posix(),
            "definition": definition.relative_to(root).as_posix(),
        })
    return bindings


def test_consumer_consistency_checks_a_real_receipt_and_bi_extracts(tmp_path):
    root = build_version_world(tmp_path / "world", fold="highest", rows=STALE, publish=True)
    advance(root)
    receipts = ledger.load(root)
    assert receipts and {r["kind"] for r in receipts} == {"table"}
    fresh = {r["target"]: ledger.table_contents(root, r["target"])["canonical_hash"]
             for r in receipts}
    assert all(r["canonical_hash"] == fresh[r["target"]] for r in receipts)
    expected = {str(r["seq"]): fresh[r["target"]] for r in receipts}
    result = evaluate_contract(
        root, "consumer_consistency",
        {"expected": expected, "bindings": _bi_bindings(root)}, receipts=receipts)
    assert result.passed, result.reason
    assert all(clause.passed for clause in result.clauses), result.reason

    _write(root / "workspace" / "oracle.py",
           "def published_at(receipt, ctx):\n"
           f"    rows = ctx.query({ORACLE_SQL!r})\n"
           "    return [[None if value is None else str(value) for value in row] "
           "for row in rows]\n")
    logical = _oracle_rows(root)
    oracle_receipts = [dict(receipts[0], canonical_hash=digest(logical), sample=logical)]
    oracle = evaluate_contract(
        root, "consumer_consistency",
        {"oracle": "workspace/oracle.py::published_at"}, receipts=oracle_receipts)
    assert oracle.passed, oracle.reason


def test_consumer_consistency_fails_closed_when_the_warehouse_drifts(tmp_path):
    root = build_version_world(tmp_path / "world", fold="highest", rows=STALE, publish=True)
    advance(root)
    receipts = ledger.load(root)
    db = root / "var" / "warehouse" / "warehouse.duckdb"
    con = _connect(db)
    try:
        con.execute(
            "UPDATE wh.warehouse.fct_orders_v SET amount = 99 WHERE order_id = 'O-1'")
        con.execute("CHECKPOINT")
    finally:
        con.close()
    expected = {str(r["seq"]): ledger.table_contents(root, r["target"])["canonical_hash"]
                for r in receipts}
    result = evaluate_contract(
        root, "consumer_consistency", {"expected": expected}, receipts=receipts)
    clauses = _by_id(result)
    assert not clauses["consumer_consistency.hashes_match"].passed
    assert clauses["consumer_consistency.hashes_match"].fatal
    assert clauses["consumer_consistency.every_receipt_covered"].passed
    assert result.points == 0.0
    assert result.max > 0


def _audit_params():
    return {
        "declared": ["amounts_non_negative"],
        "results": "workspace/target/run_results.json",
        "runner": "advance",
        "advance_hours": 24,
        "phases": [
            {"name": "clean", "expect": "green", "advance_hours": 24},
            {"name": "novel", "expect": "red", "advance_hours": 24, "plant_sql": [
                "INSERT INTO wh.warehouse.fct_orders VALUES ('Z-NEG', -5.00)"]},
        ],
    }


def test_auditability_advance_fires_green_and_red(tmp_path):
    root = build_audit_world(tmp_path / "world", assertion="live")
    result = evaluate_contract(root, "auditability", _audit_params())
    assert result.passed, result.reason
    assert "clean:green" in result.reason
    assert "novel:red" in result.reason
    db = root / "var" / "warehouse" / "warehouse.duckdb"
    con = _connect(db, read_only=True)
    try:
        negatives = con.execute(
            "SELECT COUNT(*) FROM wh.warehouse.fct_orders WHERE amount < 0"
        ).fetchone()[0]
    finally:
        con.close()
    assert negatives == 0


def test_auditability_advance_fails_closed_when_the_assertion_cannot_go_red(tmp_path):
    root = build_audit_world(tmp_path / "world", assertion="noop")
    result = evaluate_contract(root, "auditability", _audit_params())
    clauses = _by_id(result)
    assert clauses["auditability.declared_ran"].passed
    assert not clauses["auditability.discriminates"].passed
    assert clauses["auditability.discriminates"].fatal
    assert "novel: expected red, got green" in clauses["auditability.discriminates"].detail
    assert result.points == 0.0
    assert result.max > 0
