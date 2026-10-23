"""Delivery boundaries, early planning, and current queue hints; all tests are offline."""
import copy
import json

from test_handoff import RepositoryCase, HandoffError, requirement, digest, State


class DeliveryTests(RepositoryCase):
    def test_new_publish_requires_delivery_before_any_write(self):
        self.stage_backend()
        draft = self.draft()
        del draft["tickets"][0]["delivery"]
        with self.assertRaises(HandoffError) as error:
            self.flow.publish(draft)
        self.assertEqual(error.exception.code, "invalid_delivery")
        self.assertEqual(self.tickets.writes, 0)

    def test_legacy_publish_checkpoint_can_resume_without_changing_manifest(self):
        self.stage_backend()
        draft = self.draft()
        del draft["tickets"][0]["delivery"]
        self.state.data["batches"][self.flow.namespace + ":" + draft["snapshot"]] = {"manifest_hash": digest(draft), "items": {}}
        result = self.flow.publish(draft)
        self.assertIsNone(self.flow.show(result["published"][0]["id"])["delivery"])

    def test_publish_preview_reports_shared_scope_without_writing_or_creating_dependencies(self):
        self.stage_backend()
        draft = self.draft()
        first = draft["tickets"][0]
        first["delivery"]["shared_contracts"] = ["order-status"]
        second = copy.deepcopy(first)
        second.update(key="another-app", title="其他应用适配")
        second["delivery"]["apps"] = ["admin-app"]
        draft["tickets"].append(second)
        preview = self.flow.publish(draft, check_only=True)
        self.assertEqual(preview["overlap_warnings"][0]["shared_contracts"], ["order-status"])
        self.assertEqual(self.tickets.writes, 0)
        self.assertFalse(self.state.data["batches"])
        result = self.flow.publish(draft)
        self.assertEqual(len(result["published"]), 2)
        self.assertTrue(all(not r["前置依赖"] for r in self.tickets.rows.values()))

    def test_source_path_overlap_alone_is_not_receiver_file_overlap(self):
        self.stage_backend()
        draft = self.draft()
        second = copy.deepcopy(draft["tickets"][0])
        second["key"] = "other"
        second["delivery"]["apps"] = ["other-app"]
        draft["tickets"].append(second)
        self.assertEqual(self.flow.publish(draft, check_only=True)["overlap_warnings"], [])

    def test_required_integration_check_cannot_be_skipped_or_replaced_by_mock(self):
        delivery = self.delivery()
        delivery["checks"][0]["kind"] = "integration"
        self.ticket(来源=json.dumps({"delivery": delivery}))
        self.start()
        result = self.result()
        result["checks"][0]["kind"] = "mock"
        with self.assertRaises(HandoffError) as error:
            self.flow.ready("task-1", result)
        self.assertEqual(error.exception.code, "check_kind_mismatch")
        result["checks"][0].update(kind="integration", outcome="not_run", reason="环境未提供")
        with self.assertRaises(HandoffError) as error:
            self.flow.ready("task-1", result)
        self.assertEqual(error.exception.code, "required_checks_missing")
        self.assertEqual(self.tickets.get("task-1")["状态"], "处理中")

    def test_external_acceptance_gate_keeps_frontend_result_pending(self):
        self.ticket("backend", 状态="待对接")
        delivery = self.delivery()
        delivery["external_checks"] = [{"description": "真实权限联调", "owner": "后端与测试", "gate": "acceptance", "ticket_id": "backend"}]
        self.ticket(来源=json.dumps({"delivery": delivery}))
        self.start()
        ready = self.flow.ready("task-1", self.result())
        self.assertEqual(self.flow.show("task-1")["current_blocking"]["external_checks"][0]["id"], "backend")
        with self.assertRaises(HandoffError):
            self.flow.accept("task-1", ready["result_version"])
        self.tickets.rows["backend"]["状态"] = "已完成"
        self.flow.accept("task-1", ready["result_version"])

    def test_external_start_gate_blocks_claim_and_start(self):
        self.ticket("backend")
        delivery = self.delivery()
        delivery["external_checks"] = [{"description": "契约部署", "owner": "后端", "gate": "start", "ticket_id": "backend"}]
        self.ticket(来源=json.dumps({"delivery": delivery}))
        with self.assertRaises(HandoffError):
            self.start()
        self.assertNotIn("task-1", [r["id"] for r in self.flow.queue()["ready"]])

    def test_delivery_changes_invalidate_result_but_legacy_results_still_work(self):
        self.ticket(来源=json.dumps({"delivery": self.delivery()}))
        self.start()
        ready = self.flow.ready("task-1", self.result())
        changed = self.delivery()
        changed["scope"] = "扩大范围"
        self.tickets.rows["task-1"]["来源"] = json.dumps({"delivery": changed})
        with self.assertRaises(HandoffError) as error:
            self.flow.accept("task-1", ready["result_version"])
        self.assertEqual(error.exception.code, "requirements_changed")

    def test_recovering_acceptance_rechecks_reopened_external_gate(self):
        self.ticket("backend", 状态="已完成")
        delivery = self.delivery()
        delivery["external_checks"] = [{"description": "真实联调", "owner": "测试", "gate": "acceptance", "ticket_id": "backend"}]
        self.ticket(来源=json.dumps({"delivery": delivery}))
        self.start()
        ready = self.flow.ready("task-1", self.result())
        self.logs.fail_create_after_commit = True
        with self.assertRaises(HandoffError):
            self.flow.accept("task-1", ready["result_version"])
        self.tickets.rows["backend"]["状态"] = "待对接"
        with self.assertRaises(HandoffError) as error:
            self.flow.sync()
        self.assertEqual(error.exception.code, "external_verification_blocked")
        self.assertEqual(self.flow.session("task-1")["phase"], "pending_acceptance")
        self.tickets.rows["backend"]["状态"] = "已完成"
        self.flow.sync()
        self.assertEqual(self.flow.session("task-1")["phase"], "completed")
        self.assertEqual(len(self.logs.rows), 3)


class PlanningTests(RepositoryCase):
    def plan(self, rid, paths):
        return self.flow.preflight([{"record_id": rid, "paths": paths}])

    def test_early_file_conflict_is_visible_without_starting_or_writing_remote(self):
        self.ticket()
        self.ticket("next")
        self.start()
        ready = self.flow.ready("task-1", self.result())
        writes = self.tickets.writes
        preflight = self.plan("next", ["screen.js"])
        conflict = preflight["plans"][0]["current_blocking"]["files"][0]
        self.assertEqual(conflict["result_version"], ready["result_version"])
        self.assertEqual(self.tickets.writes, writes)
        self.assertEqual(self.tickets.get("next")["状态"], "待对接")
        self.assertIn("next", [r["id"] for r in self.flow.queue()["blocked"]])
        with self.assertRaises(HandoffError):
            self.start("next")

    def test_batch_plans_warn_about_peers_without_locking_them(self):
        self.ticket()
        self.ticket("next")
        preflight = self.flow.preflight([{"record_id": rid, "paths": ["screen.js"]} for rid in ("task-1", "next")])
        self.assertEqual(preflight["planned_overlap"][0]["overlap"], ["screen.js"])
        self.assertEqual(self.tickets.writes, 0)
        self.assertEqual(self.flow.queue()["counts"]["ready"], 2)
        self.start()

    def test_plan_is_persisted_across_chats_and_stale_plan_is_not_recommended(self):
        self.ticket()
        self.plan("task-1", ["screen.js"])
        self.state = State(self.git, self.state_dir)
        self.flow = self.make_flow()
        self.assertTrue(self.flow.queue()["ready"][0]["scope_checked"])
        self.tickets.rows["task-1"]["验收标准"] = "新增要求"
        self.assertIsNone(self.flow.queue()["recommended"])
        self.assertTrue(self.flow.show("task-1")["current_blocking"]["plan_stale"])
        self.plan("task-1", ["screen.js"])
        self.assertEqual(self.flow.queue()["recommended"], "task-1")

    def test_block_history_keeps_old_version_while_current_hint_reads_new_one(self):
        self.ticket()
        self.ticket("next")
        self.start()
        old = self.flow.ready("task-1", self.result())
        self.start("next", ["other.js"])
        self.flow.block("next", "等旧版本验收", "检查重叠", "验收保护事项", kind="files", waiting_for=["task-1"], paths=["screen.js"])
        history = self.tickets.get("next")["处理结果"]
        (self.root / "screen.js").write_text("const changed = 5;\n")
        new = self.flow.ready("task-1", self.result(), revalidate=True)
        current = self.flow.show("next")["current_blocking"]
        self.assertEqual(current["files"][0]["result_version"], new["result_version"])
        self.assertIn(old["result_version"], history)
        self.assertEqual(self.tickets.get("next")["处理结果"], history)
        with self.assertRaises(HandoffError):
            self.flow.accept("task-1", old["result_version"])
        self.flow.accept("task-1", new["result_version"])
        self.assertFalse(self.flow.show("next")["current_blocking"]["files"])
        self.assertIn("resume", self.flow.show("next")["current_blocking"]["next"])
        self.assertEqual(self.tickets.get("next")["状态"], "阻塞")
        self.assertEqual(self.tickets.get("next")["处理结果"], history)

    def test_other_table_local_protection_is_retained_and_marked_as_unrefreshed(self):
        self.ticket()
        self.state.data["sessions"]["another:table:other"] = {"record_id": "other", "phase": "pending_acceptance", "paths": ["screen.js"], "result_version": "old"}
        detail = self.plan("task-1", ["screen.js"])["plans"][0]["current_blocking"]["files"][0]
        self.assertEqual(detail["freshness"], "local_only")
        with self.assertRaises(HandoffError):
            self.start()

    def test_invalid_batch_does_not_partially_replace_local_plans(self):
        self.ticket()
        self.ticket("next")
        self.plan("task-1", ["screen.js"])
        previous = copy.deepcopy(self.state.data["plans"])
        with self.assertRaises(HandoffError):
            self.flow.preflight([{"record_id": "task-1", "paths": ["other.js"]}, {"record_id": "next", "paths": "screen.js"}])
        self.assertEqual(self.state.data["plans"], previous)
        with self.assertRaises(HandoffError):
            self.plan("next", ["."])
        self.assertEqual(self.state.data["plans"], previous)

    def test_legacy_block_can_be_replanned_without_changing_historical_note(self):
        self.ticket()
        self.ticket("next")
        self.start()
        self.flow.ready("task-1", self.result())
        self.start("next", ["other.js"])
        self.flow.block("next", "旧版文字原因", "已检查", "核对保护事项")
        legacy = "原因：等待旧版本\n所需信息：验收旧结果"
        self.tickets.rows["next"]["处理结果"] = legacy
        self.plan("next", ["screen.js"])
        current = self.flow.show("next")["current_blocking"]
        self.assertEqual(current["files"][0]["id"], "task-1")
        self.assertEqual(self.tickets.get("next")["处理结果"], legacy)

    def test_resumed_task_does_not_reuse_old_structured_dependency_as_current_gate(self):
        self.ticket("dep")
        self.ticket()
        self.start()
        self.flow.block("task-1", "外部安排待定", "已沟通", "明确后恢复", kind="dependency", waiting_for=["dep"])
        self.flow.resume("task-1", "已确认本单可以独立交付")
        self.assertFalse(self.flow.show("task-1")["current_blocking"]["waiting_for"])
        self.assertIn("handoff_block_v1", self.tickets.get("task-1")["处理结果"])


class CompactQueueTests(RepositoryCase):
    def scoped_ticket(self, rid, apps, **patch):
        return self.ticket(rid, 来源=json.dumps({"delivery": self.delivery(apps)}), **patch)

    def test_filter_and_pagination_keep_global_dependencies_and_shared_tickets(self):
        self.scoped_ticket("dep", ["backend"], 状态="已完成")
        self.scoped_ticket("admin", ["admin-app"], 前置依赖=["dep"])
        self.scoped_ticket("shared", ["*"])
        self.scoped_ticket("other", ["other-app"])
        self.ticket("legacy")
        queue = self.flow.queue(apps=["admin-app"], limit=1)
        self.assertEqual(queue["counts"]["ready"], 2)
        self.assertEqual(queue["total_counts"]["ready"], 4)
        self.assertEqual(queue["unscoped_count"], 1)
        self.assertEqual(queue["page"]["remaining"], 1)
        second = self.flow.queue(apps=["admin-app"], limit=1, offset=1)
        self.assertEqual({r["id"] for r in queue["ready"] + second["ready"]}, {"admin", "shared"})

    def test_default_queue_and_claimable_never_echo_full_records_or_evidence(self):
        for index in range(25):
            self.scoped_ticket(str(index), ["admin-app"], 跟进人=[], 描述="SECRET_FULL_DESCRIPTION" * 1000,
                               处理结果="HUGE_OLD_HISTORY" * 1000, _结果证据=json.dumps({"secret": "PRIVATE_EVIDENCE"}))
        queue = self.flow.queue()
        serialized = json.dumps(queue)
        self.assertEqual(queue["counts"]["claimable"], 25)
        self.assertEqual(queue["page"]["shown"], 20)
        self.assertEqual(queue["page"]["remaining"], 5)
        for sentinel in ("SECRET_FULL_DESCRIPTION", "HUGE_OLD_HISTORY", "PRIVATE_EVIDENCE"):
            self.assertNotIn(sentinel, serialized)
            self.assertNotIn(sentinel, json.dumps(self.flow.claimable()))
        self.assertLess(len(serialized), 20000)
