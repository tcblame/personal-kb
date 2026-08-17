#!/usr/bin/env python3
"""
批量迁移 KB 条目到正确的项目桶

用法：
    kb_batch_migrate.py --from-repo <repo> --from-branch <branch> \\
        --to-repo <repo> --to-branch <branch> \\
        --filter-keywords <关键词> \\
        --confidence <0-1> --reason "<原因>" [--dry-run] [--limit <数量>]

示例 1：按关键词批量迁移
    kb_batch_migrate.py \\
        --from-repo example-project-a/example-branch-a --from-branch dev-gch \\
        --to-repo example-project-a/ubc-dms-server-standard --to-branch example-project-a-new-gch \\
        --filter-keywords "DMS,督办,project_dms" \\
        --confidence 0.9 --reason "督办相关内容迁移到督办桶"

示例 2：试运行模式（只显示不执行）
    kb_batch_migrate.py \\
        --from-repo example-project-a/example-branch-a --from-branch dev-gch \\
        --to-repo example-project-a/ubc-dms-server-standard --to-branch example-project-a-new-gch \\
        --filter-keywords "DMS" --dry-run

示例 3：限制迁移数量
    kb_batch_migrate.py \\
        --from-repo example-project-a/example-branch-a --from-branch dev-gch \\
        --to-repo example-project-a/ubc-dms-server-standard --to-branch example-project-a-new-gch \\
        --filter-keywords "DMS" --limit 10
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

from kb_lib import kb_base_dir, read_jsonl


def _match_keywords(entry: dict[str, Any], keywords: list[str]) -> bool:
    """检查条目是否匹配任一关键词"""
    title = entry.get("title", "").lower()
    story = entry.get("story", "").lower()
    tags = " ".join(entry.get("tags", [])).lower()
    aliases = " ".join(entry.get("aliases", [])).lower()

    text = f"{title} {story} {tags} {aliases}"

    return any(kw.lower() in text for kw in keywords)


def _exclude_keywords(entry: dict[str, Any], keywords: list[str]) -> bool:
    """检查条目是否包含排除关键词"""
    if not keywords:
        return False

    title = entry.get("title", "").lower()
    story = entry.get("story", "").lower()
    tags = " ".join(entry.get("tags", [])).lower()
    aliases = " ".join(entry.get("aliases", [])).lower()

    text = f"{title} {story} {tags} {aliases}"

    return any(kw.lower() in text for kw in keywords)


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="批量迁移 KB 条目到正确的项目桶"
    )
    parser.add_argument("--from-repo", required=True, help="源项目名（如 example-project-a/example-branch-a）")
    parser.add_argument("--from-branch", required=True, help="源分支名（如 dev-gch）")
    parser.add_argument("--to-repo", required=True, help="目标项目名（如 example-project-a/ubc-dms-server-standard）")
    parser.add_argument("--to-branch", required=True, help="目标分支名（如 example-project-a-new-gch）")
    parser.add_argument(
        "--filter-keywords",
        required=True,
        help="筛选关键词（逗号分隔，匹配 title/story/tags/aliases，任一命中即迁移）",
    )
    parser.add_argument(
        "--exclude-keywords",
        default="",
        help="排除关键词（逗号分隔，包含这些词的条目不迁移，用于避免误伤混合内容）",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=0.9,
        help="迁移置信度（0-1，默认 0.9）",
    )
    parser.add_argument(
        "--reason",
        default="批量迁移错位条目",
        help="迁移原因",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="试运行模式，只显示操作不实际执行",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="限制迁移数量（0 = 不限制）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过确认提示，直接执行",
    )
    args = parser.parse_args(argv)

    # 解析关键词
    filter_keywords = [k.strip() for k in args.filter_keywords.split(",") if k.strip()]
    exclude_keywords = [k.strip() for k in args.exclude_keywords.split(",") if k.strip()]

    if not filter_keywords:
        sys.stderr.write("错误：--filter-keywords 不能为空\n")
        return 2

    # 构建源桶路径
    base = kb_base_dir()
    source_path = base / args.from_repo / args.from_branch / "kb.jsonl"

    if not source_path.exists():
        sys.stderr.write(f"错误：源桶不存在：{source_path}\n")
        return 2

    # 读取源桶条目
    entries = read_jsonl(source_path)

    # 筛选符合条件的条目
    candidates = []
    for entry in entries:
        if entry.get("_deleted") or entry.get("_archived"):
            continue

        # 检查是否匹配筛选关键词
        if not _match_keywords(entry, filter_keywords):
            continue

        # 检查是否包含排除关键词
        if _exclude_keywords(entry, exclude_keywords):
            continue

        candidates.append(entry)

    if not candidates:
        print(f"未找到匹配的条目（关键词：{', '.join(filter_keywords)}）")
        return 0

    # 应用 limit
    if args.limit > 0 and len(candidates) > args.limit:
        candidates = candidates[:args.limit]

    # 显示待迁移条目
    print(f"=== 批量迁移计划 ===\n")
    print(f"源桶：{args.from_repo}/{args.from_branch}")
    print(f"目标桶：{args.to_repo}/{args.to_branch}")
    print(f"筛选关键词：{', '.join(filter_keywords)}")
    if exclude_keywords:
        print(f"排除关键词：{', '.join(exclude_keywords)}")
    print(f"置信度：{args.confidence}")
    print(f"原因：{args.reason}")
    print(f"模式：{'试运行' if args.dry_run else '实际执行'}")
    print(f"\n待迁移条目（共 {len(candidates)} 条）：\n")

    for i, entry in enumerate(candidates, 1):
        entry_id = entry.get("id", "?")[:8]
        title = entry.get("title", "")[:70]
        tags = ", ".join(entry.get("tags", [])[:3])
        print(f"{i:3d}. {entry_id}  {title}")
        if tags:
            print(f"     tags: {tags}")

    # 确认提示
    if not args.yes and not args.dry_run:
        print(f"\n确认迁移 {len(candidates)} 条记录？[y/N] ", end="", flush=True)
        try:
            response = input().strip().lower()
            if response not in ("y", "yes"):
                print("已取消")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\n已取消")
            return 0

    # 执行迁移
    print(f"\n{'[试运行] ' if args.dry_run else ''}开始迁移...\n")

    success_count = 0
    failed_count = 0

    migrate_script = Path(__file__).parent / "kb_migrate.py"

    for i, entry in enumerate(candidates, 1):
        entry_id = entry.get("id", "")
        title = entry.get("title", "")[:50]

        print(f"[{i}/{len(candidates)}] 迁移 {entry_id[:8]}: {title}...", end=" ", flush=True)

        if args.dry_run:
            print("✓ (试运行)")
            success_count += 1
            continue

        # 调用 kb_migrate.py
        cmd = [
            sys.executable,
            str(migrate_script),
            entry_id,
            "--from-repo", args.from_repo,
            "--from-branch", args.from_branch,
            "--to-repo", args.to_repo,
            "--to-branch", args.to_branch,
            "--confidence", str(args.confidence),
            "--reason", args.reason,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

            if result.returncode == 0:
                print("✓")
                success_count += 1
            else:
                print(f"✗ ({result.stderr.strip()[:50]})")
                failed_count += 1
        except Exception as e:
            print(f"✗ (异常: {e})")
            failed_count += 1

    # 总结
    print(f"\n=== 迁移完成 ===")
    print(f"成功：{success_count} 条")
    if failed_count > 0:
        print(f"失败：{failed_count} 条")

    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
