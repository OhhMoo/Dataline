"""Layer 0 - the task validity card.

Before any rollout of a task counts, the TASK has to be shown valid: the
oracle scores max, the no-op scores nothing, a false claim scores below the
no-op, every check the scoring matrix names is one the grader emits, and every
baseline `[matrix].accepted` names is a declared validity baseline. Today that
is done by hand per task, and GT007 once shipped a matrix naming a check
(`D2`) its grader never emitted, so its headline was withheld as n/a on a task
at 5/5 - silently. The same hole in `[matrix.outcome]` is a BLOCK here.

Two halves, kept apart so a report can say which one it is quoting:

  static_card  reads task.toml (through taskspec, so the arm's matrix
               override is the one audited) and the grader's check table.
               No subprocess, no DuckDB, seconds.
  run_card     runs tests/run_baselines.py as a subprocess - minutes to an
               hour - and PARSES its stdout. Every suite prints a different
               table, so the parser records how much of it it understood
               (`parse_confidence`) rather than pretending.
  ladder_card  (--ladder, DECISION_TEMPLATE.md §5/§9) reads the JSON that
               `run_baselines.py --json` writes (baselines_schema.py),
               classifies every baseline with the arm's [matrix.outcome],
               prices it with reward.py and refuses the arm when a required
               baseline is missing, a class disagrees with the template or
               the suite's own `outcome`, or the scalar mis-orders the tiers.
  lab_card     (--lab-card) is lab_card.py's A1-A12 / O1-O8 table.

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

import baselines_schema
import outcome
import reward
import taskspec
from outcome import classify_outcome

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
    missing_outcome: dict[str, list[str]] = {}
    accepted: list[str] = []
    must_have: list[str] = []
    if m is None:
        findings.append(_finding(INFO, "no_matrix",
                                 "task declares no [matrix]; Pass@1 is the "
                                 "pass_requires_checks AND"))
    else:
        accepted = list(m.accepted)
        table = getattr(m, "outcome", None)
        if table is not None and hasattr(table, "conditions_for"):
            # every check the outcome table names must be one the grader
            # emits: a class that reads a check the grader never emits can
            # never fire (an ALL class) or never charge (an ANY class), and
            # the headline pass / score would be read off a table with a
            # hole in it. taskspec refuses this where the manifest is
            # readable; this is the check against the wider id set.
            for cls, conds in table.map.items():
                gone = sorted({outcome.split_condition(c)[1] for c in conds
                               if known
                               and outcome.split_condition(c)[1] not in known})
                if gone:
                    missing_outcome[cls] = gone
                    findings.append(_finding(
                        BLOCK, "outcome_check_missing_from_grader",
                        f"[matrix.outcome].{cls} names {gone}, which the "
                        f"grader ({source}) never emits; the class would "
                        f"read off a hole forever"))
            must_have = sorted(outcome.must_have_conditions(
                table, getattr(m, "role", None) or "control"))

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
            "role": getattr(m, "role", None) if m else None,
            "outcome_declared": bool(m is not None and m.outcome is not None),
            "must_have": must_have,
            "grader_check_ids": ids,
            "grader_check_source": source,
            "outcome_checks_missing_from_grader": missing_outcome,
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


# ------------------------------------------------------------- the ladder
# DECISION_TEMPLATE.md §5 and §9: every baseline the arm ships is classified
# with the arm's own [matrix.outcome] table and priced with the declared
# scalar (reward.py); the arm is refused when a class disagrees with what the
# template (or the suite's own `outcome`) says it must be, or when the scalar
# does not order the classes resolved > partial >= silent > damage.
LADDER_NO_OUTCOME_TABLE = "LADDER_NO_OUTCOME_TABLE"
BASELINES_SCHEMA = "BASELINES_SCHEMA"
LADDER_MISSING_BASELINE = "LADDER_MISSING_BASELINE"
LADDER_OUTCOME_MISMATCH = "LADDER_OUTCOME_MISMATCH"
LADDER_ORDER_VIOLATION = "LADDER_ORDER_VIOLATION"
PROGRESS_UNDECLARED = "PROGRESS_UNDECLARED"
PROGRESS_MODULE_UNAVAILABLE = "PROGRESS_MODULE_UNAVAILABLE"
PROGRESS_UNREACHABLE = "PROGRESS_UNREACHABLE"

# The scalar's tiers, top to bottom. `withheld_with_evidence` is the pass on a
# decision arm and a partial on a control arm (reward.band_for), so which tier
# it joins depends on the role.
_DAMAGE = ("false_claim", "boundary_violated")


def _tier(expected: str, role: str) -> str | None:
    """top | partial | silent | damage, or None for a class the ladder does
    not order (unmeasured)."""
    if expected == "resolved":
        return "top"
    if expected == "withheld_with_evidence":
        return "top" if role == "decision" else "partial"
    if expected == "acted_unresolved":
        return "partial"
    if expected == "silent":
        return "silent"
    if expected in _DAMAGE:
        return "damage"
    return None


def _progress_for_arm(matrix, bj: dict, findings: list[dict]):
    """(progress module, anchors) when the arm declares [matrix.progress] and
    progress.py is importable; (None, None) otherwise, with one info finding
    saying which. Imported lazily: H2 lands the module concurrently."""
    try:
        import progress  # noqa: WPS433 - optional layer, see module doc
    except ImportError:
        findings.append(_finding(INFO, PROGRESS_MODULE_UNAVAILABLE,
                                 "progress.py is not importable here; the "
                                 "ladder prices every partial class at its "
                                 "band floor"))
        return None, None
    spec = getattr(matrix, "progress", None)
    if spec is None:
        findings.append(_finding(INFO, PROGRESS_UNDECLARED,
                                 "the arm declares no [matrix.progress]; the "
                                 "two partial classes are priced at their "
                                 "band floor and reachability is not checked"))
        return None, None
    try:
        anchors = progress.anchors_from_baselines(spec, bj)
    except (KeyError, TypeError, ValueError) as exc:
        findings.append(_finding(BLOCK, BASELINES_SCHEMA,
                                 f"[matrix.progress] anchors could not be read "
                                 f"from the baseline JSON: {exc}"))
        return None, None
    return progress, anchors


def ladder_card(task_id: str, tasks_dir: Path, variant: str | None,
                baselines_json_path: Path) -> dict:
    """Classify and price every baseline in a `run_baselines.py --json` file
    against the arm's [matrix.outcome] and the declared scalar. Raises
    taskspec.TaskSpecError when the package refuses to load (CLI exit 3);
    everything else is a finding."""
    spec = taskspec.load(task_id, Path(tasks_dir), variant=variant)
    arm = spec.variant or "default"
    matrix = spec.matrix
    role = getattr(matrix, "role", None) or "control"
    findings: list[dict] = []
    ladder: list[dict] = []
    out = {"schema_version": SCHEMA_VERSION, "task": task_id, "variant": arm,
           "role": role, "baselines_json": str(baselines_json_path),
           "findings": findings, "ladder": ladder,
           "progress_reachability": None}

    outcome_spec = getattr(matrix, "outcome", None) if matrix is not None else None
    if outcome_spec is None:
        findings.append(_finding(BLOCK, LADDER_NO_OUTCOME_TABLE,
                                 f"arm {arm!r} declares no [matrix.outcome]; "
                                 f"no baseline can be classified"))

    try:
        bj = json.loads(Path(baselines_json_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        findings.append(_finding(BLOCK, BASELINES_SCHEMA,
                                 f"{baselines_json_path}: {exc}"))
        out["valid"] = False
        return out
    for err in baselines_schema.validate(bj):
        findings.append(_finding(BLOCK, BASELINES_SCHEMA, err))
    baselines = bj.get("baselines") if isinstance(bj, dict) else None
    if not isinstance(baselines, dict):
        baselines = {}
    if isinstance(bj, dict) and bj.get("task") not in (None, task_id):
        findings.append(_finding(BLOCK, BASELINES_SCHEMA,
                                 f"$.task is {bj.get('task')!r}, card is for "
                                 f"{task_id!r}"))
    if isinstance(bj, dict) and bj.get("variant") not in (None, arm):
        findings.append(_finding(BLOCK, BASELINES_SCHEMA,
                                 f"$.variant is {bj.get('variant')!r}, card is "
                                 f"for arm {arm!r}"))
    if isinstance(bj, dict) and bj.get("role") not in (None, role):
        findings.append(_finding(BLOCK, BASELINES_SCHEMA,
                                 f"$.role is {bj.get('role')!r}, the arm declares "
                                 f"role {role!r}; the runner and task.toml "
                                 f"disagree on which band withholding lands in"))

    expected_table = baselines_schema.EXPECTED_OUTCOME[role]
    for name in baselines_schema.missing_required(baselines, role):
        findings.append(_finding(
            BLOCK, LADDER_MISSING_BASELINE,
            f"role {role!r} requires baseline {name!r} (expected "
            f"{expected_table[name]!r}); the JSON carries "
            f"{sorted(baselines)}"))
    if outcome_spec is None:
        out["valid"] = False
        return out

    prog_mod, anchors = (None, None)
    if isinstance(bj, dict):
        prog_mod, anchors = _progress_for_arm(matrix, bj, findings)

    for name, row in baselines.items():
        if not isinstance(row, dict) or not isinstance(row.get("checks"), dict):
            continue  # already a BASELINES_SCHEMA block
        checks = row["checks"]
        classified = classify_outcome(outcome_spec, checks,
                                      row.get("artifact"))["outcome"]
        declared = row.get("outcome") if isinstance(row.get("outcome"), str) else None
        expected = expected_table.get(name, declared)
        if declared is not None and declared != classified:
            findings.append(_finding(
                BLOCK, LADDER_OUTCOME_MISMATCH,
                f"{name}: the suite declares outcome {declared!r}, "
                f"[matrix.outcome] classifies its checks as {classified!r}"))
        if name in expected_table and expected_table[name] != classified:
            findings.append(_finding(
                BLOCK, LADDER_OUTCOME_MISMATCH,
                f"{name}: the template requires {expected_table[name]!r} on a "
                f"{role} arm, [matrix.outcome] classifies its checks as "
                f"{classified!r}"))
        p = None
        if prog_mod is not None:
            try:
                p = prog_mod.episode_progress(matrix.progress, checks, anchors)
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                findings.append(_finding(WARN, PROGRESS_UNREACHABLE,
                                         f"{name}: progress could not be "
                                         f"computed: {exc}"))
        r = reward.episode_reward(classified, role, p, 0.0)
        ladder.append({"baseline": name, "declared_outcome": declared,
                       "expected_outcome": expected_table.get(name),
                       "classified_outcome": classified,
                       "reward": r["reward"], "excluded": r["excluded"],
                       "progress": p,
                       "total": row.get("total"), "max": row.get("max")})

    # The ordering the scalar must produce, judged on the classes the template
    # (or the suite) SAYS each baseline is, priced at what it classified as.
    # A baseline that classified wrongly therefore also shows up here when
    # its reward lands in the wrong tier, which is the point: mis-ordering is
    # what makes the scalar unusable, the mismatch is why.
    tiers: dict[str, list[dict]] = {"top": [], "partial": [], "silent": [],
                                    "damage": []}
    for row in ladder:
        exp = row["expected_outcome"] or row["declared_outcome"]
        t = _tier(exp, role) if exp else None
        if t is not None and row["reward"] is not None:
            tiers[t].append(row)

    def _order(upper: str, lower: str, strict: bool) -> None:
        for hi in tiers[upper]:
            for lo in tiers[lower]:
                ok = hi["reward"] > lo["reward"] if strict \
                    else hi["reward"] >= lo["reward"]
                if not ok:
                    findings.append(_finding(
                        BLOCK, LADDER_ORDER_VIOLATION,
                        f"{hi['baseline']} (expected {upper}, reward "
                        f"{hi['reward']}) must be {'>' if strict else '>='} "
                        f"{lo['baseline']} (expected {lower}, reward "
                        f"{lo['reward']})"))

    _order("top", "partial", strict=True)
    _order("top", "silent", strict=True)
    _order("top", "damage", strict=True)
    _order("partial", "silent", strict=False)
    _order("partial", "damage", strict=True)
    _order("silent", "damage", strict=True)
    # Within the top tier the template writes `resolved >= withheld_with_evidence`
    # (both 1.0 on a decision arm); a scalar that paid the withholding MORE
    # than the repair would be one that prefers stopping.
    for hi in tiers["top"]:
        if (hi["expected_outcome"] or hi["declared_outcome"]) != "resolved":
            continue
        for lo in tiers["top"]:
            if (lo["expected_outcome"] or lo["declared_outcome"]) \
                    == "withheld_with_evidence" and hi["reward"] < lo["reward"]:
                findings.append(_finding(
                    BLOCK, LADDER_ORDER_VIOLATION,
                    f"{hi['baseline']} (expected resolved, reward {hi['reward']}) "
                    f"must be >= {lo['baseline']} (expected "
                    f"withheld_with_evidence, reward {lo['reward']})"))

    if prog_mod is not None:
        values = [row["progress"] for row in ladder if row["progress"] is not None]
        if values:
            reach = prog_mod.reachability(values)
            out["progress_reachability"] = reach
            if reach.get("distinct", 0) < 3:
                findings.append(_finding(
                    WARN, PROGRESS_UNREACHABLE,
                    f"[matrix.progress] takes {reach.get('distinct')} distinct "
                    f"value(s) across {len(values)} baselines; a quantity the "
                    f"rivals cannot spread is a bit in disguise "
                    f"(docs/research/Scoring_Redesign.md §3)"))
    out["valid"] = not any(f["severity"] == BLOCK for f in findings)
    return out


def render_ladder(c: dict) -> str:
    lines = [f"# Validity ladder - {c['task']} ({c['variant']}, role {c['role']})",
             "", f"**valid: {c['valid']}**", "",
             "| baseline | expected | declared | classified | reward | progress | total |",
             "|---|---|---|---|---|---|---|"]
    for row in sorted(c["ladder"], key=lambda r: (-(r["reward"] if r["reward"]
                                                  is not None else -9), r["baseline"])):
        total = "" if row["total"] is None else f"{row['total']} / {row['max']}"
        lines.append(f"| {row['baseline']} | {row['expected_outcome'] or ''} "
                     f"| {row['declared_outcome'] or ''} | {row['classified_outcome']} "
                     f"| {'excluded' if row['reward'] is None else row['reward']} "
                     f"| {'' if row['progress'] is None else row['progress']} "
                     f"| {total} |")
    lines += ["", "## Findings", ""]
    if not c["findings"]:
        lines.append("none")
    for f in c["findings"]:
        lines.append(f"- **{f['severity']}** `{f['code']}`: {f['detail']}")
    if c.get("progress_reachability"):
        lines += ["", f"progress reachability: `{c['progress_reachability']!r}`"]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- render
def render(c: dict) -> str:
    s = c["static"]
    arm = c["variant"] or "default"
    lines = [f"# Validity card - {c['task']} ({arm})", "",
             f"**valid: {c['valid']}**" + (
                 "" if c["valid"] else " - " + "; ".join(c["invalid_because"])),
             "", "## Static", "", "| field | value |", "|---|---|"]
    for key in ("matrix_present", "role", "outcome_declared", "must_have",
                "grader_check_source",
                "accepted", "accepted_not_in_validity", "validity_rows",
                "has_oracle_row", "has_no_op_row", "has_false_claim_row",
                "has_positive_control_row", "outcome_checks_missing_from_grader",
                "pass_requires_checks_missing_from_grader",
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
    ap.add_argument("--ladder", default=None, metavar="BASELINES.json",
                    help="classify and price the baselines in a "
                         "`run_baselines.py --json` file for --variant; "
                         "exit 1 on any block finding (needs --variant)")
    ap.add_argument("--lab-card", action="store_true",
                    help="print the A1-A12 / O1-O8 requirement table for "
                         "--task; always exits 0")
    ap.add_argument("--stored-runs", default=None, metavar="DIR",
                    help="with --lab-card: directory holding "
                         "<task>/**/report.json for the run-dependent rows")
    a = ap.parse_args(argv)
    if a.lab_card:
        import lab_card
        try:
            md = lab_card.render(a.task, Path(a.tasks_dir),
                                 stored_runs=a.stored_runs)
        except (taskspec.TaskSpecError, OSError, ValueError) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
        try:
            print(md)
        except UnicodeEncodeError:  # the requirement texts carry em dashes
            enc = sys.stdout.encoding or "ascii"
            print(md.encode(enc, "replace").decode(enc))
        return 0
    if a.ladder is not None:
        if not a.variant:
            ap.error("--ladder needs --variant (the arm whose [matrix.outcome] "
                     "classifies the baselines)")
        try:
            lc = ladder_card(a.task, Path(a.tasks_dir), a.variant, Path(a.ladder))
        except (taskspec.TaskSpecError, OSError, ValueError) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
        md = render_ladder(lc)
        if a.out:
            out = Path(a.out)
            out.mkdir(parents=True, exist_ok=True)
            (out / "ladder.json").write_text(json.dumps(lc, indent=2),
                                             encoding="utf-8")
            (out / "ladder.md").write_text(md, encoding="utf-8")
            print(f"wrote {out / 'ladder.json'} and {out / 'ladder.md'}")
        else:
            print(md)
        return 0 if lc["valid"] else 1
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
