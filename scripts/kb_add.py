from __future__ import annotations

import argparse
import base64
import binascii
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from datetime import datetime

from kb_kinds import DEFAULT_KIND, VALID_KINDS, legacy_type_to_kind
from kb_sensitive_scan import sensitive_findings
import kb_evidence

# Windows UTF-8 输出修复
if sys.platform == 'win32' and hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

from kb_lib import (
    JsonlSafetyError,
    append_jsonl,
    bucket_lock,
    ensure_branch_layout,
    find_entry,
    generate_entry_id,
    json_object_from_b64,
    load_config,
    mark_entry_as_milestone,
    now_iso,
    resolve_context,
    search_related_entries,
    should_replace_old_entry,
    update_entry_in_place,
    validate_entry_fields,
    write_index,
    kb_base_dir,
)

# 尝试导入倒排索引模块（可选依赖）
try:
    import importlib.util

    _kb_ii_path = Path(__file__).parent.parent / "backend" / "inverted_index.py"
    if _kb_ii_path.exists():
        _ii_spec = importlib.util.spec_from_file_location("inverted_index", _kb_ii_path)
        if _ii_spec and _ii_spec.loader:
            _ii_module = importlib.util.module_from_spec(_ii_spec)
            _ii_spec.loader.exec_module(_ii_module)
            InvertedIndex = _ii_module.InvertedIndex
            get_inverted_index_path = _ii_module.get_inverted_index_path
        else:
            InvertedIndex = None
            get_inverted_index_path = None
    else:
        InvertedIndex = None
        get_inverted_index_path = None
except Exception:
    InvertedIndex = None
    get_inverted_index_path = None

# 尝试导入桶聚合器模块（可选依赖，用于写入后增量刷新聚合视图）
try:
    import importlib.util

    _kb_ba_path = Path(__file__).parent.parent / "backend" / "bucket_aggregator.py"
    if _kb_ba_path.exists():
        _ba_spec = importlib.util.spec_from_file_location("bucket_aggregator", _kb_ba_path)
        if _ba_spec and _ba_spec.loader:
            _ba_module = importlib.util.module_from_spec(_ba_spec)
            _ba_spec.loader.exec_module(_ba_module)
            BucketAggregator = _ba_module.BucketAggregator
            get_bucket_aggregator_path = _ba_module.get_bucket_aggregator_path
        else:
            BucketAggregator = None
            get_bucket_aggregator_path = None
    else:
        BucketAggregator = None
        get_bucket_aggregator_path = None
except Exception:
    BucketAggregator = None
    get_bucket_aggregator_path = None


def _entry_keywords(entry: dict[str, Any]) -> list[str]:
    """从条目的 tags/aliases/trigger_terms 提取关键词（小写去重）。"""
    keywords: list[str] = []
    for field in ("tags", "aliases", "trigger_terms"):
        value = entry.get(field, [])
        if isinstance(value, list):
            keywords.extend([v for v in value if isinstance(v, str)])
        elif isinstance(value, str):
            keywords.extend([v.strip() for v in value.split(",") if v.strip()])
    seen: set[str] = set()
    out: list[str] = []
    for kw in keywords:
        low = kw.lower().strip()
        if low and low not in seen:
            seen.add(low)
            out.append(low)
    return out


def _refresh_affected_aggregations(entry: dict[str, Any]) -> None:
    """写入新记录后，增量刷新受影响的聚合视图（后台自动化，静默失败）。

    仅当新记录的关键词已存在聚合视图时才刷新；不存在聚合视图的关键词
    不会因一次写入就凭空生成聚合（生成仍由 AI 语义归纳决定）。
    刷新时通过倒排索引收集该关键词的全部条目，重算聚合并追加新版本。
    """
    if not (BucketAggregator and get_bucket_aggregator_path):
        return
    if not (InvertedIndex and get_inverted_index_path):
        return
    try:
        base = kb_base_dir()
        agg_dir = get_bucket_aggregator_path(base)
        aggregator = BucketAggregator(agg_dir)

        index_path = get_inverted_index_path(base)
        index = InvertedIndex(index_path)
        index.load()

        for kw in _entry_keywords(entry):
            # 只刷新已存在的聚合视图，不凭空生成
            if not aggregator.exists(kw):
                continue
            # 通过倒排索引收集该关键词的全部条目
            entry_ids = set(index.get_entries_by_keyword(kw))
            if not entry_ids:
                continue
            current_entries = []
            for kb_file in base.rglob("kb.jsonl"):
                for e in _iter_kb_jsonl(kb_file):
                    if e.get("id") in entry_ids and not e.get("_deleted") and not e.get("_archived"):
                        current_entries.append(e)
            if not current_entries:
                continue
            # 新鲜则跳过，过期才刷新
            freshness = aggregator.check_freshness(kw, current_entries)
            if freshness.get("is_fresh"):
                continue
            meta = {}
            info = index.get_keyword_info(kw)
            if info:
                meta["search_count"] = info.get("search_count", 0)
            aggregator.refresh_aggregation(kw, current_entries, meta)
    except Exception:
        pass


def _iter_kb_jsonl(path: Path) -> Any:
    """逐行读取 kb.jsonl，跳过坏行（用于聚合刷新收集条目）。"""
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                s = raw.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _update_inverted_index(entry: dict[str, Any], ctx) -> None:
    """写入后更新倒排索引（静默失败）"""
    if not InvertedIndex or not get_inverted_index_path:
        return
    try:
        base = kb_base_dir()
        index_path = get_inverted_index_path(base)
        index = InvertedIndex(index_path)
        index.load()
        keywords = []
        tags = entry.get("tags", [])
        if isinstance(tags, list):
            keywords.extend([t for t in tags if isinstance(t, str)])
        elif isinstance(tags, str):
            keywords.extend([t.strip() for t in tags.split(",") if t.strip()])
        aliases = entry.get("aliases", [])
        if isinstance(aliases, list):
            keywords.extend([a for a in aliases if isinstance(a, str)])
        elif isinstance(aliases, str):
            keywords.extend([a.strip() for a in aliases.split(",") if a.strip()])
        trigger_terms = entry.get("trigger_terms", [])
        if isinstance(trigger_terms, list):
            keywords.extend([t for t in trigger_terms if isinstance(t, str)])
        elif isinstance(trigger_terms, str):
            keywords.extend([t.strip() for t in trigger_terms.split(",") if t.strip()])
        bucket = f"{ctx.repo_name}/{ctx.branch}"
        if keywords and entry.get("id"):
            index.add_entry(entry["id"], keywords, bucket)
            index.save()
    except Exception:
        pass


def _parse_tags(value: str | None) -> list[str]:
    if not value:
        return []
    tags: list[str] = []
    for t in value.split(","):
        s = t.strip()
        if s:
            tags.append(s)
    return tags


def _normalize_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    if isinstance(value, str):
        return _parse_tags(value)
    return []


def _parse_kv_fields(items: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        out[key] = value
    return out


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _valid_evidence_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and str(value.get("type") or "") in {"git_commit", "conversation", "retained"}
        and bool(str(value.get("value") or "").strip())
    )


def _resolved_sources(entry: dict[str, Any], *, workspace_dir: Path) -> list[str]:
    resolved: list[str] = []
    for value in _as_list(entry.get("source_paths")):
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = workspace_dir / path
        if path.exists():
            resolved.append(str(value))
    return resolved


def _apply_quality_defaults(entry: dict[str, Any], *, ts: str, workspace_dir: Path) -> None:
    has_evidence = bool(
        _resolved_sources(entry, workspace_dir=workspace_dir)
        or any(_valid_evidence_ref(value) for value in _as_list(entry.get("evidence_refs")))
    )
    entry.setdefault("status", "current" if has_evidence else "draft_pending_evidence")
    if has_evidence:
        entry.setdefault("verified_at", ts)
    if entry.get("artifact_locator"):
        entry.setdefault("evidence_level", "derived_artifact" if has_evidence else "unverified")
        entry.setdefault("authority", "artifact_locator")
        entry.setdefault("verification_scope", "artifact_presence")
    else:
        entry.setdefault("evidence_level", "documented" if has_evidence else "unverified")
        entry.setdefault("authority", "verified_summary" if has_evidence else "unverified")
        entry.setdefault("verification_scope", "evidence_reference")


def _strict_quality_errors(entry: dict[str, Any], *, workspace_dir: Path) -> list[str]:
    errors: list[str] = []
    aliases = [item for item in _as_list(entry.get("aliases")) if str(item).strip()]
    triggers = [item for item in _as_list(entry.get("trigger_terms")) if str(item).strip()]
    sources = [item for item in _as_list(entry.get("source_paths")) if str(item).strip()]
    refs = [item for item in _as_list(entry.get("evidence_refs")) if item]
    if len(aliases) < 2 or len(aliases) > 8:
        errors.append("aliases 必须有 2-8 个")
    if len(triggers) < 3 or len(triggers) > 15:
        errors.append("trigger_terms 必须有 3-15 个")
    if not _resolved_sources(entry, workspace_dir=workspace_dir) and not any(_valid_evidence_ref(value) for value in refs):
        errors.append("必须提供当前可解析的 source_paths 或合法 evidence_refs")
    if refs and not all(_valid_evidence_ref(value) for value in refs):
        errors.append("evidence_refs 必须是包含 type/value 的合法引用")
    if any(str(value).startswith(("commit:", "conversation:")) for value in sources):
        errors.append("commit/conversation 引用必须放入 evidence_refs，不能放在 source_paths")
    if entry.get("artifact_locator") and entry.get("kind") != "map":
        errors.append("artifact_locator 必须使用 kind=map")
    return errors


def _write_success_summary(action: str, ctx: Any, entry: dict[str, Any], *, reason: str = "") -> None:
    summary = {
        "status": "ok",
        "action": action,
        "id": entry.get("id", ""),
        "repo": entry.get("repo", ctx.repo_name),
        "branch": entry.get("branch", ctx.branch),
        "kind": entry.get("kind", ""),
        "title": entry.get("title", ""),
        "aliases": _as_list(entry.get("aliases")),
        "key_files": _as_list(entry.get("key_files")),
        "source_paths": _as_list(entry.get("source_paths")),
        "confidence": entry.get("confidence", ""),
        "schema_version": entry.get("schema_version", 1),
        "record_rev": entry.get("record_rev", ""),
        "freshness_state": kb_evidence.verify_entry_evidence(entry).get("state", "legacy_unverified"),
        "path": str(ctx.kb_path),
    }
    if reason:
        summary["reason"] = reason
    if entry.get("supersedes"):
        summary["supersedes"] = entry.get("supersedes")
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Append one entry into personal-kb (JSONL).")
    parser.add_argument(
        "--kind",
        default=None,
        choices=sorted(VALID_KINDS),
        help=f"Entry kind: {','.join(sorted(VALID_KINDS))} (default: {DEFAULT_KIND})",
    )
    parser.add_argument(
        "--type",
        dest="legacy_type",
        default=None,
        help="Deprecated alias for --kind. Legacy fine-grained types are mapped to the 6-kind model.",
    )
    parser.add_argument("--title", default="", help="Short title (optional)")
    parser.add_argument(
        "--story",
        default="",
        help="Main content,建议写: 起因/经过/结果/验证 (optional). If omitted, reads from stdin.",
    )
    parser.add_argument(
        "--file",
        default="",
        help="Read story from a UTF-8 text/markdown file (optional). Used when --story is omitted.",
    )
    parser.add_argument("--tags", default="", help="Comma-separated tags, e.g. db,nginx,deploy")
    parser.add_argument("--repo", default="", help="Override repo name bucket (optional)")
    parser.add_argument("--branch", default="", help="Override branch bucket (optional)")
    parser.add_argument("--max-mb", type=float, default=20.0, help="Auto-compact threshold in MB (default: 20)")
    parser.add_argument("--keep-ratio", type=float, default=0.30, help="Keep newest ratio when auto-compacting (default: 0.30)")
    parser.add_argument("--no-auto-compact", action="store_true", help="Disable auto compact even if kb.jsonl exceeds threshold")
    parser.add_argument(
        "--field",
        action="append",
        default=[],
        help="Extra flat fields in key=value form. Repeatable.",
    )
    parser.add_argument(
        "--json",
        dest="json_data",
        default="",
        help="Extra JSON object to merge into the entry. Prefer --json-b64/--entry-b64 on PowerShell.",
    )
    parser.add_argument(
        "--json-b64",
        dest="json_b64",
        default="",
        help="Extra JSON object encoded as UTF-8 base64. Recommended for direct PowerShell writes.",
    )
    parser.add_argument(
        "--json-file",
        dest="json_file",
        default="",
        help="Read JSON object from a UTF-8 file (fallback only; prefer --entry-b64 for KB writes).",
    )
    parser.add_argument(
        "--entry-b64",
        dest="entry_b64",
        default="",
        help=(
            "Full entry payload encoded as UTF-8 base64 JSON. "
            "May include kind/title/story/tags/repo/branch plus extra fields; writes directly to KB without temp files."
        ),
    )
    parser.add_argument(
        "--update-mode",
        choices=["auto", "replace", "milestone"],
        default="auto",
        help="更新模式：auto=AI判断, replace=直接替换旧条目, milestone=保留旧条目为里程碑 (default: auto)",
    )
    parser.add_argument(
        "--smart-field-check",
        action="store_true",
        help="启用长期知识质量门禁：检查 aliases、trigger_terms、证据指针和 artifact_locator 结构",
    )
    parser.add_argument(
        "--task-hint",
        default="",
        help="任务描述或关键词，用于智能推断仓库（多子仓库场景）",
    )
    args = parser.parse_args(argv)

    entry_payload: dict[str, Any] = {}
    if args.entry_b64.strip():
        try:
            entry_payload = json_object_from_b64(args.entry_b64.strip(), "--entry-b64")
        except ValueError as e:
            sys.stderr.write(f"{e}\n")
            return 2

    if (
        sys.platform == "win32"
        and args.json_data.strip()
        and not args.json_file.strip()
        and not args.json_b64.strip()
        and os.environ.get("PERSONAL_KB_ALLOW_INLINE_JSON") != "1"
    ):
        sys.stderr.write(
            "Inline --json is disabled by default on Windows because PowerShell often rewrites quotes. "
            "Use --json-b64 or --entry-b64 for direct writes, or set PERSONAL_KB_ALLOW_INLINE_JSON=1 for a verified legacy command.\n"
        )
        return 2

    payload_legacy_type = ""
    if "type" in entry_payload:
        payload_legacy_type = str(entry_payload.pop("type") or "").strip()

    # 提前提取 task_hint（在 resolve_context 之前）
    task_hint = args.task_hint.strip()
    if not task_hint:
        # 从 title + tags 自动提取
        title_hint = args.title.strip() or entry_payload.get("title", "")
        tags_hint = args.tags.strip()
        if not tags_hint:
            tags_list = entry_payload.get("tags", [])
            if isinstance(tags_list, list):
                tags_hint = " ".join(tags_list)
        task_hint = f"{title_hint} {tags_hint}".strip()

    legacy_kind = None
    legacy_value = (args.legacy_type or payload_legacy_type).strip() if (args.legacy_type or payload_legacy_type) else ""
    if legacy_value:
        legacy_kind = legacy_type_to_kind(legacy_value)
        if not legacy_kind:
            sys.stderr.write(
                f"Unsupported --type value '{legacy_value}'. Use --kind with one of: {', '.join(sorted(VALID_KINDS))}.\n"
            )
            return 2
        sys.stderr.write(f"⚠️  --type 已废弃，已按 kind={legacy_kind} 兼容处理；新命令请改用 --kind。\n")

    entry_kind = args.kind or str(entry_payload.get("kind") or legacy_kind or DEFAULT_KIND)
    story = args.story or str(entry_payload.get("story") or "")
    if not story.strip():
        if args.file.strip():
            try:
                story = Path(args.file.strip()).read_text(encoding="utf-8-sig", errors="replace")
            except OSError as e:
                sys.stderr.write(f"Failed to read --file: {e}\n")
                return 2
        elif not sys.stdin.isatty():
            story = sys.stdin.read()
        story = story.strip()

    # 长文本警告：--story 参数传递超长内容时提示使用 --file
    if story and len(story) > 2000 and args.story.strip():
        sys.stderr.write(
            f"⚠️  Warning: --story content is {len(story)} chars (>2000). "
            "Consider using --file <path> to avoid encoding issues.\n"
        )

    title = (args.title or str(entry_payload.get("title") or "")).strip()
    if not title and story:
        title = story.splitlines()[0].strip()[:120]
    if not title:
        title = f"{entry_kind} @ {now_iso()}"

    ctx = resolve_context(
        cwd=Path.cwd(),
        repo_name_override=(args.repo.strip() or str(entry_payload.get("repo") or "").strip() or None),
        branch_override=(args.branch.strip() or str(entry_payload.get("branch") or "").strip() or None),
        task_hint=task_hint or None,
    )
    ensure_branch_layout(ctx)

    extra: dict[str, Any] = {}
    reserved_payload_keys = {
        "kind", "title", "story", "tags", "repo", "branch", "branch_dir",
        "workspace_dir", "id", "ts", "used_count", "last_used_ts",
        "schema_version", "record_rev", "evidence_snapshots",
    }
    extra.update({k: v for k, v in entry_payload.items() if k not in reserved_payload_keys})
    extra.update(_parse_kv_fields(args.field))
    json_data_to_parse = args.json_data.strip()
    if args.json_b64.strip():
        try:
            extra.update(json_object_from_b64(args.json_b64.strip(), "--json-b64"))
        except ValueError as e:
            sys.stderr.write(f"{e}\n")
            return 2
    if args.json_file.strip():
        try:
            json_data_to_parse = Path(args.json_file.strip()).read_text(encoding="utf-8-sig", errors="replace").strip()
        except OSError as e:
            sys.stderr.write(f"Failed to read --json-file: {e}\n")
            return 2
    if json_data_to_parse:
        try:
            parsed = json.loads(json_data_to_parse)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"Invalid JSON: {e}\n")
            return 2
        if not isinstance(parsed, dict):
            sys.stderr.write("JSON must be a JSON object\n")
            return 2
        extra.update(parsed)

    if "type" in extra:
        sys.stderr.write("Entry field 'type' is no longer supported. Use 'kind' with one of the 6 valid kinds.\n")
        return 2

    protected_extra_fields = {
        "id", "ts", "kind", "title", "story", "tags", "repo", "branch", "branch_dir",
        "workspace_dir", "schema_version", "record_rev", "evidence_snapshots",
    }
    invalid_extra_fields = sorted(protected_extra_fields.intersection(extra))
    if invalid_extra_fields:
        sys.stderr.write(
            "Extra JSON/fields cannot override core entry fields: "
            + ", ".join(invalid_extra_fields)
            + "\n"
        )
        return 2

    tags = _parse_tags(args.tags) if args.tags else _normalize_tags(entry_payload.get("tags"))

    ts = now_iso()
    entry: dict[str, Any] = {
        "id": generate_entry_id(ts, title),
        "ts": ts,
        "kind": entry_kind,
        "repo": ctx.repo_name,
        "branch": ctx.branch,
        "branch_dir": ctx.branch_dir,
        "workspace_dir": ctx.workspace_dir,
        "title": title,
        "story": story,
        "tags": tags,
        "used_count": 0,
        "last_used_ts": "",
    }
    entry.update(extra)
    entry["used_count"] = 0
    entry["last_used_ts"] = ""
    _apply_quality_defaults(entry, ts=ts, workspace_dir=Path(ctx.workspace_dir))
    entry["schema_version"] = 2
    entry["evidence_snapshots"] = kb_evidence.capture_evidence_snapshots(entry, Path(ctx.workspace_dir))
    initial_freshness = kb_evidence.verify_entry_evidence(entry, Path(ctx.workspace_dir))
    entry["verification_scope"] = "local_head_snapshot"
    if initial_freshness["state"] == "fresh":
        entry["verified_at"] = ts
        entry["status"] = "current"
    else:
        entry.pop("verified_at", None)
        entry["status"] = "draft_pending_evidence"
    entry["record_rev"] = kb_evidence.canonical_entry_revision(entry)

    # P0 最低质量校验：复杂分层字段可选，提供时校验类型和值域
    valid, error_msg = validate_entry_fields(entry)
    if not valid:
        sys.stderr.write(f"❌ 字段验证失败: {error_msg}\n")
        sys.stderr.write("请运行 kb_schema_discover.py suggest --kind <kind> 查看 6kind 字段建议\n")
        return 3

    findings = sensitive_findings(entry)
    if findings:
        sys.stderr.write(
            "Sensitive credential-shaped content detected in the durable row; retain full bytes as a local asset and store its asset_id, or use a redacted summary. "
            f"finding_types={','.join(findings)}\n"
        )
        return 4

    # --smart-field-check：先给字段建议，再执行长期知识严格质量门禁。
    if args.smart_field_check:
        try:
            schema_script = Path(__file__).with_name("kb_schema_discover.py")
            if schema_script.exists():
                import subprocess
                check_result = subprocess.run(
                    [sys.executable, str(schema_script), "check", "--kind", entry_kind, "--entry-json", json.dumps(entry), "--json"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if check_result.returncode == 0 and check_result.stdout.strip():
                    try:
                        check_data = json.loads(check_result.stdout)
                        warnings = []
                        if check_data.get("missing_core"):
                            warnings.append(f"缺失推荐核心字段: {', '.join(check_data['missing_core'])}")
                        if not check_data.get("has_trigger_terms") and entry_kind in ["issue", "map", "pitfall"]:
                            warnings.append(f"{entry_kind} 类记录建议提供 trigger_terms ({check_data.get('trigger_terms_hint', '')})")

                        if warnings:
                            sys.stderr.write("💡 智能字段建议:\n")
                            for w in warnings:
                                sys.stderr.write(f"   • {w}\n")
                            sys.stderr.write("   (建议用于补充语义字段；下方严格门禁负责阻止低质量写入)\n\n")
                    except json.JSONDecodeError:
                        pass
        except (OSError, subprocess.TimeoutExpired):
            pass

        quality_errors = _strict_quality_errors(entry, workspace_dir=Path(ctx.workspace_dir))
        if quality_errors:
            sys.stderr.write("❌ 严格质量门禁失败:\n")
            for error in quality_errors:
                sys.stderr.write(f"   • {error}\n")
            return 3

    # 固定 bucket 锁覆盖“查重/判断/更新或追加”的完整事务。
    with bucket_lock(ctx.kb_path):
        related_entries = search_related_entries(ctx.kb_path, entry)

        if related_entries and args.update_mode != "milestone":
            old_entry = related_entries[0]
            if args.update_mode == "auto":
                context_str = f"{title} {story}"
                should_replace, reason = should_replace_old_entry(old_entry, context_str)
            else:
                should_replace = (args.update_mode == "replace")
                reason = f"用户指定模式: {args.update_mode}"

            if should_replace:
                updates = {
                    **extra,
                    "kind": entry_kind,
                    "title": title,
                    "story": story,
                    "tags": tags,
                    "status": entry.get("status"),
                    "verified_at": entry.get("verified_at"),
                    "evidence_level": entry.get("evidence_level"),
                    "authority": entry.get("authority"),
                    "verification_scope": entry.get("verification_scope"),
                    "schema_version": entry.get("schema_version"),
                    "evidence_snapshots": entry.get("evidence_snapshots"),
                }
                if update_entry_in_place(ctx.kb_path, old_entry["id"], updates, lock_held=True):
                    from kb_lib import read_jsonl

                    updated_entry = next(
                        candidate for candidate in read_jsonl(ctx.kb_path)
                        if candidate.get("id") == old_entry["id"]
                    )
                    write_index(ctx)
                    _update_inverted_index(updated_entry, ctx)
                    _write_success_summary("updated", ctx, updated_entry, reason=f"替换旧条目 {old_entry['id']}: {reason}")
                    return 0
            else:
                if mark_entry_as_milestone(ctx.kb_path, old_entry["id"], reason, lock_held=True):
                    entry["supersedes"] = [old_entry["id"]]
                    entry["record_rev"] = kb_evidence.canonical_entry_revision(entry)
                    sys.stderr.write(f"[里程碑] 旧条目 {old_entry['id']} 保留为里程碑: {reason}\n")

        append_jsonl(ctx.kb_path, entry, lock_held=True)

    write_index(ctx)
    _update_inverted_index(entry, ctx)

    # 增量刷新受影响的聚合视图（后台自动化，静默失败）
    _refresh_affected_aggregations(entry)

    if not args.no_auto_compact:
        try:
            max_bytes = int(args.max_mb * 1024 * 1024)
            if max_bytes > 0 and ctx.kb_path.exists() and ctx.kb_path.stat().st_size > max_bytes:
                compact_script = Path(__file__).with_name("kb_compact.py")
                subprocess.run(
                    [
                        sys.executable,
                        str(compact_script),
                        "--repo",
                        ctx.repo_name,
                        "--branch",
                        ctx.branch,
                        "--max-mb",
                        str(args.max_mb),
                        "--keep-ratio",
                        str(args.keep_ratio),
                    ],
                    check=False,
                )
        except OSError:
            pass

    # 写入日志记录
    log_dir = Path.home() / ".codex" / "personal-kb-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "kb_add.log"
    log_entry = {
        "ts": datetime.now().isoformat(),
        "kind": entry_kind,
        "repo": ctx.repo_name,
        "branch": ctx.branch,
        "title": title[:100],
        "aliases": extra.get("aliases", []),
        "key_files": extra.get("key_files", []),
        "cwd": str(Path.cwd())
    }
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except:
        pass

    _write_success_summary("added", ctx, entry)
    return 0


def main(argv: list[str]) -> int:
    try:
        return _main(argv)
    except JsonlSafetyError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 5


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
