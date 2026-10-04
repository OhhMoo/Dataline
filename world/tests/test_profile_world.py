"""Profile-aware seeding: a world built for the slots an environment plugs.

`alkera_gateway.plugs` decides which SLOTS an environment profile plugs
(`da/laptop` has no orchestrator, no ingest, no observability, no release);
the harness resolves that list and writes it into the seed as
`profile: {name, slots}`. The world only honours the list - it never imports
the gateway - and an unplugged slot must leave NO state behind: a world that
seeds `var/workflow` for a profile without a `human` slot has a plane no tool
can reach, and a fingerprint that hashes something the episode cannot see.

Four claims, one class each:

- `UnflaggedTest`: a seed without `profile:` is the world it always was - the
  recorded gate-1 hash, no provenance key, no record file.
- `ProfileBuildTest`: unplugged slots leave nothing behind; the profile is
  recorded in provenance and as the conditional `profile` fingerprint section;
  two profiles over one seed hash differently; and a profile that plugs every
  slot is the unflagged world plus exactly that one section.
- `ProfileRefusalTest`: an unknown slot, a malformed profile, or `files:`
  asked of a profile with no `files` slot is refused before anything is built.
- `NativeProfileTest`: `native: true` under a narrow profile names no service
  the profile unplugged, in any of the three files the agent discovers from.
- `BIVendorDiscoveryTest` / `BIVendorBuildTest`: the explicit-only BI vendors
  (powerbi, tableau, sigma) are discoverable only in a world whose profile
  record names them under `plugs: {bi: [...]}` - never unprofiled, never on a
  profile that plugs `bi` without saying which vendor - and the optional
  `plugs` key leaves every existing profile record as it was.
"""
import json
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

import yaml

WORLD_DIR = Path(__file__).resolve().parents[1]
REPO = WORLD_DIR.parent
ACCEPTANCE = WORLD_DIR / "evidence" / "acceptance.json"
SEED = WORLD_DIR / "seed.example.yaml"
SFTP_SEED = WORLD_DIR / "world" / "themes" / "sftp_vendor" / "seed.example.yaml"
LOCK = WORLD_DIR / "requirements.lock"

sys.path.insert(0, str(WORLD_DIR))

from world import build, fingerprint, native_workspace, scheduler  # noqa: E402

try:  # the gateway's slot catalogue, for the drift check only
    sys.path.insert(0, str(REPO / "gateway" / "src"))
    from alkera_gateway import plugs as gateway_plugs
except Exception:  # pragma: no cover - reported as a skip, never silent
    gateway_plugs = None

#: `da/laptop` with the optional surfaces unplugged as well: no human, no bi.
#: Narrow on purpose, so every conditional plane is exercised as absent.
NARROW = {"name": "da/narrow", "slots": ["files", "warehouse", "transform"]}
#: The real `da` role on the laptop stack (alkera_gateway.plugs.ROLES["da"]).
DA = {"name": "da/laptop",
      "slots": ["files", "notebook", "warehouse", "transform", "bi", "human"]}
EVERYTHING = {"name": "all/laptop", "slots": list(build.SLOTS)}


def _seed_with(tmp: Path, name: str, base: Path = SEED, **extra) -> Path:
    seed = yaml.safe_load(base.read_text(encoding="utf-8"))
    path = tmp / f"{name}.yaml"
    path.write_text(yaml.safe_dump({**seed, **extra}, sort_keys=True), encoding="utf-8")
    return path


class SlotCatalogueTest(unittest.TestCase):
    """The world's copy of the slot names and the native service -> slot map
    agree with the gateway's. Duplicated, not imported, for the layering rule
    (tasks -> gateway -> world); this is what makes the duplication safe."""

    @unittest.skipIf(gateway_plugs is None, "alkera_gateway not importable")
    def test_slot_names_match_the_gateway(self):
        self.assertEqual(tuple(build.SLOTS), tuple(gateway_plugs.SLOTS))

    @unittest.skipIf(gateway_plugs is None, "alkera_gateway not importable")
    def test_native_service_slots_match_the_gateway_plugs(self):
        theirs = {svc: plug.slot for plug in gateway_plugs.PLUGS.values()
                  for svc in plug.services}
        ours = {s.name: native_workspace.SERVICE_SLOTS[s.name]
                for s in (*native_workspace.SERVICES, *native_workspace.PLUG_SERVICES)}
        self.assertEqual(ours, theirs)

    @unittest.skipIf(gateway_plugs is None, "alkera_gateway not importable")
    def test_native_service_plugs_match_the_gateway_plugs(self):
        """Which PLUG mounts each service: what a record's `plugs` is read by."""
        theirs = {svc: plug.name for plug in gateway_plugs.PLUGS.values()
                  for svc in plug.services}
        self.assertEqual(native_workspace.SERVICE_PLUGS, theirs)


class UnflaggedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="profile-plain-"))
        cls.root = build.build(SEED, cls.tmp / "plain", LOCK)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_unflagged_build_matches_the_recorded_default(self):
        recorded = json.loads(ACCEPTANCE.read_text(encoding="utf-8"))
        self.assertEqual(fingerprint.semantic_hash(self.root),
                         recorded["gate1_host_semantic_hash"])

    def test_no_profile_record_anywhere(self):
        self.assertNotIn("profile", fingerprint.semantic(self.root))
        self.assertFalse((self.root / build.PROFILE_RECORD).exists())
        prov = json.loads((self.root / "build" / "provenance.json").read_text())
        self.assertNotIn("profile", prov)
        self.assertNotIn("files", prov)


class ProfileBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="profile-build-"))
        cls.plain = build.build(SEED, cls.tmp / "plain", LOCK)
        cls.narrow = build.build(_seed_with(cls.tmp, "narrow", profile=NARROW),
                                 cls.tmp / "narrow", LOCK)
        cls.da = build.build(_seed_with(cls.tmp, "da", profile=DA), cls.tmp / "da", LOCK)
        cls.all = build.build(_seed_with(cls.tmp, "all", profile=EVERYTHING),
                              cls.tmp / "all", LOCK)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_unplugged_slots_leave_no_state(self):
        var = self.narrow / "var"
        self.assertFalse((var / "workflow").exists(), "human unplugged, workflow seeded")
        self.assertFalse((var / "obs").exists(), "observability unplugged, obs seeded")
        self.assertEqual(list((var / "bi" / "dashboards").glob("*")), [],
                         "bi unplugged, dashboards seeded")
        self.assertNotIn("workflow", fingerprint.semantic(self.narrow))
        self.assertNotIn("obs", fingerprint.semantic(self.narrow))

    def test_the_always_built_core_is_still_built(self):
        for rel in ("workspace/.git", "var/warehouse/warehouse.duckdb",
                    "var/sched/state.json", "var/world/live.json"):
            self.assertTrue((self.narrow / rel).exists(), rel)
        # The warehouse is the same warehouse: a profile selects planes, it
        # does not change the data.
        self.assertEqual(fingerprint.semantic(self.narrow)["warehouse"],
                         fingerprint.semantic(self.plain)["warehouse"])

    def test_plugged_slots_are_seeded(self):
        self.assertTrue((self.da / "var" / "workflow" / "issues.json").is_file())
        self.assertTrue(list((self.da / "var" / "bi" / "dashboards").glob("*.yml")))
        self.assertFalse((self.da / "var" / "obs").exists())

    def test_advance_writes_no_samples_without_observability(self):
        """observe.record is a no-op without definitions, so the clock moving
        must not resurrect var/obs (scheduler.advance appends after every run)."""
        copy = self.tmp / "narrow-advanced"
        shutil.copytree(self.narrow, copy)
        scheduler.advance(copy, 24)
        self.assertFalse((copy / "var" / "obs").exists())

    def test_profile_recorded_in_provenance_and_fingerprint(self):
        prov = json.loads((self.da / "build" / "provenance.json").read_text())
        self.assertEqual(prov["profile"], DA)
        sem = fingerprint.semantic(self.da)
        self.assertIn("profile", sem)
        self.assertEqual(json.loads((self.da / build.PROFILE_RECORD).read_text()), DA)

    def test_slots_are_recorded_in_canonical_order(self):
        """Seed order is not world state: the same set spelled in another order
        is the same profile and must hash the same."""
        shuffled = {"name": DA["name"], "slots": list(reversed(DA["slots"]))}
        self.assertEqual(build.resolve_profile({"profile": shuffled}), DA)

    def test_two_profiles_over_one_seed_hash_differently(self):
        self.assertNotEqual(fingerprint.semantic_hash(self.narrow),
                            fingerprint.semantic_hash(self.da))

    def test_every_slot_is_the_unflagged_world_plus_one_section(self):
        full = fingerprint.semantic(self.all)
        plain = fingerprint.semantic(self.plain)
        self.assertEqual(set(full) - set(plain), {"profile"})
        full.pop("profile")
        self.assertEqual(full, plain)


class ProfileRefusalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="profile-refuse-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _refused(self, needle: str, **extra):
        seed = _seed_with(self.tmp, "bad", **extra)
        with self.assertRaises(SystemExit) as ctx:
            build.build(seed, self.tmp / "w", LOCK)
        self.assertIn(needle, str(ctx.exception))
        # Refused before anything was written.
        self.assertFalse((self.tmp / "w" / "workspace").exists())

    def test_unknown_slot(self):
        self._refused("lakehouse", profile={"name": "x", "slots": ["warehouse", "lakehouse"]})

    def test_duplicate_slot(self):
        self._refused("twice", profile={"name": "x", "slots": ["bi", "bi"]})

    def test_missing_name(self):
        self._refused("name", profile={"slots": ["warehouse"]})

    def test_slots_not_a_list(self):
        self._refused("slots", profile={"name": "x", "slots": "warehouse"})

    def test_files_without_a_files_slot(self):
        self._refused("files", profile={"name": "x", "slots": ["warehouse"]},
                      files={"formats": ["json"]})


class IngestProfileTest(unittest.TestCase):
    """The only theme with a connector plane: an unplugged `ingest` slot
    leaves no var/ingest behind."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="profile-ingest-"))
        cls.plain = build.build(SFTP_SEED, cls.tmp / "plain", LOCK)
        cls.root = build.build(
            _seed_with(cls.tmp, "noingest", base=SFTP_SEED, profile=NARROW),
            cls.tmp / "noingest", LOCK)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_ingest_unplugged(self):
        self.assertTrue((self.plain / "var" / "ingest").is_dir(),
                        "precondition: sftp_vendor seeds a connector plane")
        self.assertFalse((self.root / "var" / "ingest").exists())
        self.assertNotIn("ingest", fingerprint.semantic(self.root))


class NativeProfileTest(unittest.TestCase):
    """The discovery files name only what the profile plugged."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="profile-native-"))
        cls.root = build.build(
            _seed_with(cls.tmp, "native", profile=NARROW, native=True),
            cls.tmp / "native", LOCK)
        cls.full = build.build(_seed_with(cls.tmp, "native-full", native=True),
                               cls.tmp / "native-full", LOCK)
        ws = cls.root / "workspace"
        cls.onboarding = (ws / "docs" / "onboarding.md").read_text(encoding="utf-8")
        cls.envrc = (ws / ".envrc").read_text(encoding="utf-8")
        cls.toml_text = (cls.root / native_workspace.HOME / ".config" / "alkera"
                         / "services.toml").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_no_excluded_service_is_named(self):
        excluded = [s for s in native_workspace.SERVICES
                    if native_workspace.SERVICE_SLOTS[s.name] not in NARROW["slots"]]
        self.assertTrue(excluded)
        for service in excluded:
            for label, text in (("onboarding.md", self.onboarding),
                                ("services.toml", self.toml_text),
                                (".envrc block", self.envrc)):
                self.assertNotIn(service.alias, text, f"{service.name} in {label}")
                self.assertNotIn(service.url_var, text, f"{service.name} in {label}")

    def test_unplugged_backends_are_not_named(self):
        # object storage and the scheduler are unplugged in NARROW
        for text in (self.onboarding, self.toml_text, self.envrc):
            self.assertNotIn("minio.internal", text)
            self.assertNotIn("airflow.internal", text)
        parsed = tomllib.loads(self.toml_text)
        self.assertNotIn("storage", parsed)
        self.assertNotIn("scheduler", parsed)
        self.assertEqual(parsed.get("service", {}), {})
        self.assertIn("warehouse", parsed)

    def test_onboarding_has_no_pipeline_without_an_orchestrator(self):
        self.assertNotIn("## The pipeline", self.onboarding)

    def test_the_warehouse_is_still_discoverable(self):
        self.assertIn("postgres.internal", self.onboarding)
        self.assertIn("PGHOST", self.envrc)
        self.assertTrue((self.root / native_workspace.HOME / ".dbt" / "profiles.yml")
                        .is_file())

    def test_unprofiled_native_build_still_names_every_service(self):
        page = (self.full / "workspace" / "docs" / "onboarding.md").read_text()
        for service in native_workspace.SERVICES:
            self.assertIn(service.alias, page)


class ProfilePlugsRecordTest(unittest.TestCase):
    """`profile: {name, slots, plugs?}` - the optional plug record."""

    def test_plugs_recorded_only_when_given(self):
        self.assertNotIn("plugs", build.resolve_profile({"profile": DA}))
        # an empty mapping says nothing, so it is the same world as no key
        self.assertEqual(build.resolve_profile({"profile": {**DA, "plugs": {}}}), DA)

    def test_plugs_are_canonicalised(self):
        got = build.resolve_profile({"profile": {
            **DA, "plugs": {"bi": ["tableau", "powerbi"], "warehouse": ["duckdb"]}}})
        self.assertEqual(got, {**DA, "plugs": {"warehouse": ["duckdb"],
                                               "bi": ["powerbi", "tableau"]}})
        self.assertEqual(list(got["plugs"]), ["warehouse", "bi"])  # SLOTS order

    def test_malformed_plugs_are_refused(self):
        for plugs, needle in (
                ("powerbi", "mapping"),
                ({"lakehouse": ["x"]}, "lakehouse"),
                ({"orchestrator": ["ops_sched"]}, "orchestrator"),  # not in DA's slots
                ({"bi": "powerbi"}, "list"),
                ({"bi": []}, "list"),
                ({"bi": [3]}, "string"),
                ({"bi": [""]}, "string"),
                ({"bi": ["powerbi", "powerbi"]}, "twice")):
            with self.assertRaises(SystemExit, msg=plugs) as ctx:
                build.resolve_profile({"profile": {**DA, "plugs": plugs}})
            self.assertIn(needle, str(ctx.exception), plugs)

    def test_unknown_profile_key_is_still_refused(self):
        with self.assertRaises(SystemExit):
            build.resolve_profile({"profile": {**DA, "plug": {"bi": ["sigma"]}}})


#: The three vendors, the variables their clients read, and the port.
VENDORS = {
    "powerbi": (8095, ("POWERBI_API_URL", "FABRIC_API_URL", "AZURE_AUTHORITY_HOST",
                       "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID")),
    "tableau": (8096, ("TABLEAU_SERVER_URL", "TABLEAU_SITE", "TABLEAU_PAT_NAME",
                       "TABLEAU_PAT_SECRET")),
    "sigma": (8097, ("SIGMA_BASE_URL", "SIGMA_CLIENT_ID", "SIGMA_CLIENT_SECRET")),
}


def _every_vendor_word():
    for name, (_, names) in VENDORS.items():
        yield f"{name}.internal"
        yield from names


class BIVendorDiscoveryTest(unittest.TestCase):
    """The discovery functions, called directly: no world is built."""

    def _texts(self, slots=None, plugs=None):
        nw = native_workspace
        root = Path(tempfile.mkdtemp(prefix="bi-onboarding-"))
        self.addCleanup(shutil.rmtree, root, True)
        kw = {} if plugs is None else {"plugs": plugs}
        return {"env": "\n".join(f"{k}={v}" for k, v in nw.environment(slots, **kw).items()),
                ".envrc": nw.envrc_block(slots, **kw),
                "services.toml": nw.services_toml("/world", slots, **kw),
                "onboarding.md": nw.onboarding_markdown(root, {}, slots, **kw)}

    def test_never_named_unprofiled_or_without_plugs(self):
        for label, texts in (("unprofiled", self._texts()),
                             ("da, no plugs", self._texts(DA["slots"])),
                             ("da, bi=metabase", self._texts(
                                 DA["slots"], {"bi": ["metabase_fake"]}))):
            for where, text in texts.items():
                for word in _every_vendor_word():
                    self.assertNotIn(word, text, f"{label}: {word} in {where}")

    def test_unprofiled_output_is_unchanged_by_an_absent_plugs(self):
        nw = native_workspace
        for slots in (None, DA["slots"]):
            self.assertEqual(nw.environment(slots), nw.environment(slots, plugs=None))
            self.assertEqual(nw.envrc_block(slots), nw.envrc_block(slots, plugs=None))
            self.assertEqual(nw.services_toml("/w", slots),
                             nw.services_toml("/w", slots, plugs=None))
        # bi=metabase_fake is what `bi` meant before vendors existed
        self.assertEqual(nw.envrc_block(DA["slots"]),
                         nw.envrc_block(DA["slots"], plugs={"bi": ["metabase_fake"]}))

    def test_each_vendor_is_named_only_when_plugged(self):
        for name, (port, names) in VENDORS.items():
            texts = self._texts(DA["slots"], {"bi": [name]})
            env = native_workspace.environment(DA["slots"], plugs={"bi": [name]})
            self.assertEqual(env[names[0]], f"http://{name}.internal:{port}")
            for var in names:
                self.assertIn(f"export {var}=", texts[".envrc"], var)
            self.assertIn(f"`{name}.internal:{port}`", texts["onboarding.md"])
            parsed = tomllib.loads(texts["services.toml"])
            self.assertEqual(parsed["service"][name]["port"], port)
            self.assertEqual(parsed["service"][name]["env"], names[0])
            # metabase is not what this box's BI is any more
            for where, text in texts.items():
                self.assertNotIn("metabase.internal", text, (name, where))
                self.assertNotIn("METABASE_URL", text, (name, where))
                for other in set(VENDORS) - {name}:
                    self.assertNotIn(f"{other}.internal", text, (name, other, where))

    def test_reached_with_is_the_vendors_own_tooling(self):
        page = self._texts(DA["slots"], {"bi": ["powerbi", "tableau", "sigma"]})[
            "onboarding.md"]
        rows = {line.split("|")[1].strip(): line for line in page.splitlines()
                if line.startswith("| ") and ".internal:" in line}
        self.assertIn("Fabric REST", rows["powerbi"])
        self.assertIn("Entra", rows["powerbi"])
        self.assertIn("tabcmd", rows["tableau"])
        self.assertIn("Tableau Server Client", rows["tableau"])
        self.assertIn("REST API v2", rows["sigma"])
        self.assertIn("`SIGMA_BASE_URL`", rows["sigma"])

    def test_the_credentials_are_the_fake_ones(self):
        env = native_workspace.environment(DA["slots"],
                                           plugs={"bi": list(VENDORS)})
        for var in ("AZURE_CLIENT_SECRET", "TABLEAU_PAT_SECRET", "SIGMA_CLIENT_SECRET"):
            self.assertEqual(env[var], native_workspace.FAKE_TOKEN)
        self.assertEqual(env["TABLEAU_SITE"], "")   # the Default site's contentUrl

    def test_ports_and_variables_match_the_gateway_and_harness(self):
        try:
            from alkera_gateway.native import serve
            sys.path.insert(0, str(REPO / "tasks" / "harness"))
            import agent_under_test as aut
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"gateway/harness not importable: {exc}")
        ours = {s.name: s.port for s in native_workspace.PLUG_SERVICES}
        self.assertEqual(ours, serve.PLUG_PORTS)
        self.assertEqual({s.name: (s.url_var, s.port)
                          for s in native_workspace.PLUG_SERVICES},
                         dict(aut.PLUG_SERVICES))
        env = native_workspace.environment(DA["slots"], plugs={"bi": list(VENDORS)})
        for name, (_, port) in aut.PLUG_SERVICES.items():
            theirs = aut.plug_service_env(name, f"http://{name}.internal:{port}")
            self.assertEqual({k: env[k] for k in theirs}, theirs, name)


class BIVendorBuildTest(unittest.TestCase):
    """One world, built for a Power BI shop: the record carries the plug, and
    the discovery files name Power BI and not Metabase."""

    PROFILE = {**DA, "plugs": {"bi": ["powerbi"]}}

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="profile-bi-"))
        # A BI vendor needs a theme with a `bi_projects` hook (world/bi_projects.py
        # refuses one without): commerce_analytics is the BI-heavy theme.
        commerce = WORLD_DIR / "world" / "themes" / "commerce_analytics" / "seed.example.yaml"
        cls.root = build.build(
            _seed_with(cls.tmp, "powerbi", base=commerce, profile=cls.PROFILE, native=True),
            cls.tmp / "powerbi", LOCK)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_record_carries_the_plugs(self):
        self.assertEqual(json.loads((self.root / build.PROFILE_RECORD).read_text()),
                         self.PROFILE)
        prov = json.loads((self.root / "build" / "provenance.json").read_text())
        self.assertEqual(prov["profile"], self.PROFILE)

    def test_the_box_names_power_bi(self):
        envrc = (self.root / "workspace" / ".envrc").read_text(encoding="utf-8")
        self.assertIn('export POWERBI_API_URL="http://powerbi.internal:8095"', envrc)
        self.assertNotIn("METABASE_URL", envrc)
        page = (self.root / "workspace" / "docs" / "onboarding.md").read_text()
        self.assertIn("powerbi.internal:8095", page)


if __name__ == "__main__":
    unittest.main()


def test_notebook_server_is_named_only_when_a_profile_plugs_it():
    # "No profile" is the box as it stood before profiles, fingerprinted byte
    # for byte, so the newer notebook plug appears only when a profile names it.
    from world import native_workspace as nw

    assert "JUPYTER_URL" not in nw.environment()
    assert "jupyter.internal" not in nw.envrc_block()
    da = ["files", "notebook", "warehouse", "transform", "bi", "human"]
    assert nw.environment(da)["JUPYTER_URL"] == "http://jupyter.internal:8888"
    assert "JUPYTER_URL" in nw.envrc_block(da)
    assert "JUPYTER_URL" not in nw.environment([s for s in da if s != "notebook"])
