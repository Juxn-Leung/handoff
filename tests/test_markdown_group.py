"""Markdown group flow across two account clients and a shared fake Tencent service."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev-handoff/scripts"))
from handoff_core import Git, State, HandoffError, requirement
from delivery import contract
from tencent_tables import create_group, bind_group, groupify, create, tables, join, identity
from tencent_documents import Documents
from workflow import Workflow
from test_handoff import RepositoryCase
from test_tencent_workflow import Client, World


class GroupWorld(World):
    def __init__(self):
        super().__init__()
        self.folders = {}
        self.docs = {}
        self.fail_append_after_commit = False
        self.fail_append_before_commit = False


def doc_url(file_id):
    encoded = base64.b64encode(file_id.encode()).decode().rstrip("=")
    return "https://docs.qq.com/doc/D" + encoded


class GroupClient(Client):
    def call(self, name, args, write=False):
        w = self.world
        if name != "get_user_info" and self.uid not in w.allowed:
            raise HandoffError("tencent_permission_denied", "not shared", not_executed=True)
        if name == "manage.create_file" and args["file_type"] == "folder":
            file_id = "folder-" + str(len(w.folders) + 1)
            w.folders[file_id] = {"title": args["title"], "children": []}
            return {"file_id": file_id, "url": "https://docs.qq.com/folder/" + file_id}
        if name == "manage.create_file" and args["file_type"] == "smartcanvas":
            file_id = "document-" + str(len(w.docs) + 1)
            w.docs[file_id] = {"title": args["title"], "page": False, "agreement": "", "notes": []}
            w.folders[args["parent_id"]]["children"].append(file_id)
            return {"file_id": file_id, "url": doc_url(file_id)}
        if name == "manage.create_file" and args["file_type"] == "smartsheet":
            result = super().call(name, args, write=write)
            if args.get("parent_id"):
                w.folders[args["parent_id"]]["children"].append(result["file_id"])
            return result
        if name == "manage.query_file_info":
            file_id = args["file_id"]
            if file_id in w.folders:
                return {"file_id": file_id, "type": "folder", "is_folder": True,
                        "url": "https://docs.qq.com/folder/" + file_id}
            if file_id in w.docs:
                return {"file_id": file_id, "type": "smartcanvas", "is_folder": False,
                        "url": doc_url(file_id)}
        if name == "manage.folder_list":
            children = w.folders[args["folder_id"]]["children"]
            return {"list": [{"id": file_id, "title": w.docs[file_id]["title"] if file_id in w.docs else "项目进度",
                              "is_folder": False} for file_id in children[args.get("start", 0):]], "finish": True}
        if name == "manage.move_file":
            w.folders[args["target_folder_id"]]["children"].append(args["file_id"])
            return {}
        if name == "get_content":
            doc = w.docs[args["file_id"]]
            return {"content": "当前约定\n" + doc["agreement"] + "\n" + "\n".join(doc["notes"])}
        if name == "smartcanvas.get_top_level_pages":
            doc = w.docs[args["file_id"]]
            return {"top_level_pages": [{"id": "agreement-page", "type": "Page",
                                          "element": json.dumps({"title": "当前约定"})}] if doc["page"] else []}
        if name == "smartcanvas.get_page_info":
            doc = w.docs[args["file_id"]]
            return {"element_infos": [{"id": "agreement-text", "type": "Text",
                                       "element": json.dumps({"rich_text": {"text": doc["agreement"]}})}] if doc["agreement"] else [],
                    "is_over": True}
        if name == "smartcanvas.create_smartcanvas_element":
            doc = w.docs[args["file_id"]]
            if "pages" in args:
                doc["page"] = True
                return {"element_infos": [{"id": "agreement-page"}]}
            doc["agreement"] = args["texts"][0]["rich_text"]["text"]
            return {"element_infos": [{"id": "agreement-text"}]}
        if name == "smartcanvas.update_element":
            w.docs[args["file_id"]]["agreement"] = args["updates"][0]["text"]["rich_text"]["text"]
            return {"updated_elements": [{"id": "agreement-text"}]}
        if name == "smartcanvas.append_insert_smartcanvas_by_markdown":
            if w.fail_append_before_commit:
                w.fail_append_before_commit = False
                raise HandoffError("write_unknown", "response lost before commit")
            w.docs[args["file_id"]]["notes"].append(args["markdown"])
            if w.fail_append_after_commit:
                w.fail_append_after_commit = False
                raise HandoffError("write_unknown", "response lost")
            return {}
        return super().call(name, args, write=write)


class MarkdownGroupTests(RepositoryCase):
    def setUp(self):
        super().setUp()
        self.world = GroupWorld()
        self.backend = GroupClient(self.world, "backend")
        self.frontend = GroupClient(self.world, "frontend")
        self.bwho, self.fwho = identity(self.backend), identity(self.frontend)
        self.state.project["bindings"] = []
        created = create_group(self.backend, self.state, self.bwho, "项目", "项目")
        self.binding = created["created"]
        self.assertTrue(created["sharing_pending"])
        bt, bl, bm = tables(self.backend, self.binding, self.bwho)
        join(bm, self.state, self.bwho)
        self.frontroot = Path(self.tmp.name) / "frontend"
        subprocess.run(["git", "clone", "-q", str(self.root), str(self.frontroot)], check=True)
        self.fgit = Git(self.frontroot)
        self.fstate = State(self.fgit, Path(self.tmp.name) / "frontend-state")
        bound = bind_group(self.frontend, self.fstate, self.fwho, self.binding["group_id"], "项目")
        self.assertTrue(bound["document_check_pending"])
        ft, fl, fm = tables(self.frontend, bound["bound"], self.fwho)
        join(fm, self.fstate, self.fwho)
        self.bflow = Workflow(self.git, self.state, self.bwho, self.binding, bt, bl, bm, Documents(self.backend))
        self.fflow = Workflow(self.fgit, self.fstate, self.fwho, bound["bound"], ft, fl, fm, Documents(self.frontend))

    def publish(self):
        source = Path(self.tmp.name) / "backend.md"
        source.write_text("# 订单接口\n\n完成订单查询，包含分页和错误处理。", encoding="utf-8")
        draft = {"mode": "markdown", "source": str(source), "tickets": [{
            "key": "orders", "title": "订单列表接入", "description": "前端接入订单列表",
            "agreement": "前端调用 GET /orders，展示列表、空态与失败重试。",
            "change_types": ["feat"], "delivery": self.acceptance_delivery(reviewer="frontend")}]}
        checked = self.bflow.publish(draft, check_only=True)
        self.assertFalse(checked["remote_modified"])
        draft["source_digest"] = checked["source_digest"]
        self.published_draft = draft
        rid = self.bflow.publish(draft)["published"][0]["id"]
        self.assertEqual(self.bflow.publish(draft)["published"][0]["id"], rid)
        return rid

    def test_group_document_link_and_five_column_progress(self):
        rid = self.publish()
        row = self.bflow.tickets.get(rid)
        self.assertEqual(row["状态"], "待对接")
        self.assertEqual(row["下一步"], "待领取")
        self.assertIn(row["_文档ID"], self.world.folders[self.binding["group_id"]]["children"])
        self.assertEqual(self.fflow.show(rid)["document"]["agreement_current"], True)
        verified = bind_group(self.frontend, self.fstate, self.fwho, self.binding["group_id"], "项目")
        self.assertTrue(verified["document_readable"])
        self.assertEqual(verified["documents_checked"], 1)
        sheet = self.world.sheets[self.binding["table_id"]]
        view = json.loads(sheet["view"]["config"])
        shown = {field["field_title"] for field in sheet["fields"] if not view["fieldMetas"][field["field_id"]]["hidden"]}
        self.assertEqual(shown, {"标题", "状态", "跟进人", "下一步", "文档"})

    def test_group_bind_error_identifies_failed_resource(self):
        original = self.frontend.call
        folder_id = self.binding["group_id"]
        def fail_folder(name, args, write=False):
            if name == "manage.query_file_info" and args["file_id"] == folder_id:
                raise HandoffError("mcp_rpc_error", "腾讯文档 MCP 返回错误。", rpc_code=-32603)
            return original(name, args, write=write)
        self.frontend.call = fail_folder
        with self.assertRaises(HandoffError) as captured:
            bind_group(self.frontend, self.fstate, self.fwho, folder_id, "项目")
        self.assertEqual(captured.exception.details["step"], "folder_info")
        self.assertEqual(captured.exception.details["resource_id"], folder_id)
        self.assertIn("网页", captured.exception.details["next"])

        self.frontend.call = original
        rid = self.publish()
        doc_id = self.bflow.tickets.get(rid)["_文档ID"]
        def fail_document(name, args, write=False):
            if name == "smartcanvas.get_top_level_pages" and args["file_id"] == doc_id:
                raise HandoffError("mcp_rpc_error", "腾讯文档 MCP 返回错误。", rpc_code=-32603)
            return original(name, args, write=write)
        self.frontend.call = fail_document
        with self.assertRaises(HandoffError) as captured:
            bind_group(self.frontend, self.fstate, self.fwho, folder_id, "项目")
        self.assertEqual(captured.exception.details["step"], "ticket_document")
        self.assertEqual(captured.exception.details["resource_id"], doc_id)
        self.assertIn("文档", captured.exception.details["next"])

    def test_group_bind_accepts_shared_folder_link(self):
        folder_id = self.binding["group_id"]
        link = "https://docs.qq.com/desktop/mydoc/folder/" + folder_id + "?from=share"
        result = bind_group(self.frontend, self.fstate, self.fwho, link, "项目")
        self.assertEqual(result["bound"]["group_id"], folder_id)
        with self.assertRaises(HandoffError):
            bind_group(self.frontend, self.fstate, self.fwho,
                       "https://docs.qq.com.evil.test/folder/" + folder_id, "项目")

    def test_group_bind_rejects_ticket_document_outside_folder(self):
        rid = self.publish()
        file_id = self.bflow.tickets.get(rid)["_文档ID"]
        self.world.folders[self.binding["group_id"]]["children"].remove(file_id)
        with self.assertRaisesRegex(HandoffError, "不在项目组目录"):
            bind_group(self.frontend, self.fstate, self.fwho, self.binding["group_id"], "项目")

    def test_group_bind_checks_later_documents(self):
        self.publish()
        self.bflow.tickets.create_once("_工单键", "second-ticket", {"标题": "第二单", "状态": "待对接",
            "_文档ID": "document-outside", "文档": doc_url("document-outside")})
        with self.assertRaisesRegex(HandoffError, "不在项目组目录"):
            bind_group(self.frontend, self.fstate, self.fwho, self.binding["group_id"], "项目")

    def test_group_bind_rejects_document_reuse(self):
        rid = self.publish()
        doc_id = self.bflow.tickets.get(rid)["_文档ID"]
        self.bflow.tickets.create_once("_工单键", "second-ticket", {"标题": "第二单", "状态": "待对接",
            "_文档ID": doc_id, "文档": doc_url(doc_id)})
        with self.assertRaisesRegex(HandoffError, "同一份文档"):
            bind_group(self.frontend, self.fstate, self.fwho, self.binding["group_id"], "项目")

    def test_dialogue_does_not_change_requirement_but_agreement_does(self):
        rid = self.publish()
        before = requirement(self.fflow.tickets.get(rid))
        self.fflow.append_note(rid, "前端：先讨论空态文案。", "discussion-1")
        self.assertEqual(before, requirement(self.fflow.tickets.get(rid)))
        self.assertEqual(self.fflow.show(rid)["document"]["agreement_current"], True)
        self.fflow.claim(rid, before)
        self.fflow.start(rid, ["screen.js"], before)
        (self.frontroot / "screen.js").write_text("fetch('/orders');\n")
        ready = self.fflow.ready(rid, self.result())
        self.assertEqual(self.fflow.tickets.get(rid)["状态"], "待验收")
        self.assertIn("提交待验收", self.fflow.show(rid)["document"]["content"])
        self.bflow.update_agreement(rid, self.bflow.documents.agreement(self.bflow.tickets.get(rid)["_文档ID"])["hash"],
                                    "前端调用 GET /orders，增加分页总数展示。", self.acceptance_delivery(reviewer="frontend"))
        self.assertNotEqual(before, requirement(self.fflow.tickets.get(rid)))
        self.assertEqual(self.fflow.tickets.get(rid)["状态"], "待对接")
        with self.assertRaisesRegex(HandoffError, "只能确认待验收"):
            self.fflow.confirm(rid, ready["result_version"], self.confirmation(self.fflow, rid))
        self.fflow.start(rid, ["screen.js"], requirement(self.fflow.tickets.get(rid)))

    def test_direct_agreement_edit_blocks_progress_until_synced(self):
        rid = self.publish()
        before = requirement(self.fflow.tickets.get(rid))
        file_id = self.bflow.tickets.get(rid)["_文档ID"]
        self.world.docs[file_id]["agreement"] = "后端明确改为 GET /v2/orders。"
        self.assertFalse(self.fflow.show(rid)["document"]["agreement_current"])
        with self.assertRaisesRegex(HandoffError, "当前约定已变化"):
            self.fflow.claim(rid, before)
        with self.assertRaisesRegex(HandoffError, "交付与验收契约"):
            self.bflow.sync_agreement(rid)
        synced = self.bflow.sync_agreement(rid, self.acceptance_delivery(reviewer="frontend"))
        self.assertNotEqual(before, synced["requirement_version"])
        self.fflow.claim(rid, synced["requirement_version"])

    def test_agreement_change_requires_valid_revised_acceptance_before_edit(self):
        rid = self.publish()
        row = self.bflow.tickets.get(rid)
        before = requirement(row)
        original_text = self.bflow.documents.agreement(row["_文档ID"])["text"]
        with self.assertRaises(HandoffError):
            self.bflow.update_agreement(rid, row["_约定指纹"], "增加分页总数", {"invalid": True})
        self.assertEqual(self.bflow.documents.agreement(row["_文档ID"])["text"], original_text)
        revised = self.acceptance_delivery(reviewer="frontend")
        revised["criteria"][0]["expected"] = "语法检查成功，并显示分页总数"
        changed = self.bflow.update_agreement(rid, row["_约定指纹"], "增加分页总数", revised)
        self.assertNotEqual(before, changed["requirement_version"])
        self.assertEqual(contract(self.bflow.tickets.get(rid))["criteria"][0]["expected"],
                         "语法检查成功，并显示分页总数")
        replay = self.bflow.publish(self.published_draft)
        self.assertEqual(replay["published"][0]["id"], rid)
        self.assertEqual(replay["overlap_warnings"], [])

    def test_changed_source_cannot_publish_stale_draft(self):
        source = Path(self.tmp.name) / "changing.md"
        source.write_text("# 原要求", encoding="utf-8")
        draft = {"mode": "markdown", "source": str(source), "tickets": [{"key": "changing",
                 "title": "变更任务", "description": "接入接口", "agreement": "接入 GET /orders",
                 "delivery": self.acceptance_delivery(reviewer="frontend")}]}
        draft["source_digest"] = self.bflow.publish(draft, check_only=True)["source_digest"]
        source.write_text("# 新要求", encoding="utf-8")
        with self.assertRaisesRegex(HandoffError, "来源文档已变化"):
            self.bflow.publish(draft)
        self.assertEqual(self.bflow.tickets.records(), [])

    def test_unknown_append_readback_avoids_duplicate(self):
        rid = self.publish()
        file_id = self.bflow.tickets.get(rid)["_文档ID"]
        before = len(self.world.docs[file_id]["notes"])
        self.world.fail_append_after_commit = True
        self.bflow.append_note(rid, "后端：补充联调环境说明。", "stable-note")
        self.bflow.append_note(rid, "后端：补充联调环境说明。", "stable-note")
        self.assertEqual(len(self.world.docs[file_id]["notes"]), before + 1)

    def test_unconfirmed_append_stops_automatic_replay(self):
        rid = self.publish()
        file_id = self.bflow.tickets.get(rid)["_文档ID"]
        before = len(self.world.docs[file_id]["notes"])
        self.world.fail_append_before_commit = True
        with self.assertRaises(HandoffError) as error:
            self.bflow.append_note(rid, "后端：待核对的消息。", "maybe-note")
        self.assertEqual(error.exception.details["event_id"], "maybe-note")
        with self.assertRaisesRegex(HandoffError, "停止自动重发"):
            self.bflow.sync()
        self.assertEqual(len(self.world.docs[file_id]["notes"]), before)

    def test_existing_tencent_document_can_be_source(self):
        self.world.docs["external-source"] = {"title": "后端说明", "page": True,
                                              "agreement": "原始接口说明", "notes": ["GET /orders"]}
        source = doc_url("external-source")
        draft = {"mode": "markdown", "source": source, "tickets": [{"key": "from-url",
                 "title": "文档来源任务", "description": "接入订单接口", "agreement": "接入 GET /orders",
                 "delivery": self.acceptance_delivery(reviewer="frontend")}]}
        draft["source_digest"] = self.bflow.publish(draft, check_only=True)["source_digest"]
        rid = self.bflow.publish(draft)["published"][0]["id"]
        self.assertEqual(self.bflow.show(rid)["document"]["agreement"], "接入 GET /orders")

    def test_legacy_ticket_migrates_on_next_processing(self):
        row = self.bflow.tickets.create_once("_工单键", "legacy-key", {
            "标题": "旧订单任务", "描述": "展示订单", "交接说明": "GET /orders",
            "验收标准": "列表正确", "来源": "{}", "状态": "待对接", "跟进人": [],
            "关联工单": [], "前置依赖": [], "变更类型": ["feat"], "优先级": "普通"})
        migrated = self.bflow.migrate_document(row["id"])
        self.assertTrue(migrated["migrated"])
        self.assertTrue(self.bflow.migrate_document(row["id"])["already_migrated"])
        self.assertEqual(self.bflow.show(row["id"])["document"]["agreement_current"], True)


class GroupUpgradeTests(RepositoryCase):
    def test_old_table_moves_into_group_without_replacing_rows(self):
        world = GroupWorld()
        backend = GroupClient(world, "backend")
        who = identity(backend)
        self.state.project["bindings"] = []
        binding = create(backend, self.state, who, "旧项目", "旧项目")["created"]
        tickets, _, _ = tables(backend, binding, who)
        row = tickets.create_once("_工单键", "existing-task", {"标题": "保留此任务", "描述": "旧说明",
            "状态": "待对接", "来源": "{}", "交接说明": "旧交接", "验收标准": "旧验收"})
        upgraded = groupify(backend, self.state, who, binding)
        self.assertEqual(upgraded["bound"]["base_id"], binding["base_id"])
        self.assertIn(binding["base_id"], world.folders[upgraded["group_id"]]["children"])
        new_tickets, _, _ = tables(backend, upgraded["bound"], who)
        self.assertEqual(new_tickets.get(row["id"])["标题"], "保留此任务")
        self.assertIn("文档", new_tickets.expected)


class BlockClient:
    """Minimal current SmartCanvas read/edit API, separate from the old element API fake."""
    def __init__(self):
        self.folders = {"": [], "group": []}
        self.docs = {}

    def list_tools(self):
        return {name: {} for name in ("manage.create_file", "manage.folder_list", "manage.query_file_info",
                "get_content", "smartcanvas.get_top_level_pages", "create_smartcanvas_by_mdx",
                "manage.move_file", "smartcanvas.read", "smartcanvas.edit")}

    def call(self, name, args, write=False):
        if name == "manage.folder_list":
            return {"list": [{"id": fid, "title": self.docs[fid]["title"]} for fid in self.folders[args["folder_id"]]],
                    "finish": True}
        if name == "create_smartcanvas_by_mdx":
            fid = "doc-" + str(len(self.docs) + 1)
            agreement = args["mdx"].split("## 当前约定\n\n", 1)[1].split("\n\n---", 1)[0]
            self.docs[fid] = {"title": args["title"], "agreement": agreement, "notes": args["mdx"], "id": "agreement-1"}
            self.folders[""].append(fid)
            return {"file_id": fid}
        if name == "manage.move_file":
            fid = args["file_id"]
            self.folders[""].remove(fid)
            self.folders[args["target_folder_id"]].append(fid)
            return {}
        if name == "manage.query_file_info":
            fid = args["file_id"]
            return {"file_id": fid, "type": "smartcanvas", "url": doc_url(fid)}
        if name == "smartcanvas.read":
            doc = self.docs[args["file_id"]]
            return {"content": (f'<Heading id="current">\n  当前约定\n</Heading>\n\n'
                    f'<Paragraph id="{doc["id"]}">\n  {doc["agreement"]}\n</Paragraph>\n\n'
                    '<Divider id="divider" />\n\n<Heading id="dialogue">\n  来源与对话\n</Heading>\n\n'
                    + doc["notes"]), "next_token": ""}
        if name == "get_content":
            doc = self.docs[args["file_id"]]
            return {"content": doc["agreement"] + doc["notes"]}
        if name == "smartcanvas.edit":
            doc = self.docs[args["file_id"]]
            if args["action"] == "UPDATE":
                doc["agreement"] = args["content"]
                doc["id"] += "-new"
            else:
                doc["notes"] += args["content"]
            return {}
        raise AssertionError(name)


class CurrentBlockApiTests(unittest.TestCase):
    def test_paginated_read_drops_repeated_front_matter(self):
        class Pages:
            def __init__(self):
                self.calls = 0

            def call(self, name, args, write=False):
                self.calls += 1
                if self.calls == 1:
                    return {"content": "---\ntitle: 工单\n---\n\n<Heading>一</Heading>", "next_token": "page-2"}
                return {"content": "---\ntitle: 工单\n---\n\n<Paragraph>二</Paragraph>", "next_token": ""}

        result = Documents(Pages())._blocks("doc-1")
        self.assertEqual(result.count("title: 工单"), 1)
        self.assertIn("<Paragraph>二</Paragraph>", result)

    def test_live_api_shape_creates_moves_reads_and_updates(self):
        client = BlockClient()
        docs = Documents(client)
        created = docs.create("group", "测试工单", "stable-key", "第一条\n第二条",
                              "来源正文", "成员", "source.md")
        self.assertEqual(client.folders["group"], [created["file_id"]])
        self.assertEqual(docs.agreement(created["file_id"])["text"], "第一条\n第二条")
        self.assertIn("来源正文", docs.source(created["url"])["content"])
        docs.append_once(created["file_id"], "event-1", "成员", "普通讨论")
        docs.append_once(created["file_id"], "event-1", "成员", "普通讨论")
        self.assertEqual(client.docs[created["file_id"]]["notes"].count("event-1"), 1)
        changed = docs.update_agreement(created["file_id"], created["agreement_hash"], "第三条\n第四条")
        self.assertEqual(changed["text"], "第三条\n第四条")
        self.assertNotEqual(changed["hash"], created["agreement_hash"])


if __name__ == "__main__":
    unittest.main()
