"""Spark Connect at spark_connect.internal:15002: an audited gRPC proxy to the Spark engine.

    serve(("0.0.0.0", 15002), ("spark", 15002))        # blocks; tool "databricks.spark"

Databricks tier 2 (`alkera_gateway.backends.BACKENDS["spark_connect"]`, hosted) is
two processes, the BigQuery pattern. The ENGINE is Apache Spark 4.2.0 with Delta
Lake (gateway/docker/spark/Dockerfile.spark), a compose service on backend_net
the episode cannot reach, seeded from the world's warehouse at boot. The FRONT
DOOR is this module in native-services on episode_net, which the agent's own
client - Spark's thin client, `SparkSession.builder.getOrCreate()` with
$SPARK_REMOTE - talks to; this module talks to `$ALKERA_SPARK_CONNECT_UPSTREAM`
(`spark:15002` in compose), never a hard-coded address.

ONE 9.2.2 ROW PER RPC, WRITTEN BEFORE IT IS FORWARDED (the redshift proxy's
order, pgwire_proxy): the row is appended (fsync'd, under the writer lock) and
only then does the engine see the request, so every plan the engine ran has
its row on file first. Tool `databricks.spark`, `args.rpc` the method:

  ExecutePlan   `args.plan` summarises the plan - `root:filter(read)`,
                `command:write_operation(read)` - its operators in tree order;
                `args.sql` is the SQL text of an `sql` command or relation
                (spark.sql(...)), `args.expressions` any SQL expression strings
                (F.expr, selectExpr, filter("...")). op_class: the gateway's own
                SQL classifier for SQL text (pgwire_proxy.observation, dialect
                databricks: DROP is destructive, INSERT a write, CREATE ddl);
                `write` for WriteOperation / WriteOperationV2 /
                CreateDataFrameViewCommand / MergeIntoTableCommand; `read` for a
                relation. targets: the tables read and written, by the names the
                client sent.
  AnalyzePlan   schema / explain / ...: `args.analyze`, the plan summarised as
                above; read (persist and unpersist are exec).
  Config        `args.config` = {operation: [keys]} (values are not recorded);
                set / unset are exec, the rest read.
  the rest      Interrupt, ReattachExecute, ReleaseExecute, ReleaseSession,
                FetchErrorDetails, ArtifactStatus, CloneSession, GetStatus:
                session housekeeping the client does on its own; exec, a row each.

`result` is {"forwarded": bool}: what the engine then answered is not in the
row - the agent's next action is what follows it. An upstream error reaches the
client with its status, details and trailing metadata (`grpc-status-details-bin`,
which the client turns into Spark's own exception classes) unchanged.

REFUSED BY NAME, FAILED_PRECONDITION "Spark feature not available in this
environment: <feature>", one error row flagged `refused:<feature>`, never
forwarded - each something the engine would do and the box must not:

  AddArtifacts                 jars, pyfiles, archives and files uploaded to
                               the engine (and with them createDataFrame of
                               local data too large to send inline);
  Python UDFs / UDTFs / data sources   CommonInlineUserDefinedFunction in any
                               expression, spark.udf.register, mapInPandas,
                               applyInPandas, cogroup, *WithState, Python UDTFs
                               and data sources - code the engine would run;
  file paths and URLs          spark.read.load(path) and every Read.DataSource,
                               df.write.save(path), a `path` option, Catalog
                               createTable / createExternalTable with a path,
                               refreshByPath: DBFS and cloud storage are not
                               provided, tables are;
  Structured Streaming         readStream, writeStream, streaming queries and
                               listeners, withWatermark;
  reflect / java_method        a JVM method call from SQL or the DataFrame API;
  everything else that loads or runs code or reaches outside the catalog:
                               Spark ML, resource profiles, external commands,
                               declarative pipelines, extensions, compressed
                               plans this process cannot open;
  Config set of a key that changes what code runs or where data comes from
                               (FENCED_CONFS: spark.sql.catalog.*, extensions,
                               jars, hadoop.*, ...).

The engine's own check rule (gateway/docker/spark/java/org/alkera/spark/
Guard.java) fences what only the analyzer sees whatever the client sent - SQL
text: TRANSFORM, ADD JAR, CREATE FUNCTION, LOCATION, parquet.`/x`, SET of the
same keys. A method SparkConnectService does not publish, or another service,
is gRPC's own UNIMPLEMENTED "Method not found: <method>" and one
`databricks.unknown_route` row (the vendor 404 rule of every plug).

TRANSPORT. The listener is plain TCP: each accepted connection is counted
(`serve.DestinationLog.accepted`, at accept) and relayed byte for byte to a
grpcio server on a private unix socket in this process, which does the gRPC.
Plain gRPC, not TLS: Spark's thin client switches TLS on whenever the URL
carries a token (`;token=`), so the box's SPARK_REMOTE has none, and nothing
here checks one - the episode network is the boundary, as for every
native-services door.

THE CLIENT'S OWN MESSAGES. Requests are decoded with the descriptors of the
Spark Connect client the image pins (pyspark-client, gateway/requirements-
spark.lock), read from its generated `*_pb2.py` sources without importing
pyspark (`descriptor_set`); a FileDescriptorSet file stands in for them where
the client is not installed (the gateway tests). The engine is the same
release, so what the agent sends, this module reads and the engine runs are one
protocol.

WAITING FOR THE ENGINE HAPPENS AT IMPORT, the bigquery module's rule: serve.py
imports a module before it binds any socket, and native-services is healthy
only once every declared port listens. So when $ALKERA_SPARK_CONNECT_UPSTREAM is
set, importing this module waits until the engine accepts a connection - it
binds its port only once its seed is in - and raises SystemExit if it never
does, so `compose up --wait` fails loudly. Unset (the tests, every unplugged
stack) nothing is dialled at import.

SERVED BY serve.py THROUGH `serve_socket`, not `app`: Spark Connect is HTTP/2,
which uvicorn does not speak. `app` exists for the service contract (NAME, HOST,
PORT, app) and answers plain HTTP with a pointer to the gRPC door.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

NAME = "spark_connect"
HOST = "spark_connect.internal"
PORT = 15002
TOOL = "databricks.spark"
UNKNOWN_TOOL = "databricks.unknown_route"
ACTOR = "spark_connect"
#: Where the engine answers (`spark:15002` in compose). Read at import and per
#: proxy, never defaulted.
UPSTREAM_ENV = "ALKERA_SPARK_CONNECT_UPSTREAM"
#: How long importing this module waits for the engine (its cold start is
#: ~30 s plus the seed).
UPSTREAM_WAIT_S = float(os.environ.get("ALKERA_SPARK_CONNECT_WAIT_S", "600"))
SERVICE = "spark.connect.SparkConnectService"
NOT_AVAILABLE = "Spark feature not available in this environment"
#: Spark Connect's own inbound limit (spark.connect.grpc.maxInboundMessageSize)
#: and the client's: a plan may carry 64 MB of inline local data.
MAX_MESSAGE = 128 * 1024 * 1024
GRPC_OPTIONS = (("grpc.max_receive_message_length", MAX_MESSAGE),
                ("grpc.max_send_message_length", MAX_MESSAGE))
#: SparkConnectService's methods (spark/connect/base.proto, Spark 4.2): the
#: request message, the response's cardinality, and how the row reads it.
METHODS = {
    "ExecutePlan": ("ExecutePlanRequest", "unary_stream", "plan"),
    "AnalyzePlan": ("AnalyzePlanRequest", "unary_unary", "analyze"),
    "Config": ("ConfigRequest", "unary_unary", "config"),
    "AddArtifacts": ("AddArtifactsRequest", "stream_unary", "refused"),
    "ArtifactStatus": ("ArtifactStatusesRequest", "unary_unary", "session"),
    "Interrupt": ("InterruptRequest", "unary_unary", "session"),
    "ReattachExecute": ("ReattachExecuteRequest", "unary_stream", "session"),
    "ReleaseExecute": ("ReleaseExecuteRequest", "unary_unary", "session"),
    "ReleaseSession": ("ReleaseSessionRequest", "unary_unary", "session"),
    "FetchErrorDetails": ("FetchErrorDetailsRequest", "unary_unary", "session"),
    "CloneSession": ("CloneSessionRequest", "unary_unary", "session"),
    "GetStatus": ("GetStatusRequest", "unary_unary", "session"),
}
PATHS = "file paths and URLs (DBFS and cloud paths are not provided; {} a table)"
PYTHON_UDFS = "Python UDFs"
STREAMING = "Structured Streaming"
REFLECT = "reflect / java_method / try_reflect (calls a JVM method)"
#: Relation types refused wherever they appear in a plan, by `rel_type`.
FENCED_RELATIONS = {
    "map_partitions": PYTHON_UDFS, "group_map": PYTHON_UDFS, "co_group_map": PYTHON_UDFS,
    "apply_in_pandas_with_state": PYTHON_UDFS,
    "common_inline_user_defined_table_function": "Python UDTFs",
    "common_inline_user_defined_data_source": "Python data sources",
    "with_watermark": STREAMING,
    "cached_local_relation": "local data cached as an artifact (AddArtifacts)",
    "chunked_cached_local_relation": "local data cached as an artifact (AddArtifacts)",
    "ml_relation": "Spark ML",
    "extension": "Spark Connect extensions",
    "unknown": "an unknown relation",
}
#: Command types refused, by `command_type`. Every other command is listed in
#: COMMANDS with its op_class; a command in neither is refused too.
FENCED_COMMANDS = {
    "register_function": "Python UDFs (spark.udf.register)",
    "register_table_function": "Python UDTFs",
    "register_data_source": "Python data sources",
    "write_stream_operation_start": STREAMING,
    "streaming_query_command": STREAMING,
    "streaming_query_manager_command": STREAMING,
    "streaming_query_listener_bus_command": STREAMING,
    "create_resource_profile_command": "resource profiles",
    "ml_command": "Spark ML",
    "execute_external_command": "external commands (ExecuteExternalCommand)",
    "pipeline_command": "Spark Declarative Pipelines",
    "extension": "Spark Connect extensions",
}
COMMANDS = {
    "sql_command": "read",                 # the classifier decides (`_Walk.sql`)
    "write_operation": "write",
    "write_operation_v2": "write",
    "create_dataframe_view": "write",
    "merge_into_table_command": "write",
    "checkpoint_command": "exec",
    "remove_cached_remote_relation_command": "exec",
    "get_resources_command": "read",
}
#: Catalog API calls (spark.catalog.*) that change something; the rest read.
CATALOG_WRITES = frozenset({
    "set_current_database", "create_external_table", "create_table", "drop_temp_view",
    "drop_global_temp_view", "recover_partitions", "cache_table", "uncache_table",
    "clear_cache", "refresh_table", "refresh_by_path", "set_current_catalog",
    "drop_table", "drop_view", "create_database", "drop_database", "truncate_table",
    "analyze_table"})
REFLECTION = frozenset({"reflect", "java_method", "try_reflect"})
#: Configuration prefixes a Config set may not change (the engine's Guard
#: refuses the same keys in a SQL SET).
FENCED_CONFS = ("spark.sql.catalog.", "spark.sql.extensions", "spark.jars", "spark.files",
                "spark.archives", "spark.sql.warehouse.dir", "spark.hadoop.",
                "spark.sql.hive.", "spark.plugins", "spark.driver.", "spark.executor.",
                "spark.connect.", "spark.sql.streaming.", "spark.submit.", "spark.api.mode")
SEVERITY = {"read": 0, "exec": 1, "write": 2, "ddl": 3, "destructive": 4}
#: Request metadata the proxy does not forward: transport headers grpcio sets
#: itself on the upstream call.
HOP_BY_HOP = frozenset({"user-agent", "te", "content-type", "grpc-accept-encoding",
                        "grpc-encoding", "grpc-timeout"})
CHUNK = 1 << 16


# ------------------------------------------------------------------ the messages
def _client_proto_dir() -> Path:
    spec = importlib.util.find_spec("pyspark")     # finds; does not import pyspark
    if spec is None or not spec.origin:
        raise ImportError("pyspark-client is not installed (gateway/requirements-spark.lock)")
    return Path(spec.origin).parent / "sql" / "connect" / "proto"


def _serialized_files(proto_dir: Path) -> list[bytes]:
    """The FileDescriptorProto bytes each generated `*_pb2.py` hands to
    `AddSerializedFile`, read from its source: executing it would import pyspark."""
    out = []
    for path in sorted(proto_dir.glob("*_pb2.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") ==
                    "AddSerializedFile" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, bytes)):
                out.append(node.args[0].value)
    if not out:
        raise ImportError(f"no Spark Connect descriptors under {proto_dir}")
    return out


def descriptor_set(proto_dir: Path | None = None) -> bytes:
    """The installed client's Spark Connect protos (and the well-known types they
    import) as one FileDescriptorSet, dependencies first, serialized
    deterministically - the bytes gateway/tests/testdata holds."""
    import importlib

    from google.protobuf import descriptor_pb2

    files = {}
    for raw in _serialized_files(proto_dir or _client_proto_dir()):
        proto = descriptor_pb2.FileDescriptorProto.FromString(raw)
        files[proto.name] = proto
    # protobuf's own well-known types (any.proto, timestamp.proto, ...), from the
    # runtime's generated modules, until every dependency is present
    missing = {d for f in files.values() for d in f.dependency} - set(files)
    while missing:
        name = missing.pop()
        if not name.startswith("google/protobuf/"):
            raise ImportError(f"Spark Connect descriptors import {name}, which is not here")
        module = importlib.import_module(
            "google.protobuf." + name.removeprefix("google/protobuf/").removesuffix(".proto")
            .replace("/", ".") + "_pb2")
        proto = descriptor_pb2.FileDescriptorProto()
        module.DESCRIPTOR.CopyToProto(proto)
        files[name] = proto
        missing |= set(proto.dependency) - set(files)
    ordered, done = [], set()

    def visit(name: str) -> None:
        if name in done:
            return
        done.add(name)
        for dependency in files[name].dependency:
            visit(dependency)
        ordered.append(files[name])

    for name in sorted(files):
        visit(name)
    return descriptor_pb2.FileDescriptorSet(file=ordered).SerializeToString(
        deterministic=True)


class Protos:
    """Message classes over a private descriptor pool (never protobuf's default
    pool, where another copy of the same files could collide)."""

    def __init__(self, data: bytes):
        from google.protobuf import descriptor_pb2, descriptor_pool

        self.pool = descriptor_pool.DescriptorPool()
        for proto in descriptor_pb2.FileDescriptorSet.FromString(data).file:
            self.pool.Add(proto)
        self._classes: dict[str, type] = {}

    def cls(self, full_name: str) -> type:
        from google.protobuf import message_factory

        if full_name not in self._classes:
            self._classes[full_name] = message_factory.GetMessageClass(
                self.pool.FindMessageTypeByName(full_name))
        return self._classes[full_name]

    def parse(self, name: str, data: bytes):
        return self.cls(f"spark.connect.{name}").FromString(data)


def load_protos(source: Path | None = None) -> Protos:
    """The installed client's messages, or those of a FileDescriptorSet file."""
    return Protos(Path(source).read_bytes() if source is not None else descriptor_set())


# ------------------------------------------------------------------ reading a plan
class Refused(Exception):
    """A plan or request this environment does not run; str() is the feature."""


@dataclass
class Inspection:
    op_class: str = "read"
    plan: str = ""
    sql: list[str] = field(default_factory=list)
    expressions: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def raise_to(self, op_class: str) -> None:
        if SEVERITY[op_class] > SEVERITY[self.op_class]:
            self.op_class = op_class

    def target(self, name: str) -> None:
        if name and name not in self.targets:
            self.targets.append(name)

    def read(self, name: str) -> None:
        if name and name not in self.reads:
            self.reads.append(name)


class _Walk:
    """One plan, walked once: every Relation, Expression and Command anywhere in
    it, refusing a fenced one by name (raises Refused) and summarising the rest."""

    def __init__(self, found: Inspection):
        self.found = found

    def plan(self, plan, protos: Protos) -> str:
        kind = plan.WhichOneof("op_type")
        if kind == "root":
            return "root:" + self.relation(plan.root)
        if kind == "command":
            return "command:" + self.command(plan.command)
        if kind == "compressed_operation":
            relation, command = _decompress(plan.compressed_operation, protos)
            if relation is not None:
                return "root:" + self.relation(relation)
            return "command:" + self.command(command)
        raise Refused(f"a plan of kind {kind}")

    # -- relations
    def relation(self, rel) -> str:
        kind = rel.WhichOneof("rel_type")
        if kind is None:
            return "empty"
        if kind in FENCED_RELATIONS:
            raise Refused(FENCED_RELATIONS[kind])
        body = getattr(rel, kind)
        if kind == "read":
            self._read(body)
        elif kind == "sql":
            self.sql(body.query)
        elif kind == "catalog":
            self._catalog(body)
        children = self.children(body)
        return f"{kind}({','.join(children)})" if children else kind

    def _read(self, read) -> None:
        if read.is_streaming:
            raise Refused(STREAMING)
        which = read.WhichOneof("read_type")
        if which == "named_table":
            if "path" in read.named_table.options:
                raise Refused(PATHS.format("read"))
            self.found.read(read.named_table.unparsed_identifier)
            return
        source = read.data_source
        if source.paths or "path" in source.options:
            raise Refused(PATHS.format("read"))
        raise Refused(f"the {source.format or 'unnamed'} data source (read a table)")

    def _catalog(self, catalog) -> None:
        kind = catalog.WhichOneof("cat_type")
        body = getattr(catalog, kind) if kind else None
        if kind == "refresh_by_path" or (
                kind in ("create_external_table", "create_table")
                and (body.path or "path" in body.options)):
            raise Refused(PATHS.format("create"))
        if kind in CATALOG_WRITES:
            self.found.raise_to("ddl" if kind.startswith(("create", "drop", "truncate"))
                                else "exec")
        name = getattr(body, "table_name", "") if body is not None else ""
        if name:
            self.found.target(name)

    # -- expressions
    def expression(self, expr) -> None:
        kind = expr.WhichOneof("expr_type")
        if kind == "common_inline_user_defined_function":
            udf = expr.common_inline_user_defined_function
            raise Refused(PYTHON_UDFS if udf.WhichOneof("function") in ("python_udf", None)
                          else "JVM UDFs")
        if kind == "typed_aggregate_expression":
            raise Refused("JVM UDFs")
        if kind == "extension":
            raise Refused("Spark Connect extensions")
        if kind == "unresolved_function" and \
                expr.unresolved_function.function_name.lower() in REFLECTION:
            raise Refused(REFLECT)
        if kind == "expression_string":
            self.found.expressions.append(expr.expression_string.expression)

    # -- commands
    def command(self, cmd) -> str:
        kind = cmd.WhichOneof("command_type")
        if kind in FENCED_COMMANDS:
            raise Refused(FENCED_COMMANDS[kind])
        if kind not in COMMANDS:
            raise Refused(f"the {kind} command")
        body = getattr(cmd, kind)
        self.found.raise_to(COMMANDS[kind])
        if kind == "sql_command" and body.sql:
            self.sql(body.sql)
        elif kind == "write_operation":
            if body.WhichOneof("save_type") == "path" or "path" in body.options:
                raise Refused(PATHS.format("write"))
            self.found.target(body.table.table_name)
        elif kind == "write_operation_v2":
            if "path" in body.options:
                raise Refused(PATHS.format("write"))
            self.found.target(body.table_name)
        elif kind == "create_dataframe_view":
            self.found.target(body.name)
        elif kind == "merge_into_table_command":
            self.found.target(body.target_table_name)
        children = self.children(body)
        return f"{kind}({','.join(children)})" if children else kind

    def sql(self, text: str) -> None:
        from ..pgwire_proxy import observation

        self.found.sql.append(text)
        seen = observation(text, "databricks")
        self.found.raise_to(seen.op_class if seen.op_class in SEVERITY else "exec")
        for name in seen.targets:
            self.found.target(name)
        for name in seen.read_targets:
            self.found.read(name)
        self.found.flags += [f for f in seen.flags if f not in self.found.flags]

    # -- the generic part
    def children(self, message) -> list[str]:
        """Summaries of every Relation below `message` (any depth, any field),
        each Expression checked on the way down."""
        from google.protobuf.message import Message

        out: list[str] = []
        for fd, value in message.ListFields():
            if fd.message_type is None:
                continue
            if fd.message_type.GetOptions().map_entry:
                values = [v for v in value.values() if isinstance(v, Message)]
            else:
                values = [value] if isinstance(value, Message) else list(value)
            for item in values:
                name = item.DESCRIPTOR.full_name
                if name == "spark.connect.Relation":
                    out.append(self.relation(item))
                elif name == "spark.connect.Command":
                    out.append(self.command(item))
                else:
                    if name == "spark.connect.Expression":
                        self.expression(item)
                    out += self.children(item)
        return out


def _decompress(compressed, protos: Protos):
    """(relation, command) from a Plan.CompressedOperation, or Refused."""
    codec = compressed.compression_codec
    try:
        import zstandard
    except ImportError as exc:
        raise Refused("compressed plans (zstandard is not installed here)") from exc
    if codec != 1:                                   # COMPRESSION_CODEC_ZSTD
        raise Refused(f"compressed plans (codec {codec})")
    data = zstandard.ZstdDecompressor().decompress(compressed.data,
                                                   max_output_size=MAX_MESSAGE)
    if compressed.op_type == 1:                      # OP_TYPE_RELATION
        return protos.parse("Relation", data), None
    if compressed.op_type == 2:                      # OP_TYPE_COMMAND
        return None, protos.parse("Command", data)
    raise Refused(f"compressed plans (op type {compressed.op_type})")


def inspect_plan(plan, protos: Protos) -> Inspection:
    """What a plan does, or Refused naming the first fenced feature in it."""
    found = Inspection()
    found.plan = _Walk(found).plan(plan, protos)
    if found.op_class == "read":
        found.targets = list(dict.fromkeys(found.reads + found.targets))
    return found


# ------------------------------------------------------------------ the rows
def describe(method: str, request: bytes, protos: Protos) -> tuple[dict, Inspection]:
    """(args, inspection) for one SparkConnectService request; raises Refused."""
    message, _, kind = METHODS[method]
    args: dict = {"rpc": method}
    found = Inspection(op_class="exec")
    if kind == "refused":
        raise Refused(method)
    parsed = protos.parse(message, request)
    if parsed.session_id:
        # which client session sent it: the client chooses it (a UUID), and the
        # checks match a session's rows to what that session sent
        args["session_id"] = parsed.session_id
    if kind == "plan":
        found = inspect_plan(parsed.plan, protos)
    elif kind == "analyze":
        which = parsed.WhichOneof("analyze")
        args["analyze"] = which
        body = getattr(parsed, which) if which else None
        found = Inspection(op_class="exec" if which in ("persist", "unpersist") else "read")
        walk = _Walk(found)
        plans = [v for fd, v in (body.ListFields() if body is not None else [])
                 if fd.message_type is not None
                 and fd.message_type.full_name == "spark.connect.Plan"]
        relations = [v for fd, v in (body.ListFields() if body is not None else [])
                     if fd.message_type is not None
                     and fd.message_type.full_name == "spark.connect.Relation"]
        parts = [walk.plan(p, protos) for p in plans] + [walk.relation(r) for r in relations]
        if parts:
            found.plan = ";".join(parts)
        found.targets = list(dict.fromkeys(found.reads + found.targets))
    elif kind == "config":
        op = parsed.operation.WhichOneof("op_type")
        body = getattr(parsed.operation, op) if op else None
        keys = ([p.key for p in body.pairs] if hasattr(body, "pairs") else
                list(getattr(body, "keys", [])) if body is not None else [])
        args["config"] = {op: keys}
        found = Inspection(op_class="exec" if op in ("set", "unset") else "read")
        if op == "set":
            for key in keys:
                if key.startswith(FENCED_CONFS):
                    raise _RefusedWith(args, f"setting {key}")
    elif method == "ReattachExecute" or method == "ReleaseExecute":
        args["operation_id"] = parsed.operation_id
    elif method == "Interrupt":
        args["interrupt"] = parsed.WhichOneof("interrupt") or str(parsed.interrupt_type)
    if found.plan:
        args["plan"] = found.plan
    if found.sql:
        args["sql"] = found.sql[0] if len(found.sql) == 1 else ";\n".join(found.sql)
    if found.expressions:
        args["expressions"] = found.expressions
    return args, found


class _RefusedWith(Refused):
    """A refusal that already knows the row's args (a Config set)."""

    def __init__(self, row_args: dict, feature: str):
        super().__init__(feature)
        self.row_args = row_args


# ------------------------------------------------------------------ the proxy
@dataclass
class _Call:
    method: str
    started: float


class Proxy:
    """One listening proxy: a TCP front (accepts counted) relayed to a grpcio
    server on a private unix socket, which audits and forwards each RPC."""

    def __init__(self, upstream: tuple[str, int], *, protos: Protos | None = None,
                 tool: str = TOOL, destinations=None,
                 record: Callable[..., dict] | None = None):
        self.upstream = upstream
        self.protos = protos or load_protos()
        self.tool = tool
        self.destinations = destinations
        self._record = record
        self._audit_lock = asyncio.Lock()
        self._dir: str | None = None
        self._grpc = None
        self._channel = None
        self._front: asyncio.base_events.Server | None = None

    def record_fn(self):
        if self._record is None:
            from ..audit import record

            self._record = record
        return self._record

    # -- lifecycle
    async def _start_grpc(self) -> str:
        import grpc

        self._dir = tempfile.mkdtemp(prefix="sc-")
        path = os.path.join(self._dir, "grpc.sock")
        self._grpc = grpc.aio.server(options=GRPC_OPTIONS)
        self._grpc.add_generic_rpc_handlers((_Handler(self),))
        self._grpc.add_insecure_port(f"unix:{path}")
        await self._grpc.start()
        host, port = self.upstream
        self._channel = grpc.aio.insecure_channel(f"{host}:{port}", options=GRPC_OPTIONS)
        return path

    async def start(self, host: str, port: int) -> int:
        """Bind (host, port) and serve; returns the bound port."""
        path = await self._start_grpc()
        self._front = await asyncio.start_server(
            lambda r, w: self._relay(r, w, path), host, port)
        return self._front.sockets[0].getsockname()[1]

    async def start_on(self, listener: socket.socket) -> None:
        """Serve a socket already bound and listening (serve.py's)."""
        path = await self._start_grpc()
        listener.setblocking(False)
        self._front = await asyncio.start_server(
            lambda r, w: self._relay(r, w, path), sock=listener)

    async def stop(self) -> None:
        if self._front is not None:
            self._front.close()
            with contextlib.suppress(Exception):
                await self._front.wait_closed()
        if self._grpc is not None:
            await self._grpc.stop(1)
        if self._channel is not None:
            await self._channel.close()
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)

    # -- the front door
    async def _relay(self, reader, writer, path: str) -> None:
        sock = writer.get_extra_info("sockname")
        if self.destinations is not None and sock:
            self.destinations.accepted(int(sock[1]))
        try:
            up_reader, up_writer = await asyncio.open_unix_connection(path)
        except OSError:
            writer.close()
            return

        async def pump(src, dst):
            try:
                while chunk := await src.read(CHUNK):
                    dst.write(chunk)
                    await dst.drain()
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    dst.close()

        await asyncio.gather(pump(reader, up_writer), pump(up_reader, writer))

    # -- the row
    def _write_row(self, *, tool: str, args: dict, found: Inspection | None,
                   refused: str | None, latency_ms: float, flags: list[str]) -> None:
        found = found or Inspection(op_class="exec")
        reads = found.reads if found.op_class != "read" else found.targets
        self.record_fn()(
            tool=tool, args=args, op_class=found.op_class, targets=list(found.targets),
            read_targets=list(reads),
            exit="error" if refused else "ok",
            error={"code": "FAILED_PRECONDITION" if tool == self.tool else "UNIMPLEMENTED",
                   "message": refused} if refused else None,
            latency_ms=latency_ms, result={"forwarded": refused is None},
            flags=list(found.flags) + flags, actor=ACTOR)

    async def audit(self, method: str, request: bytes, started: float) -> str | None:
        """Write the row for one RPC; the refusal's message, or None to forward."""
        args, found, refused, flags = {"rpc": method}, None, None, []
        try:
            args, found = describe(method, request, self.protos)
        except _RefusedWith as exc:
            args, refused = exc.row_args, str(exc)
        except Refused as exc:
            refused = str(exc)
        except Exception as exc:  # noqa: BLE001 - an unreadable request is never forwarded blind
            refused = f"a request this proxy cannot read ({type(exc).__name__})"
        if refused:
            refused = f"{NOT_AVAILABLE}: {refused}"
            flags.append(f"refused:{refused.split(': ', 1)[1].split(' (')[0]}"
                         if method != "AddArtifacts" else "refused:AddArtifacts")
            if found is None:
                found = Inspection(op_class="exec")
        latency = (time.monotonic() - started) * 1000
        async with self._audit_lock:
            await asyncio.to_thread(self._write_row, tool=self.tool, args=args, found=found,
                                    refused=refused, latency_ms=latency, flags=flags)
        return refused

    async def unknown(self, method: str) -> str:
        message = f"Method not found: {method.lstrip('/')}"
        async with self._audit_lock:
            await asyncio.to_thread(
                self._write_row, tool=UNKNOWN_TOOL,
                args={"rpc": method.lstrip("/"), "transport": "spark_connect"},
                found=Inspection(op_class="exec"), refused=message, latency_ms=0.0,
                flags=["unknown_rpc"])
        return message

    def upstream_call(self, method: str, cardinality: str):
        factory = getattr(self._channel, cardinality)
        return factory(f"/{SERVICE}/{method}", request_serializer=None,
                       response_deserializer=None)


def _forwardable(metadata) -> tuple:
    return tuple((k, v) for k, v in (metadata or ()) if k not in HOP_BY_HOP
                 and not k.startswith(":"))


class _Handler:
    """grpcio's GenericRpcHandler for every method, published or not."""

    def __init__(self, proxy: Proxy):
        self.proxy = proxy

    def service(self, details):
        import grpc

        method = details.method or ""
        service, _, name = method.lstrip("/").rpartition("/")
        proxy = self.proxy
        if service != SERVICE or name not in METHODS:
            async def unknown(requests, context):
                await context.abort(grpc.StatusCode.UNIMPLEMENTED, await proxy.unknown(method))
                yield b""  # pragma: no cover - abort raises
            return grpc.stream_stream_rpc_method_handler(unknown)
        _, cardinality, _ = METHODS[name]

        async def refuse_or_forward(request, context):
            refused = await proxy.audit(name, request, time.monotonic())
            if refused:
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, refused)
            return _forwardable(context.invocation_metadata())

        async def passthrough_error(context, exc) -> None:
            await context.abort(exc.code(), exc.details() or "",
                                tuple(exc.trailing_metadata() or ()))

        if cardinality == "unary_stream":
            async def unary_stream(request, context):
                metadata = await refuse_or_forward(request, context)
                call = proxy.upstream_call(name, "unary_stream")(request, metadata=metadata)
                try:
                    async for response in call:
                        yield response
                    trailing = await call.trailing_metadata()
                    if trailing:
                        context.set_trailing_metadata(tuple(trailing))
                except grpc.aio.AioRpcError as exc:
                    await passthrough_error(context, exc)
            return grpc.unary_stream_rpc_method_handler(unary_stream)

        if cardinality == "stream_unary":                # AddArtifacts: refused unread
            async def stream_unary(requests, context):
                refused = await proxy.audit(name, b"", time.monotonic())
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, refused)
            return grpc.stream_unary_rpc_method_handler(stream_unary)

        async def unary_unary(request, context):
            metadata = await refuse_or_forward(request, context)
            call = proxy.upstream_call(name, "unary_unary")(request, metadata=metadata)
            try:
                response = await call
                trailing = await call.trailing_metadata()
                if trailing:
                    context.set_trailing_metadata(tuple(trailing))
                return response
            except grpc.aio.AioRpcError as exc:
                await passthrough_error(context, exc)
        return grpc.unary_unary_rpc_method_handler(unary_unary)


# ------------------------------------------------------------------ serving
def _upstream_from_env() -> tuple[str, int]:
    value = os.environ.get(UPSTREAM_ENV, "")
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"{UPSTREAM_ENV} must be host:port, not {value!r}")
    return host, int(port)


async def serve_socket(listener: socket.socket, destinations=None) -> None:
    """serve.py's entry for this module: serve the bound `listener` until
    cancelled, forwarding to $ALKERA_SPARK_CONNECT_UPSTREAM, counting each
    accepted connection into `destinations`."""
    proxy = Proxy(_upstream_from_env(), destinations=destinations)
    await proxy.start_on(listener)
    try:
        await asyncio.Event().wait()
    finally:
        await proxy.stop()


def serve(listen: tuple[str, int], upstream: tuple[str, int], *, tool: str = TOOL,
          destinations=None, protos: Protos | None = None,
          on_listening: Callable[[int], None] | None = None) -> None:
    """Listen on `listen`, relay to `upstream`, audit every RPC. Blocks."""

    async def main() -> None:
        proxy = Proxy(upstream, protos=protos, tool=tool, destinations=destinations)
        port = await proxy.start(*listen)
        if on_listening is not None:
            on_listening(port)
        try:
            await asyncio.Event().wait()
        finally:
            await proxy.stop()

    asyncio.run(main())


def wait_for_engine(upstream: tuple[str, int], budget_s: float = UPSTREAM_WAIT_S) -> float:
    """Seconds until the engine accepts a TCP connection (it binds only once
    seeded); SystemExit when it never does. Straight upstream: no row."""
    started = time.monotonic()
    while True:
        try:
            with socket.create_connection(upstream, timeout=2):
                return time.monotonic() - started
        except OSError as exc:
            if time.monotonic() - started > budget_s:
                raise SystemExit(f"spark_connect: the engine at {upstream[0]}:{upstream[1]} "
                                 f"never answered in {budget_s:.0f} s ({exc})") from exc
            time.sleep(0.5)


# ------------------------------------------------------------------ the module contract
async def app(scope, receive, send) -> None:
    """Plain HTTP on this port is not Spark Connect: say where the door is."""
    if scope["type"] != "http":
        return
    body = (json.dumps({"error": "Spark Connect speaks gRPC (HTTP/2): connect with "
                                 f"SparkSession.builder.remote('sc://{HOST}:{PORT}')"})
            + "\n").encode()
    await send({"type": "http.response.start", "status": 426,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


if os.environ.get(UPSTREAM_ENV) and __name__ != "__main__":
    _waited = wait_for_engine(_upstream_from_env())
    print(json.dumps({"spark_connect": "engine answered", "upstream": os.environ[UPSTREAM_ENV],
                      "waited_s": round(_waited, 1)}), flush=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write-descriptors", type=Path,
                        help="write the installed client's FileDescriptorSet here and exit")
    parser.add_argument("--listen", default=f"0.0.0.0:{PORT}")
    parser.add_argument("--upstream", default=None, help=f"host:port (default ${UPSTREAM_ENV})")
    args = parser.parse_args(argv)
    if args.write_descriptors:
        args.write_descriptors.write_bytes(descriptor_set())
        print(args.write_descriptors)
        return 0
    if args.upstream:
        os.environ[UPSTREAM_ENV] = args.upstream
    host, _, port = args.listen.rpartition(":")
    serve((host, int(port)), _upstream_from_env())
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["NAME", "HOST", "PORT", "TOOL", "app", "serve", "serve_socket", "Proxy",
           "load_protos", "descriptor_set", "inspect_plan", "describe", "Refused"]
