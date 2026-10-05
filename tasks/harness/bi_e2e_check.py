#!/usr/bin/env python3
"""Are the native tool calls ACCURATE, end to end, on a real BI world?

    cd gateway && uv run --with tabcmd --with tableauserverclient \\
        python ../tasks/harness/bi_e2e_check.py --topology host
    sg docker -c "cd gateway && uv run --with tabcmd --with tableauserverclient \\
        python ../tasks/harness/bi_e2e_check.py --topology container"

WHAT IT PROVES. In native mode the agent has bash and nothing else, and every
action it takes against the world must leave exactly one agent.md 9.2.2 row,
written by the fake service it talked to or by the PATH shim in front of the
binary it ran (gateway/NATIVE.md, "The one thing that must survive"). This check
builds a real `commerce_analytics` world for the `da/laptop` profile with Power
BI, Tableau and Sigma plugged (`world.build`, under ~/alkera-e2e/),
brings the native stack up the way the harness does - `native_stack.NativeStack`
on the host, `container_episode.NativeContainerEpisode` in docker - and drives
every tool an analyst or analytics engineer would reach for, with the real
clients, from a shell holding the episode's own environment:

    warehouse  the `duckdb` CLI (shim), python/python3 + the duckdb library
    dbt        dbt ls / test -s / build -s
    scheduler  ./ops/sched list | logs | trigger   (this profile plugs NO
               orchestrator, so the CLI is not in the repo: each must fail
               and leave no row)
    human      jira (python `jira`), slack (slack_sdk), confluence
               (atlassian-python-api): one read and one write each, and the
               wiki page by the id docs/onboarding.md lists; Jira REST v2
               (`rest_api_version: 2`) and Confluence REST v2 (`/wiki/api/v2`)
               reads, the versions the BI001 run reached for first
    Power BI   Entra token, groups/datasets, executeQueries, getDefinition,
               updateDefinition with an edited measure, refresh + history
    Tableau    tabcmd 2 (login, publish .tds/.twb, refreshextracts, export
               --csv) and tableauserverclient
    Sigma      token, workbooks, ?includeContents, PUT /contents, export +
               download, connection sync, an element's query / columns and the
               workbook's sources
    Jupyter    `jupyter execute` (nbclient) on the alkera-python kernel:
               pandas over a parquet export, duckdb over the warehouse, a file
    unrouted   one request per HTTP service for a path it does not serve, and
               the guesses the BI001 run made that the real products do not
               serve either (they must stay 404)

For EACH action it verifies, from the files the stack wrote:

  (a) rows      the expected rows appeared - normally one per request or
                binary, and the documented second rows (the python shim's row
                beside the service rows of a python client) are expected, not
                tolerated
  (b) schema    every row has the 23 9.2.2 keys, valid types, matching hashes,
                exit/error agreement, a contiguous seq and the world's vts
  (c) class     tool, op_class, targets (and read_targets where the answer is
                known) are right for what was done
  (d) network   destinations.jsonl counted a connection to exactly the
                services the action talked to, and to no other
  (e) values    what the tool returned equals an independent direct DuckDB
                query of the world's warehouse (or the world's fixture file)
  (f) world     every file that changed under the world root is explained by
                that action's rows (`blindness._unattributed`, the harness's own
                rule, plus: a working-tree change no row names is a failure)

and prints an accuracy table. Exit 1 on any FAIL. A row whose resolution is
coarser BY DESIGN (the python shim names files, not tables) is `DOC`, with the
reason; a finding that is not an accuracy failure (a surface the profile
excludes being reachable, a client the toolbelt does not import) is `FINDING`.

Evidence: tasks/harness/evidence/bi_e2e_<topology>.json (`--out`).
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

# --------------------------------------------------------------------------
# the world and the profile
# --------------------------------------------------------------------------
PROFILE_NAME = "da/laptop"
PROFILE_SLOTS = ("files", "notebook", "warehouse", "transform", "bi", "human")
PLUGS = {"bi": ["powerbi", "tableau", "sigma"]}
SEED_EXAMPLE = REPO / "world" / "world" / "themes" / "commerce_analytics" / "seed.example.yaml"
WORK_ROOT = Path.home() / "alkera-e2e"
DEFAULT_IMAGE = "alkera-episode:head-da"
EVIDENCE = HERE / "evidence"
#: Services this profile serves over HTTP, and the ones it must not.
PLUGGED_HTTP = ("jira", "slack", "confluence", "powerbi", "tableau", "sigma")
EXCLUDED_HTTP = ("metabase", "airbyte", "deploy", "prometheus", "alertmanager", "loki",
                 "sched", "minio")
#: The working-tree directories `shims/run.py` deliberately does not snapshot
#: ("dbt's own scratch output", bytecode caches): churn there is named by no
#: row BY DESIGN, and the transcript audits the working tree in native mode.
SKIP_DIRECTORIES = frozenset({".git", ".venv", "venv", "__pycache__", ".pytest_cache",
                              "target", "logs"})
RESULT_PREFIX = "E2E-RESULT "
JIRA_KEY = "ANA-239"
SLACK_CHANNEL = "analytics-eng"
KB_TITLE = "Net Revenue and friends"
KB_SLUG = "ae/metrics/net-revenue"
SCRATCH_TABLE = "main.e2e_scratch"
#: No trailing newline: the engineer passes it as "$(cat file)", which strips one.
KB_BODY = "# Net Revenue and friends\n\nEdited by the e2e accuracy check."


def seed_text(example: str) -> str:
    """The example seed plus the three opt-in keys this check needs.

    `files:` asks for the parquet exports the notebook reads (the `files` slot
    is in the profile); `native: true` writes the discovery layer; `profile:`
    plugs the three BI vendors, which is what makes the build write their
    projects into the workspace (world/world/bi_projects.py)."""
    return (example.rstrip("\n") + "\n"
            "native: true\n"
            "files: {formats: [parquet]}\n"
            "profile:\n"
            f"  name: {PROFILE_NAME}\n"
            f"  slots: [{', '.join(PROFILE_SLOTS)}]\n"
            f"  plugs: {{bi: [{', '.join(PLUGS['bi'])}]}}\n")


def manifest(root: Path, grading: Path) -> dict:
    """The world manifest the harness passes around, for a `world.build` tree."""
    return {"world_root": str(root), "workspace": str(root / "workspace"),
            "warehouse": str(root / "var" / "warehouse" / "warehouse.duckdb"),
            "scheduler": str(root / "var" / "sched" / "state.json"),
            "dashboards": str(root / "var" / "bi" / "dashboards"),
            "profiles_dir": str(root / "build"),
            "workflow_dir": str(root / "var" / "workflow"),
            "grading": str(grading)}


def build_world(work: Path) -> dict:
    """A fresh world under `work/world`, built by world.build with this profile."""
    import episode

    root, grading = work / "world", work / "grading"
    shutil.rmtree(root, ignore_errors=True)
    grading.mkdir(parents=True, exist_ok=True)
    seed = work / "seed.yaml"
    seed.write_text(seed_text(SEED_EXAMPLE.read_text(encoding="utf-8")), encoding="utf-8")
    done = subprocess.run([str(episode.PY), "-m", "world.build", "--seed", str(seed),
                           "--root", str(root)], capture_output=True, text=True,
                          env=episode.venv_env())
    if done.returncode != 0:
        raise SystemExit(f"world.build failed:\n{done.stdout}\n{done.stderr}")
    return manifest(root, grading)


# --------------------------------------------------------------------------
# pure checks
# --------------------------------------------------------------------------
def validate_row(row: dict, *, vnow: str | tuple[str, ...] | None = None) -> list[str]:
    """Problems with one row against agent.md 9.2.2 as `native.audit` writes it.

    `vnow` is the world clock the row must carry - one value, or the values it
    held during the action (a manual trigger moves `vnow` by the run's
    duration, and the rows written after it carry the new time)."""
    from alkera_gateway.native.audit import EXITS, OP_CLASSES, ROW_KEYS, SUMMARY_CHARS
    from alkera_gateway.records import canonical, digest

    problems = []
    keys = set(row)
    if keys != set(ROW_KEYS):
        problems.append(f"keys: missing {sorted(set(ROW_KEYS) - keys)} "
                        f"extra {sorted(keys - set(ROW_KEYS))}")
        return problems

    def kind(name, types, nullable=False):
        value = row[name]
        if value is None and nullable:
            return
        if isinstance(value, bool) or not isinstance(value, types):
            problems.append(f"{name}: {type(value).__name__} {value!r:.60}")

    kind("seq", int)
    kind("wts", int)
    kind("vts", str)
    kind("tool", str)
    kind("transport", str)
    kind("args", dict)
    kind("args_hash", str)
    kind("args_summary", str)
    kind("op_class", str)
    kind("targets", list)
    kind("read_targets", list)
    kind("flags", list)
    kind("result_hash", str)
    kind("result_bytes", int)
    kind("latency_ms", (int, float))
    kind("rss_mb", (int, float))
    kind("disk_mb", (int, float), nullable=True)
    kind("rows_affected", int, nullable=True)
    kind("target", str, nullable=True)
    kind("schema_version", int)
    kind("exit", str)
    if problems:
        return problems
    if row["schema_version"] != 1:
        problems.append(f"schema_version {row['schema_version']}")
    if row["seq"] < 1:
        problems.append(f"seq {row['seq']}")
    if row["transport"] != "native":
        problems.append(f"transport {row['transport']!r}")
    if row["op_class"] not in OP_CLASSES:
        problems.append(f"op_class {row['op_class']!r}")
    if row["exit"] not in EXITS:
        problems.append(f"exit {row['exit']!r}")
    if (row["exit"] == "error") != (row["error"] is not None):
        problems.append(f"exit {row['exit']} with error {row['error']!r}")
    if row["error"] is not None and (not isinstance(row["error"], dict)
                                     or set(row["error"]) != {"code", "message"}):
        problems.append(f"error shape {row['error']!r}")
    if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", row["tool"]):
        problems.append(f"tool {row['tool']!r} is not <service>.<operation>")
    for name in ("targets", "read_targets", "flags"):
        if not all(isinstance(item, str) for item in row[name]):
            problems.append(f"{name} holds a non-string")
    if row["target"] != (row["targets"][0] if row["targets"] else None):
        problems.append(f"target {row['target']!r} is not targets[0]")
    if row["args_hash"] != digest(row["args"]):
        problems.append("args_hash does not match args")
    if row["args_summary"] != canonical(row["args"]).decode("utf-8")[:SUMMARY_CHARS]:
        problems.append("args_summary is not canonical(args)[:240]")
    if row["result_hash"] != digest(row["result"]):
        problems.append("result_hash does not match result")
    if row["result_bytes"] != len(canonical(row["result"])):
        problems.append("result_bytes does not match result")
    if row["latency_ms"] < 0:
        problems.append("negative latency")
    clocks = (vnow,) if isinstance(vnow, str) else tuple(vnow or ())
    if clocks and row["vts"] not in clocks:
        problems.append(f"vts {row['vts']} is not the world's vnow {' / '.join(clocks)}")
    return problems


@dataclass
class Expect:
    """One row an action must leave, in order."""

    tool: str
    op_class: str = "read"
    #: exact list, a callable(targets, ctx) -> bool, or None (not checked)
    targets: Any = None
    read_targets: Any = None
    exit: str = "ok"
    note: str = ""

    def check(self, row: dict, ctx: dict) -> list[str]:
        problems = []
        if row.get("tool") != self.tool:
            return [f"tool {row.get('tool')} != {self.tool}"]
        if row.get("op_class") != self.op_class:
            problems.append(f"{self.tool}: op_class {row.get('op_class')} != {self.op_class}")
        if row.get("exit") != self.exit:
            problems.append(f"{self.tool}: exit {row.get('exit')} != {self.exit} "
                            f"({(row.get('error') or {}).get('message', '')[:120]})")
        for name, spec in (("targets", self.targets), ("read_targets", self.read_targets)):
            if spec is None:
                continue
            got = row.get(name)
            ok = spec(got, ctx) if callable(spec) else got == spec
            if not ok:
                problems.append(f"{self.tool}: {name} {got}")
        return problems


def match_rows(expected: list[Expect], rows: list[dict], ctx: dict) -> list[str]:
    """Order-sensitive: the rows are the expected rows, one for one."""
    got = [r.get("tool") for r in rows]
    want = [e.tool for e in expected]
    if got != want:
        return [f"rows {got} != expected {want}"]
    problems = []
    for exp, row in zip(expected, rows, strict=True):
        problems += exp.check(row, ctx)
    return problems


def destination_delta(before: list[dict] | None, after: list[dict] | None) -> dict:
    """{(host, port): connections accepted between the two reads}."""
    def table(rows):
        return {(r.get("host"), int(r.get("port", 0))): int(r.get("count") or 0)
                for r in rows or []}
    old, new = table(before), table(after)
    return {key: new[key] - old.get(key, 0) for key in new
            if new[key] - old.get(key, 0)}


def changed_paths(before: dict, after: dict) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def listed_wiki_id(onboarding: str, title: str) -> str | None:
    """The id an engineer copies off docs/onboarding.md for the wiki page
    `title`: the first backticked token on that page's line (a profiled box
    lists the Confluence content id, with the path in brackets beside it)."""
    for match in re.finditer(r"^\s*- `([^`]+)` - (.*)$", onboarding, re.M):
        if match.group(2) == title or match.group(2).startswith(title + " ("):
            return match.group(1)
    return None


def in_skip_dir(rel_in_workspace: str) -> bool:
    return any(part in SKIP_DIRECTORIES for part in Path(rel_in_workspace).parts[:-1])


def explain_world(before: dict, after: dict, rows: list[dict], layout: dict,
                  container_paths: dict | None) -> dict:
    """(f): every change explained by the action's rows.

    Outside the working tree the harness's own rule decides
    (`blindness._unattributed`, HARD in native mode). Inside it the transcript
    is the auditor in native mode, but a shim or a cell row NAMES the files it
    changed - so a working-tree change no row names is a failure here, except
    in the directories the shims skip on purpose (dbt's target/ and logs/)."""
    import blindness

    changed = changed_paths(before, after)
    workspace = blindness.workspace_rel(layout)
    unexplained = blindness._unattributed(before, after, rows, layout, container_paths)
    outside = [p for p in unexplained if not (workspace and p.startswith(workspace + "/"))]
    # A file the action's own write/exec rows NAME is explained, whatever the
    # harness's attribution table knows about the tool that named it: the rule
    # under test is "no change without a row", and `blindness` is a second
    # opinion recorded beside it, so a writer it has not been taught (a
    # notebook cell naming the warehouse) shows up as a disagreement, not as a
    # missing row.
    root = Path(layout["world_root"]).resolve()
    prefixes = sorted((container_paths or {}).items(), key=lambda kv: -len(kv[0]))

    def world_rel(target: str) -> str | None:
        text = str(target).replace("\\", "/")
        for prefix, replacement in prefixes:
            if text.startswith(prefix):
                return f"{replacement}{text[len(prefix):]}".lstrip("/")
        if not text.startswith("/"):
            return None
        try:
            return Path(text).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            return None

    named_files = {world_rel(t) for r in rows
                   if r.get("op_class") in ("write", "ddl", "destructive", "exec")
                   for t in (r.get("targets") or [])} - {None}
    blindness_disagrees = [p for p in outside if p in named_files]
    outside = [p for p in outside if p not in named_files]
    named = {t for r in rows for t in (r.get("targets") or [])}
    # Only a shim or a notebook cell can name working-tree files; a service row
    # (or no row: `sed`, `curl`, `tabcmd`) cannot, and in native mode those
    # changes are the transcript's to audit, with rollout.py writing their
    # `workspace.edit` rows at the end of the episode (blindness, rule 6).
    file_namers = any(str(r.get("tool", "")).startswith(("shim.", "duckdb.", "jupyter."))
                      for r in rows)
    inside, skipped, transcript = [], [], []
    for path in changed:
        if not (workspace and path.startswith(workspace + "/")):
            continue
        rel = path[len(workspace) + 1:]
        if in_skip_dir(rel):
            skipped.append(path)
        elif rel in named:
            continue
        elif file_namers:
            inside.append(path)
        else:
            transcript.append(path)
    return {"changed": changed, "unexplained_outside_workspace": outside,
            "unnamed_in_workspace": inside, "skip_dir_churn": skipped,
            "transcript_audited": transcript,
            "named_by_a_row_but_unattributed_by_blindness": blindness_disagrees,
            "ok": not outside and not inside}


# -- values -----------------------------------------------------------------
def money(value) -> str:
    """Tableau's `$#,0.00`, half away from zero."""
    q = Decimal(str(value)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return f"{'-' if q < 0 else ''}${abs(q):,.2f}"


def pct(value) -> str:
    """Tableau's `0.0%`, half away from zero."""
    q = (Decimal(str(value)) * 100).quantize(Decimal("0.1"), ROUND_HALF_UP)
    return f"{q:.1f}%"


def integer(value) -> str:
    """Tableau's automatic integer format: thousands separators."""
    return f"{int(value):,}"


def same_number(a, b, places: int = 6) -> bool:
    try:
        return round(Decimal(str(a)), places) == round(Decimal(str(b)), places)
    except Exception:  # noqa: BLE001 - a non-number is simply not equal
        return False


def parse_result(stdout: str) -> dict | None:
    """The JSON object a client step printed after RESULT_PREFIX, if any."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_PREFIX):
            return json.loads(line[len(RESULT_PREFIX):])
    return None


def strip_catalog(relation: str, catalog: str = "warehouse") -> str:
    """`warehouse.warehouse.fct_orders` -> `warehouse.fct_orders`: the DuckDB
    file's own catalog (its stem) prefixed to a schema-qualified name."""
    parts = relation.split(".")
    return ".".join(parts[1:]) if len(parts) == 3 and parts[0] == catalog else relation


class Truth:
    """Independent answers, straight from the warehouse file and the fixtures."""

    def __init__(self, world: dict):
        self.world = world
        self.warehouse = Path(world["warehouse"])

    def q(self, sql: str) -> list[tuple]:
        import duckdb

        con = duckdb.connect(":memory:")
        try:
            con.execute(f"ATTACH '{self.warehouse.as_posix()}' AS wh (READ_ONLY)")
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def kpis(self) -> dict:
        net, n = self.q("SELECT SUM(order_amount), COUNT(*) FROM wh.warehouse.fct_orders "
                        "WHERE order_status = 'completed'")[0]
        return {"net": net, "completed": n, "aov": Decimal(net) / n}

    def gross_all(self) -> Decimal:
        return self.q("SELECT SUM(order_amount) FROM wh.warehouse.fct_orders")[0][0]

    def net_with_shipping(self) -> Decimal:
        a, b = self.q("SELECT SUM(order_amount), SUM(shipping_amount) FROM "
                      "wh.warehouse.fct_orders WHERE order_status = 'completed'")[0]
        return a + b

    def by_segment(self) -> list[tuple]:
        rows = self.q("SELECT c.segment, SUM(o.order_amount) FROM wh.warehouse.fct_orders o "
                      "JOIN wh.warehouse.dim_customer c USING (customer_id) "
                      "WHERE o.order_status = 'completed' GROUP BY 1 ORDER BY 2 DESC")
        total = sum(v for _, v in rows)
        return [(s, v, Decimal(v) / total) for s, v in rows]

    def by_month(self) -> list[tuple]:
        return self.q("SELECT d.year, d.month, SUM(o.order_amount) FROM "
                      "wh.warehouse.fct_orders o JOIN wh.warehouse.dim_date d "
                      "ON o.order_date = d.date_day WHERE o.order_status = 'completed' "
                      "GROUP BY 1, 2 ORDER BY 1, 2")

    def top_products(self) -> list[tuple]:
        return self.q("SELECT p.product_name, SUM(l.quantity) FROM "
                      "wh.warehouse.fct_order_lines l JOIN wh.warehouse.dim_product p "
                      "USING (product_id) GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 3")

    def region_2026(self) -> list[tuple]:
        return self.q("SELECT c.region, SUM(o.order_amount) FROM wh.warehouse.fct_orders o "
                      "JOIN wh.warehouse.dim_customer c USING (customer_id) "
                      "JOIN wh.warehouse.dim_date d ON o.order_date = d.date_day "
                      "WHERE o.order_status = 'completed' AND d.year = 2026 "
                      "GROUP BY 1 ORDER BY 1")

    def table_exists(self, schema: str, table: str) -> bool:
        return bool(self.q(f"SELECT 1 FROM information_schema.tables WHERE table_catalog "
                           f"= 'wh' AND table_schema = '{schema}' AND table_name = "
                           f"'{table}'"))

    def count(self, relation: str) -> int:
        return self.q(f"SELECT COUNT(*) FROM wh.{relation}")[0][0]

    def fixture(self, name: str) -> dict:
        return json.loads((Path(self.world["workflow_dir"]) / name).read_text("utf-8"))

    def issue(self, key: str) -> dict:
        return next(i for i in self.fixture("issues.json")["issues"]
                    if (i.get("id") or i.get("key")) == key)

    def channel(self, name: str) -> dict:
        return next(c for c in self.fixture("chat.json")["channels"] if c["id"] == name)

    def page(self, page_id: str) -> dict:
        return next(p for p in self.fixture("kb.json")["pages"] if p["id"] == page_id)


# --------------------------------------------------------------------------
# the client steps, run INSIDE the episode by its own `python`
# --------------------------------------------------------------------------
CLIENTS_PY = r'''"""The engineer's scripts: one step per invocation, real client libraries.

Each step prints one line `E2E-RESULT <json>` the checker parses. Credentials are
read the way an engineer reads them: the environment, else the repo's .envrc.
"""
import base64, json, os, re, sys

E = os.environ
PREFIX = "E2E-RESULT "


def out(obj):
    print(PREFIX + json.dumps(obj, sort_keys=True, default=str), flush=True)


def cred(name, default="alkera-local-dev"):
    if E.get(name):
        return E[name]
    try:
        text = open(os.path.join(E.get("ALKERA_WORKSPACE", "."), ".envrc")).read()
        found = re.findall(r'export ' + name + r'="([^"]*)"', text)
        if found:
            return found[-1]
    except OSError:
        pass
    return default


# ---------------------------------------------------------------- warehouse
def py_select(db):
    import duckdb
    con = duckdb.connect(db, read_only=True)
    net, n = con.execute("SELECT SUM(order_amount), COUNT(*) FROM warehouse.warehouse."
                         "fct_orders WHERE order_status = 'completed'").fetchone()
    con.close()
    out({"net": str(net), "completed": n})


def py_insert(db, table, value):
    import duckdb
    con = duckdb.connect(db)
    con.execute(f"INSERT INTO {table} VALUES ({int(value)})")
    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.close()
    out({"count": n})


# -------------------------------------------------------------------- human
def jira_client():
    from jira import JIRA
    return JIRA(server=E["JIRA_URL"], basic_auth=(cred("JIRA_USER"), cred("JIRA_API_TOKEN")),
                options={"rest_api_version": "3"})


def jira_get(key):
    issue = jira_client().issue(key)
    out({"key": issue.key, "status": str(issue.fields.status),
         "summary": issue.fields.summary})


def jira_comment(key, text):
    comment = jira_client().add_comment(key, text)
    out({"id": comment.id})


def jira_get_v2(key):
    """REST v2: the description and comment bodies are plain strings."""
    from jira import JIRA
    client = JIRA(server=E["JIRA_URL"], basic_auth=(cred("JIRA_USER"), cred("JIRA_API_TOKEN")),
                  options={"rest_api_version": "2"})
    issue = client.issue(key)
    out({"key": issue.key, "self": issue.self, "status": str(issue.fields.status),
         "description": issue.fields.description,
         "comments": [comment.body for comment in issue.fields.comment.comments]})


def slack():
    from slack_sdk import WebClient
    return WebClient(base_url=E["SLACK_BASE_URL"].rstrip("/") + "/api/",
                     token=cred("SLACK_BOT_TOKEN"))


def slack_history(channel, limit):
    history = slack().conversations_history(channel=channel, limit=int(limit))
    out({"texts": [m["text"] for m in history["messages"]]})


def slack_post(channel, text):
    posted = slack().chat_postMessage(channel=channel, text=text)
    out({"ts": posted["ts"], "channel": posted["channel"]})


def confluence():
    from atlassian import Confluence
    return Confluence(url=E["CONFLUENCE_URL"].rstrip("/") + "/wiki",
                      username=cred("CONFLUENCE_USER"), password=cred("CONFLUENCE_API_TOKEN"))


def conf_get(space, title):
    c = confluence()
    page_id = c.get_page_id(space, title)
    page = c.get_page_by_id(page_id, expand="body.storage,version")
    out({"id": page_id, "title": page["title"], "version": page["version"]["number"],
         "body": page["body"]["storage"]["value"]})


def conf_edit(page_id, title, body):
    updated = confluence().update_page(page_id, title, body)
    out({"version": updated["version"]["number"]})


def conf_v2_get(title):
    """Confluence REST v2, the way a page is found and read there: the space,
    the page by title in it (the client's own v2 helper), its storage body."""
    c = confluence()
    space = c.get("api/v2/spaces")["results"][0]
    found = c.get_all_pages_by_space_ids_confluence_cloud([space["id"]], title=title)
    page = c.get("api/v2/pages/" + found[0]["id"], params={"body-format": "storage"})
    out({"space": space["key"], "id": page["id"], "title": page["title"],
         "version": page["version"]["number"], "body": page["body"]["storage"]["value"]})


def conf_by_world_id(page_id):
    try:
        page = confluence().get_page_by_id(page_id, expand="body.storage,version")
        out({"found": True, "title": page["title"], "version": page["version"]["number"],
             "body": page["body"]["storage"]["value"]})
    except Exception as exc:  # noqa: BLE001 - a 404 is the observation
        out({"found": False, "error": type(exc).__name__})


# ----------------------------------------------------------------- Power BI
def pbi_token():
    import requests
    r = requests.post(f"{E['AZURE_AUTHORITY_HOST']}/{E['AZURE_TENANT_ID']}/oauth2/v2.0/token",
                      data={"grant_type": "client_credentials",
                            "client_id": E["AZURE_CLIENT_ID"],
                            "client_secret": E["AZURE_CLIENT_SECRET"],
                            "scope": "https://analysis.windows.net/powerbi/api/.default"},
                      timeout=60)
    r.raise_for_status()
    return r.json()["access_token"]


def pbi_session():
    import requests
    s = requests.Session()
    s.headers["Authorization"] = "Bearer " + pbi_token()
    api = E["POWERBI_API_URL"] + "/v1.0/myorg"
    group = s.get(f"{api}/groups", timeout=60).json()["value"][0]
    dataset = s.get(f"{api}/groups/{group['id']}/datasets", timeout=60).json()["value"][0]
    return s, api, group, dataset


KPI_DAX = ('EVALUATE SUMMARIZECOLUMNS ( "Net Revenue", [Net Revenue], '
           '"Completed Orders", [Completed Orders], "AOV", [AOV] )')


def pbi_dax(s, api, group, dataset):
    r = s.post(f"{api}/groups/{group['id']}/datasets/{dataset['id']}/executeQueries",
               json={"queries": [{"query": KPI_DAX}]}, timeout=120)
    r.raise_for_status()
    return r.json()["results"][0]["tables"][0]["rows"][0]


def pbi_step(step, *args):
    if step == "token":
        out({"token_type": "Bearer", "length": len(pbi_token())})
        return
    s, api, group, dataset = pbi_session()
    fab = E["FABRIC_API_URL"] + "/v1"
    model = f"{fab}/workspaces/{group['id']}/semanticModels/{dataset['id']}"
    if step == "list":
        out({"group": group["name"], "dataset": dataset["name"]})
    elif step == "dax":
        out({"row": pbi_dax(s, api, group, dataset)})
    elif step == "getdef":
        parts = s.post(f"{model}/getDefinition", timeout=60).json()["definition"]["parts"]
        out({"parts": {p["path"]: p["payload"] for p in parts}})
    elif step == "update":
        parts = s.post(f"{model}/getDefinition", timeout=60).json()["definition"]["parts"]
        old = "measure 'Gross Revenue' = SUM ( Orders[order_amount] )"
        new = ("measure 'Gross Revenue' = SUM ( Orders[order_amount] ) + "
               "SUM ( Orders[shipping_amount] )")
        edited = 0
        for p in parts:
            text = base64.b64decode(p["payload"]).decode("utf-8")
            if old in text:
                p["payload"] = base64.b64encode(text.replace(old, new).encode()).decode()
                edited += 1
        r = s.post(f"{model}/updateDefinition", json={"definition": {"parts": parts}},
                   timeout=120)
        r.raise_for_status()
        out({"edited": edited, "row": pbi_dax(s, api, group, dataset)})
    elif step == "deploy_refresh":
        root = args[0]
        parts = []
        for here, _dirs, files in os.walk(root):
            for name in sorted(files):
                path = os.path.join(here, name)
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                parts.append({"path": rel, "payloadType": "InlineBase64",
                              "payload": base64.b64encode(open(path, "rb").read()).decode()})
        r = s.post(f"{model}/updateDefinition", json={"definition": {"parts": parts}},
                   timeout=120)
        r.raise_for_status()
        r = s.post(f"{api}/groups/{group['id']}/datasets/{dataset['id']}/refreshes",
                   json={"notifyOption": "NoNotification"}, timeout=300)
        r.raise_for_status()
        history = s.get(f"{api}/groups/{group['id']}/datasets/{dataset['id']}/refreshes",
                        params={"$top": "3"}, timeout=60).json()["value"]
        out({"deployed": len(parts), "location": r.headers.get("Location"),
             "history": history})


# ------------------------------------------------------------------ Jupyter
def jupyter_server_cell(code_file):
    """One cell on the notebook SERVER the box advertises (JUPYTER_URL): a kernel
    over REST, the cell over the kernel's websocket, as JupyterLab sends it."""
    import uuid
    import requests
    import websocket
    base = E["JUPYTER_URL"].rstrip("/")
    kernel = requests.post(f"{base}/api/kernels", json={"name": "alkera-python"},
                           timeout=120).json()
    ws = websocket.create_connection(
        base.replace("http", "ws", 1) + f"/api/kernels/{kernel['id']}/channels", timeout=180)
    msg_id = uuid.uuid4().hex
    ws.send(json.dumps({
        "header": {"msg_id": msg_id, "username": "e2e", "session": uuid.uuid4().hex,
                   "msg_type": "execute_request", "version": "5.3"},
        "parent_header": {}, "metadata": {}, "channel": "shell", "buffers": [],
        "content": {"code": open(code_file).read(), "silent": False, "store_history": True,
                    "user_expressions": {}, "allow_stdin": False, "stop_on_error": True}}))
    texts, status = [], None
    while status is None:
        msg = json.loads(ws.recv())
        if (msg.get("parent_header") or {}).get("msg_id") != msg_id:
            continue
        if msg.get("msg_type") == "stream":
            texts.append(msg["content"]["text"])
        elif msg.get("msg_type") == "execute_reply":
            status = msg["content"]["status"]
    ws.close()
    requests.delete(f"{base}/api/kernels/{kernel['id']}", timeout=60)
    out({"status": status, "stdout": "".join(texts)})


# ------------------------------------------------------------------ Tableau
def tsc_view(sheet):
    import csv, io
    import tableauserverclient as TSC
    server = TSC.Server(E["TABLEAU_SERVER_URL"], use_server_version=True)
    server.auth.sign_in(TSC.PersonalAccessTokenAuth(E["TABLEAU_PAT_NAME"],
                                                    E["TABLEAU_PAT_SECRET"],
                                                    site_id=E.get("TABLEAU_SITE", "")))
    workbooks, _ = server.workbooks.get()
    workbook = workbooks[0]
    server.workbooks.populate_views(workbook)
    view = next(v for v in workbook.views if v.name == sheet)
    server.views.populate_csv(view)
    rows = list(csv.reader(io.StringIO(b"".join(view.csv).decode("utf-8"))))
    server.auth.sign_out()
    out({"rows": rows})


# -------------------------------------------------------------------- Sigma
def sigma_session():
    import requests
    base = E["SIGMA_BASE_URL"].rstrip("/") + "/v2"
    r = requests.post(f"{base}/auth/token", data={"grant_type": "client_credentials"},
                      auth=(E["SIGMA_CLIENT_ID"], E["SIGMA_CLIENT_SECRET"]), timeout=60)
    r.raise_for_status()
    s = requests.Session()
    s.headers["Authorization"] = "Bearer " + r.json()["access_token"]
    return s, base


def sigma_export(s, base, workbook_id, element_id):
    job = s.post(f"{base}/workbooks/{workbook_id}/export", timeout=120,
                 json={"elementId": element_id, "format": {"type": "csv"}})
    job.raise_for_status()
    got = s.get(f"{base}/query/{job.json()['queryId']}/download", timeout=60)
    got.raise_for_status()
    return got.text


def sigma_step(step):
    if step == "token":
        s, base = sigma_session()
        out({"ok": True})
        return
    s, base = sigma_session()
    if step == "sync":
        conn = s.get(f"{base}/connections", timeout=60).json()["entries"][0]["connectionId"]
        r = s.post(f"{base}/connections/{conn}/sync", timeout=120,
                   json={"path": ["ANALYTICS", "WAREHOUSE", "FCT_ORDERS"]})
        r.raise_for_status()
        out({"connectionId": conn, "status": r.status_code, "body": r.json()})
        return
    entries = s.get(f"{base}/workbooks", timeout=60).json()["entries"]
    if step == "list":
        out({"workbooks": [(w["workbookId"], w["name"]) for w in entries]})
        return
    workbook_id = entries[0]["workbookId"]
    doc = s.get(f"{base}/workbooks/{workbook_id}", params={"includeContents": "true"},
                timeout=60).json()
    if step == "get":
        out({"workbookId": workbook_id, "contents": doc["contents"]})
    elif step == "export":
        files = {el["name"]: sigma_export(s, base, workbook_id, el["id"])
                 for el in doc["contents"]["elements"]}
        out({"files": files})
    elif step == "put":
        contents = doc["contents"]
        kpis = next(el for el in contents["elements"] if el["name"] == "KPIs")
        for column in kpis["columns"]:
            if column["formula"] == "[Metrics/Net Revenue]":
                column["formula"] = "[Metrics/Gross Revenue]"
        r = s.put(f"{base}/workbooks/{workbook_id}/contents", json={"contents": contents},
                  timeout=120)
        r.raise_for_status()
        out({"version": r.json().get("latestVersion"),
             "kpis": sigma_export(s, base, workbook_id, kpis["id"])})
    elif step == "inspect":
        kpis = next(el for el in doc["contents"]["elements"] if el["name"] == "KPIs")
        workbook = f"{base}/workbooks/{workbook_id}"
        got = {}
        for name, path in (("query", f"/elements/{kpis['id']}/query"),
                           ("columns", f"/elements/{kpis['id']}/columns"),
                           ("sources", "/sources")):
            r = s.get(workbook + path, timeout=60)
            r.raise_for_status()
            got[name] = r.json()
        out({"elementId": kpis["id"], **got})


if __name__ == "__main__":
    step, args = sys.argv[1], sys.argv[2:]
    if step.startswith("pbi_"):
        pbi_step(step[4:], *args)
    elif step.startswith("sigma_"):
        sigma_step(step[6:])
    else:
        globals()[step](*args)
'''


def notebook_cells(workspace: str, warehouse: str, out_file: str) -> list[str]:
    """Three cells an analyst would write: a parquet export, the warehouse, a file."""
    parquet = f"{workspace}/data/exports/orders/part-00000.parquet"
    return [
        "import pandas as pd\n"
        f"df = pd.read_parquet({parquet!r})\n"
        "n_parquet = len(df)\nn_parquet",
        "import duckdb\n"
        f"con = duckdb.connect({warehouse!r}, read_only=True)\n"
        "net, completed = con.execute(\"SELECT SUM(order_amount), COUNT(*) FROM "
        "warehouse.warehouse.fct_orders WHERE order_status = 'completed'\").fetchone()\n"
        "con.close()\n(net, completed)",
        "import json\n"
        f"with open({out_file!r}, 'w') as fh:\n"
        "    json.dump({'n_parquet': n_parquet, 'net': str(net), 'completed': completed}, fh)\n",
    ]


def write_notebook(path: Path, cells: list[str]) -> Path:
    import nbformat

    nb = nbformat.v4.new_notebook()
    nb.cells = [nbformat.v4.new_code_cell(c) for c in cells]
    nb.metadata["kernelspec"] = {"name": "alkera-python", "display_name": "Python 3 (alkera)",
                                 "language": "python"}
    path.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb, str(path))
    return path


# --------------------------------------------------------------------------
# topologies: how a command reaches the episode, and how it names the world
# --------------------------------------------------------------------------
@dataclass
class Outcome:
    returncode: int
    stdout: str
    stderr: str
    seconds: float


class HostTopology:
    """`native_stack.NativeStack`: services on loopback, shims on PATH, a host shell."""

    name = "host"

    def __init__(self, world: dict, work: Path):
        self.world, self.work = world, work
        self.audit_dir = work / "audit"
        self.rollout_dir = work / "rollout"
        self.scripts = work / "scripts"
        self.stack = None
        self.env: dict = {}
        self.endpoints: dict = {}

    # how the episode spells things
    @property
    def workspace(self) -> str:
        return self.world["workspace"]

    @property
    def warehouse(self) -> str:
        return self.world["warehouse"]

    def script(self, name: str) -> str:
        return str(self.scripts / name)

    def container_paths(self) -> dict | None:
        return None

    def destination(self, service: str) -> tuple[str, int]:
        import agent_under_test as aut

        return aut._host_port(self.endpoints[service])

    def up(self) -> dict:
        import agent_under_test as aut
        import native_stack

        record = {"workspace_cli": native_stack.install_workspace_cli(self.world),
                  "discovery": native_stack.stage_discovery(self.world),
                  "sanitised": native_stack.sanitize_world_files(self.world)}
        spec = SimpleNamespace(profile=PROFILE_NAME, plugs=PLUGS, required_surfaces=[])
        self.stack = native_stack.NativeStack(world=self.world, audit_dir=self.audit_dir,
                                              rollout_dir=self.rollout_dir, spec=spec,
                                              key=f"bi-e2e/{self.work}")
        self.endpoints = self.stack.up()
        # The shims are in front of PATH (NativeStack._install_shims); behind
        # them, the interpreter this check runs under, which `uv run --with`
        # gave tabcmd and tableauserverclient - on the episode image they are
        # the toolbelt's (/opt/toolbelt/bin). Nothing else is added.
        tools = str(Path(sys.executable).parent)
        path = [p for p in os.environ["PATH"].split(os.pathsep) if p != tools]
        path.insert(1, tools)
        home = self.work / "home"
        home.mkdir(parents=True, exist_ok=True)
        jupyter = self.work / "jupyter"
        from alkera_gateway.native.notebook import install_kernelspec

        install_kernelspec(jupyter, interpreter=sys.executable)
        self.env = {**os.environ, "PATH": os.pathsep.join(path), "PYTHONUTF8": "1",
                    **aut.native_env(endpoints=self.endpoints, audit_dir=self.audit_dir,
                                     profiles_dir=self.world["profiles_dir"],
                                     scheduler=self.world["scheduler"],
                                     gateway_config=self.stack.config_file),
                    "ALKERA_WORKSPACE": self.workspace,
                    # the engineer's own HOME and Jupyter data dir, not the operator's
                    "HOME": str(home), "JUPYTER_PATH": str(jupyter),
                    "JUPYTER_RUNTIME_DIR": str(self.work / "jupyter-runtime")}
        self.scripts.mkdir(parents=True, exist_ok=True)
        record["endpoints"] = self.endpoints
        record["stack"] = self.stack.record()
        record["subject_env"] = {k: v for k, v in sorted(self.env.items()) if k.startswith(
            ("ALKERA_", "JIRA", "SLACK", "CONFLUENCE", "POWERBI", "FABRIC", "AZURE",
             "TABLEAU", "SIGMA", "DBT_", "SCHED", "JUPYTER"))}
        return record

    def put(self, name: str, text: str) -> str:
        path = self.scripts / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def run(self, command: str, timeout: int = 900) -> Outcome:
        started = time.monotonic()
        done = subprocess.run(["bash", "-c", command], cwd=self.workspace, env=self.env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
        return Outcome(done.returncode, done.stdout, done.stderr,
                       round(time.monotonic() - started, 2))

    def down(self) -> None:
        if self.stack is not None:
            self.stack.down()
            self.stack = None


class ContainerTopology:
    """`container_episode.NativeContainerEpisode`: compose, docker exec, *.internal."""

    name = "container"
    IN_SCRIPTS = "/tmp/e2e"

    def __init__(self, world: dict, work: Path, image: str):
        self.world, self.work, self.image = world, work, image
        self.audit_dir = work / "audit"
        self.rollout_dir = work / "rollout"
        self.stack = None
        self.project = f"alkera-bi-e2e-{int(time.time())}"

    @property
    def workspace(self) -> str:
        return "/workspace"

    @property
    def warehouse(self) -> str:
        return "/world/var/warehouse/warehouse.duckdb"

    def script(self, name: str) -> str:
        return f"{self.IN_SCRIPTS}/{name}"

    def container_paths(self) -> dict | None:
        import blindness

        return blindness.container_path_map(self.world)

    def destination(self, service: str) -> tuple[str, int]:
        import agent_under_test as aut

        return aut._host_port(self.stack.episode_endpoints()[service])

    def up(self) -> dict:
        import container_episode as ce
        import native_stack

        record = {"workspace_cli": native_stack.install_workspace_cli(self.world,
                                                                      "container"),
                  "discovery": native_stack.stage_discovery(self.world),
                  "sanitised": native_stack.sanitize_world_files(self.world)}
        self.stack = ce.NativeContainerEpisode(
            project=self.project, world=self.world, audit_dir=self.audit_dir,
            rollout_dir=self.rollout_dir, image=self.image, profile=PROFILE_NAME,
            plugs={k: list(v) for k, v in PLUGS.items()})
        record["container_id"] = self.stack.up()[:12]
        record["prepared"] = self.stack.prepare()
        record["image"] = self.image
        record["image_digest"] = getattr(self.stack, "image_digest", None)
        record["services"] = self.stack.services
        record["episode_endpoints"] = self.stack.episode_endpoints()
        self.exec_raw(["mkdir", "-p", self.IN_SCRIPTS])
        env = self.exec_raw(["/usr/local/bin/python", "-c",
                             "import json,os;print(json.dumps(dict(os.environ)))"])
        self.container_env = json.loads(env.stdout)
        record["subject_env"] = {k: v for k, v in sorted(self.container_env.items())
                                 if k.startswith(("ALKERA_", "JIRA", "SLACK", "CONFLUENCE",
                                                  "POWERBI", "FABRIC", "AZURE", "TABLEAU",
                                                  "SIGMA", "DBT_", "SCHED", "JUPYTER",
                                                  "PATH", "HOME", "SNOWFLAKE", "PG",
                                                  "METABASE", "PROMETHEUS", "ALERTMANAGER",
                                                  "LOKI", "AIRBYTE", "DEPLOY", "AWS_"))}
        labels = subprocess.run(["docker", "image", "inspect", self.image, "--format",
                                 "{{json .Config.Labels}}"], capture_output=True, text=True)
        record["image_labels"] = json.loads(labels.stdout or "null")
        return record

    def exec_raw(self, argv: list[str], *, workdir: str | None = None,
                 timeout: int = 900) -> subprocess.CompletedProcess:
        command = ["docker", "exec"] + (["-w", workdir] if workdir else []) + [
            self.stack.container_id, *argv]
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)

    def put(self, name: str, text: str) -> str:
        local = self.work / "scripts" / name
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(text, encoding="utf-8")
        self.stack.copy_in(local, self.script(name))
        return self.script(name)

    def run(self, command: str, timeout: int = 900) -> Outcome:
        started = time.monotonic()
        done = self.exec_raw(["bash", "-c", command], workdir="/workspace", timeout=timeout)
        return Outcome(done.returncode, done.stdout, done.stderr,
                       round(time.monotonic() - started, 2))

    def down(self) -> None:
        if self.stack is not None:
            try:
                self.stack.down()
            finally:
                self.stack = None


# --------------------------------------------------------------------------
# the drive
# --------------------------------------------------------------------------
@dataclass
class Action:
    id: str
    family: str
    label: str
    command: str
    expect: list[Expect]
    #: HTTP services this action talks to (destinations.jsonl must move for
    #: exactly these)
    services: tuple[str, ...] = ()
    value: Callable[[Outcome, dict], tuple[bool | None, str]] | None = None
    #: expected process exit: 0, "nonzero", or None (not checked)
    returncode: Any = 0
    #: a documented loss of resolution: the row is right, and coarser by design
    doc: str = ""
    #: not an accuracy failure, but something the reader must know
    finding: Callable[[Outcome, list[dict]], str] | None = None


def relation(name: str) -> Callable:
    """targets/read_targets == [name], with the file's own catalog tolerated."""
    return lambda got, ctx: [strip_catalog(t) for t in (got or [])] == [name]


def exactly(*names: str) -> Callable:
    return lambda got, ctx: list(got or []) == list(names)


def subset_of(*names: str) -> Callable:
    return lambda got, ctx: set(got or []) <= set(names)


def warehouse_named(ctx_key: str = "warehouse") -> Callable:
    return lambda got, ctx: list(got or []) == [ctx[ctx_key]]


def starts(prefix: str, n: int | None = None) -> Callable:
    return lambda got, ctx: bool(got) and all(t.startswith(prefix) for t in got) and (
        n is None or len(got) == n)


def drive(topo, truth: Truth) -> list[Action]:
    """Every action, in order. Values are compared AFTER each action ran."""
    ws, wh = topo.workspace, topo.warehouse
    py = f"python {topo.script('clients.py')}"
    k0 = truth.kpis()
    actions: list[Action] = []

    def add(**kw):
        actions.append(Action(**kw))

    # ---------------------------------------------------------- warehouse
    def v_cli_select(o, ctx):
        line = o.stdout.strip().splitlines()[-1] if o.stdout.strip() else ""
        k = truth.kpis()
        return (line == f"{k['net']}\t{k['completed']}",
                f"cli {line!r} vs sql {k['net']}\t{k['completed']}")

    add(id="W1", family="warehouse", label="duckdb CLI SELECT (shim)",
        command=(f"duckdb -readonly {wh} -c \"SELECT SUM(order_amount), COUNT(*) FROM "
                 "warehouse.warehouse.fct_orders WHERE order_status = 'completed'\""),
        expect=[Expect("duckdb.statement", "read", relation("warehouse.fct_orders"),
                       relation("warehouse.fct_orders"))],
        value=v_cli_select)

    def v_scratch(n):
        def check(o, ctx):
            got = truth.count(SCRATCH_TABLE) if truth.table_exists("main", "e2e_scratch") \
                else None
            return got == n, f"{SCRATCH_TABLE} has {got} rows, expected {n}"
        return check

    add(id="W2", family="warehouse", label="duckdb CLI CREATE + INSERT (shim)",
        command=(f"duckdb {wh} -c \"CREATE TABLE {SCRATCH_TABLE} (n INTEGER)\" "
                 f"-c \"INSERT INTO {SCRATCH_TABLE} VALUES (1), (2), (3)\""),
        expect=[Expect("duckdb.statement", "ddl", exactly(SCRATCH_TABLE), exactly()),
                Expect("duckdb.statement", "write", exactly(SCRATCH_TABLE), exactly())],
        value=v_scratch(3))

    def v_py_select(o, ctx):
        got, k = parse_result(o.stdout) or {}, truth.kpis()
        return (same_number(got.get("net"), k["net"]) and got.get("completed")
                == k["completed"], f"python {got} vs sql net={k['net']} n={k['completed']}")

    add(id="W3", family="warehouse", label="python + duckdb SELECT (python shim)",
        command=f"{py} py_select {wh}",
        expect=[Expect("shim.python", "exec", exactly())], value=v_py_select,
        doc="python shim row: argv, exit and changed files - no table targets for "
            "a read (gateway/NATIVE.md, a stated loss of resolution)")
    add(id="W4", family="warehouse", label="python + duckdb INSERT (python shim)",
        command=f"{py} py_insert {wh} {SCRATCH_TABLE} 4",
        expect=[Expect("shim.python", "exec", warehouse_named())], value=v_scratch(4),
        doc="the python shim names the warehouse FILE it changed, not the table")
    add(id="W5", family="warehouse", label="python3 + duckdb INSERT (python3)",
        command=f"python3 {topo.script('clients.py')} py_insert {wh} {SCRATCH_TABLE} 5",
        expect=[Expect("shim.python", "exec", warehouse_named())], value=v_scratch(5),
        doc="filed as shim.python; argv[0] keeps python3")

    def v_dropped(o, ctx):
        gone = not truth.table_exists("main", "e2e_scratch")
        return gone, "scratch table dropped" if gone else "scratch table still there"

    add(id="W6", family="warehouse", label="duckdb CLI DROP (shim)",
        command=f"duckdb {wh} -c \"DROP TABLE {SCRATCH_TABLE}\"",
        expect=[Expect("duckdb.statement", "destructive", exactly(SCRATCH_TABLE))],
        value=v_dropped)

    # ---------------------------------------------------------------- dbt
    dbt = (f"DBT_WAREHOUSE_PATH={wh} dbt --no-use-colors {{cmd}} --project-dir dbt_project "
           "--profiles-dir config")

    def v_dbt_ls(o, ctx):
        listed = sorted(line.rsplit(".", 1)[-1] for line in o.stdout.split()
                        if line.startswith("commerce_analytics."))
        files = sorted(p.stem for p in (Path(truth.world["workspace"]) / "dbt_project" /
                                        "models").rglob("*.sql"))
        marts = all(truth.table_exists("warehouse", m) for m in listed
                    if not m.startswith("stg_"))
        return listed == files and marts, f"ls {listed} vs model files {files}; " \
                                          f"every mart in the warehouse: {marts}"

    add(id="D1", family="dbt", label="dbt ls --resource-type model (shim)",
        command=dbt.format(cmd="ls") + " --resource-type model -q",
        expect=[Expect("shim.dbt", "exec", subset_of("config/.user.yml"))], value=v_dbt_ls,
        doc="shim row names workspace files changed (dbt's anonymous-id file); "
            "target/ and logs/ are skipped by design")

    def v_dbt_test(o, ctx):
        m = re.search(r"PASS=(\d+) WARN=(\d+) ERROR=(\d+)", o.stdout)
        dupes, nulls = truth.q(
            "SELECT COUNT(*) - COUNT(DISTINCT order_id), COUNT(*) - COUNT(order_id) "
            "FROM wh.warehouse.fct_orders")[0]
        ok = bool(m) and m.group(3) == "0" and dupes == 0 and nulls == 0
        return ok, f"dbt {m.group(0) if m else '?'}; sql: duplicate order_id={dupes}, " \
                   f"null order_id={nulls}"

    add(id="D2", family="dbt", label="dbt test -s fct_orders (shim)",
        command=dbt.format(cmd="test") + " -s fct_orders",
        expect=[Expect("shim.dbt", "exec",
                       lambda got, ctx: set(got or []) <= {"config/.user.yml",
                                                           ctx["warehouse"]})],
        value=v_dbt_test)

    def v_dbt_build(o, ctx):
        k = truth.kpis()
        ok = "ERROR=0" in o.stdout and k == k0
        return ok, f"after build: net={k['net']} n={k['completed']} (before: " \
                   f"net={k0['net']} n={k0['completed']})"

    add(id="D3", family="dbt", label="dbt build -s fct_orders (shim)",
        command=dbt.format(cmd="build") + " -s fct_orders",
        expect=[Expect("shim.dbt", "exec",
                       lambda got, ctx: ctx["warehouse"] in (got or []))],
        value=v_dbt_build,
        doc="the shim names the warehouse FILE dbt rewrote, not the model")

    # ---------------------------------------------------------- scheduler
    # da/laptop plugs NO orchestrator, so the box has no scheduler CLI on
    # either topology (native_stack.install_workspace_cli reads the world's
    # profile record): each operation must fail before anything runs - no row,
    # no connection, no world change, the scheduler's state untouched.
    sched_cli = Path(truth.world["workspace"]) / "ops" / "sched"
    sched_before = Path(truth.world["scheduler"]).read_bytes()

    def v_no_sched(o, ctx):
        state = Path(truth.world["scheduler"]).read_bytes()
        ok = not sched_cli.exists() and state == sched_before
        return ok, (f"ops/sched in the working tree: {sched_cli.exists()}; "
                    f"var/sched/state.json unchanged: {state == sched_before}")

    for sid, argv in (("S1", "list"), ("S2", "logs"), ("S3", "trigger bi_refresh")):
        add(id=sid, family="scheduler", label=f"./ops/sched {argv} (no orchestrator)",
            command=f"./ops/sched {argv}", expect=[], returncode="nonzero",
            value=v_no_sched)

    # -------------------------------------------------------------- human
    def v_jira_get(o, ctx):
        got, want = parse_result(o.stdout) or {}, truth.issue(JIRA_KEY)
        ok = got.get("status") == want["status"] and got.get("summary") == want["title"]
        return ok, f"client {got.get('status')}/{got.get('summary')!r} vs issues.json " \
                   f"{want['status']}/{want['title']!r}"

    add(id="H1", family="jira", label="python jira: JIRA() + issue()",
        command=f"{py} jira_get {JIRA_KEY}", services=("jira",),
        expect=[Expect("jira.server_info", "read", exactly("issues")),
                Expect("jira.issues_get", "read", exactly(f"issue:{JIRA_KEY}"),
                       exactly(f"issue:{JIRA_KEY}")),
                Expect("shim.python", "exec", exactly())],
        value=v_jira_get)
    comment = "e2e accuracy check: looking at the January number"

    def v_jira_comment(o, ctx):
        comments = truth.issue(JIRA_KEY).get("comments") or []
        last = comments[-1] if comments else {}
        ok = last.get("body") == comment
        return ok, f"issues.json last comment {last.get('body')!r} by {last.get('author')}"

    add(id="H2", family="jira", label="python jira: add_comment()",
        command=f"{py} jira_comment {JIRA_KEY} '{comment}'", services=("jira",),
        expect=[Expect("jira.server_info", "read", exactly("issues")),
                Expect("jira.issues_comment", "write", exactly(f"issue:{JIRA_KEY}")),
                Expect("shim.python", "exec", exactly())],
        value=v_jira_comment)

    def v_jira_v2(o, ctx):
        got, want = parse_result(o.stdout) or {}, truth.issue(JIRA_KEY)
        bodies = [c["body"] for c in want.get("comments") or []]
        ok = (got.get("description") == want["body"] and got.get("comments") == bodies
              and got.get("status") == want["status"]
              and str(got.get("self", "")).endswith(f"/rest/api/2/issue/{JIRA_KEY}"))
        return ok, (f"v2 description == issues.json body (plain text): "
                    f"{got.get('description') == want['body']}; {len(got.get('comments') or [])}"
                    f" comment strings == issues.json: {got.get('comments') == bodies}")

    add(id="H8", family="jira", label="python jira rest_api_version=2: issue()",
        command=f"{py} jira_get_v2 {JIRA_KEY}", services=("jira",),
        expect=[Expect("jira.server_info", "read", exactly("issues")),
                Expect("jira.issues_get", "read", exactly(f"issue:{JIRA_KEY}"),
                       exactly(f"issue:{JIRA_KEY}")),
                Expect("shim.python", "exec", exactly())],
        value=v_jira_v2)

    def v_slack_history(o, ctx):
        got = (parse_result(o.stdout) or {}).get("texts")
        top = [m for m in truth.channel(SLACK_CHANNEL)["messages"] if not m.get("thread_id")]
        want = [m["text"] for m in sorted(top, key=lambda m: m["at"], reverse=True)[:3]]
        return got == want, f"{len(got or [])} messages; equal to chat.json's newest 3: " \
                            f"{got == want}"

    add(id="H3", family="slack", label="slack_sdk: conversations_history",
        command=f"{py} slack_history {SLACK_CHANNEL} 3", services=("slack",),
        expect=[Expect("slack.chat_history", "read", exactly(f"chat:{SLACK_CHANNEL}"),
                       exactly(f"chat:{SLACK_CHANNEL}")),
                Expect("shim.python", "exec", exactly())],
        value=v_slack_history)
    post = "e2e accuracy check: dashboards look consistent"

    def v_slack_post(o, ctx):
        texts = [m["text"] for m in truth.channel(SLACK_CHANNEL)["messages"]]
        return post in texts, f"chat.json has the posted text: {post in texts}"

    add(id="H4", family="slack", label="slack_sdk: chat_postMessage",
        command=f"{py} slack_post {SLACK_CHANNEL} '{post}'", services=("slack",),
        expect=[Expect("slack.chat_post", "write", exactly(f"chat:{SLACK_CHANNEL}")),
                Expect("shim.python", "exec", exactly())],
        value=v_slack_post)

    def v_conf_get(o, ctx):
        got, want = parse_result(o.stdout) or {}, truth.page(KB_SLUG)
        ok = got.get("body") == want["body"] and got.get("version") == want["version"]
        ctx["kb_id"] = got.get("id")
        return ok, f"page {got.get('id')} v{got.get('version')} body==kb.json: " \
                   f"{got.get('body') == want['body']} (kb.json v{want['version']})"

    add(id="H5", family="confluence", label="atlassian: get_page_id + get_page_by_id",
        command=f"{py} conf_get AE '{KB_TITLE}'", services=("confluence",),
        expect=[Expect("confluence.kb_list", "read", exactly("kb")),
                Expect("confluence.kb_get", "read", exactly(f"kb:{KB_SLUG}"),
                       exactly(f"kb:{KB_SLUG}")),
                Expect("shim.python", "exec", exactly())],
        value=v_conf_get)
    edited_body = KB_BODY

    def v_conf_edit(o, ctx):
        page, got = truth.page(KB_SLUG), parse_result(o.stdout) or {}
        ok = page["body"] == edited_body and got.get("version") == page["version"]
        return ok, (f"kb.json v{page['version']} (client saw v{got.get('version')}) body == "
                    f"the edit: {page['body'] == edited_body}")

    add(id="H6", family="confluence", label="atlassian: update_page",
        command=(f"{py} conf_edit \"$(cat {topo.script('kb_id')})\" '{KB_TITLE}' "
                 f"\"$(cat {topo.script('kb_body')})\""), services=("confluence",),
        expect=[Expect("confluence.kb_get", "read", exactly(f"kb:{KB_SLUG}"),
                       note="is_page_content_is_already_updated"),
                Expect("confluence.kb_get", "read", exactly(f"kb:{KB_SLUG}"),
                       note="update_page: the current version"),
                Expect("confluence.kb_get", "read", exactly(f"kb:{KB_SLUG}"),
                       note="update_page: the page's ancestors/type"),
                Expect("confluence.kb_edit", "write", exactly(f"kb:{KB_SLUG}")),
                Expect("shim.python", "exec", exactly())],
        value=v_conf_edit)
    listed = listed_wiki_id(
        (Path(truth.world["workspace"]) / "docs" / "onboarding.md").read_text("utf-8"),
        KB_TITLE)

    def v_conf_listed(o, ctx):
        got, want = parse_result(o.stdout) or {}, truth.page(KB_SLUG)
        ok = (got.get("found") is True and got.get("title") == KB_TITLE
              and got.get("body") == want["body"] and got.get("version") == want["version"])
        return ok, (f"onboarding.md lists `{listed}`: found={got.get('found')} "
                    f"{got.get('error', '')} v{got.get('version')} body==kb.json: "
                    f"{got.get('body') == want['body']}")

    add(id="H7", family="confluence", label="atlassian: page by the id onboarding.md lists",
        command=f"{py} conf_by_world_id {listed}", services=("confluence",),
        expect=[Expect("confluence.kb_get", "read", exactly(f"kb:{KB_SLUG}"),
                       exactly(f"kb:{KB_SLUG}")),
                Expect("shim.python", "exec", exactly())],
        value=v_conf_listed)

    def v_conf_v2(o, ctx):
        got, want = parse_result(o.stdout) or {}, truth.page(KB_SLUG)
        space = truth.fixture("kb.json").get("space") or {}
        ok = (got.get("id") == ctx.get("kb_id") and got.get("title") == KB_TITLE
              and got.get("body") == want["body"] and got.get("version") == want["version"]
              and got.get("space") == (space.get("key") if isinstance(space, dict) else space))
        return ok, (f"v2 page {got.get('id')} (v1 listed {ctx.get('kb_id')}) "
                    f"v{got.get('version')} body==kb.json: {got.get('body') == want['body']} "
                    f"(kb.json v{want['version']}, after H6's edit)")

    add(id="H9", family="confluence",
        label="atlassian REST v2: spaces, pages?title=&space-id=, pages/{id}",
        command=f"{py} conf_v2_get '{KB_TITLE}'", services=("confluence",),
        expect=[Expect("confluence.kb_list", "read", exactly("kb"), exactly("kb"),
                       note="GET /wiki/api/v2/spaces"),
                Expect("confluence.kb_list", "read", exactly("kb"), exactly("kb"),
                       note="GET /wiki/api/v2/pages?space-id=&title="),
                Expect("confluence.kb_get", "read", exactly(f"kb:{KB_SLUG}"),
                       exactly(f"kb:{KB_SLUG}"), note="GET /wiki/api/v2/pages/{id}"),
                Expect("shim.python", "exec", exactly())],
        value=v_conf_v2)

    # ----------------------------------------------------------- Power BI
    sm = "powerbi:semantic_model:Commerce"
    token = Expect("powerbi.token", "read", exactly("powerbi:token"), exactly("powerbi:token"))
    lists = [token,
             Expect("powerbi.groups_list", "read", exactly("powerbi:workspace:Analytics"),
                    exactly("powerbi:workspace:Analytics")),
             Expect("powerbi.datasets_list", "read", exactly(sm), exactly(sm))]
    shim_py = Expect("shim.python", "exec", exactly())

    def v_dax(expect_net, label):
        def check(o, ctx):
            got = parse_result(o.stdout) or {}
            row = got.get("row") or {}
            k = truth.kpis()
            want_net = expect_net()
            ok = (same_number(row.get("[Net Revenue]"), want_net)
                  and row.get("[Completed Orders]") == k["completed"]
                  and same_number(row.get("[AOV]"), Decimal(want_net) / k["completed"]))
            return ok, f"DAX {row} vs sql {label}={want_net} n={k['completed']}"
        return check

    add(id="P1", family="powerbi", label="Entra token (client credentials)",
        command=f"{py} pbi_token", services=("powerbi",), expect=[token, shim_py],
        value=lambda o, ctx: ((parse_result(o.stdout) or {}).get("length", 0) > 0,
                              "a bearer token was issued"))
    add(id="P2", family="powerbi", label="groups + datasets",
        command=f"{py} pbi_list", services=("powerbi",), expect=[*lists, shim_py],
        value=lambda o, ctx: ((parse_result(o.stdout) or {}) == {
            "group": "Analytics", "dataset": "Commerce"}, f"{parse_result(o.stdout)}"))
    add(id="P3", family="powerbi", label="executeQueries (KPIs DAX)",
        command=f"{py} pbi_dax", services=("powerbi",),
        expect=[*lists, Expect("powerbi.execute_queries", "read", exactly(sm), exactly(sm)),
                shim_py],
        value=v_dax(lambda: truth.kpis()["net"], "net"))

    def v_getdef(o, ctx):
        parts = (parse_result(o.stdout) or {}).get("parts") or {}
        root = Path(truth.world["workspace"]) / "bi" / "powerbi" / "Commerce.SemanticModel"
        files = {p.relative_to(root).as_posix(): p.read_bytes()
                 for p in root.rglob("*") if p.is_file()}
        same = {k: base64.b64decode(v) == files.get(k) for k, v in parts.items()}
        ok = bool(parts) and set(parts) == set(files) and all(same.values())
        return ok, f"{len(parts)} parts == bi/powerbi/Commerce.SemanticModel: {ok}"

    add(id="P4", family="powerbi", label="Fabric getDefinition",
        command=f"{py} pbi_getdef", services=("powerbi",),
        expect=[*lists, Expect("powerbi.get_definition", "read", exactly(sm), exactly(sm)),
                shim_py],
        value=v_getdef)
    add(id="P5", family="powerbi", label="updateDefinition (edited measure) + DAX",
        command=f"{py} pbi_update", services=("powerbi",),
        expect=[*lists, Expect("powerbi.get_definition", "read", exactly(sm)),
                Expect("powerbi.update_definition", "write", exactly(sm)),
                Expect("powerbi.execute_queries", "read", exactly(sm)), shim_py],
        value=v_dax(truth.net_with_shipping, "net incl. shipping"))
    relations = ["warehouse.dim_customer", "warehouse.dim_date", "warehouse.dim_product",
                 "warehouse.fct_order_lines", "warehouse.fct_orders"]

    def v_refresh(o, ctx):
        got = parse_result(o.stdout) or {}
        history = got.get("history") or []
        rows = ctx.get("rows") or []
        refresh = next((r for r in rows if r["tool"] == "powerbi.refresh"), {})
        values = ((refresh.get("result") or {}).get("values") or {}).get(
            "Executive Overview", {}).get("Overview", {}).get("KPIs", {})
        k = truth.kpis()
        published = (values.get("rows") or [[None, None, None]])[0]
        ok = (bool(history) and history[0].get("status") == "Completed"
              and same_number(published[0], k["net"]) and published[1] == k["completed"]
              and same_number(published[2], k["aov"]))
        return ok, f"history[0]={history[0].get('status') if history else None}; " \
                   f"published KPIs {published} vs sql {k['net']}, {k['completed']}"

    add(id="P6", family="powerbi", label="deploy repo TMDL + refresh + history",
        command=(f"{py} pbi_deploy_refresh "
                 f"{ws}/bi/powerbi/Commerce.SemanticModel"), services=("powerbi",),
        expect=[*lists, Expect("powerbi.update_definition", "write", exactly(sm)),
                Expect("powerbi.refresh", "write", exactly(sm), exactly(*relations)),
                Expect("powerbi.refreshes_list", "read", exactly(sm), exactly(sm)), shim_py],
        value=v_refresh)

    # ------------------------------------------------------------ Tableau
    signin = [Expect("tableau.server_info", "read", exactly("tableau:server")),
              Expect("tableau.auth_signin", "read", exactly("tableau:session")),
              Expect("tableau.users_get", "read", starts("tableau:user:", 1))]
    projects = Expect("tableau.projects_list", "read", exactly("tableau:project:Default"))
    # tabcmd 2 writes tabcmd.log in the current directory (the repo) on every
    # run; the PATH shim's row names it (shims/install.py DEFAULT_BINARIES).
    shim_tab = Expect("shim.tabcmd", "exec", exactly("tabcmd.log"))
    tab = 'tabcmd {}'
    login = ('tabcmd login -s "$TABLEAU_SERVER_URL" --token-name "$TABLEAU_PAT_NAME" '
             '--token-value "$TABLEAU_PAT_SECRET"')
    add(id="T1", family="tableau", label="tabcmd login (PAT)", command=login,
        services=("tableau",), expect=[*signin, shim_tab])
    def v_published(rel: str):
        def check(o, ctx):
            import hashlib
            want = hashlib.sha256((Path(truth.world["workspace"]) / rel).read_bytes()
                                  ).hexdigest()
            row = next((r for r in ctx.get("rows") or []
                        if r["tool"].endswith("_publish")), {})
            got = (row.get("result") or {}).get("sha256")
            return got == want, f"server stored sha256 {str(got)[:12]} vs {rel} {want[:12]}"
        return check

    add(id="T2", family="tableau", label="tabcmd publish Commerce.tds -o",
        command=tab.format("publish bi/tableau/datasources/Commerce.tds -o"),
        services=("tableau",), value=v_published("bi/tableau/datasources/Commerce.tds"),
        expect=[*signin, projects,
                Expect("tableau.datasources_publish", "write",
                       exactly("tableau:datasource:Commerce")), shim_tab])
    add(id="T3", family="tableau", label="tabcmd publish 'Executive Overview.twb' -o",
        command=tab.format("publish 'bi/tableau/workbooks/Executive Overview.twb' -o"),
        services=("tableau",),
        value=v_published("bi/tableau/workbooks/Executive Overview.twb"),
        expect=[*signin, projects,
                Expect("tableau.workbooks_publish", "write",
                       exactly("tableau:workbook:ExecutiveOverview")), shim_tab])
    add(id="T4", family="tableau", label="tabcmd refreshextracts --datasource Commerce",
        command=tab.format("refreshextracts --datasource Commerce --synchronous"),
        services=("tableau",), returncode="nonzero",
        expect=[*signin, projects,
                Expect("tableau.datasources_list", "read",
                       exactly("tableau:datasource:Commerce")),
                Expect("tableau.datasources_refresh", "write",
                       exactly("tableau:datasource:Commerce"), exit="error"),
                Expect("shim.tabcmd", "exec", exactly("tabcmd.log"), exit="error")],
        value=lambda o, ctx: (any((r.get("error") or {}).get("code") == "403104"
                                  for r in ctx.get("rows") or []),
                              "refused 403104: the published source is a LIVE "
                              "connection (bi_projects writes extract=False)"))
    csv_out = topo.script("kpis.csv")

    def v_tab_kpis(o, ctx):
        try:
            text = Path(ctx["local_csv"]()).read_text("utf-8")
        except OSError as exc:
            return False, f"no CSV: {exc}"
        rows = list(csv.reader(io.StringIO(text)))
        k = truth.kpis()
        want = [["Net Revenue", "Completed Orders", "AOV"],
                [money(k["net"]), integer(k["completed"]), money(k["aov"])]]
        return rows == want, f"tabcmd {rows} vs sql {want}"

    add(id="T5", family="tableau", label="tabcmd export /ExecutiveOverview/KPIs --csv",
        command=tab.format(f"export /ExecutiveOverview/KPIs --csv -f {csv_out}"),
        services=("tableau",),
        expect=[*signin,
                Expect("tableau.views_list", "read",
                       exactly("tableau:view:ExecutiveOverview/sheets/KPIs")),
                Expect("tableau.views_data", "read",
                       exactly("tableau:view:ExecutiveOverview/sheets/KPIs"),
                       exactly("warehouse.fct_orders")), shim_tab],
        value=v_tab_kpis)

    def v_tsc_segment(o, ctx):
        rows = (parse_result(o.stdout) or {}).get("rows")
        want = [["segment", "Net Revenue", "Segment Share"]] + [
            [s, money(v), pct(share)] for s, v, share in truth.by_segment()]
        return rows == want, f"TSC {rows} vs sql {want}"

    tsc_expect = [Expect("tableau.auth_signin", "read", exactly("tableau:session")),
                  Expect("tableau.server_info", "read", exactly("tableau:server")),
                  Expect("tableau.workbooks_list", "read",
                         exactly("tableau:workbook:ExecutiveOverview")),
                  Expect("tableau.workbooks_views", "read",
                         exactly("tableau:workbook:ExecutiveOverview")),
                  Expect("tableau.views_data", "read",
                         exactly("tableau:view:ExecutiveOverview/sheets/RevenuebySegment"),
                         exactly("warehouse.dim_customer", "warehouse.fct_orders")),
                  Expect("tableau.auth_signout", "read", exactly("tableau:session"))]
    add(id="T6", family="tableau", label="tableauserverclient: populate_csv (python)",
        command=f"{py} tsc_view 'Revenue by Segment'", services=("tableau",),
        expect=[*tsc_expect, shim_py], value=v_tsc_segment,
        finding=lambda o, rows: ("" if o.returncode == 0 else
                                 "`python` cannot import tableauserverclient: the toolbelt "
                                 "installs it in /opt/toolbelt's own venv, beside tabcmd"))
    if topo.name == "container":
        add(id="T6b", family="tableau",
            label="tableauserverclient via /opt/toolbelt/bin/python",
            command=f"/opt/toolbelt/bin/python {topo.script('clients.py')} tsc_view "
                    "'Revenue by Segment'",
            services=("tableau",), expect=list(tsc_expect), value=v_tsc_segment,
            doc="an absolute interpreter is not shimmed: the service rows are the record")

    # -------------------------------------------------------------- Sigma
    wb_doc = json.loads((Path(truth.world["workspace"]) / "bi" / "sigma" / "workbooks" /
                         "executive-overview.json").read_text("utf-8"))
    wb = f"sigma:workbook:{wb_doc['workbookId']}"
    stoken = Expect("sigma.auth_token", "read", exactly("sigma:session"),
                    exactly("sigma:session"))
    slist = Expect("sigma.workbooks_list", "read", exactly(wb), exactly(wb))
    sget = Expect("sigma.workbook_get", "read", exactly(wb), exactly(wb))
    add(id="G1", family="sigma", label="client-credentials token",
        command=f"{py} sigma_token", services=("sigma",), expect=[stoken, shim_py],
        value=lambda o, ctx: ((parse_result(o.stdout) or {}).get("ok") is True, "issued"))
    add(id="G2", family="sigma", label="GET /v2/workbooks",
        command=f"{py} sigma_list", services=("sigma",), expect=[stoken, slist, shim_py],
        value=lambda o, ctx: ((parse_result(o.stdout) or {}).get("workbooks") == [
            [wb_doc["workbookId"], wb_doc["name"]]], f"{parse_result(o.stdout)}"))
    add(id="G3", family="sigma", label="GET /v2/workbooks/{id}?includeContents=true",
        command=f"{py} sigma_get", services=("sigma",),
        expect=[stoken, slist, sget, shim_py],
        value=lambda o, ctx: ((parse_result(o.stdout) or {}).get("contents")
                              == wb_doc["contents"],
                              "contents == bi/sigma/workbooks/executive-overview.json"))
    element_relations = {
        "KPIs": ["warehouse.fct_orders"],
        "Revenue by Month": ["warehouse.dim_date", "warehouse.fct_orders"],
        "Revenue by Segment": ["warehouse.dim_customer", "warehouse.fct_orders"],
        "Top Products": ["warehouse.dim_product", "warehouse.fct_order_lines"],
        "Revenue by Region 2026": ["warehouse.dim_customer", "warehouse.dim_date",
                                   "warehouse.fct_orders"]}
    exports = []
    for el in wb_doc["contents"]["elements"]:
        exports += [Expect("sigma.workbook_export", "read", exactly(wb),
                           exactly(*element_relations[el["name"]], wb)),
                    Expect("sigma.query_download", "read", starts("sigma:query:", 1))]

    def sigma_want() -> dict:
        k = truth.kpis()
        return {
            "KPIs": [["Net Revenue", "Completed Orders", "AOV"],
                     [str(k["net"]), str(k["completed"]), None]],
            "Revenue by Month": [["year", "month", "Net Revenue"]]
            + [[str(y), str(m), str(v)] for y, m, v in truth.by_month()],
            "Revenue by Segment": [["segment", "Net Revenue"]]
            + [[s, str(v)] for s, v, _ in truth.by_segment()],
            "Top Products": [["product_name", "Units"]]
            + [[p, str(u)] for p, u in truth.top_products()],
            "Revenue by Region 2026": [["region", "Net Revenue"]]
            + [[r, str(v)] for r, v in truth.region_2026()]}

    def v_sigma_export(o, ctx):
        files = (parse_result(o.stdout) or {}).get("files") or {}
        want, bad = sigma_want(), []
        k = truth.kpis()
        for name, rows in want.items():
            got = list(csv.reader(io.StringIO(files.get(name, ""))))
            if name == "KPIs" and len(got) == 2 and len(got[1]) == 3:
                ok = got[0] == rows[0] and got[1][:2] == rows[1][:2] and same_number(
                    got[1][2], k["aov"])
            else:
                ok = got == rows
            if not ok:
                bad.append(f"{name}: {got[:3]} vs {rows[:3]}")
        return not bad and len(files) == 5, ("5 elements == sql" if not bad else
                                             "; ".join(bad))

    add(id="G4", family="sigma", label="export + download every element (csv)",
        command=f"{py} sigma_export", services=("sigma",),
        expect=[stoken, slist, sget, *exports, shim_py], value=v_sigma_export)
    kpis_id = next(el["id"] for el in wb_doc["contents"]["elements"] if el["name"] == "KPIs")

    def v_sigma_put(o, ctx):
        got = parse_result(o.stdout) or {}
        rows = list(csv.reader(io.StringIO(got.get("kpis", ""))))
        gross = truth.gross_all()
        ok = len(rows) == 2 and rows[1][0] == str(gross) and got.get("version") == 2
        return ok, f"published v{got.get('version')}; KPIs[Net Revenue column] " \
                   f"{rows[1][0] if len(rows) > 1 else None} vs sql gross {gross}"

    add(id="G5", family="sigma", label="PUT /contents (KPIs -> Gross Revenue) + export",
        command=f"{py} sigma_put", services=("sigma",),
        expect=[stoken, slist, sget, Expect("sigma.workbook_update", "write", exactly(wb)),
                Expect("sigma.workbook_export", "read", exactly(wb),
                       exactly("warehouse.fct_orders", wb)),
                Expect("sigma.query_download", "read", starts("sigma:query:", 1)), shim_py],
        value=v_sigma_put)
    del kpis_id
    # The connection sync replaces what Sigma's catalog knows of the synced
    # tables: a write on the connection, reading the warehouse relations.
    conn_doc = json.loads((Path(truth.world["workspace"]) / "bi" / "sigma" / "connections" /
                           "snowflake.json").read_text("utf-8"))
    conn = f"sigma:connection:{conn_doc['connectionId']}"
    add(id="G6", family="sigma", label="POST /v2/connections/{id}/sync (one table)",
        command=f"{py} sigma_sync", services=("sigma",),
        expect=[stoken, Expect("sigma.connections_list", "read", exactly(conn), exactly(conn)),
                Expect("sigma.connection_sync", "write", exactly(conn),
                       exactly("warehouse.fct_orders")), shim_py],
        value=lambda o, ctx: ((parse_result(o.stdout) or {}) == {
            "connectionId": conn_doc["connectionId"], "status": 200, "body": {}},
            f"{parse_result(o.stdout)}"))
    dm_doc = json.loads((Path(truth.world["workspace"]) / "bi" / "sigma" / "data-models" /
                         "commerce.json").read_text("utf-8"))
    dm_elements = {el["id"] for page in dm_doc["pages"] for el in page["elements"]}
    wb_sources = list(dict.fromkeys(el["source"]["elementId"]
                                    for el in wb_doc["contents"]["elements"]))

    def v_sigma_inspect(o, ctx):
        """The KPIs element's SQL, run independently on the warehouse in the
        connection's names, is what the element computes - after G5 published
        it on Gross Revenue; its columns are G5's formulas; its sources are the
        data model elements the workbook reads."""
        got = parse_result(o.stdout) or {}
        query, columns = got.get("query") or {}, (got.get("columns") or {}).get("entries", [])
        sql = re.sub(r"ANALYTICS\.WAREHOUSE\.([A-Z_]+)",
                     lambda m: f"wh.warehouse.{m.group(1).lower()}", query.get("sql") or "")
        row = truth.q(sql)[0] if sql else ()
        k = truth.kpis()
        formulas = [c.get("formula") for c in columns]
        sql_ok = (len(row) == 3 and row[0] == truth.gross_all() and row[1] == k["completed"]
                  and same_number(row[2], k["aov"]))
        cols_ok = (formulas == ["[Metrics/Gross Revenue]", "[Metrics/Completed Orders]",
                                "[Metrics/AOV]"]
                   and all(c.get("type") == {"type": "number"} for c in columns))
        src_ok = got.get("sources") == [{"type": "data-model",
                                         "dataModelId": dm_doc["dataModelId"],
                                         "elementIds": wb_sources}] and \
            set(wb_sources) <= dm_elements
        return sql_ok and cols_ok and src_ok and query.get("elementId") == got.get(
            "elementId"), (f"element SQL on the warehouse {list(row)} vs sql gross "
                           f"{truth.gross_all()} / n {k['completed']}: {sql_ok}; columns "
                           f"{formulas}: {cols_ok}; sources == the data model's "
                           f"{wb_sources}: {src_ok}")

    add(id="G7", family="sigma",
        label="GET element query + columns, workbook sources",
        command=f"{py} sigma_inspect", services=("sigma",),
        expect=[stoken, slist, sget,
                Expect("sigma.workbook_element_query", "read", exactly(wb), exactly(wb)),
                Expect("sigma.workbook_element_columns", "read", exactly(wb), exactly(wb)),
                Expect("sigma.workbook_sources", "read", exactly(wb), exactly(wb)),
                shim_py],
        value=v_sigma_inspect)

    # ------------------------------------------------------------ Jupyter
    out_rel = "data/e2e_notebook_out.json"

    def v_notebook(o, ctx):
        path = Path(truth.world["workspace"]) / out_rel
        try:
            got = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            return False, f"no output file: {exc}"
        parquet = Path(truth.world["workspace"]) / "data/exports/orders/part-00000.parquet"
        n = truth.q(f"SELECT COUNT(*) FROM read_parquet('{parquet.as_posix()}')")[0][0]
        k = truth.kpis()
        ok = (got.get("n_parquet") == n and same_number(got.get("net"), k["net"])
              and got.get("completed") == k["completed"])
        return ok, f"notebook {got} vs duckdb parquet={n} net={k['net']} n={k['completed']}"

    add(id="N1", family="jupyter", label="jupyter execute (nbclient, alkera-python)",
        command=f"jupyter execute --kernel_name=alkera-python {topo.script('e2e.ipynb')}",
        expect=[Expect("jupyter.cell", "exec", exactly()),
                Expect("jupyter.cell", "exec", exactly()),
                Expect("jupyter.cell", "exec", exactly(out_rel))],
        value=v_notebook,
        doc="a cell row is exec + the files it changed (the python shim's diff): "
            "what a cell READ (the parquet, the warehouse) is not in the row")

    def v_cell_write(o, ctx):
        gone = not truth.table_exists("main", "e2e_cell_scratch")
        return gone and o.returncode == 0, "the cell created and dropped its scratch table"

    add(id="N3", family="jupyter", label="a cell that writes the warehouse (scratch table)",
        command=f"jupyter execute --kernel_name=alkera-python {topo.script('write.ipynb')}",
        expect=[Expect("jupyter.cell", "exec", warehouse_named())], value=v_cell_write,
        doc="the cell row names the warehouse FILE (the shim's watch), not the table")

    if topo.name == "container":
        def v_server_cell(o, ctx):
            got = parse_result(o.stdout) or {}
            try:
                cell = json.loads((got.get("stdout") or "").strip().splitlines()[-1])
            except (ValueError, IndexError):
                return False, f"no cell output: {got}"
            k = truth.kpis()
            ok = (got.get("status") == "ok" and same_number(cell.get("net"), k["net"])
                  and cell.get("completed") == k["completed"])
            return ok, f"kernel {cell} vs sql net={k['net']} n={k['completed']}"

        add(id="N2", family="jupyter", label="a cell through jupyter.internal (REST + ws)",
            command=f"{py} jupyter_server_cell {topo.script('cell.py')}",
            expect=[Expect("jupyter.cell", "exec", exactly()), shim_py],
            value=v_server_cell,
            finding=lambda o, rows: (
                "the connection to jupyter.internal:8888 is in NO destinations.jsonl row: "
                "serve.py does not front jupyter-server (gateway/docker/jupyter/README.md, "
                "'Not executed')"))

    # ----------------------------------------------------------- unrouted
    paths = {"jira": "/rest/api/3/no-such-endpoint", "slack": "/api/no.such_method",
             "confluence": f"/wiki/rest/api/content/{KB_SLUG}/no-such",
             "powerbi": "/v1.0/myorg/no-such", "tableau": "/api/3.25/no-such",
             "sigma": "/v2/no-such"}
    vars_ = {"jira": "JIRA_URL", "slack": "SLACK_BASE_URL", "confluence": "CONFLUENCE_URL",
             "powerbi": "POWERBI_API_URL", "tableau": "TABLEAU_SERVER_URL",
             "sigma": "SIGMA_BASE_URL"}
    curl = "; ".join(f'curl -s -o /dev/null -w "%{{http_code}}\\n" "${vars_[s]}{p}"'
                     for s, p in paths.items())
    add(id="U1", family="unrouted", label="one unknown path per HTTP service (curl)",
        command=curl, services=tuple(paths),
        expect=[Expect(f"{s}.request" if s != "sigma" else "sigma.unknown_route", "read",
                       None, exit="error") for s in paths],
        value=lambda o, ctx: (o.stdout.split() == ["404"] * len(paths),
                              f"statuses {o.stdout.split()}"))
    # The first guesses the BI001 run (2026-10-04) made that the REAL products
    # do not serve: Confluence Cloud's REST lives under the site's /wiki; Jira
    # has no REST v4; Sigma's `contents` is PUT-only. They must stay 404.
    kb_id = f"$(cat {topo.script('kb_id')})"
    guesses = [("confluence", "CONFLUENCE_URL", f"/rest/api/content/{kb_id}"),
               ("confluence", "CONFLUENCE_URL", f"/api/v2/pages/{kb_id}"),
               ("jira", "JIRA_URL", f"/rest/api/4/issue/{JIRA_KEY}"),
               ("sigma", "SIGMA_BASE_URL", f"/v2/workbooks/{wb_doc['workbookId']}/contents")]
    add(id="U2", family="unrouted",
        label="guesses the real products do not serve either (curl)",
        command="; ".join(f'curl -s -o /dev/null -w "%{{http_code}}\\n" "${var}{path}"'
                          for _s, var, path in guesses),
        services=("confluence", "jira", "sigma"),
        expect=[Expect(f"{s}.request" if s != "sigma" else "sigma.unknown_route", "read",
                       exactly(), exit="error") for s, _v, _p in guesses],
        value=lambda o, ctx: (o.stdout.split() == ["404"] * len(guesses),
                              f"statuses {o.stdout.split()}"))
    return actions


# --------------------------------------------------------------------------
# running one action and judging it
# --------------------------------------------------------------------------
def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def read_destinations(audit_dir: Path) -> list[dict] | None:
    import blindness

    return blindness.load_destinations(audit_dir)


def judge(action: Action, outcome: Outcome, rows: list[dict], dest: dict,
          world: dict, ctx: dict, topo) -> dict:
    """One action's verdict, with every check's evidence."""
    schema = {r.get("seq"): validate_row(r, vnow=ctx["vnow_window"]) for r in rows}
    schema_bad = {k: v for k, v in schema.items() if v}
    class_problems = match_rows(action.expect, rows, ctx) if action.expect or rows else []
    wanted = {topo.destination(s) for s in action.services}
    moved = {key for key in dest}
    dest_ok = moved == wanted and all(v > 0 for v in dest.values())
    rc_ok = (action.returncode is None
             or (action.returncode == "nonzero" and outcome.returncode != 0)
             or outcome.returncode == action.returncode)
    value_ok, value_detail = (None, "n/a")
    if action.value is not None:
        try:
            value_ok, value_detail = action.value(outcome, ctx)
        except Exception as exc:  # noqa: BLE001 - a broken check is a failed check
            value_ok, value_detail = False, f"{type(exc).__name__}: {exc}"
    finding = action.finding(outcome, rows) if action.finding else ""
    disagree = ctx["world_judgement"].get("named_by_a_row_but_unattributed_by_blindness")
    if disagree:
        finding = "; ".join(filter(None, [finding, (
            f"{disagree} is named by this action's own row, but "
            f"blindness._unattributed does not accept that writer as an explanation: "
            f"the harness would quarantine this HARD")]))
    transcript = ctx["world_judgement"].get("transcript_audited") or []
    if transcript:
        finding = "; ".join(filter(None, [finding, (
            f"{transcript} changed and NO row names it: the binary is not shimmed, so "
            f"in native mode only the transcript (and rollout.py's post-episode "
            f"`workspace.edit` row) accounts for it")]))
    failures = []
    if not rc_ok:
        failures.append(f"exit code {outcome.returncode} (expected {action.returncode}): "
                        f"{(outcome.stderr or outcome.stdout)[-300:]}")
    if class_problems:
        failures += class_problems
    if schema_bad:
        failures.append(f"schema: {schema_bad}")
    if not dest_ok:
        failures.append(f"destinations moved {sorted(moved)} expected {sorted(wanted)}")
    if value_ok is False:
        failures.append(f"value: {value_detail}")
    if not ctx["world_judgement"]["ok"]:
        failures.append(f"world: {ctx['world_judgement']}")
    blocked = bool(finding) and not rc_ok and action.id == "T6"
    if blocked:
        verdict = "FINDING"
    elif failures:
        verdict = "FAIL"
    elif finding:
        verdict = "FINDING"
    elif action.doc:
        verdict = "DOC"
    else:
        verdict = "PASS"
    return {
        "id": action.id, "family": action.family, "label": action.label,
        "command": action.command, "returncode": outcome.returncode,
        "seconds": outcome.seconds,
        "rows_expected": len(action.expect), "rows_got": len(rows),
        "tools": [r.get("tool") for r in rows],
        "rows": [{k: r.get(k) for k in ("seq", "tool", "op_class", "targets",
                                         "read_targets", "exit", "error", "flags")}
                 for r in rows],
        "schema_ok": not schema_bad, "schema_problems": schema_bad,
        "classification_ok": not class_problems, "classification_problems": class_problems,
        "destinations": {f"{h}:{p}": n for (h, p), n in sorted(dest.items())},
        "destinations_expected": sorted(f"{h}:{p}" for h, p in wanted),
        "destinations_ok": dest_ok,
        "value_ok": value_ok, "value": value_detail,
        "world": ctx["world_judgement"],
        "doc": action.doc, "finding": finding,
        "failures": failures if verdict == "FAIL" else [],
        "verdict": verdict,
        "stdout_tail": outcome.stdout[-600:], "stderr_tail": outcome.stderr[-600:],
    }


def run_drive(topo, world: dict, truth: Truth) -> list[dict]:
    import episode

    audit = topo.audit_dir / "actions.jsonl"
    vnow = json.loads(Path(world["scheduler"]).read_text("utf-8"))["vnow"]
    # The engineer's scripts and notebook: outside the world, like ~/scripts.
    topo.put("clients.py", CLIENTS_PY)
    topo.put("e2e.ipynb", write_notebook(
        topo.work / "scripts" / "e2e.ipynb",
        notebook_cells(topo.workspace, topo.warehouse,
                       f"{topo.workspace}/data/e2e_notebook_out.json")).read_text("utf-8"))
    topo.put("kb_body", KB_BODY + "\n")
    topo.put("write.ipynb", write_notebook(topo.work / "scripts" / "write.ipynb", [
        "import duckdb\n"
        f"con = duckdb.connect({topo.warehouse!r})\n"
        "con.execute('CREATE TABLE main.e2e_cell_scratch AS SELECT 1 AS n')\n"
        "con.execute('DROP TABLE main.e2e_cell_scratch')\n"
        "con.close()"]).read_text("utf-8"))
    topo.put("cell.py", (
        "import duckdb, json\n"
        f"con = duckdb.connect({topo.warehouse!r}, read_only=True)\n"
        "net, n = con.execute(\"SELECT SUM(order_amount), COUNT(*) FROM warehouse.warehouse."
        "fct_orders WHERE order_status = 'completed'\").fetchone()\n"
        "con.close()\nprint(json.dumps({'net': str(net), 'completed': n}))\n"))
    results = []
    ctx: dict = {"vnow": vnow,
                 "warehouse": topo.warehouse,
                 "local_csv": lambda: (topo.work / "scripts" / "kpis.csv"
                                       if topo.name == "host" else _pull(topo, "kpis.csv"))}
    for action in drive(topo, truth):
        before_rows = len(read_rows(audit))
        before_dest = read_destinations(topo.audit_dir)
        before_tree = episode.tree_manifest(Path(world["world_root"]))
        clock_before = json.loads(Path(world["scheduler"]).read_text("utf-8"))["vnow"]
        outcome = topo.run(action.command)
        clock_after = json.loads(Path(world["scheduler"]).read_text("utf-8"))["vnow"]
        ctx["vnow_window"] = tuple(dict.fromkeys((clock_before, clock_after)))
        rows = read_rows(audit)[before_rows:]
        after_tree = episode.tree_manifest(Path(world["world_root"]))
        dest = destination_delta(before_dest, read_destinations(topo.audit_dir))
        ctx["rows"] = rows
        ctx["world_judgement"] = explain_world(before_tree, after_tree, rows, world,
                                               topo.container_paths())
        result = judge(action, outcome, rows, dest, world, ctx, topo)
        results.append(result)
        print(f"  {result['verdict']:<8}{action.id:<5}{action.label}", flush=True)
        if action.id == "H5":
            # The engineer copies the id the listing returned into the next command.
            kb_id = (parse_result(outcome.stdout) or {}).get("id") or ""
            topo.put("kb_id", str(kb_id))
    return results


def _pull(topo, name: str) -> Path:
    """Copy a file the episode wrote under its scripts dir back to the host."""
    local = topo.work / "pulled" / name
    local.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["docker", "cp", f"{topo.stack.container_id}:{topo.script(name)}",
                    str(local)], capture_output=True, text=True)
    return local


# --------------------------------------------------------------------------
# episode-wide checks
# --------------------------------------------------------------------------
def episode_checks(topo, world: dict, rows: list[dict], before: dict, after: dict,
                   results: list[dict]) -> dict:
    import agent_under_test as aut
    import blindness

    from alkera_gateway.audit import validate_trace

    out: dict = {}
    try:
        validate_trace(topo.audit_dir / "actions.jsonl")
        out["trace_valid"] = True
    except Exception as exc:  # noqa: BLE001
        out["trace_valid"] = f"{type(exc).__name__}: {exc}"
    out["rows_total"] = len(rows)
    out["rows_attributed_to_actions"] = sum(r["rows_got"] for r in results)
    out["no_stray_rows"] = out["rows_total"] == out["rows_attributed_to_actions"]
    destinations = read_destinations(topo.audit_dir)
    if topo.name == "host":
        allowed = aut.allowed_destinations(topo.endpoints)
    else:
        allowed = aut.allowed_destinations(topo.stack.episode_endpoints())
    violations = blindness.network_violations(destinations, allowed, aut.normalise_host)
    out["network_violations"] = [str(v) for v in violations]
    out["destinations"] = destinations
    unexplained = blindness._unattributed(before, after, rows, world, topo.container_paths())
    workspace = blindness.workspace_rel(world)
    out["episode_world_unexplained_outside_workspace"] = [
        p for p in unexplained if not p.startswith(workspace + "/")]
    out["episode_world_changed"] = changed_paths(before, after)
    # Which clock each kind of writer stamped: the services carry the world's
    # `vnow`; a writer with no scheduler state to read falls back to a constant.
    vts: dict[str, set] = {}
    for row in rows:
        prefix = str(row.get("tool", "")).split(".", 1)[0]
        writer = "service" if prefix in (*PLUGGED_HTTP, "sched") else prefix
        vts.setdefault(writer, set()).add(row.get("vts"))
    out["vts_by_writer"] = {k: sorted(v) for k, v in sorted(vts.items())}
    out["ok"] = (out["trace_valid"] is True and out["no_stray_rows"]
                 and not out["network_violations"]
                 and not out["episode_world_unexplained_outside_workspace"])
    return out


def observations(evidence: dict) -> list[str]:
    """What the reader must know that is not one action's verdict."""
    notes = []
    env = (evidence.get("stack") or {}).get("subject_env") or {}
    creds = ("JIRA_USER", "JIRA_API_TOKEN", "SLACK_BOT_TOKEN", "CONFLUENCE_USER",
             "CONFLUENCE_API_TOKEN")
    missing = [v for v in creds if v not in env]
    if missing:
        notes.append(f"the subject environment carries no {missing} (the BI vendors' "
                     f"credentials it does carry): the clients read them from the "
                     f"workspace .envrc, whose URLs are the *.internal ones")
    clocks = (evidence.get("episode") or {}).get("vts_by_writer") or {}
    service = set(clocks.get("service") or [])
    for writer, stamps in clocks.items():
        if writer != "service" and service and set(stamps) - service:
            notes.append(f"rows written by `{writer}` carry vts {stamps}; the services' "
                         f"rows carry the world's {sorted(service)}")
    res = evidence.get("resolution") or {}
    if res.get("excluded_env_present"):
        notes.append(f"the episode environment still names excluded services "
                     f"{sorted(res['excluded_env_present'])} (profile_overlay's "
                     f"`!reset null` does not remove them)")
    for name, resolves in (res.get("named_by_envrc_but_unplugged") or {}).items():
        notes.append(f"{name}.internal (named by .envrc/onboarding) "
                     f"{'RESOLVES' if resolves else 'does not resolve'} on this profile")
    seen = []
    for r in evidence.get("actions") or []:
        for text in (r.get("finding"), r.get("doc")):
            if text and text not in seen:
                seen.append(text)
    return notes + seen


def container_checks(topo) -> dict:
    """Name resolution from inside the episode, plugged vs excluded, and P7."""
    names = {n: f"{n}.internal" for n in (*PLUGGED_HTTP, "jupyter", *EXCLUDED_HTTP,
                                          "postgres", "snowflake")}
    probe = ("import json,socket,sys\nout={}\nfor n,h in json.loads(sys.argv[1]).items():\n"
             "  try:\n    out[n]=sorted({a[4][0] for a in socket.getaddrinfo(h,None)})\n"
             "  except OSError as e:\n    out[n]=None\nprint(json.dumps(out))\n")
    done = topo.exec_raw(["/usr/local/bin/python", "-c", probe, json.dumps(names)])
    resolved = json.loads(done.stdout) if done.returncode == 0 else {}
    out = {"resolved": resolved,
           "plugged_resolve": {n: bool(resolved.get(n)) for n in (*PLUGGED_HTTP, "jupyter")},
           "excluded_do_not_resolve": {n: not resolved.get(n) for n in EXCLUDED_HTTP},
           "named_by_envrc_but_unplugged": {n: bool(resolved.get(n))
                                            for n in ("postgres", "snowflake")}}
    # The overlay `!reset`s the excluded services' variables
    # (container_episode.profile_overlay): none of them may reach the episode.
    excluded_vars = ("SCHED_URL", "METABASE_URL", "PROMETHEUS_URL", "ALERTMANAGER_URL",
                     "LOKI_URL", "AIRBYTE_URL", "DEPLOY_URL", "AWS_ENDPOINT_URL")
    out["excluded_env_present"] = {v: topo.container_env[v] for v in excluded_vars
                                   if v in topo.container_env}
    out["ok"] = (all(out["plugged_resolve"].values())
                 and all(out["excluded_do_not_resolve"].values()))
    return out


def containment(topo, work: Path) -> dict:
    """The harness's own P7 for this rollout (`containment_probe`), then the
    repository's `containment.py --native-episode` against the same project."""
    out: dict = {}
    grading = Path(topo.world["grading"])
    grading.mkdir(parents=True, exist_ok=True)
    try:
        evidence = topo.stack.containment_probe(when="after", canaries=[],
                                                grading=grading,
                                                rollout_dir=topo.rollout_dir)
        checks = evidence.get("checks") or {}
        out["containment_probe"] = {
            "contained": evidence.get("contained"), "checks": checks,
            "failed": sorted(name for name, ok in checks.items() if ok is False),
            "services_probed": sorted(r.get("service") for r in evidence.get("services") or []
                                      if isinstance(r, dict)),
            "note": "P7 probes NATIVE_SERVICES + the scheduler plane only: the plugged BI "
                    "vendors are not in its service list, and a profile with no "
                    "orchestrator cannot pass `the_scheduler_plane_answers_with_jobs`"}
    except Exception as exc:  # noqa: BLE001 - reported, not hidden
        out["containment_probe"] = {"error": f"{type(exc).__name__}: {exc}"}
    script = REPO / "gateway" / "docker" / "containment.py"
    target = work / "containment-native-episode.json"
    argv = [sys.executable, str(script), "--native-episode", "--project", topo.project,
            "--world", topo.world["world_root"], "--out", str(target)]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=900)
    out["containment_py"] = {"command": " ".join(argv), "returncode": done.returncode,
                             "stdout_tail": done.stdout[-1500:],
                             "stderr_tail": done.stderr[-1500:]}
    if target.exists():
        try:
            evidence = json.loads(target.read_text("utf-8"))
            out["containment_py"]["passed"] = evidence.get("passed")
            checks = ((evidence.get("p7_native_episode_filesystem") or {}).get("checks")
                      or evidence.get("checks") or {})
            out["containment_py"]["failed_checks"] = sorted(
                name for name, ok in checks.items() if ok is False) \
                if isinstance(checks, dict) else checks
        except ValueError:
            pass
    return out


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------
def render_table(results: list[dict]) -> str:
    """rows = expected/got; schema = 9.2.2 + vts; class = tool, op_class,
    targets (and read_targets where known); dest = destinations.jsonl; value =
    against an independent DuckDB query / fixture; world = every change
    explained by the action's rows."""
    head = (f"{'id':<5}{'tool / action':<50}{'rows':>7}  {'op_class':<20}{'schema':<7}"
            f"{'class':<6}{'dest':<5}{'value':<6}{'world':<6}verdict")
    lines = [head, "-" * len(head)]
    mark = {True: "ok", False: "BAD", None: "n/a"}
    for r in results:
        classes = ",".join(sorted({row["op_class"] for row in r["rows"]})) or "-"
        lines.append(
            f"{r['id']:<5}{(r['family'] + ': ' + r['label'])[:49]:<50}"
            f"{str(r['rows_expected']) + '/' + str(r['rows_got']):>7}  {classes[:19]:<20}"
            f"{mark[r['schema_ok']]:<7}{mark[r['classification_ok']]:<6}"
            f"{mark[r['destinations_ok']]:<5}{mark[r['value_ok']]:<6}"
            f"{mark[r['world']['ok']]:<6}{r['verdict']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--topology", choices=("host", "container"), default="host")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--work", type=Path, default=None,
                        help="scratch root (default ~/alkera-e2e/<topology>)")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--keep", action="store_true", help="leave the stack up")
    args = parser.parse_args(argv)
    work = args.work or WORK_ROOT / args.topology
    out_path = args.out or EVIDENCE / f"bi_e2e_{args.topology}.json"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    import episode

    started = time.time()
    print(f"\n  bi e2e accuracy check - topology {args.topology}, work {work}")
    world = build_world(work)
    truth = Truth(world)
    topo = (HostTopology(world, work) if args.topology == "host"
            else ContainerTopology(world, work, args.image))
    evidence: dict = {"topology": args.topology, "profile": PROFILE_NAME, "plugs": PLUGS,
                      "world": world, "seed": seed_text(SEED_EXAMPLE.read_text("utf-8")),
                      "revision": _git_rev(), "started": started}
    try:
        evidence["stack"] = topo.up()
        before = episode.tree_manifest(Path(world["world_root"]))
        results = run_drive(topo, world, truth)
        after = episode.tree_manifest(Path(world["world_root"]))
        rows = read_rows(topo.audit_dir / "actions.jsonl")
        evidence["episode"] = episode_checks(topo, world, rows, before, after, results)
        if args.topology == "container":
            evidence["resolution"] = container_checks(topo)
            evidence["containment"] = containment(topo, work)
        evidence["actions"] = results
    finally:
        if not args.keep:
            topo.down()
    evidence["seconds"] = round(time.time() - started, 1)
    table = render_table(results)
    counts = {v: sum(r["verdict"] == v for r in results)
              for v in ("PASS", "DOC", "FINDING", "FAIL")}
    evidence["summary"] = {**counts, "episode_ok": evidence["episode"]["ok"],
                           "resolution_ok": (evidence.get("resolution") or {}).get("ok")}
    evidence["table"] = table
    evidence["observations"] = observations(evidence)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(evidence, indent=1, sort_keys=True, default=str) + "\n",
                        encoding="utf-8")
    print("\n" + table)
    for r in results:
        if r["verdict"] == "FAIL":
            print(f"\n  FAIL {r['id']}: " + "\n       ".join(r["failures"]))
        elif r["verdict"] in ("DOC", "FINDING"):
            print(f"  {r['verdict']} {r['id']}: {r['finding'] or r['doc']}")
    episode_view = {k: v for k, v in evidence["episode"].items()
                    if k not in ("destinations", "episode_world_changed")}
    print(f"\n  episode: {json.dumps(episode_view, default=str)[:1200]}")
    if "resolution" in evidence:
        print(f"  resolution: {json.dumps(evidence['resolution'])[:1200]}")
        print(f"  containment: {json.dumps(evidence['containment'], default=str)[:1500]}")
    for note in evidence["observations"][:8]:
        print(f"  note: {note}")
    print(f"\n  {counts}  evidence: {out_path}")
    bad = counts["FAIL"] or not evidence["episode"]["ok"] or (
        args.topology == "container" and not evidence["resolution"]["ok"])
    return 1 if bad else 0


def _git_rev() -> str:
    done = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    return done.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
