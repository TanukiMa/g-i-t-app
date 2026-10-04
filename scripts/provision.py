"""JIT provisioning: create sites/<slug>/website-stalker.yaml for every URL in config.yaml.

Everything lives inside the single g-i-t-data repository; nothing is created on GitHub.
"""
import os
import re
import sys
import hashlib
import subprocess
import argparse
from urllib.parse import urlparse
import yaml

# CI runners have no git identity; commits would fail without one.
os.environ.setdefault("GIT_AUTHOR_NAME", "g-i-t-bot")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "g-i-t-bot@users.noreply.github.com")
os.environ.setdefault("GIT_COMMITTER_NAME", os.environ["GIT_AUTHOR_NAME"])
os.environ.setdefault("GIT_COMMITTER_EMAIL", os.environ["GIT_AUTHOR_EMAIL"])

# Per-site defaults for website-stalker.yaml. website-stalker rejects unknown
# top-level keys, so headers/editors must live inside the site entry.
DEFAULT_SITE_OPTIONS = {
    "headers": [
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ],
    # Keep only the page body and drop noise that changes without meaning
    # (scripts, inline styles, embeds), so snapshots stay small and diffs readable.
    "editors": [
        {"css_select": "body"},
        {"css_remove": "script, style, noscript, iframe"},
        "html_sanitize",
        "html_prettify",
    ],
}


def generate_slug(url: str) -> str:
    """Generate slug from URL: <domain>-<path_hash_or_clean_string>."""
    parsed = urlparse(url)
    domain = parsed.netloc.replace(':', '_').replace('.', '-')
    path = parsed.path.strip('/')
    if path:
        clean_path = re.sub(r'[^a-zA-Z0-9_-]', '-', path)
        if len(clean_path) > 30:
            path_hash = hashlib.md5(path.encode('utf-8')).hexdigest()[:8]
            clean_path = clean_path[:20] + '-' + path_hash
        slug = f"{domain}-{clean_path}".lower()
    else:
        slug = domain.lower()
    return slug


def parse_stalker_yaml(data_dir: str):
    master_yaml_path = os.path.join(data_dir, "config.yaml")
    if not os.path.exists(master_yaml_path):
        print(f"Error: {master_yaml_path} does not exist.")
        return []

    with open(master_yaml_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    targets = []
    # Support various website-stalker yaml formats (list of targets or sites)
    if isinstance(config, list):
        targets = config
    elif isinstance(config, dict):
        targets = config.get("sites", config.get("targets", []))

    return targets


# slug becomes a directory name and a URL path segment: ASCII only, no surprises across OSes/Git.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def target_slug(target) -> str:
    """Explicit `slug` or one generated from the URL ("" when the target has no URL)."""
    url = target.get("url") if isinstance(target, dict) else str(target)
    if not url:
        return ""
    if isinstance(target, dict) and target.get("slug"):
        return str(target["slug"])
    return generate_slug(url)


def configured_sites(data_dir: str) -> list:
    """[{slug, name, url, tags}] for every valid target in config.yaml; `name` falls back to the host name."""
    sites = []
    for target in parse_stalker_yaml(data_dir):
        slug = target_slug(target)
        if not slug or not SLUG_RE.match(slug):
            continue
        url = target.get("url") if isinstance(target, dict) else str(target)
        name = (target.get("name") if isinstance(target, dict) else None) or urlparse(url).netloc.removeprefix("www.")
        raw_tags = target.get("tags") if isinstance(target, dict) else None
        tags = [str(t).strip() for t in raw_tags if str(t).strip() and "|" not in str(t)] if isinstance(raw_tags, list) else []
        sites.append({"slug": slug, "name": str(name), "url": url, "tags": tags})
    return sites


def run_cmd(cmd, cwd=None):
    print(f"Executing in {cwd or '.'}: {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        print(f"Command failed with code {res.returncode}:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
    return res


def provision_site(data_dir: str, target) -> str:
    url = target.get("url") if isinstance(target, dict) else str(target)
    if not url:
        return ""

    slug = target_slug(target)
    if not SLUG_RE.match(slug):
        print(f"WARNING: skipping {url}: slug {slug!r} must match {SLUG_RE.pattern} (ASCII lowercase letters, digits, - and _).")
        return ""
    site_dir = os.path.join(data_dir, "sites", slug)
    config_path = os.path.join(site_dir, "website-stalker.yaml")

    if os.path.exists(config_path):
        print(f"Site '{slug}' already provisioned.")
        return slug

    print(f"Provisioning new site '{slug}' (URL: {url})...")
    os.makedirs(site_dir, exist_ok=True)

    # name/slug are G-I-T metadata; website-stalker rejects them.
    extra = {k: v for k, v in target.items() if k not in ("name", "slug", "url", "tags")} if isinstance(target, dict) else {}
    site_cfg = {"url": url, **DEFAULT_SITE_OPTIONS, **extra}  # master config overrides defaults
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump({"sites": [site_cfg]}, f, allow_unicode=True, sort_keys=False)

    # Own commit, so the first stalker run only contains fetched content. Pushed later by website_stalk.py.
    rel_config = f"sites/{slug}/website-stalker.yaml"
    run_cmd(["git", "add", "--", rel_config], cwd=data_dir)
    run_cmd(["git", "commit", "-m", f"Provision {slug}", "--only", "--", rel_config], cwd=data_dir)
    return slug


def main():
    parser = argparse.ArgumentParser(description="JIT site provisioner for G-I-T")
    parser.add_argument("--data-dir", default="./data", help="Path to g-i-t-data repository")
    args = parser.parse_args()

    targets = parse_stalker_yaml(args.data_dir)
    print(f"Found {len(targets)} targets in master config.yaml.")
    for target in targets:
        try:
            provision_site(args.data_dir, target)
        except Exception as e:
            print(f"Error provisioning target {target}: {e}")


if __name__ == "__main__":
    main()
