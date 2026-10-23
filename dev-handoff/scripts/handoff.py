#!/usr/bin/env python3
"""Complete dev-handoff CLI backed exclusively by personal Tencent Docs accounts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from handoff_core import Git, HandoffError, State, exclusive_lock, read_json
from tencent_mcp import TencentMCP, load_token, prepare_directory
from tencent_tables import identity as live_identity, bind, create, tables, join
from workflow import Workflow


def parser():
    p = argparse.ArgumentParser(description="dev-handoff：腾讯文档个人账号开发对接")
    p.add_argument("--repo", default=".", help="当前开发仓库；所有 Git 操作均只读")
    p.add_argument("--state-dir", help="个人恢复目录，默认仓库的 .dev-handoff-local/state")
    p.add_argument("--table", help="当前项目绑定的表名、别名、Base ID 或完整 URL")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="检查依赖、账号和本人资料；不启动登录")
    sub.add_parser("identity", help="查看当前 Token 对应的个人账号")
    member = sub.add_parser("join", help="登记本人账号；分工可选，不限制任务领取")
    member.add_argument("--role", help="成员分工；未配置选项时可自由填写或留空")
    sub.add_parser("members", help="只读列出已登记的共享项目成员")
    sub.add_parser("access", help="只读核对文档分享策略；指定成员编辑权限由分享面板设置")
    claim = sub.add_parser("claim", help="领取一条未分配、依赖已完成的工单")
    claim.add_argument("record_id")
    claim.add_argument("--requirement", required=True)
    assign = sub.add_parser("assign", help="发布方将一条待对接工单分配给已登记成员")
    assign.add_argument("record_id")
    assign.add_argument("--user-id", required=True)
    assign.add_argument("--requirement", required=True)
    cap = sub.add_parser("capture", help="读取暂存区快照和差异，不写入任何暂存文件")
    cap.add_argument("--output", help="将 JSON 上下文写到明确指定的本地文件")
    cap.add_argument("--paths", nargs="+")
    idx = sub.add_parser("index-file", help="读取文件的暂存版本")
    idx.add_argument("path")
    sub.add_parser("tables", help="列出当前项目绑定的工单表")
    c = sub.add_parser("create", help="创建并绑定工单表")
    c.add_argument("name")
    c.add_argument("--alias")
    c.add_argument("--roles", nargs="+", help="可选的成员分工选项，如 前端 后端 测试；默认自由填写或留空")
    sub.add_parser("layout", help="精简默认表头；隐藏内部字段，保留全部数据")
    b = sub.add_parser("bind", help="检查并绑定已有 AI 表格")
    b.add_argument("url")
    b.add_argument("alias")
    b.add_argument("--repair", action="store_true", help="显式同意补齐缺失字段和必要数据表，保留既有数据")
    pub = sub.add_parser("publish", help="发布 AI 整理后的草稿 JSON")
    pub.add_argument("draft_file")
    pub.add_argument("--check", action="store_true", help="只校验草稿与共享范围重叠，不发布")
    queue = sub.add_parser("queue", help="精简队列；完整需求与证据使用 show/review")
    queue.add_argument("--app", action="append", help="按应用筛选，可重复；同时包含 apps 为 * 的共享事项")
    queue.add_argument("--limit", type=int, default=20, help="本次最多显示条数，默认 20，上限 100")
    queue.add_argument("--offset", type=int, default=0, help="跳过筛选后的条数")
    preflight = sub.add_parser("preflight", help="提前检查接手方文件；保存本地计划，不修改远端、不授权编辑")
    preflight.add_argument("record_id", nargs="?")
    preflight.add_argument("--paths", nargs="+")
    preflight.add_argument("--requirement", help="可选的 show 需求指纹；不符时停止")
    preflight.add_argument("--plan", help='批量 JSON：{"plans": [{"record_id": "…", "paths": ["…"]}]}')
    show = sub.add_parser("show", help="查看工单内容和需求指纹")
    show.add_argument("record_id")
    start = sub.add_parser("start", help="开始单条任务；成功后才允许编辑代码")
    start.add_argument("record_id")
    start.add_argument("--paths", nargs="+", required=True)
    start.add_argument("--requirement", required=True)
    extend = sub.add_parser("extend", help="编辑更多文件前声明并检查新增范围")
    extend.add_argument("record_id")
    extend.add_argument("--paths", nargs="+", required=True)
    ready = sub.add_parser("ready", help="验证结果并提交待验收，不自动处理下一条")
    ready.add_argument("record_id")
    ready.add_argument("result_file")
    ready.add_argument("--revalidate", action="store_true")
    ready.add_argument("--manual-accept", action="store_true", help="本轮仅提交待验收，不启用确认后的本机自动回写")
    auto = sub.add_parser("auto-accept", help="原工作区启用、恢复、查看或停止自动回写；省略 ID 时恢复已启用事项")
    auto.add_argument("record_id", nargs="?")
    mode = auto.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="只读查看本机任务及进程是否运行")
    mode.add_argument("--stop", action="store_true", help="停止指定工单的本机自动回写，不修改远端")
    for name in ("_auto-accept-worker", "_auto-accept-step"):
        internal = sub.add_parser(name, help=argparse.SUPPRESS)
        internal.add_argument("job_id")
    block = sub.add_parser("block", help="追加阻塞备注并停止当前任务")
    block.add_argument("record_id")
    block.add_argument("--reason", required=True)
    block.add_argument("--attempted", required=True)
    block.add_argument("--needed", required=True)
    block.add_argument("--kind", choices=("manual", "files", "dependency"), default="manual")
    block.add_argument("--waiting-for", nargs="+", help="阻塞关联的同表真实工单 ID")
    block.add_argument("--paths", nargs="+", help="文件阻塞涉及的计划文件；不授权编辑")
    review = sub.add_parser("review", help="只读核对需求、代码证据和结果版本")
    review.add_argument("record_id")
    confirm = sub.add_parser("confirm", help="指定验收人逐项确认同一结果版本；不代替原工作区验收回写")
    confirm.add_argument("record_id")
    confirm.add_argument("confirmation_file")
    confirm.add_argument("--confirmed-version", required=True)
    accept = sub.add_parser("accept", help="原工作区复核指定验收人的同版本完整确认并回写")
    accept.add_argument("record_id")
    accept.add_argument("--confirmed-version", required=True)
    reject = sub.add_parser("reject", help="操作者退回待验收工单")
    reject.add_argument("record_id")
    reject.add_argument("--reason", required=True)
    reject.add_argument("--confirmed-version", help="新版工单退回须绑定已审阅的版本")
    reject.add_argument("--criteria", nargs="+", help="退回涉及的验收标准 id；省略时关联本单所有标准")
    resume = sub.add_parser("resume", help="明确解除阻塞，恢复到待对接")
    resume.add_argument("record_id")
    resume.add_argument("--reason", required=True)
    sub.add_parser("sync", help="回读并恢复上次未完整确认的工单写入和日志")
    abandon = sub.add_parser("abandon-operation", help="仅在操作者明确要求时放弃本地恢复尝试，保留记录且不改远端")
    abandon.add_argument("operation_id")
    abandon.add_argument("--reason", required=True)
    return p


def _execute(args):
    git = Git(args.repo)
    state_dir = args.state_dir or (prepare_directory(git.root) / "state")
    state = State(git, state_dir)
    # Even read-only state loads take the local lock to avoid selecting stale checkpoints.
    with exclusive_lock(state.lock_path):
        state = State(git, state_dir)
        if args.command == "capture":
            context = git.staged_context(args.paths)
            if args.output:
                from handoff_core import write_json
                write_json(args.output, context)
                return {"snapshot": context["snapshot"], "output": str(Path(args.output).resolve())}
            return context
        if args.command == "index-file":
            return {"path": args.path, "content": git.index_file(args.path)}
        if args.command == "tables":
            return {"bindings": state.project["bindings"]}
        if args.command == "auto-accept" and args.status:
            from auto_accept import status
            binding = state.select(args.table)
            return {"auto_accept": status(state, binding["base_id"] + ":" + binding["table_id"], args.record_id)}
        client = TencentMCP(load_token(git.root))
        identity = live_identity(client)
        if args.command in ("doctor", "identity"):
            return {"identity": identity, "role": state.data["identities"].get(identity["profile"], {}).get("role"),
                    "git": {"root": str(git.root), "head": git.head()}, "live_identity_verified": True}
        if args.command == "create":
            return create(client, state, identity, args.name, args.alias or args.name, args.roles)
        if args.command == "bind":
            return {"bound": bind(client, state, identity, args.url, args.alias, args.repair)}
        binding = state.select(args.table)
        if binding.get("provider") != "tencent":
            raise HandoffError("legacy_binding", "此绑定尚未迁移到腾讯文档，请绑定腾讯智能表；旧表不会被删除。")
        if args.command == "access":
            access = client.call("manage.get_privilege", {"file_id": binding["base_id"]})
            return {"url": binding["url"], "policy": access.get("policy"),
                    "next": "在腾讯文档分享面板将指定同事添加为可编辑；请勿共享 Token。"}
        if args.command == "layout":
            from tencent_layout import clean_layout
            return clean_layout(client, binding)
        tickets, logs, members = tables(client, binding, identity)
        if args.command == "join":
            return join(members, state, identity, args.role)
        roster = [r for r in members.records() if r.get("账号ID")]
        if args.command == "members":
            return {"members": roster, "url": binding["url"]}
        current = [r for r in roster if r["账号ID"] == identity["user_id"]]
        if len(current) != 1:
            raise HandoffError("join_required", "请先用本人账号 join 登记为共享成员；分工可选。")
        flow = Workflow(git, state, identity, binding, tickets, logs, members)
        command = args.command
        if command == "_auto-accept-step":
            from auto_accept import step
            return step(flow, args.job_id)
        if command == "auto-accept":
            from auto_accept import cancel, enable
            if args.stop:
                if not args.record_id:
                    raise HandoffError("record_required", "停止自动回写需要工单 ID。")
                return cancel(flow, args.record_id)
            if args.record_id:
                enable(flow, args.record_id)
            return _schedule_auto_accept(flow, state_dir, {})
        if command == "queue":
            return flow.queue(args.app, args.limit, args.offset)
        if command == "show":
            return flow.show(args.record_id)
        if command == "preflight":
            if args.plan:
                if args.record_id or args.paths or args.requirement:
                    raise HandoffError("invalid_plan", "--plan 不与单条工单参数混用。")
                data = read_json(args.plan)
                return flow.preflight(data.get("plans") if isinstance(data, dict) else None)
            if not args.record_id or not args.paths:
                raise HandoffError("invalid_plan", "单条预检需要 record_id 和 --paths。")
            plan = {"record_id": args.record_id, "paths": args.paths}
            if args.requirement:
                plan["requirement_version"] = args.requirement
            return flow.preflight([plan])
        if command == "claim":
            return flow.claim(args.record_id, args.requirement)
        if command == "assign":
            target = [r for r in roster if r["账号ID"] == args.user_id]
            if len(target) != 1:
                raise HandoffError("choose_member", "请选择成员表中唯一的账号 ID。")
            return flow.assign(args.record_id, {"user_id": args.user_id}, args.requirement)
        if command == "publish":
            return flow.publish(read_json(args.draft_file), args.check)
        if command == "start":
            return flow.start(args.record_id, args.paths, args.requirement)
        if command == "extend":
            return flow.extend(args.record_id, args.paths)
        if command == "ready":
            result = flow.ready(args.record_id, read_json(args.result_file), args.revalidate,
                                auto_accept=not args.manual_accept)
            return _schedule_auto_accept(flow, state_dir, result)
        if command == "block":
            return flow.block(args.record_id, args.reason, args.attempted, args.needed,
                              args.kind, args.waiting_for, args.paths)
        if command == "review":
            return flow.review(args.record_id)
        if command == "confirm":
            return flow.confirm(args.record_id, args.confirmed_version, read_json(args.confirmation_file))
        if command == "accept":
            return flow.accept(args.record_id, args.confirmed_version)
        if command == "reject":
            return flow.reject(args.record_id, args.reason, args.confirmed_version, args.criteria)
        if command == "resume":
            return flow.resume(args.record_id, args.reason)
        if command == "sync":
            return _schedule_auto_accept(flow, state_dir, {"recovered": flow.sync()})
        if command == "abandon-operation":
            return flow.abandon_operation(args.operation_id, args.reason)
        raise HandoffError("invalid_command", "未支持的操作。")


def _schedule_auto_accept(flow, state_dir, result):
    from auto_accept import register, status
    active = register(flow)
    result["auto_accept"] = [{k: v for k, v in item.items() if k != "worker_running"}
                             for item in status(flow.state, flow.namespace)]
    result["_launch"] = {"repo": str(flow.git.root), "state_dir": str(Path(state_dir).resolve()), "jobs": active}
    return result


def execute(args):
    if args.command == "_auto-accept-worker":
        from auto_accept import worker
        step_args = argparse.Namespace(**{**vars(args), "command": "_auto-accept-step"})
        return worker(step_args, _execute)
    result = _execute(args)
    launch = result.pop("_launch", None)
    if launch:
        from auto_accept import launch as launch_worker
        # Release the workspace operation lock before starting/waiting for workers.
        result["auto_accept_workers"] = [launch_worker(launch["repo"], launch["state_dir"], job) for job in launch["jobs"]]
    return result


def main():
    try:
        result = execute(parser().parse_args())
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False, indent=2))
        return 0
    except HandoffError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message,
                                                 **exc.details}}, ensure_ascii=False, indent=2))
        return 2
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Fail closed on malformed files/API shapes without dumping source/credentials.
        print(json.dumps({"ok": False, "error": {"code": "invalid_input_or_response",
                          "message": "输入文件或远端结构不符合契约，请检查对应 JSON 和命令 Schema。",
                          "exception": type(exc).__name__}}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
