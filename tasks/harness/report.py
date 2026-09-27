"""Aggregate rollout records into the per-task difficulty report.

The number on its own is not the deliverable. Pass@1 without the failure
breakdown, the quarantine count and the per-check pass rates is a story; with
them it is a measurement. So the report refuses to print a bare pass rate:

  * the DENOMINATOR is stated every time, and there are three of them
  * quarantined rollouts are counted and excluded, never silently dropped
  * per-check pass rates say which sub-checks models actually cleared, which is
    what tells us how to harden the task

RUN CONDITIONS, and why they are above the number rather than below it. GT004's
report printed `dead surfaces in this world: [alarms, chat, dbt, deploy, ingest,
issues, kb, metrics]` - eight of the agent's surfaces absent - at the BOTTOM,
under a Pass@1 that read like any other Pass@1. A pass rate whose world is not
described is not a result, and describing it after the reader has taken the
number is the same as not describing it. So the conditions block comes first and
the rate carries its live-surface set inline, and when the conditions are bad
enough the rate is WITHHELD rather than printed with a caveat.

The CONTEXT VARIANT is the first line of that block. A task directory is one
context; its `[variants]` are ablations of it, and GT004's five context arms
span Pass@1 0.0 to 0.2 on one world with four YAML files changed. A rate whose
arm is not stated is the same defect as a rate whose dead surfaces are not
stated, so a run that mixes two arms is INVALIDATED here rather than averaged.

The prompt variant belongs in the same block for the same reason
(`GT004/prompts/variant.py identify` already establishes that a run must name
which ticket produced it): world and ticket are the two halves of "what was this
measured in".

PASS^K. Pass@1 says how often the model gets it right. Pass^k says whether it
gets it right EVERY time, which is the property a product needs and the one that
has already discriminated here: on GT001 Claude scored Pass@1 1.0 and Pass^5
1.0, while Kimi scored Pass@1 0.5-0.75 and Pass^k 0. It costs nothing - the
world is byte-identical across K by construction - so the only reason it was
missing is that nobody computed it.

THE HEADLINE (docs/research/Scoring_Redesign.md). When the task declares a [matrix]
with an outcome table, the report's headline is APEX-Agents style and nothing
is gated - every graded episode is in the denominator:

  * `pass` per episode: the outcome class is in the arm's TOP BAND
    (`resolved`, plus `withheld_with_evidence` on a decision arm), i.e.
    reward.band_for(outcome, role) == 1.0. `pass@1` is its rate over the
    graded episodes, with the Wilson interval and the unbiased pass@k /
    pass^k table beside it. An `unmeasured` episode (none of the declared
    checks on record) has no class and leaves the denominator, exactly as
    quarantine does - the only exclusion left.
  * `score` per episode, "Mean Score" aggregated: the fraction of MUST-HAVE
    criteria met - the conditions the top-band classes list - taken as the
    max over those classes (outcome.episode_score). A resolved episode
    scores 1.0 by construction; a false_claim episode still gets its score,
    because "lied, but 80 % of the work was there" is informative.
  * `criteria`: for every check id on record in the run, {n, passed, rate}
    plus `must_have` (is it a top-band condition on this arm). This is the
    "why did it fail" view.

The grader's own all-checks bit stays on the report as `pass_all_required`,
BELOW the headline: it remains the strict criterion and a fact about the run,
not the number the run presents. A task without a [matrix] renders as it
always did: no spec, no block.

THE INTERVAL, THE ESTIMATOR TABLE AND THE LEARNING SIGNAL (docs/research/Scoring_Redesign.md).
K=5 cannot separate 0.8 from 1.0, and a rate printed without its interval
invites the reader to do exactly that, so the Wilson interval and n print on
the SAME LINE as the rate, every time. Beside the run-level Pass^k bit (did
every scored rollout pass) the report carries the unbiased pass@k / pass^k
estimators for k <= n; they answer a different question and are labelled
`(unbiased est.)` so nobody reads one as the other. The `## Learning signal`
section prints what a trainer's filter reads - group spread p(1-p), whether
the group collapsed, the difficulty band with the rule that placed it - and
obeys the same rule as the rate: when the run is not a clean measurement,
every line is marked provisional rather than dropped. The matrix block also
carries each episode's outcome class and the DECLARED training scalar, and
the scalar is labelled as the thing a trainer is handed, never as the
measurement. The layer modules are loaded optionally, so one that will not
import or still raises NotImplementedError costs the report that block and a
caveat, not the report.

THE THREE TIERS (DECISION_TEMPLATE.md §9, docs/research/Scoring_Redesign.md). The
declared scalar is band + progress-in-band + path penalty, and each tier is
another module's: the band is reward.py's table over outcome.py's class;
progress is progress.py's normalisation of the grader's product quantities
against the anchors a baselines JSON carries (`aggregate(...,
baselines_json=)`; without one, progress is None and the caveat says so);
the path penalty is path.py's four rules over the episode's own audit rows
(`rec["actions"]`, or the actions.jsonl the record names; none on record
means 0.0 and an info caveat). The matrix block carries every per-episode
tier, the tier means with their denominators, whether the arm is SATURATED
(pass@1 >= 0.9 or <= 0.1 gives a group-relative trainer no signal), the
group signal over the declared scalar, progress reachability (a quantity
every episode maxes or zeroes is a bit in disguise) and the path-event counts
by rule. Beside the scalar, never in it, each row carries tracked_metrics.py's
counts.
"""
from __future__ import annotations

import importlib
import json
import statistics
from pathlib import Path

import tracked_metrics

# The three learning-signal modules are contracts first and code second: a
# stub that raises NotImplementedError, or one that will not even import yet,
# must cost the report its new block and nothing else. So each is loaded as
# "the module, or the reason it is not one", and every call goes through
# _call(), which turns a refusal into a run-conditions caveat. Tests replace
# the module attributes below with deterministic fakes.
_IMPORT_FAILURES: dict[str, str] = {}


def _optional_import(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - the reason is the caveat
        _IMPORT_FAILURES[name] = f"{type(exc).__name__}: {exc}"
        return None


learning_signal = _optional_import("learning_signal")
reward = _optional_import("reward")
outcome = _optional_import("outcome")
progress = _optional_import("progress")
path = _optional_import("path")


def _call(module, module_name: str, fn: str, caveats: list[str] | None,
          *args, **kw):
    """module.fn(*args, **kw), or None plus a caveat saying why not.

    Only an unfilled interface degrades: a module that did not import, a
    function the interface does not have, or a body that raises
    NotImplementedError. Anything else is a real defect and is allowed to
    crash the report, because a number computed over a bug is worse than no
    number.
    """
    if module is None:
        note = (f"{module_name}.{fn} unavailable: the module did not import "
                f"({_IMPORT_FAILURES.get(module_name, 'unknown')}); its block "
                f"reads unavailable")
    elif getattr(module, fn, None) is None:
        note = (f"{module_name}.{fn} unavailable: the module has no such "
                f"function; its block reads unavailable")
    else:
        try:
            return getattr(module, fn)(*args, **kw)
        except NotImplementedError:
            note = (f"{module_name}.{fn} is not implemented yet; its block "
                    f"reads unavailable")
    if caveats is not None and note not in caveats:
        caveats.append(note)
    return None


def _ci_list(ci) -> list[float] | None:
    """A [lo, hi] pair rounded for the report, or None."""
    if ci is None:
        return None
    lo, hi = ci
    return [round(float(lo), 3), round(float(hi), 3)]


def _fmt_ci(ci) -> str:
    return "[95% CI unavailable]" if ci is None else f"[{ci[0]:.2f}, {ci[1]:.2f}]"


def _k_table(table: dict | None, n: int) -> dict | None:
    """pass_at_k_table's output restricted to k <= n, keys as strings.

    Strings because report.json round-trips through JSON and a table whose
    keys change type on the way back would render differently from the one
    that was written. k > n is not "0", it is undefined, so those ks leave.
    """
    if table is None:
        return None
    out = {}
    for name in ("pass_at_k", "pass_hat_k"):
        rows = table.get(name) or {}
        out[name] = {str(k): (None if v is None else round(float(v), 3))
                     for k, v in sorted(rows.items(), key=lambda kv: int(kv[0]))
                     if int(k) <= n}
    return out


CLASSES = ("pass", "reasoning", "tool_gap", "timeout", "budget",
           "grader_defect", "harness_error", "quota_exhausted",
           "blindness_violation", "unobserved")


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 2) if xs else None


def _pass_hat_k(records: list[dict]) -> dict:
    """Did the model pass ALL of them?

    Over SCORED rollouts, and k is stated, because the alternative definitions
    are both wrong. Counting a quarantined rollout as a failure blames the model
    for the harness; counting it as a pass is obvious nonsense. So k shrinks, and
    the report says when k < K - a Pass^3 of a K=5 run is a weaker claim than the
    run set out to make, and the reader has to be able to see that.
    """
    scored = [r for r in records if r.get("scored")]
    passed = [r for r in scored if r.get("passed")]
    return {
        "pass_hat_k": (None if not scored
                       else (1.0 if len(passed) == len(scored) else 0.0)),
        "pass_hat_k_k": len(scored),
        "pass_hat_k_attempted": len(records),
        "pass_hat_k_complete": bool(scored) and len(scored) == len(records),
        "pass_hat_k_passed": len(passed),
    }


def _run_conditions(records: list[dict]) -> dict:
    """What this run was measured IN. Everything here can invalidate the rate."""
    audits = [r.get("surface_audit") for r in records]
    have = [a for a in audits if a]
    missing = len(records) - len(have)

    def union(key):
        return sorted({x for a in have for x in (a.get(key) or ())})

    # The world is byte-identical across K by construction, so the live set
    # should be too. If it is not, that is a determinism defect and it is louder
    # than anything the surfaces themselves say.
    live_sets = {tuple(a.get("live") or ()) for a in have}
    variants = sorted({r.get("prompt_variant") for r in records
                       if r.get("prompt_variant")})
    arms = sorted({r.get("variant") for r in records if r.get("variant")})
    prompt_hashes = sorted({r.get("prompt_sha256") for r in records
                            if r.get("prompt_sha256")})
    gates = sorted({a.get("gate") for a in have if a.get("gate")})

    # WAS THIS RUN RE-GRADED? A run whose records were produced by
    # `rollout.py --regrade` is exactly as good as the episodes it re-read and
    # no better, and a reader has to be told which it is: the grader that
    # produced these numbers is not the grader that was on disk when the
    # episodes ran. The pieces a partial regrade could not recover are named
    # here rather than left in the per-rollout records, for the same reason the
    # dead-surface set is: a condition nobody can see is not a condition.
    regraded = [r for r in records if r.get("regraded_at")]
    partial = sorted(r.get("rollout_id") for r in regraded
                     if r.get("regrade_partial"))
    missing_pieces = sorted({m.get("piece") for r in regraded
                             for m in ((r.get("regrade") or {}).get("missing") or ())
                             if m.get("piece")})

    conditions = {
        "regraded": bool(regraded),
        "regraded_rollouts": len(regraded),
        "regraded_partial": partial,
        "regraded_missing": missing_pieces,
        "regraded_grader_sha256": sorted({r.get("regrade_grader_sha256")
                                          for r in regraded
                                          if r.get("regrade_grader_sha256")}),
        # "" when the task declares no [variants] at all, which is a different
        # statement from "this run used the default one".
        "variants": arms,
        "prompt_variants": variants,
        "prompt_sha256": prompt_hashes,
        "surface_gate": gates,
        "surfaces_live": union("live"),
        "surfaces_dead": union("dead"),
        "surfaces_suspect": union("suspect"),
        "surfaces_dead_required": union("dead_required"),
        "surfaces_dead_by_design": union("dead_by_design"),
        "surfaces_dead_undeclared": union("dead_undeclared"),
        "surfaces_dead_by_design_but_live": union("dead_by_design_but_live"),
        "surfaces_required": union("required_labels"),
        "surfaces_unreachable_by_sweep": union("unreachable_by_sweep"),
        "rollouts_without_surface_audit": missing,
        "live_set_identical_across_k": len(live_sets) <= 1,
        "declared_probes": max([a.get("declared_probes", 0) for a in have] or [0]),
        "swept_probes": max([a.get("swept_probes", 0) for a in have] or [0]),
    }

    # WHY THE RATE MAY NOT BE PUBLISHED. Each entry is a fact about the run, not
    # a judgement about the model.
    bad: list[str] = []
    if any(r.get("redteam_detector_test") for r in records):
        bad.append("the run contains red-team DETECTOR TESTS, which are not "
                   "measurements of anything")
    if conditions["surfaces_dead_required"]:
        bad.append(f"required surfaces were DEAD: "
                   f"{conditions['surfaces_dead_required']}. Those rollouts were "
                   f"never given the world the task describes.")
    if conditions["surfaces_dead_undeclared"]:
        bad.append(f"surfaces were dead that the task neither requires nor "
                   f"declares dead by design: "
                   f"{conditions['surfaces_dead_undeclared']}. Until the task "
                   f"says which, a failure here is unattributable.")
    if "unprobed" in gates:
        bad.append("the task declares no surface_probes, so no surface was "
                   "verified live before the subject was handed the terminal")
    if missing:
        bad.append(f"{missing} rollout(s) carry no surface audit at all, so the "
                   f"world they ran in is undescribed")
    if not conditions["live_set_identical_across_k"]:
        bad.append("the live-surface set DIFFERS between rollouts of a run whose "
                   "world is supposed to be byte-identical across K")
    if len(arms) > 1:
        bad.append(f"the run mixes CONTEXT VARIANTS ({arms}); the arms are "
                   f"different worlds and one rate over two of them is not a "
                   f"rate for either")
    if len(variants) > 1 or len(prompt_hashes) > 1:
        bad.append(f"the run mixes prompts ({variants or prompt_hashes}); one "
                   f"rate over two tickets is not a rate for either")
    if regraded and len(regraded) != len(records):
        bad.append(f"{len(regraded)} of {len(records)} rollouts were re-graded "
                   f"and the rest were not, so this rate is taken over two "
                   f"graders at once")
    if len(conditions["regraded_grader_sha256"]) > 1:
        bad.append(f"the re-graded rollouts were scored by DIFFERENT graders "
                   f"({conditions['regraded_grader_sha256']}); one rate over two "
                   f"rubrics is not a rate for either")
    if any(v.startswith("UNLOCKED") for v in variants):
        bad.append(f"the brief matches no locked prompt variant ({variants}), so "
                   f"this run cannot be attributed to a ticket")
    if any(r.get("driver") == "native"
           and r.get("topology", "host") == "host"
           for r in records):
        bad.append(
            "host_native_not_contained: --driver native --topology host is not "
            "measurement_valid; the subject's bash is a host process"
        )

    # NOT an invalidation, but it must be said: a task that declares no
    # required_surfaces cannot fail the gate, so "gate: pass" is unavailable to
    # it and the absence of a dead-required finding means nothing.
    caveats = []
    if regraded:
        caveats.append(
            f"every number here was produced by `rollout.py --regrade` over "
            f"episodes that had already run: the subject was not re-run and the "
            f"world was not rebuilt, and the grader that scored them is "
            f"{', '.join(s[:12] for s in conditions['regraded_grader_sha256']) or 'unrecorded'}")
    if partial:
        caveats.append(
            f"{len(partial)} rollout(s) re-graded PARTIALLY ({partial}): "
            f"{missing_pieces} were never written to disk by the run that "
            f"produced them, so those stages could not be re-run from evidence. "
            f"Each rollout's `regrade.missing` says what that cost it.")
    if "not_declared" in gates:
        caveats.append("this task declares no [harness].required_surfaces, so "
                       "the surface gate had nothing to enforce")
    if conditions["surfaces_dead_by_design_but_live"]:
        caveats.append(f"dead_by_design names surfaces that are actually live "
                       f"({conditions['surfaces_dead_by_design_but_live']}); the "
                       f"declaration is stale")
    if conditions["surfaces_suspect"]:
        caveats.append(f"surfaces answered an absence-shaped error this harness "
                       f"does not know: {conditions['surfaces_suspect']}")
    conditions["caveats"] = caveats
    conditions["measurement_valid"] = not bad
    conditions["invalidated_because"] = bad
    return conditions


def _surface_tag(c: dict) -> str:
    """The live-surface set, short enough to sit at the end of every rate."""
    if c.get("rollouts_without_surface_audit") and not c["surfaces_live"]:
        return "surfaces: NOT AUDITED"
    n_live, n_dead = len(c["surfaces_live"]), len(c["surfaces_dead"])
    # named only while naming them stays readable; the full lists are in the
    # conditions block a few lines above, and a tag repeated on every rate line
    # has to be short enough that it is still read on the third one
    dead = c["surfaces_dead"]
    tail = f" ({', '.join(dead)})" if 0 < n_dead <= 4 else ""
    return f"surfaces: {n_live} live / {n_dead} dead{tail}"


#: The conventions `_path_allowlist` falls back on when the task declares no
#: `path_allow` of its own (DECISION_TEMPLATE.md §9, path.py's tests): the
#: platform tree no subject may write, and the grading surface no subject may
#: edit. Consumer-owned objects are the task's to name.
PATH_PLATFORM_PREFIX = ".platform/"
PATH_GRADING_PREFIXES = ("tests/", "grading/", "var/ledger/receipts.jsonl")


def _path_allowlist(spec) -> dict:
    """The `allow` dict path.episode_path_penalty judges an episode against.

    Read off the task first: a TaskSpec that carries `path_allow` has said
    exactly which writes are in-mandate, and nothing here second-guesses it.
    Otherwise it is built from the conventions every GT world shares -
    `.platform/` is the platform tree, `tests/` / `grading/` / the receipts
    ledger are the grading surface - plus whatever consumer-owned objects the
    arm's `[matrix.path]` lists (`consumer_objects`), or none. One helper so
    the integrator can move the construction into taskspec later without
    touching the call site.
    """
    declared = getattr(spec, "path_allow", None) if spec is not None else None
    if isinstance(declared, dict):
        return dict(declared)
    matrix = getattr(spec, "matrix", None) if spec is not None else None
    # the arm's own [matrix.path_allow] (taskspec) overrides key by key; the
    # two prefixes default to the shared world conventions
    arm_allow = getattr(matrix, "path_allow", None) if matrix is not None else None
    arm_allow = dict(arm_allow) if isinstance(arm_allow, dict) else {}
    path_spec = getattr(matrix, "path", None) if matrix is not None else None
    consumers = arm_allow.get("consumer_objects")
    if consumers is None:
        consumers = getattr(path_spec, "consumer_objects", None)
    if consumers is None and isinstance(path_spec, dict):
        consumers = path_spec.get("consumer_objects")
    return {
        "platform_prefix": arm_allow.get("platform_prefix", PATH_PLATFORM_PREFIX),
        "grading_prefixes": list(arm_allow.get("grading_prefixes",
                                               PATH_GRADING_PREFIXES)),
        "consumer_objects": sorted(str(c) for c in (consumers or ())),
    }


def _read_jsonl(path_like) -> list[dict] | None:
    """Rows of a .jsonl file, or None when there is no such file."""
    if not path_like:
        return None
    p = Path(path_like)
    if not p.is_file():
        return None
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _actions_for(rec: dict) -> list[dict] | None:
    """The SUBJECT's audit rows for one record, or None when none is on record.

    `rec["actions"]` when the record carries its rows; else the actions.jsonl
    the record names (`audit_path`, `action_log.path`,
    `<rollout_dir>/audit/actions.jsonl`), read back off disk. Rows at or
    below the record's `action_log_watermark` were written before the
    subject was handed the terminal (rollout.py) and are dropped; the
    harness's post-episode `workspace.edit` bookkeeping rows are KEPT,
    because they are the only row a native subject's file write leaves, and
    a write under `.platform/` is the subject's whatever wrote the row.
    """
    rows = rec.get("actions")
    if rows is None:
        rollout_dir = rec.get("rollout_dir")
        log = rec.get("action_log") if isinstance(rec.get("action_log"), dict) else {}
        for cand in (rec.get("audit_path"), log.get("path"),
                     Path(rollout_dir) / "audit" / "actions.jsonl" if rollout_dir else None):
            rows = _read_jsonl(cand)
            if rows is not None:
                break
    if rows is None:
        return None
    # the watermark excludes rows the WORLD BUILDER wrote (seq <= high). A
    # row with no seq at all (a hand-built log) is the agent's; a real row
    # always carries one (R1 info: `0 > 0` dropped seq-less rows even at
    # watermark 0).
    high = int(rec.get("action_log_watermark") or 0)
    return [r for r in rows if isinstance(r, dict)
            and (r.get("seq") is None or int(r.get("seq") or 0) > high)]


def _load_baselines(baselines_json, caveats: list[str] | None) -> dict | None:
    """The baselines JSON (`run_baselines.py --json`) as a dict, or None
    plus the caveat that says why progress has no anchors."""
    if baselines_json is None:
        note = "progress anchors unavailable: no baselines JSON given"
    else:
        try:
            data = json.loads(Path(baselines_json).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            note = (f"progress anchors unavailable: baselines JSON "
                    f"{Path(baselines_json).name} is not an object")
        except (OSError, ValueError) as exc:
            note = (f"progress anchors unavailable: baselines JSON "
                    f"{Path(baselines_json).name} could not be read "
                    f"({type(exc).__name__})")
    if caveats is not None and note not in caveats:
        caveats.append(note)
    return None


def _mean(xs: list[float], nd: int = 3) -> float | None:
    return round(sum(xs) / len(xs), nd) if xs else None


def _pvariance(xs: list[float], nd: int = 4) -> float | None:
    return round(statistics.pvariance(xs), nd) if xs else None


def _matrix_block(spec, records: list[dict],
                  caveats: list[str] | None = None,
                  baselines_json=None) -> dict | None:
    """The headline block over this run's SCORED rollouts, or None.

    None means "not a matrix task" (no spec, or a spec without a matrix) and
    the report is byte-identical to the pre-matrix one. Otherwise every
    scored record - the same inclusion rule the grader's own pass uses -
    gets a per_rollout row with:

      * `outcome`  - outcome.classify_outcome's dict (class + evidence);
      * `pass`     - outcome.episode_pass: is the class in the arm's top
                     band. None for an unmeasured episode;
      * `score`    - outcome.episode_score: the fraction of must-have
                     criteria met, max over the top-band classes, with the
                     conditions met and missed. None when no table;
      * the three scalar tiers `band`, `progress`, `path_penalty` (with
        `path_events`), the DECLARED TRAINING SCALAR that combines them
        (`reward`, the full reward.episode_reward dict) and its
        `tracked_metrics`. The scalar is what a trainer is handed; it is
        kept out of the headline and labelled for what it is.

    Aggregated: `pass` {n, passed, rate, ci95, pass_at_k, pass_hat_k} over
    the GRADED episodes (scored, with a class other than unmeasured);
    `mean_score` {n, mean, per_episode}; `criteria` {check id -> {n, passed,
    rate, must_have, required_by, condition}} for every check id any graded
    episode carries, where `passed` counts the criterion MET (`condition`
    `!X`, a check the table names only negated: X did not charge; `X`: X
    passed) and `must_have` says whether a top-band class requires it;
    `outcome_counts` in precedence order.

    Progress needs anchors, and anchors come from `baselines_json`
    (progress.anchors_from_baselines); path needs the episode's audit rows
    and the task's allowlist (_path_allowlist). Either tier the arm does not
    declare (`matrix.progress` / `matrix.path` is None) is not attempted:
    progress stays None and the path penalty 0.0 with no events.
    """
    matrix = getattr(spec, "matrix", None) if spec is not None else None
    if matrix is None:
        return None
    scored = [r for r in records if r.get("scored")]
    per_rollout: dict = {r.get("rollout_id"): {} for r in scored}

    # Outcome class and reward per episode. The outcome needs a declaration
    # the task may not carry (MatrixSpec.outcome is optional); the reward is
    # always attempted. Either module still being a stub leaves None here
    # and a caveat above the number.
    outcome_spec = getattr(matrix, "outcome", None)
    progress_spec = getattr(matrix, "progress", None)
    path_spec = getattr(matrix, "path", None)
    role = getattr(matrix, "role", None) or "control"
    must_have = _call(outcome, "outcome", "must_have_conditions", caveats,
                      outcome_spec, role) or {}
    top_band = list(_call(outcome, "outcome", "top_band_classes", caveats,
                          role) or (["resolved"] + (["withheld_with_evidence"]
                                                    if role == "decision"
                                                    else [])))

    # The anchors progress is normalised against, once per run: the no-op
    # and oracle values of each declared quantity, read off the baselines
    # JSON the task's own battery wrote. No JSON, no anchors, no progress -
    # and the caveat says which of the three is missing.
    anchors = None
    if progress_spec is not None:
        baselines = _load_baselines(baselines_json, caveats)
        if baselines is not None:
            try:
                anchors = _call(progress, "progress", "anchors_from_baselines",
                                caveats, progress_spec, baselines)
            except ValueError as exc:
                # progress.py refuses a baselines file that lacks a quantity
                # it needs (an anchor nobody measured). That is a fact about
                # the run's inputs, not a defect: progress is n/a and the
                # caveat names the missing anchor.
                note = f"progress anchors unavailable: {exc}"
                if caveats is not None and note not in caveats:
                    caveats.append(note)
                anchors = None
    allow = _path_allowlist(spec) if path_spec is not None else None

    rewards: list[float] = []
    bands: list[float] = []
    progresses: list[float] = []
    paid_progresses: list[float] = []
    penalties: list[float] = []
    outcome_counts: dict[str, int] = {}
    event_counts: dict[str, int] = {}
    no_audit: list[str] = []
    passes: list[bool] = []
    scores: dict = {}
    criteria: dict[str, dict] = {}
    for r in scored:
        rid = r.get("rollout_id")
        row = per_rollout[rid]
        checks = r.get("checks") or {}
        cls = None
        ep_pass = ep_score = None
        if outcome_spec is not None:
            cls = _call(outcome, "outcome", "classify_outcome", caveats,
                        outcome_spec, checks, r.get("artifact"))
            if cls is not None:
                outcome_counts[cls["outcome"]] = \
                    outcome_counts.get(cls["outcome"], 0) + 1
                ep_pass = _call(outcome, "outcome", "episode_pass", caveats,
                                cls["outcome"], role)
            ep_score = _call(outcome, "outcome", "episode_score", caveats,
                             outcome_spec, checks, role)
        graded = cls is not None and cls["outcome"] != "unmeasured"
        if not graded:
            # an unmeasured episode has no class and therefore no score:
            # it leaves every headline denominator, it is not a 0
            ep_pass = ep_score = None
        if graded and ep_pass is not None:
            passes.append(bool(ep_pass))
        if graded and ep_score is not None:
            scores[rid] = ep_score["score"]
        if graded:
            # every check on record, counted as a CRITERION MET: a check
            # the outcome table names only negated (`!H1`, a penalty or
            # violation) is met when it did not charge; any other check is
            # met when it passed
            for cid, c in checks.items():
                if not isinstance(c, dict) or "passed" not in c:
                    continue
                cond = _criterion_form(outcome_spec, cid)
                e = criteria.setdefault(cid, {"n": 0, "passed": 0,
                                              "condition": cond})
                e["n"] += 1
                held = _call(outcome, "outcome", "condition_holds", caveats,
                             cond, checks)
                met = (not held) if cond.startswith("!") else bool(held)
                e["passed"] += 1 if met else 0

        # Tier 2: progress-in-band, only when the arm declares quantities
        # and the run has anchors for them.
        prog = None
        if progress_spec is not None and anchors is not None:
            prog = _call(progress, "progress", "episode_progress", caveats,
                         progress_spec, checks, anchors)
            if prog is not None:
                prog = float(prog)

        # Tier 3: path penalty over the subject's own audit rows.
        penalty, events = 0.0, []
        if path_spec is not None:
            actions = _actions_for(r)
            if actions is None:
                no_audit.append(str(rid))
            else:
                pp = _call(path, "path", "episode_path_penalty", caveats,
                           path_spec, actions, allow)
                if pp is not None:
                    penalty = float(pp.get("penalty") or 0.0)
                    events = list(pp.get("events") or [])
        for ev in events:
            rule = (ev or {}).get("rule") or "unknown"
            event_counts[rule] = event_counts.get(rule, 0) + 1

        # reward.py's three-tier scalar (DECISION_TEMPLATE §9): outcome class,
        # arm role, progress-in-band, path penalty.
        rw = _call(reward, "reward", "episode_reward", caveats,
                   cls["outcome"] if cls is not None else None,
                   role, prog, penalty)
        comps = (rw or {}).get("components") or {}
        if rw is not None and rw.get("reward") is not None:
            rewards.append(float(rw["reward"]))
            if comps.get("band") is not None:
                bands.append(float(comps["band"]))
            penalties.append(penalty)
        if prog is not None:
            progresses.append(prog)
            # the PAID set: progress only reaches the scalar in a partial
            # class (reward.py). R4 2026-09-19: GT009 read "2 distinct" over
            # all episodes while the paid set was 1 distinct - the RLVP
            # question is about the paid set.
            if comps.get("progress") is not None:
                paid_progresses.append(prog)
        row["outcome"] = cls
        row["pass"] = ep_pass
        row["score"] = ep_score
        row["band"] = comps.get("band")
        row["progress"] = prog
        row["path_penalty"] = penalty
        row["path_events"] = events
        row["reward"] = rw
        row["tracked_metrics"] = tracked_metrics.from_record(r)
    if no_audit and caveats is not None:
        note = (f"info: no audit log on record for {no_audit}; their path "
                f"penalty is taken as 0.0")
        if note not in caveats:
            caveats.append(note)

    # precedence order first (outcome.OUTCOMES), so the counts line reads the
    # same way the classifier decided; anything unknown sorts after
    order = list(getattr(outcome, "OUTCOMES", ()) or ())
    outcome_counts = {k: outcome_counts[k]
                      for k in sorted(outcome_counts,
                                      key=lambda k: (k not in order,
                                                     order.index(k) if k in order else 0,
                                                     k))}

    # THE HEADLINE. pass@1 over the graded episodes, with its interval and
    # the unbiased k-table beside it. SATURATED when the rate is >= 0.9 or
    # <= 0.1: nearly every episode lands in the top band or none does, and a
    # group-relative trainer reads that as no gradient.
    n_graded, n_pass = len(passes), sum(1 for p in passes if p)
    rate = round(n_pass / n_graded, 3) if n_graded else None
    k_table = _k_table(_call(learning_signal, "learning_signal",
                             "pass_at_k_table", caveats,
                             n=n_graded, c=n_pass),
                       n_graded) if n_graded else None
    headline = {
        "n": n_graded,
        "passed": n_pass,
        "rate": rate,
        "ci95": _ci_list(
            _call(learning_signal, "learning_signal", "wilson_interval",
                  caveats, n_pass, n_graded) if n_graded else None),
        "pass_at_k": (k_table or {}).get("pass_at_k"),
        "pass_hat_k": (k_table or {}).get("pass_hat_k"),
        "top_band": top_band,
        "denominator": "graded rollouts: scored, with an outcome class other "
                       "than unmeasured. pass = the class is in the arm's "
                       "top band (reward band 1.0). pass_at_k / pass_hat_k "
                       "are the unbiased estimators over these n, sampling "
                       "k without replacement; k > n is not listed.",
    }
    saturated = rate is not None and (rate >= 0.9 or rate <= 0.1)
    mean_score = {
        "n": len(scores),
        "mean": _mean(list(scores.values())),
        "per_episode": {str(k): v for k, v in scores.items()},
        "denominator": "graded rollouts with a score: the fraction of "
                       "must-have criteria met, max over the top-band "
                       "classes' condition lists",
    }
    for cid, e in criteria.items():
        e["rate"] = round(e["passed"] / e["n"], 3) if e["n"] else None
        e["must_have"] = cid in must_have
        e["required_by"] = list(must_have.get(cid, {}).get("required_by", []))
    criteria = dict(sorted(criteria.items(), key=_criteria_order))

    reachability = None
    if progresses:
        reachability = _call(progress, "progress", "reachability", caveats,
                             list(progresses))
    paid_reachability = None
    if paid_progresses:
        paid_reachability = _call(progress, "progress", "reachability", caveats,
                                  list(paid_progresses))
    return {
        "role": role,
        "scored": len(scored),
        "graded": n_graded,
        "unmeasured": outcome_counts.get("unmeasured", 0),
        "pass": headline,
        "mean_score": mean_score,
        "criteria": criteria,
        "criteria_denominator": "graded rollouts that carry the check; "
                                "`passed` counts the criterion MET: for a "
                                "check the outcome table names only negated "
                                "(`condition` = `!X`, a penalty/violation) "
                                "that is X did not charge, otherwise X passed",
        "must_have": {cid: dict(v) for cid, v in must_have.items()},
        "outcome_declared": outcome_spec is not None,
        "outcome_counts": outcome_counts,
        "outcome_counts_denominator": "scored rollouts with an outcome class",
        "saturated": saturated,
        "reward_progress_span": getattr(reward, "PROGRESS_SPAN", None),
        "reward_mean": _mean(rewards),
        "reward_var": _pvariance(rewards),
        "reward_denominator": len(rewards),
        "reward_note": "the DECLARED training scalar (reward.py), what a "
                       "trainer is handed per episode; not the measurement, "
                       "which is pass@1 / mean score above it",
        # the tiers, each over its own denominator: band and path over the
        # episodes that have a scalar, progress over the episodes that have
        # a progress value (an arm without quantities has none)
        "band_mean": _mean(bands),
        "progress_mean": _mean(progresses),
        "progress_denominator": len(progresses),
        "progress_declared": progress_spec is not None,
        "progress_anchored": anchors is not None,
        "progress_reachability": reachability,
        # over the episodes whose progress REACHED the scalar (partial
        # classes only); the trainer's gradient question is about this set
        "progress_reachability_paid": paid_reachability,
        "progress_paid_denominator": len(paid_progresses),
        "path_mean": _mean(penalties),
        "path_declared": path_spec is not None,
        "path_event_counts": dict(sorted(event_counts.items())),
        "path_allow": allow,
        "rollouts_without_audit_log": no_audit,
        "per_rollout": per_rollout,
    }


def _criterion_form(outcome_spec, cid: str) -> str:
    """How the criteria table reads check `cid`: `!cid` when the outcome
    table names it ONLY negated (a penalty or violation check: the criterion
    is "did not charge"), else `cid` (the criterion is "passed"). A check the
    table names both ways, or not at all, reads positively."""
    negated = positive = False
    if outcome_spec is not None and hasattr(outcome_spec, "map"):
        for conds in outcome_spec.map.values():
            for cond in conds:
                if cond == f"!{cid}":
                    negated = True
                elif cond == cid:
                    positive = True
    return f"!{cid}" if negated and not positive else cid


def _criteria_order(item) -> tuple:
    """must-have first, then by rate ascending (the failing criteria float to
    the top of the 'why did it fail' view), then by id."""
    cid, e = item
    rate = e.get("rate")
    return (not e.get("must_have"), rate is None, rate if rate is not None else 0.0, cid)


def aggregate(records: list[dict], spec=None, baselines_json=None) -> dict:
    """The run summary. `baselines_json` (a `run_baselines.py --json` file,
    str or Path) anchors the progress tier; without it progress is None and
    the run-conditions caveats say so."""
    by_class = {c: 0 for c in CLASSES}
    for r in records:
        by_class[r.get("classification", "harness_error")] = \
            by_class.get(r.get("classification", "harness_error"), 0) + 1

    scored = [r for r in records if r.get("scored")]
    passed = [r for r in scored if r.get("passed")]
    quarantined = [r for r in records
                   if r.get("classification") in ("blindness_violation", "unobserved")]
    caught = [r for r in records if r.get("classification") == "blindness_violation"]
    unobserved = [r for r in records if r.get("classification") == "unobserved"]
    excluded = [r for r in records if not r.get("scored")]

    # per-check pass rates, over scored rollouts only
    checks: dict[str, dict] = {}
    for r in scored:
        for cid, c in (r.get("checks") or {}).items():
            e = checks.setdefault(cid, {"max": c["max"], "passed": 0, "n": 0,
                                        "points": []})
            e["n"] += 1
            e["passed"] += 1 if c["passed"] else 0
            e["points"].append(c["points"])
    for cid, e in checks.items():
        e["pass_rate"] = round(e["passed"] / e["n"], 3) if e["n"] else None
        e["mean_points"] = round(sum(e["points"]) / len(e["points"]), 2) if e["points"] else None
        e.pop("points")

    totals = [(r.get("score") or {}).get("total") for r in scored]
    totals = [t for t in totals if t is not None]
    mx = next((( r.get("score") or {}).get("max") for r in scored
               if r.get("score")), None)

    unprobed = sorted({t for r in records for t in (r.get("unprobed_tools") or [])})
    conditions = _run_conditions(records)
    surfaces_dead = conditions["surfaces_dead"]

    # The headline block (pass / mean score / criteria), when the task
    # declares a matrix.
    matrix = _matrix_block(spec, records, caveats=conditions["caveats"],
                           baselines_json=baselines_json)

    # The interval and the estimators, over the SAME scored set as Pass@1.
    # The rate itself is still computed here, not read back from
    # learning_signal, so a stub cannot move the number - only withhold the
    # interval beside it.
    caveats = conditions["caveats"]
    p1 = _call(learning_signal, "learning_signal", "pass_at_1", caveats,
               scored) or {}
    k_table = _k_table(_call(learning_signal, "learning_signal",
                             "pass_at_k_table", caveats,
                             n=len(scored), c=len(passed)),
                       len(scored))
    pass_at_k = None
    if k_table is not None:
        pass_at_k = {
            "denominator": "unbiased estimators over the n scored rollouts "
                           "(c of them passed), sampling k WITHOUT "
                           "replacement; k > n is undefined and is not "
                           "listed. Distinct from pass_hat_k, the run-level "
                           "bit: that says whether every scored rollout "
                           "passed, this estimates the probability that k "
                           "fresh samples all would.",
            "n": len(scored),
            "c": len(passed),
            **k_table,
        }
    signal = _call(learning_signal, "learning_signal", "summarise", caveats,
                   records, matrix)

    # Pass^k per model as well as overall: one Pass^k over a run that mixed two
    # models says nothing about either, and the GT001 comparison this metric
    # exists for (Claude 1.0 vs Kimi 0) is exactly that comparison.
    by_model = {}
    for m in sorted({r.get("model") for r in records if r.get("model")}):
        by_model[m] = _pass_hat_k([r for r in records if r.get("model") == m])

    out = {
        "rollouts": len(records),
        "scored": len(scored),
        "quarantined": len(quarantined),
        "quarantined_caught_violating": len(caught),
        "quarantined_unobserved": len(unobserved),
        "redteam_detector_tests": len([r for r in records
                                       if r.get("redteam_detector_test")]),
        "excluded_from_pass_at_1": len(excluded),
        "pass_at_1": round(len(passed) / len(scored), 3) if scored else None,
        "pass_at_1_denominator": "scored rollouts (attempted, not quarantined, "
                                 "no harness or grader defect)",
        # the interval prints on the same line as the rate, every time: K=5
        # cannot tell 0.8 from 1.0 and the reader has to be shown that
        "pass_at_1_ci95": _ci_list(p1.get("ci95")),
        "pass_at_1_se": (None if p1.get("se") is None
                         else round(float(p1["se"]), 4)),
        "pass_at_1_n": p1.get("n", len(scored)),
        "pass_at_k": pass_at_k,
        **_pass_hat_k(records),
        "pass_hat_k_denominator":
            "1.0 iff EVERY one of the k scored rollouts passed, else 0.0. k is "
            "the scored count, not K: a quarantined rollout counted as a failure "
            "would blame the model for the harness. When k < K the claim is "
            "weaker than the run intended and `pass_hat_k_complete` is false.",
        "pass_hat_k_by_model": by_model,
        # Nothing below this line may be read without the block above it.
        "run_conditions": conditions,
        "measurement_valid": conditions["measurement_valid"],
        "invalidated_because": conditions["invalidated_because"],
        "classification_counts": by_class,
        "score_distribution": {
            "max": mx,
            "totals": sorted(totals),
            "median": _median(totals),
            "mean": round(sum(totals) / len(totals), 2) if totals else None,
        },
        "per_check": checks,
        "tool_calls_median": _median([r.get("tool_calls") for r in scored]),
        "wall_clock_s_median": _median([r.get("wall_clock_s") for r in scored]),
        "dead_surfaces": surfaces_dead,
        "unprobed_tools": unprobed,
        "needs_review": [r["rollout_id"] for r in records if r.get("needs_review")],
        "models": sorted({r.get("model") for r in records if r.get("model")}),
        "drivers": sorted({r.get("driver") for r in records if r.get("driver")}),
        "topologies": sorted({r.get("topology", "host") for r in records}),
        "blindness_guarantees": sorted({r.get("blindness_guarantee")
                                        for r in records if r.get("blindness_guarantee")}),
        # what a trainer's filter reads (docs/research/Scoring_Redesign.md): None
        # while learning_signal is a stub, and the caveat above says so
        "learning_signal": signal,
    }
    # Absent - not null - for a non-matrix task, so report.json for one is
    # byte-identical to before the matrix existed.
    if matrix is not None:
        out["matrix"] = matrix
    return out


def _fmt_rate(v) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def _render_k_table(pk: dict | None, m: dict | None, provisional: str) -> list[str]:
    """The pass@k / pass^k estimator table(s), or one line saying why not."""
    L = [""]
    if pk is None:
        L.append(f"{provisional}pass@k / pass^k (unbiased est.): unavailable "
                 f"- see caveats under Run conditions")
        return L

    def table(block: dict, what: str) -> list[str]:
        ks = sorted(block["pass_at_k"], key=int)
        rows = [(f"{provisional}pass@k / pass^k (unbiased est.) {what}: "
                 f"n={block['n']}, c={block['c']} passed; sampled without "
                 f"replacement, so only k ≤ n is defined")]
        if not ks:
            rows.append("  - no k ≤ n: nothing was scored")
            return rows
        rows.append("")
        rows.append("| k | pass@k | pass^k (unbiased est.) |")
        rows.append("|---|---|---|")
        for k in ks:
            rows.append(f"| {k} | {_fmt_rate(block['pass_at_k'][k])} | "
                        f"{_fmt_rate(block['pass_hat_k'].get(k))} |")
        return rows

    L.extend(table(pk, "over scored rollouts"
                       + (" (pass_all_required)" if m is not None else "")))
    return L


def _render_headline_k_table(h: dict, provisional: str) -> list[str]:
    """The unbiased pass@k / pass^k table for the headline `pass` bit."""
    L = []
    if not h.get("n"):
        return L
    ks = sorted((h.get("pass_at_k") or {}), key=int)
    if not ks:
        L.append(f"{provisional}pass@k / pass^k (unbiased est.) for the "
                 f"headline pass: unavailable - see caveats under Run conditions")
        return L
    L.append(f"{provisional}pass@k / pass^k (unbiased est.) for the headline "
             f"pass: n={h['n']} graded, c={h['passed']} passed; sampled "
             f"without replacement, so only k ≤ n is defined")
    L.append("")
    L.append("| k | pass@k | pass^k (unbiased est.) |")
    L.append("|---|---|---|")
    for k in ks:
        L.append(f"| {k} | {_fmt_rate(h['pass_at_k'][k])} | "
                 f"{_fmt_rate((h.get('pass_hat_k') or {}).get(k))} |")
    return L


def _render_criteria(m: dict) -> list[str]:
    """The criteria table: every check on record over the graded episodes,
    must-have first, then by rate ascending. This is the 'why did it fail'
    view."""
    crit = m.get("criteria") or {}
    if not crit:
        if m.get("outcome_declared"):
            return ["criteria: none on record (no graded episode carries a "
                    "check)"]
        return []
    L = [f"criteria over {m.get('graded', 0)} graded rollouts (must-have = a "
         f"top-band class requires it; `met` counts `X` passed, or for a "
         f"`!X` penalty/violation check, X did not charge):", ""]
    L.append("| criterion | read as | must-have | required by | met | n | rate |")
    L.append("|---|---|---|---|---|---|---|")
    for cid, e in crit.items():
        L.append(f"| {cid} | `{e.get('condition', cid)}` | "
                 f"{'yes' if e.get('must_have') else 'no'} | "
                 f"{', '.join(e.get('required_by') or []) or '-'} | "
                 f"{e['passed']} | {e['n']} | {_fmt_rate(e.get('rate'))} |")
    return L


def _render_learning_signal(agg: dict, m: dict | None, provisional: str) -> list[str]:
    """The `## Learning signal` body. Every line takes the provisional prefix
    when the run is not a clean measurement - the same rule as the rate: the
    numbers stay on the page, marked, never dropped."""
    ls = agg.get("learning_signal")
    if ls is None:
        return [(f"{provisional}learning signal: unavailable - see caveats "
                 f"under Run conditions")]
    n_scored = agg["scored"]
    what = ("the headline pass bit (outcome class in the arm's top band)"
            if m is not None
            else "the Pass@1 bit (every required check passed)")
    L = [(f"{provisional}what a trainer's filter reads (docs/research/Scoring_Redesign.md): "
          f"the within-group spread of the per-episode reward - {what} - and "
          f"the band that spread puts this task in. Each line states its own "
          f"n; the run scored {n_scored} rollouts, and an episode without "
          f"that bit leaves the group rather than scoring 0 in it")]
    g = ls.get("group") or {}
    L.append(f"{provisional}- group: n={g.get('n')} · mean "
             f"{_fmt_rate(g.get('mean'))} · variance "
             f"{_fmt_rate(g.get('variance'))} · p(1−p) "
             f"{_fmt_rate(g.get('p_one_minus_p'))} · collapsed: "
             f"{str(g.get('collapsed')).lower()}"
             + (" (a group whose rewards are all identical gives a "
                "group-relative trainer zero gradient)"
                if g.get("collapsed") else ""))
    t = ls.get("thresholds") or {}
    L.append(f"{provisional}- band: {ls.get('band')} · rule: core "
             f"{t.get('core_low')} ≤ pass@1 ≤ {t.get('core_high')}; frontier "
             f"pass@1 < {t.get('core_low')} and pass@k > 0; control pass@1 > "
             f"{t.get('control_high')}; eval_only pass@k = 0; unmeasured when "
             f"pass@1 is n/a")
    return L


def _render_tiers(m: dict) -> list[str]:
    """The three tiers under the scalar, each with its denominator, plus the
    saturation flag and the path events. Only on a task that declares
    outcome classes: without a class there is no band, and without a band
    the other two tiers have nothing to sit inside."""
    if not m.get("outcome_declared"):
        return []
    L = []
    n_rw = m.get("reward_denominator") or 0
    n_pg = m.get("progress_denominator") or 0
    if m.get("progress_declared") is False:
        prog = "progress n/a (arm declares no quantities)"
    elif m.get("progress_mean") is None:
        prog = "progress n/a (no anchored value; see caveats)"
    else:
        prog = f"progress {_fmt_rate(m['progress_mean'])} over {n_pg} with a value"
    if m.get("path_declared") is False:
        pth = "path n/a (arm declares no [matrix.path])"
    else:
        pth = f"path {_fmt_rate(m.get('path_mean'))} over {n_rw}"
    L.append(f"tiers: band {_fmt_rate(m.get('band_mean'))} over {n_rw} with a "
             f"scalar · {prog} · {pth} · scalar variance "
             f"{_fmt_rate(m.get('reward_var'))} (population, over {n_rw})")
    h = m.get("pass") or {}
    L.append(f"saturated: {'yes' if m.get('saturated') else 'no'} "
             f"({m.get('role')} arm, top band "
             f"{' | '.join(h.get('top_band') or [])}; pass@1 "
             f"{_fmt_rate(h.get('rate'))} over {h.get('n', 0)} graded)"
             + (" - >= 0.9 or <= 0.1: this arm gives a group-relative "
                "trainer no signal" if m.get("saturated") else ""))
    reach = m.get("progress_reachability")
    if reach:
        L.append(f"progress reachability: n={reach.get('n')} · var "
                 f"{_fmt_rate(reach.get('var'))} · range "
                 f"[{_fmt_rate(reach.get('min'))}, {_fmt_rate(reach.get('max'))}]"
                 f" · {reach.get('distinct')} distinct value(s) - a quantity "
                 f"every episode maxes or zeroes is a bit in disguise")
        paid = m.get("progress_reachability_paid")
        if paid:
            L.append(f"  over the {paid.get('n')} episode(s) whose progress "
                     f"reached the scalar (partial classes only): var "
                     f"{_fmt_rate(paid.get('var'))} · {paid.get('distinct')} "
                     f"distinct - this is the set a trainer's gradient sees")
        elif m.get("progress_paid_denominator") == 0:
            L.append("  no episode's progress reached the scalar (none in a "
                     "partial class): the tier is inert on this run")
    if m.get("path_declared"):
        counts = m.get("path_event_counts") or {}
        n_scored = m.get("scored") or 0
        n_no_audit = len(m.get("rollouts_without_audit_log") or [])
        L.append("path events: "
                 + (" · ".join(f"{k} {n}" for k, n in counts.items())
                    if counts else "none")
                 + f" (over {n_scored - n_no_audit} of {n_scored} scored "
                 f"rollouts with an audit log on record)")
    return L


def render(task: str, run_id: str, agg: dict, records: list[dict],
           spec=None) -> str:
    L = [f"# {task} - rollout report ({run_id})", ""]
    L.append(f"subject: {', '.join(agg['models']) or 'unknown'} "
             f"via driver {', '.join(agg['drivers'])} "
             f"in topology {', '.join(agg.get('topologies') or ['host'])} "
             f"(blindness: {', '.join(agg['blindness_guarantees'])})")
    L.append("")
    L.append(f"K = {agg['rollouts']} rollouts, {agg['scored']} scored, "
             f"{agg['quarantined']} quarantined "
             f"({agg['quarantined_caught_violating']} caught reaching outside the "
             f"toolbelt, {agg['quarantined_unobserved']} unobservable), "
             f"{agg['excluded_from_pass_at_1'] - agg['quarantined']} otherwise excluded")
    if agg["redteam_detector_tests"]:
        L.append("")
        L.append(f"**{agg['redteam_detector_tests']} of these are red-team DETECTOR "
                 "TESTS, not measurements.** A run containing any of them is not a "
                 "Pass@1 measurement of anything.")

    # --- run conditions. ABOVE the number, deliberately. ------------------
    c = agg["run_conditions"]
    L.append("")
    L.append("## Run conditions")
    L.append("")
    # First line of the block, above even the context variant: everything below
    # it describes episodes, and this says whether the numbers beside them were
    # produced by the run or by a re-grade of it.
    L.append(f"- regraded: {str(bool(c['regraded'])).lower()}"
             + (f" ({c['regraded_rollouts']} of {agg['rollouts']} rollouts "
                f"re-graded from stored files"
                + (f"; {len(c['regraded_partial'])} partial: "
                   f"{c['regraded_missing']}" if c["regraded_partial"] else "")
                + ")" if c["regraded"] else ""))
    L.append(f"- context variant: {', '.join(c['variants']) or 'n/a (task declares one context)'}")
    L.append(f"- prompt variant: {', '.join(c['prompt_variants']) or 'not recorded'}")
    L.append(f"- surfaces live ({len(c['surfaces_live'])}): {c['surfaces_live']}")
    if c["surfaces_dead"]:
        L.append(f"- surfaces DEAD ({len(c['surfaces_dead'])}): {c['surfaces_dead']}")
    L.append(f"- surface gate: {', '.join(c['surface_gate']) or 'not run'}"
             + (f" · required {c['surfaces_required']}"
                if c["surfaces_required"] else " · no required_surfaces declared"))
    if c["surfaces_dead_required"]:
        L.append(f"- **required surfaces dead: {c['surfaces_dead_required']}** - "
                 "those rollouts are harness errors, not scores")
    if c["surfaces_dead_by_design"]:
        L.append(f"- dead by design (declared): {c['surfaces_dead_by_design']}")
    if c["surfaces_dead_undeclared"]:
        L.append(f"- **dead and undeclared: {c['surfaces_dead_undeclared']}** - "
                 "the task has not said whether these matter")
    L.append(f"- probes: {c['declared_probes']} declared by the task, "
             f"{c['swept_probes']} swept by the harness over the rest of the "
             f"toolbelt")
    for x in c["caveats"]:
        L.append(f"- caveat: {x}")

    L.append("")
    p = agg["pass_at_1"]
    tag = _surface_tag(c)

    # --- the headline. ABOVE the grader's own criterion, deliberately: when
    # the task declares a [matrix], pass@1 / mean score is the number the run
    # presents.
    m = agg.get("matrix")
    if m is None and spec is not None and getattr(spec, "matrix", None) is not None:
        # an agg that predates the matrix block, rendered under a matrix task
        m = _matrix_block(spec, records)
    if m is not None:
        h = m.get("pass") or {}
        ms = m.get("mean_score") or {}
        score_txt = (f"mean score = {_fmt_rate(ms.get('mean'))} "
                     f"(n={ms.get('n', 0)})")
        if not m.get("outcome_declared"):
            L.append(f"**pass@1 = WITHHELD - the task declares no "
                     f"[matrix.outcome], so no episode has a class.** · {tag}")
        elif not agg["measurement_valid"]:
            L.append(f"**pass@1 = WITHHELD - this run is not a clean "
                     f"measurement.** · {tag}")
            L.append(f"  (provisional, do not publish: pass@1 = "
                     f"{h.get('passed', 0)}/{h.get('n', 0)} "
                     f"{_fmt_ci(h.get('ci95'))} · {score_txt})")
        elif not h.get("n"):
            L.append(f"**pass@1 = WITHHELD - no scored rollout has an outcome "
                     f"class** ({m.get('unmeasured', 0)} unmeasured of "
                     f"{m['scored']} scored) · {tag}")
        else:
            L.append(f"**pass@1 = {h['passed']}/{h['n']} = "
                     f"{_fmt_rate(h['rate'])}** {_fmt_ci(h.get('ci95'))} "
                     f"(n={h['n']} graded rollouts; top band "
                     f"{' | '.join(h.get('top_band') or [])}) · "
                     f"**{score_txt}** · {tag}")
        # The outcome mix explains the misses; the criteria table says which
        # checks carried them.
        counts = m.get("outcome_counts") or {}
        if counts:
            L.append("outcomes: "
                     + " · ".join(f"{k} {n}" for k, n in counts.items())
                     + f" (over {sum(counts.values())} scored rollouts with an "
                     "outcome class)")
        elif m.get("outcome_declared"):
            L.append("outcomes: unavailable (the task declares "
                     "[matrix.outcome] but no episode could be classified; see "
                     "caveats)")
        crit = _render_criteria(m)
        if crit:
            L.append("")
            L.extend(crit)
        hk = _render_headline_k_table(h, "" if agg["measurement_valid"]
                                      else "(provisional) ")
        if hk:
            L.append("")
            L.extend(hk)
        L.append("")
        # The declared scalar sits UNDER the headline: it is what a trainer
        # is handed, which is not what was measured.
        if m.get("reward_mean") is not None:
            L.append(f"declared training scalar (mean) = {m['reward_mean']} "
                     f"over {m['reward_denominator']} "
                     f"(band + {m.get('reward_progress_span')}*progress + path) "
                     f"- reward.py's DECLARED scalar, what a trainer is handed "
                     f"per episode; NOT the measurement, which is pass@1 / "
                     f"mean score above")
        L.extend(_render_tiers(m))
        L.append("")

    # The pre-matrix criterion stays, relabelled for what it is: EVERY required
    # check passed and the point total met. On a matrix task it is no longer
    # the headline, so it prints below the projection.
    label = "pass_all_required" if m is not None else "Pass@1"
    # The interval and n on the SAME line as the rate: a reader who takes
    # 0.8 away from a K=5 run without [0.38, 0.96] beside it has been misled.
    ci = (f"{_fmt_ci(agg.get('pass_at_1_ci95'))} "
          f"(n={agg.get('pass_at_1_n', agg['scored'])} scored rollouts)")
    if not agg["measurement_valid"]:
        L.append(f"**{label} = WITHHELD - this run is not a clean measurement.** "
                 f"· {tag}")
        for why in agg["invalidated_because"]:
            L.append(f"  - {why}")
        L.append("")
        L.append(f"  (provisional, do not publish: {p if p is not None else 'n/a'} "
                 f"{ci} · {tag})")
    else:
        L.append(f"**{label} = {p if p is not None else 'n/a'}** {ci} · {tag}")
    pk = agg["pass_hat_k"]
    L.append(f"Pass^k = {pk if pk is not None else 'n/a'} "
             f"(k = {agg['pass_hat_k_k']} scored of "
             f"{agg['pass_hat_k_attempted']} attempted; "
             f"{agg['pass_hat_k_passed']} passed) · {tag}")
    if not agg["pass_hat_k_complete"]:
        L.append("  - k < K: the excluded rollouts were not observed to pass, so "
                 "this is a weaker consistency claim than the run intended")
    if len(agg["pass_hat_k_by_model"]) > 1:
        L.append("")
        L.append("| model | Pass^k | k |")
        L.append("|---|---|---|")
        for mdl, e in agg["pass_hat_k_by_model"].items():
            L.append(f"| {mdl} | {e['pass_hat_k']} | {e['pass_hat_k_k']} |")

    # The estimators, kept visibly apart from the bit above them. Pass^k on
    # the line above answers "did every scored rollout pass"; pass^k here
    # estimates how often k fresh samples all would, and the two have already
    # been confused once in a review.
    provisional = "" if agg["measurement_valid"] else "(provisional) "
    L.extend(_render_k_table(agg.get("pass_at_k"), m, provisional))

    L.append("")
    L.append("## Learning signal")
    L.append("")
    L.extend(_render_learning_signal(agg, m, provisional))
    L.append("")
    L.append("## Failure classification")
    L.append("")
    L.append("| class | n |")
    L.append("|---|---|")
    for c, n in agg["classification_counts"].items():
        if n:
            L.append(f"| {c} | {n} |")
    L.append("")
    L.append("## Score distribution")
    sd = agg["score_distribution"]
    L.append("")
    L.append(f"max {sd['max']} · median {sd['median']} · mean {sd['mean']} · "
             f"totals {sd['totals']}")
    L.append("")
    L.append("## Per-check pass rate (scored rollouts)")
    L.append("")
    L.append("| check | max | pass rate | mean points |")
    L.append("|---|---|---|---|")
    for cid in sorted(agg["per_check"]):
        e = agg["per_check"][cid]
        L.append(f"| {cid} | {e['max']} | {e['pass_rate']} | {e['mean_points']} |")
    L.append("")
    L.append(f"median tool calls {agg['tool_calls_median']} · "
             f"median wall clock {agg['wall_clock_s_median']}s")
    if agg["dead_surfaces"]:
        L.append("")
        L.append(f"dead surfaces in this world: {agg['dead_surfaces']} - any "
                 "failure attributable to these is a world gap, not difficulty. "
                 "See **Run conditions** above for which of them the task "
                 "requires, declares, or has said nothing about.")
    if agg["unprobed_tools"]:
        L.append("")
        L.append(f"**{len(agg['unprobed_tools'])} tools whose liveness is unknown:** "
                 f"{agg['unprobed_tools']} - neither declared by the task nor "
                 "reachable by the zero-argument sweep, so a failure on any of "
                 "them cannot be attributed to a dead surface and will land on "
                 "the agent as `reasoning`.")
    if agg["needs_review"]:
        L.append("")
        L.append(f"**needs human review:** {agg['needs_review']}")
    L.append("")
    L.append("## Per rollout")
    L.append("")
    # the outcome column appears only once some episode has a class: a
    # column of n/a on a task that declares no [matrix.outcome] says nothing
    per_rollout = (m or {}).get("per_rollout") or {}
    outcomes = {rid: (row.get("outcome") or {}).get("outcome")
                for rid, row in per_rollout.items()}
    with_outcome = any(v for v in outcomes.values())
    if with_outcome:
        L.append("| id | class | outcome | pass | score | band | prog | path | "
                 "total | tool calls | wall s | why |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    else:
        L.append("| id | class | total | tool calls | wall s | why |")
        L.append("|---|---|---|---|---|---|")

    def tier(rid, key):
        v = (per_rollout.get(rid) or {}).get(key)
        return "n/a" if v is None else f"{v:.2f}"

    def bit(rid):
        v = (per_rollout.get(rid) or {}).get("pass")
        return "n/a" if v is None else ("yes" if v else "no")

    def score(rid):
        v = (per_rollout.get(rid) or {}).get("score")
        return "n/a" if not v else f"{v['score']:.2f}"

    for r in records:
        rid = r.get("rollout_id")
        cls = (f"{outcomes.get(rid) or 'n/a'} | {bit(rid)} | {score(rid)} | "
               f"{tier(rid, 'band')} | "
               f"{tier(rid, 'progress')} | {tier(rid, 'path_penalty')} | "
               if with_outcome else "")
        L.append(f"| {r.get('rollout_id')} | {r.get('classification')} | {cls}"
                 f"{(r.get('score') or {}).get('total')} | {r.get('tool_calls')} | "
                 f"{r.get('wall_clock_s')} | {str(r.get('why'))[:110]} |")
    q = [r for r in records
         if r.get("classification") in ("blindness_violation", "unobserved")]
    if q:
        L.append("")
        L.append("## Quarantined")
        L.append("")
        for r in q:
            L.append(f"- `{r['rollout_id']}` ({r['classification']}): "
                     f"{r['blindness']['hard_kinds']}")
            for x in r["blindness"]["violations"][:6]:
                if x["hard"]:
                    L.append(f"  - [{x['detector']}] {x['evidence'][:200]}")
    return "\n".join(L) + "\n"


def write(out_dir: Path, task: str, run_id: str, records: list[dict],
          spec=None, baselines_json=None) -> dict:
    agg = aggregate(records, spec=spec, baselines_json=baselines_json)
    (out_dir / "report.json").write_text(
        json.dumps({"task": task, "run_id": run_id, "summary": agg,
                    "rollouts": records}, indent=2, default=str), encoding="utf-8")
    (out_dir / "report.md").write_text(render(task, run_id, agg, records, spec=spec),
                                       encoding="utf-8")
    return agg
