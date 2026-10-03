"""Re-rooting host paths onto container paths, and the leak detector.

The world is built on the HOST, in a temp directory, by the same builder the
rollout harness runs. Every absolute host path the builder wrote into a file
(a dbt profile, the grading zone's variant.json, the world manifest) has to be
spelled the way the container spells it, and nothing of the export machine may
survive in a published task: a host path is a fingerprint of the exporter, it
breaks digest reproducibility, and in the agent's image it names a directory
that does not exist.

Two halves:

* `Rerooter` rewrites known prefixes (longest first, on a path boundary) in
  UTF-8 text files and in JSON-like objects. A BINARY file that contains a
  prefix cannot be rewritten without corrupting it, so it is an error.
* `scan` is the leak detector. It looks for the export's own temp directory,
  its unique marker, the repository path and similar needles in every file
  (text or binary), inside every git object (loose objects are zlib-compressed,
  so a plain byte scan would miss them), and inside every DuckDB file (catalog
  SQL and every string column: DuckDB compresses strings on disk). Any hit
  fails the export.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: The prefix every export temp directory starts with. A second, path-free
#: needle: it still matches when only a fragment of the temp path leaked.
EXPORT_MARKER = "alkera-harbor-export-"
#: Python's tempfile.mkdtemp default spelling under /tmp (8 chars from
#: `[a-z0-9_]`): a temp path of SOME process that leaked into a file.
MKDTEMP_RE = re.compile(rb"/tmp/tmp[a-z0-9_]{8}(?![a-z0-9_])")
_BOUNDARY = r"(?![A-Za-z0-9._-])"


class LeakError(Exception):
    """A host path survived (or could not be rewritten). Fails the export."""


def _variants(path: str | Path) -> list[str]:
    raw = str(path).rstrip("/") or "/"
    out = [raw]
    try:
        resolved = str(Path(raw).resolve())
    except OSError:
        resolved = raw
    if resolved != raw:
        out.append(resolved)
    return out


@dataclass
class Rerooter:
    """Rewrite host prefixes to container prefixes.

    `mapping` is a list of (host_prefix, container_prefix). Both the given and
    the resolved spelling of each host prefix are rewritten; the longest prefix
    wins, and a prefix only matches on a path boundary (so `/x/world` never
    rewrites `/x/worlds`).
    """

    mapping: list[tuple[str, str]]
    _pairs: list[tuple[str, str]] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        pairs: dict[str, str] = {}
        for host, container in self.mapping:
            for spelling in _variants(host):
                if spelling in ("", "/"):
                    raise ValueError(f"refusing to re-root the filesystem root ({host!r})")
                pairs.setdefault(spelling, container.rstrip("/") or "/")
        self._pairs = sorted(pairs.items(), key=lambda kv: (-len(kv[0]), kv[0]))
        alternation = "|".join(re.escape(host) for host, _ in self._pairs)
        self._re = re.compile(f"(?:{alternation}){_BOUNDARY}")
        self._lookup = dict(self._pairs)

    @property
    def needles(self) -> list[str]:
        return [host for host, _ in self._pairs]

    def text(self, value: str) -> str:
        return self._re.sub(lambda m: self._lookup[m.group(0)], value)

    def obj(self, value):
        """Re-root every string inside a JSON-like structure (keys untouched)."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.obj(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.obj(v) for v in value)
        if isinstance(value, dict):
            return {k: self.obj(v) for k, v in value.items()}
        return value

    def tree(self, root: Path) -> list[str]:
        """Rewrite every text file under `root` in place. Returns what changed.

        A binary file containing a host prefix raises LeakError: it cannot be
        rewritten safely, and shipping it would leak the export machine.
        """
        root = Path(root)
        needles = [n.encode("utf-8") for n in self.needles]
        changed: list[str] = []
        for path in iter_files(root):
            data = path.read_bytes()
            if not any(n in data for n in needles):
                continue
            rel = path.relative_to(root).as_posix()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise LeakError(f"{rel}: binary file contains a host path; it "
                                f"cannot be re-rooted safely") from None
            new = self.text(text)
            if new != text:
                mode = path.stat().st_mode
                path.write_bytes(new.encode("utf-8"))
                os.chmod(path, mode & 0o7777)
                changed.append(rel)
        return changed


def iter_files(root: Path) -> list[Path]:
    """Every regular file under `root`, sorted; symlinks are refused."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in dirnames + filenames:
            if (here / name).is_symlink():
                raise LeakError(f"{(here / name).relative_to(root)}: symlinks are "
                                f"not allowed in a generated task")
        dirnames.sort()
        out.extend(here / name for name in sorted(filenames))
    return out


# --------------------------------------------------------------- the detector
@dataclass(frozen=True)
class Needle:
    label: str
    value: bytes


def needles_for(*, export_dirs=(), repo: Path | None = None,
                home: Path | None = None, extra=()) -> list[Needle]:
    """The strings that must never appear in a generated task."""
    out: dict[bytes, str] = {EXPORT_MARKER.encode(): "export temp-dir marker"}
    for path in export_dirs:
        for spelling in _variants(path):
            out.setdefault(spelling.encode(), "export temp dir")
    if repo is not None:
        for spelling in _variants(repo):
            out.setdefault(spelling.encode(), "repository path")
    if home is not None:
        parts = [p for p in str(home).split("/") if p]
        if len(parts) >= 2 and str(home) not in ("/root",):
            for spelling in _variants(home):
                out.setdefault(spelling.encode(), "exporter home directory")
    for label, value in extra:
        out.setdefault(str(value).encode(), label)
    return [Needle(label, value) for value, label in sorted(out.items())]


def _hits(data: bytes, needles: list[Needle]) -> list[str]:
    found = [n.label for n in needles if n.value in data]
    if MKDTEMP_RE.search(data):
        found.append("python mkdtemp path")
    return found


def _git_env() -> dict:
    env = dict(os.environ)
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0"})
    return env


def _scan_git(git_dir: Path, needles: list[Needle]) -> list[str]:
    """Every object of the repository, decompressed, scanned for needles."""
    r = subprocess.run(["git", f"--git-dir={git_dir}", "cat-file",
                        "--batch-all-objects", "--batch"],
                       capture_output=True, env=_git_env())
    if r.returncode != 0:
        return [f"git objects unreadable: {r.stderr.decode(errors='replace')[:200]}"]
    return _hits(r.stdout, needles)


_NOT_TEXT = re.compile(r"^(BOOLEAN|TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|UTINYINT|"
                       r"USMALLINT|UINTEGER|UBIGINT|UHUGEINT|FLOAT|DOUBLE|REAL|"
                       r"DECIMAL.*|DATE|TIME.*|TIMESTAMP.*|INTERVAL|UUID|BIT)$")


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _scan_duckdb(path: Path, needles: list[Needle]) -> list[str]:
    """Catalog SQL and every string-bearing column of a DuckDB file."""
    try:
        import duckdb
    except ImportError:                                  # pragma: no cover
        return ["duckdb module unavailable: the file could not be scanned"]
    strings = [n.value.decode("utf-8", "replace") for n in needles]
    labels = {n.value.decode("utf-8", "replace"): n.label for n in needles}
    found: list[str] = []
    con = duckdb.connect(str(path), read_only=True)
    try:
        db = con.execute("SELECT current_database()").fetchone()[0]
        catalog = [
            "SELECT 'view ' || schema_name || '.' || view_name, sql FROM duckdb_views() "
            "WHERE NOT internal AND database_name = ?",
            "SELECT 'table ' || schema_name || '.' || table_name, sql FROM duckdb_tables() "
            "WHERE database_name = ?",
            "SELECT 'macro ' || schema_name || '.' || function_name, "
            "coalesce(macro_definition, '') FROM duckdb_functions() "
            "WHERE NOT internal AND database_name = ?",
            "SELECT 'index ' || schema_name || '.' || index_name, coalesce(sql, '') "
            "FROM duckdb_indexes() WHERE database_name = ?",
        ]
        for query in catalog:
            for name, sql in con.execute(query, [db]).fetchall():
                for s in strings:
                    if s in (sql or ""):
                        found.append(f"{labels[s]} in {name}")
        tables = con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_catalog = ? AND table_type = 'BASE TABLE' "
            "ORDER BY 1, 2", [db]).fetchall()
        for schema, table in tables:
            cols = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_catalog = ? AND table_schema = ? AND table_name = ? "
                "ORDER BY ordinal_position", [db, schema, table]).fetchall()
            text_cols = [c for c, t in cols if not _NOT_TEXT.match(str(t).upper())]
            if not text_cols:
                continue
            cond = " OR ".join(f"contains(CAST({_quote(c)} AS VARCHAR), ?)"
                               for c in text_cols for _ in strings)
            params = [s for _ in text_cols for s in strings]
            n = con.execute(f"SELECT count(*) FROM {_quote(db)}.{_quote(schema)}."
                            f"{_quote(table)} WHERE {cond}", params).fetchone()[0]
            if n:
                found.append(f"host path in {n} row(s) of {schema}.{table}")
    finally:
        con.close()
    return found


def scan(root: Path, needles: list[Needle], *, git: bool = True,
         duckdb: bool = True) -> list[str]:
    """Every leak under `root`, as 'relpath: what'. Empty means clean."""
    root = Path(root)
    findings: list[str] = []
    for path in iter_files(root):
        rel = path.relative_to(root).as_posix()
        for label in _hits(path.read_bytes(), needles):
            findings.append(f"{rel}: {label}")
        if duckdb and path.suffix == ".duckdb":
            findings.extend(f"{rel}: {hit}" for hit in _scan_duckdb(path, needles))
        if git and path.name == "HEAD" and path.parent.name == ".git":
            findings.extend(f"{rel}: git object, {hit}"
                            for hit in _scan_git(path.parent, needles))
    return findings
