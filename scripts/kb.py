#!/usr/bin/env python3
"""Stable daily Personal KB entry point.

The existing focused scripts remain the implementation modules; this wrapper
only gives agents a small, discoverable command surface.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Callable


DAILY_COMMANDS: dict[str, tuple[str, str, list[str]]] = {
    "retrieve": ("kb_rag_context", "main", []),
    "search": ("kb_search", "main", []),
    "remember": ("kb_add", "main", []),
    "update": ("kb_update", "main", []),
    "retain": ("kb_retain_file", "main", ["retain"]),
    "reference": ("kb_retain_file", "main", ["reference"]),
    "evidence": ("kb_retain_file", "main", []),
    "closeout": ("kb_closeout", "main", []),
    "challenge": ("kb_challenge", "main", []),
    "where": ("kb_whereami", "main", []),
}


def _run(module_name: str, function_name: str, argv: list[str]) -> int:
    module = importlib.import_module(module_name)
    function: Callable[[list[str]], int] = getattr(module, function_name)
    try:
        result = function(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return int(result or 0)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        parser = argparse.ArgumentParser(
            prog="kb.py",
            description="Personal KB daily entry point; use one canonical configured data root.",
        )
        parser.add_argument("command", choices=sorted(DAILY_COMMANDS), nargs="?", help="daily operation")
        parser.print_help()
        return 0

    command = args.pop(0)
    spec = DAILY_COMMANDS.get(command)
    if spec is None:
        sys.stderr.write(f"unknown daily command: {command}\n")
        return 2
    module_name, function_name, prefix = spec
    return _run(module_name, function_name, [*prefix, *args])


if __name__ == "__main__":
    raise SystemExit(main())
