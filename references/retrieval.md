# Retrieval And Storage

Read this file when configuring the Skill, changing storage locations, or deciding whether a file belongs in the durable record or a retained evidence package.

## One physical root

```text
<storage.root>/
  <records>/                 # only long-term retrieval source
  <retained_files>/          # full local evidence bytes
  <manifests>/               # small asset manifests
  <runtime>/                 # closeout, adoption, brief, challenge, audit events
  <cache>/                   # rebuildable index and aggregation files
```

`repo`, `branch`, and `kind` are logical fields below `records`. They are useful for ranking and filtering but do not create separate data sources. `storage.root` may be selected with `PERSONAL_KB_ROOT`; a complete config may be selected with `PERSONAL_KB_CONFIG`. A configured child must be a relative path that stays below the root.

## Daily wrapper

Run from the installed Skill directory or substitute its absolute path for `<skill-root>`:

| Wrapper | Focused operation | Side effect |
|---|---|---|
| `kb.py retrieve` | `kb_rag_context.py` | read-only retrieval |
| `kb.py search` | `kb_search.py` | read-only deep search unless an explicit maintenance flag is supplied |
| `kb.py remember` | `kb_add.py` | add or promote a durable record |
| `kb.py update` | `kb_update.py` | update, supersede, or record adoption |
| `kb.py retain` | `kb_retain_file.py retain` | copy/move local evidence and write a manifest |
| `kb.py reference` | `kb_retain_file.py reference` | save an external credential/resource locator without copying its bytes |
| `kb.py closeout` | `kb_closeout.py` | write one runtime closeout and apply declared adoption |
| `kb.py challenge` | `kb_challenge.py` | prepare/propose/resolve a proposal-only critique |

The audit parser treats these wrappers as the corresponding focused calls, so migrating commands does not create a false drop in retrieval metrics.

## What retrieval returns

The RAG result is a small historical hint: `retrieval_id`, `entry_id`, `kind`, title, score/confidence, freshness, applicability, summary, and source pointers. It must not include a large retained-file body by default. The caller opens a relative pointer or asks an explicit evidence command when the current task needs details.

Never treat a hit as current truth. Verify it against current files, logs, tests, Git state, the live schema, or the user's current instruction. A stale or conflicting hit remains useful as a lead for a challenge proposal, not as authority.

## Precision and ranking

The default RAG executor (`kb_rag_context.py`) uses a bounded two-stage search:

1. Search the exact user query first, with the raw query represented as the `task` group.
2. Keep no more than the configured number of plans (default 3). Expand only when the raw pass has fewer than 2 usable, distinct strong-field hits after eligibility, evidence, deduplication, diversity, and minimum-score checks. Evidence verification is cached within that invocation; blocked and duplicate hits do not stop expansion. Expansion has at most 4 variants per plan by default, and each variant retains at least one concrete anchor from the original query.

Freshness is a ranking and safety signal, not a claim that remote repositories are current. `fresh` verifies the local snapshot bytes, while `runtime_recent` describes a recent runtime hint; neither verifies current task applicability; `needs_recheck` and legacy (`legacy_unverified`/`not_snapshotted`) rows remain warning-level leads; `dirty_worktree`, `diverged`, `unresolvable`, and `conflicted` rows are filtered unless the caller requests non-current data. Set `search.precision.legacy_fallback` to `false` to hide legacy rows; when enabled, complementary legacy rows remain warning-level leads. Only a reliable equivalent in the same repo/branch/kind suppresses its legacy duplicate, before deduplication runs; unrelated fresh records and session briefs do not suppress it. Deduplication requires the same `repo`, `branch`, and `kind`, plus identical normalized titles or title similarity of at least 0.88. A shared `source_paths` or `key_files` value alone never proves duplication. Ordinary non-artifact queries allow at most the configured number of locator maps (default 1).

The output explains these decisions with `matched_anchors`, `trust_tier`, `deduped_similar_count`, `filtered_freshness_count`, and `filtered_map_count` when applicable. These are retrieval diagnostics; they do not replace current-evidence verification.

For a project-owned receipt, pass `--receipt-output <path> --json`. Optional repeated
`--scope-anchor` values must already occur in the query; a `kind:value` prefix is allowed.
An unbound anchor returns `invalid_request` with `retryable:false`; correct the command
only if another attempt is useful. Accept an explicit unavailable result without replaying
the query. If a caller needs exit code 0 for resource unavailability, pass `--best-effort`
on the initial invocation. Internal errors, corrupt records, invalid configuration, and
invalid parameters stay non-zero. Argument parsing follows the JSON error contract too.

Receipts must target a new file outside the canonical data root. Existing files,
configuration targets, symlinks, and junctions are rejected. The writer publishes complete
bytes with an atomic, no-overwrite hard link; filesystems without hard-link support return
a receipt error rather than falling back to a destructive write.

If receipt saving fails, stdout still contains the retrieval payload and an additional
`receipt` object with `status:error`, `retryable:false`, and
`error.code:receipt_write_failed`; exit code is 5. A successful retrieval is marked
`status:partial_failure`, while an original retrieval error keeps its own status/code.
Reuse the returned items and ID. Saving that payload elsewhere does not require retrieval.

## Evidence pointers

Use `source_paths` for current workspace files and `evidence_refs` for typed evidence. A retained asset is represented by an opaque `asset_id` and a relative pointer, for example:

```json
{
  "evidence_refs": [
    {"type": "retained", "value": "asset_260816_004", "path": "database/schema.sql"}
  ]
}
```

The durable record keeps the conclusion and pointer, not the bytes. Local manifests may resolve the asset to absolute origin/stored paths. Public export removes those paths and all real corpus rows.

## Full local archival

When the user requests complete project evidence, use `retain` with the default `copy` mode and a stable case ID. Categories include `database`, `resources`, `datasets`, `logs`, `configs`, `verification`, `conversation`, `screenshots`, and `attachments`. SHA-256 deduplication may reuse bytes across cases while keeping a new asset relation. `verify` checks the stored bytes or confirms an external reference.

`retain` accepts any regular project file, including `.env`, database connection files, active credentials, tokens, and private keys, and preserves its bytes verbatim. The local copy is plaintext; POSIX owner-only modes are best-effort hardening, not encryption or Agent isolation. Keep this data root private and never point the public exporter at it.

Use `reference` instead when the material already lives in a vault, credential store, or external resource system. `--locator` remains a locator field rather than a place to paste content. This version has no password prompt or built-in vault.
