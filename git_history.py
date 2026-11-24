#!/usr/bin/env python3
"""Release review page generator.

For every git repository inside REPOS_FOLDER (directly, or inside sub folders down
to REPO_SEARCH_DEPTH levels), list the commits that are on the target release/hotfix branch but not on the closest lower branch of the
same country, grouped by repository and then by file, as one static HTML page.

Rules this script follows:
  * Every value shown comes from git output (log, remote branch list, remote URL).
    Nothing is inferred, summarised, counted or scored.
  * Only remote-tracking refs (refs/remotes/<REMOTE_NAME>/...) are read. The only
    command that changes anything is `git fetch <REMOTE_NAME> --prune`, which
    updates remote-tracking refs. No checkout, merge, pull or working-tree change.
  * Standard library only. git is called with argument lists (no shell).

Run with no arguments:  python git_history.py
"""

# ===== CONFIGURATION =====
REPOS_FOLDER      = "/path/to/repos"        # parent folder containing the git repositories
REPO_SEARCH_DEPTH = 3                       # folder levels searched below REPOS_FOLDER (2 = REPOS_FOLDER/<group>/<repo>)
TARGET_BRANCH     = "release/SG_19.18.0"    # branch being released: <release|hotfix>/<Country>_<Version>
REMOTE_NAME       = "origin"                # remote to read branches from
OUTPUT_FILE       = "index.html"            # generated HTML page
BITBUCKET_BASE_URL = None                   # Bitbucket web address for links, e.g. "https://bitbucket.mycompany.com"
FALLBACK_BASE_BRANCH = "master"             # base used when no lower release/hotfix exists (None = no fallback)
STATS_OUTPUT_FILE = "stats.html"            # separate per-file statistics page (None = don't create it)
DIFF_URL_TEMPLATE = None                    # only needed for self-hosted / unrecognised git hosts
#
# BITBUCKET_BASE_URL: set this when your repositories are on Bitbucket. Every link
#   (commit diff, base -> target compare diff, and file view) is then built from this
#   address, for every repository. The project/workspace and repository name come
#   from each repository's remote URL (the last two parts of its path, after "scm/").
#   - "https://bitbucket.org"            -> Bitbucket Cloud link formats
#   - any other address (self-hosted)    -> Bitbucket Server / Data Center formats,
#                                           e.g. https://bitbucket.mycompany.com/projects/PROJ/repos/repo
#   Include the context path if your server has one: "https://host/bitbucket".
#   Leave it as None to detect the platform from the remote URL instead.
#   It can't be combined with DIFF_URL_TEMPLATE.
#
# FALLBACK_BASE_BRANCH: when the target branch exists but no release/ or hotfix/
#   branch of the same country has a lower version, compare against
#   <REMOTE_NAME>/<FALLBACK_BASE_BRANCH> instead (e.g. origin/master). The page marks
#   such repositories as using the fallback base. If that branch doesn't exist on the
#   remote either, the repository shows "No previous branch found". Set it to None to
#   turn the fallback off. It is never used for a tie or a missing target branch.
#
# DIFF_URL_TEMPLATE example (commit and compare links required, file link optional):
# DIFF_URL_TEMPLATE = {
#     "commit":  "{base_url}/commit/{commit}#diff-{path_sha256}",
#     "compare": "{base_url}/compare/{base}...{target}#diff-{path_sha256}",
#     "file":    "{base_url}/blob/{target_commit}/{path}",
# }
# Placeholders: {base_url} {commit} {base} {target} {target_commit} {path} {path_sha256} {path_sha1}
#   base/target are branch names without the "<REMOTE_NAME>/" prefix, URL-encoded
#   (so "/" becomes %2F). target_commit is the full hash the target branch points to.
#   path is URL-encoded with "/" kept. path_sha256 / path_sha1 are hex digests of the
#   UTF-8 file path. In commit links for commits that change no file, path,
#   path_sha256 and path_sha1 are empty strings.
# =========================

import base64
import dataclasses
import datetime as _dt
import gzip
import hashlib
import html
import os
import re
import string
import subprocess
import sys
import urllib.parse
from typing import Callable, Dict, List, Optional, Sequence, Tuple


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class ConfigError(Exception):
    """Invalid configuration. Raised before any repository is touched."""


class GitError(Exception):
    """A git command failed."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str):
        self.git_args = list(args)
        self.returncode = returncode
        self.stderr = stderr
        cmd = "git " + " ".join(args)
        detail = stderr.strip() or "(no error output)"
        super().__init__(f"`{cmd}` failed with exit code {returncode}:\n{detail}")


class LogParseError(Exception):
    """git log output did not have the expected structure."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BRANCH_RE = re.compile(r"^(release|hotfix)/([A-Za-z]+)_([0-9]+(?:\.[0-9]+)*)$")
TEMPLATE_KEYS = ("commit", "compare")          # required
TEMPLATE_OPTIONAL_KEYS = ("file",)
TEMPLATE_PLACEHOLDERS = frozenset(
    {"base_url", "commit", "base", "target", "target_commit", "path", "path_sha256", "path_sha1"})


@dataclasses.dataclass(frozen=True)
class BranchName:
    """A branch name that follows <release|hotfix>/<Country>_<Version>."""
    name: str                    # e.g. "release/SG_19.18.0" (no remote prefix)
    kind: str                    # "release" or "hotfix"
    country: str                 # e.g. "SG"
    version: str                 # e.g. "19.18.0", exactly as written
    version_key: Tuple[int, ...]  # numeric parts, as written


def version_key(version: str) -> Tuple[int, ...]:
    """Numeric, part-by-part comparison key: "19.9.0" < "19.17.1" < "19.18.0".

    Parts are compared as numbers from left to right. When all shared parts are
    equal, the version with more parts is higher: "19.17" < "19.17.0" < "19.17.1".
    So two versions are the same only when every part is the same number.
    """
    return tuple(int(p) for p in version.split("."))


def parse_branch_name(name: str) -> Optional[BranchName]:
    m = BRANCH_RE.match(name)
    if not m:
        return None
    kind, country, version = m.groups()
    return BranchName(name, kind, country, version, version_key(version))


@dataclasses.dataclass(frozen=True)
class Config:
    repos_folder: str
    target: BranchName
    remote_name: str
    output_file: str
    diff_url_template: Optional[Dict[str, str]]
    bitbucket_base_url: Optional[str] = None
    fallback_base_branch: Optional[str] = None
    stats_output_file: Optional[str] = None


def load_config(repos_folder, target_branch, remote_name, output_file,
                diff_url_template, bitbucket_base_url=None, fallback_base_branch=None,
                stats_output_file=None) -> Config:
    """Validate all inputs. Raises ConfigError with a clear message."""
    if not isinstance(target_branch, str) or parse_branch_name(target_branch) is None:
        raise ConfigError(
            f"TARGET_BRANCH {target_branch!r} does not follow the naming convention "
            "<release|hotfix>/<Country>_<Version>, for example 'release/SG_19.18.0' "
            "(Country: letters only; Version: dot-separated numbers).")
    if not isinstance(repos_folder, str) or not repos_folder:
        raise ConfigError("REPOS_FOLDER must be a non-empty path.")
    if not os.path.isdir(repos_folder):
        raise ConfigError(f"REPOS_FOLDER {repos_folder!r} does not exist or is not a folder.")
    if (not isinstance(remote_name, str) or not remote_name
            or remote_name.startswith("-") or "/" in remote_name or remote_name.strip() != remote_name):
        raise ConfigError(f"REMOTE_NAME {remote_name!r} is not a valid remote name.")
    if not isinstance(output_file, str) or not output_file:
        raise ConfigError("OUTPUT_FILE must be a non-empty path.")
    template = _validate_template(diff_url_template)
    bb = _validate_bitbucket_base_url(bitbucket_base_url)
    if bb and template:
        raise ConfigError("Set either BITBUCKET_BASE_URL or DIFF_URL_TEMPLATE, not both.")
    if fallback_base_branch is not None and (
            not isinstance(fallback_base_branch, str) or not fallback_base_branch
            or fallback_base_branch != fallback_base_branch.strip()
            or fallback_base_branch.startswith(("-", "/", "refs/"))
            or re.search(r"[\s~^:?*\[\\]|\.\.|@\{", fallback_base_branch)):
        raise ConfigError(
            f"FALLBACK_BASE_BRANCH {fallback_base_branch!r} must be None or a branch name "
            "without the remote prefix, such as 'master'.")
    if stats_output_file is not None and (not isinstance(stats_output_file, str) or not stats_output_file):
        raise ConfigError("STATS_OUTPUT_FILE must be None or a non-empty path.")
    if stats_output_file and os.path.abspath(stats_output_file) == os.path.abspath(output_file):
        raise ConfigError("STATS_OUTPUT_FILE must be a different file from OUTPUT_FILE.")
    return Config(os.path.abspath(repos_folder), parse_branch_name(target_branch),
                  remote_name, output_file, template, bb, fallback_base_branch, stats_output_file)


def _validate_bitbucket_base_url(url) -> Optional[str]:
    if url is None:
        return None
    if not isinstance(url, str) or not re.match(r"^https?://[^/\s?#]+(/[^\s?#]*)?$", url.strip()):
        raise ConfigError(
            f"BITBUCKET_BASE_URL {url!r} must be None or a web address such as "
            "'https://bitbucket.mycompany.com' (no query string or #).")
    parts = urllib.parse.urlsplit(url.strip())
    if parts.username or parts.password:
        raise ConfigError("BITBUCKET_BASE_URL must not contain a user name or password.")
    return url.strip().rstrip("/")


def _validate_template(tpl) -> Optional[Dict[str, str]]:
    if tpl is None:
        return None
    if isinstance(tpl, (tuple, list)) and len(tpl) == 2:
        tpl = dict(zip(TEMPLATE_KEYS, tpl))
    if (not isinstance(tpl, dict) or not set(TEMPLATE_KEYS) <= set(tpl)
            or not set(tpl) <= set(TEMPLATE_KEYS + TEMPLATE_OPTIONAL_KEYS)):
        raise ConfigError(
            "DIFF_URL_TEMPLATE must be None or a pair of templates: "
            "{'commit': '...', 'compare': '...'} (or a 2-tuple in that order), "
            "optionally with a 'file' template.")
    for key in tpl:
        value = tpl[key]
        if not isinstance(value, str) or not value:
            raise ConfigError(f"DIFF_URL_TEMPLATE[{key!r}] must be a non-empty string.")
        try:
            fields = [f for _, f, _, _ in string.Formatter().parse(value) if f is not None]
        except ValueError as exc:
            raise ConfigError(f"DIFF_URL_TEMPLATE[{key!r}] is not a valid template: {exc}")
        unknown = sorted({f for f in fields if f not in TEMPLATE_PLACEHOLDERS})
        if unknown:
            raise ConfigError(
                f"DIFF_URL_TEMPLATE[{key!r}] uses unknown placeholder(s) "
                f"{', '.join('{' + u + '}' for u in unknown)}. Allowed: "
                f"{', '.join('{' + p + '}' for p in sorted(TEMPLATE_PLACEHOLDERS))}.")
        if not re.match(r"^\{base_url\}|^https?://", value):
            raise ConfigError(
                f"DIFF_URL_TEMPLATE[{key!r}] must start with {{base_url}} or http(s)://.")
    return dict(tpl)


# --------------------------------------------------------------------------
# Running git
# --------------------------------------------------------------------------

_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")


def _git_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env["GIT_TERMINAL_PROMPT"] = "0"   # never block waiting for a password
    return env


def run_git(repo: str, args: Sequence[str]) -> str:
    """Run git in `repo` and return stdout decoded as UTF-8 (undecodable bytes replaced)."""
    proc = subprocess.run(["git", "-C", repo, *args], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_env())
    if proc.returncode != 0:
        raise GitError(args, proc.returncode, proc.stderr.decode("utf-8", errors="replace"))
    return proc.stdout.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Repository discovery
# --------------------------------------------------------------------------

def discover_repos(repos_folder: str, max_depth: int = 3) -> Tuple[List[str], List[str]]:
    """Git repositories under repos_folder, and the folders skipped because none was found.

    A folder that contains .git is a repository; it is not searched further. Other
    folders are searched down to max_depth levels below repos_folder, so both
    REPOS_FOLDER/<repo> and REPOS_FOLDER/<group>/<repo> are found. Names are paths
    relative to repos_folder with "/" between folders, e.g. "payments/api-service".
    Skipped: the highest folders that hold no repository at any searched depth.
    A symlink to a repository counts as a repository, but symlinked folders are not
    searched further (so a link loop or a second link to a folder can't repeat repos).
    """
    repos: List[str] = []
    skipped: List[str] = []

    def walk(path: str, rel: List[str]) -> bool:
        found = False
        empty_children: List[str] = []
        try:
            entries = sorted(os.listdir(path), key=_sort_key)
        except OSError:
            entries = []
        for entry in entries:
            child = os.path.join(path, entry)
            if not os.path.isdir(child):
                continue
            name = "/".join(rel + [entry])
            if os.path.exists(os.path.join(child, ".git")):
                repos.append(name)
                found = True
                continue
            if len(rel) + 1 < max_depth and not os.path.islink(child):
                if walk(child, rel + [entry]):
                    found = True
                    continue
            empty_children.append(name)
        if found or not rel:
            skipped.extend(empty_children)   # report these; the parent holds repositories
        return found

    walk(repos_folder, [])
    skipped.sort(key=lambda n: [_sort_key(p) for p in n.split("/")])
    return repos, skipped


def _sort_key(s: str):
    return (s.casefold(), s)


# --------------------------------------------------------------------------
# Branch selection
# --------------------------------------------------------------------------

@dataclasses.dataclass
class BranchSelection:
    target_ref: Optional[str] = None          # "origin/release/SG_19.18.0" if it exists
    base_ref: Optional[str] = None            # "origin/hotfix/SG_19.17.1" if chosen
    candidates: List[BranchName] = dataclasses.field(default_factory=list)
    ignored: List[Tuple[str, str]] = dataclasses.field(default_factory=list)  # (ref, reason)
    outcome: str = ""                          # "ok" | "no_target" | "no_base" | "tie"
    tie: List[BranchName] = dataclasses.field(default_factory=list)
    # other candidates with the same version as the chosen base (e.g. a hotfix/
    # branch when release/ was preferred); shown on the page, never compared
    same_version_as_base: List[BranchName] = dataclasses.field(default_factory=list)
    base_is_fallback: bool = False             # base is FALLBACK_BASE_BRANCH, not a candidate
    fallback_ref: Optional[str] = None         # "origin/master" when a fallback was configured


def list_remote_branches(repo: str) -> List[str]:
    # `%(refname)` (full name) is used instead of `%(refname:short)`: the short form
    # becomes "remotes/<remote>/..." when a local branch has the same name, which
    # would silently drop that remote branch from the candidates.
    out = run_git(repo, ["branch", "-r", "--format=%(refname)"])
    refs = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("refs/remotes/"):
            refs.append(line[len("refs/remotes/"):])
        elif line:
            refs.append(line)
    return refs


def select_branches(remote_refs: Sequence[str], remote: str, target: BranchName,
                    fallback_base: Optional[str] = None) -> BranchSelection:
    sel = BranchSelection()
    prefix = remote + "/"
    for ref in remote_refs:
        if not ref.startswith(prefix):
            sel.ignored.append((ref, f"not on remote '{remote}'"))
            continue
        parsed = parse_branch_name(ref[len(prefix):])
        if parsed is None:
            sel.ignored.append((ref, "does not match <release|hotfix>/<Country>_<Version>"))
        elif parsed.country != target.country:
            sel.ignored.append((ref, f"country '{parsed.country}' is not '{target.country}'"))
        else:
            sel.candidates.append(parsed)
    sel.candidates.sort(key=lambda b: (b.version_key, b.name))

    if not any(c.name == target.name for c in sel.candidates):
        sel.outcome = "no_target"
        return sel
    sel.target_ref = prefix + target.name

    lower = [c for c in sel.candidates if c.version_key < target.version_key]
    if not lower:
        if fallback_base:
            sel.fallback_ref = prefix + fallback_base
            if sel.fallback_ref in remote_refs:
                sel.base_ref = sel.fallback_ref
                sel.base_is_fallback = True
                sel.outcome = "ok"
                return sel
        sel.outcome = "no_base"
        return sel
    best = max(c.version_key for c in lower)
    closest = [c for c in lower if c.version_key == best]
    chosen = closest[0] if len(closest) == 1 else None
    if chosen is None:
        # Tie rule: when release/ and hotfix/ branches share the closest lower
        # version, the single release/ branch is the base. Any other tie (two
        # release/ branches, or only hotfix/ branches) is an error.
        releases = [c for c in closest if c.kind == "release"]
        if len(releases) == 1:
            chosen = releases[0]
    if chosen is None:
        sel.outcome = "tie"
        sel.tie = closest
        return sel
    sel.base_ref = prefix + chosen.name
    sel.same_version_as_base = [c for c in closest if c is not chosen]
    sel.outcome = "ok"
    return sel


# --------------------------------------------------------------------------
# git log: fetch and parse
# --------------------------------------------------------------------------

# Field separator and markers. With `-z`, git separates every output token with NUL,
# and NUL cannot occur in a commit message, author, email or date, so each field is
# exactly one NUL-separated token. Paths are not quoted or escaped under -z.
_COMMIT_MARK = "\x01COMMIT\x01"
_END_MARK = "\x01END\x01"
LOG_FORMAT = f"%x00{_COMMIT_MARK}%x00%H%x00%an%x00%ae%x00%ad%x00%B%x00{_END_MARK}"
_STATUS_RE = re.compile(r"^([ACDMRTUXB])(\d*)$")


@dataclasses.dataclass
class FileChange:
    status: str                 # letter: A C D M R T U X B
    score: str                  # similarity score digits for R/C (e.g. "087"), else ""
    path: str                   # path the commit is listed under (new path for R/C)
    old_path: Optional[str] = None  # source path for R/C


@dataclasses.dataclass
class Commit:
    hash: str
    author_name: str
    author_email: str
    author_date: str
    message: str
    changes: List[FileChange]


def git_log_args(base_ref: str, target_ref: str) -> List[str]:
    """The git log command. Full refnames make sure only remote-tracking refs are read."""
    return [
        "log", f"refs/remotes/{base_ref}..refs/remotes/{target_ref}",
        "--name-status", "--date=iso-strict", f"--format={LOG_FORMAT}",
        "-z",                    # NUL-separated output, unquoted paths
        "--encoding=UTF-8",      # re-encode messages that declare another encoding
        "--no-color", "--no-show-signature", "--no-mailmap",
        "--",
    ]


def parse_log(output: str) -> List[Commit]:
    """Parse the output of git_log_args(). Commits are returned in git's order."""
    tokens = output.split("\x00")
    commits: List[Commit] = []
    i, n = 0, len(tokens)
    current: Optional[Commit] = None
    while i < n:
        tok = tokens[i]
        if tok == _COMMIT_MARK:
            if i + 6 >= n or tokens[i + 6] != _END_MARK:
                raise LogParseError(f"unexpected git log output near token {i}")
            h, an, ae, ad, body = tokens[i + 1:i + 6]
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", h):
                raise LogParseError(f"unexpected commit hash {h!r} in git log output")
            current = Commit(h, an, ae, ad, _strip_trailing_newlines(body), [])
            commits.append(current)
            i += 7
            continue
        status_tok = tok.lstrip("\n")
        if status_tok == "":
            i += 1
            continue
        m = _STATUS_RE.match(status_tok)
        if current is None or not m:
            raise LogParseError(f"unexpected token {tok!r} in git log output")
        letter, score = m.groups()
        if letter in ("R", "C"):
            if i + 2 >= n:
                raise LogParseError("truncated rename/copy entry in git log output")
            current.changes.append(FileChange(letter, score, tokens[i + 2], tokens[i + 1]))
            i += 3
        else:
            if i + 1 >= n:
                raise LogParseError("truncated file entry in git log output")
            current.changes.append(FileChange(letter, score, tokens[i + 1]))
            i += 2
    return commits


def _strip_trailing_newlines(text: str) -> str:
    # git terminates each message with a newline; only trailing line breaks are
    # removed. Everything else (including leading/inner blank lines) is kept.
    return text.rstrip("\r\n")


# --------------------------------------------------------------------------
# Links
# --------------------------------------------------------------------------
#
# Built-in formats. None of these platforms documents its per-file anchors, so each
# format was checked against real links quoted in public sources:
#   GitHub    - Stack Overflow question 44029061 (live compare link with a SHA-256 anchor)
#   GitLab    - forum.gitlab.com topic 55921 (compare link; anchor = SHA-1 of the path)
#   Azure     - Stack Overflow answer 60229869 (branch compare URL, GB/GC/GT prefixes)
#   Bitbucket - Atlassian issues BCLOUD-11657 and BCLOUD-16327 (branches/compare/a%0Db)
#
# GitHub (github.com)
#   commit : https://github.com/<owner>/<repo>/commit/<sha>#diff-<sha256(path)>
#   compare: https://github.com/<owner>/<repo>/compare/<base>...<target>#diff-<sha256(path)>
#   GitHub's commit and compare pages give each file's diff the anchor
#   "diff-" + hex SHA-256 of the file path (the old MD5 anchors were replaced).
#   "base...target" (three dots) shows what target has that base does not, i.e.
#   the same range as `git log base..target`.
#
# GitLab (gitlab.com)
#   commit : https://gitlab.com/<group>/<project>/-/commit/<sha>#<sha1(path)>
#   compare: https://gitlab.com/<group>/<project>/-/compare/<base>...<target>#<sha1(path)>
#   GitLab's diff file anchors are the hex SHA-1 of the file path. On very large
#   compare pages that GitLab paginates, the anchor only scrolls when the file is
#   on the first page. "base...target" compares from the merge base, like GitHub.
#
# Bitbucket Cloud (bitbucket.org)
#   commit : https://bitbucket.org/<workspace>/<repo>/commits/<sha>#chg-<path>
#   compare: https://bitbucket.org/<workspace>/<repo>/branches/compare/<target>%0D<base>#chg-<path>
#   The "branches/compare/<a>%0D<b>" form is confirmed by Atlassian issues
#   BCLOUD-11657 and BCLOUD-16327. The order used here, source (target, the branch
#   with the new changes) first and destination (base) second, and the per-file
#   anchor "chg-<path>" follow Bitbucket's own UI links; no written Atlassian
#   documentation states them, so check one generated link before relying on it.
#
# Azure DevOps (dev.azure.com)
#   commit : https://dev.azure.com/<org>/<project>/_git/<repo>/commit/<sha>
#   compare: https://dev.azure.com/<org>/<project>/_git/<repo>/branches
#            ?baseVersion=GB<base>&targetVersion=GB<target>&_a=files
#   "GB" prefixes a branch name (GC = commit, GT = tag); _a=files opens the file
#   diff tab. No documented URL parameter or anchor selects a single file on
#   these pages, so Azure links are not narrowed to the file (the spec allows
#   this: "where the platform allows").
#
# Bitbucket Server / Data Center (self-hosted; used when BITBUCKET_BASE_URL is set
# to an address other than https://bitbucket.org). <repo> = <BASE>/projects/<KEY>/repos/<slug>
# (personal repositories "~user": <BASE>/users/<user>/repos/<slug>)
#   commit : <repo>/commits/<sha>#<path>
#   compare: <repo>/compare/diff?sourceBranch=refs%2Fheads%2F<target>
#            &targetBranch=refs%2Fheads%2F<base>#<path>
#   file   : <repo>/browse/<path>?at=<target commit>
#   The file format is from Atlassian's Bitbucket Server API docs
#   (NavBuilder.BrowseRepoResource: /browse/...?at=<ref or commit>) and a community
#   answer quoting browse/<path>?at=<branch>. The commit and compare formats are
#   the ones Bitbucket Server's own pages use; Atlassian doesn't document them, so
#   check one link of each kind on your server. In compare/diff, "source" is the
#   branch with the new changes (target) and "target" is the branch it would merge
#   into (base). The project key and repository slug are used exactly as they
#   appear in the remote URL.
#
# File links (column 1, "File ↗") open the file as it is at the target branch's
# current commit (the hash comes from `git rev-parse`), and only for paths that exist
# in that commit (`git ls-tree`), so deleted files get no file link:
#   GitHub   {repo}/blob/<target commit>/<path>
#   GitLab   {repo}/-/blob/<target commit>/<path>
#   Bitbucket Cloud {repo}/src/<target commit>/<path>
#   Azure    {repo}?path=/<path>&version=GC<target commit>
#
# Encoding: branch names are percent-encoded including "/" (-> %2F). File paths are
# percent-encoded with "/" kept, because "/" separates folders in the URL path,
# query and fragment.

KNOWN_HOSTS = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
    "dev.azure.com": "azure",
}
PLATFORM_LABELS = {"github": "GitHub", "gitlab": "GitLab", "bitbucket": "Bitbucket",
                   "azure": "Azure DevOps", "template": "DIFF_URL_TEMPLATE",
                   "bitbucket-server": "Bitbucket Server / Data Center (BITBUCKET_BASE_URL)"}


def _enc_branch(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def _enc_path(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


def _sha256(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8", errors="surrogateescape")).hexdigest()


def _sha1(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8", errors="surrogateescape")).hexdigest()


def remote_url_to_https(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Convert a git remote URL to (https base URL, host). (None, None) if not possible.

    git@host:org/repo.git           -> https://host/org/repo
    ssh://git@host:22/org/repo.git  -> https://host/org/repo
    https://user:token@host/org/repo.git -> https://host/org/repo (credentials removed)
    git@ssh.dev.azure.com:v3/org/project/repo -> https://dev.azure.com/org/project/_git/repo
    Local paths and file:// URLs cannot be converted.
    """
    url = url.strip()
    if not url:
        return None, None
    scheme_m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", url)
    if scheme_m:
        scheme = scheme_m.group(1).lower()
        if scheme not in ("http", "https", "ssh", "git", "git+ssh", "ssh+git"):
            return None, None
        try:
            parts = urllib.parse.urlsplit(url)
            host = (parts.hostname or "").lower()
            port = parts.port
        except ValueError:          # malformed host or port
            return None, None
        if not host:
            return None, None
        path = parts.path
        if scheme in ("http", "https"):
            netloc = host + (f":{port}" if port else "")
            out_scheme = scheme
        else:
            netloc = host          # an ssh/git port is not the web port
            out_scheme = "https"
    else:
        # scp-like syntax: [user@]host:path  (no slash before the colon)
        m = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", url)
        if not m or os.path.exists(url):
            return None, None
        host = m.group(1).lower()
        path = "/" + m.group(2).lstrip("/")
        netloc = host
        out_scheme = "https"
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path.strip("/"):
        return None, None

    if host in ("ssh.dev.azure.com", "dev.azure.com"):
        segs = [s for s in path.split("/") if s]
        if host == "ssh.dev.azure.com":
            # v3/<org>/<project>/<repo>
            if len(segs) == 4 and segs[0] == "v3":
                return f"https://dev.azure.com/{segs[1]}/{segs[2]}/_git/{segs[3]}", "dev.azure.com"
            return None, None
        return f"https://dev.azure.com{path}", "dev.azure.com"
    return f"{out_scheme}://{netloc}{path}", host


@dataclasses.dataclass
class LinkBuilder:
    platform: Optional[str]          # github | gitlab | bitbucket | bitbucket-server | azure | template | None
    base_url: Optional[str]
    template: Optional[Dict[str, str]] = None
    unavailable_reason: str = ""     # why there are no links (when platform is None)

    @property
    def available(self) -> bool:
        return self.platform is not None

    def commit_url(self, commit: str, path: Optional[str]) -> Optional[str]:
        if not self.available:
            return None
        b = self.base_url
        p = self.platform
        if p == "github":
            return f"{b}/commit/{commit}" + (f"#diff-{_sha256(path)}" if path else "")
        if p == "gitlab":
            return f"{b}/-/commit/{commit}" + (f"#{_sha1(path)}" if path else "")
        if p == "bitbucket":
            return f"{b}/commits/{commit}" + (f"#chg-{_enc_path(path)}" if path else "")
        if p == "azure":
            return f"{b}/commit/{commit}"
        if p == "bitbucket-server":
            return f"{b}/commits/{commit}" + (f"#{_enc_path(path)}" if path else "")
        return self._fill("commit", commit=commit, path=path)

    def compare_url(self, base: str, target: str, path: str) -> Optional[str]:
        if not self.available:
            return None
        b = self.base_url
        p = self.platform
        eb, et, ep = _enc_branch(base), _enc_branch(target), _enc_path(path)
        if p == "github":
            return f"{b}/compare/{eb}...{et}#diff-{_sha256(path)}"
        if p == "gitlab":
            return f"{b}/-/compare/{eb}...{et}#{_sha1(path)}"
        if p == "bitbucket":
            return f"{b}/branches/compare/{et}%0D{eb}#chg-{ep}"
        if p == "azure":
            return f"{b}/branches?baseVersion=GB{eb}&targetVersion=GB{et}&_a=files"
        if p == "bitbucket-server":
            return (f"{b}/compare/diff?sourceBranch={_enc_branch('refs/heads/' + target)}"
                    f"&targetBranch={_enc_branch('refs/heads/' + base)}#{ep}")
        return self._fill("compare", base=base, target=target, path=path)

    @property
    def has_file_links(self) -> bool:
        if self.platform == "template":
            return "file" in self.template
        return self.platform is not None

    def file_url(self, target_commit: str, path: str) -> Optional[str]:
        """The file as it is at the target branch's commit."""
        if not self.has_file_links:
            return None
        b, p, ep = self.base_url, self.platform, _enc_path(path)
        if p == "github":
            return f"{b}/blob/{target_commit}/{ep}"
        if p == "gitlab":
            return f"{b}/-/blob/{target_commit}/{ep}"
        if p == "bitbucket":
            return f"{b}/src/{target_commit}/{ep}"
        if p == "azure":
            return f"{b}?path=/{ep}&version=GC{target_commit}"
        if p == "bitbucket-server":
            return f"{b}/browse/{ep}?at={target_commit}"
        return self._fill("file", path=path, target_commit=target_commit)

    def _fill(self, key: str, commit: str = "", base: str = "", target: str = "",
              path: Optional[str] = None, target_commit: str = "") -> str:
        values = {
            "base_url": self.base_url or "",
            "commit": commit,
            "target_commit": target_commit,
            "base": _enc_branch(base),
            "target": _enc_branch(target),
            "path": _enc_path(path) if path else "",
            "path_sha256": _sha256(path) if path else "",
            "path_sha1": _sha1(path) if path else "",
        }
        return self.template[key].format_map(values)


def bitbucket_repo_path(remote_url: str) -> Optional[Tuple[str, str]]:
    """(project or workspace, repository slug) from a Bitbucket remote URL, as written.

    ssh://git@host:7999/proj/repo.git        -> ("proj", "repo")
    https://host/scm/proj/repo.git           -> ("proj", "repo")
    https://host/context/scm/~user/repo.git  -> ("~user", "repo")
    git@bitbucket.org:workspace/repo.git     -> ("workspace", "repo")
    """
    base_url, _ = remote_url_to_https(remote_url)
    if not base_url:
        return None
    path = urllib.parse.urlsplit(base_url).path
    segs = [s for s in path.split("/") if s]
    if "scm" in segs:
        segs = segs[segs.index("scm") + 1:]
    if len(segs) < 2:
        return None
    return segs[-2], segs[-1]


def make_bitbucket_link_builder(remote_url: Optional[str], bitbucket_base_url: str) -> LinkBuilder:
    if not remote_url:
        return LinkBuilder(None, None, unavailable_reason="the remote URL could not be read")
    owner_repo = bitbucket_repo_path(remote_url)
    if not owner_repo:
        return LinkBuilder(None, None, unavailable_reason=(
            "BITBUCKET_BASE_URL is set, but the project and repository name can't be read "
            "from the remote URL (for example, it is a local path)"))
    owner, slug = (urllib.parse.quote(x, safe="~") for x in owner_repo)
    host = (urllib.parse.urlsplit(bitbucket_base_url).hostname or "").lower()
    if host == "bitbucket.org":
        return LinkBuilder("bitbucket", f"{bitbucket_base_url}/{owner}/{slug}")
    if owner.startswith("~"):
        return LinkBuilder("bitbucket-server", f"{bitbucket_base_url}/users/{owner[1:]}/repos/{slug}")
    return LinkBuilder("bitbucket-server", f"{bitbucket_base_url}/projects/{owner}/repos/{slug}")


def make_link_builder(remote_url: Optional[str], template: Optional[Dict[str, str]],
                      bitbucket_base_url: Optional[str] = None) -> LinkBuilder:
    if bitbucket_base_url:
        return make_bitbucket_link_builder(remote_url, bitbucket_base_url)
    if not remote_url:
        return LinkBuilder(None, None, unavailable_reason="the remote URL could not be read")
    base_url, host = remote_url_to_https(remote_url)
    if host in KNOWN_HOSTS and base_url:
        return LinkBuilder(KNOWN_HOSTS[host], base_url)
    if template:
        needs_base = any("{base_url}" in t for t in template.values())
        if needs_base and not base_url:
            return LinkBuilder(None, None, unavailable_reason=(
                "DIFF_URL_TEMPLATE uses {base_url}, but the remote URL cannot be converted "
                "to a web address (for example, it is a local path)"))
        return LinkBuilder("template", base_url, template)
    return LinkBuilder(None, base_url, unavailable_reason=(
        "the remote host is not a recognised platform (GitHub, GitLab, Bitbucket, "
        "Azure DevOps) and neither BITBUCKET_BASE_URL nor DIFF_URL_TEMPLATE is set"))


# --------------------------------------------------------------------------
# Data model per repository
# --------------------------------------------------------------------------

STATE_ERROR, STATE_CHANGES, STATE_NO_CHANGE = "error", "changes", "no_change"


@dataclasses.dataclass
class RepoReport:
    name: str
    path: str
    state: str = STATE_ERROR
    reason: str = ""                                   # error / no-change message
    selection: Optional[BranchSelection] = None
    commits: List[Commit] = dataclasses.field(default_factory=list)
    files: List[Tuple[str, List[Tuple[Commit, FileChange]]]] = dataclasses.field(default_factory=list)
    no_file_commits: List[Commit] = dataclasses.field(default_factory=list)
    links: Optional[LinkBuilder] = None
    target_commit: Optional[str] = None                 # hash the target branch points to
    target_paths: frozenset = frozenset()               # paths that exist in target_commit
    diffs: Dict[str, Tuple[str, str]] = dataclasses.field(default_factory=dict)  # path -> (diff, error)

    @property
    def target_branch(self) -> Optional[str]:
        return self.selection.target_ref if self.selection else None

    @property
    def base_branch(self) -> Optional[str]:
        return self.selection.base_ref if self.selection else None


def git_diff_args(base_ref: str, target_ref: str, *extra: str) -> List[str]:
    """`git diff base...target`: the code changes on the target since it branched from
    the base (diff from the merge base to the target), the same scope as
    `git log base..target`. One call covers every file of the repository."""
    return [
        "diff", f"refs/remotes/{base_ref}...refs/remotes/{target_ref}",
        "--no-color", "--no-ext-diff", "--no-textconv", "--find-renames",
        "--src-prefix=a/", "--dst-prefix=b/", *extra, "--",
    ]


def repo_diffs(repo: str, base_ref: str, target_ref: str) -> Dict[str, str]:
    """Per-file diff text for the whole range, from two git calls (instead of one per
    file): `--name-status -z` names each file exactly, and the patch output has one
    section per file in the same order, each starting with a "diff --git " line (a
    line inside a hunk always starts with " ", "+", "-" or "\\", so it can't be
    mistaken for a section start). Keys: the new path, and for a rename or copy also
    the old path, so both rows of a renamed file show the rename."""
    names = run_git(repo, git_diff_args(base_ref, target_ref, "--name-status", "-z"))
    tokens = [t for t in names.split("\x00")]
    entries: List[List[str]] = []
    i = 0
    while i < len(tokens):
        st = tokens[i]
        if not st:
            i += 1
            continue
        if st[0] in "RC":
            entries.append([tokens[i + 2], tokens[i + 1]])
            i += 3
        else:
            entries.append([tokens[i + 1]])
            i += 2
    patch = run_git(repo, git_diff_args(base_ref, target_ref, "-p"))
    sections: List[List[str]] = []
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            sections.append([])
        if sections:
            sections[-1].append(line)
    if len(sections) != len(entries):
        raise LogParseError(f"git diff listed {len(entries)} files but printed "
                            f"{len(sections)} file sections")
    out: Dict[str, str] = {}
    for paths, sec in zip(entries, sections):
        text = "\n".join(sec).rstrip("\n") + "\n"
        for pth in paths:
            out[pth] = out.get(pth, "") + text
    return out


def group_by_file(commits: Sequence[Commit]):
    """Group (commit, change) pairs by file path, keeping git's commit order per file."""
    by_path: Dict[str, List[Tuple[Commit, FileChange]]] = {}
    no_files: List[Commit] = []
    for c in commits:
        if not c.changes:
            no_files.append(c)
        for ch in c.changes:
            by_path.setdefault(ch.path, []).append((c, ch))
    files = sorted(by_path.items(), key=lambda kv: _sort_key(kv[0]))
    return files, no_files


def process_repo(cfg: Config, name: str, log: Callable[[str], None]) -> RepoReport:
    rep = RepoReport(name=name, path=os.path.join(cfg.repos_folder, *name.split("/")))
    remote = cfg.remote_name
    target = cfg.target
    try:
        try:
            run_git(rep.path, ["fetch", remote, "--prune"])
        except GitError as exc:
            rep.reason = f"Fetch failed, so no data is shown for this repository.\n{exc}"
            return rep

        sel = select_branches(list_remote_branches(rep.path), remote, target,
                              cfg.fallback_base_branch)
        rep.selection = sel
        for ref, why in sel.ignored:
            log(f"  [{name}] ignored branch {ref}: {why}")

        if sel.outcome == "no_target":
            rep.state = STATE_NO_CHANGE
            rep.reason = f"Branch `{target.name}` not found on remote."
            return rep
        if sel.outcome == "no_base":
            rep.reason = f"No previous branch found for `{target.country}` below `{target.version}`"
            if sel.fallback_ref:
                rep.reason += f", and the fallback base branch `{sel.fallback_ref}` was not found on remote."
            return rep
        if sel.outcome == "tie":
            names = ", ".join(f"`{remote}/{b.name}`" for b in sel.tie)
            rep.reason = (f"More than one candidate branch has the closest lower version, and "
                          f"they are not one release/ branch plus hotfix/ branches, so no base "
                          f"branch was chosen: {names}")
            return rep

        try:
            remote_url = run_git(rep.path, ["remote", "get-url", remote]).strip()
        except GitError:
            remote_url = None
        rep.links = make_link_builder(remote_url, cfg.diff_url_template, cfg.bitbucket_base_url)

        rep.commits = parse_log(run_git(rep.path, git_log_args(sel.base_ref, sel.target_ref)))
        if not rep.commits:
            rep.state = STATE_NO_CHANGE
            rep.reason = f"No commits on `{sel.target_ref}` that are not on `{sel.base_ref}`."
            return rep
        rep.files, rep.no_file_commits = group_by_file(rep.commits)
        if rep.links.has_file_links:
            ref = f"refs/remotes/{sel.target_ref}"
            rep.target_commit = run_git(rep.path, ["rev-parse", "--verify", ref + "^{commit}"]).strip()
            listing = run_git(rep.path, ["ls-tree", "-r", "-z", "--name-only", "--full-tree",
                                         rep.target_commit])
            rep.target_paths = frozenset(p for p in listing.split("\x00") if p)
        try:
            all_diffs = repo_diffs(rep.path, sel.base_ref, sel.target_ref)
            for path, _ in rep.files:
                rep.diffs[path] = (all_diffs.get(path, ""), "")
        except (GitError, LogParseError) as exc:
            # the commit list is still shown; only the code diff column reports the error
            for path, _ in rep.files:
                rep.diffs[path] = ("", f"Code diff not available: {exc}")
        rep.state = STATE_CHANGES
        return rep
    except (GitError, LogParseError, OSError) as exc:
        rep.state = STATE_ERROR
        rep.reason = str(exc)
        return rep
    except Exception as exc:  # one failing repository must not stop the others
        rep.state = STATE_ERROR
        rep.reason = f"Unexpected error: {type(exc).__name__}: {exc}"
        return rep


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Report:
    config: Config
    generated_at: str
    repos: List[RepoReport]
    skipped: List[str]
    html: str
    stats_html: Optional[str] = None


def generate_report(repos_folder, target_branch, remote_name, output_file, diff_url_template,
                    bitbucket_base_url=None, fallback_base_branch=None, stats_output_file=None, *,
                    log: Callable[[str], None] = print,
                    now: Optional[_dt.datetime] = None) -> Report:
    """Build the review page. Raises ConfigError for invalid inputs, before any git call."""
    cfg = load_config(repos_folder, target_branch, remote_name, output_file, diff_url_template,
                      bitbucket_base_url, fallback_base_branch, stats_output_file)
    generated = (now or _dt.datetime.now().astimezone()).isoformat(timespec="seconds")

    log(f"Target branch {cfg.target.name}: country {cfg.target.country}, "
        f"version {cfg.target.version}")
    repo_names, skipped = discover_repos(cfg.repos_folder, REPO_SEARCH_DEPTH)
    for s in skipped:
        log(f"Skipped (not a git repository): {s}")

    reports = []
    for name in repo_names:
        log(f"Processing {name} ...")
        rep = process_repo(cfg, name, log)
        summary = {STATE_CHANGES: "changes", STATE_NO_CHANGE: "No change",
                   STATE_ERROR: "ERROR"}[rep.state]
        log(f"  [{name}] {summary}" + (f": {rep.reason}" if rep.reason else ""))
        reports.append(rep)

    page = render_html(cfg, generated, reports)
    with open(cfg.output_file, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(page)
    log(f"Wrote {os.path.abspath(cfg.output_file)}")
    stats = None
    if cfg.stats_output_file:
        stats = render_stats_html(cfg, generated, reports)
        with open(cfg.stats_output_file, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(stats)
        log(f"Wrote {os.path.abspath(cfg.stats_output_file)}")
    return Report(cfg, generated, reports, skipped, page, stats)


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

STATUS_NAMES = {"A": "Added", "M": "Modified", "D": "Deleted", "R": "Renamed", "C": "Copied",
                "T": "Type changed", "U": "Unmerged", "X": "Unknown", "B": "Broken pairing"}
FILTER_STATUSES = ["A", "M", "D", "R", "C", "T", "U", "X", "B"]
EXT = 'target="_blank" rel="noopener noreferrer"'


def esc(text) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def anchor_id(*parts: str) -> str:
    """Stable, readable HTML id. Characters outside [A-Za-z0-9._/-] become ~HEX."""
    out = []
    for ch in "/".join(parts):
        if re.match(r"[A-Za-z0-9._/-]", ch):
            out.append(ch)
        else:
            out.append("~" + "".join(f"{b:02X}" for b in ch.encode("utf-8", "surrogateescape")))
    return "".join(out)


def render_html(cfg: Config, generated: str, reports: Sequence[RepoReport]) -> str:
    errors = [r for r in reports if r.state == STATE_ERROR]
    changes = [r for r in reports if r.state == STATE_CHANGES]
    nochange = [r for r in reports if r.state == STATE_NO_CHANGE]
    present = sorted({ch.status for r in changes for c in r.commits for ch in c.changes},
                     key=FILTER_STATUSES.index)
    has_nofile = any(r.no_file_commits for r in changes)

    out: List[str] = []
    w = out.append
    w("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n")
    w('<meta name="viewport" content="width=device-width, initial-scale=1">\n')
    w(f"<title>Release review: {esc(cfg.target.name)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n")

    # ---- top: page facts ------------------------------------------------
    w('<header class="page-head">\n')
    w(f"<h1>Release review: <code>{esc(cfg.target.name)}</code></h1>\n")
    if cfg.stats_output_file:
        w(f'<p class="pagelink"><a href="{esc(_rel_href(cfg.output_file, cfg.stats_output_file))}">'
          "Per-file statistics page →</a></p>\n")
    w('<dl class="facts">\n')
    for k, v in (("Generated", generated), ("REPOS_FOLDER", cfg.repos_folder),
                 ("TARGET_BRANCH", cfg.target.name), ("Country", cfg.target.country),
                 ("Version", cfg.target.version), ("Remote", cfg.remote_name),
                 ("BITBUCKET_BASE_URL", cfg.bitbucket_base_url or ""),
                 ("FALLBACK_BASE_BRANCH", cfg.fallback_base_branch or "")):
        w(f"<dt>{esc(k)}</dt><dd><code>{esc(v)}</code></dd>\n")
    w("</dl>\n")

    w('<table class="overview"><thead><tr><th>Repository</th><th>State</th>'
      "<th>Base branch → target branch</th></tr></thead><tbody>\n")
    for group in (errors, changes, nochange):
        for r in group:
            state = {STATE_ERROR: '<span class="pill pill-error">Error</span>',
                     STATE_CHANGES: '<span class="pill pill-changes">Changes</span>',
                     STATE_NO_CHANGE: '<span class="pill pill-quiet">No change</span>'}[r.state]
            w(f'<tr class="ov-{r.state}"><td><a href="#{esc(anchor_id("repo", r.name))}">'
              f"{esc(r.name)}</a></td><td>{state}</td><td>{_branch_pair(r)}</td></tr>\n")
    if not reports:
        w('<tr><td colspan="3">No git repositories were found in REPOS_FOLDER.</td></tr>\n')
    w("</tbody></table>\n</header>\n")

    # ---- toolbar ---------------------------------------------------------
    w('<div class="toolbar" id="toolbar">\n')
    w('<input type="search" id="q" placeholder="Search file path, message, commit id, user" '
      'aria-label="Search file path, commit message, commit id or user">\n')
    w('<fieldset class="status-filter"><legend>Status</legend>\n')
    for s in present:
        w(f'<label><input type="checkbox" class="sf" value="{s}" checked> '
          f'<span class="badge b-{s}">{s}</span> {esc(STATUS_NAMES[s])}</label>\n')
    if has_nofile:
        w('<label><input type="checkbox" class="sf" value="none" checked> '
          "Commits with no file changes</label>\n")
    w("</fieldset>\n")
    w('<button type="button" id="expand-all">Expand all</button>'
      '<button type="button" id="collapse-all">Collapse all</button>'
      '<button type="button" id="clear-filters">Clear filters</button>'
      '<button type="button" id="toggle-diff" aria-pressed="false">Hide code diff column</button>'
      '<button type="button" id="toggle-details" aria-pressed="false">Hide changes column</button>'
      '<button type="button" id="draw-all" title="Code diffs are drawn as you scroll to them. '
      'This draws every one now (for printing or Ctrl+F).">Draw all diffs</button>\n')
    w('<div class="diff-search"><input type="search" id="qd" placeholder="Search code diffs" '
      'aria-label="Search inside the code diffs" autocomplete="off">'
      '<button type="button" id="qd-prev" title="Previous match (Shift+Enter)">↑</button>'
      '<button type="button" id="qd-next" title="Next match (Enter)">↓</button>'
      '<span id="qd-state" class="qd-state" aria-live="polite"></span></div>\n')
    w('<span id="filter-state" class="filter-state" aria-live="polite"></span>\n')
    w("</div>\n<main>\n")

    if errors:
        w('<section class="group group-error"><h2 class="group-title">Repositories with errors</h2>\n')
        for r in errors:
            _render_repo_message(w, r, "error", cfg.remote_name)
        w("</section>\n")
    if changes:
        w('<section class="group group-changes"><h2 class="group-title">Repositories with changes</h2>\n')
        for r in changes:
            _render_repo_changes(w, r, cfg.remote_name)
        w("</section>\n")
    if nochange:
        w('<section class="group group-quiet"><h2 class="group-title">No change</h2>\n')
        for r in nochange:
            _render_repo_message(w, r, "quiet", cfg.remote_name)
        w("</section>\n")

    w('<noscript><style>pre.diff[data-z]:not(.drawn)::before{content:"The code diff is stored '
      'compressed and needs JavaScript to be shown."}</style></noscript>\n')
    w(f"</main>\n<script>{JS}</script>\n</body>\n</html>\n")
    return "".join(out)


def _branch_pair(r: RepoReport) -> str:
    if r.base_branch and r.target_branch:
        fb = (' <span class="tag tag-fallback">fallback base</span>'
              if r.selection and r.selection.base_is_fallback else "")
        return f"<code>{esc(r.base_branch)}</code>{fb} → <code>{esc(r.target_branch)}</code>"
    if r.target_branch:
        return f"→ <code>{esc(r.target_branch)}</code>"
    return ""


def _render_branch_block(w, r: RepoReport, remote: str):
    sel = r.selection
    if sel is None:
        return
    w('<dl class="branches">\n')
    w(f"<dt>Target branch</dt><dd>{_code_or_empty(sel.target_ref)}</dd>\n")
    w(f"<dt>Base branch</dt><dd>{_code_or_empty(sel.base_ref)}</dd>\n")
    if sel.base_is_fallback:
        w(f'<dt>Fallback base</dt><dd class="fallback-note">No <code>release/</code> or '
          f"<code>hotfix/</code> branch has a version lower than the target, so "
          f"<code>{esc(sel.base_ref)}</code> (FALLBACK_BASE_BRANCH) is used as the base.</dd>\n")
    w("<dt>Matching candidate branches</dt><dd>")
    if sel.candidates:
        w('<ul class="cands">')
        for c in sel.candidates:
            ref = f"{remote}/{c.name}"
            tags = []
            if ref == sel.target_ref:
                tags.append('<span class="tag">target</span>')
            if ref == sel.base_ref:
                tags.append('<span class="tag">base</span>')
            if c in sel.same_version_as_base:
                tags.append('<span class="tag tag-same">same version as base, not used</span>')
            w(f"<li><code>{esc(ref)}</code> {' '.join(tags)}</li>")
        w("</ul>")
    w("</dd>\n")
    if sel.same_version_as_base:
        others = ", ".join(f"<code>{esc(remote + '/' + c.name)}</code>" for c in sel.same_version_as_base)
        w(f'<dt>Same version as base</dt><dd class="same-version">{others} has the same version as '
          f"the base branch. When release/ and hotfix/ share the closest lower version, the "
          f"release/ branch is used as the base.</dd>\n")
    w("</dl>\n")


def _code_or_empty(v: Optional[str]) -> str:
    return f"<code>{esc(v)}</code>" if v else ""


def _repo_summary(w, r: RepoReport, extra: str):
    rid = anchor_id("repo", r.name)
    w(f'<details class="repo repo-{r.state}" id="{esc(rid)}" open>\n')
    w(f'<summary class="repo-head"><span class="repo-name">{esc(r.name)}</span> {extra}'
      f'<a class="anchor" href="#{esc(rid)}" title="Link to this repository">#</a></summary>\n')


def _render_repo_message(w, r: RepoReport, kind: str, remote: str):
    label = ('<span class="pill pill-error">Error</span>' if kind == "error"
             else '<span class="pill pill-quiet">No change</span>')
    _repo_summary(w, r, label)
    w('<div class="repo-body">\n')
    w(f'<p class="reason reason-{kind}">{esc(r.reason)}</p>\n')
    _render_branch_block(w, r, remote)
    w("</div>\n</details>\n")


def _render_repo_changes(w, r: RepoReport, remote: str):
    _repo_summary(w, r, f'<span class="pair">[{_branch_pair(r)}]</span>')
    w('<div class="repo-body">\n')
    _render_branch_block(w, r, remote)
    if not r.links.available:
        w(f'<p class="note">Links aren’t available for this repository: '
          f"{esc(r.links.unavailable_reason)}. Commit ids are shown as plain text.</p>\n")
    else:
        w(f'<p class="note-small">Links: {esc(PLATFORM_LABELS[r.links.platform])} '
          f"(<code>{esc(r.links.base_url or '')}</code>)</p>\n")
    w('<p class="no-match" hidden>No entries in this repository match the current search or status filter.</p>\n')

    base = r.base_branch[len(remote) + 1:]        # branch names without "<remote>/"
    target = r.target_branch[len(remote) + 1:]
    # Three columns per file: column 1 = file path (with Compare/File links and
    # anchor), column 2 = the file's code diff (git diff base...target), column 3 =
    # the file's commits (status, message, date, commit, user). Columns 2 and 3 can
    # be collapsed per file or for the whole page.
    w('<div class="files">\n<div class="files-head"><div>File</div>'
      f'<div class="diff-label">Code diff: <code>{esc(r.base_branch)}...{esc(r.target_branch)}</code></div>'
      '<div class="detail-label">Changes: status, commit message, date, commit id, user</div></div>\n')
    for path, entries in r.files:
        fid = anchor_id("file", r.name, path)
        url = r.links.compare_url(base, target, path)
        compare = f'<a class="compare" href="{esc(url)}" {EXT}>Compare ↗</a>' if url else ""
        if r.target_commit and path in r.target_paths:
            furl = r.links.file_url(r.target_commit, path)
            if furl:
                compare += (f' <a class="filelink" href="{esc(furl)}" {EXT} '
                            f'title="File at {esc(r.target_branch)} ({esc(r.target_commit)})">File ↗</a>')
        _render_file_row(w, r, fid, f'<span class="path">{esc(path)}</span>', path, compare,
                         [(c, ch) for c, ch in entries], "file")
    if r.no_file_commits:
        fid = anchor_id("nofiles", r.name)
        _render_file_row(w, r, fid, '<span class="path">Commits with no file changes</span>', "", "",
                         [(c, None) for c in r.no_file_commits], "file nofiles")
    w("</div>\n</div>\n</details>\n")


def _render_file_row(w, r: RepoReport, fid: str, label: str, path: str, compare: str,
                     entries, classes: str):
    diff, err = r.diffs.get(path, ("", "")) if path else ("", "")
    lines = [] if err else diff_code_lines(diff)
    # Rows far from the screen are skipped by the browser's layout (CSS
    # content-visibility:auto) until scrolled near; this estimated height keeps the
    # scrollbar and jumps to a file accurate before the row is laid out.
    w(f'<div class="{classes}" id="{esc(fid)}" data-path="{esc(path)}" '
      f'style="contain-intrinsic-size:auto {_estimate_row_height(path, len(lines), entries)}px">\n')
    w(f'<div class="file-col"><div class="file-col-inner">{label}'
      f'<div class="file-links">{compare}'
      f'<a class="anchor" href="#{esc(fid)}" title="Link to this file">#</a></div></div></div>\n')
    w('<details class="diff-col" open><summary class="diff-head">Code diff</summary>\n')
    if path:
        if err:
            w(f'<p class="diff-error">{esc(err)}</p>\n')
        elif lines:
            # Stored compressed and drawn by the page script when the row comes near
            # the screen (see pack_diff and JS). The height is reserved up front so
            # the page doesn't jump while diffs are drawn.
            w(f'<pre class="diff" data-z="{pack_diff(lines)}" data-n="{len(lines)}" '
              f'style="--n:{len(lines)}"></pre>\n')
        else:
            w('<p class="diff-empty">git diff shows no difference for this file between the '
              'merge base and the target branch.</p>\n')
    w("</details>\n")
    w('<details class="detail-col" open><summary class="detail-head">Changes</summary>\n'
      '<ol class="entries">\n')
    for commit, ch in entries:
        _render_entry(w, r, commit, ch)
    w("</ol>\n</details>\n</div>\n")


def _estimate_row_height(path: str, n_diff_lines: int, entries) -> int:
    """Rough pixel height of a file row, used only until the browser lays it out."""
    diff_h = (24 + (min(600, 12 + 17 * n_diff_lines) if n_diff_lines else 40)) if path else 0
    changes_h = 24 + sum(62 + 19 * (c.message.count("\n") + 1) + (22 if ch and ch.old_path else 0)
                         for c, ch in entries)
    return max(60, diff_h, changes_h)


_DIFF_HEADER_SKIP = ("diff --git ", "index ", "--- ", "+++ ")


def diff_code_lines(diff: str) -> List[Tuple[str, str]]:
    """(kind, line) pairs for the code-diff column: the code changes only.

    git diff prints a header for each file (diff --git, index, ---, +++, and lines such
    as "new file mode" or "rename from"). The header is left out, and every line
    from the first @@ hunk header on is kept unchanged. When a file has no hunks at
    all (a pure rename, a mode change or a binary file), its header lines other than
    diff --git / index / --- / +++ are kept, because they are then the only record of
    the change, e.g. "rename from a.txt" or "Binary files ... differ".
    kind: "h" hunk header, "a" added, "d" removed, "c" context, "m" header info.
    """
    out: List[Tuple[str, str]] = []
    header: List[str] = []
    in_hunk = False
    has_hunk = False

    def flush_header():
        if not has_hunk:
            out.extend(("m", h) for h in header if not h.startswith(_DIFF_HEADER_SKIP))

    for line in diff.rstrip("\n").split("\n") if diff else []:
        if line.startswith("diff --git "):
            flush_header()
            header, in_hunk, has_hunk = [line], False, False
        elif line.startswith("@@"):
            in_hunk = has_hunk = True
            out.append(("h", line))
        elif not in_hunk:
            header.append(line)
        elif line.startswith("+"):
            out.append(("a", line))
        elif line.startswith("-"):
            out.append(("d", line))
        else:
            out.append(("c", line))
    flush_header()
    return out


def pack_diff(lines: Sequence[Tuple[str, str]]) -> str:
    """The code-diff lines of one file, gzip-compressed and base64-encoded.

    Each line is stored as its kind letter (h/a/d/c/m, see diff_code_lines) followed
    by the line exactly as git printed it; lines are joined with "\\n". The page
    script unpacks this with the browser's DecompressionStream and draws it.
    Nothing is cut or summarised: unpack_diff(pack_diff(x)) == x.
    """
    text = "\n".join(k + line for k, line in lines)
    data = gzip.compress(text.encode("utf-8"), compresslevel=6, mtime=0)
    return base64.b64encode(data).decode("ascii")


def unpack_diff(packed: str) -> List[Tuple[str, str]]:
    """Inverse of pack_diff (used by the tests and for checking a page)."""
    text = gzip.decompress(base64.b64decode(packed)).decode("utf-8")
    return [(l[0], l[1:]) for l in text.split("\n")] if text else []


def _render_diff(lines: Sequence[Tuple[str, str]]) -> str:
    # Each line is its own block-level span, so no newline characters between them
    # (a newline inside <pre> would add an empty line).
    return "".join(f'<span class="d{k}">{esc(line)}</span>' for k, line in lines)


def _render_entry(w, r: RepoReport, c: Commit, ch: Optional[FileChange]):
    status = ch.status if ch else "none"
    w(f'<li class="entry" data-status="{status}">')
    if ch:
        label = ch.status + ch.score
        w(f'<span class="badge b-{ch.status}" title="{esc(STATUS_NAMES.get(ch.status, ""))}">'
          f"{esc(label)}</span>")
        w(f'<span class="status-name">{esc(STATUS_NAMES.get(ch.status, ""))}</span>')
        if ch.old_path is not None:
            w(f'<div class="rename"><span class="old-path">{esc(ch.old_path)}</span> → '
              f'<span class="new-path">{esc(ch.path)}</span></div>')
    w(f'<pre class="msg">{esc(c.message)}</pre>')
    w('<div class="meta">')
    w(f'<span class="date" title="Author date">{esc(c.author_date)}</span>')
    url = r.links.commit_url(c.hash, ch.path if ch else None)
    if url:
        w(f'<a class="hash" href="{esc(url)}" {EXT}>{esc(c.hash)} ↗</a>')
    else:
        w(f'<span class="hash">{esc(c.hash)}</span>')
    w(f'<button type="button" class="copy" data-copy="{esc(c.hash)}" '
      'title="Copy commit id">Copy</button>')
    email = f' <span class="ae">&lt;{esc(c.author_email)}&gt;</span>' if c.author_email else ""
    w(f'<span class="user"><span class="an">{esc(c.author_name)}</span>{email}</span>')
    w("</div></li>\n")


CSS = r"""
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--panel:#f6f8fa;--accent:#0969da;
--err-bg:#ffebe9;--err-line:#cf222e;--quiet:#818b98;--tb-h:0px;--rh-h:0px;
--A:#1a7f37;--M:#0969da;--D:#cf222e;--R:#8250df;--C:#1b7c83;--T:#9a6700;--U:#6e7781;--X:#6e7781;--B:#6e7781}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;color:var(--fg);background:var(--bg)}
code,pre,.hash,.path,.old-path,.new-path{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}
.page-head{padding:16px 20px;border-bottom:1px solid var(--line)}
h1{font-size:20px;margin:0 0 10px}
.facts{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px;margin:0 0 12px}
.facts dt{color:var(--muted)}.facts dd{margin:0;word-break:break-all}
.overview{border-collapse:collapse;width:100%;max-width:1100px}
.overview th,.overview td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
.overview th{background:var(--panel)}
.overview tr.ov-error td{background:var(--err-bg)}
.overview tr.ov-no_change td{color:var(--quiet)}
.pill{display:inline-block;padding:0 8px;border-radius:10px;font-size:12px;font-weight:600;border:1px solid}
.pill-error{color:#fff;background:var(--err-line);border-color:var(--err-line)}
.pill-changes{color:var(--accent);border-color:var(--accent)}
.pill-quiet{color:var(--quiet);border-color:var(--line)}
.toolbar{position:sticky;top:0;z-index:30;background:var(--panel);border-bottom:1px solid var(--line);padding:8px 20px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.toolbar input[type=search]{flex:1 1 320px;min-width:200px;padding:5px 8px;border:1px solid var(--line);border-radius:6px;font:inherit}
.status-filter{border:0;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center}
.status-filter legend{float:left;margin-right:6px;color:var(--muted);padding:0}
.status-filter label{white-space:nowrap;cursor:pointer}
.toolbar button{font:inherit;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:#fff;cursor:pointer}
.filter-state{color:var(--muted);font-size:12px}
.diff-search{display:flex;flex:1 1 300px;gap:4px;align-items:center;min-width:220px}
.toolbar .diff-search input[type=search]{flex:1 1 auto;min-width:140px}
.diff-search button{padding:4px 8px}
.qd-state{color:var(--muted);font-size:12px;white-space:nowrap}
pre.diff[data-z]{position:relative}
pre.diff[data-z]:not(.drawn){height:min(600px,calc(var(--n) * 16.8px + 12px));color:var(--muted);overflow:hidden}
pre.diff[data-z]:not(.drawn)::before{content:"Loading " attr(data-n) " lines\2026";display:block;padding:0 10px}
pre.diff.unpack-error{white-space:pre-wrap;color:#82071e;height:auto;padding:6px 10px}
pre.diff span.hit{background:#fff8c5;box-shadow:inset 3px 0 0 #bf8700}
pre.diff span.hit-cur{background:#ffd33d;box-shadow:inset 3px 0 0 #9a6700}
.pagelink{margin:0 0 10px}
main{padding:12px 20px 40px}
.group{margin-bottom:28px}
.group-title{font-size:16px;margin:8px 0;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.group-error .group-title{color:var(--err-line)}
details.repo{border:1px solid var(--line);border-radius:8px;margin:0 0 14px;background:#fff}
details.repo-error{border:2px solid var(--err-line);background:var(--err-bg)}
details.repo-no_change{border-style:dashed;background:#fbfbfc;color:#424a53}
summary{cursor:pointer;list-style:revert}
.repo-head{position:sticky;top:var(--tb-h);z-index:20;background:inherit;background-color:#fff;padding:8px 12px;font-size:16px;font-weight:600;border-bottom:1px solid var(--line);border-radius:8px 8px 0 0}
details.repo-error>.repo-head{background-color:var(--err-bg)}
details.repo-no_change>.repo-head{background-color:#fbfbfc;font-weight:500;color:var(--quiet)}
.repo-name{font-size:17px}
.pair{font-weight:400;font-size:13px;margin-left:6px}
.anchor{margin-left:8px;color:var(--quiet);text-decoration:none;font-weight:400}
.anchor:hover{color:var(--accent)}
.repo-body{padding:8px 12px 12px}
.reason{white-space:pre-wrap;margin:4px 0 8px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
.reason-error{color:#82071e;font-weight:600}
.reason-quiet{color:var(--muted)}
.branches{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:0 0 8px;font-size:13px}
.branches dt{color:var(--muted)}.branches dd{margin:0}
.cands{margin:0;padding-left:18px}
.tag{font-size:11px;border:1px solid var(--line);border-radius:4px;padding:0 4px;color:var(--muted)}
.tag-same,.same-version{background:#fff8c5}
.tag-fallback{background:#fff1e5;border-color:#bc4c00;color:#953800;font-weight:600}
.fallback-note{background:#fff1e5;border:1px solid #bc4c00;border-radius:4px;padding:2px 6px}
.same-version{border:1px solid #d4a72c;border-radius:4px;padding:2px 6px}
.note{background:#fff8c5;border:1px solid #d4a72c;border-radius:6px;padding:6px 10px}
.note-small{color:var(--muted);font-size:12px;margin:2px 0 8px}
.files{border:1px solid var(--line);border-radius:6px;margin:8px 0;background:#fff}
.files-head,.file{display:grid;grid-template-columns:minmax(180px,20%) minmax(0,1.35fr) minmax(0,1fr)}
.files-head{background:var(--panel);font-size:12px;font-weight:600;color:var(--muted);border-bottom:1px solid var(--line)}
.files-head>div{padding:4px 10px}
.files-head>div:first-child,.files-head>.diff-label{border-right:1px solid var(--line)}
.file{border-top:1px solid var(--line)}
.files-head+.file{border-top:0}
.file-col{background:var(--panel);border-right:1px solid var(--line);padding:8px 10px}
.file-col-inner{position:sticky;top:calc(var(--tb-h) + var(--rh-h) + 6px)}
.file-col .path{font-weight:600;word-break:break-all;font-size:13px}
.nofiles .file-col .path{font-family:inherit;font-style:italic;font-weight:500}
.file-links{margin-top:2px;font-size:12px}
.file-links .anchor{margin-left:8px}
.file-links .filelink{margin-left:10px}
.diff-col{min-width:0;border-right:1px solid var(--line)}
.detail-col{min-width:0}
.detail-col>summary.detail-head,.diff-col>summary.diff-head{padding:3px 10px;font-size:12px;color:var(--muted);border-bottom:1px solid #eef1f4}
.detail-col:not([open])>summary.detail-head,.diff-col:not([open])>summary.diff-head{border-bottom:0}
pre.diff{margin:0;padding:6px 0;font-size:12px;line-height:1.4;max-height:600px;overflow:auto;background:#fff}
pre.diff span{display:block;padding:0 10px;white-space:pre;min-width:max-content}
pre.diff .da{background:#e6ffec}pre.diff .dd{background:#ffebe9}
pre.diff .dh{background:#ddf4ff;color:#0550ae}pre.diff .dm{color:var(--muted);font-style:italic}
.diff-error{color:#82071e;white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;margin:6px 10px}
.diff-empty{color:var(--muted);font-size:12px;margin:6px 10px}
body.diff-hidden .diff-col,body.diff-hidden .diff-label{display:none}
body.details-hidden .detail-col,body.details-hidden .detail-label{display:none}
body.diff-hidden .files-head,body.diff-hidden .file{grid-template-columns:minmax(220px,34%) minmax(0,1fr)}
body.details-hidden .files-head,body.details-hidden .file{grid-template-columns:minmax(180px,22%) minmax(0,1fr)}
body.details-hidden .diff-col,body.details-hidden .files-head>.diff-label{border-right:0}
body.diff-hidden.details-hidden .files-head,body.diff-hidden.details-hidden .file{grid-template-columns:1fr}
body.diff-hidden.details-hidden .file-col,body.diff-hidden.details-hidden .files-head>div:first-child{border-right:0}
body.diff-hidden.details-hidden .file-col{padding:4px 10px}
body.diff-hidden.details-hidden .file-col-inner{display:flex;flex-wrap:wrap;gap:2px 12px;align-items:baseline}
body.diff-hidden.details-hidden .file-links{margin-top:0}
@media (max-width:900px){.files-head,.file,body.diff-hidden .file,body.details-hidden .file{grid-template-columns:1fr}.file-col,.diff-col,.files-head>div{border-right:0!important}.files-head .detail-label,.files-head .diff-label{display:none}}
.entries{list-style:none;margin:0;padding:0}
.entry{padding:8px 10px;border-top:1px solid #eef1f4}
.entry:first-child{border-top:0}
.badge{display:inline-block;min-width:22px;text-align:center;padding:0 6px;border-radius:4px;color:#fff;font-weight:700;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.b-A{background:var(--A)}.b-M{background:var(--M)}.b-D{background:var(--D)}.b-R{background:var(--R)}
.b-C{background:var(--C)}.b-T{background:var(--T)}.b-U,.b-X,.b-B{background:var(--U)}
.status-name{margin-left:6px;color:var(--muted);font-size:12px}
.rename{margin:4px 0;font-size:13px;word-break:break-all}
.msg{white-space:pre-wrap;word-wrap:break-word;margin:6px 0;font-size:13px;background:none}
.meta{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center;font-size:12px;color:var(--muted)}
.hash{color:var(--accent);word-break:break-all}
span.hash{color:var(--fg)}
.copy{font:inherit;font-size:11px;padding:0 6px;border:1px solid var(--line);border-radius:4px;background:#fff;cursor:pointer}
.user .an{color:var(--fg);font-weight:600}
.filtered{display:none!important}
.file{content-visibility:auto;contain-intrinsic-size:auto 300px}
:target{scroll-margin-top:calc(var(--tb-h) + var(--rh-h) + 40px)}
@media print{
 .toolbar,.anchor,.copy{display:none!important}
 .repo-head,.file-col-inner{position:static}
 body.details-hidden .detail-col,body.diff-hidden .diff-col{display:block!important}
 body.diff-hidden .diff-label,body.details-hidden .detail-label{display:block!important}
 .files-head,.file,body .file,body .files-head{grid-template-columns:minmax(120px,18%) minmax(0,1.35fr) minmax(0,1fr)!important}
 .detail-col>summary.detail-head,.diff-col>summary.diff-head{display:none}
 pre.diff{max-height:none;overflow:visible}
 pre.diff[data-z]:not(.drawn){height:auto}
 pre.diff[data-z]:not(.drawn)::before{content:"This code diff was not drawn yet. Click \"Draw all diffs\" on the page, wait for it to finish, then print again."}
 pre.diff span{white-space:pre-wrap;min-width:0;word-break:break-all}
 .filtered{display:revert!important}
 .file{content-visibility:visible}
 .no-match{display:none!important}
 details.repo,.file{break-inside:auto}
 a{color:inherit}
 body{font-size:11px}
}
"""

JS = r"""
(function(){
  var root=document.documentElement;
  function q(s,el){return Array.prototype.slice.call((el||document).querySelectorAll(s));}
  function setHeights(){
    var tb=document.getElementById('toolbar');
    root.style.setProperty('--tb-h',(tb?tb.offsetHeight:0)+'px');
    var rh=document.querySelector('.repo-head');
    root.style.setProperty('--rh-h',(rh?rh.offsetHeight:0)+'px');
  }
  window.addEventListener('resize',setHeights); setHeights();

  // Search index: file path(s), message, commit id, user name and email.
  var entries=q('li.entry').map(function(li){
    var file=li.closest('.file');
    var parts=[file?file.getAttribute('data-path'):''];
    ['.old-path','.msg','.hash','.an','.ae'].forEach(function(s){var e=li.querySelector(s); if(e) parts.push(e.textContent);});
    return {el:li,file:file,status:li.getAttribute('data-status'),text:parts.join('\n').toLowerCase()};
  });
  var search=document.getElementById('q');
  var boxes=q('input.sf');
  var stateEl=document.getElementById('filter-state');

  function apply(){
    var term=(search.value||'').trim().toLowerCase();
    var allowed={}; boxes.forEach(function(b){allowed[b.value]=b.checked;});
    var allOn=boxes.every(function(b){return b.checked;});
    var active=!!term||!allOn;
    entries.forEach(function(e){
      var ok=(allowed[e.status]!==false)&&(!term||e.text.indexOf(term)!==-1);
      e.el.classList.toggle('filtered',!ok);
    });
    q('.file').forEach(function(f){
      var any=f.querySelector('li.entry:not(.filtered)');
      f.classList.toggle('filtered',!any);
    });
    q('details.repo-changes').forEach(function(r){
      var any=r.querySelector('.file:not(.filtered)');
      var nm=r.querySelector('.no-match'); if(nm) nm.hidden=!!any;
    });
    stateEl.textContent=active?'Filter active: some entries are hidden. Clear filters to show everything.':'';
  }
  search.addEventListener('input',apply);
  boxes.forEach(function(b){b.addEventListener('change',apply);});
  document.getElementById('clear-filters').addEventListener('click',function(){
    search.value=''; boxes.forEach(function(b){b.checked=true;}); apply();
  });
  function setAll(open){q('details.repo,details.detail-col,details.diff-col').forEach(function(d){d.open=open;});}
  function columnToggle(id,cls,name){
    var b=document.getElementById(id);
    b.addEventListener('click',function(){
      var hidden=document.body.classList.toggle(cls);
      b.textContent=(hidden?'Show ':'Hide ')+name+' column';
      b.setAttribute('aria-pressed',hidden?'true':'false');
      setHeights();
    });
  }
  columnToggle('toggle-diff','diff-hidden','code diff');
  columnToggle('toggle-details','details-hidden','changes');
  document.getElementById('expand-all').addEventListener('click',function(){setAll(true);});
  document.getElementById('collapse-all').addEventListener('click',function(){setAll(false);});

  // Open the sections that contain a linked anchor.
  function openTarget(){
    var id=location.hash.slice(1); try{id=decodeURIComponent(id);}catch(e){} if(!id) return;
    var el=document.getElementById(id); if(!el) return;
    for(var d=el; d; d=d.parentElement){ if(d.tagName==='DETAILS') d.open=true; }
    el.scrollIntoView();
  }
  window.addEventListener('hashchange',openTarget); openTarget();

  // ---- Code diffs: stored gzip-compressed + base64 in data-z, drawn on demand ----
  // Each unpacked line is a kind letter (h/a/d/c/m) followed by the line exactly as
  // git printed it. A diff is drawn when its row comes near the screen, when a
  // search match in it is shown, or by "Draw all diffs". Nothing is cut.
  var pres=q('pre.diff[data-z]');
  var canUnpack=typeof DecompressionStream!=='undefined'&&typeof Blob!=='undefined'&&typeof Response!=='undefined';
  var KIND={h:'dh',a:'da',d:'dd',c:'dc',m:'dm'};
  function b64bytes(b64){var bin=atob(b64),n=bin.length,u=new Uint8Array(n);for(var i=0;i<n;i++)u[i]=bin.charCodeAt(i);return u;}
  function unpack(pre){
    if(!canUnpack) return Promise.reject(new Error('this browser has no DecompressionStream (needs Chrome/Edge 80+, Firefox 113+ or Safari 16.4+)'));
    var s=new Blob([b64bytes(pre.getAttribute('data-z'))]).stream().pipeThrough(new DecompressionStream('gzip'));
    return new Response(s).text().then(function(t){return t?t.split('\n'):[];});
  }
  function escH(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  var io=null;
  function draw(pre){
    if(pre._drawn) return pre._drawn;
    pre._drawn=unpack(pre).then(function(lines){
      var h=new Array(lines.length);
      for(var i=0;i<lines.length;i++){var l=lines[i];h[i]='<span class="'+(KIND[l.charAt(0)]||'dc')+'">'+escH(l.slice(1))+'</span>';}
      pre.innerHTML=h.join('');
      pre.classList.add('drawn');
      markPre(pre);
    },function(err){
      pre.classList.add('drawn','unpack-error');
      pre.textContent='This code diff could not be unpacked: '+(err&&err.message||err);
    }).then(function(){if(io) io.unobserve(pre);});
    return pre._drawn;
  }
  if('IntersectionObserver' in window){
    io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting) draw(e.target);});},{rootMargin:'1500px 0px'});
    pres.forEach(function(p){io.observe(p);});
  }
  var drawBtn=document.getElementById('draw-all'), drawing=null;
  function drawAll(){
    if(drawing) return drawing;
    var todo=pres.filter(function(p){return !p.classList.contains('drawn');}), i=0;
    drawBtn.disabled=true;
    drawing=new Promise(function(done){
      (function step(){
        var batch=[], lines=0;
        while(i<todo.length&&(batch.length===0||lines<20000)){lines+=+todo[i].getAttribute('data-n'); batch.push(draw(todo[i++]));}
        drawBtn.textContent='Drawing diffs '+i+' / '+todo.length+'…';
        Promise.all(batch).then(function(){
          if(i<todo.length) setTimeout(step,0);
          else{drawBtn.textContent='All diffs drawn'; done();}
        });
      })();
    });
    return drawing;
  }
  drawBtn.addEventListener('click',drawAll);
  if(!io) drawAll();
  if(!pres.length){drawBtn.disabled=true; drawBtn.textContent='All diffs drawn';}

  // ---- Search inside the code diffs ----
  var qd=document.getElementById('qd'), qdState=document.getElementById('qd-state');
  var low=new Map();          // pre -> lower-cased lines (without the kind letter)
  var hits=[], hitsBy=new Map(), cur=-1, term='', runId=0, timer=null;
  function linesOf(pre){
    if(low.has(pre)) return Promise.resolve(low.get(pre));
    return unpack(pre).then(function(lines){
      var l=lines.map(function(x){return x.slice(1).toLowerCase();}); low.set(pre,l); return l;
    });
  }
  function markPre(pre){
    if(!pre.classList.contains('drawn')) return;
    q('span.hit',pre).forEach(function(s){s.classList.remove('hit','hit-cur');});
    (hitsBy.get(pre)||[]).forEach(function(k){var s=pre.children[k]; if(s) s.classList.add('hit');});
  }
  function runSearch(){
    var t=(qd.value||'').toLowerCase(), id=++runId;
    var old=Array.from(hitsBy.keys());
    hits=[]; hitsBy=new Map(); cur=-1; term=t;
    old.forEach(markPre);
    if(!t){qdState.textContent=''; return Promise.resolve();}
    if(!canUnpack){qdState.textContent='This browser cannot unpack the diffs.'; return Promise.resolve();}
    var i=0;
    return new Promise(function(done){
      (function step(){
        if(id!==runId) return done();
        var batch=pres.slice(i,i+50); i+=batch.length;
        qdState.textContent='Searching '+i+' / '+pres.length+' files…';
        Promise.all(batch.map(function(p){return linesOf(p).catch(function(){return [];});})).then(function(all){
          if(id!==runId) return done();
          all.forEach(function(lines,b){
            var pre=batch[b], ks=[];
            for(var k=0;k<lines.length;k++){ if(lines[k].indexOf(t)!==-1){ks.push(k); hits.push({pre:pre,k:k});} }
            if(ks.length){hitsBy.set(pre,ks); markPre(pre);}
          });
          if(i<pres.length) return setTimeout(step,0);
          var files=hitsBy.size;
          qdState.textContent=hits.length?(hits.length+' matching line'+(hits.length===1?'':'s')+' in '+files+' file'+(files===1?'':'s')+'. Press Enter for the next one.'):'No code diff line matches.';
          done();
        });
      })();
    });
  }
  function go(step){
    if(!hits.length) return;
    cur=(cur+step+hits.length)%hits.length;
    var h=hits[cur], pre=h.pre, n=cur;
    if(document.body.classList.contains('diff-hidden')) document.getElementById('toggle-diff').click();
    for(var d=pre; d; d=d.parentElement){ if(d.tagName==='DETAILS') d.open=true; }
    var hidden=!!pre.closest('.filtered');
    draw(pre).then(function(){
      if(n!==cur) return;
      q('span.hit-cur').forEach(function(s){s.classList.remove('hit-cur');});
      var s=pre.children[h.k]; if(!s) return;
      s.classList.add('hit-cur');
      pre.scrollTop=Math.max(0,s.offsetTop-pre.clientHeight/3);
      var file=pre.closest('.file');
      var top=s.getBoundingClientRect().top-(document.getElementById('toolbar').offsetHeight+120);
      window.scrollBy(0,top);
      var where=file?file.getAttribute('data-path'):'';
      qdState.textContent='Match '+(cur+1)+' of '+hits.length+(where?' — '+where:'')+(hidden?' (this file is hidden by the search/status filter above: clear filters to see it)':'');
    });
  }
  qd.addEventListener('input',function(){clearTimeout(timer); timer=setTimeout(runSearch,300);});
  qd.addEventListener('keydown',function(e){
    if(e.key!=='Enter') return; e.preventDefault();
    clearTimeout(timer);
    var back=e.shiftKey;
    (term===(qd.value||'').toLowerCase()?Promise.resolve():runSearch()).then(function(){go(back?-1:1);});
  });
  document.getElementById('qd-next').addEventListener('click',function(){(term===(qd.value||'').toLowerCase()?Promise.resolve():runSearch()).then(function(){go(1);});});
  document.getElementById('qd-prev').addEventListener('click',function(){(term===(qd.value||'').toLowerCase()?Promise.resolve():runSearch()).then(function(){go(-1);});});

  // Printing: expand every section (filters are ignored by the print stylesheet)
  // and start drawing every diff (a diff not drawn yet prints a note saying so).
  window.addEventListener('beforeprint',function(){setAll(true); drawAll();});

  // Copy commit id.
  document.addEventListener('click',function(ev){
    var b=ev.target.closest&&ev.target.closest('button.copy'); if(!b) return;
    ev.preventDefault();
    var text=b.getAttribute('data-copy');
    function done(){var t=b.textContent; b.textContent='Copied'; setTimeout(function(){b.textContent=t;},1200);}
    if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(text).then(done,fallback);}
    else fallback();
    function fallback(){
      var ta=document.createElement('textarea'); ta.value=text; ta.setAttribute('readonly','');
      ta.style.position='fixed'; ta.style.opacity='0'; document.body.appendChild(ta); ta.select();
      try{document.execCommand('copy'); done();}catch(e){} document.body.removeChild(ta);
    }
  });
})();
"""

# --------------------------------------------------------------------------
# Statistics page (STATS_OUTPUT_FILE)
# --------------------------------------------------------------------------
#
# A separate page with, per repository, every file changed in base..target and:
#   - how many commits changed the file (commits listed under it on the review page)
#   - how many different users (author names, %an) made those commits
#   - a collapsible list of those commits' messages
# Files are grouped by a type read from the file name (see file_type below).
# Merge commits with no file changes are not counted, as they change no file.

FILE_TYPES = ["Controller", "Processor", "Route", "Config", "API", "Builder", "Mapper",
              "VO", "Request", "Response", "Test", "Properties", "Util", "Constant",
              "Resources", "Template"]
OTHER_TYPE = "Other"
# words in a file name that count as each type (compared case-insensitively)
_TYPE_WORDS = {
    "Controller": ("controller", "controllers"),
    "Processor": ("processor", "processors"),
    "Route": ("route", "routes", "router", "routing"),
    "Config": ("config", "configs", "configuration", "configurations"),
    "API": ("api", "apis"),
    "Builder": ("builder", "builders"),
    "Mapper": ("mapper", "mappers"),
    "VO": ("vo", "vos"),
    "Request": ("request", "requests"),
    "Response": ("response", "responses"),
    "Test": ("test", "tests", "spec", "specs"),   # decided separately, see file_type
    "Properties": ("properties", "property", "props"),
    "Util": ("util", "utils", "utility", "utilities"),
    "Constant": ("constant", "constants", "const", "consts"),
}
# Folder groups: files whose name matches no type above, but that sit in one of
# these folders (at any depth), go to the folder's group instead of Other.
_FOLDER_TYPES = {
    "resources": "Resources", "resource": "Resources",
    "routes": "Route", "route": "Route",
    "templates": "Template", "template": "Template",
}
_WORD_TO_TYPE = {word: t for t, words in _TYPE_WORDS.items() for word in words}


def _name_words(file_name: str) -> List[str]:
    """Words of a file name: split on . _ - and spaces, and at camelCase boundaries,
    keeping acronyms together ("UserVOMapper" -> User, VO, Mapper)."""
    words = []
    for part in re.split(r"[._\-\s]+", file_name):
        words += re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", part)
    return words


def file_type(path: str) -> str:
    """Type of a file from its name only (not its folder or content).

    The file name without its extension(s) is split into words.
    Test: the name starts with "test"/"tests" or ends with "Test", "Tests", "Spec" or
    "IT" ("UserControllerTest.java", "test_api.py", "user.spec.ts", "UserServiceIT.java").
    Otherwise the last word that names a type decides: "UserController.java" ->
    Controller, "OrderRequestMapper.java" -> Mapper, "application-config.yml" ->
    Config, "UserVO.java" -> VO, "DateUtils.java" -> Util, "ErrorConstants.java" ->
    Constant. A ".properties" file is Properties.
    If the name matches nothing, the folder decides: a file anywhere under a
    resources/, routes/ or template(s)/ folder goes to Resources, Route or Template
    (the innermost such folder wins). Otherwise -> Other.
    """
    name = path.rsplit("/", 1)[-1]
    stem = name.split(".", 1)[0] if not name.startswith(".") else name.lstrip(".").split(".", 1)[0]
    words = _name_words(stem)
    if "." in name.lstrip("."):      # e.g. "user.spec.ts": the middle parts count too
        words += _name_words(".".join(name.lstrip(".").split(".")[1:-1]))
    if words and (words[0].lower() in ("test", "tests")               # test_api.py, TestUser.java
                  or words[-1].lower() in ("test", "tests", "spec", "specs")
                  or words[-1] == "IT"):                                # UserServiceIT.java
        return "Test"
    found = None
    for word in words:
        t = _WORD_TO_TYPE.get(word.lower())
        if t and t != "Test":
            found = t
    if found:
        return found
    if name.lower().endswith(".properties"):
        return "Properties"
    for folder in reversed(path.split("/")[:-1]):
        t = _FOLDER_TYPES.get(folder.lower())
        if t:
            return t
    return OTHER_TYPE


@dataclasses.dataclass
class FileStats:
    path: str
    type: str
    commits: List[Commit]          # in git log order, newest first
    users: List[str]               # distinct author names, in order of first appearance
    statuses: List[str]            # status of each commit for this file, same order


def file_stats(r: RepoReport) -> List[FileStats]:
    out = []
    for path, entries in r.files:
        users: List[str] = []
        for c, _ in entries:
            if c.author_name not in users:
                users.append(c.author_name)
        out.append(FileStats(path, file_type(path), [c for c, _ in entries], users,
                             [ch.status + ch.score for _, ch in entries]))
    return out


def _rel_href(from_file: str, to_file: str) -> str:
    rel = os.path.relpath(os.path.abspath(to_file), os.path.dirname(os.path.abspath(from_file)))
    return urllib.parse.quote(rel.replace(os.sep, "/"), safe="/")


def render_stats_html(cfg: Config, generated: str, reports: Sequence[RepoReport]) -> str:
    review_href = _rel_href(cfg.stats_output_file, cfg.output_file)
    out: List[str] = []
    w = out.append
    w("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n")
    w('<meta name="viewport" content="width=device-width, initial-scale=1">\n')
    w(f"<title>Release statistics: {esc(cfg.target.name)}</title>\n<style>{STATS_CSS}</style>\n"
      "</head>\n<body>\n<header>\n")
    w(f"<h1>Release statistics: <code>{esc(cfg.target.name)}</code></h1>\n")
    w(f'<p class="pagelink"><a href="{esc(review_href)}">← Review page</a></p>\n')
    w('<dl class="facts">\n')
    for k, v in (("Generated", generated), ("REPOS_FOLDER", cfg.repos_folder),
                 ("TARGET_BRANCH", cfg.target.name), ("Country", cfg.target.country),
                 ("Version", cfg.target.version)):
        w(f"<dt>{esc(k)}</dt><dd><code>{esc(v)}</code></dd>\n")
    w("</dl>\n")
    w('<p class="explain">For every file changed between each repository\'s base and target '
      "branch: <b>Commits</b> = number of commits that changed the file, <b>Users</b> = number "
      "of different commit authors. Files are grouped by a type taken from the file name "
      "(e.g. <code>UserController.java</code> → Controller, <code>UserControllerTest.java</code> "
      "→ Test, <code>DateUtils.java</code> → Util); a file whose name matches no type but "
      "sits in a <code>resources/</code>, <code>routes/</code> or <code>template(s)/</code> "
      "folder goes to Resources, Route or Template. The <b>Summary</b> tab lists the files changed by more than one user; the <b>Details</b> tab lists every file by type, with its commits. Repositories with no change are not shown.</p>\n")
    w("</header>\n<main>\n")

    changed = [r for r in reports if r.state == STATE_CHANGES]
    # Repositories with no change are left off this page; errors are still listed.
    errors = [r for r in reports if r.state == STATE_ERROR]
    if errors:
        w('<section class="others"><h2>Repositories with errors</h2><ul>\n')
        for r in errors:
            w(f'<li><b>{esc(r.name)}</b> <span class="pill pill-{r.state}">Error</span> '
              f'<span class="reason">{esc(r.reason)}</span></li>\n')
        w("</ul></section>\n")
    if not changed:
        w("<p>No repository has commits to summarise.</p>\n")

    if changed:
        w('<div class="tabs" role="tablist">'
          '<button type="button" role="tab" id="tab-btn-summary" aria-controls="tab-summary" '
          'aria-selected="true">Summary</button>'
          '<button type="button" role="tab" id="tab-btn-details" aria-controls="tab-details" '
          'aria-selected="false">Details</button></div>\n')
        w('<div class="tab-panel" id="tab-summary" role="tabpanel" aria-labelledby="tab-btn-summary">\n')
    # Summary tab: per repository, the files changed by more than one user
    if changed:
        w('<section class="summary"><h2>Files modified by more than one user</h2>\n'
          '<p class="muted">Test files are left out of this summary. They are still listed '
          'in the Test group on the Details tab.</p>\n')
        shown = 0
        for r in changed:
            multi = sorted((fs for fs in file_stats(r) if len(fs.users) > 1 and fs.type != "Test"),
                           key=lambda fs: (-len(fs.users), _sort_key(fs.path)))
            if not multi:
                continue  # repositories with no such file are left out of the summary
            shown += 1
            w(f'<h3><a href="#{esc(anchor_id("stats", r.name))}">{esc(r.name)}</a> '
              f'<span class="muted">({len(multi)} file{"s" if len(multi) != 1 else ""})</span></h3>\n')
            w('<table class="stats summary-table sortable"><thead><tr>'
              '<th aria-sort="none"><button type="button" class="sort" data-kind="text">File</button></th>'
              '<th aria-sort="none"><button type="button" class="sort" data-kind="text">Type</button></th>'
              '<th class="num" aria-sort="descending"><button type="button" class="sort" data-kind="num">Users</button></th>'
              '<th class="num" aria-sort="none"><button type="button" class="sort" data-kind="num">Commits</button></th>'
              '<th aria-sort="none"><button type="button" class="sort" data-kind="text">User names</button></th>'
              '</tr></thead><tbody>\n')
            for fs in multi:
                sid = anchor_id("stats-file", r.name, fs.path)
                w(f'<tr><td class="path"><a href="#{esc(sid)}">{esc(fs.path)}</a></td>'
                  f'<td>{esc(fs.type)}</td><td class="num">{len(fs.users)}</td>'
                  f'<td class="num">{len(fs.commits)}</td><td>{esc(", ".join(fs.users))}</td></tr>\n')
            w("</tbody></table>\n")
        if not shown:
            w('<p class="muted">No repository has a file modified by more than one user.</p>\n')
        w("</section>\n</div>\n")
        w('<div class="tab-panel" id="tab-details" role="tabpanel" aria-labelledby="tab-btn-details" hidden>\n')
        w('<div class="tools"><button type="button" id="open-all">Expand all commit lists</button>'
          '<button type="button" id="close-all">Collapse all commit lists</button></div>\n')

    for r in changed:
        rid = anchor_id("stats", r.name)
        stats = file_stats(r)
        w(f'<details class="repo" id="{esc(rid)}" open><summary><span class="repo-name">'
          f"{esc(r.name)}</span> <code>{esc(r.base_branch)}</code> → <code>{esc(r.target_branch)}</code>"
          f'<a class="anchor" href="#{esc(rid)}">#</a></summary>\n')
        w('<div class="repo-body">\n')
        by_type: Dict[str, List[FileStats]] = {}
        for fs in stats:
            by_type.setdefault(fs.type, []).append(fs)
        for t in FILE_TYPES + [OTHER_TYPE]:
            group = by_type.get(t)
            if not group:
                continue
            w(f'<h3 class="type-head">{esc(t)} <span class="muted">({len(group)} '
              f'file{"s" if len(group) != 1 else ""})</span></h3>\n')
            w('<table class="stats"><thead><tr><th>File</th><th class="num">Commits</th>'
              '<th class="num">Users</th><th>Commit messages</th></tr></thead><tbody>\n')
            for fs in group:
                fid = anchor_id("file", r.name, fs.path)
                sid = anchor_id("stats-file", r.name, fs.path)
                w(f'<tr id="{esc(sid)}"><td class="path"><a href="{esc(review_href)}#{esc(fid)}" '
                  f'title="Open this file on the review page">{esc(fs.path)}</a></td>'
                  f'<td class="num">{len(fs.commits)}</td>'
                  f'<td class="num" title="{esc(", ".join(fs.users))}">{len(fs.users)}</td><td>')
                w(f'<details class="msgs"><summary>{len(fs.commits)} commit'
                  f'{"s" if len(fs.commits) != 1 else ""} by {esc(", ".join(fs.users))}</summary>'
                  '<ol>')
                for c, st in zip(fs.commits, fs.statuses):
                    w(f'<li><pre class="msg">{esc(c.message)}</pre><div class="meta">'
                      f'<span class="st">{esc(st)}</span> <span>{esc(c.author_date)}</span> '
                      f'<code>{esc(c.hash)}</code> <b>{esc(c.author_name)}</b></div></li>')
                w("</ol></details></td></tr>\n")
            w("</tbody></table>\n")
        w("</div>\n</details>\n")
    if changed:
        w("</div>\n")

    w(f"</main>\n<script>{STATS_JS}</script>\n</body>\n</html>\n")
    return "".join(out)


STATS_CSS = r"""
:root{--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--panel:#f6f8fa;--accent:#0969da}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;color:var(--fg);background:#fff}
code,pre,.path{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}
header{padding:16px 20px;border-bottom:1px solid var(--line)}
h1{font-size:20px;margin:0 0 6px}
.pagelink{margin:0 0 10px}
.facts{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px;margin:0 0 10px}
.facts dt{color:var(--muted)}.facts dd{margin:0;word-break:break-all}
.explain{color:var(--muted);max-width:900px;margin:0 0 10px}
.tools button{font:inherit;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:#fff;cursor:pointer;margin-right:6px}
main{padding:12px 20px 40px}
.others ul{padding-left:18px}.reason{color:var(--muted);font-size:13px}
.summary{margin:0 0 24px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:#fbfbfc}
.summary h2{font-size:16px;margin:4px 0 8px}.summary h3{font-size:14px;margin:12px 0 4px}
.summary-table td.path{width:40%}
.summary-table th{white-space:nowrap}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:0 0 14px}
.tabs [role=tab]{font:inherit;font-weight:600;padding:8px 16px;border:1px solid transparent;border-bottom:0;border-radius:6px 6px 0 0;background:none;cursor:pointer;color:var(--muted);margin-bottom:-1px}
.tabs [role=tab][aria-selected=true]{color:var(--fg);background:#fff;border-color:var(--line);border-bottom:1px solid #fff}
.tab-panel .tools{margin:0 0 12px}
button.sort{font:inherit;font-weight:600;border:0;background:none;padding:0;cursor:pointer;color:inherit}
button.sort::after{content:" \2195";color:#818b98}
th[aria-sort=ascending] button.sort::after{content:" \25B2";color:var(--fg)}
th[aria-sort=descending] button.sort::after{content:" \25BC";color:var(--fg)}
@media print{button.sort::after{content:""}}
tr:target{background:#fff8c5}
.pill{display:inline-block;padding:0 8px;border-radius:10px;font-size:12px;font-weight:600;border:1px solid}
.pill-error{color:#fff;background:#cf222e;border-color:#cf222e}.pill-no_change{color:#818b98;border-color:var(--line)}
details.repo{border:1px solid var(--line);border-radius:8px;margin:0 0 16px}
details.repo>summary{padding:8px 12px;font-size:15px;background:var(--panel);border-bottom:1px solid var(--line);cursor:pointer;position:sticky;top:0;z-index:5}
.repo-name{font-weight:700;font-size:17px;margin-right:6px}
.anchor{margin-left:8px;color:#818b98;text-decoration:none}
.repo-body{padding:4px 12px 12px}
.type-head{font-size:15px;margin:14px 0 6px}
.muted{color:var(--muted);font-weight:400;font-size:13px}
table.stats{border-collapse:collapse;width:100%}
table.stats th,table.stats td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
table.stats th{background:var(--panel);font-size:12px}
table.stats td.path{width:38%;word-break:break-all;font-size:13px}
.num{text-align:right!important;width:80px;font-variant-numeric:tabular-nums}
details.msgs>summary{cursor:pointer;color:var(--accent);font-size:13px}
details.msgs ol{margin:6px 0 0;padding-left:20px}
details.msgs li{margin:0 0 8px}
.msg{white-space:pre-wrap;word-wrap:break-word;margin:0;font-size:12px}
.meta{font-size:11px;color:var(--muted)}.meta .st{font-weight:700}
@media print{.tools,.pagelink,.anchor,.tabs{display:none}.tab-panel[hidden]{display:block}details.repo>summary{position:static}}
@media (max-width:700px){table.stats td.path{width:auto}}
"""

STATS_JS = r"""
(function(){
  var tabs={summary:document.getElementById('tab-summary'),details:document.getElementById('tab-details')};
  function show(name){
    if(!tabs.summary) return;
    Object.keys(tabs).forEach(function(k){
      tabs[k].hidden=(k!==name);
      document.getElementById('tab-btn-'+k).setAttribute('aria-selected',k===name?'true':'false');
    });
  }
  document.querySelectorAll('[role=tab]').forEach(function(b){
    b.addEventListener('click',function(){show(b.id.replace('tab-btn-',''));});
  });
  // A link or address to a row or repository on the Details tab opens that tab first.
  function follow(hash){
    if(!hash||hash.length<2) return;
    var el=document.getElementById(decodeURIComponent(hash.slice(1)));
    if(el&&tabs.details&&tabs.details.contains(el)){show('details');el.scrollIntoView();}
  }
  document.addEventListener('click',function(e){
    var a=e.target.closest&&e.target.closest('a[href^="#"]');
    if(a) follow(a.getAttribute('href'));
  });
  window.addEventListener('hashchange',function(){follow(location.hash);});
  follow(location.hash);
  function all(open){document.querySelectorAll('details.msgs').forEach(function(d){d.open=open;});}
  var oa=document.getElementById('open-all'), ca=document.getElementById('close-all');
  if(oa) oa.addEventListener('click',function(){all(true);});
  if(ca) ca.addEventListener('click',function(){all(false);});
  window.addEventListener('beforeprint',function(){all(true);});
  // Click a summary column header to sort by it; click again to reverse.
  document.querySelectorAll('table.sortable').forEach(function(t){
    var body=t.tBodies[0], heads=t.tHead.rows[0].cells;
    Array.prototype.forEach.call(body.rows,function(r,i){r.dataset.i=i;});
    Array.prototype.forEach.call(heads,function(th,col){
      var b=th.querySelector('button.sort'); if(!b) return;
      b.addEventListener('click',function(){
        var num=b.dataset.kind==='num';
        var dir=th.getAttribute('aria-sort')==='ascending'?'descending':
                th.getAttribute('aria-sort')==='descending'?'ascending':(num?'descending':'ascending');
        Array.prototype.forEach.call(heads,function(h){h.setAttribute('aria-sort','none');});
        th.setAttribute('aria-sort',dir);
        var sign=dir==='ascending'?1:-1;
        var rows=Array.prototype.slice.call(body.rows);
        rows.sort(function(a,c){
          var x=a.cells[col].textContent, y=c.cells[col].textContent, d;
          if(num){d=(parseFloat(x)||0)-(parseFloat(y)||0);}
          else{d=x.localeCompare(y,undefined,{numeric:true,sensitivity:'base'});}
          return d*sign||(a.dataset.i-c.dataset.i);
        });
        rows.forEach(function(r){body.appendChild(r);});
      });
    });
  });
})();
"""



def main() -> int:
    try:
        generate_report(REPOS_FOLDER, TARGET_BRANCH, REMOTE_NAME, OUTPUT_FILE, DIFF_URL_TEMPLATE,
                        BITBUCKET_BASE_URL, FALLBACK_BASE_BRANCH, STATS_OUTPUT_FILE)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
