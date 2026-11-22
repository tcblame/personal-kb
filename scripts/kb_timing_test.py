from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import kb_test_guard

_TEST_GUARD = kb_test_guard.activate(__file__) if __name__ == "__main__" else None

import kb
import kb_timing


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kb-timing-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "data"
        self.config = self.directory / "config.json"
        self.config.write_text(json.dumps({"storage": {
            "root": str(self.root), "records": "records", "runtime": "runtime",
            "cache": "cache", "manifests": "manifests", "retained_files": "evidence",
        }}), encoding="utf-8")
        self.environment = {**os.environ, "PERSONAL_KB_ROOT": str(self.root),
                            "PERSONAL_KB_CONFIG": str(self.config)}

    def call(self, *args):
        return subprocess.run([sys.executable, str(Path(kb.__file__)), *args],
                              cwd=self.directory, env=self.environment,
                              capture_output=True, text=True)

    def report(self, result):
        lines = [line for line in result.stderr.splitlines() if line.startswith("KB_TIMING ")]
        self.assertEqual(len(lines), 1, result.stderr)
        value = json.loads(lines[0][len("KB_TIMING "):])
        self.assertEqual(value["exit_code"], result.returncode)
        self.assertTrue(value["stages_are_inclusive"])
        self.assertGreaterEqual(value["elapsed_ms"], 0)
        self.assertNotIn(str(self.directory), lines[0])
        self.assertNotIn("PRIVATE_QUERY_SENTINEL", lines[0])
        return value

    def test_cli_retrieval_stdout_is_unchanged_and_read_only(self):
        bucket = self.root / "records/demo/main/kb.jsonl"
        bucket.parent.mkdir(parents=True)
        bucket.write_text(json.dumps({"id": "timing-fixture", "kind": "experience",
                                     "title": "PRIVATE_QUERY_SENTINEL", "ts": "2026-10-21T00:00:00",
                                     "story": "Synthetic timing fixture", "confidence": 0.9}) + "\n")
        before = (bucket.read_bytes(), bucket.stat().st_mtime_ns)
        args = ["retrieve", "PRIVATE_QUERY_SENTINEL", "--repo", "demo", "--branch", "main", "--json"]
        plain = self.call(*args)
        measured = self.call(*args, "--timings")
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(plain.stderr, "")
        self.assertEqual(measured.returncode, 0, measured.stderr)
        first, second = json.loads(plain.stdout), json.loads(measured.stdout)
        first.pop("retrieval_id"); second.pop("retrieval_id")
        self.assertEqual(first, second)
        self.assertEqual(first["hit_count"], 1)
        self.assertEqual(before, (bucket.read_bytes(), bucket.stat().st_mtime_ns))
        self.assertFalse((self.root / "runtime").exists())
        stages = self.report(measured)["stages"]
        self.assertIn("evidence.verify", stages)
        self.assertIn("retrieve.search_raw", stages)

    def test_invalid_value_and_argument_parse_failures_are_timed(self):
        for options in (["--min-confidence", "2"], ["--unknown-option"]):
            result = self.call("retrieve", "PRIVATE_QUERY_SENTINEL", "--json", "--timings", *options)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid_request")
            self.report(result)

    def test_receipt_failure_keeps_result_and_exit_code(self):
        receipt = self.directory / "receipt.json"
        receipt.write_text("existing")
        result = self.call("retrieve", "PRIVATE_QUERY_SENTINEL", "--repo", "demo", "--branch", "main",
                           "--json", "--receipt-output", str(receipt), "--timings")
        self.assertEqual(result.returncode, 5)
        self.assertEqual(json.loads(result.stdout)["receipt"]["error"]["code"], "receipt_write_failed")
        self.assertEqual(receipt.read_text(), "existing")
        self.assertIn("retrieve.receipt", self.report(result)["stages"])

    def test_real_closeout_write_and_silent_default(self):
        args = ["closeout", "--rag-calls", "0", "--repo", "demo", "--branch", "main", "--reason", "synthetic"]
        plain = self.call(*args, "--closeout-id", "plain")
        measured = self.call(*args, "--closeout-id", "measured", "--timings")
        self.assertEqual((plain.returncode, plain.stdout, plain.stderr), (0, "", ""))
        self.assertEqual((measured.returncode, measured.stdout), (0, ""))
        value = self.report(measured)
        for stage in ("closeout.build", "closeout.append", "storage.lock_wait", "storage.safety_check", "storage.write"):
            self.assertEqual(value["stages"][stage]["calls"], 1)
        rows = [json.loads(line) for line in (self.root / "runtime/closeout.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("stages" not in row for row in rows))

    def test_storage_failure_keeps_failure_and_reports_timing(self):
        import kb_closeout
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.environment), patch.object(kb_closeout, "append_jsonl", side_effect=OSError("synthetic write failure")), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = kb.main(["closeout", "--rag-calls", "0", "--repo", "demo", "--branch", "main", "--reason", "synthetic", "--timings"])
        self.assertEqual(code, 1)
        self.assertIn('"closeout_write_failed"', stderr.getvalue())
        report = self.report(subprocess.CompletedProcess([], code, stdout.getvalue(), stderr.getvalue()))
        self.assertIn("closeout.append", report["stages"])

    def test_nested_aggregation_and_context_cleanup(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with kb_timing.recording("test") as report:
                with kb_timing.span("outer"):
                    with kb_timing.git_span(["rev-parse", "PRIVATE_QUERY_SENTINEL"]):
                        kb_timing.count("synthetic")
                    with kb_timing.git_span(["rev-parse"]):
                        pass
                report.exit_code = 0
        payload = self.report(subprocess.CompletedProcess([], 0, "", stderr.getvalue()))
        self.assertEqual(payload["stages"]["git.rev-parse"]["calls"], 2)
        self.assertEqual(payload["counters"]["synthetic"], 1)
        with kb_timing.span("inactive"):
            pass
        self.assertNotIn("inactive", report.stages)

    def test_wrapper_rejection_is_timed(self):
        result = self.call("closeout", "--legacy-allow-unlinked-retrievals", "--timings")
        self.assertEqual(result.returncode, 2)
        self.assertIn("focused kb_closeout.py", result.stderr)
        self.report(result)

    def test_closed_stderr_does_not_mask_result_or_exception(self):
        closed = io.StringIO()
        closed.close()
        with contextlib.redirect_stderr(closed):
            with kb_timing.recording("test") as report:
                report.exit_code = 0
            with self.assertRaisesRegex(RuntimeError, "original failure"):
                with kb_timing.recording("test"):
                    raise RuntimeError("original failure")
        self.assertIsNone(kb_timing._ACTIVE.get())


def main():
    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(TimingTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(_TEST_GUARD.run(main) if _TEST_GUARD else main())
