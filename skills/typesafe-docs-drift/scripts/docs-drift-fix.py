#!/usr/bin/env python3
"""Phase 2 of the docs drift loop: from a gate run to verified docs edits applied by code.

``docs-drift-gate.py`` says what to look at. This script prepares the inputs of three agent roles and checks what
they return, so every model judgment sits between two deterministic steps:

  rows       blind inputs for the row verifier: each new DRIFT row's claim, code evidence and doc section, no model numbers
  check      validate the verdict files; keep REAL rows and CONFIRMed proposals; write ledger.json, ledger.md and plan.md
  drafts     inputs for the drafter: one per queued pull request, and one per REAL row
  proposals  blind inputs for the proposal verifier, from the drafter's proposal files
  apply      apply the kept proposals' exact text replacements to a docs checkout (no commit, no stamp bump)

The roles, file formats and prompts are in ``references/phase2.md``. This script never calls a model and never writes
outside ``--out``, except ``apply``, which edits the pages of the docs checkout the operator names.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile


SKILL_DIR = Path(__file__).resolve().parents[1]
DETECTOR_PATH = Path(__file__).with_name("typesafe-docs-drift.py")
ROW_VERDICTS = ("REAL", "NOT-A-DOCS-ERROR", "UNSURE")
PROPOSAL_VERDICTS = ("CONFIRM", "CONFIRM-WITH-CORRECTION", "REJECT")
DRAFT_PAGE_LIMIT = 6
ROW_FIX_PREFIX = "row-fix-"
EXCERPT_CONTEXT = 500
ROW_QUESTION = ("Read the code at the given file and line. Does the doc section below state something that is wrong, "
                "missing a required step, or contradicted by the code on the point the claim makes? Judge from the code only.")
PROPOSAL_QUESTION = ("Read the cited code. Is each replacement text true of the code, and does it fix what was wrong in "
                     "the old text without adding a claim the code does not support? Judge from the code only.")
DRAFT_RULES = (
    "Code is the only source of truth: read src/, setup/, container/ and .claude/skills/ at the release commit, not the pull request text, README or SKILL.md prose.",
    "Change only what the code contradicts or what a reader now needs and cannot find. If no page needs a change, answer no_change with the reason.",
    "Every change is an exact text replacement: `old` must occur exactly once in the page file; `new` keeps the page's voice, headings and component syntax.",
    "Cite evidence as file, line and a short quote from the code. Never edit verified-against stamps unless you re-checked every claim they cover.",
    "If the only honest fix is a warning or workaround for a product bug, answer no_change and name the bug in `paper_cut`.",
)


class FixError(RuntimeError):
    """A failure the operator can act on; printed without a traceback."""


def load_detector():
    spec = importlib.util.spec_from_file_location("typesafe_docs_drift_detector", DETECTOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DETECTOR = load_detector()


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "item"


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def retire(out, relative):
    """Move a file of a withdrawn item to out/stale/ so nothing keeps counting it."""
    source = Path(out) / relative
    if source.is_file():
        target = Path(out) / "stale" / relative.replace("/", "__")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)


def fingerprint(item):
    """Digest of an input without its own fingerprint: a verdict must carry it, so it cannot approve another text."""
    body = {key: value for key, value in item.items() if key != "fingerprint"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def write_input(out, stem, data, suffix=".json"):
    """Write an input only when its content differs. A changed input moves its old verdict aside, and every verdict
    must carry the fingerprint of the input it answered, so a verdict given to one text can never approve another."""
    data = dict(data)
    data["fingerprint"] = fingerprint(data)
    path = Path(out) / "inputs" / (stem + suffix)
    try:
        if path.is_file() and json.loads(path.read_text(encoding="utf-8")) == data:
            return False
    except ValueError:
        pass
    verdict = Path(out) / "verdicts" / (stem + ".json")
    if verdict.is_file():
        verdict.replace(verdict.with_name("stale-" + verdict.name))
    write_json(path, data)
    return True


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise FixError("could not read %s: %s" % (path, error))


def load_gate(gate_dir):
    gate = read_json(Path(gate_dir) / "gate.json")
    if not isinstance(gate, dict) or "detector" not in gate or "prs" not in gate:
        raise FixError("%s is not a gate.json written by docs-drift-gate.py" % (Path(gate_dir) / "gate.json"))
    return gate


def page_path(docs_root, page):
    """The page file under the docs checkout, or None when it does not exist or leaves the checkout."""
    for suffix in (".mdx", ".md"):
        path = Path(docs_root) / (page + suffix)
        if path.is_file() and DETECTOR.inside_checkout(docs_root, path):
            return path
    return None


def sections_by_id(docs_root):
    try:
        return {section["id"]: section for section in DETECTOR.load_sections(docs_root)}
    except DETECTOR.DriftError as error:
        raise FixError(str(error))


def code_ref(gate, code_root):
    return {"root": str(code_root), "commit": gate["release"]["sha"], "ref": gate["release"]["ref"]}


# ---------------------------------------------------------------------------
# rows and drafts: inputs
# ---------------------------------------------------------------------------

def command_rows(args):
    gate = load_gate(args.gate_dir)
    sections = sections_by_id(args.docs)
    out = Path(args.out)
    written = []
    drift = [row for row in gate["detector"]["new"] if row["verdict"] == "DRIFT" and row.get("best")]
    for index, row in enumerate(drift, 1):
        section = row["best"]["section"]
        stem = "row-%02d-%s" % (index, slug(row["id"]))
        found = sections.get(section["id"])
        write_input(out, stem, {
            "kind": "row", "id": stem, "fact": row["id"], "role": "row-verifier", "claim": row["statement"],
            "code": dict(code_ref(gate, args.code), **{key: row["evidence"][key] for key in ("file", "line", "excerpt")}),
            "doc": {"root": str(args.docs), "page": section["page"], "heading": section["heading"], "line": section["line"],
                    "text": found["text"] if found else None},
            "question": ROW_QUESTION,
            "answer_file": str(out / "verdicts" / (stem + ".json")),
            "answer_schema": {"id": stem, "fingerprint": "copy the input's fingerprint field unchanged", "verdict": "|".join(ROW_VERDICTS),
                              "evidence": "file:line and what the code does", "reason": "one sentence"},
        })
        written.append(stem)
    for path in sorted((out / "inputs").glob("row-*.json")) if (out / "inputs").is_dir() else []:
        if path.name.endswith((".draft.json", ".verify.json")) or path.stem in written:
            continue
        verdict = out / "verdicts" / (path.stem + ".json")
        path.unlink()  # the gate no longer raises this row: its input, verdict and any fix drafted from it go with it
        if verdict.is_file():
            verdict.replace(verdict.with_name("stale-" + verdict.name))
        derived = ROW_FIX_PREFIX + path.stem[len("row-"):]
        for relative in ("inputs/%s.draft.json" % derived, "inputs/%s.verify.json" % derived, "proposals/%s.json" % derived,
                         "verdicts/%s.json" % derived):
            retire(out, relative)
    print("%d row input(s) written to %s" % (len(written), out / "inputs"))
    return 0


def parse_numbers(text):
    try:
        return {int(part) for part in (text or "").replace(",", " ").split()}
    except ValueError:
        raise FixError("expected pull request numbers, got %r" % text)


def draft_input(gate, args, out, item):
    stem = "pr-%d" % item["number"] if item.get("number") else "commit-%s" % item["sha"]
    pages = [{"page": page, "path": str(page_path(args.docs, page) or ""), "files_changed": item["hits"][page][:20]}
             for page in item["pages"][:DRAFT_PAGE_LIMIT]]
    return stem, {
        "kind": "draft", "id": stem, "role": "drafter", "source": "queue", "pr": item.get("number"), "title": item["title"],
        "release": gate["release"]["ref"], "range": gate["prs"]["range"], "code": code_ref(gate, args.code),
        "docs": {"root": str(args.docs)}, "pages": pages, "files": item["files"][:50], "rules": list(DRAFT_RULES),
        "how": "In the code checkout run `git show %s` for the pull request's merge or squash commit and read the changed "
               "files at the release commit. The listed pages only cite those files: also grep the docs checkout for the "
               "command, setting or behaviour names the change touches, because the page that should say it may not be listed." % item["sha"],
        "answer_file": str(out / "proposals" / (stem + ".json")), "answer_schema": proposal_schema(stem),
    }


def proposal_schema(stem):
    return {
        "id": stem, "no_change": "true when no page needs a change", "reason": "why, when no_change", "paper_cut": "optional product bug",
        "changes": [{"page": "operate/ncl-cli", "heading": "section heading", "old": "exact text, once in the page file",
                     "new": "replacement", "why": "one sentence"}],
        "evidence": [{"file": "src/x.ts", "line": 1, "quote": "short quote from the code"}],
    }


def command_drafts(args):
    gate = load_gate(args.gate_dir)
    out = Path(args.out)
    only, skip = parse_numbers(args.only), parse_numbers(args.skip)
    written = []
    for item in gate["prs"]["items"]:
        if item["documented"] or item["internal"] or item.get("baseline"):
            continue
        if (only and item["number"] not in only) or item["number"] in skip:
            continue
        stem, data = draft_input(gate, args, out, item)
        write_json(out / "inputs" / (stem + ".draft.json"), data)
        written.append(stem)
    ledger_path = out / "ledger.json"
    if ledger_path.is_file():
        for row in read_json(ledger_path).get("rows", []):
            if row["decision"] != "keep":
                continue
            stem = ROW_FIX_PREFIX + row["id"][4:]
            write_json(out / "inputs" / (stem + ".draft.json"), {
                "kind": "draft", "id": stem, "role": "drafter", "source": "row", "claim": row["claim"], "code": row["code"],
                "docs": {"root": str(args.docs), "page": row["doc"]["page"], "heading": row["doc"]["heading"], "text": row["doc"]["text"]},
                "evidence_from_verifier": row["evidence"], "rules": list(DRAFT_RULES),
                "answer_file": str(out / "proposals" / (stem + ".json")), "answer_schema": proposal_schema(stem)})
            written.append(stem)
    print("%d drafter input(s) written to %s" % (len(written), out / "inputs"))
    return 0


# ---------------------------------------------------------------------------
# proposals: validation and blind inputs
# ---------------------------------------------------------------------------

def validate_proposal(proposal, docs_root):
    """Problems with a drafter's proposal; an empty list means every change can be applied mechanically."""
    if not isinstance(proposal, dict) or not isinstance(proposal.get("id"), str):
        return ["not an object with an id"]
    if "no_change" in proposal and not isinstance(proposal["no_change"], bool):
        return ["no_change must be true or false"]
    if proposal.get("no_change"):
        return [] if proposal.get("reason") or proposal.get("paper_cut") else ["no_change needs a reason"]
    changes, problems = proposal.get("changes"), []
    if not isinstance(changes, list) or not changes:
        return ["changes must be a non-empty list"]
    if not isinstance(proposal.get("evidence"), list) or not proposal["evidence"]:
        problems.append("evidence must be a non-empty list")
    elif not all(isinstance(item, dict) and isinstance(item.get("file"), str) and item["file"] for item in proposal["evidence"]):
        problems.append("every evidence item needs a file")
    for index, change in enumerate(changes):
        if not isinstance(change, dict) or not all(isinstance(change.get(key), str) and change[key] for key in ("page", "old", "new")):
            problems.append("change %d needs page, old and new text" % index)
            continue
        if change["old"] == change["new"]:
            problems.append("change %d replaces text with itself" % index)
        for key in ("why", "heading"):
            if change.get(key) is not None and not isinstance(change[key], str):
                problems.append("change %d: %s must be text" % (index, key))
        path = page_path(docs_root, change["page"])
        if path is None:
            problems.append("change %d: page %s is not in the docs checkout" % (index, change["page"]))
        elif path.read_text(encoding="utf-8", errors="replace").count(change["old"]) != 1:
            problems.append("change %d: old text does not occur exactly once in %s" % (index, change["page"]))
    return problems


def excerpt_around(text, needle):
    start = text.find(needle)
    if start < 0:
        return None
    return text[max(0, start - EXCERPT_CONTEXT): start + len(needle) + EXCERPT_CONTEXT]


def blind_evidence(items):
    """Only file, line and a short quote reach the verifier: free-text fields could carry the drafter's reasoning."""
    kept = []
    for item in items:
        entry = {"file": item["file"]}
        if isinstance(item.get("line"), int) and not isinstance(item.get("line"), bool):
            entry["line"] = item["line"]
        if isinstance(item.get("quote"), str):
            entry["quote"] = item["quote"][:400]
        kept.append(entry)
    return kept


def command_proposals(args):
    gate = load_gate(args.gate_dir)
    out = Path(args.out)
    results = {"valid": [], "invalid": [], "no_change": [], "missing": []}
    proposals = {path.stem: path for path in sorted((out / "proposals").glob("*.json"))} if (out / "proposals").is_dir() else {}
    for stem, path in proposals.items():
        try:
            proposal = read_json(path)
        except FixError:
            results["invalid"].append((stem, ["not valid JSON"]))
            continue
        try:
            problems = validate_proposal(proposal, args.docs)
        except (TypeError, KeyError, AttributeError, ValueError):
            problems = ["malformed proposal"]
        if problems:
            results["invalid"].append((stem, problems))
            continue
        if proposal.get("no_change"):
            results["no_change"].append(stem)
            continue
        changes = []
        for index, change in enumerate(proposal["changes"]):
            text = page_path(args.docs, change["page"]).read_text(encoding="utf-8", errors="replace")
            changes.append({"index": index, "page": change["page"], "heading": change.get("heading"), "old": change["old"],
                            "new": change["new"], "why": (change.get("why") or "")[:300], "page_excerpt": excerpt_around(text, change["old"])})
        item = {
            "kind": "proposal", "id": stem, "role": "proposal-verifier", "code": code_ref(gate, args.code), "changes": changes,
            "evidence": blind_evidence(proposal["evidence"]), "question": PROPOSAL_QUESTION,
            "answer_file": str(out / "verdicts" / (stem + ".json")),
            "answer_schema": {"id": stem, "fingerprint": "copy the input's fingerprint field unchanged", "verdict": "|".join(PROPOSAL_VERDICTS),
                              "evidence": "file:line and what the code does",
                              "corrections": [{"index": 0, "new": "replacement text, only for CONFIRM-WITH-CORRECTION"}]},
        }
        if stem.startswith(ROW_FIX_PREFIX):
            item["source_row"] = "row-" + stem[len(ROW_FIX_PREFIX):]
        write_input(out, stem, item, ".verify.json")
        results["valid"].append(stem)
    for path in sorted((out / "inputs").glob("*.verify.json")) if (out / "inputs").is_dir() else []:
        if path.name[: -len(".verify.json")] not in results["valid"]:
            stem = path.name[: -len(".verify.json")]
            path.unlink()  # the proposal was withdrawn or is now invalid: its old input and verdict must not stay usable
            verdict = out / "verdicts" / (stem + ".json")
            if verdict.is_file():
                verdict.replace(verdict.with_name("stale-" + verdict.name))
    for path in sorted((out / "inputs").glob("*.draft.json")) if (out / "inputs").is_dir() else []:
        stem = path.name[: -len(".draft.json")]
        if stem not in proposals:
            results["missing"].append(stem)
    write_json(out / "proposal-check.json", results)
    print("proposals: %d ready for the verifier, %d invalid, %d no change, %d drafts with no proposal" % (
        len(results["valid"]), len(results["invalid"]), len(results["no_change"]), len(results["missing"])))
    for stem, problems in results["invalid"]:
        print("  invalid %s: %s" % (stem, "; ".join(problems)))
    for stem in results["missing"]:
        print("  no proposal for %s" % stem)
    return 1 if results["invalid"] or results["missing"] else 0


# ---------------------------------------------------------------------------
# check: verdicts to a ledger
# ---------------------------------------------------------------------------

def read_verdict(path, item):
    """The verdict dict, or a string naming what is wrong with it."""
    if not path.is_file():
        return "no verdict file"
    try:
        verdict = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return "verdict is not JSON"
    if not isinstance(verdict, dict) or verdict.get("id") != item["id"]:
        return "verdict id does not match %s" % item["id"]
    if item.get("fingerprint") != fingerprint(item):
        return "the input was edited after it was written"
    if verdict.get("fingerprint") != item["fingerprint"]:
        return "the verdict does not carry this input's fingerprint (it answered another version)"
    allowed = ROW_VERDICTS if item["kind"] == "row" else PROPOSAL_VERDICTS
    if verdict.get("verdict") not in allowed:
        return "verdict must be one of %s" % ", ".join(allowed)
    if not isinstance(verdict.get("evidence"), str) or not verdict["evidence"].strip():
        return "evidence is required"
    corrected = verdict["verdict"] == "CONFIRM-WITH-CORRECTION"
    if "corrections" in verdict and not corrected:
        return "corrections are only valid with CONFIRM-WITH-CORRECTION"
    if corrected:
        corrections = verdict.get("corrections")
        valid = isinstance(corrections, list) and corrections and all(
            isinstance(c, dict) and isinstance(c.get("index"), int) and not isinstance(c.get("index"), bool)
            and 0 <= c["index"] < len(item["changes"]) and isinstance(c.get("new"), str) and c["new"].strip()
            for c in corrections)
        if not valid:
            return "CONFIRM-WITH-CORRECTION needs corrections with a valid index and new text"
    return verdict


def command_check(args):
    out = Path(args.out)
    ledger = {"rows": [], "proposals": []}
    unverified = 0
    inputs = sorted((out / "inputs").glob("*.json")) if (out / "inputs").is_dir() else []
    for path in inputs:
        item = read_json(path)
        if item.get("kind") not in ("row", "proposal"):
            continue
        verdict = read_verdict(out / "verdicts" / (item["id"] + ".json"), item)
        if isinstance(verdict, str):
            unverified += 1
            entry = {"id": item["id"], "decision": "unverified", "problem": verdict}
        elif item["kind"] == "row":
            entry = {"id": item["id"], "fact": item["fact"], "claim": item["claim"], "code": item["code"], "doc": item["doc"],
                     "verdict": verdict["verdict"], "evidence": verdict["evidence"], "reason": verdict.get("reason"),
                     "decision": "keep" if verdict["verdict"] == "REAL" else "drop"}
        else:
            changes = [dict(change) for change in item["changes"]]
            if verdict["verdict"] == "CONFIRM-WITH-CORRECTION":
                for correction in verdict["corrections"]:
                    changes[correction["index"]]["new"] = correction["new"]
            entry = {"id": item["id"], "source_row": item.get("source_row"), "verdict": verdict["verdict"], "evidence": verdict["evidence"],
                     "decision": "drop" if verdict["verdict"] == "REJECT" else "keep",
                     "changes": [{key: change.get(key) for key in ("page", "heading", "old", "new", "why")} for change in changes]}
        ledger["rows" if item["kind"] == "row" else "proposals"].append(entry)
    kept_rows = {row["id"] for row in ledger["rows"] if row["decision"] == "keep"}
    for proposal in ledger["proposals"]:  # a fix drafted from a row stands only while its row stands
        if proposal["decision"] == "keep" and proposal.get("source_row") and proposal["source_row"] not in kept_rows:
            proposal["decision"], proposal["problem"] = "drop", "its source row %s is not kept" % proposal["source_row"]
    check_path = out / "proposal-check.json"
    attention = []
    if check_path.is_file():
        found = read_json(check_path)
        attention = [stem for stem, _problems in found.get("invalid", [])] + list(found.get("missing", []))
    unverified += len(attention)
    ledger["needs_attention"] = attention
    ledger["counts"] = {
        "rows": len(ledger["rows"]), "rows_kept": sum(1 for r in ledger["rows"] if r["decision"] == "keep"),
        "rows_dropped": sum(1 for r in ledger["rows"] if r["decision"] == "drop"),
        "proposals": len(ledger["proposals"]), "proposals_kept": sum(1 for p in ledger["proposals"] if p["decision"] == "keep"),
        "proposals_dropped": sum(1 for p in ledger["proposals"] if p["decision"] == "drop"), "unverified": unverified}
    write_json(out / "ledger.json", ledger)
    (out / "ledger.md").write_text(render_ledger(ledger), encoding="utf-8")
    print(render_ledger(ledger))
    return 1 if args.strict and unverified else 0


def render_ledger(ledger):
    counts = ledger["counts"]
    lines = ["# Docs drift ledger", "",
             "Rows: %d checked, %d kept, %d dropped. Proposals: %d checked, %d kept, %d dropped. Unverified: %d." % (
                 counts["rows"], counts["rows_kept"], counts["rows_dropped"], counts["proposals"], counts["proposals_kept"],
                 counts["proposals_dropped"], counts["unverified"]), ""]
    if ledger["rows"]:
        lines += ["## Rows", "", "| Row | Verifier | Decision | Evidence |", "|---|---|---|---|"]
        for row in ledger["rows"]:
            lines.append("| %s | %s | %s | %s |" % (row["id"], row.get("verdict", "-"), row["decision"],
                                                   (row.get("evidence") or row.get("problem") or "").replace("|", "/")[:100]))
        lines.append("")
    if ledger["proposals"]:
        lines += ["## Proposals", "", "| Proposal | Verifier | Decision | Pages |", "|---|---|---|---|"]
        for proposal in ledger["proposals"]:
            pages = ", ".join(sorted({change["page"] for change in proposal.get("changes", [])})) or proposal.get("problem", "-")
            if proposal.get("problem") and proposal.get("changes"):
                pages += " (%s)" % proposal["problem"]
            lines.append("| %s | %s | %s | %s |" % (proposal["id"], proposal.get("verdict", "-"), proposal["decision"], pages))
        lines.append("")
    if ledger.get("needs_attention"):
        lines += ["Needs attention (invalid or missing proposals): %s" % ", ".join(ledger["needs_attention"]), ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def files_text(paths, docs_root):
    return ": " + ", ".join(str(Path(path).relative_to(Path(docs_root).resolve())) for path in paths) if paths else ""


def stage(path, text):
    """A new file next to ``path`` with ``text`` and the page's mode, created exclusively so no existing link is followed."""
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".drift-fix.tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(name, path.stat().st_mode & 0o7777)
    except OSError:
        os.unlink(name)
        raise
    return Path(name)


def command_apply(args):
    ledger = read_json(Path(args.out) / "ledger.json")
    contents, order = {}, []
    only = {part for part in (args.only or "").replace(",", " ").split()}
    unknown = only - {proposal["id"] for proposal in ledger["proposals"] if proposal["decision"] == "keep"}
    if unknown:
        raise FixError("--only names proposals that are not kept: %s" % ", ".join(sorted(unknown)))
    for proposal in ledger["proposals"]:
        if proposal["decision"] != "keep" or (only and proposal["id"] not in only):
            continue
        for index, change in enumerate(proposal["changes"]):
            path = page_path(args.docs, change["page"])
            if path is None:
                raise FixError("%s change %d: page %s is not in the docs checkout" % (proposal["id"], index, change["page"]))
            path = path.resolve()  # two spellings of one page share one buffer
            if path not in contents:
                contents[path] = path.read_text(encoding="utf-8")
                order.append(path)
            if contents[path].count(change["old"]) != 1:
                raise FixError("%s change %d: old text does not occur exactly once in %s (another change may have touched it)"
                               % (proposal["id"], index, change["page"]))
            contents[path] = contents[path].replace(change["old"], change["new"])
    if args.dry_run:
        print("would change %d file(s)%s" % (len(order), files_text(order, args.docs)))
        return 0
    originals = {path: path.read_text(encoding="utf-8") for path in order}
    staged, replaced = [], []
    try:
        for path in order:  # stage every file first: a failure here leaves the checkout untouched
            if not DETECTOR.inside_checkout(args.docs, path):
                raise FixError("%s is no longer a regular page of the docs checkout" % path)
            staged.append((stage(path, contents[path]), path))
        for temporary, path in staged:
            os.replace(str(temporary), str(path))
            replaced.append(path)
    except OSError as error:
        stuck = []
        for path in replaced:  # put back every page already replaced
            try:
                os.replace(str(stage(path, originals[path])), str(path))
            except OSError:
                stuck.append(path)
        if stuck:
            raise FixError("could not write the pages (%s) and could not put back: %s; check them with git diff"
                           % (error.strerror or error, ", ".join(str(path) for path in stuck)))
        raise FixError("could not write the pages (%s); %d page(s) were put back" % (error.strerror or error, len(replaced)))
    finally:
        for temporary, _path in staged:
            if temporary.exists():
                temporary.unlink()
    print("changed %d file(s)%s" % (len(order), files_text(order, args.docs)))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def common(command, gate=True, docs=True, code=True):
        command.add_argument("--out", required=True, help="work directory: inputs/, verdicts/, proposals/ and the ledger live here")
        if gate:
            command.add_argument("--gate-dir", required=True, help="a run directory written by docs-drift-gate.py (holds gate.json)")
        if docs:
            command.add_argument("--docs", required=True, help="docs checkout (a worktree cut from current origin/main)")
        if code:
            command.add_argument("--code", required=True, help="NanoClaw checkout at the release commit, read-only")

    rows = commands.add_parser("rows", help="blind verifier inputs for the new DRIFT rows")
    common(rows)
    drafts = commands.add_parser("drafts", help="drafter inputs for queued pull requests and kept rows")
    common(drafts)
    drafts.add_argument("--only", help="comma-separated pull request numbers to draft")
    drafts.add_argument("--skip", help="comma-separated pull request numbers to leave out (tracked elsewhere)")
    proposals = commands.add_parser("proposals", help="validate drafter proposals and write blind verifier inputs")
    common(proposals)
    check = commands.add_parser("check", help="validate the verdict files and write the ledger")
    common(check, gate=False, docs=False, code=False)
    check.add_argument("--strict", action="store_true", help="exit 1 when any input has no valid verdict")
    apply = commands.add_parser("apply", help="apply the kept proposals to the docs checkout")
    common(apply, gate=False, code=False)
    apply.add_argument("--dry-run", action="store_true", help="check every replacement and print the files, write nothing")
    apply.add_argument("--only", help="comma-separated proposal ids to apply (one concern per commit); default: every kept proposal")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    handlers = {"rows": command_rows, "drafts": command_drafts, "proposals": command_proposals, "check": command_check, "apply": command_apply}
    try:
        return handlers[args.command](args)
    except FixError as error:
        print("error: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
