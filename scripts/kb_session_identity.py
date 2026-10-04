"""Resolve a rollout's own identity without inheriting a parent's metadata."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

UUID_RE = re.compile(r'(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})')


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
