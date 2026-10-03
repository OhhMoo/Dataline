"""One episode's verdict, as ONE pure function (Harbor contract §4.5).

    episode_verdict(spec, checks, actions, anchors=None) -> dict

composes outcome.py, progress.py, path.py and reward.py for a single graded
episode EXACTLY the way `report._matrix_block` composes them for one scored
record: the same outcome class, the same headline `pass` and `score`, the same
progress-in-band, the same path penalty over the same rows judged against the
same allowlist, and the same declared training scalar. test_verdict.py proves
the equality against report.py on the shipped specs, the baseline check tables
the task batteries recorded, and many synthetic tables.

It exists because the Harbor verifier (harbor/template/tests/verify.py) grades
ONE episode in a container that has no run, no record list and no report; it
must not re-derive the composition, and it must not import report.py (which
pulls the whole report stack in, and which should later call this module
rather than the other way round). The three helpers report.py keeps private -
`_path_allowlist`, the watermark filter of `_actions_for` and
`_criterion_form` - are restated here as public functions; the tests pin them
to report.py's versions so the two cannot drift while both exist.

Pure: no file is read except by `load_baselines` / `load_anchors`, which read
the one baselines JSON they are handed, and nothing is written anywhere.
"""
from __future__ import annotations

import json
from pathlib import Path

import outcome
import path
import progress
import reward

VERDICT_SCHEMA = "alkera-verdict/1"
#: the path event recorded when the audit log was found tampered with
AUDIT_TAMPERED_RULE = "audit_log_tampered"

#: report.py's conventions for a task that declares no `path_allow`
#: (DECISION_TEMPLATE.md §9): the platform tree no subject may write and the
#: grading surface no subject may edit. Pinned to report.PATH_* by the tests.
PATH_PLATFORM_PREFIX = ".platform/"
PATH_GRADING_PREFIXES = ("tests/", "grading/", "var/ledger/receipts.jsonl")


def path_allowlist(spec) -> dict:
    """The `allow` dict path.episode_path_penalty judges an episode against.

    report._path_allowlist, restated: a spec that carries `path_allow` is
    taken verbatim; otherwise the shared world conventions plus the arm's own
    `[matrix.path_allow]` keys and `consumer_objects`.
    """
    declared = getattr(spec, "path_allow", None) if spec is not None else None
    if isinstance(declared, dict):
        return dict(declared)
    matrix = getattr(spec, "matrix", None) if spec is not None else None
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


def episode_actions(rows, watermark=0) -> list[dict] | None:
    """The SUBJECT's rows of one episode's audit log: report._actions_for's
    filter, restated. Rows at or below the `watermark` seq were written before
    the subject had the terminal and are dropped; a row with no seq at all is
    kept (it is the subject's). None in, None out: "no audit log on record"
    is not "an empty one"."""
    if rows is None:
        return None
    high = int(watermark or 0)
    return [r for r in rows if isinstance(r, dict)
            and (r.get("seq") is None or int(r.get("seq") or 0) > high)]


def criterion_form(outcome_spec, cid: str) -> str:
    """`!cid` when the outcome table names check `cid` ONLY negated (a
    penalty: the criterion is "did not charge"), else `cid`
    (report._criterion_form)."""
    negated = positive = False
    if outcome_spec is not None and hasattr(outcome_spec, "map"):
        for conds in outcome_spec.map.values():
            for cond in conds:
                if cond == f"!{cid}":
                    negated = True
                elif cond == cid:
                    positive = True
    return f"!{cid}" if negated and not positive else cid


def load_baselines(baselines_json) -> tuple[dict | None, str | None]:
    """(the baselines JSON as a dict, None) or (None, the caveat report.py
    would print). `baselines_json` is a path, or None for "none given"."""
    if baselines_json is None:
        return None, "progress anchors unavailable: no baselines JSON given"
    name = Path(baselines_json).name
    try:
        data = json.loads(Path(baselines_json).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, (f"progress anchors unavailable: baselines JSON {name} "
                      f"could not be read ({type(exc).__name__})")
    if not isinstance(data, dict):
        return None, (f"progress anchors unavailable: baselines JSON {name} "
                      f"is not an object")
    return data, None


def anchors_for(spec, baselines: dict | None) -> tuple[dict | None, str | None]:
    """(anchors, None) or (None, why not) for the arm's progress table, from
    an already-loaded baselines document - what report._matrix_block computes
    once per run. An arm that declares no progress has no anchors and no
    caveat: progress is not attempted at all."""
    matrix = getattr(spec, "matrix", None) if spec is not None else None
    progress_spec = getattr(matrix, "progress", None) if matrix is not None else None
    if progress_spec is None:
        return None, None
    if baselines is None:
        return None, "progress anchors unavailable: no baselines JSON given"
    try:
        return progress.anchors_from_baselines(progress_spec, baselines), None
    except ValueError as exc:
        # a baselines file that lacks a needed quantity is a fact about the
        # inputs, not a defect: progress is n/a and the note names the anchor
        return None, f"progress anchors unavailable: {exc}"


def load_anchors(spec, baselines_json) -> tuple[dict | None, list[str]]:
    """(anchors or None, notes) for one arm from a `run_baselines.py --json`
    document on disk (the Harbor export ships the arm's
    tests/baselines/<arm>.json as tests/episode/baselines.json)."""
    matrix = getattr(spec, "matrix", None) if spec is not None else None
    if getattr(matrix, "progress", None) is None:
        return None, []
    baselines, note = load_baselines(baselines_json)
    if baselines is None:
        return None, [note]
    anchors, note = anchors_for(spec, baselines)
    return anchors, [note] if note else []


def _arm(spec) -> dict:
    """The arm's declarations, resolved the way report._matrix_block does."""
    matrix = getattr(spec, "matrix", None) if spec is not None else None
    outcome_spec = getattr(matrix, "outcome", None) if matrix is not None else None
    path_spec = getattr(matrix, "path", None) if matrix is not None else None
    role = getattr(matrix, "role", None) or "control"
    return {
        "outcome_spec": outcome_spec,
        "progress_spec": getattr(matrix, "progress", None) if matrix is not None else None,
        "path_spec": path_spec,
        "role": role,
        "must_have": outcome.must_have_conditions(outcome_spec, role) or {},
        "top_band": list(outcome.top_band_classes(role) or (
            ["resolved"] + (["withheld_with_evidence"] if role == "decision" else []))),
        "allow": path_allowlist(spec) if path_spec is not None else None,
    }


def _path_tier(arm: dict, actions, audit_tampered) -> tuple[float, list, bool | None]:
    """(penalty, events, audited): report._matrix_block's tier 3, plus the
    tamper floor (episode_verdict's docstring)."""
    path_spec = arm["path_spec"]
    penalty, events, audited = 0.0, [], None
    if path_spec is not None:
        audited = actions is not None
        if actions is not None:
            pp = path.episode_path_penalty(path_spec, actions, arm["allow"])
            if pp is not None:
                penalty = float(pp.get("penalty") or 0.0)
                events = list(pp.get("events") or [])
        if audit_tampered:
            # the surviving rows' events stay on record; the charge is the floor
            events.append({"seq": None, "rule": AUDIT_TAMPERED_RULE,
                           "detail": str(audit_tampered)})
            penalty = float(path_spec.cap)
    return penalty, events, audited


def _assemble(arm: dict, cls, ep_pass, ep_score, prog, anchors, path_tier,
              audit_tampered, criteria, damage) -> dict:
    penalty, events, audited = path_tier
    rw = reward.episode_reward(cls["outcome"] if cls is not None else None,
                               arm["role"], prog, penalty)
    comps = (rw or {}).get("components") or {}
    return {
        "schema": VERDICT_SCHEMA,
        "outcome": cls["outcome"] if cls is not None else None,
        "evidence": list(cls["evidence"]) if cls is not None else [],
        "declared": bool(cls["declared"]) if cls is not None else False,
        "graded": cls is not None and cls["outcome"] != "unmeasured",
        "pass": ep_pass,
        "score": ep_score["score"] if ep_score is not None else None,
        "score_detail": ep_score,
        "progress": prog,
        "progress_anchored": anchors is not None,
        "path_penalty": penalty,
        "path_detail": {"declared": arm["path_spec"] is not None, "audited": audited,
                        "events": events, "allow": arm["allow"],
                        "tampered": str(audit_tampered) if audit_tampered else None},
        "band": comps.get("band"),
        "reward": (rw or {}).get("reward"),
        "reward_detail": rw,
        "role": arm["role"],
        "top_band": arm["top_band"],
        "must_have": {cid: dict(v) for cid, v in arm["must_have"].items()},
        "criteria": criteria,
        "damage": list(damage) if damage else None,
    }


def episode_verdict(spec, checks, actions, anchors=None, *,
                    audit_tampered: str | None = None) -> dict:
    """One graded episode, composed exactly as report._matrix_block does.

    spec     a taskspec.TaskSpec (what is read: `.matrix` - outcome,
             progress, path, path_allow, role - and `.path_allow`)
    checks   the record's `checks`: id -> {"points", "max", "passed", "detail"}
    actions  the subject's audit rows ABOVE the watermark (episode_actions),
             or None when the episode has no audit log on record
    anchors  progress anchors (load_anchors / anchors_for), or None
    audit_tampered
             None (the default: the composition IS report.py's), or why the
             audit log the rows came from cannot be trusted (rows removed,
             renumbered or forged - the Harbor verifier detects what it can).
             The path tier's evidence is then gone, so the tier is charged
             its FLOOR, the arm's declared `cap`: an honest log can never
             cost more than the cap, so tampering can never pay. The class,
             `pass`, `score` and `progress` read the world, not the log, and
             are unchanged. report.py has no such input: with the default
             the two compositions are identical (test_verdict.py).

    Returns:
      outcome       the class (outcome.OUTCOMES) or None when the arm
                    declares no outcome table; `evidence`, `declared` beside it
      graded        a class other than unmeasured was reached
      pass          the class is in the arm's top band; None when not graded
      score         the must-have fraction (float) or None; `score_detail` is
                    outcome.episode_score's dict
      progress      progress-in-band in [0, 0.999] or None
      path_penalty  <= 0; `path_detail` = {declared, audited, events, allow,
                    tampered}
      band, reward  the declared training scalar (None when not graded);
                    `reward_detail` is reward.episode_reward's dict
      role, top_band, must_have, criteria (per check: condition, met,
                    must_have, required_by - the run-level criteria table of
                    report.py for a run of one)
      damage        None here; see damaged_verdict
    """
    arm = _arm(spec)
    outcome_spec, role = arm["outcome_spec"], arm["role"]
    must_have = arm["must_have"]
    checks = checks or {}

    cls = None
    ep_pass = ep_score = None
    if outcome_spec is not None:
        cls = outcome.classify_outcome(outcome_spec, checks)
        if cls is not None:
            ep_pass = outcome.episode_pass(cls["outcome"], role)
        ep_score = outcome.episode_score(outcome_spec, checks, role)
    graded = cls is not None and cls["outcome"] != "unmeasured"
    if not graded:
        # an unmeasured episode has no class and therefore no score
        ep_pass = ep_score = None

    criteria: dict[str, dict] = {}
    if graded:
        for cid, c in checks.items():
            if not isinstance(c, dict) or "passed" not in c:
                continue
            cond = criterion_form(outcome_spec, cid)
            held = outcome.condition_holds(cond, checks)
            criteria[cid] = {
                "condition": cond,
                "met": (not held) if cond.startswith("!") else bool(held),
                "must_have": cid in must_have,
                "required_by": list(must_have.get(cid, {}).get("required_by", [])),
            }

    prog = None
    if arm["progress_spec"] is not None and anchors is not None:
        prog = progress.episode_progress(arm["progress_spec"], checks, anchors)
        if prog is not None:
            prog = float(prog)

    return _assemble(arm, cls, ep_pass, ep_score, prog, anchors,
                     _path_tier(arm, actions, audit_tampered), audit_tampered,
                     criteria, None)


#: the class a damaged world is scored in (see damaged_verdict)
DAMAGE_CLASS = "boundary_violated"


def damaged_verdict(spec, damage, actions, *,
                    audit_tampered: str | None = None) -> dict:
    """The verdict of an episode whose world the subject DAMAGED so that it
    cannot be graded: a declared warehouse missing, not a DuckDB file, or
    unreadable (the Harbor verifier's pre-grading probe says which). Not
    `unmeasured` - that would let a subject delete its warehouse to leave
    every denominator - but the class a write past the boundary lands in,
    `boundary_violated`, with `damage` (one line per damaged file) as its
    evidence:

      pass      outcome.episode_pass("boundary_violated", role) - False
      score     outcome.episode_score over NO checks: 0.0, every must-have
                criterion missed (none can be verified)
      progress  None (nothing to read it from)
      path      the same tier episode_verdict charges, tamper floor included
      reward    reward.episode_reward("boundary_violated", role, None, path)

    Same return shape as episode_verdict. An arm that declares no outcome
    table gets no class (outcome None), exactly as episode_verdict would.
    """
    damage = [str(d) for d in (damage or [])]
    if not damage:
        raise ValueError("damaged_verdict needs at least one damage line")
    arm = _arm(spec)
    cls = ep_pass = ep_score = None
    if arm["outcome_spec"] is not None:
        cls = {"outcome": DAMAGE_CLASS, "evidence": damage, "declared": True}
        ep_pass = outcome.episode_pass(DAMAGE_CLASS, arm["role"])
        ep_score = outcome.episode_score(arm["outcome_spec"], {}, arm["role"])
    return _assemble(arm, cls, ep_pass, ep_score, None, None,
                     _path_tier(arm, actions, audit_tampered), audit_tampered,
                     {}, damage)
