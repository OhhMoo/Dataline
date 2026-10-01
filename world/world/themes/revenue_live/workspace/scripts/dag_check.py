"""Read dags/ statically and check it against the runs the scheduler recorded.

Airflow is not installed on this box, and importing a DAG file to inspect it
needs the whole airflow package, so this reads the source with `ast` instead:
task ids, the `>>` edges between them, and each DAG's schedule. That is enough
to answer the two questions worth asking of a DAG you did not write:

    what runs, and in what order?
    does that still match what the platform has actually been running?

The second question is answered against var/sched/state.json - the run history
the scheduler writes - so a task that was renamed in the DAG and not in the
deployment shows up here instead of at 01:00 on a Sunday.

Exit status: 0 clean, 1 drift, 2 could not read the DAGs.

    python scripts/dag_check.py [--dags dags] [--state ../var/sched/state.json]
"""
import argparse
import ast
import json
import sys
from pathlib import Path

# Task types that are plumbing rather than a job: they wait, or they mark a
# point in the graph, and the scheduler has no run record for them.
NON_JOB_SUFFIXES = ("Sensor",)
NON_JOB_NAMES = {"EmptyOperator", "DummyOperator", "LatestOnlyOperator"}


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _kwarg(node: ast.Call, name: str):
    for kw in node.keywords:
        if kw.arg == name:
            try:
                return ast.literal_eval(kw.value)
            except (ValueError, TypeError, SyntaxError):
                return None
    return None


def _operands(node) -> list[str]:
    """Variable names on one side of a >> / << expression."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for e in node.elts:
            out += _operands(e)
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.RShift, ast.LShift)):
        # a >> b >> c: the middle name is both a target and a source
        return _operands(node.right if isinstance(node.op, ast.RShift)
                         else node.left)
    return []


def _edges(tree: ast.AST, var_to_task: dict) -> set:
    edges = set()

    def walk(node):
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.RShift, ast.LShift)):
            left, right = node.left, node.right
            if isinstance(node.op, ast.LShift):
                left, right = right, left
            for a in _operands(left):
                for b in _operands(right):
                    if a in var_to_task and b in var_to_task:
                        edges.add((var_to_task[a], var_to_task[b]))
            walk(left)
            walk(right)
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    return edges


def read_dags(dag_dir: Path) -> list:
    """[{file, dag_id, schedule, tasks: {id: operator}, edges: {(a, b)}}]"""
    dags = []
    for path in sorted(Path(dag_dir).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        dag_id, schedule, tasks, var_to_task = None, None, {}, {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name == "DAG" or name == "dag":
                dag_id = dag_id or _kwarg(node, "dag_id")
                if schedule is None:
                    schedule = _kwarg(node, "schedule")
                if schedule is None:
                    schedule = _kwarg(node, "schedule_interval")
            task_id = _kwarg(node, "task_id")
            if task_id:
                tasks[task_id] = name
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                task_id = _kwarg(node.value, "task_id")
                if task_id:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            var_to_task[target.id] = task_id
        dags.append({"file": path.name, "dag_id": dag_id or path.stem,
                     "schedule": schedule, "tasks": tasks,
                     "edges": _edges(tree, var_to_task)})
    return dags


def is_job(operator: str) -> bool:
    return not (operator in NON_JOB_NAMES
                or operator.endswith(NON_JOB_SUFFIXES))


def recorded_jobs(state_path: Path) -> set | None:
    if not Path(state_path).is_file():
        return None
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    return {r["job"] for r in state.get("runs", []) if "job" in r}


def main(argv=None) -> int:
    here = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(prog="dag_check")
    p.add_argument("--dags", type=Path, default=here / "dags")
    p.add_argument("--state", type=Path,
                   default=here.parent / "var" / "sched" / "state.json",
                   help="scheduler run history (default: the world's)")
    a = p.parse_args(argv)

    if not a.dags.is_dir():
        print(f"no dag directory at {a.dags}", file=sys.stderr)
        return 2
    dags = read_dags(a.dags)
    if not dags:
        print(f"no dag files under {a.dags}", file=sys.stderr)
        return 2

    declared = set()
    for d in dags:
        print(f"{d['file']}: dag_id={d['dag_id']} schedule={d['schedule']!r}")
        for task_id, operator in sorted(d["tasks"].items()):
            kind = "job" if is_job(operator) else "plumbing"
            print(f"    {task_id:<24} {operator:<22} {kind}")
            if is_job(operator):
                declared.add(task_id)
        for a_, b_ in sorted(d["edges"]):
            print(f"    {a_} -> {b_}")

    ran = recorded_jobs(a.state)
    if ran is None:
        print(f"\nno run history at {a.state}; skipped the drift check")
        return 0

    missing_in_dags = sorted(ran - declared)
    never_ran = sorted(declared - ran)
    print(f"\nrun history: {len(ran)} distinct job(s) in {a.state}")
    for job in missing_in_dags:
        print(f"  DRIFT: {job!r} has run but no dag declares it")
    for task in never_ran:
        print(f"  DRIFT: dag task {task!r} has never run")
    if missing_in_dags or never_ran:
        return 1
    print("  clean: every recorded job is declared, every declared task has run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
