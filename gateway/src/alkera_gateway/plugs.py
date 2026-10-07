"""Environment profiles: which tools a box has (docs/environments/TOOL_INVENTORY.md).

An environment is a set of SLOTS (warehouse, notebook, BI, ...). A PLUG is one
concrete local implementation of a slot (fakesnow for the warehouse, the
Metabase-shaped fake for BI). A profile is `<role>/<stack>`: the role decides
which slots the box has (a data analyst gets no scheduler), the stack decides
which plug fills each one (a Snowflake shop's warehouse is Snowflake), and a
task may override any slot - name other plugs, add a slot the role lacks, or
remove one with an empty list.

Every tool on the 2026-09-30 meeting list is a plug here, with its honest build
status. `planned` plugs resolve only with `allow_planned=True`: a task may be
authored ahead of its tools, but nothing serves a tool that does not exist.

This module imports nothing from the gateway or the world; the gateway config,
the native services and the harness's task loader read it.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

SLOTS = ("files", "notebook", "compute", "warehouse", "transform", "orchestrator",
         "object_store", "ingest", "bi", "observability", "release", "human")

# The MCP tools each slot owns. Together they are exactly `tools.TOOLS`
# (tests/test_plugs.py); a tool no slot owns (the runtime `bash`) is never
# filtered out by a profile.
SLOT_TOOLS: dict[str, tuple[str, ...]] = {
    "files": ("fs_read", "fs_write", "fs_list", "git_log", "git_diff", "git_commit",
              "docs_search", "docs_list", "docs_get"),
    "notebook": (),
    "compute": (),
    "warehouse": ("sql_query", "sql_execute"),
    "transform": ("dbt_run", "dbt_test", "dbt_compile"),
    "orchestrator": ("sched_list", "sched_trigger", "sched_state", "sched_logs"),
    "object_store": ("s3_list", "s3_get", "s3_put", "s3_delete"),
    "ingest": ("ingest_list", "ingest_get", "ingest_runs", "ingest_run", "ingest_schema",
               "ingest_alerts", "ingest_trigger", "ingest_schema_update"),
    "bi": ("bi_render", "bi_list", "bi_get", "bi_cached", "bi_update", "bi_refresh"),
    "observability": ("metrics_list", "metrics_get", "metrics_series", "alarms_list",
                      "alarms_get", "alarms_history", "logs_list", "logs_get",
                      "logs_search"),
    "release": ("deploy_list", "deploy_run", "deploy_history"),
    "human": ("issues_list", "issues_get", "issues_search", "issues_comment",
              "issues_update", "chat_channels", "chat_history", "chat_thread",
              "chat_search", "chat_post", "kb_list", "kb_get", "kb_search", "kb_edit",
              "kb_create"),
}

Status = Literal["shipped", "partial", "planned"]


@dataclass(frozen=True)
class Plug:
    name: str
    slot: str
    status: Status
    services: tuple[str, ...] = ()   # native/services modules this plug mounts
    surfaces: tuple[str, ...] = ()   # task.toml [environment].surfaces labels it provides
    note: str = ""


_CATALOG = (
    Plug("workspace", "files", "shipped", surfaces=("git", "docs"),
         note="the world's git repo, landing files and docs"),
    Plug("jupyter", "notebook", "partial", surfaces=("notebook",),
         note="audited kernel (native/notebook.py) + jupyter-server launcher; "
              "compose service gated behind the `notebook` profile; not yet run "
              "in a container"),
    Plug("ml_workers", "compute", "planned", surfaces=("compute",)),
    Plug("mlflow", "compute", "planned", surfaces=("mlflow",)),
    Plug("duckdb", "warehouse", "shipped",
         surfaces=("duckdb", "duckdb_legacy", "streaming"),
         note="GT002's `streaming` surface is tables in the DuckDB file"),
    Plug("postgres", "warehouse", "partial", surfaces=("postgres",),
         note="real server at postgres.internal:5432 in every native stack; its "
              "role-scoped statement log is folded into the action log at "
              "teardown (psql.statement, one row per statement); seeded with "
              "the world's warehouse.* only when a profile plugs it; psql, psycopg "
              "and dbt-postgres run live against it in a container "
              "(tasks/harness/evidence/postgres_e2e.json); rows land at teardown, "
              "not per call; connections are not counted in destinations.jsonl; "
              "no task uses it"),
    Plug("snowflake", "warehouse", "partial", surfaces=("snowflake",),
         note="fakesnow 0.11.18 behind a TLS proxy; sql_* and dbt-snowflake verified "
              "against it in-process; clients only in images built with "
              "--warehouse-clients snowflake; TLS route untested; no task uses it"),
    Plug("redshift", "warehouse", "planned", surfaces=("redshift",)),
    Plug("bigquery", "warehouse", "partial", surfaces=("bigquery",),
         note="goccy emulator v0.8.1 behind an audited proxy at "
              "bigquery.internal:9050 (native/services/bigquery.py), seeded from "
              "the world; google-cloud-bigquery in images built with "
              "--warehouse-clients bigquery; cost not modelled; transactions, "
              "ALTER, EXECUTE IMMEDIATE, ASSERT, WRITE_TRUNCATE, schema patches, "
              "load jobs and legacy SQL refused by name; INT64 overflow wraps "
              "instead of erroring (emulator, not fenced); no dbt-bigquery (works "
              "against the proxy, but its pandas/nbformat break the de image's "
              "role gate); container e2e only, arm64 not run, no task uses it"),
    Plug("databricks", "warehouse", "planned", surfaces=("databricks",)),
    Plug("dbt", "transform", "shipped", surfaces=("dbt",)),
    Plug("ops_sched", "orchestrator", "shipped",
         surfaces=("scheduler", "orchestration_secondary"),
         note="GT002's `orchestration_secondary` is this same scheduler"),
    Plug("airflow", "orchestrator", "partial", surfaces=("airflow",),
         note="Airflow 3 REST v2 fake over the world ledger, not real Airflow: DAGs "
              "parsed (ast) from the workspace's dags/, runs and logs from "
              "var/sched/state.json; trigger and clear queue for the grader's advance "
              "(no clock, no backfill, no pause); airflowctl, no airflow CLI; at "
              "airflow.internal:8100 (backends.BACKENDS)"),
    Plug("s3mock", "object_store", "shipped", surfaces=("s3",)),
    Plug("airbyte_fake", "ingest", "shipped", services=("airbyte",), surfaces=("ingest",)),
    Plug("metabase_fake", "bi", "shipped", services=("metabase",), surfaces=("bi",)),
    # The three BI vendors are EXPLICIT-ONLY (native/services PLUG_NAMES): an
    # unprofiled stack never serves them, a profile that plugs one serves it in
    # place of metabase. Each is the vendor's files-in-git format lowered onto
    # world.semantic, so the service and the graders evaluate one definition.
    Plug("powerbi", "bi", "partial", services=("powerbi",), surfaces=("powerbi",),
         note="PBIP/TMDL/PBIR project + DAX subset over world.semantic; Power BI "
              "REST, Fabric item definitions and Entra client credentials at "
              "powerbi.internal:8095; no golden calibration against the real "
              "product yet; not yet run in a container"),
    Plug("tableau", "bi", "partial", services=("tableau",), surfaces=("tableau",),
         note=".twb/.tds + calculated-field subset over world.semantic; REST API "
              "3.25 with PAT sign-in, extracts as DuckDB copies, at "
              "tableau.internal:8096; no golden calibration against the real "
              "product yet; not yet run in a container"),
    Plug("sigma", "bi", "partial", services=("sigma",), surfaces=("sigma",),
         note="workbooks- and data-models-as-code JSON + formula subset over "
              "world.semantic; REST API v2 with client credentials at "
              "sigma.internal:8097; no golden calibration against the real "
              "product yet; not yet run in a container"),
    Plug("prom_stack", "observability", "shipped",
         services=("prometheus", "alertmanager", "loki"), surfaces=("obs", "observability")),
    Plug("deploy_fake", "release", "shipped", services=("deploy",), surfaces=("release",)),
    Plug("workflow", "human", "shipped", services=("jira", "slack", "confluence"),
         surfaces=("workflow",)),
)
PLUGS: dict[str, Plug] = {p.name: p for p in _CATALOG}

ROLES: dict[str, frozenset[str]] = {
    "de": frozenset({"files", "warehouse", "transform", "orchestrator", "object_store",
                     "ingest", "bi", "observability", "release", "human"}),
    "ds": frozenset({"files", "notebook", "compute", "warehouse", "transform",
                     "orchestrator", "object_store", "human"}),
    "da": frozenset({"files", "notebook", "warehouse", "transform", "bi", "human"}),
}

_LAPTOP: dict[str, tuple[str, ...]] = {
    "files": ("workspace",), "notebook": ("jupyter",), "compute": ("ml_workers", "mlflow"),
    "warehouse": ("duckdb",), "transform": ("dbt",), "orchestrator": ("ops_sched",),
    "object_store": ("s3mock",), "ingest": ("airbyte_fake",), "bi": ("metabase_fake",),
    "observability": ("prom_stack",), "release": ("deploy_fake",), "human": ("workflow",),
}
STACKS: dict[str, dict[str, tuple[str, ...]]] = {
    "laptop": _LAPTOP,
    "postgres": {**_LAPTOP, "warehouse": ("postgres",)},
    "snowflake": {**_LAPTOP, "warehouse": ("snowflake",), "bi": ("sigma",)},
    "databricks": {**_LAPTOP, "warehouse": ("databricks",), "bi": ("powerbi",)},
    "aws": {**_LAPTOP, "warehouse": ("redshift",), "bi": ("tableau",)},
    "gcp": {**_LAPTOP, "warehouse": ("bigquery",)},
}


#: Slots whose MCP tools speak to ONE plug rather than to the slot: bi_* render
#: the YAML-of-SQL dashboards through the Metabase-shaped fake, so a box whose BI
#: is Power BI, Tableau or Sigma (their own REST APIs and project files) does not
#: offer them - serving them there would be a map to a system the box lacks.
TOOLS_NEED_PLUG = {"bi": "metabase_fake"}


def _serves_slot_tools(slot: str, plugged: tuple[str, ...]) -> bool:
    need = TOOLS_NEED_PLUG.get(slot)
    return need is None or need in plugged


class ProfileError(ValueError):
    """A profile that cannot be served, with every reason at once."""


@dataclass(frozen=True)
class Profile:
    name: str
    slots: dict[str, tuple[str, ...]]   # plugged slots only; never an empty tuple
    tools: frozenset[str]
    excluded_tools: frozenset[str]
    services: tuple[str, ...]           # catalog order
    surfaces: frozenset[str]
    planned: tuple[str, ...]


def _as_plugs(value: Sequence[str] | str) -> tuple[str, ...]:
    return (value,) if isinstance(value, str) else tuple(value)


def resolve(name: str, plugs: Mapping[str, Sequence[str] | str] | None = None,
            *, allow_planned: bool = False) -> Profile:
    role, _, stack = name.partition("/")
    problems = []
    if role not in ROLES:
        problems.append(f"unknown role {role!r} (known: {', '.join(ROLES)})")
    if stack not in STACKS:
        problems.append(f"unknown stack {stack!r} (known: {', '.join(STACKS)})")
    if problems:
        raise ProfileError(f"profile {name!r}: " + "; ".join(problems))

    slots = {s: STACKS[stack][s] for s in SLOTS if s in ROLES[role]}
    for slot, value in (plugs or {}).items():
        if slot not in SLOTS:
            problems.append(f"unknown slot {slot!r} (known: {', '.join(SLOTS)})")
            continue
        chosen = _as_plugs(value)
        for plug in chosen:
            if plug not in PLUGS:
                problems.append(f"unknown plug {plug!r} for slot {slot!r}")
            elif PLUGS[plug].slot != slot:
                problems.append(f"plug {plug!r} belongs to slot "
                                f"{PLUGS[plug].slot!r}, not {slot!r}")
        if chosen:
            slots[slot] = chosen
        else:
            slots.pop(slot, None)
    if problems:
        raise ProfileError(f"profile {name!r}: " + "; ".join(problems))

    plugged = [PLUGS[p] for s in SLOTS if s in slots for p in slots[s]]
    planned = tuple(p.name for p in plugged if p.status == "planned")
    if planned and not allow_planned:
        raise ProfileError(f"profile {name!r} needs plugs that are planned, not built: "
                           f"{', '.join(planned)}")
    order = {p: i for i, p in enumerate(PLUGS)}
    in_order = sorted(plugged, key=lambda p: order[p.name])
    return Profile(
        name=name,
        slots={s: slots[s] for s in SLOTS if s in slots},
        tools=frozenset(t for s in slots if _serves_slot_tools(s, slots[s])
                        for t in SLOT_TOOLS[s]),
        excluded_tools=frozenset(t for s in SLOTS
                                 if s not in slots or not _serves_slot_tools(s, slots[s])
                                 for t in SLOT_TOOLS[s]),
        services=tuple(svc for p in in_order for svc in p.services),
        surfaces=frozenset(x for p in plugged for x in p.surfaces),
        planned=planned,
    )


def parse_plug_args(values: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """`["notebook=", "warehouse=duckdb,postgres"]` -> {slot: plugs}; empty removes."""
    out = {}
    for value in values:
        slot, sep, rest = value.partition("=")
        if not sep or not slot:
            raise ProfileError(f"--plug {value!r} is not SLOT=PLUG[,PLUG] (or SLOT= to remove)")
        out[slot] = tuple(p for p in rest.split(",") if p)
    return out


def readiness() -> list[dict]:
    rows = []
    for role in ROLES:
        for stack in STACKS:
            planned = list(resolve(f"{role}/{stack}", allow_planned=True).planned)
            rows.append({"profile": f"{role}/{stack}",
                         "status": "planned" if planned else "runnable",
                         "planned": planned})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alkera-gateway plugs")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("matrix", help="every role/stack profile and whether it can run today")
    show = sub.add_parser("show", help="what one profile plugs in")
    show.add_argument("profile")
    show.add_argument("--plug", action="append", default=[], metavar="SLOT=PLUG[,PLUG]")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "matrix":
            for row in readiness():
                tail = f": {', '.join(row['planned'])}" if row["planned"] else ""
                print(f"{row['profile']:<16} {row['status']}{tail}")
            return 0
        p = resolve(args.profile, parse_plug_args(args.plug))
    except ProfileError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"profile   {p.name}")
    for slot, names in p.slots.items():
        statuses = ", ".join(f"{n} ({PLUGS[n].status})" for n in names)
        print(f"  {slot:<14} {statuses}")
    print(f"tools     {len(p.tools)} served, {len(p.excluded_tools)} excluded")
    print(f"services  {', '.join(p.services) or '-'}")
    print(f"surfaces  {', '.join(sorted(p.surfaces))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
