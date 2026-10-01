"""Markdown "Damage curves" section for a dynamic-world episode (plan Task 5).

`render(twins_doc)` takes the dict `twins.episode_scores` writes to
twins.json and returns a self-contained markdown section: J and S, a per-tick
table of w.V for agent / no-op / oracle with a sparkline per curve, and the
per-fault timeline. It does not touch tasks/harness/report.py; a caller
appends the section to its report.md.

    python tasks/harness/dyn_report.py <episode_dir>/twins.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BARS = "▁▂▃▄▅▆▇█"
CURVES = ("agent", "noop", "oracle")


def sparkline(values: list[float], top: float | None = None) -> str:
    """One block per value, scaled to [0, top] (top = max(values) by default)."""
    vals = [float(v) for v in values]
    top = max(vals, default=0.0) if top is None else float(top)
    if top <= 0:
        return BARS[0] * len(vals)
    return "".join(BARS[min(len(BARS) - 1, round(v / top * (len(BARS) - 1)))]
                   for v in vals)


def _fmt(x, nd=3) -> str:
    return "-" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def render(doc: dict) -> str:
    S = doc.get("S")
    lines = ["## Damage curves", ""]
    s_txt = (f"S = {S:.3f}" if S is not None
             else "S undefined (J_noop == J_oracle: no room between no-op and oracle)")
    lines += [f"J_agent = {doc['J_agent']:.3f} · J_noop = {doc['J_noop']:.3f} · "
              f"J_oracle = {doc['J_oracle']:.3f} · **{s_txt}**", ""]
    curves = doc["curves"]
    top = max((r["wV"] for k in CURVES for r in curves.get(k, [])), default=0.0)
    for k in CURVES:
        if k in curves:
            lines.append(f"    {k:<7} {sparkline([r['wV'] for r in curves[k]], top)}")
    lines.append("")
    contracts = sorted({c for k in CURVES for r in curves.get(k, []) for c in r["V"]})
    head = ["tick", "phase", "vnow"] + [f"w·V {k}" for k in CURVES if k in curves] \
        + [f"agent {c}" for c in contracts]
    lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    n = len(curves["agent"])
    for i in range(n):
        a = curves["agent"][i]
        cells = [str(a["tick"]), str(a.get("phase") or ""), str(a.get("vnow") or "")]
        cells += [_fmt(curves[k][i]["wV"]) for k in CURVES if k in curves]
        cells += [_fmt(float(a["V"].get(c, 0.0))) for c in contracts]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    tl = doc.get("timeline") or []
    if tl:
        lines += ["### Fault timeline", ""]
        head = ["fault", "contract", "vts", "onset tick", "first 0", "mitigated",
                "ttm (h)", "ttd (h)", "status"]
        lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        for e in tl:
            lines.append("| " + " | ".join([
                e["fault"], str(e.get("contract")), str(e.get("vts") or "t0"),
                _fmt(e.get("onset_tick")), _fmt(e.get("first_zero_tick")),
                _fmt(e.get("mitigated_tick")), _fmt(e.get("time_to_mitigate_hours"), 1),
                _fmt(e.get("time_to_detect_hours"), 1), str(e.get("status"))]) + " |")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print(__doc__)
        return 2
    print(render(json.loads(Path(argv[0]).read_text(encoding="utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
