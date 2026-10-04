#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kb_lib import configure_utf8_streams

configure_utf8_streams()

from kb_lib import kb_base_dir, read_jsonl, runtime_file
from kb_runtime import is_test_event
from kb_command_contract import retrieval_linkage_present


FRESH_TOP3_STATES = {"fresh", "runtime_recent"}
ARTIFACT_QUERY_MARKERS = (
    "简历",
    "面试材料",
    "项目经历",
    "工作经历",
    "资料",
    "材料",
    "artifact_locator",
)


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


def _retrieval_items(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Read optional top-level retrieval telemetry without requiring it."""
    candidates: list[Any] = [
        row.get("retrieval_items"),
        row.get("top3_items"),
        row.get("retrieval_snapshot"),
        row.get("items"),
    ]
    extra = row.get("extra")
    if isinstance(extra, dict):
        candidates.extend(
            [extra.get("retrieval_items"), extra.get("top3_items"), extra.get("retrieval_snapshot")]
        )
    for value in candidates:
        if isinstance(value, dict):
            value = value.get("items")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _is_artifact_query(value: Any) -> bool:
    text = str(value or "").casefold()
    return any(marker.casefold() in text for marker in ARTIFACT_QUERY_MARKERS)


def _rejection_feedback(row: dict[str, Any]) -> tuple[list[str], dict[str, int]]:
    rejected: list[str] = []
    for value in _as_list(row.get("rejected_hit_ids")):
        entry_id = str(value).strip()
        if entry_id and entry_id not in rejected:
            rejected.append(entry_id)
    raw_reasons = row.get("rejection_reasons")
    reasons: dict[str, int] = {}
    reason_entry_ids: set[str] = set()
    if isinstance(raw_reasons, dict):
        for entry_id, reason in raw_reasons.items():
            normalized_entry_id = str(entry_id).strip()
            key = str(reason or "unknown").strip() or "unknown"
            reasons[key] = reasons.get(key, 0) + 1
            if normalized_entry_id:
                reason_entry_ids.add(normalized_entry_id)
                if normalized_entry_id not in rejected:
                    rejected.append(normalized_entry_id)
    for entry_id in rejected:
        if entry_id in reason_entry_ids:
            continue
        reasons["unknown"] = reasons.get("unknown", 0) + 1
    return rejected, reasons


def _top3_quality(row: dict[str, Any]) -> tuple[int, int, int, int, bool]:
    """Return stale/map counters and whether top-3 telemetry was present."""
    items = _retrieval_items(row)
    if not items:
        # Allow producers that already aggregate the counters in closeout.
        top3 = _as_int(row.get("top3_count"))
        stale = _as_int(row.get("stale_top3_count"))
        map_slots = _as_int(row.get("map_top3_count"))
        map_noise = _as_int(row.get("map_noise_count"))
        present = top3 > 0 or any(key in row for key in ("stale_top3_count", "map_noise_count"))
        return stale, top3, map_noise, map_slots, present
    top3_items = items[:3]
    stale = sum(
        str(item.get("freshness_state") or "legacy_unverified") not in FRESH_TOP3_STATES
        for item in top3_items
    )
    query = row.get("queries")
    query_text = " ".join(str(value) for value in query) if isinstance(query, list) else str(query or "")
    ordinary = not _is_artifact_query(query_text)
    map_slots = sum(str(item.get("kind") or "") == "map" for item in top3_items)
    map_noise = sum(ordinary and str(item.get("kind") or "") == "map" for item in top3_items)
    return stale, len(top3_items), map_noise, map_slots, True


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
        and not retrieval_linkage_present(row)
        for row in recent_rows
    )
    closeout_integrity_missing = sum(
        (
            _as_int(row.get("rag_calls")) > 0
            and not retrieval_linkage_present(row)
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

    stale_top3_count = 0
    top3_slot_count = 0
    map_noise_count = 0
    map_top3_slot_count = 0
    top3_quality_rows = 0
    rejection_reason_counts: dict[str, int] = {}
    rejected_hit_count = 0
    explicit_missing_closeout_count = 0
    explicit_expected_closeout_count = 0
    for row in recent_rows:
        stale, top3, map_noise, map_slots, present = _top3_quality(row)
        if present:
            top3_quality_rows += 1
            stale_top3_count += stale
            top3_slot_count += top3
            map_noise_count += map_noise
            map_top3_slot_count += map_slots
        rejected, row_reasons = _rejection_feedback(row)
        rejected_hit_count += len(rejected)
        for reason, count in row_reasons.items():
            rejection_reason_counts[reason] = rejection_reason_counts.get(reason, 0) + count
        # These optional counters are emitted by session/runtime bridges when
        # they can observe a retrieval without a matching closeout.  Do not
        # infer a false zero from old closeout rows that predate the bridge.
        explicit_missing_closeout_count += _as_int(row.get("missing_closeout_count"))
        explicit_missing_closeout_count += int(_as_bool(row.get("missing_closeout")))
        explicit_expected_closeout_count += _as_int(row.get("expected_closeout_count"))

    stale_top3_rate = _rate_or_none(stale_top3_count, top3_slot_count)
    map_noise_rate = _rate_or_none(map_noise_count, top3_slot_count)
    missing_closeout_rate = _rate_or_none(
        explicit_missing_closeout_count,
        explicit_expected_closeout_count,
    )
    confirmed_adoption_rate = confirmed_use_rate

    summary = (
        f"{last_days}d closeouts={closeout_total}, hits={hit_closeouts}, "
        f"used_hits={used_hit_closeouts}, confirmed_adoptions={adopted_hit_closeouts}, "
        f"self_reported_use_rate={self_reported_use_rate}, confirmed_use_rate={confirmed_use_rate}, "
        f"unconfirmed_used_hits={unconfirmed_used_hit_closeouts}, "
        f"linked_retrieval_id_missing={linked_retrieval_id_missing}, "
        f"closeout_integrity_missing={closeout_integrity_missing}, "
        f"excluded_test_rows={excluded_test_rows}, "
        f"session_brief_help_rate={'n/a' if session_brief_help_rate is None else session_brief_help_rate}, "
        f"session_brief_telemetry_missing={session_brief_telemetry_missing}, "
        f"stale_top3_rate={'n/a' if stale_top3_rate is None else stale_top3_rate}, "
        f"map_noise_rate={'n/a' if map_noise_rate is None else map_noise_rate}, "
        f"confirmed_adoption_rate={confirmed_adoption_rate}, "
        f"missing_closeout_rate={'n/a' if missing_closeout_rate is None else missing_closeout_rate}"
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
        "confirmed_adoption_rate": confirmed_adoption_rate,
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
        "stale_top3_count": stale_top3_count,
        "top3_slot_count": top3_slot_count,
        "stale_top3_rate": stale_top3_rate,
        "map_noise_count": map_noise_count,
        "map_top3_slot_count": map_top3_slot_count,
        "map_noise_rate": map_noise_rate,
        "top3_quality_rows": top3_quality_rows,
        "rejected_hit_count": rejected_hit_count,
        "rejection_reason_counts": rejection_reason_counts,
        "explicit_missing_closeout_count": explicit_missing_closeout_count,
        "explicit_expected_closeout_count": explicit_expected_closeout_count,
        "missing_closeout_rate": missing_closeout_rate,
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
    print(
        "- retrieval_quality: "
        f"stale_top3_rate={report.get('stale_top3_rate')} "
        f"map_noise_rate={report.get('map_noise_rate')} "
        f"rejected_hits={report.get('rejected_hit_count', 0)}"
    )
    print(
        "- lifecycle: "
        f"confirmed_adoption_rate={report.get('confirmed_adoption_rate')} "
        f"missing_closeout_rate={report.get('missing_closeout_rate')}"
    )
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
