"""Running commands and git: the one place for it (the pipeline, the dashboard build and the tools).

run_cmd / git      logged: the command is printed, and its output when it fails (the pipeline's log).
git_out            for the analysis tools: stdout only, file names unquoted, exit on failure (unless check=False).
git_silent         nothing printed, the CompletedProcess as it is (text, or bytes with binary=True).
"""
import subprocess
import sys


def run_cmd(cmd, cwd=None, input_text=None, ok_codes=(0,)):
    """Run a command, print it, and print its output when the exit status is not in ok_codes."""
    print(f"Executing in {cwd or '.'}: {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=cwd, input=input_text, capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    if res.returncode not in ok_codes:
        print(f"Command failed code {res.returncode}:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
    return res


def git(args, cwd, **kw):
    return run_cmd(["git"] + list(args), cwd=cwd, **kw)


def git_out(repo, args, check=True) -> str:
    res = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=repo, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0 and check:
        sys.exit(f"git {' '.join(args[:2])} failed: {res.stderr.strip()}")
    return res.stdout


def git_silent(repo, args, binary=False) -> subprocess.CompletedProcess:
    if binary:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True)
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
