"""Dated observations on existing domain entities, kept inside the owning KB row."""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import re
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path

import kb_domain
import kb_evidence


def bounds(period: str, zone=timezone.utc) -> tuple[datetime, datetime]:
    """Preserve the stated precision; timestamp inputs carry their own UTC offset."""
    if not isinstance(period, str):
        raise ValueError("observation date must be text")
    if "T" in period:
        stamp = datetime.fromisoformat(period.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("timestamps require an explicit UTC offset")
        return stamp, stamp
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}", period):
        first = date.fromisoformat(period + "-01")
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", period):
        first = last = date.fromisoformat(period)
    else:
        raise ValueError("expected YYYY-MM, YYYY-MM-DD or an offset timestamp")
    return datetime.combine(first, time.min, zone), datetime.combine(last, time.max, zone)


def event_id(event: dict) -> str:
    data = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def usable_events(events: list[dict]) -> list[dict]:
    withdrawn = {e["withdraws"] for e in events if "withdraws" in e}
    return [e for e in events if "withdraws" not in e and event_id(e) not in withdrawn]


def observation_errors(events: object) -> list[str]:
    if not isinstance(events, list):
        return ["domain.observations must be a list"]
    for event in events:
        if not isinstance(event, dict) or set(event) - {
            "field", "value", "date", "state", "basis", "evidence_refs", "target", "withdraws"
        }:
            return ["observation must be an object with supported fields"]
        try:
            bounds(event.get("date"))
        except ValueError as exc:
            return [str(exc)]
        for key in ("field", "value"):
            if not isinstance(event.get(key), str) or not event[key].strip() or len(event[key]) > 320:
                return ["observation field/value must be concise non-empty strings"]
        if event.get("state") not in kb_domain.model()["states"]:
            return ["observation requires a supported state"]
        if event.get("basis") not in ("observed", "effective", "ended"):
            return ["observation basis must be observed, effective or ended"]
        if "withdraws" in event and (event["state"] != "confirmed" or
                                    not isinstance(event["withdraws"], str) or
                                    not re.fullmatch(r"[0-9a-f]{64}", event["withdraws"])):
            return ["withdraws requires a confirmed correction and a full observation ID"]
        if "target" in event and (not isinstance(event["target"], str) or not kb_domain.ID.fullmatch(event["target"])):
            return ["observation target must be a stable entity ID"]
        refs = event.get("evidence_refs")
        if not refs:
            return ["observation requires evidence_refs"]
        errors = kb_evidence.validate_evidence_refs(refs)
        if errors:
            return errors
    return []


def confirmed_names(events: list[dict]) -> list[str]:
    return [e["value"] for e in usable_events(events)
            if e["field"] in {"name", "alias"} and e["state"] == "confirmed"]


def check_new_assets(events: list[dict]) -> list[str]:
    """Check archived bytes at write time, including the manifest content digest."""
    checked = set()
    for event in events:
        for ref in event["evidence_refs"]:
            if ref["type"] == "retained" and ref["value"] not in checked:
                checked.add(ref["value"])
                capture = kb_evidence._capture_retained_ref(ref)
                row = kb_evidence._retained_manifest_row(ref["value"]) or {}
                if not capture.get("resolvable") or not row.get("sha256") or capture.get("sha256") != row["sha256"]:
                    return [f"retained observation evidence missing or changed: {ref['value']}"]
    return []


def append_file(entry: dict, filename: str) -> None:
    if not isinstance(entry.get("domain"), dict):
        raise ValueError("register a stable domain entity before appending observations")
    data = json.loads(Path(filename).read_text(encoding="utf-8-sig"))
    batch = data if isinstance(data, list) else [data]
    if not batch:
        raise ValueError("empty observation batch")
    errors = observation_errors(batch)
    if errors:
        raise ValueError(", ".join(errors))
    history = list(entry["domain"].get("observations", []))
    for event in batch:
        if event not in history:
            history.append(event)
    entry["domain"] = {**entry["domain"], "observations": history}


def view(events: list[dict], as_of: datetime, zone) -> list[dict]:
    by_field: dict[str, list] = {}
    for e in usable_events(events):
        first, last = bounds(e["date"], zone)
        if first <= as_of:
            by_field.setdefault(e["field"], []).append((first, last, e))
    result = []
    for key, rows in sorted(by_field.items()):
        # Among possibly overlapping intervals, preserve every possible latest value.
        confirmed = [r for r in rows if r[2]["state"] == "confirmed" and r[1] <= as_of]
        floor = max((r[0] for r in confirmed), default=datetime.min.replace(tzinfo=timezone.utc))
        relevant = [r for r in rows if r[1] >= floor and r[2]["state"] != "candidate"]
        if not relevant:
            continue
        identities = {(e["value"], e.get("target"), e["basis"] == "ended") for _, _, e in relevant}
        uncertain = len(identities) > 1 or any(
            last > as_of or e["state"] == "conflicted" for _, last, e in relevant
        )
        status = "uncertain" if uncertain else ("ended" if relevant[0][2]["basis"] == "ended" else "last_known")
        result.append({"field": key, "status": status, "observations": [e for _, _, e in relevant]})
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Read the history of one stable project object")
    parser.add_argument("object", help="Stable entity ID, title, or exact current/historical name")
    parser.add_argument("--project-id", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--as-of", help="Latest known by the end of this period or at an offset timestamp")
    mode.add_argument("--history", action="store_true", help="Include dated events, candidates and evidence")
    parser.add_argument("--verify-assets", action="store_true", help="Verify archived evidence used on this page")
    parser.add_argument("--field", help="Limit output to one independent relationship or property")
    parser.add_argument("--branch", default="")
    parser.add_argument("--repo", default="")
    parser.add_argument("--environment", default="")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="Calendar zone for month/day precision")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        zone = ZoneInfo(args.timezone)
        cutoff = bounds(args.as_of, zone)[1] if args.as_of else datetime.now(zone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        parser.error(str(exc))
    if args.offset < 0 or not 1 <= args.limit <= 500:
        parser.error("offset must be nonnegative and limit in 1..500")
    lookup = args.object.strip().casefold()
    matches = []
    scope = {"branch": args.branch, "repo_id": args.repo, "environment": args.environment}
    for entry in kb_domain.load_graph()["entries"]:
        d = entry["domain"]
        if d["project_id"] != args.project_id or entry.get("_deleted"):
            continue
        if any(scope.get(key) != value for key, value in d.get("scope", {}).items()):
            continue
        names = [d["entity_id"], entry.get("title", ""), *entry.get("aliases", [])]
        events = d.get("observations", [])
        names.extend(confirmed_names(events))
        if lookup and lookup in {str(name).casefold() for name in names}:
            matches.append(entry)
    if len(matches) > 1 and args.as_of:
        # A dated positive match can disambiguate reuse only when every other
        # owner has a known, different name in that period.
        possible = []
        for entry in matches:
            fields = view(entry["domain"].get("observations", []), cutoff, zone)
            names = [row for row in fields if row["field"] == "name"]
            if (not names or names[0]["status"] == "uncertain"
                    or any(e["value"].casefold() == lookup for e in names[0]["observations"])
                    or lookup == entry["domain"]["entity_id"].casefold()
                    or lookup in {str(x).casefold() for x in entry.get("aliases", [])}):
                possible.append(entry)
        matches = possible
    if len(matches) != 1:
        payload = {"status": "ambiguous" if matches else "not_found",
                   "matches": [e["domain"]["entity_id"] for e in matches]}
    else:
        entry = matches[0]
        d = entry["domain"]
        events = sorted((e for e in d.get("observations", []) if not args.field or e["field"] == args.field),
                        key=lambda e: bounds(e["date"], zone))
        payload = {"status": "found", "entity_id": d["entity_id"], "entry_id": entry["id"],
                   "owner_state": d["state"], "record_status": entry.get("status"),
                   "project_id": d["project_id"], "record_rev": entry.get("record_rev") or kb_evidence.canonical_entry_revision(entry)}
        if args.history:
            payload["history"] = [dict(e, observation_id=event_id(e)) for e in events[args.offset:args.offset + args.limit]]
            payload["total"] = len(events)
        else:
            fields = view(events, cutoff, zone)
            payload.update(as_of=cutoff.isoformat(), total=len(fields), fields=fields[args.offset:args.offset + args.limit])
            owner_valid = (d.get("state") == "confirmed"
                           and kb_domain.declaration_active(d, scope, now=cutoff)
                           and entry.get("status", "") in kb_domain.CURRENT and not entry.get("superseded_by"))
            if not owner_valid:
                payload["status"] = "needs_review"
        if args.verify_assets:
            page = payload.get("history", []) if args.history else [
                obs for field in payload["fields"] for obs in field["observations"]
            ]
            errors = check_new_assets(page)
            payload["asset_errors"] = errors
            if errors:
                payload["status"] = "needs_review"
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0
