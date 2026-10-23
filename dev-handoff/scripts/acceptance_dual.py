#!/usr/bin/env python3
"""Phased live two-account exercise. Each participant uses their OWN local authorization.

Only explicitly marked test tickets are created. No credentials or execution state
are included in the portable reviewer package. Reviewer actions run on their machine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from acceptance_smoke import CHECK, workspace
from handoff_core import HandoffError, State, digest, now, read_json, requirement, write_json
from tencent_mcp import TencentMCP, load_token, prepare_directory
from tencent_tables import identity, bind, tables
from workflow import Workflow

FIXTURE = "live_dual_acceptance_v1"
ALIAS = "真实双账号联调"


def runtime_fingerprint(skill):
    return digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((skill / "scripts").glob("*.py"))})


def context(repo, run_dir, table_url, expected_actor=None):
    client = TencentMCP(load_token(repo))
    who = identity(client)
    if expected_actor and who["user_id"] != expected_actor:
        raise HandoffError("identity_mismatch", "当前 Token 的真实账号与本阶段指定参与人不符，未继续操作。", expected=expected_actor, actual=who["user_id"])
    git = workspace(run_dir / "workspace")
    state = State(git, run_dir / "state")
    binding = state.select(ALIAS) if state.project["bindings"] else bind(client, state, who, table_url, ALIAS)
    if binding["url"] != table_url:
        raise HandoffError("table_mismatch", "联调目录已经绑定另一张表，不能替换。")
    tickets, logs, members = tables(client, binding, who)
    flow = Workflow(git, state, who, binding, tickets, logs, members)
    flow.registered_member(who["user_id"])
    flow.sync()
    return flow, who


def expect_error(action, code):
    try:
        action()
    except HandoffError as exc:
        if exc.code == code:
            return code
        raise
    raise HandoffError("dual_guard_failed", "联调操作未被预期保护拒绝。", expected=code)


def result(flow):
    check = subprocess.run([sys.executable, "-c", CHECK], cwd=flow.git.root, check=True, capture_output=True, text=True)
    return {"summary": "真实双账号协议联调夹具；非人工业务验收", "checks": [
        {"id": "render-check", "kind": "local", "command": sys.executable + " -c " + CHECK, "outcome": "passed"}],
        "acceptance": [{"criterion_id": "render-states", "actual": check.stdout.strip(),
                        "evidence": [{"check_id": "render-check", "content": check.stdout.strip() + "\nexit_code=0"}]}]}


def decision(report, observed):
    return {"result_version": report["result_version"], "items": [
        {"criterion_id": c["id"], "outcome": "passed", "observed": json.dumps(observed, ensure_ascii=False, sort_keys=True),
         "evidence_digests": [e["digest"] for e in c["evidence"]]} for c in report["acceptance_matrix"]]}


def check_plan(flow, plan):
    row = flow.tickets.get(plan["record_id"])
    if requirement(row) != plan["requirement_version"]:
        raise HandoffError("requirements_changed", "联调需求已变化，不能沿用本轮计划。")
    evidence = flow._shared_evidence(row)
    marker = flow.show(row["id"])["delivery"].get("verification_run")
    expected = {key: plan[key] for key in ("run_id", "runtime_fingerprint", "processor_id", "reviewer_id")}
    if marker != expected:
        raise HandoffError("dual_plan_mismatch", "共享工单与联调包的身份或代码版本不符。")
    return row, evidence


def approval_observation(flow, plan, row, evidence):
    approval = flow._approval(row, evidence, required=True)
    logged = flow.logs.records(equals={"_事件键": approval["event_key"]})[0]
    event = json.loads(logged["内容"])
    observations = [json.loads(item["observed"]) for item in event["items"]]
    for value in observations:
        if (value.get("run_id") != plan["run_id"] or value.get("actor_id") != plan["reviewer_id"]
                or value.get("runtime_fingerprint") != plan["runtime_fingerprint"]
                or value.get("reviewer_workspace") == evidence["workspace"] or value.get("local_code") != "not_checked"):
            raise HandoffError("dual_observation_missing", "指定账号的确认没有对应本轮跨工作区联调证据。")
    return approval, observations


def create_package(run_dir, plan, flow):
    package = run_dir / "package"
    source = Path(__file__).resolve().parents[1]
    target = package / "dev-handoff"
    if not target.exists():
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if runtime_fingerprint(target) != plan["runtime_fingerprint"]:
        raise HandoffError("runtime_changed", "联调快照与当前执行代码不符，不能混用版本。")
    write_json(package / "handoff.json", plan)
    (package / "fixture").mkdir(exist_ok=True)
    (package / "fixture/service.py").write_bytes((flow.git.root / "service.py").read_bytes())
    (package / "开始联调.txt").write_text(
        "codedroll 请在自己的电脑解压，以自己的已授权 Git 项目加载本人 Token；无需转交凭据。\n"
        "第一轮：python3 dev-handoff/scripts/acceptance_dual.py reviewer-first --repo 本人的项目绝对路径 --package . --execute\n"
        "第一轮会确认 V1 再退回，均为协议测试，不代表业务验收；完成后告知处理方。\n"
        "第二轮等处理方提供 V2，再运行：python3 dev-handoff/scripts/acceptance_dual.py reviewer-second --repo 本人的项目绝对路径 --package . --confirmed-version 完整V2 --execute\n"
        "第二轮会验证 V1 确认失效，再确认 V2；最终由原处理工作区回写。\n", encoding="utf-8")
    archive = run_dir / "dev-handoff-dual-reviewer.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(package.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                zipped.write(path, path.relative_to(package))
    return str(archive)


def prepare(repo, run_dir, table_url, reviewer_id):
    if run_dir.exists() and any(run_dir.iterdir()) and not (run_dir / "run.json").is_file():
        raise HandoffError("unsafe_smoke_directory", "目录非空且不属于本联调，不能继续。")
    run_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = read_json(run_dir / "run.json", {"fixture": FIXTURE, "checks": {}})
    if checkpoint.get("fixture") != FIXTURE:
        raise HandoffError("unsafe_smoke_directory", "联调目录类型不符。")
    runtime = runtime_fingerprint(Path(__file__).resolve().parents[1])
    if checkpoint.get("runtime_fingerprint", runtime) != runtime or checkpoint.get("reviewer_id", reviewer_id) != reviewer_id:
        raise HandoffError("runtime_changed", "已有联调不能更换代码版本或指定验收人。")
    if checkpoint.get("phase") in ("waiting_reviewer_second", "complete"):
        return checkpoint
    flow, who = context(repo, run_dir / "processor", table_url, checkpoint.get("processor_id"))
    if who["user_id"] == reviewer_id:
        raise HandoffError("distinct_accounts_required", "真实双账号联调必须使用两个不同的真实账号。")
    flow.registered_member(reviewer_id)
    checkpoint.update({"processor_id": who["user_id"], "reviewer_id": reviewer_id, "table_url": table_url, "runtime_fingerprint": runtime})
    write_json(run_dir / "run.json", checkpoint)
    marker = {"run_id": run_dir.name, "runtime_fingerprint": runtime, "processor_id": who["user_id"], "reviewer_id": reviewer_id}
    draft_path = run_dir / "draft.json"
    if not draft_path.exists():
        delivery = {"apps": ["real-dual-acceptance-test"], "scope": "独立标记的真实双账号联调，不承接业务改动", "deliverable": "验证真实账号权限、跨工作区确认、重提版本隔离和原工作区指纹回写",
                    "acceptance_owner": "指定成员执行协议验收测试，处理方原工作区复核回写；非业务验收", "reviewer_id": reviewer_id,
                    "checks": [{"id": "render-check", "kind": "local", "description": "执行夹具的列表、空态和失败状态检查", "owner": "处理方与验收方分别执行", "required": True}],
                    "external_checks": [], "shared_contracts": [], "verification_run": marker,
                    "criteria": [{"id": "render-states", "description": "夹具行为与预期一致", "steps": [sys.executable + " -c " + CHECK], "expected": "list=1,2; empty=empty; failure=error", "check_ids": ["render-check"]}]}
        write_json(draft_path, {"snapshot": flow.git.staged()["fingerprint"], "tickets": [{
            "key": "dual-acceptance-" + run_dir.name, "title": "[真实双账号联调，非业务验收] 验收权限与版本隔离",
            "description": "仅此测试工单参与联调，其他业务工单不修改；指定验收人在本人电脑操作。", "change_types": ["feat"], "paths": ["service.py"],
            "handoff": "第一轮由指定验收人本人确认 V1 后退回；处理方重提 V2；第二轮验证旧确认失效并确认 V2；最后原工作区复核回写。两端使用同一联调包。",
            "acceptance": "按逐项标准验证夹具，保留两个真实账号的确认、退回与回写记录。", "delivery": delivery}]})
    if "record_id" not in checkpoint:
        checkpoint["record_id"] = flow.publish(read_json(draft_path))["published"][0]["id"]
        write_json(run_dir / "run.json", checkpoint)
    rid = checkpoint["record_id"]
    if "first_version" not in checkpoint:
        row = flow.tickets.get(rid)
        if not row.get("跟进人"):
            flow.claim(rid, requirement(row))
            row = flow.tickets.get(rid)
        if row["状态"] == "待对接":
            flow.start(rid, ["service.py"], requirement(row))
        row = flow.tickets.get(rid)
        checkpoint["first_version"] = row["结果版本"] if row["状态"] == "待验收" else flow.ready(rid, result(flow))["result_version"]
        write_json(run_dir / "run.json", checkpoint)
    report = flow.review(rid)
    if "non_designated_reviewer" not in checkpoint["checks"]:
        error = expect_error(lambda: flow.confirm(rid, checkpoint["first_version"], decision(report, {"test": "unauthorized attempt"})), "not_reviewer")
        checkpoint["checks"]["non_designated_reviewer"] = {"outcome": "passed", "actor_id": who["user_id"], "version": checkpoint["first_version"], "error": error}
    plan = {**marker, "table_url": table_url, "record_id": rid, "first_version": checkpoint["first_version"], "requirement_version": report["requirement_version"]}
    checkpoint["phase"] = "waiting_reviewer_first"
    checkpoint["package"] = create_package(run_dir, plan, flow)
    write_json(run_dir / "run.json", checkpoint)
    write_json(run_dir / "plan.json", plan)
    return checkpoint


def reviewer_phase(repo, package, phase, confirmed_version=None):
    plan = read_json(package / "handoff.json")
    if not isinstance(plan, dict):
        raise HandoffError("invalid_dual_plan", "找不到联调包 handoff.json。")
    if runtime_fingerprint(Path(__file__).resolve().parents[1]) != plan["runtime_fingerprint"]:
        raise HandoffError("runtime_changed", "请执行联调包内的冻结版本脚本。")
    run_dir = prepare_directory(repo) / "dual-acceptance" / plan["run_id"]
    flow, who = context(repo, run_dir, plan["table_url"], plan["reviewer_id"])
    row, evidence = check_plan(flow, plan)
    target = plan["first_version"] if phase == "first" else confirmed_version
    if not target or (phase == "second" and target == plan["first_version"]):
        raise HandoffError("review_version_required", "第二轮必须由处理方提供新的完整结果版本。")
    if row["结果版本"] != target:
        raise HandoffError("review_version_changed", "联调目标版本与共享表当前结果不符。")
    logs = flow.logs.records(equals={"工单": [row["id"]]})
    if phase == "first" and row["状态"] == "待对接":
        approval_observation(flow, plan, row, evidence)
        require_rejection(logs, plan, target)
        return {"phase": "first_already_done", "actor_id": who["user_id"], "result_version": target}
    if phase == "second" and row["状态"] == "已完成":
        approval_observation(flow, plan, row, evidence)
        return {"phase": "already_finalized", "actor_id": who["user_id"], "result_version": target}
    report = flow.review(row["id"])
    if report["local_code"] != "not_checked":
        raise HandoffError("different_workspace_required", "本次要求在独立验收工作区查看材料。")
    fixture = (package / "fixture/service.py").read_bytes()
    if hashlib.sha256(fixture).hexdigest() != evidence["code"].get("service.py", {}).get("sha256"):
        raise HandoffError("fixture_changed", "联调包夹具与当前交付代码指纹不符。")
    if (flow.git.root / "service.py").read_bytes() != fixture:
        raise HandoffError("fixture_changed", "本地验收夹具发生变化，未自动覆盖。")
    observed = {"run_id": plan["run_id"], "actor_id": who["user_id"], "runtime_fingerprint": plan["runtime_fingerprint"],
                "reviewer_workspace": digest(str(flow.git.root)), "local_code": report["local_code"],
                "actual": result(flow)["acceptance"][0]["actual"], "business_acceptance": False}
    if phase == "second":
        observed["old_confirmation_rejected"] = expect_error(lambda: flow.confirm(row["id"], plan["first_version"], {"result_version": plan["first_version"], "items": []}), "review_version_changed")
    observed["foreign_finalize_rejected"] = expect_error(lambda: flow.accept(row["id"], target), "owner_changed")
    flow.confirm(row["id"], target, decision(report, observed))
    if phase == "first":
        flow.reject(row["id"], "真实双账号协议联调：主动退回已确认 V1，验证重提后旧确认不可复用；非业务缺陷。", target, ["render-states"])
    receipt = {"phase": phase + "_done", "actor_id": who["user_id"], "record_id": row["id"], "result_version": target,
               "observed": observed, "next": "告知处理方本轮已完成；无需传递 Token 或本地恢复状态。"}
    write_json(run_dir / (phase + "-receipt.json"), receipt)
    return receipt


def require_rejection(logs, plan, version):
    for row in logs:
        if row.get("标题") != "验收退回":
            continue
        event = json.loads(row["内容"])
        if (event.get("type") == "acceptance_rejection_v1" and event.get("result_version") == version
                and event.get("actor_id") == plan["reviewer_id"]
                and row.get("操作者") == [{"userId": plan["reviewer_id"], "corpId": "tencent-personal"}]):
            return
    raise HandoffError("waiting_reviewer", "尚未读取到指定真实账号对 V1 的退回记录。")


def processor_phase(repo, run_dir, phase):
    checkpoint, plan = read_json(run_dir / "run.json"), read_json(run_dir / "plan.json")
    if not checkpoint or checkpoint.get("fixture") != FIXTURE or not plan:
        raise HandoffError("invalid_dual_plan", "缺少原处理方联调检查点。")
    if runtime_fingerprint(Path(__file__).resolve().parents[1]) != plan["runtime_fingerprint"]:
        raise HandoffError("runtime_changed", "请使用 run-dir/package 中冻结的脚本继续。")
    flow, who = context(repo, run_dir / "processor", plan["table_url"], plan["processor_id"])
    row, evidence = check_plan(flow, plan)
    if phase == "resubmit":
        if "second_version" not in checkpoint:
            if row["结果版本"] == plan["first_version"]:
                approval, _ = approval_observation(flow, plan, row, evidence)
                require_rejection(flow.logs.records(equals={"工单": [row["id"]]}), plan, plan["first_version"])
                checkpoint["checks"]["designated_cross_workspace_first"] = {"outcome": "passed", **approval}
                write_json(run_dir / "run.json", checkpoint)
                if row["状态"] == "待对接":
                    flow.start(row["id"], ["service.py"], requirement(row))
                row = flow.tickets.get(row["id"])
            if "designated_cross_workspace_first" not in checkpoint["checks"]:
                raise HandoffError("dual_observation_missing", "缺少第一轮真实验收人的确认与退回证据，不能推断联调成功。")
            checkpoint["second_version"] = row["结果版本"] if row["状态"] == "待验收" else flow.ready(row["id"], result(flow))["result_version"]
            if checkpoint["second_version"] == plan["first_version"]:
                raise HandoffError("dual_guard_failed", "重提复用了旧结果版本。")
            checkpoint["checks"]["old_approval_not_reused"] = {"outcome": "passed", "error": expect_error(lambda: flow.accept(row["id"], checkpoint["second_version"]), "confirmation_missing")}
        checkpoint["phase"] = "waiting_reviewer_second"
    else:
        if not checkpoint.get("second_version") or row["结果版本"] != checkpoint["second_version"]:
            raise HandoffError("review_version_changed", "当前交付不是本次第二轮版本。")
        approval, observations = approval_observation(flow, plan, row, evidence)
        if any(value.get("old_confirmation_rejected") != "review_version_changed" for value in observations):
            raise HandoffError("dual_observation_missing", "指定账号尚未提交旧版本确认被拒的联调证据。")
        checkpoint["checks"]["designated_cross_workspace_second"] = {"outcome": "passed", **approval}
        if row["状态"] != "已完成":
            source = flow.git.root / "service.py"
            original = source.read_bytes()
            try:
                source.write_bytes(original + b"\n# dual-account guard exercise\n")
                error = expect_error(lambda: flow.accept(row["id"], checkpoint["second_version"]), "code_changed")
            finally:
                source.write_bytes(original)
            checkpoint["checks"]["original_workspace_fingerprint"] = {"outcome": "passed", "error": error}
            write_json(run_dir / "run.json", checkpoint)
            result(flow)
            flow.accept(row["id"], checkpoint["second_version"])
        completed = flow.tickets.get(row["id"])
        if completed.get("验收人") != [{"userId": plan["reviewer_id"], "corpId": who["corp_id"]}]:
            raise HandoffError("dual_readback_failed", "回写的实际验收人不符。")
        flow._check_evidence(completed)
        checkpoint["phase"] = "complete"
        checkpoint["completed_at"] = now()
    write_json(run_dir / "run.json", checkpoint)
    if checkpoint["phase"] == "complete":
        required = {"non_designated_reviewer", "designated_cross_workspace_first", "old_approval_not_reused", "designated_cross_workspace_second", "original_workspace_fingerprint"}
        if not required.issubset(checkpoint["checks"]):
            raise HandoffError("dual_observation_missing", "联调记录不完整，不能报告全部通过。")
        write_json(run_dir / "report.json", {"mode": "live_dual_account_cross_workspace", "business_acceptance": False,
                   "table_url": plan["table_url"], "record_id": row["id"], "processor_id": plan["processor_id"], "reviewer_id": plan["reviewer_id"],
                   "first_version": plan["first_version"], "final_version": checkpoint["second_version"],
                   "completed_at": checkpoint["completed_at"], "checks": checkpoint["checks"],
                   "history_events": [r["标题"] for r in flow.logs.records(equals={"工单": [row["id"]]})]})
    return checkpoint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    for name in ("prepare", "resubmit", "finalize", "reviewer-first", "reviewer-second"):
        command = sub.add_parser(name)
        command.add_argument("--repo", required=True, help="仅使用本人此项目中的既有授权")
        command.add_argument("--execute", action="store_true", required=True, help="明确执行标记为测试的真实表格操作")
        if name.startswith("reviewer-"):
            command.add_argument("--package", default=".")
            if name.endswith("second"):
                command.add_argument("--confirmed-version", required=True)
        else:
            command.add_argument("--run-dir", required=True)
            if name == "prepare":
                command.add_argument("--table-url", required=True)
                command.add_argument("--reviewer-id", required=True)
    args = parser.parse_args()
    try:
        if args.phase == "prepare":
            data = prepare(Path(args.repo).resolve(), Path(args.run_dir).resolve(), args.table_url, args.reviewer_id)
        elif args.phase.startswith("reviewer-"):
            data = reviewer_phase(Path(args.repo).resolve(), Path(args.package).resolve(), args.phase.split("-")[1], getattr(args, "confirmed_version", None))
        else:
            data = processor_phase(Path(args.repo).resolve(), Path(args.run_dir).resolve(), args.phase)
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, indent=2))
        return 0
    except HandoffError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message, **exc.details},
                          "next": "保留两端本人的检查点；等待指定账号操作或先 sync，不更换账号、不共享 Token。"}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
