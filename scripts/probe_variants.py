"""Catch a page that is served in more than one version: fetch it at an interval and keep every version it shows.

    python scripts/probe_variants.py --url https://www.jarm.jp/ --out probe_jarm --interval 600 --hours 24

Every request is logged (time, status, size, the fingerprint of the version, and the headers that tell where the answer
came from: cf-ray = the Cloudflare site, cf-cache-status, age, date, last-modified, etag). The FINGERPRINT is a hash of the
tag-name sequence and the visible text, so per-request tokens (nonces, cache busters in attributes) do not make every
fetch look different. The full HTML of every new fingerprint is saved (`<out>/<time>_<fingerprint>.html`, with its
headers in a .json next to it). The first fingerprint is the baseline; when another one shows up the script takes
--extra more samples one minute apart (to see how long it stays) and ends, so that a background run tells you at once.
Same User-Agent as the pipeline, no other request header. Be gentle: the default is one request every 10 minutes.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
SHOW = ("cf-ray", "cf-cache-status", "age", "date", "last-modified", "etag", "server", "via", "x-cache", "cache-control")


def fingerprint(html: str) -> str:
    """Tag-name sequence + visible text (white space collapsed): the same page gives the same value on every request."""
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(["script", "style"]):
        el.decompose()
    tags = ",".join(t.name for t in soup.find_all(True))
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return hashlib.sha1((tags + "\n" + text).encode("utf-8")).hexdigest()[:10]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    parser = argparse.ArgumentParser(description="Catch a page that is served in more than one version")
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", default="probe_out", help="folder for the log and the saved versions")
    parser.add_argument("--interval", type=float, default=600, help="seconds between requests (default 600)")
    parser.add_argument("--hours", type=float, default=24, help="stop after this many hours (default 24)")
    parser.add_argument("--extra", type=int, default=3, help="samples (1 minute apart) after a new version appears (default 3)")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    seen, baseline, extra_left = {}, None, None
    end = time.time() + args.hours * 3600
    print(f"Probing {args.url} every {args.interval:g} s for up to {args.hours:g} h; output in {args.out}")
    while time.time() < end:
        t = now_utc()
        try:
            res = requests.get(args.url, headers={"User-Agent": UA}, timeout=30)
            html = res.text
            fp = fingerprint(html)
            headers = {k: res.headers[k] for k in SHOW if k in res.headers}
            line = f"{t:%Y-%m-%dT%H:%M:%SZ} {res.status_code} {len(res.content)}B fp={fp} " + \
                   " ".join(f"{k}={headers[k]}" for k in ("cf-ray", "cf-cache-status", "age") if k in headers)
            if fp not in seen:
                seen[fp] = t
                stamp = f"{t:%Y%m%dT%H%M%SZ}_{fp}"
                with open(os.path.join(args.out, stamp + ".html"), "w", encoding="utf-8") as f:
                    f.write(html)
                with open(os.path.join(args.out, stamp + ".json"), "w", encoding="utf-8") as f:
                    json.dump({"url": args.url, "time": t.isoformat(), "status": res.status_code, "headers": dict(res.headers)},
                              f, ensure_ascii=False, indent=2)
                if baseline is None:
                    baseline = fp
                    line += "  [baseline, saved]"
                else:
                    line += "  [NEW VERSION, saved]"
                    extra_left = args.extra
        except requests.RequestException as e:
            line = f"{t:%Y-%m-%dT%H:%M:%SZ} error {type(e).__name__}: {e}"
        print(line)
        with open(os.path.join(args.out, "probe.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if extra_left is not None:
            if extra_left <= 0:
                print(f"A second version was caught ({len(seen)} versions: {', '.join(seen)}). Done.")
                return
            extra_left -= 1
            time.sleep(60)
        else:
            time.sleep(args.interval)
    print(f"Time is up: {len(seen)} version(s) seen: {', '.join(seen)}.")


if __name__ == "__main__":
    main()
