from __future__ import annotations

import tempfile
import importlib.util
import sys
from pathlib import Path

import kb_release


def _source(root: Path, *, script_text: str = "print('ok')\n") -> Path:
    source = root / "workspace" / "skills" / "personal-kb"
    (source / "references").mkdir(parents=True)
    (source / "backend").mkdir()
    (source / "scripts").mkdir()
    (source / "SKILL.md").write_text("---\nname: personal-kb\ndescription: test\n---\n", encoding="utf-8")
    (source / "references" / "retrieval.md").write_text("# Retrieval\n", encoding="utf-8")
    (source / "backend" / "index.py").write_text("# cache\n", encoding="utf-8")
    (source / "scripts" / "kb.py").write_text(script_text, encoding="utf-8")
    (source / "scripts" / "kb_demo_test.py").write_text("raise AssertionError\n", encoding="utf-8")
    (source / "config.json").write_text('{"storage":{"root":"/private/data"}}\n', encoding="utf-8")
    publishing = root / "workspace" / "docs" / "req" / "001-personal-kb-taxonomy" / "publishing"
    publishing.mkdir(parents=True)
    (publishing / "config.example.json").write_text('{"storage":{"root":"${PERSONAL_KB_ROOT}"}}\n', encoding="utf-8")
    (publishing / "GITHUB_README.md").write_text("# Public README\n", encoding="utf-8")
    (publishing / "metrics.example.json").write_text('{"metric":"example"}\n', encoding="utf-8")
    (publishing / "LICENSE").write_text("Test-only license fixture\n", encoding="utf-8")
    evals = root / "workspace" / "docs" / "req" / "001-personal-kb-taxonomy" / "evals"
    evals.mkdir(parents=True)
    (evals / "runtime-preflight-cases.json").write_text('{"cases":[]}\n', encoding="utf-8")
    (evals / "runtime-preflight-gold.json").write_text('{"cases":[]}\n', encoding="utf-8")
    return source


def test_allowlist_export_is_repeatable_and_excludes_local_data() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = _source(root)
        output = root / "release"

        first = kb_release.build_release(source, output)
        second = kb_release.build_release(source, output)

        assert first["status"] == "ok"
        assert second["status"] == "ok"
        assert (output / kb_release.RELEASE_MARKER).is_file()
        assert (output / "skills" / "personal-kb" / "config.example.json").is_file()
        assert (output / "metrics.example.json").is_file()
        assert (output / "LICENSE").read_text(encoding="utf-8") == "Test-only license fixture\n"
        assert (output / "docs" / "req" / "001-personal-kb-taxonomy" / "evals" / "runtime-preflight-cases.json").is_file()
        assert (output / "skills" / "personal-kb" / "references" / "evals" / "runtime-preflight-cases.json").is_file()
        assert not (output / "skills" / "personal-kb" / "config.json").exists()
        assert not (output / "skills" / "personal-kb" / "scripts" / "kb_demo_test.py").exists()


def test_unknown_nonempty_output_is_refused() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = _source(root)
        output = root / "release"
        output.mkdir()
        (output / "user-file.txt").write_text("keep me\n", encoding="utf-8")
        try:
            kb_release.build_release(source, output)
        except ValueError as exc:
            assert "refusing to overwrite" in str(exc)
        else:
            raise AssertionError("unknown non-empty output was overwritten")
        assert (output / "user-file.txt").read_text(encoding="utf-8") == "keep me\n"


def test_release_scan_rejects_credential_shaped_tokens() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        token = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        source = _source(root, script_text=f"TOKEN = '{token}'\n")
        try:
            kb_release.build_release(source, root / "release")
        except ValueError as exc:
            assert "credential-shaped token" in str(exc)
        else:
            raise AssertionError("credential-shaped release content was accepted")


def test_release_scan_rejects_generic_credentials() -> None:
    samples = (
        "PASS" + "WORD = '" + "super" + "secret123'\n",
        "DATABASE_" + "PASSWORD = '" + "super" + "secret123'\n",
        "AUTH = 'Bear" + "er " + "abcdefghijklmnopqrstuvwxyz'\n",
        "URL = 'mysql://demo:" + "super" + "secret123@example.invalid/db'\n",
    )
    for script_text in samples:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = _source(root, script_text=script_text)
            try:
                kb_release.build_release(source, root / "release")
            except ValueError as exc:
                assert "sensitive content" in str(exc)
            else:
                raise AssertionError("generic credential-shaped release content was accepted")


def test_exported_release_scanner_accepts_public_placeholders() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = _source(root)
        scripts = source / "scripts"
        scripts.joinpath("kb_release.py").write_text(Path(kb_release.__file__).read_text(encoding="utf-8"), encoding="utf-8")
        scripts.joinpath("kb_sensitive_scan.py").write_text(
            Path(kb_release.__file__).with_name("kb_sensitive_scan.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        output = root / "release"
        kb_release.build_release(source, output)

        exported_path = output / "skills" / "personal-kb" / "scripts" / "kb_release.py"
        spec = importlib.util.spec_from_file_location("exported_kb_release", exported_path)
        assert spec is not None and spec.loader is not None
        exported = importlib.util.module_from_spec(spec)
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(exported)
        finally:
            sys.dont_write_bytecode = previous
        assert exported._scan_release(output) == []


def main() -> int:
    test_allowlist_export_is_repeatable_and_excludes_local_data()
    test_unknown_nonempty_output_is_refused()
    test_release_scan_rejects_credential_shaped_tokens()
    test_release_scan_rejects_generic_credentials()
    test_exported_release_scanner_accepts_public_placeholders()
    print("kb_release tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
