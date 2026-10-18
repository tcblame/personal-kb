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
