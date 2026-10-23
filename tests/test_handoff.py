"""Offline workflow invariants. Fakes do not count as real cross-account verification."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "dev-handoff/scripts"
sys.path.insert(0, str(SCRIPTS))
from handoff_core import Git, State, HandoffError, digest, owned, plan_queue, requirement, exclusive_lock
from workflow import Workflow
from table_schema import normalized, TICKET_FIELDS, LOG_FIELDS, ticket_links, log_links


BACKEND = {"profile": "corp:backend", "corp_id": "corp", "user_id": "backend", "name": "后端同事"}
FRONTEND = {"profile": "corp:frontend", "corp_id": "corp", "user_id": "frontend", "name": "前端同事"}


def person(who):
    return [{"userId": who["user_id"], "corpId": who["corp_id"]}]


class MemoryTable:
    """Remote state shared across new clients, with fail-after-commit injection."""
    def __init__(self):
        self.rows = {}
        self.writes = 0
        self.fail_create_after_commit = False
        self.fail_update_after_commit = False
        self.fail_create_before = False

    def records(self, ids=None, equals=None, names=None):
        rows = list(self.rows.values())
        if ids:
            rows = [r for r in rows if r["id"] in ids]
        if equals:
            rows = [r for r in rows if all(r.get(k) == v for k, v in equals.items())]
        return copy.deepcopy(rows)

    def get(self, rid):
        if rid not in self.rows:
            raise HandoffError("record_missing", "missing")
        return copy.deepcopy(self.rows[rid])

    def matches(self, row, values):
        return all(row.get(k) == v for k, v in values.items())

    def create_once(self, key_field, key, values, uncertain=False):
        rows = self.records(equals={key_field: key})
        if len(rows) > 1:
            raise HandoffError("duplicate_key", "duplicate")
        if rows:
            return rows[0]
        if uncertain:
            raise HandoffError("write_unknown", "unknown prior write")
        if self.fail_create_before:
            self.fail_create_before = False
            raise HandoffError("write_unknown", "no response")
        self.writes += 1
        rid = f"rec-{len(self.rows)+1}"
        self.rows[rid] = {"id": rid, **copy.deepcopy(values), key_field: key}
        if self.fail_create_after_commit:
            self.fail_create_after_commit = False
            raise HandoffError("write_unknown", "response lost after commit")
        return self.get(rid)

    def update(self, rid, values):
        self.writes += 1
        self.rows[rid].update(copy.deepcopy(values))
        if self.fail_update_after_commit:
            self.fail_update_after_commit = False
            raise HandoffError("write_unknown", "response lost after commit")
        return self.get(rid)


class RepositoryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.git_cmd("init", "-q")
        self.git_cmd("config", "user.name", "Test")
        self.git_cmd("config", "user.email", "test@example.invalid")
        (self.root / "api.py").write_text("value = 1\n")
        (self.root / "screen.js").write_text("const value = 1;\n")
        (self.root / "other.js").write_text("const other = 1;\n")
        self.git_cmd("add", ".")
        self.git_cmd("commit", "-qm", "initial")
        self.git = Git(self.root)
        self.state_dir = Path(self.tmp.name) / "personal"
        self.state = State(self.git, self.state_dir)
        self.binding = {"alias": "示例", "url": "https://docs.qq.com/smartsheet/Dexample",
                        "base_id": "base", "table_id": "tickets", "log_table_id": "logs", "corp_id": "corp"}
        self.state.project["bindings"] = [self.binding]
        self.state.save_project()
        self.tickets, self.logs = MemoryTable(), MemoryTable()
        self.members = MemoryTable()
        for identity, role in ((BACKEND, "后端"), (FRONTEND, "前端")):
            self.state.data["identities"][identity["profile"]] = {"role": role}
            self.members.rows[identity["user_id"]] = {"id": identity["user_id"], "账号ID": identity["user_id"], "姓名": identity["name"]}
        self.flow = self.make_flow(FRONTEND)

    def make_flow(self, identity=FRONTEND):
        return Workflow(self.git, self.state, identity, self.binding, self.tickets, self.logs, self.members)

    def git_cmd(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True).stdout

    def stage_backend(self):
        (self.root / "api.py").write_text("value = 2\n")
        self.git_cmd("add", "api.py")

    def draft(self, key="feature", paths=None):
        return {"snapshot": self.git.staged()["fingerprint"], "tickets": [{
            "key": key, "title": "读取订单", "description": "页面展示订单",
            "change_types": ["feat"], "paths": paths or ["api.py"],
            "handoff": "GET /orders，无请求体；返回 {items: []}。测试环境已可用。",
            "acceptance": "订单页能展示返回数据；接口失败显示重试入口。", "critical_unknowns": [],
            "delivery": self.acceptance_delivery()}]}

    def delivery(self, apps=None):
        return {"apps": apps or ["orders-app"], "scope": "订单列表页面", "deliverable": "接入订单列表，处理空状态和失败重试",
                "acceptance_owner": "接手方操作者", "shared_contracts": [],
                "checks": [{"id": "syntax", "description": "前端语法检查", "kind": "local", "owner": "接手方", "required": True}],
                "external_checks": []}

    def acceptance_delivery(self, reviewer="frontend"):
        return {**self.delivery(), "reviewer_id": reviewer,
                "criteria": [{"id": "syntax-valid", "description": "页面代码语法正确", "steps": ["执行页面语法检查"],
                              "expected": "检查命令成功退出", "check_ids": ["syntax"]}]}

    def confirmation(self, flow, rid):
        report = flow.review(rid)
        return {"result_version": report["result_version"], "items": [
            {"criterion_id": c["id"], "outcome": "passed", "observed": "已核对本项操作、预期与所附结果证据。",
             "evidence_digests": [e["digest"] for e in c["evidence"]]} for c in report["acceptance_matrix"]]}

    def ticket(self, rid="task-1", **patch):
        row = {"id": rid, "标题": "接入订单接口", "描述": "展示订单", "变更类型": ["feat"],
               "跟进人": person(FRONTEND), "状态": "待对接", "优先级": "普通", "交接说明": "GET /orders",
               "验收标准": "展示订单", "来源": "{}", "关联工单": [], "前置依赖": [],
               "创建时间": "2026-10-21T12:00:00+00:00", **patch}
        self.tickets.rows[rid] = row
        return self.tickets.get(rid)

    def start(self, rid="task-1", paths=None):
        return self.flow.start(rid, paths or ["screen.js"], requirement(self.tickets.get(rid)))

    def result(self):
        return {"summary": "订单页已接入，包含加载和错误重试。", "checks": [{"id": "syntax", "kind": "local", "command": "node --check screen.js", "outcome": "passed"}],
                "acceptance": [{"criterion_id": "syntax-valid", "actual": "页面代码检查成功退出", "evidence": [{"check_id": "syntax", "content": "Offline fixture: syntax check exit code 0"}]}]}


class GitTests(RepositoryCase):
    def test_staged_context_never_reads_unstaged_change(self):
        self.stage_backend()
        fingerprint = self.git.staged()["fingerprint"]
        (self.root / "api.py").write_text("SECRET_UNSTAGED = 'never publish'\n")
        context = self.git.staged_context()
        self.assertIn("+value = 2", context["diff"])
        self.assertNotIn("SECRET_UNSTAGED", context["diff"])
        self.assertEqual(self.git.index_file("api.py"), "value = 2\n")
        self.assertEqual(fingerprint, context["snapshot"]["fingerprint"])

    def test_empty_index(self):
        with self.assertRaisesRegex(HandoffError, "暂存区没有"):
            self.git.staged()

    def test_unborn_repository_and_deleted_file(self):
        fresh = self.root / "fresh"
        fresh.mkdir()
        subprocess.run(["git", "init", "-q", str(fresh)], check=True)
        (fresh / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(fresh), "add", "new.txt"], check=True)
        self.assertIsNone(Git(fresh).staged()["base_commit"])
        self.git_cmd("rm", "api.py")
        self.assertIn("api.py", self.git.staged()["paths"])
        self.assertEqual(self.git.code(["api.py"]), {"api.py": {"missing": True}})

    def test_path_escape_and_external_symlink_rejected(self):
        (self.root / "outside").symlink_to(Path(self.tmp.name))
        for path in ("../other", "/tmp/file", ".git/config", "outside/file"):
            with self.subTest(path=path), self.assertRaises(HandoffError):
                self.git.clean_paths([path])

    def test_local_lock_released_and_exclusive(self):
        with exclusive_lock(self.state.lock_path):
            with self.assertRaises(HandoffError):
                with exclusive_lock(self.state.lock_path):
                    self.fail("lock must be exclusive")
        with exclusive_lock(self.state.lock_path):
            pass


class PublishingTests(RepositoryCase):
    def test_backend_publish_frontend_pickup_full_cycle(self):
        self.stage_backend()
        backend = self.make_flow(BACKEND)
        published = backend.publish(self.draft())
        rid = published["published"][0]["id"]
        row = self.tickets.get(rid)
        self.assertEqual(row["发布人"], person(BACKEND))
        self.assertEqual(row["跟进人"], [])
        self.tickets.rows[rid]["跟进人"] = person(FRONTEND)
        self.assertEqual(self.flow.queue()["recommended"], rid)
        self.start(rid)
        (self.root / "screen.js").write_text("fetch('/orders');\n")
        ready = self.flow.ready(rid, self.result())
        self.assertEqual(self.tickets.get(rid)["状态"], "待验收")
        self.flow.confirm(rid, ready["result_version"], self.confirmation(self.flow, rid))
        self.flow.accept(rid, ready["result_version"])
        self.assertEqual(self.tickets.get(rid)["状态"], "已完成")
        self.assertEqual(len(self.logs.rows), 4)

    def test_repeat_publish_keeps_manual_edits(self):
        self.stage_backend()
        backend = self.make_flow(BACKEND)
        draft = self.draft()
        first = backend.publish(draft)["published"][0]["id"]
        self.tickets.rows[first]["跟进人"] = person(FRONTEND)
        self.tickets.rows[first]["描述"] = "人工修改"
        self.assertEqual(first, backend.publish(draft)["published"][0]["id"])
        self.assertEqual(len(self.tickets.rows), 1)
        self.assertEqual(self.tickets.get(first)["描述"], "人工修改")

    def test_changed_index_and_critical_unknowns_stop_before_writing(self):
        self.stage_backend()
        backend = self.make_flow(BACKEND)
        draft = self.draft()
        draft["tickets"][0]["critical_unknowns"] = ["鉴权方式？"]
        with self.assertRaises(HandoffError):
            backend.publish(draft)
        self.assertFalse(self.tickets.rows)
        draft["tickets"][0]["critical_unknowns"] = []
        (self.root / "api.py").write_text("value = 3\n")
        self.git_cmd("add", "api.py")
        with self.assertRaises(HandoffError):
            backend.publish(draft)

    def test_independent_features_split_and_uncovered_files_rejected(self):
        self.stage_backend()
        (self.root / "other.js").write_text("const other = 2;\n")
        self.git_cmd("add", "other.js")
        draft = self.draft()
        backend = self.make_flow(BACKEND)
        with self.assertRaises(HandoffError):
            backend.publish(draft)
        second = copy.deepcopy(draft["tickets"][0])
        second.update(key="second", paths=["other.js"], title="另一个事项")
        draft["tickets"].append(second)
        self.assertEqual(len(backend.publish(draft)["published"]), 2)

    def test_partial_publish_recovers_without_duplicate(self):
        self.stage_backend()
        self.tickets.fail_create_after_commit = True
        backend = self.make_flow(BACKEND)
        draft = self.draft()
        with self.assertRaises(HandoffError):
            backend.publish(draft)
        self.state = State(self.git, self.state_dir)
        self.make_flow(BACKEND).publish(draft)
        self.assertEqual(self.tickets.writes, 1)

    def test_unknown_create_without_visible_record_never_replays(self):
        self.stage_backend()
        self.tickets.fail_create_before = True
        backend = self.make_flow(BACKEND)
        draft = self.draft()
        with self.assertRaises(HandoffError):
            backend.publish(draft)
        with self.assertRaises(HandoffError):
            backend.publish(draft)
        self.assertEqual(self.tickets.writes, 0)


class WorkflowTests(RepositoryCase):
    def test_remote_dates_survive_live_minute_precision_without_weakening_comparison(self):
        from datetime import datetime
        class MinuteLogTable(MemoryTable):
            def create_once(inner, key_field, key, values, uncertain=False):
                # Model the live service returning dates without seconds or fractions.
                returned = dict(values)
                returned["时间"] = datetime.fromisoformat(values["时间"]).replace(second=0, microsecond=0).isoformat()
                if returned["时间"] != values["时间"]:
                    raise HandoffError("write_unknown", "date readback differs")
                return super().create_once(key_field, key, returned, uncertain)
        self.flow.logs = MinuteLogTable()
        self.ticket()
        self.start()
        self.flow.ready("task-1", self.result())
        self.flow.accept("task-1", self.tickets.get("task-1")["结果版本"])
        accepted = self.tickets.get("task-1")
        self.assertEqual(accepted["状态"], "已完成")
        stamp = datetime.fromisoformat(accepted["验收时间"])
        self.assertEqual((stamp.second, stamp.microsecond), (0, 0))

    def test_continue_can_leave_two_pending_acceptance(self):
        self.ticket()
        self.ticket("task-2")
        self.start()
        (self.root / "screen.js").write_text("const value = 2;\n")
        self.flow.ready("task-1", self.result())
        self.start("task-2", ["other.js"])
        (self.root / "other.js").write_text("const other = 2;\n")
        self.flow.ready("task-2", self.result())
        self.assertEqual(self.flow.queue()["counts"]["pending_acceptance"], 2)
        self.assertEqual(self.tickets.get("task-1")["状态"], "待验收")
        for rid in ("task-1", "task-2"):
            self.flow.accept(rid, self.tickets.get(rid)["结果版本"])

    def test_pending_file_overlap_prevents_start(self):
        self.ticket()
        self.ticket("task-2")
        self.start()
        self.flow.ready("task-1", self.result())
        with self.assertRaises(HandoffError) as cm:
            self.start("task-2")
        self.assertEqual(cm.exception.code, "pending_code_conflict")
        self.assertEqual(self.tickets.get("task-2")["状态"], "待对接")

    def test_related_but_independent_tasks_can_continue_and_revalidate_separately(self):
        self.ticket(关联工单=["task-2"])
        self.ticket("task-2", 关联工单=["task-1"])
        self.start()
        first = self.flow.ready("task-1", self.result())
        self.start("task-2", ["other.js"])
        (self.root / "other.js").write_text("const other = 2;\n")
        second = self.flow.ready("task-2", self.result())
        (self.root / "screen.js").write_text("const value = 3;\n")
        refreshed = self.flow.ready("task-1", self.result(), revalidate=True)
        self.assertNotEqual(first["result_version"], refreshed["result_version"])
        self.flow.accept("task-2", second["result_version"])
        self.flow.accept("task-1", refreshed["result_version"])

    def test_identity_switch_cannot_resume_another_persons_session(self):
        self.ticket()
        self.start()
        with self.assertRaises(HandoffError) as cm:
            self.make_flow(BACKEND).session("task-1")
        self.assertEqual(cm.exception.code, "session_missing")

    def test_dependency_reopened_after_ready_prevents_acceptance(self):
        self.ticket("prerequisite", 状态="已完成")
        self.ticket(前置依赖=["prerequisite"])
        self.start()
        result = self.flow.ready("task-1", self.result())
        self.tickets.rows["prerequisite"]["状态"] = "待对接"
        with self.assertRaises(HandoffError) as cm:
            self.flow.accept("task-1", result["result_version"])
        self.assertEqual(cm.exception.code, "dependency_blocked")

    def test_manual_code_edit_prevents_accepting_old_result(self):
        self.ticket()
        self.start()
        result = self.flow.ready("task-1", self.result())
        (self.root / "screen.js").write_text("const changed = 99;\n")
        with self.assertRaises(HandoffError) as cm:
            self.flow.accept("task-1", result["result_version"])
        self.assertEqual(cm.exception.code, "code_changed")
        new = self.flow.ready("task-1", self.result(), revalidate=True)
        with self.assertRaises(HandoffError):
            self.flow.accept("task-1", result["result_version"])
        self.flow.accept("task-1", new["result_version"])

    def test_requirement_or_owner_change_prevents_review(self):
        self.ticket()
        self.start()
        result = self.flow.ready("task-1", self.result())
        self.tickets.rows["task-1"]["验收标准"] = "新增验收标准"
        with self.assertRaises(HandoffError) as cm:
            self.flow.accept("task-1", result["result_version"])
        self.assertEqual(cm.exception.code, "requirements_changed")
        self.tickets.rows["task-1"]["跟进人"] = person(BACKEND)
        with self.assertRaises(HandoffError) as cm:
            self.flow.reject("task-1", "退回")
        self.assertEqual(cm.exception.code, "owner_changed")

    def test_state_recovers_across_chat_restart(self):
        self.ticket()
        self.start()
        result = self.flow.ready("task-1", self.result())
        self.state = State(self.git, self.state_dir)
        self.make_flow().accept("task-1", result["result_version"])

    def test_failed_check_and_unplanned_file_never_mark_ready(self):
        self.ticket()
        self.start()
        result = self.result()
        result["checks"][0]["outcome"] = "failed"
        with self.assertRaises(HandoffError):
            self.flow.ready("task-1", result)
        (self.root / "other.js").write_text("const changed = 2;\n")
        with self.assertRaises(HandoffError) as cm:
            self.flow.ready("task-1", self.result())
        self.assertEqual(cm.exception.code, "scope_changed")

    def test_extend_checks_pending_conflict_before_expanding(self):
        self.ticket()
        self.ticket("task-2")
        self.start()
        self.flow.ready("task-1", self.result())
        self.start("task-2", ["other.js"])
        with self.assertRaises(HandoffError):
            self.flow.extend("task-2", ["screen.js"])

    def test_switching_tables_does_not_bypass_pending_code_protection(self):
        self.ticket()
        self.start()
        self.flow.ready("task-1", self.result())
        other_tickets = MemoryTable()
        other_tickets.rows["other-task"] = {**self.tickets.get("task-1"), "id": "other-task", "状态": "待对接", "_执行标记": ""}
        binding = {**self.binding, "base_id": "another-base", "alias": "另一个项目"}
        other = Workflow(self.git, self.state, FRONTEND, binding, other_tickets, MemoryTable())
        with self.assertRaises(HandoffError) as cm:
            other.start("other-task", ["screen.js"], requirement(other_tickets.get("other-task")))
        self.assertEqual(cm.exception.code, "pending_code_conflict")

    def test_block_note_and_explicit_resume(self):
        self.ticket()
        self.start()
        self.tickets.rows["task-1"]["描述"] = "需求中途修改"
        self.flow.block("task-1", "鉴权缺失", "检查接口定义", "提供鉴权规则")
        self.assertEqual(self.flow.queue()["counts"]["blocked"], 1)
        self.assertIn("鉴权缺失", list(self.logs.rows.values())[-1]["内容"])
        self.flow.resume("task-1", "已补齐鉴权说明")
        self.start()

    def test_log_failure_recovered_without_rewriting_ticket(self):
        self.ticket()
        self.logs.fail_create_after_commit = True
        with self.assertRaises(HandoffError):
            self.start()
        writes = self.tickets.writes
        self.state = State(self.git, self.state_dir)
        flow = self.make_flow()
        flow.sync()
        self.assertEqual(self.tickets.writes, writes)
        self.assertEqual(len(self.logs.rows), 1)
        self.assertEqual(flow.session("task-1")["phase"], "working")

    def test_unknown_update_recovered_after_commit(self):
        self.ticket()
        self.tickets.fail_update_after_commit = True
        with self.assertRaises(HandoffError):
            self.start()
        self.flow.sync()
        self.assertEqual(self.tickets.writes, 1)

    def test_conflicting_recovery_can_be_explicitly_abandoned_without_remote_write(self):
        self.ticket()
        self.logs.fail_create_after_commit = True
        with self.assertRaises(HandoffError):
            self.start()
        self.tickets.rows["task-1"]["描述"] = "人工修改的需求"
        with self.assertRaises(HandoffError):
            self.flow.sync()
        pending = self.flow._pending_operations()
        writes = self.tickets.writes
        result = self.flow.abandon_operation(pending[0]["id"], "已与操作者核对，原任务另外恢复")
        self.assertFalse(result["remote_modified"])
        self.assertEqual(self.tickets.writes, writes)
        self.assertEqual(self.flow._pending_operations(), [])

    def test_reject_reports_downstream_and_preserves_history(self):
        self.ticket()
        self.ticket("task-2", 前置依赖=["task-1"])
        self.start()
        self.flow.ready("task-1", self.result())
        result = self.flow.reject("task-1", "缺少错误重试")
        self.assertEqual(result["affected"], ["task-2"])
        self.assertEqual(self.tickets.get("task-1")["状态"], "待对接")
        self.assertEqual(len(self.logs.rows), 3)

    def test_unfinished_dependency_cannot_be_started(self):
        self.ticket()
        self.ticket("task-2", 前置依赖=["task-1"])
        with self.assertRaises(HandoffError):
            self.start("task-2", ["other.js"])


class QueueTests(RepositoryCase):
    def test_owner_filter_completed_exclusion_related_priority_and_cycles(self):
        self.ticket("previous", 状态="已完成", 关联工单=["related"])
        self.ticket("related", 优先级="低")
        self.ticket("urgent", 优先级="紧急")
        self.ticket("other-owner", 跟进人=person(BACKEND))
        self.ticket("cycle-a", 前置依赖=["cycle-b"])
        self.ticket("cycle-b", 前置依赖=["cycle-a"])
        queue = plan_queue(self.tickets.records(), FRONTEND, "previous")
        self.assertEqual([r["id"] for r in queue["ready"]], ["related", "urgent"])
        self.assertEqual({r["id"] for r in queue["blocked"]}, {"cycle-a", "cycle-b"})
        self.assertTrue(all(r["queue_reason"] == "循环依赖" for r in queue["blocked"]))

    def test_same_name_different_id_or_organization_does_not_match(self):
        row = self.ticket()
        self.assertFalse(owned(row, {**FRONTEND, "user_id": "someone-else"}))
        self.assertFalse(owned(row, {**FRONTEND, "corp_id": "another-org"}))

    def test_multiple_bindings_and_same_name_require_selection(self):
        self.state.project["bindings"].append({**self.binding, "base_id": "second", "url": "https://example.invalid/second"})
        for selector in (None, "示例"):
            with self.assertRaises(HandoffError):
                self.state.select(selector)
        self.assertEqual(self.state.select("second")["base_id"], "second")


if __name__ == "__main__":
    unittest.main()
