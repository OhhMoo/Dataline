"""Vendor-neutral HTML for the UI clones: page shell, tables, inline SVG charts.

Everything here is server-rendered, deterministic text: no JavaScript, no
external CSS or fonts, no timestamps of its own. The same request on the same
published state gives the same bytes, so an agent can read a page through a
browser tool, a text browser or plain `curl`, and a test can parse it with
`html.parser`. Nothing is hidden: every number on a page is in a visible table
cell (charts repeat the table; they never carry data the table lacks).

Stable hooks an agent (or a test) can rely on:
    <table class="summary" data-view="...">   one per rendered sheet
    <svg class="chart" data-kind="bar|line">   chart of the first measures
    <form id="calc-editor">                   with <textarea name="formula">
    <div class="error" role="alert">          every refusal, verbatim
"""
from __future__ import annotations

from decimal import Decimal

STYLE = """
body{margin:0;font:14px/1.4 system-ui,sans-serif;color:#1f2933;background:#f5f6f7}
header{display:flex;align-items:center;gap:16px;background:#1f3b57;color:#fff;padding:8px 16px}
header a{color:#fff;text-decoration:none}header .brand{font-weight:600}
header form{margin-left:auto}nav.crumbs{padding:8px 16px;color:#52606d}
nav.crumbs a{color:#1f6fb2}main{padding:0 16px 24px}
.tabs{display:flex;gap:4px;border-bottom:1px solid #cbd2d9;margin:8px 0 12px}
.tabs a{padding:6px 12px;border:1px solid #cbd2d9;border-bottom:none;background:#e4e7eb;
color:#1f2933;text-decoration:none}.tabs a.current{background:#fff;font-weight:600}
.sheet{background:#fff;border:1px solid #cbd2d9;padding:12px;margin-bottom:12px}
.sheet h2{font-size:15px;margin:0 0 8px}table{border-collapse:collapse}
th,td{border:1px solid #e4e7eb;padding:3px 8px;text-align:left}td.num{text-align:right}
th{background:#f0f2f4}.error{border:1px solid #c62828;background:#fdecea;color:#8a1c1c;
padding:8px;margin:8px 0;white-space:pre-wrap}.ok{border:1px solid #2e7d32;
background:#e8f5e9;padding:8px;margin:8px 0}.kpis{display:flex;gap:16px;flex-wrap:wrap}
.kpi{border:1px solid #cbd2d9;padding:8px 12px}.kpi .v{font-size:22px;font-weight:600}
textarea{width:100%;font:13px ui-monospace,monospace}code{font:13px ui-monospace,monospace}
"""


def esc(value) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def page(title: str, body: str, *, header: str = "", crumbs: list[tuple[str, str | None]]
         = ()) -> str:
    trail = ""
    if crumbs:
        parts = [f'<a href="{esc(href)}">{esc(label)}</a>' if href else esc(label)
                 for label, href in crumbs]
        trail = f'<nav class="crumbs" aria-label="Breadcrumb">{" &rsaquo; ".join(parts)}</nav>'
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<title>{esc(title)}</title><style>{STYLE}</style></head><body>"
            f"{header}{trail}<main>{body}</main></body></html>")


def error_box(message: str) -> str:
    return f'<div class="error" role="alert">{esc(message)}</div>'


def table(columns: list[str], rows: list[list[str]], *, numeric_from: int,
          attrs: dict | None = None) -> str:
    """A plain HTML table; cells are the already-formatted strings."""
    a = "".join(f' {k}="{esc(v)}"' for k, v in (attrs or {}).items())
    head = "".join(f"<th scope=\"col\">{esc(c)}</th>" for c in columns)
    body = "".join(
        "<tr>" + "".join(
            f'<td class="num">{esc(v)}</td>' if i >= numeric_from else f"<td>{esc(v)}</td>"
            for i, v in enumerate(r)) + "</tr>" for r in rows)
    return f"<table{a}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    try:                      # the engine returns decimals as exact strings
        return float(Decimal(str(v)))
    except ArithmeticError:
        return None


def bar_chart(labels: list[str], values: list, shown: list[str], *, title: str) -> str:
    """Horizontal bars from a zero baseline; negative values extend left."""
    nums = [_num(v) for v in values]
    finite = [n for n in nums if n is not None]
    if not finite:
        return ""
    lo, hi = min(0.0, *finite), max(0.0, *finite)
    span = (hi - lo) or 1.0
    left, width, row_h = 160, 360, 22
    zero = left + (0 - lo) / span * width
    out = []
    for i, (label, n, text) in enumerate(zip(labels, nums, shown, strict=True)):
        y = 8 + i * row_h
        out.append(f'<text x="{left - 6}" y="{y + 15}" text-anchor="end">{esc(label[:28])}</text>')
        if n is not None:
            x0, x1 = sorted((zero, left + (n - lo) / span * width))
            out.append(f'<rect x="{x0:.1f}" y="{y + 3}" width="{max(x1 - x0, 0.5):.1f}" '
                       f'height="{row_h - 6}" fill="#4e79a7"><title>{esc(label)}: '
                       f'{esc(text)}</title></rect>')
            out.append(f'<text x="{x1 + 4:.1f}" y="{y + 15}">{esc(text)}</text>')
    h = 16 + len(labels) * row_h
    return (f'<svg class="chart" data-kind="bar" role="img" aria-label="{esc(title)}" '
            f'width="{left + width + 110}" height="{h}" font-size="11" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<line x1="{zero:.1f}" y1="4" x2="{zero:.1f}" y2="{h - 4}" stroke="#9aa5b1"/>'
            + "".join(out) + "</svg>")


def line_chart(labels: list[str], values: list, shown: list[str], *, title: str) -> str:
    nums = [_num(v) for v in values]
    finite = [n for n in nums if n is not None]
    if not finite:
        return ""
    lo, hi = min(0.0, *finite), max(*finite, 0.0)
    span = (hi - lo) or 1.0
    left, top, width, height = 70, 10, 560, 180
    step = width / max(len(nums) - 1, 1)

    def xy(i, n):
        return left + i * step, top + height - (n - lo) / span * height

    pts = [(i, n) for i, n in enumerate(nums) if n is not None]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, n) for i, n in pts))
    dots = "".join(
        f'<circle cx="{xy(i, n)[0]:.1f}" cy="{xy(i, n)[1]:.1f}" r="3" fill="#4e79a7">'
        f"<title>{esc(labels[i])}: {esc(shown[i])}</title></circle>" for i, n in pts)
    every = max(1, len(labels) // 8)
    ticks = "".join(
        f'<text x="{left + i * step:.1f}" y="{top + height + 16}" text-anchor="middle">'
        f"{esc(label)}</text>" for i, label in enumerate(labels) if i % every == 0)
    axis = (f'<text x="{left - 6}" y="{top + 4}" text-anchor="end">{hi:,.0f}</text>'
            f'<text x="{left - 6}" y="{top + height}" text-anchor="end">{lo:,.0f}</text>'
            f'<line x1="{left}" y1="{top + height}" x2="{left + width}" y2="{top + height}" '
            f'stroke="#9aa5b1"/>')
    return (f'<svg class="chart" data-kind="line" role="img" aria-label="{esc(title)}" '
            f'width="{left + width + 20}" height="{top + height + 26}" font-size="11" '
            f'xmlns="http://www.w3.org/2000/svg">{axis}'
            f'<polyline points="{path}" fill="none" stroke="#4e79a7" stroke-width="2"/>'
            f"{dots}{ticks}</svg>")
