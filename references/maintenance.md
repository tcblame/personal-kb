# Maintenance

Read this file only for explicit Personal KB maintenance.

Treat `<storage.root>/<records>/**/kb.jsonl` and explicit retained evidence as durable local facts. Treat closeout logs, session briefs, adoption events, challenge proposals, locks, backups, indexes, aggregations, and effectiveness logs as runtime or derived state. The configured paths are authoritative; do not assume `repos/_meta` or a home-directory fallback.

For archive, migrate, normalize, rebuild, or retention work:

1. Inspect the current corpus and Git state.
2. Run a dry-run when supported.
3. Apply the smallest scoped change.
4. Rebuild derived indexes only after durable records change.
5. Run quality and sensitive-data gates.

An explicit repo or branch override must short-circuit discovery. A safe workspace-parent fallback is valid and silent; record its reason in telemetry. Fail only when routing cannot be made safe. There is one physical root; never “repair” a routing issue by creating another KB root.

Common maintenance commands:

```bash
python3 <skill-root>/scripts/kb_admin.py quality-gate --keep-from 2026-07-01 --strict
python3 <skill-root>/scripts/kb_admin.py rebuild-index --apply
python3 <skill-root>/scripts/kb_admin.py sensitive-scan --include-backups --json
python3 <skill-root>/scripts/kb_admin.py archive --dry-run
```

Use `kb.py retain` for full local evidence, including credential-bearing files when complete archival is requested. Use `kb.py reference` only for material that remains in an external credential or resource system. Retention defaults to a plaintext `copy`; an original file may remain plaintext too. Do not call it encryption, password protection, or secure deletion. Do not add a vault abstraction to this version.

Before publishing, run the allowlist exporter and scan its output. It must exclude the real `config.json`, the complete data root, manifests, runtime logs, and absolute origin/stored paths. Do not export a sanitized copy by overwriting the source data.

## Date-range curation

For an explicitly requested session/date-range review, use
`kb.py curate-sessions --from YYYY-MM-DD --to YYYY-MM-DD --source all --json`.
Use `--include-continuations` when older sessions with activity in the range
belong in the review. The command returns a bounded read-only bundle, never
durable KB. Review one candidate at a time against current evidence and write
only under the applicable destination's authorization. Skip raw transcript
imports, guesses, duplicates, and temporary state.

## Maintaining the Skill repository

Commit verified changes to the Skill repository promptly on the active maintenance branch. When a push is requested, push that branch and verify the remote commit ID.

## Local backup

Use the configured private backup integration when installed; consult the
verified local runtime mapping for its command. Do not publish local data or
credentials with a Skill release. Backup push is a separate network action,
not an implicit part of retrieval or code installation.
