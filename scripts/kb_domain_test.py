"""Behavioral checks through the daily CLI, using only isolated fixture records."""
from __future__ import annotations

import base64
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

import kb_test_guard
_TEST_GUARD = kb_test_guard.activate(__file__) if __name__ == "__main__" else None

import kb
import kb_domain
import kb_evidence
import kb_lib


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="domain-fixture-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "kb"
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({
            "storage": {"root": str(self.root), "records": "records", "runtime": "runtime",
                        "cache": "cache", "retained_files": "retained-files", "manifests": "manifests"},
            "search": {"expand_queries": False}, "cleanup": {"auto_merge_mode": "off"},
        }))
        self.env = patch.dict(os.environ, {"PERSONAL_KB_ROOT": str(self.root),
                                          "PERSONAL_KB_CONFIG": str(self.config)}, clear=False)
        self.env.start()
        self.cwd = Path.cwd()
        os.chdir(self.base)
        self.project = self.row("p", "project", "map", aliases=["灯塔系统", "lighthouse"])
        self.save(self.project)

    def tearDown(self):
        os.chdir(self.cwd)
        self.env.stop()
        self.temporary.cleanup()

    def row(self, entity, typ, kind, *, project="p", repo="backend", aliases=None, state="confirmed",
            scope=None, relations=None, title=None, story=None):
        evidence = self.base / f"{entity.replace(':', '-')}.md"
        evidence.write_text(f"Fixture authority for {entity}\n", encoding="utf-8")
        entry = {"id": "entry-" + entity, "repo": repo, "branch": "main", "kind": kind,
                 "title": title or entity, "story": story or f"Detailed evidence for {entity}",
                 "aliases": aliases or [entity + " alias", entity + " lookup"],
                 "trigger_terms": [entity, "fixture", "domain"], "source_paths": [str(evidence)],
                 "status": "current", "ts": "2026-01-01T00:00:00+00:00",
                 "workspace_dir": str(self.base), "schema_version": 2,
                 "domain": {"entity_id": entity, "entity_type": typ, "project_id": project,
                            "state": state, "scope": scope or {}, "relations": relations or []}}
        entry["evidence_snapshots"] = kb_evidence.capture_evidence_snapshots(entry, self.base)
        entry["record_rev"] = kb_evidence.canonical_entry_revision(entry)
        return entry

    def save(self, entry):
        path = self.root / "records" / entry["repo"] / "main" / "kb.jsonl"
        kb_lib.append_jsonl(path, entry)

    def edge(self, typ, target, *, state="confirmed", scope=None):
        return {"type": typ, "target": target, "state": state, "scope": scope or {},
                "evidence_refs": [{"type": "conversation", "value": "Explicit fixture confirmation"}]}

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = kb.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def query(self, text="灯塔系统", *args):
        code, out, err = self.call("retrieve", text, "--repo", "backend", "--branch", "main",
                                   "--no-session-briefs", "--no-plan", "--limit", "10", "--json", *args)
        self.assertEqual(code, 0, err or out)
        return json.loads(out)

    def remember(self, entry):
        data = {key: value for key, value in entry.items()
                if key not in {"id", "repo", "branch", "workspace_dir", "schema_version",
                               "evidence_snapshots", "record_rev", "ts"}}
        payload = base64.b64encode(json.dumps(data).encode()).decode()
        return self.call("remember", "--repo", entry["repo"], "--branch", "main",
                         "--entry-b64", payload, "--no-auto-compact")

    def ids(self, payload):
        return {e["domain"]["entity_id"] for e in payload["items"] if e.get("domain")}

    def test_legacy_records_still_validate(self):
        old = copy.deepcopy(self.project)
        old.pop("domain")
        self.assertEqual(kb_domain.domain_errors(old), [])
        self.assertTrue(kb_lib.validate_entry_fields(old)[0])

    def test_cross_repo_content_and_sources_survive(self):
        repository = self.row("client", "repository", "map", repo="client")
        repository["domain"]["repo_id"] = "client"
        self.save(repository)
        requirement = self.row("subtitles", "requirement", "requirement", repo="client",
                               title="字幕连续显示", relations=[self.edge("applies_to", "client")])
        self.save(requirement)
        result = self.query("灯塔系统 字幕连续显示")
        hit = next(e for e in result["items"] if e["entry_id"] == requirement["id"])
        self.assertTrue(hit["same_project"])
        self.assertEqual(hit["summary"], requirement["story"])
        self.assertEqual(hit["source_paths"], requirement["source_paths"])
        self.assertEqual(result["domain_context"]["project_id"], "p")

    def test_scoped_replacement_does_not_cancel_main_requirement(self):
        self.save(self.row("old", "requirement", "requirement"))
        self.save(self.row("new", "requirement", "requirement", scope={"branch": "experiment"},
                           relations=[self.edge("supersedes", "old", scope={"branch": "experiment"})]))
        self.assertIn("old", self.ids(self.query()))
        self.assertNotIn("new", self.ids(self.query()))
        experimental = self.query("灯塔系统", "--branch", "experiment")
        self.assertIn("new", self.ids(experimental))
        self.assertNotIn("old", self.ids(experimental))
        history = self.query("灯塔系统 old", "--branch", "experiment", "--include-noncurrent")
        self.assertIn("old", self.ids(history))

    def test_candidate_and_conflicted_edges_never_replace(self):
        self.save(self.row("old", "requirement", "requirement"))
        for state in ("candidate", "conflicted"):
            self.save(self.row(state, "requirement", "requirement", state=state,
                               relations=[self.edge("supersedes", "old")]))
        self.save(self.row("proposal", "requirement", "requirement",
                           relations=[self.edge("supersedes", "old", state="candidate")]))
        ids = self.ids(self.query())
        self.assertIn("old", ids)
        self.assertNotIn("candidate", ids)
        self.assertNotIn("conflicted", ids)

    def test_same_alias_remains_ambiguous(self):
        self.save(self.row("other", "project", "map", project="other", aliases=["灯塔系统", "other-name"]))
        result = self.query()
        self.assertEqual(result["domain_context"]["state"], "ambiguous")
        self.assertFalse(any(e.get("same_project") for e in result["items"]))
        self.assertFalse(any("project-graph" in e["query_groups"] for e in result["items"]))

    def test_explicit_project_disambiguates(self):
        self.save(self.row("other", "project", "map", project="other", aliases=["灯塔系统", "other-name"]))
        self.assertEqual(self.query("灯塔系统", "--project-id", "p")["domain_context"]["project_id"], "p")

    def test_invalid_reference_write_is_rejected_without_change(self):
        bad = self.row("bad", "requirement", "requirement", relations=[self.edge("applies_to", "absent")])
        before = {str(p): p.read_bytes() for p in self.root.rglob("kb.jsonl")}
        code, _, err = self.remember(bad)
        self.assertEqual(code, 3, err)
        self.assertIn("resolve", err)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("kb.jsonl")})

    def test_incompatible_relation_and_cross_project_are_rejected(self):
        self.save(self.row("other", "project", "map", project="other"))
        for target in ("p", "other"):
            row = self.row("new", "requirement", "requirement", relations=[self.edge("supersedes", target)])
            self.assertTrue(kb_domain.reference_errors(row))

    def test_replacement_cycle_is_rejected(self):
        first = self.row("first", "requirement", "requirement")
        self.save(first)
        self.save(self.row("second", "requirement", "requirement", relations=[self.edge("supersedes", "first")]))
        first["domain"]["relations"] = [self.edge("supersedes", "second")]
        self.assertTrue(any("cycle" in error for error in kb_domain.reference_errors(first)))

    def test_time_and_environment_are_checked(self):
        future = self.row("future", "requirement", "requirement")
        future["domain"]["valid_from"] = "2999-01-01"
        self.save(future)
        expired = self.row("expired", "requirement", "requirement")
        expired["domain"]["valid_until"] = "2000-01-01"
        self.save(expired)
        self.save(self.row("lab", "requirement", "requirement", scope={"environment": "lab"}))
        self.assertFalse({"future", "expired", "lab"} & self.ids(self.query()))
        self.assertIn("lab", self.ids(self.query("灯塔系统", "--environment", "lab")))

    def test_changed_evidence_cannot_authorize_replacement(self):
        self.save(self.row("old", "requirement", "requirement"))
        newer = self.row("new", "requirement", "requirement", relations=[self.edge("supersedes", "old")])
        self.save(newer)
        Path(newer["source_paths"][0]).write_text("Changed authority, requiring review")
        result = self.query()
        self.assertIn("old", self.ids(result))
        old = next(e for e in result["items"] if e.get("domain", {}).get("entity_id") == "old")
        self.assertEqual(old["applicability_state"], "needs_review")

    def test_index_is_derived_and_queries_do_not_write(self):
        code, out, err = self.call("project-index")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["indexed_records"], 1)
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertTrue(self.query()["domain_context"]["cache_hit"])
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.save(self.row("added", "requirement", "requirement"))
        self.assertFalse(self.query()["domain_context"]["cache_hit"])
        self.assertIn("added", self.ids(self.query()))

    def test_remember_then_restore_without_similarity_merge(self):
        old = self.row("old", "requirement", "requirement", title="字幕策略", story="原有字幕策略")
        self.save(old)
        newer = self.row("new", "requirement", "requirement", title="字幕策略", story="实验字幕策略",
                         scope={"branch": "experiment"}, relations=[self.edge("supersedes", "old")])
        code, out, err = self.remember(newer)
        self.assertEqual(code, 0, err or out)
        rows = kb_lib.read_jsonl(self.root / "records" / "backend" / "main" / "kb.jsonl")
        self.assertEqual(len(rows), 3)
        self.assertEqual(next(e for e in rows if e["id"] == old["id"])["status"], "current")
        self.assertIn("new", self.ids(self.query("灯塔系统", "--branch", "experiment")))
        self.assertIn("old", self.ids(self.query()))

    def test_project_command_respects_limits_and_filters(self):
        self.save(self.row("req", "requirement", "requirement"))
        code, out, err = self.call("project", "灯塔系统", "--repo", "backend", "--branch", "main",
                                   "--no-session-briefs", "--kind", "requirement", "--limit", "1", "--json")
        self.assertEqual(code, 0, err or out)
        result = json.loads(out)
        self.assertEqual(result["project_view"]["coverage"], "bounded_retrieval")
        self.assertEqual(result["hit_count"], 1)
        self.assertEqual(result["items"][0]["kind"], "requirement")
        self.assertEqual(self.query("灯塔系统", "--tags", "not-present")["items"], [])

    def test_malformed_domain_is_a_validation_error(self):
        for value in (None, [], {"entity_type": []}, {"entity_type": "project", "state": "confirmed"}):
            row = copy.deepcopy(self.project)
            row["domain"] = value
            self.assertTrue(kb_domain.domain_errors(row))

    def test_no_domain_preserves_legacy_path(self):
        result = self.query("灯塔系统", "--no-domain")
        self.assertNotIn("domain_context", result)
        self.assertFalse(any(e.get("same_project") for e in result["items"]))

    def test_distinct_entity_history_survives_title_deduplication(self):
        self.save(self.row("first", "requirement", "requirement", title="同一字幕要求"))
        self.save(self.row("second", "requirement", "requirement", title="同一字幕要求",
                           relations=[self.edge("supersedes", "first")]))
        history = self.query("灯塔系统", "--include-noncurrent")
        self.assertTrue({"first", "second"}.issubset(self.ids(history)))
        old = next(e for e in history["items"] if e.get("domain", {}).get("entity_id") == "first")
        self.assertEqual(old["applicability_state"], "historical_or_out_of_scope")

    def test_duplicate_identity_write_is_rejected(self):
        duplicate = copy.deepcopy(self.project)
        duplicate["repo"] = "another-repo"
        code, _, err = self.remember(duplicate)
        self.assertEqual(code, 3, err)
        self.assertIn("already belongs", err)

    def test_domain_identity_cannot_be_removed_or_renamed_by_update(self):
        for domain in (None, {**self.project["domain"], "entity_id": "renamed"}):
            patch_value = base64.b64encode(json.dumps({"domain": domain}).encode()).decode()
            code, _, err = self.call("update", self.project["id"], "--repo", "backend", "--branch", "main",
                                      "--entry-b64", patch_value, "--expected-rev", self.project["record_rev"])
            self.assertEqual(code, 3, err)
            self.assertIn("immutable", err)

    def test_domain_mode_errors_are_structured(self):
        for extra in (("--no-domain", "--project-id", "p"), ("--project-id", "missing")):
            code, out, _ = self.call("retrieve", "灯塔系统", "--repo", "backend", "--json", *extra)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)["error"]["code"], "invalid_request")

    def test_broken_cache_is_rebuilt_in_memory(self):
        self.call("project-index")
        cache = self.root / "cache" / "domain-index.json"
        cache.write_text("[]")
        self.assertEqual(self.query()["domain_context"]["state"], "resolved")
        self.assertEqual(cache.read_text(), "[]")

    def test_stale_alias_owner_does_not_make_an_ambiguous_name_unique(self):
        other = self.row("other", "project", "map", project="other", aliases=["灯塔系统", "other-name"])
        self.save(other)
        Path(other["source_paths"][0]).write_text("Evidence needs recheck")
        self.assertEqual(self.query()["domain_context"]["state"], "ambiguous")


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DomainTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(_TEST_GUARD.run(main))
