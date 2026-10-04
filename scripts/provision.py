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


# G-I-T metadata in config.yaml; website-stalker rejects unknown keys, so these are never written out.
RESERVED_KEYS = ("name", "slug", "url", "tags", "ignore")

# website-stalker uses the Rust `regex` crate: no look-around and no back-references.
_UNSUPPORTED_REGEX = re.compile(r"\(\?<?[=!]|\\[1-9]")


def ignore_editors(target, slug: str = "") -> list:
    """`regex_replace` editors for a site's `ignore` rules in config.yaml.

    Each rule is a pattern string (matches are removed) or {pattern, replace}. Invalid rules are skipped
    with a warning so one typo cannot break provisioning.
    """
    rules = target.get("ignore") if isinstance(target, dict) else None
    if not rules:
        return []
    if not isinstance(rules, list):
        print(f"WARNING: {slug}: `ignore` must be a list; ignored.")
        return []
    editors = []
    for rule in rules:
        if isinstance(rule, str):
            pattern, replace = rule, ""
        elif isinstance(rule, dict) and isinstance(rule.get("pattern"), str):
            pattern, replace = rule["pattern"], str(rule.get("replace", ""))
        else:
            print(f"WARNING: {slug}: unsupported ignore rule {rule!r}; skipped.")
            continue
        try:
            re.compile(pattern)
        except re.error as e:
            print(f"WARNING: {slug}: invalid ignore pattern {pattern!r} ({e}); skipped.")
            continue
        if _UNSUPPORTED_REGEX.search(pattern):
            print(f"WARNING: {slug}: ignore pattern {pattern!r} uses look-around/back-references, "
                  "which website-stalker's regex engine does not support; skipped.")
            continue
        editors.append({"regex_replace": {"pattern": pattern, "replace": replace}})
    return editors


def merge_editors(editors: list, extra: list) -> list:
    """Insert `extra` editors before `html_sanitize` (else at the end), skipping ones already present."""
    merged = list(editors)
    new = [e for e in extra if e not in merged]
    if not new:
        return merged
    at = merged.index("html_sanitize") if "html_sanitize" in merged else len(merged)
    return merged[:at] + new + merged[at:]


def commit_config(data_dir: str, slug: str, message: str):
    # Own commit, so the stalker run only contains fetched content. Pushed later by website_stalk.py.
    rel_config = f"sites/{slug}/website-stalker.yaml"
    run_cmd(["git", "add", "--", rel_config], cwd=data_dir)
    run_cmd(["git", "commit", "-m", message, "--only", "--", rel_config], cwd=data_dir)


def sync_ignore_rules(data_dir: str, slug: str, config_path: str, wanted: list):
    """Add missing `ignore` rules to an already provisioned site. Everything else in its YAML is kept."""
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    try:
        site_cfg = cfg["sites"][0]
    except (TypeError, KeyError, IndexError):
        print(f"WARNING: {slug}: unexpected website-stalker.yaml layout; ignore rules not applied.")
        return
    merged = merge_editors(site_cfg.get("editors") or [], wanted)
    if merged == (site_cfg.get("editors") or []):
        return
    site_cfg["editors"] = merged
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)
    commit_config(data_dir, slug, f"Update ignore rules for {slug}")
    print(f"Added ignore rules to '{slug}'.")


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
    ignores = ignore_editors(target, slug)

    if os.path.exists(config_path):
        print(f"Site '{slug}' already provisioned.")
        if ignores:
            sync_ignore_rules(data_dir, slug, config_path, ignores)
        return slug

    print(f"Provisioning new site '{slug}' (URL: {url})...")
    os.makedirs(site_dir, exist_ok=True)

    extra = {k: v for k, v in target.items() if k not in RESERVED_KEYS} if isinstance(target, dict) else {}
    site_cfg = {"url": url, **DEFAULT_SITE_OPTIONS, **extra}  # master config overrides defaults
    if ignores:
        site_cfg["editors"] = merge_editors(site_cfg.get("editors", []), ignores)
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump({"sites": [site_cfg]}, f, allow_unicode=True, sort_keys=False)

    commit_config(data_dir, slug, f"Provision {slug}")
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
