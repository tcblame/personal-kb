#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kb_lib import StorageLayout, storage_layout


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


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _protected_files(layout: StorageLayout) -> list[Path]:
    """Return production facts and runtime artifacts, excluding rebuildable cache."""
    cache = layout.cache.resolve(strict=False)
    paths: set[Path] = set()
    for directory in (
        layout.records,
        layout.runtime,
        layout.manifests,
        layout.retained_files,
    ):
        if directory.is_file():
            candidates = (directory,)
        elif directory.is_dir():
            candidates = directory.rglob("*")
        else:
            continue
        for path in candidates:
            if not path.is_file():
                continue
            resolved = path.resolve(strict=False)
            if _is_within(resolved, cache):
                continue
            paths.add(resolved)
    return sorted(paths)


def _production_fingerprint(layout: StorageLayout) -> dict[str, str]:
    paths = _protected_files(layout)

    fingerprint: dict[str, str] = {}
    for path in paths:
        try:
            relative = path.relative_to(layout.root).as_posix()
        except ValueError:
            relative = str(path)
        try:
            stat = path.stat()
            fingerprint[relative] = (
                f"sha256={_sha256(path)};size={stat.st_size};mtime_ns={stat.st_mtime_ns}"
            )
        except FileNotFoundError:
            fingerprint[relative] = "missing"
    return fingerprint


@dataclass
class TestGuard:
    test_name: str
    temporary: tempfile.TemporaryDirectory[str]
    production_layout: StorageLayout
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
            after = _production_fingerprint(self.production_layout)
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
    production_layout = storage_layout()
    before = _production_fingerprint(production_layout)
    previous = {key: os.environ.get(key) for key in _ENV_KEYS}
    temporary = tempfile.TemporaryDirectory(prefix="personal-kb-test-")
    test_name = Path(test_file).stem
    os.environ["PERSONAL_KB_ROOT"] = str(Path(temporary.name) / "personal-kb")
    os.environ["PERSONAL_KB_RUNTIME_SOURCE"] = "test"
    os.environ["PERSONAL_KB_TEST_RUN_ID"] = f"{test_name}-{uuid.uuid4().hex}"
    return TestGuard(
        test_name=test_name,
        temporary=temporary,
        production_layout=production_layout,
        before=before,
        previous_env=previous,
    )
