from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kb_lib import ensure_branch_layout, resolve_context, write_index


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Show current repo/branch bucket and paths.")
    parser.add_argument("--repo", default="", help="Override repo bucket (optional)")
    parser.add_argument("--branch", default="", help="Override branch bucket (optional)")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--read-only", action="store_true", help="只输出路径，不写入 index.json")
    args = parser.parse_args(argv)

    ctx = resolve_context(
        cwd=Path.cwd(),
        repo_name_override=(args.repo.strip() or None),
        branch_override=(args.branch.strip() or None),
    )
    ensure_branch_layout(ctx)

    # 只在非只读模式下写入 index.json
    if not args.read_only:
        write_index(ctx)

    payload = {
        "repo": ctx.repo_name,
        "branch": ctx.branch,
        "branch_dir": ctx.branch_dir,
        "branch_path": str(ctx.branch_path),
        "kb_path": str(ctx.kb_path),
        "summary_path": str(ctx.summary_path),
        "index_path": str(ctx.index_path),
    }

    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0

    sys.stdout.write(f"repo: {ctx.repo_name}\n")
    sys.stdout.write(f"branch: {ctx.branch}\n")
    sys.stdout.write(f"bucket: {ctx.branch_path}\n")
    sys.stdout.write(f"kb: {ctx.kb_path}\n")
    sys.stdout.write(f"summary: {ctx.summary_path}\n")
    sys.stdout.write(f"index: {ctx.index_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
