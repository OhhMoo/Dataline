"""Power Query (M) partition sources <-> Table.source / Table.source_sql.

WHY M AT ALL. In a Power BI semantic model a table does not name a warehouse
relation; its partition carries an M query that navigates to one. A data
engineer reading `tables/Orders.tmdl` expects the connector the team's
warehouse uses, behind parameters a deployment pipeline can rebind:

    expressions.tmdl
        expression DatabricksServer = "adb-...azuredatabricks.net" meta [IsParameterQuery=true, ...]
    tables/Orders.tmdl
        partition Orders = m
            mode: import
            source =
                    let
                        Source = Databricks.Catalogs(DatabricksServer, DatabricksHttpPath, [...]),
                        Database = Source{[Name=DatabricksCatalog,Kind="Database"]}[Data],
                        warehouse_Schema = Database{[Name="warehouse",Kind="Schema"]}[Data],
                        fct_orders_Table = warehouse_Schema{[Name="fct_orders",Kind="Table"]}[Data]
                    in
                        fct_orders_Table

That is the navigation Power BI Desktop's connector generates (step names
`<name>_Schema` / `<name>_Table`), with the host, HTTP path and catalog lifted
into parameters, which is how CI/CD-managed models are kept environment-free.
A table with `source_sql` becomes `Value.NativeQuery(<database>, "<sql>", null,
[EnableFolding=true])`.

THE READER IS A TINY SYMBOLIC M EVALUATOR, not a pattern match: it parses the
let-expression, resolves step names and shared expressions (parameters and
non-parameter queries alike, so a model whose partitions start from a shared
`Source = DatabaseQuery` - the Fabric Direct Lake layout - reads too), and
evaluates only connector calls, record navigation `{[...]}[Data]` and
Value.NativeQuery. Every other function a step applies raises
Unsupported("Power Query step <Function>") naming the function (detail: the
step), because a Table.SelectRows or a Table.TransformColumnTypes changes the
rows or the types the engine would read, and the engine reads the relation.

Connectors read: Databricks.Catalogs / DatabricksMultiCloud.Catalogs,
Snowflake.Databases, Sql.Database (also with [Query="..."]), Odbc.DataSource
with [HierarchicalNavigation=true] and Fabric Direct Lake entity partitions
(TMDL `partition x = entity` with entityName/schemaName, handled in tmdl.py).
Snowflake stores unquoted identifiers upper-case and Power BI navigates by the
stored name, so the writer upper-cases them and the reader lower-cases them
back.

DUCKDB. Power BI has no DuckDB connector; a workstation reaches a DuckDB file
through the DuckDB ODBC driver, and Desktop's ODBC dialog writes
`Odbc.DataSource(<connection string>, [HierarchicalNavigation=true])` followed
by the same Database / Schema / Table navigation the warehouse connectors use
(DuckDB's catalog is the file stem). That is the `duckdb` connector, the laptop
stack's. Without HierarchicalNavigation an ODBC source is a flat list of
tables whose names carry no schema, so that form stays refused by name.

Sources: Power Query M language specification (learn.microsoft.com/powerquery-m,
"Lexical structure", "let expression", "Item access", "Field access"); connector
pages learn.microsoft.com/power-query/connectors/databricks,
/databricks-azure, /snowflake, /sql-server; Value.NativeQuery
(learn.microsoft.com/powerquery-m/value-nativequery); Desktop-generated sample
models in github.com/RuiRomano/pbip-demo-agentic and
github.com/microsoft/fabric-toolbox (MIT; vendored under
world/tests/fixtures/powerbi/samples).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...errors import ModelError, Unsupported
from ...model import Table

VENDOR = "Power BI"


@dataclass(frozen=True)
class Connector:
    """One warehouse connector: its parameters (name, default value) and how it
    spells a navigation to schema.relation."""
    name: str
    parameters: tuple[tuple[str, str], ...]


#: Parameter defaults are the values a team's dev environment would carry. They
#: are hostnames, not credentials (Power BI never stores credentials in TMDL),
#: and the world never dials them: the fake service reads the warehouse slot.
CONNECTORS = {
    "databricks": Connector("databricks", (
        ("DatabricksServer", "adb-4412038295371946.6.azuredatabricks.net"),
        ("DatabricksHttpPath", "/sql/1.0/warehouses/7c1e5d0a9b3f2e64"),
        ("DatabricksCatalog", "analytics"),
    )),
    "snowflake": Connector("snowflake", (
        ("SnowflakeServer", "xy12345.west-europe.azure.snowflakecomputing.com"),
        ("SnowflakeWarehouse", "BI_WH"),
        ("SnowflakeDatabase", "ANALYTICS"),
    )),
    "sql": Connector("sql", (
        ("SqlServer", "analytics-sql.database.windows.net"),
        ("SqlDatabase", "analytics"),
    )),
    # The DuckDB ODBC driver's documented connection string; the path is where
    # the world's warehouse file is on the box. DuckDBCatalog is the file stem.
    "duckdb": Connector("duckdb", (
        ("DuckDBConnectionString",
         "Driver={DuckDB Driver};Database=/world/var/warehouse/warehouse.duckdb"),
        ("DuckDBCatalog", "warehouse"),
    )),
}
DEFAULT_CONNECTOR = "databricks"


def unsupported(feature: str, detail: str = "") -> Unsupported:
    return Unsupported(feature, VENDOR, detail)


# ======================================================================
# Writing
# ======================================================================
def m_string(text: str) -> str:
    """An M text literal, with M's escapes (#(lf), #(cr), #(tab), "")."""
    out = text.replace("#(", "#(#)(").replace('"', '""')
    out = out.replace("\r", "#(cr)").replace("\n", "#(lf)").replace("\t", "#(tab)")
    return f'"{out}"'


def m_identifier(name: str) -> str:
    """A step name: bare when it is a plain identifier, #"..." otherwise."""
    if name.replace("_", "a").isalnum() and not name[:1].isdigit() and name.isascii():
        return name
    return "#" + m_string(name)


def parameter_expressions(connector: str) -> list[tuple[str, str]]:
    """The shared expressions (expressions.tmdl) a connector's partitions use:
    (name, M text including the parameter `meta` record)."""
    spec = CONNECTORS[connector]
    meta = ' meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'
    return [(name, m_string(value) + meta) for name, value in spec.parameters]


def partition_source(table: Table, connector: str) -> str:
    """The M query of a table's single partition, as Desktop lays it out:
    one step per line, four-space indent inside let/in."""
    steps: list[tuple[str, str]] = []
    if connector == "databricks":
        steps.append(("Source", "Databricks.Catalogs(DatabricksServer, DatabricksHttpPath, "
                                "[Catalog=null, Database=null, "
                                "EnableAutomaticProxyDiscovery=null])"))
        steps.append(("Database", 'Source{[Name=DatabricksCatalog,Kind="Database"]}[Data]'))
        database = "Database"
    elif connector == "snowflake":
        steps.append(("Source", "Snowflake.Databases(SnowflakeServer, SnowflakeWarehouse, "
                                "[Role=null, CreateNavigationProperties=null, "
                                "ConnectionTimeout=null, CommandTimeout=null])"))
        steps.append(("Database", 'Source{[Name=SnowflakeDatabase,Kind="Database"]}[Data]'))
        database = "Database"
    elif connector == "sql":
        steps.append(("Source", "Sql.Database(SqlServer, SqlDatabase)"))
        database = "Source"
    elif connector == "duckdb":
        steps.append(("Source", "Odbc.DataSource(DuckDBConnectionString, "
                                "[HierarchicalNavigation=true])"))
        steps.append(("Database", 'Source{[Name=DuckDBCatalog,Kind="Database"]}[Data]'))
        database = "Database"
    else:
        raise ModelError(f"unknown Power BI connector {connector!r}; "
                         f"known: {', '.join(sorted(CONNECTORS))}")
    if table.source_sql is not None:
        steps.append(("Query", f"Value.NativeQuery({database}, {m_string(table.source_sql)}, "
                               f"null, [EnableFolding=true])"))
    else:
        if not table.source or "." not in table.source:
            raise ModelError(f"table {table.name!r} needs a 'schema.relation' source")
        schema, relation = table.source.split(".", 1)
        if connector == "snowflake":
            schema, relation = schema.upper(), relation.upper()
        if connector == "sql":
            step = m_identifier(f"{schema}_{relation}")
            steps.append((step, f"Source{{[Schema={m_string(schema)},"
                                f"Item={m_string(relation)}]}}[Data]"))
        else:
            schema_step = m_identifier(f"{schema}_Schema")
            table_step = m_identifier(f"{relation}_Table")
            steps.append((schema_step, f"{database}{{[Name={m_string(schema)},"
                                       f'Kind="Schema"]}}[Data]'))
            steps.append((table_step, f"{schema_step}{{[Name={m_string(relation)},"
                                      f'Kind="Table"]}}[Data]'))
    lines = ["let"]
    for k, (name, value) in enumerate(steps):
        lines.append(f"    {name} = {value}" + ("," if k < len(steps) - 1 else ""))
    lines += ["in", f"    {steps[-1][0]}"]
    return "\n".join(lines)


# ======================================================================
# Reading: tokens
# ======================================================================
@dataclass(frozen=True)
class MTok:
    kind: str      # ident str num op eof
    text: str
    value: Any
    pos: int


KEYWORDS = {"let", "in", "null", "true", "false", "meta", "each", "if", "then", "else",
            "type", "try", "otherwise", "and", "or", "not", "as", "is", "section", "shared"}


def decode_m_string(raw: str) -> str:
    """The body of an M text literal (quotes already removed, "" collapsed)."""
    out, i = [], 0
    names = {"lf": "\n", "cr": "\r", "tab": "\t", "#": "#"}
    while i < len(raw):
        if raw.startswith("#(", i):
            end = raw.find(")", i)
            if end < 0:
                break
            codes = raw[i + 2:end]
            for code in codes.split(","):
                code = code.strip()
                if code.lower() in names:
                    out.append(names[code.lower()])
                elif code and all(c in "0123456789abcdefABCDEF" for c in code):
                    out.append(chr(int(code, 16)))
                else:
                    raise ModelError(f"invalid M escape #({codes})")
            i = end + 1
            continue
        out.append(raw[i])
        i += 1
    return "".join(out)


def m_tokens(text: str) -> list[MTok]:
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch == '"' or text.startswith('#"', i):
            start = i
            j = i + (2 if ch == "#" else 1)
            buf = []
            while True:
                if j >= n:
                    raise ModelError(f"Power Query: unterminated text at offset {start}")
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            value = decode_m_string("".join(buf))
            out.append(MTok("ident" if ch == "#" else "str", text[start:j + 1], value, start))
            i = j + 1
            continue
        if ch.isdigit():
            j = i
            while j < n and (text[j].isdigit() or text[j] == "."):
                j += 1
            out.append(MTok("num", text[i:j], text[i:j], i))
            i = j
            continue
        if ch.isalpha() or ch == "_" or ch == "#":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "_."):
                j += 1
            word = text[i:j]
            out.append(MTok("ident", word, word, i))
            i = j
            continue
        if text.startswith("=>", i) or text.startswith("<>", i) or text.startswith("<=", i) \
                or text.startswith(">=", i):
            out.append(MTok("op", text[i:i + 2], text[i:i + 2], i))
            i += 2
            continue
        out.append(MTok("op", ch, ch, i))
        i += 1
    out.append(MTok("eof", "", None, n))
    return out


# ======================================================================
# Reading: syntax
# ======================================================================
@dataclass(frozen=True)
class MNode:
    kind: str                 # let ident str num null bool call record list item field meta
    value: Any = None
    args: tuple = ()
    pos: int = 0


def called_function(node: MNode) -> str | None:
    """`step Table.SelectRows` for a call node, else None."""
    if node.kind == "call" and node.args and node.args[0].kind == "ident":
        return f"step {node.args[0].value}"
    return None


class MParser:
    """The expression subset partition sources are written in: let/in, calls,
    records, lists, item access {..}, field access [..], literals, `meta`."""

    def __init__(self, text: str):
        self.text = text
        self.toks = m_tokens(text)
        self.i = 0

    def peek(self) -> MTok:
        return self.toks[self.i]

    def next(self) -> MTok:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def is_op(self, text: str) -> bool:
        tok = self.peek()
        return tok.kind == "op" and tok.text == text

    def is_word(self, word: str) -> bool:
        tok = self.peek()
        return tok.kind == "ident" and tok.text == word

    def expect(self, text: str) -> None:
        if not self.is_op(text):
            raise self.error()
        self.next()

    def error(self) -> ModelError:
        tok = self.peek()
        line = self.text.count("\n", 0, tok.pos) + 1
        return ModelError(f"Power Query: Token '{tok.text or 'end of input'}' unexpected at "
                          f"line {line}.")

    def parse_all(self) -> MNode:
        node = self.expr()
        if self.peek().kind != "eof":
            raise self.error()
        return node

    def expr(self) -> MNode:
        if self.is_word("let"):
            pos = self.next().pos
            binds = []
            while True:
                name = self.next()
                if name.kind != "ident":
                    raise self.error()
                self.expect("=")
                binds.append((name.value, self.expr(), name.pos))
                if self.is_op(","):
                    self.next()
                    continue
                break
            if not self.is_word("in"):
                raise self.error()
            self.next()
            return MNode("let", tuple(binds), (self.expr(),), pos)
        for word in ("each", "if", "try", "type", "not", "error"):
            if self.is_word(word):
                return self.opaque(self.peek(), f"Power Query {word} expression")
        node = self.postfix()
        if self.is_word("meta"):
            self.next()
            record = self.postfix()
            node = MNode("meta", None, (node, record), node.pos)
        tok = self.peek()
        if (tok.kind == "op" and tok.text not in (",", ")", "]", "}")) or (
                tok.kind == "ident" and tok.text in ("and", "or", "as", "is")):
            # An operator expression. Nothing in a plain source needs one, so it
            # is kept opaque and refused - by the function it applies, if any -
            # only if the query's result actually depends on it.
            opaque = self.opaque(tok, f"Power Query operator {tok.text}")
            function = called_function(node)
            return MNode("opaque", function or opaque.value, pos=node.pos)
        return node

    def opaque(self, start: MTok, what: str) -> MNode:
        """Skip one expression this reader does not evaluate (to the next `,`
        or closing bracket at its own depth, or the `in` of its let) and keep
        the first function it calls, which is what a refusal names."""
        depth, function = 0, None
        while True:
            tok = self.peek()
            if tok.kind == "eof":
                break
            if depth == 0 and ((tok.kind == "op" and tok.text in (",", ")", "]", "}"))
                               or (tok.kind == "ident" and tok.text == "in")):
                break
            if tok.kind == "op" and tok.text in ("(", "[", "{"):
                depth += 1
            elif tok.kind == "op" and tok.text in (")", "]", "}"):
                depth -= 1
            elif (function is None and tok.kind == "ident" and "." in tok.text
                  and self.toks[self.i + 1].text == "("):
                function = tok.text
            self.next()
        return MNode("opaque", f"step {function}" if function else what, pos=start.pos)

    def postfix(self) -> MNode:
        node = self.primary()
        while True:
            if self.is_op("{"):
                pos = self.next().pos
                key = self.expr()
                self.expect("}")
                node = MNode("item", None, (node, key), pos)
            elif self.is_op("[") and self.field_access_ahead():
                pos = self.next().pos
                name = self.next()
                self.expect("]")
                node = MNode("field", name.value, (node,), pos)
            elif self.is_op("("):
                pos = self.next().pos
                args = []
                if not self.is_op(")"):
                    args.append(self.expr())
                    while self.is_op(","):
                        self.next()
                        args.append(self.expr())
                self.expect(")")
                node = MNode("call", None, (node, *args), pos)
            else:
                return node

    def field_access_ahead(self) -> bool:
        a, b = self.toks[self.i + 1], self.toks[self.i + 2]
        return a.kind == "ident" and b.kind == "op" and b.text == "]"

    def primary(self) -> MNode:
        tok = self.next()
        if tok.kind == "str":
            return MNode("str", tok.value, pos=tok.pos)
        if tok.kind == "num":
            return MNode("num", tok.value, pos=tok.pos)
        if tok.kind == "ident":
            if tok.text == "null":
                return MNode("null", None, pos=tok.pos)
            if tok.text in ("true", "false"):
                return MNode("bool", tok.text == "true", pos=tok.pos)
            return MNode("ident", tok.value, pos=tok.pos)
        if tok.kind == "op" and tok.text == "(":
            node = self.expr()
            self.expect(")")
            return node
        if tok.kind == "op" and tok.text == "[":
            fields = []
            if not self.is_op("]"):
                while True:
                    name = self.next()
                    if name.kind != "ident":
                        raise self.error()
                    self.expect("=")
                    fields.append((name.value, self.expr()))
                    if self.is_op(","):
                        self.next()
                        continue
                    break
            self.expect("]")
            return MNode("record", tuple(fields), pos=tok.pos)
        if tok.kind == "op" and tok.text == "{":
            items = []
            if not self.is_op("}"):
                items.append(self.expr())
                while self.is_op(","):
                    self.next()
                    items.append(self.expr())
            self.expect("}")
            return MNode("list", None, tuple(items), tok.pos)
        self.i -= 1
        raise self.error()


# ======================================================================
# Reading: symbolic evaluation
# ======================================================================
@dataclass(frozen=True)
class Nav:
    """A navigation value: a connector, a database, a schema, a relation, or a
    native query."""
    level: str                 # connector database schema table native sql_database
    connector: str
    schema: str | None = None
    name: str | None = None
    sql: str | None = None


CONNECTOR_FUNCTIONS = {
    "Databricks.Catalogs": "databricks",
    "DatabricksMultiCloud.Catalogs": "databricks",
    "Snowflake.Databases": "snowflake",
}


KNOWN_FUNCTIONS = frozenset({*CONNECTOR_FUNCTIONS, "Sql.Database", "Odbc.DataSource",
                             "Value.NativeQuery"})


class Evaluator:
    def __init__(self, shared: dict[str, str]):
        self.shared_text = shared
        self.shared: dict[str, Any] = {}
        self.busy: set[str] = set()

    def shared_value(self, name: str) -> Any:
        if name in self.shared:
            return self.shared[name]
        if name in self.busy:
            raise ModelError(f"Power Query: shared expression {name!r} refers to itself")
        self.busy.add(name)
        try:
            value = self.evaluate(MParser(self.shared_text[name]).parse_all(), {}, name)
        finally:
            self.busy.discard(name)
        self.shared[name] = value
        return value

    def evaluate(self, node: MNode, env: dict, step: str) -> Any:
        kind = node.kind
        if kind in ("str", "num", "bool"):
            return node.value
        if kind == "null":
            return None
        if kind == "meta":
            return self.evaluate(node.args[0], env, step)
        if kind == "opaque":
            what = node.value
            feature = f"Power Query {what}" if what.startswith("step ") else what
            raise unsupported(feature, f"step {step}")
        if kind == "record":
            return {name: self.evaluate(value, env, step) for name, value in node.value}
        if kind == "list":
            return [self.evaluate(item, env, step) for item in node.args]
        if kind == "ident":
            name = node.value
            if name in env:
                return env[name]()
            if name in self.shared_text:
                return self.shared_value(name)
            raise ModelError(f"Power Query: The name '{name}' wasn't recognized.")
        if kind == "let":
            scope = dict(env)
            cache: dict[str, Any] = {}
            busy: set[str] = set()

            def thunk(name, value):
                def run():
                    if name in cache:
                        return cache[name]
                    if name in busy:
                        raise ModelError(f"Power Query: step {name!r} refers to itself")
                    busy.add(name)
                    cache[name] = self.evaluate(value, scope, name)
                    return cache[name]
                return run

            for name, value, _pos in node.value:
                scope[name] = thunk(name, value)
            return self.evaluate(node.args[0], scope, step)
        if kind == "call":
            return self.call(node, env, step)
        if kind == "item":
            return self.item(node, env, step)
        if kind == "field":
            target = self.evaluate(node.args[0], env, step)
            if isinstance(target, tuple) and target[0] == "row" and node.value == "Data":
                return target[1]
            raise unsupported("Power Query field access", f"[{node.value}] in step {step}")
        raise ModelError(f"Power Query: unexpected {kind}")    # pragma: no cover

    def call(self, node: MNode, env: dict, step: str) -> Any:
        head = node.args[0]
        if head.kind != "ident":
            raise unsupported("Power Query function value", f"in step {step}")
        function = head.value
        if function not in KNOWN_FUNCTIONS:
            # Named before its arguments are looked at: the outermost function
            # of the step is the one an engineer reads (Csv.Document, not the
            # Web.Contents inside it).
            raise unsupported(f"Power Query step {function}", f"step {step}")
        args = [self.evaluate(a, env, step) for a in node.args[1:]]
        if function in CONNECTOR_FUNCTIONS:
            return Nav("connector", CONNECTOR_FUNCTIONS[function])
        if function == "Odbc.DataSource":
            options = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
            if options.get("HierarchicalNavigation") is not True:
                # The flat navigation (module docstring, DUCKDB): refused by
                # the function's name, as any step this reader cannot follow.
                raise unsupported("Power Query step Odbc.DataSource",
                                  f"step {step}: only [HierarchicalNavigation=true] "
                                  f"navigates by schema")
            return Nav("connector", "odbc")
        if function == "Sql.Database":
            options = args[2] if len(args) > 2 and isinstance(args[2], dict) else {}
            if options.get("Query"):
                return Nav("native", "sql", sql=options["Query"])
            return Nav("sql_database", "sql")
        if function == "Value.NativeQuery":
            if not args or not isinstance(args[0], Nav) or args[0].level not in (
                    "database", "sql_database"):
                raise unsupported("Value.NativeQuery over anything but a database",
                                  f"in step {step}")
            if len(args) < 2 or not isinstance(args[1], str):
                raise ModelError(f"Power Query: Value.NativeQuery needs query text "
                                 f"(step {step})")
            if len(args) > 2 and args[2] not in (None, {}):
                raise unsupported("Value.NativeQuery parameters", f"in step {step}")
            return Nav("native", args[0].connector, sql=args[1])
        raise unsupported(f"Power Query step {function}", f"step {step}")  # pragma: no cover

    def item(self, node: MNode, env: dict, step: str) -> Any:
        target = self.evaluate(node.args[0], env, step)
        key = self.evaluate(node.args[1], env, step)
        if not isinstance(target, Nav) or not isinstance(key, dict):
            raise unsupported("Power Query item access", f"in step {step}")
        if target.level == "sql_database" and set(key) == {"Schema", "Item"}:
            return ("row", Nav("table", "sql", schema=key["Schema"], name=key["Item"]))
        kinds = {"connector": "Database", "database": "Schema", "schema": ("Table", "View")}
        expected = kinds.get(target.level)
        if expected is None or set(key) != {"Name", "Kind"} or not isinstance(key["Name"], str):
            raise unsupported("Power Query navigation", f"{key!r} in step {step}")
        kind = key["Kind"]
        if kind not in ((expected,) if isinstance(expected, str) else expected):
            raise unsupported("Power Query navigation", f"Kind={kind!r} in step {step}")
        name = key["Name"]
        if target.level == "connector":
            return ("row", Nav("database", target.connector, name=name))
        if target.level == "database":
            return ("row", Nav("schema", target.connector, name=name))
        return ("row", Nav("table", target.connector, schema=target.name, name=name))


def parse_partition(text: str, shared: dict[str, str]) -> tuple[str | None, str | None]:
    """An M partition source -> (source "schema.relation", source_sql)."""
    value = Evaluator(shared).evaluate(MParser(text).parse_all(), {}, "Source")
    if isinstance(value, Nav) and value.level == "table":
        schema, name = value.schema, value.name
        if value.connector == "snowflake":
            schema, name = schema.lower(), name.lower()
        return f"{schema}.{name}", None
    if isinstance(value, Nav) and value.level == "native":
        return None, value.sql
    raise unsupported("Power Query query that does not return a warehouse relation")


def parameter_value(text: str) -> Any:
    """The value of a parameter expression (`"x" meta [IsParameterQuery=true]`)."""
    node = MParser(text).parse_all()
    return Evaluator({}).evaluate(node, {}, "parameter")


__all__ = ["CONNECTORS", "DEFAULT_CONNECTOR", "parameter_expressions", "partition_source",
           "parse_partition"]
