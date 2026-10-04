"""The BI projects: each plugged BI vendor's dashboards-as-code, checked into the repo.

WHY. On a team that ships Power BI, Tableau or Sigma, the dashboards are not
clicked together in a browser: the semantic model and the reports are text in
the same repository as the dbt project, changed by pull request and deployed or
refreshed through the vendor's CLI and REST API. The fake vendor services
(gateway/src/alkera_gateway/native/services/{powerbi,tableau,sigma}.py) take
their initial PUBLISHED state from exactly those files. So a world built for a
BI vendor must carry the vendor's project, or the box is a Power BI shop with no
Power BI project in it.

TRIGGER. The seed's profile record plugs a vendor: `profile: {..., plugs: {bi:
[powerbi | tableau | sigma, ...]}}` (build.resolve_profile; the harness writes
it, and its episode check compares it with the task's profile). Any other plug
name under `bi` (`metabase_fake`) asks for nothing. No profile, no `plugs`, or
no vendor under `plugs.bi` -> this module writes nothing and every recorded
hash is unchanged: the explicit rule that an unflagged seed builds
byte-identically (world/README.md).

THE THEME SAYS WHAT THE PROJECTS ARE, through an optional gen.py hook:

    bi_projects(seed) -> {vendor: (Model, (Report, ...))}

returning world.semantic models and reports - the one contract every vendor
front-end reads and writes (world/world/semantic/__init__.py). A theme without
the hook, under a seed that plugs a vendor, is refused before anything is built
(`require`), naming the theme: a vendor box with an empty bi/ is a world that
silently lacks the thing its tasks are about. A hook that leaves out a plugged
vendor is refused the same way.

LAYOUT - the paths each vendor's service and front-end read:

    workspace/bi/powerbi/   PBIP: <Model>.SemanticModel/ (TMDL), <Report>.Report/
                            (PBIR), <Report>.pbip, .gitignore
    workspace/bi/tableau/   datasources/<Model>.tds, workbooks/<Report>.twb
    workspace/bi/sigma/     data-models/, workbooks/, connections/ (API spec JSON)

CONNECTIONS. The world's warehouse is a DuckDB file (the laptop stack), and the
fake services only evaluate against DuckDB. Power BI's partitions go through
the DuckDB ODBC driver (powerbi/mquery.py, connector `duckdb`), Tableau's data
source is a generic ODBC connection through the same driver (tableau/tds.py
CONNECTIONS["duckdb"]) - both naming /world/var/warehouse/warehouse.duckdb, the
file's path on the box. Sigma is cloud-only and has no DuckDB connector; its
front-end writes the Snowflake connection a Sigma shop runs on (sigma/
_common.py), whose paths read back warehouse-relative, so the Sigma service
resolves them against the same file. The Tableau data source is LIVE, not an
extract: nothing on the world's clock refreshes a Tableau extract (the 06:00
`bi_refresh` job re-renders var/bi/dashboards), so an extract would make every
Tableau number silently stale after the first advance - an incident no theme
declared. A task that wants a stale extract plants one.

WHEN. Written by build.build after the warehouse is built (the sources are
checked against it) and BEFORE the workspace git commit: the services seed
their published state from the repository - Power BI explicitly from the first
commit that added bi/powerbi - so the projects must be in that commit to be
what "was deployed at provisioning".

DETERMINISM. Every front-end's write_project is deterministic to the byte: ids
derived from names, sorted keys, no timestamps (Sigma's are a constant epoch).
Nothing here reads a clock or a PRNG. The files are workspace files, so they
move the `workspace` fingerprint section and nothing else.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

#: Canonical order: a profile listing them in another order writes the same files.
VENDORS = ("powerbi", "tableau", "sigma")
#: Where each vendor's project lives in the workspace (the services' PROJECT_DIR).
PROJECT_DIRS = {vendor: Path("bi") / vendor for vendor in VENDORS}
#: The theme's gen.py hook.
HOOK = "bi_projects"


def vendors(profile: dict | None) -> list[str]:
    """The BI vendors a resolved profile record plugs, canonically ordered;
    [] when it plugs none - the build every recorded hash was taken from."""
    if not profile:
        return []
    named = (profile.get("plugs") or {}).get("bi") or ()
    return [v for v in VENDORS if v in named]


def require(theme, profile: dict | None) -> list[str]:
    """`vendors(profile)`, refusing a theme that cannot write them.

    Called before anything is built, so a refused seed leaves no tree behind."""
    wanted = vendors(profile)
    if wanted and getattr(theme.gen, HOOK, None) is None:
        raise SystemExit(
            f"seed profile {profile['name']!r} plugs BI vendor(s) {', '.join(wanted)}, "
            f"but theme {theme.name!r} has no `{HOOK}` hook in its gen.py to say what "
            f"their projects are")
    return wanted


def _quote(text: str) -> str:
    return text.replace("'", "''")


def check_sources(model, db_path: Path) -> None:
    """Every warehouse relation and column the model reads exists in the built
    warehouse - or a loud SystemExit naming the first that does not.

    At build time, because a theme whose dbt marts drifted from its BI project
    would otherwise ship a project every vendor service refuses at the first
    request, in an episode, where nobody authored the failure. Columns only:
    types are the front-ends' business (Sigma reads them from the warehouse)."""
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH '{_quote(str(db_path))}' AS wh (READ_ONLY)")
        rows = con.execute(
            "SELECT table_schema || '.' || table_name, column_name "
            "FROM information_schema.columns WHERE table_catalog = 'wh'").fetchall()
    finally:
        con.close()
    columns: dict[str, set[str]] = {}
    for relation, column in rows:
        columns.setdefault(relation.lower(), set()).add(column.lower())
    for table in model.tables:
        if table.source is None:
            continue  # native SQL: the front-end describes it against the warehouse
        have = columns.get(table.source.lower())
        if have is None:
            raise SystemExit(f"BI model {model.name!r}: table {table.name!r} reads "
                             f"{table.source}, which the built warehouse does not have")
        for column in table.columns:
            if column.expr is None and column.source.lower() not in have:
                raise SystemExit(f"BI model {model.name!r}: column {table.name}."
                                 f"{column.name} reads {table.source}.{column.source}, "
                                 f"which the built warehouse does not have")


def _write_powerbi(model, reports, dest: Path) -> list[Path]:
    from .semantic.formats.powerbi import project
    return project.write_project(model, reports, dest, connector="duckdb")


def _write_tableau(model, reports, dest: Path) -> list[Path]:
    from .semantic.formats.tableau import project
    # server/port default to the fake Tableau Server's (tableau.internal:8096),
    # which is where the workbooks find the published data source.
    return project.write_project(model, reports, dest, warehouse="duckdb", extract=False)


def _write_sigma(model, reports, dest: Path) -> list[Path]:
    from .semantic.formats.sigma import project
    return project.write_project(model, reports, dest)


WRITERS = {"powerbi": _write_powerbi, "tableau": _write_tableau, "sigma": _write_sigma}


def write(root: Path, theme, seed: dict, profile: dict | None,
          db_path: Path | None = None) -> list[Path]:
    """Write every plugged vendor's project into `<root>/workspace`. Returns the
    written files (sorted), [] when the profile plugs no vendor."""
    wanted = require(theme, profile)
    if not wanted:
        return []
    root = Path(root)
    db_path = Path(db_path) if db_path else root / "var" / "warehouse" / "warehouse.duckdb"
    projects = getattr(theme.gen, HOOK)(seed)
    missing = [v for v in wanted if v not in (projects or {})]
    if missing:
        raise SystemExit(f"theme {theme.name!r}: `{HOOK}` returned no project for "
                         f"plugged BI vendor(s) {', '.join(missing)}")
    written: list[Path] = []
    for vendor in wanted:
        model, reports = projects[vendor]
        dest = root / "workspace" / PROJECT_DIRS[vendor]
        if dest.exists():
            # A theme that also ships the directory statically would get the two
            # merged file by file - a project neither of them wrote.
            raise SystemExit(f"theme {theme.name!r} ships workspace/{PROJECT_DIRS[vendor]} "
                             f"and also generates it with `{HOOK}`; keep one")
        check_sources(model, db_path)
        written += WRITERS[vendor](model, tuple(reports), dest)
    return sorted(written)


__all__ = ["HOOK", "PROJECT_DIRS", "VENDORS", "check_sources", "require", "vendors", "write"]
