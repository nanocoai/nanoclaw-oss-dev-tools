"""Docs-drift benchmark: workload sampling, LLM prompt-and-parse, metrics and CLI, all offline."""

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/typesafe-docs-drift/scripts/drift-benchmark.py"
WORKLOAD = ROOT / "skills/typesafe-docs-drift/fixtures/benchmark-workload.json"
GOLD = ROOT / "skills/typesafe-docs-drift/fixtures/benchmark-gold.json"


def load():
    spec = importlib.util.spec_from_file_location("drift_benchmark_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = load()


def response(contradicts, covers, staleness=0.2, tokens=100):
    return {"model": "jev-latest", "answers": {"contradicts": {"type": "noul", "noul": contradicts},
                                                "covers": {"type": "noul", "noul": covers},
                                                "staleness": {"type": "score", "score": staleness, "confidence": 0.9}},
            "usage": {"input_tokens": tokens, "output_tokens": 10}}


def record(ok_facts=8):
    facts, sections, candidates, responses = [], [], {}, {}

    def add(fact_id, answers):
        facts.append({"id": fact_id, "area": fact_id.split(":")[0], "statement": "`%s` fact." % fact_id,
                      "evidence": {"file": "src/x.ts", "line": 1, "excerpt": "x"}, "terms": []})
        candidates[fact_id] = []
        for number, (contradicts, covers) in enumerate(answers, 1):
            section_id = "page#%s-%d" % (fact_id.replace(":", "-"), number)
            sections.append({"id": section_id, "page": "page", "title": "T", "heading": "h", "level": 2, "line": number, "text": "doc text"})
            candidates[fact_id].append(section_id)
            responses["%s|%s" % (fact_id, section_id)] = response(contradicts, covers)

    add("ncl:drift", [(0.9, 0.9), (0.8, 0.8), (0.1, 0.1)])
    add("env:missing", [(0.05, 0.1), (0.05, 0.2)])
    add("env:unsure", [(0.5, 0.6)])
    for number in range(ok_facts):
        add("skills:ok%d" % number, [(0.05, 0.95), (0.05, 0.1)])
    return {"recorded_at": "t", "code_commit": "c", "docs_commit": "d", "facts": facts, "sections": sections,
            "candidates": candidates, "scores": {}, "responses": responses}


def small_workload(pairs=3):
    data = bench.build_workload(record(), limit=0)
    data["pairs"] = data["pairs"][:pairs]
    return data


class Completed:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def claude_envelope(items, tokens=(1000, 50), cost=0.01):
    return json.dumps({"type": "result", "is_error": False, "result": json.dumps(items), "total_cost_usd": cost,
                       "usage": {"input_tokens": tokens[0], "cache_creation_input_tokens": 5, "cache_read_input_tokens": 7,
                                 "output_tokens": tokens[1]}})


class WorkloadTests(unittest.TestCase):
    def test_sample_takes_every_drift_missing_and_unsure_row_then_a_seeded_ok_fill(self):
        workload = bench.build_workload(record(), limit=6, seed=1)
        self.assertEqual(workload["strata"], {"DRIFT": 2, "MISSING": 1, "UNSURE": 1, "OK": 2})
        keys = [pair["key"] for pair in workload["pairs"]]
        self.assertEqual(keys[:2], ["ncl:drift|page#ncl-drift-1", "ncl:drift|page#ncl-drift-2"])
        self.assertIn("env:missing|page#env-missing-2", keys)
        self.assertEqual(keys, [pair["key"] for pair in bench.build_workload(record(), limit=6, seed=1)["pairs"]])
        other = [pair["key"] for pair in bench.build_workload(record(), limit=6, seed=2)["pairs"]]
        self.assertEqual(keys[:4], other[:4])
        self.assertNotEqual(keys[4:], other[4:])
        self.assertEqual({section["id"] for section in workload["sections"]}, {pair["section_id"] for pair in workload["pairs"]})
        for pair in workload["pairs"]:
            self.assertNotIn("terms", pair["fact"])
            self.assertIn("recorded_response", pair)

    def test_recorded_latencies_come_from_the_results_file(self):
        results = {"results": [{"pairs": [{"key": "env:unsure|page#env-unsure-1", "elapsed_seconds": 0.42}]}]}
        workload = bench.build_workload(record(), results, limit=0)
        unsure = next(pair for pair in workload["pairs"] if pair["stratum"] == "UNSURE")
        self.assertEqual(unsure["recorded_elapsed_seconds"], 0.42)

    def test_shipped_workload_and_gold_are_consistent(self):
        workload = json.loads(WORKLOAD.read_text())
        gold = json.loads(GOLD.read_text())
        self.assertEqual(len(workload["pairs"]), 60)
        self.assertEqual(sum(workload["strata"].values()), 60)
        keys = {pair["key"] for pair in workload["pairs"]}
        self.assertEqual(len(gold["labels"]), 30)
        self.assertTrue(set(gold["labels"]) <= keys)
        self.assertIn("LLM-assisted", gold["labeled_by"])
        must = {pair["key"] for pair in workload["pairs"] if pair["stratum"] in ("DRIFT", "MISSING")}
        self.assertTrue(must <= set(gold["labels"]))
        for label in gold["labels"].values():
            self.assertIsInstance(label["contradicts"], bool)
            self.assertIsInstance(label["covers"], bool)
            self.assertTrue(len(label["justification"]) > 20)


class PromptTests(unittest.TestCase):
    def test_prompt_carries_the_same_questions_and_state_as_the_typesafe_request(self):
        workload = small_workload(2)
        sections = {section["id"]: section for section in workload["sections"]}
        prompt = json.loads(bench.build_llm_prompt(workload["pairs"], sections))
        questions = bench.drift.build_questions()
        self.assertEqual(prompt["instructions"]["contradicts"]["question"], questions["contradicts"]["instructions"])
        self.assertEqual(prompt["instructions"]["covers"]["true"], questions["covers"]["criteria"]["true"])
        self.assertEqual(len(prompt["instructions"]["staleness"]["levels"]), 4)
        self.assertEqual([item["id"] for item in prompt["items"]], ["p01", "p02"])
        self.assertEqual(prompt["items"][0], dict(id="p01", **bench.pair_state(workload["pairs"][0], sections)))

    def test_parse_accepts_fenced_json_and_rejects_bad_items(self):
        expected = {"p01": "a", "p02": "b", "p03": "c", "p04": "d"}
        text = '```json\n[{"id":"p01","contradicts":true,"covers":false,"staleness":2},' \
               '{"id":"p02","contradicts":"yes","covers":true,"staleness":1},' \
               '{"id":"p03","contradicts":false,"covers":true,"staleness":7},' \
               '{"id":"p09","contradicts":false,"covers":true,"staleness":0},' \
               '{"id":"p01","contradicts":false,"covers":true,"staleness":0}]\n```'
        self.assertEqual(bench.parse_llm_answer(text, expected), {}, "p01 answered twice is ambiguous and dropped")
        self.assertEqual(bench.parse_llm_answer(text.replace('{"id":"p01","contradicts":false,"covers":true,"staleness":0}', '{"id":[]}'), expected),
                         {"p01": {"contradicts": True, "covers": False, "staleness": 2}})
        self.assertFalse(bench.is_strict_json_array(text))
        self.assertFalse(bench.is_strict_json_array("Here: []"))
        self.assertTrue(bench.is_strict_json_array(' [{"id": "p01"}]\n'))
        self.assertEqual(bench.parse_llm_answer("Sure! Here you go: [{\"id\":\"p04\",\"contradicts\":false,\"covers\":true,\"staleness\":0}] Hope it helps", expected),
                         {"p04": {"contradicts": False, "covers": True, "staleness": 0}})
        for bad in ("", "no json", "[not json]", '{"id":"p01"}', '[{"id":"p01","contradicts":true,"covers":true,"staleness":true}]'):
            self.assertEqual(bench.parse_llm_answer(bad, expected), {}, bad)

    def test_cli_commands_disable_tools_and_sandbox_codex(self):
        command = bench.cli_command("claude:haiku")
        self.assertEqual(command[:6], ["claude", "-p", "--model", "haiku", "--output-format", "json"])
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--no-session-persistence", command)
        codex = bench.cli_command("codex")
        self.assertEqual(codex[:3], ["codex", "exec", "--ephemeral"])
        self.assertIn('sandbox_mode="read-only"', codex)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", codex)
        with self.assertRaises(bench.BenchmarkError):
            bench.cli_command("gpt:4")


class LlmRunTests(unittest.TestCase):
    def test_batches_sum_usage_and_subtract_the_measured_overhead(self):
        workload = bench.build_workload(record(), limit=0)
        workload["pairs"] = workload["pairs"][:5]
        calls = []

        def run(command, input, capture_output, text, timeout, env):
            calls.append((command, input, env))
            items = [{"id": item["id"], "contradicts": True, "covers": True, "staleness": 1} for item in json.loads(input)["items"]]
            if len(calls) == 2:
                items = items[:1] + [{"id": "p02", "contradicts": "maybe"}]
            return Completed(claude_envelope(items))

        clock = iter([0, 0, 4.0, 4.0, 10.0, 10.0, 12.0, 12.0])
        with patch.object(bench.time, "monotonic", side_effect=lambda: next(clock)):
            outcome = bench.run_llm("claude:haiku", workload, run=run, environ={"TYPESAFE_API_KEY": "ts-FAKE-KEY-0123456789", "PATH": "/bin"},
                                    batch_size=2, overhead=2.0)
        self.assertEqual(outcome["calls"], 3)
        self.assertEqual([len(json.loads(prompt)["items"]) for _c, prompt, _e in calls], [2, 2, 1])
        self.assertEqual(outcome["input_tokens"], 3 * 1012)
        self.assertEqual(outcome["output_tokens"], 150)
        self.assertEqual(outcome["cost_usd"], 0.03)
        self.assertEqual(outcome["parse_failures"], 1)
        self.assertEqual(outcome["format_violations"], 0)
        self.assertEqual(len(outcome["verdicts"]), 4)
        self.assertEqual(outcome["latencies"], [2.0, 2.0, 3.0, 3.0, 2.0])
        self.assertEqual(outcome["adjusted_latencies"], [1.0, 1.0, 2.0, 2.0, 0.0])
        for _command, _prompt, env in calls:
            self.assertNotIn("TYPESAFE_API_KEY", env)
            self.assertEqual(env["PATH"], "/bin")

    def test_cli_failures_count_every_pair_in_the_batch_as_a_parse_failure(self):
        workload = small_workload(3)
        replies = iter([Completed("", 1, "boom\nlast line"), Completed("not json"),
                        Completed(json.dumps({"is_error": True, "result": "rate limited"}))])
        outcome = bench.run_llm("claude:sonnet", workload, run=lambda *a, **k: next(replies), environ={}, batch_size=1)
        self.assertEqual(outcome["parse_failures"], 3)
        self.assertEqual((outcome["calls"], outcome["failed_calls"], len(outcome["latencies"])), (3, 3, 3))
        self.assertEqual(outcome["verdicts"], {})
        self.assertIn("exited 1: last line", outcome["errors"][0])
        self.assertIn("did not return JSON", outcome["errors"][1])
        self.assertIn("rate limited", outcome["errors"][2])

    def test_failed_calls_still_cost_what_the_cli_reported_and_odd_envelopes_do_not_crash(self):
        failed = json.dumps({"is_error": True, "result": "overloaded", "total_cost_usd": 0.5,
                             "usage": {"input_tokens": 900, "output_tokens": "many"}})
        replies = iter([Completed(failed), Completed("null"), Completed(json.dumps({"result": None, "usage": None})),
                        Completed(failed, returncode=1, stderr="boom")])
        outcome = bench.run_llm("claude:haiku", small_workload(4), run=lambda *a, **k: next(replies), environ={}, batch_size=1)
        self.assertEqual((outcome["cost_usd"], outcome["input_tokens"], outcome["output_tokens"]), (1.0, 1800, 0))
        self.assertEqual((outcome["failed_calls"], outcome["parse_failures"]), (3, 4))
        self.assertIsNone(bench.verdict_from_answers({"contradicts": True}, 0.7))
        self.assertIsNone(bench.verdict_from_answers([], 0.7))

    def test_cli_diagnostics_never_carry_secret_environment_values(self):
        environ = {"ANTHROPIC_AUTH_TOKEN": "sk-SENTINEL-0123456789", "PATH": "/bin"}
        replies = iter([Completed("", 1, "auth failed for sk-SENTINEL-0123456789"),
                        Completed(json.dumps({"is_error": True, "result": "bad token sk-SENTINEL-0123456789"}))])
        outcome = bench.run_llm("claude:haiku", small_workload(2), run=lambda *a, **k: next(replies), environ=environ, batch_size=1)
        self.assertEqual(len(outcome["errors"]), 2)
        self.assertNotIn("SENTINEL", json.dumps(outcome))
        self.assertIn("[redacted]", outcome["errors"][0])

    def test_timeout_kills_the_whole_process_group(self):
        import sys as _sys
        script = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"
        with self.assertRaises(subprocess.TimeoutExpired):
            bench.spawn([_sys.executable, "-c", script], input="", timeout=1)
        finished = bench.spawn([_sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input="ok", timeout=20)
        self.assertEqual((finished.returncode, finished.stdout.strip()), (0, "OK"))

    def test_missing_cli_and_timeouts_are_clean_errors(self):
        def missing(*args, **kwargs):
            raise FileNotFoundError("claude")

        def slow(*args, **kwargs):
            raise subprocess.TimeoutExpired("claude", 1)

        with self.assertRaises(bench.BenchmarkError) as raised:
            bench.measure_overhead("claude:haiku", run=missing, environ={})
        self.assertIn("not on PATH", str(raised.exception))
        with self.assertRaises(bench.BenchmarkError):
            bench.measure_overhead("claude:haiku", run=slow, environ={})

    def test_event_list_output_and_codex_plain_text_are_understood(self):
        workload = small_workload(1)
        items = [{"id": "p01", "contradicts": False, "covers": True, "staleness": 0}]
        events = json.dumps([{"type": "system"}, json.loads(claude_envelope(items))])
        outcome = bench.run_llm("claude:haiku", workload, run=lambda *a, **k: Completed(events), environ={})
        self.assertEqual(len(outcome["verdicts"]), 1)
        outcome = bench.run_llm("codex", workload, run=lambda *a, **k: Completed("thinking...\n" + json.dumps(items)), environ={})
        self.assertEqual(len(outcome["verdicts"]), 1)
        self.assertIsNone(outcome["cost_usd"])
        self.assertIsNone(outcome["input_tokens"], "codex token use is unknown, not zero")
        summary = bench.summarize({"codex": [outcome]}, {}, [workload["pairs"][0]["key"]])
        self.assertIn("- / -", bench.render(summary))


class TypeSafeRunTests(unittest.TestCase):
    def test_recorded_run_binarizes_at_the_gate_and_sums_recorded_usage(self):
        workload = bench.build_workload(record(), limit=0)
        run = bench.run_typesafe_recorded(workload, 0.7)
        self.assertTrue(run["verdicts"]["ncl:drift|page#ncl-drift-1"]["contradicts"])
        self.assertFalse(run["verdicts"]["env:unsure|page#env-unsure-1"]["contradicts"])
        self.assertTrue(bench.run_typesafe_recorded(workload, 0.5)["verdicts"]["env:unsure|page#env-unsure-1"]["contradicts"])
        self.assertEqual(run["input_tokens"], 100 * len(workload["pairs"]))
        self.assertEqual(run["parse_failures"], 0)
        self.assertIsNone(run["wall_seconds"], "no recorded latencies means unknown, not zero")
        for bad in ({"contradicts": {"noul": True}, "covers": {"noul": "0.9"}, "staleness": {"score": 1}},
                    {"contradicts": {"noul": 0.9}, "covers": {"noul": 0.9}},
                    {"contradicts": {"noul": 1.4}, "covers": {"noul": 0.9}, "staleness": {"score": 1}},
                    {"contradicts": {"noul": 0.4}, "covers": {"noul": 0.9}, "staleness": {"score": 9}}, None):
            self.assertIsNone(bench.verdict_from_answers(bad, 0.7), bad)

    def test_live_run_sends_the_skill_request_sequentially_and_survives_failures(self):
        workload = small_workload(3)
        seen = []

        def transport(payload):
            seen.append(payload)
            if len(seen) == 2:
                raise bench.BenchmarkError("HTTP 529 from TypeSafe: overloaded")
            return response(0.9, 0.9, tokens=7)

        run = bench.run_typesafe_live(workload, 0.7, transport)
        self.assertEqual(len(seen), 3)
        self.assertEqual(set(seen[0]["questions"]), {"contradicts", "covers", "staleness"})
        self.assertEqual(seen[0]["model"], "jev-latest")
        self.assertEqual(set(seen[0]["state"]), {"fact", "doc_section"})
        self.assertEqual((len(run["verdicts"]), run["parse_failures"], run["input_tokens"]), (2, 1, 14))
        self.assertEqual((len(run["errors"]), run["calls"], run["failed_calls"], len(run["latencies"])), (1, 3, 1, 3))


class MetricTests(unittest.TestCase):
    def run_with(self, verdicts):
        run = bench.empty_run("x")
        run["verdicts"] = {key: {"contradicts": c, "covers": v, "staleness": 0} for key, (c, v) in verdicts.items()}
        return run

    def test_percentiles_flip_rate_and_agreement(self):
        self.assertEqual(bench.percentile([1, 2, 3, 4], 0.5), 2.5)
        self.assertEqual(bench.percentile([1, 2, 3, 4, 100], 0.95), 80.8)
        self.assertIsNone(bench.percentile([], 0.5))
        first = self.run_with({"a": (True, True), "b": (False, True), "c": (True, False), "d": (False, False)})
        second = self.run_with({"a": (True, True), "b": (True, True), "c": (True, True)})
        self.assertEqual(bench.flip_rate(first, second), {"pairs": 3, "flips": 1, "rate": round(1 / 3, 4)})
        self.assertEqual(bench.agreement(first, second, "covers"), {"pairs": 3, "rate": round(2 / 3, 4)})
        self.assertIsNone(bench.flip_rate(first, self.run_with({})))

    def test_precision_and_recall_count_unanswered_gold_pairs_as_a_no(self):
        gold = {"a": {"contradicts": True, "covers": True}, "b": {"contradicts": True, "covers": True},
                "c": {"contradicts": False, "covers": True}, "z": {"contradicts": True, "covers": True}}
        run = self.run_with({"a": (True, True), "c": (True, True)})
        scores = bench.precision_recall(run, gold, ["a", "b", "c", "d"])
        self.assertEqual((scores["tp"], scores["fp"], scores["fn"], scores["tn"]), (1, 1, 1, 0))
        self.assertEqual((scores["precision"], scores["recall"]), (0.5, 0.5))
        empty = bench.precision_recall(self.run_with({}), {"c": {"contradicts": False, "covers": False}}, ["c"])
        self.assertEqual((empty["precision"], empty["recall"]), (None, None))

    def test_summary_rows_and_rendered_table_have_the_requested_columns(self):
        first = self.run_with({"a": (True, True)})
        first.update(wall_seconds=3.0, latencies=[1.0], adjusted_latencies=[0.5], input_tokens=9, output_tokens=1, cost_usd=0.002)
        second = self.run_with({"a": (False, True)})
        second["parse_failures"] = 4
        summary = bench.summarize({"claude:haiku": [first, second], "typesafe-recorded": [self.run_with({"a": (True, True)})]},
                                  {"a": {"contradicts": True, "covers": True}}, ["a"])
        row = summary["rows"][0]
        self.assertEqual((row["flip"]["rate"], row["flip"]["pairs"], row["gold"]["precision"], row["gold"]["recall"]), (1.0, 1, 1.0, 1.0))
        self.assertEqual(row["parse_failures_by_run"], [0, 4])
        self.assertIsNone(summary["rows"][1]["flip"])
        self.assertEqual(summary["agreement"][0]["contradicts"], {"pairs": 1, "rate": 1.0})
        text = bench.render(summary)
        for column in ("Contender", "Pairs", "Wall s", "p50 s/pair", "Tokens in/out", "Cost $", "Parse fail", "Flip %", "Precision", "Recall"):
            self.assertIn(column, text)
        self.assertIn("agreement claude:haiku vs typesafe-recorded: contradicts 100.0%", text)
        self.assertIn("0/4", text)
        self.assertIn("100.0 (1/1)", text)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="drift-benchmark-")
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "out"
        self.patcher = patch.object(bench.drift.urllib.request, "urlopen", side_effect=AssertionError("live TypeSafe call attempted"))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def run_main(self, argv, environ=None, run=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        runner = run or (lambda *a, **k: self.fail("no CLI may be spawned"))
        code = bench.main(argv + ["--output-dir", str(self.output)], environ={} if environ is None else environ,
                          stdout=stdout, stderr=stderr, run=runner)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_run_is_offline_and_scores_the_recorded_answers_against_the_gold(self):
        code, out, err = self.run_main(["--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        row = payload["summary"]["rows"][0]
        self.assertEqual((row["contender"], row["pairs"], row["parse_failures"]), ("typesafe-recorded", 60, 0))
        self.assertEqual(payload["gold"]["pairs"], 30)
        self.assertIn("LLM-assisted", payload["gold"]["labeled_by"])
        self.assertGreater(row["input_tokens"], 0)
        self.assertIsNotNone(row["gold"]["precision"])
        self.assertEqual(len(list(self.output.glob("benchmark-*.json"))), 1)

    def test_llm_contender_runs_twice_after_one_overhead_call_and_limit_applies(self):
        prompts = []

        def run(command, input, capture_output, text, timeout, env):
            prompts.append(input)
            if input == bench.OVERHEAD_PROMPT:
                return Completed(claude_envelope([]))
            items = [{"id": item["id"], "contradicts": len(prompts) % 2 == 0, "covers": True, "staleness": 0} for item in json.loads(input)["items"]]
            return Completed(claude_envelope(items))

        code, out, err = self.run_main(["--contenders", "typesafe-recorded,claude:haiku", "--limit", "12", "--batch-size", "10", "--json"],
                                       environ={"TYPESAFE_API_KEY": "ts-FAKE-KEY-0123456789"}, run=run)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(prompts), 1 + 2 * 2)
        payload = json.loads(out)
        rows = {row["contender"]: row for row in payload["summary"]["rows"]}
        self.assertEqual(rows["claude:haiku"]["pairs"], 12)
        self.assertEqual(rows["claude:haiku"]["answered"], 12)
        self.assertEqual(rows["claude:haiku"]["flip"]["pairs"], 12)
        self.assertIsNotNone(rows["claude:haiku"]["overhead_seconds"])
        self.assertEqual(len(payload["summary"]["agreement"]), 1)
        for text in (out, err, next(self.output.glob("benchmark-*.json")).read_text()):
            self.assertNotIn("ts-FAKE-KEY", text)

    def test_a_failed_overhead_probe_does_not_lose_the_run(self):
        def run(command, input, capture_output, text, timeout, env):
            if input == bench.OVERHEAD_PROMPT:
                return Completed("", 1, "rate limited")
            items = [{"id": item["id"], "contradicts": False, "covers": True, "staleness": 0} for item in json.loads(input)["items"]]
            return Completed(claude_envelope(items))

        code, out, err = self.run_main(["--contenders", "claude:haiku", "--limit", "3", "--runs", "1", "--json"], run=run)
        self.assertEqual(code, 0, err)
        self.assertIn("could not be measured", err)
        row = json.loads(out)["summary"]["rows"][0]
        self.assertEqual((row["answered"], row["overhead_seconds"]), (3, None))

    def test_typesafe_live_needs_the_key_and_uses_the_mocked_transport(self):
        code, out, err = self.run_main(["--contenders", "claude:haiku,typesafe-live"], environ={})
        self.assertEqual(code, 2, "the key is checked before any paid contender is spawned")
        self.assertIn("TYPESAFE_API_KEY is not set", err)
        self.assertEqual(out, "")
        with patch.object(bench.drift, "HttpTransport", return_value=lambda payload: response(0.9, 0.9, tokens=3)):
            code, out, err = self.run_main(["--contenders", "typesafe-live", "--limit", "4", "--json"],
                                           environ={"TYPESAFE_API_KEY": "ts-FAKE-KEY-0123456789"})
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(len(payload["runs"]["typesafe-live"]), 2)
        self.assertEqual(payload["summary"]["rows"][0]["flip"]["rate"], 0.0)
        self.assertEqual(payload["summary"]["rows"][0]["input_tokens"], 12)

    def test_saved_results_can_be_rendered_again_with_the_gold_they_were_scored_on(self):
        custom = Path(self.temporary.name) / "gold.json"
        first_key = json.loads(WORKLOAD.read_text())["pairs"][0]["key"]
        custom.write_text(json.dumps({"labeled_by": "someone else", "labels": {first_key: {"contradicts": False, "covers": True}}}))
        code, _out, _err = self.run_main(["--gold", str(custom)])
        self.assertEqual(code, 0)
        saved = next(self.output.glob("benchmark-*.json"))
        code, out, _err = self.run_main(["--from-results", str(saved), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual((payload["gold"]["pairs"], payload["gold"]["labeled_by"]), (1, "someone else"))
        self.assertEqual(payload["summary"]["rows"][0]["gold"]["fp"], 1)
        code, out, _err = self.run_main(["--from-results", str(saved)])
        self.assertIn("typesafe-recorded", out)
        self.assertEqual(len(list(self.output.glob("benchmark-*.json"))), 1)

    def test_record_mode_writes_a_workload_and_bad_input_is_a_clean_error(self):
        source = Path(self.temporary.name) / "record.json"
        source.write_text(json.dumps(record()))
        target = Path(self.temporary.name) / "workload.json"
        code, out, err = self.run_main(["--record", str(source), "--write-workload", str(target), "--limit", "6"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(target.read_text())["pairs"]), 6)
        code, _out, err = self.run_main(["--workload", str(source)])
        self.assertEqual(code, 1)
        self.assertIn("'pairs'", err)
        with self.assertRaises(SystemExit):
            with patch("sys.stderr", io.StringIO()):
                bench.parse_args(["--contenders", "gpt:4"])


if __name__ == "__main__":
    unittest.main()
