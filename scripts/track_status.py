#!/usr/bin/env python3
"""
track_status.py -- mechanical progress + incompleteness detector for Tracks A and B.

Silent partial completion is this project's most expensive failure mode: every
track passes its own tests while the composition is broken.  This script makes
"started but not finished" *detectable* instead of discovered by accident.

It answers, mechanically and against `3_track.md` sections 3 and 4:

  Track A (world/)    A1 theme spec lifted out of build.py?  A2 how many world
                      themes, which of C1/D1/D2?  A3 /grading unreachability
                      demonstrated?  A4 gate 1 cross-host, shared-volume writers?
  Track B (gateway/)  B1 duckdb still hardcoded, connector interface?  B2 more
                      than one episode-zone service, egress proven from each?
                      B3 docs/BI/S3 surfaces?  B4 fakesnow plumbing?

plus an incompleteness scan (stubs, truncated files, assert-less/skipped tests,
unresolvable imports, declared-but-unimplemented interfaces, uncommitted work)
and a diff against a saved baseline.

Stdlib only.  No install, like gates/run_gates.py.

    python scripts/track_status.py                      # human readable
    python scripts/track_status.py --json               # machine readable
    python scripts/track_status.py --no-tests           # skip pytest (fast)
    python scripts/track_status.py --save-baseline scripts/track_status_baseline.json
    python scripts/track_status.py --baseline scripts/track_status_baseline.json

Exit 0 = no failing suite and no new blocker.  Exit 1 = a suite fails, or (in
diff mode) something regressed against the baseline.

RULES THIS SCRIPT OBEYS
  * Existence is never completion.  Every check carries `evidence` of one of
    three strengths -- "existence", "reference", "executed" -- and no check
    reports `done` on existence alone.
  * What cannot be determined mechanically is reported `unknown`, never guessed.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORLD = ROOT / "world"
GATEWAY = ROOT / "gateway"

# status vocabulary -- `done` requires behaviour, not a file on disk
NOT_STARTED = "not_started"
IN_PROGRESS = "in_progress"
DONE = "done"
UNKNOWN = "unknown"

# evidence strength, printed next to every status so nobody misreads it
EXISTENCE = "existence"      # a file/name is present; proves nothing ran
REFERENCE = "reference"      # something else in the repo consumes it
EXECUTED = "executed"        # this script ran it and saw the result

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
             ".mypy_cache", "node_modules", "site-packages", "dist", "build-cache"}
SCAN_EXT = {".py", ".md", ".sh", ".yml", ".yaml", ".json", ".toml", ".sql", ".txt", ".cfg", ".ini"}

# 3_track.md section 3 A1: the eight module-level constants that ARE the world shape
A1_CONSTANTS = ["DBT_PROJECT_YML", "SOURCES_YML", "STG_SQL", "FCT_SQL",
                "DASHBOARD", "README", "DOC_DATA_DICTIONARY", "DOC_RUNBOOK"]

# 3_track.md section 3 A2 / R002 T3 section 1: the three T1-tier themes owed this round
THEMES = {
    "C1": {"name": "Mainframe extract + batch window",
           "keywords": ["ebcdic", "copybook", "packed decimal", "fixed-width", "fixed width",
                        "cp037", "batch window", "code page", "codepage", "zoned decimal"]},
    "D1": {"name": "SFTP-and-cron vendor exchange",
           "keywords": ["sftp", "drop folder", "dropfolder", "redeliver", "sentinel file",
                        "vendor exchange", "crontab", "partial file"]},
    "D2": {"name": "Excel as the integration layer",
           "keywords": ["xlsx", "workbook", "worksheet", "excel", "leading zero",
                        "1462", "date system", "sig figs", "significant digits"]},
}

# 3_track.md section 4 B3 / gateway/CONNECTOR_CONTRACT.md: the surfaces owed
B3_SURFACES = {
    "docs":  ["docs_list", "docs_get", "docs_search"],
    "bi":    ["bi_render", "bi_list", "bi_get", "bi_refresh", "bi_cached"],
    "s3":    ["s3_list", "s3_get", "s3_put", "s3_delete"],
}
B3_PREEXISTING = {"docs_search", "bi_render"}   # already shipped before this round


# --------------------------------------------------------------------------- io

def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return ""


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def walk(base: Path, exts=None):
    """Every file under base, skipping venvs and caches."""
    if not base.exists():
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git"))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if exts is None or path.suffix in exts:
                yield path


def git(*args, cwd: Path = ROOT):
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def stamp(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def check(cid, title, status, summary, evidence, **extra):
    """One acceptance-criterion verdict.  `evidence` says how strong the signal is."""
    out = {"id": cid, "title": title, "status": status, "summary": summary, "evidence": evidence}
    out.update(extra)
    return out


def finding(kind, severity, path, detail, name=None, line=None):
    """One piece of half-finished work.  `key` is line-insensitive so edits elsewhere
    in the file do not churn the diff."""
    return {"kind": kind, "severity": severity, "path": path, "name": name,
            "line": line, "detail": detail, "key": f"{kind}|{path}|{name or detail[:60]}"}


# ------------------------------------------------------------------ python parsing

def parse_py(path: Path):
    """(tree, syntax_error).  A SyntaxError here is itself a finding: a file that
    ends mid-statement does not parse."""
    text = read(path)
    try:
        return ast.parse(text, filename=str(path)), None
    except SyntaxError as exc:
        return None, f"line {exc.lineno}: {exc.msg}"


def _docstring_stripped(body):
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:], True
    return list(body), False


def stub_kind(node) -> str | None:
    """`pass`, `...`, or `raise NotImplementedError` as the entire body."""
    body, had_doc = _docstring_stripped(node.body)
    if not body:
        return "docstring only, no body" if had_doc else None
    if len(body) != 1:
        return None
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return "body is `pass`"
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
        return "body is `...`"
    if isinstance(stmt, ast.Raise):
        exc = stmt.exc
        target = exc.func if isinstance(exc, ast.Call) else exc
        name = getattr(target, "id", None) or getattr(target, "attr", None)
        if name == "NotImplementedError":
            return "body is `raise NotImplementedError`"
    return None


def _base_names(node) -> set[str]:
    names = set()
    for base in getattr(node, "bases", []):
        names.add(getattr(base, "id", None) or getattr(base, "attr", None) or "")
    return {n for n in names if n}


MODEL_BASES = {"BaseModel", "Exception", "Protocol", "ABC", "ABCMeta", "Enum", "IntEnum",
               "StrEnum", "TypedDict", "NamedTuple", "Structure", "str", "int", "dict", "tuple"}


def is_declaration_class(node, model_names: set[str] | None = None) -> bool:
    """Protocols, ABCs, exception types and empty model/marker subclasses legitimately
    have empty bodies -- `class Empty(Arguments): pass` is a finished pydantic model,
    not abandoned work.  A subclass of something with METHODS still gets flagged, which
    is what keeps `class PostgresConnector(WarehouseConnector): pass` visible."""
    bases = _base_names(node)
    if bases & MODEL_BASES:
        return True
    if any(b.endswith(("Error", "Exception", "Enum", "Model", "Config")) for b in bases):
        return True
    if model_names and bases & model_names:
        return True
    for item in node.body:
        for deco in getattr(item, "decorator_list", []):
            if (getattr(deco, "id", "") or getattr(deco, "attr", "")) in {"abstractmethod", "abstractproperty"}:
                return True
    return False


def model_class_names(tree) -> set[str]:
    """Classes in this module that are pure data declarations (annotations only, or a
    subclass of one).  Used to keep empty model subclasses out of the stub report."""
    names, changed = set(), True
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    for node in classes:
        body, _ = _docstring_stripped(node.body)
        if body and all(isinstance(s, (ast.AnnAssign, ast.Assign)) for s in body):
            if _base_names(node) & MODEL_BASES or not _base_names(node):
                names.add(node.name)
    while changed:                      # propagate through subclass chains
        changed = False
        for node in classes:
            if node.name not in names and _base_names(node) & names:
                names.add(node.name)
                changed = True
    return names


def test_functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            yield node


def asserts_something(node) -> bool:
    """An assert statement, an assert_* helper call, or pytest.raises."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            return True
        if isinstance(sub, ast.Call):
            name = getattr(sub.func, "id", "") or getattr(sub.func, "attr", "")
            if name.startswith("assert") or name in {"raises", "fail", "approx"}:
                return True
        if isinstance(sub, (ast.With, ast.AsyncWith)):
            for item in sub.items:
                src = ast.dump(item.context_expr)
                if "raises" in src or "warns" in src:
                    return True
    return False


def skip_marker(node) -> str | None:
    for deco in node.decorator_list:
        src = ast.dump(deco)
        for mark in ("skipif", "skip", "xfail"):
            if f"'{mark}'" in src or f'"{mark}"' in src:
                return mark
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = getattr(sub.func, "attr", "") or getattr(sub.func, "id", "")
            if name in {"skip", "xfail"} and "test" not in name:
                return f"{name}() called in body"
    return None


def imported_modules(tree):
    """(module-level absolute, deferred absolute, relative).

    A deferred import -- inside a function or a try/except -- is an optional or
    cross-track dependency by intent; an unresolvable one is a note, not a blocker.
    A module-level one that does not resolve breaks the module on import."""
    top = {id(n) for n in tree.body}
    absolute, deferred, relative = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {a.name.split(".")[0] for a in node.names}
            (absolute if id(node) in top else deferred).update(names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative.add((node.level, node.module or "", tuple(a.name for a in node.names)))
            elif node.module:
                head = node.module.split(".")[0]
                (absolute if id(node) in top else deferred).add(head)
    return absolute, deferred - absolute, relative


# -------------------------------------------------------------- structural scans

def compose_services(text: str):
    """Structural scan of docker-compose.yml -- indentation only, NOT a YAML parse.
    Returns {} when `services:` is absent, and the caller reports `unknown`."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(r"^services:\s*$", ln)), None)
    if start is None:
        return {}
    services, current = {}, None
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith(" "):
            break                                     # back to a top-level key
        m = re.match(r"^  (\S[^:]*):\s*$", line)
        if m:
            current = m.group(1)
            services[current] = {"raw": [], "networks": [], "profiles": [], "volumes": [],
                                 "ports": [], "network_mode": None}
            continue
        if current:
            services[current]["raw"].append(line)
    for name, svc in services.items():
        block, key = svc["raw"], None
        for line in block:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = re.match(r"^(\w+):\s*(.*)$", stripped)
            if m and re.match(r"^    \w", line):
                key, value = m.group(1), m.group(2).strip()
                if key == "network_mode":
                    svc["network_mode"] = value
                elif key in {"networks", "profiles", "volumes", "ports"} and value.startswith("["):
                    svc[key] = [v.strip().strip('"\'') for v in value.strip("[]").split(",") if v.strip()]
                    key = None
                continue
            if key in {"networks", "profiles", "volumes", "ports"} and stripped.startswith("- "):
                svc[key].append(stripped[2:].strip().strip('"\''))
    for svc in services.values():
        svc.pop("raw", None)
    return services


def gate_blocks(text: str):
    """Split acceptance.sh into its `echo "== gate N: ..."` blocks."""
    blocks, name, buf = {}, "preamble", []
    for line in text.splitlines():
        m = re.match(r'\s*echo\s+"==\s*(gate\s*\d+)[^"]*"', line)
        if m:
            blocks[name] = "\n".join(buf)
            name, buf = m.group(1).replace(" ", "").lower(), [line]
        else:
            buf.append(line)
    blocks[name] = "\n".join(buf)
    return blocks


def tool_names(path: Path) -> list[str]:
    """Keys of the TOOLS registry, by AST -- not by grepping strings."""
    tree, _ = parse_py(path)
    if tree is None:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for target in node.targets:
                if getattr(target, "id", "") == "TOOLS":
                    return [k.value for k in node.value.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    return []


def module_constants(path: Path) -> set[str]:
    tree, _ = parse_py(path)
    if tree is None:
        return set()
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.update(getattr(t, "id", "") for t in node.targets)
        elif isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None):
            names.add(node.target.id)
    return names - {""}


# ----------------------------------------------------------------------- tests

def interpreters():
    """3_track.md section 7 runs both suites on the gateway venv; world/.venv is a fallback."""
    cands = [GATEWAY / ".venv" / "Scripts" / "python.exe", GATEWAY / ".venv" / "bin" / "python",
             WORLD / ".venv" / "Scripts" / "python.exe", WORLD / ".venv" / "bin" / "python"]
    return [p for p in cands if p.exists()]


def run_suite(track: str, cwd: Path, pythonpath: Path | None, timeout: int):
    """Run one suite and report what actually happened.  `-p no:cacheprovider` so
    this detector never writes into a track it does not own."""
    result = {"ran": False, "passed": None, "failed": None, "errors": None, "skipped": None,
              "xfailed": None, "returncode": None, "interpreter": None, "ran_at": None,
              "duration_s": None, "summary": None, "status": UNKNOWN}
    exes = interpreters()
    if not exes:
        result["summary"] = "no venv interpreter found (gateway/.venv or world/.venv)"
        return result
    exe = exes[0]
    env = dict(os.environ)
    if pythonpath:
        env["PYTHONPATH"] = str(pythonpath)
    env["PYTHONIOENCODING"] = "utf-8"
    started = time.time()
    try:
        proc = subprocess.run([str(exe), "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
                              cwd=str(cwd), env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        result["returncode"] = proc.returncode
    except subprocess.TimeoutExpired:
        result["summary"] = f"pytest timed out after {timeout}s"
        result["status"] = "timeout"
        result["ran_at"] = started
        return result
    except OSError as exc:
        result["summary"] = f"could not run pytest: {exc}"
        return result
    result["ran"] = True
    result["ran_at"] = started
    result["duration_s"] = round(time.time() - started, 1)
    result["interpreter"] = rel(exe)
    tail = [ln for ln in out.splitlines() if ln.strip()][-1:] or [""]
    result["summary"] = tail[0].strip()[:200]
    for field, word in (("passed", "passed"), ("failed", "failed"), ("errors", "error"),
                        ("skipped", "skipped"), ("xfailed", "xfailed")):
        m = re.search(rf"(\d+)\s+{word}", out)
        result[field] = int(m.group(1)) if m else 0
    if result["returncode"] == 0 and result["passed"]:
        result["status"] = "pass"
    elif result["returncode"] == 5:
        result["status"] = "no tests collected"
    else:
        result["status"] = "FAIL"
        result["output_tail"] = "\n".join(out.splitlines()[-25:])
    return result


# --------------------------------------------------------------------- track A

def track_a(run_tests: bool, timeout: int):
    tests = {"expected_baseline": 7}
    if run_tests:
        tests.update(run_suite("world", WORLD, WORLD, timeout))
    else:
        tests.update({"ran": False, "status": UNKNOWN, "summary": "skipped (--no-tests)"})
    tests["collected_statically"] = count_test_defs(WORLD / "tests")
    green = tests.get("status") == "pass"

    checks = [a1_check(green), a2_check(green), grading_check(),
              cross_host_check(), shared_volume_check()]
    return {"track": "A", "owner": "Kimi", "path": "world/", "checks": checks, "tests": tests}


def a1_check(suite_green: bool):
    """A1.  Is the world shape still the eight string constants, or is it data now?"""
    build_py = WORLD / "world" / "build.py"
    constants = module_constants(build_py)
    still_hardcoded = [c for c in A1_CONSTANTS if c in constants]
    contract = theme_contract()
    themes = theme_dirs(contract)
    consumes = theme_consumption(build_py)
    if still_hardcoded and not themes and not consumes["any"]:
        status, summary, ev = NOT_STARTED, (
            f"{len(still_hardcoded)}/8 shape constants still module-level in world/world/build.py; "
            "no declarative theme spec found"), EXISTENCE
    elif still_hardcoded:
        status, summary, ev = IN_PROGRESS, (
            f"{len(still_hardcoded)}/8 constants remain ({still_hardcoded}) alongside theme "
            f"machinery ({len(themes)} theme dir(s), hooks {sorted(consumes['hooks'])}) "
            "-- a partial lift leaves two sources of truth"), REFERENCE
    elif themes and consumes["any"] and suite_green:
        status, summary, ev = DONE, (
            f"0/8 constants remain; builder consumes {sorted(consumes['hooks'])} and "
            f"{len(themes)} theme dir(s); world suite green after the change"), EXECUTED
    elif themes and consumes["any"]:
        status, summary, ev = IN_PROGRESS, (
            "constants gone and the builder consumes a theme, but the world suite is not green "
            "in this run -- the lift is unverified"), REFERENCE
    else:
        status, summary, ev = UNKNOWN, (
            "constants gone but no theme dir or builder hook found -- inspect by hand"), EXISTENCE
    return check("A1", "World shape lifted out of build.py into a declarative theme spec",
                 status, summary, ev,
                 constants_present=still_hardcoded,
                 constants_absent=[c for c in A1_CONSTANTS if c not in constants],
                 theme_contract=contract, theme_dirs=themes,
                 builder_hooks=sorted(consumes["hooks"]))


def a2_check(suite_green: bool):
    """A2.  How many themes, which of C1/D1/D2, and is each one actually complete?"""
    contract = theme_contract()
    themes = theme_dirs(contract)
    complete = [n for n, t in themes.items() if t["status"] == "complete"]
    partial = {n: t["missing"] for n, t in themes.items() if t["status"] == "incomplete"}
    per_theme = {tid: theme_evidence(tid, meta, themes) for tid, meta in THEMES.items()}
    present = [t for t, v in per_theme.items() if v["status"] != NOT_STARTED]
    if not themes:
        status, ev = NOT_STARTED, EXISTENCE
        summary = ("1 world shape exists and it is hardcoded, not declared; 0 theme dirs; "
                   "none of C1/D1/D2 present")
    elif not present:
        status, ev = IN_PROGRESS, REFERENCE
        summary = (f"{len(themes)} theme dir(s) {sorted(themes)}, {len(complete)} structurally "
                   f"complete; none of C1/D1/D2 yet (3 owed)")
    elif len(present) < 3 or any(v["status"] != DONE for v in per_theme.values()):
        status, ev = IN_PROGRESS, REFERENCE
        summary = (f"{len(themes)} theme dir(s), {len(complete)} complete; "
                   f"C1/D1/D2 present: {present} (3 owed); incomplete: {partial or 'none'}")
    elif suite_green:
        status, ev = DONE, EXECUTED
        summary = "C1, D1 and D2 all structurally complete and the world suite is green"
    else:
        status, ev = IN_PROGRESS, REFERENCE
        summary = "C1/D1/D2 present but the world suite is not green -- unverified"
    return check("A2", "Three new world shapes: C1 mainframe, D1 SFTP/cron, D2 Excel",
                 status, summary, ev, theme_count=len(themes), theme_detail=themes,
                 structurally_complete=complete, incomplete=partial, owed_themes=per_theme)


def theme_contract():
    """What a theme must provide, derived from what the loader and builder actually
    consume -- so the check does not go stale when Kimi changes the contract."""
    loader = read(WORLD / "world" / "themes" / "__init__.py")
    build = read(WORLD / "world" / "build.py")
    contract = {"exists": bool(loader), "files": [], "dirs": [], "gen_hooks": [],
                "manifest_keys": [], "derived_from": []}
    if not loader and not build:
        return contract
    files = set(re.findall(r'\bd\s*/\s*"([^"/]+\.\w+)"', loader))
    files |= set(re.findall(r'theme\.dir\s*/\s*"([^"/]+\.\w+)"', build))
    dirs = set(re.findall(r'theme\.dir\s*/\s*"([^"/.]+)"', build))
    dirs |= set(re.findall(r'\bd\s*/\s*"([^"/.]+)"', loader))
    contract["files"] = sorted(files)
    contract["dirs"] = sorted(dirs)
    contract["gen_hooks"] = sorted(set(re.findall(r"theme\.gen\.(\w+)", build)))
    contract["manifest_keys"] = sorted(set(re.findall(r'theme\.manifest\[[\'"](\w+)[\'"]\]', build))
                                       | set(re.findall(r'manifest\[[\'"](\w+)[\'"]\]', loader)))
    contract["derived_from"] = [rel(WORLD / "world" / "themes" / "__init__.py"),
                                rel(WORLD / "world" / "build.py")]
    return contract


def theme_dirs(contract: dict):
    """Every theme directory, and whether it satisfies the derived contract.
    A theme dir missing a hook the builder calls is a build-time crash waiting."""
    base = WORLD / "world" / "themes"
    out = {}
    if not base.is_dir():
        return out
    for d in sorted(p for p in base.iterdir() if p.is_dir() and p.name not in SKIP_DIRS):
        missing = []
        for f in contract["files"]:
            if not (d / f).is_file():
                missing.append(f)
        for sub in contract["dirs"]:
            if not (d / sub).is_dir() or not any((d / sub).iterdir()):
                missing.append(f"{sub}/ (missing or empty)")
        hooks_found = []
        gen = d / "gen.py"
        if gen.is_file():
            tree, err = parse_py(gen)
            if tree is None:
                missing.append(f"gen.py does not parse ({err})")
            else:
                hooks_found = [n.name for n in tree.body
                               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                for hook in contract["gen_hooks"]:
                    if hook not in hooks_found:
                        missing.append(f"gen.py:{hook}() -- builder calls it")
        manifest_keys = []
        man = d / "theme.yaml"
        if man.is_file():
            manifest_keys = re.findall(r"^(\w+):", read(man), re.M)
            for key in contract["manifest_keys"]:
                if key not in manifest_keys:
                    missing.append(f"theme.yaml:{key} -- builder reads it")
        out[d.name] = {"path": rel(d), "status": "incomplete" if missing else "complete",
                       "missing": missing, "gen_hooks": sorted(hooks_found),
                       "manifest_keys": sorted(set(manifest_keys))}
    return out


def theme_consumption(build_py: Path):
    """Does the builder actually read a theme, or is the spec inert?"""
    text = read(build_py)
    hooks = set()
    if re.search(r"--theme\b", text):
        hooks.add("cli --theme")
    if re.search(r"\bseed\[[\"']theme[\"']\]|seed\.get\([\"']theme", text):
        hooks.add("seed['theme']")
    if re.search(r"\bfrom \.(theme|themes) import|\bimport (theme|themes)\b", text):
        hooks.add("theme module import")
    if re.search(r"\bload_theme\b|\btheme_spec\b|\bTheme\b", text):
        hooks.add("theme symbol")
    return {"any": bool(hooks), "hooks": hooks}


def theme_evidence(tid: str, meta: dict, themes: dict):
    """Is C1 / D1 / D2 present?  A theme dir whose manifest or name carries the id or
    the theme's signature vocabulary counts; a word in a round report does not, because
    only world/ is scanned.  Structural completeness comes from theme_dirs()."""
    owning = []
    for name, info in themes.items():
        blob = (name + " " + read(ROOT / info["path"] / "theme.yaml")).lower()
        if re.search(rf"\b{tid.lower()}\b", blob) or any(k in blob for k in meta["keywords"]):
            owning.append(name)
    signature_files = []
    for path in walk(WORLD, SCAN_EXT):
        low = read(path).lower()
        if any(k in low for k in meta["keywords"]):
            signature_files.append(rel(path))
    tested = [p for p in signature_files if "/tests/" in p or "/test_" in p]
    if not owning and not signature_files:
        status, ev = NOT_STARTED, EXISTENCE
    elif owning and all(themes[n]["status"] == "complete" for n in owning) and tested:
        status, ev = DONE, REFERENCE
    else:
        status, ev = IN_PROGRESS, (REFERENCE if owning else EXISTENCE)
    return {"name": meta["name"], "status": status, "evidence": ev,
            "theme_dirs": owning,
            "structurally_complete": [n for n in owning if themes[n]["status"] == "complete"],
            "signature_files": sorted(set(signature_files))[:10],
            "test_files": sorted(set(tested))[:10]}


def grading_check():
    """A3.  A build gate, not a test: the proof has to come from a live container."""
    probes, artifacts = [], []
    # a command run INSIDE a container, not a compose volume mount and not this file
    probe_cmd = re.compile(r"(exec\b[^|]*|run\s+--rm\s+\S+[^|]*)"
                           r"(test\s+-[edfr]\s+/grading|ls\s+/grading|stat\s+/grading|"
                           r"cat\s+/grading|/grading)")
    mount_line = re.compile(r"^\s*-\s+\S*:/grading(:\w+)?\s*(#.*)?$")
    for path in list(walk(WORLD, {".sh", ".py", ".yml", ".yaml"})) + list(walk(ROOT / "scripts", {".sh", ".py"})):
        if path.resolve() == Path(__file__).resolve():
            continue
        for i, line in enumerate(read(path).splitlines(), 1):
            if "/grading" not in line or mount_line.match(line) or line.strip().startswith("#"):
                continue
            if probe_cmd.search(line):
                probes.append({"path": rel(path), "line": i, "text": line.strip()[:140]})
    run_markers = re.compile(r"exit (code|status)|returncode|No such file or directory|"
                             r"docker (compose )?exec|\$COMPOSE exec|Cannot open", re.I)
    for base in (WORLD, ROOT / "implementation_structure" / "rounds", ROOT / "verifier"):
        for path in walk(base, {".json", ".log", ".txt", ".md", ".out"}):
            if "grading" in path.name.lower() and run_markers.search(read(path)):
                artifacts.append(rel(path))
    if not probes and not artifacts:
        return check("A3", "/grading proven unreachable in a live container", NOT_STARTED,
                     "no in-container /grading probe in any script, and no run artifact anywhere",
                     EXISTENCE, probes=probes, artifacts=artifacts)
    if probes and not artifacts:
        return check("A3", "/grading proven unreachable in a live container", IN_PROGRESS,
                     f"{len(probes)} probe line(s) exist but nothing records a run -- an unrun gate "
                     "is not a demonstration", REFERENCE, probes=probes, artifacts=artifacts)
    return check("A3", "/grading proven unreachable in a live container", UNKNOWN,
                 f"artifact(s) found ({', '.join(artifacts)}); this script cannot confirm they came "
                 "from a live container run -- verify by hand or have the gate stamp an image digest",
                 EXISTENCE, probes=probes, artifacts=artifacts)


def cross_host_check():
    """A4a.  Gate 1 builds twice on the same host, which proves less than claimed."""
    path = WORLD / "scripts" / "acceptance.sh"
    if not path.exists():
        return check("A4a", "acceptance.sh gate 1 is cross-host", UNKNOWN,
                     "world/scripts/acceptance.sh not found", EXISTENCE)
    text = read(path)
    gate1 = gate_blocks(text).get("gate1", "")
    builds = len(re.findall(r"world\.build\b", gate1))
    same_host = bool(re.search(r"same host|same-host", gate1, re.I))
    cross = sorted({m for m in re.findall(r"\bssh\b|\bdocker run\b|CROSS_HOST|runs-on|matrix|"
                                          r"REMOTE_HOST|\bpodman\b", gate1)})
    ci = [rel(p) for p in walk(ROOT / ".github", {".yml", ".yaml"}) if "acceptance" in read(p)]
    if cross or ci:
        return check("A4a", "acceptance.sh gate 1 is cross-host", IN_PROGRESS,
                     f"cross-host markers present ({cross or ci}); this script cannot confirm two "
                     "distinct hosts actually ran", UNKNOWN if not ci else REFERENCE,
                     builds_in_gate1=builds, self_declared_same_host=same_host, ci_workflows=ci)
    says = "the script itself says 'same host'" if same_host else "the script says nothing about hosts"
    return check("A4a", "acceptance.sh gate 1 is cross-host", NOT_STARTED,
                 f"gate 1 runs {builds} builds on one host; {says}; no CI workflow runs it",
                 EXISTENCE, builds_in_gate1=builds, self_declared_same_host=same_host, ci_workflows=ci)


def shared_volume_check():
    """A4b.  Any service writing logs or temp state into the fingerprinted volume
    perturbs the semantic fingerprint."""
    path = WORLD / "docker" / "docker-compose.yml"
    services = compose_services(read(path))
    if not services:
        return check("A4b", "No service writes into the fingerprinted world volume", UNKNOWN,
                     f"could not scan services in {rel(path)}", EXISTENCE)
    writers, readers = [], []
    for name, svc in services.items():
        for mount in svc["volumes"]:
            if mount.startswith("worlddata:"):
                (readers if mount.endswith(":ro") else writers).append(f"{name} -> {mount}")
    if len(writers) > 1:
        status = IN_PROGRESS
        summary = (f"HAZARD: {len(writers)} services mount worlddata writable ({writers}) -- "
                   "logs or temp state from any of them perturbs the semantic fingerprint")
    else:
        status = UNKNOWN
        summary = (f"only {writers or 'no service'} writes worlddata, which is by design; "
                   f"read-only: {readers or 'none'}. This check is the tripwire for B2: it turns "
                   "into a hazard the moment a second service mounts the fingerprinted tree.")
    return check("A4b", "No extra service writes into the fingerprinted world volume",
                 status, summary, EXISTENCE,
                 writable_mounts=writers, readonly_mounts=readers, services=sorted(services))


def count_test_defs(base: Path) -> int:
    total = 0
    for path in walk(base, {".py"}):
        tree, _ = parse_py(path)
        if tree is not None:
            total += sum(1 for _ in test_functions(tree))
    return total


# --------------------------------------------------------------------- track B

def track_b(run_tests: bool, timeout: int):
    checks = [duckdb_coupling_check(), compose_check(), surfaces_check(), fakesnow_check()]
    tests = {"expected_baseline": 12}
    if run_tests:
        tests.update(run_suite("gateway", GATEWAY, None, timeout))
    else:
        tests.update({"ran": False, "status": UNKNOWN, "summary": "skipped (--no-tests)"})
    tests["collected_statically"] = count_test_defs(GATEWAY / "tests")
    return {"track": "B", "owner": "Astra", "path": "gateway/", "checks": checks, "tests": tests}


def duckdb_coupling_check():
    """B1.  3_track.md names lines 3, 15, 50, 78, 119 -- report where they are now."""
    sql = GATEWAY / "src" / "alkera_gateway" / "sql.py"
    text = read(sql)
    sites = []
    for i, line in enumerate(text.splitlines(), 1):
        for pattern, label in ((r"^\s*import duckdb\b", "import duckdb"),
                               (r"duckdb\.connect\(", "duckdb.connect()"),
                               (r'dialect\s*=\s*"duckdb"', 'dialect="duckdb"')):
            if re.search(pattern, line):
                sites.append({"line": i, "kind": label, "text": line.strip()[:100]})
    src = GATEWAY / "src" / "alkera_gateway"
    iface = {
        "contract_doc": rel(GATEWAY / "CONNECTOR_CONTRACT.md") if (GATEWAY / "CONNECTOR_CONTRACT.md").exists() else None,
        "connector_module": [rel(p) for p in walk(src, {".py"}) if "connector" in p.name.lower()],
        "protocol_symbol": [], "factory_symbol": [], "config_backend_field": False,
    }
    for path in walk(src, {".py"}):
        text_p = read(path)
        if re.search(r"class\s+\w*(Warehouse)?Connector\b", text_p):
            iface["protocol_symbol"].append(rel(path))
        if re.search(r"\bdef\s+create_warehouse\b|\bdef\s+connector_for\b", text_p):
            iface["factory_symbol"].append(rel(path))
    if re.search(r"warehouse_backend", read(src / "config.py")):
        iface["config_backend_field"] = True
    implemented = bool(iface["connector_module"] or iface["protocol_symbol"]
                       or iface["factory_symbol"] or iface["config_backend_field"])
    if sites and not implemented and iface["contract_doc"]:
        status = IN_PROGRESS
        summary = (f"{len(sites)} duckdb couplings remain at lines "
                   f"{[s['line'] for s in sites]}; CONNECTOR_CONTRACT.md is published but NOTHING "
                   "in src/ implements it -- declared interface, zero implementation")
        ev = EXISTENCE
    elif sites and not implemented:
        status, summary, ev = NOT_STARTED, (
            f"{len(sites)} duckdb couplings at lines {[s['line'] for s in sites]}; "
            "no connector interface, no contract doc"), EXISTENCE
    elif sites and implemented:
        status, summary, ev = IN_PROGRESS, (
            f"connector machinery appeared ({iface['protocol_symbol'] or iface['connector_module']}) "
            f"but {len(sites)} duckdb couplings still in sql.py"), REFERENCE
    else:
        status, summary, ev = IN_PROGRESS, (
            "sql.py no longer names duckdb directly; `done` needs byte-identical action-log rows "
            "on two backends, which this script does not verify"), REFERENCE
    return check("B1", "Warehouse de-hardcoded behind a frozen connector interface",
                 status, summary, ev, coupling_sites=sites,
                 spec_claimed_lines=[3, 15, 50, 78, 119], interface=iface)


def evidence_artifact(name: str):
    """Read a gateway/docker/evidence/*.json produced by an acceptance script.

    These are the only executed evidence Track B produces. Without this the
    status scan can report `not_started` for work whose proof is sitting on
    disk - which it did for B2 while containment.json recorded all five
    properties green, measured from inside each container's own netns.
    """
    path = GATEWAY / "docker" / "evidence" / name
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def compose_check():
    """B2.  More than one episode-zone service, and egress proven from every one."""
    ev_json = evidence_artifact("containment.json")
    if ev_json is not None and ev_json.get("passed") is True:
        props = [k for k in ev_json if k.startswith("p") and "_" in k]
        return check("B2", "Multi-container episode zone with egress proven from each", DONE,
                     f"containment.json: all {len(props)} properties green, measured per-service "
                     f"({', '.join(sorted(props))})", EXECUTED)
    # Track B's stack is gateway/docker/compose.yml. Reading Track A's file here
    # counted Track A's services and reported Track B as not_started.
    path = GATEWAY / "docker" / "compose.yml"
    services = compose_services(read(path))
    if not services:
        return check("B2", "Multi-container episode zone with egress proven from each", UNKNOWN,
                     f"could not scan services in {rel(path)}", EXISTENCE)
    episode_zone = [n for n, s in services.items()
                    if "episode_net" in s["networks"] and not s["profiles"]]
    profiled = {n: s["profiles"] for n, s in services.items() if s["profiles"]}
    published = {n: s["ports"] for n, s in services.items() if s["ports"]}

    covered = set()
    egress_refs = []
    for script in list(walk(WORLD, {".sh", ".py", ".yml", ".yaml"})) + list(walk(ROOT / "scripts", {".sh", ".py"})):
        for i, line in enumerate(read(script).splitlines(), 1):
            if "egress_test" in line and "def " not in line and not line.strip().startswith("#"):
                egress_refs.append({"path": rel(script), "line": i, "text": line.strip()[:120]})
                m = re.search(r"(?:exec(?:\s+-\w+)*|run\s+--rm)\s+(\w[\w-]*)", line)
                if m:
                    covered.add(m.group(1))
    uncovered = sorted(set(episode_zone) - covered)
    if len(episode_zone) <= 1:
        status = NOT_STARTED
        summary = (f"{len(episode_zone)} episode-zone service(s): {episode_zone}; "
                   f"profiled (other zones): {profiled}")
        ev = EXISTENCE
    elif uncovered:
        status = IN_PROGRESS
        summary = (f"{len(episode_zone)} episode-zone services but egress_test only runs from "
                   f"{sorted(covered) or 'none'} -- {len(uncovered)} unproven claim(s): {uncovered}")
        ev = REFERENCE
    else:
        status = IN_PROGRESS
        summary = ("every episode-zone service has an egress_test invocation; `done` needs the "
                   "battery to actually run green (docker), which this script does not do")
        ev = REFERENCE
    return check("B2", "Multi-container episode zone with egress proven from each",
                 status, summary, ev, episode_zone_services=sorted(episode_zone),
                 other_zone_services=profiled, egress_covered=sorted(covered),
                 egress_uncovered=uncovered, egress_invocations=egress_refs,
                 host_published_ports=published,
                 gateway_is_only_channel=UNKNOWN)


def surfaces_check():
    """B3.  Registered is not the same as working: also report which are exercised."""
    tools = tool_names(GATEWAY / "src" / "alkera_gateway" / "tools.py")
    test_text = "\n".join(read(p) for p in walk(GATEWAY / "tests", {".py"}))
    detail, owed, present = {}, 0, 0
    for surface, names in B3_SURFACES.items():
        rows = {}
        for name in names:
            registered = name in tools
            exercised = registered and re.search(rf'["\']{name}["\']', test_text) is not None
            rows[name] = {"registered": registered, "exercised_by_tests": exercised,
                          "preexisting": name in B3_PREEXISTING}
            if name not in B3_PREEXISTING:
                owed += 1
                if registered:
                    present += 1
        detail[surface] = rows
    if present == 0:
        status, ev = NOT_STARTED, EXISTENCE
        summary = (f"0 of {owed} new surface tools registered; present already: "
                   f"{sorted(B3_PREEXISTING & set(tools))}")
    elif present < owed:
        status, ev = IN_PROGRESS, REFERENCE
        summary = f"{present} of {owed} new surface tools registered"
    else:
        untested = [n for s in detail.values() for n, v in s.items()
                    if v["registered"] and not v["exercised_by_tests"]]
        status = IN_PROGRESS if untested else DONE
        ev = REFERENCE if untested else EXECUTED
        summary = (f"all {owed} registered; untested: {untested}" if untested
                   else f"all {owed} registered and exercised by tests")
    return check("B3", "Docs / BI / S3 surfaces", status, summary, ev,
                 registered_tools=sorted(tools), tool_count=len(tools), surfaces=detail)


def fakesnow_check():
    """B4.  Lay the plumbing (fake hostname, local TLS, connector route) and stop."""
    ev_json = evidence_artifact("snowflake_socket.json")
    if ev_json is not None and ev_json.get("passed") is True:
        return check("B4", "fakesnow plumbing (socket only; Andrew owns semantics)", DONE,
                     "snowflake_socket.json: live driver round trip over TLS passed, incl. "
                     "negative controls. Marker grep cannot see this - trust is carried by a "
                     "generated cert + REQUESTS_CA_BUNDLE, not by extra_hosts.", EXECUTED)
    code_hits, dep_hits, doc_hits = [], [], []
    for base in (GATEWAY, WORLD):
        for path in walk(base, SCAN_EXT):
            text = read(path)
            if not re.search(r"fakesnow|snowflake", text, re.I):
                continue
            if path.suffix in {".py"}:
                code_hits.append(rel(path))
            elif path.name in {"pyproject.toml", "requirements.lock", "uv.lock"} or path.suffix in {".yml", ".yaml", ".json"}:
                dep_hits.append(rel(path))
            else:
                doc_hits.append(rel(path))
    markers = {"login-request": False, "query-request": False, "tls": False, "hostname": False}
    for path in code_hits + dep_hits:
        text = read(ROOT / path)
        markers["login-request"] |= "/session/v1/login-request" in text
        markers["query-request"] |= "/queries/v1/query-request" in text
        markers["tls"] |= bool(re.search(r"\btls\b|\bssl\b|\.pem\b|certifi", text, re.I))
        markers["hostname"] |= bool(re.search(r"extra_hosts|snowflakecomputing", text, re.I))
    if not code_hits and not dep_hits:
        status, ev = NOT_STARTED, EXISTENCE
        summary = ("no fakesnow/snowflake reference in gateway or world code or dependencies"
                   + (f"; named only in docs: {doc_hits}" if doc_hits else ""))
    elif not all(markers.values()):
        status, ev = IN_PROGRESS, EXISTENCE
        summary = f"references exist; missing plumbing markers: {[k for k, v in markers.items() if not v]}"
    else:
        status, ev = IN_PROGRESS, REFERENCE
        summary = "all plumbing markers present; a live login/query round trip is not verified here"
    return check("B4", "fakesnow plumbing (socket only; Andrew owns semantics)",
                 status, summary, ev, code_refs=code_hits, dependency_refs=dep_hits,
                 doc_refs=doc_hits, wire_markers=markers)


# ------------------------------------------------------- incompleteness scanning

def scan_incomplete(bases: dict[str, Path], venv_check: bool = True):
    """Work that is started but not finished.  Every finding names its file."""
    findings, protocols, defined, referenced = [], {}, {}, set()
    absolute_imports: dict[str, set[str]] = {}

    for track, base in bases.items():
        for path in walk(base, SCAN_EXT):
            relp = rel(path)
            text = read(path)

            # -- TODO / FIXME markers
            for i, line in enumerate(text.splitlines(), 1):
                m = re.search(r"\b(TODO|FIXME|XXX|HACK)\b", line)
                if m and "track_status" not in relp:
                    findings.append(finding("todo_marker", "warn", relp,
                                            line.strip()[:140], name=f"{m.group(1)}:{i}", line=i))

            # -- truncation
            findings.extend(truncation_findings(path, relp, text))

            if path.suffix != ".py":
                continue
            tree, err = parse_py(path)
            if tree is None:
                findings.append(finding("unparseable", "blocker", relp,
                                        f"file does not parse ({err}) -- ends mid-statement?",
                                        name="syntax"))
                continue

            is_test = "tests" in path.parts or path.name.startswith("test_")
            models = model_class_names(tree)

            # -- stubs
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    if _base_names(node) & {"Protocol"}:
                        protocols[node.name] = {
                            "path": relp,
                            "methods": sorted(n.name for n in node.body
                                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                                              and not n.name.startswith("__"))}
                    if not is_declaration_class(node, models):
                        kind = stub_kind(node)
                        if kind:
                            findings.append(finding("stub", "warn", relp,
                                                    f"class {node.name}: {kind}",
                                                    name=node.name, line=node.lineno))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = stub_kind(node)
                    if kind and not in_declaration_class(tree, node, models):
                        findings.append(finding("stub", "warn", relp,
                                                f"def {node.name}: {kind}",
                                                name=node.name, line=node.lineno))

            # -- tests that do not test
            if is_test:
                for node in test_functions(tree):
                    mark = skip_marker(node)
                    if mark:
                        findings.append(finding("test_skipped", "warn", relp,
                                                f"{node.name} is marked {mark}",
                                                name=node.name, line=node.lineno))
                    elif not asserts_something(node):
                        findings.append(finding("test_asserts_nothing", "warn", relp,
                                                f"{node.name} contains no assert, assert_* call or "
                                                "pytest.raises", name=node.name, line=node.lineno))

            # -- imports
            absolute, deferred, relative = imported_modules(tree)
            absolute_imports.setdefault(track, {"module_level": set(), "deferred": set()})
            absolute_imports[track]["module_level"].update(absolute)
            absolute_imports[track]["deferred"].update(deferred)
            for level, module, names in relative:
                findings.extend(relative_import_findings(path, relp, level, module, names))

            # -- cross-track coupling: the composition, which no track's own suite covers
            foreign = {"A": {"alkera_gateway"}, "B": {"world"}}.get(track, set())
            for name in (absolute | deferred) & foreign:
                findings.append(finding("cross_track_import", "note", relp,
                                        f"track {track} imports `{name}` from another track's "
                                        "package -- a rename over there breaks this at runtime and "
                                        "neither track's own suite has to notice", name=name))

            # -- definitions vs references, for the "nothing calls it" check
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    defined[node.name] = {"path": relp, "line": node.lineno, "track": track}
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    referenced.add(node.id)
                elif isinstance(node, ast.Attribute):
                    referenced.add(node.attr)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    referenced.add(node.value.strip())
                elif isinstance(node, ast.ImportFrom):
                    referenced.update(a.name for a in node.names)

    # -- declared interface with no structural implementer
    all_classes = collect_classes(bases)
    for pname, meta in protocols.items():
        if not meta["methods"]:
            continue
        implementers = [c for c, methods in all_classes.items()
                        if c != pname and set(meta["methods"]) <= methods]
        if not implementers:
            findings.append(finding("interface_unimplemented", "warn", meta["path"],
                                    f"Protocol {pname} declares {meta['methods']} and no class in "
                                    "world/ or gateway/ defines all of them", name=pname))

    # -- implementation nothing references
    entry_points = entry_point_names()
    for name, meta in defined.items():
        if name in referenced or name in entry_points or name.startswith("__"):
            continue
        if name in {"main"} or "tests" in meta["path"]:
            continue
        findings.append(finding("defined_never_referenced", "note", meta["path"],
                                f"{name} is defined and never named anywhere in world/, gateway/ "
                                "or scripts/ (dynamic dispatch would be a false positive)",
                                name=name, line=meta["line"]))

    # -- imports of modules that do not exist
    if venv_check:
        findings.extend(unresolvable_imports(absolute_imports))
    return findings


def in_declaration_class(tree, target, models=None):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and target in node.body and is_declaration_class(node, models):
            return True
    return False


def collect_classes(bases):
    out = {}
    for base in bases.values():
        for path in walk(base, {".py"}):
            tree, _ = parse_py(path)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    out[node.name] = {n.name for n in node.body
                                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return out


def entry_point_names():
    names = set()
    for path in (GATEWAY / "pyproject.toml", WORLD / "pyproject.toml", ROOT / "pyproject.toml"):
        for m in re.finditer(r"=\s*\"[\w.]+:(\w+)\"", read(path)):
            names.add(m.group(1))
    return names


def relative_import_findings(path: Path, relp: str, level: int, module: str, names):
    """`from .foo import bar` where foo.py was renamed or never written."""
    out = []
    pkg = path.parent
    for _ in range(level - 1):
        pkg = pkg.parent
    if module:
        head = module.split(".")[0]
        if not (pkg / f"{head}.py").exists() and not (pkg / head / "__init__.py").exists():
            out.append(finding("import_missing", "blocker", relp,
                               f"relative import `from {'.' * level}{module}` resolves to no module "
                               f"under {rel(pkg)}", name=module))
    else:
        for name in names:
            if not (pkg / f"{name}.py").exists() and not (pkg / name / "__init__.py").exists() \
                    and not re.search(rf"^\s*(def|class)\s+{name}\b|^{name}\s*=",
                                      read(pkg / "__init__.py"), re.M):
                out.append(finding("import_missing", "blocker", relp,
                                   f"`from {'.' * level} import {name}` resolves to no module or "
                                   f"symbol under {rel(pkg)}", name=name))
    return out


def unresolvable_imports(absolute_imports: dict[str, dict]):
    """Resolve imports inside each track's own venv, on the path pytest would give it.
    `unknown` when there is no interpreter -- never a guess."""
    out = []
    exes = interpreters()
    if not exes:
        return [finding("import_unknown", "note", "scripts/track_status.py",
                        "no venv interpreter found; import resolution skipped "
                        "(reported as unknown, not as clean)", name="resolver")]
    probe = ("import importlib.util, json, sys\n"
             "names = json.loads(sys.stdin.read())\n"
             "bad = []\n"
             "for n in names:\n"
             "    try:\n"
             "        if importlib.util.find_spec(n) is None:\n"
             "            bad.append(n)\n"
             "    except BaseException:\n"
             "        bad.append(n)\n"
             "print(json.dumps(bad))\n")
    for track, groups in sorted(absolute_imports.items()):
        base = {"A": WORLD, "B": GATEWAY}.get(track, ROOT)
        # the path pytest actually builds: track root, src/, tests/ (rootdir conftest),
        # the repo root and the sibling world package (gateway soft-depends on it)
        path_parts = [base, base / "src", base / "tests", ROOT, WORLD]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(str(p) for p in path_parts if p.exists())
        for kind, names in (("module_level", groups["module_level"]), ("deferred", groups["deferred"])):
            if not names:
                continue
            try:
                proc = subprocess.run([str(exes[0]), "-c", probe], input=json.dumps(sorted(names)),
                                      cwd=str(base), env=env, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=120)
                missing = json.loads(proc.stdout.strip() or "[]")
            except (OSError, ValueError, subprocess.SubprocessError):
                out.append(finding("import_unknown", "note", rel(base),
                                   "import resolution failed to run; treat as unknown, not clean",
                                   name=f"{track}:{kind}"))
                continue
            for name in missing:
                if kind == "module_level":
                    out.append(finding("import_missing", "blocker", rel(base),
                                       f"track {track} imports `{name}` at module level and it does "
                                       f"not resolve in {rel(exes[0])} -- the module cannot import",
                                       name=name))
                else:
                    out.append(finding("import_missing_deferred", "note", rel(base),
                                       f"track {track} defers an import of `{name}` (inside a "
                                       "function or try/except) and it does not resolve here; "
                                       "optional or cross-track by design, but it fails at runtime",
                                       name=name))
    return out


def truncation_findings(path: Path, relp: str, text: str):
    """A file that ends mid-statement or mid-sentence."""
    out = []
    if not text.strip():
        return out
    if path.suffix == ".json":
        try:
            json.loads(text)
        except ValueError as exc:
            out.append(finding("truncated", "blocker", relp, f"invalid JSON: {exc}", name="json"))
        return out
    lines = [ln for ln in text.splitlines() if ln.strip()]
    last = lines[-1].rstrip()
    if path.suffix == ".md":
        if text.count("```") % 2:
            out.append(finding("truncated", "warn", relp,
                               "odd number of ``` fences -- unclosed code block", name="fence"))
        if re.search(r"[,;]$|\($|\b(and|or|the|a|to|of|with|is|are|which|that)$", last, re.I) \
                and not last.startswith(("|", "#", "-", "*", ">")):
            out.append(finding("truncated", "warn", relp,
                               f"last line ends mid-sentence: {last[-70:]!r}", name="tail"))
        return out
    if path.suffix in {".yml", ".yaml", ".toml", ".sh", ".sql"}:
        if last.endswith((",", "\\", "|", "&&")) or last.count('"') % 2 or last.count("'") % 2:
            out.append(finding("truncated", "warn", relp,
                               f"last line looks unterminated: {last[-70:]!r}", name="tail"))
    if not text.endswith("\n") and path.suffix in {".py", ".yml", ".yaml", ".sh", ".md"}:
        out.append(finding("truncated", "note", relp,
                           "file does not end with a newline (weak truncation signal)", name="eof"))
    return out


# ------------------------------------------------------------------ git signals

def git_signals(scan_started: float):
    """git status is a strong signal of an interrupted session."""
    porcelain = git("status", "--porcelain=v1", "--untracked-files=all")
    out = {"available": porcelain is not None, "untracked": [], "modified": [], "staged": [],
           "deleted": [], "stashes": 0, "head": None, "head_at": None, "by_track": {}}
    if porcelain is None:
        return out
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip().strip('"')
        if code == "??":
            out["untracked"].append(path)
            continue
        if "D" in code:                      # a tracked file deleted but not committed
            out["deleted"].append(path)
        if code[0] not in " ?D":
            out["staged"].append(path)
        if code[1] not in " ?D":
            out["modified"].append(path)
    stash = git("stash", "list") or ""
    out["stashes"] = len([l for l in stash.splitlines() if l.strip()])
    head = git("log", "-1", "--format=%h|%ct|%s") or ""
    if head.strip():
        h, ct, subject = head.strip().split("|", 2)
        out["head"], out["head_at"], out["head_subject"] = h, int(ct), subject
    # C and corpus are outside this detector's remit but git sees the whole repo, and an
    # interrupted session anywhere is the signal the lead agent needs.
    for track, prefix in (("A (world)", "world/"), ("B (gateway)", "gateway/"),
                          ("C (tasks)", "tasks/"), ("corpus", "implementation_structure/"),
                          ("monitor", "scripts/")):
        out["by_track"][track] = {
            key: sorted(p for p in out[key] if p.startswith(prefix))
            for key in ("untracked", "modified", "staged", "deleted")}
    out["by_track"]["other"] = {
        key: sorted(p for p in out[key] if not p.split("/")[0] + "/" in
                    {"world/", "gateway/", "tasks/", "implementation_structure/", "scripts/"})
        for key in ("untracked", "modified", "staged", "deleted")}
    return out


SELF_FILES = {"scripts/track_status.py", "scripts/track_status_baseline.json"}


def files_changed_since(epoch: float, bases: dict[str, Path]):
    """This detector's own files are excluded -- it must not report its own noise."""
    changed = {}
    for track, base in bases.items():
        hits = [{"path": rel(p), "mtime": stamp(mtime(p))}
                for p in walk(base, SCAN_EXT)
                if mtime(p) > epoch and rel(p) not in SELF_FILES]
        changed[track] = sorted(hits, key=lambda h: h["path"])
    return changed


# --------------------------------------------------------------------- assembly

def collect(args):
    scan_started = time.time()
    bases = {"A": WORLD, "B": GATEWAY, "monitor": ROOT / "scripts"}
    report = {
        "schema": "track_status/1",
        "generated_at": stamp(scan_started),
        "generated_at_epoch": scan_started,
        "root": str(ROOT),
        "spec": "3_track.md sections 3 and 4",
        "tracks": {},
    }
    report["tracks"]["A"] = track_a(not args.no_tests, args.timeout)
    report["tracks"]["B"] = track_b(not args.no_tests, args.timeout)
    report["findings"] = scan_incomplete(bases, venv_check=not args.no_imports)
    report["git"] = git_signals(scan_started)

    # anything that moved while this detector was looking: "the corpus moved under me"
    moved = files_changed_since(scan_started, bases)
    report["changed_during_scan"] = moved
    live = {hit["path"] for hits in moved.values() for hit in hits}
    for track, hits in moved.items():
        for hit in hits:
            report["findings"].append(finding(
                "changed_during_scan", "warn", hit["path"],
                "changed while this scan was running -- another agent is editing it now",
                name="live"))
    # a file whose mtime is newer than its track's last passing run is unverified work.
    # Files already reported as live are not reported a second time.
    for track, data in report["tracks"].items():
        ran_at = data["tests"].get("ran_at")
        if ran_at:
            stale = [rel(p) for p in walk(bases[track], SCAN_EXT)
                     if mtime(p) > ran_at and rel(p) not in SELF_FILES]
            data["tests"]["files_touched_after_run"] = sorted(stale)
            for path in stale:
                if path in live:
                    continue
                report["findings"].append(finding(
                    "changed_after_test_run", "warn", path,
                    f"modified after track {track}'s test run started -- that run does not cover it",
                    name="mtime"))

    counts = {"blocker": 0, "warn": 0, "note": 0}
    for f in report["findings"]:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    report["finding_counts"] = counts
    report["finding_counts_by_kind"] = {}
    for f in report["findings"]:
        report["finding_counts_by_kind"][f["kind"]] = report["finding_counts_by_kind"].get(f["kind"], 0) + 1
    statuses = [t["tests"].get("status") for t in report["tracks"].values()]
    report["verdict"] = {
        "suites_green": UNKNOWN if UNKNOWN in statuses else all(s == "pass" for s in statuses),
        "blockers": counts["blocker"],
    }
    return report


# ------------------------------------------------------------------------ diff

def diff(baseline: dict, current: dict):
    lines, regressed = [], False

    def checks_of(report, track):
        return {c["id"]: c for c in report["tracks"][track]["checks"]}

    order = {NOT_STARTED: 0, IN_PROGRESS: 1, DONE: 2, UNKNOWN: -1}
    for track in ("A", "B"):
        if track not in baseline.get("tracks", {}) or track not in current.get("tracks", {}):
            continue
        old, new = checks_of(baseline, track), checks_of(current, track)
        for cid in sorted(set(old) | set(new)):
            o, n = old.get(cid), new.get(cid)
            if o is None:
                lines.append(f"  + {cid} new check: {n['status']}")
            elif n is None:
                lines.append(f"  - {cid} disappeared from the report")
                regressed = True
            elif o["status"] != n["status"]:
                arrow = "->"
                back = order.get(n["status"], -1) < order.get(o["status"], -1)
                regressed |= back
                lines.append(f"  {'!' if back else '*'} {cid} {o['status']} {arrow} {n['status']}"
                             f"  ({n['summary'][:90]})")
        ob, nb = baseline["tracks"][track]["tests"], current["tracks"][track]["tests"]
        if ob.get("passed") != nb.get("passed") or ob.get("status") != nb.get("status"):
            not_measured = nb.get("ran") is False and "skipped" in (nb.get("summary") or "")
            worse = not not_measured and (
                nb.get("status") != "pass" or (nb.get("passed") or 0) < (ob.get("passed") or 0))
            regressed |= worse
            lines.append(f"  {'!' if worse else '*'} {track} tests: "
                         f"{ob.get('passed')} passed/{ob.get('status')} -> "
                         f"{nb.get('passed')} passed/{nb.get('status')}")

    old_keys = {f["key"]: f for f in baseline.get("findings", [])}
    new_keys = {f["key"]: f for f in current.get("findings", [])}
    added = [new_keys[k] for k in sorted(set(new_keys) - set(old_keys))]
    gone = [old_keys[k] for k in sorted(set(old_keys) - set(new_keys))]
    for f in added:
        regressed |= f["severity"] == "blocker"
        lines.append(f"  {'!' if f['severity'] == 'blocker' else '+'} NEW {f['severity']} "
                     f"{f['kind']} {f['path']}"
                     + (f":{f['line']}" if f.get("line") else "") + f" -- {f['detail'][:90]}")
    for f in gone:
        lines.append(f"  - resolved {f['kind']} {f['path']} -- {f['detail'][:70]}")

    since = baseline.get("generated_at_epoch")
    if since:
        changed = files_changed_since(since, {"A": WORLD, "B": GATEWAY, "monitor": ROOT / "scripts"})
        for track, hits in changed.items():
            if hits:
                lines.append(f"  * {track}: {len(hits)} file(s) changed since baseline "
                             f"({', '.join(h['path'] for h in hits[:8])}"
                             f"{' ...' if len(hits) > 8 else ''})")
    for track in ("A", "B"):
        obl = baseline["tracks"].get(track, {}).get("tests", {}).get("ran_at")
        if obl:
            stale = [rel(p) for p in walk({"A": WORLD, "B": GATEWAY}[track], SCAN_EXT)
                     if mtime(p) > obl and rel(p) not in SELF_FILES]
            if stale:
                lines.append(f"  * {track}: {len(stale)} file(s) newer than the baseline's last "
                             f"test run ({', '.join(stale[:6])}{' ...' if len(stale) > 6 else ''})")

    og, ng = baseline.get("git", {}), current.get("git", {})
    for key in ("untracked", "modified", "staged", "deleted"):
        new_paths = sorted(set(ng.get(key, [])) - set(og.get(key, [])))
        if new_paths:
            lines.append(f"  + git {key}: {', '.join(new_paths[:10])}"
                         f"{' ...' if len(new_paths) > 10 else ''}")
    if not lines:
        lines.append("  (no change against baseline)")
    return lines, regressed


# ----------------------------------------------------------------------- output

def render(report, show_all=False):
    out = []
    out.append(f"track_status  {report['generated_at']}  root={report['root']}")
    out.append(f"measured against {report['spec']}")
    out.append("")
    for track in ("A", "B"):
        t = report["tracks"][track]
        out.append(f"== Track {t['track']} ({t['owner']}, {t['path']})")
        for c in t["checks"]:
            out.append(f"  [{c['status']:<12}] {c['id']}  {c['title']}")
            out.append(f"       evidence: {c['evidence']}")
            for chunk in wrap(c["summary"], 92):
                out.append(f"       {chunk}")
        tests = t["tests"]
        mark = {"pass": "PASS", "FAIL": "FAIL"}.get(tests.get("status"), tests.get("status", "?"))
        out.append(f"  [{mark:<12}] tests  {tests.get('passed')} passed / "
                   f"{tests.get('failed')} failed / {tests.get('skipped')} skipped "
                   f"(baseline {tests['expected_baseline']}, "
                   f"{tests.get('collected_statically')} test defs on disk)")
        if tests.get("summary"):
            out.append(f"       {tests['summary'][:100]}")
        if tests.get("files_touched_after_run"):
            out.append(f"       WARN {len(tests['files_touched_after_run'])} file(s) changed after "
                       f"this run started: {', '.join(tests['files_touched_after_run'][:5])}")
        out.append("")

    out.append("== Incompleteness scan")
    counts = report["finding_counts"]
    out.append(f"  {counts.get('blocker', 0)} blocker / {counts.get('warn', 0)} warn / "
               f"{counts.get('note', 0)} note")
    by_kind = report["finding_counts_by_kind"]
    if by_kind:
        out.append("  by kind: " + ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())))
    shown = {"blocker": 40, "warn": 25, "note": 10}
    for severity in ("blocker", "warn", "note"):
        rows = [f for f in report["findings"] if f["severity"] == severity]
        limit = len(rows) if show_all else shown[severity]
        for f in rows[:limit]:
            loc = f["path"] + (f":{f['line']}" if f.get("line") else "")
            out.append(f"  {severity:<7} {f['kind']:<24} {loc}")
            out.append(f"          {f['detail'][:100]}")
        if len(rows) > limit:
            out.append(f"  ... {len(rows) - limit} more {severity} (use --json or --all)")
    if not report["findings"]:
        out.append("  (none)")
    out.append("")

    g = report["git"]
    out.append("== Git (interrupted-session signal)")
    if not g["available"]:
        out.append("  git unavailable -- uncommitted-work detection is UNKNOWN, not clean")
    else:
        out.append(f"  HEAD {g.get('head')} {g.get('head_subject', '')[:70]}")
        for track, buckets in g["by_track"].items():
            bits = [f"{k}={len(v)}" for k, v in buckets.items() if v]
            detail = "; ".join(f"{k}: {', '.join(v[:6])}" for k, v in buckets.items() if v)
            out.append(f"  {track}: {', '.join(bits) if bits else 'clean'}"
                       + (f"  [{detail}]" if detail else ""))
        if g["stashes"]:
            out.append(f"  {g['stashes']} stash entr(ies) -- parked work")
    out.append("")
    v = report["verdict"]
    out.append(f"VERDICT  suites_green={v['suites_green']}  blockers={v['blockers']}")
    out.append("NOTE     `done` is never awarded for a file existing. Statuses above are progress,")
    out.append("         not quality; composition across tracks is not tested here.")
    return "\n".join(out)


def wrap(text, width):
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out or [""]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Track A/B progress and incompleteness detector")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--all", action="store_true", help="print every finding, not a capped sample")
    ap.add_argument("--no-tests", action="store_true", help="skip pytest (fast, but test status is unknown)")
    ap.add_argument("--no-imports", action="store_true", help="skip venv import resolution")
    ap.add_argument("--timeout", type=int, default=900, help="per-suite pytest timeout, seconds")
    ap.add_argument("--baseline", type=Path, help="diff against this saved baseline JSON")
    ap.add_argument("--save-baseline", type=Path, help="write this run to a baseline JSON")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    report = collect(args)
    regressed = False
    diff_lines = None
    if args.baseline:
        if not args.baseline.exists():
            print(f"baseline not found: {args.baseline}", file=sys.stderr)
            return 2
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        diff_lines, regressed = diff(baseline, report)
        report["diff_against"] = str(args.baseline)
        report["diff"] = diff_lines
        report["regressed"] = regressed

    if args.save_baseline:
        args.save_baseline.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n": .gitattributes normalizes this repo to LF
        with open(args.save_baseline, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(report, indent=2, sort_keys=False) + "\n")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report, show_all=args.all))
        if diff_lines is not None:
            print(f"\n== Diff against {args.baseline}")
            print("\n".join(diff_lines))
            print(f"\n{'REGRESSED' if regressed else 'no regression'}")
        if args.save_baseline:
            print(f"\nbaseline written to {args.save_baseline}")

    failing = report["verdict"]["suites_green"] is False
    return 1 if (failing or regressed or report["verdict"]["blockers"]) else 0


if __name__ == "__main__":
    sys.exit(main())
