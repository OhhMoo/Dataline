"""The grading side of the BI vendors' published state (`{bi_state}` in grade_argv).

A BI task's outcome is what each vendor's SERVICE publishes - the data source
Tableau Server serves, the workbook version Sigma serves, the semantic model
Power BI was last deployed with - and that state is deliberately not in the
world tree: `GatewayConfig.bi_state_dir` must be outside it (gateway/src/
alkera_gateway/config.py), and defaults to `<audit_dir>/bi`. The agent's repo
holding a fix and the server serving it are two different facts, which is the
point (gateway/src/alkera_gateway/native/services/{powerbi,tableau,sigma}.py).

Where that is, per topology - the harness never sets `bi_state_dir`, so it is
always the default, and the audit directory is always `<rollout>/audit`:

    host, mcp / native   the gateway (or native/serve.py) runs on the host with
                         audit_dir=<rollout>/audit           -> <rollout>/audit/bi
    container, native    native-services runs `serve --audit /audit`, /audit
                         being a bind of <rollout>/audit     -> <rollout>/audit/bi
    container, mcp       the in-container gateway's audit_dir is /audit, the
                         same bind                           -> <rollout>/audit/bi

Two rules, the publication ledger's (tasks/harness/ledger.py):

1. **The grader reads a copy in the grading zone.** `stage()` runs after
   teardown and before the grader, copies the state into
   `<grading>/bi_state/`, and returns that path for `{bi_state}`. The live
   directory belongs to the services that just served the subject; a grader
   that read it would be one path mistake from reading what the subject could
   still change.
2. **It imports nothing from the gateway or the world.** It copies files. The
   task's grader re-renders every number from the staged files with
   world.semantic (rule 2 of world/world/semantic/__init__.py); caches are
   left behind on purpose: a Power BI import snapshot (`snapshots/*.duckdb`)
   and a Tableau extract copy (`extracts/*.duckdb`) are what a SERVER renders
   from, never outcome, and they are the bulk of the bytes.

No state (a task with no BI vendor, or a world whose services were never
touched) stages nothing and `{bi_state}` substitutes to the empty string, the
same contract as `{ledger}`; a grader must treat that as "published = as
provisioned".
"""
from __future__ import annotations

import shutil
from pathlib import Path

#: Where the harness's services keep it, relative to the rollout directory.
AUDIT_REL = Path("audit") / "bi"
#: Where stage() puts the copy inside the grading zone.
STAGED_REL = Path("bi_state")
#: Cache files a server renders from, never graded (see the module docstring),
#: and the services' own lock / temp files.
SKIP_DIRS = ("snapshots", "extracts")
SKIP_SUFFIXES = (".lock", ".tmp", ".duckdb", ".duckdb.wal")


def source_for(rollout_dir) -> Path:
    """The default `bi_state_dir` of a rollout's services (see the table above)."""
    return Path(rollout_dir) / AUDIT_REL


def _ignore(directory: str, names: list[str]) -> set[str]:
    return {n for n in names
            if n in SKIP_DIRS and (Path(directory) / n).is_dir()
            or n.endswith(SKIP_SUFFIXES)}


def stage(bi_state_dir, grading_dir) -> Path | None:
    """Copy the services' published state into the grading zone. Returns the
    copy, or None when there is no state (never an error).

    Called by the harness AFTER teardown and BEFORE the grader runs. A previous
    stage of the same zone (a regrade, the second of grade_twice) is replaced
    whole, so a file the services removed cannot survive in the copy.
    """
    source = Path(bi_state_dir) if bi_state_dir else None
    if source is None or not source.is_dir() or not any(source.iterdir()):
        return None
    target = Path(grading_dir) / STAGED_REL
    if target.resolve().is_relative_to(source.resolve()):
        raise ValueError(f"refusing to stage {source} into itself ({target})")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=_ignore)
    return target


def resolve(grading_dir) -> Path | None:
    """The staged copy under a grading zone, or None."""
    staged = Path(grading_dir) / STAGED_REL
    return staged if staged.is_dir() else None
