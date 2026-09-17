"""TypeSafe docs-drift detector: extraction, sections, search, gate, fixture replay and transport, all offline."""

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/typesafe-docs-drift/scripts/typesafe-docs-drift.py"
FIXTURE = ROOT / "skills/typesafe-docs-drift/fixtures/nanoclaw-sample.json"


def load():
    spec = importlib.util.spec_from_file_location("typesafe_docs_drift_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


drift = load()
THRESHOLDS = {"contradicts": 0.7, "covers": 0.7, "staleness": 0.5}


def write(root, relative_path, content):
    path = Path(root) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))
    return path


def make_code(root, with_gateway_doc=False):
    write(root, "src/cli/resources/index.ts", "import './widgets.js';\n")
    write(root, "src/cli/resources/widgets.test.ts", "registerResource({ plural: 'ghosts', operations: { list: 'open' } });\n")
    write(root, "src/cli/resources/widgets.ts", """
        import { registerResource } from '../crud.js';

        registerResource({
          name: 'widget',
          plural: 'widgets',
          table: 'widgets',
          description: 'Widgets.',
          idColumn: 'id',
          columns: [
            { name: 'id', type: 'string', description: 'UUID.', generated: true },
            {
              name: 'state',
              type: 'string',
              description: 'Live state.',
              enum: [
                'on',
                'off',
              ],
            },
          ],
          operations: { list: 'open', get: 'open', update: 'approval' },
          customOperations: {
            poke: {
              access: 'approval',
              description: 'Poke a widget. Use --id <widget-id> [--hard]. ' +
                'Pass --hard to poke harder.',
              handler: async (args) => ({ poked: args.id, flag: '--not-a-flag-after-handler' }),
            },
            'config get': {
              access: 'open',
              hostOnly: true,
              description: 'Show config. Use --id <widget-id>.',
              handler: async () => ({}),
            },
            run: {
              access: 'open',
              description: 'Run now. Unlike update --process-after now, it keeps the schedule.',
              args: [
                { name: 'id', type: 'string', description: 'Id.', required: true },
                {
                  name: 'group_id',
                  type: 'string',
                  description: 'Group.',
                },
              ],
              handler: async () => ({}),
            },
          },
        });
        """)
    write(root, "src/cli/resources/groups.ts", """
        registerResource({ name: 'group', plural: 'groups', columns: [], operations: { list: 'open' } });
        if (!['disabled', 'group', 'global'].includes(scope)) {
          throw new Error('--cli-scope must be one of: disabled, group, global');
        }
        """)
    write(root, "src/config.ts", """
        const envConfig = readEnvFile([
          'ASSISTANT_NAME',
          'FLAG',
          'LIMIT',
          'NOTHING',
        ]);
        export const ASSISTANT_NAME = process.env.ASSISTANT_NAME || envConfig.ASSISTANT_NAME || 'Andy';
        export const FLAG = (process.env.FLAG || envConfig.FLAG) === 'true';
        export const LIMIT = process.env.LIMIT ?? envConfig.LIMIT ?? '2048';
        export const NOTHING = process.env.NOTHING || envConfig.NOTHING;
        """)
    write(root, "src/gateway-providers/index.ts", """
        /**
         * Settings honor `.env` with `process.env` taking precedence, like every other setting.
         */
        const DEFAULT_GATEWAY_PROVIDER_KIND = 'onecli';
        throw new Error(`NANOCLAW_GATEWAY_PROVIDER='${kind}' but no gateway provider is registered for '${kind}'`);
        """)
    write(root, "src/drivers/index.ts", "const DEFAULT_DRIVER_KIND = 'docker';\n")
    write(root, "src/log.ts", "const threshold = LEVELS[(process.env.LOG_LEVEL as Level) || 'info'] ?? LEVELS.info;\nconst home = process.env.HOME || '/root';\n")
    write(root, "src/db/migrations/014-container-configs.ts", """
        db.exec(`
          CREATE TABLE container_configs (
            agent_group_id        TEXT PRIMARY KEY REFERENCES agent_groups(id) ON DELETE CASCADE,
            provider              TEXT,
            skills                TEXT NOT NULL DEFAULT '"all"',
            updated_at            TEXT NOT NULL
          );
        `);
        """)
    write(root, "src/db/migrations/015-cli-scope.ts", """
        db.prepare("ALTER TABLE container_configs ADD COLUMN cli_scope TEXT NOT NULL DEFAULT 'group'").run();
        """)
    write(root, ".claude/skills/add-foo/SKILL.md", """
        ---
        name: add-foo
        description: "Add the Foo channel via Chat SDK."
        ---

        # Add Foo
        """)
    write(root, ".claude/skills/bar/SKILL.md", "---\nname: bar\ndescription: Bar things.\n---\n")
    (Path(root) / ".claude/skills/no-skill-file").mkdir(parents=True, exist_ok=True)
    write(root, "CLAUDE.md", """
        # Project

        ## Timestamps

        Two rules, no exceptions:

        - **Storage**: every timestamp written from JS is `new Date().toISOString()` (ISO-8601 UTC with `Z`).
        - **Display**: anything shown to a user renders in the install timezone via `formatLocalTime`.

        An agent group can override the install timezone with `ncl groups config update --timezone <IANA>`.

        ## Next section

        Unrelated.
        """)
    write(root, ".env.example", "# comment\nFOO=bar\nEMPTY=\n")
    if with_gateway_doc:
        write(root, "docs/gateway-seam.md", """
            # Gateway Provider Seam

            ## Selection

            One provider per install, chosen by `NANOCLAW_GATEWAY_PROVIDER` and resolved
            once at startup by `getGatewayProvider()`.

            With no provider registered, the host refuses to start: there is no implicit default.

            ## Other

            Nothing here.
            """)
    return root


def make_docs(root):
    write(root, "docs.json", json.dumps({
        "navigation": {"tabs": [
            {"tab": "Documentation", "groups": [
                {"group": "Operate", "pages": ["operate/ncl-cli", "operate/ncl-cli"]},
                {"group": "Changelog", "pages": ["changelog/index"]},
            ]},
            {"tab": "Reference", "groups": [{"group": "Operating", "pages": ["reference/env", "missing/page"]}]},
        ]},
    }))
    write(root, "operate/ncl-cli.mdx", """
        ---
        title: "The ncl admin CLI"
        description: "Administer a host."
        keywords: ["ncl", "cli_scope"]
        ---

        {/* verified-against: src/cli/dispatch.ts @ abc123 */}

        Intro paragraph about `ncl`.

        ## Agents can run ncl too

        <Note>
        What an agent may do is controlled per group by `cli_scope`: `disabled`, `group` (default) or `global`.
        </Note>

        ```bash
        ## not a heading
        ncl groups config update --id <group-id> --cli-scope disabled
        ```

        ## Working with resources

        ### widgets

        Poke widgets with `ncl widgets poke --id <id>`.

        ### widgets

        Duplicate heading.
        """)
    write(root, "reference/env.mdx", """
        ---
        title: "Environment variables"
        ---

        ## Read from .env

        | Variable | Default |
        |---|---|
        | `ASSISTANT_NAME` | `Andy` |
        | `LIMIT` | `1024` |
        """)
    write(root, "changelog/index.mdx", "---\ntitle: Changelog\n---\n\n## 2026-09-01\n\nAdded `ASSISTANT_NAME` and `cli_scope` notes.\n")
    return root


def noul(value):
    return {"type": "noul", "noul": value}


def score(value, confidence):
    return {"type": "score", "score": value, "legend": {}, "probabilities": {}, "confidence": confidence}


def answers(contradicts=0.1, covers=0.9, staleness=0.2, confidence=0.9):
    return {"contradicts": noul(contradicts), "covers": noul(covers), "staleness": score(staleness, confidence)}


def response(contradicts=0.1, covers=0.9, staleness=0.2, confidence=0.9, tokens=100):
    return {"model": "jev-latest", "answers": answers(contradicts, covers, staleness, confidence),
            "usage": {"input_tokens": tokens, "output_tokens": 10}}


def pair(section_id, decision):
    return {"key": "f|" + section_id, "section": {"id": section_id, "page": section_id.split("#")[0], "heading": "h"},
            "decision": decision}


class TempDirs(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="typesafe-docs-drift-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def code(self, **kwargs):
        return make_code(self.base / "nanoclaw", **kwargs)

    def docs(self):
        return make_docs(self.base / "nanoclaw-docs")


class ExtractionTests(TempDirs):
    def by_id(self, facts):
        return {entry["id"]: entry for entry in facts}

    def test_ncl_resources_yield_verbs_flags_and_enums_and_skip_test_modules(self):
        facts = self.by_id(drift.extract_facts(self.code(), ["ncl"]))
        self.assertEqual(set(facts), {"ncl:widgets-verbs", "ncl:widgets-poke-flags", "ncl:widgets-config-get-flags",
                                      "ncl:widgets-run-flags", "ncl:widgets-state-enum"})
        verbs = facts["ncl:widgets-verbs"]
        self.assertIn("list (open), get (open), update (approval), poke (approval), config get (open, host only), run (open)", verbs["statement"])
        self.assertEqual(verbs["evidence"]["file"], "src/cli/resources/widgets.ts")
        self.assertEqual(verbs["evidence"]["line"], 3)
        poke = facts["ncl:widgets-poke-flags"]
        self.assertIn("names these flags in its help text: `--id`, `--hard`", poke["statement"])
        self.assertNotIn("not-a-flag", poke["statement"])
        run = facts["ncl:widgets-run-flags"]
        self.assertIn("declares exactly these flags (any other flag is rejected): `--id`, `--group-id`", run["statement"])
        self.assertNotIn("process-after", run["statement"])
        self.assertIn("`on`, `off`", facts["ncl:widgets-state-enum"]["statement"])
        self.assertIn("widgets", facts["ncl:widgets-state-enum"]["terms"])
        self.assertNotIn("ncl:ghosts-verbs", facts)

    def test_env_facts_capture_precedence_defaults_and_boolean_flags(self):
        facts = self.by_id(drift.extract_facts(self.code(), ["env"]))
        self.assertIn("default is `Andy`", facts["env:ASSISTANT_NAME"]["statement"])
        self.assertIn("only when set to the string `true`", facts["env:FLAG"]["statement"])
        self.assertIn("default is `2048`", facts["env:LIMIT"]["statement"])
        self.assertIn("`??`", facts["env:LIMIT"]["statement"])
        self.assertIn("no default", facts["env:NOTHING"]["statement"])
        self.assertIn("defaults to `onecli`", facts["env:NANOCLAW_GATEWAY_PROVIDER"]["statement"])
        self.assertIn("defaults to `docker`", facts["env:NANOCLAW_RUNTIME_DRIVER"]["statement"])
        self.assertIn("process environment only", facts["env:LOG_LEVEL"]["statement"])
        self.assertNotIn("env:HOME", facts)
        self.assertIn("example value: `bar`", facts["env:example-FOO"]["statement"])
        self.assertIn("example value: `empty`", facts["env:example-EMPTY"]["statement"])
        self.assertEqual(facts["env:ASSISTANT_NAME"]["evidence"]["line"], 7)

    def test_container_config_columns_defaults_and_cli_scope_values(self):
        facts = self.by_id(drift.extract_facts(self.code(), ["container-config"]))
        self.assertIn("`agent_group_id`, `provider`, `skills`, `updated_at`, `cli_scope`", facts["container-config:columns"]["statement"])
        self.assertIn("defaults to '\"all\"'", facts["container-config:skills-default"]["statement"])
        self.assertIn("defaults to 'group'", facts["container-config:cli_scope-default"]["statement"])
        self.assertEqual(facts["container-config:cli_scope-default"]["evidence"]["file"], "src/db/migrations/015-cli-scope.ts")
        self.assertIn("`disabled`, `group`, `global`", facts["container-config:cli-scope-values"]["statement"])
        self.assertNotIn("container-config:provider-default", facts)

    def test_skills_come_from_frontmatter_and_directories_without_a_skill_file_are_ignored(self):
        facts = self.by_id(drift.extract_facts(self.code(), ["skills"]))
        self.assertEqual(set(facts), {"skills:catalog", "skills:add-foo", "skills:bar"})
        self.assertIn("Add the Foo channel via Chat SDK.", facts["skills:add-foo"]["statement"])
        self.assertIn("exactly 2 workspace skills", facts["skills:catalog"]["statement"])
        self.assertEqual(facts["skills:add-foo"]["evidence"]["file"], ".claude/skills/add-foo/SKILL.md")
        self.assertIn("/add-foo", facts["skills:add-foo"]["terms"])

    def test_gateway_facts_come_from_code_or_from_the_seam_doc_when_present(self):
        from_code = self.by_id(drift.extract_facts(self.code(), ["gateway"]))
        self.assertEqual(set(from_code), {"gateway:selection-default", "gateway:selection-unknown", "gateway:selection-precedence"})
        self.assertIn("`onecli`", from_code["gateway:selection-default"]["statement"])
        with_doc = self.by_id(drift.extract_facts(make_code(self.base / "with-doc", with_gateway_doc=True), ["gateway"]))
        self.assertEqual(set(with_doc), {"gateway:selection-1", "gateway:selection-2"})
        self.assertEqual(with_doc["gateway:selection-1"]["evidence"]["file"], "docs/gateway-seam.md")
        self.assertEqual(with_doc["gateway:selection-1"]["evidence"]["line"], 5)
        self.assertIn("refuses to start", with_doc["gateway:selection-2"]["statement"])

    def test_timestamp_rules_are_split_by_bullet_and_paragraph(self):
        facts = drift.extract_facts(self.code(), ["timestamps"])
        self.assertEqual([entry["id"] for entry in facts], ["timestamps:rule-1", "timestamps:rule-2", "timestamps:rule-3"])
        self.assertTrue(facts[0]["statement"].startswith("**Storage**"))
        self.assertIn("--timezone", facts[2]["statement"])
        self.assertEqual(facts[0]["evidence"]["file"], "CLAUDE.md")
        self.assertEqual(facts[0]["evidence"]["line"], 7)
        self.assertNotIn("Unrelated", " ".join(entry["statement"] for entry in facts))

    def test_ids_are_unique_and_areas_filter(self):
        facts = drift.extract_facts(self.code())
        ids = [entry["id"] for entry in facts]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({entry["area"] for entry in facts}, set(drift.AREAS))
        for entry in facts:
            self.assertEqual(set(entry), {"id", "area", "statement", "evidence", "terms"})
            self.assertEqual(set(entry["evidence"]), {"file", "line", "excerpt"})
        self.assertEqual({entry["area"] for entry in drift.extract_facts(self.code(), ["skills", "env"])}, {"skills", "env"})

    def test_missing_checkout_is_a_clean_error(self):
        with self.assertRaises(drift.DriftError):
            drift.extract_facts(self.base / "nope")


class SectionTests(TempDirs):
    def test_navigation_pages_are_collected_in_order_without_duplicates(self):
        config = json.loads((self.docs() / "docs.json").read_text())
        self.assertEqual(drift.navigation_pages(config), ["operate/ncl-cli", "changelog/index", "reference/env", "missing/page"])

    def test_sections_split_on_headings_and_skip_code_fences_comments_and_tags(self):
        sections = {section["id"]: section for section in drift.load_sections(self.docs())}
        self.assertEqual(set(sections), {
            "operate/ncl-cli#the-ncl-admin-cli", "operate/ncl-cli#agents-can-run-ncl-too", "operate/ncl-cli#working-with-resources",
            "operate/ncl-cli#widgets", "operate/ncl-cli#widgets-2", "reference/env#read-from-env",
        })
        agents = sections["operate/ncl-cli#agents-can-run-ncl-too"]
        self.assertEqual(agents["title"], "The ncl admin CLI")
        self.assertEqual(agents["level"], 2)
        self.assertIn("## not a heading", agents["text"])
        self.assertNotIn("<Note>", agents["text"])
        self.assertNotIn("verified-against", sections["operate/ncl-cli#the-ncl-admin-cli"]["text"])
        self.assertEqual(sections["operate/ncl-cli#widgets"]["level"], 3)
        self.assertEqual(agents["line"], 11)
        self.assertEqual(sections["operate/ncl-cli#working-with-resources"]["line"], 22)
        self.assertEqual(sections["reference/env#read-from-env"]["line"], 5)
        self.assertIn("cli_scope", agents["keywords"])

    def test_changelog_pages_are_excluded_unless_asked(self):
        default = {section["page"] for section in drift.load_sections(self.docs())}
        self.assertNotIn("changelog/index", default)
        included = {section["page"] for section in drift.load_sections(self.docs(), include_changelog=True)}
        self.assertIn("changelog/index", included)

    def test_missing_docs_json_is_a_clean_error(self):
        with self.assertRaises(drift.DriftError):
            drift.load_sections(self.base)


class SearchTests(TempDirs):
    def test_identifier_matches_rank_the_right_section_first(self):
        sections = drift.load_sections(self.docs())
        index = drift.SectionIndex(sections)
        scope = {"id": "container-config:cli-scope-values", "area": "container-config",
                 "statement": "`cli_scope` accepts exactly these values: `disabled`, `group`, `global`.", "terms": ["cli_scope", "cli-scope"]}
        hits = index.search(scope, 3)
        self.assertEqual(hits[0][0], "operate/ncl-cli#agents-can-run-ncl-too")
        self.assertLessEqual(len(hits), 3)
        self.assertTrue(all(isinstance(score, float) for _id, score in hits))
        limit = {"id": "env:LIMIT", "area": "env", "statement": "`LIMIT` defaults to `2048`.", "terms": ["LIMIT"]}
        self.assertEqual(index.search(limit, 1)[0][0], "reference/env#read-from-env")
        widget = {"id": "ncl:widgets-poke-flags", "area": "ncl", "statement": "`ncl widgets poke` accepts `--id`.", "terms": ["ncl", "widgets", "poke", "id"]}
        self.assertEqual(index.search(widget, 1)[0][0], "operate/ncl-cli#widgets")
        nothing = {"id": "x", "area": "env", "statement": "zzzz qqqq", "terms": ["zzzz"]}
        self.assertEqual(index.search(nothing, 3), [])


class QuestionTests(unittest.TestCase):
    def test_request_shape_matches_the_documented_api(self):
        entry = {"id": "env:X", "area": "env", "statement": "`X` defaults to 1.", "evidence": {"file": "src/config.ts", "line": 3, "excerpt": "x"}, "terms": ["X"]}
        section = {"id": "p#h", "page": "p", "title": "T", "heading": "h", "level": 2, "line": 1, "text": "y" * (drift.SECTION_LIMIT + 50)}
        request = drift.build_request(entry, section, code_commit="abc123")
        self.assertEqual(set(request), {"state", "model", "questions"})
        self.assertEqual(request["model"], "jev-latest")
        self.assertEqual(set(request["state"]), {"fact", "doc_section"})
        self.assertEqual(request["state"]["fact"]["evidence"]["commit"], "abc123")
        self.assertEqual(len(request["state"]["doc_section"]["text"]), drift.SECTION_LIMIT)
        self.assertEqual(set(request["questions"]), {"contradicts", "covers", "staleness"})
        self.assertEqual(request["questions"]["contradicts"]["type"], "noul")
        self.assertEqual(request["questions"]["covers"]["type"], "noul")
        self.assertEqual(request["questions"]["staleness"]["type"], "score")
        self.assertEqual(len(request["questions"]["staleness"]["criteria"]), 4)
        for question in request["questions"].values():
            self.assertTrue(question["instructions"])
        self.assertNotIn("terms", request["state"]["fact"])


class DecisionTests(unittest.TestCase):
    def test_pair_verdicts_follow_the_gates(self):
        self.assertEqual(drift.decide_pair(answers(contradicts=0.7), THRESHOLDS)["verdict"], "DRIFT")
        self.assertEqual(drift.decide_pair(answers(contradicts=0.3, covers=0.7), THRESHOLDS)["verdict"], "OK")
        self.assertEqual(drift.decide_pair(answers(contradicts=0.3, covers=0.3), THRESHOLDS)["verdict"], "UNRELATED")
        self.assertEqual(drift.decide_pair(answers(contradicts=0.5, covers=0.9), THRESHOLDS)["verdict"], "UNSURE")
        self.assertEqual(drift.decide_pair(answers(contradicts=0.2, covers=0.5), THRESHOLDS)["verdict"], "UNSURE")
        missing = drift.decide_pair({}, THRESHOLDS)
        self.assertEqual((missing["verdict"], missing["note"], missing["contradicts"]), ("UNSURE", "no answer", None))
        bad = drift.decide_pair({"contradicts": noul("high"), "covers": noul(float("nan"))}, THRESHOLDS)
        self.assertEqual(bad["verdict"], "UNSURE")

    def test_staleness_rounds_to_a_level_and_is_gated_on_confidence(self):
        decision = drift.decide_pair(answers(staleness=1.4, confidence=0.5), THRESHOLDS)
        self.assertEqual((decision["staleness_label"], decision["staleness_gated"]), ("slightly outdated", False))
        decision = drift.decide_pair(answers(staleness=1.5, confidence=0.49), THRESHOLDS)
        self.assertEqual((decision["staleness_label"], decision["staleness_gated"]), ("wrong", True))
        self.assertEqual(drift.staleness_label(9), "dangerous if followed")
        self.assertEqual(drift.staleness_label(-1), "current")
        self.assertIsNone(drift.staleness_label(None))

    def test_thresholds_are_overridable(self):
        strict = dict(THRESHOLDS, contradicts=0.9)
        self.assertEqual(drift.decide_pair(answers(contradicts=0.8), strict)["verdict"], "UNSURE")
        loose = dict(THRESHOLDS, contradicts=0.6, covers=0.6)
        self.assertEqual(drift.decide_pair(answers(contradicts=0.6), loose)["verdict"], "DRIFT")
        self.assertEqual(drift.decide_pair(answers(contradicts=0.4, covers=0.6), loose)["verdict"], "OK")

    def test_fact_verdict_folds_pairs_with_drift_winning(self):
        entry = {"id": "f", "area": "env", "statement": "s", "evidence": {"file": "a", "line": 1, "excerpt": ""}}
        drifty = pair("p#a", drift.decide_pair(answers(contradicts=0.75, covers=0.9, staleness=1.6), THRESHOLDS))
        okay = pair("p#b", drift.decide_pair(answers(contradicts=0.1, covers=0.95), THRESHOLDS))
        unrelated = pair("p#c", drift.decide_pair(answers(contradicts=0.05, covers=0.1), THRESHOLDS))
        unsure = pair("p#d", drift.decide_pair(answers(contradicts=0.5, covers=0.5), THRESHOLDS))
        result = drift.decide_fact(entry, [okay, drifty, unrelated], THRESHOLDS)
        self.assertEqual((result["verdict"], result["best"]["section"]["id"]), ("DRIFT", "p#a"))
        self.assertEqual(result["documented_anywhere"], 0.95)
        result = drift.decide_fact(entry, [unrelated, okay], THRESHOLDS)
        self.assertEqual((result["verdict"], result["best"]["section"]["id"]), ("OK", "p#b"))
        result = drift.decide_fact(entry, [unrelated, unsure], THRESHOLDS)
        self.assertEqual((result["verdict"], result["best"]["section"]["id"]), ("UNSURE", "p#d"))
        result = drift.decide_fact(entry, [unrelated], THRESHOLDS)
        self.assertEqual(result["verdict"], "MISSING")
        self.assertIn("max covers 0.10", result["note"])
        result = drift.decide_fact(entry, [], THRESHOLDS)
        self.assertEqual((result["verdict"], result["best"], result["documented_anywhere"]), ("MISSING", None, None))
        self.assertIn("no doc section matched", result["note"])
        result = drift.decide_fact(entry, [], THRESHOLDS, failed=2)
        self.assertEqual((result["verdict"], result["failed_pairs"]), ("UNSURE", 2))
        self.assertIn("2 request(s) failed", result["note"])
        result = drift.decide_fact(entry, [unrelated], THRESHOLDS, failed=1)
        self.assertEqual(result["verdict"], "UNSURE")
        result = drift.decide_fact(entry, [drifty], THRESHOLDS, failed=1)
        self.assertEqual(result["verdict"], "DRIFT")

    def test_ranking_puts_drift_first_by_contradiction_then_missing_unsure_ok(self):
        entry = {"id": "f", "area": "env", "statement": "s", "evidence": {"file": "a", "line": 1, "excerpt": ""}}

        def result(fact_id, pairs):
            return drift.decide_fact(dict(entry, id=fact_id), pairs, THRESHOLDS)

        results = [
            result("ok", [pair("p#1", drift.decide_pair(answers(contradicts=0.1, covers=0.9), THRESHOLDS))]),
            result("drift-low", [pair("p#2", drift.decide_pair(answers(contradicts=0.72), THRESHOLDS))]),
            result("missing", []),
            result("unsure", [pair("p#3", drift.decide_pair(answers(contradicts=0.5, covers=0.5), THRESHOLDS))]),
            result("drift-high", [pair("p#4", drift.decide_pair(answers(contradicts=0.95), THRESHOLDS))]),
        ]
        self.assertEqual([entry["id"] for entry in drift.rank(results)], ["drift-high", "drift-low", "missing", "unsure", "ok"])
        summary = drift.summarize(results, pair_count=4, request_count=4)
        self.assertEqual(summary["verdicts"], {"DRIFT": 2, "MISSING": 1, "UNSURE": 1, "OK": 1})
        self.assertEqual(summary["areas"]["env"]["DRIFT"], 2)
        self.assertEqual((summary["facts"], summary["pairs"], summary["requests"]), (5, 4, 4))


class TransportTests(unittest.TestCase):
    class Response:
        def __init__(self, body):
            self.body = body

        def read(self):
            return json.dumps(self.body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def test_http_transport_sends_bearer_key_strips_private_keys_and_retries_on_429(self):
        seen = []
        sleeps = []

        def opener(request, timeout):
            seen.append(request)
            if len(seen) == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"{}"))
            return self.Response(response())

        transport = drift.HttpTransport("secret-key", opener=opener, sleep=sleeps.append)
        result = transport({"state": {"x": 1}, "model": "jev-latest", "questions": {}, "_key": "f|s"})
        self.assertEqual(result["model"], "jev-latest")
        self.assertEqual(len(seen), 2)
        self.assertEqual(sleeps, [1.0])
        request = seen[0]
        self.assertEqual(request.full_url, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")
        self.assertEqual(json.loads(request.data.decode()), {"state": {"x": 1}, "model": "jev-latest", "questions": {}})

    def test_http_transport_does_not_retry_client_errors(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 422, "Unprocessable", {}, io.BytesIO(b'{"detail":"bad question"}'))

        transport = drift.HttpTransport("k", opener=opener, sleep=lambda s: self.fail("should not sleep"))
        with self.assertRaises(drift.DriftError) as raised:
            transport({})
        self.assertIn("422", str(raised.exception))
        self.assertIn("bad question", str(raised.exception))

    def test_http_transport_gives_up_after_the_last_attempt(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 529, "Overloaded", {}, io.BytesIO(b""))

        transport = drift.HttpTransport("k", attempts=3, opener=opener, sleep=lambda s: None)
        with self.assertRaises(drift.DriftError):
            transport({})

    def test_invalid_json_body_is_a_clean_error(self):
        class Broken(self.Response):
            def read(self):
                return b"not json"

        transport = drift.HttpTransport("k", opener=lambda request, timeout: Broken({}), sleep=lambda s: None)
        with self.assertRaises(drift.DriftError) as raised:
            transport({})
        self.assertIn("could not be read", str(raised.exception))

    def test_missing_or_malformed_key_is_rejected_without_echoing_it(self):
        with self.assertRaises(drift.DriftError):
            drift.HttpTransport("")
        secret = "ts-FAKE-KEY-with-newline\n"
        with self.assertRaises(drift.DriftError) as raised:
            drift.HttpTransport(secret)
        self.assertNotIn("ts-FAKE-KEY", str(raised.exception))
        self.assertIn("whitespace", str(raised.exception))

    def test_error_bodies_that_echo_the_key_are_redacted(self):
        secret = "ts-FAKE-KEY-0123456789"

        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {},
                                         io.BytesIO(("bad header Bearer %s" % secret).encode()))

        transport = drift.HttpTransport(secret, opener=opener, sleep=lambda s: None)
        with self.assertRaises(drift.DriftError) as raised:
            transport({})
        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("[redacted]", str(raised.exception))

    def test_redaction_happens_before_the_error_body_is_truncated(self):
        secret = "ts-FAKE-KEY-0123456789"
        body = ("x" * (301 - len(secret)) + secret).encode()

        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(body))

        transport = drift.HttpTransport(secret, opener=opener, sleep=lambda s: None)
        with self.assertRaises(drift.DriftError) as raised:
            transport({})
        self.assertNotIn(secret[:-1], str(raised.exception))
        self.assertIn("[redacted]", str(raised.exception))

    def test_truncated_response_bodies_become_a_drift_error(self):
        import http.client

        class Truncated(self.Response):
            def read(self):
                raise http.client.IncompleteRead(b"partial")

        transport = drift.HttpTransport("ts-FAKE-KEY-0123456789", opener=lambda request, timeout: Truncated({}), sleep=lambda s: None)
        with self.assertRaises(drift.DriftError) as raised:
            transport({})
        self.assertIn("IncompleteRead", str(raised.exception))

    def test_default_opener_is_resolved_at_call_time_so_tests_can_block_it(self):
        transport = drift.HttpTransport("ts-FAKE-KEY-0123456789", sleep=lambda s: None)
        with patch.object(drift.urllib.request, "urlopen", side_effect=urllib.error.URLError("blocked")):
            with self.assertRaises(drift.DriftError) as raised:
                transport({})
        self.assertIn("blocked", str(raised.exception))

    def test_fixture_transport_replays_by_pair_key_and_rejects_unknown_pairs(self):
        transport = drift.FixtureTransport({"f|s": {"answers": {}}})
        self.assertEqual(transport({"_key": "f|s"}), {"answers": {}})
        with self.assertRaises(drift.DriftError):
            transport({"_key": "f|other"})


class RunPairsTests(unittest.TestCase):
    def jobs(self, count):
        jobs = []
        for number in range(count):
            entry = {"id": "env:F%d" % number, "area": "env", "statement": "s", "evidence": {"file": "a", "line": 1, "excerpt": ""}, "terms": []}
            section = {"id": "p#s%d" % number, "page": "p", "title": "T", "heading": "s", "level": 2, "line": 1, "text": "t"}
            jobs.append((entry, section, 1.5))
        return jobs

    def test_concurrent_runs_keep_job_order_and_sum_usage(self):
        lock = threading.Lock()
        seen = []

        def transport(payload):
            with lock:
                seen.append(payload["_key"])
            number = int(payload["_key"].split("|")[0][5:])
            return response(contradicts=0.9 if number % 2 else 0.1, tokens=number)

        outcomes, usage, errors = drift.run_pairs(self.jobs(6), transport, THRESHOLDS, concurrency=3)
        self.assertEqual(errors, [])
        self.assertEqual([outcome["key"] for outcome in outcomes], ["env:F%d|p#s%d" % (n, n) for n in range(6)])
        self.assertEqual(usage["input_tokens"], sum(range(6)))
        self.assertEqual([outcome["decision"]["verdict"] for outcome in outcomes], ["OK", "DRIFT"] * 3)
        self.assertEqual(sorted(seen), sorted("env:F%d|p#s%d" % (n, n) for n in range(6)))
        self.assertEqual(outcomes[0]["lexical_score"], 1.5)

    def test_failures_are_collected_without_losing_completed_pairs(self):
        def transport(payload):
            if payload["_key"].startswith("env:F1|"):
                raise drift.DriftError("HTTP 529 from TypeSafe: overloaded")
            return response()

        outcomes, _usage, errors = drift.run_pairs(self.jobs(3), transport, THRESHOLDS, concurrency=2)
        self.assertEqual(len(errors), 1)
        self.assertIn("env:F1|p#s1", errors[0])
        self.assertEqual([outcome is None for outcome in outcomes], [False, True, False])
        sequential, _usage, errors = drift.run_pairs(self.jobs(3), transport, THRESHOLDS, concurrency=1)
        self.assertEqual([outcome is None for outcome in sequential], [False, True, False])
        self.assertEqual(len(errors), 1)

    def test_sequential_runs_stop_after_consecutive_failures_and_hide_unexpected_error_text(self):
        def transport(payload):
            raise RuntimeError("Bearer ts-SECRET-IN-MESSAGE")

        outcomes, _usage, errors = drift.run_pairs(self.jobs(5), transport, THRESHOLDS, concurrency=1)
        self.assertEqual(outcomes, [None] * 5)
        self.assertEqual(len(errors), drift.MAX_CONSECUTIVE_FAILURES + 1)
        self.assertIn("stopped after", errors[-1])
        self.assertNotIn("SECRET", " ".join(errors))
        self.assertIn("unexpected RuntimeError", errors[0])
        concurrent_outcomes, _usage, errors = drift.run_pairs(self.jobs(4), transport, THRESHOLDS, concurrency=2)
        self.assertEqual(concurrent_outcomes, [None] * 4)
        self.assertEqual(len(errors), 4)

    def test_response_without_answers_is_an_error(self):
        _outcomes, _usage, errors = drift.run_pairs(self.jobs(1), lambda payload: {"model": "x"}, THRESHOLDS)
        self.assertEqual(len(errors), 1)
        self.assertIn("no 'answers' map", errors[0])


class PipelineTests(TempDirs):
    def setUp(self):
        super().setUp()
        self.output = self.base / "out"
        self.patcher = patch.object(drift.urllib.request, "urlopen", side_effect=AssertionError("live TypeSafe call attempted"))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def run_main(self, argv, environ=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = drift.main(argv, environ={} if environ is None else environ, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_facts_only_needs_neither_docs_nor_key(self):
        code, out, err = self.run_main(["--code", str(self.code()), "--facts-only"])
        self.assertEqual(code, 0, err)
        self.assertIn("ncl:widgets-verbs", out)
        self.assertIn("facts from", out)
        self.assertFalse(self.output.exists())
        code, out, _ = self.run_main(["--code", str(self.code()), "--facts-only", "--json", "--areas", "skills"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual({entry["area"] for entry in payload["facts"]}, {"skills"})

    def test_plan_prints_pairs_without_a_key(self):
        code, out, err = self.run_main(["--code", str(self.code()), "--docs", str(self.docs()), "--plan", "--areas", "container-config", "--top-k", "2"])
        self.assertEqual(code, 0, err)
        self.assertIn("container-config:cli-scope-values", out)
        self.assertIn("-> operate/ncl-cli#agents-can-run-ncl-too", out)
        self.assertIn("pairs", out)
        self.assertNotIn("changelog", out)

    def test_missing_key_exits_with_a_clear_message_and_no_request(self):
        code, out, err = self.run_main(["--code", str(self.code()), "--docs", str(self.docs()), "--output-dir", str(self.output)], environ={})
        self.assertEqual(code, 2)
        self.assertIn("TYPESAFE_API_KEY is not set", err)
        self.assertIn("--fixture", err)
        self.assertEqual(out, "")
        self.assertFalse(self.output.exists())

    def test_docs_argument_is_required_for_a_live_run(self):
        with self.assertRaises(SystemExit):
            with patch("sys.stderr", io.StringIO()):
                drift.parse_args(["--code", "."])

    def test_fixture_replay_runs_the_whole_pipeline_offline(self):
        code, out, err = self.run_main(["--fixture", str(FIXTURE), "--output-dir", str(self.output)])
        self.assertEqual(code, 0, err)
        self.assertIn("fixture mode", out)
        self.assertIn("Nothing was written", out)
        for token in ("DRIFT", "MISSING", "UNSURE", "OK", "slightly outdated", "reference/ncl-cli#groups", "tokens: input", "wall time"):
            self.assertIn(token, out)
        saved = sorted(self.output.glob("drift-*.json"))
        self.assertEqual(len(saved), 1)
        payload = json.loads(saved[0].read_text())
        self.assertEqual(payload["mode"], "fixture")
        self.assertEqual(payload["summary"]["verdicts"], {"DRIFT": 1, "MISSING": 2, "UNSURE": 1, "OK": 2})
        self.assertEqual(payload["summary"]["requests"], 6)
        self.assertEqual(payload["usage"]["input_tokens"], 11230)
        self.assertEqual([result["verdict"] for result in payload["results"]][:1], ["DRIFT"])
        first = payload["results"][0]
        self.assertEqual(first["id"], "ncl:groups-config-update-flags")
        self.assertEqual(first["best"]["section"]["page"], "reference/ncl-cli")
        self.assertEqual(first["best"]["decision"]["staleness_label"], "slightly outdated")
        self.assertIn("pairs", first)
        self.assertIn("lexical_score", first["pairs"][0])
        no_candidates = next(result for result in payload["results"] if result["id"] == "timestamps:rule-1")
        self.assertEqual((no_candidates["verdict"], no_candidates["pairs"]), ("MISSING", []))

    def test_fixture_pairs_get_the_documented_questions(self):
        fixture = json.loads(FIXTURE.read_text())
        self.assertEqual(len(fixture["facts"]), 6)
        self.assertEqual(len(fixture["responses"]), 6)
        jobs = drift.plan_pairs(fixture["facts"], fixture["sections"], fixture["candidates"], fixture["scores"])
        transport = drift.FixtureTransport(fixture["responses"])
        drift.run_pairs(jobs, transport, THRESHOLDS)
        self.assertEqual(len(transport.requests), 6)
        for request in transport.requests:
            self.assertEqual(request["model"], "jev-latest")
            self.assertEqual(set(request["questions"]), {"contradicts", "covers", "staleness"})
            self.assertEqual(request["_key"].split("|")[1], request["state"]["doc_section"]["page"] + "#" + request["_key"].split("#")[1])
        for answer_map in fixture["responses"].values():
            for name, answer in answer_map["answers"].items():
                if answer["type"] == "score":
                    self.assertAlmostEqual(sum(answer["probabilities"].values()), 1.0, places=3, msg=name)
                    self.assertTrue(0 <= answer["confidence"] <= 1)
                else:
                    self.assertTrue(0 <= answer["noul"] <= 1)

    def test_flags_override_the_gate_and_areas_filter_the_fixture(self):
        code, out, _ = self.run_main(["--fixture", str(FIXTURE), "--output-dir", str(self.output), "--json", "--contradicts-threshold", "0.8"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["summary"]["verdicts"]["DRIFT"], 0)
        self.assertEqual(payload["summary"]["verdicts"]["UNSURE"], 2)
        code, out, _ = self.run_main(["--fixture", str(FIXTURE), "--output-dir", str(self.output), "--json", "--areas", "gateway"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["summary"]["facts"], 2)
        self.assertEqual(set(payload["summary"]["areas"]), {"gateway"})

    def test_fixture_mode_honours_limit_plan_and_facts_only(self):
        code, out, err = self.run_main(["--fixture", str(FIXTURE), "--output-dir", str(self.output), "--json", "--limit-facts", "1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["summary"]["facts"], 1)
        self.assertEqual(len(list(self.output.glob("drift-*.json"))), 1)
        code, out, _ = self.run_main(["--fixture", str(FIXTURE), "--output-dir", str(self.output), "--plan"])
        self.assertEqual(code, 0)
        self.assertIn("-> reference/ncl-cli#groups", out)
        code, out, _ = self.run_main(["--fixture", str(FIXTURE), "--output-dir", str(self.output), "--facts-only"])
        self.assertEqual(code, 0)
        self.assertIn("ncl:groups-config-update-flags", out)
        self.assertEqual(len(list(self.output.glob("drift-*.json"))), 1)

    def test_invalid_flags_are_rejected(self):
        for argv in (["--contradicts-threshold", "0.4"], ["--covers-threshold", "1.5"], ["--top-k", "0"], ["--areas", "bogus"], ["--concurrency", "0"]):
            with self.assertRaises(SystemExit, msg=argv):
                with patch("sys.stderr", io.StringIO()):
                    drift.parse_args(["--fixture", "x"] + argv)

    def test_key_never_appears_in_output_or_saved_json(self):
        sentinel = "ts-live-FAKE-KEY-0123456789"
        code, out, err = self.run_main(["--fixture", str(FIXTURE), "--output-dir", str(self.output)], environ={"TYPESAFE_API_KEY": sentinel})
        self.assertEqual(code, 0)
        saved = next(self.output.glob("drift-*.json")).read_text()
        for text in (out, err, saved):
            self.assertNotIn(sentinel, text)

    def test_partial_failure_keeps_completed_pairs_and_exits_nonzero(self):
        fixture = json.loads(FIXTURE.read_text())
        original = drift.FixtureTransport(fixture["responses"])

        def flaky(payload):
            if payload["_key"].startswith("env:CONTAINER_PIDS_LIMIT|"):
                raise drift.DriftError("HTTP 529 from TypeSafe: overloaded")
            return original(payload)

        with patch.object(drift, "FixtureTransport", return_value=flaky):
            code, out, err = self.run_main(["--fixture", str(FIXTURE), "--output-dir", str(self.output), "--json"])
        self.assertEqual(code, 1)
        self.assertIn("keeping 5 completed pair(s)", err)
        payload = json.loads(out)
        self.assertIn("env:CONTAINER_PIDS_LIMIT|", payload["partial"])
        self.assertEqual(payload["summary"]["requests"], 5)
        self.assertEqual(payload["summary"]["verdicts"]["DRIFT"], 1)
        pids = next(result for result in payload["results"] if result["id"] == "env:CONTAINER_PIDS_LIMIT")
        self.assertEqual((pids["verdict"], pids["pairs"], pids["failed_pairs"]), ("UNSURE", [], 1))
        self.assertIn("request(s) failed", pids["note"])
        self.assertEqual(len(list(self.output.glob("drift-*.json"))), 1)

    def test_output_files_never_overwrite_each_other(self):
        with patch.object(drift, "stamp", return_value="fixed"):
            first = drift.write_output(self.output, {"a": 1})
            second = drift.write_output(self.output, {"a": 2})
        self.assertNotEqual(first, second)
        self.assertEqual(json.loads(first.read_text()), {"a": 1})
        self.assertEqual(json.loads(second.read_text()), {"a": 2})

    def test_bad_fixture_is_a_clean_error(self):
        broken = self.base / "broken.json"
        broken.write_text("[]")
        code, out, err = self.run_main(["--fixture", str(broken), "--output-dir", str(self.output)])
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        self.assertIn("'facts'", err)

    def test_live_mode_with_mocked_transport_records_a_replayable_fixture(self):
        def fake(payload):
            if payload["_key"].startswith("env:LIMIT|"):
                raise drift.DriftError("HTTP 529 from TypeSafe: overloaded")
            return response(contradicts=0.9 if "cli-scope" in payload["_key"] else 0.1)
        record = self.base / "record.json"
        with patch.object(drift, "HttpTransport", return_value=fake):
            code, out, err = self.run_main(
                ["--code", str(self.code()), "--docs", str(self.docs()), "--output-dir", str(self.output),
                 "--areas", "container-config,env", "--top-k", "1", "--record", str(record), "--json"],
                environ={"TYPESAFE_API_KEY": "ts-FAKE-KEY-0123456789"})
        self.assertEqual(code, 1, err)
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "live")
        self.assertIn("[1/", err)
        self.assertIn("env:LIMIT|", payload["partial"])
        limit = next(result for result in payload["results"] if result["id"] == "env:LIMIT")
        self.assertEqual((limit["verdict"], limit["failed_pairs"]), ("UNSURE", 1))
        self.assertGreaterEqual(payload["summary"]["verdicts"]["DRIFT"], 1)
        self.assertIn(".env.example", " ".join(payload["notes"])) if not payload["notes"] else None
        self.assertIn("gateway-seam.md is absent", " ".join(payload["notes"]))
        recorded = json.loads(record.read_text())
        self.assertEqual(set(recorded), {"schema_version", "recorded_at", "code_commit", "docs_commit", "facts", "sections", "candidates", "scores", "responses"})
        self.assertEqual(len(recorded["responses"]), payload["summary"]["requests"])
        self.assertEqual(recorded["candidates"]["env:LIMIT"], [])
        self.assertEqual(sum(len(ids) for ids in recorded["candidates"].values()), len(recorded["responses"]))
        self.assertEqual(set(recorded["scores"]), set(recorded["responses"]))
        code, replay_out, err = self.run_main(["--fixture", str(record), "--output-dir", str(self.output), "--json"])
        self.assertEqual(code, 0, err)
        replayed = json.loads(replay_out)["summary"]["verdicts"]
        self.assertEqual(replayed["DRIFT"], payload["summary"]["verdicts"]["DRIFT"])
        self.assertEqual(replayed["OK"], payload["summary"]["verdicts"]["OK"])


if __name__ == "__main__":
    unittest.main()
