#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

import kb_audit_runtime_value
import kb_session_brief
from kb_lib import append_jsonl, kb_base_dir, now_iso, read_jsonl, runtime_file
from kb_runtime import attach_runtime_scope, is_test_event


def closeout_path(base_dir: Path) -> Path:
    return runtime_file("closeout.jsonl", base_dir=base_dir)


def session_briefs_path(base_dir: Path) -> Path:
    return runtime_file("session_briefs.jsonl", base_dir=base_dir)


def quality_log_path(base_dir: Path) -> Path:
    return runtime_file("runtime_quality_log.jsonl", base_dir=base_dir)


def watch_state_path(base_dir: Path) -> Path:
    return runtime_file("runtime_quality_watch_state.json", base_dir=base_dir)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"files": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"files": {}}
    if not isinstance(payload, dict):
        return {"files": {}}
    files = payload.get("files")
    return {"files": files if isinstance(files, dict) else {}}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _read_new_rows(path: Path, offset: int) -> tuple[int, list[tuple[int, dict[str, Any]]], bool]:
    if not path.exists():
        return 0, [], False

    size = path.stat().st_size
    truncated = False
    safe_offset = max(0, offset)
    if safe_offset > size:
        safe_offset = 0
        truncated = True

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        if safe_offset:
            handle.seek(safe_offset)
        chunk = handle.read()
        new_offset = handle.tell()

    if not chunk:
        return new_offset, [], truncated

    rows: list[tuple[int, dict[str, Any]]] = []
    line_no = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            while handle.tell() < safe_offset:
                handle.readline()
                line_no += 1
    except OSError:
        line_no = 0

    for raw_line in chunk.splitlines():
        line_no += 1
        if not raw_line.strip():
            continue
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError:
            rows.append((line_no, {"_invalid_json_line": raw_line}))
            continue
        if isinstance(parsed, dict):
            rows.append((line_no, parsed))
    return new_offset, rows, truncated


def _current_brief_counts(base_dir: Path) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    path = session_briefs_path(base_dir)
    if not path.exists():
        return counts
    for row in read_jsonl(path):
        if row.get("event") != "kb_session_brief":
            continue
        if not kb_session_brief._is_current(row):
            continue
        coord = (str(row.get("repo", "")), str(row.get("branch", "")))
        counts[coord] = counts.get(coord, 0) + 1
    return counts


def evaluate_closeout(row: dict[str, Any]) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    warnings: list[str] = []

    if row.get("event") != "kb_closeout":
        issues.append("unexpected closeout event type")
        return issues, warnings

    hit_count = _coerce_int(row.get("hit_count"))
    rag_calls = _coerce_int(row.get("rag_calls"))
    used = _as_list(row.get("used_entry_ids"))
    written = _as_list(row.get("written_entry_ids"))
    updated = _as_list(row.get("updated_entry_ids"))
    hit_entry_ids = set(_as_list(row.get("hit_entry_ids")))
    heated = set(_as_list(row.get("heated_entry_ids")))
    skipped_reason = str(row.get("skipped_reason", "") or "").strip()
    linked_retrieval_ids = _as_list(row.get("linked_retrieval_ids"))
    brief_telemetry_present = "session_brief_hit" in row and "session_brief_help" in row

    if hit_count > 0 and rag_calls <= 0:
        issues.append("hit_count>0 but rag_calls<=0")
    if not (used or written or updated) and not skipped_reason:
        issues.append("missing skipped_reason for no-action closeout")
    if hit_entry_ids:
        invalid_used = [entry_id for entry_id in used if entry_id not in hit_entry_ids]
        if invalid_used:
            issues.append("used entries fall outside hit_entry_ids")
            warnings.append("invalid used ids: " + ",".join(invalid_used[:5]))
    if used and not _as_list(row.get("heat_failed_entry_ids")) and not heated.issuperset(set(used)):
        warnings.append("used entries were not fully heated")
    if row.get("session_brief_ids") == [] and str(row.get("session_brief_skipped_reason", "") or "").strip():
        warnings.append("session brief skipped: " + str(row.get("session_brief_skipped_reason")))
    if rag_calls > 0 and not linked_retrieval_ids:
        warnings.append("rag_calls>0 but linked_retrieval_ids missing")
    if not brief_telemetry_present:
        warnings.append("session brief telemetry missing")

    return issues, warnings


def evaluate_session_brief(
    row: dict[str, Any],
    *,
    base_dir: Path,
    keep_current: int,
) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    warnings: list[str] = []

    if row.get("event") != "kb_session_brief":
        issues.append("unexpected session brief event type")
        return issues, warnings

    title = str(row.get("title", "") or "").strip()
    summary = str(row.get("summary", "") or "").strip()
    if not title:
        issues.append("empty brief title")
    if not summary:
        issues.append("empty brief summary")
    if len(summary) > 600:
        warnings.append("brief summary exceeds expected 600-char clip")

    if kb_session_brief._is_current(row):
        counts = _current_brief_counts(base_dir)
        coord = (str(row.get("repo", "")), str(row.get("branch", "")))
        current_count = counts.get(coord, 0)
        limit = min(3, max(1, keep_current))
        if current_count > limit:
            issues.append(f"too many current briefs for repo/branch: {current_count}>{limit}")

    return issues, warnings


def make_quality_event(
    *,
    source: str,
    source_path: Path,
    line_no: int,
    row: dict[str, Any],
    issues: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    status = "issue" if issues else ("warn" if warnings else "ok")
    payload = {
        "ts": now_iso(),
        "event": "kb_runtime_quality_observation",
        "status": status,
        "source": source,
        "source_path": str(source_path),
        "source_line": line_no,
        "source_event": row.get("event", ""),
        "source_id": row.get("id", ""),
        "source_ts": row.get("ts", ""),
        "repo": row.get("repo", ""),
        "branch": row.get("branch", ""),
        "issues": issues,
        "warnings": warnings,
    }
    if source == "closeout":
        payload["hit_count"] = _coerce_int(row.get("hit_count"))
        payload["rag_calls"] = _coerce_int(row.get("rag_calls"))
        payload["used_count"] = len(_as_list(row.get("used_entry_ids")))
    return attach_runtime_scope(payload)


def make_repair_event(*, base_dir: Path, changed_groups: int) -> dict[str, Any]:
    return attach_runtime_scope({
        "ts": now_iso(),
        "event": "kb_runtime_quality_repair",
        "status": "repair",
        "source": "session_brief",
        "source_path": str(session_briefs_path(base_dir)),
        "changed_groups": changed_groups,
        "note": "maintained current brief window",
    })


def process_once(
    *,
    base_dir: Path,
    state_path: Path,
    log_path: Path,
    keep_current: int,
    repair_session_briefs: bool,
) -> dict[str, Any]:
    state = _load_state(state_path)
    state_files = state.setdefault("files", {})
    observed = 0
    issue_count = 0
    warn_count = 0
    closeout_checked_count = 0
    linked_retrieval_id_missing_count = 0
    session_brief_telemetry_missing_count = 0
    closeout_integrity_missing_count = 0
    repaired_groups = 0
    excluded_test_rows = 0
    truncated_sources: list[str] = []

    sources = [
        ("closeout", closeout_path(base_dir)),
        ("session_brief", session_briefs_path(base_dir)),
    ]

    for source_name, source_path in sources:
        source_state = state_files.get(str(source_path), {})
        offset = _coerce_int(source_state.get("offset"))
        new_offset, rows, truncated = _read_new_rows(source_path, offset)
        if truncated:
            truncated_sources.append(source_name)
        state_files[str(source_path)] = {"offset": new_offset}

        for line_no, row in rows:
            observed += 1
            if "_invalid_json_line" not in row and is_test_event(row):
                excluded_test_rows += 1
                continue
            if "_invalid_json_line" in row:
                event = make_quality_event(
                    source=source_name,
                    source_path=source_path,
                    line_no=line_no,
                    row={"event": "invalid_json"},
                    issues=["invalid json line"],
                    warnings=[],
                )
            else:
                if source_name == "closeout":
                    issues, warnings = evaluate_closeout(row)
                else:
                    issues, warnings = evaluate_session_brief(row, base_dir=base_dir, keep_current=keep_current)
                event = make_quality_event(
                    source=source_name,
                    source_path=source_path,
                    line_no=line_no,
                    row=row,
                    issues=issues,
                    warnings=warnings,
                )

            issue_count += len(event.get("issues", []))
            warn_count += len(event.get("warnings", []))
            if source_name == "closeout":
                closeout_checked_count += 1
                warnings = event.get("warnings", [])
                linked_missing = "rag_calls>0 but linked_retrieval_ids missing" in warnings
                brief_missing = "session brief telemetry missing" in warnings
                linked_retrieval_id_missing_count += int(linked_missing)
                session_brief_telemetry_missing_count += int(brief_missing)
                if linked_missing or brief_missing:
                    closeout_integrity_missing_count += 1
            append_jsonl(log_path, event)

    if repair_session_briefs:
        _path, changed = kb_session_brief.maintain_current_briefs(base_dir=base_dir, keep_current=keep_current)
        if changed:
            repaired_groups = changed
            append_jsonl(log_path, make_repair_event(base_dir=base_dir, changed_groups=changed))

    _save_state(state_path, state)
    return attach_runtime_scope({
        "ts": now_iso(),
        "event": "kb_runtime_quality_watch_summary",
        "observed_rows": observed,
        "issue_count": issue_count,
        "warn_count": warn_count,
        "closeout_checked_count": closeout_checked_count,
        "linked_retrieval_id_missing_count": linked_retrieval_id_missing_count,
        "session_brief_telemetry_missing_count": session_brief_telemetry_missing_count,
        "closeout_integrity_missing_count": closeout_integrity_missing_count,
        "repaired_groups": repaired_groups,
        "excluded_test_rows": excluded_test_rows,
        "truncated_sources": truncated_sources,
        "quality_log_path": str(log_path),
    })


def _print_summary(summary: dict[str, Any], *, include_runtime_value: bool, base_dir: Path) -> None:
    payload = dict(summary)
    if include_runtime_value:
        payload["runtime_value"] = kb_audit_runtime_value.build_report(
            path=closeout_path(base_dir),
            last_days=7,
        )
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Watch closeout/session brief runtime files, record quality observations, and optionally repair brief current-window drift."
    )
    parser.add_argument("--root", default="", help="Override personal-kb repos root")
    parser.add_argument("--watch", action="store_true", help="Keep polling for new runtime rows")
    parser.add_argument("--interval", type=float, default=2.0, help="Polling interval seconds for --watch")
    parser.add_argument("--keep-current", type=int, default=2, help="Expected current brief count per repo/branch (1-3)")
    parser.add_argument("--repair-session-briefs", action="store_true", help="Actively maintain current brief window after each poll")
    parser.add_argument("--state-file", default="", help="Override watch state JSON path")
    parser.add_argument("--quality-log", default="", help="Override runtime quality log JSONL path")
    parser.add_argument("--runtime-value", action="store_true", help="Include current runtime value summary in stdout output")
    parser.add_argument("--max-loops", type=int, default=0, help="Stop after N polling loops when --watch is used; 0 means forever")
    args = parser.parse_args(argv)

    base_dir = Path(args.root).expanduser() if args.root else kb_base_dir()
    state_file = Path(args.state_file).expanduser() if args.state_file else watch_state_path(base_dir)
    log_file = Path(args.quality_log).expanduser() if args.quality_log else quality_log_path(base_dir)

    loops = 0
    while True:
        summary = process_once(
            base_dir=base_dir,
            state_path=state_file,
            log_path=log_file,
            keep_current=args.keep_current,
            repair_session_briefs=args.repair_session_briefs,
        )
        _print_summary(summary, include_runtime_value=args.runtime_value, base_dir=base_dir)
        if not args.watch:
            break
        loops += 1
        if args.max_loops > 0 and loops >= args.max_loops:
            break
        time.sleep(max(0.2, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
