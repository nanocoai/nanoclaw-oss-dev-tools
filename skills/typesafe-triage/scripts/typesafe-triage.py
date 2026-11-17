#!/usr/bin/env python3
"""Issue and PR triage for nanocoai/nanoclaw using the TypeSafe decision API.

Fetches recently updated open issues and pull requests with ``gh``, sends one
fan-out request per item to TypeSafe System One (area, kind, priority, and a
yes/no question), applies a confidence gate, and prints the proposed labels
next to the existing ones. Dry run by default: nothing is written to GitHub.
Raw answers are saved as JSON under a gitignored output directory.

``--apply`` adds the two labels that measured 100% agreement on a live run:
the ungated ``kind/*`` proposal (when the item has no existing ``kind/*``
label) and, on issues only, ``triage/needs-repro`` when ``needs_repro``
resolved yes and the label is not already present. Nothing is ever removed,
and area/priority/pr_ready are never applied or touched.

``--fixture <file>`` replays recorded items and API responses so the whole
pipeline runs offline. ``TYPESAFE_API_KEY`` is read from the environment only
when the live API is used; it is never read from a file and never printed.
"""

import argparse
import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request


API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_REPO = "nanocoai/nanoclaw"
KEY_ENV = "TYPESAFE_API_KEY"
SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = SKILL_DIR / "output"
BODY_LIMIT = 12000
FILE_LIMIT = 80
UNRESOLVED = "triage/unresolved"
NEEDS_REPRO = "triage/needs-repro"
NEEDS_AUTHOR = "triage/needs-author"
NEEDS_REVIEW = "Status: Needs Review"
PR_TEMPLATE_MARKER = "nanoclaw-pr-template:v2"
PR_TEMPLATE_SECTIONS = (
    "Summary",
    "Related work",
    "Change kind",
    "Validation",
    "User and release impact",
    "Security and trust boundaries",
    "Skill delivery",
    "AI assistance",
)

# Rubrics come from the NanoClaw label descriptions (`gh label list`), CLAUDE.md,
# docs/ and the path -> area mapping in .github/labeler.yml. One line per option.
AREA_CRITERIA = {
    "area/agent-memory": (
        "Persistent per-agent-group memory: the Markdown memory tree, memory migration, "
        "memory hooks and memory add-ons (container/agent-runner/src/memory, docs/memory.md, "
        "migrate-memory, add-mnemon, add-karpathy-llm-wiki)."
    ),
    "area/agent-runner": (
        "The Bun agent-runner inside the container: poll loop, prompt formatting, provider "
        "abstraction, media handling, status management (container/agent-runner/**, "
        "docs/agent-runner-details.md, docs/SDK_DEEP_DIVE.md)."
    ),
    "area/channels": (
        "Messaging platform adapters and channel install skills: Slack, Discord, Telegram, "
        "WhatsApp, Signal, iMessage, Teams, Matrix, GitHub, Linear, email, interactive and "
        "typing modules, channel pairing/auth setup (src/channels, setup/channels, add-<channel>)."
    ),
    "area/configuration": (
        "Host and container configuration: env parsing, container_configs, group folder and "
        "persona scaffolding, project-doc composition, timezone, .env.example, config examples, "
        "the customize skill (src/config*, src/container-config*, src/project-doc-compose*)."
    ),
    "area/containers": (
        "Docker/container runtime: Dockerfile, image build and pull, container-runner spawn and "
        "restart, mounts, drivers, egress lockdown, self-mod apply, image workflows "
        "(container/Dockerfile, container/build.sh, src/container-runner*, src/drivers, manage-mounts)."
    ),
    "area/core": (
        "Host orchestration core: entry point, inbound router, outbound delivery and delivery guard, "
        "host sweep, lifecycle, circuit breaker, webhook server, mailbox and DB layer, agent-to-agent, "
        "cross-session context (src/index, src/router, src/delivery*, src/host-sweep, src/db, src/mailbox)."
    ),
    "area/credentials": (
        "Credential gateway seam and gateway providers: OneCLI/Iron Proxy integration, vault, "
        "credential helper, registry login, gateway approvals bridge (src/gateway-providers, "
        "setup/onecli*, container/skills/onecli-gateway, docs/onecli-upgrades.md)."
    ),
    "area/ncl-cli": (
        "The ncl admin CLI and its socket/session transport, command gate, resource definitions, "
        "query helper script (src/cli, src/command-gate*, container/agent-runner/src/cli, bin/ncl, scripts/q*)."
    ),
    "area/providers": (
        "Agent providers and their selection/auth: Claude, OpenCode, Codex, Ollama provider "
        "config, provider migration (src/providers, container/agent-runner/src/providers, "
        "setup/providers, setup/provider-auth*, add-codex, add-opencode, add-ollama-provider)."
    ),
    "area/repository-maintenance": (
        "Repository plumbing rather than product behavior: GitHub workflows and templates, husky, "
        "lint/tsconfig/vitest config, package.json and lockfiles, READMEs, CHANGELOG, CONTRIBUTING, "
        "release scripts, versions.json, CLAUDE.md/AGENTS.md (.github/**, package.json, README*, scripts/release*)."
    ),
    "area/scheduled-tasks": (
        "Scheduled and recurring tasks: cron recurrence, task runs, ncl tasks resource, "
        "scheduling modules (src/modules/scheduling, container/agent-runner/src/scheduling, docs/scheduled-tasks.md)."
    ),
    "area/security": (
        "Privileged-action guard, approvals, permissions and access resolution, mount security, "
        "attachment and inbox safety, harness tag stripping, SECURITY.md "
        "(src/guard, src/modules/approvals, src/modules/permissions, src/modules/mount-security)."
    ),
    "area/sessions": (
        "Session resolution and lifecycle: session manager, sessions table, per-session inbound/outbound "
        "DB files, session cleanup (src/session-manager*, src/db/sessions*, docs/db-session.md)."
    ),
    "area/setup-installation": (
        "Setup wizard and installation lifecycle: nanoclaw.sh, setup/ steps, service (launchd/systemd), "
        "bootstrap, install slug, upgrade state, update-nanoclaw, v1/OpenClaw migration, init-first-agent, "
        "debug skill (setup/**, nanoclaw.sh, migrate-v2.sh, scripts/update*, launchd/**)."
    ),
    "area/skills": (
        "The skills system itself and skill packaging: skill directives engine, skill apply/lint, "
        "registry skills workflow, group skills, container skills directory, skill guidelines docs, "
        "or a new/changed operator skill with no more specific area (.claude/skills/**, scripts/skill-*, docs/skills-model.md)."
    ),
    "area/tools": (
        "Tools the agent calls from inside the container: MCP tool server, plugin MCP, agent-browser, "
        "frontend-engineer, and tool add-on skills such as add-vercel, add-rtk, add-tavily-tool, "
        "add-ollama-tool, add-dashboard, add-clidash (container/agent-runner/src/mcp-tools, .mcp.json)."
    ),
}

KIND_CRITERIA = {
    "kind/bug": "Something is not working as expected: a defect, crash, hang, wrong output or regression.",
    "kind/feature": "New capability or improvement: adds behavior, an option, a channel, provider or skill.",
    "kind/documentation": "Documentation is wrong, missing or unclear; the change is to docs, comments or guidance text.",
    "kind/question": "Usage or design question; asks how something works or should work rather than reporting or changing it.",
    "kind/security": "Exploitable vulnerability crossing a trust boundary on a correctly configured install.",
    "kind/hardening": "Defense-in-depth improvement that is not exploitable across a trust boundary; reduces blast radius or tightens defaults.",
    "kind/cleanup": "Refactor, tidy-up, test-only or tooling change with no user-visible behavior change.",
}

PRIORITY_LABELS = ("priority/low", "priority/medium", "priority/high", "priority/critical")
PRIORITY_CRITERIA = [
    "Low: cosmetic, rare, or easily worked around; affects an optional path or one unusual setup; "
    "no data loss and no security impact.",
    "Medium: a real defect or gap on a supported configuration that has a workaround, or a well-scoped "
    "improvement most operators would notice; can wait for a normal release.",
    "High: breaks a core flow (install, message routing, container spawn, delivery, scheduled runs) "
    "for many users with no clean workaround, or a hardening gap that is actively being discussed.",
    "Critical: data loss, credential or secret exposure, an exploitable trust-boundary vulnerability, "
    "or the service cannot start or run for everyone; must be fixed before the next release.",
]

CHOICE_QUESTIONS = ("area", "kind")
LABEL_FAMILIES = ("area/", "kind/", "priority/")


class TriageError(RuntimeError):
    """A failure the operator can act on; printed without a traceback."""


class PartialFailure(TriageError):
    """A request failed after earlier items succeeded; carries the completed results."""

    def __init__(self, message, failed_key, results, usage):
        super().__init__(message)
        self.failed_key = failed_key
        self.results = results
        self.usage = usage


class ApplyFailure(TriageError):
    """One label write failed after an earlier label for the same item already succeeded."""

    def __init__(self, message, applied):
        super().__init__(message)
        self.applied = applied


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def stamp():
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# GitHub fetch (read-only, via gh api)
# ---------------------------------------------------------------------------

def gh_json(path, run=subprocess.run):
    """Run ``gh api <path>`` and return the parsed JSON body."""
    result = run(["gh", "api", path], capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise TriageError("gh api failed for %s: %s" % (path, detail[-1] if detail else "no output"))
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise TriageError("gh api returned invalid JSON for %s: %s" % (path, error))


def gh_pages(path, run=subprocess.run, per_page=100, max_pages=10, enough=None):
    """Follow ``page=N`` until a short page, ``max_pages`` or ``enough(items)``."""
    collected = []
    joiner = "&" if "?" in path else "?"
    for page in range(1, max_pages + 1):
        batch = gh_json("%s%sper_page=%d&page=%d" % (path, joiner, per_page, page), run)
        if not isinstance(batch, list):
            raise TriageError("gh api returned a non-list body for %s" % path)
        collected.extend(batch)
        if len(batch) < per_page or (enough and enough(collected)):
            break
    return collected


def template_status(body):
    """Report which nanoclaw PR template sections carry content."""
    text = body or ""
    marker = PR_TEMPLATE_MARKER in text
    without_comments = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    headings = re.findall(r"(?m)^#{1,6}\s+(.+?)\s*$", without_comments)
    # Template sections are level-2 headings; deeper headings stay inside their section.
    chunks = re.split(r"(?m)^##\s+", without_comments)
    content = {}
    for chunk in chunks[1:]:
        heading, _, rest = chunk.partition("\n")
        content[heading.strip()] = rest
    sections = {}
    for name in PR_TEMPLATE_SECTIONS:
        rest = content.get(name)
        if rest is None:
            sections[name] = "missing"
            continue
        lines = []
        for line in rest.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("- [ ]") or re.match(r"^#{3,6}\s", stripped):
                continue
            if stripped in ("```release-note", "```"):
                continue
            if stripped.startswith("Optional: one user-facing line"):
                continue
            if stripped == "Closes #":
                continue
            lines.append(stripped)
        sections[name] = "filled" if lines else "empty"
    filled = sum(1 for value in sections.values() if value == "filled")
    return {
        "marker": marker,
        "headings": headings[:40],
        "sections": sections,
        "filled": filled,
        "total": len(PR_TEMPLATE_SECTIONS),
    }


def normalize_issue(raw):
    return {
        "type": "issue",
        "number": raw["number"],
        "title": raw.get("title") or "",
        "body": raw.get("body") or "",
        "author_association": raw.get("author_association") or "NONE",
        "labels": sorted(label["name"] for label in raw.get("labels") or []),
        "url": raw.get("html_url") or "",
        "updated_at": raw.get("updated_at") or "",
        "created_at": raw.get("created_at") or "",
    }


def normalize_pull(raw, files):
    item = normalize_issue(raw)
    item["type"] = "pr"
    item["draft"] = bool(raw.get("draft"))
    item["files"] = [entry["filename"] for entry in files][:FILE_LIMIT]
    item["file_count"] = len(files)
    item["template"] = template_status(item["body"])
    return item


def fetch_items(repo, issue_limit, pull_limit, run=subprocess.run):
    """Return the most recently updated open issues and PRs, newest first."""
    items = []
    if issue_limit > 0:
        # The issues endpoint interleaves pull requests, so keep paging until enough real issues arrive.
        def enough_issues(collected):
            return sum(1 for entry in collected if "pull_request" not in entry) >= issue_limit

        raw_issues = gh_pages("repos/%s/issues?state=open&sort=updated&direction=desc" % repo, run,
                              enough=enough_issues)
        issues = [entry for entry in raw_issues if "pull_request" not in entry]
        items.extend(normalize_issue(entry) for entry in issues[:issue_limit])
    if pull_limit > 0:
        raw_pulls = gh_pages("repos/%s/pulls?state=open&sort=updated&direction=desc" % repo, run,
                             enough=lambda collected: len(collected) >= pull_limit)
        for entry in raw_pulls[:pull_limit]:
            files = gh_pages("repos/%s/pulls/%d/files" % (repo, entry["number"]), run, max_pages=5)
            items.append(normalize_pull(entry, files))
    return items


def parse_iso(value):
    """Parse an ISO-8601 timestamp, accepting a trailing 'Z' (UTC).

    A timestamp with no UTC offset at all (naive) is assumed to already be UTC,
    so it compares cleanly against GitHub's always-offset-aware timestamps
    instead of raising ``TypeError`` at filter time.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def filter_since(items, since):
    """Keep only items created strictly after ``since`` (an ISO-8601 timestamp).

    Items with no recorded ``created_at`` are dropped rather than guessed at.
    """
    if not since:
        return items
    threshold = parse_iso(since)
    kept = []
    for item in items:
        created = item.get("created_at")
        if not created:
            continue
        try:
            when = parse_iso(created)
        except ValueError:
            continue
        if when > threshold:
            kept.append(item)
    return kept


def filter_unlabeled(items):
    """Keep only items that carry no existing ``kind/*`` label."""
    return [item for item in items if not any(label_family(name) == "kind/" for name in item.get("labels") or [])]


# ---------------------------------------------------------------------------
# Questions and state
# ---------------------------------------------------------------------------

def build_state(repo, item):
    state = {
        "repository": repo,
        "item_type": "pull_request" if item["type"] == "pr" else "issue",
        "number": item["number"],
        "title": item["title"],
        "author_association": item["author_association"],
        "body": (item.get("body") or "")[:BODY_LIMIT],
    }
    if item["type"] == "pr":
        state["draft"] = item.get("draft", False)
        state["changed_files"] = item.get("files", [])
        state["changed_file_count"] = item.get("file_count", len(item.get("files", [])))
        template = item.get("template") or template_status(item.get("body"))
        state["template"] = {
            "nanoclaw_template_marker_present": template["marker"],
            "sections": template["sections"],
            "headings_in_body": template["headings"],
        }
    return state


def build_questions(item_type):
    is_pr = item_type == "pr"
    subject = "`title`, `body` and `changed_files`" if is_pr else "`title` and `body`"
    questions = {
        "area": {
            "type": "choice",
            "instructions": (
                "Which single NanoClaw subsystem does this %s primarily belong to, judging from %s? "
                "Pick the one area a maintainer would file it under."
                % ("pull request" if is_pr else "issue", subject)
            ),
            "criteria": dict(AREA_CRITERIA),
        },
        "kind": {
            "type": "choice",
            "instructions": (
                "What kind of change or report is this %s, judging from %s?"
                % ("pull request" if is_pr else "issue", subject)
            ),
            "criteria": dict(KIND_CRITERIA),
        },
        "priority": {
            "type": "score",
            "instructions": (
                "How urgent is this %s for NanoClaw maintainers, judging from the impact described in %s?"
                % ("pull request" if is_pr else "issue", subject)
            ),
            "criteria": list(PRIORITY_CRITERIA),
        },
    }
    if is_pr:
        questions["pr_ready"] = {
            "type": "noul",
            "instructions": (
                "Is this pull request ready for maintainer review: the template sections in "
                "`template.sections` carry real content (not placeholders), `body` describes tests "
                "or validation or gives a reason none is needed, and `changed_files` match the scope "
                "stated in `title`?"
            ),
            "criteria": {
                "true": "Template filled, validation or an explicit reason for none, and the change matches its title.",
                "false": "Template missing or left as placeholders, no validation evidence and no reason given, or the diff is broader or narrower than the title.",
            },
        }
    else:
        questions["needs_repro"] = {
            "type": "noul",
            "instructions": (
                "Does this issue, judging from `body`, lack reproduction steps or a concrete failing case: "
                "no command sequence or configuration that reproduces it, no exact error output, "
                "failing test, or log excerpt?"
            ),
            "criteria": {
                "true": "The report describes the problem abstractly; a maintainer could not reproduce it from what is written.",
                "false": "The report includes concrete steps, a failing command, exact error text, a log excerpt, or a pointer to a failing run.",
            },
        }
    return questions


def build_request(repo, item, model=DEFAULT_MODEL):
    return {
        "state": build_state(repo, item),
        "model": model,
        "questions": build_questions(item["type"]),
    }


def item_key(item):
    return "%s:%d" % (item["type"], item["number"])


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

class HttpTransport:
    """POST to the TypeSafe API with exponential backoff on 429/529."""

    RETRY_STATUSES = (429, 529)

    def __init__(self, api_key, url=API_URL, timeout=60, attempts=4, sleep=time.sleep, opener=None):
        if not api_key:
            raise TriageError("%s is not set" % KEY_ENV)
        if any(character.isspace() for character in api_key) or not api_key.isprintable():
            # Never echo the value: a newline inside a header would otherwise surface in a ValueError.
            raise TriageError("%s contains whitespace or non-printable characters; export the bare key" % KEY_ENV)
        self._api_key = api_key
        self.url = url
        self.timeout = timeout
        self.attempts = attempts
        self.sleep = sleep
        self.opener = opener

    def __call__(self, payload):
        body = json.dumps(payload).encode("utf-8")
        delay = 1.0
        last_error = None
        for attempt in range(1, self.attempts + 1):
            request = urllib.request.Request(
                self.url,
                data=body,
                method="POST",
                headers={
                    "Authorization": "Bearer " + self._api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "nanoclaw-oss-dev-tools typesafe-triage",
                },
            )
            opener = self.opener or urllib.request.urlopen
            try:
                with opener(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
            except urllib.error.HTTPError as error:
                detail = ""
                try:
                    detail = error.read().decode("utf-8", "replace")[:300]
                except Exception:  # pragma: no cover - best effort
                    detail = ""
                finally:
                    error.close()
                last_error = "HTTP %s from TypeSafe: %s" % (error.code, self.redact(detail.strip() or str(error.reason)))
                if error.code not in self.RETRY_STATUSES or attempt == self.attempts:
                    raise TriageError(last_error)
                self.sleep(delay)
                delay *= 2
                continue
            except urllib.error.URLError as error:
                last_error = "could not reach TypeSafe: %s" % self.redact(str(error.reason))
                if attempt == self.attempts:
                    raise TriageError(last_error)
                self.sleep(delay)
                delay *= 2
                continue
            except TimeoutError as error:
                # A read (not connect) timeout raises this directly rather than URLError.
                last_error = "TypeSafe request timed out: %s" % self.redact(str(error) or "timed out")
                if attempt == self.attempts:
                    raise TriageError(last_error)
                self.sleep(delay)
                delay *= 2
                continue
            except Exception as error:
                # Anything else reading the response (a dropped connection mid-body, etc.) is a
                # transient network condition too, not a TypeSafe API error to surface verbatim;
                # retry it the same way, since the request itself has no side effects to worry
                # about duplicating.
                last_error = "TypeSafe request failed: %s" % self.redact(str(error) or type(error).__name__)
                if attempt == self.attempts:
                    raise TriageError(last_error)
                self.sleep(delay)
                delay *= 2
                continue
            try:
                return json.loads(raw)
            except json.JSONDecodeError as error:
                # A 200 with an unparseable body is not a transient condition retries would fix.
                raise TriageError("TypeSafe returned a response that is not valid JSON: %s" % self.redact(str(error)))
        raise TriageError(last_error or "TypeSafe request failed")

    def redact(self, text):
        # Real keys are long; skip tiny values so redaction cannot mangle ordinary words.
        return text.replace(self._api_key, "[redacted]") if len(self._api_key) >= 8 else text


class FixtureTransport:
    """Replay recorded responses keyed by ``<type>:<number>``."""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __call__(self, payload):
        self.requests.append(payload)
        state = payload["state"]
        key = "%s:%d" % ("pr" if state["item_type"] == "pull_request" else "issue", state["number"])
        try:
            return self.responses[key]
        except KeyError:
            raise TriageError("fixture has no recorded response for %s" % key)


def load_fixture(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise TriageError("could not read fixture %s: %s" % (path, error))
    if not isinstance(data, dict) or "items" not in data or "responses" not in data:
        raise TriageError("fixture %s must be an object with 'items' and 'responses'" % path)
    return data


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def label_family(label):
    for prefix in LABEL_FAMILIES:
        if label.startswith(prefix):
            return prefix
    return label


def verdict(label, existing):
    if label in existing:
        return "AGREE"
    family = label_family(label)
    if family in LABEL_FAMILIES and any(label_family(name) == family for name in existing):
        return "DISAGREE"
    return "NEW"


def priority_label(score):
    """Round the score half-up to the nearest level (1.5 -> priority/high)."""
    index = int(math.floor(float(score) + 0.5))
    index = max(0, min(len(PRIORITY_LABELS) - 1, index))
    return PRIORITY_LABELS[index]


def decide(item, answers, thresholds):
    """Turn one item's answers into label proposals with confidence and verdicts."""
    existing = set(item.get("labels") or [])
    proposals = []

    def add(question, label, confidence, gated, note=""):
        proposals.append({
            "question": question,
            "label": label,
            "confidence": None if confidence is None else round(float(confidence), 3),
            "gated": gated,
            "verdict": verdict(label, existing),
            "existing": sorted(name for name in existing if label_family(name) == label_family(label)),
            "note": note,
        })

    for question in CHOICE_QUESTIONS:
        answer = answers.get(question) or {}
        choice = answer.get("choice")
        confidence = answer.get("confidence")
        criteria = AREA_CRITERIA if question == "area" else KIND_CRITERIA
        threshold = thresholds[question]
        if choice in criteria and confidence is not None and confidence >= threshold:
            add(question, choice, confidence, False)
        else:
            note = "top %s" % choice if choice else "no answer"
            add(question, UNRESOLVED, confidence, True, note)

    answer = answers.get("priority") or {}
    score = answer.get("score")
    confidence = answer.get("confidence")
    if score is not None and confidence is not None and confidence >= thresholds["priority"]:
        add("priority", priority_label(score), confidence, False, "score %.2f" % float(score))
    else:
        note = "score %.2f" % float(score) if score is not None else "no answer"
        add("priority", UNRESOLVED, confidence, True, note)

    noul_threshold = thresholds["noul"]
    if item["type"] == "pr":
        answer = answers.get("pr_ready") or {}
        value = answer.get("noul")
        if value is None:
            add("pr_ready", UNRESOLVED, None, True, "no answer")
        elif value >= noul_threshold:
            add("pr_ready", NEEDS_REVIEW, value, False)
        elif value <= 1 - noul_threshold:
            add("pr_ready", NEEDS_AUTHOR, 1 - value, False, "not ready")
        else:
            add("pr_ready", UNRESOLVED, value, True, "uncertain")
    else:
        answer = answers.get("needs_repro") or {}
        value = answer.get("noul")
        top_kind = (answers.get("kind") or {}).get("choice")
        if top_kind not in (None, "kind/bug", "kind/security"):
            proposals.append({
                "question": "needs_repro", "label": None, "confidence": None if value is None else round(float(value), 3),
                "gated": False, "verdict": "SKIP", "existing": [], "note": "not a bug report (%s)" % top_kind,
            })
        elif value is None:
            add("needs_repro", UNRESOLVED, None, True, "no answer")
        elif value >= noul_threshold:
            add("needs_repro", NEEDS_REPRO, value, False)
        elif value <= 1 - noul_threshold:
            proposals.append({
                "question": "needs_repro", "label": None, "confidence": round(float(value), 3),
                "gated": False, "verdict": "AGREE" if NEEDS_REPRO not in existing else "DISAGREE",
                "existing": [NEEDS_REPRO] if NEEDS_REPRO in existing else [], "note": "has repro",
            })
        else:
            add("needs_repro", UNRESOLVED, value, True, "uncertain")
    return proposals


def summarize(results):
    questions = {}
    below_gate_items = 0
    labels_applied = 0
    for result in results:
        item_gated = False
        for proposal in result["proposals"]:
            entry = questions.setdefault(proposal["question"], {"agree": 0, "disagree": 0, "new": 0, "skip": 0, "below_gate": 0})
            if proposal["gated"]:
                entry["below_gate"] += 1
                item_gated = True
                continue
            if proposal["verdict"] == "AGREE":
                entry["agree"] += 1
            elif proposal["verdict"] == "DISAGREE":
                entry["disagree"] += 1
            elif proposal["verdict"] == "NEW":
                entry["new"] += 1
            else:
                entry["skip"] += 1
        if item_gated:
            below_gate_items += 1
        labels_applied += len(result.get("applied") or [])
    for entry in questions.values():
        compared = entry["agree"] + entry["disagree"]
        entry["agreement_rate"] = round(entry["agree"] / compared, 3) if compared else None
    return {"questions": questions, "items_below_gate": below_gate_items, "items": len(results), "labels_applied": labels_applied}


# ---------------------------------------------------------------------------
# Apply (--apply only): kind and needs_repro, additive only
# ---------------------------------------------------------------------------

def labels_to_apply(item, proposals):
    """Kind and needs_repro proposals eligible for --apply.

    Only two questions ever apply: ``kind`` (when ungated and the item has no
    existing ``kind/*`` label) and ``needs_repro`` (when it resolved yes and the
    issue has no existing ``triage/needs-repro`` label). area, priority and
    pr_ready are never applied, and no label is ever removed.
    """
    existing = set(item.get("labels") or [])
    to_apply = []
    for proposal in proposals:
        if proposal["question"] == "kind":
            if proposal["gated"] or not proposal["label"] or proposal["label"] == UNRESOLVED:
                continue
            if any(label_family(name) == "kind/" for name in existing):
                continue
            to_apply.append(proposal["label"])
        elif proposal["question"] == "needs_repro":
            if item["type"] != "issue":
                continue
            if proposal["gated"] or proposal["label"] != NEEDS_REPRO:
                continue
            if NEEDS_REPRO in existing:
                continue
            # needs_repro's whole premise is "this is a confirmed bug/security report". decide()
            # only checks the model's raw top kind choice for that (SKIP when it's a known other
            # kind), not whether that choice actually cleared its own confidence gate — so an
            # unresolved/gated kind (including no kind answer at all) must still withhold
            # needs_repro here, even though decide() itself proposed a label for it.
            kind_proposal = next((p for p in proposals if p["question"] == "kind"), None)
            if kind_proposal is None or kind_proposal["gated"] or kind_proposal["label"] not in ("kind/bug", "kind/security"):
                continue
            to_apply.append(proposal["label"])
    return to_apply


def gh_add_label(repo, item_type, number, label, run=subprocess.run):
    """Add one label via ``gh issue edit`` / ``gh pr edit --add-label``. Additive only."""
    subcommand = "pr" if item_type == "pr" else "issue"
    command = ["gh", subcommand, "edit", str(number), "-R", repo, "--add-label", label]
    result = run(command, capture_output=True, text=True)
    if result.returncode != 0:
        # gh often prints the actual cause on one line and a generic "failed to update N
        # issue(s)" summary after it; keep a tail long enough to carry both, not just the
        # last line, which would keep only the generic summary and drop the real cause.
        detail = (result.stderr or result.stdout or "").strip()
        raise TriageError("gh %s edit --add-label failed for %s#%d: %s" % (
            subcommand, repo, number, detail[-300:] if detail else "no output"))


def current_labels(repo, number, run=subprocess.run):
    """Read an item's labels right now, via the same read-only ``gh api`` fetch_items uses."""
    raw = gh_json("repos/%s/issues/%d" % (repo, number), run=run)
    return set(label["name"] for label in raw.get("labels") or [])


def apply_item_labels(repo, item, proposals, run=subprocess.run):
    """Apply the eligible kind/needs_repro labels for one item; returns what was added.

    ``labels_to_apply`` decides eligibility from the labels as they stood when
    the item was fetched; a live run can take a while, so immediately before
    writing we re-read the item's labels once and skip anything a human (or a
    concurrent run) added to that family in the meantime. This keeps the "no
    existing kind/* label" / "no existing triage/needs-repro" promise true at
    write time, not just at fetch time — and also withholds needs_repro if the
    live kind/* label is no longer bug/security, since a human reclassifying
    the item invalidates needs_repro's "this is a bug report" premise even
    though the label itself wasn't present a moment ago.
    """
    candidates = labels_to_apply(item, proposals)
    if not candidates:
        return []
    try:
        live = current_labels(repo, item["number"], run=run)
    except Exception as error:
        # Not just TriageError: the subprocess call itself can raise (e.g. the gh binary is
        # missing, or the OS refuses to spawn it) before there is any exit code to check.
        raise ApplyFailure(str(error), [])
    applied = []
    for label in candidates:
        if label_family(label) == "kind/":
            if any(label_family(name) == "kind/" for name in live):
                continue
        elif label == NEEDS_REPRO:
            if NEEDS_REPRO in live:
                continue
            # needs_repro's premise is "this is a bug report", decided from the fetch-time
            # kind answer. If a human has since classified it under a live kind/* other than
            # bug/security, that premise no longer holds even though the label wasn't yet
            # present a moment ago — reuse the same live read, no extra gh call needed. Checked
            # as "any conflicting kind/* present" (not "the" live kind) so this stays correct
            # and deterministic even in the unusual case of more than one live kind/* label.
            if any(label_family(name) == "kind/" and name not in ("kind/bug", "kind/security") for name in live):
                continue
        try:
            gh_add_label(repo, item["type"], item["number"], label, run=run)
        except Exception as error:
            # Not just TriageError, same reasoning as the current_labels() call above. Either
            # way: don't let a second label's failure erase the first one's success from the
            # caller's view — it already happened on GitHub, so the result must say so.
            raise ApplyFailure(str(error), applied)
        applied.append(label)
        live.add(label)
    return applied


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def evaluate(repo, items, transport, thresholds, model=DEFAULT_MODEL, log=None, apply=False, run=subprocess.run,
             environ=None):
    environ = os.environ if environ is None else environ
    results = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    for index, item in enumerate(items, 1):
        payload = build_request(repo, item, model)
        started = time.monotonic()
        try:
            response = transport(payload)
        except TriageError as error:
            raise PartialFailure(str(error), item_key(item), results, usage)
        elapsed = time.monotonic() - started
        # Everything from here through the apply step is wrapped in one handler: a malformed
        # nested shape anywhere in the response (not just a missing top-level "answers"), or any
        # other unexpected failure while deciding or applying, must not erase earlier items'
        # results — or, with --apply, the record of labels this item already wrote to GitHub
        # before hitting the problem. `result` is filled in incrementally so whatever got built
        # (including a partial `applied` list) survives into the PartialFailure.
        result = {
            "key": item_key(item),
            "item": {k: item[k] for k in ("type", "number", "title", "url", "labels", "author_association") if k in item},
            "response": response,
            "elapsed_seconds": round(elapsed, 3),
        }
        try:
            answers = response.get("answers") if isinstance(response, dict) else None
            if not isinstance(answers, dict):
                raise TriageError("TypeSafe response for %s has no 'answers' map" % item_key(item))
            for name, value in (response.get("usage") or {}).items():
                if name in usage and isinstance(value, (int, float)):
                    usage[name] += int(value)
            result["proposals"] = decide(item, answers, thresholds)
            result["applied"] = apply_item_labels(repo, item, result["proposals"], run=run) if apply else []
        except ApplyFailure as error:
            result["applied"] = error.applied
            results.append(result)
            raise PartialFailure(str(error), item_key(item), results, usage)
        except TriageError as error:
            raise PartialFailure(str(error), item_key(item), results, usage)
        except Exception as error:  # never let an unexpected shape carry a traceback out of here
            # redact_env: an untrusted/malformed response value could, in principle, echo the
            # API key back verbatim into an exception's own message (e.g. a ValueError from
            # float() embedding the exact string that failed to parse); nothing upstream can
            # pre-redact an error this generic catch-all wasn't expecting, so redact here.
            raise PartialFailure(
                "unexpected %s processing %s: %s" % (
                    type(error).__name__, item_key(item), redact_env(str(error), environ)),
                item_key(item), results, usage,
            )
        results.append(result)
        if log:
            log("[%d/%d] %s (%.1fs)" % (index, len(items), item_key(item), elapsed))
    return results, usage


def truncate(text, width):
    text = " ".join((text or "").split())
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def render_table(results):
    columns = ("#", "Title", "Question", "Proposed", "Conf", "Existing", "Verdict", "Applied")
    rows = []
    for result in results:
        item = result["item"]
        applied = set(result.get("applied") or [])
        first = True
        for proposal in result["proposals"]:
            label = proposal["label"] or "-"
            if proposal["gated"] and proposal["note"]:
                label = "%s (%s)" % (label, proposal["note"])
            elif proposal["note"] and proposal["label"] not in (None, UNRESOLVED):
                label = "%s (%s)" % (label, proposal["note"])
            elif proposal["note"] and proposal["label"] is None:
                label = "- (%s)" % proposal["note"]
            confidence = "-" if proposal["confidence"] is None else "%.2f" % proposal["confidence"]
            was_applied = proposal["question"] in ("kind", "needs_repro") and proposal["label"] in applied
            rows.append((
                ("%s%d" % ("PR " if item["type"] == "pr" else "#", item["number"])) if first else "",
                truncate(item["title"], 44) if first else "",
                proposal["question"],
                truncate(label, 46),
                confidence,
                truncate(", ".join(proposal["existing"]) or "-", 34),
                proposal["verdict"],
                "applied" if was_applied else "-",
            ))
            first = False
    widths = [len(name) for name in columns]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    fmt = "  ".join("{:<%d}" % width for width in widths)
    lines = [fmt.format(*columns).rstrip(), fmt.format(*["-" * width for width in widths]).rstrip()]
    lines.extend(fmt.format(*row).rstrip() for row in rows)
    return "\n".join(lines)


def render_summary(summary, usage, wall_seconds, thresholds, output_path):
    lines = ["", "Summary"]
    lines.append("  items: %d, items with at least one question below the gate: %d" % (summary["items"], summary["items_below_gate"]))
    lines.append("  gate: area >= %.2f, kind >= %.2f, priority >= %.2f, yes/no >= %.2f" % (
        thresholds["area"], thresholds["kind"], thresholds["priority"], thresholds["noul"]))
    for question, entry in summary["questions"].items():
        rate = "n/a" if entry["agreement_rate"] is None else "%.0f%%" % (entry["agreement_rate"] * 100)
        lines.append("  %-11s agreement %s (agree %d, disagree %d, new %d, skip %d, below gate %d)" % (
            question, rate, entry["agree"], entry["disagree"], entry["new"], entry["skip"], entry["below_gate"]))
    lines.append("  labels applied: %d" % summary["labels_applied"])
    lines.append("  tokens: input %d, output %d" % (usage["input_tokens"], usage["output_tokens"]))
    lines.append("  wall time: %.1fs" % wall_seconds)
    if output_path:
        lines.append("  raw answers: %s" % output_path)
    return "\n".join(lines)


def write_output(directory, payload):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    base = "triage-%s" % stamp()
    for suffix in [""] + ["-%d" % n for n in range(2, 1000)]:
        path = directory / (base + suffix + ".json")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as output:
            output.write(json.dumps(payload, indent=2) + "\n")
        return path
    raise TriageError("could not find a free output name under %s" % directory)


def write_record(path, repo, items, results):
    record = {
        "schema_version": 1,
        "repo": repo,
        "recorded_at": utc_now().isoformat(),
        "items": items,
        "responses": {result["key"]: result["response"] for result in results},
    }
    Path(path).write_text(json.dumps(record, indent=2) + "\n")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="typesafe-triage.py",
        description=(
            "TypeSafe label triage for NanoClaw issues and PRs. Dry run by default (writes nothing to "
            "GitHub); pass --apply to add the two labels that measured 100%% agreement on a live run."
        ),
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name (default %(default)s)")
    parser.add_argument("--issues", type=int, default=30, help="open issues to fetch, most recently updated first (default %(default)s)")
    parser.add_argument("--prs", type=int, default=20, help="open pull requests to fetch (default %(default)s)")
    parser.add_argument("--fixture", help="replay recorded items and responses from this JSON file instead of gh and the API")
    parser.add_argument("--record", help="write the fetched items and raw responses to this JSON file for later --fixture replay")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="where raw answers are saved (default: skill-local output/, gitignored)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout per request in seconds")
    parser.add_argument("--area-threshold", type=float, default=0.6)
    parser.add_argument("--kind-threshold", type=float, default=0.6)
    parser.add_argument("--priority-threshold", type=float, default=0.6)
    parser.add_argument("--noul-threshold", type=float, default=0.7, help="yes/no probability needed to propose a triage label")
    parser.add_argument("--json", action="store_true", help="print the full result JSON to stdout instead of the table")
    parser.add_argument("--apply", action="store_true", help=(
        "add the ungated kind/* label and, on issues, an ungated triage/needs-repro label via gh. "
        "Never removes a label, never touches area/priority/pr_ready, never comments. Default: dry run."
    ))
    parser.add_argument("--since", help="only consider items created strictly after this ISO-8601 timestamp (e.g. 2026-09-01T00:00:00Z)")
    parser.add_argument("--only-unlabeled", action="store_true", help="skip items that already carry an existing kind/* label")
    args = parser.parse_args(argv)
    for name in ("area_threshold", "kind_threshold", "priority_threshold"):
        value = getattr(args, name)
        if not 0 <= value <= 1:
            parser.error("--%s must be between 0 and 1" % name.replace("_", "-"))
    if not 0.5 <= args.noul_threshold <= 1:
        parser.error("--noul-threshold must be between 0.5 and 1")
    if args.since:
        try:
            parse_iso(args.since)
        except ValueError:
            parser.error("--since must be an ISO-8601 timestamp, e.g. 2026-09-01T00:00:00Z")
    if args.fixture and args.apply:
        parser.error(
            "--apply cannot be combined with --fixture: a fixture's hand-written answers and "
            "labels are not the item's real current state, and --apply would write them for real"
        )
    return args


def main(argv=None, environ=None, stdout=None, stderr=None, run=None):
    environ = os.environ if environ is None else environ
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    run = subprocess.run if run is None else run
    args = parse_args(argv)
    thresholds = {
        "area": args.area_threshold,
        "kind": args.kind_threshold,
        "priority": args.priority_threshold,
        "noul": args.noul_threshold,
    }
    started = time.monotonic()
    try:
        if args.fixture:
            fixture = load_fixture(args.fixture)
            repo = fixture.get("repo") or args.repo
            items = fixture["items"]
            transport = FixtureTransport(fixture["responses"])
            mode = "fixture"
        else:
            api_key = environ.get(KEY_ENV)
            if not api_key:
                print(
                    "%s is not set. Export it in this shell (for example `export %s=...` from your "
                    "password manager) and rerun. The key is read from the environment only; never put it "
                    "in a file inside the repository. Use --fixture <file> to run offline." % (KEY_ENV, KEY_ENV),
                    file=stderr,
                )
                return 2
            repo = args.repo
            transport = HttpTransport(api_key, timeout=args.timeout)
            mode = "live"
            items = fetch_items(repo, args.issues, args.prs)
        if args.since:
            items = filter_since(items, args.since)
        if args.only_unlabeled:
            items = filter_unlabeled(items)
        if not items:
            # A distinct exit status (not 1, used for real errors and partial failures): an
            # automated caller (the scheduled workflow) can tell "nothing matched" apart from
            # a failure by checking the exit code alone, with no text matching against stderr
            # — which an adversarial or merely broken upstream error body could otherwise spoof
            # by coincidentally containing this same line.
            print("no open items to triage", file=stderr)
            return 3
        error_text = None
        try:
            results, usage = evaluate(repo, items, transport, thresholds, args.model,
                                      log=lambda line: print(line, file=stderr) if mode == "live" else None,
                                      apply=args.apply, run=run, environ=environ)
        except PartialFailure as partial:
            results, usage = partial.results, partial.usage
            error_text = "%s failed: %s" % (partial.failed_key, partial)
            print("error: %s; keeping %d completed item(s)" % (error_text, len(results)), file=stderr)
            if not results:
                return 1
    except TriageError as error:
        print("error: %s" % error, file=stderr)
        return 1
    except Exception as error:  # never let a traceback carry request details to the terminal
        print("error: unexpected %s: %s" % (type(error).__name__, redact_env(str(error), environ)), file=stderr)
        return 1
    wall = time.monotonic() - started
    summary = summarize(results)
    payload = {
        "schema_version": 1,
        "generated_at": utc_now().isoformat(),
        "mode": mode,
        "repo": repo,
        "model": args.model,
        "thresholds": thresholds,
        "usage": usage,
        "wall_seconds": round(wall, 3),
        "partial": error_text,
        "summary": summary,
        "results": results,
    }
    # Evaluation (and any --apply writes to GitHub) already finished at this point, so a bad
    # --output-dir/--record destination must degrade to a warning, never hide the console report
    # of what actually happened (including real, already-written labels) behind a save failure.
    try:
        output_path = write_output(args.output_dir, payload)
    except (OSError, TriageError) as error:
        output_path = None
        print("warning: could not save raw answers under %s: %s" % (args.output_dir, error), file=stderr)
    record_failed = False
    if args.record and mode == "live":
        try:
            write_record(args.record, repo, items, results)
        except OSError as error:
            # Unlike --output-dir (a default-valued, best-effort location), --record was
            # explicitly requested with no default; a caller relying on it for later --fixture
            # replay must be able to tell it didn't happen from the exit code, not only a log
            # line, so this affects the return value even though evaluation itself succeeded.
            record_failed = True
            print("warning: could not write --record file %s: %s" % (args.record, error), file=stderr)
    if args.json:
        print(json.dumps(payload, indent=2), file=stdout)
    else:
        if args.apply:
            print("Apply run (%s mode) for %s: %d items. %d label(s) applied." % (
                mode, repo, len(items), summary["labels_applied"]), file=stdout)
        else:
            print("Dry run (%s mode) for %s: %d items. No labels were written." % (mode, repo, len(items)), file=stdout)
        print("", file=stdout)
        print(render_table(results), file=stdout)
        print(render_summary(summary, usage, wall, thresholds, output_path), file=stdout)
    return 1 if (error_text or record_failed) else 0


def redact_env(text, environ):
    key = environ.get(KEY_ENV) or ""
    return text.replace(key, "[redacted]") if len(key) >= 8 else text


if __name__ == "__main__":
    sys.exit(main())
