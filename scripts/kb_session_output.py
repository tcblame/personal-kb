from __future__ import annotations

import json
import re
import shlex
from typing import Any


RETRIEVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
RAG_TEXT_RETRIEVAL_ID_RE = re.compile(r'retrieval_id="([A-Za-z0-9._:-]+)"')
RAG_TEXT_HITS_RE = re.compile(r"\bhits=(\d+)(?=\s|$)")


def _command_key(command: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(command))
    except ValueError:
        return (command.strip(),)


def shell_completion_outputs(rows: list[dict], calls: list[dict], session_id: str) -> dict[tuple[str, int], str]:
    """Match actual shell completions to original commands, without timing guesses.

    Keep shell completion separate from model-visible tool delivery. A failed
    wrapper chain does not establish that a later retrieval actually ran.
    """
    import kb_command_contract as contract
    from kb_exec_parse import commands as js_commands

    completed: dict[str, dict] = {}
    for index, row in enumerate(rows):
        payload = row.get("payload") or {}
        item = payload.get("item") or {}
        if (row.get("type") != "event_msg" or payload.get("type") != "item_completed"
                or item.get("type") != "CommandExecution"
                or not isinstance(item.get("exit_code"), int)):
            continue
        if payload.get("thread_id") and payload["thread_id"] != session_id:
            continue
        command = item.get("command") or ""
        if isinstance(command, list):
            command = command[-1] if len(command) >= 3 and command[-2] in {"-c", "-lc"} else shlex.join(command)
        completed[str(item.get("id") or f"row-{index}")] = {
            "key": _command_key(str(command)), "turn_id": str(payload.get("turn_id") or ""),
            "output": json.dumps({"output": str(item.get("aggregated_output") or item.get("stdout") or ""),
                                  "exit_code": item["exit_code"]}),
        }
    available = list(completed.values())
    matched: dict[tuple[str, int], str] = {}
    for info in calls:
        payload = (info.get("row") or info).get("payload") or {}
        call_id = str(payload.get("call_id") or "")
        if payload.get("type") == "custom_tool_call":
            raw_commands = js_commands(str(payload.get("input") or ""))
        else:
            try:
                args = json.loads(payload.get("arguments") or "{}")
            except (ValueError, TypeError):
                args = {}
            raw_commands = [str(args.get("cmd") or "")]
        offset = 0
        for command in raw_commands:
            segments = contract.split_shell_commands(command)
            key = _command_key(command)
            turn_id = str(info.get("turn_id") or "")
            index = next((i for i, event in enumerate(available)
                          if event["key"] == key and (not turn_id or not event["turn_id"] or turn_id == event["turn_id"])), None)
            if index is not None:
                event = available.pop(index)
                for position in range(len(segments)):
                    matched[(call_id, offset + position)] = event["output"]
            offset += len(segments)
    return matched


def output_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := output_text(item)))
    if isinstance(value, dict):
        for key in ("text", "output", "content"):
            if key in value:
                return output_text(value.get(key))
    return ""


def json_dicts_from_output(output: str) -> list[dict[str, Any]]:
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


def nested_output_texts(output: str) -> list[str]:
    def nested_values(value: Any) -> list[str]:
        found: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"output", "text", "content"}:
                    text = output_text(item)
                    if text:
                        found.append(text)
                if isinstance(item, (dict, list)):
                    found.extend(nested_values(item))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    found.extend(nested_values(item))
        return found

    texts: list[str] = []
    queue = [output or ""]
    seen: set[str] = set()
    while queue:
        text = queue.pop(0)
        if not text or text in seen:
            continue
        seen.add(text)
        texts.append(text)
        for payload in json_dicts_from_output(text):
            for nested in nested_values(payload):
                if nested not in seen:
                    queue.append(nested)
    return texts


def _header_results(text: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("KB_RAG_CONTEXT ") and "\\n" in stripped:
            stripped = stripped.replace("\\n", "\n").splitlines()[0]
        if stripped.startswith("KB_RAG_CONTEXT "):
            stripped = stripped.replace(r'\"', '"')
        if not stripped.startswith("KB_RAG_CONTEXT "):
            continue
        query_match = re.search(r'query="(.*?)"\s+retrieval_id=', stripped)
        ids = RAG_TEXT_RETRIEVAL_ID_RE.findall(stripped)
        hits = RAG_TEXT_HITS_RE.findall(stripped)
        retrieval_id = ids[-1] if ids and RETRIEVAL_ID_RE.fullmatch(ids[-1]) else ""
        results.append({
            "retrieval_id": retrieval_id,
            "hit_count": int(hits[-1]) if hits else None,
            "success": True,
            "query": query_match[1] if query_match else "",
        })
    return results


def _json_results(text: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for payload in json_dicts_from_output(text):
        if payload.get("mode") != "read_only_rag_context":
            continue
        retrieval_id = str(payload.get("retrieval_id") or "").strip()
        if not RETRIEVAL_ID_RE.fullmatch(retrieval_id):
            retrieval_id = ""
        hit_count: int | None = None
        if "hit_count" in payload:
            try:
                hit_count = int(payload.get("hit_count"))
            except (TypeError, ValueError):
                hit_count = None
        elif isinstance(payload.get("items"), list):
            hit_count = len(payload["items"])
        results.append({"retrieval_id": retrieval_id, "hit_count": hit_count,
                        "query": str(payload.get("query") or ""),
                        "success": "error" not in payload and payload.get("status") not in {"error", "unavailable"}})
    return results


def retrieval_results(output: str) -> list[dict[str, Any]]:
    """Return unique ordered results, preferring authoritative headers over note JSON."""
    best: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for text in nested_output_texts(output):
        for candidates in (_header_results(text) or _json_results(text),):
            for candidate in candidates:
                identity = candidate.get("retrieval_id")
                if identity and identity in seen_ids:
                    continue
                if identity:
                    seen_ids.add(identity)
                best.append(candidate)
    return best
