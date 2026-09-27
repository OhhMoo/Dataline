"""Layer 2 — the learning-signal statistics a training pipeline reads.

The signatures and return shapes below are the contract `report.py` and
`export.py` code against. Do not change a signature or a returned key name
without changing the callers; adding a key is fine, renaming one is not.

Everything here is a pure function over already-scored episode records or
over plain numbers. Nothing here re-reads a world or re-grades.

Record shape (one scored episode, as `report.py` sees it):
    {"scored": bool, "passed": bool, "classification": str, ...}
Matrix per-episode row (report._matrix_block's per_rollout):
    {"outcome": {...}|None, "pass": bool|None, "score": {...}|None, ...}

Why these statistics and not others (docs/research/Scoring_Redesign.md): a
GRPO/DAPO trainer drops every prompt group whose rewards are all identical,
so the number a buyer needs is not "how hard is this task" but "at the
current policy, do K episodes of it disagree with each other". pass@k /
pass^k say whether disagreement is possible at all; `group_signal` says
whether it happened in this run; `band` names the regime; `clustered_mean`
gives the error bar a run-of-runs claim needs.

Rounding: the raw estimators (`pass_at_k_unbiased`, `pass_hat_k_unbiased`,
`wilson_interval`, `trials_to_separate`) return exact values so a caller can
compose them. The dict-returning summaries round to 4 dp because they are
embedded in a report that people read.
"""
from __future__ import annotations

import math
import statistics

# Band thresholds (docs/research/Scoring_Redesign.md). Module constants so a
# report can print the rule it applied.
CORE_LOW = 0.05
CORE_HIGH = 0.70
CONTROL_HIGH = 0.90
BANDS = ("core", "frontier", "eval_only", "control", "unmeasured")

# The k values a pass@k table reports by default. Group sizes trainers
# actually use (5, 8, 10, 16) plus the small ones that let a K=3 run still
# print something.
DEFAULT_KS = (1, 2, 3, 5, 8, 10)

_DP = 4


def _r(x):
    """Round for display; None passes through."""
    return None if x is None else round(x, _DP)


def wilson_interval(passed: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion; None when n == 0.

    Wilson rather than Wald because the runs this reports on are small
    (K = 3..10) and often sit at 0/K or K/K, exactly where Wald collapses
    to a zero-width interval around 0 or 1. Wilson stays inside [0, 1] and
    keeps width there.
    """
    if n <= 0:
        return None
    p = passed / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    lo = max(0.0, centre - half)
    hi = min(1.0, centre + half)
    return (lo, hi)


def pass_at_1(records: list[dict]) -> dict:
    """Over SCORED records only.

    Returns {"rate": float|None, "n": int, "passed": int, "se": float|None,
             "ci95": [lo, hi]|None}.

    `se` is the plain binomial sqrt(p(1-p)/n) - it is what the trainer-side
    formulas (p(1-p), trials_to_separate) are built from, so it is reported
    alongside the Wilson interval rather than instead of it.
    """
    scored = [r for r in records if r.get("scored")]
    n = len(scored)
    passed = sum(1 for r in scored if r.get("passed"))
    if n == 0:
        return {"rate": None, "n": 0, "passed": 0, "se": None, "ci95": None}
    p = passed / n
    ci = wilson_interval(passed, n)
    return {
        "rate": _r(p),
        "n": n,
        "passed": passed,
        "se": _r(math.sqrt(p * (1 - p) / n)),
        "ci95": [_r(ci[0]), _r(ci[1])],
    }


def pass_at_k_unbiased(n: int, c: int, k: int) -> float | None:
    """P(at least one of k sampled without replacement passes) = 1 - C(n-c,k)/C(n,k).

    None when k > n or n == 0 (or k < 1). This is the Chen et al. (2021)
    estimator; the naive 1 - (1-p)^k overstates pass@k for small n.
    """
    if n <= 0 or k < 1 or k > n:
        return None
    if c < 0 or c > n:
        raise ValueError(f"c={c} must lie in [0, n={n}]")
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_hat_k_unbiased(n: int, c: int, k: int) -> float | None:
    """P(all k sampled without replacement pass) = C(c,k)/C(n,k). None when k > n.

    Coexists with report._pass_hat_k, which is the run-level bit "did every
    scored episode pass". This one is the estimator for a k drawn from the
    run, so a K=10 run can say what a Pass^3 would have looked like.
    """
    if n <= 0 or k < 1 or k > n:
        return None
    if c < 0 or c > n:
        raise ValueError(f"c={c} must lie in [0, n={n}]")
    return math.comb(c, k) / math.comb(n, k)


def pass_at_k_table(n: int, c: int, ks=DEFAULT_KS) -> dict:
    """{"pass_at_k": {k: float|None}, "pass_hat_k": {k: float|None}} for each k in ks.

    A k the run cannot support (k > n) is present with value None rather
    than dropped, so a reader sees which columns the run was too small for.
    """
    return {
        "pass_at_k": {k: pass_at_k_unbiased(n, c, k) for k in ks},
        "pass_hat_k": {k: pass_hat_k_unbiased(n, c, k) for k in ks},
    }


def group_signal(rewards: list[float]) -> dict:
    """One prompt group's within-group spread, the quantity GRPO/DAPO reads.

    Returns {"n": int, "mean": float|None, "variance": float|None,
             "p_one_minus_p": float|None (binary rewards only, else None),
             "collapsed": bool|None (all rewards identical)}.

    `variance` is the population variance (divide by n), which is what a
    group-normalised advantage divides by. `p_one_minus_p` is only defined
    when every reward is 0 or 1; for a shaped reward it is None rather than
    a number that means something else. `collapsed` is the DAPO filter's
    condition: a group with identical rewards has zero advantage and is
    discarded before the gradient step. A group of one is collapsed by that
    definition (a lone reward has nothing to differ from), and None only
    when the group is empty.
    """
    n = len(rewards)
    if n == 0:
        return {"n": 0, "mean": None, "variance": None,
                "p_one_minus_p": None, "collapsed": None}
    mean = statistics.fmean(rewards)
    variance = statistics.pvariance(rewards, mu=mean)
    binary = all(r in (0, 1) for r in rewards)
    return {
        "n": n,
        "mean": _r(mean),
        "variance": _r(variance),
        "p_one_minus_p": _r(mean * (1 - mean)) if binary else None,
        "collapsed": len(set(rewards)) == 1,
    }


def advantage_collapse_rate(groups: list[list[float]]) -> float | None:
    """Fraction of groups whose rewards are all identical. None for no groups.

    The per-task diagnostic "Advantage Collapse in GRPO" proposes. An empty group is not a group and leaves the denominator.
    """
    flags = [group_signal(g)["collapsed"] for g in groups]
    flags = [f for f in flags if f is not None]
    if not flags:
        return None
    return sum(1 for f in flags if f) / len(flags)


def band(pass1: float | None, passk: float | None, k: int | None) -> str:
    """One of BANDS.

    core       CORE_LOW <= pass1 <= CORE_HIGH
    frontier   pass1 < CORE_LOW and passk (at the largest k available) > 0
    eval_only  passk == 0 (or pass1 < CORE_LOW with no pass@k evidence)
    control    pass1 > CONTROL_HIGH
    unmeasured pass1 is None
    (CORE_HIGH < pass1 <= CONTROL_HIGH is still "core"; the report prints the
    rule.)

    `k` is the k `passk` was computed at; it is carried so the report can say
    "frontier at pass@8" and so a passk with no k is treated as no evidence.
    The band is a statement about THIS run's policy, not about the task: the
    same task moves bands as the model improves.
    """
    if pass1 is None:
        return "unmeasured"
    if pass1 > CONTROL_HIGH:
        return "control"
    if pass1 >= CORE_LOW:
        # includes the (CORE_HIGH, CONTROL_HIGH] stretch: still trainable,
        # just past the sweet spot, and the report prints the thresholds
        return "core"
    # below CORE_LOW: pass@k decides between "rare but reachable" and
    # "not reachable by outcome RL at this k"
    if passk is None or k is None:
        return "eval_only"
    return "frontier" if passk > 0 else "eval_only"


def clustered_mean(per_cluster: list[tuple[int, int]]) -> dict:
    """Miller (2024) clustered SE: each cluster is (passed, n) for one task.

    Returns {"mean": float|None, "se": float|None, "ci95": [lo, hi]|None,
             "clusters": int, "n": int}, plus "se_naive" (the iid Wald SE)
             so a reader can see how much the clustering cost.

    Episodes of one task are not independent draws: they share a world,
    a prompt and a grader, so the iid SE understates the uncertainty of a
    cross-task mean. The cluster-robust formula ("Adding Error Bars to
    Evals", Miller 2024, eq. for clustered standard error) is

        mean = sum_c passed_c / sum_c n_c                    (= N)
        se   = sqrt( sum_c ( sum_{i in c} (y_i - mean) )^2 ) / N
             = sqrt( sum_c ( passed_c - n_c * mean )^2 ) / N

    the second line because within a cluster every residual is either
    (1 - mean) or (0 - mean), so the cluster's residual sum is closed-form.
    With ONE cluster the residual sum is identically zero and the formula
    degenerates to 0; there is no between-cluster structure to correct for,
    so that case falls back to the iid SE sqrt(p(1-p)/N). With few clusters
    the estimate is still noisy - the report should print `clusters`.
    ci95 = mean ± 1.96·se.
    """
    clusters = [(int(p), int(n)) for p, n in per_cluster if n > 0]
    total_n = sum(n for _, n in clusters)
    total_p = sum(p for p, _ in clusters)
    if total_n == 0:
        return {"mean": None, "se": None, "se_naive": None, "ci95": None,
                "clusters": 0, "n": 0}
    mean = total_p / total_n
    se_naive = math.sqrt(mean * (1 - mean) / total_n)
    if len(clusters) == 1:
        se = se_naive
    else:
        ss = sum((p - n * mean) ** 2 for p, n in clusters)
        se = math.sqrt(ss) / total_n
    return {
        "mean": _r(mean),
        "se": _r(se),
        "se_naive": _r(se_naive),
        "ci95": [_r(mean - 1.96 * se), _r(mean + 1.96 * se)],
        "clusters": len(clusters),
        "n": total_n,
    }


def trials_to_separate(p1: float, p2: float, z: float = 1.96,
                       power_z: float = 0.84) -> int | None:
    """Trials per arm to distinguish p1 from p2 at 95% two-sided, 80% power.

        n = ceil( (z + power_z)^2 * (p1(1-p1) + p2(1-p2)) / (p1 - p2)^2 )

    The standard two-proportion sample-size formula with pooled-free
    variances. None when p1 == p2: no number of trials separates identical
    rates. This is the number to quote when someone asks whether a K=5
    difference between two arms means anything (Segato et al.: gaps under
    3 points are noise at these sizes).
    """
    if p1 == p2:
        return None
    for p in (p1, p2):
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"rate {p} is not in [0, 1]")
    var = p1 * (1 - p1) + p2 * (1 - p2)
    n = ((z + power_z) ** 2 * var) / (p1 - p2) ** 2
    return math.ceil(n)


def _reward_group(records: list[dict], matrix_block: dict | None) -> dict:
    """The per-episode binary reward this run hands a trainer.

    The headline `pass` bit (outcome class in the arm's top band) when the
    task has a matrix; the plain `passed` bit otherwise. An episode whose
    pass is None (unmeasured: none of the declared checks on record) is
    EXCLUDED - it leaves the group, it is not a 0. Counting it as 0 would be
    the quarantine mistake report._pass_hat_k refuses to make, moved one
    layer up.
    """
    if matrix_block is None:
        scored = [r for r in records if r.get("scored")]
        rewards = [1.0 if r.get("passed") else 0.0 for r in scored]
        return {"rewards": rewards, "excluded": 0, "source": "passed"}
    rows = list((matrix_block.get("per_rollout") or {}).values())
    passes = [row.get("pass") for row in rows]
    rewards = [1.0 if p else 0.0 for p in passes if p is not None]
    return {"rewards": rewards,
            "excluded": sum(1 for p in passes if p is None),
            "source": "pass"}


def summarise(records: list[dict], matrix_block: dict | None) -> dict:
    """The block `report.py` embeds under summary["learning_signal"].

    Must contain: pass_at_1 (dict from pass_at_1), pass_at_k (dict from
    pass_at_k_table over scored/passed), group (dict from group_signal over
    the per-episode binary reward: the headline `pass` bit when
    matrix_block is not None, else `passed`), band (str), thresholds
    ({"core_low", "core_high", "control_high"}).

    Also carries: "k_available" (the largest k in the pass@k table the
    group's n supports, 0 when empty), "excluded" (episodes whose pass was
    None and so left the reward group), "reward_source" ("pass" | "passed").

    Every number here is over the SAME reward group, so pass_at_1, pass_at_k,
    group and band agree with each other and with what a trainer would see.
    For a matrix task that means pass_at_1 here is the headline pass rate
    over graded episodes - the same n and c as summary["matrix"]["pass"] -
    while the record-level pass_all_required the report prints below it is
    a different denominator and stays where it is. When matrix_block is None
    the two coincide.
    """
    grp = _reward_group(records, matrix_block)
    rewards = grp["rewards"]
    n = len(rewards)
    c = sum(1 for r in rewards if r == 1.0)
    # pass_at_1 is defined over records; the reward group is re-expressed as
    # minimal scored records so the two share one code path
    p1 = pass_at_1([{"scored": True, "passed": r == 1.0} for r in rewards])
    table = pass_at_k_table(n, c)
    supported = [k for k, v in table["pass_at_k"].items() if v is not None]
    k_available = max(supported) if supported else 0
    passk = table["pass_at_k"][k_available] if k_available else None
    return {
        "pass_at_1": p1,
        "pass_at_k": table,
        "group": group_signal(rewards),
        "band": band(p1["rate"], passk, k_available or None),
        "k_available": k_available,
        "excluded": grp["excluded"],
        "reward_source": grp["source"],
        "thresholds": {"core_low": CORE_LOW, "core_high": CORE_HIGH,
                       "control_high": CONTROL_HIGH},
    }
