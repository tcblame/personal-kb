#!/usr/bin/env python3
"""
kb_schema_discover.py - KB 字段发现与软性建议脚本

用途：
1. 发现 KB 中已有记录实际使用的字段
2. 根据 kind 类型推荐合适的字段
3. 提供字段使用示例和最佳实践

不做的事：
- 不验证必填字段（由 kb_add.py 的 validate_entry_fields 负责）
- 不阻止写入（只提供建议）
- 不强制字段格式（只提示推荐格式）
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Windows UTF-8 输出修复
if sys.platform == 'win32' and hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

from kb_kinds import VALID_KINDS
from kb_lib import kb_base_dir, read_jsonl


# kind 推荐字段配置
KIND_RECOMMENDED_FIELDS = {
    "map": {
        "core": ["aliases", "trigger_terms", "term", "definition"],
        "optional": ["scope", "domain", "subject", "key_files", "related_entries"],
        "description": "映射/定位类记录：用于回答'这是什么、在哪、对应哪个项目/文件/分支'",
        "trigger_terms_hint": "配置键、接口路径、类名、文件路径、服务名、IP/端口",
        "examples": {
            "aliases": ["业务名", "英文缩写", "项目代号"],
            "trigger_terms": ["LoginService", "/api/v1/login", "ai.report.base-url"],
        }
    },
    "issue": {
        "core": ["aliases", "trigger_terms", "symptom", "root_cause", "solution"],
        "optional": ["issue_scope", "risk_level", "verification", "key_files", "retained_assets"],
        "description": "问题/故障类记录：bug修复、环境差异、配置问题、依赖问题",
        "trigger_terms_hint": "异常类名、错误码、日志关键字、配置键",
        "examples": {
            "aliases": ["GMOS换token超时", "登录接口500"],
            "trigger_terms": ["NullPointerException", "curl: (28) Connection timed out", "sql_require_primary_key"],
        }
    },
    "pitfall": {
        "core": ["aliases", "trigger_terms", "symptom", "root_cause", "solution_pattern"],
        "optional": ["risk_level", "scope", "domain"],
        "description": "踩坑/环境差异类记录：工具坑、编码问题、平台差异",
        "trigger_terms_hint": "工具命令、环境变量、特殊字符、平台标识",
        "examples": {
            "aliases": ["PowerShell Hashtable 陷阱", "Maven 本地仓库缓存"],
            "trigger_terms": ["$matches", "System.Collections.Hashtable", ".m2/repository"],
        }
    },
    "experience": {
        "core": ["aliases", "design_pattern", "solution_pattern"],
        "optional": ["domain", "scope", "key_facts", "related_entries"],
        "description": "经验/模式类记录：通用排查套路、开发最佳实践",
        "trigger_terms_hint": "框架/工具名、关键API、技术栈标识",
        "examples": {
            "aliases": ["Spring 事务失效排查", "Redis 缓存穿透"],
            "trigger_terms": ["@Transactional", "RedisTemplate", "Spring AOP"],
        }
    },
    "requirement": {
        "core": ["aliases", "purpose", "business_logic"],
        "optional": ["subject", "scope", "verification", "key_files", "related_entries"],
        "description": "需求/业务事实类记录：需求摘要、业务规则、验收标准",
        "trigger_terms_hint": "需求编号、页面名、接口名、业务术语",
        "examples": {
            "aliases": ["example supervision迁移", "LED大屏数据看板"],
            "trigger_terms": ["REQ-2024-001", "/supervision/migrate", "督办系统"],
        }
    },
    "implementation": {
        "core": ["aliases", "design_pattern", "purpose"],
        "optional": ["domain", "subject", "key_files", "related_entries"],
        "description": "实现/设计思路类记录：架构决策、模块边界、技术选型",
        "trigger_terms_hint": "类名、方法名、模块名、架构关键词",
        "examples": {
            "aliases": ["动态数据源切换", "多租户隔离方案"],
            "trigger_terms": ["DynamicDataSource", "AbstractRoutingDataSource", "TenantContext"],
        }
    },
}

def discover_fields_in_kb() -> dict[str, Any]:
    """扫描 KB 发现实际使用的字段分布"""
    base = kb_base_dir()
    if not base.exists():
        return {"error": "KB base dir not found"}

    all_fields = Counter()
    kind_fields = defaultdict(lambda: Counter())
    field_examples = defaultdict(list)

    # 递归查找所有 kb.jsonl
    for kb_file in base.rglob("kb.jsonl"):
        entries = read_jsonl(kb_file)
        for entry in entries:
            if entry.get("_deleted") or entry.get("_archived"):
                continue

            kind = entry.get("kind", "")
            if kind and kind not in VALID_KINDS:
                kind = ""

            for field, value in entry.items():
                if field.startswith("_"):
                    continue

                all_fields[field] += 1
                if kind:
                    kind_fields[kind][field] += 1

                # 收集字段示例（只保留前3个）
                if len(field_examples[field]) < 3 and value:
                    if isinstance(value, (str, int, float, bool)):
                        field_examples[field].append(value)
                    elif isinstance(value, list) and value:
                        field_examples[field].append(value[0] if len(value) == 1 else value[:2])

    return {
        "total_fields": dict(all_fields.most_common()),
        "by_kind": {k: dict(v.most_common(15)) for k, v in kind_fields.items()},
        "examples": {k: v for k, v in field_examples.items() if v},
    }


def suggest_fields_for_kind(kind: str, entry: dict[str, Any] | None = None) -> dict[str, Any]:
    """根据 kind 推荐字段，并检查当前条目缺失的推荐字段"""
    if kind not in KIND_RECOMMENDED_FIELDS:
        return {
            "error": f"Unknown kind: {kind}",
            "valid_kinds": list(KIND_RECOMMENDED_FIELDS.keys()),
        }

    config = KIND_RECOMMENDED_FIELDS[kind]
    result = {
        "kind": kind,
        "description": config["description"],
        "recommended_fields": {
            "core": config["core"],
            "optional": config["optional"],
        },
        "trigger_terms_hint": config["trigger_terms_hint"],
        "examples": config["examples"],
    }

    if entry:
        missing_core = [f for f in config["core"] if f not in entry or not entry[f]]
        result["missing_core"] = missing_core
        result["has_trigger_terms"] = bool(entry.get("trigger_terms"))

        # 检查 trigger_terms 质量
        if entry.get("trigger_terms"):
            terms = entry["trigger_terms"]
            if isinstance(terms, list):
                result["trigger_terms_count"] = len(terms)
                result["trigger_terms_preview"] = terms[:3]

    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Discover KB field usage and suggest fields for a given kind"
    )
    parser.add_argument(
        "action",
        choices=["discover", "suggest", "check"],
        help="Action: discover=扫描 KB 字段分布, suggest=推荐 kind 字段, check=检查条目字段",
    )
    parser.add_argument(
        "--kind",
        default="",
        help="Kind to suggest fields for (required for 'suggest' and 'check')",
    )
    parser.add_argument(
        "--entry-json",
        default="",
        help="Entry JSON to check (for 'check' action)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    args = parser.parse_args(argv)

    if args.action == "discover":
        result = discover_fields_in_kb()
        if args.json:
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        else:
            sys.stdout.write("=== KB 字段使用分布 ===\n\n")
            sys.stdout.write("所有字段出现次数:\n")
            for field, count in sorted(result["total_fields"].items(), key=lambda x: -x[1])[:20]:
                sys.stdout.write(f"  {field}: {count}\n")

            sys.stdout.write("\n各 kind 字段分布:\n")
            for kind, fields in sorted(result["by_kind"].items()):
                sys.stdout.write(f"\n  [{kind}]:\n")
                for field, count in list(fields.items())[:10]:
                    sys.stdout.write(f"    {field}: {count}\n")

    elif args.action == "suggest":
        if not args.kind:
            sys.stderr.write("Error: --kind is required for 'suggest' action\n")
            return 2

        result = suggest_fields_for_kind(args.kind)
        if "error" in result:
            sys.stderr.write(f"Error: {result['error']}\n")
            if "valid_kinds" in result:
                sys.stderr.write(f"Valid kinds: {', '.join(result['valid_kinds'])}\n")
            return 2

        if args.json:
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        else:
            sys.stdout.write(f"=== {result['kind']} 类记录字段建议 ===\n\n")
            sys.stdout.write(f"用途: {result['description']}\n\n")
            sys.stdout.write("核心字段 (推荐提供):\n")
            for field in result["recommended_fields"]["core"]:
                sys.stdout.write(f"  - {field}\n")
            sys.stdout.write("\n可选字段:\n")
            for field in result["recommended_fields"]["optional"]:
                sys.stdout.write(f"  - {field}\n")
            sys.stdout.write(f"\ntrigger_terms 建议: {result['trigger_terms_hint']}\n")
            sys.stdout.write("\n字段示例:\n")
            for field, examples in result["examples"].items():
                sys.stdout.write(f"  {field}: {json.dumps(examples, ensure_ascii=False)}\n")

    elif args.action == "check":
        if not args.kind:
            sys.stderr.write("Error: --kind is required for 'check' action\n")
            return 2

        entry = None
        if args.entry_json:
            try:
                entry = json.loads(args.entry_json)
            except json.JSONDecodeError as e:
                sys.stderr.write(f"Error parsing --entry-json: {e}\n")
                return 2

        result = suggest_fields_for_kind(args.kind, entry)
        if "error" in result:
            sys.stderr.write(f"Error: {result['error']}\n")
            return 2

        if args.json:
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        else:
            sys.stdout.write(f"=== {result['kind']} 类记录字段检查 ===\n\n")
            if entry:
                if result.get("missing_core"):
                    sys.stdout.write("⚠️  缺失核心字段:\n")
                    for field in result["missing_core"]:
                        sys.stdout.write(f"  - {field}\n")
                    sys.stdout.write("\n")
                else:
                    sys.stdout.write("✅ 核心字段完整\n\n")

                if not result.get("has_trigger_terms") and args.kind in ["issue", "map", "pitfall"]:
                    sys.stdout.write(f"⚠️  {args.kind} 类记录建议提供 trigger_terms\n")
                    sys.stdout.write(f"   提示: {result['trigger_terms_hint']}\n\n")
                elif result.get("has_trigger_terms"):
                    sys.stdout.write(f"✅ trigger_terms: {result['trigger_terms_count']} 个\n")
                    sys.stdout.write(f"   预览: {json.dumps(result['trigger_terms_preview'], ensure_ascii=False)}\n\n")

            sys.stdout.write(f"用途: {result['description']}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
