#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from kb_session_identity import default_sessions_root, open_rollout, rollout_paths


EXIT_RE = re.compile(r"Exit code:\s*(-?\d+)")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open_rollout(path) as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        return []
    return rows


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return parsed if isinstance(parsed, dict) else {"raw": value}


def _call_name(call: dict[str, Any]) -> str:
    name = call.get("name") or call.get("tool_name") or call.get("function_name") or ""
    function = call.get("function")
    if not name and isinstance(function, dict):
        name = function.get("name", "")
    return str(name)


def _call_id(call: dict[str, Any]) -> str:
    return str(call.get("call_id") or call.get("id") or "")


def _exit_code(output: str) -> int | None:
    match = EXIT_RE.search(output)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _count_result(output: str) -> str:
    code = _exit_code(output)
    if code is None:
        return "unknown"
    return "success" if code == 0 else "failures"


def _within_window(path: Path, since: datetime | None, until: datetime | None) -> bool:
    if since is None and until is None:
        return True
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return False
    if since is not None and stamp < since:
        return False
    if until is not None and stamp > until:
        return False
    return True


def _extract_function_calls(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    calls: list[dict[str, Any]] = []
    outputs: dict[str, str] = {}
    for row in rows:
        for item in _walk_dicts(row):
            item_type = item.get("type")
            if item_type == "function_call":
                calls.append(item)
            elif item_type == "function_call_output":
                cid = _call_id(item)
                if cid:
                    outputs[cid] = str(item.get("output", ""))
    return calls, outputs


def audit_root(root: Path, since: datetime | None = None, until: datetime | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "root": str(root),
        "files": 0,
        "function_calls": 0,
        "kb_search_calls": 0,
        "kb_search_success": 0,
        "kb_search_failures": 0,
        "kb_search_unknown": 0,
        "kb_add_calls": 0,
        "kb_add_success": 0,
        "kb_add_failures": 0,
        "kb_add_unknown": 0,
        "file_story_calls": 0,
        "json_file_calls": 0,
        "inline_json_calls": 0,
        "spawn_agent_calls": 0,
        "spawn_agent_with_kb_background": 0,
        "spawn_agent_forbid_kb_search": 0,
        "spawn_agent_forbid_kb_add": 0,
        "spawn_agent_forbid_kb_update": 0,
    }

    if not root.exists():
        return result

    for path in rollout_paths(root):
        if not _within_window(path, since, until):
            continue
        rows = _load_jsonl(path)
        if not rows:
            continue
        result["files"] += 1
        calls, outputs = _extract_function_calls(rows)
        result["function_calls"] += len(calls)

        for call in calls:
            name = _call_name(call)
            args = _parse_arguments(call.get("arguments") or call.get("args"))
            command = str(args.get("command", ""))
            prompt = " ".join(str(args.get(key, "")) for key in ("message", "prompt", "instructions"))
            cid = _call_id(call)
            output = outputs.get(cid, "")

            if "kb_search.py" in command:
                result["kb_search_calls"] += 1
                result[f"kb_search_{_count_result(output)}"] += 1
            if "kb_add.py" in command:
                result["kb_add_calls"] += 1
                result[f"kb_add_{_count_result(output)}"] += 1
                if "--file" in command:
                    result["file_story_calls"] += 1
                if "--json-file" in command:
                    result["json_file_calls"] += 1
                if re.search(r"(^|\s)--json(\s|=)", command):
                    result["inline_json_calls"] += 1

            if name.endswith("spawn_agent") or name == "spawn_agent":
                result["spawn_agent_calls"] += 1
                if "背景知识" in prompt and "KB" in prompt:
                    result["spawn_agent_with_kb_background"] += 1
                if "kb_search.py" in prompt and ("禁止" in prompt or "不得" in prompt):
                    result["spawn_agent_forbid_kb_search"] += 1
                if "kb_add.py" in prompt and ("禁止" in prompt or "不得" in prompt):
                    result["spawn_agent_forbid_kb_add"] += 1
                if "kb_update.py" in prompt and ("禁止" in prompt or "不得" in prompt):
                    result["spawn_agent_forbid_kb_update"] += 1

    return result


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def main(argv: list[str]) -> int:
    default_root = default_sessions_root()
    parser = argparse.ArgumentParser(description="Audit Codex JSONL sessions for real personal-kb and subagent usage.")
    parser.add_argument("--root", default=str(default_root), help="Codex sessions root")
    parser.add_argument("--since", default="", help="Filter by file modified time, ISO format")
    parser.add_argument("--until", default="", help="Filter by file modified time, ISO format")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args(argv)

    result = audit_root(Path(args.root), _parse_datetime(args.since), _parse_datetime(args.until))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"Codex sessions root: {result['root']}")
    for key in (
        "files",
        "function_calls",
        "kb_search_calls",
        "kb_search_success",
        "kb_search_failures",
        "kb_add_calls",
        "kb_add_success",
        "kb_add_failures",
        "file_story_calls",
        "json_file_calls",
        "inline_json_calls",
        "spawn_agent_calls",
        "spawn_agent_with_kb_background",
        "spawn_agent_forbid_kb_search",
        "spawn_agent_forbid_kb_add",
        "spawn_agent_forbid_kb_update",
    ):
        print(f"{key}: {result[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
