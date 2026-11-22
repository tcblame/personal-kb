import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from kb_audit_codex_sessions import _parse_session, _session_paths
from kb_record_validation import metadata_errors
from kb_rag_context import _prefer_learned_alias_to_repo_name
from kb_session_identity import filename_session_id, open_rollout


class RecallTests(unittest.TestCase):
    def test_owner_keeps_its_eight_existing_aliases_when_a_new_one_is_evidenced(self):
        row = {"aliases": [f"seed-{n}" for n in range(8)] + ["中途歇会"],
               "trigger_terms": ["问卷", "暂停", "恢复"]}
        self.assertTrue(metadata_errors(row))
        row["recall_checks"] = {"positive": ["答问卷想中途歇会"], "negative": ["中途歇首歌"]}
        self.assertEqual(metadata_errors(row), [])
        row["aliases"].append("没有问过的另一个名字")
        self.assertTrue(metadata_errors(row))

    def test_learned_object_precedes_matches_on_project_name_alone(self):
        def record(entry_id, score, *terms, branch="main"):
            return {"entry_id": entry_id, "repo": "example_project", "branch": branch,
                    "matched_anchors": [{"term": value} for value in terms], "_score": score}
        items = {item["entry_id"]: item for item in [
            record("form", 3, "example", "问卷停止"), record("package", 8, "example"),
            record("equally_specific", 6, "问卷恢复"), record("other_branch", 7, "example", "dev", branch="dev")]}
        _prefer_learned_alias_to_repo_name(items, {"form", "equally_specific"})
        self.assertEqual(items["package"]["_score"], 8)
        _prefer_learned_alias_to_repo_name(items, {"form"})
        self.assertLess(items["package"]["_score"], items["form"]["_score"])
        self.assertEqual(items["equally_specific"]["_score"], 6)
        self.assertEqual(items["other_branch"]["_score"], 7)


class ArchiveTests(unittest.TestCase):
    # Zstandard frame containing an own session_meta and a user turn the following day.
    frame = base64.b64decode(
        "KLUv/WAPADUFACLKIB1gR+zwpcOug9tbLwFr6QBBUNu0jSDhZoKYDhzWCcOc+mD7hniCorclMkevAL4B"
        "RcsV9u/uiP6N8u6Qzwl8DTXMqW1BkNjAb94iKaVU6u5OzMwpAIAoEFSzNn3Q0oTBxWEMk+6OB9u/vzO"
        "OVDRQgd21u7iN7lRsInpb0VLGMfnumtQnDAAuBCCkywigMMgBejQMmCuI7iqQdtKoEn9npI1AlQE=")

    def test_archived_continuation_and_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            root.mkdir()
            archive = root.parent / "archived_sessions"
            archive.mkdir()
            name = "rollout-2026-09-22T21-00-00-12345678-1111-2222-3333-123456789012.jsonl"
            packed = archive / (name + ".zst")
            packed.write_bytes(self.frame)
            self.assertEqual(_session_paths(root, ["2026-09-23"]), [packed])
            with open_rollout(packed) as stream:
                events = list(stream)
            self.assertEqual(json.loads(events[-1])["payload"]["message"], "known earlier decision")
            self.assertTrue(_parse_session(packed, dates=["2026-09-23"])["in_requested_window"])
            self.assertFalse(_parse_session(packed, dates=["2026-09-25"])["in_requested_window"])
            plain = root / name
            plain.write_text("".join(events))
            os.utime(plain, ns=(packed.stat().st_mtime_ns, packed.stat().st_mtime_ns))
            self.assertEqual(_session_paths(root, ["2026-09-23"]), [plain])
            self.assertEqual(filename_session_id(packed), filename_session_id(plain))
            plain.write_text(events[0])
            self.assertEqual(_session_paths(root, ["2026-09-23"]), [packed])

    def test_corrupt_frame_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.jsonl.zst"
            path.write_bytes(self.frame[:16])
            with self.assertRaises((OSError, EOFError)):
                with open_rollout(path) as stream:
                    stream.read()


if __name__ == "__main__":
    unittest.main()
