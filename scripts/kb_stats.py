#!/usr/bin/env python3
"""personal-kb 使用统计：当前 closeout 与旧日志分开报告。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import kb_audit_runtime_value
from kb_lib import JsonlSafetyError, PersonalKbConfigError, read_jsonl, storage_layout


def build_stats(*, last_days: int = 30, legacy_log_dir: Path | None = None) -> dict[str, Any]:
    layout = storage_layout()
    report = {
        "root": str(layout.root),
        "runtime": kb_audit_runtime_value.build_report(
            path=layout.runtime / "closeout.jsonl", last_days=last_days,
        ),
    }
    if legacy_log_dir is not None:
        report["legacy_logs"] = {
            "log_dir": str(legacy_log_dir),
            "scope": "legacy_all_time_unfiltered",
            "search_log_rows": len(read_jsonl(legacy_log_dir / "kb_search.log")),
            "add_log_rows": len(read_jsonl(legacy_log_dir / "kb_add.log")),
        }
    return report


def _print_text(report: dict[str, Any]) -> None:
    runtime = report["runtime"]
    print(f"personal-kb 使用统计（最近 {runtime['last_days']} 天，排除测试事件）")
    print(f"数据根: {report['root']}")
    print(f"运行记录: {runtime['closeout_path']}")
    print(f"已记录的收尾: {runtime['closeout_total']}")
    print(f"  有命中: {runtime['hit_closeouts']}；无命中: {runtime['no_hit_closeouts']}")
    print(f"  自报采用: {runtime['used_hit_closeouts']}；确认采用: {runtime['adopted_hit_closeouts']}")
    adoption = f"{runtime['confirmed_adoption_rate']:.2%}" if runtime["hit_closeouts"] else "n/a"
    print(f"  确认采用率（确认采用 / 有命中的收尾）: {adoption}")
    print(f"  有新增记录的收尾: {runtime['written_closeouts']}；有更新记录的收尾: {runtime['updated_closeouts']}")
    print(f"  Top-3 质量样本: {runtime['top3_quality_rows']} 次收尾 / {runtime['top3_slot_count']} 个结果位")
    missing = runtime["missing_closeout_rate"]
    missing_text = "n/a（缺少显式分母）" if missing is None else f"{missing:.2%}"
    print(f"  漏收尾率: {missing_text}")
    print("口径: closeout 不是搜索次数，未记录的调用不可由此推算；采用不等于最终任务收益。")
    legacy = report.get("legacy_logs")
    if legacy is not None:
        print(f"Legacy 旧日志（全时段、未过滤测试、不计入当前统计）: {legacy['log_dir']}")
        print(f"  搜索日志行: {legacy['search_log_rows']}；新增日志行: {legacy['add_log_rows']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--last-days", type=int, default=30, help="Report closeouts from the last N days")
    parser.add_argument("--json", action="store_true", help="Print structured runtime statistics")
    parser.add_argument("--legacy-log-dir", type=Path, help="Optionally report old logs separately (all time, unfiltered)")
    args = parser.parse_args(argv)
    if args.last_days < 1:
        parser.error("--last-days must be at least 1")
    try:
        report = build_stats(
            last_days=args.last_days,
            legacy_log_dir=args.legacy_log_dir.expanduser() if args.legacy_log_dir is not None else None,
        )
    except (JsonlSafetyError, PersonalKbConfigError, OSError) as exc:
        sys.stderr.write(f"kb_stats: {exc}\n")
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
