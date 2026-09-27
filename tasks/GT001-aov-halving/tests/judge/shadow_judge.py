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


AXIS_TEMPLATE = """You are scoring ONE axis of an engineer's incident report. The score is
advisory only (shadow mode) and never enters the official grade.

Axis {axis}: {instruction}

Score 0, 1 or 2. If the report does not contain enough to decide, answer "unknown";
do not guess. Judge the report's content, not its length or confidence.

The report (JSON):
{artifact}

End with one fenced JSON block: {{"axis": "{axis}", "score": 0|1|2|"unknown", "rationale": "..."}}
"""


def model_judge(prompt: dict[str, str], artifact: dict, client) -> dict:
    """The model backend: one call per axis (one criterion per call), the
    client's k samples, strict majority, `unknown` when there is none.

    `client` is tasks/harness/judge/model_client.ClaudeClient or anything with
    `samples(prompt) -> list[str]`. Scores stay advisory: this function's
    output has the same shape as heuristic_judge's plus the votes.
    """
    from model_client import extract_json, majority  # noqa: PLC0415 - optional backend

    body = json.dumps(artifact, indent=1)[:12000]
    out = {}
    for ax in AXES:
        votes, why = [], ""
        for text in client.samples(AXIS_TEMPLATE.format(
                axis=ax, instruction=prompt.get(ax, ""), artifact=body)):
            data = extract_json(text) or {}
            score = data.get("score")
            votes.append(str(score) if score in (0, 1, 2, "unknown") else "")
            why = why or str(data.get("rationale", ""))
        label, agreement = majority(votes)
        score = int(label) if label in ("0", "1", "2") else None
        out[ax] = {"score": score, "rationale": why[:400], "votes": votes,
                   "agreement": agreement}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--prompt", help="optimised prompt JSON; defaults to the seed")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--backend", choices=["heuristic", "claude"], default="heuristic",
                    help="heuristic: the keyword stand-in (default; what GEPA "
                         "optimises today). claude: a real model through "
                         "tasks/harness/judge/model_client.py")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--k", type=int, default=3)
    a = ap.parse_args()

    artifact = json.loads(Path(a.artifact).read_text(encoding="utf-8"))
    prompt = (json.loads(Path(a.prompt).read_text(encoding="utf-8"))
              if a.prompt else SEED_PROMPT)
    if a.backend == "claude":
        import sys  # noqa: PLC0415
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "harness" / "judge"))
        from model_client import ClaudeClient  # noqa: PLC0415
        client = ClaudeClient(model=a.model, k=a.k)
        res = model_judge(prompt, artifact, client)
    else:
        res = heuristic_judge(prompt, artifact, {})
    payload = {"axes": res, "backend": a.backend,
               "total": sum(v["score"] or 0 for v in res.values()),
               "unknown_axes": [k for k, v in res.items() if v["score"] is None],
               "max": 6, "advisory_only": True,
               "note": "shadow mode - does NOT contribute to the official score"}
    print(json.dumps(payload, indent=2) if a.json else
          "\n".join(f"  {k} {v['score'] if v['score'] is not None else '?'}/2  "
                    f"{v['rationale']}" for k, v in res.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
