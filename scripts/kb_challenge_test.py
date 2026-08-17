from __future__ import annotations

import io
import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import kb_test_guard

_TEST_GUARD = kb_test_guard.activate(__file__) if __name__ == "__main__" else None

import kb_challenge
from kb_lib import append_jsonl, kb_base_dir, read_jsonl, runtime_file


def _call(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = kb_challenge.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _write_entry(entry_id: str = "entry-1") -> None:
    path = kb_base_dir() / "demo" / "main" / "kb.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": entry_id,
        "kind": "issue",
        "title": "Demo issue",
        "story": "Verified historical lead",
        "record_rev": "rev-1",
        "status": "current",
        "source_paths": ["docs/demo.md"],
    }) + "\n", encoding="utf-8")


def _write_adoption(entry_id: str = "entry-1", *, event_id: str = "adopt-1", session_id: str = "session-1") -> None:
    append_jsonl(runtime_file("adoption_events.jsonl"), {
        "event": "kb_adoption",
        "event_id": event_id,
        "entry_id": entry_id,
        "effect": "decide",
        "repo": "demo",
        "branch": "main",
        "session_id": session_id,
        "ts": "2026-08-17T00:00:00+08:00",
    })


def test_stable_sampling() -> None:
    first = kb_challenge.stable_sample("task-123", 0.10)
    second = kb_challenge.stable_sample("task-123", 0.10)
    assert first == second


def test_normal_skips_even_for_risk_and_challenge_prepares_without_writing() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "personal-kb"
        with patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(root)}, clear=False):
            _write_entry()
            _write_adoption()
            rc, out, err = _call(["prepare", "--task-id", "normal-task", "--entry-id", "entry-1", "--mode", "normal"])
            assert rc == 0, err
            assert json.loads(out)["should_challenge"] is False
            assert not runtime_file("challenge_events.jsonl").exists()

            rc, out, err = _call([
                "prepare", "--task-id", "risk-task", "--task-text", "生产事故需要回滚",
                "--entry-id", "entry-1", "--mode", "normal",
            ])
            assert rc == 0, err
            payload = json.loads(out)
            assert payload["should_challenge"] is False

            rc, out, err = _call([
                "prepare", "--task-id", "risk-task", "--task-text", "生产事故需要回滚",
                "--entry-id", "entry-1", "--mode", "challenge",
                "--adoption-event-id", "adopt-1", "--session-id", "session-1",
            ])
            assert rc == 0, err
            payload = json.loads(out)
            assert payload["should_challenge"] is True
            assert payload["trigger"] == "risk"
            assert payload["execution_timing"] == "immediate"
            assert payload["constraints"]["may_write_kb"] is False
            assert payload["entries"][0]["entry_id"] == "entry-1"

            rc, out, err = _call([
                "prepare", "--task-id", "sample-task", "--task-text", "任务成功",
                "--entry-id", "entry-1", "--mode", "challenge", "--sample-rate", "1",
                "--adoption-event-id", "adopt-1", "--session-id", "session-1",
            ])
            assert rc == 0, err
            sampled = json.loads(out)
            assert sampled["trigger"] == "sample"
            assert sampled["execution_timing"] == "deferred"


def test_unadopted_search_hit_cannot_enter_critic() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "personal-kb"
        with patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(root)}, clear=False):
            _write_entry()
            rc, out, err = _call([
                "prepare", "--task-id", "risk-task", "--task-text", "生产事故",
                "--entry-id", "entry-1", "--mode", "challenge", "--enqueue",
            ])
            assert rc == 1, err
            payload = json.loads(out)
            assert payload["status"] == "unverified_adoption"
            assert payload["unverified_adoption_entry_ids"] == ["entry-1"]
            assert not runtime_file("challenge_events.jsonl").exists()


def test_proposal_is_runtime_only_and_cannot_recurse() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "personal-kb"
        proposal = {
            "schema": "personal-kb.challenge-proposal/v1",
            "critique_depth": 1,
            "entry_ids": ["entry-1"],
            "error_type": "record_error",
            "claim": "Current evidence contradicts the historical cause.",
            "evidence": ["docs/demo.md"],
        }
        with patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(root)}, clear=False):
            _write_entry()
            _write_adoption()
            rc, out, err = _call([
                "prepare", "--task-id", "risk-task", "--task-text", "生产事故",
                "--entry-id", "entry-1", "--mode", "challenge",
                "--adoption-event-id", "adopt-1", "--session-id", "session-1", "--enqueue",
            ])
            assert rc == 0, err
            proposal_id = json.loads(out)["proposal_id"]
            proposal.update({
                "proposal_id": proposal_id,
                "proposed_action": "correct",
                "proposed_change": "Replace the stale cause with the verified one.",
                "why_original_failed": "The historical evidence was stale.",
            })
            rc, out, err = _call(["propose", "--json", json.dumps(proposal)])
            assert rc == 0, err
            assert json.loads(out)["proposal_id"] == proposal_id
            events = read_jsonl(runtime_file("challenge_events.jsonl"))
            assert [event["event"] for event in events[:2]] == ["challenge_queued", "challenge_proposal"]
            assert json.loads((kb_base_dir() / "demo" / "main" / "kb.jsonl").read_text())["story"] == "Verified historical lead"

            rc, out, err = _call([
                "resolve", "--proposal-id", proposal_id, "--decision", "accepted",
                "--verified-against", "docs/demo.md",
            ])
            assert rc == 0, err
            assert json.loads(out)["kb_write_applied"] is False

            recursive = {**proposal, "parent_proposal_id": proposal_id}
            rc, _out, err = _call(["propose", "--json", json.dumps(recursive)])
            assert rc == 3
            assert "recursive" in err

            unqueued = {**proposal, "proposal_id": "challenge-not-queued"}
            rc, _out, err = _call(["propose", "--json", json.dumps(unqueued)])
            assert rc == 3
            assert "queued" in err


def main() -> int:
    test_stable_sampling()
    test_normal_skips_even_for_risk_and_challenge_prepares_without_writing()
    test_unadopted_search_hit_cannot_enter_critic()
    test_proposal_is_runtime_only_and_cannot_recurse()
    print("kb_challenge tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_TEST_GUARD.run(main) if _TEST_GUARD else main())
