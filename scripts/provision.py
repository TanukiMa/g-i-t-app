import os
import re
import sys
import hashlib
import subprocess
import tempfile
import argparse

# CI runners have no git identity; commits would fail without one.
os.environ.setdefault("GIT_AUTHOR_NAME", "g-i-t-bot")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "g-i-t-bot@users.noreply.github.com")
os.environ.setdefault("GIT_COMMITTER_NAME", os.environ["GIT_AUTHOR_NAME"])
os.environ.setdefault("GIT_COMMITTER_EMAIL", os.environ["GIT_AUTHOR_EMAIL"])
from urllib.parse import urlparse
import yaml

# Per-site defaults for website-stalker.yaml. website-stalker rejects unknown
# top-level keys, so headers/editors must live inside the site entry.
DEFAULT_SITE_OPTIONS = {
    "headers": [
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ],
    "editors": ["html_sanitize"],
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

def check_submodule_exists(data_dir: str, slug: str) -> bool:
    gitmodules_path = os.path.join(data_dir, ".gitmodules")
    if not os.path.exists(gitmodules_path):
        return False
    with open(gitmodules_path, "r", encoding="utf-8") as f:
        content = f.read()
    wanted = f"sites/{slug}"
    for line in content.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "path" and value.strip().replace("\\", "/") == wanted:
            return True
    return False

def run_cmd(cmd, cwd=None, check=True):
    print(f"Executing: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    res = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, text=True)
    if check and res.returncode != 0:
        print(f"Command failed with code {res.returncode}:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
    return res

def provision_submodule(data_dir: str, target: dict, org_or_user: str = "TanukiMa"):
    url = target.get("url") if isinstance(target, dict) else str(target)
    if not url:
        return
    
    slug = target.get("slug") if isinstance(target, dict) and target.get("slug") else generate_slug(url)
    site_dir = os.path.join(data_dir, "sites", slug)
    
    if check_submodule_exists(data_dir, slug) or os.path.exists(site_dir):
        print(f"Submodule for site '{slug}' already provisioned.")
        return slug

    print(f"Provisioning new submodule repo for site '{slug}' (URL: {url})...")
    repo_name = f"g-i-t-data-{slug}"
    full_repo = f"{org_or_user}/{repo_name}"
    
    # 1. Create remote repo using gh CLI
    create_res = run_cmd(["gh", "repo", "create", full_repo, "--public"], check=False)
    if create_res.returncode != 0 and "already exists" not in create_res.stderr:
        print(f"Warning/Error creating GitHub repository {full_repo}: {create_res.stderr}")
    
    repo_url = f"https://github.com/{full_repo}.git"

    # 2. Seed the child repo only if it has no main branch yet (never overwrite history)
    has_main = run_cmd(["git", "ls-remote", "--exit-code", "--heads", repo_url, "main"], check=False).returncode == 0
    if has_main:
        print(f"{full_repo} already has a main branch; not re-initializing.")
    else:
        # name/slug are G-I-T metadata; website-stalker rejects them.
        extra = {k: v for k, v in target.items() if k not in ("name", "slug", "url")} if isinstance(target, dict) else {}
        site_cfg = {"url": url, **DEFAULT_SITE_OPTIONS, **extra}  # master config overrides defaults
        with tempfile.TemporaryDirectory(prefix=f"git-prov-{slug}-") as temp_dir:
            with open(os.path.join(temp_dir, "website-stalker.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"sites": [site_cfg]}, f, allow_unicode=True, sort_keys=False)
            run_cmd(["git", "init", "-b", "main"], cwd=temp_dir, check=False)
            run_cmd(["git", "add", "website-stalker.yaml"], cwd=temp_dir, check=False)
            run_cmd(["git", "commit", "-m", f"Initial commit for {slug}"], cwd=temp_dir, check=False)
            run_cmd(["git", "remote", "add", "origin", repo_url], cwd=temp_dir, check=False)
            push_res = run_cmd(["git", "push", "-u", "origin", "main"], cwd=temp_dir, check=False)
            if push_res.returncode != 0:
                print(f"Initial push failed for {full_repo}; skipping submodule registration.")
                return None

    # 3. Add submodule inside g-i-t-data
    rel_site_path = f"sites/{slug}"
    add_res = run_cmd(["git", "submodule", "add", repo_url, rel_site_path], cwd=data_dir, check=False)
    if add_res.returncode == 0:
        print(f"Successfully added submodule {rel_site_path}")
    else:
        print(f"Failed to add submodule {rel_site_path}: {add_res.stderr}")
    
    return slug

def main():
    parser = argparse.ArgumentParser(description="JIT Submodule Provisioner for G-I-T")
    parser.add_argument("--data-dir", default="./data", help="Path to g-i-t-data repository")
    args = parser.parse_args()

    targets = parse_stalker_yaml(args.data_dir)
    print(f"Found {len(targets)} targets in master config.yaml.")
    for target in targets:
        try:
            provision_submodule(args.data_dir, target)
        except Exception as e:
            print(f"Error provisioning target {target}: {e}")

if __name__ == "__main__":
    main()
