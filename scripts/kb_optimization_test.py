"""Isolated correctness and latency regressions; never mutates production KB."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import kb_test_guard

_TEST_GUARD = kb_test_guard.activate(__file__) if __name__ == "__main__" else None

import kb_evidence as evidence
import kb_timing
import kb_git_reads
import kb_lib
import kb
import kb_rag_context as rag


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "KB Regression")
    git(path, "config", "user.email", "kb@example.invalid")
    git(path, "remote", "add", "origin", "https://example.invalid/project.git")
    (path / "evidence file.txt").write_text("version one\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


def entry(repo: Path) -> dict:
    row = {"id": "synthetic", "workspace_dir": str(repo), "source_paths": ["evidence file.txt"]}
    row["evidence_snapshots"] = evidence.capture_evidence_snapshots(row, repo)
    return row


class OptimizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="kb-optimization-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = make_repo(self.root / "repo")
        self.entry = entry(self.repo)

    def test_status_and_index_nonzero_never_become_fresh(self):
        original = evidence.subprocess.run
        for failed_command in ("status", "ls-files"):
            def fail(args, *positional, **kwargs):
                if args[:2] == ["git", failed_command]:
                    return subprocess.CompletedProcess(args, 128, "", "synthetic failure")
                return original(args, *positional, **kwargs)
            with self.subTest(command=failed_command), patch.object(evidence.subprocess, "run", side_effect=fail):
                self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "unresolvable")

    def test_status_execution_error_never_becomes_fresh(self):
        original = evidence.subprocess.run
        def fail(args, *positional, **kwargs):
            if args[:2] == ["git", "status"]:
                raise PermissionError("synthetic failure")
            return original(args, *positional, **kwargs)
        with patch.object(evidence.subprocess, "run", side_effect=fail):
            self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "unresolvable")

    def test_timing_counts_exact_repeats_without_exposing_operands(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with kb_timing.recording("test") as report:
                for repo, args in [(self.repo, ["status", "private-path"]),
                                   (self.repo, ["status", "private-path"]),
                                   (self.repo, ["status", "different-path"]),
                                   (self.root, ["status", "private-path"])]:
                    with kb_timing.git_span(args, cwd=repo):
                        pass
                report.exit_code = 0
        text = stderr.getvalue()
        row = json.loads(text.removeprefix("KB_TIMING "))["git_checks"]["status"]
        self.assertEqual((row["calls"], row["unique_checks"], row["repeated_calls"]), (4, 3, 1))
        self.assertGreaterEqual(row["repeated_ms"], 0)
        self.assertNotIn("private-path", text)
        self.assertNotIn(str(self.root), text)

    def test_conflicts_in_new_index_are_observed_in_the_same_retrieval(self):
        file = self.repo / "归档 检查.jsonl"
        file.write_text("{}\n")
        git(self.repo, "add", file.name)
        oid = git(self.repo, "rev-parse", ":" + file.name)
        with kb_git_reads.read_scope(), patch.object(subprocess, "run", wraps=subprocess.run) as calls:
            kb_lib.ensure_jsonl_safe(file)
            kb_lib.ensure_jsonl_safe(file)
            command = ["git", "ls-files", "--unmerged", "--full-name", "-z"]
            self.assertEqual(sum(c.args[0] == command for c in calls.call_args_list), 1)
            # Insert unresolved stages without changing the JSONL bytes.
            update = f"0 {'0' * len(oid)}\t{file.name}\n100644 {oid} 1\t{file.name}\n100644 {oid} 2\t{file.name}\n"
            subprocess.run(["git", "update-index", "--index-info"], cwd=self.repo,
                           input=update, text=True, check=True, capture_output=True)
            with self.assertRaises(kb_lib.JsonlSafetyError):
                kb_lib.ensure_jsonl_safe(file)
            self.assertEqual(sum(c.args[0] == command for c in calls.call_args_list), 2)
            git(self.repo, "add", file.name)
            kb_lib.ensure_jsonl_safe(file)

    def test_stable_git_observations_are_reused_only_within_each_retrieval(self):
        # Use a known single-source configuration regardless of the user's global includes.
        with patch.object(kb_git_reads, "_config_paths", return_value=[]), \
                patch.object(evidence, "_run_git", wraps=evidence._run_git) as calls:
            for _ in range(2):
                calls.reset_mock()
                with kb_git_reads.read_scope():
                    for _ in range(2):
                        self.assertEqual(evidence._git_root(self.repo), self.repo)
                        self.assertEqual(evidence._current_checkout(self.repo)[0], "main")
                        self.assertTrue(evidence._repo_id(self.repo))
                commands = [c.args[1:] for c in calls.call_args_list]
                self.assertEqual(commands.count(("rev-parse", "--show-toplevel")), 1)
                self.assertEqual(commands.count(("rev-parse", "HEAD", "--symbolic-full-name", "HEAD")), 1)
                self.assertEqual(commands.count(("remote", "get-url", "origin")), 1)

    def test_branch_switch_during_read_does_not_cache_another_branch_at_the_same_commit(self):
        def switched_checkout():
            git(self.repo, "switch", "-c", "other")
            return evidence._read_checkout(self.repo)

        with kb_git_reads.read_scope():
            self.assertEqual(kb_git_reads.checkout(self.repo, switched_checkout)[0], "other")
            git(self.repo, "switch", "main")
            self.assertEqual(evidence._current_checkout(self.repo)[0], "main")

    def test_included_remote_config_change_is_observed(self):
        included = self.root / "remote.conf"
        included.write_text("")
        git(self.repo, "config", "include.path", str(included))
        with kb_git_reads.read_scope():
            original = evidence._repo_id(self.repo)
            git(self.repo, "config", "--file", str(included), "url.https://mirror.invalid/.insteadOf",
                "https://example.invalid/")
            self.assertNotEqual(evidence._repo_id(self.repo), original)

    def test_object_facts_reused_but_not_across_requests(self):
        original = evidence._run_git
        with patch.object(evidence, "_run_git", wraps=original) as calls:
            with evidence.verification_scope():
                self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "fresh")
                self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "fresh")
            operations = [call.args[1] for call in calls.call_args_list]
            self.assertEqual(operations.count("cat-file"), 1)
            self.assertEqual(operations.count("merge-base"), 1)
            self.assertEqual(operations.count("status"), 0)  # Direct subprocess; checked below.
            calls.reset_mock()
            with evidence.verification_scope():
                self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "fresh")
            self.assertEqual(sum(call.args[1] == "cat-file" for call in calls.call_args_list), 1)
        self.assertIsNone(evidence._FACTS.get())

    def test_same_path_old_and_new_snapshots_remain_distinct(self):
        old = self.entry
        (self.repo / "evidence file.txt").write_text("version two\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "update")
        new = entry(self.repo)
        with evidence.verification_scope():
            self.assertEqual(evidence.verify_entry_evidence(old)["state"], "needs_recheck")
            self.assertEqual(evidence.verify_entry_evidence(new)["state"], "fresh")
            self.assertEqual(evidence.verify_entry_evidence(old)["state"], "needs_recheck")

    def test_mutable_checks_are_not_cached(self):
        source = self.repo / "evidence file.txt"
        with kb_git_reads.read_scope(), evidence.verification_scope():
            self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "fresh")
            source.write_text("modified\n")
            self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "dirty_worktree")
            git(self.repo, "add", ".")
            self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "dirty_worktree")
            git(self.repo, "restore", "--staged", ".")
            git(self.repo, "restore", ".")
            git(self.repo, "remote", "set-url", "origin", "https://example.invalid/another.git")
            self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "diverged")
            git(self.repo, "remote", "set-url", "origin", "https://example.invalid/project.git")
            git(self.repo, "switch", "-c", "other")
            self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "diverged")

    def test_head_change_inside_request_uses_new_oid(self):
        with kb_git_reads.read_scope(), evidence.verification_scope():
            self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "fresh")
            (self.repo / "evidence file.txt").write_text("updated\n")
            git(self.repo, "add", ".")
            git(self.repo, "commit", "-m", "update")
            self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "needs_recheck")
            self.assertEqual(evidence.verify_entry_evidence(entry(self.repo))["state"], "fresh")

    def test_nested_repo_and_linked_worktree_are_separate(self):
        nested = make_repo(self.repo / "nested")
        linked = self.root / "linked"
        git(self.repo, "worktree", "add", "-b", "linked", str(linked))
        nested_entry, linked_entry = entry(nested), entry(linked)
        # A nested untracked repo is outside the evidence path being checked.
        with kb_git_reads.read_scope(), evidence.verification_scope():
            for row in (self.entry, nested_entry, linked_entry):
                self.assertEqual(evidence.verify_entry_evidence(row)["state"], "fresh")
            (linked / "evidence file.txt").write_text("linked only\n")
            self.assertEqual(evidence.verify_entry_evidence(linked_entry)["state"], "dirty_worktree")
            self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "fresh")

    def test_failed_object_fact_is_not_cached(self):
        original = evidence._run_git
        failed = False
        def run(repo, *args):
            nonlocal failed
            if args[0] == "cat-file" and not failed:
                failed = True
                return 128, "", "temporary failure"
            return original(repo, *args)
        with patch.object(evidence, "_run_git", side_effect=run), evidence.verification_scope():
            self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "diverged")
            self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "fresh")

    def test_shallow_boundary_invalidates_ancestry_fact(self):
        (self.repo / "unrelated.txt").write_text("extra\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "unrelated")
        with evidence.verification_scope():
            self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "fresh")
            (self.repo / ".git/shallow").write_text(git(self.repo, "rev-parse", "HEAD") + "\n")
            self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "diverged")

    def test_detached_checkout_and_context_exception_cleanup(self):
        head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "--detach", head)
        branch, observed = evidence._current_checkout(self.repo)
        self.assertEqual((branch, observed), ("detached-" + head[:12], head))
        detached = entry(self.repo)
        with evidence.verification_scope():
            self.assertEqual(evidence.verify_entry_evidence(detached)["state"], "fresh")
        with self.assertRaises(RuntimeError):
            with evidence.verification_scope():
                raise RuntimeError("synthetic")
        self.assertIsNone(evidence._FACTS.get())

    def test_tag_named_like_branch_keeps_branch_identity(self):
        git(self.repo, "tag", "main")
        self.assertEqual(evidence._current_checkout(self.repo)[0], "main")
        self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "fresh")

    def test_replacement_refs_disable_object_fact_cache(self):
        initial = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "unrelated.txt").write_text("extra\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "unrelated")
        git(self.repo, "replace", initial, git(self.repo, "rev-parse", "HEAD"))
        with patch.object(evidence, "_run_git", wraps=evidence._run_git) as calls, evidence.verification_scope():
            self.assertEqual(evidence._commit_exists(self.repo, initial)[0], 0)
            self.assertEqual(evidence._commit_exists(self.repo, initial)[0], 0)
        self.assertEqual(sum(call.args[1] == "cat-file" for call in calls.call_args_list), 2)

    def test_conflict_and_renamed_evidence_are_not_fresh(self):
        git(self.repo, "switch", "-c", "other")
        (self.repo / "evidence file.txt").write_text("other\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "other")
        git(self.repo, "switch", "main")
        (self.repo / "evidence file.txt").write_text("main\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "main")
        result = subprocess.run(["git", "merge", "other"], cwd=self.repo, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        with evidence.verification_scope():
            self.assertEqual(evidence.verify_entry_evidence(self.entry)["state"], "conflicted")
        git(self.repo, "merge", "--abort")
        git(self.repo, "mv", "evidence file.txt", "renamed file.txt")
        git(self.repo, "commit", "-m", "rename")
        self.assertNotEqual(evidence.verify_entry_evidence(self.entry)["state"], "fresh")


class SubjectRetrievalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="kb-subject-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "data"
        config = self.directory / "config.json"
        config.write_text(json.dumps({"storage": {"root": str(self.root), "records": "records",
                                                "runtime": "runtime", "cache": "cache"},
                                      "search": {"expand_queries": False}}))
        self.env = {"PERSONAL_KB_ROOT": str(self.root), "PERSONAL_KB_CONFIG": str(config)}
        rows = [
            {"id": "business", "repo": "business", "title": "数字人自由对话 4 秒与 96 token",
             "aliases": ["数字人", "96 token", "自由对话"], "trigger_terms": ["token", "对话"]},
            {"id": "codex", "repo": "tools", "title": "Codex 上下文压缩与 token 成本",
             "aliases": ["上下文压缩"], "trigger_terms": ["Codex", "token"]},
            {"id": "actor-only", "repo": "tools", "title": "Codex MCP 安装与配置",
             "aliases": ["Codex"], "trigger_terms": ["Codex", "MCP"]},
            {"id": "shared", "repo": "another", "title": "上下文压缩保留当前目标和任务约束",
             "aliases": ["上下文压缩"], "trigger_terms": ["上下文压缩"]},
            {"id": "wsl", "repo": "another", "title": "WSL 代理连接诊断",
             "aliases": ["WSL"], "trigger_terms": ["WSL", "代理连接"]},
        ]
        for row in rows:
            row.update(kind="experience", branch="main", ts="2026-10-21T00:00:00", story="Synthetic verified troubleshooting context")
            path = self.root / "records" / row["repo"] / "main/kb.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def retrieve(self, query, *options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = kb.main(["retrieve", query, "--global", "--no-domain", "--no-session-briefs",
                            "--no-plan", "--limit", "10", "--json", *options])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        return {item["entry_id"] for item in json.loads(stdout.getvalue())["items"]}

    def test_codex_subject_rejects_generic_business_overlap(self):
        hits = self.retrieve("Codex 对话 token 上下文压缩 Luna 主模型 子agent 分工 重复审计")
        self.assertNotIn("business", hits)
        self.assertNotIn("actor-only", hits)
        self.assertIn("codex", hits)
        self.assertIn("shared", hits)

    def test_business_positive_is_retained(self):
        self.assertIn("business", self.retrieve("数字人 自由对话 96 token 限制"))

    def test_cross_project_technical_experience_is_retained(self):
        self.assertIn("wsl", self.retrieve("Codex WSL 代理连接诊断"))

    def test_product_as_execution_role_does_not_restrict_business(self):
        query = "用 Codex 帮我查数字人 96 token 限制"
        self.assertEqual(rag._query_subjects(query), [])
        self.assertIn("business", self.retrieve(query))

    def test_multiple_explicit_topics_are_retained(self):
        hits = self.retrieve("Codex 上下文压缩；另外数字人 96 token 限制")
        self.assertTrue({"codex", "business"}.issubset(hits))

    def test_unspecified_subject_keeps_generic_retrieval(self):
        self.assertIn("business", self.retrieve("token 对话"))

    def test_storage_path_is_not_a_product_subject(self):
        row = {"title": "数字人 token 对话", "source_paths": [str(self.root / ".codex" / "business.py")]}
        self.assertFalse(rag._subject_compatible(row, "Codex token 对话", ["codex"], []))

    def test_product_name_alone_is_not_enough_for_a_token_query(self):
        hits = self.retrieve("Codex token")
        self.assertIn("codex", hits)
        self.assertNotIn("actor-only", hits)

    def test_explicit_weak_diagnostic_mode_can_inspect_rejected_candidates(self):
        self.assertIn("business", self.retrieve("Codex token 对话", "--include-weak"))

    def test_subject_is_preserved_and_role_is_not(self):
        self.assertIn("codex", rag._concrete_query_terms("Codex 上下文压缩"))
        self.assertNotIn("codex", rag._concrete_query_terms("用 Codex 排查 TTS"))


def main():
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(case)
                               for case in (OptimizationTests, SubjectRetrievalTests))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(_TEST_GUARD.run(main) if _TEST_GUARD else main())
