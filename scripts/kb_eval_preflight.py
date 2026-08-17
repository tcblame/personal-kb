#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import kb_audit_codex_sessions as session_audit
import kb_command_contract as command_contract
from kb_lib import personal_kb_root_dir


ACTIONS = {"skip", "retrieve", "audit_readonly", "maintain", "reuse_parent_hints"}
ACTION_PHASE = {
    "skip": "skip",
    "retrieve": "retrieve",
    "audit_readonly": "audit",
    "maintain": "maintain",
    "reuse_parent_hints": "skip",
}
PHASES = {"skip", "retrieve", "audit", "maintain"}
KB_OWNERS = {"none", "parent", "kb_scout"}
RETRIEVAL_ROUTES = {
    "none",
    "parent_direct",
    "kb_scout",
    "reuse_parent_hints",
    "request_parent_followup",
    "parent_audit",
    "kb_scout_audit",
    "parent_maintenance",
}
ORDINARY_SUBAGENT_ROLES = {"subagent", "ordinary_subagent", "worker", "explorer"}
KB_SCOUT_ROLES = {"kb_scout", "scout"}
SCOUT_BRIEF_SCHEMA = "personal-kb.scout-brief/v1"

FORBID_RE = re.compile(
    r"禁止.{0,12}(?:personal[- ]?kb|\bkb\b|知识库)|"
    r"不要.{0,12}(?:personal[- ]?kb|\bkb\b|知识库)|"
    r"do not run personal-kb|"
    r"do not (?:use|reuse|reference).{0,24}(?:personal[- ]?kb|(?:parent )?kb hints?|\bkb\b)|"
    r"without (?:using )?(?:personal[- ]?kb|kb)",
    re.I,
)
AUDIT_RE = re.compile(
    r"(?:personal[- ]?kb|\bkb\b|(?:个人|本地).{0,8}知识库).{0,36}(?:审计|效果|运行|runtime|会话|历史对话|"
    r"有没有用|是否合适|妨碍|误触发|漏触发|为什么没识别)|"
    r"(?:审计|效果|运行|runtime|会话|历史对话|误触发|漏触发|为什么没识别).{0,36}"
    r"(?:personal[- ]?kb|\bkb\b|(?:个人|本地).{0,8}知识库)|"
    r"codex.{0,24}历史对话.{0,64}(?:skill\s*影响|为什么.{0,8}没识别)|"
    r"(?:多agent|多 Agent|分析|看看).{0,24}(?:历史对话|会话).{0,24}"
    r"(?:kb|personal[- ]?kb|知识库|skill).{0,24}(?:优化|合适|良性|妨碍|有没有用|是否需要)",
    re.I,
)
MAINTAIN_RE = re.compile(
    r"(?:personal[- ]?kb|\bkb\b|(?:个人|本地).{0,8}知识库).{0,28}(?:closeout|维护|修复数据|迁移|归档|"
    r"新增记录|更新记录|写入记录|质量门禁|重建索引)|"
    r"(?:closeout|维护|修复数据|迁移|归档|新增记录|更新记录|写入记录|质量门禁|重建索引)"
    r".{0,28}(?:personal[- ]?kb|\bkb\b|(?:个人|本地).{0,8}知识库)",
    re.I,
)
NON_KB_SKILL_MAINTENANCE_RE = re.compile(
    r"(?:更新|升级|同步|整理|统一).{0,20}(?:所有|全部)?\s*(?:skill|skills|mcp)|"
    r"(?:skill|skills|mcp).{0,20}(?:更新|升级|同步|说明文件|标题格式)",
    re.I,
)
DURABLE_REQUEST_RE = re.compile(
    r"永远记住|你记住|请记住|以后.{0,24}(?:一定|必须|默认)|"
    r"下次.{0,32}(?:直接|自动|照做)|做成命令.{0,48}(?:记录|下次)|"
    r"固定工作流|跨会话复用|以后我一说",
    re.I,
)
HISTORY_DEP_RE = re.compile(
    r"之前的那个|前两天|上次|以前.{0,28}(?:遇到|确认|决定|说过|做过)|"
    r"之前.{0,20}(?:说过|记录过|确认过|写过命令)|"
    r"我记得.{0,24}(?:之前|以前|说过)|沿用.{0,24}(?:决定|材料|风险|口径)|"
    r"按.{0,16}(?:上次|之前|已确认).{0,16}(?:决定|口径|方案)|"
    r"另一个项目.{0,24}(?:以前|类似)|"
    r"改回.{0,16}(?:以前|之前|原来)|恢复成.{0,16}(?:以前|之前)|历史副作用|长期副作用|"
    r"(?:last time|previously).{0,32}(?:agreed|decided|confirmed)|"
    r"(?:agreed|decided|confirmed).{0,32}(?:last time|previously)",
    re.I,
)
RECENT_HISTORY_RE = re.compile(
    r"找一下.{0,28}(?:应该有|之前做过|做过|有设计|设计稿|方案|决定)|"
    r"为什么每次都要(?:重新|重复).{0,16}(?:安装|配置|登录|输入)|"
    r"(?:每次都要|又没生效|反复).{0,16}(?:重装|重新安装|重新配置)|"
    r"分析最近.{0,24}(?:codex|对话|会话|历史记录|历史对话)|"
    r"最近.{0,16}(?:codex|对话|会话).{0,20}(?:记录|历史)|"
    r"昨天.{0,24}(?:会话|对话|确认过|决定过)|"
    r"(?:历史记录|旧记录)里有没有.{0,24}(?:决定|合并|确认|方案)",
    re.I,
)
CC_SWITCH_SAFE_OPERATION_RE = re.compile(
    r"(?:(?:更新|升级|检查更新|重启|退出|关闭|kill).{0,24}(?:ccs|ccswitch|cc[- ]switch)|"
    r"(?:ccs|ccswitch|cc[- ]switch).{0,24}(?:更新|升级|检查更新|最新版|重启|退出|关闭|kill))",
    re.I,
)
CC_SWITCH_CURRENT_DIAGNOSTIC_RE = re.compile(
    r"(?=.*(?:ccs|ccswitch|cc[- ]switch))"
    r"(?=.*(?:检查|查看|看看|确认|排查|分析|为什么|是否|当前|现在|进程|日志|状态))"
    r"(?=.*(?:关闭|退出|停止|kill|进程|日志|状态|卡死))",
    re.I,
)
CC_SWITCH_HIGH_RISK_RE = re.compile(
    r"(?=(?:ccs|ccswitch|cc[- ]switch))"
    r"(?=.*(?:guard|127\.0\.0\.1|localhost|本地地址|回环地址|高危|危险操作|"
    r"安全重启|延迟重启|守护进程|被改回|改回本地))",
    re.I,
)
TOPIC_BOUNDARY_RE = re.compile(r"(?:[。；;\n]+|(?:，|,)?(?:然后|另外|同时|顺带|并且)(?:再)?(?:，|,)?)")
WORKER_SCRIPT_GUARD_RE = re.compile(r"do not run personal-kb scripts\.?", re.I)
EXPLICIT_RETRIEVAL_RE = re.compile(
    r"(?:(?:帮我|请|去|现在|先|直接)\s*)?"
    r"(?:查(?:一查|找)?|搜(?:一搜|索)?|找(?:一找)?|看(?:一下)?|回看|检索(?:一下)?)"
    r"\s*(?:一下|一遍)?\s*"
    r"(?:历史记录|旧记录|历史对话|个人(?:知识库|记录|经验)|personal[- ]?kb|\bkb\b|知识库)|"
    r"(?:历史记录|旧记录|历史对话|个人(?:知识库|记录|经验)|personal[- ]?kb|\bkb\b|知识库)"
    r"\s*(?:里|中|上)?\s*(?:有没有|是否有|是否)|"
    r"(?:personal[- ]?kb|\bkb\b|知识库).{0,12}"
    r"(?:(?:请|帮我|去|直接|现在)\s*(?:查|搜|找|检索|召回)|(?:查|搜|找))",
    re.I,
)
MAPPING_RE = re.compile(
    r"(?:项目名|仓库|repo).{0,24}(?:目录|路径|分支|branch|映射)|"
    r"(?:目录|路径|分支|branch).{0,24}(?:项目名|仓库|repo).{0,12}映射",
    re.I,
)
CURRENT_EVIDENCE_RE = re.compile(
    r"当前(?:ccs|ccswitch|cc[- ]switch)?.{0,16}?(?:文件|代码|日志|异常栈|配置).{0,24}(?:足够|完整|已经给出|可以回答)|"
    r"只看当前(?:ccs|ccswitch|cc[- ]switch)?.{0,16}?(?:文件|代码|日志|配置)|fully answerable from current",
    re.I,
)
REJECT_HISTORY_RE = re.compile(
    r"不要.{0,16}(?:参考|沿用|使用).{0,16}(?:上次|之前|历史)|"
    r"别.{0,16}(?:参考|沿用|使用).{0,16}(?:上次|之前|历史)|"
    r"ignore.{0,16}(?:last time|previous|history)",
    re.I,
)
CURRENT_TASK_RE = re.compile(
    r"当前文件.{0,28}(?:修改|改名|替换)|第\s*\d+\s*行|运行.{0,20}(?:单测|测试)|"
    r"(?:我的|当前)?项目.{0,20}(?:有用到|用过|使用)|"
    r"重构.{0,28}(?:前端|页面)|(?:卡死|无法点击|无法输入中文).{0,30}(?:看看|分析|为什么)|"
    r"分析简历.{0,24}(?:技术|项目)|"
    r"(?:不打算|要不要).{0,24}(?:写|放).{0,16}(?:项目|简历).{0,24}(?:合适|技术含量)",
    re.I,
)
PRIVATE_IP_RE = re.compile(
    r"\b(?:10\.(?:\d{1,3}\.){2}\d{1,3}|192\.168\.(?:\d{1,3}\.)\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[01])\.(?:\d{1,3}\.)\d{1,3})\b"
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|password|passwd|secret|api[_-]?key)\b\s*[:=]\s*([^\s,;]+)"
)
OPENAI_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b")
RETRIEVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").split())


def _normalized_key(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _redact(value: str) -> str:
    text = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", value)
    text = OPENAI_TOKEN_RE.sub("<redacted-token>", text)
    return PRIVATE_IP_RE.sub("<private-ip>", text)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    return value


def _query_for(prompt: str, context: dict[str, Any]) -> str:
    subject = _normalize_text(str(context.get("task_subject") or ""))
    prompt_text = _normalize_text(prompt)
    query = f"{subject} {prompt_text}".strip() if subject else prompt_text
    query = re.sub(r"^作为\s*(?:personal[- ]?kb|kb)?\s*scout[，,：:]?\s*", "", query, flags=re.I)
    query = re.sub(r"^只读检索\s*", "", query, flags=re.I)
    return query[:360]


def _context_int(context: dict[str, Any], key: str) -> int:
    value = context.get(key)
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _should_use_kb_scout(context: dict[str, Any]) -> bool:
    return bool(
        context.get("dedicated_kb_scout")
        or str(context.get("retrieval_scope") or "").strip().lower() == "broad"
        or context.get("retrieval_expansion_required")
        or context.get("cross_project_history")
        or context.get("cross_historical_stages")
        or context.get("candidate_deduplication_required")
        or context.get("conflict_analysis_required")
        or context.get("context_pressure_high")
        or _context_int(context, "dependent_worker_count") >= 2
    )


def _retrieval_route(
    action: str,
    *,
    kb_owner: str,
    parent_followup_required: bool,
) -> str:
    if parent_followup_required:
        return "request_parent_followup"
    if action == "retrieve":
        return "kb_scout" if kb_owner == "kb_scout" else "parent_direct"
    if action == "reuse_parent_hints":
        return "reuse_parent_hints"
    if action == "audit_readonly":
        return "kb_scout_audit" if kb_owner == "kb_scout" else "parent_audit"
    if action == "maintain":
        return "parent_maintenance"
    return "none"


def _prediction(
    action: str,
    reason_codes: list[str],
    *,
    prompt: str,
    context: dict[str, Any],
    kb_owner: str = "none",
    current_evidence_required: bool = True,
    parent_followup_required: bool = False,
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise ValueError(f"unknown preflight action: {action}")
    should_retrieve = action == "retrieve"
    role = str(context.get("agent_role") or "parent").strip().lower()
    current_agent_is_parent = role not in ORDINARY_SUBAGENT_ROLES | KB_SCOUT_ROLES
    scout_handoff_required = kb_owner == "kb_scout" and action in {"retrieve", "audit_readonly"}
    scout_brief_required = kb_owner == "kb_scout" and action == "retrieve"
    handoff_to_parent_required = (
        role in KB_SCOUT_ROLES and kb_owner == "parent" and action == "maintain"
    )
    return {
        "action": action,
        "phase": ACTION_PHASE[action],
        "should_use_kb": action != "skip",
        "should_retrieve": should_retrieve,
        "kb_owner": kb_owner,
        "current_evidence_required": current_evidence_required,
        "reason_codes": reason_codes,
        "retrieval_plan": {
            "query": _query_for(prompt, context) if should_retrieve else "",
            "limit": 5,
            "global": bool(context.get("global_retrieval")),
            "repo": str(context.get("repo") or ""),
            "branch": str(context.get("branch") or ""),
            "initial_retrieval_count": 1 if should_retrieve else 0,
            "route": _retrieval_route(
                action,
                kb_owner=kb_owner,
                parent_followup_required=parent_followup_required,
            ),
            "retrieval_id_required": should_retrieve,
        },
        "lifecycle_plan": {
            "closeout_if_retrieved": should_retrieve and current_agent_is_parent,
            "parent_closeout_required": should_retrieve and kb_owner == "kb_scout",
            "current_agent_may_closeout": current_agent_is_parent,
            "current_agent_may_heat": current_agent_is_parent,
            "current_agent_may_write": current_agent_is_parent,
            "adoption": "undetermined" if should_retrieve else "none",
            "heat": "undetermined" if should_retrieve else False,
            "durable_write": "undetermined" if should_retrieve else False,
        },
        "handoff_plan": {
            "required": bool(
                scout_handoff_required or handoff_to_parent_required or parent_followup_required
            ),
            "direction": (
                "scout_to_parent"
                if scout_handoff_required or handoff_to_parent_required
                else "worker_to_parent"
                if parent_followup_required
                else "none"
            ),
            "schema": SCOUT_BRIEF_SCHEMA if scout_brief_required else "",
            "parent_followup_required": parent_followup_required,
        },
    }


def predict_preflight(
    prompt: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = dict(context or {})
    text = _normalize_text(prompt)
    role = str(context.get("agent_role") or "parent").strip().lower()
    forbid_text = WORKER_SCRIPT_GUARD_RE.sub("", text) if role in ORDINARY_SUBAGENT_ROLES else text

    if context.get("explicit_kb_forbidden") or FORBID_RE.search(forbid_text):
        return _prediction(
            "skip",
            ["explicit_kb_forbidden"],
            prompt=text,
            context=context,
            current_evidence_required=True,
        )

    if role in ORDINARY_SUBAGENT_ROLES:
        parent_provided_hints = bool(context.get("parent_provided_kb_hints"))
        new_history_work = bool(
            context.get("kb_followup_required")
            or context.get("new_history_anchor")
            or context.get("retrieval_expansion_required")
            or context.get("history_conflict")
        )
        unresolved_history_dependency = bool(
            context.get("cross_session_dependency")
            or HISTORY_DEP_RE.search(text)
            or RECENT_HISTORY_RE.search(text)
            or EXPLICIT_RETRIEVAL_RE.search(text)
        )
        worker_needs_parent_history = bool(
            new_history_work or (unresolved_history_dependency and not parent_provided_hints)
        )
        if worker_needs_parent_history:
            return _prediction(
                "skip",
                ["ordinary_subagent_requests_parent_kb_followup"],
                prompt=text,
                context=context,
                kb_owner="parent",
                parent_followup_required=True,
            )
        if parent_provided_hints:
            return _prediction(
                "reuse_parent_hints",
                ["ordinary_subagent_reuses_parent_hints"],
                prompt=text,
                context=context,
                kb_owner="parent",
            )
        return _prediction(
            "skip",
            ["ordinary_subagent_parent_owns_retrieval"],
            prompt=text,
            context=context,
        )

    if (
        context.get("logical_task_completed")
        and not context.get("new_history_anchor")
        and not MAINTAIN_RE.search(text)
        and not AUDIT_RE.search(text)
    ):
        return _prediction(
            "skip",
            ["logical_task_already_completed"],
            prompt=text,
            context=context,
        )

    if role in KB_SCOUT_ROLES and MAINTAIN_RE.search(text):
        return _prediction(
            "maintain",
            ["kb_scout_hands_maintenance_to_parent"],
            prompt=text,
            context=context,
            kb_owner="parent",
        )

    if MAINTAIN_RE.search(text):
        return _prediction(
            "maintain",
            ["explicit_kb_maintenance"],
            prompt=text,
            context=context,
            kb_owner="parent",
        )

    if AUDIT_RE.search(text):
        owner = "kb_scout" if role in KB_SCOUT_ROLES or _should_use_kb_scout(context) else "parent"
        return _prediction(
            "audit_readonly",
            ["kb_runtime_audit_uses_raw_evidence"],
            prompt=text,
            context=context,
            kb_owner=owner,
        )

    if context.get("current_evidence_fully_answers_task"):
        if DURABLE_REQUEST_RE.search(text):
            return _prediction(
                "maintain",
                ["current_evidence_supports_new_durable_knowledge"],
                prompt=text,
                context=context,
                kb_owner="parent",
            )
        return _prediction(
            "skip",
            ["current_evidence_fully_answers_task"],
            prompt=text,
            context=context,
        )

    if REJECT_HISTORY_RE.search(text) and CURRENT_EVIDENCE_RE.search(text):
        return _prediction(
            "skip",
            ["historical_context_explicitly_rejected"],
            prompt=text,
            context=context,
        )

    if (
        context.get("scout_brief_received")
        and not context.get("new_history_anchor")
        and not context.get("retrieval_expansion_required")
        and not context.get("history_conflict")
    ):
        return _prediction(
            "reuse_parent_hints",
            ["parent_reuses_scout_brief"],
            prompt=text,
            context=context,
            kb_owner="parent",
        )

    durable_request = bool(DURABLE_REQUEST_RE.search(text))
    safe_operation_history = bool(
        CC_SWITCH_SAFE_OPERATION_RE.search(text)
        and not CC_SWITCH_CURRENT_DIAGNOSTIC_RE.search(text)
    )
    high_risk_config_history = bool(
        CC_SWITCH_HIGH_RISK_RE.search(text) and not CURRENT_EVIDENCE_RE.search(text)
    )
    history_dependency = bool(
        HISTORY_DEP_RE.search(text)
        or RECENT_HISTORY_RE.search(text)
        or EXPLICIT_RETRIEVAL_RE.search(text)
        or (MAPPING_RE.search(text) and (HISTORY_DEP_RE.search(text) or context.get("cross_session_dependency")))
        or context.get("cross_session_dependency")
        or context.get("new_history_anchor")
        or (safe_operation_history and not durable_request)
        or high_risk_config_history
    )
    if durable_request and not history_dependency:
        return _prediction(
            "maintain",
            ["new_durable_knowledge_candidate"],
            prompt=text,
            context=context,
            kb_owner="parent",
        )
    already_initialized = bool(
        context.get("duplicate_logical_task") or context.get("initial_retrieval_already_done")
    )
    new_history_work = bool(
        context.get("new_history_anchor")
        or context.get("retrieval_expansion_required")
        or context.get("history_conflict")
    )
    if history_dependency and already_initialized and not new_history_work:
        return _prediction(
            "skip",
            ["logical_task_already_initialized"],
            prompt=text,
            context=context,
        )
    if history_dependency:
        owner = "kb_scout" if role in KB_SCOUT_ROLES or _should_use_kb_scout(context) else "parent"
        reasons: list[str] = []
        if durable_request:
            reasons.append("durable_cross_session_request")
        if MAPPING_RE.search(text):
            reasons.append("repo_branch_path_mapping")
        if EXPLICIT_RETRIEVAL_RE.search(text):
            reasons.append("explicit_history_retrieval")
        if HISTORY_DEP_RE.search(text) or context.get("cross_session_dependency"):
            reasons.append("cross_session_dependency")
        if RECENT_HISTORY_RE.search(text):
            reasons.append("recent_history_reference")
        if safe_operation_history:
            reasons.append("durable_safe_operation_rule")
        if high_risk_config_history:
            reasons.append("high_risk_cc_switch_config")
        if owner == "kb_scout" and role not in KB_SCOUT_ROLES:
            reasons.append("broad_retrieval_delegated_to_kb_scout")
        return _prediction(
            "retrieve",
            reasons or ["historical_context_required"],
            prompt=text,
            context=context,
            kb_owner=owner,
        )

    if NON_KB_SKILL_MAINTENANCE_RE.search(text):
        return _prediction(
            "skip",
            ["unrelated_skill_maintenance"],
            prompt=text,
            context=context,
        )

    if CURRENT_EVIDENCE_RE.search(text) or CURRENT_TASK_RE.search(text):
        return _prediction(
            "skip",
            ["current_evidence_first"],
            prompt=text,
            context=context,
        )

    return _prediction(
        "skip",
        ["no_cross_session_dependency"],
        prompt=text,
        context=context,
    )


def predict_topic_preflights(
    topics: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate independently identified topics without sharing retrieval state."""
    if not isinstance(topics, list) or not topics:
        raise ValueError("topics must be a non-empty list")

    shared_context = dict(context or {})
    initialized_topic_ids = {
        str(value).strip()
        for value in shared_context.pop("initialized_topic_ids", [])
        if str(value).strip()
    }
    # Task-wide state must not suppress a sibling topic. Callers track initialized
    # work with initialized_topic_ids instead.
    for key in ("duplicate_logical_task", "initial_retrieval_already_done", "logical_task_completed"):
        shared_context.pop(key, None)

    topic_results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, topic in enumerate(topics):
        if not isinstance(topic, dict):
            raise ValueError(f"topic {index} must be an object")
        topic_id = str(topic.get("id") or "").strip()
        topic_prompt = _normalize_text(str(topic.get("prompt") or ""))
        if not topic_id or not topic_prompt:
            raise ValueError(f"topic {index} requires non-empty id and prompt")
        if topic_id in seen_ids:
            raise ValueError(f"duplicate topic id: {topic_id}")
        seen_ids.add(topic_id)

        topic_context = dict(shared_context)
        raw_topic_context = topic.get("context") or {}
        if not isinstance(raw_topic_context, dict):
            raise ValueError(f"topic {topic_id} context must be an object")
        topic_context.update(raw_topic_context)
        if topic_id in initialized_topic_ids:
            topic_context["initial_retrieval_already_done"] = True

        topic_results.append(
            {
                "topic_id": topic_id,
                "prompt": topic_prompt,
                "prediction": predict_preflight(topic_prompt, topic_context),
            }
        )

    return {
        "schema": "personal-kb.topic-preflight/v1",
        "topic_count": len(topic_results),
        "initial_retrieval_count": sum(
            int(item["prediction"]["retrieval_plan"]["initial_retrieval_count"])
            for item in topic_results
        ),
        "topics": topic_results,
    }


def predict_request_preflights(
    prompt: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Split an explicit multi-subject request for deterministic policy simulation."""
    request_context = dict(context or {})
    explicit_topics = request_context.pop("topics", None)
    if explicit_topics is not None:
        if not isinstance(explicit_topics, list):
            raise ValueError("context.topics must be a list")
        return predict_topic_preflights(explicit_topics, request_context)

    parts = [part.strip(" ，,") for part in TOPIC_BOUNDARY_RE.split(_normalize_text(prompt))]
    topic_prompts = [part for part in parts if part]
    topics = [
        {"id": f"topic-{index}", "prompt": topic_prompt}
        for index, topic_prompt in enumerate(topic_prompts, start=1)
    ]
    return predict_topic_preflights(topics, request_context)


def _read_json_or_jsonl(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("every JSONL case must be an object")
        return {}, rows
    payload = json.loads(text)
    if isinstance(payload, list):
        if any(not isinstance(row, dict) for row in payload):
            raise ValueError("every case must be an object")
        return {}, payload
    if not isinstance(payload, dict):
        raise ValueError("cases file must contain an object, array, or JSONL objects")
    if "cases" not in payload:
        raise ValueError("cases object must contain a cases array")
    rows = payload.get("cases")
    if not isinstance(rows, list):
        raise ValueError("cases must be a list")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("every case must be an object")
    return payload, rows


def validate_cases(
    cases: list[dict[str, Any]],
    *,
    require_retrieval_expectation: bool = False,
    require_owner: bool = False,
) -> None:
    if not cases:
        raise ValueError("cases must contain at least one case")
    seen_ids: set[str] = set()
    for index, case in enumerate(cases):
        case_id = case.get("id") or case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"case {index + 1} requires a non-empty id")
        if case_id in seen_ids:
            raise ValueError(f"duplicate case id: {case_id}")
        seen_ids.add(case_id)
        prompt = case.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"case {case_id} requires a non-empty prompt")
        expected = case.get("expected")
        if not isinstance(expected, dict):
            raise ValueError(f"case {case_id} requires an expected object")
        action = _expected_action(expected)
        if action not in ACTIONS:
            raise ValueError(f"case {case_id} has invalid expected action: {action}")
        phase = str(expected.get("phase") or ACTION_PHASE[action])
        if phase not in PHASES:
            raise ValueError(f"case {case_id} has invalid expected phase: {phase}")
        if phase != ACTION_PHASE[action]:
            raise ValueError(f"case {case_id} action/phase are inconsistent")
        owner = expected.get("kb_owner")
        if require_owner and owner is None:
            raise ValueError(f"case {case_id} requires expected.kb_owner in strict mode")
        if owner is not None and (not isinstance(owner, str) or owner not in KB_OWNERS):
            raise ValueError(f"case {case_id} has invalid kb_owner: {owner}")
        retrieval_route = expected.get("retrieval_route")
        if retrieval_route is not None and (
            not isinstance(retrieval_route, str) or retrieval_route not in RETRIEVAL_ROUTES
        ):
            raise ValueError(f"case {case_id} has invalid retrieval_route: {retrieval_route}")
        if "handoff_required" in expected and not isinstance(expected.get("handoff_required"), bool):
            raise ValueError(f"case {case_id} expected.handoff_required must be boolean")
        if "should_retrieve" in expected and not isinstance(expected.get("should_retrieve"), bool):
            raise ValueError(f"case {case_id} expected.should_retrieve must be boolean")
        should_retrieve = _expected_should_retrieve(expected)
        if should_retrieve != (action == "retrieve"):
            raise ValueError(f"case {case_id} action/should_retrieve are inconsistent")
        topic_preflight = expected.get("topic_preflight")
        if topic_preflight is not None:
            if not isinstance(topic_preflight, dict):
                raise ValueError(f"case {case_id} expected.topic_preflight must be an object")
            expected_topic_count = topic_preflight.get("topic_count")
            expected_initial_count = topic_preflight.get("initial_retrieval_count")
            expected_topics = topic_preflight.get("topics")
            if (
                not isinstance(expected_topic_count, int)
                or isinstance(expected_topic_count, bool)
                or expected_topic_count < 1
            ):
                raise ValueError(
                    f"case {case_id} expected.topic_preflight.topic_count must be >= 1"
                )
            if (
                not isinstance(expected_initial_count, int)
                or isinstance(expected_initial_count, bool)
                or expected_initial_count < 0
            ):
                raise ValueError(
                    f"case {case_id} expected.topic_preflight.initial_retrieval_count must be >= 0"
                )
            if not isinstance(expected_topics, list) or len(expected_topics) != expected_topic_count:
                raise ValueError(
                    f"case {case_id} expected.topic_preflight.topics must match topic_count"
                )
            for topic_index, topic_expected in enumerate(expected_topics, start=1):
                if not isinstance(topic_expected, dict):
                    raise ValueError(
                        f"case {case_id} expected topic {topic_index} must be an object"
                    )
                topic_action = str(topic_expected.get("action") or "").strip()
                if topic_action not in ACTIONS:
                    raise ValueError(
                        f"case {case_id} expected topic {topic_index} has invalid action: {topic_action}"
                    )
                forbidden_terms = topic_expected.get("query_forbidden_terms", [])
                if not isinstance(forbidden_terms, list) or any(
                    not isinstance(value, str) for value in forbidden_terms
                ):
                    raise ValueError(
                        f"case {case_id} expected topic {topic_index} query_forbidden_terms must be strings"
                    )
        if require_retrieval_expectation and should_retrieve:
            expectation = case.get("retrieval_expectation")
            if not isinstance(expectation, dict):
                raise ValueError(f"case {case_id} requires retrieval_expectation for --run-rag")
            if "allow_zero_hits" not in expectation or not isinstance(
                expectation.get("allow_zero_hits"), bool
            ):
                raise ValueError(
                    f"case {case_id} retrieval_expectation.allow_zero_hits must be boolean"
                )
            if "min_hits" in expectation:
                min_hits = expectation.get("min_hits")
                if not isinstance(min_hits, int) or isinstance(min_hits, bool) or min_hits < 0:
                    raise ValueError(f"case {case_id} retrieval_expectation.min_hits must be >= 0")
                if expectation["allow_zero_hits"] and min_hits > 0:
                    raise ValueError(
                        f"case {case_id} cannot allow zero hits while min_hits is positive"
                    )
                if not expectation["allow_zero_hits"] and min_hits == 0:
                    raise ValueError(
                        f"case {case_id} cannot require a hit while min_hits is zero"
                    )
            if "max_irrelevant_rate" in expectation:
                rate = expectation.get("max_irrelevant_rate")
                if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not 0 <= rate <= 1:
                    raise ValueError(
                        f"case {case_id} retrieval_expectation.max_irrelevant_rate must be 0..1"
                    )
            for field in ("must_find_any", "forbidden_entry_ids", "relevance_anchor_any"):
                if field not in expectation:
                    continue
                values = expectation.get(field)
                if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                    raise ValueError(f"case {case_id} retrieval_expectation.{field} must be strings")
            if expectation["allow_zero_hits"] and expectation.get("must_find_any"):
                raise ValueError(
                    f"case {case_id} cannot allow zero hits while requiring a specific entry"
                )


def _expected_action(expected: dict[str, Any]) -> str:
    action = str(expected.get("action") or "").strip()
    if action:
        return action
    phase = str(expected.get("phase") or "skip").strip()
    return "audit_readonly" if phase == "audit" else phase


def _expected_should_retrieve(expected: dict[str, Any]) -> bool:
    if "should_retrieve" in expected:
        return bool(expected.get("should_retrieve"))
    return _expected_action(expected) == "retrieve"


def merge_case_inputs_and_gold(
    inputs: list[dict[str, Any]],
    gold_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    input_by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(inputs):
        case_id = item.get("id") or item.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"input case {index + 1} requires a non-empty id")
        if case_id in input_by_id:
            raise ValueError(f"duplicate input case id: {case_id}")
        if "expected" in item or "retrieval_expectation" in item:
            raise ValueError(f"input case {case_id} must not contain gold labels")
        input_by_id[case_id] = item

    gold_by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(gold_rows):
        case_id = item.get("id") or item.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"gold case {index + 1} requires a non-empty id")
        if case_id in gold_by_id:
            raise ValueError(f"duplicate gold case id: {case_id}")
        gold_by_id[case_id] = item

    missing_gold = sorted(set(input_by_id) - set(gold_by_id))
    orphan_gold = sorted(set(gold_by_id) - set(input_by_id))
    if missing_gold or orphan_gold:
        raise ValueError(
            f"case/gold ids differ: missing_gold={missing_gold} orphan_gold={orphan_gold}"
        )

    merged: list[dict[str, Any]] = []
    for case_id, item in input_by_id.items():
        gold = gold_by_id[case_id]
        combined = dict(item)
        combined["expected"] = gold.get("expected")
        if "retrieval_expectation" in gold:
            combined["retrieval_expectation"] = gold.get("retrieval_expectation")
        merged.append(combined)
    return merged


def _query_anchor_failures(query: str, groups: Any) -> list[list[str]]:
    if not isinstance(groups, list):
        return []
    lower = query.casefold()
    failures: list[list[str]] = []
    for raw_group in groups:
        group = raw_group if isinstance(raw_group, list) else [raw_group]
        terms = [str(term).strip() for term in group if str(term).strip()]
        if terms and not any(term.casefold() in lower for term in terms):
            failures.append(terms)
    return failures


def _evaluate_topic_preflight(
    prompt: str,
    context: dict[str, Any],
    expected: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    prediction = predict_request_preflights(prompt, context)
    expected_topics = expected.get("topics") if isinstance(expected.get("topics"), list) else []
    actual_topics = prediction.get("topics") if isinstance(prediction.get("topics"), list) else []
    topic_checks: list[dict[str, Any]] = []

    for index, topic_expected in enumerate(expected_topics):
        actual = actual_topics[index] if index < len(actual_topics) else {}
        topic_prediction = (
            actual.get("prediction") if isinstance(actual.get("prediction"), dict) else {}
        )
        retrieval_plan = (
            topic_prediction.get("retrieval_plan")
            if isinstance(topic_prediction.get("retrieval_plan"), dict)
            else {}
        )
        query = str(retrieval_plan.get("query") or "")
        forbidden_terms = [
            str(value).strip()
            for value in topic_expected.get("query_forbidden_terms", [])
            if str(value).strip()
        ]
        forbidden_hits = [
            term for term in forbidden_terms if term.casefold() in query.casefold()
        ]
        anchor_failures = _query_anchor_failures(
            query,
            topic_expected.get("query_anchor_groups"),
        )
        expected_action = str(topic_expected.get("action") or "")
        expected_route = str(topic_expected.get("retrieval_route") or "")
        expected_owner = str(topic_expected.get("kb_owner") or "")
        expected_initial = topic_expected.get("initial_retrieval_count")
        checks = {
            "topic_id_ok": (
                not topic_expected.get("topic_id")
                or actual.get("topic_id") == topic_expected.get("topic_id")
            ),
            "action_ok": topic_prediction.get("action") == expected_action,
            "owner_ok": not expected_owner or topic_prediction.get("kb_owner") == expected_owner,
            "route_ok": not expected_route or retrieval_plan.get("route") == expected_route,
            "initial_retrieval_count_ok": (
                expected_initial is None
                or retrieval_plan.get("initial_retrieval_count") == expected_initial
            ),
            "query_anchor_failures": anchor_failures,
            "query_forbidden_hits": forbidden_hits,
        }
        checks["pass"] = bool(
            checks["topic_id_ok"]
            and checks["action_ok"]
            and checks["owner_ok"]
            and checks["route_ok"]
            and checks["initial_retrieval_count_ok"]
            and not anchor_failures
            and not forbidden_hits
        )
        topic_checks.append(checks)

    summary = {
        "topic_count_ok": prediction.get("topic_count") == expected.get("topic_count"),
        "initial_retrieval_count_ok": (
            prediction.get("initial_retrieval_count") == expected.get("initial_retrieval_count")
        ),
        "topics": topic_checks,
    }
    summary["pass"] = bool(
        summary["topic_count_ok"]
        and summary["initial_retrieval_count_ok"]
        and len(actual_topics) == len(expected_topics)
        and all(item.get("pass") for item in topic_checks)
    )
    return prediction, summary


def evaluate_cases(cases: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    validate_cases(cases)
    details: list[dict[str, Any]] = []
    phase_correct = 0
    retrieve_correct = 0
    action_correct = 0
    action_owner_correct = 0
    action_owner_total = 0
    coordination_correct = 0
    coordination_total = 0
    false_positives = 0
    false_negatives = 0
    query_anchor_failure_count = 0
    topic_preflight_total = 0
    topic_preflight_correct = 0

    for index, case in enumerate(cases):
        case_id = str(case.get("id") or case.get("case_id") or f"case-{index + 1}")
        prompt = str(case.get("prompt") or "")
        context = case.get("context") if isinstance(case.get("context"), dict) else {}
        expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
        prediction = predict_preflight(prompt, context)
        expected_action = _expected_action(expected)
        expected_phase = str(expected.get("phase") or ACTION_PHASE.get(expected_action, "skip"))
        expected_retrieve = _expected_should_retrieve(expected)
        expected_owner = str(expected.get("kb_owner") or "")
        expected_route = str(expected.get("retrieval_route") or "")
        expected_handoff = expected.get("handoff_required")
        phase_ok = prediction["phase"] == expected_phase
        retrieve_ok = prediction["should_retrieve"] == expected_retrieve
        owner_ok = not expected_owner or prediction["kb_owner"] == expected_owner
        action_ok = prediction["action"] == expected_action
        route_ok = (
            not expected_route
            or prediction["retrieval_plan"].get("route") == expected_route
        )
        handoff_ok = (
            expected_handoff is None
            or prediction["handoff_plan"].get("required") is expected_handoff
        )
        anchor_failures = _query_anchor_failures(
            str(prediction["retrieval_plan"].get("query") or ""),
            expected.get("query_anchor_groups"),
        )
        expected_topic_preflight = expected.get("topic_preflight")
        request_prediction: dict[str, Any] | None = None
        topic_preflight_checks: dict[str, Any] | None = None
        if isinstance(expected_topic_preflight, dict):
            topic_preflight_total += 1
            request_prediction, topic_preflight_checks = _evaluate_topic_preflight(
                prompt,
                context,
                expected_topic_preflight,
            )
            topic_preflight_correct += int(bool(topic_preflight_checks.get("pass")))

        phase_correct += int(phase_ok)
        retrieve_correct += int(retrieve_ok)
        action_correct += int(action_ok)
        if expected_owner:
            action_owner_total += 1
            action_owner_correct += int(action_ok and owner_ok)
        if expected_route or expected_handoff is not None:
            coordination_total += 1
            coordination_correct += int(route_ok and handoff_ok)
        false_positives += int(not expected_retrieve and prediction["should_retrieve"])
        false_negatives += int(expected_retrieve and not prediction["should_retrieve"])
        topic_anchor_failures = bool(
            topic_preflight_checks
            and any(
                item.get("query_anchor_failures")
                for item in topic_preflight_checks.get("topics", [])
            )
        )
        query_anchor_failure_count += int(bool(anchor_failures) or topic_anchor_failures)
        public_prediction = json.loads(json.dumps(prediction, ensure_ascii=False))
        public_prediction["retrieval_plan"]["query"] = _redact(
            str(public_prediction["retrieval_plan"].get("query") or "")
        )
        public_request_prediction = None
        if request_prediction is not None:
            public_request_prediction = json.loads(
                json.dumps(request_prediction, ensure_ascii=False)
            )
            for topic in public_request_prediction.get("topics", []):
                topic_prediction = topic.get("prediction") if isinstance(topic, dict) else None
                if not isinstance(topic_prediction, dict):
                    continue
                retrieval_plan = topic_prediction.get("retrieval_plan")
                if isinstance(retrieval_plan, dict):
                    retrieval_plan["query"] = _redact(str(retrieval_plan.get("query") or ""))
        details.append(
            {
                "case_id": case_id,
                "prompt": _redact(prompt),
                "expected": {
                    "action": expected_action,
                    "phase": expected_phase,
                    "should_retrieve": expected_retrieve,
                    "kb_owner": expected_owner or None,
                    "retrieval_route": expected_route or None,
                    "handoff_required": expected_handoff,
                    "topic_preflight": expected_topic_preflight,
                },
                "prediction": public_prediction,
                "request_prediction": public_request_prediction,
                "checks": {
                    "action_ok": action_ok,
                    "phase_ok": phase_ok,
                    "retrieve_ok": retrieve_ok,
                    "owner_ok": owner_ok,
                    "route_ok": route_ok,
                    "handoff_ok": handoff_ok,
                    "query_anchor_failures": anchor_failures,
                    "topic_preflight": topic_preflight_checks,
                },
            }
        )

    total = len(cases)
    metrics = {
        "case_total": total,
        "phase_accuracy": round(phase_correct / total, 4) if total else None,
        "retrieve_accuracy": round(retrieve_correct / total, 4) if total else None,
        "action_accuracy": round(action_correct / total, 4) if total else None,
        "action_owner_accuracy": (
            round(action_owner_correct / action_owner_total, 4) if action_owner_total else None
        ),
        "coordination_accuracy": (
            round(coordination_correct / coordination_total, 4) if coordination_total else None
        ),
        "false_positive_count": false_positives,
        "false_negative_count": false_negatives,
        "query_anchor_failure_count": query_anchor_failure_count,
        "topic_preflight_case_total": topic_preflight_total,
        "topic_preflight_accuracy": (
            round(topic_preflight_correct / topic_preflight_total, 4)
            if topic_preflight_total
            else None
        ),
    }
    return metrics, details


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_fingerprint(root: Path) -> dict[str, str]:
    if not root.exists():
        return {"<root>": "missing"}
    fingerprint: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        fingerprint[path.relative_to(root).as_posix()] = (
            f"sha256={_file_hash(path)};size={stat.st_size};"
            f"mtime_ns={stat.st_mtime_ns};mode={stat.st_mode & 0o7777:o}"
        )
    return fingerprint


def _snapshot_id(fingerprint: dict[str, str]) -> str:
    payload = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _fingerprint_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    keys = sorted(set(before) | set(after))
    return [key for key in keys if before.get(key) != after.get(key)]


def _run_rag_query(
    query: str,
    *,
    rag_script: Path,
    kb_root: Path,
    plan: dict[str, Any],
    cwd: Path,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(rag_script),
        query,
        "--limit",
        str(int(plan.get("limit") or 5)),
        "--json",
        "--no-session-briefs",
    ]
    if plan.get("repo"):
        command.extend(["--repo", str(plan["repo"])])
    if plan.get("branch"):
        command.extend(["--branch", str(plan["branch"])])
    if plan.get("global"):
        command.append("--global")
    env = dict(os.environ)
    env["PERSONAL_KB_ROOT"] = str(kb_root)
    env["PERSONAL_KB_RUNTIME_SOURCE"] = "test"
    env["PERSONAL_KB_TEST_RUN_ID"] = f"preflight-{os.getpid()}-{time.time_ns()}"
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        return {
            "exit_code": 124,
            "elapsed_ms": elapsed_ms,
            "stderr": f"RAG preflight timed out after {timeout_seconds:g}s",
            "parse_error": "timeout",
            "payload": {},
        }
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    payload: dict[str, Any] = {}
    parse_error = ""
    if not completed.stdout.strip():
        parse_error = "empty RAG stdout"
    else:
        try:
            decoded = json.loads(completed.stdout)
            if isinstance(decoded, dict):
                payload = decoded
            else:
                parse_error = "RAG JSON must be an object"
        except json.JSONDecodeError as exc:
            parse_error = str(exc)
    return {
        "exit_code": completed.returncode,
        "elapsed_ms": elapsed_ms,
        "stderr": _redact(completed.stderr.strip())[:800],
        "parse_error": parse_error,
        "payload": payload,
    }


def _item_text(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, sort_keys=True).casefold()


def _retrieval_check(
    observation: dict[str, Any],
    expectation: dict[str, Any],
) -> dict[str, Any]:
    payload = observation.get("payload") if isinstance(observation.get("payload"), dict) else {}
    raw_items = payload.get("items")
    items = raw_items if isinstance(raw_items, list) else []
    reported_hit_count = payload.get("hit_count")
    schema_ok = bool(
        payload.get("mode") == "read_only_rag_context"
        and isinstance(payload.get("retrieval_id"), str)
        and bool(RETRIEVAL_ID_RE.fullmatch(str(payload.get("retrieval_id") or "")))
        and isinstance(raw_items, list)
        and isinstance(payload.get("query_groups"), list)
        and isinstance(reported_hit_count, int)
        and not isinstance(reported_hit_count, bool)
        and reported_hit_count == len(items)
        and all(
            isinstance(item, dict) and isinstance(item.get("entry_id"), str) and item.get("entry_id")
            for item in items
        )
    )
    entry_ids = [str(item.get("entry_id") or "") for item in items if isinstance(item, dict)]
    allow_zero = bool(expectation.get("allow_zero_hits"))
    min_hits = (
        int(expectation["min_hits"])
        if "min_hits" in expectation
        else (0 if allow_zero else 1)
    )
    must_find_any = {str(value) for value in expectation.get("must_find_any", []) if str(value)}
    forbidden = {str(value) for value in expectation.get("forbidden_entry_ids", []) if str(value)}
    anchors = [str(value).casefold() for value in expectation.get("relevance_anchor_any", []) if str(value)]
    irrelevant = 0
    for item in items:
        if not isinstance(item, dict):
            irrelevant += 1
        elif anchors and not any(anchor in _item_text(item) for anchor in anchors):
            irrelevant += 1
    irrelevant_rate = round(irrelevant / len(items), 4) if items else 0.0
    max_irrelevant_rate = float(expectation.get("max_irrelevant_rate", 1.0))
    checks = {
        "exit_ok": observation.get("exit_code") == 0 and not observation.get("parse_error"),
        "schema_ok": schema_ok,
        "min_hits_ok": len(items) >= min_hits,
        "must_find_ok": not must_find_any or bool(must_find_any.intersection(entry_ids)),
        "forbidden_ok": not bool(forbidden.intersection(entry_ids)),
        "irrelevant_rate_ok": irrelevant_rate <= max_irrelevant_rate,
    }
    return {
        "pass": all(checks.values()),
        "checks": checks,
        "hit_count": len(items),
        "retrieval_id": str(payload.get("retrieval_id") or ""),
        "entry_ids": entry_ids,
        "rejected_weak_count": int(payload.get("rejected_weak_count") or 0),
        "irrelevant_slot_count": irrelevant,
        "irrelevant_slot_rate": irrelevant_rate,
        "query_groups": [
            _redact_value(group)
            for group in (payload.get("query_groups") or [])
            if isinstance(group, dict)
        ],
    }


def run_retrieval_preflight(
    cases: list[dict[str, Any]],
    *,
    production_root: Path,
    rag_script: Path,
    cwd: Path,
    timeout_seconds: float = 30.0,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    validate_cases(cases, require_retrieval_expectation=True)
    production_before = _tree_fingerprint(production_root)
    results: list[dict[str, Any]] = []
    snapshot_changes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="personal-kb-preflight-") as temp_dir:
        snapshot_root = Path(temp_dir) / "personal-kb"
        shutil.copytree(production_root, snapshot_root)
        snapshot_before = _tree_fingerprint(snapshot_root)
        for index, case in enumerate(cases):
            case_id = str(case.get("id") or case.get("case_id") or f"case-{index + 1}")
            expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
            context = case.get("context") if isinstance(case.get("context"), dict) else {}
            predicted = predict_preflight(str(case.get("prompt") or ""), context)
            expected_retrieve = _expected_should_retrieve(expected)
            if not (expected_retrieve or predicted.get("should_retrieve")):
                continue
            query = str(predicted.get("retrieval_plan", {}).get("query") or case.get("prompt") or "")
            plan = dict(predicted.get("retrieval_plan") or {})
            case_routing = case.get("routing") if isinstance(case.get("routing"), dict) else {}
            for key in ("repo", "branch", "global", "limit"):
                if key in case_routing:
                    plan[key] = case_routing[key]
            observation = _run_rag_query(
                query,
                rag_script=rag_script,
                kb_root=snapshot_root,
                plan=plan,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )
            expectation = (
                case.get("retrieval_expectation")
                if isinstance(case.get("retrieval_expectation"), dict)
                else {"allow_zero_hits": True}
            )
            check = _retrieval_check(observation, expectation)
            results.append(
                {
                    "case_id": case_id,
                    "expected_retrieve": expected_retrieve,
                    "forced_for_expected_case": bool(expected_retrieve and not predicted.get("should_retrieve")),
                    "query": _redact(query),
                    "observation": {
                        "exit_code": observation["exit_code"],
                        "elapsed_ms": observation["elapsed_ms"],
                        "stderr": observation["stderr"],
                        "parse_error": observation["parse_error"],
                    },
                    "retrieval_check": check,
                }
            )
        snapshot_after = _tree_fingerprint(snapshot_root)
        snapshot_changes = _fingerprint_changes(snapshot_before, snapshot_after)
        snapshot_identifier = _snapshot_id(snapshot_before)

    production_after = _tree_fingerprint(production_root)
    production_changes = _fingerprint_changes(production_before, production_after)
    safety_changes = [f"production:{path}" for path in production_changes] + [
        f"snapshot:{path}" for path in snapshot_changes
    ]
    expected_results = [item for item in results if item["expected_retrieve"]]
    passed = sum(bool(item["retrieval_check"]["pass"]) for item in expected_results)
    nonempty = sum(item["retrieval_check"]["hit_count"] > 0 for item in expected_results)
    required_hit_results = [
        item
        for item in expected_results
        if next(
            (
                not bool((case.get("retrieval_expectation") or {}).get("allow_zero_hits"))
                for case in cases
                if str(case.get("id") or case.get("case_id")) == item["case_id"]
            ),
            False,
        )
    ]
    required_hit_passed = sum(
        bool(item["retrieval_check"]["pass"]) for item in required_hit_results
    )
    total_slots = sum(item["retrieval_check"]["hit_count"] for item in results)
    irrelevant_slots = sum(item["retrieval_check"]["irrelevant_slot_count"] for item in results)
    metrics = {
        "snapshot_id": snapshot_identifier,
        "retrieval_case_total": len(expected_results),
        "retrieval_case_pass_rate": round(passed / len(expected_results), 4) if expected_results else None,
        "retrieval_nonempty_cases": nonempty,
        "retrieval_zero_hit_cases": len(expected_results) - nonempty,
        "retrieval_nonempty_rate": round(nonempty / len(expected_results), 4) if expected_results else None,
        "required_hit_case_total": len(required_hit_results),
        "required_hit_case_pass_rate": (
            round(required_hit_passed / len(required_hit_results), 4)
            if required_hit_results
            else None
        ),
        "irrelevant_slot_rate": round(irrelevant_slots / total_slots, 4) if total_slots else 0.0,
        "safety_file_changes": len(safety_changes),
    }
    return metrics, results, safety_changes


def _date_strings(last_days: int) -> list[str]:
    today = date.today()
    return [f"{today - timedelta(days=offset):%Y-%m-%d}" for offset in range(max(1, last_days))]


def _read_session_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    invalid_rows = 0
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid_rows += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            invalid_rows += 1
    return rows, invalid_rows


def _historical_execution_status(payload_type: str, output: str) -> str:
    exit_codes = [int(value) for value in re.findall(r'["\']exit_code["\']\s*:\s*(-?\d+)', output)]
    if exit_codes:
        if all(code == 0 for code in exit_codes):
            return "success"
        if all(code != 0 for code in exit_codes):
            return "failure"
        return "unknown"
    if payload_type == "function_call":
        status = session_audit._execution_success(output)
        return "success" if status is True else "failure" if status is False else "unknown"
    return "unknown"


def _historical_turns(
    path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    rows, invalid_rows = _read_session_rows(path)
    meta = session_audit._select_session_meta(path, rows)
    if not meta or session_audit._is_subagent_meta(meta):
        return meta, [], {"invalid_json_rows": invalid_rows}
    outputs: dict[str, str] = {}
    for row in rows:
        payload = row.get("payload") or {}
        if row.get("type") == "response_item" and payload.get("type") in {
            "function_call_output",
            "custom_tool_call_output",
        }:
            call_id = str(payload.get("call_id") or "")
            if call_id:
                outputs[call_id] = session_audit._output_text(payload.get("output"))

    current_turn = ""
    segments: list[dict[str, Any]] = []
    latest_segment_by_turn: dict[str, dict[str, Any]] = {}
    unattributed_attempts = 0
    for row_index, row in enumerate(rows):
        payload = row.get("payload") or {}
        if row.get("type") == "turn_context" and isinstance(payload, dict):
            current_turn = session_audit._turn_id(payload, current_turn)
        if row.get("type") == "event_msg" and payload.get("type") == "task_started":
            current_turn = session_audit._turn_id(payload, current_turn)
        if row.get("type") == "event_msg" and payload.get("type") == "user_message":
            turn_id = session_audit._turn_id(payload, current_turn) or f"unknown-{row_index}"
            message = str(payload.get("message") or payload.get("text") or "")
            if message:
                segment = {
                    "turn_id": turn_id,
                    "message": message,
                    "timestamp": str(row.get("timestamp") or ""),
                    "row_index": row_index,
                    "observed_retrieval_attempts": 0,
                    "observed_confirmed_success_attempts": 0,
                    "observed_failed_attempts": 0,
                    "observed_unknown_attempts": 0,
                }
                segments.append(segment)
                latest_segment_by_turn[turn_id] = segment
        if row.get("type") != "response_item" or payload.get("type") not in {
            "function_call",
            "custom_tool_call",
        }:
            continue
        turn_id = session_audit._turn_id(payload, current_turn) or f"unknown-call-{row_index}"
        commands, _ = session_audit._call_commands(payload)
        status = _historical_execution_status(
            str(payload.get("type") or ""),
            outputs.get(str(payload.get("call_id") or ""), ""),
        )
        for command in commands:
            tokens = command_contract.parse_cli_tokens(command)
            detected = command_contract.direct_or_wrapper_scripts(command)
            # Keep nested Python invocation support from the session parser,
            # while direct and wrapper calls use the shared public contract.
            for key in session_audit._detected_scripts(command):
                if key not in detected:
                    detected.append(key)
            scripts = [
                key
                for key in detected
                if not command_contract.is_help_invocation(
                    tokens,
                    command_contract.KB_SCRIPT_PATTERNS[key],
                )
            ]
            attempts = sum(key in {"kb_rag_context", "kb_search"} for key in scripts)
            if not attempts:
                continue
            segment = latest_segment_by_turn.get(turn_id)
            if segment is None:
                unattributed_attempts += attempts
                continue
            segment["observed_retrieval_attempts"] += attempts
            if status == "success":
                segment["observed_confirmed_success_attempts"] += attempts
            elif status == "failure":
                segment["observed_failed_attempts"] += attempts
            else:
                segment["observed_unknown_attempts"] += attempts

    session_id = str(meta.get("id") or session_audit._filename_session_id(path) or path.stem)
    output: list[dict[str, Any]] = []
    seen_prompts: set[str] = set()
    turn_message_counts: dict[str, int] = defaultdict(int)
    for segment in segments:
        turn_message_counts[str(segment["turn_id"])] += 1
    for segment_index, segment in enumerate(segments):
        prompt = _normalize_text(str(segment["message"]))
        if not prompt:
            continue
        key = _normalized_key(prompt)
        possible_duplicate = bool(key and key in seen_prompts)
        if key:
            seen_prompts.add(key)
        multi_message_turn = turn_message_counts[str(segment["turn_id"])] > 1
        comparison_eligible = not possible_duplicate and not multi_message_turn
        prediction = predict_preflight(prompt)
        output.append(
            {
                "session_id": session_id,
                "turn_id": segment["turn_id"],
                "segment_index": segment_index,
                "timestamp": segment["timestamp"],
                "prompt_excerpt": _redact(prompt)[:240],
                "observed_retrieval_attempts": int(segment["observed_retrieval_attempts"]),
                "observed_confirmed_success_attempts": int(
                    segment["observed_confirmed_success_attempts"]
                ),
                "observed_failed_attempts": int(segment["observed_failed_attempts"]),
                "observed_unknown_attempts": int(segment["observed_unknown_attempts"]),
                "multi_message_turn": multi_message_turn,
                "possible_duplicate_prompt": possible_duplicate,
                "comparison_eligible": comparison_eligible,
                "comparison_exclusion_reason": (
                    "multi_message_turn"
                    if multi_message_turn
                    else "possible_duplicate_without_lifecycle_state"
                    if possible_duplicate
                    else ""
                ),
                "prediction": {
                    "action": prediction["action"],
                    "should_retrieve": prediction["should_retrieve"],
                    "reason_codes": prediction["reason_codes"],
                },
            }
        )
    return meta, output, {
        "invalid_json_rows": invalid_rows,
        "unattributed_retrieval_attempts": unattributed_attempts,
    }


def replay_history(
    sessions_root: Path,
    *,
    dates: list[str],
    excluded_session_ids: set[str],
    examples: int,
) -> dict[str, Any]:
    paths = session_audit._session_paths(sessions_root, dates)
    segments: list[dict[str, Any]] = []
    session_count = 0
    invalid_json_rows = 0
    unattributed_attempts = 0
    for path in paths:
        meta, session_segments, parser_stats = _historical_turns(path)
        session_id = str(meta.get("id") or session_audit._filename_session_id(path) or path.stem)
        is_main = bool(meta) and not session_audit._is_subagent_meta(meta)
        if session_id in excluded_session_ids or not is_main:
            continue
        invalid_json_rows += int(parser_stats.get("invalid_json_rows") or 0)
        unattributed_attempts += int(parser_stats.get("unattributed_retrieval_attempts") or 0)
        if not session_segments:
            continue
        session_count += 1
        segments.extend(session_segments)

    eligible = [item for item in segments if item["comparison_eligible"]]
    observed_segments = [item for item in eligible if item["observed_retrieval_attempts"] > 0]
    predicted_segments = [item for item in eligible if item["prediction"]["should_retrieve"]]
    suppressions = [
        item
        for item in eligible
        if item["observed_retrieval_attempts"] > 0 and not item["prediction"]["should_retrieve"]
    ]
    additions = [
        item
        for item in eligible
        if item["observed_retrieval_attempts"] == 0 and item["prediction"]["should_retrieve"]
    ]
    agreements = sum(
        (item["observed_retrieval_attempts"] > 0) == bool(item["prediction"]["should_retrieve"])
        for item in eligible
    )
    action_distribution: dict[str, int] = defaultdict(int)
    for item in eligible:
        action_distribution[str(item["prediction"]["action"])] += 1
    return {
        "dates": dates,
        "session_files_scanned": len(paths),
        "main_sessions_replayed": session_count,
        "turn_total": len({(item["session_id"], item["turn_id"]) for item in segments}),
        "decision_segment_total": len(segments),
        "comparison_eligible_segments": len(eligible),
        "ambiguous_segments": len(segments) - len(eligible),
        "multi_message_segments": sum(item["multi_message_turn"] for item in segments),
        "possible_duplicate_segments": sum(item["possible_duplicate_prompt"] for item in segments),
        "historical_observed_retrieval_segments": len(observed_segments),
        "historical_observed_retrieval_attempts": sum(
            item["observed_retrieval_attempts"] for item in eligible
        ),
        "historical_observed_confirmed_success_attempts": sum(
            item["observed_confirmed_success_attempts"] for item in eligible
        ),
        "historical_observed_failed_attempts": sum(
            item["observed_failed_attempts"] for item in eligible
        ),
        "historical_observed_unknown_attempts": sum(
            item["observed_unknown_attempts"] for item in eligible
        ),
        "unattributed_retrieval_attempts": unattributed_attempts,
        "candidate_predicted_retrieval_segments": len(predicted_segments),
        "candidate_predicted_initial_calls": len(predicted_segments),
        "candidate_suppressions": len(suppressions),
        "candidate_additions": len(additions),
        "audit_direct_count": sum(
            item["prediction"]["action"] == "audit_readonly" for item in eligible
        ),
        "candidate_action_distribution": dict(action_distribution),
        "retrieval_boolean_agreement": round(agreements / len(eligible), 4) if eligible else None,
        "observed_multi_retrieval_segments": sum(
            item["observed_retrieval_attempts"] > 1 for item in eligible
        ),
        "invalid_json_rows": invalid_json_rows,
        "examples": {
            "candidate_predicted_retrievals": predicted_segments[:examples],
            "candidate_suppressions": suppressions[:examples],
            "candidate_additions": additions[:examples],
            "ambiguous_segments": [
                item for item in segments if not item["comparison_eligible"]
            ][:examples],
        },
    }


def _strict_failures(report: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    case_metrics = report.get("case_metrics") or {}
    if not case_metrics.get("case_total"):
        failures.append("gold cases are empty")
    if case_metrics.get("false_positive_count"):
        failures.append("gold cases contain retrieval false positives")
    if case_metrics.get("false_negative_count"):
        failures.append("gold cases contain retrieval false negatives")
    if case_metrics.get("phase_accuracy") not in {None, 1.0}:
        failures.append("gold case phase accuracy is below 100%")
    if case_metrics.get("action_accuracy") not in {None, 1.0}:
        failures.append("gold case action accuracy is below 100%")
    if case_metrics.get("action_owner_accuracy") not in {None, 1.0}:
        failures.append("gold case action/owner accuracy is below 100%")
    if case_metrics.get("coordination_accuracy") not in {None, 1.0}:
        failures.append("gold case route/handoff accuracy is below 100%")
    if (
        case_metrics.get("topic_preflight_case_total")
        and case_metrics.get("topic_preflight_accuracy") != 1.0
    ):
        failures.append("gold case topic preflight accuracy is below 100%")
    if case_metrics.get("query_anchor_failure_count"):
        failures.append("candidate retrieval query missed required anchors")
    retrieval = report.get("retrieval_metrics") or {}
    if report.get("strict_retrieval_required") and not retrieval:
        failures.append("strict mode requires retrieval preflight")
    if report.get("strict_retrieval_required") and retrieval.get("retrieval_case_total") == 0:
        failures.append("strict retrieval preflight requires at least one retrieval case")
    if retrieval and retrieval.get("retrieval_case_pass_rate") not in {None, 1.0}:
        failures.append("one or more expected retrieval cases failed")
    if retrieval and retrieval.get("required_hit_case_pass_rate") not in {None, 1.0}:
        failures.append("one or more required-hit cases failed")
    if retrieval.get("safety_file_changes"):
        failures.append("KB files changed during read-only preflight")
    history = report.get("history") or {}
    if report.get("history_requested"):
        if not history.get("session_files_scanned"):
            failures.append("history replay found no session files")
        elif not history.get("main_sessions_replayed"):
            failures.append("history replay found no eligible main sessions")
        if history.get("invalid_json_rows"):
            failures.append("history replay encountered invalid JSON rows")
    return failures


def _metric_domains(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cases = report.get("case_metrics") or {}
    retrieval = report.get("retrieval_metrics") or {}
    return {
        "policy_routing_accuracy": {
            "source": "deterministic_policy_simulator",
            "available": bool(cases.get("case_total")),
            "case_total": int(cases.get("case_total") or 0),
            "phase_accuracy": cases.get("phase_accuracy"),
            "action_accuracy": cases.get("action_accuracy"),
            "coordination_accuracy": cases.get("coordination_accuracy"),
            "live_codex_behavior": False,
        },
        "retrieval_hit_quality": {
            "source": "read_only_counterfactual_retrieval",
            "available": bool(retrieval),
            "case_total": int(retrieval.get("retrieval_case_total") or 0),
            "contract_pass_rate": retrieval.get("retrieval_case_pass_rate"),
            "required_hit_case_pass_rate": retrieval.get("required_hit_case_pass_rate"),
            "irrelevant_slot_rate": retrieval.get("irrelevant_slot_rate"),
            "live_codex_behavior": False,
        },
        "real_adoption": {
            "source": "production_closeout_runtime",
            "available": False,
            "value": None,
            "measure_with": "kb_eval.py audit-runtime --last-days N",
        },
        "closeout_completion": {
            "source": "real_codex_session_audit",
            "available": False,
            "value": None,
            "measure_with": "kb_eval.py audit-sessions --last-days N",
        },
        "final_task_benefit": {
            "source": "human_outcome_review",
            "available": False,
            "value": None,
            "measure_with": "task-specific human review",
        },
    }


def _print_text(report: dict[str, Any]) -> None:
    cases = report["case_metrics"]
    print(
        "KB_POLICY_SIMULATOR "
        f"cases={cases['case_total']} phase_accuracy={cases['phase_accuracy']} "
        f"retrieve_accuracy={cases['retrieve_accuracy']} "
        f"coordination_accuracy={cases['coordination_accuracy']} "
        f"topic_preflight_accuracy={cases.get('topic_preflight_accuracy')}"
    )
    print(
        f"- false_positive_count: {cases['false_positive_count']}\n"
        f"- false_negative_count: {cases['false_negative_count']}\n"
        f"- query_anchor_failure_count: {cases['query_anchor_failure_count']}"
    )
    if report.get("retrieval_metrics"):
        retrieval = report["retrieval_metrics"]
        print(
            f"- retrieval_contract_pass_rate: {retrieval['retrieval_case_pass_rate']}\n"
            f"- retrieval_nonempty_rate: {retrieval['retrieval_nonempty_rate']} "
            f"zero_hit_cases={retrieval['retrieval_zero_hit_cases']}\n"
            f"- required_hit_case_pass_rate: {retrieval['required_hit_case_pass_rate']}\n"
            f"- irrelevant_slot_rate: {retrieval['irrelevant_slot_rate']}\n"
            f"- safety_file_changes: {retrieval['safety_file_changes']}"
        )
    if report.get("history"):
        history = report["history"]
        print("- history_shadow: behavior delta only; observed calls are not ground truth or quality scores")
        print(
            f"- history_turns: {history['turn_total']} decision_segments={history['decision_segment_total']} "
            f"eligible={history['comparison_eligible_segments']} ambiguous={history['ambiguous_segments']}\n"
            f"- historical_observed_retrieval_segments: {history['historical_observed_retrieval_segments']} "
            f"attempts={history['historical_observed_retrieval_attempts']} "
            f"unknown_execution={history['historical_observed_unknown_attempts']}\n"
            f"- candidate_predicted_retrieval_segments: {history['candidate_predicted_retrieval_segments']}\n"
            f"- candidate_suppressions: {history['candidate_suppressions']}\n"
            f"- candidate_additions: {history['candidate_additions']}"
        )
    domains = report.get("metric_domains") or {}
    print("- metric_domains:")
    for name in (
        "policy_routing_accuracy",
        "retrieval_hit_quality",
        "real_adoption",
        "closeout_completion",
        "final_task_benefit",
    ):
        domain = domains.get(name) or {}
        print(
            f"  - {name}: source={domain.get('source', '')} "
            f"available={bool(domain.get('available'))}"
        )
    if report.get("strict_failures"):
        print("- strict_failures:")
        for failure in report["strict_failures"]:
            print(f"  - {failure}")


def main(argv: list[str] | None = None) -> int:
    script_root = Path(__file__).resolve().parent
    repository_eval_dir = (
        script_root.parent.parent.parent
        / "docs"
        / "req"
        / "001-personal-kb-taxonomy"
        / "evals"
    )
    bundled_eval_dir = script_root.parent / "references" / "evals"
    required_eval_files = ("runtime-preflight-cases.json", "runtime-preflight-gold.json")
    eval_dir = repository_eval_dir
    if not all((repository_eval_dir / name).is_file() for name in required_eval_files):
        eval_dir = bundled_eval_dir
    default_cases = eval_dir / "runtime-preflight-cases.json"
    default_gold = eval_dir / "runtime-preflight-gold.json"
    parser = argparse.ArgumentParser(
        description="Read-only Personal KB routing and retrieval preflight with historical replay."
    )
    parser.add_argument("--cases", default=str(default_cases), help="Blind input cases JSON/JSONL")
    parser.add_argument("--gold", default=str(default_gold), help="Frozen gold labels JSON/JSONL")
    parser.add_argument(
        "--run-rag",
        action="store_true",
        help="Run the trusted RAG script against a temporary KB copy and detect mutations",
    )
    parser.add_argument(
        "--routing-only",
        action="store_true",
        help="With --strict, explicitly skip retrieval checks and gate only routing policy",
    )
    parser.add_argument(
        "--kb-root",
        default="",
        help="Production KB root; defaults to configured storage.root only when RAG checks run",
    )
    parser.add_argument("--rag-script", default=str(script_root / "kb_rag_context.py"))
    parser.add_argument(
        "--allow-unsafe-rag-script",
        action="store_true",
        help="Allow a non-canonical RAG script; isolation is detection-only, not a sandbox",
    )
    parser.add_argument("--rag-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--sessions-root", default="~/.codex/sessions")
    parser.add_argument("--last-days", type=int, default=0, help="Replay recent main sessions; 0 disables")
    parser.add_argument("--date", action="append", default=[], help="Replay date YYYY-MM-DD; repeatable")
    parser.add_argument("--include-current-session", action="store_true")
    parser.add_argument("--exclude-session-id", action="append", default=[])
    parser.add_argument("--examples", type=int, default=5)
    parser.add_argument("--strict", action="store_true", help="Return nonzero on any gold/safety failure")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.last_days < 0:
        parser.error("--last-days must be >= 0")
    if args.examples < 1:
        parser.error("--examples must be >= 1")
    if args.rag_timeout_seconds <= 0:
        parser.error("--rag-timeout-seconds must be > 0")
    if args.routing_only and args.run_rag:
        parser.error("--routing-only and --run-rag are mutually exclusive")
    if args.strict and not args.routing_only:
        args.run_rag = True

    dates: list[str] = []
    if args.date:
        seen_dates: set[str] = set()
        for raw_date in args.date:
            try:
                normalized = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                parser.error(f"invalid --date value: {raw_date}")
            if normalized not in seen_dates:
                dates.append(normalized)
                seen_dates.add(normalized)
    elif args.last_days > 0:
        dates = _date_strings(args.last_days)

    cases_path = Path(args.cases).expanduser().resolve()
    gold_path = Path(args.gold).expanduser().resolve()
    if not cases_path.is_file():
        parser.error(f"cases file does not exist: {cases_path}")
    if not gold_path.is_file():
        parser.error(f"gold file does not exist: {gold_path}")
    try:
        input_metadata, case_inputs = _read_json_or_jsonl(cases_path)
        gold_metadata, gold_rows = _read_json_or_jsonl(gold_path)
        cases = merge_case_inputs_and_gold(case_inputs, gold_rows)
        validate_cases(
            cases,
            require_retrieval_expectation=args.run_rag,
            require_owner=args.strict,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))
    case_metrics, case_details = evaluate_cases(cases)
    report: dict[str, Any] = {
        "mode": "deterministic_policy_simulator_with_read_only_counterfactual_retrieval",
        "cases_file": str(cases_path),
        "cases_sha256": _file_hash(cases_path),
        "gold_file": str(gold_path),
        "gold_sha256": _file_hash(gold_path),
        "gold_version": gold_metadata.get("gold_version") or gold_metadata.get("version") or "",
        "input_version": input_metadata.get("version") or "",
        "case_metrics": case_metrics,
        "case_details": case_details,
        "retrieval_metrics": {},
        "retrieval_results": [],
        "safety_file_changes": [],
        "history": {},
        "history_requested": bool(dates),
        "strict_retrieval_required": bool(args.strict and not args.routing_only),
        "limitations": [
            "the policy simulator is not the live Codex routing decision and is not a final answer quality predictor",
            "historical observed behavior is a baseline delta, not ground truth",
            "retrieval uses a temporary current KB copy with mutation detection, not an OS sandbox or historical snapshot",
            "adoption, heat, and final answer quality remain undetermined before task completion",
        ],
    }

    if args.run_rag:
        kb_root_value = args.kb_root.strip()
        if not kb_root_value:
            try:
                kb_root_value = str(personal_kb_root_dir())
            except ValueError as exc:
                parser.error(str(exc))
        production_root = Path(kb_root_value).expanduser().resolve()
        rag_script = Path(args.rag_script).expanduser().resolve()
        if production_root in {Path("/"), Path.home().resolve()}:
            parser.error("--kb-root cannot be the filesystem root or home directory")
        if not production_root.is_dir() or not (production_root / "repos").is_dir():
            parser.error(f"--kb-root must be a Personal KB directory containing repos/: {production_root}")
        canonical_rag_script = (script_root / "kb_rag_context.py").resolve()
        if not rag_script.is_file():
            parser.error(f"RAG script does not exist: {rag_script}")
        if rag_script != canonical_rag_script and not args.allow_unsafe_rag_script:
            parser.error(
                "non-canonical --rag-script requires --allow-unsafe-rag-script; "
                "the temporary copy is not a security sandbox"
            )
        retrieval_metrics, retrieval_results, safety_changes = run_retrieval_preflight(
            cases,
            production_root=production_root,
            rag_script=rag_script,
            cwd=Path.cwd(),
            timeout_seconds=args.rag_timeout_seconds,
        )
        report["retrieval_metrics"] = retrieval_metrics
        report["retrieval_results"] = retrieval_results
        report["safety_file_changes"] = safety_changes

    if dates:
        sessions_root = Path(args.sessions_root).expanduser().resolve()
        if not sessions_root.is_dir():
            parser.error(f"sessions root does not exist: {sessions_root}")
        excluded = {str(value) for value in args.exclude_session_id if str(value)}
        current_session_id = str(os.environ.get("CODEX_THREAD_ID") or "")
        if current_session_id and not args.include_current_session:
            excluded.add(current_session_id)
        report["history"] = replay_history(
            sessions_root,
            dates=dates,
            excluded_session_ids=excluded,
            examples=max(1, args.examples),
        )
        report["history"]["excluded_session_ids"] = sorted(excluded)

    report["metric_domains"] = _metric_domains(report)
    report["strict_failures"] = _strict_failures(report)
    report["strict_pass"] = not report["strict_failures"]
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_text(report)
    return 1 if args.strict and report["strict_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
