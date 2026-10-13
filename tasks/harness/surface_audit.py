"""Pre-episode surface audit: which of this world's surfaces are actually there.

WHY THIS EXISTS. GT004 ran five scored Opus rollouts in a world where eight of
the agent's surfaces answered `no_issue_tracker` / `no_chat` /
`no_knowledge_base` / `no_observability` / `no_ingest_state`. The harness
PRINTED that - at the bottom of the report, under the number - and did nothing
with it. The audit log shows the subject calling four of those surfaces exactly
once, getting nothing, and never looking again. The scores were real; the world
they were measured in was half absent, and a Pass@1 measured in a half-absent
world looks exactly like a Pass@1 measured in a whole one.

So a dead surface is a run OUTCOME here, not a footnote:

  required + dead   the rollout never starts. `episode.HarnessError`, classified
                    `harness_error`, never scored. This follows blindness.py's
                    precedent rather than inventing a mechanism: a compromised
                    episode is quarantined instead of being let into the number.
                    The justification is different from blindness's but no
                    weaker - the subject was never given the world the task
                    describes, so whatever it scored is not a measurement OF
                    that task.

  dead_by_design    declared in task.toml, with a reason. A deliberately minimal
                    world must be declarable or it is flagged forever, and a
                    flag that fires forever stops being read.

  dead, neither     the rollout RUNS - we do not know the surface mattered - but
                    the run is NOT a clean measurement, and report.py says so
                    above the rate instead of below it. Silence in exactly this
                    cell is what produced GT004.

THE CEILING. `surface_probes` is what the task thought to check; it is a floor,
not a ceiling. Every gateway tool the task does not probe is swept too, called
with no arguments, so a dead surface cannot hide behind a tool nobody declared.
A tool that needs arguments cannot be swept blind - that is recorded as
`unreachable`, never silently counted as live. Measured basis for the sweep
being read-only in fact (gateway/.venv, 2026-09-14): of the gateway's 63 tools
exactly 23 validate against `{}`, and of those only dbt_run and dbt_test mutate
or cost anything, so both are denied below. Every other tool rejects `{}` at
pydantic validation with `invalid_arguments` - before any handler runs.

Probes, swept or declared, run against a THROWAWAY COPY of the world with their
own audit directory (episode.probe_surfaces / container_shim.cmd_probe). That is
what makes the sweep safe structurally rather than by naming convention.

Under `--driver native` there is no gateway to call and the probe becomes a
service health check. `audit()`, `gate_error()` and everything downstream are
unchanged; only `probe_native()` differs, and what that costs is written down
where it is implemented - see the NATIVE MODE section below.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# An error code that says THE WORLD HAS NO SUCH SURFACE, as opposed to "your
# call was wrong". Read out of the gateway, not guessed:
#   gateway/src/alkera_gateway/ingest.py:34          no_ingest_state
#   gateway/src/alkera_gateway/observability.py:538  no_observability
#   gateway/src/alkera_gateway/workflow.py:118       no_issue_tracker
#   gateway/src/alkera_gateway/workflow.py:296       no_chat
#   gateway/src/alkera_gateway/workflow.py:433       no_knowledge_base
ABSENT_SURFACE_CODES = frozenset({
    "no_ingest_state", "no_observability", "no_issue_tracker",
    "no_chat", "no_knowledge_base",
})

# A `no_*` code this table does not know is SUSPECT, never automatically fatal.
# The gateway also raises `no_output` for a scheduler run that captured none -
# a fact about one run, not about an absent surface - so a regex that gated on
# its own would quarantine a healthy world. Suspects are reported and reviewed.
SUSPECT_ABSENT = re.compile(r"^no_[a-z0-9_]+$")

# The probe call could not be FORMED, so it says nothing about the world.
INCONCLUSIVE_CODES = frozenset({"invalid_arguments", "unknown_tool",
                                "request_too_large"})

# The same fact - THIS WORLD HAS NO SUCH SURFACE - as told by a native health
# check rather than by a gateway error. Kept as its own set because the codes
# above are read out of the gateway's source and these are decided here.
# `no_content` is the GT004 case in native clothes: the service is up, and the
# world behind it is empty, which is a dead surface however healthy the process.
# The gateway-less surfaces speak the same way: each names the one fact that
# makes it unusable, and every one of them is an ABSENCE and not an ambiguity,
# so each is dead rather than suspect. `SUSPECT_ABSENT` below would otherwise
# catch them all by shape and report a surface nobody can use as "we could not
# tell", which is the one verdict that is certainly wrong here.
NATIVE_ABSENT_CODES = frozenset({"no_content", "health_not_ok",
                                 "no_working_tree", "no_docs",
                                 "no_git_repository", "no_dbt_toolchain",
                                 "no_dbt_profile", "no_scheduler",
                                 "no_scheduler_state"})

# Swept with `{}`, these would actually run: dbt_run/dbt_test mutate the copy and
# cost minutes. dbt_compile already speaks for both via `covers` on every task
# that probes dbt at all.
SWEEP_DENY = frozenset({"dbt_run", "dbt_test"})


def declared_tools(probes: list[dict]) -> set[str]:
    """Every tool a task's own probes speak for, directly or via `covers`."""
    out = set()
    for p in probes or ():
        out.add(p["tool"])
        out.update(p.get("covers") or ())
    return out


def sweep_probes(toolbelt, probes: list[dict]) -> list[dict]:
    """Zero-argument probes for every tool the task did not declare.

    Marked `declared: false` so the audit never confuses "the task checked this"
    with "we checked this because the task did not".
    """
    covered = declared_tools(probes)
    return [{"label": f"~{tool}", "tool": tool, "args": {}, "declared": False}
            for tool in sorted(set(toolbelt) - covered - SWEEP_DENY)]


def all_probes(toolbelt, probes: list[dict]) -> list[dict]:
    declared = [{**p, "declared": True} for p in (probes or ())]
    return declared + sweep_probes(toolbelt, probes)


def _verdict(surface: dict) -> str:
    """live | dead | unreachable | suspect, from one probe result."""
    if surface.get("live"):
        return "live"
    code = surface.get("error_code")
    if code is None:
        # pre-audit rollouts, and transport failures, carry only the json blob
        blob = surface.get("error") or ""
        for known in ABSENT_SURFACE_CODES:
            if known in blob:
                return "dead"
        for known in INCONCLUSIVE_CODES:
            if known in blob:
                return "unreachable" if not surface.get("declared", True) else "dead"
        return "dead" if surface.get("declared", True) else "unreachable"
    if code in ABSENT_SURFACE_CODES or code in NATIVE_ABSENT_CODES:
        return "dead"
    if code in INCONCLUSIVE_CODES:
        # a DECLARED probe that will not even validate is the task's bug and is
        # still a surface nobody can use, so it stays dead. A SWEPT one just
        # means we could not form a blind call.
        return "dead" if surface.get("declared", True) else "unreachable"
    if SUSPECT_ABSENT.match(code):
        return "suspect"
    # any other error on a declared probe: the task asked, the world said no.
    return "dead" if surface.get("declared", True) else "suspect"


def audit(*, spec, surfaces: dict, toolbelt) -> dict:
    """The run condition, computed once per rollout, before the subject runs."""
    got = (surfaces or {}).get("surfaces") or {}
    probed = bool((surfaces or {}).get("probed"))

    by_verdict: dict[str, list[str]] = {"live": [], "dead": [],
                                        "unreachable": [], "suspect": []}
    tools_of: dict[str, set[str]] = {}
    for label, s in got.items():
        by_verdict[_verdict(s)].append(label)
        tools_of[label] = {s.get("tool", label)} | set(s.get("covers") or ())
    for k in by_verdict:
        by_verdict[k].sort()

    dead = set(by_verdict["dead"])

    # required_surfaces may name a probe LABEL or any tool that probe speaks
    # for; taskspec.load has already refused anything that names neither.
    required = list(spec.required_surfaces)
    required_labels = sorted({lbl for lbl in got
                              for r in required
                              if r == lbl or r in tools_of.get(lbl, ())})
    dead_required = sorted(set(required_labels) & dead)

    by_design = {d["label"]: d["reason"] for d in spec.dead_by_design}
    dead_by_design = sorted(set(by_design) & dead)
    stale_by_design = sorted(set(by_design) & set(by_verdict["live"]))
    dead_undeclared = sorted(dead - set(by_design) - set(dead_required))

    covered = declared_tools(spec.surface_probes)
    swept = {s.get("tool") for lbl, s in got.items() if not s.get("declared", True)}
    unprobed = sorted(set(toolbelt) - covered - swept)
    # swept but the call could not be formed: known-unknown, not live
    unswept = sorted({got[line].get("tool", line) for line in by_verdict["unreachable"]})

    if not probed:
        gate, reason = "unprobed", (
            "the task declares no surface_probes, so no surface was verified "
            "live before the subject was handed the terminal")
    elif dead_required:
        gate, reason = "fail", (
            f"required surfaces are DEAD in this world: {dead_required}. The "
            f"subject was never given the world the task describes, so anything "
            f"it scored is not a measurement of this task.")
    elif not required:
        gate, reason = "not_declared", (
            "the task declares no [harness].required_surfaces, so the gate had "
            "nothing to enforce; no dead surface can be fatal here until it does")
    else:
        gate, reason = "pass", (
            f"all {len(required_labels)} required surfaces live: {required_labels}")

    not_clean = []
    if gate == "fail":
        not_clean.append(reason)
    if gate == "unprobed":
        not_clean.append(reason)
    if dead_undeclared:
        not_clean.append(
            f"dead surfaces this task neither requires nor declares dead by "
            f"design: {dead_undeclared}. Each is a hole in the world the agent "
            f"was measured in, and the task has not said whether it matters.")
    if by_verdict["suspect"]:
        not_clean.append(
            f"surfaces whose answer could not be read as live or dead - an "
            f"unrecognised absence-shaped gateway code, or a native health "
            f"body the gate could not interpret: "
            f"{by_verdict['suspect']}")

    return {
        "probed": probed,
        "gate": gate,
        "gate_reason": reason,
        "clean": not not_clean,
        "not_clean_because": not_clean,
        "live": by_verdict["live"],
        "dead": sorted(dead),
        "suspect": by_verdict["suspect"],
        "unreachable_by_sweep": by_verdict["unreachable"],
        "required": required,
        "required_labels": required_labels,
        "dead_required": dead_required,
        "dead_by_design": dead_by_design,
        "dead_by_design_declared": sorted(by_design),
        "dead_by_design_but_live": stale_by_design,
        "dead_undeclared": dead_undeclared,
        "declared_probes": sum(1 for s in got.values() if s.get("declared", True)),
        "swept_probes": sum(1 for s in got.values() if not s.get("declared", True)),
        "unprobed_tools": unprobed,
        "unswept_tools": unswept,
    }


# =========================================================== NATIVE MODE ====
# `--driver native` (gateway/NATIVE.md) has no gateway to probe: the
# surfaces are services, and the question "is this surface there" becomes "does
# the service answer". So a native probe is a health check - GET <base>/health
# for each fake service, `SELECT 1` through psycopg for the warehouse, HEAD on
# the bucket for object storage, and `is_dir()` for the working tree.
#
# Everything downstream is unchanged on purpose. `audit()` reads the same
# surface dict, `gate_error()` raises the same HarnessError, and a dead REQUIRED
# surface is still harness_error and still never scored. Only the probe differs.
#
# THE CHECK IS CONTENT-BEARING, and it has to be. A liveness probe that only
# asks "is the process up" cannot tell a live service over an EMPTY world from a
# live service over a populated one - which is GT004 exactly, in native clothes.
# So every native check asks for a count and requires it to be non-zero:
#
#   service   GET /health -> {"ok": true, "service": <name>, "items": <n>}
#             ok AND items > 0            -> live
#             ok AND items == 0           -> dead (`no_content`)
#             ok missing / body not json  -> suspect (`no_health_schema`)
#             items missing               -> suspect (`no_item_count`)
#   sql       connect; and, WHEN THE TASK DECLARES `[harness].warehouse_schemas`,
#             find a table with at least one row in them. A task that declares
#             none gets the MCP probe's own question - `sql_query("SELECT 1")` -
#             because GT008's primary warehouse is empty at t0 by design and a
#             native gate stricter than the MCP gate would make one task.toml
#             describe two tasks. The warehouse is a
#             DuckDB FILE for every task in the corpus (NATIVE.md,
#             amendment 2026-09-16), so the check opens it through an ALIASED
#             READ-ONLY attach - `connect(":memory:")` then `ATTACH <file> AS wh
#             (READ_ONLY)` - which takes no write lock on the world's warehouse
#             and sidesteps the catalog/schema name collision a direct
#             `connect(file)` has when the file is called `warehouse.duckdb`
#             and carries a schema called `warehouse`. A task that declares
#             `warehouse_backend: postgres` keeps the psycopg path.
#   dbt       `dbt` resolves on PATH AND the profiles.yml under DBT_PROFILES_DIR
#             names an output whose `path` IS this world's warehouse file. Both
#             halves, because a dbt that is installed and profiled against
#             another file builds a warehouse nobody is grading, and a profile
#             that is right with no dbt behind it builds nothing at all.
#   sched     `var/sched/state.json` parses and declares at least one job. The
#             content rule in its own clothes: a scheduler with no jobs is a
#             surface `sched_list`, `sched_trigger` and `sched_logs` can do
#             nothing with.
#   docs      `workspace/docs` is a directory with at least one file in it -
#             what `Operations.docs_list` reads.
#   s3        the bucket exists AND holds at least one object
#   workspace the working tree is a directory holding at least one file
#   git       ...and it is a git repository with at least one commit, which is
#             what `git_log` and `git_diff` need
#
# A body the gate cannot read is SUSPECT, never live and never dead: an
# unreadable answer is a fact about the service contract, not about the world,
# and a gate that guessed either way would be lying in one direction.
#
# THE GATE'S OWN POLLING IS AUDITED. Each service records the /health request it
# served as a 9.2.2 row with `targets: ["health:<service>"]` and its real
# read_targets (SERVICES-B). That is deliberate - a surface check that left no
# trace could not be reviewed - and it is also why the `health:` namespace is
# harness bookkeeping everywhere downstream: without that, polling nine services
# before the episode would inject their read_targets into the episode's target
# set and a rollout would appear to have read things it never asked for. See
# blindness.is_harness_row.

NATIVE_SERVICE_NAMES = ("jira", "slack", "confluence", "metabase", "prometheus",
                        "alertmanager", "loki", "airbyte", "deploy")
#: The explicit-only BI vendors (`agent_under_test.PLUG_SERVICES`). Separate
#: from the nine for the same reason the harness keeps them separate: the nine
#: are the unprofiled box. They are swept as `~<name>`, like the nine, only
#: when the endpoint map carries them - which only a profile that plugs one
#: does (`agent_under_test.profile_endpoints`) - so an unprofiled gate checks
#: exactly what it always checked.
NATIVE_PLUG_SERVICE_NAMES = ("powerbi", "tableau", "sigma")

# Which native surface a task's gateway-tool probe is really asking about. The
# task packages declare their probes in MCP terms (`issues_list`, `sql_query`),
# and they must keep doing so: the same task.toml has to describe the same task
# under both drivers or the two measurements are not comparable.
NATIVE_SURFACE_OF = {
    "issues_": "jira",
    "chat_": "slack",
    "kb_": "confluence",
    # docs_* is NOT confluence. `Operations.docs_list/docs_get/docs_search`
    # read `workspace/docs` through `files.Workspace` - the confluence service
    # serves `kb_*` and nothing else (native/services/confluence.py) - so the
    # native docs surface is the directory the MCP family actually reads.
    # Mapping it at the service would have reported a live surface whenever the
    # knowledge base was up, in a world whose `docs/` was empty.
    "docs_": "docs",
    "bi_": "metabase",
    "metrics_": "prometheus",
    "alarms_": "alertmanager",
    "logs_": "loki",
    "ingest_": "airbyte",
    "deploy_": "deploy",
    "sql_": "sql",
    "s3_": "s3",
    # dbt is its own surface, not the warehouse's. The warehouse answering says
    # nothing about whether `dbt` is installed or whether its profile points at
    # that warehouse, and both are what `dbt_compile` proved under MCP.
    "dbt_": "dbt",
    # `sched_*` is the world's scheduler - a state file plus the `./ops/sched`
    # CLI the native stack materialises, not a server. `sched` and `sched_log`
    # are the same plane and the same check.
    "sched_": "sched",
    # fs_* and git_* are the shell and the working tree: in native mode they are
    # the transport itself. `fs_` asks whether the tree is there and has
    # anything in it; `git_` asks the strictly stronger question the git tools
    # need, which is whether it is a repository with a commit in it.
    "fs_": "workspace",
    "git_": "git",
    # The explicit-only BI vendors have no MCP tools at all (the gateway's bi_*
    # speak to the Metabase-shaped fake); a native task that requires one names
    # a `<vendor>_health` probe, which is checked as the vendor's /health -
    # what the `~<vendor>` sweep already asks, under a label the task can
    # require. Only ever reached through a profiled endpoint map.
    "powerbi_": "powerbi",
    "tableau_": "tableau",
    "sigma_": "sigma",
}

#: The surface a `sql_*`/`dbt_*` probe names when it addresses a SECOND
#: warehouse (`sql_query(warehouse="legacy")`, GT008's `sql_legacy` probe).
#: Kept as a prefix rather than a fixed name because the warehouses a world
#: runs are the world's to declare (episode.WORLD_WAREHOUSE_KEYS).
NAMED_WAREHOUSE_PREFIX = "sql:"

NATIVE_CHECK_KIND = {**{n: "http_health"
                        for n in (*NATIVE_SERVICE_NAMES, *NATIVE_PLUG_SERVICE_NAMES)},
                     "sql": "sql", "s3": "s3", "workspace": "path",
                     "git": "git", "docs": "path", "dbt": "dbt",
                     "sched": "sched"}


def native_surface_for(tool: str, args: dict | None = None) -> str | None:
    """The native surface a gateway tool name maps to, or None if unmapped.

    `args` is the probe's own argument dict, and only one key in it changes the
    answer: a `warehouse` naming a second warehouse makes the probe a question
    about THAT file. Without it GT008's `sql` and `sql_legacy` probes would both
    resolve to the primary and the gate would report the legacy warehouse live
    on the strength of the new one.
    """
    warehouse = (args or {}).get("warehouse")
    for prefix, surface in NATIVE_SURFACE_OF.items():
        if tool.startswith(prefix):
            if surface == "sql" and warehouse:
                return f"{NAMED_WAREHOUSE_PREFIX}{warehouse}"
            return surface
    return None


def native_check_kind(surface: str) -> str:
    """Which check runs for a surface, including the named-warehouse forms."""
    if surface.startswith(NAMED_WAREHOUSE_PREFIX):
        return "sql"
    return NATIVE_CHECK_KIND.get(surface, "unmapped")


def health_verdict(body: object, where: str) -> tuple[bool, str | None, str]:
    """`{"ok": bool, "service": str, "items": int}` -> (live, code, detail).

    Pure, so both directions are testable without a service. See the table at
    the top of this section for why each cell is what it is.
    """
    if not isinstance(body, dict) or "ok" not in body:
        return (False, "no_health_schema",
                f"{where} -> body is not a health document: "
                f"{json.dumps(body)[:120]}")
    if not body.get("ok"):
        return (False, "health_not_ok", f"{where} -> ok=false")
    if "items" not in body:
        return (False, "no_item_count",
                f"{where} -> ok, but the body carries no `items` count, so "
                f"whether the world behind it has anything in it is unknown")
    items = body.get("items")
    if not isinstance(items, int):
        return (False, "no_item_count",
                f"{where} -> `items` is {type(items).__name__}, not an int")
    if items <= 0:
        return (False, "no_content",
                f"{where} -> ok, {items} items: the service is up and the world "
                f"behind it is empty")
    return (True, None, f"{where} -> ok, {items} items")


def _http_health(base: str, timeout: float) -> tuple[bool, str | None, str]:
    """`GET <base>/health`, verdict from the BODY whatever the status.

    A service whose world has no such plane answers **503 with a health
    document** - `{"ok": false, "service": ..., "error": "no_ingest_state"}` -
    because that is the contract `native/context.service_health` writes. Reading
    only the status code there filed it as `health_http_503`, which no table
    knows, so a swept surface came out SUSPECT ("we could not read the answer")
    when the answer was perfectly readable and said the surface is absent.
    Measured on GT008's `~airbyte`, whose world seeds no ingest plane.
    """
    import urllib.error
    import urllib.request
    url = base.rstrip("/") + "/health"
    status = 200
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw, status = resp.read(65536), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(65536), exc.code
    except Exception as exc:                            # noqa: BLE001 - recorded
        return (False, "service_unreachable", f"{url} -> {type(exc).__name__}: {exc}")
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        body = raw.decode("utf-8", "replace")[:120]
    live, code, detail = health_verdict(body, f"{url} -> {status}")
    if live and not 200 <= status < 300:
        # A body that says `ok: true` under a failure status is a service
        # contradicting itself, and neither answer may be believed.
        return (False, "no_health_schema",
                f"{url} -> {status} with a body claiming ok")
    return (live, code, detail)


# How many tables the warehouse check will open before it gives up looking for
# one with a row in it. A bound, because a warehouse with a thousand empty
# staging tables must not turn the surface gate into a table scan.
SQL_TABLE_SAMPLE = 25

# Every table in these schemas, in every non-system schema, when the task has
# not said which it cares about.
SQL_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

SQL_TABLES = """\
SELECT n.nspname, c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm')
  AND n.nspname <> ALL(%(system)s)
  AND (%(schemas)s IS NULL OR n.nspname = ANY(%(schemas)s))
ORDER BY n.nspname, c.relname
"""


def warehouse_verdict(tables: list[tuple[str, str]], nonempty: list[str],
                      schemas: list[str], where: str
                      ) -> tuple[bool, str | None, str]:
    """(live, code, detail) from what the warehouse scan found.

    Pure, so the content rule is testable without a warehouse. `tables` is every
    candidate the scan saw, `nonempty` the ones that returned a row.

    THE CONTENT RULE BINDS ONLY WHERE THE TASK DECLARED WHAT IT EXPECTS, which
    is what `schemas` - `[harness].warehouse_schemas` - is. A warehouse that
    answers and is empty is LIVE when the task named no schemas, and the reason
    is GT008 measured: its primary warehouse `nova` holds no table at t0
    BECAUSE BUILDING IT IS THE TASK, and the MCP probe for the same surface is
    `sql_query(sql = "SELECT 1")`, which asks nothing about content and passes.
    A native gate stricter than the MCP gate on the same `task.toml` would mean
    the file described two different tasks under the two drivers, which
    `gateway/NATIVE.md` forbids outright - the two measurements would
    not be comparable, and comparing them is the whole experiment.

    The count is reported either way, so an empty warehouse is always visible in
    the audit; what changes is whether it stops the rollout.
    """
    scope = f"schemas {schemas}" if schemas else "every non-system schema"
    if not tables:
        detail = f"{where} answers and holds no table at all in {scope}"
        return ((False, "no_content", detail) if schemas else
                (True, None, f"{detail}; the task declares no "
                             f"[harness].warehouse_schemas, so the content rule "
                             f"does not bind and this is the MCP probe's own "
                             f"question - can SQL run here - answered"))
    if not nonempty:
        detail = (f"{where} answers, and all {len(tables)} table(s) sampled in "
                  f"{scope} are empty")
        return ((False, "no_content", detail) if schemas else
                (True, None, f"{detail}; the task declares no "
                             f"[harness].warehouse_schemas, so emptiness is "
                             f"reported and not fatal"))
    return (True, None,
            f"{where} -> {len(tables)} table(s) in {scope}, "
            f"non-empty: {nonempty[:3]}")


def _sql_content(pg: dict, schemas: list[str], timeout: float
                 ) -> tuple[bool, str | None, str]:
    """Connect, then prove the warehouse holds something, not just that it is up."""
    where = f"{pg['host']}:{pg['port']}/{pg['database']}"
    try:
        import psycopg
        from psycopg import sql as pgsql
    except ImportError as exc:
        # fails CLOSED: a check we cannot form says nothing about the world, and
        # a required surface nobody could verify must not start a rollout
        return (False, "probe_driver_missing", f"psycopg is not installed: {exc}")
    try:
        with psycopg.connect(host=pg["host"], port=int(pg["port"]),
                             user=pg["user"], dbname=pg["database"],
                             password=pg["password"],
                             connect_timeout=int(max(1, timeout))) as con:
            con.execute("SELECT 1").fetchone()
            tables = con.execute(SQL_TABLES, {
                "system": list(SQL_SYSTEM_SCHEMAS),
                "schemas": list(schemas) or None}).fetchall()
            nonempty = []
            for ns, rel in tables[:SQL_TABLE_SAMPLE]:
                stmt = pgsql.SQL("SELECT 1 FROM {}.{} LIMIT 1").format(
                    pgsql.Identifier(ns), pgsql.Identifier(rel))
                if con.execute(stmt).fetchone():
                    nonempty.append(f"{ns}.{rel}")
                    break
        return warehouse_verdict([tuple(t) for t in tables], nonempty,
                                 list(schemas), where)
    except Exception as exc:                            # noqa: BLE001 - recorded
        return (False, "service_unreachable",
                f"warehouse {where} -> {type(exc).__name__}: {exc}")


def _s3_bucket_content(endpoints: dict, timeout: float
                       ) -> tuple[bool, str | None, str]:
    """HEAD the bucket, then list one object. An empty bucket is a dead surface.

    Signed with boto3 and the workspace's own obviously fake credentials rather
    than raw HTTP, because an unsigned list against MinIO answers 403 and a 403
    tells you nothing about whether the bucket has anything in it.
    """
    bucket = endpoints["s3_bucket"]
    where = f"s3://{bucket} at {endpoints['s3']}"
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        return (False, "probe_driver_missing", f"boto3 is not installed: {exc}")
    try:
        client = boto3.client(
            "s3", endpoint_url=endpoints["s3"],
            aws_access_key_id=endpoints["s3_access_key"],
            aws_secret_access_key=endpoints["s3_secret_key"],
            region_name=endpoints["s3_region"],
            config=Config(signature_version="s3v4",
                          s3={"addressing_style": "path"},
                          connect_timeout=int(max(1, timeout)),
                          read_timeout=int(max(1, timeout)),
                          retries={"max_attempts": 1}))
        client.head_bucket(Bucket=bucket)
        listing = client.list_objects_v2(Bucket=bucket, MaxKeys=1)
    except Exception as exc:                            # noqa: BLE001 - recorded
        return (False, "service_unreachable", f"{where} -> "
                f"{type(exc).__name__}: {exc}")
    n = int(listing.get("KeyCount", len(listing.get("Contents") or [])))
    if n <= 0:
        return (False, "no_content", f"{where} exists and holds no object")
    return (True, None, f"{where} -> at least {n} object")


# The catalog alias the warehouse file is attached under. Anything but the
# file's own stem: `connect(<...>/warehouse.duckdb)` names the catalog
# `warehouse`, the same word as the schema every task's marts live in, and
# `warehouse.fct_orders` then resolves to a catalog with no such schema. The
# alias is what `world/world/fingerprint.py:54` records hitting the same way.
DUCKDB_ALIAS = "wh"

DUCKDB_TABLES = """\
SELECT schema_name, table_name FROM duckdb_tables()
WHERE database_name = ? ORDER BY schema_name, table_name
"""


def _duckdb_content(path: str, schemas: list[str], timeout: float
                    ) -> tuple[bool, str | None, str]:
    """Open the world's DuckDB warehouse read-only and prove it holds rows.

    `timeout` is accepted and unused: a local file either opens or raises, and
    a parameter that silently did nothing would be worse than one that says so.
    The same `warehouse_verdict` renders the answer as the Postgres path's, so
    the content rule has one statement and two transports.
    """
    where = str(path)
    if not path or not Path(path).is_file():
        return (False, "service_unreachable",
                f"the world declares its warehouse at {where or '<unset>'} and "
                f"there is no file there")
    try:
        import duckdb
    except ImportError as exc:
        # fails CLOSED, like the psycopg branch: a check we cannot form says
        # nothing about the world.
        return (False, "probe_driver_missing", f"duckdb is not installed: {exc}")
    quoted = Path(path).as_posix().replace("'", "''")
    try:
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                f"ATTACH '{quoted}' AS {DUCKDB_ALIAS} (READ_ONLY)")
            connection.execute(f"USE {DUCKDB_ALIAS}")
            tables = [
                (str(ns), str(rel)) for ns, rel in
                connection.execute(DUCKDB_TABLES, [DUCKDB_ALIAS]).fetchall()
                if not schemas or str(ns) in schemas
            ]
            nonempty = []
            for ns, rel in tables[:SQL_TABLE_SAMPLE]:
                statement = (f'SELECT 1 FROM {DUCKDB_ALIAS}."{ns}"."{rel}" '
                             f"LIMIT 1")
                if connection.execute(statement).fetchone():
                    nonempty.append(f"{ns}.{rel}")
                    break
        finally:
            connection.close()
    except Exception as exc:                            # noqa: BLE001 - recorded
        return (False, "service_unreachable",
                f"warehouse {where} -> {type(exc).__name__}: {exc}")
    return warehouse_verdict(tables, nonempty, list(schemas), where)


def _warehouse_path(world: dict, surface: str) -> str:
    """The DuckDB file a `sql`/`sql:<name>` surface is a question about.

    The primary comes from the manifest's `warehouse`; a named one from the
    `warehouses` specs the world publishes (episode.WORLD_WAREHOUSE_KEYS), so
    the harness never guesses a path from a name.
    """
    if not surface.startswith(NAMED_WAREHOUSE_PREFIX):
        return str(world.get("warehouse") or "")
    wanted = surface[len(NAMED_WAREHOUSE_PREFIX):]
    for spec in world.get("warehouses") or []:
        if isinstance(spec, dict) and str(spec.get("name")) == wanted:
            return str(spec.get("path") or "")
    return ""


def _sql_surface(world: dict, surface: str, endpoints: dict,
                 schemas: list[str], timeout: float
                 ) -> tuple[bool, str | None, str]:
    """The warehouse check, sent to whichever backend this world actually runs.

    DuckDB unless the world says otherwise, which is the amendment's decision
    and not a default chosen here: `world/world/executor.py` runs every
    scheduled job against `var/warehouse/*.duckdb`, dbt targets it and every
    grader reads it, so a gate that asked Postgres would report `sql_*` and
    `dbt_*` DEAD for every task in the corpus - which is exactly what it did
    until 2026-09-16.
    """
    if str(world.get("warehouse_backend") or "duckdb") == "duckdb":
        return _duckdb_content(_warehouse_path(world, surface), schemas, timeout)
    return _sql_content(endpoints["postgres"], schemas, timeout)


def dbt_executable() -> str | None:
    """The `dbt` the agent's shell would reach, ignoring the shim standing in
    front of it.

    Resolved the way `alkera_gateway.commands.Commands.dbt` resolves it - the
    interpreter's own scripts directory first, because a venv Python launched by
    absolute path does not put that directory on PATH - and with the shim
    directory removed, because `native_stack` puts a `dbt` WRAPPER at the front
    of PATH and a wrapper with no binary behind it runs nothing.
    """
    import shutil

    shim_bin = os.environ.get("ALKERA_SHIM_BIN")
    entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if e]
    if shim_bin:
        resolved = Path(shim_bin).resolve(strict=False)
        entries = [e for e in entries
                   if Path(e).resolve(strict=False) != resolved]
    return (shutil.which("dbt", path=str(Path(sys.executable).parent))
            or shutil.which("dbt", path=os.pathsep.join(entries)))


def dbt_profile_targets_warehouse(profiles_dir: str, warehouse: str
                                  ) -> tuple[bool, str]:
    """Does an output in profiles.yml name THIS world's warehouse file?

    Compared as RESOLVED paths, so the `../var/warehouse/warehouse.duckdb` a
    checked-in profile spells and the absolute path the world builder renders
    (`world/world/executor.write_profiles`) are one answer. A profile whose
    path is a jinja `env_var(...)` expression resolves to itself and matches
    nothing, which is the honest answer: the gate cannot know what dbt would
    render it to.
    """
    path = Path(profiles_dir or ".") / "profiles.yml"
    try:
        import yaml

        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:                            # noqa: BLE001 - recorded
        return (False, f"{path} -> {type(exc).__name__}: {exc}")
    if not warehouse:
        return (False, "this world's manifest declares no warehouse file")
    wanted = Path(warehouse).resolve(strict=False)
    seen = []
    for profile in document.values():
        if not isinstance(profile, dict):
            continue
        for name, output in (profile.get("outputs") or {}).items():
            if not isinstance(output, dict) or not output.get("path"):
                continue
            candidate = Path(str(output["path"]))
            if not candidate.is_absolute():
                candidate = Path(profiles_dir or ".") / candidate
            seen.append(f"{name}={output['path']}")
            if candidate.resolve(strict=False) == wanted:
                return (True, name)
    return (False, f"outputs {seen or '(none)'} name no path that resolves to "
                   f"{wanted}")


def _dbt_toolchain(profiles_dir: str, warehouse: str
                   ) -> tuple[bool, str | None, str]:
    """`dbt` on PATH and profiled against this world's warehouse, or dead.

    Any output counts, not only the default `target`: a box that plugs a
    remote warehouse (world.native_workspace.box_profiles_yaml) defaults to
    that warehouse and keeps the DuckDB target this check finds
    (test_backends.test_surface_gate_accepts_a_box_profile_defaulting_to_the_backend)."""
    executable = dbt_executable()
    if executable is None:
        return (False, "no_dbt_toolchain",
                "no `dbt` on PATH outside the shim directory, so every dbt_* "
                "tool's native equivalent would exit 127")
    ok, detail = dbt_profile_targets_warehouse(profiles_dir, warehouse)
    if not ok:
        return (False, "no_dbt_profile",
                f"{executable} is installed and {profiles_dir}/profiles.yml "
                f"builds another warehouse: {detail}")
    return (True, None,
            f"{executable}, target {detail!r} in {profiles_dir}/profiles.yml "
            f"-> {warehouse}")


def scheduler_verdict(state: object, where: str) -> tuple[bool, str | None, str]:
    """`var/sched/state.json` -> (live, code, detail). Pure, both directions."""
    if state is None:
        return (False, "no_scheduler",
                f"{where} is not there, so this world has no scheduler plane")
    if not isinstance(state, dict) or "jobs" not in state:
        return (False, "no_scheduler_state",
                f"{where} is not a scheduler state document")
    jobs = state["jobs"]
    count = len(jobs) if isinstance(jobs, (dict, list)) else 0
    if count <= 0:
        return (False, "no_content",
                f"{where} parses and declares no job, so sched_list, "
                f"sched_trigger and sched_logs have nothing to work on")
    return (True, None, f"{where} -> {count} job(s)")


def _scheduler_state(path: str) -> tuple[bool, str | None, str]:
    where = str(path or "<unset>")
    if not path or not Path(path).is_file():
        return scheduler_verdict(None, where)
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return (False, "no_scheduler_state",
                f"{where} -> {type(exc).__name__}: {exc}")
    return scheduler_verdict(state, where)


def _tree_files(root: Path, limit: int = 200) -> int:
    """How many files are under `root`, `.git` excluded, up to `limit`."""
    seen = 0
    for current, directories, files in os.walk(root):
        directories[:] = [d for d in directories if d != ".git"]
        seen += len(files)
        if seen >= limit:
            break
    return seen


def _path_present(path: str, *, what: str = "working tree",
                  code: str = "no_working_tree") -> tuple[bool, str | None, str]:
    """A directory that exists AND holds at least one file.

    Content-bearing like every other native check: an empty `workspace/docs` is
    a `docs_list` that answers nothing, which is the GT004 failure in the one
    plane that has no service in front of it.
    """
    if not path or not Path(path).is_dir():
        return (False, code, f"{what} {path or '<unset>'} -> absent")
    files = _tree_files(Path(path))
    if files <= 0:
        return (False, "no_content",
                f"{what} {path} is a directory and holds no file")
    return (True, None, f"{what} {path} -> at least {files} file(s)")


def _git_repo(workspace: str) -> tuple[bool, str | None, str]:
    """The working tree is a git repository with a commit in it.

    `git_log` and `git_diff` are the tools this stands for and both need a
    HEAD, so `rev-parse HEAD` is the question rather than `.git` existing: a
    freshly `git init`ed tree has the directory and answers neither tool.
    """
    import shutil
    import subprocess

    present, code, detail = _path_present(workspace)
    if not present:
        return (present, code, detail)
    if not (Path(workspace) / ".git").exists():
        return (False, "no_git_repository",
                f"{workspace} has no .git, so git_log and git_diff have no "
                f"history to read")
    git = shutil.which("git")
    if git is None:
        return (False, "probe_driver_missing", "no `git` on PATH")
    result = subprocess.run([git, "rev-parse", "HEAD"], cwd=workspace,
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        return (False, "no_content",
                f"{workspace} is a git repository with no commit: "
                f"{(result.stderr or '').strip()[:120]}")
    return (True, None, f"{workspace} -> git HEAD {result.stdout.strip()[:12]}")


def _run_native_check(surface: str, endpoints: dict, world: dict,
                      schemas: list[str], timeout: float,
                      served_planes: frozenset = frozenset()
                      ) -> tuple[bool, str | None, str]:
    workspace = str(world.get("workspace") or "")
    kind = native_check_kind(surface)
    address = "s3" if kind == "s3" else surface
    if (surface in served_planes or kind in ("http_health", "s3")) \
            and address not in endpoints:
        # agent_under_test.profile_endpoints dropped it: the task's environment
        # profile does not serve this surface, so there is nothing to dial.
        return (False, "excluded_by_profile",
                f"the environment profile serves no {surface!r}")
    if surface in served_planes:
        # THE GATE MUST ASK THE PLANE THE SUBJECT CAN REACH. Under the container
        # topology the episode mounts the workspace and the warehouse files and
        # nothing else, so `var/sched/state.json` - which the host check reads -
        # is not on the subject's filesystem at all: a gate that read it would
        # report a live scheduler the subject cannot touch, which is precisely
        # the misattribution this module exists to prevent. There the plane is
        # served (`sched.internal`) and its `/health` counts the same jobs.
        # `served_planes` is empty on the host topology, where the CLI runs
        # locally and the file IS the surface.
        return _http_health(endpoints[surface], timeout)
    if kind == "http_health":
        return _http_health(endpoints[surface], timeout)
    if kind == "sql":
        return _sql_surface(world, surface, endpoints, schemas, timeout)
    if kind == "dbt":
        return _dbt_toolchain(str(world.get("profiles_dir") or ""),
                              str(world.get("warehouse") or ""))
    if kind == "sched":
        return _scheduler_state(str(world.get("scheduler") or ""))
    if kind == "s3":
        return _s3_bucket_content(endpoints, timeout)
    if kind == "path":
        if surface == "docs":
            return _path_present(str(Path(workspace) / "docs") if workspace
                                 else "", what="docs directory",
                                 code="no_docs")
        return _path_present(workspace)
    if kind == "git":
        return _git_repo(workspace)
    return (False, "native_surface_unmapped",
            f"native mode has no health check for {surface!r}")


def warehouse_schemas(spec) -> list[str]:
    """`[harness].warehouse_schemas` - which schemas the task's world lives in.

    Read straight off the task's own toml through `TaskSpec.raw`, the way the
    MCP gate reads `required_surfaces`: the harness does not invent the list,
    and a task that declares none gets the broader check (every non-system
    schema) rather than a skipped one.
    """
    harness = (getattr(spec, "raw", None) or {}).get("harness") or {}
    return [str(x) for x in (harness.get("warehouse_schemas") or [])]


def probe_native(*, probes: list[dict], endpoints: dict, world: dict,
                 schemas: list[str] | None = None,
                 timeout: float = 5.0,
                 served_planes: frozenset = frozenset()) -> dict:
    """Health-check this episode's native surfaces, before the subject runs.

    Returns the same `{"probed": bool, "surfaces": {...}}` shape
    `episode.probe_surfaces` returns, so `audit()` and `classify()` need no
    native branch at all.

    `served_planes` names the surfaces this TOPOLOGY serves over HTTP rather
    than off the world's filesystem - `{"sched"}` under `--topology container`,
    nothing on the host. It is the caller's to declare, because which planes the
    subject can reach is a property of the container the caller built and a
    harness that guessed at it would be reporting a surface nobody probed.

    Two sources, as under MCP. The task's DECLARED probes keep their labels, so
    `required_surfaces` resolves exactly as it does under the gateway. Every
    service the task did not speak for is swept as `~<name>`, because "the task
    did not ask about Loki" and "Loki is up" are different facts and GT004 is
    what happens when they are printed as one.
    """
    checks: dict[str, dict] = {}
    for probe in probes or ():
        label = probe.get("label") or probe["tool"]
        surface = native_surface_for(probe["tool"], probe.get("args"))
        checks[label] = {"surface": surface, "tool": probe["tool"],
                         "covers": list(probe.get("covers") or []),
                         "declared": True}
    spoken_for = {c["surface"] for c in checks.values()}
    for name in (*NATIVE_SERVICE_NAMES, *NATIVE_PLUG_SERVICE_NAMES):
        # a profiled endpoint map names only what the profile serves, and is
        # the only kind that can name a BI vendor at all
        if name not in spoken_for and name in endpoints:
            checks[f"~{name}"] = {"surface": name, "tool": f"{name}.health",
                                  "covers": [], "declared": False}
    if not checks:
        return {"probed": False, "surfaces": {}, "native": True}

    cache: dict[str, tuple] = {}
    surfaces: dict[str, dict] = {}
    for label, c in checks.items():
        surface = c["surface"]
        if surface is None:
            live, code, detail = (
                False, "native_surface_unmapped",
                f"no native surface is defined for the tool {c['tool']!r}, so "
                f"this probe cannot be evaluated in native mode")
        else:
            if surface not in cache:
                cache[surface] = _run_native_check(surface, endpoints, world,
                                                   list(schemas or ()), timeout,
                                                   served_planes)
            live, code, detail = cache[surface]
        surfaces[label] = {
            "tool": c["tool"], "covers": c["covers"], "declared": c["declared"],
            "native_surface": surface,
            "check": ("http_health" if surface in served_planes
                      else native_check_kind(surface) if surface else "unmapped"),
            "live": live,
            "error_code": code,
            "error": None if live else json.dumps({"code": code,
                                                   "detail": detail})[:300],
            "result_summary": detail[:300],
        }
    return {"probed": True, "surfaces": surfaces, "native": True}


def gate_error(a: dict) -> str | None:
    """The HarnessError text when this rollout must not start, else None.

    Only `fail` stops a rollout. `unprobed` and `not_declared` are conditions on
    the REPORT, not on the run: refusing to run a task that has not yet declared
    its requirements would make this module decide which tasks exist, and that
    is the task's call, not the harness's.
    """
    if a.get("gate") != "fail":
        return None
    return ("dead required surface: " + a["gate_reason"] +
            " Declare it in [harness].dead_by_design (with a reason) if the "
            "world is meant to be without it, or fix the world builder.")
