# Durable record schema

Read this file only for add, update, supersede, import, or evidence-refresh work.

Use one of six kinds: `map`, `issue`, `pitfall`, `experience`, `requirement`, or `implementation`.

Every durable record needs:

- A short stable title and concise reusable conclusion.
- 2-8 aliases and 3-15 trigger terms.
- At least one current evidence pointer.
- The durable row contains a concise conclusion and evidence pointers, not a large file body.
- Active passwords, tokens, private keys, and connection secrets may be preserved verbatim in a retained local file, but are referenced by `asset_id` rather than duplicated into the retrieval row.
- Database schema/DDL, samples, connection files, and internal resource evidence may be retained locally without redacting the archived bytes. Keep the durable summary concise and point to the retained asset for exact values.

Use `source_paths` only for files that semantically support the conclusion. Use `evidence_refs` for commits or conversation evidence. A fresh snapshot of an unrelated file does not make a record trustworthy. If a user preference is also recorded in a current state file, point to that file.

For retained evidence, use a typed reference with an opaque `asset_id` and a relative path. Keep manifest absolute paths out of the row:

```json
"evidence_refs": [
  {"type": "retained", "value": "asset_260816_004", "path": "database/schema.sql"}
]
```

The `asset_id` must resolve through the local manifest before it is used. Public export keeps the opaque ID and relative category/path only.

Records that only locate generated resume/interview artifacts must use `kind=map` and `artifact_locator=true`. Keep technical facts and user-confirmed decisions in separate records backed by code, config, logs, tests, current documentation, or explicit conversation evidence.

Use base64 through Python rather than browser-only `btoa`:

```bash
python3 - <<'PY'
import base64, json, subprocess
entry = {
    "kind": "requirement",
    "title": "<stable title>",
    "story": "<verified reusable conclusion>",
    "aliases": ["<alias-1>", "<alias-2>"],
    "trigger_terms": ["<anchor-1>", "<anchor-2>", "<anchor-3>"],
    "source_paths": ["<current evidence file>"],
}
payload = base64.b64encode(json.dumps(entry, ensure_ascii=False).encode()).decode()
subprocess.run([
    "python3", "<skill-root>/scripts/kb.py", "remember",
    "--entry-b64", payload, "--smart-field-check",
], check=True)
PY
```

Prefer update over duplicate add. Use `--expected-rev` for optimistic concurrency. Refresh evidence only after reviewing current files. When replacing a decision, mark the old record `superseded` and link its replacement and reason.
