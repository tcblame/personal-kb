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

## Project selection in conversation

Pass an absolute path or a path relative to the task cwd using `--current-file` (also `--active-file`). Supply up to four actual files, including a document outside the cwd when that is the one under discussion. The CLI sees the cwd and the supplied paths. The host supplies the editor selection. Only local project markers, bounded text from those files and selected project overviews, and vocabulary from current KB rows contribute to ranking.

Full JSON, brief JSON, and text output surface the same decision. `project_routing.candidates` lists names with their location, matched words and source pointers. A dated `project_alias` records a user-confirmed colloquial name with an evidence reference. Short ambiguous names and dictionary equivalents can nominate a project for review. An evidenced `belongs_to` relation identifies a subsystem. New working directories can provide a candidate before any KB entry exists. A clear choice sets the read scope for the current lookup. An ambiguous result includes a question and up to three candidates, including substantiated siblings. Use the original wording in the question and offer an independent topic when appropriate. `items` and their freshness still govern whether any historical fact applies.

An explicit repository, branch or project ID keeps the caller's scope. `--as-of` uses the existing dated lookup. `--no-domain` selects the legacy lexical path. A `--global` lookup can return candidates while continuing to search globally.

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

Ordinary callers request `--brief` for compact JSON. It preserves the query, session/topic, ordered candidate IDs, closeout telemetry fields, conclusions, warnings, domain applicability and one source pointer per hit when available. Errors, including a failed optional receipt, retain their full recovery payload and exit semantics. The existing `--json` format includes ranking diagnostics. Both views work as a closeout snapshot. For same-call diagnostic expansion, when a file output is authorized use `--brief --receipt-output <new-path>` and read the selected item from that complete receipt by entry ID. Without a receipt, follow the evidence pointer. No automatic persistent cache is needed.

Never treat a hit as current truth. Verify it against current files, logs, tests, Git state, the live schema, or the user's current instruction. A stale or conflicting hit remains useful as a lead for a challenge proposal, not as authority.

## Precision and ranking

A complete ticket code can locate one domain owner in an established repository and branch. Codes may appear inside a question. The owner and project must share the requested bucket, have active declarations for the requested scope and date, and have either fresh evidence or evidence that needs rechecking. If either needs rechecking, `retrieve` returns one `locate_only` result with the code, entity and project IDs, record revision, source pointers and the two evidence states in `locator_evidence`. Its retrieval score measures the exact identity match. The caller opens the source to establish the answer. An as-of locator carries the cutoff. Its contents supply no dated observations. Competing owners, scoped replacements and unresolved sources prevent this fallback. Normal kind, tag, recency and output limits still apply.

For a Git file, `source_at_commit` identifies the recorded commit and path. That revision also locates the original bytes if the current branch has since renamed or removed the file.

The record's `branch` identifies its source. When that branch differs from the target, a confirmed declaration or applicable scoped replacement can establish coverage. Otherwise the result carries the `target_branch` and a review warning. A replacement limited to another branch also requires review in the source branch. A fresh file can support a historical result. The cutoff still controls which observations and references appear.

With `--focus` or `--action`, a repeatable `--scope-anchor` can carry the
specific device, character, or ticket named in the original task into the
selected question. Use it when the shorter question omits that object. Other
topics in the task stay out of the search. An anchor absent from both texts is
an invalid request. The result's `query` is the wording actually searched.

Global and foreign-scope RAG hits retain a compact conclusion and source paths,
marked `cross_project_review`. They are evidence leads, never implicit promotion
to the current project. Missing workspace buckets use read-only canonical-record
discovery even when the optional inverted index is absent; no index is rebuilt.
The legacy `search` formatter's transferable-only view is not the RAG contract.

Long diagnostic queries require two descriptive task anchors or an exact code
identifier before evidence verification. A repository component or a path alone
cannot provide that evidence. Short lookups and explicit locator queries keep
their recall behavior; `--include-weak` deliberately bypasses the topic gate.
Queries with six or more concrete anchors require three matches, so a product
name plus a generic symptom does not admit a different mechanism. An exact
informative stored phrase remains a recall path; this lexical gate is not a
semantic relevance guarantee.
Locator wording allows evidence paths to supply anchors, but no longer skips
the task-topic gate. Merely mentioning a path in a long diagnostic query does
not authorize unrelated configuration results. Short and exact file lookups
retain their recall paths.

For maps and standing requirements, a query with at least three concrete
original anchors must match two descriptive task anchors, or an exact code/path
identifier. A resource name alone must not admit unrelated configuration or
tool-policy records. Source paths and planner-added terms do not satisfy this
topic check. Explicit material/path/mapping lookup, short lookups and incident/implementation records keep their
existing recall rules; `--include-weak` remains the explicit diagnostic escape.

Repository basenames and path components cannot satisfy the exact-identifier shortcut on a multi-anchor query. In a Chinese query, two distinct short curated triggers appearing in the same record title or alias can jointly establish a topic. A literal compound notation or qualified identifier in the record's triggers can also supply an original anchor. These routes still pass the normal scope, eligibility and freshness checks.

When Codex, Claude, or Gemini is the query subject, its name remains an anchor.
Default candidate filtering requires a matching topic as well; a product name
alone, or only generic overlaps such as `token` and conversation, must not admit
an unrelated record. A tool named as an execution role (for example, asking it
to inspect a business system) does not impose a product-topic boundary.
Descriptive fields and informative stored aliases can establish topical relevance
for cross-project experience without requiring the literal product name.
Explicit `--include-weak` permits broader diagnostic inspection. These are
candidate rules for records. A scope anchor binds a caller-selected object to
the effective query and its receipt. With a focus or action it can carry a
name explicitly present in the full task.

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

## Learning a user's wording

After checking the owner's evidence, run `kb.py learn-alias ENTRY --alias PHRASE --positive QUESTION --negative NEARBY_QUESTION --apply` from its project. Repeat the positive option for a differently worded question. The command compares the same questions before and after on a private copy of the current corpus. It publishes the alias and its bounded checks on the existing owner only when at least one case improves, every positive ranks first and every negative excludes the owner. Later alias changes replay earlier checks on that owner. A new record starts with up to eight aliases and fifteen triggers. A long-lived owner can grow past those initial counts with additional phrases grounded in its saved positive questions and a negative control. Batch limits apply to the new proposals; earlier questions stay in the replay. Omitting `--apply` previews the comparison. A failed case can indicate the wrong project, missing evidence or missing context. Investigate that cause before changing wording further.

If the user separates a short object name and an action in the sentence, optional repeated `--trigger` values can corroborate their shared alias. Keep a negative question that uses the same action on a different object. For a linked conversation, put its original chat URI or resume ID in the focused query. A verified record citing that exact thread includes its link under `related_via`.
