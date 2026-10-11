"""Write the `tags:` of every site (and the `tag_groups:` block) into config.yaml, keeping comments and layout.

    python scripts/apply_tags.py --data-dir ../g-i-t-data --mapping ../tags_proposal.yaml          # shows the diff only
    python scripts/apply_tags.py --data-dir ../g-i-t-data --mapping ../tags_proposal.yaml --apply  # writes config.yaml

The mapping is a YAML file `slug: [tag, ...]` (a `# name` comment after each line is ignored). config.yaml is edited as
text: an existing `tags:` line of a site is replaced, otherwise one is inserted after the site's `slug:` line (or its
`name:` / `url:` line); nothing else is touched. Sites that are not in the mapping keep what they have. Afterwards run
check_config.py, look at `git diff`, and commit.
"""
import argparse
import difflib
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from provision import target_slug  # noqa: E402

# Heading -> tags, in display order (the filter bar and the site list follow it).
TAG_GROUPS = {
    "種別": ["学会", "行政", "公的機関", "職能・業界団体", "報道・情報"],
    "組織": ["厚生労働省", "PMDA"],
    "内容": ["通知・法令", "ガイドライン", "診療報酬・保険", "マスター", "医薬品・医療機器", "医療DX・医療情報", "感染症", "医療安全"],
    "領域": ["内科系", "外科系", "産婦人科・小児", "精神・神経", "感覚器・皮膚・口腔", "放射線・画像・検査", "がん",
           "救急・麻酔・集中治療", "基礎医学", "公衆衛生・予防", "運動器・リハ", "薬剤・栄養", "医療機器・工学", "総合・教育"],
}
RENAME = {"薬・栄養": "薬剤・栄養"}


def quote(t: str) -> str:
    return '"' + t.replace("\\", "\\\\").replace('"', '\\"') + '"'


def tags_line(indent: str, tags: list) -> str:
    return f"{indent}tags: [" + ", ".join(quote(t) for t in tags) + "]"


def groups_block() -> list:
    lines = ["# Headings of the tag filter (filter bar and site list); the tags of a heading are shown in this order.", "tag_groups:"]
    for g, tags in TAG_GROUPS.items():
        lines.append(f"  {g}: [" + ", ".join(quote(t) for t in tags) + "]")
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="../g-i-t-data")
    ap.add_argument("--mapping", required=True)
    ap.add_argument("--apply", action="store_true", help="write config.yaml (default: show the diff)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    with open(args.mapping, encoding="utf-8") as f:
        mapping = {k: [RENAME.get(str(t), str(t)) for t in v] for k, v in yaml.safe_load(f).items()}
    unknown = sorted({t for v in mapping.values() for t in v} - {t for v in TAG_GROUPS.values() for t in v})
    if unknown:
        sys.exit(f"tags in the mapping that are in no group: {unknown}")

    path = os.path.join(args.data_dir, "config.yaml")
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")

    root = yaml.compose("\n".join(lines))
    sites_node = next(v for k, v in root.value if k.value == "sites")
    config = yaml.safe_load("\n".join(lines))
    edits = []                      # (insert_before_index, replace_count, [new lines]) applied from the bottom
    missing = []
    for node, site in zip(sites_node.value, config["sites"]):
        slug = target_slug(site)
        if slug not in mapping:
            missing.append(slug)
            continue
        start, end = node.start_mark.line, node.end_mark.line     # 0-based, end exclusive-ish
        end = min(end, len(lines))
        indent = None
        tag_idx = slug_idx = name_idx = url_idx = None
        for i in range(start, end):
            body = lines[i].lstrip(" -")
            ind = " " * (len(lines[i]) - len(lines[i].lstrip(" -")))
            if lines[i].lstrip().startswith("- url:") or body.startswith("url:"):
                url_idx = i if url_idx is None else url_idx
            for key, name in (("tags:", "tag"), ("slug:", "slug"), ("name:", "name")):
                if body.startswith(key) and len(ind) >= 4 - 2:
                    if name == "tag":
                        tag_idx = i
                    elif name == "slug":
                        slug_idx = i
                    else:
                        name_idx = i
        first = lines[start]
        indent = " " * (len(first) - len(first.lstrip(" -")))
        new = tags_line(indent, mapping[slug])
        if tag_idx is not None:
            if lines[tag_idx] != new:
                edits.append((tag_idx, 1, [new]))
        else:
            at = max(i for i in (slug_idx, name_idx, url_idx, start) if i is not None)
            edits.append((at + 1, 0, [new]))

    for idx, count, new in sorted(edits, key=lambda e: -e[0]):
        lines[idx:idx + count] = new
    if "tag_groups" not in config:
        i = next(n for n, l in enumerate(lines) if l.startswith("sites:"))
        lines[i:i] = groups_block()
    else:
        print("config.yaml already has tag_groups: it is left as it is")

    new_text = eol.join(lines)
    diff = list(difflib.unified_diff(text.replace("\r\n", "\n").split("\n"), lines, "config.yaml", "config.yaml (tags)", lineterm="", n=1))
    print("\n".join(diff) if diff else "no change")
    print(f"\n{len(edits)} site(s) changed, {len(missing)} site(s) not in the mapping" + (f": {', '.join(missing)}" if missing else ""))
    if args.apply and diff:
        with open(path, "wb") as f:
            f.write((b"\xef\xbb\xbf" if bom else b"") + new_text.encode("utf-8"))
        print("written. Next: python scripts/check_config.py --data-dir " + args.data_dir)
    elif diff:
        print("(diff only; add --apply to write)")


if __name__ == "__main__":
    main()
