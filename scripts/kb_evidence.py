#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


REVISION_EXCLUDED_FIELDS = frozenset({"record_rev", "used_count", "last_used_ts"})
FRESHNESS_STATES = frozenset(
    {
        "fresh",
        "needs_recheck",
        "dirty_worktree",
        "diverged",
        "unresolvable",
        "conflicted",
        "legacy_unverified",
        "not_snapshotted",
    }
)
VALID_EVIDENCE_REF_TYPES = frozenset({"git_commit", "conversation", "retained"})
GIT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")


def validate_evidence_refs(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return ["evidence_refs must be a list"]
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for index, ref in enumerate(value):
        label = f"evidence_refs[{index}]"
        if not isinstance(ref, dict):
            errors.append(f"{label} must be an object")
            continue
        ref_type = str(ref.get("type") or "").strip()
        ref_value = str(ref.get("value") or "").strip()
        if ref_type not in VALID_EVIDENCE_REF_TYPES:
            errors.append(
                f"{label}.type must be one of: {', '.join(sorted(VALID_EVIDENCE_REF_TYPES))}"
            )
            continue
        if not ref_value:
            errors.append(f"{label}.value is required")
            continue
        if ref_type == "git_commit" and not GIT_COMMIT_RE.fullmatch(ref_value):
            errors.append(f"{label}.value must be a 7-64 character hexadecimal Git commit")
        if ref_type == "retained":
            if not ASSET_ID_RE.fullmatch(ref_value):
                errors.append(f"{label}.value must be an opaque retained asset ID")
            relative = str(ref.get("path") or "").strip()
            if not relative:
                errors.append(f"{label}.path is required for retained evidence")
            else:
                path = Path(relative)
                if path.is_absolute() or ".." in path.parts:
                    errors.append(f"{label}.path must be a safe relative path")
        key = (ref_type, ref_value)
        if key in seen:
            errors.append(f"{label} duplicates an earlier evidence reference")
        seen.add(key)
    return errors


def _retained_manifest_row(asset_id: str) -> dict[str, Any] | None:
    try:
        from kb_lib import manifests_dir, read_jsonl

        rows = read_jsonl(manifests_dir() / "retained-files.jsonl")
    except (ImportError, OSError, ValueError):
        return None
    for row in rows:
        if str(row.get("asset_id") or "").strip() == asset_id:
            return row
    return None


def _capture_retained_ref(ref: dict[str, Any]) -> dict[str, Any]:
    asset_id = str(ref.get("value") or "").strip()
    row = _retained_manifest_row(asset_id)
    if row is None:
        return {"type": "retained", "asset_id": asset_id, "resolvable": False}
    status = str(row.get("status") or "active").strip()
    stored_path = str(row.get("stored_path") or "").strip()
    if stored_path:
        path = Path(stored_path).expanduser()
        if not path.is_file():
            return {"type": "retained", "asset_id": asset_id, "status": status, "resolvable": False}
        return {
            "type": "retained",
            "asset_id": asset_id,
            "status": status,
            "sha256": _sha256_file(path),
            "resolvable": status == "active",
        }
    locator = str(row.get("locator") or "").strip()
    return {
        "type": "retained_reference",
        "asset_id": asset_id,
        "status": status,
        "locator_sha256": hashlib.sha256(locator.encode("utf-8")).hexdigest() if locator else "",
        "resolvable": bool(locator) and status in {"active", "external_reference"},
    }


def has_resolvable_evidence_ref(entry: dict[str, Any], workspace_dir: str | Path) -> bool:
    refs = entry.get("evidence_refs")
    if validate_evidence_refs(refs):
        return False
    workspace = Path(workspace_dir).expanduser().resolve(strict=False)
    for ref in refs if isinstance(refs, list) else []:
        ref_type = str(ref.get("type") or "")
        if ref_type == "conversation":
            return True
        if ref_type == "git_commit":
            repository = str(ref.get("repository") or ref.get("url") or "").strip()
            if repository:
                continue
            snapshot = _capture_git_commit(ref, workspace)
            if snapshot and snapshot.get("resolvable") is True:
                return True
        if ref_type == "retained":
            if _capture_retained_ref(ref).get("resolvable") is True:
                return True
    return False


def _run_git(repo: Path, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def canonical_entry_revision(entry: dict[str, Any]) -> str:
    """Hash durable semantic content while ignoring legacy heat-only fields."""
    canonical = {
        key: value
        for key, value in entry.items()
        if key not in REVISION_EXCLUDED_FIELDS
    }
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_directory(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted((item for item in path.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        relative = child.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        file_hash = _sha256_file(child).encode("ascii")
        digest.update(file_hash)
    return digest.hexdigest()


def _resolve_path(value: str, workspace_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workspace_dir / path
    return path.resolve(strict=False)


def _git_root(path: Path) -> Path | None:
    cwd = path if path.is_dir() else path.parent
    code, out, _ = _run_git(cwd, "rev-parse", "--show-toplevel")
    return Path(out).resolve() if code == 0 and out else None


def _canonical_remote(remote: str) -> str:
    value = remote.strip()
    if not value:
        return ""
    if "://" in value:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        path = parsed.path.rstrip("/")
        if host:
            return f"{host.lower()}/{path.lstrip('/')}"
        return urlunsplit((parsed.scheme.lower(), "", path, "", ""))
    # SCP-style Git URL: user@host:org/repo.git. Drop the user identity.
    if ":" in value and not value.startswith(("/", "./", "../")):
        host_part, repo_part = value.split(":", 1)
        host = host_part.rsplit("@", 1)[-1].lower()
        return f"{host}/{repo_part.lstrip('/').rstrip('/')}"
    return value.replace("\\", "/").rstrip("/")


def _repo_id(repo: Path) -> str:
    code, remotes, _ = _run_git(repo, "remote")
    if code == 0:
        names = [line.strip() for line in remotes.splitlines() if line.strip()]
        ordered = (["origin"] if "origin" in names else []) + [name for name in sorted(names) if name != "origin"]
        for name in ordered:
            remote_code, remote, _ = _run_git(repo, "remote", "get-url", name)
            canonical = _canonical_remote(remote) if remote_code == 0 else ""
            if canonical:
                return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    code, roots, _ = _run_git(repo, "rev-list", "--max-parents=0", "--all")
    root_commits = ",".join(sorted(line.strip() for line in roots.splitlines() if line.strip())) if code == 0 else ""
    identity = f"local:{repo.name}:{root_commits}"
    return "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _current_branch(repo: Path) -> str:
    code, branch, _ = _run_git(repo, "branch", "--show-current")
    if code == 0 and branch:
        return branch
    code, head, _ = _run_git(repo, "rev-parse", "HEAD")
    return f"detached-{head[:12]}" if code == 0 and head else "unknown"


def _worktree_state(repo: Path, relative: str) -> str:
    code, unmerged, _ = _run_git(repo, "ls-files", "-u", "--", relative)
    if code == 0 and unmerged:
        return "conflicted"
    code, status, _ = _run_git(repo, "status", "--porcelain=v1", "--untracked-files=all", "--", relative)
    if code != 0 or not status:
        return "clean"
    states = [line[:2] for line in status.splitlines() if len(line) >= 2]
    conflict_codes = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
    if any(state in conflict_codes for state in states):
        return "conflicted"
    if any(state[1:2] not in {"", " "} for state in states):
        return "modified"
    if any(state[:1] not in {"", " ", "?"} for state in states):
        return "staged"
    if any(state == "??" for state in states):
        return "untracked"
    return "modified"


def _capture_source(source_path: str, workspace_dir: Path) -> dict[str, Any]:
    resolved = _resolve_path(source_path, workspace_dir)
    if not resolved.exists():
        return {
            "type": "missing",
            "source_path": source_path,
            "path": str(resolved),
        }

    repo = _git_root(resolved)
    if repo is not None and resolved.is_file():
        try:
            relative = resolved.relative_to(repo).as_posix()
        except ValueError:
            relative = ""
        if relative:
            tracked_code, _, _ = _run_git(repo, "ls-files", "--error-unmatch", "--", relative)
            head_code, head, _ = _run_git(repo, "rev-parse", "HEAD")
            blob_code, blob, _ = _run_git(repo, "rev-parse", f"HEAD:{relative}")
            if tracked_code == 0 and head_code == 0 and blob_code == 0:
                return {
                    "type": "git_file",
                    "source_path": source_path,
                    "repo_id": _repo_id(repo),
                    "branch": _current_branch(repo),
                    "commit": head,
                    "path": relative,
                    "blob_oid": blob,
                    "worktree_state": _worktree_state(repo, relative),
                }

    if resolved.is_dir():
        return {
            "type": "directory",
            "source_path": source_path,
            "path": source_path,
            "sha256": _sha256_directory(resolved),
        }
    return {
        "type": "file",
        "source_path": source_path,
        "path": source_path,
        "sha256": _sha256_file(resolved),
    }


def _capture_git_commit(ref: dict[str, Any], workspace_dir: Path) -> dict[str, Any] | None:
    repo = _git_root(workspace_dir)
    if repo is None:
        return None
    value = str(ref.get("value") or "").strip()
    code, commit, _ = _run_git(repo, "rev-parse", f"{value}^{{commit}}")
    if code != 0 or not commit:
        return {
            "type": "git_commit",
            "source_ref": value,
            "repo_id": _repo_id(repo),
            "branch": _current_branch(repo),
            "commit": value,
            "resolvable": False,
        }
    return {
        "type": "git_commit",
        "source_ref": value,
        "repo_id": _repo_id(repo),
        "branch": _current_branch(repo),
        "commit": commit,
        "resolvable": True,
    }


def capture_evidence_snapshots(entry: dict[str, Any], workspace_dir: str | Path) -> list[dict[str, Any]]:
    """Capture immutable local evidence coordinates for a durable KB entry."""
    workspace = Path(workspace_dir).expanduser().resolve(strict=False)
    snapshots: list[dict[str, Any]] = []
    source_paths = entry.get("source_paths")
    if isinstance(source_paths, list):
        for value in source_paths:
            source = str(value).strip()
            if source:
                snapshots.append(_capture_source(source, workspace))

    refs = entry.get("evidence_refs")
    if isinstance(refs, list):
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            if str(ref.get("type") or "") == "retained":
                snapshots.append(_capture_retained_ref(ref))
                continue
            if str(ref.get("type") or "") != "git_commit":
                continue
            if str(ref.get("repository") or ref.get("url") or "").strip():
                # Remote commits are verified by the publishing workflow and
                # backed by current local source/sidecar snapshots. Do not turn
                # them into an unresolvable snapshot in an unrelated local repo.
                continue
            snapshot = _capture_git_commit(ref, workspace)
            if snapshot is not None:
                snapshots.append(snapshot)
    return snapshots


def _snapshot_source_path(snapshot: dict[str, Any], workspace: Path) -> Path | None:
    source = str(snapshot.get("source_path") or "").strip()
    if source:
        return _resolve_path(source, workspace)
    repo_root = str(snapshot.get("repo_root") or "").strip()
    relative = str(snapshot.get("path") or "").strip()
    if repo_root and relative:
        return (Path(repo_root).expanduser() / relative).resolve(strict=False)
    snapshot_workspace = str(snapshot.get("workspace_dir") or "").strip()
    if snapshot_workspace and relative:
        return (Path(snapshot_workspace).expanduser() / relative).resolve(strict=False)
    if relative:
        return _resolve_path(relative, workspace)
    return None


def _verify_git_file(snapshot: dict[str, Any], workspace: Path) -> dict[str, Any]:
    source = _snapshot_source_path(snapshot, workspace)
    if source is None:
        return {"state": "unresolvable", "reason": "snapshot path is missing"}
    repo = _git_root(source if source.exists() else source.parent)
    if repo is None:
        return {"state": "unresolvable", "reason": "Git repository is unavailable"}

    relative = str(snapshot.get("path") or "").strip()
    if not relative:
        try:
            relative = source.relative_to(repo).as_posix()
        except ValueError:
            return {"state": "unresolvable", "reason": "source is outside the Git repository"}

    state = _worktree_state(repo, relative)
    if state == "conflicted":
        return {"state": "conflicted", "reason": "evidence file has an unresolved Git conflict"}
    if state in {"modified", "staged", "untracked"}:
        return {"state": "dirty_worktree", "reason": f"evidence file worktree_state={state}"}

    expected_repo_id = str(snapshot.get("repo_id") or "").strip()
    if expected_repo_id.startswith("sha256:") and expected_repo_id != _repo_id(repo):
        return {"state": "diverged", "reason": "repository identity differs from the snapshot"}

    expected_branch = str(snapshot.get("branch") or "").strip()
    current_branch = _current_branch(repo)
    if expected_branch and expected_branch != current_branch:
        return {"state": "diverged", "reason": f"branch changed: {expected_branch} -> {current_branch}"}

    expected_commit = str(snapshot.get("commit") or "").strip()
    if expected_commit:
        exists_code, _, _ = _run_git(repo, "cat-file", "-e", f"{expected_commit}^{{commit}}")
        if exists_code != 0:
            return {"state": "diverged", "reason": "snapshotted commit is no longer resolvable"}
        ancestor_code, _, _ = _run_git(repo, "merge-base", "--is-ancestor", expected_commit, "HEAD")
        if ancestor_code != 0:
            return {"state": "diverged", "reason": "current HEAD no longer descends from the snapshotted commit"}

    blob_code, current_blob, _ = _run_git(repo, "rev-parse", f"HEAD:{relative}")
    if blob_code != 0 or not current_blob:
        return {"state": "needs_recheck", "reason": "evidence path is absent from current HEAD"}
    if current_blob != str(snapshot.get("blob_oid") or "").strip():
        return {"state": "needs_recheck", "reason": "Git blob changed since verification"}
    return {"state": "fresh", "reason": "Git blob matches current local HEAD"}


def _verify_git_commit(snapshot: dict[str, Any], workspace: Path) -> dict[str, Any]:
    repo = _git_root(workspace)
    if repo is None:
        return {"state": "unresolvable", "reason": "Git repository is unavailable"}
    expected_repo_id = str(snapshot.get("repo_id") or "").strip()
    if expected_repo_id.startswith("sha256:") and expected_repo_id != _repo_id(repo):
        return {"state": "diverged", "reason": "repository identity differs from the snapshot"}
    commit = str(snapshot.get("commit") or "").strip()
    if not commit:
        return {"state": "unresolvable", "reason": "commit is missing"}
    exists_code, _, _ = _run_git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
    if exists_code != 0:
        return {"state": "unresolvable", "reason": "commit is unavailable locally"}
    ancestor_code, _, _ = _run_git(repo, "merge-base", "--is-ancestor", commit, "HEAD")
    if ancestor_code != 0:
        return {"state": "diverged", "reason": "commit is not an ancestor of current HEAD"}
    return {"state": "fresh", "reason": "commit is reachable from current local HEAD"}


def _verify_plain_snapshot(snapshot: dict[str, Any], workspace: Path) -> dict[str, Any]:
    path = _snapshot_source_path(snapshot, workspace)
    if path is None or not path.exists():
        return {"state": "unresolvable", "reason": "evidence path is unavailable"}
    expected = str(snapshot.get("sha256") or "").strip()
    if not expected:
        return {"state": "unresolvable", "reason": "snapshot hash is missing"}
    actual = _sha256_directory(path) if path.is_dir() else _sha256_file(path)
    if actual != expected:
        return {"state": "needs_recheck", "reason": "content hash changed since verification"}
    return {"state": "fresh", "reason": "content hash matches"}


def _verify_retained_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    asset_id = str(snapshot.get("asset_id") or "").strip()
    if not asset_id:
        return {"state": "unresolvable", "reason": "retained asset ID is missing"}
    current = _capture_retained_ref({"type": "retained", "value": asset_id})
    if current.get("resolvable") is not True:
        return {"state": "unresolvable", "reason": "retained asset no longer resolves through the manifest"}
    if str(current.get("type") or "") != str(snapshot.get("type") or ""):
        return {"state": "needs_recheck", "reason": "retained asset mode changed"}
    digest_field = "sha256" if current.get("type") == "retained" else "locator_sha256"
    if str(current.get(digest_field) or "") != str(snapshot.get(digest_field) or ""):
        return {"state": "needs_recheck", "reason": "retained evidence changed since verification"}
    return {"state": "fresh", "reason": "retained evidence matches the current manifest"}


_STATE_PRIORITY = {
    "fresh": 0,
    "legacy_unverified": 1,
    "not_snapshotted": 2,
    "unresolvable": 3,
    "needs_recheck": 4,
    "diverged": 5,
    "dirty_worktree": 6,
    "conflicted": 7,
}


def verify_entry_evidence(
    entry: dict[str, Any],
    workspace_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Compare stored evidence snapshots with current local files and local HEAD.

    This function is intentionally read-only. It never fetches, pulls, checks a
    hosting API, or updates the durable record, so ``fresh`` means only "fresh
    relative to the currently available local repository state".
    """
    if "evidence_snapshots" not in entry:
        return {
            "state": "legacy_unverified",
            "scope": "local_head",
            "applicability_state": "unknown",
            "items": [],
            "warning": "legacy_unverified: this record predates evidence snapshots",
        }
    snapshots = entry.get("evidence_snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return {
            "state": "not_snapshotted",
            "scope": "local_head",
            "applicability_state": "unknown",
            "items": [],
            "warning": "not_snapshotted: no verifiable evidence snapshot is stored",
        }

    workspace_value = workspace_dir or entry.get("workspace_dir") or os.getcwd()
    workspace = Path(str(workspace_value)).expanduser().resolve(strict=False)
    items: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            result = {"state": "unresolvable", "reason": "snapshot must be an object"}
            items.append(result)
            continue
        snapshot_type = str(snapshot.get("type") or "").strip()
        if snapshot_type == "git_file":
            result = _verify_git_file(snapshot, workspace)
        elif snapshot_type == "git_commit":
            result = _verify_git_commit(snapshot, workspace)
        elif snapshot_type in {"file", "directory"}:
            result = _verify_plain_snapshot(snapshot, workspace)
        elif snapshot_type in {"retained", "retained_reference"}:
            result = _verify_retained_snapshot(snapshot)
        elif snapshot_type == "missing":
            result = {"state": "unresolvable", "reason": "evidence was missing when snapshotted"}
        else:
            result = {"state": "unresolvable", "reason": f"unsupported snapshot type: {snapshot_type or '<empty>'}"}
        items.append({"type": snapshot_type, "source_path": snapshot.get("source_path", ""), **result})

    state = max((item["state"] for item in items), key=lambda value: _STATE_PRIORITY.get(value, 99))
    warning = "" if state == "fresh" else f"{state}: " + "; ".join(
        str(item.get("reason") or "") for item in items if item.get("state") == state
    )
    applicability = "current" if state == "fresh" else ("diverged" if state == "diverged" else "needs_review")
    return {
        "state": state,
        "scope": "local_head",
        "applicability_state": applicability,
        "items": items,
        "warning": warning,
    }


def evidence_strength(entry: dict[str, Any], verification: dict[str, Any] | None = None) -> str:
    snapshots = entry.get("evidence_snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return "legacy" if "evidence_snapshots" not in entry else "none"
    types = {str(item.get("type") or "") for item in snapshots if isinstance(item, dict)}
    if types.intersection({"git_file", "git_commit", "file", "directory"}):
        state = str((verification or {}).get("state") or "")
        return "strong" if state == "fresh" else "stale"
    return "none"
