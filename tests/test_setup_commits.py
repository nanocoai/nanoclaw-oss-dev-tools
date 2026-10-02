"""HEAD may move past the tested commit only by NanoClaw setup's own commits."""

import importlib.util
import inspect
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
COPIES = {
    "e2e-evidence": ROOT / "skills/e2e-exe-dev/scripts/e2e-evidence.py",
    "macos-service": ROOT / "skills/e2e-macos/scripts/macos-service.py",
}
IDENTITY_VARIABLES = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                      "GIT_COMMITTER_EMAIL", "EMAIL")
SETUP = ("NanoClaw setup", "setup@nanoclaw.invalid")
OPERATOR = ("Operator", "operator@example.invalid")
OTHER = ("Other", "other@example.invalid")
CURRENT = object()  # accepted() default: the tested commit, or the checkout's HEAD
SIGNATURE = b"gpgsig -----BEGIN SSH SIGNATURE-----\n U1NIU0lHAAAAAQ==\n -----END SSH SIGNATURE-----\n"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_setup_commits", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULES = {name: load(name, path) for name, path in COPIES.items()}


class SetupCommitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="setup-commits-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        hooks = self.root / ".hooks"
        hooks.mkdir()
        # Only the repository's own config: no signing prompt, hooks or identity.
        self.env = {key: value for key, value in os.environ.items() if key not in IDENTITY_VARIABLES}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(hooks))
        self.base = self.commit("base", "chore: base", OPERATOR)
        self.tested = self.commit("tested", "feat: tested change", OPERATOR)

    def git(self, *args, env=None):
        run = subprocess.run(["git", *args], cwd=self.root, env=env or self.env,
                             text=True, capture_output=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.strip()

    def commit(self, name, message, author=None, committer=None):
        """Commit one new file; author/committer default to the checkout's configured identity."""
        env = dict(self.env)
        for role, identity in (("AUTHOR", author), ("COMMITTER", committer or author)):
            if identity:
                env["GIT_" + role + "_NAME"], env["GIT_" + role + "_EMAIL"] = identity
        (self.root / name).write_text(name + "\n")
        self.git("add", name, env=env)
        self.git("commit", "-q", "-m", message, env=env)
        return self.git("rev-parse", "HEAD")

    def raw(self, message, extra=b"", tree=None, stamp=b"1790000000 +0000"):
        """Write a setup-identity commit on HEAD by hand, for shapes `git commit` will not make."""
        head = self.git("rev-parse", "HEAD")
        ident = "{} <{}> ".format(*SETUP).encode() + stamp
        tree = tree or self.git("rev-parse", "HEAD^{tree}").encode()
        body = (b"tree " + tree + b"\nparent " + head.encode()
                + b"\nauthor " + ident + b"\ncommitter " + ident + b"\n" + extra + b"\n" + message)
        run = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--literally", "--stdin"],
                             cwd=self.root, env=self.env, input=body, capture_output=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.decode().strip()

    def configure(self, identity):
        self.git("config", "user.name", identity[0])
        self.git("config", "user.email", identity[1])

    def accepted(self, tested=CURRENT, head=CURRENT):
        tested = self.tested if tested is CURRENT else tested
        head = self.git("rev-parse", "HEAD") if head is CURRENT else head
        verdicts = {}
        with patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}):
            for name in IDENTITY_VARIABLES:
                os.environ.pop(name, None)
            for name, module in MODULES.items():
                verdicts[name] = module.only_setup_commits_since(self.root, tested, head)
        self.assertEqual(len(set(verdicts.values())), 1, verdicts)
        return verdicts["e2e-evidence"]

    def test_tested_commit_itself_is_accepted_without_git(self):
        self.assertTrue(self.accepted())
        for module in MODULES.values():
            self.assertTrue(module.only_setup_commits_since(self.root / "missing", self.tested, self.tested))

    def test_setup_commits_under_the_fallback_identity_are_accepted(self):
        self.configure(SETUP)  # what setup writes when the machine has no identity
        self.commit("onecli", "setup: apply add-onecli")
        self.commit("slack", "setup: apply slack provisioning core")
        self.assertTrue(self.accepted())

    def test_setup_commits_under_the_checkout_identity_are_accepted(self):
        self.configure(OPERATOR)  # an existing identity, as on a developer Mac
        self.commit("onecli", "setup: apply add-onecli")
        self.assertTrue(self.accepted())

    def test_foreign_commit_after_the_tested_commit_is_rejected(self):
        self.configure(SETUP)
        self.commit("onecli", "setup: apply add-onecli")
        self.commit("fix", "fix: unrelated change")
        self.assertFalse(self.accepted())
        self.commit("later", "setup: apply add-codex")  # a later setup commit does not launder it
        self.assertFalse(self.accepted())

    def test_setup_subject_from_another_identity_is_rejected(self):
        for configured, author, committer in (
            (SETUP, OTHER, SETUP),
            (SETUP, SETUP, OTHER),
            (SETUP, ("Someone else", SETUP[1]), SETUP),
            (OPERATOR, SETUP, OPERATOR),  # the fallback is trusted only when it is the checkout's identity
        ):
            with self.subTest(configured=configured, author=author, committer=committer):
                self.git("reset", "-q", "--hard", self.tested)
                self.configure(configured)
                self.commit("change", "setup: apply add-onecli", author=author, committer=committer)
                self.assertFalse(self.accepted())

    def test_setup_subject_with_more_lines_or_no_label_is_rejected(self):
        self.configure(SETUP)
        for message in ("setup: apply add-onecli\n\nand more", "setup: apply", "setup: applied add-onecli",
                        "setup: apply\tadd-onecli"):
            with self.subTest(message=message):
                self.git("reset", "-q", "--hard", self.tested)
                self.commit("change", message)
                self.assertFalse(self.accepted())

    def test_setup_commit_with_a_declared_encoding_is_accepted(self):
        self.configure(SETUP)
        self.git("config", "i18n.commitEncoding", "ISO-8859-1")
        self.commit("onecli", "setup: apply add-onecli")
        self.assertIn("\nencoding ISO-8859-1\n", self.git("cat-file", "commit", "HEAD"))
        self.assertTrue(self.accepted())

    def test_signed_hidden_body_or_malformed_commit_is_rejected(self):
        self.configure(SETUP)
        self.assertTrue(self.accepted(head=self.raw(b"setup: apply add-onecli\n")))  # the hand-made shape itself passes
        self.assertFalse(self.accepted(head=self.raw(b"setup: apply add-onecli\n", extra=SIGNATURE)))
        self.assertFalse(self.accepted(head=self.raw(b"setup: apply add-onecli\0\n\nunrelated body\n")))
        self.assertFalse(self.accepted(head=self.raw(b"setup: apply add-onecli\n", tree=b"garbage")))
        self.assertFalse(self.accepted(head=self.raw(b"setup: apply add-onecli\n", stamp=b"soon +0000")))
        self.assertFalse(self.accepted(head=self.raw(b"setup: apply add-onecli\n", extra=b"encoding \n")))

    def test_merge_of_setup_commits_is_rejected(self):
        self.configure(SETUP)
        self.git("checkout", "-q", "-b", "side")
        self.commit("side", "setup: apply add-codex")
        self.git("checkout", "-q", "main")
        self.commit("main", "setup: apply add-onecli")
        self.git("merge", "-q", "--no-ff", "-m", "setup: apply merged", "side")
        self.assertFalse(self.accepted())

    def test_grafts_and_replace_refs_cannot_hide_a_foreign_commit(self):
        self.configure(SETUP)
        self.commit("fix", "fix: unrelated change")
        setup = self.commit("onecli", "setup: apply add-onecli")
        grafts = self.root / ".git/info/grafts"
        grafts.parent.mkdir(exist_ok=True)
        grafts.write_text(setup + " " + self.tested + "\n")
        self.assertEqual(self.git("rev-parse", setup + "^"), self.tested)  # Git's history view is fooled
        self.assertFalse(self.accepted())
        grafts.unlink()
        self.git("replace", "--graft", setup, self.tested)
        self.assertEqual(self.git("rev-parse", setup + "^"), self.tested)
        self.assertFalse(self.accepted())

    def test_diverged_or_older_head_is_rejected(self):
        self.configure(SETUP)
        self.git("checkout", "-q", "--detach", self.base)
        self.commit("sibling", "setup: apply add-onecli")
        self.assertFalse(self.accepted())
        self.assertFalse(self.accepted(head=self.base))

    def test_malformed_or_unknown_commits_are_rejected(self):
        self.configure(SETUP)
        head = self.commit("onecli", "setup: apply add-onecli")
        self.assertTrue(self.accepted(head=head))  # the same history passes with full SHAs
        for value in (None, "", "HEAD", head[:12], head.upper(), "--all", 7):
            with self.subTest(value=value):
                self.assertFalse(self.accepted(tested=value, head=value))  # never equal, so never trusted
        self.assertFalse(self.accepted(head="HEAD"))  # a ref name is not resolved
        self.assertFalse(self.accepted(tested="f" * 40, head=head))

    def test_shipped_copies_are_identical(self):
        self.assertEqual(inspect.getsource(MODULES["e2e-evidence"].only_setup_commits_since),
                         inspect.getsource(MODULES["macos-service"].only_setup_commits_since))


if __name__ == "__main__":
    unittest.main()
