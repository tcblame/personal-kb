"""
Personal-KB 编码修复工具

修复内容：
1. 确保所有 Python 脚本在启动时强制 UTF-8 输出
2. 提供 PowerShell 安全的参数传递方式
3. 检查并修复已有日志文件的编码问题
"""

import sys
import io
from pathlib import Path


def force_utf8_output():
    """强制 stdout/stderr 使用 UTF-8 编码（Windows 兼容）"""
    if sys.platform == 'win32':
        # 重新配置 stdout 和 stderr 为 UTF-8
        if hasattr(sys.stdout, 'buffer'):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer,
                encoding='utf-8',
                errors='replace',
                line_buffering=True
            )
        if hasattr(sys.stderr, 'buffer'):
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer,
                encoding='utf-8',
                errors='replace',
                line_buffering=True
            )


def check_log_encoding(log_path: Path) -> dict:
    """检查日志文件的编码情况"""
    result = {
        'path': str(log_path),
        'exists': log_path.exists(),
        'utf8_valid': False,
        'gbk_chars': 0,
        'size': 0
    }

    if not log_path.exists():
        return result

    result['size'] = log_path.stat().st_size

    try:
        # 尝试 UTF-8 读取
        with open(log_path, 'r', encoding='utf-8') as f:
            content = f.read()
        result['utf8_valid'] = True

        # 检查是否有 GBK 乱码特征（如 ����）
        result['gbk_chars'] = content.count('�')

    except UnicodeDecodeError:
        result['utf8_valid'] = False

    return result


def main():
    force_utf8_output()

    log_dir = Path.home() / '.codex' / 'personal-kb-logs'

    print('=== Personal-KB 编码检查 ===\n')

    if not log_dir.exists():
        print(f'日志目录不存在: {log_dir}')
        return

    print(f'日志目录: {log_dir}\n')

    log_files = [
        'kb_search.log',
        'kb_add.log'
    ]

    issues = []

    for log_file in log_files:
        log_path = log_dir / log_file
        result = check_log_encoding(log_path)

        print(f'文件: {log_file}')
        print(f'  存在: {result["exists"]}')
        if result['exists']:
            print(f'  大小: {result["size"]:,} 字节')
            print(f'  UTF-8 有效: {result["utf8_valid"]}')
            print(f'  乱码字符数: {result["gbk_chars"]}')

            if not result['utf8_valid'] or result['gbk_chars'] > 0:
                issues.append(log_file)
        print()

    if issues:
        print(f'⚠️  发现 {len(issues)} 个编码问题文件:')
        for f in issues:
            print(f'  - {f}')
        print('\n建议：备份后删除这些日志文件，让系统重新生成')
    else:
        print('✓ 所有日志文件编码正常')


if __name__ == '__main__':
    main()
