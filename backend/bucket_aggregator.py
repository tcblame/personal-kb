"""
桶聚合器模块：AI 自动生成和管理聚合视图

设计原则：
- 聚合视图是延迟生成的缓存，可随时重建
- 聚合视图记录共性和差异，不替代局部记录
- 支持新鲜度检查，自动发现新增记录
- 聚合视图本身也有热值管理

核心功能：
1. 生成聚合视图：从多个桶中提取共性和差异
2. 刷新聚合视图：检测新增记录并更新
3. 检查新鲜度：判断聚合视图是否过期
4. 聚合视图索引：快速查找已有聚合
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


class BucketAggregator:
    """桶聚合器：生成和管理聚合视图"""

    def __init__(self, aggregation_dir: Path):
        """
        Args:
            aggregation_dir: 聚合视图目录（通常是 _meta/_aggregations）
        """
        self.aggregation_dir = aggregation_dir
        self.aggregation_dir.mkdir(parents=True, exist_ok=True)

    def get_aggregation_path(self, keyword: str) -> Path:
        """
        获取关键词对应的聚合视图文件路径

        Args:
            keyword: 关键词

        Returns:
            聚合视图文件路径
        """
        # 使用 keyword 作为文件名（小写，替换特殊字符）
        safe_keyword = keyword.lower().strip().replace("/", "_").replace("\\", "_")
        return self.aggregation_dir / f"{safe_keyword}.jsonl"

    def exists(self, keyword: str) -> bool:
        """
        检查关键词是否已有聚合视图

        Args:
            keyword: 关键词

        Returns:
            是否存在聚合视图
        """
        return self.get_aggregation_path(keyword).exists()

    def load_aggregation(self, keyword: str) -> Optional[Dict[str, Any]]:
        """
        加载聚合视图

        Args:
            keyword: 关键词

        Returns:
            聚合视图字典，如果不存在返回 None
        """
        agg_path = self.get_aggregation_path(keyword)
        if not agg_path.exists():
            return None

        try:
            with agg_path.open("r", encoding="utf-8") as f:
                # 读取最后一行（最新的聚合视图）
                lines = f.readlines()
                if not lines:
                    return None
                return json.loads(lines[-1])
        except (json.JSONDecodeError, OSError, IndexError):
            return None

    def save_aggregation(self, aggregation: Dict[str, Any]) -> bool:
        """
        保存聚合视图（追加模式，保留历史版本）

        Args:
            aggregation: 聚合视图字典

        Returns:
            是否保存成功
        """
        keyword = aggregation.get("keyword", "")
        if not keyword:
            return False

        try:
            agg_path = self.get_aggregation_path(keyword)
            with agg_path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(aggregation, ensure_ascii=False) + "\n")
            return True
        except (OSError, IOError):
            return False

    def create_aggregation(
        self,
        keyword: str,
        entries: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        创建聚合视图（核心聚合逻辑）

        Args:
            keyword: 关键词
            entries: 所有相关条目列表
            metadata: 额外元信息（如 search_count）

        Returns:
            聚合视图字典
        """
        if not entries:
            return {}

        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

        # 提取所有条目的基本信息
        entry_ids = [e.get("id", "") for e in entries if e.get("id")]
        buckets = list(set(
            f"{e.get('repo', '')}/{e.get('branch', '')}"
            for e in entries
            if e.get("repo") and e.get("branch")
        ))

        # 提取项目列表（去重）
        projects = []
        seen_buckets = set()
        for entry in entries:
            repo = entry.get("repo", "")
            branch = entry.get("branch", "")
            bucket = f"{repo}/{branch}" if repo and branch else ""

            if bucket and bucket not in seen_buckets:
                seen_buckets.add(bucket)
                projects.append({
                    "name": entry.get("title", ""),
                    "bucket": bucket,
                    "entry_id": entry.get("id", ""),
                    "repo": repo,
                    "branch": branch
                })

        # 提取所有 tags（统计频次）
        tag_counter: Dict[str, int] = {}
        for entry in entries:
            for tag in entry.get("tags", []):
                if isinstance(tag, str):
                    tag_counter[tag] = tag_counter.get(tag, 0) + 1

        # 共同 tags：出现在 >= 50% 的条目中
        threshold = len(entries) / 2
        common_tags = [tag for tag, count in tag_counter.items() if count >= threshold]
        common_tags.sort(key=lambda t: tag_counter[t], reverse=True)

        # 提取共同模式（需要 AI 进一步提取）
        # 这里只做基础统计，AI 可以在调用此方法后进一步丰富
        common_patterns = {
            "total_entries": len(entries),
            "total_buckets": len(buckets),
            "tag_distribution": dict(sorted(tag_counter.items(), key=lambda x: x[1], reverse=True)[:10])
        }

        # 构建聚合视图（ID 使用微秒确保唯一性）
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:21]  # 精确到毫秒
        aggregation = {
            "id": f"aggregation_{keyword.lower().replace(' ', '_')}_{timestamp}",
            "keyword": keyword,
            "type": "aggregation",
            "title": f"{keyword} 聚合视图",
            "story": f"包含 {len(entries)} 个相关条目，分布在 {len(buckets)} 个桶中。",
            "projects": projects,
            "common_tags": common_tags,
            "common_patterns": common_patterns,
            "aggregated_entries": entry_ids,
            "created_ts": now,
            "search_triggered_count": metadata.get("search_count", 0) if metadata else 0,
            "version": 1
        }

        return aggregation

    def check_freshness(
        self,
        keyword: str,
        all_current_entries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        检查聚合视图的新鲜度

        Args:
            keyword: 关键词
            all_current_entries: 当前所有匹配的条目列表

        Returns:
            新鲜度检查结果：
            {
                "is_fresh": bool,
                "aggregation": Dict,
                "new_entries": List[Dict],
                "missing_entries": List[str]
            }
        """
        aggregation = self.load_aggregation(keyword)
        if not aggregation:
            return {
                "is_fresh": False,
                "aggregation": None,
                "new_entries": all_current_entries,
                "missing_entries": []
            }

        # 提取聚合视图中已包含的条目 ID
        aggregated_ids = set(aggregation.get("aggregated_entries", []))
        current_ids = {e.get("id", "") for e in all_current_entries if e.get("id")}

        # 新增条目：在当前列表中但不在聚合视图中
        new_entry_ids = current_ids - aggregated_ids
        new_entries = [e for e in all_current_entries if e.get("id") in new_entry_ids]

        # 缺失条目：在聚合视图中但当前已不存在
        missing_entry_ids = aggregated_ids - current_ids

        is_fresh = len(new_entries) == 0 and len(missing_entry_ids) == 0

        return {
            "is_fresh": is_fresh,
            "aggregation": aggregation,
            "new_entries": new_entries,
            "missing_entries": list(missing_entry_ids)
        }

    def refresh_aggregation(
        self,
        keyword: str,
        all_current_entries: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        刷新聚合视图（重新生成）

        Args:
            keyword: 关键词
            all_current_entries: 当前所有匹配的条目列表
            metadata: 额外元信息

        Returns:
            新的聚合视图，如果失败返回 None
        """
        # 重新生成聚合视图
        new_aggregation = self.create_aggregation(keyword, all_current_entries, metadata)
        if not new_aggregation:
            return None

        # 保存（追加模式）
        if self.save_aggregation(new_aggregation):
            return new_aggregation
        return None

    def list_aggregations(self) -> List[Dict[str, Any]]:
        """
        列出所有聚合视图的元信息

        Returns:
            聚合视图元信息列表
        """
        aggregations = []

        if not self.aggregation_dir.exists():
            return []

        for agg_file in self.aggregation_dir.glob("*.jsonl"):
            try:
                with agg_file.open("r", encoding="utf-8") as f:
                    lines = f.readlines()
                    if not lines:
                        continue

                    # 读取最后一行（最新版本）
                    latest = json.loads(lines[-1])
                    aggregations.append({
                        "keyword": latest.get("keyword", ""),
                        "file": agg_file.name,
                        "total_entries": len(latest.get("aggregated_entries", [])),
                        "total_buckets": latest.get("common_patterns", {}).get("total_buckets", 0),
                        "created_ts": latest.get("created_ts", ""),
                        "version": latest.get("version", 1),
                        "search_count": latest.get("search_triggered_count", 0)
                    })
            except (json.JSONDecodeError, OSError, IndexError):
                continue

        # 按搜索次数降序排序
        aggregations.sort(key=lambda x: x.get("search_count", 0), reverse=True)
        return aggregations


def get_bucket_aggregator_path(kb_base_dir: Path) -> Path:
    """
    获取聚合视图目录路径

    Args:
        kb_base_dir: KB 基础目录（通常是 ~/.codex/personal-kb/repos）

    Returns:
        聚合视图目录路径
    """
    try:
        from kb_lib import cache_dir_for_records

        cache_base = cache_dir_for_records(kb_base_dir)
    except (ImportError, ValueError):
        cache_base = kb_base_dir.parent / "_meta"
    return cache_base / "_aggregations"
