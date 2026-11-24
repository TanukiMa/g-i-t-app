"""Text for the dashboard pages: times in JST, the small Markdown subset of the summaries, excerpts, and the grouping of
updates by week, month and day. Pure functions (Jinja filters and helpers of the other dashboard modules)."""
import html
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from markupsafe import Markup

from common import SUMMARY_FAILED, SUMMARY_INITIAL, SUMMARY_UNAVAILABLE

JST = timezone(timedelta(hours=9))  # no DST, so a fixed offset is exact and needs no tzdata


def parse_timestamp(value) -> datetime:
    """Parse a Supabase timestamptz string (fraction may have 1-6 digits)."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        text = re.sub(r"\.(\d+)", lambda m: "." + m.group(1).ljust(6, "0")[:6], text, count=1)
        dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")  # fixed: no locale needed


def to_jst(value, with_suffix: bool = True, weekday=None) -> str:
    """JST time text. Full display: "2026-10-07 Wednesday 09:46 JST".

    with_suffix=False gives the short "2026-10-07 09:46" (callers slice it: [:10] date, [11:] time);
    weekday=True puts the English weekday name after the date in that form too (default: only with the suffix).
    """
    if weekday is None:
        weekday = with_suffix
    try:
        dt = parse_timestamp(value).astimezone(JST)
    except (ValueError, TypeError):
        return str(value or "")
    day = f"{dt:%Y-%m-%d} {WEEKDAYS[dt.weekday()]}" if weekday else f"{dt:%Y-%m-%d}"
    text = f"{day} {dt:%H:%M}"
    return f"{text} JST" if with_suffix else text


def hostname(url) -> str:
    host = urlparse(str(url)).netloc
    return host[4:] if host.startswith("www.") else (host or str(url))


_BULLET = re.compile(r"^(\s*)([*+\-]|\d+[.)])\s+(.*)$")


_HEADING = re.compile(r"^#{1,6}\s+(.*)$")


def _inline(text: str) -> str:
    text = html.escape(text, quote=True)  # escape first: nothing from the source can become markup
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def render_markdown(text) -> Markup:
    """Tiny, safe Markdown subset for AI summaries: bullets (nested), **bold**, `code`, paragraphs."""
    out, indents, para = [], [], []

    def flush_para():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def close_lists():
        while indents:
            out.append("</li></ul>")
            indents.pop()

    for raw in str(text or "").splitlines():
        if not raw.strip():
            flush_para()
            close_lists()
            continue
        bullet = _BULLET.match(raw.expandtabs(4))
        if bullet:
            flush_para()
            depth, content = len(bullet.group(1)), bullet.group(3)
            if not indents or depth > indents[-1]:
                out.append("<ul>")
                indents.append(depth)
            else:
                while len(indents) > 1 and depth < indents[-1]:
                    out.append("</li></ul>")
                    indents.pop()
                out.append("</li>")
            out.append("<li>" + _inline(content))
        elif indents:
            out.append(" " + _inline(raw.strip()))  # continuation of the current list item
        else:
            heading = _HEADING.match(raw.strip())
            if heading:
                flush_para()
                out.append("<p><strong>" + _inline(heading.group(1)) + "</strong></p>")
            else:
                para.append(raw.strip())
    flush_para()
    close_lists()
    return Markup("".join(out))


def summary_by(model) -> str:
    """The line at the end of a summary: "<model> による要約". Nothing ("") when the model is unknown (older rows) or no AI
    was used (summary_model "rule": the text says by itself that nothing changed)."""
    model = str(model or "").strip()
    return "" if model in ("", "rule") else f"{model} による要約"


def excerpt(summary, limit: int = 90) -> str:
    """One plain-text line of an AI summary for the site list ("" when there is nothing useful)."""
    if not summary or summary in (SUMMARY_INITIAL, SUMMARY_FAILED, SUMMARY_UNAVAILABLE):
        return ""
    for raw in str(summary).splitlines():
        line = re.sub(r"^\s*(?:[*+\-]|\d+[.)]|#{1,6})\s+", "", raw).replace("**", "").replace("`", "").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def plain_summary(summary) -> str:
    """Whole AI summary as one plain-text line for the search index ("" when there is no real summary)."""
    if not summary or summary in (SUMMARY_INITIAL, SUMMARY_FAILED, SUMMARY_UNAVAILABLE):
        return ""
    lines = (re.sub(r"^\s*(?:[*+\-]|\d+[.)]|#{1,6})\s+", "", raw).replace("**", "").replace("`", "").strip()
             for raw in str(summary).splitlines())
    return " ".join(line for line in lines if line)


def iso_attr(value) -> str:
    """A time stamp for a <time datetime="..."> attribute: HTML allows at most three digits of a second's fraction, and the
    database gives six (2026-10-11T07:33:30.134784+00:00)."""
    return re.sub(r"(\.\d{3})\d+", r"\g<1>", str(value))


def jst_datetime(value) -> datetime:
    return parse_timestamp(value).astimezone(JST)


def group_periods(updates: list):
    """Group updates (newest first) by JST month and ISO week.

    Returns (months, weeks): lists of {key, label, updates, count}, newest first.
    """
    months, weeks = {}, {}
    for u in updates:
        try:
            dt = jst_datetime(u["created_at"])
        except (KeyError, ValueError, TypeError):
            continue
        mkey = f"{dt.year}-{dt.month:02d}"
        iso = dt.isocalendar()
        wkey = f"{iso[0]}-W{iso[1]:02d}"
        months.setdefault(mkey, {"key": mkey, "label": f"{dt.year}年{dt.month}月", "updates": []})["updates"].append(u)
        if wkey not in weeks:
            monday = date.fromisocalendar(iso[0], iso[1], 1)
            sunday = monday + timedelta(days=6)
            weeks[wkey] = {"key": wkey, "label": f"{iso[0]}年第{iso[1]}週",
                           "range": f"{monday.month}/{monday.day}〜{sunday.month}/{sunday.day}", "updates": []}
        weeks[wkey]["updates"].append(u)

    def finish(groups):
        out = sorted(groups.values(), key=lambda g: g["key"], reverse=True)
        for g in out:
            g["count"] = len(g["updates"])
        return out

    return finish(months), finish(weeks)


def group_by_day(updates: list) -> list:
    """[(YYYY-MM-DD (曜), [updates])] newest first, JST days."""
    youbi = "月火水木金土日"
    days = {}
    for u in updates:
        dt = jst_datetime(u["created_at"])
        days.setdefault(dt.date(), []).append(u)
    return [(f"{d.isoformat()}（{youbi[d.weekday()]}）", ups) for d, ups in sorted(days.items(), reverse=True)]
