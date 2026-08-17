#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from kb_kinds import DEFAULT_KIND, VALID_KINDS, legacy_type_to_kind
from kb_lib import kb_base_dir, now_iso, read_jsonl


CORE_TRIGGER_KINDS = {"issue", "map", "pitfall"}
LIST_FIELDS = {"tags", "aliases", "trigger_terms", "source_paths", "key_files", "previous_ids"}
BOOL_FIELDS = {"_archived", "_deleted", "verified"}
ARTIFACT_TITLE_MARKERS = ("已生成", "已补齐", "训练卡", "初稿", "参考已沉淀")
PRIMARY_SUFFIXES = (
    ".py", ".java", ".kt", ".js", ".ts", ".tsx", ".yml", ".yaml", ".toml",
    ".xml", ".sql", ".log", ".properties", ".gradle", ".gitignore",
)


def _words(value: str) -> list[str]:
    parts = re.split(r"[\s,，、。；;：:|/\\()\[\]{}<>\"'`]+", value or "")
    return [p.strip() for p in parts if len(p.strip()) >= 2]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


def _bool_or_original(value: Any) -> Any:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return value


def _dedupe(values: list[str], *, limit: int = 12) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _infer_kind(entry: dict[str, Any]) -> tuple[str, str | None]:
    kind = str(entry.get("kind") or "").strip()
    if kind in VALID_KINDS:
        return kind, None

    legacy = str(entry.get("type") or entry.get("legacy_type") or "").strip()
    mapped = legacy_type_to_kind(legacy)
    if mapped:
        return mapped, legacy

    return DEFAULT_KIND, legacy or kind or None


def _bucket_metadata(root: Path, kb_file: Path) -> tuple[str, str, str]:
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
    return repo, branch, branch


def _split_typed_evidence(source_paths: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    paths: list[str] = []
    refs: list[dict[str, str]] = []
    for value in source_paths:
        prefix, separator, ref_value = value.partition(":")
        ref_value = ref_value.strip()
        if separator and prefix.strip().lower() == "commit" and ref_value:
            refs.append({"type": "git_commit", "value": ref_value})
        elif separator and prefix.strip().lower() == "conversation" and ref_value:
            refs.append({"type": "conversation", "value": ref_value})
        else:
            paths.append(value)
    return paths, refs


def _path_exists(value: str, *, root: Path) -> bool:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root.parent.parent / path
    return path.exists()


def _evidence_level(entry: dict[str, Any]) -> str:
    if entry.get("artifact_locator"):
        return "derived_artifact"
    refs = entry.get("evidence_refs") if isinstance(entry.get("evidence_refs"), list) else []
    if any(isinstance(ref, dict) and ref.get("type") == "conversation" for ref in refs):
        return "canonical_or_user_confirmed"
    paths = _string_list(entry.get("source_paths"))
    has_primary = any(str(path).lower().endswith(PRIMARY_SUFFIXES) or "/src/" in str(path).lower() for path in paths)
    has_derived = any("/interview-java-backend/" in str(path) or str(path).startswith("docs/") for path in paths)
    if has_primary and has_derived:
        return "mixed"
    if has_primary:
        return "primary"
    if has_derived:
        return "derived_verified"
    return "documented"


def _normalize_entry(entry: dict[str, Any], *, root: Path, kb_file: Path) -> tuple[dict[str, Any], list[str]]:
    e = dict(entry)
    changes: list[str] = []

    for field in sorted(LIST_FIELDS):
        if field in e and isinstance(e[field], str):
            e[field] = _string_list(e[field])
            changes.append(f"{field}:list")

    for field in sorted(BOOL_FIELDS):
        if field in e:
            new_value = _bool_or_original(e[field])
            if new_value is not e[field] and new_value != e[field]:
                e[field] = new_value
                changes.append(f"{field}:bool")

    if "used_count" not in e:
        e["used_count"] = 0
        changes.append("used_count")

    if "type" in e or e.get("kind") not in VALID_KINDS:
        kind, legacy = _infer_kind(e)
        old_kind = e.get("kind")
        old_type = e.pop("type", None)
        e["kind"] = kind
        if old_type and old_type != kind and "legacy_type" not in e:
            e["legacy_type"] = old_type
        elif legacy and legacy != kind and "legacy_type" not in e:
            e["legacy_type"] = legacy
        changes.append(f"kind:{old_kind or old_type}->{kind}")

    if not e.get("aliases"):
        aliases = _dedupe(_string_list(e.get("tags")) + _words(str(e.get("title", ""))), limit=6)
        if aliases:
            e["aliases"] = aliases
            changes.append("aliases")

    if e.get("kind") in CORE_TRIGGER_KINDS and not e.get("trigger_terms"):
        trigger_terms = _dedupe(
            _string_list(e.get("aliases"))
            + _string_list(e.get("tags"))
            + _words(str(e.get("title", ""))),
            limit=10,
        )
        if trigger_terms:
            e["trigger_terms"] = trigger_terms
            changes.append("trigger_terms")

    if not e.get("source_paths") and e.get("key_files"):
        key_files = _string_list(e.get("key_files"))
        if key_files:
            e["source_paths"] = key_files
            changes.append("source_paths")

    source_paths, evidence_refs = _split_typed_evidence(_string_list(e.get("source_paths")))
    if source_paths != _string_list(e.get("source_paths")):
        e["source_paths"] = source_paths
        changes.append("source_paths:typed_refs")
    if evidence_refs:
        existing_refs = e.get("evidence_refs") if isinstance(e.get("evidence_refs"), list) else []
        for ref in evidence_refs:
            if ref not in existing_refs:
                existing_refs.append(ref)
        e["evidence_refs"] = existing_refs
        changes.append("evidence_refs")

    title = str(e.get("title") or "")
    if e.get("kind") == "experience" and any(marker in title for marker in ARTIFACT_TITLE_MARKERS):
        e["kind"] = "map"
        e["artifact_locator"] = True
        changes.append("kind:experience->map:artifact_locator")

    if not str(e.get("status") or "").strip():
        e["status"] = "current"
        changes.append("status")

    evidence_level = _evidence_level(e)
    if not e.get("evidence_level"):
        e["evidence_level"] = evidence_level
        changes.append("evidence_level")

    if not e.get("authority"):
        if e.get("artifact_locator"):
            e["authority"] = "artifact_locator"
        elif evidence_level == "canonical_or_user_confirmed":
            e["authority"] = "user_confirmed"
        elif evidence_level in {"primary", "mixed"}:
            e["authority"] = "current_evidence"
        else:
            e["authority"] = "verified_summary"
        changes.append("authority")

    evidence_present = any(_path_exists(path, root=root) for path in _string_list(e.get("source_paths"))) or bool(e.get("evidence_refs"))
    if evidence_present and not e.get("verified_at"):
        e["verified_at"] = now_iso()
        e["verification_scope"] = "artifact_presence" if e.get("artifact_locator") else "evidence_reference_presence"
        changes.append("verified_at")

    repo, branch, branch_dir = _bucket_metadata(root, kb_file)
    old_location = {
        "repo": e.get("repo"),
        "branch": e.get("branch"),
        "branch_dir": e.get("branch_dir"),
    }
    if old_location != {"repo": repo, "branch": branch, "branch_dir": branch_dir}:
        if any(old_location.values()):
            history = e.get("previous_location_metadata")
            if not isinstance(history, list):
                history = []
            if old_location not in history:
                history.append(old_location)
            e["previous_location_metadata"] = history
        e["repo"] = repo
        e["branch"] = branch
        e["branch_dir"] = branch_dir
        changes.append("bucket_metadata")

    if changes:
        e["normalized_ts"] = now_iso()

    return e, changes


def _fix_duplicate_ids(entries_by_file: dict[Path, list[dict[str, Any]]]) -> int:
    seen: dict[str, tuple[Path, int]] = {}
    changed = 0
    used_ids: set[str] = set()

    for kb_file in sorted(entries_by_file):
        for entry in entries_by_file[kb_file]:
            eid = str(entry.get("id") or "").strip()
            if eid:
                used_ids.add(eid)

    for kb_file in sorted(entries_by_file):
        for idx, entry in enumerate(entries_by_file[kb_file]):
            eid = str(entry.get("id") or "").strip()
            if not eid:
                continue
            if eid not in seen:
                seen[eid] = (kb_file, idx)
                continue

            seed = f"{eid}|{kb_file}|{idx}|{entry.get('title','')}"
            new_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
            counter = 1
            while new_id in used_ids:
                counter += 1
                new_id = hashlib.sha256(f"{seed}|{counter}".encode("utf-8")).hexdigest()[:8]
            prev = _string_list(entry.get("previous_ids"))
            if eid not in prev:
                prev.insert(0, eid)
            entry["previous_ids"] = prev
            entry["id"] = new_id
            entry["normalized_ts"] = now_iso()
            used_ids.add(new_id)
            changed += 1

    return changed


def _write_jsonl(path: Path, entries: list[dict[str, Any]], *, backup: bool) -> None:
    if backup and path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.bak-normalize-{stamp}"))
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize personal-kb storage to current 6-kind schema.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Default is dry-run.")
    parser.add_argument("--root", default="", help="Override KB repos root; accepts personal-kb or personal-kb/repos.")
    parser.add_argument("--no-backup", action="store_true", help="Do not create per-file .bak-normalize backups when applying.")
    parser.add_argument("--skip-duplicate-id-fix", action="store_true", help="Only report duplicate IDs; do not rewrite duplicate IDs.")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser() if args.root else kb_base_dir()
    if root.name != "repos":
        root = root / "repos"
    if not root.exists():
        print(json.dumps({"ok": False, "error": f"KB repos root not found: {root}"}, ensure_ascii=False))
        return 2

    entries_by_file: dict[Path, list[dict[str, Any]]] = {}
    file_changes: dict[str, int] = {}
    dirty_files: set[Path] = set()
    entry_changes = 0

    for kb_file in sorted(root.rglob("kb.jsonl")):
        raw_entries = read_jsonl(kb_file)
        new_entries: list[dict[str, Any]] = []
        changed_in_file = 0
        for entry in raw_entries:
            normalized, changes = _normalize_entry(entry, root=root, kb_file=kb_file)
            if changes:
                changed_in_file += 1
                entry_changes += 1
            new_entries.append(normalized)
        entries_by_file[kb_file] = new_entries
        if changed_in_file:
            file_changes[str(kb_file)] = changed_in_file
            dirty_files.add(kb_file)

    duplicate_id_changes = 0
    if not args.skip_duplicate_id_fix:
        duplicate_id_changes = _fix_duplicate_ids(entries_by_file)
        if duplicate_id_changes:
            dirty_files.update(entries_by_file.keys())

    changed_files = []
    if args.apply:
        for kb_file, entries in entries_by_file.items():
            if kb_file in dirty_files:
                _write_jsonl(kb_file, entries, backup=not args.no_backup)
                changed_files.append(str(kb_file))

    result = {
        "ok": True,
        "mode": "apply" if args.apply else "dry-run",
        "root": str(root),
        "files_scanned": len(entries_by_file),
        "files_with_schema_changes": len(file_changes),
        "entries_normalized": entry_changes,
        "duplicate_ids_rewritten": duplicate_id_changes,
        "changed_files": changed_files if args.apply else list(file_changes.keys()),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
