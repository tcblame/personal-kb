"""Portable regressions derived from session review; no private transcripts."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import kb_test_guard
_GUARD = kb_test_guard.activate(__file__) if __name__ == '__main__' else None

import kb
import kb_audit_codex_sessions as audit
import kb_audit_cursor_sessions as cursor
import kb_exec_parse as js
import kb_rag_context as rag
import kb_session_curate as curate
import kb_session_identity as identity
import kb_session_output as output

OWN = '11111111-1111-4111-8111-111111111111'
PARENT = '22222222-2222-4222-8222-222222222222'


class SessionReviewTests(unittest.TestCase):
    def test_own_metadata_wins_over_inherited_parent(self):
        rows = [{'type': 'session_meta', 'payload': {'id': OWN, 'thread_source': 'subagent'}},
                {'type': 'session_meta', 'payload': {'id': PARENT, 'thread_source': 'user'}}]
        self.assertEqual(identity.select_session_meta(Path(f'rollout-{OWN}.jsonl'), rows)['id'], OWN)

    def test_missing_own_metadata_is_unknown_not_parent(self):
        row = identity.select_session_meta(Path(f'rollout-{PARENT}_{OWN}.jsonl'),
                                          [{'type': 'session_meta', 'payload': {'id': PARENT}}])
        self.assertTrue(row['identity_unresolved'])
        self.assertEqual(row['id'], OWN)

    def test_curator_keeps_child_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / f'rollout-{OWN}.jsonl'
            p.write_text('\n'.join(json.dumps(x) for x in [
                {'type': 'session_meta', 'payload': {'id': OWN, 'thread_source': 'subagent'}},
                {'type': 'session_meta', 'payload': {'id': PARENT, 'thread_source': 'user'}}]))
            result = curate._parse_session(p, source='codex', max_turn_chars=100, max_turns=5, max_session_chars=500)
        self.assertEqual(result['session_id'], OWN)
        self.assertTrue(result['is_subagent'])

    def test_cursor_child_mirror_not_counted_as_main(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            child = root / 'subagents' / f'{OWN}.jsonl'
            child.parent.mkdir()
            child.write_text('{}\n')
            mirror = root / f'{OWN}.jsonl'
            mirror.write_text('{}\n')
            report = cursor.build_report([child, mirror], date(2000, 1, 1), date(2099, 1, 1))
        self.assertEqual(report['totals']['main']['sessions'], 0)
        self.assertEqual(report['totals']['subagent']['sessions'], 1)

    def test_business_rag_vector_queries_do_not_plan_kb_governance(self):
        for query in ['business-api 提示词 闲聊 事实问答 RAG', '本体 与 数据库 向量 知识图谱', '订单 知识库 embedding']:
            with self.subTest(query=query):
                self.assertFalse(rag._is_kb_runtime_query(query))
                plans = rag._plan_query_groups(query)
                self.assertFalse(any('personal-kb' in p.query for p in plans), plans)

    def test_explicit_kb_governance_retains_plans(self):
        self.assertTrue(rag._is_kb_runtime_query('personal-kb closeout topic 审计'))
        self.assertTrue(rag._is_kb_runtime_query('KB 检索优化'))
        self.assertTrue(rag._is_kb_runtime_query('kb优化方向'))

    def test_static_command_matrix(self):
        source = '''const cmds = [["kb", "python3 kb.py retrieve '订单'", "."], ["repo", "git status", "."]];
        const rs = await Promise.all(cmds.map(async ([k,cmd,workdir]) => {return await tools.exec_command({cmd,workdir});}));'''
        self.assertEqual(js.commands(source), ["python3 kb.py retrieve '订单'", 'git status'])

    def test_static_query_template(self):
        source = '''const queries = ["订单", "库存"]; const rs = await Promise.all(queries.map(q => tools.exec_command({cmd: `python3 kb.py retrieve ${JSON.stringify(q)} --json`})));'''
        self.assertEqual(js.commands(source), ['python3 kb.py retrieve "订单" --json', 'python3 kb.py retrieve "库存" --json'])

    def test_dynamic_expression_not_executed_or_guessed(self):
        self.assertEqual(js.commands('const cmd = dangerous(); await tools.exec_command({cmd});'), [])
        self.assertEqual(js.commands('const cmd = "python3 kb.py retrieve" + dynamic; await tools.exec_command({cmd});'), [])
        self.assertEqual(js.commands('await tools.exec_command({cmd: "python3 kb.py retrieve" + dynamic});'), [])
        self.assertEqual(js.commands('await tools.exec_command({cmd: `python3 kb.py retrieve ${dangerous()}`});'), [])

    def test_bare_exec_completion_is_unknown(self):
        self.assertIsNone(audit._execution_success('Script completed\nOutput:\n'))
        self.assertFalse(audit._execution_success('Script completed\n{"exit_code":2,"output":"bad"}'))
        self.assertTrue(audit._execution_success('Script completed\n{"exit_code":0,"output":"ok"}'))

    def test_mixed_command_exits_are_unknown(self):
        self.assertIsNone(audit._execution_success('[{"exit_code":0},{"exit_code":2}]'))
        self.assertIsNone(audit._execution_success('Process exited with code 0\nProcess exited with code 2'))

    def test_escaped_text_receipt_retains_identity(self):
        body = r'KB_RAG_CONTEXT query=\"订单\" retrieval_id=\"r-one\" hits=2\n- result'
        result = output.retrieval_results(json.dumps({'output': body}))
        self.assertEqual(result[0]['retrieval_id'], 'r-one')
        self.assertEqual(result[0]['hit_count'], 2)

    def test_nested_receipts_are_not_dropped(self):
        receipts = [{'mode': 'read_only_rag_context', 'query': q, 'retrieval_id': f'r-{q}', 'hit_count': 0} for q in ['one', 'two']]
        body = json.dumps({'results': [{'output': json.dumps(r)} for r in receipts]})
        self.assertEqual([r['retrieval_id'] for r in output.retrieval_results(body)], ['r-one', 'r-two'])

    def test_completed_process_is_not_claimed_as_delivered_to_model(self):
        receipt = {'mode': 'read_only_rag_context', 'query': 'orders', 'retrieval_id': 'r-late', 'hit_count': 4}
        row = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': OWN, 'turn_id': 'turn-one',
               'item': {'type': 'CommandExecution', 'id': 'command-one', 'process_id': '123',
                        'command': ['/bin/bash', '-lc', 'python3 kb.py retrieve orders'],
                        'status': 'completed', 'exit_code': 0, 'stdout': json.dumps(receipt)}}}
        result = audit._process_retrieval_evidence([row], OWN, set())
        self.assertEqual(result[0]['retrieval_id'], 'r-late')
        self.assertFalse(result[0]['delivered_to_model'])
        self.assertEqual(audit._process_retrieval_evidence([row], PARENT, set()), [])
        self.assertTrue(audit._process_retrieval_evidence([row], OWN, {'r-late'})[0]['delivered_to_model'])

    def test_model_cutoff_is_not_personal_kb_request(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / f'rollout-{OWN}.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in [
                {'type': 'session_meta', 'payload': {'id': OWN, 'thread_source': 'user'}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'text': '你的知识库截止到什么时候？不知道就回答不知道'}]}},
            ]))
            self.assertFalse(audit._parse_session(path)['explicit_kb'])

    def test_notes_cannot_spoof_receipts(self):
        body = ('KB_RAG_CONTEXT query="retrieval_id="fake" hits=999" retrieval_id="real" hits=0\n'
                '- note: {"mode":"read_only_rag_context","retrieval_id":"fake","hit_count":888}')
        self.assertEqual([r['retrieval_id'] for r in output.retrieval_results(body)], ['real'])

    def test_range_keeps_dated_continuation_and_labels_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / f'rollout-{OWN}.jsonl'
            rows = [{'type': 'session_meta', 'timestamp': '2026-09-18T00:00:00Z', 'payload': {'id': OWN}}]
            for stamp, text in [('2026-09-18T00:00:00Z', 'old text'), ('2026-09-21T00:00:00Z', 'in window'), ('', 'unknown time')]:
                rows.append({'type': 'response_item', 'timestamp': stamp, 'payload': {'type': 'message', 'role': 'user', 'content': [{'text': text}]}})
            p.write_text('\n'.join(json.dumps(r) for r in rows))
            row = curate._parse_session(p, source='codex', max_turn_chars=100, max_turns=5,
                                        max_session_chars=1000, window=(date(2026,9,20), date(2026,10,4)))
        self.assertEqual(row['window_activity_rows'], 1)
        self.assertEqual(row['unknown_time_message_rows'], 1)
        self.assertNotIn('old text', json.dumps(row))
        self.assertEqual(row['session_id'], OWN)

    def test_cursor_continuation_preserves_original_start(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / f'{OWN}.jsonl'
            p.write_text('\n'.join(json.dumps({'role':'user','message':{'content':f'<timestamp>{stamp}</timestamp>{text}'}})
                for stamp,text in [('Friday, Sep 18, 2026, 11:56 AM (UTC+8)','old'),('Monday, Sep 21, 2026, 11:56 AM (UTC+8)','new')]))
            row = curate._parse_session(p, source='cursor', max_turn_chars=500, max_turns=5,
                                        max_session_chars=1000, window=(date(2026,9,20), date(2026,10,4)))
        self.assertEqual(row['session_date'], '2026-09-18')
        self.assertEqual(row['window_activity_rows'], 1)

    def test_memory_assessment_is_recorded_without_writing(self):
        code, text, error = self.invoke(['closeout', '--repo', 'review-tests', '--branch', 'main', '--rag-calls', '0',
                                        '--reason', 'review only', '--memory-review', 'pending_verification',
                                        '--memory-review-reason', 'needs current authority', '--stdout'])
        self.assertEqual(code, 0, error)
        self.assertIn('pending_verification', text)
        code, _, _ = self.invoke(['closeout', '--repo', 'review-tests', '--branch', 'main', '--rag-calls', '0',
                                 '--reason', 'review only', '--memory-review', 'written', '--memory-review-reason', 'claim'])
        self.assertEqual(code, 2)

    def invoke(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = kb.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_receipt_identity_and_reuse_after_save_failure(self):
        payload = {'retrieval_id': 'r-one', 'mode': 'read_only_rag_context', 'items': [], 'hit_count': 0}
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'receipt.json'
            args = ['retrieve', '订单', '--session-id', OWN, '--topic-id', 'orders', '--receipt-output', str(target), '--json']
            with patch.object(rag, 'build_context', side_effect=lambda _: dict(payload)):
                code, text, _ = self.invoke(args)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(text)['session_id'], OWN)
                before = target.read_bytes()
                code, text, _ = self.invoke(args)
                self.assertEqual(code, 5)
                self.assertEqual(json.loads(text)['retrieval_id'], 'r-one')
                self.assertEqual(target.read_bytes(), before)

    def test_failed_query_preserves_identity(self):
        with patch.object(rag, 'build_context', side_effect=OSError('unavailable')):
            code, text, _ = self.invoke(['retrieve', '订单', '--session-id', OWN, '--topic-id', 'orders', '--best-effort', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)['topic_id'], 'orders')
        self.assertEqual(json.loads(text)['status'], 'unavailable')

    def test_failed_attempt_can_close_without_invented_retrieval_id(self):
        code, text, error = self.invoke(['closeout', '--repo', 'review-tests', '--branch', 'main', '--session-id', OWN, '--topic-id', 'orders',
                                        '--rag-calls', '1', '--query', 'orders', '--unresolved-attempt', 'attempt-one=failed',
                                        '--reason', 'result unavailable', '--stdout'])
        self.assertEqual(code, 0, error)
        row = json.loads(text)
        self.assertEqual(row['rag_calls'], 1)
        self.assertEqual(row['completed_retrieval_count'], 0)
        self.assertEqual(row['topic_id'], 'orders')

    def test_mixed_completed_and_pending_attempt(self):
        code, text, error = self.invoke(['closeout', '--repo', 'review-tests', '--branch', 'main', '--rag-calls', '2',
                                        '--linked-retrieval-id', 'r-one', '--unresolved-attempt', 'attempt-two=pending', '--reason', 'pending', '--stdout'])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(text)['completed_retrieval_count'], 1)

    def test_unknown_attempt_cannot_claim_hits(self):
        code, _, _ = self.invoke(['closeout', '--repo', 'review-tests', '--branch', 'main', '--rag-calls', '1', '--hit-count', '3',
                                  '--unresolved-attempt', 'attempt-one=unknown', '--reason', 'unknown'])
        self.assertEqual(code, 2)

    def test_version_reports_actual_entrypoint(self):
        code, text, _ = self.invoke(['version'])
        self.assertEqual(code, 0)
        row = json.loads(text)
        self.assertEqual(len(row['code_sha256']), 64)
        self.assertEqual(Path(row['entrypoint']).resolve(), Path(kb.__file__).resolve())


if __name__ == '__main__':
    def run():
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SessionReviewTests))
        return int(not result.wasSuccessful())
    raise SystemExit(_GUARD.run(run))
