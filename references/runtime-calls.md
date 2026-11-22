# Runtime calls and truthful closeout

Check `kb.py version` when diagnosing source/install drift. `code_sha256` fingerprints executable files; `portable_code_sha256` normalizes local mappings for comparison with the public source. Config/data paths report the actual selected root. A matching fingerprint does not prove business retrieval quality.

## Asynchronous tools

### Optional latency diagnostics

Append `--timings` to the stable wrapper, for example
`kb.py retrieve "<query>" --limit 3 --json --timings` or
`kb.py closeout <normal closeout arguments> --timings`.
For recognized commands it emits one `KB_TIMING {json}` line on stderr,
including on a handled failure. Unknown command names are not recorded;
`exit_code: null` means an unhandled exception prevented a command result.
Default output, retrieval ranking, freshness, exit codes, and silent closeout
are unchanged. Timing data is process-local and is not written to KB records,
runtime logs, or receipts. Labels contain no queries, paths, or command operands.

`elapsed_ms` covers the wrapper after option dispatch, including module import
and command execution. It excludes WSL/Python startup, wrapper bootstrap,
report emission and external output delivery. Compare it with the outer tool's
wall time; the difference is **unattributed outer overhead**, not a direct WSL
cold-start measurement. Stages are inclusive/nested and must not be added.
Git stages aggregate fixed subcommands, including KB-file safety checks as well
as evidence verification. `retrieve.collect` includes eligibility and evidence
checks; `retrieve.domain_init` measures only context construction.
Entry cache misses count unique candidate verification attempts; cache hits
count reused attempts; snapshot counts refer to actual verification visits.
Raw candidate visits can repeat a record across query plans.
Closeout append includes lock wait, existing-file safety checks, append/close,
and any index hook; `storage.write` does not promise an fsync/durable commit.

`git_checks` reports each fixed subcommand's calls, unique exact cwd/argument
combinations, repeated calls, and time spent on repeated calls. Comparison keys
stay private in memory; directory aliases are counted separately, without extra
filesystem resolution in the timer. Repeats are optimization candidates, not a
claim they can safely be skipped. Evidence object-fact cache hits/misses and its
metadata guard cost are separately reported when present.

Retrieval first uses project, scope, date and wording to find candidates. It
verifies the candidate, its project authority and any decision or one-hop
relation that affects the result. Stale evidence cannot authorize a replacement.
The domain summary also checks applicable replacement declarations for its
existing review warning.

Each query has its own Git read cache. Git confirms repository discovery and
checkout identity. Ordinary worktrees can reuse that result while the observed
metadata matches. An index change triggers a new unresolved-path scan. Every
evidence file still receives its own worktree status check. Changed HEADs,
remote configuration or worktree indexes are observed in that query. Unusual
layouts and config overrides fall back to Git. Successful full-OID object,
ancestry and committed-blob checks retain their existing freshness guards.
All reuse ends with the query.

Preserve the complete tool response, including `session_id`, `exit_code`, and `output`. With `functions.exec`, emit `text(await tools.exec_command(...))`; do not emit only `.output`. If the tool returns a process session ID, poll `write_stdin` until completion and emit every result. If the enclosing exec yields a cell ID, resume that cell with `functions.wait`. The two IDs belong to different layers.

`Script completed` confirms only the JavaScript wrapper finished. Empty initial output does not mean the shell command failed or never ran. Before retrying, recover the existing process or receipt. Do not execute a second retrieval just to recover a lost first output. A typed receipt is evidence of a retrieval result; adoption requires a separate task decision.

If the result cannot be recovered, record the real attempt:

```bash
python3 <skill-root>/scripts/kb.py closeout --rag-calls 1 \
  --unresolved-attempt attempt-1=unknown --reason "result not recovered"
```

States are `pending`, `failed`, `unavailable`, `aborted`, and `unknown`. Completed retrieval IDs plus unresolved attempts must equal `rag_calls`. Do not invent a retrieval ID, hit, or adoption. Pass known `--session-id` and `--topic-id` consistently. A later recovered receipt should be reconciled as evidence; do not fabricate a second original call.

Retrieval and closeout reuse the current Codex thread/session environment when
the caller omits the session ID. Keep an explicit stable topic per task. Preserve
each original result and pass it through `retrieval_snapshot(s)` with
`--require-complete-context`; a copied summary or a hand-counted hit total loses
the context needed for reliable pairing.

## Candidate assessment

When a task reasonably produces reusable knowledge, assess it separately from retrieval. Optional closeout fields `--memory-review` and `--memory-review-reason` distinguish `not_reusable`, `already_recorded`, `pending_verification`, `blocked_by_instruction`, `rejected_evidence`, `written`, and `updated`. The last two require actual written/updated entry IDs. These fields record an assessment, never authorize or perform a durable write. Missing assessment means unobserved, not no candidate.

## Range review

Use `curate-sessions --from YYYY-MM-DD --to YYYY-MM-DD --include-continuations` to consider older-started sessions with activity in the window. Unknown timestamps remain labelled. Filename UUID selects the session's own metadata; missing own metadata is unresolved identity, not parent identity. Keep continuation artifacts and child mirrors outside the verified main-session denominator.
