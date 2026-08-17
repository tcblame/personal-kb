from __future__ import annotations

from pathlib import Path
from typing import Any

import kb_evidence


def _values(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _resolved_sources(entry: dict[str, Any], workspace_dir: Path) -> list[str]:
    resolved: list[str] = []
    for raw in _values(entry.get("source_paths")):
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = workspace_dir / path
        if path.exists():
            resolved.append(raw)
    return resolved


def evidence_errors(
    entry: dict[str, Any],
    *,
    workspace_dir: Path,
    require_fresh_snapshot: bool,
) -> list[str]:
    errors: list[str] = []
    sources = entry.get("source_paths")
    if sources is not None and not isinstance(sources, list):
        errors.append("source_paths must be a list")
    elif isinstance(sources, list):
        if any(not isinstance(value, str) or not value.strip() for value in sources):
            errors.append("source_paths must contain non-empty strings")
        if any(str(value).startswith(("commit:", "conversation:")) for value in sources):
            errors.append("typed commit/conversation references must use evidence_refs")

    errors.extend(kb_evidence.validate_evidence_refs(entry.get("evidence_refs")))
    if errors:
        return errors

    resolved_sources = _resolved_sources(entry, workspace_dir)
    resolvable_ref = kb_evidence.has_resolvable_evidence_ref(entry, workspace_dir)
    if not resolved_sources and not resolvable_ref:
        errors.append("at least one current source_path or resolvable evidence_ref is required")

    if require_fresh_snapshot:
        verification = kb_evidence.verify_entry_evidence(entry, workspace_dir)
        if verification.get("state") != "fresh":
            warning = str(verification.get("warning") or verification.get("state") or "unknown")
            errors.append(f"evidence snapshot is not fresh: {warning}")
    return errors


def metadata_errors(entry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    aliases = [value for value in _values(entry.get("aliases")) if str(value).strip()]
    triggers = [value for value in _values(entry.get("trigger_terms")) if str(value).strip()]
    if len(aliases) < 2 or len(aliases) > 8:
        errors.append("aliases must contain 2-8 values")
    if len(triggers) < 3 or len(triggers) > 15:
        errors.append("trigger_terms must contain 3-15 values")
    if entry.get("artifact_locator") and entry.get("kind") != "map":
        errors.append("artifact_locator records must use kind=map")
    return errors


def strict_record_errors(
    entry: dict[str, Any],
    *,
    workspace_dir: Path,
    require_fresh_snapshot: bool = True,
) -> list[str]:
    return [
        *metadata_errors(entry),
        *evidence_errors(
            entry,
            workspace_dir=workspace_dir,
            require_fresh_snapshot=require_fresh_snapshot,
        ),
    ]
