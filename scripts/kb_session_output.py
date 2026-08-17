from __future__ import annotations

import json
import re
from typing import Any


RETRIEVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
RAG_TEXT_RETRIEVAL_ID_RE = re.compile(r'retrieval_id="([A-Za-z0-9._:-]+)"')
RAG_TEXT_HITS_RE = re.compile(r"\bhits=(\d+)(?=\s|$)")


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
        if not stripped.startswith("KB_RAG_CONTEXT "):
            continue
        ids = RAG_TEXT_RETRIEVAL_ID_RE.findall(stripped)
        hits = RAG_TEXT_HITS_RE.findall(stripped)
        retrieval_id = ids[-1] if ids and RETRIEVAL_ID_RE.fullmatch(ids[-1]) else ""
        results.append({
            "retrieval_id": retrieval_id,
            "hit_count": int(hits[-1]) if hits else None,
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
        results.append({"retrieval_id": retrieval_id, "hit_count": hit_count})
    return results


def retrieval_results(output: str) -> list[dict[str, Any]]:
    """Return the most complete ordered RAG-result group from an exec envelope."""
    best: list[dict[str, Any]] = []
    for text in nested_output_texts(output):
        for candidates in (_header_results(text), _json_results(text)):
            if len(candidates) > len(best):
                best = candidates
    return best
