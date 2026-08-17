#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

from kb_lib import (
    append_jsonl,
    bucket_lock,
    manifests_dir as configured_manifests_dir,
    now_iso,
    read_jsonl,
    retained_files_dir,
)
from kb_sensitive_scan import sensitive_findings


WINDOWS_RESERVED_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

VALID_CATEGORIES = {
    "logs",
    "screenshots",
    "conversation",
    "verification",
    "requirements",
    "configs",
    "attachments",
    "database",
    "resources",
    "datasets",
}

VALID_REFERENCE_KINDS = {"credential", "secret-manager", "external-resource"}

HIGH_CONFIDENCE_SECRET_PATTERNS = (
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("putty_private_key", re.compile(r"^PuTTY-User-Key-File-", re.M)),
)
def _write_json(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def retained_files_base_dir() -> Path:
    return retained_files_dir()


def manifests_dir() -> Path:
    return configured_manifests_dir()


def global_manifest_path() -> Path:
    return manifests_dir() / "retained-files.jsonl"


def _safe_dir_name(value: str, *, fallback: str) -> str:
    safe = (value or "").strip()
    safe = safe.replace("/", "-").replace("\\", "-")
    safe = re.sub(r'[<>:"|?*]', "_", safe)
    safe = re.sub(r"[\x00-\x1f]", "_", safe)
    safe = re.sub(r"\s+", "-", safe)
    safe = safe.strip().rstrip(". ")
    if not safe:
        safe = fallback
    if safe.lower() in WINDOWS_RESERVED_DEVICE_NAMES:
        safe = f"{fallback}-{safe}"
    return safe


def _safe_file_name(name: str) -> str:
    return _safe_dir_name(name, fallback="retained-file")


def _harden_local_file(path: Path) -> None:
    """Apply best-effort owner-only permissions without claiming encryption."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
        resolved_path = path.resolve(strict=False)
        for base in (retained_files_base_dir(), manifests_dir()):
            resolved_base = base.resolve(strict=False)
            try:
                resolved_path.relative_to(resolved_base)
            except ValueError:
                continue
            cursor = resolved_path.parent
            while True:
                os.chmod(cursor, 0o700)
                if cursor == resolved_base:
                    break
                cursor = cursor.parent
            break
    except OSError:
        pass


def _case_year(case_id: str) -> str:
    match = re.search(r"(?:^|-)(\d{6})(?:-\d+)?$", case_id)
    if match:
        yy = int(match.group(1)[:2])
        return f"20{yy:02d}"
    return now_iso()[:4]


def _asset_date(case_id: str) -> str:
    match = re.search(r"(?:^|-)(\d{6})(?:-\d+)?$", case_id)
    if match:
        return match.group(1)
    iso = now_iso()
    return f"{iso[2:4]}{iso[5:7]}{iso[8:10]}"


def case_archive_dir(project_key: str, case_id: str) -> Path:
    project_dir = _safe_dir_name(project_key, fallback="unknown-project")
    case_dir = _safe_dir_name(case_id, fallback="unknown-case")
    return retained_files_base_dir() / project_dir / _case_year(case_id) / case_dir


def _category_dir(project_key: str, case_id: str, category: str) -> Path:
    category_dir = _safe_dir_name(category, fallback="attachments")
    return case_archive_dir(project_key, case_id) / category_dir


def _case_manifest_path(project_key: str, case_id: str) -> Path:
    return case_archive_dir(project_key, case_id) / "manifest.json"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_case_manifest(project_key: str, case_id: str) -> dict[str, Any]:
    path = _case_manifest_path(project_key, case_id)
    if not path.exists():
        return {
            "project_key": project_key,
            "case_id": case_id,
            "archive_path": str(case_archive_dir(project_key, case_id)),
            "created_at": now_iso(),
            "updated_at": "",
            "assets": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("project_key", project_key)
    data.setdefault("case_id", case_id)
    data.setdefault("archive_path", str(case_archive_dir(project_key, case_id)))
    data.setdefault("created_at", now_iso())
    data.setdefault("assets", [])
    if not isinstance(data["assets"], list):
        data["assets"] = []
    return data


def _write_case_manifest(project_key: str, case_id: str, manifest: dict[str, Any]) -> None:
    path = _case_manifest_path(project_key, case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = now_iso()

    fd, temp_path_str = tempfile.mkstemp(dir=path.parent, prefix=".manifest-", suffix=".json")
    temp_path = Path(temp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            if sys.platform != "win32":
                os.fchmod(f.fileno(), 0o600)
        if sys.platform == "win32" and path.exists():
            path.unlink()
        shutil.move(str(temp_path), str(path))
        _harden_local_file(path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise


def _next_asset_id(case_id: str) -> str:
    date_part = _asset_date(case_id)
    prefix = f"asset_{date_part}_"
    max_seen = 0
    for row in read_jsonl(global_manifest_path()):
        asset_id = str(row.get("asset_id", ""))
        if not asset_id.startswith(prefix):
            continue
        suffix = asset_id[len(prefix):]
        if suffix.isdigit():
            max_seen = max(max_seen, int(suffix))
    return f"{prefix}{max_seen + 1:03d}"


def _dedupe_target(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    index = 2
    while True:
        candidate = parent / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _find_asset(asset_id: str) -> dict[str, Any] | None:
    for row in read_jsonl(global_manifest_path()):
        if row.get("asset_id") == asset_id:
            return row
    return None


def _find_duplicate_by_hash(sha256: str, size_bytes: int) -> dict[str, Any] | None:
    """Find an existing local evidence object that can be safely reused."""
    for row in read_jsonl(global_manifest_path()):
        if str(row.get("sha256", "")) != sha256:
            continue
        try:
            if int(row.get("size_bytes", -1)) != size_bytes:
                continue
        except (TypeError, ValueError):
            continue
        stored_path = str(row.get("stored_path", ""))
        if stored_path and Path(stored_path).is_file():
            return row
    return None


def _is_same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return str(left) == str(right)


def _safe_reference_locator(value: str) -> bool:
    """Reject pasted secret-shaped values while allowing a vault/item locator."""
    if not value or "\n" in value or "\r" in value:
        return False
    lowered = value.lower()
    secret_markers = ("password=", "passwd=", "token=", "secret=", "-----begin", "bearer ")
    if any(marker in lowered for marker in secret_markers):
        return False
    if sensitive_findings(value):
        return False
    return not any(pattern.search(value) for _label, pattern in HIGH_CONFIDENCE_SECRET_PATTERNS)


def _copy_or_move(source: Path, target: Path, mode: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == "copy":
        shutil.copy2(source, target)
    else:
        shutil.move(str(source), str(target))


def retain_file(args: argparse.Namespace) -> int:
    source = Path(args.path).expanduser()
    if not source.exists():
        sys.stderr.write(f"File not found: {source}\n")
        return 2
    if not source.is_file():
        sys.stderr.write(f"Not a file: {source}\n")
        return 2

    category = args.category.strip()
    if category not in VALID_CATEGORIES:
        sys.stderr.write(f"Invalid category: {category}\n")
        return 2

    project_key = args.project_key.strip()
    case_id = args.case_id.strip()
    if not project_key or not case_id:
        sys.stderr.write("--project-key and --case-id are required\n")
        return 2

    source_hash = _file_sha256(source)
    size_bytes = source.stat().st_size
    mode = args.mode
    target = _dedupe_target(_category_dir(project_key, case_id, category) / _safe_file_name(source.name))
    created_target = False
    storage_action = "copied" if mode == "copy" else "moved"
    duplicate_source = ""

    # The global manifest is the small coordination point for content dedupe.
    # A second case gets a new asset relation, but the bytes are stored once.
    try:
        with bucket_lock(global_manifest_path()):
            existing = None if getattr(args, "no_dedupe", False) else _find_duplicate_by_hash(source_hash, size_bytes)
            if existing:
                existing_path = Path(str(existing.get("stored_path", "")))
                target = existing_path
                storage_action = "reused"
                duplicate_source = str(existing.get("asset_id", ""))
                if mode == "move" and not _is_same_path(source, existing_path):
                    source.unlink()
            else:
                _copy_or_move(source, target, mode)
                created_target = True
                stored_hash = _file_sha256(target)
                if stored_hash != source_hash:
                    raise OSError("sha256 mismatch after copy/move")
                _harden_local_file(target)

            asset = {
                "asset_id": _next_asset_id(case_id),
                "project_key": project_key,
                "case_id": case_id,
                "category": category,
                "mode": mode,
                "storage_action": storage_action,
                "deduplicated_from_asset": duplicate_source,
                "origin_path": str(source),
                "stored_path": str(target),
                "sha256": source_hash,
                "size_bytes": size_bytes,
                "created_at": now_iso(),
                "reason": args.reason.strip(),
                "related_entry": args.related_entry.strip(),
                "local_only": True,
                "storage_protection": "filesystem-permissions-only",
                "status": "active",
            }

            manifest = _load_case_manifest(project_key, case_id)
            manifest["assets"].append(asset)
            _write_case_manifest(project_key, case_id, manifest)
            append_jsonl(global_manifest_path(), asset, lock_held=True)
            _harden_local_file(global_manifest_path())
    except (OSError, ValueError) as e:
        if created_target:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        sys.stderr.write(f"Failed to retain file: {e}\n")
        return 2

    summary = {
        "status": "ok",
        "action": "retain",
        "asset_id": asset["asset_id"],
        "project_key": project_key,
        "case_id": case_id,
        "category": category,
        "mode": mode,
        "storage_action": storage_action,
        "deduplicated_from_asset": duplicate_source,
        "archive_path": str(case_archive_dir(project_key, case_id)),
        "stored_path": str(target),
        "sha256": source_hash,
        "size_bytes": size_bytes,
        "local_only": True,
        "storage_protection": "filesystem-permissions-only",
    }
    _write_json(summary)
    return 0


def retain_reference(args: argparse.Namespace) -> int:
    """Record a resource locator; use retain when the actual bytes must be archived."""
    project_key = args.project_key.strip()
    case_id = args.case_id.strip()
    locator = args.locator.strip()
    reference_kind = args.reference_kind.strip()
    if not project_key or not case_id:
        sys.stderr.write("--project-key and --case-id are required\n")
        return 2
    if reference_kind not in VALID_REFERENCE_KINDS:
        sys.stderr.write(f"Invalid reference kind: {reference_kind}\n")
        return 2
    if not _safe_reference_locator(locator):
        sys.stderr.write("Reference locator looks like a pasted secret; provide only a vault/item reference.\n")
        return 4

    with bucket_lock(global_manifest_path()):
        asset = {
            "asset_id": _next_asset_id(case_id),
            "project_key": project_key,
            "case_id": case_id,
            "category": "references",
            "mode": "reference",
            "storage_action": "external_reference",
            "reference_kind": reference_kind,
            "locator": locator,
            "origin_path": "",
            "stored_path": "",
            "sha256": "",
            "size_bytes": 0,
            "created_at": now_iso(),
            "reason": args.reason.strip(),
            "related_entry": args.related_entry.strip(),
            "local_only": True,
            "status": "external_reference",
        }
        manifest = _load_case_manifest(project_key, case_id)
        manifest["assets"].append(asset)
        _write_case_manifest(project_key, case_id, manifest)
        append_jsonl(global_manifest_path(), asset, lock_held=True)
        _harden_local_file(global_manifest_path())

    _write_json({
        "status": "ok",
        "action": "reference",
        "asset_id": asset["asset_id"],
        "project_key": project_key,
        "case_id": case_id,
        "reference_kind": reference_kind,
        "locator": locator,
        "archive_path": str(case_archive_dir(project_key, case_id)),
    })
    return 0


def list_case(args: argparse.Namespace) -> int:
    project_key = args.project_key.strip()
    case_id = args.case_id.strip()
    manifest = _load_case_manifest(project_key, case_id)
    _write_json(
        {
            "status": "ok",
            "action": "list",
            "project_key": project_key,
            "case_id": case_id,
            "archive_path": str(case_archive_dir(project_key, case_id)),
            "assets": manifest.get("assets", []),
        }
    )
    return 0


def show_asset(args: argparse.Namespace) -> int:
    asset = _find_asset(args.asset_id.strip())
    if asset is None:
        sys.stderr.write(f"Asset not found: {args.asset_id}\n")
        return 2
    _write_json(asset)
    return 0


def path_case(args: argparse.Namespace) -> int:
    _write_json(
        {
            "status": "ok",
            "action": "path",
            "project_key": args.project_key.strip(),
            "case_id": args.case_id.strip(),
            "archive_path": str(case_archive_dir(args.project_key.strip(), args.case_id.strip())),
        }
    )
    return 0


def _verify_asset(asset: dict[str, Any]) -> dict[str, Any]:
    result = {
        "asset_id": asset.get("asset_id", ""),
        "stored_path": asset.get("stored_path", ""),
        "ok": False,
        "error": "",
    }
    if asset.get("storage_action") == "external_reference" or asset.get("mode") == "reference":
        result["ok"] = bool(str(asset.get("locator", "")).strip())
        result["error"] = "external_reference" if result["ok"] else "reference_missing"
        return result
    stored_path = Path(str(asset.get("stored_path", "")))
    if not stored_path.exists():
        result["error"] = "file_missing"
        return result
    try:
        actual_hash = _file_sha256(stored_path)
    except OSError:
        result["error"] = "read_failed"
        return result
    expected_hash = str(asset.get("sha256", ""))
    if actual_hash != expected_hash:
        result["error"] = "sha256_mismatch"
        result["actual_sha256"] = actual_hash
        result["expected_sha256"] = expected_hash
        return result
    result["ok"] = True
    return result


def verify_case(args: argparse.Namespace) -> int:
    project_key = args.project_key.strip()
    case_id = args.case_id.strip()
    manifest = _load_case_manifest(project_key, case_id)
    assets = [asset for asset in manifest.get("assets", []) if asset.get("status", "active") == "active"]
    results = [_verify_asset(asset) for asset in assets]
    ok = all(result["ok"] for result in results)
    _write_json(
        {
            "status": "ok" if ok else "failed",
            "action": "verify",
            "project_key": project_key,
            "case_id": case_id,
            "ok": ok,
            "assets": results,
        }
    )
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Retain original evidence files verbatim in the local plaintext personal-kb archive."
    )
    sub = parser.add_subparsers(dest="action")

    retain = sub.add_parser("retain", help="Copy or move one file verbatim into local retained-files.")
    retain.add_argument("--path", required=True, help="Source file path.")
    retain.add_argument("--project-key", required=True, help="Project key, e.g. study.")
    retain.add_argument("--case-id", required=True, help="Stable case id.")
    retain.add_argument("--category", required=True, choices=sorted(VALID_CATEGORIES))
    retain.add_argument("--mode", choices=["copy", "move"], default="copy")
    retain.add_argument("--no-dedupe", action="store_true", help="Keep a separate physical copy even when SHA-256 matches.")
    retain.add_argument("--reason", default="")
    retain.add_argument("--related-entry", default="")

    reference = sub.add_parser("reference", help="Record an external credential/resource locator without copying its value.")
    reference.add_argument("--project-key", required=True)
    reference.add_argument("--case-id", required=True)
    reference.add_argument("--reference-kind", choices=sorted(VALID_REFERENCE_KINDS), required=True)
    reference.add_argument("--locator", required=True, help="Vault/item or external resource locator; use retain for content bytes.")
    reference.add_argument("--reason", default="")
    reference.add_argument("--related-entry", default="")

    list_cmd = sub.add_parser("list", help="List retained assets for a case.")
    list_cmd.add_argument("--project-key", required=True)
    list_cmd.add_argument("--case-id", required=True)

    show = sub.add_parser("show", help="Show one retained asset by asset_id.")
    show.add_argument("--asset-id", required=True)

    verify = sub.add_parser("verify", help="Verify retained files for a case.")
    verify.add_argument("--project-key", required=True)
    verify.add_argument("--case-id", required=True)

    path = sub.add_parser("path", help="Return retained-files archive path for a case.")
    path.add_argument("--project-key", required=True)
    path.add_argument("--case-id", required=True)

    args = parser.parse_args(argv)
    if not args.action:
        parser.print_help()
        return 1

    if args.action == "retain":
        return retain_file(args)
    if args.action == "reference":
        return retain_reference(args)
    if args.action == "list":
        return list_case(args)
    if args.action == "show":
        return show_asset(args)
    if args.action == "verify":
        return verify_case(args)
    if args.action == "path":
        return path_case(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
