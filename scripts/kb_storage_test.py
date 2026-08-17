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

import kb_lib
import kb_retain_file


def _call_retain(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = kb_retain_file.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def test_single_root_layout_and_escape_guard() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "data"
        config = {
            "storage": {
                "root": str(root),
                "records": "records",
                "retained_files": "evidence",
                "manifests": "manifests",
                "runtime": "runtime",
                "cache": "cache",
            }
        }
        with patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(root)}, clear=False), patch.object(kb_lib, "load_config", return_value=config):
            layout = kb_lib.storage_layout()
            assert layout.root == root.resolve()
            assert layout.records == root.resolve() / "records"
            assert layout.retained_files == root.resolve() / "evidence"
            assert kb_lib.runtime_file("closeout.jsonl") == root.resolve() / "runtime" / "closeout.jsonl"
            assert kb_lib.cache_dir_for_records(layout.records) == root.resolve() / "cache"

            bad = {**config, "storage": {**config["storage"], "cache": "../outside"}}
            with patch.object(kb_lib, "load_config", return_value=bad):
                try:
                    kb_lib.storage_layout()
                except kb_lib.PersonalKbConfigError:
                    pass
                else:
                    raise AssertionError("storage path escape was accepted")


def test_explicit_missing_config_is_an_error() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        missing = Path(temp_dir) / "missing.json"
        with patch.dict(os.environ, {"PERSONAL_KB_CONFIG": str(missing)}, clear=False):
            try:
                kb_lib.load_config()
            except kb_lib.PersonalKbConfigError as exc:
                assert "PERSONAL_KB_CONFIG" in str(exc)
            else:
                raise AssertionError("explicit missing config was silently accepted")


def test_same_content_is_reused_across_cases() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        first = root / "first.log"
        second = root / "second.log"
        first.write_bytes(b"same evidence\n")
        second.write_bytes(b"same evidence\n")
        kb_root = root / "personal-kb"
        env = {"PERSONAL_KB_ROOT": str(kb_root)}
        args = ["retain", "--project-key", "study", "--category", "logs", "--mode", "copy"]
        with patch.dict(os.environ, env, clear=False):
            rc1, out1, err1 = _call_retain([*args, "--case-id", "case-one-260816", "--path", str(first)])
            rc2, out2, err2 = _call_retain([*args, "--case-id", "case-two-260816", "--path", str(second)])
        assert rc1 == 0, err1
        assert rc2 == 0, err2
        first_summary = json.loads(out1)
        second_summary = json.loads(out2)
        assert first_summary["storage_action"] == "copied"
        assert second_summary["storage_action"] == "reused"
        assert second_summary["deduplicated_from_asset"] == first_summary["asset_id"]
        assert second_summary["stored_path"] == first_summary["stored_path"]


def test_external_reference_never_copies_secret_value() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        kb_root = Path(temp_dir) / "personal-kb"
        env = {"PERSONAL_KB_ROOT": str(kb_root)}
        with patch.dict(os.environ, env, clear=False):
            rc, out, err = _call_retain(
                [
                    "reference",
                    "--project-key",
                    "study",
                    "--case-id",
                    "db-access-260816",
                    "--reference-kind",
                    "credential",
                    "--locator",
                    "vault://personal-kb/study-db",
                ]
            )
            assert rc == 0, err
            payload = json.loads(out)
            assert payload["reference_kind"] == "credential"
            assert "stored_path" not in payload

            rc, _out, err = _call_retain(
                [
                    "reference",
                    "--project-key",
                    "study",
                    "--case-id",
                    "db-access-260816",
                    "--reference-kind",
                    "credential",
                    "--locator",
                    "pass" + "word=" + "real-" + "secret-value",
                ]
            )
            assert rc == 4
            assert "secret" in err.lower()


def test_active_credentials_are_retained_verbatim_locally() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        kb_root = root / "personal-kb"
        private_key = root / "id_ed25519"
        private_key.write_text(
            "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nnot-a-real-key\n"
            "-----END OPENSSH " + "PRIVATE KEY-----\n",
            encoding="utf-8",
        )
        env_file = root / ".env"
        env_file.write_text("DATABASE_" + "PASSWORD=" + "actual-" + "password-value\n", encoding="utf-8")
        args = [
            "retain",
            "--project-key",
            "study",
            "--case-id",
            "credentials-260816",
            "--category",
            "configs",
        ]
        with patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(kb_root)}, clear=False):
            key_rc, key_out, key_err = _call_retain([*args, "--path", str(private_key)])
            env_rc, env_out, env_err = _call_retain([*args, "--path", str(env_file)])

        assert key_rc == 0, key_err
        assert env_rc == 0, env_err
        key_summary = json.loads(key_out)
        env_summary = json.loads(env_out)
        assert Path(key_summary["stored_path"]).read_bytes() == private_key.read_bytes()
        assert Path(env_summary["stored_path"]).read_bytes() == env_file.read_bytes()
        assert key_summary["local_only"] is True
        assert env_summary["storage_protection"] == "filesystem-permissions-only"


def main() -> int:
    test_single_root_layout_and_escape_guard()
    test_explicit_missing_config_is_an_error()
    test_same_content_is_reused_across_cases()
    test_external_reference_never_copies_secret_value()
    test_active_credentials_are_retained_verbatim_locally()
    print("kb_storage tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_TEST_GUARD.run(main) if _TEST_GUARD else main())
