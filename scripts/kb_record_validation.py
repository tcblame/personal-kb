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


def _valid_recall_count(values: list, minimum: int, seed_limit: int, questions: set[str]) -> bool:
    if minimum <= len(values) <= seed_limit:
        return True
    if len(values) < minimum or len(values) > seed_limit + len(questions):
        return False
    # Beyond the initial vocabulary, each extra slot needs a saved positive
    # example and only phrases used in those examples can extend the seed set.
    untested = sum(not any(str(value).strip().casefold() in query for query in questions) for value in values)
    return untested <= seed_limit


def metadata_errors(entry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    aliases = [value for value in _values(entry.get("aliases")) if str(value).strip()]
    triggers = [value for value in _values(entry.get("trigger_terms")) if str(value).strip()]
    checks = entry.get("recall_checks")
    questions = set()
    if isinstance(checks, dict) and any(isinstance(q, str) and q.strip() for q in _values(checks.get("negative"))):
        questions = {q.strip().casefold() for q in _values(checks.get("positive")) if isinstance(q, str) and q.strip()}
    if not _valid_recall_count(aliases, 2, 8, questions):
        errors.append("use 2-8 initial aliases; additional aliases need distinct saved positive recall checks and a negative control")
    if not _valid_recall_count(triggers, 3, 15, questions):
        errors.append("use 3-15 initial trigger terms; additional terms need distinct saved positive recall checks and a negative control")
    if entry.get("artifact_locator") and entry.get("kind") != "map":
        errors.append("artifact_locator records must use kind=map")
    return errors


def strict_record_errors(
    entry: dict[str, Any],
    *,
    workspace_dir: Path,
    require_fresh_snapshot: bool = True,
) -> list[str]:
    from kb_domain import domain_errors, reference_errors
    return [
        *domain_errors(entry),
        *reference_errors(entry),
        *metadata_errors(entry),
        *evidence_errors(
            entry,
            workspace_dir=workspace_dir,
            require_fresh_snapshot=require_fresh_snapshot,
        ),
    ]
