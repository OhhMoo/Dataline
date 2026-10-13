"""World fingerprint: the two determinism gates plus a diagnostic.

- semantic_hash  — THE GATE. Canonical logical contents: warehouse schemas +
  canonically-ordered rows, scheduler state (incl. v-clock), BI definitions,
  workspace files (dbt project, landing CSVs, docs), and — only when the theme
  seeds them — the ingestion control-plane state (var/ingest/state.json), the
  live-world control plane (var/world/live.json, which carries the fault
  catalogue and the ledger of what fired), the observability store
  (var/obs/: metric definitions plus the appended sample record), and the
  environment profile the seed built for (var/world/profile.json).
  Equal across hosts <=> same world. Comparison excludes `git_head`
  (informational).
- provenance     — attestation: seed hash, generator + dependency versions,
  requirements.lock hash, image digest.
- raw_byte_hash  — diagnostic only (duckdb page layout may differ across
  hosts/versions); used as THE GATE for snapshot restore, where bytes must
  match exactly.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb

from .hashing import canon_value, file_hash, json_canon, json_hash, tree_hash

DB = Path("var/warehouse/warehouse.duckdb")
SCHEMAS = ("raw", "staging", "warehouse")


def _warehouse_hash(db_path: Path) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    catalog = db_path.stem  # duckdb names the catalog after the file stem
    tables = {}
    try:
        rows = con.execute(
            "select table_schema, table_name from information_schema.tables "
            "where table_schema in ('raw','staging','warehouse') "
            # BASE TABLES only. A view is a deterministic function of the tables
            # it reads, so hashing it is redundant - and a view whose body uses a
            # 2-part name breaks when the catalog shares the schema's name.
            "and table_type = 'BASE TABLE' order by 1, 2"
        ).fetchall()
        for schema, table in rows:
            cols = con.execute(
                "select column_name, data_type from information_schema.columns "
                "where table_schema = ? and table_name = ? "
                "order by ordinal_position", [schema, table]).fetchall()
            h = hashlib.sha256()
            h.update(json_canon({"table": f"{schema}.{table}", "columns": cols}))
            order = ", ".join(f'"{c}"' for c, _ in cols)
            # three-part name: schema `warehouse` collides with the catalog name
            cur = con.execute(
                f'select * from "{catalog}"."{schema}"."{table}" order by {order}')
            for row in cur.fetchall():
                h.update(json_canon([canon_value(c) for c in row]) + b"\n")
            tables[f"{schema}.{table}"] = h.hexdigest()
    finally:
        con.close()
    return {"tables": tables, "hash": json_hash(tables)}


def semantic(root: Path) -> dict:
    root = Path(root)
    sections = {
        "warehouse": _warehouse_hash(root / DB),
        "scheduler": json_hash(json.loads(
            (root / "var" / "sched" / "state.json").read_text(encoding="utf-8"))),
        "bi": tree_hash(root / "var" / "bi"),
        "workspace": tree_hash(root / "workspace", exclude=(".git",)),
    }
    # Conditional sections: a world only gets one when it actually seeds the
    # surface, so a world without it keeps its recorded semantic hash.
    #   ingest — the connector control plane (a theme's write_ingest hook)
    #   live   — the live-world control plane: schedule, exec blocks, arrival
    #            stream and how far the traffic generator has run. It is not
    #            reachable from any gateway tool, but it IS world state, so it
    #            is hashed: a tampered live.json must not pass as the same world.
    for name, path in (("ingest", root / "var" / "ingest" / "state.json"),
                       ("live", root / "var" / "world" / "live.json")):
        if path.is_file():
            sections[name] = json_hash(json.loads(path.read_text(encoding="utf-8")))
    #   obs — the observability store (gateway/SURFACES.md): the metric and
    #         alarm definitions a theme seeds, AND samples.jsonl, the record of
    #         what those metrics read as the world moved. Both are world state.
    #         OPS-SURFACE's patch shape hashed the parsed definitions YAML; this
    #         hashes the whole directory byte-exact instead, because the samples
    #         file is generated here and must reproduce byte for byte anyway,
    #         and because an append-only record whose bytes are not pinned is
    #         not evidence. Conditional like the rest: a world that seeds no
    #         definitions writes no samples, has no var/obs, and keeps its
    #         recorded hash.
    obs = root / "var" / "obs"
    if obs.is_dir():
        sections["obs"] = tree_hash(obs)
    #   workflow — the issue tracker, the chat workspace and the wiki
    #         (gateway/SURFACES.md#workflow). The seeded backlog is part of
    #         what makes two builds the same world, and the agent writes into
    #         these same documents during an episode, so they are world state
    #         on the same footing as var/sched/state.json.
    #
    #         tree_hash, not json_hash of the parsed documents, on the obs
    #         precedent and for a sharper reason: the build writes them with
    #         ensure_ascii=True and the gateway rewrites them with
    #         ensure_ascii=False, so hashing bytes is what forces the seed to
    #         stay ASCII (world/workflow.py refuses anything else) instead of
    #         letting the file change under a world nobody changed.
    #         Conditional like the rest: a theme with no workflow corpus has no
    #         var/workflow and keeps its recorded hash.
    workflow = root / "var" / "workflow"
    if workflow.is_dir():
        sections["workflow"] = tree_hash(workflow)
    #   home - the box's HOME, written only when the seed says `native: true`
    #         (world/world/native_workspace.py): ~/.dbt/profiles.yml and
    #         ~/.config/alkera/services.toml. Those two files are part of what
    #         the agent discovers its stack from, so a world whose home was
    #         edited after the build is not the world that was built - and
    #         until this section existed it passed `semantic_hash` unchanged,
    #         the runner-up finding in gateway/docker/NATIVE_STACK.md.
    #         Conditional like the rest, and load-bearingly so: every world
    #         built without the flag has no home/ and keeps its recorded hash.
    home = root / "home"
    if home.is_dir():
        sections["home"] = tree_hash(home)
    #   warehouses - an episode may run a SECOND live warehouse (the source or
    #         the target of a migration). Every additional *.duckdb beside the
    #         primary is hashed by the same rule the primary gets: canonical
    #         logical contents, not file bytes. Conditional like the rest, so a
    #         world with one warehouse finds nothing here and keeps its recorded
    #         hash - which is the whole reason this is a new section rather than
    #         a change to the "warehouse" one.
    #
    #         A warehouse that does NOT live in the tree (postgres, snowflake)
    #         cannot be reached from here and is not silently skipped: it is
    #         covered by world/warehouse_seed.logical_hash, which produces this
    #         same canonical shape from a live server, and the gateway already
    #         refuses to boot such a world without an explicit initial_world_fp
    #         (GatewayConfig in gateway/src/alkera_gateway/config.py).
    extra = sorted(
        path for path in (root / "var" / "warehouse").glob("*.duckdb")
        if path.name != DB.name
    )
    if extra:
        sections["warehouses"] = {p.stem: _warehouse_hash(p) for p in extra}
    #   profile - which environment slots the world was built for, written
    #         only when the seed says `profile:` (build.resolve_profile). The
    #         planes it unplugs already change other sections by being absent,
    #         but not always: a theme with no ingest hook builds the same tree
    #         with or without the `ingest` slot. Two profiles over one seed
    #         are still two worlds - the gateway serves them different tools -
    #         so the record itself is hashed. Conditional like the rest: an
    #         unprofiled world has no record and keeps its recorded hash.
    profile = root / "var" / "world" / "profile.json"
    if profile.is_file():
        sections["profile"] = json_hash(json.loads(profile.read_text(encoding="utf-8")))
    return sections


def semantic_hash(root: Path) -> str:
    return json_hash(semantic(root))


# git_head runs in the AGENT's repository once an episode has run (and on the
# host), and the agent owns `.git/config`: git runs there exactly as
# gateway/src/alkera_gateway/safegit.py runs it - no system/global config, no
# GIT_* inherited, fsmonitor/hooks/attributes/protocols off, GIT_DIR pinned to a
# plain `.git` directory. world/ does not import the gateway, so the env and
# argv are a private copy, pinned equal by gateway/tests/test_safegit.py.
def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""})
    return env


def _git_argv(repo: Path) -> list[str]:
    return ["git", "-c", "safe.directory=*", "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null",
            "-c", "protocol.allow=never", "-C", str(repo)]


def git_head(root: Path) -> str | None:
    workspace = (Path(root) / "workspace").absolute()
    git_dir = workspace / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return None
    env = {**_git_env(), "GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(workspace)}
    try:
        r = subprocess.run([*_git_argv(workspace), "rev-parse", "HEAD"],
                           capture_output=True, env=env, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.decode().strip() if r.returncode == 0 else None


def report(root: Path) -> dict:
    root = Path(root)
    sem = semantic(root)
    prov_path = root / "build" / "provenance.json"
    return {
        "semantic": sem,
        "semantic_hash": json_hash(sem),
        "git_head": git_head(root),
        "raw_byte_hash": file_hash(root / DB),
        "provenance": json.loads(prov_path.read_text(encoding="utf-8"))
        if prov_path.exists() else None,
    }


def main(argv=None):
    p = argparse.ArgumentParser(prog="world.fingerprint")
    p.add_argument("root", type=Path)
    p.add_argument("--full", action="store_true", help="print the full report")
    a = p.parse_args(argv)
    if a.full:
        print(json.dumps(report(a.root), indent=2, sort_keys=True))
    else:
        print(semantic_hash(a.root))


if __name__ == "__main__":
    sys.exit(main())
