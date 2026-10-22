import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev-handoff/scripts"))
from handoff_core import HandoffError
from tencent_tables import create, bind, identity, tables, join
from test_handoff import RepositoryCase
from test_tencent_workflow import Client, World
from tencent_layout import clean_layout, LAYOUTS


class SetupTests(RepositoryCase):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.client = Client(self.world, "frontend")
        self.who = identity(self.client)
        self.state.project["bindings"] = []

    def test_default_members_have_optional_free_form_division(self):
        binding = create(self.client, self.state, self.who, "项目", "项目")["created"]
        _, _, members = tables(self.client, binding, self.who)
        join(members, self.state, self.who)
        self.assertEqual(members.records()[0]["分工"], "")
        join(members, self.state, self.who, "设计与测试")
        self.assertEqual(members.records()[0]["分工"], "设计与测试")

    def test_custom_divisions_are_shared_across_accounts(self):
        binding = create(self.client, self.state, self.who, "项目", "项目", ["设计", "测试", "开发"])["created"]
        _, _, members = tables(self.client, binding, self.who)
        with self.assertRaises(HandoffError) as error:
            join(members, self.state, self.who)
        self.assertEqual(error.exception.code, "choose_role")
        join(members, self.state, self.who, "测试")
        other = Client(self.world, "backend")
        bind(other, self.state, identity(other), binding["url"], "项目")
        _, _, other_members = tables(other, binding, identity(other))
        self.assertEqual(other_members.expected["分工"]["config"]["options"],
                         [{"name": x} for x in ("设计", "测试", "开发")])
        with self.assertRaises(HandoffError):
            join(other_members, self.state, identity(other), "后端")
        join(other_members, self.state, identity(other), "测试")
        self.assertEqual(len(other_members.records()), 2)

    def test_invalid_division_options_do_not_create_remote_file(self):
        with self.assertRaises(HandoffError):
            create(self.client, self.state, self.who, "项目", "项目", ["测试", "测试"])
        self.assertEqual(self.world.file_created, 0)

    def test_clean_headers_reuse_default_column_and_keep_internal_fields(self):
        result = create(self.client, self.state, self.who, "项目", "项目")
        binding = result["created"]
        self.assertNotIn("pending", result["layout"])
        for _, (key, visible) in LAYOUTS.items():
            sheet = self.world.sheets[binding[key]]
            self.assertNotIn("智能表列", [f["field_title"] for f in sheet["fields"]])
            view = json.loads(sheet["view"]["config"])
            self.assertEqual({f["field_title"] for f in sheet["fields"]
                if not view["fieldMetas"][f["field_id"]]["hidden"]}, set(visible))
        fields = [f["field_title"] for f in self.world.sheets[binding["table_id"]]["fields"]]
        self.assertIn("_结果证据", fields)
        self.assertIn("发布人ID", fields)
        self.assertNotIn("前端", fields)

    def test_layout_failure_reports_pending_without_recreating_document(self):
        previous = self.client.call
        def call(name, args, **kwargs):
            if name == "smartsheet.update_view":
                raise HandoffError("mcp_rpc_error", "remote rejects layout")
            return previous(name, args, **kwargs)
        self.client.call = call
        result = create(self.client, self.state, self.who, "项目", "项目")
        self.assertTrue(result["layout"]["pending"])
        self.assertEqual(self.world.file_created, 1)
        self.assertEqual(len(self.state.project["bindings"]), 1)
        self.client.call = previous
        clean_layout(self.client, result["created"])
        self.assertEqual(self.world.file_created, 1)

    def test_layout_noop_cannot_be_reported_as_success(self):
        binding = create(self.client, self.state, self.who, "项目", "项目")["created"]
        sheet = self.world.sheets[binding["table_id"]]
        config = json.loads(sheet["view"]["config"])
        config["fieldMetas"] = {}
        sheet["view"]["config"] = json.dumps(config)
        previous = self.client.call
        self.client.call = lambda name, args, **kw: {} if name == "smartsheet.update_view" else previous(name, args, **kw)
        with self.assertRaises(HandoffError) as cm:
            clean_layout(self.client, binding)
        self.assertEqual(cm.exception.code, "layout_unverified")

    def test_create_and_rebind_preserve_schema_and_existing_rows(self):
        binding = create(self.client, self.state, self.who, "项目", "项目")["created"]
        before = {sid: len(s["fields"]) for sid, s in self.world.sheets.items()}
        bind(self.client, self.state, self.who, binding["url"], "项目", repair=True)
        self.assertEqual(before, {sid: len(s["fields"]) for sid, s in self.world.sheets.items()})
        self.assertEqual(self.world.file_created, 1)
        self.assertEqual(len(self.world.sheets), 3)

    def test_bind_missing_tables_requires_explicit_repair(self):
        with self.assertRaises(HandoffError):
            bind(self.client, self.state, self.who, "shared-file", "项目")
        self.assertEqual(self.world.sheets, {})

    def test_unknown_create_is_never_replayed(self):
        previous = self.client.call
        def call(name, args, **kwargs):
            result = previous(name, args, **kwargs)
            if name == "manage.create_file":
                raise HandoffError("write_unknown", "lost receipt")
            return result
        self.client.call = call
        for _ in range(2):
            with self.assertRaises(HandoffError):
                create(self.client, self.state, self.who, "项目", "项目")
        self.assertEqual(self.world.file_created, 1)

    def test_wrong_existing_field_type_is_not_deleted_or_replaced(self):
        binding = create(self.client, self.state, self.who, "项目", "项目")["created"]
        field = self.world.sheets[binding["table_id"]]["fields"][0]
        field["field_type"] = "number"
        with self.assertRaises(HandoffError):
            bind(self.client, self.state, self.who, "shared-file", "项目", repair=True)
        self.assertEqual(field["field_type"], "number")

    def test_known_authorization_rejection_does_not_block_later_create(self):
        previous = self.client.call
        def call(name, args, **kwargs):
            if name == "manage.create_file":
                raise HandoffError("tencent_permission_denied", "not executed", not_executed=True)
            return previous(name, args, **kwargs)
        self.client.call = call
        with self.assertRaises(HandoffError):
            create(self.client, self.state, self.who, "项目", "项目")
        self.client.call = previous
        result = create(self.client, self.state, self.who, "项目", "项目")
        self.assertEqual(result["created"]["provider"], "tencent")
        self.assertEqual(self.world.file_created, 1)

    def test_capture_without_token_never_reads_unstaged_content_or_loads_dingtalk(self):
        self.stage_backend()
        (self.root / "api.py").write_text("unstaged = 'private'\n")
        before = self.git_cmd("status", "--porcelain=v1")
        script = Path(__file__).resolve().parents[1] / "dev-handoff/scripts/handoff.py"
        result = subprocess.run([sys.executable, str(script), "--repo", str(self.root), "capture"], capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        self.assertIn("value = 2", data["data"]["diff"])
        self.assertNotIn("private", data["data"]["diff"])
        self.assertEqual(before, self.git_cmd("status", "--porcelain=v1"))

    def test_doctor_missing_token_returns_personal_auth_hint(self):
        script = Path(__file__).resolve().parents[1] / "dev-handoff/scripts/handoff.py"
        result = subprocess.run([sys.executable, str(script), "--repo", str(self.root), "doctor"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "tencent_token_missing")


if __name__ == "__main__":
    unittest.main()
