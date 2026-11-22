"""Validate a user's wording against its verified owner before learning the alias."""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from kb_evidence import canonical_entry_revision, verify_entry_evidence
from kb_lib import read_jsonl, resolve_context, storage_layout
from kb_record_validation import metadata_errors
from kb_sensitive_scan import sensitive_findings


def invoke(root: Path, args: list[str]) -> dict:
    env = {**os.environ, "PERSONAL_KB_ROOT": str(root)}
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("kb.py")), *args],
                            env=env, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "KB operation failed")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def evaluate(root: Path, entry_id: str, checks: dict, scope: list[str]) -> list[dict]:
    rows = []
    for intent, queries in checks.items():
        for query in queries:
            result = invoke(root, ["retrieve", query, *scope, "--limit", "5", "--json"])
            ids = [item["entry_id"] for item in result["items"]]
            passed = ids[:1] == [entry_id] if intent == "positive" else entry_id not in ids
            rows.append({"type": intent, "query": query, "ids": ids, "passed": passed})
    return rows


def link_bytes(source, dest):
    try:
        os.link(source, dest)
    except OSError:
        shutil.copy2(source, dest)


def snapshot(layout, dest: Path):
    # Mutable JSONL, indexes and manifests get private copies. Only retained,
    # immutable evidence bytes may share an inode with the evaluation snapshot.
    roots = sorted({layout.records, layout.runtime, layout.cache, layout.manifests, layout.retained_files},
                   key=lambda path: len(path.parts))
    copied = []
    for path in roots:
        if path.is_dir() and not any(path.is_relative_to(parent) for parent in copied):
            shutil.copytree(path, dest / path.relative_to(layout.root),
                            copy_function=link_bytes if path == layout.retained_files else shutil.copy2)
            copied.append(path)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("entry_id")
    parser.add_argument("--alias", action="append", required=True, help="Short, evidenced wording for this owner")
    parser.add_argument("--trigger", action="append", default=[], help="Short object or action that corroborates an alias")
    parser.add_argument("--positive", action="append", required=True, help="Question that should rank the owner first")
    parser.add_argument("--negative", action="append", required=True, help="Nearby question that must exclude the owner")
    parser.add_argument("--repo", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--apply", action="store_true", help="Publish a passing candidate with its tested revision")
    args = parser.parse_args(argv)
    try:
        layout = storage_layout()
        ctx = resolve_context(cwd=Path.cwd(), repo_name_override=args.repo or None,
                              branch_override=args.branch or None, operation="search")
        matches = [r for r in read_jsonl(ctx.kb_path) if r.get("id") == args.entry_id and not r.get("_deleted")]
        if len(matches) != 1:
            raise ValueError("The named owner must exist exactly once in this project and branch")
        entry = matches[0]
        if (entry.get("_archived") or entry.get("superseded_by") or
                entry.get("status", "current") not in {"current", "active", "implemented", "decision_confirmed", "partial_current"} or
                verify_entry_evidence(entry, Path(entry["workspace_dir"]))["state"] != "fresh"):
            raise ValueError("Verify the current owner and refresh its evidence before learning from it")
        aliases = list(dict.fromkeys([*entry.get("aliases", []), *(s.strip() for s in args.alias)]))
        triggers = list(dict.fromkeys([*entry.get("trigger_terms", []), *(s.strip() for s in args.trigger)]))
        checks = {key: list(dict.fromkeys([*entry.get("recall_checks", {}).get(key, []),
                                          *(s.strip() for s in getattr(args, key))]))
                  for key in ("positive", "negative")}
        if (len(args.alias) > 8 or len(args.trigger) > 15 or any(not s or len(s) > 80 for s in aliases + triggers) or
                any(not s or len(s) > 240 for group in checks.values() for s in group) or
                any(len(group) > 12 for group in (args.positive, args.negative)) or set(checks["positive"]) & set(checks["negative"])):
            raise ValueError("Use distinct, bounded probes and short owner-specific aliases")
        change = {"aliases": aliases, "trigger_terms": triggers, "recall_checks": checks}
        problems = metadata_errors({**entry, **change})
        if problems:
            raise ValueError("; ".join(problems))
        if sensitive_findings(change):
            raise ValueError("Remove credential-shaped material from the proposed recall checks")
        encoded = base64.b64encode(json.dumps(change, ensure_ascii=False).encode()).decode()
        scope = ["--repo", ctx.repo_name, "--branch", ctx.branch]
        update = ["update", args.entry_id, *scope, "--expected-rev",
                  entry.get("record_rev") or canonical_entry_revision(entry), "--entry-b64", encoded]
        with tempfile.TemporaryDirectory(prefix="personal-kb-recall-") as tmp:
            trial = Path(tmp)
            snapshot(layout, trial)
            before = evaluate(trial, args.entry_id, checks, scope)
            invoke(trial, update)
            after = evaluate(trial, args.entry_id, checks, scope)
        for prior, candidate in zip(before, after):
            if prior["type"] == "negative" and prior["passed"] and prior["ids"][:1] != candidate["ids"][:1]:
                candidate["passed"] = False
        improved = any(not row["passed"] and new["passed"] for row, new in zip(before, after))
        passed = all(row["passed"] for row in after)
        status = "regression" if not passed else "no_gain"
        if passed and improved:
            status = "candidate"
            if args.apply:
                invoke(layout.root, update)
                status = "applied"
        print(json.dumps({"status": status, "entry_id": args.entry_id,
                          "before": before, "after": after}, ensure_ascii=False))
        return 0 if passed else 1
    except (ValueError, OSError) as exc:
        parser.exit(2, f"{exc}\n")
