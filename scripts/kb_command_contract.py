from __future__ import annotations

import re
import shlex
from pathlib import Path


DAILY_COMMANDS: dict[str, tuple[str, str, list[str]]] = {
    "retrieve": ("kb_rag_context", "main", []),
    "search": ("kb_search", "main", []),
    "remember": ("kb_add", "main", []),
    "update": ("kb_update", "main", ["update"]),
    "retain": ("kb_retain_file", "main", ["retain"]),
    "reference": ("kb_retain_file", "main", ["reference"]),
    "evidence": ("kb_retain_file", "main", []),
    "closeout": ("kb_closeout", "main", []),
    "challenge": ("kb_challenge", "main", []),
    "where": ("kb_whereami", "main", []),
}

KB_SCRIPT_PATTERNS = {
    "kb_rag_context": "kb_rag_context.py",
    "kb_search": "kb_search.py",
    "kb_closeout": "kb_closeout.py",
    "kb_add": "kb_add.py",
    "kb_update": "kb_update.py",
}
KB_WRAPPER_COMMANDS = {
    command: module
    for command, (module, _function, _prefix) in DAILY_COMMANDS.items()
    if module in KB_SCRIPT_PATTERNS
}
PYTHON_EXECUTABLE_RE = re.compile(r"python(?:\d+(?:\.\d+)?)?|py", re.I)
SUBAGENT_ONLY_KB_GUARD_RE = re.compile(
    r"Do not run personal-kb scripts\.\s*Use only KB hints provided by the parent;[^\n]{0,240}|"
    r"(?:子\s*agent|subagent|worker|explorer)[^。.!?\n]{0,120}"
    r"(?:不要|禁止|do not)[^。.!?\n]{0,80}(?:personal-kb|kb_)",
    re.I,
)


def strip_subagent_only_kb_guards(text: str) -> tuple[str, bool]:
    stripped, count = SUBAGENT_ONLY_KB_GUARD_RE.subn(" ", str(text or ""))
    return stripped, count > 0


def parse_cli_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def token_basename(token: str) -> str:
    return Path(str(token or "").strip().strip("\"'")).name


def direct_script_index(tokens: list[str], script_name: str) -> int | None:
    for index, token in enumerate(tokens):
        if token_basename(token) != script_name:
            continue
        if index == 0:
            return index
        previous = tokens[index - 1]
        if previous in {"&&", ";", "||", "|"}:
            return index
        cursor = index - 1
        while cursor >= 0 and tokens[cursor].startswith("-") and tokens[cursor] != "-":
            cursor -= 1
        if cursor >= 0 and PYTHON_EXECUTABLE_RE.fullmatch(token_basename(tokens[cursor])):
            return index
    return None


def wrapper_invocation(tokens: list[str]) -> tuple[str, int] | None:
    wrapper_index = direct_script_index(tokens, "kb.py")
    if wrapper_index is None or wrapper_index + 1 >= len(tokens):
        return None
    command_index = wrapper_index + 1
    script_key = KB_WRAPPER_COMMANDS.get(tokens[command_index])
    return (script_key, command_index) if script_key else None


def script_index(tokens: list[str], script_name: str) -> int | None:
    direct_index = direct_script_index(tokens, script_name)
    if direct_index is not None:
        return direct_index
    expected_key = next(
        (key for key, value in KB_SCRIPT_PATTERNS.items() if value == script_name),
        "",
    )
    wrapped = wrapper_invocation(tokens)
    if wrapped and wrapped[0] == expected_key:
        return wrapped[1]
    return None


def is_help_invocation(tokens: list[str], script_name: str) -> bool:
    index = script_index(tokens, script_name)
    return index is not None and any(
        token in {"-h", "--help"} for token in tokens[index + 1 :]
    )


def repeated_flag_values(tokens: list[str], flag: str) -> list[str]:
    values: list[str] = []
    for index, token in enumerate(tokens):
        if token == flag and index + 1 < len(tokens):
            values.append(tokens[index + 1])
        elif token.startswith(flag + "="):
            values.append(token.split("=", 1)[1])
    return [value for value in values if value]


def first_positional(tokens: list[str], script_name: str) -> str:
    index = script_index(tokens, script_name)
    if index is None:
        return ""
    for token in tokens[index + 1 :]:
        if token.startswith("--"):
            break
        return token
    return ""


def direct_or_wrapper_scripts(command: str) -> list[str]:
    tokens = parse_cli_tokens(command)
    detected = [
        key
        for key, script_name in KB_SCRIPT_PATTERNS.items()
        if direct_script_index(tokens, script_name) is not None
    ]
    wrapped = wrapper_invocation(tokens)
    if wrapped and wrapped[0] not in detected:
        detected.append(wrapped[0])
    return detected
