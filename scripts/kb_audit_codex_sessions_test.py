#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

import kb_test_guard

_TEST_GUARD = kb_test_guard.activate(__file__) if __name__ == "__main__" else None

import kb_audit_codex_sessions as audit
from kb_audit_codex_sessions import build_report


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _tool_call(call_id: str, command: str, turn_id: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec",
            "call_id": call_id,
            "arguments": json.dumps({"cmd": command}),
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        },
    }


def _tool_output(call_id: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": "Process exited with code 0\n",
        },
    }


def _tool_output_text(call_id: str, output: str) -> dict:
    row = _tool_output(call_id)
    row["payload"]["output"] = output
    return row


def test_public_wrapper_commands_are_detected() -> None:
    retrieve = "python3 /tmp/kb.py retrieve 'old mapping' --limit 5"
    closeout = "python3 /tmp/kb.py closeout --query 'old mapping' --rag-calls 1"
    help_only = "python3 /tmp/kb.py retrieve --help"

    assert audit._detected_scripts(retrieve) == ["kb_rag_context"]
    assert audit._rag_query(audit._parse_cli_tokens(retrieve), "kb_rag_context.py") == "old mapping"
    assert audit._detected_scripts(closeout) == ["kb_closeout"]
    assert audit._is_help_invocation(audit._parse_cli_tokens(help_only), "kb_rag_context.py") is True


def test_chained_wrapper_retrievals_keep_distinct_results() -> None:
    session_id = "11111111-2222-7333-8444-555555555555"
    ids = [f"chain-retrieval-{index}" for index in range(5)]
    command = " && ".join(
        f"python3 /tmp/kb.py retrieve 'topic {index}' --json"
        for index in range(5)
    )
    output = "Script completed\n" + "\n".join(
        json.dumps({
            "mode": "read_only_rag_context",
            "retrieval_id": retrieval_id,
            "items": [],
        })
        for retrieval_id in ids
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / f"rollout-2026-08-17T10-00-00-{session_id}.jsonl"
        _write_jsonl(path, [
            {
                "type": "session_meta",
                "payload": {"id": session_id, "thread_source": "user", "source": "cli"},
            },
            _tool_call("rag-chain", command, "turn-1"),
            _tool_output_text("rag-chain", output),
        ])
        parsed = audit._parse_session(path)

    assert parsed["rag_or_search"] == 5
    assert [row["retrieval_id"] for row in parsed["retrieval_events"]] == ids
    assert parsed["retrieval_id_missing_count"] == 0


def test_parent_scout_retrieval_link_avoids_false_missing_reports() -> None:
    audit_day = date.today()
    parent_id = "33333333-3333-7333-8333-333333333333"
    child_id = "44444444-4444-7444-8444-444444444444"
    retrieval_id = "audit-link-1"
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        day_dir = temp / "sessions" / f"{audit_day:%Y}" / f"{audit_day:%m}" / f"{audit_day:%d}"
        _write_jsonl(
            day_dir / f"rollout-{audit_day.isoformat()}T10-00-00-{parent_id}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {"id": parent_id, "thread_source": "user", "source": "cli"},
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "查以前多个项目的 Personal KB 记录。"},
                },
                _tool_call(
                    "closeout",
                    "python3 /tmp/kb_closeout.py --query 'old mapping' --rag-calls 1 "
                    f"--hit-count 1 --reason recorded --linked-retrieval-id {retrieval_id}",
                    "turn-parent",
                ),
                _tool_output("closeout"),
            ],
        )
        _write_jsonl(
            day_dir / f"rollout-{audit_day.isoformat()}T10-01-00-{child_id}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": child_id,
                        "thread_source": "subagent",
                        "parent_thread_id": parent_id,
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": parent_id,
                                    "agent_role": "kb_scout",
                                }
                            }
                        },
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "KB scout 检索旧路径映射。"},
                },
                _tool_call("rag", "python3 /tmp/kb_rag_context.py 'old mapping'", "turn-child"),
                _tool_output_text(
                    "rag",
                    "Script completed\n"
                    f"KB_RAG_CONTEXT retrieval_id=\"{retrieval_id}\" query=\"old mapping\" hits=1\n",
                ),
            ],
        )
        closeout = temp / "closeout.jsonl"
        closeout.write_text("", encoding="utf-8")
        report = build_report(
            argparse.Namespace(
                sessions_root=str(temp / "sessions"),
                date=[audit_day.isoformat()],
                last_days=0,
                closeout=str(closeout),
                examples=10,
            )
        )

    assert report["main_missed_rag_sessions"] == 0
    assert report["rag_without_closeout_sessions"] == 0
    assert report["parent_sessions_with_delegated_retrieval"] == 1
    assert report["scout_retrieval_linked_to_parent_closeout_count"] == 1
    assert report["orphan_scout_retrieval_id_count"] == 0


def test_audit_parser_uses_canonical_header_id() -> None:
    output = (
        'KB_RAG_CONTEXT query="retrieval_id="fake"" retrieval_id="real-id" hits=0\n'
        '- note: retrieval_id="later-fake"\n'
        '- note: {"mode":"read_only_rag_context","retrieval_id":"json-fake"}\n'
    )
    assert audit._retrieval_id_from_output(output) == "real-id"
    invalid_json = json.dumps(
        {"mode": "read_only_rag_context", "retrieval_id": "invalid id"}
    )
    assert audit._retrieval_id_from_output(invalid_json) == ""


def test_audit_parser_unwraps_nested_exec_output() -> None:
    retrieval_id = "a64f65889b9b49c5b2bdfe13036816e8"
    nested_header = json.dumps({
        "output": (
            "Script completed\n"
            f'KB_RAG_CONTEXT retrieval_id="{retrieval_id}" query="update ccs" hits=1\n'
        ),
        "exit_code": 0,
    })
    assert audit._retrieval_id_from_output(nested_header) == retrieval_id

    nested_json = json.dumps({
        "content": [{
            "output": json.dumps({
                "mode": "read_only_rag_context",
                "retrieval_id": retrieval_id,
            })
        }]
    })
    assert audit._retrieval_id_from_output(nested_json) == retrieval_id

    named_result = json.dumps({
        "kb": {
            "output": (
                "Script completed\n"
                f'KB_RAG_CONTEXT retrieval_id="{retrieval_id}" query="update ccs" hits=1\n'
            )
        }
    })
    assert audit._retrieval_id_from_output(named_result) == retrieval_id

    real_exec_envelope = (
        'Script completed with cell ID 74\n'
        + json.dumps({
            "chunk_id": "chunk-1",
            "exit_code": 0,
            "original_token_count": 42,
            "output": json.dumps({
                "mode": "read_only_rag_context",
                "retrieval_id": retrieval_id,
            }),
            "wall_time_seconds": 0.1,
        })
    )
    assert audit._retrieval_id_from_output(real_exec_envelope) == retrieval_id


def test_audit_same_rollout_pairing_prefers_id_and_reserves_scout_capacity() -> None:
    rag = {
        "sequence": 0,
        "turn_id": "turn-1",
        "query": "local query",
        "retrieval_id": "local-id",
    }
    exact_closeout = {
        "sequence": 1,
        "turn_id": "turn-2",
        "queries": ["different query"],
        "linked_retrieval_ids": ["local-id"],
        "capacity": 1,
    }
    matched, unmatched = audit._match_rag_closeouts([rag], [exact_closeout])
    assert matched == 1
    assert unmatched == []

    scout_only_closeout = {
        "sequence": 1,
        "turn_id": "turn-1",
        "queries": [],
        "linked_retrieval_ids": ["scout-id"],
        "capacity": 1,
    }
    matched, unmatched = audit._match_rag_closeouts([rag], [scout_only_closeout])
    assert matched == 0
    assert unmatched == [rag]


def test_audit_reconciliation_rejects_forbidden_scout_and_reports_dangling_link() -> None:
    sessions = [
        {
            "session_id": "parent-1",
            "is_main": True,
            "is_subagent": False,
            "closeout_linked_retrieval_ids": ["forbidden-id", "missing-id"],
            "retrieval_events": [],
            "rag_or_search": 0,
        },
        {
            "session_id": "scout-1",
            "is_main": False,
            "is_subagent": True,
            "subagent_parent_thread_id": "parent-1",
            "forbid_kb": True,
            "retrieval_events": [
                {"retrieval_id": "forbidden-id", "query": "q"}
            ],
            "rag_calls_without_closeout": 1,
            "retrieval_id_missing_count": 0,
        },
    ]
    stats = audit._reconcile_parent_scout_sessions(sessions)
    assert sessions[1]["parent_closeout_linked_retrieval_ids"] == []
    assert sessions[1]["invalid_scout_retrieval_ids"] == ["forbidden-id"]
    assert stats["invalid_scout_retrieval_id_count"] == 1
    assert stats["dangling_parent_linked_retrieval_ids_sample"] == ["missing-id"]


def test_context_instructions_are_not_treated_as_user_prompt() -> None:
    session_id = "88888888-8888-7888-8888-888888888888"
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        day_dir = temp / "sessions" / f"{date.today():%Y}" / f"{date.today():%m}" / f"{date.today():%d}"
        _write_jsonl(
            day_dir / f"rollout-{date.today().isoformat()}T10-00-00-{session_id}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "thread_source": "user",
                        "source": "cli",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "# AGENTS.md instructions for <local-home>/study\n"
                                "不要运行 kb_rag_context.py，除非收到明确检索任务。",
                            },
                            {
                                "type": "input_text",
                                "text": "<environment_context>\n"
                                "<cwd><local-home>/study</cwd>\n</environment_context>",
                            },
                            {
                                "type": "input_text",
                                "text": "继续上次的简历修改，需要上次视频四点建议的历史记录。",
                            },
                        ],
                    },
                },
            ],
        )
        parsed = audit._parse_session(day_dir / f"rollout-{date.today().isoformat()}T10-00-00-{session_id}.jsonl")

    assert parsed["forbid_kb"] is False
    assert parsed["runtime_audit"] is False
    assert parsed["explicit_kb"] is True
    assert parsed["memory_needed"] is True
    assert "不要运行 kb_rag_context.py" not in parsed["user_excerpt"]
    assert "继续上次的简历修改" in parsed["user_excerpt"]


def test_subagent_notification_is_not_treated_as_user_prompt() -> None:
    session_id = "99999999-9999-7999-8999-999999999999"
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        day_dir = temp / "sessions" / f"{date.today():%Y}" / f"{date.today():%m}" / f"{date.today():%d}"
        _write_jsonl(
            day_dir / f"rollout-{date.today().isoformat()}T10-00-00-{session_id}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "thread_source": "user",
                        "source": "cli",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "<subagent_notification>\n"
                                    '{"status":{"completed":"这里包含 personal-kb 优化和历史记录"}}\n'
                                    "</subagent_notification>"
                                ),
                            },
                            {
                                "type": "input_text",
                                "text": "检查当前项目代码。",
                            },
                        ],
                    },
                },
            ],
        )
        parsed = audit._parse_session(day_dir / f"rollout-{date.today().isoformat()}T10-00-00-{session_id}.jsonl")

    assert parsed["forbid_kb"] is False
    assert parsed["runtime_audit"] is False
    assert parsed["explicit_kb"] is False
    assert parsed["memory_needed"] is False
    assert "personal-kb 优化" not in parsed["user_excerpt"]
    assert "检查当前项目代码" in parsed["user_excerpt"]


def test_runtime_audit_session_is_not_counted_as_missed_rag() -> None:
    session_id = "aaaaaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
    audit_day = date.today()
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        day_dir = temp / "sessions" / f"{audit_day:%Y}" / f"{audit_day:%m}" / f"{audit_day:%d}"
        _write_jsonl(
            day_dir / f"rollout-{audit_day.isoformat()}T10-00-00-{session_id}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "thread_source": "user",
                        "source": "cli",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "kbskill 帮我看看 personal-kb 的优化效果。"}
                        ],
                    },
                },
            ],
        )
        closeout = temp / "closeout.jsonl"
        closeout.write_text("", encoding="utf-8")
        report = build_report(
            argparse.Namespace(
                sessions_root=str(temp / "sessions"),
                date=[audit_day.isoformat()],
                last_days=0,
                closeout=str(closeout),
                examples=10,
            )
        )

    assert report["main_missed_rag_sessions"] == 0
    assert report["main_expected_rag_sessions"] == 0


def test_terminal_aborted_session_is_not_a_retrieval_candidate() -> None:
    session_id = "abababab-abab-7bab-8bab-abababababab"
    audit_day = date.today()
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        day_dir = temp / "sessions" / f"{audit_day:%Y}" / f"{audit_day:%m}" / f"{audit_day:%d}"
        path = day_dir / f"rollout-{audit_day.isoformat()}T10-00-00-{session_id}.jsonl"
        _write_jsonl(path, [
            {
                "type": "session_meta",
                "payload": {"id": session_id, "thread_source": "user", "source": "cli"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "继续上次的简历修改"},
            },
            {"type": "event_msg", "payload": {"type": "turn_aborted"}},
        ])
        parsed = audit._parse_session(path)
        closeout = temp / "closeout.jsonl"
        closeout.write_text("", encoding="utf-8")
        report = build_report(argparse.Namespace(
            sessions_root=str(temp / "sessions"),
            date=[audit_day.isoformat()],
            last_days=0,
            closeout=str(closeout),
            examples=10,
        ))

    assert parsed["terminal_status"] == "turn_aborted"
    assert parsed["terminal_aborted"] is True
    assert report["terminal_aborted_sessions"] == 1
    assert report["main_expected_rag_sessions"] == 0
    assert report["main_missed_rag_sessions"] == 0


def _session_rows(
    session_id: str,
    *,
    source: str,
    rag_query: str,
    closeout_query: str,
    parent_meta: dict | None = None,
) -> list[dict]:
    turn_id = f"turn-{session_id[:8]}"
    rows = [
        {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "thread_source": source,
                "source": (
                    {"subagent": {"thread_spawn": {"parent_thread_id": "parent-id"}}}
                    if source == "subagent"
                    else "cli"
                ),
                "parent_thread_id": "parent-id" if source == "subagent" else "",
            },
        },
    ]
    if parent_meta:
        rows.append({"type": "session_meta", "payload": parent_meta})
    rows.extend(
        [
            {"type": "turn_context", "payload": {"turn_id": turn_id}},
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "检查历史简历资料"},
            },
            _tool_call(
                "rag",
                f"python3 /tmp/kb_rag_context.py {json.dumps(rag_query, ensure_ascii=False)}",
                turn_id,
            ),
            _tool_output("rag"),
            _tool_call(
                "closeout",
                "python3 /tmp/kb_closeout.py "
                f"--query {json.dumps(closeout_query, ensure_ascii=False)} "
                "--hit-count 1 --rag-calls 1 --reason recorded",
                turn_id,
            ),
            _tool_output("closeout"),
        ]
    )
    return rows


def test_session_audit_excludes_test_closeouts_by_default() -> None:
    audit_day = date.today()
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        sessions_root = root / "sessions"
        sessions_root.mkdir()
        closeout = root / "closeout.jsonl"
        base = {
            "event": "kb_closeout",
            "ts": f"{audit_day.isoformat()}T10:00:00+00:00",
            "rag_calls": 0,
            "hit_count": 0,
            "used_entry_ids": [],
            "session_brief_hit": False,
            "session_brief_help": False,
        }
        _write_jsonl(
            closeout,
            [
                {**base, "closeout_id": "production", "runtime": {"source": "production", "test_run_id": ""}},
                {**base, "closeout_id": "test", "runtime": {"source": "test", "test_run_id": "run-1"}},
            ],
        )
        report = build_report(
            argparse.Namespace(
                sessions_root=str(sessions_root),
                date=[audit_day.isoformat()],
                last_days=0,
                closeout=str(closeout),
                examples=5,
                include_test=False,
            )
        )
    assert report["closeout_issues"]["rows"] == 1
    assert report["excluded_test_rows"] == 1


def test_human_labels_separate_confirmed_miss_from_auditor_false_positive() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        labels_path = Path(temp_dir) / "labels.json"
        labels_path.write_text(json.dumps({
            "labels": [
                {
                    "session_id": "confirmed-session",
                    "label": "confirmed_missed_retrieval",
                    "reason": "The request explicitly depended on prior project material.",
                },
                {
                    "session_id": "false-positive-session",
                    "label": "auditor_false_positive",
                    "reason": "A pasted code-smell configuration contained history-like words.",
                },
            ],
        }), encoding="utf-8")
        sessions = [
            {"session_id": "confirmed-session"},
            {"session_id": "false-positive-session"},
            {"session_id": "unreviewed-session"},
        ]
        summary = audit._human_review_summary(sessions, sessions, labels_path)

    assert summary["status"] == "loaded"
    assert summary["auditor_candidate_total"] == 3
    assert summary["confirmed_missed_retrieval_total"] == 1
    assert summary["auditor_false_positive_total"] == 1
    assert summary["unreviewed_auditor_candidate_total"] == 1


def test_session_audit_redacts_opaque_tokens_from_examples() -> None:
    session_id = "99999999-9999-7999-8999-999999999999"
    token = "github_pat_" + "B" * 48
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / f"rollout-2026-08-17T10-00-00-{session_id}.jsonl"
        _write_jsonl(path, [
            {
                "type": "session_meta",
                "payload": {"id": session_id, "thread_source": "user", "source": "cli"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": f"历史配置 {token}中文"},
            },
        ])
        parsed = audit._parse_session(path)

    assert token not in parsed["user_excerpt"]
    assert "[REDACTED]" in parsed["user_excerpt"]
    assert parsed["user_excerpt"].endswith("[REDACTED]中文")


def test_parent_subagent_guard_is_not_a_parent_kb_forbid() -> None:
    session_id = "aaaaaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
    guard = (
        "Do not run personal-kb scripts. Use only KB hints provided by the parent; "
        "the parent owns retrieval routing and final hint selection, adoption, heating, writes, and closeout."
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / f"rollout-2026-08-17T10-00-00-{session_id}.jsonl"
        _write_jsonl(path, [
            {
                "type": "session_meta",
                "payload": {"id": session_id, "thread_source": "user", "source": "cli"},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": f"维护 Personal KB，派发普通子 Agent 时写入：{guard}",
                },
            },
            _tool_call("update", "python3 /tmp/kb_update.py update entry-1 --refresh-evidence", "turn-1"),
            _tool_output("update"),
        ])
        parsed = audit._parse_session(path)

    assert parsed["forbid_kb_scope"] == "subagent_only"
    assert parsed["forbid_kb"] is False


def main() -> int:
    test_public_wrapper_commands_are_detected()
    test_chained_wrapper_retrievals_keep_distinct_results()
    audit_day = date.today()
    old_day = audit_day - timedelta(days=5)
    root_id = "11111111-1111-7111-8111-111111111111"
    subagent_id = "22222222-2222-7222-8222-222222222222"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        sessions_root = temp / "sessions"
        day_dir = sessions_root / f"{audit_day:%Y}" / f"{audit_day:%m}" / f"{audit_day:%d}"
        _write_jsonl(
            day_dir / f"rollout-{audit_day.isoformat()}T10-00-00-{root_id}.jsonl",
            _session_rows(root_id, source="user", rag_query="same query", closeout_query="same query"),
        )
        _write_jsonl(
            day_dir / f"rollout-{audit_day.isoformat()}T10-01-00-{subagent_id}.jsonl",
            _session_rows(
                subagent_id,
                source="subagent",
                rag_query="unclosed query",
                closeout_query="different query",
                parent_meta={"id": root_id, "thread_source": "user", "source": "cli"},
            ),
        )
        current_only_id = "33333333-3333-7333-8333-333333333333"
        _write_jsonl(
            day_dir / f"rollout-{audit_day.isoformat()}T10-02-00-{current_only_id}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": current_only_id,
                        "thread_source": "user",
                        "source": "cli",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "重构当前前端，可能需要 skill，直接以当前代码和浏览器为准",
                    },
                },
            ],
        )
        runtime_audit_id = "44444444-4444-7444-8444-444444444444"
        _write_jsonl(
            day_dir / f"rollout-{audit_day.isoformat()}T10-03-00-{runtime_audit_id}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": runtime_audit_id,
                        "thread_source": "user",
                        "source": "cli",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "审计 Personal KB 这两天的运行效果，直接读取 session/runtime 日志",
                    },
                },
                _tool_call(
                    "rag-runtime",
                    "python3 /tmp/kb_rag_context.py 'runtime audit helper query'",
                    "turn-runtime-audit",
                ),
                _tool_output("rag-runtime"),
            ],
        )
        current_incident_id = "55555555-5555-7555-8555-555555555555"
        _write_jsonl(
            day_dir / f"rollout-{audit_day.isoformat()}T10-04-00-{current_incident_id}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": current_incident_id,
                        "thread_source": "user",
                        "source": "cli",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "排查当前线上 500，现有异常栈和 traceId 已足够定位",
                    },
                },
            ],
        )
        used_no_closeout_id = "77777777-7777-7777-8777-777777777777"
        _write_jsonl(
            day_dir / f"rollout-{audit_day.isoformat()}T10-05-00-{used_no_closeout_id}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": used_no_closeout_id,
                        "thread_source": "user",
                        "source": "cli",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "继续上次的简历修改，需要历史简历资料",
                    },
                },
                _tool_call(
                    "rag-used-no-closeout",
                    "python3 /tmp/kb_rag_context.py 'last resume revision direction'",
                    "turn-used-no-closeout",
                ),
                _tool_output("rag-used-no-closeout"),
            ],
        )
        help_only_id = "66666666-6666-7666-8666-666666666666"
        _write_jsonl(
            day_dir / f"rollout-{audit_day.isoformat()}T10-06-00-{help_only_id}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": help_only_id,
                        "thread_source": "user",
                        "source": "cli",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "查看当前脚本帮助"},
                },
                _tool_call("help", "python3 /tmp/kb_search.py --help", "turn-help"),
                _tool_output("help"),
            ],
        )

        closeout_path = temp / "closeout.jsonl"
        current_ts = f"{audit_day.isoformat()}T12:00:00+08:00"
        _write_jsonl(
            closeout_path,
            [
                {
                    "ts": f"{old_day.isoformat()}T12:00:00+08:00",
                    "hit_count": 1,
                    "rag_calls": 1,
                    "skipped_reason": "",
                    "adoption_effects": {"decide": ["old"]},
                    "heated_entry_ids": [],
                },
                {
                    "ts": current_ts,
                    "hit_count": 1,
                    "rag_calls": 1,
                    "skipped_reason": "hits were irrelevant",
                    "linked_retrieval_ids": ["retrieval-1"],
                    "session_brief_hit": False,
                    "session_brief_help": False,
                },
                {
                    "ts": current_ts,
                    "hit_count": 1,
                    "rag_calls": 1,
                    "skipped_reason": "",
                    "linked_retrieval_ids": [],
                    "session_brief_hit": False,
                    "session_brief_help": False,
                },
                {
                    "ts": current_ts,
                    "hit_count": 1,
                    "rag_calls": 1,
                    "used_entry_ids": ["locate-id"],
                    "adoption_effects": {"locate": ["locate-id"]},
                    "heated_entry_ids": [],
                    "skipped_reason": "",
                    "linked_retrieval_ids": ["retrieval-2"],
                    "session_brief_hit": True,
                    "session_brief_help": False,
                },
                {
                    "ts": current_ts,
                    "hit_count": 1,
                    "rag_calls": 1,
                    "used_entry_ids": ["decide-id"],
                    "adoption_effects": {"decide": ["decide-id"]},
                    "heated_entry_ids": [],
                    "skipped_reason": "",
                    "linked_retrieval_ids": [],
                    "session_brief_hit": False,
                    "session_brief_help": False,
                },
                {
                    "ts": current_ts,
                    "hit_count": 1,
                    "rag_calls": 1,
                    "used_entry_ids": ["fix-id"],
                    "adoption_effects": {"fix": ["fix-id"]},
                    "heated_entry_ids": ["fix-id"],
                    "skipped_reason": "",
                    "linked_retrieval_ids": ["retrieval-3"],
                    "session_brief_hit": True,
                    "session_brief_help": True,
                },
            ],
        )

        report = build_report(
            argparse.Namespace(
                sessions_root=str(sessions_root),
                date=[],
                last_days=2,
                closeout=str(closeout_path),
                examples=10,
            )
        )

    assert report["session_files"] == 7
    assert report["sessions_by_source"] == {"user": 6, "subagent": 1}
    assert report["main_sessions"] == 6
    assert report["subagent_sessions"] == 1
    assert report["main_missed_rag_sessions"] == 0
    assert report["main_expected_rag_sessions"] == 2
    assert report["main_expected_and_used_rag_sessions"] == 2
    assert report["main_used_sessions"] == 2
    assert report["main_used_without_closeout_sessions"] == 1
    assert report["main_with_closeout_sessions"] == 1
    assert report["rag_without_closeout_sessions"] == 2
    assert report["call_totals"].get("kb_search", 0) == 0
    unmatched = report["examples"]["rag_without_closeout"]
    assert unmatched[0]["session_id"] == subagent_id
    assert unmatched[0]["rag_queries_without_closeout"] == ["unclosed query"]
    used_without_closeout = report["examples"]["main_used_without_closeout"]
    assert used_without_closeout[0]["session_id"] == used_no_closeout_id

    issues = report["closeout_issues"]
    assert issues["rows"] == 5
    assert issues["hit_with_no_action"] == 1
    assert issues["used_without_heat"] == 1
    assert issues["linked_retrieval_id_missing"] == 2
    assert issues["session_brief_telemetry_missing"] == 0
    assert issues["closeout_integrity_missing"] == 2
    test_parent_scout_retrieval_link_avoids_false_missing_reports()
    test_audit_parser_uses_canonical_header_id()
    test_audit_parser_unwraps_nested_exec_output()
    test_audit_same_rollout_pairing_prefers_id_and_reserves_scout_capacity()
    test_audit_reconciliation_rejects_forbidden_scout_and_reports_dangling_link()
    test_context_instructions_are_not_treated_as_user_prompt()
    test_terminal_aborted_session_is_not_a_retrieval_candidate()
    test_session_audit_excludes_test_closeouts_by_default()
    test_human_labels_separate_confirmed_miss_from_auditor_false_positive()
    test_session_audit_redacts_opaque_tokens_from_examples()
    test_parent_subagent_guard_is_not_a_parent_kb_forbid()
    print("kb_audit_codex_sessions tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_TEST_GUARD.run(main) if _TEST_GUARD else main())
