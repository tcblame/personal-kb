#!/usr/bin/env python3
"""Read-only Cursor transcript audit. Tool requests are not execution results."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from kb_command_contract import KB_SCRIPT_PATTERNS, repeated_flag_values

OPERATIONS = {name: key.removeprefix('kb_') for key, name in KB_SCRIPT_PATTERNS.items()}
OPERATIONS.update({'kb_rag_context.py': 'retrieve', 'kb_add.py': 'remember'})
WRAPPER_OPS = {'retrieve', 'search', 'remember', 'update', 'closeout', 'curate-sessions'}
PYTHON = re.compile(r'python(?:\d+(?:\.\d+)?)?|py', re.I)


def invocations(command: str) -> tuple[list[dict], bool]:
    """Recognize direct Python shell invocations, never quoted mentions or Python bodies.

    Unsupported heredoc bodies are reported for manual review. Their surrounding
    direct shell calls are still counted. No session command is ever executed.
    """
    body_lines, shell_lines, marker = [], [], None
    for line in command.splitlines():
        if marker is not None:
            if line.strip() == marker:
                marker = None
            else:
                body_lines.append(line)
            continue
        match = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z_0-9]*)['\"]?", line)
        if match:
            prefix = line[:match.start()].strip()
            if prefix:
                shell_lines.append(prefix)
            marker = match.group(1)
            continue
        shell_lines.append(line)
    body = '\n'.join(body_lines)
    unsupported = bool(re.search(r'kb(?:_[a-z_]+)?\.py', body))
    try:
        lexer = shlex.shlex('\n'.join(shell_lines), posix=True, punctuation_chars=';&|\n')
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        lexer.commenters = '#'
        tokens = list(lexer)
    except ValueError:
        return [], True
    segments, current = [], []
    for token in tokens:
        if token and all(c in ';&|\n' for c in token):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    calls = []
    for tokens in segments:
        # Only executable position; echo/rg/cat arguments never become calls.
        start = 0
        while start < len(tokens) and re.fullmatch(r'[A-Za-z_]\w*=.*', tokens[start], re.S):
            start += 1
        if start >= len(tokens):
            continue
        if Path(tokens[start]).name == 'env':
            start += 1
            while start < len(tokens) and '=' in tokens[start] and not tokens[start].startswith('-'):
                start += 1
        if start < len(tokens) and PYTHON.fullmatch(Path(tokens[start]).name):
            start += 1
            while start < len(tokens) and tokens[start] in {'-u', '-B'}:
                start += 1
        if start >= len(tokens):
            continue
        script = Path(tokens[start]).name
        offset = start + 1
        if script == 'kb.py' and offset < len(tokens) and tokens[offset] in WRAPPER_OPS:
            operation = tokens[offset]
            offset += 1
        elif script in OPERATIONS:
            operation = OPERATIONS[script]
        else:
            if any(Path(t).name in {*OPERATIONS, 'kb.py'} for t in tokens) and Path(tokens[0]).name not in {'echo', 'printf', 'rg', 'grep', 'cat', 'sed', 'head', 'tail'}:
                unsupported = True
            continue
        args = tokens[offset:]
        calls.append({
            'operation': operation,
            'help': any(t in {'-h', '--help'} for t in args),
            'closeout_ids': repeated_flag_values(args, '--closeout-id'),
            'session_ids': repeated_flag_values(args, '--session-id'),
            'topic_ids': repeated_flag_values(args, '--topic-id'),
            'linked_retrieval_ids': repeated_flag_values(args, '--linked-retrieval-id'),
            'query_chars': len(args[0]) if operation == 'retrieve' and args and not args[0].startswith('-') else None,
            # This parser reports requests. Runtime reconciliation is separate.
            'execution_status': 'unknown',
        })
    return calls, unsupported


def audit_session(path: Path) -> dict:
    raw = path.read_bytes()
    calls, invalid, unsupported, tool_results = [], 0, [], 0
    for line_no, line in enumerate(raw.decode('utf-8', 'replace').splitlines(), 1):
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            invalid += 1
            continue
        if not isinstance(row, dict):
            invalid += 1
            continue
        message = row.get('message') or {}
        content = message.get('content', []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'tool_result':
                tool_results += 1
            if row.get('role') != 'assistant' or item.get('type') != 'tool_use' or item.get('name') != 'Shell':
                continue
            inputs = item.get('input', {})
            command = inputs.get('command', '') if isinstance(inputs, dict) else ''
            if not isinstance(command, str):
                continue
            found, unresolved = invocations(command)
            calls.extend(dict(call, line=line_no) for call in found)
            if unresolved:
                unsupported.append(line_no)
    return {
        'session_id': path.stem, 'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
        'scope': 'subagent' if 'subagents' in path.parts else 'main',
        'selection_date': datetime.fromtimestamp(path.stat().st_mtime).date().isoformat(),
        'date_basis': 'file_mtime_not_call_time', 'invalid_rows': invalid,
        'tool_result_items': tool_results, 'unsupported_shell_lines': unsupported,
        'calls': calls,
    }


def reconcile(sessions: list[dict], runtime: list[dict], start: date, end: date) -> dict:
    owners = defaultdict(set)
    for session in sessions:
        for call in session['calls']:
            if call['help'] or call['operation'] != 'closeout':
                continue
            for cid in call['closeout_ids']:
                owners[cid].add(session['session_id'])
    by_id = defaultdict(list)
    for row in runtime:
        try:
            stamp = datetime.fromisoformat(str(row.get('ts', '')).replace('Z', '+00:00')).astimezone().date()
        except ValueError:
            continue
        if start <= stamp <= end and row.get('closeout_id'):
            by_id[row['closeout_id']].append(row)
    totals = Counter()
    for session in sessions:
        matched = []
        cids = {cid for c in session['calls'] if not c['help'] and c['operation'] == 'closeout' for cid in c['closeout_ids']}
        for cid in sorted(cids):
            if len(owners[cid]) != 1 or len(by_id.get(cid, [])) != 1:
                continue  # no guesses on duplicate identities or repeated events
            row = by_id[cid][0]
            if row.get('session_id') and row['session_id'] != session['session_id']:
                continue
            effects = row.get('adoption_applied') or {}
            matched.append({
                'closeout_id': cid, 'join': 'unique_explicit_closeout_id',
                'hit_count': row.get('hit_count'),
                'adoption_applied': effects,
                'written_entry_ids_reported': row.get('written_entry_ids', []),
                'updated_entry_ids_reported': row.get('updated_entry_ids', []),
            })
        session['runtime_matches'] = matched
        totals['matched_closeouts'] += len(matched)
        totals['matched_sessions'] += bool(matched)
    return dict(totals)


def build_report(paths: list[Path], start: date, end: date, runtime: list[dict] | None = None) -> dict:
    sessions = [audit_session(p) for p in sorted(set(p.resolve() for p in paths))
                if start <= datetime.fromtimestamp(p.stat().st_mtime).date() <= end]
    totals = {}
    for scope in ('main', 'subagent'):
        selected = [s for s in sessions if s['scope'] == scope]
        calls = [c for s in selected for c in s['calls']]
        totals[scope] = {
            'sessions': len(selected), 'sessions_with_requests': sum(bool(s['calls']) for s in selected),
            'operation_requests': dict(Counter(c['operation'] for c in calls if not c['help'])),
            'help_requests': sum(c['help'] for c in calls),
            'execution_unknown': sum(not c['help'] for c in calls),
            'unsupported_shell_calls': sum(len(s['unsupported_shell_lines']) for s in selected),
        }
    joined = reconcile(sessions, runtime or [], start, end)
    return {'schema_version': 'cursor-kb-audit-v1', 'read_only': True,
            'range': {'from': start.isoformat(), 'to': end.isoformat()},
            'selection': 'file_mtime; entire file calls may predate window',
            'success_rate': None, 'task_benefit': None,
            'totals': totals, 'runtime_linkage': joined, 'sessions': sessions}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', action='append', required=True, type=Path)
    parser.add_argument('--from', dest='start', type=date.fromisoformat, required=True)
    parser.add_argument('--to', dest='end', type=date.fromisoformat, required=True)
    parser.add_argument('--closeouts', type=Path, help='Optional private runtime JSONL, read-only')
    args = parser.parse_args(argv)
    if args.start > args.end or any(not p.is_dir() for p in args.root):
        parser.error('invalid date window or missing root')
    paths = [p for root in args.root for p in root.rglob('*.jsonl') if 'agent-transcripts' in p.parts]
    try:
        runtime = [json.loads(line) for line in args.closeouts.read_text().splitlines() if line.strip()] if args.closeouts else []
        if any(not isinstance(row, dict) for row in runtime):
            raise ValueError('closeout rows must be objects')
        report = build_report(paths, args.start, args.end, runtime)
    except (OSError, ValueError) as exc:
        parser.exit(2, f'audit input error: {exc}\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
