#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from kb_lib import configure_utf8_streams

configure_utf8_streams()

import kb_update
import kb_session_brief
import kb_adoption
import kb_evidence
from kb_lib import append_jsonl, find_entry, kb_base_dir, now_iso, read_jsonl, resolve_context, runtime_file
from kb_runtime import attach_runtime_scope, runtime_session_id
from kb_timing import span, timed


RETRIEVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MISS_REASONS = {
    "trigger_missed",
    "query_too_weak",
    "no_verified_hit",
    "execution_failed",
    "unknown",
}
MEMORY_REVIEW_STATES = {
    "not_reusable", "already_recorded", "pending_verification", "blocked_by_instruction",
    "rejected_evidence", "written", "updated",
}


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        value = value.strip()
        if value and value not in out:
            out.append(value)
    return out


def _read_json_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if args.json_file:
        try:
            payload = json.loads(Path(args.json_file).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid --json-file: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("closeout payload must be a JSON object")
    if args.json:
        try:
            inline = json.loads(args.json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid --json: {exc}") from exc
        if not isinstance(inline, dict):
            raise ValueError("--json must be a JSON object")
        payload.update(inline)
    if not isinstance(payload, dict):
        raise ValueError("closeout payload must be a JSON object")
    return payload


def _clip(text: str, limit: int = 320) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off", ""}:
            return False
    return default


def _list_from_payload(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _parse_rejection_specs(values: Any) -> dict[str, str]:
    """Parse CLI ``entry_id=reason`` feedback into a stable mapping."""
    specs = values if isinstance(values, list) else [values] if isinstance(values, str) else []
    parsed: dict[str, str] = {}
    for raw in specs:
        text = str(raw or "").strip()
        if not text:
            continue
        if "=" in text:
            entry_id, reason = text.split("=", 1)
        elif ":" in text:
            entry_id, reason = text.split(":", 1)
        else:
            raise ValueError("rejection reason must use entry_id=reason")
        entry_id = entry_id.strip()
        reason = _clip(reason.strip(), 160)
        if not entry_id or not reason:
            raise ValueError("rejection reason requires a non-empty entry ID and reason")
        parsed[entry_id] = reason
    return parsed


def _compact_retrieval_items(value: Any) -> list[dict[str, Any]]:
    """Keep only ranking telemetry needed by runtime quality audits."""
    if isinstance(value, dict):
        value = value.get("items")
    if not isinstance(value, list):
        return []
    compact: list[dict[str, Any]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        entry_id = str(item.get("entry_id") or item.get("id") or "").strip()
        if not entry_id:
            continue
        compact.append(
            {
                "entry_id": entry_id,
                "kind": str(item.get("kind") or ""),
                "freshness_state": str(item.get("freshness_state") or "legacy_unverified"),
                "trust_tier": str(item.get("trust_tier") or ""),
                **({"applicability_state": "locate_only"} if item.get("applicability_state") == "locate_only" else {}),
            }
        )
    return compact


def _linked_retrieval_ids(payload: dict[str, Any], args: argparse.Namespace) -> list[str]:
    values = [
        str(value).strip()
        for value in [
            *_list_from_payload(payload.get("linked_retrieval_ids")),
            *list(getattr(args, "linked_retrieval_id", []) or []),
        ]
        if str(value).strip()
    ]
    invalid = [value for value in values if not RETRIEVAL_ID_RE.fullmatch(value)]
    if invalid:
        raise ValueError(
            "linked retrieval ids must be 1-128 opaque characters using letters, digits, '.', '_', ':', or '-'"
        )
    if len(set(values)) != len(values):
        raise ValueError("linked retrieval ids must be unique within one closeout")
    return values


def _session_brief_ids(base_dir: Path) -> set[str]:
    path = kb_session_brief.session_briefs_path(base_dir)
    return {
        str(row.get("id", "")).strip()
        for row in read_jsonl(path)
        if str(row.get("id", "")).strip()
    }


def _read_session_brief_payload(args: argparse.Namespace) -> dict[str, Any] | None:
    payload: dict[str, Any] = {}
    if args.session_brief_json_file:
        try:
            payload = json.loads(Path(args.session_brief_json_file).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid --session-brief-json-file: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("--session-brief-json-file must contain a JSON object")
    if args.session_brief_json:
        try:
            inline = json.loads(args.session_brief_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid --session-brief-json: {exc}") from exc
        if not isinstance(inline, dict):
            raise ValueError("--session-brief-json must be a JSON object")
        payload.update(inline)

    has_inline_fields = any(
        [
            args.session_brief_title.strip(),
            args.session_brief_summary.strip(),
            args.session_brief_tags.strip(),
            args.session_brief_anchors.strip(),
            args.session_brief_source.strip(),
            args.session_id.strip(),
        ]
    )
    if not payload and not has_inline_fields and not args.auto_session_brief:
        return None

    if args.session_brief_title.strip():
        payload["title"] = args.session_brief_title.strip()
    if args.session_brief_summary.strip():
        payload["summary"] = args.session_brief_summary.strip()
    if args.session_brief_tags.strip():
        payload["tags"] = [part.strip() for part in args.session_brief_tags.split(",") if part.strip()]
    if args.session_brief_anchors.strip():
        payload["anchors"] = [part.strip() for part in args.session_brief_anchors.split(",") if part.strip()]
    if args.session_brief_source.strip():
        payload["source"] = args.session_brief_source.strip()
    if args.session_id.strip():
        payload["session_id"] = args.session_id.strip()
    return payload


def apply_used_entries(closeout: dict[str, Any], args: argparse.Namespace) -> None:
    """Heat adopted KB entries as part of AI-only task closeout.

    Closeout is the normal post-use hook. It applies the narrow `use` side
    effect so future AI runs do not depend on a second remembered command.
    Failures are captured in the closeout event instead of being printed into
    the task answer.
    """
    used = [eid for eid in closeout.get("used_entry_ids", []) if isinstance(eid, str) and eid.strip()]
    if args.no_apply_use or not used:
        closeout["adoption_applied"] = False
        closeout["adopted_entry_ids"] = []
        closeout["heat_applied"] = False
        closeout["heated_entry_ids"] = []
        closeout["heat_failed_entry_ids"] = []
        return

    effect_by_entry: dict[str, str] = {}
    adoption_effects = closeout.get("adoption_effects")
    if isinstance(adoption_effects, dict):
        for effect in ("locate", "decide", "fix", "write"):
            for entry_id in adoption_effects.get(effect, []):
                if isinstance(entry_id, str) and entry_id.strip():
                    effect_by_entry[entry_id.strip()] = effect
    for entry_id in closeout.get("heat_entry_ids", []):
        if isinstance(entry_id, str) and entry_id.strip():
            effect_by_entry.setdefault(entry_id.strip(), "legacy")

    adopted: list[str] = []
    heated: list[str] = []
    failed: list[dict[str, Any]] = []
    repo = args.repo.strip()
    branch = args.branch.strip()
    closeout_id = str(closeout.get("closeout_id") or "")
    session_id = str(closeout.get("session_id") or "")

    for entry_id in used:
        effect = effect_by_entry.get(entry_id, "legacy")
        if effect != "locate":
            entry = _load_entry_for_closeout(entry_id, repo=repo, branch=branch)
            if entry is None:
                failed.append({"entry_id": entry_id, "code": 2, "message": "entry not found for freshness check"})
                continue
            freshness = kb_evidence.verify_entry_evidence(entry)
            freshness_state = str(freshness.get("state") or "legacy_unverified")
            # Dual-read compatibility: v1 records have no snapshots and remain
            # usable with an explicit warning. Once a record has adopted the v2
            # snapshot contract, stale/dirty evidence blocks decision heat.
            if "evidence_snapshots" in entry and freshness_state != "fresh":
                failed.append({
                    "entry_id": entry_id,
                    "code": 7,
                    "message": str(freshness.get("warning") or freshness.get("state") or "evidence is not fresh"),
                    "freshness_state": freshness.get("state"),
                })
                continue
            if freshness_state == "legacy_unverified":
                closeout.setdefault("freshness_warnings", []).append({
                    "entry_id": entry_id,
                    "freshness_state": freshness_state,
                    "message": freshness.get("warning", "legacy record has no evidence snapshot"),
                })

        event_id = hashlib.sha256(f"{closeout_id}|{entry_id}|{effect}".encode("utf-8")).hexdigest()
        argv = ["use", entry_id, "--effect", effect, "--event-id", event_id]
        if session_id:
            argv.extend(["--session-id", session_id])
        if repo:
            argv.extend(["--repo", repo])
        if branch:
            argv.extend(["--branch", branch])

        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = kb_update.main(argv)
            except SystemExit as exc:
                rc = 0 if exc.code is None else (exc.code if isinstance(exc.code, int) else 1)
            except Exception as exc:  # defensive: closeout should still be recorded
                rc = 1
                err.write(f"{type(exc).__name__}: {exc}")

        if rc == 0:
            adopted.append(entry_id)
            if effect in kb_adoption.HEAT_EFFECTS:
                heated.append(entry_id)
        else:
            failed.append({
                "entry_id": entry_id,
                "code": rc,
                "message": _clip(err.getvalue() or out.getvalue()),
            })

    closeout["adoption_applied"] = True
    closeout["adopted_entry_ids"] = adopted
    closeout["heat_applied"] = bool(heated)
    closeout["heated_entry_ids"] = heated
    closeout["heat_failed_entry_ids"] = [item["entry_id"] for item in failed]
    if failed:
        closeout["heat_errors"] = failed


def _load_entry_for_closeout(entry_id: str, *, repo: str, branch: str) -> dict[str, Any] | None:
    ctx = resolve_context(
        cwd=Path.cwd(),
        repo_name_override=(repo or None),
        branch_override=(branch or None),
        operation="closeout-freshness",
    )
    entries = read_jsonl(ctx.kb_path)
    idx = find_entry(entries, entry_id)
    if idx is not None:
        return entries[idx]
    for path in kb_base_dir().rglob("kb.jsonl"):
        entries = read_jsonl(path)
        idx = find_entry(entries, entry_id)
        if idx is not None:
            return entries[idx]
    return None


def apply_session_brief(
    closeout: dict[str, Any],
    args: argparse.Namespace,
    payload: dict[str, Any] | None,
) -> None:
    if payload is None:
        closeout["session_brief_ids"] = []
        return

    title = str(payload.get("title", "") or "").strip()
    summary = str(payload.get("summary", "") or payload.get("story", "") or "").strip()
    if not title:
        first_query = closeout.get("queries", [""])
        title = str(first_query[0] if first_query else "").strip() or "recent session brief"
    if not summary:
        closeout["session_brief_ids"] = []
        closeout["session_brief_skipped_reason"] = "explicit session brief summary required"
        return

    tags = payload.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    tags = [str(item) for item in tags if str(item).strip()]
    if closeout.get("repo"):
        tags.append(str(closeout["repo"]))
    tags.append("recent-session")

    anchors = payload.get("anchors", [])
    if not isinstance(anchors, list):
        anchors = []
    anchors = [str(item) for item in anchors if str(item).strip()]

    brief = kb_session_brief.build_brief(
        title=title,
        summary=summary,
        repo=str(closeout.get("repo", "")),
        branch=str(closeout.get("branch", "")),
        cwd=str(closeout.get("cwd", "")),
        tags=tags,
        anchors=anchors,
        queries=[str(item) for item in closeout.get("queries", []) if str(item).strip()],
        used_entry_ids=[str(item) for item in closeout.get("used_entry_ids", []) if str(item).strip()],
        written_entry_ids=[str(item) for item in closeout.get("written_entry_ids", []) if str(item).strip()],
        updated_entry_ids=[str(item) for item in closeout.get("updated_entry_ids", []) if str(item).strip()],
        source=str(payload.get("source", "") or "unknown"),
        session_id=str(payload.get("session_id", "") or ""),
    )
    path = kb_session_brief.append_brief(brief, base_dir=kb_base_dir())
    closeout["session_brief_ids"] = [brief["id"]]
    closeout["session_brief_path"] = str(path)


@timed("closeout.build")
def build_closeout(args: argparse.Namespace) -> dict[str, Any]:
    payload = _read_json_payload(args)
    telemetry_present = any(key in payload for key in ("retrieval_items", "retrieval_snapshot", "retrieval_snapshots"))
    retrieval_items = _compact_retrieval_items(
        payload.get("retrieval_items", payload.get("retrieval_snapshot"))
    )
    retrieval_items_json = str(getattr(args, "retrieval_items_json", "") or "")
    if retrieval_items_json:
        try:
            inline_items = json.loads(retrieval_items_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid --retrieval-items-json: {exc}") from exc
        retrieval_items = _compact_retrieval_items(inline_items)
        telemetry_present = True
    linked_retrieval_ids = _linked_retrieval_ids(payload, args)
    snapshot = payload.get("retrieval_snapshot")
    snapshots = payload.get("retrieval_snapshots")
    if snapshots is not None:
        if snapshot is not None or not isinstance(snapshots, list) or not snapshots:
            raise ValueError("retrieval_snapshots requires a non-empty array; do not combine it with retrieval_snapshot")
    else:
        snapshots = [snapshot] if isinstance(snapshot, dict) and "retrieval_id" in snapshot else []
    snapshot_authoritative = bool(snapshots)
    snapshot_ids: list[str] = []
    snapshot_hit_count = 0
    snapshot_queries: list[str] = []
    snapshot_sessions: list[str] = []
    snapshot_topics: list[str] = []
    locate_only_ids = {i["entry_id"] for i in retrieval_items if i.get("applicability_state") == "locate_only"}
    if snapshot_authoritative:
        retrieval_ids: list[str] = []
        all_items: list[dict[str, Any]] = []
        for result in snapshots:
            if not isinstance(result, dict):
                raise ValueError("retrieval_snapshot must be an object")
            retrieval_id = result.get("retrieval_id")
            if not isinstance(retrieval_id, str) or not RETRIEVAL_ID_RE.fullmatch(retrieval_id):
                raise ValueError("retrieval_snapshot requires a valid retrieval_id")
            if retrieval_id in retrieval_ids:
                raise ValueError("retrieval_snapshots retrieval IDs must be unique")
            items = result.get("items")
            if not isinstance(items, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("entry_id"), str) or not item["entry_id"].strip()
                for item in items
            ):
                raise ValueError("retrieval_snapshot.items must be an array of entry_id objects")
            ids = [item["entry_id"].strip() for item in items]
            if len(set(ids)) != len(ids):
                raise ValueError("retrieval_snapshot entry IDs must be unique")
            if "hit_count" in result and result["hit_count"] != len(ids):
                raise ValueError("retrieval_snapshot hit_count conflicts with items")
            retrieval_ids.append(retrieval_id)
            snapshot_ids.extend(ids)
            all_items.extend(items)
            snapshot_queries.extend(_list_from_payload(result.get("query")))
            snapshot_sessions.extend(_list_from_payload(result.get("session_id")))
            snapshot_topics.extend(_list_from_payload(result.get("topic_id")))
        snapshot_hit_count = len(all_items)
        snapshot_ids = _dedupe(snapshot_ids)
        # A fresh child can still be location-only because its project evidence
        # changed. Checking just the child again during adoption misses that.
        locate_only_ids = {i["entry_id"] for i in all_items if i.get("applicability_state") == "locate_only"}
        if linked_retrieval_ids and linked_retrieval_ids != retrieval_ids:
            raise ValueError("retrieval_snapshot conflicts with linked retrieval ids")
        linked_retrieval_ids = retrieval_ids
        compact_items = _compact_retrieval_items(all_items)
        if ("retrieval_items" in payload or retrieval_items_json) and retrieval_items != compact_items:
            raise ValueError("retrieval_snapshot conflicts with retrieval_items")
        retrieval_items = compact_items
    ctx = resolve_context(
        cwd=Path.cwd(),
        repo_name_override=(args.repo.strip() or None),
        branch_override=(args.branch.strip() or None),
        task_hint=" ".join(args.query),
        operation="closeout",
        debug=bool(getattr(args, "debug", False)),
    )

    queries = _dedupe([*_list_from_payload(payload.get("queries")), *args.query, *snapshot_queries])
    legacy_used = _dedupe([*_list_from_payload(payload.get("used_entry_ids")), *args.used])
    adoption_effects_payload = payload.get("adoption_effects") if isinstance(payload.get("adoption_effects"), dict) else {}
    adoption_effects = {
        "locate": _dedupe([*_list_from_payload(adoption_effects_payload.get("locate")), *args.used_locate]),
        "decide": _dedupe([*_list_from_payload(adoption_effects_payload.get("decide")), *args.used_decide]),
        "fix": _dedupe([*_list_from_payload(adoption_effects_payload.get("fix")), *args.used_fix]),
        "write": _dedupe([*_list_from_payload(adoption_effects_payload.get("write")), *args.used_write]),
    }
    assigned_effect: dict[str, str] = {}
    for effect in ("write", "fix", "decide", "locate"):
        normalized: list[str] = []
        for entry_id in adoption_effects[effect]:
            if entry_id in assigned_effect:
                continue
            assigned_effect[entry_id] = effect
            normalized.append(entry_id)
        adoption_effects[effect] = normalized
    adopted = _dedupe([*legacy_used, *adoption_effects["locate"], *adoption_effects["decide"], *adoption_effects["fix"], *adoption_effects["write"]])
    brief_ids = _session_brief_ids(kb_base_dir())
    session_brief_used = [entry_id for entry_id in adopted if entry_id in brief_ids]
    used = [entry_id for entry_id in adopted if entry_id not in brief_ids]
    heat_entry_ids = [
        entry_id
        for entry_id in _dedupe([*legacy_used, *adoption_effects["decide"], *adoption_effects["fix"], *adoption_effects["write"]])
        if entry_id not in brief_ids
    ]
    if locate_only_ids.intersection(heat_entry_ids):
        raise ValueError("a locate_only receipt supports only the locate effect; verify and retrieve again for decision adoption")
    written = _dedupe([*_list_from_payload(payload.get("written_entry_ids")), *args.written])
    updated = _dedupe([*_list_from_payload(payload.get("updated_entry_ids")), *args.updated])
    hit_entry_ids = _dedupe(
        [
            *_list_from_payload(payload.get("hit_entry_ids")),
            *_list_from_payload(payload.get("allowed_hit_entry_ids")),
            *args.allowed_hit_id,
        ]
    )
    if snapshot_authoritative:
        if hit_entry_ids and not set(hit_entry_ids).issubset(snapshot_ids):
            raise ValueError("allowed hit ids must belong to retrieval_snapshot")
        hit_entry_ids = snapshot_ids

    rejected_hit_ids = _dedupe(
        [
            *_list_from_payload(payload.get("rejected_hit_ids")),
            *list(getattr(args, "rejected_hit_id", []) or []),
        ]
    )
    rejection_reasons: dict[str, str] = {}
    payload_reasons = payload.get("rejection_reasons")
    if payload_reasons is not None:
        if not isinstance(payload_reasons, dict):
            raise ValueError("rejection_reasons must be an object mapping entry IDs to reasons")
        for entry_id, reason in payload_reasons.items():
            key = str(entry_id).strip()
            value = _clip(str(reason or "").strip(), 160)
            if not key or not value:
                raise ValueError("rejection_reasons requires non-empty entry IDs and reasons")
            rejection_reasons[key] = value
    rejection_reasons.update(
        _parse_rejection_specs(getattr(args, "rejection_reason", []))
    )
    for entry_id in rejection_reasons:
        if entry_id not in rejected_hit_ids:
            rejected_hit_ids.append(entry_id)

    miss_reason = str(payload.get("miss_reason") or getattr(args, "miss_reason", "") or "").strip()
    if miss_reason and miss_reason not in MISS_REASONS:
        raise ValueError(
            "miss_reason must be one of: " + ", ".join(sorted(MISS_REASONS))
        )
    if hit_entry_ids or snapshot_authoritative:
        invalid_rejected = [entry_id for entry_id in rejected_hit_ids if entry_id not in set(hit_entry_ids)]
        if invalid_rejected:
            raise ValueError(
                "rejected entries must belong to allowed hit ids: " + ", ".join(invalid_rejected)
            )
    adopted_ids_for_feedback = set(adopted)
    overlap = sorted(adopted_ids_for_feedback.intersection(rejected_hit_ids))
    if overlap:
        raise ValueError("an entry cannot be both adopted and rejected: " + ", ".join(overlap))

    hit_count = _coerce_int(payload.get("hit_count", args.hit_count), 0)
    if snapshot_authoritative:
        if ("hit_count" in payload or args.hit_count != 0) and hit_count != snapshot_hit_count:
            raise ValueError("hit_count conflicts with retrieval_snapshot")
        hit_count = snapshot_hit_count
    elif retrieval_items and "hit_count" not in payload and args.hit_count == 0:
        hit_count = len(retrieval_items)
    if hit_count < 0:
        raise ValueError("hit_count must be >= 0")

    rag_calls_explicit = "rag_calls" in payload or args.rag_calls is not None
    rag_calls_raw = payload.get("rag_calls", args.rag_calls)
    rag_calls_inferred = False
    if not rag_calls_explicit:
        rag_calls = max(len(linked_retrieval_ids), len(queries), 1 if hit_count > 0 else 0)
        rag_calls_inferred = bool(linked_retrieval_ids or queries or hit_count > 0)
    else:
        rag_calls = _coerce_int(rag_calls_raw, 0)
        if rag_calls == 0 and (queries or hit_count > 0):
            raise ValueError("rag_calls cannot be 0 when queries or hit_count indicate KB usage")
    if rag_calls < 0:
        raise ValueError("rag_calls must be >= 0")

    legacy_unlinked = bool(getattr(args, "legacy_allow_unlinked_retrievals", False))
    attempts = []
    payload_attempts = payload.get("unresolved_retrieval_attempts", [])
    if not isinstance(payload_attempts, list):
        raise ValueError("unresolved_retrieval_attempts must be an array")
    attempt_specs = []
    for attempt in payload_attempts:
        if not isinstance(attempt, dict) or not isinstance(attempt.get("attempt_id"), str) or not isinstance(attempt.get("state"), str):
            raise ValueError("unresolved_retrieval_attempts requires attempt_id and state strings")
        attempt_specs.append(attempt["attempt_id"] + "=" + attempt["state"])
    for spec in [*attempt_specs, *(getattr(args, "unresolved_attempt", []) or [])]:
        attempt_id, separator, state = spec.partition("=")
        if not separator or not RETRIEVAL_ID_RE.fullmatch(attempt_id) or state not in {"pending", "failed", "unavailable", "aborted", "unknown"}:
            raise ValueError("--unresolved-attempt requires ID=pending|failed|unavailable|aborted|unknown")
        if any(a["attempt_id"] == attempt_id for a in attempts) or attempt_id in linked_retrieval_ids:
            raise ValueError("attempt IDs must be unique and separate from linked retrieval IDs")
        attempts.append({"attempt_id": attempt_id, "state": state})
    if attempts and not rag_calls_explicit:
        rag_calls = len(linked_retrieval_ids) + len(attempts)
    if attempts and not linked_retrieval_ids and hit_count:
        raise ValueError("unresolved attempts alone cannot supply confirmed hits")
    if rag_calls == 0 and linked_retrieval_ids:
        raise ValueError("linked retrieval ids require rag_calls > 0")
    if len(linked_retrieval_ids) + len(attempts) != rag_calls:
        if not (legacy_unlinked and not linked_retrieval_ids):
            raise ValueError(
                "rag_calls must equal unique linked retrieval ids plus unresolved attempts"
            )

    skipped_reason = args.reason.strip() or str(payload.get("skipped_reason", "") or "")
    if not (used or session_brief_used or written or updated) and not skipped_reason and not (
        rejected_hit_ids or miss_reason
    ):
        raise ValueError("reason/skipped_reason is required when no used/written/updated entry ids are recorded")

    if hit_entry_ids or snapshot_authoritative:
        invalid_used = [entry_id for entry_id in adopted if entry_id not in set(hit_entry_ids)]
        if invalid_used:
            raise ValueError(f"--used entries must belong to allowed hit ids: {', '.join(invalid_used)}")

    session_brief_help = (
        bool(session_brief_used)
        or args.session_brief_help
        or _coerce_bool(payload.get("session_brief_help"), False)
    )
    session_brief_hit = (
        args.session_brief_hit
        or session_brief_help
        or _coerce_bool(payload.get("session_brief_hit"), False)
    )

    closeout = attach_runtime_scope({
        "closeout_id": (
            str(getattr(args, "closeout_id", "") or "").strip()
            or str(payload.get("closeout_id") or "").strip()
            or uuid.uuid4().hex
        ),
        "ts": now_iso(),
        "event": "kb_closeout",
        "mode": "ai_only_runtime_audit",
        "cwd": str(Path.cwd()),
        "repo": ctx.repo_name,
        "branch": ctx.branch,
        "session_id": runtime_session_id(
            str(getattr(args, "session_id", "") or payload.get("session_id") or ""),
            snapshot_sessions[0] if len(set(snapshot_sessions)) == 1 else "",
        ),
        "topic_id": str(getattr(args, "topic_id", "") or payload.get("topic_id") or "").strip()
                    or (snapshot_topics[0] if len(set(snapshot_topics)) == 1 else ""),
        "rag_calls": rag_calls,
        "queries": queries,
        "hit_count": hit_count,
        "used_entry_ids": used,
        "adoption_effects": adoption_effects,
        "heat_entry_ids": heat_entry_ids,
        "written_entry_ids": written,
        "updated_entry_ids": updated,
        "skipped_reason": skipped_reason,
        "routing": {
            "source": str(getattr(ctx, "routing_source", "unknown")),
            "candidate_repos": list(getattr(ctx, "candidate_repos", ())),
        },
        "linked_retrieval_ids": linked_retrieval_ids,
    })
    if legacy_unlinked and rag_calls > 0 and not linked_retrieval_ids:
        closeout["legacy_unlinked_retrievals"] = True
    if attempts:
        closeout["unresolved_retrieval_attempts"] = attempts
        closeout["completed_retrieval_count"] = len(linked_retrieval_ids)
    review_payload = payload.get("memory_review", {})
    if not isinstance(review_payload, dict):
        raise ValueError("memory_review must be an object")
    review = getattr(args, "memory_review", "") or review_payload.get("disposition", "")
    if review:
        if not isinstance(review, str) or review not in MEMORY_REVIEW_STATES:
            raise ValueError("invalid memory_review disposition")
        review_reason = str(getattr(args, "memory_review_reason", "") or review_payload.get("reason", "") or "").strip()
        if not review_reason:
            raise ValueError("--memory-review-reason is required with --memory-review")
        if (review == "written" and not written) or (review == "updated" and not updated):
            raise ValueError("written/updated memory review requires the corresponding entry IDs")
        closeout["memory_review"] = {"disposition": review, "reason": _clip(review_reason)}
        for field, allowed in {
            "target": {"builtin_memory", "personal_kb", "task_writes", "unknown"},
            "instruction_source": {"system", "developer", "user", "task", "unknown"},
        }.items():
            if field in review_payload:
                value = review_payload[field]
                if not isinstance(value, str) or value not in allowed:
                    raise ValueError(f"invalid memory_review {field}")
                closeout["memory_review"][field] = value
    elif review_payload:
        raise ValueError("memory_review requires disposition and reason")
    if written or updated:
        import kb_domain
        owners = set(written) | set(updated)
        links = []
        for record in kb_domain.load_graph()["entries"]:
            if record.get("id") not in owners or record.get("_deleted"):
                continue
            domain = record["domain"]
            for edge in domain.get("relations", []):
                if edge["type"] == "supersedes" and edge["state"] == "confirmed":
                    links.append({"entry_id": record["id"], "successor": domain["entity_id"],
                                  "predecessor": edge["target"], "scope": edge.get("scope", {}),
                                  "valid_from": edge.get("valid_from", domain.get("valid_from", ""))})
        if links:
            closeout["replacement_links"] = links
    if session_brief_used:
        closeout["session_brief_used_entry_ids"] = session_brief_used
    if hit_entry_ids:
        closeout["hit_entry_ids"] = hit_entry_ids
    if telemetry_present:
        closeout["retrieval_items"] = retrieval_items
    if rejected_hit_ids:
        closeout["rejected_hit_ids"] = rejected_hit_ids
    gap = payload.get("memory_gap")
    if gap is not None:
        categories = {"missing_record", "missed_trigger", "missed_recall", "wrong_scope",
                      "stale_fact", "unused_hit", "late_use", "broken_evidence"}
        if not isinstance(gap, dict) or not isinstance(gap.get("stage"), str) or gap["stage"] not in categories:
            raise ValueError("memory_gap requires a supported stage")
        evidence = gap.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("memory_gap requires a short evidence pointer")
        closeout["memory_gap"] = {"stage": gap["stage"], "evidence": _clip(evidence.strip())}
    if rejection_reasons:
        closeout["rejection_reasons"] = rejection_reasons
    if miss_reason:
        closeout["miss_reason"] = miss_reason
    if rag_calls_inferred:
        closeout["rag_calls_inferred"] = True
    closeout["session_brief_hit"] = session_brief_hit
    closeout["session_brief_help"] = session_brief_help
    extra = payload.get("extra")
    if isinstance(extra, dict):
        closeout["extra"] = extra
    if getattr(args, "require_complete_context", False):
        missing = [key for key in ("session_id", "topic_id", "memory_review") if not closeout.get(key)]
        if linked_retrieval_ids and not telemetry_present:
            missing.append("retrieval_snapshot(s) or retrieval_items")
        if missing:
            raise ValueError("complete closeout context requires: " + ", ".join(missing))
    return closeout


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Append AI-only personal-kb closeout state.")
    parser.add_argument("--query", action="append", default=[], help="RAG query used in this task; repeatable")
    parser.add_argument("--hit-count", type=int, default=0, help="Total RAG hit count observed by the AI")
    parser.add_argument(
        "--retrieval-items-json",
        default="",
        help="Optional JSON array/object of compact RAG hit telemetry for quality auditing",
    )
    parser.add_argument("--rag-calls", type=int, default=None, help="Number of RAG/KB calls in this task")
    parser.add_argument("--used", action="append", default=[], help="Legacy adopted entry ID; heats the record")
    parser.add_argument("--used-locate", action="append", default=[], help="Entry only helped locate evidence; record adoption without heat")
    parser.add_argument("--used-decide", action="append", default=[], help="Entry materially changed a decision; heat the record")
    parser.add_argument("--used-fix", action="append", default=[], help="Entry materially helped a diagnosis or fix; heat the record")
    parser.add_argument("--used-write", action="append", default=[], help="Entry materially supported the final output; heat the record")
    parser.add_argument("--written", action="append", default=[], help="Entry ID written by the AI; repeatable")
    parser.add_argument("--updated", action="append", default=[], help="Entry ID updated by the AI; repeatable")
    parser.add_argument("--allowed-hit-id", action="append", default=[], help="Allowed hit candidate entry ID for validating --used; repeatable")
    parser.add_argument(
        "--rejected-hit-id",
        action="append",
        default=[],
        help="Hit candidate explicitly rejected after verification; repeatable",
    )
    parser.add_argument(
        "--rejection-reason",
        action="append",
        default=[],
        metavar="ENTRY_ID=REASON",
        help="Why a hit was rejected; repeatable",
    )
    parser.add_argument(
        "--miss-reason",
        default="",
        choices=sorted(MISS_REASONS),
        help="Why a retrieval was missed or returned no verified hit",
    )
    parser.add_argument("--reason", default="", help="Reason for no use/write/update when skipped")
    parser.add_argument("--repo", default="", help="Override repo bucket for closeout context")
    parser.add_argument("--branch", default="", help="Override branch bucket for closeout context")
    parser.add_argument("--json", default="", help="Inline JSON object to merge into closeout")
    parser.add_argument("--json-file", default="", help="Read closeout JSON object from a UTF-8 file")
    parser.add_argument("--require-complete-context", action="store_true",
                        help="Validate session/topic identity, memory assessment and observed retrieval telemetry before side effects")
    parser.add_argument("--auto-session-brief", action="store_true", help="Write a recent session brief when an explicit summary is provided")
    parser.add_argument("--session-brief-title", default="", help="Override recent session brief title")
    parser.add_argument("--session-brief-summary", default="", help="Override recent session brief summary")
    parser.add_argument("--session-brief-tags", default="", help="Comma-separated recent session brief tags")
    parser.add_argument("--session-brief-anchors", default="", help="Comma-separated recent session brief anchors")
    parser.add_argument("--session-brief-json", default="", help="Inline JSON object to merge into the recent session brief")
    parser.add_argument("--session-brief-json-file", default="", help="Read recent session brief JSON object from a UTF-8 file")
    parser.add_argument("--session-brief-source", default="", help="Recent session brief source, for example codex or cc-switch")
    parser.add_argument("--session-brief-hit", action="store_true", help="Mark that this task hit a recent session brief during retrieval")
    parser.add_argument("--session-brief-help", action="store_true", help="Mark that a recent session brief materially helped this task")
    parser.add_argument("--session-id", default="", help="Optional runtime session identifier")
    parser.add_argument("--topic-id", default="", help="Optional independent topic identifier")
    parser.add_argument("--unresolved-attempt", action="append", default=[], metavar="ID=STATE",
                        help="Record a pending, failed, unavailable, aborted or unknown attempt without inventing a retrieval ID")
    parser.add_argument("--memory-review", default="", choices=["not_reusable", "already_recorded", "pending_verification",
                        "blocked_by_instruction", "rejected_evidence", "written", "updated"],
                        help="Optional outcome of this task's durable-memory assessment; never promotes a record")
    parser.add_argument("--memory-review-reason", default="", help="Evidence or reason for the assessment")
    parser.add_argument("--closeout-id", default="", help="Idempotency key for retrying the same closeout")
    parser.add_argument(
        "--linked-retrieval-id",
        action="append",
        default=[],
        help="Runtime retrieval ID handled by this parent closeout; repeatable",
    )
    parser.add_argument(
        "--legacy-allow-unlinked-retrievals",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--verbose", action="store_true", help="Print one minimal closeout summary")
    output_group.add_argument("--stdout", action="store_true", help="Print the full closeout JSON (compatibility option)")
    output_group.add_argument("--debug", action="store_true", help="Print the full closeout JSON and routing diagnostics")
    parser.add_argument("--no-apply-use", action="store_true", help="Audit only: do not heat --used entries")
    args = parser.parse_args(argv)

    try:
        closeout = build_closeout(args)
        session_brief_payload = _read_session_brief_payload(args)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    path = runtime_file("closeout.jsonl", base_dir=kb_base_dir())
    try:
        with span("closeout.adoption"):
            apply_used_entries(closeout, args)
        with span("closeout.session_brief"):
            apply_session_brief(closeout, args, session_brief_payload)
        closeout["status"] = "partial_failure" if closeout.get("heat_failed_entry_ids") else "ok"
        with span("closeout.append"):
            append_jsonl(path, closeout)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    except Exception as exc:
        heated_ids = closeout.get("heated_entry_ids", [])
        adopted_ids = closeout.get("adopted_entry_ids", [])
        session_brief_ids = closeout.get("session_brief_ids", [])
        side_effects_applied = bool(adopted_ids or session_brief_ids)
        sys.stderr.write(json.dumps({
            "status": "error",
            "error": "closeout_write_failed",
            "message": _clip(f"{type(exc).__name__}: {exc}"),
            "path": str(path),
            "side_effects_applied": side_effects_applied,
            "heated_entry_ids": heated_ids,
            "adopted_entry_ids": adopted_ids,
            "session_brief_ids": session_brief_ids,
            "retry_safe": not side_effects_applied,
            "recovery": (
                "Do not automatically retry: inspect KB side effects, repair closeout storage, "
                "then record recovery without reapplying heat."
                if side_effects_applied
                else "Repair closeout storage and retry."
            ),
        }, ensure_ascii=False) + "\n")
        return 1

    if args.stdout or args.debug:
        sys.stdout.write(json.dumps(closeout, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
    elif args.verbose:
        sys.stdout.write(json.dumps({
            "status": closeout["status"],
            "path": str(path),
            "event": "kb_closeout",
            "heated": len(closeout.get("heated_entry_ids", [])),
            "heat_failed": len(closeout.get("heat_failed_entry_ids", [])),
            "session_briefs_written": len(closeout.get("session_brief_ids", [])),
        }, ensure_ascii=False) + "\n")

    failed_ids = closeout.get("heat_failed_entry_ids", [])
    if failed_ids:
        heated_ids = closeout.get("heated_entry_ids", [])
        adopted_ids = closeout.get("adopted_entry_ids", [])
        sys.stderr.write(json.dumps({
            "status": "partial_failure",
            "error": "adopted_entry_heat_failed",
            "event": "kb_closeout",
            "heat_failed_entry_ids": failed_ids,
            "heated_entry_ids": heated_ids,
            "adopted_entry_ids": adopted_ids,
            "side_effects_applied": bool(adopted_ids),
            "failed_side_effects_unknown": True,
            "retry_safe": False,
            "recovery": "Do not automatically retry the full closeout: inspect failed entries, repair them, and recover without reapplying heat.",
            "path": str(path),
        }, ensure_ascii=False) + "\n")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
