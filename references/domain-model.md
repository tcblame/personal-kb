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
