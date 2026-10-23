"""Acceptance decisions never replace original-workspace verification. Offline only."""
import copy
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from test_handoff import RepositoryCase, BACKEND, FRONTEND, HandoffError, State, Git, Workflow, person, requirement
from test_tencent_workflow import Client, World


class AcceptanceTests(RepositoryCase):
    def prepare(self, delivery=None):
        self.ticket(来源=json.dumps({"delivery": delivery or self.acceptance_delivery("backend")}))
        self.start()
        version = self.flow.ready("task-1", self.result())["result_version"]
        return self.make_flow(BACKEND), version

    def approve(self, reviewer, version):
        return reviewer.confirm("task-1", version, self.confirmation(reviewer, "task-1"))

    def test_publisher_must_select_a_registered_actual_reviewer(self):
        self.stage_backend()
        draft = self.draft()
        draft["tickets"][0]["delivery"]["reviewer_id"] = "unknown-account"
        with self.assertRaises(HandoffError) as error:
            self.flow.publish(draft)
        self.assertEqual(error.exception.code, "choose_reviewer")
        self.assertEqual(self.tickets.writes, 0)
        del draft["tickets"][0]["delivery"]["reviewer_id"]
        with self.assertRaises(HandoffError):
            self.flow.publish(draft)
        self.assertEqual(self.tickets.writes, 0)

    def test_ready_requires_each_standard_observation_and_linked_evidence(self):
        self.ticket(来源=json.dumps({"delivery": self.acceptance_delivery()}))
        self.start()
        for altered in ([], [{"criterion_id": "syntax-valid", "actual": "passed", "evidence": []}],
                        [{"criterion_id": "syntax-valid", "actual": "passed", "evidence": [{"check_id": "unrelated", "content": "ok"}]}]):
            result = self.result()
            result["acceptance"] = altered
            with self.subTest(altered=altered), self.assertRaises(HandoffError):
                self.flow.ready("task-1", result)
        self.assertEqual(self.tickets.get("task-1")["状态"], "处理中")

    def test_actual_reviewer_confirms_and_handler_finalizes_recorded_reviewer(self):
        reviewer, version = self.prepare()
        report = reviewer.review("task-1")
        self.assertEqual(report["acceptance_matrix"][0]["steps"], ["执行页面语法检查"])
        self.assertEqual(report["acceptance_matrix"][0]["expected"], "检查命令成功退出")
        self.assertEqual(report["acceptance_matrix"][0]["actual"], "页面代码检查成功退出")
        with self.assertRaises(HandoffError):
            self.flow.confirm("task-1", version, self.confirmation(self.flow, "task-1"))
        with self.assertRaises(HandoffError):
            self.flow.accept("task-1", version)
        self.approve(reviewer, version)
        self.assertEqual(self.tickets.get("task-1")["状态"], "待验收")
        with self.assertRaises(HandoffError):
            reviewer.accept("task-1", version)
        self.flow.accept("task-1", version)
        self.assertEqual(self.tickets.get("task-1")["验收人"], person(BACKEND))
        self.assertEqual(list(self.logs.rows.values())[-1]["操作者"], person(FRONTEND))

    def test_confirmation_rejects_missing_items_mixed_versions_or_wrong_evidence(self):
        reviewer, version = self.prepare()
        baseline = self.confirmation(reviewer, "task-1")
        invalid = []
        missing = copy.deepcopy(baseline)
        missing["items"] = []
        invalid.append(missing)
        mixed = copy.deepcopy(baseline)
        mixed["items"][0]["result_version"] = "different-version"
        invalid.append(mixed)
        wrong = copy.deepcopy(baseline)
        wrong["items"][0]["evidence_digests"] = ["wrong-proof"]
        invalid.append(wrong)
        writes = self.logs.writes
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(HandoffError):
                reviewer.confirm("task-1", version, value)
        self.assertEqual(self.logs.writes, writes)

    def test_confirmation_is_idempotent_and_cannot_be_overwritten(self):
        reviewer, version = self.prepare()
        self.approve(reviewer, version)
        writes = self.logs.writes
        self.assertTrue(self.approve(reviewer, version)["already_confirmed"])
        changed = self.confirmation(reviewer, "task-1")
        changed["items"][0]["observed"] = "另一份观察"
        with self.assertRaises(HandoffError):
            reviewer.confirm("task-1", version, changed)
        self.assertEqual(self.logs.writes, writes)

    def test_revalidate_same_code_and_checks_still_requires_a_new_confirmation(self):
        reviewer, first = self.prepare()
        self.approve(reviewer, first)
        second = self.flow.ready("task-1", self.result(), revalidate=True)["result_version"]
        self.assertNotEqual(first, second)
        with self.assertRaises(HandoffError):
            reviewer.confirm("task-1", first, self.confirmation(reviewer, "task-1"))
        with self.assertRaises(HandoffError) as error:
            self.flow.accept("task-1", second)
        self.assertEqual(error.exception.code, "confirmation_missing")
        self.approve(reviewer, second)
        self.flow.accept("task-1", second)

    def test_rejection_after_confirmation_and_resubmission_cannot_reuse_old_approval(self):
        reviewer, first = self.prepare()
        self.approve(reviewer, first)
        original = (self.root / "screen.js").read_text()
        reviewer.reject("task-1", "本项交付需要重做", first, ["syntax-valid"])
        self.assertEqual((self.root / "screen.js").read_text(), original)
        self.start()
        second = self.flow.ready("task-1", self.result())["result_version"]
        self.assertNotEqual(first, second)
        with self.assertRaises(HandoffError):
            self.flow.accept("task-1", second)
        with self.assertRaises(HandoffError):
            reviewer.reject("task-1", "旧版本反馈", first)
        self.approve(reviewer, second)
        self.flow.accept("task-1", second)
        rejected = [json.loads(r["内容"]) for r in self.logs.rows.values() if r["标题"] == "验收退回"]
        self.assertEqual(rejected[0]["result_version"], first)
        self.assertEqual(rejected[0]["criterion_ids"], ["syntax-valid"])

    def test_requirements_change_can_be_returned_but_cannot_be_confirmed(self):
        reviewer, first = self.prepare()
        changed = self.acceptance_delivery("backend")
        changed["criteria"][0]["expected"] = "补充新的预期行为"
        self.tickets.rows["task-1"]["来源"] = json.dumps({"delivery": changed})
        with self.assertRaises(HandoffError):
            reviewer.confirm("task-1", first, {"result_version": first, "items": []})
        reviewer.reject("task-1", "需求已更新，重新处理", first)
        self.start()
        second = self.flow.ready("task-1", self.result())["result_version"]
        self.assertNotEqual(first, second)

    def test_confirmed_result_still_rejects_changed_code_and_other_workspaces(self):
        reviewer, version = self.prepare()
        self.approve(reviewer, version)
        other_root = Path(self.tmp.name) / "other-workspace"
        subprocess.run(["git", "clone", "-q", str(self.root), str(other_root)], check=True, capture_output=True)
        other_git = Git(other_root)
        other = Workflow(other_git, State(other_git, Path(self.tmp.name) / "other-state"), FRONTEND,
                         self.binding, self.tickets, self.logs, self.members)
        with self.assertRaises(HandoffError) as error:
            other.accept("task-1", version)
        self.assertEqual(error.exception.code, "wrong_workspace")
        (self.root / "screen.js").write_text("const modified = true;\n")
        with self.assertRaises(HandoffError) as error:
            self.flow.accept("task-1", version)
        self.assertEqual(error.exception.code, "code_changed")
        self.assertEqual(self.tickets.get("task-1")["状态"], "待验收")

    def test_report_keeps_materials_visible_when_external_acceptance_is_blocked(self):
        self.ticket("external")
        delivery = self.acceptance_delivery("backend")
        delivery["external_checks"] = [{"description": "真实联调", "owner": "测试", "gate": "acceptance", "ticket_id": "external"}]
        reviewer, version = self.prepare(delivery)
        report = reviewer.review("task-1")
        self.assertEqual(len(report["acceptance_matrix"]), 1)
        self.assertIn("external_verification_blocked", [x["code"] for x in report["blocking"]])
        self.approve(reviewer, version)
        with self.assertRaises(HandoffError):
            self.flow.accept("task-1", version)
        self.tickets.rows["external"]["状态"] = "已完成"
        self.flow.accept("task-1", version)

    def test_lost_confirmation_receipt_recovers_without_duplicate_or_status_change(self):
        reviewer, version = self.prepare()
        self.logs.fail_create_after_commit = True
        with self.assertRaises(HandoffError):
            self.approve(reviewer, version)
        writes = self.logs.writes
        reviewer.sync()
        self.assertEqual(self.logs.writes, writes)
        self.assertEqual(self.tickets.get("task-1")["状态"], "待验收")
        self.flow.accept("task-1", version)

    def test_unknown_confirmation_write_does_not_blindly_replay(self):
        reviewer, version = self.prepare()
        self.logs.fail_create_before = True
        with self.assertRaises(HandoffError):
            self.approve(reviewer, version)
        writes = self.logs.writes
        with self.assertRaises(HandoffError):
            reviewer.sync()
        self.assertEqual(self.logs.writes, writes)

    def test_finalization_recovery_rejects_changed_confirmation_record(self):
        reviewer, version = self.prepare()
        self.approve(reviewer, version)
        self.logs.fail_create_after_commit = True
        with self.assertRaises(HandoffError):
            self.flow.accept("task-1", version)
        approval_row = next(r for r in self.logs.rows.values() if r["标题"] == "逐项验收确认")
        original = approval_row["内容"]
        altered = json.loads(original)
        altered["items"][0]["observed"] = "确认后的人工改动"
        approval_row["内容"] = json.dumps(altered, ensure_ascii=False)
        with self.assertRaises(HandoffError) as error:
            self.flow.sync()
        self.assertEqual(error.exception.code, "confirmation_changed")
        self.assertEqual(self.flow.session("task-1")["phase"], "pending_acceptance")
        approval_row["内容"] = original
        self.flow.sync()
        self.assertEqual(self.flow.session("task-1")["phase"], "completed")

    def test_actual_actor_in_confirmation_log_must_match_designated_reviewer(self):
        reviewer, version = self.prepare()
        self.approve(reviewer, version)
        logged = next(r for r in self.logs.rows.values() if r["标题"] == "逐项验收确认")
        logged["操作者"] = person(FRONTEND)
        with self.assertRaises(HandoffError) as error:
            self.flow.accept("task-1", version)
        self.assertEqual(error.exception.code, "invalid_confirmation")

    def test_live_exercise_runner_uses_new_fixtures_and_resumes_without_duplicate_records(self):
        from acceptance_smoke import exercise
        client = Client(World(), "frontend")
        run_dir = Path(self.tmp.name) / "live-protocol-fixture"
        with patch("acceptance_smoke.TencentMCP", return_value=client), patch("acceptance_smoke.load_token", return_value="offline-fixture"):
            first = exercise(self.root, run_dir)
            second = exercise(self.root, run_dir)
        self.assertEqual(first["record_id"], second["record_id"])
        self.assertEqual(first["final_version"], second["final_version"])
        self.assertEqual(len(second["history_events"]), 8)
        self.assertFalse(first["business_acceptance"])
        self.assertEqual(client.world.file_created, 1)
