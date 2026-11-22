#!/usr/bin/env python3
"""Public, synthetic route/precision regressions; no held-out task data."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import kb_test_guard
GUARD=kb_test_guard.activate(__file__) if __name__=='__main__' else None
import kb


class RoutePrecision(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.authority=self.root/'current.py';self.authority.write_text('authority = True\n')
        self.config=self.root/'config.json'
        self.config.write_text(json.dumps({'storage':{'root':str(self.root/'data'),'records':'repos','runtime':'runtime','cache':'cache'}}))
        self.env=patch.dict(os.environ,{'PERSONAL_KB_ROOT':str(self.root/'data'),'PERSONAL_KB_CONFIG':str(self.config)})
        self.env.start()

    def tearDown(self):
        self.env.stop();self.temp.cleanup()

    def row(self,id,kind,title,triggers,story='Historical lead; check current authority.'):
        return {'id':id,'kind':kind,'repo':'widget_client','branch':'main','status':'current',
                'title':title,'story':story,'aliases':[],'trigger_terms':triggers,
                'source_paths':[str(self.authority)],'schema_version':2,
                'evidence_snapshots':[{'type':'file','source_path':str(self.authority),'path':str(self.authority),
                    'sha256':hashlib.sha256(self.authority.read_bytes()).hexdigest()}]}

    def write(self,rows):
        path=self.root/'data/repos/widget_client/main/kb.jsonl';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows));return path

    def retrieve(self,query,*options):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            code=kb.main(['retrieve',query,'--no-domain','--no-session-briefs','--json',*options])
        self.assertEqual(code,0)
        return json.loads(out.getvalue())

    def test_global_evidence_is_actionable_but_not_promoted_to_current_project(self):
        self.write([self.row('useful','pitfall','codec first PCM underflow',['codec','firstPCM','underflow'])])
        result=self.retrieve('codec firstPCM underflow','--global')
        self.assertEqual([r['entry_id'] for r in result['items']],['useful'])
        item=result['items'][0]
        self.assertTrue(item['summary']);self.assertEqual(item['source_paths'],[str(self.authority)])
        self.assertEqual(item['applicability_state'],'cross_project_review');self.assertTrue(item['cross_project'])
        self.assertNotIn('same_project',item)

    def test_scope_and_path_do_not_supply_descriptive_task_anchors(self):
        relevant=self.row('useful','pitfall','widget firstPCM underflow',['widget','firstPCM','underflow'])
        noise=self.row('noise','pitfall','widget deployment notes',['widget','container'],story='The old incident also mentioned firstPCM underflow')
        noise['source_paths']=[str(self.root/'firstPCM_underflow.py')]
        # Deliberately legacy: it must be filtered by topic before evidence.
        noise.pop('evidence_snapshots');noise.pop('schema_version')
        self.write([relevant,noise])
        result=self.retrieve('widget firstPCM underflow','--global')
        self.assertEqual([r['entry_id'] for r in result['items']],['useful'])

    def test_missing_workspace_bucket_discovers_corpus_without_index_or_writes(self):
        path=self.write([self.row('useful','issue','codec first PCM underflow',['codec','firstPCM','underflow'])])
        before=path.read_bytes()
        result=self.retrieve('codec firstPCM underflow','--repo','empty-workspace','--branch','main')
        self.assertEqual([r['entry_id'] for r in result['items']],['useful'])
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse((self.root/'data/cache').exists())

    def test_unrelated_query_stays_empty(self):
        self.write([self.row('other','pitfall','widget voice deployment',['widget','voice','deployment'])])
        result=self.retrieve('codex token pricing conversation','--global')
        self.assertEqual(result['items'],[])

    def test_focused_lookup_can_carry_only_the_named_object(self):
        self.write([self.row("unit", "experience", "lamp_controller calibration",
                             ["lamp_controller", "calibration"]),
                    self.row("other", "issue", "audio_mixer feedback", ["audio_mixer", "feedback"])])
        task = "lamp_controller calibration and audio_mixer feedback"
        for mode in ("--focus", "--action"):
            result = self.retrieve(task, mode, "calibration", "--scope-anchor", "device:lamp_controller")
            self.assertEqual(result["query"], "calibration lamp_controller")
            self.assertEqual([item["entry_id"] for item in result["items"]], ["unit"])
        with contextlib.redirect_stdout(io.StringIO()):
            code = kb.main(["retrieve", task, "--focus", "calibration", "--scope-anchor", "unknown_unit"])
        self.assertEqual(code, 2)

    def test_code_identifier_lookup_keeps_recall(self):
        self.write([self.row('exact','issue','configuration recurrence',['common_config_codex'])])
        result=self.retrieve('Codex provider template common_config_codex','--global')
        self.assertEqual([r['entry_id'] for r in result['items']],['exact'])

    def test_include_weak_remains_explicit_diagnostic_escape(self):
        self.write([self.row('lead','pitfall','widget notes',['widget'],story='firstPCM underflow seen before')])
        result=self.retrieve('widget firstPCM underflow','--global','--include-weak')
        self.assertEqual([r['entry_id'] for r in result['items']],['lead'])

    def test_long_query_rejects_product_and_generic_symptom_overlap(self):
        self.write([
            self.row('noise-a','pitfall','Nebula package download timeout',['Nebula','timeout','registry']),
            self.row('noise-b','issue','Nebula generation timeout',['Nebula','timeout','generation']),
            self.row('useful','pitfall','Nebula radio pairing timeout',['Nebula','radio','pairing','timeout']),
        ])
        result=self.retrieve('Nebula radio pairing channel timeout parameter validation','--global')
        self.assertEqual([r['entry_id'] for r in result['items']],['useful'])

    def test_long_query_preserves_precise_stored_phrase(self):
        entry=self.row('useful','pitfall','runtime controls',['runtime'])
        entry['aliases']=['短句追加确认词']
        self.write([entry])
        result=self.retrieve('Nebula 短句追加确认词 开关 默认 旧版本 兼容 保存','--global')
        self.assertEqual([r['entry_id'] for r in result['items']],['useful'])

    def test_locator_word_does_not_bypass_long_query_topic_gate(self):
        self.write([
            self.row('noise','pitfall','Vega voice configuration',['Vega','配置','voice']),
            self.row('useful','pitfall','Vega 容器 挂载 缓存 模型目录',['Vega','容器重建','挂载','缓存','模型路径']),
        ])
        result=self.retrieve('Vega 模型路径 容器重建 缓存 挂载 稳定 配置','--global')
        self.assertEqual([r['entry_id'] for r in result['items']],['useful'])

    def test_locator_keeps_exact_filename_evidence_in_long_query(self):
        self.write([self.row('useful','pitfall','runtime evidence',['runtime'])])
        result=self.retrieve('Vega 模型路径 容器重建 current.py 配置 核验','--global')
        self.assertEqual([r['entry_id'] for r in result['items']],['useful'])

    def test_scope_identifier_alone_does_not_admit_unrelated_rule(self):
        rule = self.row("rule", "requirement", "扫码后开锁", ["扫码", "开锁", "widget_client"])
        self.write([rule])
        result = self.retrieve("widget_client 停机 数据复制 超时 网络重试", "--global")
        self.assertEqual(result["items"], [])

    def test_two_short_terms_embedded_in_prose_recall_declared_topic(self):
        rule = self.row("rule", "requirement", "扫码后开锁", ["扫码", "开锁"])
        self.write([rule])
        result = self.retrieve("处理多个场次时应该在哪里扫码以及何时开锁", "--global")
        self.assertEqual([item["entry_id"] for item in result["items"]], ["rule"])

    def test_brief_keeps_candidate_order_warnings_and_closeout_context(self):
        self.write([self.row("useful", "pitfall", "codec first PCM underflow", ["codec", "firstPCM", "underflow"])])
        receipt = self.root / "receipt.json"
        brief = self.retrieve("codec firstPCM underflow", "--global", "--brief",
                              "--receipt-output", str(receipt), "--session-id", "s1", "--topic-id", "t1")
        full = json.loads(receipt.read_text())
        for field in ("retrieval_id", "query", "session_id", "topic_id", "hit_count"):
            self.assertEqual(brief[field], full[field])
        self.assertEqual(len(brief["items"]), 1)
        for field in ("entry_id", "kind", "trust_tier", "freshness_state", "applicability_state",
                      "summary", "warning"):
            self.assertEqual(brief["items"][0][field], full["items"][0][field])
        self.assertNotIn("matched_anchors", brief["items"][0])
        self.assertIn("matched_anchors", full["items"][0])
        obj = {"closeout_id": "brief-one", "retrieval_snapshot": brief, "miss_reason": "no_verified_hit",
               "memory_review": {"disposition": "not_reusable", "reason": "test fixture"}}
        code = kb.main(["closeout", "--json", json.dumps(obj), "--require-complete-context"])
        self.assertEqual(code, 0)

    def test_brief_distinguishes_empty_and_invalid_query_and_receipt_failure(self):
        self.write([])
        empty = self.retrieve("nothing relevant", "--brief")
        self.assertEqual(empty["items"], [])
        for options, expected in [(["--scope-anchor", "unrelated"], 2),
                                  (["--receipt-output", str(self.authority)], 5)]:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = kb.main(["retrieve", "nothing relevant", "--brief", *options])
            data = json.loads(out.getvalue())
            self.assertEqual(code, expected)
            self.assertIn(data["status"], ("error", "partial_failure"))
            self.assertTrue(data["retrieval_id"])


def main():
    result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(RoutePrecision))
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(GUARD.run(main))
