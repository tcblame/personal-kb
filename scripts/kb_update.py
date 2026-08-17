from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import kb_adoption
import kb_evidence
from kb_lib import JsonlSafetyError, bucket_lock, find_entry, generate_entry_id, json_object_from_b64, kb_base_dir, now_iso, read_jsonl, resolve_context, validate_entry_fields, write_index, _rewrite_jsonl
from kb_sensitive_scan import sensitive_findings

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


def _entry_keywords(entry: dict[str, Any]) -> list[str]:
    """从条目的 tags/aliases/trigger_terms 提取关键词"""
    keywords: list[str] = []
    for field in ("tags", "aliases", "trigger_terms"):
        value = entry.get(field, [])
        if isinstance(value, list):
            keywords.extend([v for v in value if isinstance(v, str) and v.strip()])
        elif isinstance(value, str):
            keywords.extend([v.strip() for v in value.split(",") if v.strip()])
    return keywords


def _sync_inverted_index(entry: dict[str, Any], ctx, *, old_keywords: list[str] | None = None, remove_only: bool = False) -> None:
    """同步倒排索引：删除旧关键词、添加新关键词（静默失败）"""
    if not InvertedIndex or not get_inverted_index_path:
        return
    try:
        base = kb_base_dir()
        index_path = get_inverted_index_path(base)
        index = InvertedIndex(index_path)
        index.load()

        entry_id = entry.get("id")
        if not entry_id:
            return

        # 删除旧关键词（如果提供）
        if old_keywords:
            index.remove_entry(entry_id, old_keywords)

        # 添加新关键词（除非是纯删除）
        if not remove_only:
            new_keywords = _entry_keywords(entry)
            bucket = f"{ctx.repo_name}/{ctx.branch}"
            if new_keywords:
                index.add_entry(entry_id, new_keywords, bucket)

        index.save()
    except Exception:
        pass


def _find_entry_across_buckets(entry_id: str) -> tuple[list[dict], int, Path] | None:
    """跨所有 bucket 扫描查找 entry，返回 (entries, idx, kb_path) 或 None。

    当 cwd-based bucket 找不到 entry 时使用此函数作为 fallback。
    扫描所有 repos/**/kb.jsonl 文件定位 entry 所在位置。
    """
    base = kb_base_dir()
    if not base.exists():
        return None

    # 遍历所有 kb.jsonl 文件
    for kb_file in base.rglob("kb.jsonl"):
        entries = read_jsonl(kb_file)
        idx = find_entry(entries, entry_id)
        if idx is not None:
            return entries, idx, kb_file

    return None


def _resolve_context_from_kb_path(kb_path: Path):
    """从已知的 kb_path 反推 RepoContext（用于跨 bucket 定位后的写回）。"""
    base = kb_base_dir()
    # kb_path 格式: base / repo_parts... / branch / kb.jsonl
    rel = kb_path.relative_to(base)
    parts = rel.parts[:-1]  # 去掉 kb.jsonl

    if len(parts) < 2:
        # 至少需要 repo/branch 两层
        return resolve_context(cwd=Path.cwd(), repo_name_override="/".join(parts[:-1]) if len(parts) > 1 else parts[0] if parts else None, branch_override=parts[-1] if parts else None)

    branch = parts[-1]
    repo_name = "/".join(parts[:-1])

    return resolve_context(cwd=Path.cwd(), repo_name_override=repo_name, branch_override=branch)


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _current_record_rev(entry: dict[str, Any]) -> str:
    stored = entry.get("record_rev")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    return kb_evidence.canonical_entry_revision(entry)


def _locate_entry_context(ctx, entry_id: str):
    entries = read_jsonl(ctx.kb_path)
    if find_entry(entries, entry_id) is not None:
        return ctx
    fallback = _find_entry_across_buckets(entry_id)
    if fallback is None:
        return None
    _entries, _idx, found_kb_path = fallback
    located_ctx = _resolve_context_from_kb_path(found_kb_path)
    sys.stderr.write(f"⚠️  Entry 不在当前 bucket，已定位到：{found_kb_path}\n")
    return located_ctx


def _repos_base_from_context(ctx) -> Path:
    for parent in [ctx.kb_path.parent, *ctx.kb_path.parents]:
        if parent.name == "repos":
            return parent
    return kb_base_dir()


def _main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Update or delete a personal-kb entry by ID.")
    sub = parser.add_subparsers(dest="action")

    # update subcommand
    up = sub.add_parser("update", help="Update fields of an existing entry")
    up.add_argument("entry_id", help="Entry ID (8-char hex from 'id' field or computed from ts+title)")
    up.add_argument("--field", action="append", default=[], help="key=value fields to update (repeatable)")
    up.add_argument("--json", dest="json_data", default="", help="JSON object to merge into entry")
    up.add_argument("--json-b64", dest="json_b64", default="", help="JSON object encoded as UTF-8 base64")
    up.add_argument("--json-file", dest="json_file", default="", help="Read JSON object from a UTF-8 file (fallback only)")
    up.add_argument(
        "--entry-b64",
        dest="entry_b64",
        default="",
        help="Update payload encoded as UTF-8 base64 JSON. May include title/story/tags plus extra fields.",
    )
    up.add_argument("--title", default=None, help="Update title")
    up.add_argument("--story", default=None, help="Update story")
    up.add_argument("--file", dest="story_file", default="", help="Read story from a UTF-8 file")
    up.add_argument("--tags", default=None, help="Replace tags (comma-separated)")
    up.add_argument("--expected-rev", default="", help="Reject the update unless the current record_rev matches")
    up.add_argument("--refresh-evidence", action="store_true", help="Recapture evidence snapshots after reviewing current files")
    up.add_argument("--repo", default="", help="Override repo bucket")
    up.add_argument("--branch", default="", help="Override branch bucket")

    # delete subcommand
    dl = sub.add_parser("delete", help="Soft-delete an entry (marks as deleted, keeps in file)")
    dl.add_argument("entry_id", help="Entry ID")
    dl.add_argument("--hard", action="store_true", help="Hard delete (remove from file entirely)")
    dl.add_argument("--expected-rev", default="", help="Reject the delete unless the current record_rev matches")
    dl.add_argument("--repo", default="", help="Override repo bucket")
    dl.add_argument("--branch", default="", help="Override branch bucket")

    # list-ids subcommand
    li = sub.add_parser("list-ids", help="List recent entry IDs for reference")
    li.add_argument("--limit", type=int, default=10, help="Number of recent entries to show")
    li.add_argument("--repo", default="", help="Override repo bucket")
    li.add_argument("--branch", default="", help="Override branch bucket")

    # use subcommand
    use = sub.add_parser("use", help="Record one runtime adoption event without rewriting durable KB")
    use.add_argument("entry_id", help="Entry ID")
    use.add_argument("--effect", choices=sorted(kb_adoption.VALID_EFFECTS), default="legacy", help="Adoption effect")
    use.add_argument("--event-id", default="", help="Idempotency key; retries with the same ID count once")
    use.add_argument("--session-id", default="", help="Optional runtime session identifier")
    use.add_argument("--repo", default="", help="Override repo bucket")
    use.add_argument("--branch", default="", help="Override branch bucket")

    args = parser.parse_args(argv)

    if not args.action:
        parser.print_help()
        return 1

    repo_override = getattr(args, "repo", "").strip() or None
    branch_override = getattr(args, "branch", "").strip() or None
    ctx = resolve_context(cwd=Path.cwd(), repo_name_override=repo_override, branch_override=branch_override)

    if args.action == "list-ids":
        entries = read_jsonl(ctx.kb_path)
        entries.reverse()
        for e in entries[: args.limit]:
            eid = e.get("id") or generate_entry_id(e.get("ts", ""), e.get("title", ""))
            ts = e.get("ts", "")
            title = e.get("title", "")[:60]
            sys.stdout.write(f"{eid}  {ts}  {title}\n")
        return 0

    located_ctx = _locate_entry_context(ctx, args.entry_id)
    if located_ctx is None:
        sys.stderr.write(f"Entry not found: {args.entry_id}\n")
        return 2
    ctx = located_ctx

    # The stable bucket lock covers the complete read-modify-write transaction.
    with bucket_lock(ctx.kb_path):
        entries = read_jsonl(ctx.kb_path)
        idx = find_entry(entries, args.entry_id)
        if idx is None:
            sys.stderr.write(f"Entry changed while waiting for bucket lock: {args.entry_id}\n")
            return 6

        if args.action == "update":
            entry = entries[idx]
            expected_rev = args.expected_rev.strip()
            current_rev = _current_record_rev(entry)
            if expected_rev and expected_rev != current_rev:
                sys.stderr.write(
                    f"record_rev mismatch for {args.entry_id}: expected={expected_rev} current={current_rev}\n"
                )
                return 6
            original_entry = dict(entry)
            old_keywords_before_update = _entry_keywords(entry)

            if args.entry_b64.strip():
                try:
                    entry.update(json_object_from_b64(args.entry_b64.strip(), "--entry-b64"))
                except ValueError as exc:
                    sys.stderr.write(f"{exc}\n")
                    return 2
            for item in args.field:
                if "=" in item:
                    key, value = item.split("=", 1)
                    entry[key.strip()] = value.strip()
            if args.json_b64.strip():
                try:
                    entry.update(json_object_from_b64(args.json_b64.strip(), "--json-b64"))
                except ValueError as exc:
                    sys.stderr.write(f"{exc}\n")
                    return 2
            json_data_to_parse = args.json_data.strip()
            if args.json_file.strip():
                try:
                    json_data_to_parse = Path(args.json_file.strip()).read_text(
                        encoding="utf-8-sig", errors="replace"
                    ).strip()
                except OSError as exc:
                    sys.stderr.write(f"Failed to read --json-file: {exc}\n")
                    return 2
            if json_data_to_parse:
                try:
                    parsed = json.loads(json_data_to_parse)
                except json.JSONDecodeError as exc:
                    sys.stderr.write(f"Invalid JSON: {exc}\n")
                    return 2
                if not isinstance(parsed, dict):
                    sys.stderr.write("Update JSON must be an object\n")
                    return 2
                entry.update(parsed)
            if args.title is not None:
                entry["title"] = args.title
            if args.story is not None:
                entry["story"] = args.story
            elif args.story_file.strip():
                try:
                    entry["story"] = Path(args.story_file.strip()).read_text(
                        encoding="utf-8-sig", errors="replace"
                    ).strip()
                except OSError as exc:
                    sys.stderr.write(f"Failed to read --file: {exc}\n")
                    return 2
            if args.tags is not None:
                entry["tags"] = [tag.strip() for tag in args.tags.split(",") if tag.strip()]

            immutable_fields = {
                "id", "ts", "repo", "branch", "branch_dir", "workspace_dir",
                "used_count", "last_used_ts", "schema_version", "record_rev", "evidence_snapshots",
            }
            changed_immutable = sorted(
                field for field in immutable_fields
                if entry.get(field) != original_entry.get(field)
            )
            if changed_immutable:
                sys.stderr.write(
                    "Update cannot modify immutable entry fields: "
                    + ", ".join(changed_immutable)
                    + "\n"
                )
                return 2

            valid, error_msg = validate_entry_fields(entry)
            if not valid:
                sys.stderr.write(f"Entry validation failed: {error_msg}\n")
                return 3
            findings = sensitive_findings(entry)
            if findings:
                sys.stderr.write(
                    "Sensitive credential-shaped content detected in the durable row; retain full bytes as a local asset and store its asset_id, or use a redacted summary. "
                    f"finding_types={','.join(findings)}\n"
                )
                return 4

            try:
                schema_version = int(entry.get("schema_version") or 1)
            except (TypeError, ValueError):
                schema_version = 1
            needs_initial_snapshot = schema_version < 2 or "evidence_snapshots" not in entry
            if args.refresh_evidence or needs_initial_snapshot:
                workspace = Path(str(entry.get("workspace_dir") or ctx.workspace_dir))
                entry["schema_version"] = 2
                entry["evidence_snapshots"] = kb_evidence.capture_evidence_snapshots(entry, workspace)
                verification = kb_evidence.verify_entry_evidence(entry, workspace)
                entry["verification_scope"] = "local_head_snapshot"
                if verification["state"] == "fresh":
                    entry["verified_at"] = now_iso()
                    if str(entry.get("status") or "") == "draft_pending_evidence":
                        entry["status"] = "current"
                else:
                    entry.pop("verified_at", None)
                    entry["status"] = "draft_pending_evidence"

            entry["updated_ts"] = now_iso()
            entry["record_rev"] = kb_evidence.canonical_entry_revision(entry)
            entries[idx] = entry
            _rewrite_jsonl(ctx.kb_path, entries, lock_held=True)
            write_index(ctx)
            _sync_inverted_index(entry, ctx, old_keywords=old_keywords_before_update)
            sys.stdout.write(json.dumps({
                "status": "ok",
                "action": "update",
                "id": args.entry_id,
                "record_rev": entry["record_rev"],
                "path": str(ctx.kb_path),
            }, ensure_ascii=False, indent=2) + "\n")
            return 0

        if args.action == "use":
            entry = entries[idx]
            event_id = args.event_id.strip() or uuid.uuid4().hex
            adoption_base = _repos_base_from_context(ctx)
            kb_adoption.append_adoption_event(
                str(entry.get("id") or args.entry_id),
                args.effect,
                str(entry.get("repo") or ctx.repo_name),
                str(entry.get("branch") or ctx.branch),
                event_id,
                session_id=args.session_id,
                base_dir=adoption_base,
            )
            stats = kb_adoption.load_adoption_stats(adoption_base)
            entry_stats = stats.get(str(entry.get("id") or args.entry_id), {})
            summary = {
                "status": "ok",
                "action": args.action,
                "id": entry.get("id", args.entry_id),
                "event_id": event_id,
                "effect": args.effect,
                "legacy_used_count": _int_or_zero(entry.get("used_count", 0)),
                "runtime_heated_count": _int_or_zero(entry_stats.get("heated_count", 0)),
                "effective_used_count": kb_adoption.effective_usage(entry, stats),
                "last_used_ts": entry_stats.get("last_used_ts", "") or entry.get("last_used_ts", ""),
                "path": str(kb_adoption.adoption_events_path(adoption_base)),
            }
            sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            return 0

        if args.action == "delete":
            entry = entries[idx]
            expected_rev = args.expected_rev.strip()
            current_rev = _current_record_rev(entry)
            if expected_rev and expected_rev != current_rev:
                sys.stderr.write(
                    f"record_rev mismatch for {args.entry_id}: expected={expected_rev} current={current_rev}\n"
                )
                return 6
            old_keywords = _entry_keywords(entry)
            if args.hard:
                entries.pop(idx)
            else:
                entries[idx]["_deleted"] = True
                entries[idx]["deleted_ts"] = now_iso()
                entries[idx]["updated_ts"] = now_iso()
                entries[idx]["record_rev"] = kb_evidence.canonical_entry_revision(entries[idx])
            _rewrite_jsonl(ctx.kb_path, entries, lock_held=True)
            write_index(ctx)
            _sync_inverted_index(entry, ctx, old_keywords=old_keywords, remove_only=True)
            mode = "hard-deleted" if args.hard else "soft-deleted"
            sys.stdout.write(f"{mode}: {args.entry_id}\n")
            return 0

    return 1


def main(argv: list[str]) -> int:
    try:
        return _main(argv)
    except JsonlSafetyError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 5


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
