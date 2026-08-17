#!/usr/bin/env python3
"""
KB 迁移脚本：将错位的 KB 条目迁移到正确的项目桶

职责：只执行迁移，不做判断（判断交给 AI）

用法：
    kb_migrate.py <entry_id> --to-repo <repo> --to-branch <branch> \\
        --confidence <0-1> --reason "<原因>" [--from-repo <repo>] [--from-branch <branch>] [--dry-run]

示例：
    kb_migrate.py abc12345 --to-repo example-project-a --to-branch example-branch-a/dev-gch \\
        --confidence 0.95 --reason "标题和tags均为example-project-a项目"
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from kb_lib import (
    generate_entry_id,
    kb_base_dir,
    now_iso,
    read_jsonl,
    resolve_context,
    write_index,
    _rewrite_jsonl,
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


def _migrate_inverted_index(entry: dict[str, Any], from_bucket: str, to_bucket: str) -> None:
    """迁移时更新倒排索引：从旧桶移除，在新桶添加（静默失败）"""
    if not InvertedIndex or not get_inverted_index_path:
        return
    try:
        base = kb_base_dir()
        index_path = get_inverted_index_path(base)
        index = InvertedIndex(index_path)
        index.load()

        entry_id = entry.get("id")
        keywords = _entry_keywords(entry)
        if not entry_id or not keywords:
            return

        # 先从旧桶移除
        index.remove_entry(entry_id, keywords)
        # 再在新桶添加
        index.add_entry(entry_id, keywords, to_bucket)
        index.save()
    except Exception:
        pass


def _find_entry_global(entry_id: str) -> tuple[Path, int, dict[str, Any]] | None:
    """全局查找条目（扫描所有桶）

    Returns:
        (kb_path, index, entry) 或 None
    """
    base = kb_base_dir()
    if not base.exists():
        return None

    for kb_file in base.rglob("kb.jsonl"):
        entries = read_jsonl(kb_file)
        for i, e in enumerate(entries):
            if e.get("_deleted") or e.get("_archived"):
                continue
            if e.get("id") == entry_id:
                return (kb_file, i, e)
            # 尝试计算 ID 匹配
            computed = generate_entry_id(e.get("ts", ""), e.get("title", ""))
            if computed == entry_id:
                return (kb_file, i, e)

    return None


def _find_entry_local(kb_path: Path, entry_id: str) -> int | None:
    """在指定桶中查找条目

    Returns:
        index 或 None
    """
    entries = read_jsonl(kb_path)
    for i, e in enumerate(entries):
        if e.get("_deleted") or e.get("_archived"):
            continue
        if e.get("id") == entry_id:
            return i
        computed = generate_entry_id(e.get("ts", ""), e.get("title", ""))
        if computed == entry_id:
            return i
    return None


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="迁移 KB 条目到正确的项目桶（只执行迁移，不做判断）"
    )
    parser.add_argument("entry_id", help="条目 ID（8字符 hex）")
    parser.add_argument("--to-repo", required=True, help="目标项目名（如 example-project-a）")
    parser.add_argument("--to-branch", required=True, help="目标分支路径（如 example-branch-a/dev-gch）")
    parser.add_argument(
        "--confidence",
        type=float,
        default=0.9,
        help="AI 判断置信度（0-1，默认 0.9）",
    )
    parser.add_argument(
        "--reason",
        default="AI判断错位",
        help="迁移原因（如：标题和tags均为example-project-a项目）",
    )
    parser.add_argument(
        "--from-repo",
        default="",
        help="源项目名（可选，不指定则全局查找）",
    )
    parser.add_argument(
        "--from-branch",
        default="",
        help="源分支路径（可选，不指定则全局查找）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="试运行模式，只显示操作不实际执行",
    )
    args = parser.parse_args(argv)

    # 校验参数
    if args.confidence < 0 or args.confidence > 1:
        sys.stderr.write("错误：--confidence 必须在 0-1 之间\n")
        return 2

    # 1. 查找源条目
    if args.from_repo and args.from_branch:
        # 指定了源桶，直接查找
        base = kb_base_dir()
        source_path = base / args.from_repo / args.from_branch / "kb.jsonl"
        if not source_path.exists():
            sys.stderr.write(f"错误：源桶不存在：{source_path}\n")
            return 2
        source_idx = _find_entry_local(source_path, args.entry_id)
        if source_idx is None:
            sys.stderr.write(f"错误：条目未找到：{args.entry_id}\n")
            return 2
        entries = read_jsonl(source_path)
        entry = entries[source_idx]
    else:
        # 全局查找
        result = _find_entry_global(args.entry_id)
        if result is None:
            sys.stderr.write(f"错误：条目未找到（全局查找）：{args.entry_id}\n")
            return 2
        source_path, source_idx, entry = result
        entries = read_jsonl(source_path)

    # 检查是否为用户明确指定位置
    metadata = entry.setdefault("metadata", {})
    if metadata.get("location_source") == "user_specified":
        sys.stderr.write(
            f"错误：条目为用户明确指定位置，不允许自动迁移：{args.entry_id}\n"
        )
        sys.stderr.write(f"  标题：{entry.get('title', '')}\n")
        sys.stderr.write(f"  当前位置：{entry.get('repo', '')}/{entry.get('branch', '')}\n")
        return 2

    # 2. 构建目标路径
    base = kb_base_dir()
    target_path = base / args.to_repo / args.to_branch / "kb.jsonl"

    # 记录原位置
    from_repo = entry.get("repo", "")
    from_branch = entry.get("branch", "")
    from_location = f"{from_repo}/{from_branch}" if from_repo else str(source_path.parent.relative_to(base))

    # 3. 更新条目元数据
    entry["repo"] = args.to_repo
    entry["branch"] = args.to_branch
    metadata.update({
        "location_source": "ai_migrated",
        "migrated_from": from_location,
        "migrated_at": now_iso(),
        "migration_confidence": args.confidence,
        "migration_reason": args.reason,
        "verified": False,
        "used_count_after_migration": 0,
        "heat_penalty": 0.95,  # 降低 5% 热值
    })
    entry["metadata"] = metadata

    # 4. 试运行模式：只显示不执行
    if args.dry_run:
        sys.stdout.write(f"[DRY RUN] 将迁移条目：{entry.get('title', '')}\n")
        sys.stdout.write(f"  ID：{args.entry_id}\n")
        sys.stdout.write(f"  从：{from_location}\n")
        sys.stdout.write(f"  到：{args.to_repo}/{args.to_branch}\n")
        sys.stdout.write(f"  置信度：{args.confidence:.2%}\n")
        sys.stdout.write(f"  原因：{args.reason}\n")
        sys.stdout.write(f"  热值惩罚：0.95\n")
        return 0

    # 5. 写入目标桶
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_entries = read_jsonl(target_path)
    target_entries.append(entry)
    _rewrite_jsonl(target_path, target_entries)

    # 6. 源桶软删除
    entries[source_idx]["_deleted"] = True
    entries[source_idx]["deleted_ts"] = now_iso()
    entries[source_idx]["deletion_reason"] = f"已迁移到 {args.to_repo}/{args.to_branch}"
    _rewrite_jsonl(source_path, entries)

    # 6.5 同步倒排索引：从旧桶移除，在新桶添加
    _migrate_inverted_index(entry, from_location, f"{args.to_repo}/{args.to_branch}")

    # 7. 重建索引
    # 目标桶索引
    target_ctx = resolve_context(
        cwd=Path.cwd(),
        repo_name_override=args.to_repo,
        branch_override=args.to_branch,
    )
    write_index(target_ctx)

    # 源桶索引（如果能解析）
    try:
        source_repo = from_repo or source_path.parent.parent.name
        source_branch_parts = []
        current = source_path.parent
        base_parts_count = len(base.parts)
        while len(current.parts) > base_parts_count + 1:
            source_branch_parts.insert(0, current.name)
            current = current.parent
        source_branch = "/".join(source_branch_parts) if source_branch_parts else "main"

        source_ctx = resolve_context(
            cwd=Path.cwd(),
            repo_name_override=source_repo,
            branch_override=source_branch,
        )
        write_index(source_ctx)
    except Exception:
        pass  # 索引失败不影响迁移

    # 8. 输出结果
    summary = {
        "status": "ok",
        "action": "migrated",
        "id": entry.get("id", args.entry_id),
        "title": entry.get("title", ""),
        "from": from_location,
        "to": f"{args.to_repo}/{args.to_branch}",
        "confidence": args.confidence,
        "reason": args.reason,
        "heat_penalty": 0.95,
        "source_path": str(source_path),
        "target_path": str(target_path),
    }
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")

    sys.stdout.write(f"\n✓ 迁移完成：{entry.get('title', '')}\n")
    sys.stdout.write(f"  {from_location} → {args.to_repo}/{args.to_branch}\n")
    sys.stdout.write(f"  热值惩罚：0.95（使用3次后恢复1.0）\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
