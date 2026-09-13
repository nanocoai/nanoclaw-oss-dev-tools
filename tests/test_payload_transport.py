import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/e2e-wizard/scripts/proxmox-wizard.py'
spec = importlib.util.spec_from_file_location('payload_transport_adapter', SCRIPT)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


class PayloadTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='payload-transport-')
        self.root = Path(self.temporary.name)
        self.core_bare = self.root / 'core.git'
        self.payload_bare = self.root / 'payload.git'
        subprocess.run(['git', 'init', '--bare', '-q', self.core_bare], check=True)
        subprocess.run(['git', 'init', '--bare', '-q', self.payload_bare], check=True)
        self._seed(self.core_bare, 'main', 'core.txt', 'core\n')
        self.payload_commit = self._seed(
            self.payload_bare, 'providers', 'setup/providers/codex.ts', 'payload\n',
        )
        self.checkout = self.root / 'checkout'
        subprocess.run(['git', 'clone', '-q', self.core_bare, self.checkout], check=True)
        subprocess.run(['git', 'remote', 'set-url', 'origin', 'https://core.example.test/nanoclaw.git'],
                       cwd=self.checkout, check=True)
        subprocess.run(['git', 'remote', 'add', 'payload-owner', str(self.payload_bare)],
                       cwd=self.checkout, check=True)
        subprocess.run(['git', 'fetch', '-q', 'payload-owner',
                        'refs/heads/providers:refs/remotes/payload-owner/providers'],
                       cwd=self.checkout, check=True)
        subprocess.run(['git', 'remote', 'set-url', 'payload-owner',
                        'https://payload.example.test/nanoclaw.git'], cwd=self.checkout, check=True)

    def tearDown(self):
        self.temporary.cleanup()

    def _seed(self, bare, branch, name, content):
        work = self.root / ('seed-' + branch)
        subprocess.run(['git', 'init', '-q', '-b', branch, work], check=True)
        subprocess.run(['git', 'config', 'user.email', 'e2e@example.invalid'], cwd=work, check=True)
        subprocess.run(['git', 'config', 'user.name', 'E2E'], cwd=work, check=True)
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        subprocess.run(['git', 'add', '.'], cwd=work, check=True)
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                        'commit', '-qm', branch], cwd=work, check=True)
        subprocess.run(['git', 'remote', 'add', 'target', str(bare)], cwd=work, check=True)
        subprocess.run(['git', 'push', '-q', 'target', branch], cwd=work, check=True)
        return git(work, 'rev-parse', 'HEAD')

    def test_alternate_owning_remote_transports_exact_selected_commit(self):
        transport = adapter.payload_transport(
            self.checkout, 'refs/remotes/payload-owner/providers', self.payload_commit,
        )
        self.assertEqual(transport, {
            'url': 'https://payload.example.test/nanoclaw.git',
            'ref': 'refs/heads/providers',
            'commit': self.payload_commit,
        })
        home = self.root / 'guest-home'
        home.mkdir()
        (home / '.gitconfig').write_text(
            '[url "' + self.payload_bare.as_uri() + '"]\n'
            '\tinsteadOf = https://payload.example.test/nanoclaw.git\n'
        )
        environment = {**os.environ, 'HOME': str(home)}
        subprocess.run(['bash', '-ceu', adapter.payload_setup(transport)], cwd=self.checkout,
                       env=environment, check=True)
        arrived = git(home / '.nanoclaw-e2e/provider-payload.git',
                      'rev-parse', 'refs/heads/providers^{commit}')
        self.assertEqual(arrived, self.payload_commit)
        self.assertNotEqual(arrived, git(self.checkout, 'rev-parse', 'origin/main'))
        unavailable = subprocess.run(
            ['git', '--git-dir', str(self.core_bare), 'cat-file', '-e', self.payload_commit + '^{commit}'],
            capture_output=True,
        )
        self.assertNotEqual(unavailable.returncode, 0)

    def test_origin_remote_tracking_ref_remains_supported(self):
        subprocess.run(['git', 'remote', 'set-url', 'origin',
                        'https://github.com/nanocoai/nanoclaw.git'], cwd=self.checkout, check=True)
        subprocess.run(['git', 'update-ref', 'refs/remotes/origin/providers', self.payload_commit],
                       cwd=self.checkout, check=True)
        transport = adapter.payload_transport(
            self.checkout, 'refs/remotes/origin/providers', self.payload_commit,
        )
        self.assertEqual(transport['url'], 'https://github.com/nanocoai/nanoclaw.git')

    def test_moved_owning_branch_cannot_substitute_new_tip(self):
        transport = adapter.payload_transport(
            self.checkout, 'refs/remotes/payload-owner/providers', self.payload_commit,
        )
        advance = self.root / 'advance-payload'
        subprocess.run(['git', 'clone', '-q', self.payload_bare, advance], check=True)
        subprocess.run(['git', 'checkout', '-q', 'providers'], cwd=advance, check=True)
        subprocess.run(['git', 'config', 'user.email', 'e2e@example.invalid'], cwd=advance, check=True)
        subprocess.run(['git', 'config', 'user.name', 'E2E'], cwd=advance, check=True)
        (advance / 'setup/providers/codex.ts').write_text('moved tip\n')
        subprocess.run(['git', 'add', '.'], cwd=advance, check=True)
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                        'commit', '-qm', 'advance payload'], cwd=advance, check=True)
        subprocess.run(['git', 'push', '-q', 'origin', 'providers'], cwd=advance, check=True)
        home = self.root / 'drift-guest-home'
        home.mkdir()
        (home / '.gitconfig').write_text(
            '[url "' + self.payload_bare.as_uri() + '"]\n'
            '\tinsteadOf = https://payload.example.test/nanoclaw.git\n'
        )
        run = subprocess.run(
            ['bash', '-ceu', adapter.payload_setup(transport)], cwd=self.checkout,
            env={**os.environ, 'HOME': str(home)}, capture_output=True,
        )
        self.assertNotEqual(run.returncode, 0)
        payload_repo = home / '.nanoclaw-e2e/provider-payload.git'
        exposed = subprocess.run(
            ['git', '--git-dir', str(payload_repo), 'show-ref', '--verify',
             'refs/heads/providers'], capture_output=True,
        )
        self.assertNotEqual(exposed.returncode, 0)

    def test_raw_ambiguous_ref_and_private_url_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'require an explicit'):
            adapter.payload_transport(self.checkout, None, self.payload_commit)
        with self.assertRaisesRegex(ValueError, 'explicit remote-tracking ref'):
            adapter.payload_transport(self.checkout, self.payload_commit, self.payload_commit)
        subprocess.run(['git', 'remote', 'set-url', 'payload-owner',
                        'https://user:secret@example.test/nanoclaw.git'], cwd=self.checkout, check=True)
        with self.assertRaisesRegex(ValueError, 'without credentials'):
            adapter.payload_transport(
                self.checkout, 'refs/remotes/payload-owner/providers', self.payload_commit,
            )
        for private_url in ('file:///private/payload.git', 'https://127.0.0.1/payload.git'):
            with self.subTest(private_url=private_url):
                subprocess.run(['git', 'remote', 'set-url', 'payload-owner', private_url],
                               cwd=self.checkout, check=True)
                with self.assertRaisesRegex(ValueError, 'public HTTPS'):
                    adapter.payload_transport(
                        self.checkout, 'refs/remotes/payload-owner/providers', self.payload_commit,
                    )

    def test_overlapping_remote_prefixes_are_ambiguous(self):
        with (self.checkout / '.git/config').open('a') as config:
            config.write(
                '\n[remote "forks"]\n\turl = https://forks.example.test/nanoclaw.git\n'
                '[remote "forks/alice"]\n\turl = https://alice.example.test/nanoclaw.git\n'
            )
        subprocess.run(['git', 'update-ref', 'refs/remotes/forks/alice/providers', self.payload_commit],
                       cwd=self.checkout, check=True)
        with self.assertRaisesRegex(ValueError, 'explicit remote-tracking ref'):
            adapter.payload_transport(
                self.checkout, 'refs/remotes/forks/alice/providers', self.payload_commit,
            )


if __name__ == '__main__':
    unittest.main()
