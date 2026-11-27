# Personal KB iteration notes

## 2026-10-18: lightweight domain, relationship, and retrieval slice

Implemented an optional domain model in the existing six-kind records. Two local
Python modules define and validate entity identity, project membership, typed
evidenced relationships, time/scope, and one-hop retrieval. Existing daily commands
remain; `project` adds a bounded project view and `project-index` explicitly builds
a derived cache. The runtime has no new service, LLM call, embedding, or database.

The write path checks references, identity uniqueness, compatible types, and
replacement cycles. Domain entities avoid similarity-based merging; cross-bucket
writers serialize identity checks. Scoped replacement leaves other branches and
environments intact. Candidate/conflicted/stale declarations cannot authorize
replacement. Matching local evidence remains a snapshot check, not field acceptance.
Queries remain read-only, and evidence verification is reused within an invocation.
Write telemetry now follows the configured runtime directory so isolated tests do
not append to an unrelated global log.

Validation: 22 new behavioral tests cover write/read integration, cross-repository
content and evidence locators, partial replacement, ambiguous aliases, duplicate
IDs, invalid references, cycles, expired/future/environment scopes, stale evidence,
cache invalidation, historical views, filters, and read-only behavior. Existing
retrieval, reliability, evidence, storage, lifecycle, audit, quality, and release
regressions passed. The 54-case deterministic routing gate passed; its scores do
not measure real adoption or task benefit. The clean allowlisted release passed
root-layout smoke and sensitive-data checks. Source checkout bytecode is excluded
from the published tree rather than being treated as a shipped artifact.

The existing 19-case counterfactual retrieval suite was also compared against the
installed baseline on the same pre-update corpus. Both versions had the same
26.32% contract pass rate and 0% required-hit case pass rate, with no production
file changes. Its strict retrieval gate remains failed. This iteration does not
claim to repair those existing corpus/gold-specific recall gaps; the new domain
slice is separately validated by its synthetic behavioral cases and incremental
project registration. A CLI exit code of zero for the non-strict evaluator is not
reported as a passing retrieval gate.

A small isolated performance sample used 1,000 unrelated legacy rows and nine
domain rows, with 12 samples per mode, alternating mode order. Timing covered new
CLI process start through complete JSON output, with the OS file cache warm. The
lexical median was 392.919 ms and the indexed-domain median 365.903 ms; observed
ranges overlapped widely (264.419-947.924 ms and 268.576-1008.649 ms). This does
not establish a speedup or a general latency bound. The domain path returned the
intended sibling-repository record; the lexical baseline did not. Output grew from
1,488 to 6,206 JSON characters because more detailed candidates were returned;
callers can cap items and total characters. The derived index was 7,895 bytes.
One uncached sample was 962.232 ms and did not create a cache. This fixture is not
an actual-user effectiveness evaluation or a whole-corpus scalability benchmark.

Deployment retains local configuration and machine-specific code substitutions.
Back up changed installed files, apply the candidate delta, verify portable code
and model fingerprints, then validate through the actual WSL entry point. Real
knowledge is registered incrementally after evidence review; no bulk inference of
project identities or production corpus migration is performed. Public Git contains
code, schema, synthetic tests, and documentation only.

The slice supplies domain semantics, explicit relationships, and retrieval. It does
not yet provide reliable host hooks, automatic capture of all changes, arbitrary
graph traversal, complete project inventory, or proven long-term self-evolution.
See [the runtime contract](../../../references/domain-model.md).

### 2026-10-21: optional process-local latency diagnostics

后续已确认实施的范围、对抗审阅取舍与分阶段验收见
[延迟与误召回治理实施方案](latency-and-retrieval-plan-20261021.md)。

该方案已先记录后实施：Git 状态检查失败按不可核验处理；计时增加脱敏
重复率；检索内仅复用完整 OID 的成功事实；主体与主题共同筛选候选。
隔离验证及三轮交错对照已完成，数字与边界见方案和
[脱敏验证数据](evals/latency-20261021.json)。更大范围批处理和 closeout
重写仍未实施，不改变宽松触发阈值。
canonical WSL 安装现已备份更新，portable 指纹与已测候选一致，安装入口
24 项优化与 8 项计时回归再次通过。具体版本、回滚和未覆盖项见方案末尾。

Added `kb.py <command> ... --timings`. The stable wrapper emits one structured
stderr report with import/command stages, retrieval search/collection/domain
stages, evidence/cache/snapshot counters, fixed Git-subcommand aggregates, and
closeout build/adoption/brief/append stages. Storage append separates lock wait,
safety checks, write/close, and optional index-hook work. Timings are inclusive;
they do not measure Windows/WSL startup or interpreter bootstrap. Default output,
ranking, evidence checks, and durable/runtime record schemas are unchanged.
See [runtime diagnostics](../../../references/runtime-calls.md).

A controlled read-only replay against the same corpus returned the same result,
order, and freshness with and without instrumentation. One candidate sample took
7,352 ms inside the wrapper: five entry checks, eleven snapshots, four entry-cache
hits, and 128 Git calls. Evidence verification accounted for 6,539 ms. This is a
single warm-environment sample, not a latency percentile or a speedup claim.
The observation motivates investigating per-request reuse of common repository
facts; no caching, verification skipping, or routing change was implemented.

Eight focused timing tests cover default output, read-only retrieval equivalence,
parse/value errors, receipt failure, actual isolated closeout append, storage
failure, wrapper rejection, and closed-stderr/context cleanup. Twelve existing
focused suites (including evidence, retrieval, adoption, storage, domain, audit,
sensitive-data, quality, and smoke) passed against a staged runtime. Production
records/runtime/manifests/retained files were checked by SHA-256, size, and mtime.
The canonical WSL runtime was backed up and updated while preserving local
substitutions/configuration, then its timing suite and portable fingerprint were
verified. One installed-entry isolated closeout measured 33 ms inside the wrapper
and 99 ms including Python launch, with an empty temporary runtime; this does not
explain the historical production closeout's full duration.

## 2026-10-22: Monthly audit implementation

Implemented task-topic filtering for unrelated maps/requirements while preserving
explicit locator/material queries; fixed structured closeout's ignored memory
review/unresolved attempts and lost empty telemetry. Added frozen sanitized
monthly inputs/gold, public contract tests, and a production guard for Cursor
review tests. Shortened the Skill entry and moved environment-specific entry
instructions into a reference. See [implementation and evidence boundaries](iteration-20261022.md).

Validated 31 staged runtime test entrypoints, 13 public test entrypoints, the
15-case closeout contract, 30 monthly cases and 54 deterministic routing cases.
The old 19 retrieval cases remain 5/19 before/after, with identical final hit
IDs, so their unmet required-hit checks remain explicit. Updated the actual WSL
runtime and Windows discovery/routing instructions with backup and per-file
hashes; preserved local configuration and protected corpus files. At the final check, two shared runtime logs had append-only events from another project with their original byte-prefix hashes intact; those events were preserved.
No durable knowledge rows, remote publication or Git commits were made.


## 2026-10-23: Retrieval routing and independent task evaluation

Global/foreign RAG now retains historical evidence leads without current-project
promotion. Missing workspace buckets support read-only canonical discovery without
an index. Long queries reject sparse topic overlap; locator wording allows path
anchors but no longer bypasses the topic gate. Final validation passed 13 suites
and 10 route regressions; installed and tested portable fingerprints match.

Two independently curated task cohorts exposed additional false positives and
were preserved as diagnostic evidence. With actual corrections included, the
two-task follow-up completed 2/2 in both arms: no KB 438.1s, KB 615.6s;
raw input+output tokens 1,962,700 versus 2,827,837,
uncached input+output 188,876 versus 188,989.
Both needed one external correction and two observed internal rework cycles.
No net benefit is established. The last locator fix additionally has one fresh independently selected
current-corpus task probe; it does not establish general ROI. The comparison
uses historical 112-row corpora and explicit global context injection; autonomous
Skill trigger rate and general current-corpus ROI remain outside the claim.

The full evaluation report and per-task evidence were archived locally. The [routing regression tests](../../../scripts/kb_route_precision_test.py) exercise the published behavior.
No source commit, push or business deployment was made.


## 2026-10-23: Shared CLI configuration trust side effect

The benchmark used separate workspaces but inherited the active user Codex
state directory. Its execution workflow persisted 30 temporary project trust
entries (29 completed task runs and one excluded pilot) in Windows config.toml.
This was an unintended benchmark side effect, not a Personal KB requirement.
Statements implying no global config changes were too broad. The native writer
was not instrumented, so do not claim its precise internal mechanism.

Backed up the current configuration, removed only provenance-verified temporary
trust blocks, and verified all other project entries and settings were unchanged.
Legacy worker and grader entrypoints now fail before creating a workspace or
launching a model. Re-enable only after disposable execution-state isolation is
implemented and verified; separate -C roots and --ephemeral were insufficient.
Production Personal KB retrieval is unchanged. Cleanup receipt and trust-only
diff are in the routing-blind-eval evidence directory.

## 2026-11-18 global retrieval feedback and relevance

The global agent instructions now request `retrieve --brief`. This read-only view preserves ordered candidates, warnings, conclusions, provenance and strict closeout fields. Full JSON and optional full receipts remain available. Repository path components and basenames no longer qualify as exact code evidence in multi-anchor topic checks. Two short curated Chinese triggers in the same title or alias and literal compound triggers can recover useful records in unsegmented queries.

Fixed-snapshot replay covered 88 real result payloads. Serialized tool output fell from 65,131 to 18,161 o200k_base tokens. In the independently labeled natural-language holdout, the timely-positive subset moved from 2/8 to 3/8. Three of five separate prior wrong-target/mechanism cases stopped returning the rejected record. Verification included 24 test entry points, 30 frozen monthly cases and 54 routing simulator cases. These measurements concern output and retrieval. Future task adoption requires new session evidence.

## 2026-11-18 Dated project knowledge and recall

The global runtime now stores dated, evidenced observations on stable entities in existing records. File retention supplies evidence IDs. The daily wrapper supports timeline, focused decisions and action lookup. Closeout can record a verified memory gap for later replay. Existing rows require no migration.

In an isolated replay the original compound reset query recovered its decision, and a delivery-action query recovered the client rendering experience. A same-day gate deployment replay returned the earlier version at 16:30 and the later version at 17:00. The 18 previously labelled cases kept their previous pass/fail results. The old corpus copy still contains missing and outdated evidence. Subsequent live adoption and total token savings remain unmeasured.

## 2026-11-21 project names, independent tickets and creative practice

Focused lookups can carry a caller-selected object named in the full task. The WSL runtime and both discovery documents use this behavior. The evaluation covered two independent tickets, a dated project alias, a device project locator and a shared visual workflow. The original ticket and OCR evidence were retained locally.

Twelve fixed query cases met their expected first-hit or empty-result condition in the installed runtime. Three additional queries from their actual project directories also returned the expected first result. The visual workflow uses one feedback table with scope for the character, work and shot. The acceptance report, changes and detailed results were archived locally.

## 2026-11-22 workspace and feature evidence for project selection

The daily retrieval command now accepts a current file and exposes a bounded project decision in brief, full, and text output. It consults project names, existing record vocabulary, the task directory, and local project files. An ambiguous or conflicting match can be shown to the user before acting. A single choice stays in conversation unless independent evidence establishes a durable name or relationship. The public project/component schema and the bidirectional synonym table continue to supply their respective evidence.

On November 22 the installed version met the expected project decision on 13 of 13 actual user turns, compared with 3 of 13 before the change. Four lookups also returned historical items. Tests for new projects, active files, confirmed children, general questions and existing historical scopes passed. Run `python3 scripts/kb_domain_test.py` for the portable fixtures covering file evidence, competing projects, linked children and the withdrawal of a project name.

## 2026-11-24 source location and branch applicability

Maintenance candidate for phase A. An exact ticket can return a source locator when the owner or its project has changed evidence. The receipt distinguishes location from adoption and carries the record revision. The branch review uses verified scoped replacements and the target branch for ordinary and dated queries. The domain CLI tests cover changed authorities, scope, closeout effects and sibling-branch history. This phase was evaluated on isolated data.

## 2026-11-24 project names and outcome evidence

A unique current alias can locate an owner with changed evidence. The project router can independently confirm its Git checkout and branch. Source-only cards carry the revision and keep the original verification requirements. The daily instructions bring device- or client-specific retrieval before the first dependent decision, and tie a correction to the earlier owner. Structured closeout accepts an optional outcome pointer on a declared adoption.

## 2026-11-24 session curation and conversation references

An isolated replay exposed a user correction lost in a short excerpt and a conversation reference stored as a native session ID that a chat link could not resolve. Curation now gives answers first claim on the text budget and returns original line numbers. Retrieval preserves the chat URI through focused lookup and handles the session ID directly on the owning record. A link alone can locate the source. A substantive answer needs a separate topic match and current evidence. Session review and domain CLI regressions cover these paths, ambiguous wording, scope and historical cutoffs.

## 2026-11-26 daily retrieval receipts

For everyday use, typically one or two questions, answer accuracy and coverage of the evidence take priority over a small increase in the total tokens spent checking sources. Group adjacent project match terms by source when one checkout has been verified. Preserve their order, the source of every term, warnings and clarification questions. The daily entry uses `--compact`. Callers that consume per-term objects can keep using `--brief`.

## 2026-11-27 · 已知出处的单点问题

用户确认日常查询优先核对当前对话或可直接定位的现行出处。仅当历史可能影响结论或后续操作时启动检索；对历史决定、旧名与证据冲突继续主动取证。新知识的维护独立判断，实际检索或维护后统一收尾。触发条件写入 Skill 发现描述和正式入口，适用于正常模式。

同日在 WSL 与 Windows 的 Codex 安装入口更新了 Skill 和路由评估。已有 54 个模拟用例及 7 个针对当前来源、历史与明确记忆请求的边界用例全部符合预期，公开安装包通过 9 项本地检查。实际 CLI 问答中，网关协议与独立进度都能直接依给定证据作答。审核规则同时涉及触发时序和放行门槛时，中档两次均未明确回答单次是否须审满五轮，最高档两次答清，共享提示词据此将此类复合判断留在高档。WSL 的 Cursor 已有链接指向同一份 Skill，Cursor 的独立路由配置沿用原值。
