#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from kb_adoption import ADOPTION_EVENT, HEAT_EFFECTS, adoption_events_path
from kb_lib import append_jsonl, kb_base_dir, load_config, now_iso, read_jsonl, runtime_file
from kb_sensitive_scan import sensitive_findings


ERROR_TYPES = {
    "record_error",
    "retrieval_error",
    "scope_error",
    "application_error",
    "evidence_error",
    "outcome_unknown",
}
RESOLUTIONS = {"accepted", "rejected", "deferred"}
PROPOSED_ACTIONS = {"keep", "correct", "supersede", "defer"}
DEFAULT_RISK_TERMS = (
    "用户否定",
    "证据冲突",
    "高风险",
    "生产事故",
    "数据丢失",
    "安全",
    "回滚",
    "结果异常",
    "contradiction",
    "security",
    "production incident",
)


def _challenge_config() -> dict[str, Any]:
    config = load_config()
    challenge = config.get("challenge") if isinstance(config, dict) else {}
    return challenge if isinstance(challenge, dict) else {}


def _runtime_mode() -> str:
    config = load_config()
    runtime = config.get("runtime") if isinstance(config, dict) else {}
    mode = runtime.get("mode", "normal") if isinstance(runtime, dict) else "normal"
    return str(mode).strip().lower() or "normal"


def stable_sample(task_id: str, rate: float) -> tuple[bool, float]:
    """Return a retry-stable sampling decision and the task's hash fraction."""
    if not 0.0 <= rate <= 1.0:
        raise ValueError("sample rate must be between 0 and 1")
    digest = hashlib.sha256(task_id.encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return fraction < rate, fraction


def _risk_from_text(task_text: str, configured_terms: Any) -> tuple[bool, list[str]]:
    terms = configured_terms if isinstance(configured_terms, list) else list(DEFAULT_RISK_TERMS)
    lowered = task_text.lower()
    matched = [str(term) for term in terms if str(term).strip() and str(term).lower() in lowered]
    return bool(matched), matched[:5]


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        clean = value.strip()
        if clean and clean not in out:
            out.append(clean)
    return out


def _find_entries(entry_ids: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    wanted = set(entry_ids)
    found: dict[str, dict[str, Any]] = {}
    for kb_file in sorted(kb_base_dir().rglob("kb.jsonl")):
        for row in read_jsonl(kb_file):
            entry_id = str(row.get("id", ""))
            if entry_id in wanted and entry_id not in found:
                found[entry_id] = {
                    "entry_id": entry_id,
                    "kind": row.get("kind", ""),
                    "title": row.get("title", ""),
                    "story": str(row.get("story", ""))[:1600],
                    "record_rev": row.get("record_rev", ""),
                    "status": row.get("status", ""),
                    "source_paths": row.get("source_paths", []),
                    "retained_assets": row.get("retained_assets", []),
                    "kb_file": str(kb_file),
                }
        if len(found) == len(wanted):
            break
    return [found[entry_id] for entry_id in entry_ids if entry_id in found], [entry_id for entry_id in entry_ids if entry_id not in found]


def _adoption_proofs(
    entry_ids: list[str],
    *,
    event_ids: list[str],
    session_id: str,
) -> tuple[list[dict[str, str]], list[str]]:
    """Verify that every critic candidate was materially adopted in runtime telemetry."""
    allowed_event_ids = set(_dedupe(event_ids))
    clean_session_id = session_id.strip()
    if not allowed_event_ids and not clean_session_id:
        return [], list(entry_ids)

    wanted = set(entry_ids)
    matched: dict[str, dict[str, str]] = {}
    for event in read_jsonl(adoption_events_path()):
        if event.get("event") != ADOPTION_EVENT:
            continue
        event_id = str(event.get("event_id", "")).strip()
        event_session_id = str(event.get("session_id", "")).strip()
        entry_id = str(event.get("entry_id", "")).strip()
        effect = str(event.get("effect", "")).strip().lower()
        if entry_id not in wanted or effect not in HEAT_EFFECTS:
            continue
        if allowed_event_ids and event_id not in allowed_event_ids:
            continue
        if clean_session_id and event_session_id != clean_session_id:
            continue
        matched.setdefault(
            entry_id,
            {
                "entry_id": entry_id,
                "event_id": event_id,
                "session_id": event_session_id,
                "effect": effect,
            },
        )
    return [matched[value] for value in entry_ids if value in matched], [value for value in entry_ids if value not in matched]


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _events_path() -> Path:
    return runtime_file("challenge_events.jsonl")


def prepare(args: argparse.Namespace) -> int:
    config = _challenge_config()
    task_id = args.task_id.strip()
    if not task_id:
        sys.stderr.write("--task-id is required\n")
        return 2

    mode = (args.mode or _runtime_mode()).strip().lower()
    if mode not in {"normal", "challenge"}:
        sys.stderr.write("--mode must be normal or challenge\n")
        return 2

    entry_ids = _dedupe(args.entry_id)
    max_entries = int(config.get("max_adopted_entries", 3) or 3)
    if not entry_ids:
        sys.stderr.write("at least one --entry-id is required\n")
        return 2
    if len(entry_ids) > max_entries:
        sys.stderr.write(f"challenge accepts at most {max_entries} adopted entries\n")
        return 2

    rate = args.sample_rate if args.sample_rate is not None else float(config.get("success_sample_rate", 0.10))
    sampled, fraction = stable_sample(task_id, rate)
    text_risk, matched_risk_terms = _risk_from_text(args.task_text, config.get("risk_terms"))
    is_risk = bool(args.risk or text_risk)
    should_challenge = bool(args.force or (mode == "challenge" and (is_risk or sampled)))
    trigger = "forced" if args.force else "risk" if is_risk else "sample" if should_challenge else "none"
    if not should_challenge:
        trigger = "none"
    defer_success = bool(config.get("defer_success_samples", True))
    execution_timing = "deferred" if trigger == "sample" and defer_success else "immediate" if should_challenge else "none"

    adoption_proofs: list[dict[str, str]] = []
    unverified_adoption_ids: list[str] = []
    if should_challenge and not args.force:
        adoption_proofs, unverified_adoption_ids = _adoption_proofs(
            entry_ids,
            event_ids=args.adoption_event_id,
            session_id=args.session_id,
        )
    entries, missing = _find_entries(entry_ids) if should_challenge else ([], [])
    fingerprint_payload = {
        "task_id": task_id,
        "entry_revisions": [(entry["entry_id"], entry.get("record_rev", "")) for entry in entries],
        "adoption_event_ids": [proof["event_id"] for proof in adoption_proofs],
        "trigger": trigger,
        "execution_timing": execution_timing,
    }
    proposal_id = f"challenge-{_fingerprint(fingerprint_payload)[:20]}"
    brief = {
        "schema": "personal-kb.challenge-brief/v1",
        "proposal_id": proposal_id,
        "task_id": task_id,
        "mode": mode,
        "should_challenge": should_challenge,
        "trigger": trigger,
        "execution_timing": execution_timing,
        "risk_terms": matched_risk_terms,
        "sample_rate": rate,
        "sample_fraction": round(fraction, 12),
        "entry_ids": entry_ids,
        "entries": entries,
        "missing_entry_ids": missing,
        "adoption_proofs": adoption_proofs,
        "unverified_adoption_entry_ids": unverified_adoption_ids,
        "critique_depth": 1,
        "allowed_error_types": sorted(ERROR_TYPES),
        "constraints": {
            "proposal_only": True,
            "may_write_kb": False,
            "may_heat": False,
            "may_closeout": False,
            "may_recurse": False,
            "verify_against_current_evidence": True,
            "adoption_proof_required": not args.force,
        },
    }
    if should_challenge and unverified_adoption_ids:
        brief["status"] = "unverified_adoption"
    elif should_challenge and missing:
        brief["status"] = "missing_entries"
    else:
        brief["status"] = "ready" if should_challenge else "skipped"

    if args.enqueue and should_challenge and not missing and not unverified_adoption_ids:
        append_jsonl(_events_path(), {"event": "challenge_queued", "ts": now_iso(), **brief})
    sys.stdout.write(json.dumps(brief, ensure_ascii=False, indent=2) + "\n")
    return 1 if missing or unverified_adoption_ids else 0


def _read_payload(args: argparse.Namespace) -> dict[str, Any]:
    raw = args.json
    if args.json_file:
        raw = Path(args.json_file).read_text(encoding="utf-8-sig")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"invalid proposal JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("proposal must be a JSON object")
    return payload


def propose(args: argparse.Namespace) -> int:
    try:
        payload = _read_payload(args)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    if payload.get("schema") != "personal-kb.challenge-proposal/v1":
        sys.stderr.write("proposal schema must be personal-kb.challenge-proposal/v1\n")
        return 2
    if int(payload.get("critique_depth", 1) or 1) != 1 or payload.get("parent_proposal_id"):
        sys.stderr.write("recursive challenge proposals are forbidden\n")
        return 3
    proposal_id = str(payload.get("proposal_id", "")).strip()
    queued = next(
        (
            event
            for event in reversed(read_jsonl(_events_path()))
            if event.get("event") == "challenge_queued" and event.get("proposal_id") == proposal_id
        ),
        None,
    )
    if not proposal_id or queued is None:
        sys.stderr.write("proposal must reference a queued challenge brief\n")
        return 3
    error_type = str(payload.get("error_type", ""))
    if error_type not in ERROR_TYPES:
        sys.stderr.write(f"invalid error_type; allowed={','.join(sorted(ERROR_TYPES))}\n")
        return 2
    entry_ids = _dedupe([str(value) for value in payload.get("entry_ids", [])])
    max_entries = int(_challenge_config().get("max_adopted_entries", 3) or 3)
    if not entry_ids or len(entry_ids) > max_entries:
        sys.stderr.write(f"proposal entry_ids must contain 1-{max_entries} IDs\n")
        return 2
    queued_entry_ids = {str(value) for value in queued.get("entry_ids", [])}
    if not set(entry_ids).issubset(queued_entry_ids):
        sys.stderr.write("proposal entry_ids must come from the queued challenge brief\n")
        return 3
    if not str(payload.get("claim", "")).strip():
        sys.stderr.write("proposal claim is required\n")
        return 2
    evidence = payload.get("current_evidence", payload.get("evidence"))
    if isinstance(evidence, str):
        evidence_ok = bool(evidence.strip())
    elif isinstance(evidence, list):
        evidence_ok = any(str(value).strip() for value in evidence)
    else:
        evidence_ok = False
    if not evidence_ok:
        sys.stderr.write("proposal current_evidence is required\n")
        return 2
    proposed_action = str(payload.get("proposed_action", "")).strip().lower()
    if proposed_action not in PROPOSED_ACTIONS:
        sys.stderr.write(f"proposal proposed_action must be one of: {','.join(sorted(PROPOSED_ACTIONS))}\n")
        return 2
    if proposed_action in {"correct", "supersede"} and not str(payload.get("proposed_change", "")).strip():
        sys.stderr.write("proposal proposed_change is required for correction or supersession\n")
        return 2
    if not str(payload.get("why_original_failed", "")).strip():
        sys.stderr.write("proposal why_original_failed is required\n")
        return 2
    findings = sensitive_findings(payload)
    if findings:
        sys.stderr.write(
            "proposal contains credential-shaped content; redact it or use a retained evidence reference. "
            f"finding_types={','.join(findings)}\n"
        )
        return 4

    fingerprint = _fingerprint(payload)
    for event in read_jsonl(_events_path()):
        if event.get("event") == "challenge_proposal" and event.get("fingerprint") == fingerprint:
            sys.stdout.write(json.dumps({"status": "duplicate", "proposal_id": event.get("proposal_id", ""), "fingerprint": fingerprint}, ensure_ascii=False) + "\n")
            return 0

    event = {
        "event": "challenge_proposal",
        "status": "pending",
        "ts": now_iso(),
        "proposal_id": proposal_id,
        "fingerprint": fingerprint,
        "task_id": str(queued.get("task_id", "")),
        "payload": payload,
    }
    append_jsonl(_events_path(), event)
    sys.stdout.write(json.dumps({"status": "pending", "proposal_id": proposal_id, "fingerprint": fingerprint}, ensure_ascii=False) + "\n")
    return 0


def resolve(args: argparse.Namespace) -> int:
    proposal_id = args.proposal_id.strip()
    decision = args.decision.strip().lower()
    if not proposal_id or decision not in RESOLUTIONS:
        sys.stderr.write(f"proposal id is required and decision must be one of: {','.join(sorted(RESOLUTIONS))}\n")
        return 2
    known = any(event.get("event") == "challenge_proposal" and event.get("proposal_id") == proposal_id for event in read_jsonl(_events_path()))
    if not known:
        sys.stderr.write(f"proposal not found: {proposal_id}\n")
        return 2
    verified_against = _dedupe(args.verified_against)
    if decision in {"accepted", "rejected"} and not verified_against:
        sys.stderr.write("accepted or rejected resolutions require --verified-against\n")
        return 2
    append_jsonl(_events_path(), {
        "event": "challenge_resolution",
        "ts": now_iso(),
        "proposal_id": proposal_id,
        "decision": decision,
        "verified_against": verified_against,
        "reason": args.reason.strip(),
        "kb_write_applied": False,
    })
    sys.stdout.write(json.dumps({"status": "resolved", "proposal_id": proposal_id, "decision": decision, "kb_write_applied": False}, ensure_ascii=False) + "\n")
    return 0


def list_events(_args: argparse.Namespace) -> int:
    sys.stdout.write(json.dumps(read_jsonl(_events_path()), ensure_ascii=False, indent=2) + "\n")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Prepare and record bounded Personal KB challenge proposals.")
    sub = parser.add_subparsers(dest="action")

    prepare_cmd = sub.add_parser("prepare", help="Prepare a read-only challenge brief for adopted KB entries.")
    prepare_cmd.add_argument("--task-id", required=True)
    prepare_cmd.add_argument("--task-text", default="")
    prepare_cmd.add_argument("--entry-id", action="append", default=[], required=True)
    prepare_cmd.add_argument("--mode", choices=["normal", "challenge"], default="")
    prepare_cmd.add_argument("--risk", action="store_true")
    prepare_cmd.add_argument("--force", action="store_true")
    prepare_cmd.add_argument("--sample-rate", type=float, default=None)
    prepare_cmd.add_argument("--adoption-event-id", action="append", default=[])
    prepare_cmd.add_argument("--session-id", default="")
    prepare_cmd.add_argument("--enqueue", action="store_true")

    propose_cmd = sub.add_parser("propose", help="Record an isolated critic proposal without changing KB.")
    proposal_source = propose_cmd.add_mutually_exclusive_group(required=True)
    proposal_source.add_argument("--json", default="")
    proposal_source.add_argument("--json-file", default="")

    resolve_cmd = sub.add_parser("resolve", help="Record the main session's verified proposal decision.")
    resolve_cmd.add_argument("--proposal-id", required=True)
    resolve_cmd.add_argument("--decision", choices=sorted(RESOLUTIONS), required=True)
    resolve_cmd.add_argument("--verified-against", action="append", default=[])
    resolve_cmd.add_argument("--reason", default="")

    sub.add_parser("list", help="List challenge runtime events.")
    args = parser.parse_args(argv)
    if args.action == "prepare":
        return prepare(args)
    if args.action == "propose":
        return propose(args)
    if args.action == "resolve":
        return resolve(args)
    if args.action == "list":
        return list_events(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
