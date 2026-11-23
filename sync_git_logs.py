"""
sync_git_logs.py
Fetches the commit history of every Bitbucket repo you can access.
No code is checked out: each repo is a bare repo with history only.

Run:  python sync_git_logs.py
Needs: Python 3.8+ and Git 2.27+ (no extra packages)
"""

import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime

# ===== FILL THESE IN =====
BITBUCKET_URL = "https://bitbucket.yourcompany.com"   # your Bitbucket Server URL
TOKEN         = "PASTE-YOUR-HTTP-ACCESS-TOKEN-HERE"   # HTTP access token (read only)
BASE_DIR      = r"C:\git-logs"                         # where repos + log are saved
PROJECTS      = ["PROJ1", "PROJ2"]                     # project keys to sync; [] = all projects
# =========================

LOG_FILE = os.path.join(BASE_DIR, "sync.log")


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_paged(path):
    """Call a Bitbucket list endpoint and return all pages of results."""
    items, start = [], 0
    while True:
        url = f"{BITBUCKET_URL.rstrip('/')}{path}?limit=100&start={start}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req) as resp:
            page = json.load(resp)
        items.extend(page["values"])
        if page.get("isLastPage", True):
            return items
        start = page["nextPageStart"]


def get_repos():
    """Step 1: get the repos of the projects in PROJECTS (or all if empty)."""
    if not PROJECTS:
        return get_paged("/rest/api/1.0/repos")
    repos = []
    for key in PROJECTS:
        try:
            found = get_paged(f"/rest/api/1.0/projects/{key}/repos")
            log(f"Project {key}: {len(found)} repos")
            repos.extend(found)
        except Exception as e:
            log(f"ERROR: could not list project {key} (check the key / access). {e}")
    return repos


def git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    return result.returncode, (result.stderr or result.stdout).strip()


def main():
    os.makedirs(BASE_DIR, exist_ok=True)
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    log("===== Sync started =====")

    try:
        repos = get_repos()
    except Exception as e:
        log(f"ERROR: could not get repo list. Check URL and token. {e}")
        return
    log(f"Found {len(repos)} repos")

    ok = failed = 0
    for repo in repos:
        project, slug = repo["project"]["key"], repo["slug"]
        name = f"{project}/{slug}"
        clone = next(c["href"] for c in repo["links"]["clone"] if c["name"] == "http")
        url = re.sub(r"://[^@/]+@", "://", clone)          # drop username from URL
        repo_dir = os.path.join(BASE_DIR, project, slug + ".git")

        # Step 2: init + add remote (also repairs a half-created folder)
        if not os.path.isfile(os.path.join(repo_dir, "HEAD")):
            code, out = git("init", "--bare", "-q", repo_dir)
            if code != 0:
                failed += 1
                log(f"FAILED  {name} : init failed: {out}")
                continue
        if git("-C", repo_dir, "remote", "get-url", "origin")[0] == 0:
            git("-C", repo_dir, "remote", "set-url", "origin", url)
        else:
            git("-C", repo_dir, "remote", "add", "origin", url)

        # Step 3: fetch history only (commits + tags, no file contents)
        code, out = git("-C", repo_dir,
                        "-c", f"http.extraHeader=Authorization: Bearer {TOKEN}",
                        "fetch", "--quiet", "--prune", "--tags", "--filter=tree:0", "origin")

        # Step 4: write to the run log
        if code == 0:
            ok += 1
            log(f"OK      {name}")
        else:
            failed += 1
            lines = out.splitlines() or ["unknown error"]
            error = next((l for l in lines if l.startswith("fatal")), lines[0])
            log(f"FAILED  {name} : {error}")

    log(f"===== Done: {ok} ok, {failed} failed =====")


if __name__ == "__main__":
    main()
