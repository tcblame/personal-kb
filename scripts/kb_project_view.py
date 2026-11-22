"""Bounded project resolution and one-hop retrieval over domain records."""
from __future__ import annotations

import re
from datetime import datetime
from itertools import islice
from typing import Any, Callable

import kb_domain


def mentions(query: str, value: str) -> bool:
    value = value.strip().casefold()
    if len(value) < 2:
        return False
    if value.isascii():
        return bool(re.search(r"(?<![\w])" + re.escape(value) + r"(?![\w])", query.casefold()))
    return value in query.casefold()


class ProjectContext:
    def __init__(self, query: str, *, repo: str, branch: str, environment: str,
                 project_id: str, verify: Callable[[dict], dict], as_of: datetime | None = None,
                 graph: dict | None = None, clarify: bool = False):
        if graph is None:
            graph = kb_domain.load_graph()
        self.graph = graph
        self.query = query
        self.verify = verify
        self.as_of = as_of
        self.scope = {"repo_id": repo, "branch": branch, "environment": environment}
        grouped: dict[str, list[dict]] = {}
        for entry in graph["entries"]:
            grouped.setdefault(entry["domain"]["entity_id"], []).append(entry)
        self.entities = {key: rows[0] for key, rows in grouped.items() if len(rows) == 1}
        self.duplicate_entities = sorted(key for key, rows in grouped.items() if len(rows) != 1)
        self.projects = {key: e for key, e in self.entities.items()
                         if e["domain"]["entity_type"] == "project" and kb_domain.active(e, self.scope, now=as_of)}
        self.state = "unresolved"
        self.project_id = ""
        self.matches: list[str] = []
        if project_id:
            if not self._current_project(project_id):
                raise ValueError("--project-id must name a current, evidenced project entity")
            self.matches = [project_id]
        else:
            self.matches = sorted(key for key, e in self.projects.items()
                                  if any(mentions(query, str(name))
                                         for name in [key, *e.get("aliases", [])]))
            if clarify and len(self.matches) == 1:
                self.matches = []
            if not self.matches and repo and not clarify:
                self.matches = sorted({e["domain"]["project_id"] for e in self.entities.values()
                                       if e["domain"]["entity_type"] == "repository" and e["domain"].get("repo_id") == repo
                                       and self.current(e) and self._current_project(e["domain"]["project_id"])})
        if len(self.matches) == 1 and self._current_project(self.matches[0]):
            self.project_id = self.matches[0]
            self.state = "resolved"
        elif len(self.matches) > 1:
            self.state = "ambiguous"
        elif self.matches:
            self.state = "needs_recheck"
        self.replaced: dict[str, str] = {}
        self.uncertain_replacements: dict[str, str] = {}
        self.history_links: dict[str, list[dict]] = {}
        self._edges: dict[str, list[tuple[dict, dict, bool]]] = {}
        self._scoped_edges: list[tuple[str, dict]] = []
        self._checked_history: set[str] = set()
        for e in self.entities.values():
            if not kb_domain.active(e, self.scope, now=as_of):
                continue
            for edge in e["domain"].get("relations", []):
                target = self.entities.get(edge["target"])
                if (edge["type"] == "supersedes" and target
                        and target["domain"]["project_id"] == e["domain"]["project_id"]
                        and target["domain"]["entity_type"] == e["domain"]["entity_type"]
                        and kb_domain.declaration_active(edge, edge.get("scope", {}), now=as_of)):
                    applies = kb_domain.declaration_active(edge, self.scope, now=as_of)
                    source_id = e["domain"]["entity_id"]
                    for endpoint in (source_id, edge["target"]):
                        self._edges.setdefault(endpoint, []).append((e, edge, applies))
                    if applies:
                        self._scoped_edges.append((edge["target"], e))

    def _current_project(self, project_id: str) -> bool:
        project = self.projects.get(project_id)
        return project is not None and self.current(project)

    def check_history(self, entity_id: str) -> None:
        """Verify replacement evidence when an endpoint becomes a candidate."""
        if entity_id in self._checked_history:
            return
        for source, edge, applies in self._edges.get(entity_id, []):
            successor = source["domain"]["entity_id"]
            predecessor = edge["target"]
            fresh = self.current(source)
            if entity_id == predecessor and applies:
                mapping = self.replaced if fresh else self.uncertain_replacements
                mapping[entity_id] = successor
            if fresh:
                outgoing = entity_id == successor
                other = predecessor if outgoing else successor
                self.history_links.setdefault(entity_id, []).append({
                    "scope": {**source["domain"].get("scope", {}), **edge.get("scope", {})},
                    "effective_from": edge.get("valid_from", source["domain"].get("valid_from")),
                    "applies_here": applies, "relation": "supersedes" if outgoing else "superseded_by",
                    "entity_id": other, "entry_id": self.entities[other]["id"],
                })
        self._checked_history.add(entity_id)

    def current(self, entry: dict) -> bool:
        return kb_domain.active(entry, self.scope, now=self.as_of) and self.verify(entry).get("state") == "fresh"

    def eligible(self, entry: dict, *, include_noncurrent: bool = False) -> bool:
        if "domain" not in entry:
            return True
        if kb_domain.domain_errors(entry):
            return False
        d = entry["domain"]
        if d["entity_id"] not in self.entities:
            return False
        if self.project_id and d["project_id"] != self.project_id:
            return False
        if include_noncurrent:
            return True
        if not kb_domain.active(entry, self.scope, now=self.as_of) or not self._current_project(d["project_id"]):
            return False
        self.check_history(d["entity_id"])
        return d["entity_id"] not in self.replaced

    def same_project(self, entry: dict) -> bool:
        d = entry.get("domain", {})
        return bool(self.project_id and d.get("project_id") == self.project_id
                    and self.eligible(entry) and self.current(entry))

    def prefer_local_versions(self, candidates: dict) -> None:
        """An update confined to another repository cannot displace its local predecessor."""
        repo = self.scope.get("repo_id")
        if not repo:
            return
        for item in candidates.values():
            domain = item.get("domain", {})
            source = self.entities.get(domain.get("entity_id"), {})
            for edge in source.get("domain", {}).get("relations", []):
                owner = edge.get("scope", {}).get("repo_id")
                if edge["type"] != "supersedes" or edge["state"] != "confirmed" or not owner or owner == repo:
                    continue
                target = self.entities.get(edge["target"], {})
                local = candidates.get(target.get("id"))
                if (local and local.get("repo") == repo and item.get("repo") != repo and
                        target["domain"]["project_id"] == domain.get("project_id") and
                        target["domain"]["entity_type"] == domain.get("entity_type") and self.current(source) and self.current(target)):
                    item["_score"] = min(item["_score"], local["_score"] - 0.01)

    def related(self, seed_record_ids: set[str], *, limit: int,
                include_noncurrent: bool = False) -> list[tuple[dict, dict]]:
        if not self.project_id or limit <= 0:
            return []
        entries = [e for e in self.entities.values() if e["domain"]["project_id"] == self.project_id
                   and kb_domain.active(e, self.scope, now=self.as_of)]

        def verified(entry: dict) -> bool:
            return self.eligible(entry, include_noncurrent=include_noncurrent) and self.current(entry)

        project = self.projects[self.project_id]
        names = [self.project_id, *project.get("aliases", [])]
        residual = self.query.casefold()
        for name in sorted(names, key=len, reverse=True):
            residual = residual.replace(str(name).casefold(), " ")
        terms = [t for t in re.findall(r"[a-zA-Z0-9_./:-]{2,}|[\u4e00-\u9fff]{2,}", residual) if t]

        from kb_timeline import confirmed_names
        dated_names = {e["domain"]["entity_id"]: confirmed_names(e["domain"].get("observations", []))
                       for e in entries}

        def relevance(entry: dict) -> int:
            fields = [entry.get("title", ""), *entry.get("aliases", []), *entry.get("trigger_terms", []),
                      *dated_names[entry["domain"]["entity_id"]]]
            # Match stored phrases inside natural Chinese wording as well as query tokens.
            return sum(1 for value in fields if mentions(residual, str(value))) + sum(
                1 for term in terms if any(term in str(value).casefold() for value in fields))

        # Rank using metadata first. A stale seed must never authorize a neighbour.
        scores = {e["domain"]["entity_id"]: relevance(e) for e in entries}
        seed_rows = [e for e in entries if (scores[e["domain"]["entity_id"]] or e.get("id") in seed_record_ids)
                     and verified(e)]
        seeds = {e["domain"]["entity_id"] for e in seed_rows}
        named_owners = {key for key in seeds if any(mentions(residual, name) for name in dated_names[key])}
        alias_owners = {e["domain"]["entity_id"] for e in seed_rows if e["domain"]["entity_id"] != self.project_id
                        and any(len(str(alias).strip()) >= 4 and mentions(residual, str(alias))
                                for alias in e.get("aliases", []))}
        topical = any(scores[key] for key in seeds)
        if topical:
            # A concrete subject should not fan out from the project root to
            # every requirement just because the query also names the project.
            seeds.discard(self.project_id)
        if not seeds:
            seeds = {self.project_id}
        related: dict[str, tuple[dict, dict]] = {}
        for e in entries:
            d = e["domain"]
            # project_id is itself an explicit membership assertion. Restoring
            # a project must also work when no separate belongs_to edge was
            # redundantly recorded for each requirement.
            if self.project_id in seeds:
                related[d["entity_id"]] = (e, {"type": "project_member", "from": self.project_id,
                                               "to": d["entity_id"]})
            if d["entity_id"] in seeds:
                match_type = "entity_match"
                if named_owners == {d["entity_id"]}:
                    match_type = "dated_name"
                elif alias_owners == {d["entity_id"]}:
                    match_type = "entity_alias"
                related[d["entity_id"]] = (e, {"type": match_type, "entity_id": d["entity_id"]})
            for edge in d.get("relations", []):
                target = self.entities.get(edge["target"])
                if (not target or target["domain"]["project_id"] != self.project_id
                        or not kb_domain.declaration_active(edge, self.scope, now=self.as_of)):
                    continue
                if (d["entity_id"] in seeds and edge["target"] in scores
                        and (not include_noncurrent or self.eligible(target))):
                    related.setdefault(edge["target"], (target, {"type": edge["type"], "from": d["entity_id"],
                                                               "to": edge["target"]}))
                if edge["target"] in seeds:
                    related.setdefault(d["entity_id"], (e, {"type": edge["type"], "from": d["entity_id"],
                                                            "to": edge["target"]}))
        ranked = sorted(related.items(), key=lambda item: (-scores[item[0]], item[0]))
        return list(islice((pair for _, pair in ranked if verified(pair[0])), limit))

    def payload(self) -> dict[str, Any]:
        return {"state": self.state, "project_id": self.project_id, "matched_projects": self.matches,
                **({"as_of": self.as_of.isoformat()} if self.as_of else {}),
                "scope": self.scope, "cache_hit": self.graph["cache_hit"],
                "indexed_records": len(self.graph["entries"]),
                "invalid_records": self.graph["invalid_count"],
                "uncertain_replacement_count": len({key for key, source in self._scoped_edges if not self.current(source)}),
                "ambiguous_entity_count": len(self.duplicate_entities), "max_hops": 1}


def compact_domain(entry: dict) -> dict:
    d = entry.get("domain")
    if not isinstance(d, dict):
        return {}
    return {key: d[key] for key in ("entity_id", "entity_type", "project_id", "repo_id", "state",
                                   "scope", "valid_from", "valid_until") if key in d}


def project_view(items: list[dict], context: dict) -> dict:
    groups: dict[str, list[str]] = {}
    for item in items:
        d = item.get("domain", {})
        if d:
            groups.setdefault(d["entity_type"], []).append(item["entry_id"])
    return {"project_id": context["project_id"], "state": context["state"],
            "coverage": "bounded_retrieval", "groups": groups,
            "needs_review_ids": [item["entry_id"] for item in items
                                 if item.get("applicability_state") in {"needs_review", "historical_or_out_of_scope"}],
            "note": "Only returned, evidenced records are represented; this is not a complete project inventory."}
