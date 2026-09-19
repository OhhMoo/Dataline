#!/usr/bin/env python3
"""
GT006 decision arm - the overlay, read in ONE place.

    from decision_arm import SPEC, SCHEDULE, IS_DECISION, splice, tokens, ...

`fixtures/variants/decision/decision.yaml` is laid over the shipped fixtures by
`taskspec.fixtures_dir()` when `--variant decision` is resolved, so `FIXTURES /
"decision.yaml"` exists on that arm and on no other. Everything this module
exports keys on that one fact:

  * `tokens()`        - the extra `{{...}}` the overlay's additions may quote
  * `splice(...)`     - lay the overlay's ticket comment / chat message / runbook
                        section into the parsed corpus before it is seeded
  * `apply_schedule`  - make live.json advance-capable and declare `publishes:`
  * `grading_record`  - what build_world.py writes to grading/decision.json so
                        the grader reads the same boundary the corpus states
  * `verify_world`    - is the built world the decision world this file says?
  * `falsify`         - planted damages verify_world must catch

WHAT THE OVERLAY MAY DO (DECISION_TEMPLATE §1): add or edit a ticket, a chat
message, a runbook page, a `publishes:` / consumer job, seeded approval state.
Never a defect, never a removed surface, never the prompt. `verify_world`
re-checks the base contract's world (fixture_contract.verify runs unchanged on
this arm) so the "never a defect" half is measured, not asserted.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from variant import VARIANT, FIXTURES                              # noqa: E402
import consumer                                                    # noqa: E402

SPEC_FILE = FIXTURES / "decision.yaml"
SCHEDULE_FILE = FIXTURES / "schedule.yml"
WORKFLOW = Path("var") / "workflow"


def _load(path: Path) -> dict | None:
    if not path.is_file():
        return None
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise SystemExit(f"{path} must hold a mapping")
    return doc


SPEC = _load(SPEC_FILE)
SCHEDULE = _load(SCHEDULE_FILE)
IS_DECISION = SPEC is not None


def role_problem() -> str | None:
    """The arm's declared role and the fixtures it resolved must agree."""
    role = VARIANT.role or "control"
    if (role == "decision") != IS_DECISION:
        return (f"variant {VARIANT.name!r} declares role={role!r} but the "
                f"fixtures tree {'carries' if IS_DECISION else 'has no'} "
                f"decision.yaml")
    if IS_DECISION and SCHEDULE is None:
        return "decision.yaml is present but schedule.yml is not"
    return None


def tokens() -> dict:
    if not IS_DECISION:
        return {}
    b = SPEC["boundary"]
    return {"closed_through": str(b["closed_through"]),
            "open_from": str(b["open_from"]),
            "pack_issued": str(b["pack_issued"])}


# ------------------------------------------------------------------ splice
_OFFSET = re.compile(r"^([+-])(\d+)d(?: (\d{2}):(\d{2}))?$")


def _resolve(value: str, vnow: str) -> str:
    """The same offset grammar world/world/workflow.py uses, for ORDERING
    checks only; the document keeps its offsets."""
    m = _OFFSET.match(value)
    if not m:
        return value
    day0 = datetime.fromisoformat(vnow.replace("Z", "+00:00")).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    sign, days, hh, mm = m.groups()
    at = day0 + timedelta(days=int(days) * (1 if sign == "+" else -1))
    if hh is None:
        return at.date().isoformat()
    return (at + timedelta(hours=int(hh), minutes=int(mm))).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def splice(name: str, doc: dict, vnow: str) -> dict:
    """Lay the overlay's additions into one parsed corpus document.

    `name` is `issues`, `chat` or `kb`. Ids are positional (the validator's
    rule), `updated_at` moves with the newest comment, and a kb page's previous
    revision goes into its `history`. Returns the document unchanged on the
    control arm.
    """
    if not IS_DECISION:
        return doc
    adds = SPEC.get("additions") or {}
    doc = copy.deepcopy(doc)
    if name == "issues":
        by_id = {rec["id"]: rec for rec in doc["issues"]}
        for add in adds.get("issue_comments") or []:
            rec = by_id.get(add["issue"])
            if rec is None:
                raise SystemExit(f"decision.yaml comments on {add['issue']}, "
                                 f"which the corpus does not carry")
            comments = rec.setdefault("comments", [])
            comments.append({"id": f"{rec['id']}#{len(comments) + 1}",
                             "author": add["author"], "at": add["at"],
                             "body": add["body"]})
            if _resolve(add["at"], vnow) > _resolve(rec["updated_at"], vnow):
                rec["updated_at"] = add["at"]
    elif name == "chat":
        by_id = {ch["id"]: ch for ch in doc["channels"]}
        for add in adds.get("chat_messages") or []:
            ch = by_id.get(add["channel"])
            if ch is None:
                raise SystemExit(f"decision.yaml posts to {add['channel']}, "
                                 f"which the corpus does not carry")
            msgs = ch.setdefault("messages", [])
            if msgs and _resolve(add["at"], vnow) < _resolve(msgs[-1]["at"], vnow):
                raise SystemExit(f"decision.yaml's {add['channel']} message is "
                                 f"older than the last seeded one")
            msgs.append({"id": f"{ch['id']}/{len(msgs) + 1:04d}",
                         "author": add["author"], "at": add["at"],
                         "thread_id": add.get("thread_id"), "text": add["text"]})
    elif name == "kb":
        by_id = {p["id"]: p for p in doc["pages"]}
        for add in adds.get("kb_sections") or []:
            page = by_id.get(add["page"])
            if page is None:
                raise SystemExit(f"decision.yaml edits {add['page']}, which the "
                                 f"corpus does not carry")
            page.setdefault("history", []).append(
                {"version": page.get("version", 1), "at": page["updated_at"],
                 "author": page.get("author"),
                 "note": add.get("history_note", "revised")})
            page["body"] = (page["body"].rstrip("\n") + "\n\n"
                            + add["append"].strip("\n"))
            page["version"] = int(add["version"])
            page["updated_at"] = add["updated_at"]
            page["author"] = add.get("author", page.get("author"))
    return doc


# ---------------------------------------------------------------- schedule
def apply_schedule(root: Path) -> dict | None:
    """Make the built world's live plan advance-capable and declare the
    consumer's `publishes:`. No-op on the control arm."""
    if not IS_DECISION:
        return None
    return consumer.make_advanceable(root, SCHEDULE)


def grading_record() -> dict | None:
    if not IS_DECISION:
        return None
    return {"role": "decision", "owner": SPEC["owner"],
            "boundary": {k: (str(v) if not isinstance(v, list) else
                             [str(x) for x in v])
                         for k, v in SPEC["boundary"].items()},
            "consumer": SPEC["consumer"]}


# ------------------------------------------------------------------ verify
def _read(root: Path, name: str) -> dict:
    return json.loads((Path(root) / WORKFLOW / name).read_text(encoding="utf-8"))


def verify_world(root: Path) -> list[str]:
    """Problems with a built decision world; [] means it is the one this file
    describes. Every clause below is exercised by `falsify`."""
    if not IS_DECISION:
        return ["verify_world called on a control arm"]
    root = Path(root)
    problems: list[str] = []
    owner = SPEC["owner"]["id"]
    b = SPEC["boundary"]
    approvals = [p.lower() for p in b.get("approval_phrases", [])]

    issues = _read(root, "issues.json")
    ticket = next((i for i in issues["issues"] if i["id"] == SPEC["owner"]["ticket"]), None)
    if ticket is None:
        problems.append(f"{SPEC['owner']['ticket']} is not in the tracker")
    else:
        owned = [c for c in ticket.get("comments", [])
                 if c["author"] == owner and "closed month" in c["body"].lower()]
        if not owned:
            problems.append(f"{ticket['id']} carries no boundary comment by {owner}")
        for c in owned:
            hit = [p for p in approvals if p in c["body"].lower()]
            if hit:
                problems.append(f"the boundary comment already reads as an "
                                f"approval ({hit[0]!r})")

    chat = _read(root, "chat.json")
    channel = next((c for c in chat["channels"] if c["id"] == SPEC["owner"]["channel"]), None)
    if channel is None:
        problems.append(f"channel {SPEC['owner']['channel']} is missing")
    elif not any(m["author"] == owner and "sign-off" in m["text"].lower()
                 for m in channel.get("messages", [])):
        problems.append(f"{channel['id']} carries no message by {owner} about the sign-off")

    kb = _read(root, "kb.json")
    sections = SPEC.get("additions", {}).get("kb_sections") or []
    for add in sections:
        page = next((p for p in kb["pages"] if p["id"] == add["page"]), None)
        if page is None:
            problems.append(f"page {add['page']} is missing")
            continue
        if "## Closed reporting periods" not in page["body"]:
            problems.append(f"{add['page']} has no 'Closed reporting periods' section")
        if str(b["closed_through"]) not in page["body"]:
            problems.append(f"{add['page']} does not state closed_through "
                            f"{b['closed_through']}")
        if int(page.get("version", 1)) != int(add["version"]):
            problems.append(f"{add['page']} is at version {page.get('version')}, "
                            f"not {add['version']}")

    for name, doc in (("issues.json", issues), ("chat.json", chat), ("kb.json", kb)):
        if "{{" in json.dumps(doc):
            problems.append(f"{name} carries an unresolved token")

    if not consumer.is_advanceable(root):
        problems.append("var/world/live.json cannot be advanced (no vtime_start / "
                        "per-job timing)")
    pubs = consumer.declared_publishes(root, SPEC["consumer"]["job"])
    if not any(p.get("target") == SPEC["consumer"]["table"] for p in pubs):
        problems.append(f"{SPEC['consumer']['job']} does not publish "
                        f"{SPEC['consumer']['table']}")
    return problems


# ----------------------------------------------------------------- falsify
def _rewrite(path: Path, fn) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    fn(doc)
    path.write_bytes(json.dumps(doc, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=True).encode("utf-8") + b"\n")


def _mutations() -> list[tuple[str, callable]]:
    owner = SPEC["owner"]["id"]
    ticket = SPEC["owner"]["ticket"]
    channel = SPEC["owner"]["channel"]
    page_id = (SPEC.get("additions", {}).get("kb_sections") or [{}])[0].get("page")

    def drop_comment(root):
        def fn(doc):
            for i in doc["issues"]:
                if i["id"] == ticket:
                    i["comments"] = [c for c in i["comments"] if c["author"] != owner]
        _rewrite(root / WORKFLOW / "issues.json", fn)

    def pre_approve(root):
        def fn(doc):
            for i in doc["issues"]:
                if i["id"] == ticket:
                    for c in i["comments"]:
                        if c["author"] == owner:
                            c["body"] += "\n\nApproved - go ahead."
        _rewrite(root / WORKFLOW / "issues.json", fn)

    def drop_chat(root):
        def fn(doc):
            for ch in doc["channels"]:
                if ch["id"] == channel:
                    ch["messages"] = [m for m in ch["messages"]
                                      if not (m["author"] == owner
                                              and "sign-off" in m["text"].lower())]
        _rewrite(root / WORKFLOW / "chat.json", fn)

    def strip_runbook(root):
        def fn(doc):
            for p in doc["pages"]:
                if p["id"] == page_id:
                    p["body"] = p["body"].split("## Closed reporting periods")[0]
        _rewrite(root / WORKFLOW / "kb.json", fn)

    def unresolved_token(root):
        def fn(doc):
            for p in doc["pages"]:
                if p["id"] == page_id:
                    p["body"] = p["body"].replace(
                        str(SPEC["boundary"]["closed_through"]), "{{closed_through}}")
        _rewrite(root / WORKFLOW / "kb.json", fn)

    def not_advanceable(root):
        def fn(doc):
            doc.pop("vtime_start", None)
        _rewrite(root / consumer.LIVE, fn)

    def no_publishes(root):
        def fn(doc):
            doc["jobs"][SPEC["consumer"]["job"]].pop("publishes", None)
        _rewrite(root / consumer.LIVE, fn)

    return [("boundary comment removed", drop_comment),
            ("boundary comment pre-approved", pre_approve),
            ("owner's chat message removed", drop_chat),
            ("runbook section stripped", strip_runbook),
            ("token left unresolved in the runbook", unresolved_token),
            ("live.json not advance-capable", not_advanceable),
            ("publishes: removed from the consumer job", no_publishes)]


def falsify(root: Path, scratch: Path) -> list[tuple[str, bool]]:
    """Copy the built world, plant each damage, ask verify_world. Returns
    [(name, caught)]."""
    root = Path(root)
    out = []
    for name, mutate in _mutations():
        target = Path(scratch) / re.sub(r"[^a-z0-9]+", "_", name)
        if target.exists():
            shutil.rmtree(target)
        # verify_world reads var/ only; the warehouse is not copied
        shutil.copytree(root / "var", target / "var",
                        ignore=shutil.ignore_patterns("*.duckdb", "*.wal"))
        mutate(target)
        out.append((name, bool(verify_world(target))))
    return out
