#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import kb_test_guard

_TEST_GUARD = kb_test_guard.activate(__file__) if __name__ == "__main__" else None

import kb_eval_preflight as preflight


def test_shared_command_contract_splits_chained_wrappers() -> None:
    command = (
        "python3 /tmp/kb.py retrieve 'first && quoted' --json && "
        "python3 /tmp/kb.py retrieve second --json"
    )
    segments = preflight.command_contract.split_shell_commands(command)
    assert len(segments) == 2
    assert all(
        preflight.command_contract.direct_or_wrapper_scripts(segment) == ["kb_rag_context"]
        for segment in segments
    )


def test_current_evidence_skips_retrieval() -> None:
    prediction = preflight.predict_preflight(
        "当前 500 异常栈已经足够，请直接根据当前日志定位根因。"
    )
    assert prediction["action"] == "skip"
    assert prediction["should_retrieve"] is False


def test_runtime_audit_reads_raw_evidence_without_rag() -> None:
    prediction = preflight.predict_preflight(
        "多 Agent 审计这两天 Personal KB 的运行效果和历史会话。"
    )
    assert prediction["action"] == "audit_readonly"
    assert prediction["phase"] == "audit"
    assert prediction["should_retrieve"] is False


def test_prior_decision_retrieves_once() -> None:
    prediction = preflight.predict_preflight("在线考试要不要单列？按上次确认的决定来。")
    assert prediction["action"] == "retrieve"
    assert prediction["kb_owner"] == "parent"
    assert prediction["retrieval_plan"]["initial_retrieval_count"] == 1
    assert prediction["retrieval_plan"]["route"] == "parent_direct"
    assert prediction["retrieval_plan"]["retrieval_id_required"] is True
    assert prediction["handoff_plan"]["required"] is False
    assert "上次确认" in prediction["retrieval_plan"]["query"]
    assert prediction["lifecycle_plan"]["adoption"] == "undetermined"


def test_skill_update_boundary() -> None:
    generic = preflight.predict_preflight("更新所有 Skill 和 MCP 的说明文件，统一标题格式。")
    durable = preflight.predict_preflight(
        "更新所有 Skill，做成命令并记录，这样下次我一说你就直接更新。"
    )
    assert generic["action"] == "skip"
    assert durable["action"] == "maintain"
    assert durable["should_retrieve"] is False


def test_ccs_update_routes_to_historical_safe_update() -> None:
    prediction = preflight.predict_preflight("更新 ccs")
    assert prediction["action"] == "retrieve"
    assert prediction["kb_owner"] == "parent"
    assert prediction["retrieval_plan"]["route"] == "parent_direct"
    assert prediction["retrieval_plan"]["initial_retrieval_count"] == 1
    assert "durable_safe_operation_rule" in prediction["reason_codes"]

    current_diagnostic = preflight.predict_preflight("cc-switch 已关闭，检查当前进程和日志")
    assert current_diagnostic["action"] == "skip"
    assert current_diagnostic["reason_codes"] == ["no_cross_session_dependency"]

    for prompt in (
        "检查 cc-switch 是否已经关闭",
        "cc-switch 关闭了，帮我看看当前进程",
        "看看 cc-switch 为什么退出了",
    ):
        prediction = preflight.predict_preflight(prompt)
        assert prediction["action"] == "skip", prompt
        assert prediction["should_retrieve"] is False, prompt


def test_cc_switch_high_risk_config_requires_history_unless_current_evidence() -> None:
    prediction = preflight.predict_preflight(
        "cc-switch 的 guard 被改回 127.0.0.1，为什么？之前有没有确认过安全规则？"
    )
    assert prediction["action"] == "retrieve"
    assert prediction["kb_owner"] == "parent"
    assert prediction["retrieval_plan"]["route"] == "parent_direct"
    assert "high_risk_cc_switch_config" in prediction["reason_codes"]

    local_address = preflight.predict_preflight(
        "把 cc-switch 改成本地地址前，先查以前的高危操作规则。"
    )
    assert local_address["action"] == "retrieve"
    assert "high_risk_cc_switch_config" in local_address["reason_codes"]

    current = preflight.predict_preflight(
        "当前 cc-switch 配置已经足够，直接查看 127.0.0.1 和 guard 的当前配置。"
    )
    assert current["action"] == "skip"
    assert current["should_retrieve"] is False


def test_mixed_topics_keep_independent_retrieval_budgets() -> None:
    result = preflight.predict_request_preflights(
        "用之前写过的安全命令更新 ccs，然后按上次确认的决定修改我的简历",
        # This legacy task-wide flag must not suppress either fresh topic.
        {"initial_retrieval_already_done": True},
    )
    assert result["topic_count"] == 2
    assert result["initial_retrieval_count"] == 2
    first, second = [item["prediction"] for item in result["topics"]]
    assert first["action"] == "retrieve"
    assert "ccs" in first["retrieval_plan"]["query"].lower()
    assert "简历" not in first["retrieval_plan"]["query"]
    assert second["action"] == "retrieve"
    assert "简历" in second["retrieval_plan"]["query"]
    assert "ccs" not in second["retrieval_plan"]["query"].lower()

    resumed = preflight.predict_topic_preflights(
        [
            {"id": "cc-switch-update", "prompt": "更新 ccs"},
            {"id": "resume-update", "prompt": "按上次确认的决定修改我的简历"},
        ],
        {"initialized_topic_ids": ["resume-update"]},
    )
    resumed_by_id = {item["topic_id"]: item["prediction"] for item in resumed["topics"]}
    assert resumed["initial_retrieval_count"] == 1
    assert resumed_by_id["cc-switch-update"]["action"] == "retrieve"
    assert resumed_by_id["resume-update"]["action"] == "skip"


def test_ordinary_subagent_reuses_parent_hints() -> None:
    prediction = preflight.predict_preflight(
        """核实父会话给出的历史线索。
Treat KB hints as historical leads and verify them against current evidence.
Do not run personal-kb scripts. Use only KB hints provided by the parent; the parent owns retrieval routing and final hint selection, adoption, heating, writes, and closeout.""",
        {
            "agent_role": "ordinary_subagent",
            "parent_provided_kb_hints": True,
            "cross_session_dependency": True,
        },
    )
    assert prediction["action"] == "reuse_parent_hints"
    assert prediction["kb_owner"] == "parent"
    assert prediction["should_retrieve"] is False
    assert prediction["retrieval_plan"]["route"] == "reuse_parent_hints"
    assert prediction["retrieval_plan"]["initial_retrieval_count"] == 0
    assert prediction["handoff_plan"]["required"] is False
    assert prediction["lifecycle_plan"]["current_agent_may_closeout"] is False
    assert prediction["lifecycle_plan"]["current_agent_may_heat"] is False
    assert prediction["lifecycle_plan"]["current_agent_may_write"] is False


def test_ordinary_subagent_honors_full_kb_forbid_despite_script_guard() -> None:
    prediction = preflight.predict_preflight(
        """禁止使用 Personal KB，也不要复用父会话历史线索。
Do not run personal-kb scripts. Use only KB hints provided by the parent.""",
        {
            "agent_role": "ordinary_subagent",
            "parent_provided_kb_hints": True,
            "cross_session_dependency": True,
        },
    )
    assert prediction["action"] == "skip"
    assert prediction["reason_codes"] == ["explicit_kb_forbidden"]
    assert prediction["retrieval_plan"]["route"] == "none"

    english_prediction = preflight.predict_preflight(
        "Do not run personal-kb scripts and do not use parent KB hints.",
        {
            "agent_role": "ordinary_subagent",
            "parent_provided_kb_hints": True,
            "cross_session_dependency": True,
        },
    )
    assert english_prediction["action"] == "skip"
    assert english_prediction["reason_codes"] == ["explicit_kb_forbidden"]
    assert english_prediction["retrieval_plan"]["route"] == "none"


def test_parent_delegates_broad_retrieval_to_readonly_scout() -> None:
    prediction = preflight.predict_preflight(
        "整理以前多个项目和历史阶段确认过的路径映射，供两个 worker 共用。",
        {
            "cross_session_dependency": True,
            "retrieval_scope": "broad",
            "dependent_worker_count": 2,
        },
    )
    assert prediction["action"] == "retrieve"
    assert prediction["kb_owner"] == "kb_scout"
    assert prediction["retrieval_plan"]["route"] == "kb_scout"
    assert prediction["handoff_plan"] == {
        "required": True,
        "direction": "scout_to_parent",
        "schema": preflight.SCOUT_BRIEF_SCHEMA,
        "parent_followup_required": False,
    }
    assert prediction["lifecycle_plan"]["parent_closeout_required"] is True
    assert prediction["lifecycle_plan"]["current_agent_may_closeout"] is True

    scout = preflight.predict_preflight(
        "作为 KB scout，只读检索以前确认的多个项目路径映射。",
        {"agent_role": "kb_scout", "cross_session_dependency": True},
    )
    assert scout["kb_owner"] == "kb_scout"
    assert scout["lifecycle_plan"]["closeout_if_retrieved"] is False
    assert scout["lifecycle_plan"]["current_agent_may_closeout"] is False
    assert scout["lifecycle_plan"]["current_agent_may_heat"] is False
    assert scout["lifecycle_plan"]["current_agent_may_write"] is False


def test_parent_reuses_scout_brief_without_retrieving_again() -> None:
    prediction = preflight.predict_preflight(
        "继续按 scout 返回的历史候选核实当前文件。",
        {
            "scout_brief_received": True,
            "initial_retrieval_already_done": True,
            "cross_session_dependency": True,
        },
    )
    assert prediction["action"] == "reuse_parent_hints"
    assert prediction["should_retrieve"] is False
    assert prediction["kb_owner"] == "parent"
    assert prediction["retrieval_plan"]["route"] == "reuse_parent_hints"


def test_worker_requests_parent_followup_instead_of_self_retrieval() -> None:
    prediction = preflight.predict_preflight(
        "现有线索与当前代码冲突，需要查新发现的旧项目别名。",
        {
            "agent_role": "ordinary_subagent",
            "parent_provided_kb_hints": True,
            "new_history_anchor": True,
        },
    )
    assert prediction["action"] == "skip"
    assert prediction["should_retrieve"] is False
    assert prediction["kb_owner"] == "parent"
    assert prediction["retrieval_plan"]["route"] == "request_parent_followup"
    assert prediction["handoff_plan"]["parent_followup_required"] is True

    without_hints = preflight.predict_preflight(
        "请你自己去 Personal KB 查以前确认的部署路径。",
        {"agent_role": "ordinary_subagent"},
    )
    assert without_hints["action"] == "skip"
    assert without_hints["should_retrieve"] is False
    assert without_hints["kb_owner"] == "parent"
    assert without_hints["retrieval_plan"]["route"] == "request_parent_followup"


def test_kb_scout_hands_maintenance_back_to_parent() -> None:
    prediction = preflight.predict_preflight(
        "Personal KB 检索完成，请写入、加热并 closeout。",
        {"agent_role": "kb_scout"},
    )
    assert prediction["action"] == "maintain"
    assert prediction["kb_owner"] == "parent"
    assert prediction["lifecycle_plan"]["current_agent_may_closeout"] is False
    assert prediction["lifecycle_plan"]["current_agent_may_heat"] is False
    assert prediction["lifecycle_plan"]["current_agent_may_write"] is False
    assert prediction["handoff_plan"]["direction"] == "scout_to_parent"


def test_duplicate_logical_task_does_not_repeat_initial_rag() -> None:
    prediction = preflight.predict_preflight(
        "按上次决定继续处理。",
        {"duplicate_logical_task": True, "initial_retrieval_already_done": True},
    )
    assert prediction["action"] == "skip"
    assert prediction["retrieval_plan"]["initial_retrieval_count"] == 0


def test_initialized_task_allows_maintenance_and_new_history_anchor() -> None:
    closeout = preflight.predict_preflight(
        "Personal KB 已检索完成，请维护本轮 closeout。",
        {"initial_retrieval_already_done": True},
    )
    expansion = preflight.predict_preflight(
        "刚才零命中，请扩大范围查以前的启动故障。",
        {
            "initial_retrieval_already_done": True,
            "retrieval_expansion_required": True,
            "new_history_anchor": True,
        },
    )
    assert closeout["action"] == "maintain"
    assert expansion["action"] == "retrieve"
    assert expansion["kb_owner"] == "kb_scout"
    current_durable = preflight.predict_preflight(
        "请记住这个已经由当前代码验证的稳定规则。",
        {"current_evidence_fully_answers_task": True},
    )
    assert current_durable["action"] == "maintain"

    evidence = {"current_evidence_fully_answers_task": True}
    assert preflight.predict_preflight("请记录刚核实的这个新规则，下次沿用。", evidence)["action"] == "maintain"
    assert preflight.predict_preflight("请记录本次结果，下次是否沿用还待讨论。", evidence)["action"] == "skip"


def test_hard_negative_and_english_history_boundaries() -> None:
    assert preflight.predict_preflight("给订单历史记录接口加分页，只看当前代码。")["action"] == "skip"
    assert preflight.predict_preflight("审计当前产品知识库的运行效果，只看项目日志。")["action"] == "skip"
    assert preflight.predict_preflight(
        "不要参考上次结论，当前异常栈已经足够，请直接定位。"
    )["action"] == "skip"
    assert preflight.predict_preflight("从当前 Git 配置整理 repo、目录和分支映射。")["action"] == "skip"
    assert preflight.predict_preflight(
        "Use the decision we agreed on last time for the resume structure."
    )["action"] == "retrieve"


def test_history_wording_triggers_retrieve_and_current_text_stays_skip() -> None:
    high_confidence_cases = [
        "找一下，预约工具应该有排队界面的设计稿",
        "为什么每次都要重新安装这个示例客户端才打得开？",
        "分析最近三次关于排班的对话",
        "昨天的会话确认过排班规则，请查一下",
        "旧记录里有没有两个示例插件的合并决定",
    ]
    for prompt in high_confidence_cases:
        prediction = preflight.predict_preflight(prompt)
        assert prediction["action"] == "retrieve", prompt
        assert prediction["should_retrieve"] is True, prompt
        assert prediction["retrieval_plan"]["route"] == "parent_direct", prompt
        assert prediction["retrieval_plan"]["initial_retrieval_count"] == 1, prompt
        assert "recent_history_reference" in prediction["reason_codes"], prompt

    ambiguous = preflight.predict_preflight(
        "帮我分析个人优势，看简历怎么改，但当前简历文件足够，允许跳过"
    )
    assert ambiguous["action"] == "skip"
    assert ambiguous["should_retrieve"] is False

    resume_copy = (
        "示例简历中的个人优势写了两段。第一段讲如何给虚构的借阅服务编写接口、排查重复预约。"
        "第二段讲如何把演示用的消息任务移到后台并补测试。"
        "请把这两段改写成一条项目经历，删掉个人优势这个小标题。"
    )
    rewrite = preflight.predict_preflight(resume_copy)
    assert rewrite["action"] == "skip"
    assert rewrite["should_retrieve"] is False
    assert "explicit_history_retrieval" not in rewrite["reason_codes"]

    real_audit = preflight.predict_preflight(
        "回查这两天的历史对话，审计 Personal KB 是否在合适的任务里被采用。"
    )
    assert real_audit["action"] == "audit_readonly"
    assert real_audit["should_retrieve"] is False


def test_gold_cases_pass_routing_gate() -> None:
    eval_dir = Path(__file__).resolve().parents[1] / "references" / "evals"
    _, inputs = preflight._read_json_or_jsonl(eval_dir / "runtime-preflight-cases.json")
    _, gold = preflight._read_json_or_jsonl(eval_dir / "runtime-preflight-gold.json")
    cases = preflight.merge_case_inputs_and_gold(inputs, gold)
    metrics, details = preflight.evaluate_cases(cases)
    assert metrics["phase_accuracy"] == 1.0
    assert metrics["retrieve_accuracy"] == 1.0
    assert metrics["action_accuracy"] == 1.0
    assert metrics["action_owner_accuracy"] == 1.0
    assert metrics["coordination_accuracy"] == 1.0
    assert metrics["false_positive_count"] == 0
    assert metrics["false_negative_count"] == 0
    assert metrics["query_anchor_failure_count"] == 0
    assert metrics["topic_preflight_case_total"] == 1
    assert metrics["topic_preflight_accuracy"] == 1.0
    mixed = next(
        item for item in details if item["case_id"] == "mixed-ccs-update-and-resume-history"
    )
    assert mixed["checks"]["topic_preflight"]["pass"] is True
    assert mixed["request_prediction"]["topic_count"] == 2
    assert mixed["request_prediction"]["initial_retrieval_count"] == 2


def _write_fake_rag(
    path: Path,
    *,
    mutate_snapshot: bool,
    production_to_mutate: Path | None = None,
    sleep_seconds: float = 0.0,
) -> None:
    mutation = "\nroot.joinpath('repos/demo/main/kb.jsonl').write_text('changed\\n')" if mutate_snapshot else ""
    production_mutation = (
        f"\nPath({str(production_to_mutate)!r}).write_text('production changed\\n')"
        if production_to_mutate
        else ""
    )
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, time\n"
        "from pathlib import Path\n"
        "root = Path(os.environ['PERSONAL_KB_ROOT'])"
        f"{mutation}\n"
        f"{production_mutation}\n"
        f"time.sleep({sleep_seconds!r})\n"
        "print(json.dumps({'retrieval_id': 'fixture-retrieval', 'hit_count': 1, "
        "'rejected_weak_count': 0, 'query_groups': [], "
        "'mode': 'read_only_rag_context', "
        "'items': [{'entry_id': 'entry-1', 'title': 'DemoShelf repository map', "
        "'summary': 'DemoShelf path mapping'}]}))\n",
        encoding="utf-8",
    )


def _retrieval_fixture(root: Path) -> tuple[list[dict], list[dict]]:
    case = {
        "id": "fixture-retrieve",
        "prompt": "找以前确认的 DemoShelf 路径映射。",
        "expected": {
            "action": "retrieve",
            "phase": "retrieve",
            "should_retrieve": True,
            "kb_owner": "parent",
        },
        "retrieval_expectation": {
            "allow_zero_hits": False,
            "min_hits": 1,
            "must_find_any": ["entry-1"],
            "relevance_anchor_any": ["demoshelf", "mapping"],
            "max_irrelevant_rate": 0.0,
        },
    }
    metrics, details = preflight.evaluate_cases([case])
    assert metrics["retrieve_accuracy"] == 1.0
    return [case], details


def test_rag_preflight_uses_copy_and_preserves_production() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        production = root / "personal-kb"
        kb_file = production / "repos" / "demo" / "main" / "kb.jsonl"
        kb_file.parent.mkdir(parents=True)
        kb_file.write_text('{"id":"entry-1"}\n', encoding="utf-8")
        before = kb_file.read_bytes()
        fake_rag = root / "fake_rag.py"
        _write_fake_rag(fake_rag, mutate_snapshot=False)
        cases, details = _retrieval_fixture(root)

        metrics, results, safety = preflight.run_retrieval_preflight(
            cases,
            production_root=production,
            rag_script=fake_rag,
            cwd=root,
        )

        assert metrics["retrieval_case_pass_rate"] == 1.0
        assert metrics["safety_file_changes"] == 0
        assert results[0]["retrieval_check"]["pass"] is True
        assert safety == []
        assert kb_file.read_bytes() == before


def test_snapshot_mutation_is_detected_without_touching_production() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        production = root / "personal-kb"
        kb_file = production / "repos" / "demo" / "main" / "kb.jsonl"
        kb_file.parent.mkdir(parents=True)
        kb_file.write_text('{"id":"entry-1"}\n', encoding="utf-8")
        before = kb_file.read_bytes()
        fake_rag = root / "mutating_rag.py"
        _write_fake_rag(fake_rag, mutate_snapshot=True)
        cases, details = _retrieval_fixture(root)

        metrics, _, safety = preflight.run_retrieval_preflight(
            cases,
            production_root=production,
            rag_script=fake_rag,
            cwd=root,
        )

        assert metrics["safety_file_changes"] == 1
        assert safety == ["snapshot:repos/demo/main/kb.jsonl"]
        assert kb_file.read_bytes() == before


def test_direct_production_mutation_is_detected_but_not_rolled_back() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        production = root / "personal-kb"
        kb_file = production / "repos" / "demo" / "main" / "kb.jsonl"
        kb_file.parent.mkdir(parents=True)
        kb_file.write_text('{"id":"entry-1"}\n', encoding="utf-8")
        fake_rag = root / "production_mutator.py"
        _write_fake_rag(
            fake_rag,
            mutate_snapshot=False,
            production_to_mutate=kb_file,
        )
        cases, details = _retrieval_fixture(root)

        metrics, _, safety = preflight.run_retrieval_preflight(
            cases,
            production_root=production,
            rag_script=fake_rag,
            cwd=root,
        )

        assert metrics["safety_file_changes"] == 1
        assert safety == ["production:repos/demo/main/kb.jsonl"]
        assert kb_file.read_text(encoding="utf-8") == "production changed\n"


def test_rag_timeout_fails_fast() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        production = root / "personal-kb"
        kb_file = production / "repos" / "demo" / "main" / "kb.jsonl"
        kb_file.parent.mkdir(parents=True)
        kb_file.write_text('{"id":"entry-1"}\n', encoding="utf-8")
        fake_rag = root / "slow_rag.py"
        _write_fake_rag(fake_rag, mutate_snapshot=False, sleep_seconds=0.2)
        cases, details = _retrieval_fixture(root)

        metrics, results, safety = preflight.run_retrieval_preflight(
            cases,
            production_root=production,
            rag_script=fake_rag,
            cwd=root,
            timeout_seconds=0.01,
        )

        assert metrics["retrieval_case_pass_rate"] == 0.0
        assert results[0]["observation"]["exit_code"] == 124
        assert results[0]["observation"]["parse_error"] == "timeout"
        assert safety == []


def test_rag_runner_rejects_empty_or_nonobject_stdout() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        empty_script = root / "empty.py"
        array_script = root / "array.py"
        empty_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        array_script.write_text("#!/usr/bin/env python3\nprint('[]')\n", encoding="utf-8")
        plan = {"limit": 1, "repo": "", "branch": "", "global": False}

        empty = preflight._run_rag_query(
            "query",
            rag_script=empty_script,
            kb_root=root,
            plan=plan,
            cwd=root,
        )
        array = preflight._run_rag_query(
            "query",
            rag_script=array_script,
            kb_root=root,
            plan=plan,
            cwd=root,
        )

        assert empty["parse_error"] == "empty RAG stdout"
        assert array["parse_error"] == "RAG JSON must be an object"


def test_history_replay_flags_possible_duplicate_without_suppressing_it() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        session_path = root / "fixture.jsonl"
        rows = [
            {"type": "session_meta", "payload": {"id": "session-1", "thread_source": "user"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
            {
                "type": "event_msg",
                "timestamp": "2026-07-15T10:00:00Z",
                "payload": {"type": "user_message", "turn_id": "turn-1", "message": "按上次决定处理。"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "turn_id": "turn-1",
                    "arguments": json.dumps({"cmd": "python3 kb_rag_context.py '上次决定' --json"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "Script completed\n{}",
                },
            },
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-2"}},
            {
                "type": "event_msg",
                "timestamp": "2026-07-15T10:05:00Z",
                "payload": {"type": "user_message", "turn_id": "turn-2", "message": "按上次决定处理。"},
            },
        ]
        session_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

        _, turns, stats = preflight._historical_turns(session_path)

        assert len(turns) == 2
        assert turns[0]["observed_retrieval_attempts"] == 1
        assert turns[0]["prediction"]["action"] == "retrieve"
        assert turns[1]["prediction"]["action"] == "retrieve"
        assert turns[1]["possible_duplicate_prompt"] is True
        assert turns[1]["comparison_eligible"] is False
        assert stats["unattributed_retrieval_attempts"] == 0


def test_history_custom_exec_failure_is_an_attempt_not_a_success() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "fixture.jsonl"
        source = (
            'const r = await tools.exec_command({cmd: "python3 kb_rag_context.py \\\"old issue\\\" --json"}); '
            "text(r.output);"
        )
        rows = [
            {"type": "session_meta", "payload": {"id": "session-2", "thread_source": "user"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "turn_id": "turn-1", "message": "查以前的 issue。"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "call-1",
                    "turn_id": "turn-1",
                    "input": source,
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-1",
                    "output": "Script completed\n{\"exit_code\":1}",
                },
            },
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

        _, segments, _ = preflight._historical_turns(path)

        assert segments[0]["observed_retrieval_attempts"] == 1
        assert segments[0]["observed_failed_attempts"] == 1
        assert segments[0]["observed_confirmed_success_attempts"] == 0
    assert preflight._historical_execution_status(
        "custom_tool_call", "Script completed\nOutput:\n{}"
    ) == "unknown"


def test_history_multi_message_turn_is_excluded_from_efficiency_comparison() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "fixture.jsonl"
        rows = [
            {"type": "session_meta", "payload": {"id": "session-3", "thread_source": "user"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "turn_id": "turn-1", "message": "更新所有 skill 并记住。"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "turn_id": "turn-1", "message": "再记住退出前延迟重启。"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "turn_id": "turn-1",
                    "arguments": json.dumps({"cmd": "python3 kb_rag_context.py 'cc-switch restart'"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "Script completed\n{}",
                },
            },
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

        _, segments, _ = preflight._historical_turns(path)

        assert len(segments) == 2
        assert all(segment["multi_message_turn"] for segment in segments)
        assert all(not segment["comparison_eligible"] for segment in segments)
        assert segments[0]["observed_retrieval_attempts"] == 0
        assert segments[1]["observed_retrieval_attempts"] == 1


def test_case_schema_rejects_empty_duplicate_and_string_boolean() -> None:
    invalid_sets = [
        [],
        [
            {"id": "same", "prompt": "a", "expected": {"action": "skip", "phase": "skip"}},
            {"id": "same", "prompt": "b", "expected": {"action": "skip", "phase": "skip"}},
        ],
        [
            {
                "id": "bad-bool",
                "prompt": "x",
                "expected": {"action": "skip", "phase": "skip", "should_retrieve": "false"},
            }
        ],
        [
            {
                "id": "bad-route",
                "prompt": "x",
                "expected": {
                    "action": "skip",
                    "phase": "skip",
                    "retrieval_route": "worker_direct",
                },
            }
        ],
        [
            {
                "id": "bad-handoff",
                "prompt": "x",
                "expected": {
                    "action": "skip",
                    "phase": "skip",
                    "handoff_required": "false",
                },
            }
        ],
    ]
    for cases in invalid_sets:
        try:
            preflight.validate_cases(cases)
        except ValueError:
            continue
        raise AssertionError(f"invalid cases accepted: {cases}")


def test_case_schema_rejects_contradictory_retrieval_expectations() -> None:
    base = {
        "id": "contradiction",
        "prompt": "查上次决定",
        "expected": {
            "action": "retrieve",
            "phase": "retrieve",
            "should_retrieve": True,
            "kb_owner": "parent",
        },
    }
    contradictions = [
        {"allow_zero_hits": True, "min_hits": 1},
        {"allow_zero_hits": True, "must_find_any": ["entry-1"]},
        {"allow_zero_hits": False, "min_hits": 0},
    ]
    for expectation in contradictions:
        case = {**base, "retrieval_expectation": expectation}
        try:
            preflight.validate_cases([case], require_retrieval_expectation=True)
        except ValueError:
            continue
        raise AssertionError(f"contradictory retrieval expectation accepted: {expectation}")


def test_report_redacts_sensitive_retrieval_query() -> None:
    opaque_token = "github_pat_" + "D" * 48
    cases = [
        {
            "id": "sensitive",
            "prompt": f"查历史记录 token=secret-value 内网 10.0.0.8 {opaque_token}中文",
            "expected": {
                "action": "retrieve",
                "phase": "retrieve",
                "should_retrieve": True,
                "kb_owner": "parent",
            },
        }
    ]
    _, details = preflight.evaluate_cases(cases)
    query = details[0]["prediction"]["retrieval_plan"]["query"]
    assert "secret-value" not in query
    assert "10.0.0.8" not in query
    assert opaque_token not in query
    assert "<redacted>" in query
    assert "<private-ip>" in query
    assert "[REDACTED]" in query


def test_retrieval_schema_is_required_and_query_groups_are_redacted() -> None:
    malformed = preflight._retrieval_check(
        {"exit_code": 0, "parse_error": "", "payload": {}},
        {"allow_zero_hits": True},
    )
    assert malformed["pass"] is False
    assert malformed["checks"]["schema_ok"] is False

    synthetic_anchor = "fixture-anchor"
    password_anchor = "=".join(("password", synthetic_anchor))
    valid = preflight._retrieval_check(
        {
            "exit_code": 0,
            "parse_error": "",
            "payload": {
                "retrieval_id": "fixture-retrieval",
                "mode": "read_only_rag_context",
                "hit_count": 0,
                "items": [],
                "query_groups": [
                    {
                        "name": "token=secret-name",
                        "query": "token=secret-value host 10.0.0.8",
                        "anchors": [password_anchor, "192.168.1.8"],
                    }
                ],
            },
        },
        {"allow_zero_hits": True},
    )
    assert valid["pass"] is True
    group_query = valid["query_groups"][0]["query"]
    assert "secret-value" not in group_query
    assert "10.0.0.8" not in group_query
    assert "<redacted>" in group_query
    assert "<private-ip>" in group_query
    serialized_groups = json.dumps(valid["query_groups"], ensure_ascii=False)
    assert "secret-name" not in serialized_groups
    assert synthetic_anchor not in serialized_groups
    assert "192.168.1.8" not in serialized_groups


def test_tree_fingerprint_detects_metadata_only_change() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        path = root / "repos" / "demo" / "main" / "kb.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("same content\n", encoding="utf-8")
        before = preflight._tree_fingerprint(root)
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        after = preflight._tree_fingerprint(root)
        assert preflight._fingerprint_changes(before, after) == ["repos/demo/main/kb.jsonl"]


def test_history_replay_counts_invalid_main_session_without_segments() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        day = root / "2026" / "07" / "15"
        day.mkdir(parents=True)
        path = day / "fixture.jsonl"
        path.write_text(
            json.dumps(
                {"type": "session_meta", "payload": {"id": "session-empty", "thread_source": "user"}}
            )
            + "\nnot-json\n",
            encoding="utf-8",
        )
        report = preflight.replay_history(
            root,
            dates=["2026-07-15"],
            excluded_session_ids=set(),
            examples=2,
        )
        assert report["session_files_scanned"] == 1
        assert report["main_sessions_replayed"] == 0
        assert report["invalid_json_rows"] == 1


def test_strict_gate_fails_on_safety_change() -> None:
    report = {
        "case_metrics": {
            "case_total": 1,
            "false_positive_count": 0,
            "false_negative_count": 0,
            "phase_accuracy": 1.0,
            "action_accuracy": 1.0,
            "action_owner_accuracy": 1.0,
            "query_anchor_failure_count": 0,
        },
        "retrieval_metrics": {
            "retrieval_case_pass_rate": 1.0,
            "required_hit_case_pass_rate": 1.0,
            "safety_file_changes": 1,
        },
    }
    assert preflight._strict_failures(report) == [
        "KB files changed during read-only preflight"
    ]


def test_strict_gate_rejects_zero_retrieval_cases() -> None:
    report = {
        "case_metrics": {
            "case_total": 1,
            "false_positive_count": 0,
            "false_negative_count": 0,
            "phase_accuracy": 1.0,
            "action_accuracy": 1.0,
            "action_owner_accuracy": 1.0,
            "query_anchor_failure_count": 0,
        },
        "strict_retrieval_required": True,
        "retrieval_metrics": {
            "retrieval_case_total": 0,
            "retrieval_case_pass_rate": None,
            "required_hit_case_pass_rate": None,
            "safety_file_changes": 0,
        },
    }
    assert preflight._strict_failures(report) == [
        "strict retrieval preflight requires at least one retrieval case"
    ]


def test_strict_gate_rejects_topic_preflight_mismatch() -> None:
    report = {
        "case_metrics": {
            "case_total": 1,
            "false_positive_count": 0,
            "false_negative_count": 0,
            "phase_accuracy": 1.0,
            "action_accuracy": 1.0,
            "action_owner_accuracy": 1.0,
            "coordination_accuracy": 1.0,
            "query_anchor_failure_count": 0,
            "topic_preflight_case_total": 1,
            "topic_preflight_accuracy": 0.0,
        },
        "retrieval_metrics": {},
    }
    assert preflight._strict_failures(report) == [
        "gold case topic preflight accuracy is below 100%"
    ]


def test_metric_domains_do_not_present_simulation_as_live_effectiveness() -> None:
    domains = preflight._metric_domains({
        "case_metrics": {
            "case_total": 54,
            "phase_accuracy": 1.0,
            "action_accuracy": 1.0,
            "coordination_accuracy": 1.0,
        },
        "retrieval_metrics": {
            "retrieval_case_total": 19,
            "retrieval_case_pass_rate": 1.0,
            "required_hit_case_pass_rate": 1.0,
            "irrelevant_slot_rate": 0.0,
        },
    })
    assert domains["policy_routing_accuracy"]["live_codex_behavior"] is False
    assert domains["retrieval_hit_quality"]["live_codex_behavior"] is False
    assert domains["real_adoption"]["available"] is False
    assert domains["closeout_completion"]["available"] is False
    assert domains["final_task_benefit"]["available"] is False


def main() -> int:
    tests = [
        test_shared_command_contract_splits_chained_wrappers,
        test_current_evidence_skips_retrieval,
        test_runtime_audit_reads_raw_evidence_without_rag,
        test_prior_decision_retrieves_once,
        test_skill_update_boundary,
        test_ccs_update_routes_to_historical_safe_update,
        test_cc_switch_high_risk_config_requires_history_unless_current_evidence,
        test_mixed_topics_keep_independent_retrieval_budgets,
        test_ordinary_subagent_reuses_parent_hints,
        test_ordinary_subagent_honors_full_kb_forbid_despite_script_guard,
        test_parent_delegates_broad_retrieval_to_readonly_scout,
        test_parent_reuses_scout_brief_without_retrieving_again,
        test_worker_requests_parent_followup_instead_of_self_retrieval,
        test_kb_scout_hands_maintenance_back_to_parent,
        test_duplicate_logical_task_does_not_repeat_initial_rag,
        test_initialized_task_allows_maintenance_and_new_history_anchor,
        test_hard_negative_and_english_history_boundaries,
        test_gold_cases_pass_routing_gate,
        test_rag_preflight_uses_copy_and_preserves_production,
        test_snapshot_mutation_is_detected_without_touching_production,
        test_direct_production_mutation_is_detected_but_not_rolled_back,
        test_rag_timeout_fails_fast,
        test_rag_runner_rejects_empty_or_nonobject_stdout,
        test_history_replay_flags_possible_duplicate_without_suppressing_it,
        test_history_custom_exec_failure_is_an_attempt_not_a_success,
        test_history_multi_message_turn_is_excluded_from_efficiency_comparison,
        test_history_wording_triggers_retrieve_and_current_text_stays_skip,
        test_case_schema_rejects_empty_duplicate_and_string_boolean,
        test_case_schema_rejects_contradictory_retrieval_expectations,
        test_report_redacts_sensitive_retrieval_query,
        test_retrieval_schema_is_required_and_query_groups_are_redacted,
        test_tree_fingerprint_detects_metadata_only_change,
        test_history_replay_counts_invalid_main_session_without_segments,
        test_strict_gate_fails_on_safety_change,
        test_strict_gate_rejects_zero_retrieval_cases,
        test_strict_gate_rejects_topic_preflight_mismatch,
        test_metric_domains_do_not_present_simulation_as_live_effectiveness,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failures.append(test.__name__)
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}")
        return 1
    print("kb_eval_preflight tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_TEST_GUARD.run(main) if _TEST_GUARD else main())
