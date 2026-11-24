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

    def test_dated_observations_through_daily_commands(self):
        device = self.row("device:one", "device", "map")
        self.save(device)
        # Retain an actual byte stream and prove that an append can resolve it.
        source = self.base / "inventory.txt"
        source.write_text("old then new", encoding="utf-8")
        code, out, err = self.call("retain", "--path", str(source), "--project-key", "test",
                                   "--case-id", "inventory-261018", "--category", "logs", "--reason", "fixture")
        self.assertEqual(code, 0, err)
        asset = json.loads(out)
        ref = {"type": "retained", "value": asset["asset_id"], "path": "logs/inventory.txt"}
        def event(day, field, value, state="confirmed"):
            return dict(date=day, field=field, value=value, state=state, basis="observed", evidence_refs=[ref])
        old = event("2026-10", "name", "Old unit")
        new = event("2026-11-06", "name", "New unit")
        version = event("2026-11-15", "service:api", "v2")
        file = self.base / "observations.json"
        file.write_text(json.dumps([new, old, version, event("2026-11-07", "name", "maybe", "candidate")]))
        args = ("update", device["id"], "--observation-file", str(file), "--repo", "backend", "--branch", "main")
        code, out, err = self.call(*args, "--expected-rev", device["record_rev"])
        self.assertEqual(code, 0, err or out)
        self.assertNotEqual(self.call(*args, "--expected-rev", device["record_rev"])[0], 0)
        self.assertEqual(self.call(*args)[0], 0)
        def lookup(name, *options):
            code, out, err = self.call("timeline", name, "--project-id", "p", *options)
            self.assertEqual(code, 0, err)
            return json.loads(out)
        self.assertEqual(lookup("Old unit", "--as-of", "2026-11-10")["fields"][0]["observations"], [new])
        self.assertEqual(lookup("New unit", "--as-of", "2026-10-31")["fields"][0]["observations"], [old])
        self.assertEqual(lookup("Old unit", "--as-of", "2026-10-04")["fields"][0]["status"], "uncertain")
        self.assertEqual(lookup("New unit", "--history")["total"], 4)
        self.assertEqual(lookup("Old unit", "--as-of", "2026-09-30")["fields"], [])
        self.assertIn("device:one", self.ids(self.query("灯塔系统 Old unit")))
        self.assertFalse(lookup("New unit", "--verify-assets")["asset_errors"])
        self.assertEqual(lookup("maybe")["status"], "not_found")
        self.save(self.row("device:other", "device", "map", aliases=["Old unit", "other unit"]))
        self.assertEqual(lookup("Old unit")["status"], "ambiguous")
        Path(asset["stored_path"]).write_text("modified")
        self.assertEqual(lookup("New unit", "--verify-assets")["status"], "needs_review")
        file.write_text(json.dumps(event("2026-11-18", "service:api", "v3")))
        self.assertNotEqual(self.call(*args)[0], 0)
        self.assertEqual(lookup("New unit", "--history")["total"], 4)

    def test_withdrawn_fact_and_same_day_deployments(self):
        device = self.row("server", "device", "map")
        self.save(device)
        pointer = [{"type": "conversation", "value": "fixture transcript, verified inventory"}]
        wrong = dict(field="name", value="mistaken", date="2026-11-16", state="confirmed",
                     basis="observed", evidence_refs=pointer)
        import kb_timeline
        events = [wrong, dict(wrong, withdraws=kb_timeline.event_id(wrong)),
                  dict(wrong, value="box", date="2026-11-16T16:01:00+08:00"),
                  dict(wrong, value="unit", date="2026-11-16T16:53:47+08:00")]
        facts = self.base / "correction.json"
        facts.write_text(json.dumps(events))
        code, out, err = self.call("update", device["id"], "--observation-file", str(facts))
        self.assertEqual(code, 0, err or out)
        def lookup(name, stamp):
            code, out, err = self.call("timeline", name, "--project-id", "p", "--as-of", stamp)
            self.assertEqual(code, 0, err)
            return json.loads(out)
        self.assertEqual(lookup("mistaken", "2026-11-17")["status"], "not_found")
        self.assertEqual(lookup("unit", "2026-11-16T16:30:00+08:00")["fields"][0]["observations"][0]["value"], "box")
        self.assertEqual(lookup("box", "2026-11-16")["fields"][0]["observations"][0]["value"], "unit")

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

    def test_changed_project_evidence_can_locate_only_the_exact_ticket(self):
        ticket = self.row("ticket", "requirement", "requirement", aliases=["T022"], title="T022 old answer",
                          relations=[self.edge("supported_by", "neighbour")])
        self.save(ticket)
        self.save(self.row("neighbour", "evidence", "implementation"))
        Path(self.project["source_paths"][0]).write_text("new project evidence")
        for query, flags in [("T022", []), ("请查t022的依据", ["--brief"]),
                             ("T022", ["--project-id", "p", "--project-view"]),
                             ("请查T022", ["--as-of", "2026-11-19"])]:
            with self.subTest(query=query, flags=flags):
                result = self.query(query, *flags)
                self.assertEqual([i["entry_id"] for i in result["items"]], [ticket["id"]])
                item = result["items"][0]
                self.assertEqual(item["title"], "T022")
                self.assertEqual(item["applicability_state"], "locate_only")
                self.assertEqual(item["locator_evidence"], {"owner": "fresh", "project": "needs_recheck"})
                self.assertEqual(item["record_rev"], ticket["record_rev"])
                self.assertEqual(item["source_paths"][0], self.project["source_paths"][0])
                self.assertFalse(item.get("summary") or item.get("evidence_refs") or item.get("decision_history"))
                if "--as-of" in flags:
                    self.assertTrue(item["as_of"].startswith("2026-11-19"))
                if "--project-view" in flags:
                    self.assertEqual(result["project_view"]["needs_review_ids"], [ticket["id"]])
        for query in ("T0220", "AT022", "T022-A", "T022.1", "T022 与 T021"):
            with self.subTest(query=query):
                self.assertEqual(self.query(query)["items"], [])
        for flags in [("--repo", "wrong"), ("--branch", "other"), ("--global",),
                      ("--kind", "issue"), ("--tags", "missing"), ("--in", "summary")]:
            with self.subTest(flags=flags):
                self.assertFalse(any(i.get("applicability_state") == "locate_only"
                                     for i in self.query("T022", *flags)["items"]))

    def test_ticket_locator_requires_a_unique_active_owner_and_resolvable_sources(self):
        ticket = self.row("ticket", "requirement", "requirement", aliases=["T022"])
        ticket["domain"]["scope"] = {"environment": "lab"}
        ticket["domain"]["valid_from"] = "2026-09-01"
        self.save(ticket)
        Path(self.project["source_paths"][0]).write_text("project changed")
        self.assertEqual(self.query("T022")["items"], [])
        self.assertEqual(self.query("T022", "--environment", "lab", "--as-of", "2026-08-01")["items"], [])
        def lookup():
            return self.query("T022", "--environment", "lab")["items"]
        self.assertEqual(lookup()[0]["applicability_state"], "locate_only")
        authority = Path(ticket["source_paths"][0])
        authority.unlink()
        self.assertEqual(lookup(), [])
        authority.write_text("changed ticket evidence")
        self.save(self.row("another", "requirement", "requirement", aliases=["T022"]))
        self.assertEqual(lookup(), [])

    def test_ticket_locator_respects_fresh_and_uncertain_replacements(self):
        self.save(self.row("ticket", "requirement", "requirement", aliases=["T022"]))
        successor = self.row("next", "requirement", "requirement",
                             relations=[self.edge("supersedes", "ticket", scope={"branch": "main"})])
        self.save(successor)
        Path(self.project["source_paths"][0]).write_text("changed")
        self.assertEqual(self.query("T022")["items"], [])
        Path(successor["source_paths"][0]).write_text("change still under review")
        self.assertEqual(self.query("T022")["items"], [])

    def test_changed_file_does_not_mask_an_unresolvable_ticket_source(self):
        ticket = self.row("ticket", "requirement", "requirement", aliases=["T022"], title="T022")
        second = self.base / "second.md"
        second.write_text("initial authority")
        ticket["source_paths"].append(str(second))
        ticket["evidence_snapshots"] = kb_evidence.capture_evidence_snapshots(ticket, self.base)
        self.save(ticket)
        Path(ticket["source_paths"][0]).write_text("changed")
        second.unlink()
        self.assertEqual(kb_evidence.verify_entry_evidence(ticket)["state"], "unresolvable")
        self.assertEqual(self.query("T022")["items"], [])

    def test_locate_only_receipt_cannot_heat_a_fresh_child(self):
        ticket = self.row("ticket", "requirement", "requirement", aliases=["T022"])
        self.save(ticket)
        Path(self.project["source_paths"][0]).write_text("project changed")
        receipt = self.query("T022", "--brief")
        self.assertEqual(receipt["items"][0]["applicability_state"], "locate_only")
        data = self.base / "closeout.json"
        for effect in ("decide", "fix", "write", "legacy", "locate"):
            args = {"used_entry_ids": [ticket["id"]]} if effect == "legacy" else {"adoption_effects": {effect: [ticket["id"]]}}
            data.write_text(json.dumps({"retrieval_snapshot": receipt, **args}))
            code, out, err = self.call("closeout", "--json-file", str(data), "--repo", "backend", "--branch", "main")
            if effect == "locate":
                self.assertEqual(code, 0, err or out)
            else:
                self.assertNotEqual(code, 0)
                self.assertIn("locate_only", err or out)
        rows = kb_lib.read_jsonl(self.root / "runtime" / "closeout.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["adoption_effects"]["locate"], [ticket["id"]])
        self.assertEqual(rows[0]["heated_entry_ids"], [])

    def test_branch_scope_of_replacement_is_visible_in_daily_views(self):
        old = self.row("previous", "requirement", "requirement")
        new = self.row("revision", "requirement", "requirement",
                       relations=[self.edge("supersedes", "previous", scope={"repo_id": "backend", "branch": "feature"})])
        self.save(old)
        self.save(new)
        def by_id(result):
            return {i["entry_id"]: i for i in result["items"]}
        main = by_id(self.query("灯塔系统"))
        self.assertEqual(main[old["id"]]["applicability_state"], "current")
        self.assertEqual(main[new["id"]]["applicability_state"], "historical_or_out_of_scope")
        feature = by_id(self.query("灯塔系统", "--branch", "feature"))
        self.assertNotIn(old["id"], feature)
        self.assertEqual(feature[new["id"]]["applicability_state"], "current")
        for extra in [[], ["--brief"], ["--as-of", "2026-11-19"]]:
            result = self.query("灯塔系统", "--branch", "unknown", "--project-view", *extra)
            items = by_id(result)
            self.assertIn(new["id"], result["project_view"]["needs_review_ids"])
            for entry in (old, new):
                item = items[entry["id"]]
                self.assertEqual(item["applicability_state"], "historical_or_out_of_scope")
                self.assertEqual(item["branch"], "main")
                self.assertEqual(item["target_branch"], "unknown")
                self.assertEqual(item["freshness_state"], "fresh")
                self.assertIn("unknown", item["warning"])
            self.assertFalse(items[new["id"]]["decision_history"][0]["applies_here"])

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

    def test_as_of_applies_to_replacement_and_returned_observations(self):
        old = self.row("prior", "implementation", "implementation")
        self.save(old)
        newer = self.row("changed", "implementation", "implementation",
                         relations=[self.edge("supersedes", "prior")])
        newer["domain"]["valid_from"] = "2026-10-23T09:00:00+08:00"
        newer["domain"]["observations"] = [
            {"field": field, "value": value, "date": stamp, "state": "confirmed", "basis": "observed",
             "evidence_refs": [{"type": "conversation", "value": "fixture " + field}]}
            for field, value, stamp in [
                ("source", "candidate ready", "2026-10-23T09:00:00+08:00"),
                ("deployment", "service deployed", "2026-10-23T18:00:00+08:00")]]
        self.save(newer)
        early = self.ids(self.query("灯塔系统", "--as-of", "2026-09-16"))
        self.assertIn("prior", early)
        self.assertNotIn("changed", early)
        midday = self.query("灯塔系统", "--as-of", "2026-10-23T12:00:00+08:00")
        self.assertNotIn("prior", self.ids(midday))
        item = next(i for i in midday["items"] if i["entry_id"] == newer["id"])
        self.assertEqual(item["decision_history"][0]["entry_id"], old["id"])
        self.assertIn("candidate ready", item["summary"])
        self.assertNotIn("service deployed", item["summary"])
        end = self.query("灯塔系统", "--as-of", "2026-10-23")
        self.assertIn("service deployed", next(i["summary"] for i in end["items"] if i["entry_id"] == newer["id"]))

    def test_unrelated_project_and_replacement_skip_evidence_io(self):
        remote = "remote-project"
        self.save(self.row(remote, "project", "map", project=remote, repo="elsewhere", aliases=["外地项目"]))
        self.save(self.row("former", "requirement", "requirement", project=remote, repo="elsewhere"))
        edge = self.edge("supersedes", "former", scope={"repo_id": "elsewhere"})
        self.save(self.row("successor", "requirement", "requirement", project=remote, repo="elsewhere",
                           relations=[edge]))
        self.save(self.row("optics", "requirement", "requirement", aliases=["离焦补偿"]))
        with patch.object(kb_evidence, "verify_entry_evidence", wraps=kb_evidence.verify_entry_evidence) as checks:
            result = self.query("灯塔系统 离焦补偿")
        self.assertIn("optics", self.ids(result))
        checked = {call.args[0]["id"] for call in checks.call_args_list}
        self.assertIn("entry-optics", checked)
        self.assertFalse(checked & {"entry-remote-project", "entry-former", "entry-successor"})

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

    def routed(self, query, *opts):
        code, out, err = self.call("retrieve", query, "--brief", *map(str, opts))
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_active_file_in_unregistered_checkout_can_supply_all_feature_evidence(self):
        remote = self.base / "output" / "wayfinder"
        remote.mkdir(parents=True)
        (remote / "pyproject.toml").write_text("[project]\n")
        current = remote / "logic.py"
        current.write_text("# tide_window gateway_queue\n")
        result = self.routed("请完善 tide_window 和 gateway_queue", "--current-file", current.relative_to(self.base))
        route = result["project_routing"]
        self.assertEqual((route["state"], route["candidates"][0]["repo"]), ("use", "wayfinder"))
        self.assertEqual({x["source"] for x in route["candidates"][0]["features"]}, {str(current)})
        self.assertEqual(result["repo"], "wayfinder")
        self.assertEqual(result["items"], [])
        self.assertNotIn("project_id", route["candidates"][0])

    def test_open_files_with_competing_projects_leave_a_real_choice(self):
        files = []
        for name in ("north-terminal", "south-terminal"):
            root = self.base / name
            root.mkdir()
            (root / "pyproject.toml").write_text("[project]\n")
            file = root / "feature.md"
            file.write_text("raster_preview palette_table\n")
            files.append(file)
        options = [arg for file in files for arg in ("--current-file", file)]
        result = self.routed("修改 raster_preview 和 palette_table", *options)
        routing = result["project_routing"]
        self.assertEqual(routing["state"], "clarify")
        self.assertEqual({c["repo"] for c in routing["candidates"]}, {"north-terminal", "south-terminal"})
        self.assertIn("其他", routing["question"])
        self.assertFalse(result.get("domain_context", {}).get("project_id"))
        self.assertNotIn("project_routing", self.routed("浅拷贝是什么意思？", *options))
        code, text, _ = self.call("retrieve", "修改 raster_preview 和 palette_table", *map(str, options))
        self.assertEqual(code, 0)
        self.assertIn("KB_PROJECT_ROUTING", text)
        self.assertNotIn("project_routing", self.routed("raster_preview palette_table", "--no-domain", *options))

    def test_family_offers_only_linked_current_children(self):
        for key, name, state in (("north", "北线灯塔", "confirmed"), ("south", "南线灯塔", "confirmed"),
                                 ("old", "西线灯塔", "candidate")):
            e = self.row(key, "component", "map", aliases=[name, key], state=state,
                         relations=[self.edge("belongs_to", "p")])
            self.save(e)
        result = self.routed("灯塔系统要选哪个子项目？")["project_routing"]
        self.assertEqual(result["state"], "clarify")
        self.assertEqual({c["entity_id"] for c in result["candidates"]}, {"north", "south"})
        member = self.routed("北线灯塔")["project_routing"]
        self.assertEqual((member["state"], member["candidates"][0]["entity_id"]), ("use", "north"))
        self.assertEqual(member["candidates"][0]["parent_label"], "灯塔系统")

    def test_qualified_names_respect_context_and_withdrawal(self):
        ref = [{"type": "conversation", "value": "Explicit project spelling in fixture"}]
        for value in ("研修助手", "期末答辩"):
            event = {"field": "project_alias", "value": value, "basis": "observed", "state": "confirmed",
                     "date": "2026-01-01", "evidence_refs": ref}
            self.project["domain"].setdefault("observations", []).append(event)
        self.project["domain"]["observations"].append({**event, "value": "去年的名称", "basis":"ended"})
        path = self.root / "records/backend/main/kb.jsonl"
        path.write_text(json.dumps(self.project, ensure_ascii=False) + "\n", encoding="utf-8")
        bare = self.routed("研修助手")
        self.assertEqual(bare["project_routing"]["state"], "use")
        self.assertEqual(bare["items"][0]["entry_id"], self.project["id"])
        self.assertNotIn("project_routing", self.routed("我的期末答辩开场白怎么讲？"))
        self.assertNotIn("project_routing", self.routed("去年的名称"))
        self.assertNotIn("project_routing", self.routed("研修助手", "--as-of", "2025-12-20"))
        code, result, _ = self.call("retrieve", "研修助手", "--brief", *(["--current-file", "file.py"] * 5))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(result)["error"]["code"], "invalid_request")


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DomainTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(_TEST_GUARD.run(main))
