"""Single-file HTML dashboard: inline CSS and SVG, no CDN, no JavaScript framework.

Deliberately self-contained so it renders with the network disabled and can be mailed
around without anyone needing a Power BI licence.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

from .analysis import Analysis

_CHART_W = 1160
_CHART_H = 220
_PAD_L = 54
_PAD_B = 26

_UTILIZATION_CAPTION = (
    "CU consumption as a share of the capacity, per 30-second window (peak per bucket)"
)


def _e(text: object) -> str:
    return html.escape(str(text))


def _pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def _downsample(values: list[float], buckets: int) -> list[float]:
    """Peak per bucket, matching how the Metrics app collapses timepoints when zoomed out."""
    if not values:
        return []
    if len(values) <= buckets:
        return values
    step = len(values) / buckets
    return [
        max(values[int(i * step) : max(int((i + 1) * step), int(i * step) + 1)] or [0.0])
        for i in range(buckets)
    ]


def _area_chart(values: list[float], *, threshold: float = 1.0, title: str = "") -> str:
    """Filled area chart scaled so the 100% threshold always sits on the grid."""
    points = _downsample(values, _CHART_W - _PAD_L)
    if not points:
        return '<p class="empty">No data in range.</p>'
    top = max(max(points), threshold) * 1.15
    height = _CHART_H - _PAD_B

    def y(value: float) -> float:
        return height - (value / top) * height

    coords = " ".join(f"{_PAD_L + i},{y(v):.1f}" for i, v in enumerate(points))
    area = f"{_PAD_L},{height} {coords} {_PAD_L + len(points) - 1},{height}"
    over = [
        f'<rect x="{_PAD_L + i}" y="0" width="1" height="{height}" class="over"/>'
        for i, v in enumerate(points)
        if v > threshold
    ]
    grid = []
    for fraction in (0.25, 0.5, 0.75, 1.0):
        value = top * fraction
        grid.append(
            f'<line x1="{_PAD_L}" y1="{y(value):.1f}" x2="{_CHART_W}" '
            f'y2="{y(value):.1f}" class="grid"/>'
            f'<text x="{_PAD_L - 8}" y="{y(value) + 4:.1f}" class="ylab">'
            f'{value * 100:.0f}%</text>'
        )
    return (
        f'<figure><figcaption>{_e(title)}</figcaption>'
        f'<svg viewBox="0 0 {_CHART_W} {_CHART_H}" role="img" aria-label="{_e(title)}">'
        + "".join(over)
        + "".join(grid)
        + f'<line x1="{_PAD_L}" y1="{y(threshold):.1f}" x2="{_CHART_W}" '
        + f'y2="{y(threshold):.1f}" class="limit"/>'
        + f'<polygon points="{area}" class="area"/>'
        + f'<polyline points="{coords}" class="line"/>'
        + "</svg></figure>"
    )


def _multi_line(series: list[tuple[str, list[float], str]], title: str) -> str:
    """Overlaid line chart for the three throttling horizons."""
    prepared = [(label, _downsample(values, _CHART_W - _PAD_L), css) for label, values, css in series]
    prepared = [item for item in prepared if item[1]]
    if not prepared:
        return '<p class="empty">No data in range.</p>'
    top = max(max(max(values) for _, values, _ in prepared), 1.0) * 1.15
    height = _CHART_H - _PAD_B

    def y(value: float) -> float:
        return height - (value / top) * height

    paths = []
    legend = []
    for label, values, css in prepared:
        coords = " ".join(f"{_PAD_L + i},{y(v):.1f}" for i, v in enumerate(values))
        paths.append(f'<polyline points="{coords}" class="line {css}"/>')
        legend.append(f'<span class="key {css}">■</span>{_e(label)}')
    return (
        f'<figure><figcaption>{_e(title)}</figcaption>'
        f'<svg viewBox="0 0 {_CHART_W} {_CHART_H}" role="img" aria-label="{_e(title)}">'
        + f'<line x1="{_PAD_L}" y1="{y(1.0):.1f}" x2="{_CHART_W}" '
        + f'y2="{y(1.0):.1f}" class="limit"/>'
        + f'<text x="{_PAD_L - 8}" y="{y(1.0) + 4:.1f}" class="ylab">100%</text>'
        + "".join(paths)
        + "</svg>"
        + f'<div class="legend">{" &nbsp; ".join(legend)}</div></figure>'
    )


def _daily_bars(analysis: Analysis) -> str:
    budget = analysis.capacity.daily_budget_cu_seconds()
    if not analysis.days or not budget:
        return ""
    rows = []
    peak = max((d.cu_seconds for d in analysis.days), default=1.0) or 1.0
    for day in analysis.days:
        util = day.cu_seconds / budget
        width = day.cu_seconds / peak * 100
        css = "bar over" if util > 1.0 else "bar"
        rows.append(
            f'<tr><td class="mono">{_e(day.date)}</td>'
            f'<td class="mono num">{day.cu_seconds:,.0f}</td>'
            f'<td class="mono num">{util * 100:.1f}%</td>'
            f'<td class="mono num">{day.operations}</td>'
            f'<td class="barcell">'
            f'<span class="{css}" style="width:{width:.1f}%"></span></td></tr>'
        )
    return (
        '<table class="grid-table"><thead><tr><th>Date</th>'
        '<th class="num">CU-s</th><th class="num">Utilization</th>'
        '<th class="num">Runs</th><th></th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _items_table(analysis: Analysis, top: int) -> str:
    if not analysis.items:
        return ""
    rows = []
    for row in analysis.items[:top]:
        delta = row.performance_delta
        delta_text = "—" if delta is None else f"{delta:+.0f}%"
        delta_css = "" if delta is None else ("up" if delta > 10 else "down" if delta < -10 else "")
        mark = "" if row.exactness == "exact" else ' <span class="est">est</span>'  # noqa: E501
        rows.append(
            f"<tr><td>{_e(row.item_name)}{mark}</td>"
            f"<td>{_e(row.item_kind)}</td>"
            f'<td>{_e(row.workspace)}</td>'
            f'<td class="mono num">{row.cu_seconds:,.0f}</td>'
            f'<td class="mono num">{row.duration_seconds:,.0f}</td>'
            f'<td class="mono num">{row.operations}</td>'
            f'<td class="mono num">{row.failed or ""}</td>'
            f'<td class="mono num {delta_css}">{delta_text}</td></tr>'
        )
    return (
        '<table class="grid-table"><thead><tr><th>Item</th><th>Kind</th><th>Workspace</th>'
        '<th class="num">CU-s</th><th class="num">Duration s</th><th class="num">Runs</th>'
        '<th class="num">Failed</th><th class="num">Δ 7d</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


_CSS = """
:root{color-scheme:light dark;
--bg:#fbfbfa;--panel:#fff;--ink:#1c1c1a;--muted:#6b6b66;--line:#e3e3df;
--accent:#3b6ea5;--accent-soft:#3b6ea51f;--warn:#b4531f;--bad:#a32f2f;--good:#2f7d4f;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--bg:#16181a;--panel:#1e2124;--ink:#e6e6e3;--muted:#9a9a94;--line:#2e3236;
--accent:#7aa9dd;--accent-soft:#7aa9dd24;--warn:#e0904f;--bad:#e07a7a;
--good:#6fc08f;}}
:root[data-theme="dark"]{--bg:#16181a;--panel:#1e2124;--ink:#e6e6e3;--muted:#9a9a94;
--line:#2e3236;--accent:#7aa9dd;--accent-soft:#7aa9dd24;--warn:#e0904f;
--bad:#e07a7a;--good:#6fc08f;}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);margin:0;padding:32px 24px 64px;
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1220px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.07em;
color:var(--muted);margin:34px 0 10px;font-weight:600}
.sub{color:var(--muted);margin:0 0 26px;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.card .value{font-size:24px;font-variant-numeric:tabular-nums;margin-top:4px;letter-spacing:-.02em}
.card.bad .value{color:var(--bad)}.card.good .value{color:var(--good)}
figure{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:14px 16px 8px;margin:0 0 14px;overflow-x:auto}
figcaption{color:var(--muted);font-size:12px;margin-bottom:6px}
svg{width:100%;height:auto;min-width:640px;display:block}
.grid{stroke:var(--line);stroke-width:1}
.ylab{fill:var(--muted);font-size:10px;text-anchor:end}
.limit{stroke:var(--bad);stroke-width:1.2;stroke-dasharray:4 3}
.area{fill:var(--accent-soft)}
.line{fill:none;stroke:var(--accent);stroke-width:1.3}
.line.delay{stroke:var(--warn)}.line.reject{stroke:var(--bad)}.line.bg{stroke:var(--accent)}
.over{fill:var(--bad);opacity:.14}
.legend{font-size:12px;color:var(--muted);padding:4px 0 8px}
.key{margin-right:4px}.key.delay{color:var(--warn)}
.key.reject{color:var(--bad)}.key.bg{color:var(--accent)}
table.grid-table{width:100%;border-collapse:collapse;background:var(--panel);
border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:7px 11px;text-align:left;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}
tr:last-child td{border-bottom:none}
.num{text-align:right}
.mono{font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.up{color:var(--bad)}.down{color:var(--good)}
.est{font-size:10px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:0 4px}
.barcell{width:34%}
.bar{display:block;height:9px;background:var(--accent);border-radius:3px}
.bar.over{background:var(--bad)}
.note{background:var(--panel);border:1px solid var(--line);border-radius:8px;
border-left:3px solid var(--warn);padding:11px 14px;margin:0 0 10px;
color:var(--ink);font-size:13px}
.note.plain{border-left-color:var(--line);color:var(--muted)}
.empty{color:var(--muted);padding:20px 0}
footer{margin-top:40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:14px}
"""


def render(analysis: Analysis, *, top: int = 25) -> str:
    capacity = analysis.capacity
    breaches = analysis.breaches
    risky = any(
        breaches[k] for k in ("interactive_delay", "interactive_rejection", "background_rejection")
    )
    utilization = [w.utilization for w in analysis.windows]

    cards = [
        ("SKU", capacity.sku or "—", ""),
        ("Average utilization", _pct(analysis.average_utilization), ""),
        (
            "Peak utilization",
            _pct(analysis.peak_utilization),
            "bad" if (analysis.peak_utilization or 0) > 1 else "",
        ),
        ("Total CU-s", f"{analysis.total_cu_seconds:,.0f}", ""),
        ("Operations", f"{analysis.total_operations:,}", ""),
        ("Users", str(len(analysis.users)), ""),
        ("Throttled windows", str(max(breaches.values())), "bad" if risky else "good"),
    ]
    card_html = "".join(
        f'<div class="card {css}"><div class="label">{_e(label)}</div>'
        f'<div class="value">{_e(value)}</div></div>'
        for label, value, css in cards
    )

    notes = "".join(
        f'<p class="note">{_e(w)}</p>' for w in analysis.collection.warnings
    ) + "".join(
        f'<p class="note plain">Not counted: {_e(u)}</p>' for u in analysis.collection.unaccounted
    )

    item_heading = (
        "By Spark session — attributed to the notebook that opened the session"
        if analysis.collection.high_concurrency_present
        else "By item"
    )

    cost = ""
    if analysis.cost_rows:
        rows = "".join(
            f'<tr><td>{_e(sub)}</td><td>{_e(meter)}</td>'
            f'<td class="mono num">{amount:,.2f}</td></tr>'
            for amount, sub, meter in analysis.cost_rows[:12]
        )
        cost = (
            "<h2>Azure cost by meter</h2>"
            '<table class="grid-table"><thead><tr><th>Sub-category</th><th>Meter</th>'
            f'<th class="num">Cost</th></tr></thead><tbody>{rows}</tbody></table>'
        )

    events = ""
    if analysis.activity_events:
        rows = "".join(
            f'<tr><td class="mono">{_e(e.get("eventTimestamp", "")[:19])}</td>'
            f'<td>{_e((e.get("operationName") or {}).get("localizedValue", "?"))}</td>'
            f'<td>{_e((e.get("status") or {}).get("localizedValue", ""))}</td></tr>'
            for e in analysis.activity_events[:15]
        )
        events = (
            "<h2>System events</h2>"
            '<table class="grid-table"><thead><tr><th>Time (UTC)</th><th>Operation</th>'
            f"<th>Status</th></tr></thead><tbody>{rows}</tbody></table>"
        )

    return f"""<title>Capacity · {_e(capacity.name)}</title>
<style>{_CSS}</style>
<div class="wrap">
<h1>{_e(capacity.name)}</h1>
<p class="sub">{_e(capacity.sku or "?")} · {_e(capacity.region or "?")} ·
{_e(capacity.state or "state unknown")} ·
{analysis.start:%Y-%m-%d %H:%M}–{analysis.end:%Y-%m-%d %H:%M} UTC ·
{len(capacity.workspaces)} workspace(s) · rates as of {_e(analysis.rates.as_of)}</p>

<div class="cards">{card_html}</div>

<h2>Capacity utilization</h2>
{_area_chart(utilization, title=_UTILIZATION_CAPTION)}

<h2>Throttling</h2>
{_multi_line([
    ("Interactive delay (10 min)", [w.interactive_delay for w in analysis.windows], "delay"),
    ("Interactive rejection (60 min)", [w.interactive_reject for w in analysis.windows], "reject"),
    ("Background rejection (24 h)", [w.background_reject for w in analysis.windows], "bg"),
], "Forward-window mean utilization. Crossing 100% starts the matching throttle.")}

<h2>Overage carryforward</h2>
{_area_chart(
    [w.carry_cumulative / (capacity.base_cu * 30) if capacity.base_cu else 0.0 for w in analysis.windows],
    threshold=1.0,
    title="Cumulative carryforward, as a multiple of one window's CU budget",
)}

<h2>Daily</h2>
{_daily_bars(analysis)}

<h2>{_e(item_heading)}</h2>
{_items_table(analysis, top)}

{cost}
{events}

<h2>Notes</h2>
{notes or '<p class="note plain">No warnings.</p>'}

<footer>Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by fabric-capacity-monitor.
Spark CU is computed exactly from session duration and pool vCores; other workloads are
estimated from published rates and marked <span class="est">est</span>.</footer>
</div>"""
