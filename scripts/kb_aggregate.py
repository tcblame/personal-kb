#!/usr/bin/env python3
"""
kb_aggregate.py - 生成关键词聚合视图

用法：
    kb_aggregate.py <keyword>                    # 为关键词生成聚合视图
    kb_aggregate.py <keyword> --force            # 强制重新生成（即使已存在）
    kb_aggregate.py <keyword> --dry-run          # 预览聚合结果，不保存

功能：
1. 通过倒排索引查找所有相关条目
2. 提取共性（共同 tags、技术栈、常见问题）
3. 提取差异（各项目的特定配置）
4. 生成聚合视图并保存到 _meta/_aggregations/

设计约束：
- 聚合视图是延迟生成的缓存，可随时重建
- 聚合视图不替代局部记录，只是索引性质
- 记录 aggregated_entries 用于新鲜度检查
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
        print(f"关键词 '{keyword}' 没有匹配的条目", file=sys.stderr)
        return []

    # 获取关键词信息
    keyword_info = index.get_keyword_info(keyword)
    if keyword_info:
        print(f"关键词 '{keyword}' 统计：")
        print(f"  - 条目数：{len(entry_ids)}")
        print(f"  - 桶数：{len(keyword_info.get('buckets', []))}")
        print(f"  - 搜索次数：{keyword_info.get('search_count', 0)}")
        print()

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

    print(f"成功收集 {len(entries)} 个条目")
    return entries


def generate_aggregation(
    keyword: str,
    entries: List[Dict[str, Any]],
    force: bool = False,
    dry_run: bool = False
) -> bool:
    """
    生成聚合视图

    Args:
        keyword: 关键词
        entries: 条目列表
        force: 是否强制重新生成
        dry_run: 是否只预览不保存

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

    # 检查是否已存在
    if aggregator.exists(keyword) and not force:
        print(f"聚合视图已存在，使用 --force 强制重新生成")
        return False

    # 获取倒排索引信息（用于记录 search_count）
    metadata = {}
    if _INVERTED_INDEX_AVAILABLE and InvertedIndex and get_inverted_index_path:
        index_path = get_inverted_index_path(base_dir)
        index = InvertedIndex(index_path)
        index.load()
        keyword_info = index.get_keyword_info(keyword)
        if keyword_info:
            metadata["search_count"] = keyword_info.get("search_count", 0)

    # 创建聚合视图
    aggregation = aggregator.create_aggregation(keyword, entries, metadata)
    if not aggregation:
        print("生成聚合视图失败", file=sys.stderr)
        return False

    # 预览模式
    if dry_run:
        print("\n=== 聚合视图预览 ===")
        print(f"ID: {aggregation.get('id')}")
        print(f"关键词: {aggregation.get('keyword')}")
        print(f"标题: {aggregation.get('title')}")
        print(f"说明: {aggregation.get('story')}")
        print(f"\n项目列表 ({len(aggregation.get('projects', []))}):")
        for proj in aggregation.get("projects", []):
            print(f"  - {proj.get('name')} ({proj.get('bucket')})")
        print(f"\n共同标签: {', '.join(aggregation.get('common_tags', []))}")
        print(f"\n聚合条目数: {len(aggregation.get('aggregated_entries', []))}")
        print(f"创建时间: {aggregation.get('created_ts')}")
        return True

    # 保存聚合视图
    if aggregator.save_aggregation(aggregation):
        agg_path = aggregator.get_aggregation_path(keyword)
        print(f"\n✅ 聚合视图已保存：{agg_path}")
        print(f"   - 包含 {len(aggregation.get('projects', []))} 个项目")
        print(f"   - 共同标签：{', '.join(aggregation.get('common_tags', [])[:5])}")
        return True
    else:
        print("保存聚合视图失败", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="生成关键词聚合视图",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  kb_aggregate.py 督办                    # 生成"督办"聚合视图
  kb_aggregate.py 督办 --force            # 强制重新生成
  kb_aggregate.py 督办 --dry-run          # 预览不保存
        """
    )

    parser.add_argument("keyword", help="关键词")
    parser.add_argument("--force", action="store_true", help="强制重新生成（即使已存在）")
    parser.add_argument("--dry-run", action="store_true", help="预览聚合结果，不保存")

    args = parser.parse_args()

    # 检查依赖
    if not _INVERTED_INDEX_AVAILABLE:
        print("错误：倒排索引模块不可用，请先实施 P0", file=sys.stderr)
        return 1

    if not _BUCKET_AGGREGATOR_AVAILABLE:
        print("错误：桶聚合器模块不可用", file=sys.stderr)
        return 1

    # 收集条目
    print(f"正在收集关键词 '{args.keyword}' 的相关条目...")
    entries = collect_entries_by_keyword(args.keyword)
    if not entries:
        return 1

    # 生成聚合视图
    success = generate_aggregation(args.keyword, entries, args.force, args.dry_run)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
