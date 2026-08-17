#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import collections
import json
import re
import shlex
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from kb_lib import runtime_file
from kb_runtime import is_test_event
from kb_sensitive_scan import redact_value
import kb_command_contract as command_contract


KB_CALL_PATTERNS = command_contract.KB_SCRIPT_PATTERNS
KB_WRAPPER_COMMANDS = command_contract.KB_WRAPPER_COMMANDS

FORBID_KB_RE = re.compile(
    r"禁止执行\s*(KB|kb)|不要执行\s*(KB|kb)|禁止运行\s*kb_|不要运行\s*kb_|"
    r"do not run personal-kb|do not run kb_|不要调用\s*personal-kb",
    re.I,
)
EXPLICIT_KB_RE = re.compile(r"personal-kb|\bKB\b|kb_|\bRAG\b|记忆系统|知识库|历史记录", re.I)
MEMORY_NEEDED_RE = re.compile(
    r"跨会话|历史记录|已有记录|记录过|确认过|以前|曾经|之前|历史|上次|"
    r"沿用.*(上次|之前|历史|已确认)|继续.*(上次|之前|历史|已确认|旧决定)",
    re.I,
)
RUNTIME_AUDIT_RE = re.compile(
    r"(?:kbskill|personal-kb|\bKB\b|知识库).{0,40}(?:优化|效果|审计|运行|runtime|会话|session|质量|telemetry|对话记录)|"
    r"(?:优化|效果|审计|运行|runtime|会话|session|质量|telemetry|对话记录).{0,40}(?:kbskill|personal-kb|\bKB\b|知识库)",
    re.I,
)
EXIT_RE = re.compile(r"Process exited with code\s+(-?\d+)")
RAG_TEXT_RETRIEVAL_ID_RE = re.compile(r'retrieval_id="([A-Za-z0-9._:-]+)"')
RETRIEVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PYTHON_EXECUTABLE_RE = command_contract.PYTHON_EXECUTABLE_RE
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
CUSTOM_EXEC_CMD_RE = re.compile(
    r"(?:[\"']cmd[\"']|\bcmd)\s*:\s*(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)",
    re.S,
)
UUID_RE = re.compile(
    r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
PARSER_VERSION = "codex-shell-v9"
HUMAN_LABEL_VALUES = frozenset({"confirmed_missed_retrieval", "auditor_false_positive"})


def default_human_labels_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "docs"
        / "req"
        / "001-personal-kb-taxonomy"
        / "evals"
        / "runtime-session-human-labels.json"
    )


def _load_human_labels(path: Path) -> tuple[list[dict[str, Any]], str, list[str]]:
    if not path.is_file():
        return [], "not_configured", []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], "invalid", [str(exc)]
    raw_labels = payload.get("labels") if isinstance(payload, dict) else None
    if not isinstance(raw_labels, list):
        return [], "invalid", ["labels must be a list"]

    labels: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(raw_labels):
        if not isinstance(row, dict):
            errors.append(f"labels[{index}] must be an object")
            continue
        session_id = str(row.get("session_id") or "").strip()
        label = str(row.get("label") or "").strip()
        if not session_id:
            errors.append(f"labels[{index}].session_id is required")
            continue
        if session_id in seen:
            errors.append(f"labels[{index}].session_id is duplicated")
            continue
        if label not in HUMAN_LABEL_VALUES:
            errors.append(
                f"labels[{index}].label must be one of: {', '.join(sorted(HUMAN_LABEL_VALUES))}"
            )
            continue
        seen.add(session_id)
        labels.append({
            "session_id": session_id,
            "label": label,
            "reviewed_at": str(row.get("reviewed_at") or ""),
            "reason": str(row.get("reason") or ""),
        })
    return labels, "invalid" if errors else "loaded", errors


def _human_review_summary(
    sessions: list[dict[str, Any]],
    missed_main: list[dict[str, Any]],
    path: Path,
) -> dict[str, Any]:
    labels, status, errors = _load_human_labels(path)
    session_ids = {str(row.get("session_id") or "") for row in sessions}
    candidate_ids = {str(row.get("session_id") or "") for row in missed_main}
    in_window = [row for row in labels if row["session_id"] in session_ids]
    reviewed_candidates = [row for row in in_window if row["session_id"] in candidate_ids]
    confirmed = [row for row in in_window if row["label"] == "confirmed_missed_retrieval"]
    false_positives = [row for row in in_window if row["label"] == "auditor_false_positive"]
    reviewed_candidate_ids = {row["session_id"] for row in reviewed_candidates}
    parsed = {
        "path": str(path),
        "status": status,
        "errors": errors,
        "label_total": len(labels),
        "in_window_label_total": len(in_window),
        "auditor_candidate_total": len(missed_main),
        "reviewed_candidate_total": len(reviewed_candidates),
        "confirmed_missed_retrieval_total": len(confirmed),
        "auditor_false_positive_total": len(false_positives),
        "unreviewed_auditor_candidate_total": len(candidate_ids - reviewed_candidate_ids),
        "confirmed_not_flagged_total": sum(
            row["session_id"] not in candidate_ids for row in confirmed
        ),
        "samples": in_window[:10],
    }
    redacted, _findings = redact_value(parsed)
    return redacted


def _json_loads(value: str) -> dict[str, Any]:
    try:
        obj = json.loads(value or "{}")
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


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


def _execution_success(output: str) -> bool | None:
    match = EXIT_RE.search(output or "")
    if match:
        try:
            return int(match.group(1)) == 0
        except ValueError:
            return None
    if re.search(r"\bScript completed\b", output or "", re.I):
        return True
    if re.search(r"\b(?:Script failed|command failed)\b", output or "", re.I):
        return False
    return None


def _json_dicts_from_output(output: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    rows: list[dict[str, Any]] = []
    cursor = 0
    text = output or ""
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


def _nested_output_texts(output: str) -> list[str]:
    texts: list[str] = []
    queue = [output or ""]
    seen: set[str] = set()
    while queue:
        text = queue.pop(0)
        if not text or text in seen:
            continue
        seen.add(text)
        texts.append(text)
        for payload in _json_dicts_from_output(text):
            for key in ("output", "text", "content"):
                nested = _output_text(payload.get(key))
                if nested and nested not in seen:
                    queue.append(nested)
    return texts


def _retrieval_id_from_output(output: str) -> str:
    header_lines = [
        line.strip()
        for text in _nested_output_texts(output)
        for line in text.splitlines()
        if line.strip().startswith("KB_RAG_CONTEXT ")
    ]
    matches = [
        match for line in header_lines for match in RAG_TEXT_RETRIEVAL_ID_RE.findall(line)
    ]
    if matches and RETRIEVAL_ID_RE.fullmatch(matches[-1]):
        return matches[-1]
    for text in _nested_output_texts(output):
        for payload in _json_dicts_from_output(text):
            if payload.get("mode") != "read_only_rag_context":
                continue
            retrieval_id = str(payload.get("retrieval_id") or "").strip()
            if RETRIEVAL_ID_RE.fullmatch(retrieval_id):
                return retrieval_id
    return ""


def _decode_js_string(literal: str) -> str:
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
    return [
        command
        for match in CUSTOM_EXEC_CMD_RE.finditer(source)
        if (command := _decode_js_string(match.group(1)))
    ]


def _call_commands(payload: dict[str, Any]) -> tuple[list[str], str]:
    if payload.get("type") == "function_call":
        args = _json_loads(str(payload.get("arguments") or "{}"))
        command = str(args.get("cmd") or "")
        return ([command] if command else []), "function_call"
    if payload.get("type") == "custom_tool_call" and payload.get("name") == "exec":
        return _custom_exec_commands(str(payload.get("input") or "")), "custom_tool_call_exec"
    return [], ""


def _parse_cli_tokens(command: str) -> list[str]:
    return command_contract.parse_cli_tokens(command)


def _token_basename(token: str) -> str:
    return command_contract.token_basename(token)


def _direct_script(tokens: list[str], script_name: str) -> bool:
    return command_contract.direct_script_index(tokens, script_name) is not None


def _nested_script(command: str, script_name: str) -> bool:
    script = re.escape(script_name)
    patterns = [
        rf"subprocess\.(?:run|check_call|check_output|Popen)\s*\(\s*\[[^\]]{{0,1600}}?[\"'](?:python(?:\d+(?:\.\d+)?)?|py)[\"'][^\]]{{0,1600}}?{script}",
        rf"os\.system\s*\([^\)]{{0,1600}}?(?:python(?:\d+(?:\.\d+)?)?|py)\s+[^\)]{{0,1600}}?{script}",
    ]
    return any(re.search(pattern, command, re.I | re.S) for pattern in patterns)


def _wrapper_invocation(tokens: list[str]) -> tuple[str, int] | None:
    return command_contract.wrapper_invocation(tokens)


def _detected_scripts(command: str) -> list[str]:
    tokens = _parse_cli_tokens(command)
    detected = [
        key
        for key, script_name in KB_CALL_PATTERNS.items()
        if _direct_script(tokens, script_name) or (script_name in command and _nested_script(command, script_name))
    ]
    wrapped = _wrapper_invocation(tokens)
    if wrapped and wrapped[0] not in detected:
        detected.append(wrapped[0])
    return detected


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        return [value]
    return []


def _as_int(value: Any) -> int:
    if isinstance(value, list):
        total = 0
        for item in value:
            total += _as_int(item)
        return total
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _filename_session_id(path: Path) -> str:
    matches = UUID_RE.findall(path.stem)
    return matches[-1].lower() if matches else ""


def _select_session_meta(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    metas = [
        payload
        for row in rows
        if row.get("type") == "session_meta"
        and isinstance((payload := row.get("payload")), dict)
    ]
    if not metas:
        return {}

    filename_id = _filename_session_id(path)
    if filename_id:
        for meta in metas:
            if str(meta.get("id") or "").lower() == filename_id:
                return meta

    # Old/non-rollout fixture names have no UUID. The first metadata row is the
    # rollout's own metadata; forked histories may append a parent row later.
    return metas[0]


def _is_subagent_meta(meta: dict[str, Any]) -> bool:
    thread_source = str(meta.get("thread_source") or "").lower()
    source = meta.get("source")
    source_text = json.dumps(source, ensure_ascii=False) if isinstance(source, dict) else str(source or "")
    return (
        thread_source == "subagent"
        or bool(meta.get("parent_thread_id"))
        or (isinstance(source, dict) and "subagent" in source)
        or "subagent" in source_text.lower()
    )


def _subagent_parent_thread_id(meta: dict[str, Any]) -> str:
    direct = str(meta.get("parent_thread_id") or "").strip()
    if direct:
        return direct
    source = meta.get("source")
    if not isinstance(source, dict):
        return ""
    subagent = source.get("subagent")
    if not isinstance(subagent, dict):
        return ""
    spawn = subagent.get("thread_spawn")
    return str(spawn.get("parent_thread_id") or "").strip() if isinstance(spawn, dict) else ""


def _source_bucket(meta: dict[str, Any]) -> str:
    if _is_subagent_meta(meta):
        return "subagent"
    return str(meta.get("thread_source") or "<empty>")


def _turn_id(payload: dict[str, Any], fallback: str = "") -> str:
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(metadata, dict) and metadata.get("turn_id"):
        return str(metadata["turn_id"])
    return str(payload.get("turn_id") or fallback)


def _script_index(tokens: list[str], script_name: str) -> int | None:
    return command_contract.script_index(tokens, script_name)


def _is_help_invocation(tokens: list[str], script_name: str) -> bool:
    return command_contract.is_help_invocation(tokens, script_name)


def _rag_query(tokens: list[str], script_name: str) -> str:
    index = _script_index(tokens, script_name)
    if index is not None and index + 1 < len(tokens):
        candidate = tokens[index + 1]
        return candidate if candidate and not candidate.startswith("-") else ""
    wrapped = _wrapper_invocation(tokens)
    expected_key = next((key for key, value in KB_CALL_PATTERNS.items() if value == script_name), "")
    if not wrapped or wrapped[0] != expected_key or wrapped[1] + 1 >= len(tokens):
        return ""
    candidate = tokens[wrapped[1] + 1]
    return candidate if candidate and not candidate.startswith("-") else ""


def _flag_values(tokens: list[str], flag: str) -> list[str]:
    return command_contract.repeated_flag_values(tokens, flag)


def _closeout_details(tokens: list[str]) -> tuple[list[str], int, list[str]]:
    inline_payloads = _flag_values(tokens, "--json")
    payload = _json_loads(inline_payloads[-1]) if inline_payloads else {}
    queries = [
        *[str(value) for value in _as_list(payload.get("queries")) if str(value)],
        *_flag_values(tokens, "--query"),
    ]
    linked_retrieval_ids = list(dict.fromkeys([
        *[str(value) for value in _as_list(payload.get("linked_retrieval_ids")) if str(value)],
        *_flag_values(tokens, "--linked-retrieval-id"),
    ]))
    rag_call_values = _flag_values(tokens, "--rag-calls")
    rag_calls = (
        _as_int(payload.get("rag_calls"))
        if "rag_calls" in payload
        else _as_int(rag_call_values[-1]) if rag_call_values else 0
    )
    lifecycle_flags = {
        "--query", "--hit-count", "--rag-calls", "--used", "--used-locate",
        "--used-decide", "--used-fix", "--used-write", "--written", "--updated",
        "--reason", "--json", "--json-file", "--closeout-id", "--linked-retrieval-id",
    }
    meaningful = bool(queries) or any(
        token in lifecycle_flags or any(token.startswith(flag + "=") for flag in lifecycle_flags)
        for token in tokens
    )
    if not meaningful:
        return queries, 0, linked_retrieval_ids
    return queries, rag_calls if rag_calls > 0 else max(len(queries), 1), linked_retrieval_ids


def _normalized_query(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _match_rag_closeouts(
    rag_events: list[dict[str, Any]],
    closeout_events: list[dict[str, Any]],
) -> tuple[int, list[dict[str, Any]]]:
    remaining = [max(0, _as_int(event.get("capacity"))) for event in closeout_events]
    linked_ids_by_closeout = [
        set(str(value) for value in _as_list(event.get("linked_retrieval_ids")) if str(value))
        for event in closeout_events
    ]
    matched_link_ids_by_closeout: list[set[str]] = [set() for _ in closeout_events]
    matched_indexes: set[int] = set()

    exact_candidates: list[tuple[int, int, int, int]] = []
    for rag_index, rag in enumerate(rag_events):
        retrieval_id = str(rag.get("retrieval_id") or "").strip()
        if not RETRIEVAL_ID_RE.fullmatch(retrieval_id):
            continue
        rag_turn = str(rag.get("turn_id") or "")
        rag_sequence = _as_int(rag.get("sequence"))
        for closeout_index, closeout in enumerate(closeout_events):
            closeout_sequence = _as_int(closeout.get("sequence"))
            if (
                remaining[closeout_index] <= 0
                or closeout_sequence <= rag_sequence
                or retrieval_id not in linked_ids_by_closeout[closeout_index]
            ):
                continue
            closeout_turn = str(closeout.get("turn_id") or "")
            same_turn_rank = 0 if rag_turn and rag_turn == closeout_turn else 1
            exact_candidates.append(
                (same_turn_rank, closeout_sequence - rag_sequence, rag_index, closeout_index)
            )

    for _same_turn_rank, _distance, rag_index, closeout_index in sorted(exact_candidates):
        if rag_index in matched_indexes or remaining[closeout_index] <= 0:
            continue
        retrieval_id = str(rag_events[rag_index].get("retrieval_id") or "").strip()
        matched_indexes.add(rag_index)
        remaining[closeout_index] -= 1
        matched_link_ids_by_closeout[closeout_index].add(retrieval_id)

    fallback_remaining = [
        max(0, remaining[index] - len(linked_ids - matched_link_ids_by_closeout[index]))
        for index, linked_ids in enumerate(linked_ids_by_closeout)
    ]

    for rag_index, rag in enumerate(rag_events):
        if rag_index in matched_indexes:
            continue
        rag_turn = str(rag.get("turn_id") or "")
        rag_query = _normalized_query(str(rag.get("query") or ""))
        retrieval_id = str(rag.get("retrieval_id") or "").strip()
        best_index: int | None = None
        best_score = 0
        for index, closeout in enumerate(closeout_events):
            if fallback_remaining[index] <= 0:
                continue
            if _as_int(closeout.get("sequence")) <= _as_int(rag.get("sequence")):
                continue
            if (
                retrieval_id
                and linked_ids_by_closeout[index]
                and retrieval_id not in linked_ids_by_closeout[index]
            ):
                continue
            closeout_turn = str(closeout.get("turn_id") or "")
            closeout_queries = {
                _normalized_query(str(query))
                for query in _as_list(closeout.get("queries"))
                if _normalized_query(str(query))
            }
            same_turn = bool(rag_turn and closeout_turn and rag_turn == closeout_turn)
            exact_query = bool(rag_query and rag_query in closeout_queries)
            score = (
                4 if same_turn and exact_query
                else 3 if exact_query
                else 2 if same_turn and (not rag_query or not closeout_queries)
                else 0
            )
            if score > best_score:
                best_index = index
                best_score = score
        if best_index is None:
            continue
        fallback_remaining[best_index] -= 1
        matched_indexes.add(rag_index)

    unmatched = [rag for index, rag in enumerate(rag_events) if index not in matched_indexes]
    return len(matched_indexes), unmatched


def _session_paths(root: Path, dates: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in dates:
        d = datetime.strptime(raw, "%Y-%m-%d").date()
        day_dir = root / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}"
        out.extend(sorted(day_dir.glob("*.jsonl")))
    return out


def _default_dates(days: int) -> list[str]:
    today = date.today()
    return [f"{today - timedelta(days=i):%Y-%m-%d}" for i in range(max(1, days))]


def _parse_session(path: Path) -> dict[str, Any]:
    user_msgs: list[str] = []
    rows: list[dict[str, Any]] = []
    call_rows: list[dict[str, Any]] = []
    output_by_call_id: dict[str, str] = {}
    counters: collections.Counter[str] = collections.Counter()
    source_format_counts: collections.Counter[str] = collections.Counter()
    unparsed_exec_count = 0
    failed_kb_call_count = 0
    execution_unknown_kb_call_count = 0

    for line in path.read_text(errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)

    meta = _select_session_meta(path, rows)
    current_turn_id = ""
    for obj in rows:
        payload = obj.get("payload") or {}
        if obj.get("type") == "turn_context" and isinstance(payload, dict):
            current_turn_id = _turn_id(payload, current_turn_id)
        if obj.get("type") == "event_msg" and payload.get("type") == "task_started":
            current_turn_id = _turn_id(payload, current_turn_id)
        if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
            user_msgs.append(str(payload.get("message") or payload.get("text") or ""))
        if (
            obj.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            user_text = _user_response_text(payload.get("content"))
            if user_text.strip():
                user_msgs.append(user_text)
        if obj.get("type") == "response_item" and payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or "")
            if call_id:
                output_by_call_id[call_id] = _output_text(payload.get("output"))
        if obj.get("type") == "response_item" and payload.get("type") == "function_call":
            args = _json_loads(str(payload.get("arguments") or "{}"))
            if payload.get("name") in {"exec", "exec_command"} or "cmd" in args:
                call_rows.append({
                    "row": obj,
                    "turn_id": _turn_id(payload, current_turn_id),
                    "sequence": len(call_rows),
                })
                source_format_counts["function_call"] += 1
                if not str(args.get("cmd") or ""):
                    unparsed_exec_count += 1
        if obj.get("type") == "response_item" and payload.get("type") == "custom_tool_call" and payload.get("name") == "exec":
            call_rows.append({
                "row": obj,
                "turn_id": _turn_id(payload, current_turn_id),
                "sequence": len(call_rows),
            })
            source_format_counts["custom_tool_call_exec"] += 1
            if not _custom_exec_commands(str(payload.get("input") or "")):
                unparsed_exec_count += 1

    detected_kb_call_count = 0
    rag_events: list[dict[str, Any]] = []
    closeout_events: list[dict[str, Any]] = []
    for call_info in call_rows:
        call = call_info["row"]
        payload = call.get("payload") or {}
        commands, _ = _call_commands(payload)
        output = output_by_call_id.get(str(payload.get("call_id") or ""), "")
        success = _execution_success(output)
        for cmd in commands:
            tokens = _parse_cli_tokens(cmd)
            if "personal-kb/SKILL.md" in cmd and success is True:
                counters["skill_read"] += 1
            scripts = [
                key for key in _detected_scripts(cmd)
                if not _is_help_invocation(tokens, KB_CALL_PATTERNS[key])
            ]
            detected_kb_call_count += len(scripts)
            if scripts and success is False:
                failed_kb_call_count += len(scripts)
            if scripts and success is None:
                execution_unknown_kb_call_count += len(scripts)
            if success is not True:
                continue
            for key in scripts:
                counters[key] += 1
                if key in {"kb_rag_context", "kb_search"}:
                    rag_events.append({
                        "turn_id": call_info["turn_id"],
                        "sequence": call_info["sequence"],
                        "query": _rag_query(tokens, KB_CALL_PATTERNS[key]),
                        "script": key,
                        "retrieval_id": (
                            _retrieval_id_from_output(output)
                            or (_flag_values(tokens, "--retrieval-id")[-1] if _flag_values(tokens, "--retrieval-id") else "")
                        ) if key == "kb_rag_context" else "",
                    })
                elif key == "kb_closeout":
                    queries, capacity, linked_retrieval_ids = _closeout_details(tokens)
                    if capacity > 0:
                        closeout_events.append({
                            "turn_id": call_info["turn_id"],
                            "sequence": call_info["sequence"],
                            "queries": queries,
                            "capacity": capacity,
                            "linked_retrieval_ids": linked_retrieval_ids,
                        })
            if "kb_update" in scripts and re.search(r"\buse\b", cmd):
                counters["kb_update_use"] += 1

    matched_rag_calls, unmatched_rag_events = _match_rag_closeouts(rag_events, closeout_events)
    user_text = " ".join(user_msgs)
    source = _source_bucket(meta)
    is_subagent = _is_subagent_meta(meta)
    is_main = bool(meta) and not is_subagent
    forbid_kb = bool(FORBID_KB_RE.search(user_text))
    runtime_audit = bool(RUNTIME_AUDIT_RE.search(user_text))
    explicit_kb = bool(EXPLICIT_KB_RE.search(user_text)) and not runtime_audit
    memory_needed = bool(MEMORY_NEEDED_RE.search(user_text))
    rag_or_search = counters["kb_rag_context"] + counters["kb_search"]
    write_or_update = counters["kb_add"] + counters["kb_update"]

    parsed = {
        "file": path.name,
        "path": str(path),
        "session_id": str(meta.get("id") or _filename_session_id(path) or path.stem),
        "source": source,
        "is_subagent": is_subagent,
        "is_main": is_main,
        "subagent_parent_thread_id": _subagent_parent_thread_id(meta),
        "user_excerpt": user_text[:180],
        "forbid_kb": forbid_kb,
        "runtime_audit": runtime_audit,
        "explicit_kb": explicit_kb,
        "memory_needed": memory_needed,
        "rag_or_search": rag_or_search,
        "rag_closeout_match_count": matched_rag_calls,
        "rag_calls_without_closeout": len(unmatched_rag_events),
        "rag_queries_without_closeout": [
            str(event.get("query") or "") for event in unmatched_rag_events[:10]
        ],
        "retrieval_events": [event for event in rag_events if event.get("retrieval_id")][:20],
        "retrieval_id_missing_count": sum(
            event.get("script") == "kb_rag_context" and not event.get("retrieval_id")
            for event in rag_events
        ),
        "closeout_linked_retrieval_ids": list(dict.fromkeys(
            str(retrieval_id)
            for event in closeout_events
            for retrieval_id in event.get("linked_retrieval_ids", [])
            if str(retrieval_id)
        ))[:20],
        "write_or_update": write_or_update,
        "counters": dict(counters),
        "call_count": len(call_rows),
        "parser_version": PARSER_VERSION,
        "source_format_counts": dict(source_format_counts),
        "parsed_shell_call_count": len(call_rows) - unparsed_exec_count,
        "unparsed_exec_count": unparsed_exec_count,
        "parser_coverage": round((len(call_rows) - unparsed_exec_count) / len(call_rows), 4) if call_rows else None,
        "detected_kb_call_count": detected_kb_call_count,
        "failed_kb_call_count": failed_kb_call_count,
        "execution_unknown_kb_call_count": execution_unknown_kb_call_count,
        "execution_status_coverage": round(
            (detected_kb_call_count - execution_unknown_kb_call_count) / detected_kb_call_count, 4
        ) if detected_kb_call_count else None,
    }
    redacted, _findings = redact_value(parsed)
    return redacted


def _read_closeouts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _closeout_date(row: dict[str, Any]) -> str:
    raw = str(row.get("ts") or "").strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    return match.group(1) if match else ""


def _adoption_effect_ids(row: dict[str, Any], effects: tuple[str, ...]) -> list[str]:
    payload = row.get("adoption_effects")
    if not isinstance(payload, dict):
        return []
    return [
        str(entry_id)
        for effect in effects
        for entry_id in _as_list(payload.get(effect))
        if str(entry_id)
    ]


def _closeout_has_action(row: dict[str, Any]) -> bool:
    return bool(
        _as_list(row.get("used_entry_ids"))
        or _as_list(row.get("written_entry_ids"))
        or _as_list(row.get("updated_entry_ids"))
        or _as_list(row.get("session_brief_used_entry_ids"))
        or _adoption_effect_ids(row, ("locate", "decide", "fix", "write"))
    )


def _closeout_has_integrity_gap(row: dict[str, Any]) -> bool:
    linked_missing = (
        _as_int(row.get("rag_calls")) > 0
        and not bool(_as_list(row.get("linked_retrieval_ids")))
    )
    brief_missing = "session_brief_hit" not in row or "session_brief_help" not in row
    return linked_missing or brief_missing


def _required_heat_ids(row: dict[str, Any]) -> set[str]:
    session_brief_ids = {
        str(entry_id) for entry_id in _as_list(row.get("session_brief_used_entry_ids"))
    }
    return set(_adoption_effect_ids(row, ("decide", "fix", "write"))) - session_brief_ids


def _reconcile_parent_scout_sessions(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    main_by_identity: dict[str, set[int]] = collections.defaultdict(set)
    parents_by_linked_id: dict[str, list[int]] = collections.defaultdict(list)
    retrievals_by_id: dict[str, list[tuple[int, dict[str, Any]]]] = collections.defaultdict(list)
    invalid_event_ids_by_child: dict[int, list[str]] = collections.defaultdict(list)

    for index, session in enumerate(sessions):
        if session.get("is_main"):
            for identity in (session.get("session_id"),):
                value = str(identity or "").strip()
                if value:
                    main_by_identity[value].add(index)
            for retrieval_id in session.get("closeout_linked_retrieval_ids") or []:
                value = str(retrieval_id or "").strip()
                if value:
                    parents_by_linked_id[value].append(index)
        for event in session.get("retrieval_events") or []:
            if not isinstance(event, dict):
                continue
            retrieval_id = str(event.get("retrieval_id") or "").strip()
            if RETRIEVAL_ID_RE.fullmatch(retrieval_id):
                retrievals_by_id[retrieval_id].append((index, event))
            elif retrieval_id and session.get("is_subagent"):
                invalid_event_ids_by_child[index].append(retrieval_id)

    duplicate_ids = {retrieval_id for retrieval_id, events in retrievals_by_id.items() if len(events) != 1}
    linked_by_child: dict[int, list[str]] = collections.defaultdict(list)
    orphan_by_child: dict[int, list[str]] = collections.defaultdict(list)
    invalid_by_child: dict[int, list[str]] = collections.defaultdict(list)
    for child_index, retrieval_ids in invalid_event_ids_by_child.items():
        invalid_by_child[child_index].extend(retrieval_ids)
    delegated_by_parent: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)

    for retrieval_id, events in retrievals_by_id.items():
        for child_index, event in events:
            child = sessions[child_index]
            if not child.get("is_subagent"):
                continue
            if child.get("forbid_kb") or retrieval_id in duplicate_ids:
                invalid_by_child[child_index].append(retrieval_id)
                continue
            parent_id = str(child.get("subagent_parent_thread_id") or "").strip()
            if not parent_id:
                orphan_by_child[child_index].append(retrieval_id)
                continue
            candidates = set(parents_by_linked_id.get(retrieval_id, []))
            candidates &= main_by_identity.get(parent_id, set())
            if len(candidates) != 1:
                orphan_by_child[child_index].append(retrieval_id)
                continue
            parent_index = next(iter(candidates))
            linked_by_child[child_index].append(retrieval_id)
            delegated_by_parent[parent_index].append(
                {
                    "retrieval_id": retrieval_id,
                    "scout_session_id": str(child.get("session_id") or ""),
                    "query": str(event.get("query") or ""),
                }
            )

    linked_count = 0
    orphan_count = 0
    invalid_count = 0
    missing_id_count = 0
    parent_session_count = 0
    for index, session in enumerate(sessions):
        if session.get("is_subagent"):
            linked = list(dict.fromkeys(linked_by_child.get(index, [])))
            orphan = list(dict.fromkeys(orphan_by_child.get(index, [])))
            invalid = list(dict.fromkeys(invalid_by_child.get(index, [])))
            session["parent_closeout_linked_retrieval_ids"] = linked
            session["orphan_scout_retrieval_ids"] = orphan
            session["invalid_scout_retrieval_ids"] = invalid
            session["effective_rag_calls_without_closeout"] = max(
                0,
                _as_int(session.get("rag_calls_without_closeout")) - len(linked),
            )
            linked_count += len(linked)
            orphan_count += len(orphan)
            invalid_count += len(invalid)
            missing_id_count += _as_int(session.get("retrieval_id_missing_count"))
            continue

        delegated = delegated_by_parent.get(index, [])
        session["delegated_retrieval_events"] = delegated[:20]
        session["delegated_retrieval_call_count"] = len(delegated)
        session["effective_rag_or_search"] = _as_int(session.get("rag_or_search")) + len(delegated)
        if delegated:
            parent_session_count += 1

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


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    dates = args.date or _default_dates(args.last_days)
    paths = _session_paths(Path(args.sessions_root).expanduser(), dates)
    sessions = [_parse_session(path) for path in paths]
    parent_scout_stats = _reconcile_parent_scout_sessions(sessions)
    date_set = set(dates)
    dated_closeouts = [
        row
        for row in _read_closeouts(Path(args.closeout).expanduser())
        if _closeout_date(row) in date_set
    ]
    include_test = bool(getattr(args, "include_test", False))
    excluded_test_rows = sum(is_test_event(row) for row in dated_closeouts) if not include_test else 0
    closeouts = [
        row for row in dated_closeouts if include_test or not is_test_event(row)
    ]

    def count(pred) -> int:
        return sum(1 for item in sessions if pred(item))

    kb_activity = [
        s for s in sessions
        if s["rag_or_search"] or s["write_or_update"] or s["counters"].get("kb_closeout")
    ]
    violations = [
        s for s in sessions
        if s["forbid_kb"] and (
            s["rag_or_search"] or s["write_or_update"] or s["counters"].get("kb_closeout")
        )
    ]
    missed_main = [
        s for s in sessions
        if s["is_main"]
        and not s["forbid_kb"]
        and not s["runtime_audit"]
        and (s["explicit_kb"] or s["memory_needed"])
        and not s.get("effective_rag_or_search", s["rag_or_search"])
    ]
    rag_no_closeout = [
        s for s in sessions
        if s.get("effective_rag_calls_without_closeout", s["rag_calls_without_closeout"]) > 0
        and not s["runtime_audit"]
        and not s["forbid_kb"]
    ]
    main_expected_rag = [
        s for s in sessions
        if s["is_main"]
        and not s["forbid_kb"]
        and not s["runtime_audit"]
        and (s["explicit_kb"] or s["memory_needed"])
    ]
    main_expected_and_used_rag = [
        s for s in main_expected_rag
        if s.get("effective_rag_or_search", s["rag_or_search"]) > 0
    ]
    main_used_rag = [
        s for s in sessions
        if s["is_main"]
        and not s["forbid_kb"]
        and not s["runtime_audit"]
        and s.get("effective_rag_or_search", s["rag_or_search"]) > 0
    ]
    main_used_without_closeout = [
        s for s in main_used_rag
        if _as_int(s["counters"].get("kb_closeout")) <= 0
    ]
    main_with_closeout = [
        s for s in sessions
        if s["is_main"]
        and _as_int(s["counters"].get("kb_closeout")) > 0
    ]
    human_labels_value = str(getattr(args, "human_labels", "") or "").strip()
    human_labels_path = (
        Path(human_labels_value).expanduser()
        if human_labels_value
        else default_human_labels_path()
    )
    human_review = _human_review_summary(sessions, missed_main, human_labels_path)

    closeout_issues = {
        "rows": len(closeouts),
        "rag_calls_zero_with_query_or_hit": sum(
            _as_int(r.get("rag_calls")) == 0
            and (bool(_as_list(r.get("queries"))) or _as_int(r.get("hit_count")) > 0)
            for r in closeouts
        ),
        "hit_with_no_action": sum(
            _as_int(r.get("hit_count")) > 0
            and not _closeout_has_action(r)
            and not str(r.get("skipped_reason") or "").strip()
            for r in closeouts
        ),
        "written_latest": sum("latest" in _as_list(r.get("written_entry_ids")) for r in closeouts),
        "used_without_heat": sum(
            bool(
                _required_heat_ids(r)
                - set(str(entry_id) for entry_id in _as_list(r.get("heated_entry_ids")))
            )
            for r in closeouts
        ),
        "linked_retrieval_id_missing": sum(
            _as_int(r.get("rag_calls")) > 0
            and not bool(_as_list(r.get("linked_retrieval_ids")))
            for r in closeouts
        ),
        "session_brief_telemetry_missing": sum(
            "session_brief_hit" not in r or "session_brief_help" not in r
            for r in closeouts
        ),
        "closeout_integrity_missing": sum(_closeout_has_integrity_gap(r) for r in closeouts),
    }

    call_totals: collections.Counter[str] = collections.Counter()
    by_source: collections.Counter[str] = collections.Counter()
    source_format_counts: collections.Counter[str] = collections.Counter()
    shell_call_count = 0
    parsed_shell_call_count = 0
    unparsed_exec_count = 0
    detected_kb_call_count = 0
    failed_kb_call_count = 0
    execution_unknown_kb_call_count = 0
    for session in sessions:
        by_source[session["source"]] += 1
        call_totals.update(session["counters"])
        source_format_counts.update(session.get("source_format_counts") or {})
        shell_call_count += _as_int(session.get("call_count"))
        parsed_shell_call_count += _as_int(session.get("parsed_shell_call_count"))
        unparsed_exec_count += _as_int(session.get("unparsed_exec_count"))
        detected_kb_call_count += _as_int(session.get("detected_kb_call_count"))
        failed_kb_call_count += _as_int(session.get("failed_kb_call_count"))
        execution_unknown_kb_call_count += _as_int(session.get("execution_unknown_kb_call_count"))

    return {
        "dates": dates,
        "session_files": len(paths),
        "sessions_by_source": dict(by_source),
        "kb_activity_sessions": len(kb_activity),
        "main_sessions": count(lambda s: s["is_main"]),
        "subagent_sessions": count(lambda s: s["is_subagent"]),
        "main_missed_rag_sessions": len(missed_main),
        "main_missed_rag_semantics": "auditor_candidate_not_human_ground_truth",
        "auditor_candidate_missed_rag_sessions": len(missed_main),
        "human_confirmed_missed_rag_sessions": human_review["confirmed_missed_retrieval_total"],
        "human_confirmed_false_positive_sessions": human_review["auditor_false_positive_total"],
        "human_review": human_review,
        "forbidden_kb_violation_sessions": len(violations),
        "main_expected_rag_sessions": len(main_expected_rag),
        "main_expected_and_used_rag_sessions": len(main_expected_and_used_rag),
        "main_used_sessions": len(main_used_rag),
        "main_used_without_closeout_sessions": len(main_used_without_closeout),
        "main_with_closeout_sessions": len(main_with_closeout),
        "rag_without_closeout_sessions": len(rag_no_closeout),
        **parent_scout_stats,
        "call_totals": dict(call_totals),
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
        "closeout_issues": closeout_issues,
        "excluded_test_rows": excluded_test_rows,
        "include_test": include_test,
        "examples": {
            "forbidden_kb_violations": violations[: args.examples],
            "main_missed_rag": missed_main[: args.examples],
            "main_used_without_closeout": main_used_without_closeout[: args.examples],
            "rag_without_closeout": rag_no_closeout[: args.examples],
        },
    }


def _print_text(report: dict[str, Any]) -> None:
    print(f"KB_CODEX_SESSION_AUDIT dates={','.join(report['dates'])} session_files={report['session_files']}")
    print(f"- sessions_by_source: {report['sessions_by_source']}")
    print(f"- kb_activity_sessions: {report['kb_activity_sessions']}")
    print(f"- main_sessions: {report['main_sessions']}")
    print(f"- main_missed_rag_sessions: {report['main_missed_rag_sessions']}")
    print(
        "- missed_rag_review: "
        f"auditor_candidates={report['auditor_candidate_missed_rag_sessions']} "
        f"human_confirmed={report['human_confirmed_missed_rag_sessions']} "
        f"human_false_positives={report['human_confirmed_false_positive_sessions']} "
        f"unreviewed={report['human_review']['unreviewed_auditor_candidate_total']}"
    )
    print(
        f"- main_expected_rag_sessions: {report['main_expected_rag_sessions']} "
        f"(used {report['main_expected_and_used_rag_sessions']})"
    )
    print(f"- main_used_without_closeout_sessions: {report['main_used_without_closeout_sessions']}")
    print(f"- main_with_closeout_sessions: {report['main_with_closeout_sessions']}")
    print(f"- forbidden_kb_violation_sessions: {report['forbidden_kb_violation_sessions']}")
    print(f"- rag_without_closeout_sessions: {report['rag_without_closeout_sessions']}")
    print(f"- call_totals: {report['call_totals']}")
    print(
        f"- parser: version={report['parser_version']} formats={report['source_format_counts']} "
        f"coverage={report['parser_coverage']} unparsed_exec={report['unparsed_exec_count']} "
        f"execution_coverage={report['execution_status_coverage']}"
    )
    print(f"- closeout_issues: {report['closeout_issues']}")
    print(f"- excluded_test_rows: {report['excluded_test_rows']}")
    for group, rows in report["examples"].items():
        print(f"\n{group}:")
        if not rows:
            print("  none")
            continue
        for row in rows:
            counters = row.get("counters", {})
            print(f"  - {row['source']} {row['file']} counters={counters}")
            if row.get("user_excerpt"):
                print(f"    user: {row['user_excerpt']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only audit for Codex sessions and personal-kb runtime behavior.")
    parser.add_argument("--sessions-root", default="~/.codex/sessions", help="Codex sessions root")
    parser.add_argument("--date", action="append", default=[], help="Date to audit, YYYY-MM-DD; repeatable")
    parser.add_argument("--last-days", type=int, default=2, help="Audit today and previous N-1 days when --date is omitted")
    parser.add_argument("--closeout", default=str(runtime_file("closeout.jsonl")))
    parser.add_argument("--examples", type=int, default=5, help="Examples per issue type")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    parser.add_argument("--include-test", action="store_true", help="Include closeout rows explicitly marked as test runtime")
    parser.add_argument(
        "--human-labels",
        default=str(default_human_labels_path()),
        help="Optional human-reviewed session labels JSON",
    )
    args = parser.parse_args(argv)

    report = build_report(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
