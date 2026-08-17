#!/usr/bin/env python3
"""Build a clean, allowlisted public Personal KB Skill tree.

The exporter never reads the durable KB as publishable content and never
overwrites a non-empty destination. It is intentionally conservative: a
release failure is safer than silently shipping local paths or corpus data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from kb_sensitive_scan import sensitive_findings


KNOWN_INTERNAL_TERMS = (
    "example-user",
    "example-user",
    "example-project-a",
    "example-project-b",
    "example-project-c",
    "example-project-d",
    "example-domain",
    "example-project-a",
    "example-project-b",
    "example-project-c",
    "example-project-d",
    "example-domain",
    "example-branch-a",
    "example-branch-b",
    "example-art-project",
)
PUBLIC_PLACEHOLDER_TERMS = frozenset(
    {
        "example-user",
        "example-project-a",
        "example-project-b",
        "example-project-c",
        "example-project-d",
        "example-domain",
        "example-branch-a",
        "example-branch-b",
        "example-art-project",
    }
)
LOCAL_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])/(?:home|Users|mnt|private/var)/[^\s'\"`]+|"
    r"\b[A-Za-z]:[\\/][^\s'\"`]+"
)
TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|"
    r"sk-[A-Za-z0-9_-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b"
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s*\n"
    r"[A-Za-z0-9+/=\s]{32,}\n-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
LEGACY_RELEASE_MARKER = ".personal-kb-release.json"
RELEASE_STATE_SCHEMA = "personal-kb.release-state/v2"
FORBIDDEN_DATA_DIRECTORIES = {
    "cache",
    "manifests",
    "personal-kb-data",
    "retained-files",
    "runtime",
    "storage",
}
PUBLIC_TESTS = {
    "kb_smoke_test.py",
    "kb_storage_test.py",
    "kb_challenge_test.py",
    "kb_retain_file_test.py",
    "kb_release_test.py",
}


def _default_output(source: Path) -> Path:
    if source.parent.name == "skills":
        # <workspace>/skills/personal-kb -> <workspace>/../personal-kb-release
        workspace = source.parents[1]
        return workspace.parent / "personal-kb-release"
    # Public repository root -> a sibling release tree, never a filesystem root.
    return source.parent / f"{source.name}-release"


def _release_state_path(output: Path) -> Path:
    return output.parent / f".{output.name}.personal-kb-release-state.json"


def _public_replacements(text: str) -> str:
    replacements = {
        "example supervision": "example supervision",
        "example-project-a": "example-project-a",
        "example-project-b": "example-project-b",
        "example-project-c": "example-project-c",
        "example-project-d": "example-project-d",
        "example-domain": "example-domain",
        "example-project-a": "example-project-a",
        "example-project-b": "example-project-b",
        "example-project-c": "example-project-c",
        "example-project-d": "example-project-d",
        "example-domain": "example-domain",
        "example-branch-a": "example-branch-a",
        "example-branch-b": "example-branch-b",
        "example-user": "example-user",
        "<local-home>": "<local-home>",
        "<local-home>": "<local-home>",
        "example-user": "example-user",
        "example-art-project": "example-art-project",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return LOCAL_ABSOLUTE_PATH_RE.sub("<local-path>", text)


def _copy_text(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_public_replacements(source.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _first_file(*candidates: Path | None) -> Path | None:
    return next((path for path in candidates if path is not None and path.is_file()), None)


def _copy_tree_allowlisted(source: Path, staging: Path) -> list[str]:
    copied: list[str] = []
    skill_target = staging
    _copy_text(source / "SKILL.md", skill_target / "SKILL.md")
    copied.append("SKILL.md")

    for directory_name in ("references", "backend"):
        source_dir = source / directory_name
        target_dir = skill_target / directory_name
        for path in sorted(source_dir.iterdir()):
            if not path.is_file() or path.suffix not in {".md", ".json", ".py"}:
                continue
            _copy_text(path, target_dir / path.name)
            copied.append(str(Path(directory_name) / path.name))

    source_scripts = source / "scripts"
    target_scripts = skill_target / "scripts"
    for path in sorted(source_scripts.glob("*.py")):
        # Only tests exercised by the public wrapper are exported. The larger
        # local regression corpus contains project-specific replay fixtures.
        if (path.name.endswith("_test.py") or path.name.startswith("test_")) and path.name not in PUBLIC_TESTS:
            continue
        _copy_text(path, target_scripts / path.name)
        copied.append(str(Path("scripts") / path.name))

    workspace_root = source.parents[1] if source.parent.name == "skills" else None
    requirement_dir = (
        workspace_root / "docs" / "req" / "001-personal-kb-taxonomy"
        if workspace_root is not None
        else None
    )
    publishing_dir = requirement_dir / "publishing" if requirement_dir is not None else None

    agent_source = _first_file(source / "agents" / "openai.yaml")
    if agent_source is None:
        raise ValueError("Skill source is missing agents/openai.yaml")
    _copy_text(agent_source, staging / "agents" / "openai.yaml")
    copied.append("agents/openai.yaml")

    gitignore_source = _first_file(
        publishing_dir / "GITHUB_GITIGNORE" if publishing_dir is not None else None,
        source / ".gitignore",
    )
    if gitignore_source is None:
        raise ValueError("public release source is missing GITHUB_GITIGNORE or .gitignore")
    _copy_text(gitignore_source, staging / ".gitignore")
    copied.append(".gitignore")

    config_source = _first_file(
        publishing_dir / "config.example.json" if publishing_dir is not None else None,
        source / "config.example.json",
    )
    if config_source is not None:
        _copy_text(config_source, skill_target / "config.example.json")
    else:
        config = {
            "storage": {
                "root": "${PERSONAL_KB_ROOT}",
                "records": "records",
                "retained_files": "retained-files",
                "manifests": "manifests",
                "runtime": "runtime",
                "cache": "cache",
            },
            "runtime": {"mode": "normal"},
            "challenge": {"success_sample_rate": 0.1, "max_adopted_entries": 3, "critique_depth": 1},
        }
        (skill_target / "config.example.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    copied.append("config.example.json")

    readme_source = _first_file(
        publishing_dir / "GITHUB_README.md" if publishing_dir is not None else None,
        source / "README.md",
    )
    if readme_source is not None:
        _copy_text(readme_source, staging / "README.md")
        copied.append("README.md")

    metrics_source = _first_file(
        publishing_dir / "metrics.example.json" if publishing_dir is not None else None,
        source / "metrics.example.json",
    )
    if metrics_source is not None:
        _copy_text(metrics_source, staging / "metrics.example.json")
        copied.append("metrics.example.json")

    license_source = _first_file(
        publishing_dir / "LICENSE" if publishing_dir is not None else None,
        source / "LICENSE",
    )
    if license_source is None:
        raise ValueError("public release source is missing LICENSE")
    _copy_text(license_source, staging / "LICENSE")
    copied.append("LICENSE")

    docs_target = staging / "docs"
    docs_target.mkdir(parents=True, exist_ok=True)
    decision_source = _first_file(
        requirement_dir / "decisions" / "2026-08-17-single-root-growing-veteran-memory.md"
        if requirement_dir is not None
        else None,
        source / "docs" / "single-root-growing-veteran-memory.md",
    )
    if decision_source is not None:
        _copy_text(decision_source, docs_target / "single-root-growing-veteran-memory.md")
        copied.append("docs/single-root-growing-veteran-memory.md")

    eval_source_dir = (
        requirement_dir / "evals"
        if requirement_dir is not None
        else source / "docs" / "req" / "001-personal-kb-taxonomy" / "evals"
    )
    eval_target_dir = docs_target / "req" / "001-personal-kb-taxonomy" / "evals"
    bundled_eval_target_dir = skill_target / "references" / "evals"
    for filename in ("runtime-preflight-cases.json", "runtime-preflight-gold.json"):
        eval_source = eval_source_dir / filename
        if eval_source.is_file():
            _copy_text(eval_source, eval_target_dir / filename)
            copied.append(str(Path("docs/req/001-personal-kb-taxonomy/evals") / filename))
            _copy_text(eval_source, bundled_eval_target_dir / filename)
            copied.append(str(Path("references/evals") / filename))
    return sorted(copied)


def _root_layout_findings(root: Path) -> list[str]:
    findings: list[str] = []
    required_files = (
        ".gitignore",
        "LICENSE",
        "README.md",
        "SKILL.md",
        "agents/openai.yaml",
        "config.example.json",
        "scripts/kb.py",
    )
    for relative in required_files:
        if not (root / relative).is_file():
            findings.append(f"missing root-layout file: {relative}")
    if (root / "skills").exists():
        findings.append("nested Skill directory is forbidden: skills/")
    if (root / LEGACY_RELEASE_MARKER).exists():
        findings.append(f"release manifest is forbidden in public tree: {LEGACY_RELEASE_MARKER}")
    return findings


def _scan_release(root: Path) -> list[str]:
    findings: list[str] = []
    forbidden_names = {
        LEGACY_RELEASE_MARKER,
        "config.json",
        "kb.jsonl",
        "manifest.json",
        "retained-files.jsonl",
    }
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(root)
        lowered_directories = {part.casefold() for part in relative.parts[:-1]}
        if path.is_symlink():
            findings.append(f"symlink is forbidden: {relative}")
            continue
        if (
            "__pycache__" in path.parts
            or lowered_directories & FORBIDDEN_DATA_DIRECTORIES
            or path.name in forbidden_names
            or path.suffix in {".jsonl", ".log", ".bak", ".pyc"}
        ):
            findings.append(f"forbidden data file: {path.relative_to(root)}")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            findings.append(f"unreadable file {path.relative_to(root)}: {exc}")
            continue
        if LOCAL_ABSOLUTE_PATH_RE.search(text):
            findings.append(f"absolute local path: {path.relative_to(root)}")
        if TOKEN_RE.search(text):
            findings.append(f"credential-shaped token: {path.relative_to(root)}")
        if PRIVATE_KEY_RE.search(text):
            findings.append(f"private key material: {path.relative_to(root)}")
        # The detector source necessarily contains its own credential patterns.
        # It still goes through token/private-key/path/internal-term checks above.
        if path.name != "kb_sensitive_scan.py":
            for finding_type in sensitive_findings(text):
                findings.append(f"sensitive content {finding_type}: {path.relative_to(root)}")
        for term in KNOWN_INTERNAL_TERMS:
            if term.casefold() in PUBLIC_PLACEHOLDER_TERMS:
                continue
            if term.casefold() in text.casefold():
                findings.append(f"internal term {term}: {path.relative_to(root)}")
                break
    return findings


def _load_owned_output_state(output: Path) -> dict[str, object] | None:
    if not output.is_dir() or not any(output.iterdir()):
        return None
    state_path = _release_state_path(output)
    legacy_path = output / LEGACY_RELEASE_MARKER
    metadata_path = state_path if state_path.is_file() else legacy_path
    if not metadata_path.is_file():
        raise ValueError(f"refusing to overwrite non-empty release directory: {output}")
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"known release state is invalid: {metadata_path}") from exc
    if not isinstance(payload, dict) or payload.get("generated_by") != "personal-kb-release":
        raise ValueError(f"release state is not owned by this exporter: {metadata_path}")
    files = payload.get("files")
    if not isinstance(files, list) or not all(isinstance(value, str) for value in files):
        raise ValueError(f"release state has an invalid files list: {metadata_path}")

    known_files: set[str] = set()
    for value in files:
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"release state contains an unsafe path: {value}")
        known_files.add(str(relative))
    known_directories = {
        str(parent)
        for value in known_files
        for parent in Path(value).parents
        if str(parent) != "."
    }
    unexpected: list[str] = []
    for path in output.rglob("*"):
        relative = path.relative_to(output)
        relative_text = str(relative)
        if relative_text == LEGACY_RELEASE_MARKER:
            continue
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            unexpected.append(relative_text)
        elif path.is_dir():
            if relative_text not in known_directories:
                unexpected.append(relative_text)
        elif relative_text not in known_files:
            unexpected.append(relative_text)
    if unexpected:
        raise ValueError(
            "refusing to overwrite release directory with unknown entries: "
            + ", ".join(sorted(unexpected)[:5])
        )
    return payload


def _install_release_tree(staging: Path, output: Path) -> None:
    if not output.exists():
        staging.rename(output)
        return
    backup = staging.with_name(f"{staging.name}-previous")
    output.rename(backup)
    try:
        staging.rename(output)
    except Exception:
        backup.rename(output)
        raise
    shutil.rmtree(backup)


def _write_release_state(output: Path, files: list[str]) -> Path:
    state_path = _release_state_path(output)
    payload = {
        "generated_by": "personal-kb-release",
        "schema": RELEASE_STATE_SCHEMA,
        "files": files,
    }
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(state_path.parent),
        prefix=f".{state_path.name}-",
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary_state = Path(handle.name)
    temporary_state.replace(state_path)
    return state_path


def build_release(source: Path, output: Path) -> dict[str, object]:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"Skill source is missing SKILL.md: {source}")
    if output == source or source in output.parents or output in source.parents:
        raise ValueError("release output must be outside the Skill source")
    if output.exists() and not output.is_dir():
        raise ValueError(f"release output is not a directory: {output}")
    _load_owned_output_state(output)

    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=str(parent)))
    try:
        copied = _copy_tree_allowlisted(source, staging)
        findings = [*_root_layout_findings(staging), *_scan_release(staging)]
        if findings:
            raise ValueError("public release scan failed:\n" + "\n".join(f"- {item}" for item in findings))
        _install_release_tree(staging, output)
        state_path = _write_release_state(output, copied)
        return {
            "status": "ok",
            "output": str(output),
            "state_file": str(state_path),
            "files": copied,
            "file_count": len(copied),
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _release_fingerprint(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        result[path.relative_to(root).as_posix()] = _sha256_file(path)
    return result


def check_release(source: Path, output: Path) -> dict[str, object]:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    before = _release_fingerprint(output)
    state_path = _release_state_path(output)
    state_before = _sha256_file(state_path) if state_path.is_file() else ""
    state_error = ""
    if output.exists():
        try:
            _load_owned_output_state(output)
        except ValueError as exc:
            state_error = str(exc)

    with tempfile.TemporaryDirectory(prefix="personal-kb-release-check-") as temp_dir:
        candidate = Path(temp_dir) / "release"
        build_release(source, candidate)
        expected = _release_fingerprint(candidate)

    after = _release_fingerprint(output)
    state_after = _sha256_file(state_path) if state_path.is_file() else ""
    if before != after or state_before != state_after:
        raise RuntimeError("release-check modified the formal release output")

    missing = sorted(set(expected) - set(after))
    extra = sorted(set(after) - set(expected))
    changed = sorted(
        name for name in set(expected).intersection(after)
        if expected[name] != after[name]
    )
    scan_findings = [] if not output.is_dir() else [
        *_root_layout_findings(output),
        *_scan_release(output),
    ]
    if not output.is_dir():
        scan_findings.append(f"release output is missing: {output}")
    matches = not (state_error or missing or extra or changed or scan_findings)
    return {
        "status": "ok" if matches else "different",
        "source": str(source),
        "output": str(output),
        "read_only": True,
        "matches": matches,
        "state_error": state_error,
        "missing_files": missing,
        "extra_files": extra,
        "changed_files": changed,
        "scan_findings": scan_findings,
        "expected_file_count": len(expected),
        "actual_file_count": len(after),
    }


def check_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only check of the formal Personal KB release tree.")
    parser.add_argument("--source", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--output", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    source = Path(args.source)
    output = Path(args.output) if args.output else _default_output(source.resolve())
    try:
        result = check_release(source, output)
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"release check failed: {exc}\n")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["matches"] else 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an allowlisted public Personal KB release tree.")
    parser.add_argument("--source", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--output", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    source = Path(args.source)
    output = Path(args.output) if args.output else _default_output(source.resolve())
    try:
        result = build_release(source, output)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"release build failed: {exc}\n")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
