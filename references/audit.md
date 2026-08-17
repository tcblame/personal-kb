# Runtime audit

Read this file only for explicit Personal KB quality, effectiveness, or runtime debugging. Runtime audit does not need a long-term RAG query unless record recall itself is under test.

Audit three independent layers:

1. Trigger and lifecycle: expected retrieval, per-turn missing closeout, and subagent boundary violations.
2. Retrieval value: returned hit slots, locate/decide/fix/write adoption, rejected hits, and session-brief help.
3. Corpus quality: lifecycle status, duplicates, evidence alignment, freshness, resolvable paths, artifact-locator share, and sensitive content.

Use current session JSONL and runtime logs as evidence. Runtime and derived data are measurements, not durable facts.

```bash
python3 <skill-root>/scripts/kb_audit_codex_sessions.py --last-days 2
python3 <skill-root>/scripts/kb_record_codex_effectiveness.py \
  --current-cutoff YYYY-MM-DD --strict-quality
python3 <skill-root>/scripts/kb_audit_runtime_value.py --last-days 2
python3 <skill-root>/scripts/kb_eval.py audit-runtime --last-days 2
```

`kb_eval_preflight.py` is a deterministic policy simulator. Its routing accuracy and
temporary-copy retrieval checks are not live Codex adoption, closeout completion,
or final task benefit. Measure those separately with runtime closeouts, real session
audits, and human outcome review.

Identity and pairing rules:

- Identify a rollout by the `session_meta.id` matching the UUID in its filename. Do not let embedded parent metadata replace the rollout's own identity.
- Keep root and subagent metrics separate even when they share a parent `session_id`.
- Link a delegated scout retrieval to the parent only through an exact runtime `retrieval_id`, an explicit parent closeout link, and the rollout `parent_thread_id`. Never guess a cross-rollout link from query similarity or timing alone.
- Pair retrieval and closeout by turn/query/call evidence; an old closeout elsewhere in a long session does not close a later retrieval.
- Apply the requested date window to sessions and closeout rows.
- Treat explicit Personal KB runtime audits as direct session/runtime inspection, not as missed long-term RAG.

Metric semantics:

- Locate without heat is correct, not an error.
- Decide/fix/write without required heat is an error.
- Hits rejected with a non-empty reason are measured as rejection, not malformed closeout.
- Count real script execution, not text mentions or source-file reads.
- Treat `kb.py retrieve/search/remember/update/closeout` as their focused command equivalents.
- Do not count `kb_*.py --help` as retrieval, maintenance, or closeout usage.
- Count per-entry adoption as confirmed only when actual closeout JSON exposes successful IDs; keep silent-command `--used-*` values as requested/unconfirmed and report session briefs separately.
- Treat summaries generated before the current window as stale until rebuilt.
- Treat `main_missed_rag_sessions` as auditor candidates, not human ground truth.
  The canonical small review sample is
  `docs/req/001-personal-kb-taxonomy/evals/runtime-session-human-labels.json`;
  report confirmed misses, confirmed false positives, and unreviewed candidates
  separately.

Keep main-session metrics separate from ordinary subagent behavior. Do not expose routine telemetry in normal answers; surface actionable findings only when the user asks for an audit.

Challenge proposals and resolutions are runtime audit events, not adoption proof. A critic that writes the durable KB or closes out its own proposal is a quality failure.
