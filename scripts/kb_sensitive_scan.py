#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable


REDACTED = "[REDACTED]"
SAFE_MARKERS = (
    "redacted",
    "example",
    "placeholder",
    "your_",
    "your-",
    "changeme",
    "xxxx",
    "****",
    "${",
    "{{",
)


def _credential_replacement(match: re.Match[str]) -> str:
    value = match.group("value")
    if _looks_safe(value):
        return match.group(0)
    quote = match.group("quote") or ""
    return f"{match.group('key')}{match.group('sep')}{quote}{REDACTED}{quote}"


def _bearer_replacement(match: re.Match[str]) -> str:
    value = match.group("value")
    if _looks_safe(value):
        return match.group(0)
    return f"{match.group('prefix')}{REDACTED}"


def _url_replacement(match: re.Match[str]) -> str:
    password = match.group("password")
    if _looks_safe(password):
        return match.group(0)
    return f"{match.group('prefix')}{REDACTED}@"


def _opaque_token_replacement(match: re.Match[str]) -> str:
    value = match.group("value")
    return match.group(0) if _looks_safe(value) else REDACTED


PatternSpec = tuple[str, re.Pattern[str], Callable[[re.Match[str]], str]]

PATTERNS: tuple[PatternSpec, ...] = (
    (
        "credential_assignment",
        re.compile(
            r"(?i)(?<![A-Za-z0-9])(?P<key>password|passwd|pwd|client[_-]?secret|access[_-]?token|"
            r"refresh[_-]?token|api[_-]?key|secret[_-]?key)\b"
            r"(?P<sep>\s*[:=]\s*)(?P<quote>[\"']?)(?P<value>[^\s\"'&,;}\]]{6,})(?P=quote)"
        ),
        _credential_replacement,
    ),
    (
        "bearer_token",
        re.compile(r"(?i)(?P<prefix>\bBearer\s+)(?P<value>[A-Za-z0-9._~+/-]{12,}=*)"),
        _bearer_replacement,
    ),
    (
        "url_userinfo",
        re.compile(
            r"(?i)(?P<prefix>\b[a-z][a-z0-9+.-]*://[^\s/@:]+:)"
            r"(?P<password>[^\s/@]{6,})@"
        ),
        _url_replacement,
    ),
    (
        "opaque_token",
        re.compile(
            r"(?<![A-Za-z0-9_])(?P<value>gh[pousr]_[A-Za-z0-9]{20,}|"
            r"github_pat_[A-Za-z0-9_]{30,}|"
            r"sk-[A-Za-z0-9_-]{20,}|"
            r"(?:AKIA|ASIA)[A-Z0-9]{16})(?![A-Za-z0-9_])"
        ),
        _opaque_token_replacement,
    ),
)

PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.S,
)


def _looks_safe(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered or lowered in {"none", "null", "true", "false"}:
        return True
    if lowered.startswith("..."):
        return True
    return any(marker in lowered for marker in SAFE_MARKERS)


def redact_text(text: str) -> tuple[str, list[str]]:
    current = text
    findings: list[str] = []
    for name, pattern, replacement in PATTERNS:
        changed_count = 0

        def apply_replacement(match: re.Match[str]) -> str:
            nonlocal changed_count
            replaced = replacement(match)
            if replaced != match.group(0):
                changed_count += 1
            return replaced

        current = pattern.sub(apply_replacement, current)
        findings.extend([name] * changed_count)
    current, private_key_count = PRIVATE_KEY_RE.subn(
        "-----BEGIN PRIVATE KEY-----\n[REDACTED]\n-----END PRIVATE KEY-----",
        current,
    )
    findings.extend(["private_key"] * private_key_count)
    return current, findings


def redact_value(value: Any) -> tuple[Any, list[str]]:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        out: list[Any] = []
        findings: list[str] = []
        for item in value:
            redacted, item_findings = redact_value(item)
            out.append(redacted)
            findings.extend(item_findings)
        return out, findings
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        findings: list[str] = []
        for key, item in value.items():
            redacted, item_findings = redact_value(item)
            out[key] = redacted
            findings.extend(item_findings)
        return out, findings
    return value, []


def sensitive_findings(value: Any) -> list[str]:
    _redacted, findings = redact_value(value)
    return sorted(set(findings))


def _iter_candidate_files(root: Path, *, include_backups: bool) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(".jsonl") or (include_backups and ".jsonl.bak" in name):
            files.append(path)
    return sorted(files)


def _rewrite_atomic(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o777
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        temp_path = Path(temp_name)
        if temp_path.exists():
            temp_path.unlink()


def scan_file(path: Path, *, apply: bool) -> dict[str, Any]:
    original = path.read_text(encoding="utf-8", errors="replace")
    out_lines: list[str] = []
    findings: list[str] = []
    invalid_json_lines = 0

    for raw in original.splitlines():
        if not raw.strip():
            out_lines.append(raw)
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            invalid_json_lines += 1
            redacted, row_findings = redact_text(raw)
            out_lines.append(redacted)
            findings.extend(row_findings)
            continue
        redacted_row, row_findings = redact_value(row)
        out_lines.append(json.dumps(redacted_row, ensure_ascii=False, separators=(",", ":")))
        findings.extend(row_findings)

    changed = bool(findings)
    if apply and changed:
        trailing_newline = original.endswith("\n")
        output = "\n".join(out_lines) + ("\n" if trailing_newline else "")
        _rewrite_atomic(path, output)

    return {
        "path": str(path),
        "changed": changed,
        "match_count": len(findings),
        "finding_types": sorted(set(findings)),
        "invalid_json_lines": invalid_json_lines,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan and optionally redact credential-shaped values in personal-kb JSONL files.")
    parser.add_argument("--root", required=True, help="Directory containing personal-kb JSONL files")
    parser.add_argument("--apply", action="store_true", help="Rewrite affected files with redacted values")
    parser.add_argument("--include-backups", action="store_true", help="Also scan kb.jsonl.bak* files")
    parser.add_argument("--json", action="store_true", help="Print full JSON report")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(json.dumps({"ok": False, "error": f"root not found: {root}"}, ensure_ascii=False))
        return 2

    results = [
        scan_file(path, apply=args.apply)
        for path in _iter_candidate_files(root, include_backups=args.include_backups)
    ]
    changed = [item for item in results if item["changed"]]
    report = {
        "ok": True,
        "apply": args.apply,
        "root": str(root),
        "files_scanned": len(results),
        "files_with_findings": len(changed),
        "match_count": sum(int(item["match_count"]) for item in results),
        "finding_types": sorted({kind for item in results for kind in item["finding_types"]}),
        "invalid_json_lines": sum(int(item["invalid_json_lines"]) for item in results),
        "affected_paths": [item["path"] for item in changed],
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            "KB_SENSITIVE_SCAN "
            f"apply={report['apply']} files={report['files_scanned']} "
            f"affected={report['files_with_findings']} matches={report['match_count']}"
        )
        for path in report["affected_paths"]:
            print(f"- {path}")
    return 1 if changed and not args.apply else 0


if __name__ == "__main__":
    raise SystemExit(main())
