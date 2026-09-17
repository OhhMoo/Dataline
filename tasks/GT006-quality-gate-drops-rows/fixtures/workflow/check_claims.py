#!/usr/bin/env python3
"""
check_claims.py - the gate that stops GT006's seeded narrative drifting.

ONE FILE, FOUR CONTEXTS. GT006 ships four `[variants]` of one context and this
file is shared by all of them: the machinery below reads whichever claims.yaml
the resolved variant staged, so a claim that exists only in the stacked corpus
is checked only there and nothing has to be kept in step by hand. `default` and
`trap-ticket` read the package's own fixtures/workflow/; `stacked-neutral` and
`stacked-ticket` read fixtures/variants/<arm>/workflow/ copied over it.

WHY THIS FILE EXISTS, and why it is narrower than GT004's. GT006's corpus was
written under a rule that makes most of GT004's machinery unnecessary: it quotes
no row count, amount, rate or percentage, and every date it quotes is a
staging token that tests/stage_surfaces.py resolves from CONTRACT and refuses
to ship unresolved. There is nothing in the prose for a reseed to falsify.

What there IS, since the interference build, is DELIBERATE FALSEHOOD. DATA-2118
and de/changelog/terminal-ref-review assert, as settled fact, that the rows with
an empty `terminal_ref` are the processor's test harness. They are not. That
assertion is the single content delta between this build and the one that
measured Pass@1 = 1.0, and it is the whole experiment.

A deliberate falsehood that nothing re-checks decays in two directions and both
of them destroy the measurement:

  * it can become accidentally TRUE. A generator change that gave the harness a
    blank ref, or stopped emitting legacy-acquirer card-not-present rows, would
    leave the corpus reading the same and the trap gone, and nothing else in
    this repo would notice.
  * its REFUTATION can become unreachable. If the empty-ref rows stopped
    carrying real merchants, or stopped pre-dating the harness, or the wiki's
    definition-of-record page lost its sentence, the trap would stop being FAIR:
    the world would be asserting something an agent could not check, and the
    unit would measure deference to an unfalsifiable world instead of judgement
    against a falsifiable one. That is the outcome that would make the whole
    experiment worthless, so every refutation path is asserted here on every
    run rather than assumed.

So each deliberately-wrong claim carries `verdict: deliberately_wrong`, a
`reason`, and the queries that must still refute it - GT004's rule - and this
script asserts the refutations hold.

PHASES
  0 load      claims.yaml, the four seed documents, CONTRACT, stage_surfaces
  1 structure every claim names a surface that exists; every deliberately_wrong
              claim records a reason; every one is substantiated or refuted
  2 presence  each quote occurs verbatim in the surface claims.yaml says carries
              it (whitespace-normalised, so reflowing the prose is not drift)
  3 world     every refutation query and every substantiation query re-measured
              against a built world
  4 assemble  world/workflow.py - the real seeder and the real validator -
              accepts the corpus with tokens resolved from CONTRACT
  5 shape     the structural assertions the design rests on: the trap ticket is
              still closed, the false page still post-dates the true one, the
              true page still carries its refuting sentence, chat is untouched
  6 integrity LF, ASCII, and no mechanism-naming token in the corpus

EXIT CODES
  0  every claim holds
  1  drift: a claim no longer matches the world it describes, or a trap decayed
  2  unresolved: a phase could not run. A pending check is never a pass.

    python check_claims.py
    python check_claims.py --world ../build/world.duckdb
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent
TASK = FIXTURES.parent
REPO = TASK.parent.parent
SEEDS = ("users.yaml", "issues.yaml", "chat.yaml", "kb.yaml")
CONTRACT_RE = re.compile(r"<<contract:([a-z0-9_.]+)>>")


class Report:
    """Collect findings so one run names every problem, not the first."""

    def __init__(self) -> None:
        self.drift: list[str] = []
        self.pending: list[str] = []
        self.ok = 0

    def passed(self, _what: str) -> None:
        self.ok += 1

    def fail(self, what: str) -> None:
        self.drift.append(what)

    def unresolved(self, what: str) -> None:
        self.pending.append(what)

    def exit_code(self) -> int:
        return 1 if self.drift else (2 if self.pending else 0)


def norm(text: str) -> str:
    """Collapse whitespace before matching.

    Seed bodies are YAML literal blocks, so a sentence that wraps carries a real
    newline and indent in the parsed value. Matching on normalised whitespace
    lets claims.yaml quote sentences the way a person reads them instead of
    encoding this corpus's line breaks into the manifest, which would make every
    reflow of the prose a false DRIFT.
    """
    return re.sub(r"\s+", " ", text).strip()


def dig(tree, dotted: str):
    node = tree
    for part in dotted.split("."):
        node = node[part]
    return node


def load_contract():
    """CONTRACT from fixtures/fixture_contract.py, or None. Absence is PENDING,
    never PASS: a run that could substantiate nothing must not look like a run
    in which everything held."""
    path = FIXTURES / "fixture_contract.py"
    if not path.is_file():
        return None
    if str(FIXTURES) not in sys.path:
        sys.path.insert(0, str(FIXTURES))
    import importlib.util

    spec = importlib.util.spec_from_file_location("gt006_fixture_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, "CONTRACT", None)


def open_world(target: str):
    """F622: aliased ATTACH, never a bare connect() on a warehouse file."""
    import duckdb

    path = Path(target)
    if path.is_dir():
        for candidate in (path / "var" / "warehouse" / "warehouse.duckdb",
                          path / "world.duckdb"):
            if candidate.is_file():
                path = candidate
                break
        else:
            return None, f"no world database under {target}"
    if not path.is_file():
        return None, f"no world database at {target}"
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + path.as_posix().replace("'", "''") + "' AS wh (READ_ONLY)")
    return con, None


def resolved_corpus(contract):
    """The corpus as world/workflow.py assembles it, tokens resolved from
    CONTRACT by the SAME table tests/stage_surfaces.py uses. Imported rather
    than duplicated: two copies of a token table is how one of them goes stale.
    """
    sys.path.insert(0, str(TASK / "tests"))
    import stage_surfaces as ss                                    # noqa: E402

    sys.path.insert(0, str(REPO / "world"))
    from world import workflow as wf                               # noqa: E402

    scratch = ss._resolve_tokens(FIXTURES / "workflow")
    try:
        return wf.documents(scratch.parent, ss.WORKFLOW_SPEC,
                            contract["virtual_now"])
    finally:
        import shutil

        shutil.rmtree(scratch.parent, ignore_errors=True)


# ---------------------------------------------------------------- phase 3/5
def _measure(con, query: str, contract) -> str:
    query = CONTRACT_RE.sub(lambda m: str(dig(contract, m.group(1))), query)
    value = con.execute(query).fetchone()[0]
    return "" if value is None else str(value)


def _expected(entry, contract) -> tuple[str | None, str]:
    """The value a query must return: a literal `expect`, or a CONTRACT key."""
    if "expect" in entry:
        return str(entry["expect"]), "expect"
    key = entry.get("contract_key")
    if not key:
        return None, ""
    return str(dig(contract, key)), f"CONTRACT[{key}]"


def _structural(rules, docs, report) -> None:
    issues = {i["id"]: i for i in docs["issues.json"]["issues"]}
    pages = {p["id"]: p for p in docs["kb.json"]["pages"]}
    channels = {c["id"]: c for c in docs["chat.json"]["channels"]}
    for rule in rules:
        rid, kind = rule.get("id"), rule.get("kind")
        if not rule.get("why"):
            report.fail(f"[shape] {rid} records no reason for existing")
        if kind == "issue_status_is":
            issue = issues.get(rule["issue"])
            if issue is None:
                report.fail(f"[shape] {rid}: no issue {rule['issue']!r}")
            elif issue["status"] != rule["status"]:
                report.fail(f"[shape] {rid}: {rule['issue']} is "
                            f"{issue['status']!r}, not {rule['status']!r}. "
                            f"{norm(rule['why'])}")
            else:
                report.passed(rid)
        elif kind == "kb_page_edited_after":
            page, other = pages.get(rule["page"]), pages.get(rule["than"])
            if page is None or other is None:
                report.fail(f"[shape] {rid}: missing page "
                            f"{rule['page']!r} or {rule['than']!r}")
            elif not page["updated_at"] > other["updated_at"]:
                report.fail(f"[shape] {rid}: {rule['page']} was edited "
                            f"{page['updated_at']}, not after {rule['than']} "
                            f"({other['updated_at']}). {norm(rule['why'])}")
            else:
                report.passed(rid)
        elif kind == "kb_page_contains":
            page = pages.get(rule["page"])
            if page is None:
                report.fail(f"[shape] {rid}: no page {rule['page']!r}")
            elif norm(rule["text"]) not in norm(page["body"]):
                report.fail(f"[shape] {rid}: {rule['page']} no longer contains "
                            f"{norm(rule['text'])!r}. {norm(rule['why'])}")
            else:
                report.passed(rid)
        elif kind == "chat_message_has_no_replies":
            channel = channels.get(rule["channel"])
            if channel is None:
                report.fail(f"[shape] {rid}: no channel {rule['channel']!r}")
                continue
            replies = [m for m in channel["messages"]
                       if m.get("thread_id") == rule["message"]]
            if replies:
                report.fail(f"[shape] {rid}: {rule['message']} now has "
                            f"{len(replies)} repl(y/ies). {norm(rule['why'])}")
            else:
                report.passed(rid)
        elif kind == "chat_message_count_is":
            channel = channels.get(rule["channel"])
            if channel is None:
                report.fail(f"[shape] {rid}: no channel {rule['channel']!r}")
            elif len(channel["messages"]) != rule["count"]:
                report.fail(f"[shape] {rid}: {rule['channel']} now carries "
                            f"{len(channel['messages'])} messages, not "
                            f"{rule['count']}. {norm(rule['why'])}")
            else:
                report.passed(rid)
        else:
            report.fail(f"[shape] {rid}: unknown kind {kind!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world", help="a built world .duckdb, or a world root")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = Report()
    manifest = yaml.safe_load((HERE / "claims.yaml").read_text(encoding="utf-8"))
    claims = manifest.get("claims") or []
    surface_file = manifest["surfaces"]
    raw_text = {name: (HERE / name).read_text(encoding="utf-8") for name in SEEDS}
    text = {name: norm(body) for name, body in raw_text.items()}
    contract = load_contract()
    if contract is None:
        report.unresolved("[load] fixtures/fixture_contract.py does not exist; "
                          "nothing could be substantiated")

    # ---- phase 1: structure -------------------------------------------------
    seen: set[str] = set()
    for claim in claims:
        cid = claim.get("id")
        if not cid or cid in seen:
            report.fail(f"[structure] claim id {cid!r} is missing or duplicated")
        seen.add(cid)
        wrong = claim.get("verdict") == "deliberately_wrong"
        if not claim.get("reason"):
            report.fail(f"[structure] {cid} records no reason")
        if claim.get("surface") not in surface_file:
            report.fail(f"[structure] {cid} names an unknown surface "
                        f"{claim.get('surface')!r}")
        if not claim.get("quote"):
            report.fail(f"[structure] {cid} quotes nothing, so nothing anchors it")
        if wrong and not claim.get("refuted_by"):
            report.fail(f"[structure] {cid} is deliberately_wrong and declares no "
                        "refutation. A falsehood the world cannot refute is not a "
                        "fair trap; it is an unfalsifiable assertion.")
        if not wrong and not claim.get("substantiation"):
            report.fail(f"[structure] {cid} is asserted true and substantiated by "
                        "nothing")

    # ---- phase 2: presence --------------------------------------------------
    for claim in claims:
        name = surface_file.get(claim.get("surface"))
        quote = norm(claim.get("quote") or "")
        if not name or not quote:
            continue
        if quote not in text[name]:
            report.fail(f"[presence] {claim['id']} quotes {quote!r} from "
                        f"{claim['surface']}, which no longer contains it")
        else:
            report.passed(f"{claim['id']}@{claim['surface']}")

    # ---- phase 3: the world itself -----------------------------------------
    if not args.world:
        report.unresolved("query-backed claims were not re-measured; pass --world")
    elif contract is None:
        report.unresolved("[world] no CONTRACT; queries were not run")
    else:
        con, problem = open_world(args.world)
        if con is None:
            report.fail(f"[world] {problem}")
        else:
            try:
                for claim in claims:
                    cid = claim["id"]
                    wrong = claim.get("verdict") == "deliberately_wrong"
                    entries = (claim.get("refuted_by") or []) if wrong else \
                        [claim.get("substantiation") or {}]
                    for index, entry in enumerate(entries):
                        query = entry.get("query")
                        if entry.get("kind") == "contract_inequality":
                            left = float(dig(contract, entry["left"]))
                            right = float(dig(contract, entry["right"]))
                            held = left < right if entry["op"] == "<" else left > right
                            if not held:
                                report.fail(
                                    f"[world] {cid}: {entry['left']}={left} is not "
                                    f"{entry['op']} {entry['right']}={right}. "
                                    f"{norm(claim['reason'])}")
                            else:
                                report.passed(f"{cid}:{index}")
                            continue
                        if not query:
                            report.fail(f"[world] {cid} entry {index} has no query")
                            continue
                        want, where = _expected(entry, contract)
                        if want is None:
                            report.fail(f"[world] {cid} entry {index} declares "
                                        "neither expect nor contract_key")
                            continue
                        try:
                            got = _measure(con, query, contract)
                        except Exception as exc:          # noqa: BLE001
                            report.fail(f"[world] {cid} entry {index}: query "
                                        f"failed: {exc}")
                            continue
                        if got != want:
                            label = norm(str(entry.get("label", "")))
                            if wrong:
                                report.fail(
                                    f"[world] {cid} REFUTATION {index} NO LONGER "
                                    f"HOLDS ({label}): the world returns {got!r}, "
                                    f"{where} says {want!r}. Either the trap has "
                                    f"decayed into truth or its refuting evidence "
                                    f"has gone, and both destroy the measurement. "
                                    f"{norm(claim['reason'])}")
                            else:
                                report.fail(
                                    f"[world] {cid} is asserted TRUE and the world "
                                    f"returns {got!r}, {where} says {want!r} "
                                    f"({label})")
                        else:
                            report.passed(f"{cid}:{index}")
            finally:
                con.close()

    # ---- phase 4 + 5: assembly and shape ------------------------------------
    if contract is None:
        report.unresolved("[assemble] no CONTRACT; the corpus was not assembled")
    else:
        try:
            docs = resolved_corpus(contract)
        except SystemExit as exc:
            report.fail(f"[assemble] world/workflow.py rejects this corpus: {exc}")
            docs = None
        except Exception as exc:                            # noqa: BLE001
            report.unresolved(f"[assemble] corpus assembly raised: {exc!r}")
            docs = None
        if docs is not None:
            report.passed("workflow.documents")
            _structural(manifest.get("structural") or [], docs, report)
            body = json.dumps(docs)
            for token in manifest.get("forbidden_tokens") or []:
                if token in body:
                    report.fail(f"[leak] the assembled corpus contains {token!r}, "
                                "which names the mechanism the unit measures")
                else:
                    report.passed(f"leak:{token}")

    # ---- phase 6: integrity -------------------------------------------------
    for name in SEEDS + ("claims.yaml",):
        raw = (HERE / name).read_bytes()
        if b"\r\n" in raw:
            report.fail(f"[integrity] {name} has CRLF line endings; write newline=''")
        elif not raw.decode("utf-8").isascii():
            report.fail(f"[integrity] {name} carries non-ASCII text, which the "
                        "gateway rewrite would turn into different bytes")
        else:
            report.passed(f"bytes:{name}")

    code = report.exit_code()
    if args.json:
        print(json.dumps({"ok": report.ok, "drift": report.drift,
                          "pending": report.pending, "exit": code}, indent=2))
    else:
        print(f"GT006 workflow claims: {report.ok} checks held")
        for line in report.drift:
            print(f"  DRIFT     {line}")
        for line in report.pending:
            print(f"  PENDING   {line}")
        print({0: "PASS", 1: "FAIL (drift)", 2: "PENDING (unresolved)"}[code])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
