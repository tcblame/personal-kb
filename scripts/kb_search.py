from __future__ import annotations

import argparse
import heapq
import io
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import kb_adoption

# Windows UTF-8 输出修复
if sys.platform == 'win32' and hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

from kb_kinds import VALID_KINDS, parse_kind_filter, parse_legacy_type_filter
from kb_lib import (
    JsonlSafetyError,
    cache_dir_for_records,
    ensure_jsonl_safe,
    expand_query,
    expand_query_variants,
    global_bucket_dir,
    kb_base_dir,
    load_config,
    load_synonyms,
    read_jsonl,
    resolve_context,
    MIXED_TYPES,
    PURE_GENERIC_TYPES,
    PURE_PROJECT_TYPES,
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

# 尝试导入时间索引模块（可选依赖）
try:
    import importlib.util

    _kb_ti_path = Path(__file__).parent.parent / "backend" / "time_index.py"
    if _kb_ti_path.exists():
        _ti_spec = importlib.util.spec_from_file_location("time_index", _kb_ti_path)
        if _ti_spec and _ti_spec.loader:
            _ti_module = importlib.util.module_from_spec(_ti_spec)
            _ti_spec.loader.exec_module(_ti_module)
            parse_recent_param = _ti_module.parse_recent_param
            TIME_INDEX_AVAILABLE = True
        else:
            parse_recent_param = None
            TIME_INDEX_AVAILABLE = False
    else:
        parse_recent_param = None
        TIME_INDEX_AVAILABLE = False
except Exception:
    parse_recent_param = None
    TIME_INDEX_AVAILABLE = False

# 尝试导入聚合增强器（可选依赖）
try:
    import importlib.util

    _kb_agg_path = Path(__file__).parent.parent / "backend" / "aggregation_enhancer.py"
    if _kb_agg_path.exists():
        _agg_spec = importlib.util.spec_from_file_location("aggregation_enhancer", _kb_agg_path)
        if _agg_spec and _agg_spec.loader:
            _agg_module = importlib.util.module_from_spec(_agg_spec)
            _agg_spec.loader.exec_module(_agg_module)
            inject_aggregation_view = _agg_module.inject_aggregation_view
            AGGREGATION_ENHANCER_AVAILABLE = True
        else:
            inject_aggregation_view = None
            AGGREGATION_ENHANCER_AVAILABLE = False
    else:
        inject_aggregation_view = None
        AGGREGATION_ENHANCER_AVAILABLE = False
except Exception:
    inject_aggregation_view = None
    AGGREGATION_ENHANCER_AVAILABLE = False


def _parse_tags(value: str) -> list[str]:
    tags: list[str] = []
    for t in (value or "").split(","):
        s = t.strip()
        if s:
            tags.append(s)
    return tags


def _relevance_score(entry: dict[str, Any], query_original: str, expanded_terms: list[str] | None = None) -> float:
    """
    计算条目相关性分数（分数越高越相关）

    权重设计（面向 RAG 自动调用场景）：
    - title/aliases/trigger_terms 是强召回字段
    - tags/source_paths/key_files 是中等召回字段
    - story 是弱召回字段
    - superseded/archived 记录降权，避免旧设计或旧结论继续霸榜

    P0: 搜索默认只读，相关性不再使用 call_count 抬高排序。
    """
    score = 0.0
    q_lower = query_original.lower()
    terms = expanded_terms if expanded_terms else [q_lower]
    terms = [t.lower() for t in terms if isinstance(t, str) and t.strip()]
    terms = list(dict.fromkeys([q_lower, *terms]))

    title = entry.get("title", "").lower()
    tags = entry.get("tags", [])
    story = entry.get("story", "").lower()
    aliases = entry.get("aliases", [])
    trigger_terms = entry.get("trigger_terms", [])
    source_paths = entry.get("source_paths", [])
    key_files = entry.get("key_files", [])

    # 标题匹配（权重最高）
    if title == q_lower:
        score += 30  # 完全匹配
    elif q_lower in title:
        score += 18  # 包含完整查询词

    def list_match_score(values: Any, weight: float, cap: float) -> float:
        if not isinstance(values, list):
            return 0.0
        text = " ".join(v.lower() for v in values if isinstance(v, str))
        if not text:
            return 0.0
        return min(cap, sum(weight for term in terms if term and term in text))

    def text_match_score(text: str, weight: float, cap: float) -> float:
        if not text:
            return 0.0
        return min(cap, sum(weight for term in terms if term and term in text))

    # 多关键词命中要累加。RAG 查询常是 "项目 + 概念 + 问题"，
    # 只给第一个命中的同义词加分会让旧高热记录压过新权威记录。
    score += text_match_score(title, 6, 24)
    score += list_match_score(aliases, 5, 20)
    score += list_match_score(trigger_terms, 5, 20)

    # tags 匹配（权重次高）
    score += list_match_score(tags, 4, 16)
    score += list_match_score(source_paths, 3, 12)
    score += list_match_score(key_files, 3, 12)

    # story 匹配（权重最低）
    if q_lower in story:
        score += 4
    score += text_match_score(story, 1, 8)

    status = entry.get("status")
    if isinstance(status, str) and status.lower() in {"superseded", "archived", "obsolete"}:
        score -= 12
    if entry.get("superseded_by"):
        score -= 8

    return score


def _ts_key(entry: dict[str, Any]) -> float:
    ts = entry.get("ts")
    if not isinstance(ts, str) or not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return 0.0


def _used_count_key(entry: dict[str, Any]) -> float:
    """读取记录的使用热值（used_count），用于排序。

    P0 红线：这里只读，不写。搜索本身不给记录加热；加热只发生在
    `kb_update.py use`。本函数把已积累的 used_count 用于排序，让用过更多次
    的记录在相关性相同时排前面，不突破相关性门槛。

    迁移记录带 heat_penalty（<1.0）时按比例折算，未确认迁移的记录排序略低。
    """
    try:
        raw = kb_adoption.effective_usage(entry, _adoption_stats())
    except (OSError, ValueError):
        raw = entry.get("used_count", 0)
    try:
        used = float(raw)
    except (TypeError, ValueError):
        used = 0.0
    penalty = entry.get("heat_penalty", 1.0)
    try:
        penalty = float(penalty)
    except (TypeError, ValueError):
        penalty = 1.0
    return used * penalty


_ADOPTION_STATS_CACHE: tuple[Path, int, int, dict[str, dict[str, Any]]] | None = None


def _adoption_stats() -> dict[str, dict[str, Any]]:
    """Load runtime heat once per adoption-log version; never write during search."""
    global _ADOPTION_STATS_CACHE
    base = kb_base_dir()
    path = kb_adoption.adoption_events_path(base)
    try:
        stat = path.stat()
        version = (stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        version = (0, 0)
    cached = _ADOPTION_STATS_CACHE
    if cached and cached[0] == path and cached[1:3] == version:
        return cached[3]
    stats = kb_adoption.load_adoption_stats(base)
    _ADOPTION_STATS_CACHE = (path, version[0], version[1], stats)
    return stats


def _recency_boost(entry: dict[str, Any]) -> float:
    """给最近写入的记录一个微小的热值加成，让新记录在 used_count 接近时略微靠前。

    设计（轻微方案）：
    - 上限 +0.5，30 天内线性衰减到 0，超过 30 天无加成。
    - 加法叠加到 used_count（而非乘法），保证 used_count=0 的新记录也能拿到加成。
    - 加成上限 0.5 远小于真实热值，绝不会让没用过的新记录盖过高热值旧记录；
      只在两条记录 used_count 极接近时，让较新的那条排前面。

    用 POSIX 时间戳比较（_ts_key 已折算为绝对时间戳），避免 ts 带时区导致的相减异常。
    """
    ts = _ts_key(entry)
    if ts <= 0.0:
        return 0.0
    age_days = (datetime.now().timestamp() - ts) / 86400.0
    if age_days <= 0.0:
        return 0.5
    if age_days >= 30.0:
        return 0.0
    return 0.5 * (1.0 - age_days / 30.0)


def _read_decision_baseline(agg_file: Path) -> int | None:
    """读取上次聚合/skip 决策时的基线条目数。

    返回 None 表示从未做过决策（文件不存在或无有效记录）。
    兼容两种记录：
    - AI 写的语义聚合记录：取 aggregated_entries 长度
    - AI 写的 skip 记录：取 entry_count_at_decision
    - 代码生成的旧聚合：取 common_patterns.total_entries
    取文件最后一条有效记录为准。
    """
    if not agg_file.exists():
        return None
    baseline = None
    try:
        with agg_file.open("r", encoding="utf-8") as f:
            for raw in f:
                s = raw.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if "entry_count_at_decision" in obj:
                    baseline = obj.get("entry_count_at_decision")
                elif isinstance(obj.get("aggregated_entries"), list):
                    baseline = len(obj["aggregated_entries"])
                elif isinstance(obj.get("common_patterns"), dict):
                    baseline = obj["common_patterns"].get("total_entries")
    except OSError:
        return None
    return baseline if isinstance(baseline, int) else None


def _entry_contains_term(entry: dict[str, Any], term: str) -> bool:
    """检查条目是否包含指定词（用于 AND 模式）"""
    q = term.lower()

    for k in ("title", "story", "kind", "repo", "branch"):
        v = entry.get(k)
        if isinstance(v, str) and q in v.lower():
            return True

    tags = entry.get("tags")
    if isinstance(tags, list) and any(isinstance(t, str) and q in t.lower() for t in tags):
        return True

    aliases = entry.get("aliases")
    if isinstance(aliases, list) and any(isinstance(a, str) and q in a.lower() for a in aliases):
        return True

    for list_key in ("trigger_terms", "source_paths", "key_files"):
        values = entry.get(list_key)
        if isinstance(values, list) and any(isinstance(v, str) and q in v.lower() for v in values):
            return True

    key_facts = entry.get("key_facts")
    if isinstance(key_facts, list) and any(isinstance(f, str) and q in f.lower() for f in key_facts):
        return True

    term_field = entry.get("term")
    if isinstance(term_field, str) and q in term_field.lower():
        return True

    definition = entry.get("definition")
    if isinstance(definition, str) and q in definition.lower():
        return True

    return False


def _matches_query(entry: dict[str, Any], query: str, expanded_terms: list[str] | None = None, match_mode: str = "any", query_words_groups: list[list[str]] | None = None) -> bool:
    """
    检查条目是否匹配查询

    match_mode:
    - "any": OR 逻辑，匹配任意一个词即可（默认）
    - "all": AND 逻辑，必须匹配所有查询词（每个词至少匹配其同义词之一）

    query_words_groups: 用于 AND 模式的分组展开
      例如 "Redis 登录" → [["redis", "缓存", "cache"], ["登录"]]
      要求每组至少匹配一个词
    """
    if not query and not expanded_terms:
        return True

    terms = expanded_terms if expanded_terms else [query.lower()]

    if match_mode == "all" and query_words_groups:
        # AND 逻辑：每个查询词组至少匹配一个词
        for group in query_words_groups:
            if not any(_entry_contains_term(entry, q) for q in group):
                return False  # 有一个词组完全不匹配，返回 False
        return True
    elif match_mode == "all":
        # AND 逻辑（无分组）：所有词都必须匹配
        return all(_entry_contains_term(entry, q) for q in terms)
    else:
        # ANY 逻辑：匹配任意一个词即可（原逻辑）
        return any(_entry_contains_term(entry, q) for q in terms)


def _matches_tags(entry: dict[str, Any], required_tags: list[str]) -> bool:
    if not required_tags:
        return True
    tags = entry.get("tags")
    if not isinstance(tags, list):
        return False
    present = {t for t in tags if isinstance(t, str)}
    return any(t in present for t in required_tags)


def _iter_jsonl(path: Path) -> Any:
    if not path.exists():
        return
    ensure_jsonl_safe(path)
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


def _search_once(
    query: str,
    *,
    args: Any,
    allowed_kinds: set[str] | None,
    required_tags: list[str],
    recent_cutoff: datetime | None,
    should_expand: bool,
    synonyms: dict[str, Any],
    limit: int,
) -> tuple[list[dict[str, Any]], Any]:
    """对单个 query 执行一次检索，返回 (filtered 结果列表, ctx)。

    封装查询展开 + 全局/本地搜索 + 去重 + 排序。供单 query 与 --group 批量分组复用。
    ctx 仅在非全局搜索时有值（用于参考 repo / 日志），全局搜索时为 None。
    """
    # 智能查询展开：生成多个查询变体
    if should_expand and query:
        query_variants = expand_query_variants(query)
    else:
        query_variants = [query] if query else []

    all_expanded_terms: list[str] = []
    query_words_groups: list[list[str]] | None = None

    for variant in query_variants:
        if not variant:
            continue
        if " " in variant:
            query_words = variant.split()
            variant_groups = [expand_query(w, synonyms) for w in query_words]
            if query_words_groups is None:
                query_words_groups = variant_groups
            for terms in variant_groups:
                for t in terms:
                    if t not in all_expanded_terms:
                        all_expanded_terms.append(t)
        else:
            variant_terms = expand_query(variant, synonyms)
            for t in variant_terms:
                if t not in all_expanded_terms:
                    all_expanded_terms.append(t)

    expanded_terms = all_expanded_terms if all_expanded_terms else None
    ctx = None

    if args.global_search:
        base = kb_base_dir()
        if not base.exists():
            sys.stderr.write(f"kb base dir not found: {base}\n")
            return [], None

        sources: list[tuple[str, str]] = []
        if args.search_in in ("kb", "all"):
            sources.append(("kb", "kb.jsonl"))
        if args.search_in in ("summary", "all"):
            sources.append(("summary", "summary.jsonl"))

        all_matches: list[dict[str, Any]] = []

        def find_all_kb_files(root: Path) -> list[tuple[Path, str]]:
            files = []
            for source_name, filename in sources:
                for kb_file in root.rglob(filename):
                    files.append((kb_file, source_name))
            return sorted(files, key=lambda x: str(x[0]))

        for kb_file, source_name in find_all_kb_files(base):
            branch_dir = kb_file.parent
            for e in _iter_jsonl(kb_file):
                if e.get("_deleted") or e.get("_archived"):
                    continue
                if allowed_kinds and e.get("kind") not in allowed_kinds:
                    continue
                if not _matches_tags(e, required_tags):
                    continue
                if not _matches_query(e, query, expanded_terms, args.match_mode, query_words_groups):
                    continue
                if recent_cutoff:
                    entry_ts = _ts_key(e)
                    if entry_ts == 0.0 or datetime.fromtimestamp(entry_ts) < recent_cutoff:
                        continue
                e["source"] = source_name
                e["bucket_path"] = str(branch_dir)
                all_matches.append(e)

        seen_ids: dict[str, dict[str, Any]] = {}
        for e in all_matches:
            entry_id = e.get("id")
            if not entry_id:
                continue
            if entry_id not in seen_ids:
                seen_ids[entry_id] = e
            elif _ts_key(e) > _ts_key(seen_ids[entry_id]):
                seen_ids[entry_id] = e

        deduped_matches = list(seen_ids.values())
        if query:
            deduped_matches.sort(
                key=lambda e: (_relevance_score(e, query, expanded_terms), _used_count_key(e) + _recency_boost(e), _ts_key(e)),
                reverse=True,
            )
        else:
            deduped_matches.sort(key=_ts_key, reverse=True)
        return deduped_matches[:limit], None

    ctx = resolve_context(
        cwd=Path.cwd(),
        repo_name_override=(args.repo.strip() or None),
        branch_override=(args.branch.strip() or None),
        task_hint=query,
        operation="search",
    )

    sources_local: list[tuple[str, Path]] = []

    # 多仓库自动搜索：桶不存在时搜当前目录名下所有桶（父目录桶 + 所有子仓库桶）
    if not ctx.kb_path.exists():
        base = kb_base_dir()
        parent_kb_dir = base / Path.cwd().name
        if parent_kb_dir.exists():
            for kb_file in parent_kb_dir.rglob("kb.jsonl"):
                sources_local.append(("parent", kb_file))
    else:
        if args.search_in in ("kb", "all"):
            sources_local.append(("kb", ctx.kb_path))
        if args.search_in in ("summary", "all"):
            sources_local.append(("summary", ctx.summary_path))

    global_dir = global_bucket_dir()
    if global_dir.exists():
        gkb = global_dir / "kb.jsonl"
        if gkb.exists():
            sources_local.append(("global", gkb))

    all_entries: list[dict[str, Any]] = []
    for source_name, p in sources_local:
        entries = read_jsonl(p)
        for e in entries:
            e["source"] = source_name
        all_entries.extend(entries)

    pre_filtered = []
    for e in all_entries:
        if e.get("_deleted") or e.get("_archived"):
            continue
        if allowed_kinds and e.get("kind") not in allowed_kinds:
            continue
        if not _matches_tags(e, required_tags):
            continue
        if not _matches_query(e, query, expanded_terms, args.match_mode, query_words_groups):
            continue
        if recent_cutoff:
            entry_ts = _ts_key(e)
            if entry_ts == 0.0 or datetime.fromtimestamp(entry_ts) < recent_cutoff:
                continue
        pre_filtered.append(e)

    seen_ids = {}
    for e in pre_filtered:
        entry_id = e.get("id")
        if not entry_id:
            continue
        if entry_id not in seen_ids:
            seen_ids[entry_id] = e
        elif _ts_key(e) > _ts_key(seen_ids[entry_id]):
            seen_ids[entry_id] = e

    # ---- 索引驱动的智能跨项目扩展 ----
    # 本地结果不足时，利用倒排索引定位相关桶，精准补充跨项目记录
    if query and len(seen_ids) < limit and InvertedIndex and get_inverted_index_path:
        try:
            base = kb_base_dir()
            index_path = get_inverted_index_path(base)
            index = InvertedIndex(index_path)
            index.load()

            # 从查询词中找到倒排索引命中的桶路径
            query_keywords = [w.lower() for w in query.split() if w.strip()]
            # 也用展开后的 terms 查询索引
            if expanded_terms:
                for t in expanded_terms[:10]:
                    if t not in query_keywords:
                        query_keywords.append(t)

            # 收集倒排索引指向的桶路径
            candidate_buckets: set[str] = set()
            for kw in query_keywords:
                info = index.get_keyword_info(kw)
                if info:
                    for bucket in info.get("buckets", []):
                        candidate_buckets.add(bucket)

            # 排除已搜索的桶（当前项目桶 + 全局桶）
            already_searched: set[str] = set()
            if ctx:
                already_searched.add(f"{ctx.repo_name}/{ctx.branch}")
                already_searched.add(str(ctx.branch_path))
            already_searched.add("_global/_shared")

            # 定位对应的 kb.jsonl 文件
            extra_kb_files: list[Path] = []
            for bucket in candidate_buckets:
                if bucket in already_searched:
                    continue
                # bucket 格式: "repo/branch" 或 "parent/child/branch"
                bucket_path = base / bucket.replace("/", os.sep)
                kb_file = bucket_path / "kb.jsonl"
                if kb_file.exists():
                    extra_kb_files.append(kb_file)

            # 限制最多扩展 8 个桶，避免性能问题
            extra_kb_files = extra_kb_files[:8]

            if extra_kb_files:
                sys.stderr.write(
                    f"ℹ️  本地命中不足，从倒排索引扩展 {len(extra_kb_files)} 个相关桶\n"
                )
                for kb_file in extra_kb_files:
                    for e in _iter_jsonl(kb_file):
                        if e.get("_deleted") or e.get("_archived"):
                            continue
                        if allowed_kinds and e.get("kind") not in allowed_kinds:
                            continue
                        if not _matches_tags(e, required_tags):
                            continue
                        if not _matches_query(e, query, expanded_terms, args.match_mode, query_words_groups):
                            continue
                        if recent_cutoff:
                            entry_ts = _ts_key(e)
                            if entry_ts == 0.0 or datetime.fromtimestamp(entry_ts) < recent_cutoff:
                                continue
                        e["source"] = "index_expanded"
                        entry_id = e.get("id")
                        if entry_id and entry_id not in seen_ids:
                            seen_ids[entry_id] = e
        except JsonlSafetyError:
            raise
        except Exception:
            pass  # 索引扩展失败不影响正常搜索

    filtered = list(seen_ids.values())
    if query:
        filtered.sort(
            key=lambda e: (_relevance_score(e, query, expanded_terms), _used_count_key(e) + _recency_boost(e), _ts_key(e)),
            reverse=True,
        )
    else:
        filtered.sort(key=_ts_key, reverse=True)
    return filtered[:limit], ctx


def _track_search_in_index(query: str, *, show_candidates: bool = False) -> None:
    """显式记录关键词 search_count（聚合候选信号）+ 聚合候选提示。

    注意：这里写的是关键词热度 search_count（_meta 索引），不是记录的 used_count。
    普通搜索必须保持只读；只有 AI 显式传入 --track-search 时才调用本函数。
    """
    if not (InvertedIndex and get_inverted_index_path and query):
        return
    try:
        base = kb_base_dir()
        index_path = get_inverted_index_path(base)
        index = InvertedIndex(index_path)
        index.load()
        query_keywords = [w.lower() for w in query.split() if w.strip()]
        if not query_keywords:
            return
        index.update_search_count(query_keywords)
        index.save()

        if not show_candidates:
            return

        agg_dir = cache_dir_for_records(base) / "_aggregations"
        for kw in query_keywords:
            info = index.get_keyword_info(kw)
            if not info:
                continue
            sc = info.get("search_count", 0)
            ec = len(info.get("entry_ids", []))
            bc = len(info.get("buckets", []))
            if not (sc >= 5 and ec >= 3 and bc >= 2):
                continue
            safe_kw = kw.replace("/", "_").replace("\\", "_")
            agg_file = agg_dir / f"{safe_kw}.jsonl"
            baseline = _read_decision_baseline(agg_file)
            if baseline is not None and ec - baseline < 3:
                continue
            sys.stderr.write(
                f"\n[聚合候选] \"{kw}\" 搜索{sc}次 / {ec}条 / 跨{bc}桶"
                + (f"（上次决策时 {baseline} 条，已新增 {ec - baseline} 条）" if baseline is not None else "，无聚合视图")
                + "。AI 可判断是否执行语义归纳并写入 KB。\n"
            )
    except Exception:
        pass


def _main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description=(
            "Search personal-kb entries (default: current repo/branch bucket). "
            "Use --global to scan all buckets under kb_base_dir()."
        )
    )
    parser.add_argument("query", nargs="?", default="", help="Search keyword (optional)")
    parser.add_argument("--repo", default="", help="Override repo bucket (optional)")
    parser.add_argument("--branch", default="", help="Override branch bucket (optional)")
    parser.add_argument(
        "--kind",
        dest="kind_filter",
        default="",
        help=f"Filter by entry kind, comma-separated. Valid kinds: {','.join(sorted(VALID_KINDS))}",
    )
    parser.add_argument(
        "--type",
        dest="legacy_type_filter",
        default="",
        help="Deprecated alias for --kind. Legacy fine-grained types are mapped to the 6-kind model.",
    )
    parser.add_argument("--tags", default="", help="Comma-separated tags filter (optional, match ANY)")
    parser.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    parser.add_argument(
        "--in",
        dest="search_in",
        choices=["kb", "summary", "all"],
        default="all",
        help="Search in kb.jsonl, summary.jsonl or both (default: all)",
    )
    parser.add_argument(
        "--global",
        dest="global_search",
        action="store_true",
        help="Scan all repo/branch buckets under kb_base_dir() (ignores --repo/--branch).",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON array")
    parser.add_argument(
        "--group",
        action="append",
        dest="groups",
        metavar="NAME=QUERY1,QUERY2",
        help="批量分组检索：可重复多次。格式 '组名=词1,词2'。组内 OR，每组独立 limit。与单 query 互斥。",
    )
    parser.add_argument(
        "--match-mode",
        choices=["any", "all"],
        default="any",
        help="any=匹配任意关键词(OR), all=匹配所有关键词(AND). Default: any",
    )
    parser.add_argument(
        "--expand",
        action="store_true",
        help="启用智能查询展开（生成多个查询变体）",
    )
    parser.add_argument(
        "--cleanup-mode",
        choices=["ai", "off"],
        help="查询时清理模式（覆盖配置文件）：ai=AI自动判断, off=不清理",
    )
    parser.add_argument(
        "--log-search",
        action="store_true",
        help="Opt in to append a search log entry. Default search is read-only.",
    )
    parser.add_argument(
        "--track-search",
        action="store_true",
        help="Opt in to update keyword search_count in the inverted index. Default search is read-only.",
    )
    parser.add_argument(
        "--recent",
        default="",
        help="时间过滤：仅返回最近 N 天/周/月的条目。格式：7、7d、2w、1m",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="重建倒排索引后再搜索（修复索引与数据不一致）",
    )
    parser.add_argument(
        "--show-aggregation-candidates",
        action="store_true",
        help="Show aggregation candidate hints. Default search keeps this maintenance signal quiet.",
    )
    parser.add_argument(
        "--include-aggregation-view",
        action="store_true",
        help="Include aggregation view entries in search results. Default keeps search output focused on concrete KB entries.",
    )
    args = parser.parse_args(argv)

    required_tags = _parse_tags(args.tags)
    limit = max(0, args.limit)
    try:
        kind_filter = args.kind_filter
        if args.legacy_type_filter.strip():
            legacy_kinds = parse_legacy_type_filter(args.legacy_type_filter)
            if legacy_kinds:
                sys.stderr.write(
                    "⚠️  --type 已废弃，已映射为 --kind "
                    + ",".join(legacy_kinds)
                    + "；新命令请改用 --kind。\n"
                )
            if kind_filter.strip():
                explicit_kinds = parse_kind_filter(kind_filter)
                allowed_kinds = list(dict.fromkeys([*explicit_kinds, *legacy_kinds]))
            else:
                allowed_kinds = legacy_kinds
        else:
            allowed_kinds = parse_kind_filter(kind_filter)
    except ValueError as e:
        sys.stderr.write(f"错误：{e}\n")
        return 2

    # 倒排索引维护必须显式触发。普通搜索不能因为索引过期而写 _meta/_index。
    if InvertedIndex and get_inverted_index_path:
        base = kb_base_dir()
        index_path = get_inverted_index_path(base)
        need_rebuild = args.rebuild_index

        if need_rebuild:
            index = InvertedIndex(index_path)
            index.load()
            old_count = len(index._index)

            all_entries_for_rebuild = []
            for kb_file in base.rglob("kb.jsonl"):
                bucket = str(kb_file.parent.relative_to(base)).replace(os.sep, "/")
                for e in _iter_jsonl(kb_file):
                    if e.get("_deleted") or e.get("_archived"):
                        continue
                    if "repo" not in e:
                        parts = bucket.split("/")
                        e["repo"] = "/".join(parts[:-1]) if len(parts) >= 2 else bucket
                        e["branch"] = parts[-1] if len(parts) >= 2 else ""
                    all_entries_for_rebuild.append(e)

            # 保留旧 search_count
            old_search_counts = {
                kw: info.get("search_count", 0)
                for kw, info in index._index.items()
                if info.get("search_count", 0) > 0
            }
            old_last_searches = {
                kw: info.get("last_search")
                for kw, info in index._index.items()
                if info.get("last_search")
            }

            new_count = index.rebuild_from_entries(all_entries_for_rebuild)

            # 恢复 search_count
            for kw, sc in old_search_counts.items():
                if kw in index._index:
                    index._index[kw]["search_count"] = sc
                    if kw in old_last_searches:
                        index._index[kw]["last_search"] = old_last_searches[kw]

            index.save()
            sys.stderr.write(
                f"✓ 倒排索引已重建：{old_count} → {new_count} 关键词，"
                f"{len(all_entries_for_rebuild)} 条活跃条目\n"
            )

    # 解析时间过滤参数
    recent_cutoff = None
    if args.recent:
        if not TIME_INDEX_AVAILABLE or not parse_recent_param:
            sys.stderr.write("警告：--recent 参数需要时间索引模块支持，已忽略此参数\n")
        else:
            try:
                recent_days = parse_recent_param(args.recent)
                recent_cutoff = datetime.now() - timedelta(days=recent_days)
            except ValueError as e:
                sys.stderr.write(f"错误：无效的 --recent 参数格式：{e}\n")
                return 1

    # 加载配置
    config = load_config()

    # 判断是否启用查询展开
    should_expand = args.expand or config.get("search", {}).get("expand_queries", False)
    log_search = args.log_search

    synonyms = load_synonyms()

    # 批量分组检索模式
    if args.groups:
        group_results = []
        all_seen_ids: dict[str, list[str]] = {}  # id -> 命中的组名列表

        for group_spec in args.groups:
            if "=" not in group_spec:
                sys.stderr.write(f"错误：--group 格式应为 '组名=词1,词2'，收到：{group_spec}\n")
                return 2
            group_name, queries_str = group_spec.split("=", 1)
            group_name = group_name.strip()
            queries = [q.strip() for q in queries_str.split(",") if q.strip()]
            if not queries:
                sys.stderr.write(f"错误：组 '{group_name}' 无有效查询词\n")
                return 2

            # 组内 OR：合并多个词的结果
            combined_results = []
            seen_in_group: set[str] = set()
            for q in queries:
                results, _ = _search_once(
                    query=q,
                    args=args,
                    allowed_kinds=allowed_kinds,
                    required_tags=required_tags,
                    recent_cutoff=recent_cutoff,
                    should_expand=should_expand,
                    synonyms=synonyms,
                    limit=limit,
                )
                for e in results:
                    eid = e.get("id")
                    if eid and eid not in seen_in_group:
                        combined_results.append(e)
                        seen_in_group.add(eid)
                        # 跨组去重标注
                        if eid not in all_seen_ids:
                            all_seen_ids[eid] = []
                        all_seen_ids[eid].append(group_name)

            # 按组排序（相关性+热值+时间，用第一个 query 作参考）
            if queries and combined_results:
                combined_results.sort(
                    key=lambda e: (_relevance_score(e, queries[0], None), _used_count_key(e) + _recency_boost(e), _ts_key(e)),
                    reverse=True,
                )
            combined_results = combined_results[:limit]

            group_results.append({
                "group": group_name,
                "query": queries_str,
                "results": combined_results,
                "hit_count": len(combined_results),
            })

        # 标注跨组重复
        for gr in group_results:
            for e in gr["results"]:
                eid = e.get("id")
                if eid and len(all_seen_ids.get(eid, [])) > 1:
                    e["matched_groups"] = all_seen_ids[eid]

        # 输出
        if args.json:
            sys.stdout.write(json.dumps(group_results, ensure_ascii=False, indent=2))
            sys.stdout.write("\n")
        else:
            for gr in group_results:
                sys.stdout.write(f"\n{'='*60}\n")
                sys.stdout.write(f"【组：{gr['group']}】查询：{gr['query']}\n")
                sys.stdout.write(f"命中 {gr['hit_count']} 条\n")
                sys.stdout.write(f"{'='*60}\n\n")
                for i, e in enumerate(gr["results"], start=1):
                    _print_entry(i, e, show_repo=True)
                    matched_groups = e.get("matched_groups")
                    if matched_groups and len(matched_groups) > 1:
                        sys.stdout.write(f"    🔗 同时匹配组：{', '.join(matched_groups)}\n\n")
        return 0

    # 单 query 模式：调用 _search_once() 核心检索
    filtered, ctx = _search_once(
        query=args.query,
        args=args,
        allowed_kinds=allowed_kinds,
        required_tags=required_tags,
        recent_cutoff=recent_cutoff,
        should_expand=should_expand,
        synonyms=synonyms,
        limit=limit,
    )

    # P0: 默认搜索只读。日志也需要显式开启，避免搜索本身产生写入副作用。
    if log_search:
        log_dir = Path.home() / ".codex" / "personal-kb-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "kb_search.log"
        log_entry = {
            "ts": datetime.now().isoformat(),
            "query": args.query,
            "kind_filter": args.kind_filter,
            "global": args.global_search,
            "hits_count": len(filtered),
            "hit_ids": [e.get("id") for e in filtered[:10]],
            "repo": "global" if args.global_search else (ctx.repo_name if ctx else ""),
            "cwd": str(Path.cwd())
        }
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except:
            pass

    # 关键词热度统计会写 _meta/_index；默认关闭，确保普通搜索真只读。
    if args.track_search:
        _track_search_in_index(args.query, show_candidates=args.show_aggregation_candidates)

    # 聚合视图接入默认关闭：聚合条目很大，普通任务先看具体 KB 命中。
    if args.include_aggregation_view and AGGREGATION_ENHANCER_AVAILABLE and inject_aggregation_view and args.query:
        try:
            base = kb_base_dir()
            filtered = inject_aggregation_view(args.query, filtered, base)
        except Exception:
            pass  # 聚合注入失败不影响正常搜索

    if args.json:
        # JSON 输出模式：跨项目结果只返回 transferable 层
        # 全局搜索时，需要指定一个参考 repo 来判断是否跨项目
        reference_repo = ctx.repo_name if not args.global_search else None
        output = []
        for e in filtered:
            # 全局搜索时，每个条目都按跨项目处理（除非是纯通用类型）
            if args.global_search:
                formatted = _format_cross_project_entry(e, "__global_search__")
            else:
                formatted = _format_cross_project_entry(e, reference_repo)
            output.append(formatted)
        sys.stdout.write(json.dumps(output, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        return 0

    # 文本输出模式：分组展示当前项目和跨项目结果
    current_repo = None if args.global_search else ctx.repo_name
    current_project_results = []
    cross_project_results = []

    for e in filtered:
        e_repo = e.get("repo", "")
        if current_repo and e_repo == current_repo:
            current_project_results.append(e)
        else:
            cross_project_results.append(e)

    # 展示当前项目结果
    if current_repo:
        if current_project_results:
            sys.stdout.write(f"【当前项目 {current_repo}】\n")
            for i, e in enumerate(current_project_results, start=1):
                _print_entry(i, e, show_repo=False)
        else:
            sys.stdout.write(f"【当前项目 {current_repo}】\n")
            sys.stdout.write(f"  无相关记录\n\n")

        # 展示跨项目结果
        if cross_project_results:
            sys.stdout.write(f"【历史经验参考 - 来自其他项目】\n")
            sys.stdout.write(f"⚠️  以下是其他项目的经验，项目特定细节已隐藏，请在当前项目中验证\n\n")
            for i, e in enumerate(cross_project_results, start=1):
                formatted = _format_cross_project_entry(e, current_repo)
                _print_entry(i, formatted, show_repo=True, cross_project=True)
    else:
        # 全局搜索模式：直接展示所有结果
        for i, e in enumerate(filtered, start=1):
            _print_entry(i, e, show_repo=True)

    return 0


def main(argv: list[str]) -> int:
    try:
        return _main(argv)
    except JsonlSafetyError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 5


def _format_cross_project_entry(entry: dict[str, Any], current_repo: str | None) -> dict[str, Any]:
    """格式化跨项目条目：只返回 transferable 层 + 来源标注。

    Args:
        entry: 原始条目
        current_repo: 当前项目名称，或 "__global_search__" 表示全局搜索模式，或 None 表示同项目
    """
    entry_kind = entry.get("kind", "")
    entry_repo = entry.get("repo", "")

    # 纯通用类型：始终返回全部
    if entry_kind in PURE_GENERIC_TYPES:
        return entry

    # 全局搜索模式：所有混合型都按跨项目处理
    is_cross_project = (current_repo == "__global_search__") or (current_repo and entry_repo != current_repo)

    # 同项目：返回全部
    if not is_cross_project:
        return entry

    # map 类不脱密：map 是"业务词→项目坐标/目录/分支"的定位映射，
    # 跨项目时正需要完整坐标才有用，脱掉 transferable 外的字段就失去定位价值。
    # 返回全部 + 来源标注。
    if entry_kind == "map":
        return {
            **entry,
            "_from_project": entry_repo,
            "_cross_project": True,
            "_warning": "此映射来自其他项目，请确认是否适用于当前上下文"
        }

    # 混合型跨项目：只返回 transferable 层
    if entry_kind in MIXED_TYPES:
        transferable = entry.get("transferable", {})
        if not isinstance(transferable, dict):
            transferable = {}

        result = {
            "kind": entry_kind,
            "title": entry.get("title", ""),
            "tags": entry.get("tags", []),
            "ts": entry.get("ts", ""),
            "id": entry.get("id", ""),
            **transferable,
            "_from_project": entry_repo,
            "_cross_project": True,
            "_warning": "以上是通用经验，项目特定细节已隐藏"
        }

        # 保留来源坐标 repo/branch/bucket_path：它们是来源标识不是敏感内容，
        # 跨项目时让 AI 知道"这来自哪个项目/分支"，修复 --global --json 丢字段问题。
        for key in ("repo", "branch", "bucket_path"):
            if key in entry:
                result[key] = entry[key]

        return result

    # 纯项目类型：返回全部 + 标注
    return {
        **entry,
        "_from_project": entry_repo,
        "_cross_project": True,
        "_warning": "此记录来自其他项目，请注意适用性"
    }


def _print_entry(index: int, entry: dict[str, Any], show_repo: bool = False, cross_project: bool = False):
    """打印单条记录。"""
    ts = entry.get("ts", "")
    kind = entry.get("kind", "")
    title = entry.get("title", "")
    repo = entry.get("repo", "")
    branch = entry.get("branch", "")
    source = entry.get("source", "")
    tags = entry.get("tags", [])

    tags_str = ""
    if isinstance(tags, list):
        tags_str = ",".join([t for t in tags if isinstance(t, str)])

    # 标题行
    if show_repo:
        extra = []
        if isinstance(repo, str) and repo:
            extra.append(f"repo={repo}")
        if isinstance(branch, str) and branch:
            extra.append(f"branch={branch}")
        if isinstance(source, str) and source:
            extra.append(f"source={source}")
        extra_str = (" " + " ".join(extra)) if extra else ""
        sys.stdout.write(f"[{index}] {ts} {kind} {title}{extra_str}\n")
    else:
        sys.stdout.write(f"[{index}] {ts} {kind} {title}\n")

    # 标签
    if tags_str:
        sys.stdout.write(f"    tags: {tags_str}\n")

    # 跨项目警告
    if cross_project and entry.get("_cross_project"):
        warning = entry.get("_warning", "")
        if warning:
            sys.stdout.write(f"    ⚠️  {warning}\n")

    # 内容展示
    # map 跨项目时不脱密（返回全字段含 story），走常规展示；其余混合型展示 transferable 字段
    if entry.get("_cross_project") and kind in MIXED_TYPES and kind != "map":
        # 混合型跨项目结果：展示 transferable 字段
        for key in ["symptom", "root_cause", "solution_pattern", "design_pattern",
                    "purpose", "business_logic", "deployment_pattern"]:
            if key in entry and entry[key]:
                value = str(entry[key])
                if "\n" in value:
                    sys.stdout.write(f"    {key}:\n")
                    for line in value.split("\n"):
                        if line.strip():
                            sys.stdout.write(f"      {line}\n")
                else:
                    value_short = (value[:200] + "...") if len(value) > 200 else value
                    sys.stdout.write(f"    {key}: {value_short}\n")
    else:
        # 常规结果：展示 story
        story = entry.get("story", "")
        if isinstance(story, str):
            story_one = story.strip().replace("\r\n", "\n").split("\n", 1)[0]
            story_one = (story_one[:240] + "...") if len(story_one) > 240 else story_one
            if story_one:
                sys.stdout.write(f"    {story_one}\n")

    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
