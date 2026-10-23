#!/usr/bin/env python3
"""Opt-in two-account test: peer confirms; the normal background worker finalizes.

Each participant uses their own authorization. The isolated processor inherits its
owner's token only in memory; no credential is copied into the fixture or snapshot.
The audit reads shared data and never calls accept, sync or a worker step.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import auto_accept
from acceptance_dual import runtime_fingerprint, result, decision, check_plan, approval_observation
from acceptance_smoke import SOURCE, CHECK, workspace
from handoff_core import HandoffError, State, digest, exclusive_lock, now, read_json, requirement, write_json
from tencent_mcp import TencentMCP, load_token, prepare_directory
from tencent_tables import identity, bind, tables
from workflow import Workflow

FIXTURE = "live_dual_auto_acceptance_v1"
ALIAS = "真实双账号自动回写联调"


@contextmanager
def locked(path):
    # A just-started worker may hold the same workspace lock during its first read.
    lock = exclusive_lock(path)
    for attempt in range(61):
        try:
            lock.__enter__()
            break
        except HandoffError as exc:
            if exc.code != "local_busy" or attempt == 60:
                raise
            time.sleep(0.5)
            lock = exclusive_lock(path)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


@contextmanager
def context(repo, directory, table_url, actor=None):
    git = workspace(directory / "workspace")
    state_dir = directory / "state"
    with locked(State(git, state_dir).lock_path):
        client = TencentMCP(load_token(repo))
        who = identity(client)
        if actor and who["user_id"] != actor:
            raise HandoffError("identity_mismatch", "本阶段须由指定真实账号在本人电脑执行。")
        state = State(git, state_dir)
        binding = state.select(ALIAS) if state.project["bindings"] else bind(client, state, who, table_url, ALIAS)
        if binding["url"] != table_url:
            raise HandoffError("table_mismatch", "隔离目录已绑定另一张表。")
        flow = Workflow(git, state, who, binding, *tables(client, binding, who))
        flow.registered_member(who["user_id"])
        yield flow


def cli(repo, run_dir, table_url, arguments):
    # subprocess env is private process memory, never shell text or stored JSON.
    env = os.environ.copy()
    env["TENCENT_DOCS_TOKEN"] = load_token(repo)
    command = [sys.executable, str(run_dir / "runtime/dev-handoff/scripts/handoff.py"),
               "--repo", str(run_dir / "processor/workspace"),
               "--state-dir", str(run_dir / "processor/state"), "--table", table_url, *arguments]
    completed = subprocess.run(command, env=env, capture_output=True, text=True)
    try:
        response = json.loads(completed.stdout)
    except ValueError:
        raise HandoffError("auto_test_cli_failed", "隔离 CLI 未返回有效回执；保留原目录排查。") from None
    if completed.returncode or not response.get("ok"):
        error = response.get("error", {})
        raise HandoffError(error.get("code", "auto_test_cli_failed"), error.get("message", "隔离 CLI 失败。"))
    return response["data"]


def prepare(repo, run_dir, table_url, reviewer_id):
    if run_dir.exists() and any(run_dir.iterdir()) and not (run_dir / "run.json").is_file():
        raise HandoffError("unsafe_smoke_directory", "目录非空且没有本联调检查点。")
    run_dir.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(run_dir / "run.lock"):
        return _prepare(repo, run_dir, table_url, reviewer_id)


def _prepare(repo, run_dir, table_url, reviewer_id):
    skill = Path(__file__).resolve().parents[1]
    runtime = runtime_fingerprint(skill)
    expected = {"fixture": FIXTURE, "run_id": run_dir.name, "table_url": table_url,
                "reviewer_id": reviewer_id, "runtime_fingerprint": runtime}
    checkpoint = read_json(run_dir / "run.json", expected)
    if any(checkpoint.get(key) != value for key, value in expected.items()):
        raise HandoffError("runtime_changed", "已有联调不能更换表、验收人或运行代码；使用原冻结版本继续。")
    write_json(run_dir / "run.json", checkpoint)
    frozen = run_dir / "runtime/dev-handoff"
    if not frozen.exists():
        shutil.copytree(skill, frozen, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if runtime_fingerprint(frozen) != runtime:
        raise HandoffError("runtime_changed", "本轮冻结运行代码不完整或发生变化。")
    with context(repo, run_dir / "processor", table_url, checkpoint.get("processor_id")) as flow:
        processor_id = flow.identity["user_id"]
        if processor_id == reviewer_id:
            raise HandoffError("distinct_accounts_required", "真实双账号联调必须使用两个不同账号。")
        flow.registered_member(reviewer_id)
        checkpoint["processor_id"] = processor_id
        write_json(run_dir / "run.json", checkpoint)
        marker = {key: checkpoint[key] for key in ("run_id", "runtime_fingerprint", "processor_id", "reviewer_id")}
        if "result_version" not in checkpoint:
            flow.sync()  # Only preparation recovery; never used by the final audit.
            draft_path = run_dir / "draft.json"
            if not draft_path.exists():
                delivery = {"apps": ["real-dual-auto-acceptance-test"], "scope": "隔离测试夹具，非业务验收",
                            "deliverable": "另一真实账号逐项确认后，由原工作区后台自动回写",
                            "acceptance_owner": "指定账号实际运行夹具检查；处理方后台复核完成", "reviewer_id": reviewer_id,
                            "verification_mode": FIXTURE, "verification_run": marker,
                            "checks": [{"id": "render-check", "kind": "local", "description": "实际检查列表、空态与失败态",
                                        "owner": "处理方与验收方分别在本人电脑执行", "required": True}],
                            "external_checks": [], "shared_contracts": [],
                            "criteria": [{"id": "render-states", "description": "三种输入均返回预期结果",
                                          "steps": [CHECK], "expected": "list=1,2; empty=empty; failure=error", "check_ids": ["render-check"]}]}
                write_json(draft_path, {"snapshot": flow.git.staged()["fingerprint"], "tickets": [{
                    "key": "auto-acceptance-" + run_dir.name, "title": "[真实双账号自动回写联调，非业务验收] 确认后自动完成",
                    "description": "本单仅验证真实账号、跨工作区确认与后台自动回写。", "change_types": ["feat"], "paths": ["service.py"],
                    "handoff": "验收方执行 acceptance_auto.py reviewer，实际检查并确认；处理方后台自动回写。最后 audit 只读核验。",
                    "acceptance": "列表=1,2；空态=empty；失败态=error。确认须来自指定真实账号。完成须由原工作区后台写入。",
                    "delivery": delivery}]})
            if "record_id" not in checkpoint:
                checkpoint["record_id"] = flow.publish(read_json(draft_path))["published"][0]["id"]
                write_json(run_dir / "run.json", checkpoint)
            row = flow.tickets.get(checkpoint["record_id"])
            if not row.get("跟进人"):
                flow.claim(row["id"], requirement(row))
                row = flow.tickets.get(row["id"])
            if row["状态"] == "待对接":
                flow.start(row["id"], ["service.py"], requirement(row))
            row = flow.tickets.get(row["id"])
            if row["状态"] == "处理中":
                write_json(run_dir / "result.json", result(flow))
                arguments = ["ready", row["id"], str(run_dir / "result.json")]
            elif row["状态"] == "待验收" and flow.session(row["id"]).get("auto_accept"):
                arguments = ["auto-accept"]
            else:
                raise HandoffError("auto_test_state_changed", "测试工单状态不符合准备阶段，未重新提交。")
        else:
            row = flow.tickets.get(checkpoint["record_id"])
            if row["结果版本"] != checkpoint["result_version"]:
                raise HandoffError("review_version_changed", "本轮结果版本已变化。")
            arguments = ["auto-accept"]
    launched = cli(repo, run_dir, table_url, arguments)
    with context(repo, run_dir / "processor", table_url, processor_id) as flow:
        row = flow.tickets.get(checkpoint["record_id"])
        checkpoint["result_version"] = row["结果版本"]
        plan = {**marker, "record_id": row["id"], "table_url": table_url,
                "result_version": row["结果版本"], "requirement_version": requirement(row)}
        write_json(run_dir / "plan.json", plan)
        phase = "ready_for_audit" if row["状态"] == "已完成" else "waiting_reviewer"
        checkpoint.update(phase=phase, auto_accept=auto_accept.status(flow.state, flow.namespace, row["id"]),
                          workers=launched.get("auto_accept_workers", []))
        write_json(run_dir / "run.json", checkpoint)
    return checkpoint


def reviewer(repo, table_url, rid):
    directory = prepare_directory(repo) / "auto-acceptance-review" / digest(table_url + "#" + rid)
    with context(repo, directory, table_url) as flow:
        row = flow.tickets.get(rid)
        delivery = flow.show(rid)["delivery"]
        marker = delivery.get("verification_run", {})
        if delivery.get("verification_mode") != FIXTURE:
            raise HandoffError("invalid_auto_test", "此工单不是自动回写联调夹具。")
        if marker.get("reviewer_id") != flow.identity["user_id"] or marker.get("processor_id") == flow.identity["user_id"]:
            raise HandoffError("identity_mismatch", "须由指定的另一真实账号验收。")
        if marker.get("runtime_fingerprint") != runtime_fingerprint(Path(__file__).resolve().parents[1]):
            raise HandoffError("runtime_changed", "安装版本与本轮冻结代码不符，请更新或使用匹配版本。")
        plan = {**marker, "record_id": rid, "table_url": table_url,
                "result_version": row["结果版本"], "requirement_version": requirement(row)}
        saved = read_json(directory / "plan.json", plan)
        if saved != plan:
            raise HandoffError("review_version_changed", "本轮固定的需求或结果版本已变化，需另开联调。")
        write_json(directory / "plan.json", plan)
        flow.sync()  # Recover only this reviewer's own confirmation journal.
        row, evidence = check_plan(flow, plan)
        if row["状态"] == "已完成":
            approval_observation(flow, plan, row, evidence)
            return {"record_id": rid, "phase": "already_completed", "business_acceptance": False}
        report = flow.review(rid)
        if report["local_code"] != "not_checked":
            raise HandoffError("different_workspace_required", "必须在另一工作区执行验收。")
        fixture = (SOURCE + "\n# staged protocol fixture\n").encode("utf-8")
        if (hashlib.sha256(fixture).hexdigest() != evidence["code"].get("service.py", {}).get("sha256")
                or (flow.git.root / "service.py").read_bytes() != fixture):
            raise HandoffError("fixture_changed", "交付证据或本机夹具与可信内置代码不符，未执行或确认。")
        try:
            actual = result(flow)["acceptance"][0]["actual"]
        except subprocess.CalledProcessError:
            flow.reject(rid, "自动回写联调：本机三态检查执行失败。", plan["result_version"], ["render-states"])
            return {"record_id": rid, "phase": "rejected", "business_acceptance": False}
        observed = {**marker, "actor_id": flow.identity["user_id"], "reviewer_workspace": digest(str(flow.git.root)),
                    "local_code": report["local_code"], "actual": actual, "business_acceptance": False}
        flow.confirm(rid, plan["result_version"], decision(report, observed))
        row, evidence = check_plan(flow, plan)
        approval, _ = approval_observation(flow, plan, row, evidence)
        return {"record_id": rid, "phase": "confirmed_waiting_auto_accept", "result_version": plan["result_version"],
                "approval": approval, "actual": actual, "shared_status": row["状态"], "business_acceptance": False}


def audit(repo, run_dir):
    checkpoint, plan = read_json(run_dir / "run.json", {}), read_json(run_dir / "plan.json", {})
    if checkpoint.get("fixture") != FIXTURE or not plan:
        raise HandoffError("invalid_auto_test", "找不到原处理工作区的联调检查点。")
    if runtime_fingerprint(Path(__file__).resolve().parents[1]) != plan["runtime_fingerprint"]:
        raise HandoffError("runtime_changed", "请使用本轮 runtime 中的冻结脚本读取结果。")
    with context(repo, run_dir / "processor", plan["table_url"], plan["processor_id"]) as flow:
        row, evidence = check_plan(flow, plan)
        if row["结果版本"] != plan["result_version"]:
            raise HandoffError("review_version_changed", "共享表已不是本轮版本。")
        state = auto_accept.status(flow.state, flow.namespace, row["id"])
        report = {"mode": "live_dual_account_auto_acceptance", "business_acceptance": False, **plan,
                  "shared_status": row["状态"], "auto_accept": state, "phase": "waiting", "checked_at": now()}
        if row["状态"] == "已完成":
            approval, observed = approval_observation(flow, plan, row, evidence)
            job = auto_accept.jobs(flow.state).get(digest([flow.key(row["id"]), plan["result_version"]]), {})
            logs = flow.logs.records(equals={"工单": [row["id"]]})
            completions = [r for r in logs if r.get("标题") == "验收通过" and plan["result_version"] in r.get("内容", "")]
            reviewer_person = [{"userId": plan["reviewer_id"], "corpId": flow.identity["corp_id"]}]
            if (job.get("status") != "completed" or flow.session(row["id"]).get("phase") != "completed"
                    or row.get("验收人") != reviewer_person or len(completions) != 1
                    or completions[0].get("操作者") != flow.person
                    or any(r.get("标题") == "验收退回" for r in logs)):
                raise HandoffError("auto_test_incomplete", "后台完成记录、实际验收人或历史不符合本轮联调。")
            flow._check_evidence(row)
            report.update(phase="complete", approval=approval, observed=observed,
                          history_events=[r["标题"] for r in logs])
    write_json(run_dir / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    for name in ("prepare", "reviewer", "audit"):
        command = sub.add_parser(name)
        command.add_argument("--repo", required=True, help="本人已有授权的项目")
        if name != "audit":
            command.add_argument("--execute", required=True, action="store_true")
            command.add_argument("--table-url", required=True)
        if name == "reviewer":
            command.add_argument("--record-id", required=True)
        else:
            command.add_argument("--run-dir", required=True)
        if name == "prepare":
            command.add_argument("--reviewer-id", required=True)
    args = parser.parse_args()
    try:
        repo = Path(args.repo).resolve()
        if args.phase == "prepare":
            data = prepare(repo, Path(args.run_dir).resolve(), args.table_url, args.reviewer_id)
        elif args.phase == "reviewer":
            data = reviewer(repo, args.table_url, args.record_id)
        else:
            data = audit(repo, Path(args.run_dir).resolve())
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, indent=2))
        return 0
    except HandoffError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message, **exc.details}}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
