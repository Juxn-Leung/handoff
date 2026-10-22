"""Two personal-account clients, distinct Git workspaces, real Tencent wire shapes."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev-handoff/scripts"))
from handoff_core import Git, State, HandoffError, requirement
from tencent_tables import TencentTable, CORP, identity, create, bind, tables, join
from workflow import Workflow
from test_handoff import RepositoryCase


class World:
    def __init__(self):
        self.sheets = {}
        self.allowed = {"backend", "frontend"}
        self.fail_record = None
        self.writes = 0
        self.file_created = 0


class Client:
    def __init__(self, world, uid):
        self.world, self.uid = world, uid

    def call(self, name, args, write=False):
        w = self.world
        if name == "get_user_info":
            return {"user_id": self.uid, "nick": "相同昵称"}
        if self.uid not in w.allowed:
            raise HandoffError("tencent_permission_denied", "not shared", not_executed=True)
        if name == "manage.create_file":
            w.file_created += 1
            return {"file_id": "shared-file", "url": "https://docs.qq.com/smartsheet/Dc2hhcmVkLWZpbGU"}
        if name == "manage.query_file_info":
            return {"file_id": "shared-file", "url": "https://docs.qq.com/smartsheet/Dc2hhcmVkLWZpbGU", "type": "smartsheet"}
        if name == "smartsheet.list_tables":
            return {"sheets": [{"sheet_id": sid, "title": sheet["title"]} for sid, sheet in w.sheets.items()]}
        if name == "smartsheet.add_table":
            sid = "sheet-" + str(len(w.sheets))
            w.sheets[sid] = {"title": args["properties"]["title"], "fields": [
                {"field_id": "default", "field_title": "智能表列", "field_type": "text", "property_text": {}}],
                "records": {}, "view": {"view_id": "default-view", "view_name": "默认视图", "view_type": "grid",
                "config": json.dumps({"fieldIds": ["default"], "fieldMetas": {}})}}
            return {"properties": {"sheet_id": sid, "title": args["properties"]["title"]}}
        sheet = w.sheets[args["sheet_id"]]
        if name == "smartsheet.list_fields":
            return {"fields": copy.deepcopy(sheet["fields"]), "total": len(sheet["fields"]), "has_more": False}
        if name == "smartsheet.add_fields":
            for f in args["fields"]:
                sheet["fields"].append({**copy.deepcopy(f), "field_id": "field-" + str(len(sheet["fields"]))})
            view = json.loads(sheet["view"]["config"])
            view["fieldIds"] = [f["field_id"] for f in sheet["fields"]]
            sheet["view"]["config"] = json.dumps(view)
            return {"fields": copy.deepcopy(sheet["fields"])}
        if name == "smartsheet.update_fields":
            for patch in args["fields"]:
                next(f for f in sheet["fields"] if f["field_id"] == patch["field_id"]).update(copy.deepcopy(patch))
            return {}
        if name == "smartsheet.list_views":
            return {"views": [copy.deepcopy(sheet["view"])], "total": 1, "has_more": False}
        if name == "smartsheet.update_view":
            patch = json.loads(args["patch_json"])
            view = json.loads(sheet["view"]["config"])
            view["fieldMetas"].update({f["field_id"]: {"hidden": f["hidden"]} for f in patch["fields"]})
            sheet["view"]["config"] = json.dumps(view)
            return {"view": copy.deepcopy(sheet["view"])}
        if name == "smartsheet.list_records":
            rows = list(sheet["records"].values())
            if args.get("record_ids"):
                rows = [r for r in rows if r["record_id"] in args["record_ids"]]
            offset = args.get("offset", 0)
            page = rows[offset:offset + args.get("limit", 100)]
            return {"records": copy.deepcopy(page), "total": len(rows), "has_more": offset + len(page) < len(rows), "next": offset + len(page)}
        if name == "smartsheet.add_records":
            w.writes += 1
            rid = "record-" + str(len(sheet["records"]))
            row = {"record_id": rid, **copy.deepcopy(args["records"][0])}
            sheet["records"][rid] = row
            if w.fail_record == sheet["title"]:
                w.fail_record = None
                raise HandoffError("write_unknown", "lost response after commit")
            return {"records": [copy.deepcopy(row)]}
        if name == "smartsheet.update_records":
            w.writes += 1
            patch = args["records"][0]
            row = sheet["records"][patch["record_id"]]
            entries = {v["field"]: v for v in row["field_values"]}
            entries.update({v["field"]: copy.deepcopy(v) for v in patch["field_values"]})
            row["field_values"] = list(entries.values())
            return {}
        raise AssertionError(name)


class TencentWorkflowTests(RepositoryCase):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.backend, self.frontend = Client(self.world, "backend"), Client(self.world, "frontend")
        self.bwho, self.fwho = identity(self.backend), identity(self.frontend)
        self.state.project["bindings"] = []
        self.binding = create(self.backend, self.state, self.bwho, "共享项目", "项目")["created"]
        self.bt, self.bl, self.bm = tables(self.backend, self.binding, self.bwho)
        join(self.bm, self.state, self.bwho, "后端")
        self.frontroot = Path(self.tmp.name) / "frontend"
        subprocess.run(["git", "clone", "-q", str(self.root), str(self.frontroot)], check=True, capture_output=True)
        self.fgit = Git(self.frontroot)
        self.fstate = State(self.fgit, Path(self.tmp.name) / "frontend-state")
        bind(self.frontend, self.fstate, self.fwho, self.binding["url"], "项目")
        self.ft, self.fl, self.fm = tables(self.frontend, self.binding, self.fwho)
        join(self.fm, self.fstate, self.fwho, "前端")
        self.bflow = Workflow(self.git, self.state, self.bwho, self.binding, self.bt, self.bl)
        self.fflow = Workflow(self.fgit, self.fstate, self.fwho, self.binding, self.ft, self.fl)

    def publish_one(self):
        self.stage_backend()
        return self.bflow.publish(self.draft())["published"][0]["id"]

    def test_two_accounts_publish_claim_develop_and_explicit_acceptance(self):
        rid = self.publish_one()
        self.assertEqual(self.fflow.queue()["counts"]["ready"], 0)
        self.assertEqual([r["id"] for r in self.fflow.claimable()], [rid])
        row = self.ft.get(rid)
        self.fflow.claim(rid, requirement(row))
        self.fflow.start(rid, ["screen.js"], requirement(row))
        (self.frontroot / "screen.js").write_text("const value = 2;\n")
        result = self.fflow.ready(rid, self.result())
        self.assertEqual(self.bt.get(rid)["状态"], "待验收")
        self.assertEqual(self.bt.get(rid)["跟进人"][0]["userId"], "frontend")
        self.assertEqual(self.bt.get(rid)["发布人"][0]["userId"], "backend")
        with self.assertRaises(HandoffError):
            self.bflow.accept(rid, result["result_version"])
        self.fflow.accept(rid, result["result_version"])
        row = self.bt.get(rid)
        self.assertEqual(row["状态"], "已完成")
        self.assertEqual(row["验收人"], [{"userId": "frontend", "corpId": CORP}])
        self.assertEqual(len(self.bl.records()), 4)
        self.assertEqual(self.fflow.queue()["counts"]["pending_acceptance"], 0)

    def test_publisher_can_claim_without_role_but_cannot_steal_claim(self):
        rid = self.publish_one()
        version = requirement(self.bt.get(rid))
        self.assertIn(rid, [r["id"] for r in self.bflow.claimable()])
        self.fflow.claim(rid, version)
        with self.assertRaises(HandoffError):
            self.bflow.claim(rid, version)
        self.assertEqual(self.bt.get(rid)["跟进人"][0]["userId"], "frontend")

    def test_publisher_can_assign_registered_member(self):
        rid = self.publish_one()
        self.bflow.assign(rid, self.fwho, requirement(self.bt.get(rid)))
        self.assertEqual(self.fflow.queue()["recommended"], rid)

    def test_same_division_or_no_division_can_publish_claim_and_process(self):
        join(self.bm, self.state, self.bwho, "测试")
        join(self.fm, self.fstate, self.fwho, "测试")
        rid = self.publish_one()
        self.fflow.claim(rid, requirement(self.ft.get(rid)))
        self.fflow.start(rid, ["screen.js"], requirement(self.ft.get(rid)))
        self.assertEqual(self.bt.get(rid)["状态"], "处理中")
        self.assertEqual(self.bt.get(rid)["发布人"][0]["userId"], "backend")

    def test_publisher_can_claim_own_ticket_without_a_local_role(self):
        self.state.data["identities"].clear()
        rid = self.publish_one()
        self.bflow.claim(rid, requirement(self.bt.get(rid)))
        self.bflow.start(rid, ["api.py"], requirement(self.bt.get(rid)))
        self.assertEqual(self.bt.get(rid)["跟进人"][0]["userId"], "backend")

    def test_lost_log_receipt_during_claim_recovers_without_second_claim(self):
        rid = self.publish_one()
        self.world.fail_record = "跟进记录"
        self.fflow.claim(rid, requirement(self.ft.get(rid)))
        self.assertEqual(len(self.fl.records()), 1)
        self.fflow.sync()
        self.assertEqual(len(self.fl.records()), 1)

    def test_partial_record_write_does_not_duplicate_published_ticket(self):
        self.world.fail_record = "工单"
        rid = self.publish_one()
        self.assertEqual(len(self.bt.records()), 1)
        self.assertEqual(self.bflow.publish(self.draft())["published"][0]["id"], rid)
        self.assertEqual(len(self.bt.records()), 1)

    def test_shared_access_denial_never_changes_sharing_policy(self):
        stranger = Client(self.world, "uninvited")
        with self.assertRaises(HandoffError) as cm:
            bind(stranger, self.fstate, identity(stranger), self.binding["url"], "项目")
        self.assertEqual(cm.exception.code, "tencent_permission_denied")

    def test_same_nickname_different_ids_keep_ownership_separate(self):
        rid = self.publish_one()
        self.bflow.assign(rid, self.fwho, requirement(self.bt.get(rid)))
        self.assertEqual(self.bflow.queue()["counts"]["ready"], 0)
        self.assertEqual(self.fflow.queue()["counts"]["ready"], 1)

    def test_code_change_invalidates_old_acceptance_version(self):
        rid = self.publish_one()
        self.fflow.claim(rid, requirement(self.ft.get(rid)))
        self.fflow.start(rid, ["screen.js"], requirement(self.ft.get(rid)))
        result = self.fflow.ready(rid, self.result())
        (self.frontroot / "screen.js").write_text("const changed = true;\n")
        with self.assertRaises(HandoffError) as cm:
            self.fflow.accept(rid, result["result_version"])
        self.assertEqual(cm.exception.code, "code_changed")


if __name__ == "__main__":
    unittest.main()
