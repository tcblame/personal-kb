#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import collections
import io
import json
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

from kb_lib import kb_base_dir, now_iso, runtime_file
from kb_runtime import attach_runtime_scope
from kb_sensitive_scan import redact_value
import kb_command_contract as command_contract


KB_SCRIPT_PATTERNS = command_contract.KB_SCRIPT_PATTERNS
KB_WRAPPER_COMMANDS = command_contract.KB_WRAPPER_COMMANDS

CURRENT_WORKFLOW_CUTOFF = "2026-07-03"

FORBID_KB_RE = re.compile(
    r"(禁止|不要)\s*(执行|运行|调用)?\s*(任何\s*)?(personal-kb|KB|kb)\s*(脚本|skill)?|"
    r"do not run personal-kb|do not run kb_|do not call personal-kb|"
    r"禁止运行\s*kb_|不要运行\s*kb_|不要执行\s*kb_",
    re.I,
)
SUBAGENT_FORBID_KB_RE = re.compile(
    r"(子\s*agent|子agent|subagent|worker|explorer).*?(禁止|不要|do not).*?(personal-kb|KB|kb)|"
    r"(禁止|不要|do not).*?(子\s*agent|子agent|subagent|worker|explorer).*?(personal-kb|KB|kb)",
    re.I,
)
FORBID_WRITE_KB_RE = re.compile(
    r"(禁止|不要)\s*(执行|运行|调用)?\s*(kb_add|kb_update|kb_closeout)|"
    r"(禁止|不要).*?(写入|加热|closeout|update|add).*?(KB|kb|personal-kb)|"
    r"do not run (kb_add|kb_update|kb_closeout)",
    re.I,
)
EXPLICIT_KB_RE = re.compile(r"personal-kb|\bKB\b|kb_|\bRAG\b|记忆系统|知识库|历史记录", re.I)
MEMORY_NEEDED_RE = re.compile(
    r"跨会话|历史记录|已有记录|记录过|确认过|以前|曾经|之前|历史|上次|"
    r"沿用.*(上次|之前|历史|已确认)|继续.*(上次|之前|历史|已确认|旧决定)",
    re.I,
)
RUNTIME_AUDIT_RE = re.compile(
    r"(?:personal-kb|\bKB\b|知识库).{0,24}(?:运行|runtime|会话|session|效果|质量|审计|telemetry)|"
    r"(?:运行|runtime|会话|session|效果|质量|审计|telemetry).{0,24}(?:personal-kb|\bKB\b|知识库)",
    re.I,
)
GENERIC_SKILL_INSTALL_RE = re.compile(r"(面试|简历).{0,20}skill|skill.{0,20}(面试|简历)|interview-coach|resume.*skill", re.I)
CONTINUITY_RE = re.compile(r"继续|上次|之前|历史|已有|已确认|当前材料|现有材料", re.I)
SUBAGENT_KB_TASK_RE = re.compile(
    r"KB\s*scout|kb\s*scout|"
    r"kb_rag_context\.py|kb_search\.py|"
    r"(运行|执行|调用).*?kb_(rag_context|search)|"
    r"(检索|搜索).*?(KB|kb|知识库|历史记录)|"
    r"(KB|kb|知识库|历史记录).*?(检索|搜索)|"
    r"(审计|维护|修复).*?(KB\s*(数据|记录)|kb\.jsonl|closeout\.jsonl|session_briefs\.jsonl|个人知识库数据|知识库数据)",
    re.I,
)
SUBAGENT_NON_KB_TASK_RE = re.compile(
    r"not\s+(?:a\s+)?KB\s*(?:scout|task)|ordinary\s+(?:subagent|worker|explorer)|"
    r"普通(?:的)?(?:子\s*agent|子agent|worker|explorer)|未授权.*?(?:KB|personal-kb)|"
    r"(?:KB|personal-kb).*?未授权",
    re.I,
)
EXIT_RE = re.compile(r"Process exited with code\s+(-?\d+)")
RAG_TEXT_HITS_RE = re.compile(r"\bhits=(\d+)(?=\s|$)")
RAG_TEXT_RETRIEVAL_ID_RE = re.compile(r'retrieval_id="([A-Za-z0-9._:-]+)"')
RETRIEVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PYTHON_EXECUTABLE_RE = command_contract.PYTHON_EXECUTABLE_RE
CUSTOM_EXEC_CMD_RE = re.compile(
    r"(?:[\"']cmd[\"']|\bcmd)\s*:\s*(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)",
    re.S,
)
ROLLOUT_UUID_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?![0-9a-f])", re.I)
ADOPTION_EFFECTS = ("locate", "decide", "fix", "write")
ADOPTION_EFFECT_PRECEDENCE = ("write", "fix", "decide", "locate")
CONTEXT_PREFIXES = (
    "# AGENTS.md instructions",
    "<INSTRUCTIONS>",
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "<subagent_notification>",
    "<turn_aborted>",
)
PARSER_VERSION = "codex-shell-v8"


def log_path(base_dir: Path | None = None) -> Path:
    return runtime_file("codex_kb_effectiveness_log.jsonl", base_dir=base_dir)


def summary_path(base_dir: Path | None = None) -> Path:
    return runtime_file("codex_kb_effectiveness_summary.json", base_dir=base_dir)


def legacy_log_path(base_dir: Path | None = None) -> Path:
    return runtime_file("codex_kb_effectiveness_legacy_log.jsonl", base_dir=base_dir)


def state_path(base_dir: Path | None = None) -> Path:
    return runtime_file("codex_kb_effectiveness_state.json", base_dir=base_dir)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    rows.append(parsed)
    except OSError:
        return []
    return rows


def _json_loads(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dictish_loads(value: str) -> dict[str, Any]:
    text = (value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _source_text(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    return str(value)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"sessions": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"sessions": {}}
    if not isinstance(payload, dict):
        return {"sessions": {}}
    sessions = payload.get("sessions")
    return {"sessions": sessions if isinstance(sessions, dict) else {}}


def _save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _file_signature(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))}


def _default_session_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def _session_key(path: Path) -> str:
    return str(path.resolve())


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _short_text(value: str, limit: int = 220) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        clean = str(value).strip()
        if clean and clean not in out:
            out.append(clean)
    return out


def _rollout_id_from_path(path: Path) -> str:
    matches = ROLLOUT_UUID_RE.findall(path.stem)
    return matches[-1].lower() if matches else ""


def _select_session_meta(path: Path, metas: list[dict[str, Any]]) -> tuple[dict[str, Any], str, str]:
    """Select this rollout's metadata instead of inherited metadata copied into a fork."""
    rollout_id = _rollout_id_from_path(path)
    if rollout_id:
        for meta in metas:
            if str(meta.get("id") or "").strip().lower() == rollout_id:
                return meta, rollout_id, "rollout_filename_id"
    if metas:
        return metas[0], rollout_id, "first_session_meta_fallback"
    return {}, rollout_id, "missing_session_meta"


def _normalize_adoption_effects(effects: dict[str, list[str]]) -> dict[str, list[str]]:
    """Mirror kb_closeout precedence when an entry is assigned more than one effect."""
    assigned: set[str] = set()
    normalized: dict[str, list[str]] = {effect: [] for effect in ADOPTION_EFFECTS}
    for effect in ADOPTION_EFFECT_PRECEDENCE:
        for entry_id in _dedupe(effects.get(effect, [])):
            if entry_id in assigned:
                continue
            assigned.add(entry_id)
            normalized[effect].append(entry_id)
    return normalized


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return _dedupe([str(item) for item in value])
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off", ""}:
            return False
    return default


def _normalized_query(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _turn_id(payload: dict[str, Any], fallback: str = "") -> str:
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(metadata, dict) and metadata.get("turn_id"):
        return str(metadata["turn_id"])
    return str(payload.get("turn_id") or fallback)


def _session_day(row: dict[str, Any]) -> str:
    return str(row.get("session_ts") or "")[:10]


def _is_legacy_row(row: dict[str, Any], cutoff: str) -> bool:
    day = _session_day(row)
    return bool(cutoff and day and day < cutoff)


def _forbid_kb_scope(user_text: str, *, is_subagent: bool) -> str:
    text = user_text or ""
    parent_scope_text, contract_subagent_only = command_contract.strip_subagent_only_kb_guards(text)
    write_only = bool(FORBID_WRITE_KB_RE.search(parent_scope_text))
    full_forbid = bool(FORBID_KB_RE.search(parent_scope_text))
    subagent_only = contract_subagent_only or bool(SUBAGENT_FORBID_KB_RE.search(text))
    if full_forbid:
        if subagent_only and not is_subagent:
            return "subagent_only"
        return "global"
    if subagent_only:
        return "subagent_only"
    if write_only:
        return "write_only"
    return "none"


def _dedupe_priority(row: dict[str, Any]) -> tuple[int, int, int, int, str, str]:
    verdict = str(row.get("effect_verdict") or "")
    priority_by_verdict = {
        "used_hit_adopted": 90,
        "used_hit_adoption_unconfirmed": 85,
        "used_hit_recorded_not_adopted": 80,
        "used_hit_not_adopted": 70,
        "used_without_closeout": 60,
        "kb_used_no_effect_signal": 55,
        "subagent_kb_used": 55,
        "subagent_kb_used_authorization_unknown": 52,
        "used_no_hit": 50,
        "forbidden_but_used": 40,
        "subagent_forbidden_but_used": 40,
        "subagent_unexpected_kb_used": 35,
        "needed_but_not_used": 30,
        "subagent_kb_task_not_used": 30,
        "no_kb_needed": 20,
        "subagent_no_kb_expected": 20,
    }
    return (
        priority_by_verdict.get(verdict, 0),
        1 if row.get("closeout_called") else 0,
        1 if row.get("kb_used") else 0,
        _coerce_int(row.get("user_message_count"), 0),
        str(row.get("session_ts") or ""),
        str(row.get("session_path") or ""),
    )


def _dedupe_rows_by_session_id(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    scopes_by_session_id: dict[str, set[str]] = collections.defaultdict(set)
    for row in rows:
        session_id = str(row.get("session_id") or row.get("session_path") or "")
        agent_scope = "subagent" if row.get("is_subagent") else "main"
        grouped[(session_id, agent_scope)].append(row)
        scopes_by_session_id[session_id].add(agent_scope)

    deduped: list[dict[str, Any]] = []
    duplicate_ids: list[str] = []
    for (session_id, agent_scope), group in grouped.items():
        if len(group) > 1:
            duplicate_ids.append(session_id)
        chosen = max(group, key=_dedupe_priority)
        if len(group) > 1:
            chosen = dict(chosen)
            chosen["dedupe_group_size"] = len(group)
            chosen["dedupe_agent_scope"] = agent_scope
            chosen["dedupe_session_files"] = [str(item.get("session_file") or "") for item in group][:20]
        deduped.append(chosen)

    deduped.sort(key=lambda row: (str(row.get("session_ts", "")), str(row.get("session_path", ""))))
    cross_scope_ids = [session_id for session_id, scopes in scopes_by_session_id.items() if len(scopes) > 1]
    stats = {
        "logical_session_total": len(deduped),
        "duplicate_session_id_count": len(duplicate_ids),
        "duplicate_row_extra": sum(len(group) - 1 for group in grouped.values()),
        "duplicate_session_ids_sample": duplicate_ids[:20],
        "cross_agent_scope_session_id_count": len(cross_scope_ids),
        "cross_agent_scope_session_ids_sample": cross_scope_ids[:20],
    }
    return deduped, stats


def _subagent_info(meta: dict[str, Any]) -> dict[str, Any]:
    source_raw = meta.get("source")
    source_text = _source_text(source_raw)
    source_dict = source_raw if isinstance(source_raw, dict) else _dictish_loads(source_text)
    thread_source = str(meta.get("thread_source") or "")

    spawn: dict[str, Any] = {}
    subagent_payload = source_dict.get("subagent") if isinstance(source_dict, dict) else None
    if isinstance(subagent_payload, dict):
        maybe_spawn = subagent_payload.get("thread_spawn")
        if isinstance(maybe_spawn, dict):
            spawn = maybe_spawn

    parent_thread_id = str(meta.get("parent_thread_id") or spawn.get("parent_thread_id") or "")
    agent_role = str(meta.get("agent_role") or spawn.get("agent_role") or "")
    agent_nickname = str(meta.get("agent_nickname") or spawn.get("agent_nickname") or "")
    depth = spawn.get("depth")
    try:
        depth = int(depth) if depth is not None else None
    except (TypeError, ValueError):
        depth = None

    is_subagent = (
        thread_source == "subagent"
        or bool(parent_thread_id)
        or (isinstance(source_dict, dict) and "subagent" in source_dict)
        or "subagent" in source_text.lower()
    )

    return {
        "is_subagent": is_subagent,
        "subagent_parent_thread_id": parent_thread_id,
        "subagent_role": agent_role,
        "subagent_nickname": agent_nickname,
        "subagent_depth": depth,
    }


def _parse_exit_code(output: str) -> int | None:
    match = EXIT_RE.search(output or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _output_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _output_text(item)))
    if isinstance(value, dict):
        for key in ("text", "output", "content"):
            if key in value:
                return _output_text(value.get(key))
    return ""


def _user_response_text(content: Any) -> str:
    parts: list[str] = []
    for item in _as_list(content):
        text = _output_text(item)
        if text.lstrip().startswith(CONTEXT_PREFIXES):
            continue
        if text.strip():
            parts.append(text)
    return "\n".join(parts)


def _has_subagent_no_kb_guard(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return (
        "do not run personal-kb scripts" in normalized
        and (
            "parent owns kb retrieval, closeout, heating, and writes" in normalized
            or (
                "parent owns retrieval routing and final hint selection" in normalized
                and "adoption, heating, writes, and closeout" in normalized
            )
        )
    )


def _execution_success(output: str) -> bool | None:
    exit_code = _parse_exit_code(output)
    if exit_code is not None:
        return exit_code == 0
    if re.search(r"\bScript completed\b", output or "", re.I):
        return True
    if re.search(r"\b(?:Script failed|command failed|Process exited with code\s+[1-9]\d*)\b", output or "", re.I):
        return False
    return None


def _json_dicts_from_output(output: str) -> list[dict[str, Any]]:
    """Extract JSON objects from shell wrappers without assuming JSON-only stdout."""
    text = output or ""
    decoder = json.JSONDecoder()
    rows: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(text):
        starts = [index for token in ("{", "[") if (index := text.find(token, cursor)) >= 0]
        if not starts:
            break
        start = min(starts)
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if isinstance(value, dict):
            rows.append(value)
        elif isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
        cursor = max(end, start + 1)
    return rows


_CLOSEOUT_OUTPUT_DETAIL_KEYS = {
    "linked_retrieval_ids",
    "queries",
    "rag_calls",
    "hit_count",
    "used_entry_ids",
    "adoption_effects",
    "adopted_entry_ids",
    "adoption_applied",
    "heated_entry_ids",
    "heat_failed_entry_ids",
    "session_brief_used_entry_ids",
    "session_brief_ids",
    "session_brief_hit",
    "session_brief_help",
    "written_entry_ids",
    "updated_entry_ids",
}


def _closeout_payload_from_output(output: str) -> dict[str, Any]:
    """Merge full closeout stdout with any later partial-failure result object."""
    payload: dict[str, Any] = {}
    for candidate in _json_dicts_from_output(output):
        if str(candidate.get("event") or "") != "kb_closeout":
            continue
        if not any(key in candidate for key in _CLOSEOUT_OUTPUT_DETAIL_KEYS):
            # `--verbose` summaries do not carry per-entry adoption truth.
            continue
        payload.update(candidate)
    return payload


def _decode_js_string(literal: str) -> str:
    if not literal:
        return ""
    if literal.startswith('"'):
        try:
            value = json.loads(literal)
            return str(value) if isinstance(value, str) else ""
        except json.JSONDecodeError:
            return ""
    if literal.startswith("'"):
        try:
            value = ast.literal_eval(literal)
            return str(value) if isinstance(value, str) else ""
        except (SyntaxError, ValueError):
            return ""
    if literal.startswith("`") and literal.endswith("`"):
        value = literal[1:-1]
        if "${" in value:
            return ""
        return value.replace(r"\`", "`").replace(r"\n", "\n").replace(r"\r", "\r").replace(r"\t", "\t")
    return ""


def _custom_exec_commands(source: str) -> list[str]:
    if "tools.exec_command" not in (source or ""):
        return []
    commands: list[str] = []
    for match in CUSTOM_EXEC_CMD_RE.finditer(source):
        command = _decode_js_string(match.group(1))
        if command:
            commands.append(command)
    return commands


def _parse_hit_count_from_output(output: str) -> int | None:
    header_lines = [
        line.strip()
        for line in (output or "").splitlines()
        if line.strip().startswith("KB_RAG_CONTEXT ")
    ]
    matches = [match for line in header_lines for match in RAG_TEXT_HITS_RE.findall(line)]
    if matches:
        # The canonical hit count is emitted after the quoted query. Taking
        # the final header value prevents query text from spoofing an earlier
        # `hits=` token while remaining compatible with legacy text output.
        return _coerce_int(matches[-1], 0)
    for parsed in _json_dicts_from_output(output):
        if parsed.get("mode") != "read_only_rag_context":
            continue
        if "hit_count" in parsed:
            return _coerce_int(parsed.get("hit_count"), 0)
        items = parsed.get("items")
        if isinstance(items, list):
            return len(items)
    try:
        parsed = json.loads(output or "")
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        if "hit_count" in parsed:
            return _coerce_int(parsed.get("hit_count"), 0)
        items = parsed.get("items")
        if isinstance(items, list):
            return len(items)
    if isinstance(parsed, list):
        return len(parsed)
    return None


def _parse_retrieval_id_from_output(output: str) -> str:
    header_lines = [
        line.strip()
        for line in (output or "").splitlines()
        if line.strip().startswith("KB_RAG_CONTEXT ")
    ]
    matches = [
        match for line in header_lines for match in RAG_TEXT_RETRIEVAL_ID_RE.findall(line)
    ]
    if matches and RETRIEVAL_ID_RE.fullmatch(matches[-1]):
        # The runtime-generated ID follows the quoted query in canonical text
        # output. Use the last valid token so query text cannot forge the ID.
        return matches[-1]
    for parsed in _json_dicts_from_output(output):
        if parsed.get("mode") != "read_only_rag_context":
            continue
        retrieval_id = str(parsed.get("retrieval_id") or "").strip()
        if RETRIEVAL_ID_RE.fullmatch(retrieval_id):
            return retrieval_id
    return ""


def _parse_cli_tokens(command: str) -> list[str]:
    return command_contract.parse_cli_tokens(command)


def _token_basename(token: str) -> str:
    return command_contract.token_basename(token)


def _is_python_executable(token: str) -> bool:
    return bool(PYTHON_EXECUTABLE_RE.fullmatch(_token_basename(token)))


def _direct_script_index(tokens: list[str], script_name: str) -> int | None:
    return command_contract.direct_script_index(tokens, script_name)


def _nested_python_invocation(command: str, script_name: str) -> bool:
    script = re.escape(script_name)
    patterns = [
        rf"subprocess\.(?:run|check_call|check_output|Popen)\s*\(\s*\[[^\]]{{0,1600}}?[\"'](?:python(?:\d+(?:\.\d+)?)?|py)[\"'][^\]]{{0,1600}}?{script}",
        rf"os\.system\s*\([^\)]{{0,1600}}?(?:python(?:\d+(?:\.\d+)?)?|py)\s+[^\)]{{0,1600}}?{script}",
    ]
    return any(re.search(pattern, command, re.I | re.S) for pattern in patterns)


def _wrapper_invocation(tokens: list[str]) -> tuple[str, int] | None:
    return command_contract.wrapper_invocation(tokens)


def _detect_kb_script(command: str, tokens: list[str]) -> tuple[str, str]:
    for key, script_name in KB_SCRIPT_PATTERNS.items():
        if _direct_script_index(tokens, script_name) is not None:
            return key, "direct"
    wrapped = _wrapper_invocation(tokens)
    if wrapped:
        return wrapped[0], "wrapper"
    for key, script_name in KB_SCRIPT_PATTERNS.items():
        if script_name in command and _nested_python_invocation(command, script_name):
            return key, "nested_python"
    return "", ""


def _script_index(tokens: list[str], script_name: str) -> int | None:
    return command_contract.script_index(tokens, script_name)


def _is_help_invocation(tokens: list[str], script_name: str) -> bool:
    return command_contract.is_help_invocation(tokens, script_name)


def _script_name(script_key: str) -> str:
    return KB_SCRIPT_PATTERNS.get(script_key, "")


def _first_positional_after_script(tokens: list[str], script_name: str) -> str:
    return command_contract.first_positional(tokens, script_name)


def _parse_repeated_flag(tokens: list[str], flag: str) -> list[str]:
    return command_contract.repeated_flag_values(tokens, flag)


def _parse_optional_flag(tokens: list[str], flag: str, default: str = "") -> str:
    values = _parse_repeated_flag(tokens, flag)
    return values[-1] if values else default


def _inline_closeout_payload(tokens: list[str]) -> dict[str, Any]:
    raw = _parse_optional_flag(tokens, "--json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _closeout_command_fallback(tokens: list[str]) -> dict[str, Any]:
    """Compatibility-only estimate for silent/legacy successful closeout calls."""
    payload = _inline_closeout_payload(tokens)
    queries = _dedupe([
        *_string_list(payload.get("queries")),
        *_parse_repeated_flag(tokens, "--query"),
    ])
    hit_count = _coerce_int(payload.get("hit_count", _parse_optional_flag(tokens, "--hit-count", "0")), 0)
    rag_calls_flag = _parse_optional_flag(tokens, "--rag-calls")
    if "rag_calls" in payload:
        rag_calls = _coerce_int(payload.get("rag_calls"), 0)
    elif rag_calls_flag:
        rag_calls = _coerce_int(rag_calls_flag, 0)
    else:
        rag_calls = max(len(queries), 1 if hit_count > 0 else 0)

    legacy_requested = _dedupe([
        *_string_list(payload.get("used_entry_ids")),
        *_parse_repeated_flag(tokens, "--used"),
    ])
    payload_effects = payload.get("adoption_effects") if isinstance(payload.get("adoption_effects"), dict) else {}
    requested_effects = _normalize_adoption_effects({
        effect: [
            *_string_list(payload_effects.get(effect)),
            *_parse_repeated_flag(tokens, f"--used-{effect}"),
        ]
        for effect in ADOPTION_EFFECTS
    })
    requested_entry_ids = _dedupe([
        *legacy_requested,
        *(entry_id for effect in ADOPTION_EFFECTS for entry_id in requested_effects[effect]),
    ])
    no_apply_use = "--no-apply-use" in tokens
    linked_retrieval_ids = _dedupe([
        *_string_list(payload.get("linked_retrieval_ids")),
        *_parse_repeated_flag(tokens, "--linked-retrieval-id"),
    ])
    # A silent successful closeout confirms the lifecycle call, but not which
    # per-entry heat/adoption operations actually succeeded. Keep requested and
    # confirmed adoption separate instead of presenting CLI intent as fact.
    adopted_entry_ids: list[str] = []
    adoption_effects = {effect: [] for effect in ADOPTION_EFFECTS}

    return {
        "linked_retrieval_ids": linked_retrieval_ids,
        "queries": queries,
        "hit_count": hit_count,
        "rag_calls": rag_calls,
        "requested_entry_ids": requested_entry_ids,
        "requested_legacy_used_entry_ids": legacy_requested,
        "requested_adoption_effects": requested_effects,
        "legacy_used_entry_ids": [],
        "adoption_effects": adoption_effects,
        "adopted_entry_ids": adopted_entry_ids,
        "long_term_adopted_entry_ids": adopted_entry_ids,
        "session_brief_used_entry_ids": [],
        "adopted_count": len(adopted_entry_ids),
        "used_entry_ids": adopted_entry_ids,
        "written_entry_ids": _dedupe([
            *_string_list(payload.get("written_entry_ids")),
            *_parse_repeated_flag(tokens, "--written"),
        ]),
        "updated_entry_ids": _dedupe([
            *_string_list(payload.get("updated_entry_ids")),
            *_parse_repeated_flag(tokens, "--updated"),
        ]),
        "session_brief_hit": (
            "--session-brief-hit" in tokens or _coerce_bool(payload.get("session_brief_hit"), False)
        ),
        "session_brief_help": (
            "--session-brief-help" in tokens or _coerce_bool(payload.get("session_brief_help"), False)
        ),
        "session_brief_ids": [],
        "heated_entry_ids": [],
        "heat_failed_entry_ids": [],
        "no_apply_use": no_apply_use,
        "closeout_data_source": "command_fallback",
        "adoption_data_source": "command_fallback",
        "adoption_confirmation": (
            "confirmed_not_applied_by_flag" if no_apply_use else "unconfirmed_command_fallback"
        ),
        "closeout_output_parsed": False,
        "closeout_recorded": True,
        "json_file_unresolved": bool(_parse_optional_flag(tokens, "--json-file")),
    }


def _closeout_info(tokens: list[str], output: str, execution_success: bool | None) -> dict[str, Any]:
    fallback = _closeout_command_fallback(tokens)
    fallback["closeout_recorded"] = execution_success is True
    payload = _closeout_payload_from_output(output)
    if not payload:
        return fallback

    status = str(payload.get("status") or "")
    closeout_recorded = status in {"ok", "partial_failure"} or execution_success is True
    queries = _string_list(payload.get("queries")) if "queries" in payload else fallback["queries"]
    hit_count = _coerce_int(payload.get("hit_count"), 0) if "hit_count" in payload else fallback["hit_count"]
    rag_calls = _coerce_int(payload.get("rag_calls"), 0) if "rag_calls" in payload else fallback["rag_calls"]
    linked_retrieval_ids = (
        _string_list(payload.get("linked_retrieval_ids"))
        if "linked_retrieval_ids" in payload
        else fallback["linked_retrieval_ids"]
    )

    recorded_used = (
        _string_list(payload.get("used_entry_ids"))
        if "used_entry_ids" in payload
        else list(fallback["requested_entry_ids"])
    )
    session_brief_used = _string_list(payload.get("session_brief_used_entry_ids"))
    failed_ids = _string_list(payload.get("heat_failed_entry_ids"))
    if "adopted_entry_ids" in payload:
        long_term_adopted = _string_list(payload.get("adopted_entry_ids"))
    elif payload.get("adoption_applied") is False:
        long_term_adopted = []
    else:
        failed = set(failed_ids)
        long_term_adopted = [entry_id for entry_id in recorded_used if entry_id not in failed]
    # Session briefs are short-term runtime context, not long-term KB adoption.
    # Report them separately so they never inflate classified adoption totals.
    adopted_entry_ids = _dedupe(long_term_adopted)

    payload_effects = payload.get("adoption_effects") if isinstance(payload.get("adoption_effects"), dict) else None
    effect_candidates = (
        _normalize_adoption_effects({effect: _string_list(payload_effects.get(effect)) for effect in ADOPTION_EFFECTS})
        if payload_effects is not None
        else fallback["requested_adoption_effects"]
    )
    adopted_set = set(adopted_entry_ids)
    adoption_effects = {
        effect: [entry_id for entry_id in effect_candidates[effect] if entry_id in adopted_set]
        for effect in ADOPTION_EFFECTS
    }
    classified = {entry_id for effect in ADOPTION_EFFECTS for entry_id in adoption_effects[effect]}
    legacy_used_entry_ids = [
        entry_id for entry_id in long_term_adopted
        if entry_id not in classified
    ]

    written_entry_ids = (
        _string_list(payload.get("written_entry_ids"))
        if "written_entry_ids" in payload
        else fallback["written_entry_ids"]
    )
    updated_entry_ids = (
        _string_list(payload.get("updated_entry_ids"))
        if "updated_entry_ids" in payload
        else fallback["updated_entry_ids"]
    )
    session_brief_ids = _string_list(payload.get("session_brief_ids"))
    no_apply_use = fallback["no_apply_use"] or payload.get("adoption_applied") is False

    return {
        "linked_retrieval_ids": linked_retrieval_ids,
        "queries": queries,
        "hit_count": hit_count,
        "rag_calls": rag_calls,
        "requested_entry_ids": _dedupe([*recorded_used, *session_brief_used]),
        "requested_legacy_used_entry_ids": fallback["requested_legacy_used_entry_ids"],
        "requested_adoption_effects": (
            _normalize_adoption_effects({
                effect: _string_list(payload_effects.get(effect)) for effect in ADOPTION_EFFECTS
            })
            if payload_effects is not None
            else fallback["requested_adoption_effects"]
        ),
        "legacy_used_entry_ids": legacy_used_entry_ids,
        "adoption_effects": adoption_effects,
        "adopted_entry_ids": adopted_entry_ids,
        "long_term_adopted_entry_ids": long_term_adopted,
        "session_brief_used_entry_ids": session_brief_used,
        "adopted_count": len(adopted_entry_ids),
        "used_entry_ids": adopted_entry_ids,
        "written_entry_ids": written_entry_ids,
        "updated_entry_ids": updated_entry_ids,
        "session_brief_hit": _coerce_bool(payload.get("session_brief_hit"), fallback["session_brief_hit"]),
        "session_brief_help": _coerce_bool(payload.get("session_brief_help"), fallback["session_brief_help"]),
        "session_brief_ids": session_brief_ids,
        "heated_entry_ids": _string_list(payload.get("heated_entry_ids")),
        "heat_failed_entry_ids": failed_ids,
        "no_apply_use": no_apply_use,
        "closeout_status": status,
        "closeout_data_source": "output_json",
        "adoption_data_source": (
            "output_json" if payload_effects is not None else "output_json_with_command_effect_fallback"
        ),
        "adoption_confirmation": "confirmed_output_json",
        "closeout_output_parsed": True,
        "closeout_recorded": closeout_recorded,
        "json_file_unresolved": False,
    }


def _session_rows(path: Path) -> list[dict[str, Any]]:
    return _read_jsonl(path)


def _call_commands(payload: dict[str, Any]) -> tuple[list[str], str]:
    payload_type = str(payload.get("type") or "")
    if payload_type == "function_call":
        arguments = _json_loads(str(payload.get("arguments") or "{}"))
        command = str(arguments.get("cmd") or "")
        return ([command] if command else []), "function_call"
    if payload_type == "custom_tool_call" and payload.get("name") == "exec":
        return _custom_exec_commands(str(payload.get("input") or "")), "custom_tool_call_exec"
    return [], ""


def _parse_command_call(
    call: dict[str, Any],
    output_by_call_id: dict[str, str],
    *,
    turn_id: str = "",
) -> list[dict[str, Any]]:
    payload = call.get("payload") or {}
    commands, source_format = _call_commands(payload)
    if not commands:
        return []

    call_id = str(payload.get("call_id") or "")
    output = output_by_call_id.get(call_id, "")
    exit_code = _parse_exit_code(output)
    success = _execution_success(output)
    parsed: list[dict[str, Any]] = []
    for command in commands:
        tokens = _parse_cli_tokens(command)
        script_key, invocation_kind = _detect_kb_script(command, tokens)
        if script_key and _is_help_invocation(tokens, _script_name(script_key)):
            script_key, invocation_kind = "", "help_only"

        info: dict[str, Any] = {
            "command": command,
            "source_format": source_format,
            "call_id": call_id,
            "turn_id": turn_id,
            "script_key": script_key,
            "invocation_kind": invocation_kind,
            "exit_code": exit_code,
            "success": success is True,
            "execution_status": "success" if success is True else "failed" if success is False else "unknown",
        }

        if script_key == "kb_rag_context":
            info["query"] = _first_positional_after_script(tokens, _script_name(script_key))
            info["hit_count"] = _parse_hit_count_from_output(output)
            retrieval_id = (
                _parse_retrieval_id_from_output(output)
                or _parse_optional_flag(tokens, "--retrieval-id")
            )
            info["retrieval_id"] = (
                retrieval_id if RETRIEVAL_ID_RE.fullmatch(retrieval_id) else ""
            )
        elif script_key == "kb_search":
            info["query"] = _first_positional_after_script(tokens, _script_name(script_key))
            info["hit_count"] = _parse_hit_count_from_output(output)
        elif script_key == "kb_closeout":
            info.update(_closeout_info(tokens, output, success))
        elif script_key == "kb_add":
            info["inline_write"] = "--entry-b64" in tokens or "--json-b64" in tokens
        elif script_key == "kb_update":
            info["is_use"] = " use " in f" {command} "
        parsed.append(info)
    return parsed


def _match_retrieval_closeouts(
    retrievals: list[dict[str, Any]],
    closeouts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Match exact retrieval IDs first, then use capacity-safe legacy fallbacks."""
    remaining = [max(0, _coerce_int(closeout.get("rag_calls"), 0)) for closeout in closeouts]
    linked_ids_by_closeout = [
        set(_string_list(closeout.get("linked_retrieval_ids"))) for closeout in closeouts
    ]
    matched_link_ids_by_closeout: list[set[str]] = [set() for _ in closeouts]
    matched_retrievals: set[int] = set()
    pairings: list[dict[str, Any]] = []

    exact_id_candidates: list[tuple[int, int, int, int]] = []
    for retrieval_index, retrieval in enumerate(retrievals):
        retrieval_id = str(retrieval.get("retrieval_id") or "").strip()
        if not RETRIEVAL_ID_RE.fullmatch(retrieval_id):
            continue
        retrieval_sequence = _coerce_int(retrieval.get("sequence"), -1)
        retrieval_turn = str(retrieval.get("turn_id") or "")
        for closeout_index, closeout in enumerate(closeouts):
            closeout_sequence = _coerce_int(closeout.get("sequence"), -1)
            if (
                remaining[closeout_index] <= 0
                or closeout_sequence <= retrieval_sequence
                or retrieval_id not in linked_ids_by_closeout[closeout_index]
            ):
                continue
            closeout_turn = str(closeout.get("turn_id") or "")
            same_turn_rank = 0 if retrieval_turn and retrieval_turn == closeout_turn else 1
            distance = closeout_sequence - retrieval_sequence
            exact_id_candidates.append(
                (same_turn_rank, distance, retrieval_index, closeout_index)
            )

    for _same_turn_rank, _distance, retrieval_index, closeout_index in sorted(
        exact_id_candidates
    ):
        if retrieval_index in matched_retrievals or remaining[closeout_index] <= 0:
            continue
        retrieval = retrievals[retrieval_index]
        closeout = closeouts[closeout_index]
        retrieval_id = str(retrieval.get("retrieval_id") or "").strip()
        matched_retrievals.add(retrieval_index)
        remaining[closeout_index] -= 1
        matched_link_ids_by_closeout[closeout_index].add(retrieval_id)
        pairings.append(
            {
                "retrieval_sequence": _coerce_int(retrieval.get("sequence"), -1),
                "closeout_sequence": _coerce_int(closeout.get("sequence"), -1),
                "turn_id": str(retrieval.get("turn_id") or ""),
                "query": str(retrieval.get("query") or ""),
                "retrieval_id": retrieval_id,
                "match_kind": "retrieval_id",
            }
        )

    fallback_remaining = [
        max(0, remaining[index] - len(linked_ids - matched_link_ids_by_closeout[index]))
        for index, linked_ids in enumerate(linked_ids_by_closeout)
    ]
    candidates: list[tuple[int, int, int, int, int]] = []
    for retrieval_index, retrieval in enumerate(retrievals):
        if retrieval_index in matched_retrievals:
            continue
        retrieval_sequence = _coerce_int(retrieval.get("sequence"), -1)
        retrieval_turn = str(retrieval.get("turn_id") or "")
        retrieval_query = _normalized_query(str(retrieval.get("query") or ""))
        retrieval_id = str(retrieval.get("retrieval_id") or "").strip()
        for closeout_index, closeout in enumerate(closeouts):
            closeout_sequence = _coerce_int(closeout.get("sequence"), -1)
            if fallback_remaining[closeout_index] <= 0 or closeout_sequence <= retrieval_sequence:
                continue
            if (
                retrieval_id
                and linked_ids_by_closeout[closeout_index]
                and retrieval_id not in linked_ids_by_closeout[closeout_index]
            ):
                continue
            closeout_turn = str(closeout.get("turn_id") or "")
            closeout_queries = {
                _normalized_query(query)
                for query in _string_list(closeout.get("queries"))
                if _normalized_query(query)
            }
            same_turn = bool(retrieval_turn and closeout_turn and retrieval_turn == closeout_turn)
            exact_query = bool(retrieval_query and retrieval_query in closeout_queries)
            if closeout_queries and retrieval_query and not exact_query:
                continue
            if same_turn and exact_query:
                score = 4
            elif exact_query:
                score = 3
            elif same_turn and (not retrieval_query or not closeout_queries):
                score = 2
            elif not closeout_queries:
                score = 1
            else:
                continue
            distance = closeout_sequence - retrieval_sequence
            candidates.append((-score, distance, -retrieval_sequence, retrieval_index, closeout_index))

    for _negative_score, _distance, _negative_sequence, retrieval_index, closeout_index in sorted(candidates):
        if retrieval_index in matched_retrievals or fallback_remaining[closeout_index] <= 0:
            continue
        matched_retrievals.add(retrieval_index)
        fallback_remaining[closeout_index] -= 1
        retrieval = retrievals[retrieval_index]
        closeout = closeouts[closeout_index]
        pairings.append({
            "retrieval_sequence": _coerce_int(retrieval.get("sequence"), -1),
            "closeout_sequence": _coerce_int(closeout.get("sequence"), -1),
            "turn_id": str(retrieval.get("turn_id") or ""),
            "query": str(retrieval.get("query") or ""),
            "retrieval_id": str(retrieval.get("retrieval_id") or ""),
            "match_kind": "legacy_fallback",
        })

    unmatched = [
        retrieval for index, retrieval in enumerate(retrievals)
        if index not in matched_retrievals
    ]
    pairings.sort(key=lambda item: (item["retrieval_sequence"], item["closeout_sequence"]))
    return pairings, unmatched


def _parse_session(path: Path) -> dict[str, Any]:
    rows = _session_rows(path)
    metas: list[dict[str, Any]] = []
    user_messages: list[str] = []
    instruction_messages: list[str] = []
    guard_observed = False
    output_by_call_id: dict[str, str] = {}
    calls: list[dict[str, Any]] = []
    source_format_counts: collections.Counter[str] = collections.Counter()
    unparsed_exec_count = 0
    current_turn_id = ""
    synthetic_turn = 0
    last_user_turn = ""

    for row in rows:
        row_type = row.get("type")
        payload = row.get("payload") or {}
        if row_type == "session_meta" and isinstance(payload, dict):
            metas.append(payload)
        elif row_type == "turn_context" and isinstance(payload, dict):
            current_turn_id = _turn_id(payload, current_turn_id)
        elif row_type == "event_msg" and payload.get("type") == "task_started":
            current_turn_id = _turn_id(payload, current_turn_id)
        elif row_type == "event_msg" and payload.get("type") == "user_message":
            user_messages.append(str(payload.get("message") or payload.get("text") or ""))
            observed_turn = _turn_id(payload, "")
            if observed_turn:
                current_turn_id = observed_turn
            elif not current_turn_id or current_turn_id == last_user_turn:
                synthetic_turn += 1
                current_turn_id = f"user-turn-{synthetic_turn}"
            last_user_turn = current_turn_id
        elif row_type == "response_item" and payload.get("type") == "message" and payload.get("role") in {"user", "developer"}:
            if payload.get("role") == "user":
                instruction_text = _user_response_text(payload.get("content"))
            else:
                instruction_text = _output_text(payload.get("content"))
            if instruction_text.strip():
                instruction_messages.append(instruction_text)
            if _has_subagent_no_kb_guard(instruction_text):
                guard_observed = True
        elif row_type == "response_item" and payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or "")
            if call_id:
                output_by_call_id[call_id] = _output_text(payload.get("output"))
        elif row_type == "response_item" and payload.get("type") == "function_call":
            arguments = _json_loads(str(payload.get("arguments") or "{}"))
            if payload.get("name") in {"exec", "exec_command"} or "cmd" in arguments:
                calls.append({"row": row, "turn_id": _turn_id(payload, current_turn_id)})
                source_format_counts["function_call"] += 1
                if not str(arguments.get("cmd") or ""):
                    unparsed_exec_count += 1
        elif row_type == "response_item" and payload.get("type") == "custom_tool_call" and payload.get("name") == "exec":
            calls.append({"row": row, "turn_id": _turn_id(payload, current_turn_id)})
            source_format_counts["custom_tool_call_exec"] += 1
            if not _custom_exec_commands(str(payload.get("input") or "")):
                unparsed_exec_count += 1

    meta, rollout_id, session_meta_selection = _select_session_meta(path, metas)
    parsed_calls: list[dict[str, Any]] = []
    for call_info in calls:
        for item in _parse_command_call(
            call_info["row"],
            output_by_call_id,
            turn_id=str(call_info.get("turn_id") or ""),
        ):
            item["sequence"] = len(parsed_calls)
            parsed_calls.append(item)

    detected_kb_calls = [call for call in parsed_calls if call.get("script_key")]
    kb_calls = [
        call for call in detected_kb_calls
        if call.get("success") or (call.get("script_key") == "kb_closeout" and call.get("closeout_recorded"))
    ]
    failed_kb_call_count = sum(call.get("execution_status") == "failed" for call in detected_kb_calls)
    execution_unknown_kb_call_count = sum(call.get("execution_status") == "unknown" for call in detected_kb_calls)
    counters: collections.Counter[str] = collections.Counter()
    rag_queries: list[str] = []
    retrieval_events: list[dict[str, Any]] = []
    closeout_queries: list[str] = []
    closeout_linked_retrieval_ids: list[str] = []
    closeout_hit_count_total = 0
    closeout_adopted_count_total = 0
    closeout_written_count_total = 0
    closeout_updated_count_total = 0
    session_brief_help_count = 0
    session_brief_used_entry_ids: list[str] = []
    session_brief_written_count = 0
    derived_hit_count_total = 0
    adoption_effect_candidates: dict[str, list[str]] = {effect: [] for effect in ADOPTION_EFFECTS}
    requested_adoption_effect_candidates: dict[str, list[str]] = {effect: [] for effect in ADOPTION_EFFECTS}
    legacy_used_entry_ids: list[str] = []
    requested_legacy_used_entry_ids: list[str] = []
    requested_entry_ids: list[str] = []
    adopted_entry_ids: list[str] = []
    closeout_data_source_counts: collections.Counter[str] = collections.Counter()
    adoption_confirmation_counts: collections.Counter[str] = collections.Counter()

    for call in kb_calls:
        script_key = str(call.get("script_key", ""))
        counters[script_key] += 1
        if script_key in {"kb_rag_context", "kb_search"}:
            query = str(call.get("query", "") or "")
            if query:
                rag_queries.append(query)
            derived_hit_count_total += _coerce_int(call.get("hit_count"), 0)
            retrieval_id = str(call.get("retrieval_id") or "").strip()
            if script_key == "kb_rag_context" and retrieval_id:
                retrieval_events.append(
                    {
                        "retrieval_id": retrieval_id,
                        "query": query,
                        "hit_count": _coerce_int(call.get("hit_count"), 0),
                        "turn_id": str(call.get("turn_id") or ""),
                        "sequence": _coerce_int(call.get("sequence"), -1),
                    }
                )
        elif script_key == "kb_closeout":
            closeout_data_source_counts[str(call.get("closeout_data_source") or "unknown")] += 1
            adoption_confirmation_counts[str(call.get("adoption_confirmation") or "unknown")] += 1
            closeout_queries.extend(str(item) for item in call.get("queries", []) if str(item).strip())
            closeout_linked_retrieval_ids.extend(
                str(item) for item in call.get("linked_retrieval_ids", []) if str(item).strip()
            )
            closeout_hit_count_total += _coerce_int(call.get("hit_count"), 0)
            closeout_adopted_count_total += _coerce_int(call.get("adopted_count"), 0)
            legacy_used_entry_ids.extend(str(item) for item in call.get("legacy_used_entry_ids", []))
            requested_legacy_used_entry_ids.extend(
                str(item) for item in call.get("requested_legacy_used_entry_ids", [])
            )
            requested_entry_ids.extend(str(item) for item in call.get("requested_entry_ids", []))
            adopted_entry_ids.extend(str(item) for item in call.get("adopted_entry_ids", []))
            session_brief_used_entry_ids.extend(
                str(item) for item in call.get("session_brief_used_entry_ids", [])
            )
            session_brief_written_count += len(call.get("session_brief_ids", []))
            call_effects = call.get("adoption_effects") if isinstance(call.get("adoption_effects"), dict) else {}
            requested_effects = (
                call.get("requested_adoption_effects")
                if isinstance(call.get("requested_adoption_effects"), dict)
                else {}
            )
            for effect in ADOPTION_EFFECTS:
                adoption_effect_candidates[effect].extend(str(item) for item in call_effects.get(effect, []))
                requested_adoption_effect_candidates[effect].extend(
                    str(item) for item in requested_effects.get(effect, [])
                )
            closeout_written_count_total += len(call.get("written_entry_ids", []))
            closeout_updated_count_total += len(call.get("updated_entry_ids", []))
            if call.get("session_brief_help"):
                session_brief_help_count += 1

    adoption_effects = _normalize_adoption_effects(adoption_effect_candidates)
    requested_adoption_effects = _normalize_adoption_effects(requested_adoption_effect_candidates)
    legacy_used_entry_ids = _dedupe(legacy_used_entry_ids)
    requested_legacy_used_entry_ids = _dedupe(requested_legacy_used_entry_ids)
    requested_entry_ids = _dedupe(requested_entry_ids)
    adopted_entry_ids = _dedupe(adopted_entry_ids)
    session_brief_used_entry_ids = _dedupe(session_brief_used_entry_ids)
    closeout_linked_retrieval_ids = _dedupe(closeout_linked_retrieval_ids)
    adopted_count = len(adopted_entry_ids)

    retrieval_calls = [
        call for call in kb_calls if call.get("script_key") in {"kb_rag_context", "kb_search"}
    ]
    closeout_calls = [
        call for call in kb_calls
        if call.get("script_key") == "kb_closeout" and call.get("closeout_recorded")
    ]
    closeout_pairings, unmatched_retrieval_calls = _match_retrieval_closeouts(retrieval_calls, closeout_calls)
    unmatched_retrieval_call_count = len(unmatched_retrieval_calls)

    user_text = " ".join(user_messages)
    instruction_text = " ".join(instruction_messages)
    subagent = _subagent_info(meta)
    forbid_kb_scope = _forbid_kb_scope(user_text, is_subagent=bool(subagent["is_subagent"]))
    instruction_forbid_scope = _forbid_kb_scope(
        instruction_text,
        is_subagent=bool(subagent["is_subagent"]),
    )
    forbid_kb = (
        forbid_kb_scope == "global"
        or (bool(subagent["is_subagent"]) and forbid_kb_scope == "subagent_only")
        or (
            bool(subagent["is_subagent"])
            and (guard_observed or instruction_forbid_scope in {"global", "subagent_only"})
        )
    )
    forbid_write_kb = forbid_kb_scope == "write_only" or (
        bool(subagent["is_subagent"]) and instruction_forbid_scope == "write_only"
    )
    runtime_audit = bool(RUNTIME_AUDIT_RE.search(user_text))
    explicit_kb = bool(EXPLICIT_KB_RE.search(user_text)) and not runtime_audit
    memory_needed = bool(MEMORY_NEEDED_RE.search(user_text))
    generic_skill_install = bool(GENERIC_SKILL_INSTALL_RE.search(user_text)) and not explicit_kb
    if generic_skill_install and not CONTINUITY_RE.search(user_text):
        memory_needed = False
    instruction_non_kb_task = bool(SUBAGENT_NON_KB_TASK_RE.search(instruction_text))
    instruction_kb_task = bool(SUBAGENT_KB_TASK_RE.search(instruction_text)) and not instruction_non_kb_task
    event_kb_task = bool(SUBAGENT_KB_TASK_RE.search(user_text))
    subagent_kb_task = instruction_kb_task or event_kb_task
    subagent_authorization = "not_applicable"
    subagent_authorization_source = ""
    if subagent["is_subagent"]:
        if forbid_kb:
            subagent_authorization = "forbidden"
            subagent_authorization_source = "explicit_no_kb_guard"
        elif instruction_non_kb_task:
            subagent_authorization = "not_authorized"
            subagent_authorization_source = "response_instruction"
        elif instruction_kb_task:
            subagent_authorization = "authorized"
            subagent_authorization_source = "response_instruction"
        elif event_kb_task:
            subagent_authorization = "authorized"
            subagent_authorization_source = "event_user_message"
        else:
            # Real forked rollouts often expose the parent's question as the
            # event user_message while the child task itself is unavailable.
            subagent_authorization = "unknown"
            subagent_authorization_source = "child_task_unavailable"
    subagent_missing_no_kb_guard = (
        bool(subagent["is_subagent"])
        and subagent_authorization == "not_authorized"
        and not guard_observed
        and forbid_kb_scope not in {"global", "subagent_only"}
        and instruction_forbid_scope not in {"global", "subagent_only"}
    )
    kb_expected = not forbid_kb and (explicit_kb or memory_needed)
    if subagent["is_subagent"]:
        kb_expected = subagent_authorization == "authorized"
    retrieval_used = counters["kb_rag_context"] + counters["kb_search"] > 0
    kb_used = bool(kb_calls)
    kb_write_used = counters["kb_add"] + counters["kb_update"] + counters["kb_closeout"] > 0
    forbidden_kb_used = (forbid_kb and kb_used) or (forbid_write_kb and kb_write_used)
    closeout_called = counters["kb_closeout"] > 0
    missing_closeout = unmatched_retrieval_call_count > 0
    effective_hit_count = closeout_hit_count_total if closeout_called else derived_hit_count_total
    adopted = adopted_count > 0
    requested_adoption_count = len(requested_entry_ids)
    adoption_unconfirmed = (
        requested_adoption_count > 0
        and adopted_count == 0
        and adoption_confirmation_counts.get("unconfirmed_command_fallback", 0) > 0
    )
    wrote_or_updated = (closeout_written_count_total + closeout_updated_count_total) > 0 or counters["kb_add"] > 0

    verdict = "no_kb_needed"
    score = 0
    reasons: list[str] = []
    if explicit_kb:
        reasons.append("user explicitly mentioned KB/history")
    if memory_needed:
        reasons.append("user task looks history-dependent")
    if generic_skill_install:
        reasons.append("generic interview/resume skill work does not imply personal-kb usage")
    if forbid_kb:
        reasons.append("user/task forbids KB usage")
    if forbid_write_kb:
        reasons.append("user/task forbids KB write/heat/closeout scripts")
    if forbid_kb_scope == "subagent_only":
        reasons.append("KB prohibition applies to subagents, not the main session")
    if subagent["is_subagent"]:
        reasons.append("session is a subagent; parent session owns normal KB lifecycle")
        reasons.append(f"subagent KB authorization={subagent_authorization}")
    if subagent_kb_task:
        reasons.append("subagent task explicitly asks for KB retrieval/audit/maintenance")
    if subagent_missing_no_kb_guard:
        reasons.append("ordinary subagent prompt is missing an explicit no-personal-kb-script guard")
    if counters["kb_rag_context"]:
        reasons.append(f"kb_rag_context called {counters['kb_rag_context']} time(s)")
    if counters["kb_search"]:
        reasons.append(f"kb_search called {counters['kb_search']} time(s)")
    if closeout_called:
        reasons.append(
            f"kb_closeout recorded hit_count={closeout_hit_count_total}, adopted={adopted_count}, "
            f"written={closeout_written_count_total}, updated={closeout_updated_count_total}"
        )
    if missing_closeout:
        reasons.append(f"{unmatched_retrieval_call_count} retrieval call(s) have no later matching closeout")
    if adoption_unconfirmed:
        reasons.append("adoption was requested but per-entry success is unconfirmed because closeout output was silent")

    if subagent["is_subagent"]:
        if forbidden_kb_used:
            verdict = "subagent_forbidden_but_used"
            score = -2
        elif kb_expected and not kb_used:
            verdict = "subagent_kb_task_not_used"
            score = -1
        elif kb_used and subagent_authorization == "unknown":
            verdict = "subagent_kb_used_authorization_unknown"
            score = 0
        elif kb_used and not kb_expected:
            verdict = "subagent_unexpected_kb_used"
            score = -1
        elif kb_used:
            verdict = "subagent_kb_used"
            score = 1
        else:
            verdict = "subagent_no_kb_expected"
            score = 0
    elif forbidden_kb_used:
        verdict = "forbidden_but_used"
        score = -2
    elif kb_expected and not retrieval_used:
        verdict = "needed_but_not_used"
        score = -1
    elif retrieval_used and missing_closeout:
        verdict = "used_without_closeout"
        score = 1 if effective_hit_count > 0 else 0
    elif retrieval_used and effective_hit_count <= 0:
        verdict = "used_no_hit"
        score = 0
    elif effective_hit_count > 0 and adopted:
        verdict = "used_hit_adopted"
        score = 4
    elif effective_hit_count > 0 and adoption_unconfirmed:
        verdict = "used_hit_adoption_unconfirmed"
        score = 2
    elif effective_hit_count > 0 and wrote_or_updated:
        verdict = "used_hit_recorded_not_adopted"
        score = 3
    elif effective_hit_count > 0:
        verdict = "used_hit_not_adopted"
        score = 2
    elif kb_used:
        verdict = "kb_used_no_effect_signal"
        score = 1

    thread_source = str(meta.get("thread_source") or "")
    source = _source_text(meta.get("source") or thread_source or "unknown")
    session_ts = str(meta.get("timestamp") or "")
    if not session_ts:
        session_ts = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).astimezone().isoformat(timespec="seconds")

    analysis = {
        "analysis_ts": now_iso(),
        "event": "kb_codex_effectiveness_record",
        "session_id": str(meta.get("id") or path.stem),
        "session_path": str(path),
        "session_file": path.name,
        "rollout_id": rollout_id,
        "session_meta_count": len(metas),
        "session_meta_selection": session_meta_selection,
        "session_ts": session_ts,
        "cwd": str(meta.get("cwd") or ""),
        "source": source,
        "thread_source": thread_source,
        "is_subagent": subagent["is_subagent"],
        "subagent_parent_thread_id": subagent["subagent_parent_thread_id"],
        "subagent_role": subagent["subagent_role"],
        "subagent_nickname": subagent["subagent_nickname"],
        "subagent_depth": subagent["subagent_depth"],
        "subagent_kb_task": subagent_kb_task,
        "subagent_kb_authorization": subagent_authorization,
        "subagent_kb_authorization_source": subagent_authorization_source,
        "subagent_instruction_observed": bool(instruction_text.strip()),
        "subagent_no_kb_guard_observed": guard_observed,
        "subagent_missing_no_kb_guard": subagent_missing_no_kb_guard,
        "generic_skill_install": generic_skill_install,
        "runtime_audit": runtime_audit,
        "user_message_count": len(user_messages),
        "user_excerpt": _short_text(user_text, 260),
        "kb_expected": kb_expected,
        "forbid_kb": forbid_kb,
        "forbid_kb_scope": forbid_kb_scope,
        "forbid_write_kb": forbid_write_kb,
        "forbidden_kb_used": forbidden_kb_used,
        "kb_expected_reasons": reasons[:8],
        "kb_used": kb_used,
        "kb_write_used": kb_write_used,
        "retrieval_used": retrieval_used,
        "closeout_called": closeout_called,
        "missing_closeout": missing_closeout,
        "retrieval_call_count": len(retrieval_calls),
        "paired_retrieval_call_count": len(closeout_pairings),
        "unpaired_retrieval_call_count": unmatched_retrieval_call_count,
        "unpaired_rag_queries": _dedupe([
            str(call.get("query") or "") for call in unmatched_retrieval_calls if str(call.get("query") or "")
        ])[:10],
        "closeout_pairings": closeout_pairings[:20],
        "kb_call_counts": dict(counters),
        "rag_queries": _dedupe(rag_queries)[:10],
        "retrieval_events": retrieval_events[:20],
        "retrieval_ids": _dedupe(
            [str(event.get("retrieval_id") or "") for event in retrieval_events]
        )[:20],
        "retrieval_id_missing_count": max(
            0,
            counters["kb_rag_context"] - len(retrieval_events),
        ),
        "closeout_queries": _dedupe(closeout_queries)[:10],
        "closeout_linked_retrieval_ids": closeout_linked_retrieval_ids[:20],
        "closeout_hit_count_total": closeout_hit_count_total,
        "derived_hit_count_total": derived_hit_count_total,
        "adoption_effects": adoption_effects,
        "adoption_effect_counts": {effect: len(adoption_effects[effect]) for effect in ADOPTION_EFFECTS},
        "requested_adoption_effects": requested_adoption_effects,
        "requested_adoption_effect_counts": {
            effect: len(requested_adoption_effects[effect]) for effect in ADOPTION_EFFECTS
        },
        "requested_legacy_used_entry_ids": requested_legacy_used_entry_ids,
        "requested_entry_ids": requested_entry_ids,
        "requested_adoption_count": requested_adoption_count,
        "legacy_used_entry_ids": legacy_used_entry_ids,
        "legacy_used_count": len(legacy_used_entry_ids),
        "adopted_entry_ids": adopted_entry_ids,
        "adopted_count": adopted_count,
        "closeout_adopted_count_total": closeout_adopted_count_total,
        "closeout_used_count_total": closeout_adopted_count_total,
        "closeout_written_count_total": closeout_written_count_total,
        "closeout_updated_count_total": closeout_updated_count_total,
        "session_brief_help_count": session_brief_help_count,
        "session_brief_used_entry_ids": session_brief_used_entry_ids,
        "session_brief_used_count": len(session_brief_used_entry_ids),
        "session_brief_written_count": session_brief_written_count,
        "closeout_data_source_counts": dict(closeout_data_source_counts),
        "closeout_command_fallback_count": closeout_data_source_counts.get("command_fallback", 0),
        "closeout_output_json_count": closeout_data_source_counts.get("output_json", 0),
        "adoption_confirmation_counts": dict(adoption_confirmation_counts),
        "adoption_unconfirmed": adoption_unconfirmed,
        "effect_verdict": verdict,
        "effect_score": score,
        "parser_version": PARSER_VERSION,
        "source_format_counts": dict(source_format_counts),
        "shell_call_count": len(calls),
        "parsed_shell_call_count": len(calls) - unparsed_exec_count,
        "unparsed_exec_count": unparsed_exec_count,
        "parser_coverage": round((len(calls) - unparsed_exec_count) / len(calls), 4) if calls else None,
        "detected_kb_call_count": len(detected_kb_calls),
        "failed_kb_call_count": failed_kb_call_count,
        "execution_unknown_kb_call_count": execution_unknown_kb_call_count,
        "execution_status_coverage": round(
            (len(detected_kb_calls) - execution_unknown_kb_call_count) / len(detected_kb_calls), 4
        ) if detected_kb_calls else None,
    }
    redacted, _findings = redact_value(analysis)
    return redacted


def _iter_session_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*.jsonl"))


def _delegated_parent_verdict(row: dict[str, Any]) -> tuple[str, int]:
    hit_count = max(
        _coerce_int(row.get("closeout_hit_count_total"), 0),
        _coerce_int(row.get("delegated_hit_count"), 0),
    )
    if hit_count <= 0:
        return "used_no_hit", 0
    if _coerce_int(row.get("adopted_count"), 0) > 0:
        return "used_hit_adopted", 4
    if row.get("adoption_unconfirmed"):
        return "used_hit_adoption_unconfirmed", 2
    if (
        _coerce_int(row.get("closeout_written_count_total"), 0)
        + _coerce_int(row.get("closeout_updated_count_total"), 0)
    ) > 0:
        return "used_hit_recorded_not_adopted", 3
    return "used_hit_not_adopted", 2


def _reconcile_parent_scout_links(rows: list[dict[str, Any]]) -> dict[str, Any]:
    retrievals_by_id: dict[str, list[tuple[int, dict[str, Any]]]] = collections.defaultdict(list)
    parents_by_linked_id: dict[str, list[int]] = collections.defaultdict(list)
    main_rows_by_identity: dict[str, set[int]] = collections.defaultdict(set)
    invalid_event_ids_by_row: dict[int, list[str]] = collections.defaultdict(list)

    for index, row in enumerate(rows):
        if not row.get("is_subagent"):
            for identity in (row.get("session_id"), row.get("rollout_id")):
                value = str(identity or "").strip()
                if value:
                    main_rows_by_identity[value].add(index)
            for retrieval_id in row.get("closeout_linked_retrieval_ids") or []:
                value = str(retrieval_id or "").strip()
                if value:
                    parents_by_linked_id[value].append(index)
        for event in row.get("retrieval_events") or []:
            if not isinstance(event, dict):
                continue
            retrieval_id = str(event.get("retrieval_id") or "").strip()
            if RETRIEVAL_ID_RE.fullmatch(retrieval_id):
                retrievals_by_id[retrieval_id].append((index, event))
            elif retrieval_id:
                invalid_event_ids_by_row[index].append(retrieval_id)

    duplicate_ids = {retrieval_id for retrieval_id, events in retrievals_by_id.items() if len(events) != 1}
    parent_linked_events: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    scout_linked_ids: dict[int, list[str]] = collections.defaultdict(list)
    scout_orphan_ids: dict[int, list[str]] = collections.defaultdict(list)
    scout_invalid_ids: dict[int, list[str]] = collections.defaultdict(list)
    for row_index, invalid_ids in invalid_event_ids_by_row.items():
        if rows[row_index].get("is_subagent"):
            scout_invalid_ids[row_index].extend(invalid_ids)

    for retrieval_id, events in retrievals_by_id.items():
        for child_index, event in events:
            child = rows[child_index]
            if not child.get("is_subagent"):
                continue
            authorization = str(child.get("subagent_kb_authorization") or "")
            role = str(child.get("subagent_role") or "").strip().lower()
            explicitly_forbidden = (
                authorization in {"forbidden", "not_authorized"}
                or bool(child.get("forbid_kb"))
            )
            authorized = not explicitly_forbidden and (
                authorization == "authorized" or role in {"kb_scout", "scout"}
            )
            if not authorized or retrieval_id in duplicate_ids:
                scout_invalid_ids[child_index].append(retrieval_id)
                continue
            parent_identity = str(child.get("subagent_parent_thread_id") or "").strip()
            if not parent_identity:
                scout_orphan_ids[child_index].append(retrieval_id)
                continue
            candidate_parent_indexes = set(parents_by_linked_id.get(retrieval_id, []))
            candidate_parent_indexes &= main_rows_by_identity.get(parent_identity, set())
            if len(candidate_parent_indexes) != 1:
                scout_orphan_ids[child_index].append(retrieval_id)
                continue
            parent_index = next(iter(candidate_parent_indexes))
            scout_linked_ids[child_index].append(retrieval_id)
            parent_linked_events[parent_index].append(
                {
                    "retrieval_id": retrieval_id,
                    "scout_session_id": str(child.get("session_id") or ""),
                    "query": str(event.get("query") or ""),
                    "hit_count": _coerce_int(event.get("hit_count"), 0),
                }
            )

    linked_count = 0
    orphan_count = 0
    invalid_count = 0
    missing_id_count = 0
    parent_session_count = 0
    for index, row in enumerate(rows):
        raw_unpaired_count = _coerce_int(row.get("unpaired_retrieval_call_count"), 0)
        if row.get("is_subagent"):
            linked = _dedupe(scout_linked_ids.get(index, []))
            orphan = _dedupe(scout_orphan_ids.get(index, []))
            invalid = _dedupe(scout_invalid_ids.get(index, []))
            row["parent_closeout_linked_retrieval_ids"] = linked
            row["orphan_scout_retrieval_ids"] = orphan
            row["invalid_scout_retrieval_ids"] = invalid
            row["delegated_closeout_linked"] = bool(linked)
            effective_unpaired_count = max(0, raw_unpaired_count - len(linked))
            row["effective_unpaired_retrieval_call_count"] = effective_unpaired_count
            row["effective_missing_closeout"] = effective_unpaired_count > 0
            linked_count += len(linked)
            orphan_count += len(orphan)
            invalid_count += len(invalid)
            missing_id_count += _coerce_int(row.get("retrieval_id_missing_count"), 0)
            continue

        linked_events = parent_linked_events.get(index, [])
        row["effective_unpaired_retrieval_call_count"] = raw_unpaired_count
        row["effective_missing_closeout"] = raw_unpaired_count > 0
        row["delegated_retrieval_events"] = linked_events[:20]
        row["delegated_retrieval_ids"] = _dedupe(
            [str(event.get("retrieval_id") or "") for event in linked_events]
        )[:20]
        row["delegated_retrieval_call_count"] = len(linked_events)
        row["delegated_hit_count"] = sum(
            _coerce_int(event.get("hit_count"), 0) for event in linked_events
        )
        row["effective_retrieval_used"] = bool(row.get("retrieval_used") or linked_events)
        if linked_events:
            parent_session_count += 1
            verdict, score = _delegated_parent_verdict(row)
            row["delegated_effect_verdict"] = verdict
            row["delegated_effect_score"] = score
            current_verdict = str(row.get("effect_verdict") or "")
            can_replace_verdict = (
                not row.get("retrieval_used")
                and raw_unpaired_count == 0
                and not row.get("forbid_kb")
                and not row.get("forbidden_kb_used")
                and current_verdict
                in {"needed_but_not_used", "no_kb_needed", "kb_used_no_effect_signal"}
            )
            if can_replace_verdict:
                row["effect_verdict"] = verdict
                row["effect_score"] = score

    dangling_parent_link_ids = sorted(
        retrieval_id
        for retrieval_id in parents_by_linked_id
        if retrieval_id not in retrievals_by_id
    )

    return {
        "parent_sessions_with_delegated_retrieval": parent_session_count,
        "scout_retrieval_linked_to_parent_closeout_count": linked_count,
        "orphan_scout_retrieval_id_count": orphan_count,
        "invalid_scout_retrieval_id_count": invalid_count,
        "scout_retrieval_missing_id_count": missing_id_count,
        "duplicate_retrieval_id_count": len(duplicate_ids),
        "duplicate_retrieval_ids_sample": sorted(duplicate_ids)[:20],
        "dangling_parent_linked_retrieval_id_count": len(dangling_parent_link_ids),
        "dangling_parent_linked_retrieval_ids_sample": dangling_parent_link_ids[:20],
    }


def _summary_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    verdict_counts: collections.Counter[str] = collections.Counter()
    main_verdict_counts: collections.Counter[str] = collections.Counter()
    subagent_verdict_counts: collections.Counter[str] = collections.Counter()
    total = len(rows)
    main_total = 0
    subagent_total = 0
    kb_expected = 0
    kb_used = 0
    kb_expected_and_used = 0
    all_kb_used = 0
    effective = 0
    adopted_sessions = 0
    adopted_count = 0
    all_adopted_sessions = 0
    all_adopted_count = 0
    missed = 0
    missing_closeout = 0
    forbidden_but_used = 0
    subagent_kb_expected = 0
    subagent_kb_used = 0
    subagent_forbidden_but_used = 0
    subagent_kb_task_not_used = 0
    subagent_no_kb_expected = 0
    subagent_unexpected_kb_used = 0
    subagent_kb_authorization_unknown = 0
    subagent_kb_used_authorization_unknown = 0
    subagent_missing_no_kb_guard = 0
    source_format_counts: collections.Counter[str] = collections.Counter()
    shell_call_count = 0
    parsed_shell_call_count = 0
    unparsed_exec_count = 0
    detected_kb_call_count = 0
    failed_kb_call_count = 0
    execution_unknown_kb_call_count = 0
    adoption_effect_counts: collections.Counter[str] = collections.Counter()
    all_adoption_effect_counts: collections.Counter[str] = collections.Counter()
    subagent_adoption_effect_counts: collections.Counter[str] = collections.Counter()
    classified_adoption_count = 0
    all_classified_adoption_count = 0
    subagent_classified_adoption_count = 0
    legacy_used_count = 0
    all_legacy_used_count = 0
    subagent_legacy_used_count = 0
    adoption_unconfirmed_sessions = 0
    requested_adoption_count = 0
    main_adoption_unconfirmed_sessions = 0
    main_requested_adoption_count = 0
    retrieval_call_count = 0
    paired_retrieval_call_count = 0
    unpaired_retrieval_call_count = 0
    raw_unpaired_retrieval_call_count = 0
    main_retrieval_call_count = 0
    main_paired_retrieval_call_count = 0
    main_unpaired_retrieval_call_count = 0
    closeout_data_source_counts: collections.Counter[str] = collections.Counter()
    for row in rows:
        source_format_counts.update(row.get("source_format_counts") or {})
        shell_call_count += _coerce_int(row.get("shell_call_count"), 0)
        parsed_shell_call_count += _coerce_int(row.get("parsed_shell_call_count"), 0)
        unparsed_exec_count += _coerce_int(row.get("unparsed_exec_count"), 0)
        detected_kb_call_count += _coerce_int(row.get("detected_kb_call_count"), 0)
        failed_kb_call_count += _coerce_int(row.get("failed_kb_call_count"), 0)
        execution_unknown_kb_call_count += _coerce_int(row.get("execution_unknown_kb_call_count"), 0)
        retrieval_call_count += _coerce_int(row.get("retrieval_call_count"), 0)
        paired_retrieval_call_count += _coerce_int(row.get("paired_retrieval_call_count"), 0)
        row_raw_unpaired_count = _coerce_int(row.get("unpaired_retrieval_call_count"), 0)
        raw_unpaired_retrieval_call_count += row_raw_unpaired_count
        unpaired_retrieval_call_count += _coerce_int(
            row.get("effective_unpaired_retrieval_call_count"),
            row_raw_unpaired_count,
        )
        closeout_data_source_counts.update(row.get("closeout_data_source_counts") or {})
        verdict = str(row.get("effect_verdict", ""))
        verdict_counts[verdict] += 1
        if row.get("kb_used"):
            all_kb_used += 1
        row_adopted_count = _coerce_int(row.get("adopted_count"), 0)
        row_effect_counts = row.get("adoption_effect_counts") if isinstance(row.get("adoption_effect_counts"), dict) else {}
        row_classified_adoption_count = sum(
            _coerce_int(row_effect_counts.get(effect), 0)
            for effect in ADOPTION_EFFECTS
        )
        row_legacy_used_count = _coerce_int(row.get("legacy_used_count"), 0)
        requested_adoption_count += _coerce_int(row.get("requested_adoption_count"), 0)
        if row.get("adoption_unconfirmed"):
            adoption_unconfirmed_sessions += 1
        if row_adopted_count > 0:
            all_adopted_sessions += 1
        all_adopted_count += row_adopted_count
        all_legacy_used_count += row_legacy_used_count
        all_classified_adoption_count += row_classified_adoption_count
        for effect in ADOPTION_EFFECTS:
            all_adoption_effect_counts[effect] += _coerce_int(row_effect_counts.get(effect), 0)
        is_subagent = bool(row.get("is_subagent"))
        if is_subagent:
            subagent_total += 1
            subagent_verdict_counts[verdict] += 1
            if row.get("kb_expected"):
                subagent_kb_expected += 1
            if row.get("kb_used"):
                subagent_kb_used += 1
            if verdict == "subagent_forbidden_but_used":
                subagent_forbidden_but_used += 1
            if verdict == "subagent_kb_task_not_used":
                subagent_kb_task_not_used += 1
            if verdict == "subagent_no_kb_expected":
                subagent_no_kb_expected += 1
            if verdict == "subagent_unexpected_kb_used":
                subagent_unexpected_kb_used += 1
            if row.get("subagent_kb_authorization") == "unknown":
                subagent_kb_authorization_unknown += 1
            if verdict == "subagent_kb_used_authorization_unknown":
                subagent_kb_used_authorization_unknown += 1
            if row.get("subagent_missing_no_kb_guard"):
                subagent_missing_no_kb_guard += 1
            subagent_legacy_used_count += row_legacy_used_count
            subagent_classified_adoption_count += row_classified_adoption_count
            for effect in ADOPTION_EFFECTS:
                subagent_adoption_effect_counts[effect] += _coerce_int(row_effect_counts.get(effect), 0)
            continue

        main_total += 1
        main_retrieval_call_count += _coerce_int(row.get("retrieval_call_count"), 0)
        main_paired_retrieval_call_count += _coerce_int(row.get("paired_retrieval_call_count"), 0)
        main_unpaired_retrieval_call_count += _coerce_int(
            row.get("effective_unpaired_retrieval_call_count"),
            row_raw_unpaired_count,
        )
        main_verdict_counts[verdict] += 1
        main_requested_adoption_count += _coerce_int(row.get("requested_adoption_count"), 0)
        if row.get("adoption_unconfirmed"):
            main_adoption_unconfirmed_sessions += 1
        if row.get("kb_expected"):
            kb_expected += 1
        if row.get("kb_used"):
            kb_used += 1
        if row.get("kb_expected") and row.get("kb_used"):
            kb_expected_and_used += 1
        if verdict == "used_hit_adopted":
            effective += 1
        if row_adopted_count > 0:
            adopted_sessions += 1
        adopted_count += row_adopted_count
        legacy_used_count += row_legacy_used_count
        classified_adoption_count += row_classified_adoption_count
        for effect in ADOPTION_EFFECTS:
            adoption_effect_counts[effect] += _coerce_int(row_effect_counts.get(effect), 0)
        if verdict == "needed_but_not_used":
            missed += 1
        if row.get("missing_closeout") or verdict == "used_without_closeout":
            missing_closeout += 1
        if verdict == "forbidden_but_used":
            forbidden_but_used += 1

    return {
        "analysis_ts": now_iso(),
        "event": "kb_codex_effectiveness_summary",
        "session_total": total,
        "main_session_total": main_total,
        "subagent_session_total": subagent_total,
        "main_kb_expected_sessions": kb_expected,
        "main_kb_used_sessions": kb_used,
        "main_kb_expected_and_used_sessions": kb_expected_and_used,
        "main_effective_sessions": effective,
        "main_adopted_sessions": adopted_sessions,
        "main_adopted_count": adopted_count,
        "main_confirmed_adoption_count": adopted_count,
        "main_classified_adoption_count": classified_adoption_count,
        "main_legacy_confirmed_adoption_count": legacy_used_count,
        "main_adoption_effects": {effect: adoption_effect_counts[effect] for effect in ADOPTION_EFFECTS},
        "main_legacy_used_count": legacy_used_count,
        "main_missed_sessions": missed,
        "main_missing_closeout_sessions": missing_closeout,
        "main_retrieval_call_count": main_retrieval_call_count,
        "main_paired_retrieval_call_count": main_paired_retrieval_call_count,
        "main_unpaired_retrieval_call_count": main_unpaired_retrieval_call_count,
        "kb_expected_sessions": kb_expected,
        "kb_used_sessions": kb_used,
        "kb_expected_and_used_sessions": kb_expected_and_used,
        "all_kb_used_sessions": all_kb_used,
        "effective_sessions": effective,
        "adopted_sessions": adopted_sessions,
        "adopted_count": adopted_count,
        "confirmed_adoption_count": adopted_count,
        "classified_adoption_count": classified_adoption_count,
        "legacy_confirmed_adoption_count": legacy_used_count,
        "adoption_effects": {effect: adoption_effect_counts[effect] for effect in ADOPTION_EFFECTS},
        "adoption_effect_counts": {effect: adoption_effect_counts[effect] for effect in ADOPTION_EFFECTS},
        "legacy_used_count": legacy_used_count,
        "all_adopted_sessions": all_adopted_sessions,
        "all_adopted_count": all_adopted_count,
        "all_confirmed_adoption_count": all_adopted_count,
        "all_classified_adoption_count": all_classified_adoption_count,
        "all_legacy_confirmed_adoption_count": all_legacy_used_count,
        "all_adoption_effects": {effect: all_adoption_effect_counts[effect] for effect in ADOPTION_EFFECTS},
        "all_legacy_used_count": all_legacy_used_count,
        "requested_adoption_count": main_requested_adoption_count,
        "adoption_unconfirmed_sessions": main_adoption_unconfirmed_sessions,
        "all_requested_adoption_count": requested_adoption_count,
        "all_adoption_unconfirmed_sessions": adoption_unconfirmed_sessions,
        "missed_sessions": missed,
        "missing_closeout_sessions": missing_closeout,
        "retrieval_call_count": retrieval_call_count,
        "paired_retrieval_call_count": paired_retrieval_call_count,
        "unpaired_retrieval_call_count": unpaired_retrieval_call_count,
        "raw_unpaired_retrieval_call_count": raw_unpaired_retrieval_call_count,
        "closeout_data_source_counts": dict(closeout_data_source_counts),
        "forbidden_but_used_sessions": forbidden_but_used,
        "kb_usage_rate": round(kb_expected_and_used / kb_expected, 4) if kb_expected else 0.0,
        "effectiveness_rate": round(effective / kb_used, 4) if kb_used else 0.0,
        "subagent_kb_expected_sessions": subagent_kb_expected,
        "subagent_kb_used_sessions": subagent_kb_used,
        "subagent_forbidden_but_used_sessions": subagent_forbidden_but_used,
        "subagent_kb_task_not_used_sessions": subagent_kb_task_not_used,
        "subagent_no_kb_expected_sessions": subagent_no_kb_expected,
        "subagent_unexpected_kb_used_sessions": subagent_unexpected_kb_used,
        "subagent_kb_authorization_unknown_sessions": subagent_kb_authorization_unknown,
        "subagent_kb_used_authorization_unknown_sessions": subagent_kb_used_authorization_unknown,
        "subagent_missing_no_kb_guard_sessions": subagent_missing_no_kb_guard,
        "subagent_adopted_sessions": all_adopted_sessions - adopted_sessions,
        "subagent_adopted_count": all_adopted_count - adopted_count,
        "subagent_confirmed_adoption_count": all_adopted_count - adopted_count,
        "subagent_classified_adoption_count": subagent_classified_adoption_count,
        "subagent_adoption_effects": {
            effect: subagent_adoption_effect_counts[effect] for effect in ADOPTION_EFFECTS
        },
        "subagent_legacy_used_count": subagent_legacy_used_count,
        "parser_version": PARSER_VERSION,
        "source_format_counts": dict(source_format_counts),
        "shell_call_count": shell_call_count,
        "parsed_shell_call_count": parsed_shell_call_count,
        "unparsed_exec_count": unparsed_exec_count,
        "parser_coverage": round(parsed_shell_call_count / shell_call_count, 4) if shell_call_count else None,
        "detected_kb_call_count": detected_kb_call_count,
        "failed_kb_call_count": failed_kb_call_count,
        "execution_unknown_kb_call_count": execution_unknown_kb_call_count,
        "execution_status_coverage": round(
            (detected_kb_call_count - execution_unknown_kb_call_count) / detected_kb_call_count, 4
        ) if detected_kb_call_count else None,
        "verdict_counts": dict(verdict_counts),
        "main_verdict_counts": dict(main_verdict_counts),
        "subagent_verdict_counts": dict(subagent_verdict_counts),
    }


def rebuild_logs(
    *,
    sessions_root: Path,
    base_dir: Path,
    force: bool = False,
    current_cutoff: str = CURRENT_WORKFLOW_CUTOFF,
    include_legacy: bool = False,
    dedupe_session_id: bool = True,
) -> dict[str, Any]:
    state = _load_state(state_path(base_dir))
    if state.get("parser_version") != PARSER_VERSION:
        state["parser_version"] = PARSER_VERSION
        state["sessions"] = {}
    session_state = state.setdefault("sessions", {})
    seen: set[str] = set()
    parsed = 0
    reused = 0

    for path in _iter_session_files(sessions_root):
        key = _session_key(path)
        seen.add(key)
        signature = _file_signature(path)
        existing = session_state.get(key, {})
        if not force and existing.get("signature") == signature and isinstance(existing.get("analysis"), dict):
            reused += 1
            continue
        analysis = _parse_session(path)
        session_state[key] = {
            "signature": signature,
            "analysis": analysis,
        }
        parsed += 1

    for key in list(session_state.keys()):
        if key not in seen:
            del session_state[key]

    all_rows = [value["analysis"] for value in session_state.values() if isinstance(value, dict) and isinstance(value.get("analysis"), dict)]
    all_rows.sort(key=lambda row: (str(row.get("session_ts", "")), str(row.get("session_path", ""))))

    if include_legacy:
        scoped_rows = list(all_rows)
        legacy_rows: list[dict[str, Any]] = []
    else:
        scoped_rows = [row for row in all_rows if not _is_legacy_row(row, current_cutoff)]
        legacy_rows = [row for row in all_rows if _is_legacy_row(row, current_cutoff)]

    dedupe_stats: dict[str, Any] = {
        "logical_session_total": len(scoped_rows),
        "duplicate_session_id_count": 0,
        "duplicate_row_extra": 0,
        "duplicate_session_ids_sample": [],
        "cross_agent_scope_session_id_count": 0,
        "cross_agent_scope_session_ids_sample": [],
    }
    rows = scoped_rows
    if dedupe_session_id:
        rows, dedupe_stats = _dedupe_rows_by_session_id(scoped_rows)
    rows = [attach_runtime_scope(dict(row)) for row in rows]
    legacy_rows = [attach_runtime_scope(dict(row)) for row in legacy_rows]
    parent_scout_stats = _reconcile_parent_scout_links(rows)

    summary = _summary_from_rows(rows)
    summary.update(
        {
            "summary_scope": "all" if include_legacy else "current",
            "current_cutoff": "" if include_legacy else current_cutoff,
            "include_legacy": include_legacy,
            "dedupe_session_id": dedupe_session_id,
            "raw_session_file_total": len(all_rows),
            "scoped_session_file_total": len(scoped_rows),
            "legacy_excluded_rows": len(legacy_rows),
            **parent_scout_stats,
            **dedupe_stats,
        }
    )
    summary = attach_runtime_scope(summary)

    _write_jsonl(log_path(base_dir), rows)
    if include_legacy:
        _write_jsonl(legacy_log_path(base_dir), [])
    else:
        _write_jsonl(legacy_log_path(base_dir), legacy_rows)
    summary_path(base_dir).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _save_state(state_path(base_dir), state)

    return attach_runtime_scope({
        "analysis_ts": now_iso(),
        "event": "kb_codex_effectiveness_rebuild",
        "sessions_root": str(sessions_root),
        "session_total": len(rows),
        "raw_session_file_total": len(all_rows),
        "scoped_session_file_total": len(scoped_rows),
        "legacy_excluded_rows": len(legacy_rows),
        "parsed_sessions": parsed,
        "reused_sessions": reused,
        "log_path": str(log_path(base_dir)),
        "legacy_log_path": str(legacy_log_path(base_dir)),
        "summary_path": str(summary_path(base_dir)),
        "summary": summary,
    })


def _quality_failures(
    summary: dict[str, Any],
    *,
    fail_on_subagent_unexpected_kb: bool = False,
    fail_on_subagent_forbidden_kb: bool = False,
    fail_on_subagent_missing_kb_guard: bool = False,
    fail_on_main_missed_kb: bool = False,
    fail_on_main_missing_closeout: bool = False,
) -> list[str]:
    failures: list[str] = []
    checks = [
        (
            fail_on_subagent_unexpected_kb,
            "subagent_unexpected_kb_used_sessions",
            "subagent used KB without explicit KB scout/retrieval/audit/maintenance authorization",
        ),
        (
            fail_on_subagent_forbidden_kb,
            "subagent_forbidden_but_used_sessions",
            "subagent used KB despite an explicit no-KB prompt",
        ),
        (
            fail_on_subagent_missing_kb_guard,
            "subagent_missing_no_kb_guard_sessions",
            "ordinary subagent prompt is missing the required no-personal-kb-script guard",
        ),
        (
            fail_on_main_missed_kb,
            "missed_sessions",
            "main session likely needed KB but did not run retrieval",
        ),
        (
            fail_on_main_missing_closeout,
            "missing_closeout_sessions",
            "main session has retrieval calls without a later matching closeout",
        ),
    ]
    for enabled, key, message in checks:
        count = _coerce_int(summary.get(key), 0)
        if enabled and count > 0:
            failures.append(f"{key}={count}: {message}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build searchable per-session Codex KB effectiveness logs from Codex JSONL conversations and KB command traces."
    )
    parser.add_argument("--sessions-root", default=str(_default_session_root()), help="Codex sessions root")
    parser.add_argument("--root", default="", help="Override personal-kb repos root")
    parser.add_argument("--force", action="store_true", help="Re-parse all session files even if unchanged")
    parser.add_argument(
        "--current-cutoff",
        default=CURRENT_WORKFLOW_CUTOFF,
        help=f"Default current workflow cutoff date; rows before it go to legacy log unless --include-legacy is used (default: {CURRENT_WORKFLOW_CUTOFF})",
    )
    parser.add_argument("--include-legacy", action="store_true", help="Include old pre-cutoff sessions in the active log and summary")
    parser.add_argument("--no-dedupe-session-id", action="store_true", help="Do not collapse duplicate session_id rows")
    parser.add_argument("--fail-on-subagent-unexpected-kb", action="store_true", help="Exit 2 if any subagent used KB without explicit KB authorization")
    parser.add_argument("--fail-on-subagent-forbidden-kb", action="store_true", help="Exit 2 if any subagent used KB despite an explicit no-KB prompt")
    parser.add_argument("--fail-on-subagent-missing-kb-guard", action="store_true", help="Exit 2 if any ordinary subagent prompt lacks the required no-KB guard")
    parser.add_argument("--fail-on-main-missed-kb", action="store_true", help="Exit 2 if any main session likely needed KB but did not run retrieval")
    parser.add_argument("--fail-on-main-missing-closeout", action="store_true", help="Exit 2 if any main session has retrieval calls without a later matching closeout")
    parser.add_argument(
        "--strict-quality",
        action="store_true",
        help="Exit 2 on any current main missed KB, missing closeout, forbidden subagent KB, or unexpected subagent KB usage",
    )
    parser.add_argument("--json", action="store_true", help="Print rebuild result JSON")
    args = parser.parse_args(argv)

    base_dir = Path(args.root).expanduser() if args.root else kb_base_dir()
    result = rebuild_logs(
        sessions_root=Path(args.sessions_root).expanduser(),
        base_dir=base_dir,
        force=args.force,
        current_cutoff=str(args.current_cutoff or ""),
        include_legacy=args.include_legacy,
        dedupe_session_id=not args.no_dedupe_session_id,
    )
    quality_failures = _quality_failures(
        result["summary"],
        fail_on_subagent_unexpected_kb=args.fail_on_subagent_unexpected_kb or args.strict_quality,
        fail_on_subagent_forbidden_kb=args.fail_on_subagent_forbidden_kb or args.strict_quality,
        fail_on_subagent_missing_kb_guard=args.fail_on_subagent_missing_kb_guard or args.strict_quality,
        fail_on_main_missed_kb=args.fail_on_main_missed_kb or args.strict_quality,
        fail_on_main_missing_closeout=args.fail_on_main_missing_closeout or args.strict_quality,
    )
    result["quality_failures"] = quality_failures
    if args.json:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
    else:
        summary = result["summary"]
        sys.stdout.write(
            json.dumps(
                {
                    "status": "ok",
                    "session_total": result["session_total"],
                    "raw_session_file_total": result["raw_session_file_total"],
                    "legacy_excluded_rows": result["legacy_excluded_rows"],
                    "parsed_sessions": result["parsed_sessions"],
                    "reused_sessions": result["reused_sessions"],
                    "log_path": result["log_path"],
                    "legacy_log_path": result["legacy_log_path"],
                    "summary_path": result["summary_path"],
                    "kb_usage_rate": summary["kb_usage_rate"],
                    "effectiveness_rate": summary["effectiveness_rate"],
                    "quality_failures": quality_failures,
                },
                ensure_ascii=False,
            )
        )
        sys.stdout.write("\n")
    return 2 if quality_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
