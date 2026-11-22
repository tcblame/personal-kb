#!/usr/bin/env python3
"""Public isolated regressions for structured closeout and quality reporting."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import kb_test_guard

_TEST_GUARD = kb_test_guard.activate(__file__) if __name__ == "__main__" else None

import kb
from kb_audit_runtime_value import build_report


class CloseoutContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.root = self.directory / "data"
        config = self.directory / "config.json"
        config.write_text(json.dumps({"storage": {"root": str(self.root), "records": "records",
                                                  "runtime": "runtime", "cache": "cache"}}))
        self.env = {"PERSONAL_KB_ROOT": str(self.root), "PERSONAL_KB_CONFIG": str(config)}

    def call(self, payload, *extra):
        path = self.directory / "closeout.json"
        path.write_text(json.dumps(payload))
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kb.main(["closeout", "--json-file", str(path), *extra])
        return code, out.getvalue(), err.getvalue()

    def rows(self):
        path = self.root / "runtime/closeout.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_verified_gap_is_preserved_and_invalid_gap_is_rejected(self):
        gap = {"stage": "late_use", "evidence": "example session, turns 3 and 4"}
        self.assertEqual(self.call({"memory_gap": gap, "skipped_reason": "late action lookup"})[0], 0)
        self.assertEqual(self.rows()[0]["memory_gap"], gap)
        self.assertNotEqual(self.call({"memory_gap": {"stage": "late_use"}, "skipped_reason": "test"})[0], 0)
        self.assertEqual(len(self.rows()), 1)

    def test_json_memory_review_is_preserved(self):
        payload = {"rag_calls": 0, "skipped_reason": "maintenance only",
                   "memory_review": {"disposition": "blocked_by_instruction", "reason": "explicit task restriction",
                                     "target": "personal_kb", "instruction_source": "user"}}
        self.assertEqual(self.call(payload), (0, "", ""))
        self.assertEqual(self.rows()[0]["memory_review"], payload["memory_review"])

    def test_unknown_memory_disposition_fails_before_append(self):
        code, _, _ = self.call({"skipped_reason": "maintenance", "memory_review": {"disposition": "made_up", "reason": "x"}})
        self.assertNotEqual(code, 0)
        self.assertEqual(self.rows(), [])

    def test_written_review_requires_written_id(self):
        code, _, _ = self.call({"skipped_reason": "maintenance", "memory_review": {"disposition": "written", "reason": "x"}})
        self.assertNotEqual(code, 0)
        self.assertEqual(self.rows(), [])

    def test_permission_metadata_cannot_imply_a_write(self):
        payload = {"skipped_reason": "permission unknown", "memory_review": {
            "disposition": "blocked_by_instruction", "reason": "authority unavailable",
            "target": "unknown", "instruction_source": "unknown"}}
        self.assertEqual(self.call(payload)[0], 0)
        self.assertEqual(self.rows()[0]["written_entry_ids"], [])
        self.assertFalse((self.root / "records").exists())

    def test_empty_observed_telemetry_is_distinct_from_missing(self):
        payload = {"closeout_id": "observed", "rag_calls": 1, "linked_retrieval_ids": ["retrieval-1"],
                   "retrieval_items": [], "skipped_reason": "zero hits"}
        self.assertEqual(self.call(payload)[0], 0)
        self.assertEqual(self.rows()[0]["retrieval_items"], [])
        self.assertEqual(self.call({"closeout_id": "missing", "skipped_reason": "maintenance"})[0], 0)
        report = build_report(path=self.root / "runtime/closeout.jsonl", last_days=1, include_test=True)
        self.assertEqual(report["top3_quality_rows"], 1)
        self.assertEqual(report["top3_slot_count"], 0)
        self.assertIsNone(report["stale_top3_rate"])

    def test_json_unresolved_attempt_is_preserved(self):
        payload = {"rag_calls": 1, "unresolved_retrieval_attempts": [{"attempt_id": "attempt-1", "state": "unknown"}],
                   "skipped_reason": "tool result lost"}
        self.assertEqual(self.call(payload), (0, "", ""))
        self.assertEqual(self.rows()[0]["unresolved_retrieval_attempts"], payload["unresolved_retrieval_attempts"])

    def test_snapshot_copies_objective_fields_without_adoption(self):
        payload = {"retrieval_snapshot": {"retrieval_id": "retrieval-1", "items": [
            {"entry_id": "entry-1", "kind": "issue", "freshness_state": "fresh", "trust_tier": "verified",
             "summary": "private body must be discarded", "path": "private location"}]},
                   "skipped_reason": "no useful hint"}
        self.assertEqual(self.call(payload), (0, "", ""))
        row = self.rows()[0]
        self.assertEqual(row["linked_retrieval_ids"], ["retrieval-1"])
        self.assertEqual(row["rag_calls"], 1)
        self.assertEqual(row["hit_count"], 1)
        self.assertEqual(row["hit_entry_ids"], ["entry-1"])
        self.assertEqual(row["used_entry_ids"], [])
        self.assertEqual(set(row["retrieval_items"][0]), {"entry_id", "kind", "freshness_state", "trust_tier"})

    def test_empty_snapshot_cannot_authorize_adoption(self):
        code, _, _ = self.call({"retrieval_snapshot": {"retrieval_id": "r1", "items": []},
                                "adoption_effects": {"locate": ["invented"]}})
        self.assertNotEqual(code, 0)
        self.assertEqual(self.rows(), [])

    def test_snapshot_reuses_actual_identity_query_and_zero_telemetry(self):
        payload = {"retrieval_snapshot": {"retrieval_id": "r-context", "query": "actual task",
                   "session_id": "snapshot-session", "topic_id": "actual-topic", "hit_count": 0, "items": []},
                   "skipped_reason": "no hits", "memory_review": {"disposition": "not_reusable", "reason": "no new decision"}}
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CODEX_SESSION_ID": ""}):
            self.assertEqual(self.call(payload, "--require-complete-context"), (0, "", ""))
        row = self.rows()[0]
        self.assertEqual((row["session_id"], row["topic_id"], row["queries"]),
                         ("snapshot-session", "actual-topic", ["actual task"]))
        self.assertEqual(row["retrieval_items"], [])

    def test_parent_identity_takes_precedence_over_scout_snapshot(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "parent-thread"}):
            self.assertEqual(self.call({"retrieval_snapshot": {"retrieval_id": "r-child", "session_id": "child-thread", "items": []},
                                        "skipped_reason": "no useful hint"})[0], 0)
        self.assertEqual(self.rows()[0]["session_id"], "parent-thread")

    def test_multiple_actual_results_derive_every_link_and_union_candidates(self):
        results = [{"retrieval_id": "r-first", "query": "first", "topic_id": "topic-a", "items": [{"entry_id": "same"}]},
                   {"retrieval_id": "r-second", "query": "second", "topic_id": "topic-b", "items": [{"entry_id": "same"}, {"entry_id": "other"}]}]
        self.assertEqual(self.call({"retrieval_snapshots": results, "topic_id": "parent-task", "skipped_reason": "reviewed independently"})[0], 0)
        row = self.rows()[0]
        self.assertEqual(row["linked_retrieval_ids"], ["r-first", "r-second"])
        self.assertEqual((row["rag_calls"], row["hit_count"], row["hit_entry_ids"]), (2, 3, ["same", "other"]))
        self.assertEqual(row["queries"], ["first", "second"])
        self.assertEqual(row["used_entry_ids"], [])

    def test_multiple_results_conflicts_fail_before_any_write(self):
        results = [{"retrieval_id": "r-a", "items": []}, {"retrieval_id": "r-b", "items": []}]
        for fields in ({"linked_retrieval_ids": ["r-a"]}, {"rag_calls": 1}, {"hit_count": 1},
                       {"retrieval_snapshot": results[0]}, {"retrieval_snapshots": [results[0], results[0]]}):
            with self.subTest(fields=fields):
                self.assertNotEqual(self.call({"retrieval_snapshots": results, "skipped_reason": "x", **fields})[0], 0)
                self.assertEqual(self.rows(), [])

    def test_complete_context_rejects_missing_assessment_and_telemetry(self):
        for payload in ({"session_id": "s", "topic_id": "t", "skipped_reason": "x"},
                        {"session_id": "s", "topic_id": "t", "skipped_reason": "x", "rag_calls": 1,
                         "linked_retrieval_ids": ["r"], "memory_review": {"disposition": "not_reusable", "reason": "x"}}):
            self.assertNotEqual(self.call(payload, "--require-complete-context")[0], 0)
            self.assertEqual(self.rows(), [])

    def test_snapshot_id_conflict_fails_before_append(self):
        code, _, _ = self.call({"retrieval_snapshot": {"retrieval_id": "r1", "items": []},
                                "linked_retrieval_ids": ["other"], "skipped_reason": "x"})
        self.assertNotEqual(code, 0)
        self.assertEqual(self.rows(), [])

    def test_conflicting_snapshot_metadata_never_appends(self):
        for fields in ({"hit_count": 2}, {"hit_entry_ids": ["unreturned"]},
                       {"retrieval_items": []}, {"rejected_hit_ids": ["unreturned"]}):
            with self.subTest(fields=fields):
                code, _, _ = self.call({"retrieval_snapshot": {"retrieval_id": "r1", "items": [{"entry_id": "real"}]},
                                        "skipped_reason": "x", **fields})
                self.assertNotEqual(code, 0)
                self.assertEqual(self.rows(), [])

    def test_invalid_snapshot_and_attempt_ids_fail_before_append(self):
        for snapshot in ({"retrieval_id": "bad id", "items": []},
                         {"retrieval_id": "r1", "items": [None]},
                         {"retrieval_id": "r1", "items": [{"entry_id": "same"}, {"entry_id": "same"}]}):
            with self.subTest(snapshot=snapshot):
                self.assertNotEqual(self.call({"retrieval_snapshot": snapshot, "skipped_reason": "x"})[0], 0)
                self.assertEqual(self.rows(), [])
        for attempts in ([{"attempt_id": "a1", "state": "invented"}],
                         [{"attempt_id": "a1", "state": "unknown"}] * 2):
            self.assertNotEqual(self.call({"unresolved_retrieval_attempts": attempts, "skipped_reason": "x"})[0], 0)
            self.assertEqual(self.rows(), [])

    def test_cli_memory_review_remains_supported(self):
        self.assertEqual(self.call({"skipped_reason": "maintenance"}, "--memory-review", "already_recorded",
                                   "--memory-review-reason", "existing authority")[0], 0)
        self.assertEqual(self.rows()[0]["memory_review"], {"disposition": "already_recorded", "reason": "existing authority"})

    def test_unknown_permission_target_fails_before_append(self):
        self.assertNotEqual(self.call({"skipped_reason": "x", "memory_review": {
            "disposition": "blocked_by_instruction", "reason": "x", "target": "allow_everything"}})[0], 0)
        self.assertEqual(self.rows(), [])

    def test_nonobject_json_payload_is_a_handled_error(self):
        code, _, err = self.call([])
        self.assertNotEqual(code, 0)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.rows(), [])

    def test_duplicate_audit_rows_are_visible_without_rewriting_history(self):
        self.assertEqual(self.call({"closeout_id": "same", "skipped_reason": "maintenance"})[0], 0)
        path = self.root / "runtime/closeout.jsonl"
        with path.open("a") as handle:
            handle.write(json.dumps(self.rows()[0]) + "\n")
        before = path.read_bytes()
        report = build_report(path=path, last_days=1, include_test=True)
        self.assertEqual(report["closeout_total"], 2)
        self.assertEqual(report["unique_closeout_ids"], 1)
        self.assertEqual(report["duplicate_closeout_rows"], 1)
        self.assertEqual(path.read_bytes(), before)


def main():
    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(CloseoutContractTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(_TEST_GUARD.run(main))
