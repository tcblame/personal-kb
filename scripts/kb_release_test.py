from __future__ import annotations

import json
import importlib.util
import sys
import tempfile
from pathlib import Path

import kb_test_guard

_TEST_GUARD = kb_test_guard.activate(__file__) if __name__ == "__main__" else None

import kb_release


def _source(root: Path, *, script_text: str = "print('ok')\n") -> Path:
    source = root / "workspace" / "skills" / "personal-kb"
    (source / "references").mkdir(parents=True)
    (source / "backend").mkdir()
    (source / "scripts").mkdir()
    (source / "agents").mkdir()
    (source / "SKILL.md").write_text("---\nname: personal-kb\ndescription: test\n---\n", encoding="utf-8")
    (source / "agents" / "openai.yaml").write_text(
        'interface:\n  display_name: "Personal KB"\n'
        '  short_description: "A public test Skill"\n'
        '  default_prompt: "Use $personal-kb for this task."\n',
        encoding="utf-8",
    )
    (source / "references" / "retrieval.md").write_text("# Retrieval\n", encoding="utf-8")
    (source / "backend" / "index.py").write_text("# cache\n", encoding="utf-8")
    (source / "scripts" / "kb.py").write_text(script_text, encoding="utf-8")
    (source / "scripts" / "kb_demo_test.py").write_text("raise AssertionError\n", encoding="utf-8")
    (source / "config.json").write_text('{"storage":{"root":"/private/data"}}\n', encoding="utf-8")
    publishing = root / "workspace" / "docs" / "req" / "001-personal-kb-taxonomy" / "publishing"
    publishing.mkdir(parents=True)
    (publishing / "GITHUB_GITIGNORE").write_text("config.json\npersonal-kb-data/\n", encoding="utf-8")
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
        assert kb_release._release_state_path(output).is_file()
        assert not (output / kb_release.LEGACY_RELEASE_MARKER).exists()
        assert kb_release._root_layout_findings(output) == []
        assert (output / "config.example.json").is_file()
        assert (output / "metrics.example.json").is_file()
        assert (output / "LICENSE").read_text(encoding="utf-8") == "Test-only license fixture\n"
        assert (output / "docs" / "req" / "001-personal-kb-taxonomy" / "evals" / "runtime-preflight-cases.json").is_file()
        assert (output / "references" / "evals" / "runtime-preflight-cases.json").is_file()
        assert not (output / "config.json").exists()
        assert not (output / "scripts" / "kb_demo_test.py").exists()
        assert not (output / "skills").exists()
        assert not any("__pycache__" in path.parts for path in output.rglob("*"))

        public_regenerated = root / "release-from-public-root"
        kb_release.build_release(output, public_regenerated)
        assert _file_list(output) == _file_list(public_regenerated)


def test_default_output_supports_source_and_public_root_layouts() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = _source(root)
        assert kb_release._default_output(source) == root / "personal-kb-release"
        public_root = root / "public-personal-kb"
        assert kb_release._default_output(public_root) == root / "public-personal-kb-release"


def _file_list(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def _metadata_fingerprint(output: Path) -> dict[str, tuple[int, int, str]]:
    paths = [path for path in output.rglob("*") if path.is_file()]
    state = kb_release._release_state_path(output)
    if state.is_file():
        paths.append(state)
    return {
        str(path): (path.stat().st_size, path.stat().st_mtime_ns, kb_release._sha256_file(path))
        for path in paths
    }


def test_release_check_is_read_only_and_reports_differences() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = _source(root)
        output = root / "release"
        kb_release.build_release(source, output)
        before = _metadata_fingerprint(output)

        matching = kb_release.check_release(source, output)
        assert matching["matches"] is True
        assert matching["read_only"] is True
        assert _metadata_fingerprint(output) == before

        (source / "scripts" / "kb.py").write_text("print('changed')\n", encoding="utf-8")
        different = kb_release.check_release(source, output)
        assert different["matches"] is False
        assert different["changed_files"] == ["scripts/kb.py"]
        assert _metadata_fingerprint(output) == before


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


def test_missing_license_is_refused() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = _source(root)
        license_path = root / "workspace" / "docs" / "req" / "001-personal-kb-taxonomy" / "publishing" / "LICENSE"
        license_path.unlink()

        try:
            kb_release.build_release(source, root / "release")
        except ValueError as exc:
            assert "missing LICENSE" in str(exc)
        else:
            raise AssertionError("release without LICENSE was accepted")


def test_unknown_entry_in_owned_output_is_refused() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = _source(root)
        output = root / "release"
        kb_release.build_release(source, output)
        (output / "user-file.txt").write_text("keep me\n", encoding="utf-8")
        try:
            kb_release.build_release(source, output)
        except ValueError as exc:
            assert "unknown entries" in str(exc)
        else:
            raise AssertionError("unknown owned-output entry was overwritten")
        assert (output / "user-file.txt").read_text(encoding="utf-8") == "keep me\n"


def test_legacy_nested_release_is_migrated_to_root_layout() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = _source(root)
        output = root / "release"
        nested_skill = output / "skills" / "personal-kb"
        nested_skill.mkdir(parents=True)
        (nested_skill / "SKILL.md").write_text("legacy\n", encoding="utf-8")
        (output / kb_release.LEGACY_RELEASE_MARKER).write_text(
            json.dumps(
                {
                    "generated_by": "personal-kb-release",
                    "schema": "personal-kb.release-manifest/v1",
                    "files": ["skills/personal-kb/SKILL.md"],
                }
            ),
            encoding="utf-8",
        )

        kb_release.build_release(source, output)

        assert kb_release._root_layout_findings(output) == []
        assert not (output / "skills").exists()
        assert not (output / kb_release.LEGACY_RELEASE_MARKER).exists()
        assert kb_release._release_state_path(output).is_file()


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

        exported_path = output / "scripts" / "kb_release.py"
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
        assert exported._root_layout_findings(output) == []


def main() -> int:
    test_allowlist_export_is_repeatable_and_excludes_local_data()
    test_default_output_supports_source_and_public_root_layouts()
    test_release_check_is_read_only_and_reports_differences()
    test_unknown_nonempty_output_is_refused()
    test_missing_license_is_refused()
    test_unknown_entry_in_owned_output_is_refused()
    test_legacy_nested_release_is_migrated_to_root_layout()
    test_release_scan_rejects_credential_shaped_tokens()
    test_release_scan_rejects_generic_credentials()
    test_exported_release_scanner_accepts_public_placeholders()
    print("kb_release tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_TEST_GUARD.run(main) if _TEST_GUARD else main())
