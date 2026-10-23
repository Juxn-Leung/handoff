"""Automatic finalization uses local sessions and real guards; no live accounts."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from test_handoff import RepositoryCase, BACKEND, FRONTEND, HandoffError, State, Git, Workflow, person, exclusive_lock
import auto_accept
import handoff


class AutoAcceptTests(RepositoryCase):
    def prepare(self, delivery=None, enabled=True):
        self.ticket(来源=json.dumps({"delivery": delivery or self.acceptance_delivery("backend")}))
        self.start()
        self.version = self.flow.ready("task-1", self.result(), auto_accept=enabled)["result_version"]
        active = auto_accept.register(self.flow)
        self.job = active[0] if active else None
        # The reviewer really has a different repository and private local state.
        reviewer_root = Path(self.tmp.name) / "reviewer"
        subprocess.run(["git", "clone", "-q", str(self.root), str(reviewer_root)], check=True, capture_output=True)
        git = Git(reviewer_root)
        self.reviewer = Workflow(git, State(git, Path(self.tmp.name) / "reviewer-state"), BACKEND,
                                 self.binding, self.tickets, self.logs, self.members)

    def approve(self):
        self.reviewer.confirm("task-1", self.version, self.confirmation(self.reviewer, "task-1"))

    def tick(self):
        self.state = State(self.git, self.state_dir)
        self.flow = self.make_flow()
        return auto_accept.step(self.flow, self.job["id"])

    def args(self):
        return argparse.Namespace(repo=str(self.root), state_dir=str(self.state_dir),
                                  table=self.binding["url"], job_id=self.job["id"])

    def test_confirm_on_other_workspace_triggers_original_workspace_completion_once(self):
        self.prepare()
        writes = self.tickets.writes, self.logs.writes
        self.assertEqual(self.tick()["reason"], "confirmation_missing")
        self.assertEqual((self.tickets.writes, self.logs.writes), writes)
        self.approve()
        self.assertEqual(self.tick()["status"], "completed")
        self.assertEqual(self.tickets.get("task-1")["验收人"], person(BACKEND))
        self.assertEqual(list(self.logs.rows.values())[-1]["操作者"], person(FRONTEND))
        writes = self.tickets.writes, self.logs.writes
        self.assertEqual(self.tick()["status"], "completed")
        self.assertEqual((self.tickets.writes, self.logs.writes), writes)

    def test_code_change_stops_and_does_not_resume_just_because_bytes_are_restored(self):
        self.prepare()
        self.approve()
        path = self.root / "screen.js"
        old = path.read_bytes()
        path.write_text("changed = true;\n")
        self.assertEqual(self.tick()["reason"], "code_changed")
        path.write_bytes(old)
        self.assertEqual(self.tick()["status"], "stopped")
        self.assertEqual(self.tickets.get("task-1")["状态"], "待验收")
        with self.assertRaises(HandoffError):
            auto_accept.enable(self.flow, "task-1")

    def test_rejection_and_resubmission_never_use_old_approval_or_old_worker(self):
        self.prepare()
        self.approve()
        self.reviewer.reject("task-1", "需要修复", self.version)
        self.assertEqual(self.tick()["status"], "stopped")
        self.start()
        second = self.flow.ready("task-1", self.result(), auto_accept=True)["result_version"]
        active = auto_accept.register(self.flow)
        self.assertEqual(len(active), 1)
        self.assertNotEqual(active[0]["id"], self.job["id"])
        self.assertEqual(self.tick()["status"], "stopped")
        self.job = active[0]
        self.assertEqual(self.tick()["reason"], "confirmation_missing")
        self.version = second
        self.approve()
        self.assertEqual(self.tick()["status"], "completed")

    def test_external_gate_waits_and_completes_after_gate_is_satisfied(self):
        self.ticket("external")
        delivery = self.acceptance_delivery("backend")
        delivery["external_checks"] = [{"description": "联调", "owner": "后端", "gate": "acceptance", "ticket_id": "external"}]
        self.prepare(delivery)
        self.approve()
        self.assertEqual(self.tick()["reason"], "external_verification_blocked")
        self.tickets.rows["external"]["状态"] = "已完成"
        self.assertEqual(self.tick()["status"], "completed")

    def test_requirement_change_stops_without_writing(self):
        self.prepare()
        self.approve()
        self.tickets.rows["task-1"]["验收标准"] = "新的要求"
        writes = self.tickets.writes
        self.assertEqual(self.tick()["reason"], "review_version_changed")
        self.assertEqual(self.tickets.writes, writes)

    def test_identity_switch_stops_without_using_another_accounts_confirmation(self):
        self.prepare()
        self.approve()
        self.assertEqual(auto_accept.step(self.make_flow(BACKEND), self.job["id"])["status"], "stopped")
        self.assertEqual(self.tickets.get("task-1")["状态"], "待验收")

    def test_other_workspace_cannot_finalize_even_with_copied_job_and_session(self):
        self.prepare()
        self.approve()
        self.reviewer.state.data = copy.deepcopy(self.state.data)
        other = Workflow(self.reviewer.git, self.reviewer.state, FRONTEND, self.binding, self.tickets, self.logs, self.members)
        self.assertEqual(auto_accept.step(other, self.job["id"])["reason"], "identity_or_workspace_changed")
        self.assertEqual(self.tickets.get("task-1")["状态"], "待验收")

    def test_changed_confirmation_actor_stops_without_finalization(self):
        self.prepare()
        self.approve()
        row = next(row for row in self.logs.rows.values() if row["标题"] == "逐项验收确认")
        row["操作者"] = person(FRONTEND)
        self.assertEqual(self.tick()["reason"], "invalid_confirmation")
        self.assertEqual(self.tickets.get("task-1")["状态"], "待验收")

    def test_lost_completion_response_recovers_without_duplicate_write(self):
        self.prepare()
        self.approve()
        self.tickets.fail_update_after_commit = True
        self.assertEqual(self.tick()["reason"], "write_unknown")
        writes = self.tickets.writes
        self.assertEqual(self.tick()["status"], "completed")
        self.assertEqual(self.tickets.writes, writes)
        self.assertEqual(len([r for r in self.logs.rows.values() if r["标题"] == "验收通过"]), 1)

    def test_unknown_log_write_stops_after_safe_recovery_cannot_find_it(self):
        self.prepare()
        self.approve()
        self.logs.fail_create_before = True
        self.assertEqual(self.tick()["reason"], "write_unknown")
        writes = self.logs.writes
        self.assertEqual(self.tick()["status"], "stopped")
        self.assertEqual(self.logs.writes, writes)
        self.assertEqual(self.flow.session("task-1")["phase"], "pending_acceptance")

    def test_recovery_does_not_replay_other_pending_operations(self):
        self.prepare()
        self.approve()
        self.logs.fail_create_after_commit = True
        self.assertEqual(self.tick()["reason"], "write_unknown")
        self.state.data["operations"]["other"] = {"id": "other", "namespace": self.flow.namespace,
            "record_id": "other-ticket", "profile": FRONTEND["profile"], "phase": "prepared"}
        self.state.save()
        self.assertEqual(self.tick()["status"], "completed")
        self.assertEqual(self.state.data["operations"]["other"]["phase"], "prepared")

    def test_code_change_during_recovery_never_claims_success(self):
        self.prepare()
        self.approve()
        self.logs.fail_create_after_commit = True
        self.assertEqual(self.tick()["status"], "waiting")
        (self.root / "screen.js").write_text("changed = true;\n")
        self.assertEqual(self.tick()["reason"], "code_changed")
        self.assertEqual(self.flow.session("task-1")["phase"], "pending_acceptance")

    def test_manual_mode_cancellation_and_expiry_do_not_finalize(self):
        self.prepare(enabled=False)
        self.assertEqual(auto_accept.register(self.flow), [])
        self.job = auto_accept.enable(self.flow, "task-1")[0]
        self.approve()
        auto_accept.cancel(self.flow, "task-1")
        self.assertEqual(self.tick()["status"], "cancelled")
        auto_accept.enable(self.flow, "task-1")
        self.state.data["auto_accept"][self.job["id"]]["expires_at"] = 0
        self.state.save()
        self.assertEqual(self.tick()["status"], "expired")
        self.assertEqual(self.tickets.get("task-1")["状态"], "待验收")

    def test_legacy_tickets_are_never_enrolled(self):
        self.ticket()
        self.start()
        self.flow.ready("task-1", self.result(), auto_accept=True)
        self.assertEqual(auto_accept.register(self.flow), [])
        with self.assertRaises(HandoffError):
            auto_accept.enable(self.flow, "task-1")

    def test_worker_releases_operation_lock_while_waiting_and_resumes_to_complete(self):
        self.prepare()
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            with exclusive_lock(self.state.lock_path):
                self.assertTrue(auto_accept.running(self.state, self.job["id"]))
                self.approve()
        result = auto_accept.worker(self.args(), lambda args: self.tick(), sleep=sleep)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sleeps, [auto_accept.POLL_SECONDS])
        self.assertFalse(auto_accept.running(self.state, self.job["id"]))

    def test_transient_network_errors_retry_but_auth_failure_stops(self):
        self.prepare()
        calls, delays = [], []
        def callback(args):
            calls.append(1)
            if len(calls) == 1:
                raise HandoffError("tencent_network_error", "offline")
            raise HandoffError("tencent_unauthenticated", "expired token")
        result = auto_accept.worker(self.args(), callback, sleep=delays.append)
        self.assertEqual(len(calls), 2)
        self.assertEqual(delays, [120])
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["reason"], "tencent_unauthenticated")

    def test_network_error_returned_inside_review_is_retried_not_a_failed_acceptance(self):
        self.prepare()
        self.approve()
        with patch.object(self.logs, "records", side_effect=HandoffError("tencent_network_error", "offline")):
            result = self.tick()
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["reason"], "tencent_network_error")
        self.assertEqual(self.tick()["status"], "completed")

    def test_worker_lock_prevents_duplicate_runner_and_launcher_reports_spawn_failure(self):
        self.prepare()
        with exclusive_lock(auto_accept.lock_path(self.state, self.job["id"])):
            with patch("auto_accept.Git", return_value=self.git), patch("auto_accept.subprocess.Popen") as spawn:
                self.assertEqual(auto_accept.launch(self.root, self.state_dir, self.job)["worker"], "running")
                spawn.assert_not_called()
            self.assertEqual(auto_accept.worker(self.args(), lambda _: self.fail("duplicate worker"))["worker"], "already_running")
        with patch("auto_accept.Git", return_value=self.git), patch("auto_accept.subprocess.Popen", side_effect=OSError("no process")):
            self.assertEqual(auto_accept.launch(self.root, self.state_dir, self.job)["worker"], "start_failed")

    def test_real_worker_process_expires_locally_without_loading_credentials(self):
        self.prepare()
        self.state.data["auto_accept"][self.job["id"]]["expires_at"] = 0
        self.state.save()
        result = subprocess.run([sys.executable, str(Path(handoff.__file__)), "--repo", str(self.root),
            "--state-dir", str(self.state_dir), "--table", self.binding["url"], "_auto-accept-worker", self.job["id"]],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)["data"]["status"], "expired")
        self.assertEqual(State(self.git, self.state_dir).data["auto_accept"][self.job["id"]]["status"], "expired")

    def test_cli_ready_defaults_to_auto_and_launches_after_releasing_operation_lock(self):
        self.binding["provider"] = "tencent"
        self.state.save_project()
        self.ticket(来源=json.dumps({"delivery": self.acceptance_delivery("backend")}))
        self.start()
        result_file = Path(self.tmp.name) / "result.json"
        result_file.write_text(json.dumps(self.result()))
        args = handoff.parser().parse_args(["--repo", str(self.root), "--state-dir", str(self.state_dir),
                                          "ready", "task-1", str(result_file)])
        def launch(repo, state_dir, job):
            with exclusive_lock(self.state.lock_path):
                self.assertEqual(job["profile"], FRONTEND["profile"])
            return {"worker": "running"}
        with patch("handoff.load_token", return_value="offline-fixture"), patch("handoff.TencentMCP"), \
             patch("handoff.live_identity", return_value=FRONTEND), \
             patch("handoff.tables", return_value=(self.tickets, self.logs, self.members)), \
             patch("auto_accept.launch", side_effect=launch) as spawn:
            result = handoff.execute(args)
        spawn.assert_called_once()
        self.assertNotIn("_launch", result)
        self.assertEqual(result["auto_accept_workers"], [{"worker": "running"}])
        self.assertTrue(State(self.git, self.state_dir).data["sessions"][self.flow.key("task-1")]["auto_accept"])

    def test_cli_manual_ready_does_not_launch_and_status_needs_no_token(self):
        self.binding["provider"] = "tencent"
        self.state.save_project()
        self.ticket(来源=json.dumps({"delivery": self.acceptance_delivery("backend")}))
        self.start()
        result_file = Path(self.tmp.name) / "result.json"
        result_file.write_text(json.dumps(self.result()))
        prefix = ["--repo", str(self.root), "--state-dir", str(self.state_dir)]
        with patch("handoff.load_token", return_value="offline-fixture"), patch("handoff.TencentMCP"), \
             patch("handoff.live_identity", return_value=FRONTEND), \
             patch("handoff.tables", return_value=(self.tickets, self.logs, self.members)), patch("auto_accept.launch") as spawn:
            handoff.execute(handoff.parser().parse_args([*prefix, "ready", "task-1", str(result_file), "--manual-accept"]))
        spawn.assert_not_called()
        with patch("handoff.load_token", side_effect=AssertionError("status must be local")):
            self.assertEqual(handoff.execute(handoff.parser().parse_args([*prefix, "auto-accept", "--status"])), {"auto_accept": []})
