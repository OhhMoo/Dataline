"""Postgres in the container topology: seeded only when plugged, audited at teardown.

    gateway/.venv/bin/python -m pytest tasks/harness/test_postgres_live.py -q

No docker: the `docker exec` calls `postgres_live` makes are answered by fakes. The
teardown test runs the REAL tailer (`alkera_gateway.native.tailers.postgres`) over a
canned server log into a real action log, through `NativeContainerEpisode.down()`.
The live half is `postgres_e2e_check.py`.
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import container_episode as ce  # noqa: E402
import postgres_live  # noqa: E402

CLOCK = "2026-03-02T00:00:00Z"
#: What the server writes with compose.native.yml's settings (`%m [%p] %u@%d`,
#: role-scoped log_statement=all + log_min_duration_statement=0): a psql simple
#: query, a multi-line one, psycopg's extended protocol with parameters, and the
#: server's own lines, which are not statements.
SERVER_LOG = """\
2026-10-07 08:00:00.001 UTC [41] @ LOG:  database system is ready to accept connections
2026-10-07 08:00:05.100 UTC [77] alkera@alkera LOG:  statement: SELECT count(*) FROM warehouse.items
2026-10-07 08:00:05.101 UTC [77] alkera@alkera LOG:  duration: 0.412 ms
2026-10-07 08:00:06.200 UTC [78] alkera@alkera LOG:  statement: SELECT sum(order_amount)
\t  FROM warehouse.fct_orders
\t WHERE order_status = 'completed'
2026-10-07 08:00:06.209 UTC [78] alkera@alkera LOG:  duration: 8.734 ms
2026-10-07 08:00:07.300 UTC [79] alkera@alkera LOG:  execute <unnamed>: SELECT count(*) FROM warehouse.fct_orders WHERE order_status = $1
2026-10-07 08:00:07.300 UTC [79] alkera@alkera DETAIL:  parameters: $1 = 'completed'
2026-10-07 08:00:07.302 UTC [79] alkera@alkera LOG:  duration: 1.020 ms
2026-10-07 08:05:00.000 UTC [42] @ LOG:  checkpoint starting: time
"""
SENT = ["SELECT count(*) FROM warehouse.items",
        "SELECT sum(order_amount)\n  FROM warehouse.fct_orders\n WHERE order_status = 'completed'",
        "SELECT count(*) FROM warehouse.fct_orders WHERE order_status = $1"]


def _world(base: Path) -> dict:
    root = base / "world"
    (root / "var" / "warehouse").mkdir(parents=True, exist_ok=True)
    (root / "var" / "sched").mkdir(parents=True, exist_ok=True)
    (root / "var" / "sched" / "state.json").write_text(
        json.dumps({"vnow": CLOCK, "jobs": {}}), encoding="utf-8")
    (root / "workspace").mkdir(exist_ok=True)
    return {"world_root": str(root), "workspace": str(root / "workspace"),
            "warehouse": str(root / "var" / "warehouse" / "warehouse.duckdb"),
            "scheduler": str(root / "var" / "sched" / "state.json"),
            "profiles_dir": str(root / "build")}


def _stack(tmp_path: Path, **kw) -> ce.NativeContainerEpisode:
    return ce.NativeContainerEpisode(project="t", world=_world(tmp_path),
                                     audit_dir=tmp_path / "audit",
                                     rollout_dir=tmp_path / "rollout", **kw)


def _warehouse(path: Path) -> Path:
    import duckdb

    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA warehouse")
    con.execute("CREATE TABLE warehouse.warehouse.fct_orders (order_id INTEGER, order_status "
                "VARCHAR, order_amount DECIMAL(12,2), order_date DATE, paid BOOLEAN, "
                "placed_at TIMESTAMP)")
    con.execute("INSERT INTO warehouse.warehouse.fct_orders VALUES "
                "(1, 'completed', 10.50, DATE '2026-02-01', true, TIMESTAMP '2026-02-01 09:00:00'),"
                "(2, '', 20.25, DATE '2026-02-02', NULL, NULL),"
                "(3, NULL, 5.00, DATE '2026-03-01', false, TIMESTAMP '2026-03-01 10:30:00')")
    con.execute("CREATE TABLE warehouse.warehouse.dim_date (d DATE)")
    con.execute("CREATE VIEW warehouse.warehouse.v_orders AS SELECT * FROM warehouse.warehouse.fct_orders")
    con.execute("CREATE SCHEMA raw")
    con.execute("CREATE TABLE raw.orders (id INTEGER)")
    con.close()
    return path


def _rows(audit: Path) -> list[dict]:
    path = audit / "actions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()] if path.exists() else []


# ------------------------------------------------------------ seeding is opt-in
def test_unprofiled_postgres_not_seeded(tmp_path):
    """The server an unprofiled box (every GT task) and BI001/BI002's da/laptop box
    get is compose's: `warehouse.items`, two rows, and nothing of the world. Seeding
    asks no container anything unless the profile plugs postgres."""
    seed_sql = (REPO / "gateway" / "docker" / "postgres.sql").read_text(encoding="utf-8")
    tables = re.findall(r"CREATE TABLE ([\w.]+)", seed_sql)
    assert tables == ["warehouse.items"]
    values = re.search(r"INSERT INTO warehouse\.items VALUES (.*);", seed_sql).group(1)
    assert len(re.findall(r"\(\d+, [\d.]+\)", values)) == 2

    def refuse(*args, **kwargs):
        raise AssertionError(f"an unplugged box ran {args[0] if args else kwargs}")

    for name, kw in {"unprofiled": {},
                     "da/laptop (BI001/BI002)": {"profile": "da/laptop",
                                                 "plugs": {"bi": ["powerbi"]}},
                     "de/laptop": {"profile": "de/laptop"},
                     "da/snowflake": {"profile": "da/snowflake"}}.items():
        stack = _stack(tmp_path / name.replace("/", "_").replace(" ", "_"), **kw)
        stack._compose = refuse
        assert postgres_live.seed(stack, runner=refuse) is None, name
    assert postgres_live.plugged(None) is False


def test_prepare_records_a_seed_only_when_there_is_one(tmp_path, monkeypatch):
    """`prepare()` keeps today's record for a box that seeds nothing."""
    calls = []
    monkeypatch.setattr(postgres_live, "seed", lambda stack: calls.append(stack) or None)
    stack = _stack(tmp_path)
    monkeypatch.setattr(stack, "exec_", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, "", ""))
    monkeypatch.setattr(stack, "_copy_home", lambda: [])
    monkeypatch.setattr(stack, "_stage_profiles", lambda: [])
    monkeypatch.setattr(ce, "_native_probe",
                        lambda: type("P", (), {"CONTAINER_PYTHON": "python"}))
    record = stack.prepare()
    assert calls == [stack] and "postgres_seed" not in record
    monkeypatch.setattr(postgres_live, "seed", lambda stack: {"tables": {"t": 1}})
    assert stack.prepare()["postgres_seed"] == {"tables": {"t": 1}}


@pytest.mark.parametrize("profile,plugs", [("de/postgres", {}),
                                           ("da/laptop", {"warehouse": ["postgres"]})])
def test_a_profile_that_plugs_postgres_is_seeded_from_the_world(tmp_path, profile, plugs):
    stack = _stack(tmp_path, profile=profile, plugs=plugs)
    _warehouse(Path(stack.world["warehouse"]))
    stack._compose = lambda *a, **k: subprocess.CompletedProcess(a, 0, "pgcid\n", "")
    seen = []

    def runner(argv, **kwargs):
        seen.append((argv, kwargs.get("input")))
        if "-c" in argv:
            return subprocess.CompletedProcess(argv, 0, "dim_date|0\nfct_orders|3\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    record = postgres_live.seed(stack, runner=runner)
    assert record["tables"] == {"dim_date": 0, "fct_orders": 3}
    assert record["views_skipped"] == ["v_orders"]
    (copy, script), (count, _) = seen
    assert copy[:4] == ["docker", "exec", "-i", "pgcid"]
    assert copy[4:] == ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres",
                        "-d", "alkera", "-1"]
    assert script.startswith("SET ROLE episode;\n")
    assert 'CREATE TABLE "warehouse"."fct_orders" ("order_id" integer, "order_status" ' \
           'varchar, "order_amount" numeric(12,2), "order_date" date, "paid" boolean, ' \
           '"placed_at" timestamp);' in script
    # DuckDB's CSV: an empty string is quoted, a NULL is an empty field.
    data = script.split('FROM STDIN WITH (FORMAT csv);\n')[2].split("\n\\.\n")[0]
    assert data.splitlines() == ["1,completed,10.50,2026-02-01,true,2026-02-01 09:00:00",
                                 '2,"",20.25,2026-02-02,,',
                                 "3,,5.00,2026-03-01,false,2026-03-01 10:30:00"]
    assert 'GRANT SELECT, INSERT, UPDATE, DELETE ON "warehouse"."fct_orders" TO alkera;' \
        in script
    assert "raw" not in script and "v_orders" not in script
    assert count[-2:] == ["-c", postgres_live.count_query(
        postgres_live.read_tables(Path(stack.world["warehouse"]))[0])]


def test_a_short_seed_is_refused(tmp_path):
    stack = _stack(tmp_path, profile="de/postgres")
    _warehouse(Path(stack.world["warehouse"]))
    stack._compose = lambda *a, **k: subprocess.CompletedProcess(a, 0, "pgcid\n", "")
    runner = lambda argv, **kw: subprocess.CompletedProcess(  # noqa: E731
        argv, 0, "dim_date|0\nfct_orders|2\n" if "-c" in argv else "", "")
    with pytest.raises(postgres_live.SeedError, match="fct_orders"):
        postgres_live.seed(stack, runner=runner)


def test_unmapped_column_type_refused_by_name():
    assert postgres_live.pg_type("DECIMAL(18,3)") == "numeric(18,3)"
    assert postgres_live.pg_type("TIMESTAMP WITH TIME ZONE") == "timestamptz"
    with pytest.raises(postgres_live.SeedError, match=r"warehouse\.t\.c.*STRUCT"):
        postgres_live.pg_type("STRUCT(a INTEGER)", column="warehouse.t.c")


# ------------------------------------------------------------ audited at teardown
def _local_tailer(log_text: str, audit: Path, scheduler: Path, calls: list):
    """A `docker exec` into native-services, answered on the host: the same tailer
    module, the same environment keys, /audit and /world mapped to the test's."""
    def runner(argv, **kwargs):
        calls.append(("fold", argv))
        assert argv[:2] == ["docker", "exec"] and argv[-3:-1] == ["sh", "-c"]
        env = dict(part.split("=", 1) for part in argv[3:-4:2])
        assert env["ALKERA_NATIVE_AUDIT"] == "/audit/actions.jsonl"
        assert env["ALKERA_SCHEDULER"] == "/world/var/sched/state.json"
        local = {**os.environ,
                 "ALKERA_NATIVE_AUDIT": str(audit / "actions.jsonl"),
                 "ALKERA_SCHEDULER": str(scheduler),
                 "ALKERA_VIRTUAL_TIME": env.get("ALKERA_VIRTUAL_TIME", "")}
        local.pop("ALKERA_VTS", None)
        return subprocess.run([sys.executable, "-m", postgres_live.TAILER],
                              input=log_text, capture_output=True, text=True,
                              env=local, timeout=120)
    return runner


def _teardown_stack(tmp_path, monkeypatch, runner, calls):
    stack = _stack(tmp_path)
    stack._up, stack.container_id, stack.services_container = True, "epi", "svc"
    monkeypatch.setattr(postgres_live, "fold_statement_log",
                        functools.partial(postgres_live.fold_statement_log, runner=runner))
    monkeypatch.setattr(ce, "_run", lambda argv, **kw: calls.append(("run", argv))
                        or subprocess.CompletedProcess(argv, 0, "1000:1000\n", ""))
    monkeypatch.setattr(stack, "_compose", lambda *a, **kw: calls.append(("compose", a))
                        or subprocess.CompletedProcess(a, 0, "", ""))
    return stack


def test_postgres_rows_land_after_teardown_with_contiguous_seq(tmp_path, monkeypatch):
    """Rows other writers made during the episode stay where they are; the
    statement log's rows follow them at teardown with the next `seq`s, one
    `psql.statement` per statement in server order, at the world's clock - and
    they are in actions.jsonl before ownership is handed back and the stack (and
    the log's volume) goes down."""
    from alkera_gateway.audit import validate_trace
    from alkera_gateway.native.audit import ROW_KEYS, record

    audit = tmp_path / "audit"
    audit.mkdir()
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit / "actions.jsonl"))
    monkeypatch.setenv("ALKERA_VTS", CLOCK)
    for n in range(3):   # a shim's rows, written while the episode ran
        record(tool="shim.dbt", args={"argv": ["dbt", str(n)]}, op_class="exec",
               targets=[], actor="shim")
    calls: list = []
    stack = _teardown_stack(tmp_path, monkeypatch, _local_tailer(
        SERVER_LOG, audit, Path(stack_world := tmp_path / "world" / "var" / "sched"
                                / "state.json"), calls), calls)
    assert stack_world.exists()
    stack.down()

    rows = _rows(audit)
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1)) == [1, 2, 3, 4, 5, 6]
    tail = rows[3:]
    assert [r["tool"] for r in tail] == ["psql.statement"] * 3
    assert [r["args"]["sql"] for r in tail] == SENT
    assert tail[2]["args"]["parameters"] == "$1 = 'completed'"
    assert all(r["op_class"] == "read" and r["vts"] == CLOCK and set(r) == set(ROW_KEYS)
               and "actor:postgres-tailer" in r["flags"] for r in tail)
    assert tail[1]["read_targets"] == ["warehouse.fct_orders"]
    validate_trace(audit / "actions.jsonl")
    order = [kind if kind != "run" else argv[1] for kind, argv in calls]
    assert order[0] == "fold", order
    assert order.index("fold") < order.index("exec") < order.index("compose"), order
    assert stack.postgres_statements["rows"] == 3 and "error" not in stack.postgres_statements
    assert not stack._up


def test_an_empty_statement_log_writes_no_row(tmp_path, monkeypatch):
    """A box whose agent never ran SQL against Postgres - every unprofiled run that
    did not - ends with the action log it had: zero rows appended."""
    audit = tmp_path / "audit"
    audit.mkdir()
    calls: list = []
    sched = tmp_path / "world" / "var" / "sched" / "state.json"
    stack = _teardown_stack(tmp_path, monkeypatch, _local_tailer(
        SERVER_LOG.splitlines(True)[0] + SERVER_LOG.splitlines(True)[-1], audit, sched,
        calls), calls)
    stack.down()
    assert _rows(audit) == [] and stack.postgres_statements["rows"] == 0


def test_a_failed_fold_is_loud_after_the_stack_is_down(tmp_path, monkeypatch):
    calls: list = []

    def broken(argv, **kwargs):
        calls.append(("fold", argv))
        return subprocess.CompletedProcess(argv, postgres_live.NO_LOG, "", "")

    stack = _teardown_stack(tmp_path, monkeypatch, broken, calls)
    with pytest.raises(ce.ContainerError, match="postgres statement log not folded"):
        stack.down()
    assert ("compose", ("down", "-v", "-t", "2", "--remove-orphans")) in calls
    assert not stack._up


def test_a_stack_that_never_came_up_tears_down_quietly(tmp_path, monkeypatch):
    """`up()` failed before naming its containers: no agent ran, nothing to fold,
    and the teardown must not replace the original failure with its own."""
    calls: list = []
    stack = _teardown_stack(tmp_path, monkeypatch, lambda *a, **k: calls.append("fold"),
                            calls)
    stack.services_container = ""
    stack.down()
    assert "fold" not in calls and "skipped" in stack.postgres_statements
    assert not stack._up


def test_tail_runs_in_native_services_over_the_read_only_log():
    argv = postgres_live.tail_argv("svc", CLOCK)
    assert argv[:2] == ["docker", "exec"] and argv[-4] == "svc"
    assert "ALKERA_VIRTUAL_TIME=" + CLOCK in argv
    assert argv[-1] == ("test -f /pgdata/log/postgresql.log || exit 3; exec "
                        "/usr/local/bin/python -m alkera_gateway.native.tailers.postgres "
                        "< /pgdata/log/postgresql.log")
    native = (REPO / "gateway" / "docker" / "compose.native.yml").read_text(encoding="utf-8")
    assert "- pgdata:/pgdata:ro" in native and "log_filename=postgresql.log" in native
    assert "ALTER ROLE alkera SET log_statement = 'all';" in native
