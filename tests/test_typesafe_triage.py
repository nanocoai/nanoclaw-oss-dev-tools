"""TypeSafe triage dry run: questions, gate, fixture replay and transport, all offline."""

import importlib.util
import io
import json
import os
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


def issue(number=1, labels=(), body="body", created_at=""):
    return {"type": "issue", "number": number, "title": "Issue %d" % number, "body": body,
            "author_association": "NONE", "labels": sorted(labels), "url": "", "updated_at": "",
            "created_at": created_at}


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


class FakeGhResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def recording_run(calls, current_labels_by_number=None):
    """Fake ``run`` for gh_add_label / current_labels: records every command and, for the
    ``gh api repos/<repo>/issues/<n>`` label-recheck read, serves labels from the given map
    (default: none, i.e. the item still looks unlabeled at write time)."""
    current_labels_by_number = current_labels_by_number or {}

    def run(command, capture_output, text):
        calls.append(command)
        if command[:2] == ["gh", "api"]:
            number = int(command[2].rsplit("/", 1)[-1])
            names = current_labels_by_number.get(number, [])
            return FakeGhResult(stdout=json.dumps({"labels": [{"name": name} for name in names]}))
        return FakeGhResult()
    return run


class ApplyTests(unittest.TestCase):
    """labels_to_apply / gh_add_label / apply_item_labels: pure decision + the gh call shape."""

    def test_ungated_kind_and_yes_needs_repro_are_both_eligible_on_a_fresh_issue(self):
        item = issue(1)
        proposals = triage.decide(item, answers(kind="kind/bug", kind_conf=0.9, noul=0.9), THRESHOLDS)
        self.assertEqual(triage.labels_to_apply(item, proposals), ["kind/bug", "triage/needs-repro"])

    def test_kind_is_skipped_when_a_kind_label_already_exists(self):
        item = issue(1, labels=["kind/feature"])
        proposals = triage.decide(item, answers(kind="kind/bug", kind_conf=0.9, noul=0.9), THRESHOLDS)
        applied = triage.labels_to_apply(item, proposals)
        self.assertNotIn("kind/bug", applied)

    def test_needs_repro_is_skipped_when_already_present(self):
        item = issue(1, labels=["triage/needs-repro"])
        proposals = triage.decide(item, answers(noul=0.9), THRESHOLDS)
        self.assertNotIn("triage/needs-repro", triage.labels_to_apply(item, proposals))

    def test_gated_kind_is_never_applied(self):
        item = issue(1)
        proposals = triage.decide(item, answers(kind_conf=0.1, noul=0.9), THRESHOLDS)
        applied = triage.labels_to_apply(item, proposals)
        self.assertNotIn("kind/bug", applied)
        self.assertNotIn(triage.UNRESOLVED, applied)

    def test_uncertain_needs_repro_is_never_applied(self):
        item = issue(1)
        proposals = triage.decide(item, answers(noul=0.5), THRESHOLDS)
        self.assertEqual([l for l in triage.labels_to_apply(item, proposals) if "repro" in l], [])

    def test_needs_repro_is_withheld_when_kind_itself_is_gated(self):
        # decide()'s needs_repro branch only checks the model's raw top kind CHOICE, not
        # whether that choice cleared its own confidence gate. If kind is "kind/bug" at 0.1
        # confidence (gated, shown as triage/unresolved in the table), needs_repro's premise
        # ("this is a confirmed bug report") isn't actually established, so --apply must
        # withhold it even though decide() itself proposed NEEDS_REPRO ungated.
        item = issue(1)
        proposals = triage.decide(item, answers(kind_conf=0.1, noul=0.95), THRESHOLDS)
        self.assertEqual([l for l in triage.labels_to_apply(item, proposals) if "repro" in l], [])

    def test_needs_repro_is_withheld_when_there_is_no_kind_answer_at_all(self):
        # Same underlying issue as the gated-kind case above, for the degenerate response shape
        # where the "kind" question has no answer at all: decide()'s SKIP check explicitly
        # exempts a missing kind answer (treats it like a confirmed bug/security report), but
        # labels_to_apply must still withhold needs_repro since nothing confirmed that premise.
        item = issue(1)
        raw_answers = {"needs_repro": {"type": "noul", "noul": 0.95}}
        proposals = triage.decide(item, raw_answers, THRESHOLDS)
        self.assertEqual([l for l in triage.labels_to_apply(item, proposals) if "repro" in l], [])

    def test_needs_repro_is_never_applied_to_a_pull_request(self):
        item = pull(2)
        proposals = triage.decide(item, answers(noul=0.9, noul_name="pr_ready"), THRESHOLDS)
        self.assertNotIn(triage.NEEDS_REPRO, triage.labels_to_apply(item, proposals))

    def test_kind_still_applies_to_an_ungated_pull_request(self):
        item = pull(2)
        proposals = triage.decide(item, answers(kind="kind/bug", kind_conf=0.9, noul=0.9, noul_name="pr_ready"), THRESHOLDS)
        self.assertEqual(triage.labels_to_apply(item, proposals), ["kind/bug"])

    def test_area_priority_and_pr_ready_are_never_returned(self):
        item = pull(2)
        proposals = triage.decide(item, answers(kind="kind/bug", kind_conf=0.9, noul=0.9, noul_name="pr_ready"), THRESHOLDS)
        applied = triage.labels_to_apply(item, proposals)
        for label in applied:
            self.assertFalse(label.startswith("area/"))
            self.assertFalse(label.startswith("priority/"))
        self.assertNotIn("Status: Needs Review", applied)
        self.assertNotIn("triage/needs-author", applied)

    def test_gh_add_label_uses_issue_edit_or_pr_edit_with_add_label(self):
        calls = []
        triage.gh_add_label("o/r", "issue", 5, "kind/bug", run=recording_run(calls))
        triage.gh_add_label("o/r", "pr", 6, "triage/needs-repro", run=recording_run(calls))
        self.assertEqual(calls[0], ["gh", "issue", "edit", "5", "-R", "o/r", "--add-label", "kind/bug"])
        self.assertEqual(calls[1], ["gh", "pr", "edit", "6", "-R", "o/r", "--add-label", "triage/needs-repro"])
        for command in calls:
            self.assertNotIn("--remove-label", command)

    def test_gh_add_label_failure_is_a_clean_triage_error(self):
        def run(command, capture_output, text):
            return FakeGhResult(returncode=1, stderr="gh: label not found\n")

        with self.assertRaises(triage.TriageError) as raised:
            triage.gh_add_label("o/r", "issue", 5, "kind/bug", run=run)
        self.assertIn("label not found", str(raised.exception))

    def test_gh_add_label_failure_keeps_the_real_cause_not_just_the_last_line(self):
        # gh's own multi-line failure output typically has the actual cause first and a
        # generic "failed to update N issue(s)" summary last; keeping only the last line
        # (as the read-only gh_json helper does for a different, single-line failure shape)
        # would silently drop the one line that actually explains what went wrong.
        def run(command, capture_output, text):
            return FakeGhResult(returncode=1, stderr="HTTP 403: Resource not accessible by integration\nfailed to update 1 issue\n")

        with self.assertRaises(triage.TriageError) as raised:
            triage.gh_add_label("o/r", "issue", 5, "kind/bug", run=run)
        self.assertIn("Resource not accessible by integration", str(raised.exception))

    def test_apply_item_labels_calls_gh_once_per_eligible_label_in_order(self):
        item = issue(9)
        proposals = triage.decide(item, answers(kind="kind/bug", kind_conf=0.9, noul=0.9), THRESHOLDS)
        calls = []
        applied = triage.apply_item_labels("o/r", item, proposals, run=recording_run(calls))
        self.assertEqual(applied, ["kind/bug", "triage/needs-repro"])
        # First call is the live label recheck; the two edits follow in question order.
        self.assertEqual(calls[0], ["gh", "api", "repos/o/r/issues/9"])
        edit_calls = [c for c in calls if c[1:3] in (["issue", "edit"], ["pr", "edit"])]
        self.assertEqual([command[-1] for command in edit_calls], ["kind/bug", "triage/needs-repro"])

    def test_apply_item_labels_makes_no_calls_when_nothing_is_eligible(self):
        item = issue(9, labels=["kind/feature", "triage/needs-repro"])
        proposals = triage.decide(item, answers(kind="kind/bug", kind_conf=0.9, noul=0.9), THRESHOLDS)
        calls = []
        applied = triage.apply_item_labels("o/r", item, proposals, run=recording_run(calls))
        self.assertEqual(applied, [])
        self.assertEqual(calls, [])

    def test_current_labels_reads_via_gh_api(self):
        def run(command, capture_output, text):
            self.assertEqual(command, ["gh", "api", "repos/o/r/issues/42"])
            return FakeGhResult(stdout=json.dumps({"labels": [{"name": "kind/bug"}, {"name": "area/core"}]}))

        self.assertEqual(triage.current_labels("o/r", 42, run=run), {"kind/bug", "area/core"})

    def test_apply_item_labels_skips_a_label_added_concurrently_since_the_fetch(self):
        # The item was unlabeled when fetched (stale snapshot), but by the time --apply writes,
        # a human (or another run) already added both labels. The live recheck must catch that
        # instead of trusting the fetch-time snapshot and adding a second kind/* label.
        item = issue(9)
        proposals = triage.decide(item, answers(kind="kind/bug", kind_conf=0.9, noul=0.9), THRESHOLDS)
        calls = []
        run = recording_run(calls, current_labels_by_number={9: ["kind/feature", "triage/needs-repro"]})
        applied = triage.apply_item_labels("o/r", item, proposals, run=run)
        self.assertEqual(applied, [])
        edit_calls = [c for c in calls if c[1:3] in (["issue", "edit"], ["pr", "edit"])]
        self.assertEqual(edit_calls, [])
        self.assertEqual(calls, [["gh", "api", "repos/o/r/issues/9"]])

    def test_needs_repro_is_withheld_when_a_human_reclassified_the_item_live(self):
        # Fetched unlabeled with a kind/bug answer and a yes needs_repro. By apply time, a
        # human has already added kind/feature (so the item now looks like a feature request,
        # not a bug report) but hasn't touched triage/needs-repro. The stale kind/bug proposal
        # is correctly skipped by the recheck; needs_repro must be withheld too, since its
        # whole premise was "this is a bug report" and that premise no longer holds live.
        item = issue(9)
        proposals = triage.decide(item, answers(kind="kind/bug", kind_conf=0.9, noul=0.9), THRESHOLDS)
        calls = []
        run = recording_run(calls, current_labels_by_number={9: ["kind/feature"]})
        applied = triage.apply_item_labels("o/r", item, proposals, run=run)
        self.assertEqual(applied, [])
        edit_calls = [c for c in calls if c[1:3] in (["issue", "edit"], ["pr", "edit"])]
        self.assertEqual(edit_calls, [])

    def test_needs_repro_still_applies_when_the_live_kind_is_still_bug_or_security(self):
        # A live kind/security (added by a human, or by an earlier label in this same run)
        # still supports needs_repro's premise, so it must still be applied.
        item = issue(9)
        proposals = triage.decide(item, answers(kind="kind/bug", kind_conf=0.9, noul=0.9), THRESHOLDS)
        calls = []
        run = recording_run(calls, current_labels_by_number={9: ["kind/security"]})
        applied = triage.apply_item_labels("o/r", item, proposals, run=run)
        self.assertEqual(applied, ["triage/needs-repro"])

    def test_needs_repro_withholding_is_deterministic_with_more_than_one_live_kind_label(self):
        # GitHub doesn't enforce "exactly one kind/*" at the API level, so an item can carry
        # more than one live kind label. The withhold decision must not depend on set/dict
        # iteration order picking one of them arbitrarily (e.g. by PYTHONHASHSEED) — any
        # conflicting kind/* present is enough to withhold, checked deterministically.
        item = issue(9)
        proposals = triage.decide(item, answers(kind="kind/bug", kind_conf=0.9, noul=0.9), THRESHOLDS)
        for live_labels in (["kind/bug", "kind/feature"], ["kind/feature", "kind/bug"], ["kind/security", "kind/feature"]):
            calls = []
            run = recording_run(calls, current_labels_by_number={9: list(live_labels)})
            applied = triage.apply_item_labels("o/r", item, proposals, run=run)
            self.assertNotIn("triage/needs-repro", applied, live_labels)


class FilterTests(unittest.TestCase):
    """--since and --only-unlabeled filtering, offline."""

    def test_parse_iso_accepts_a_trailing_z(self):
        parsed = triage.parse_iso("2026-01-01T00:00:00Z")
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_parse_iso_assumes_utc_for_a_naive_timestamp(self):
        # No offset and no trailing Z: assumed UTC, not left tz-naive (which would raise
        # "can't compare offset-naive and offset-aware datetimes" against created_at values).
        parsed = triage.parse_iso("2026-01-01T00:00:00")
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_filter_since_accepts_a_naive_since_value_without_raising(self):
        items = [issue(1, created_at="2026-06-01T00:00:00Z")]
        kept = triage.filter_since(items, "2026-01-01T00:00:00")  # no trailing Z
        self.assertEqual([item["number"] for item in kept], [1])

    def test_filter_since_keeps_only_items_created_strictly_after_the_threshold(self):
        items = [
            issue(1, created_at="2026-01-01T00:00:00Z"),
            issue(2, created_at="2026-01-02T00:00:00Z"),
            issue(3, created_at="2026-01-03T00:00:00Z"),
        ]
        kept = triage.filter_since(items, "2026-01-02T00:00:00Z")
        self.assertEqual([item["number"] for item in kept], [3])

    def test_filter_since_drops_items_with_no_created_at(self):
        items = [issue(1, created_at=""), issue(2, created_at="2026-06-01T00:00:00Z")]
        kept = triage.filter_since(items, "2026-01-01T00:00:00Z")
        self.assertEqual([item["number"] for item in kept], [2])

    def test_filter_since_is_a_no_op_when_unset(self):
        items = [issue(1, created_at=""), issue(2, created_at="2026-06-01T00:00:00Z")]
        self.assertEqual(triage.filter_since(items, None), items)

    def test_filter_unlabeled_drops_items_with_an_existing_kind_label(self):
        items = [issue(1, labels=["kind/bug"]), issue(2, labels=["area/core"]), issue(3)]
        kept = triage.filter_unlabeled(items)
        self.assertEqual([item["number"] for item in kept], [2, 3])

    def test_since_flag_is_validated_at_parse_time(self):
        with self.assertRaises(SystemExit):
            with patch("sys.stderr", io.StringIO()):
                triage.parse_args(["--since", "not-a-timestamp"])
        triage.parse_args(["--since", "2026-01-01T00:00:00Z"])  # does not raise


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

    def test_malformed_200_body_is_a_clean_triage_error_not_a_raw_traceback(self):
        # A 200 OK with an unparseable body must not escape as a bare JSONDecodeError: that
        # would bypass evaluate()'s "except TriageError" and, with --apply, discard the record
        # of any labels already written for earlier items in the same run.
        class BadResponse(self.Response):
            def read(self):
                return b"not json"

        transport = triage.HttpTransport("k", opener=lambda request, timeout: BadResponse(None), sleep=lambda s: None)
        with self.assertRaises(triage.TriageError) as raised:
            transport({})
        self.assertIn("not valid JSON", str(raised.exception))

    def test_read_timeout_is_retried_like_other_transient_failures(self):
        # response.read() can raise a bare TimeoutError (not wrapped in URLError) on a slow
        # server; it must be retried and redacted like every other transient transport failure.
        calls = []

        def opener(request, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("timed out")
            return self.Response({"model": "jev-latest", "answers": {}, "usage": {}})

        transport = triage.HttpTransport("k", opener=opener, sleep=lambda s: None)
        result = transport({"state": {}, "model": "jev-latest", "questions": {}})
        self.assertEqual(result["model"], "jev-latest")
        self.assertEqual(len(calls), 2)

    def test_read_timeout_gives_up_after_the_last_attempt(self):
        def opener(request, timeout):
            raise TimeoutError("timed out")

        transport = triage.HttpTransport("k", attempts=2, opener=opener, sleep=lambda s: None)
        with self.assertRaises(triage.TriageError) as raised:
            transport({})
        self.assertIn("timed out", str(raised.exception))

    def test_an_unexpected_read_error_is_retried_then_a_clean_triage_error(self):
        # A dropped connection mid-body (e.g. http.client.IncompleteRead) is neither an
        # HTTPError, a URLError, nor a TimeoutError; it must still be retried and end as a
        # clean TriageError, not a raw traceback that would lose earlier items' results.
        calls = []

        def opener(request, timeout):
            calls.append(1)
            raise ConnectionResetError("connection reset by peer")

        transport = triage.HttpTransport("k", attempts=2, opener=opener, sleep=lambda s: None)
        with self.assertRaises(triage.TriageError) as raised:
            transport({})
        self.assertEqual(len(calls), 2)
        self.assertIn("connection reset", str(raised.exception))

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

    def run_main(self, argv, environ=None, run=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = triage.main(argv, environ={} if environ is None else environ, stdout=stdout, stderr=stderr, run=run)
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

    def test_apply_write_failure_preserves_completed_items_via_partial_failure(self):
        # Item 201 fully succeeds (both labels). Item 202's first write (kind/bug) succeeds but
        # its second (triage/needs-repro) fails. Both items must still appear in partial.results
        # with their real applied labels: 202's already-written kind/bug must not vanish just
        # because the run stopped on its second write.
        first = issue(201, labels=[])
        second = issue(202, labels=[])
        responses = {
            "issue:201": {"model": "jev-latest", "answers": answers(kind="kind/bug", kind_conf=0.9, noul=0.9), "usage": {}},
            "issue:202": {"model": "jev-latest", "answers": answers(kind="kind/bug", kind_conf=0.9, noul=0.9), "usage": {}},
        }
        transport = triage.FixtureTransport(responses)

        def run(command, capture_output, text):
            if command[:2] == ["gh", "api"]:
                return FakeGhResult(stdout=json.dumps({"labels": []}))
            if command[3] == "202" and command[-1] == "triage/needs-repro":
                return FakeGhResult(returncode=1, stderr="gh: rate limited\n")
            return FakeGhResult()

        with self.assertRaises(triage.PartialFailure) as raised:
            triage.evaluate("o/r", [first, second], transport, THRESHOLDS, apply=True, run=run)
        partial = raised.exception
        self.assertEqual(partial.failed_key, "issue:202")
        self.assertEqual([r["key"] for r in partial.results], ["issue:201", "issue:202"])
        self.assertEqual(partial.results[0]["applied"], ["kind/bug", "triage/needs-repro"])
        self.assertEqual(partial.results[1]["applied"], ["kind/bug"])

    def test_apply_survives_a_bare_subprocess_exception_not_just_triageerror(self):
        # gh_add_label's own run() call can raise something that isn't a TriageError at all
        # (e.g. OSError if the OS can't spawn the process) before there's an exit code to check.
        # The already-applied kind/bug for this single-item batch must still be recorded.
        item = issue(203, labels=[])
        responses = {"issue:203": {"model": "jev-latest", "answers": answers(kind="kind/bug", kind_conf=0.9, noul=0.9), "usage": {}}}
        transport = triage.FixtureTransport(responses)

        def run(command, capture_output, text):
            if command[:2] == ["gh", "api"]:
                return FakeGhResult(stdout=json.dumps({"labels": []}))
            if command[-1] == "triage/needs-repro":
                raise OSError("[Errno 11] Resource temporarily unavailable")
            return FakeGhResult()

        with self.assertRaises(triage.PartialFailure) as raised:
            triage.evaluate("o/r", [item], transport, THRESHOLDS, apply=True, run=run)
        partial = raised.exception
        self.assertEqual(partial.failed_key, "issue:203")
        self.assertEqual([r["key"] for r in partial.results], ["issue:203"])
        self.assertEqual(partial.results[0]["applied"], ["kind/bug"])

    def test_apply_failure_on_the_live_recheck_itself_is_a_partial_failure(self):
        # The recheck read (gh api) can fail too, before any label write is attempted; that
        # must also surface as PartialFailure with an empty applied list for the item, not a
        # bare TriageError that skips the PartialFailure/output path entirely.
        item = issue(301, labels=[])
        responses = {"issue:301": {"model": "jev-latest", "answers": answers(kind="kind/bug", kind_conf=0.9, noul=0.9), "usage": {}}}
        transport = triage.FixtureTransport(responses)

        def run(command, capture_output, text):
            if command[:2] == ["gh", "api"]:
                return FakeGhResult(returncode=1, stderr="gh: rate limited\n")
            return FakeGhResult()

        with self.assertRaises(triage.PartialFailure) as raised:
            triage.evaluate("o/r", [item], transport, THRESHOLDS, apply=True, run=run)
        partial = raised.exception
        self.assertEqual(partial.failed_key, "issue:301")
        self.assertEqual(partial.results[0]["applied"], [])

    def test_malformed_response_mid_run_preserves_earlier_completed_items(self):
        # A response missing "answers" for a later item must not erase an earlier item's
        # already-recorded result (and, under --apply, its already-written labels).
        first = issue(401, labels=[])
        second = issue(402, labels=[])
        responses = {
            "issue:401": {"model": "jev-latest", "answers": answers(kind="kind/bug", kind_conf=0.9, noul=0.9), "usage": {}},
            "issue:402": {"model": "jev-latest", "usage": {}},  # no "answers" key
        }
        transport = triage.FixtureTransport(responses)
        with self.assertRaises(triage.PartialFailure) as raised:
            triage.evaluate("o/r", [first, second], transport, THRESHOLDS)
        partial = raised.exception
        self.assertEqual(partial.failed_key, "issue:402")
        self.assertEqual([r["key"] for r in partial.results], ["issue:401"])
        self.assertIn("no 'answers' map", str(partial))

    def test_malformed_nested_answer_shape_is_a_partial_failure_not_a_raw_traceback(self):
        # A response with a top-level "answers" map (so it passes that shape check) but a
        # malformed value inside it (a string instead of an object) raises AttributeError deep
        # inside decide(); that must still become a clean PartialFailure preserving item 501,
        # not an unhandled traceback that discards it.
        first = issue(501, labels=[])
        second = issue(502, labels=[])
        responses = {
            "issue:501": {"model": "jev-latest", "answers": answers(kind="kind/bug", kind_conf=0.9, noul=0.9), "usage": {}},
            "issue:502": {"model": "jev-latest", "answers": {"kind": "not-an-object"}, "usage": {}},
        }
        transport = triage.FixtureTransport(responses)
        with self.assertRaises(triage.PartialFailure) as raised:
            triage.evaluate("o/r", [first, second], transport, THRESHOLDS)
        partial = raised.exception
        self.assertEqual(partial.failed_key, "issue:502")
        self.assertEqual([r["key"] for r in partial.results], ["issue:501"])
        self.assertIn("issue:502", str(partial))

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

    def test_no_matching_items_exits_3_not_1(self):
        # A distinct exit code from real errors/partial failures (1) or a missing key (2), so
        # an automated caller can tell "nothing to do" apart from a failure by exit code alone.
        empty = json.loads(FIXTURE.read_text())
        empty["items"] = []
        empty["responses"] = {}
        path = Path(self.temporary.name) / "empty-fixture.json"
        path.write_text(json.dumps(empty))
        code, out, err = self.run_main(["--fixture", str(path), "--output-dir", str(self.output)])
        self.assertEqual(code, 3)
        self.assertIn("no open items to triage", err)
        self.assertEqual(out, "")

    def test_only_unlabeled_filtering_down_to_zero_items_also_exits_3(self):
        fully_labeled = json.loads(FIXTURE.read_text())
        for item in fully_labeled["items"]:
            item["labels"] = ["kind/bug"]
        path = Path(self.temporary.name) / "fully-labeled-fixture.json"
        path.write_text(json.dumps(fully_labeled))
        code, out, err = self.run_main(["--fixture", str(path), "--output-dir", str(self.output), "--only-unlabeled"])
        self.assertEqual(code, 3)

    def test_unwritable_output_dir_still_prints_the_console_report(self):
        # A save failure (bad --output-dir) must degrade to a warning, not hide the report of
        # what already happened (evaluation, and any --apply writes) behind a crash.
        unwritable = Path(self.temporary.name) / "not-a-directory"
        unwritable.write_text("occupied by a plain file, not a directory")
        code, out, err = self.run_main(["--fixture", str(FIXTURE), "--output-dir", str(unwritable)])
        self.assertEqual(code, 0, err)
        self.assertIn("warning: could not save raw answers", err)
        self.assertIn("Dry run", out)
        self.assertIn("Summary", out)

    def test_unwritable_record_path_fails_the_run_but_still_prints_the_report(self):
        # --record only ever runs in live mode (see main(): "if args.record and mode ==
        # 'live'"), so this needs an actual live-mode run, not --fixture: mock fetch_items to
        # avoid needing a real gh, and the HTTP opener to avoid a real TypeSafe call. --record
        # is explicitly requested with no default (unlike --output-dir), so a caller relying on
        # it for later --fixture replay must see the failure in the exit code, not only a
        # warning line — but the console report must still print, same as --output-dir.
        environ = dict(os.environ, TYPESAFE_API_KEY="ts-fake-live-key-0123456789")
        fixture = json.loads(FIXTURE.read_text())
        items = fixture["items"][:1]
        responses = fixture["responses"]

        def opener(request, timeout):
            body = json.loads(request.data.decode())
            key = "pr" if body["state"]["item_type"] == "pull_request" else "issue"
            return TransportTests.Response(responses["%s:%d" % (key, body["state"]["number"])])

        unwritable_record = Path(self.temporary.name) / "missing-parent" / "record.json"
        with patch.object(triage, "fetch_items", return_value=items), \
             patch.object(triage.urllib.request, "urlopen", side_effect=opener):
            code, out, err = self.run_main(
                ["--output-dir", str(self.output), "--record", str(unwritable_record)],
                environ=environ,
            )
        self.assertEqual(code, 1)
        self.assertIn("warning: could not write --record file", err)
        self.assertIn("Dry run", out)
        self.assertFalse(unwritable_record.exists())

    def test_an_unexpected_processing_error_never_leaks_the_key_even_from_response_data(self):
        # decide() can raise from a malformed nested value (e.g. float() on a non-numeric
        # "score"), and that exception's own message embeds the bad value verbatim. If a
        # response happened to echo the key back in such a value, evaluate()'s generic
        # exception handler must still redact it before it reaches stderr or the saved JSON.
        sentinel = "ts-live-FAKE-KEY-0123456789"
        # A first, normal item so `results` isn't empty when the second one blows up:
        # main() only reaches write_output()/the console report when at least one item
        # completed, and this test wants to check both the report and the saved JSON.
        first = issue(600, labels=[])
        malformed = issue(601, labels=[])
        responses = {
            "issue:600": {"model": "jev-latest", "answers": answers(kind="kind/bug", kind_conf=0.9, noul=0.9), "usage": {}},
            "issue:601": {"model": "jev-latest", "answers": {"priority": {"score": sentinel, "confidence": 0.9}}, "usage": {}},
        }
        path = Path(self.temporary.name) / "malformed-fixture.json"
        path.write_text(json.dumps({"repo": "o/r", "items": [first, malformed], "responses": responses}))
        code, out, err = self.run_main(
            ["--fixture", str(path), "--output-dir", str(self.output), "--json"],
            environ={"TYPESAFE_API_KEY": sentinel},
        )
        self.assertEqual(code, 1)
        saved = next(self.output.glob("triage-*.json")).read_text()
        for text in (out, err, saved):
            self.assertNotIn(sentinel, text)
        self.assertIn("could not convert string to float", err)

    def apply_fixture_data(self):
        """Three issues exercising every --apply branch: eligible, already-labeled, gated."""
        eligible = issue(101, labels=[])
        already_labeled = issue(102, labels=["kind/feature", "triage/needs-repro"])
        gated = issue(103, labels=[])
        return {
            "schema_version": 1,
            "repo": "nanocoai/nanoclaw",
            "items": [eligible, already_labeled, gated],
            "responses": {
                "issue:101": {"model": "jev-latest", "answers": answers(kind="kind/bug", kind_conf=0.9, noul=0.9), "usage": {}},
                "issue:102": {"model": "jev-latest", "answers": answers(kind="kind/bug", kind_conf=0.9, noul=0.9), "usage": {}},
                "issue:103": {"model": "jev-latest", "answers": answers(kind_conf=0.2, noul=0.5), "usage": {}},
            },
        }

    def write_fixture(self, data, name="apply-fixture.json"):
        path = Path(self.temporary.name) / name
        path.write_text(json.dumps(data))
        return path

    def test_apply_flag_off_makes_no_gh_calls(self):
        fixture_path = self.write_fixture(self.apply_fixture_data())
        calls = []
        code, out, err = self.run_main(
            ["--fixture", str(fixture_path), "--output-dir", str(self.output), "--json"],
            run=recording_run(calls),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [])
        payload = json.loads(out)
        self.assertEqual(payload["summary"]["labels_applied"], 0)
        for result in payload["results"]:
            self.assertEqual(result["applied"], [])

    def test_fixture_and_apply_together_is_rejected(self):
        # --apply must act on real, current GitHub state, not a fixture's hand-written
        # snapshot, so the CLI refuses the combination before anything runs.
        fixture_path = self.write_fixture(self.apply_fixture_data())
        with self.assertRaises(SystemExit):
            with patch("sys.stderr", io.StringIO()):
                triage.parse_args(["--fixture", str(fixture_path), "--apply"])

    def test_apply_flag_makes_exact_add_label_calls_for_the_eligible_item_only(self):
        # --apply can't be combined with --fixture at the CLI (see the test above), so this
        # exercises evaluate() directly with a FixtureTransport and a mocked gh run — the same
        # two seams main() wires together, just without going through the CLI guard.
        data = self.apply_fixture_data()
        transport = triage.FixtureTransport(data["responses"])
        calls = []
        results, usage = triage.evaluate(
            data["repo"], data["items"], transport, THRESHOLDS, apply=True, run=recording_run(calls),
        )
        # item 101: ungated kind/bug + yes needs_repro, no existing labels -> both applied, in
        # question order (after one live-label recheck read). item 102: both already present ->
        # no calls at all. item 103: both gated/uncertain -> no calls at all.
        self.assertEqual(calls, [
            ["gh", "api", "repos/nanocoai/nanoclaw/issues/101"],
            ["gh", "issue", "edit", "101", "-R", "nanocoai/nanoclaw", "--add-label", "kind/bug"],
            ["gh", "issue", "edit", "101", "-R", "nanocoai/nanoclaw", "--add-label", "triage/needs-repro"],
        ])
        summary = triage.summarize(results)
        self.assertEqual(summary["labels_applied"], 2)
        applied_by_number = {result["item"]["number"]: result["applied"] for result in results}
        self.assertEqual(applied_by_number[101], ["kind/bug", "triage/needs-repro"])
        self.assertEqual(applied_by_number[102], [])
        self.assertEqual(applied_by_number[103], [])
        self.assertIn("applied", triage.render_table(results).lower())

    def test_apply_flag_never_calls_gh_with_remove(self):
        data = self.apply_fixture_data()
        transport = triage.FixtureTransport(data["responses"])
        calls = []
        triage.evaluate(data["repo"], data["items"], transport, THRESHOLDS, apply=True, run=recording_run(calls))
        for command in calls:
            self.assertNotIn("--remove-label", command)
            self.assertNotIn("comment", command)

    def test_since_flag_filters_items_before_evaluation(self):
        fixture = json.loads(FIXTURE.read_text())
        for item in fixture["items"]:
            item["created_at"] = "2026-09-10T00:00:00Z" if item["number"] == 3811 else "2026-01-01T00:00:00Z"
        path = Path(self.temporary.name) / "since-fixture.json"
        path.write_text(json.dumps(fixture))
        code, out, err = self.run_main([
            "--fixture", str(path), "--output-dir", str(self.output), "--since", "2026-06-01T00:00:00Z", "--json",
        ])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual([result["item"]["number"] for result in payload["results"]], [3811])

    def test_only_unlabeled_flag_skips_items_that_already_carry_a_kind_label(self):
        code, out, err = self.run_main([
            "--fixture", str(FIXTURE), "--output-dir", str(self.output), "--only-unlabeled", "--json",
        ])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        numbers = [result["item"]["number"] for result in payload["results"]]
        self.assertNotIn(3839, numbers)  # already carries kind/bug
        self.assertNotIn(3841, numbers)  # already carries kind/bug
        self.assertIn(3811, numbers)
        self.assertIn(3814, numbers)
        self.assertIn(3844, numbers)


if __name__ == "__main__":
    unittest.main()
