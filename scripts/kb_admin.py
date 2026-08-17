#!/usr/bin/env python3
"""Stable explicit-maintenance entry point for Personal KB."""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Callable


ADMIN_COMMANDS: dict[str, tuple[str, str]] = {
    "normalize": ("kb_normalize", "main"),
    "rebuild-index": ("kb_rebuild_index", "main"),
    "quality-gate": ("kb_quality_gate", "main"),
    "sensitive-scan": ("kb_sensitive_scan", "main"),
    "archive": ("kb_archive_old_records", "main"),
    "migrate": ("kb_migrate", "main"),
    "compact": ("kb_compact", "main"),
    "schema": ("kb_schema_discover", "main"),
}


def _run(module_name: str, function_name: str, argv: list[str]) -> int:
    module = importlib.import_module(module_name)
    function: Callable[..., int] = getattr(module, function_name)
    try:
        result = function(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return int(result or 0)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        parser = argparse.ArgumentParser(prog="kb_admin.py", description="Explicit Personal KB maintenance operations.")
        parser.add_argument("command", choices=sorted(ADMIN_COMMANDS), nargs="?")
        parser.print_help()
        return 0
    command = args.pop(0)
    spec = ADMIN_COMMANDS.get(command)
    if spec is None:
        sys.stderr.write(f"unknown admin command: {command}\n")
        return 2
    return _run(*spec, args)


if __name__ == "__main__":
    raise SystemExit(main())
