"""TypeSafe triage dry run: questions, gate, fixture replay and transport, all offline."""

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/typesafe-triage/scripts/typesafe-triage.py"
FIXTURE = ROOT / "skills/typesafe-triage/fixtures/nanoclaw-sample.json"


def load():
    spec = importlib.util.spec_from_file_location("typesafe_triage_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


triage = load()
THRESHOLDS = {"area": 0.6, "kind": 0.6, "priority": 0.6, "noul": 0.7}


def choice(option, confidence, options):
    rest = (1 - 0.9) / (len(options) - 1)
    probabilities = {name: (0.9 if name == option else rest) for name in options}
    return {"type": "choice", "choice": option, "probabilities": probabilities, "confidence": confidence}


def score(value, confidence):
    return {"type": "score", "score": value, "legend": {}, "probabilities": {}, "confidence": confidence}


def answers(area="area/core", area_conf=0.9, kind="kind/bug", kind_conf=0.9, priority=1.0, priority_conf=0.9, noul=0.5, noul_name="needs_repro"):
    return {
        "area": choice(area, area_conf, list(triage.AREA_CRITERIA)),
        "kind": choice(kind, kind_conf, list(triage.KIND_CRITERIA)),
        "priority": score(priority, priority_conf),
        noul_name: {"type": "noul", "noul": noul},
    }


def issue(number=1, labels=(), body="body"):
    return {"type": "issue", "number": number, "title": "Issue %d" % number, "body": body,
            "author_association": "NONE", "labels": sorted(labels), "url": "", "updated_at": ""}


def pull(number=2, labels=(), body="body", files=("src/router.ts",)):
    item = issue(number, labels, body)
    item.update({"type": "pr", "draft": False, "files": list(files), "file_count": len(files),
                 "template": triage.template_status(body)})
    return item


class QuestionTests(unittest.TestCase):
    def test_area_and_kind_criteria_cover_the_nanoclaw_label_taxonomy(self):
        questions = triage.build_questions("issue")
        expected_areas = {
            "area/agent-memory", "area/agent-runner", "area/channels", "area/configuration", "area/containers",
            "area/core", "area/credentials", "area/ncl-cli", "area/providers", "area/repository-maintenance",
            "area/scheduled-tasks", "area/security", "area/sessions", "area/setup-installation", "area/skills",
            "area/tools",
        }
        self.assertEqual(set(questions["area"]["criteria"]), expected_areas)
        self.assertEqual(set(questions["kind"]["criteria"]), {
            "kind/bug", "kind/feature", "kind/documentation", "kind/question", "kind/security",
            "kind/hardening", "kind/cleanup",
        })
        for criteria in (questions["area"]["criteria"], questions["kind"]["criteria"]):
            for option, rubric in criteria.items():
                self.assertTrue(rubric and "\n" not in rubric, option)
        self.assertEqual(questions["priority"]["type"], "score")
        self.assertEqual(len(questions["priority"]["criteria"]), 4)
        self.assertEqual(triage.PRIORITY_LABELS, ("priority/low", "priority/medium", "priority/high", "priority/critical"))

    def test_issues_ask_needs_repro_and_prs_ask_pr_ready(self):
        self.assertIn("needs_repro", triage.build_questions("issue"))
        self.assertNotIn("pr_ready", triage.build_questions("issue"))
        self.assertIn("pr_ready", triage.build_questions("pr"))
        self.assertNotIn("needs_repro", triage.build_questions("pr"))
        for item_type in ("issue", "pr"):
            for question in triage.build_questions(item_type).values():
                self.assertIn(question["type"], ("noul", "choice", "score"))
                self.assertTrue(question["instructions"])

    def test_request_shape_matches_the_documented_api(self):
        request = triage.build_request("nanocoai/nanoclaw", pull(body="x" * 20000))
        self.assertEqual(set(request), {"state", "model", "questions"})
        self.assertEqual(request["model"], "jev-latest")
        state = request["state"]
        self.assertEqual(state["item_type"], "pull_request")
        self.assertEqual(len(state["body"]), triage.BODY_LIMIT)
        self.assertEqual(state["changed_files"], ["src/router.ts"])
        self.assertIn("sections", state["template"])
        self.assertNotIn("labels", state)
        self.assertNotIn("existing_labels", state)


class TemplateTests(unittest.TestCase):
    TEMPLATE = (
        "<!-- nanoclaw-pr-template:v2 -->\n\n## Summary\n\n<!-- Shape, by example -->\n\n"
        "## Related work\n\nCloses #\n\n## Change kind\n\n- [ ] `kind/bug`\n- [ ] `kind/feature`\n\n"
        "## Validation\n\n- [ ] Tests cover the changed behavior\n\n## User and release impact\n\n"
        "- [ ] No user-visible behavior change\n\n```release-note\nOptional: one user-facing line for the changelog.\n```\n\n"
        "## Security and trust boundaries\n\n<!-- or None -->\n\n## Skill delivery\n\n- [ ] Not a skill\n\n"
        "## AI assistance\n\n- [ ] AI tools or agents helped produce this change\n"
    )

    def test_untouched_template_counts_every_section_as_empty(self):
        status = triage.template_status(self.TEMPLATE)
        self.assertTrue(status["marker"])
        self.assertEqual(status["filled"], 0)
        self.assertEqual(set(status["sections"].values()), {"empty"})

    def test_filled_template_counts_content_and_checked_boxes(self):
        body = (self.TEMPLATE
                .replace("## Summary\n\n<!-- Shape, by example -->\n", "## Summary\n\nStops the sweep killing turns.\n- **Fix**: poll tick.\n")
                .replace("- [ ] `kind/bug`", "- [x] `kind/bug`")
                .replace("- [ ] Tests cover the changed behavior", "- [x] Tests cover the changed behavior\n- `pnpm test` -> 12 passed"))
        status = triage.template_status(body)
        self.assertEqual(status["sections"]["Summary"], "filled")
        self.assertEqual(status["sections"]["Change kind"], "filled")
        self.assertEqual(status["sections"]["Validation"], "filled")
        self.assertEqual(status["sections"]["Related work"], "empty")
        self.assertEqual(status["filled"], 3)

    def test_nested_headings_stay_inside_their_section(self):
        body = self.TEMPLATE.replace("## Validation\n\n", "## Validation\n\n### Tests\n\nAll tests passed.\n\n")
        status = triage.template_status(body)
        self.assertEqual(status["sections"]["Validation"], "filled")
        self.assertEqual(status["sections"]["User and release impact"], "empty")
        self.assertIn("Tests", status["headings"])

    def test_body_without_the_template_reports_missing_sections_and_its_own_headings(self):
        status = triage.template_status("## Problem\n\nIt breaks.\n\n## Testing\n\nRan it.\n")
        self.assertFalse(status["marker"])
        self.assertEqual(status["filled"], 0)
        self.assertEqual(set(status["sections"].values()), {"missing"})
        self.assertEqual(status["headings"], ["Problem", "Testing"])
        self.assertEqual(triage.template_status(None)["headings"], [])


class DecisionTests(unittest.TestCase):
    def proposals(self, item, answer_map):
        return {p["question"]: p for p in triage.decide(item, answer_map, THRESHOLDS)}

    def test_confident_answers_agree_with_matching_existing_labels(self):
        item = issue(labels=["area/core", "kind/bug", "priority/medium"])
        result = self.proposals(item, answers(priority=1.2))
        self.assertEqual((result["area"]["label"], result["area"]["verdict"]), ("area/core", "AGREE"))
        self.assertEqual((result["kind"]["label"], result["kind"]["verdict"]), ("kind/bug", "AGREE"))
        self.assertEqual((result["priority"]["label"], result["priority"]["verdict"]), ("priority/medium", "AGREE"))

    def test_different_label_in_the_same_family_is_a_disagreement_and_no_label_is_new(self):
        item = issue(labels=["area/channels"])
        result = self.proposals(item, answers())
        self.assertEqual(result["area"]["verdict"], "DISAGREE")
        self.assertEqual(result["area"]["existing"], ["area/channels"])
        self.assertEqual(result["kind"]["verdict"], "NEW")

    def test_low_confidence_proposes_unresolved_instead_of_guessing(self):
        result = self.proposals(issue(), answers(area_conf=0.59, kind_conf=0.6, priority_conf=0.59))
        self.assertEqual(result["area"]["label"], "triage/unresolved")
        self.assertTrue(result["area"]["gated"])
        self.assertIn("area/core", result["area"]["note"])
        self.assertEqual(result["kind"]["label"], "kind/bug")
        self.assertFalse(result["kind"]["gated"])
        self.assertEqual(result["priority"]["label"], "triage/unresolved")
        self.assertTrue(result["priority"]["gated"])

    def test_thresholds_are_overridable(self):
        thresholds = dict(THRESHOLDS, area=0.3, priority=0.5)
        result = {p["question"]: p for p in triage.decide(issue(), answers(area_conf=0.35, priority_conf=0.55), thresholds)}
        self.assertEqual(result["area"]["label"], "area/core")
        self.assertEqual(result["priority"]["label"], "priority/medium")

    def test_score_rounds_to_the_nearest_priority_level(self):
        self.assertEqual(triage.priority_label(0.4), "priority/low")
        self.assertEqual(triage.priority_label(1.4), "priority/medium")
        self.assertEqual(triage.priority_label(1.5), "priority/high")
        self.assertEqual(triage.priority_label(2.4), "priority/high")
        self.assertEqual(triage.priority_label(2.6), "priority/critical")
        self.assertEqual(triage.priority_label(9), "priority/critical")
        self.assertEqual(triage.priority_label(-1), "priority/low")

    def test_unknown_choice_from_the_api_is_gated(self):
        result = self.proposals(issue(), answers(area="area/does-not-exist", area_conf=0.99))
        self.assertEqual(result["area"]["label"], "triage/unresolved")
        self.assertTrue(result["area"]["gated"])

    def test_missing_answers_are_gated_rather_than_crashing(self):
        result = self.proposals(issue(), {})
        for question in ("area", "kind", "priority", "needs_repro"):
            self.assertEqual(result[question]["label"], "triage/unresolved", question)
            self.assertTrue(result[question]["gated"])

    def test_needs_repro_thresholds(self):
        yes = self.proposals(issue(), answers(noul=0.7))["needs_repro"]
        self.assertEqual((yes["label"], yes["verdict"], yes["gated"]), ("triage/needs-repro", "NEW", False))
        agreed = self.proposals(issue(labels=["triage/needs-repro"]), answers(noul=0.9))["needs_repro"]
        self.assertEqual(agreed["verdict"], "AGREE")
        no = self.proposals(issue(), answers(noul=0.3))["needs_repro"]
        self.assertEqual((no["label"], no["verdict"], no["gated"]), (None, "AGREE", False))
        stale = self.proposals(issue(labels=["triage/needs-repro"]), answers(noul=0.1))["needs_repro"]
        self.assertEqual(stale["verdict"], "DISAGREE")
        unsure = self.proposals(issue(), answers(noul=0.5))["needs_repro"]
        self.assertEqual((unsure["label"], unsure["gated"]), ("triage/unresolved", True))

    def test_needs_repro_is_skipped_when_the_item_is_not_a_bug(self):
        result = self.proposals(issue(), answers(kind="kind/feature", noul=0.95))["needs_repro"]
        self.assertEqual((result["label"], result["verdict"]), (None, "SKIP"))
        result = self.proposals(issue(), answers(kind="kind/security", noul=0.95))["needs_repro"]
        self.assertEqual(result["label"], "triage/needs-repro")

    def test_pr_ready_maps_to_review_or_author_labels(self):
        ready = self.proposals(pull(), answers(noul=0.85, noul_name="pr_ready"))["pr_ready"]
        self.assertEqual((ready["label"], ready["verdict"]), ("Status: Needs Review", "NEW"))
        stalled = self.proposals(pull(), answers(noul=0.2, noul_name="pr_ready"))["pr_ready"]
        self.assertEqual(stalled["label"], "triage/needs-author")
        self.assertAlmostEqual(stalled["confidence"], 0.8)
        unsure = self.proposals(pull(), answers(noul=0.6, noul_name="pr_ready"))["pr_ready"]
        self.assertEqual((unsure["label"], unsure["gated"]), ("triage/unresolved", True))
        self.assertNotIn("needs_repro", self.proposals(pull(), answers(noul=0.6, noul_name="pr_ready")))

    def test_summary_reports_agreement_per_question_and_gated_items(self):
        results = [
            {"proposals": triage.decide(issue(1, ["area/core", "kind/feature"]), answers(), THRESHOLDS)},
            {"proposals": triage.decide(issue(2, ["area/core"]), answers(area_conf=0.2), THRESHOLDS)},
            {"proposals": triage.decide(issue(3), answers(kind="kind/feature"), THRESHOLDS)},
        ]
        summary = triage.summarize(results)
        self.assertEqual(summary["items"], 3)
        # needs_repro at 0.5 is uncertain on the two bug items; the feature item skips it
        self.assertEqual(summary["items_below_gate"], 2)
        area = summary["questions"]["area"]
        self.assertEqual((area["agree"], area["disagree"], area["new"], area["below_gate"]), (1, 0, 1, 1))
        self.assertEqual(area["agreement_rate"], 1.0)
        kind = summary["questions"]["kind"]
        self.assertEqual((kind["agree"], kind["disagree"], kind["new"]), (0, 1, 2))
        self.assertEqual(kind["agreement_rate"], 0.0)
        self.assertIsNone(summary["questions"]["priority"]["agreement_rate"])


class FetchTests(unittest.TestCase):
    def test_fetch_items_uses_gh_api_read_only_and_drops_prs_from_the_issues_endpoint(self):
        calls = []

        class Result:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def run(command, capture_output, text):
            calls.append(command)
            self.assertEqual(command[:2], ["gh", "api"])
            path = command[2]
            self.assertIn("per_page=100&page=", path)
            if path.startswith("repos/o/r/issues?"):
                return Result(json.dumps([
                    {"number": 5, "title": "Real issue", "body": "steps", "author_association": "MEMBER",
                     "labels": [{"name": "kind/bug"}], "html_url": "u5", "updated_at": "t"},
                    {"number": 6, "title": "PR in disguise", "body": "", "author_association": "NONE",
                     "labels": [], "html_url": "u6", "updated_at": "t", "pull_request": {}},
                    {"number": 4, "title": "Older issue", "body": None, "author_association": "NONE",
                     "labels": [], "html_url": "u4", "updated_at": "t"},
                ]))
            if path.startswith("repos/o/r/pulls?"):
                return Result(json.dumps([
                    {"number": 7, "title": "feat: x", "body": "<!-- nanoclaw-pr-template:v2 -->\n## Summary\n\nDoes x.\n",
                     "author_association": "CONTRIBUTOR", "labels": [{"name": "area/core"}], "html_url": "u7",
                     "updated_at": "t", "draft": True},
                ]))
            if path == "repos/o/r/pulls/7/files?per_page=100&page=1":
                return Result(json.dumps([{"filename": "src/router.ts"}, {"filename": "docs/x.md"}]))
            self.fail("unexpected gh call %s" % path)

        items = triage.fetch_items("o/r", 1, 1, run=run)
        self.assertEqual([(item["type"], item["number"]) for item in items], [("issue", 5), ("pr", 7)])
        self.assertEqual(items[0]["labels"], ["kind/bug"])
        self.assertEqual(items[1]["files"], ["src/router.ts", "docs/x.md"])
        self.assertTrue(items[1]["draft"])
        self.assertTrue(items[1]["template"]["marker"])
        self.assertEqual(items[1]["template"]["sections"]["Summary"], "filled")
        for command in calls:
            self.assertNotIn("-X", command)
            self.assertNotIn("--method", command)
            self.assertNotIn("-f", command)

    def test_issue_fetch_keeps_paging_past_pull_requests(self):
        pages = {
            1: [{"number": 100 - n, "title": "pr", "body": "", "author_association": "NONE", "labels": [],
                 "html_url": "", "updated_at": "", "pull_request": {}} for n in range(100)],
            2: [{"number": 3, "title": "issue", "body": "", "author_association": "NONE", "labels": [],
                 "html_url": "", "updated_at": ""}] + [
                {"number": 2 - n, "title": "pr", "body": "", "author_association": "NONE", "labels": [],
                 "html_url": "", "updated_at": "", "pull_request": {}} for n in range(2)],
        }

        class Result:
            returncode = 0
            stderr = ""

            def __init__(self, stdout):
                self.stdout = stdout

        def run(command, capture_output, text):
            page = int(command[2].rsplit("page=", 1)[1])
            return Result(json.dumps(pages[page]))

        items = triage.fetch_items("o/r", 5, 0, run=run)
        self.assertEqual([item["number"] for item in items], [3])

    def test_gh_failure_is_reported_as_a_triage_error(self):
        class Failed:
            returncode = 1
            stdout = ""
            stderr = "gh: Not Found (HTTP 404)\n"

        with self.assertRaises(triage.TriageError) as raised:
            triage.gh_json("repos/o/r/issues", run=lambda *a, **k: Failed())
        self.assertIn("HTTP 404", str(raised.exception))


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

    def test_http_transport_sends_bearer_key_and_retries_on_429(self):
        seen = []
        sleeps = []

        def opener(request, timeout):
            seen.append(request)
            if len(seen) == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"{}"))
            return self.Response({"model": "jev-latest", "answers": {}, "usage": {"input_tokens": 1, "output_tokens": 1}})

        transport = triage.HttpTransport("secret-key", opener=opener, sleep=sleeps.append)
        result = transport({"state": {"x": 1}, "model": "jev-latest", "questions": {}})
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

        transport = triage.HttpTransport("k", opener=opener, sleep=lambda s: self.fail("should not sleep"))
        with self.assertRaises(triage.TriageError) as raised:
            transport({})
        self.assertIn("422", str(raised.exception))
        self.assertIn("bad question", str(raised.exception))

    def test_http_transport_gives_up_after_the_last_attempt(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 529, "Overloaded", {}, io.BytesIO(b""))

        transport = triage.HttpTransport("k", attempts=3, opener=opener, sleep=lambda s: None)
        with self.assertRaises(triage.TriageError):
            transport({})

    def test_missing_key_is_rejected_before_any_request(self):
        with self.assertRaises(triage.TriageError):
            triage.HttpTransport("")

    def test_key_with_whitespace_is_rejected_without_echoing_it(self):
        secret = "ts-FAKE-KEY-with-newline\n"
        with self.assertRaises(triage.TriageError) as raised:
            triage.HttpTransport(secret)
        self.assertNotIn("ts-FAKE-KEY", str(raised.exception))
        self.assertIn("whitespace", str(raised.exception))

    def test_error_bodies_that_echo_the_key_are_redacted(self):
        secret = "ts-FAKE-KEY-0123456789"

        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {},
                                         io.BytesIO(("bad header Bearer %s" % secret).encode()))

        transport = triage.HttpTransport(secret, opener=opener, sleep=lambda s: None)
        with self.assertRaises(triage.TriageError) as raised:
            transport({})
        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("[redacted]", str(raised.exception))

    def test_default_opener_is_resolved_at_call_time_so_tests_can_block_it(self):
        transport = triage.HttpTransport("ts-FAKE-KEY-0123456789", sleep=lambda s: None)
        with patch.object(triage.urllib.request, "urlopen", side_effect=urllib.error.URLError("blocked")):
            with self.assertRaises(triage.TriageError) as raised:
                transport({})
        self.assertIn("blocked", str(raised.exception))

    def test_fixture_transport_replays_by_item_key_and_rejects_unknown_items(self):
        transport = triage.FixtureTransport({"issue:1": {"answers": {}}})
        payload = triage.build_request("o/r", issue(1))
        self.assertEqual(transport(payload), {"answers": {}})
        with self.assertRaises(triage.TriageError):
            transport(triage.build_request("o/r", pull(1)))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="typesafe-triage-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "out"
        self.patcher = patch.object(triage.urllib.request, "urlopen",
                                    side_effect=AssertionError("live TypeSafe call attempted"))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def run_main(self, argv, environ=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = triage.main(argv, environ={} if environ is None else environ, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_missing_key_exits_with_a_clear_message_and_no_fetch(self):
        with patch.object(triage, "fetch_items", side_effect=AssertionError("gh should not run")):
            code, out, err = self.run_main(["--output-dir", str(self.output)], environ={})
        self.assertEqual(code, 2)
        self.assertIn("TYPESAFE_API_KEY is not set", err)
        self.assertIn("--fixture", err)
        self.assertEqual(out, "")
        self.assertFalse(self.output.exists())

    def test_fixture_replay_runs_the_whole_pipeline_offline(self):
        code, out, err = self.run_main(["--fixture", str(FIXTURE), "--output-dir", str(self.output)])
        self.assertEqual(code, 0, err)
        self.assertIn("fixture mode", out)
        self.assertIn("No labels were written", out)
        for token in ("AGREE", "NEW", "triage/unresolved", "triage/needs-repro", "Status: Needs Review",
                      "agreement", "tokens: input", "wall time"):
            self.assertIn(token, out)
        saved = sorted(self.output.glob("triage-*.json"))
        self.assertEqual(len(saved), 1)
        payload = json.loads(saved[0].read_text())
        self.assertEqual(payload["mode"], "fixture")
        self.assertEqual(payload["repo"], "nanocoai/nanoclaw")
        self.assertEqual(len(payload["results"]), 5)
        self.assertEqual(payload["usage"]["input_tokens"], 10960)
        self.assertEqual(set(payload["thresholds"]), {"area", "kind", "priority", "noul"})
        for result in payload["results"]:
            self.assertIn("answers", result["response"])
            self.assertTrue(result["proposals"])

    def test_fixture_items_get_the_documented_questions(self):
        fixture = json.loads(FIXTURE.read_text())
        self.assertEqual(len(fixture["items"]), 5)
        transport = triage.FixtureTransport(fixture["responses"])
        triage.evaluate(fixture["repo"], fixture["items"], transport, THRESHOLDS)
        for request in transport.requests:
            self.assertEqual(request["model"], "jev-latest")
            expected = "pr_ready" if request["state"]["item_type"] == "pull_request" else "needs_repro"
            self.assertEqual(set(request["questions"]), {"area", "kind", "priority", expected})
        for response in fixture["responses"].values():
            for name, answer in response["answers"].items():
                if answer["type"] in ("choice", "score"):
                    self.assertAlmostEqual(sum(answer["probabilities"].values()), 1.0, places=3, msg=name)
                    self.assertTrue(0 <= answer["confidence"] <= 1)
                else:
                    self.assertTrue(0 <= answer["noul"] <= 1)

    def test_fixture_summary_shows_agreement_with_existing_labels(self):
        code, out, _ = self.run_main(["--fixture", str(FIXTURE), "--output-dir", str(self.output), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        summary = payload["summary"]["questions"]
        self.assertEqual(summary["area"]["agree"], 3)
        self.assertEqual(summary["area"]["disagree"], 0)
        self.assertEqual(summary["area"]["below_gate"], 1)
        self.assertEqual(summary["needs_repro"]["agree"], 1)
        self.assertEqual(summary["priority"]["below_gate"], 2)
        self.assertEqual(payload["summary"]["items_below_gate"], 4)

    def test_flags_override_the_gate(self):
        code, out, _ = self.run_main(["--fixture", str(FIXTURE), "--output-dir", str(self.output),
                                      "--priority-threshold", "0.5", "--area-threshold", "0.4", "--json"])
        self.assertEqual(code, 0)
        summary = json.loads(out)["summary"]["questions"]
        self.assertEqual(summary["priority"]["below_gate"], 0)
        self.assertEqual(summary["area"]["below_gate"], 0)

    def test_invalid_threshold_is_rejected(self):
        with self.assertRaises(SystemExit):
            with patch("sys.stderr", io.StringIO()):
                triage.parse_args(["--noul-threshold", "0.4"])

    def test_key_never_appears_in_output_or_saved_json(self):
        sentinel = "ts-live-FAKE-KEY-0123456789"
        code, out, err = self.run_main(["--fixture", str(FIXTURE), "--output-dir", str(self.output)],
                                       environ={"TYPESAFE_API_KEY": sentinel})
        self.assertEqual(code, 0)
        saved = next(self.output.glob("triage-*.json")).read_text()
        for text in (out, err, saved):
            self.assertNotIn(sentinel, text)

    def test_partial_failure_keeps_completed_results_and_exits_nonzero(self):
        fixture = json.loads(FIXTURE.read_text())
        responses = dict(fixture["responses"])
        failing = triage.FixtureTransport(responses)
        original = failing.__call__

        def flaky(payload):
            if payload["state"]["number"] == 3814:
                raise triage.TriageError("HTTP 529 from TypeSafe: overloaded")
            return original(payload)

        with patch.object(triage, "FixtureTransport", return_value=flaky):
            code, out, err = self.run_main(["--fixture", str(FIXTURE), "--output-dir", str(self.output), "--json"])
        self.assertEqual(code, 1)
        self.assertIn("keeping 2 completed item(s)", err)
        payload = json.loads(out)
        self.assertEqual(len(payload["results"]), 2)
        self.assertIn("issue:3814 failed", payload["partial"])
        self.assertEqual(len(list(self.output.glob("triage-*.json"))), 1)

    def test_output_files_never_overwrite_each_other(self):
        with patch.object(triage, "stamp", return_value="fixed"):
            first = triage.write_output(self.output, {"a": 1})
            second = triage.write_output(self.output, {"a": 2})
        self.assertNotEqual(first, second)
        self.assertEqual(json.loads(first.read_text()), {"a": 1})
        self.assertEqual(json.loads(second.read_text()), {"a": 2})

    def test_bad_fixture_is_a_clean_error(self):
        broken = Path(self.temporary.name) / "broken.json"
        broken.write_text("[]")
        code, out, err = self.run_main(["--fixture", str(broken), "--output-dir", str(self.output)])
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        self.assertIn("'items'", err)


if __name__ == "__main__":
    unittest.main()
