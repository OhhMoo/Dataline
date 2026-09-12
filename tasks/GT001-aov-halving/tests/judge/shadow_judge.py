#!/usr/bin/env python3
"""
GT001 shadow judge — ADVISORY ONLY. Never contributes to the official score.

Grades the three qualitative axes a state check cannot see:
  J1  root-cause narrative quality  (is the reasoning sound, not just the label right)
  J2  repair design                 (idempotent, adjustment-preserving, targeted)
  J3  regression test quality       (does it actually catch a recurrence)

Why shadow: the official score for v1 is fully deterministic. This runs
alongside, gets logged, and is the thing GEPA optimises — so that by the time a
judge is allowed near a real score, its prompt has been tuned against a
human-labelled set and reported on a held-out split it never saw.

    python shadow_judge.py --artifact final_report.json --score score.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# The seed prompt GEPA starts from. Deliberately mediocre: a strong hand-written
# prompt would make the optimisation look better than it is.
SEED_PROMPT = {
    "J1": "Score 0-2. Did the agent explain the root cause correctly?",
    "J2": "Score 0-2. Was the repair well designed?",
    "J3": "Score 0-2. Did the agent add a good regression test?",
}

AXES = ("J1", "J2", "J3")


def heuristic_judge(prompt: dict[str, str], artifact: dict, evidence: dict) -> dict:
    """Stand-in scorer used until a model is wired in.

    It reads the PROMPT and looks for the criteria the prompt names, so an
    optimised prompt that names more specific criteria produces more accurate
    scores. That is what makes GEPA's search meaningful here rather than
    cosmetic — a richer prompt genuinely changes the output.
    """
    out = {}
    text = " ".join(str(v) for v in artifact.values()).lower()
    for ax in AXES:
        p = prompt.get(ax, "").lower()
        score, why = 0, []

        # criteria the prompt may or may not ask for; each is checkable in the artifact
        criteria = {
            "mechanism": ("append", "merge", "unique_key", "idempoten", "retry"),
            "ruled out": ("untouched", "not affected", "unchanged", "ruled out"),
            "evidence": ("batch_id", "row_count", "checksum", "query", "kpi_daily"),
            "preserve": ("manual", "adjustment", "preserv", "earliest"),
            "window": ("2026-02-01", "2026-03-02"),
            "recurrence": ("unique", "test", "assert", "not_null", "regression"),
        }
        asked = [k for k in criteria if k.split()[0] in p or k in p]
        if not asked:                       # vague prompt -> coarse scoring
            score = 1 if len(text) > 200 else 0
            why.append("prompt named no criteria; fell back to a length heuristic")
        else:
            hit = [k for k in asked if any(t in text for t in criteria[k])]
            score = round(2 * len(hit) / len(asked))
            why.append(f"asked for {asked}; found {hit}")
        out[ax] = {"score": min(2, max(0, score)), "rationale": "; ".join(why)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--prompt", help="optimised prompt JSON; defaults to the seed")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    artifact = json.loads(Path(a.artifact).read_text(encoding="utf-8"))
    prompt = (json.loads(Path(a.prompt).read_text(encoding="utf-8"))
              if a.prompt else SEED_PROMPT)
    res = heuristic_judge(prompt, artifact, {})
    payload = {"axes": res, "total": sum(v["score"] for v in res.values()),
               "max": 6, "advisory_only": True,
               "note": "shadow mode - does NOT contribute to the official score"}
    print(json.dumps(payload, indent=2) if a.json else
          "\n".join(f"  {k} {v['score']}/2  {v['rationale']}" for k, v in res.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
