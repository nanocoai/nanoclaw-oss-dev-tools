#!/usr/bin/env python3
"""Benchmark: TypeSafe System One versus a prompt-and-parse LLM on the same docs-drift pairs.

Every contender judges the same fact x section pairs (same evidence text, same
doc section text) taken from a recorded ``typesafe-docs-drift.py --record`` run:

* ``typesafe-recorded``  the recorded answers, no network.
* ``typesafe-live``      the same requests sent again, sequentially, for timing.
* ``claude:<model>``     headless ``claude -p`` with tools disabled, 10 pairs per call,
                         strict JSON out (contradicts, covers, staleness).
* ``codex``              ``codex exec`` in a read-only sandbox, same prompt (fallback).

Only ``typesafe-recorded`` runs by default; every contender that uses the
network or spawns a CLI must be named in ``--contenders``. The script reports
wall time, per-pair latency, tokens, cost, parse failures, run-to-run flips,
pairwise agreement, and precision/recall on ``contradicts`` against a
hand-labeled gold file. ``TYPESAFE_API_KEY`` is read from the environment only
for ``typesafe-live``, is never printed, and is removed from the environment of
every spawned CLI.
"""

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys
import time


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_WORKLOAD = SKILL_DIR / "fixtures" / "benchmark-workload.json"
DEFAULT_GOLD = SKILL_DIR / "fixtures" / "benchmark-gold.json"
DEFAULT_OUTPUT_DIR = SKILL_DIR / "output"
DEFAULT_LIMIT = 60
DEFAULT_SEED = 20260916
DEFAULT_BATCH = 10
SYSTEM_PROMPT = (
    "You are a documentation drift checker. You compare one fact derived from source code with one "
    "documentation section. You answer with JSON only: no prose, no code fences."
)
OVERHEAD_PROMPT = "Reply with the single word OK."


def load_drift():
    spec = importlib.util.spec_from_file_location("typesafe_docs_drift", SCRIPT_DIR / "typesafe-docs-drift.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


drift = load_drift()
BenchmarkError = drift.DriftError


# ---------------------------------------------------------------------------
# Workload
# ---------------------------------------------------------------------------

def build_workload(record, results=None, limit=DEFAULT_LIMIT, seed=DEFAULT_SEED, thresholds=None):
    """Stratified sample of recorded pairs: DRIFT pairs of DRIFT facts, the best pair of every
    MISSING and UNSURE fact, then a seeded random fill with the best pair of OK facts."""
    thresholds = thresholds or {"contradicts": 0.7, "covers": 0.7, "staleness": 0.5}
    sections = {section["id"]: section for section in record["sections"]}
    elapsed = {}
    for result in (results or {}).get("results", []):
        for pair in result.get("pairs", []):
            elapsed[pair["key"]] = pair.get("elapsed_seconds")
    strata = {"DRIFT": [], "MISSING": [], "UNSURE": [], "OK": []}
    for entry in record["facts"]:
        pairs = []
        for section_id in record["candidates"].get(entry["id"], []):
            key = drift.pair_key(entry["id"], section_id)
            response = record["responses"].get(key)
            if response is None or section_id not in sections:
                continue
            pairs.append({"key": key, "section": {"id": section_id}, "response": response,
                          "decision": drift.decide_pair(response.get("answers") or {}, thresholds)})
        verdict = drift.decide_fact(entry, pairs, thresholds)
        if not pairs:
            continue
        if verdict["verdict"] == "DRIFT":
            chosen = [pair for pair in pairs if pair["decision"]["verdict"] == "DRIFT"]
        else:
            chosen = [pair for pair in pairs if pair["section"]["id"] == verdict["best"]["section"]["id"]][:1]
        for pair in chosen:
            strata[verdict["verdict"]].append({
                "key": pair["key"],
                "stratum": verdict["verdict"],
                "fact": {name: entry[name] for name in ("id", "area", "statement", "evidence")},
                "section_id": pair["section"]["id"],
                "recorded_response": pair["response"],
                "recorded_elapsed_seconds": elapsed.get(pair["key"]),
            })
    picked = strata["DRIFT"] + strata["MISSING"] + strata["UNSURE"]
    if limit and len(picked) > limit:
        picked = picked[:limit]
    room = max(0, (limit or len(picked) + len(strata["OK"])) - len(picked))
    fill = sorted(strata["OK"], key=lambda pair: pair["key"])
    random.Random(seed).shuffle(fill)
    picked = picked + fill[:room]
    used = sorted({pair["section_id"] for pair in picked})
    return {
        "schema_version": 1,
        "source": {"recorded_at": record.get("recorded_at"), "code_commit": record.get("code_commit"),
                   "docs_commit": record.get("docs_commit")},
        "seed": seed,
        "limit": limit,
        "strata": {name: sum(1 for pair in picked if pair["stratum"] == name) for name in strata},
        "sections": [{name: sections[section_id][name] for name in ("id", "page", "title", "heading", "line", "text")}
                     for section_id in used],
        "pairs": picked,
    }


def load_json(path, what):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkError("could not read %s %s: %s" % (what, path, error))


def load_workload(path, limit=0):
    data = load_json(path, "workload")
    if not isinstance(data, dict) or "pairs" not in data or "sections" not in data:
        raise BenchmarkError("workload %s must be an object with 'pairs' and 'sections'" % path)
    if limit and limit < len(data["pairs"]):
        data = dict(data, pairs=data["pairs"][:limit])
    return data


def pair_state(pair, sections):
    section = sections[pair["section_id"]]
    return drift.build_state(dict(pair["fact"], terms=[]), section)


# ---------------------------------------------------------------------------
# Contenders
# ---------------------------------------------------------------------------

def verdict_from_answers(answers, gate):
    """Binarize TypeSafe answers the way the skill's gate does: yes means probability >= gate."""
    def real(value, low, high):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        return float(value) if low <= value <= high else None

    # Same bar as the LLM parser: every field present, correctly typed and in range, or the pair is a parse failure.
    def field(question, name):
        answer = answers.get(question) if isinstance(answers, dict) else None
        return answer.get(name) if isinstance(answer, dict) else None

    contradicts = real(field("contradicts", "noul"), 0, 1)
    covers = real(field("covers", "noul"), 0, 1)
    staleness = real(field("staleness", "score"), 0, len(drift.STALENESS_LEVELS) - 1)
    if contradicts is None or covers is None or staleness is None:
        return None
    return {"contradicts": contradicts >= gate, "covers": covers >= gate,
            "staleness": int(math.floor(staleness + 0.5)),
            "p_contradicts": round(contradicts, 3), "p_covers": round(covers, 3)}


def count(value):
    """A token count from an API payload; anything that is not a plain non-negative number counts as zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return 0
    return int(value)


def empty_run(name):
    return {"contender": name, "verdicts": {}, "latencies": [], "adjusted_latencies": [], "wall_seconds": 0.0,
            "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "parse_failures": 0, "format_violations": 0,
            "calls": 0, "failed_calls": 0, "overhead_seconds": None, "errors": []}


def run_typesafe_recorded(workload, gate):
    run = empty_run("typesafe-recorded")
    for pair in workload["pairs"]:
        response = pair["recorded_response"]
        response = response if isinstance(response, dict) else {}
        verdict = verdict_from_answers(response.get("answers"), gate)
        if verdict is None:
            run["parse_failures"] += 1
        else:
            run["verdicts"][pair["key"]] = verdict
        if pair.get("recorded_elapsed_seconds") is not None:
            run["latencies"].append(pair["recorded_elapsed_seconds"])
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        run["input_tokens"] += count(usage.get("input_tokens"))
        run["output_tokens"] += count(usage.get("output_tokens"))
        run["calls"] += 1
    # The recorded run was concurrent; the sum of request latencies is the sequential-equivalent wall time.
    if len(run["latencies"]) == len(workload["pairs"]):
        run["wall_seconds"] = round(sum(run["latencies"]), 3)
    else:  # no --results file, or only some latencies: a partial sum would pose as the whole workload
        run["wall_seconds"] = None
        run["latencies"] = []
    run["adjusted_latencies"] = list(run["latencies"])
    return run


def run_typesafe_live(workload, gate, transport, model=None, log=None):
    run = empty_run("typesafe-live")
    sections = {section["id"]: section for section in workload["sections"]}
    started = time.monotonic()
    for index, pair in enumerate(workload["pairs"], 1):
        payload = {"state": pair_state(pair, sections), "model": model or drift.DEFAULT_MODEL,
                   "questions": drift.build_questions(), "_key": pair["key"]}
        begun = time.monotonic()
        run["calls"] += 1
        try:
            response = transport(payload)
        except BenchmarkError as error:
            run["latencies"].append(round(time.monotonic() - begun, 3))
            run["errors"].append("%s: %s" % (pair["key"], error))
            run["parse_failures"] += 1
            run["failed_calls"] += 1
            continue
        latency = time.monotonic() - begun
        run["latencies"].append(round(latency, 3))
        response = response if isinstance(response, dict) else {}
        verdict = verdict_from_answers(response.get("answers"), gate)
        if verdict is None:
            run["parse_failures"] += 1
        else:
            run["verdicts"][pair["key"]] = verdict
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        run["input_tokens"] += count(usage.get("input_tokens"))
        run["output_tokens"] += count(usage.get("output_tokens"))
        if log:
            log("[typesafe-live %d/%d] %.1fs" % (index, len(workload["pairs"]), latency))
    run["wall_seconds"] = round(time.monotonic() - started, 3)
    run["adjusted_latencies"] = list(run["latencies"])
    return run


def build_llm_prompt(batch, sections):
    questions = drift.build_questions()
    items = []
    for number, pair in enumerate(batch, 1):
        state = pair_state(pair, sections)
        items.append({"id": "p%02d" % number, "fact": state["fact"], "doc_section": state["doc_section"]})
    instructions = {
        "task": "For every item, judge `doc_section` against `fact`. The three judgments are independent.",
        "contradicts": {"question": questions["contradicts"]["instructions"], "true": questions["contradicts"]["criteria"]["true"],
                        "false": questions["contradicts"]["criteria"]["false"]},
        "covers": {"question": questions["covers"]["instructions"], "true": questions["covers"]["criteria"]["true"],
                   "false": questions["covers"]["criteria"]["false"]},
        "staleness": {"question": questions["staleness"]["instructions"],
                      "levels": {str(index): text for index, text in enumerate(questions["staleness"]["criteria"])}},
        "output": ("Return one JSON array and nothing else, with exactly one object per item, in order: "
                   '{"id": "<item id>", "contradicts": true|false, "covers": true|false, "staleness": 0|1|2|3}'),
    }
    return json.dumps({"instructions": instructions, "items": items}, indent=1, ensure_ascii=False)


def is_strict_json_array(text):
    """True when the whole reply is one JSON array, as the prompt demands (no prose, no code fence)."""
    try:
        return isinstance(json.loads((text or "").strip()), list)
    except json.JSONDecodeError:
        return False


def parse_llm_answer(text, expected):
    """Return {item id: verdict} for the valid objects in the model's reply; anything else is a parse failure.

    Recovery is what a real pipeline would do (strip a fence, take the outermost array); whether the reply
    obeyed the strict-JSON contract is reported separately by ``is_strict_json_array``. An id answered twice
    is ambiguous and dropped."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    verdicts = {}
    seen = set()
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] not in expected:
            continue
        if item["id"] in seen:
            verdicts.pop(item["id"], None)
            continue
        seen.add(item["id"])
        contradicts, covers, staleness = item.get("contradicts"), item.get("covers"), item.get("staleness")
        if not isinstance(contradicts, bool) or not isinstance(covers, bool):
            continue
        if isinstance(staleness, bool) or not isinstance(staleness, int) or not 0 <= staleness <= 3:
            continue
        verdicts[item["id"]] = {"contradicts": contradicts, "covers": covers, "staleness": staleness}
    return verdicts


def child_environment(environ):
    return {name: value for name, value in environ.items() if name != drift.KEY_ENV}


_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)


def redact_secrets(text, environ):
    """Blank any secret-looking environment value a CLI echoed into its diagnostics."""
    for name, value in environ.items():
        if value and len(value) >= 8 and _SECRET_NAME.search(name):
            text = text.replace(value, "[redacted]")
    return text


class CliError(BenchmarkError):
    """A failed CLI call; ``usage`` holds whatever the CLI still reported, so failures are not free."""

    def __init__(self, message, usage=None):
        super().__init__(message)
        self.usage = usage


class Finished:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def spawn(command, input=None, capture_output=True, text=True, timeout=None, env=None):
    """Like subprocess.run, but in its own process group so a timeout also stops the CLI's children."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=text, env=env, start_new_session=True)
    try:
        stdout, stderr = process.communicate(input, timeout=timeout)
    except BaseException:  # timeout, Ctrl+C, anything: the detached group must not keep spending tokens
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        try:
            process.communicate(timeout=5)
        except Exception:
            pass
        raise
    return Finished(process.returncode, stdout, stderr)


def cli_command(contender):
    kind, _, model = contender.partition(":")
    if kind == "claude":
        return ["claude", "-p", "--model", model or "haiku", "--output-format", "json", "--tools", "",
                "--strict-mcp-config", "--no-session-persistence", "--system-prompt", SYSTEM_PROMPT]
    if kind == "codex":
        command = ["codex", "exec", "--ephemeral", "-c", 'sandbox_mode="read-only"']
        if model:
            command += ["-m", model]
        return command + ["-"]
    raise BenchmarkError("unknown contender %s" % contender)


def envelope_usage(envelope):
    raw = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    cost = envelope.get("total_cost_usd")
    return {"input_tokens": sum(count(raw.get(name)) for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
            "output_tokens": count(raw.get("output_tokens")),
            "cost_usd": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None}


def claude_usage(stdout):
    """Usage from whatever envelope a failed ``claude -p`` still printed, or None."""
    try:
        envelope = json.loads(stdout or "")
    except json.JSONDecodeError:
        return None
    if isinstance(envelope, list):
        envelope = next((event for event in reversed(envelope) if isinstance(event, dict) and event.get("type") == "result"), None)
    return envelope_usage(envelope) if isinstance(envelope, dict) else None


def call_cli(contender, prompt, run, environ, timeout):
    """Spawn the CLI once; return (reply text, usage dict)."""
    command = cli_command(contender)
    try:
        result = run(command, input=prompt, capture_output=True, text=True, timeout=timeout, env=child_environment(environ))
    except FileNotFoundError:
        raise BenchmarkError("%s is not on PATH" % command[0])
    except subprocess.TimeoutExpired:
        raise BenchmarkError("%s timed out after %ss" % (command[0], timeout))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise CliError("%s exited %d: %s" % (
            command[0], result.returncode, redact_secrets(detail[-1], environ)[:200] if detail else "no output"),
            claude_usage(result.stdout) if contender.startswith("claude") else None)
    if contender.startswith("claude"):
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise BenchmarkError("claude -p did not return JSON")
        if isinstance(envelope, list):  # some versions emit the event list; the result event is last
            envelope = next((event for event in reversed(envelope) if isinstance(event, dict) and event.get("type") == "result"), {})
        if not isinstance(envelope, dict):
            raise BenchmarkError("claude -p returned JSON that is not a result object")
        usage = envelope_usage(envelope)
        if envelope.get("is_error"):
            raise CliError("claude -p reported an error: %s" % redact_secrets(str(envelope.get("result")), environ)[:200], usage)
        reply = envelope.get("result")
        return reply if isinstance(reply, str) else "", usage
    # codex exec prints plain text; its token use is not machine-readable here, so it is unknown rather than zero.
    return result.stdout, {"input_tokens": None, "output_tokens": None, "cost_usd": None}


def add_usage(outcome, usage):
    if not usage:
        return
    for name in ("input_tokens", "output_tokens"):
        if usage.get(name) is None:
            outcome[name] = None  # unknown for this CLI; never report it as zero
        elif outcome[name] is not None:
            outcome[name] += usage[name]
    if usage.get("cost_usd") is not None:
        outcome["cost_usd"] = round((outcome["cost_usd"] or 0) + usage["cost_usd"], 6)


def run_llm(contender, workload, run=spawn, environ=None, batch_size=DEFAULT_BATCH, timeout=600,
            overhead=None, log=None):
    environ = os.environ if environ is None else environ
    outcome = empty_run(contender)
    outcome["overhead_seconds"] = overhead
    sections = {section["id"]: section for section in workload["sections"]}
    pairs = workload["pairs"]
    started = time.monotonic()
    for offset in range(0, len(pairs), batch_size):
        batch = pairs[offset: offset + batch_size]
        ids = {"p%02d" % number: pair["key"] for number, pair in enumerate(batch, 1)}
        begun = time.monotonic()
        outcome["calls"] += 1
        failure = None
        try:
            reply, usage = call_cli(contender, build_llm_prompt(batch, sections), run, environ, timeout)
        except BenchmarkError as error:
            failure = error
        wall = time.monotonic() - begun
        outcome["latencies"].extend([round(wall / len(batch), 3)] * len(batch))
        outcome["adjusted_latencies"].extend([round(max(0.0, wall - (overhead or 0.0)) / len(batch), 3)] * len(batch))
        if failure is not None:
            outcome["errors"].append("batch %d: %s" % (offset // batch_size + 1, failure))
            outcome["parse_failures"] += len(batch)
            outcome["failed_calls"] += 1
            add_usage(outcome, getattr(failure, "usage", None))
            continue
        if not is_strict_json_array(reply):
            outcome["format_violations"] += 1
        add_usage(outcome, usage)
        verdicts = parse_llm_answer(reply, ids)
        outcome["parse_failures"] += len(batch) - len(verdicts)
        for item_id, verdict in verdicts.items():
            outcome["verdicts"][ids[item_id]] = verdict
        if log:
            log("[%s batch %d] %d pairs in %.1fs, %d parsed" % (contender, offset // batch_size + 1, len(batch), wall, len(verdicts)))
    outcome["wall_seconds"] = round(time.monotonic() - started, 3)
    return outcome


def measure_overhead(contender, run=spawn, environ=None, timeout=300):
    """Wall time of one trivial call: process start, auth, and one minimal API round trip."""
    environ = os.environ if environ is None else environ
    begun = time.monotonic()
    call_cli(contender, OVERHEAD_PROMPT, run, environ, timeout)
    return round(time.monotonic() - begun, 3)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = int(math.floor(position)), int(math.ceil(position))
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 3)


def flip_rate(first, second, field="contradicts"):
    """Share of pairs answered in both runs whose verdict differs, with that denominator."""
    shared = [key for key in first["verdicts"] if key in second["verdicts"]]
    if not shared:
        return None
    flips = sum(1 for key in shared if first["verdicts"][key][field] != second["verdicts"][key][field])
    return {"pairs": len(shared), "flips": flips, "rate": round(flips / len(shared), 4)}


def agreement(first, second, field):
    shared = [key for key in first["verdicts"] if key in second["verdicts"]]
    if not shared:
        return None
    same = sum(1 for key in shared if first["verdicts"][key][field] == second["verdicts"][key][field])
    return {"pairs": len(shared), "rate": round(same / len(shared), 4)}


def precision_recall(run, gold, keys, field="contradicts"):
    """Gold pairs in the workload only. A pair the contender failed to answer counts as a predicted no."""
    tp = fp = fn = tn = 0
    for key in keys:
        label = gold.get(key)
        if label is None:
            continue
        predicted = bool((run["verdicts"].get(key) or {}).get(field))
        actual = bool(label[field])
        if predicted and actual:
            tp += 1
        elif predicted:
            fp += 1
        elif actual:
            fn += 1
        else:
            tn += 1
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None}


def summarize(runs_by_contender, gold, keys):
    rows = []
    for name, runs in runs_by_contender.items():
        first = runs[0]
        rows.append({
            "contender": name,
            "pairs": len(keys),
            "answered": len(first["verdicts"]),
            "wall_seconds": first["wall_seconds"],
            "p50_seconds": percentile(first["latencies"], 0.5),
            "p95_seconds": percentile(first["latencies"], 0.95),
            "p50_adjusted_seconds": percentile(first["adjusted_latencies"], 0.5),
            "overhead_seconds": first["overhead_seconds"],
            "input_tokens": first["input_tokens"],
            "output_tokens": first["output_tokens"],
            "cost_usd": first["cost_usd"],
            "parse_failures": first["parse_failures"],
            "parse_failures_by_run": [entry["parse_failures"] for entry in runs],
            "format_violations_by_run": [entry.get("format_violations", 0) for entry in runs],
            "failed_calls_by_run": [entry.get("failed_calls", 0) for entry in runs],
            "flip": flip_rate(first, runs[1]) if len(runs) > 1 else None,
            "gold": precision_recall(first, gold, keys),
            "gold_covers": precision_recall(first, gold, keys, "covers"),
            "errors": first["errors"][:5],
        })
    names = list(runs_by_contender)
    pairwise = []
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            pairwise.append({"a": left, "b": right,
                             "contradicts": agreement(runs_by_contender[left][0], runs_by_contender[right][0], "contradicts"),
                             "covers": agreement(runs_by_contender[left][0], runs_by_contender[right][0], "covers")})
    return {"rows": rows, "agreement": pairwise, "gold_pairs": sum(1 for key in keys if key in gold)}


def fmt(value, pattern="%s", missing="-"):
    return missing if value is None else pattern % value


def render(summary):
    columns = ("Contender", "Pairs", "Wall s", "p50 s/pair", "p50 adj", "Tokens in/out", "Cost $", "Parse fail", "Flip %", "Precision", "Recall")
    rows = []
    for row in summary["rows"]:
        rows.append((
            row["contender"], str(row["pairs"]), fmt(row["wall_seconds"], "%.1f"), fmt(row["p50_seconds"], "%.2f"),
            fmt(row["p50_adjusted_seconds"], "%.2f"), "%s / %s" % (fmt(row["input_tokens"], "%d"), fmt(row["output_tokens"], "%d")),
            fmt(row["cost_usd"], "%.4f"), "/".join(str(count) for count in row["parse_failures_by_run"]),
            "-" if row["flip"] is None else "%.1f (%d/%d)" % (row["flip"]["rate"] * 100, row["flip"]["flips"], row["flip"]["pairs"]),
            fmt(row["gold"]["precision"], "%.2f"), fmt(row["gold"]["recall"], "%.2f"),
        ))
    lines = [drift.table(columns, rows), "",
             "Parse fail is per run (pairs). Flip % is over the pairs answered in both runs (flips/pairs).",
             "Precision and recall are for `contradicts` on %d hand-labeled gold pairs." % summary["gold_pairs"]]
    for row in summary["rows"]:
        if any(row["format_violations_by_run"]) or any(row["failed_calls_by_run"]):
            lines.append("%s: replies that were not a bare JSON array per run %s; failed calls per run %s" % (
                row["contender"], row["format_violations_by_run"], row["failed_calls_by_run"]))
    for entry in summary["agreement"]:
        lines.append("agreement %s vs %s: contradicts %s, covers %s" % (
            entry["a"], entry["b"],
            fmt(entry["contradicts"] and entry["contradicts"]["rate"] * 100, "%.1f%%"),
            fmt(entry["covers"] and entry["covers"]["rate"] * 100, "%.1f%%")))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workload", default=str(DEFAULT_WORKLOAD), help="sampled pairs to judge (default: the shipped fixture)")
    parser.add_argument("--record", help="build the workload from this typesafe-docs-drift --record file instead")
    parser.add_argument("--results", help="with --record: the run's saved drift-*.json, for recorded per-request latencies")
    parser.add_argument("--write-workload", help="with --record: save the sampled workload here and exit")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="pairs in the sample (default %d)" % DEFAULT_LIMIT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="seed for the OK fill (default %d)" % DEFAULT_SEED)
    parser.add_argument("--gold", help="hand-labeled gold file (default: the shipped fixture; with --from-results: the labels saved in that file)")
    parser.add_argument("--contenders", default="typesafe-recorded",
                        help="comma-separated: typesafe-recorded (offline), typesafe-live, claude:<model>, codex[:<model>]")
    parser.add_argument("--runs", type=int, default=2, help="runs per live contender, for the flip rate (default 2)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH, help="pairs per LLM call (default %d)" % DEFAULT_BATCH)
    parser.add_argument("--gate", type=float, default=0.7, help="TypeSafe yes threshold on the noul probabilities (default 0.7)")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds per CLI call or HTTP request")
    parser.add_argument("--from-results", help="re-render a saved benchmark-*.json; runs nothing")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.runs < 1 or args.batch_size < 1 or args.limit < 0:
        parser.error("--runs and --batch-size must be at least 1 and --limit at least 0")
    if not 0.5 <= args.gate <= 1:
        parser.error("--gate must be between 0.5 and 1")
    args.contender_list = [name.strip() for name in args.contenders.split(",") if name.strip()]
    for name in args.contender_list:
        if name not in ("typesafe-recorded", "typesafe-live") and name.partition(":")[0] not in ("claude", "codex"):
            parser.error("unknown contender %s" % name)
    return args


def main(argv=None, environ=None, stdout=None, stderr=None, run=spawn):
    environ = os.environ if environ is None else environ
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    args = parse_args(argv)
    log = lambda line: print(line, file=stderr)
    try:
        if args.from_results:
            payload = load_json(args.from_results, "benchmark results")
            if args.gold:
                gold_file = load_json(args.gold, "gold")
                payload["gold"] = {"labeled_by": gold_file.get("labeled_by"), "labels": gold_file.get("labels", {})}
            gold = (payload.get("gold") or {}).get("labels", {})
            payload["summary"] = summarize(payload["runs"], gold, payload["keys"])
            payload["gold"]["pairs"] = payload["summary"]["gold_pairs"]
            print(json.dumps(payload, indent=2) if args.json else render(payload["summary"]), file=stdout)
            return 0
        if args.record:
            workload = build_workload(load_json(args.record, "record"), load_json(args.results, "results") if args.results else None,
                                      args.limit, args.seed)
            if args.write_workload:
                Path(args.write_workload).write_text(json.dumps(workload, indent=1, ensure_ascii=False) + "\n")
                print("wrote %d pairs (%s) to %s" % (len(workload["pairs"]), workload["strata"], args.write_workload), file=stdout)
                return 0
        else:
            workload = load_workload(args.workload, args.limit)
        gold_path = args.gold or str(DEFAULT_GOLD)
        gold_file = load_json(gold_path, "gold") if Path(gold_path).exists() else {"labels": {}}
        gold = gold_file.get("labels", {})
        keys = [pair["key"] for pair in workload["pairs"]]
        runs_by_contender = {}
        if "typesafe-live" in args.contender_list and not environ.get(drift.KEY_ENV):
            # Checked before anything runs, so a paid contender earlier in the list is never wasted.
            print("%s is not set; typesafe-live needs it exported in this shell. It is never read from a file." % drift.KEY_ENV, file=stderr)
            return 2
        for name in args.contender_list:
            if name == "typesafe-recorded":
                runs_by_contender[name] = [run_typesafe_recorded(workload, args.gate)]
            elif name == "typesafe-live":
                transport = drift.HttpTransport(environ.get(drift.KEY_ENV), timeout=args.timeout)
                runs_by_contender[name] = [run_typesafe_live(workload, args.gate, transport, log=log) for _ in range(args.runs)]
            else:
                try:
                    overhead = measure_overhead(name, run, environ, args.timeout)
                    log("[%s] startup overhead %.1fs" % (name, overhead))
                except BenchmarkError as error:
                    overhead = None
                    log("[%s] startup overhead could not be measured (%s); adjusted latency equals raw" % (name, error))
                runs_by_contender[name] = [run_llm(name, workload, run, environ, args.batch_size, args.timeout, overhead, log)
                                           for _ in range(args.runs)]
    except BenchmarkError as error:
        print("error: %s" % error, file=stderr)
        return 1
    except Exception as error:  # never let a traceback carry request details to the terminal
        print("error: unexpected %s: %s" % (type(error).__name__, redact_secrets(drift.redact_env(str(error), environ), environ)), file=stderr)
        return 1
    summary = summarize(runs_by_contender, gold, keys)
    payload = {
        "schema_version": 1,
        "generated_at": drift.utc_now().isoformat(),
        "workload": {"source": workload.get("source"), "seed": workload.get("seed"), "pairs": len(keys), "strata": workload.get("strata")},
        "gold": {"pairs": summary["gold_pairs"], "labeled_by": gold_file.get("labeled_by"),
                 "labels": {key: gold[key] for key in keys if key in gold}},
        "gate": args.gate,
        "batch_size": args.batch_size,
        "keys": keys,
        "summary": summary,
        "runs": runs_by_contender,
    }
    output_path = drift.write_output(args.output_dir, payload, prefix="benchmark")
    if args.json:
        print(json.dumps(payload, indent=2), file=stdout)
    else:
        print(render(summary), file=stdout)
        print("\nraw runs: %s" % output_path, file=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
