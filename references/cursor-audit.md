# Cursor usage audit

Use this read-only audit when Cursor is the main client or when a user asks how
Personal KB was used in Cursor. The source is the Cursor project transcript
directory, usually `~/.cursor/projects`.

```bash
python3 <skill-root>/scripts/kb_audit_cursor_sessions.py \
  --root ~/.cursor/projects \
  --from YYYY-MM-DD --to YYYY-MM-DD \
  --closeouts <private-kb-root>/repos/_meta/closeout.jsonl
```

The report has three separate evidence layers:

1. `operation_requests`: executable Shell requests found in main transcripts;
2. `execution_unknown`: requests for which the transcript has no tool result;
3. `runtime_linkage`: closeouts joined only by a unique closeout ID or explicit
   session ID.

Do not call request counts successful usage. Do not merge subagent counts into
main-session counts. The parser intentionally reports heredoc bodies as
`unsupported_shell_calls` for review because a command written inside Python is
not proof that it ran. Runtime adoption still requires `adoption_applied`,
`written_entry_ids`, or `updated_entry_ids` in the closeout record.

The audit uses file modification dates as a selection boundary and says so in
the output. A transcript may contain older embedded turns; do not describe the
window as exact per-call timing. Raw Cursor transcripts stay outside the public
release; commit only aggregate reports after redaction.
