"""Airflow's view of this world's scheduler: DAGs from the workspace, runs from the ledger.

The Airflow plug (`alkera_gateway.native.services.airflow`, Airflow 3 REST
`/api/v2` at airflow.internal:8100) is a VIEW, not a second scheduler. Two
sources, both the agent's own:

  the DAGs     `workspace/dags/*.py`, read with `ast` - never imported, there is
               no `airflow` package on this box - the way the themes' own
               `scripts/dag_check.py` reads them: dag_id, schedule, tasks, the
               `>>` / `<<` / `set_downstream` / `chain` edges, default_args.
               Read live, as the designed real-Airflow stanza in
               compose.native.yml would (`DAGS_FOLDER=/world/workspace/dags`).
  the runs     `var/sched/state.json`, the ledger - the single time authority
               and the single record of what executed - plus the engine output
               `world/world/executor.py` keeps at `build/sched/logs/run-NNNNNN.log`.

THE MAPPING (the archived bridge, `_archive/world/world/airflow_bridge.py`,
read and not imported):

  task  -> job       A task whose task_id is a ledger job (`state.json` `jobs`)
                     runs that job; the themes name them so on purpose ("task_id
                     is the job name in sched_state - keep it that way"). A task
                     that is no job (a sensor, an EmptyOperator) is shown and
                     never runs: its task instances have no state and say why.
  DAG run            Scheduled history: one DAG run per occurrence of the DAG's
                     root job(s) - a root-job run starts one, every other run of
                     the DAG's jobs (a retry attempt included) joins the latest.
                     Its logical date is the root run's `vts_start`
                     (`scheduled__<iso>`), which is Airflow 3's CronTriggerTimetable
                     instant: `run_after == logical_date`, an empty data interval.
                     A run the Airflow API asked for carries its dag_run_id in
                     the ledger record's `origin` and is grouped by it
                     (`manual__...`); a `sched_trigger` run (MCP, ./ops/sched) is
                     a manual DAG run of its one task.
  task instance      The latest attempt of the task in that DAG run; `try_number`
                     counts its attempts, retries and clears included.
  queued             A run asked for and not yet run is an entry in state.json's
                     `queue` (`world.scheduler.enqueue`), shown `queued` until the
                     grader's `advance` drains it.

LEDGER_IS_NOT_THE_DAG (the archive's name for it, and still true). WHEN a job
runs comes from the world's schedule, not from the DAG's cron or its edges:
editing `schedule=` or a `>>` changes what this API shows, not what the ledger
records. Where the two disagree, the ledger is right.

NO CLOCK. `trigger` and `clear` call `WorldScheduler.enqueue` and nothing else:
`vnow` does not move, no job's work runs, no backfill exists. The grader's
`advance` executes the queue (world/world/scheduler.py `_drain`).

Shapes are Airflow 3.3.2's (apache-airflow-core 3.3.2,
`airflow/api_fastapi/core_api/openapi/v2-rest-api-generated.yaml`), which the
REST v2 contract keeps a superset of 3.0's and which airflowctl 0.1.5's
generated models require. Nothing here reads a wall clock, a random number or
`var/world/live.json`; every id is derived from the ledger, so two reads of one
world answer the same bytes.
"""
from __future__ import annotations

import ast
import hashlib
import json
import string
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import scheduler

DAGS_DIR = Path("workspace") / "dags"
LOGS_DIR = Path("build") / "sched" / "logs"
#: Airflow 3's default DAG bundle name; every DAG here lives in one folder.
BUNDLE = "dags-folder"
#: Airflow's own defaults for what a DAG file does not say.
DEFAULT_MAX_ACTIVE_RUNS = 16
DEFAULT_MAX_ACTIVE_TASKS = 16
DEFAULT_OWNER = "airflow"
#: The task classes a scheduler keeps no job record for (dag_check.py's rule).
NON_JOB_NAMES = frozenset({"EmptyOperator", "DummyOperator", "LatestOnlyOperator"})
#: Namespace for the ids Airflow would draw at random (task instance UUIDs).
ID_NAMESPACE = uuid.UUID("6f1d3c7e-8a52-4d0b-9c1e-a1fa1e0a1fa1")
_SUFFIX_ALPHABET = string.ascii_letters + string.digits


class AirflowError(Exception):
    """A request Airflow would refuse: HTTP `status` and its `detail`."""

    def __init__(self, status: int, detail):
        super().__init__(detail if isinstance(detail, str) else json.dumps(detail))
        self.status = status
        self.detail = detail


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------
def _dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else \
            value.replace(tzinfo=UTC)
    text = str(value).replace("Z", "+00:00")
    if len(text) == 10:                                  # a bare date
        text += "T00:00:00+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed.astimezone(UTC) if parsed.tzinfo else \
        parsed.replace(tzinfo=UTC)


def stamp(value) -> str | None:
    """Airflow's response spelling of an instant: `2026-03-02T02:30:00Z`."""
    moment = _dt(value)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def _run_id_time(value) -> str:
    """The spelling inside a run id: `datetime.isoformat()`, `+00:00`."""
    return _dt(value).isoformat()


def _seconds(start, end) -> float | None:
    a, b = _dt(start), _dt(end)
    return (b - a).total_seconds() if a and b else None


# --------------------------------------------------------------------------
# the DAG files
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TaskInfo:
    task_id: str
    job: str | None                    # the ledger job it runs, None if it runs none
    operator: str = "BaseOperator"
    upstream: tuple[str, ...] = ()
    downstream: tuple[str, ...] = ()
    retries: int = 0
    retry_delay_s: float | None = 300.0
    execution_timeout_s: float | None = None
    owner: str = DEFAULT_OWNER
    depends_on_past: bool = False
    bash_command: str | None = None


@dataclass(frozen=True)
class DagInfo:
    dag_id: str
    schedule: str | None
    tasks: tuple[TaskInfo, ...]
    file: str = ""                     # its file name under dags/
    description: str | None = None
    start_date: str | None = None
    catchup: bool = False
    max_active_runs: int = DEFAULT_MAX_ACTIVE_RUNS
    tags: tuple[str, ...] = ()
    owners: tuple[str, ...] = (DEFAULT_OWNER,)
    default_args: dict = field(default_factory=dict, hash=False, compare=False)
    doc_md: str | None = None

    def task(self, task_id: str) -> TaskInfo | None:
        return next((t for t in self.tasks if t.task_id == task_id), None)

    @property
    def jobs(self) -> tuple[str, ...]:
        return tuple(t.task_id for t in self.tasks if t.job)


_MISSING = object()


class _Evaluator:
    """Literal evaluation over a DAG file's own module constants.

    Enough of Python to read what the themes write - strings, numbers, f-strings
    over module constants, dicts and lists, `timedelta(...)`, `datetime(...)`,
    `pendulum.datetime(...)` - and nothing that runs code. What it cannot read
    is `_MISSING`, never a guess.
    """

    def __init__(self, names: dict):
        self.names = names

    def __call__(self, node):
        try:
            return self._eval(node)
        except Exception:                                # noqa: BLE001
            return _MISSING

    def _eval(self, node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self.names:
                return self.names[node.id]
            return {"True": True, "False": False, "None": None}.get(node.id, _MISSING)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                got = self._eval(value.value if isinstance(value, ast.FormattedValue)
                                 else value)
                if got is _MISSING:
                    return _MISSING
                parts.append(str(got))
            return "".join(parts)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            items = [self._eval(e) for e in node.elts]
            return _MISSING if _MISSING in items else list(items)
        if isinstance(node, ast.Dict):
            out = {}
            for key, value in zip(node.keys, node.values, strict=True):
                k, v = (self._eval(key) if key is not None else _MISSING), self._eval(value)
                if k is _MISSING:
                    return _MISSING
                out[k] = v
            return out
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mult)):
            a, b = self._eval(node.left), self._eval(node.right)
            if _MISSING in (a, b):
                return _MISSING
            return a + b if isinstance(node.op, ast.Add) else a * b
        if isinstance(node, ast.Call):
            name = _call_name(node)
            args = [self._eval(a) for a in node.args]
            kwargs = {k.arg: self._eval(k.value) for k in node.keywords if k.arg}
            if _MISSING in args or _MISSING in kwargs.values():
                return _MISSING
            if name == "timedelta":
                return timedelta(*args, **kwargs)
            if name == "datetime":
                kwargs.pop("tz", None)
                kwargs.pop("tzinfo", None)
                return datetime(*args, **kwargs, tzinfo=UTC)
            if name in ("days_ago",):
                return _MISSING
        return _MISSING


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _kwarg(node: ast.Call, name: str):
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _jsonable(value):
    """A default_args value as Airflow's API serialises it."""
    if isinstance(value, timedelta):
        return {"__type": "TimeDelta", "days": value.days, "seconds": value.seconds,
                "microseconds": value.microseconds}
    if isinstance(value, datetime):
        return stamp(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _secs(value) -> float | None:
    return value.total_seconds() if isinstance(value, timedelta) else None


def _operands(node) -> list:
    """The task nodes on one side of a `>>` / `<<` expression."""
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for element in node.elts:
            out += _operands(element)
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.RShift, ast.LShift)):
        # a >> b >> c: the expression's value is its right-hand operand (b, then c)
        return _operands(node.right if isinstance(node.op, ast.RShift) else node.left)
    return [node]


def _ref(node, tasks_by_var: dict) -> str | None:
    if isinstance(node, ast.Name):
        return tasks_by_var.get(node.id)
    return None


def _parse_file(path: Path, jobs: set) -> tuple[list[DagInfo], str | None]:
    """The DAGs one file defines, or ([], why) when it does not parse."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=path.name)
    except (OSError, SyntaxError, ValueError) as exc:
        return [], f"{type(exc).__name__}: {exc}"
    names: dict = {}
    evaluate = _Evaluator(names)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            value = evaluate(node.value)
            if value is not _MISSING:
                names[node.targets[0].id] = value

    dags: dict[str, dict] = {}            # dag variable (or a synthetic key) -> spec
    order: list[str] = []

    def add_dag(call: ast.Call, var: str | None) -> str:
        dag_id = evaluate(_kwarg(call, "dag_id")) if _kwarg(call, "dag_id") is not None \
            else (evaluate(call.args[0]) if call.args else _MISSING)
        key = var or f"__dag{len(order)}"
        dags[key] = {"call": call, "dag_id": dag_id, "tasks": {}}
        order.append(key)
        return key

    def is_dag(call) -> bool:
        return isinstance(call, ast.Call) and _call_name(call) == "DAG"

    # DAG(...) as `x = DAG(...)` or `with DAG(...) as x:`; tasks inside a
    # `with` block belong to it, a task with `dag=x` belongs to x.
    task_calls: list[tuple[str, ast.Call, str | None]] = []   # (var, call, dag key)

    def walk(body, current: str | None):
        for node in body:
            if isinstance(node, ast.With):
                inner = current
                for item in node.items:
                    if is_dag(item.context_expr):
                        var = item.optional_vars.id if isinstance(
                            item.optional_vars, ast.Name) else None
                        inner = add_dag(item.context_expr, var)
                walk(node.body, inner)
                continue
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) \
                    and isinstance(node.value, ast.Call):
                var, call = node.targets[0].id, node.value
                if is_dag(call):
                    add_dag(call, var)
                elif _kwarg(call, "task_id") is not None:
                    task_calls.append((var, call, current))
            for attr in ("body", "orelse", "finalbody"):
                if isinstance(getattr(node, attr, None), list) and \
                        not isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.With)):
                    walk(getattr(node, attr), current)

    walk(tree.body, None)
    if not order:
        return [], None
    tasks_by_var: dict[str, str] = {}
    owner_of: dict[str, str] = {}
    for var, call, current in task_calls:
        task_id = evaluate(_kwarg(call, "task_id"))
        if not isinstance(task_id, str):
            continue
        dag_kw = _kwarg(call, "dag")
        key = current
        if isinstance(dag_kw, ast.Name) and dag_kw.id in dags:
            key = dag_kw.id
        if key is None:
            key = order[0] if len(order) == 1 else None
        if key is None:
            continue
        tasks_by_var[var] = task_id
        owner_of[task_id] = key
        dags[key]["tasks"][task_id] = call

    edges: set[tuple[str, str]] = set()

    def edge_walk(node):
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.RShift, ast.LShift)):
            left, right = node.left, node.right
            if isinstance(node.op, ast.LShift):
                left, right = right, left
            for a in _operands(left):
                for b in _operands(right):
                    ta, tb = _ref(a, tasks_by_var), _ref(b, tasks_by_var)
                    if ta and tb:
                        edges.add((ta, tb))
            edge_walk(node.left)
            edge_walk(node.right)
            return
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in ("set_downstream", "set_upstream") and \
                    isinstance(node.func, ast.Attribute):
                me = _ref(node.func.value, tasks_by_var)
                for arg in node.args:
                    for other in _operands(arg):
                        them = _ref(other, tasks_by_var)
                        if me and them:
                            edges.add((me, them) if name == "set_downstream" else (them, me))
            elif name == "chain":
                refs = [[_ref(o, tasks_by_var) for o in _operands(arg)] for arg in node.args]
                for ups, downs in zip(refs, refs[1:], strict=False):
                    edges.update((a, b) for a in ups for b in downs if a and b)
        for child in ast.iter_child_nodes(node):
            edge_walk(child)

    edge_walk(tree)
    out = []
    for key in order:
        spec = dags[key]
        call, dag_id = spec["call"], spec["dag_id"]
        if not isinstance(dag_id, str):
            continue

        def kw(name, default=None, call=call):
            node = _kwarg(call, name)
            value = evaluate(node) if node is not None else _MISSING
            return default if value is _MISSING else value

        schedule = kw("schedule", _MISSING)
        if schedule is _MISSING:
            schedule = kw("schedule_interval", None)
        default_args = kw("default_args", {}) or {}
        if not isinstance(default_args, dict):
            default_args = {}
        tasks = []
        for task_id, tcall in spec["tasks"].items():
            def tkw(name, default=None, tcall=tcall, default_args=default_args):
                node = _kwarg(tcall, name)
                value = evaluate(node) if node is not None else _MISSING
                if value is _MISSING:
                    value = default_args.get(name, default)
                return value

            operator = _call_name(tcall)
            upstream = sorted(a for a, b in edges if b == task_id and owner_of.get(a) == key)
            downstream = sorted(b for a, b in edges if a == task_id and owner_of.get(b) == key)
            runs_job = task_id in jobs and not operator.endswith("Sensor") \
                and operator not in NON_JOB_NAMES
            retries = tkw("retries", 0)
            delay = tkw("retry_delay", timedelta(minutes=5))
            tasks.append(TaskInfo(
                task_id=task_id, job=task_id if runs_job else None, operator=operator,
                upstream=tuple(upstream), downstream=tuple(downstream),
                retries=int(retries) if isinstance(retries, (int, float)) else 0,
                retry_delay_s=_secs(delay),
                execution_timeout_s=_secs(tkw("execution_timeout")),
                owner=str(tkw("owner", DEFAULT_OWNER) or DEFAULT_OWNER),
                depends_on_past=bool(tkw("depends_on_past", False)),
                bash_command=tkw("bash_command") if isinstance(tkw("bash_command"), str)
                else None))
        tasks.sort(key=lambda t: t.task_id)
        start = kw("start_date")
        tags = kw("tags", []) or []
        owners = sorted({t.owner for t in tasks}) or [
            str(default_args.get("owner") or DEFAULT_OWNER)]
        max_runs = kw("max_active_runs", DEFAULT_MAX_ACTIVE_RUNS)
        out.append(DagInfo(
            dag_id=dag_id, schedule=schedule if isinstance(schedule, str) else None,
            tasks=tuple(tasks), file=path.name,
            description=kw("description") if isinstance(kw("description"), str) else None,
            start_date=stamp(start) if isinstance(start, datetime) else None,
            catchup=bool(kw("catchup", False)),
            max_active_runs=int(max_runs) if isinstance(max_runs, int) else
            DEFAULT_MAX_ACTIVE_RUNS,
            tags=tuple(sorted(str(t) for t in tags)) if isinstance(tags, list) else (),
            owners=tuple(owners), default_args=_jsonable(default_args),
            doc_md=kw("doc_md") if isinstance(kw("doc_md"), str) else None))
    return out, None


def _state(world: Path) -> dict:
    path = Path(world) / scheduler.STATE
    if not path.is_file():
        return {"vnow": None, "jobs": {}, "runs": []}
    return json.loads(path.read_text(encoding="utf-8"))


def dags(world: Path) -> list[DagInfo]:
    """Every DAG `workspace/dags/*.py` defines, sorted by dag_id.

    A file that does not parse contributes no DAG (`import_errors` names it);
    two files defining one dag_id keep the first, by file name, as Airflow's
    DagBag does."""
    jobs = set(_state(world).get("jobs") or {})
    found: dict[str, DagInfo] = {}
    for path in sorted((Path(world) / DAGS_DIR).glob("*.py")):
        for dag in _parse_file(path, jobs)[0]:
            found.setdefault(dag.dag_id, dag)
    return [found[k] for k in sorted(found)]


def import_errors(world: Path) -> dict[str, str]:
    """{file name: why} for every DAG file that does not parse."""
    out = {}
    for path in sorted((Path(world) / DAGS_DIR).glob("*.py")):
        why = _parse_file(path, set())[1]
        if why:
            out[path.name] = why
    return out


def dag(world: Path, dag_id: str) -> DagInfo:
    for found in dags(world):
        if found.dag_id == dag_id:
            return found
    raise AirflowError(404, f"Dag with id {dag_id} was not found")


def dag_source(world: Path, dag_id: str) -> str:
    found = dag(world, dag_id)
    return (Path(world) / DAGS_DIR / found.file).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# the schedule, as the DAG states it
# --------------------------------------------------------------------------
_PRESETS = {"@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *"}


def _daily(schedule: str | None) -> tuple[int | None, int] | None:
    """(hour or None for hourly, minute) for `M H * * *` / `M * * * *`, else None."""
    if not schedule:
        return None
    fields = _PRESETS.get(schedule.strip(), schedule).split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"] or not fields[0].isdigit():
        return None
    if fields[1] == "*":
        return None, int(fields[0])
    return (int(fields[1]), int(fields[0])) if fields[1].isdigit() else None


def timetable_description(schedule: str | None) -> str | None:
    daily = _daily(schedule)
    if daily is None:
        return None
    hour, minute = daily
    if hour is None:
        return f"At {minute} minutes past the hour"
    return f"At {hour:02d}:{minute:02d}"


def next_run_after(schedule: str | None, after) -> str | None:
    """The DAG's next cron instant strictly after `after`, or None if the
    schedule is not one this view reads (`M H * * *`, `M * * * *`, presets)."""
    daily, moment = _daily(schedule), _dt(after)
    if daily is None or moment is None:
        return None
    hour, minute = daily
    if hour is None:
        candidate = moment.replace(minute=minute, second=0, microsecond=0)
        while candidate <= moment:
            candidate += timedelta(hours=1)
    else:
        candidate = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
        while candidate <= moment:
            candidate += timedelta(days=1)
    return stamp(candidate)


# --------------------------------------------------------------------------
# runs from the ledger
# --------------------------------------------------------------------------
@dataclass
class Attempt:
    task_id: str
    record: dict                       # the ledger run, or the queue entry
    index: int | None                  # 1-based ledger position (run-%06d); None: queued

    @property
    def queued(self) -> bool:
        return self.index is None

    @property
    def ledger_run_id(self) -> str | None:
        return f"run-{self.index:06d}" if self.index else None


@dataclass
class DagRun:
    dag_id: str
    run_id: str
    run_type: str                      # scheduled | manual
    triggered_by: str                  # timetable | rest_api | cli
    logical_date: str | None
    run_after: str
    queued_at: str | None = None
    conf: dict | None = None
    note: str | None = None
    user: str | None = None
    data_interval: tuple[str | None, str | None] = (None, None)
    attempts: dict = field(default_factory=dict)        # task_id -> [Attempt]

    def add(self, attempt: Attempt) -> None:
        self.attempts.setdefault(attempt.task_id, []).append(attempt)


def _suffix(*parts) -> str:
    """Eight [A-Za-z0-9] characters derived from `parts`: Airflow draws these at
    random for a run with no logical date; here they are a function of the ledger."""
    digest = hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).digest()
    return "".join(_SUFFIX_ALPHABET[b % len(_SUFFIX_ALPHABET)] for b in digest[:8])


def _roots(found: DagInfo) -> set[str]:
    """The DAG's job-tasks with no job upstream of them (through non-job tasks)."""
    by_id = {t.task_id: t for t in found.tasks}

    def job_ancestors(task_id, seen):
        out = set()
        for up in by_id.get(task_id, TaskInfo(task_id, None)).upstream:
            if up in seen:
                continue
            seen.add(up)
            if by_id.get(up) and by_id[up].job:
                out.add(up)
            else:
                out |= job_ancestors(up, seen)
        return out

    return {t.task_id for t in found.tasks if t.job and not job_ancestors(t.task_id, set())}


def _origin_of(record: dict) -> dict:
    origin = record.get("origin")
    return origin if isinstance(origin, dict) else {}


def _tagged_run(found: DagInfo, origin: dict) -> DagRun:
    return DagRun(
        dag_id=found.dag_id, run_id=origin["dag_run_id"],
        run_type=origin.get("run_type") or "manual",
        triggered_by=origin.get("triggered_by") or "rest_api",
        logical_date=origin.get("logical_date"),
        run_after=origin.get("run_after") or origin.get("queued_at"),
        queued_at=origin.get("queued_at"), conf=origin.get("conf") or {},
        note=origin.get("note"), user=origin.get("user"),
        data_interval=(origin.get("data_interval_start"), origin.get("data_interval_end")))


def dag_run_objects(world: Path, found: DagInfo, state: dict | None = None) -> list[DagRun]:
    """The DAG's runs, oldest first (by run_after, then run_id)."""
    state = state if state is not None else _state(world)
    jobs = set(found.jobs)
    roots = _roots(found)
    runs: list[DagRun] = []
    tagged: list[tuple[dict, Attempt]] = []          # in ledger order, then queue order
    scheduled: list[tuple[dict, int]] = []
    for index, record in enumerate(state.get("runs") or [], 1):
        if record.get("job") not in jobs:
            continue
        origin = _origin_of(record)
        if origin.get("dag_id"):
            if origin["dag_id"] == found.dag_id and origin.get("dag_run_id"):
                tagged.append((origin, Attempt(record["job"], record, index)))
            # else: asked for through another DAG, and shown there
        elif record.get("trigger") == "manual":
            at = record["vts_start"]
            run = DagRun(found.dag_id, f"manual__{_run_id_time(at)}_{_suffix(index)}",
                         "manual", "cli", None, at, queued_at=at, conf={})
            run.add(Attempt(record["job"], record, index))
            runs.append(run)
        else:
            scheduled.append((record, index))
    for position, entry in enumerate(state.get(scheduler.QUEUE) or [], 1):
        if entry.get("job") not in jobs:
            continue
        origin = _origin_of(entry)
        if origin.get("dag_id"):
            if origin["dag_id"] == found.dag_id and origin.get("dag_run_id"):
                tagged.append((origin, Attempt(entry["job"], entry, None)))
        else:
            at = entry["queued_at"]
            run = DagRun(found.dag_id, f"manual__{_run_id_time(at)}_{_suffix('q', position)}",
                         "manual", "cli", None, at, queued_at=at, conf={})
            run.add(Attempt(entry["job"], entry, None))
            runs.append(run)

    current: DagRun | None = None
    for record, index in sorted(scheduled, key=lambda pair: (pair[0]["vts_start"], pair[1])):
        job = record["job"]
        retry = int(record.get("try_number") or 1) > 1
        if current is None or (job in roots and job in current.attempts and not retry):
            at = record["vts_start"]
            current = DagRun(found.dag_id, f"scheduled__{_run_id_time(at)}", "scheduled",
                             "timetable", at, at, queued_at=at, conf={},
                             data_interval=(at, at))
            runs.append(current)
        current.add(Attempt(job, record, index))
    # A run the API asked for joins the DAG run it names: its own (a trigger),
    # or an existing one (a clear re-runs a task inside the run it cleared).
    by_id = {run.run_id: run for run in runs}
    for origin, attempt in tagged:
        run = by_id.get(origin["dag_run_id"])
        if run is None:
            run = by_id[origin["dag_run_id"]] = _tagged_run(found, origin)
            runs.append(run)
        run.add(attempt)
    return sorted(runs, key=lambda r: (_dt(r.run_after) or datetime.min.replace(
        tzinfo=UTC), r.run_id))


def _latest(attempts: list[Attempt]) -> Attempt | None:
    return attempts[-1] if attempts else None


def _ti_state(attempt: Attempt | None) -> str | None:
    if attempt is None:
        return None
    if attempt.queued:
        return "queued"
    return "success" if attempt.record.get("status") == "success" else "failed"


def _run_state(found: DagInfo, run: DagRun, newest: bool) -> str:
    states = [_ti_state(_latest(run.attempts.get(t, []))) for t in found.jobs]
    present = [s for s in states if s is not None]
    if "queued" in present:
        return "queued"
    if "failed" in present:
        return "failed"
    if run.run_type == "scheduled" and newest and None in states:
        return "running"                # the occurrence's later jobs have not run yet
    return "success"


def _times(run: DagRun) -> tuple[str | None, str | None]:
    ran = [a for attempts in run.attempts.values() for a in attempts if not a.queued]
    if not ran:
        return None, None
    starts = sorted(a.record["vts_start"] for a in ran)
    ends = sorted(a.record["vts_end"] for a in ran)
    return starts[0], ends[-1]


def dag_run_shape(found: DagInfo, run: DagRun, newest: bool = False) -> dict:
    """One DAGRunResponse (Airflow 3.3.2)."""
    state = _run_state(found, run, newest)
    start, end = _times(run)
    if state in ("queued",):
        end = None
        if not any(not a.queued for attempts in run.attempts.values() for a in attempts):
            start = None
    if state == "running":
        end = None
    return {
        "dag_run_id": run.run_id, "dag_id": found.dag_id,
        "logical_date": stamp(run.logical_date), "queued_at": stamp(run.queued_at),
        "start_date": stamp(start), "end_date": stamp(end),
        "duration": _seconds(start, end) if end else None,
        "data_interval_start": stamp(run.data_interval[0]),
        "data_interval_end": stamp(run.data_interval[1]),
        "run_after": stamp(run.run_after), "last_scheduling_decision": None,
        "run_type": run.run_type, "state": state, "triggered_by": run.triggered_by,
        "triggering_user_name": run.user, "conf": run.conf if run.conf is not None else {},
        "note": run.note, "dag_versions": [], "bundle_version": None,
        "dag_display_name": found.dag_id, "partition_key": None, "partition_date": None,
    }


def dag_runs(world: Path, dag_id: str) -> list[dict]:
    """The DAG's runs as Airflow 3 DAGRun objects, oldest first."""
    found = dag(world, dag_id)
    runs = dag_run_objects(world, found)
    newest = _newest_scheduled(runs)
    return [dag_run_shape(found, r, r is newest) for r in runs]


def _newest_scheduled(runs: list[DagRun]) -> DagRun | None:
    scheduled = [r for r in runs if r.run_type == "scheduled"]
    return scheduled[-1] if scheduled else None


def find_run(world: Path, found: DagInfo, run_id: str,
             state: dict | None = None) -> tuple[DagRun, bool]:
    runs = dag_run_objects(world, found, state)
    newest = _newest_scheduled(runs)
    for run in runs:
        if run.run_id == run_id:
            return run, run is newest
    raise AirflowError(404, f"The DagRun with dag_id: `{found.dag_id}` and run_id: "
                            f"`{run_id}` was not found")


def _ti_id(dag_id: str, run_id: str, task_id: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, f"{dag_id}/{run_id}/{task_id}"))


def task_instance_shape(found: DagInfo, run: DagRun, task: TaskInfo) -> dict:
    """One TaskInstanceResponse (Airflow 3.3.2): the task's latest attempt."""
    attempts = run.attempts.get(task.task_id, [])
    latest = _latest(attempts)
    state = _ti_state(latest)
    record = latest.record if latest else {}
    ran = latest is not None and not latest.queued
    tries = max([len(attempts)] + [int(a.record.get("try_number") or 0) for a in attempts])
    note = None
    if task.job is None:
        note = (f"not recorded by the world's scheduler: no job named "
                f"{task.task_id!r} runs, so this task never has a state")
    elif latest is None:
        note = "this DAG run holds no run of this task"
    queued_when = record.get("queued_at") if latest else None
    return {
        "id": _ti_id(found.dag_id, run.run_id, task.task_id),
        "task_id": task.task_id, "dag_id": found.dag_id, "dag_run_id": run.run_id,
        "map_index": -1, "logical_date": stamp(run.logical_date),
        "run_after": stamp(run.run_after),
        "start_date": stamp(record.get("vts_start")) if ran else None,
        "end_date": stamp(record.get("vts_end")) if ran else None,
        "duration": _seconds(record.get("vts_start"), record.get("vts_end")) if ran else None,
        "state": state, "try_number": tries, "max_tries": max(task.retries, tries - 1, 0),
        "task_display_name": task.task_id, "dag_display_name": found.dag_id,
        "hostname": None, "unixname": None, "pool": "default_pool", "pool_slots": 1,
        "queue": "default", "priority_weight": 1 + len(_descendants(found, task.task_id)),
        "operator": task.operator, "operator_name": task.operator,
        "queued_when": stamp(queued_when), "scheduled_when": None, "pid": None,
        "executor": None, "executor_config": "{}", "note": note,
        "rendered_map_index": None, "rendered_fields": {}, "trigger": None,
        "triggerer_job": None, "dag_version": None,
    }


def _descendants(found: DagInfo, task_id: str) -> set[str]:
    out, todo = set(), [task_id]
    while todo:
        task = found.task(todo.pop())
        for down in (task.downstream if task else ()):
            if down not in out:
                out.add(down)
                todo.append(down)
    return out


def _ancestors(found: DagInfo, task_id: str) -> set[str]:
    out, todo = set(), [task_id]
    while todo:
        task = found.task(todo.pop())
        for up in (task.upstream if task else ()):
            if up not in out:
                out.add(up)
                todo.append(up)
    return out


def task_instances(world: Path, dag_id: str, run_id: str) -> list[dict]:
    """Every task of the DAG in one DAG run, as TaskInstance objects."""
    found = dag(world, dag_id)
    run, _ = find_run(world, found, run_id)
    return [task_instance_shape(found, run, t) for t in found.tasks]


# --------------------------------------------------------------------------
# logs
# --------------------------------------------------------------------------
def task_log_events(world: Path, dag_id: str, run_id: str, task_id: str,
                    try_number: int) -> list[dict]:
    """One attempt's log as Airflow 3 StructuredLogMessage objects.

    Built from the ledger record and, when the engine kept one, its raw output
    (`build/sched/logs/run-NNNNNN.log`). It states nothing the world does not
    hold: a seeded history row says the scheduler kept no engine output, a
    queued run says it has not run."""
    found = dag(world, dag_id)
    run, _ = find_run(world, found, run_id)
    task = found.task(task_id)
    if task is None:
        raise AirflowError(404, f"Task {task_id} not found in Dag {dag_id}")
    attempts = run.attempts.get(task_id, [])
    if try_number < 1 or try_number > len(attempts):
        raise AirflowError(404, "TaskInstance not found")
    attempt = attempts[try_number - 1]
    record = attempt.record
    state = _state(world)
    command = ((state.get("jobs") or {}).get(task_id) or {}).get("command")
    if attempt.queued:
        return [{"timestamp": stamp(record.get("queued_at")), "event":
                 f"Task {task_id} is queued and has not run: it runs when the world's "
                 f"scheduler next advances the clock", "level": "info",
                 "logger": "airflow.task"}]
    start, end = stamp(record.get("vts_start")), stamp(record.get("vts_end"))
    events = [{"timestamp": start, "event": f"Executing {task.operator} {task_id} "
                                            f"(try {try_number})", "level": "info",
               "logger": "airflow.task", "ledger_run": attempt.ledger_run_id,
               "command": command}]
    raw = Path(world) / LOGS_DIR / f"{attempt.ledger_run_id}.log"
    if raw.is_file():
        for line in raw.read_text(encoding="utf-8", errors="replace").splitlines():
            events.append({"timestamp": end, "event": line, "level": "info",
                           "logger": "task.stdout"})
    elif "executed" not in record:
        events.append({"timestamp": end, "event": "The scheduler kept no engine output "
                       "for this run: it is part of the history the episode began with",
                       "level": "info", "logger": "airflow.task"})
    if record.get("detail"):
        events.append({"timestamp": end, "event": str(record["detail"]), "level": "info",
                       "logger": "airflow.task"})
    if record.get("error"):
        events.append({"timestamp": end, "event": str(record["error"]), "level": "error",
                       "logger": "airflow.task"})
    status = record.get("status")
    events.append({"timestamp": end, "event": f"Task instance in {status} state"
                   if status in ("success", "failed") else f"Task finished: {status}",
                   "level": "error" if status != "success" else "info",
                   "logger": "airflow.task"})
    return events


def task_log(world: Path, dag_id: str, run_id: str, task_id: str, try_number: int) -> str:
    """The same log as plain text, one event per line."""
    return "\n".join(e["event"] for e in task_log_events(
        world, dag_id, run_id, task_id, try_number)) + "\n"


# --------------------------------------------------------------------------
# the two writes: both enqueue, neither runs anything
# --------------------------------------------------------------------------
def _topological(found: DagInfo, task_ids) -> list[str]:
    """`task_ids` in dependency order, ties by task_id - the order a queue runs."""
    wanted, done, out = set(task_ids), set(), []
    while wanted - done:
        ready = sorted(t for t in wanted - done
                       if not (set(found.task(t).upstream) & (wanted - done)))
        if not ready:                     # a cycle: Airflow refuses such a DAG too
            raise AirflowError(400, f"Dag {found.dag_id} has a cycle among "
                                    f"{sorted(wanted - done)}")
        out.append(ready[0])
        done.add(ready[0])
    return out


def _scheduler(world: Path):
    from .gateway_scheduler import WorldScheduler

    return WorldScheduler(Path(world))


def trigger(world: Path, dag_id: str, *, dag_run_id: str | None = None,
            logical_date: str | None = None, conf: dict | None = None,
            note: str | None = None, user: str | None = None,
            data_interval_start: str | None = None,
            data_interval_end: str | None = None) -> dict:
    """`POST /dags/{dag_id}/dagRuns`: queue every job-task, in dependency order.

    Nothing executes and the clock does not move (`WorldScheduler.enqueue`); the
    returned run is `queued` until the grader's `advance` drains it. The run id
    is Airflow's: `manual__<logical_date>` when one is given, otherwise
    `manual__<vnow>_<8 characters>` (Airflow's are random; these are derived
    from the ledger, so a second trigger at the same instant gets its own)."""
    found = dag(world, dag_id)
    state = _state(world)
    if not found.jobs:
        raise AirflowError(400, f"Dag {dag_id} has no task the world's scheduler runs: "
                                f"none of {[t.task_id for t in found.tasks]} is a job in "
                                f"var/sched/state.json")
    vnow = state["vnow"]
    existing = dag_run_objects(world, found, state)
    if logical_date is not None:
        logical = _dt(logical_date)
        if any(r.logical_date and _dt(r.logical_date) == logical for r in existing):
            raise AirflowError(409, f"A DAG Run already exists for DAG {dag_id} with "
                                    f"logical date {logical.isoformat()}")
    if dag_run_id is None:
        if logical_date is not None:
            dag_run_id = f"manual__{_run_id_time(logical_date)}"
        else:
            queued = len(state.get(scheduler.QUEUE) or [])
            dag_run_id = (f"manual__{_run_id_time(vnow)}_"
                          f"{_suffix(dag_id, vnow, len(state.get('runs') or []), queued)}")
    if any(r.run_id == dag_run_id for r in existing):
        raise AirflowError(409, f"DAG Run with run_id: `{dag_run_id}` already exists "
                                f"for DAG `{dag_id}`")
    origin = {"dag_id": dag_id, "dag_run_id": dag_run_id, "run_type": "manual",
              "triggered_by": "rest_api", "run_after": vnow, "queued_at": vnow,
              "logical_date": stamp(logical_date), "conf": conf or {}, "note": note,
              "user": user, "data_interval_start": stamp(data_interval_start),
              "data_interval_end": stamp(data_interval_end)}
    execution_date = (stamp(logical_date) or stamp(vnow))[:10]
    sched = _scheduler(world)
    for task_id in _topological(found, found.jobs):
        sched.enqueue(task_id, execution_date, None, origin)
    run, newest = find_run(world, found, dag_run_id)
    return dag_run_shape(found, run, newest)


def clear(world: Path, dag_id: str, *, dag_run_id: str, task_ids=None,
          only_failed: bool = True, only_running: bool = False,
          include_upstream: bool = False, include_downstream: bool = False,
          dry_run: bool = True, user: str | None = None,
          note: str | None = None) -> list[dict]:
    """`POST /dags/{dag_id}/clearTaskInstances` for ONE DAG run.

    Each selected task instance whose task runs a job gets a re-run queued in
    that DAG run (its next try), in dependency order; nothing executes until
    the grader's `advance`. `dry_run` (Airflow's default) changes nothing and
    returns what would be cleared. Returns the task instances as they stand
    after the call."""
    found = dag(world, dag_id)
    state = _state(world)
    run, _ = find_run(world, found, dag_run_id, state)
    unknown = sorted(set(task_ids or ()) - {t.task_id for t in found.tasks})
    if unknown:
        raise AirflowError(400, f"Tasks not found in Dag {dag_id}: {unknown}")
    wanted = set(task_ids or [t.task_id for t in found.tasks])
    if include_upstream:
        wanted |= {a for t in list(wanted) for a in _ancestors(found, t)}
    if include_downstream:
        wanted |= {d for t in list(wanted) for d in _descendants(found, t)}
    selected = []
    for task_id in sorted(wanted):
        task = found.task(task_id)
        state_now = _ti_state(_latest(run.attempts.get(task_id, [])))
        if only_failed and state_now not in ("failed", "upstream_failed"):
            continue
        if only_running and state_now != "running":
            continue
        if state_now == "queued":
            continue                     # already waiting for the clock
        if task.job is None:
            continue                     # nothing the scheduler could re-run
        selected.append(task_id)
    if not dry_run and selected:
        origin = {"dag_id": dag_id, "dag_run_id": run.run_id, "run_type": run.run_type,
                  "triggered_by": run.triggered_by, "run_after": run.run_after,
                  "queued_at": run.queued_at, "logical_date": run.logical_date,
                  "conf": run.conf or {}, "note": note if note is not None else run.note,
                  "user": run.user, "data_interval_start": run.data_interval[0],
                  "data_interval_end": run.data_interval[1], "cleared_by": user}
        if run.run_type == "scheduled":
            origin["triggered_by"] = "timetable"
        latest_record = {t: _latest(run.attempts.get(t, [])) for t in selected}
        sched = _scheduler(world)
        for task_id in _topological(found, selected):
            previous = latest_record[task_id]
            execution_date = (previous.record.get("execution_date") if previous else None) \
                or (stamp(run.logical_date or run.run_after) or "")[:10] or None
            sched.enqueue(task_id, execution_date, None, origin)
        run, _ = find_run(world, found, dag_run_id)
    return [task_instance_shape(found, run, found.task(t)) for t in selected]


# --------------------------------------------------------------------------
# DAG objects
# --------------------------------------------------------------------------
def file_token(found: DagInfo) -> str:
    return hashlib.sha256(f"{BUNDLE}/{found.file}".encode()).hexdigest()[:40]


def dag_shape(world: Path, found: DagInfo, fileloc: str) -> dict:
    """One DAGResponse (Airflow 3.3.2)."""
    vnow = _state(world).get("vnow")
    nxt = next_run_after(found.schedule, vnow)
    return {
        "dag_id": found.dag_id, "dag_display_name": found.dag_id, "is_paused": False,
        "is_stale": False, "last_parsed_time": None, "last_parse_duration": None,
        "last_expired": None, "bundle_name": BUNDLE, "bundle_version": None,
        "relative_fileloc": found.file, "fileloc": fileloc,
        "description": found.description, "timetable_summary": found.schedule,
        "timetable_description": timetable_description(found.schedule),
        "timetable_partitioned": False, "timetable_periodic": bool(found.schedule),
        "tags": [{"name": t, "dag_id": found.dag_id, "dag_display_name": found.dag_id}
                 for t in found.tags],
        "max_active_tasks": DEFAULT_MAX_ACTIVE_TASKS,
        "max_active_runs": found.max_active_runs,
        "max_consecutive_failed_dag_runs": 0, "has_task_concurrency_limits": False,
        "has_import_errors": False,
        "next_dagrun_logical_date": nxt, "next_dagrun_data_interval_start": nxt,
        "next_dagrun_data_interval_end": nxt, "next_dagrun_run_after": nxt,
        "allowed_run_types": None, "owners": list(found.owners),
        # Backfills are refused here (the clock is the world's), so no.
        "is_backfillable": False, "file_token": file_token(found),
    }


def dag_details_shape(world: Path, found: DagInfo, fileloc: str) -> dict:
    """One DAGDetailsResponse (Airflow 3.3.2)."""
    out = dag_shape(world, found, fileloc)
    runs = dag_run_objects(world, found)
    newest = _newest_scheduled(runs)
    active = sum(dag_run_shape(found, r, r is newest)["state"] in ("queued", "running")
                 for r in runs)
    out.update({
        "catchup": found.catchup, "dag_run_timeout": None, "asset_expression": None,
        "doc_md": found.doc_md, "start_date": found.start_date, "end_date": None,
        "is_paused_upon_creation": None, "params": {}, "render_template_as_native_obj": False,
        "template_search_path": None, "timezone": "UTC", "last_parsed": None,
        "default_args": found.default_args or None, "owner_links": None,
        "is_favorite": False, "active_runs_count": active,
        "concurrency": DEFAULT_MAX_ACTIVE_TASKS, "latest_dag_version": None,
    })
    return out


def _timedelta_shape(seconds: float | None) -> dict | None:
    if seconds is None:
        return None
    delta = timedelta(seconds=seconds)
    return {"__type": "TimeDelta", "days": delta.days, "seconds": delta.seconds,
            "microseconds": delta.microseconds}


#: (module_path, ui_color, template_fields) of the operator classes the themes use.
OPERATORS = {
    "BashOperator": ("airflow.providers.standard.operators.bash", "#f0ede4",
                     ["bash_command", "env", "cwd"]),
    "TimeDeltaSensor": ("airflow.providers.standard.sensors.time_delta", "#e6f1f2", []),
    "EmptyOperator": ("airflow.providers.standard.operators.empty", "#e8f7e4", []),
}


def task_shape(found: DagInfo, task: TaskInfo) -> dict:
    """One TaskResponse (Airflow 3.3.2)."""
    module, color, fields = OPERATORS.get(task.operator, (None, "#fff", []))
    return {
        "task_id": task.task_id, "task_display_name": task.task_id, "owner": task.owner,
        "start_date": found.start_date, "end_date": None, "trigger_rule": "all_success",
        "depends_on_past": task.depends_on_past, "wait_for_downstream": False,
        "retries": float(task.retries), "queue": "default", "pool": "default_pool",
        "pool_slots": 1.0, "execution_timeout": _timedelta_shape(task.execution_timeout_s),
        "retry_delay": _timedelta_shape(task.retry_delay_s),
        "retry_exponential_backoff": 0.0,
        "priority_weight": float(1 + len(_descendants(found, task.task_id))),
        "weight_rule": "downstream", "ui_color": color, "ui_fgcolor": "#000",
        "template_fields": fields, "downstream_task_ids": list(task.downstream),
        "doc_md": None, "operator_name": task.operator, "params": {},
        "class_ref": {"module_path": module, "class_name": task.operator} if module
        else None,
        "is_mapped": False, "extra_links": [],
    }
