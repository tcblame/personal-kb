---
name: personal-kb
description: Retrieve and maintain verified local personal knowledge when a task depends on prior project history, recurring issues, user corrections, terminology, repo/branch/path mappings, reusable decisions, similar past work, or cross-session continuity. Also use for explicit Personal KB audit or maintenance. Do not use for one-off questions fully answerable from current files, generic code edits, current-state diagnostics with sufficient logs, or unrelated Skill/MCP maintenance.
---

# Personal KB

Use Personal KB as an AI-only historical context layer. Treat every hit as a lead, never as current truth. Current user instructions, files, code, logs, configs, tests, Git state, and current official sources take precedence.

The product goal is a growing veteran colleague for the primary conversation: it remembers explicit user requests, and it may also preserve a verified, reusable bug cause, design decision, resource mapping, or tool lesson discovered while doing work. It is not a transcript dump and it is not a system that must remember every prompt.

There is one physical KB root. `repo`, `branch`, and `kind` are logical facets inside that root, not independent data sources. Keep long-term records in the configured `records` directory; keep retained evidence, runtime events, and rebuildable caches in their configured sibling directories. Retrieval always reads the one records corpus unless an explicit maintenance command says otherwise.

## Decide whether to retrieve

Retrieve only when the answer depends on at least one of these:

- A decision, correction, project state, or unfinished direction from another session.
- A recurring issue, term, path, repo, branch, service, or business-name mapping.
- Similar past implementation or troubleshooting experience.
- An explicit request to retrieve Personal KB records.

Also retrieve when an action depends on an unfamiliar user shorthand or ambiguous durable term whose meaning is not established by current evidence. Treat this as a possible terminology mapping; do not guess that the shorthand is an executable, path, or product name.

Skip ordinary RAG when current evidence fully answers a one-off question, a current log already determines a diagnosis, or the task is unrelated Skill/MCP maintenance. An explicit Personal KB audit or maintenance request triggers this Skill, but runtime audit should read `references/audit.md` and inspect session/runtime evidence directly; it does not require a self-referential long-term RAG query.

A request to remember a newly established rule is maintenance, not by itself a reason for ordinary RAG. Verify the new rule against current evidence, then check for an existing record during the write/update flow so it is updated instead of duplicated. Retrieve first only when the requested rule also depends on an older decision, mapping, incident, or other cross-session fact.

Explicit task constraints win. If a task forbids Personal KB, run no KB command. If it forbids writes but allows KB reading, use only read-only retrieval or audit commands.

## Configure the root and mode

Read `config.json` (or the explicitly selected `PERSONAL_KB_CONFIG`) before writing. `storage.root` is the canonical data root; `storage.records` is the only long-term retrieval source; `storage.retained_files` stores full local evidence packages; `storage.manifests` stores their small manifests; `storage.runtime` stores closeout/challenge/effectiveness events; and `storage.cache` stores rebuildable indexes and aggregations. Each child path must remain relative to `storage.root`.

The installed local config may point runtime/cache at legacy directories so existing history remains readable. A portable installation should use distinct `records`, `retained-files`, `manifests`, `runtime`, and `cache` directories. Do not create a second root to work around a routing ambiguity.

Runtime mode is `normal` by default. `challenge` adds a bounded critic pass after the main conversation has verified and adopted records; it never changes the default retrieval path. Read `references/challenge.md` before enabling it.

Use the stable wrapper for daily work (replace `<skill-root>` with the installed Skill directory):

```bash
python3 <skill-root>/scripts/kb.py retrieve "<history-dependent task>" --limit 5
python3 <skill-root>/scripts/kb.py remember --entry-b64 <base64-json>
python3 <skill-root>/scripts/kb.py update ...
python3 <skill-root>/scripts/kb.py retain --path <file> --project-key <project> --case-id <case> --category <category>
python3 <skill-root>/scripts/kb.py reference --project-key <project> --case-id <case> --reference-kind credential --locator <vault-or-resource-locator>
python3 <skill-root>/scripts/kb.py closeout ...
```

The focused scripts remain available for maintenance and compatibility. A wrapper invocation is equivalent to its focused command for audit and effectiveness metrics.

## Use one budget per topic

Read this Skill once per user request. Before any retrieval, split a request containing independently answerable subjects into separate topics; one user message is not automatically one topic.

For each topic:

1. Decide separately whether current evidence is sufficient or cross-session history is required.
2. Run at most one initial RAG query through the chosen retrieval owner for each history-dependent topic, using only that topic's concrete anchors.
3. Reuse selected hints only within that topic. A retrieval for one topic never consumes or satisfies another topic's budget, even when both topics appeared in the same user message.
4. Within the same topic, retrieve again only when the user introduces a new durable anchor, asks for broader history after a zero-hit query, current evidence contradicts the prior result, or context was lost and no selected result remains. A later independent topic starts its own budget and is not a retry.

Do not combine unrelated topic anchors into one query. After all topics are integrated, run one parent closeout for the whole request and link every retrieval ID that was actually used. A request that fully skipped KB needs no closeout.

## Core lifecycle

1. Retrieve a small context set only when historical knowledge is needed.
2. Verify useful hits against current evidence.
3. Apply the verified current evidence, not the KB text itself.
4. When this task used retrieval or KB maintenance, classify actual adoption once and close out; otherwise stop without a KB lifecycle event.
5. Add or update long-term KB only for verified, stable, reusable conclusions.

Retrieve with:

```bash
python3 <skill-root>/scripts/kb.py retrieve \
  "<task plus project, module, path, config, error, table, or durable business anchor>" --limit 5
```

Default retrieval is read-only. Do not update search counts, rebuild indexes, refresh aggregations, or include non-current records during ordinary work. Use `kb_search.py` only for full-record inspection, recall debugging, migration, normalization, or explicit corpus audit.

## Apply decisions without redoing them

When a hit represents a user-confirmed `requirement` or `decision_confirmed` record:

1. Check whether the current user message replaces it.
2. Open its current source or Canonical file and verify that it still agrees.
3. If it agrees, state that the choice is already settled and execute it; do not restart full option analysis or dispatch broad comparison agents.
4. If the record is fresh and materially selects the route, classify it as `used-decide`. If it only located the current authority, classify it as `used-locate`.
5. If current evidence disagrees, follow current evidence and update or supersede the stale record during explicit maintenance.

## Classify adoption

Search hits are not automatically adopted. Classify only records that materially affected the completed task:

- `--used-locate`: located current evidence; record without heat.
- `--used-decide`: changed or replayed a verified decision; heat.
- `--used-fix`: changed diagnosis or repair; heat.
- `--used-write`: materially supported final content; heat.

Do not use legacy unclassified `--used` in the recommended flow. Do not heat records that were merely read. Session brief IDs are short-term context and must never enter long-term heat.

When this task retrieved or maintained KB, run closeout once after the parent integrates results. Explicitly mark `--session-brief-hit` / `--session-brief-help` when a recent brief was consulted or helped; missing telemetry must be reported as `telemetry_missing`, not as a false/zero result. Routine write/closeout success is silent. Read `references/closeout.md` for fields, brief retention, compatibility, or telemetry debugging.

## Write durable knowledge

Write or update only a verified reusable root cause/fix, mapping, requirement, decision, pitfall, or implementation pattern. The primary record stores a concise conclusion, `asset_id` values, and relative evidence pointers; it does not embed large file bodies. Prefer updating or superseding an existing record over adding a duplicate.

Full project evidence may be retained locally under `retained-files` when the user asks for complete archival: source files, logs, screenshots, database samples, schema/DDL, internal addresses, MCP/SSH resource material, active credentials, tokens, and private keys are all allowed there. Preserve the source bytes verbatim unless the user explicitly requests transformation. The archive is a plaintext copy by default; the original may also remain, so never describe retention as encryption, password protection, or secure deletion. The command applies best-effort owner-only permissions on POSIX, but this is not an isolation boundary. Manifests may contain absolute origin/stored paths locally; public export excludes the real manifests and data root.

Keep the durable `kb.jsonl` row concise: record the conclusion, opaque `asset_id`, and relative evidence pointer instead of duplicating secret values or large bodies into the retrieval index. Use `reference` only when the material already lives in an external vault, credential store, or resource system; use `retain` when the actual bytes must be archived. The first version intentionally has no password prompt or vault abstraction.

For MySQL, retain requested schema, relations, field meaning, DDL, samples, connection material, and source exports verbatim in local evidence. For SSH/MCP, retain requested resource files, purpose, invocation method, local mapping, and connection material. Put reusable non-secret conclusions in the durable row and point to the full local asset for exact details.

Read `references/record-schema.md` before adding, updating, superseding, importing, or refreshing evidence.

## Parent and subagent boundary

The parent decides whether retrieval is needed and chooses its executor. Run a narrow, single-anchor, single-pass retrieval directly in the parent, including one concrete similar incident from another project. Use one dedicated read-only KB scout when retrieval explicitly spans multiple projects or multiple historical stages, follows a zero-hit expansion, requires deduplication or conflict analysis, produces a large candidate set, or must serve multiple downstream workers. Do not infer broad scope merely from words such as `history` or `another project`; the parent must identify a broad routing reason.

The parent owns final hint selection, adoption, heating, writes, and closeout. Pass only selected hints to ordinary workers and require current-evidence verification. Ordinary workers must not run Personal KB scripts. If a worker discovers a new history anchor or insufficient/conflicting hints, it asks the parent for a KB follow-up instead of retrieving directly.

Before dispatching an ordinary worker whose assigned topic depends on history, the parent must finish that topic's retrieval, verify candidates against current authoritative evidence, and pass 1-3 verified topic-relevant hints. Each hint names the authority checked and the verification result. Hints or retrievals from a sibling topic do not satisfy this requirement. If no candidate can be verified, pass no hint and state the history gap; a current-evidence-only worker also receives no KB hint.

Read `references/subagents.md` before dispatching workers or a KB scout.

## Normal and challenge lifecycle

In `normal`, the primary conversation owns retrieval, current-evidence verification, adoption classification, durable writes, and one final closeout. It may proactively remember a stable, verified reusable lesson without waiting for the user to say “remember this”; temporary observations and unverified guesses stay out.

In `challenge`, risk terms trigger a critic immediately; ordinary successful tasks are sampled deterministically at the configured rate (default 10%). The critic receives at most three records actually adopted this turn and produces a proposal only. It cannot write or heat the KB, run closeout, recurse, or silently alter the main result. The primary conversation verifies or rejects the proposal and performs any resulting update itself. Record error types such as `record_error`, `retrieval_error`, `scope_error`, `application_error`, `evidence_error`, or `outcome_unknown`, including why the error occurred.

A non-forced challenge must prove each candidate through a material runtime adoption event (`decide`, `fix`, `write`, or legacy heat) from the declared session; a search hit or locate-only event is not eligible. A proposal must reference the queued brief and include current evidence, a proposed action, and the failure explanation before it can be resolved.

Challenge is an audit cost multiplier, not a second memory source. It exists to expose stale conclusions and explain failures; it must not turn every successful task into a token-heavy debate.

## Runtime output contract

- Retrieval and search commands return their requested context on stdout; this data output is expected.
- Successful write, update, closeout, and maintenance operations default to no stdout or stderr.
- `--verbose`: one minimal structured summary.
- `--debug`: routing reasons, candidate repos, failure IDs, and full events.
- Safe workspace-parent fallback: silent, with internal routing metadata.
- Parameter, unsafe-routing, corruption, lock, and write failures: concise actionable stderr plus non-zero exit.

An explicit repo or branch override must take precedence over repository discovery.

## Read details only when needed

- `references/closeout.md`: adoption, closeout, session brief, and telemetry fields.
- `references/record-schema.md`: durable add/update/supersede/evidence rules.
- `references/subagents.md`: exact worker and KB-scout prompt contracts.
- `references/audit.md`: runtime effectiveness, Codex session audit, and metric semantics.
- `references/maintenance.md`: archive, migrate, normalize, rebuild, sensitive scan, and Git boundaries.
- `references/validation.md`: required checks after changing Skill, scripts, routing, lifecycle, or KB data.
- `references/retrieval.md`: configured paths, wrapper commands, evidence pointers, and retrieval output boundaries.
- `references/challenge.md`: normal/challenge triggers, proposal-only critic contract, and error taxonomy.

Keep routine work on retrieve, verify, apply, classified closeout, and durable write. Do not load audit or maintenance details during ordinary project work.
