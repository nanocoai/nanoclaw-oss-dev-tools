"""Keep distributed skill metadata synchronized across contributor-facing files."""
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RepositoryContractTests(unittest.TestCase):
    def setUp(self):
        self.skills = sorted(path.parent.name for path in (ROOT / 'skills').glob('*/SKILL.md'))
        self.readme = (ROOT / 'README.md').read_text()
        self.catalog = (ROOT / 'docs/skills-catalog.md').read_text()

    def test_every_distributed_skill_is_listed_in_readme_and_catalog(self):
        for name in self.skills:
            with self.subTest(skill=name):
                self.assertIn(f'[{name}](skills/{name}/SKILL.md)', self.readme)
                self.assertIn(f'[{name}](../skills/{name}/SKILL.md)', self.catalog)
                self.assertIn(f'## {name}\n', self.catalog)

    def test_plugin_metadata_matches_catalog_and_marketplace(self):
        manifest = json.loads((ROOT / '.claude-plugin/plugin.json').read_text())
        version = manifest['version']
        match = re.search(r'plugin\s+manifest version \*\*([^*]+)\*\*', self.catalog)
        self.assertIsNotNone(match, 'catalog must state the plugin manifest version')
        self.assertEqual(match.group(1), version)
        count = re.search(r'contains \*\*(\d+) skills\*\*', self.catalog)
        self.assertIsNotNone(count, 'catalog must state the distributed skill count')
        self.assertEqual(int(count.group(1)), len(self.skills))
        marketplace = json.loads((ROOT / '.claude-plugin/marketplace.json').read_text())
        entries = [item for item in marketplace.get('plugins', [])
                   if item.get('name') == manifest['name']]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].get('description'), manifest['description'])


if __name__ == '__main__':
    unittest.main()
