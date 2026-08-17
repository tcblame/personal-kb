#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


GUARD_FAILURE_EXIT = 97
_ENV_KEYS = (
    "PERSONAL_KB_ROOT",
    "PERSONAL_KB_RUNTIME_SOURCE",
    "PERSONAL_KB_TEST_RUN_ID",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _configured_layout() -> tuple[Path, Path, Path]:
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    storage = payload.get("storage") if isinstance(payload.get("storage"), dict) else {}
    root = Path(str(storage.get("root") or "")).expanduser().resolve()
    records = root / str(storage.get("records") or "records")
    runtime = root / str(storage.get("runtime") or "runtime")
    return root, records, runtime


def _production_fingerprint() -> dict[str, str]:
    root, records, runtime = _configured_layout()
    paths = [
        runtime / "closeout.jsonl",
        runtime / "adoption_events.jsonl",
        runtime / "session_briefs.jsonl",
    ]
    if records.is_dir():
        paths.extend(sorted(records.rglob("kb.jsonl")))

    fingerprint: dict[str, str] = {}
    for path in paths:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = str(path)
        if not path.exists():
            fingerprint[relative] = "missing"
            continue
        stat = path.stat()
        fingerprint[relative] = (
            f"sha256={_sha256(path)};size={stat.st_size};mtime_ns={stat.st_mtime_ns}"
        )
    return fingerprint


@dataclass
class TestGuard:
    test_name: str
    temporary: tempfile.TemporaryDirectory[str]
    before: dict[str, str]
    previous_env: dict[str, str | None]

    def run(self, main: Callable[[], int]) -> int:
        error: BaseException | None = None
        code = 1
        try:
            try:
                code = int(main() or 0)
            except SystemExit as exc:
                code = int(exc.code or 0) if isinstance(exc.code, (int, type(None))) else 1
            except BaseException as exc:
                error = exc
        finally:
            after = _production_fingerprint()
            for key, value in self.previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self.temporary.cleanup()

        changed = sorted(
            key for key in set(self.before) | set(after)
            if self.before.get(key) != after.get(key)
        )
        if changed:
            sys.stderr.write(
                "production Personal KB changed during test run: "
                + ", ".join(changed)
                + "\n"
            )
            return GUARD_FAILURE_EXIT
        if error is not None:
            raise error
        return code


def activate(test_file: str) -> TestGuard:
    before = _production_fingerprint()
    previous = {key: os.environ.get(key) for key in _ENV_KEYS}
    temporary = tempfile.TemporaryDirectory(prefix="personal-kb-test-")
    test_name = Path(test_file).stem
    os.environ["PERSONAL_KB_ROOT"] = str(Path(temporary.name) / "personal-kb")
    os.environ["PERSONAL_KB_RUNTIME_SOURCE"] = "test"
    os.environ["PERSONAL_KB_TEST_RUN_ID"] = f"{test_name}-{uuid.uuid4().hex}"
    return TestGuard(
        test_name=test_name,
        temporary=temporary,
        before=before,
        previous_env=previous,
    )
