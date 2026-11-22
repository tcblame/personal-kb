#!/usr/bin/env python3
"""Regressions for real completion evidence, identity and continued chats."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
import contextlib
import io
from unittest.mock import patch

import kb_test_guard

_TEST_GUARD = kb_test_guard.activate(__file__) if __name__ == "__main__" else None

import kb_audit_codex_sessions as audit
import kb_record_codex_effectiveness as effectiveness
import kb
import kb_evidence
from kb_runtime import runtime_session_id


class RecentLifecycleTests(unittest.TestCase):
    session = "11111111-2222-7333-8444-555555555555"

    def parse(self, rows, *, dates=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"rollout-2026-01-01T00-00-00-{self.session}.jsonl"
            rows = [{"type": "session_meta", "payload": {"id": self.session, "source": "cli"}}, *rows]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            return audit._parse_session(path, dates=dates, timezone_name="Asia/Shanghai"), effectiveness._parse_session(path)

    def call(self, command):
        return {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "outer-call",
                "input": 'text(await tools.exec_command({cmd:' + json.dumps(command) + '}));'}}

    def completion(self, command, output, code=0, **extra):
        return {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": self.session,
                "item": {"type": "CommandExecution", "id": "shell-one", "command": ["/bin/bash", "-lc", command],
                         "aggregated_output": output, "exit_code": code, "status": "completed"}, **extra}}

    def test_shell_completion_recovers_wrapper_result_without_claiming_delivery(self):
        command = "python3 '/tmp/kb.py' retrieve 'prior decision' --json"
        result = json.dumps({"mode": "read_only_rag_context", "retrieval_id": "real-receipt", "query": "prior decision", "items": []})
        completion = self.completion(command, result)
        a, e = self.parse([self.call(command), completion, completion])
        self.assertEqual(a["rag_or_search"], 1)
        self.assertEqual(a["retrieval_events"][0]["retrieval_id"], "real-receipt")
        self.assertEqual(a["result_not_delivered_count"], 1)
        self.assertEqual(e["kb_call_counts"].get("kb_rag_context"), 1)

    def test_short_circuit_is_not_a_successful_retrieval(self):
        command = "python3 /tmp/kb.py version && python3 /tmp/kb.py retrieve 'prior decision' --json"
        a, e = self.parse([self.call(command), self.completion(command, "unknown daily command: version", 2)])
        self.assertEqual(a["rag_or_search"], 0)
        self.assertEqual(e["kb_call_counts"].get("kb_rag_context", 0), 0)

    def test_parent_completion_cannot_supply_child_receipt(self):
        command = "python3 /tmp/kb.py retrieve 'prior decision' --json"
        result = json.dumps({"mode": "read_only_rag_context", "retrieval_id": "foreign-receipt", "items": []})
        a, e = self.parse([self.call(command), self.completion(command, result, thread_id="another-thread")])
        self.assertEqual(a["rag_or_search"], 0)
        self.assertEqual(e["kb_call_counts"].get("kb_rag_context", 0), 0)

    def test_timestamp_window_keeps_continuation_and_excludes_old_turn(self):
        command = "python3 /tmp/kb.py retrieve 'prior decision' --json"
        old = self.call(command)
        old["timestamp"] = "2026-10-20T01:00:00Z"
        recent = {"type": "response_item", "timestamp": "2026-10-22T17:00:00Z",
                  "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "查一下之前的决定"}]}}
        a, _ = self.parse([old, recent], dates=["2026-10-23"])
        self.assertTrue(a["in_requested_window"])
        self.assertEqual(a["rag_request_count"], 0)
        self.assertEqual(a["window_unknown_timestamp_rows"], 0)

    def test_older_directory_is_a_candidate_for_recent_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026/01/01/rollout-old.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text('{}\n')
            self.assertIn(path, audit._session_paths(Path(tmp), ["2026-10-23"]))

    def test_current_environment_is_used_but_explicit_identity_wins(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "current-thread", "CODEX_SESSION_ID": "older-alias"}):
            self.assertEqual(runtime_session_id(), "current-thread")
            self.assertEqual(runtime_session_id("explicit-child"), "explicit-child")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CODEX_SESSION_ID": ""}):
            self.assertEqual(runtime_session_id(fallback="snapshot-session"), "snapshot-session")

    def test_response_user_intent_is_observed_without_developer_prompt_noise(self):
        a, e = self.parse([
            {"type": "response_item", "payload": {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "历史需求不等于必须检索；不要将指令文本当用户请求"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "查一下之前的需求决定"}]}}])
        self.assertEqual(e["user_message_count"], 1)
        self.assertIn("之前的需求决定", e["user_excerpt"])
        self.assertNotIn("指令文本", e["user_excerpt"])

    def test_natural_chinese_questions_recall_explicit_stored_phrases(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            config = directory / "config.json"
            config.write_text(json.dumps({"storage": {"root": str(directory / 'data'), "records": "records", "runtime": "runtime", "cache": "cache"}}))
            evidence = directory / 'authority.txt'
            evidence.write_text('每日学习十点，6+2+2；结束问卷清除进度，暂停可以恢复。')
            entries = []
            for entry_id, title, phrases in [('daily', '每日学习课程数量分配', ['每日学习', '每日知识点']),
                                              ('stop', '结束问卷与暂停的区别', ['结束问卷', '问卷暂停'])]:
                entry = {'id': entry_id, 'ts': '2026-10-28T00:00:00+00:00', 'kind': 'requirement',
                         'title': title, 'story': evidence.read_text(), 'repo': 'demo', 'branch': 'main',
                         'status': 'current', 'aliases': phrases, 'trigger_terms': phrases,
                         'source_paths': [str(evidence)]}
                entry['evidence_snapshots'] = kb_evidence.capture_evidence_snapshots(entry, directory)
                entries.append(entry)
            path = directory / 'data/records/demo/main/kb.jsonl'
            path.parent.mkdir(parents=True)
            path.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in entries))
            for query, expected in [('每日学习每天有几个知识点，如何分配', 'daily'),
                                    ('我想了解每日学习的数量与安排', 'daily'),
                                    ('结束问卷后还能恢复吗，暂停与结束有什么区别', 'stop'),
                                    ('请解释结束问卷之后的进度处理', 'stop')]:
                with self.subTest(query=query), patch.dict(os.environ, {'PERSONAL_KB_ROOT': str(directory/'data'), 'PERSONAL_KB_CONFIG': str(config)}):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                        code = kb.main(['retrieve', query, '--global', '--no-domain', '--no-session-briefs', '--json'])
                    self.assertEqual(code, 0)
                    self.assertIn(expected, [i['entry_id'] for i in json.loads(output.getvalue())['items']])


def main():
    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(RecentLifecycleTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(_TEST_GUARD.run(main))
