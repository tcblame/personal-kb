#!/usr/bin/env python3
"""
Personal-KB 验证脚本

用于检查：
1. 核心脚本是否存在
2. SKILL.md 长度是否合理
3. 是否有未填充的 FILL 标记
4. Common Tasks 引用的文件是否存在
5. KB 条目统计
6. map 映射记录是否有文件映射（质量建议）
7. 是否有过期条目（> 6 个月且未被引用）
8. 是否有重复条目（标题相似度 > 80%）
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from collections import defaultdict

sys.dont_write_bytecode = True

from kb_kinds import VALID_KINDS
from kb_lib import kb_base_dir

SKILL_MD_MAX_LINES = 500

# 设置 UTF-8 输出（Windows 兼容）
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 颜色输出
def green(s): return f"\033[92m[OK] {s}\033[0m"
def red(s): return f"\033[91m[FAIL] {s}\033[0m"
def yellow(s): return f"\033[93m[WARN] {s}\033[0m"
def blue(s): return f"\033[94m[INFO] {s}\033[0m"

def check_scripts(skill_dir):
    """检查核心脚本是否存在"""
    print("\n=== 1. 检查核心脚本 ===")
    scripts_dir = skill_dir / "scripts"
    required_scripts = [
        "kb.py",
        "kb_admin.py",
        "kb_eval.py",
        "kb_challenge.py",
        "kb_release.py",
        "kb_release_test.py",
        "kb_rag_context.py",
        "kb_add.py",
        "kb_search.py",
        "kb_update.py",
        "kb_closeout.py",
        "kb_evidence.py",
        "kb_adoption.py",
        "kb_audit_runtime_value.py",
        "kb_record_codex_effectiveness.py",
        "kb_runtime_quality_watch.py",
        "kb_compact.py",
        "kb_whereami.py",
        "kb_retain_file.py",
        "kb_sensitive_scan.py",
        "kb_archive_old_records.py",
        "kb_normalize.py",
        "kb_rebuild_index.py",
        "kb_quality_gate.py",
        "kb_lib.py"
    ]

    all_exist = True
    for script in required_scripts:
        script_path = scripts_dir / script
        if script_path.exists():
            print(green(f"{script}"))
        else:
            print(red(f"缺少 {script}"))
            all_exist = False

    return all_exist

def check_skill_md_length(skill_dir):
    """检查 SKILL.md 长度"""
    print("\n=== 2. 检查 SKILL.md 长度 ===")
    skill_md = skill_dir / "SKILL.md"

    if not skill_md.exists():
        print(red("SKILL.md 不存在"))
        return False

    with open(skill_md, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    line_count = len(lines)
    if line_count <= SKILL_MD_MAX_LINES:
        print(green(f"SKILL.md 长度正常（{line_count} 行）"))
        return True
    else:
        print(yellow(f"SKILL.md 超过 {SKILL_MD_MAX_LINES} 行（当前 {line_count} 行），建议拆分"))
        return False

def check_fill_markers(skill_dir):
    """检查是否有未填充的 FILL 标记"""
    print("\n=== 3. 检查 FILL 标记 ===")
    fill_markers = []

    for root, dirs, files in os.walk(skill_dir):
        # 跳过 __pycache__ 和 kb_smoke_test.py 本身
        if '__pycache__' in root:
            continue

        for file in files:
            if file.endswith(('.md', '.py')) and file != 'kb_smoke_test.py':
                file_path = Path(root) / file
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        for line_num, line in enumerate(f, 1):
                            if '<!-- FILL:' in line or 'FILL:' in line:
                                fill_markers.append((file_path, line_num, line.strip()))
                except:
                    pass

    if not fill_markers:
        print(green("无未填充的 FILL 标记"))
        return True
    else:
        print(red(f"发现 {len(fill_markers)} 个未填充的 FILL 标记："))
        for path, line_num, line in fill_markers[:5]:  # 只显示前 5 个
            rel_path = path.relative_to(skill_dir)
            print(f"  {rel_path}:{line_num} - {line[:60]}...")
        return False

def check_referenced_files(skill_dir):
    """检查 SKILL.md 中引用的文件是否存在"""
    print("\n=== 4. 检查 Common Tasks 引用文件 ===")
    skill_md = skill_dir / "SKILL.md"

    if not skill_md.exists():
        return False

    with open(skill_md, 'r', encoding='utf-8') as f:
        content = f.read()

    # 匹配 references/xxx.md 或 workflows/xxx.md 等引用
    import re
    referenced_files = re.findall(r'`(references/[^`]+\.md|workflows/[^`]+\.md|rules/[^`]+\.md)`', content)

    if not referenced_files:
        print(green("AI-only runtime 未引用额外 Markdown 资源"))
        return True

    all_exist = True
    for ref_file in referenced_files:
        file_path = skill_dir / ref_file
        if file_path.exists():
            print(green(ref_file))
        else:
            print(red(f"缺少文件: {ref_file}"))
            all_exist = False

    return all_exist

def get_kb_root():
    """获取 KB 根目录"""
    return kb_base_dir()

def count_kb_entries():
    """统计 KB 条目数"""
    print("\n=== 5. 统计 KB 条目 ===")
    kb_root = get_kb_root()

    if not kb_root.exists():
        print(yellow(f"KB 根目录不存在: {kb_root}"))
        return 0

    total_entries = 0
    repo_counts = defaultdict(int)

    for kb_file in kb_root.rglob('kb.jsonl'):
        try:
            with open(kb_file, 'r', encoding='utf-8') as f:
                entries = [line for line in f if line.strip()]
                count = len(entries)
                total_entries += count

                # 提取 repo 名称
                repo_name = kb_file.parent.parent.name
                repo_counts[repo_name] += count
        except:
            pass

    print(blue(f"总条目数: {total_entries}"))

    if repo_counts:
        print("\n各 repo 分布：")
        for repo, count in sorted(repo_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"  {repo}: {count}")

    return total_entries

def check_map_mappings():
    """检查 map 映射记录是否有文件映射。

    P0 只把 aliases/source_paths/key_files 作为质量建议。旧数据或轻量 map
    缺少文件路径时应提示修补，不应让 smoke 失败。
    """
    print("\n=== 6. 检查 map 映射文件（质量建议） ===")
    kb_root = get_kb_root()

    if not kb_root.exists():
        return True

    missing_mappings = []
    total_map = 0

    for kb_file in kb_root.rglob('kb.jsonl'):
        try:
            with open(kb_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get('_deleted') or entry.get('_archived'):
                            continue
                        if entry.get('kind') == 'map':
                            total_map += 1
                            # 检查是否有实际的文件路径映射
                            key_files = entry.get('key_files', [])
                            story = entry.get('story', '')
                            # key_files 非空，或 story 中包含文件路径标记（/ 或 \）
                            has_mapping = bool(key_files) or any(c in story for c in ['/', '\\'])
                            if not has_mapping:
                                missing_mappings.append({
                                    'id': entry.get('id'),
                                    'term': entry.get('term', entry.get('title', '未知')),
                                    'file': str(kb_file)
                                })
                    except json.JSONDecodeError:
                        pass
        except:
            pass

    if total_map == 0:
        print(yellow("未找到 map kind 条目"))
        return True

    if not missing_mappings:
        print(green(f"所有 map 映射都有文件映射（共 {total_map} 个）"))
        return True
    else:
        print(yellow(f"发现 {len(missing_mappings)}/{total_map} 个 map 映射缺少文件映射："))
        for item in missing_mappings[:5]:  # 只显示前 5 个
            print(f"  {item['term']} (ID: {item['id']})")
        return True

def check_stale_entries():
    """检查过期条目（> 6 个月且未被引用）"""
    print("\n=== 7. 检查过期条目 ===")
    kb_root = get_kb_root()

    if not kb_root.exists():
        return True

    six_months_ago = datetime.now(timezone.utc) - timedelta(days=180)
    stale_entries = []

    for kb_file in kb_root.rglob('kb.jsonl'):
        try:
            with open(kb_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)

                        # 跳过已归档的
                        if entry.get('_archived'):
                            continue

                        # 检查创建时间（使用 ts 字段，而非 created_at）
                        ts = entry.get('ts', '')
                        if ts:
                            created_date = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                            call_count = entry.get('call_count', 0)

                            if created_date < six_months_ago and call_count == 0:
                                stale_entries.append({
                                    'id': entry.get('id'),
                                    'title': entry.get('title', '未知'),
                                    'ts': ts,
                                    'call_count': call_count
                                })
                    except (json.JSONDecodeError, ValueError):
                        pass
        except:
            pass

    if not stale_entries:
        print(green("无过期条目"))
        return True
    else:
        print(yellow(f"发现 {len(stale_entries)} 条过期条目（> 6 个月且未被引用）："))
        for item in stale_entries[:5]:  # 只显示前 5 个
            print(f"  {item['title']} (ID: {item['id']}, 创建于 {item['ts']})")
        return False

def title_similarity(title1, title2):
    """计算标题相似度"""
    return SequenceMatcher(None, title1.lower(), title2.lower()).ratio()

def check_duplicate_entries():
    """检查重复条目（标题相似度 > 80%）"""
    print("\n=== 8. 检查重复条目 ===")
    kb_root = get_kb_root()

    if not kb_root.exists():
        return True

    all_entries = []

    for kb_file in kb_root.rglob('kb.jsonl'):
        try:
            with open(kb_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        # 跳过已删除、已归档、自动提升的条目
                        if entry.get('_deleted') or entry.get('_archived') or entry.get('promoted_from'):
                            continue
                        if entry.get('kind') not in VALID_KINDS:
                            continue
                        all_entries.append({
                            'id': entry.get('id'),
                            'title': entry.get('title', ''),
                            'kind': entry.get('kind', ''),
                            'file': str(kb_file)
                        })
                    except json.JSONDecodeError:
                        pass
        except:
            pass

    duplicates = []
    checked = set()

    # 数量限制：条目 > 1000 时跳过检查，避免性能问题
    if len(all_entries) > 1000:
        print(yellow(f"条目数 {len(all_entries)} > 1000，跳过重复检查以避免性能问题"))
        return True

    # 按 kind 分组，只比对同 kind 条目
    by_kind = defaultdict(list)
    for entry in all_entries:
        kind = entry.get('kind', 'unknown')
        by_kind[kind].append(entry)

    # 对每个 kind 分别检查重复
    for kind_name, entries in by_kind.items():
        if len(entries) < 2:
            continue

        for i, entry1 in enumerate(entries):
            for entry2 in entries[i+1:]:
                # 跳过没有 ID 的条目
                if not entry1.get('id') or not entry2.get('id'):
                    continue

                pair_key = tuple(sorted([entry1['id'], entry2['id']]))
                if pair_key in checked:
                    continue
                checked.add(pair_key)

                similarity = title_similarity(entry1['title'], entry2['title'])
                if similarity > 0.8:
                    duplicates.append({
                        'entry1': entry1,
                        'entry2': entry2,
                        'similarity': similarity
                    })

    if not duplicates:
        print(green("无重复条目"))
        return True
    else:
        print(yellow(f"发现 {len(duplicates)} 组可能重复的条目："))
        for dup in duplicates[:3]:  # 只显示前 3 组
            print(f"  相似度 {dup['similarity']:.0%}:")
            print(f"    - {dup['entry1']['title']} (ID: {dup['entry1']['id']})")
            print(f"    - {dup['entry2']['title']} (ID: {dup['entry2']['id']})")
        return False

def check_storage_schema():
    """检查历史 type、非法 kind 和重复 ID。"""
    print("\n=== 9. 检查存储字段与 ID ===")
    kb_root = get_kb_root()

    if not kb_root.exists():
        return True

    invalid_kind = []
    legacy_type = []
    id_locations = defaultdict(list)

    for kb_file in kb_root.rglob('kb.jsonl'):
        try:
            with open(kb_file, 'r', encoding='utf-8') as f:
                for line_no, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        invalid_kind.append((str(kb_file), line_no, "invalid-json"))
                        continue
                    if entry.get('id'):
                        id_locations[entry['id']].append((str(kb_file), line_no, entry.get('title', '')))
                    kind = entry.get('kind')
                    if kind not in VALID_KINDS:
                        invalid_kind.append((str(kb_file), line_no, kind or "<missing>"))
                    if 'type' in entry:
                        legacy_type.append((str(kb_file), line_no, entry.get('type')))
        except OSError:
            pass

    duplicate_ids = {eid: locs for eid, locs in id_locations.items() if len(locs) > 1}

    ok = True
    if invalid_kind:
        ok = False
        print(red(f"发现 {len(invalid_kind)} 条非法或缺失 kind"))
        for path, line_no, kind in invalid_kind[:5]:
            print(f"  {path}:{line_no} kind={kind}")
    else:
        print(green("所有条目都有有效 6kind"))

    if legacy_type:
        ok = False
        print(red(f"发现 {len(legacy_type)} 条仍保留旧 type 字段"))
        for path, line_no, legacy in legacy_type[:5]:
            print(f"  {path}:{line_no} type={legacy}")
    else:
        print(green("无旧 type 字段残留"))

    if duplicate_ids:
        ok = False
        print(red(f"发现 {len(duplicate_ids)} 个重复 ID"))
        for eid, locs in list(duplicate_ids.items())[:5]:
            print(f"  {eid}: {len(locs)} 处")
            for path, line_no, title in locs[:3]:
                print(f"    {path}:{line_no} {title[:60]}")
    else:
        print(green("无重复 ID"))

    return ok


def check_root_layout(skill_dir):
    """检查公开发布包是否把 Skill 放在仓库根目录。"""
    print("\n=== 10. 检查公开根目录布局 ===")
    required_files = (
        ".gitignore",
        "README.md",
        "SKILL.md",
        "agents/openai.yaml",
        "config.example.json",
        "scripts/kb.py",
    )
    missing = [relative for relative in required_files if not (skill_dir / relative).is_file()]
    nested = (skill_dir / "skills").exists()
    forbidden = []
    for path in skill_dir.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(skill_dir)
        if (
            path.name in {".personal-kb-release.json", "config.json", "manifest.json"}
            or path.suffix in {".jsonl", ".pyc"}
            or "__pycache__" in relative.parts
        ):
            forbidden.append(str(relative))

    ok = not missing and not nested and not forbidden
    if missing:
        print(red("缺少根目录文件: " + ", ".join(missing)))
    if nested:
        print(red("发现禁止的嵌套目录: skills/"))
    if forbidden:
        print(red("发现禁止的发布文件: " + ", ".join(sorted(forbidden)[:5])))
    if ok:
        print(green("根目录 Skill 布局、ownership 状态隔离和发布数据排除均通过"))
    return ok

def main():
    # 确定 skill 目录
    root_layout_requested = "--root-layout" in sys.argv[1:]
    positional_args = [arg for arg in sys.argv[1:] if arg != "--root-layout"]
    if positional_args:
        skill_dir = Path(positional_args[0])
    else:
        # Validate the checked-out Skill itself; storage is resolved by config.json.
        skill_dir = Path(__file__).resolve().parent.parent

    if not skill_dir.exists():
        print(red(f"Skill 目录不存在: {skill_dir}"))
        sys.exit(1)

    print(f"\n验证 Personal-KB Skill: {skill_dir}\n")

    results = []
    results.append(("核心脚本", check_scripts(skill_dir)))
    results.append(("SKILL.md 长度", check_skill_md_length(skill_dir)))
    results.append(("FILL 标记", check_fill_markers(skill_dir)))
    results.append(("引用文件", check_referenced_files(skill_dir)))

    count_kb_entries()

    results.append(("map 映射", check_map_mappings()))
    results.append(("过期条目", check_stale_entries()))
    results.append(("重复条目", check_duplicate_entries()))
    results.append(("存储字段与 ID", check_storage_schema()))
    if root_layout_requested:
        results.append(("公开根目录布局", check_root_layout(skill_dir)))

    # 总结
    print("\n" + "="*50)
    print("=== 验证总结 ===")
    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = green("通过") if result else red("失败")
        print(f"{name}: {status}")

    print(f"\n总计: {passed}/{total} 项通过")

    if passed == total:
        print(green("\n所有检查通过！"))
        sys.exit(0)
    else:
        print(yellow(f"\n有 {total - passed} 项需要注意"))
        sys.exit(1)

if __name__ == '__main__':
    main()
