"""Bounded project resolution and one-hop retrieval over domain records."""
from __future__ import annotations

import re
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
                 project_id: str, verify: Callable[[dict], dict]):
        graph = kb_domain.load_graph()
        self.graph = graph
        self.query = query
        self.verify = verify
        self.scope = {"repo_id": repo, "branch": branch, "environment": environment}
        grouped: dict[str, list[dict]] = {}
        for entry in graph["entries"]:
            grouped.setdefault(entry["domain"]["entity_id"], []).append(entry)
        self.entities = {key: rows[0] for key, rows in grouped.items() if len(rows) == 1}
        self.duplicate_entities = sorted(key for key, rows in grouped.items() if len(rows) != 1)
        declared_projects = {key: e for key, e in self.entities.items()
                             if e["domain"]["entity_type"] == "project" and kb_domain.active(e, self.scope)}
        self.projects = {key: e for key, e in declared_projects.items() if self.current(e)}
        self.state = "unresolved"
        self.project_id = ""
        self.matches: list[str] = []
        if project_id:
            if project_id not in self.projects:
                raise ValueError("--project-id must name a current, evidenced project entity")
            self.matches = [project_id]
        else:
            self.matches = sorted(key for key, e in declared_projects.items()
                                  if any(mentions(query, str(name))
                                         for name in [key, *e.get("aliases", [])]))
            if not self.matches and repo:
                self.matches = sorted({e["domain"]["project_id"] for e in self.entities.values()
                                       if e["domain"].get("repo_id") == repo and self.current(e)
                                       and e["domain"]["entity_type"] == "repository"
                                       and e["domain"]["project_id"] in self.projects})
        if len(self.matches) == 1 and self.matches[0] in self.projects:
            self.project_id = self.matches[0]
            self.state = "resolved"
        elif len(self.matches) > 1:
            self.state = "ambiguous"
        elif self.matches:
            self.state = "needs_recheck"
        self.replaced: dict[str, str] = {}
        self.uncertain_replacements: dict[str, str] = {}
        for e in self.entities.values():
            if not kb_domain.active(e, self.scope):
                continue
            for edge in e["domain"].get("relations", []):
                target = self.entities.get(edge["target"])
                if (edge["type"] == "supersedes" and target
                        and target["domain"]["project_id"] == e["domain"]["project_id"]
                        and target["domain"]["entity_type"] == e["domain"]["entity_type"]
                        and kb_domain.declaration_active(edge, self.scope)):
                    mapping = self.replaced if self.current(e) else self.uncertain_replacements
                    mapping[edge["target"]] = e["domain"]["entity_id"]

    def current(self, entry: dict) -> bool:
        return kb_domain.active(entry, self.scope) and self.verify(entry).get("state") == "fresh"

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
        return (d["project_id"] in self.projects and kb_domain.active(entry, self.scope)
                and d["entity_id"] not in self.replaced)

    def same_project(self, entry: dict) -> bool:
        d = entry.get("domain", {})
        return bool(self.project_id and d.get("project_id") == self.project_id
                    and self.eligible(entry) and self.current(entry))

    def related(self, seed_record_ids: set[str], *, limit: int,
                include_noncurrent: bool = False) -> list[tuple[dict, dict]]:
        if not self.project_id or limit <= 0:
            return []
        entries = [e for e in self.entities.values()
                   if e["domain"]["project_id"] == self.project_id
                   and self.eligible(e, include_noncurrent=include_noncurrent) and self.current(e)]
        project = self.projects[self.project_id]
        names = [self.project_id, *project.get("aliases", [])]
        residual = self.query.casefold()
        for name in sorted(names, key=len, reverse=True):
            residual = residual.replace(str(name).casefold(), " ")
        terms = [t for t in re.findall(r"[a-zA-Z0-9_./:-]{2,}|[\u4e00-\u9fff]{2,}", residual) if t]

        def relevance(entry: dict) -> int:
            fields = [entry.get("title", ""), *entry.get("aliases", []), *entry.get("trigger_terms", [])]
            # Match stored phrases inside natural Chinese wording as well as query tokens.
            return sum(1 for value in fields if mentions(residual, str(value))) + sum(
                1 for term in terms if any(term in str(value).casefold() for value in fields))

        topical = {e["domain"]["entity_id"] for e in entries if relevance(e) > 0}
        seeds = {e["domain"]["entity_id"] for e in entries
                 if e.get("id") in seed_record_ids or e["domain"]["entity_id"] in topical}
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
                related[d["entity_id"]] = (e, {"type": "entity_match", "entity_id": d["entity_id"]})
            for edge in d.get("relations", []):
                target = self.entities.get(edge["target"])
                if (not target or target["domain"]["project_id"] != self.project_id
                        or not kb_domain.declaration_active(edge, self.scope)):
                    continue
                if d["entity_id"] in seeds and self.eligible(target) and self.current(target):
                    related.setdefault(edge["target"], (target, {"type": edge["type"], "from": d["entity_id"],
                                                               "to": edge["target"]}))
                if edge["target"] in seeds:
                    related.setdefault(d["entity_id"], (e, {"type": edge["type"], "from": d["entity_id"],
                                                            "to": edge["target"]}))
        return sorted(related.values(), key=lambda pair: (-relevance(pair[0]), pair[0]["domain"]["entity_id"]))[:limit]

    def payload(self) -> dict[str, Any]:
        return {"state": self.state, "project_id": self.project_id, "matched_projects": self.matches,
                "scope": self.scope, "cache_hit": self.graph["cache_hit"],
                "indexed_records": len(self.graph["entries"]),
                "invalid_records": self.graph["invalid_count"],
                "uncertain_replacement_count": len(self.uncertain_replacements),
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
