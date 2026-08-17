from __future__ import annotations

import os
import re
from typing import Any, Mapping


RUNTIME_SOURCE_ENV = "PERSONAL_KB_RUNTIME_SOURCE"
TEST_RUN_ID_ENV = "PERSONAL_KB_TEST_RUN_ID"
VALID_RUNTIME_SOURCES = frozenset({"production", "test"})
TEST_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def runtime_scope(env: Mapping[str, str] | None = None) -> dict[str, str]:
    values = os.environ if env is None else env
    source = str(values.get(RUNTIME_SOURCE_ENV) or "production").strip().lower()
    test_run_id = str(values.get(TEST_RUN_ID_ENV) or "").strip()
    if source not in VALID_RUNTIME_SOURCES:
        raise ValueError(
            f"{RUNTIME_SOURCE_ENV} must be one of: {', '.join(sorted(VALID_RUNTIME_SOURCES))}"
        )
    if source == "test":
        if not TEST_RUN_ID_RE.fullmatch(test_run_id):
            raise ValueError(
                f"{TEST_RUN_ID_ENV} is required for test runtime events and must be an opaque 1-128 character ID"
            )
    elif test_run_id:
        raise ValueError(f"{TEST_RUN_ID_ENV} is only valid when {RUNTIME_SOURCE_ENV}=test")
    return {"source": source, "test_run_id": test_run_id}


def attach_runtime_scope(event: dict[str, Any]) -> dict[str, Any]:
    scoped = dict(event)
    scoped["runtime"] = runtime_scope()
    return scoped


def event_runtime_source(event: Mapping[str, Any]) -> str:
    runtime = event.get("runtime")
    if isinstance(runtime, Mapping):
        source = str(runtime.get("source") or "").strip().lower()
        if source:
            return source
    # Compatibility for the 2026-08-17 regression rows, where the test marker
    # was incorrectly stored as a routing source.
    routing = event.get("routing")
    if isinstance(routing, Mapping):
        source = str(routing.get("source") or "").strip().lower()
        if source == "test":
            return "test"
    # Compatibility for older test fixtures that used a top-level source
    # marker before runtime scope became explicit.
    if str(event.get("source") or "").strip().lower() == "test":
        return "test"
    return "production"


def event_test_run_id(event: Mapping[str, Any]) -> str:
    runtime = event.get("runtime")
    if isinstance(runtime, Mapping):
        return str(runtime.get("test_run_id") or "").strip()
    return ""


def is_test_event(event: Mapping[str, Any]) -> bool:
    return event_runtime_source(event) == "test"
