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

    def mixed_skill(self):
        """Mirror .claude/skills/add-codex/SKILL.md at nanoclaw 290aa683: a
        from-branch registry payload plus a bundled auth hook."""
        path = self.root / ".claude/skills/add-mixed"
        (path / "payload/setup/providers").mkdir(parents=True)
        (path / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: add-mixed
            metadata:
              nanoclaw-provider: mixed
              nanoclaw-provider-label: Mixed
              nanoclaw-provider-hint: registry payload plus bundled auth hook
              nanoclaw-provider-offered: 'true'
              nanoclaw-provider-image: local-required
            ---
            ```nc:copy from-branch:provider-payload
            src/providers/mixed.ts
            container/agent-runner/src/providers/mixed.ts
            ```

            ```nc:copy
            payload/setup/providers/mixed.ts -> setup/providers/mixed.ts
            payload/setup/providers/mixed.test.ts -> setup/providers/mixed.test.ts
            ```
        """))
        (path / "payload/setup/providers/mixed.ts").write_text(textwrap.dedent("""
            registerSetupProvider({ value: 'mixed', label: 'Mixed', hint: 'x', runAuth: runMixedAuthStep });
            const method = await brightSelect({
              message: 'How would you like to connect Mixed?',
              options: [
                { value: 'device', label: 'Device pairing', hint: 'URL and code' },
                { value: 'api', label: 'Paste an OpenAI API key', hint: 'pay per use' },
                { value: 'skip', label: "Skip — I'll connect later", hint: 'no replies' },
              ],
            });
            setupLog.userInput('mixed_auth_method', method);
        """))
        (path / "payload/setup/providers/mixed.test.ts").write_text("test\n")
        self.git("add", ".")
        self.git("commit", "-m", "mixed skill")
        return self.git("rev-parse", "HEAD")

    def test_multi_payload_skill_reports_every_source_and_picks_the_auth_hook_commit(self):
        mixed_commit = self.mixed_skill()
        selected = provider_options.discover(self.root, "mixed", payload_ref="provider-payload")["selected"]
        self.assertEqual(selected["payload_kind"], "mixed")
        self.assertEqual(
            [(source["kind"], source["branch"], source["commit"], source["file_count"])
             for source in selected["payload_sources"]],
            [("branch", "provider-payload", self.payload_commit, 2), ("bundled", None, mixed_commit, 2)],
        )
        # The auth hook is bundled, so the auth source is the NanoClaw commit,
        # not the registry branch that the first block names.
        self.assertEqual(selected["auth_source_commit"], mixed_commit)
        self.assertEqual(selected["auth_source"], ".claude/skills/add-mixed/payload/setup/providers/mixed.ts")
        self.assertEqual(selected["auth_input_key"], "mixed_auth_method")
        self.assertEqual([item["automation"] for item in selected["auth_methods"]],
                         ["human-handoff", "credential-file", "unsupported"])
        by_destination = {item["destination"]: item for item in selected["payload_files"]}
        self.assertEqual(by_destination["src/providers/mixed.ts"]["commit"], self.payload_commit)
        self.assertEqual(by_destination["setup/providers/mixed.ts"]["commit"], mixed_commit)
        self.assertEqual(by_destination["setup/providers/mixed.ts"]["source"],
                         ".claude/skills/add-mixed/payload/setup/providers/mixed.ts")
        # Without the branch fetched, discovery still refuses to guess.
        self.git("branch", "-m", "provider-payload", "elsewhere")
        with self.assertRaises(provider_options.DiscoveryError):
            provider_options.discover(self.root, "mixed")

    def test_single_block_skills_keep_their_report_shape(self):
        selected = provider_options.discover(self.root, "codex", payload_ref="provider-payload")["selected"]
        self.assertEqual(selected["payload_kind"], "branch")
        self.assertNotIn("payload_sources", selected)
        self.assertEqual(set(selected["payload_files"][0]), {"source", "destination", "branch"})

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
