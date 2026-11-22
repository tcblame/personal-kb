from __future__ import annotations

import json
import tempfile
from pathlib import Path

import kb_test_guard

_TEST_GUARD = kb_test_guard.activate(__file__) if __name__ == "__main__" else None

import kb_audit_cursor_sessions as audit


def test_shell_request_and_heredoc_are_separated() -> None:
    calls, uncertain = audit.invocations(
        "python3 kb.py retrieve 'anchor' --topic-id t1 && "
        "python3 - <<'PY'\nsubprocess.run(['python3','kb.py','remember'])\nPY"
    )
    assert [call["operation"] for call in calls] == ["retrieve"]
    assert uncertain is True


def test_cursor_transcript_counts_requests_without_claiming_success() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "session.jsonl"
        path.write_text(json.dumps({"role": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Shell", "input": {
                "command": "python3 kb.py retrieve 'anchor'"
            }}
        ]}}) + "\n", encoding="utf-8")
        report = audit.audit_session(path)
    assert report["calls"][0]["execution_status"] == "unknown"
    assert report["tool_result_items"] == 0


def main():
    test_shell_request_and_heredoc_are_separated()
    test_cursor_transcript_counts_requests_without_claiming_success()
    print("kb_cursor_audit tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_TEST_GUARD.run(main))
