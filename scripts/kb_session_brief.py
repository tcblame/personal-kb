#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

from kb_lib import append_jsonl, generate_entry_id, kb_base_dir, now_iso, read_jsonl, resolve_context, runtime_file


CURRENT_BRIEF_STATUSES = {"", "active", "current"}
DEFAULT_KEEP_CURRENT_BRIEFS = 2
ROLLED_OFF_STATUS = "rolled_off_current"
LOW_SIGNAL_TERMS = {
    "ai",
    "agent",
    "agents",
    "kb",
    "personal-kb",
    "知识库",
    "系统",
    "问题",
    "设计",
    "流程",
    "使用",
    "分析",
    "会话",
    "记录",
    "历史",
    "最近",
    "当前",
    "今天",
    "昨天",
    "明天",
}


def session_briefs_path(base_dir: Path | None = None) -> Path:
    effective_base = kb_base_dir() if base_dir is None else base_dir
    return runtime_file("session_briefs.jsonl", base_dir=effective_base)


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _dedupe_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        stripped = value.strip()
        if stripped and stripped not in out:
            out.append(stripped)
    return out


def _split_terms(text: str) -> list[str]:
    terms: list[str] = []
    for part in re.split(r"[\s,，、;；:：()（）\[\]【】{}<>《》\"'`]+", text or ""):
        value = part.strip().lower()
        if len(value) >= 2 and value not in terms:
            terms.append(value)
    return terms


def _is_code_like(term: str) -> bool:
    return bool(re.search(r"[a-z][\w-]*\.(py|md|json|ya?ml|toml|java|ts|tsx|js|jsx)$", term)) or bool(
        re.search(r"[/\\_.:-]", term)
    )


def _specific_terms(terms: list[str]) -> list[str]:
    out: list[str] = []
    for term in terms:
        value = term.strip().lower()
        if not value:
            continue
        if not _is_code_like(value) and value in LOW_SIGNAL_TERMS:
            continue
        if value not in out:
            out.append(value)
    return out


def _default_anchor_terms(*texts: str) -> list[str]:
    terms: list[str] = []
    for text in texts:
        for term in _specific_terms(_split_terms(text)):
            if term not in terms:
                terms.append(term)
    return terms[:10]


def _status_value(entry: dict[str, Any]) -> str:
    value = entry.get("status")
    return value.strip().lower().replace("-", "_") if isinstance(value, str) else ""


def _is_current(entry: dict[str, Any]) -> bool:
    return _status_value(entry) in CURRENT_BRIEF_STATUSES


def _ts_value(entry: dict[str, Any]) -> float:
    value = entry.get("ts")
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def build_brief(
    *,
    title: str,
    summary: str,
    repo: str,
    branch: str,
    cwd: str,
    tags: list[str],
    anchors: list[str],
    queries: list[str],
    used_entry_ids: list[str],
    written_entry_ids: list[str],
    updated_entry_ids: list[str],
    source: str,
    session_id: str,
    status: str = "current",
) -> dict[str, Any]:
    ts = now_iso()
    clean_title = _clip(title.strip() or "recent session brief", 120)
    clean_summary = _clip(summary.strip() or "recent session summary", 600)
    clean_queries = _dedupe_strings(queries)
    clean_tags = _dedupe_strings(tags)
    clean_anchors = _dedupe_strings(anchors) or _default_anchor_terms(clean_title, clean_summary, *clean_queries)
    return {
        "id": generate_entry_id(ts, clean_title),
        "ts": ts,
        "event": "kb_session_brief",
        "kind": "session_brief",
        "repo": repo,
        "branch": branch,
        "cwd": cwd,
        "title": clean_title,
        "summary": clean_summary,
        "tags": clean_tags,
        "anchors": clean_anchors,
        "queries": clean_queries[:6],
        "used_entry_ids": _dedupe_strings(used_entry_ids),
        "written_entry_ids": _dedupe_strings(written_entry_ids),
        "updated_entry_ids": _dedupe_strings(updated_entry_ids),
        "source": source.strip() or "unknown",
        "session_id": session_id.strip(),
        "status": status.strip() or "current",
    }


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")


def _same_repo_branch(entry: dict[str, Any], repo: str, branch: str) -> bool:
    return str(entry.get("repo", "")) == repo and str(entry.get("branch", "")) == branch


def _apply_current_limit(
    rows: list[dict[str, Any]],
    *,
    repo: str,
    branch: str,
    keep_current: int,
) -> bool:
    if not repo and not branch:
        return False
    keep_limit = min(3, max(1, keep_current))
    current_rows: list[tuple[float, str, int]] = []
    for index, row in enumerate(rows):
        if row.get("event") != "kb_session_brief":
            continue
        if not _same_repo_branch(row, repo, branch):
            continue
        if not _is_current(row):
            continue
        current_rows.append((_ts_value(row), str(row.get("id", "")), index))

    if len(current_rows) <= keep_limit:
        return False

    current_rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
    keep_indexes = {index for _, _, index in current_rows[:keep_limit]}
    changed = False
    for _, _, index in current_rows[keep_limit:]:
        row = rows[index]
        if row.get("status") != ROLLED_OFF_STATUS:
            row["status"] = ROLLED_OFF_STATUS
            changed = True
    return changed


def maintain_current_briefs(
    *,
    base_dir: Path | None = None,
    repo: str = "",
    branch: str = "",
    keep_current: int = DEFAULT_KEEP_CURRENT_BRIEFS,
) -> tuple[Path, int]:
    path = session_briefs_path(base_dir)
    if not path.exists():
        return path, 0

    rows = read_jsonl(path)
    changed = 0
    if repo or branch:
        changed = 1 if _apply_current_limit(rows, repo=repo, branch=branch, keep_current=keep_current) else 0
    else:
        coords: list[tuple[str, str]] = []
        for row in rows:
            if row.get("event") != "kb_session_brief":
                continue
            coord = (str(row.get("repo", "")), str(row.get("branch", "")))
            if coord not in coords:
                coords.append(coord)
        for current_repo, current_branch in coords:
            if _apply_current_limit(rows, repo=current_repo, branch=current_branch, keep_current=keep_current):
                changed += 1

    if changed:
        _write_rows(path, rows)
    return path, changed


def append_brief(
    brief: dict[str, Any],
    *,
    base_dir: Path | None = None,
    keep_current: int = DEFAULT_KEEP_CURRENT_BRIEFS,
) -> Path:
    path = session_briefs_path(base_dir)
    rows = read_jsonl(path) if path.exists() else []
    rows.append(brief)
    _apply_current_limit(
        rows,
        repo=str(brief.get("repo", "")),
        branch=str(brief.get("branch", "")),
        keep_current=keep_current,
    )
    _write_rows(path, rows)
    return path


def _read_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if args.json_file:
        try:
            payload = json.loads(Path(args.json_file).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid --json-file: {exc}") from exc
    if args.json:
        try:
            inline = json.loads(args.json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid --json: {exc}") from exc
        if not isinstance(inline, dict):
            raise ValueError("--json must be a JSON object")
        payload.update(inline)
    return payload


def _field_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item is not None)
    return "" if value is None else str(value)


def _match_fields(entry: dict[str, Any], terms: list[str]) -> dict[str, list[str]]:
    matched: dict[str, list[str]] = {}
    for field in ("title", "anchors", "queries", "tags", "summary", "repo", "branch"):
        text = _field_text(entry.get(field)).lower()
        if not text:
            continue
        hits = [term for term in terms if term in text]
        if hits:
            matched[field] = hits
    return matched


def _score_brief(entry: dict[str, Any], terms: list[str], *, repo: str, branch: str) -> tuple[float, dict[str, list[str]]]:
    matched = _match_fields(entry, terms)
    if not matched:
        return 0.0, {}

    specific = set(_specific_terms(terms))
    score = 0.0
    for field, hits in matched.items():
        if field == "title":
            weight = 6.0
        elif field in {"anchors", "queries"}:
            weight = 5.0
        elif field == "tags":
            weight = 3.0
        elif field in {"repo", "branch"}:
            weight = 2.5
        else:
            weight = 1.0
        for term in hits:
            score += weight if term in specific else min(weight, 0.4)

    if repo and entry.get("repo") == repo:
        score += 2.0
    if branch and entry.get("branch") == branch:
        score += 1.0

    ts = _ts_value(entry)
    if ts > 0.0:
        age_hours = max(0.0, (datetime.now().timestamp() - ts) / 3600.0)
        score += max(0.0, 2.5 - age_hours / 24.0)

    if specific and not any(field in {"title", "anchors", "queries", "tags"} for field in matched):
        return 0.0, matched
    return score, matched


def search_recent_briefs(
    query: str,
    *,
    cwd: Path | None = None,
    repo_name_override: str | None = None,
    branch_override: str | None = None,
    recent_days: int = 2,
    limit: int = 2,
    include_cross_repo: bool = False,
    max_snippet_chars: int = 220,
) -> list[dict[str, Any]]:
    path = session_briefs_path()
    if not path.exists():
        return []

    ctx = resolve_context(
        cwd=(cwd or Path.cwd()),
        repo_name_override=repo_name_override,
        branch_override=branch_override,
        task_hint=query,
        operation="search",
    )
    terms = _split_terms(query)
    cutoff_ts = (datetime.now() - timedelta(days=max(1, recent_days))).timestamp()
    rows = read_jsonl(path)
    scored: list[tuple[float, float, dict[str, Any], dict[str, list[str]]]] = []
    for row in rows:
        if row.get("event") != "kb_session_brief":
            continue
        if not _is_current(row):
            continue
        ts = _ts_value(row)
        if ts <= 0.0 or ts < cutoff_ts:
            continue
        if not include_cross_repo and ctx.repo_name and row.get("repo") != ctx.repo_name:
            continue
        score, matched = _score_brief(row, terms, repo=ctx.repo_name, branch=ctx.branch)
        if score <= 0.0:
            continue
        scored.append((score, ts, row, matched))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    items: list[dict[str, Any]] = []
    for score, _ts, row, matched in scored[: max(0, limit)]:
        matched_fields = list(matched.keys())
        item = {
            "entry_id": row.get("id", ""),
            "kind": "session_brief",
            "title": row.get("title", ""),
            "repo": row.get("repo", ""),
            "branch": row.get("branch", ""),
            "confidence": round(max(0.55, min(0.96, 0.58 + score / 20.0)), 2),
            "why_matched": ("matched " + ", ".join(matched_fields[:6])) if matched_fields else "matched recent session brief",
            "matched_fields": matched_fields,
            "summary": _clip(_field_text(row.get("summary")), max(80, max_snippet_chars)),
            "source_paths": [],
            "key_files": [],
            "context_layer": "recent_session",
            "queries": [q for q in row.get("queries", []) if isinstance(q, str)][:3],
            "anchors": [a for a in row.get("anchors", []) if isinstance(a, str)][:5],
            "warning": "recent session brief; verify against the current turn and current files",
            "_score": round(score, 4),
        }
        items.append(item)
    return items


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Manage recent AI-only session briefs for personal-kb.")
    sub = parser.add_subparsers(dest="action")

    add = sub.add_parser("add", help="Append a recent session brief")
    add.add_argument("--title", default="", help="Brief title")
    add.add_argument("--summary", default="", help="Brief summary")
    add.add_argument("--tags", default="", help="Comma-separated tags")
    add.add_argument("--anchors", default="", help="Comma-separated anchor terms")
    add.add_argument("--query", action="append", default=[], help="Original query used in the session; repeatable")
    add.add_argument("--used", action="append", default=[], help="Used KB entry ID; repeatable")
    add.add_argument("--written", action="append", default=[], help="Written KB entry ID; repeatable")
    add.add_argument("--updated", action="append", default=[], help="Updated KB entry ID; repeatable")
    add.add_argument("--repo", default="", help="Override repo bucket")
    add.add_argument("--branch", default="", help="Override branch bucket")
    add.add_argument("--source", default="unknown", help="Source runtime, for example codex or cc-switch")
    add.add_argument("--session-id", default="", help="Optional runtime session identifier")
    add.add_argument("--status", default="current", help="Brief status")
    add.add_argument("--json", default="", help="Inline JSON object to merge into the brief")
    add.add_argument("--json-file", default="", help="Read brief JSON object from a UTF-8 file")
    add.add_argument("--keep-current", type=int, default=DEFAULT_KEEP_CURRENT_BRIEFS, help="Keep at most N current briefs per repo/branch (1-3)")
    add.add_argument("--stdout", action="store_true", help="Also print the written brief JSON")

    maintain = sub.add_parser("maintain", help="Demote old current briefs and keep only the latest few per repo/branch")
    maintain.add_argument("--repo", default="", help="Limit maintenance to one repo bucket")
    maintain.add_argument("--branch", default="", help="Limit maintenance to one branch bucket")
    maintain.add_argument("--keep-current", type=int, default=DEFAULT_KEEP_CURRENT_BRIEFS, help="Keep at most N current briefs per repo/branch (1-3)")
    maintain.add_argument("--stdout", action="store_true", help="Also print maintenance result JSON")

    search = sub.add_parser("search", help="Search recent session briefs")
    search.add_argument("query", help="Search query")
    search.add_argument("--repo", default="", help="Override repo bucket")
    search.add_argument("--branch", default="", help="Override branch bucket")
    search.add_argument("--recent-days", type=int, default=2, help="Recent window in days")
    search.add_argument("--limit", type=int, default=2, help="Max brief hits")
    search.add_argument("--global", dest="global_search", action="store_true", help="Allow cross-repo recent briefs")
    search.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args(argv)
    if not args.action:
        parser.print_help()
        return 1

    if args.action == "search":
        items = search_recent_briefs(
            args.query,
            cwd=Path.cwd(),
            repo_name_override=(args.repo.strip() or None),
            branch_override=(args.branch.strip() or None),
            recent_days=args.recent_days,
            limit=args.limit,
            include_cross_repo=args.global_search,
        )
        payload = {"query": args.query, "items": items, "hit_count": len(items)}
        if args.json:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
            sys.stdout.write("\n")
        else:
            sys.stdout.write(f'KB_SESSION_BRIEF query="{args.query}" hits={len(items)}\n')
            for item in items:
                sys.stdout.write(f"- [{item['entry_id']}] {item['title']} ({item.get('repo','')}/{item.get('branch','')})\n")
                if item.get("summary"):
                    sys.stdout.write(f"  note: {item['summary']}\n")
        return 0

    if args.action == "maintain":
        path, changed = maintain_current_briefs(
            base_dir=kb_base_dir(),
            repo=args.repo.strip(),
            branch=args.branch.strip(),
            keep_current=args.keep_current,
        )
        payload = {"status": "ok", "path": str(path), "groups_changed": changed}
        if args.stdout:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
            sys.stdout.write("\n")
        else:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False))
            sys.stdout.write("\n")
        return 0

    try:
        payload = _read_payload(args)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    ctx = resolve_context(
        cwd=Path.cwd(),
        repo_name_override=(args.repo.strip() or None),
        branch_override=(args.branch.strip() or None),
        task_hint=" ".join(args.query),
        operation="closeout",
    )
    title = str(payload.get("title", "") or args.title)
    summary = str(payload.get("summary", "") or payload.get("story", "") or args.summary)
    if not title.strip() or not summary.strip():
        sys.stderr.write("recent session brief requires non-empty title and summary\n")
        return 2

    tags = []
    if isinstance(payload.get("tags"), list):
        tags.extend(str(item) for item in payload.get("tags", []) if str(item).strip())
    tags.extend(part.strip() for part in args.tags.split(",") if part.strip())

    anchors = []
    if isinstance(payload.get("anchors"), list):
        anchors.extend(str(item) for item in payload.get("anchors", []) if str(item).strip())
    anchors.extend(part.strip() for part in args.anchors.split(",") if part.strip())

    brief = build_brief(
        title=title,
        summary=summary,
        repo=ctx.repo_name,
        branch=ctx.branch,
        cwd=str(Path.cwd()),
        tags=tags,
        anchors=anchors,
        queries=[*payload.get("queries", []), *args.query] if isinstance(payload.get("queries"), list) else args.query,
        used_entry_ids=[*payload.get("used_entry_ids", []), *args.used] if isinstance(payload.get("used_entry_ids"), list) else args.used,
        written_entry_ids=[*payload.get("written_entry_ids", []), *args.written] if isinstance(payload.get("written_entry_ids"), list) else args.written,
        updated_entry_ids=[*payload.get("updated_entry_ids", []), *args.updated] if isinstance(payload.get("updated_entry_ids"), list) else args.updated,
        source=str(payload.get("source", "") or args.source),
        session_id=str(payload.get("session_id", "") or args.session_id),
        status=str(payload.get("status", "") or args.status),
    )
    path = append_brief(brief, keep_current=args.keep_current)

    if args.stdout:
        sys.stdout.write(json.dumps({"status": "ok", "path": str(path), "brief": brief}, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(json.dumps({"status": "ok", "path": str(path), "id": brief["id"]}, ensure_ascii=False))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
