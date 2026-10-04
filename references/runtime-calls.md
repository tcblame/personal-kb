# Runtime calls and truthful closeout

Check `kb.py version` when diagnosing source/install drift. `code_sha256` fingerprints executable files; `portable_code_sha256` normalizes local mappings for comparison with the public source. Config/data paths report the actual selected root. A matching fingerprint does not prove business retrieval quality.

## Asynchronous tools

Preserve the complete tool response, including `session_id`, `exit_code`, and `output`. With `functions.exec`, emit `text(await tools.exec_command(...))`; do not emit only `.output`. If the tool returns a process session ID, poll `write_stdin` until completion and emit every result. If the enclosing exec yields a cell ID, resume that cell with `functions.wait`. The two IDs belong to different layers.

`Script completed` confirms only the JavaScript wrapper finished. Empty initial output does not mean the shell command failed or never ran. Before retrying, recover the existing process or receipt. Do not execute a second retrieval just to recover a lost first output. A typed receipt is evidence of a retrieval result; adoption requires a separate task decision.

If the result cannot be recovered, record the real attempt:

```bash
python3 <skill-root>/scripts/kb.py closeout --rag-calls 1 \
  --unresolved-attempt attempt-1=unknown --reason "result not recovered"
```

States are `pending`, `failed`, `unavailable`, `aborted`, and `unknown`. Completed retrieval IDs plus unresolved attempts must equal `rag_calls`. Do not invent a retrieval ID, hit, or adoption. Pass known `--session-id` and `--topic-id` consistently. A later recovered receipt should be reconciled as evidence; do not fabricate a second original call.

## Candidate assessment

When a task reasonably produces reusable knowledge, assess it separately from retrieval. Optional closeout fields `--memory-review` and `--memory-review-reason` distinguish `not_reusable`, `already_recorded`, `pending_verification`, `blocked_by_instruction`, `rejected_evidence`, `written`, and `updated`. The last two require actual written/updated entry IDs. These fields record an assessment, never authorize or perform a durable write. Missing assessment means unobserved, not no candidate.

## Range review

Use `curate-sessions --from YYYY-MM-DD --to YYYY-MM-DD --include-continuations` to consider older-started sessions with activity in the window. Unknown timestamps remain labelled. Filename UUID selects the session's own metadata; missing own metadata is unresolved identity, not parent identity. Keep continuation artifacts and child mirrors outside the verified main-session denominator.
