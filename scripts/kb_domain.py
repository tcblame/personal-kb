"""Small domain contract and rebuildable index over the existing records.

No daemon, model call, graph database, or second writable fact store is used.
Reads never repair or save caches. An index rebuild is an explicit operation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
CURRENT = {"", "active", "current", "implemented", "decision_confirmed", "partial_current"}


@lru_cache(maxsize=1)
def model() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "references" / "domain-model.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _scope_errors(scope: Any, label: str) -> list[str]:
    if not isinstance(scope, dict):
        return [f"{label} must be an object"]
    return [f"{label}.{key} must be a supported non-empty string"
            for key, value in scope.items()
            if key not in model()["scope_fields"] or not isinstance(value, str) or not value.strip()]


def _validity_errors(value: dict, label: str) -> list[str]:
    errors = []
    dates = {}
    for field in ("valid_from", "valid_until"):
        if field not in value:
            continue
        try:
            dates[field] = _timestamp(value[field])
        except (ValueError, TypeError, AttributeError):
            errors.append(f"{label}.{field} must be an ISO date/time")
    if len(dates) == 2 and dates["valid_from"] >= dates["valid_until"]:
        errors.append(f"{label} validity interval must be non-empty")
    return errors


def domain_errors(entry: dict[str, Any]) -> list[str]:
    """Validate optional metadata; legacy records retain their existing contract."""
    if "domain" not in entry:
        return []
    d = entry["domain"]
    if not isinstance(d, dict):
        return ["domain must be an object"]
    errors = []
    allowed = {"entity_id", "entity_type", "project_id", "repo_id", "state", "scope",
               "relations", "valid_from", "valid_until"}
    if set(d) - allowed:
        errors.append("domain has unsupported fields: " + ", ".join(sorted(set(d) - allowed)))
    for field in ("entity_id", "project_id"):
        if not isinstance(d.get(field), str) or not ID.fullmatch(d[field]):
            errors.append(f"domain.{field} must be a stable opaque ID")
    kind = d.get("entity_type")
    if not isinstance(kind, str) or kind not in model()["entity_kinds"]:
        errors.append("domain.entity_type is unsupported")
    elif entry.get("kind") not in model()["entity_kinds"][kind]:
        errors.append("domain.entity_type is incompatible with kind")
    if kind == "project" and d.get("entity_id") != d.get("project_id"):
        errors.append("project entity_id must equal project_id")
    if d.get("state") not in model()["states"]:
        errors.append("domain.state must be confirmed, candidate, or conflicted")
    if "repo_id" in d and (not isinstance(d["repo_id"], str) or not ID.fullmatch(d["repo_id"])):
        errors.append("domain.repo_id must be a stable opaque ID")
    errors.extend(_scope_errors(d.get("scope", {}), "domain.scope"))
    errors.extend(_validity_errors(d, "domain"))
    relations = d.get("relations", [])
    if not isinstance(relations, list) or len(relations) > model()["max_relations_per_record"]:
        return errors + ["domain.relations must be a bounded list"]
    import kb_evidence
    for i, relation in enumerate(relations):
        label = f"domain.relations[{i}]"
        if not isinstance(relation, dict):
            errors.append(f"{label} must be an object")
            continue
        if set(relation) - {"type", "target", "state", "scope", "evidence_refs", "valid_from", "valid_until"}:
            errors.append(f"{label} has unsupported fields")
        if not isinstance(relation.get("type"), str) or relation["type"] not in model()["relations"]:
            errors.append(f"{label}.type is unsupported")
        target = relation.get("target")
        if not isinstance(target, str) or not ID.fullmatch(target):
            errors.append(f"{label}.target must be an entity ID")
        if target == d.get("entity_id"):
            errors.append(f"{label} cannot reference itself")
        if relation.get("state") not in model()["states"]:
            errors.append(f"{label}.state is required")
        if not relation.get("evidence_refs"):
            errors.append(f"{label} requires evidence_refs")
        else:
            errors.extend(kb_evidence.validate_evidence_refs(relation["evidence_refs"]))
        errors.extend(_scope_errors(relation.get("scope", {}), label + ".scope"))
        errors.extend(_validity_errors(relation, label))
    return errors


def active(entry: dict, scope: dict[str, str], *, now: datetime | None = None) -> bool:
    if entry.get("_deleted") or str(entry.get("status") or "") not in CURRENT or entry.get("superseded_by"):
        return False
    d = entry.get("domain")
    return not d or declaration_active(d, scope, now=now)


def declaration_active(d: dict, scope: dict[str, str], *, now: datetime | None = None) -> bool:
    if d.get("state") != "confirmed":
        return False
    if any(scope.get(key) != value for key, value in d.get("scope", {}).items()):
        return False
    now = now or datetime.now(timezone.utc)
    return ((not d.get("valid_from") or _timestamp(d["valid_from"]) <= now)
            and (not d.get("valid_until") or now < _timestamp(d["valid_until"])))


def _files() -> list[Path]:
    from kb_lib import storage_layout
    root = storage_layout().records.resolve()
    result = []
    for path in sorted(root.rglob("kb.jsonl")):
        relative = path.relative_to(root)
        if any(part in {"archive", "archives", "_meta", ".git"} for part in relative.parts):
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError("domain index cannot follow a records symlink outside its root")
        result.append(path)
    return result


def _signature(files: list[Path]) -> str:
    from kb_lib import storage_layout
    digest = hashlib.sha256(str(storage_layout().records.resolve()).encode())
    for path in files:
        stat = path.stat()
        digest.update(f"{path}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}".encode())
    digest.update(json.dumps(model(), sort_keys=True).encode())
    return digest.hexdigest()


def load_graph(*, rebuild: bool = False) -> dict:
    from kb_lib import read_jsonl, storage_layout
    files = _files()
    signature = _signature(files)
    cache = storage_layout().cache / "domain-index.json"
    if not rebuild and cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if (isinstance(data, dict) and isinstance(data.get("entries"), list)
                    and data.get("signature") == signature and data.get("schema") == "personal-kb.domain-index/v1"):
                return {**data, "cache_hit": True}
        except (ValueError, UnicodeError):
            pass  # Derived cache only; rebuild in memory, without writing on reads.
    entries = []
    invalid_count = 0
    for path in files:
        for entry in read_jsonl(path):
            if "domain" not in entry:
                continue
            if domain_errors(entry):
                invalid_count += 1
            else:
                entries.append(entry)
    data = {"schema": "personal-kb.domain-index/v1", "signature": signature,
            "entries": entries, "invalid_count": invalid_count, "cache_hit": False}
    if rebuild:
        cache.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".domain-", dir=cache.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(temporary, cache)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return data


def reference_errors(entry: dict) -> list[str]:
    if "domain" not in entry or domain_errors(entry):
        return []
    entries = [e for e in load_graph()["entries"] if e.get("id") != entry.get("id")]
    entries.append(entry)
    by_id: dict[str, list[dict]] = {}
    for row in entries:
        by_id.setdefault(row["domain"]["entity_id"], []).append(row)
    d = entry["domain"]
    errors = []
    if len(by_id[d["entity_id"]]) != 1:
        errors.append("domain.entity_id already belongs to another record; update its owner")
    project = by_id.get(d["project_id"], [])
    if len(project) != 1 or project[0]["domain"]["entity_type"] != "project":
        errors.append("domain.project_id must resolve to exactly one project record")
    for relation in d.get("relations", []):
        targets = by_id.get(relation["target"], [])
        if len(targets) != 1:
            errors.append("domain relation target must resolve to exactly one entity")
            continue
        target = targets[0]["domain"]
        if target["project_id"] != d["project_id"]:
            errors.append("domain relations cannot silently join different projects")
        if target["entity_type"] not in model()["relations"][relation["type"]]["target_types"]:
            errors.append("domain relation target type is incompatible")
        if relation["type"] == "supersedes" and target["entity_type"] != d["entity_type"]:
            errors.append("supersedes requires matching entity types")
        # A scoped source cannot silently widen a replacement beyond its own scope.
        if relation["type"] == "supersedes" and any(
            relation.get("scope", {}).get(k, v) != v for k, v in d.get("scope", {}).items()
        ):
            errors.append("supersedes scope cannot contradict its source scope")
    # Reject replacement cycles, including candidate edges: they have no valid promotion path.
    edges = {key: [r["target"] for r in rows[0]["domain"].get("relations", [])
                   if r["type"] == "supersedes"] for key, rows in by_id.items() if len(rows) == 1}
    pending = list(edges.get(d["entity_id"], []))
    seen = set()
    while pending:
        node = pending.pop()
        if node == d["entity_id"]:
            errors.append("supersedes relationships cannot form a cycle")
            break
        if node not in seen:
            seen.add(node)
            pending.extend(edges.get(node, []))
    return errors


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Explicitly rebuild the derived domain index")
    parser.parse_args(argv)
    data = load_graph(rebuild=True)
    print(json.dumps({"status": "ok", "indexed_records": len(data["entries"]),
                      "invalid_records": data["invalid_count"]}))
    return 0
