# Closeout and short-term continuity

Run closeout once after the parent integrates one logical task that actually retrieved, adopted, wrote, or updated KB. A task that fully skipped KB needs no closeout. Do not repeat an earlier query in a later aggregate closeout.

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

- `--used-locate`: record without heat.
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
