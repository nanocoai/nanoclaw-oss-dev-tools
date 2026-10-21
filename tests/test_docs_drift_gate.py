"""Release-day docs gate: stamp parsing, staleness, pull request queue, guard, baseline and CLI, all offline."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/typesafe-docs-drift/scripts/docs-drift-gate.py"
FIXTURE = ROOT / "skills/typesafe-docs-drift/fixtures/nanoclaw-sample.json"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
}


def load():
    spec = importlib.util.spec_from_file_location("docs_drift_gate_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load()


def git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=dict(os.environ, **GIT_ENV))
    if result.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), result.stderr))
    return result.stdout.strip()


def write(root, relative_path, content):
    path = Path(root) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))
    return path


def commit(root, message, body=None):
    git(root, "add", "-A")
    args = ["commit", "--quiet", "--no-gpg-sign", "-m", message]
    if body:
        args += ["-m", body]
    git(root, *args)
    return git(root, "rev-parse", "HEAD")


def snapshot(root):
    return git(root, "status", "--porcelain"), git(root, "for-each-ref"), git(root, "worktree", "list")


class Fixtures(unittest.TestCase):
    """A code repo with two releases, a merge commit, a channels branch, and a docs repo that stamps against it."""

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="docs-drift-gate-")
        base = Path(cls.temporary.name)
        cls.code = base / "nanoclaw"
        cls.docs = base / "nanoclaw-docs"
        for path in (cls.code, cls.docs):
            path.mkdir()
            git(path, "init", "--quiet", "-b", "main")
        write(cls.code, "src/a.ts", "export const a = 1;\n")
        write(cls.code, "src/b.ts", "export const b = 1;\n")
        write(cls.code, "src/c.ts", "export const c = 1;\n")
        write(cls.code, "setup/x.sh", "echo x\n")
        write(cls.code, ".claude/skills/add-x/SKILL.md", "---\nname: add-x\n---\n")
        cls.c1 = commit(cls.code, "initial")
        git(cls.code, "tag", "v1.0.0")
        write(cls.code, "src/a.ts", "export const a = 2;\n")
        cls.c2 = commit(cls.code, "fix: change a (#1101)")
        git(cls.code, "checkout", "--quiet", "-b", "feat")
        write(cls.code, "src/b.ts", "export const b = 2;\n")
        commit(cls.code, "feat: change b")
        git(cls.code, "checkout", "--quiet", "main")
        git(cls.code, "merge", "--quiet", "--no-gpg-sign", "--no-ff", "feat", "-m", "Merge pull request #1102 from x/feat", "-m", "feat: change b")
        cls.c3 = git(cls.code, "rev-parse", "HEAD")
        write(cls.code, "src/a.test.ts", "test('a');\n")
        write(cls.code, "src/d.ts", "export const d = 1;\n")
        commit(cls.code, "ci: tidy workflow (#1103)")
        write(cls.code, "setup/x.sh", "echo y\n")
        cls.c5 = commit(cls.code, "fix(setup): x says y (#1104)")
        write(cls.code, "src/c.ts", "export const c = 2;\n")
        write(cls.code, "package.json", "{}\n")
        commit(cls.code, "chore(release): v1.1.0-rc.1 (#1105)")
        git(cls.code, "tag", "v1.1.0-rc.1")
        git(cls.code, "checkout", "--quiet", "-b", "channels")
        write(cls.code, "src/channels/chan.ts", "export const chan = 1;\n")
        cls.chan1 = commit(cls.code, "feat: chan")
        write(cls.code, "src/channels/chan.ts", "export const chan = 2;\n")
        commit(cls.code, "fix: chan two")
        git(cls.code, "checkout", "--quiet", "main")
        write(cls.docs, "docs.json", json.dumps({"navigation": {"groups": [{"pages": ["a", "b", "c", "d", "e", "changelog/index"]}]}}))
        write(cls.docs, "a.mdx", "---\ntitle: A\n---\n{/* verified-against: src/a.ts, src/{b,c}.ts @ %s */}\n\n## Heading\n\nbody\n" % cls.c1[:8])
        write(cls.docs, "b.mdx", "---\ntitle: B\n---\n{/* verified-against: src/b.ts, src/a.ts @ %s (v1.0) */}\n" % cls.c3[:8])
        write(cls.docs, "c.mdx", "---\ntitle: C\n---\n{/* verified-against: src/zzz.ts @ deadbeef */}\n")
        write(cls.docs, "d.mdx", "---\ntitle: D\n---\nNo stamp here.\n")
        write(cls.docs, "e.mdx", "---\ntitle: E\n---\n{/* verified-against: setup/x.sh @ %s; channels branch src/channels/chan.ts @ %s */}\n" % (cls.c1[:8], cls.chan1[:8]))
        write(cls.docs, "changelog/index.mdx", "---\ntitle: Changelog\n---\n{/* verified-against: src/a.ts @ %s */}\n" % cls.c1[:8])
        commit(cls.docs, "docs: cover the a change (nanoclaw #1101)")
        cls.head = git(cls.code, "rev-parse", "v1.1.0-rc.1")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        patcher = patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.scratch = tempfile.TemporaryDirectory(prefix="docs-drift-gate-run-")
        self.addCleanup(self.scratch.cleanup)
        self.work = Path(self.scratch.name)

    def run_gate(self, *extra, environ=None):
        argv = ["--release", "v1.1.0-rc.1", "--code-repo", str(self.code), "--docs-repo", str(self.docs),
                "--output-dir", str(self.work / "out")] + list(extra)
        stdout, stderr = io.StringIO(), io.StringIO()
        code = gate.main(argv, dict(os.environ, **(environ or {})), stdout, stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def run_dir(self):
        return next((self.work / "out").iterdir())


class StampParsingTests(unittest.TestCase):
    def test_braces_expand_in_every_position_and_nest(self):
        self.assertEqual(gate.expand_braces("src/{a,b}.ts"), ["src/a.ts", "src/b.ts"])
        self.assertEqual(gate.expand_braces(".claude/skills/{x,y}/{SKILL.md,payload/}"),
                         [".claude/skills/x/SKILL.md", ".claude/skills/x/payload/", ".claude/skills/y/SKILL.md", ".claude/skills/y/payload/"])
        self.assertEqual(gate.expand_braces("src/{a,{b,c}}.ts"), ["src/a.ts", "src/b.ts", "src/c.ts"])
        self.assertEqual(gate.expand_braces("src/{a, b}.ts"), ["src/a.ts", "src/b.ts"])

    def test_only_path_like_words_are_kept(self):
        self.assertTrue(gate.looks_like_path("src/a.ts"))
        self.assertTrue(gate.looks_like_path("nanoclaw.sh"))
        self.assertTrue(gate.looks_like_path("container/skills/"))
        self.assertTrue(gate.looks_like_path(".claude/skills/add-x/payload/**"))
        self.assertFalse(gate.looks_like_path("listings"))
        self.assertFalse(gate.looks_like_path("v2026.10.0"))
        self.assertFalse(gate.looks_like_path("nanocoai/nanoclaw#4104"))
        self.assertFalse(gate.looks_like_path("https://example.com/a/b"))

    def test_a_page_stamp_splits_into_anchors_with_their_own_paths(self):
        body = (": src/channels/{adapter,index}.ts, src/webhook-server.ts @ 53ea1e8b; channels branch src/channels/, "
                ".claude/skills/{add-slack,slack-a2a-rooms}/SKILL.md @ d69c9c5d; Dial: .claude/skills/add-dial/SKILL.md, "
                "setup/auto.ts picker @ 475ba766, channels branch src/channels/dial.ts @ a45fab71; rerun behavior: setup/x.ts.")
        anchors, unanchored = gate.parse_stamp_body(body)
        self.assertEqual([entry["sha"] for entry in anchors], ["53ea1e8b", "d69c9c5d", "475ba766", "a45fab71"])
        self.assertEqual(anchors[0]["paths"], ["src/channels/adapter.ts", "src/channels/index.ts", "src/webhook-server.ts"])
        self.assertEqual(anchors[1]["paths"], ["src/channels/", ".claude/skills/add-slack/SKILL.md", ".claude/skills/slack-a2a-rooms/SKILL.md"])
        self.assertEqual(anchors[1]["branch"], "channels")
        self.assertIsNone(anchors[0]["branch"])
        self.assertEqual(anchors[2]["paths"], [".claude/skills/add-dial/SKILL.md", "setup/auto.ts"])
        self.assertEqual(unanchored, ["setup/x.ts"])

    def test_the_branch_word_after_the_at_sign_names_the_branch(self):
        anchors, _ = gate.parse_stamp_body(": container/agent-runner/src/providers/opencode.ts @ providers branch f503f23c")
        self.assertEqual(anchors, [{"sha": "f503f23c", "branch": "providers", "paths": ["container/agent-runner/src/providers/opencode.ts"]}])

    def test_a_word_before_the_sha_never_lets_paths_bleed_into_the_next_anchor(self):
        anchors, _ = gate.parse_stamp_body(": src/a.ts @ trunk abcdef12; src/b.ts @ providers branch 12345678; "
                                           "install/x.sh @ glifocat/other-repo 6f908859")
        self.assertEqual([(a["sha"], a["branch"], a["paths"]) for a in anchors], [
            ("abcdef12", None, ["src/a.ts"]), ("12345678", "providers", ["src/b.ts"]), ("6f908859", None, ["install/x.sh"])])

    def test_question_mark_globs_survive_parsing_and_match(self):
        self.assertEqual(gate.segment_paths("src/a?.ts, src/b*.ts"), ["src/a?.ts", "src/b*.ts"])
        self.assertTrue(gate.path_matcher("src/a?.ts")("src/ax.ts"))
        self.assertFalse(gate.path_matcher("src/a?.ts")("src/a/b.ts"))

    def test_scoped_stamps_trailing_parentheses_and_descriptions_are_handled(self):
        text = textwrap.dedent("""\
            ---
            title: T
            ---
            {/* verified-against: src/a.ts @ 7203e00d (v2026.10.0) */}
            {/* verified-against (scoped correction) — wrap nudge, retry guard: container/poll-loop.ts @ 15c6124f */}
            {/* verified-against (scoped correction): isConnected: src/channels/adapter.ts, src/cli/commands/status.ts @ 2594b86a (nanocoai/nanoclaw#4104) */}
            """)
        stamps = gate.parse_stamps(text)
        self.assertEqual([stamp["scope"] for stamp in stamps], ["page", "scoped", "scoped"])
        self.assertEqual([stamp["anchors"][0]["sha"] for stamp in stamps], ["7203e00d", "15c6124f", "2594b86a"])
        self.assertEqual(stamps[1]["anchors"][0]["paths"], ["container/poll-loop.ts"])
        self.assertEqual(stamps[2]["anchors"][0]["paths"], ["src/channels/adapter.ts", "src/cli/commands/status.ts"])
        self.assertEqual(stamps[0]["line"], 4)

    def test_matchers_accept_a_file_a_parent_directory_and_a_glob(self):
        self.assertTrue(gate.path_matcher("src/a.ts")("src/a.ts"))
        self.assertFalse(gate.path_matcher("src/a.ts")("src/a.tsx"))
        self.assertTrue(gate.path_matcher("src/channels/")("src/channels/x/y.ts"))
        self.assertTrue(gate.path_matcher("src/channels")("src/channels/x.ts"))
        self.assertFalse(gate.path_matcher("src/channels")("src/channels2/x.ts"))
        self.assertTrue(gate.path_matcher(".claude/skills/add-x/payload/**")(".claude/skills/add-x/payload/a/b.ts"))
        self.assertTrue(gate.path_matcher("src/*.ts")("src/a.ts"))
        self.assertFalse(gate.path_matcher("src/*.ts")("src/x/a.ts"))


class GitHistoryTests(Fixtures):
    def clone(self):
        repo, sha = gate.clone_at(self.code, "v1.1.0-rc.1", self.work / "code")
        return repo, sha

    def test_clone_resolves_a_tag_and_checks_it_out_detached(self):
        repo, sha = self.clone()
        self.assertEqual(sha, self.head)
        self.assertEqual(git(repo.path, "rev-parse", "HEAD"), self.head)
        self.assertEqual(git(repo.path, "branch", "--show-current"), "")
        with self.assertRaises(gate.GateError):
            gate.clone_at(self.code, "no-such-ref", self.work / "other")

    def test_the_source_repository_is_never_changed(self):
        before = snapshot(self.code), snapshot(self.docs)
        code, _out, _err = self.run_gate("--no-model")
        self.assertEqual(code, 0)
        self.assertEqual((snapshot(self.code), snapshot(self.docs)), before)

    def test_previous_stable_skips_prereleases_and_the_release_itself(self):
        repo, sha = self.clone()
        self.assertEqual(gate.previous_stable(repo, sha), "v1.0.0")

    def test_release_prs_read_squash_and_merge_commits_and_drop_test_files(self):
        repo, sha = self.clone()
        prs = gate.release_prs(repo, repo.commit("v1.0.0"), sha)
        numbers = [pr["number"] for pr in prs]
        self.assertEqual(numbers, [1105, 1104, 1103, 1102, 1101])
        by_number = {pr["number"]: pr for pr in prs}
        self.assertEqual(by_number[1102]["title"], "feat: change b")
        self.assertEqual(by_number[1102]["files"], ["src/b.ts"])
        self.assertEqual(by_number[1103]["files"], ["src/d.ts"])  # src/a.test.ts is dropped
        self.assertEqual(by_number[1101]["files"], ["src/a.ts"])

    def test_docs_mentions_read_history_and_pages(self):
        docs_repo, _ = gate.clone_at(self.docs, "main", self.work / "docs")
        self.assertEqual(gate.docs_mentions(docs_repo, docs_repo.path), {1101})  # from the commit message
        write(docs_repo.path, "z.mdx", "See nanocoai/nanoclaw#4104, #4105, docs PR #12 and colour #123456.\n")
        self.assertEqual(gate.docs_mentions(docs_repo, docs_repo.path), {1101, 4104, 4105})  # short numbers are the docs' own

    def test_an_anchor_is_current_changed_unknown_or_ahead(self):
        repo, sha = self.clone()
        stale = gate.evaluate_page(repo, "a", (self.docs / "a.mdx").read_text(), sha)
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["anchors"][0]["changed"], ["src/a.ts", "src/b.ts", "src/c.ts"])
        current = gate.evaluate_page(repo, "b", (self.docs / "b.mdx").read_text(), sha)
        self.assertEqual(current["status"], "current")  # only a test file, d.ts, c.ts and setup/x.sh changed after the merge
        self.assertEqual(current["changed_files"], [])
        unknown = gate.evaluate_page(repo, "c", (self.docs / "c.mdx").read_text(), sha)
        self.assertEqual(unknown["status"], "unknown-anchor")
        unstamped = gate.evaluate_page(repo, "d", (self.docs / "d.mdx").read_text(), sha)
        self.assertEqual(unstamped["status"], "unstamped")
        older = gate.evaluate_page(repo, "a2", "{/* verified-against: src/a.ts @ %s */}" % sha[:8], repo.commit("v1.0.0"))
        self.assertEqual(older["anchors"][0]["status"], "ahead")
        self.assertEqual(older["status"], "current")

    def test_a_channels_anchor_is_checked_against_the_channels_branch(self):
        repo, sha = self.clone()
        page = gate.evaluate_page(repo, "e", (self.docs / "e.mdx").read_text(), sha)
        statuses = {entry["sha"]: (entry["status"], entry["ref"]) for entry in page["anchors"]}
        self.assertEqual(statuses[self.c1[:8]][0], "changed")
        self.assertEqual(statuses[self.chan1[:8]], ("changed", "origin/channels"))
        self.assertEqual(page["anchors"][1]["changed"], ["src/channels/chan.ts"])

    def test_only_version_shaped_tags_count_as_stable_releases(self):
        repo, sha = self.clone()
        git(repo.path, "tag", "checkpoint", sha + "~1")
        self.assertEqual(gate.previous_stable(repo, sha), "v1.0.0")

    def test_a_citation_with_no_anchor_still_feeds_the_queue(self):
        repo, sha = self.clone()
        page = gate.evaluate_page(repo, "u", "{/* verified-against: src/a.ts @ %s; setup/x.sh */}" % sha[:8], sha)
        self.assertEqual(page["unanchored"], ["setup/x.sh"])
        prs = gate.release_prs(repo, repo.commit("v1.0.0"), sha)
        items = gate.build_queue(prs, [page], mentioned=set(), repo=repo)
        self.assertEqual([item["number"] for item in items], [1104])  # src/a.ts is stamped at the head, which holds #1101

    def test_a_page_stamped_after_a_change_is_not_hit_by_it(self):
        repo, sha = self.clone()
        page = gate.evaluate_page(repo, "late", "{/* verified-against: src/a.ts, src/b.ts, src/c.ts, setup/x.sh @ %s */}" % sha[:8], sha)
        prs = gate.release_prs(repo, repo.commit("v1.0.0"), sha)
        self.assertEqual(gate.build_queue(prs, [page], mentioned=set(), repo=repo), [])
        self.assertEqual(len(gate.build_queue(prs, [page], mentioned=set())), 4)  # without history every change would count

    def test_docs_mentions_read_markdown_pages_and_never_follow_a_link_out_of_the_clone(self):
        docs_repo, _ = gate.clone_at(self.docs, "main", self.work / "docs")
        write(self.work, "outside.mdx", "Secret mention #7777.\n")
        (Path(docs_repo.path) / "linked.mdx").symlink_to(self.work / "outside.mdx")
        write(docs_repo.path, "plain.md", "Mentions #4321.\n")
        self.assertEqual(gate.docs_mentions(docs_repo, docs_repo.path), {1101, 4321})

    def test_the_detector_reads_the_commit_of_the_checkout_it_was_given(self):
        expected = git(self.code, "rev-parse", "--short", "HEAD")
        with patch.dict(os.environ, {"GIT_DIR": str(self.docs / ".git")}):
            self.assertEqual(gate.DETECTOR.git_commit(self.code), expected)

    def test_a_failed_git_call_is_an_error_not_an_empty_answer(self):
        real = subprocess.run

        def fail_diffs(command, *args, **kwargs):
            if command[:2] == ["git", "diff"] and any(str(part).endswith("^1") for part in command):
                return subprocess.CompletedProcess(command, 128, stdout="", stderr="fatal: bad object")
            return real(command, *args, **kwargs)

        with patch.object(gate.subprocess, "run", side_effect=fail_diffs):
            code, _out, err = self.run_gate("--no-model", "--strict", "--max-undocumented", "0")
        self.assertEqual(code, 2)
        self.assertIn("git diff failed: fatal: bad object", err)

    def test_the_guard_reads_the_page_title_like_the_model_does(self):
        docs, _ = gate.clone_at(self.docs, "main", self.work / "docs")
        texts = gate.section_texts(gate.DETECTOR, docs.path)
        self.assertTrue(texts["a#heading"].startswith("A\nHeading\n"))

    def test_git_errors_and_timeouts_never_carry_url_credentials(self):
        url = "https://user:secret@example.com/org/repo.git"
        timeout = subprocess.TimeoutExpired(cmd=["git", "clone", url], timeout=1)
        with patch.object(gate.subprocess, "run", side_effect=timeout):
            with self.assertRaises(gate.GateError) as caught:
                gate.run_git(["clone", url, "x"])
        self.assertNotIn("secret", str(caught.exception))
        failed = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="fatal: unable to access '%s'" % url)
        with patch.object(gate.subprocess, "run", return_value=failed):
            with self.assertRaises(gate.GateError) as caught:
                gate.run_git(["clone", url, "x"])
        self.assertNotIn("secret", str(caught.exception))
        self.assertIn("example.com/org/repo.git", str(caught.exception))
        long_url = "https://user:" + "S" * 100 + "@example.com/x.git"
        failed = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="a" * 250 + " " + long_url)
        with patch.object(gate.subprocess, "run", return_value=failed):
            with self.assertRaises(gate.GateError) as caught:
                gate.run_git(["clone", long_url, "x"])
        self.assertNotIn("SSSS", str(caught.exception))  # redacted before the message is cut to length

    def test_the_queue_lists_undocumented_prs_by_the_files_pages_cite(self):
        repo, sha = self.clone()
        pages = gate.evaluate_pages(gate.load_detector(), repo, self.docs, sha)
        names = [page["page"] for page in pages]
        self.assertNotIn("changelog/index", names)
        prs = gate.release_prs(repo, repo.commit("v1.0.0"), sha)
        items = gate.build_queue(prs, pages, mentioned={1101}, repo=repo)
        by_number = {item["number"]: item for item in items}
        self.assertTrue(by_number[1101]["documented"])
        self.assertFalse(by_number[1102]["documented"])
        self.assertEqual(by_number[1102]["pages"], ["a"])  # b is stamped at the merge commit itself, so it was verified with #1102 in place
        self.assertEqual(by_number[1102]["hits"]["a"], ["src/b.ts"])
        self.assertEqual(by_number[1101]["pages"], ["a"])
        self.assertNotIn(1103, by_number)  # src/d.ts is cited by no page
        self.assertEqual(by_number[1104]["pages"], ["e"])
        self.assertTrue(by_number[1105]["internal"])
        self.assertEqual(by_number[1105]["title"], "chore(release): v1.1.0-rc.1")
        self.assertEqual([item["number"] for item in items], [1104, 1102, 1105, 1101])  # open work, then internal, then documented


class TitleTests(unittest.TestCase):
    def test_the_number_is_the_one_the_commit_carries(self):
        self.assertEqual(gate.pr_number('Revert "feat: change API (#1101)" (#1102)'), 1102)
        self.assertEqual(gate.pr_number("Merge pull request #1103 from x/y"), 1103)
        self.assertEqual(gate.pr_number("fix: a (#1104)"), 1104)
        self.assertIsNone(gate.pr_number("direct push without a number"))
        self.assertIsNone(gate.pr_number('Revert "feat: change a (#1101)"'))  # a plain git revert carries no number of its own

    def test_only_plain_ci_test_build_style_and_release_chores_are_internal(self):
        for title in ("ci: pin runners", "test(setup): fixtures", "build(deps-dev): bump vitest", "style: format",
                      "chore(release): v1.2.3", "chore(deps): bump x"):
            self.assertTrue(gate.is_internal(title), title)
        for title in ("build!: require Node 24", "feat!: drop the old flag", "fix(ci): wrong exit code", "chore(skills): bump the pin",
                      "feat(setup): wait longer"):
            self.assertFalse(gate.is_internal(title), title)


class RowTests(unittest.TestCase):
    def pair(self, section, page="p", verdict="DRIFT", contradicts=0.9):
        return {"section": {"id": section, "page": page, "heading": "h", "line": 4},
                "decision": {"verdict": verdict, "contradicts": contradicts, "covers": 0.9, "staleness": 0.5}}

    def row(self, fact, verdict, section="p#s", page="p", terms=("TERM_X",), contradicts=0.9):
        pair = self.pair(section, page, verdict, contradicts)
        return {"id": fact, "area": "env", "verdict": verdict, "statement": "claim %s" % fact, "terms": list(terms),
                "evidence": {"file": "src/x.ts", "line": 3, "excerpt": "x"}, "note": "", "pairs": [pair],
                "best": {"section": pair["section"], "decision": pair["decision"]}}

    def test_the_guard_demotes_a_drift_row_whose_section_never_mentions_its_terms(self):
        rows = [self.row("env:A", "DRIFT"), self.row("env:B", "DRIFT", section="q#t", terms=("TERM_B", "ncl")),
                self.row("env:C", "UNSURE", section="r#u"), self.row("env:D", "DRIFT", section="gone#x"),
                self.row("ncl:E", "DRIFT", section="v#w", terms=("ncl",))]
        text = {"p#s": "mentions term_x in lower case", "q#t": "talks about something else", "r#u": "nothing", "v#w": "nothing"}
        self.assertEqual(gate.apply_guard(rows, text), 1)
        verdicts = {row["id"]: row["verdict"] for row in rows}
        self.assertEqual(verdicts, {"env:A": "DRIFT", "env:B": "UNSURE", "env:C": "UNSURE", "env:D": "DRIFT", "ncl:E": "DRIFT"})
        self.assertIn("`TERM_B`", rows[1]["guard"])
        self.assertEqual(gate.apply_guard([self.row("env:F", "DRIFT")], {}), 0)

    def test_the_guard_moves_the_best_pair_to_a_section_that_names_the_fact(self):
        row = self.row("env:A", "DRIFT", section="unrelated#x", contradicts=0.99)
        row["pairs"].append(self.pair("real#y", verdict="DRIFT", contradicts=0.9))
        row["pairs"].append(self.pair("other#z", verdict="OK", contradicts=0.1))
        text = {"unrelated#x": "nothing here", "real#y": "the TERM_X setting", "other#z": "TERM_X again"}
        self.assertEqual(gate.apply_guard([row], text), 0)
        self.assertEqual(row["verdict"], "DRIFT")
        self.assertEqual(row["best"]["section"]["id"], "real#y")
        self.assertEqual(row["best"]["decision"]["contradicts"], 0.9)

    def test_the_open_section_becomes_the_best_pair_when_another_is_held(self):
        row = self.row("env:A", "DRIFT", section="a#x", page="pa", contradicts=0.99)
        row["pairs"].append(self.pair("b#y", page="pb", verdict="DRIFT", contradicts=0.9))
        new, known = gate.classify_rows([dict(row)], [{"fact": "env:A", "section": "a#x", "status": "held"}])
        self.assertEqual((len(new), len(known)), (1, 0))
        self.assertEqual(new[0]["best"]["section"]["id"], "b#y")
        self.assertEqual(new[0]["best"]["section"]["page"], "pb")

    def test_a_pair_the_guard_rejected_needs_no_baseline_entry(self):
        row = self.row("env:A", "DRIFT", section="a#x", contradicts=0.99)
        row["pairs"].append(self.pair("b#y", verdict="DRIFT", contradicts=0.9))
        self.assertEqual(gate.apply_guard([row], {"a#x": "unrelated", "b#y": "the TERM_X setting"}), 0)
        self.assertTrue(row["pairs"][0].get("guarded"))
        new, known = gate.classify_rows([row], [{"fact": "env:A", "section": "b#y", "status": "held"}])
        self.assertEqual((len(new), len(known)), (0, 1))

    def test_a_section_entry_does_not_hide_a_second_drifting_section(self):
        row = self.row("env:A", "DRIFT", section="a#x", contradicts=0.99)
        row["pairs"].append(self.pair("b#y", verdict="DRIFT", contradicts=0.9))
        only_a = [{"fact": "env:A", "section": "a#x", "status": "held"}]
        new, known = gate.classify_rows([dict(row)], only_a)
        self.assertEqual((len(new), len(known)), (1, 0))
        both = only_a + [{"fact": "env:A", "section": "b#y", "status": "held"}]
        new, known = gate.classify_rows([dict(row)], both)
        self.assertEqual((len(new), len(known)), (0, 1))
        new, known = gate.classify_rows([dict(row)], [{"fact": "env:A", "status": "accepted"}])
        self.assertEqual((len(new), len(known)), (0, 1))

    def test_the_baseline_holds_decided_rows_by_fact_and_optionally_by_section(self):
        entries = [{"fact": "env:A", "status": "false-positive", "reason": "extractor read a raw default"},
                   {"fact": "env:B", "section": "other#s", "status": "held"}]
        rows = [self.row("env:A", "DRIFT"), self.row("env:B", "DRIFT"), self.row("env:C", "OK"), self.row("env:D", "MISSING")]
        new, known = gate.classify_rows(rows, entries)
        self.assertEqual([row["id"] for row in new], ["env:B", "env:D"])
        self.assertEqual([row["id"] for row in known], ["env:A"])
        self.assertEqual(known[0]["baseline"]["status"], "false-positive")
        self.assertNotIn("pairs", new[0])

    def test_a_malformed_baseline_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            for content in ('{"entries": [{"fact": "x", "status": "maybe"}]}', '{"entries": "x"}', "not json", '{"entries": [{"status": "held"}]}'):
                path.write_text(content)
                with self.assertRaises(gate.GateError):
                    gate.load_baseline(path)
            path.write_text('{"entries": [{"fact": "x", "status": "held"}]}')
            self.assertEqual(len(gate.load_baseline(path)), 1)
            self.assertEqual(gate.load_baseline(None), [])


class CommandTests(Fixtures):
    def test_stamps_and_queue_without_a_model_write_a_digest_json_and_briefs(self):
        code, out, err = self.run_gate("--no-model")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Pull requests: 5 merged in v1.0.0..v1.1.0-rc.1", out)
        run = self.run_dir()
        data = json.loads((run / "gate.json").read_text())
        self.assertEqual(data["release"]["sha"], self.head)
        self.assertEqual(data["prs"]["base"], "v1.0.0")
        self.assertEqual((data["prs"]["total"], data["prs"]["touching"], data["prs"]["documented"], data["prs"]["internal"], data["prs"]["undocumented"]),
                         (5, 4, 1, 1, 2))
        self.assertEqual(data["detector"]["mode"], "skipped")
        digest = (run / "digest.md").read_text()
        self.assertIn("| #1102 | feat: change b |", digest)
        self.assertNotIn("| #1101 |", digest)
        self.assertIn("Detector: skipped (--no-model)", digest)
        self.assertIn("`deadbeef` (unknown-anchor)", digest)
        briefs = sorted(path.name for path in (run / "briefs").iterdir())
        self.assertEqual(briefs[0].split("-", 1)[0], "01")
        brief = (run / "briefs" / briefs[0]).read_text()
        self.assertIn("Draft only: do not push", brief)
        self.assertIn("Cited files changed: `src/b.ts`", brief)

    def test_a_directory_that_exists_is_never_cloned_into_or_removed(self):
        workdir = self.work / "shared"
        (workdir / "docs").mkdir(parents=True)
        (workdir / "docs" / "other-run.txt").write_text("another run's clone")
        code, _out, err = self.run_gate("--no-model", "--workdir", str(workdir))
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual((workdir / "docs" / "other-run.txt").read_text(), "another run's clone")
        self.assertFalse((workdir / "code").exists())  # the one directory this run did create is cleaned up

    def test_a_fatal_git_error_while_resolving_a_ref_is_not_a_missing_ref(self):
        real = subprocess.run

        def fail(command, *args, **kwargs):
            if command[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(command, 128, stdout="", stderr="fatal: unable to read tree")
            return real(command, *args, **kwargs)

        with patch.object(gate.subprocess, "run", side_effect=fail):
            code, _out, err = self.run_gate("--no-model", "--strict", "--max-undocumented", "0")
        self.assertEqual(code, 2)
        self.assertIn("git rev-parse failed: fatal: unable to read tree", err)

    def test_an_ambiguous_short_sha_reads_as_an_unknown_anchor_not_a_failure(self):
        repo, _sha = gate.clone_at(self.code, "v1.1.0-rc.1", self.work / "code")
        ambiguous = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="error: short object ID abc1234 is ambiguous")
        with patch.object(gate.subprocess, "run", return_value=ambiguous):
            self.assertIsNone(repo.commit("abc1234"))

    def test_scratch_clones_are_removed_unless_kept(self):
        before = set(Path(tempfile.gettempdir()).glob("docs-drift-gate-*"))
        self.run_gate("--no-model")
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("docs-drift-gate-*")), before)
        workdir = self.work / "kept"
        code, _out, err = self.run_gate("--no-model", "--workdir", str(workdir), "--keep")
        self.assertEqual(code, 0)
        self.assertTrue((workdir / "code/.git").exists() and (workdir / "docs/.git").exists())
        self.assertIn("scratch clones kept", err)
        code, _out, err = self.run_gate("--no-model", "--workdir", str(workdir))
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertTrue((workdir / "code/.git").exists())  # an existing directory is never removed

    def test_a_given_workdir_survives_a_normal_run_but_loses_its_clones(self):
        workdir = self.work / "mine"
        workdir.mkdir()
        (workdir / "notes.txt").write_text("keep me")
        code, _out, _err = self.run_gate("--no-model", "--workdir", str(workdir))
        self.assertEqual(code, 0)
        self.assertTrue((workdir / "notes.txt").exists())
        self.assertFalse((workdir / "code").exists())

    def test_without_a_key_or_fixture_the_run_stops_before_cloning(self):
        with patch.dict(os.environ):
            os.environ.pop("TYPESAFE_API_KEY", None)
            stdout, stderr = io.StringIO(), io.StringIO()
            code = gate.main(["--release", "v1.1.0-rc.1", "--code-repo", str(self.code), "--docs-repo", str(self.docs),
                              "--output-dir", str(self.work / "out")], dict(os.environ), stdout, stderr)
        self.assertEqual(code, 2)
        self.assertIn("TYPESAFE_API_KEY is not set", stderr.getvalue())
        self.assertFalse((self.work / "out").exists())

    def test_the_fixture_replays_detector_rows_through_the_gate(self):
        code, out, err = self.run_gate("--fixture", str(FIXTURE), environ={"TYPESAFE_API_KEY": ""})
        self.assertEqual((code, err), (0, ""))
        data = json.loads((self.run_dir() / "gate.json").read_text())
        self.assertEqual(data["detector"]["mode"], "fixture")
        self.assertEqual(data["counts"]["new_drift"], 1)
        self.assertEqual(data["detector"]["new"][0]["id"], "ncl:groups-config-update-flags")
        self.assertNotIn("pairs", data["detector"]["new"][0])
        self.assertIn("## New DRIFT candidates", (self.run_dir() / "digest.md").read_text())
        self.assertIn("Detector: fixture, 6 facts: new DRIFT 1, MISSING 2, UNSURE 1, held back 0.", out)

    def test_strict_exits_one_on_new_drift_and_the_baseline_clears_it(self):
        code, _out, _err = self.run_gate("--fixture", str(FIXTURE), "--strict", environ={"TYPESAFE_API_KEY": ""})
        self.assertEqual(code, 1)
        baseline = self.work / "baseline.json"
        baseline.write_text(json.dumps({"entries": [{"fact": "ncl:groups-config-update-flags", "status": "false-positive", "reason": "decided"}]}))
        code, out, _err = self.run_gate("--fixture", str(FIXTURE), "--strict", "--baseline", str(baseline), environ={"TYPESAFE_API_KEY": ""})
        self.assertEqual(code, 0)
        self.assertIn("held back 1", out)

    def test_max_undocumented_gates_the_queue_under_strict(self):
        self.assertEqual(self.run_gate("--no-model", "--strict", "--max-undocumented", "5")[0], 0)
        self.assertEqual(self.run_gate("--no-model", "--strict", "--max-undocumented", "1")[0], 1)
        self.assertEqual(self.run_gate("--no-model", "--max-undocumented", "0")[0], 0)  # not strict: reported only

    def test_max_briefs_limits_pages_but_not_the_json(self):
        self.run_gate("--no-model", "--max-briefs", "1")
        run = self.run_dir()
        self.assertEqual(len(list((run / "briefs").iterdir())), 1)
        self.assertGreater(len(json.loads((run / "gate.json").read_text())["pages"]), 1)

    def test_bad_refs_are_reported_without_a_traceback(self):
        code, _out, err = self.run_gate("--no-model", "--since", "no-such-tag")
        self.assertEqual(code, 2)
        self.assertIn("--since 'no-such-tag' not found", err)
        stdout, stderr = io.StringIO(), io.StringIO()
        code = gate.main(["--release", "nope", "--code-repo", str(self.code), "--docs-repo", str(self.docs), "--no-model",
                          "--output-dir", str(self.work / "o2")], dict(os.environ), stdout, stderr)
        self.assertEqual(code, 2)
        self.assertIn("ref 'nope' not found", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_credentials_in_a_repository_url_are_never_shown(self):
        self.assertEqual(gate.public("https://user:secret@github.com/a/b.git"), "https://github.com/a/b.git")
        self.assertEqual(gate.public("https://token@github.com/a/b.git"), "https://github.com/a/b.git")
        self.assertEqual(gate.public("/local/path"), "/local/path")
        self.assertEqual(gate.public("git@github.com:a/b.git"), "git@github.com:a/b.git")

    def test_inherited_git_location_variables_cannot_reach_the_source(self):
        before = snapshot(self.code)
        with patch.dict(os.environ, {"GIT_INDEX_FILE": str(self.code / ".git" / "index"), "GIT_DIR": str(self.code / ".git")}):
            code, _out, _err = self.run_gate("--no-model", "--release", "v1.0.0")
        self.assertEqual(code, 0)
        self.assertEqual(snapshot(self.code), before)

    def test_since_must_be_an_ancestor_of_the_release(self):
        code, _out, err = self.run_gate("--no-model", "--since", "channels")
        self.assertEqual(code, 2)
        self.assertIn("is not an ancestor of v1.1.0-rc.1", err)

    def test_an_unusable_workdir_is_an_operational_error_not_a_traceback(self):
        code, _out, err = self.run_gate("--no-model", "--workdir", "/dev/null")
        self.assertEqual(code, 2)
        self.assertIn("cannot create the scratch directory", err)
        self.assertNotIn("Traceback", err)

    def test_a_partial_detector_run_is_shown_and_fails_a_strict_gate(self):
        partial = {"mode": "live", "results": [], "summary": {"facts": 3}, "partial": "2 request(s) failed"}
        with patch.object(gate, "run_detector", return_value=partial):
            code, out, _err = self.run_gate(environ={"TYPESAFE_API_KEY": "x" * 12})
            self.assertEqual(code, 0)
            self.assertIn("Detector run was PARTIAL: 2 request(s) failed", out)
            run = self.run_dir()
            self.assertEqual(json.loads((run / "gate.json").read_text())["detector"]["partial"], "2 request(s) failed")
            self.assertIn("Detector run was partial", (run / "digest.md").read_text())
            code, _out, err = self.run_gate("--strict", environ={"TYPESAFE_API_KEY": "x" * 12})
            self.assertEqual(code, 2)
            self.assertIn("detector run was partial", err)

    def test_a_ref_that_looks_like_an_option_is_refused(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = gate.main(["--release=--upload-pack=x", "--code-repo", str(self.code), "--docs-repo", str(self.docs),
                          "--no-model", "--output-dir", str(self.work / "o4")], dict(os.environ), stdout, stderr)
        self.assertEqual(code, 2)
        self.assertIn("looks like an option", stderr.getvalue())

    def test_an_unexpected_failure_is_one_line_and_never_carries_the_key(self):
        key = "tsk_" + "a1b2c3d4" * 4
        with patch.object(gate, "evaluate_pages", side_effect=RuntimeError("boom with %s inside" % key)):
            code, _out, err = self.run_gate("--no-model", environ={"TYPESAFE_API_KEY": key})
        self.assertEqual(code, 2)
        self.assertIn("error: unexpected RuntimeError: boom with [redacted] inside", err)
        self.assertNotIn(key, err)
        self.assertNotIn("Traceback", err)

    def test_a_page_that_points_outside_the_docs_clone_is_skipped(self):
        docs = self.work / "docs"
        write(docs, "docs.json", json.dumps({"navigation": ["inside", "../outside"]}))
        write(docs, "inside.mdx", "---\ntitle: In\n---\n")
        write(self.work, "outside.mdx", "---\ntitle: Out\n---\n")
        self.assertEqual([page for page, _path in gate.page_files(gate.load_detector(), docs)], ["inside"])

    def test_json_flag_prints_the_payload(self):
        code, out, _err = self.run_gate("--no-model", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["release"]["ref"], "v1.1.0-rc.1")

    def test_no_earlier_stable_tag_skips_the_queue_with_a_hint(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = gate.main(["--release", "v1.0.0", "--code-repo", str(self.code), "--docs-repo", str(self.docs), "--no-model",
                          "--output-dir", str(self.work / "o3")], dict(os.environ), stdout, stderr)
        self.assertEqual(code, 0)
        self.assertIn("skipped (no earlier stable tag; pass --since)", stdout.getvalue())
        digest = next((self.work / "o3").iterdir()) / "digest.md"
        self.assertIn("Pass --since <tag>", digest.read_text())


if __name__ == "__main__":
    unittest.main()
