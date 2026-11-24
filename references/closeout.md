# Closeout and short-term continuity

Run closeout once after the parent integrates one logical task that actually retrieved, adopted, wrote, or updated KB. A task that fully skipped KB needs no closeout. Do not repeat an earlier query in a later aggregate closeout.

Prefer structured input for ordinary closeout:

```bash
python3 <skill-root>/scripts/kb.py closeout --json-file <task-closeout.json> --require-complete-context
```

The JSON object separates actual result metadata from the parent's judgment:

```json
{
  "closeout_id": "stable-task-id",
  "session_id": "known-session-id",
  "topic_id": "known-topic-id",
  "retrieval_snapshot": {
    "retrieval_id": "copy-actual-result-id",
    "items": [
      {"entry_id": "actual-entry-id", "kind": "issue", "freshness_state": "fresh", "trust_tier": "verified"}
    ]
  },
  "adoption_effects": {"locate": [], "decide": [], "fix": [], "write": []},
  "rejection_reasons": {"actual-entry-id": "current evidence did not support this task"},
  "memory_review": {"disposition": "not_reusable", "reason": "no verified new reusable conclusion"}
}
```

Copy a single actual retrieval result into `retrieval_snapshot`, including its
original ID and items. Extra item bodies/paths are stripped. The writer derives
linkage, candidate IDs, hit count and compact telemetry; it does not adopt or heat
returned records. Explicit conflicting IDs/counts fail before side effects. An
empty snapshot supplies an empty candidate set and cannot authorize adoption.
This is caller-supplied result evidence, not an authenticated retrieval registry.

For multiple calls, put every original returned object in `retrieval_snapshots`.
Do not also supply `retrieval_snapshot`. The writer validates unique call IDs,
derives all links, sums hit slots, unions candidate IDs, copies queries and
compacts ranking telemetry; repeated records across calls are legitimate slots.
Explicit conflicting counts or IDs fail before side effects. Legacy callers can
still provide explicit `linked_retrieval_ids`, `rag_calls`, candidate IDs and
compact `retrieval_items`. Existing quality statistics describe the first three
supplied positions, not per-topic recall.

`--require-complete-context` requires a session ID, task topic, memory assessment,
and observed telemetry for completed retrievals. An empty observed result is
valid. Session precedence is explicit CLI/JSON, current Codex thread/session
environment, then one unambiguous snapshot session. A parent keeps its own
identity when using a child scout's snapshot. A shared snapshot topic is reused;
different topics require an explicit parent task topic. No identity is guessed
from cwd or another rollout. This validation does not authorize writes or infer
adoption, and the compatibility path without the flag remains supported.
JSON `unresolved_retrieval_attempts` accepts objects with `attempt_id` and `state`
(`pending`, `failed`, `unavailable`, `aborted`, `unknown`). Completed IDs plus
unresolved attempts must equal `rag_calls`; they never supply confirmed hits.

`memory_review` accepts `disposition` and a required `reason`, matching the CLI
assessment states. `written`/`updated` require corresponding actual entry IDs.
Optional `target` is `builtin_memory`, `personal_kb`, `task_writes` or `unknown`;
optional `instruction_source` is `system`, `developer`, `user`, `task` or `unknown`.
These fields explain a restriction and never grant write permission. Omit unknown
details or mark them `unknown`; do not copy the controlling prompt into telemetry.

The equivalent CLI remains supported:

```bash
python3 <skill-root>/scripts/kb.py closeout \
  --closeout-id "<stable task id>" \
  --linked-retrieval-id "<runtime retrieval id>" \
  --query "<retrieval query>" \
  --rag-calls <count> \
  --hit-count <count> \
  --allowed-hit-id <entry_id> \
  --used-locate <entry_id> \
  --reason "<required when nothing was adopted/written/updated>"
```

Repeat `--linked-retrieval-id` for every RAG call integrated by this parent closeout. For parent-direct retrieval, copy the ID from the parent's RAG output. For delegated retrieval, copy the unchanged IDs from the KB scout brief. Never invent IDs or rerun RAG in the parent only to create telemetry.

Preserve useful negative feedback and ranking telemetry when the caller has it:

```bash
python3 <skill-root>/scripts/kb.py closeout \
  --rejected-hit-id "<entry-id>" \
  --rejection-reason "<entry-id>=stale evidence" \
  --miss-reason no_verified_hit \
  --retrieval-items-json '[{"entry_id":"<entry-id>","kind":"issue","freshness_state":"needs_recheck","trust_tier":"recheck"}]'
```

`--rejected-hit-id` must be one of the `--allowed-hit-id` candidates when an allow-list is present. A rejection reason is clipped and stored by entry ID; it is not an adoption event. `--miss-reason` is one of `trigger_missed`, `query_too_weak`, `no_verified_hit`, `execution_failed`, or `unknown`, and permits a closeout with no action IDs when the retrieval genuinely produced no usable verified record. `--retrieval-items-json` accepts an array or an object containing `items`, keeps at most 20 compact ranking rows, and strips summaries, paths, and other large fields before persistence.

Use only classified adoption in the normal path:

- `--used-locate`: record without heat. A `locate_only` retrieval receipt supports this effect. Decision, fix, write and legacy adoption for the same ID require a separate verified retrieval with appropriate applicability.
- `--used-decide`, `--used-fix`, `--used-write`: record and heat a fresh long-term record.
- No adopted record: provide a concise skipped reason.

Validate adopted IDs against retrieved hit IDs when available. Keep legacy `--used` only for compatibility tests.

Session briefs are short-term context for the last 1-2 days. Require an explicit meaningful `--session-brief-summary`, keep only a small current set per repo/branch, roll off replaced decisions, and never pass brief IDs into long-term heat. A brief must not match a future task solely through generic tags such as `study` or `recent-session`.

Every closeout event writes `session_brief_hit` and `session_brief_help`. Pass `--session-brief-hit` only when retrieval actually returned or consulted a recent brief; pass `--session-brief-help` only when that brief materially changed the completed answer. If neither flag is used, the event records explicit `false` values. Legacy closeouts that lack either field are missing telemetry, not negative evidence; audits must report `session_brief_telemetry_missing` and leave `session_brief_help_rate` as `null`.

Use `--used-locate` for a record that only found current evidence; use `--used-decide`, `--used-fix`, or `--used-write` when it materially changed the result. A record cannot be both adopted and rejected in one closeout. The closeout writer validates this conflict before applying heat, so a malformed feedback payload fails without pretending that the task was successfully recorded.

Default successful closeout is silent. Use `--verbose` for one minimal summary and `--debug` or `--stdout` for full events. Parameter, storage, lock, corruption, and long-term heat failures must remain actionable and non-zero. Reuse a stable closeout ID for safe retries; inspect `retry_safe` before retrying a partial failure.

When a partial closeout reports `retry_safe:false` or
`failed_side_effects_unknown:true`, do not rerun the same closeout automatically. Keep the
partial event as the audit record and continue the user task; repair the missing entry or
side-effect state in a separate maintenance action.

Observed empty results retain `retrieval_items: []` and count as an observed
quality row with zero slots. Missing telemetry remains unknown. Runtime audit
reports both event rows and unique non-empty closeout IDs, plus duplicate and
missing-ID counts. It does not deduplicate or rewrite historical events.

An optional memory_gap object records a verified failure stage and a brief evidence
pointer. Supported stages are missing_record, missed_trigger, missed_recall,
wrong_scope, stale_fact, unused_hit, late_use and broken_evidence. Establish that
a target record already existed before calling it missed_recall. The stats JSON
counts reported stages by unique closeout ID. Missing feedback is unknown.

When a maintained record has a confirmed `supersedes` edge, closeout derives `replacement_links` from the stored owner. The event keeps the predecessor, successor, scope and effective time for later review. A separate release document can be the evidence for such a change.
