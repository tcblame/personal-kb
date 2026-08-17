#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kb_lib import append_jsonl, kb_base_dir, now_iso, read_jsonl, runtime_file


ADOPTION_EVENT = "kb_adoption"
VALID_EFFECTS = frozenset({"locate", "decide", "fix", "write", "legacy"})
HEAT_EFFECTS = frozenset({"decide", "fix", "write", "legacy"})


def adoption_events_path(base_dir: Path | None = None) -> Path:
    """Return the configured runtime adoption log."""
    effective_base = kb_base_dir() if base_dir is None else base_dir
    return runtime_file("adoption_events.jsonl", base_dir=effective_base)


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def append_adoption_event(
    entry_id: str,
    effect: str,
    repo: str,
    branch: str,
    event_id: str,
    session_id: str = "",
    ts: str = "",
    base_dir: Path | None = None,
) -> dict[str, str]:
    """Append one idempotent adoption event without rewriting durable KB data."""
    normalized_effect = _required_text(effect, "effect").lower()
    if normalized_effect not in VALID_EFFECTS:
        allowed = ", ".join(sorted(VALID_EFFECTS))
        raise ValueError(f"effect must be one of: {allowed}")

    event = {
        "event": ADOPTION_EVENT,
        "event_id": _required_text(event_id, "event_id"),
        "entry_id": _required_text(entry_id, "entry_id"),
        "effect": normalized_effect,
        "repo": repo.strip() if isinstance(repo, str) else "",
        "branch": branch.strip() if isinstance(branch, str) else "",
        "session_id": session_id.strip() if isinstance(session_id, str) else "",
        "ts": ts.strip() if isinstance(ts, str) and ts.strip() else now_iso(),
    }
    append_jsonl(adoption_events_path(base_dir), event)
    return event


def _timestamp_key(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def load_adoption_stats(base_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Aggregate adoption events, deduplicating physical retries by event_id."""
    stats: dict[str, dict[str, Any]] = {}
    latest_heat_keys: dict[str, float] = {}
    seen_event_ids: set[str] = set()

    for event in read_jsonl(adoption_events_path(base_dir)):
        event_type = _optional_text(event.get("event"))
        if event_type and event_type != ADOPTION_EVENT:
            continue
        event_id = _optional_text(event.get("event_id"))
        entry_id = _optional_text(event.get("entry_id"))
        effect = _optional_text(event.get("effect")).lower()
        if not event_id or not entry_id or effect not in VALID_EFFECTS:
            continue
        if event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)

        entry_stats = stats.setdefault(
            entry_id,
            {"heated_count": 0, "last_used_ts": "", "effects": {}},
        )
        effects = entry_stats["effects"]
        effects[effect] = effects.get(effect, 0) + 1

        if effect not in HEAT_EFFECTS:
            continue
        entry_stats["heated_count"] += 1
        event_ts = _optional_text(event.get("ts"))
        timestamp_key = _timestamp_key(event_ts)
        if timestamp_key is not None and timestamp_key >= latest_heat_keys.get(entry_id, float("-inf")):
            latest_heat_keys[entry_id] = timestamp_key
            entry_stats["last_used_ts"] = event_ts

    return stats


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def effective_usage(entry: dict[str, Any], stats: dict[str, dict[str, Any]]) -> int:
    """Combine immutable legacy heat with deduplicated runtime adoption heat."""
    entry_id = _optional_text(entry.get("id"))
    runtime = stats.get(entry_id, {}) if entry_id else {}
    return _nonnegative_int(entry.get("used_count")) + _nonnegative_int(runtime.get("heated_count"))
