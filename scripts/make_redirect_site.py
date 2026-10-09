"""Turn the dashboard (public/) into a "we have moved" site for GitHub Pages.

    python scripts/make_redirect_site.py --src ../g-i-t-data/public --dst /tmp/ghp --target https://giiit.goudge.org/ \
        --strip-prefix /g-i-t-data

GitHub Pages cannot answer with an HTTP 301, so:
  * every HTML page becomes a small stub that moves the visitor to the same page on the new site (meta refresh + JavaScript;
    the query string and the #fragment are kept, so a shared `?follow=a,b,c` list survives). `index.html` points at the
    directory address (the site never links to index.html);
  * `404.html` does the same for every address that has no file (GitHub Pages serves it for them), mapping
    /<strip-prefix>/<path> to <target>/<path>;
  * everything that is not HTML is copied unchanged and stays up to date at every run: the Atom feeds, feeds/status.json
    and search.json (a feed reader or an installed client app cannot follow an HTML redirect), the assets and the retired
    service worker. The feeds' links point to the new site because the build uses SITE_BASE_URL.

Used by container/entrypoint.sh for the deploy target `github-pages-redirect` (SITE_BASE_URL = the new address).
"""
import argparse
import html
import json
import os
import shutil
import sys
from urllib.parse import quote, urlparse

STUB = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>G醫t は引っ越しました</title>
<link rel="canonical" href="{url}">
<meta http-equiv="refresh" content="0; url={url}">
<meta name="robots" content="noindex">
<script>location.replace({js_url} + location.search + location.hash);</script>
<style>body{{font:16px/1.7 system-ui,'Hiragino Sans','Yu Gothic',sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}}</style>
</head>
<body>
<h1>G醫t は引っ越しました</h1>
<p>新しい場所: <a href="{url}">{url_text}</a></p>
<p>自動で移動しないときは、上のリンクを押してください。ブックマークや購読は、新しい場所に変えてください。</p>
</body>
</html>
"""

# For addresses without a file: the path below the project (/g-i-t-data/...) is carried over to the new site.
NOT_FOUND = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>G醫t は引っ越しました</title>
<meta name="robots" content="noindex">
<script>
(function () {{
  var target = {js_target}, prefix = {js_prefix};
  var path = location.pathname;
  if (prefix && path.indexOf(prefix) === 0) path = path.slice(prefix.length);
  path = path.replace(/index\\.html$/, "");
  location.replace(target + path.replace(/^\\/+/, "") + location.search + location.hash);
}})();
</script>
<style>body{{font:16px/1.7 system-ui,'Hiragino Sans','Yu Gothic',sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}}</style>
</head>
<body>
<h1>G醫t は引っ越しました</h1>
<p>新しい場所: <a href="{url}">{url_text}</a></p>
<p>自動で移動しないときは、上のリンクを押してください。</p>
</body>
</html>
"""


def normalize_target(target: str) -> str:
    t = target.strip()
    parsed = urlparse(t)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        sys.exit(f"--target must be an http(s) address, got {target!r}")
    return t if t.endswith("/") else t + "/"


def page_url(target: str, rel_path: str) -> str:
    """The new address of public/<rel_path>: index.html -> the directory address, other pages keep their file name."""
    rel = rel_path.replace(os.sep, "/")
    if rel == "index.html":
        rel = ""
    elif rel.endswith("/index.html"):
        rel = rel[: -len("index.html")]
    return target + quote(rel, safe="/%")


def stub(url: str) -> str:
    return STUB.format(url=html.escape(url, quote=True), url_text=html.escape(url), js_url=json.dumps(url))


def build(src: str, dst: str, target: str, strip_prefix: str = "") -> dict:
    if not os.path.isdir(src):
        sys.exit(f"--src {src} is not a directory")
    target = normalize_target(target)
    if os.path.abspath(src) == os.path.abspath(dst):
        sys.exit("--src and --dst must differ")
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.makedirs(dst)
    counts = {"stubs": 0, "copied": 0}
    for root, _dirs, files in os.walk(src):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, src)
            out = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            if name.lower().endswith(".html") and rel != "404.html":
                with open(out, "w", encoding="utf-8", newline="\n") as f:
                    f.write(stub(page_url(target, rel)))
                counts["stubs"] += 1
            elif rel != "404.html":
                shutil.copy2(full, out)
                counts["copied"] += 1
    prefix = ("/" + strip_prefix.strip("/") + "/") if strip_prefix.strip("/") else ""
    with open(os.path.join(dst, "404.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(NOT_FOUND.format(js_target=json.dumps(target), js_prefix=json.dumps(prefix), url=html.escape(target, quote=True),
                                 url_text=html.escape(target)))
    open(os.path.join(dst, ".nojekyll"), "w").close()
    return counts


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Make the 'we have moved' site for GitHub Pages from the dashboard")
    parser.add_argument("--src", required=True, help="the dashboard (g-i-t-data/public)")
    parser.add_argument("--dst", required=True, help="where to write the redirect site (replaced)")
    parser.add_argument("--target", required=True, help="the new address, e.g. https://giiit.goudge.org/")
    parser.add_argument("--strip-prefix", default="", help="the project path of the old site, e.g. /g-i-t-data (for 404.html)")
    args = parser.parse_args()
    host = urlparse(args.target).netloc.lower()
    if host.endswith("github.io"):
        sys.exit(f"--target points at GitHub Pages itself ({host}): that would redirect to the same site")
    counts = build(args.src, args.dst, args.target, args.strip_prefix)
    print(f"Redirect site: {counts['stubs']} HTML page(s) -> {normalize_target(args.target)}, {counts['copied']} other file(s) copied, 404.html written ({args.dst}).")


if __name__ == "__main__":
    main()
