# Project knowledge: domain model, relations, and retrieval

This optional local extension keeps the six existing record kinds. The definition
in `domain-model.json` describes entity and relation types; ordinary `kb.jsonl`
records remain the only writable fact source. Python checks the contract and
references. This is a limited domain model, not an OWL reasoner.

## Write verified objects and relationships

Use existing `remember --entry-b64` and `update --entry-b64 --expected-rev`.
Add the following object to a normal record only after verifying its identity,
scope, and supporting evidence:

```json
{
  "domain": {
    "entity_id": "requirement:subtitle-trial",
    "entity_type": "requirement",
    "project_id": "project:example",
    "state": "confirmed",
    "scope": {"repo_id": "example-client", "branch": "experiment"},
    "relations": [{
      "type": "supersedes",
      "target": "requirement:subtitle-current",
      "state": "confirmed",
      "scope": {"branch": "experiment"},
      "evidence_refs": [{"type": "conversation", "value": "Explicit confirmation of this experiment's scope"}]
    }]
  }
}
```

This is a structural example, not a real user requirement. Register the project
first (`entity_type=project`, `kind=map`, `entity_id=project_id`), then repositories
and other entities. Use project `aliases` for real-world names. Repository records
use `repo_id` matching the existing KB repo bucket; different repositories keep
different identities. A project ID establishes membership; separate `belongs_to`
edges express component hierarchy without being required on every record.

Each relation has a target entity ID, explicit state, and typed evidence references.
Allowed relations are `belongs_to`, `applies_to`, `supported_by`, `supersedes`, and
`migrated_from`. Targets must exist, have compatible types, and belong to the same
project. Cross-project links require a future explicit contract. Entity identity
is immutable after registration. Duplicate IDs, dangling references, self-links,
and replacement cycles are rejected. `candidate` and `conflicted` declarations
are preserved without becoming current facts.

`scope` supports `repo_id`, `branch`, and `environment`; these dimensions must match
the request. Unknown scope does not mean all scopes. Optional ISO
`valid_from`/`valid_until` bounds use a half-open UTC interval. Confirmed, evidenced
replacement edges suppress old entities only in the applicable scope. Do not
globally retire an old requirement for a partial replacement. Domain entities
bypass text-similarity merging: update their existing owner. Requirement
confirmation and implementation/deployment/acceptance stay separate attributes
and evidence. Neither a relationship nor a matching file hash proves field acceptance.
If replacement evidence becomes stale, the previous requirement is returned only
with `needs_review`; it is not silently restored as a current instruction. A stale
project alias owner still contributes to name ambiguity until reviewed.

## Retrieve or restore a project

For an applicable earlier decision, supply `retrieve QUERY --as-of YYYY-MM-DD`; a calendar day ends in Asia/Shanghai and an offset timestamp can select a narrower point. The same date applies to both entities and replacement edges. Scope a replacement to the project and branch in which it took effect. Keep the predecessor available for the old presentation or other explicitly supported scope.

```bash
python3 scripts/kb.py retrieve "example project subtitle requirements" --repo example-client --branch main --json
python3 scripts/kb.py project "example project" --project-id project:example --branch experiment --environment lab --json
python3 scripts/kb.py project-index
```

`retrieve` executes the existing lexical path first. It resolves one project from
an explicit ID, unambiguous aliases, or an evidenced repository mapping, then adds
project members and at most one relation hop. Multiple matching projects remain
ambiguous; expansion does not merge them. Only current records with freshly
matching local evidence authorize expansion or scoped replacement. Relevant
sibling-repository records retain their content and source paths; unrelated
project formatting retains the previous behavior.

Existing kind/tag/date/limit/character filters still apply. Expansion returns at
most 32 graph candidates; this bounds added output, not all index loading or
evidence-check work. A graph match identifies its path and still needs current
authority checks. `project` uses the same retrieval invocation and ID, adding a
grouped, bounded view. This is not a complete inventory and does not assert
deployment or acceptance. `--include-noncurrent` permits historical hints labelled
as inapplicable to the current scope. `--no-domain` selects the lexical baseline;
do not use that mode to enforce scoped requirements.

## Index, compatibility, and rollback

`project-index` explicitly rebuilds `cache/domain-index.json`. The index is a
derived copy keyed to record-file metadata and the domain definition. Queries read
a valid cache or rebuild in memory without saving. After a batch of domain writes,
rebuild once to avoid repeated scans. Evidence is still verified when the index is
unchanged. The cache is not an independent authority.

Legacy records need no migration; project membership is not guessed from similar
titles or repository names. Add metadata to verified records incrementally.
This version adds no daemon, embedding call, vector store, or background LLM.
The host must still trigger the Skill; it does not capture every conversation.

Back up changed installed code and preserve local config/data. Roll back by
restoring that code and ignoring/removing only the derived domain index. Optional
metadata remains readable by older code, but scoped replacement enforcement needs
this version. Review affected requirements before real work on an older runtime.

## Dated object history and work products

Use the existing project and owner row. A device, service, database or table is a
map entity with a stable ID. A physically replaced device gets another identity.
Static typed links include runs_on, uses_database and contains_table. The same
owner can also have dated observations; an optional target ID connects a dated
relationship to another registered entity in the project.

Keep one independent field per property or relationship slot. Use name for a
rename, alias for a business nickname, and distinct keys for concurrently
deployed services or multiple interfaces. Record only the precision supported
by the source: YYYY-MM, YYYY-MM-DD or an ISO timestamp with a UTC offset.
Each observation supplies field, value, date, state, basis and evidence_refs.
Use observed for a dated inspection, effective for a verified change, ended for
a confirmed removal. Candidate and conflicted are states. A recurring observation
with a later date adds a checkpoint. A byte-identical event is idempotent.

In the authorized task, keep the inventory, schema excerpt, relevant log window
or test report with the existing retain command. Reuse its returned asset ID on
retry. Keep credentials and file bodies out of observation values. A table seen
on a shared host requires project-specific evidence before recording ownership.
A symptom, diagnosis, deployed fix and acceptance test can have separate dates,
states and evidence. A meeting timestamp, a proposal and a confirmed requirement
can likewise be recovered independently. Keep an unresolved idea in an authorized
source artifact; its candidate label must survive retrieval.

Example observation file (replace the example asset with the real retained result):

```json
{"field":"name","value":"Front desk","date":"2026-11-06","basis":"effective","state":"confirmed","evidence_refs":[{"type":"retained","value":"asset_261106_001","path":"logs/inventory.txt"}]}
```

Use the daily wrapper from the active Skill:
```bash
python3 scripts/kb.py update ENTRY_ID --repo REPO --branch BRANCH --observation-file facts.json --expected-rev CURRENT_REV
python3 scripts/kb.py timeline 'Front desk' --project-id project:example --as-of 2026-11-10
python3 scripts/kb.py timeline device:front --project-id project:example --history --field name
```

A scoped owner needs the corresponding --repo, --branch or --environment.
Lookup accepts the ID, title, stored aliases or an evidenced historical name.
Ambiguous names return candidates; follow an observation's target with another
timeline lookup. Dates use Asia/Shanghai by default; use --timezone for another
calendar zone. An as-of result describes the last known value, including its
actual observation date. Overlapping imprecise dates with different values are
uncertain. Use --offset and --limit to page. History includes candidates and
observation IDs. To correct a wrong statement, append a confirmed observation
with the same field, evidence for the correction, and withdraws set to the bad
observation ID. Append the corrected fact separately with its authentic date.
The original and its withdrawal remain available in history. When a link points
to a retained asset, use --verify-assets to verify the retained bytes on that page.

These are optional fields on the existing JSONL rows. Old records work without
migration. Register identities only from current evidence; a short lexical
lookup remains appropriate for legacy material. Rolling back code leaves dated
data intact but older graph readers cannot interpret the extended owner rows.
