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

from kb_command_contract import DAILY_COMMANDS
from kb_timing import recording, span


def _run(module_name: str, function_name: str, argv: list[str]) -> int:
    with span("module_import"):
        module = importlib.import_module(module_name)
    function: Callable[[list[str]], int] = getattr(module, function_name)
    try:
        with span("command"):
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
        parser.add_argument("--timings", action="store_true", help="Emit one process-local timing report on stderr")
        parser.print_help()
        return 0

    command = args.pop(0)
    spec = DAILY_COMMANDS.get(command)
    if spec is None:
        sys.stderr.write(f"unknown daily command: {command}\n")
        return 2
    module_name, function_name, prefix = spec

    def execute() -> int:
        if command == "closeout" and "--legacy-allow-unlinked-retrievals" in args:
            sys.stderr.write(
                "legacy unlinked closeout compatibility is available only through the focused kb_closeout.py script\n"
            )
            return 2
        return _run(module_name, function_name, [*prefix, *args])

    # Consume only an option token, never a positional value after `--`.
    option_end = args.index("--") if "--" in args else len(args)
    if "--timings" in args[:option_end]:
        args = [value for index, value in enumerate(args) if index >= option_end or value != "--timings"]
        with recording(command) as report:
            result = execute()
            report.exit_code = result
            return result
    return execute()


if __name__ == "__main__":
    raise SystemExit(main())
