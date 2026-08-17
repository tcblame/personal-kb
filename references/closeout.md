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

Use only classified adoption in the normal path:

- `--used-locate`: record without heat.
- `--used-decide`, `--used-fix`, `--used-write`: record and heat a fresh long-term record.
- No adopted record: provide a concise skipped reason.

Validate adopted IDs against retrieved hit IDs when available. Keep legacy `--used` only for compatibility tests.

Session briefs are short-term context for the last 1-2 days. Require an explicit meaningful `--session-brief-summary`, keep only a small current set per repo/branch, roll off replaced decisions, and never pass brief IDs into long-term heat. A brief must not match a future task solely through generic tags such as `study` or `recent-session`.

Every closeout event writes `session_brief_hit` and `session_brief_help`. Pass `--session-brief-hit` only when retrieval actually returned or consulted a recent brief; pass `--session-brief-help` only when that brief materially changed the completed answer. If neither flag is used, the event records explicit `false` values. Legacy closeouts that lack either field are missing telemetry, not negative evidence; audits must report `session_brief_telemetry_missing` and leave `session_brief_help_rate` as `null`.

Default successful closeout is silent. Use `--verbose` for one minimal summary and `--debug` or `--stdout` for full events. Parameter, storage, lock, corruption, and long-term heat failures must remain actionable and non-zero. Reuse a stable closeout ID for safe retries; inspect `retry_safe` before retrying a partial failure.
