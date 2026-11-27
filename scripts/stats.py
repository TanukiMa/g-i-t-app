"""stats.html: how many updates were detected per day, month, year and weekday, and how the monitored URLs grew.

The charts are SVG drawn here at build time (no script, no library); a value is shown by the browser's own tooltip
(<title>), above the bar where there are few bars, and in the lists of the page. An update = a row of `updates` that is
not the first snapshot of a site; days are JST days of the detection."""
import html
from collections import Counter
from datetime import date, timedelta

from markupsafe import Markup

from common import SUMMARY_INITIAL
from dashboard_text import jst_datetime

DAILY_SPAN = 90                                   # days in the daily chart
YOUBI = "月火水木金土日"
PLOT_H, TOP, BOTTOM, RIGHT = 180, 18, 22, 44      # the scale is on the RIGHT: a chart that scrolls starts at its right end


def _scale(highest):
    """(top of the axis, step): clean numbers, at most four steps."""
    step = 1
    while True:
        for s in (step, 2 * step, 5 * step):
            if highest <= 4 * s:
                return max(1, int(-(-highest // s))) * s, s
        step *= 10


def _y(value, top):
    return TOP + PLOT_H * (1 - value / top)


def _frame(width, top, step, label, body):
    grid = []
    for v in range(0, top + 1, step):
        y = _y(v, top)
        grid.append(f'<line class="st-grid" x1="0" y1="{y:.1f}" x2="{width}" y2="{y:.1f}"/>'
                    f'<text class="st-tick" x="{width + 6}" y="{y + 4:.1f}">{v:,}</text>')
    total, height = width + RIGHT, TOP + PLOT_H + BOTTOM
    return Markup(f'<svg width="{total}" height="{height}" viewBox="0 0 {total} {height}" role="img" '
                  f'aria-label="{html.escape(label)}">{"".join(grid)}{body}</svg>')


def bar_chart(label, bars, slot, width_of_bar, values=True, sundays=()):
    """bars: [(axis label or "", tooltip, value, value text)], oldest first. values=False: no number above the bars.
    `sundays`: indexes of the bars drawn in the Sunday colour."""
    top, step = _scale(max([b[2] for b in bars] + [1]))
    base, body = TOP + PLOT_H, []
    for i, (axis, tip, value, text) in enumerate(bars):
        left = i * slot + (slot - width_of_bar) / 2
        y = _y(value, top)
        r = min(4, width_of_bar / 2, base - y)
        shape = ""
        if value > 0:      # rounded at the data end, square at the baseline
            shape = (f'<path class="st-bar{" st-sun" if i in sundays else ""}" d="M{left:.1f},{base}V{y + r:.1f}q0,{-r:.1f} {r:.1f},{-r:.1f}'
                     f'h{width_of_bar - 2 * r:.1f}q{r:.1f},0 {r:.1f},{r:.1f}V{base}z"/>')
        number = f'<text class="st-value" x="{i * slot + slot / 2:.1f}" y="{y - 4:.1f}">{text}</text>' if values else ""
        body.append(f'<g><title>{html.escape(tip)}</title><rect class="st-hit" x="{i * slot}" y="{TOP}" width="{slot}" '
                    f'height="{PLOT_H}"/>{shape}{number}</g>')
        if axis:
            centered = values
            x = i * slot + slot / 2 if centered else i * slot
            body.append(f'<text class="st-tick{" st-mid" if centered else ""}" x="{x:.1f}" y="{base + 15}">{axis}</text>')
    return _frame(slot * len(bars), top, step, label, "".join(body))


def line_chart(label, points, width=640):
    """A step line over consecutive days. points: [(date, value)], oldest first."""
    top, step = _scale(max([v for _, v in points] + [1]))
    dx = width / max(1, len(points))
    path = "".join((f"M0,{_y(v, top):.1f}" if i == 0 else f"V{_y(v, top):.1f}") + f"H{(i + 1) * dx:.1f}"
                   for i, (_, v) in enumerate(points))
    (first_day, first), (last_day, last) = points[0], points[-1]
    base = TOP + PLOT_H
    body = (f'<path class="st-line" d="{path}"/>'
            f'<text class="st-value st-start" x="2" y="{_y(first, top) - 6:.1f}">{first:,}</text>'
            f'<text class="st-value st-end" x="{width - 2}" y="{_y(last, top) - 6:.1f}">{last:,}</text>'
            f'<text class="st-tick" x="0" y="{base + 15}">{first_day:%Y/%m/%d}</text>'
            f'<text class="st-tick st-end" x="{width}" y="{base + 15}">{last_day:%Y/%m/%d}</text>')
    hits = "".join(f'<rect class="st-hit" x="{i * dx:.1f}" y="{TOP}" width="{dx:.1f}" height="{PLOT_H}">'
                   f'<title>{d.isoformat()}: {v:,} 件</title></rect>' for i, (d, v) in enumerate(points))
    return _frame(width, top, step, label, body + hits)


def _days(first, last):
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def build_stats(configured: list, updates: list, today: date) -> dict:
    """Everything stats.html shows. `configured` = the sites of config.yaml, `today` = the JST date of the build."""
    counts, first_seen, first = Counter(), {}, today
    for u in updates:
        try:
            day = jst_datetime(u["created_at"]).date()
        except (KeyError, ValueError, TypeError):
            continue
        slug = u.get("site_slug")
        first = min(first, day)
        first_seen[slug] = min(day, first_seen.get(slug, day))
        if u.get("summary") != SUMMARY_INITIAL:
            counts[day] += 1

    slugs = {s["slug"]: s for s in configured}
    active = [s for s in slugs.values() if not s.get("paused")]
    result = {"monitored": len(active), "paused": len(slugs) - len(active), "total": sum(counts.values()),
              "since": first.isoformat(), "daily_span": DAILY_SPAN}

    span = _days(max(first, today - timedelta(days=DAILY_SPAN - 1)), today)
    name = {d: f"{d.isoformat()}（{YOUBI[d.weekday()]}）" for d in span}
    result["daily_chart"] = bar_chart(
        "日別の更新検知件数", [(f"{d.month}/{d.day}" if d.weekday() == 0 else "", f"{name[d]}: {counts[d]:,} 件",
                               counts[d], "") for d in span], slot=10, width_of_bar=8, values=False,
        sundays={i for i, d in enumerate(span) if d.weekday() == 6})
    result["daily"] = [(name[d], counts[d]) for d in reversed(span)]

    def periods(title, key_of, axis_of, label_of, slot):
        sums = Counter()
        for d in _days(first, today):     # periods without an update are shown too
            sums[key_of(d)] += counts[d]
        keys = sorted(sums)
        chart = bar_chart(title, [(axis_of(k), f"{label_of(k)}: {sums[k]:,} 件", sums[k], f"{sums[k]:,}") for k in keys],
                          slot=slot, width_of_bar=24)
        return chart, [(label_of(k), sums[k]) for k in reversed(keys)]

    this_month, this_year = (today.year, today.month), today.year
    result["monthly_chart"], result["monthly"] = periods(
        "月別の更新検知件数", lambda d: (d.year, d.month), lambda k: f"{k[0]}/{k[1]}",
        lambda k: f"{k[0]}年{k[1]}月" + ("（途中）" if k == this_month else ""), slot=64)
    result["yearly_chart"], result["yearly"] = periods(
        "年別の更新検知件数", lambda d: d.year, lambda k: f"{k}年",
        lambda k: f"{k}年" + (f"（{first.month}/{first.day}から）" if k == first.year else "")
        + ("（途中）" if k == this_year else ""), slot=80)

    # Averages over whole days only: the first day of the record and today are partial.
    whole = _days(first, today)[1:-1]
    weekday = []
    for i, youbi in enumerate(YOUBI):
        values = [counts[d] for d in whole if d.weekday() == i]
        weekday.append((youbi, sum(values) / len(values) if values else 0))
    result["weekday_chart"] = bar_chart(
        "曜日別の1日あたりの平均件数", [(y, f"{y}曜日: 平均 {v:.1f} 件", v, f"{v:.0f}") for y, v in weekday],
        slot=48, width_of_bar=24)
    result["weekday"] = [(f"{y}曜日", f"{v:.1f}") for y, v in weekday]

    # When did the record of each currently monitored URL begin? (A site not fetched yet has no row and is not counted.)
    started = Counter(first_seen[s["slug"]] for s in active if s["slug"] in first_seen)
    total, points = 0, []
    for d in _days(first, today):
        total += started[d]
        points.append((d, total))
    result["growth_chart"] = line_chart("監視している URL 数の推移", points)
    return result
