#!/usr/bin/env python3
"""Frozen monthly recall/noise sample; sanitized corpus, never production writes."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import kb_test_guard

_TEST_GUARD = kb_test_guard.activate(__file__) if __name__ == "__main__" else None

import kb
from kb_eval_preflight import predict_preflight


def main():
    directory = Path(__file__).resolve().parents[1] / "references/evals"
    inputs = json.loads((directory / "monthly-retrieval-cases.json").read_text())["cases"]
    gold = {row["id"]: row for row in json.loads((directory / "monthly-retrieval-gold.json").read_text())["cases"]}
    assert len(inputs) == len(gold) == 30
    assert {row["id"] for row in inputs} == set(gold)
    outcomes = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "data"
        config = Path(tmp) / "config.json"
        config.write_text(json.dumps({"storage": {"root": str(root), "records": "records", "runtime": "runtime", "cache": "cache"}}))
        for row in json.loads((directory / "monthly-retrieval-corpus.json").read_text())["records"]:
            path = root / "records" / row["repo"] / row["branch"] / "kb.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
        for case in inputs:
            expected = gold[case["id"]]
            if case["category"] == "routing_simulator":
                action = predict_preflight(case["prompt"], case.get("context"))["action"]
                outcomes.append({"id": case["id"], "pass": action == expected["action"], "action": action})
                continue
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(root), "PERSONAL_KB_CONFIG": str(config)}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = kb.main(["retrieve", case["query"], "--global", "--no-domain", "--no-session-briefs", "--limit", "10", "--json", *case.get("options", [])])
            payload = json.loads(out.getvalue())
            hits = {row["entry_id"] for row in payload.get("items", [])}
            passed = code == 0 and set(expected["must_include"]).issubset(hits) and not hits.intersection(expected["must_exclude"])
            outcomes.append({"id": case["id"], "pass": passed, "hits": sorted(hits), "exit_code": code})
        assert hashes == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}, "retrieval changed isolated corpus"
    report = {"cases": len(outcomes), "passed": sum(row["pass"] for row in outcomes), "outcomes": outcomes,
              "scope": "sanitized retrieval plus deterministic routing simulation; not live task success"}
    if len(sys.argv) == 3 and sys.argv[1] == "--report":
        Path(sys.argv[2]).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"cases": report["cases"], "passed": report["passed"], "failures": [row for row in outcomes if not row["pass"]]}, ensure_ascii=False))
    return 0 if report["cases"] == report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(_TEST_GUARD.run(main))
