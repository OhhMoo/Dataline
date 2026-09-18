"""The public benchmark artefact: what a website row needs and nothing else.

report.json is the run's full record and it is FOR the people who built the
task: it carries every check's detail text, every rollout's `why`, the
artifacts, the canaries. A benchmark page needs none of that, and publishing
it would hand the next model the grader. So `write()` emits `benchmark.json`
- a second, smaller file with a STABLE schema - rather than a filtered view
of the first, and `render_row()` turns it into the one markdown table row the
README and the website print.

Everything the report refuses to do, this file refuses too. The denominator
travels with every rate (`n` beside each one, `K`/`scored`/`quarantined` at
the top). A run that is not `measurement_valid` has every rate NULLED here
and `withheld: true` set - the counts and the reasons stay, the numbers go,
because a provisional number on a public page is a published number. Axes
are never aggregated: the row prints four axis means and the projected pass
on ONE of them, and no suite mean.

SCHEMA (schema_version 1). Keys and their meaning; a `null` rate means
"withheld or not measurable", never 0.

    schema_version        1
    task                  task id, e.g. "GT009-..."
    run_id                the run directory name
    arm                   context variant ("default", "no-runbook", ...);
                          null when the task declares no [variants]; arms
                          joined with "+" when a run mixed them (which also
                          invalidates it)
    prompt_variant        the locked prompt variant, same joining rule; null
                          when not recorded
    models                sorted list of subject model ids
    driver                driver(s), "+"-joined
    topology              "container" | "host" (+ joined when mixed)
    blindness_guarantee   the guarantee string(s) the harness recorded
    K                     rollouts attempted
    scored                rollouts in every rate's denominator
    quarantined           rollouts excluded for blindness violation /
                          unobservability (counted, never dropped)
    measurement_valid     bool
    invalidated_because   list of run-condition strings (empty when valid)
    withheld              not measurement_valid; when true every rate below
                          is null
    pass_at_1             {rate, ci95: [lo, hi]|null, n} | null.
                          On a [matrix] task this is the strict all-checks
                          criterion (pass_all_required); the headline for such
                          a task is matrix.projected_pass
    pass_at_k             {n, c, pass_at_k: {k: v}, pass_hat_k: {k: v}} | null
                          - the UNBIASED ESTIMATORS for k <= n, distinct from
                          pass_hat_k below
    pass_hat_k            {pass_hat_k: 1.0|0.0|null, k, complete} - the
                          run-level bit: did every one of the k scored
                          rollouts pass; k < K when `complete` is false
    matrix                null on a non-matrix task, else
                          {pass_axis, gate, projected_pass, projected_pass_ci95,
                           denominator, axis_means: {found, warranted, scoped,
                           consumer}, outcome_counts: {class: n}}
    learning_signal       null when unavailable or withheld, else
                          {band, group: {n, p_one_minus_p, collapsed},
                           axis_separation: {found_rate, pass_axis_rate, gap,
                           gap_ci95, separated} | null}

Never here: per-check rows, per-rollout `why` text, artifacts, transcripts,
check detail text, canaries, filesystem paths, per-rollout rewards.
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA_VERSION = 1

AXES = ("found", "warranted", "scoped", "consumer")


def _join(xs, default=None):
    xs = [x for x in (xs or []) if x]
    return "+".join(sorted(set(map(str, xs)))) if xs else default


def _ci(ci):
    if ci is None:
        return None
    return [round(float(ci[0]), 3), round(float(ci[1]), 3)]


def build(task: str, run_id: str, agg: dict, records: list[dict],
          spec=None) -> dict:
    """The benchmark dict for one run. Pure; `write()` puts it on disk.

    `records` are consulted for nothing a rate needs - agg already carries
    every count - and are accepted so a caller can hand over what it has.
    `spec` likewise: the matrix's pass axis is read from agg["matrix"], which
    was resolved under the spec when the report was written.
    """
    c = agg.get("run_conditions") or {}
    valid = bool(agg.get("measurement_valid"))
    withheld = not valid

    def rate(v):
        return None if withheld else v

    p1 = None
    if agg.get("pass_at_1") is not None and not withheld:
        p1 = {"rate": agg["pass_at_1"],
              "ci95": _ci(agg.get("pass_at_1_ci95")),
              "n": agg.get("pass_at_1_n", agg.get("scored"))}

    pk = agg.get("pass_at_k")
    pass_at_k = None
    if pk is not None and not withheld:
        pass_at_k = {"n": pk.get("n"), "c": pk.get("c"),
                     "pass_at_k": dict(pk.get("pass_at_k") or {}),
                     "pass_hat_k": dict(pk.get("pass_hat_k") or {})}

    m = agg.get("matrix")
    matrix = None
    if m is not None:
        matrix = {
            "pass_axis": m.get("pass_axis"),
            "gate": m.get("gate"),
            "projected_pass": rate(m.get("projected_pass")),
            "projected_pass_ci95": rate(_ci(m.get("projected_pass_ci95"))),
            "denominator": m.get("projected_pass_denominator"),
            "axis_means": {a: rate((m.get("axis_means") or {}).get(a))
                           for a in AXES},
            "outcome_counts": dict(m.get("outcome_counts") or {}),
        }

    ls = agg.get("learning_signal")
    signal = None
    if ls is not None and not withheld:
        g = ls.get("group") or {}
        sep = ls.get("axis_separation")
        signal = {
            "band": ls.get("band"),
            "group": {"n": g.get("n"),
                      "p_one_minus_p": g.get("p_one_minus_p"),
                      "collapsed": g.get("collapsed")},
            "axis_separation": None if sep is None else {
                "found_rate": sep.get("found_rate"),
                "pass_axis_rate": sep.get("pass_axis_rate"),
                "gap": sep.get("gap"),
                "gap_ci95": _ci(sep.get("gap_ci95")),
                "separated": sep.get("separated"),
            },
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "task": task,
        "run_id": run_id,
        "arm": _join(c.get("variants")),
        "prompt_variant": _join(c.get("prompt_variants")),
        "models": sorted(agg.get("models") or []),
        "driver": _join(agg.get("drivers"), default="unknown"),
        "topology": _join(agg.get("topologies"), default="host"),
        "blindness_guarantee": _join(agg.get("blindness_guarantees")),
        "K": agg.get("rollouts"),
        "scored": agg.get("scored"),
        "quarantined": agg.get("quarantined"),
        "measurement_valid": valid,
        "invalidated_because": list(agg.get("invalidated_because") or []),
        "withheld": withheld,
        "pass_at_1": p1,
        "pass_at_k": pass_at_k,
        "pass_hat_k": {"pass_hat_k": rate(agg.get("pass_hat_k")),
                       "k": agg.get("pass_hat_k_k"),
                       "complete": agg.get("pass_hat_k_complete")},
        "matrix": matrix,
        "learning_signal": signal,
    }


def write(out_dir: Path, task: str, run_id: str, agg: dict,
          records: list[dict], spec=None) -> dict:
    """Write out_dir/benchmark.json and return the dict it holds."""
    bench = build(task, run_id, agg, records, spec=spec)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "benchmark.json").write_text(
        json.dumps(bench, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return bench


# --- the table row -------------------------------------------------------

COLUMNS = ("task", "arm", "model", "π", "projected pass [ci]", "Pass@1 [ci]",
           "Pass^k", "FOUND", "WARRANTED", "SCOPED", "CONSUMER", "band",
           "valid")


def _rate_cell(rate, ci, n, withheld: bool) -> str:
    """`0.80 [0.38, 0.96] (n=5)`; WITHHELD when the run is not valid; n/a
    when there is no such number for this task."""
    if withheld:
        return "WITHHELD"
    if rate is None:
        return "n/a"
    ci_s = f" [{ci[0]:.2f}, {ci[1]:.2f}]" if ci else ""
    return f"{rate:.2f}{ci_s} (n={n})"


def _axis_cell(v, withheld: bool) -> str:
    if withheld:
        return "WITHHELD"
    return "n/a" if v is None else f"{v:.2f}"


def render_header() -> str:
    return ("| " + " | ".join(COLUMNS) + " |\n"
            + "|" + "---|" * len(COLUMNS))


def render_row(bench: dict) -> str:
    """One markdown row for the README / website table. A withheld run's
    rates print as WITHHELD, never as a number with a footnote."""
    withheld = bool(bench.get("withheld"))
    m = bench.get("matrix")
    p1 = bench.get("pass_at_1") or {}
    pk = bench.get("pass_hat_k") or {}
    ls = bench.get("learning_signal") or {}
    if m is not None:
        proj = _rate_cell(m.get("projected_pass"), m.get("projected_pass_ci95"),
                          m.get("denominator"), withheld)
        pi = m.get("pass_axis") or "n/a"
        axes = [_axis_cell((m.get("axis_means") or {}).get(a), withheld)
                for a in AXES]
    else:
        proj = "WITHHELD" if withheld else "n/a"
        pi = "n/a"
        axes = ["n/a"] * len(AXES)
    if withheld:
        passk = "WITHHELD"
    elif pk.get("pass_hat_k") is None:
        passk = "n/a"
    else:
        passk = f"{pk['pass_hat_k']:.1f} (k={pk.get('k')})"
    cells = [
        bench.get("task") or "",
        bench.get("arm") or "default",
        "+".join(bench.get("models") or []) or "unknown",
        pi,
        proj,
        _rate_cell(p1.get("rate"), p1.get("ci95"), p1.get("n"), withheld),
        passk,
        *axes,
        "WITHHELD" if withheld else (ls.get("band") or "n/a"),
        "yes" if bench.get("measurement_valid") else "no",
    ]
    return "| " + " | ".join(str(x) for x in cells) + " |"
