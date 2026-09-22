#!/usr/bin/env python3
"""Discover NanoClaw's offered agent providers and provider-owned auth choices.

This is a read-only source inspection. It follows the same two sources as the
public picker: setup provider registrations and offered provider descriptors.
Installable providers may bundle their payload in the same Git tree, name a
branch with ``nc:copy from-branch``, or declare both (a registry payload plus a
bundled auth hook). Report the exact commit of every payload source, and the
auth source commit of the block that carries the provider's setup registration.
"""

import argparse
import ast
import json
import posixpath
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit


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
    key = 'opencode_backend' if provider == 'opencode' else '[a-z0-9_-]*auth_method'
    markers = list(re.finditer(r"setupLog\.userInput\(\s*['\"](" + key + r")['\"]", source))
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
        if provider == 'opencode' and value in ('openrouter', 'deepseek', 'local', 'custom'):
            credential_kind = 'opencode-api-key'
        elif value == "oauth" and provider == "claude":
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


def copy_entries(markdown, skill_path):
    """Read copy sources from Git, supporting bundled and branch-owned payloads.

    A skill may declare several ``nc:copy`` blocks, for example a
    ``from-branch:`` registry payload plus a bundled auth hook. Every entry
    carries its own ``branch`` (``None`` for a bundled file), so a caller can
    resolve each payload source separately.
    """
    entries = []
    for attrs, body in re.findall(r'```nc:copy([^\n]*)\n(.*?)\n```', markdown, re.S):
        branch = re.search(r'\bfrom-branch:([A-Za-z0-9._/-]+)', attrs)
        for line in body.splitlines():
            if not line.strip():
                continue
            parts = [part.strip() for part in line.strip().split(' -> ')]
            if len(parts) > 2:
                raise DiscoveryError('unsupported provider copy declaration')
            source, destination = parts[0], parts[-1]
            for path in (source, destination):
                if path.startswith('/') or '..' in path.split('/') or not re.fullmatch(r'[A-Za-z0-9_./-]+', path):
                    raise DiscoveryError('unsafe provider copy path')
            if not branch:
                source = posixpath.join(posixpath.dirname(skill_path), source)
            entries.append({'source': source, 'destination': destination,
                            'branch': branch.group(1) if branch else None})
    if not entries:
        raise DiscoveryError('provider skill must declare a bundled or branch-owned payload')
    if len({item['destination'] for item in entries}) != len(entries):
        raise DiscoveryError('provider skill has duplicate payload destinations')
    return entries


def payload_branches(entries):
    """Distinct payload sources in declaration order (``None`` is the bundled tree)."""
    return list(dict.fromkeys(item['branch'] for item in entries))


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


def validate_model(provider, backend, model, base_url=None, model_provider=None):
    if provider != 'opencode':
        if model or base_url or model_provider:
            raise DiscoveryError('OpenCode options require --provider opencode')
        return
    if backend not in ('openrouter', 'deepseek', 'local', 'custom'):
        raise DiscoveryError('OpenCode automation supports API-key backends only')
    runtime_provider = backend
    if backend in ('local', 'custom'):
        runtime_provider = model_provider or 'openai'
        if runtime_provider not in ('openai', 'openrouter', 'deepseek', 'google', 'anthropic'):
            raise DiscoveryError('Unsupported --opencode-provider API-key scheme')
        if backend == 'local' and runtime_provider != 'openai':
            raise DiscoveryError('The local backend requires the openai provider')
        try:
            url = urlsplit(base_url or '')
            valid = (base_url and len(base_url) <= 2048 and url.scheme in ('http', 'https')
                     and url.hostname and url.port != 0 and url.username is None and url.password is None
                     and not url.query and not url.fragment and '?' not in base_url and '#' not in base_url
                     and not re.search(r'[\s\x00-\x1f\x7f\\]', base_url))
        except ValueError:
            valid = False
        if not valid:
            raise DiscoveryError('--opencode-base-url must be an HTTP(S) endpoint without credentials, query or fragment')
    elif base_url or model_provider:
        raise DiscoveryError('Endpoint/provider overrides require --auth-method custom or local')
    if not model or len(model) > 256 or not re.fullmatch(re.escape(runtime_provider) + r'/[A-Za-z0-9][A-Za-z0-9._:/-]*', model):
        raise DiscoveryError('--opencode-model must be a full model ID matching the OpenCode provider')
    return runtime_provider


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
        entries = copy_entries(skill, provider['source'])
        branches = payload_branches(entries)
        # Resolve every payload source once. A bundled block uses the NanoClaw
        # revision; a from-branch block uses the fetched owning ref. The
        # optional --payload-ref names the (single) branch-owned source.
        resolved = {}
        for branch in branches:
            if branch:
                ref = resolve_payload_ref(root, branch, payload_ref)
                resolved[branch] = (ref, git(root, 'rev-parse', ref + '^{commit}'))
            else:
                if payload_ref and len(branches) == 1 \
                        and git(root, 'rev-parse', payload_ref + '^{commit}') != commit:
                    raise DiscoveryError('bundled provider payload must use the NanoClaw revision')
                resolved[None] = (revision, commit)
        if len([branch for branch in branches if branch]) > 1:
            raise DiscoveryError('provider skill declares more than one branch-owned payload')
        if len(branches) == 1:
            branch = branches[0]
            source_ref, source_commit = resolved[branch]
            provider.update(payload_kind='branch' if branch else 'bundled', payload_files=entries)
        else:
            # Several payload blocks: report every source with its commit and
            # stamp each file with the commit it is copied from.
            for item in entries:
                item['ref'], item['commit'] = resolved[item['branch']]
            provider.update(payload_kind='mixed', payload_files=entries, payload_sources=[
                {'kind': 'branch' if branch else 'bundled', 'branch': branch,
                 'ref': resolved[branch][0], 'commit': resolved[branch][1],
                 'file_count': sum(1 for item in entries if item['branch'] == branch)}
                for branch in branches
            ])
        matches = [item for item in entries if item['destination'] == f'setup/providers/{selected}.ts']
        if len(matches) != 1:
            raise DiscoveryError('provider skill has no setup registration payload')
        # The auth source commit is the one that carries the provider's setup
        # registration (its auth hook), not necessarily the first payload block.
        if len(branches) > 1:
            source_ref, source_commit = resolved[matches[0]['branch']]
        source_path = matches[0]['source']
        source = tree_read(root, source_commit, source_path)
    if selected == 'opencode':
        # OpenCode's registry entry delegates auth to this skill-owned helper.
        if 'runOpenCodeSetupAuth' not in source:
            raise DiscoveryError('unsupported OpenCode setup auth contract')
        helper = next((item for item in provider.get('payload_files', [])
                       if item['destination'] == 'scripts/opencode-auth.ts'), None)
        source_path = helper['source'] if helper else 'scripts/opencode-auth.ts'
        if helper and helper.get('commit'):
            source_ref, source_commit = helper['ref'], helper['commit']
        source = tree_read(root, source_commit, source_path)
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
