"""The diff pages: public/sites/<slug>/diff_<hash7>.html, a small view of the patch next to it (diff_<hash7>.patch.txt,
written by the pipeline), drawn by diff2html in the browser."""
import html
import os

from canonical import inject_canonical
from common import (DATA_REPO_URL, DIFF_MARK_V3, PATCH_SUFFIX, SITE_PAGE, SUMMARY_FAILED, SUMMARY_INITIAL,
                    SUMMARY_UNAVAILABLE)
from dashboard_text import render_markdown, summary_by, to_jst


def attach_diff_pages(updates, public_dir: str):
    """Set item['diff_file'] for updates whose diff_<hash7>.html was written by website_stalk.py."""
    for item in updates:
        slug, commit = item.get("site_slug"), item.get("commit_hash") or ""
        if not slug or not commit:
            continue
        name = f"diff_{commit[:7]}.html"
        if os.path.isfile(os.path.join(public_dir, "sites", slug, name)):
            item["diff_file"] = name


DIFF_STYLE = (
    "<style>"
    ".git-nav{display:flex;flex-wrap:wrap;gap:.4rem 1.2rem;align-items:center;margin:0;padding:.6rem 1rem;"
    "background:#f6f8fa;border-bottom:1px solid #d0d7de;font:14px/1.4 system-ui,sans-serif;text-align:left}"
    ".git-head{max-width:70rem;margin:0 auto;padding:1rem 1rem .5rem;text-align:left;font:15px/1.7 system-ui,'Hiragino Sans','Yu Gothic',sans-serif;color:#1f2328}"
    ".git-head h1{font-size:1.35rem;margin:.2rem 0 .6rem;color:inherit}"
    ".git-head dl{display:grid;grid-template-columns:max-content 1fr;gap:.2rem 1rem;margin:0 0 .8rem}"
    ".git-head dt{color:#656d76}.git-head dd{margin:0;overflow-wrap:anywhere}"
    ".git-head details{margin:.5rem 0;padding:.5rem .9rem;border:1px solid #d0d7de;border-radius:8px}"
    ".git-head summary{cursor:pointer;font-weight:600}"
    ".git-head .how{color:#656d76;font-size:.9em;margin:.4rem 0 0}"
    ".git-head .by{color:#656d76;font-size:.8em;margin:.3rem 0 0;text-align:right}"
    ".git-foot{max-width:70rem;margin:2rem auto 1.5rem;padding:0 1rem;text-align:center;font:13px/1.6 system-ui,sans-serif;color:#656d76}"
    ".git-nav a,.git-head a,.git-foot a{color:#0969da;text-decoration:none}"
    ".git-nav a:hover,.git-head a:hover,.git-foot a:hover{text-decoration:underline}"
    ".git-nav span{color:#656d76}"
    "@media (prefers-color-scheme:dark){.git-nav{background:#161b22;border-color:#30363d}"
    ".git-head{color:#e6edf3}.git-head details{border-color:#30363d}"
    ".git-head dt,.git-head .how,.git-head .by,.git-nav span,.git-foot{color:#8b949e}"
    ".git-nav a,.git-head a,.git-foot a{color:#58a6ff}}"
    "</style>"
)


def diff_head_foot(slug: str, hash7: str, names: dict, by_key: dict):
    """(site name, update time, header html, footer html) of a diff page: link bar, what/when/where, the AI summary."""
    site = html.escape(names.get(slug, slug))
    upd = by_key.get((slug, hash7))
    when = html.escape(to_jst(upd["created_at"])) if upd else ""

    rows = []
    if when:
        rows.append(f"<dt>更新日時</dt><dd>{when}</dd>")
    if upd and upd.get("url"):
        url = html.escape(upd["url"], quote=True)
        rows.append(f'<dt>確認先のページ</dt><dd><a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a></dd>')
    if upd and upd.get("commit_hash"):
        commit = html.escape(str(upd["commit_hash"]), quote=True)
        rows.append(f'<dt>記録</dt><dd><a href="{DATA_REPO_URL}/commit/{commit}" target="_blank" '
                    f'rel="noopener noreferrer">commit {commit[:7]}</a></dd>')
    summary = ""
    text = (upd or {}).get("summary") or ""
    if text and text not in (SUMMARY_INITIAL, SUMMARY_FAILED, SUMMARY_UNAVAILABLE):
        by = summary_by((upd or {}).get("summary_model"))
        by_note = f'<p class="by">{html.escape(by)}</p>' if by else ""
        summary = (f'<details open><summary>AI による要約</summary>{render_markdown(text)}{by_note}'
                   '<p class="how">AI の要約は誤りを含むことがあります。下の差分、または確認先のページで確かめてください。</p></details>')
    head = (f'<nav class="git-nav">'
            f'<a href="{SITE_PAGE}" onclick="if(document.referrer&&history.length>1){{history.back();return false}}">← 戻る</a>'
            f'<a href="{SITE_PAGE}">📜 {site} の更新歴</a>'
            f'<a href="../../">全体タイムライン</a>'
            f'<a href="../../sites.html">サイト一覧</a></nav>'
            f'<header class="git-head"><h1>{site} の更新差分</h1>'
            f'<dl>{"".join(rows)}</dl>{summary}'
            f'<p class="how">ページのテキストの、前回の確認との違いです。左が変更前、右が変更後で、'
            f'<strong>緑</strong>が追加、<strong>赤</strong>が削除された部分です。</p></header>')
    foot = ('<footer class="git-foot"><p>G醫t が記録した差分です。'
            f'<a href="../../about.html">G醫tについて</a> ・ <a href="../../">全体タイムライン</a></p>'
            '<p>差分の表示: Diff to HTML by <a href="https://github.com/rtfpessoa">rtfpessoa</a>（diff2html）</p></footer>')
    return site, when, head, foot


# A diff page is a view of a patch. website_stalk.py writes the raw `git show` output of the commit as
# diff_<hash7>.patch.txt (the record: plain text, about 13 KB on average); this builds diff_<hash7>.html next to it, a small
# page with the header, the AI summary and the shared diff2html (assets/diff/) that draws the patch in the browser. The
# page is written when it is missing or not of this kind (diff2html's own HTML of the past, 100 KB-9 MB, made the container
# run out of memory); a page that already has the marker is a record and is left as it is, so a change of the design,
# the domain or this script never rewrites the past (MIGRATE_DIFF_PAGES=1: all are written again, once, on purpose).
DIFF_VIEW_CSS = (
    "<style>@media screen and (prefers-color-scheme:light){body{background-color:var(--d2h-bg-color)}}"
    "@media screen and (prefers-color-scheme:dark){body{background-color:rgb(13,17,23)}}"
    ".git-note{max-width:70rem;margin:.5rem auto;padding:0 1rem;text-align:left;font:14px/1.6 system-ui,sans-serif}"
    "#diff{text-align:left}</style>"
)


HLJS = "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/"


def render_diff_view(slug: str, hash7: str, names: dict, by_key: dict, base, version: str) -> str:
    site, when, head, foot = diff_head_foot(slug, hash7, names, by_key)
    patch = f"diff_{hash7}{PATCH_SUFFIX}"
    v = f"?v={version}" if version else ""
    page = (
        "<!doctype html>" + DIFF_MARK_V3 + '<html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>差分 {site} {when or hash7} - G醫t</title>"
        f'<link rel="stylesheet" href="{HLJS}github.min.css" media="screen and (prefers-color-scheme: light)">'
        f'<link rel="stylesheet" href="{HLJS}github-dark.min.css" media="screen and (prefers-color-scheme: dark)">'
        f'<link rel="stylesheet" href="../../assets/diff/diff2html.css{v}">{DIFF_VIEW_CSS}{DIFF_STYLE}</head>'
        "<body style=\"text-align: center; font-family: 'Source Sans Pro', sans-serif\">"
        f'{head}<div id="diff" data-patch="{patch}"></div>'
        '<p id="diff-note" class="git-note" hidden></p>'
        f'<noscript><p class="git-note">差分の表示には JavaScript が必要です。</p></noscript>'
        f'<p class="git-note"><a href="{patch}">元の差分（テキスト）</a></p>{foot}'
        f'<script src="../../assets/diff/diff2html-ui.js{v}"></script><script src="../../assets/diff/diff-view.js{v}"></script>'
        "</body></html>" + chr(10)
    )
    return inject_canonical(page, f"sites/{slug}/diff_{hash7}.html", base)


def make_diff_pages(public_dir: str, names: dict, updates: list, base, migrate: bool, version: str) -> int:
    """Write diff_<hash7>.html for every diff_<hash7>.patch.txt that has no page of the new kind yet. Returns how many."""
    by_key = {(u.get("site_slug"), str(u.get("commit_hash", ""))[:7]): u for u in updates}
    sites_root = os.path.join(public_dir, "sites")
    made = 0
    for slug in sorted(os.listdir(sites_root)) if os.path.isdir(sites_root) else []:
        folder = os.path.join(sites_root, slug)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not (name.startswith("diff_") and name.endswith(PATCH_SUFFIX)):
                continue
            hash7 = name[5:-len(PATCH_SUFFIX)]
            page_path = os.path.join(folder, f"diff_{hash7}.html")
            if os.path.isfile(page_path) and not migrate:
                with open(page_path, "r", encoding="utf-8", errors="replace") as f:
                    if DIFF_MARK_V3 in f.read(300):
                        continue
            with open(page_path, "w", encoding="utf-8", newline="") as f:
                f.write(render_diff_view(slug, hash7, names, by_key, base, version))
            made += 1
    return made
