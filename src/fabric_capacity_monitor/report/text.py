"""Plain-text report. No colour, no dependencies, pipe-friendly."""

from __future__ import annotations

from .analysis import Analysis

WIDTH = 78


def _rule(char: str = "=") -> str:
    return char * WIDTH


def _pct(value: float | None) -> str:
    return "  n/a  " if value is None else f"{value * 100:6.1f}%"


def _num(value: float) -> str:
    return f"{value:,.0f}"


def _sparkline(values: list[float], width: int = 60) -> str:
    """A coarse utilization sparkline; '!' marks any bucket that went over budget."""
    blocks = " ▁▂▃▄▅▆▇█"
    if not values:
        return ""
    step = max(len(values) / width, 1.0)
    out = []
    for index in range(min(width, len(values))):
        chunk = values[int(index * step) : max(int((index + 1) * step), int(index * step) + 1)]
        peak = max(chunk) if chunk else 0.0
        if peak > 1.0:
            out.append("!")
        else:
            out.append(blocks[min(int(peak * (len(blocks) - 1)), len(blocks) - 1)])
    return "".join(out)


def render(analysis: Analysis, *, by_item: bool = False, by_workspace: bool = False, top: int = 15) -> str:
    capacity = analysis.capacity
    budget = capacity.daily_budget_cu_seconds()
    lines: list[str] = []

    lines.append(_rule())
    title = f" Capacity report: {capacity.name}"
    detail = f"{capacity.sku or '?'} · {capacity.region or '?'} · {capacity.state or 'unknown'}"
    lines.append(f"{title}  [{detail}]")
    lines.append(
        f" {analysis.start:%Y-%m-%d %H:%M} to {analysis.end:%Y-%m-%d %H:%M} UTC"
        f" · rates as of {analysis.rates.as_of}"
    )
    lines.append(_rule())
    lines.append("")

    lines.append(f"  Average utilization   {_pct(analysis.average_utilization)}   (active windows only)")
    lines.append(f"  Peak utilization      {_pct(analysis.peak_utilization)}")
    if budget:
        lines.append(f"  Daily CU budget       {_num(budget)} CU-s  ({capacity.base_cu:g} CU)")
    lines.append(f"  Total CU consumed     {_num(analysis.total_cu_seconds)} CU-s")
    lines.append(f"  Operations            {analysis.total_operations:,}")
    lines.append(f"  Distinct users        {len(analysis.users)}")
    lines.append("")

    breaches = analysis.breaches
    lines.append("  Throttling (windows over threshold)")
    lines.append(f"    Interactive delay      {breaches['interactive_delay']:>6}   (forward 10 min)")
    lines.append(f"    Interactive rejection  {breaches['interactive_rejection']:>6}   (forward 60 min)")
    lines.append(f"    Background rejection   {breaches['background_rejection']:>6}   (forward 24 h)")
    lines.append(f"    Over 100% in-window    {breaches['over_capacity']:>6}")
    lines.append("")

    if analysis.windows:
        lines.append("  Utilization over time (peak per bucket, '!' = over budget)")
        lines.append(f"    {_sparkline([w.utilization for w in analysis.windows])}")
        lines.append(
            f"    {analysis.start:%m-%d}"
            + " " * 52
            + f"{analysis.end:%m-%d}"
        )
        lines.append("")

    lines.append(f"  {'DATE':<12}{'CU-s':>12}{'UTIL':>8}{'RUNS':>7}{'FAILED':>8}{'QUEUED':>9}")
    lines.append(f"  {'-' * 12}{'-' * 12:>12}{'-' * 7:>8}{'-' * 6:>7}{'-' * 7:>8}{'-' * 8:>9}")
    for day in analysis.days:
        util = day.utilization(budget)
        lines.append(
            f"  {day.date:<12}{_num(day.cu_seconds):>12}{_pct(util):>8}"
            f"{day.operations:>7}{day.failed:>8}{_num(day.queued_seconds) + 's':>9}"
        )
    lines.append("")

    if by_workspace and analysis.workspaces:
        lines.append("  BY WORKSPACE")
        lines.append(f"  {'WORKSPACE':<34}{'CU-s':>12}{'SHARE':>8}{'RUNS':>7}")
        for row in analysis.workspaces[:top]:
            share = row.cu_seconds / analysis.total_cu_seconds if analysis.total_cu_seconds else 0
            lines.append(
                f"  {row.item_name[:32]:<34}{_num(row.cu_seconds):>12}{_pct(share):>8}{row.operations:>7}"
            )
        lines.append("")

    if by_item and analysis.items:
        if analysis.collection.high_concurrency_present:
            lines.append("  BY SPARK SESSION (attributed to the notebook that OPENED the session)")
            lines.append("  Not per-notebook: see the note below.")
        else:
            lines.append("  BY ITEM")
        header = f"  {'ITEM':<38}{'KIND':<12}{'CU-s':>11}{'RUNS':>6}{'FAIL':>6}{'Δ7d':>8}"
        lines.append(header)
        for row in analysis.items[:top]:
            delta = row.performance_delta
            delta_text = "     n/a" if delta is None else f"{delta:+7.0f}%"
            mark = "" if row.exactness == "exact" else " ~"
            lines.append(
                f"  {(row.item_name + mark)[:36]:<38}{row.item_kind[:10]:<12}"
                f"{_num(row.cu_seconds):>11}{row.operations:>6}{row.failed:>6}{delta_text:>8}"
            )
        lines.append("")
        lines.append("  ~ = estimated, not measured")
        lines.append("")

    if analysis.activity_events:
        lines.append("  SYSTEM EVENTS")
        for event in analysis.activity_events[:10]:
            when = event.get("eventTimestamp", "")[:19]
            what = (event.get("operationName") or {}).get("localizedValue", "?")
            status = (event.get("status") or {}).get("localizedValue", "")
            lines.append(f"    {when}  {what}  {status}")
        lines.append("")

    if analysis.cost_rows:
        lines.append("  AZURE COST BY METER")
        for amount, sub_category, meter in analysis.cost_rows[:10]:
            lines.append(f"    {amount:>10,.2f}  {sub_category} / {meter}")
        lines.append("")

    if analysis.collection.warnings:
        lines.append("  NOTES")
        for warning in analysis.collection.warnings:
            for index, chunk in enumerate(_wrap(warning, WIDTH - 6)):
                lines.append(("    - " if index == 0 else "      ") + chunk)
        lines.append("")

    if analysis.collection.unaccounted:
        lines.append("  NOT COUNTED IN THE TOTALS ABOVE")
        for entry in analysis.collection.unaccounted:
            for index, chunk in enumerate(_wrap(entry, WIDTH - 6)):
                lines.append(("    - " if index == 0 else "      ") + chunk)
        lines.append("")

    worst = max(
        breaches["interactive_delay"],
        breaches["interactive_rejection"],
        breaches["background_rejection"],
    )
    verdict = "THROTTLING RISK" if worst else "healthy"
    lines.append(_rule())
    peak = _pct(analysis.peak_utilization).strip()
    lines.append(f"SUMMARY: {verdict} · peak {peak} of {capacity.sku or '?'}")
    lines.append(_rule())
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    out: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width and current:
            out.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        out.append(current)
    return out
