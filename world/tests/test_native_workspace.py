"""The native discovery layer: one table, four files, and a flag that is off.

`world/world/native_workspace.py` writes what a data engineer finds on the box
when the harness runs `--driver native`. Three things can go wrong with it and
each has a class here.

`EndpointTableTest` is the drift check, and it is the reason the endpoint table
may be duplicated at all. `world/` never imports `alkera_gateway`, so the same
nine services and their ports are spelled out in three places - the module's own
table, `gateway/docker/compose.native.yml`'s aliases and episode environment,
and `alkera_gateway.native.serve.STUB_PORTS`. This class reads all three and
requires them to agree. It builds no world, so it is fast, and it is the test
that fails the day somebody moves a port in one file.

`NativeBuildTest` is the flag. It builds the default world three times: once
without `native: true`, twice with. The unflagged build must reproduce the
`semantic_hash` recorded in `world/evidence/acceptance.json` - the gate the
whole world factory rests on, and the reason this feature is opt-in. The two
flagged builds must be byte-identical to each other, because a discovery file
that varies between builds would move the fingerprint under a world nobody
changed. The flagged hash is ALLOWED to differ from the default: the workspace
tree is hashed and two of the four files are in it.

`OnboardingTest` is the fact check. `docs/onboarding.md` is agent-visible prose
assembled at build time, which makes it the one place in the world where a
generator could invent a fact no grader knows about. Every wiki page id and
every job name the page prints is required to be a key that already exists in
the world it was built from.
"""
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import tomllib
import yaml

WORLD_DIR = Path(__file__).resolve().parents[1]
REPO = WORLD_DIR.parent
COMPOSE_NATIVE = REPO / "gateway" / "docker" / "compose.native.yml"
ACCEPTANCE = WORLD_DIR / "evidence" / "acceptance.json"
SEED = WORLD_DIR / "seed.example.yaml"
LOCK = WORLD_DIR / "requirements.lock"

sys.path.insert(0, str(WORLD_DIR))

from world.themes import load_theme  # noqa: E402

from world import build, fingerprint, native_workspace  # noqa: E402

try:  # the gateway half of the drift check, when the gateway is importable
    sys.path.insert(0, str(REPO / "gateway" / "src"))
    from alkera_gateway.native import serve as native_serve
except Exception:  # pragma: no cover - reported as a skip, never silent
    native_serve = None

try:  # the harness half: the table the subject's environment is built from.
    # `tasks/harness` and not `tasks`, because the harness modules import each
    # other by bare name (`import episode`) rather than as a package.
    sys.path.insert(0, str(REPO / "tasks" / "harness"))
    from agent_under_test import NATIVE_SERVICES as harness_services
    from agent_under_test import NATIVE_WORKSPACE_SERVICES as harness_planes
except Exception:  # pragma: no cover
    harness_services = None
    harness_planes = None


class _ComposeLoader(yaml.SafeLoader):
    """A YAML loader that understands compose's `!override` tag.

    `!override` tells compose to replace a list or mapping instead of merging
    it; to a plain `safe_load` it is an unknown tag and the file will not
    parse. Read here rather than shelled out to `docker compose config`,
    because this test's job is to catch a table drifting out of the file and it
    should run on a host with no docker daemon.
    """


def _override(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_constructor("!override", _override)


def compose_native() -> dict:
    return yaml.load(COMPOSE_NATIVE.read_text(encoding="utf-8"), Loader=_ComposeLoader)


class EndpointTableTest(unittest.TestCase):
    """The three copies of the endpoint table agree."""

    @classmethod
    def setUpClass(cls):
        cls.compose = compose_native()
        cls.env = native_workspace.environment()

    def test_compose_episode_environment_is_the_table(self):
        """Every variable compose puts in the episode's environment has the
        same value the workspace's own .envrc exports. Both exist on purpose -
        the .envrc is what an engineer reads, the environment is what a process
        that never sourced it still sees - and the two disagreeing would give
        the agent two different answers for one endpoint."""
        declared = self.compose["services"]["episode"]["environment"]
        overlap = {k: str(v) for k, v in declared.items() if k in self.env}
        self.assertTrue(overlap, "compose declares none of the table's variables")
        for key, value in sorted(overlap.items()):
            self.assertEqual(self.env[key], value, f"{key} differs")
        # Anything compose declares and the table does not must be a variable
        # that is not an endpoint at all, or a workspace plane the .envrc
        # withholds on purpose (WORKSPACE_PLANES: `SCHED_URL` set is what
        # switches `./ops/sched` into its container mode, so the build-time
        # .envrc, shared by both topologies, must not export it).
        planes = {p.url_var for p in native_workspace.WORKSPACE_PLANES}
        not_in_table = sorted(set(declared) - set(self.env) - planes)
        self.assertEqual(not_in_table, ["ALKERA_NATIVE_AUDIT", "HOME"],
                         "compose declares an endpoint the .envrc does not export")
        for plane in native_workspace.WORKSPACE_PLANES:
            self.assertEqual(str(declared.get(plane.url_var)), plane.url,
                             f"{plane.url_var}: compose and the plane table differ")
            self.assertNotIn(plane.url_var, self.env,
                             f"{plane.url_var} in .envrc would flip the host-topology shim")

    def test_compose_aliases_are_the_service_table(self):
        aliases = set(
            self.compose["services"]["native-services"]["networks"]["episode_net"]["aliases"]
        )
        expected = {f"{service.name}.internal" for service in native_workspace.SERVICES}
        expected |= {plane.alias for plane in native_workspace.WORKSPACE_PLANES}
        self.assertEqual(aliases - {"native-services.internal"}, expected)

    @unittest.skipIf(harness_planes is None, "tasks/harness not importable")
    def test_workspace_planes_match_the_harness(self):
        """`agent_under_test.NATIVE_WORKSPACE_SERVICES` and WORKSPACE_PLANES are
        one table: same names, same variables, same ports."""
        mine = {p.name: (p.url_var, p.port) for p in native_workspace.WORKSPACE_PLANES}
        self.assertEqual(mine, dict(harness_planes))

    @unittest.skipIf(native_serve is None, "alkera_gateway not importable")
    def test_workspace_planes_match_the_server(self):
        """The plane is served where the episode's `SCHED_URL` says it is."""
        (sched,) = native_workspace.WORKSPACE_PLANES
        self.assertEqual(native_serve.SCHED_PORT, sched.port)

    def test_backend_aliases_are_declared(self):
        """Every `<name>.internal` the table points a client at is an alias some
        container actually carries. An endpoint variable naming a host nothing
        answers on is worse than no variable at all."""
        declared = set()
        for spec in self.compose["services"].values():
            networks = spec.get("networks") or {}
            if isinstance(networks, list):
                continue
            for net in networks.values():
                declared.update((net or {}).get("aliases") or [])
        for _, where, _ in native_workspace.BACKEND_NOTES:
            self.assertIn(where.split(":")[0], declared)

    @unittest.skipIf(native_serve is None, "alkera_gateway not importable")
    def test_ports_match_the_server(self):
        """The workspace table and `serve.STUB_PORTS` are one table."""
        table = {service.name: service.port for service in native_workspace.SERVICES}
        self.assertEqual(table, native_serve.STUB_PORTS)

    @unittest.skipIf(harness_services is None, "tasks/harness not importable")
    def test_ports_and_variables_match_the_harness(self):
        """`agent_under_test.NATIVE_SERVICES` is the authority.

        It is what `--driver native` puts in the subject's own environment
        (`tasks/harness/NATIVE_DRIVER_REQUESTS.md` item 3). If this table
        disagrees, the subject's `JIRA_URL` points at a closed socket and the
        native surface gate reports a dead world - a failure that reads as the
        agent's, which is the worst kind."""
        mine = {s.name: (s.url_var, s.port) for s in native_workspace.SERVICES}
        self.assertEqual(mine, dict(harness_services))

    @unittest.skipIf(native_serve is None, "alkera_gateway not importable")
    def test_the_service_modules_listen_where_the_subject_is_told(self):
        """The other half of the same contract, and the one that is not ours.

        A service module declares its own `PORT` and `serve.py` serves it
        there; the subject is told the number in the table above. This test is
        the only place the two meet, so it names the modules that have to
        move rather than letting a rollout discover it."""
        from alkera_gateway.native import services as native_services

        table = {service.name: service.port for service in native_workspace.SERVICES}
        loaded, _ = native_services.load()
        wrong = {name: (int(module.PORT), table[name])
                 for name, module in sorted(loaded.items())
                 if name in table and int(module.PORT) != table[name]}
        self.assertEqual(
            wrong, {},
            "service modules listen on a port the subject is not told about "
            "(module, subject): see gateway/docker/COMPOSE_REQUESTS.md")
        hosts = {name: module.HOST for name, module in loaded.items()}
        self.assertEqual(
            hosts,
            {s.name: s.alias for s in native_workspace.SERVICES if s.name in hosts})

    def test_services_toml_is_valid_toml_and_holds_the_table(self):
        parsed = tomllib.loads(native_workspace.services_toml("/world"))
        self.assertEqual(sorted(parsed["service"]),
                         sorted(s.name for s in native_workspace.SERVICES))
        for service in native_workspace.SERVICES:
            entry = parsed["service"][service.name]
            self.assertEqual(entry["url"], service.url)
            self.assertEqual(entry["port"], service.port)
            self.assertEqual(entry["env"], service.url_var)
        self.assertEqual(parsed["warehouse"]["host"], native_workspace.POSTGRES["PGHOST"])
        self.assertEqual(parsed["world"]["workspace"], "/world/workspace")

    def test_envrc_block_is_idempotent(self):
        block = native_workspace.envrc_block()
        original = "# the theme's own file\nexport DBT_TARGET=dev\n"
        once = native_workspace.merge_envrc(original, block)
        twice = native_workspace.merge_envrc(once, block)
        self.assertEqual(once, twice)
        self.assertEqual(once.count(native_workspace.ENVRC_BEGIN), 1)
        self.assertIn("export DBT_TARGET=dev", once)
        self.assertIn('export JIRA_URL="http://jira.internal:8080"', once)

    def test_profiles_point_at_the_served_warehouse(self):
        parsed = yaml.safe_load(native_workspace.profiles_yaml("world_dbt"))
        output = parsed["world_dbt"]["outputs"]["dev"]
        self.assertEqual(output["type"], "postgres")
        self.assertEqual(output["host"], native_workspace.POSTGRES["PGHOST"])
        self.assertEqual(output["threads"], 1)

    def test_credentials_are_obviously_fake(self):
        """Repository rule: nothing in a world may look like a real key."""
        for key, value in native_workspace.environment().items():
            if any(word in key for word in ("TOKEN", "PASSWORD", "SECRET", "KEY")):
                self.assertIn("alkera-local-dev", value, key)


class NativeBuildTest(unittest.TestCase):
    """The flag: off by default, deterministic when on."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="native-ws-"))
        cls.plain = cls.tmp / "plain"
        cls.n1, cls.n2 = cls.tmp / "n1", cls.tmp / "n2"
        seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
        plain_seed = cls.tmp / "plain.yaml"
        native_seed = cls.tmp / "native.yaml"
        plain_seed.write_text(yaml.safe_dump(seed, sort_keys=True), encoding="utf-8")
        native_seed.write_text(yaml.safe_dump({**seed, "native": True}, sort_keys=True),
                               encoding="utf-8")
        build.build(plain_seed, cls.plain, LOCK)
        build.build(native_seed, cls.n1, LOCK)
        build.build(native_seed, cls.n2, LOCK)
        cls.h_plain = fingerprint.semantic_hash(cls.plain)
        cls.h1 = fingerprint.semantic_hash(cls.n1)
        cls.h2 = fingerprint.semantic_hash(cls.n2)
        print(f"\nunflagged semantic_hash: {cls.h_plain}")
        print(f"flagged   semantic_hash: {cls.h1}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_unflagged_build_matches_the_recorded_default(self):
        recorded = json.loads(ACCEPTANCE.read_text(encoding="utf-8"))
        self.assertEqual(
            self.h_plain, recorded["gate1_host_semantic_hash"],
            "the default world's fingerprint moved; the native flag is opt-in "
            "precisely so this cannot happen")

    def test_the_flag_is_off_by_default(self):
        self.assertFalse((self.plain / native_workspace.HOME).exists())
        self.assertFalse((self.plain / "workspace" / "docs" / "onboarding.md").exists())
        envrc = (self.plain / "workspace" / ".envrc").read_text(encoding="utf-8")
        self.assertNotIn(native_workspace.ENVRC_BEGIN, envrc)

    def test_the_flag_writes_exactly_four_files(self):
        expected = [
            self.n1 / "workspace" / ".envrc",
            self.n1 / "workspace" / "docs" / "onboarding.md",
            self.n1 / native_workspace.HOME / ".dbt" / "profiles.yml",
            self.n1 / native_workspace.HOME / ".config" / "alkera" / "services.toml",
        ]
        for path in expected:
            self.assertTrue(path.is_file(), path)
        home_files = sorted(
            p.relative_to(self.n1).as_posix()
            for p in (self.n1 / native_workspace.HOME).rglob("*") if p.is_file())
        self.assertEqual(home_files,
                         ["home/.config/alkera/services.toml", "home/.dbt/profiles.yml"])

    def test_two_flagged_builds_are_byte_identical(self):
        relative = [
            Path("workspace") / ".envrc",
            Path("workspace") / "docs" / "onboarding.md",
            Path(native_workspace.HOME) / ".dbt" / "profiles.yml",
            Path(native_workspace.HOME) / ".config" / "alkera" / "services.toml",
        ]
        for item in relative:
            first, second = (self.n1 / item).read_bytes(), (self.n2 / item).read_bytes()
            self.assertEqual(first, second, f"{item} is not deterministic")
            self.assertNotIn(b"\r", first, f"{item} is not LF")
        # ASCII is a rule about what this module GENERATES. `.envrc` is the
        # theme's own file with a block appended, and the theme's half already
        # carries an em dash.
        block = native_workspace.envrc_block()
        self.assertTrue(block.isascii())
        for item in relative[1:]:
            self.assertTrue((self.n1 / item).read_bytes().isascii(), f"{item} is not ASCII")
        self.assertIn(block.rstrip("\n"),
                      (self.n1 / relative[0]).read_text(encoding="utf-8"))
        self.assertEqual(self.h1, self.h2,
                         "two flagged builds of one seed are not one world")

    def test_the_flagged_world_is_a_different_world(self):
        """Allowed, and required to be visible: the workspace is fingerprinted
        and two of the four files live in it, so a flagged build MUST NOT pass
        as the unflagged one."""
        self.assertNotEqual(self.h1, self.h_plain)

    def test_the_workspace_files_are_committed(self):
        """On a real box the checked-in files are checked in. `_git` runs after
        the discovery layer for exactly this reason, and a file the commit
        missed would show up as a dirty tree on the agent's first `git status`."""
        import subprocess
        status = subprocess.run(["git", "status", "--porcelain"],
                                cwd=self.n1 / "workspace", capture_output=True, text=True)
        self.assertEqual(status.stdout.strip(), "")


class OnboardingTest(unittest.TestCase):
    """The onboarding page states nothing the world does not already say."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="native-onboarding-"))
        cls.root = cls.tmp / "w"
        seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
        path = cls.tmp / "native.yaml"
        path.write_text(yaml.safe_dump({**seed, "native": True}, sort_keys=True),
                        encoding="utf-8")
        build.build(path, cls.root, LOCK)
        cls.theme = load_theme(seed["theme"])
        cls.page = (cls.root / "workspace" / "docs" / "onboarding.md").read_text(
            encoding="utf-8")
        cls.kb = json.loads(
            (cls.root / "var" / "workflow" / "kb.json").read_text(encoding="utf-8"))
        cls.issues = json.loads(
            (cls.root / "var" / "workflow" / "issues.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_page_id_it_names_exists(self):
        ids = {page["id"] for page in self.kb["pages"]}
        named = set(re.findall(r"^\s*- `([a-z0-9_\-./]+)` - ", self.page, re.M))
        self.assertTrue(named, "the page lists no runbooks at all")
        self.assertEqual(named - ids, set(),
                         "onboarding.md names a wiki page the world does not have")
        self.assertEqual(ids - named, set(),
                         "onboarding.md hides a wiki page the agent can list anyway")

    def test_every_job_it_names_exists(self):
        jobs = set(self.theme.manifest["jobs"])
        named = set(re.findall(r"^\| `([a-z0-9_]+)` \| `", self.page, re.M))
        self.assertEqual(named, jobs)

    def test_it_carries_no_authoring_notes(self):
        """The corpus's author-side `authoring.note` blocks are the solution
        manual. `workflow.py` strips them from every tool response; a generated
        page that pasted one back in would be a hint channel."""
        for page in self.kb["pages"]:
            note = (page.get("authoring") or {}).get("note")
            if note:
                self.assertNotIn(note, self.page)
        for issue in self.issues["issues"]:
            note = (issue.get("authoring") or {}).get("note")
            if note:
                self.assertNotIn(note, self.page)

    def test_it_carries_no_ticket_bodies(self):
        """Counts by status, never the tickets themselves. `issues_list` is how
        the agent reads the queue; a page that pre-digested it would change what
        the task measures."""
        for issue in self.issues["issues"]:
            self.assertNotIn(issue["title"], self.page)

    def test_it_names_every_service(self):
        for service in native_workspace.SERVICES:
            self.assertIn(f"`{service.alias}:{service.port}`", self.page)


class WriteForBuiltWorldTest(unittest.TestCase):
    """Discovery files for a fixture world that never set native: true."""

    def test_workspace_only_writes_envrc_and_onboarding_not_postgres_profiles(self):
        tmp = Path(tempfile.mkdtemp(prefix="native-built-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / "w"
        (root / "workspace").mkdir(parents=True)
        native_workspace.write_for_built_world(root)
        envrc = (root / "workspace" / ".envrc").read_text(encoding="utf-8")
        self.assertIn("JIRA_URL", envrc)
        self.assertTrue((root / "workspace" / "docs" / "onboarding.md").is_file())
        self.assertFalse((root / native_workspace.HOME / ".dbt" / "profiles.yml").exists())

    def test_onboarding_jobs_come_only_from_sched_state(self):
        tmp = Path(tempfile.mkdtemp(prefix="native-built-jobs-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / "w"
        (root / "workspace").mkdir(parents=True)
        state = root / "var" / "sched" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({
            "vnow": "2026-01-01T00:00:00Z",
            "jobs": {"nightly_load": {"command": "script"}},
            "runs": [],
        }), encoding="utf-8")
        native_workspace.write_for_built_world(root)
        page = (root / "workspace" / "docs" / "onboarding.md").read_text(
            encoding="utf-8")
        self.assertIn("`nightly_load`", page)
        named = set(re.findall(r"^\| `([a-z0-9_]+)` \| `", page, re.M))
        self.assertEqual(named, {"nightly_load"})


if __name__ == "__main__":
    unittest.main()
