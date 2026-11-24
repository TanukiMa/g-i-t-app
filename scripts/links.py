"""Which links of an update go into archive_queue besides the page itself (archive_worker.py saves them)."""
import os
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

ATTACHMENT_EXTS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")
MAX_ATTACHMENTS_PER_UPDATE = 50

# New links of an update (any host, not only documents): `archive_links` in config.yaml (default on, `false` per site).
# At most this many per update, same-domain pages first; attachments (above) come on top of them and are not counted.
MAX_LINKS_PER_UPDATE = int(os.environ.get("ARCHIVE_LINKS_MAX") or 10)
# Never worth a capture: images, style sheets, scripts, fonts, audio/video, archives.
LINK_SKIP_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".ico", ".css", ".js", ".woff", ".woff2", ".ttf",
                  ".mp3", ".mp4", ".mov", ".avi", ".zip", ".gz", ".exe")


def _host(netloc: str) -> str:
    return netloc.lower().removeprefix("www.")


def extract_attachment_links(diff_text: str, base_url: str) -> list:
    """Same-domain document links found in the lines added by this diff."""
    added = "\n".join(
        line[1:] for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    base_host = _host(urlparse(base_url).netloc)
    links = []
    for a in BeautifulSoup(added, "html.parser").find_all("a", href=True):
        full = urljoin(base_url, a["href"].strip()).split("#")[0]
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https") or _host(parsed.netloc) != base_host:
            continue
        if not parsed.path.lower().endswith(ATTACHMENT_EXTS):
            continue
        if full not in links:
            links.append(full)
    return links[:MAX_ATTACHMENTS_PER_UPDATE]


def extract_new_links(added_html: str, base_url: str, known=()) -> list:
    """Links (any host) in the lines that this diff REALLY adds (DiffAnalysis.added_markup). Documents come first, then
    pages of the same site, then the rest; in page order inside each group. `known` (the attachments) are left out."""
    base_host = _host(urlparse(base_url).netloc)
    base_clean = base_url.split("#")[0].rstrip("/")
    seen = set(known)
    docs, same, other = [], [], []
    for a in BeautifulSoup(added_html, "html.parser").find_all("a", href=True):
        full = urljoin(base_url, a["href"].strip()).split("#")[0]
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or len(full) > 2000:
            continue
        path = parsed.path.lower()
        if path.endswith(LINK_SKIP_EXTS) or full in seen or full.rstrip("/") == base_clean:
            continue
        seen.add(full)
        if path.endswith(ATTACHMENT_EXTS):
            docs.append(full)
        elif _host(parsed.netloc) == base_host:
            same.append(full)
        else:
            other.append(full)
    return (docs + same + other)[:MAX_LINKS_PER_UPDATE]


def attachments_for(raw_diff: str, analysis, page_url: str, archive_links: bool = True) -> list:
    """What goes into archive_queue besides the page itself: same-domain documents of the added lines (as before) and, unless
    the site has `archive_links: false`, the other new links (extract_new_links). Never for a site's first snapshot."""
    docs = extract_attachment_links(raw_diff, page_url)
    if not archive_links:
        return docs
    return docs + extract_new_links(analysis.added_markup(), page_url, known=docs)
