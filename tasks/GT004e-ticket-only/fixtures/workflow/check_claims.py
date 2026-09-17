#!/usr/bin/env python3
"""
check_claims.py - the gate that stops the seeded narrative drifting into fiction.

WHY THIS FILE EXISTS. Twice now a builder in this repo has authored workflow
prose that quotes figures - row counts, totals, dates, window lengths - which
nothing downstream ever re-checks. Structural validation still passes, because a
ticket body is a string and every string is a valid string. So the numbers rot
silently: WORLD moves a seed, the world rebuilds, every test goes green, and the
tracker now contains a dozen confidently-worded sentences about a world that no
longer exists. A task whose human context is fiction does not measure
context-seeking; it measures tolerance for noise.

So: every factual assertion the seed makes about world state is declared in
claims.yaml against the CONTRACT key or the SQL that substantiates it, and this
script refuses to pass unless each one still holds. It is paranoid in both
directions - it fails if the seed quotes a number the world does not support,
AND it fails if the seed stops quoting a number claims.yaml still declares,
because a claim that quietly vanished from the prose is a claim nobody is
checking any more.

THE DELIBERATELY-WRONG CASES ARE ASSERTED, NOT EXEMPTED. The stale wiki
definition and the mistaken chat message are the point of the task. A checker
that skipped them would let the trap decay: if a world change ever made the
stale definition accidentally correct, or the misread window accidentally the
real one, the task would lose a gate and nothing would say so. Each such claim
carries verdict: deliberately_wrong, the CONTRACT key or query it must NOT
equal, and a reason - and this script asserts the inequality.

PHASES
  0 load      claims.yaml, the four seed documents, CONTRACT
  1 structure every token used is declared; every token declared is used
  2 resolve   no unresolved {{TOKEN}} survives in any seed document
  3 contract  each contract-backed token's rendered value equals - or, when
              deliberately wrong, differs from - what its CONTRACT key yields
  4 presence  each rendered value and each declared quote occurs verbatim in the
              surface claims.yaml says carries it
  5 world     query-backed tokens and claims re-measured against a built world
  6 integrity LF, ASCII, and the corpus passes the same world/workflow.py
              validator the episode build will run
  6b shape    the structural assertions the design rests on: the stale page
              still pre-dates the change it misses, the unanswered question
              still has no replies, the closed tickets are still closed
  7 false     every known_false_causes family is anchored in a surface that
              still exists, and NO family fires on the true cause or on any
              phrasing a correct answer would use

EXIT CODES
  0  every claim holds
  1  drift: a claim no longer matches the world it describes
  2  unresolved: placeholders survive, CONTRACT is absent, or a phase could not
     run (a pending check is never reported as a pass)

    python check_claims.py
    python check_claims.py --world ../build/world.duckdb
    python check_claims.py --world /path/to/world/root      # var/warehouse/...
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent
TASK = FIXTURES.parent
REPO = TASK.parent.parent
TOKEN_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
SEEDS = ("users.yaml", "issues.yaml", "chat.yaml", "kb.yaml")
WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
         7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
         12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen",
         16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen",
         20: "twenty", 21: "twenty-one", 30: "thirty"}


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


def load_contract():
    """CONTRACT from fixtures/fixture_contract.py, or None. Absence is PENDING,
    never PASS: a run that could not substantiate anything must not look like a
    run in which everything held."""
    path = FIXTURES / "fixture_contract.py"
    if not path.is_file():
        return None
    # It imports make_fixture_world by bare name, so fixtures/ has to be
    # importable before it is executed.
    if str(FIXTURES) not in sys.path:
        sys.path.insert(0, str(FIXTURES))
    spec = importlib.util.spec_from_file_location("gt004_fixture_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, "CONTRACT", None)


def dig(tree, dotted: str):
    """CONTRACT['defect_1']['window_start'] addressed as 'defect_1.window_start'."""
    node = tree
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, (list, tuple)) and part.isdigit():
            node = node[int(part)]
        else:
            raise KeyError(dotted)
    return node


def render(value, fmt: str | None):
    """A world value -> the exact string the prose is allowed to carry.

    Formats are explicit rather than inferred. '2026-02-09' and a date object
    must not both silently satisfy a claim, and 15649 must not satisfy a claim
    whose prose reads '15,649' - the whole purpose here is that the string in
    the ticket is the string the world produces.
    """
    if fmt in (None, "str"):
        return str(value)
    if fmt == "int":
        return str(int(value))
    if fmt == "comma":
        return f"{int(value):,}"
    if fmt == "money":
        return f"{Decimal(str(value)):,.2f}"
    if fmt == "decimal2":
        return f"{Decimal(str(value)):.2f}"
    if fmt == "date":
        return str(value)[:10]
    if fmt == "hhmm":
        return str(value)[11:16]
    if fmt == "pct":
        return f"{float(value) * 100:g}"
    if fmt == "words_days":
        return f"{WORDS[int(value)]} days"
    if fmt == "key_tuple":
        return "(" + ", ".join(value) + ")"
    if fmt == "list_items":
        return sorted(str(v) for v in value)
    raise ValueError(f"unknown format {fmt!r}")


def norm(text: str) -> str:
    """Collapse whitespace before matching.

    Seed bodies are YAML literal blocks, so a sentence that wraps carries a real
    newline and two spaces of indent in the parsed value. Matching on normalised
    whitespace lets claims.yaml quote sentences the way a person would read
    them, instead of encoding this file's line breaks into the manifest - which
    would make every reflow of the prose a false DRIFT.
    """
    return re.sub(r"\s+", " ", text)


def occurrences(rendered, haystack: str) -> bool:
    if isinstance(rendered, list):
        return all(norm(item) in haystack for item in rendered)
    return norm(rendered) in haystack


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
            return None, f"no world.duckdb or var/warehouse/warehouse.duckdb under {target}"
    if not path.is_file():
        return None, f"no world database at {target}"
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + path.as_posix().replace("'", "''") + "' AS wh (READ_ONLY)")
    return con, None


def _structural(rules, docs, contract, report) -> None:
    """Shape assertions with no figure and no quote to hang them on.

    Each of these is one careless edit away from silently removing a gate: a
    metric page re-dated forward stops being stale, a seeded reply under the
    unanswered question turns the join into a lookup, a reopened DATA-4402 puts
    the second defect in the default issue list. Nothing else in this repo would
    notice any of them.
    """
    pages = {p["id"]: p for p in docs["kb.json"]["pages"]}
    issues = {i["id"]: i for i in docs["issues.json"]["issues"]}
    channels = {c["id"]: c for c in docs["chat.json"]["channels"]}
    for rule in rules:
        rid, kind = rule.get("id"), rule.get("kind")
        if not rule.get("why"):
            report.fail(f"[structural] {rid} records no reason for existing")
        if kind in ("kb_page_edited_before", "kb_page_edited_after"):
            page = pages.get(rule["page"])
            if page is None:
                report.fail(f"[structural] {rid}: no page {rule['page']!r}")
                continue
            if contract is None:
                report.unresolved(f"[structural] {rid}: no CONTRACT to compare against")
                continue
            try:
                pivot = str(dig(contract, rule["contract_key"]))[:10]
            except KeyError:
                report.fail(f"[structural] {rid}: CONTRACT has no {rule['contract_key']!r}")
                continue
            edited = str(page["last_edited_at"])[:10]
            before = edited < pivot
            if (kind == "kb_page_edited_before") != before:
                report.fail(f"[structural] {rid}: {rule['page']} was last edited "
                            f"{edited}, which is on the wrong side of {pivot}. "
                            f"{rule.get('why', '').strip()}")
            else:
                report.passed(rid)
        elif kind == "chat_message_has_no_replies":
            channel = channels.get(rule["channel"])
            if channel is None:
                report.fail(f"[structural] {rid}: no channel {rule['channel']!r}")
                continue
            replies = [m for m in channel["messages"]
                       if m.get("thread_id") == rule["message"]]
            if replies:
                report.fail(f"[structural] {rid}: {rule['message']} now has "
                            f"{len(replies)} repl(y/ies). {rule.get('why', '').strip()}")
            else:
                report.passed(rid)
        elif kind == "issue_status_is":
            issue = issues.get(rule["issue"])
            if issue is None:
                report.fail(f"[structural] {rid}: no issue {rule['issue']!r}")
            elif issue["status"] != rule["status"]:
                report.fail(f"[structural] {rid}: {rule['issue']} is "
                            f"{issue['status']!r}, not {rule['status']!r}. "
                            f"{rule.get('why', '').strip()}")
            else:
                report.passed(rid)
        else:
            report.fail(f"[structural] {rid}: unknown kind {kind!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world", help="built world .duckdb, or a world root")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = Report()
    manifest = yaml.safe_load((HERE / "claims.yaml").read_text(encoding="utf-8"))
    tokens = {entry["token"]: entry for entry in manifest.get("tokens", [])}
    claims = manifest.get("claims", [])
    raw_text = {name: (HERE / name).read_text(encoding="utf-8") for name in SEEDS}
    text = {name: norm(body) for name, body in raw_text.items()}
    surface_file = manifest["surfaces"]

    # ---- phase 1: structure -------------------------------------------------
    used: dict[str, set[str]] = {}
    for name, body in raw_text.items():
        for token in TOKEN_RE.findall(body):
            used.setdefault(token, set()).add(name)
    for token in sorted(used):
        if token not in tokens:
            report.fail(f"[structure] {{{{{token}}}}} appears in {sorted(used[token])} "
                        "and is not declared in claims.yaml")
    for token, entry in sorted(tokens.items()):
        if entry.get("rendered") in (None, ""):
            report.unresolved(f"[structure] {token} has no rendered value")
        if not entry.get("source"):
            report.fail(f"[structure] {token} declares no source")
        if entry.get("verdict") == "deliberately_wrong" and not entry.get("reason"):
            report.fail(f"[structure] {token} is deliberately_wrong with no reason")

    # ---- phase 2: resolution ------------------------------------------------
    for token in sorted(used):
        report.unresolved(f"[resolve] {{{{{token}}}}} is still a placeholder in "
                          f"{sorted(used[token])}")

    contract = load_contract()
    if contract is None:
        report.unresolved("[resolve] fixtures/fixture_contract.py does not exist; "
                          "no contract-backed claim could be substantiated")

    # ---- phase 3: contract --------------------------------------------------
    for token, entry in sorted(tokens.items()):
        source = entry.get("source") or {}
        if source.get("kind") != "contract" or contract is None:
            continue
        rendered = entry.get("rendered")
        if rendered in (None, ""):
            continue
        wrong = entry.get("verdict") == "deliberately_wrong"
        key = source.get("must_differ_from") if wrong else source.get("key")
        if not key:
            report.fail(f"[contract] {token} names no CONTRACT key")
            continue
        try:
            expected = render(dig(contract, key), entry.get("format"))
        except KeyError:
            report.fail(f"[contract] {token}: CONTRACT has no key {key!r}")
            continue
        if wrong and rendered == expected:
            report.fail(f"[contract] {token} must be WRONG but now equals "
                        f"CONTRACT[{key}] = {expected!r}. The trap has decayed: "
                        f"{entry.get('reason')}")
        elif wrong:
            report.passed(token)
        elif rendered != expected:
            report.fail(f"[contract] {token} reads {rendered!r} in the seed but "
                        f"CONTRACT[{key}] renders {expected!r}")
        else:
            report.passed(token)

    # ---- phase 4: presence --------------------------------------------------
    for token, entry in sorted(tokens.items()):
        rendered = entry.get("rendered")
        if rendered in (None, ""):
            continue
        for surface in entry.get("used_in", []):
            name = surface_file[surface]
            if not occurrences(rendered, text[name]):
                report.fail(f"[presence] {token} renders {rendered!r}, which claims.yaml "
                            f"says {surface} carries, and {name} does not contain it")
            else:
                report.passed(f"{token}@{surface}")
    for claim in claims:
        name = surface_file[claim["surface"]]
        quote = claim.get("quote")
        if quote and norm(quote) not in text[name]:
            report.fail(f"[presence] claim {claim['id']} quotes {quote!r} from "
                        f"{claim['surface']}, which no longer contains it")
        else:
            report.passed(claim["id"])
        if claim.get("verdict") == "deliberately_wrong" and not claim.get("reason"):
            report.fail(f"[presence] claim {claim['id']} is deliberately_wrong "
                        "with no reason")

    # ---- phase 5: the world itself -----------------------------------------
    measurable = [("token", t["token"], t.get("source") or {}, t) for t in tokens.values()
                  if (t.get("source") or {}).get("kind") == "query"]
    measurable += [("claim", c["id"], c.get("substantiation") or {}, c) for c in claims
                   if (c.get("substantiation") or {}).get("kind") == "query"]
    if args.world:
        con, problem = open_world(args.world)
        if con is None:
            report.fail(f"[world] {problem}")
        else:
            for _kind, label, source, item in measurable:
                want = item.get("rendered", item.get("expect"))
                pair = [(source.get("query"), "eq"),
                        (source.get("must_differ_query"), "ne")]
                if not any(q for q, _ in pair):
                    report.fail(f"[world] {label} declares kind: query with no query")
                    continue
                if want is None:
                    report.unresolved(f"[world] {label} has no expected value")
                    continue
                for query, mode in pair:
                    if not query:
                        continue
                    try:
                        got = render(con.execute(query).fetchone()[0], item.get("format"))
                    except Exception as exc:  # noqa: BLE001 - the message is the finding
                        report.fail(f"[world] {label}: query failed: {exc}")
                        continue
                    if mode == "eq" and got != want:
                        report.fail(f"[world] {label}: the world returns {got!r}, "
                                    f"the seed says {want!r}")
                    elif mode == "ne" and got == want:
                        report.fail(f"[world] {label}: the value it must DIFFER from is "
                                    f"now {got!r}, equal to the seed. The trap has "
                                    f"decayed: {item.get('reason')}")
                    else:
                        report.passed(f"{label}:{mode}")
            con.close()
    elif measurable:
        report.unresolved(f"[world] {len(measurable)} query-substantiated claim(s) not "
                          "re-measured; pass --world to check them")

    # ---- phase 6: integrity -------------------------------------------------
    for name in SEEDS:
        raw = (HERE / name).read_bytes()
        if b"\r\n" in raw:
            report.fail(f"[integrity] {name} has CRLF line endings; write newline=''")
        elif not raw.decode("utf-8").isascii():
            report.fail(f"[integrity] {name} carries non-ASCII text, which the gateway "
                        "rewrite would turn into different bytes")
        else:
            report.passed(f"bytes:{name}")

    sys.path.insert(0, str(REPO / "world"))
    try:
        from world import workflow as wf
    except Exception as exc:  # noqa: BLE001
        report.unresolved(f"[integrity] cannot import world.workflow ({exc}); the corpus "
                          "was not validated against the real seeder")
    else:
        vnow = manifest.get("vnow")
        key = manifest.get("vnow_contract_key")
        if contract is not None and key:
            try:
                vnow = dig(contract, key)
            except KeyError:
                report.fail(f"[integrity] CONTRACT has no world-clock key {key!r}")
        if not vnow:
            report.unresolved("[integrity] no world clock; the corpus was not checked "
                              "for timestamps at or after vnow")
        else:
            if manifest.get("vnow") not in (None, vnow):
                report.fail(f"[integrity] claims.yaml pins vnow {manifest['vnow']!r} but "
                            f"the contract says {vnow!r}")
            try:
                wf.documents(FIXTURES, manifest["manifest_workflow"], vnow)
            except SystemExit as exc:
                report.fail(f"[integrity] world/workflow.py rejects this corpus: {exc}")
            except Exception as exc:  # noqa: BLE001
                report.fail(f"[integrity] corpus assembly raised: {exc!r}")
            else:
                report.passed("workflow.documents")
                docs = wf.documents(FIXTURES, manifest["manifest_workflow"], vnow)
                _structural(manifest.get("structural") or [], docs, contract, report)


    # ---- phase 7: the false-cause families ---------------------------------
    # GRADER scores `known_false_causes` per atom, so a family that fires on a
    # CORRECT answer penalises exactly the behaviour the task exists to produce.
    # This phase is the negative test: every pattern is run against the true
    # cause as this seed states it and against a hostile list of things a right
    # answer says, and any match is a DRIFT finding rather than a warning.
    fc_path = HERE / "false_causes.yaml"
    if not fc_path.is_file():
        report.unresolved("[false-cause] false_causes.yaml is missing; GRADER's "
                          "known_false_causes has nothing to load")
    else:
        fc = yaml.safe_load(fc_path.read_text(encoding="utf-8"))
        truth = fc.get("true_cause") or {}
        safe = [truth.get("statement", "")] + list(truth.get("acceptable_phrasings") or [])
        safe = [norm(t) for t in safe if t]
        if not safe:
            report.fail("[false-cause] no true_cause to test families against")
        seen_ids = set()
        for family in fc.get("families") or []:
            fid = family.get("id")
            for field in ("id", "label", "invited_by", "why_wrong", "patterns"):
                if not family.get(field):
                    report.fail(f"[false-cause] family {fid!r} has no {field}")
            if fid in seen_ids:
                report.fail(f"[false-cause] duplicate family id {fid!r}")
            seen_ids.add(fid)
            for ref in family.get("invited_by") or []:
                surface, _, target = str(ref).partition(":")
                if surface in surface_file and target:
                    if norm(target) not in text[surface_file[surface]]:
                        report.fail(f"[false-cause] {fid} says it is invited by "
                                    f"{ref}, which no longer exists in the seed")
                    else:
                        report.passed(f"{fid}:{ref}")
            for index, pattern in enumerate(family.get("patterns") or []):
                try:
                    alls = [re.compile(r, re.I) for r in pattern.get("all_of") or []]
                    nones = [re.compile(r, re.I) for r in pattern.get("none_of") or []]
                except re.error as exc:
                    report.fail(f"[false-cause] {fid} pattern {index}: bad regex: {exc}")
                    continue
                if not alls:
                    report.fail(f"[false-cause] {fid} pattern {index} has no all_of; "
                                "a family with no required term matches everything")
                    continue
                for phrase in safe:
                    if all(r.search(phrase) for r in alls) and                             not any(r.search(phrase) for r in nones):
                        report.fail(
                            f"[false-cause] {fid} pattern {index} FIRES ON A CORRECT "
                            f"answer: {phrase!r}. A false-cause penalty that hits a "
                            "right answer is worse than one that misses a wrong one; "
                            "narrow the pattern or drop the family.")
                        break
                else:
                    report.passed(f"{fid}:pattern{index}")
        for entry in fc.get("rejected") or []:
            if not entry.get("reason"):
                report.fail(f"[false-cause] rejected entry {entry.get('id')!r} "
                            "records no reason")

    code = report.exit_code()
    if args.json:
        print(json.dumps({"ok": report.ok, "drift": report.drift,
                          "pending": report.pending, "exit": code}, indent=2))
    else:
        print(f"GT004 workflow claims: {report.ok} checks held")
        for line in report.drift:
            print(f"  DRIFT     {line}")
        for line in report.pending:
            print(f"  PENDING   {line}")
        print({0: "PASS", 1: "FAIL (drift)", 2: "PENDING (unresolved)"}[code])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
