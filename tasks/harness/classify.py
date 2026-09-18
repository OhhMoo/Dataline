"""Failure classification.

A pass rate is only worth reading if the failures are attributable. A 12% pass
rate where a third of the failures are timeouts is not a 12% task, and a harness
that reports the 12% without the breakdown has measured nothing.

Classes (one primary per rollout, plus non-exclusive flags):

  pass                 the task's own criterion was met
  blindness_violation  quarantined - the subject reached outside the toolbelt.
                       Never scored. See blindness.py.
  unobserved           quarantined - the harness could not see enough of the
                       episode to assert blindness either way. Also never
                       scored, but it is a statement about the harness, not
                       about the subject, and the report keeps the two apart.
  quota_exhausted      the model's provider refused for a usage or spend limit.
                       Not scored, not the subject's reasoning, not our bug
                       either - and no longer `harness_error` + "needs human
                       review", because the review always found the same
                       thing (`GT004 authorized-20260917-k5`: five episodes,
                       every one "You've hit your monthly spend limit").
                       Decided by `agent_under_test.exit_reason_for`, and
                       re-derivable by `--reclassify` from `agent.final_text`
                       for rollouts recorded before the class existed.
  harness_error        our bug, or the subject process failed to run at all
  grader_defect        the grader crashed, or scored the same world differently
                       twice. The score is untrustworthy, so nothing else can
                       be attributed.
  tool_gap             it needed something the toolbelt could not do
  timeout              ran out of wall clock
  budget               ran out of tool calls
  reasoning            it had the tools and the time and got it wrong

Order matters and is deliberate. tool_gap sits ABOVE timeout and budget: an
episode that burned its clock hammering a dead surface is a tool gap that
presented as a timeout, and calling it a timeout hides the thing worth fixing.

Every heuristic class carries needs_review=True. tool_gap is inferred from error
patterns, and an inference is not a finding.
"""
from __future__ import annotations

TOOL_GAP_TRAILING_FAILURES = 3
TOOL_GAP_ERROR_RATE = 0.5
TOOL_GAP_MIN_CALLS = 10
TOOL_GAP_DEAD_SURFACE_CALLS = 3


def action_stats(rows: list[dict]) -> dict:
    errs = [r for r in rows if r.get("exit") != "ok"]
    trailing = 0
    for r in reversed(rows):
        if r.get("exit") == "ok":
            break
        trailing += 1
    by_tool: dict[str, dict] = {}
    for r in rows:
        t = by_tool.setdefault(r.get("tool", "?"), {"calls": 0, "errors": 0})
        t["calls"] += 1
        if r.get("exit") != "ok":
            t["errors"] += 1
    return {
        "tool_calls": len(rows),
        "errors": len(errs),
        "error_rate": round(len(errs) / len(rows), 3) if rows else 0.0,
        "trailing_failures": trailing,
        "by_tool": by_tool,
        "error_codes": sorted({(r.get("error") or {}).get("code")
                               for r in errs if r.get("error")} - {None}),
        "op_classes": sorted({r.get("op_class") for r in rows} - {None}),
    }


def _tool_gap_evidence(stats: dict, surfaces: dict) -> list[str]:
    ev = []
    dead = set()
    for s_ in surfaces.values():
        if not s_.get("live"):
            dead.add(s_["tool"])
            dead.update(s_.get("covers") or [])
    for tool, t in stats["by_tool"].items():
        if tool in dead and t["errors"] >= TOOL_GAP_DEAD_SURFACE_CALLS:
            ev.append(f"{t['errors']} failed calls to '{tool}', a surface the "
                      f"pre-episode probe already found dead in this world")
    if stats["trailing_failures"] >= TOOL_GAP_TRAILING_FAILURES:
        ev.append(f"episode ended on {stats['trailing_failures']} consecutive "
                  f"failed tool calls")
    if (stats["tool_calls"] >= TOOL_GAP_MIN_CALLS
            and stats["error_rate"] >= TOOL_GAP_ERROR_RATE):
        ev.append(f"{stats['error_rate']:.0%} of {stats['tool_calls']} tool calls "
                  f"failed")
    return ev


def quota_exhaustion_evidence(agent_exit_reason: str, agent_final_text: str,
                              agent_stderr_tail: str = "") -> str | None:
    """Why this rollout is a quota refusal, or None.

    The recorded exit reason is the primary evidence - `agent_under_test`
    decided it with the subject's exit code and result event in hand. The
    prose fallback exists for records made before `quota_exhausted` was a
    reason: they carry `agent_error` and the limit message in `final_text`,
    and `--reclassify` must be able to relabel them without a re-run. Prose is
    only consulted for a subject that FAILED; a clean exit is never relabelled
    on the strength of a sentence in its report.
    """
    if agent_exit_reason == "quota_exhausted":
        return "the subject's provider refused for a usage or spend limit"
    if agent_exit_reason == "agent_error":
        from agent_under_test import looks_like_quota_exhaustion
        for where, text in (("final_text", agent_final_text),
                            ("stderr", (agent_stderr_tail or "")[-1500:])):
            if looks_like_quota_exhaustion(text):
                snippet = " ".join(str(text).split())[:160]
                return (f"the subject process failed and its {where} reads as "
                        f"a usage-limit refusal: \"{snippet}\"")
    return None


def classify(*, spec, score: dict | None, score_stable: bool,
             grader_crashed: bool, harness_failure: str | None,
             agent_exit_reason: str, blindness: dict, action_rows: list[dict],
             surfaces: dict, artifact_present: bool,
             wall_clock_s: int | None = None,
             max_tool_calls: int | None = None,
             unprobed_tools: list[str] | None = None,
             surface_audit: dict | None = None,
             agent_final_text: str = "",
             agent_stderr_tail: str = "") -> dict:
    # the EFFECTIVE ceilings this rollout actually ran under, which are not the
    # task's defaults whenever the caller overrode them
    wall_clock_s = wall_clock_s or spec.wall_clock_seconds
    max_tool_calls = max_tool_calls or spec.max_tool_calls
    stats = action_stats(action_rows)
    flags: list[str] = []
    dead = sorted({lbl for lbl, s in surfaces.items() if not s.get("live")})
    if dead:
        # a COUNT plus the ones the task declared. Since the harness began
        # sweeping every undeclared tool as well, listing all of them here ran
        # to 45 entries on GT001 and turned the flag into wallpaper; the three
        # flags below say which of them actually decide anything.
        named = [d for d in dead if not d.startswith("~")]
        flags.append(f"dead_surfaces={len(dead)} (task-declared: {named})")
    if not artifact_present:
        flags.append("no_final_artifact")
    if blindness.get("soft_count"):
        flags.append(f"soft_blindness_signals={blindness.get('kinds')}")
    if not score_stable:
        flags.append("grader_nondeterministic")
    # errors on a tool the task never probed cannot be attributed to a dead
    # surface, so they silently become the agent's fault. Name them.
    unattributable = sorted({t for t, x in stats["by_tool"].items()
                             if x["errors"] and t in set(unprobed_tools or [])})
    if unattributable:
        flags.append(f"errors_on_unprobed_tools={unattributable}")
    sa = surface_audit or {}
    if sa.get("dead_required"):
        flags.append(f"dead_required_surfaces={sa['dead_required']}")
    if sa.get("dead_undeclared"):
        flags.append(f"dead_undeclared_surfaces={sa['dead_undeclared']}")
    if sa.get("dead_by_design"):
        flags.append(f"dead_by_design={sa['dead_by_design']}")
    if sa.get("dead_by_design_but_live"):
        flags.append("stale_dead_by_design_declaration="
                     f"{sa['dead_by_design_but_live']}")

    def out(cls, why, needs_review=False, passed=False):
        return {"classification": cls, "why": why, "needs_review": needs_review,
                "passed": passed, "scored": cls not in (
                    "blindness_violation", "unobserved", "harness_error",
                    "grader_defect", "quota_exhausted"),
                "flags": flags, "action_stats": stats}

    # "we caught it reaching outside" and "we could not watch it" are both
    # quarantines and both excluded from Pass@1, but only the first says
    # anything about the subject. Filing the second under blindness_violation
    # would be a quiet libel, and would also hide a harness fault as an agent
    # fault.
    unobserved_only = blindness.get("quarantine_reason") == "unobserved"
    if blindness.get("quarantined") and not unobserved_only:
        return out("blindness_violation",
                   f"hard violations: {blindness.get('hard_kinds')}")

    # A harness failure OUTRANKS "unobserved": when we know why we could not
    # watch, say so. Reporting our own crash as an unexplained blind spot throws
    # away the diagnosis we already have.
    if harness_failure:
        return out("harness_error", harness_failure, needs_review=True)
    # A provider refusal sits where `agent_error` used to catch it and above
    # everything scored: the episode the grader saw is however far the model
    # got before the limit, which is a fact about the account, not the model.
    # Named, not `needs_review` - the diagnosis is the whole point.
    quota = quota_exhaustion_evidence(agent_exit_reason, agent_final_text,
                                      agent_stderr_tail)
    if quota:
        return out("quota_exhausted",
                   f"{quota}; excluded from Pass@1 and from the harness-error "
                   f"count, re-run when the limit resets")
    if agent_exit_reason == "agent_error":
        return out("harness_error",
                   "the subject process exited non-zero without finishing; this is "
                   "infrastructure, not the agent's reasoning", needs_review=True)

    # A required surface that was dead before the subject started is a HARNESS
    # error, not a score. rollout.py's gate normally raises before the episode,
    # so this branch is the backstop: it also catches a --reclassify of a
    # rollout recorded before the gate existed, or a run whose gate was somehow
    # bypassed. A gate that only works when it is reached is the defect this
    # repo has already shipped once.
    #
    # It sits BELOW blindness_violation - a subject caught reaching outside the
    # toolbelt is quarantined whatever the world looked like - and BELOW the
    # named harness_failure, for the same reason that one outranks `unobserved`:
    # when we already know the cause, say the cause. It sits ABOVE `unobserved`,
    # `grader_defect` and everything scored, because none of those is worth
    # attributing in a world the task does not describe.
    if (surface_audit or {}).get("gate") == "fail":
        return out("harness_error",
                   f"required surfaces were dead before the episode started: "
                   f"{(surface_audit or {}).get('dead_required')}; the subject "
                   f"was never given the world the task describes",
                   needs_review=True)

    if blindness.get("quarantined"):
        return out("unobserved",
                   f"the episode could not be observed well enough to assert "
                   f"blindness: {blindness.get('unobserved_because')}",
                   needs_review=True)

    if grader_crashed:
        return out("grader_defect", "the grader did not produce a score")
    if not score_stable:
        return out("grader_defect",
                   "the grader scored the same unchanged world differently twice")
    if score is None:
        return out("harness_error", "no score and no grader failure recorded")

    if spec.scores_pass(score):
        return out("pass", f"total {score['total']}/{score['max']}, "
                           f"all of {spec.pass_requires_checks} passed", passed=True)

    gap = _tool_gap_evidence(stats, surfaces)
    if gap:
        return out("tool_gap", "; ".join(gap), needs_review=True)

    if agent_exit_reason == "timeout":
        return out("timeout", f"killed at the {wall_clock_s}s ceiling "
                              f"after {stats['tool_calls']} tool calls")
    if agent_exit_reason == "budget":
        return out("budget", f"killed at the {max_tool_calls}-tool-call ceiling")

    failed = [c["id"] for c in score.get("checks", []) if not c.get("passed")]
    return out("reasoning",
               f"finished inside budget with a working toolbelt; scored "
               f"{score['total']}/{score['max']}, failed checks {failed}")
