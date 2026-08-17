#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

from kb_lib import kb_base_dir, read_jsonl, runtime_file
from kb_runtime import is_test_event


def default_closeout_path(base_dir: Path | None = None) -> Path:
    effective_base = kb_base_dir() if base_dir is None else base_dir
    return runtime_file("closeout.jsonl", base_dir=effective_base)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _rate_or_none(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _row_sample(rows: list[dict[str, Any]], predicate, limit: int = 6) -> list[str]:
    samples: list[str] = []
    for row in rows:
        if not predicate(row):
            continue
        label = str(row.get("closeout_id") or row.get("session_id") or "")
        ts = str(row.get("ts") or "")
        samples.append(f"{label}@{ts}" if label and ts else label or ts)
        if len(samples) >= limit:
            break
    return samples


def build_report(
    *,
    path: Path,
    last_days: int,
    include_test: bool = False,
) -> dict[str, Any]:
    rows = read_jsonl(path) if path.exists() else []
    since = datetime.now(timezone.utc) - timedelta(days=max(1, last_days))

    recent_rows: list[dict[str, Any]] = []
    skipped_invalid_ts = 0
    excluded_test_rows = 0
    for row in rows:
        if row.get("event") != "kb_closeout":
            continue
        ts = _parse_ts(row.get("ts"))
        if ts is None:
            skipped_invalid_ts += 1
            continue
        if ts < since:
            continue
        if not include_test and is_test_event(row):
            excluded_test_rows += 1
            continue
        recent_rows.append(row)

    closeout_total = len(recent_rows)
    hit_closeouts = sum(_as_int(row.get("hit_count")) > 0 for row in recent_rows)
    no_hit_closeouts = sum(_as_int(row.get("hit_count")) <= 0 for row in recent_rows)
    used_closeouts = sum(bool(_as_list(row.get("used_entry_ids"))) for row in recent_rows)
    used_hit_closeouts = sum(
        _as_int(row.get("hit_count")) > 0 and bool(_as_list(row.get("used_entry_ids")))
        for row in recent_rows
    )
    adopted_closeouts = sum(bool(_as_list(row.get("adopted_entry_ids"))) for row in recent_rows)
    adopted_hit_closeouts = sum(
        _as_int(row.get("hit_count")) > 0 and bool(_as_list(row.get("adopted_entry_ids")))
        for row in recent_rows
    )
    unconfirmed_used_closeouts = sum(
        bool(_as_list(row.get("used_entry_ids")))
        and not bool(_as_list(row.get("adopted_entry_ids")))
        for row in recent_rows
    )
    unconfirmed_used_hit_closeouts = sum(
        _as_int(row.get("hit_count")) > 0
        and bool(_as_list(row.get("used_entry_ids")))
        and not bool(_as_list(row.get("adopted_entry_ids")))
        for row in recent_rows
    )
    hit_not_used_closeouts = sum(
        _as_int(row.get("hit_count")) > 0 and not bool(_as_list(row.get("used_entry_ids")))
        for row in recent_rows
    )
    hit_not_adopted_closeouts = sum(
        _as_int(row.get("hit_count")) > 0 and not bool(_as_list(row.get("adopted_entry_ids")))
        for row in recent_rows
    )
    written_closeouts = sum(bool(_as_list(row.get("written_entry_ids"))) for row in recent_rows)
    updated_closeouts = sum(bool(_as_list(row.get("updated_entry_ids"))) for row in recent_rows)
    session_brief_hit_closeouts = sum(_as_bool(row.get("session_brief_hit")) for row in recent_rows)
    session_brief_help_closeouts = sum(_as_bool(row.get("session_brief_help")) for row in recent_rows)
    session_brief_telemetry_present = sum(
        "session_brief_hit" in row or "session_brief_help" in row
        for row in recent_rows
    )
    session_brief_telemetry_missing = closeout_total - session_brief_telemetry_present
    linked_retrieval_id_missing = sum(
        _as_int(row.get("rag_calls")) > 0
        and not bool(_as_list(row.get("linked_retrieval_ids")))
        for row in recent_rows
    )
    closeout_integrity_missing = sum(
        (
            _as_int(row.get("rag_calls")) > 0
            and not bool(_as_list(row.get("linked_retrieval_ids")))
        )
        or ("session_brief_hit" not in row or "session_brief_help" not in row)
        for row in recent_rows
    )
    closeout_integrity_missing_rate = _rate_or_none(
        closeout_integrity_missing,
        closeout_total,
    )
    self_reported_use_rate = _rate(used_hit_closeouts, hit_closeouts)
    confirmed_use_rate = _rate(adopted_hit_closeouts, hit_closeouts)
    adoption_confirmation_rate = _rate(adopted_closeouts, used_closeouts)
    session_brief_help_rate = _rate_or_none(session_brief_help_closeouts, session_brief_hit_closeouts)
    session_brief_telemetry_missing_rate = _rate_or_none(
        session_brief_telemetry_missing,
        closeout_total,
    )

    summary = (
        f"{last_days}d closeouts={closeout_total}, hits={hit_closeouts}, "
        f"used_hits={used_hit_closeouts}, confirmed_adoptions={adopted_hit_closeouts}, "
        f"self_reported_use_rate={self_reported_use_rate}, confirmed_use_rate={confirmed_use_rate}, "
        f"unconfirmed_used_hits={unconfirmed_used_hit_closeouts}, "
        f"linked_retrieval_id_missing={linked_retrieval_id_missing}, "
        f"closeout_integrity_missing={closeout_integrity_missing}, "
        f"excluded_test_rows={excluded_test_rows}, "
        f"session_brief_help_rate={'n/a' if session_brief_help_rate is None else session_brief_help_rate}, "
        f"session_brief_telemetry_missing={session_brief_telemetry_missing}"
    )

    return {
        "closeout_path": str(path),
        "last_days": max(1, last_days),
        "since": since.isoformat(timespec="seconds"),
        "closeout_total": closeout_total,
        "hit_closeouts": hit_closeouts,
        "used_closeouts": used_closeouts,
        "used_hit_closeouts": used_hit_closeouts,
        "self_reported_use_rate": self_reported_use_rate,
        "adopted_closeouts": adopted_closeouts,
        "adopted_hit_closeouts": adopted_hit_closeouts,
        "confirmed_use_rate": confirmed_use_rate,
        "adoption_confirmation_rate": adoption_confirmation_rate,
        "use_rate": confirmed_use_rate,
        "unconfirmed_used_closeouts": unconfirmed_used_closeouts,
        "unconfirmed_used_hit_closeouts": unconfirmed_used_hit_closeouts,
        "no_hit_closeouts": no_hit_closeouts,
        "hit_not_used_closeouts": hit_not_used_closeouts,
        "hit_not_adopted_closeouts": hit_not_adopted_closeouts,
        "written_closeouts": written_closeouts,
        "updated_closeouts": updated_closeouts,
        "session_brief_hit_closeouts": session_brief_hit_closeouts,
        "session_brief_help_closeouts": session_brief_help_closeouts,
        "session_brief_help_rate": session_brief_help_rate,
        "session_brief_telemetry_present": session_brief_telemetry_present,
        "session_brief_telemetry_missing": session_brief_telemetry_missing,
        "session_brief_telemetry_missing_rate": session_brief_telemetry_missing_rate,
        "linked_retrieval_id_missing": linked_retrieval_id_missing,
        "closeout_integrity_missing": closeout_integrity_missing,
        "closeout_integrity_missing_rate": closeout_integrity_missing_rate,
        "unconfirmed_used_sample": _row_sample(
            recent_rows,
            lambda row: bool(_as_list(row.get("used_entry_ids")))
            and not bool(_as_list(row.get("adopted_entry_ids"))),
        ),
        "confirmed_adoption_sample": _row_sample(
            recent_rows,
            lambda row: bool(_as_list(row.get("adopted_entry_ids"))),
        ),
        "skipped_invalid_ts_rows": skipped_invalid_ts,
        "excluded_test_rows": excluded_test_rows,
        "include_test": include_test,
        "summary": summary,
    }


def _print_text(report: dict[str, Any]) -> None:
    print(f"KB_RUNTIME_VALUE {report['summary']}")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit whether recent personal-kb closeouts are producing usable runtime value.")
    parser.add_argument("--closeout", default="", help="Override closeout.jsonl path")
    parser.add_argument("--last-days", type=int, default=7, help="Only include recent closeouts from the last N days")
    parser.add_argument("--text", action="store_true", help="Print a short text line before the JSON payload")
    parser.add_argument("--include-test", action="store_true", help="Include rows explicitly marked as test runtime")
    args = parser.parse_args(argv)

    path = Path(args.closeout).expanduser() if args.closeout else default_closeout_path()
    report = build_report(path=path, last_days=args.last_days, include_test=args.include_test)
    if args.text:
        _print_text(report)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
