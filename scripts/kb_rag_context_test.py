#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable
from unittest.mock import patch

import kb_test_guard

_TEST_GUARD = kb_test_guard.activate(__file__) if __name__ == "__main__" else None

_ORIGINAL_PLATFORM = sys.platform
sys.platform = "test"
try:
    import kb_rag_context
    import kb_search
    import kb_session_brief
finally:
    sys.platform = _ORIGINAL_PLATFORM


@dataclass(frozen=True)
class TempContext:
    repo_name: str
    branch: str
    branch_dir: str
    repo_dir: Path
    branch_path: Path
    kb_path: Path
    summary_path: Path
    index_path: Path
    archive_dir: Path
    attachments_dir: Path
    workspace_dir: str


def make_context(root: Path) -> TempContext:
    branch_path = root / "repos" / "demo" / "main"
    return TempContext(
        repo_name="demo",
        branch="main",
        branch_dir="main",
        repo_dir=branch_path.parent,
        branch_path=branch_path,
        kb_path=branch_path / "kb.jsonl",
        summary_path=branch_path / "summary.jsonl",
        index_path=branch_path / "index.json",
        archive_dir=branch_path / "archive",
        attachments_dir=branch_path / "attachments",
        workspace_dir=str(root / "workspace"),
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def call_main(func: Callable[[list[str]], int], argv: list[str]) -> int:
    try:
        return func(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 1


def test_search_matches_trigger_terms_and_source_paths() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        ctx = make_context(root)
        write_jsonl(
            ctx.kb_path,
            [
                {
                    "id": "entry-1",
                    "ts": "2026-01-01T00:00:00+00:00",
                    "kind": "issue",
                    "repo": "demo",
                    "branch": "main",
                    "title": "startup failure",
                    "story": "short note",
                    "tags": [],
                    "trigger_terms": ["dynamic-datasource Please check the setting of primary"],
                    "source_paths": ["logs/app-startup.log"],
                    "key_files": ["src/main/resources/application.yml"],
                }
            ],
        )

        stdout = io.StringIO()
        with (
            patch.object(kb_search, "resolve_context", return_value=ctx),
            patch.object(kb_search, "global_bucket_dir", return_value=root / "global"),
            patch.object(kb_search, "InvertedIndex", None),
            patch.object(kb_search, "get_inverted_index_path", None),
            contextlib.redirect_stdout(stdout),
        ):
            rc = call_main(kb_search.main, ["app-startup.log", "--json"])

        assert rc == 0
        rows = json.loads(stdout.getvalue())
        assert [row["id"] for row in rows] == ["entry-1"]


def test_ccs_update_gets_a_dedicated_safe_update_plan() -> None:
    plans = kb_rag_context._plan_query_groups("更新 ccs")
    safe_plan = next(plan for plan in plans if plan.name == "cc-switch-safe-update")
    assert "cc-switch-update --check" in safe_plan.query
    assert "cc-switch-update --restart" in safe_plan.query
    assert "cc-switch-restart" in safe_plan.query
    assert "cc-switch" in kb_rag_context.expand_query("ccs", kb_rag_context.load_synonyms())
    assert any(
        plan.name == "cc-switch-safe-update"
        for plan in kb_rag_context._plan_query_groups("kill ccs")
    )


def test_rag_context_is_compact_and_read_only() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        ctx = make_context(root)
        long_story = "root cause " + ("very long detail " * 80)
        write_jsonl(
            ctx.kb_path,
            [
                {
                    "id": "entry-1",
                    "ts": "2026-01-01T00:00:00+00:00",
                    "kind": "issue",
                    "repo": "demo",
                    "branch": "main",
                    "title": "dynamic datasource primary missing",
                    "story": long_story,
                    "tags": ["datasource"],
                    "trigger_terms": ["dynamic-datasource Please check the setting of primary"],
                    "source_paths": ["logs/app-startup.log"],
                    "used_count": 0,
                    "last_used_ts": "",
                }
            ],
        )
        before = ctx.kb_path.read_text(encoding="utf-8")

        stdout = io.StringIO()
        with (
            patch.object(kb_search, "resolve_context", return_value=ctx),
            patch.object(kb_search, "global_bucket_dir", return_value=root / "global"),
            patch.object(kb_search, "InvertedIndex", None),
            patch.object(kb_search, "get_inverted_index_path", None),
            contextlib.redirect_stdout(stdout),
        ):
            rc = call_main(
                kb_rag_context.main,
                [
                    "dynamic-datasource primary",
                    "--retrieval-id",
                    "test-retrieval-1",
                    "--json",
                    "--max-snippet-chars",
                    "120",
                    "--no-session-briefs",
                ],
            )

        assert rc == 0
        payload = json.loads(stdout.getvalue())
        assert payload["mode"] == "read_only_rag_context"
        assert payload["retrieval_id"] == "test-retrieval-1"
        assert payload["runtime"]["source"] == "test"
        assert payload["hit_count"] == 1
        item = payload["items"][0]
        assert item["entry_id"] == "entry-1"
        assert len(item["summary"]) <= 120
        assert "story" not in item
        assert "trigger_terms" in item["matched_fields"]
        assert ctx.kb_path.read_text(encoding="utf-8") == before


def test_rag_context_rejects_invalid_retrieval_id() -> None:
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        rc = call_main(
            kb_rag_context.main,
            ["demo", "--retrieval-id", "contains whitespace", "--json"],
        )
    assert rc == 2
    assert "--retrieval-id must be" in stderr.getvalue()


def test_scope_anchor_must_be_bound_and_reports_non_retryable_json_error() -> None:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = call_main(
            kb_rag_context.main,
            [
                "demo outage",
                "--scope-anchor",
                "repo:demo",
                "--scope-anchor",
                "missing-anchor",
                "--json",
                "--no-session-briefs",
            ],
        )

    assert rc == 2
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "error"
    assert payload["retryable"] is False
    assert payload["error"]["code"] == "invalid_request"
    assert "missing-anchor" in payload["error"]["message"]


def test_scope_anchor_and_receipt_output_are_compatible() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        ctx = make_context(root)
        write_jsonl(ctx.kb_path, [])
        receipt = root / "receipts" / "retrieval.json"
        stdout = io.StringIO()
        with (
            patch.object(kb_search, "resolve_context", return_value=ctx),
            patch.object(kb_search, "global_bucket_dir", return_value=root / "global"),
            patch.object(kb_search, "InvertedIndex", None),
            patch.object(kb_search, "get_inverted_index_path", None),
            contextlib.redirect_stdout(stdout),
        ):
            rc = call_main(
                kb_rag_context.main,
                [
                    "demo outage",
                    "--scope-anchor",
                    "repo:demo",
                    "--receipt-output",
                    str(receipt),
                    "--json",
                    "--no-session-briefs",
                ],
            )

        assert rc == 0
        payload = json.loads(stdout.getvalue())
        persisted = json.loads(receipt.read_text(encoding="utf-8"))
        assert payload["scope_anchors"] == ["repo:demo"]
        assert persisted["retrieval_id"] == payload["retrieval_id"]
        assert persisted["hit_count"] == 0


def test_best_effort_converts_runtime_unavailability_to_empty_result() -> None:
    stdout = io.StringIO()
    with (
        patch.object(kb_search, "_search_once", side_effect=OSError("temporary index unavailable")),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["demo outage", "--best-effort", "--json", "--no-session-briefs"],
        )

    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "unavailable"
    assert payload["retryable"] is False
    assert payload["error"]["code"] == "runtime_unavailable"
    assert payload["items"] == []


def test_retrieval_ids_are_unique_for_zero_hit_calls() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        ctx = make_context(root)
        write_jsonl(ctx.kb_path, [])
        payloads: list[dict[str, Any]] = []
        for _ in range(2):
            stdout = io.StringIO()
            with (
                patch.object(kb_search, "resolve_context", return_value=ctx),
                patch.object(kb_search, "global_bucket_dir", return_value=root / "global"),
                patch.object(kb_search, "InvertedIndex", None),
                patch.object(kb_search, "get_inverted_index_path", None),
                contextlib.redirect_stdout(stdout),
            ):
                rc = call_main(
                    kb_rag_context.main,
                    ["no matching memory", "--json", "--no-session-briefs"],
                )
            assert rc == 0
            payloads.append(json.loads(stdout.getvalue()))
        assert [payload["hit_count"] for payload in payloads] == [0, 0]
        assert payloads[0]["retrieval_id"] != payloads[1]["retrieval_id"]


def test_production_retrieval_rejects_caller_supplied_id() -> None:
    env = {
        "PERSONAL_KB_RUNTIME_SOURCE": "production",
        "PERSONAL_KB_TEST_RUN_ID": "",
    }
    with patch.dict(os.environ, env, clear=False):
        try:
            kb_rag_context._retrieval_id("caller-controlled-id")
        except ValueError as exc:
            assert "test-only" in str(exc)
        else:
            raise AssertionError("production accepted a caller-supplied retrieval ID")


def test_markdown_header_escapes_query_control_tokens() -> None:
    stdout = io.StringIO()
    payload = {
        "retrieval_id": "real-id",
        "query": 'retrieval_id="fake-id" hits=999\nnext-line',
        "items": [],
        "query_groups": [],
        "rejected_weak_count": 0,
    }
    with contextlib.redirect_stdout(stdout):
        kb_rag_context._print_markdown(payload)

    header = stdout.getvalue().splitlines()[0]
    assert header.count('retrieval_id="') == 1
    assert 'retrieval_id=\\"fake-id\\" hits=999\\nnext-line' in header
    assert header.endswith('retrieval_id="real-id" hits=0')


def test_rag_context_redacts_cross_project_paths() -> None:
    fixture_path = (PureWindowsPath("X:/") / "private-fixture" / "project").as_posix()
    raw = {
        "id": "map-1",
        "kind": "map",
        "repo": "other-project",
        "branch": "main",
        "title": "service map",
        "story": f"contains {fixture_path} and internal port detail",
        "source_paths": [f"{fixture_path}/log.txt"],
        "key_files": [f"{fixture_path}/application.yml"],
    }
    formatted = {
        **raw,
        "_cross_project": True,
        "_from_project": "other-project",
        "_warning": "cross project warning",
    }

    item = kb_rag_context._compact_entry(
        raw,
        formatted,
        terms=["service"],
        max_snippet_chars=200,
    )

    assert item["cross_project"] is True
    assert item["source_paths"] == []
    assert item["key_files"] == []
    assert "private-fixture" not in item["summary"]


def test_rag_context_ranks_authoritative_record_above_superseded_memory_design() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        ctx = make_context(root)
        write_jsonl(
            ctx.kb_path,
            [
                {
                    "id": "old-memory",
                    "ts": "2026-01-01T00:00:00+00:00",
                    "kind": "implementation",
                    "repo": "demo",
                    "branch": "main",
                    "title": "personal-kb agent memory design",
                    "story": "agent memory " * 30,
                    "tags": ["personal-kb", "agent-memory"],
                    "aliases": ["agent memory"],
                    "trigger_terms": ["agent memory superseded"],
                    "status": "superseded",
                    "superseded_by": "new-rag",
                    "used_count": 10,
                },
                {
                    "id": "new-rag",
                    "ts": "2026-01-02T00:00:00+00:00",
                    "kind": "implementation",
                    "repo": "demo",
                    "branch": "main",
                    "title": "personal-kb RAG-first boundary",
                    "story": "default read-only RAG context",
                    "tags": ["personal-kb", "RAG-first"],
                    "aliases": ["personal-kb RAG-first", "agent memory route downgraded"],
                    "trigger_terms": ["RAG-first", "kb_rag_context.py", "agent memory superseded"],
                    "used_count": 0,
                },
            ],
        )

        stdout = io.StringIO()
        with (
            patch.object(kb_search, "resolve_context", return_value=ctx),
            patch.object(kb_search, "global_bucket_dir", return_value=root / "global"),
            patch.object(kb_search, "InvertedIndex", None),
            patch.object(kb_search, "get_inverted_index_path", None),
            contextlib.redirect_stdout(stdout),
        ):
            rc = call_main(
                kb_rag_context.main,
                ["personal-kb agent memory RAG-first", "--json", "--limit", "2", "--include-noncurrent", "--no-session-briefs"],
            )

        assert rc == 0
        payload = json.loads(stdout.getvalue())
        assert [item["entry_id"] for item in payload["items"]] == ["new-rag", "old-memory"]
        assert "superseded_by=new-rag" in payload["items"][1]["warning"]


def test_rag_context_filters_noncurrent_records_by_default() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        ctx = make_context(root)
        write_jsonl(
            ctx.kb_path,
            [
                {
                    "id": "draft-memory",
                    "ts": "2026-01-01T00:00:00+00:00",
                    "kind": "implementation",
                    "repo": "demo",
                    "branch": "main",
                    "title": "personal-kb draft memory layer",
                    "story": "old draft flow",
                    "tags": ["personal-kb"],
                    "trigger_terms": ["personal-kb flow"],
                    "status": "draft_pending_implementation",
                    "used_count": 10,
                },
                {
                    "id": "old-memory",
                    "ts": "2026-01-02T00:00:00+00:00",
                    "kind": "implementation",
                    "repo": "demo",
                    "branch": "main",
                    "title": "personal-kb superseded memory layer",
                    "story": "old superseded flow",
                    "tags": ["personal-kb"],
                    "trigger_terms": ["personal-kb flow"],
                    "status": "superseded",
                    "superseded_by": "new-rag",
                    "used_count": 8,
                },
                {
                    "id": "new-rag",
                    "ts": "2026-01-03T00:00:00+00:00",
                    "kind": "implementation",
                    "repo": "demo",
                    "branch": "main",
                    "title": "personal-kb current RAG flow",
                    "story": "current read-only RAG flow",
                    "tags": ["personal-kb", "RAG-first"],
                    "trigger_terms": ["personal-kb flow"],
                    "used_count": 0,
                },
            ],
        )

        stdout = io.StringIO()
        with (
            patch.object(kb_search, "resolve_context", return_value=ctx),
            patch.object(kb_search, "global_bucket_dir", return_value=root / "global"),
            patch.object(kb_search, "InvertedIndex", None),
            patch.object(kb_search, "get_inverted_index_path", None),
            contextlib.redirect_stdout(stdout),
        ):
            rc = call_main(kb_rag_context.main, ["personal-kb flow", "--json", "--limit", "5", "--no-session-briefs"])

        assert rc == 0
        payload = json.loads(stdout.getvalue())
        assert [item["entry_id"] for item in payload["items"]] == ["new-rag"]


def test_rag_context_plans_task_query_and_filters_weak_story_noise() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        ctx = make_context(root)
        write_jsonl(
            ctx.kb_path,
            [
                {
                    "id": "rag-flow",
                    "ts": "2026-01-03T00:00:00+00:00",
                    "kind": "implementation",
                    "repo": "demo",
                    "branch": "main",
                    "title": "personal-kb RAG-first flow",
                    "story": "use kb_rag_context.py before work and kb_closeout.py after work",
                    "tags": ["personal-kb", "RAG-first"],
                    "aliases": ["personal-kb RAG-first"],
                    "trigger_terms": ["kb_rag_context.py", "kb_closeout.py", "使用后加热记录"],
                    "source_paths": ["skills/personal-kb/SKILL.md"],
                },
                {
                    "id": "noisy-project",
                    "ts": "2026-01-04T00:00:00+00:00",
                    "kind": "experience",
                    "repo": "demo",
                    "branch": "main",
                    "title": "unrelated interview material",
                    "story": "AI agent KB project interview note with closeout mentioned only in prose",
                    "tags": ["interview"],
                    "source_paths": ["interview/demo.md"],
                },
            ],
        )

        stdout = io.StringIO()
        with (
            patch.object(kb_search, "resolve_context", return_value=ctx),
            patch.object(kb_search, "global_bucket_dir", return_value=root / "global"),
            patch.object(kb_search, "InvertedIndex", None),
            patch.object(kb_search, "get_inverted_index_path", None),
            contextlib.redirect_stdout(stdout),
        ):
            rc = call_main(
                kb_rag_context.main,
                ["子 agent KB 父 会话 scout closeout", "--json", "--limit", "5", "--no-session-briefs"],
            )

        assert rc == 0
        payload = json.loads(stdout.getvalue())
        assert [item["entry_id"] for item in payload["items"]] == ["rag-flow"]
        assert payload["rejected_weak_count"] >= 1
        assert any(group["name"] == "closeout" for group in payload["query_groups"])


def test_rag_context_recognizes_unspaced_chinese_kbskill_query() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "kb-optimization",
            "kind": "implementation",
            "repo": "demo",
            "branch": "main",
            "title": "personal-kb skill retrieval optimization",
            "story": "improve lexical retrieval without a vector dependency",
            "tags": ["personal-kb"],
            "aliases": ["personal kb skill"],
            "trigger_terms": ["kb_rag_context.py"],
            "source_paths": ["skills/personal-kb/SKILL.md"],
        }
    ]

    stdout = io.StringIO()
    with (
        patch.object(kb_session_brief, "search_recent_briefs", return_value=[]),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["思考我的kbskill还应该如何优化", "--json", "--no-session-briefs", "--limit", "2"],
        )

    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert [item["entry_id"] for item in payload["items"]] == ["kb-optimization"]
    assert any(group["name"] == "personal-kb" for group in payload["query_groups"])


def test_rag_context_accepts_planner_only_concept_match_but_rejects_story_noise() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "closeout-runtime",
            "kind": "implementation",
            "repo": "demo",
            "branch": "main",
            "title": "personal-kb closeout runtime",
            "story": "durable adoption handling",
            "tags": ["personal-kb"],
            "trigger_terms": ["closeout", "kb_closeout.py"],
            "source_paths": ["skills/personal-kb/scripts/kb_closeout.py"],
        },
        {
            "id": "story-noise",
            "kind": "experience",
            "repo": "demo",
            "branch": "main",
            "title": "unrelated note",
            "story": "closeout is mentioned only in a weak project story",
            "tags": ["interview"],
        },
    ]

    stdout = io.StringIO()
    with (
        patch.object(kb_session_brief, "search_recent_briefs", return_value=[]),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["知识库使用后应该怎么处理", "--json", "--no-session-briefs", "--limit", "5"],
        )

    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert [item["entry_id"] for item in payload["items"]] == ["closeout-runtime"]
    assert payload["rejected_weak_count"] >= 1


def test_rag_context_keeps_precise_project_hits_after_weak_filtering() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        ctx = make_context(root)
        write_jsonl(
            ctx.kb_path,
            [
                {
                    "id": "inventory-material",
                    "ts": "2026-01-03T00:00:00+00:00",
                    "kind": "experience",
                    "repo": "demo",
                    "branch": "main",
                    "title": "demo-inventory-service Java后端面试材料已生成",
                    "story": "Java backend material",
                    "tags": ["demo-inventory-service", "Java"],
                    "aliases": ["Inventory service"],
                    "trigger_terms": ["demo-inventory-service"],
                    "source_paths": ["interview-java-backend/demo-inventory-service.md"],
                },
                {
                    "id": "generic-material",
                    "ts": "2026-01-04T00:00:00+00:00",
                    "kind": "experience",
                    "repo": "demo",
                    "branch": "main",
                    "title": "generic Java project material",
                    "story": "Java 后端 面试材料",
                    "tags": ["interview"],
                    "source_paths": ["interview-java-backend/generic.md"],
                },
            ],
        )

        stdout = io.StringIO()
        with (
            patch.object(kb_search, "resolve_context", return_value=ctx),
            patch.object(kb_search, "global_bucket_dir", return_value=root / "global"),
            patch.object(kb_search, "InvertedIndex", None),
            patch.object(kb_search, "get_inverted_index_path", None),
            contextlib.redirect_stdout(stdout),
        ):
            rc = call_main(
                kb_rag_context.main,
                ["demo-inventory-service Java 后端 面试材料", "--json", "--limit", "5", "--no-session-briefs"],
            )

        assert rc == 0
        payload = json.loads(stdout.getvalue())
        assert [item["entry_id"] for item in payload["items"]] == ["inventory-material"]


def test_rag_context_reads_recent_session_briefs_before_long_term_kb() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        ctx = make_context(root)
        write_jsonl(
            ctx.kb_path,
            [
                {
                    "id": "long-term",
                    "ts": "2026-01-01T00:00:00+00:00",
                    "kind": "implementation",
                    "repo": "demo",
                    "branch": "main",
                    "title": "long term RAG flow",
                    "story": "durable RAG rule",
                    "tags": ["personal-kb", "RAG-first"],
                    "trigger_terms": ["kb_rag_context.py"],
                    "source_paths": ["skills/personal-kb/SKILL.md"],
                }
            ],
        )
        write_jsonl(
            root / "repos" / "_meta" / "session_briefs.jsonl",
            [
                kb_session_brief.build_brief(
                    title="recent closeout correction",
                    summary="user just corrected the closeout and current entrypoint route",
                    repo="demo",
                    branch="main",
                    cwd=str(root / "workspace"),
                    tags=["recent-session", "personal-kb"],
                    anchors=["closeout", "entrypoint"],
                    queries=["closeout entrypoint personal-kb"],
                    used_entry_ids=[],
                    written_entry_ids=[],
                    updated_entry_ids=[],
                    source="codex",
                    session_id="s-1",
                )
            ],
        )

        stdout = io.StringIO()
        with (
            patch.object(kb_search, "resolve_context", return_value=ctx),
            patch.object(kb_search, "global_bucket_dir", return_value=root / "global"),
            patch.object(kb_search, "InvertedIndex", None),
            patch.object(kb_search, "get_inverted_index_path", None),
            patch.object(kb_session_brief, "resolve_context", return_value=ctx),
            patch.object(kb_session_brief, "kb_base_dir", return_value=root / "repos"),
            contextlib.redirect_stdout(stdout),
        ):
            rc = call_main(
                kb_rag_context.main,
                ["closeout entrypoint personal-kb", "--json", "--limit", "5"],
            )

        assert rc == 0
        payload = json.loads(stdout.getvalue())
        assert payload["items"][0]["kind"] == "session_brief"
        assert payload["items"][0]["context_layer"] == "recent_session"
        assert payload["items"][0]["query_groups"] == ["recent-session"]
        assert any(group["name"] == "recent-session" for group in payload["query_groups"])


def test_rag_context_filters_by_min_confidence() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        ctx = make_context(root)
        write_jsonl(
            ctx.kb_path,
            [
                {
                    "id": "strong-hit",
                    "ts": "2026-01-01T00:00:00+00:00",
                    "kind": "issue",
                    "repo": "demo",
                    "branch": "main",
                    "title": "dynamic datasource timeout",
                    "story": "primary datasource timeout",
                    "trigger_terms": ["dynamic-datasource", "primary"],
                    "source_paths": ["logs/app.log"],
                },
                {
                    "id": "weak-hit",
                    "ts": "2026-01-02T00:00:00+00:00",
                    "kind": "experience",
                    "repo": "demo",
                    "branch": "main",
                    "title": "generic note",
                    "story": "dynamic-datasource primary timeout only mentioned in prose",
                },
            ],
        )

        stdout = io.StringIO()
        with (
            patch.object(kb_search, "resolve_context", return_value=ctx),
            patch.object(kb_search, "global_bucket_dir", return_value=root / "global"),
            patch.object(kb_search, "InvertedIndex", None),
            patch.object(kb_search, "get_inverted_index_path", None),
            contextlib.redirect_stdout(stdout),
        ):
            rc = call_main(
                kb_rag_context.main,
                [
                    "dynamic-datasource primary",
                    "--json",
                    "--include-weak",
                    "--min-confidence",
                    "0.75",
                    "--no-session-briefs",
                ],
            )

        assert rc == 0
        payload = json.loads(stdout.getvalue())
        assert [item["entry_id"] for item in payload["items"]] == ["strong-hit"]
        assert payload["filtered_low_confidence_count"] >= 1


def test_rag_context_applies_max_total_chars_stably() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        ctx = make_context(root)
        long_story = "timeout detail " * 50
        write_jsonl(
            ctx.kb_path,
            [
                {
                    "id": "entry-1",
                    "ts": "2026-01-01T00:00:00+00:00",
                    "kind": "issue",
                    "repo": "demo",
                    "branch": "main",
                    "title": "order timeout entry one",
                    "story": long_story,
                    "trigger_terms": ["order-timeout"],
                    "source_paths": ["logs/one.log"],
                },
                {
                    "id": "entry-2",
                    "ts": "2026-01-02T00:00:00+00:00",
                    "kind": "issue",
                    "repo": "demo",
                    "branch": "main",
                    "title": "order timeout entry two",
                    "story": long_story,
                    "trigger_terms": ["order-timeout"],
                    "source_paths": ["logs/two.log"],
                },
            ],
        )

        stdout = io.StringIO()
        with (
            patch.object(kb_search, "resolve_context", return_value=ctx),
            patch.object(kb_search, "global_bucket_dir", return_value=root / "global"),
            patch.object(kb_search, "InvertedIndex", None),
            patch.object(kb_search, "get_inverted_index_path", None),
            contextlib.redirect_stdout(stdout),
        ):
            rc = call_main(
                kb_rag_context.main,
                [
                    "order-timeout",
                    "--json",
                    "--limit",
                    "2",
                    "--max-total-chars",
                    "260",
                    "--no-session-briefs",
                ],
            )

        assert rc == 0
        payload = json.loads(stdout.getvalue())
        assert payload["hit_count"] == len(payload["items"])
        assert payload["truncation"]["max_total_chars"] == 260
        assert payload["truncation"]["returned_item_chars"] <= 260
        assert payload["truncation"]["omitted_items"] >= 0


def test_rag_context_recent_brief_does_not_outrank_strong_long_term_hit() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        ctx = make_context(root)
        write_jsonl(
            ctx.kb_path,
            [
                {
                    "id": "strong-long-term",
                    "ts": "2026-01-01T00:00:00+00:00",
                    "kind": "issue",
                    "repo": "demo",
                    "branch": "main",
                    "title": "dynamic datasource primary missing",
                    "story": "stable fix for datasource primary",
                    "trigger_terms": ["dynamic-datasource", "primary"],
                    "source_paths": ["logs/app.log"],
                }
            ],
        )
        write_jsonl(
            root / "repos" / "_meta" / "session_briefs.jsonl",
            [
                kb_session_brief.build_brief(
                    title="recent note",
                    summary="user corrected current task boundary",
                    repo="demo",
                    branch="main",
                    cwd=str(root / "workspace"),
                    tags=["recent-session", "personal-kb"],
                    anchors=["primary"],
                    queries=["primary"],
                    used_entry_ids=[],
                    written_entry_ids=[],
                    updated_entry_ids=[],
                    source="codex",
                    session_id="s-1",
                )
            ],
        )

        stdout = io.StringIO()
        with (
            patch.object(kb_search, "resolve_context", return_value=ctx),
            patch.object(kb_search, "global_bucket_dir", return_value=root / "global"),
            patch.object(kb_search, "InvertedIndex", None),
            patch.object(kb_search, "get_inverted_index_path", None),
            patch.object(kb_session_brief, "resolve_context", return_value=ctx),
            patch.object(kb_session_brief, "kb_base_dir", return_value=root / "repos"),
            contextlib.redirect_stdout(stdout),
        ):
            rc = call_main(
                kb_rag_context.main,
                ["dynamic-datasource primary", "--json", "--limit", "5"],
            )

        assert rc == 0
        payload = json.loads(stdout.getvalue())
        assert [item["entry_id"] for item in payload["items"][:2]] == ["strong-long-term", payload["items"][1]["entry_id"]]
        assert payload["items"][1]["kind"] == "session_brief"


def test_rag_context_downgrades_personal_kb_design_noise_for_non_kb_query() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "kb-design",
            "kind": "implementation",
            "repo": "demo",
            "branch": "main",
            "title": "timeout design note",
            "story": "runtime timeout design",
            "tags": ["personal-kb"],
            "trigger_terms": ["timeout"],
            "source_paths": ["skills/personal-kb/SKILL.md"],
        },
        {
            "id": "business-hit",
            "kind": "issue",
            "repo": "demo",
            "branch": "main",
            "title": "order timeout",
            "story": "business order timeout",
            "trigger_terms": ["timeout"],
            "source_paths": ["services/order/log.txt"],
        },
    ]

    stdout = io.StringIO()
    with (
        patch.object(kb_session_brief, "search_recent_briefs", return_value=[]),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["timeout", "--json", "--no-plan", "--no-session-briefs", "--limit", "2"],
        )

    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert [item["entry_id"] for item in payload["items"]] == ["business-hit", "kb-design"]


def test_rag_context_expands_real_chinese_resume_queries() -> None:
    cases = {
        "帮我分析，我要做简历的话，现在提取出来的内容是否已经足够": "resume-coverage",
        "你能不能去学一下，好的简历怎么制作的？然后修改我的简历？": "resume-current",
        "我的项目经历应该怎么写": "project-experience",
    }
    for query, expected_group in cases.items():
        plans = kb_rag_context._plan_query_groups(query)
        assert any(plan.name == expected_group for plan in plans), (query, plans)

    ambiguous_queries = [
        "这个项目经历了哪些重构",
        "这个仓库有哪些项目",
        "公司项目录怎么配置",
    ]
    for query in ambiguous_queries:
        plans = kb_rag_context._plan_query_groups(query)
        assert all(plan.name != "project-experience" for plan in plans), (query, plans)


def test_rag_context_plans_durable_skill_and_superpowers_history_queries() -> None:
    durable = "更新所有skill，做成命令并记录，下次我一说就直接更新"
    generic = "更新所有 Skill 和 MCP 的说明文件，统一标题格式"
    superpowers = "去看历史记录，superpowers 会不会起反作用"
    expansion = "刚才零命中，请扩大范围再查以前的 cc-switch 启动故障"

    assert any(plan.name == "skill-update-durable" for plan in kb_rag_context._plan_query_groups(durable))
    assert all(plan.name != "skill-update-durable" for plan in kb_rag_context._plan_query_groups(generic))
    assert any(plan.name == "superpowers-governance" for plan in kb_rag_context._plan_query_groups(superpowers))
    expansion_plans = kb_rag_context._plan_query_groups(expansion)
    assert any(plan.name == "retrieval-expansion" for plan in expansion_plans)
    assert "cc-switch" in next(
        plan.query for plan in expansion_plans if plan.name == "retrieval-expansion"
    )


def test_rag_context_short_chinese_resume_query_uses_planner_authority_anchor() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "resume-authority",
            "kind": "map",
            "repo": "demo",
            "branch": "main",
            "title": "示例候选人的当前简历权威入口",
            "tags": ["简历结构", "工作经历"],
            "trigger_terms": ["canonical_content"],
            "source_paths": ["11-简历成稿/00-当前版本.md"],
            "artifact_locator": True,
        },
        {
            "id": "generic-resume-noise",
            "kind": "map",
            "repo": "demo",
            "branch": "main",
            "title": "Java 后端面试材料",
            "tags": ["study", "简历", "面试"],
            "trigger_terms": ["简历"],
            "artifact_locator": True,
        },
    ]
    stdout = io.StringIO()
    with (
        patch.object(kb_session_brief, "search_recent_briefs", return_value=[]),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["请修改我的简历", "--json", "--no-session-briefs"],
        )

    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert [item["entry_id"] for item in payload["items"]] == ["resume-authority"]
    assert "resume-current" in payload["items"][0]["query_groups"]


def test_rag_context_synonym_anchor_survives_original_gate() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "mysql-connection",
            "kind": "issue",
            "repo": "demo",
            "branch": "main",
            "title": "MySQL connection pool failure",
            "trigger_terms": ["mysql"],
            "source_paths": ["logs/mysql-startup.log"],
        },
        {
            "id": "generic-database-noise",
            "kind": "experience",
            "repo": "demo",
            "branch": "main",
            "title": "Java 后端知识整理",
            "story": "generic database prose without a reusable anchor",
            "tags": ["study", "项目"],
        },
    ]
    stdout = io.StringIO()
    with (
        patch.object(kb_rag_context, "load_config", return_value={"search": {"expand_queries": True}}),
        patch.object(kb_rag_context, "load_synonyms", return_value={"数据库": ["mysql"]}),
        patch.object(kb_session_brief, "search_recent_briefs", return_value=[]),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["数据库", "--json", "--no-plan", "--no-session-briefs"],
        )

    assert rc == 0
    assert [item["entry_id"] for item in json.loads(stdout.getvalue())["items"]] == ["mysql-connection"]


def test_rag_context_does_not_treat_all_skill_updates_as_personal_kb() -> None:
    query = "更新所有 skill 和 MCP，还有 cc-switch"
    assert kb_rag_context._is_kb_runtime_query(query) is False
    assert kb_rag_context._is_non_kb_maintenance_query(query) is True
    assert all(plan.name != "personal-kb" for plan in kb_rag_context._plan_query_groups(query))


def test_rag_context_keeps_cross_project_evidence_lead() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "mixed-empty",
            "kind": "implementation",
            "repo": "other",
            "branch": "main",
            "title": "service timeout design",
            "story": "project-private detail",
            "trigger_terms": ["timeout"],
        }
    ]
    stdout = io.StringIO()
    with (
        patch.object(kb_session_brief, "search_recent_briefs", return_value=[]),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(kb_rag_context.main, ["timeout", "--json", "--global", "--no-session-briefs"])
    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert [item["entry_id"] for item in payload["items"]] == ["mixed-empty"]
    item = payload["items"][0]
    assert item["summary"] == "project-private detail"
    assert item["applicability_state"] == "cross_project_review"
    assert item["cross_project"] is True
    assert "same_project" not in item


def test_rag_context_cross_project_map_has_coordinate_summary() -> None:
    raw = {
        "id": "map-empty",
        "kind": "map",
        "repo": "other",
        "branch": "main",
        "title": "project coordinate",
    }
    item = kb_rag_context._compact_entry(
        raw,
        {**raw, "_cross_project": True, "_from_project": "other"},
        terms=["project"],
        max_snippet_chars=200,
    )
    assert "定位映射" in item["summary"]
    assert "other@main" in item["summary"]


def test_rag_context_rejects_generic_artifact_map_without_concrete_anchor() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "generic-artifact",
            "kind": "map",
            "repo": "study",
            "branch": "main",
            "title": "DemoShelf interview material",
            "story": "generic historical project material",
            "tags": ["study", "recent-session", "项目", "历史"],
            "artifact_locator": True,
        }
    ]
    stdout = io.StringIO()
    with (
        patch.object(kb_session_brief, "search_recent_briefs", return_value=[]),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["study recent-session 历史 项目", "--json", "--no-session-briefs"],
        )
    assert rc == 0
    assert json.loads(stdout.getvalue())["items"] == []


def test_rag_context_fault_query_prioritizes_pitfall_over_artifact_map() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "cc-artifact",
            "kind": "map",
            "repo": "demo",
            "branch": "main",
            "title": "cc-switch troubleshooting material",
            "tags": ["cc-switch"],
            "trigger_terms": ["cc-switch"],
            "artifact_locator": True,
        },
        {
            "id": "cc-pitfall",
            "kind": "pitfall",
            "repo": "demo",
            "branch": "main",
            "title": "cc-switch startup freeze",
            "story": "verified root cause and recovery",
            "trigger_terms": ["cc-switch", "卡死"],
        },
    ]
    stdout = io.StringIO()
    with (
        patch.object(kb_session_brief, "search_recent_briefs", return_value=[]),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["cc-switch 卡死 问题", "--json", "--no-session-briefs", "--limit", "2"],
        )
    assert rc == 0
    assert json.loads(stdout.getvalue())["items"][0]["entry_id"] == "cc-pitfall"


def test_rag_context_rejects_session_brief_matched_only_by_generic_context() -> None:
    ctx = make_context(Path("/tmp/demo"))
    generic_brief = {
        "entry_id": "brief-generic",
        "kind": "session_brief",
        "title": "recent note",
        "repo": "study",
        "branch": "main",
        "confidence": 0.8,
        "matched_fields": ["tags", "repo"],
        "summary": "unrelated recent work",
        "anchors": ["recent-session"],
        "queries": [],
        "_score": 3.0,
    }
    stdout = io.StringIO()
    with (
        patch.object(kb_session_brief, "search_recent_briefs", return_value=[generic_brief]),
        patch.object(kb_search, "_search_once", return_value=([], ctx)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(kb_rag_context.main, ["study recent-session", "--json"])
    assert rc == 0
    assert json.loads(stdout.getvalue())["items"] == []


def test_rag_context_boosts_prior_requirement_but_keeps_recheck_warning() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "resume-decision",
            "kind": "requirement",
            "repo": "demo",
            "branch": "main",
            "title": "简历结构已确认",
            "story": "在线考试归入工作经历，不单列项目经历。",
            "tags": ["简历结构", "工作经历", "项目经历"],
            "trigger_terms": ["考试", "单列"],
            "status": "decision_confirmed",
        }
    ]
    stdout = io.StringIO()
    with (
        patch.object(kb_session_brief, "search_recent_briefs", return_value=[]),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["在线考试要不要单列，按上次决定", "--json", "--no-session-briefs"],
        )
    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert payload["items"][0]["entry_id"] == "resume-decision"
    assert "verify against the current request and current evidence" in payload["items"][0]["warning"]


def test_rag_context_recognizes_natural_prior_decision_phrasing() -> None:
    online_exam = "在线考试要不要单列为项目经历？按上次确认的决定来。"
    resume_variant = "简历备选版本，之前的那个呢？前两天的也保留。"

    assert kb_rag_context._is_prior_decision_query(online_exam)
    assert kb_rag_context._is_prior_decision_query(resume_variant)
    online_plans = kb_rag_context._plan_query_groups(online_exam)
    resume_plans = kb_rag_context._plan_query_groups(resume_variant)
    assert online_plans[0].name == "prior-decision"
    assert "在线考试" in online_plans[0].query
    assert resume_plans[0].name == "prior-decision"
    assert "简历" in resume_plans[0].query
    english_plans = kb_rag_context._plan_query_groups(
        "Use the decision we agreed on last time for the resume structure."
    )
    assert english_plans[0].name == "prior-decision"
    assert "简历" in english_plans[0].query
    assert not kb_rag_context._is_prior_decision_query("前两天这个 500 又发生了，请查相似 issue")


def test_rag_context_prior_decision_requires_confirmed_status_or_authority() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "authority-confirmed",
            "kind": "requirement",
            "repo": "demo",
            "branch": "main",
            "title": "简历结构权威口径",
            "story": "在线考试归入工作经历，不单列项目经历。",
            "tags": ["简历结构", "工作经历", "项目经历"],
            "trigger_terms": ["考试", "单列"],
            "status": "current",
            "authority": "verified_summary",
        },
        {
            "id": "plain-requirement",
            "kind": "requirement",
            "repo": "demo",
            "branch": "main",
            "title": "简历结构普通建议",
            "story": "在线考试也许可以单列项目经历。",
            "tags": ["简历结构", "工作经历", "项目经历"],
            "trigger_terms": ["考试", "单列"],
            "status": "current",
        },
    ]
    stdout = io.StringIO()
    with (
        patch.object(kb_session_brief, "search_recent_briefs", return_value=[]),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["在线考试要不要单列，按上次决定", "--json", "--no-session-briefs"],
        )

    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert [item["entry_id"] for item in payload["items"]] == ["authority-confirmed"]
    assert "verify against the current request and current evidence" in payload["items"][0]["warning"]


def test_rag_context_keeps_precise_resume_artifact_lookup() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "inventory-artifact",
            "kind": "map",
            "repo": "demo",
            "branch": "main",
            "title": "demo-inventory-service Java 后端面试材料",
            "tags": ["demo-inventory-service", "简历", "面试"],
            "trigger_terms": ["demo-inventory-service"],
            "source_paths": ["interview-java-backend/demo-inventory-service.md"],
            "artifact_locator": True,
        }
    ]
    stdout = io.StringIO()
    with (
        patch.object(kb_session_brief, "search_recent_briefs", return_value=[]),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["demo-inventory-service Java 后端 面试材料", "--json", "--no-session-briefs"],
        )
    assert rc == 0
    assert json.loads(stdout.getvalue())["items"][0]["entry_id"] == "inventory-artifact"


def test_rag_context_rejects_nonmap_without_original_concrete_anchor() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "unrelated-requirement",
            "kind": "requirement",
            "repo": "study",
            "branch": "main",
            "title": "退出 cc-switch 前安排延迟重启",
            "story": "Skill updater must restart safely.",
            "trigger_terms": ["cc-switch", "skill"],
        }
    ]
    stdout = io.StringIO()
    with (
        patch.object(kb_session_brief, "search_recent_briefs", return_value=[]),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["superpower 历史误触发 流程膨胀 skill 插件", "--json", "--no-session-briefs"],
        )
    assert rc == 0
    assert json.loads(stdout.getvalue())["items"] == []


def test_rag_context_fault_query_requires_product_and_fault_anchor() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "restart-only",
            "kind": "requirement",
            "repo": "demo",
            "branch": "main",
            "title": "cc-switch safe restart",
            "trigger_terms": ["cc-switch"],
        },
        {
            "id": "ime-pitfall",
            "kind": "pitfall",
            "repo": "demo",
            "branch": "main",
            "title": "cc-switch WSLg input method failure",
            "trigger_terms": ["cc-switch", "WSLg", "IBus"],
        },
        {
            "id": "time-false-positive",
            "kind": "pitfall",
            "repo": "demo",
            "branch": "main",
            "title": "unrelated timeout note",
            "trigger_terms": ["runtime"],
        },
    ]
    stdout = io.StringIO()
    with (
        patch.object(kb_session_brief, "search_recent_briefs", return_value=[]),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["CC Switch WSLg IME IBus 无法输入中文", "--json", "--no-session-briefs"],
        )
    assert rc == 0
    assert [item["entry_id"] for item in json.loads(stdout.getvalue())["items"]] == ["ime-pitfall"]


def test_rag_context_searches_raw_query_before_expansion() -> None:
    ctx = make_context(Path("/tmp/demo"))
    calls: list[str] = []

    def search_once(*, query: str, **_kwargs: Any) -> tuple[list[dict[str, Any]], TempContext]:
        calls.append(query)
        if query == "order-42 timeout":
            return [], ctx
        return ([{
            "id": "expanded-hit",
            "kind": "issue",
            "repo": "demo",
            "branch": "main",
            "title": "order-42 timeout incident",
            "trigger_terms": ["order-42", "timeout"],
        }], ctx)

    stdout = io.StringIO()
    with (
        patch.object(
            kb_rag_context,
            "load_config",
            return_value={"search": {"expand_queries": True, "precision": {"max_query_variants": 3}}},
        ),
        patch.object(kb_rag_context, "expand_query_variants", return_value=["generic translated"]),
        patch.object(kb_rag_context, "load_synonyms", return_value={}),
        patch.object(kb_search, "_search_once", side_effect=search_once),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["order-42 timeout", "--json", "--no-plan", "--no-session-briefs"],
        )

    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert calls[0] == "order-42 timeout"
    assert calls[1:] == ["order-42 generic translated"]
    assert payload["query_expansion"]["performed"] is True
    assert [item["entry_id"] for item in payload["items"]] == ["expanded-hit"]


def test_rag_context_skips_expansion_after_two_raw_strong_hits() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": f"strong-{index}",
            "kind": "issue",
            "repo": "demo",
            "branch": "main",
            "title": (
                "order-42 timeout payment webhook incident"
                if index == 1
                else "order-42 timeout redis session incident"
            ),
            "trigger_terms": ["order-42", "timeout"],
            "source_paths": [f"logs/order-{index}.log"],
        }
        for index in (1, 2)
    ]
    calls: list[str] = []

    def search_once(*, query: str, **_kwargs: Any) -> tuple[list[dict[str, Any]], TempContext]:
        calls.append(query)
        return raw_results, ctx

    stdout = io.StringIO()
    with (
        patch.object(
            kb_rag_context,
            "load_config",
            return_value={"search": {"expand_queries": True, "precision": {"max_query_variants": 4}}},
        ),
        patch.object(kb_rag_context, "expand_query_variants", return_value=["generic translated"]),
        patch.object(kb_rag_context, "load_synonyms", return_value={}),
        patch.object(kb_search, "_search_once", side_effect=search_once),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["order-42 timeout", "--json", "--no-plan", "--no-session-briefs"],
        )

    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert calls == ["order-42 timeout"]
    assert payload["query_expansion"]["performed"] is False
    assert payload["query_expansion"]["variants"] == []
    assert {item["entry_id"] for item in payload["items"]} == {"strong-1", "strong-2"}


def test_rag_context_expansion_variants_keep_original_anchor() -> None:
    with patch.object(
        kb_rag_context,
        "expand_query_variants",
        return_value=["generic translated timeout", "another broad rewrite"],
    ):
        variants = kb_rag_context._safe_query_variants(
            "order-42 timeout",
            anchor_terms=["order-42"],
            synonyms={},
            max_variants=4,
        )

    assert variants[0] == "order-42 timeout"
    assert len(variants) <= 4
    assert all("order-42" in variant for variant in variants)


def test_rag_context_legacy_records_preserve_complementary_leads() -> None:
    items = [
        {"entry_id": "fresh", "freshness_state": "fresh"},
        {"entry_id": "recheck", "freshness_state": "needs_recheck"},
        {"entry_id": "legacy", "freshness_state": "legacy_unverified"},
    ]

    filtered, removed = kb_rag_context._filter_freshness_items(
        items,
        include_noncurrent=False,
        legacy_fallback=True,
    )

    assert [item["entry_id"] for item in filtered] == ["fresh", "recheck", "legacy"]
    assert removed == 0
    assert filtered[-1].get("warning")

    fallback_items, fallback_removed = kb_rag_context._filter_freshness_items(
        items[1:],
        include_noncurrent=False,
        legacy_fallback=True,
    )
    assert [item["entry_id"] for item in fallback_items] == ["recheck", "legacy"]
    assert fallback_removed == 0

    hidden_items, hidden_removed = kb_rag_context._filter_freshness_items(
        items[1:],
        include_noncurrent=False,
        legacy_fallback=False,
    )
    assert [item["entry_id"] for item in hidden_items] == ["recheck"]
    assert hidden_removed == 1


def test_rag_context_expansion_gate_uses_raw_task_hits_only() -> None:
    ctx = make_context(Path("/tmp/demo"))
    planner_hits = [
        {
            "id": "planner-1",
            "kind": "issue",
            "repo": "demo",
            "branch": "main",
            "title": "known planner issue one",
            "trigger_terms": ["planner-anchor", "failure"],
            "source_paths": ["logs/planner-1.log"],
        },
        {
            "id": "planner-2",
            "kind": "issue",
            "repo": "demo",
            "branch": "main",
            "title": "known planner issue two",
            "trigger_terms": ["planner-anchor", "failure"],
            "source_paths": ["logs/planner-2.log"],
        },
    ]
    calls: list[str] = []

    def search_once(*, query: str, **_kwargs: Any) -> tuple[list[dict[str, Any]], TempContext]:
        calls.append(query)
        if query == "planner concept":
            return planner_hits, ctx
        if query == "raw translated":
            return [
                {
                    "id": "expanded-hit",
                    "kind": "issue",
                    "repo": "demo",
                    "branch": "main",
                    "title": "raw translated issue",
                    "trigger_terms": ["raw", "translated"],
                    "source_paths": ["logs/raw.log"],
                }
            ], ctx
        return [], ctx

    stdout = io.StringIO()
    with (
        patch.object(
            kb_rag_context,
            "load_config",
            return_value={"search": {"expand_queries": True, "precision": {"max_query_variants": 2}}},
        ),
        patch.object(kb_rag_context, "load_synonyms", return_value={}),
        patch.object(
            kb_rag_context,
            "_plan_query_groups",
            return_value=[
                kb_rag_context.QueryPlan("task", "raw query", 0.0, None),
                kb_rag_context.QueryPlan("specific-terms", "planner concept", 0.35, "any"),
            ],
        ),
        patch.object(kb_rag_context, "expand_query_variants", return_value=["raw translated"]),
        patch.object(kb_search, "_search_once", side_effect=search_once),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["raw query", "--json", "--no-session-briefs"],
        )

    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert payload["query_expansion"]["performed"] is True
    assert "raw translated" in calls


def test_kb_search_expansion_infers_original_anchor() -> None:
    with patch.object(kb_search, "expand_query_variants", return_value=["database"]):
        variants = kb_search._bounded_query_variants("数据库", max_variants=3)

    assert variants[0] == "数据库"
    assert all("数据库" in variant for variant in variants)


def test_rag_context_ranks_fresh_evidence_above_needs_recheck() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "stale-hot",
            "kind": "issue",
            "repo": "demo",
            "branch": "main",
            "title": "order-42 timeout old diagnosis",
            "trigger_terms": ["order-42", "timeout"],
            "source_paths": ["logs/old.log"],
            "used_count": 200,
        },
        {
            "id": "fresh-cold",
            "kind": "issue",
            "repo": "demo",
            "branch": "main",
            "title": "order-42 timeout current diagnosis",
            "trigger_terms": ["order-42", "timeout"],
            "source_paths": ["logs/current.log"],
            "used_count": 0,
        },
    ]

    def freshness(entry: dict[str, Any]) -> dict[str, Any]:
        state = "fresh" if entry["id"] == "fresh-cold" else "needs_recheck"
        return {
            "state": state,
            "scope": "local_head",
            "applicability_state": "current" if state == "fresh" else "needs_review",
            "items": [],
            "warning": "" if state == "fresh" else "needs_recheck: source changed",
        }

    stdout = io.StringIO()
    with (
        patch.object(kb_rag_context, "load_config", return_value={"search": {"expand_queries": False}}),
        patch.object(kb_rag_context, "_safe_evidence_verification", side_effect=freshness),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["order-42 timeout", "--json", "--no-plan", "--no-session-briefs"],
        )

    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert [item["entry_id"] for item in payload["items"]] == ["fresh-cold", "stale-hot"]
    assert payload["items"][0]["trust_tier"] == "verified"
    assert payload["items"][1]["trust_tier"] == "recheck"


def test_rag_context_dedupes_semantically_similar_results() -> None:
    ctx = make_context(Path("/tmp/demo"))
    raw_results = [
        {
            "id": "duplicate-a",
            "kind": "issue",
            "repo": "demo",
            "branch": "main",
            "title": "order-42 timeout diagnosis",
            "trigger_terms": ["order-42", "timeout"],
            "source_paths": ["logs/order-42.log"],
        },
        {
            "id": "duplicate-b",
            "kind": "issue",
            "repo": "demo",
            "branch": "main",
            "title": "order-42 timeout diagnosis copy",
            "trigger_terms": ["order-42", "timeout"],
            "source_paths": ["logs/order-42.log"],
        },
    ]
    stdout = io.StringIO()
    with (
        patch.object(kb_rag_context, "load_config", return_value={"search": {"expand_queries": False}}),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["order-42 timeout", "--json", "--no-plan", "--no-session-briefs"],
        )

    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert payload["hit_count"] == 1
    assert payload["deduped_similar_count"] == 1
    assert len(payload["items"]) == 1


def test_rag_context_keeps_distinct_conclusions_with_shared_evidence() -> None:
    ctx = make_context(Path("/tmp/demo"))
    common = {
        "kind": "issue",
        "repo": "demo",
        "branch": "main",
        "trigger_terms": ["order-42", "timeout"],
        "source_paths": ["logs/order-42.log"],
    }
    raw_results = [
        {**common, "id": "timeout", "title": "order-42 timeout diagnosis"},
        {**common, "id": "retry", "title": "order-42 retry duplicates payment"},
        {**common, "id": "requirement", "kind": "requirement", "title": "order-42 timeout diagnosis"},
    ]
    stdout = io.StringIO()
    with (
        patch.object(kb_rag_context, "load_config", return_value={"search": {"expand_queries": False}}),
        patch.object(kb_search, "_search_once", return_value=(raw_results, ctx)),
        patch.object(kb_search, "_format_cross_project_entry", side_effect=lambda entry, _repo: dict(entry)),
        contextlib.redirect_stdout(stdout),
    ):
        rc = call_main(
            kb_rag_context.main,
            ["order-42 timeout", "--json", "--no-plan", "--no-session-briefs"],
        )
    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert {item["entry_id"] for item in payload["items"]} == {"timeout", "retry", "requirement"}
    assert payload.get("deduped_similar_count", 0) == 0


def test_dedupe_preserves_cc_switch_rules_and_kind_boundaries() -> None:
    common = {"kind": "requirement", "repo": "study", "branch": "no-git"}
    restart = {**common, "title": "CC Switch（ccs）更新与重启必须使用安全入口"}
    display = {**common, "title": "本机多显示器下 CC Switch 禁止持久强制 X11"}
    for field in ("source_paths", "key_files"):
        left = {**restart, field: ["tools/cc-switch-restart", "AGENTS.md"]}
        right = {**display, field: ["tools/cc-switch-restart", "AGENTS.md"]}
        assert kb_rag_context._dedupe_similar_items([left, right]) == ([left, right], 0)
    for kind in ("issue", "map"):
        other_kind = {**restart, "kind": kind}
        assert not kb_rag_context._similar_result(restart, other_kind)
        assert not kb_rag_context._similar_result(other_kind, restart)
    assert not kb_rag_context._similar_result(common, common)
    for scope in ({"repo": "other"}, {"branch": "other"}):
        other_scope = {**restart, **scope}
        assert not kb_rag_context._similar_result(restart, other_scope)
    duplicate = {**restart, "title": "CC Switch ccs 更新与重启必须使用安全入口"}
    assert kb_rag_context._similar_result(restart, duplicate)


def main() -> int:
    tests = [
        test_search_matches_trigger_terms_and_source_paths,
        test_ccs_update_gets_a_dedicated_safe_update_plan,
        test_rag_context_is_compact_and_read_only,
        test_rag_context_rejects_invalid_retrieval_id,
        test_scope_anchor_must_be_bound_and_reports_non_retryable_json_error,
        test_scope_anchor_and_receipt_output_are_compatible,
        test_best_effort_converts_runtime_unavailability_to_empty_result,
        test_retrieval_ids_are_unique_for_zero_hit_calls,
        test_production_retrieval_rejects_caller_supplied_id,
        test_markdown_header_escapes_query_control_tokens,
        test_rag_context_redacts_cross_project_paths,
        test_rag_context_ranks_authoritative_record_above_superseded_memory_design,
        test_rag_context_filters_noncurrent_records_by_default,
        test_rag_context_plans_task_query_and_filters_weak_story_noise,
        test_rag_context_recognizes_unspaced_chinese_kbskill_query,
        test_rag_context_accepts_planner_only_concept_match_but_rejects_story_noise,
        test_rag_context_keeps_precise_project_hits_after_weak_filtering,
        test_rag_context_reads_recent_session_briefs_before_long_term_kb,
        test_rag_context_filters_by_min_confidence,
        test_rag_context_applies_max_total_chars_stably,
        test_rag_context_recent_brief_does_not_outrank_strong_long_term_hit,
        test_rag_context_downgrades_personal_kb_design_noise_for_non_kb_query,
        test_rag_context_expands_real_chinese_resume_queries,
        test_rag_context_plans_durable_skill_and_superpowers_history_queries,
        test_rag_context_short_chinese_resume_query_uses_planner_authority_anchor,
        test_rag_context_synonym_anchor_survives_original_gate,
        test_rag_context_does_not_treat_all_skill_updates_as_personal_kb,
        test_rag_context_keeps_cross_project_evidence_lead,
        test_rag_context_cross_project_map_has_coordinate_summary,
        test_rag_context_rejects_generic_artifact_map_without_concrete_anchor,
        test_rag_context_fault_query_prioritizes_pitfall_over_artifact_map,
        test_rag_context_rejects_session_brief_matched_only_by_generic_context,
        test_rag_context_boosts_prior_requirement_but_keeps_recheck_warning,
        test_rag_context_recognizes_natural_prior_decision_phrasing,
        test_rag_context_prior_decision_requires_confirmed_status_or_authority,
        test_rag_context_keeps_precise_resume_artifact_lookup,
        test_rag_context_rejects_nonmap_without_original_concrete_anchor,
        test_rag_context_fault_query_requires_product_and_fault_anchor,
        test_rag_context_searches_raw_query_before_expansion,
        test_rag_context_skips_expansion_after_two_raw_strong_hits,
        test_rag_context_expansion_variants_keep_original_anchor,
        test_rag_context_legacy_records_preserve_complementary_leads,
        test_rag_context_expansion_gate_uses_raw_task_hits_only,
        test_kb_search_expansion_infers_original_anchor,
        test_rag_context_ranks_fresh_evidence_above_needs_recheck,
        test_rag_context_dedupes_semantically_similar_results,
        test_rag_context_keeps_distinct_conclusions_with_shared_evidence,
        test_dedupe_preserves_cc_switch_rules_and_kind_boundaries,
    ]
    failures: list[str] = []
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failures.append(test.__name__)
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")

    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}")
        return 1

    print("kb_rag_context tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_TEST_GUARD.run(main) if _TEST_GUARD else main())
