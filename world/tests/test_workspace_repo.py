"""Workspace-contents tests: the repo the agent opens.

These cover the parts of `world/world/themes/*/workspace/` that are not
exercised by building a world - most importantly the Airflow DAG files, which
nothing executes.

Why the DAG tests exist at all. `theme.yaml` is the executing source of truth:
`world/world/executor.py` runs the real dbt, the real loaders and the real
DuckDB out of each job's `exec:` block, and it does not read `dags/` and never
will. A DAG directory that is free to disagree with the manifest is a lie that
teaches an agent the wrong thing about its own environment. So the DAG files
are not generated - each theme's is written the way that shop would write it -
but they are *pinned*: every job in the manifest must appear as a task, every
`after:` relation must be a path through the DAG's `>>` edges, and the DAG's
schedule must be the root job's time. Change the manifest without changing the
DAG and this file goes red.

What that does NOT buy: editing a DAG still changes nothing about what runs.
That seam is stated in each theme's `dags/README.md` and is the honest
description of the system as built.

The DAG files are read with `ast` (never imported): Airflow is not installed,
and importing anything out of a theme's workspace would drop a `__pycache__`
into a tree whose bytes are inside the semantic fingerprint.
"""
import ast
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import build as wbuild, fingerprint  # noqa: E402
from world.themes import load_theme  # noqa: E402

TRACK_A = Path(__file__).resolve().parents[1]
THEMES = ("revenue_daily", "sftp_vendor", "mainframe_batch", "excel_close")
THEME_DIR = TRACK_A / "world" / "themes"


def workspace(theme: str) -> Path:
    return THEME_DIR / theme / "workspace"


def dag_check_module(theme: str):
    """Load a theme's own scripts/dag_check.py without writing bytecode.

    The module is the tool the agent runs (`./run.sh dag-check`), so the tests
    read the DAGs through it rather than through a second implementation that
    could agree with the manifest while the shipped one does not.
    """
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True          # never a __pycache__ in a hashed tree
    try:
        path = workspace(theme) / "scripts" / "dag_check.py"
        spec = importlib.util.spec_from_file_location(
            f"_dag_check_{theme}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = previous


def reachable(edges, start):
    """Every task reachable from `start` through the DAG's >> edges."""
    seen, stack = set(), [start]
    while stack:
        node = stack.pop()
        for a, b in edges:
            if a == node and b not in seen:
                seen.add(b)
                stack.append(b)
    return seen


def cron_for(time_str: str) -> str:
    hour, minute = time_str.split(":")
    return f"{int(minute)} {int(hour)} * * *"


# --------------------------------------------------------------------------
# the DAG layer, pinned to the manifest
# --------------------------------------------------------------------------

class DagsMatchTheManifestTest(unittest.TestCase):
    """dags/ describes exactly the pipeline theme.yaml declares."""

    def dags_for(self, theme: str):
        module = dag_check_module(theme)
        dags = module.read_dags(workspace(theme) / "dags")
        self.assertTrue(dags, f"{theme}: no dag files")
        return module, dags

    def test_every_theme_ships_dags(self):
        for theme in THEMES:
            with self.subTest(theme=theme):
                self.assertTrue((workspace(theme) / "dags").is_dir())

    def test_every_manifest_job_is_a_dag_task(self):
        for theme in THEMES:
            with self.subTest(theme=theme):
                module, dags = self.dags_for(theme)
                jobs = set(load_theme(theme).manifest["jobs"])
                declared = set()
                for dag in dags:
                    declared |= set(dag["tasks"])
                self.assertEqual(
                    jobs - declared, set(),
                    f"{theme}: manifest jobs with no dag task")

    def test_no_dag_task_invents_a_job(self):
        """Every non-plumbing task is a job the manifest declares.

        A task that is neither a sensor nor a declared job would be a step the
        agent can read and nothing can run.
        """
        for theme in THEMES:
            with self.subTest(theme=theme):
                module, dags = self.dags_for(theme)
                jobs = set(load_theme(theme).manifest["jobs"])
                for dag in dags:
                    for task_id, operator in dag["tasks"].items():
                        if module.is_job(operator):
                            self.assertIn(task_id, jobs,
                                          f"{theme}: task {task_id!r} is not a job")

    def test_after_relations_are_paths_through_the_dag(self):
        for theme in THEMES:
            with self.subTest(theme=theme):
                _, dags = self.dags_for(theme)
                edges = set()
                for dag in dags:
                    edges |= set(dag["edges"])
                jobs = load_theme(theme).manifest["jobs"]
                for name, spec in jobs.items():
                    after = spec.get("after")
                    if not after:
                        continue
                    upstream = after["job"]
                    self.assertIn(
                        name, reachable(edges, upstream),
                        f"{theme}: {name} must be downstream of {upstream}")
                    self.assertNotIn(
                        upstream, reachable(edges, name),
                        f"{theme}: {upstream} must not be downstream of {name}")

    def test_dag_schedule_matches_the_root_job_time(self):
        for theme in THEMES:
            with self.subTest(theme=theme):
                _, dags = self.dags_for(theme)
                jobs = load_theme(theme).manifest["jobs"]
                roots = {n: s for n, s in jobs.items() if not s.get("after")}
                self.assertEqual(len(roots), 1, f"{theme}: expected one root job")
                root, spec = next(iter(roots.items()))
                owning = [d for d in dags if root in d["tasks"]]
                self.assertEqual(len(owning), 1)
                self.assertEqual(owning[0]["schedule"], cron_for(spec["time"]),
                                 f"{theme}: dag schedule vs job time")

    def test_a_wait_task_waits_for_the_declared_gap(self):
        """Where a theme models the gap with a sensor, the sensor's delta is
        the manifest's `after.minutes`. mainframe_batch's 45-minute settle
        window is the one that matters; a theme that just chains its tasks has
        nothing to check here and that is stated in its dags/README.md."""
        checked = 0
        for theme in THEMES:
            for path in sorted((workspace(theme) / "dags").glob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not (isinstance(node, ast.Call)
                            and getattr(node.func, "id", "") == "TimeDeltaSensor"):
                        continue
                    minutes = None
                    for kw in node.keywords:
                        if kw.arg == "delta" and isinstance(kw.value, ast.Call):
                            for inner in kw.value.keywords:
                                if inner.arg == "minutes":
                                    minutes = inner.value
                    self.assertIsNotNone(minutes, f"{theme}: sensor with no minutes=")
                    # the value is a module constant; resolve it from the source
                    source = path.read_text(encoding="utf-8")
                    if isinstance(minutes, ast.Name):
                        found = re.search(rf"^{minutes.id}\s*=\s*(\d+)",
                                          source, re.M)
                        self.assertIsNotNone(found)
                        value = int(found.group(1))
                    else:
                        value = ast.literal_eval(minutes)
                    gaps = {s["after"]["minutes"]
                            for s in load_theme(theme).manifest["jobs"].values()
                            if s.get("after")}
                    self.assertIn(value, gaps,
                                  f"{theme}: sensor waits {value}m, manifest "
                                  f"declares {sorted(gaps)}")
                    checked += 1
        self.assertGreater(checked, 0, "no theme models a gap with a sensor")

    def test_reading_the_dags_left_no_bytecode_in_a_hashed_tree(self):
        for theme in THEMES:
            with self.subTest(theme=theme):
                self.assertEqual(
                    sorted(p.as_posix()
                           for p in workspace(theme).rglob("__pycache__")),
                    [])


# --------------------------------------------------------------------------
# config and credential surface
# --------------------------------------------------------------------------

SECRET_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key block"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws access key id"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "aws session key id"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "slack token"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "github token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "api key"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api_key)\s*[:=]\s*"
                r"['\"]?[A-Za-z0-9+/=_\-]{12,}"), "assigned credential value"),
)

TEXT_SUFFIXES = {".py", ".sh", ".yml", ".yaml", ".md", ".cfg", ".sql", ".txt",
                 ".jcl", ".sample", ".example", ".ini", ".toml", ".json", ""}


class NoPlantedSecretsTest(unittest.TestCase):
    def test_no_workspace_file_carries_a_credential_shaped_string(self):
        for theme in THEMES:
            for path in sorted(workspace(theme).rglob("*")):
                if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                for pattern, label in SECRET_PATTERNS:
                    hit = pattern.search(text)
                    self.assertIsNone(
                        hit, f"{theme}/{path.relative_to(workspace(theme))}: "
                             f"{label}: {hit.group(0) if hit else ''}")


class ConfigSurfaceTest(unittest.TestCase):
    """Each theme ships the config archaeology, and it is internally wired."""

    def test_each_theme_ships_a_committed_environment_file(self):
        """`.envrc` is the committed environment, and `run.sh` sources it.

        Not `.env`: this repository's root .gitignore excludes `.env` and
        `.env.*` outright, so a theme shipping one would have it silently
        dropped on the next clone and the world would build differently from
        a fresh checkout than it does here. `.envrc` is the direnv convention,
        is normally committed, and survives that rule - and the file a
        developer must NOT commit (`.env`) is still modelled, as the thing
        `.envrc` sources on top of itself.
        """
        for theme in THEMES:
            with self.subTest(theme=theme):
                ws = workspace(theme)
                envrc = ws / ".envrc"
                self.assertTrue(envrc.is_file(), "no .envrc")
                self.assertIn(".env", envrc.read_text(encoding="utf-8"),
                              ".envrc does not source a local .env")
                self.assertIn(".envrc", (ws / "run.sh").read_text(encoding="utf-8"),
                              "run.sh does not source .envrc")

    def test_no_workspace_file_is_ignored_by_the_outer_repository(self):
        """Every byte a theme ships must survive `git clone`.

        The workspace tree is inside the semantic fingerprint, so a file that
        this repository's .gitignore quietly drops makes a fresh checkout build
        a different world - and the two nested .gitignore files a theme ships
        of its own are read by the OUTER repository too, which is how this
        nearly happened.
        """
        repo = TRACK_A.parent
        paths = [str(p.relative_to(repo).as_posix())
                 for theme in THEMES
                 for p in workspace(theme).rglob("*") if p.is_file()]
        self.assertTrue(paths)
        result = subprocess.run(["git", "check-ignore", "--stdin"], cwd=repo,
                                input="\n".join(paths), capture_output=True,
                                text=True)
        self.assertEqual(result.stdout.strip(), "",
                         "these workspace files would never be committed")

    def test_the_local_override_file_is_gitignored_in_world(self):
        for theme in THEMES:
            with self.subTest(theme=theme):
                ignore = (workspace(theme) / ".gitignore")
                self.assertTrue(ignore.is_file())
                lines = [line.strip() for line
                         in ignore.read_text(encoding="utf-8").splitlines()]
                self.assertIn(".env", lines)
                self.assertNotIn(".envrc", lines)

    def test_each_theme_ships_a_dbt_profile_outside_the_dbt_project(self):
        """A profiles.yml inside dbt_project/ would be picked up by a bare dbt
        run and quietly override --profiles-dir semantics for anyone who
        forgets the flag. Every theme keeps it in config/ (conf/ for
        mainframe_batch) and has run.sh export DBT_PROFILES_DIR."""
        for theme in THEMES:
            with self.subTest(theme=theme):
                ws = workspace(theme)
                self.assertFalse((ws / "dbt_project" / "profiles.yml").exists())
                found = list(ws.glob("config/profiles.yml")) + \
                    list(ws.glob("conf/profiles.yml"))
                self.assertEqual(len(found), 1, "exactly one profiles.yml")
                run_sh = (ws / "run.sh").read_text(encoding="utf-8")
                self.assertIn("DBT_PROFILES_DIR", run_sh)

    def test_ci_only_calls_run_sh_targets_that_exist(self):
        """Every `./run.sh X` a CI config runs is a real target."""
        checked = 0
        for theme in THEMES:
            ws = workspace(theme)
            run_sh = (ws / "run.sh").read_text(encoding="utf-8")
            configs = list(ws.rglob(".github/workflows/*.yml")) + \
                list(ws.glob(".gitlab-ci.yml"))
            self.assertTrue(configs, f"{theme}: no ci config")
            for config in configs:
                text = config.read_text(encoding="utf-8")
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue          # commented-out jobs are deliberate
                    for target in re.findall(r"\./run\.sh\s+([a-z-]+)", stripped):
                        self.assertIsNotNone(
                            re.search(rf"^\s*{re.escape(target)}\)",
                                      run_sh, re.M),
                            f"{theme}: ci runs ./run.sh {target}, "
                            f"which run.sh does not implement")
                        checked += 1
        self.assertGreater(checked, 0)


# --------------------------------------------------------------------------
# the one duplicated implementation, held to an equality
# --------------------------------------------------------------------------

class RevenueLoaderEquivalenceTest(unittest.TestCase):
    """revenue_daily's workspace loader must produce exactly what the
    scheduler's load produces.

    revenue_daily is the one theme whose `daily_load` job runs a gen.py hook
    rather than a script in the workspace, so the loader the agent can read
    (`loader/load_daily_revenue.py`) is NOT the code the nightly job runs.
    That duplication is only acceptable while the two are provably the same
    table, which is what this asserts: build a world, re-run the workspace
    loader over the same landing zone, and require raw.daily_revenue to hash
    identically.

    The seam it does not close: an agent who EDITS the workspace loader and
    runs it changes the warehouse, and the next scheduled `daily_load` still
    runs the gen hook. Closing that needs revenue_daily's `theme.yaml` to
    declare `exec: {kind: script, path: loader/load_daily_revenue.py}`, the
    way the other three themes already run their own loaders - a manifest
    change, which is not this track's to make.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="workspace_repo_"))
        lock = TRACK_A / "requirements.lock"
        cls.root = wbuild.build(TRACK_A / "seed.example.yaml", cls.tmp / "w",
                                lock if lock.exists() else None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def table_hash(self) -> str:
        return fingerprint.semantic(self.root)["warehouse"]["tables"][
            "raw.daily_revenue"]

    def test_workspace_loader_reproduces_the_scheduled_load(self):
        before = self.table_hash()
        ws = self.root / "workspace"
        result = subprocess.run(
            [sys.executable, str(ws / "loader" / "load_daily_revenue.py"),
             "--landing", str(ws / "data" / "landing" / "daily_revenue"),
             "--db", str(self.root / "var" / "warehouse" / "warehouse.duckdb")],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)
        self.assertEqual(before, self.table_hash(),
                         "loader/load_daily_revenue.py and the theme's "
                         "load_raw hook no longer agree")

    def test_the_built_workspace_is_fully_committed(self):
        """`git status` is clean in a fresh world, and nothing the theme ships
        was dropped by the workspace's own .gitignore."""
        for args in (["status", "--porcelain"],
                     ["status", "--porcelain", "--ignored"]):
            result = subprocess.run(["git", *args], cwd=self.root / "workspace",
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "", f"git {' '.join(args)}")

    def test_dag_check_runs_clean_against_the_built_world(self):
        """The tool the agent runs, run against the world it ships in."""
        ws = self.root / "workspace"
        result = subprocess.run(
            [sys.executable, str(ws / "scripts" / "dag_check.py")],
            cwd=ws, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)
        self.assertIn("clean:", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
