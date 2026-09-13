#!/usr/bin/env python3
"""Discover NanoClaw's offered agent providers and provider-owned auth choices.

This is a read-only source inspection. It follows the same two sources as the
public picker: setup provider registrations and offered provider descriptors.
For an installable provider, auth belongs to the payload branch named by the
skill's ``nc:copy from-branch`` directive, so that payload commit is reported
separately from the NanoClaw checkout commit.
"""

import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import sys


class DiscoveryError(Exception):
    pass


def git(root, *args):
    process = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, timeout=30,
    )
    if process.returncode:
        raise DiscoveryError("git could not resolve the requested source revision")
    return process.stdout.strip()


def literal(value):
    value = value.strip()
    if value.startswith("`") and value.endswith("`") and "${" not in value:
        return value[1:-1]
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return None
    return parsed if isinstance(parsed, str) else None


def field(source, name):
    match = re.search(
        rf"(?:^|[,{{])\s*{re.escape(name)}\s*:\s*((?:'(?:\\.|[^'\\])*')|(?:\"(?:\\.|[^\"\\])*\")|(?:`(?:\\.|[^`\\])*`))",
        source,
        re.M,
    )
    return literal(match.group(1)) if match else None


def balanced(source, start, opening, closing):
    if start < 0 or source[start] != opening:
        raise DiscoveryError("provider source has an unsupported option-list shape")
    depth = 0
    quote = None
    escaped = False
    line_comment = block_comment = False
    index = start
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
        elif block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 1
        elif quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "/" and nxt == "/":
            line_comment = True
            index += 1
        elif char == "/" and nxt == "*":
            block_comment = True
            index += 1
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return source[start + 1:index]
        index += 1
    raise DiscoveryError("provider source has an unterminated option list")


def option_objects(array):
    objects = []
    index = 0
    while index < len(array):
        if array[index] == "{":
            body = balanced(array, index, "{", "}")
            objects.append(body)
            index += len(body) + 2
        else:
            index += 1
    return objects


def auth_methods(source, provider):
    markers = list(re.finditer(r"setupLog\.userInput\(\s*['\"]([a-z0-9_-]*auth_method)['\"]", source))
    if not markers:
        raise DiscoveryError("provider auth source does not expose an auth-method choice")
    marker = markers[0]
    options_at = source.rfind("options:", 0, marker.start())
    if options_at < 0:
        raise DiscoveryError("provider auth source has no option list")
    select_at = source.rfind("brightSelect", 0, options_at)
    prompt = field(source[select_at:options_at], "message") if select_at >= 0 else None
    if not prompt:
        raise DiscoveryError("provider auth source has no readable prompt")
    start = source.find("[", options_at, marker.start())
    body = balanced(source, start, "[", "]")
    methods = []
    for option in option_objects(body):
        value, label = field(option, "value"), field(option, "label")
        if not value or not label:
            continue
        hint = field(option, "hint") or ""
        lowered = (value + " " + label).lower()
        skipped = value == "skip" or label.lower().startswith("skip")
        credential_kind = None
        if value == "oauth" and provider == "claude":
            credential_kind = "anthropic-oauth"
        elif "anthropic api" in lowered or (value == "api" and provider == "claude"):
            credential_kind = "anthropic-api-key"
        elif "openai api" in lowered or (value == "api" and provider == "codex"):
            credential_kind = "openai-api-key"
        automated = credential_kind is not None
        methods.append({
            "value": value,
            "label": label,
            "hint": hint,
            "usable_for_e2e": not skipped,
            "automation": "credential-file" if automated else ("human-handoff" if not skipped else "unsupported"),
            "credential_kind": credential_kind,
        })
    if not methods:
        raise DiscoveryError("provider auth source has no readable choices")
    return prompt, marker.group(1), methods


def frontmatter(markdown, directory):
    lines = markdown.splitlines()
    if not lines or lines[0] != "---":
        return {}
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise DiscoveryError(f"{directory}/SKILL.md has invalid frontmatter")
    metadata = {}
    in_metadata = False
    for line in lines[1:end]:
        if line.strip() == "metadata:":
            in_metadata = True
            continue
        if in_metadata and re.match(r"^\S", line):
            in_metadata = False
        if not in_metadata:
            continue
        match = re.match(r"^\s+([a-z0-9-]+):\s*(.*?)\s*$", line)
        if match:
            raw = match.group(2)
            metadata[match.group(1)] = literal(raw) if raw[:1] in "'\"`" else raw
    return metadata


def tree_read(root, commit, path):
    try:
        return git(root, "show", f"{commit}:{path}")
    except DiscoveryError:
        raise DiscoveryError(f"source file is missing at the requested revision: {path}")


def tree_paths(root, commit, prefix):
    value = git(root, "ls-tree", "-r", "--name-only", commit, "--", prefix)
    return value.splitlines() if value else []


def registered_providers(root, commit):
    index = tree_read(root, commit, "setup/providers/index.ts")
    entries = []
    for module in re.findall(r"import\s+['\"]\./([a-z0-9-]+)\.js['\"]", index):
        path = f"setup/providers/{module}.ts"
        source = tree_read(root, commit, path)
        value, label, hint = field(source, "value"), field(source, "label"), field(source, "hint")
        if not value or not label or not hint:
            raise DiscoveryError(f"registered provider metadata is unreadable: {module}")
        entries.append({
            "value": value, "label": label, "hint": hint,
            "installed": True, "source": path,
        })
    return entries


def offered_descriptors(root, commit, installed):
    entries = []
    paths = [path for path in tree_paths(root, commit, ".claude/skills")
             if re.fullmatch(r"\.claude/skills/[^/]+/SKILL\.md", path)]
    for path in sorted(paths):
        directory = Path(path).parent.name
        markdown = tree_read(root, commit, path)
        metadata = frontmatter(markdown, directory)
        value = metadata.get("nanoclaw-provider")
        if not value or metadata.get("nanoclaw-provider-offered") != "true" or value in installed:
            continue
        label = metadata.get("nanoclaw-provider-label")
        hint = metadata.get("nanoclaw-provider-hint")
        if not label or not hint:
            raise DiscoveryError(f"{directory}/SKILL.md has incomplete provider metadata")
        entries.append({
            "value": value, "label": label, "hint": hint,
            "installed": False, "source": path,
        })
    return entries


def provider_branch(markdown):
    branches = set(re.findall(r"\bnc:copy\s+from-branch:([A-Za-z0-9._/-]+)", markdown))
    if len(branches) != 1:
        raise DiscoveryError("installable provider skill must name one payload branch")
    return branches.pop()


def resolve_payload_ref(root, branch, explicit):
    if explicit:
        return explicit
    refs = git(root, "for-each-ref", "--format=%(refname:short)", "refs/remotes", "refs/heads").splitlines()
    candidates = [ref for ref in refs if ref == branch or ref.endswith("/" + branch)]
    if len(candidates) != 1:
        raise DiscoveryError(
            f"cannot choose the {branch} payload ref; fetch its owning remote and pass --payload-ref"
        )
    return candidates[0]


def discover(root, selected=None, payload_ref=None, revision="HEAD"):
    root = Path(root).resolve()
    if not (root / ".git").exists():
        raise DiscoveryError("run from a NanoClaw checkout")
    commit = git(root, "rev-parse", revision + "^{commit}")
    try:
        package = json.loads(tree_read(root, commit, "package.json"))
    except (DiscoveryError, ValueError):
        raise DiscoveryError("NanoClaw package.json is unreadable")
    if package.get("name") != "nanoclaw":
        raise DiscoveryError("run from a NanoClaw checkout")
    providers = registered_providers(root, commit)
    installed = {entry["value"] for entry in providers}
    providers += offered_descriptors(root, commit, installed)
    report = {"schema_version": 1, "nanoclaw_commit": commit, "providers": providers}
    if selected is None:
        return report
    matches = [entry for entry in providers if entry["value"] == selected]
    if len(matches) != 1:
        raise DiscoveryError(f"provider is not offered by this revision: {selected}")
    provider = dict(matches[0])
    if provider["installed"]:
        source_path = provider["source"]
        source = tree_read(root, commit, source_path)
        # Claude deliberately owns the standard auth implementation in auto.ts.
        if "runAuth" not in source:
            source_path = "setup/auto.ts"
            source = tree_read(root, commit, source_path)
        source_ref, source_commit = revision, commit
    else:
        skill = tree_read(root, commit, provider["source"])
        branch = provider_branch(skill)
        source_ref = resolve_payload_ref(root, branch, payload_ref)
        source_commit = git(root, "rev-parse", source_ref + "^{commit}")
        source_path = f"setup/providers/{selected}.ts"
        source = git(root, "show", f"{source_commit}:{source_path}")
    prompt, input_key, methods = auth_methods(source, selected)
    provider.update({
        "auth_prompt": prompt,
        "auth_input_key": input_key,
        "auth_source": source_path,
        "auth_source_ref": source_ref,
        "auth_source_commit": source_commit,
        "auth_methods": methods,
    })
    report["selected"] = provider
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--provider", help="include auth choices for this offered provider")
    parser.add_argument("--payload-ref", help="already-fetched ref for an installable provider payload")
    parser.add_argument("--revision", default="HEAD", help="exact NanoClaw revision to inspect (default: HEAD)")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(discover(args.root, args.provider, args.payload_ref, args.revision), indent=2))
        return 0
    except (DiscoveryError, OSError) as error:
        print("[e2e-provider] " + str(error), file=sys.stderr)
        return 65


if __name__ == "__main__":
    sys.exit(main())
