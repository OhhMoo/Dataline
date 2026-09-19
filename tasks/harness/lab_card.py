"""The lab card: one task against the two labs' published checklists.

Anthropic's "Demystifying evals for AI agents" (A1-A12) and OpenAI's
"Reinforcement fine-tuning use cases" (O1-O8) are the two public statements
of what a lab reads off an environment before it trains on it. The ids and
texts are the requirement-traceability table of
docs/superpowers/plans/2026-09-18-lab-grade-eval-structure.md §0, copied
here verbatim so `validity.py --lab-card` prints the same rows the plan
promised.

Every row has a PROBE that answers from files, never from opinion:

  met        the file or report field the row points at is there and says so
  unmet      it is not, and `detail` says what is missing
  needs_run  the row can only be answered from a run (pass@k, group
             variance, saturation, class balance, tracked metrics) and no
             stored run was given - or none carries a report.json

Static probes read the task package: task.toml (arms, `role`, [matrix],
[matrix.outcome], [matrix.progress], [[validity]] rows, [grading]),
tests/grade.py (its import lines), DESIGN.md (the template's §2 staging
words and §7 pre-registration), tests/VALIDITY.md (falsifier and
determinism records), tests/run_baselines.py (whether it declares --json for
the ladder) and this harness's own export.py (whether the RFT grader export
exists). Run-dependent probes read the newest report.json under
`<stored_runs>/<task_id>/`.

Two ids that name the same property share one probe and the detail says so
(A6/O1 deterministic grader; A7/O5 graded score; A8/O6 hack resistance).
The card is a reading, never a gate: `--lab-card` always exits 0. What it
must never do is round a missing thing up to `met` - GT009 today prints
several `unmet` rows (no [matrix.progress], no `role`, no pre-registration)
and that is the card doing its job.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import validity

ANTHROPIC = "https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents"
OPENAI = "https://developers.openai.com/api/docs/guides/rft-use-cases"

MET, UNMET, NEEDS_RUN = "met", "unmet", "needs_run"
STATUSES = (MET, UNMET, NEEDS_RUN)

_IMPORT_LINE = re.compile(r"^\s*(?:from\s+(\S+)\s+import|import\s+(.+))$", re.MULTILINE)
_JUDGE_MODULES = re.compile(r"\b(judge|llm|openai|anthropic)\b", re.IGNORECASE)
#: the template's "§7" - either literally numbered 7, or any level-2 heading
#: that names pre-registration (a task whose DESIGN.md already has a §7 adds
#: the section under its own number)
_H2_7 = re.compile(r"^##\s*(7\b.*|.*pre-?registration.*)$",
                   re.MULTILINE | re.IGNORECASE)
_PREREG = re.compile(r"pre-?regist|predicted band|failure hypothesis", re.IGNORECASE)
_STAGING_WORDS = ("owner", "boundary", "consumer", "cost")
_JSON_FLAG = re.compile(r"""add_argument\(\s*["']--json["']""")
_RFT_GRADER = re.compile(r"^\s*def\s+rft_grader\s*\(", re.MULTILINE)
_PATH_READS = re.compile(r"actions\.jsonl|transcript\.jsonl|action_log", re.IGNORECASE)


# --------------------------------------------------------------- context
@dataclass
class TaskContext:
    task_id: str
    root: Path
    raw: dict
    arms: list[str]                       # [""] for a task with no [variants]
    grade_py: str | None
    design_md: str | None
    validity_md: str | None
    run_baselines_py: str | None
    export_py: str | None
    stored_runs: Path | None
    report: dict | None
    report_path: Path | None
    _matrix_cache: dict = field(default_factory=dict)

    def matrix(self, arm: str) -> dict:
        """The arm's resolved raw [matrix]: task-level keys overridden per key
        by [variants.<arm>.matrix], the same rule taskspec applies."""
        if arm in self._matrix_cache:
            return self._matrix_cache[arm]
        base = self.raw.get("matrix") or {}
        over = ((self.raw.get("variants") or {}).get(arm) or {}).get("matrix") or {}
        out = {**base, **over} if isinstance(base, dict) and isinstance(over, dict) \
            else (base if isinstance(base, dict) else {})
        self._matrix_cache[arm] = out
        return out

    def arm_block(self, arm: str) -> dict:
        block = (self.raw.get("variants") or {}).get(arm)
        return block if isinstance(block, dict) else {}


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _newest_report(stored_runs: Path | None, task_id: str) -> tuple[dict | None, Path | None]:
    if stored_runs is None:
        return None, None
    root = Path(stored_runs) / task_id
    if not root.is_dir():
        return None, None
    candidates = sorted(root.rglob("report.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            import json
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            return doc, path
    return None, None


def load_context(task_id: str, tasks_dir: Path, stored_runs=None) -> TaskContext:
    root = Path(tasks_dir) / task_id
    toml_path = root / "task.toml"
    if not toml_path.is_file():
        raise validity.taskspec.TaskSpecError(f"no task.toml at {toml_path}")
    raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    variants = raw.get("variants") or {}
    arms = sorted(variants) if isinstance(variants, dict) and variants else [""]
    grading = raw.get("grading") or {}
    entry = root / str(grading.get("entrypoint") or "tests/grade.py")
    report, report_path = _newest_report(
        Path(stored_runs) if stored_runs else None, task_id)
    return TaskContext(
        task_id=task_id, root=root, raw=raw, arms=arms,
        grade_py=_read(entry),
        design_md=_read(root / "DESIGN.md"),
        validity_md=_read(root / "tests" / "VALIDITY.md"),
        run_baselines_py=_read(root / "tests" / "run_baselines.py"),
        export_py=_read(Path(__file__).resolve().parent / "export.py"),
        stored_runs=Path(stored_runs) if stored_runs else None,
        report=report, report_path=report_path)


def _arm_label(arm: str) -> str:
    return arm or "default"


# ---------------------------------------------------------- static probes
def _probe_outcome_not_path(ctx: TaskContext):
    if ctx.grade_py is None:
        return UNMET, "no grader at [grading].entrypoint"
    grading = ctx.raw.get("grading") or {}
    reasons = []
    if grading.get("action_log_required"):
        reasons.append("[grading].action_log_required = true")
    hits = sorted(set(m.lower() for m in _PATH_READS.findall(ctx.grade_py)))
    if hits:
        reasons.append(f"grade.py names {hits}")
    if reasons:
        return UNMET, "grader reads the path: " + "; ".join(reasons)
    return MET, ("grade.py reads world state only: no transcript / action-log "
                 "reference; [grading].action_log_required is "
                 f"{grading.get('action_log_required', 'absent')!r}")


def _oracle_rows(ctx: TaskContext) -> dict[str, bool]:
    return {arm: any(validity._is_oracle(r)
                     for r in validity.validity_rows(ctx.raw, arm))
            for arm in ctx.arms}


def _probe_reference_solution(ctx: TaskContext):
    have = _oracle_rows(ctx)
    json_flag = bool(ctx.run_baselines_py and _JSON_FLAG.search(ctx.run_baselines_py))
    json_note = ("run_baselines.py declares --json (ladder input)" if json_flag
                 else "run_baselines.py declares no --json yet; --ladder needs "
                      "a hand-built or Wave-2 JSON")
    missing = [_arm_label(a) for a, ok in have.items() if not ok]
    if missing:
        return UNMET, f"no oracle-at-max [[validity]] row on arm(s) {missing}; {json_note}"
    return MET, (f"oracle row expected at max on {len(have)} arm(s) "
                 f"({', '.join(_arm_label(a) for a in have)}); {json_note}")


def _probe_preregistration(ctx: TaskContext):
    control = any(validity._is_positive_control(r)
                  for arm in ctx.arms for r in validity.validity_rows(ctx.raw, arm))
    control_note = ("positive-control row present" if control
                    else "no positive-control row")
    if ctx.design_md is None:
        return UNMET, f"no DESIGN.md; {control_note}"
    heading = _H2_7.search(ctx.design_md)
    prereg = _PREREG.search(ctx.design_md)
    if heading and prereg and control:
        return MET, (f"DESIGN.md §7 {heading.group(0).strip()!r} carries "
                     f"pre-registration text; {control_note}")
    why = []
    if not heading:
        why.append("DESIGN.md has no `## 7` heading")
    elif not prereg:
        why.append(f"DESIGN.md §7 is {heading.group(0).strip()!r}, not a "
                   f"pre-registration (no 'pre-registration' / 'predicted "
                   f"band' / 'failure hypothesis' text)")
    if not control:
        why.append(control_note)
    return UNMET, "; ".join(why)


def _roles(ctx: TaskContext) -> dict[str, str | None]:
    return {arm: (ctx.arm_block(arm).get("role") if arm else None) for arm in ctx.arms}


def _probe_two_arms_with_roles(ctx: TaskContext):
    roles = _roles(ctx)
    declared = {a: r for a, r in roles.items() if r}
    if len(ctx.arms) < 2 or ctx.arms == [""]:
        return UNMET, (f"{len([a for a in ctx.arms if a])} arm(s) declared; the "
                       f"template wants a control arm and a decision arm")
    if not declared:
        return UNMET, (f"{len(ctx.arms)} arms declared "
                       f"({', '.join(ctx.arms)}) but none declares role=; "
                       f"which side of the decision each prices is inferred "
                       f"from its name")
    kinds = set(declared.values())
    if not {"control", "decision"} <= kinds:
        return UNMET, (f"arms declare roles {declared}; both a control and a "
                       f"decision arm are required")
    undeclared = [a for a, r in roles.items() if not r]
    if undeclared:
        return UNMET, f"arm(s) {undeclared} declare no role=; declared: {declared}"
    return MET, f"roles per arm: {declared}"


def _probe_isolation(ctx: TaskContext):
    harness = ctx.raw.get("harness") or {}
    builder = harness.get("world_builder")
    det = bool(ctx.validity_md and re.search(r"determin", ctx.validity_md, re.IGNORECASE))
    if not builder:
        return UNMET, "no [harness].world_builder: no per-trial world to build"
    if not det:
        return UNMET, (f"[harness].world_builder = {builder!r} builds each "
                       f"trial's world, but tests/VALIDITY.md records no "
                       f"determinism check (two builds agree)")
    return MET, (f"per-trial world from {builder!r} (rollout.py copies it and "
                 f"builds the grading zone beside it); VALIDITY.md records the "
                 f"determinism gate")


def _grader_imports(text: str) -> list[str]:
    mods = []
    for m in _IMPORT_LINE.finditer(text):
        mods.append((m.group(1) or m.group(2)).strip())
    return mods


def _probe_deterministic_grader(ctx: TaskContext):
    if ctx.grade_py is None:
        return UNMET, "no grader at [grading].entrypoint"
    mods = _grader_imports(ctx.grade_py)
    bad = sorted({m for m in mods if _JUDGE_MODULES.search(m)})
    mode = (ctx.raw.get("grading") or {}).get("mode")
    if bad:
        return UNMET, f"grade.py imports {bad}; [grading].mode = {mode!r}"
    if mode not in (None, "deterministic"):
        return UNMET, f"[grading].mode = {mode!r}; no judge import found"
    return MET, (f"grade.py imports no judge / llm / openai / anthropic module "
                 f"({len(mods)} import lines); [grading].mode = {mode!r}")


def _has_table(ctx: TaskContext, key: str) -> dict[str, bool]:
    return {arm: isinstance(ctx.matrix(arm).get(key), dict) and bool(ctx.matrix(arm).get(key))
            for arm in ctx.arms}


def _probe_graded_score(ctx: TaskContext):
    outcome = _has_table(ctx, "outcome")
    progress = _has_table(ctx, "progress")
    no_outcome = [_arm_label(a) for a, ok in outcome.items() if not ok]
    no_progress = [_arm_label(a) for a, ok in progress.items() if not ok]
    if no_outcome:
        return UNMET, (f"no [matrix.outcome] on arm(s) {no_outcome}; without "
                       f"the class there is no band to put progress in")
    if no_progress:
        return UNMET, (f"no progress quantities declared: [matrix.outcome] "
                       f"present on every arm, [matrix.progress] absent on "
                       f"{no_progress}; the partial classes sit at their band floor")
    return MET, ("[matrix.outcome] and [matrix.progress] declared on every arm: "
                 "band from the class, progress inside the two partial classes")


def _probe_hack_resistance(ctx: TaskContext):
    fals = bool(ctx.validity_md and re.search(r"falsifier", ctx.validity_md, re.IGNORECASE))
    why = []
    if not fals:
        why.append("tests/VALIDITY.md records no falsifier count")
    for arm in ctx.arms:
        table = ctx.matrix(arm).get("outcome")
        if not isinstance(table, dict):
            why.append(f"arm {_arm_label(arm)!r} declares no [matrix.outcome]")
            continue
        for cls in ("false_claim", "boundary_violated"):
            if not table.get(cls):
                why.append(f"arm {_arm_label(arm)!r} names no {cls} check")
    if why:
        return UNMET, "; ".join(why)
    return MET, ("falsifier recorded in tests/VALIDITY.md; every arm's "
                 "[matrix.outcome] charges false_claim and boundary_violated")


def _probe_accepted_and_staging(ctx: TaskContext):
    why = []
    for arm in ctx.arms:
        acc = ctx.matrix(arm).get("accepted")
        if not acc:
            why.append(f"arm {_arm_label(arm)!r} declares no [matrix].accepted")
    if ctx.design_md is None:
        why.append("no DESIGN.md")
    else:
        low = ctx.design_md.lower()
        missing = [w for w in _STAGING_WORDS if not re.search(rf"\b{w}", low)]
        if missing:
            why.append(f"DESIGN.md never names {missing} (template §2: owner, "
                       f"boundary, consumer, cost of acting / not acting)")
    if why:
        return UNMET, "; ".join(why)
    return MET, ("`accepted` frozen on every arm; DESIGN.md names the owner, "
                 "the boundary, the consumer and both costs")


def _probe_no_lucky_guess(ctx: TaskContext):
    why = []
    for arm in ctx.arms:
        m = ctx.matrix(arm)
        axis = m.get("pass_axis")
        mapped = (m.get("map") or {}).get(axis) if isinstance(m.get("map"), dict) else None
        if not axis or not mapped:
            why.append(f"arm {_arm_label(arm)!r}: pass_axis {axis!r} maps to no checks")
        table = m.get("outcome")
        if not isinstance(table, dict) or not table.get("withheld_with_evidence"):
            why.append(f"arm {_arm_label(arm)!r}: no withheld_with_evidence "
                       f"conditions, so withholding cannot be told from silence")
    if why:
        return UNMET, "; ".join(why)
    return MET, ("pass axis maps to grader checks on every arm (whole-object "
                 "receipts, never row presence); withheld_with_evidence needs "
                 "positive evidence; silent is derived and pays 0")


def _probe_rft_grader_export(ctx: TaskContext):
    if ctx.export_py and _RFT_GRADER.search(ctx.export_py):
        return MET, "tasks/harness/export.py defines rft_grader (grade(sample, item) + multi)"
    return UNMET, ("tasks/harness/export.py defines no rft_grader; no "
                   "grade(sample, item) -> float export exists yet")


# ------------------------------------------------------------ run probes
def _needs_run(ctx: TaskContext, what: str):
    if ctx.stored_runs is None:
        return NEEDS_RUN, f"{what}: answered from a run's report.json; none given (--stored-runs)"
    return NEEDS_RUN, (f"{what}: no report.json under "
                       f"{ctx.stored_runs / ctx.task_id}")


def _probe_pass_at_k(ctx: TaskContext):
    if ctx.report is None:
        return _needs_run(ctx, "pass@k and pass^k with CI")
    r = ctx.report
    have = [k for k in ("pass_at_k", "pass_hat_k", "pass_at_1_ci95") if k in r]
    if {"pass_at_k", "pass_at_1_ci95"} <= set(have):
        return MET, (f"{ctx.report_path.name}: {have}; n = {r.get('pass_at_1_n')}, "
                     f"pass@1 = {r.get('pass_at_1')} CI {r.get('pass_at_1_ci95')}")
    return UNMET, f"{ctx.report_path}: carries {have}, needs pass_at_k and pass_at_1_ci95"


def _probe_transcripts_read(ctx: TaskContext):
    if ctx.report is None:
        return _needs_run(ctx, "fairness read of every miss")
    for key in ("fairness_read", "fairness", "transcript_review"):
        if key in ctx.report:
            return MET, f"{ctx.report_path.name} carries `{key}`"
    return UNMET, (f"{ctx.report_path.name} records no fairness read; misses "
                   f"carry an outcome class but nobody has signed that they "
                   f"seem fair")


def _probe_saturation(ctx: TaskContext):
    if ctx.report is None:
        return _needs_run(ctx, "saturation / band")
    ls = ctx.report.get("learning_signal") or {}
    band = ls.get("band") if isinstance(ls, dict) else None
    if band is None and "saturated" not in ctx.report:
        return UNMET, f"{ctx.report_path.name}: no learning_signal.band and no saturated flag"
    return MET, (f"{ctx.report_path.name}: band = {band!r}, saturated = "
                 f"{ctx.report.get('saturated', 'not reported')!r}")


def _probe_tracked_metrics(ctx: TaskContext):
    if ctx.report is None:
        return _needs_run(ctx, "turns / tool calls / tokens / latency")
    if "tracked_metrics" in ctx.report:
        return MET, f"{ctx.report_path.name} carries tracked_metrics"
    partial = [k for k in ("tool_calls_median", "wall_clock_s_median") if k in ctx.report]
    return UNMET, (f"{ctx.report_path.name} carries {partial} only; no turns, "
                   f"tokens or time-to-first-action")


def _probe_group_signal(ctx: TaskContext):
    if ctx.report is None:
        return _needs_run(ctx, "group variance / advantage collapse")
    ls = ctx.report.get("learning_signal") or {}
    g = ls.get("group") if isinstance(ls, dict) else None
    if not isinstance(g, dict) or g.get("collapsed") is None:
        return UNMET, f"{ctx.report_path.name}: no learning_signal.group"
    p1 = ctx.report.get("pass_at_1")
    if g.get("collapsed") or p1 in (0, 0.0, 1, 1.0):
        return UNMET, (f"{ctx.report_path.name}: group collapsed = "
                       f"{g.get('collapsed')}, pass@1 = {p1}; the model sits at "
                       f"an extreme, so there is no gradient")
    return MET, (f"{ctx.report_path.name}: pass@1 = {p1}, p(1-p) = "
                 f"{g.get('p_one_minus_p')}, collapsed = {g.get('collapsed')}")


def _probe_class_balance(ctx: TaskContext):
    if ctx.report is None:
        return _needs_run(ctx, "class balance across arms")
    bal = ctx.report.get("balance")
    counts = (ctx.report.get("matrix") or {}).get("outcome_counts") \
        if isinstance(ctx.report.get("matrix"), dict) else None
    if isinstance(bal, dict):
        return MET, f"{ctx.report_path.name}: balance = {bal}"
    if counts:
        return UNMET, (f"{ctx.report_path.name}: outcome_counts = {counts} but no "
                       f"balance block (share of the correct class per arm)")
    return UNMET, f"{ctx.report_path.name}: no balance block and no outcome_counts"


# ------------------------------------------------------------ the table
def _shared(probe, with_id: str):
    def wrapped(ctx: TaskContext):
        status, detail = probe(ctx)
        return status, f"{detail} (same probe as {with_id})"
    return wrapped


REQUIREMENTS: dict[str, dict] = {
    "A1": {"text": "Grade the outcome — final environment state — not the path",
           "source": ANTHROPIC, "probe": _probe_outcome_not_path},
    "A2": {"text": "Reference solution that passes all graders proves solvability and grader config",
           "source": ANTHROPIC, "probe": _probe_reference_solution},
    "A3": {"text": "0% pass@many is a broken task; everything the grader checks is clear from the task text",
           "source": ANTHROPIC, "probe": _probe_preregistration},
    "A4": {"text": "Balanced problem sets — test where the behaviour should and should not occur",
           "source": ANTHROPIC, "probe": _probe_two_arms_with_roles},
    "A5": {"text": "Isolated clean environment per trial; no shared state",
           "source": ANTHROPIC, "probe": _probe_isolation},
    "A6": {"text": "Deterministic graders where possible",
           "source": ANTHROPIC, "probe": _probe_deterministic_grader},
    "A7": {"text": "Partial credit for multi-component tasks — represent the continuum",
           "source": ANTHROPIC, "probe": _probe_graded_score},
    "A8": {"text": "Graders resistant to bypasses",
           "source": ANTHROPIC, "probe": _probe_hack_resistance},
    "A9": {"text": "Multiple trials; report pass@k and pass^k",
           "source": ANTHROPIC, "probe": _probe_pass_at_k},
    "A10": {"text": "Read the transcripts; failures should seem fair",
            "source": ANTHROPIC, "probe": _probe_transcripts_read},
    "A11": {"text": "Monitor saturation; capability vs regression",
            "source": ANTHROPIC, "probe": _probe_saturation},
    "A12": {"text": "Tracked metrics: turns, tool calls, tokens, latency",
            "source": ANTHROPIC, "probe": _probe_tracked_metrics},
    "O1": {"text": "Task clear with a verifiable answer; grader scores with no human",
           "source": OPENAI, "probe": _shared(_probe_deterministic_grader, "A6")},
    "O2": {"text": "Model must already solve occasionally; not at min or max",
           "source": OPENAI, "probe": _probe_group_signal},
    "O3": {"text": "Remove doubt about the right answer — two careful people agree",
           "source": OPENAI, "probe": _probe_accepted_and_staging},
    "O4": {"text": "Limit lucky guesses",
           "source": OPENAI, "probe": _probe_no_lucky_guess},
    "O5": {"text": "Smooth score, not a pass/fail stamp",
           "source": OPENAI, "probe": _shared(_probe_graded_score, "A7")},
    "O6": {"text": "Guard against reward hacking",
           "source": OPENAI, "probe": _shared(_probe_hack_resistance, "A8")},
    "O7": {"text": "Avoid skewed label distribution",
           "source": OPENAI, "probe": _probe_class_balance},
    "O8": {"text": "Grader format the platform accepts: Python grade(sample, item) -> float, or multi with calculate_output",
           "source": OPENAI, "probe": _probe_rft_grader_export},
}

# The rows that can only be answered from a run. Kept as data so a test can
# assert "every run-dependent row is needs_run without stored runs" without
# re-deriving the list from the probes.
RUN_DEPENDENT: tuple[str, ...] = ("A9", "A10", "A11", "A12", "O2", "O7")


def _order(rid: str) -> tuple[int, int]:
    return (0 if rid[0] == "A" else 1, int(rid[1:]))


def evaluate(task_id: str, tasks_dir: Path, stored_runs=None) -> dict[str, dict]:
    """id -> {"status", "detail", "text", "source"} for every requirement."""
    ctx = load_context(task_id, Path(tasks_dir), stored_runs)
    out: dict[str, dict] = {}
    for rid in sorted(REQUIREMENTS, key=_order):
        req = REQUIREMENTS[rid]
        try:
            status, detail = req["probe"](ctx)
        except Exception as exc:  # a probe that crashes is a row, not a crash
            status, detail = UNMET, f"probe raised {type(exc).__name__}: {exc}"
        if status not in STATUSES:
            raise ValueError(f"{rid}: probe returned status {status!r}")
        out[rid] = {"status": status, "detail": str(detail),
                    "text": req["text"], "source": req["source"]}
    return out


def render(task_id: str, tasks_dir: Path, stored_runs=None) -> str:
    rows = evaluate(task_id, tasks_dir, stored_runs)
    lines = ["| id | requirement | status | detail |", "|---|---|---|---|"]
    for rid, row in rows.items():
        detail = row["detail"].replace("|", "\\|").replace("\n", " ")
        text = row["text"].replace("|", "\\|")
        lines.append(f"| {rid} | {text} | {row['status']} | {detail} |")
    counts = {s: sum(1 for r in rows.values() if r["status"] == s) for s in STATUSES}
    lines += ["", f"{task_id}: {counts[MET]} met, {counts[UNMET]} unmet, "
                  f"{counts[NEEDS_RUN]} need a run. Sources: A = {ANTHROPIC}; "
                  f"O = {OPENAI}"]
    return "\n".join(lines) + "\n"
