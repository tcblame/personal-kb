"""Resolve a rollout's own identity without inheriting a parent's metadata."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

UUID_RE = re.compile(r'(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})')


def session_roots(root: Path) -> list[Path]:
    """The desktop moves archived rollouts alongside its ordinary session tree."""
    return [root, root.parent / 'archived_sessions'] if root.name == 'sessions' else [root]


def default_sessions_root() -> Path:
    if os.environ.get('CODEX_SESSIONS_ROOT'):
        return Path(os.environ['CODEX_SESSIONS_ROOT']).expanduser()
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser() / 'sessions'


def rollout_paths(root: Path) -> list[Path]:
    copies = {}
    for folder in session_roots(root):
        for path in folder.rglob('*.jsonl*'):
            if not path.is_file() or not path.name.endswith(('.jsonl', '.jsonl.zst')):
                continue
            key = filename_session_id(path) or str(path.resolve()).removesuffix('.zst')
            copies.setdefault(key, []).append(path)
    # A later compression/copy time does not establish later chat activity.
    return sorted(group[0] if len(group) == 1 else max(group, key=_rollout_coverage) for group in copies.values())


def _rollout_coverage(path: Path) -> tuple:
    last = datetime.min.replace(tzinfo=timezone.utc)
    rows = 0
    try:
        with open_rollout(path) as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        continue
                    rows += 1
                    stamp = datetime.fromisoformat(str(row.get('timestamp', '')).replace('Z', '+00:00'))
                    last = max(last, stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc))
                except ValueError:
                    continue
    except (OSError, EOFError):
        rows = 0
    return bool(rows), last, rows, path.suffix == '.jsonl'


@contextmanager
def open_rollout(path: Path):
    if path.suffix != '.zst':
        with path.open(encoding='utf-8-sig', errors='replace') as stream:
            yield stream
        return
    # Python 3.14+, or an existing system decoder on older hosts. Stream large
    # rollouts through a pipe and surface incomplete/corrupt frames to the caller.
    try:
        from compression import zstd
    except ImportError:
        zstd = None
    if zstd is not None:
        try:
            with zstd.open(path, 'rt', encoding='utf-8-sig', errors='replace') as stream:
                yield stream
        except zstd.ZstdError as exc:
            raise OSError(f'Failed to decompress rollout {path}') from exc
        return
    decoder = shutil.which('zstd')
    if decoder:
        cmd = [decoder, '-dc', '--', str(path.resolve())]
    elif Path('/usr/lib/apt/apt-helper').is_file():
        cmd = ['/usr/lib/apt/apt-helper', 'cat-file', str(path.resolve())]
    else:
        raise OSError(f'A Zstandard decoder is required to audit {path.name}')
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, encoding='utf-8-sig', errors='replace') as proc:
        try:
            yield proc.stdout
        except BaseException:
            proc.terminate()
            raise
        finally:
            proc.stdout.close()
        if proc.wait():
            raise OSError(f'Failed to decompress rollout {path}')


def filename_session_id(path: Path) -> str:
    matches = UUID_RE.findall(path.stem)
    return matches[-1].lower() if matches else ''


def select_session_meta(path: Path, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    metas = [r['payload'] for r in rows if r.get('type') == 'session_meta' and isinstance(r.get('payload'), dict)]
    expected = filename_session_id(path)
    if expected:
        for meta in metas:
            if str(meta.get('id', '')).lower() == expected:
                return meta
        # An inherited parent's ID is never a substitute for missing own metadata.
        return {'id': expected, 'identity_unresolved': True}
    return metas[0] if metas else {}
