"""Offline harness checks; no real accounts or real background launch."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from test_handoff import RepositoryCase, HandoffError
from test_tencent_workflow import Client, World
from tencent_tables import identity, create, tables, join
from handoff_core import read_json, write_json, digest
import acceptance_auto as harness
import auto_accept
import handoff


class AutoHarnessTests(RepositoryCase):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.processor_client = Client(self.world, "frontend")
        self.reviewer_client = Client(self.world, "backend")
        self.state.project["bindings"] = []
        self.binding = create(self.processor_client, self.state, identity(self.processor_client), "测试", "测试")["created"]
        for client in (self.processor_client, self.reviewer_client):
            _, _, members = tables(client, self.binding, identity(client))
            join(members, self.state, identity(client))
        self.run_dir = Path(self.tmp.name) / "auto-run"
        self.peer_repo = Path(self.tmp.name) / "reviewer-project"

    def call(self, client, function, *args):
        def cli(repo, run_dir, url, arguments):
            parsed = handoff.parser().parse_args(["--repo", str(run_dir / "processor/workspace"),
                "--state-dir", str(run_dir / "processor/state"), "--table", url, *arguments])
            return handoff.execute(parsed)
        with patch("acceptance_auto.TencentMCP", return_value=client), patch("acceptance_auto.load_token", return_value="offline-test"), \
             patch("handoff.TencentMCP", return_value=client), patch("handoff.load_token", return_value="offline-test"), \
             patch("acceptance_auto.cli", side_effect=cli), patch("auto_accept.launch", return_value={"worker": "offline_no_process"}):
            return function(*args)

    def prepare_run(self):
        return self.call(self.processor_client, harness.prepare, self.root, self.run_dir, self.binding["url"], "backend")

    def tick(self, checkpoint):
        with harness.context(self.root, self.run_dir / "processor", self.binding["url"]) as flow:
            jid = digest([flow.key(checkpoint["record_id"]), checkpoint["result_version"]])
            return auto_accept.step(flow, jid)

    def test_confirmation_then_worker_completion_and_readonly_audit(self):
        prepared = self.prepare_run()
        self.assertEqual(prepared["auto_accept"][0]["status"], "waiting")
        self.assertEqual(self.call(self.processor_client, self.tick, prepared)["reason"], "confirmation_missing")
        with patch("workflow.Workflow.accept", side_effect=AssertionError("audit must not finalize")), \
             patch("workflow.Workflow.sync", side_effect=AssertionError("audit must not recover writes")):
            writes = self.world.writes
            audit = self.call(self.processor_client, harness.audit, self.root, self.run_dir)
            self.assertEqual(audit["phase"], "waiting")
            self.assertEqual(writes, self.world.writes)
        reviewed = self.call(self.reviewer_client, harness.reviewer, self.peer_repo, self.binding["url"], prepared["record_id"])
        self.assertEqual(reviewed["phase"], "confirmed_waiting_auto_accept")
        self.assertEqual(reviewed["shared_status"], "待验收")
        writes = self.world.writes
        self.call(self.reviewer_client, harness.reviewer, self.peer_repo, self.binding["url"], prepared["record_id"])
        self.assertEqual(self.world.writes, writes)
        self.assertEqual(self.call(self.processor_client, self.tick, prepared)["status"], "completed")
        with patch("workflow.Workflow.accept", side_effect=AssertionError("audit must not finalize")), \
             patch("workflow.Workflow.sync", side_effect=AssertionError("audit must not recover writes")):
            audit = self.call(self.processor_client, harness.audit, self.root, self.run_dir)
        self.assertEqual(audit["phase"], "complete")
        self.assertEqual(audit["history_events"].count("验收通过"), 1)
        self.assertNotIn("验收退回", audit["history_events"])
        repeated = self.prepare_run()
        self.assertEqual(repeated["record_id"], prepared["record_id"])
        self.assertEqual(repeated["result_version"], prepared["result_version"])

    def test_preparation_is_idempotent_and_snapshot_has_no_private_state(self):
        first = self.prepare_run()
        writes = self.world.writes
        repeated = self.prepare_run()
        self.assertEqual(first["record_id"], repeated["record_id"])
        self.assertEqual(first["result_version"], repeated["result_version"])
        self.assertEqual(writes, self.world.writes)
        files = [str(p.relative_to(self.run_dir / "runtime")) for p in (self.run_dir / "runtime").rglob("*") if p.is_file()]
        self.assertFalse(any("token.json" in n or "state.json" in n or "/.git/" in n for n in files))

    def test_wrong_actor_and_runtime_cannot_confirm(self):
        prepared = self.prepare_run()
        with self.assertRaises(HandoffError) as error:
            self.call(self.processor_client, harness.reviewer, self.root, self.binding["url"], prepared["record_id"])
        self.assertEqual(error.exception.code, "identity_mismatch")
        with patch("acceptance_auto.runtime_fingerprint", return_value="wrong"):
            with self.assertRaises(HandoffError) as error:
                self.call(self.reviewer_client, harness.reviewer, self.peer_repo, self.binding["url"], prepared["record_id"])
        self.assertEqual(error.exception.code, "runtime_changed")

    def test_same_account_cannot_prepare(self):
        with self.assertRaises(HandoffError) as error:
            self.call(self.processor_client, harness.prepare, self.root, self.run_dir, self.binding["url"], "frontend")
        self.assertEqual(error.exception.code, "distinct_accounts_required")

    def test_fixture_failure_rejects_and_background_stops(self):
        prepared = self.prepare_run()
        with patch("acceptance_auto.result", side_effect=subprocess.CalledProcessError(1, "trusted fixture")):
            result = self.call(self.reviewer_client, harness.reviewer, self.peer_repo, self.binding["url"], prepared["record_id"])
        self.assertEqual(result["phase"], "rejected")
        self.assertEqual(self.call(self.processor_client, self.tick, prepared)["status"], "stopped")

    def test_authorization_only_in_child_environment_not_arguments_or_files(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "data": {"prepared": True}}))
        with patch("acceptance_auto.load_token", return_value="private-offline-token"), \
             patch("acceptance_auto.subprocess.run", return_value=completed) as run:
            harness.cli(self.root, self.run_dir, self.binding["url"], ["auto-accept"])
        args, kwargs = run.call_args
        self.assertNotIn("private-offline-token", str(args))
        self.assertEqual(kwargs["env"]["TENCENT_DOCS_TOKEN"], "private-offline-token")
        self.assertNotEqual(os.environ.get("TENCENT_DOCS_TOKEN"), "private-offline-token")
        self.assertFalse(self.run_dir.exists())
