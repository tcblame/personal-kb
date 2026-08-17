from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 动态导入倒排索引模块
try:
    import importlib.util

    _kb_backend_path = Path(__file__).parent.parent / "backend" / "inverted_index.py"
    if _kb_backend_path.exists():
        spec = importlib.util.spec_from_file_location("inverted_index", _kb_backend_path)
        if spec and spec.loader:
            _inverted_index_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_inverted_index_module)
            InvertedIndex = _inverted_index_module.InvertedIndex
            get_inverted_index_path = _inverted_index_module.get_inverted_index_path
            _INVERTED_INDEX_AVAILABLE = True
        else:
            _INVERTED_INDEX_AVAILABLE = False
    else:
        _INVERTED_INDEX_AVAILABLE = False
except Exception:
    _INVERTED_INDEX_AVAILABLE = False

from kb_lib import kb_base_dir, read_jsonl


def _iter_all_entries():
    """遍历所有 KB 条目"""
    base = kb_base_dir()
    if not base.exists():
        return

    for repo_dir in base.iterdir():
        if not repo_dir.is_dir():
            continue
        for branch_dir in repo_dir.iterdir():
            if not branch_dir.is_dir():
                continue
            kb_path = branch_dir / "kb.jsonl"
            if not kb_path.exists():
                continue

            for entry in read_jsonl(kb_path):
                if entry.get("_deleted"):
                    continue
                yield entry


def cmd_rebuild_index(args) -> int:
    """重建倒排索引"""
    if not _INVERTED_INDEX_AVAILABLE:
        sys.stderr.write("错误：倒排索引模块不可用\n")
        return 1

    base = kb_base_dir()
    index_path = get_inverted_index_path(base)

    sys.stdout.write(f"正在重建倒排索引: {index_path}\n")

    # 收集所有条目
    entries = list(_iter_all_entries())
    sys.stdout.write(f"已扫描 {len(entries)} 个条目\n")

    # 重建索引
    index = InvertedIndex(index_path)
    keyword_count = index.rebuild_from_entries(entries)

    # 保存索引
    if index.save():
        sys.stdout.write(f"索引重建完成: {keyword_count} 个关键词\n")
        return 0
    else:
        sys.stderr.write("保存索引失败\n")
        return 1


def cmd_view_index(args) -> int:
    """查看倒排索引统计"""
    if not _INVERTED_INDEX_AVAILABLE:
        sys.stderr.write("错误：倒排索引模块不可用\n")
        return 1

    base = kb_base_dir()
    index_path = get_inverted_index_path(base)

    if not index_path.exists():
        sys.stdout.write(f"索引文件不存在: {index_path}\n")
        sys.stdout.write("提示：运行 'kb_manage.py rebuild-index' 创建索引\n")
        return 0

    index = InvertedIndex(index_path)
    index.load()

    stats = index.get_stats()

    if args.json:
        sys.stdout.write(json.dumps(stats, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
    else:
        sys.stdout.write("=== 倒排索引统计 ===\n")
        sys.stdout.write(f"索引文件: {index_path}\n")
        sys.stdout.write(f"关键词总数: {stats['total_keywords']}\n")
        sys.stdout.write(f"条目总数: {stats['total_entries']}\n")
        sys.stdout.write(f"搜索总次数: {stats['total_searches']}\n")
        sys.stdout.write("\n热门关键词 Top 10:\n")
        for item in stats["hot_keywords"]:
            sys.stdout.write(f"  {item['keyword']}: {item['search_count']} 次\n")

    return 0


def cmd_check_aggregation(args) -> int:
    """检查聚合触发条件"""
    if not _INVERTED_INDEX_AVAILABLE:
        sys.stderr.write("错误：倒排索引模块不可用\n")
        return 1

    base = kb_base_dir()
    index_path = get_inverted_index_path(base)

    if not index_path.exists():
        sys.stdout.write("索引文件不存在，无法检查聚合触发条件\n")
        return 0

    index = InvertedIndex(index_path)
    index.load()

    candidates = index.get_aggregation_candidates(
        min_search_count=args.min_search_count,
        min_entries=args.min_entries,
        min_buckets=args.min_buckets
    )

    if args.json:
        sys.stdout.write(json.dumps(candidates, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
    else:
        if not candidates:
            sys.stdout.write("没有需要聚合的关键词\n")
        else:
            sys.stdout.write(f"需要聚合的关键词 ({len(candidates)}):\n")
            for item in candidates:
                kw = item["keyword"]
                search_count = item.get("search_count", 0)
                entry_count = len(item.get("entry_ids", []))
                bucket_count = len(item.get("buckets", []))
                sys.stdout.write(f"  {kw}: 搜索 {search_count} 次, {entry_count} 条目, {bucket_count} 桶\n")

    return 0


def cmd_keyword_info(args) -> int:
    """查看关键词详细信息"""
    if not _INVERTED_INDEX_AVAILABLE:
        sys.stderr.write("错误：倒排索引模块不可用\n")
        return 1

    if not args.keyword:
        sys.stderr.write("错误：必须提供关键词参数\n")
        return 1

    base = kb_base_dir()
    index_path = get_inverted_index_path(base)

    if not index_path.exists():
        sys.stdout.write("索引文件不存在\n")
        return 0

    index = InvertedIndex(index_path)
    index.load()

    info = index.get_keyword_info(args.keyword)

    if not info:
        sys.stdout.write(f"关键词 '{args.keyword}' 未在索引中找到\n")
        return 0

    if args.json:
        sys.stdout.write(json.dumps({args.keyword: info}, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(f"=== 关键词: {args.keyword} ===\n")
        sys.stdout.write(f"搜索次数: {info.get('search_count', 0)}\n")
        sys.stdout.write(f"最后搜索: {info.get('last_search', 'N/A')}\n")
        sys.stdout.write(f"条目数量: {len(info.get('entry_ids', []))}\n")
        sys.stdout.write(f"桶数量: {len(info.get('buckets', []))}\n")
        sys.stdout.write("\n条目 ID:\n")
        for eid in info.get("entry_ids", []):
            sys.stdout.write(f"  {eid}\n")
        sys.stdout.write("\n桶路径:\n")
        for bucket in info.get("buckets", []):
            sys.stdout.write(f"  {bucket}\n")

    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="KB 倒排索引管理工具：重建、查看、验证索引",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
子命令:
  rebuild-index       重建倒排索引（全量扫描）
  view-index          查看索引统计信息
  check-aggregation   检查需要聚合的关键词
  keyword-info        查看关键词详细信息

示例:
  # 重建索引
  python kb_manage.py rebuild-index

  # 查看索引统计
  python kb_manage.py view-index

  # 检查聚合触发条件
  python kb_manage.py check-aggregation --min-search-count 5

  # 查看关键词信息
  python kb_manage.py keyword-info 督办
"""
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # rebuild-index
    parser_rebuild = subparsers.add_parser("rebuild-index", help="重建倒排索引")

    # view-index
    parser_view = subparsers.add_parser("view-index", help="查看索引统计")
    parser_view.add_argument("--json", action="store_true", help="输出 JSON 格式")

    # check-aggregation
    parser_check = subparsers.add_parser("check-aggregation", help="检查聚合触发条件")
    parser_check.add_argument("--min-search-count", type=int, default=5, help="最小搜索次数 (default: 5)")
    parser_check.add_argument("--min-entries", type=int, default=3, help="最小条目数 (default: 3)")
    parser_check.add_argument("--min-buckets", type=int, default=2, help="最小桶数 (default: 2)")
    parser_check.add_argument("--json", action="store_true", help="输出 JSON 格式")

    # keyword-info
    parser_keyword = subparsers.add_parser("keyword-info", help="查看关键词详细信息")
    parser_keyword.add_argument("keyword", help="关键词")
    parser_keyword.add_argument("--json", action="store_true", help="输出 JSON 格式")

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "rebuild-index":
        return cmd_rebuild_index(args)
    elif args.command == "view-index":
        return cmd_view_index(args)
    elif args.command == "check-aggregation":
        return cmd_check_aggregation(args)
    elif args.command == "keyword-info":
        return cmd_keyword_info(args)
    else:
        sys.stderr.write(f"未知命令: {args.command}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
