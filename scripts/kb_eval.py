#!/usr/bin/env python3
"""Stable validation and audit entry point for Personal KB."""

from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from pathlib import Path
from typing import Callable


PYTHON_FUNCTION_COMMANDS: dict[str, tuple[str, str]] = {
    "preflight": ("kb_eval_preflight", "main"),
    "audit-runtime": ("kb_audit_runtime_value", "main"),
    "audit-sessions": ("kb_audit_codex_sessions", "main"),
    "effectiveness": ("kb_record_codex_effectiveness", "main"),
    "release-check": ("kb_release", "main"),
}
SCRIPT_COMMANDS = {
    "smoke": "kb_smoke_test.py",
    "storage-test": "kb_storage_test.py",
    "challenge-test": "kb_challenge_test.py",
    "retain-test": "kb_retain_file_test.py",
    "release-test": "kb_release_test.py",
}


def _run_module(module_name: str, function_name: str, argv: list[str]) -> int:
    module = importlib.import_module(module_name)
    function: Callable[..., int] = getattr(module, function_name)
    try:
        result = function(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return int(result or 0)


def _run_script(script_name: str, argv: list[str]) -> int:
    script = Path(__file__).with_name(script_name)
    proc = subprocess.run([sys.executable, str(script), *argv], check=False)
    return int(proc.returncode)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    commands = sorted({*PYTHON_FUNCTION_COMMANDS, *SCRIPT_COMMANDS})
    if not args or args[0] in {"-h", "--help"}:
        parser = argparse.ArgumentParser(prog="kb_eval.py", description="Personal KB tests, audits, and release checks.")
        parser.add_argument("command", choices=commands, nargs="?")
        parser.print_help()
        return 0
    command = args.pop(0)
    if command in PYTHON_FUNCTION_COMMANDS:
        return _run_module(*PYTHON_FUNCTION_COMMANDS[command], args)
    script_name = SCRIPT_COMMANDS.get(command)
    if script_name:
        return _run_script(script_name, args)
    sys.stderr.write(f"unknown eval command: {command}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
