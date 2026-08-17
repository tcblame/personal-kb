#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import kb_evidence
import kb_record_validation
from kb_lib import JsonlSafetyError, ensure_jsonl_safe, kb_base_dir


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _source_exists(value: str, *, workspace_root: Path) -> bool:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workspace_root / path
    return path.exists()


def _schema_version(value: Any) -> int:
    try:
        return int(value or 1)
    except (TypeError, ValueError):
        return 1


def _read_jsonl_strict(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]], int]:
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    non_empty_lines = 0
    ensure_jsonl_safe(path)
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw in enumerate(handle, start=1):
            text = raw.strip()
            if not text:
                continue
            non_empty_lines += 1
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                errors.append({"entry": f"{path}:{line_number}", "issue": f"invalid JSON: {exc.msg}"})
                continue
            if not isinstance(value, dict):
                errors.append({"entry": f"{path}:{line_number}", "issue": "JSONL row must be an object"})
                continue
            entries.append(value)
    return entries, errors, non_empty_lines


def audit(root: Path, *, keep_from: str) -> dict[str, Any]:
    if root.name != "repos":
        root = root / "repos"
    workspace_root = root.parent.parent
    files = sorted(root.rglob("kb.jsonl"))
    rows: list[dict[str, Any]] = []
    empty_files: list[str] = []
    violations: list[dict[str, str]] = []
    non_empty_lines = 0
    conflict_files = 0
    for path in files:
        try:
            entries, parse_errors, line_count = _read_jsonl_strict(path)
        except JsonlSafetyError as exc:
            conflict_files += 1
            violations.append({"entry": str(path), "issue": str(exc)})
            continue
        non_empty_lines += line_count
        violations.extend(parse_errors)
        if line_count == 0:
            empty_files.append(str(path))
        rows.extend(entries)

    ids: dict[str, int] = {}
    titles: dict[tuple[str, str, str], int] = {}
    source_total = 0
    source_resolved = 0
    artifact_locators = 0
    legacy_records = 0
    v2_records = 0
    freshness_counts: dict[str, int] = {}

    for entry in rows:
        entry_id = str(entry.get("id") or "").strip()
        title = str(entry.get("title") or "").strip()
        ids[entry_id] = ids.get(entry_id, 0) + 1
        title_key = (str(entry.get("repo") or ""), str(entry.get("branch") or ""), title.casefold())
        titles[title_key] = titles.get(title_key, 0) + 1
        prefix = entry_id or title or "<unknown>"

        if str(entry.get("ts") or "") < keep_from:
            violations.append({"entry": prefix, "issue": f"ts before keep_from: {entry.get('ts', '')}"})
        for field in ("status", "evidence_level", "authority"):
            if not str(entry.get(field) or "").strip():
                violations.append({"entry": prefix, "issue": f"missing {field}"})
        status = str(entry.get("status") or "").strip()
        if status not in {"draft_pending_evidence", "superseded", "archived"} and not str(entry.get("verified_at") or "").strip():
            violations.append({"entry": prefix, "issue": "missing verified_at"})

        if _schema_version(entry.get("schema_version")) >= 2:
            v2_records += 1
            record_rev = str(entry.get("record_rev") or "").strip()
            if len(record_rev) != 64 or any(ch not in "0123456789abcdef" for ch in record_rev.lower()):
                violations.append({"entry": prefix, "issue": "invalid v2 record_rev"})
            elif record_rev != kb_evidence.canonical_entry_revision(entry):
                violations.append({"entry": prefix, "issue": "record_rev does not match canonical content"})
            snapshots = entry.get("evidence_snapshots")
            if not isinstance(snapshots, list):
                violations.append({"entry": prefix, "issue": "v2 evidence_snapshots must be a list"})
            freshness = kb_evidence.verify_entry_evidence(entry)
            freshness_state = str(freshness.get("state") or "unresolvable")
            freshness_counts[freshness_state] = freshness_counts.get(freshness_state, 0) + 1
            if status not in {"draft_pending_evidence", "superseded", "archived"} and freshness_state != "fresh":
                violations.append({"entry": prefix, "issue": f"evidence freshness is {freshness_state}"})
            entry_workspace = Path(str(entry.get("workspace_dir") or workspace_root)).expanduser()
            for issue in kb_record_validation.strict_record_errors(
                entry,
                workspace_dir=entry_workspace,
                require_fresh_snapshot=True,
            ):
                violations.append({"entry": prefix, "issue": issue})
        else:
            legacy_records += 1

        sources = _string_list(entry.get("source_paths"))
        refs = entry.get("evidence_refs") if isinstance(entry.get("evidence_refs"), list) else []
        if not sources and not refs:
            violations.append({"entry": prefix, "issue": "missing evidence pointer"})
        resolved_for_entry = 0
        for source in sources:
            source_total += 1
            if source.startswith(("commit:", "conversation:")):
                violations.append({"entry": prefix, "issue": f"typed ref stored in source_paths: {source.split(':', 1)[0]}"})
            elif _source_exists(source, workspace_root=workspace_root):
                source_resolved += 1
                resolved_for_entry += 1
        ref_errors = kb_evidence.validate_evidence_refs(refs)
        valid_refs = refs if not ref_errors else []
        for issue in ref_errors:
            violations.append({"entry": prefix, "issue": issue})
        if resolved_for_entry == 0 and not valid_refs:
            violations.append({"entry": prefix, "issue": "no resolvable evidence for record"})

        if entry.get("artifact_locator"):
            artifact_locators += 1
            if entry.get("kind") != "map":
                violations.append({"entry": prefix, "issue": "artifact_locator must use kind=map"})

    duplicate_ids = sorted(key for key, count in ids.items() if key and count > 1)
    duplicate_titles = sorted("@".join(key) for key, count in titles.items() if key[2] and count > 1)
    for entry_id in duplicate_ids:
        violations.append({"entry": entry_id, "issue": "duplicate id"})
    for title in duplicate_titles:
        violations.append({"entry": title, "issue": "duplicate title"})
    for path in empty_files:
        violations.append({"entry": path, "issue": "empty bucket"})

    resolved_rate = (source_resolved / source_total) if source_total else 1.0
    if resolved_rate < 0.95:
        violations.append({"entry": "corpus", "issue": f"source resolve rate below 0.95: {resolved_rate:.3f}"})

    return {
        "ok": not violations,
        "root": str(root),
        "keep_from": keep_from,
        "records": len(rows),
        "non_empty_lines": non_empty_lines,
        "files": len(files),
        "conflict_files": conflict_files,
        "empty_files": len(empty_files),
        "artifact_locators": artifact_locators,
        "legacy_records": legacy_records,
        "v2_records": v2_records,
        "freshness_counts": freshness_counts,
        "source_total": source_total,
        "source_resolved": source_resolved,
        "source_resolved_rate": round(resolved_rate, 4),
        "duplicate_ids": duplicate_ids,
        "duplicate_titles": duplicate_titles,
        "violations": violations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit durable personal-kb records against runtime quality gates.")
    parser.add_argument("--root", default="", help="personal-kb root or repos root")
    parser.add_argument("--keep-from", default="2026-07-01", help="Reject records created before this ISO date")
    parser.add_argument("--strict", action="store_true", help="Exit 2 when any quality gate fails")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser() if args.root else kb_base_dir()
    result = audit(root, keep_from=args.keep_from)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if args.strict and not result["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
