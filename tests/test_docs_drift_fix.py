"""Docs drift phase 2: blind inputs, proposal validation, verdict ledger and mechanical apply, all offline."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/typesafe-docs-drift/scripts/docs-drift-fix.py"


def load():
    spec = importlib.util.spec_from_file_location("docs_drift_fix_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fix = load()


def write(root, relative_path, content):
    path = Path(root) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))
    return path


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fix.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="docs-drift-fix-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.docs, self.code, self.out, self.gate_dir = (self.base / name for name in ("docs", "code", "out", "gate"))
        write(self.docs, "docs.json", json.dumps({"navigation": ["ops/cli", "ops/env"]}))
        write(self.docs, "ops/cli.mdx", """
            ---
            title: CLI
            ---
            {/* verified-against: src/a.ts @ abc12345 */}

            ## Groups

            Delete a group with `ncl groups delete`. Its container keeps running.

            ## Tasks

            Tasks run every minute.
            """)
        write(self.docs, "ops/env.mdx", "---\ntitle: Env\n---\n## Ceiling\n\nThe ceiling is 30 minutes. The ceiling is 30 minutes.\n")
        self.code.mkdir()
        self.write_gate()

    def write_gate(self, rows=None, items=None):
        row = {"id": "env:CEILING", "area": "env", "verdict": "DRIFT", "statement": "CEILING defaults to 30 minutes.",
               "terms": ["CEILING"], "evidence": {"file": "src/config.ts", "line": 7, "excerpt": "const x = 1800000;"},
               "best": {"section": {"id": "ops/env#ceiling", "page": "ops/env", "heading": "Ceiling", "line": 3},
                        "decision": {"verdict": "DRIFT", "contradicts": 0.91, "covers": 0.8, "staleness": 0.7}}}
        item = {"number": 4110, "sha": "deadbeef", "title": "fix(cli): delete stops the container", "pages": ["ops/cli"],
                "hits": {"ops/cli": ["src/cli/resources/groups.ts"]}, "files": ["src/cli/resources/groups.ts"],
                "documented": False, "internal": False, "baseline": None}
        gate = {"release": {"ref": "v1.0.0", "sha": "f" * 40}, "docs": {"ref": "main", "sha": "0" * 40},
                "prs": {"range": "v0.9.0..v1.0.0", "items": items if items is not None else [
                    item, dict(item, number=4111, documented=True), dict(item, number=4112, internal=True),
                    dict(item, number=4113, baseline={"status": "no-docs-needed"}), dict(item, number=4114, sha="cafef00d")]},
                "detector": {"new": rows if rows is not None else [row]}}
        write(self.gate_dir, "gate.json", json.dumps(gate))

    def args(self, command, *extra, gate=True, docs=True, code=True):
        argv = [command, "--out", str(self.out)]
        if gate:
            argv += ["--gate-dir", str(self.gate_dir)]
        if docs:
            argv += ["--docs", str(self.docs)]
        if code:
            argv += ["--code", str(self.code)]
        return argv + list(extra)

    def read(self, relative):
        return json.loads((self.out / relative).read_text())

    def bound(self, stem, data):
        """A verdict as an agent writes it: the fingerprint of the input it read is copied in."""
        for name in (stem + ".json", stem + ".verify.json"):
            if (self.out / "inputs" / name).is_file():
                data = dict({"fingerprint": self.read("inputs/" + name)["fingerprint"]}, **data)
                break
        return json.dumps(data)

    def proposal(self, stem="pr-4110", **overrides):
        data = {"id": stem, "changes": [{"page": "ops/cli", "heading": "Groups", "old": "Its container keeps running.",
                                         "new": "Its container stops within a minute.", "why": "orphan sweep stops it"}],
                "evidence": [{"file": "src/host-sweep.ts", "line": 103, "quote": "stopOrphanedSessions"}], "reasoning": "secret thoughts"}
        data.update(overrides)
        write(self.out, "proposals/%s.json" % stem, json.dumps(data))
        return data


class RowTests(Base):
    def test_a_row_input_carries_the_claim_and_evidence_but_no_model_numbers(self):
        code, out, _err = run(*self.args("rows"))
        self.assertEqual(code, 0)
        item = self.read("inputs/row-01-env-ceiling.json")
        self.assertEqual(item["claim"], "CEILING defaults to 30 minutes.")
        self.assertEqual(item["code"]["commit"], "f" * 40)
        self.assertEqual((item["code"]["file"], item["code"]["line"]), ("src/config.ts", 7))
        self.assertIn("The ceiling is 30 minutes.", item["doc"]["text"])
        self.assertEqual(item["role"], "row-verifier")
        raw = (self.out / "inputs/row-01-env-ceiling.json").read_text()
        for leak in ("contradicts", "0.91", "DRIFT", "staleness"):
            self.assertNotIn(leak, raw)
        self.assertIn("1 row input(s)", out)

    def test_only_new_drift_rows_with_a_best_pair_become_inputs(self):
        self.write_gate(rows=[{"id": "env:A", "verdict": "UNSURE", "statement": "x", "evidence": {}, "best": None},
                              {"id": "env:B", "verdict": "DRIFT", "statement": "x", "evidence": {}, "best": None}])
        run(*self.args("rows"))
        self.assertFalse((self.out / "inputs").exists() and list((self.out / "inputs").iterdir()))


class DraftTests(Base):
    def test_queue_items_that_are_open_become_drafter_inputs(self):
        code, out, _err = run(*self.args("drafts"))
        self.assertEqual(code, 0)
        names = sorted(path.name for path in (self.out / "inputs").iterdir())
        self.assertEqual(names, ["pr-4110.draft.json", "pr-4114.draft.json"])  # not documented, internal or held
        item = self.read("inputs/pr-4110.draft.json")
        self.assertEqual((item["role"], item["pr"], item["range"]), ("drafter", 4110, "v0.9.0..v1.0.0"))
        self.assertEqual(item["pages"][0]["page"], "ops/cli")
        self.assertTrue(item["pages"][0]["path"].endswith("ops/cli.mdx"))
        self.assertTrue(any("exactly once" in rule for rule in item["rules"]))

    def test_only_and_skip_narrow_the_queue(self):
        run(*self.args("drafts", "--skip", "4110"))
        self.assertEqual([p.name for p in (self.out / "inputs").iterdir()], ["pr-4114.draft.json"])
        run(*self.args("drafts", "--only", "4110"))
        self.assertTrue((self.out / "inputs/pr-4110.draft.json").exists())
        code, _out, err = run(*self.args("drafts", "--only", "abc"))
        self.assertEqual(code, 2)
        self.assertIn("expected pull request numbers", err)

    def test_kept_rows_become_drafter_inputs_after_the_check(self):
        run(*self.args("rows"))
        write(self.out, "verdicts/row-01-env-ceiling.json", self.bound("row-01-env-ceiling", {"id": "row-01-env-ceiling", "verdict": "REAL", "evidence": "src/config.ts:7 says 1800000 ms"}))
        run("check", "--out", str(self.out))
        run(*self.args("drafts", "--only", "1"))
        item = self.read("inputs/row-fix-01-env-ceiling.draft.json")
        self.assertEqual((item["source"], item["claim"]), ("row", "CEILING defaults to 30 minutes."))
        self.assertEqual(item["evidence_from_verifier"], "src/config.ts:7 says 1800000 ms")


class ProposalTests(Base):
    def test_a_valid_proposal_gives_a_blind_input_without_the_drafters_reasoning(self):
        self.proposal()
        code, out, _err = run(*self.args("proposals"))
        self.assertEqual(code, 0)
        item = self.read("inputs/pr-4110.verify.json")
        self.assertEqual(item["role"], "proposal-verifier")
        self.assertEqual(item["changes"][0]["old"], "Its container keeps running.")
        self.assertIn("Delete a group", item["changes"][0]["page_excerpt"])
        self.assertNotIn("secret thoughts", (self.out / "inputs/pr-4110.verify.json").read_text())
        self.assertIn("1 ready for the verifier, 0 invalid, 0 no change", out)

    def test_proposals_that_cannot_be_applied_mechanically_are_rejected_before_the_verifier(self):
        self.proposal("a-twice", changes=[{"page": "ops/env", "old": "The ceiling is 30 minutes.", "new": "x"}])
        self.proposal("b-missing-page", changes=[{"page": "ops/nope", "old": "x", "new": "y"}])
        self.proposal("c-traversal", changes=[{"page": "../outside", "old": "x", "new": "y"}])
        self.proposal("d-absent-text", changes=[{"page": "ops/cli", "old": "not in the page", "new": "y"}])
        self.proposal("e-same", changes=[{"page": "ops/cli", "old": "Tasks run every minute.", "new": "Tasks run every minute."}])
        self.proposal("f-no-evidence", evidence=[])
        self.proposal("g-string-false", no_change="false", reason="x")
        code, out, _err = run(*self.args("proposals"))
        self.assertEqual(code, 1)  # invalid proposals need attention
        result = self.read("proposal-check.json")
        self.assertEqual(result["valid"], [])
        self.assertEqual(sorted(stem for stem, _ in result["invalid"]), ["a-twice", "b-missing-page", "c-traversal", "d-absent-text", "e-same", "f-no-evidence", "g-string-false"])
        self.assertIn("does not occur exactly once", out)
        self.assertIn("not in the docs checkout", out)

    def test_a_no_change_proposal_needs_a_reason_and_skips_the_verifier(self):
        write(self.out, "proposals/pr-4114.json", json.dumps({"id": "pr-4114", "no_change": True, "reason": "internal rename"}))
        write(self.out, "proposals/pr-4115.json", json.dumps({"id": "pr-4115", "no_change": True}))
        code, _out, _err = run(*self.args("proposals"))
        self.assertEqual(code, 1)
        result = self.read("proposal-check.json")
        self.assertEqual(result["no_change"], ["pr-4114"])
        self.assertEqual([stem for stem, _ in result["invalid"]], ["pr-4115"])


class CheckAndApplyTests(Base):
    def verdict(self, stem, **fields):
        data = {"id": stem, "verdict": "CONFIRM", "evidence": "src/host-sweep.ts:103 stops it"}
        data.update(fields)
        write(self.out, "verdicts/%s.json" % stem, self.bound(stem, data))

    def ready(self):
        self.proposal()
        run(*self.args("proposals"))

    def test_verdicts_decide_keep_or_drop_and_bad_ones_are_unverified(self):
        run(*self.args("rows"))
        self.ready()
        self.verdict("row-01-env-ceiling", verdict="NOT-A-DOCS-ERROR", evidence="src/config.ts:7 matches the docs")
        self.verdict("pr-4110", verdict="CONFIRM-WITH-CORRECTION", corrections=[{"index": 0, "new": "Its container stops."}])
        code, out, _err = run("check", "--out", str(self.out))
        self.assertEqual(code, 0)
        ledger = self.read("ledger.json")
        self.assertEqual(ledger["counts"], {"rows": 1, "rows_kept": 0, "rows_dropped": 1, "proposals": 1, "proposals_kept": 1,
                                            "proposals_dropped": 0, "unverified": 0})
        self.assertEqual(ledger["proposals"][0]["changes"][0]["new"], "Its container stops.")
        self.assertIn("Rows: 1 checked, 0 kept, 1 dropped. Proposals: 1 checked, 1 kept, 0 dropped. Unverified: 0.", out)
        self.assertTrue((self.out / "ledger.md").exists())

    def test_invalid_verdicts_count_as_unverified_and_strict_fails(self):
        run(*self.args("rows"))
        self.ready()
        self.verdict("pr-4110")  # valid, so every strict failure below comes from the row verdict
        good = {"id": "row-01-env-ceiling", "verdict": "REAL", "evidence": "x"}
        self.assertEqual(run("check", "--out", str(self.out), "--strict")[0], 1)  # no row verdict yet
        write(self.out, "verdicts/row-01-env-ceiling.json", self.bound("row-01-env-ceiling", good))
        self.assertEqual(run("check", "--out", str(self.out), "--strict")[0], 0)
        for bad in ({"verdict": None}, {"id": "other"}, {"verdict": "MAYBE"}, {"evidence": " "}, {"corrections": []},
                    {"verdict": "NOT-A-DOCS-ERROR", "corrections": [{"index": 0, "new": "x"}]}):
            write(self.out, "verdicts/row-01-env-ceiling.json", self.bound("row-01-env-ceiling", dict(good, **bad)))
            code, _out, _err = run("check", "--out", str(self.out), "--strict")
            self.assertEqual(code, 1, bad)
            self.assertEqual(self.read("ledger.json")["counts"]["unverified"], 1, bad)
        self.verdict("pr-4110", verdict="CONFIRM-WITH-CORRECTION")  # no corrections
        write(self.out, "verdicts/row-01-env-ceiling.json", self.bound("row-01-env-ceiling", good))
        self.assertEqual(run("check", "--out", str(self.out), "--strict")[0], 1)
        self.verdict("pr-4110", verdict="CONFIRM-WITH-CORRECTION", corrections=[{"index": 3, "new": "x"}])
        self.assertEqual(run("check", "--out", str(self.out), "--strict")[0], 1)
        self.verdict("pr-4110", verdict="CONFIRM-WITH-CORRECTION", corrections=[{"index": 0, "new": None}])
        self.assertEqual(run("check", "--out", str(self.out), "--strict")[0], 1)
        self.verdict("pr-4110", verdict="REJECT", corrections=[{"index": 9, "new": "x"}])  # must not crash the check
        self.assertEqual(run("check", "--out", str(self.out), "--strict")[0], 1)

    def test_apply_replaces_the_kept_text_exactly_once(self):
        self.ready()
        self.verdict("pr-4110")
        run("check", "--out", str(self.out))
        code, out, _err = run("apply", "--out", str(self.out), "--docs", str(self.docs), "--dry-run")
        self.assertEqual((code, "ops/cli.mdx" in out), (0, True))
        self.assertIn("Its container keeps running.", (self.docs / "ops/cli.mdx").read_text())
        code, out, _err = run("apply", "--out", str(self.out), "--docs", str(self.docs))
        self.assertEqual(code, 0)
        text = (self.docs / "ops/cli.mdx").read_text()
        self.assertIn("Its container stops within a minute.", text)
        self.assertNotIn("keeps running", text)
        self.assertIn("verified-against: src/a.ts @ abc12345", text)  # stamps are never touched

    def test_rejected_and_unverified_proposals_are_not_applied(self):
        self.ready()
        self.verdict("pr-4110", verdict="REJECT", evidence="src/host-sweep.ts:103 does not stop it")
        run("check", "--out", str(self.out))
        code, out, _err = run("apply", "--out", str(self.out), "--docs", str(self.docs))
        self.assertEqual((code, out.strip()), (0, "changed 0 file(s)"))
        self.assertIn("keeps running", (self.docs / "ops/cli.mdx").read_text())

    def test_apply_is_all_or_nothing(self):
        self.proposal("pr-a", changes=[{"page": "ops/cli", "old": "Tasks run every minute.", "new": "Tasks run hourly.", "why": "x"}])
        self.proposal("pr-b", changes=[{"page": "ops/cli", "old": "Its container keeps running.", "new": "Its container stops.", "why": "x"}])
        run(*self.args("proposals"))
        self.verdict("pr-a")
        self.verdict("pr-b")
        run("check", "--out", str(self.out))
        ledger = self.read("ledger.json")
        ledger["proposals"][1]["changes"][0]["old"] = "text that is gone"
        (self.out / "ledger.json").write_text(json.dumps(ledger))
        before = (self.docs / "ops/cli.mdx").read_text()
        code, _out, err = run("apply", "--out", str(self.out), "--docs", str(self.docs))
        self.assertEqual(code, 2)
        self.assertIn("old text does not occur exactly once", err)
        self.assertEqual((self.docs / "ops/cli.mdx").read_text(), before)

    def test_a_late_verdict_for_an_older_input_is_refused(self):
        self.ready()
        stale_fingerprint = self.read("inputs/pr-4110.verify.json")["fingerprint"]
        self.proposal("pr-4110", changes=[{"page": "ops/cli", "old": "Its container keeps running.", "new": "Its container is deleted.", "why": "x"}])
        run(*self.args("proposals"))
        self.assertNotEqual(stale_fingerprint, self.read("inputs/pr-4110.verify.json")["fingerprint"])
        write(self.out, "verdicts/pr-4110.json", json.dumps({"id": "pr-4110", "fingerprint": stale_fingerprint, "verdict": "CONFIRM", "evidence": "x"}))
        code, _out, _err = run("check", "--out", str(self.out), "--strict")
        self.assertEqual(code, 1)
        self.assertIn("another version", self.read("ledger.json")["proposals"][0]["problem"])
        write(self.out, "verdicts/pr-4110.json", json.dumps({"id": "pr-4110", "verdict": "CONFIRM", "evidence": "x"}))
        self.assertEqual(run("check", "--out", str(self.out), "--strict")[0], 1)  # no fingerprint at all

    def test_an_input_edited_by_hand_cannot_be_approved(self):
        self.ready()
        path = self.out / "inputs/pr-4110.verify.json"
        data = json.loads(path.read_text())
        data["changes"][0]["new"] = "something else entirely"
        path.write_text(json.dumps(data))
        self.verdict("pr-4110")
        self.assertEqual(run("check", "--out", str(self.out), "--strict")[0], 1)
        self.assertIn("edited after it was written", self.read("ledger.json")["proposals"][0]["problem"])

    def test_malformed_proposal_json_removes_the_old_approval(self):
        self.ready()
        self.verdict("pr-4110")
        run("check", "--out", str(self.out))
        write(self.out, "proposals/pr-4110.json", "{not json")
        code, out, _err = run(*self.args("proposals"))
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", out)
        self.assertFalse((self.out / "inputs/pr-4110.verify.json").exists())
        code, _out, _err = run("check", "--out", str(self.out), "--strict")
        self.assertEqual(code, 1)
        self.assertEqual(self.read("ledger.json")["counts"]["proposals_kept"], 0)

    def test_a_row_the_gate_no_longer_raises_takes_its_input_verdict_and_fix_with_it(self):
        run(*self.args("rows"))
        write(self.out, "verdicts/row-01-env-ceiling.json", self.bound("row-01-env-ceiling", {"id": "row-01-env-ceiling", "verdict": "REAL", "evidence": "x"}))
        run("check", "--out", str(self.out))
        run(*self.args("drafts", "--only", "1"))
        self.proposal("row-fix-01-env-ceiling")
        run(*self.args("proposals"))
        self.verdict("row-fix-01-env-ceiling")
        run("check", "--out", str(self.out))
        self.assertEqual(self.read("ledger.json")["counts"]["proposals_kept"], 1)
        self.write_gate(rows=[])
        run(*self.args("rows"))
        self.assertFalse((self.out / "inputs/row-01-env-ceiling.json").exists())
        self.assertTrue((self.out / "verdicts/stale-row-01-env-ceiling.json").exists())
        run("check", "--out", str(self.out))
        ledger = self.read("ledger.json")
        self.assertEqual((ledger["counts"]["rows"], ledger["counts"]["proposals_kept"], ledger["proposals"]), (0, 0, []))
        self.assertTrue((self.out / "stale/verdicts__row-fix-01-env-ceiling.json").exists())

    def test_a_staging_link_planted_next_to_a_page_is_never_followed(self):
        import os
        self.proposal("pr-a", changes=[{"page": "ops/cli", "old": "Tasks run every minute.", "new": "Tasks run hourly.", "why": "x"}])
        run(*self.args("proposals"))
        self.verdict("pr-a")
        run("check", "--out", str(self.out))
        outside = self.base / "outside.txt"
        outside.write_text("keep me")
        os.symlink(str(outside), str(self.docs / "ops" / "cli.mdx.drift-fix.tmp"))
        self.assertEqual(run("apply", "--out", str(self.out), "--docs", str(self.docs))[0], 0)
        self.assertEqual(outside.read_text(), "keep me")
        self.assertIn("Tasks run hourly.", (self.docs / "ops/cli.mdx").read_text())
        self.assertFalse((self.docs / "ops/cli.mdx").is_symlink())

    def test_a_failed_replace_puts_back_the_pages_already_replaced(self):
        import os
        from unittest import mock
        self.proposal("pr-a", changes=[{"page": "ops/cli", "old": "Tasks run every minute.", "new": "Tasks run hourly.", "why": "x"}])
        self.proposal("pr-b", changes=[{"page": "ops/env", "old": "The ceiling is 30 minutes. The ceiling is 30 minutes.", "new": "The ceiling is 30 minutes.", "why": "x"}])
        run(*self.args("proposals"))
        self.verdict("pr-a")
        self.verdict("pr-b")
        run("check", "--out", str(self.out))
        before = [(self.docs / name).read_text() for name in ("ops/cli.mdx", "ops/env.mdx")]
        real, calls = os.replace, []

        def flaky(source, destination):
            calls.append(destination)
            if len(calls) == 2:
                raise OSError(5, "disk error")
            return real(source, destination)

        with mock.patch.object(fix.os, "replace", side_effect=flaky):
            code, _out, err = run("apply", "--out", str(self.out), "--docs", str(self.docs))
        self.assertEqual(code, 2)
        self.assertIn("1 page(s) were put back", err)
        self.assertEqual([(self.docs / name).read_text() for name in ("ops/cli.mdx", "ops/env.mdx")], before)
        self.assertEqual([path.name for path in (self.docs / "ops").iterdir() if path.name.endswith(".tmp")], [])

    def test_a_rollback_that_fails_is_reported_not_claimed(self):
        import os
        from unittest import mock
        self.proposal("pr-a", changes=[{"page": "ops/cli", "old": "Tasks run every minute.", "new": "Tasks run hourly.", "why": "x"}])
        self.proposal("pr-b", changes=[{"page": "ops/env", "old": "The ceiling is 30 minutes. The ceiling is 30 minutes.", "new": "The ceiling is 30 minutes.", "why": "x"}])
        run(*self.args("proposals"))
        self.verdict("pr-a")
        self.verdict("pr-b")
        run("check", "--out", str(self.out))
        real, calls = os.replace, []

        def failing(source, destination):
            calls.append(destination)
            if len(calls) >= 2:
                raise OSError(5, "disk error")
            return real(source, destination)

        with mock.patch.object(fix.os, "replace", side_effect=failing):
            code, _out, err = run("apply", "--out", str(self.out), "--docs", str(self.docs))
        self.assertEqual(code, 2)
        self.assertIn("could not put back", err)
        self.assertNotIn("were put back", err)
        self.assertIn("cli.mdx", err)

    def test_a_proposal_with_a_wrongly_typed_field_is_invalid_and_withdraws_its_approval(self):
        self.ready()
        self.verdict("pr-4110")
        run("check", "--out", str(self.out))
        self.proposal("pr-4110", changes=[{"page": "ops/cli", "old": "Its container keeps running.", "new": "Its container stops.", "why": 42}])
        code, out, _err = run(*self.args("proposals"))
        self.assertEqual(code, 1)
        self.assertIn("why must be text", out)
        self.assertFalse((self.out / "inputs/pr-4110.verify.json").exists())
        self.proposal("pr-4110", evidence=[5], changes=[{"page": "ops/cli", "old": "Its container keeps running.", "new": "Its container stops."}])
        code, out, _err = run(*self.args("proposals"))  # a malformed entry must not abort the run
        self.assertEqual(code, 1)
        self.assertEqual(run("check", "--out", str(self.out), "--strict")[0], 1)
        self.assertEqual(self.read("ledger.json")["counts"]["proposals_kept"], 0)

    def test_pruning_a_row_also_retires_its_unfinished_fix(self):
        run(*self.args("rows"))
        write(self.out, "verdicts/row-01-env-ceiling.json", self.bound("row-01-env-ceiling", {"id": "row-01-env-ceiling", "verdict": "REAL", "evidence": "x"}))
        run("check", "--out", str(self.out))
        run(*self.args("drafts", "--only", "1"))
        self.proposal("row-fix-01-env-ceiling")
        run(*self.args("proposals"))  # a verify input exists, with no verdict yet
        self.write_gate(rows=[])
        run(*self.args("rows"))
        for relative in ("inputs/row-fix-01-env-ceiling.draft.json", "inputs/row-fix-01-env-ceiling.verify.json", "proposals/row-fix-01-env-ceiling.json"):
            self.assertFalse((self.out / relative).exists(), relative)
        self.assertTrue((self.out / "stale/proposals__row-fix-01-env-ceiling.json").exists())
        run(*self.args("proposals"))
        self.assertEqual(run("check", "--out", str(self.out), "--strict")[0], 0)

    def test_a_changed_proposal_moves_its_old_verdict_aside(self):
        self.ready()
        self.verdict("pr-4110")
        run("check", "--out", str(self.out))
        self.assertEqual(self.read("ledger.json")["counts"]["proposals_kept"], 1)
        run(*self.args("proposals"))  # unchanged: the input and its verdict stay as they are
        run("check", "--out", str(self.out))
        self.assertEqual(self.read("ledger.json")["counts"]["proposals_kept"], 1)
        self.proposal("pr-4110", changes=[{"page": "ops/cli", "old": "Its container keeps running.", "new": "Its container is deleted.", "why": "x"}])
        run(*self.args("proposals"))
        self.assertFalse((self.out / "verdicts/pr-4110.json").exists())
        self.assertTrue((self.out / "verdicts/stale-pr-4110.json").exists())
        code, _out, _err = run("check", "--out", str(self.out), "--strict")
        self.assertEqual(code, 1)
        ledger = self.read("ledger.json")
        self.assertEqual((ledger["counts"]["proposals_kept"], ledger["counts"]["unverified"]), (0, 1))
        self.assertEqual(ledger["proposals"][0]["problem"], "no verdict file")

    def test_a_changed_row_input_moves_its_verdict_aside(self):
        run(*self.args("rows"))
        write(self.out, "verdicts/row-01-env-ceiling.json", self.bound("row-01-env-ceiling", {"id": "row-01-env-ceiling", "verdict": "REAL", "evidence": "x"}))
        run(*self.args("rows"))
        self.assertTrue((self.out / "verdicts/row-01-env-ceiling.json").exists())
        self.write_gate(rows=[{"id": "env:CEILING", "verdict": "DRIFT", "statement": "CEILING defaults to 60 minutes.", "terms": [],
                               "evidence": {"file": "src/config.ts", "line": 7, "excerpt": "x"},
                               "best": {"section": {"id": "ops/env#ceiling", "page": "ops/env", "heading": "Ceiling", "line": 3}, "decision": {}}}])
        run(*self.args("rows"))
        self.assertFalse((self.out / "verdicts/row-01-env-ceiling.json").exists())
        self.assertTrue((self.out / "verdicts/stale-row-01-env-ceiling.json").exists())

    def test_a_withdrawn_or_invalidated_proposal_loses_its_input_and_cannot_be_applied(self):
        self.ready()
        self.verdict("pr-4110")
        run("check", "--out", str(self.out))
        write(self.out, "proposals/pr-4110.json", json.dumps({"id": "pr-4110", "no_change": True, "reason": "withdrawn"}))
        run(*self.args("proposals"))
        self.assertFalse((self.out / "inputs/pr-4110.verify.json").exists())
        run("check", "--out", str(self.out))
        self.assertEqual(self.read("ledger.json")["proposals"], [])
        code, _out, err = run("apply", "--out", str(self.out), "--docs", str(self.docs), "--only", "pr-4110")
        self.assertEqual(code, 2)
        self.proposal("pr-4110", changes=[{"page": "ops/cli", "old": "text that is not there", "new": "x", "why": "x"}])
        run(*self.args("proposals"))
        self.assertFalse((self.out / "inputs/pr-4110.verify.json").exists())

    def test_a_fix_drafted_from_a_row_is_dropped_when_its_row_is_dropped(self):
        import os
        run(*self.args("rows"))
        write(self.out, "verdicts/row-01-env-ceiling.json", self.bound("row-01-env-ceiling", {"id": "row-01-env-ceiling", "verdict": "REAL", "evidence": "x"}))
        run("check", "--out", str(self.out))
        run(*self.args("drafts", "--only", "1"))
        self.proposal("row-fix-01-env-ceiling")
        run(*self.args("proposals"))
        self.assertEqual(self.read("inputs/row-fix-01-env-ceiling.verify.json")["source_row"], "row-01-env-ceiling")
        self.verdict("row-fix-01-env-ceiling")
        run("check", "--out", str(self.out))
        self.assertEqual(self.read("ledger.json")["counts"]["proposals_kept"], 1)
        write(self.out, "verdicts/row-01-env-ceiling.json", self.bound("row-01-env-ceiling", {"id": "row-01-env-ceiling", "verdict": "NOT-A-DOCS-ERROR", "evidence": "x"}))
        run("check", "--out", str(self.out))
        ledger = self.read("ledger.json")
        self.assertEqual((ledger["counts"]["proposals_kept"], ledger["proposals"][0]["decision"]), (0, "drop"))
        self.assertIn("source row", ledger["proposals"][0]["problem"])

    def test_two_spellings_of_one_page_share_one_buffer(self):
        self.proposal("pr-a", changes=[{"page": "ops/cli", "old": "Tasks run every minute.", "new": "Tasks run hourly.", "why": "x"}])
        self.proposal("pr-b", changes=[{"page": "ops/../ops/cli", "old": "Its container keeps running.", "new": "Its container stops.", "why": "x"}])
        run(*self.args("proposals"))
        self.verdict("pr-a")
        self.verdict("pr-b")
        run("check", "--out", str(self.out))
        self.assertEqual(run("apply", "--out", str(self.out), "--docs", str(self.docs))[0], 0)
        text = (self.docs / "ops/cli.mdx").read_text()
        self.assertIn("Tasks run hourly.", text)
        self.assertIn("Its container stops.", text)

    def test_a_write_failure_leaves_every_page_untouched(self):
        import os
        self.proposal("pr-a", changes=[{"page": "ops/cli", "old": "Tasks run every minute.", "new": "Tasks run hourly.", "why": "x"}])
        self.proposal("pr-b", changes=[{"page": "ops/env", "old": "The ceiling is 30 minutes. The ceiling is 30 minutes.", "new": "The ceiling is 30 minutes.", "why": "x"}])
        run(*self.args("proposals"))
        self.verdict("pr-a")
        self.verdict("pr-b")
        run("check", "--out", str(self.out))
        before = [(self.docs / name).read_text() for name in ("ops/cli.mdx", "ops/env.mdx")]
        os.chmod(str(self.docs / "ops"), 0o555)  # cannot create the staging file for the second page
        self.addCleanup(os.chmod, str(self.docs / "ops"), 0o755)
        code, _out, err = run("apply", "--out", str(self.out), "--docs", str(self.docs))
        self.assertEqual(code, 2)
        self.assertIn("could not write the pages", err)
        self.assertEqual([(self.docs / name).read_text() for name in ("ops/cli.mdx", "ops/env.mdx")], before)
        self.assertEqual([path.name for path in (self.docs / "ops").iterdir() if path.name.endswith(".tmp")], [])

    def test_free_text_in_evidence_never_reaches_the_blind_verifier(self):
        self.proposal(evidence=[{"file": "src/x.ts", "line": 3, "quote": "q" * 900, "reasoning": "my private analysis", "confidence": 0.9}])
        run(*self.args("proposals"))
        item = self.read("inputs/pr-4110.verify.json")
        self.assertEqual(item["evidence"], [{"file": "src/x.ts", "line": 3, "quote": "q" * 400}])
        self.assertNotIn("private analysis", (self.out / "inputs/pr-4110.verify.json").read_text())

    def test_drafts_with_no_proposal_and_invalid_proposals_fail_a_strict_check(self):
        run(*self.args("drafts", "--only", "4110"))
        code, out, _err = run(*self.args("proposals"))
        self.assertEqual(code, 1)
        self.assertIn("no proposal for pr-4110", out)
        self.assertEqual(run("check", "--out", str(self.out), "--strict")[0], 1)
        self.proposal("pr-4110", changes=[{"page": "ops/cli", "old": "absent", "new": "x"}])
        run(*self.args("proposals"))
        code, _out, _err = run("check", "--out", str(self.out), "--strict")
        self.assertEqual(code, 1)
        self.assertEqual(self.read("ledger.json")["needs_attention"], ["pr-4110"])
        self.assertIn("Needs attention", (self.out / "ledger.md").read_text())

    def test_only_applies_the_named_proposals_and_refuses_unkept_ones(self):
        self.proposal("pr-a", changes=[{"page": "ops/cli", "old": "Tasks run every minute.", "new": "Tasks run hourly.", "why": "x"}])
        self.proposal("pr-b", changes=[{"page": "ops/cli", "old": "Its container keeps running.", "new": "Its container stops.", "why": "x"}])
        run(*self.args("proposals"))
        self.verdict("pr-a")
        self.verdict("pr-b", verdict="REJECT")
        run("check", "--out", str(self.out))
        self.assertEqual(run("apply", "--out", str(self.out), "--docs", str(self.docs), "--only", "pr-a")[0], 0)
        text = (self.docs / "ops/cli.mdx").read_text()
        self.assertIn("Tasks run hourly.", text)
        self.assertIn("keeps running", text)
        code, _out, err = run("apply", "--out", str(self.out), "--docs", str(self.docs), "--only", "pr-b")
        self.assertEqual(code, 2)
        self.assertIn("not kept: pr-b", err)

    def test_two_kept_proposals_on_one_page_apply_in_order(self):
        self.proposal("pr-a", changes=[{"page": "ops/cli", "old": "Tasks run every minute.", "new": "Tasks run hourly.", "why": "x"}])
        self.proposal("pr-b", changes=[{"page": "ops/cli", "old": "Its container keeps running.", "new": "Its container stops.", "why": "x"}])
        run(*self.args("proposals"))
        self.verdict("pr-a")
        self.verdict("pr-b")
        run("check", "--out", str(self.out))
        self.assertEqual(run("apply", "--out", str(self.out), "--docs", str(self.docs))[0], 0)
        text = (self.docs / "ops/cli.mdx").read_text()
        self.assertIn("Tasks run hourly.", text)
        self.assertIn("Its container stops.", text)

    def test_missing_files_are_reported_without_a_traceback(self):
        code, _out, err = run("rows", "--out", str(self.out), "--gate-dir", str(self.base / "nowhere"), "--docs", str(self.docs), "--code", str(self.code))
        self.assertEqual(code, 2)
        self.assertIn("could not read", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
