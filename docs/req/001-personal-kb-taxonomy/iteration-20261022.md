# Monthly audit follow-up: structured closeout and retrieval noise

Implemented 2026-10-22. Existing uncommitted 2026-10-21 changes were preserved.

Long non-locator queries previously admitted maps and standing requirements through a resource name alone. Such records now need two original descriptive task anchors, or an exact code/path identifier, when the query contains at least three concrete anchors. Explicit material/path/mapping lookups retain recall; incident and implementation records retain the existing cross-project policy. Source-path matches and planner-added anchors do not establish the new task-topic check.

Structured closeout now preserves and validates JSON memory review and unresolved attempts. A single actual `retrieval_snapshot` supplies linkage, hit candidates/count and compact metadata, without adoption. Conflicting IDs/counts, unreturned adoption, invalid review states and non-object payloads fail before side effects. Observed empty telemetry remains distinct from missing telemetry. Runtime audit adds unique, duplicate and missing closeout-ID counters without changing event-row denominators.

The Skill entry was shortened, permissions separated by destination, and structured closeout made the recommended path. Local runtime mappings live in a separate reference. The public allowlist now includes the optimization, timing, closeout-contract and monthly-regression tests, with sanitized corpus, separate inputs and frozen gold. The Cursor audit test now activates the production guard.

Validation: 31 staged runtime test entrypoints and 13 public-source entrypoints passed; 15 structured-closeout cases passed. The new fixed 30-case sample improved from 25/30 to 30/30: 24 sanitized retrieval cases and six deterministic policy cases, not live-agent outcomes. The existing 54 routing cases passed. Published candidate construction/checks and installed-entry commands passed. Protected fingerprints matched at the test checkpoint. The final check observed one external Thor closeout and one locate event appended to two shared runtime logs, with original byte-prefix hashes intact; the other 39 protected files and private configuration were unchanged.

The old 19 retrieval cases were replayed against the same current corpus before and after. Both passed 5/19, with identical final returned IDs; all 14 required-hit cases remained unmet. This is an unresolved fixture/corpus applicability gap, not a green gate or evidence of improved task success. No old gold was rewritten. No persistent corpus corrections, new domain migration, general latency improvement or live-agent benefit are claimed.

Reproduce the portable sample:

```bash
python3 scripts/kb_monthly_regression_test.py
python3 scripts/kb_closeout_contract_test.py
python3 scripts/kb_eval_preflight.py --routing-only --strict
```

Read `references/validation.md` for the full isolated test and release workflow. Installed private tests require their original fixtures/layout; they are not all part of the public release. Local deployment manifests and rollback evidence remain outside the public package.
