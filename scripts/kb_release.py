#!/usr/bin/env python3
"""Build a clean, allowlisted public Personal KB Skill tree.

The exporter never reads the durable KB as publishable content and never
overwrites a non-empty destination. It is intentionally conservative: a
release failure is safer than silently shipping local paths or corpus data.
"""

from __future__ import annotations

import argparse
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
RELEASE_MARKER = ".personal-kb-release.json"
PUBLIC_TESTS = {
    "kb_smoke_test.py",
    "kb_storage_test.py",
    "kb_challenge_test.py",
    "kb_retain_file_test.py",
    "kb_release_test.py",
}


def _default_output(source: Path) -> Path:
    # <workspace>/skills/personal-kb -> <workspace>/../personal-kb-release
    workspace = source.parents[1]
    candidate = workspace.parent / "personal-kb-release"
    return candidate if candidate != workspace else workspace.parent / "personal-kb-release-export"


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


def _copy_tree_allowlisted(source: Path, staging: Path) -> list[str]:
    copied: list[str] = []
    skill_target = staging / "skills" / "personal-kb"
    _copy_text(source / "SKILL.md", skill_target / "SKILL.md")
    copied.append("skills/personal-kb/SKILL.md")

    for directory_name in ("references", "backend"):
        source_dir = source / directory_name
        target_dir = skill_target / directory_name
        for path in sorted(source_dir.iterdir()):
            if not path.is_file() or path.suffix not in {".md", ".json", ".py"}:
                continue
            _copy_text(path, target_dir / path.name)
            copied.append(str((Path("skills/personal-kb") / directory_name / path.name)))

    source_scripts = source / "scripts"
    target_scripts = skill_target / "scripts"
    for path in sorted(source_scripts.glob("*.py")):
        # Only tests exercised by the public wrapper are exported. The larger
        # local regression corpus contains project-specific replay fixtures.
        if (path.name.endswith("_test.py") or path.name.startswith("test_")) and path.name not in PUBLIC_TESTS:
            continue
        _copy_text(path, target_scripts / path.name)
        copied.append(str(Path("skills/personal-kb/scripts") / path.name))

    publishing_dir = source.parents[1] / "docs" / "req" / "001-personal-kb-taxonomy" / "publishing"
    config_source = publishing_dir / "config.example.json"
    if config_source.exists():
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
    copied.append("skills/personal-kb/config.example.json")

    readme_source = publishing_dir / "GITHUB_README.md"
    if readme_source.exists():
        _copy_text(readme_source, staging / "README.md")
        copied.append("README.md")

    metrics_source = publishing_dir / "metrics.example.json"
    if metrics_source.exists():
        _copy_text(metrics_source, staging / "metrics.example.json")
        copied.append("metrics.example.json")

    license_source = publishing_dir / "LICENSE"
    if license_source.exists():
        _copy_text(license_source, staging / "LICENSE")
        copied.append("LICENSE")

    docs_target = staging / "docs"
    docs_target.mkdir(parents=True, exist_ok=True)
    decision_source = source.parents[1] / "docs" / "req" / "001-personal-kb-taxonomy" / "decisions" / "2026-08-17-single-root-growing-veteran-memory.md"
    if decision_source.exists():
        _copy_text(decision_source, docs_target / "single-root-growing-veteran-memory.md")
        copied.append("docs/single-root-growing-veteran-memory.md")

    eval_source_dir = source.parents[1] / "docs" / "req" / "001-personal-kb-taxonomy" / "evals"
    eval_target_dir = docs_target / "req" / "001-personal-kb-taxonomy" / "evals"
    bundled_eval_target_dir = skill_target / "references" / "evals"
    for filename in ("runtime-preflight-cases.json", "runtime-preflight-gold.json"):
        eval_source = eval_source_dir / filename
        if eval_source.is_file():
            _copy_text(eval_source, eval_target_dir / filename)
            copied.append(str(Path("docs/req/001-personal-kb-taxonomy/evals") / filename))
            _copy_text(eval_source, bundled_eval_target_dir / filename)
            copied.append(str(Path("skills/personal-kb/references/evals") / filename))
    return copied


def _scan_release(root: Path) -> list[str]:
    findings: list[str] = []
    forbidden_names = {"config.json", "kb.jsonl", "manifest.json", "retained-files.jsonl"}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if "__pycache__" in path.parts or path.name in forbidden_names or path.suffix in {".jsonl", ".log", ".bak", ".pyc"}:
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


def build_release(source: Path, output: Path) -> dict[str, object]:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"Skill source is missing SKILL.md: {source}")
    if output == source or source in output.parents or output in source.parents:
        raise ValueError("release output must be outside the Skill source")
    if output.exists() and not output.is_dir():
        raise ValueError(f"release output is not a directory: {output}")
    existing_manifest: dict[str, object] | None = None
    if output.is_dir() and any(output.iterdir()):
        marker = output / RELEASE_MARKER
        if not marker.is_file():
            raise ValueError(f"refusing to overwrite non-empty release directory: {output}")
        try:
            existing_manifest = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"known release marker is invalid: {marker}") from exc
        if not isinstance(existing_manifest, dict) or existing_manifest.get("generated_by") != "personal-kb-release":
            raise ValueError(f"release marker is not owned by this exporter: {marker}")
        for cache_dir in sorted(output.rglob("__pycache__"), reverse=True):
            if cache_dir.is_dir():
                shutil.rmtree(cache_dir)
        for bytecode in output.rglob("*.pyc"):
            bytecode.unlink()
        known_files = {
            str(value)
            for value in existing_manifest.get("files", [])
            if isinstance(existing_manifest.get("files"), list)
        }
        unexpected = [
            str(path.relative_to(output))
            for path in output.rglob("*")
            if path.is_file() and path.name != RELEASE_MARKER and str(path.relative_to(output)) not in known_files
        ]
        if unexpected:
            raise ValueError(
                "refusing to overwrite release directory with unknown files: "
                + ", ".join(unexpected[:5])
            )

    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=str(parent)))
    try:
        copied = _copy_tree_allowlisted(source, staging)
        marker_payload = {
            "generated_by": "personal-kb-release",
            "schema": "personal-kb.release-manifest/v1",
            "files": copied,
        }
        (staging / RELEASE_MARKER).write_text(
            json.dumps(marker_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        findings = _scan_release(staging)
        if findings:
            raise ValueError("public release scan failed:\n" + "\n".join(f"- {item}" for item in findings))
        if output.exists():
            old_files = existing_manifest.get("files", []) if isinstance(existing_manifest, dict) else []
            if isinstance(old_files, list):
                for relative in old_files:
                    old_path = output / str(relative)
                    if old_path.is_file() or old_path.is_symlink():
                        old_path.unlink()
                    elif old_path.is_dir():
                        shutil.rmtree(old_path)
            (output / RELEASE_MARKER).unlink(missing_ok=True)
            for item in staging.iterdir():
                shutil.copytree(item, output / item.name, dirs_exist_ok=True) if item.is_dir() else shutil.copy2(item, output / item.name)
            shutil.rmtree(staging)
        else:
            staging.rename(output)
        return {"status": "ok", "output": str(output), "files": copied, "file_count": len(copied)}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


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
