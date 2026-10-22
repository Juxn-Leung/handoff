#!/usr/bin/env python3
"""Tencent Docs personal-account MCP client. Python standard library only."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import time
import urllib.error
import urllib.request

from handoff_core import HandoffError, read_json, write_json

ENDPOINT = "https://docs.qq.com/openapi/mcp"
TOKEN_PAGE = "https://docs.qq.com/open/auth/mcp.html"
LOCAL_DIR = ".dev-handoff-local"


def local_directory(repo):
    root = Path(repo).resolve()
    directory = root / LOCAL_DIR
    if directory.is_symlink():
        raise HandoffError("unsafe_config_path", "本地配置目录不能是符号链接。")
    return directory


def prepare_directory(repo):
    directory = local_directory(repo)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    ignore = directory / ".gitignore"
    if ignore.is_symlink():
        raise HandoffError("unsafe_config_path", "私有目录中的忽略文件不能是符号链接。")
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    elif "*" not in ignore.read_text(encoding="utf-8").splitlines():
        with ignore.open("a", encoding="utf-8") as stream:
            stream.write("\n*\n")
    return directory


def configure(repo):
    """The human enters a secret on their own terminal, never in argv or chat."""
    if not sys.stdin.isatty():
        raise HandoffError("terminal_required", "请在自己的交互终端运行 configure，粘贴 Token 后按回车。")
    print(f"在 {TOKEN_PAGE} 登录个人微信或 QQ 后复制 Token。", file=sys.stderr)
    token = getpass.getpass("粘贴个人 Token（输入不显示）：").strip()
    if not token or any(c in token for c in "\r\n\0"):
        raise HandoffError("invalid_token", "Token 不能为空或包含换行。")
    return save_token(repo, token)


def save_token(repo, token):
    if not isinstance(token, str) or not token.strip() or any(c in token for c in "\r\n\0"):
        raise HandoffError("invalid_token", "腾讯文档没有返回有效 Token。")
    directory = prepare_directory(repo)
    path = directory / "tencent-token.json"
    if path.is_symlink():
        raise HandoffError("unsafe_config_path", "Token 文件不能是符号链接。")
    # write_json creates a mode-0600 temporary file and atomically replaces the target.
    write_json(path, {"token": token})
    path.chmod(0o600)
    return {"configured": True, "token_file": str(path), "token_displayed": False}


def auth_start(repo):
    """Match the device authorization flow in Tencent's official skill package."""
    directory = prepare_directory(repo)
    path = directory / "tencent-auth.json"
    if path.is_symlink():
        raise HandoffError("unsafe_config_path", "授权检查点不能是符号链接。")
    pending = read_json(path, {})
    if not pending or pending.get("expires_at", 0) <= time.time() or pending.get("complete"):
        pending = {"code": secrets.token_hex(8), "expires_at": time.time() + 300}
        write_json(path, pending)
    from urllib.parse import urlencode
    url = "https://docs.qq.com/scenario/open-claw.html?" + urlencode({
        "nlc": 1, "authType": 1, "code": pending["code"], "mcp_source": "desktop"})
    return {"authorization_url": url, "expires_in_seconds": max(0, int(pending["expires_at"] - time.time())),
            "next": "本人使用 QQ 或微信在浏览器完成授权后，回复已完成授权；再执行 auth-finish --confirmed。"}


def auth_finish(repo, opener=None):
    path = local_directory(repo) / "tencent-auth.json"
    pending = read_json(path, {})
    if not pending.get("code") or pending.get("complete"):
        raise HandoffError("tencent_auth_missing", "没有待完成授权，请先 auth-start。")
    if pending.get("expires_at", 0) <= time.time():
        raise HandoffError("tencent_auth_expired", "授权链接已过期，请重新 auth-start。")
    from urllib.parse import urlencode
    url = "https://docs.qq.com/oauth/v2/mcp/token/get?" + urlencode({"code": pending["code"]})
    try:
        with (opener or urllib.request.build_opener(NoRedirect())).open(url, timeout=25) as response:
            value = json.load(response)
    except (urllib.error.URLError, OSError, ValueError):
        raise HandoffError("tencent_auth_network", "获取授权结果失败，请检查网络后重试；未输出原始响应。") from None
    if not isinstance(value, dict):
        raise HandoffError("tencent_auth_shape", "授权响应格式不符。")
    data = value.get("data") or {}
    if value.get("ret", 0) != 0 or not data.get("token"):
        code = value.get("ret")
        if code == 11510:
            raise HandoffError("tencent_not_authorized", "尚未完成浏览器授权，请完成后再重试。")
        if code == 400007:
            raise HandoffError("tencent_membership_required", "腾讯文档当前账号返回会员权限不足；请由本人决定是否开通。")
        raise HandoffError("tencent_auth_failed", "未获取到 Token，请核对授权结果或使用 configure 手动配置。", remote_code=code)
    result = save_token(repo, data["token"])
    write_json(path, {"complete": True, "completed_at": time.time()})
    return result


def load_token(repo):
    token = os.environ.get("TENCENT_DOCS_TOKEN", "").strip()
    if not token:
        path = local_directory(repo) / "tencent-token.json"
        if path.is_symlink():
            raise HandoffError("unsafe_config_path", "Token 文件不能是符号链接。")
        if path.exists() and os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise HandoffError("unsafe_token_permissions", "Token 文件应仅本人可读写，请设置 chmod 600。")
        config = read_json(path, {})
        token = config.get("token", "") if isinstance(config, dict) else ""
    if not isinstance(token, str) or not token.strip() or any(c in token for c in "\r\n\0"):
        raise HandoffError("tencent_token_missing", "请先在本机运行 tencent_mcp.py configure。", token_page=TOKEN_PAGE)
    return token.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def decode_response(response, request_id):
    """Support JSON and SSE, returning as soon as the matching RPC response arrives."""
    if "text/event-stream" not in response.headers.get("Content-Type", ""):
        body = response.read()
        if not body:
            return None
        value = json.loads(body)
        if not isinstance(value, dict) or value.get("id") != request_id:
            raise HandoffError("mcp_response_shape", "MCP 响应 ID 与请求不一致。")
        return value
    data = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line and data:
            value = json.loads("\n".join(data))
            data = []
            if isinstance(value, dict) and value.get("id") == request_id:
                return value
    if data:
        value = json.loads("\n".join(data))
        if isinstance(value, dict) and value.get("id") == request_id:
            return value
    raise HandoffError("mcp_response_shape", "MCP 流结束前未返回对应请求的结果。")


class TencentMCP:
    def __init__(self, token, opener=None):
        self.token = token
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.sequence = 0
        self.session = None
        self.protocol = "2025-03-26"
        self.initialized = False
        self.tools = None

    def redact(self, value):
        if isinstance(value, str):
            return value.replace(self.token, "[REDACTED]")
        if isinstance(value, dict):
            return {k: self.redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        return value

    def rpc(self, method, params=None, *, notification=False, write=False):
        self.sequence += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            body["id"] = self.sequence
        headers = {"Authorization": self.token, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.initialized:
            headers["MCP-Protocol-Version"] = self.protocol
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(ENDPOINT, json.dumps(body).encode(), headers, method="POST")
        try:
            with self.opener.open(request, timeout=25) as response:
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                if notification:
                    return None
                value = decode_response(response, body["id"])
        except urllib.error.HTTPError as exc:
            code = {401: "tencent_unauthenticated", 403: "tencent_permission_denied",
                    429: "tencent_rate_limited"}.get(exc.code, "write_unknown" if write else "tencent_http_error")
            raise HandoffError(code, "腾讯文档请求未获成功响应，请核对 Token、权限及额度。",
                               http_status=exc.code, not_executed=exc.code in (401, 403, 429)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise HandoffError("write_unknown" if write else "tencent_network_error",
                               "腾讯文档连接失败；写入结果未知时先回读，禁止直接重放。") from None
        except (ValueError, UnicodeError):
            raise HandoffError("write_unknown" if write else "mcp_response_shape", "腾讯文档响应不能解析。") from None
        if not isinstance(value, dict):
            raise HandoffError("write_unknown" if write else "mcp_response_shape", "腾讯文档未返回明确结果。")
        if value.get("error"):
            error = value["error"]
            code = error.get("code") if isinstance(error, dict) else None
            raise HandoffError("mcp_rpc_error", "腾讯文档 MCP 返回错误；未输出可能含凭据的原始内容。", rpc_code=code)
        return self.redact(value.get("result"))

    def initialize(self):
        if self.initialized:
            return
        result = self.rpc("initialize", {"protocolVersion": self.protocol, "capabilities": {},
                                          "clientInfo": {"name": "dev-handoff", "version": "0.1.0"}})
        if not isinstance(result, dict) or not result.get("protocolVersion"):
            raise HandoffError("mcp_response_shape", "MCP 初始化未返回协议版本。")
        self.protocol = result["protocolVersion"]
        self.initialized = True
        self.rpc("notifications/initialized", notification=True)

    def list_tools(self):
        self.initialize()
        found, seen, cursor = {}, set(), None
        for _ in range(100):
            result = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise HandoffError("mcp_response_shape", "工具列表不完整，不能视为没有能力。")
            for tool in result["tools"]:
                name = tool.get("name") if isinstance(tool, dict) else None
                if not name or name in found or not isinstance(tool.get("inputSchema"), dict):
                    raise HandoffError("mcp_response_shape", "工具名重复或缺少参数结构。")
                found[name] = tool
            cursor = result.get("nextCursor")
            if not cursor:
                self.tools = found
                return found
            if cursor in seen:
                raise HandoffError("mcp_pagination", "工具列表分页游标循环，停止读取。")
            seen.add(cursor)
        raise HandoffError("mcp_pagination", "工具列表超过分页上限，未报告空结果。")

    def call(self, name, arguments, *, write=False):
        tools = self.tools if self.tools is not None else self.list_tools()
        if name not in tools:
            raise HandoffError("tencent_tool_missing", "当前账号未提供所需工具。", tool=name)
        schema = tools[name]["inputSchema"]
        missing = [key for key in schema.get("required", []) if key not in arguments]
        if missing:
            raise HandoffError("tencent_tool_arguments", "所需工具参数缺失，请核对实时 Schema。", tool=name, missing=missing)
        result = self.rpc("tools/call", {"name": name, "arguments": arguments}, write=write)
        if not isinstance(result, dict) or result.get("isError"):
            raise HandoffError("write_unknown" if write else "tencent_tool_error", "腾讯文档工具返回失败，需检查权限或回读写入结果。", tool=name)
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            data = structured
        else:
            texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
            if len(texts) != 1:
                raise HandoffError("write_unknown" if write else "mcp_response_shape", "工具未返回唯一结构化结果。", tool=name)
            try:
                data = json.loads(texts[0])
            except ValueError:
                raise HandoffError("write_unknown" if write else "mcp_response_shape", "工具结果不是 JSON 对象。", tool=name) from None
        if not isinstance(data, dict):
            raise HandoffError("mcp_response_shape", "工具结果不是对象。", tool=name)
        if data.get("error") or data.get("ret", 0) != 0 or data.get("code", 0) != 0:
            raise HandoffError("write_unknown" if write else "tencent_tool_error", "腾讯文档业务接口未确认成功，未输出原始错误。", tool=name)
        return data


def main():
    parser = argparse.ArgumentParser(description="腾讯文档个人账号 Token 配置和 MCP 连通性检查")
    parser.add_argument("--repo", default=".")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("configure", help="本人在交互终端安全输入 Token")
    subs.add_parser("auth-start", help="生成官方个人账号授权链接，不读取登录凭据")
    finish = subs.add_parser("auth-finish", help="本人完成浏览器授权后获取 Token 并保存")
    finish.add_argument("--confirmed", action="store_true", required=True, help="本人已明确确认完成授权")
    subs.add_parser("doctor", help="只读验证鉴权、列出工单相关工具")
    subs.add_parser("schema", help="输出当前账号真实提供的工单工具参数")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            data = configure(args.repo)
        elif args.command == "auth-start":
            data = auth_start(args.repo)
        elif args.command == "auth-finish":
            data = auth_finish(args.repo)
        else:
            client = TencentMCP(load_token(args.repo))
            tools = client.list_tools()
            relevant = {n: t for n, t in tools.items() if n.startswith(("smartsheet.", "manage.")) or n in (
                "create_space_node", "query_space_node", "search_space_file", "get_content", "get_user_info")}
            directory = local_directory(args.repo)
            write_json(directory / "tencent-tools.json", relevant)
            data = {"authenticated": True, "tool_count": len(tools), "workflow_tools": sorted(relevant),
                    "schema_file": str(directory / "tencent-tools.json")}
            if args.command == "schema":
                data["schemas"] = relevant
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, indent=2))
        return 0
    except HandoffError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message, **exc.details}}, ensure_ascii=False))
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"ok": False, "error": {"code": "invalid_input_or_response", "message": "本地配置或腾讯文档响应不符合预期。"}}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
