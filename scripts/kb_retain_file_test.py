#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import kb_retain_file


def call_main(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            rc = kb_retain_file.main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    return rc, stdout.getvalue(), stderr.getvalue()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_retain_defaults_to_copy_and_writes_manifests() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        src = root / "error.log"
        src.write_bytes(b"boom\n")

        with patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(root / ".codex" / "personal-kb")}):
            rc, out, err = call_main(
                [
                    "retain",
                    "--path",
                    str(src),
                    "--project-key",
                    "study",
                    "--case-id",
                    "issue-login-500-authfilter-260615",
                    "--category",
                    "logs",
                    "--reason",
                    "线上 500 原始异常日志",
                ]
            )

        assert rc == 0, err
        summary = json.loads(out)
        stored = Path(summary["stored_path"])
        assert summary["mode"] == "copy"
        assert summary["category"] == "logs"
        assert summary["asset_id"].startswith("asset_260615_")
        assert src.exists()
        assert stored.exists()
        assert stored.read_bytes() == b"boom\n"
        assert summary["sha256"] == hashlib.sha256(b"boom\n").hexdigest()

        case_dir = root / ".codex" / "personal-kb" / "retained-files" / "study" / "2026" / "issue-login-500-authfilter-260615"
        case_manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))
        assert case_manifest["project_key"] == "study"
        assert case_manifest["case_id"] == "issue-login-500-authfilter-260615"
        assert case_manifest["assets"][0]["stored_path"] == str(stored)

        global_manifest = root / ".codex" / "personal-kb" / "manifests" / "retained-files.jsonl"
        rows = read_jsonl(global_manifest)
        assert rows[0]["asset_id"] == summary["asset_id"]
        assert rows[0]["origin_path"] == str(src)
        assert rows[0]["status"] == "active"


def test_retain_move_removes_original_file() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        src = root / "prod.yml"
        src.write_bytes(b"server: prod\n")

        with patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(root / ".codex" / "personal-kb")}):
            rc, out, err = call_main(
                [
                    "retain",
                    "--path",
                    str(src),
                    "--project-key",
                    "study",
                    "--case-id",
                    "issue-login-500-authfilter-260615",
                    "--category",
                    "configs",
                    "--mode",
                    "move",
                ]
            )

        assert rc == 0, err
        summary = json.loads(out)
        assert summary["mode"] == "move"
        assert not src.exists()
        assert Path(summary["stored_path"]).read_bytes() == b"server: prod\n"


def test_existing_target_is_not_overwritten() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        src1 = root / "error.log"
        src2_dir = root / "other"
        src2_dir.mkdir()
        src2 = src2_dir / "error.log"
        src1.write_bytes(b"first\n")
        src2.write_bytes(b"second\n")

        args = [
            "retain",
            "--project-key",
            "study",
            "--case-id",
            "issue-login-500-authfilter-260615",
            "--category",
            "logs",
        ]
        with patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(root / ".codex" / "personal-kb")}):
            rc1, out1, err1 = call_main([*args, "--path", str(src1)])
            rc2, out2, err2 = call_main([*args, "--path", str(src2)])

        assert rc1 == 0, err1
        assert rc2 == 0, err2
        stored1 = Path(json.loads(out1)["stored_path"])
        stored2 = Path(json.loads(out2)["stored_path"])
        assert stored1 != stored2
        assert stored1.read_bytes() == b"first\n"
        assert stored2.read_bytes() == b"second\n"


def test_list_show_path_and_verify_commands() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        src = root / "result.txt"
        src.write_bytes(b"ok\n")
        case_id = "issue-login-500-authfilter-260615"

        with patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(root / ".codex" / "personal-kb")}):
            rc, out, err = call_main(
                [
                    "retain",
                    "--path",
                    str(src),
                    "--project-key",
                    "study",
                    "--case-id",
                    case_id,
                    "--category",
                    "verification",
                ]
            )
            assert rc == 0, err
            asset_id = json.loads(out)["asset_id"]

            rc, out, err = call_main(["list", "--project-key", "study", "--case-id", case_id])
            assert rc == 0, err
            listed = json.loads(out)
            assert listed["assets"][0]["asset_id"] == asset_id

            rc, out, err = call_main(["show", "--asset-id", asset_id])
            assert rc == 0, err
            shown = json.loads(out)
            assert shown["case_id"] == case_id

            rc, out, err = call_main(["path", "--project-key", "study", "--case-id", case_id])
            assert rc == 0, err
            assert Path(json.loads(out)["archive_path"]).name == case_id

            rc, out, err = call_main(["verify", "--project-key", "study", "--case-id", case_id])
            assert rc == 0, err
            verified = json.loads(out)
            assert verified["ok"] is True
            assert verified["assets"][0]["ok"] is True


def test_verify_detects_hash_mismatch() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        src = root / "result.txt"
        src.write_bytes(b"ok\n")
        case_id = "issue-login-500-authfilter-260615"

        with patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(root / ".codex" / "personal-kb")}):
            rc, out, err = call_main(
                [
                    "retain",
                    "--path",
                    str(src),
                    "--project-key",
                    "study",
                    "--case-id",
                    case_id,
                    "--category",
                    "verification",
                ]
            )
            assert rc == 0, err
            stored = Path(json.loads(out)["stored_path"])
            stored.write_bytes(b"changed\n")

            rc, out, err = call_main(["verify", "--project-key", "study", "--case-id", case_id])

        assert rc == 1
        verified = json.loads(out)
        assert verified["ok"] is False
        assert verified["assets"][0]["ok"] is False
        assert verified["assets"][0]["error"] == "sha256_mismatch"


def test_retain_preserves_large_sensitive_file_verbatim() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        src = root / "large-config.txt"
        credential_line = b"\nAPI_" + b"TOKEN=" + b"real-" + b"secret-value-123456\n"
        src.write_bytes(b"x" * (5 * 1024 * 1024) + credential_line)

        with patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(root / "personal-kb")}):
            rc, out, err = call_main([
                "retain", "--path", str(src), "--project-key", "study",
                "--case-id", "large-config-260817", "--category", "configs",
            ])

        assert rc == 0, err
        summary = json.loads(out)
        stored = Path(summary["stored_path"])
        assert stored.read_bytes() == src.read_bytes()
        assert summary["size_bytes"] == src.stat().st_size
        if os.name != "nt":
            assert stat.S_IMODE(stored.stat().st_mode) == 0o600


def test_reference_accepts_locator_and_rejects_pasted_secret() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        common = [
            "reference", "--project-key", "study", "--case-id", "resource-260817",
            "--reference-kind", "credential",
        ]
        with patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(root / "personal-kb")}):
            rc, out, err = call_main([*common, "--locator", "vault://personal-kb/mysql-prod"])
            assert rc == 0, err
            assert json.loads(out)["locator"] == "vault://personal-kb/mysql-prod"

            for secret in (
                "pass" + "word=" + "super" + "secret123",
                "Bear" + "er " + "abcdefghijklmnopqrstuvwxyz",
                "sk-" + "abcdefghijklmnopqrstuvwxyz123456",
            ):
                rc, out, err = call_main([*common, "--locator", secret])
                assert rc == 4
                assert not out
                assert "pasted secret" in err


def test_env_and_private_key_files_are_retained_verbatim() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        env_file = root / ".env"
        env_content = "PASS" + "WORD=local-only-value-123456\n"
        env_file.write_text(env_content, encoding="utf-8")
        private_file = root / "id_ed25519"
        private_content = (
            "-----BEGIN " + "OPENSSH PRIVATE KEY-----\n"
            "local-only-test-material\n"
            "-----END " + "OPENSSH PRIVATE KEY-----\n"
        )
        private_file.write_text(private_content, encoding="utf-8")
        with patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(root / "personal-kb")}):
            rc, out, err = call_main([
                "retain", "--path", str(env_file), "--project-key", "study",
                "--case-id", "env-260817", "--category", "configs",
            ])
            assert rc == 0, err
            stored_env = Path(json.loads(out)["stored_path"])
            assert stored_env.read_text(encoding="utf-8") == env_content

            rc, out, err = call_main([
                "retain", "--path", str(private_file), "--project-key", "study",
                "--case-id", "env-260817", "--category", "configs",
            ])
            assert rc == 0, err
            stored_private = Path(json.loads(out)["stored_path"])
            assert stored_private.read_text(encoding="utf-8") == private_content


def main() -> int:
    tests = [
        test_retain_defaults_to_copy_and_writes_manifests,
        test_retain_move_removes_original_file,
        test_existing_target_is_not_overwritten,
        test_list_show_path_and_verify_commands,
        test_verify_detects_hash_mismatch,
        test_retain_preserves_large_sensitive_file_verbatim,
        test_reference_accepts_locator_and_rejects_pasted_secret,
        test_env_and_private_key_files_are_retained_verbatim,
    ]
    for test in tests:
        test()
    print("kb_retain_file tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
