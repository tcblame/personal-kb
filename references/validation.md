# Validation

After changing prompt policy, routing, lifecycle, scripts, or durable data:

1. Validate Skill frontmatter and every referenced file.
2. Compile changed Python modules.
3. Run focused retrieval, closeout, session brief, audit, effectiveness, routing, sensitive-data, and quality-gate tests.
4. Run the end-to-end smoke test.
5. Run positive, negative, and boundary eval prompts against the baseline and candidate when trigger policy changes.

Core commands:

```bash
python3 -m py_compile <skill-root>/scripts/*.py
python3 <skill-root>/scripts/kb_rag_context_test.py
python3 <skill-root>/scripts/kb_audit_codex_sessions_test.py
python3 <skill-root>/scripts/kb_record_codex_effectiveness_test.py
python3 <skill-root>/scripts/kb_storage_test.py
python3 <skill-root>/scripts/kb_retain_file_test.py
python3 <skill-root>/scripts/kb_challenge_test.py
python3 <skill-root>/scripts/kb_eval_preflight_test.py
# 无历史窗口时运行确定性的策略/路由门禁；当前环境没有可回放主会话时，
# 不要把空历史窗口误判为策略回归。
python3 <skill-root>/scripts/kb_eval_preflight.py \
  --routing-only --strict
# RAG 运行时隔离检查（不依赖最近主会话窗口）：
python3 <skill-root>/scripts/kb_eval_preflight.py \
  --run-rag --json
python3 <skill-root>/scripts/kb_p0_usage_test.py
python3 <skill-root>/scripts/kb_session_brief_test.py
python3 <skill-root>/scripts/kb_sensitive_scan_test.py
python3 <skill-root>/scripts/kb_quality_gate_test.py
python3 <skill-root>/scripts/kb_quality_gate.py --keep-from 2026-07-01 --strict
python3 <skill-root>/scripts/kb_smoke_test.py <skill-root>
python3 <skill-root>/scripts/kb_eval.py release-build --output <release-dir>
python3 <skill-root>/scripts/kb_eval.py release-check --output <release-dir>
python3 <skill-creator-root>/scripts/quick_validate.py <skill-root>
git diff --check
```

Required regression assertions:

- Rollout identity uses the filename UUID's own `session_meta.id`; root and subagent metrics stay separate.
- New classified adoption flags are parsed and reported.
- Retrieval/closeout pairing is per turn or query, not merely per long session.
- Artifact locators do not fill unrelated diagnostics; session briefs do not match on generic tags alone.
- User-confirmed requirements remain retrievable and current evidence remains authoritative.
- Explicit repo override and safe workspace fallback are silent.
- Successful closeout is silent; locate does not heat; decide/fix/write do; brief IDs never heat.
- Parameter, corruption, lock, unsafe-routing, and write failures remain actionable and non-zero.
- Ordinary subagents do not invoke KB.
- A multi-topic request is split before retrieval; each history-dependent topic has an independent initial RAG budget, while the parent still closes out once.
- `更新 ccs` resolves the durable `ccs = cc-switch` mapping and recalls the verified safe update entry instead of searching for a `ccs` executable.
- Safety-critical `ccs` mappings and update/restart commands remain in a mandatory project instruction section, outside optional KB policy, and the project prompt requires verified parent hints before dispatching a history-dependent worker.
- Broad parent retrieval routes to one read-only KB scout; the scout cannot write, heat, or close out; the parent reuses the returned brief without a duplicate RAG.
- Every RAG output has an opaque `retrieval_id`; delegated IDs link only to the declared parent closeout and orphan/duplicate IDs remain visible.
- Policy-simulator inputs and frozen gold remain separate; `--strict` runs trusted RAG against a temporary current-KB copy and checks production/snapshot hashes.
- Historical replay is a behavior shadow only. Multi-message turns and possible duplicates without lifecycle state are excluded from call-efficiency comparison; unknown execution status is reported, not guessed as success.
- Runtime and cache path overrides resolve below one canonical root; no hidden work-directory fallback is accepted.
- Full evidence retention keeps bytes outside `kb.jsonl`, deduplicates by hash, and preserves credential-bearing files verbatim under the private local data root.
- Challenge proposals are bounded, proposal-only, non-recursive, and do not change the durable KB until the primary conversation resolves them.
- The public release check excludes real data, manifests, runtime logs, local config, absolute paths, and credential-shaped content.
