#!/usr/bin/env python3
"""
Machine verdict for the research swarm.  "Agents generate; machines judge."

No agent opinion is consulted anywhere in this file.  Stdlib only, so it runs
day one with no pip install.

    python gates/run_gates.py                 # G0 + G3 only (offline, fast)
    python gates/run_gates.py --online        # adds G1 citations + G2 license/liveness
    python gates/run_gates.py --gate G1       # run one gate
    python gates/run_gates.py --json          # machine-readable, for the coordinator

Exit code 0 = all requested gates passed.  Non-zero = at least one FAIL.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
SCHEMAS = json.loads((ROOT / "schemas" / "schemas.json").read_text(encoding="utf-8"))

UA = {"User-Agent": "alkera-research-swarm/1.0"}
TIMEOUT = 20

# Licenses that end the conversation.  Not warnings -- rejections.
LICENSE_DENY = {"agpl-3.0", "agpl-3.0-only", "agpl-3.0-or-later", "sspl-1.0", "bsl-1.1", "elastic-2.0"}
LICENSE_ALLOW = {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "mpl-2.0", "unlicense", "cc0-1.0"}

# Coverage targets from state/problem.yaml
ARCHETYPE_MIN, ARCHETYPE_MAX = 10, 30
DISCIPLINES = {"data_engineering", "data_science", "data_analytics"}
APPS_MIN = 20
NEAR_DUP_RATIO = 0.82


# --------------------------------------------------------------------------- io

def _strip_comment(line: str) -> str:
    out, q = [], None
    for ch in line:
        if q:
            out.append(ch)
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def load_yaml(path: Path) -> dict:
    """Tiny YAML reader for the flat docs this swarm writes.

    Supports: key: value, nested maps by indent, '- ' lists (scalar or inline),
    and '>' / '|' block scalars.  Anything fancier belongs in a real parser --
    if you hit that, add pyyaml and swap this out.
    """
    text = path.read_text(encoding="utf-8")
    root: dict = {}
    stack = [(-1, root)]
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip())
        line = _strip_comment(raw).strip()
        if not line:
            i += 1
            continue

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else root

        if line.startswith("- "):
            item = line[2:].strip().strip("\"'")
            if isinstance(parent, list):
                parent.append(item)
            i += 1
            continue

        if ":" not in line:
            i += 1
            continue

        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()

        if val in (">", "|", ">-", "|-"):
            block, j = [], i + 1
            while j < len(lines):
                if lines[j].strip() and (len(lines[j]) - len(lines[j].lstrip())) <= indent:
                    break
                block.append(lines[j].strip())
                j += 1
            parent[key] = " ".join(b for b in block if b)
            i = j
            continue

        if val == "":
            nxt = None
            for j in range(i + 1, len(lines)):
                if lines[j].strip() and not lines[j].lstrip().startswith("#"):
                    nxt = lines[j]
                    break
            if nxt is not None and nxt.lstrip().startswith("- ") and (len(nxt) - len(nxt.lstrip())) > indent:
                lst: list = []
                parent[key] = lst
                stack.append((indent, lst))
            else:
                d: dict = {}
                parent[key] = d
                stack.append((indent, d))
            i += 1
            continue

        v = val.strip("\"'")
        if v == "[]":
            parent[key] = []
        elif v.lower() in ("true", "false"):
            parent[key] = v.lower() == "true"
        elif re.fullmatch(r"-?\d+", v):
            parent[key] = int(v)
        else:
            parent[key] = v
        i += 1
    return root


def artifacts(kind: str) -> list[tuple[Path, dict]]:
    glob = SCHEMAS[kind]["_file_glob"].split("/", 1)[1]
    out = []
    for p in sorted((STATE / SCHEMAS[kind]["_file_glob"].split("/")[1]).glob(glob.split("/")[-1])):
        try:
            out.append((p, load_yaml(p)))
        except Exception as e:  # a file we cannot parse is itself a G0 failure
            out.append((p, {"__parse_error__": str(e)}))
    return out


def fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


# ------------------------------------------------------------------------ gates

class Result:
    def __init__(self, gate: str, name: str):
        self.gate, self.name = gate, name
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.checked = 0

    def fail(self, msg: str): self.failures.append(msg)
    def warn(self, msg: str): self.warnings.append(msg)

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict:
        return {"gate": self.gate, "name": self.name, "passed": self.passed,
                "checked": self.checked, "failures": self.failures, "warnings": self.warnings}


def _type_ok(val, spec: str) -> bool:
    if spec.startswith("enum:"):
        return isinstance(val, str) and val in spec[5:].split("|")
    if spec == "list[str]":
        return isinstance(val, list) and all(isinstance(x, str) for x in val)
    return {"str": str, "int": int, "float": (int, float), "bool": bool,
            "list": list, "dict": dict}.get(spec, object) and isinstance(
        val, {"str": str, "int": int, "float": (int, float), "bool": bool,
              "list": list, "dict": dict}[spec])


def gate0_schema() -> Result:
    r = Result("G0", "schema")
    for kind, schema in SCHEMAS.items():
        if kind.startswith("_"):
            continue
        for path, doc in artifacts(kind):
            r.checked += 1
            rel = path.relative_to(ROOT)
            if "__parse_error__" in doc:
                r.fail(f"{rel}: unparseable -- {doc['__parse_error__']}")
                continue
            for field, spec in schema["required"].items():
                if field not in doc:
                    r.fail(f"{rel}: missing required field '{field}'")
                elif not _type_ok(doc[field], spec):
                    r.fail(f"{rel}: field '{field}' should be {spec}, got {doc[field]!r}")
            known = set(schema["required"]) | set(schema.get("optional", {}))
            for field in doc:
                if field not in known and not field.startswith("_"):
                    r.warn(f"{rel}: unknown field '{field}'")
    return r


def gate1_citations() -> Result:
    """The one that kills hallucinated sources: fetch the URL, string-match the quote."""
    r = Result("G1", "citation resolves and quote matches")
    for path, doc in artifacts("source"):
        url, quote = doc.get("url"), doc.get("quote")
        rel = path.relative_to(ROOT)
        if not url or not quote:
            continue
        r.checked += 1
        if len(quote) < 20:
            r.fail(f"{rel}: quote is {len(quote)} chars, need >= 20 to be checkable")
            continue
        status, body = fetch(url)
        if status != 200:
            r.fail(f"{rel}: {url} returned {status or 'unreachable'}")
            continue
        flat = re.sub(r"<[^>]+>", " ", body)
        flat = re.sub(r"\s+", " ", flat).lower()
        needle = re.sub(r"\s+", " ", quote).lower().strip()
        if needle in flat:
            continue
        # tolerate light editorial trimming, but nothing more
        head = " ".join(needle.split()[:8])
        if head and head in flat:
            r.warn(f"{rel}: quote only partially matches at {url} -- verify by hand")
        else:
            r.fail(f"{rel}: quote NOT FOUND at {url} -- possible hallucinated citation")
    return r


def gate2_license_liveness() -> Result:
    r = Result("G2", "license and liveness")
    token = os.environ.get("GITHUB_TOKEN")
    for path, doc in artifacts("tool"):
        repo = doc.get("repo_url", "")
        rel = path.relative_to(ROOT)
        m = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", repo)
        if not m:
            if doc.get("license_verified"):
                r.fail(f"{rel}: license_verified=true but no GitHub repo_url to verify against")
            continue
        r.checked += 1
        owner, name = m.group(1), m.group(2).removesuffix(".git")
        req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{name}", headers=dict(UA))
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                meta = json.loads(resp.read().decode())
        except Exception as e:
            r.fail(f"{rel}: GitHub API lookup failed for {owner}/{name} -- {e}")
            continue

        spdx = ((meta.get("license") or {}).get("spdx_id") or "NOASSERTION").lower()
        if spdx in LICENSE_DENY:
            r.fail(f"{rel}: {owner}/{name} is {spdx.upper()} -- rejected, cannot use")
        elif spdx not in LICENSE_ALLOW:
            r.warn(f"{rel}: {owner}/{name} license is '{spdx}' -- needs a human read")
        declared = (doc.get("license") or "").lower()
        if declared and spdx != "noassertion" and declared != spdx:
            r.fail(f"{rel}: declared license '{declared}' != actual '{spdx}'")

        if meta.get("archived"):
            r.fail(f"{rel}: {owner}/{name} is ARCHIVED upstream")
        pushed = meta.get("pushed_at", "")
        if pushed:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(pushed.replace("Z", "+00:00"))).days
            if age > 730:
                r.fail(f"{rel}: {owner}/{name} last pushed {age} days ago -- dead project")
            elif age > 365:
                r.warn(f"{rel}: {owner}/{name} last pushed {age} days ago")
    return r


def gate3_coverage() -> Result:
    r = Result("G3", "coverage and dedup")
    arch = artifacts("archetype")
    r.checked = len(arch)

    if arch:
        n = len(arch)
        if not (ARCHETYPE_MIN <= n <= ARCHETYPE_MAX):
            r.fail(f"archetype count {n} outside target {ARCHETYPE_MIN}-{ARCHETYPE_MAX}")
        seen = {d.get("discipline") for _, d in arch}
        for missing in DISCIPLINES - seen:
            r.fail(f"no archetypes for discipline '{missing}'")

        titles = [(p, (d.get("title") or "").lower()) for p, d in arch]
        for i in range(len(titles)):
            for j in range(i + 1, len(titles)):
                ratio = difflib.SequenceMatcher(None, titles[i][1], titles[j][1]).ratio()
                if ratio >= NEAR_DUP_RATIO:
                    r.fail(f"near-duplicate ({ratio:.2f}): "
                           f"{titles[i][0].name} vs {titles[j][0].name}")

        known_sources = {d.get("id") for _, d in artifacts("source")}
        for p, d in arch:
            df = d.get("derived_from") or []
            if not df:
                r.fail(f"{p.name}: derived_from is empty -- archetypes must be derived, not invented")
            for sid in df:
                if known_sources and sid not in known_sources:
                    r.fail(f"{p.name}: derived_from cites unknown source id '{sid}'")
            g = (d.get("grading") or "").lower()
            if g and not any(k in g for k in ("query", "diff", "test", "compare", "assert",
                                              "row", "hash", "count", "check", "rubric", "metric")):
                r.warn(f"{p.name}: grading is vague -- name a concrete check")
            hh = (d.get("human_hours") or "")
            if re.fullmatch(r"\s*(0|1|<\s*1|0\.\d+)\s*", hh):
                r.warn(f"{p.name}: human_hours='{hh}' -- meeting specified tens of hours")

    apps = artifacts("tool")
    targets = [d for _, d in apps if d.get("role") == "simulate_target"]
    if targets and len(targets) < APPS_MIN:
        r.warn(f"only {len(targets)} simulate_target apps, target is {APPS_MIN}-30")

    claims = artifacts("claim")
    for p, d in claims:
        if d.get("status") == "verified":
            if not d.get("evidence"):
                r.fail(f"{p.name}: status=verified with no evidence")
            if len(d.get("attacks") or []) < 2:
                r.fail(f"{p.name}: status=verified but < 2 independent attacks recorded")
    return r


def gate5_replication() -> Result:
    """Did anyone actually run the install probe?  We record, we do not execute."""
    r = Result("G5", "replication probe recorded")
    for path, doc in artifacts("tool"):
        if doc.get("replication_ease") != "easy":
            continue
        r.checked += 1
        if doc.get("probe_result") != "pass":
            r.fail(f"{path.name}: replication_ease=easy but probe_result="
                   f"{doc.get('probe_result', 'missing')!r} -- run the probe or downgrade the claim")
        if not doc.get("install_probe"):
            r.fail(f"{path.name}: no install_probe command recorded")
    return r


GATES = {"G0": gate0_schema, "G1": gate1_citations, "G2": gate2_license_liveness,
         "G3": gate3_coverage, "G5": gate5_replication}
ONLINE = {"G1", "G2"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--online", action="store_true", help="run network gates G1, G2")
    ap.add_argument("--gate", action="append", help="run only these gates, e.g. --gate G1")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    selected = args.gate or [g for g in GATES if g not in ONLINE or args.online]
    results = [GATES[g]() for g in selected if g in GATES]

    if args.json:
        print(json.dumps({"passed": all(r.passed for r in results),
                          "gates": [r.to_dict() for r in results]}, indent=2))
        return 0 if all(r.passed for r in results) else 1

    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"[{mark}] {r.gate} {r.name}  ({r.checked} checked, "
              f"{len(r.failures)} failures, {len(r.warnings)} warnings)")
        for f in r.failures:
            print(f"        FAIL  {f}")
        for w in r.warnings:
            print(f"        warn  {w}")
    skipped = [g for g in GATES if g in ONLINE and not args.online and not args.gate]
    if skipped:
        print(f"\nskipped (need --online): {', '.join(skipped)}")
    ok = all(r.passed for r in results)
    print(f"\n{'ALL GATES PASSED' if ok else 'GATES FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
