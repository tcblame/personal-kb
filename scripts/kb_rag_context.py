#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import difflib
import io
import json
import math
import os
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kb_lib import configure_utf8_streams

configure_utf8_streams()

import kb_git_reads
import kb_search
import kb_session_brief
from kb_runtime import runtime_scope, runtime_session_id
from kb_timing import count, span, timed
import kb_evidence
import kb_domain
import kb_project_view
import kb_project_routing
from kb_kinds import VALID_KINDS, parse_kind_filter, parse_legacy_type_filter
from kb_lib import (
    JsonlSafetyError,
    PersonalKbConfigError,
    expand_query,
    expand_query_variants,
    load_config,
    load_synonyms,
    read_jsonl,
    skill_root_dir,
    storage_layout,
)


MATCH_FIELDS = (
    "title",
    "tags",
    "aliases",
    "trigger_terms",
    "source_paths",
    "key_files",
    "key_facts",
    "term",
    "definition",
    "story",
    "symptom",
    "root_cause",
    "solution",
    "solution_pattern",
    "design_pattern",
    "purpose",
    "business_logic",
    "repo",
    "branch",
)

SUMMARY_FIELDS = (
    "story",
    "root_cause",
    "solution",
    "solution_pattern",
    "design_pattern",
    "purpose",
    "business_logic",
    "symptom",
    "definition",
)

CURRENT_STATUSES = {"", "active", "current", "implemented", "decision_confirmed", "partial_current"}
RETRIEVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _retrieval_id(value: str) -> str:
    explicit = str(value or "").strip()
    scope = runtime_scope()
    if explicit and scope["source"] != "test":
        raise ValueError(
            "--retrieval-id is test-only; production retrieval IDs are generated automatically"
        )
    candidate = explicit or uuid.uuid4().hex
    if not RETRIEVAL_ID_RE.fullmatch(candidate):
        raise ValueError(
            "--retrieval-id must be 1-128 opaque characters using letters, digits, '.', '_', ':', or '-'"
        )
    return candidate


def _scope_anchor_value(anchor: str) -> str:
    """Return the human-readable part of an optional ``kind:value`` anchor."""
    text = str(anchor or "").strip()
    if ":" in text:
        _prefix, value = text.split(":", 1)
        return value.strip() or text
    return text


def _validate_scope_anchors(query: str, anchors: list[str]) -> None:
    """Reject a scope that was never expressed by the caller's query.

    This catches a common agent failure mode early: a generated command adds
    unrelated anchors and then blindly retries after the command is rejected.
    The error is deliberately non-retryable and names the missing values.
    """
    normalized_query = str(query or "").casefold()
    missing = [
        anchor
        for anchor in anchors
        if _scope_anchor_value(anchor).casefold() not in normalized_query
    ]
    if missing:
        values = ", ".join(_scope_anchor_value(anchor) for anchor in missing)
        raise ValueError(
            f"scope anchors are not explicitly bound to the query; include {values} in the query"
        )


@timed("retrieve.receipt")
def _write_receipt(path_value: str, payload: dict[str, Any]) -> None:
    """Publish a complete new receipt without replacing existing files or KB data."""
    if not str(path_value or "").strip():
        return

    path = Path(os.path.abspath(Path(path_value).expanduser()))
    protected_root = storage_layout().root.resolve()
    configs = {skill_root_dir() / "config.json"}
    if os.environ.get("PERSONAL_KB_CONFIG"):
        configs.add(Path(os.environ["PERSONAL_KB_CONFIG"]).expanduser())

    def validate_target() -> None:
        # Reject links even when they currently resolve outside protected data;
        # a receipt is a new output, not permission to follow an alias and write.
        for component in (path, *path.parents):
            if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
                raise ValueError("receipt path must not contain symlinks or junctions")
        resolved = path.resolve(strict=False)
        if resolved == protected_root or protected_root in resolved.parents:
            raise ValueError("receipt must be outside the canonical Personal KB data root")
        if resolved in {config.resolve(strict=False) for config in configs}:
            raise ValueError("receipt must not target a Personal KB configuration")
        if path.exists():
            raise FileExistsError("receipt target already exists; choose a new output path")

    validate_target()
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    validate_target()
    descriptor, temporary = tempfile.mkstemp(prefix=".kb-receipt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
        validate_target()
        # A hard link publishes completed bytes atomically and fails if another
        # writer has claimed the target. Never fall back to replacing the target.
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class RetrievalArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _error_payload(
    *,
    retrieval_id: str,
    query: str,
    message: str,
    error_code: str,
    status: str = "error",
) -> dict[str, Any]:
    return {
        "retrieval_id": retrieval_id,
        "query": query,
        "hit_count": 0,
        "limit": 0,
        "mode": "read_only_rag_context",
        "status": status,
        "retryable": False,
        "items": [],
        "error": {"code": error_code, "message": str(message)},
        "runtime": runtime_scope(),
    }

STRONG_FIELDS = {"title", "trigger_terms", "source_paths", "key_files"}
MEDIUM_FIELDS = {"tags", "aliases", "key_facts", "term", "definition", "repo", "branch"}
WEAK_FIELDS = {
    "story",
    "symptom",
    "root_cause",
    "solution",
    "solution_pattern",
    "design_pattern",
    "purpose",
    "business_logic",
}

LOW_SIGNAL_TERMS = {
    "ai",
    "agent",
    "agents",
    "kb",
    "personal-kb",
    "知识库",
    "项目",
    "系统",
    "功能",
    "问题",
    "优化",
    "设计",
    "流程",
    "使用",
    "更新",
    "修改",
    "分析",
    "查看",
    "看看",
    "查找",
    "查一下",
    "需要",
    "需要吗",
    "应该",
    "是否",
    "怎么",
    "如何",
    "能否",
    "帮我",
    "请",
    "历史",
    "记录",
    "已有",
    "当前",
    "最近",
    "今天",
    "昨天",
    "明天",
    "面试",
    "面试材料",
    "材料",
    "java",
    "python",
    "后端",
    "前端",
    "会话",
}

KB_RUNTIME_MARKERS = (
    "personal-kb",
    "skills/personal-kb",
    "kb_rag_context.py",
    "kb_closeout.py",
    "session brief",
    "session_brief",
    "rag-first",
)

KNOWN_DOMAIN_MARKERS = {
    "personal-kb": ("personal-kb", "personal kb", "personalkb", "kbskill", "kb skill"),
    "kb": ("知识库",),
    "rag": ("检索增强", "使用前检索"),
    "closeout": ("使用后", "加热"),
    "session brief": ("session brief", "session_brief"),
}

NON_KB_MAINTENANCE_MARKERS = (
    "更新所有 skill",
    "更新全部 skill",
    "所有 skill 和 mcp",
    "全部 skill 和 mcp",
    "安装 skill",
    "cc-switch",
)

CONTEXT_GENERIC_TERMS = {
    "study",
    "recent-session",
    "recent_session",
    "session",
    "main",
    "master",
    "no-git",
    "skill",
    "skills",
    "plugin",
    "plugins",
    "插件",
    "mcp",
    "codex",
    "claude",
    "gemini",
    "interview-java-backend",
    "java后端",
    "java面试",
    "python后端",
    "ai应用",
    "springboot",
    "spring-boot",
    "技术栈",
}

# These words remain valid business anchors. Only an explicit product-subject
# query needs additional topical evidence beyond these broad overlaps.
PRODUCT_SUBJECTS = ("codex", "claude", "gemini")
SUBJECT_BROAD_TERMS = {
    "token", "tokens", "对话", "聊天", "chat", "conversation", "模型", "model",
    "models", "agent", "agents", "成本", "cost", "限制", "limit", "limits",
    "优化", "配置", "设置", "系统", "开销", "费用", "costs",
}

ARTIFACT_QUERY_MARKERS = (
    "简历",
    "面试材料",
    "简历素材",
    "项目经历",
    "工作经历",
    "面试讲稿",
    "面试官追问",
    "八股",
    "训练卡",
    "速查",
    "资料",
    "材料",
    "artifact_locator",
    "canonical_content",
    "00-当前版本.md",
)

FAULT_QUERY_MARKERS = (
    "故障",
    "报错",
    "异常",
    "错误",
    "失败",
    "卡死",
    "卡住",
    "闪退",
    "崩溃",
    "无响应",
    "不可用",
    "启动不了",
    "无法启动",
    "不能启动",
    "无法输入",
    "不能输入",
    "连接不上",
    "超时",
    "排查",
    "修复",
    "根因",
    "问题",
    "error",
    "exception",
    "timeout",
    "traceid",
    "500",
)

PRIOR_DECISION_MARKERS = (
    "之前已经决定",
    "之前决定",
    "以前决定",
    "上次决定",
    "按上次决定",
    "上次确认",
    "按上次确认",
    "按之前决定",
    "按之前确认",
    "之前记录过",
    "已经记录过",
    "之前确认",
    "已经确认",
    "沿用上次",
    "沿用之前",
    "照上次",
    "按原来",
)

DECISION_QUERY_FILLERS = (
    *PRIOR_DECISION_MARKERS,
    "之前的那个",
    "前两天",
    "last time",
    "previously",
    "agreed",
    "decided",
    "confirmed",
    "决定过",
    "记录过",
    "确认过",
    "已决定",
    "已记录",
    "已确认",
    "决定",
    "决策",
    "记录",
    "确认",
    "按上次",
    "按之前",
    "要不要",
    "是不是",
    "是否",
    "应该",
    "怎么处理",
    "怎么",
    "继续",
    "执行",
    "处理",
    "对吧",
    "那个",
    "这个",
)

DECISION_TOPIC_MARKERS = (
    "00-当前版本.md",
    "canonical_content",
    "简历结构",
    "工作经历",
    "项目经历",
    "公司项目",
    "在线考试",
    "learnai",
    "kys",
    "南沙",
    "dms",
    "个人负责范围",
    "代码存在",
    "技术栈",
    "简历",
)

DECISION_AUTHORITY_MARKERS = (
    "user_confirmed",
    "user-confirmed",
    "用户确认",
    "用户明确确认",
    "verified_summary",
    "canonical",
    "authoritative",
    "权威",
    "唯一事实源",
)

USER_CONFIRMATION_MARKERS = (
    "用户明确确认",
    "用户确认",
    "用户明确要求",
    "user confirmed",
    "explicitly confirmed by user",
)

PLANNER_ANCHOR_EXCLUDED_GROUPS = {"task", "specific-terms", "hard-anchors", "prior-decision"}

MAP_ANCHOR_FIELDS = {"title", "tags", "aliases", "trigger_terms", "source_paths", "key_files"}
SESSION_BRIEF_ANCHOR_FIELDS = {"title", "anchors", "queries"}

# Precision-first defaults.  They are intentionally small: the normal RAG
# path should be cheap and explainable, while callers can still use
# ``kb_search.py`` for an explicit broad corpus audit.
DEFAULT_MAX_QUERY_PLANS = 3
DEFAULT_MAX_QUERY_VARIANTS = 4
HARD_FRESHNESS_STATES = {"dirty_worktree", "diverged", "unresolvable", "conflicted"}
RELIABLE_FRESHNESS_STATES = {"fresh", "runtime_recent"}
RECHECK_FRESHNESS_STATES = {"needs_recheck"}
LEGACY_FRESHNESS_STATES = {"legacy_unverified", "not_snapshotted"}


@dataclass(frozen=True)
class QueryPlan:
    name: str
    query: str
    weight: float = 0.0
    match_mode: str | None = None


def _query_terms(query: str) -> list[str]:
    parts = [p.strip().lower() for p in re.split(r"[\s,，、;；:：]+", query or "") if p.strip()]
    if query.strip() and query.strip().lower() not in parts:
        parts.insert(0, query.strip().lower())
    deduped: list[str] = []
    for part in parts:
        if len(part) >= 2 and part not in deduped:
            deduped.append(part)
    return deduped


def _split_query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for part in re.split(r"[\s,，、;；:：()（）\[\]【】{}<>《》\"'`]+", query or ""):
        value = part.strip().lower()
        if len(value) >= 2 and value not in terms:
            terms.append(value)
    return terms


def _known_domain_terms(query: str) -> list[str]:
    lower = query.lower()
    compact = re.sub(r"[\s_-]+", "", lower)
    terms: list[str] = []
    for canonical, markers in KNOWN_DOMAIN_MARKERS.items():
        if any(marker in lower or re.sub(r"[\s_-]+", "", marker) in compact for marker in markers):
            terms.append(canonical)
    return terms


def _load_query_intents() -> list[dict[str, Any]]:
    path = Path(__file__).resolve().parent.parent / "references" / "query_intents.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _intent_plans(query: str) -> list[QueryPlan]:
    lower = query.lower()
    plans: list[QueryPlan] = []
    for item in _load_query_intents():
        markers = [str(value).strip().lower() for value in item.get("markers", []) if str(value).strip()]
        required_any = [str(value).strip().lower() for value in item.get("required_any", []) if str(value).strip()]
        negative_markers = [str(value).strip().lower() for value in item.get("negative_markers", []) if str(value).strip()]
        anchors = [str(value).strip() for value in item.get("anchors", []) if str(value).strip()]
        if not markers or not anchors or not any(marker in lower for marker in markers):
            continue
        if required_any and not any(marker in lower for marker in required_any):
            continue
        if negative_markers and any(marker in lower for marker in negative_markers):
            continue
        try:
            weight = float(item.get("weight") or 0.8)
        except (TypeError, ValueError):
            continue
        plans.append(
            QueryPlan(
                str(item.get("name") or "intent"),
                " ".join(anchors),
                weight,
                "any",
            )
        )
    return plans


def _query_planning_terms(query: str) -> list[str]:
    terms = _split_query_terms(query)
    domain_terms = _known_domain_terms(query)
    if "personal-kb" in domain_terms:
        terms = [term for term in terms if term not in {"kbskill", "personalkb"}]
    for token in re.findall(r"[a-z][a-z0-9_.-]*", query.lower()):
        if "personal-kb" in domain_terms and token in {"kbskill", "personalkb"}:
            continue
        if len(token) >= 2 and token not in terms:
            terms.append(token)
    for marker in domain_terms:
        if marker not in terms:
            terms.append(marker)
    return terms


def _paired_trigger_terms(entry: dict[str, Any], query: str) -> set[str]:
    """Two independent curated terms must identify the same title or alias."""
    triggers = entry.get("trigger_terms") or []
    if not isinstance(triggers, list):
        return set()
    found = {term for term in triggers if isinstance(term, str)
             and re.fullmatch(r"[\u4e00-\u9fff]{2,12}", term)
             and term in query and not _is_low_signal_term(term)
             and term not in CONTEXT_GENERIC_TERMS}
    # Counting a short word inside a longer match would turn one clue into two.
    independent = {term for term in found if not any(term != other and term in other for other in found)}
    if len(independent) < 2:
        return set()
    aliases = entry.get("aliases") or []
    phrases = [entry.get("title", ""), *(aliases if isinstance(aliases, list) else [])]
    for phrase in phrases:
        matching = {term for term in independent if term in str(phrase)}
        if len(matching) >= 2:
            return matching
    return set()


def _symbolic_trigger(phrase: str, text: str) -> bool:
    # Segmentation loses punctuation in compound notations and qualified identifiers.
    syntax = r"(?:[0-9]+(?:\+[0-9]+){2,}|[a-z][a-z0-9]*(?:[_.][a-z0-9]+)+)"
    return bool(re.fullmatch(syntax, phrase) and
                re.search(r"(?<![a-z0-9_.+])" + re.escape(phrase) + r"(?![a-z0-9_.+])", text))


def _literal_cjk_anchors(query: str) -> list[str]:
    """Recover explicit stored phrases inside an unsegmented Chinese question.

    Use literal phrases and corroborated short triggers from current records.
    """
    if not re.search(r"[\u4e00-\u9fff]", query):
        return []
    text = query.casefold()
    matches: set[str] = set()
    for path in sorted(storage_layout().records.rglob("kb.jsonl")):
        for entry in read_jsonl(path):
            if entry.get("_deleted") or entry.get("_archived") or _is_noncurrent(entry):
                continue
            matches.update(_paired_trigger_terms(entry, text))
            for field in ("aliases", "trigger_terms"):
                values = entry.get(field) or []
                if not isinstance(values, list):
                    continue
                for value in values:
                    phrase = str(value).strip().casefold()
                    if (4 <= len(re.findall(r"[\u4e00-\u9fff]", phrase)) and len(phrase) <= 64
                            and phrase in text and not _is_low_signal_term(phrase)) or _symbolic_trigger(phrase, text):
                        matches.add(phrase)
    return sorted(matches, key=lambda value: (-len(value), value))[:6]


CHAT_LINK = re.compile(r"(?:codex://(?:threads?|chats?)/|\bcodex\s+resume\s+)"
                       r"([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})(?![a-f0-9-])", re.I)


def _linked_records(query, project, include_noncurrent):
    ids = {value.lower() for value in CHAT_LINK.findall(query)}
    if not ids:
        return []
    found = []
    for path in sorted(storage_layout().records.rglob("kb.jsonl")):
        for entry in read_jsonl(path):
            if entry.get("_deleted") or entry.get("_archived") or (_is_noncurrent(entry) and not include_noncurrent):
                continue
            domain = entry.get("domain") or {}
            refs = [ref for obj in [entry, *domain.get("observations", []), *domain.get("relations", [])]
                    for ref in obj.get("evidence_refs", []) if ref.get("type") == "conversation"]
            linked = {value.lower() for ref in refs for value in CHAT_LINK.findall(str(ref.get("value", "")))}
            overlap = sorted(ids & linked)
            if overlap and project.eligible(entry, include_noncurrent=include_noncurrent):
                found.append((entry, {"type": "conversation", "threads": overlap}))
    return found


def _is_code_like(term: str) -> bool:
    return bool(re.search(r"[a-z][\w-]*\.(py|md|json|ya?ml|toml|java|ts|tsx|js|jsx)$", term)) or bool(
        re.search(r"[/\\_.:-]", term)
    )


def _is_low_signal_term(term: str) -> bool:
    value = term.strip().lower()
    if not value:
        return True
    if _is_code_like(value):
        return False
    if len(value) < 2:
        return True
    if value in LOW_SIGNAL_TERMS:
        return True
    return False


def _specific_terms(terms: list[str]) -> list[str]:
    out: list[str] = []
    for term in terms:
        value = term.strip().lower()
        if len(value) > 10 and re.search(r"[\u4e00-\u9fff]", value) and not _is_code_like(value):
            continue
        if value and not _is_low_signal_term(value) and value not in out:
            out.append(value)
    return out


def _contains_any(terms: list[str], values: set[str]) -> bool:
    return any(term in values for term in terms)


def _has_query_text(query: str, *needles: str) -> bool:
    lower = query.lower()
    return any(needle.lower() in lower for needle in needles)


def _is_kb_runtime_query(query: str) -> bool:
    # RAG/vector/knowledge-base describe many business systems, not this tool.
    return bool(re.search(
        r"personal[-_ ]?kb|kbskill|kb_(?:rag|closeout|search|session|audit)|"
        r"\bcloseout\b|session brief|记忆系统|知识库.{0,8}(?:加热|采用率)|"
        r"(?<![A-Za-z0-9_])KB(?=$|[^A-Za-z0-9_]).{0,16}(?:审计|优化|治理|加热|采用|检索|记忆)", query, re.I
    ))


def _is_non_kb_maintenance_query(query: str) -> bool:
    lower = query.lower()
    if _is_kb_runtime_query(query):
        return False
    return any(marker in lower for marker in NON_KB_MAINTENANCE_MARKERS)


def _is_artifact_query(query: str) -> bool:
    lower = query.lower()
    return any(marker.lower() in lower for marker in ARTIFACT_QUERY_MARKERS)


def _is_fault_query(query: str) -> bool:
    lower = query.lower()
    return any(marker.lower() in lower for marker in FAULT_QUERY_MARKERS)


def _is_prior_decision_query(query: str) -> bool:
    lower = query.lower()
    if any(marker.lower() in lower for marker in PRIOR_DECISION_MARKERS):
        return True
    if re.search(r"(?:last time|previously).{0,32}(?:agreed|decided|confirmed)", lower):
        return True
    if re.search(r"(?:agreed|decided|confirmed).{0,32}(?:last time|previously)", lower):
        return True
    ambiguous_history = any(marker in lower for marker in ("之前的那个", "前两天"))
    decision_context = any(
        marker in lower
        for marker in (
            "决定",
            "确认",
            "版本",
            "备选",
            "保留",
            "沿用",
            "口径",
            "方案",
            "简历",
            "工作经历",
            "项目经历",
        )
    )
    return ambiguous_history and decision_context


def _query_subjects(query: str) -> list[str]:
    residual = re.sub(
        r"(?:用|使用|通过|借助|让|请|using\s+|with\s+|via\s+)\s*"
        r"(?:codex|claude|gemini)\s*(?=(?:来|帮我|帮助我|to\s+)?\s*"
        r"(?:排查|检查|分析|修复|修改|实现|处理|查|看|写|执行|整理|检索|搜索|生成|debug|fix|review|run|write|search))",
        " ", query, flags=re.I,
    )
    return [name for name in PRODUCT_SUBJECTS
            if re.search(r"(?<![a-z0-9])" + name + r"(?![a-z0-9])", residual, re.I)]


def _concrete_query_terms(query: str) -> list[str]:
    subjects = _query_subjects(query)
    terms = [
        term
        for term in _specific_terms(_query_planning_terms(query))
        if term not in CONTEXT_GENERIC_TERMS or term in subjects
    ]
    compact = query.lower()
    if re.search(r"\bcc[\s_-]+switch\b", compact):
        terms = [term for term in terms if term not in {"cc", "switch", "cc_switch"}]
        if "cc-switch" not in terms:
            terms.insert(0, "cc-switch")
    return terms


def _decision_subject_terms(query: str) -> list[str]:
    cleaned = query.lower()
    for filler in sorted(DECISION_QUERY_FILLERS, key=len, reverse=True):
        cleaned = cleaned.replace(filler.lower(), " ")

    terms = _concrete_query_terms(cleaned)
    lower = query.lower()
    for marker in DECISION_TOPIC_MARKERS:
        if marker.lower() in lower and marker.lower() not in terms:
            terms.append(marker.lower())
    if "在线考试" in lower and "考试" not in terms:
        terms.append("考试")
    if "resume" in lower and "简历" not in terms:
        terms.insert(0, "简历")
    if "work experience" in lower and "工作经历" not in terms:
        terms.insert(0, "工作经历")
    if "project experience" in lower and "项目经历" not in terms:
        terms.insert(0, "项目经历")
    if any(marker in lower for marker in ("单列", "拆出", "独立项目")):
        for marker in ("简历结构", "工作经历", "项目经历"):
            if marker not in terms:
                terms.append(marker)
    return terms


def _is_decision_candidate(entry: dict[str, Any]) -> bool:
    if _status_value(entry) == "decision_confirmed":
        return True
    if entry.get("kind") != "requirement":
        return False
    return _has_explicit_decision_authority(entry)


def _has_explicit_decision_authority(entry: dict[str, Any]) -> bool:
    if entry.get("user_confirmed") is True:
        return True

    confirmed_by = str(entry.get("confirmed_by") or "").strip().lower()
    if confirmed_by in {"user", "the_user", "用户", "用户本人"}:
        return True

    for field in ("authority", "evidence_level", "decision_authority", "confirmation_source"):
        value = str(entry.get(field) or "").strip().lower()
        if value and any(marker in value for marker in DECISION_AUTHORITY_MARKERS):
            return True

    evidence_refs = entry.get("evidence_refs")
    if not isinstance(evidence_refs, list):
        return False
    for ref in evidence_refs:
        if not isinstance(ref, dict):
            continue
        ref_type = str(ref.get("type") or "").strip().lower()
        if ref_type not in {"conversation", "user_confirmation", "decision"}:
            continue
        text = " ".join(str(value) for value in ref.values() if value is not None).lower()
        if any(marker in text for marker in USER_CONFIRMATION_MARKERS):
            return True
    return False


def _term_matches_text(term: str, text: str) -> bool:
    value = term.strip().lower()
    haystack = text.lower()
    if not value:
        return False
    if re.fullmatch(r"[a-z0-9_.-]+", value):
        normalized_value = re.sub(r"[\s_-]+", "-", value)
        normalized_text = re.sub(r"[\s_-]+", "-", haystack)
        if len(normalized_value) <= 3:
            return bool(
                re.search(
                    rf"(?<![a-z0-9]){re.escape(normalized_value)}(?![a-z0-9])",
                    normalized_text,
                )
            )
        return normalized_value in normalized_text
    return value in haystack


def _matched_anchor_terms(entry: dict[str, Any], terms: list[str], fields: set[str]) -> set[str]:
    matched: set[str] = set()
    for field in fields:
        text = _field_text(entry.get(field))
        if not text:
            continue
        matched.update(term for term in terms if _term_matches_text(term, text))
    return matched


def _has_term_match(entry: dict[str, Any], terms: list[str], fields: set[str]) -> bool:
    return bool(_matched_anchor_terms(entry, terms, fields))


def _informative_subject_term(value: str) -> bool:
    pieces = re.findall(r"[a-z0-9_./:-]+|[\u4e00-\u9fff]+", value.casefold())
    return any(len(piece) >= 2 and not piece.isdigit()
               and piece not in SUBJECT_BROAD_TERMS and piece not in CONTEXT_GENERIC_TERMS
               and not _is_low_signal_term(piece) for piece in pieces)


def _subject_compatible(entry: dict[str, Any], query: str, subjects: list[str], topics: list[str]) -> bool:
    if not subjects:
        return True
    descriptive = (STRONG_FIELDS | MEDIUM_FIELDS) - {"source_paths", "key_files", "repo", "branch"}
    # A tool name appearing only in a storage path is not the record's subject.
    same_subject = any(re.search(r"(?<![a-z0-9])" + name + r"(?![a-z0-9])", _field_text(entry.get(field)), re.I)
                       for name in subjects for field in descriptive)
    if _has_term_match(entry, topics, STRONG_FIELDS | MEDIUM_FIELDS):
        return True
    # Stored aliases/trigger phrases can be embedded in unsegmented Chinese.
    # This retains topic-relevant cross-project experience without a product name.
    compact_query = re.sub(r"[\s_-]+", "", query.casefold())
    for field in ("aliases", "trigger_terms"):
        values = entry.get(field) or []
        for value in values if isinstance(values, list) else [values]:
            phrase = str(value).strip()
            if (_informative_subject_term(phrase)
                    and re.sub(r"[\s_-]+", "", phrase.casefold()) in compact_query):
                return True
    if not same_subject or topics:
        return False
    # Even when the query contains only broad topic words (e.g. Codex token),
    # the product name alone must not admit unrelated installation records.
    other_terms = [term for term in _specific_terms(_query_planning_terms(query))
                   if term not in subjects and term not in CONTEXT_GENERIC_TERMS]
    return not other_terms or _has_term_match(entry, other_terms, STRONG_FIELDS | MEDIUM_FIELDS)


def _explicit_alias_match(entry: dict[str, Any], terms: list[str]) -> bool:
    original = " ".join(terms)
    if _paired_trigger_terms(entry, original):
        return True
    triggers = entry.get("trigger_terms") or []
    if isinstance(triggers, list) and any(_symbolic_trigger(str(t).casefold(), original)
                                          for t in triggers if str(t).casefold() not in _scope_words(entry)):
        return True
    aliases = entry.get("aliases") or []
    for alias in aliases if isinstance(aliases, list) else [aliases]:
        phrase = str(alias).strip().casefold()
        if (len(re.findall(r"[\u4e00-\u9fff]", phrase)) >= 4
                and _informative_subject_term(phrase)
                and _term_matches_text(phrase, " ".join(terms))):
            return True
    return False


def _scope_words(entry: dict[str, Any]) -> set[str]:
    # Both a repository basename and its components identify where, not what.
    parts = re.split(r"[/\\\s]+", f"{entry.get('repo', '')} {entry.get('branch', '')}".casefold())
    return {word for part in parts for word in (part, *re.split(r"[_-]+", part)) if word}


def _prefer_learned_alias_to_repo_name(candidates: dict, owners: set[str]) -> None:
    if len(owners) != 1:
        return
    owner = candidates[next(iter(owners))]
    project_terms = _scope_words({"repo": owner.get("repo", "")})
    for item in candidates.values():
        if item["entry_id"] == owner["entry_id"]:
            continue
        anchors = {match["term"].casefold() for match in item.get("matched_anchors", [])}
        if anchors and anchors <= project_terms:
            item["_score"] = min(item["_score"], owner["_score"] - 0.01)


def _named_subtopic(entry: dict, terms: list[str], repo: str, branch: str) -> bool:
    """An explicitly named, curated operation inside a compound in-repository question."""
    if (entry.get("kind") != "requirement" or not repo or entry.get("repo") != repo
            or entry.get("branch") != branch):
        return False
    title = entry.get("title", "")
    aliases = entry.get("aliases") or []
    triggers = set(entry.get("trigger_terms") or [])
    return any(re.fullmatch(r"[一-鿿]{4,16}", term) and term in triggers
               and _informative_subject_term(term) and term in title
               and any(term in alias for alias in aliases) for term in terms)


def _guidance_has_task_topic(entry: dict[str, Any], terms: list[str]) -> bool:
    """Do not let a lone resource name admit unrelated maps or standing rules.

    Keep short lookups and exact code/path identifiers. For longer requests,
    require another original task anchor in descriptive fields, not merely a
    repository path or a term introduced by the query planner. Incident and
    implementation records retain their existing cross-project recall rules.
    """
    if entry.get("kind") not in {"map", "requirement"} or len(terms) < 3:
        return True
    descriptive = set(MATCH_FIELDS) - {"source_paths", "key_files", "repo", "branch"}
    matches = _matched_anchor_terms(entry, terms, descriptive) - _scope_words(entry)
    return len(matches) >= 2 or any(_is_code_like(term) for term in matches) or _explicit_alias_match(entry, terms)


def _topical_candidate(entry: dict[str, Any], terms: list[str], *, allow_paths: bool = False) -> bool:
    """Do not turn a project/path match into task relevance.

    Long diagnostic queries need two descriptive anchors, or an exact code
    identifier. Repository components describe scope, not a second topic.
    Short lookups stay recall biased. Story-only recall remains --include-weak.
    """
    concrete = [term for term in _specific_terms(terms)
                if term not in CONTEXT_GENERIC_TERMS and term not in PRODUCT_SUBJECTS]
    if len(concrete) < 3:
        return True
    fields = {"title", "aliases", "trigger_terms", "tags", "term", "definition", "key_facts"}
    if allow_paths:
        fields.update({"source_paths", "key_files"})
    matches = _matched_anchor_terms(entry, concrete, fields) - _scope_words(entry)
    if any(_is_code_like(term) for term in matches):
        return True
    # An explicit, informative stored phrase is already a precise anchor;
    # requiring a second unrelated word would lose transferable experience.
    if _explicit_alias_match(entry, terms):
        return True
    # Two shared words become weak evidence as a query adds more distinct
    # constraints (for example a product and a generic failure symptom).
    # Keep precise stored phrases and code identifiers above as recall paths.
    required = 3 if len(concrete) >= 6 else 2
    return len(matches) >= required


def _expanded_anchor_terms(
    terms: list[str],
    *,
    synonyms: dict[str, Any],
    enabled: bool,
) -> list[str]:
    expanded: list[str] = list(terms)
    if not enabled:
        return expanded
    for term in terms:
        for variant in expand_query(term, synonyms):
            value = variant.strip().lower()
            if value and not _is_low_signal_term(value) and value not in CONTEXT_GENERIC_TERMS and value not in expanded:
                expanded.append(value)
    return expanded


def _planner_anchor_terms(plan: QueryPlan) -> list[str]:
    if plan.name in PLANNER_ANCHOR_EXCLUDED_GROUPS:
        return []
    return _concrete_query_terms(plan.query)


def _effective_anchor_terms(
    original_terms: list[str],
    *,
    plan: QueryPlan,
    synonyms: dict[str, Any],
    should_expand: bool,
) -> list[str]:
    terms = _expanded_anchor_terms(original_terms, synonyms=synonyms, enabled=should_expand)
    for term in _planner_anchor_terms(plan):
        if term not in terms:
            terms.append(term)
    return terms


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    """Read a small precision-policy integer without trusting config input."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _config_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _safe_query_variants(
    query: str,
    *,
    anchor_terms: list[str],
    synonyms: dict[str, Any],
    max_variants: int = DEFAULT_MAX_QUERY_VARIANTS,
) -> list[str]:
    """Return bounded expansion variants that retain an original anchor.

    ``expand_query_variants`` contains useful translations/abbreviation/IP
    rewrites, but some rewrites replace the only concrete token.  RAG must not
    turn those rewrites into an unrelated broad query, so a missing anchor is
    put back explicitly.  The original query is always the first variant.
    """
    limit = _bounded_int(max_variants, DEFAULT_MAX_QUERY_VARIANTS, minimum=1, maximum=8)
    original = " ".join(str(query or "").split())
    if not original:
        return []

    anchors = [
        term.strip().lower()
        for term in anchor_terms
        if term and not _is_low_signal_term(term)
    ]
    variants: list[str] = []

    def add(value: str) -> None:
        normalized = " ".join(str(value or "").split())
        if not normalized or normalized.casefold() in {item.casefold() for item in variants}:
            return
        if anchors and not any(_term_matches_text(anchor, normalized) for anchor in anchors):
            # Keep the most useful original anchor visible in every variant.
            normalized = f"{anchors[0]} {normalized}"
        if normalized.casefold() not in {item.casefold() for item in variants}:
            variants.append(normalized)

    add(original)
    try:
        generated = expand_query_variants(original)
    except Exception:
        generated = []
    for value in generated:
        add(value)
        if len(variants) >= limit:
            return variants[:limit]

    # ``expand_query_variants`` handles phrase rewrites, while the synonym
    # table handles project/user shorthand (for example 数据库 -> mysql).
    for anchor in anchors:
        try:
            synonyms_for_anchor = expand_query(anchor, synonyms)
        except Exception:
            synonyms_for_anchor = []
        for synonym in synonyms_for_anchor[1:]:
            add(f"{original} {synonym}")
            if len(variants) >= limit:
                return variants[:limit]
    return variants[:limit]


def _map_has_concrete_anchor(
    entry: dict[str, Any],
    query: str,
    *,
    anchor_terms: list[str] | None = None,
) -> bool:
    terms = anchor_terms if anchor_terms is not None else _concrete_query_terms(query)
    return bool(terms) and _has_term_match(entry, terms, MAP_ANCHOR_FIELDS)


def _session_brief_has_concrete_anchor(
    item: dict[str, Any],
    query: str,
    *,
    anchor_terms: list[str] | None = None,
) -> bool:
    terms = anchor_terms if anchor_terms is not None else _concrete_query_terms(query)
    if not terms:
        return False
    return _has_term_match(item, terms, SESSION_BRIEF_ANCHOR_FIELDS)


def _append_warning(item: dict[str, Any], warning: str) -> None:
    existing = str(item.get("warning") or "").strip()
    if warning in existing:
        return
    item["warning"] = f"{existing}; {warning}" if existing else warning


def _add_plan(plans: list[QueryPlan], seen: set[str], plan: QueryPlan) -> None:
    query = " ".join(plan.query.split())
    if not query:
        return
    key = query.lower()
    if key in seen:
        return
    plans.append(QueryPlan(plan.name, query, plan.weight, plan.match_mode))
    seen.add(key)


def _plan_query_groups(
    query: str,
    *,
    enabled: bool = True,
    max_plans: int = DEFAULT_MAX_QUERY_PLANS,
    literal_anchors: list[str] | None = None,
) -> list[QueryPlan]:
    if not enabled:
        return [QueryPlan("task", query, 0.0, None)]

    split_terms = _query_planning_terms(query)
    specific = _specific_terms(split_terms)
    hard = [term for term in specific if _is_code_like(term) or any(ch.isdigit() for ch in term)]

    plans: list[QueryPlan] = []
    seen: set[str] = set()

    is_kb_task = _is_kb_runtime_query(query)

    # Domain-specific groups go first. They prevent broad terms such as "agent" or
    # "面试材料" from filling the result window before the real anchor is searched.
    for intent_plan in _intent_plans(query):
        if intent_plan.name == "retrieval-expansion" and hard:
            intent_plan = QueryPlan(
                intent_plan.name,
                " ".join([*hard[:3], intent_plan.query]),
                intent_plan.weight,
                intent_plan.match_mode,
            )
        _add_plan(plans, seen, intent_plan)

    if _is_prior_decision_query(query):
        subject_terms = _decision_subject_terms(query)
        if subject_terms:
            _add_plan(
                plans,
                seen,
                QueryPlan(
                    "prior-decision",
                    " ".join([*subject_terms[:6], "稳定决策", "已确认", "口径"]),
                    1.10,
                    "any",
                ),
            )

    if is_kb_task:
        detail_terms = [term for term in specific if term not in {"personal-kb"}]
        if detail_terms:
            _add_plan(plans, seen, QueryPlan("personal-kb", "personal-kb " + " ".join(detail_terms[:5]), 0.70, "any"))
        else:
            _add_plan(plans, seen, QueryPlan("personal-kb", "personal-kb", 0.45, "any"))

    if _contains_any(split_terms, {"closeout"}) or _has_query_text(query, "加热", "使用后", "post-use"):
        _add_plan(plans, seen, QueryPlan("closeout", "personal-kb closeout kb_closeout.py 使用后加热记录", 0.90, "any"))

    if is_kb_task and (_contains_any(split_terms, {"rag"}) or _has_query_text(query, "检索增强", "使用前检索", "kb_rag_context")):
        _add_plan(plans, seen, QueryPlan("rag-runtime", "personal-kb RAG kb_rag_context.py 使用前检索", 0.85, "any"))

    if is_kb_task and (_contains_any(split_terms, {"sidecar", "vector", "embedding"}) or _has_query_text(query, "向量")):
        _add_plan(plans, seen, QueryPlan("vector-sidecar", "personal-kb vector sidecar 向量 embedding", 0.75, "any"))

    if is_kb_task and _has_query_text(query, "系统提示词", "agents.md", "claude.md", "entrypoint", "真实入口"):
        _add_plan(
            plans,
            seen,
            QueryPlan("prompt-entrypoint", "系统提示词.md AGENTS.md CLAUDE.md entrypoint personal-kb", 0.85, "any"),
        )

    if is_kb_task and _has_query_text(query, "子 agent", "子agent", "subagent", "父会话", "父 会话", "scout"):
        _add_plan(plans, seen, QueryPlan("subagent", "personal-kb subagent parent scout closeout", 0.55, "any"))

    if hard:
        _add_plan(plans, seen, QueryPlan("hard-anchors", " ".join(hard[:6]), 0.60, "any"))
    if specific:
        _add_plan(plans, seen, QueryPlan("specific-terms", " ".join(specific[:6]), 0.35, "any"))

    if literal_anchors:
        _add_plan(plans, seen, QueryPlan("literal-anchors", " ".join(literal_anchors), 0.80, "any"))

    # Always retain the raw task query, but keep the total plan budget small.
    # Specialized plans are ranked by their declared confidence; insertion
    # order breaks ties so intent/prior-decision plans remain deterministic.
    task_plan = QueryPlan("task", query, 0.0, None)
    non_task = [(index, plan) for index, plan in enumerate(plans) if plan.name != "task"]
    non_task.sort(key=lambda pair: (-pair[1].weight, pair[0]))
    plan_limit = _bounded_int(max_plans, DEFAULT_MAX_QUERY_PLANS, minimum=1, maximum=8)
    selected = [plan for _index, plan in non_task[: max(0, plan_limit - 1)]]
    selected.sort(key=lambda plan: next(index for index, candidate in enumerate(plans) if candidate is plan))
    selected.append(task_plan)
    return selected[:plan_limit]


def _field_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item is not None)
    if isinstance(value, dict):
        return " ".join(str(v) for v in value.values() if v is not None)
    return "" if value is None else str(value)


def _status_value(entry: dict[str, Any]) -> str:
    value = entry.get("status")
    return value.strip().lower().replace("-", "_") if isinstance(value, str) else ""


def _is_noncurrent(entry: dict[str, Any]) -> bool:
    return bool(entry.get("superseded_by")) or _status_value(entry) not in CURRENT_STATUSES


def _matched_fields(entry: dict[str, Any], terms: list[str]) -> list[str]:
    if not terms:
        return []
    matched: list[str] = []
    for field in MATCH_FIELDS:
        text = _field_text(entry.get(field))
        if text and any(_term_matches_text(term, text) for term in terms):
            matched.append(field)
    return matched


def _matched_terms_by_field(entry: dict[str, Any], terms: list[str]) -> dict[str, list[str]]:
    term_values = []
    for term in terms:
        value = term.strip().lower()
        if value and value not in term_values:
            term_values.append(value)
    if not term_values:
        return {}

    matched: dict[str, list[str]] = {}
    for field in MATCH_FIELDS:
        text = _field_text(entry.get(field)).lower()
        if not text:
            continue
        hits = [term for term in term_values if _term_matches_text(term, text)]
        if hits:
            matched[field] = hits
    return matched


def _matched_anchor_details(
    entry: dict[str, Any],
    terms: list[str],
    *,
    fields: tuple[str, ...] = MATCH_FIELDS,
) -> list[dict[str, str]]:
    """Explain concrete lexical anchors without exposing the whole record."""
    specific = set(_specific_terms(terms))
    details: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for field in fields:
        text = _field_text(entry.get(field))
        if not text:
            continue
        for term in terms:
            value = term.strip().lower()
            if not value or (specific and value not in specific):
                continue
            key = (value, field)
            if key in seen or not _term_matches_text(value, text):
                continue
            seen.add(key)
            details.append({"term": value, "field": field})
    return details


def _match_quality(
    entry: dict[str, Any],
    *,
    terms: list[str],
    include_weak: bool,
) -> tuple[bool, float, str]:
    matched = _matched_terms_by_field(entry, terms)
    if not matched:
        return False, 0.0, "no lexical match"

    specific = set(_specific_terms(terms))
    strong_specific = []
    medium_specific = []
    weak_specific = []
    score = 0.0

    for field, hits in matched.items():
        if field in STRONG_FIELDS:
            field_weight = 5.0
        elif field in MEDIUM_FIELDS:
            field_weight = 3.0
        else:
            field_weight = 0.8

        for term in hits:
            is_specific = term in specific
            score += field_weight if is_specific else min(field_weight, 0.45)
            if not is_specific:
                continue
            if field in STRONG_FIELDS:
                strong_specific.append(term)
            elif field in MEDIUM_FIELDS:
                medium_specific.append(term)
            else:
                weak_specific.append(term)

    if specific:
        if strong_specific:
            return True, score + 5.0, "strong specific field match"
        if medium_specific:
            return True, score + 3.0, "medium specific field match"
        if weak_specific and include_weak:
            return True, score, "weak specific field match allowed"
        return False, score, "only weak or generic fields matched"

    if any(field in STRONG_FIELDS or field in MEDIUM_FIELDS for field in matched):
        return True, score, "generic strong/medium field match"
    return include_weak, score, "generic weak-only match"


def _confidence(
    fields: list[str],
    entry: dict[str, Any],
    quality_score: float = 0.0,
    *,
    matched_anchor_count: int = 0,
) -> float:
    """Estimate lexical match quality (not whether the historical fact is true).

    The old formula saturated at ``0.95`` for nearly every strong record.  A
    smaller additive scale preserves the compatibility alias while making a
    one-token match visibly weaker than a multi-anchor match.
    """
    if not fields:
        return 0.35

    score = 0.35

    if any(field in STRONG_FIELDS for field in fields):
        score += 0.22
    if any(field in MEDIUM_FIELDS for field in fields):
        score += 0.12
    if any(field in WEAK_FIELDS for field in fields):
        score += 0.04
    score += min(0.16, max(0.0, quality_score) / 50.0)
    score += min(0.12, max(0, matched_anchor_count - 1) * 0.04)
    if entry.get("source") in {"kb", "parent"}:
        score += 0.02
    if entry.get("_cross_project"):
        score -= 0.08

    return round(max(0.30, min(score, 0.94)), 2)


def _clip(text: str, limit: int) -> str:
    text = " ".join(line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _summary(entry: dict[str, Any], limit: int) -> str:
    chunks: list[str] = []
    for field in SUMMARY_FIELDS:
        value = _field_text(entry.get(field)).strip()
        if value:
            chunks.append(value)
    if not chunks:
        return ""
    return _clip(" ".join(chunks), limit)


def _cross_project_map_summary(entry: dict[str, Any], limit: int) -> str:
    title = str(entry.get("title") or "").strip()
    repo = str(entry.get("repo") or entry.get("_from_project") or "").strip()
    branch = str(entry.get("branch") or "").strip()
    coordinate = "@".join(part for part in (repo, branch) if part)
    parts = [f"定位映射：{title}" if title else "定位映射"]
    if coordinate:
        parts.append(f"来源：{coordinate}")
    return _clip("；".join(parts), limit)


def _trust_tier(freshness_state: str) -> str:
    if freshness_state in RELIABLE_FRESHNESS_STATES:
        return "verified"
    if freshness_state in RECHECK_FRESHNESS_STATES:
        return "recheck"
    if freshness_state in HARD_FRESHNESS_STATES:
        return "blocked"
    return "legacy"


def _freshness_rank(freshness_state: str) -> float:
    """Make evidence freshness dominate heat and tiny lexical score ties."""
    return {
        "fresh": 2.8,
        "runtime_recent": 2.4,
        "needs_recheck": -0.8,
        "legacy_unverified": -1.6,
        "not_snapshotted": -1.9,
        "dirty_worktree": -5.0,
        "diverged": -5.0,
        "unresolvable": -5.0,
        "conflicted": -5.0,
    }.get(freshness_state, -2.0)


def _safe_evidence_verification(entry: dict[str, Any]) -> dict[str, Any]:
    try:
        return kb_evidence.verify_entry_evidence(entry)
    except Exception as exc:  # malformed historical rows must not break RAG
        return {
            "state": "unresolvable",
            "scope": "local_head",
            "applicability_state": "needs_review",
            "items": [],
            "warning": f"unresolvable: evidence verification failed ({type(exc).__name__})",
        }


def _title_similarity(left: str, right: str) -> float:
    a = re.sub(r"[^\w\u4e00-\u9fff]+", " ", left.casefold()).strip()
    b = re.sub(r"[^\w\u4e00-\u9fff]+", " ", right.casefold()).strip()
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _similar_result(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_entity = left.get("domain", {}).get("entity_id")
    right_entity = right.get("domain", {}).get("entity_id")
    if left_entity or right_entity:
        return bool(left_entity and left_entity == right_entity)
    same_scope = (
        str(left.get("repo") or "").casefold(),
        str(left.get("branch") or "").casefold(),
    ) == (
        str(right.get("repo") or "").casefold(),
        str(right.get("branch") or "").casefold(),
    )
    same_kind = str(left.get("kind") or "") == str(right.get("kind") or "")
    # One evidence file can support several independent conclusions.
    if not same_scope or not same_kind:
        return False
    title_left = str(left.get("title") or "")
    title_right = str(right.get("title") or "")
    normalized_left = re.sub(r"[^\w\u4e00-\u9fff]+", "", title_left.casefold())
    normalized_right = re.sub(r"[^\w\u4e00-\u9fff]+", "", title_right.casefold())
    if normalized_left and normalized_left == normalized_right:
        return True
    return _title_similarity(title_left, title_right) >= 0.88


@timed("retrieve.dedupe")
def _dedupe_similar_items(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    kept: list[dict[str, Any]] = []
    removed = 0
    for item in items:
        if any(_similar_result(item, existing) for existing in kept):
            removed += 1
            continue
        kept.append(item)
    return kept, removed


@timed("retrieve.filter_freshness")
def _filter_freshness_items(
    items: list[dict[str, Any]],
    *,
    include_noncurrent: bool,
    legacy_fallback: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """Keep complementary legacy leads; prefer reliable equivalents in their scope."""
    if include_noncurrent:
        return items, 0
    safe = [item for item in items if item.get("freshness_state") not in HARD_FRESHNESS_STATES]
    reliable = [
        item for item in safe
        if item.get("freshness_state") in RELIABLE_FRESHNESS_STATES
        and item.get("kind") != "session_brief"
        and item.get("freshness_scope") != "session_brief_window"
    ]
    retained = []
    for item in safe:
        state = item.get("freshness_state")
        if state in LEGACY_FRESHNESS_STATES:
            if not legacy_fallback or any(_similar_result(item, current) for current in reliable):
                continue
            _append_warning(item, "legacy evidence: verify this historical lead against current sources")
        elif state not in RELIABLE_FRESHNESS_STATES | RECHECK_FRESHNESS_STATES:
            continue
        retained.append(item)
    return retained, len(items) - len(retained)


@timed("retrieve.diversity")
def _apply_result_diversity(
    items: list[dict[str, Any]],
    *,
    artifact_query: bool,
    fault_query: bool,
    kb_runtime_query: bool,
    map_limit: int = 1,
) -> tuple[list[dict[str, Any]], int]:
    """Keep locator maps from occupying an ordinary business context window."""
    if artifact_query or fault_query or kb_runtime_query:
        return items, 0
    map_limit = _bounded_int(map_limit, 1, minimum=0, maximum=5)
    map_count = 0
    kept: list[dict[str, Any]] = []
    removed = 0
    for item in items:
        if item.get("kind") == "map":
            if map_count >= map_limit:
                removed += 1
                continue
            map_count += 1
        kept.append(item)
    return kept, removed


def _short_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        if len(out) >= limit:
            break
    return out


def _item_char_size(item: dict[str, Any]) -> int:
    return len(json.dumps(item, ensure_ascii=False, sort_keys=True))


def _fit_item_to_char_budget(item: dict[str, Any], limit: int) -> dict[str, Any] | None:
    if limit <= 0:
        return None
    current = dict(item)
    if _item_char_size(current) <= limit:
        return current

    summary = str(item.get("summary", "") or "")
    if not summary:
        return None

    low = 0
    high = len(summary)
    best: dict[str, Any] | None = None
    while low <= high:
        mid = (low + high) // 2
        candidate = dict(item)
        candidate["summary"] = _clip(summary, mid) if mid > 0 else ""
        size = _item_char_size(candidate)
        if size <= limit:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def _apply_total_char_budget(items: list[dict[str, Any]], *, max_total_chars: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if max_total_chars <= 0:
        return items, None

    kept: list[dict[str, Any]] = []
    used_chars = 0
    clipped_last_item = False
    omitted_items = 0

    for index, item in enumerate(items):
        remaining = max_total_chars - used_chars
        if remaining <= 0:
            omitted_items = len(items) - index
            break

        candidate = _fit_item_to_char_budget(item, remaining)
        if candidate is None:
            omitted_items = len(items) - index
            break

        clipped_last_item = clipped_last_item or candidate.get("summary") != item.get("summary")
        kept.append(candidate)
        used_chars += _item_char_size(candidate)
        if candidate.get("summary") != item.get("summary"):
            omitted_items = len(items) - index - 1
            break

    if omitted_items <= 0 and not clipped_last_item:
        return kept, None
    return kept, {
        "max_total_chars": max_total_chars,
        "returned_item_chars": used_chars,
        "omitted_items": max(0, omitted_items),
        "clipped_last_item": clipped_last_item,
    }


def _recent_session_rank(item: dict[str, Any], *, index: int) -> float:
    raw_score = float(item.get("_score") or 0.0)
    confidence = float(item.get("confidence") or 0.0)
    return round(6.0 + raw_score * 0.55 + confidence * 2.4 - index * 0.05, 4)


def _is_personal_kb_runtime_entry(entry: dict[str, Any]) -> bool:
    combined = " ".join(
        _field_text(entry.get(field)).lower()
        for field in ("title", "tags", "aliases", "trigger_terms", "source_paths", "key_files")
    )
    return any(marker in combined for marker in KB_RUNTIME_MARKERS)


def _compact_entry(
    raw_entry: dict[str, Any],
    formatted_entry: dict[str, Any],
    *,
    terms: list[str],
    max_snippet_chars: int,
    quality_score: float = 0.0,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fields = _matched_fields(raw_entry, terms)
    warnings: list[str] = []
    warning = formatted_entry.get("_warning") or raw_entry.get("_warning") or ""
    if warning:
        warnings.append(str(warning))
    status = _status_value(formatted_entry) or _status_value(raw_entry)
    superseded_by = formatted_entry.get("superseded_by") or raw_entry.get("superseded_by")
    if status and status not in CURRENT_STATUSES:
        warnings.append(f"non-current KB record: status={status}")
    if superseded_by:
        warnings.append(f"superseded_by={superseded_by}")
    is_cross_project = bool(formatted_entry.get("_cross_project"))
    freshness = evidence if evidence is not None else _safe_evidence_verification(raw_entry)
    freshness_state = str(freshness.get("state") or "legacy_unverified")
    if freshness_state != "fresh" and freshness.get("warning"):
        warnings.append(str(freshness["warning"]))

    summary_entry = formatted_entry
    if is_cross_project and formatted_entry.get("kind") == "map" and not formatted_entry.get("_evidence_lead"):
        summary_entry = {**formatted_entry, "story": ""}

    matched_anchors = _matched_anchor_details(raw_entry, terms)
    retrieval_score = _confidence(
        fields,
        formatted_entry,
        quality_score,
        matched_anchor_count=len({item["term"] for item in matched_anchors}),
    )
    anchor_text = [f"{item['term']} ({item['field']})" for item in matched_anchors[:6]]
    item = {
        "entry_id": formatted_entry.get("id") or raw_entry.get("id") or "",
        "kind": formatted_entry.get("kind") or raw_entry.get("kind") or "",
        "title": formatted_entry.get("title") or raw_entry.get("title") or "",
        "repo": formatted_entry.get("repo") or raw_entry.get("repo") or "",
        "branch": formatted_entry.get("branch") or raw_entry.get("branch") or "",
        # confidence is retained as a compatibility alias. It is query match
        # quality, not the probability that the historical fact is still true.
        "retrieval_score": retrieval_score,
        "confidence": retrieval_score,
        "freshness_state": freshness_state,
        "freshness_scope": freshness.get("scope", "local_head"),
        "evidence_strength": kb_evidence.evidence_strength(raw_entry, freshness),
        "trust_tier": _trust_tier(freshness_state),
        "applicability_state": (
            "cross_project_review" if is_cross_project else freshness.get("applicability_state", "unknown")
        ),
        "why_matched": (
            "matched anchors: " + ", ".join(anchor_text)
            if anchor_text
            else ("matched fields: " + ", ".join(fields[:6]) if fields else "ranked by KB search")
        ),
        "matched_fields": fields,
        "matched_anchors": matched_anchors[:12],
        "summary": _summary(summary_entry, max_snippet_chars),
        "source_paths": [] if is_cross_project and not formatted_entry.get("_evidence_lead") else _short_list(formatted_entry.get("source_paths", raw_entry.get("source_paths")), 3),
        "key_files": [] if is_cross_project and not formatted_entry.get("_evidence_lead") else _short_list(formatted_entry.get("key_files", raw_entry.get("key_files")), 3),
    }
    if is_cross_project and item["kind"] == "map" and not item["summary"]:
        item["summary"] = _cross_project_map_summary(formatted_entry, max_snippet_chars)
    if warnings:
        item["warning"] = "; ".join(warnings)
    if is_cross_project:
        item["cross_project"] = True
        item["from_project"] = formatted_entry.get("_from_project") or raw_entry.get("repo", "")
    if raw_entry.get("domain"):
        item["domain"] = kb_project_view.compact_domain(raw_entry)
        item["evidence_refs"] = (raw_entry.get("evidence_refs") or [])[:3]
    if formatted_entry.get("_same_project"):
        item["same_project"] = True
    if formatted_entry.get("_domain_history"):
        item["applicability_state"] = "historical_or_out_of_scope"
        _append_warning(item, "historical, candidate, or out-of-scope domain record; do not apply as a current requirement")
    return item


def _parse_allowed_kinds(kind_filter: str, legacy_type_filter: str) -> list[str]:
    if legacy_type_filter.strip():
        legacy_kinds = parse_legacy_type_filter(legacy_type_filter)
        if kind_filter.strip():
            return list(dict.fromkeys([*parse_kind_filter(kind_filter), *legacy_kinds]))
        return legacy_kinds
    return parse_kind_filter(kind_filter)


def _recent_cutoff(value: str) -> datetime | None:
    if not value:
        return None
    if not kb_search.TIME_INDEX_AVAILABLE or not kb_search.parse_recent_param:
        raise ValueError("--recent requires time index support")
    days = kb_search.parse_recent_param(value)
    return datetime.now() - timedelta(days=days)


@timed("retrieve.build")
@kb_git_reads.read_scope()
@kb_evidence.with_verification_scope
def build_context(args: argparse.Namespace) -> dict[str, Any]:
    if args.min_confidence < 0.0 or args.min_confidence > 1.0:
        raise ValueError("--min-confidence must be between 0 and 1")
    if args.max_total_chars < 0:
        raise ValueError("--max-total-chars must be >= 0")
    if getattr(args, "no_domain", False) and (getattr(args, "project_id", "") or getattr(args, "project_view", False)):
        raise ValueError("--no-domain cannot be combined with --project-id or --project-view")
    retrieval_id = _retrieval_id(getattr(args, "retrieval_id", ""))
    scope_anchors = [str(value).strip() for value in (getattr(args, "scope_anchor", []) or []) if str(value).strip()]
    _validate_scope_anchors(args.query, scope_anchors)

    original_query = args.query
    clean_query, link_titles = kb_project_routing.chat_titles(original_query)
    if link_titles and not getattr(args, "no_domain", False):
        args = argparse.Namespace(**vars(args))
        args.query = clean_query
    as_of = None
    period = getattr(args, "as_of", "")
    if not period and not getattr(args, "no_domain", False) and any(
            word in args.query for word in ("当时", "截至", "那天", "那一天")) and not any(
            word in args.query for word in ("现在", "如今", "目前")):
        dates = re.findall(r"(?<!\d)(\d{4})\s*[-年]\s*(\d{1,2})\s*[-月]\s*(\d{1,2})(?!\d)", args.query)
        if len(dates) == 1:
            year, month, day = map(int, dates[0])
            period = f"{year:04d}-{month:02d}-{day:02d}"
    if period:
        from kb_timeline import bounds
        from zoneinfo import ZoneInfo
        if args.no_domain:
            raise ValueError("--as-of requires domain history")
        history_zone = ZoneInfo("Asia/Shanghai")
        as_of = bounds(period, history_zone)[1]
    evidence_cache: dict[str, dict[str, Any]] = {}

    def verify_once(entry: dict) -> dict:
        key = str(entry.get("id") or "")
        if key not in evidence_cache:
            count("evidence.entry_cache_misses")
            evidence_cache[key] = _safe_evidence_verification(entry)
        else:
            count("evidence.entry_cache_hits")
        return evidence_cache[key]

    synonyms = load_synonyms()
    explicit_repo = args.repo
    graph = None
    routing = {}
    if not getattr(args, "no_domain", False):
        graph = kb_domain.load_graph()
        if not (period or args.project_id):
            with span("retrieve.project_routing"):
                routing = kb_project_routing.suggest(
                    original_query, graph, cwd=None if args.global_search else Path.cwd(),
                    files=getattr(args, "current_file", ()), verify=verify_once, synonyms=synonyms,
                    scope={"repo_id": explicit_repo, "branch": args.branch, "environment": args.environment},
                )
            if routing.get("state") == "use" and not (args.global_search or explicit_repo):
                selected = routing["candidates"][0]
                # Route only this read. A repository choice is not a graph edge
                # and does not authorize writes or assert historical applicability.
                args = argparse.Namespace(**vars(args))
                args.repo = selected["repo"]
                args.branch = args.branch or kb_project_routing.search_branch(selected, graph)

    allowed_kinds = _parse_allowed_kinds(args.kind_filter, args.legacy_type_filter)
    config = load_config()
    expand_requested = bool(args.expand or config.get("search", {}).get("expand_queries", False))
    precision_config = config.get("search", {}).get("precision", {})
    max_query_variants = _bounded_int(
        precision_config.get("max_query_variants"),
        DEFAULT_MAX_QUERY_VARIANTS,
        minimum=1,
        maximum=8,
    )
    max_query_plans = _bounded_int(
        precision_config.get("max_query_plans"),
        DEFAULT_MAX_QUERY_PLANS,
        minimum=1,
        maximum=8,
    )
    legacy_fallback = _config_bool(precision_config.get("legacy_fallback"), True)
    map_results_per_query = _bounded_int(
        precision_config.get("map_results_per_query"),
        1,
        minimum=0,
        maximum=5,
    )
    is_kb_runtime_query = _is_kb_runtime_query(args.query)
    non_kb_maintenance_query = _is_non_kb_maintenance_query(args.query)
    artifact_query = _is_artifact_query(args.query)
    fault_query = _is_fault_query(args.query)
    prior_decision_query = _is_prior_decision_query(args.query)
    decision_subject_terms = _decision_subject_terms(args.query) if prior_decision_query else []
    concrete_query_terms = (
        decision_subject_terms if prior_decision_query else _concrete_query_terms(args.query)
    )
    with span("retrieve.literal_anchors"):
        literal_anchors = [] if args.no_plan else _literal_cjk_anchors(args.query)
    concrete_query_terms = list(dict.fromkeys([*concrete_query_terms, *literal_anchors]))
    # The URI scheme names the chat client, not the subject of the linked work.
    subjects = _query_subjects(CHAT_LINK.sub(" ", args.query))
    subject_topics = [term for term in concrete_query_terms
                      if term not in subjects and _informative_subject_term(term)]

    search_args = argparse.Namespace(
        global_search=args.global_search,
        search_in=args.search_in,
        repo=args.repo,
        branch=args.branch,
        match_mode=args.match_mode,
    )
    recent_cutoff = _recent_cutoff(args.recent)
    requested_limit = max(0, args.limit)
    search_limit = requested_limit if args.include_noncurrent else max(requested_limit * 4, requested_limit + 10)

    plans = _plan_query_groups(
        args.query,
        enabled=not args.no_plan,
        max_plans=max_query_plans,
        literal_anchors=literal_anchors,
    )
    session_anchor_terms: list[str] = []
    for plan in plans:
        for term in _effective_anchor_terms(
            concrete_query_terms,
            plan=plan,
            synonyms=synonyms,
            # Session briefs are short-lived context; require an original
            # concrete anchor before any optional synonym expansion.
            should_expand=False,
        ):
            if term not in session_anchor_terms:
                session_anchor_terms.append(term)
    original_terms = list(dict.fromkeys([*_query_planning_terms(args.query), *literal_anchors]))
    required_tags = kb_search._parse_tags(args.tags)
    reference_repo = None
    reference_branch = args.branch
    candidates: dict[str, dict[str, Any]] = {}
    rejected_count = 0
    rejected_nonactionable_count = 0
    query_group_payload: list[dict[str, str]] = []

    if not args.no_session_briefs:
        query_group_payload.append({"name": "recent-session", "query": args.query})
        with span("retrieve.session_briefs"):
            recent_briefs = kb_session_brief.search_recent_briefs(
                args.query,
                cwd=Path.cwd(),
                repo_name_override=(args.repo.strip() or None),
                branch_override=(args.branch.strip() or None),
                recent_days=max(1, args.session_brief_days),
                limit=min(args.session_brief_limit, requested_limit),
                include_cross_repo=args.global_search,
                max_snippet_chars=max(80, args.max_snippet_chars),
            )
        for index, item in enumerate(recent_briefs):
            if not args.include_weak and not _subject_compatible(item, args.query, subjects, subject_topics):
                count("retrieve.rejected_subject")
                rejected_count += 1
                continue
            entry_id = item.get("entry_id")
            if not entry_id:
                continue
            if not _session_brief_has_concrete_anchor(
                item,
                args.query,
                anchor_terms=session_anchor_terms,
            ):
                rejected_count += 1
                continue
            item["retrieval_score"] = float(item.get("confidence") or 0.0)
            item["freshness_state"] = "runtime_recent"
            item["freshness_scope"] = "session_brief_window"
            item["evidence_strength"] = "runtime"
            item["trust_tier"] = "verified"
            item["applicability_state"] = "current"
            item.setdefault("matched_anchors", [])
            item.setdefault("why_matched", "matched recent session anchor")
            item["query_groups"] = ["recent-session"]
            item["_score"] = _recent_session_rank(item, index=index)
            candidates[entry_id] = item

    # Search the raw user query first.  Planner groups are still useful, but
    # they must not hide the exact wording that established the retrieval
    # budget.  Expansion is considered only after this first pass.
    execution_plans: list[QueryPlan] = []
    seen_execution_queries: set[str] = set()
    for plan in [plan for plan in plans if plan.name == "task"] + [
        plan for plan in plans if plan.name != "task"
    ]:
        key = " ".join(plan.query.split()).casefold()
        if key in seen_execution_queries:
            continue
        seen_execution_queries.add(key)
        execution_plans.append(plan)
    search_batches: list[dict[str, Any]] = []
    for plan_index, plan in enumerate(execution_plans):
        query_group_payload.append({"name": plan.name, "query": plan.query})
        group_args = argparse.Namespace(
            global_search=args.global_search,
            search_in=args.search_in,
            repo=args.repo,
            branch=args.branch,
            match_mode=plan.match_mode or args.match_mode,
        )
        stderr_capture = io.StringIO()
        stderr_context = contextlib.nullcontext() if args.debug else contextlib.redirect_stderr(stderr_capture)
        with stderr_context, span("retrieve.search_raw"):
            raw_results, ctx = kb_search._search_once(
                query=plan.query,
                args=group_args,
                allowed_kinds=set(allowed_kinds) if allowed_kinds else None,
                required_tags=required_tags,
                recent_cutoff=recent_cutoff,
                # First pass is deliberately unexpanded.
                should_expand=False,
                synonyms=synonyms,
                limit=search_limit,
            )
        if reference_repo is None and not args.global_search and ctx is not None:
            reference_repo = ctx.repo_name
            reference_branch = args.branch or ctx.branch
        search_batches.append(
            {
                "plan_index": plan_index,
                "plan": plan,
                "query": plan.query,
                "expanded": False,
                "variant_index": 0,
                "raw_results": raw_results,
            }
        )

    project_context = None
    if not getattr(args, "no_domain", False):
        with span("retrieve.domain_init"):
            project_context = kb_project_view.ProjectContext(
                args.query, repo=reference_repo or args.repo, branch=reference_branch,
                environment=getattr(args, "environment", ""),
                project_id=getattr(args, "project_id", ""), verify=verify_once, as_of=as_of,
                graph=graph, clarify=not explicit_repo and routing.get("state") == "clarify",
            )

    def format_entry(raw: dict) -> dict:
        if project_context and project_context.same_project(raw):
            return {**raw, "_same_project": True}
        if (project_context and args.include_noncurrent and raw.get("domain")
                and raw["domain"]["project_id"] == project_context.project_id
                and project_context.eligible(raw, include_noncurrent=True)):
            return {**raw, "_same_project": True, "_domain_history": True}
        if not args.global_search and (not reference_repo or raw.get("repo") == reference_repo):
            return raw
        # Search scope is not applicability. A global/foreign hit must retain
        # enough evidence to be checked without pretending it belongs here.
        return {**raw, "_cross_project": True, "_evidence_lead": True,
                "_from_project": raw.get("repo", ""),
                "_warning": "Historical evidence from another scope; verify the current project, branch and authority before applying"}

    def annotate_domain(item: dict, raw: dict) -> None:
        if project_context and raw.get("domain"):
            entity_id = raw["domain"]["entity_id"]
            project_context.check_history(entity_id)
            if entity_id in project_context.history_links:
                item["decision_history"] = project_context.history_links[entity_id][:4]
            if as_of:
                item["as_of"] = as_of.isoformat()
                if project_context.eligible(raw):
                    item["applicability_state"] = "historical_as_of"
                observations = raw["domain"].get("observations")
                if observations:
                    from kb_timeline import view
                    fields = view(observations, as_of, history_zone)
                    lines, refs = [], []
                    for field in fields:
                        for event in field["observations"]:
                            lines.append(f"{event['date']} {field['field']} ({field['status']}) {event['value']}")
                            for ref in event["evidence_refs"]:
                                if ref not in refs:
                                    refs.append(ref)
                    item["summary"] = _clip(" | ".join(lines), args.max_snippet_chars) if lines else "No confirmed observation by this date"
                    item["evidence_refs"] = refs
            if entity_id in project_context.replaced:
                item["superseded_in_scope_by"] = project_context.replaced[entity_id]
            if entity_id in project_context.uncertain_replacements:
                item["applicability_state"] = "needs_review"
                _append_warning(item, "A scoped replacement needs fresh evidence; do not assume this older requirement is current")

    learned_alias_owners: set[str] = set()

    @timed("retrieve.collect")
    def collect_batch(batch: dict[str, Any]) -> None:
        nonlocal rejected_count, rejected_nonactionable_count
        plan_index = int(batch["plan_index"])
        plan = batch["plan"]
        actual_query = str(batch["query"] or "")
        batch_expanded = bool(batch["expanded"])
        variant_index = int(batch.get("variant_index") or 0)
        raw_results = batch["raw_results"]
        for raw_rank, raw in enumerate(raw_results):
            count("retrieve.raw_candidate_visits")
            locator_query = artifact_query or _has_query_text(
                args.query, "映射", "路径", "目录", "位置", "在哪", "mapping", "locator", "location",
            )
            named_subtopic = _named_subtopic(raw, concrete_query_terms, reference_repo, reference_branch)
            if not args.include_weak and not named_subtopic and not _topical_candidate(raw, concrete_query_terms, allow_paths=locator_query):
                count("retrieve.rejected_task_topic")
                rejected_count += 1
                continue
            if not args.include_weak and not named_subtopic and not locator_query and not _guidance_has_task_topic(raw, concrete_query_terms):
                count("retrieve.rejected_guidance_topic")
                rejected_count += 1
                continue
            if not args.include_weak and not _subject_compatible(raw, args.query, subjects, subject_topics):
                count("retrieve.rejected_subject")
                rejected_count += 1
                continue
            if not args.include_noncurrent and _is_noncurrent(raw):
                continue
            if non_kb_maintenance_query and _is_personal_kb_runtime_entry(raw):
                rejected_count += 1
                continue
            if prior_decision_query and (
                not _is_decision_candidate(raw)
                or not decision_subject_terms
                or not _has_term_match(raw, decision_subject_terms, set(MATCH_FIELDS))
            ):
                rejected_count += 1
                continue
            effective_anchor_terms = _effective_anchor_terms(
                concrete_query_terms,
                plan=plan,
                synonyms=synonyms,
                should_expand=batch_expanded,
            )
            anchor_matches = _matched_anchor_terms(raw, effective_anchor_terms, MAP_ANCHOR_FIELDS)
            if effective_anchor_terms and not args.include_weak and not anchor_matches:
                rejected_count += 1
                continue
            if (
                fault_query
                and not args.include_weak
                and len(concrete_query_terms) >= 2
                and len(anchor_matches) < 2
            ):
                rejected_count += 1
                continue
            if (raw.get("kind") == "map" or raw.get("artifact_locator")) and not artifact_query:
                if not _map_has_concrete_anchor(
                    raw,
                    args.query,
                    anchor_terms=effective_anchor_terms,
                ):
                    rejected_count += 1
                    continue
            quality_terms = list(original_terms)
            for term in _query_planning_terms(actual_query):
                if term not in quality_terms:
                    quality_terms.append(term)
            for term in effective_anchor_terms:
                if term not in quality_terms:
                    quality_terms.append(term)
            usable, quality_score, _quality_reason = _match_quality(
                raw,
                terms=quality_terms,
                include_weak=args.include_weak,
            )
            if not usable:
                rejected_count += 1
                continue

            if project_context and not project_context.eligible(raw, include_noncurrent=args.include_noncurrent):
                rejected_count += 1
                continue
            formatted = format_entry(raw)

            merged_terms = _query_terms(f"{args.query} {actual_query}")
            for term in effective_anchor_terms:
                if term not in merged_terms:
                    merged_terms.append(term)
            item = _compact_entry(
                raw,
                formatted,
                terms=merged_terms,
                max_snippet_chars=max(80, args.max_snippet_chars),
                quality_score=quality_score,
                evidence=verify_once(raw),
            )
            annotate_domain(item, raw)
            if (
                item.get("cross_project")
                and item.get("kind") in {"issue", "pitfall", "experience", "implementation"}
                and not item.get("summary")
            ):
                rejected_nonactionable_count += 1
                continue
            entry_id = item.get("entry_id")
            if not entry_id:
                continue

            freshness_state = str(item.get("freshness_state") or "legacy_unverified")
            if freshness_state in HARD_FRESHNESS_STATES and not args.include_noncurrent:
                rejected_count += 1
                continue
            if (freshness_state == "fresh" and (raw.get("recall_checks") or {}).get("positive")
                    and any(isinstance(alias, str) and alias.casefold() in literal_anchors
                            for alias in raw.get("aliases", []))):
                learned_alias_owners.add(entry_id)
            status_penalty = 2.0 if _is_noncurrent(raw) else 0.0
            heat_value = 0.0
            try:
                heat_value = float(kb_search._used_count_key(raw))
            except (AttributeError, TypeError, ValueError, OSError):
                heat_value = 0.0
            score = (
                quality_score
                + plan.weight
                + float(item.get("confidence") or 0.0)
                + max(0.0, 0.25 - raw_rank * 0.01)
                - plan_index * 0.03
                - variant_index * 0.04
                - status_penalty
                + _freshness_rank(freshness_state)
                # Heat is only a tie breaker; it cannot compensate for stale
                # evidence or a weak lexical match.
                + min(0.25, math.log1p(max(0.0, heat_value)) * 0.08)
            )
            if not is_kb_runtime_query and _is_personal_kb_runtime_entry(raw):
                score -= 1.5
            if fault_query:
                kind = str(raw.get("kind") or "")
                if kind == "issue":
                    score += 4.5
                elif kind == "pitfall":
                    score += 4.0
                elif kind == "experience":
                    score += 2.5
                if not artifact_query:
                    if raw.get("artifact_locator"):
                        score -= 5.0
                    elif kind == "map":
                        score -= 2.5
            if prior_decision_query and _is_decision_candidate(raw):
                score += 3.5
                if _status_value(raw) == "decision_confirmed":
                    score += 1.0
                _append_warning(
                    item,
                    "historical decision hint; verify against the current request and current evidence before applying",
                )

            existing = candidates.get(entry_id)
            if existing is None:
                item["query_groups"] = [plan.name]
                item["_score"] = round(score, 4)
                candidates[entry_id] = item
                continue

            groups = existing.setdefault("query_groups", [])
            if plan.name not in groups:
                groups.append(plan.name)
            if score > float(existing.get("_score") or 0.0):
                item["query_groups"] = groups
                item["_score"] = round(score, 4)
                candidates[entry_id] = item

    for batch in search_batches:
        collect_batch(batch)

    raw_strong_ids: set[str] = set()
    for batch in search_batches:
        plan = batch["plan"]
        # The expansion gate is defined by the raw user wording.  A planner
        # concept query may find a strong record, but that must not suppress
        # expansion when the actual task query still has no precise hit.
        if plan.name != "task":
            continue
        raw_terms = _effective_anchor_terms(
            concrete_query_terms,
            plan=plan,
            synonyms=synonyms,
            should_expand=False,
        )
        for raw in batch["raw_results"]:
            if _matched_anchor_terms(raw, raw_terms, STRONG_FIELDS):
                entry_id = str(raw.get("id") or "").strip()
                if entry_id in candidates and "task" in candidates[entry_id].get("query_groups", []):
                    raw_strong_ids.add(entry_id)

    raw_usable = sorted(
        (item for entry_id, item in candidates.items() if entry_id in raw_strong_ids),
        key=lambda item: float(item.get("_score") or 0.0), reverse=True,
    )
    raw_usable, _ = _filter_freshness_items(
        raw_usable, include_noncurrent=args.include_noncurrent, legacy_fallback=legacy_fallback,
    )
    raw_usable, _ = _dedupe_similar_items(raw_usable)
    raw_usable, _ = _apply_result_diversity(
        raw_usable, artifact_query=artifact_query, fault_query=fault_query,
        kb_runtime_query=is_kb_runtime_query, map_limit=map_results_per_query,
    )
    raw_strong_ids = {
        item["entry_id"] for item in raw_usable
        if float(item.get("retrieval_score", item.get("confidence")) or 0.0) >= args.min_confidence
    }

    expansion_variants: list[dict[str, Any]] = []
    if expand_requested and len(raw_strong_ids) < 2:
        # At most four variants per planner query, each retaining at least one
        # original concrete anchor.  The original query itself was searched
        # above and is not repeated here.
        for batch in list(search_batches):
            plan = batch["plan"]
            variants = _safe_query_variants(
                plan.query,
                anchor_terms=concrete_query_terms,
                synonyms=synonyms,
                max_variants=max_query_variants,
            )
            for variant_index, variant in enumerate(variants[1:], start=1):
                group_args = argparse.Namespace(
                    global_search=args.global_search,
                    search_in=args.search_in,
                    repo=args.repo,
                    branch=args.branch,
                    match_mode=plan.match_mode or args.match_mode,
                )
                stderr_capture = io.StringIO()
                stderr_context = contextlib.nullcontext() if args.debug else contextlib.redirect_stderr(stderr_capture)
                with stderr_context, span("retrieve.search_expanded"):
                    raw_results, ctx = kb_search._search_once(
                        query=variant,
                        args=group_args,
                        allowed_kinds=set(allowed_kinds) if allowed_kinds else None,
                        required_tags=required_tags,
                        recent_cutoff=recent_cutoff,
                        should_expand=False,
                        synonyms=synonyms,
                        limit=search_limit,
                    )
                if reference_repo is None and not args.global_search and ctx is not None:
                    reference_repo = ctx.repo_name
                search_batches.append(
                    {
                        "plan_index": batch["plan_index"],
                        "plan": plan,
                        "query": variant,
                        "expanded": True,
                        "variant_index": variant_index,
                        "raw_results": raw_results,
                    }
                )
                collect_batch(search_batches[-1])
                expansion_variants.append(
                    {"plan": plan.name, "query": variant}
                )

    if project_context:
        with span("retrieve.domain_related"):
            related = project_context.related(set(candidates), limit=min(32, max(0, search_limit)),
                                              include_noncurrent=args.include_noncurrent)
            if args.search_in in ("kb", "all"):
                related.extend(_linked_records(original_query, project_context, args.include_noncurrent))
                if routing.get("state") == "use":
                    chosen = routing["candidates"][0]
                    owner = project_context.entities.get(chosen.get("entity_id") or chosen.get("project_id"))
                    name = chosen.get("matched_name", "")
                    if (owner and name and args.query.strip(" 。？！?!").casefold() == name.casefold()
                            and project_context.eligible(owner) and project_context.current(owner)):
                        related.append((owner, {"type": "project_name", "name": name}))
        for raw, path in related:
            if not args.include_weak and not _subject_compatible(raw, args.query, subjects, subject_topics):
                count("retrieve.rejected_subject")
                continue
            if allowed_kinds and raw.get("kind") not in allowed_kinds:
                continue
            if not kb_search._matches_tags(raw, required_tags):
                continue
            if recent_cutoff:
                timestamp = kb_search._ts_key(raw)
                if not timestamp or datetime.fromtimestamp(timestamp) < recent_cutoff:
                    continue
            if prior_decision_query and not _is_decision_candidate(raw):
                continue
            if non_kb_maintenance_query and _is_personal_kb_runtime_entry(raw):
                continue
            if path["type"] == "conversation" and verify_once(raw)["state"] != "fresh":
                continue
            entry_id = raw.get("id")
            if entry_id in candidates:
                candidates[entry_id]["related_via"] = path
                continue
            item = _compact_entry(
                raw, format_entry(raw), terms=original_terms,
                max_snippet_chars=max(80, args.max_snippet_chars),
                evidence=verify_once(raw),
            )
            item["related_via"] = path
            annotate_domain(item, raw)
            item["query_groups"] = ["linked-chat" if path["type"] == "conversation" else "project-graph"]
            item["why_matched"] = "verified evidence link: " + path["type"]
            item["_score"] = 2.5 + _freshness_rank(item["freshness_state"])
            # Graph matches have a different basis from lexical confidence.
            item["retrieval_score"] = item["confidence"] = 0.8
            candidates[entry_id] = item

    _prefer_learned_alias_to_repo_name(candidates, learned_alias_owners)
    if project_context and not args.global_search:
        project_context.prefer_local_versions(candidates)
    items = sorted(candidates.values(), key=lambda item: (
        item.get("related_via", {}).get("type") in {"dated_name", "entity_alias", "project_name"}, float(item.get("_score") or 0.0)
    ), reverse=True)
    for item in items:
        item.pop("_score", None)
    items, freshness_filtered_count = _filter_freshness_items(
        items,
        include_noncurrent=args.include_noncurrent,
        legacy_fallback=legacy_fallback,
    )
    items, deduped_similar_count = _dedupe_similar_items(items)
    items, map_filtered_count = _apply_result_diversity(
        items,
        artifact_query=artifact_query,
        fault_query=fault_query,
        kb_runtime_query=is_kb_runtime_query,
        map_limit=map_results_per_query,
    )
    filtered_low_confidence = 0
    if args.min_confidence > 0.0:
        retained: list[dict[str, Any]] = []
        for item in items:
            if float(item.get("retrieval_score", item.get("confidence")) or 0.0) >= args.min_confidence:
                retained.append(item)
            else:
                filtered_low_confidence += 1
        items = retained

    items = items[:requested_limit]
    items, truncation = _apply_total_char_budget(items, max_total_chars=args.max_total_chars)

    payload = {
        "retrieval_id": retrieval_id,
        "query": original_query,
        "session_id": runtime_session_id(getattr(args, "session_id", "")),
        "topic_id": str(getattr(args, "topic_id", "") or "").strip(),
        "query_chars": len(args.query),
        "hit_count": len(items),
        "limit": requested_limit,
        "mode": "read_only_rag_context",
        "repo": "global" if args.global_search else (reference_repo or ""),
        "query_groups": query_group_payload,
        "rejected_weak_count": rejected_count,
        "items": items,
        "runtime": runtime_scope(),
    }
    if routing:
        payload["project_routing"] = routing
    if project_context:
        payload["domain_context"] = project_context.payload()
        if getattr(args, "project_view", False):
            payload["project_view"] = kb_project_view.project_view(items, payload["domain_context"])
    if scope_anchors:
        payload["scope_anchors"] = scope_anchors
    if expand_requested:
        payload["query_expansion"] = {
            "enabled": True,
            "performed": bool(expansion_variants),
            "max_plans": max_query_plans,
            "max_variants": max_query_variants,
            "variants": expansion_variants[: max_query_variants * max(1, len(plans))],
        }
    if deduped_similar_count:
        payload["deduped_similar_count"] = deduped_similar_count
    if freshness_filtered_count:
        payload["filtered_freshness_count"] = freshness_filtered_count
    if map_filtered_count:
        payload["filtered_map_count"] = map_filtered_count
    if filtered_low_confidence:
        payload["filtered_low_confidence_count"] = filtered_low_confidence
    if rejected_nonactionable_count:
        payload["rejected_nonactionable_count"] = rejected_nonactionable_count
    if truncation:
        payload["truncation"] = truncation
    return payload


def _brief_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Project an existing result. Never search again or change closeout candidates."""
    if payload.get("error") or payload.get("receipt", {}).get("error"):
        return payload
    brief = {key: payload[key] for key in (
        "retrieval_id", "query", "session_id", "topic_id", "mode", "repo", "hit_count",
        "scope_anchors", "truncation", "project_view", "project_routing", "intent",
    ) if key in payload}
    context = payload.get("domain_context", {})
    if context and (payload.get("project_view") or context.get("project_id") or context.get("state") == "ambiguous"
                    or context.get("uncertain_replacement_count") or context.get("invalid_records")):
        brief["domain_context"] = {key: value for key, value in context.items()
                                   if key not in {"cache_hit", "indexed_records", "max_hops"}}
    items = []
    for hit in payload["items"]:
        item = {key: hit[key] for key in (
            "entry_id", "kind", "title", "summary", "repo", "branch", "freshness_state",
            "freshness_scope", "trust_tier", "applicability_state", "warning", "domain", "superseded_in_scope_by",
            "evidence_refs", "as_of", "related_via", "decision_history",
        ) if hit.get(key)}
        sources = hit.get("source_paths") or hit.get("key_files") or []
        if sources:
            item["source_paths"] = sources[:1]
        items.append(item)
    brief["items"] = items
    return brief


def _print_markdown(payload: dict[str, Any]) -> None:
    retrieval_id = payload.get("retrieval_id", "")
    query = payload.get("query", "")
    items = payload.get("items", [])
    query_groups = payload.get("query_groups") or []
    group_names = [group.get("name") for group in query_groups if isinstance(group, dict) and group.get("name")]
    rejected_weak = payload.get("rejected_weak_count", 0)
    if payload.get("project_routing"):
        sys.stdout.write("KB_PROJECT_ROUTING " + json.dumps(payload["project_routing"], ensure_ascii=False) + "\n")
    if payload.get("domain_context", {}).get("state") in {"resolved", "ambiguous"}:
        sys.stdout.write("KB_PROJECT_CONTEXT " + json.dumps(payload["domain_context"], ensure_ascii=False) + "\n")
    suffix = ""
    if group_names:
        suffix += f" groups={','.join(group_names[:6])}"
    if rejected_weak:
        suffix += f" rejected_weak={rejected_weak}"
    escaped_query = (
        str(query)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    sys.stdout.write(
        f'KB_RAG_CONTEXT query="{escaped_query}" retrieval_id="{retrieval_id}" hits={len(items)}{suffix}\n'
    )
    if not items:
        sys.stdout.write("No usable KB context found.\n")
        return

    for item in items:
        coord = "/".join(part for part in [item.get("repo"), item.get("branch")] if part)
        coord_text = f" ({coord})" if coord else ""
        sys.stdout.write(
            f"- [{item.get('entry_id')}] {item.get('kind')} {item.get('title')}"
            f"{coord_text} retrieval_score={item.get('retrieval_score', item.get('confidence'))}"
            f" freshness={item.get('freshness_state', 'unknown')}"
            f" trust={item.get('trust_tier', 'unknown')}\n"
        )
        sys.stdout.write(f"  why: {item.get('why_matched')}\n")
        if item.get("summary"):
            sys.stdout.write(f"  note: {item.get('summary')}\n")
        sources = item.get("source_paths") or item.get("key_files") or []
        if sources:
            sys.stdout.write(f"  sources: {', '.join(sources)}\n")
        if item.get("warning"):
            sys.stdout.write(f"  warning: {item.get('warning')}\n")


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = RetrievalArgumentParser(
        description="Build compact read-only personal-kb context for RAG-style AI invocation."
    )
    parser.add_argument("query", help="Search terms for the current user request")
    parser.add_argument(
        "--retrieval-id",
        default="",
        help="Test-only opaque correlation ID; production IDs are generated automatically",
    )
    parser.add_argument(
        "--scope-anchor",
        action="append",
        default=[],
        help="Object named in the task or selected question; keep it in a focused lookup",
    )
    parser.add_argument(
        "--receipt-output",
        default="",
        help="Explicitly write the compact retrieval result to this JSON file",
    )
    intent = parser.add_mutually_exclusive_group()
    intent.add_argument("--focus", help="Independent historical question within the larger task")
    intent.add_argument("--action", help="Concrete upcoming operation and client/environment")
    parser.add_argument("--session-id", default="", help="Caller session identifier")
    parser.add_argument("--topic-id", default="", help="Independent topic identifier")
    parser.add_argument("--repo", default="", help="Override repo bucket")
    parser.add_argument("--branch", default="", help="Override branch bucket")
    parser.add_argument("--current-file", "--active-file", action="append", default=[],
                        help="File currently in use, relative to the caller or absolute (up to four)")
    parser.add_argument("--project-id", default="", help="Resolve this evidenced domain project ID")
    parser.add_argument("--as-of", default="", help="Evaluate dated domain records and replacement edges at this period or offset timestamp")
    parser.add_argument("--environment", default="", help="Domain applicability environment")
    parser.add_argument("--no-domain", action="store_true", help="Use the legacy lexical path without domain lookup")
    parser.add_argument("--project-view", action="store_true", help="Include a bounded current project view")
    parser.add_argument(
        "--kind",
        dest="kind_filter",
        default="map,requirement,implementation,experience,issue,pitfall",
        help=f"Comma-separated kinds. Valid: {','.join(sorted(VALID_KINDS))}",
    )
    parser.add_argument("--type", dest="legacy_type_filter", default="", help="Deprecated alias mapped to --kind")
    parser.add_argument("--tags", default="", help="Comma-separated tags filter, match ANY")
    parser.add_argument("--limit", type=int, default=5, help="Max compact context items")
    parser.add_argument("--in", dest="search_in", choices=["kb", "summary", "all"], default="all")
    parser.add_argument("--global", dest="global_search", action="store_true", help="Scan all KB buckets")
    parser.add_argument("--match-mode", choices=["any", "all"], default="any")
    parser.add_argument("--no-plan", action="store_true", help="Disable task query planning and search only the raw query")
    parser.add_argument("--no-session-briefs", action="store_true", help="Skip recent session brief retrieval before long-term KB")
    parser.add_argument("--session-brief-days", type=int, default=2, help="Recent session brief window in days")
    parser.add_argument("--session-brief-limit", type=int, default=2, help="Max recent session brief hits")
    parser.add_argument("--include-weak", action="store_true", help="Include weak story-only matches in RAG output")
    parser.add_argument("--debug", action="store_true", help="Show internal search warnings from planned query groups")
    parser.add_argument("--expand", action="store_true", help="Enable query expansion")
    parser.add_argument("--recent", default="", help="Only return recent entries, for example 7d or 2w")
    parser.add_argument("--include-noncurrent", action="store_true", help="Include draft/superseded/historical records")
    parser.add_argument("--min-confidence", type=float, default=0.0, help="Drop items below this confidence threshold (0-1)")
    parser.add_argument("--max-snippet-chars", type=int, default=360, help="Max summary chars per item")
    parser.add_argument("--max-total-chars", type=int, default=0, help="Cap total chars of returned compact items; 0 disables")
    parser.add_argument(
        "--best-effort",
        action="store_true",
        help="Treat runtime/storage unavailability as an empty, non-retryable result",
    )
    parser.add_argument("--brief", action="store_true", help="Return decision fields and closeout identity as compact JSON")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of compact text")
    try:
        args = parser.parse_args(argv)
    except ValueError as exc:
        payload = _error_payload(
            retrieval_id=_retrieval_id(""),
            query=argv[0] if argv and not argv[0].startswith("-") else "",
            message=str(exc), error_code="invalid_request",
        )
        options = argv[:argv.index("--")] if "--" in argv else argv
        if "--json" in options or "--brief" in options:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        else:
            parser.print_usage(sys.stderr)
        sys.stderr.write(f"error: {exc}\n")
        return 2

    return_code = 0
    try:
        task_query = args.query
        selected = args.action if args.action is not None else args.focus
        if selected is not None:
            if not selected.strip():
                raise ValueError("focused lookup needs a concrete question or action")
            args.query = selected.strip()
            # The focus can say "that device" while the task names it. Carry only
            # caller-selected objects actually present in the task or the focus.
            for anchor in args.scope_anchor:
                value = _scope_anchor_value(anchor)
                if value.casefold() not in args.query.casefold():
                    _validate_scope_anchors(task_query, [anchor])
                    args.query += " " + value
        payload = build_context(args)
        if selected is not None:
            payload["task_query"] = task_query
            payload["intent"] = "action" if args.action is not None else "focus"
    except (ValueError, JsonlSafetyError, OSError, RuntimeError, KeyError, TypeError) as exc:
        if isinstance(exc, JsonlSafetyError):
            error_code, return_code = "data_corruption", 5
        elif isinstance(exc, PersonalKbConfigError):
            error_code, return_code = "configuration_error", 5
        elif isinstance(exc, ValueError):
            error_code, return_code = "invalid_request", 2
        elif isinstance(exc, OSError):
            error_code = "runtime_unavailable"
            return_code = 0 if args.best_effort else 3
        else:
            error_code, return_code = "internal_error", 5
        payload = _error_payload(
            retrieval_id=_retrieval_id(""), query=args.query,
            message=str(exc), error_code=error_code,
            status="unavailable" if error_code == "runtime_unavailable" else "error",
        )
        if not (args.json or args.brief) or error_code != "runtime_unavailable":
            sys.stderr.write(f"error: {error_code}: {exc}\n")

    payload.update(session_id=runtime_session_id(args.session_id), topic_id=args.topic_id.strip(), query_chars=len(args.query))
    try:
        _write_receipt(args.receipt_output, payload)
    except (OSError, ValueError) as exc:
        # Retrieval output remains reusable even if the optional sink failed.
        # Preserve the original retrieval error when both operations failed.
        payload["receipt"] = {
            "status": "error", "retryable": False,
            "error": {"code": "receipt_write_failed", "message": str(exc)},
        }
        if "error" not in payload:
            payload["status"] = "partial_failure"
            payload["retryable"] = False
        sys.stderr.write(f"error: cannot write --receipt-output: {exc}\n")
        return_code = 5

    if args.brief:
        sys.stdout.write(json.dumps(_brief_result(payload), ensure_ascii=False, separators=(",", ":")) + "\n")
    elif args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    elif "error" not in payload:
        _print_markdown(payload)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
