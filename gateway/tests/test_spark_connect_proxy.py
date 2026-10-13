"""Databricks tier 2's audited Spark Connect proxy (native/services/spark_connect.py).

    gateway/.venv/bin/python -m pytest gateway/tests/test_spark_connect_proxy.py -q

A FAKE UPSTREAM stands in for the Spark engine: a grpcio server that answers
every SparkConnectService method with canned bytes and records, for each call it
receives, how many rows the rollout's action log held AT THAT MOMENT - so "the
row is written before the plan is forwarded" is checked, not inferred. The
client is grpcio with the Spark Connect messages built from the descriptors of
the client the image pins (pyspark-client 4.2.0): the proxy reads them from
the installed client in the image, and from testdata/spark_connect_4.2.0.binpb
here, the same files serialized (regenerate with `python -m
alkera_gateway.native.services.spark_connect --write-descriptors <out>` in an
environment that has gateway/requirements-spark.lock). No docker, no JVM: the
real engine behind the real proxy is tasks/harness/spark_e2e_check.py's.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent import futures
from pathlib import Path

import grpc
import pytest

from alkera_gateway.native.audit import ROW_KEYS
from alkera_gateway.native.serve import DestinationLog
from alkera_gateway.native.services import spark_connect as sc

TESTDATA = Path(__file__).resolve().parent / "testdata" / "spark_connect_4.2.0.binpb"
SERVICE = "/spark.connect.SparkConnectService/"
TOOL = "databricks.spark"


@pytest.fixture(scope="module")
def protos():
    return sc.load_protos(TESTDATA)


def _cls(protos, name):
    return protos.cls(f"spark.connect.{name}")


# --------------------------------------------------------------------------- upstream
class FakeUpstream:
    """Every method answers; `calls` is (method, request bytes, metadata, rows on arrival)."""

    def __init__(self, audit: Path, protos):
        self.audit, self.protos = audit, protos
        self.calls: list[tuple[str, bytes, dict, int]] = []
        self.fail: dict[str, tuple] = {}
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        self.server.add_generic_rpc_handlers((self,))
        self.port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()

    def _rows(self) -> int:
        return len(self.audit.read_text().splitlines()) if self.audit.exists() else 0

    def service(self, details):
        method = details.method
        upstream = self

        def note(request, context):
            upstream.calls.append((method.rsplit("/", 1)[-1], request,
                                   dict(context.invocation_metadata()), upstream._rows()))
            failing = upstream.fail.get(method.rsplit("/", 1)[-1])
            if failing:
                code, message, trailing = failing
                context.set_trailing_metadata(trailing)
                context.abort(code, message)

        def unary(request, context):
            note(request, context)
            return b""

        def stream(request, context):
            note(request, context)
            response = _cls(upstream.protos, "ExecutePlanResponse")(
                session_id="s", operation_id="op-1", response_id="r-1")
            yield response.SerializeToString()
            yield _cls(upstream.protos, "ExecutePlanResponse")(
                session_id="s", operation_id="op-1", response_id="r-2").SerializeToString()

        def client_stream(requests, context):
            note(b"".join(requests), context)
            return b""

        if method.endswith(("/ExecutePlan", "/ReattachExecute")):
            return grpc.unary_stream_rpc_method_handler(stream)
        if method.endswith("/AddArtifacts"):
            return grpc.stream_unary_rpc_method_handler(client_stream)
        return grpc.unary_unary_rpc_method_handler(unary)

    def stop(self):
        self.server.stop(0)


@pytest.fixture
def stack(tmp_path, monkeypatch, protos):
    audit = tmp_path / "audit" / "actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit))
    monkeypatch.setenv("ALKERA_VIRTUAL_TIME", "2026-03-02T00:00:00Z")
    upstream = FakeUpstream(audit, protos)
    destinations = DestinationLog(tmp_path / "audit" / "destinations.jsonl")
    box: dict = {}
    started = threading.Event()

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        box["loop"] = loop
        proxy = sc.Proxy(("127.0.0.1", upstream.port), protos=protos, tool=TOOL,
                         destinations=destinations)
        box["proxy"] = proxy
        box["port"] = loop.run_until_complete(proxy.start("127.0.0.1", 0))
        destinations.declare("databricks.internal", box["port"])
        started.set()
        loop.run_forever()
        loop.run_until_complete(proxy.stop())
        loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(20)
    channel = grpc.insecure_channel(f"127.0.0.1:{box['port']}")
    yield {"channel": channel, "upstream": upstream, "audit": audit,
           "destinations": destinations, "port": box["port"]}
    channel.close()
    box["loop"].call_soon_threadsafe(box["loop"].stop)
    thread.join(20)
    upstream.stop()


def _rows(audit: Path) -> list[dict]:
    return [json.loads(line) for line in audit.read_text().splitlines()] \
        if audit.exists() else []


def _execute(stack, protos, plan, timeout=20):
    request = _cls(protos, "ExecutePlanRequest")(session_id="s-1", plan=plan)
    call = stack["channel"].unary_stream(SERVICE + "ExecutePlan")
    return list(call(request.SerializeToString(), timeout=timeout))


def _sql_plan(protos, text):
    Relation, SQL = _cls(protos, "Relation"), _cls(protos, "SQL")
    Command, SqlCommand = _cls(protos, "Command"), _cls(protos, "SqlCommand")
    return _cls(protos, "Plan")(command=Command(
        sql_command=SqlCommand(input=Relation(sql=SQL(query=text)))))


def _read(protos, table):
    Relation, Read = _cls(protos, "Relation"), _cls(protos, "Read")
    return Relation(read=Read(named_table=Read.NamedTable(unparsed_identifier=table)))


# --------------------------------------------------------------------------- the rows
def test_execute_plan_row_written_before_forwarding(stack, protos):
    answers = _execute(stack, protos, _sql_plan(protos, "select count(*) from "
                                                "main.warehouse.fct_orders"))
    assert len(answers) == 2                     # the upstream's whole stream came back
    answers = _execute(stack, protos, _cls(protos, "Plan")(
        root=_read(protos, "main.warehouse.fct_orders")))
    calls = [c for c in stack["upstream"].calls if c[0] == "ExecutePlan"]
    assert len(calls) == 2
    rows = _rows(stack["audit"])
    # the upstream saw plan k only once row k was in the log
    for k, (_, _, _, rows_on_arrival) in enumerate(calls, start=1):
        assert rows_on_arrival == k
        assert rows[k - 1]["seq"] == k
    assert all(set(r) == set(ROW_KEYS) and r["tool"] == TOOL for r in rows)
    assert all("actor:spark_connect" in r["flags"] for r in rows)
    assert rows[0]["result"] == {"forwarded": True}
    assert rows[0]["vts"] == "2026-03-02T00:00:00Z"
    # forwarded byte for byte
    assert calls[0][1] == _cls(protos, "ExecutePlanRequest")(
        session_id="s-1", plan=_sql_plan(protos, "select count(*) from "
                                         "main.warehouse.fct_orders")).SerializeToString()


def test_sql_command_text_is_in_the_row(stack, protos):
    _execute(stack, protos, _sql_plan(protos, "SELECT order_status FROM "
                                      "main.warehouse.fct_orders"))
    row = _rows(stack["audit"])[-1]
    assert row["args"]["rpc"] == "ExecutePlan" and row["args"]["session_id"] == "s-1"
    assert row["args"]["sql"] == "SELECT order_status FROM main.warehouse.fct_orders"
    assert row["args"]["plan"] == "command:sql_command(sql)"
    assert row["op_class"] == "read"
    assert row["targets"] == ["main.warehouse.fct_orders"]


def test_sql_ddl_and_dml_take_the_classifiers_class(stack, protos):
    for text in ("DROP TABLE main.scratch.t", "INSERT INTO main.scratch.t VALUES (1)",
                 "CREATE TABLE main.scratch.t (a INT)"):
        _execute(stack, protos, _sql_plan(protos, text))
    assert [r["op_class"] for r in _rows(stack["audit"])] == ["destructive", "write", "ddl"]


def test_write_operation_is_write_class(stack, protos):
    WriteOperation = _cls(protos, "WriteOperation")
    plan = _cls(protos, "Plan")(command=_cls(protos, "Command")(
        write_operation=WriteOperation(
            input=_read(protos, "main.warehouse.fct_orders"),
            table=WriteOperation.SaveTable(table_name="main.scratch.orders_copy",
                                           save_method=WriteOperation.SaveTable
                                           .TABLE_SAVE_METHOD_SAVE_AS_TABLE),
            mode=WriteOperation.SAVE_MODE_OVERWRITE)))
    _execute(stack, protos, plan)
    row = _rows(stack["audit"])[-1]
    assert row["op_class"] == "write"
    assert row["targets"] == ["main.scratch.orders_copy"]
    assert row["read_targets"] == ["main.warehouse.fct_orders"]
    assert row["args"]["plan"] == "command:write_operation(read)"
    assert "sql" not in row["args"]
    # V2 and a temp view are writes too
    v2 = _cls(protos, "WriteOperationV2")(input=_read(protos, "t"), table_name="main.s.v2")
    _execute(stack, protos, _cls(protos, "Plan")(command=_cls(protos, "Command")(
        write_operation_v2=v2)))
    view = _cls(protos, "CreateDataFrameViewCommand")(input=_read(protos, "t"), name="v")
    _execute(stack, protos, _cls(protos, "Plan")(command=_cls(protos, "Command")(
        create_dataframe_view=view)))
    assert [r["op_class"] for r in _rows(stack["audit"])[-2:]] == ["write", "write"]
    assert _rows(stack["audit"])[-1]["targets"] == ["v"]


def test_relation_tree_summary_names_operators_and_tables(stack, protos):
    Relation, Filter = _cls(protos, "Relation"), _cls(protos, "Filter")
    Expression = _cls(protos, "Expression")
    tree = Relation(filter=Filter(
        input=_read(protos, "main.warehouse.fct_orders"),
        condition=Expression(expression_string=Expression.ExpressionString(
            expression="order_amount > 50"))))
    _execute(stack, protos, _cls(protos, "Plan")(root=tree))
    row = _rows(stack["audit"])[-1]
    assert row["args"]["plan"] == "root:filter(read)"
    assert row["op_class"] == "read"
    assert row["targets"] == ["main.warehouse.fct_orders"]
    assert row["args"]["expressions"] == ["order_amount > 50"]


def test_analyze_plan_is_one_read_row_before_forwarding(stack, protos):
    Analyze = _cls(protos, "AnalyzePlanRequest")
    request = Analyze(session_id="s-1", schema=Analyze.Schema(
        plan=_cls(protos, "Plan")(root=_read(protos, "main.warehouse.dim_date"))))
    stack["channel"].unary_unary(SERVICE + "AnalyzePlan")(request.SerializeToString(),
                                                          timeout=20)
    row = _rows(stack["audit"])[-1]
    assert row["args"]["rpc"] == "AnalyzePlan" and row["args"]["analyze"] == "schema"
    assert row["op_class"] == "read" and row["targets"] == ["main.warehouse.dim_date"]
    assert stack["upstream"].calls[-1][0] == "AnalyzePlan"
    assert stack["upstream"].calls[-1][3] == 1


def test_session_rpcs_are_forwarded_with_a_row_each(stack, protos):
    Config = _cls(protos, "ConfigRequest")
    get = Config(session_id="s-1", operation=Config.Operation(
        get=Config.Get(keys=["spark.sql.session.timeZone"])))
    stack["channel"].unary_unary(SERVICE + "Config")(get.SerializeToString(), timeout=20)
    release = _cls(protos, "ReleaseSessionRequest")(session_id="s-1")
    stack["channel"].unary_unary(SERVICE + "ReleaseSession")(release.SerializeToString(),
                                                             timeout=20)
    rows = _rows(stack["audit"])
    assert [r["args"]["rpc"] for r in rows] == ["Config", "ReleaseSession"]
    assert rows[0]["args"]["config"] == {"get": ["spark.sql.session.timeZone"]}
    assert [c[0] for c in stack["upstream"].calls] == ["Config", "ReleaseSession"]


# --------------------------------------------------------------------------- refusals
def _refused(exc_info, code):
    assert exc_info.value.code() == code, exc_info.value
    return exc_info.value.details()


def test_add_artifacts_refused_by_name(stack, protos):
    Add = _cls(protos, "AddArtifactsRequest")
    chunk = Add(session_id="s-1", batch=Add.Batch(artifacts=[Add.SingleChunkArtifact(
        name="jars/evil.jar", data=Add.ArtifactChunk(data=b"PK\x03\x04"))]))
    call = stack["channel"].stream_unary(SERVICE + "AddArtifacts")
    with pytest.raises(grpc.RpcError) as exc:
        call(iter([chunk.SerializeToString()]), timeout=20)
    assert _refused(exc, grpc.StatusCode.FAILED_PRECONDITION) == \
        "Spark feature not available in this environment: AddArtifacts"
    row = _rows(stack["audit"])[-1]
    assert row["tool"] == TOOL and row["args"]["rpc"] == "AddArtifacts"
    # read from the first message, which is all the proxy reads of the stream
    assert row["args"]["session_id"] == "s-1"
    assert row["args"]["artifacts"] == ["jars/evil.jar"]
    assert row["exit"] == "error" and "refused:AddArtifacts" in row["flags"]
    assert row["result"] == {"forwarded": False}
    assert not stack["upstream"].calls


def test_unknown_rpc_is_refused_and_recorded(stack, protos):
    for method in (SERVICE + "NoSuchMethod", "/grpc.health.v1.Health/Check"):
        with pytest.raises(grpc.RpcError) as exc:
            stack["channel"].unary_unary(method)(b"", timeout=20)
        assert _refused(exc, grpc.StatusCode.UNIMPLEMENTED) == \
            f"Method not found: {method.lstrip('/')}"
    rows = _rows(stack["audit"])
    assert [r["tool"] for r in rows] == ["databricks.unknown_route"] * 2
    assert rows[0]["args"] == {"rpc": "spark.connect.SparkConnectService/NoSuchMethod",
                               "transport": "spark_connect"}
    assert all(r["exit"] == "error" for r in rows)
    assert not stack["upstream"].calls


def _project(p, expression):
    return _cls(p, "Plan")(root=_cls(p, "Relation")(project=_cls(p, "Project")(
        input=_read(p, "t"), expressions=[expression])))


def _command(p, **kw):
    return _cls(p, "Plan")(command=_cls(p, "Command")(**kw))


def _python_udf(p):
    udf = _cls(p, "CommonInlineUserDefinedFunction")(
        function_name="f", python_udf=_cls(p, "PythonUDF")(command=b"pickle"))
    return _project(p, _cls(p, "Expression")(common_inline_user_defined_function=udf))


def _path_read(p):
    source = _cls(p, "Read").DataSource(format="parquet", paths=["/seed/x"])
    return _cls(p, "Plan")(root=_cls(p, "Relation")(read=_cls(p, "Read")(data_source=source)))


def _streaming_read(p):
    named = _cls(p, "Read").NamedTable(unparsed_identifier="t")
    return _cls(p, "Plan")(root=_cls(p, "Relation")(read=_cls(p, "Read")(
        named_table=named, is_streaming=True)))


def _reflect(p):
    function = _cls(p, "Expression").UnresolvedFunction(function_name="java_method")
    return _project(p, _cls(p, "Expression")(unresolved_function=function))


def _extension(p):
    return _command(p, extension=p.cls("google.protobuf.Any")(type_url="x/y"))


@pytest.mark.parametrize("what,build", [
    ("Python UDFs", _python_udf),
    ("file paths and URLs (DBFS and cloud paths are not provided; read a table)", _path_read),
    ("Structured Streaming", _streaming_read),
    ("Structured Streaming", lambda p: _command(
        p, write_stream_operation_start=_cls(p, "WriteStreamOperationStart")(
            input=_read(p, "t")))),
    ("Python UDFs (spark.udf.register)", lambda p: _command(
        p, register_function=_cls(p, "CommonInlineUserDefinedFunction")(function_name="f"))),
    ("file paths and URLs (DBFS and cloud paths are not provided; write a table)",
     lambda p: _command(p, write_operation=_cls(p, "WriteOperation")(
         input=_read(p, "t"), path="/tmp/out"))),
    ("reflect / java_method / try_reflect (calls a JVM method)", _reflect),
    ("Spark Connect extensions", _extension),
])
def test_fenced_plans_are_refused_by_name_and_never_forwarded(stack, protos, what, build):
    with pytest.raises(grpc.RpcError) as exc:
        _execute(stack, protos, build(protos))
    assert _refused(exc, grpc.StatusCode.FAILED_PRECONDITION) == \
        f"Spark feature not available in this environment: {what}"
    row = _rows(stack["audit"])[-1]
    assert row["exit"] == "error" and row["result"] == {"forwarded": False}
    assert any(f.startswith("refused:") for f in row["flags"])
    assert row["args"]["session_id"] == "s-1"          # which session tried it
    assert not stack["upstream"].calls


def test_config_set_of_a_fenced_key_is_refused(stack, protos):
    Config, KeyValue = _cls(protos, "ConfigRequest"), _cls(protos, "KeyValue")
    bad = Config(session_id="s-1", operation=Config.Operation(set=Config.Set(pairs=[
        KeyValue(key="spark.sql.session.timeZone", value="UTC"),
        KeyValue(key="spark.sql.catalog.evil", value="com.example.Catalog")])))
    with pytest.raises(grpc.RpcError) as exc:
        stack["channel"].unary_unary(SERVICE + "Config")(bad.SerializeToString(), timeout=20)
    assert _refused(exc, grpc.StatusCode.FAILED_PRECONDITION) == \
        "Spark feature not available in this environment: setting spark.sql.catalog.evil"
    ok = Config(session_id="s-1", operation=Config.Operation(set=Config.Set(pairs=[
        KeyValue(key="spark.sql.session.timeZone", value="UTC")])))
    stack["channel"].unary_unary(SERVICE + "Config")(ok.SerializeToString(), timeout=20)
    rows = _rows(stack["audit"])
    assert [r["exit"] for r in rows] == ["error", "ok"]
    assert rows[1]["args"]["config"] == {"set": ["spark.sql.session.timeZone"]}
    assert [c[0] for c in stack["upstream"].calls] == ["Config"]


# --------------------------------------------------------------------------- transport
def test_upstream_errors_pass_through_with_their_details(stack, protos):
    details = b"\x08\x03\x12\x04boom"              # a google.rpc.Status, as bytes
    stack["upstream"].fail["ExecutePlan"] = (
        grpc.StatusCode.INVALID_ARGUMENT, "[TABLE_OR_VIEW_NOT_FOUND] nope",
        (("grpc-status-details-bin", details),))
    with pytest.raises(grpc.RpcError) as exc:
        _execute(stack, protos, _cls(protos, "Plan")(root=_read(protos, "nope")))
    assert _refused(exc, grpc.StatusCode.INVALID_ARGUMENT) == "[TABLE_OR_VIEW_NOT_FOUND] nope"
    assert dict(exc.value.trailing_metadata())["grpc-status-details-bin"] == details
    # the attempt is still the agent's action: one row, written before the engine answered
    assert _rows(stack["audit"])[-1]["result"] == {"forwarded": True}


def test_client_metadata_reaches_the_engine(stack, protos):
    request = _cls(protos, "ReleaseSessionRequest")(session_id="s-1")
    stack["channel"].unary_unary(SERVICE + "ReleaseSession")(
        request.SerializeToString(), metadata=(("x-spark-connect-test", "1"),), timeout=20)
    assert stack["upstream"].calls[-1][2].get("x-spark-connect-test") == "1"


def test_every_accepted_connection_is_a_destination(stack, protos):
    request = _cls(protos, "ReleaseSessionRequest")(session_id="s-1").SerializeToString()
    stack["channel"].unary_unary(SERVICE + "ReleaseSession")(request, timeout=20)
    # its own subchannel pool: channels to one target share a connection otherwise
    second = grpc.insecure_channel(f"127.0.0.1:{stack['port']}",
                                   options=[("grpc.use_local_subchannel_pool", 1)])
    second.unary_unary(SERVICE + "ReleaseSession")(request, timeout=20)
    second.close()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        counts = {r["port"]: r["count"] for r in stack["destinations"].rows()}
        if counts.get(stack["port"]) == 2:
            break
        time.sleep(0.05)
    assert counts[stack["port"]] == 2


# --------------------------------------------------------------------------- the module
def test_module_names_its_registry_row():
    from alkera_gateway.backends import BACKENDS

    row = BACKENDS["spark_connect"]
    assert (sc.NAME, sc.HOST, sc.PORT) == (row.name, row.host, row.port)
    assert row.hosted and not row.tls and row.clients == "spark"
    assert row.env["SPARK_REMOTE"] == f"sc://{row.host}:{row.port}"


def test_descriptors_of_the_installed_client_match_the_fixture():
    """The proxy reads the image's own client; the fixture is that client's, frozen."""
    pytest.importorskip("pyspark", reason="pyspark-client is the image's (requirements-spark"
                                          ".lock), not the gateway venv's")
    live = sc.descriptor_set()
    assert live == TESTDATA.read_bytes()


# --------------------------------------------------------------------------- in serve.py
# The module is served by serve.py's own `serve_all` through `serve_socket` (Spark Connect
# is HTTP/2, which uvicorn does not speak), on the listener serve.py binds: the same peer
# filter as every agent listener (Task 6) and a count at accept (Task 5). Loopback stands
# in for the two networks, as in test_peer_filter.py: 127.0.0.1 is the episode, a client
# bound to 127.0.0.2 a backend_net peer.
def _served(monkeypatch, protos, upstream_port):
    from test_tls_destinations import free_port

    from alkera_gateway.native import serve

    monkeypatch.setenv(sc.UPSTREAM_ENV, f"127.0.0.1:{upstream_port}")
    monkeypatch.setattr(sc, "load_protos", lambda source=None: protos)
    return serve.Mounted(sc.NAME, sc.HOST, free_port(), sc.app, "module", False,
                         serve_socket=sc.serve_socket)


def _release(port: int, *, options=()):
    channel = grpc.insecure_channel(f"127.0.0.1:{port}", options=list(options))
    try:
        request = sc.load_protos(TESTDATA).cls("spark.connect.ReleaseSessionRequest")(
            session_id="s-1").SerializeToString()
        channel.unary_unary(SERVICE + "ReleaseSession")(request, timeout=20)
    finally:
        channel.close()


def test_mount_hands_the_hosted_module_its_socket():
    from alkera_gateway.native import serve

    item = {m.name: m for m in serve.mount({sc.NAME: sc})}[sc.NAME]
    assert item.serve_socket is sc.serve_socket and (item.host, item.port) == (sc.HOST, 15002)
    # every other module is served by uvicorn, as before
    stub = serve.mount({}, {"jira": 8080})[0]
    assert stub.serve_socket is None


def test_spark_connect_listener_refuses_backend_net_peers(tmp_path, monkeypatch, protos):
    import ipaddress

    from test_peer_filter import dial_from, rows
    from test_tls_destinations import count, serving, settled

    from alkera_gateway.native import serve

    audit = tmp_path / "audit" / "actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit))
    upstream = FakeUpstream(audit, protos)
    try:
        item = _served(monkeypatch, protos, upstream.port)
        log = serve.DestinationLog(tmp_path / "destinations.jsonl")
        log.declare(item.host, item.port)
        with serving([item], log, allowed=[ipaddress.IPv4Network("127.0.0.1/32")]):
            assert dial_from("127.0.0.2", item.port) == b""      # closed at accept
            time.sleep(0.5)
            assert count(log, item.port) == 0                    # never a destination
            _release(item.port)                                  # the episode's own
            assert settled(log, item.port, at_least=1) == 1
        refused = rows(audit)
        assert [r["args"] for r in refused] == [
            {"peer": "127.0.0.2", "host": sc.HOST, "port": item.port}]
        assert [r["args"]["rpc"] for r in rows(audit, TOOL)] == ["ReleaseSession"]
        assert [c[0] for c in upstream.calls] == ["ReleaseSession"]
    finally:
        upstream.stop()


def test_spark_connect_bare_dial_is_counted_once(tmp_path, monkeypatch, protos):
    from test_tls_destinations import bare_dial, serving, settled

    from alkera_gateway.native import serve

    audit = tmp_path / "audit" / "actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit))
    upstream = FakeUpstream(audit, protos)
    try:
        item = _served(monkeypatch, protos, upstream.port)
        log = serve.DestinationLog(tmp_path / "destinations.jsonl")
        log.declare(item.host, item.port)
        with serving([item], log):
            bare_dial(item.port)                     # a TCP connect and close: one
            assert settled(log, item.port, at_least=1) == 1
            # one gRPC connection carrying a call is one more, never two
            _release(item.port, options=[("grpc.use_local_subchannel_pool", 1)])
            assert settled(log, item.port, at_least=2) == 2
        assert not _rows(audit) or [r["tool"] for r in _rows(audit)] == [TOOL]
    finally:
        upstream.stop()
