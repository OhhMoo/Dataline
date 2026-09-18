"""Layer 0 - the task validity card.

Before any rollout of a task counts, the TASK has to be shown valid: the
oracle scores max, the no-op scores nothing, a false claim scores below the
no-op, every check the scoring matrix names is one the grader emits, and every
baseline `[matrix].accepted` names is a declared validity baseline. Today that
is done by hand per task, and GT007 shipped a `[matrix.map]` naming a check
(`D2`) its grader never emitted, so its projected pass was withheld as n/a on
a task at 5/5 - silently (tasks/MATRIX_RERUN_EDITS.md section 0.3, 0.4).

Two halves, kept apart so a report can say which one it is quoting:

  static_card  reads task.toml (through taskspec, so the arm's matrix
               override is the one audited) and the grader's check table.
               No subprocess, no DuckDB, seconds.
  run_card     runs tests/run_baselines.py as a subprocess - minutes to an
               hour - and PARSES its stdout. Every suite prints a different
               table, so the parser records how much of it it understood
               (`parse_confidence`) rather than pretending.

Where the grader's check ids come from, in order of trust: `[grading].manifest`
when it declares a `checks:` table with ids (the set audit_check_set enforces
per task); otherwise a regex scan of tests/grade.py for `.add("ID", ...)`
literals, marked heuristic. A manifest can list a check the grader never emits
(0.4's H3 was exactly that), so when both exist the scan is cross-checked and a
manifest id the scan never sees is reported at `info` - it is a hint, not a
verdict, because a grader may build ids from a table.

Heuristics on `[[validity]]` rows (all documented here because a heuristic
nobody can read is a rubric):
  * a row with `variant = X` applies to arm X only; a row without applies to
    every arm; a `[[validity_<suffix>]]` table applies to arms whose name
    contains <suffix> (GT006's `validity_stacked` -> `stacked-*`).
  * oracle row:   agent == "oracle", or agent starts with "oracle" and the
                  expectation mentions max.
  * no_op row:    agent in {no_op, noop, no-op}.
  * false claim:  expectation says "< no_op" / "below no_op", or the agent
                  is named checklist* / false_claim*.
  * positive control: agent is not "oracle" and the expectation is "== max"
                  or "== oracle" with no arithmetic after it (GT009's
                  rebuilt_not_updated; GT003's oracle_appended). "== oracle
                  - 4" is a rival, not a control.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

import taskspec

SCHEMA_VERSION = 1
BLOCK, WARN, INFO = "block", "warn", "info"

# `s.add("D5a", ...)` is how every grade.py in the corpus emits a check; the
# broad fallback catches graders that spell it differently, at the cost of
# also matching any short upper-case token in a string.
_ADD_CALL = re.compile(r"""\.add\(\s*["']([A-Za-z][\w-]*)["']""")
_ID_LITERAL = re.compile(r"""["']([A-Z]{1,3}\d{1,2}[a-z]?)["']""")

_ORACLE_LIKE = re.compile(r"^oracle")
_NO_OP = {"no_op", "noop", "no-op"}
_FALSE_CLAIM_NAME = re.compile(r"^(checklist|false[_-]?claim)")
_FALSE_CLAIM_EXPECT = re.compile(r"(<\s*no_op|below\s+no_op)", re.IGNORECASE)
# "== max", "= max", "== oracle" - and nothing arithmetic after the word.
_AT_MAX = re.compile(r"={1,2}\s*(max|oracle)\b(?!\s*[-+*/]\s*\d)", re.IGNORECASE)


def _finding(severity: str, code: str, detail: str) -> dict:
    return {"severity": severity, "code": code, "detail": detail}


# ------------------------------------------------------------- grader ids
def grader_check_ids(spec) -> tuple[list[str], str, list[str]]:
    """(ids, source, scan_ids). `scan_ids` is the grade.py scan even when the
    manifest is the source, so the two can be cross-checked."""
    scan: list[str] = []
    grade_py = spec.grade_entrypoint
    if grade_py.is_file():
        text = grade_py.read_text(encoding="utf-8", errors="replace")
        scan = sorted(set(_ADD_CALL.findall(text)))
        if not scan:
            scan = sorted(set(_ID_LITERAL.findall(text)))
    manifest = spec.grade_manifest
    if manifest is not None and manifest.is_file():
        try:
            doc = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            doc = {}
        checks = doc.get("checks") if isinstance(doc, dict) else None
        if isinstance(checks, list):
            ids = sorted({str(c["id"]) for c in checks
                          if isinstance(c, dict) and c.get("id")})
            if ids:
                return ids, "manifest", scan
    if scan:
        return scan, "grade.py scan (heuristic)", scan
    return [], "unknown", scan


# ---------------------------------------------------------- validity rows
def validity_rows(raw: dict, arm: str) -> list[dict]:
    """The `[[validity*]]` rows that apply to this arm. See module doc."""
    rows: list[dict] = []
    for key, table in raw.items():
        if key != "validity" and not key.startswith("validity_"):
            continue
        if not isinstance(table, list):
            continue
        suffix = key[len("validity_"):] if key != "validity" else ""
        if suffix and suffix not in (arm or ""):
            continue
        for row in table:
            if not isinstance(row, dict) or not row.get("agent"):
                continue
            scoped = row.get("variant")
            if scoped and scoped != (arm or "default"):
                continue
            rows.append({"agent": str(row["agent"]),
                         "expect": str(row.get("expect", "")),
                         "note": str(row.get("note", "")),
                         "table": key})
    return rows


def _is_oracle(row: dict) -> bool:
    a = row["agent"]
    return a == "oracle" or (bool(_ORACLE_LIKE.match(a))
                             and "max" in row["expect"].lower())


def _is_false_claim(row: dict) -> bool:
    return bool(_FALSE_CLAIM_NAME.match(row["agent"])
                or _FALSE_CLAIM_EXPECT.search(row["expect"]))


def _is_positive_control(row: dict) -> bool:
    return row["agent"] != "oracle" and bool(_AT_MAX.search(row["expect"]))


# ------------------------------------------------------------ static card
def static_card(task_id: str, tasks_dir: Path, variant: str | None = None) -> dict:
    """Everything that can be known about a task's validity without running
    it. Raises taskspec.TaskSpecError when the package itself refuses to load -
    that is a refusal, not a finding, and the CLI exits 3 on it."""
    spec = taskspec.load(task_id, Path(tasks_dir), variant=variant)
    arm = spec.variant or ""
    findings: list[dict] = []

    ids, source, scan = grader_check_ids(spec)
    known = set(ids)
    if source == "unknown":
        findings.append(_finding(BLOCK, "grader_checks_unknown",
                                 "neither [grading].manifest nor tests/grade.py "
                                 "yields a check id set; nothing below can be "
                                 "checked against the grader"))
    elif source != "manifest":
        findings.append(_finding(INFO, "grader_checks_heuristic",
                                 "check ids read by scanning tests/grade.py for "
                                 ".add(\"ID\") literals; the manifest declares "
                                 "no `checks:` table"))
    elif scan:
        unseen = sorted(known - set(scan))
        if unseen:
            findings.append(_finding(INFO, "manifest_check_not_in_grade_py",
                                     f"manifest lists {unseen}, which no "
                                     f".add(\"ID\") literal in grade.py names; "
                                     f"a grader that builds ids from a table is "
                                     f"fine, one that never emits them is 0.4"))

    m = spec.matrix
    missing_map: dict[str, list[str]] = {}
    accepted: list[str] = []
    if m is None:
        findings.append(_finding(INFO, "no_matrix",
                                 "task declares no [matrix]; Pass@1 is the "
                                 "pass_requires_checks AND"))
    else:
        accepted = list(m.accepted)
        for axis, cids in m.map.items():
            gone = [c for c in cids if known and c not in known]
            if gone:
                missing_map[axis] = gone
                findings.append(_finding(
                    BLOCK, "map_check_missing_from_grader",
                    f"[matrix.map].{axis} names {gone}, which the grader "
                    f"({source}) never emits; the axis would read n/a forever"))

    rows = validity_rows(spec.raw, arm)
    names = {r["agent"] for r in rows}
    accepted_missing = sorted(set(accepted) - names)
    for name in accepted_missing:
        findings.append(_finding(
            BLOCK, "accepted_not_in_validity",
            f"[matrix].accepted names {name!r}, but no [[validity]] row for "
            f"arm {arm or 'default'!r} declares it; oracle validity would be "
            f"an empty set"))

    has_oracle = any(_is_oracle(r) for r in rows)
    has_no_op = any(r["agent"] in _NO_OP for r in rows)
    has_false = any(_is_false_claim(r) for r in rows)
    has_control = any(_is_positive_control(r) for r in rows)
    if not has_oracle:
        findings.append(_finding(BLOCK, "no_oracle_row",
                                 "no [[validity]] row for an oracle at max"))
    if not has_no_op:
        findings.append(_finding(BLOCK, "no_no_op_row",
                                 "no [[validity]] row for no_op"))
    if not has_false:
        findings.append(_finding(WARN, "no_false_claim_row",
                                 "no row expects a false claim below no_op "
                                 "(checklist / false_claim)"))
    if not has_control:
        findings.append(_finding(WARN, "no_positive_control_row",
                                 "no non-oracle row expected at max; one "
                                 "solution idiom is being rewarded unproven"))

    prc_missing = [c for c in spec.pass_requires_checks if known and c not in known]
    if prc_missing:
        findings.append(_finding(
            BLOCK, "pass_requires_check_missing_from_grader",
            f"[harness].pass_requires_checks names {prc_missing}, which the "
            f"grader ({source}) never emits; no episode could ever pass"))

    return {
        "schema_version": SCHEMA_VERSION,
        "task": task_id,
        "variant": arm or None,
        "static": {
            "matrix_present": m is not None,
            "pass_axis": m.pass_axis if m else None,
            "gate": m.gate if m else None,
            "grader_check_ids": ids,
            "grader_check_source": source,
            "map_checks_missing_from_grader": missing_map,
            "proxy_declared": sorted(m.proxy) if m else [],
            "accepted": accepted,
            "accepted_not_in_validity": accepted_missing,
            "validity_rows": len(rows),
            "has_oracle_row": has_oracle,
            "has_no_op_row": has_no_op,
            "has_false_claim_row": has_false,
            "has_positive_control_row": has_control,
            "pass_requires_checks_missing_from_grader": prc_missing,
            "required_surfaces_declared": bool(spec.required_surfaces),
            "dead_by_design_declared": bool(spec.dead_by_design),
            "findings": findings,
        },
        "run": None,
        "valid": not any(f["severity"] == BLOCK for f in findings),
        "invalid_because": [f["detail"] for f in findings
                            if f["severity"] == BLOCK],
    }


# ------------------------------------------------------------- the parser
# One baseline row, in any of the corpus's table shapes:
#   GT007/8/9  "  oracle       total  20.0 / 20   C0,R1-4 [...]  PASS  [== max]"
#   GT001-4    "  oracle          19.0 /19.0 = 19.0 (max)"
#   GT005      "  oracle          19.00 / 19.0  [....] REQ  (12s)"
#   GT006      "    oracle        19.00/19    pass=True  ..."
_ROW = re.compile(
    r"^\s+(?P<name>[A-Za-z][\w-]*)\s+(?:total\s+)?"
    r"(?P<total>-?\d+(?:\.\d+)?)\s*/\s*(?P<max>-?\d+(?:\.\d+)?)(?P<rest>.*)$")
_VALID = re.compile(r"^\s*TASK VALID\b")
_NOT_VALID = re.compile(r"^\s*TASK (NOT VALID|NOT (YET )?VALIDATED|BROKEN|INVALID)\b")
_BRACKET_EXPECT = re.compile(r"\[([^\[\]]*)\]\s*$")


def _passed(rest: str) -> bool | None:
    m = re.search(r"pass=(True|False)", rest)
    if m:
        return m.group(1) == "True"
    if re.search(r"\bPASS\b", rest):
        return True
    if re.search(r"\bfail\b", rest):
        return False
    return None


def _met(expect: str, total: float, mx: float, totals: dict) -> bool | None:
    """Does the printed total satisfy the expectation string? Only the shapes
    the corpus writes; anything else is None (not judged), never False."""
    e = expect.lower()
    if _AT_MAX.search(e):
        return total == mx
    m = re.search(r"(<=|<|==|=|>=|>)\s*(-?\d+(?:\.\d+)?)", e)
    if m:
        op, rhs = m.group(1), float(m.group(2))
        return {"<=": total <= rhs, "<": total < rhs, "==": total == rhs,
                "=": total == rhs, ">=": total >= rhs, ">": total > rhs}[op]
    m = re.search(r"(<=|<|==|>)\s*(no_op|oracle|checklist)\b", e)
    if m and m.group(2) in totals:
        other = totals[m.group(2)]
        return {"<=": total <= other, "<": total < other,
                "==": total == other, ">": total > other}[m.group(1)]
    if re.search(r"~\s*0\b", e):
        return abs(total) <= 1.0
    return None


def parse_baselines(stdout: str, declared: dict[str, str] | None = None) -> dict:
    """The run half of the card, from the suite's stdout. `declared` is
    agent -> expect from task.toml, used when the table prints no expectation
    of its own (GT005/GT006)."""
    declared = declared or {}
    baselines: dict[str, dict] = {}
    valid_printed = False
    for line in stdout.splitlines():
        if _VALID.match(line):
            valid_printed = True
            continue
        if _NOT_VALID.match(line):
            valid_printed = False
            continue
        m = _ROW.match(line)
        if not m or m.group("name") in baselines:
            continue
        rest = m.group("rest")
        b = _BRACKET_EXPECT.search(rest)
        if b:
            expect = b.group(1).strip() or None
        else:
            # GT005 prints flag cells and GT006 a pass= bit where GT001-4
            # print the expectation; a cell is not an expectation.
            expect = rest.strip() or None
            if expect and ("pass=" in rest or re.match(r"\s*\[", rest)
                           or re.fullmatch(r"[.\-\d\s]+", expect)):
                expect = None
        baselines[m.group("name")] = {
            "total": float(m.group("total")), "max": float(m.group("max")),
            "passed": _passed(rest),
            "expect": expect or declared.get(m.group("name")),
            "met": None}
    totals = {n: r["total"] for n, r in baselines.items()}
    for row in baselines.values():
        if row["expect"]:
            row["met"] = _met(row["expect"], row["total"], row["max"], totals)

    oracle = baselines.get("oracle")
    no_op = next((baselines[n] for n in _NO_OP if n in baselines), None)
    # Only rows whose OWN expectation says "< no_op" are judged against it:
    # GT001's checklist expects "< oracle" and the task never claimed more.
    false_claims = [r for r in baselines.values()
                    if _FALSE_CLAIM_EXPECT.search(r["expect"] or "")]
    if valid_printed and oracle and no_op:
        confidence = "full"
    elif baselines or valid_printed:
        confidence = "partial"
    else:
        confidence = "none"
    return {
        "task_valid_printed": valid_printed,
        "baselines": baselines,
        "oracle_at_max": (oracle["total"] == oracle["max"]) if oracle else None,
        "no_op_at_or_below_zero": (no_op["total"] <= 0) if no_op else None,
        "false_claim_below_no_op": (
            all(r["total"] < no_op["total"] for r in false_claims)
            if false_claims and no_op else None),
        "parse_confidence": confidence,
    }


# --------------------------------------------------------------- run card
def _suite_flags(suite: Path) -> set[str]:
    """Which flags this suite's argparse declares. GT001 has no --quick and
    would exit 2 on it, which is a parser failure dressed as a verdict."""
    text = suite.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"""add_argument\(\s*["'](--[\w-]+)["']""", text))


def run_card(task_id: str, tasks_dir: Path, variant: str | None = None,
             quick: bool = False, timeout_s: int = 3600) -> dict:
    """Run tests/run_baselines.py and parse it. The suite is run from the task
    directory with the variant in taskspec.VARIANT_ENV - the same channel
    run_rollouts.py uses - and ALSO as --variant where the suite takes it,
    because GT009 resolves the arm from argv before argparse."""
    root = Path(tasks_dir) / task_id
    suite = root / "tests" / "run_baselines.py"
    spec = taskspec.load(task_id, Path(tasks_dir), variant=variant)
    declared = {r["agent"]: r["expect"]
                for r in validity_rows(spec.raw, spec.variant or "")}
    if not suite.is_file():
        return {"ran": False, "task_valid_printed": False, "exit_code": -1,
                "duration_s": 0.0, "baselines": {}, "oracle_at_max": None,
                "no_op_at_or_below_zero": None, "false_claim_below_no_op": None,
                "parse_confidence": "none", "argv": [],
                "stdout_tail": f"no suite at {suite}"}
    flags = _suite_flags(suite)
    argv = [sys.executable, str(suite)]
    if variant and "--variant" in flags:
        argv += ["--variant", variant]
    if quick and "--quick" in flags:
        argv.append("--quick")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if variant:
        env[taskspec.VARIANT_ENV] = variant
    else:
        env.pop(taskspec.VARIANT_ENV, None)
    started = time.monotonic()
    try:
        done = subprocess.run(argv, cwd=str(root), env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout_s, check=False)
        out, code = done.stdout + done.stderr, done.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace") \
            if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        out += f"\n[validity] timed out after {timeout_s}s"
        code = -1
    parsed = parse_baselines(out, declared)
    return {"ran": True, "exit_code": code,
            "duration_s": round(time.monotonic() - started, 1),
            "argv": argv, **parsed, "stdout_tail": out[-4000:]}


def card(task_id: str, tasks_dir: Path, variant: str | None = None,
         run: bool = False, quick: bool = False, timeout_s: int = 3600) -> dict:
    out = static_card(task_id, tasks_dir, variant)
    if run:
        out["run"] = run_card(task_id, tasks_dir, variant, quick=quick,
                              timeout_s=timeout_s)
        if not out["run"]["task_valid_printed"]:
            out["valid"] = False
            out["invalid_because"].append(
                "tests/run_baselines.py did not print TASK VALID "
                f"(exit {out['run']['exit_code']}, parse "
                f"{out['run']['parse_confidence']})")
    return out


# ----------------------------------------------------------------- render
def render(c: dict) -> str:
    s = c["static"]
    arm = c["variant"] or "default"
    lines = [f"# Validity card - {c['task']} ({arm})", "",
             f"**valid: {c['valid']}**" + (
                 "" if c["valid"] else " - " + "; ".join(c["invalid_because"])),
             "", "## Static", "", "| field | value |", "|---|---|"]
    for key in ("matrix_present", "pass_axis", "gate", "grader_check_source",
                "accepted", "accepted_not_in_validity", "validity_rows",
                "has_oracle_row", "has_no_op_row", "has_false_claim_row",
                "has_positive_control_row", "map_checks_missing_from_grader",
                "pass_requires_checks_missing_from_grader", "proxy_declared",
                "required_surfaces_declared", "dead_by_design_declared"):
        lines.append(f"| {key} | `{s[key]!r}` |")
    lines.append(f"| grader_check_ids | {', '.join(s['grader_check_ids'])} |")
    lines += ["", "### Findings", ""]
    if not s["findings"]:
        lines.append("none")
    for f in s["findings"]:
        lines.append(f"- **{f['severity']}** `{f['code']}`: {f['detail']}")
    r = c.get("run")
    lines += ["", "## Run", ""]
    if r is None:
        lines.append("not run (static card only)")
    else:
        lines += [
            (f"ran: {r['ran']}; exit {r['exit_code']}; {r['duration_s']}s; "
             f"TASK VALID printed: {r['task_valid_printed']}; "
             f"parse: {r['parse_confidence']}"),
            (f"oracle_at_max: {r['oracle_at_max']}; "
             f"no_op_at_or_below_zero: {r['no_op_at_or_below_zero']}; "
             f"false_claim_below_no_op: {r['false_claim_below_no_op']}"),
            "", "| baseline | total | max | passed | expect | met |",
            "|---|---|---|---|---|---|"]
        for name, b in r["baselines"].items():
            lines.append(f"| {name} | {b['total']} | {b['max']} | {b['passed']} "
                         f"| {b['expect'] or ''} | {b['met']} |")
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", required=True)
    ap.add_argument("--tasks-dir", default=str(taskspec.TASKS_DIR))
    ap.add_argument("--variant", default=None)
    ap.add_argument("--run", action="store_true",
                    help="also run tests/run_baselines.py (minutes to an hour)")
    ap.add_argument("--quick", action="store_true",
                    help="pass --quick to suites that take it")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--out", default=None,
                    help="directory for validity.json + validity.md; "
                         "default prints the markdown")
    a = ap.parse_args(argv)
    try:
        c = card(a.task, Path(a.tasks_dir), a.variant, run=a.run,
                 quick=a.quick, timeout_s=a.timeout)
    except (taskspec.TaskSpecError, OSError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    md = render(c)
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "validity.json").write_text(json.dumps(c, indent=2),
                                           encoding="utf-8")
        (out / "validity.md").write_text(md, encoding="utf-8")
        print(f"wrote {out / 'validity.json'} and {out / 'validity.md'}")
    else:
        print(md)
    return 0 if c["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
