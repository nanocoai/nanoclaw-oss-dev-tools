#!/usr/bin/env python3
"""Release-day docs gate for NanoClaw: which doc pages and facts need a look at this release.

Three checks over a NanoClaw release ref and the nanoclaw-docs Mintlify site:

1. Pull request queue (deterministic, free). The pull requests merged since the
   previous stable tag, minus those whose number the docs history or pages
   already name, that change a file some docs page cites. This is the list to
   work from.
2. Stamp staleness (deterministic, free). Every docs page carries
   ``{/* verified-against: <files> @ <sha> */}`` comments; the gate asks git
   whether any cited file changed between the stamped commit and the release.
   It covers every page, but on a busy tree nearly all of them are stale, so it
   is context for the queue, not a gate.
3. The phase-1 detector (``typesafe-docs-drift.py``): code facts versus doc
   sections, judged by TypeSafe. Rows already decided are held back with a
   baseline file; a DRIFT row whose best section never mentions the fact's
   terms is demoted to UNSURE, because a section cannot contradict a fact it
   does not talk about.

It writes a digest, a JSON payload and one brief per page that needs work to a
new output directory. The briefs are facts only (pull requests, changed files,
rows, rules); a person or an agent writes the doc fix, as drafts. Nothing is
written to either repository: both are cloned into a scratch directory first.
"""

import argparse
import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


SKILL_DIR = Path(__file__).resolve().parents[1]
DETECTOR_PATH = Path(__file__).with_name("typesafe-docs-drift.py")
DEFAULT_OUTPUT_DIR = SKILL_DIR / "output" / "gate"
DEFAULT_CODE_REPO = "https://github.com/nanocoai/nanoclaw.git"
DEFAULT_DOCS_REPO = "https://github.com/glifocat/nanoclaw-docs.git"
BRANCH_REFS = ("channels", "providers")
GIT_TIMEOUT = 600
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
KEY_ENV = "TYPESAFE_API_KEY"
BASELINE_STATUSES = ("held", "false-positive", "accepted")
BRIEF_COMMIT_LIMIT = 12
STAMP = re.compile(r"\{/\*\s*verified-against(.*?)\*/\}", re.S)
ANCHOR = re.compile(r"@\s*(?:([A-Za-z][\w./-]*)\s+(?:branch\s+)?)?([0-9a-f]{7,40})\b")
BRANCH_HINT = re.compile(r"\b(channels|providers)\s+(?:branch\s+)?(?=[.\w/{*-]*/)")
ROOT_FILE = re.compile(r"^[\w.\-]+\.(?:ts|tsx|js|mjs|cjs|sh|md|mdx|json|ya?ml|toml|py|sql|css)$")
PR_NUMBER = re.compile(r"\(#(\d+)\)|Merge pull request #(\d+)")
TEST_FILE = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")
TRAILING_NUMBER = re.compile(r"\s*\(#\d+\)\s*$")
INTERNAL_TITLE = re.compile(r"^(?:ci|test|build|style)(?:\([^)]*\))?:|^chore\((?:release|deps[^)]*)\):", re.I)
STABLE_TAG = re.compile(r"^v?\d+(?:\.\d+)+$")
DEFAULT_MAX_BRIEFS = 15
SLIM_KEYS = ("id", "area", "verdict", "statement", "evidence", "terms", "best", "note", "guard", "baseline")
ORDER = {"stale": 0, "unknown-anchor": 1, "current": 2, "unstamped": 3}


class GateError(RuntimeError):
    """A failure the operator can act on; printed without a traceback."""


def public(source):
    """Text without any ``user:token@`` part of a URL."""
    return re.sub(r"(?<=://)[^/@\s]+@", "", str(source))


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def load_detector():
    spec = importlib.util.spec_from_file_location("typesafe_docs_drift_detector", DETECTOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DETECTOR = load_detector()


# ---------------------------------------------------------------------------
# verified-against stamps
# ---------------------------------------------------------------------------

def split_words(segment):
    """Words of a stamp segment: whitespace, commas, semicolons, plus and parentheses split, braces do not."""
    words, current, depth = [], [], 0
    for char in segment:
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        if depth == 0 and (char.isspace() or char in ",;+()"):
            if current:
                words.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        words.append("".join(current))
    return words


def expand_braces(word):
    match = re.search(r"\{([^{}]*)\}", word)
    if not match:
        return [word]
    expanded = []
    for option in match.group(1).split(","):
        expanded.extend(expand_braces(word[:match.start()] + option.strip() + word[match.end():]))
    return list(dict.fromkeys(expanded))


def looks_like_path(token):
    if not token or token.startswith(("http:", "https:", "#", "@", "-")):
        return False
    if "/" in token:
        return re.fullmatch(r"[\w.\-*?/@]+", token) is not None
    return ROOT_FILE.match(token) is not None or token == "Dockerfile"


def segment_paths(segment):
    paths = []
    for word in split_words(segment):
        word = word.strip("`'\"").rstrip(".:;")
        if not word:
            continue
        for token in expand_braces(word):
            token = token.strip().strip("`'\"").rstrip(".:;")
            if looks_like_path(token) and token not in paths:
                paths.append(token)
    return paths


def parse_stamp_body(body):
    """Split one stamp into anchors (the paths cited before each ``@ <sha>``) and paths with no anchor."""
    anchors, position = [], 0
    for match in ANCHOR.finditer(body):
        segment = body[position:match.start()]
        named = match.group(1)
        hint = None if named else BRANCH_HINT.search(segment)
        anchors.append({"sha": match.group(2), "branch": named if named in BRANCH_REFS else (hint.group(1) if hint else None),
                        "paths": segment_paths(segment)})
        position = match.end()
    return anchors, segment_paths(body[position:])


def parse_stamps(text):
    stamps = []
    for match in STAMP.finditer(text):
        body = match.group(1)
        anchors, unanchored = parse_stamp_body(body)
        stamps.append({
            "scope": "scoped" if body.lstrip().startswith("(scoped correction)") else "page",
            "line": text.count("\n", 0, match.start()) + 1,
            "anchors": anchors,
            "unanchored": unanchored,
        })
    return stamps


def glob_to_regex(pattern):
    out, index = [], 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        out.append("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char))
        index += 1
    return "".join(out)


def path_matcher(pattern):
    """A cited path matches a changed file when equal, a parent directory, or a glob hit."""
    if any(char in pattern for char in "*?"):
        regex = re.compile("^" + glob_to_regex(pattern) + "$")
        return lambda changed: regex.match(changed) is not None
    prefix = pattern.rstrip("/")
    return lambda changed: changed == prefix or changed.startswith(prefix + "/")


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

def run_git(args, cwd=None, check=True):
    """Run git on a scratch clone; inherited repository-location variables are dropped so the source cannot be reached."""
    env = DETECTOR.clean_git_env()
    try:
        result = subprocess.run(["git"] + list(args), cwd=cwd, env=env, capture_output=True, text=True,
                                timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise GateError("git %s timed out after %d s" % (args[0], GIT_TIMEOUT))
    except (OSError, subprocess.SubprocessError) as error:
        raise GateError("git %s could not run: %s" % (args[0], public(getattr(error, "strerror", None) or type(error).__name__)))
    if check and result.returncode != 0:
        raise GateError("git %s failed: %s" % (args[0], public((result.stderr or result.stdout).strip())[:300]))
    return result


class Repo:
    """Read-only history queries on a scratch clone, cached per (anchor, ref)."""

    def __init__(self, path):
        self.path = str(path)
        self._changed = {}
        self._ancestor = {}

    def git(self, *args, **kwargs):
        return run_git(args, cwd=self.path, **kwargs)

    def commit(self, ref):
        if ref.startswith("-"):
            raise GateError("refusing ref %r: it looks like an option" % ref)
        for candidate in (ref, "origin/" + ref, "refs/tags/" + ref):
            result = self.git("rev-parse", "--verify", "--quiet", candidate + "^{commit}", check=False)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            if result.returncode > 1 and result.stderr.strip() and not re.search(r"ambiguous|unknown revision|bad revision", result.stderr, re.I):
                raise GateError("git rev-parse failed: %s" % public(result.stderr.strip())[:300])
        return None

    def is_ancestor(self, older, newer):
        key = (older, newer)
        if key not in self._ancestor:
            self._ancestor[key] = self.git("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0
        return self._ancestor[key]

    def changed(self, base, ref):
        key = (base, ref)
        if key not in self._changed:
            output = self.git("diff", "--name-only", "--no-renames", base, ref).stdout
            self._changed[key] = [line for line in output.splitlines() if line]
        return self._changed[key]

    def commits(self, base, ref, paths, limit=BRIEF_COMMIT_LIMIT):
        output = self.git("log", "--first-parent", "--format=%h\t%s", "-n", str(limit), "%s..%s" % (base, ref), "--",
                          *paths).stdout
        return [tuple(line.split("\t", 1)) for line in output.splitlines() if "\t" in line]


def clone_at(source, ref, destination):
    """Blobless clone of ``source`` with ``ref`` checked out detached; returns the Repo and the full commit."""
    args = ["clone", "--quiet", "--no-checkout", "--filter=blob:none", "--", str(source), str(destination)]
    if run_git(args, check=False).returncode != 0:
        shutil.rmtree(destination, ignore_errors=True)
        Path(destination).mkdir()
        run_git(["clone", "--quiet", "--no-checkout", "--", str(source), str(destination)])
    repo = Repo(destination)
    commit = repo.commit(ref)
    if not commit:
        raise GateError("ref %r not found in %s" % (ref, public(source)))
    repo.git("checkout", "--quiet", "--detach", commit)
    return repo, commit


def choose_ref(repo, sha, preferred, release, others):
    """The ref an anchor is measured against: ``behind`` when the ref is newer, ``ahead`` when the release is older.

    Preferred (hinted branch) refs and the release come first, so an anchor on main is never measured against a
    registry branch that merely contains it; other branch refs are the last resort.
    """
    if not repo.commit(sha):
        return None, "unknown"
    for ref in preferred + [release]:
        if repo.is_ancestor(sha, ref):
            return ref, "behind"
    if repo.is_ancestor(release, sha):
        return release, "ahead"
    for ref in others:
        if repo.is_ancestor(sha, ref):
            return ref, "behind"
    return None, "unrelated"


# ---------------------------------------------------------------------------
# Page staleness
# ---------------------------------------------------------------------------

def branch_refs_of(repo):
    return ["origin/" + name for name in BRANCH_REFS if repo.commit("origin/" + name)]


def evaluate_page(repo, page, text, release_sha, branch_refs=None):
    anchors = []
    branch_refs = branch_refs_of(repo) if branch_refs is None else branch_refs
    stamps = parse_stamps(text)
    for stamp in stamps:
        for anchor in stamp["anchors"]:
            hinted = ["origin/" + anchor["branch"]] if anchor["branch"] and "origin/" + anchor["branch"] in branch_refs else []
            ref, relation = choose_ref(repo, anchor["sha"], hinted, release_sha, [ref for ref in branch_refs if ref not in hinted])
            entry = {"scope": stamp["scope"], "line": stamp["line"], "sha": anchor["sha"], "paths": anchor["paths"],
                     "ref": ref, "changed": [], "status": "current"}
            if relation in ("unknown", "unrelated"):
                entry["status"] = "unknown-anchor"
            elif relation == "ahead":
                entry["status"] = "ahead"
            elif not anchor["paths"]:
                entry["status"] = "no-paths"
            else:
                matchers = [path_matcher(path) for path in anchor["paths"]]
                changed = [name for name in repo.changed(anchor["sha"], ref) if any(match(name) for match in matchers)]
                if changed:
                    entry["status"] = "changed"
                    entry["changed"] = changed
            anchors.append(entry)
    if not anchors:
        status = "unstamped"
    elif any(entry["status"] == "changed" for entry in anchors):
        status = "stale"
    elif any(entry["status"] in ("unknown-anchor", "no-paths") for entry in anchors):
        status = "unknown-anchor"
    else:
        status = "current"
    changed_files = sorted({name for entry in anchors for name in entry["changed"]})
    unanchored = sorted({path for stamp in stamps for path in stamp["unanchored"]})
    return {"page": page, "status": status, "anchors": anchors, "unanchored": unanchored,
            "changed_files": changed_files, "prs": []}


def pr_number(subject):
    """``Merge pull request #N``, else the last ``(#N)``: a revert subject names the reverted change first."""
    merge = re.match(r"Merge pull request #(\d+)", subject)
    if merge:
        return int(merge.group(1))
    reverted = re.match(r'^Revert ".*"(.*)$', subject)  # the quoted title names the reverted change, not this one
    trailing = re.findall(r"\(#(\d+)\)", reverted.group(1) if reverted else subject)
    return int(trailing[-1]) if trailing else None


def pr_numbers(subject):
    return [int(number) for groups in PR_NUMBER.findall(subject) for number in groups if number]


def attach_prs(repo, results, release_sha):
    """Pull request numbers from the subjects of commits that touched a stale page's cited files."""
    for result in results:
        if result["status"] != "stale":
            continue
        numbers = []
        for entry in result["anchors"]:
            if entry["status"] != "changed":
                continue
            for _sha, subject in repo.commits(entry["sha"], entry["ref"], entry["changed"]):
                numbers.extend(pr_numbers(subject))
        result["prs"] = sorted(set(numbers), reverse=True)


# ---------------------------------------------------------------------------
# Pull requests in the release range that the docs never mention
# ---------------------------------------------------------------------------

def previous_stable(repo, release_sha):
    """The newest tag without a pre-release suffix that the release contains, other than the release itself."""
    for tag in repo.git("tag", "--merged", release_sha, "--sort=-creatordate").stdout.split():
        if STABLE_TAG.match(tag) and repo.commit("refs/tags/" + tag) != release_sha:
            return tag
    return None


def release_prs(repo, base, release_sha):
    """First-parent commits in ``base..release``: merged pull requests with the files each one changed."""
    output = repo.git("log", "--first-parent", "--format=%H%x1f%s%x1f%b%x1e", "%s..%s" % (base, release_sha)).stdout
    prs = []
    for record in output.split("\x1e"):
        fields = record.strip("\n").split("\x1f")
        if len(fields) < 3 or not fields[0].strip():
            continue
        sha, subject, body = fields[0].strip(), fields[1], fields[2]
        title = subject
        if subject.startswith("Merge pull request") and body.strip():
            title = body.strip().splitlines()[0]
        number = pr_number(subject)
        parent = repo.git("rev-parse", "--verify", "--quiet", sha + "^1", check=False)
        files = repo.git("diff", "--name-only", "--no-renames", sha + "^1" if parent.returncode == 0 else EMPTY_TREE, sha).stdout.split("\n")
        prs.append({"number": number, "sha": sha[:8], "commit": sha, "title": title,
                    "files": [name for name in files if name and not TEST_FILE.search(name)]})
    return prs


def docs_mentions(docs_repo, docs_root):
    """Pull request numbers (four digits or more) named in the docs history or pages."""
    text = docs_repo.git("log", "--format=%B").stdout
    for pattern in ("*.mdx", "*.md"):
        for path in sorted(Path(docs_root).rglob(pattern)):
            if path.is_file() and DETECTOR.inside_checkout(docs_root, path):
                text += "\n" + path.read_text(encoding="utf-8", errors="replace")
    return {int(number) for number in re.findall(r"#(\d{4,5})\b", text)}


def is_internal(title):
    """CI, test, build and release chores never change what a reader does, so they are counted, not queued."""
    return INTERNAL_TITLE.match(title.strip()) is not None


def build_queue(prs, pages, mentioned, repo=None):
    """Pull requests that change files a page cites, split into documented (named in the docs), internal and open.

    A page is hit only by a change merged after the stamp that cites the file: when the stamped commit already
    contains the pull request, the page was verified with the change in place. Citations with no anchor always count.
    """
    cited = {}
    for page in pages:
        groups = [(entry["sha"], [path_matcher(path) for path in entry["paths"]]) for entry in page["anchors"] if entry["paths"]]
        groups.append((None, [path_matcher(path) for path in page.get("unanchored", [])]))
        cited[page["page"]] = groups
    items = []
    for pr in prs:
        hits = {}
        for page, groups in cited.items():
            files = set()
            for sha, matchers in groups:
                matched = [name for name in pr["files"] if any(match(name) for match in matchers)]
                if matched and sha and repo and pr.get("commit") and repo.is_ancestor(pr["commit"], sha):
                    continue
                files.update(matched)
            if files:
                hits[page] = sorted(files)
        if hits:
            title = TRAILING_NUMBER.sub("", pr["title"])
            items.append({
                "number": pr["number"], "sha": pr["sha"], "title": title,
                "pages": sorted(hits, key=lambda page: (-len(hits[page]), page)),
                "hits": hits, "files": sorted({name for files in hits.values() for name in files}),
                "documented": pr["number"] in mentioned, "internal": is_internal(title),
            })
    items.sort(key=lambda item: (item["documented"], item["internal"], -len(item["pages"]), -(item["number"] or 0)))
    return items


def page_files(detector, docs_root):
    config_path = Path(docs_root) / "docs.json"
    if not config_path.is_file():
        raise GateError("docs.json not found under %s" % docs_root)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GateError("docs.json is not valid JSON: %s" % error)
    files = []
    for page in detector.navigation_pages(config):
        if page.startswith(detector.EXCLUDED_PAGE_PREFIXES):
            continue
        for suffix in (".mdx", ".md"):
            path = Path(docs_root) / (page + suffix)
            if path.is_file() and detector.inside_checkout(docs_root, path):
                files.append((page, path))
                break
    if not files:
        raise GateError("no documentation pages found under %s" % docs_root)
    return files


def evaluate_pages(detector, repo, docs_root, release_sha):
    branch_refs = branch_refs_of(repo)
    results = [evaluate_page(repo, page, path.read_text(encoding="utf-8", errors="replace"), release_sha, branch_refs)
               for page, path in page_files(detector, docs_root)]
    attach_prs(repo, results, release_sha)
    return sorted(results, key=lambda r: (ORDER[r["status"]], -len(r["changed_files"]), r["page"]))


# ---------------------------------------------------------------------------
# Detector rows: guard and baseline
# ---------------------------------------------------------------------------

def load_baseline(path):
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GateError("could not read baseline %s: %s" % (path, error))
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise GateError('baseline %s must be {"entries": [...]}' % path)
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("fact") or entry.get("status") not in BASELINE_STATUSES:
            raise GateError("baseline entry needs a fact id and a status in %s: %r" % (", ".join(BASELINE_STATUSES), entry))
    return entries


def drifting_pairs(row):
    return [pair for pair in row.get("pairs") or [] if pair["decision"]["verdict"] == "DRIFT" and not pair.get("guarded")]


def baseline_status(entries, row):
    """``(entry, open_pairs)``: the entry that holds this row, and the DRIFT pairs no entry holds.

    An entry without a section holds the whole fact. One with a section holds only DRIFT pairs in that section,
    so a second drifting section stays open, and a pair the guard rejected needs no entry.
    """
    matching = [entry for entry in entries if entry["fact"] == row["id"]]
    drifting = drifting_pairs(row) if row["verdict"] == "DRIFT" else []
    if not matching:
        return None, drifting
    whole = next((entry for entry in matching if entry.get("section") is None), None)
    if whole:
        return whole, []
    if drifting:
        covered = {entry["section"] for entry in matching}
        open_pairs = [pair for pair in drifting if pair["section"]["id"] not in covered]
        return (None, open_pairs) if open_pairs else (matching[0], [])
    best = ((row.get("best") or {}).get("section") or {}).get("id")
    return next((entry for entry in matching if entry["section"] == best), None), []


def distinctive_terms(row):
    return [term for term in row.get("terms") or [] if len(term) >= 3 and term.lower() != "ncl"]


def apply_guard(rows, section_text):
    """Keep a DRIFT row only through a DRIFT pair whose section mentions the fact's terms; otherwise demote it.

    A section that never names the fact cannot contradict it. The best pair moves to the strongest pair that does
    name it, so a higher-scoring unrelated section never hides a real contradiction. Returns how many rows fell.
    """
    demoted = 0
    for row in rows:
        if row["verdict"] != "DRIFT" or not section_text:
            continue
        terms = distinctive_terms(row)
        if not terms:
            continue
        pairs = [pair for pair in row.get("pairs") or [] if pair["decision"]["verdict"] == "DRIFT"]
        if not pairs and row.get("best"):
            pairs = [row["best"]]

        def names_the_fact(pair):
            text = section_text.get(pair["section"]["id"])
            return text is None or any(term.lower() in text.lower() for term in terms)

        kept = [pair for pair in pairs if names_the_fact(pair)]
        for pair in pairs:
            if pair not in kept:
                pair["guarded"] = True
        if kept:
            top = max(kept, key=lambda pair: (pair["decision"]["contradicts"] or 0, pair["decision"]["staleness"] or 0))
            row["best"] = {"section": top["section"], "decision": top["decision"]}
        else:
            row["verdict"] = "UNSURE"
            row["guard"] = "no DRIFT section mentions %s" % ", ".join("`%s`" % term for term in terms[:3])
            demoted += 1
    return demoted


def has_entry(entries, row):
    return any(entry["fact"] == row["id"] for entry in entries)


def slim(row, **extra):
    """A row without its per-section pairs and raw model answers, which stay in the detector's own output file."""
    slimmed = {key: row[key] for key in SLIM_KEYS if key in row}
    slimmed.update(extra)
    return slimmed


def classify_rows(rows, entries):
    new, known = [], []
    for row in rows:
        if row["verdict"] == "OK":
            continue
        entry, open_pairs = baseline_status(entries, row)
        if entry:
            known.append(slim(row, baseline=entry))
            continue
        if has_entry(entries, row) and open_pairs:
            top = max(open_pairs, key=lambda pair: (pair["decision"]["contradicts"] or 0, pair["decision"]["staleness"] or 0))
            row = dict(row, best={"section": top["section"], "decision": top["decision"]})
        new.append(slim(row))
    return new, known


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

def run_detector(detector, code_root, docs_root, fixture, environ, output_dir, concurrency):
    argv = ["--json", "--output-dir", str(output_dir), "--concurrency", str(concurrency)]
    argv += ["--fixture", str(fixture)] if fixture else ["--code", str(code_root), "--docs", str(docs_root)]
    out, err = io.StringIO(), io.StringIO()
    try:
        code = detector.main(argv, environ, out, err)
    except SystemExit:
        raise GateError("the detector rejected its arguments")
    if code != 0:
        raise GateError("the detector failed: %s" % (err.getvalue().strip().splitlines() or ["no message"])[-1])
    try:
        return json.loads(out.getvalue())
    except ValueError:
        raise GateError("the detector printed no JSON payload")


def section_texts(detector, docs_root):
    try:
        return {section["id"]: "\n".join((section["title"], section["heading"], section["text"]))
                for section in detector.load_sections(docs_root)}
    except detector.DriftError:
        return {}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def one_line(text, width=110):
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def row_cells(row):
    best = row.get("best") or {}
    section = best.get("section") or {}
    decision = best.get("decision") or {}
    where = "%s (line %s)" % (section.get("id", "-"), section.get("line", "?"))
    numbers = "contradicts %.2f, covers %.2f" % (decision.get("contradicts") or 0, decision.get("covers") or 0)
    evidence = row["evidence"]
    return where, numbers, "%s:%s" % (evidence["file"], evidence["line"])


def pages_text(pages, limit=4):
    shown = ", ".join(pages[:limit])
    return shown + (" (+%d)" % (len(pages) - limit) if len(pages) > limit else "")


def pr_label(item):
    return "#%d" % item["number"] if item["number"] else item["sha"]


def unchecked_anchors(pages):
    return [(page["page"], entry) for page in pages for entry in page["anchors"] if entry["status"] in ("unknown-anchor", "no-paths")]


def render_digest(gate):
    counts, rows, prs = gate["counts"], gate["detector"], gate["prs"]
    unchecked = unchecked_anchors(gate["pages"])
    lines = [
        "# Docs drift gate: %s (%s) against docs %s (%s)" % (
            gate["release"]["ref"], gate["release"]["sha"][:8], gate["docs"]["ref"], gate["docs"]["sha"][:8]),
        "",
        "Generated %s. Nothing was written to either repository." % gate["generated_at"],
        "",
        "## Summary",
        "",
    ]
    if prs["base"]:
        lines.append("- Pull requests in %s: %d. Touching files the docs cite: %d (named in the docs: %d, CI or test chores: %d, to document: %d)." % (
            prs["range"], prs["total"], prs["touching"], prs["documented"], prs["internal"], prs["undocumented"]))
    else:
        lines.append("- Pull request queue skipped: no earlier stable tag found. Pass --since <tag>.")
    lines.append("- Stamps: %d of %d pages cite a file that changed since their stamp (full list in gate.json). %d anchors could not be checked." % (
        counts["stale"], counts["pages"], len(unchecked)))
    if rows["mode"] == "skipped":
        lines.append("- Detector: skipped (%s)." % rows["reason"])
    else:
        lines.append("- Detector (%s, %d facts): new DRIFT %d, new MISSING %d, new UNSURE %d, held back %d, demoted by the guard %d." % (
            rows["mode"], rows["facts"], counts["new_drift"], counts["new_missing"], counts["new_unsure"],
            counts["known"], rows["demoted"]))
    if rows.get("partial"):
        lines.append("- **Detector run was partial** (%s): its rows are incomplete." % rows["partial"])
    lines += ["", "## Pull requests to document", ""]
    queue = [item for item in prs["items"] if not item["documented"] and not item["internal"]]
    if not queue:
        lines.append("None.")
    else:
        lines += ["Merged in the range, touching files that docs pages cite, and not named in the docs history or pages."
                  " An open docs PR may already cover one. Many change internals only; read the brief before writing anything."
                  " Pages are listed by how many of the PR's files they cite.", "",
                  "| PR | Title | Pages | Files |", "|---|---|---|---|"]
        for item in queue:
            lines.append("| %s | %s | %s | %d |" % (pr_label(item), one_line(item["title"], 80).replace("|", "/"),
                                                    pages_text(item["pages"], 3), len(item["files"])))
    lines += ["", "## New DRIFT candidates", ""]
    drift = [row for row in rows["new"] if row["verdict"] == "DRIFT"]
    if not drift:
        lines.append("None.")
    else:
        lines += ["Each needs a human check against the code before any doc edit.", "",
                  "| Fact | Doc section | Numbers | Code evidence |", "|---|---|---|---|"]
        for row in drift:
            where, numbers, evidence = row_cells(row)
            lines.append("| %s | %s | %s | %s |" % (one_line(row["statement"], 90).replace("|", "/"), where, numbers, evidence))
        lines += ["", "To hold a row that is not a docs error, add it to the baseline file:", "",
                  '`{"fact": "<fact id>", "status": "false-positive", "reason": "<why>", "decidedAt": "<date>"}`']
    if unchecked:
        lines += ["", "## Anchors that could not be checked", "",
                  "The SHA is not in the NanoClaw repository (another repository, or a rewritten branch), or the stamp cites no file."
                  " Re-verify these by hand.", ""]
        for page, entry in unchecked:
            lines.append("- %s line %d: `%s` (%s)" % (page, entry["line"], entry["sha"], entry["status"]))
    if rows["known"]:
        lines += ["", "## Held back by the baseline", ""]
        for row in rows["known"]:
            entry = row["baseline"]
            lines.append("- `%s` (%s): %s" % (row["id"], entry["status"], one_line(entry.get("reason", ""), 90)))
    lines += ["", "## What to do next", "",
              "1. Check each new DRIFT row against the code; hold the ones that are not docs errors.",
              "2. Work the briefs in order as stacked draft docs PRs (the docs repo's drift-sweep skill describes the loop).",
              "3. Bump a page's stamp only for claims that were re-verified. Report product bugs found on the way; do not document around them.",
              ""]
    return "\n".join(lines)


def render_brief(gate, page, rows, items):
    lines = [
        "# Brief: %s" % page["page"],
        "",
        "Release %s (%s). Docs base %s (%s). Draft only: do not push and do not open a PR." % (
            gate["release"]["ref"], gate["release"]["sha"][:8], gate["docs"]["ref"], gate["docs"]["sha"][:8]),
        "",
        "## Merged pull requests not named in the docs that touch what this page cites",
        "",
    ]
    if not items:
        lines.append("None.")
    for item in items:
        files = item["hits"][page["page"]]
        lines.append("- %s %s" % (pr_label(item), one_line(item["title"], 100)))
        lines.append("  - Cited files changed: %s" % ", ".join("`%s`" % name for name in files[:8]) +
                     (" (+%d)" % (len(files) - 8) if len(files) > 8 else ""))
    lines += ["", "## Stamps", "", "Page status: %s. Cited files changed since the stamp: %d." % (page["status"], len(page["changed_files"])), ""]
    for entry in page["anchors"]:
        if entry["status"] == "changed":
            lines.append("- Line %d (%s, anchor `%s`): %d cited file(s) changed." % (entry["line"], entry["scope"], entry["sha"], len(entry["changed"])))
        elif entry["status"] in ("unknown-anchor", "no-paths"):
            lines.append("- Line %d (anchor `%s`): %s. Re-verify by hand." % (entry["line"], entry["sha"], entry["status"]))
    if rows:
        lines += ["", "## Detector rows", ""]
        for row in rows:
            where, numbers, evidence = row_cells(row)
            lines += ["- %s `%s`: %s" % (row["verdict"], row["id"], one_line(row["statement"], 160)),
                      "  - Section %s. %s. Code evidence %s." % (where, numbers, evidence)]
            if row.get("guard"):
                lines.append("  - Guard: %s." % row["guard"])
    lines += [
        "",
        "## Rules",
        "",
        "- Code is the only source of truth: check `src/`, `setup/`, `container/` and `.claude/skills/`, not README, SKILL.md prose or upstream docs.",
        "- Bump `verified-against` only for claims you re-checked at that SHA. A scoped fix keeps the old page stamp.",
        "- One concern per PR, stacked, as drafts. Run `mint validate`, and `mint broken-links` when links change.",
        "- If a claim can only be made true with a warning or a workaround, write it under 'Product paper cuts' in your report. Do not document around it.",
        "",
    ]
    return "\n".join(lines)


def write_outputs(gate, directory, work):
    """``work`` is a list of (page, detector rows, undocumented pull requests) in brief order."""
    directory = Path(directory)
    (directory / "briefs").mkdir(parents=True, exist_ok=True)
    (directory / "digest.md").write_text(render_digest(gate), encoding="utf-8")
    (directory / "gate.json").write_text(json.dumps(gate, indent=2) + "\n", encoding="utf-8")
    written = []
    for index, (page, rows, items) in enumerate(work, 1):
        name = "%02d-%s.md" % (index, page["page"].replace("/", "__"))
        (directory / "briefs" / name).write_text(render_brief(gate, page, rows, items), encoding="utf-8")
        written.append(name)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--release", required=True, help="NanoClaw tag, branch or commit to check the docs against")
    parser.add_argument("--since", help="start of the pull request range (default: the newest stable tag the release contains)")
    parser.add_argument("--code-repo", default=DEFAULT_CODE_REPO, help="NanoClaw repository URL or local path (cloned, never changed)")
    parser.add_argument("--docs-repo", default=DEFAULT_DOCS_REPO, help="nanoclaw-docs repository URL or local path (cloned, never changed)")
    parser.add_argument("--docs-ref", default="main", help="docs ref to check (default: main)")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="where the run directory goes (default: the skill's gitignored output/gate)")
    parser.add_argument("--baseline", help='JSON file {"entries": [{"fact", "status", "reason", "section"?}]} of decided detector rows')
    parser.add_argument("--fixture", help="replay recorded detector answers offline (no key, no network for the detector)")
    parser.add_argument("--no-model", action="store_true", help="skip the detector and the key; stamps and the pull request queue only")
    parser.add_argument("--concurrency", type=int, default=4, help="detector requests in flight (default 4)")
    parser.add_argument("--workdir", help="scratch directory for the clones (default: a temporary directory, removed afterwards)")
    parser.add_argument("--keep", action="store_true", help="keep the scratch clones")
    parser.add_argument("--strict", action="store_true", help="exit 1 on new DRIFT rows or more undocumented pull requests than --max-undocumented")
    parser.add_argument("--max-undocumented", type=int, help="with --strict: allowed number of undocumented pull requests")
    parser.add_argument("--max-briefs", type=int, default=DEFAULT_MAX_BRIEFS,
                        help="briefs for this many pages with the most queued changes, plus every page with a new detector row (default %d)" % DEFAULT_MAX_BRIEFS)
    parser.add_argument("--json", action="store_true", help="print gate.json instead of the summary")
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.max_briefs < 0:
        parser.error("--max-briefs must not be negative")
    if args.fixture and args.no_model:
        parser.error("--fixture and --no-model cannot be combined")
    return args


def summary_lines(gate, directory, briefs):
    counts, rows, prs = gate["counts"], gate["detector"], gate["prs"]
    detector = ("skipped (%s)" % rows["reason"] if rows["mode"] == "skipped" else
                "%s, %d facts: new DRIFT %d, MISSING %d, UNSURE %d, held back %d" % (
                    rows["mode"], rows["facts"], counts["new_drift"], counts["new_missing"], counts["new_unsure"], counts["known"]))
    queue = ("%d merged in %s, %d touch cited files, %d to document" % (prs["total"], prs["range"], prs["touching"], prs["undocumented"])
             if prs["base"] else "skipped (no earlier stable tag; pass --since)")
    return [
        "Docs drift gate: %s (%s) against docs %s (%s)" % (
            gate["release"]["ref"], gate["release"]["sha"][:8], gate["docs"]["ref"], gate["docs"]["sha"][:8]),
        "Pull requests: %s." % queue,
        "Stamps: %d of %d pages cite a changed file; %d anchors unchecked." % (
            counts["stale"], counts["pages"], len(unchecked_anchors(gate["pages"]))),
        "Detector: %s." % detector,
    ] + (["Detector run was PARTIAL: %s." % rows["partial"]] if rows.get("partial") else []) + [
        "Digest: %s (%d briefs)" % (Path(directory) / "digest.md", briefs),
    ]


def unique_dir(parent, label):
    """Create ``parent/label`` (or ``label-2``, ``label-3``, ...) so two runs in one second never collide."""
    parent.mkdir(parents=True, exist_ok=True)
    for suffix in [""] + ["-%d" % number for number in range(2, 1000)]:
        path = parent / (label + suffix)
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise GateError("could not find a free run directory under %s" % parent)


def main(argv=None, environ=None, stdout=None, stderr=None):
    environ = os.environ if environ is None else environ
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    args = parse_args(argv)
    detector = DETECTOR
    if not args.fixture and not args.no_model and not environ.get(KEY_ENV):
        print("%s is not set. Export it in this shell and rerun, or use --fixture <file> (offline) or --no-model "
              "(stamps and the pull request queue only). The key is read from the environment only." % KEY_ENV, file=stderr)
        return 2
    try:
        workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="docs-drift-gate-"))
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print("error: cannot create the scratch directory: %s" % (error.strerror or error), file=stderr)
        return 2
    created = []
    try:
        for name in ("code", "docs"):
            try:
                (workdir / name).mkdir()  # exclusive: a directory that exists is never ours to clone into or delete
            except FileExistsError:
                raise GateError("%s already exists; pass an empty --workdir" % (workdir / name))
            created.append(workdir / name)
        code_repo, code_sha = clone_at(args.code_repo, args.release, workdir / "code")
        docs_repo, docs_sha = clone_at(args.docs_repo, args.docs_ref, workdir / "docs")
        label = "%s-%s" % (re.sub(r"[^\w.\-]+", "-", args.release), utc_now().strftime("%Y%m%dT%H%M%SZ"))
        run_dir = unique_dir(Path(args.output_dir), label)
        pages = evaluate_pages(detector, code_repo, docs_repo.path, code_sha)
        entries = load_baseline(args.baseline)
        base = args.since or previous_stable(code_repo, code_sha)
        base_sha = code_repo.commit(base) if base else None
        if args.since and not base_sha:
            raise GateError("--since %r not found in %s" % (args.since, public(args.code_repo)))
        if base_sha and not code_repo.is_ancestor(base_sha, code_sha):
            raise GateError("--since %r is not an ancestor of %s" % (args.since, args.release))
        items = []
        total = 0
        if base_sha:
            prs = release_prs(code_repo, base_sha, code_sha)
            total = len(prs)
            items = build_queue(prs, pages, docs_mentions(docs_repo, docs_repo.path), code_repo)
        undocumented = [item for item in items if not item["documented"] and not item["internal"]]
        for page in pages:
            hits = [item for item in undocumented if page["page"] in item["hits"]]
            page["queue"] = [item["number"] for item in hits]
            page["score"] = sum(len(item["hits"][page["page"]]) for item in hits)
        if args.no_model:
            payload, mode = None, "skipped"
        else:
            payload = run_detector(detector, code_repo.path, docs_repo.path, args.fixture, environ, run_dir / "detector", args.concurrency)
            mode = payload["mode"]
        partial = payload.get("partial") if payload else None
        if payload:
            rows = [dict(row) for row in payload["results"]]
            demoted = apply_guard(rows, section_texts(detector, docs_repo.path))
            new, known = classify_rows(rows, entries)
            facts = payload["summary"]["facts"]
        else:
            new, known, demoted, facts = [], [], 0, 0
        counts = {status: sum(1 for page in pages if page["status"] == status) for status in ORDER}
        counts.update({
            "pages": len(pages),
            "new_drift": sum(1 for row in new if row["verdict"] == "DRIFT"),
            "new_missing": sum(1 for row in new if row["verdict"] == "MISSING"),
            "new_unsure": sum(1 for row in new if row["verdict"] == "UNSURE"),
            "known": len(known),
        })
        gate = {
            "schema_version": 1,
            "generated_at": utc_now().strftime("%Y-%m-%dT%H:%MZ"),
            "release": {"ref": args.release, "sha": code_sha, "repo": public(args.code_repo)},
            "docs": {"ref": args.docs_ref, "sha": docs_sha, "repo": public(args.docs_repo)},
            "counts": counts,
            "prs": {"base": base if base_sha else None, "range": "%s..%s" % (base, args.release) if base_sha else None,
                    "total": total, "touching": len(items), "documented": sum(1 for item in items if item["documented"]),
                    "internal": sum(1 for item in items if item["internal"] and not item["documented"]),
                    "undocumented": len(undocumented), "items": items},
            "pages": pages,
            "detector": {"mode": mode, "reason": "--no-model" if mode == "skipped" else None, "facts": facts,
                         "partial": partial, "demoted": demoted, "new": new, "known": known},
        }
        by_page = {}
        for row in new:
            if row["verdict"] in ("DRIFT", "MISSING"):
                page = ((row.get("best") or {}).get("section") or {}).get("page")
                if page:
                    by_page.setdefault(page, []).append(row)
        ranked = sorted((page for page in pages if page["score"]), key=lambda page: (-page["score"], page["page"]))
        chosen = {page["page"] for page in ranked[:args.max_briefs]} | set(by_page)
        needs = [page for page in pages if page["page"] in chosen]
        needs.sort(key=lambda page: (page["page"] not in by_page, -page["score"], page["page"]))
        work = [(page, by_page.get(page["page"], []), [item for item in undocumented if page["page"] in item["hits"]]) for page in needs]
        written = write_outputs(gate, run_dir, work)
    except GateError as error:
        print("error: %s" % error, file=stderr)
        return 2
    except Exception as error:  # never let a traceback carry request details to the terminal
        print("error: unexpected %s: %s" % (type(error).__name__, detector.redact_env(str(error), environ)), file=stderr)
        return 2
    finally:
        if args.keep:
            print("scratch clones kept in %s" % workdir, file=stderr)
        else:
            for path in created:
                shutil.rmtree(path, ignore_errors=True)
            if not args.workdir:
                shutil.rmtree(workdir, ignore_errors=True)
    if args.json:
        print(json.dumps(gate, indent=2), file=stdout)
    else:
        print("\n".join(summary_lines(gate, run_dir, len(written))), file=stdout)
    if args.strict and partial:
        print("error: the detector run was partial (%s); its rows are incomplete" % partial, file=stderr)
        return 2
    findings = counts["new_drift"] > 0 or (args.max_undocumented is not None and len(undocumented) > args.max_undocumented)
    return 1 if args.strict and findings else 0


if __name__ == "__main__":
    sys.exit(main())
