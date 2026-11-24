"""What a diff really changes. Pure functions, no network, no git: used by the pipeline (Re-baseline, reorder check, AI
input, new links) and by the analysis tools (find_flips, find_micro_changes, rebuild_site_history, resummarize ...)."""
import html
import json
import re
from collections import Counter
from functools import cached_property

_TAG_RE = re.compile(r"<[^>]*>")


def _has_content(text: str) -> bool:
    """A line worth showing to the model: visible text, or a link / image address (a new document)."""
    if _TAG_RE.sub("", text).strip():
        return True
    return "href=" in text or "src=" in text


def _json_object(text: str):
    """The dict of a line that is one JSON object (a record of a `json_records` file), else None."""
    t = text.rstrip(",")
    if not (t.startswith("{") and t.endswith("}")):
        return None
    try:
        obj = json.loads(t)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) and len(obj) >= 2 else None


def compact_records(lines: list) -> list:
    """[(sign, text)] with every changed record shown by its identity and the fields that changed.

    A `json_records` file has one record per line, the label and id fields first. A record that was changed appears as a
    removed and an added line with the same two first values: both lines are shortened to those two fields plus the fields
    that differ, so that the AI sees `-{"product_nm": ..., "yj_cd": ..., "shipment_status_nm": "①通常出荷"}` /
    `+{... "shipment_status_nm": "③限定出荷"}` instead of two lines of 700 characters. Records that were only added or
    removed, ambiguous keys and every other kind of line are left as they are."""
    parsed = [(sign, text, _json_object(text)) for sign, text in lines]
    index = {"-": {}, "+": {}}
    for i, (sign, _text, obj) in enumerate(parsed):
        if obj is not None:
            index[sign].setdefault(repr(tuple(list(obj.values())[:2])), []).append(i)
    out = {i: text for i, (_sign, text, _obj) in enumerate(parsed)}
    for key, minus in index["-"].items():
        plus = index["+"].get(key)
        if not plus or len(minus) != 1 or len(plus) != 1:
            continue
        old, new = parsed[minus[0]][2], parsed[plus[0]][2]
        names = list(old)[:2]
        changed = [k for k in dict.fromkeys([*old, *new]) if k not in names and old.get(k) != new.get(k)]
        if not changed:
            continue
        out[minus[0]] = json.dumps({**{k: old[k] for k in names}, **{k: old.get(k) for k in changed}}, ensure_ascii=False)
        out[plus[0]] = json.dumps({**{k: new[k] for k in names}, **{k: new.get(k) for k in changed}}, ensure_ascii=False)
    return [(sign, out[i]) for i, (sign, _text, _obj) in enumerate(parsed)]


class DiffAnalysis:
    """What a diff REALLY changes: the one place that counts lines, so that the summary, the reorder check and the
    Re-baseline check can never disagree about the same diff.

    Per changed file the lines that were removed and added are compared (stripped of leading/trailing white space, blank
    lines ignored, counted as a multiset): a line that was removed AND added the same number of times only moved (a list
    shifted by one item shows every title on both sides). What is left is the net change, in its original order.
      .moved_only   nothing but moved lines in every changed file (is_reorder_only)
      .text()       the net change for the AI, without lines that carry neither visible text nor a link (</div>, <li> ...)
      .added_text() visible text that is really added: a rule that ignores or removes something can never add a sentence,
                    a title or a date, so this is how a real addition is told from the effect of a rule change
    """

    def __init__(self, diff_text: str):
        self.files = []              # [{"path", "hunks", "added": Counter, "removed": Counter, "lines": [(sign, text)]}]
        current, in_hunk = None, False
        for line in diff_text.splitlines():
            if line.startswith("diff --git "):
                m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
                current = {"path": (m.group(2) if m else line[11:]), "hunks": 0, "added": Counter(), "removed": Counter(), "lines": []}
                self.files.append(current)
                in_hunk = False
            elif current is None:
                continue
            elif line.startswith("@@"):
                in_hunk = True
                current["hunks"] += 1
            elif in_hunk and line[:1] in ("+", "-"):
                text = line[1:].strip()
                if text:
                    current["added" if line[0] == "+" else "removed"][text] += 1
                    current["lines"].append((line[0], text))

    @property
    def has_text_hunks(self) -> bool:
        """False for a binary or mode-only diff: there is nothing to read."""
        return any(f["hunks"] for f in self.files)

    @property
    def moved_only(self) -> bool:
        """True when, in every changed file, the lines added are exactly the lines removed."""
        if not self.files:
            return False
        return all(f["hunks"] and f["added"] == f["removed"] and f["added"] for f in self.files)

    @cached_property
    def net_files(self) -> list:
        """[(path, [(sign, text)])]: the lines that really changed, in order, without lines that carry no content."""
        out = []
        for f in self.files:
            net = {"+": f["added"] - f["removed"], "-": f["removed"] - f["added"]}
            kept = []
            for sign, text in f["lines"]:
                if net[sign][text] > 0 and _has_content(text):
                    net[sign][text] -= 1
                    kept.append((sign, text))
            if kept:
                out.append((f["path"], kept))
        return out

    def text(self):
        """The net change as text for the summary. "" when nothing is left (only markup changed); None when the diff has
        no text hunks at all (binary / mode change): use the raw diff then."""
        if not self.has_text_hunks:
            return None
        return "\n".join(f"=== {path}\n" + "\n".join(f"{sign}{text}" for sign, text in compact_records(lines))
                         for path, lines in self.net_files)

    def added_markup(self) -> str:
        """The lines that are really added (net: a line that only moved is not here), as one string of HTML; the
        links of these lines are the NEW links of the page."""
        return "\n".join(text for _path, ls in self.net_files for sign, text in ls if sign == "+")

    def added_text(self) -> list:
        """Visible text that is added and not also removed. Markup, attributes and addresses do not count."""
        lines = [item for _path, ls in self.net_files for item in ls]
        removed = {text for sign, text in lines if sign == "-"}
        return [text for sign, text in lines
                if sign == "+" and "<" not in text and "href=" not in text and "src=" not in text
                and not re.match(r"^(https?:)?//|^/", text) and text not in removed]


def analyze_diff(diff_text: str) -> DiffAnalysis:
    return DiffAnalysis(diff_text)


def net_diff(diff_text: str):
    """The real additions and removals of a diff, for the AI summary (see DiffAnalysis.text)."""
    return DiffAnalysis(diff_text).text()


def added_text_lines(diff_text: str) -> list:
    """Visible text that a diff really adds (see DiffAnalysis.added_text): non-empty = a real addition."""
    return DiffAnalysis(diff_text).added_text()


def is_reorder_only(diff_text: str) -> bool:
    """True when the diff only moves lines around (see DiffAnalysis.moved_only): tabs that swap places, a list that is
    shuffled on every request. Nothing was added, removed or reworded, so there is nothing to report."""
    return DiffAnalysis(diff_text).moved_only


def visible_len(text: str) -> int:
    """Number of visible characters of an HTML (or plain) text: no tags, scripts, styles or white space."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]*>", " ", text)
    return len("".join(text.split()))


_TAG = re.compile(r"<[^>]*>")


def visible(text: str) -> str:
    """The text a reader would see in one stored line: tags removed, entities decoded, white space collapsed."""
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", text))).strip()
