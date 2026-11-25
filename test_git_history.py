"""Tests for git_history.py.  Run:  python -m unittest -v test_git_history

Every test builds real temporary repositories, each with a bare "remote", and
calls git_history.generate_report() with test values (the configuration block in
git_history.py is never edited).
"""

import contextlib
import html
import html.parser
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import urllib.parse
from unittest import mock

import git_history as gh

TARGET = "release/SG_19.18.0"


# --------------------------------------------------------------------------
# helpers to build repositories
# --------------------------------------------------------------------------

def git(cwd, *args, env=None, input_bytes=None):
    full_env = dict(os.environ)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        full_env.pop(k, None)
    full_env.update({"GIT_CONFIG_NOSYSTEM": "1", "HOME": cwd if os.path.isdir(cwd) else "/tmp"})
    if env:
        full_env.update(env)
    p = subprocess.run(["git", *args], cwd=cwd, env=full_env, input=input_bytes,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(f"git {args} failed: {p.stderr.decode(errors='replace')}")
    return p.stdout.decode("utf-8", errors="replace").strip()



# one row on the stats Details tab: file name (link to the git host, or plain text when
# there are no links), the Compare/File links, then the link to the local review page
STATS_ROW = (r'<tr id="stats-file/([^"]*)"><td class="path"><(?:a|span)[^>]*>([^<]*)</(?:a|span)>'
             r'(?:<div class="flinks">(?:[^<]|<a [^>]*>[^<]*</a>)*</div>)?'
             r'</td><td class="num">(\d+)</td><td class="num"[^>]*>(\d+)</td>')

def unpack_cell(cell):
    """Replace each compressed <pre class="diff" data-z=...> in an HTML fragment with
    the spans the page script draws from it, so tests can read the diff lines."""
    def repl(m):
        lines = gh.unpack_diff(html.unescape(m.group(1)))
        assert int(m.group(2)) == len(lines)
        return '<pre class="diff">' + gh._render_diff(lines) + "</pre>"
    return re.sub(r'<pre class="diff" data-z="([^"]*)" data-n="(\d+)" style="--n:\d+"></pre>', repl, cell)

class Repo:
    """A working clone plus its bare 'remote' (origin)."""

    def __init__(self, parent, name, remotes_dir):
        self.bare = os.path.join(remotes_dir, name + ".git")
        self.path = os.path.join(parent, name)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.bare], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", self.path], check=True)
        self.git("config", "user.name", "Setup User")
        self.git("config", "user.email", "setup@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("remote", "add", "origin", self.bare)
        self.tick = 0

    def git(self, *args, **kw):
        return git(self.path, *args, **kw)

    def write(self, rel, content):
        full = os.path.join(self.path.encode(), rel if isinstance(rel, bytes) else rel.encode())
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(content if isinstance(content, bytes) else content.encode())

    def commit(self, message, author=("Alice Lim", "alice@example.com"), date=None,
               message_bytes=None, allow_empty=False):
        self.tick += 1
        date = date or f"2024-03-{self.tick:02d}T10:{self.tick:02d}:00+08:00"
        env = {"GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
               "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
               "GIT_COMMITTER_NAME": "Committer", "GIT_COMMITTER_EMAIL": "c@example.com"}
        self.git("add", "-A")
        args = ["commit", "-q", "--no-verify", "-F", "-"]
        if allow_empty:
            args.append("--allow-empty")
        data = message_bytes if message_bytes is not None else message.encode()
        self.git(*args, env=env, input_bytes=data)
        return self.git("rev-parse", "HEAD")

    def raw_commit(self, message_bytes, author):
        """Write a commit object whose message is not valid UTF-8 (git commit would
        convert it), then move the current branch to it."""
        import datetime
        self.tick += 1
        when = datetime.datetime(2024, 3, self.tick, 10, self.tick, tzinfo=datetime.timezone(
            datetime.timedelta(hours=8)))
        stamp = f"{int(when.timestamp())} +0800".encode()
        self.git("add", "-A")
        tree = self.git("write-tree")
        parent = self.git("rev-parse", "HEAD")
        body = (b"tree " + tree.encode() + b"\nparent " + parent.encode() + b"\nauthor " +
                author[0].encode() + b" <" + author[1].encode() + b"> " + stamp +
                b"\ncommitter Committer <c@example.com> " + stamp + b"\n\n" + message_bytes)
        sha = self.git("hash-object", "-w", "-t", "commit", "--literally", "--stdin", input_bytes=body)
        self.git("update-ref", "HEAD", sha)
        return sha

    def push(self, branch, rev="HEAD"):
        self.git("push", "-q", "origin", f"{rev}:refs/heads/{branch}")


# --------------------------------------------------------------------------
# HTML extraction (to check content exactly)
# --------------------------------------------------------------------------

class EntryExtractor(html.parser.HTMLParser):
    """Collects every commit entry: (repo, file path, status label, msg, date, hash, name, email, old path)."""
    FIELDS = {"badge": "status", "msg": "msg", "date": "date", "hash": "hash",
              "an": "name", "ae": "email", "old-path": "old"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.entries = []
        self.repo = None
        self.path = None
        self.cur = None
        self.capture = None
        self.depth = 0
        self.stack = []
        self.links = []          # (class, href, target, rel)
        self.repo_ids = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "details" and "repo" in classes:
            self.repo = a["id"]
        if "file" in classes and "data-path" in a:
            self.path = a.get("data-path")
        if tag == "a" and a.get("href", "").startswith("http"):
            self.links.append((classes, a["href"], a.get("target"), a.get("rel")))
        if tag == "li" and "entry" in classes:
            self.cur = {"repo": self.repo, "path": self.path, "data_status": a.get("data-status")}
        if self.cur is not None and self.capture is None:
            for c in classes:
                if c in self.FIELDS:
                    self.capture = self.FIELDS[c]
                    self.cur[self.capture] = ""
                    self.stack = [tag]
                    return
        elif self.capture is not None:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.capture is not None:
            self.stack.pop()
            if not self.stack:
                self.capture = None
            return
        if tag == "li" and self.cur is not None:
            self.entries.append(self.cur)
            self.cur = None

    def handle_data(self, data):
        if self.capture is not None:
            self.cur[self.capture] += data


def extract(page):
    p = EntryExtractor()
    p.feed(page)
    for e in p.entries:
        if "hash" in e:
            e["hash"] = e["hash"].replace("↗", "").strip()
    return p


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------

class Fixture:
    """Builds REPOS_FOLDER with several repositories covering every scenario."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="ghtest-")
        self.repos = os.path.join(self.tmp, "repos")
        self.remotes = os.path.join(self.tmp, "remotes")
        os.makedirs(self.repos)
        os.makedirs(self.remotes)
        self.expected = {}
        self.build_alpha()
        self.build_beta()
        self.build_gamma()
        self.build_delta()
        self.build_eta()
        self.build_theta()
        self.build_iota()
        self.build_epsilon()
        self.build_zeta()
        os.makedirs(os.path.join(self.repos, "notes"))           # not a git repo
        with open(os.path.join(self.repos, "README.txt"), "w") as fh:  # a plain file
            fh.write("not a repo")

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build_alpha(self):
        r = Repo(self.repos, "alpha", self.remotes)
        self.alpha = r
        r.write("app/config.yml", "v: 1\n")
        r.write("b.txt", "bee\n")
        r.write("a.txt", "a\n" * 20)
        c0 = r.commit("Initial import")
        r.push("release/SG_19.9.0")
        r.write("app/config.yml", "v: 2\n")
        c1 = r.commit("Release 19.17.0 config")
        r.push("release/SG_19.17.0")
        r.write("app/config.yml", "v: 3\n")
        c2 = r.commit("Hotfix 19.17.1: config fix")
        r.push("hotfix/SG_19.17.1")
        # a branch of another country with a commit not in the SG line
        r.git("checkout", "-q", "-b", "my-line")
        r.write("my.txt", "malaysia\n")
        r.commit("MY only commit")
        r.push("release/MY_19.18.0")
        r.git("checkout", "-q", "main")

        # ---- commits that must appear (base hotfix/SG_19.17.1 .. target) ----
        msg3 = ("Add payment limits\n\n- first bullet\n- second <b>bullet</b> & \"quotes\"\n"
                "\nSigned-off-by: Alice Lim <alice@example.com>")
        r.write("app/config.yml", "v: 4\n")
        r.write("src/pay.py", "limit = 5\n")
        c3 = r.commit(msg3, author=("Zoë Tan", "zoe@example.com"))
        r.git("mv", "a.txt", "src/renamed.txt")
        c4 = r.commit("Rename a.txt")
        r.git("rm", "-q", "b.txt")
        c5 = r.commit("Delete b.txt")
        r.write(b"docs/na\xefve & <odd> name.md", b"x\n")
        c6 = r.raw_commit(b"Caf\xe9 latin-1 message\n", ("Bob Ong", "bob@example.com"))
        # merge commit with no file changes in --name-status
        r.git("checkout", "-q", "-b", "side")
        r.write("side.txt", "side\n")
        c7 = r.commit("Side work")
        r.git("checkout", "-q", "main")
        r.write("main2.txt", "m\n")
        c8 = r.commit("Main work")
        r.tick += 1
        env = {"GIT_AUTHOR_NAME": "Merger", "GIT_AUTHOR_EMAIL": "m@example.com",
               "GIT_AUTHOR_DATE": "2024-04-01T09:00:00+08:00",
               "GIT_COMMITTER_DATE": "2024-04-01T09:00:00+08:00"}
        r.git("merge", "-q", "--no-ff", "-m", "Merge branch 'side'", "side", env=env)
        c9 = r.git("rev-parse", "HEAD")
        r.push(TARGET)
        # same version, different kind is not a base and not a target
        # higher version: must not be chosen
        r.write("later.txt", "later\n")
        r.commit("After target (19.18.1)")
        r.push("hotfix/SG_19.18.1")
        r.git("reset", "-q", "--hard", c9)
        # local-only commit on a local branch with the target's name
        r.git("checkout", "-q", "-b", TARGET)
        r.write("local.txt", "local\n")
        r.commit("LOCAL ONLY commit must not appear")
        # a local branch that shadows the base name, pointing somewhere else
        r.git("branch", "origin/hotfix/SG_19.17.1", c0)
        r.git("checkout", "-q", "main")
        self.hashes = dict(c0=c0, c1=c1, c2=c2, c3=c3, c4=c4, c5=c5, c6=c6, c7=c7, c8=c8, c9=c9)

        A = ("Alice Lim", "alice@example.com")
        d = lambda n: f"2024-03-{n:02d}T10:{n:02d}:00+08:00"
        # git log order (newest first). ticks: c3=5, c4=6, c5=7, c6=8, c7=9, c8=10, merge 2024-04-01
        nonascii = "docs/na�ve & <odd> name.md"
        self.expected["alpha"] = {
            "app/config.yml": [("M", msg3, d(5), c3, "Zoë Tan", "zoe@example.com", None)],
            "b.txt": [("D", "Delete b.txt", d(7), c5, *A, None)],
            nonascii: [("A", "Caf� latin-1 message", d(8), c6, "Bob Ong", "bob@example.com", None)],
            "main2.txt": [("A", "Main work", d(10), c8, *A, None)],
            "side.txt": [("A", "Side work", d(9), c7, *A, None)],
            "src/pay.py": [("A", msg3, d(5), c3, "Zoë Tan", "zoe@example.com", None)],
            "src/renamed.txt": [("R100", "Rename a.txt", d(6), c4, *A, "a.txt")],
            "": [(None, "Merge branch 'side'", "2024-04-01T09:00:00+08:00", c9, "Merger", "m@example.com", None)],
        }

    def build_beta(self):
        # has 19.17.0 and 19.18.1 but no release/SG_19.18.0
        r = Repo(self.repos, "beta", self.remotes)
        r.write("x.txt", "1\n")
        r.commit("beta base")
        r.push("release/SG_19.17.0")
        r.write("x.txt", "2\n")
        r.commit("beta later")
        r.push("hotfix/SG_19.18.1")
        r.push("release/SG_19.18")    # same version written without .0 (not the target name)

    def build_gamma(self):
        # target exists but nothing lower (only same / higher / other country)
        r = Repo(self.repos, "gamma", self.remotes)
        r.write("x.txt", "1\n")
        r.commit("gamma one")
        r.push(TARGET)
        r.push("release/SG_20.0.0")
        r.push("release/MY_19.17.0")
        r.push("hotfix/SG_19.18.0")   # same version as target: never a base
        r.push("feature/whatever")

    def build_delta(self):
        # release/ and hotfix/ share 19.17.0: release/ is the base
        r = Repo(self.repos, "delta", self.remotes)
        r.write("x.txt", "1\n")
        r.commit("delta one")
        r.push("release/SG_19.10.0")
        r.push("release/SG_19.17.0")
        r.write("x.txt", "hf\n")
        r.commit("delta hotfix only")
        r.push("hotfix/SG_19.17.0")
        r.git("reset", "-q", "--hard", "HEAD~1")
        r.write("x.txt", "2\n")
        self.delta_target_commit = r.commit("delta two")
        r.push(TARGET)

    def build_iota(self):
        # target exists, nothing lower, but origin/master exists (fallback base)
        r = Repo(self.repos, "iota", self.remotes)
        r.write("x.txt", "1\n")
        r.commit("iota on master")
        r.push("master")
        r.push("release/SG_20.0.0")
        r.write("y.txt", "2\n")
        self.iota_target_commit = r.commit("iota only on target")
        r.push(TARGET)

    def build_theta(self):
        # a tie that can't be resolved: two hotfix/ branches with the same version
        r = Repo(self.repos, "theta", self.remotes)
        r.write("x.txt", "1\n")
        r.commit("theta one")
        r.push("release/SG_19.10.0")
        r.push("hotfix/SG_19.17.0")
        r.push("hotfix/SG_19.017.0")
        r.write("x.txt", "2\n")
        r.commit("theta two")
        r.push(TARGET)

    def build_eta(self):
        # 19.17 and 19.17.0 both exist: 19.17.0 is the higher one and is the base
        r = Repo(self.repos, "eta", self.remotes)
        r.write("x.txt", "1\n")
        r.commit("eta on 19.10.0")
        r.push("release/SG_19.10.0")
        r.write("x.txt", "2\n")
        r.commit("eta on 19.17")
        r.push("hotfix/SG_19.17")
        r.write("x.txt", "3\n")
        r.commit("eta on 19.17.0")
        r.push("release/SG_19.17.0")
        r.write("x.txt", "4\n")
        self.eta_target_commit = r.commit("eta only on target")
        r.push(TARGET)

    def build_epsilon(self):
        # target identical to base
        r = Repo(self.repos, "epsilon", self.remotes)
        r.write("x.txt", "1\n")
        r.commit("epsilon one")
        r.push("hotfix/SG_19.17.1")
        r.push(TARGET)

    def build_zeta(self):
        # fetch fails: the remote no longer exists
        r = Repo(self.repos, "zeta", self.remotes)
        r.write("x.txt", "1\n")
        r.commit("zeta one")
        r.push(TARGET)
        r.push("hotfix/SG_19.17.1")
        shutil.rmtree(r.bare)


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def run_report(repos, target=TARGET, template=None, out=None, bitbucket=None, fallback=None):
    logs = []
    out = out or os.path.join(tempfile.mkdtemp(prefix="ghout-"), "index.html")
    rep = gh.generate_report(repos, target, "origin", out, template, bitbucket, fallback,
                             log=logs.append)
    return rep, logs


class ConfigTests(unittest.TestCase):
    def test_country_and_version(self):
        b = gh.parse_branch_name("release/SG_19.18.0")
        self.assertEqual((b.kind, b.country, b.version), ("release", "SG", "19.18.0"))
        b = gh.parse_branch_name("hotfix/MY_2.10")
        self.assertEqual((b.kind, b.country, b.version), ("hotfix", "MY", "2.10"))

    def test_invalid_branch_names(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for bad in ["release/SG-19.18.0", "feature/SG_19.18.0", "release/SG_19.18.x",
                    "release/SG_", "SG_19.18.0", "release/SG_19..1", "origin/release/SG_19.18.0",
                    "release/SG_19.18.0 ", "", "release/SG_19.\u0661\u0668.0"]:
            with self.subTest(bad=bad):
                with self.assertRaises(gh.ConfigError) as cm:
                    gh.generate_report(tmp, bad, "origin", os.path.join(tmp, "o.html"), None,
                                       log=lambda s: None)
                self.assertIn("TARGET_BRANCH", str(cm.exception))
                self.assertFalse(os.path.exists(os.path.join(tmp, "o.html")))

    def test_missing_folder(self):
        missing = os.path.join(tempfile.gettempdir(), "definitely-missing-ghtest")
        with self.assertRaises(gh.ConfigError) as cm:
            gh.generate_report(missing, TARGET, "origin", "x.html", None, log=lambda s: None)
        self.assertIn("REPOS_FOLDER", str(cm.exception))

    def test_bad_bitbucket_base_url(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        tpl = {"commit": "{base_url}/c/{commit}", "compare": "{base_url}/x"}
        for bad, template in (("bitbucket.corp.example", None), ("https://h/x?y=1", None),
                              ("https://user:pw@h", None), ("https://bitbucket.corp.example", tpl)):
            with self.subTest(bad=bad), self.assertRaises(gh.ConfigError) as cm:
                gh.generate_report(tmp, TARGET, "origin", os.path.join(tmp, "o.html"), template, bad,
                                   log=lambda s: None)
            self.assertIn("BITBUCKET_BASE_URL", str(cm.exception))

    def test_bad_template(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for bad in ["{base_url}/commit/{commit}", {"commit": "{base_url}/c/{sha}", "compare": "{base_url}"},
                    {"commit": "{base_url}/c/{commit}"}]:
            with self.subTest(bad=bad), self.assertRaises(gh.ConfigError):
                gh.generate_report(tmp, TARGET, "origin", os.path.join(tmp, "o.html"), bad,
                                   log=lambda s: None)

    def test_main_reports_config_error(self):
        err = io.StringIO()
        with mock.patch.object(gh, "TARGET_BRANCH", "bad"), contextlib.redirect_stderr(err):
            self.assertEqual(gh.main(), 2)
        self.assertIn("Configuration error", err.getvalue())

    def test_version_ordering(self):
        self.assertLess(gh.version_key("19.9.0"), gh.version_key("19.17.1"))
        self.assertLess(gh.version_key("19.17.1"), gh.version_key("19.18.0"))
        self.assertLess(gh.version_key("19.17"), gh.version_key("19.17.0"))
        self.assertLess(gh.version_key("19.17.0"), gh.version_key("19.17.1"))
        self.assertEqual(gh.version_key("19.017.0"), gh.version_key("19.17.0"))


class BranchSelectionUnitTests(unittest.TestCase):
    T = gh.parse_branch_name(TARGET)

    def sel(self, refs):
        return gh.select_branches(refs, "origin", self.T)

    def test_closest_lower_across_release_and_hotfix(self):
        s = self.sel(["origin/release/SG_19.9.0", "origin/release/SG_19.17.0",
                      "origin/hotfix/SG_19.17.1", TARGET and "origin/" + TARGET,
                      "origin/hotfix/SG_19.18.1", "origin/release/MY_19.18.0"])
        self.assertEqual(s.outcome, "ok")
        self.assertEqual(s.base_ref, "origin/hotfix/SG_19.17.1")
        self.assertEqual(s.target_ref, "origin/" + TARGET)

    def test_user_reported_set(self):
        s = self.sel(["origin/release/SG_19.10.0", "origin/hotfix/SG_19.17",
                      "origin/release/SG_19.17.0", "origin/" + TARGET])
        self.assertEqual((s.outcome, s.base_ref), ("ok", "origin/release/SG_19.17.0"))

    def test_same_version_release_preferred(self):
        s = self.sel(["origin/release/SG_19.10.0", "origin/hotfix/SG_19.17.0",
                      "origin/release/SG_19.17.0", "origin/" + TARGET])
        self.assertEqual((s.outcome, s.base_ref), ("ok", "origin/release/SG_19.17.0"))
        self.assertEqual([c.name for c in s.same_version_as_base], ["hotfix/SG_19.17.0"])

    def test_unresolvable_ties(self):
        for refs in (["origin/hotfix/SG_19.17.0", "origin/hotfix/SG_19.017.0"],
                     ["origin/release/SG_19.17.0", "origin/release/SG_19.017.0"],
                     ["origin/release/SG_19.17.0", "origin/release/SG_19.017.0", "origin/hotfix/SG_19.17.0"]):
            with self.subTest(refs=refs):
                s = self.sel(refs + ["origin/" + TARGET])
                self.assertEqual(s.outcome, "tie")
                self.assertIsNone(s.base_ref)

    def test_numeric_not_lexicographic(self):
        s = self.sel(["origin/release/SG_19.9.0", "origin/release/SG_19.10.0", "origin/" + TARGET])
        self.assertEqual(s.base_ref, "origin/release/SG_19.10.0")

    def test_other_remote_ignored(self):
        s = self.sel(["upstream/hotfix/SG_19.17.1", "origin/release/SG_19.9.0", "origin/" + TARGET])
        self.assertEqual(s.base_ref, "origin/release/SG_19.9.0")
        self.assertIn(("upstream/hotfix/SG_19.17.1", "not on remote 'origin'"), s.ignored)


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        cls.report, cls.logs = run_report(cls.fx.repos)
        cls.page = cls.report.html
        cls.by_name = {r.name: r for r in cls.report.repos}
        cls.ex = extract(cls.page)

    @classmethod
    def tearDownClass(cls):
        cls.fx.cleanup()

    # ---- discovery / console ---------------------------------------------
    def test_discovery(self):
        self.assertEqual(sorted(self.by_name), ["alpha", "beta", "delta", "epsilon", "eta", "gamma", "iota", "theta", "zeta"])
        self.assertEqual(self.report.skipped, ["notes"])
        self.assertIn("Skipped (not a git repository): notes", self.logs)

    def test_ignored_branches_logged(self):
        joined = "\n".join(self.logs)
        self.assertIn("[gamma] ignored branch origin/feature/whatever", joined)
        self.assertIn("[alpha] ignored branch origin/release/MY_19.18.0: country 'MY' is not 'SG'", joined)

    # ---- target -------------------------------------------------------------
    def test_missing_target_no_change_no_fallback(self):
        b = self.by_name["beta"]
        self.assertEqual(b.state, gh.STATE_NO_CHANGE)
        self.assertEqual(b.reason, "Branch `release/SG_19.18.0` not found on remote.")
        self.assertEqual(b.commits, [])
        self.assertIsNone(b.base_branch)
        self.assertNotIn("beta later", self.page)
        self.assertNotIn("beta base", self.page)

    # ---- base selection -----------------------------------------------------
    def test_base_is_closest_lower(self):
        a = self.by_name["alpha"]
        self.assertEqual(a.state, gh.STATE_CHANGES)
        self.assertEqual(a.base_branch, "origin/hotfix/SG_19.17.1")
        self.assertEqual(a.target_branch, "origin/release/SG_19.18.0")
        names = [c.name for c in a.selection.candidates]
        self.assertEqual(names, ["release/SG_19.9.0", "release/SG_19.17.0", "hotfix/SG_19.17.1",
                                 "release/SG_19.18.0", "hotfix/SG_19.18.1"])
        self.assertNotIn("release/MY_19.18.0", names)

    def test_no_previous_branch(self):
        g = self.by_name["gamma"]
        self.assertEqual(g.state, gh.STATE_ERROR)
        self.assertEqual(g.reason, "No previous branch found for `SG` below `19.18.0`")
        self.assertEqual(g.commits, [])
        self.assertNotIn("gamma one", self.page)

    def test_19_17_vs_19_17_0_picks_19_17_0(self):
        # candidates: release/SG_19.10.0, hotfix/SG_19.17, release/SG_19.17.0, target
        e = self.by_name["eta"]
        self.assertEqual(e.state, gh.STATE_CHANGES)
        self.assertEqual([c.name for c in e.selection.candidates],
                         ["release/SG_19.10.0", "hotfix/SG_19.17", "release/SG_19.17.0", TARGET])
        self.assertEqual(e.base_branch, "origin/release/SG_19.17.0")
        self.assertEqual([c.hash for c in e.commits], [self.fx.eta_target_commit])
        self.assertNotIn("eta on 19.17.0", self.page)
        self.assertNotIn("eta on 19.17\n", self.page)

    def test_release_preferred_over_hotfix_with_same_version(self):
        # candidates: release/SG_19.10.0, hotfix/SG_19.17.0, release/SG_19.17.0, target
        d = self.by_name["delta"]
        self.assertEqual(d.state, gh.STATE_CHANGES)
        self.assertEqual(d.base_branch, "origin/release/SG_19.17.0")
        self.assertEqual([c.name for c in d.selection.same_version_as_base], ["hotfix/SG_19.17.0"])
        self.assertEqual([c.hash for c in d.commits], [self.fx.delta_target_commit])
        self.assertNotIn("delta hotfix only", self.page)
        sec = self.page[self.page.index('id="repo/delta"'):]
        sec = sec[:sec.index("</details>")]
        self.assertIn("<dt>Same version as base</dt>", sec)
        self.assertIn("<code>origin/hotfix/SG_19.17.0</code> has the same version as the base branch", sec)

    def test_no_fallback_when_not_configured(self):
        i = self.by_name["iota"]
        self.assertEqual(i.state, gh.STATE_ERROR)
        self.assertEqual(i.reason, "No previous branch found for `SG` below `19.18.0`")
        self.assertNotIn("iota only on target", self.page)

    def test_unresolvable_tie_is_error(self):
        t = self.by_name["theta"]
        self.assertEqual(t.state, gh.STATE_ERROR)
        self.assertIsNone(t.base_branch)
        self.assertIn("`origin/hotfix/SG_19.017.0`", t.reason)
        self.assertIn("`origin/hotfix/SG_19.17.0`", t.reason)
        self.assertNotIn("theta two", self.page)

    # ---- scope --------------------------------------------------------------
    def test_scope_only_base_to_target(self):
        a = self.by_name["alpha"]
        h = self.fx.hashes
        got = [c.hash for c in a.commits]
        want = git(self.fx.alpha.path, "rev-list", "refs/remotes/origin/hotfix/SG_19.17.1.."
                   "refs/remotes/origin/release/SG_19.18.0").split()
        self.assertEqual(got, want)
        self.assertEqual(set(got), {h[k] for k in ("c3", "c4", "c5", "c6", "c7", "c8", "c9")})
        for absent in ("Initial import", "Release 19.17.0 config", "Hotfix 19.17.1: config fix",
                       "MY only commit", "After target (19.18.1)", "LOCAL ONLY"):
            self.assertNotIn(absent, self.page)
        for k in ("c0", "c1", "c2"):
            self.assertNotIn(h[k], self.page)

    def test_identical_branches_no_change(self):
        e = self.by_name["epsilon"]
        self.assertEqual(e.state, gh.STATE_NO_CHANGE)
        self.assertEqual(e.reason, "No commits on `origin/release/SG_19.18.0` that are not on "
                                   "`origin/hotfix/SG_19.17.1`.")

    def test_fetch_failure_is_error_and_others_continue(self):
        z = self.by_name["zeta"]
        self.assertEqual(z.state, gh.STATE_ERROR)
        self.assertIn("Fetch failed", z.reason)
        self.assertIn("git fetch origin --prune", z.reason)
        self.assertNotIn("zeta one", self.page)
        self.assertEqual(self.by_name["alpha"].state, gh.STATE_CHANGES)

    def test_local_refs_untouched(self):
        r = self.fx.alpha
        self.assertEqual(r.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(r.git("status", "--porcelain"), "")
        self.assertEqual(r.git("log", "-1", "--format=%s", "refs/heads/" + TARGET),
                         "LOCAL ONLY commit must not appear")

    # ---- content ------------------------------------------------------------
    def test_content_exact(self):
        got = {}
        for e in self.ex.entries:
            if e["repo"] != "repo/alpha":
                continue
            got.setdefault(e["path"], []).append(
                (e.get("status"), e["msg"], e["date"], e["hash"], e["name"],
                 e["email"].strip("<>"), e.get("old")))
        self.assertEqual(got, self.fx.expected["alpha"])
        # no entries from any other repository
        self.assertEqual({e["repo"] for e in self.ex.entries}, {"repo/alpha", "repo/delta", "repo/eta"})
        eta = [(e["path"], e["status"], e["msg"], e["hash"]) for e in self.ex.entries
               if e["repo"] == "repo/eta"]
        self.assertEqual(eta, [("x.txt", "M", "eta only on target", self.fx.eta_target_commit)])
        delta = [(e["path"], e["status"], e["msg"], e["hash"]) for e in self.ex.entries
                 if e["repo"] == "repo/delta"]
        self.assertEqual(delta, [("x.txt", "M", "delta two", self.fx.delta_target_commit)])

    def test_file_and_repo_order(self):
        a = self.by_name["alpha"]
        paths = [p for p, _ in a.files]
        self.assertEqual(paths, sorted(paths, key=lambda s: (s.casefold(), s)))
        # groups: errors first, then changes, then no change
        i_err = self.page.index("Repositories with errors")
        i_chg = self.page.index("Repositories with changes")
        i_nc = self.page.index('<h2 class="group-title">No change')
        self.assertLess(i_err, i_chg)
        self.assertLess(i_chg, i_nc)

    def test_html_escaping(self):
        self.assertIn("second &lt;b&gt;bullet&lt;/b&gt; &amp; &quot;quotes&quot;", self.page)
        self.assertNotIn("<b>bullet</b>", self.page)
        self.assertIn("docs/na�ve &amp; &lt;odd&gt; name.md", self.page)

    def test_multiline_message_kept(self):
        msg = self.fx.expected["alpha"]["src/pay.py"][0][1]
        self.assertIn(gh.esc(msg), self.page)

    def test_top_of_page(self):
        top = self.page[:self.page.index('<div class="toolbar"')]
        for s in ("Generated", gh.esc(self.fx.repos), TARGET, "<code>SG</code>", "<code>19.18.0</code>",
                  "<code>origin/hotfix/SG_19.17.1</code> → <code>origin/release/SG_19.18.0</code>"):
            self.assertIn(s, top)

    def test_no_links_for_unknown_platform(self):
        # the remotes are local paths: no platform, no template
        self.assertEqual(self.ex.links, [])
        self.assertIn("Links aren’t available for this repository", self.page)
        # one note per repository with changes (alpha, delta and eta)
        self.assertEqual(self.page.count("Links aren’t available"), 3)

    def test_candidates_listed_with_remote_prefix(self):
        # beta has no target; its candidate list must still show full remote branch names
        sec = self.page[self.page.index('id="repo/beta"'):]
        sec = sec[:sec.index("</details>")]
        self.assertIn("<code>origin/release/SG_19.17.0</code>", sec)
        self.assertIn("<code>origin/hotfix/SG_19.18.1</code>", sec)
        self.assertNotIn("<code>/release", sec)

    def test_template_without_web_url_gives_no_links(self):
        rep, _ = run_report(self.fx.repos, template={"commit": "{base_url}/c/{commit}",
                                                     "compare": "{base_url}/x/{base}...{target}"})
        self.assertEqual(extract(rep.html).links, [])
        self.assertIn("DIFF_URL_TEMPLATE uses {base_url}, but the remote URL cannot be converted",
                      rep.html)

    def test_three_column_layout(self):
        # every file row: column 1 = path, column 2 = collapsible code diff,
        # column 3 = collapsible changes; both open by default
        rows = re.findall(r'<div class="file[^"]*" id="[^"]*" data-path="[^"]*" style="contain-intrinsic-size:auto \d+px">\n'
                          r'<div class="file-col">.*?</div></div></div>\n'
                          r'<details class="diff-col" open><summary class="diff-head">Code diff</summary>\n'
                          r'.*?</details>\n'
                          r'<details class="detail-col" open><summary class="detail-head">Changes</summary>',
                          self.page, flags=re.S)
        n_rows = sum(len(r.files) + (1 if r.no_file_commits else 0)
                     for r in self.report.repos if r.state == gh.STATE_CHANGES)
        self.assertEqual(len(rows), n_rows)
        self.assertEqual(self.page.count('<details class="detail-col" open>'), n_rows)
        self.assertEqual(self.page.count('<details class="diff-col" open>'), n_rows)
        self.assertIn('id="toggle-details"', self.page)
        self.assertIn('id="toggle-diff"', self.page)
        for row in rows:
            col1 = row[row.index('<div class="file-col">'):row.index('<details class="diff-col"')]
            self.assertNotIn("badge", col1)
            col2 = row[row.index('<details class="diff-col"'):row.index('<details class="detail-col"')]
            self.assertNotIn("badge", col2)

    def _diff_cell(self, repo, path):
        fid = gh.anchor_id("file", repo, path)
        sec = self.page[self.page.index(f'id="{gh.esc(fid)}"'):]
        cell = sec[sec.index('<details class="diff-col"'):sec.index('<details class="detail-col"')]
        return unpack_cell(cell)

    def test_code_diff_matches_git(self):
        a = self.by_name["alpha"]
        repo = self.fx.alpha.path
        rng = "refs/remotes/origin/hotfix/SG_19.17.1...refs/remotes/origin/release/SG_19.18.0"
        for path, entries in a.files:
            if "\ufffd" in path:
                # a non-UTF-8 name can't be given to git as a pathspec here, so check content
                self.assertIn("\n+x\n", a.diffs[path][0])
                continue
            old = sorted({ch.old_path for _, ch in entries if ch.old_path})
            p = subprocess.run(["git", "-C", repo, "--literal-pathspecs", "diff", rng, "--no-color",
                                "--find-renames", "--src-prefix=a/", "--dst-prefix=b/", "--", path, *old],
                               capture_output=True, check=True)
            want = p.stdout.decode("utf-8", errors="replace")
            self.assertEqual(a.diffs[path], (want, ""), path)
            # shown: every line from the first @@ on; the header only when there is no @@
            raw = want.rstrip("\n").split("\n")
            if any(l.startswith("@@") for l in raw):
                expected = raw[next(k for k, l in enumerate(raw) if l.startswith("@@")):]
            else:
                expected = [l for l in raw if not l.startswith(("diff --git ", "index ", "--- ", "+++ "))]
            cell = self._diff_cell("alpha", path)
            lines = re.findall(r'<span class="d[mhadc]">(.*?)</span>', cell)
            self.assertEqual([html.unescape(l) for l in lines], expected, path)
            for hdr in ("diff --git", "index ", "--- ", "+++ "):
                self.assertNotIn(f'<span class="dm">{hdr}', cell, path)
                self.assertNotIn(f'<span class="dh">{hdr}', cell, path)

    def test_code_diff_kinds(self):
        cfg = self._diff_cell("alpha", "app/config.yml")
        self.assertIn('<span class="dd">-v: 3</span>', cfg)
        self.assertIn('<span class="da">+v: 4</span>', cfg)
        self.assertIn('<span class="dh">@@', cfg)
        self.assertNotIn("diff --git", cfg)
        self.assertNotIn(">index ", cfg)
        self.assertNotIn("--- a/app/config.yml", cfg)
        self.assertNotIn("+++ b/app/config.yml", cfg)
        # pure rename (no code change): the rename lines are the only record, so they stay
        ren = self._diff_cell("alpha", "src/renamed.txt")
        self.assertIn('<span class="dm">rename from a.txt</span>', ren)
        self.assertIn('<span class="dm">rename to src/renamed.txt</span>', ren)
        self.assertNotIn("diff --git", ren)
        dele = self._diff_cell("alpha", "b.txt")
        self.assertNotIn("deleted file mode", dele)
        self.assertIn('<span class="dd">-bee</span>', dele)
        # escaping of file names inside the diff
        odd = self._diff_cell("alpha", "docs/na\ufffdve & <odd> name.md")
        self.assertIn('<span class="da">+x</span>', odd)
        # the no-file-changes row has an empty diff cell
        nf = self.page[self.page.index('id="nofiles/alpha"'):]
        nf = nf[nf.index('<details class="diff-col"'):nf.index('<details class="detail-col"')]
        self.assertNotIn('<pre class="diff">', nf)

    def test_diff_code_lines_unit(self):
        diff = ("diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n ctx\n"
                "--- removed line that looks like a header\n+++ added line that looks like a header\n"
                "diff --git a/y b/y\nnew file mode 100644\nindex 0..3\nBinary files /dev/null and b/y differ\n")
        self.assertEqual(gh.diff_code_lines(diff), [
            ("h", "@@ -1,3 +1,3 @@"), ("c", " ctx"),
            ("d", "--- removed line that looks like a header"),
            ("a", "+++ added line that looks like a header"),
            ("m", "new file mode 100644"), ("m", "Binary files /dev/null and b/y differ")])
        self.assertEqual(gh.diff_code_lines(""), [])

    def test_pack_diff_round_trip(self):
        lines = [("h", "@@ -1 +1 @@"), ("d", "-caf\u00e9 <b>&amp;</b> \"q\""), ("a", "+\ufffd tab\there"),
                 ("c", " "), ("c", ""), ("m", "Binary files a/x and b/x differ"), ("a", "+trailing \r")]
        packed = gh.pack_diff(lines)
        self.assertRegex(packed, r"^[A-Za-z0-9+/=]+$")        # safe inside an HTML attribute
        self.assertEqual(gh.unpack_diff(packed), lines)
        self.assertEqual(gh.pack_diff(lines), packed)           # deterministic output
        big = [("a", "+line %d of a large file" % i) for i in range(50000)]
        self.assertEqual(gh.unpack_diff(gh.pack_diff(big)), big)  # nothing cut
        self.assertLess(len(gh.pack_diff(big)), len("".join(k + l for k, l in big)) / 3)

    def test_diffs_stored_compressed(self):
        self.assertNotIn('<span class="da">', self.page)       # no pre-drawn diff markup
        pres = re.findall(r'<pre class="diff" data-z="[^"]*" data-n="(\d+)" style="--n:(\d+)"></pre>', self.page)
        self.assertTrue(pres)
        self.assertTrue(all(a == b for a, b in pres))
        for i in ("id=\"draw-all\"", "id=\"qd\"", "id=\"qd-next\"", "id=\"qd-prev\"", "DecompressionStream", "<noscript>"):
            self.assertIn(i, self.page)

    def test_repo_diffs_two_git_calls(self):
        calls = []
        real = gh.run_git
        def spy(repo, args):
            calls.append(args[0])
            return real(repo, args)
        with mock.patch.object(gh, "run_git", side_effect=spy):
            d = gh.repo_diffs(self.fx.alpha.path, "origin/hotfix/SG_19.17.1", "origin/" + TARGET)
        self.assertEqual(calls, ["diff", "diff"])
        a = self.by_name["alpha"]
        self.assertEqual(set(d), {p for p, _ in a.files} | {"a.txt"})   # a.txt: old name of the rename
        self.assertEqual(d["a.txt"], d["src/renamed.txt"])

    def test_code_diff_failure_keeps_commits(self):
        with self.assertRaises(gh.GitError):
            gh.repo_diffs(self.fx.alpha.path, "origin/no-such-branch", "origin/" + TARGET)

    def test_output_file_written(self):
        with open(self.report.config.output_file, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), self.page)


class FallbackBaseTests(unittest.TestCase):
    """FALLBACK_BASE_BRANCH = "master": used only when the target exists and nothing is lower."""

    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        cls.report, cls.logs = run_report(cls.fx.repos, fallback="master")
        cls.page = cls.report.html
        cls.by_name = {r.name: r for r in cls.report.repos}

    @classmethod
    def tearDownClass(cls):
        cls.fx.cleanup()

    def test_fallback_used(self):
        i = self.by_name["iota"]
        self.assertEqual(i.state, gh.STATE_CHANGES)
        self.assertEqual(i.base_branch, "origin/master")
        self.assertTrue(i.selection.base_is_fallback)
        self.assertEqual([c.hash for c in i.commits], [self.fx.iota_target_commit])
        self.assertNotIn("iota on master", self.page)
        sec = self.page[self.page.index('id="repo/iota"'):]
        sec = sec[:sec.index("</details>")]
        self.assertIn('<span class="tag tag-fallback">fallback base</span>', sec)
        self.assertIn("<dt>Fallback base</dt>", sec)

    def test_fallback_missing_keeps_error(self):
        g = self.by_name["gamma"]
        self.assertEqual(g.state, gh.STATE_ERROR)
        self.assertEqual(g.reason, "No previous branch found for `SG` below `19.18.0`, and the fallback "
                                   "base branch `origin/master` was not found on remote.")

    def test_fallback_not_used_otherwise(self):
        self.assertEqual(self.by_name["alpha"].base_branch, "origin/hotfix/SG_19.17.1")
        self.assertFalse(self.by_name["alpha"].selection.base_is_fallback)
        self.assertEqual(self.by_name["beta"].state, gh.STATE_NO_CHANGE)     # no target: no fallback
        self.assertEqual(self.by_name["theta"].state, gh.STATE_ERROR)        # tie: no fallback
        self.assertEqual(self.page.count('tag-fallback">fallback base'), 2)  # overview + iota header

    def test_select_unit(self):
        T = gh.parse_branch_name(TARGET)
        s = gh.select_branches(["origin/master", "origin/" + TARGET], "origin", T, "master")
        self.assertEqual((s.outcome, s.base_ref, s.base_is_fallback), ("ok", "origin/master", True))
        s = gh.select_branches(["origin/master", "origin/release/SG_1.0", "origin/" + TARGET], "origin", T, "master")
        self.assertEqual((s.base_ref, s.base_is_fallback), ("origin/release/SG_1.0", False))
        s = gh.select_branches(["origin/master"], "origin", T, "master")
        self.assertEqual(s.outcome, "no_target")
        s = gh.select_branches(["upstream/master", "origin/" + TARGET], "origin", T, "master")
        self.assertEqual(s.outcome, "no_base")
        s = gh.select_branches(["origin/master", "origin/hotfix/SG_1.0", "origin/hotfix/SG_1.0.0",
                                "origin/" + TARGET], "origin", T, "master")
        self.assertEqual(s.outcome, "ok")   # 1.0 < 1.0.0, not a tie
        s = gh.select_branches(["origin/master", "origin/hotfix/SG_1.0", "origin/hotfix/SG_01.0",
                                "origin/" + TARGET], "origin", T, "master")
        self.assertEqual(s.outcome, "tie")

    def test_bad_fallback_config(self):
        for bad in ("", " master", "origin master", "refs/heads/master", "a..b", 5):
            with self.subTest(bad=bad), self.assertRaises(gh.ConfigError):
                gh.generate_report(self.fx.repos, TARGET, "origin", "x.html", None, None, bad,
                                   log=lambda s: None)


class NestedFolderTests(unittest.TestCase):
    """Repositories inside group folders: REPOS_FOLDER/<group>/<repo>."""

    def test_discover_tree(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        def mk(*parts, git=None):
            path = os.path.join(root, *parts)
            os.makedirs(path, exist_ok=True)
            if git == "dir":
                os.makedirs(os.path.join(path, ".git"))
            elif git == "file":                                  # worktree / submodule style
                with open(os.path.join(path, ".git"), "w") as fh:
                    fh.write("gitdir: elsewhere\n")
        mk("alpha", git="dir")
        mk("alpha", "inner", git="dir")                          # inside a repo: not searched
        mk("teamA", "svc1", git="dir")
        mk("teamA", "svc2", git="file")
        mk("teamA", "docs")                                      # no repo under it
        mk("teamB", "notes", "sub")                              # no repo anywhere below teamB
        mk("deep", "a", "b", "repo", git="dir")                  # 4 levels down
        mk("Payments", "api", git="dir")
        os.symlink(os.path.join(root, "teamA"), os.path.join(root, "zlink"))      # not followed
        os.symlink(os.path.join(root, "alpha"), os.path.join(root, "alpha-link"))  # link to a repo
        with open(os.path.join(root, "README.txt"), "w") as fh:
            fh.write("x")
        repos, skipped = gh.discover_repos(root, 3)
        self.assertEqual(repos, ["alpha", "alpha-link", "Payments/api", "teamA/svc1", "teamA/svc2"])
        self.assertEqual(skipped, ["deep", "teamA/docs", "teamB", "zlink"])
        repos4, skipped4 = gh.discover_repos(root, 4)
        self.assertIn("deep/a/b/repo", repos4)
        self.assertNotIn("deep", skipped4)
        repos1, skipped1 = gh.discover_repos(root, 1)                # old behaviour: direct subfolders only
        self.assertEqual(repos1, ["alpha", "alpha-link"])
        self.assertEqual(skipped1, ["deep", "Payments", "teamA", "teamB", "zlink"])

    def test_report_with_group_folders(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        repos, remotes = os.path.join(tmp, "repos"), os.path.join(tmp, "remotes")
        os.makedirs(remotes)
        made = {}
        for group, name in (("teamA", "svc"), ("teamB", "svc"), (None, "top")):
            parent = os.path.join(repos, group) if group else repos
            os.makedirs(parent, exist_ok=True)
            r = Repo(parent, name, os.path.join(remotes, group or ""))  # same repo name in two groups
            r.write("x.txt", "1\n")
            r.commit(f"{group} base")
            r.push("hotfix/SG_19.17.1")
            r.write("x.txt", "2\n")
            r.commit(f"{group or 'top'} change")
            r.push(TARGET)
            made[f"{group}/{name}" if group else name] = r
        out = os.path.join(tmp, "index.html")
        rep = gh.generate_report(repos, TARGET, "origin", out, None, None, None,
                                 os.path.join(tmp, "stats.html"), log=lambda s: None)
        self.assertEqual([r.name for r in rep.repos], ["teamA/svc", "teamB/svc", "top"])
        self.assertEqual(rep.skipped, [])
        for r in rep.repos:
            self.assertEqual(r.state, gh.STATE_CHANGES, r.name)
            self.assertEqual(r.path, made[r.name].path)
            self.assertEqual([c.message for c in r.commits], [f"{r.name.split('/')[0] if '/' in r.name else 'top'} change"])
        # same repo name in two groups stays apart on both pages
        for page in (rep.html, rep.stats_html):
            self.assertIn('id="file/teamA/svc/x.txt"' if page is rep.html else 'id="stats-file/teamA/svc/x.txt"', page)
            self.assertIn('id="file/teamB/svc/x.txt"' if page is rep.html else 'id="stats-file/teamB/svc/x.txt"', page)
        self.assertIn('<span class="repo-name">teamA/svc</span>', rep.html)


class StatsPageTests(unittest.TestCase):
    """The separate statistics page (STATS_OUTPUT_FILE)."""

    def test_file_types(self):
        cases = {
            "src/UserController.java": "Controller", "OrderProcessor.java": "Processor",
            "routes.js": "Route", "AppRouter.tsx": "Route", "application-config.yml": "Config",
            "Configuration.java": "Config", "PaymentApi.java": "API", "APIClient.java": "API",
            "UserBuilder.java": "Builder", "OrderRequestMapper.java": "Mapper", "UserVO.java": "VO",
            "UserVo.java": "VO", "CreateUserRequest.java": "Request", "UserResponse.java": "Response",
            "UserControllerTest.java": "Test", "test_api.py": "Test", "user.spec.ts": "Test",
            "UserServiceIT.java": "Test", "README.md": "Other", ".gitignore": "Other",
            "contest.py": "Other", "latest.json": "Other", "src/test/Helper.java": "Other",
            # Properties, Util, Constant (by name)
            "src/main/resources/application.properties": "Properties", "AppProps.java": "Properties",
            "DateUtils.java": "Util", "StringUtil.java": "Util", "util.py": "Util",
            "ErrorConstants.java": "Constant", "constants.ts": "Constant",
            # folder groups, only when the name matches no type
            "src/main/resources/db/schema.sql": "Resources", "src/test/resources/data.json": "Resources",
            "routes/user.js": "Route", "web/routes/index.ts": "Route",
            "templates/email.html": "Template", "src/main/resources/templates/mail.ftl": "Template",
            "resources/routes/x.js": "Route",                       # innermost folder wins
            "src/main/resources/UserMapper.xml": "Mapper",          # the name wins over the folder
            "src/main/resources/application-config.yml": "Config",
        }
        for path, want in cases.items():
            with self.subTest(path=path):
                self.assertEqual(gh.file_type(path), want)

    def _commit(self, h, name, msg, *paths):
        return gh.Commit(h * 40, name, name.lower() + "@x", "2024-01-01T00:00:00+08:00", msg,
                         [gh.FileChange("M", "", p) for p in paths])

    def test_counts_and_grouping(self):
        c1 = self._commit("1", "Ann", "Fix user API\n\nsecond line", "src/UserController.java", "src/UserVO.java")
        c2 = self._commit("2", "Bob", "Rename field", "src/UserController.java", "src/UserControllerTest.java")
        c3 = self._commit("3", "Ann", "Tidy <tags>", "src/UserController.java", "README.md", "src/UserControllerTest.java")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        cfg = gh.load_config(tmp, TARGET, "origin", os.path.join(tmp, "index.html"), None,
                             stats_output_file=os.path.join(tmp, "stats.html"))
        r = gh.RepoReport("svc", tmp, state=gh.STATE_CHANGES)
        r.selection = gh.BranchSelection(target_ref="origin/" + TARGET, base_ref="origin/hotfix/SG_19.17.1")
        r.commits = [c3, c2, c1]
        r.files, r.no_file_commits = gh.group_by_file(r.commits)
        stats = {fs.path: fs for fs in gh.file_stats(r)}
        uc = stats["src/UserController.java"]
        self.assertEqual((uc.type, len(uc.commits), uc.users), ("Controller", 3, ["Ann", "Bob"]))
        self.assertEqual((stats["src/UserVO.java"].type, len(stats["src/UserVO.java"].commits)), ("VO", 1))
        self.assertEqual(stats["README.md"].type, "Other")
        page = gh.render_stats_html(cfg, "now", [r])
        rows = [("file/" + sid, name, c, u) for sid, name, c, u in re.findall(STATS_ROW, page)]
        self.assertEqual(rows, [("file/svc/src/UserController.java", "src/UserController.java", "3", "2"),
                                ("file/svc/src/UserVO.java", "src/UserVO.java", "1", "1"),
                                ("file/svc/src/UserControllerTest.java", "src/UserControllerTest.java", "2", "2"),
                                ("file/svc/README.md", "README.md", "1", "1")])
        # summary: only files changed by more than one user, linked to their row
        summary = page[page.index('<section class="summary">'):page.index("</section>", page.index('<section class="summary">'))]
        self.assertIn('table class="stats summary-table sortable"', summary)
        self.assertEqual(summary.count('<button type="button" class="sort"'), 5)
        self.assertIn('<td class="path"><span>src/UserController.java</span></td>'
                      '<td>Controller</td><td class="num">2</td><td class="num">3</td><td>Ann, Bob</td>', summary)
        self.assertNotIn("UserVO.java", summary)
        self.assertNotIn("README.md", summary)
        self.assertNotIn("UserControllerTest.java", summary)  # Test files stay out of the summary
        self.assertIn("Test files are left out of this summary", summary)
        self.assertIn("(1 file)", summary)
        self.assertIn('id="stats-file/svc/src/UserController.java"', page)
        # groups appear in the requested order, Other last
        self.assertLess(page.index(">Controller <"), page.index(">VO <"))
        self.assertLess(page.index(">VO <"), page.index(">Other <"))
        # commit messages: full, escaped, inside a collapsed list
        self.assertIn('<details class="msgs"><summary>3 commits by Ann, Bob</summary>', page)
        self.assertIn("Fix user API\n\nsecond line", page)
        self.assertIn("Tidy &lt;tags&gt;", page)
        self.assertNotIn("<tags>", page)

    def test_written_with_report(self):
        fx = Fixture()
        self.addCleanup(fx.cleanup)
        out_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out_dir)
        out, stats_out = os.path.join(out_dir, "index.html"), os.path.join(out_dir, "stats.html")
        rep = gh.generate_report(fx.repos, TARGET, "origin", out, None, None, None, stats_out,
                                 log=lambda s: None)
        with open(stats_out, encoding="utf-8") as fh:
            page = fh.read()
        self.assertEqual(page, rep.stats_html)
        self.assertIn('href="stats.html"', rep.html)          # review page links to stats
        self.assertIn('href="index.html"', page)              # and back
        alpha = next(r for r in rep.repos if r.name == "alpha")
        for path, entries in alpha.files:
            fid = gh.esc(gh.anchor_id("file", "alpha", path))
            sid = gh.esc(gh.anchor_id("stats-file", "alpha", path))
            m = re.search(r'<tr id="' + re.escape(sid) + r'"><td class="path">.*?</td>'
                          r'<td class="num">(\d+)</td><td class="num"[^>]*>(\d+)</td>', page)
            self.assertIsNotNone(m, path)
            self.assertEqual(int(m.group(1)), len(entries), path)
            self.assertEqual(int(m.group(2)), len({c.author_name for c, _ in entries}), path)
        self.assertIn(">Config <", page)                      # app/config.yml
        # every repository with changes has a summary entry; alpha has one author per file
        summary = page[page.index('<section class="summary">'):]
        summary = summary[:summary.index("</section>")]
        self.assertNotIn("alpha</a>", summary)                # no multi-user file: left out
        self.assertNotIn("No file was modified by more than one user.", summary)
        self.assertIn("No repository has a file modified by more than one user.", summary)
        for name in ("gamma", "zeta"):                        # error repos listed
            self.assertIn(f"<li><b>{name}</b>", page)
        for name in ("beta", "epsilon"):                      # no-change repos left out
            self.assertNotIn(f"<b>{name}</b>", page)
            self.assertNotIn(f"stats/{name}", page)
        # two tabs: the summary shown first, the per-type details hidden until chosen
        s_at, d_at = page.index('<div class="tab-panel" id="tab-summary"'), page.index('<div class="tab-panel" id="tab-details"')
        self.assertLess(page.index('<section class="summary">'), d_at)
        self.assertGreater(page.index('<section class="summary">'), s_at)
        self.assertGreater(page.index('<details class="repo" id="stats/alpha"'), d_at)
        self.assertIn('aria-labelledby="tab-btn-details" hidden>', page)
        self.assertNotIn('aria-labelledby="tab-btn-summary" hidden', page)

    def test_stats_config(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with self.assertRaises(gh.ConfigError):
            gh.load_config(tmp, TARGET, "origin", "index.html", None, stats_output_file="index.html")
        with self.assertRaises(gh.ConfigError):
            gh.load_config(tmp, TARGET, "origin", "index.html", None, stats_output_file="")
        self.assertIsNone(gh.load_config(tmp, TARGET, "origin", "index.html", None).stats_output_file)


class LinkIntegrationTests(unittest.TestCase):
    """Links rendered on the page for each platform (remote URL substituted)."""

    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cleanup()

    def render_with_remote(self, url, template=None, bitbucket=None):
        real = gh.run_git

        def fake(repo, args):
            if list(args[:2]) == ["remote", "get-url"]:
                return url + "\n"
            return real(repo, args)
        with mock.patch.object(gh, "run_git", side_effect=fake):
            rep, _ = run_report(self.fx.repos, template=template, bitbucket=bitbucket)
        return rep, extract(rep.html)

    def check(self, url, expect_commit, expect_compare, template=None, bitbucket=None,
              expect_file=None):
        rep, ex = self.render_with_remote(url, template, bitbucket)
        file_links = {l[1] for l in ex.links if "filelink" in l[0]}
        alpha = next(r for r in rep.repos if r.name == "alpha")
        if expect_file:
            tc = alpha.target_commit
            self.assertEqual(tc, git(self.fx.alpha.path, "rev-parse", "refs/remotes/origin/" + TARGET))
            self.assertIn(expect_file(tc, "src/renamed.txt"), file_links)
            self.assertIn(expect_file(tc, "app/config.yml"), file_links)
            # deleted at target: no file link
            self.assertFalse(any("b.txt" in u and "renamed" not in u for u in file_links), file_links)
            # every file link is for a path that exists at the target commit
            changed = [r for r in rep.repos if r.state == gh.STATE_CHANGES]
            self.assertEqual(len(file_links), sum(len([p for p, _ in r.files if p in r.target_paths])
                                                  for r in changed))
        else:
            self.assertEqual(file_links, set())
        h = self.fx.hashes
        commit_links = [l for l in ex.links if "hash" in l[0]]
        compare_links = [l for l in ex.links if "compare" in l[0]]
        for _, _, target, rel in ex.links:
            self.assertEqual(target, "_blank")
            self.assertEqual(rel, "noopener noreferrer")
        hrefs = {l[1] for l in commit_links}
        self.assertIn(expect_commit(h["c4"], "src/renamed.txt"), hrefs)
        self.assertIn(expect_commit(h["c9"], None), hrefs)
        chrefs = {l[1] for l in compare_links}
        self.assertIn(expect_compare("hotfix/SG_19.17.1", "release/SG_19.18.0", "src/renamed.txt"), chrefs)
        changed = [r for r in rep.repos if r.state == gh.STATE_CHANGES]
        self.assertEqual(len(compare_links), sum(len(r.files) for r in changed))
        self.assertEqual(len(commit_links), sum(sum(len(v) for _, v in r.files) + len(r.no_file_commits)
                                                for r in changed))
        return ex

    def test_github(self):
        sha = gh._sha256("src/renamed.txt")
        for url in ("git@github.com:acme/alpha.git", "https://user:tok@github.com/acme/alpha.git",
                    "ssh://git@github.com/acme/alpha"):
            with self.subTest(url=url):
                ex = self.check(
                    url,
                    lambda c, p: f"https://github.com/acme/alpha/commit/{c}" + (f"#diff-{gh._sha256(p)}" if p else ""),
                    lambda b, t, p: ("https://github.com/acme/alpha/compare/hotfix%2FSG_19.17.1..."
                                     f"release%2FSG_19.18.0#diff-{sha}"),
                    expect_file=lambda tc, p: f"https://github.com/acme/alpha/blob/{tc}/{p}")
                self.assertFalse(any("tok" in l[1] for l in ex.links))

    def test_gitlab(self):
        self.check("git@gitlab.com:grp/sub/alpha.git",
                   lambda c, p: f"https://gitlab.com/grp/sub/alpha/-/commit/{c}" + (f"#{gh._sha1(p)}" if p else ""),
                   lambda b, t, p: ("https://gitlab.com/grp/sub/alpha/-/compare/hotfix%2FSG_19.17.1..."
                                    f"release%2FSG_19.18.0#{gh._sha1(p)}"),
                   expect_file=lambda tc, p: f"https://gitlab.com/grp/sub/alpha/-/blob/{tc}/{p}")

    def test_bitbucket(self):
        self.check("git@bitbucket.org:ws/alpha.git",
                   lambda c, p: f"https://bitbucket.org/ws/alpha/commits/{c}" + (f"#chg-{p}" if p else ""),
                   lambda b, t, p: ("https://bitbucket.org/ws/alpha/branches/compare/"
                                    f"release%2FSG_19.18.0%0Dhotfix%2FSG_19.17.1#chg-{p}"),
                   expect_file=lambda tc, p: f"https://bitbucket.org/ws/alpha/src/{tc}/{p}")

    def test_azure(self):
        for url in ("git@ssh.dev.azure.com:v3/org/proj/alpha", "https://org@dev.azure.com/org/proj/_git/alpha"):
            with self.subTest(url=url):
                self.check(url,
                           lambda c, p: f"https://dev.azure.com/org/proj/_git/alpha/commit/{c}",
                           lambda b, t, p: ("https://dev.azure.com/org/proj/_git/alpha/branches?"
                                            "baseVersion=GBhotfix%2FSG_19.17.1&targetVersion=GBrelease%2FSG_19.18.0"
                                            "&_a=files"),
                           expect_file=lambda tc, p: f"https://dev.azure.com/org/proj/_git/alpha?path=/{p}&version=GC{tc}")

    def test_bitbucket_server_base_url(self):
        base = "https://bitbucket.corp.example"
        for url in ("ssh://git@bitbucket.corp.example:7999/proj/alpha.git",
                    "https://u@bitbucket.corp.example/scm/proj/alpha.git"):
            with self.subTest(url=url):
                self.check(url,
                           lambda c, p: f"{base}/projects/proj/repos/alpha/commits/{c}" + (f"#{p}" if p else ""),
                           lambda b, t, p: (f"{base}/projects/proj/repos/alpha/compare/diff?"
                                            "sourceBranch=refs%2Fheads%2Frelease%2FSG_19.18.0"
                                            f"&targetBranch=refs%2Fheads%2Fhotfix%2FSG_19.17.1#{p}"),
                           bitbucket=base,
                           expect_file=lambda tc, p: f"{base}/projects/proj/repos/alpha/browse/{p}?at={tc}")

    def test_bitbucket_server_detected_without_base_url(self):
        # BITBUCKET_BASE_URL not set: a self-hosted Bitbucket remote still gets links
        for url, base in (("https://u@bitbucket.corp.example/scm/proj/alpha.git", "https://bitbucket.corp.example"),
                          ("ssh://git@bitbucket.corp.example:7999/proj/alpha.git", "https://bitbucket.corp.example")):
            with self.subTest(url=url):
                self.check(url,
                           lambda c, p: f"{base}/projects/proj/repos/alpha/commits/{c}" + (f"#{p}" if p else ""),
                           lambda b, t, p: (f"{base}/projects/proj/repos/alpha/compare/diff?"
                                            "sourceBranch=refs%2Fheads%2Frelease%2FSG_19.18.0"
                                            f"&targetBranch=refs%2Fheads%2Fhotfix%2FSG_19.17.1#{p}"),
                           expect_file=lambda tc, p: f"{base}/projects/proj/repos/alpha/browse/{p}?at={tc}")

    def test_detect_bitbucket_server_base(self):
        d = gh.detect_bitbucket_server_base
        self.assertEqual(d("https://bb.corp.example/scm/proj/r.git"), "https://bb.corp.example")
        self.assertEqual(d("https://u:t@bb.corp.example:8443/bitbucket/scm/PROJ/r.git"),
                         "https://bb.corp.example:8443/bitbucket")
        self.assertEqual(d("http://bb.corp.example:7990/scm/~jdoe/r.git"), "http://bb.corp.example:7990")
        self.assertEqual(d("ssh://git@bb.corp.example:7999/proj/r.git"), "https://bb.corp.example")
        for url in ("ssh://git@bb.corp.example/proj/r.git", "https://github.com/o/r.git",
                    "git@bitbucket.org:ws/r.git", "https://bitbucket.org/ws/r.git", "C:\\repos\\r",
                    "/srv/git/r.git", "", "https://host/scm/r.git"):
            self.assertIsNone(d(url), url)
        lb = gh.make_link_builder("https://bb.corp.example/scm/proj/r.git", None, None)
        self.assertEqual((lb.platform, lb.base_url), ("bitbucket-server", "https://bb.corp.example/projects/proj/repos/r"))
        # BITBUCKET_BASE_URL, when set, still wins
        lb = gh.make_link_builder("https://bb.corp.example/scm/proj/r.git", None, "https://web.corp.example")
        self.assertEqual(lb.base_url, "https://web.corp.example/projects/proj/repos/r")

    def test_windows_path_remote_has_no_links(self):
        self.assertEqual(gh.remote_url_to_https("C:\\repos\\r"), (None, None))
        self.assertEqual(gh.remote_url_to_https("C:/repos/r"), (None, None))
        lb = gh.make_link_builder("C:/repos/r", None, "https://bb.corp.example")
        self.assertFalse(lb.available)

    def test_stats_page_links_to_bitbucket(self):
        base = "https://bitbucket.corp.example"
        real = gh.run_git
        def fake(repo, args):
            if list(args[:2]) == ["remote", "get-url"]:
                return "ssh://git@bitbucket.corp.example:7999/proj/alpha.git\n"
            return real(repo, args)
        out_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out_dir)
        with mock.patch.object(gh, "run_git", side_effect=fake):
            rep = gh.generate_report(self.fx.repos, TARGET, "origin", os.path.join(out_dir, "index.html"),
                                     None, base, None, os.path.join(out_dir, "stats.html"), log=lambda s: None)
        alpha = next(r for r in rep.repos if r.name == "alpha")
        tc = alpha.target_commit
        row = lambda p: rep.stats_html[rep.stats_html.index(f'<tr id="{gh.esc(gh.anchor_id("stats-file", "alpha", p))}">'):][:3000]
        cfg = row("app/config.yml")
        file_url = f"{base}/projects/proj/repos/alpha/browse/app/config.yml?at={tc}"
        compare = (f"{base}/projects/proj/repos/alpha/compare/diff?sourceBranch=refs%2Fheads%2Frelease%2FSG_19.18.0"
                   "&amp;targetBranch=refs%2Fheads%2Fhotfix%2FSG_19.17.1#app/config.yml")
        self.assertTrue(cfg.startswith(f'<tr id="stats-file/alpha/app/config.yml"><td class="path"><a href="{file_url}" target="_blank"'), cfg[:300])
        self.assertIn(f'<a class="compare" href="{compare}"', cfg)
        self.assertIn(f'<a class="filelink" href="{file_url}"', cfg)
        self.assertNotIn("index.html", cfg)
        # deleted at the target: the name opens the compare diff, and there is no File link
        dele = row("b.txt")
        dele = dele[:dele.index("</td>")]
        self.assertIn(f'<td class="path"><a href="{base}/projects/proj/repos/alpha/compare/diff?', dele)
        self.assertNotIn("filelink", dele)
        # no file link goes to the local review page; only the page-header link does
        self.assertEqual(re.findall(r'href="index.html[^"]*"', rep.stats_html), ['href="index.html"'])
        # summary tab file names and review-page file names open Bitbucket too
        for page in (rep.stats_html, rep.html):
            names = re.findall(r'<a(?: class="path")? href="([^"]*)" target="_blank"[^>]*title="Open the (?:file|compare diff)', page)
            self.assertTrue(names)
            self.assertTrue(all(u.startswith(base + "/projects/proj/repos/alpha/") for u in names), names[:3])
        self.assertIn(f'<a class="path" href="{file_url}"', rep.html)
        self.assertEqual(re.findall(r'href="[^"#:]*\.html[^"]*"', rep.html), ['href="stats.html"'])  # only the header link

    def test_bitbucket_server_compare_direction(self):
        # sourceBranch = the release being reviewed (target), targetBranch = the base
        lb = gh.make_link_builder("ssh://git@bb.corp.example:7999/proj/r.git", None,
                                  "https://bb.corp.example")
        url = lb.compare_url("hotfix/SG_19.17.1", "release/SG_19.18.0", "a.txt")
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(q["sourceBranch"], ["refs/heads/release/SG_19.18.0"])
        self.assertEqual(q["targetBranch"], ["refs/heads/hotfix/SG_19.17.1"])

    def test_bitbucket_server_context_path_and_personal_repo(self):
        base = "https://git.corp.example/bitbucket/"
        b = "https://git.corp.example/bitbucket/users/jane/repos/alpha"
        self.check("https://git.corp.example/bitbucket/scm/~jane/alpha.git",
                   lambda c, p: f"{b}/commits/{c}" + (f"#{p}" if p else ""),
                   lambda bb, t, p: (f"{b}/compare/diff?sourceBranch=refs%2Fheads%2Frelease%2FSG_19.18.0"
                                     f"&targetBranch=refs%2Fheads%2Fhotfix%2FSG_19.17.1#{p}"),
                   bitbucket=base,
                   expect_file=lambda tc, p: f"{b}/browse/{p}?at={tc}")

    def test_bitbucket_cloud_base_url(self):
        b = "https://bitbucket.org/ws/alpha"
        # a remote on another host: BITBUCKET_BASE_URL decides where links go
        self.check("git@mirror.corp.example:ws/alpha.git",
                   lambda c, p: f"{b}/commits/{c}" + (f"#chg-{p}" if p else ""),
                   lambda bb, t, p: (f"{b}/branches/compare/release%2FSG_19.18.0%0Dhotfix%2FSG_19.17.1#chg-{p}"),
                   bitbucket="https://bitbucket.org",
                   expect_file=lambda tc, p: f"{b}/src/{tc}/{p}")

    def test_bitbucket_base_url_with_local_remote_gives_no_links(self):
        rep, _ = run_report(self.fx.repos, bitbucket="https://bitbucket.corp.example")
        self.assertEqual(extract(rep.html).links, [])
        self.assertIn("BITBUCKET_BASE_URL is set, but the project and repository name", rep.html)

    def test_template_for_self_hosted(self):
        tpl = {"commit": "{base_url}/c/{commit}?p={path}#{path_sha256}",
               "compare": "{base_url}/cmp/{base}..{target}?p={path}"}
        self.check("git@git.corp.example:team/alpha.git",
                   lambda c, p: f"https://git.corp.example/team/alpha/c/{c}?p={p or ''}#{gh._sha256(p) if p else ''}",
                   lambda b, t, p: ("https://git.corp.example/team/alpha/cmp/hotfix%2FSG_19.17.1.."
                                    f"release%2FSG_19.18.0?p={p}"),
                   template=tpl)

    def test_template_file_link(self):
        lb = gh.make_link_builder("git@git.corp.example:t/r.git",
                                  {"commit": "{base_url}/c/{commit}", "compare": "{base_url}/x",
                                   "file": "{base_url}/raw/{target_commit}/{path}"})
        self.assertEqual(lb.file_url("abc123", "a b/c.txt"), "https://git.corp.example/t/r/raw/abc123/a%20b/c.txt")
        lb = gh.make_link_builder("git@git.corp.example:t/r.git",
                                  {"commit": "{base_url}/c/{commit}", "compare": "{base_url}/x"})
        self.assertIsNone(lb.file_url("abc123", "a.txt"))

    def test_unknown_host_without_template_has_no_links(self):
        rep, ex = self.render_with_remote("git@git.corp.example:team/alpha.git")
        self.assertEqual(ex.links, [])
        self.assertIn("Links aren’t available", rep.html)

    def test_path_encoding(self):
        lb = gh.make_link_builder("git@github.com:o/r.git", None)
        self.assertEqual(lb.compare_url("hotfix/SG_1.0", "release/SG_2.0", "a b/c#d.txt"),
                         f"https://github.com/o/r/compare/hotfix%2FSG_1.0...release%2FSG_2.0#diff-{gh._sha256('a b/c#d.txt')}")
        lb = gh.make_link_builder("git@bitbucket.org:o/r.git", None)
        self.assertTrue(lb.commit_url("abc", "a b/c#d.txt").endswith("#chg-a%20b/c%23d.txt"))


class RemoteUrlTests(unittest.TestCase):
    def test_conversions(self):
        cases = {
            "git@github.com:org/repo.git": ("https://github.com/org/repo", "github.com"),
            "https://github.com/org/repo.git/": ("https://github.com/org/repo", "github.com"),
            "ssh://git@gitlab.com:2222/g/s/r.git": ("https://gitlab.com/g/s/r", "gitlab.com"),
            "https://u:p@bitbucket.org/ws/r.git": ("https://bitbucket.org/ws/r", "bitbucket.org"),
            "git@ssh.dev.azure.com:v3/o/p/r": ("https://dev.azure.com/o/p/_git/r", "dev.azure.com"),
            "https://o@dev.azure.com/o/p/_git/r": ("https://dev.azure.com/o/p/_git/r", "dev.azure.com"),
            "http://git.local:8080/a/b.git": ("http://git.local:8080/a/b", "git.local"),
            "/srv/git/repo.git": (None, None),
            "file:///srv/git/repo.git": (None, None),
        }
        for url, want in cases.items():
            with self.subTest(url=url):
                self.assertEqual(gh.remote_url_to_https(url), want)

    def test_platform_only_for_clear_hosts(self):
        self.assertIsNone(gh.make_link_builder("git@github.example.com:o/r.git", None).platform)
        self.assertIsNone(gh.make_link_builder("git@mygitlab.internal:o/r.git", None).platform)
        self.assertEqual(gh.make_link_builder("git@github.com:o/r.git", None).platform, "github")


class ParseLogTests(unittest.TestCase):
    def test_tricky_tokens(self):
        h1, h2 = "a" * 40, "b" * 40
        M, E = gh._COMMIT_MARK, gh._END_MARK
        out = (f"\x00{M}\x00{h1}\x00N\x00e@x\x00D\x00line1\n\nM\nR100\nfoo\n\x00{E}\x00"
               f"\nR087\x00old name\x00{M}\x00D\x00\nweird\x00C100\x00x\x00y\x00"
               f"\x00{M}\x00{h2}\x00\x00\x00D2\x00\x00{E}\x00")
        commits = gh.parse_log(out)
        self.assertEqual(len(commits), 2)
        c = commits[0]
        self.assertEqual(c.message, "line1\n\nM\nR100\nfoo")
        self.assertEqual([(x.status, x.score, x.old_path, x.path) for x in c.changes],
                         [("R", "087", "old name", M), ("D", "", None, "\nweird"), ("C", "100", "x", "y")])
        self.assertEqual((commits[1].author_name, commits[1].message, commits[1].changes), ("", "", []))

    def test_garbage_raises(self):
        with self.assertRaises(gh.LogParseError):
            gh.parse_log("hello\x00world")


if __name__ == "__main__":
    unittest.main(verbosity=2)
