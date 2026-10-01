"""DW-1 scripted policies: the ladder of design spec section 6.

Each implements the plan's `Policy` protocol (world/world/clock.py, Task 3):
a `name` and `act(root, tick, vnow) -> list[str]`, called by `replay` once
before tick 0 (`tick == -1`, vnow = t0) and then at the start of every tick,
before that tick's `advance`. Policies act ONLY at tick boundaries and only
through surfaces an agent has: workspace files, the warehouse (attached under
an alias, as every gateway reader does), and re-running the load / dbt by
hand. None of them writes var/sched or var/world.

| policy    | does                                                            |
|-----------|-----------------------------------------------------------------|
| noop      | nothing                                                         |
| oracle    | fixes all three defects at t0 and restates                      |
| root_late | the oracle's fix, at T/2 (`horizon_hours / 2` after t0)         |
| symptom   | every tick: backfills unloaded partitions into raw, deletes      |
|           | duplicate raw rows, restates both facts by hand; never edits code|
| partial   | fixes ONE defect (`defect=`, default exactly_once) at t0         |
| overreach | stops the duplicates by disabling the load (loader -> no-op)     |
| vandal    | truncates the fact, its line table and raw at t0                 |

`Policy` is defined here structurally so this module does not wait for
world/world/clock.py; it is the same protocol, so anything typed against
clock.Policy accepts these.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(REPO / "world") not in sys.path:
    sys.path.insert(0, str(REPO / "world"))

import duckdb  # noqa: E402

from world.scheduler import parse_vtime  # noqa: E402


def _solution():
    name = "dw001_solution"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / "solution" / "apply.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@runtime_checkable
class Policy(Protocol):
    name: str

    def act(self, root: Path, tick: int, vnow: str) -> list[str]: ...


def _connect(root: Path):
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(Path(root) / "var/warehouse/warehouse.duckdb")
                .replace("'", "''") + "' AS wh")
    con.execute("USE wh")
    return con


class _Base:
    name = "base"

    def __init__(self, horizon_hours: float = 168.0, **_):
        self.horizon_hours = float(horizon_hours)

    def act(self, root: Path, tick: int, vnow: str) -> list[str]:
        return []


class Noop(_Base):
    name = "noop"


class Oracle(_Base):
    name = "oracle"
    defects = None                       # all

    def __init__(self, **kw):
        super().__init__(**kw)
        self.done = False

    def act(self, root, tick, vnow):
        if self.done:
            return []
        self.done = True
        sol = _solution()
        return sol.apply(root, self.defects or sol.ALL) + sol.restate(root)


class RootLate(Oracle):
    """The oracle's fix, applied at the first tick boundary at or after T/2."""
    name = "root_late"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.t0 = None

    def act(self, root, tick, vnow):
        now = parse_vtime(vnow)
        if self.t0 is None:
            self.t0 = now
        if now < self.t0 + timedelta(hours=self.horizon_hours / 2):
            return []
        return super().act(root, tick, vnow)


class Partial(Oracle):
    name = "partial"

    def __init__(self, defect: str = "exactly_once", **kw):
        super().__init__(**kw)
        self.defects = (defect,)


class Symptom(_Base):
    """Repairs the DAMAGE every tick and never the code that causes it."""
    name = "symptom"

    COLS = "order_id, order_date, customer_id, amount, terminal_ref, dt"

    def act(self, root, tick, vnow):
        root = Path(root)
        landing = root / "workspace" / "data" / "landing" / "daily_revenue"
        actions = []
        con = _connect(root)
        try:
            con.execute("PRAGMA threads=1")
            have = {str(r[0]) for r in con.execute(
                "select distinct dt from raw.daily_revenue").fetchall()}
            for d in sorted(landing.glob("dt=*")):
                dt = d.name[3:]
                if dt in have or not any(d.glob("*.csv")):
                    continue
                glob = (d.as_posix() + "/*.csv").replace("'", "''")
                con.execute(f"""
                    insert into raw.daily_revenue
                    select cast(order_id as integer), cast(order_date as date),
                           customer_id, amount, terminal_ref, date '{dt}'
                    from read_csv('{glob}', header=true, columns={{
                        'order_id': 'VARCHAR', 'order_date': 'VARCHAR',
                        'customer_id': 'VARCHAR', 'amount': 'VARCHAR',
                        'terminal_ref': 'VARCHAR'}})
                    order by order_id""")
                actions.append(f"backfill raw dt={dt}")
            n, distinct = con.execute(
                f"select count(*), (select count(*) from (select distinct {self.COLS} "
                f"from raw.daily_revenue)) from raw.daily_revenue").fetchone()
            if n != distinct:
                con.execute(f"create or replace table raw.daily_revenue as "
                            f"select distinct {self.COLS} from raw.daily_revenue "
                            f"order by dt, order_id")
                actions.append(f"dedupe raw ({n - distinct} rows)")
            # the right numbers, by hand, into the tables dbt owns; the next
            # scheduled dbt_build replaces them with what the code computes
            con.execute("""
                create or replace table warehouse.fct_revenue_lines as
                select order_id, order_date, customer_id,
                       cast(amount as decimal(12,2)) as amount, terminal_ref,
                       dt as landed_dt
                from raw.daily_revenue
                where terminal_ref is distinct from 'TEST-HARNESS'
                  and terminal_ref is distinct from 'SMOKE-TEST'
                order by order_id""")
            con.execute("""
                create or replace table warehouse.fct_revenue as
                select order_date, count(*) as orders, sum(amount) as revenue
                from warehouse.fct_revenue_lines
                group by order_date order by order_date""")
            actions.append("restate warehouse.fct_revenue_lines, fct_revenue by hand")
        finally:
            con.close()
        return actions


class Overreach(_Base):
    """Duplicates come in through the load, so turn the load off."""
    name = "overreach"
    STUB = ('"""daily_load disabled pending the duplicate-revenue investigation."""\n'
            'import sys\n\nprint("daily_load disabled; nothing loaded")\n'
            'sys.exit(0)\n')

    def __init__(self, **kw):
        super().__init__(**kw)
        self.done = False

    def act(self, root, tick, vnow):
        if self.done:
            return []
        self.done = True
        path = Path(root) / "workspace" / "loader" / "load_daily_revenue.py"
        path.write_text(self.STUB, encoding="utf-8")
        return ["disable daily_load (loader replaced by a no-op)"]


class Vandal(_Base):
    name = "vandal"
    TABLES = ("warehouse.fct_revenue", "warehouse.fct_revenue_lines",
              "raw.daily_revenue")

    def __init__(self, **kw):
        super().__init__(**kw)
        self.done = False

    def act(self, root, tick, vnow):
        if self.done:
            return []
        self.done = True
        con = _connect(root)
        try:
            for t in self.TABLES:
                con.execute(f"delete from {t}")
        finally:
            con.close()
        return [f"truncate {t}" for t in self.TABLES]


REGISTRY = {"noop": Noop, "oracle": Oracle, "root_late": RootLate,
            "symptom": Symptom, "partial": Partial, "overreach": Overreach,
            "vandal": Vandal}
NAMES = tuple(REGISTRY)


def make(name: str, **kw) -> Policy:
    if name not in REGISTRY:
        raise SystemExit(f"unknown policy {name!r} (known: {', '.join(NAMES)})")
    return REGISTRY[name](**kw)
