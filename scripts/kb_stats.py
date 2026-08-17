#!/usr/bin/env python3
"""personal-kb 使用统计"""
import io
import json
import sys
from pathlib import Path
from collections import Counter
from datetime import datetime, timedelta

# Windows UTF-8 输出修复
if sys.platform == 'win32' and hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

def main():
    log_dir = Path.home() / ".codex" / "personal-kb-logs"

    # 读取搜索日志
    search_log = log_dir / "kb_search.log"
    searches = []
    if search_log.exists():
        with open(search_log, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        searches.append(json.loads(line))
                    except:
                        pass

    # 读取写入日志
    add_log = log_dir / "kb_add.log"
    adds = []
    if add_log.exists():
        with open(add_log, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        adds.append(json.loads(line))
                    except:
                        pass

    print("=" * 60)
    print("personal-kb 使用统计")
    print("=" * 60)
    print()

    # 搜索统计
    print(f"总搜索次数: {len(searches)}")
    if searches:
        hit_count = sum(1 for s in searches if s.get("hits_count", 0) > 0)
        miss_count = len(searches) - hit_count
        print(f"  命中: {hit_count} ({hit_count*100//len(searches) if len(searches) > 0 else 0}%)")
        print(f"  未命中: {miss_count} ({miss_count*100//len(searches) if len(searches) > 0 else 0}%)")
        print()

        # 高频查询词
        queries = [s.get("query", "") for s in searches if s.get("query")]
        if queries:
            print("高频查询词 (Top 10):")
            for q, cnt in Counter(queries).most_common(10):
                print(f"  {cnt:3d}x  {q}")
            print()

        # 按 kind 分布
        kinds = [s.get("kind_filter", "") for s in searches if s.get("kind_filter")]
        if kinds:
            print("搜索 kind 分布:")
            for kind, cnt in Counter(kinds).most_common():
                print(f"  {cnt:3d}x  {kind}")
            print()

        # 按 repo 分布
        repos = [s.get("repo", "") for s in searches]
        print("搜索 repo 分布:")
        for r, cnt in Counter(repos).most_common():
            print(f"  {cnt:3d}x  {r}")
        print()

    # 写入统计
    print(f"总写入次数: {len(adds)}")
    if adds:
        # 按 kind 分布
        kinds = [a.get("kind", "") for a in adds]
        print("写入 kind 分布:")
        for kind, cnt in Counter(kinds).most_common():
            print(f"  {cnt:3d}x  {kind}")
        print()

        # 按 repo 分布
        repos = [a.get("repo", "") for a in adds]
        print("写入 repo 分布:")
        for r, cnt in Counter(repos).most_common():
            print(f"  {cnt:3d}x  {r}")
        print()

        # aliases 覆盖率
        with_aliases = sum(1 for a in adds if a.get("aliases"))
        print(f"aliases 覆盖率: {with_aliases}/{len(adds)} ({with_aliases*100//len(adds) if len(adds) > 0 else 0}%)")

        # key_files 覆盖率
        with_key_files = sum(1 for a in adds if a.get("key_files"))
        print(f"key_files 覆盖率: {with_key_files}/{len(adds)} ({with_key_files*100//len(adds) if len(adds) > 0 else 0}%)")
        print()

    # 时间趋势（最近7天）
    if searches or adds:
        print("最近活动:")
        now = datetime.now()
        for days_ago in range(6, -1, -1):
            target_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            target_date = target_date - timedelta(days=days_ago)
            date_str = target_date.strftime("%Y-%m-%d")

            search_count = sum(1 for s in searches if s.get("ts", "").startswith(date_str))
            add_count = sum(1 for a in adds if a.get("ts", "").startswith(date_str))

            if search_count > 0 or add_count > 0:
                print(f"  {date_str}: 搜索 {search_count:2d}次, 写入 {add_count:2d}次")
        print()

    print("=" * 60)
    print(f"日志位置: {log_dir}")
    print("=" * 60)

if __name__ == "__main__":
    main()
