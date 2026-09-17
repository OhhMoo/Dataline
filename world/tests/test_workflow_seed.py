"""The workflow plane, seeded by a theme and proved through the real gateway.

Three levels, and the split is deliberate.

`WorkflowCorpusTest` resolves each theme's corpus without building a world, so
it is fast and it covers all four themes. It asserts *content*, not `ok`: that
the deliberate-imperfection catalogue in `gateway/SURFACES.md#workflow` is
actually planted, that the stale page really is older than the page that
supersedes it, and that the status vocabulary each theme declares is the one
its tickets use. A backlog where every ticket matters is a solution manual, and
the only way to keep that from happening quietly is to check for it.

`WorkflowBuildTest` builds a world twice and requires identical bytes, and
checks that the fingerprint's `workflow` section is conditional - a world
without the directory keeps the hash it had.

`WorkflowRoundTripTest` is the one that matters. It writes through one
`Gateway`, reads the write back through a *different* one over the same world
tree, replays the whole session onto a clone, and requires the two trees to end
byte-identical. A ticketing system that accepts a comment and forgets it cannot
represent the work, and a check that never ran against the thing it ships
against proves nothing - so this runs against a world `world.build` produced,
not a fixture staged to be convenient.

That last class needs the gateway importable. The documented way to run this
suite does that (`cd world && PYTHONPATH=. ../gateway/.venv/Scripts/python -m
pytest tests`); under world's own venv it skips with the reason named.
"""
import hashlib
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import build as wbuild, fingerprint, scheduler, workflow  # noqa: E402

TRACK_A = Path(__file__).resolve().parents[1]
THEMES = TRACK_A / "world" / "themes"
NAMES = ("revenue_daily", "sftp_vendor", "mainframe_batch", "excel_close")
VNOW = "2026-03-03T00:00:00Z"          # every theme's seed.example lands here
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

try:
    from alkera_gateway import Gateway, GatewayConfig
    from alkera_gateway.replay import replay
    GATEWAY = None
except ImportError as exc:                                   # pragma: no cover
    GATEWAY = f"alkera_gateway is not importable from this interpreter: {exc}"


def manifest(name: str) -> dict:
    return yaml.safe_load((THEMES / name / "theme.yaml").read_text(encoding="utf-8"))


def documents(name: str, vnow: str = VNOW) -> dict:
    return workflow.documents(THEMES / name, manifest(name)["workflow"], vnow)


def kinds(document: dict) -> list[str]:
    """Every author-side `kind` in a document, walked rather than enumerated."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            block = node.get("authoring")
            if isinstance(block, dict) and "kind" in block:
                found.append(block["kind"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(document)
    return found


def tree_digest(root: Path) -> str:
    """Bytes of every file under root, excluding what an episode is allowed to
    churn. Used to prove a replayed clone lands on the same world."""
    out = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_file() and not {".git", "audit", "build"} & set(relative.parts):
            out.update(relative.as_posix().encode())
            out.update(path.read_bytes())
    return out.hexdigest()


# --------------------------------------------------------------------------
# the corpus itself
# --------------------------------------------------------------------------

class WorkflowCorpusTest(unittest.TestCase):
    """All four themes, resolved but not built."""

    def test_every_theme_declares_and_seeds_the_surface(self):
        for name in NAMES:
            with self.subTest(theme=name):
                spec = manifest(name)["workflow"]
                docs = documents(name)
                self.assertEqual(sorted(docs), ["chat.json", "issues.json", "kb.json"])
                issues, chat, kb = docs["issues.json"], docs["chat.json"], docs["kb.json"]
                self.assertEqual(issues["project"], spec["project"])
                self.assertEqual(issues["statuses"], spec["statuses"])
                self.assertEqual(chat["workspace"], spec["workspace"])
                self.assertEqual(kb["space"], spec["space"])
                # One roster, both documents, and the agent is in it.
                self.assertEqual(issues["users"], chat["users"])
                self.assertIn(spec["actor"], [u["id"] for u in issues["users"]])
                # A backlog worth reading, not a demo record.
                self.assertGreaterEqual(len(issues["issues"]), 8)
                self.assertGreaterEqual(len(kb["pages"]), 8)
                self.assertGreaterEqual(len(chat["channels"]), 3)
                self.assertGreaterEqual(
                    sum(len(c["messages"]) for c in chat["channels"]), 15)

    def test_the_status_vocabulary_is_per_world_and_actually_used(self):
        """The contract's claim that a theme may run a Jira workflow or a
        two-state one is only true if the themes differ. They do, and every
        ticket's status is in its own world's set."""
        sets = {name: tuple(manifest(name)["workflow"]["statuses"]) for name in NAMES}
        self.assertEqual(len(set(sets.values())), len(NAMES), sets)
        self.assertEqual(sets["sftp_vendor"], ("open", "closed"))
        self.assertIn("pending_change", sets["mainframe_batch"])
        self.assertIn("waiting_on_finance", sets["excel_close"])
        for name in NAMES:
            docs = documents(name)
            legal = set(sets[name])
            used = {issue["status"] for issue in docs["issues.json"]["issues"]}
            self.assertLessEqual(used, legal, name)
            self.assertGreaterEqual(len(used), 2, f"{name} uses one status")

    def test_the_deliberate_imperfection_catalogue_is_planted_in_every_theme(self):
        """SURFACES.md#workflow's table is the content bar. A corpus that
        carries none of it is a solution manual, and nothing downstream would
        say so - the tools would answer every call correctly over a backlog in
        which every ticket mattered."""
        required = {
            "noise", "stale", "near_duplicate_trap", "duplicate", "constraint",
            "undocumented_decision", "unanswered_mention", "confident_wrong",
            "accurate_but_partial", "unactioned_remediation",
            "current_and_contradicting", "incident_report_lay", "accurate",
        }
        for name in NAMES:
            with self.subTest(theme=name):
                present = set(kinds(documents(name)))
                self.assertEqual(required - present, set())

    def test_the_wiki_contradiction_is_resolvable_on_the_dates_it_serves(self):
        """Every theme plants a page that supersedes another and says so. The
        agent can only settle it if the superseding page is the newer one and
        both ages are served, so assert the ordering rather than trusting it."""
        for name in NAMES:
            with self.subTest(theme=name):
                pages = {p["id"]: p for p in documents(name)["kb.json"]["pages"]}
                fresh = [p for p in pages.values()
                         if (p.get("authoring") or {}).get("kind") == "current_and_contradicting"]
                self.assertTrue(fresh, f"{name} has no superseding page")
                for page in fresh:
                    targets = page["authoring"]["contradicts"]
                    self.assertTrue(targets)
                    for target in targets:
                        self.assertIn(target, pages)
                        stale = pages[target]
                        self.assertEqual(
                            (stale.get("authoring") or {}).get("kind"), "stale")
                        self.assertLess(stale["last_edited_at"], page["last_edited_at"])
                        # And the supersession is stated in prose, which is how
                        # a real wiki records one - not in a field the tools
                        # would have to serve.
                        self.assertIn("supersedes", page["body"].casefold())

    def test_the_duplicate_pair_exists_and_nobody_linked_it(self):
        for name in NAMES:
            with self.subTest(theme=name):
                issues = {i["id"]: i for i in documents(name)["issues.json"]["issues"]}
                dupes = [i for i in issues.values()
                         if (i.get("authoring") or {}).get("kind") == "duplicate"]
                self.assertEqual(len(dupes), 1, f"{name} has {len(dupes)} duplicates")
                duplicate = dupes[0]
                original = issues[duplicate["authoring"]["duplicate_of"]]
                self.assertGreater(duplicate["created_at"], original["created_at"])
                for a, b in ((duplicate, original), (original, duplicate)):
                    self.assertNotIn(b["id"], [link["issue"] for link in a["links"]])

    def test_the_useful_observation_is_a_mention_with_no_reply(self):
        for name in NAMES:
            with self.subTest(theme=name):
                chat = documents(name)["chat.json"]
                found = []
                for channel in chat["channels"]:
                    replies = [m["thread_id"] for m in channel["messages"]]
                    for message in channel["messages"]:
                        if (message.get("authoring") or {}).get("kind") != "unanswered_mention":
                            continue
                        found.append(message)
                        self.assertEqual(replies.count(message["id"]), 0,
                                         f"{message['id']} was answered")
                self.assertTrue(found, f"{name} has no unanswered mention")
                self.assertTrue(any(re.search(r"@[a-z]", m["text"]) for m in found),
                                f"{name}'s unanswered mention addresses nobody")

    def test_every_timestamp_sits_before_the_world_clock(self):
        """A ticket filed in the future is the most legible tell there is, and
        kb_get's age_days goes negative on one."""
        for name in NAMES:
            for document in documents(name).values():
                for path, value in workflow._stamps(document, name):
                    self.assertLess(value, VNOW, path)

    def test_the_corpus_moves_with_a_seed_that_moves_the_clock(self):
        """The offsets are resolved against the world clock, so a seed with a
        different vtime_start gets a coherent backlog rather than a literal one
        that is suddenly years stale."""
        later = documents("revenue_daily", "2027-01-04T00:00:00Z")
        now = documents("revenue_daily")
        for name in ("issues.json", "chat.json", "kb.json"):
            self.assertNotEqual(later[name], now[name])
        shifted = later["issues.json"]["issues"][0]["created_at"]
        self.assertTrue(STAMP.match(shifted))
        self.assertTrue(shifted.startswith("2026-"), shifted)

    def test_seeded_ids_are_the_ones_the_gateway_would_generate_next(self):
        """`issues_comment` appends `<issue>#<len+1>` and `chat_post` appends
        `<channel>/<len+1:04d>`. A seeded id that does not follow the rule
        collides with the agent's first write, which is a defect that only
        shows up mid-episode."""
        for name in NAMES:
            with self.subTest(theme=name):
                docs = documents(name)
                for issue in docs["issues.json"]["issues"]:
                    self.assertEqual(
                        [c["id"] for c in issue["comments"]],
                        [f"{issue['id']}#{n}" for n in range(1, len(issue["comments"]) + 1)])
                for channel in docs["chat.json"]["channels"]:
                    self.assertEqual(
                        [m["id"] for m in channel["messages"]],
                        [f"{channel['id']}/{n:04d}"
                         for n in range(1, len(channel["messages"]) + 1)])

    def test_a_corpus_the_contract_would_refuse_fails_the_build(self):
        """The validator is the only thing standing between an authoring slip
        and a world that looks fine until an episode hits it, so prove it
        rejects rather than trusting that it would."""
        good = documents("revenue_daily")
        cases = {
            "future ticket": ("issues.json", lambda d: d["issues"][0].update(
                {"updated_at": "2026-03-04T09:00:00Z"})),
            "unknown assignee": ("issues.json", lambda d: d["issues"][0].update(
                {"assignee": "nobody"})),
            "illegal status": ("issues.json", lambda d: d["issues"][0].update(
                {"status": "Done"})),
            "misnumbered message": ("chat.json", lambda d: d["channels"][0]["messages"][0]
                                    .update({"id": "data-eng/0000"})),
            "orphan page": ("kb.json", lambda d: d["pages"][1].update(
                {"parent": "de/nowhere"})),
            "non-ascii": ("kb.json", lambda d: d["pages"][1].update(
                {"body": "an em dash \u2014 here"})),
        }
        for label, (target, break_it) in cases.items():
            with self.subTest(case=label):
                broken = json.loads(json.dumps(good))
                break_it(broken[target])
                with self.assertRaises(SystemExit):
                    workflow.validate(broken, "agent", VNOW)
        # Not vacuous: the untouched corpus passes.
        workflow.validate(good, "agent", VNOW)


# --------------------------------------------------------------------------
# the built world
# --------------------------------------------------------------------------

class WorkflowBuildTest(unittest.TestCase):
    """revenue_daily, built twice. One build, because dbt is the slow part and
    the corpus-level checks above already cover all four themes."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="workflow_"))
        seed = THEMES / "revenue_daily" / "seed.example.yaml"
        cls.w1 = wbuild.build(seed, cls.tmp / "w1")
        cls.w2 = wbuild.build(seed, cls.tmp / "w2")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_three_documents_land_in_the_world_tree(self):
        directory = self.w1 / "var" / "workflow"
        self.assertEqual(sorted(p.name for p in directory.iterdir()),
                         ["chat.json", "issues.json", "kb.json"])
        issues = json.loads((directory / "issues.json").read_text(encoding="utf-8"))
        self.assertEqual(issues["project"]["key"], "DATA")
        self.assertEqual(issues["statuses"][0], "open")
        # Dated against the scheduler's own vnow, not against the seed or a
        # wall clock: the build reads one clock and so does the gateway.
        self.assertEqual(scheduler.load(self.w1)["vnow"], VNOW)
        self.assertTrue(all(issue["created_at"] < VNOW for issue in issues["issues"]))
        # The author-side annotation IS in the file. It is the tool responses
        # it must never reach, which the round-trip class proves.
        self.assertIn("near_duplicate_trap", (directory / "issues.json").read_text(
            encoding="utf-8"))

    def test_two_builds_produce_identical_workflow_bytes(self):
        for name in ("issues.json", "chat.json", "kb.json"):
            a = (self.w1 / "var" / "workflow" / name).read_bytes()
            b = (self.w2 / "var" / "workflow" / name).read_bytes()
            self.assertEqual(hashlib.sha256(a).hexdigest(),
                             hashlib.sha256(b).hexdigest(), name)

    def test_the_fingerprint_section_exists_and_is_conditional(self):
        sections = fingerprint.semantic(self.w1)
        self.assertIn("workflow", sections)
        self.assertEqual(sections["workflow"], fingerprint.semantic(self.w2)["workflow"])
        # A world that seeds no corpus keeps the hash it had: remove the
        # directory and the section goes with it, leaving the rest untouched.
        without = shutil.copytree(self.w1, self.tmp / "no_workflow")
        shutil.rmtree(without / "var" / "workflow")
        reduced = fingerprint.semantic(without)
        self.assertNotIn("workflow", reduced)
        self.assertEqual({k: v for k, v in sections.items() if k != "workflow"},
                         reduced)

    def test_editing_a_seeded_document_moves_the_section(self):
        """Tamper-evidence: the seeded backlog is part of what makes two builds
        the same world, so a changed ticket must not pass as the same world."""
        touched = shutil.copytree(self.w1, self.tmp / "touched")
        path = touched / "var" / "workflow" / "issues.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["issues"][0]["status"] = "done"
        path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")),
                        encoding="utf-8")
        after = fingerprint.semantic(touched)
        self.assertNotEqual(after["workflow"], fingerprint.semantic(self.w1)["workflow"])
        self.assertEqual(after["warehouse"], fingerprint.semantic(self.w1)["warehouse"])


# --------------------------------------------------------------------------
# the round trip through the real gateway
# --------------------------------------------------------------------------

@unittest.skipIf(GATEWAY, GATEWAY or "")
class WorkflowRoundTripTest(unittest.TestCase):
    """A write must be readable by the next process that opens the world."""

    SESSION = (
        ("issues_list", {}),
        ("issues_get", {"issue": "DATA-149"}),
        ("issues_search", {"query": "adjustment"}),
        ("chat_channels", {}),
        ("chat_history", {"channel": "data-eng"}),
        ("chat_thread", {"message_id": "data-eng/0001"}),
        ("chat_search", {"query": "panel"}),
        ("kb_list", {}),
        ("kb_get", {"page": "de/runbooks/nightly-revenue-load"}),
        ("kb_search", {"query": "backfill"}),
        ("issues_comment", {"issue": "DATA-149",
                            "body": "The total_revenue panel has no date filter."}),
        ("issues_update", {"issue": "DATA-149", "status": "in_progress",
                           "assignee": "agent"}),
        ("chat_post", {"channel": "data-eng", "text": "@dpatel confirming the panel.",
                       "thread_id": "data-eng/0008"}),
        ("kb_edit", {"page": "de/runbooks/nightly-revenue-load",
                     "body": "# Superseded\n\nSee de/runbooks/revenue-backfill.\n"}),
        ("kb_create", {"page": "de/postmortems/2026-03-03-panel-scope",
                       "title": "Postmortem: the unfiltered panel",
                       "body": "# Draft\n", "parent": "de/postmortems"}),
    )

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="workflow_rt_"))
        cls.world = wbuild.build(THEMES / "revenue_daily" / "seed.example.yaml",
                                 cls.tmp / "world")
        cls.fingerprint = tree_digest(cls.world)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def config(self, root: Path, audit: Path, world_fp: str | None = None):
        return GatewayConfig(
            workspace=root / "workspace",
            warehouse=root / "var" / "warehouse" / "warehouse.duckdb",
            scheduler=root / "var" / "sched" / "state.json",
            dashboards=root / "var" / "bi" / "dashboards",
            observability=root / "var" / "obs" / "definitions.yaml",
            workflow_dir=root / "var" / "workflow",
            audit_dir=audit,
            profiles_dir=root / "build",
            episode_id=audit.name,
            initial_world_fp=world_fp or tree_digest(root),
        )

    def call(self, gateway, tool, **args):
        response = gateway.call(tool, args)
        self.assertTrue(response["ok"], response["error"])
        return response["result"]

    def setUp(self):
        """Each test gets its own copy: these are writes, and a shared tree
        would let one test's comment decide another's assertion."""
        self.root = shutil.copytree(self.world,
                                    self.tmp / f"case_{self.id().rsplit('.', 1)[-1]}")
        self.audit = self.tmp / f"audit_{self.id().rsplit('.', 1)[-1]}"

    def test_the_seeded_backlog_reads_back_as_a_backlog(self):
        gateway = Gateway(self.config(self.root, self.audit / "a"))
        try:
            listing = self.call(gateway, "issues_list")
            self.assertEqual(listing["statuses"],
                             manifest("revenue_daily")["workflow"]["statuses"])
            self.assertGreaterEqual(listing["total"], 8)
            # Newest-updated first, so this morning's report sorts above a
            # November ticket.
            self.assertEqual(listing["issues"][0]["id"], "DATA-149")
            stale = next(i for i in listing["issues"] if i["id"] == "DATA-121")
            self.assertEqual(stale["assignee"], "kliu")
            self.assertEqual(stale["status"], "open")

            pages = {p["id"]: p for p in self.call(gateway, "kb_list")["pages"]}
            old = pages["de/runbooks/nightly-revenue-load"]
            new = pages["de/runbooks/revenue-backfill"]
            # age_days against the world clock is what settles the wiki, so it
            # has to be served and it has to order the two pages correctly.
            self.assertGreater(old["age_days"], new["age_days"] + 90)
            self.assertIn("appends the one new partition",
                          self.call(gateway, "kb_get",
                                    page=old["id"])["body"])
            self.assertIn("idempotent",
                          self.call(gateway, "kb_get", page=new["id"])["body"])

            thread = self.call(gateway, "chat_thread", message_id="data-eng/0001")
            self.assertGreaterEqual(thread["reply_count"], 2)
            unanswered = self.call(gateway, "chat_thread", message_id="data-eng/0008")
            self.assertEqual(unanswered["reply_count"], 0)
            self.assertEqual(unanswered["root"]["mentions"], ["kliu"])
        finally:
            gateway.close()

    def test_a_write_is_readable_by_the_next_process_that_opens_the_world(self):
        writer = Gateway(self.config(self.root, self.audit / "a"))
        try:
            comment = self.call(writer, "issues_comment", issue="DATA-149",
                                body="Panel is unfiltered; February is 139823.60.")
            self.call(writer, "issues_update", issue="DATA-149", status="in_progress",
                      assignee="agent")
            posted = self.call(writer, "chat_post", channel="data-eng",
                               text="Confirmed on the panel.", thread_id="data-eng/0008")
            edited = self.call(writer, "kb_edit",
                               page="de/runbooks/nightly-revenue-load",
                               body="# Superseded\n", comment="see revenue-backfill")
        finally:
            writer.close()
        self.assertEqual(comment["comment"]["at"], VNOW)
        self.assertEqual(comment["comment"]["author"], "agent")
        self.assertEqual(posted["message"]["id"], "data-eng/0009")
        self.assertEqual(edited["version"], 7)

        reader = Gateway(self.config(self.root, self.audit / "b"))
        try:
            issue = self.call(reader, "issues_get", issue="DATA-149")
            thread = self.call(reader, "chat_thread", message_id="data-eng/0008")
            page = self.call(reader, "kb_get", page="de/runbooks/nightly-revenue-load")
        finally:
            reader.close()
        self.assertEqual(issue["status"], "in_progress")
        self.assertEqual(issue["assignee"], "agent")
        self.assertIn("139823.60", issue["comments"][-1]["body"])
        self.assertEqual(thread["reply_count"], 1)
        self.assertEqual(thread["replies"][0]["author"], "agent")
        self.assertEqual(page["version"], 7)
        self.assertEqual(page["age_days"], 0)
        self.assertEqual(page["history"][-1]["version"], 6)

    def test_author_side_annotation_never_reaches_the_agent(self):
        """Telling the agent which ticket is the decoy destroys the
        measurement (F202). The raw file carries it; no response may."""
        notes = workflow.authoring_notes(documents("revenue_daily"))
        self.assertGreaterEqual(len(notes), 20)
        gateway = Gateway(self.config(self.root, self.audit / "a"))
        try:
            seen = [json.dumps(self.call(gateway, tool, **args), sort_keys=True)
                    for tool, args in self.SESSION]
        finally:
            gateway.close()
        evidence = "\n".join(seen)
        self.assertNotIn("authoring", evidence)
        self.assertNotIn("duplicate_of", evidence)
        self.assertNotIn("RD-", evidence)
        for note in notes:
            self.assertNotIn(note, evidence)
        # Not vacuous: the corpus itself is reachable, only the annotation is not.
        self.assertIn("Supersedes", evidence)
        self.assertIn("139,823.60", evidence)

    def test_the_annotated_documents_are_out_of_the_agents_reach(self):
        gateway = Gateway(self.config(self.root, self.audit / "a"))
        try:
            for path in ("../var/workflow/issues.json",
                         "/workspace/../var/workflow/kb.json"):
                self.assertEqual(
                    gateway.call("fs_read", {"path": path})["error"]["code"],
                    "path_denied")
            self.assertEqual(
                gateway.call("docs_get", {"path": "../var/workflow/chat.json"})
                ["error"]["code"], "path_denied")
        finally:
            gateway.close()

    def test_a_write_session_replays_onto_a_clone_byte_for_byte(self):
        clone = shutil.copytree(self.root, self.tmp / "clone")
        self.assertEqual(tree_digest(self.root), tree_digest(clone))

        source = Gateway(self.config(self.root, self.audit / "a", self.fingerprint))
        try:
            for tool, args in self.SESSION:
                self.assertTrue(source.call(tool, args)["ok"], tool)
        finally:
            source.close()
        self.assertNotEqual(tree_digest(self.root), self.fingerprint)

        target = Gateway(self.config(clone, self.audit / "replay", self.fingerprint))
        try:
            result = replay(source.audit.directory, target)
        finally:
            target.close()
        self.assertEqual(result["calls"], len(self.SESSION))
        self.assertTrue(result["responses_match"], result["differences"])
        self.assertEqual(tree_digest(self.root), tree_digest(clone))
        self.assertEqual(fingerprint.semantic(self.root)["workflow"],
                         fingerprint.semantic(clone)["workflow"])


if __name__ == "__main__":
    unittest.main()
