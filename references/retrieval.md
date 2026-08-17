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
