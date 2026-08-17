# Challenge Mode

Read this file before enabling `runtime.mode=challenge` or running `kb.py challenge`.

## Purpose

Normal mode optimizes for useful memory with one primary conversation. Challenge mode adds a bounded adversarial review so stale records, wrong scope, and evidence mistakes become visible. It is intentionally more expensive and is not a second KB or a second retrieval source.

## Trigger policy

1. While `runtime.mode=challenge`, risk terms (for example production incident, rollback, data loss, security, user contradiction, or evidence conflict) trigger a challenge immediately. Normal mode does not start a critic from risk words alone.
2. While challenge mode is active, an ordinary successful task is sampled using a stable SHA-256 fraction of `task_id`; retries make the same decision. The default sample rate is 10%, and sampled success reviews may be deferred until task completion.
3. `--force` is available for an explicit audit. `--sample-rate` is useful in evaluation, not as a hidden production override.
4. Only records actually adopted by the primary conversation are eligible, with a default maximum of three per task. A mere search hit is never sent to the critic as adopted knowledge.
5. Except for an explicit `--force` audit, `prepare` verifies every candidate against a material runtime adoption event (`decide`, `fix`, `write`, or legacy heat) and the supplied event/session identity. Locate-only events do not qualify.

## Critic contract

The critic receives the selected record conclusion, record revision, evidence pointers, current verification notes, and the task outcome. It returns a `personal-kb.challenge-proposal/v1` proposal containing:

- the suspected error type: `record_error`, `retrieval_error`, `scope_error`, `application_error`, `evidence_error`, or `outcome_unknown`;
- the claim under review and the current evidence that supports or contradicts it;
- a proposed correction, supersession, or “keep” decision;
- why the original record led to the mistake, if it did.

The payload uses `proposal_id`, `entry_ids`, `error_type`, `claim`, `current_evidence`, `proposed_action`, optional `proposed_change`, and `why_original_failed`. `proposed_action` is one of `keep`, `correct`, `supersede`, or `defer`; corrections and supersessions require `proposed_change`.

The critic is proposal-only. It must not call `remember`, `update`, `retain`, `closeout`, or another challenge; it cannot heat records, recurse, or rewrite the main answer. `critique_depth` is fixed at 1. Runtime events are written under `storage.runtime`; the durable KB is unchanged until the primary conversation verifies the proposal.

## Primary resolution

The primary conversation checks the proposal against current authoritative evidence and records `accepted`, `rejected`, or `deferred`. Only the primary conversation may then update/supersede a record, attach a retained asset, or write a concise error lesson. A rejected proposal remains useful audit history but does not affect ranking.

## Commands

```bash
python3 <skill-root>/scripts/kb.py challenge prepare \
  --task-id <stable-task-id> --task-text "<task and outcome>" \
  --entry-id <adopted-entry-id> --mode challenge \
  --adoption-event-id <runtime-adoption-event-id> --session-id <session-id> --enqueue
python3 <skill-root>/scripts/kb.py challenge propose --json-file <proposal.json>
python3 <skill-root>/scripts/kb.py challenge resolve \
  --proposal-id <proposal-id> --decision accepted --verified-against <path>
```

`prepare` may return `skipped` in normal mode, `ready` for a sampled/risk task, `unverified_adoption` when an ID has no matching material adoption event, or `missing_entries` when an adopted ID cannot be found. `--force` is the explicit exception for auditing a record that was not adopted in the current task. A submitted proposal must reference an enqueued brief and include current evidence, a `keep/correct/supersede/defer` action, and why the original conclusion failed or remained valid. A proposal or resolution never silently writes a durable record.
