"""Opt-in, process-local timings. Never records queries, paths, or command arguments."""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from time import perf_counter
from typing import Any


@dataclass
class TimingReport:
    command: str
    started: float = field(default_factory=perf_counter)
    exit_code: int | None = None
    stages: dict[str, dict[str, float | int]] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    git_checks: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def payload(self) -> dict[str, Any]:
        return {
            "schema": "personal-kb.timing/v1",
            "command": self.command,
            "exit_code": self.exit_code,
            "elapsed_ms": round((perf_counter() - self.started) * 1000, 3),
            "stages_are_inclusive": True,
            "excludes": ["wsl_startup", "python_startup", "wrapper_bootstrap", "report_emit", "output_delivery"],
            "stages": {name: {"calls": value["calls"], "total_ms": round(value["total_ms"], 3)}
                       for name, value in sorted(self.stages.items())},
            "counters": dict(sorted(self.counters.items())),
            "git_checks": {
                name: {"calls": row["calls"], "unique_checks": len(row["keys"]),
                       "repeated_calls": row["calls"] - len(row["keys"]),
                       "repeated_ms": round(row["repeated_ms"], 3)}
                for name, row in sorted(self.git_checks.items())
            },
        }


_ACTIVE: ContextVar[TimingReport | None] = ContextVar("kb_timing", default=None)


@contextmanager
def recording(command: str):
    report = TimingReport(command)
    token = _ACTIVE.set(report)
    try:
        yield report
    finally:
        _ACTIVE.reset(token)
        # One separate line preserves JSON/Markdown stdout and silent defaults.
        try:
            sys.stderr.write("KB_TIMING " + json.dumps(report.payload(), separators=(",", ":")) + "\n")
        except (OSError, ValueError):
            # Optional diagnostics must not mask a command/storage failure.
            pass


@contextmanager
def _measure(report: TimingReport, name: str):
    started = perf_counter()
    try:
        yield
    finally:
        stage = report.stages.setdefault(name, {"calls": 0, "total_ms": 0.0})
        stage["calls"] += 1
        stage["total_ms"] += (perf_counter() - started) * 1000


def span(name: str):
    report = _ACTIVE.get()
    return _measure(report, name) if report is not None else nullcontext()


def count(name: str, amount: int = 1) -> None:
    report = _ACTIVE.get()
    if report is not None:
        report.counters[name] = report.counters.get(name, 0) + amount


def timed(name: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if _ACTIVE.get() is None:
                return function(*args, **kwargs)
            with span(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def git_span(args: list[str] | tuple[str, ...], *, cwd: Path | str | None = None):
    report = _ACTIVE.get()
    if report is None:
        return nullcontext()
    operation = args[0] if args else "other"
    # Fixed labels only: never leak an operand, remote URL, or file name.
    if operation not in {"rev-parse", "status", "ls-files", "remote", "config",
                         "branch", "cat-file", "merge-base", "show", "log", "diff", "rev-list"}:
        operation = "other"
    # Exact operands and cwd are private, per-process comparison keys only.
    # Keep the exact invocation directory. Resolving it for every timed Git
    # call adds significant DrvFs I/O; aliases are conservatively counted apart.
    key = (str(cwd) if cwd is not None else "", tuple(args))
    return _measure_git(report, operation, key)


@contextmanager
def _measure_git(report: TimingReport, operation: str, key: tuple):
    started = perf_counter()
    with _measure(report, "git." + operation):
        try:
            yield
        finally:
            row = report.git_checks.setdefault(operation, {"calls": 0, "keys": set(), "repeated_ms": 0.0})
            row["calls"] += 1
            if key in row["keys"]:
                row["repeated_ms"] += (perf_counter() - started) * 1000
            row["keys"].add(key)
