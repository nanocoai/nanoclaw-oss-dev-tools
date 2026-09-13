"""Provider discovery follows the exact NanoClaw and provider payload revisions."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "skills/e2e-wizard/scripts/provider-options.py"


def load():
    spec = importlib.util.spec_from_file_location("provider_options_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


provider_options = load()


class ProviderOptionsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="provider-options-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Provider test")
        self.git("config", "user.email", "provider@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        hooks = self.root / "empty-hooks"
        hooks.mkdir()
        self.git("config", "core.hooksPath", str(hooks))
        (self.root / "package.json").write_text('{"name":"nanoclaw"}\n')
        providers = self.root / "setup/providers"
        providers.mkdir(parents=True)
        (providers / "index.ts").write_text("import './claude.js';\n")
        (providers / "claude.ts").write_text(textwrap.dedent("""
            registerSetupProvider({
              value: 'claude',
              label: 'Claude',
              hint: 'Anthropic subscription or API key',
            });
        """))
        (self.root / "setup/auto.ts").write_text(textwrap.dedent("""
            const method = await brightSelect({
              message: 'How would you like to connect to Claude?',
              options: [
                { value: 'subscription', label: 'Sign in with my Claude subscription', hint: 'opens a browser' },
                { value: 'oauth', label: 'Paste an OAuth token I already have', hint: 'sk-ant-oat' },
                { value: 'api', label: 'Paste an Anthropic API key', hint: 'pay per use' },
                { value: 'skip', label: "Skip — I'll connect later", hint: 'no model replies' },
              ],
            });
            setupLog.userInput('auth_method', method);
        """))
        self.skill("add-codex", "codex", "Codex", "OpenAI account", True, "provider-payload")
        self.skill("add-hidden", "hidden", "Hidden", "not offered", False, "provider-payload")
        self.git("add", ".")
        self.git("commit", "-m", "picker")
        self.main_commit = self.git("rev-parse", "HEAD")
        self.git("switch", "-c", "provider-payload")
        (providers / "codex.ts").write_text(textwrap.dedent("""
            registerSetupProvider({
              value: 'codex', label: 'Codex', hint: 'OpenAI account', runAuth: runCodexAuthStep,
            });
            const method = await brightSelect({
              message: 'How would you like to connect Codex?',
              options: [
                { value: 'browser', label: 'Sign in with my ChatGPT subscription', hint: 'browser' },
                { value: 'device', label: 'ChatGPT device pairing', hint: 'URL and code' },
                { value: 'api', label: 'Paste an OpenAI API key', hint: 'pay per use' },
                { value: 'skip', label: "Skip — I'll connect later", hint: 'no replies' },
              ],
            });
            setupLog.userInput('codex_auth_method', method);
        """))
        self.git("add", ".")
        self.git("commit", "-m", "codex payload")
        self.payload_commit = self.git("rev-parse", "HEAD")
        self.git("switch", "main")

    def git(self, *args):
        run = subprocess.run(["git", *args], cwd=self.root, text=True, capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.strip()

    def skill(self, directory, value, label, hint, offered, branch):
        path = self.root / ".claude/skills" / directory
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(textwrap.dedent(f"""\
            ---
            name: {directory}
            metadata:
              nanoclaw-provider: {value}
              nanoclaw-provider-label: {label}
              nanoclaw-provider-hint: {hint}
              nanoclaw-provider-offered: '{str(offered).lower()}'
              nanoclaw-provider-image: local-required
            ---
            ```nc:copy from-branch:{branch}
            setup/providers/{value}.ts
            ```
        """))

    def test_picker_uses_registered_and_offered_descriptor_sources(self):
        report = provider_options.discover(self.root)
        self.assertEqual(report["nanoclaw_commit"], self.main_commit)
        self.assertEqual([item["value"] for item in report["providers"]], ["claude", "codex"])
        self.assertTrue(report["providers"][0]["installed"])
        self.assertFalse(report["providers"][1]["installed"])

    def test_claude_auth_comes_from_exact_checkout(self):
        selected = provider_options.discover(self.root, "claude")["selected"]
        self.assertEqual(selected["auth_source_commit"], self.main_commit)
        self.assertEqual(selected["auth_input_key"], "auth_method")
        self.assertEqual(selected["auth_prompt"], "How would you like to connect to Claude?")
        self.assertEqual([item["value"] for item in selected["auth_methods"]],
                         ["subscription", "oauth", "api", "skip"])
        self.assertEqual(selected["auth_methods"][1]["credential_kind"], "anthropic-oauth")

    def test_installable_auth_records_exact_payload_commit(self):
        selected = provider_options.discover(
            self.root, "codex", payload_ref="provider-payload",
        )["selected"]
        self.assertEqual(selected["auth_source_commit"], self.payload_commit)
        self.assertEqual(selected["auth_source_ref"], "provider-payload")
        self.assertEqual(selected["auth_input_key"], "codex_auth_method")
        self.assertEqual([item["automation"] for item in selected["auth_methods"]],
                         ["human-handoff", "human-handoff", "credential-file", "unsupported"])
        self.assertEqual(selected["auth_methods"][2]["credential_kind"], "openai-api-key")

    def test_unresolved_or_changed_payload_fails_instead_of_guessing(self):
        with self.assertRaises(provider_options.DiscoveryError):
            provider_options.discover(self.root, "codex", payload_ref="missing-ref")
        (self.root / "setup/auto.ts").write_text("setupLog.userInput('auth_method', method);\n")
        # Discovery reads the requested Git tree, not uncommitted working state.
        self.assertEqual(provider_options.discover(self.root, "claude")["nanoclaw_commit"],
                         self.main_commit)
        self.git("add", "setup/auto.ts")
        self.git("commit", "-m", "changed auth surface")
        with self.assertRaises(provider_options.DiscoveryError):
            provider_options.discover(self.root, "claude")

    def test_revision_can_be_inspected_without_checking_it_out(self):
        report = provider_options.discover(self.root, "codex", payload_ref="provider-payload",
                                           revision=self.main_commit)
        self.assertEqual(report["nanoclaw_commit"], self.main_commit)
        self.assertEqual(
            provider_options.discover(self.root, "claude", revision=self.main_commit)["selected"]["auth_source_ref"],
            self.main_commit,
        )

    def test_cli_emits_json_and_rejects_unoffered_provider(self):
        run = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root)],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["schema_version"], 1)
        rejected = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), "--provider", "hidden"],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(rejected.returncode, 65)
        self.assertIn("not offered", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
