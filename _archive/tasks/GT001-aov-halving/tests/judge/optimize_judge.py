#!/usr/bin/env python3
"""
GEPA optimisation of the GT001 shadow judge prompt.

    python optimize_judge.py --labels labels.json --budget 30

WHAT THIS IS: a working optimisation loop with a held-out split, demonstrating
the machinery end to end.

WHAT THIS IS NOT: a validated judge. The labelled set here is small and derived
from baseline agents rather than human annotation of real trajectories. The
>=98.5% agreement bar is a Phase-1 gate and nothing here claims to approach it.

The discipline that matters, and the reason this exists now rather than later:
GEPA sees TRAIN and VAL only. TEST is held out, never scored during the search,
and is the only split we would ever quote an agreement number from. Optimising
against the same data you then report on inflates exactly the number a launch
would be gated on.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "vendor" / "gepa" / "src"))

from shadow_judge import AXES, SEED_PROMPT, heuristic_judge  # noqa: E402


def agreement(pred: dict, label: dict) -> float:
    """Per-axis exact agreement, averaged. Blunt on purpose - a human-labelled
    corpus would warrant something ordinal, and that is a Phase-1 problem."""
    return sum(1.0 for ax in AXES if pred[ax]["score"] == label[ax]) / len(AXES)


def evaluate(prompt: dict, examples: list[dict]) -> tuple[list[float], list[dict]]:
    scores, traces = [], []
    for ex in examples:
        pred = heuristic_judge(prompt, ex["artifact"], {})
        s = agreement(pred, ex["labels"])
        scores.append(s)
        traces.append({
            "agent": ex["agent"],
            "predicted": {ax: pred[ax]["score"] for ax in AXES},
            "human": ex["labels"],
            "agreement": s,
            "rationale": {ax: pred[ax]["rationale"] for ax in AXES},
        })
    return scores, traces


def build_adapter():
    """GEPAAdapter over the shadow judge. Components = the three axis prompts."""
    from gepa.core.adapter import EvaluationBatch, GEPAAdapter

    class JudgeAdapter(GEPAAdapter):
        def evaluate(self, batch, candidate, capture_traces=False):
            scores, traces = evaluate(candidate, batch)
            return EvaluationBatch(
                outputs=[t["predicted"] for t in traces],
                scores=scores,
                trajectories=traces if capture_traces else None,
            )

        def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
            """Feed GEPA the DISAGREEMENTS. Reflective prompt evolution needs
            textual feedback about what went wrong, not just a scalar."""
            out = {}
            for comp in components_to_update:
                rows = []
                for t in (eval_batch.trajectories or []):
                    ax = comp if comp in AXES else AXES[0]
                    if t["predicted"][ax] != t["human"][ax]:
                        rows.append({
                            "Inputs": {"agent": t["agent"], "axis": ax},
                            "Generated Outputs": f"scored {t['predicted'][ax]}",
                            "Feedback": (
                                f"Human scored {t['human'][ax]}, judge scored "
                                f"{t['predicted'][ax]}. Judge reasoning: "
                                f"{t['rationale'][ax]}. The prompt should name the "
                                f"specific evidence that distinguishes these scores."),
                        })
                out[comp] = rows or [{
                    "Inputs": {"axis": comp}, "Generated Outputs": "all correct",
                    "Feedback": "No disagreements on this axis in this batch."}]
            return out

    return JudgeAdapter()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=str(Path(__file__).parent / "labels.json"))
    ap.add_argument("--budget", type=int, default=30)
    ap.add_argument("--out", default=str(Path(__file__).parent / "optimized_prompt.json"))
    a = ap.parse_args()

    data = json.loads(Path(a.labels).read_text(encoding="utf-8"))
    rng = random.Random(1337)
    rng.shuffle(data)
    n = len(data)
    n_tr, n_va = int(n * 0.5), int(n * 0.25)
    train, val, test = data[:n_tr], data[n_tr:n_tr + n_va], data[n_tr + n_va:]
    print(f"  labelled examples: {n}   train {len(train)} / val {len(val)} / "
          f"TEST {len(test)} (held out)")

    base_test, _ = evaluate(SEED_PROMPT, test)
    base = sum(base_test) / len(base_test)
    print(f"  seed prompt, held-out agreement: {base:.3f}")

    try:
        import gepa
        adapter = build_adapter()
        res = gepa.optimize(
            seed_candidate=dict(SEED_PROMPT),
            trainset=train, valset=val, adapter=adapter,
            max_metric_calls=a.budget, reflection_lm=None,
        )
        best = res.best_candidate
        print("  GEPA search complete")
    except Exception as e:
        print(f"  GEPA unavailable or reflection_lm required ({type(e).__name__}: "
              f"{str(e)[:90]})")
        print("  falling back to a local search over the same split discipline")
        best = local_search(train, val, a.budget)

    opt_test, traces = evaluate(best, test)
    opt = sum(opt_test) / len(opt_test)
    ins_seed = sum(evaluate(SEED_PROMPT, train + val)[0]) / len(train + val)
    ins_opt = sum(evaluate(best, train + val)[0]) / len(train + val)
    Path(a.out).write_text(json.dumps(best, indent=2), encoding="utf-8")

    print()
    print("                 in-sample    held-out")
    print("  seed        %12.3f%12.3f" % (ins_seed, base))
    print("  optimised   %12.3f%12.3f" % (ins_opt, opt))
    print("  delta       %+12.3f%+12.3f" % (ins_opt - ins_seed, opt - base))
    if ins_opt - ins_seed > 0.05 and opt - base <= 0.02:
        print()
        print("  >> OVERFIT. The search improved in-sample and did not generalise.")
        print("     This is the held-out split doing its job. With 7 training")
        print("     examples that is the expected outcome, and it is exactly why")
        print("     agreement must never be quoted from data the search saw.")
    print()
    print("  prompt written to " + str(a.out))
    print("\n  NOTE: advisory only. The official GT001 score remains fully")
    print("  deterministic; this judge does not touch it. The held-out split was")
    print("  never scored during the search.")
    return 0


def local_search(train, val, budget):
    """Reflective mutation without an LLM: add the criteria the judge is
    demonstrably missing. Same split discipline, same feedback signal."""
    vocab = ["mechanism", "ruled out", "evidence", "preserve", "window", "recurrence"]
    best, best_score = dict(SEED_PROMPT), -1.0
    rng = random.Random(7)
    for _ in range(budget):
        cand = {ax: best[ax] for ax in AXES}
        ax = rng.choice(AXES)
        term = rng.choice(vocab)
        if term not in cand[ax]:
            cand[ax] = cand[ax].rstrip(".?") + f". Consider whether the report addresses {term}."
        s = sum(evaluate(cand, train + val)[0]) / len(train + val)
        if s > best_score:
            best, best_score = cand, s
    return best


if __name__ == "__main__":
    raise SystemExit(main())
