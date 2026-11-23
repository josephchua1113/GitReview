#!/usr/bin/env python3
"""
bitbucket_sync.py - Init/fetch every Bitbucket repository a token can access.

Works with:
  * Bitbucket Server / Data Center  (REST API /rest/api/1.0, HTTP access token)
  * Bitbucket Cloud                 (REST API /2.0, access token or app password)

For each repository the token can see (optionally filtered by project / repo),
the script works in  <base-dir>/<PROJECT_KEY>/<repo-slug>  (one folder per project):
  new repo       ->  git init, git remote add origin <URL>, git fetch
  existing repo  ->  git fetch

Parameters (CLI flag  ->  environment variable fallback):
  --url        BITBUCKET_URL        Base URL, e.g. https://bitbucket.example.com
  --token      BITBUCKET_TOKEN      HTTP access token / app password
  --base-dir   BITBUCKET_BASE_DIR   Where repos are written (default: ./bitbucket)
  --projects   BITBUCKET_PROJECTS   Filter list: PROJ or PROJ/repo-slug
                                    (comma or space separated; empty = everything)

Only the Python standard library and the `git` command line are required.
The token is never printed, never put on a command line, and never written
into .git/config: it is handed to git through GIT_CONFIG_* environment
variables for the duration of each git call.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

USER_AGENT = "bitbucket-sync/1.0"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Repo:
    project: str      # project key (Server) or workspace (Cloud)
    slug: str
    http_url: str | None
    ssh_url: str | None

    @property
    def full_name(self) -> str:
        return f"{self.project}/{self.slug}"


@dataclass
class Result:
    repo: Repo
    action: str       # initialized | fetched | skipped | failed | would-init | would-fetch
    detail: str = ""


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #
class RepoFilter:
    """Filter entries are 'PROJ' (whole project) or 'PROJ/repo' (one repo).
    Matching is case-insensitive. An empty filter matches everything."""

    def __init__(self, entries: list[str]):
        self.projects: set[str] = set()
        self.repos: set[tuple[str, str]] = set()
        for raw in entries:
            e = raw.strip().strip("/")
            if not e:
                continue
            if "/" in e:
                p, r = e.split("/", 1)
                self.repos.add((p.lower(), r.lower()))
            else:
                self.projects.add(e.lower())

    @property
    def empty(self) -> bool:
        return not self.projects and not self.repos

    def wants_project(self, key: str) -> bool:
        k = key.lower()
        return self.empty or k in self.projects or any(p == k for p, _ in self.repos)

    def wants_repo(self, project: str, slug: str) -> bool:
        p, s = project.lower(), slug.lower()
        return self.empty or p in self.projects or (p, s) in self.repos


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
class ApiError(RuntimeError):
    pass


class Client:
    def __init__(self, base_url: str, token: str, username: str | None, timeout: int, verify_ssl: bool):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        if username:
            raw = f"{username}:{token}".encode()
            self.auth_header = "Basic " + base64.b64encode(raw).decode()
        else:
            self.auth_header = "Bearer " + token
        self.ssl_ctx = None
        if not verify_ssl:
            import ssl
            self.ssl_ctx = ssl._create_unverified_context()  # noqa: S323 - opt-in only

    def get(self, url_or_path: str, params: dict | None = None) -> dict:
        url = url_or_path if url_or_path.startswith("http") else self.base_url + url_or_path
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": self.auth_header,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            hint = ""
            if e.code == 401:
                hint = " (token rejected - check BITBUCKET_TOKEN, or pass --username for app passwords)"
            elif e.code == 403:
                hint = " (token lacks permission for this resource)"
            raise ApiError(f"GET {redact(url)} -> HTTP {e.code}{hint}: {body}") from None
        except urllib.error.URLError as e:
            raise ApiError(f"GET {redact(url)} failed: {e.reason}") from None


def redact(url: str) -> str:
    """Remove any user:password@ from a URL before printing it."""
    parts = urllib.parse.urlsplit(url)
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        parts = parts._replace(netloc=netloc)
    return urllib.parse.urlunsplit(parts)


def strip_userinfo(url: str | None) -> str | None:
    if not url or not url.startswith("http"):
        return url
    return redact(url)


# --------------------------------------------------------------------------- #
# Bitbucket Server / Data Center
# --------------------------------------------------------------------------- #
def server_paged(client: Client, path: str, page_size: int = 100) -> Iterator[dict]:
    start = 0
    while True:
        data = client.get(path, {"start": start, "limit": page_size})
        yield from data.get("values", [])
        if data.get("isLastPage", True):
            return
        start = data.get("nextPageStart")
        if start is None:
            return


def server_clone_links(repo: dict) -> tuple[str | None, str | None]:
    http = ssh = None
    for link in repo.get("links", {}).get("clone", []):
        name, href = link.get("name"), link.get("href")
        if name in ("http", "https"):
            http = href
        elif name == "ssh":
            ssh = href
    return strip_userinfo(http), ssh


def list_server_repos(client: Client, flt: RepoFilter) -> Iterator[Repo]:
    # If the filter names projects explicitly, go straight to them (faster, and
    # works even if the token cannot list all projects).
    if not flt.empty:
        keys = sorted(flt.projects | {p for p, _ in flt.repos})
        projects = []
        for k in keys:
            try:
                projects.append(client.get(f"/rest/api/1.0/projects/{urllib.parse.quote(k.upper())}"))
            except ApiError as e:
                log(f"WARN  project {k.upper()}: {e}")
    else:
        projects = list(server_paged(client, "/rest/api/1.0/projects"))

    for proj in projects:
        key = proj["key"]
        if not flt.wants_project(key):
            continue
        for r in server_paged(client, f"/rest/api/1.0/projects/{urllib.parse.quote(key)}/repos"):
            if flt.wants_repo(key, r["slug"]):
                http, ssh = server_clone_links(r)
                yield Repo(project=key, slug=r["slug"], http_url=http, ssh_url=ssh)


# --------------------------------------------------------------------------- #
# Bitbucket Cloud
# --------------------------------------------------------------------------- #
def cloud_paged(client: Client, path: str, params: dict | None = None) -> Iterator[dict]:
    url: str | None = path
    first = True
    while url:
        data = client.get(url, params if first else None)
        first = False
        yield from data.get("values", [])
        url = data.get("next")


def list_cloud_repos(client: Client, flt: RepoFilter, workspace: str | None) -> Iterator[Repo]:
    if workspace:
        source = cloud_paged(client, f"/2.0/repositories/{urllib.parse.quote(workspace)}", {"pagelen": 100})
    else:
        source = cloud_paged(client, "/2.0/repositories", {"role": "member", "pagelen": 100})

    for r in source:
        ws = (r.get("workspace") or {}).get("slug") or r["full_name"].split("/")[0]
        proj_key = (r.get("project") or {}).get("key") or ""
        slug = r["slug"]
        # On Cloud a filter entry may be a project key or a workspace slug.
        if not (flt.wants_repo(proj_key, slug) or flt.wants_repo(ws, slug)):
            continue
        http = ssh = None
        for link in r.get("links", {}).get("clone", []):
            if link.get("name") == "https":
                http = strip_userinfo(link.get("href"))
            elif link.get("name") == "ssh":
                ssh = link.get("href")
        # Folder is <workspace>/<repo> on Cloud: repo slugs are unique per workspace.
        yield Repo(project=ws, slug=slug, http_url=http, ssh_url=ssh)


# --------------------------------------------------------------------------- #
# Git operations
# --------------------------------------------------------------------------- #
class Git:
    def __init__(self, auth_header: str, protocol: str, verify_ssl: bool, timeout: int):
        self.protocol = protocol
        self.timeout = timeout
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"          # never hang waiting for a password
        cfg: list[tuple[str, str]] = []
        if protocol == "https":
            # Passed through the environment so the token never appears in argv
            # (visible in `ps`) or in the repo's .git/config.
            cfg.append(("http.extraHeader", f"Authorization: {auth_header}"))
        if not verify_ssl:
            cfg.append(("http.sslVerify", "false"))
        base = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
        for i, (k, v) in enumerate(cfg):
            env[f"GIT_CONFIG_KEY_{base + i}"] = k
            env[f"GIT_CONFIG_VALUE_{base + i}"] = v
        env["GIT_CONFIG_COUNT"] = str(base + len(cfg))
        self.env = env

    def run(self, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=cwd, env=self.env, capture_output=True,
            text=True, timeout=self.timeout,
        )

    def sync(self, repo: Repo, base_dir: Path, dry_run: bool) -> Result:
        """New repo:      git init -> git remote add origin <URL> -> git fetch
        Existing repo:  git fetch
        Nothing is checked out or merged; your working tree is never touched."""
        url = repo.http_url if self.protocol == "https" else repo.ssh_url
        if not url:
            return Result(repo, "skipped", f"no {self.protocol} clone URL")
        dest = base_dir / repo.project / repo.slug

        if (dest / ".git").is_dir():
            if dry_run:
                return Result(repo, "would-fetch", str(dest))
            # A repo that exists locally but has no origin yet gets one added.
            if self.run(["remote", "get-url", "origin"], cwd=dest).returncode != 0:
                p = self.run(["remote", "add", "origin", url], cwd=dest)
                if p.returncode != 0:
                    return Result(repo, "failed", "remote add: " + last_line(p.stderr))
            return self._fetch(repo, dest, "fetched")

        if dry_run:
            return Result(repo, "would-init", str(dest))
        dest.mkdir(parents=True, exist_ok=True)
        p = self.run(["init", "--quiet"], cwd=dest)
        if p.returncode != 0:
            return Result(repo, "failed", "init: " + last_line(p.stderr))
        p = self.run(["remote", "add", "origin", url], cwd=dest)
        if p.returncode != 0:
            return Result(repo, "failed", "remote add: " + last_line(p.stderr))
        return self._fetch(repo, dest, "initialized")

    def _fetch(self, repo: Repo, dest: Path, action: str) -> Result:
        p = self.run(["fetch", "origin", "--prune", "--quiet"], cwd=dest)
        if p.returncode != 0:
            return Result(repo, "failed", "fetch: " + last_line(p.stderr))
        return Result(repo, action, str(dest))


def last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "unknown error"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def split_list(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        out.extend(x for x in v.replace(",", " ").split() if x)
    return out


def env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Init/fetch every Bitbucket repository a token can access.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s --url https://bitbucket.example.com --token $TOKEN --base-dir ~/src\n"
            "  %(prog)s --projects PLAT CORE/api-gateway --dry-run\n"
            "  BITBUCKET_PROJECTS=PLAT,CORE %(prog)s\n"
        ),
    )
    ap.add_argument("--url", default=os.environ.get("BITBUCKET_URL"),
                    help="Bitbucket base URL [env BITBUCKET_URL]")
    ap.add_argument("--token", default=os.environ.get("BITBUCKET_TOKEN"),
                    help="Access token / app password [env BITBUCKET_TOKEN]. "
                         "Prefer the env var so the token stays out of shell history.")
    ap.add_argument("--base-dir", default=os.environ.get("BITBUCKET_BASE_DIR", "./bitbucket"),
                    help="Directory for project folders [env BITBUCKET_BASE_DIR, default ./bitbucket]")
    ap.add_argument("--projects", nargs="*", default=None,
                    help="Projects or repos to sync: PROJ or PROJ/repo. Empty = all "
                         "[env BITBUCKET_PROJECTS, comma separated]")
    ap.add_argument("--username", default=os.environ.get("BITBUCKET_USERNAME"),
                    help="Use HTTP Basic auth with this user (Cloud app passwords, or Server "
                         "if bearer tokens are disabled) [env BITBUCKET_USERNAME]")
    ap.add_argument("--workspace", default=os.environ.get("BITBUCKET_WORKSPACE"),
                    help="Cloud only: list this workspace instead of all repos you are a member of "
                         "[env BITBUCKET_WORKSPACE]")
    ap.add_argument("--flavor", choices=["auto", "server", "cloud"],
                    default=os.environ.get("BITBUCKET_FLAVOR", "auto"),
                    help="API flavour; auto = cloud if the URL is bitbucket.org")
    ap.add_argument("--protocol", choices=["https", "ssh"],
                    default=os.environ.get("BITBUCKET_PROTOCOL", "https"),
                    help="Clone over HTTPS with the token (default) or SSH with your keys")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("BITBUCKET_WORKERS", "4")),
                    help="Parallel git operations (default 4)")
    ap.add_argument("--timeout", type=int, default=600, help="Seconds per git command (default 600)")
    ap.add_argument("--insecure", action="store_true", default=env_bool("BITBUCKET_INSECURE"),
                    help="Skip TLS certificate verification (self-signed servers)")
    ap.add_argument("--dry-run", action="store_true", help="List what would happen, change nothing")
    args = ap.parse_args(argv)

    if args.projects is None:
        args.projects = split_list([os.environ.get("BITBUCKET_PROJECTS", "")])
    else:
        args.projects = split_list(args.projects)

    missing = [n for n, v in (("--url / BITBUCKET_URL", args.url),
                              ("--token / BITBUCKET_TOKEN", args.token)) if not v]
    if missing:
        ap.error("missing required setting(s): " + ", ".join(missing))
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if shutil.which("git") is None:
        log("ERROR git is not installed or not on PATH")
        return 2

    url = args.url.rstrip("/")
    flavor = args.flavor
    if flavor == "auto":
        flavor = "cloud" if "bitbucket.org" in urllib.parse.urlsplit(url).netloc else "server"
    api_base = "https://api.bitbucket.org" if flavor == "cloud" else url

    client = Client(api_base, args.token, args.username, timeout=60, verify_ssl=not args.insecure)
    flt = RepoFilter(args.projects)
    base_dir = Path(args.base_dir).expanduser().resolve()

    log(f"Bitbucket {flavor}: {redact(url)}")
    log(f"Base dir : {base_dir}")
    log(f"Filter   : {', '.join(args.projects) if args.projects else '(all accessible)'}")

    try:
        if flavor == "cloud":
            repos = list(list_cloud_repos(client, flt, args.workspace))
        else:
            repos = list(list_server_repos(client, flt))
    except ApiError as e:
        log(f"ERROR {e}")
        return 2

    log(f"Found {len(repos)} repositor{'y' if len(repos) == 1 else 'ies'}\n")
    if not repos:
        return 0

    git = Git(client.auth_header, args.protocol, not args.insecure, args.timeout)
    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(git.sync, r, base_dir, args.dry_run): r
                   for r in sorted(repos, key=lambda r: r.full_name.lower())}
        for fut in as_completed(futures):
            repo = futures[fut]
            try:
                res = fut.result()
            except subprocess.TimeoutExpired:
                res = Result(repo, "failed", f"timed out after {args.timeout}s")
            except Exception as e:  # keep going on unexpected per-repo errors
                res = Result(repo, "failed", f"{type(e).__name__}: {e}")
            results.append(res)
            print(f"{res.action:<12} {res.repo.full_name:<50} {res.detail}", flush=True)

    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    log("\nSummary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
