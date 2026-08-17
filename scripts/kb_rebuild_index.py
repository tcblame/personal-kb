#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from kb_lib import cache_dir_for_records, kb_base_dir, now_iso, read_jsonl


KEYWORD_FIELDS = ("tags", "aliases", "trigger_terms")


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


def _is_inactive(entry: dict[str, Any]) -> bool:
    return bool(entry.get("_deleted") or entry.get("_archived"))


def _bucket_from_kb_path(root: Path, kb_file: Path) -> tuple[str, str, str]:
    rel = kb_file.relative_to(root)
    parts = rel.parts[:-1]
    if len(parts) >= 2:
        repo = "/".join(parts[:-1])
        branch = parts[-1]
    elif len(parts) == 1:
        repo = parts[0]
        branch = "no-git"
    else:
        repo = "unknown-repo"
        branch = "no-git"
    return repo, branch, "/".join(parts)


def _entry_keywords(entry: dict[str, Any]) -> list[str]:
    keywords: list[str] = []
    for field in KEYWORD_FIELDS:
        keywords.extend(_as_list(entry.get(field)))
    return sorted({kw.strip().lower() for kw in keywords if kw.strip()})


def _load_old_index(index_path: Path) -> dict[str, dict[str, Any]]:
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _build_inverted_index(root: Path, *, preserve_search_count: bool) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    index_path = cache_dir_for_records(root) / "_index" / "keywords.json"
    old = _load_old_index(index_path) if preserve_search_count else {}
    new: dict[str, dict[str, Any]] = {}
    stats = {"files": 0, "entries_seen": 0, "entries_indexed": 0, "keywords": 0}

    for kb_file in sorted(root.rglob("kb.jsonl")):
        stats["files"] += 1
        repo, branch, _ = _bucket_from_kb_path(root, kb_file)
        bucket = f"{repo}/{branch}"
        for entry in read_jsonl(kb_file):
            stats["entries_seen"] += 1
            if _is_inactive(entry):
                continue
            entry_id = str(entry.get("id") or "").strip()
            if not entry_id:
                continue
            keywords = _entry_keywords(entry)
            if not keywords:
                continue
            stats["entries_indexed"] += 1
            for keyword in keywords:
                old_info = old.get(keyword, {})
                info = new.setdefault(
                    keyword,
                    {
                        "entry_ids": [],
                        "buckets": [],
                        "search_count": old_info.get("search_count", 0),
                        "last_search": old_info.get("last_search"),
                    },
                )
                if entry_id not in info["entry_ids"]:
                    info["entry_ids"].append(entry_id)
                if bucket not in info["buckets"]:
                    info["buckets"].append(bucket)

    for info in new.values():
        info["entry_ids"].sort()
        info["buckets"].sort()
    stats["keywords"] = len(new)
    return new, stats


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _rebuild_bucket_indexes(root: Path, *, apply: bool, prune_orphans: bool) -> dict[str, Any]:
    stats = {
        "bucket_indexes_written": 0,
        "orphan_indexes": [],
        "orphan_indexes_removed": 0,
    }

    for kb_file in sorted(root.rglob("kb.jsonl")):
        repo, branch, branch_dir = _bucket_from_kb_path(root, kb_file)
        branch_path = kb_file.parent
        summary_path = branch_path / "summary.jsonl"
        index_path = branch_path / "index.json"
        data = {
            "updated_ts": now_iso(),
            "repo": repo,
            "branch": branch,
            "branch_dir": branch_dir,
            "paths": {
                "kb": str(kb_file),
                "summary": str(summary_path),
                "archive_dir": str(branch_path / "archive"),
                "attachments_dir": str(branch_path / "attachments"),
            },
            "kb": {
                "bytes": kb_file.stat().st_size if kb_file.exists() else 0,
                "entries": sum(1 for line in kb_file.read_text(encoding="utf-8").splitlines() if line.strip()),
            },
            "summary": {
                "bytes": summary_path.stat().st_size if summary_path.exists() else 0,
                "entries": sum(1 for line in summary_path.read_text(encoding="utf-8").splitlines() if line.strip()) if summary_path.exists() else 0,
            },
        }
        if apply:
            _write_json(index_path, data)
        stats["bucket_indexes_written"] += 1

    for index_path in sorted(root.rglob("index.json")):
        if not (index_path.parent / "kb.jsonl").exists():
            stats["orphan_indexes"].append(str(index_path))
            if apply and prune_orphans:
                index_path.unlink()
                stats["orphan_indexes_removed"] += 1

    return stats


def _clean_aggregation_bom(root: Path, *, apply: bool) -> dict[str, Any]:
    agg_dir = cache_dir_for_records(root) / "_aggregations"
    stats = {"aggregation_files": 0, "bom_files": [], "bom_cleaned": 0}
    if not agg_dir.exists():
        return stats
    for path in sorted(agg_dir.glob("*.jsonl")):
        stats["aggregation_files"] += 1
        data = path.read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            stats["bom_files"].append(str(path))
            if apply:
                path.write_bytes(data[3:])
                stats["bom_cleaned"] += 1
    return stats


def _write_field_usage(root: Path, *, apply: bool) -> dict[str, Any]:
    field_counts: dict[str, int] = {}
    by_kind: dict[str, dict[str, int]] = {}
    active_entries = 0
    for kb_file in sorted(root.rglob("kb.jsonl")):
        for entry in read_jsonl(kb_file):
            if _is_inactive(entry):
                continue
            active_entries += 1
            kind = str(entry.get("kind") or "")
            kind_counts = by_kind.setdefault(kind, {})
            for key, value in entry.items():
                if key.startswith("_") or value in ("", None, [], {}):
                    continue
                field_counts[key] = field_counts.get(key, 0) + 1
                kind_counts[key] = kind_counts.get(key, 0) + 1
    data = {
        "updated_ts": now_iso(),
        "active_entries": active_entries,
        "fields": dict(sorted(field_counts.items(), key=lambda item: (-item[1], item[0]))),
        "by_kind": {
            kind: dict(sorted(fields.items(), key=lambda item: (-item[1], item[0])))
            for kind, fields in sorted(by_kind.items())
        },
    }
    out = root.parent / "field_usage.json"
    if apply:
        _write_json(out, data)
    return {"field_usage_path": str(out), "active_entries": active_entries, "fields": len(field_counts)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild personal-kb indexes for the current Linux data root.")
    parser.add_argument("--apply", action="store_true", help="Write rebuilt indexes. Default is dry-run.")
    parser.add_argument("--root", default="", help="Override KB repos root; accepts personal-kb or personal-kb/repos.")
    parser.add_argument("--drop-search-count", action="store_true", help="Do not preserve keyword search_count/last_search from old index.")
    parser.add_argument("--prune-orphans", action="store_true", help="Delete index.json files that have no sibling kb.jsonl.")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser() if args.root else kb_base_dir()
    if root.name != "repos":
        root = root / "repos"
    if not root.exists():
        print(json.dumps({"ok": False, "error": f"KB repos root not found: {root}"}, ensure_ascii=False))
        return 2

    inverted, inverted_stats = _build_inverted_index(root, preserve_search_count=not args.drop_search_count)
    index_path = cache_dir_for_records(root) / "_index" / "keywords.json"
    if args.apply:
        _write_json(index_path, inverted)

    result = {
        "ok": True,
        "mode": "apply" if args.apply else "dry-run",
        "root": str(root),
        "inverted_index_path": str(index_path),
        "inverted": inverted_stats,
        "bucket_indexes": _rebuild_bucket_indexes(root, apply=args.apply, prune_orphans=args.prune_orphans),
        "aggregations": _clean_aggregation_bom(root, apply=args.apply),
        "field_usage": _write_field_usage(root, apply=args.apply),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
