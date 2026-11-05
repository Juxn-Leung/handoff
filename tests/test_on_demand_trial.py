import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev-handoff/scripts"))
import on_demand_trial as trial
from handoff_core import HandoffError


class OnDemandTrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root / "run"
        self.who = {"user_id": "real-fixture-account", "name": "测试成员"}

    def initialize(self):
        with patch.object(trial, "load_token", return_value="private-fixture-token"), \
             patch.object(trial, "identity", return_value=self.who):
            return trial.initialize(self.root, self.directory)

    def test_initialization_is_isolated_and_does_not_overwrite_later_work(self):
        first = self.initialize()
        artifact = self.directory / "workspace/order-labels.json"
        artifact.write_text('{"work":"in progress"}')
        again = self.initialize()
        self.assertEqual(first["runtime_fingerprint"], again["runtime_fingerprint"])
        self.assertEqual(artifact.read_text(), '{"work":"in progress"}')
        self.assertFalse((self.root / ".git").exists())
        self.assertNotIn("private-fixture-token", (self.directory / "trial.json").read_text())

    def test_nontrial_directory_and_wrong_runtime_are_rejected(self):
        self.directory.mkdir()
        (self.directory / "business.txt").write_text("preserve")
        with patch.object(trial, "load_token", return_value="private-fixture-token"), \
             patch.object(trial, "identity", return_value=self.who):
            with self.assertRaisesRegex(HandoffError, "不属于本联调"):
                trial.initialize(self.root, self.directory)
            with self.assertRaisesRegex(HandoffError, "版本不符"):
                trial.initialize(self.root, self.directory, "wrong-version")
        self.assertEqual((self.directory / "business.txt").read_text(), "preserve")

    def test_background_commands_are_rejected_before_credentials_or_execution(self):
        with patch.object(trial, "load_token", side_effect=AssertionError("must not read credentials")):
            for arguments in (["auto-accept"], ["_auto-accept-worker", "job"], ["ready", "id", "result.json", "--auto-accept"]):
                with self.assertRaisesRegex(HandoffError, "不允许后台"):
                    trial.invoke(self.root, self.directory, arguments)

    def test_command_uses_only_own_authorization_in_child_memory(self):
        self.initialize()
        def child(args, **kwargs):
            self.assertEqual(kwargs["env"]["TENCENT_DOCS_TOKEN"], "private-fixture-token")
            self.assertNotIn("private-fixture-token", " ".join(args))
            self.assertEqual(args[-2:], ["inbox", "ticket"])
            return subprocess.CompletedProcess(args, 0, json.dumps({"ok": True, "data": {"items": []}}), "")
        with patch.object(trial, "load_token", return_value="private-fixture-token"), \
             patch.object(trial, "identity", return_value=self.who), patch.object(trial.subprocess, "run", side_effect=child) as run:
            result = trial.invoke(self.root, self.directory, ["inbox", "ticket"])
        run.assert_called_once()
        self.assertFalse(result["background_started"])
        records = (self.directory / "calls.jsonl").read_text()
        self.assertNotIn("private-fixture-token", records)
        self.assertEqual([json.loads(line)["phase"] for line in records.splitlines()], ["started", "finished"])

    def test_switched_account_cannot_continue_original_workspace(self):
        self.initialize()
        with patch.object(trial, "load_token", return_value="other-fixture-token"), \
             patch.object(trial, "identity", return_value={"user_id": "other"}), \
             patch.object(trial.subprocess, "run") as run:
            with self.assertRaisesRegex(HandoffError, "原来的真实账号"):
                trial.invoke(self.root, self.directory, ["inbox"])
        run.assert_not_called()

    def test_verifier_checks_received_bytes_and_all_three_outputs(self):
        artifact = self.root / "received.json"
        value = {"separator": "、", "empty_text": "暂无订单", "error_text": "加载失败，请重试"}
        artifact.write_text(json.dumps(value, ensure_ascii=False) + "\n")
        checked = trial.verify(artifact, "、")
        self.assertEqual(checked["content"], artifact.read_text())
        self.assertEqual(checked["observed"]["list"], "1、2")
        with self.assertRaisesRegex(HandoffError, "约定不符"):
            trial.verify(artifact, ",")
        artifact.write_text(json.dumps({**value, "extra": "unexpected"}))
        with self.assertRaises(HandoffError):
            trial.verify(artifact, "、")


if __name__ == "__main__":
    unittest.main()
