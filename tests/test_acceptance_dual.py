"""Offline verification of the phased harness; never reported as real dual-account testing."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import zipfile

from test_handoff import RepositoryCase, HandoffError
from test_tencent_workflow import Client, World
from tencent_tables import identity, create, tables, join
from acceptance_dual import context, prepare, reviewer_phase, processor_phase, runtime_fingerprint
from acceptance_smoke import SOURCE
from handoff_core import requirement, write_json


class DualHarnessTests(RepositoryCase):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.processor_client, self.reviewer_client = Client(self.world, "frontend"), Client(self.world, "backend")
        self.state.project["bindings"] = []
        self.binding = create(self.processor_client, self.state, identity(self.processor_client), "共享测试", "共享测试")["created"]
        for client in (self.processor_client, self.reviewer_client):
            _, _, members = tables(client, self.binding, identity(client))
            join(members, self.state, identity(client))
        self.run_dir = Path(self.tmp.name) / "dual-run"
        self.peer_repo = Path(self.tmp.name) / "reviewer-authorized-project"

    def call(self, client, function, *args):
        with patch("acceptance_dual.TencentMCP", return_value=client), patch("acceptance_dual.load_token", return_value="offline-test"):
            return function(*args)

    def prepare_run(self):
        return self.call(self.processor_client, prepare, self.root, self.run_dir, self.binding["url"], "backend")

    def test_two_actual_actor_steps_are_required_before_processor_can_finish(self):
        checkpoint = self.prepare_run()
        self.assertEqual(checkpoint["phase"], "waiting_reviewer_first")
        self.assertEqual(checkpoint["checks"]["non_designated_reviewer"]["error"], "not_reviewer")
        with self.assertRaises(HandoffError):
            self.call(self.processor_client, processor_phase, self.root, self.run_dir, "resubmit")
        with self.assertRaises(HandoffError):
            self.call(self.processor_client, reviewer_phase, self.root, self.run_dir / "package", "first")
        first = self.call(self.reviewer_client, reviewer_phase, self.peer_repo, self.run_dir / "package", "first")
        self.assertEqual(first["actor_id"], "backend")
        self.assertEqual(first["phase"], "first_done")
        retry = self.call(self.reviewer_client, reviewer_phase, self.peer_repo, self.run_dir / "package", "first")
        self.assertEqual(retry["phase"], "first_already_done")
        second = self.call(self.processor_client, processor_phase, self.root, self.run_dir, "resubmit")
        self.assertNotEqual(second["first_version"], second["second_version"])
        with self.assertRaises(HandoffError):
            self.call(self.processor_client, processor_phase, self.root, self.run_dir, "finalize")
        reviewed = self.call(self.reviewer_client, reviewer_phase, self.peer_repo, self.run_dir / "package", "second", second["second_version"])
        self.assertEqual(reviewed["observed"]["old_confirmation_rejected"], "review_version_changed")
        final = self.call(self.processor_client, processor_phase, self.root, self.run_dir, "finalize")
        self.assertEqual(final["phase"], "complete")
        report = json.loads((self.run_dir / "report.json").read_text())
        self.assertEqual(report["reviewer_id"], "backend")
        self.assertEqual(len(report["history_events"]), 9)
        self.assertEqual(report["history_events"].count("逐项验收确认"), 2)

    def test_archive_contains_runtime_and_plan_but_no_credentials_or_private_state(self):
        checkpoint = self.prepare_run()
        with zipfile.ZipFile(checkpoint["package"]) as archive:
            names = archive.namelist()
        self.assertIn("handoff.json", names)
        self.assertIn("dev-handoff/scripts/acceptance_dual.py", names)
        self.assertIn("fixture/service.py", names)
        self.assertFalse(any(".dev-handoff-local" in n or "state.json" in n or "token.json" in n or ".git/" in n for n in names))

    def test_installed_runtime_can_review_using_only_public_plan_from_shared_ticket(self):
        checkpoint = self.prepare_run()
        # Follow acceptance.md's installed-skill route, without opening the archive
        # or copying any of the processor's plan, runtime or private state.
        flow, who = self.call(self.reviewer_client, context, self.peer_repo,
                              Path(self.tmp.name) / "reviewer-discovery", self.binding["url"])
        row = flow.tickets.get(checkpoint["record_id"])
        marker = flow.show(row["id"])["delivery"]["verification_run"]
        self.assertEqual(who["user_id"], marker["reviewer_id"])
        self.assertEqual(runtime_fingerprint(Path(__file__).resolve().parents[1] / "dev-handoff"), marker["runtime_fingerprint"])
        plan = {**marker, "table_url": self.binding["url"], "record_id": row["id"],
                "first_version": row["结果版本"], "requirement_version": requirement(row)}
        package = Path(self.tmp.name) / "reviewer-installed-package"
        fixture = (SOURCE + "\n# staged protocol fixture\n").encode("utf-8")
        self.assertEqual(hashlib.sha256(fixture).hexdigest(), flow._shared_evidence(row)["code"]["service.py"]["sha256"])
        write_json(package / "handoff.json", plan)
        (package / "fixture").mkdir()
        (package / "fixture/service.py").write_bytes(fixture)
        first = self.call(self.reviewer_client, reviewer_phase, self.peer_repo, package, "first")
        self.assertEqual(first["phase"], "first_done")
        second = self.call(self.processor_client, processor_phase, self.root, self.run_dir, "resubmit")
        reviewed = self.call(self.reviewer_client, reviewer_phase, self.peer_repo, package, "second", second["second_version"])
        self.assertEqual(reviewed["observed"]["old_confirmation_rejected"], "review_version_changed")
        self.assertEqual(json.loads((package / "handoff.json").read_text())["first_version"], checkpoint["first_version"])
        final = self.call(self.processor_client, processor_phase, self.root, self.run_dir, "finalize")
        self.assertEqual(final["phase"], "complete")

    def test_same_account_cannot_be_reported_as_real_dual_account(self):
        with self.assertRaises(HandoffError) as error:
            self.call(self.processor_client, prepare, self.root, self.run_dir, self.binding["url"], "frontend")
        self.assertEqual(error.exception.code, "distinct_accounts_required")
