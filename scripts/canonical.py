"""One address per page: <link rel="canonical"> and a move away from the default Firebase addresses."""
import html
import json
import os
import re
from urllib.parse import quote, urlparse

from common import SITE_BASE_URL

CANON_START, CANON_END = "<!--git-canon-->", "<!--/git-canon-->"


def canonical_base():
    """The address the pages must be known by: SITE_BASE_URL, when it is a real site of its own. None for GitHub Pages
    (github.io: the old address, which only forwards), localhost and an empty value: then the pages stay as they are."""
    base = os.environ.get("SITE_BASE_URL", SITE_BASE_URL).strip()
    host = urlparse(base).netloc.lower().split(":")[0]
    if not host or host.endswith("github.io") or host in ("localhost", "127.0.0.1"):
        return None
    return base if base.endswith("/") else base + "/"


def canonical_url(base: str, rel_path: str) -> str:
    """public/<rel_path> -> its address: index.html is the directory address, other pages keep their file name."""
    rel = rel_path.replace(os.sep, "/")
    if rel == "index.html":
        rel = ""
    elif rel.endswith("/index.html"):
        rel = rel[: -len("index.html")]
    return base + quote(rel, safe="/%")


def inject_canonical(page: str, rel_path: str, base) -> str:
    """Put the canonical link (and, for a custom domain, the move away from *.web.app / *.firebaseapp.com) right after <head>.

    The Hosting default addresses (SITE.web.app, SITE.firebaseapp.com) cannot be switched off and serve the same files, so
    a search engine could list every page three times: the canonical link names the one real address, and a visitor who
    arrives on a default address is sent to the same page on the real one (path, query and #fragment kept). A block of an
    earlier build is replaced, and removed when there is no canonical base any more (SITE_BASE_URL back on github.io)."""
    page = re.sub(re.escape(CANON_START) + r".*?" + re.escape(CANON_END), "", page, flags=re.S)
    if not base:
        return page
    m = re.search(r"<head[^>]*>", page, re.I)
    if not m:
        return page
    block = f'{CANON_START}<link rel="canonical" href="{html.escape(canonical_url(base, rel_path), quote=True)}">'
    host = urlparse(base).netloc.lower().split(":")[0]
    if not host.endswith((".web.app", ".firebaseapp.com")):      # on a default address itself there would be a loop
        block += ('<script>(function(){if(/\\.(web\\.app|firebaseapp\\.com)$/i.test(location.hostname)){location.replace('
                  + json.dumps(base) + '+location.pathname.replace(/^\\/+/,"")+location.search+location.hash)}})();</script>')
    return page[:m.end()] + block + CANON_END + page[m.end():]
