"""Behavior tests for the Tencent personal-account transport. Live verification is recorded separately."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev-handoff/scripts"))
from handoff_core import HandoffError
from tencent_mcp import TencentMCP, auth_start, decode_response, load_token, save_token


class Response(io.BytesIO):
    def __init__(self, data, content_type="application/json"):
        super().__init__(data)
        self.headers = {"Content-Type": content_type}


class TransportTests(unittest.TestCase):
    def test_sse_skips_notifications_and_unrelated_ids(self):
        data = b': ping\n\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\ndata: {"id":1,"result":{}}\n\ndata: {"id":2,"result":{"tools":[]}}\n\n'
        result = decode_response(Response(data, "text/event-stream"), 2)
        self.assertEqual(result["result"], {"tools": []})

    def test_json_rejects_mismatched_response_id(self):
        with self.assertRaises(HandoffError):
            decode_response(Response(b'{"id":99,"result":{}}'), 2)

    def test_token_only_goes_in_authorization_header_and_result_is_redacted(self):
        class Opener:
            def open(inner, request, timeout):
                self.assertEqual(request.full_url, "https://docs.qq.com/openapi/mcp")
                self.assertEqual(request.get_header("Authorization"), "private-token-value")
                body = json.loads(request.data)
                self.assertNotIn("private-token-value", request.data.decode())
                return Response(json.dumps({"id": body["id"], "result": {"echo": "private-token-value"}}).encode())
        client = TencentMCP("private-token-value", opener=Opener())
        self.assertEqual(client.rpc("test"), {"echo": "[REDACTED]"})

    def test_tool_pagination_and_no_automatic_replay_on_write_error(self):
        client = TencentMCP("test-only")
        client.initialized = True
        calls = []
        def rpc(method, params, **kwargs):
            calls.append((method, params))
            if method == "tools/list":
                if params:
                    return {"tools": [{"name": "second", "inputSchema": {}}]}
                return {"tools": [{"name": "first", "inputSchema": {}}], "nextCursor": "next"}
            raise HandoffError("write_unknown", "timeout after commit")
        client.rpc = rpc
        self.assertEqual(set(client.list_tools()), {"first", "second"})
        with self.assertRaises(HandoffError):
            client.call("first", {}, write=True)
        self.assertEqual(sum(c[0] == "tools/call" for c in calls), 1)

    def test_token_configuration_is_private_and_auth_start_reuses_pending_link(self):
        with tempfile.TemporaryDirectory() as directory:
            save_token(directory, "test-secret")
            path = Path(directory) / ".dev-handoff-local/tencent-token.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(load_token(directory), "test-secret")
            self.assertEqual(auth_start(directory)["authorization_url"], auth_start(directory)["authorization_url"])
            with self.assertRaises(HandoffError):
                save_token(directory, "bad\nheader")


if __name__ == "__main__":
    unittest.main()
