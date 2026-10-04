"""Bounded static extraction of common exec wrappers. Never evaluates JS or shell."""
from __future__ import annotations

import ast
import json
import re

LITERAL = r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`'
TOKEN = re.compile(LITERAL + r'|\s+|//[^\n]*|/\*.*?\*/|.', re.S)


def decode(literal: str) -> str | None:
    try:
        if literal.startswith('"'):
            value = json.loads(literal)
        elif literal.startswith("'"):
            value = ast.literal_eval(literal)
        elif literal.startswith('`') and '${' not in literal:
            value = literal[1:-1].replace('\\\n', '').replace(r'\`', '`').replace(r'\n', '\n').replace(r'\r', '\r').replace(r'\t', '\t')
        else:
            return None
        return value if isinstance(value, str) else None
    except (ValueError, SyntaxError):
        return None


def _value(source: str, offset: int):
    """Only string literals and arrays of such literals are statically trusted."""
    tokens = [m.group() for m in TOKEN.finditer(source[offset:]) if not m.group().isspace() and not m.group().startswith(('//', '/*'))]
    def parse(index: int):
        if index >= len(tokens):
            raise ValueError
        value = decode(tokens[index])
        if value is not None:
            return value, index + 1
        if tokens[index] != '[':
            raise ValueError
        values, index = [], index + 1
        while index < len(tokens) and tokens[index] != ']':
            value, index = parse(index)
            values.append(value)
            if index < len(tokens) and tokens[index] == ',':
                index += 1
            elif index >= len(tokens) or tokens[index] != ']':
                raise ValueError
        if index >= len(tokens):
            raise ValueError
        return values, index + 1
    try:
        value, end = parse(0)
        if end < len(tokens) and tokens[end] not in {";", ","}:
            return None
        return value
    except (ValueError, RecursionError):
        return None


def commands(source: str) -> list[str]:
    if len(source) > 250000 or 'tools.exec_command' not in source:
        return []
    # Keep offsets while hiding strings/comments from structural matching.
    masked = TOKEN.sub(lambda m: ' ' * len(m.group()) if m.group().startswith(('"', "'", '`', '//', '/*')) else m.group(), source)
    bindings = {}
    for match in re.finditer(r'\bconst\s+(\w+)\s*=\s*', masked):
        offset = match.start() + source[match.start():].index('=') + 1
        bindings[match[1]] = _value(source, offset)
    # Do not resolve bindings subsequently reassigned or mutated.
    for name in list(bindings):
        tail = masked[re.search(r'\bconst\s+' + re.escape(name) + r'\s*=', masked).end():]
        if re.search(r'\b' + re.escape(name) + r'\s*(?:=(?!=)|\.(?:push|splice|pop|shift|unshift)\s*\()', tail):
            bindings.pop(name)

    results = []
    direct = re.compile(r'(?:["\']cmd["\']|\bcmd)\s*:\s*(' + LITERAL + ')', re.S)
    for match in direct.finditer(source):
        # Do not parse cmd-shaped text inside an unrelated string literal.
        if not masked[match.start():match.start()+3].strip() and not source[match.start():].startswith(('"cmd"', "'cmd'")):
            continue
        tail = source[match.end():].lstrip()
        if tail and tail[0] not in {",", "}"}:
            continue  # A literal prefix of a dynamic expression is not a command.
        literal = match[1]
        value = decode(literal)
        if value is not None:
            results.append((match.start(), value))
            continue
        # Bounded array.map interpolation: common query wrapper, no execution.
        for loop in re.finditer(r'\b(\w+)\.map\(\s*(?:async\s+)?(?:\(\s*)?(\w+)(?:\s*\))?\s*=>', masked[:match.start()]):
            values = bindings.get(loop[1])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                continue
            for item in values:
                expanded = literal[1:-1].replace('${JSON.stringify(' + loop[2] + ')}', json.dumps(item, ensure_ascii=False)).replace('${' + loop[2] + '}', item)
                if '${' not in expanded:
                    results.append((match.start(), expanded.replace('\\\n', '')))

    for call in re.finditer(r'tools\.exec_command\s*\(\s*\{\s*cmd\s*(?:(?P<short>,|\})|:\s*(?P<name>\w+)\s*[,}])', masked):
        name = call['name'] or 'cmd'
        if isinstance(bindings.get(name), str):
            results.append((call.start(), bindings[name]))
            continue
        prefix = masked[:call.start()]
        for loop in re.finditer(r'\b(\w+)\.map\(\s*(?:async\s+)?\(\s*\[([^\]]+)\]\s*\)\s*=>', prefix):
            names = [x.strip() for x in loop[2].split(',')]
            values = bindings.get(loop[1])
            if name in names and isinstance(values, list):
                index = names.index(name)
                for row in values:
                    if isinstance(row, list) and index < len(row) and isinstance(row[index], str):
                        results.append((call.start(), row[index]))
    return [text for _, text in sorted(results, key=lambda x: x[0])]
