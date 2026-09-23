#!/usr/bin/env python3
"""Docs-drift detector for NanoClaw (phase 1): rank where the docs portal disagrees with the code.

Deterministic code extracts facts from a NanoClaw checkout (``ncl`` resources,
verbs and flags; environment variables; ``container_configs`` columns and
``cli_scope`` values; workspace skills; gateway selection rules; timestamp
rules). Each fact is matched lexically against the headings and paragraphs of
the nanoclaw-docs Mintlify site, and every fact x section pair is sent as one
speculative fan-out request to TypeSafe System One with three questions:
``contradicts`` (noul), ``covers`` (noul) and ``staleness`` (score). Code then
gates on the answers and prints a ranked DRIFT / MISSING / UNSURE / OK report.

Nothing is written to either repository. The model never writes text, so this
script detects and ranks drift; a later phase writes the fix from the JSON.

``--fixture <file>`` replays recorded facts, sections and API responses so the
whole pipeline runs offline. ``TYPESAFE_API_KEY`` is read from the environment
only when the live API is used; it is never read from a file and never printed.
"""

import argparse
import concurrent.futures
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
KEY_ENV = "TYPESAFE_API_KEY"
SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = SKILL_DIR / "output"
SECTION_LIMIT = 6000
EXCERPT_LIMIT = 400
DEFAULT_TOP_K = 3
DEFAULT_CONCURRENCY = 4
MAX_CONSECUTIVE_FAILURES = 3
EXCLUDED_PAGE_PREFIXES = ("changelog/",)
AREAS = ("ncl", "env", "container-config", "skills", "gateway", "timestamps")
VERDICTS = ("DRIFT", "MISSING", "UNSURE", "OK")
STALENESS_LEVELS = ("current", "slightly outdated", "wrong", "dangerous if followed")
STALENESS_CRITERIA = [
    "Current: the section states the fact correctly, or does not touch the point the fact makes at all.",
    "Slightly outdated: a name, default value, flag spelling or minor detail differs from the fact, "
    "but a reader following the section would still succeed.",
    "Wrong: the section states something the code no longer does or never did on this point; "
    "a reader following it would be misled, get an error, or miss a required step.",
    "Dangerous if followed: following the section on this point could lose data, expose a credential, "
    "weaken a security boundary, or leave the install broken.",
]
# Process-environment names that are not NanoClaw settings.
ENV_IGNORE = {"HOME", "PATH", "USER", "TMPDIR", "DISPLAY", "WAYLAND_DISPLAY", "COLORTERM", "NO_COLOR", "TZ"}
ENV_PREFIXES = ("NANOCLAW_", "CONTAINER_", "ONECLI_", "LOG_LEVEL", "WEBHOOK_PORT", "ASSISTANT_", "DEFAULT_AGENT_")


class DriftError(RuntimeError):
    """A failure the operator can act on; printed without a traceback."""


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def stamp():
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise DriftError("could not read %s: %s" % (path, error))


def excerpt(text, limit=EXCERPT_LIMIT):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def line_of(text, offset):
    return text.count("\n", 0, offset) + 1


def git_commit(root):
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# ---------------------------------------------------------------------------
# Fact extraction (deterministic, no model)
# ---------------------------------------------------------------------------

def fact(area, key, statement, path, line, snippet, terms=()):
    return {
        "id": "%s:%s" % (area, key),
        "area": area,
        "statement": statement,
        "evidence": {"file": str(path), "line": line, "excerpt": excerpt(snippet)},
        "terms": sorted({term for term in terms if term}),
    }


def relative(root, path):
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(path)


_VERB_LINE = re.compile(r"^    (?:'([^']+)'|([A-Za-z_][\w-]*)): \{\s*$")
_ACCESS_LINE = re.compile(r"^\s+access: '(open|approval)'")
_FLAG = re.compile(r"--[a-z][a-z0-9-]*")
_ARG_NAME = re.compile(r"name: '(\w+)'")


def parse_resource(text):
    """Pull plural, standard operations, custom verbs and column enums out of one resource module."""
    start = text.find("registerResource({")
    if start < 0:
        return None
    body = text[start:]
    plural = re.search(r"^\s*plural: '([^']+)'", body, re.M)
    if not plural:
        return None
    resource = {"plural": plural.group(1), "line": line_of(text, start), "operations": {}, "custom": [], "enums": []}
    operations = re.search(r"^\s*operations: \{([^}]*)\}", body, re.M)
    if operations:
        for verb, access in re.findall(r"(\w+): '(open|approval)'", operations.group(1)):
            resource["operations"][verb] = access
    # Top-level keys of the definition sit at two spaces; a nested array's `],` is deeper.
    columns = re.search(r"^  columns: \[(.*?)^  \],", body, re.M | re.S)
    if columns:
        for name, values in re.findall(r"name: '(\w+)'[^{}]*?enum: \[([^\]]*)\]", columns.group(1), re.S):
            resource["enums"].append((name, re.findall(r"'([^']*)'", values)))
    custom = re.search(r"^\s*customOperations: \{\s*$", body, re.M)
    if custom:
        lines = body[custom.end():].splitlines()
        offset = line_of(text, start + custom.end())
        current = None
        for index, line in enumerate(lines):
            if line == "  },":
                break
            verb_match = _VERB_LINE.match(line)
            if verb_match:
                current = {"verb": verb_match.group(1) or verb_match.group(2), "access": None, "flags": [],
                           "declared": None, "host_only": False, "line": offset + index + 1}
                resource["custom"].append(current)
                continue
            if current is None:
                continue
            access_match = _ACCESS_LINE.match(line)
            if access_match and current["access"] is None:
                current["access"] = access_match.group(1)
            if re.match(r"^\s+hostOnly: true", line):
                current["host_only"] = True
            if re.match(r"^      args: \[", line):
                current["declared"] = []
                current["in_args"] = True
                continue
            if current.get("in_args"):
                if line.startswith("      ],"):
                    current["in_args"] = False
                else:
                    for name in _ARG_NAME.findall(line):
                        flag = "--" + name.replace("_", "-")
                        if flag not in current["declared"]:
                            current["declared"].append(flag)
                continue
            if re.match(r"^\s+handler:", line):
                current["done"] = True
            if not current.get("done"):
                for flag in _FLAG.findall(line):
                    if flag not in current["flags"]:
                        current["flags"].append(flag)
    return resource


def extract_ncl(root):
    facts = []
    directory = Path(root) / "src" / "cli" / "resources"
    for path in sorted(directory.glob("*.ts")) if directory.is_dir() else []:
        if path.name.endswith(".test.ts") or path.name == "index.ts":
            continue
        text = read_text(path)
        resource = parse_resource(text)
        if not resource:
            continue
        plural = resource["plural"]
        rel = relative(root, path)
        verbs = ["%s (%s)" % (verb, access) for verb, access in resource["operations"].items()]
        verbs += ["%s (%s%s)" % (entry["verb"], entry["access"] or "unknown access", ", host only" if entry["host_only"] else "")
                  for entry in resource["custom"]]
        if verbs:
            facts.append(fact(
                "ncl", "%s-verbs" % plural,
                "`ncl %s` exposes exactly these verbs, with their access level for agent callers: %s. "
                "`open` runs inline; `approval` sends an approval card when called from inside a container."
                % (plural, ", ".join(verbs)),
                rel, resource["line"],
                "registerResource plural '%s' operations %s custom %s" % (
                    plural, resource["operations"], [entry["verb"] for entry in resource["custom"]]),
                terms=["ncl", plural] + [entry["verb"].split()[0] for entry in resource["custom"]] + list(resource["operations"]),
            ))
        for entry in resource["custom"]:
            if entry["declared"] is not None:
                flags = entry["declared"]
                wording = "declares exactly these flags (any other flag is rejected)"
                snippet = "%s: { access: '%s', args: [%s] }" % (entry["verb"], entry["access"], ", ".join(flags))
            else:
                flags = entry["flags"]
                wording = "names these flags in its help text"
                snippet = "%s: { access: '%s', description: ... %s }" % (entry["verb"], entry["access"], " ".join(flags))
            if not flags:
                continue
            facts.append(fact(
                "ncl", "%s-%s-flags" % (plural, entry["verb"].replace(" ", "-")),
                "`ncl %s %s` (%s) %s: %s." % (
                    plural, entry["verb"], entry["access"] or "unknown access", wording, ", ".join("`%s`" % flag for flag in flags)),
                rel, entry["line"], snippet,
                terms=["ncl", plural, entry["verb"].split()[0]] + [flag.lstrip("-") for flag in flags],
            ))
        for column, values in resource["enums"]:
            facts.append(fact(
                "ncl", "%s-%s-enum" % (plural, column),
                "`ncl %s` column `%s` accepts exactly these values: %s." % (
                    plural, column, ", ".join("`%s`" % value for value in values)),
                rel, line_of(text, text.find("name: '%s'" % column)), "name: '%s' enum: %s" % (column, values),
                terms=["ncl", plural, column] + values,
            ))
    return facts


def extract_env(root):
    facts = []
    config_path = Path(root) / "src" / "config.ts"
    dotenv_keys = []
    if config_path.exists():
        text = read_text(config_path)
        rel = relative(root, config_path)
        block = re.search(r"readEnvFile\(\[(.*?)\]", text, re.S)
        if block:
            dotenv_keys = re.findall(r"'([A-Z][A-Z0-9_]+)'", block.group(1))
        for key in dotenv_keys:
            match = re.search(r"process\.env\.%s\s*(\|\||\?\?)\s*envConfig\.%s\s*(?:(\|\||\?\?)\s*('([^']*)'|\"([^\"]*)\"))?" % (key, key), text)
            if not match:
                match = re.search(r"process\.env\.%s\b" % key, text)
                if not match:
                    continue
                facts.append(fact("env", key, "`%s` is read from the process environment first, then from `.env`." % key,
                                  rel, line_of(text, match.start()), text[match.start(): match.start() + 160], terms=[key]))
                continue
            default = match.group(4) if match.group(4) is not None else match.group(5)
            line = line_of(text, match.start())
            snippet = text[text.rfind("\n", 0, match.start()) + 1: text.find("\n", match.end())]
            tail = text[match.end(): match.end() + 40]
            if "=== 'true'" in tail or "=== \"true\"" in tail:
                statement = "`%s` is read from the process environment first, then from `.env`, and is enabled only when set to the string `true`." % key
            elif default is None:
                statement = "`%s` is read from the process environment first, then from `.env`; it has no default." % key
            elif default == "":
                statement = "`%s` is read from the process environment first, then from `.env`; it is unset (empty) by default." % key
            else:
                statement = "`%s` is read from the process environment first, then from `.env`; its default is `%s`." % (key, default)
            if match.group(1) == "??":
                statement += " It is resolved with `??`, so an explicitly empty value wins over the default."
            facts.append(fact("env", key, statement, rel, line, snippet, terms=[key]))
    for path, constant, key, what in (
        ("src/gateway-providers/index.ts", "DEFAULT_GATEWAY_PROVIDER_KIND", "NANOCLAW_GATEWAY_PROVIDER", "gateway provider"),
        ("src/drivers/index.ts", "DEFAULT_DRIVER_KIND", "NANOCLAW_RUNTIME_DRIVER", "session runtime driver"),
    ):
        full = Path(root) / path
        if not full.exists():
            continue
        text = read_text(full)
        match = re.search(r"%s = '([^']+)'" % constant, text)
        if match:
            facts.append(fact(
                "env", key,
                "`%s` selects the %s; it is read from the process environment first, then from `.env`, and defaults to `%s`. "
                "A configured name with no registered implementation is an error at startup, not a fallback." % (key, what, match.group(1)),
                relative(root, full), line_of(text, match.start()), text[match.start(): match.end()], terms=[key, match.group(1)]))
    seen = set(dotenv_keys) | {"NANOCLAW_GATEWAY_PROVIDER", "NANOCLAW_RUNTIME_DRIVER"}
    src = Path(root) / "src"
    for path in sorted(src.rglob("*.ts")) if src.is_dir() else []:
        if path.name.endswith(".test.ts") or "/test" in str(path):
            continue
        text = read_text(path)
        for match in re.finditer(r"process\.env\.([A-Z][A-Z0-9_]+)\s*(?:as \w+\))?\s*(\|\||\?\?)\s*'([^']*)'", text):
            key, default = match.group(1), match.group(3)
            if key in seen or key in ENV_IGNORE or not key.startswith(ENV_PREFIXES):
                continue
            seen.add(key)
            facts.append(fact(
                "env", key,
                "`%s` is read from the process environment only (no `.env` fallback) and defaults to `%s`." % (key, default),
                relative(root, path), line_of(text, match.start()), text[match.start(): match.end()], terms=[key]))
    example = Path(root) / ".env.example"
    if example.exists():
        text = read_text(example)
        for match in re.finditer(r"(?m)^\s*#?\s*([A-Z][A-Z0-9_]+)=(.*)$", text):
            key = match.group(1)
            facts.append(fact(
                "env", "example-%s" % key,
                "`.env.example` lists `%s` (example value: `%s`)." % (key, match.group(2).strip() or "empty"),
                relative(root, example), line_of(text, match.start()), match.group(0), terms=[key]))
    return facts


def extract_container_config(root):
    facts = []
    migrations = Path(root) / "src" / "db" / "migrations"
    columns = []
    for path in sorted(migrations.glob("*.ts")) if migrations.is_dir() else []:
        text = read_text(path)
        rel = relative(root, path)
        create = re.search(r"CREATE TABLE container_configs \((.*?)\);", text, re.S)
        if create:
            for line in create.group(1).splitlines():
                stripped = line.strip().rstrip(",")
                match = re.match(r"([a-z_]+)\s+([A-Z]+)(.*)$", stripped)
                if not match:
                    continue
                columns.append((match.group(1), match.group(3).strip(), rel, line_of(text, create.start())))
        for match in re.finditer(r"ALTER TABLE container_configs ADD COLUMN ([a-z_]+) ([A-Z]+)([^;`\"]*)", text):
            columns.append((match.group(1), match.group(3).strip(), rel, line_of(text, match.start())))
    if columns:
        first = columns[0]
        facts.append(fact(
            "container-config", "columns",
            "The `container_configs` table has exactly these columns: %s." % ", ".join("`%s`" % name for name, _, _, _ in columns),
            first[2], first[3], "CREATE TABLE container_configs + ALTER TABLE ADD COLUMN across migrations",
            terms=["container_configs", "container config"] + [name for name, _, _, _ in columns]))
        for name, rest, rel, line in columns:
            default = re.search(r"DEFAULT (\S+)", rest)
            if default:
                facts.append(fact(
                    "container-config", "%s-default" % name,
                    "`container_configs.%s` defaults to %s." % (name, default.group(1)),
                    rel, line, "%s %s" % (name, rest), terms=["container_configs", name, "default"]))
    groups = Path(root) / "src" / "cli" / "resources" / "groups.ts"
    if groups.exists():
        text = read_text(groups)
        match = re.search(r"\[((?:'[\w-]+'(?:,\s*)?)+)\]\.includes\(scope\)", text)
        if match:
            values = re.findall(r"'([\w-]+)'", match.group(1))
            facts.append(fact(
                "container-config", "cli-scope-values",
                "`cli_scope` accepts exactly these values via `ncl groups config update --cli-scope`: %s." % ", ".join("`%s`" % value for value in values),
                relative(root, groups), line_of(text, match.start()), match.group(0), terms=["cli_scope", "cli-scope"] + values))
    return facts


def parse_frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    fields = {}
    for line in text[3:end].splitlines():
        match = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if match:
            value = match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            fields[match.group(1)] = value
    return fields


def extract_skills(root):
    facts = []
    directory = Path(root) / ".claude" / "skills"
    names = []
    for skill_dir in sorted(directory.iterdir()) if directory.is_dir() else []:
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        text = read_text(skill_file)
        front = parse_frontmatter(text)
        name = front.get("name") or skill_dir.name
        description = front.get("description") or ""
        names.append(name)
        facts.append(fact(
            "skills", name,
            "A workspace skill `/%s` exists under `.claude/skills/`; it is invoked when: %s" % (name, description or "(no description)"),
            relative(root, skill_file), 2, "name: %s / description: %s" % (name, description), terms=[name, "/" + name]))
    if names:
        facts.insert(0, fact(
            "skills", "catalog",
            "There are exactly %d workspace skills under `.claude/skills/` with a SKILL.md: %s." % (len(names), ", ".join("`%s`" % name for name in names)),
            relative(root, directory), 1, "directory listing", terms=["skills", "catalog", "workspace skills"]))
    return facts


def extract_gateway(root):
    facts = []
    doc = Path(root) / "docs" / "gateway-seam.md"
    if doc.exists():
        text = read_text(doc)
        rel = relative(root, doc)
        for heading in ("Selection", "How a gateway gets installed"):
            match = re.search(r"(?m)^## %s\s*$" % re.escape(heading), text)
            if not match:
                continue
            end = re.search(r"(?m)^## ", text[match.end():])
            body = text[match.end(): match.end() + end.start() if end else len(text)]
            for index, paragraph in enumerate(re.split(r"\n\s*\n", body)):
                cleaned = " ".join(paragraph.split())
                if not cleaned or cleaned.startswith("```") or cleaned.startswith("{"):
                    continue
                facts.append(fact(
                    "gateway", "%s-%d" % (heading.lower().replace(" ", "-"), index + 1), cleaned, rel,
                    line_of(text, match.end() + body.find(paragraph) + len(paragraph) - len(paragraph.lstrip())), paragraph,
                    terms=["gateway", "credential gateway", "NANOCLAW_GATEWAY_PROVIDER"] + re.findall(r"`([^`]+)`", paragraph)))
        return facts
    index = Path(root) / "src" / "gateway-providers" / "index.ts"
    if not index.exists():
        return facts
    text = read_text(index)
    rel = relative(root, index)
    default = re.search(r"DEFAULT_GATEWAY_PROVIDER_KIND = '([^']+)'", text)
    if default:
        facts.append(fact(
            "gateway", "selection-default",
            "One credential gateway provider is active per install, selected by `NANOCLAW_GATEWAY_PROVIDER`; when the "
            "variable is unset the `%s` gateway is used, so an install that never sets it behaves as before." % default.group(1),
            rel, line_of(text, default.start()), text[default.start(): default.end()],
            terms=["gateway", "credential gateway", "NANOCLAW_GATEWAY_PROVIDER", default.group(1)]))
    unknown = re.search(r"no gateway provider is registered", text)
    if unknown:
        facts.append(fact(
            "gateway", "selection-unknown",
            "Setting `NANOCLAW_GATEWAY_PROVIDER` to a name with no registered gateway provider throws at startup and lists "
            "the installed kinds; other gateways arrive as skills (overlays) and the host never silently falls back to another gateway.",
            rel, line_of(text, unknown.start()), text[text.rfind("\n", 0, unknown.start()) + 1: text.find("\n", unknown.end())],
            terms=["gateway", "NANOCLAW_GATEWAY_PROVIDER", "overlay", "skill"]))
    precedence = re.search(r"process\.env.*precedence", text)
    if precedence:
        facts.append(fact(
            "gateway", "selection-precedence",
            "`NANOCLAW_GATEWAY_PROVIDER` is read once at first use; the process environment takes precedence over `.env`, "
            "and the value is lowercased.",
            rel, line_of(text, precedence.start()), text[precedence.start(): precedence.end()],
            terms=["gateway", "NANOCLAW_GATEWAY_PROVIDER", ".env"]))
    return facts


def extract_timestamps(root):
    facts = []
    claude = Path(root) / "CLAUDE.md"
    if not claude.exists():
        return facts
    text = read_text(claude)
    match = re.search(r"(?m)^## Timestamps\s*$", text)
    if not match:
        return facts
    end = re.search(r"(?m)^## ", text[match.end():])
    body = text[match.end(): match.end() + end.start() if end else len(text)]
    index = 0
    for paragraph in re.split(r"\n\s*\n", body):
        for piece in re.split(r"(?m)^- ", paragraph):
            cleaned = " ".join(piece.split())
            if len(cleaned) < 40:
                continue
            index += 1
            facts.append(fact(
                "timestamps", "rule-%d" % index, cleaned, relative(root, claude),
                line_of(text, match.end() + body.find(piece) + len(piece) - len(piece.lstrip())), piece,
                terms=["timestamp", "timezone", "ISO", "UTC"] + re.findall(r"`([^`]+)`", piece)))
    return facts


EXTRACTORS = {
    "ncl": extract_ncl,
    "env": extract_env,
    "container-config": extract_container_config,
    "skills": extract_skills,
    "gateway": extract_gateway,
    "timestamps": extract_timestamps,
}


def extract_facts(root, areas=AREAS):
    root = Path(root)
    if not root.is_dir():
        raise DriftError("code checkout not found: %s" % root)
    facts = []
    for area in AREAS:
        if area in areas:
            facts.extend(EXTRACTORS[area](root))
    seen = set()
    for entry in facts:
        if entry["id"] in seen:
            raise DriftError("duplicate fact id %s" % entry["id"])
        seen.add(entry["id"])
    return facts


# ---------------------------------------------------------------------------
# Docs sections and lexical search
# ---------------------------------------------------------------------------

def navigation_pages(config):
    pages = []

    def walk(node):
        if isinstance(node, str):
            pages.append(node)
        elif isinstance(node, dict):
            for key in ("pages", "groups", "tabs", "navigation", "anchors", "dropdowns", "versions", "languages"):
                if key in node:
                    walk(node[key])
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(config.get("navigation"))
    ordered = []
    for page in pages:
        if page not in ordered:
            ordered.append(page)
    return ordered


def blank(match):
    """Replace a match with the newlines it contained so line numbers stay those of the source file."""
    return "\n" * match.group(0).count("\n")


def clean_mdx(text):
    text = re.sub(r"\{/\*.*?\*/\}", blank, text, flags=re.S)
    text = re.sub(r"(?m)^import .*$", "", text)
    text = re.sub(r"(?m)^[ \t]*</?[A-Z][A-Za-z]*(\s[^>]*)?/?>[ \t]*$", "", text)
    return text


def slugify(heading):
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    return slug or "section"


def split_sections(page, text):
    front = parse_frontmatter(text)
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            body = "\n" * text.count("\n", 0, end + 4) + text[end + 4:]
    body = clean_mdx(body)
    title = front.get("title") or page
    sections = []
    used = {}
    current = {"heading": title, "level": 1, "line": 1, "lines": []}
    in_code = False
    for number, line in enumerate(body.splitlines(), 1):
        if line.strip().startswith("```"):
            in_code = not in_code
        match = None if in_code else re.match(r"^(#{2,4})\s+(.+?)\s*#*\s*$", line)
        if match:
            sections.append(current)
            current = {"heading": match.group(2).strip(), "level": len(match.group(1)), "line": number, "lines": []}
        else:
            current["lines"].append(line)
    sections.append(current)
    results = []
    for entry in sections:
        content = "\n".join(entry["lines"]).strip()
        if not content and entry["level"] == 1:
            continue
        anchor = slugify(entry["heading"])
        count = used.get(anchor, 0)
        used[anchor] = count + 1
        if count:
            anchor = "%s-%d" % (anchor, count + 1)
        results.append({
            "id": "%s#%s" % (page, anchor),
            "page": page,
            "title": title,
            "heading": entry["heading"],
            "level": entry["level"],
            "line": entry["line"],
            "text": content,
            "keywords": front.get("keywords", ""),
        })
    return results


def load_sections(docs_root, include_changelog=False):
    docs_root = Path(docs_root)
    config_path = docs_root / "docs.json"
    if not config_path.is_file():
        raise DriftError("docs.json not found under %s" % docs_root)
    try:
        config = json.loads(read_text(config_path))
    except json.JSONDecodeError as error:
        raise DriftError("docs.json is not valid JSON: %s" % error)
    sections = []
    for page in navigation_pages(config):
        if not include_changelog and page.startswith(EXCLUDED_PAGE_PREFIXES):
            continue
        path = docs_root / (page + ".mdx")
        if not path.is_file():
            path = docs_root / (page + ".md")
            if not path.is_file():
                continue
        sections.extend(split_sections(page, read_text(path)))
    if not sections:
        raise DriftError("no documentation sections found under %s" % docs_root)
    return sections


_TOKEN = re.compile(r"[a-z0-9][a-z0-9_./-]*[a-z0-9]|[a-z0-9]")


def tokenize(text):
    tokens = []
    for token in _TOKEN.findall((text or "").lower()):
        tokens.append(token)
        parts = [part for part in re.split(r"[_./-]", token) if len(part) > 1]
        if len(parts) > 1:
            tokens.extend(parts)
    return tokens


STOPWORDS = set("the a an and or of to in on for is are be by with from that this it its as at when only then than into not no exactly these their via one per".split())


class SectionIndex:
    """Tiny in-memory lexical index: BM25 over section text with heading and identifier boosts."""

    K1 = 1.2
    B = 0.75

    def __init__(self, sections):
        self.sections = sections
        self.doc_frequency = {}
        self.body_counts = []
        self.lowered = []
        self.lengths = []
        self.heading_sets = []
        for section in sections:
            body = tokenize(section["text"] + " " + section.get("keywords", ""))
            counts = {}
            for token in body:
                counts[token] = counts.get(token, 0) + 1
            heading = set(tokenize(section["heading"] + " " + section["title"]))
            self.body_counts.append(counts)
            self.lowered.append((section["heading"] + "\n" + section["text"]).lower())
            self.lengths.append(len(body))
            self.heading_sets.append(heading)
            for token in set(counts) | heading:
                self.doc_frequency[token] = self.doc_frequency.get(token, 0) + 1
        self.total = max(1, len(sections))
        self.average_length = max(1.0, sum(self.lengths) / self.total)

    def idf(self, token):
        frequency = self.doc_frequency.get(token, 0)
        return math.log(1 + (self.total - frequency + 0.5) / (frequency + 0.5))

    def query_terms(self, entry):
        """Token weights plus verbatim identifiers (backticked in the statement or listed as terms)."""
        weights = {}
        phrases = set()
        for term in entry.get("terms") or []:
            for token in tokenize(term):
                weights[token] = max(weights.get(token, 0), 3.0)
            if len(term) >= 4 and not term.isalpha():
                phrases.add(term.lower())
        for identifier in re.findall(r"`([^`]+)`", entry["statement"]):
            for piece in tokenize(identifier):
                weights[piece] = max(weights.get(piece, 0), 2.0)
            if len(identifier) >= 4 and not identifier.isalpha():
                phrases.add(identifier.lower())
        for token in tokenize(entry["statement"]):
            if token not in STOPWORDS and len(token) > 2:
                weights.setdefault(token, 1.0)
        return weights, phrases

    def search(self, entry, top_k=DEFAULT_TOP_K):
        weights, phrases = self.query_terms(entry)
        scored = []
        for index, section in enumerate(self.sections):
            counts = self.body_counts[index]
            heading = self.heading_sets[index]
            norm = self.K1 * (1 - self.B + self.B * self.lengths[index] / self.average_length)
            score = 0.0
            for token, weight in weights.items():
                idf = self.idf(token)
                tf = counts.get(token, 0)
                if tf:
                    score += weight * idf * (tf * (self.K1 + 1)) / (tf + norm)
                if token in heading:
                    score += weight * idf * 1.5
            if score > 0 and phrases:
                lowered = self.lowered[index]
                for phrase in phrases:
                    if phrase in lowered:
                        score += 5.0 * max(self.idf(token) for token in tokenize(phrase) or ["-"])
            if score > 0:
                scored.append((round(score, 3), index))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [(self.sections[index]["id"], score) for score, index in scored[:top_k]]


# ---------------------------------------------------------------------------
# Questions and requests
# ---------------------------------------------------------------------------

def build_questions():
    return {
        "contradicts": {
            "type": "noul",
            "instructions": (
                "Does `doc_section.text` state something incompatible with `fact.statement`? Judge only what the "
                "section actually says about the point the fact makes: a different name, value, default, list of "
                "options, verb, flag or rule is a contradiction. A section that does not mention the point at all, "
                "or is silent on a detail, is not a contradiction."
            ),
            "criteria": {
                "true": "The section asserts a name, value, default, option set, behavior or rule that cannot be true if the fact is true.",
                "false": "The section agrees with the fact, only partly restates it, or does not address the point at all.",
            },
        },
        "covers": {
            "type": "noul",
            "instructions": (
                "Is `doc_section` the place in this documentation site where a reader would expect `fact.statement` "
                "to be stated, judging from the page title, the heading and the content? Whether the section is "
                "currently correct does not matter; only whether this is where the fact belongs and is addressed."
            ),
            "criteria": {
                "true": "The section is about this exact topic and addresses the point the fact makes (correctly or not).",
                "false": "The section is about something else, or only mentions the topic in passing without addressing the point.",
            },
        },
        "staleness": {
            "type": "score",
            "instructions": (
                "Compared with `fact.statement`, which is derived from the current code at `fact.evidence`, how out of "
                "date is `doc_section.text` on that point?"
            ),
            "criteria": list(STALENESS_CRITERIA),
        },
    }


def build_state(entry, section, code_commit=None):
    return {
        "fact": {
            "area": entry["area"],
            "statement": entry["statement"],
            "evidence": dict(entry["evidence"], commit=code_commit) if code_commit else dict(entry["evidence"]),
        },
        "doc_section": {
            "page": section["page"],
            "title": section["title"],
            "heading": section["heading"],
            "text": section["text"][:SECTION_LIMIT],
        },
    }


def build_request(entry, section, model=DEFAULT_MODEL, code_commit=None):
    return {"state": build_state(entry, section, code_commit), "model": model, "questions": build_questions()}


def pair_key(fact_id, section_id):
    return "%s|%s" % (fact_id, section_id)


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


def summarize_error_body(body):
    """Keep an error body readable: an HTML page (a proxy or WAF answer) is collapsed to its text."""
    text = body.strip()
    if text[:1] == "<" or "<html" in text[:200].lower():
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = "HTML error page: " + " ".join(text.split())
    return text[:300]


class HttpTransport:
    """POST to the TypeSafe API with exponential backoff on 429/529."""

    RETRY_STATUSES = (429, 529)

    def __init__(self, api_key, url=API_URL, timeout=60, attempts=4, sleep=time.sleep, opener=None):
        if not api_key:
            raise DriftError("%s is not set" % KEY_ENV)
        if any(character.isspace() for character in api_key) or not api_key.isprintable():
            # Never echo the value: a newline inside a header would otherwise surface in a ValueError.
            raise DriftError("%s contains whitespace or non-printable characters; export the bare key" % KEY_ENV)
        self._api_key = api_key
        self.url = url
        self.timeout = timeout
        self.attempts = attempts
        self.sleep = sleep
        self.opener = opener

    def __call__(self, payload):
        body = json.dumps({name: value for name, value in payload.items() if not name.startswith("_")}).encode("utf-8")
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
                    "User-Agent": "nanoclaw-oss-dev-tools typesafe-docs-drift",
                },
            )
            opener = self.opener or urllib.request.urlopen
            try:
                with opener(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                detail = ""
                try:
                    # Redact before truncating: a cut through the key would defeat the replacement.
                    detail = summarize_error_body(self.redact(error.read().decode("utf-8", "replace")))
                except Exception:  # pragma: no cover - best effort
                    detail = ""
                finally:
                    error.close()
                last_error = "HTTP %s from TypeSafe: %s" % (error.code, detail.strip() or self.redact(str(error.reason)))
                if error.code not in self.RETRY_STATUSES or attempt == self.attempts:
                    raise DriftError(last_error)
            except urllib.error.URLError as error:
                last_error = "could not reach TypeSafe: %s" % self.redact(str(error.reason))
                if attempt == self.attempts:
                    raise DriftError(last_error)
            except Exception as error:  # truncated bodies (http.client.IncompleteRead), bad JSON, socket errors
                raise DriftError("TypeSafe response could not be read: %s: %s" % (type(error).__name__, self.redact(str(error))))
            self.sleep(delay)
            delay *= 2
        raise DriftError(last_error or "TypeSafe request failed")

    def redact(self, text):
        # Real keys are long; skip tiny values so redaction cannot mangle ordinary words.
        return text.replace(self._api_key, "[redacted]") if len(self._api_key) >= 8 else text


class FixtureTransport:
    """Replay recorded responses keyed by ``<fact id>|<section id>``."""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __call__(self, payload):
        self.requests.append(payload)
        key = payload.get("_key")
        try:
            return self.responses[key]
        except KeyError:
            raise DriftError("fixture has no recorded response for %s" % key)


def load_fixture(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise DriftError("could not read fixture %s: %s" % (path, error))
    for key in ("facts", "sections", "candidates", "responses"):
        if not isinstance(data, dict) or key not in data:
            raise DriftError("fixture %s must be an object with 'facts', 'sections', 'candidates' and 'responses'" % path)
    return data


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def staleness_label(score):
    if score is None:
        return None
    index = int(math.floor(score + 0.5))
    return STALENESS_LEVELS[max(0, min(len(STALENESS_LEVELS) - 1, index))]


def decide_pair(answers, thresholds):
    contradicts = number((answers.get("contradicts") or {}).get("noul"))
    covers = number((answers.get("covers") or {}).get("noul"))
    staleness = answers.get("staleness") or {}
    score = number(staleness.get("score"))
    confidence = number(staleness.get("confidence"))
    gate_c, gate_v = thresholds["contradicts"], thresholds["covers"]
    if contradicts is None or covers is None:
        verdict = "UNSURE"
        note = "no answer"
    elif contradicts >= gate_c:
        verdict = "DRIFT"
        note = ""
    elif contradicts <= 1 - gate_c and covers >= gate_v:
        verdict = "OK"
        note = ""
    elif contradicts <= 1 - gate_c and covers <= 1 - gate_v:
        verdict = "UNRELATED"
        note = ""
    else:
        verdict = "UNSURE"
        note = "contradicts %.2f, covers %.2f between the gates" % (contradicts, covers)
    staleness_gated = confidence is None or confidence < thresholds["staleness"]
    return {
        "verdict": verdict,
        "contradicts": None if contradicts is None else round(contradicts, 3),
        "covers": None if covers is None else round(covers, 3),
        "staleness": None if score is None else round(score, 3),
        "staleness_label": staleness_label(score),
        "staleness_confidence": None if confidence is None else round(confidence, 3),
        "staleness_gated": staleness_gated,
        "note": note,
    }


def decide_fact(entry, pairs, thresholds, failed=0):
    """Fold the pair decisions of one fact into a fact-level verdict; ``failed`` counts pairs with no answer."""
    result = {"id": entry["id"], "area": entry["area"], "statement": entry["statement"], "evidence": entry["evidence"],
              "pairs": pairs, "failed_pairs": failed, "documented_anywhere": None, "verdict": "MISSING", "best": None, "note": ""}
    if not pairs:
        if failed:
            result["verdict"] = "UNSURE"
            result["note"] = "%d request(s) failed; no answers" % failed
        else:
            result["note"] = "no doc section matched lexically"
        return result
    answered = [pair for pair in pairs if pair["decision"]["covers"] is not None]
    if answered:
        result["documented_anywhere"] = max(pair["decision"]["covers"] for pair in answered)
    drift = [pair for pair in pairs if pair["decision"]["verdict"] == "DRIFT"]
    ok = [pair for pair in pairs if pair["decision"]["verdict"] == "OK"]
    unsure = [pair for pair in pairs if pair["decision"]["verdict"] == "UNSURE"]
    if drift:
        best = max(drift, key=lambda pair: (pair["decision"]["contradicts"], pair["decision"]["staleness"] or 0))
        result["verdict"] = "DRIFT"
    elif ok:
        best = max(ok, key=lambda pair: pair["decision"]["covers"])
        result["verdict"] = "OK"
    elif unsure:
        best = max(unsure, key=lambda pair: (pair["decision"]["covers"] or 0, pair["decision"]["contradicts"] or 0))
        result["verdict"] = "UNSURE"
        result["note"] = best["decision"]["note"]
    elif failed:
        best = max(pairs, key=lambda pair: pair["decision"]["covers"] or 0)
        result["verdict"] = "UNSURE"
        result["note"] = "%d request(s) failed; the answered sections do not cover it" % failed
    else:
        best = max(pairs, key=lambda pair: pair["decision"]["covers"] or 0)
        result["verdict"] = "MISSING"
        result["note"] = "no candidate section covers it (max covers %.2f)" % (result["documented_anywhere"] or 0)
    result["best"] = {"section": best["section"], "decision": best["decision"]}
    return result


def rank(results):
    order = {name: index for index, name in enumerate(VERDICTS)}

    def sort_key(result):
        best = (result.get("best") or {}).get("decision") or {}
        if result["verdict"] == "DRIFT":
            secondary = (-(best.get("contradicts") or 0), -(best.get("staleness") or 0))
        elif result["verdict"] == "OK":
            secondary = (-(best.get("covers") or 0), 0)
        else:
            secondary = (-(best.get("contradicts") or 0), -(best.get("covers") or 0))
        return (order[result["verdict"]], secondary, result["area"], result["id"])

    return sorted(results, key=sort_key)


def summarize(results, pair_count, request_count):
    areas = {}
    totals = {name: 0 for name in VERDICTS}
    for result in results:
        entry = areas.setdefault(result["area"], {name: 0 for name in VERDICTS})
        entry[result["verdict"]] += 1
        totals[result["verdict"]] += 1
    return {"facts": len(results), "pairs": pair_count, "requests": request_count, "verdicts": totals,
            "areas": {area: areas[area] for area in sorted(areas)}}


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def plan_pairs(facts, sections, candidates, scores=None):
    """Return the ordered (fact, section, lexical score) jobs from a candidate map."""
    by_id = {section["id"]: section for section in sections}
    scores = scores or {}
    jobs = []
    for entry in facts:
        for section_id in candidates.get(entry["id"], []):
            section = by_id.get(section_id)
            if section is None:
                raise DriftError("candidate section %s for %s is not in the section list" % (section_id, entry["id"]))
            jobs.append((entry, section, scores.get(pair_key(entry["id"], section_id))))
    return jobs


def run_pairs(jobs, transport, thresholds, model=DEFAULT_MODEL, code_commit=None, concurrency=1, log=None):
    """Send one request per job; return per-job outcomes (None where a request failed), usage and the errors."""
    outcomes = [None] * len(jobs)
    usage = {"input_tokens": 0, "output_tokens": 0}
    errors = []
    done = [0]

    def work(index):
        entry, section, _score = jobs[index]
        payload = build_request(entry, section, model, code_commit)
        payload["_key"] = pair_key(entry["id"], section["id"])
        started = time.monotonic()
        response = transport(payload)
        return index, response, time.monotonic() - started

    def settle(index, response, elapsed):
        entry, section, score = jobs[index]
        answers = response.get("answers") if isinstance(response, dict) else None
        if not isinstance(answers, dict):
            raise DriftError("TypeSafe response for %s has no 'answers' map" % pair_key(entry["id"], section["id"]))
        for name, value in (response.get("usage") or {}).items():
            if name in usage and isinstance(value, (int, float)):
                usage[name] += int(value)
        outcomes[index] = {
            "key": pair_key(entry["id"], section["id"]),
            "section": {name: section[name] for name in ("id", "page", "title", "heading", "line") if name in section},
            "lexical_score": score,
            "response": response,
            "decision": decide_pair(answers, thresholds),
            "elapsed_seconds": round(elapsed, 3),
        }
        done[0] += 1
        if log:
            log("[%d/%d] %s (%.1fs)" % (done[0], len(jobs), outcomes[index]["key"], elapsed))

    def job_key(index):
        entry, section, _score = jobs[index]
        return pair_key(entry["id"], section["id"])

    def describe(error):
        if isinstance(error, DriftError):
            return str(error)
        # Not a transport message; keep the type only so no request detail can reach the terminal.
        return "unexpected %s" % type(error).__name__

    if concurrency <= 1 or len(jobs) <= 1:
        consecutive = 0
        for index in range(len(jobs)):
            try:
                settle(*work(index))
                consecutive = 0
            except Exception as error:
                errors.append("%s: %s" % (job_key(index), describe(error)))
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    errors.append("stopped after %d consecutive failures" % consecutive)
                    break
        return outcomes, usage, errors
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(work, index): index for index in range(len(jobs))}
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                settle(*future.result())
            except Exception as error:
                errors.append("%s: %s" % (job_key(index), describe(error)))
    return outcomes, usage, errors


def assemble(facts, jobs, outcomes, thresholds):
    grouped = {entry["id"]: [] for entry in facts}
    failed = {entry["id"]: 0 for entry in facts}
    for index, (entry, _section, _score) in enumerate(jobs):
        if outcomes[index] is not None:
            grouped[entry["id"]].append(outcomes[index])
        else:
            failed[entry["id"]] += 1
    return rank([decide_fact(entry, grouped[entry["id"]], thresholds, failed[entry["id"]]) for entry in facts])


# ---------------------------------------------------------------------------
# Rendering and files
# ---------------------------------------------------------------------------

def truncate(text, width):
    text = " ".join((text or "").split())
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def format_number(value):
    return "-" if value is None else "%.2f" % value


def table(columns, rows):
    widths = [len(name) for name in columns]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    fmt = "  ".join("{:<%d}" % width for width in widths)
    lines = [fmt.format(*columns).rstrip(), fmt.format(*["-" * width for width in widths]).rstrip()]
    lines.extend(fmt.format(*row).rstrip() for row in rows)
    return "\n".join(lines)


def render_table(results):
    rows = []
    for result in results:
        best = result.get("best") or {}
        section = best.get("section") or {}
        decision = best.get("decision") or {}
        where = section["id"] if section else "-"
        stale = format_number(decision.get("staleness"))
        if decision.get("staleness") is not None:
            stale = "%s %s%s" % (stale, decision.get("staleness_label"), "?" if decision.get("staleness_gated") else "")
        rows.append((
            result["verdict"],
            result["area"],
            truncate(result["statement"], 60),
            truncate(where, 48),
            format_number(decision.get("contradicts")),
            format_number(decision.get("covers")),
            stale,
            "%s:%s" % (result["evidence"]["file"], result["evidence"]["line"]),
        ))
    return table(("Verdict", "Area", "Fact", "Doc section", "Contra", "Covers", "Stale", "Evidence"), rows)


def render_summary(summary, usage, wall_seconds, thresholds, output_path, notes=(), partial=None):
    lines = ["", "Summary"]
    verdicts = summary["verdicts"]
    lines.append("  facts: %d, pairs: %d, requests: %d" % (summary["facts"], summary["pairs"], summary["requests"]))
    lines.append("  DRIFT %d, MISSING %d, UNSURE %d, OK %d" % tuple(verdicts[name] for name in VERDICTS))
    for area, counts in summary["areas"].items():
        lines.append("  %-17s DRIFT %d, MISSING %d, UNSURE %d, OK %d" % ((area,) + tuple(counts[name] for name in VERDICTS)))
    lines.append("  gate: contradicts >= %.2f, covers >= %.2f, staleness confidence >= %.2f" % (
        thresholds["contradicts"], thresholds["covers"], thresholds["staleness"]))
    lines.append("  tokens: input %d, output %d" % (usage["input_tokens"], usage["output_tokens"]))
    lines.append("  wall time: %.1fs" % wall_seconds)
    for note in notes:
        lines.append("  note: %s" % note)
    if partial:
        lines.append("  partial: %d request(s) failed after retries: %s" % (summary["pairs"] - summary["requests"], partial))
    if output_path:
        lines.append("  raw answers: %s" % output_path)
    return "\n".join(lines)


def render_facts(facts):
    rows = [(entry["id"], entry["area"], truncate(entry["statement"], 90),
             "%s:%s" % (entry["evidence"]["file"], entry["evidence"]["line"])) for entry in facts]
    return table(("Id", "Area", "Statement", "Evidence"), rows)


def render_plan(jobs):
    lines = []
    last = None
    for entry, section, score in jobs:
        if entry["id"] != last:
            lines.append("%s  %s" % (entry["id"], truncate(entry["statement"], 90)))
            last = entry["id"]
        lines.append("    -> %s (%s) score %s" % (section["id"], truncate(section["heading"], 40), score))
    return "\n".join(lines)


def write_output(directory, payload, prefix="drift"):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    base = "%s-%s" % (prefix, stamp())
    for suffix in [""] + ["-%d" % n for n in range(2, 1000)]:
        path = directory / (base + suffix + ".json")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as output:
            output.write(json.dumps(payload, indent=2) + "\n")
        return path
    raise DriftError("could not find a free output name under %s" % directory)


def write_record(path, facts, sections, candidates, scores, outcomes, code_commit, docs_commit):
    """Save a replayable fixture; pairs whose request failed are dropped so replay never hits a missing response."""
    answered = {outcome["key"] for outcome in outcomes if outcome}
    candidates = {fact_id: [section_id for section_id in ids if pair_key(fact_id, section_id) in answered]
                  for fact_id, ids in candidates.items()}
    scores = {key: value for key, value in scores.items() if key in answered}
    used = {section_id for ids in candidates.values() for section_id in ids}
    record = {
        "schema_version": 1,
        "recorded_at": utc_now().isoformat(),
        "code_commit": code_commit,
        "docs_commit": docs_commit,
        "facts": facts,
        "sections": [section for section in sections if section["id"] in used],
        "candidates": candidates,
        "scores": scores,
        "responses": {outcome["key"]: outcome["response"] for outcome in outcomes if outcome},
    }
    Path(path).write_text(json.dumps(record, indent=2) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--code", default=".", help="NanoClaw checkout to extract facts from (default: current directory)")
    parser.add_argument("--docs", help="nanoclaw-docs checkout (Mintlify site with docs.json); required unless --fixture or --facts-only")
    parser.add_argument("--areas", default=",".join(AREAS), help="comma-separated fact areas: %s" % ", ".join(AREAS))
    parser.add_argument("--limit-facts", type=int, default=0, help="stop after this many facts (0 = all)")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="candidate doc sections per fact (default %d)" % DEFAULT_TOP_K)
    parser.add_argument("--include-changelog", action="store_true", help="also search changelog pages (excluded by default)")
    parser.add_argument("--facts-only", action="store_true", help="print the extracted facts and exit; no docs or key needed")
    parser.add_argument("--plan", action="store_true", help="print the fact x section pairs that would be sent and exit; no key needed")
    parser.add_argument("--fixture", help="replay recorded facts, sections and responses from this JSON file instead of the checkouts and the API")
    parser.add_argument("--record", help="write facts, sections and raw responses to this JSON file for later --fixture replay")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="where raw answers are saved (default: skill-local output/, gitignored)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout per request in seconds")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="parallel requests in live mode (default %d)" % DEFAULT_CONCURRENCY)
    parser.add_argument("--contradicts-threshold", type=float, default=0.7, help="yes probability needed to call DRIFT (default 0.7)")
    parser.add_argument("--covers-threshold", type=float, default=0.7, help="yes probability needed to call a section covering (default 0.7)")
    parser.add_argument("--staleness-threshold", type=float, default=0.5, help="score confidence below which the staleness label is marked '?' (default 0.5)")
    parser.add_argument("--json", action="store_true", help="print the full result JSON to stdout instead of the table")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any pair failed after retries (default: exit 0 and report the failures under `partial`)")
    args = parser.parse_args(argv)
    for name in ("contradicts_threshold", "covers_threshold"):
        if not 0.5 <= getattr(args, name) <= 1:
            parser.error("--%s must be between 0.5 and 1" % name.replace("_", "-"))
    if not 0 <= args.staleness_threshold <= 1:
        parser.error("--staleness-threshold must be between 0 and 1")
    if args.top_k < 1:
        parser.error("--top-k must be at least 1")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    areas = [area.strip() for area in args.areas.split(",") if area.strip()]
    unknown = [area for area in areas if area not in AREAS]
    if unknown:
        parser.error("unknown area(s): %s (choose from %s)" % (", ".join(unknown), ", ".join(AREAS)))
    args.area_list = areas
    if not args.fixture and not args.facts_only and not args.docs:
        parser.error("--docs <nanoclaw-docs checkout> is required unless --fixture or --facts-only is used")
    return args


def main(argv=None, environ=None, stdout=None, stderr=None):
    environ = os.environ if environ is None else environ
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    args = parse_args(argv)
    thresholds = {"contradicts": args.contradicts_threshold, "covers": args.covers_threshold, "staleness": args.staleness_threshold}
    started = time.monotonic()
    notes = []
    try:
        if args.fixture:
            fixture = load_fixture(args.fixture)
            facts = [entry for entry in fixture["facts"] if entry["area"] in args.area_list]
            if args.limit_facts > 0:
                facts = facts[: args.limit_facts]
            sections = fixture["sections"]
            candidates = {entry["id"]: fixture["candidates"].get(entry["id"], []) for entry in facts}
            code_commit, docs_commit = fixture.get("code_commit"), fixture.get("docs_commit")
            scores = fixture.get("scores") or {}
            transport = FixtureTransport(fixture["responses"])
            mode = "fixture"
            if fixture.get("note"):
                notes.append(fixture["note"])
        else:
            mode = "live"
            facts = extract_facts(args.code, args.area_list)
            code_commit = git_commit(args.code)
            example = Path(args.code) / ".env.example"
            if not example.exists() or not read_text(example).strip():
                notes.append(".env.example is missing or empty; no facts came from it")
            if not (Path(args.code) / "docs" / "gateway-seam.md").exists():
                notes.append("docs/gateway-seam.md is absent in this checkout; gateway facts come from src/gateway-providers/index.ts")
            if args.limit_facts > 0:
                facts = facts[: args.limit_facts]
            if args.facts_only:
                sections, candidates, scores, docs_commit = [], {}, {}, None
        if args.facts_only:
            if args.json:
                print(json.dumps({"code_commit": code_commit, "facts": facts, "notes": notes}, indent=2), file=stdout)
            else:
                print(render_facts(facts), file=stdout)
                print("\n%d facts from %s @ %s" % (len(facts), args.fixture or args.code, code_commit or "unknown"), file=stdout)
                for note in notes:
                    print("note: %s" % note, file=stdout)
            return 0
        if mode == "live":
            sections = load_sections(args.docs, args.include_changelog)
            docs_commit = git_commit(args.docs)
            index = SectionIndex(sections)
            candidates = {}
            scores = {}
            for entry in facts:
                hits = index.search(entry, args.top_k)
                candidates[entry["id"]] = [section_id for section_id, _score in hits]
                for section_id, score in hits:
                    scores[pair_key(entry["id"], section_id)] = score
        if not facts:
            print("no facts extracted for areas: %s" % ", ".join(args.area_list), file=stderr)
            return 1
        jobs = plan_pairs(facts, sections, candidates, scores)
        if args.plan:
            print(render_plan(jobs), file=stdout)
            print("\n%d facts, %d pairs, %d sections from %s @ %s" % (
                len(facts), len(jobs), len(sections), args.fixture or args.docs, docs_commit or "unknown"), file=stdout)
            for note in notes:
                print("note: %s" % note, file=stdout)
            return 0
        if mode == "live":
            api_key = environ.get(KEY_ENV)
            if not api_key:
                print(
                    "%s is not set. Export it in this shell (for example `export %s=...` from your "
                    "password manager) and rerun. The key is read from the environment only; never put it "
                    "in a file inside the repository. Use --fixture <file> to run offline, or --plan / --facts-only "
                    "to see the pairs and facts without calling the API." % (KEY_ENV, KEY_ENV),
                    file=stderr,
                )
                return 2
            transport = HttpTransport(api_key, timeout=args.timeout)
        outcomes, usage, errors = run_pairs(
            jobs, transport, thresholds, args.model, code_commit,
            concurrency=args.concurrency if mode == "live" else 1,
            log=(lambda line: print(line, file=stderr)) if mode == "live" else None)
        completed = sum(1 for outcome in outcomes if outcome)
        error_text = None
        if errors:
            error_text = "; ".join(errors[:3]) + (" (+%d more)" % (len(errors) - 3) if len(errors) > 3 else "")
            print("error: %s; keeping %d completed pair(s)" % (error_text, completed), file=stderr)
            if not completed:
                return 1
        results = assemble(facts, jobs, outcomes, thresholds)
    except DriftError as error:
        print("error: %s" % error, file=stderr)
        return 1
    except Exception as error:  # never let a traceback carry request details to the terminal
        print("error: unexpected %s: %s" % (type(error).__name__, redact_env(str(error), environ)), file=stderr)
        return 1
    wall = time.monotonic() - started
    summary = summarize(results, len(jobs), completed)
    payload = {
        "schema_version": 1,
        "generated_at": utc_now().isoformat(),
        "mode": mode,
        "code": {"root": None if mode == "fixture" else str(Path(args.code).resolve()), "commit": code_commit},
        "docs": {"root": None if mode == "fixture" else str(Path(args.docs).resolve()), "commit": docs_commit},
        "model": args.model,
        "thresholds": thresholds,
        "top_k": args.top_k,
        "usage": usage,
        "wall_seconds": round(wall, 3),
        "partial": error_text,
        "notes": notes,
        "summary": summary,
        "results": results,
    }
    output_path = write_output(args.output_dir, payload)
    if args.record and mode == "live":
        write_record(args.record, facts, sections, candidates, scores, outcomes, code_commit, docs_commit)
    if args.json:
        print(json.dumps(payload, indent=2), file=stdout)
    else:
        print("Docs drift (%s mode): %d facts, %d pairs. Nothing was written to either repository." % (
            mode, len(facts), len(jobs)), file=stdout)
        print("", file=stdout)
        print(render_table(results), file=stdout)
        print(render_summary(summary, usage, wall, thresholds, output_path, notes, error_text), file=stdout)
    return 1 if error_text and args.strict else 0


def redact_env(text, environ):
    key = environ.get(KEY_ENV) or ""
    return text.replace(key, "[redacted]") if len(key) >= 8 else text


if __name__ == "__main__":
    sys.exit(main())
