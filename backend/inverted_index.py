"""
倒排索引模块：AI 自维护的关键词 -> 条目 ID 映射

设计原则：
- 数据源：从 KB 条目中提取 tags、aliases、trigger_terms 作为索引关键词
- 索引结构：JSON 文件存储，格式 {keyword: {entry_ids, buckets, search_count, last_search}}
- 查询能力：O(1) 关键词查询、支持统计搜索频次、支持聚合触发检测
- 维护方式：普通 RAG 搜索只读；只有 AI 显式维护动作才更新 search_count 或 entry_ids

实现约束：
- 索引文件可随时删除重建，不影响原始 KB 数据
- 支持增量更新，不需要每次全量扫描
- 索引损坏时自动降级为全量扫描
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set, Optional


class InvertedIndex:
    """倒排索引：关键词 -> 条目 ID 列表的映射"""

    def __init__(self, index_path: Path):
        """
        Args:
            index_path: 索引文件路径（通常是 _meta/_index/keywords.json）
        """
        self.index_path = index_path
        # 索引结构：{keyword: {entry_ids, buckets, search_count, last_search}}
        self._index: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    def load(self) -> bool:
        """
        加载索引文件

        Returns:
            是否加载成功
        """
        if not self.index_path.exists():
            self._index = {}
            self._loaded = True
            return False

        try:
            with self.index_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._index = data
                self._loaded = True
                return True
            else:
                self._index = {}
                self._loaded = True
                return False
        except (json.JSONDecodeError, OSError):
            self._index = {}
            self._loaded = True
            return False

    def save(self) -> bool:
        """
        保存索引到文件

        Returns:
            是否保存成功
        """
        try:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            with self.index_path.open("w", encoding="utf-8", newline="\n") as f:
                json.dump(self._index, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
            return True
        except (OSError, IOError):
            return False

    def add_entry(self, entry_id: str, keywords: List[str], bucket: str) -> None:
        """
        为条目添加关键词索引

        Args:
            entry_id: 条目 ID
            keywords: 关键词列表（tags + aliases + trigger_terms）
            bucket: 桶路径（如 "example-project-a/example-branch-a"）
        """
        if not self._loaded:
            self.load()

        for kw in keywords:
            kw_lower = kw.strip().lower()
            if not kw_lower:
                continue

            if kw_lower not in self._index:
                self._index[kw_lower] = {
                    "entry_ids": [],
                    "buckets": [],
                    "search_count": 0,
                    "last_search": None
                }

            # 添加条目 ID（去重）
            if entry_id not in self._index[kw_lower]["entry_ids"]:
                self._index[kw_lower]["entry_ids"].append(entry_id)

            # 添加桶路径（去重）
            if bucket not in self._index[kw_lower]["buckets"]:
                self._index[kw_lower]["buckets"].append(bucket)

    def remove_entry(self, entry_id: str, keywords: List[str]) -> None:
        """
        从索引中移除条目

        Args:
            entry_id: 条目 ID
            keywords: 关键词列表
        """
        if not self._loaded:
            self.load()

        for kw in keywords:
            kw_lower = kw.strip().lower()
            if kw_lower not in self._index:
                continue

            # 移除条目 ID
            if entry_id in self._index[kw_lower]["entry_ids"]:
                self._index[kw_lower]["entry_ids"].remove(entry_id)

            # 如果该关键词没有任何条目了，删除整个关键词
            if not self._index[kw_lower]["entry_ids"]:
                del self._index[kw_lower]

    def update_search_count(self, keywords: List[str]) -> None:
        """
        更新关键词的搜索计数

        Args:
            keywords: 搜索的关键词列表
        """
        if not self._loaded:
            self.load()

        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

        for kw in keywords:
            kw_lower = kw.strip().lower()
            if not kw_lower:
                continue

            if kw_lower not in self._index:
                # 首次搜索时创建条目
                self._index[kw_lower] = {
                    "entry_ids": [],
                    "buckets": [],
                    "search_count": 1,
                    "last_search": now
                }
            else:
                # 增加搜索计数
                self._index[kw_lower]["search_count"] = self._index[kw_lower].get("search_count", 0) + 1
                self._index[kw_lower]["last_search"] = now

    def get_entries_by_keyword(self, keyword: str) -> List[str]:
        """
        根据关键词获取条目 ID 列表

        Args:
            keyword: 关键词

        Returns:
            条目 ID 列表
        """
        if not self._loaded:
            self.load()

        kw_lower = keyword.strip().lower()
        if kw_lower not in self._index:
            return []

        return self._index[kw_lower].get("entry_ids", [])

    def get_keyword_info(self, keyword: str) -> Optional[Dict[str, Any]]:
        """
        获取关键词的完整信息

        Args:
            keyword: 关键词

        Returns:
            关键词信息字典，如果不存在返回 None
        """
        if not self._loaded:
            self.load()

        kw_lower = keyword.strip().lower()
        return self._index.get(kw_lower)

    def get_aggregation_candidates(
        self,
        min_search_count: int = 5,
        min_entries: int = 3,
        min_buckets: int = 2
    ) -> List[Dict[str, Any]]:
        """
        获取需要聚合的关键词候选列表

        Args:
            min_search_count: 最小搜索次数
            min_entries: 最小条目数
            min_buckets: 最小桶数

        Returns:
            候选关键词列表，每项包含 keyword 和完整的索引信息
        """
        if not self._loaded:
            self.load()

        candidates = []
        for keyword, info in self._index.items():
            search_count = info.get("search_count", 0)
            entry_count = len(info.get("entry_ids", []))
            bucket_count = len(info.get("buckets", []))

            if (search_count >= min_search_count and
                entry_count >= min_entries and
                bucket_count >= min_buckets):
                candidates.append({
                    "keyword": keyword,
                    **info
                })

        # 按搜索次数降序排序
        candidates.sort(key=lambda x: x.get("search_count", 0), reverse=True)
        return candidates

    def get_stats(self) -> Dict[str, Any]:
        """
        获取索引统计信息

        Returns:
            统计信息字典
        """
        if not self._loaded:
            self.load()

        total_keywords = len(self._index)
        total_entries = len(set(
            eid
            for info in self._index.values()
            for eid in info.get("entry_ids", [])
        ))
        total_searches = sum(info.get("search_count", 0) for info in self._index.values())

        # 热门关键词 Top 10
        hot_keywords = sorted(
            [
                (kw, info.get("search_count", 0))
                for kw, info in self._index.items()
            ],
            key=lambda x: x[1],
            reverse=True
        )[:10]

        return {
            "total_keywords": total_keywords,
            "total_entries": total_entries,
            "total_searches": total_searches,
            "hot_keywords": [{"keyword": kw, "search_count": count} for kw, count in hot_keywords]
        }

    def rebuild_from_entries(self, entries: List[Dict[str, Any]]) -> int:
        """
        从条目列表重建索引

        Args:
            entries: 条目列表

        Returns:
            索引的关键词数量
        """
        self._index = {}
        self._loaded = True

        for entry in entries:
            entry_id = entry.get("id", "")
            if not entry_id:
                continue

            repo = entry.get("repo", "")
            branch = entry.get("branch", "")
            bucket = f"{repo}/{branch}" if repo and branch else ""

            # 提取关键词
            keywords = []

            # tags
            tags = entry.get("tags", [])
            if isinstance(tags, list):
                keywords.extend([t for t in tags if isinstance(t, str)])

            # aliases
            aliases = entry.get("aliases", [])
            if isinstance(aliases, list):
                keywords.extend([a for a in aliases if isinstance(a, str)])

            # trigger_terms
            trigger_terms = entry.get("trigger_terms", [])
            if isinstance(trigger_terms, list):
                keywords.extend([t for t in trigger_terms if isinstance(t, str)])

            # 添加到索引
            if keywords:
                self.add_entry(entry_id, keywords, bucket)

        return len(self._index)


def get_inverted_index_path(kb_base_dir: Path) -> Path:
    """
    获取倒排索引文件路径

    Args:
        kb_base_dir: KB 基础目录（通常是 ~/.codex/personal-kb/repos）

    Returns:
        索引文件路径
    """
    try:
        from kb_lib import cache_dir_for_records

        cache_base = cache_dir_for_records(kb_base_dir)
    except (ImportError, ValueError):
        cache_base = kb_base_dir.parent / "_meta"
    return cache_base / "_index" / "keywords.json"
