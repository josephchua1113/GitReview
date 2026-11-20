"""Tests for git_history.py.  Run:  python -m unittest -v test_git_history

Every test builds real temporary repositories, each with a bare "remote", and
calls git_history.generate_report() with test values (the configuration block in
git_history.py is never edited).
"""

import contextlib
import html.parser
import io
import os
import shutil
import subprocess
import tempfile
import unittest
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
        if tag == "details" and "file" in classes:
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
        # forced tie for the closest lower version
        r = Repo(self.repos, "delta", self.remotes)
        r.write("x.txt", "1\n")
        r.commit("delta one")
        r.push("release/SG_19.10.0")
        r.push("release/SG_19.17.0")
        r.push("hotfix/SG_19.17")      # equals 19.17.0 numerically
        r.write("x.txt", "2\n")
        r.commit("delta two")
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

def run_report(repos, target=TARGET, template=None, out=None):
    logs = []
    out = out or os.path.join(tempfile.mkdtemp(prefix="ghout-"), "index.html")
    rep = gh.generate_report(repos, target, "origin", out, template, log=logs.append)
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
        self.assertEqual(gh.version_key("19.17"), gh.version_key("19.17.0"))


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
        self.assertEqual(sorted(self.by_name), ["alpha", "beta", "delta", "epsilon", "gamma", "zeta"])
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

    def test_tie_is_error(self):
        d = self.by_name["delta"]
        self.assertEqual(d.state, gh.STATE_ERROR)
        self.assertIsNone(d.base_branch)
        self.assertIn("`origin/hotfix/SG_19.17`", d.reason)
        self.assertIn("`origin/release/SG_19.17.0`", d.reason)
        self.assertNotIn("delta two", self.page)

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
        self.assertEqual({e["repo"] for e in self.ex.entries}, {"repo/alpha"})

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
        self.assertEqual(self.page.count("Links aren’t available"), 1)

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

    def test_output_file_written(self):
        with open(self.report.config.output_file, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), self.page)


class LinkIntegrationTests(unittest.TestCase):
    """Links rendered on the page for each platform (remote URL substituted)."""

    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cleanup()

    def render_with_remote(self, url, template=None):
        real = gh.run_git

        def fake(repo, args):
            if list(args[:2]) == ["remote", "get-url"]:
                return url + "\n"
            return real(repo, args)
        with mock.patch.object(gh, "run_git", side_effect=fake):
            rep, _ = run_report(self.fx.repos, template=template)
        return rep, extract(rep.html)

    def check(self, url, expect_commit, expect_compare, template=None):
        rep, ex = self.render_with_remote(url, template)
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
        a = next(r for r in rep.repos if r.name == "alpha")
        self.assertEqual(len(compare_links), len(a.files))
        self.assertEqual(len(commit_links), sum(len(v) for _, v in a.files) + len(a.no_file_commits))
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
                                     f"release%2FSG_19.18.0#diff-{sha}"))
                self.assertFalse(any("tok" in l[1] for l in ex.links))

    def test_gitlab(self):
        self.check("git@gitlab.com:grp/sub/alpha.git",
                   lambda c, p: f"https://gitlab.com/grp/sub/alpha/-/commit/{c}" + (f"#{gh._sha1(p)}" if p else ""),
                   lambda b, t, p: ("https://gitlab.com/grp/sub/alpha/-/compare/hotfix%2FSG_19.17.1..."
                                    f"release%2FSG_19.18.0#{gh._sha1(p)}"))

    def test_bitbucket(self):
        self.check("git@bitbucket.org:ws/alpha.git",
                   lambda c, p: f"https://bitbucket.org/ws/alpha/commits/{c}" + (f"#chg-{p}" if p else ""),
                   lambda b, t, p: ("https://bitbucket.org/ws/alpha/branches/compare/"
                                    f"release%2FSG_19.18.0%0Dhotfix%2FSG_19.17.1#chg-{p}"))

    def test_azure(self):
        for url in ("git@ssh.dev.azure.com:v3/org/proj/alpha", "https://org@dev.azure.com/org/proj/_git/alpha"):
            with self.subTest(url=url):
                self.check(url,
                           lambda c, p: f"https://dev.azure.com/org/proj/_git/alpha/commit/{c}",
                           lambda b, t, p: ("https://dev.azure.com/org/proj/_git/alpha/branches?"
                                            "baseVersion=GBhotfix%2FSG_19.17.1&targetVersion=GBrelease%2FSG_19.18.0"
                                            "&_a=files"))

    def test_template_for_self_hosted(self):
        tpl = {"commit": "{base_url}/c/{commit}?p={path}#{path_sha256}",
               "compare": "{base_url}/cmp/{base}..{target}?p={path}"}
        self.check("git@git.corp.example:team/alpha.git",
                   lambda c, p: f"https://git.corp.example/team/alpha/c/{c}?p={p or ''}#{gh._sha256(p) if p else ''}",
                   lambda b, t, p: ("https://git.corp.example/team/alpha/cmp/hotfix%2FSG_19.17.1.."
                                    f"release%2FSG_19.18.0?p={p}"),
                   template=tpl)

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
