"""Check g-i-t-data/config.yaml before it is committed or used.

    python scripts/check_config.py --data-dir ../g-i-t-data       # the working copy
    git show :config.yaml | python scripts/check_config.py --stdin # the staged version (pre-commit hook)

Errors (exit status 1) are things that would break provisioning or the dashboard: YAML that cannot be read, a bad
slug, a duplicate slug, a URL that is not http(s), an `ignore` regex website-stalker cannot run, a `remove` selector
that is not valid CSS. Warnings (exit status 0, or 1 with --strict) are things that probably are not meant: typographic
quotes inside a value, a misspelled key, the same URL twice, an odd tag.
"""
import argparse
import difflib
import os
import re
import sys
from urllib.parse import urlparse

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from provision import SLUG_RE, _UNSUPPORTED_REGEX, target_slug  # noqa: E402

TOP_KEYS = {"sites", "ignore", "remove"}
SITE_KEYS = {"url", "name", "slug", "tags", "ignore", "remove", "default_ignore"}
# Typographic quotes are almost always a paste from a word processor: they do not close a YAML string.
TYPO_QUOTES = "“”‘’"   # curly double and single quotes
TYPO_RE = re.compile("[" + TYPO_QUOTES + "]")


def _line_of(node, key=None):
    """1-based line of a YAML node (or of the value of `key` in a mapping node); 0 when unknown."""
    if node is None:
        return 0
    if key is not None and isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if getattr(k, "value", None) == key:
                return k.start_mark.line + 1
    return node.start_mark.line + 1


def check_text(text: str):
    """Returns (errors, warnings, site_count); each message is 'line N: ...' (line 0 = whole file)."""
    errors, warnings = [], []

    def err(line, msg): errors.append(f"line {line}: {msg}" if line else msg)
    def warn(line, msg): warnings.append(f"line {line}: {msg}" if line else msg)

    typo_lines = [i for i, l in enumerate(text.split("\n"), 1) if TYPO_RE.search(l)]
    try:
        config = yaml.safe_load(text)
        root = yaml.compose(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "unknown position"
        problem = getattr(e, "problem", None) or str(e)
        ctx = getattr(e, "context", None)
        err(0, f"the YAML cannot be read at {where}: {problem}" + (f" ({ctx})" if ctx else ""))
        if typo_lines:
            err(0, "typographic quotes found on line(s) " + ", ".join(map(str, typo_lines[:10])) +
                " - a closing quote such as ” does not end a YAML string; use the plain \" (or ').")
        return errors, warnings, 0

    if config is None:
        err(0, "the file is empty")
        return errors, warnings, 0
    if isinstance(config, list):
        warn(0, "the top level is a list; use `sites:` as the top-level key")
        sites, top, sites_node = config, {}, root
    elif isinstance(config, dict):
        top, sites, sites_node = config, config.get("sites"), None
        if isinstance(root, yaml.MappingNode):
            for k, v in root.value:
                if getattr(k, "value", None) == "sites":
                    sites_node = v
        for key in top:
            if key not in TOP_KEYS:
                close = difflib.get_close_matches(str(key), TOP_KEYS, n=1, cutoff=0.7)
                warn(_line_of(root, key), f"unknown top-level key `{key}`" + (f" (did you mean `{close[0]}`?)" if close else ""))
    else:
        err(0, "the top level must be a mapping with `sites:`")
        return errors, warnings, 0

    if not isinstance(sites, list) or not sites:
        err(0, "`sites:` must be a non-empty list")
        return errors, warnings, 0

    for name, value in (("ignore", top.get("ignore")), ("remove", top.get("remove"))):
        if value is not None and not isinstance(value, list):
            err(_line_of(root, name), f"top-level `{name}` must be a list")
    _check_rules(top.get("ignore"), _line_of(root, "ignore"), "top-level ignore", err)
    _check_selectors(top.get("remove"), _line_of(root, "remove"), "top-level remove", err)

    seen_slugs, seen_urls = {}, {}
    item_nodes = sites_node.value if isinstance(sites_node, yaml.SequenceNode) else [None] * len(sites)
    for n, (site, node) in enumerate(zip(sites, item_nodes), 1):
        line = _line_of(node)
        if not isinstance(site, dict):
            err(line, f"site #{n} must be a mapping with at least `url:`")
            continue
        label = f"site #{n}" + (f" ({site['slug']})" if site.get("slug") else "")
        for key in site:
            if key not in SITE_KEYS:
                close = difflib.get_close_matches(str(key), SITE_KEYS, n=1, cutoff=0.75)
                if close:
                    warn(_line_of(node, key), f"{label}: unknown key `{key}` (did you mean `{close[0]}`?)")

        url = site.get("url")
        if not isinstance(url, str) or not url.strip():
            err(_line_of(node, "url") or line, f"{label}: `url` is missing or empty")
        else:
            parsed = urlparse(url.strip())
            if parsed.scheme not in ("http", "https") or not parsed.netloc or " " in url.strip():
                err(_line_of(node, "url"), f"{label}: `url` must be a plain http(s) URL, got {url!r}")
            if TYPO_RE.search(url):
                err(_line_of(node, "url"), f"{label}: typographic quote inside the url")
            seen_urls.setdefault(url.strip(), []).append(n)

        slug = target_slug(site) if isinstance(url, str) and url.strip() else ""
        if site.get("slug") is not None and not isinstance(site.get("slug"), str):
            err(_line_of(node, "slug"), f"{label}: `slug` must be a string")
        elif slug and not SLUG_RE.match(slug):
            err(_line_of(node, "slug") or line, f"{label}: slug {slug!r} must match {SLUG_RE.pattern} (ASCII lowercase letters, digits, - and _)")
        elif slug:
            if slug in seen_slugs:
                err(_line_of(node, "slug") or line, f"{label}: slug {slug!r} is used twice (also by site #{seen_slugs[slug]})")
            seen_slugs.setdefault(slug, n)

        name = site.get("name")
        if name is not None and not isinstance(name, str):
            warn(_line_of(node, "name"), f"{label}: `name` should be a string")
        elif isinstance(name, str) and TYPO_RE.search(name):
            warn(_line_of(node, "name"), f"{label}: typographic quote inside `name` {name!r}")

        tags = site.get("tags")
        if tags is not None:
            if not isinstance(tags, list):
                err(_line_of(node, "tags"), f"{label}: `tags` must be a list")
            else:
                for t in tags:
                    if not isinstance(t, str) or not t.strip() or "|" in t:
                        warn(_line_of(node, "tags"), f"{label}: tag {t!r} is ignored (it must be a non-empty string without `|`)")

        if "default_ignore" in site and not isinstance(site["default_ignore"], bool):
            err(_line_of(node, "default_ignore"), f"{label}: `default_ignore` must be true or false")
        if site.get("ignore") is not None and not isinstance(site.get("ignore"), list):
            err(_line_of(node, "ignore"), f"{label}: `ignore` must be a list")
        if site.get("remove") is not None and not isinstance(site.get("remove"), list):
            err(_line_of(node, "remove"), f"{label}: `remove` must be a list")
        _check_rules(site.get("ignore"), _line_of(node, "ignore"), label + " ignore", err)
        _check_selectors(site.get("remove"), _line_of(node, "remove"), label + " remove", err)

    for url, nums in seen_urls.items():
        if len(nums) > 1:
            warn(0, f"the URL {url} is listed {len(nums)} times (sites #{', #'.join(map(str, nums))})")
    return errors, warnings, len(sites)


def _check_rules(rules, line, label, err):
    if not isinstance(rules, list):
        return
    for rule in rules:
        if isinstance(rule, str):
            pattern = rule
        elif isinstance(rule, dict) and isinstance(rule.get("pattern"), str):
            pattern = rule["pattern"]
        else:
            err(line, f"{label}: a rule must be a regex string or {{pattern, replace}}, got {rule!r}")
            continue
        try:
            re.compile(pattern)
        except re.error as e:
            err(line, f"{label}: invalid regex {pattern!r} ({e})")
            continue
        if _UNSUPPORTED_REGEX.search(pattern):
            err(line, f"{label}: {pattern!r} uses look-around or a back-reference, which website-stalker's regex engine does not support")


def _check_selectors(selectors, line, label, err):
    if not isinstance(selectors, list):
        return
    try:
        import soupsieve
    except ImportError:
        soupsieve = None
    for sel in selectors:
        if not isinstance(sel, str) or not sel.strip():
            err(line, f"{label}: a selector must be a non-empty string, got {sel!r}")
        elif soupsieve is not None:
            try:
                soupsieve.compile(sel.strip())
            except Exception as e:
                err(line, f"{label}: invalid CSS selector {sel!r} ({str(e).splitlines()[0]})")


def report(data_dir: str):
    """Check the working copy of config.yaml and print the findings. Returns (errors, warnings)."""
    path = os.path.join(data_dir, "config.yaml")
    with open(path, "rb") as f:
        text = f.read().decode("utf-8-sig", errors="replace")
    errors, warnings, count = check_text(text.replace("\r\n", "\n"))
    for m in errors:
        print(f"ERROR   config.yaml: {m}")
    for m in warnings:
        print(f"WARNING config.yaml: {m}")
    print(f"config.yaml: {count} site(s), {len(errors)} error(s), {len(warnings)} warning(s)")
    return errors, warnings


def main():
    parser = argparse.ArgumentParser(description="Check g-i-t-data/config.yaml")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--data-dir", default="./data", help="g-i-t-data working copy (reads config.yaml)")
    src.add_argument("--file", help="a config file to check")
    src.add_argument("--stdin", action="store_true", help="read the config from standard input")
    parser.add_argument("--strict", action="store_true", help="warnings fail too")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.stdin:
        text, name = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace"), "(stdin)"
    else:
        name = args.file or os.path.join(args.data_dir, "config.yaml")
        with open(name, "rb") as f:
            text = f.read().decode("utf-8-sig", errors="replace")
    errors, warnings, count = check_text(text.replace("\r\n", "\n"))
    for m in errors:
        print(f"ERROR   {name}: {m}")
    for m in warnings:
        print(f"WARNING {name}: {m}")
    print(f"{name}: {count} site(s), {len(errors)} error(s), {len(warnings)} warning(s)")
    sys.exit(1 if errors or (args.strict and warnings) else 0)


if __name__ == "__main__":
    main()
