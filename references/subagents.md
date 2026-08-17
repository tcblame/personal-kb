# Subagent boundary

The parent first maps each worker to one topic, then decides that topic's retrieval route and owns final hint selection, adoption, heating, writes, and closeout. A retrieval or hint for one topic never initializes or satisfies a sibling topic.

Use parent-direct retrieval for a narrow, single-project or single-topic query with concrete anchors and a small expected result set, including one concrete similar incident from another project. Use one explicitly named read-only KB scout when retrieval explicitly spans multiple projects or multiple historical stages, follows a zero-hit expansion, requires candidate deduplication or conflict analysis, or must serve multiple downstream workers. Do not infer broad scope only from vague history wording.

For an ordinary worker, include exactly:

```text
Treat KB hints as historical leads and verify them against current evidence.
Do not run personal-kb scripts. Use only KB hints provided by the parent; the parent owns retrieval routing and final hint selection, adoption, heating, writes, and closeout.
```

Before dispatching a history-dependent worker, finish that topic's retrieval and verify candidates against current authoritative evidence. Pass only 1-3 verified, task-relevant hints; do not copy the whole RAG payload, pass an unverified scout candidate, or substitute a hint from another topic. Each hint must name `verified_against` and `verification_result`. If no candidate can be verified, pass no KB hint and state the history gap. The worker still re-checks each supplied hint within its scoped current evidence.

If an ordinary worker finds a new durable history anchor, insufficient hints, or a conflict with current evidence, return this instead of running KB:

```text
kb_followup_request:
- anchor: <new project, path, error, decision, or alias>
- need: <what history would clarify>
- reason: <why current hints/evidence are insufficient or conflicting>
```

For routing/status reports, describe the current Agent's action, not the whole logical task:

- Reusing parent hints or a returned scout brief is `action=reuse_parent_hints`, `phase=skip`, `should_retrieve=false`, `kb_owner=parent`.
- An ordinary worker that needs history returns `action=skip`, `phase=skip`, `should_retrieve=false`, `kb_owner=parent`, route `request_parent_followup`, and a `kb_followup_request`.
- A dedicated scout retrieval is `action=retrieve`, `phase=retrieve`, `should_retrieve=true`, `kb_owner=kb_scout`; handoff is required because the scout must return its brief to the parent.
- An explicitly assigned KB runtime audit may remain with `kb_owner=kb_scout`, but it reads raw session/runtime evidence without long-term RAG and returns an audit report rather than a scout-brief schema.

## KB scout contract

Every read-only RAG call returns an opaque runtime-generated `retrieval_id`. The scout copies each ID into its brief without editing it. The parent links those IDs in its final closeout, so audit tools can connect scout retrievals to the parent lifecycle without repeating RAG in the parent.

Include this authorization in a scout prompt:

```text
Authorization: dedicated read-only Personal KB scout.
Scope: <broad history question and durable anchors>
Run only read-only Personal KB context/search commands. Do not add, update, heat, write, or close out.
Return one personal-kb.scout-brief/v1 JSON object; do not return the whole RAG payload.
```

Return exactly one compact object with this shape:

```json
{
  "schema": "personal-kb.scout-brief/v1",
  "retrievals": [
    {
      "retrieval_id": "runtime-generated-id",
      "query": "actual query",
      "hit_count": 1,
      "hit_entry_ids": ["entry-id"]
    }
  ],
  "candidate_ids": ["entry-id"],
  "selected_hints": [
    {
      "entry_id": "entry-id",
      "claim": "precise historical lead, not current truth",
      "source_or_time": "source path, timestamp, or both",
      "freshness_state": "fresh|legacy_unverified|needs_recheck",
      "verify_against": ["current authoritative file, code, log, or canonical"],
      "risk": "scope, conflict, or applicability warning"
    }
  ],
  "discarded": [
    {"entry_id": "entry-id", "reason": "off_topic|stale|contradicted|weak_match"}
  ],
  "conflicts": ["candidate conflict that the parent must resolve"],
  "gaps": ["question not answered by KB"]
}
```

Every selected or discarded long-term record must appear in `candidate_ids`, and every returned RAG hit must remain traceable through `retrievals[*].hit_entry_ids`. Return at most 5 `selected_hints` and 10 individual `discarded` rows; summarize any additional rejected candidates by count and reason in `gaps`. Keep each claim to one or two sentences and preserve IDs, provenance/freshness, rejection reasons, and current-authority verification targets. Do not add adoption fields such as `used-locate/decide/fix/write`; all recommendations are provisional and the parent makes final selection and adoption. The scout must not add, update, heat, write, or close out.

Follow-up workers for the same topic reuse the parent's existing selected hints. A new worker alone does not justify another RAG, but a different history-dependent topic has its own initial retrieval budget.
