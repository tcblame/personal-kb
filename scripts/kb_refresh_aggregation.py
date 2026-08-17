#!/usr/bin/env python3
"""
kb_refresh_aggregation.py - 刷新已有的聚合视图

用法：
    kb_refresh_aggregation.py <keyword>                 # 刷新聚合视图
    kb_refresh_aggregation.py <keyword> --check-only    # 仅检查新鲜度，不刷新
    kb_refresh_aggregation.py --all                     # 刷新所有聚合视图

功能：
1. 检查聚合视图的新鲜度（是否有新增/缺失条目）
2. 重新生成聚合视图（保留历史版本）
3. 批量刷新所有聚合视图

设计约束：
- 聚合视图采用追加模式，保留历史版本
- 刷新时重新计算共性和差异
- 支持检查模式，只查看新鲜度不刷新
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

# 动态导入 kb_lib
sys.path.insert(0, str(Path(__file__).parent))
from kb_lib import kb_base_dir, read_jsonl

# 动态导入 inverted_index
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
    InvertedIndex = None
    get_inverted_index_path = None

# 动态导入 bucket_aggregator
try:
    import importlib.util

    _kb_backend_path = Path(__file__).parent.parent / "backend" / "bucket_aggregator.py"
    if _kb_backend_path.exists():
        spec = importlib.util.spec_from_file_location("bucket_aggregator", _kb_backend_path)
        if spec and spec.loader:
            _bucket_aggregator_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_bucket_aggregator_module)
            BucketAggregator = _bucket_aggregator_module.BucketAggregator
            get_bucket_aggregator_path = _bucket_aggregator_module.get_bucket_aggregator_path
            _BUCKET_AGGREGATOR_AVAILABLE = True
        else:
            _BUCKET_AGGREGATOR_AVAILABLE = False
    else:
        _BUCKET_AGGREGATOR_AVAILABLE = False
except Exception:
    _BUCKET_AGGREGATOR_AVAILABLE = False
    BucketAggregator = None
    get_bucket_aggregator_path = None


def collect_entries_by_keyword(keyword: str) -> List[Dict[str, Any]]:
    """
    通过倒排索引收集所有相关条目

    Args:
        keyword: 关键词

    Returns:
        条目列表
    """
    if not _INVERTED_INDEX_AVAILABLE or not InvertedIndex or not get_inverted_index_path:
        print("错误：倒排索引模块不可用", file=sys.stderr)
        return []

    # 加载倒排索引
    base_dir = kb_base_dir()
    index_path = get_inverted_index_path(base_dir)
    index = InvertedIndex(index_path)
    index.load()

    # 获取条目 ID 列表
    entry_ids = index.get_entries_by_keyword(keyword)
    if not entry_ids:
        return []

    # 收集所有条目内容
    entries = []
    entry_id_set = set(entry_ids)

    # 遍历所有桶
    for repo_dir in base_dir.iterdir():
        if not repo_dir.is_dir():
            continue

        for branch_dir in repo_dir.iterdir():
            if not branch_dir.is_dir():
                continue

            kb_file = branch_dir / "kb.jsonl"
            if not kb_file.exists():
                continue

            # 读取桶中的条目
            bucket_entries = read_jsonl(kb_file)
            for entry in bucket_entries:
                if entry.get("id") in entry_id_set:
                    entries.append(entry)

    return entries


def check_and_refresh_aggregation(keyword: str, check_only: bool = False) -> bool:
    """
    检查并刷新聚合视图

    Args:
        keyword: 关键词
        check_only: 是否只检查不刷新

    Returns:
        是否成功
    """
    if not _BUCKET_AGGREGATOR_AVAILABLE or not BucketAggregator or not get_bucket_aggregator_path:
        print("错误：桶聚合器模块不可用", file=sys.stderr)
        return False

    # 初始化聚合器
    base_dir = kb_base_dir()
    agg_dir = get_bucket_aggregator_path(base_dir)
    aggregator = BucketAggregator(agg_dir)

    # 检查聚合视图是否存在
    if not aggregator.exists(keyword):
        print(f"聚合视图不存在：{keyword}，请先运行 kb_aggregate.py")
        return False

    # 收集当前所有条目
    current_entries = collect_entries_by_keyword(keyword)

    # 检查新鲜度
    freshness = aggregator.check_freshness(keyword, current_entries)

    print(f"\n=== 聚合视图新鲜度检查：{keyword} ===")
    if freshness["is_fresh"]:
        print("✅ 聚合视图是最新的，无需刷新")
        return True
    else:
        print("⚠️  聚合视图已过期")
        new_entries = freshness.get("new_entries", [])
        missing_entries = freshness.get("missing_entries", [])

        if new_entries:
            print(f"   - 新增条目：{len(new_entries)} 个")
            for entry in new_entries[:5]:  # 只显示前 5 个
                print(f"     • {entry.get('title', '')} ({entry.get('id', '')})")
            if len(new_entries) > 5:
                print(f"     ... 还有 {len(new_entries) - 5} 个")

        if missing_entries:
            print(f"   - 缺失条目：{len(missing_entries)} 个")
            for entry_id in missing_entries[:5]:
                print(f"     • {entry_id}")
            if len(missing_entries) > 5:
                print(f"     ... 还有 {len(missing_entries) - 5} 个")

    # 仅检查模式
    if check_only:
        print("\n提示：使用不带 --check-only 参数运行以刷新聚合视图")
        return True

    # 刷新聚合视图
    print("\n正在刷新聚合视图...")

    # 获取倒排索引信息（用于记录 search_count）
    metadata = {}
    if _INVERTED_INDEX_AVAILABLE and InvertedIndex and get_inverted_index_path:
        index_path = get_inverted_index_path(base_dir)
        index = InvertedIndex(index_path)
        index.load()
        keyword_info = index.get_keyword_info(keyword)
        if keyword_info:
            metadata["search_count"] = keyword_info.get("search_count", 0)

    # 刷新聚合视图
    new_aggregation = aggregator.refresh_aggregation(keyword, current_entries, metadata)
    if new_aggregation:
        print(f"✅ 聚合视图已刷新")
        print(f"   - 包含 {len(new_aggregation.get('projects', []))} 个项目")
        print(f"   - 聚合条目数：{len(new_aggregation.get('aggregated_entries', []))}")
        return True
    else:
        print("刷新聚合视图失败", file=sys.stderr)
        return False


def refresh_all_aggregations(check_only: bool = False) -> bool:
    """
    刷新所有聚合视图

    Args:
        check_only: 是否只检查不刷新

    Returns:
        是否全部成功
    """
    if not _BUCKET_AGGREGATOR_AVAILABLE or not BucketAggregator or not get_bucket_aggregator_path:
        print("错误：桶聚合器模块不可用", file=sys.stderr)
        return False

    # 初始化聚合器
    base_dir = kb_base_dir()
    agg_dir = get_bucket_aggregator_path(base_dir)
    aggregator = BucketAggregator(agg_dir)

    # 列出所有聚合视图
    aggregations = aggregator.list_aggregations()
    if not aggregations:
        print("没有找到任何聚合视图")
        return True

    print(f"找到 {len(aggregations)} 个聚合视图\n")

    success_count = 0
    for agg in aggregations:
        keyword = agg.get("keyword", "")
        if not keyword:
            continue

        print(f"处理聚合视图：{keyword}")
        if check_and_refresh_aggregation(keyword, check_only):
            success_count += 1

    print(f"\n总结：{success_count}/{len(aggregations)} 个聚合视图处理成功")
    return success_count == len(aggregations)


def main():
    parser = argparse.ArgumentParser(
        description="刷新聚合视图",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  kb_refresh_aggregation.py 督办                  # 刷新"督办"聚合视图
  kb_refresh_aggregation.py 督办 --check-only     # 仅检查新鲜度
  kb_refresh_aggregation.py --all                 # 刷新所有聚合视图
        """
    )

    parser.add_argument("keyword", nargs="?", help="关键词（不指定时使用 --all）")
    parser.add_argument("--check-only", action="store_true", help="仅检查新鲜度，不刷新")
    parser.add_argument("--all", action="store_true", help="刷新所有聚合视图")

    args = parser.parse_args()

    # 检查依赖
    if not _INVERTED_INDEX_AVAILABLE:
        print("错误：倒排索引模块不可用，请先实施 P0", file=sys.stderr)
        return 1

    if not _BUCKET_AGGREGATOR_AVAILABLE:
        print("错误：桶聚合器模块不可用", file=sys.stderr)
        return 1

    # 刷新所有聚合视图
    if args.all:
        success = refresh_all_aggregations(args.check_only)
        return 0 if success else 1

    # 刷新单个聚合视图
    if not args.keyword:
        print("错误：请指定关键词或使用 --all", file=sys.stderr)
        parser.print_help()
        return 1

    success = check_and_refresh_aggregation(args.keyword, args.check_only)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
